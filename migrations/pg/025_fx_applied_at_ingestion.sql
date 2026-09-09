-- 025_fx_applied_at_ingestion.sql
-- Moves the exchange rate out of the repair path and into the write path: the document is translated when it is INGESTED, once, and the ledger row that says so is the thing that makes "once" enforceable.
--
-- WHAT 023 DID AND WHAT IT DID NOT DO
-- ===================================
--
-- 023's header says it "applies the exchange rate that 013 stored and nobody
-- ever multiplied". That is TRUE OF `app/backend/pg/fx.py` AND FALSE OF THE
-- PRODUCT, and the difference is the whole of this migration.
--
--     grep -rn "translate_bill\|record_rate\|source_amount_minor" \
--          --include=*.py app/ tools/
--     -> app/backend/pg/fx.py and tests/ ONLY
--
-- Every function 023's module provides is invoked from nothing that runs in
-- production. `mirror_bill` -- the one writer of `bill` and `bill_line`, and
-- therefore of every rupee of CWIP the product reports -- writes the vendor's
-- own figure straight into `amount_paise`, which 023's own COMMENT declares to
-- be INR BASE, integer paise. It sets neither `source_currency` (so the NOT
-- NULL DEFAULT 'INR' answers for it) nor `source_amount_minor` (so it stays
-- NULL). A EUR bill is therefore still carried at its face value in rupees,
-- and `FINDINGS_REMEDIATION_STATUS.csv` still recording AUD-H-007 as not
-- implemented is accidentally correct.
--
-- THE SHARP EDGE, WHICH IS WORSE THAN THE ABSENCE
-- ===============================================
--
-- `procurement.py`'s bill-line upsert ends:
--
--     ON CONFLICT (bill_line_id) DO UPDATE SET ...
--         amount_paise = EXCLUDED.amount_paise,
--
-- and `trg_bill_line_source_amount_immutable` (023:557) fires only when
--
--     NEW.source_amount_minor IS DISTINCT FROM OLD.source_amount_minor.
--
-- Ingestion never sets that column, so on a re-mirror NEW equals OLD, the
-- trigger passes, and a TRANSLATED `amount_paise` is overwritten with the
-- UNTRANSLATED source figure -- while `bill.fx_translated_at` goes on
-- asserting a translation the lines no longer carry. The guard is not weak;
-- it is unreachable, because the column it watches was never written. A
-- control that no writer can trip is not a control.
--
-- The repair is not a second trigger on that column. It is that ingestion now
-- WRITES `source_amount_minor`, at which point 023's trigger becomes live
-- exactly as written and a revised source amount RAISES instead of drifting.
-- Not one line of 023's guard changes; what changes is that a writer finally
-- trips it.
--
-- AND THE OTHER TWO THIRDS OF THE MONEY
-- =====================================
--
-- A `bill_line` carries THREE money columns and every rollup in the product
-- sums all three -- `amount_paise + non_creditable_tax_paise + freight_paise`
-- appears verbatim in `domain.compute_ledger`, in
-- `procurement_services._RECOMPUTE_DERIVED_SQL` (the single writer of
-- `budget_ledger_cell`, and therefore of every actual the product reports), in
-- `procurement.reconcile_po_lines`, in `procurement.reconciliation_lines`, in
-- both of `reporting.py`'s branches, and in `main.py`'s `/api/bills` header
-- total. `fx.py` contains not one occurrence of either of the other two
-- columns. A bill translated by 023's module alone would therefore have had
-- ONE rupee figure added to TWO foreign-currency figures and the sum reported
-- as CWIP -- a defect of the same kind as AUD-H-007, inside the remedy for
-- AUD-H-007, and invisible to any test asserting only on `amount_paise`.
-- `source_tax_minor` and `source_freight_minor` close it.
--
--
-- WHAT THIS FILE ADDS
-- ===================
--
--   (a) `fx_translation_event` -- ONE ROW PER TRANSLATED DOCUMENT, primary key
--       (document_type, document_id), append-only. The rate, its date, its
--       source, the fx_rate row it came from, the source total in the SOURCE
--       currency's own minor units, the exponent that scaled it, the base
--       total it produced, and the rounding rule it was produced under. It is
--       simultaneously the provenance record (requirement 3), the once-only
--       enforcement (requirement 4 -- a second translation cannot be
--       INSERTed), and the replay oracle (requirement 7 -- a re-mirror
--       recomputes and compares rather than rewrites).
--
--   (b) `bill_line.source_tax_minor` and `bill_line.source_freight_minor`,
--       for the reason set out above: a bill line's money is three columns,
--       not one, and every consumer sums all three.
--
--   (c) The COMMENTs. 023 corrected `bill_line.amount_paise` where a reader is
--       already looking. The same sentence is owed to every other column that
--       is now either a SOURCE figure or a TRANSLATED one, because
--       requirement 2 is that WHICH COLUMN IS THE SOURCE AND WHICH IS THE
--       TRANSLATION is stated in the schema, not only in a module nobody
--       reads before writing a query.
--
--
-- WHAT THIS FILE DELIBERATELY DOES NOT ADD
-- ========================================
--
-- THE PURCHASE ORDER. It is the other document the approved contracts
-- identify as carrying money that may be foreign -- `013:381-384` gives it
-- `currency` and `exchange_rate`, `dto.PurchaseOrderDTO` carries
-- `currency_code`, and `C13_conflicts.json` names "foreign currency PO and
-- exchange-rate variance" as one of the required commitment-to-actual
-- positions. Its rate is still applied to nothing; `procurement.py:1988`
-- renders it to a STRING for a JSON body and that remains the whole of its
-- use.
--
-- It gets no columns here BECAUSE IT HAS NO INGESTION PATH TO WRITE THEM.
-- `_write_po` (`procurement_services.py:2119`) is the only writer of
-- `purchase_order` and `po_line`, and its only two callers are `create_po`
-- and `convert_pr_to_po` -- both reached from the API, neither from a sweep.
-- A purchase order is ORIGINATED in this product and EMITTED to Zoho; it does
-- not arrive.
--
-- Adding four provenance columns and a `po_line.source_amount_minor` with
-- nothing to write them would reproduce, exactly, the defect this migration
-- exists to remedy: 023 added `bill.source_currency` and
-- `bill_line.source_amount_minor` and no writer, and the guard built on the
-- second of them stayed unreachable for two waves. The columns and their
-- writer arrive together or they do not arrive.
--
-- THE GRN, for a different reason: it has an ingestion path and NO CURRENCY
-- ANYWHERE IN THE CONTRACT. `dto.ReceiveDTO` is the only inbound money
-- document DTO without a `currency_code` field, and `grn`/`grn_line` (013)
-- have no currency column -- while `grn_line.amount_paise` feeds
-- `received_paise` and `received_not_billed_paise`. A receipt against a
-- foreign-currency order therefore posts at face value with nothing naming the
-- currency. That is a CONTRACT gap, not an implementation one; it is absent
-- from `CONTRACT_GAPS.md` and is reported by this stream rather than papered
-- over with a column the contract does not sanction.
--
-- Nothing here edits a byte of 001..024. The checksum recorded in
-- `schema_migrations` is `sha256` over the WHOLE FILE, comments included
-- (`migrate_pg.py:138-143`), so a single added comment in a pushed migration
-- moves its checksum exactly as far as rewriting its DDL would, and both
-- readers of that checksum treat the difference as fatal. 020, 021, 022 and
-- 023 set this argument out at length; it is not restated.
--
-- ADDITIVE, AND NOT ONE CONSTRAINT IS ADDED TO AN EXISTING TABLE. One new
-- table, two added columns, three triggers, seven comments. No table dropped,
-- no column dropped, no constraint replaced, no existing row's meaning
-- changed.
--
-- Both added columns are NULLable, so every row now in `bill_line` reads
-- exactly as it read before -- an untranslated line, which is what it is. No
-- CHECK is added to `bill`, `bill_line` or any other 001..024 table, which
-- matters more than it sounds: `ALTER TABLE ... ADD CONSTRAINT` VALIDATES
-- against every existing row, so a CHECK that is true of new rows and false of
-- old ones does not fail at the first bad write, it fails the migration -- on
-- exactly the database it was written for. The one constraint that would have
-- had that shape is discussed under "what this file deliberately does not
-- add".
--
-- Every `*_paise` column below is `bigint`, integer paise, and STARTS its
-- declaration line: `migrate_pg.py::_PAISE_COLUMN_RE` is anchored at `^`
-- against each stripped field of a `CREATE TABLE` body, and a paise column the
-- parser cannot see is a money column whose type adoption never verifies.
-- Every table-level constraint carries an explicit `CONSTRAINT name` for the
-- same reason: `_named_constraints_by` reports only named ones.

