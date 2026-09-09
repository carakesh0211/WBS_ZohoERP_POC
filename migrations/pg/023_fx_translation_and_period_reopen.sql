-- 023_fx_translation_and_period_reopen.sql
-- Applies the exchange rate that 013 stored and nobody ever multiplied, gives that rate a provenance, refuses silent CWIP revaluation, and puts an APPROVED approval instance between a CLOSED period and its reopening.
--
-- Two residuals land in one file because they share a table and a
-- transaction boundary: a foreign-currency bill is translated ONCE, at bill
-- date, and the only event that would ever retranslate it is a period-end
-- movement -- which is the same period whose reopening PART 2 gates. Splitting
-- them would put the refusal in one migration and the thing it refuses in
-- another.
--
-- Nothing here edits a byte of 001..022. The checksum recorded in
-- `schema_migrations` is `sha256` over the WHOLE FILE, comments included
-- (`migrate_pg.py:138-142`), so a single added comment in a pushed migration
-- moves its checksum exactly as far as rewriting its DDL would, and both
-- readers of that checksum treat the difference as fatal. 020, 021 and 022
-- set this argument out at length; it is not restated.
--
--
-- ==========================================================================
-- PART 1 -- AUD-H-007 RESIDUAL: THE RATE IS STORED AND NEVER APPLIED
-- ==========================================================================
--
-- `013_procurement.sql:381-384` gives `purchase_order` a currency and a rate:
--
--     currency        text NOT NULL DEFAULT 'INR',
--     exchange_rate   numeric NOT NULL DEFAULT 1,
--
-- and its own comment says why the type is right: "numeric, never real: a rate
-- is multiplied into money." It is not multiplied into money. Nothing in
-- `app/backend/pg/` reads `exchange_rate` for arithmetic;
-- `procurement.py:1988` renders it to a STRING for a JSON body and that is the
-- whole of its use. `bill` -- the document CWIP is actually built from -- has
-- no currency column at all, so a EUR vendor bill is stored as though its
-- face value were rupees. The POC seed contains exactly this row: PO-012,
-- SunPeak Energy GmbH, EUR, rate 92.50 (`app/backend/db.py:540`). A bill
-- against it for EUR 1,00,000 is carried at Rs 1,00,000 instead of
-- Rs 92,50,000, understating that project's capital position by a factor of
-- 92.5.
--
-- FOUR THINGS ARE WRONG AND EACH NEEDS ITS OWN COLUMN.
--
-- (a) THE SOURCE AMOUNT HAS NOWHERE TO LIVE. `bill_line.amount_paise` is a
--     single money column. Translate into it and the source amount is gone;
--     leave the source in it and the base is gone. There is no third
--     behaviour available with one column, which is why the rate has never
--     been applied: applying it would have destroyed the evidence.
--
--     RESOLVED BY ADDING `bill_line.source_amount_minor` AND LEAVING
--     `amount_paise` MEANING EXACTLY WHAT IT MEANS TODAY -- INR base, integer
--     paise. There is deliberately NO second `base_amount_paise` column.
--     Every rollup in the product reads `amount_paise`: `reporting.py`,
--     `closure.py`, `procurement.py`'s commitment-versus-actual arithmetic and
--     `exports.py` all sum it. A second base column would be a base figure
--     nothing reads, diverging from the one everything reads on the first
--     write that updated one and not the other. One base column, one meaning,
--     unchanged.
--
-- (b) A RATE WITH NO PROVENANCE IS NOT EVIDENCE. `numeric NOT NULL DEFAULT 1`
--     records a number and not where it came from or what day it is the rate
--     FOR. An auditor asked to stand behind a CWIP figure cannot, from that
--     column, distinguish a published RBI reference rate for the bill date
--     from a number somebody typed. `fx_rate` below stores the rate with its
--     source and its rate date and a natural key over the three, and the bill
--     carries the id of the row it was translated by -- plus a copy of the
--     rate, date and source, because a rate table row that is later corrected
--     must not silently retranslate a bill that has already posted.
--
-- (c) THE MINOR-UNIT EXPONENT IS NOT ALWAYS 2, and assuming it is is a
--     hundredfold money error, not a rounding one. JPY, KRW and CLP have no
--     minor unit at all (exponent 0); KWD, BHD, JOD and OMR have three.
--     Storing "the source amount in paise" for a JPY bill and multiplying by
--     the rate would divide the true figure by 100. `currency_denomination`
--     makes the exponent a row rather than an assumption, and
--     `ck_currency_denomination_minor_exponent` bounds it to 0..4.
--
-- (d) THE API TYPES THE RATE AS AN INTEGER. `api/procurement.py:263` and
--     `:274` both declare `exchange_rate: int = 1` (review finding H-4), so
--     Pydantic REJECTS 92.50 outright on the two routes that create a
--     purchase order. Every non-integer rate in existence is unsendable, which
--     is a second, independent reason the rate has never been applied. That is
--     a Python type and not schema; it is repaired in the same commit as this
--     file, in `app/backend/api/procurement.py`, and `app/backend/pg/fx.py`
--     holds the parser both it and this schema agree on.
--
-- WHAT TYPE A RATE IS, DECIDED AND STATED. `numeric(18,8)`.
--
--   * NOT float, for the reason `app/backend/money.py` opens with: a rate is
--     multiplied into money, so a float rate makes the product a float.
--   * NOT `numeric` unqualified, which is what 013 used. Unqualified numeric
--     has arbitrary scale, so two rows can hold 92.5 and 92.50000000001 and
--     both read as "the EUR rate for that day"; the translation is then not
--     reproducible from the stored value. A DECLARED scale is what makes
--     "recompute the translation and check it" a question with one answer.
--   * SCALE 8, which covers every published rate convention in use (five
--     significant decimals is the widest any major provider quotes) with room
--     that costs nothing, and is far short of `numeric`'s limits.
--   * PRECISION 18, so a rate can never itself overflow into the money
--     multiplication.
--
-- AND THE ROUNDING RULE, STATED AND STORED AS A ROW. Half-up, away from zero,
-- applied ONCE, to the product, quantised to whole paise:
--
--     base_paise = round_half_up(source_amount_minor
--                                * rate
--                                * 10 ^ (2 - minor_exponent_of_source))
--
-- Half-up rather than banker's rounding because that is what Indian
-- accounting expects and what `money.to_paise` already does; symmetric about
-- zero so a credit note of -0.005 becomes -1 paise exactly as a bill of
-- +0.005 becomes +1, which matters because `bill_line.amount_paise` is signed
-- by design (§2.4). ONCE, on the product, and never per-line-then-summed:
-- `fx.allocate_base_paise` translates the header total and distributes it with
-- `money.split_pro_rata`, so the lines always sum to the header to the paisa.
-- The rule is `fx_policy.FX_RATE_ROUNDING`, seeded 'HALF_UP', with exactly one
-- permitted value -- so adding a second is a deliberate constraint change and
-- not a default somebody drifts.
--
-- NO SILENT CWIP REVALUATION (PLAN DECISION D-5). D-5 says translate at bill
-- date and do not revalue. That is a rule about what must NOT happen, and the
-- only way to hold it is to make the attempt visible: `fx_revaluation_attempt`
-- records the booked rate, the proposed rate, both base figures and the delta,
-- and `fx_policy.FX_PERIOD_END_REVALUATION` decides between refusing loudly
-- (the seeded default) and recording without refusing. There is no permitted
-- value that APPLIES one. Two triggers make the same rule structural rather
-- than procedural: a bill's rate, rate date, rate source and source currency
-- are immutable once written, and so is a line's source amount.
--
--
-- ==========================================================================
-- PART 2 -- AUD-C-008 RESIDUAL: REOPENING A CLOSED PERIOD
-- ==========================================================================
--
-- `pg/periods.py`'s `_ALLOWED_TRANSITIONS` makes CLOSED terminal and says so:
-- "Forward-only. There is no reopen path in this milestone." A control that
-- does not exist cannot be defeated, so that was safe. It is also not
-- survivable in practice -- a period closed a day early has to be reopened by
-- SOMEBODY, and the shape that arrives when the product has no path for it is
-- a hand-run UPDATE against the state column, which no approval gates and no
-- audit chain records.
--
-- SO THE PATH IS BUILT, AND IT IS GATED ON AN APPROVED APPROVAL INSTANCE, NOT
-- ON A FLAG. `period_reopen_request.approval_instance_id` is `NOT NULL` and
-- references `approval_instance`; `pg/periods.reopen_period` refuses unless
-- that instance is `status = 'APPROVED'` and is bound to this very period.
-- A boolean column called `is_approved` would have been one UPDATE away from
-- meaning nothing; an approval instance carries its own hash-chained action
-- history and cannot reach APPROVED without a definition to have been
-- approved under (`ck_approval_instance_definition`, 008).
--
-- SEPARATION OF DUTIES IS BOTH ENFORCED AND STRUCTURAL. Whoever closed the
-- period may not approve its reopening, and may not apply it.
-- `accounting_period.closed_by` is captured onto the request row at request
-- time as `closed_by_at_request` -- frozen, because `closed_by` is overwritten
-- by the NEXT close and a separation check that reads a column somebody else
-- can move is not a separation check. `ck_period_reopen_separation` and
-- `ck_period_reopen_applier_separation` then make the two identities
-- unequal as a CONSTRAINT, so the rule holds against a maintenance script,
-- a future service bug and a superuser session that RLS does not apply to at
-- all. The service layer calls `auth.require_separation` -- the product's own
-- maker-checker, reused and not reimplemented -- and `pg/periods.py` records
-- exactly what that call does and does not cover today.
--
-- CONCURRENCY AND IDEMPOTENCY, AS SCHEMA AND NOT AS PROSE.
--   * `ux_period_reopen_idempotency` is a plain UNIQUE on `idempotency_key`.
--     A replayed reopen inserts nothing and returns the recorded outcome; it
--     cannot produce a second application.
--   * `ux_period_reopen_one_open_per_period` is a PARTIAL unique index over
--     `period_id WHERE status = 'REQUESTED'`, so two requests cannot both be
--     outstanding against one period.
--   * The apply path takes `SELECT ... FOR UPDATE` on the period row and
--     re-reads `state` under that lock before writing -- the same shape
--     `transition_period` already uses and `tests/test_pg_period_concurrency.py`
--     already proves. Of two concurrent reopens, the loser wakes to find the
--     period OPEN and refuses; it does not also reopen it.
--
-- `reopen_count` IS ADDED AND `closed_by` IS NOT CLEARED. A reopen does not
-- erase the close it reverses: `closed_at`/`closed_by` continue to record the
-- close that happened, `reopened_at`/`reopened_by` record the reversal, and
-- `reopen_count` makes "this period has been reopened before" answerable from
-- the row rather than only from the chain. The chain is still the record of
-- record: `pg/audit.append` writes PERIOD_REOPEN onto the SAME
-- `ACCOUNTING_PERIOD:<period_id>` stream the closes are on, so close and
-- reopen interleave in one hash-chained sequence. Its payload format
-- `prev|at|actor|action|type|id|detail` is untouched -- it is frozen
-- permanently and changing it invalidates every hash already stored.
--
-- THE STATE CHECK IS NOT ALTERED. `accounting_period.state`'s inline
-- `CHECK (state IN ('FUTURE','OPEN','SOFT_CLOSED','CLOSED'))` (001) already
-- admits OPEN, which is where a reopened period goes. Nothing here drops or
-- replaces a constraint 001 created, which is what keeps this migration
-- additive.
--
--
-- ==========================================================================
-- ADDITIVE, AND WHAT THAT MEANS HERE
-- ==========================================================================
-- Five new tables, ten added columns, six added constraints, four triggers
-- and eleven seeded reference rows. No table is dropped, no column is
-- dropped, no constraint 001..022 created is replaced, and no existing row's
-- meaning changes: every added column is either NULLable or carries a DEFAULT
-- that makes an existing row read exactly as it read before -- an INR bill at
-- rate 1 with no FX provenance, which is what it was.
--
-- Every `*_paise` column below is `bigint`, integer paise, and STARTS its
-- declaration line: `migrate_pg.py::_PAISE_COLUMN_RE` is anchored at `^`
-- against each stripped field of a `CREATE TABLE` body, and a paise column
-- the parser cannot see is a money column whose type adoption never verifies.
-- Every table-level constraint is declared with an explicit `CONSTRAINT name`
-- for the same reason: `_named_constraints_by` reports only named ones, so an
-- anonymous constraint is invisible to adoption and a database missing it
-- certifies as adopted.

