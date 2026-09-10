-- 027_grn_reversal_and_commitment_sources.sql
-- Three inbound-ledger gaps, each a column the writer needed and did not have: a receive reversal that can name its original, a receive that can say what currency it arrived in, and a commitment the recompute was not allowed to zero.
--
-- ONE. A REVERSAL THAT NAMES ITS ORIGINAL
-- =======================================
--
-- `grn.is_reversal` (013) says a receipt is a reversal and says nothing about
-- WHAT it reverses. `_RECOMPUTE_DERIVED_SQL` negates a reversal by that flag,
-- so the ledger effect was right whenever a reversal arrived at all -- but
-- nothing could arrive for the one case Zoho ERP actually produces. ERP has no
-- reversal document for a receive and no list endpoint: a deleted receive
-- "arrives as an absence" on the next PO-anchored re-read
-- (`references/zoho-boundaries.md`), and `sweeps.SweepPoAnchored` was written
-- to re-walk precisely so that absence would be noticed. It was noticed and
-- then nothing happened, because there was no writer for it and no column
-- that could make writing it idempotent.
--
-- `reverses_grn_id` is that column. A reversal raised for an absence carries
-- NO `external_id` -- there is no source document, only the lack of one, and
-- inventing an external id for an absence would make `ux_grn_external_identity`
-- guard a fiction -- so the idempotency key is the ORIGINAL's id instead:
-- `ux_grn_reversal_of` admits exactly one live reversal per original, and the
-- writer's `ON CONFLICT ... DO NOTHING` is what turns the sweep's cycling
-- re-read into a no-op rather than a second reversal every fifteen minutes.
-- An explicit reversal from Books/Inventory, which does carry a document,
-- keeps its `external_id` AND names its original through the same column.
--
-- TWO. A RECEIVE THAT KNOWS ITS CURRENCY
-- ======================================
--
-- `dto.ReceiveDTO` has no `currency_code`, so `record_receive_line` wrote the
-- payload's figure straight into `grn_line.amount_paise` -- INR base paise by
-- declaration -- whatever the purchase order was denominated in. A receive
-- against the seed's own EUR purchase order stood in the ledger at its face
-- value in rupees, which is AUD-H-007 on the receipts side. The writer now
-- derives the currency from the ANCHORING PURCHASE ORDER when the receive
-- does not say, refuses when the order's currency has no rate on file, and
-- keeps the vendor's own figure beside the translated one exactly as 025 did
-- for bills. Same columns, same immutability, same reason.
--
-- THREE. A COMMITMENT THE RECOMPUTE MAY NOT ZERO (H-7)
-- ====================================================
--
-- `recompute_commitment` derives `commitment_paise` from `po_line` and writes
-- the result over whatever the cell held. That is correct for every commitment
-- that ORIGINATES in a PostgreSQL purchase order and wrong for the one kind
-- that does not: a commitment CARRIED IN -- from the SAP cutover, from the
-- SQLite estate, from an opening balance seeded directly into the ledger --
-- has no `po_line` behind it, so the first recompute on its cell derived zero
-- from nothing and wrote zero over a real obligation. Availability rose by the
-- amount zeroed. That is the permissive direction, and it was live: the demo
-- seed carries exactly such cells.
--
-- The sources of commitment this product knows are therefore TWO, and the
-- recompute is now per-source:
--
--   PO_LINE   derived, `GREATEST(0, ordered - billed)` per open purchase-order
--             line, recomputed in full by `_RECOMPUTE_DERIVED_SQL` on every
--             pass (unchanged);
--   CARRIED   NOT derivable from any PostgreSQL document, held in
--             `commitment_carried_paise` below, NEVER written by the
--             recompute, released only by an explicit correction that says
--             why.
--
-- `commitment_paise` is the sum of the two. A cell that carries no
-- PostgreSQL `po_line` at all cannot have derived any part of its commitment
-- from one, so the backfill moves such a cell's WHOLE stored commitment into
-- the carried bucket -- that is the rule stated without a second copy of the
-- derivation formula, which is the thing `tests/test_ledger_cell_writers.py`
-- exists to forbid. A cell that does carry `po_line` rows has been recomputed
-- from them since 014 and holds nothing else.
--
-- The lead may renumber this file.

BEGIN;

-- ============================================ ONE: reversal names its original
ALTER TABLE grn ADD COLUMN reverses_grn_id text REFERENCES grn (grn_id);
ALTER TABLE grn ADD COLUMN reversal_reason text;

