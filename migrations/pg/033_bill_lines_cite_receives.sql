-- 033_bill_lines_cite_receives.sql
-- A bill line remembers the receive line it cites, and billing beyond what
-- that receive delivered is a reconciliation exception.
--
-- THE FACT THIS TRANSCRIBES
-- =========================
--
-- VERIFIED LIVE 2026-09-12 on Zoho ERP DEMO WBS (organisation 60074128927):
-- once a purchase order has a receive, a bill against it MUST cite that
-- receive -- each bill line carries `purchaseorder_item_id`, `receive_id` and
-- `receive_item_id`. Zoho enforces the citation; until now this ledger read
-- only the purchase-order line off the bill and threw the receive citation
-- away, so "received, not yet billed" could only be computed per PURCHASE
-- ORDER LINE and a bill for more than a receive delivered was invisible until
-- the whole line over-billed. Decision row "Zoho receive/bill matching rule
-- reflected in the app's matching model" (DECISIONS_2026-09-11.md), closed by
-- this migration and `pg.procurement._mirror_bill_line`.
--
-- WHAT CHANGES
-- ============
--
--   * `bill_line.receive_line_external_id` -- the tenant's receive line id
--     the bill line cited, verbatim (NULL when the source cited none, which is
--     the case for a bill raised before any receive).
--   * `bill_line.grn_line_id` -- the local `grn_line` that citation resolved
--     to, or NULL when the receive has not been mirrored yet (the receive walk
--     runs before the bill-detail sweep, so this is the exception, not the
--     rule) or when the citation named a line this ledger does not hold.
--     Nullable ON PURPOSE: a bill is still attributed to its purchase-order
--     line and posted as actual whether or not its receive is here; the
--     citation refines the picture, it does not gate the posting.
--   * A ninth reconciliation-exception kind, BILL_EXCEEDS_RECEIVE: the bills
--     citing one receive line, summed, exceed what that receive line
--     delivered. Raised on `object_type = 'grn_line'`, `object_id` the local
--     grn_line_id, `local_paise` the excess in base paise. Over-billing stays
--     VISIBLE and unclamped, exactly as `reconcile_po_lines` treats it at the
--     purchase-order line: the bill lines are written, and the excess blocks
--     capitalisation and period close like every Open exception until a
--     credit note or a further receive resolves it (the writer retracts the
--     exception itself when the sum falls back within the receipt).
--
-- Nothing here edits a byte of 001..032. Additive: two nullable columns, one
-- index, and the kind CHECK dropped and re-added with nine members under the
-- SAME name, so `tests/test_pg_reconciliation.py::test_the_python_constants_
-- transcribe_the_migration_rather_than_invent_it` keeps comparing the
-- constant against the constraint by name.

BEGIN;

ALTER TABLE bill_line
    ADD COLUMN receive_line_external_id text,
    ADD COLUMN grn_line_id text REFERENCES grn_line (grn_line_id);

COMMENT ON COLUMN bill_line.receive_line_external_id IS
    'The tenant''s receive line this bill line cited (Zoho ERP receive_item_id), verbatim; NULL when the source cited none (033).';
COMMENT ON COLUMN bill_line.grn_line_id IS
    'The local grn_line the cited receive line resolved to, on the same po_line; NULL when the receive is not mirrored yet or the citation names a line this ledger does not hold. Refines attribution; never gates it (033).';

CREATE INDEX ix_bill_line_grn_line ON bill_line (grn_line_id)
    WHERE grn_line_id IS NOT NULL;

ALTER TABLE reconciliation_exception
    DROP CONSTRAINT ck_reconciliation_exception_kind;

ALTER TABLE reconciliation_exception
    ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
        kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
                 'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
                 'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING',
                 'ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT',
                 'BILL_EXCEEDS_RECEIVE')
    );

COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS
    'C18''s exception kinds: the five frozen in 011, FOREIGN_CURRENCY_BASIS_MISSING (030), ADOPTION_DIMENSION_INVALID / ADOPTION_DIMENSION_CONFLICT (032), and BILL_EXCEEDS_RECEIVE (033): the bills citing one receive line, summed, exceed what that receive line delivered; raised on the local grn_line with the excess in local_paise, visible and unclamped, retracted by the writer when the sum falls back within the receipt.';

-- Migration 033 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Every Open BILL_EXCEEDS_RECEIVE row is a bill the ledger HAS booked
--   -- beyond a receipt; review each before deleting.
--   DELETE FROM reconciliation_exception WHERE kind = 'BILL_EXCEEDS_RECEIVE';
--   ALTER TABLE reconciliation_exception
--       DROP CONSTRAINT ck_reconciliation_exception_kind;
--   ALTER TABLE reconciliation_exception
--       ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
--           kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
--                    'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
--                    'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING',
--                    'ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT'));
--   DROP INDEX IF EXISTS ix_bill_line_grn_line;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS grn_line_id;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS receive_line_external_id;
--   DELETE FROM schema_migrations WHERE version = '033';
--   COMMIT;