BEGIN;

-- ================================================== fx_translation_event
-- The record that a rate was applied to a document, and the reason "exactly
-- once" is a property of the schema rather than a promise made by a caller.
--
-- WHY THE PRIMARY KEY IS THE DOCUMENT AND NOT A SURROGATE. A surrogate id
-- would let two rows describe two translations of one document, which is the
-- thing being prevented. `(document_type, document_id)` makes a second
-- translation a primary-key violation -- loud, in the same transaction, before
-- any money column moves. Requirement 4 in one line of DDL.
--
-- WHY THE SOURCE TOTAL IS STORED AND NOT DERIVED. It is what the base figure
-- was computed FROM. Deriving it back from `SUM(bill_line.source_amount_minor)`
-- would make the evidence depend on the rows it is evidence about, so a
-- superseded line (whose money `_supersede_withdrawn_bill_lines` zeroes) would
-- silently change the answer to "what did this document translate?".
--
-- WHY `rounding` IS A COLUMN AND NOT AN ASSUMPTION. `fx_policy.FX_RATE_ROUNDING`
-- says what the rule IS; this says what the rule WAS when this row was
-- written. They are the same value today because `ck_fx_policy_known` permits
-- exactly one, and 023's own note says that key exists "so the rule is stated
-- as data and reviewable, not so it can drift". A stored figure that cannot
-- name the rounding it used is a figure an auditor cannot recompute.
--
-- NO SECOND ROUNDING RULE IS INTRODUCED. `ck_fx_translation_rounding` admits
-- HALF_UP and nothing else, which is `fx_policy`'s only permitted value and
-- what `fx.translate_to_base_paise` implements -- half AWAY FROM ZERO, applied
-- ONCE to the product, so a credit note of -0.005 rounds to -1 exactly as a
-- bill of +0.005 rounds to +1.
CREATE TABLE fx_translation_event (
    document_type         text NOT NULL,
    document_id           text NOT NULL,
    entity_id             text NOT NULL REFERENCES entity (entity_id),
    source_currency       text NOT NULL
                          REFERENCES currency_denomination (currency_code),
    source_minor_exponent integer NOT NULL,
    source_total_minor    bigint NOT NULL,
    fx_rate               numeric(18,8) NOT NULL,
    fx_rate_date          date,
    fx_rate_source        text,
    fx_rate_id            text REFERENCES fx_rate (fx_rate_id),
base_total_paise      bigint NOT NULL,
    line_count            integer NOT NULL,
    rounding              text NOT NULL DEFAULT 'HALF_UP',
    correlation_id        text,
    translated_at         timestamptz NOT NULL DEFAULT now(),
    translated_by         text NOT NULL,
    CONSTRAINT pk_fx_translation_event PRIMARY KEY (document_type, document_id),
    -- ONE KIND, BECAUSE ONE KIND HAS A WRITER. The vendor bill is the only
    -- inbound document that carries money which may be in a foreign currency
    -- AND has an ingestion path to translate it on. See the header for the
    -- purchase order, which has a currency and a rate and no ingestion path at
    -- all, and for the GRN, which has an ingestion path and no currency
    -- anywhere in the contract.
    --
    -- A SECOND KIND IS A MIGRATION, and that is the point of naming them here
    -- rather than accepting any string: it forces the schema change and the
    -- writer to arrive together, which is precisely what did not happen for
    -- the columns 023 added.
    CONSTRAINT ck_fx_translation_event_document_type
        CHECK (document_type IN ('BILL')),
    CONSTRAINT ck_fx_translation_event_document_id_not_blank
        CHECK (btrim(document_id) <> ''),
    CONSTRAINT ck_fx_translation_rate_positive CHECK (fx_rate > 0),
    CONSTRAINT ck_fx_translation_exponent
        CHECK (source_minor_exponent BETWEEN 0 AND 4),
    CONSTRAINT ck_fx_translation_line_count CHECK (line_count >= 0),
    CONSTRAINT ck_fx_translation_rounding CHECK (rounding IN ('HALF_UP')),
    -- A rate with no provenance is not evidence -- 023's rule (b), applied to
    -- the ledger that records the application. The identity translation names
    -- no rate row because there is no rate; anything else names the row, the
    -- date and the source, exactly as `ck_bill_fx_provenance` requires of the
    -- bill this row describes.
    CONSTRAINT ck_fx_translation_provenance CHECK (
        (source_currency = 'INR'
         AND fx_rate = 1 AND fx_rate_id IS NULL AND fx_rate_date IS NULL)
        OR (source_currency <> 'INR'
            AND fx_rate_id IS NOT NULL
            AND fx_rate_date IS NOT NULL
            AND btrim(coalesce(fx_rate_source, '')) <> '')
    ),
    -- THE IDENTITY TRANSLATION IS THE IDENTITY. An INR document's base total
    -- is its source total, unrounded and unmultiplied; a row claiming
    -- otherwise has had a rate applied to a document that has no rate.
    CONSTRAINT ck_fx_translation_identity_is_identity CHECK (
        source_currency <> 'INR' OR base_total_paise = source_total_minor
    )
);