-- A reversal names an original; an original names nothing. Stating it as a
-- CHECK rather than trusting the writer: `is_reversal = false` with a
-- `reverses_grn_id` would be a receipt that claims to be ordinary while
-- pointing at a document it undoes.
ALTER TABLE grn
    ADD CONSTRAINT ck_grn_reversal_names_original CHECK (
        reverses_grn_id IS NULL OR is_reversal
    ),
    ADD CONSTRAINT ck_grn_reversal_is_not_its_own_original CHECK (
        reverses_grn_id IS NULL OR reverses_grn_id <> grn_id
    );

-- ONE LIVE REVERSAL PER ORIGINAL. The PO-anchored sweep cycles by design, so
-- the absence that raised a reversal is observed again on every pass; this is
-- what makes the second observation a no-op. PARTIAL on status so that a
-- reversal later VOIDED -- the original re-appeared at the source -- does not
-- block a fresh one if it disappears again.
CREATE UNIQUE INDEX ux_grn_reversal_of
    ON grn (reverses_grn_id)
    WHERE reverses_grn_id IS NOT NULL AND status <> 'Void';

-- ============================================== TWO: receive knows its currency
ALTER TABLE grn ADD COLUMN source_currency text NOT NULL DEFAULT 'INR';
ALTER TABLE grn ADD COLUMN fx_rate numeric(18,8) NOT NULL DEFAULT 1;
ALTER TABLE grn ADD COLUMN fx_rate_id text;

ALTER TABLE grn
    ADD CONSTRAINT fk_grn_source_currency
        FOREIGN KEY (source_currency)
        REFERENCES currency_denomination (currency_code),
    ADD CONSTRAINT fk_grn_fx_rate
        FOREIGN KEY (fx_rate_id) REFERENCES fx_rate (fx_rate_id),
    ADD CONSTRAINT ck_grn_fx_rate_positive CHECK (fx_rate > 0),
    -- The shape `ck_bill_fx_provenance` (023) requires of a bill: the base
    -- currency is the identity and names no rate row; anything else names one.
    ADD CONSTRAINT ck_grn_fx_provenance CHECK (
        (source_currency = 'INR' AND fx_rate = 1 AND fx_rate_id IS NULL)
        OR (source_currency <> 'INR' AND fx_rate_id IS NOT NULL)
    );

ALTER TABLE grn_line ADD COLUMN source_amount_minor bigint;

-- The twin of `trg_bill_line_source_amount_immutable` (023:557). A source
-- figure, once written, is the evidence the base figure was derived from. A
-- revised receive is refused at the SERVICE with a coded outcome before this
-- fires; this is the backstop for a caller that reaches the table another way.
CREATE OR REPLACE FUNCTION grn_line_source_amount_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source_amount_minor IS NOT NULL
       AND NEW.source_amount_minor IS DISTINCT FROM OLD.source_amount_minor THEN
        RAISE EXCEPTION
            'grn_line %: source_amount_minor is immutable (% to %). The '
            'source amount is the evidence the INR figure was derived from; '
            'overwriting it makes the translation unverifiable.',
            OLD.grn_line_id, OLD.source_amount_minor, NEW.source_amount_minor
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_grn_line_source_amount_immutable
    BEFORE UPDATE ON grn_line
    FOR EACH ROW EXECUTE FUNCTION grn_line_source_amount_is_immutable();

-- ================================== THREE: the commitment source that is kept
ALTER TABLE budget_ledger_cell
    ADD COLUMN commitment_carried_paise bigint NOT NULL DEFAULT 0;
ALTER TABLE budget_ledger_cell
    ADD COLUMN commitment_carried_note text;

ALTER TABLE budget_ledger_cell
    ADD CONSTRAINT ck_ledger_commitment_carried_nonneg
        CHECK (commitment_carried_paise >= 0),
    -- A carried figure says where it came from, or it is a number nobody can
    -- explain sitting in the exposure that gates every spend.
    ADD CONSTRAINT ck_ledger_commitment_carried_is_attributed CHECK (
        commitment_carried_paise = 0
        OR btrim(coalesce(commitment_carried_note, '')) <> ''
    );

-- THE BACKFILL, stated as the rule above: a cell with no PostgreSQL po_line
-- cannot have derived any part of its commitment from one. Whatever it holds
-- is carried, and is kept rather than zeroed on the next recompute.
UPDATE budget_ledger_cell bl
   SET commitment_carried_paise = bl.commitment_paise,
       commitment_carried_note =
           'Carried at migration 027: the cell held this commitment with no '
           'PostgreSQL po_line behind it, so no recompute could have derived '
           'it and none may zero it. Release it through an explicit '
           'correction that records why.'
 WHERE bl.commitment_paise > 0
   AND NOT EXISTS (
        SELECT 1 FROM po_line pl
        WHERE pl.wbs_id = bl.wbs_id
          AND pl.budget_head_id = bl.budget_head_id
   );

