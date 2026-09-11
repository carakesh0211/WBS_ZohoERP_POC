-- 029_po_currency_at_origin.sql
-- A foreign-currency purchase order carries the figures the vendor quoted,
-- in the currency they quoted them in, and the rate it was committed at --
-- written together with the code that writes them.
--
-- WHAT WAS MISSING
-- ================
--
-- `purchase_order` has had `currency` and `exchange_rate` since 013, and
-- migration 025's header explains at length why it gave the purchase order
-- no source-amount columns: `_write_po` was the only writer, it stored BASE
-- paise on every line, and "the columns and their writer arrive together or
-- they do not arrive". This migration is the "together". The rate was
-- multiplied into nothing; the budget check cleared a base figure the caller
-- typed; and the outbound connector, which defaulted `currency_code` to INR,
-- priced the vendor in INR paise -- a number they never quoted. Fable 5.1
-- first made the connector REFUSE a non-INR order (`PO_CURRENCY_NOT_EMITTABLE`)
-- rather than mislabel it; this migration is what lets it emit one.
--
-- WHAT THIS ADDS
-- ==============
--
-- * `po_line.source_amount_minor` and `po_line.source_rate_minor` -- the line
--   total and the per-unit price in the ORDER'S OWN currency's minor units
--   (cents, yen, fils), never paise. NULL on an INR line, NOT NULL on every
--   line of a foreign-currency order, enforced by a trigger that reads the
--   header (a CHECK cannot). Immutable once written, as `bill_line`'s twin
--   columns are (023, 025): the figure the vendor acknowledged does not move.
--
-- * `purchase_order.fx_rate_id`, `fx_rate_date`, `fx_rate_source`,
--   `source_minor_exponent` -- the provenance of the rate `exchange_rate`
--   already held. `ck_purchase_order_fx_provenance` is `ck_bill_fx_provenance`
--   (023) applied to the order: an INR order is the identity translation and
--   names NO rate; a foreign order names the row, the date and the source.
--   `exchange_rate` therefore stops being decorative: it is the rate the
--   ACTIVE `fx_rate` row carried on the order's document date, applied ONCE
--   to the source total and allocated to the lines without loss
--   (`fx.translate_lines`), and `amount_paise` is what that produced.
--
-- * `fx_translation_event` admits `PURCHASE_ORDER`. 025's CHECK named one kind
--   "because one kind has a writer"; now two do. The ledger is what makes
--   "apply a rate exactly once" a primary-key fact for orders as it is for
--   bills.
--
-- * `fk_purchase_order_currency`: `currency` must name a
--   `currency_denomination` row, as `bill.source_currency` must (023). An
--   order in a currency whose minor exponent nobody recorded cannot be
--   translated and is refused at the database as well as in `fx.py`.
--
-- WHAT IT DOES NOT DO
-- ===================
--
-- It does not re-derive anything for rows that exist. `ck_purchase_order_
-- fx_provenance` is added VALIDATED: a pre-029 order in a foreign currency
-- with no rate provenance makes this migration FAIL, by design, with the
-- constraint's own error. Such an order was committed at a base figure the
-- caller typed and a rate that applied to nothing; the right number is not
-- derivable here and is not guessed. The PostgreSQL seed carries no purchase
-- orders, so a seeded database is unaffected; a database that carries one
-- is reviewed by hand before this runs (list them with
-- `SELECT po_number, currency, exchange_rate FROM purchase_order WHERE
-- currency <> 'INR'`).
--
-- Nothing here edits a byte of 001..028. Additive: two columns and three
-- CHECKs on `po_line`, four columns, one FK and one CHECK on
-- `purchase_order`, one CHECK widened on `fx_translation_event`, two trigger
-- functions, two triggers, comments.

BEGIN;

-- ================================================ purchase_order: provenance
ALTER TABLE purchase_order
    ADD COLUMN fx_rate_id            text REFERENCES fx_rate (fx_rate_id),
    ADD COLUMN fx_rate_date          date,
    ADD COLUMN fx_rate_source        text,
    ADD COLUMN source_minor_exponent integer NOT NULL DEFAULT 2;

ALTER TABLE purchase_order
    ADD CONSTRAINT fk_purchase_order_currency
        FOREIGN KEY (currency) REFERENCES currency_denomination (currency_code),
    ADD CONSTRAINT ck_purchase_order_minor_exponent
        CHECK (source_minor_exponent BETWEEN 0 AND 4),
    ADD CONSTRAINT ck_purchase_order_fx_provenance CHECK (
        (currency = 'INR'
         AND exchange_rate = 1 AND fx_rate_id IS NULL AND fx_rate_date IS NULL
         AND fx_rate_source IS NULL AND source_minor_exponent = 2)
        OR (currency <> 'INR'
            AND fx_rate_id IS NOT NULL
            AND fx_rate_date IS NOT NULL
            AND btrim(coalesce(fx_rate_source, '')) <> '')
    );