CREATE INDEX ix_fx_translation_event_entity
    ON fx_translation_event (entity_id, translated_at DESC);
CREATE INDEX ix_fx_translation_event_rate
    ON fx_translation_event (fx_rate_id);

-- ======================= bill_line: THE OTHER TWO THIRDS OF THE MONEY (a-ii)
-- 023 gave `bill_line` ONE source column, for `amount_paise`. A bill line
-- carries THREE money columns, and every rollup in the product sums all three:
--
--     bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise
--
-- `domain.compute_ledger` (both the `billed_by_line` and the `actual` reads),
-- `procurement_services._RECOMPUTE_DERIVED_SQL` -- the single writer of
-- `budget_ledger_cell` and therefore the source of every reported actual --
-- `procurement.reconcile_po_lines`, `procurement.reconciliation_lines`,
-- `reporting._PO_BRANCH` and `reporting._BILL_BRANCH` all sum exactly that
-- expression, and `main.py`'s `/api/bills` computes the header total from it.
--
-- `fx.translate_bill` translates `amount_paise` ALONE. There is not one
-- occurrence of `non_creditable_tax_paise` or `freight_paise` in
-- `app/backend/pg/fx.py`. So a translated EUR bill would have produced a sum
-- of one INR figure and two EUR figures added together as though all three
-- were rupees -- a defect of the same KIND as AUD-H-007, inside the repair
-- for AUD-H-007, and invisible to any test that asserted only on
-- `amount_paise`.
--
-- Two columns close it. Both NULLable, so every existing row reads exactly as
-- it read before, and both written by `mirror_bill` at ingestion alongside
-- `source_amount_minor` -- the whole line is translated or none of it is.
ALTER TABLE bill_line ADD COLUMN source_tax_minor bigint;
ALTER TABLE bill_line ADD COLUMN source_freight_minor bigint;

