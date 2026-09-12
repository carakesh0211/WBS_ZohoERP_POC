-- 030_foreign_currency_basis_missing.sql
-- A sixth reconciliation-exception kind, FOREIGN_CURRENCY_BASIS_MISSING, for a
-- receive or a bill that arrives against a purchase order denominated in a
-- currency other than the base currency and does not carry a translation
-- basis of its own.
--
-- THE DECISION THIS TRANSCRIBES
-- =============================
--
-- Product owner, 2026-09-11, decision 8 (docs/fable51/DECISIONS_2026-09-11.md):
-- "A receive or bill against a non-INR order is refused with a coded
-- reconciliation exception until it carries its own currency and rate;
-- nothing is booked at face value."
--
-- WHY A NEW KIND AND NOT ONE OF THE FIVE
-- ======================================
--
-- Migration 011 froze five kinds and said a sixth "is a deliberate contract
-- change rather than a typo that silently creates a category nobody triages".
-- This is that deliberate change, and none of the five describes this case:
--
--   * GRN_LINE_UNATTRIBUTED says the LINKAGE is missing. Here the linkage may
--     be perfectly good; what is missing is the basis on which a JPY figure
--     becomes paise. Filing it there would also put the line's face value
--     into the `unattributed_receipts` bucket -- a yen figure in a paise
--     column, which is the very thing decision 8 forbids.
--   * CONTROL_TOTAL_MISMATCH says two counts disagree. Nothing has been
--     counted.
--   * The other three are about periods, sanction and status mapping.
--
-- A receive on Zoho ERP carries no currency and no rate of its own (it
-- inherits the order's), and a bill may arrive with `currency_code` but no
-- usable `exchange_rate` and no ACTIVE rate on file for its date. In both
-- cases the document is held OPEN under this kind -- which, like every Open
-- exception, blocks capitalisation and period close through
-- `closure._open_exceptions` and `periods._has_open_reconciliation_exceptions`
-- -- and NOTHING is written to `grn_line`, `bill` or `bill_line`. The inbound
-- row stays in `integration_inbox`, unmatched, so the source document is
-- still recoverable (section 11.10) when the basis is supplied.
--
-- `local_paise` and `source_paise` are NULL on every row of this kind, by the
-- writer's own rule (`sweeps.SweepPoAnchored._attribute`,
-- `sweeps.SweepBillDetail._mirror`): the only figure available is the
-- vendor's face value in the vendor's currency, and a bigint column named
-- `_paise` is the wrong place for it. The amount and its currency are named
-- in `detail` instead, where they cannot be summed as rupees.
--
-- Nothing here edits a byte of 001..029. Additive: one CHECK dropped and
-- re-added with six members under the SAME name, so
-- `tests/test_pg_reconciliation.py::test_the_python_constants_transcribe_the_
-- migration_rather_than_invent_it` keeps comparing the constant against the
-- constraint by name.

BEGIN;

ALTER TABLE reconciliation_exception
    DROP CONSTRAINT ck_reconciliation_exception_kind;

ALTER TABLE reconciliation_exception
    ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
        kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
                 'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
                 'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING')
    );

COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS
    'C18''s exception kinds: the five frozen in 011 and FOREIGN_CURRENCY_BASIS_MISSING (030, decision 8 of 2026-09-11): a receive or bill against a non-base-currency purchase order that carries no currency and rate of its own is held here, unbooked, with its face value named in detail and NEVER in a _paise column.';

-- Migration 030 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Every Open FOREIGN_CURRENCY_BASIS_MISSING row must be resolved or
--   -- deleted first, or the narrowed CHECK cannot be re-added; review them
--   -- by hand. Each one is a document the ledger has NOT booked.
--   DELETE FROM reconciliation_exception WHERE kind = 'FOREIGN_CURRENCY_BASIS_MISSING';
--   ALTER TABLE reconciliation_exception
--       DROP CONSTRAINT ck_reconciliation_exception_kind;
--   ALTER TABLE reconciliation_exception
--       ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
--           kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
--                    'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
--                    'UNSANCTIONED_COMMITMENT'));
--   COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS NULL;
--   DELETE FROM schema_migrations WHERE version = '030';
--   COMMIT;