COMMENT ON COLUMN purchase_order.exchange_rate IS
    'The source-currency -> INR rate this order was COMMITTED at: the ACTIVE fx_rate row (fx_rate_id) for the order''s document date, applied once to the sum of the lines'' source_amount_minor and allocated to the lines by fx.translate_lines so they sum to the header exactly. 1 with no provenance for an INR order (ck_purchase_order_fx_provenance). Before migration 029 this column was stored and multiplied into nothing.';
COMMENT ON COLUMN purchase_order.fx_rate_id IS
    'The fx_rate row exchange_rate came from. NULL only for INR. The row is immutable and append-only (028), so the figure the order was committed at stays verifiable against it after the rate is retired or superseded.';
COMMENT ON COLUMN purchase_order.source_minor_exponent IS
    'Decimal places of `currency` when the order was written (currency_denomination.minor_exponent at that moment): 2 for INR/USD/EUR, 0 for JPY, 3 for KWD. Carried on the row so a later change to the denomination table cannot re-scale a committed order.';

-- ===================================================== po_line: source money
ALTER TABLE po_line
    ADD COLUMN source_rate_minor   bigint,
    ADD COLUMN source_amount_minor bigint;

ALTER TABLE po_line
    ADD CONSTRAINT ck_po_line_source_amount_nonneg
        CHECK (source_amount_minor IS NULL OR source_amount_minor >= 0),
    ADD CONSTRAINT ck_po_line_source_rate_nonneg
        CHECK (source_rate_minor IS NULL OR source_rate_minor >= 0),
    -- Both or neither: a rate with no total, or a total with no rate, is half
    -- of a vendor-facing line.
    ADD CONSTRAINT ck_po_line_source_pair
        CHECK ((source_amount_minor IS NULL) = (source_rate_minor IS NULL));

COMMENT ON COLUMN po_line.source_amount_minor IS
    'The line total in the ORDER''S OWN currency''s minor units -- cents, whole yen, fils -- exactly as quoted and exactly as emitted to the vendor. NULL on every line of an INR order and NOT NULL on every line of a foreign-currency one (trg_po_line_source_matches_header). Immutable once written. amount_paise is derived from it: fx.translate_lines applies purchase_order.exchange_rate once to the document total and allocates the paise without loss.';
COMMENT ON COLUMN po_line.source_rate_minor IS
    'The per-unit price in the order''s own minor units, the figure the connector sends as the Zoho line rate for a foreign-currency order. Exact by construction: source_rate_minor x quantity = source_amount_minor, or the line was refused (PO_LINE_RATE_NOT_EXACT). For such an order rate_paise is the translated per-unit figure and is informational only; the emission never reads it.';

-- ------------------------------------------------ the header decides the shape
CREATE OR REPLACE FUNCTION po_line_source_matches_header() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    header_currency text;
BEGIN
    SELECT currency INTO header_currency
    FROM purchase_order WHERE po_id = NEW.po_id;
    IF header_currency IS NULL THEN
        -- The FK refuses this anyway; say it in this trigger's own words so
        -- the two refusals cannot be told apart by accident.
        RAISE EXCEPTION USING ERRCODE = 'foreign_key_violation',
            MESSAGE = 'po_line ' || NEW.po_line_id || ' names purchase order ' || NEW.po_id || ', which does not exist.';
    END IF;
    IF header_currency = 'INR' THEN
        IF NEW.source_amount_minor IS NOT NULL OR NEW.source_rate_minor IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = 'po_line ' || NEW.po_line_id || ': an INR order is the identity translation; its lines carry amount_paise only. source_amount_minor and source_rate_minor must be NULL.';
        END IF;
    ELSIF NEW.source_amount_minor IS NULL OR NEW.source_rate_minor IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'check_violation',
            MESSAGE = 'po_line ' || NEW.po_line_id || ': purchase order ' || NEW.po_id || ' is in ' || header_currency || ', so every line must carry source_amount_minor and source_rate_minor -- the figures the vendor quoted. amount_paise alone is the translated figure, not the order.';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.source_amount_minor IS DISTINCT FROM OLD.source_amount_minor
           OR NEW.source_rate_minor IS DISTINCT FROM OLD.source_rate_minor THEN
            RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
                MESSAGE = 'po_line ' || OLD.po_line_id || ': source_amount_minor and source_rate_minor are immutable (' || coalesce(OLD.source_amount_minor::text, 'NULL') || ' -> ' || coalesce(NEW.source_amount_minor::text, 'NULL') || '). The figure the vendor acknowledged does not move; an amendment is a new line.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_po_line_source_matches_header
    BEFORE INSERT OR UPDATE ON po_line
    FOR EACH ROW EXECUTE FUNCTION po_line_source_matches_header();

