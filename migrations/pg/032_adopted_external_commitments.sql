-- 032_adopted_external_commitments.sql
-- Adopting a tenant-raised Zoho order: the columns that let a purchase order
-- exist locally WITHOUT having been proposed, budget-checked or emitted by
-- this system, and the two reconciliation-exception kinds that hold a tenant
-- order this system cannot honour.
--
-- THE PRODUCT DECISION THIS TRANSCRIBES
-- ======================================
--
-- Product owner, 2026-09-12: "Adopt the seven tenant-raised Zoho demo orders
-- into local WBS orders using their stamped line fields." Recorded in
-- `docs/fable51/STAGE_B_PERSISTENT_UAT.md`, "Findings from the first live
-- sweeps" (2026-09-12), finding 1: `SweepPoAnchored` anchors the receive walk
-- on `purchase_order` rows this system raised (`external_id` set for the
-- connection). The seven demo orders were created IN THE TENANT, not emitted
-- from here, so the walk found nothing to anchor on. Adoption is the "(a)"
-- option that finding named: create the local order from the tenant's own
-- stamped fields, so a receive or a bill against it has something to resolve
-- against.
--
-- WHY THIS IS A NEW COLUMN SET AND NOT A FLAG
-- ============================================
--
-- An adopted order is not an ordinary local one wearing a marker. It was
-- never proposed through a purchase request, never budget-checked before it
-- existed (`app/backend/integration/adoption.py` deliberately calls the
-- writer beneath `create_po`'s budget gate, never the gate itself), and its
-- existence in this ledger is a DETECTIVE record of an external fact, not a
-- SANCTIONING one. `commitment_origin` names that difference where every
-- report, every screen and every future migration can read it without
-- re-deriving it from `external_id IS NOT NULL`, which would also be true of
-- an order this system emitted and later re-synced -- a genuinely different
-- fact this column does not conflate.
--
-- `adopted_from_inbox_id` / `adopted_at` / `adopted_by` are the adoption's own
-- provenance, alongside the `external_source` / `external_id` §6.1 already
-- gives every mirrored document (013). `adopted_from_inbox_id` points at the
-- exact `integration_inbox` row the adoption read, so the source document is
-- recoverable (§11.10) the same way a bill or a GRN's `payload_sha` makes one
-- recoverable -- and it is an FK, not a copied string, so a row that was
-- somehow deleted upstream is refused rather than silently orphaned.
--
-- `external_capex_ref` carries the tenant's own `cf_capex_ref` value verbatim
-- (never the dedupe key this system mints when it emits an order -- see
-- `outbound.derive_dedupe_key`, which builds a DIFFERENT string for a
-- DIFFERENT direction). It is what lets a repeat adoption sweep tell "this
-- order again, unchanged" from "this order again, and the tenant changed
-- something" without re-deriving the value from `po_line`, and it is unique
-- per `external_source` so two Zoho orders cannot be adopted under the same
-- CAPEX reference (`ADOPTION_DIMENSION_INVALID` is raised first, in
-- application code, for the readable refusal; the index is the backstop).
--
-- THE UNIQUE GUARANTEE ON (external_source, external_id) ALREADY EXISTS
-- ========================================================================
--
-- `ux_po_external` (013) is already a UNIQUE index on
-- `(external_source, external_id) WHERE external_id IS NOT NULL`. Adoption
-- writes through the SAME `external_source` / `external_id` columns every
-- other mirrored purchase order does, so it is already covered and nothing
-- is added here for that guarantee -- adding a second, differently-named
-- index over the same two columns would not strengthen it and would only
-- invite the two to drift.
--
-- THE TWO NEW EXCEPTION KINDS
-- ============================
--
-- `ADOPTION_DIMENSION_INVALID` -- an order's `cf_capex_ref`, `cf_wbs_code` or
-- `cf_budget_head` is missing, does not resolve to exactly one row in the
-- connection's entity, resolves lines to more than one project, or names a
-- `cf_capex_ref` another local order already carries. Raised naming the
-- order and the line; NOTHING is written -- no local purchase order, no
-- `po_line`, no control-cell exposure. `FOREIGN_CURRENCY_BASIS_MISSING`
-- (030) is NOT reused for a foreign-currency adoption candidate with no
-- active rate on file; that case is held under `FOREIGN_CURRENCY_BASIS_
-- MISSING` itself, unchanged, because it is the same fact (030) already
-- names precisely.
--
-- `ADOPTION_DIMENSION_CONFLICT` -- a REPEAT adoption sweep finds the local
-- order already adopted (by `external_source`/`external_id`) but the
-- tenant's own `cf_capex_ref`, `cf_wbs_code` or `cf_budget_head` values have
-- changed since. The local order is left exactly as it was; adoption never
-- re-writes a posted commitment out from under itself, for the same reason
-- `trg_purchase_order_fx_basis_immutable` (029) refuses to re-base one.
--
-- Nothing here edits a byte of 001..031. Additive: five columns and two
-- CHECKs on `purchase_order`, one CHECK dropped and re-added on
-- `reconciliation_exception` with two more members under the SAME name (030's
-- own precedent), comments.

BEGIN;

-- ============================================== purchase_order: provenance
ALTER TABLE purchase_order
    ADD COLUMN commitment_origin     text NOT NULL DEFAULT 'LOCAL',
    ADD COLUMN adopted_from_inbox_id text REFERENCES integration_inbox (inbox_id),
    ADD COLUMN adopted_at            timestamptz,
    ADD COLUMN adopted_by            text,
    ADD COLUMN external_capex_ref    text;

ALTER TABLE purchase_order
    ADD CONSTRAINT ck_purchase_order_commitment_origin CHECK (
        commitment_origin IN ('LOCAL', 'EXTERNAL_UNSANCTIONED')
    ),
    -- The biconditional: an adopted order carries ALL FIVE of its own
    -- provenance columns filled, and an ordinary local order carries NONE of
    -- them -- the same shape `ck_purchase_order_fx_provenance` (029) gives
    -- an INR order versus a foreign one, applied to adoption instead of
    -- currency.
    ADD CONSTRAINT ck_purchase_order_adoption_provenance CHECK (
        (commitment_origin = 'LOCAL'
         AND adopted_from_inbox_id IS NULL
         AND adopted_at IS NULL
         AND adopted_by IS NULL
         AND external_capex_ref IS NULL)
        OR
        (commitment_origin = 'EXTERNAL_UNSANCTIONED'
         AND external_source IS NOT NULL
         AND external_id IS NOT NULL
         AND adopted_from_inbox_id IS NOT NULL
         AND adopted_at IS NOT NULL
         AND btrim(coalesce(adopted_by, '')) <> ''
         AND btrim(coalesce(external_capex_ref, '')) <> '')
    );

-- Two adopted orders under the same tenant CAPEX reference is a duplicate
-- adoption, not a second order. PARTIAL, so every non-adopted row (NULL) is
-- unconstrained, exactly as `ux_po_external` waives a locally-raised order
-- with no external identity yet.
CREATE UNIQUE INDEX ux_po_external_capex_ref
    ON purchase_order (external_source, external_capex_ref)
    WHERE external_capex_ref IS NOT NULL;

COMMENT ON COLUMN purchase_order.commitment_origin IS
    'LOCAL: proposed through this system''s own PR/PO flow and budget-checked before it existed. EXTERNAL_UNSANCTIONED: adopted from a tenant-raised Zoho order this system never proposed or budget-checked (app/backend/integration/adoption.py); its exposure is a detective record of an external fact, not a sanctioning one.';
COMMENT ON COLUMN purchase_order.adopted_from_inbox_id IS
    'The integration_inbox row the adoption read this order''s existence from. NULL for a LOCAL order. An FK, not a copied string, so the source document stays recoverable (section 11.10) or the reference is refused, never silently orphaned.';
COMMENT ON COLUMN purchase_order.adopted_at IS
    'When adoption wrote this order. NULL for a LOCAL order.';
COMMENT ON COLUMN purchase_order.adopted_by IS
    'The actor who ran the adoption sweep. NULL for a LOCAL order.';
COMMENT ON COLUMN purchase_order.external_capex_ref IS
    'The tenant''s OWN cf_capex_ref value, verbatim, for an EXTERNAL_UNSANCTIONED order. NOT the dedupe key this system mints when IT emits an order (outbound.derive_dedupe_key) -- that is a different string for a different direction. Unique per external_source (ux_po_external_capex_ref) so two adopted orders cannot share one tenant reference, and it is what a repeat adoption sweep compares to detect ADOPTION_DIMENSION_CONFLICT.';

-- ============================================ the ledger admits two more kinds
ALTER TABLE reconciliation_exception
    DROP CONSTRAINT ck_reconciliation_exception_kind;

ALTER TABLE reconciliation_exception
    ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
        kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
                 'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
                 'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING',
                 'ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT')
    );

COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS
    'C18''s exception kinds: the five frozen in 011, FOREIGN_CURRENCY_BASIS_MISSING (030), and ADOPTION_DIMENSION_INVALID / ADOPTION_DIMENSION_CONFLICT (032, 2026-09-12 product decision "adopt the seven tenant-raised Zoho demo orders"): a tenant order whose cf_capex_ref / cf_wbs_code / cf_budget_head is missing, invalid, ambiguous or has changed since adoption is held here, unbooked, rather than adopted on a guess.';

-- Migration 032 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS. Every adopted purchase order loses the record of
--   -- WHERE it came from (adopted_from_inbox_id/at/by) and the tenant's own
--   -- CAPEX reference (external_capex_ref); its external_source/external_id
--   -- and its lines stay, but nothing distinguishes it from a locally-raised
--   -- order any more, and a repeat adoption sweep run against this schema
--   -- would treat it as never adopted. Every Open ADOPTION_DIMENSION_INVALID
--   -- / ADOPTION_DIMENSION_CONFLICT exception must be resolved or deleted
--   -- first, or the narrowed CHECK cannot be re-added; review them by hand.
--   DELETE FROM reconciliation_exception
--       WHERE kind IN ('ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT');
--   ALTER TABLE reconciliation_exception
--       DROP CONSTRAINT ck_reconciliation_exception_kind;
--   ALTER TABLE reconciliation_exception
--       ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
--           kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
--                    'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
--                    'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING'));
--   COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS
--       'C18''s exception kinds: the five frozen in 011 and FOREIGN_CURRENCY_BASIS_MISSING (030, decision 8 of 2026-09-11): a receive or bill against a non-base-currency purchase order that carries no currency and rate of its own is held here, unbooked, with its face value named in detail and NEVER in a _paise column.';
--   DROP INDEX IF EXISTS ux_po_external_capex_ref;
--   COMMENT ON COLUMN purchase_order.external_capex_ref IS NULL;
--   COMMENT ON COLUMN purchase_order.adopted_by IS NULL;
--   COMMENT ON COLUMN purchase_order.adopted_at IS NULL;
--   COMMENT ON COLUMN purchase_order.adopted_from_inbox_id IS NULL;
--   COMMENT ON COLUMN purchase_order.commitment_origin IS NULL;
--   ALTER TABLE purchase_order
--       DROP CONSTRAINT IF EXISTS ck_purchase_order_adoption_provenance,
--       DROP CONSTRAINT IF EXISTS ck_purchase_order_commitment_origin,
--       DROP COLUMN IF EXISTS external_capex_ref,
--       DROP COLUMN IF EXISTS adopted_by,
--       DROP COLUMN IF EXISTS adopted_at,
--       DROP COLUMN IF EXISTS adopted_from_inbox_id,
--       DROP COLUMN IF EXISTS commitment_origin;
--   DELETE FROM schema_migrations WHERE version = '032';
--   COMMIT;