BEGIN;

-- ==================================================== currency_denomination
-- The minor-unit exponent, as data. See (c) in the header: assuming 2 is a
-- hundredfold error on JPY and a thousandfold one on KWD, in the direction
-- that understates capital spend.
--
-- `is_supported` rather than deleting a row: a currency withdrawn from use
-- must keep its exponent, or every bill already translated under it becomes
-- unverifiable.
CREATE TABLE currency_denomination (
    currency_code   text PRIMARY KEY,
    minor_exponent  integer NOT NULL,
    display_name    text NOT NULL,
    is_supported    boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT 'SYSTEM',
    CONSTRAINT ck_currency_denomination_code
        CHECK (currency_code ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_currency_denomination_minor_exponent
        CHECK (minor_exponent BETWEEN 0 AND 4),
    CONSTRAINT ck_currency_denomination_name_not_blank
        CHECK (btrim(display_name) <> '')
);

-- ISO 4217 minor units. INR first because it is the base currency and the one
-- value the whole product depends on being 2.
INSERT INTO currency_denomination (currency_code, minor_exponent, display_name)
VALUES
    ('INR', 2, 'Indian Rupee'),
    ('USD', 2, 'United States Dollar'),
    ('EUR', 2, 'Euro'),
    ('GBP', 2, 'Pound Sterling'),
    ('AED', 2, 'UAE Dirham'),
    ('SGD', 2, 'Singapore Dollar'),
    ('CHF', 2, 'Swiss Franc'),
    ('AUD', 2, 'Australian Dollar'),
    ('CNY', 2, 'Chinese Yuan'),
    ('JPY', 0, 'Japanese Yen'),
    ('KWD', 3, 'Kuwaiti Dinar');

-- ================================================================= fx_rate
-- One rate, for one currency pair, for one date, from one named source.
--
-- `rate_date` is the date the rate IS FOR, which for D-5 is the bill date.
-- `captured_at` is when this row was written, which is a different fact and
-- is not a substitute: a rate fetched three days late is still the rate for
-- its own date, and an auditor asked "was this the bill-date rate?" needs
-- both.
--
-- `ux_fx_rate_natural` admits two sources for one date deliberately -- an RBI
-- reference rate and a bank's dealt rate for the same day are both real and a
-- bill records WHICH it used. What it refuses is the same source quoting the
-- same pair twice for one date, which is the shape that makes a translation
-- irreproducible.
CREATE TABLE fx_rate (
    fx_rate_id       text PRIMARY KEY,
    from_currency    text NOT NULL REFERENCES currency_denomination (currency_code),
    to_currency      text NOT NULL DEFAULT 'INR'
                     REFERENCES currency_denomination (currency_code),
    rate_date        date NOT NULL,
    rate             numeric(18,8) NOT NULL,
    rate_source      text NOT NULL,
    source_reference text,
    captured_at      timestamptz NOT NULL DEFAULT now(),
    created_by       text NOT NULL,
    CONSTRAINT ux_fx_rate_natural
        UNIQUE (from_currency, to_currency, rate_date, rate_source),
    CONSTRAINT ck_fx_rate_positive CHECK (rate > 0),
    CONSTRAINT ck_fx_rate_not_identity CHECK (from_currency <> to_currency),
    CONSTRAINT ck_fx_rate_source_not_blank CHECK (btrim(rate_source) <> '')
);

CREATE INDEX ix_fx_rate_lookup
    ON fx_rate (from_currency, to_currency, rate_date DESC);

-- =============================================================== fx_policy
-- The business choices this stream could not answer from the plan or the
-- contracts, as rows an operator can read and change -- the posture
-- `procurement_policy` (014) established, and for the reason it gave: a
-- business choice a controller makes is one a controller must be able to see.
--
-- `ck_fx_policy_known` names every key and every permitted value, so an
-- unknown key or an unknown value is refused by the database rather than
-- silently read as a default by whichever caller looks first.
--
-- NOTE WHAT IS NOT CONFIGURABLE. There is no key that makes revaluation
-- APPLY, and none that makes a period reopen without an approval. A policy
-- table is for choices, not for a switch that turns a control off.
CREATE TABLE fx_policy (
    policy_key   text PRIMARY KEY,
    policy_value text NOT NULL,
    description  text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    version_no   integer NOT NULL DEFAULT 1,
    CONSTRAINT ck_fx_policy_known CHECK (
        (policy_key = 'FX_PERIOD_END_REVALUATION'
         AND policy_value IN ('REFUSE_AND_RECORD', 'RECORD_ONLY'))
        OR (policy_key = 'FX_RATE_ROUNDING'
            AND policy_value IN ('HALF_UP'))
        OR (policy_key = 'FX_UNKNOWN_CURRENCY'
            AND policy_value IN ('REFUSE', 'ASSUME_MINOR_EXPONENT_2'))
    )
);

INSERT INTO fx_policy (policy_key, policy_value, description, updated_by)
VALUES
    ('FX_PERIOD_END_REVALUATION', 'REFUSE_AND_RECORD',
     'What happens when a period-end movement would change an ALREADY '
     'TRANSLATED figure. Plan decision D-5 is translate at bill date, no '
     'revaluation. REFUSE_AND_RECORD (the working default) writes the attempt '
     'to fx_revaluation_attempt with both base figures and the delta, then '
     'raises. RECORD_ONLY writes the same row and returns without raising, for '
     'a reporting-only exposure run. NEITHER APPLIES THE NEW RATE: there is no '
     'permitted value that does, and adding one is a change to D-5.',
     'SYSTEM'),
    ('FX_RATE_ROUNDING', 'HALF_UP',
     'How the product of source amount and rate is quantised to whole paise. '
     'HALF_UP is half away from zero, applied ONCE to the product, symmetric '
     'about zero so a credit note rounds as a bill does. It is the only '
     'permitted value: this key exists so the rule is stated as data and '
     'reviewable, not so it can drift.',
     'SYSTEM'),
    ('FX_UNKNOWN_CURRENCY', 'REFUSE',
     'What happens when a bill names a currency with no currency_denomination '
     'row, so its minor-unit exponent is UNKNOWN. REFUSE (the working default) '
     'rejects the bill: guessing 2 for a JPY amount understates it a '
     'hundredfold, and a control that cannot be evaluated must refuse. '
     'ASSUME_MINOR_EXPONENT_2 exists for a bulk backfill of a currency set '
     'known to be two-decimal, and audits every assumption it makes.',
     'SYSTEM');

-- ================================================= fx_revaluation_attempt
-- D-5 made visible. A period-end movement that WOULD change a previously
-- translated figure lands here whether it is refused or merely recorded, with
-- enough of both sides to reconstruct the decision without the rate table.
--
-- `delta_paise` is stored AND constrained to equal the difference, rather than
-- computed on read: the two base figures are what the decision was made on,
-- and a stored delta that disagrees with them is itself the finding.
CREATE TABLE fx_revaluation_attempt (
    attempt_id            text PRIMARY KEY,
    entity_id             text NOT NULL REFERENCES entity (entity_id),
    period_id             text REFERENCES accounting_period (period_id),
    bill_id               text NOT NULL REFERENCES bill (bill_id),
    source_currency       text NOT NULL
                          REFERENCES currency_denomination (currency_code),
    booked_rate           numeric(18,8) NOT NULL,
    booked_rate_date      date NOT NULL,
    proposed_rate         numeric(18,8) NOT NULL,
    proposed_rate_date    date NOT NULL,
    proposed_rate_source  text NOT NULL,
booked_base_paise     bigint NOT NULL,
proposed_base_paise   bigint NOT NULL,
delta_paise           bigint NOT NULL,
    outcome               text NOT NULL,
    detail                text NOT NULL,
    attempted_at          timestamptz NOT NULL DEFAULT now(),
    attempted_by          text NOT NULL,
    CONSTRAINT ck_fx_revaluation_outcome
        CHECK (outcome IN ('REFUSED', 'RECORDED_NOT_APPLIED')),
    CONSTRAINT ck_fx_revaluation_delta
        CHECK (delta_paise = proposed_base_paise - booked_base_paise),
    CONSTRAINT ck_fx_revaluation_rates_positive
        CHECK (booked_rate > 0 AND proposed_rate > 0),
    CONSTRAINT ck_fx_revaluation_detail_not_blank
        CHECK (btrim(detail) <> '')
);

CREATE INDEX ix_fx_revaluation_attempt_bill ON fx_revaluation_attempt (bill_id);
CREATE INDEX ix_fx_revaluation_attempt_entity
    ON fx_revaluation_attempt (entity_id, attempted_at DESC);

-- ==================================================== period_reopen_request
-- The approval-gated reopen. See PART 2 of the header for why every column is
-- here; the two separation constraints are the load-bearing ones.
--
-- `closed_by_at_request` is NOT NULL and is a COPY, not a reference. A period
-- that has never closed cannot be reopened, so there is always a value to
-- copy; and copying is what makes the separation rule immune to the next
-- close overwriting `accounting_period.closed_by`.
CREATE TABLE period_reopen_request (
    reopen_id            text PRIMARY KEY,
    period_id            text NOT NULL REFERENCES accounting_period (period_id),
    entity_id            text NOT NULL REFERENCES entity (entity_id),
    approval_instance_id text NOT NULL REFERENCES approval_instance (instance_id),
    idempotency_key      text NOT NULL,
    reason               text NOT NULL,
    closed_by_at_request text NOT NULL,
    closed_at_at_request timestamptz,
    requested_by         text NOT NULL,
    requested_at         timestamptz NOT NULL DEFAULT now(),
    approved_by          text,
    applied_by           text,
    applied_at           timestamptz,
    status               text NOT NULL DEFAULT 'REQUESTED',
    refusal_code         text,
    CONSTRAINT ux_period_reopen_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_period_reopen_status
        CHECK (status IN ('REQUESTED', 'APPLIED', 'REFUSED')),
    CONSTRAINT ck_period_reopen_reason_not_blank CHECK (btrim(reason) <> ''),
    CONSTRAINT ck_period_reopen_applied_is_attributed CHECK (
        status <> 'APPLIED'
        OR (applied_by IS NOT NULL AND applied_at IS NOT NULL
            AND approved_by IS NOT NULL)
    ),
    CONSTRAINT ck_period_reopen_refused_says_why CHECK (
        status <> 'REFUSED' OR btrim(coalesce(refusal_code, '')) <> ''
    ),
    -- SEGREGATION OF DUTIES, AS A CONSTRAINT AND NOT ONLY AS A SERVICE CHECK.
    -- The closer may neither approve the reopening nor apply it. RLS does not
    -- apply to a superuser at all and a service check lives in one code path;
    -- a CHECK holds against a maintenance script and a future caller that
    -- forgets. Same posture as `report_saved_view_owner_is_immutable` (022).
    CONSTRAINT ck_period_reopen_separation
        CHECK (approved_by IS NULL OR approved_by <> closed_by_at_request),
    CONSTRAINT ck_period_reopen_applier_separation
        CHECK (applied_by IS NULL OR applied_by <> closed_by_at_request)
);

-- Two requests cannot both be outstanding against one period. PARTIAL, so a
-- period may be reopened more than once over its life -- what is refused is
-- two OPEN requests racing to apply.
CREATE UNIQUE INDEX ux_period_reopen_one_open_per_period
    ON period_reopen_request (period_id) WHERE status = 'REQUESTED';

CREATE INDEX ix_period_reopen_period ON period_reopen_request (period_id);
CREATE INDEX ix_period_reopen_instance
    ON period_reopen_request (approval_instance_id);

-- ======================================================= bill FX provenance
-- Every column NULLable or DEFAULTed so an existing row reads exactly as it
-- read before this migration: an INR bill, rate 1, no FX provenance.
--
-- The rate, its date and its source are COPIED onto the bill as well as
-- referenced through `fx_rate_id`. That is not redundancy: correcting an
-- `fx_rate` row must never retranslate a bill that has already posted, and a
-- bill that carries only a foreign key would do exactly that.
ALTER TABLE bill ADD COLUMN source_currency text NOT NULL DEFAULT 'INR';
ALTER TABLE bill ADD COLUMN fx_rate_id text;
ALTER TABLE bill ADD COLUMN fx_rate numeric(18,8) NOT NULL DEFAULT 1;
ALTER TABLE bill ADD COLUMN fx_rate_date date;
ALTER TABLE bill ADD COLUMN fx_rate_source text;
ALTER TABLE bill ADD COLUMN fx_translated_at timestamptz;

ALTER TABLE bill
    ADD CONSTRAINT fk_bill_source_currency
        FOREIGN KEY (source_currency)
        REFERENCES currency_denomination (currency_code),
    ADD CONSTRAINT fk_bill_fx_rate
        FOREIGN KEY (fx_rate_id) REFERENCES fx_rate (fx_rate_id),
    ADD CONSTRAINT ck_bill_fx_rate_positive CHECK (fx_rate > 0),
    -- A rate with no provenance is not evidence (header (b)). An INR bill is
    -- the identity translation and names no rate row; anything else must name
    -- the row, the date and the source it was translated by.
    ADD CONSTRAINT ck_bill_fx_provenance CHECK (
        (source_currency = 'INR'
         AND fx_rate = 1 AND fx_rate_id IS NULL AND fx_rate_date IS NULL)
        OR (source_currency <> 'INR'
            AND fx_rate_id IS NOT NULL
            AND fx_rate_date IS NOT NULL
            AND btrim(coalesce(fx_rate_source, '')) <> '')
    );

-- THE SOURCE AMOUNT, IN THE SOURCE CURRENCY'S OWN MINOR UNITS.
--
-- Named `_minor` and not `_paise` because it is NOT paise: for a JPY line it
-- is whole yen and for a KWD line it is fils. Calling it paise would be the
-- (c) defect written into the column name, and `migrate_pg`'s paise-type rule
-- would then certify a figure that is not in paise. It is still `bigint`, and
-- `tests/test_pg_fx_schema.py` holds that type -- adoption's `_paise` rule
-- does not reach a column with this name, and that is stated rather than
-- relied on.
--
-- `amount_paise` KEEPS ITS MEANING: INR base, integer paise, signed. See
-- header (a) for why there is no second base column.
ALTER TABLE bill_line ADD COLUMN source_amount_minor bigint;

-- ================================================ accounting_period, reopened
ALTER TABLE accounting_period ADD COLUMN reopened_at timestamptz;
ALTER TABLE accounting_period ADD COLUMN reopened_by text;
ALTER TABLE accounting_period ADD COLUMN reopen_count integer NOT NULL DEFAULT 0;

ALTER TABLE accounting_period
    ADD CONSTRAINT ck_accounting_period_reopen_count_nonneg
        CHECK (reopen_count >= 0),
    -- A reopening is attributed or it did not happen.
    ADD CONSTRAINT ck_accounting_period_reopened_is_attributed CHECK (
        (reopened_at IS NULL AND reopened_by IS NULL)
        OR (reopened_at IS NOT NULL AND btrim(coalesce(reopened_by, '')) <> '')
    );

-- ============================================ immutability: the FX basis
-- D-5 as structure. Once a bill is translated, the four facts that determine
-- its base figure cannot move. Without this, a single UPDATE retranslates a
-- posted bill and `fx_revaluation_attempt` never sees it -- the refusal would
-- be a code path rather than a property.
--
-- WRITING the basis for the first time is allowed: the guard fires only when
-- the OLD value is already set, so an untranslated legacy bill can still be
-- given its currency and rate exactly once.
CREATE OR REPLACE FUNCTION bill_fx_basis_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.fx_translated_at IS NOT NULL AND (
           NEW.source_currency IS DISTINCT FROM OLD.source_currency
        OR NEW.fx_rate         IS DISTINCT FROM OLD.fx_rate
        OR NEW.fx_rate_date    IS DISTINCT FROM OLD.fx_rate_date
        OR NEW.fx_rate_id      IS DISTINCT FROM OLD.fx_rate_id) THEN
        RAISE EXCEPTION
            'bill %: the foreign-currency basis is immutable once translated '
            '(currency % rate % on %). Plan decision D-5 translates at bill '
            'date and does not revalue; changing the basis in place would move '
            'a posted CWIP figure with nothing recording that it moved. '
            'Record the movement in fx_revaluation_attempt instead.',
            OLD.bill_id, OLD.source_currency, OLD.fx_rate, OLD.fx_rate_date
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bill_fx_basis_immutable
    BEFORE UPDATE ON bill
    FOR EACH ROW EXECUTE FUNCTION bill_fx_basis_is_immutable();

CREATE OR REPLACE FUNCTION bill_line_source_amount_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source_amount_minor IS NOT NULL
       AND NEW.source_amount_minor IS DISTINCT FROM OLD.source_amount_minor THEN
        RAISE EXCEPTION
            'bill_line %: source_amount_minor is immutable (% to %). The '
            'source amount is the evidence the INR figure was derived from; '
            'overwriting it makes the translation unverifiable, which is the '
            'state AUD-H-007 exists to end.',
            OLD.bill_line_id, OLD.source_amount_minor, NEW.source_amount_minor
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bill_line_source_amount_immutable
    BEFORE UPDATE ON bill_line
    FOR EACH ROW EXECUTE FUNCTION bill_line_source_amount_is_immutable();

-- ================================== immutability: the reopen decision record
-- An approval-gated reopen whose approval can be re-pointed afterwards is not
-- gated. The instance, the period and the frozen closer are fixed at INSERT;
-- status, approver, applier and timestamps are the columns the workflow moves.
CREATE OR REPLACE FUNCTION period_reopen_request_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.period_id            IS DISTINCT FROM OLD.period_id
    OR NEW.approval_instance_id IS DISTINCT FROM OLD.approval_instance_id
    OR NEW.closed_by_at_request IS DISTINCT FROM OLD.closed_by_at_request
    OR NEW.idempotency_key      IS DISTINCT FROM OLD.idempotency_key
    OR NEW.requested_by         IS DISTINCT FROM OLD.requested_by THEN
        RAISE EXCEPTION
            'period_reopen_request %: period_id, approval_instance_id, '
            'closed_by_at_request, idempotency_key and requested_by are fixed '
            'at request time. Re-pointing any of them after the fact would let '
            'an approval granted for one period authorise another, or move the '
            'identity the separation-of-duties constraints are checked against.',
            OLD.reopen_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_period_reopen_request_append_only
    BEFORE UPDATE ON period_reopen_request
    FOR EACH ROW EXECUTE FUNCTION period_reopen_request_is_append_only();

-- A request may never be deleted. An abandoned reopen is REFUSED with a code,
-- which is a record; a deleted one is the absence of one.
CREATE OR REPLACE FUNCTION period_reopen_request_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'period_reopen_request %: reopen requests are never deleted. Set '
        'status to REFUSED with a refusal_code; the request for a reopening '
        'that did not happen is itself part of the close/reopen trail.',
        OLD.reopen_id
        USING ERRCODE = 'insufficient_privilege';
    -- Unreachable: the RAISE above always fires. Present because plpgsql
    -- checks "control reached end of trigger procedure without RETURN" at RUN
    -- time, and a trigger body whose only exit is an exception is one edit
    -- away from a second, silent failure mode.
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_period_reopen_request_no_delete
    BEFORE DELETE ON period_reopen_request
    FOR EACH ROW EXECUTE FUNCTION period_reopen_request_no_delete();

-- ============================================================ row level security
-- Every new table gets ENABLE, FORCE and a policy. `migrate_pg._rls_problems`
-- verifies all three from this file's own text, so a database that has the
-- tables but not the enforcement cannot certify as adopted.
--
-- TWO SHAPES, and which one a table gets is decided by whether it carries an
-- entity.
--
-- Organisation-wide reference and policy data -- `currency_denomination`,
-- `fx_rate`, `fx_policy` -- is scoped with `capex_principal_present()`, the
-- same predicate 006 uses for reference data and 014 uses for
-- `procurement_policy`: an authenticated principal may read it, an
-- unauthenticated session may not. A minor-unit exponent belongs to no entity,
-- and partitioning it by one would mean a caller restricted to ENT-A could not
-- translate a bill in a currency only ENT-B has used.
--
-- `fx_revaluation_attempt` and `period_reopen_request` both carry
-- `entity_id NOT NULL` directly and are filtered on it, exactly as
-- `accounting_period` is (006). Plant, location and project are waived
-- because neither table has a column for any of them -- the same waiver 006
-- states for `accounting_period` itself, and the same rule
-- `repo.compile_scope` applies to a dimension a query shape cannot express.
--
-- REGISTRY NOTE. `app/backend/pg/rls.py` carries a per-migration registry of
-- these mappings and `app/backend/pg/scope_inventory.py` the independent list
-- of which tables must be protected at all. Both are owned by another stream
-- this wave and are NOT edited here; the entries they need are stated in this
-- stream's report so the lead applies them in one place. Until they are
-- applied, `tests/test_pg_rls_coverage.py` is the check that will notice.
ALTER TABLE currency_denomination ENABLE ROW LEVEL SECURITY;
ALTER TABLE currency_denomination FORCE ROW LEVEL SECURITY;
CREATE POLICY currency_denomination_scope ON currency_denomination
    USING      (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE fx_rate ENABLE ROW LEVEL SECURITY;
ALTER TABLE fx_rate FORCE ROW LEVEL SECURITY;
CREATE POLICY fx_rate_scope ON fx_rate
    USING      (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE fx_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE fx_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY fx_policy_scope ON fx_policy
    USING      (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE fx_revaluation_attempt ENABLE ROW LEVEL SECURITY;
ALTER TABLE fx_revaluation_attempt FORCE ROW LEVEL SECURITY;
CREATE POLICY fx_revaluation_attempt_scope ON fx_revaluation_attempt
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE period_reopen_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE period_reopen_request FORCE ROW LEVEL SECURITY;
CREATE POLICY period_reopen_request_scope ON period_reopen_request
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

-- ================================================== privileges for capex_app
-- Stated explicitly rather than relied upon, exactly as 011, 013 and 014
-- state theirs: 004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT
-- ISSUED IT, so a deployment whose 023 is applied by a different identity
-- than its 004 would otherwise leave the application unable to read its own
-- new tables.
--
-- NO DELETE ANYWHERE. `fx_revaluation_attempt` and `period_reopen_request`
-- are the record that a movement was refused and that a close was reversed;
-- neither is a bookmark. `currency_denomination` and `fx_rate` get no DELETE
-- for the reason `is_supported` exists: removing a rate row makes every
-- translation performed under it unverifiable.
GRANT SELECT, INSERT, UPDATE ON
    currency_denomination, fx_rate, fx_policy, fx_revaluation_attempt,
    period_reopen_request
    TO capex_app;

-- The correction a reader needs where they are already looking.
COMMENT ON COLUMN bill_line.amount_paise IS
    'INR BASE, integer paise, signed. Its meaning is unchanged by migration 023: every rollup in the product sums this column, and a foreign-currency bill now writes its TRANSLATED figure here. The untranslated figure is bill_line.source_amount_minor, in the SOURCE currency''s own minor units (yen for JPY, fils for KWD), which is why that column is not named _paise. The basis is bill.source_currency, bill.fx_rate, bill.fx_rate_date and bill.fx_rate_id, all immutable once bill.fx_translated_at is set. The invariant: amount_paise = round_half_up(source_amount_minor * fx_rate * 10 ^ (2 - minor_exponent)), allocated across lines by money.split_pro_rata so the lines sum to the header exactly. See app/backend/pg/fx.py.';

COMMENT ON COLUMN accounting_period.closed_by IS
    'Who closed this period. NOT cleared by a reopening: migration 023 adds reopened_at, reopened_by and reopen_count alongside, so the close that was reversed stays on the row. This column is overwritten by the NEXT close, which is why period_reopen_request copies it into closed_by_at_request at request time rather than referencing it; the separation-of-duties constraints are checked against the frozen copy.';

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS, PLAINLY. Two things that ARE data:
--   --
--   --   * every fx_revaluation_attempt row -- the record that a period-end
--   --     movement was refused. If any exist, they are the evidence for a
--   --     D-5 refusal and should be exported before this runs.
--   --   * every period_reopen_request row -- the record of which closed
--   --     periods were reopened, under whose approval, and by whom. The
--   --     hash-chained PERIOD_REOPEN entries in audit_log SURVIVE, and are
--   --     the record of record; what is lost here is the approval binding.
--   --
--   -- Everything else is recoverable: bill.source_currency, bill.fx_rate and
--   -- bill_line.source_amount_minor go with the columns, which returns a
--   -- foreign-currency bill to the pre-023 state of being carried at its face
--   -- value in rupees -- the AUD-H-007 defect, restored, which is what
--   -- "revert" has to mean. accounting_period keeps every state it is in,
--   -- INCLUDING a period reopened to OPEN: the columns recording that it was
--   -- reopened are dropped, the state is not silently pushed back to CLOSED.
--   DROP TRIGGER IF EXISTS trg_period_reopen_request_no_delete
--       ON period_reopen_request;
--   DROP TRIGGER IF EXISTS trg_period_reopen_request_append_only
--       ON period_reopen_request;
--   DROP TRIGGER IF EXISTS trg_bill_line_source_amount_immutable ON bill_line;
--   DROP TRIGGER IF EXISTS trg_bill_fx_basis_immutable ON bill;
--   DROP FUNCTION IF EXISTS period_reopen_request_no_delete();
--   DROP FUNCTION IF EXISTS period_reopen_request_is_append_only();
--   DROP FUNCTION IF EXISTS bill_line_source_amount_is_immutable();
--   DROP FUNCTION IF EXISTS bill_fx_basis_is_immutable();
--   COMMENT ON COLUMN accounting_period.closed_by IS NULL;
--   COMMENT ON COLUMN bill_line.amount_paise IS NULL;
--   ALTER TABLE accounting_period
--       DROP CONSTRAINT IF EXISTS ck_accounting_period_reopened_is_attributed,
--       DROP CONSTRAINT IF EXISTS ck_accounting_period_reopen_count_nonneg;
--   ALTER TABLE accounting_period
--       DROP COLUMN IF EXISTS reopen_count,
--       DROP COLUMN IF EXISTS reopened_by,
--       DROP COLUMN IF EXISTS reopened_at;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS source_amount_minor;
--   ALTER TABLE bill
--       DROP CONSTRAINT IF EXISTS ck_bill_fx_provenance,
--       DROP CONSTRAINT IF EXISTS ck_bill_fx_rate_positive,
--       DROP CONSTRAINT IF EXISTS fk_bill_fx_rate,
--       DROP CONSTRAINT IF EXISTS fk_bill_source_currency;
--   ALTER TABLE bill
--       DROP COLUMN IF EXISTS fx_translated_at,
--       DROP COLUMN IF EXISTS fx_rate_source,
--       DROP COLUMN IF EXISTS fx_rate_date,
--       DROP COLUMN IF EXISTS fx_rate,
--       DROP COLUMN IF EXISTS fx_rate_id,
--       DROP COLUMN IF EXISTS source_currency;
--   DROP TABLE IF EXISTS period_reopen_request;
--   DROP TABLE IF EXISTS fx_revaluation_attempt;
--   DROP TABLE IF EXISTS fx_policy;
--   DROP TABLE IF EXISTS fx_rate;
--   DROP TABLE IF EXISTS currency_denomination;
--   DELETE FROM schema_migrations WHERE version = '023';
--   COMMIT;