-- A header cannot change currency under its lines: the lines' shape was
-- decided by it. `exchange_rate` and the provenance are written once with the
-- header and never edited -- the same rule ck_bill_fx_provenance's trigger
-- twin (trg_bill_fx_basis_immutable, 023) applies to a bill.
CREATE OR REPLACE FUNCTION purchase_order_fx_basis_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.exchange_rate IS DISTINCT FROM OLD.exchange_rate
       OR NEW.fx_rate_id IS DISTINCT FROM OLD.fx_rate_id
       OR NEW.fx_rate_date IS DISTINCT FROM OLD.fx_rate_date
       OR NEW.source_minor_exponent IS DISTINCT FROM OLD.source_minor_exponent THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = 'purchase_order ' || OLD.po_id || ': currency, exchange_rate and the rate provenance are fixed when the order is written. Re-basing a committed order in place would move its base-currency commitment with nothing recording that it moved.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_purchase_order_fx_basis_immutable
    BEFORE UPDATE ON purchase_order
    FOR EACH ROW EXECUTE FUNCTION purchase_order_fx_basis_is_immutable();

-- ============================================ the ledger admits a second kind
ALTER TABLE fx_translation_event
    DROP CONSTRAINT ck_fx_translation_event_document_type;
ALTER TABLE fx_translation_event
    ADD CONSTRAINT ck_fx_translation_event_document_type
        CHECK (document_type IN ('BILL', 'PURCHASE_ORDER'));

-- Migration 029 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS. Every foreign-currency order written under 029
--   -- loses the figures the vendor quoted (source_amount_minor) and the
--   -- provenance of the rate it was committed at; its amount_paise stays,
--   -- but nothing records how it was derived. The fx_translation_event rows
--   -- of kind PURCHASE_ORDER must be deleted first or the narrowed CHECK
--   -- cannot be re-added. Review them by hand before this runs.
--   DELETE FROM fx_translation_event WHERE document_type = 'PURCHASE_ORDER';
--   ALTER TABLE fx_translation_event
--       DROP CONSTRAINT ck_fx_translation_event_document_type;
--   ALTER TABLE fx_translation_event
--       ADD CONSTRAINT ck_fx_translation_event_document_type
--           CHECK (document_type IN ('BILL'));
--   DROP TRIGGER IF EXISTS trg_purchase_order_fx_basis_immutable ON purchase_order;
--   DROP FUNCTION IF EXISTS purchase_order_fx_basis_is_immutable();
--   DROP TRIGGER IF EXISTS trg_po_line_source_matches_header ON po_line;
--   DROP FUNCTION IF EXISTS po_line_source_matches_header();
--   COMMENT ON COLUMN po_line.source_rate_minor IS NULL;
--   COMMENT ON COLUMN po_line.source_amount_minor IS NULL;
--   ALTER TABLE po_line
--       DROP CONSTRAINT IF EXISTS ck_po_line_source_pair,
--       DROP CONSTRAINT IF EXISTS ck_po_line_source_rate_nonneg,
--       DROP CONSTRAINT IF EXISTS ck_po_line_source_amount_nonneg,
--       DROP COLUMN IF EXISTS source_amount_minor,
--       DROP COLUMN IF EXISTS source_rate_minor;
--   COMMENT ON COLUMN purchase_order.source_minor_exponent IS NULL;
--   COMMENT ON COLUMN purchase_order.fx_rate_id IS NULL;
--   COMMENT ON COLUMN purchase_order.exchange_rate IS NULL;
--   ALTER TABLE purchase_order
--       DROP CONSTRAINT IF EXISTS ck_purchase_order_fx_provenance,
--       DROP CONSTRAINT IF EXISTS ck_purchase_order_minor_exponent,
--       DROP CONSTRAINT IF EXISTS fk_purchase_order_currency,
--       DROP COLUMN IF EXISTS source_minor_exponent,
--       DROP COLUMN IF EXISTS fx_rate_source,
--       DROP COLUMN IF EXISTS fx_rate_date,
--       DROP COLUMN IF EXISTS fx_rate_id;
--   DELETE FROM schema_migrations WHERE version = '029';
--   COMMIT;