-- ==================================================================== comments
COMMENT ON COLUMN grn.reverses_grn_id IS
    'The ORIGINAL receipt this reversal undoes, or NULL on an ordinary receipt. Set with is_reversal = true (ck_grn_reversal_names_original). A reversal raised because the receive DISAPPEARED from its purchase order on a PO-anchored re-read (Zoho ERP has no reversal document for receives) carries external_id NULL -- there is no source document, only the absence of one -- and is made idempotent by ux_grn_reversal_of on this column instead. Written by app/backend/pg/procurement.py::reverse_absent_receives and ::record_receive_line.';
COMMENT ON COLUMN grn.source_currency IS
    'The currency the receipt is denominated in. Derived from the anchoring purchase order when the receive does not say (dto.ReceiveDTO carries none). INR means the identity translation: fx_rate = 1, no fx_rate row, and grn_line.source_amount_minor left NULL.';
COMMENT ON COLUMN grn_line.source_amount_minor IS
    'The IMMUTABLE SOURCE-DOCUMENT amount in grn.source_currency''s own minor units, or NULL on an INR receipt. grn_line.amount_paise is the TRANSLATED figure, INR base paise, derived from this at grn.fx_rate. trg_grn_line_source_amount_immutable refuses to change it once written.';
COMMENT ON COLUMN budget_ledger_cell.commitment_carried_paise IS
    'The part of commitment_paise that does NOT originate in a PostgreSQL po_line and therefore CANNOT be recomputed: an opening balance, a SAP-cutover figure, a commitment migrated from the SQLite estate. procurement_services._RECOMPUTE_DERIVED_SQL ADDS this to the po_line-derived figure and never writes it (H-7). Non-zero only with a commitment_carried_note saying where it came from.';

-- Migration 027 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS, PLAINLY. `commitment_carried_paise` is the only
--   -- record of which part of a cell's commitment was carried in rather than
--   -- derived; dropping it folds that figure back into a column the next
--   -- recompute overwrites with the po_line derivation, which is H-7 exactly.
--   -- Export it first. `grn.reverses_grn_id` is the only link from a reversal
--   -- to its original; the reversal rows and their ledger effect survive, the
--   -- link does not. `grn_line.source_amount_minor` is the evidence a
--   -- translated receipt was derived from; `amount_paise` stays translated
--   -- and becomes unverifiable.
--   DROP TRIGGER IF EXISTS trg_grn_line_source_amount_immutable ON grn_line;
--   DROP FUNCTION IF EXISTS grn_line_source_amount_is_immutable();
--   COMMENT ON COLUMN budget_ledger_cell.commitment_carried_paise IS NULL;
--   COMMENT ON COLUMN grn_line.source_amount_minor IS NULL;
--   COMMENT ON COLUMN grn.source_currency IS NULL;
--   COMMENT ON COLUMN grn.reverses_grn_id IS NULL;
--   ALTER TABLE budget_ledger_cell
--       DROP CONSTRAINT IF EXISTS ck_ledger_commitment_carried_is_attributed,
--       DROP CONSTRAINT IF EXISTS ck_ledger_commitment_carried_nonneg,
--       DROP COLUMN IF EXISTS commitment_carried_note,
--       DROP COLUMN IF EXISTS commitment_carried_paise;
--   ALTER TABLE grn_line DROP COLUMN IF EXISTS source_amount_minor;
--   ALTER TABLE grn
--       DROP CONSTRAINT IF EXISTS ck_grn_fx_provenance,
--       DROP CONSTRAINT IF EXISTS ck_grn_fx_rate_positive,
--       DROP CONSTRAINT IF EXISTS fk_grn_fx_rate,
--       DROP CONSTRAINT IF EXISTS fk_grn_source_currency,
--       DROP COLUMN IF EXISTS fx_rate_id,
--       DROP COLUMN IF EXISTS fx_rate,
--       DROP COLUMN IF EXISTS source_currency;
--   DROP INDEX IF EXISTS ux_grn_reversal_of;
--   ALTER TABLE grn
--       DROP CONSTRAINT IF EXISTS ck_grn_reversal_is_not_its_own_original,
--       DROP CONSTRAINT IF EXISTS ck_grn_reversal_names_original,
--       DROP COLUMN IF EXISTS reversal_reason,
--       DROP COLUMN IF EXISTS reverses_grn_id;
--   DELETE FROM schema_migrations WHERE version = '027';
--   COMMIT;