-- The twin of `trg_bill_line_source_amount_immutable` (023:557) for the two
-- columns 023 did not have. Separate trigger rather than a replacement of
-- 023's function: 001..024 are checksum-frozen, and `CREATE OR REPLACE`ing a
-- function another migration owns would make this file the definition of
-- something 023's text still claims to define.
CREATE OR REPLACE FUNCTION bill_line_source_tax_freight_is_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.source_tax_minor IS NOT NULL
        AND NEW.source_tax_minor IS DISTINCT FROM OLD.source_tax_minor)
    OR (OLD.source_freight_minor IS NOT NULL
        AND NEW.source_freight_minor IS DISTINCT FROM OLD.source_freight_minor)
    THEN
        RAISE EXCEPTION
            'bill_line %: source_tax_minor and source_freight_minor are '
            'immutable (tax % to %, freight % to %). They are the evidence '
            'non_creditable_tax_paise and freight_paise were derived from, and '
            'every rollup in the product sums all three money columns '
            'together -- so a source figure that can be overwritten makes two '
            'thirds of a translated bill unverifiable.',
            OLD.bill_line_id, OLD.source_tax_minor, NEW.source_tax_minor,
            OLD.source_freight_minor, NEW.source_freight_minor
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bill_line_source_tax_freight_immutable
    BEFORE UPDATE ON bill_line
    FOR EACH ROW EXECUTE FUNCTION bill_line_source_tax_freight_is_immutable();

-- ================================= immutability: the translation is a fact
-- `fx_translation_event` records that a rate WAS applied. A row that can be
-- updated records that a rate is CURRENTLY BELIEVED to have been applied,
-- which is a different and much weaker statement -- and it is the statement
-- the whole once-only argument would then rest on. An UPDATE is refused rather
-- than merely ungranted, because a privilege can be granted by a later
-- migration or by a DBA in a hurry and a trigger cannot be granted around.
--
-- THIS IS WHY A CORRECTED SOURCE DOCUMENT RAISES. `fx.register_translation`
-- compares this row's `source_total_minor` against what the incoming payload
-- carries and refuses `FX_SOURCE_DOCUMENT_CHANGED` when they differ. The
-- refusal is the requirement -- a base figure derived at a rate fixed on the
-- document's own date must not be silently re-derived from a new source
-- amount -- and this trigger is what stops the refusal being routed around by
-- an UPDATE to the row it is checked against.
CREATE OR REPLACE FUNCTION fx_translation_event_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'fx_translation_event %/%: a translation event is a record that a rate '
        'was applied to a document at a moment. It is never updated and never '
        'deleted. Changing it would make the base figure it explains '
        'unverifiable, which is the state AUD-H-007 exists to end; deleting it '
        'would allow the same document to be translated a second time, which '
        'is what plan decision D-5 refuses. A period-end movement is recorded '
        'in fx_revaluation_attempt, which is the table that exists for it.',
        OLD.document_type, OLD.document_id
        USING ERRCODE = 'insufficient_privilege';
    -- Unreachable: the RAISE above always fires. Present because plpgsql checks
    -- "control reached end of trigger procedure without RETURN" at RUN time,
    -- and a trigger body whose only exit is an exception is one edit away from
    -- a second, silent failure mode. Same note as 023's
    -- `period_reopen_request_no_delete`.
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_fx_translation_event_no_update
    BEFORE UPDATE ON fx_translation_event
    FOR EACH ROW EXECUTE FUNCTION fx_translation_event_is_immutable();

CREATE TRIGGER trg_fx_translation_event_no_delete
    BEFORE DELETE ON fx_translation_event
    FOR EACH ROW EXECUTE FUNCTION fx_translation_event_is_immutable();

-- ========================================================= row level security
-- ENABLE, FORCE and a policy, verified by `migrate_pg._rls_problems` from this
-- file's own text, so a database that has the table but not the enforcement
-- cannot certify as adopted.
--
-- `fx_translation_event` carries `entity_id NOT NULL` with a foreign key to
-- `entity`, so it takes the DIRECT shape -- the one 023 gave
-- `fx_revaluation_attempt` and 006 gave `accounting_period`. Plant, location
-- and project are waived because the table has no column for any of them,
-- which is the waiver 006 states for `accounting_period` itself and the rule
-- `repo.compile_scope` applies to a dimension a query shape cannot express.
--
-- IT IS NOT REFERENCE DATA, and the distinction from `fx_rate` is worth
-- stating. A RATE belongs to nobody: two entities translating the same
-- currency on the same date must read the same number. An APPLICATION of that
-- rate belongs to the document it was applied to, and a document belongs to an
-- entity -- so an unscoped read of this table is a list of another entity's
-- foreign-currency exposure, document by document, with the amounts.
--
-- REGISTRY NOTE. `app/backend/pg/rls.py` carries a per-migration registry of
-- these mappings and `app/backend/pg/scope_inventory.py` the independent list
-- of which tables must be protected at all. Both are owned by another stream
-- this wave and are NOT edited here; the exact entries they need are stated in
-- this stream's report so the lead applies them in one place. Until they are,
-- `tests/test_pg_rls_coverage.py` is the check that will notice.
ALTER TABLE fx_translation_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE fx_translation_event FORCE ROW LEVEL SECURITY;
CREATE POLICY fx_translation_event_scope ON fx_translation_event
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

-- ================================================== privileges for capex_app
-- Stated explicitly rather than relied upon, exactly as 011, 013, 014 and 023
-- state theirs: 004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT
-- ISSUED IT, so a deployment whose 025 is applied by a different identity than
-- its 004 would otherwise leave the application unable to read its own new
-- table.
--
-- SELECT AND INSERT ONLY. No UPDATE and no DELETE, which is the same posture
-- 023 took for `fx_revaluation_attempt` and for the same reason: this is the
-- record that a rate was applied, not a bookmark. The two triggers above make
-- the same rule structural, because a privilege can be granted by a later
-- migration and a trigger cannot be granted around.
GRANT SELECT, INSERT ON fx_translation_event TO capex_app;

-- ==================================================================== comments
-- Requirement 2: which figure is the IMMUTABLE SOURCE-DOCUMENT amount and
-- which is the TRANSLATED BASE-CURRENCY amount, stated where somebody writing
-- a query is already looking. 023 did this for `bill_line.amount_paise`; these
-- are the five columns it left unsaid and the one table this file adds.

COMMENT ON TABLE fx_translation_event IS
    'ONE ROW PER TRANSLATED DOCUMENT, append-only, primary key (document_type, document_id). It records that an exchange rate WAS APPLIED: the source total in the source currency''s own minor units, the minor-unit exponent that scaled it, the rate with its date, its named source and the fx_rate row it came from, the base-currency total in integer paise that it produced, and the rounding rule (HALF_UP, half away from zero, applied once to the product) it was produced under. Three jobs in one row: it is the provenance an auditor recomputes from; it is why a rate is applied EXACTLY ONCE, because a second translation of one document is a primary-key violation rather than a convention; and it is the oracle a replay is checked against, so a repeated inbound document recomputes and compares instead of rewriting. A source total that disagrees with the stored one is a REVISED source document and is refused (fx.FX_SOURCE_DOCUMENT_CHANGED), because re-deriving a posted base figure from a new source amount is value drift with nothing recording it.';

COMMENT ON COLUMN fx_translation_event.source_total_minor IS
    'The SOURCE-DOCUMENT amount, in the SOURCE currency''s own minor units -- yen for JPY (exponent 0), cents for EUR (2), fils for KWD (3). NOT paise, which is why it is not named _paise. Stored rather than derived from SUM(bill_line.source_amount_minor): this is the evidence the base figure was computed from, and evidence that is recomputed from the rows it explains changes when those rows do -- a line superseded by a revised bill has its money zeroed, which would silently change the answer to "what did this document translate?".';

COMMENT ON COLUMN fx_translation_event.base_total_paise IS
    'The TRANSLATED amount: INR base, integer paise, signed. base_total_paise = round_half_up(source_total_minor * fx_rate * 10 ^ (2 - source_minor_exponent)), quantised ONCE on the product and then allocated across the document''s lines by money.split_pro_rata so the lines sum to this figure exactly. For an INR document it equals source_total_minor unrounded and unmultiplied, which ck_fx_translation_identity_is_identity enforces.';

COMMENT ON COLUMN bill.source_currency IS
    'The currency of the SOURCE DOCUMENT the vendor raised -- what bill_line.source_amount_minor is denominated in. It is NOT the currency of bill_line.amount_paise, which is always INR base. ''INR'' means the identity translation: fx_rate = 1, no fx_rate row, and source_amount_minor left NULL because there is no second figure to keep. Immutable once bill.fx_translated_at is set (trg_bill_fx_basis_immutable, migration 023).';

COMMENT ON COLUMN bill_line.source_amount_minor IS
    'THE IMMUTABLE SOURCE-DOCUMENT AMOUNT, in bill.source_currency''s own minor units -- yen for JPY, cents for EUR, fils for KWD -- which is why it is not named _paise. It is the evidence bill_line.amount_paise (the TRANSLATED figure, INR base paise) was derived from, and trg_bill_line_source_amount_immutable (migration 023) refuses to change it. NULL on an INR bill by design: the identity translation has one figure, not two, and inventing a second would create the divergence a single base column exists to prevent. Written at INGESTION by app/backend/pg/procurement.py::mirror_bill, which is what makes that trigger reachable -- until migration 025 nothing wrote this column, so the guard watched a value no writer ever set.';

COMMENT ON COLUMN bill_line.source_tax_minor IS
    'THE IMMUTABLE SOURCE-DOCUMENT non-creditable tax, in bill.source_currency''s own minor units. non_creditable_tax_paise is the TRANSLATED figure, INR base paise. This column exists because every rollup in the product sums amount_paise + non_creditable_tax_paise + freight_paise -- domain.compute_ledger, procurement_services._RECOMPUTE_DERIVED_SQL (the single writer of budget_ledger_cell), procurement.reconcile_po_lines, procurement.reconciliation_lines and reporting''s PO and BILL branches all sum exactly that expression -- so translating only the first of the three would add one rupee figure to two foreign ones and call the result CWIP. NULL on an INR bill by design. Immutable once written (trg_bill_line_source_tax_freight_immutable).';

COMMENT ON COLUMN bill_line.source_freight_minor IS
    'THE IMMUTABLE SOURCE-DOCUMENT freight, in bill.source_currency''s own minor units. freight_paise is the TRANSLATED figure, INR base paise. See bill_line.source_tax_minor for why all three of a bill line''s money columns are translated together and not just amount_paise. NULL on an INR bill by design. Immutable once written (trg_bill_line_source_tax_freight_immutable).';

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS, PLAINLY.
--   --
--   -- `fx_translation_event` IS DATA, and it is the only place the product
--   -- records which rate was applied to which document and what it produced.
--   -- Dropping it does not untranslate anything -- `bill.fx_rate`,
--   -- `bill_line.source_amount_minor` and their purchase-order twins keep the
--   -- basis on the documents themselves, which is why 023 copied it there --
--   -- but it does remove the once-only ENFORCEMENT, so a document could be
--   -- translated a second time by a caller that no longer has a row telling
--   -- it not to. Export it before this runs.
--   --
--   -- `bill_line.amount_paise` is NOT converted back: it holds a TRANSLATED
--   -- figure and this block cannot know the rate to undo. The rate is on
--   -- `bill.fx_rate`, which 023 owns and this revert does not touch, so a
--   -- translated bill stays translated and stays verifiable -- but the two
--   -- source columns dropped below are the tax and freight evidence, and they
--   -- must be exported first or two thirds of every translated line becomes
--   -- unrecomputable.
--   --
--   -- The hash-chained FX_DOCUMENT_TRANSLATED entries in `audit_log` SURVIVE
--   -- and are the record of record.
--   DROP TRIGGER IF EXISTS trg_bill_line_source_tax_freight_immutable
--       ON bill_line;
--   DROP TRIGGER IF EXISTS trg_fx_translation_event_no_delete
--       ON fx_translation_event;
--   DROP TRIGGER IF EXISTS trg_fx_translation_event_no_update
--       ON fx_translation_event;
--   DROP FUNCTION IF EXISTS bill_line_source_tax_freight_is_immutable();
--   DROP FUNCTION IF EXISTS fx_translation_event_is_immutable();
--   COMMENT ON COLUMN bill_line.source_freight_minor IS NULL;
--   COMMENT ON COLUMN bill_line.source_tax_minor IS NULL;
--   COMMENT ON COLUMN bill_line.source_amount_minor IS NULL;
--   COMMENT ON COLUMN bill.source_currency IS NULL;
--   ALTER TABLE bill_line
--       DROP COLUMN IF EXISTS source_freight_minor,
--       DROP COLUMN IF EXISTS source_tax_minor;
--   DROP TABLE IF EXISTS fx_translation_event;
--   DELETE FROM schema_migrations WHERE version = '025';
--   COMMIT;
