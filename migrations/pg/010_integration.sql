-- 010_integration.sql
-- Wave 5, stream 2: the integration platform's schema.
--
-- Plan traceability: Phase 5 -> section 2.2 (the job execution model), section 10.4 (data
-- classification), section 11.5 (polling and sweeps), section 11.6 (idempotency, rate
-- limiting, retry, circuit breaker), section 11.9 (modes and correlation) of
-- docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md v1.2.1. Table and column
-- names are frozen by docs/WAVE5_CONTRACTS.md seam C2; every state value is
-- transcribed from research/30_contracts/C16_integration_statuses.json and
-- none is invented here. Streams 3-7 import
-- `app/backend/pg/integration_store.py` rather than retyping any name below,
-- and `tests/test_pg_integration_schema.py` asserts that module and this file
-- cannot drift apart.
--
-- WHAT THIS FILE ADDS
--
--   integration_connection   one tenant connection: product, DC, org and MODE.
--                            Created FIRST; every other table below points at
--                            it.
--   integration_inbox        one received external payload, deduplicated by a
--                            UNIQUE constraint rather than by code.
--   integration_outbox       one outbound document emission and its dedupe key.
--   job                      one bounded, chunked, checkpointed background job.
--   integration_watermark    the per-(connection, module) high-water mark, and
--                            the 300 s overlap that makes it safe.
--   integration_rate_budget  the per-minute AND per-day call budgets.
--   integration_circuit      the per-(connection, module) circuit breaker.
--   integration_event        the append-only correlation trail.
--
-- ONE TABLE BEYOND THE SEAM, DECLARED RATHER THAN SMUGGLED
--
-- C2 freezes seven tables. This file creates EIGHT. `integration_circuit` is
-- the addition and it is called out here because a stream that adds an object
-- the contract does not name should have to say so in the first screen of the
-- file.
--
-- The reason: `C16_integration_statuses.json` freezes a fourth namespace,
-- `circuit`, whose three values (CIRCUIT_CLOSED / CIRCUIT_OPEN /
-- CIRCUIT_HALF_OPEN) are explicitly "per (connection, module)" -- and section 2.1
-- says there is NO resident process, so a breaker whose state lives in memory
-- is a breaker that resets on every cold start, which is every few minutes.
-- The breaker therefore has to be a table. C2 gives it none, and stream 4 owns
-- `throttle.py`, a Python module with no migration of its own. Leaving the gap
-- open would have left stream 4 with nowhere to put the state or a reason to
-- open an eleventh migration file, which is worse than an eighth table here.
-- If the lead would rather it lived elsewhere, dropping it costs one statement
-- and breaks no other stream's SQL, because no other stream knows it exists
-- yet.
--
-- WHAT THE CONSTRAINTS ARE DOING, AND WHY EACH IS A CONSTRAINT
--
-- Four properties in this file are load-bearing enough that they are enforced
-- by the database rather than asserted by a caller. Each is here because the
-- code path that would otherwise hold it is exactly the code path that gets
-- killed halfway through by the 15-minute Functions ceiling.
--
--   1. INBOUND IDEMPOTENCY IS FREE FROM A CONSTRAINT (section 11.6).
--      `uq_integration_inbox_idempotency UNIQUE (connection_id, module,
--      external_id, payload_sha)`. Every inbound write is
--      `INSERT ... ON CONFLICT ON CONSTRAINT uq_integration_inbox_idempotency
--      DO NOTHING RETURNING inbox_id`: a re-delivered payload produces no row
--      and returns nothing, and the "have I seen this?" question is never
--      asked in Python, where the answer would be stale by the time it was
--      acted on. Two concurrent poll invocations racing on the same bill do
--      not need a lock; one of them loses the insert, which is the correct
--      outcome and costs a round trip rather than a duplicate.
--
--   2. A CONNECTION CANNOT GO LIVE BY OMISSION (section 11.9).
--      `mode` DEFAULTS TO 'MOCK', and
--      `ck_integration_connection_live_is_authorised` makes LIVE_READ and
--      LIVE_WRITE unrepresentable without a named authoriser, a timestamp and
--      a non-blank note. A row that forgets to say anything about mode is a
--      MOCK row; a row that means to be live has to say who authorised it. The
--      failure mode being designed out is not "someone sets the wrong mode" --
--      that is visible -- but "nobody sets a mode at all and the default is
--      live", which is not.
--
--   3. A JOB CANNOT LOOP FOREVER (section 2.2).
--      `ck_job_resume_ceiling_is_terminal` says that at `max_resume_count` the
--      only representable states are DONE and DEAD, and
--      `ck_job_dead_is_alerted` says a DEAD job must carry an `alerted_at`.
--      Together they make "exceeded its resume ceiling and quietly kept being
--      re-claimed" a constraint violation. The plan's sentence is "a job
--      exceeding max_resume_count raises an alert rather than looping
--      forever"; a scheduler that simply forgot to check would otherwise
--      satisfy that sentence by loop.
--
--   4. THE RATE BUDGET IS BINDING, NOT ADVISORY (section 11.6).
--      `ck_integration_rate_budget_used_within_ceiling CHECK (used <=
--      ceiling)`. The reservation is `UPDATE ... SET used = used + n ...
--      RETURNING used`, so going over budget is a constraint violation raised
--      by the same statement that would have done the spending, not a value
--      read and then acted on a moment later. With no resident process there
--      is no other place a race between two cron invocations could be
--      resolved.
--
-- THE DAILY WINDOW IS NOT AN AFTERTHOUGHT (section 11.6)
--
-- "On ERP Standard (2,000/day) the daily budget, not the per-minute one, is
-- binding." The per-minute limit (100/org/min) is the one every Zoho document
-- states and therefore the one a reader assumes matters. It does not: at
-- 100/min the daily ceiling is reached in twenty minutes of sustained polling,
-- and the poller runs every five minutes all day.
--
-- So MINUTE and DAY are two values of `window_kind` on ONE table with ONE
-- shape -- same ceiling column, same used column, same over-budget constraint
-- -- rather than a per-minute table with a daily counter bolted onto it. There
-- is no column that only the minute window has and none that only the day
-- window has. `ix_integration_rate_budget_day` exists specifically so "how
-- much of today is left" is a first-class indexed question, and
-- `integration_connection.daily_call_ceiling` is a per-connection column
-- rather than a constant because the ceiling is plan-tier-dependent and
-- D-14 has not resolved which product, edition or tier this tenant is on.
--
-- No CHECK in this file computes a window boundary. `date_trunc(text,
-- timestamptz)`, `timestamptz + interval` and `extract(... FROM timestamptz)`
-- are all STABLE, not IMMUTABLE, because their results depend on the session
-- TimeZone -- a row valid for one connection's session would be invalid for
-- another's, and PostgreSQL would not tell us. `window_seconds` carries the
-- window length as an integer instead, which is immutable, and
-- `window_start_key` carries the bucket the application computed, in the
-- timezone named by `window_tz`. Whose midnight the daily quota resets at is
-- a tenant fact nobody has verified (Phase 0B), so the schema records the
-- timezone the boundary was computed in rather than assuming one.
--
-- WHAT section 10.4 MAKES THIS SCHEMA SAY ABOUT RAW PAYLOADS
--
-- section 10.4's last row: "Raw Zoho payloads in integration_inbox -- inherits the
-- highest class of any field within; Regulated/Restricted fields encrypted or
-- nulled on persist, with the cleartext kept only in the mapped column that
-- needs it." `vendor_master.gst_no` and `vendor_master.pan_no` in 005 ARE
-- those mapped columns. So a GSTIN in `integration_inbox.payload` is not a
-- copy; it is a second, unclassified, unmasked, un-access-controlled copy of a
-- regulated identifier sitting in a table an administrator reads raw.
--
-- The schema therefore refuses to be the honest-looking version of that:
--
--   * `payload` is the SANITISED payload, and the column is named in this
--     header rather than only in a comment because "payload" reads like "the
--     raw thing we got" and it is not.
--   * WHAT "NULLED" MEANS HERE, since section 10.4 says "encrypted or nulled"
--     and this schema reads that word in one particular way: the KEY IS
--     REMOVED, and its name is recorded in `redacted_keys` beside the payload.
--     A key kept with a null or a placeholder value would still be a key,
--     and the backstop below matches on key NAMES, so the two would
--     contradict each other -- every redacted receipt would fail to insert,
--     which is a discovery this file would otherwise have made in CI. Removal
--     also keeps the distinction a placeholder was for ("we removed the
--     GSTIN" versus "the vendor never sent one") somewhere better: a column
--     an operator can read and a query can filter on.
--   * `payload_classification` records the highest class of any field the
--     ORIGINAL carried, so the row still knows what it received even after
--     the field is gone.
--   * `redaction_policy_version` is NOT NULL with NO DEFAULT. A payload can
--     therefore not be stored without naming the redactor that processed it;
--     an insert that skipped `app.backend.pg.integration_store.redact_payload`
--     fails rather than storing an unredacted payload that looks redacted.
--   * `ck_integration_inbox_classification_shows_its_working` requires a
--     payload CLAIMING a REGULATED or RESTRICTED origin to show what it did:
--     either redacted key names or an encryption envelope. A row saying "this
--     carried bank details" and showing no treatment is a lie the table will
--     not store.
--   * `ck_integration_inbox_payload_carries_no_restricted_key` is the
--     backstop: `capex_payload_carries_restricted_key` scans the stored JSON
--     text for the frozen key names in `capex_restricted_payload_keys()` and
--     the CHECK refuses the row.
--
-- Be precise about what that backstop is and is not. It is a NAME-based scan
-- of the rendered JSON, so: it catches a restricted field at any nesting
-- depth; it FALSE-POSITIVES on a payload whose free text happens to contain
-- the literal characters `"pan_no":`, which is a refusal and therefore the
-- safe direction; and it CANNOT see a restricted VALUE stored under a key name
-- nobody anticipated. It is a guard against forgetting to redact, not a
-- classifier. The classifier is `redact_payload`, and the honest statement is
-- that the constraint makes the classifier's omission loud rather than making
-- the classifier unnecessary.
--
-- Nothing here hashes a classified field. v1.0 hashed GSTIN and PAN and section 10.4
-- withdrew that: `sha256(value)[:16]` destroys the matching, statutory
-- reporting and audit-evidence function those identifiers exist for.
-- `payload_sha` hashes the WHOLE PAYLOAD and is an idempotency key, not a
-- treatment of any field inside it.
--
-- WHAT IS DELIBERATELY NOT HERE
--
--   * NO base URL, scope string or endpoint column anywhere. D-14 is
--     unresolved and section 11 forbids a product fact reaching a caller. A
--     connection stores `product` and `dc`; the adapter (stream 1) derives the
--     rest and nothing persists it.
--   * NO `*_paise` column on any table in this file, so no `SUM()` over bigint
--     and no numeric-to-Decimal trap. Monetary amounts on an outbound document
--     live inside `integration_outbox.payload` as the integer paise the DTO
--     already carries; the ledger tables own money and this file owns
--     transport. Adding an `amount_paise` here for a screen's convenience
--     would create a second place a figure could be wrong.
--   * NO `reconciliation_exception` table. section 11.8 and section 11.5 both write to it,
--     it is named by `pg/periods.py::_has_open_reconciliation_exceptions`
--     (which probes `information_schema` for it rather than assuming it), and
--     C2 does not assign it to this stream. It stays a reported gap.
--   * NO status-mapping table. C3 of the contracts gives raw-status mapping to
--     stream 3 and `C17_zoho_status_map.json`. This file stores the raw value
--     verbatim and freezes it; it does not interpret it.
--
-- ROW-LEVEL SECURITY
--
-- `integration_connection` carries `entity_id`, so it is scopable directly.
-- Six of the other seven tables carry no dimension of their own and reach one
-- through the connection, in the shape 008 uses for `approval_rule`. `job` is
-- the exception: it carries its own nullable `entity_id`/`project_id` because
-- not every job is a connector job (`verify_audit_chains` and
-- `sweep_control_totals` are estate-wide), and a NULL there waives that
-- dimension exactly as `capex_dimension_permits` waives a SQL NULL and exactly
-- as `approval_instance.project_id` does in 008.
--
-- ENABLE *and* FORCE on every table, USING *and* WITH CHECK on every policy,
-- for the reasons 008's RLS section states at length: ENABLE without FORCE is
-- bypassed silently by the table owner, which in production is the deploy
-- identity; USING without WITH CHECK filters reads while leaving a caller free
-- to INSERT into a scope it cannot see.
--
-- Expand-only. Nothing here alters, drops or redefines anything 001..009
-- created. In particular it does NOT redefine `capex_scope_permits`,
-- `capex_dimension_permits` or `capex_principal_present`: their signatures and
-- bodies are frozen and every policy below calls them unchanged.
--
-- A NOTE FOR WHOEVER UPDATES app/backend/pg/scope_inventory.py
--
-- That module is the deliberately INDEPENDENT inventory of which tables need
-- RLS, and its maintenance rule is that a new table is added there BY HAND
-- from the CREATE TABLE, never by parsing a migration. This stream does not
-- own it, so the eight tables below are not in it yet and
-- `tests/test_pg_rls_coverage.py::test_every_table_in_the_schema_is_classified_scoped_or_deliberately_not`
-- will fail until they are. That failure is the inventory working as designed:
-- it is the only check that can catch a new table added with no RLS and no
-- registry entry. The eight entries needed are listed verbatim in this
-- stream's hand-off report; all eight are `status="protected_pending_registry"`,
-- `migration="010_integration.sql"`, exactly as 008's tables are.
--
-- ROLLBACK:
--   DROP POLICY IF EXISTS integration_connection_scope ON integration_connection;
--   DROP POLICY IF EXISTS integration_inbox_scope ON integration_inbox;
--   DROP POLICY IF EXISTS integration_outbox_scope ON integration_outbox;
--   DROP POLICY IF EXISTS job_scope ON job;
--   DROP POLICY IF EXISTS integration_watermark_scope ON integration_watermark;
--   DROP POLICY IF EXISTS integration_rate_budget_scope ON integration_rate_budget;
--   DROP POLICY IF EXISTS integration_circuit_scope ON integration_circuit;
--   DROP POLICY IF EXISTS integration_event_scope ON integration_event;
--   ALTER TABLE integration_connection NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_connection DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_inbox NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_inbox DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_outbox NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_outbox DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE job NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE job DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_watermark NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_watermark DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_rate_budget NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_rate_budget DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_circuit NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_circuit DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE integration_event NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE integration_event DISABLE ROW LEVEL SECURITY;
--   DROP TRIGGER IF EXISTS integration_inbox_receipt_frozen ON integration_inbox;
--   DROP TRIGGER IF EXISTS integration_watermark_no_silent_rewind ON integration_watermark;
--   DROP TRIGGER IF EXISTS integration_event_no_update ON integration_event;
--   DROP TRIGGER IF EXISTS integration_event_no_delete ON integration_event;
--   DROP FUNCTION IF EXISTS assert_integration_inbox_receipt_frozen() CASCADE;
--   DROP FUNCTION IF EXISTS assert_integration_watermark_no_silent_rewind() CASCADE;
--   DROP FUNCTION IF EXISTS assert_integration_event_append_only() CASCADE;
--   DROP TABLE IF EXISTS integration_event, integration_circuit,
--     integration_rate_budget, integration_watermark, job, integration_outbox,
--     integration_inbox, integration_connection CASCADE;
--   -- Dropped LAST: the CHECK constraints above depend on them, so they
--   -- cannot go while any table that calls them still exists.
--   DROP FUNCTION IF EXISTS capex_payload_carries_restricted_key(jsonb);
--   DROP FUNCTION IF EXISTS capex_restricted_payload_keys();
--   -- capex_app's table privileges disappear with the tables; the REVOKE
--   -- below needs no separate undo.

-- ================================================ the classification helpers
-- The frozen list of JSON key names that must never appear in a stored
-- payload, and the predicate the CHECK constraints call.
--
-- Two functions rather than one inlined list, for the same reason
-- `app/backend/pg/approval_schema.py` exists: three tables and one Python
-- module all have to agree on this list, and a list written out four times is
-- a list that will differ in three of them.
-- `tests/test_pg_integration_schema.py` asserts the SQL list and
-- `integration_store.RESTRICTED_PAYLOAD_KEYS` name exactly the same keys, in
-- both directions.
--
-- The names are the Zoho-side spellings of the fields section 10.4 classifies
-- Regulated (GSTIN, PAN) and Restricted (vendor bank details). They are
-- lower-case because Zoho's JSON is; the predicate does NOT lower-case the
-- payload before matching, deliberately -- a payload arriving with `GST_No`
-- is a payload from a source we have not characterised, and quietly matching
-- it would hide that. It would also be caught by the redactor, which does
-- normalise, so the effect of the stricter form here is a narrower backstop,
-- not a wider hole.
CREATE OR REPLACE FUNCTION capex_restricted_payload_keys() RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ARRAY[
        -- Regulated -- Business / Tax Identifier (section 10.4). Cleartext lives in
        -- vendor_master.gst_no and vendor_master.pan_no, nowhere else.
        'gst_no', 'gstin', 'pan_no', 'pan',
        -- Restricted -- vendor bank details (section 10.4). Never displayed, never
        -- exported, and this product makes no payments, so there is no
        -- function anywhere that needs them.
        'bank_account_number', 'account_number', 'bank_accounts',
        'routing_number', 'ifsc', 'ifsc_code', 'swift_code', 'iban'
    ]::text[];
$$;

-- Text-scan, not a jsonb walk, and the choice is deliberate.
--
-- `jsonb`'s output format renders every object key as `"key": value` -- quote,
-- name, quote, colon -- so searching the rendered text for `"name":` finds the
-- key at ANY nesting depth with no recursion, no jsonpath, and no operator
-- whose IMMUTABLE-ness this file cannot verify without a database it does not
-- have. `jsonb_out`, `unnest` and `position` are all immutable, so the
-- function genuinely is what it is declared to be.
--
-- `position(needle IN haystack)` rather than `LIKE '%..%'` because `_` is a
-- LIKE wildcard and nine of the twelve names above contain one: `'gst_no'`
-- as a LIKE pattern also matches `gstXno`. That would only ever widen the
-- refusal, but a constraint that refuses rows for a reason its author did not
-- intend is a constraint nobody will trust when it fires.
--
-- plpgsql rather than SQL, and this one IS about the planner. A single-SELECT
-- SQL function is a candidate for inlining, and `expression_planner()` runs
-- over a CHECK constraint's expression when the constraint is created -- so an
-- inlinable body would be spliced into the stored constraint expression. The
-- body needed here contains an EXISTS, and a SubLink is not something a CHECK
-- constraint expression should be relied upon to carry. A plpgsql function is
-- never inlined, so what the constraint stores is a plain function call and
-- what runs at INSERT time is a loop. This file cannot be executed against a
-- database before it reaches CI, so where there was a choice between a clever
-- form and a form with no question attached to it, it took the second.
CREATE OR REPLACE FUNCTION capex_payload_carries_restricted_key(p_payload jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    rendered   text;
    restricted text;
BEGIN
    IF p_payload IS NULL THEN
        RETURN false;
    END IF;
    rendered := p_payload::text;
    FOREACH restricted IN ARRAY capex_restricted_payload_keys() LOOP
        IF position(('"' || restricted || '":') IN rendered) > 0 THEN
            RETURN true;
        END IF;
    END LOOP;
    RETURN false;
END;
$$;

GRANT EXECUTE ON FUNCTION capex_restricted_payload_keys() TO PUBLIC;
GRANT EXECUTE ON FUNCTION capex_payload_carries_restricted_key(jsonb) TO PUBLIC;

-- ==================================================== integration_connection
-- One tenant connection. Created first: every other table below references it.
--
-- `product` carries C1's two-valued Literal, not a Zoho product name string.
-- `dc` is the data centre code (`in`, `com`, `eu`, ...) the adapter turns into
-- a base URL; the base URL itself is NEVER stored, because D-14 has not
-- resolved which product this tenant runs and a persisted URL is exactly the
-- kind of product fact section 11 forbids reaching a caller.
--
-- `UNIQUE (entity_id, organization_id)` is C2's. It says one entity connects
-- to one Zoho organisation once. Two rows for the same pair would give two
-- watermarks, two rate budgets and two circuits over one real quota, and the
-- daily ceiling would then be overspent by exactly the factor nobody noticed.
--
-- MODE. `DEFAULT 'MOCK'` plus `ck_integration_connection_live_is_authorised`.
-- See point 2 of this file's header for why the default matters more than the
-- constraint: the constraint stops a live row without an authoriser, but the
-- default is what stops a row that never mentioned mode at all from being
-- live. section 11.9's rule is "no live call is made during development without
-- explicit user authorisation" and this pair is that rule, in the schema.
--
-- Deliberately NOT constrained: the order of mode transitions. MOCK ->
-- LIVE_WRITE in one step is representable. A ladder (MOCK -> SANDBOX ->
-- LIVE_READ -> LIVE_WRITE) would be a plausible-looking control that the
-- authorisation stamp already provides better -- every live mode needs a named
-- human either way -- and it would block the legitimate case of a tenant that
-- has no sandbox.
CREATE TABLE integration_connection (
    connection_id            text PRIMARY KEY,
    entity_id                text NOT NULL REFERENCES entity (entity_id),
    product                  text NOT NULL,
    dc                       text NOT NULL,
    organization_id          text NOT NULL,
    connector_name           text NOT NULL,
    mode                     text NOT NULL DEFAULT 'MOCK',
    -- Seeded from `Capabilities.daily_call_ceiling` (C1) and
    -- `Capabilities`'s per-minute equivalent. Columns rather than constants
    -- because both are plan-tier facts and D-14 has not established the tier;
    -- the defaults are the plan's documented ASSUMPTIONS (ERP Standard
    -- 2,000/day; 100/min per organisation, which holds across all three
    -- products), not verified tenant values.
    per_minute_call_ceiling  integer NOT NULL DEFAULT 100,
    daily_call_ceiling       integer NOT NULL DEFAULT 2000,
    live_authorised_at       timestamptz,
    live_authorised_by       text,
    live_authorisation_note  text,
    is_active                boolean NOT NULL DEFAULT true,
    created_at               timestamptz NOT NULL DEFAULT now(),
    created_by               text NOT NULL,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    updated_by               text NOT NULL,
    version_no               integer NOT NULL DEFAULT 1,
    CONSTRAINT uq_integration_connection_org
        UNIQUE (entity_id, organization_id),
    CONSTRAINT ck_integration_connection_product
        CHECK (product IN ('ERP', 'BOOKS_INVENTORY')),
    CONSTRAINT ck_integration_connection_mode
        CHECK (mode IN ('MOCK', 'SANDBOX', 'LIVE_READ', 'LIVE_WRITE')),
    -- A LIVE mode is unrepresentable without a named authoriser, a time and a
    -- stated reason. All three, because an authoriser with no note records
    -- who to blame without recording what they agreed to.
    CONSTRAINT ck_integration_connection_live_is_authorised CHECK (
        mode IN ('MOCK', 'SANDBOX')
        OR (live_authorised_at IS NOT NULL
            AND btrim(coalesce(live_authorised_by, '')) <> ''
            AND btrim(coalesce(live_authorisation_note, '')) <> '')
    ),
    CONSTRAINT ck_integration_connection_dc_not_blank
        CHECK (btrim(dc) <> ''),
    CONSTRAINT ck_integration_connection_org_id_not_blank
        CHECK (btrim(organization_id) <> ''),
    CONSTRAINT ck_integration_connection_connector_not_blank
        CHECK (btrim(connector_name) <> ''),
    CONSTRAINT ck_integration_connection_ceilings_positive
        CHECK (per_minute_call_ceiling > 0 AND daily_call_ceiling > 0),
    -- A daily ceiling below one minute's worth is not a daily ceiling; it
    -- would mean the per-minute window could never be spent in full even
    -- once. Catches a units mistake (2000 typed as 20) at write time rather
    -- than as a puzzling starvation two days later.
    CONSTRAINT ck_integration_connection_daily_exceeds_minute
        CHECK (daily_call_ceiling >= per_minute_call_ceiling)
);

CREATE INDEX ix_integration_connection_entity
    ON integration_connection (entity_id);
-- "Which connections may a job actually call?" -- the question SVC-INTEGRATION
-- asks on every tick, and the one that must never accidentally include a MOCK
-- row when it meant a live one, or the reverse.
CREATE INDEX ix_integration_connection_mode
    ON integration_connection (mode) WHERE is_active;

-- ========================================================= integration_inbox
-- One received external payload.
--
-- IDEMPOTENCY IS THE CONSTRAINT, NOT THE CODE (section 11.6). See point 1 of the
-- header. `uq_integration_inbox_idempotency` is named explicitly rather than
-- left to PostgreSQL's generated name because every inbound writer says
-- `ON CONFLICT ON CONSTRAINT uq_integration_inbox_idempotency`, and a
-- constraint targeted by name in application SQL must have a name the
-- application chose.
--
-- WHAT `DISCARDED` ACTUALLY MEANS HERE, since the constraint changes it.
-- C16 defines DISCARDED as "duplicate payload_sha for an already-processed
-- external_id -- idempotent no-op". With this UNIQUE in place that case never
-- reaches a row at all: the insert conflicts and nothing is written, so there
-- is nothing to mark DISCARDED. The state is therefore written by the
-- PROCESSOR, for the case the constraint cannot see -- a payload with a
-- DIFFERENT `payload_sha` for an `external_id` whose newer version has already
-- been applied, which is ordinary out-of-order delivery under section 11.5's
-- overlapping windows. This is recorded here rather than left to be
-- rediscovered, because a reader who assumes DISCARDED counts re-deliveries
-- will read a permanently near-zero number and conclude the poller is not
-- overlapping.
--
-- `external_status_raw` IS STORED VERBATIM AND NEVER OVERWRITTEN (contract
-- C3). Alongside it, `external_status_product` and `external_status_api_version`
-- name what produced the value -- a raw status is only evidence if you know
-- which product's vocabulary it belongs to, and the product is provisional.
-- Freezing is by trigger, because a CHECK cannot see OLD.
--
-- `payload` is the SANITISED payload. See the section 10.4 section of this file's
-- header for the whole of that argument; the four columns that carry it are
-- `payload_classification`, `redaction_policy_version`, `redacted_keys` and
-- the `payload_secret`/`payload_secret_key_id` envelope.
CREATE TABLE integration_inbox (
    inbox_id                     text PRIMARY KEY,
    connection_id                text NOT NULL
                                 REFERENCES integration_connection (connection_id)
                                 ON DELETE CASCADE,
    module                       text NOT NULL,
    external_id                  text NOT NULL,
    payload_sha                  text NOT NULL,
    payload                      jsonb NOT NULL,
    external_status_raw          text,
    external_status_product      text,
    external_status_api_version  text,
    received_at                  timestamptz NOT NULL DEFAULT now(),
    state                        text NOT NULL DEFAULT 'RECEIVED',
    attempts                     integer NOT NULL DEFAULT 0,
    max_attempts                 integer NOT NULL DEFAULT 8,
    next_attempt_at              timestamptz,
    processed_at                 timestamptz,
    -- QUARANTINED is never a guess (C16, section 11.8). The reason is mandatory so
    -- the reconciliation_exception raised alongside it can be reconstructed
    -- from this row if the two ever disagree.
    quarantine_reason            text,
    last_error                   text,
    correlation_id               text,
    -- ---- section 10.4 ------------------------------------------------------------
    payload_classification       text NOT NULL DEFAULT 'CONFIDENTIAL',
    -- NOT NULL and NO DEFAULT, deliberately. This is the column that makes
    -- "someone wrote an INSERT by hand and skipped the redactor" fail instead
    -- of succeed.
    redaction_policy_version     text NOT NULL,
    redacted_keys                text[] NOT NULL DEFAULT '{}',
    payload_secret               bytea,
    payload_secret_key_id        text,
    CONSTRAINT uq_integration_inbox_idempotency
        UNIQUE (connection_id, module, external_id, payload_sha),
    -- C16 `inbox` namespace, verbatim.
    CONSTRAINT ck_integration_inbox_state CHECK (
        state IN ('RECEIVED', 'PROCESSED', 'QUARANTINED', 'DISCARDED', 'DEAD')
    ),
    CONSTRAINT ck_integration_inbox_module_not_blank CHECK (btrim(module) <> ''),
    CONSTRAINT ck_integration_inbox_external_id_not_blank
        CHECK (btrim(external_id) <> ''),
    CONSTRAINT ck_integration_inbox_payload_sha_not_blank
        CHECK (btrim(payload_sha) <> ''),
    CONSTRAINT ck_integration_inbox_payload_is_object
        CHECK (jsonb_typeof(payload) = 'object'),
    -- A processed row must say when; an unprocessed one must not claim to
    -- have been. Biconditional, for the reason 008's
    -- `ck_approval_stage_instance_opened_at` is: the one-directional form let
    -- a RECEIVED row carry a processing timestamp it never earned.
    CONSTRAINT ck_integration_inbox_processed_at
        CHECK ((state = 'PROCESSED') = (processed_at IS NOT NULL)),
    CONSTRAINT ck_integration_inbox_quarantine_reason CHECK (
        (state = 'QUARANTINED'
            AND btrim(coalesce(quarantine_reason, '')) <> '')
        OR (state <> 'QUARANTINED' AND quarantine_reason IS NULL)
    ),
    CONSTRAINT ck_integration_inbox_attempts
        CHECK (attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts),
    -- C16: DEAD means "exceeded max_attempts". A DEAD row that has not is a
    -- row somebody gave up on early and recorded as though the policy had
    -- decided.
    CONSTRAINT ck_integration_inbox_dead_exhausted_attempts
        CHECK (state <> 'DEAD' OR attempts >= max_attempts),
    CONSTRAINT ck_integration_inbox_classification CHECK (
        payload_classification IN
            ('PUBLIC', 'CONFIDENTIAL', 'REGULATED', 'RESTRICTED')
    ),
    CONSTRAINT ck_integration_inbox_redaction_policy_not_blank
        CHECK (btrim(redaction_policy_version) <> ''),
    -- An envelope is only an envelope if the key that opens it is recorded.
    -- section 10.4: "Encryption is envelope with recorded key_id and a documented
    -- rotation procedure." Ciphertext whose key_id was not written down is
    -- not encrypted data; it is lost data.
    CONSTRAINT ck_integration_inbox_secret_key_id
        CHECK ((payload_secret IS NULL) = (payload_secret_key_id IS NULL)),
    -- A row claiming a REGULATED or RESTRICTED origin must show its working:
    -- either the names of the keys it removed, or an envelope holding what it
    -- kept. Neither, and the classification is a label with nothing behind it.
    CONSTRAINT ck_integration_inbox_classification_shows_its_working CHECK (
        payload_classification NOT IN ('REGULATED', 'RESTRICTED')
        OR cardinality(redacted_keys) > 0
        OR payload_secret IS NOT NULL
    ),
    -- The backstop. See the section 10.4 section of the header for exactly what this
    -- does and does not catch.
    CONSTRAINT ck_integration_inbox_payload_carries_no_restricted_key
        CHECK (NOT capex_payload_carries_restricted_key(payload))
);

-- The processing queue: "what has arrived on this connection and not been
-- dealt with". Partial, because PROCESSED and DISCARDED rows are the
-- overwhelming majority within a day and the queue never looks at them.
CREATE INDEX ix_integration_inbox_pending
    ON integration_inbox (connection_id, module, received_at)
    WHERE state IN ('RECEIVED', 'QUARANTINED');
-- The retry sweep. RECEIVED only: a QUARANTINED row is not waiting for
-- another attempt, it is waiting for a person. Retrying it on a timer would
-- re-run the mapping that already failed, and section 11.8 is explicit that an
-- unattributable record accumulates for review rather than being retried into
-- a guess.
CREATE INDEX ix_integration_inbox_retry
    ON integration_inbox (next_attempt_at)
    WHERE next_attempt_at IS NOT NULL AND state = 'RECEIVED';
-- "Show me everything we ever received for this Zoho document" -- the lookup
-- behind SCR-38/39 and behind every attribution question in section 11.8.
CREATE INDEX ix_integration_inbox_external
    ON integration_inbox (connection_id, module, external_id);
CREATE INDEX ix_integration_inbox_correlation
    ON integration_inbox (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX ix_integration_inbox_dead
    ON integration_inbox (connection_id, received_at) WHERE state = 'DEAD';

-- ======================================================== integration_outbox
-- One outbound document emission.
--
-- ZOHO DOCUMENTS NO IDEMPOTENCY HEADER (section 11.6, and fact 3 of the Wave 5
-- contracts). `dedupe_key` is the one we synthesise, written into the Zoho
-- unique custom field `cf_capex_ref`, and retried with
-- update-by-custom-field-unique-value. `uq_integration_outbox_dedupe_key` is
-- what makes our side at least as tight as Zoho's: Zoho enforces uniqueness of
-- that custom field within an organisation, so two local rows sharing a
-- dedupe_key would be two rows fighting over one remote document, and the
-- second would silently overwrite the first.
--
-- `uq_integration_outbox_local_id` is C2's, and says a local document is
-- emitted to a connection once. Together the two constraints mean a Function
-- killed after sending but before recording finds a row to UPDATE, which is
-- exactly fact 3's requirement.
CREATE TABLE integration_outbox (
    outbox_id       text PRIMARY KEY,
    connection_id   text NOT NULL
                    REFERENCES integration_connection (connection_id)
                    ON DELETE CASCADE,
    module          text NOT NULL,
    local_id        text NOT NULL,
    dedupe_key      text NOT NULL,
    payload         jsonb NOT NULL,
    state           text NOT NULL DEFAULT 'PENDING',
    attempts        integer NOT NULL DEFAULT 0,
    -- section 11.6: max_attempts=8, then DEAD and visible on SCR-39 with manual
    -- retry.
    max_attempts    integer NOT NULL DEFAULT 8,
    next_attempt_at timestamptz,
    external_id     text,
    sent_at         timestamptz,
    last_error      text,
    correlation_id  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL,
    CONSTRAINT uq_integration_outbox_local_id
        UNIQUE (connection_id, module, local_id),
    CONSTRAINT uq_integration_outbox_dedupe_key
        UNIQUE (connection_id, dedupe_key),
    -- C16 `outbox` namespace, verbatim.
    CONSTRAINT ck_integration_outbox_state
        CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'DEAD')),
    CONSTRAINT ck_integration_outbox_module_not_blank CHECK (btrim(module) <> ''),
    CONSTRAINT ck_integration_outbox_local_id_not_blank
        CHECK (btrim(local_id) <> ''),
    CONSTRAINT ck_integration_outbox_dedupe_key_not_blank
        CHECK (btrim(dedupe_key) <> ''),
    CONSTRAINT ck_integration_outbox_payload_is_object
        CHECK (jsonb_typeof(payload) = 'object'),
    -- C16's SENT is "accepted by Zoho; external_id persisted" -- so SENT
    -- without an external_id is a claim we cannot substantiate, and an
    -- external_id on any other state is a document we created and then
    -- recorded as not sent. Biconditional both ways, and both ways are wrong
    -- in a manner that costs a duplicate purchase order.
    CONSTRAINT ck_integration_outbox_sent_carries_external_id
        CHECK ((state = 'SENT') = (external_id IS NOT NULL)),
    CONSTRAINT ck_integration_outbox_sent_at
        CHECK ((state = 'SENT') = (sent_at IS NOT NULL)),
    CONSTRAINT ck_integration_outbox_attempts
        CHECK (attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts),
    -- A retryable failure that scheduled no retry is not retryable; it is a
    -- row that stops moving and never appears on any queue. section 11.6 gives the
    -- backoff formula, so there is always a value to write.
    CONSTRAINT ck_integration_outbox_failed_reschedules
        CHECK (state <> 'FAILED' OR next_attempt_at IS NOT NULL),
    CONSTRAINT ck_integration_outbox_dead_exhausted_attempts
        CHECK (state <> 'DEAD' OR attempts >= max_attempts),
    CONSTRAINT ck_integration_outbox_payload_carries_no_restricted_key
        CHECK (NOT capex_payload_carries_restricted_key(payload))
);

-- `drain_outbox` (section 2.2: cron, 1 min, 25 rows) claims from exactly this shape:
-- the rows that are due, oldest first. PENDING rows carry no next_attempt_at,
-- so `coalesce` is what the claim query orders by; the index carries both
-- states and the drain sorts.
CREATE INDEX ix_integration_outbox_due
    ON integration_outbox (connection_id, next_attempt_at, created_at)
    WHERE state IN ('PENDING', 'FAILED');
CREATE INDEX ix_integration_outbox_dead
    ON integration_outbox (connection_id, updated_at) WHERE state = 'DEAD';
CREATE INDEX ix_integration_outbox_external
    ON integration_outbox (connection_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX ix_integration_outbox_correlation
    ON integration_outbox (correlation_id) WHERE correlation_id IS NOT NULL;

-- ==================================================================== job
-- One bounded, chunked, checkpointed background job (section 2.2).
--
-- THE PLATFORM FACT THIS TABLE EXISTS FOR: a Catalyst Cron or Event Function
-- is killed at 15 minutes. Not slowed, not warned -- killed. So no job may
-- assume it will finish, every job must be resumable from a persisted cursor,
-- and "resumable" has to be a property of the ROW, because the process holding
-- the alternative does not survive to be asked.
--
--   `checkpoint`        the resumable cursor. section 2.2 names one per job kind:
--                       `integration_watermark.hwm` for the pollers,
--                       `last_po_id_swept` for the PO-anchored sweep,
--                       `(module, period)` for the control-total sweep. A
--                       jsonb column rather than eight typed ones, because the
--                       shape belongs to the job kind and this table must not
--                       need a migration to add a job.
--   `soft_deadline_at`  when this invocation must stop and commit. section 2.2 puts
--                       it at 12 minutes -- 80% of the ceiling.
--   `resume_count`      how many invocations this job has already consumed.
--
-- `soft_deadline_seconds` carries the POLICY (default 720 s = 12 min) and is
-- CHECK-bounded at 900 s, so the AppSail ceiling is a fact stated in the
-- schema rather than a number in a comment somebody later raises to 1800
-- because a job kept running out of time. `soft_deadline_at` is the instant
-- the current invocation computed from it. The bound is on the integer, not on
-- a timestamp difference, because every timestamptz arithmetic operator is
-- STABLE and would silently make this constraint session-dependent.
--
-- `principal_user_id` is NOT NULL: section 10.3 says jobs run as principals, never as
-- "no user", each an `app_user` row with `principal_kind='SERVICE'`.
-- `scope_snapshot` is the serialised `Scope` the row runs under, and it exists
-- for SVC-EXPORT: "an export runs under the REQUESTING user's scope, never its
-- own", which is only possible if the requester's scope is persisted onto the
-- job at enqueue time and rehydrated by whichever invocation happens to pick
-- it up.
CREATE TABLE job (
    job_id                text PRIMARY KEY,
    kind                  text NOT NULL,
    state                 text NOT NULL DEFAULT 'PENDING',
    -- Nullable: `verify_audit_chains` and `sweep_control_totals` are
    -- estate-wide and belong to no connection.
    connection_id         text REFERENCES integration_connection (connection_id)
                          ON DELETE CASCADE,
    -- Nullable for the same reason, and a NULL waives that scope dimension --
    -- the documented waiver semantics of `capex_dimension_permits`, identical
    -- to `approval_instance.project_id` in 008.
    entity_id             text REFERENCES entity (entity_id),
    project_id            text REFERENCES project (project_id),
    principal_user_id     text NOT NULL REFERENCES app_user (user_id),
    scope_snapshot        jsonb,
    checkpoint            jsonb,
    soft_deadline_at      timestamptz,
    soft_deadline_seconds integer NOT NULL DEFAULT 720,
    resume_count          integer NOT NULL DEFAULT 0,
    max_resume_count      integer NOT NULL DEFAULT 20,
    attempts              integer NOT NULL DEFAULT 0,
    max_attempts          integer NOT NULL DEFAULT 8,
    run_after             timestamptz NOT NULL DEFAULT now(),
    -- Crash reaping. A CLAIMED job whose `locked_until` has passed was held by
    -- an invocation the platform killed; the reaper returns it to PENDING.
    -- Both columns, because "held until when" without "held by whom" leaves an
    -- operator unable to tell a reap from a bug.
    locked_until          timestamptz,
    locked_by             text,
    started_at            timestamptz,
    finished_at           timestamptz,
    -- The record that the DEAD job was shouted about rather than quietly
    -- abandoned. See ck_job_dead_is_alerted.
    alerted_at            timestamptz,
    last_error            text,
    correlation_id        text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            text NOT NULL,
    -- C16 `job` namespace, verbatim.
    CONSTRAINT ck_job_state CHECK (
        state IN ('PENDING', 'CLAIMED', 'CHECKPOINTED', 'DONE', 'FAILED', 'DEAD')
    ),
    CONSTRAINT ck_job_kind_not_blank CHECK (btrim(kind) <> ''),
    CONSTRAINT ck_job_counters_non_negative
        CHECK (resume_count >= 0 AND attempts >= 0),
    CONSTRAINT ck_job_ceilings_positive
        CHECK (max_resume_count >= 1 AND max_attempts >= 1),
    CONSTRAINT ck_job_attempts_within_ceiling CHECK (attempts <= max_attempts),
    CONSTRAINT ck_job_resume_within_ceiling
        CHECK (resume_count <= max_resume_count),
    -- section 2.1: the Functions ceiling is 15 minutes. section 2.2 sets the soft deadline
    -- at 80% of it. Both ends are stated: a non-positive deadline would make
    -- every invocation return before doing anything, which looks exactly like
    -- a job that is merely slow.
    CONSTRAINT ck_job_soft_deadline_within_platform_ceiling
        CHECK (soft_deadline_seconds > 0 AND soft_deadline_seconds <= 900),
    -- "A job exceeding max_resume_count raises an alert rather than looping
    -- forever" (section 2.2), as a constraint rather than as a scheduler's good
    -- intentions. AT the ceiling the only representable states are the two
    -- that stop: DONE (it finished on its last permitted invocation) and DEAD.
    -- There is no UPDATE that returns a job at its ceiling to PENDING or
    -- CHECKPOINTED, so it cannot be claimed again.
    CONSTRAINT ck_job_resume_ceiling_is_terminal CHECK (
        resume_count < max_resume_count OR state IN ('DONE', 'DEAD')
    ),
    -- ...and the alert itself. A DEAD job with no `alerted_at` is a job that
    -- gave up silently, which is the failure section 2.2 is guarding against -- not
    -- the looping, which at least shows up in the cron logs.
    --
    -- Stated honestly: this records that the alert was RAISED, in the same
    -- statement that recorded the death. It cannot record that anybody read
    -- it. That is a monitoring property, not a schema one.
    CONSTRAINT ck_job_dead_is_alerted
        CHECK (state <> 'DEAD' OR alerted_at IS NOT NULL),
    -- A job that says it checkpointed must carry the cursor it checkpointed
    -- to. Without this, "CHECKPOINTED" degrades to "gave up politely", and
    -- the next invocation restarts from the beginning while the state column
    -- says progress was preserved.
    CONSTRAINT ck_job_checkpointed_carries_a_cursor
        CHECK (state <> 'CHECKPOINTED' OR checkpoint IS NOT NULL),
    -- A claimed job must say when it must stop and who is holding it, or the
    -- reaper cannot tell a running invocation from a dead one and will either
    -- steal live work or leak the row forever.
    CONSTRAINT ck_job_claimed_is_bounded CHECK (
        state <> 'CLAIMED'
        OR (soft_deadline_at IS NOT NULL
            AND locked_until IS NOT NULL
            AND btrim(coalesce(locked_by, '')) <> ''
            AND started_at IS NOT NULL)
    ),
    -- ...and the converse, so a reaped or completed row cannot keep a lock it
    -- no longer holds. Only a CLAIMED job holds one.
    CONSTRAINT ck_job_only_claimed_holds_a_lock CHECK (
        state = 'CLAIMED' OR (locked_until IS NULL AND locked_by IS NULL)
    ),
    CONSTRAINT ck_job_finished_at
        CHECK ((state IN ('DONE', 'DEAD')) = (finished_at IS NOT NULL)),
    CONSTRAINT ck_job_scope_snapshot_is_object
        CHECK (scope_snapshot IS NULL OR jsonb_typeof(scope_snapshot) = 'object'),
    CONSTRAINT ck_job_checkpoint_is_object
        CHECK (checkpoint IS NULL OR jsonb_typeof(checkpoint) = 'object')
);

-- THE CLAIM QUERY (section 2.2 step 1: `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n`).
-- Runnable states, then the time the row becomes due. Partial, because DONE
-- rows accumulate forever and the claim never looks at one.
CREATE INDEX ix_job_claimable
    ON job (kind, run_after)
    WHERE state IN ('PENDING', 'CHECKPOINTED', 'FAILED');
-- The reaper: CLAIMED rows whose holder was killed.
CREATE INDEX ix_job_expired_claims
    ON job (locked_until) WHERE state = 'CLAIMED';
-- SCR-38's operational list, and the alert sweep.
CREATE INDEX ix_job_dead ON job (kind, finished_at) WHERE state = 'DEAD';
CREATE INDEX ix_job_correlation
    ON job (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX ix_job_connection
    ON job (connection_id) WHERE connection_id IS NOT NULL;
CREATE INDEX ix_job_scope ON job (entity_id, project_id);

-- ==================================================== integration_watermark
-- The per-(connection, module) high-water mark.
--
-- WHY `overlap_seconds` HAS A FLOOR AND NOT JUST A DEFAULT.
--
-- Fact 2 of the Wave 5 contracts: `last_modified_time` is FILTERABLE but NOT
-- SORTABLE on ERP/Books bills and purchase orders. A window can be selected;
-- it cannot be walked as a stable keyset. So every poll re-reads a bounded
-- window with a 300-second overlap (section 11.5: `[hwm - 300s, hwm + 24h]`), and the
-- inbox's UNIQUE constraint absorbs whatever the overlap re-delivers.
--
-- The overlap is the ONLY thing standing between us and a record modified
-- during the instant between one poll's read and its watermark write being
-- lost permanently -- there is no sort order that would let a later poll
-- notice the gap. `ck_integration_watermark_overlap_floor` makes 300 a floor
-- rather than a default, because a default is a number the next person tunes
-- down when the daily budget gets tight, and the cost of that tuning is
-- silent, permanent, and invisible until an auditor counts.
--
-- REWINDING. A rewind is legitimate (a backfill, a corrected mapping) and must
-- therefore be possible. It is not free: on ERP Standard, re-pulling a month
-- can consume a whole day's 2,000 calls, and the poller will then starve for
-- the rest of the day with no error anywhere. So a rewind is a stamp, not a
-- silent UPDATE -- `assert_integration_watermark_no_silent_rewind` refuses a
-- decrease unless the same statement records `rewound_at` and a non-blank
-- `rewind_reason`. This is the same treatment `approval_delegation` gives
-- revocation in 008, for the same reason: the act needs to survive in the row.
CREATE TABLE integration_watermark (
    connection_id     text NOT NULL
                      REFERENCES integration_connection (connection_id)
                      ON DELETE CASCADE,
    module            text NOT NULL,
    hwm               timestamptz NOT NULL,
    overlap_seconds   integer NOT NULL DEFAULT 300,
    last_polled_at    timestamptz,
    last_page_count   integer,
    rewound_at        timestamptz,
    rewind_reason     text,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL,
    PRIMARY KEY (connection_id, module),
    CONSTRAINT ck_integration_watermark_module_not_blank
        CHECK (btrim(module) <> ''),
    -- The floor, not a default. See above.
    CONSTRAINT ck_integration_watermark_overlap_floor
        CHECK (overlap_seconds >= 300),
    CONSTRAINT ck_integration_watermark_page_count
        CHECK (last_page_count IS NULL OR last_page_count >= 0),
    CONSTRAINT ck_integration_watermark_rewind_stamp CHECK (
        (rewound_at IS NULL AND rewind_reason IS NULL)
        OR (rewound_at IS NOT NULL
            AND btrim(coalesce(rewind_reason, '')) <> '')
    )
);

-- ================================================= integration_rate_budget
-- The per-minute AND per-day call budgets. See the header for why the daily
-- window is the binding one and why both windows share one shape.
--
-- `allocation` is section 11.6's split -- 60 polling / 30 outbound / 10 interactive
-- per minute -- and it applies to the DAY window too, which is the part the
-- plan states only for the minute. The argument is the same and it is worse by
-- a factor of the day: "an operator clicking Test connection never starves
-- behind a backfill" is a promise a per-minute-only split cannot keep, because
-- a backfill that spends 2,000 calls before lunch starves the operator for the
-- rest of the day no matter how politely it paced itself minute by minute.
--
-- `ceiling` is a per-ROW copy of the allocation's share of
-- `integration_connection`'s ceiling at the moment the window opened, not a
-- join. Deliberate: raising a tenant's plan tier mid-day must not
-- retroactively rewrite what yesterday's budget was, and the `used <= ceiling`
-- constraint has to compare against a value that cannot move under it.
--
-- `window_start_key` is the bucket identity as the application computed it
-- ('2026-09-06' for a day, '2026-09-06T14:23' for a minute) and `window_tz`
-- names the zone that computation used. Zoho's daily quota reset boundary is
-- a tenant fact nobody has verified -- Phase 0B-2 has not run -- so this
-- schema records which midnight was assumed rather than encoding one. No CHECK
-- here derives a boundary from `window_start`: every timestamptz truncation
-- and interval operator in PostgreSQL is STABLE, and a STABLE expression in a
-- CHECK is a constraint whose truth depends on the session that happens to
-- evaluate it.
CREATE TABLE integration_rate_budget (
    connection_id     text NOT NULL
                      REFERENCES integration_connection (connection_id)
                      ON DELETE CASCADE,
    window_kind       text NOT NULL,
    allocation        text NOT NULL,
    window_start      timestamptz NOT NULL,
    window_start_key  text NOT NULL,
    window_seconds    integer NOT NULL,
    window_tz         text NOT NULL,
    ceiling           integer NOT NULL,
    used              integer NOT NULL DEFAULT 0,
    exhausted_at      timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, window_kind, allocation, window_start_key),
    -- C2: window_kind IN (MINUTE, DAY), "because on ERP Standard the DAILY
    -- ceiling is the binding one".
    CONSTRAINT ck_integration_rate_budget_window_kind
        CHECK (window_kind IN ('MINUTE', 'DAY')),
    CONSTRAINT ck_integration_rate_budget_allocation
        CHECK (allocation IN ('POLLING', 'OUTBOUND', 'INTERACTIVE')),
    -- Integer seconds, not a timestamp difference, so the constraint is
    -- immutable and means the same thing in every session. A MINUTE row that
    -- claimed 86,400 seconds would make the per-minute throttle a per-day one
    -- and nothing else in the system would notice.
    CONSTRAINT ck_integration_rate_budget_window_seconds CHECK (
        (window_kind = 'MINUTE' AND window_seconds = 60)
        OR (window_kind = 'DAY' AND window_seconds = 86400)
    ),
    CONSTRAINT ck_integration_rate_budget_window_key_not_blank
        CHECK (btrim(window_start_key) <> ''),
    CONSTRAINT ck_integration_rate_budget_window_tz_not_blank
        CHECK (btrim(window_tz) <> ''),
    CONSTRAINT ck_integration_rate_budget_ceiling_positive CHECK (ceiling > 0),
    -- THE BINDING CONSTRAINT. The reservation is
    -- `UPDATE ... SET used = used + n ... RETURNING used`, so an over-budget
    -- call is refused by the statement that would have spent it. With no
    -- resident process, two cron invocations reserving at the same instant
    -- have nowhere else the race could be settled -- a read-then-check in
    -- Python is stale by the time it is acted on, and it is stale in the
    -- direction that overspends.
    CONSTRAINT ck_integration_rate_budget_used_within_ceiling
        CHECK (used >= 0 AND used <= ceiling),
    -- Exhaustion is a fact about the row, recorded by the same statement that
    -- made it true. Biconditional so a window that refilled (it cannot -- a
    -- window is immutable once elapsed, and a new window is a new row) or one
    -- stamped early cannot exist.
    CONSTRAINT ck_integration_rate_budget_exhausted_stamp
        CHECK ((exhausted_at IS NOT NULL) = (used >= ceiling))
);

-- THE DAILY QUESTION, FIRST CLASS. "How much of today is left on this
-- connection?" gets its own index rather than sharing one with the minute
-- rows, because it is the question section 11.6 says actually binds and it is asked
-- before every single outbound call and every poll page.
CREATE INDEX ix_integration_rate_budget_day
    ON integration_rate_budget (connection_id, window_start_key, allocation)
    WHERE window_kind = 'DAY';
CREATE INDEX ix_integration_rate_budget_minute
    ON integration_rate_budget (connection_id, window_start_key, allocation)
    WHERE window_kind = 'MINUTE';
-- The reaper that trims elapsed minute windows, and the operator's "when did
-- we last run out" on SCR-26.
CREATE INDEX ix_integration_rate_budget_window_start
    ON integration_rate_budget (window_start);
CREATE INDEX ix_integration_rate_budget_exhausted
    ON integration_rate_budget (connection_id, exhausted_at)
    WHERE exhausted_at IS NOT NULL;

-- ====================================================== integration_circuit
-- The per-(connection, module) circuit breaker. NOT one of C2's seven -- see
-- the header for why it is here anyway.
--
-- C16's `circuit` namespace, whose codes are prefixed CIRCUIT_ deliberately:
-- the bare code CLOSED collides with the C3 business status CLOSED and the two
-- mean opposite things. A closed circuit is healthy and passing traffic; a
-- closed project is terminal and blocks posting. C16's own naming note records
-- that the collision was caught by a contract test on first run.
--
-- section 11.6's table decides which failures COUNT. 429/44 (our own throttle failed)
-- and 429/45 (daily quota) do not; 5xx, timeouts and 401 do.
-- `consecutive_counted_failures` is therefore named for what it counts, so
-- nobody increments it on a 429 and wonders why the breaker opens every
-- afternoon at the same time.
--
-- `opened_reason` is mandatory when open, because "5 failures in 60 s" and
-- "daily quota exhausted, hold until the day boundary" produce the same state
-- and need entirely different operator responses -- one is a vendor incident,
-- the other is our own capacity planning.
CREATE TABLE integration_circuit (
    connection_id                text NOT NULL
                                 REFERENCES integration_connection (connection_id)
                                 ON DELETE CASCADE,
    module                       text NOT NULL,
    state                        text NOT NULL DEFAULT 'CIRCUIT_CLOSED',
    consecutive_counted_failures integer NOT NULL DEFAULT 0,
    first_failure_at             timestamptz,
    opened_at                    timestamptz,
    opened_reason                text,
    next_probe_at                timestamptz,
    last_probe_at                timestamptz,
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, module),
    -- C16 `circuit` namespace, verbatim, CIRCUIT_ prefixes included.
    CONSTRAINT ck_integration_circuit_state CHECK (
        state IN ('CIRCUIT_CLOSED', 'CIRCUIT_OPEN', 'CIRCUIT_HALF_OPEN')
    ),
    CONSTRAINT ck_integration_circuit_module_not_blank
        CHECK (btrim(module) <> ''),
    CONSTRAINT ck_integration_circuit_failures_non_negative
        CHECK (consecutive_counted_failures >= 0),
    -- An OPEN circuit must say when it opened, why, and when a probe may be
    -- attempted. Without `next_probe_at` the breaker has no way back: OPEN
    -- with no scheduled probe is not a circuit breaker, it is an outage that
    -- requires a human to notice.
    CONSTRAINT ck_integration_circuit_open_is_explained CHECK (
        state <> 'CIRCUIT_OPEN'
        OR (opened_at IS NOT NULL
            AND btrim(coalesce(opened_reason, '')) <> ''
            AND next_probe_at IS NOT NULL)
    ),
    -- A closed circuit holds no OPEN state. It may perfectly well hold a
    -- failure count: four counted failures with the threshold at five is a
    -- circuit that is closed and passing traffic, which is the normal state
    -- of a breaker under intermittent trouble and not an anomaly.
    --
    -- An earlier draft of this constraint also required
    -- `consecutive_counted_failures = 0` for a CLOSED row. That was simply
    -- wrong -- it made the first four failures of every outage unstorable --
    -- and it is recorded here rather than quietly corrected because it is
    -- exactly the class of mistake this file cannot test for locally: it
    -- would have passed every text assertion and failed on the first INSERT
    -- in CI.
    CONSTRAINT ck_integration_circuit_closed_is_not_open CHECK (
        state <> 'CIRCUIT_CLOSED'
        OR (opened_at IS NULL AND opened_reason IS NULL
            AND next_probe_at IS NULL)
    ),
    -- The failure WINDOW, though, is a biconditional: section 11.6 counts "5
    -- consecutive counted failures in 60 s", so a count above zero must say
    -- when the run began or the 60-second window cannot be evaluated, and a
    -- count of zero must not keep a start time from a run that is over.
    CONSTRAINT ck_integration_circuit_failure_window CHECK (
        (consecutive_counted_failures > 0) = (first_failure_at IS NOT NULL)
    )
);

CREATE INDEX ix_integration_circuit_open
    ON integration_circuit (next_probe_at)
    WHERE state IN ('CIRCUIT_OPEN', 'CIRCUIT_HALF_OPEN');

-- ======================================================== integration_event
-- APPEND-ONLY. The correlation trail.
--
-- section 11.9: "`correlation_id` propagates from the existing middleware through
-- `job` -> inbox/outbox -> `integration_event` -> `audit_log`, so one id
-- traces a Zoho bill from HTTP response to ledger movement to audit entry."
-- This table is the integration segment of that chain, and it is append-only
-- for the same reason `audit_log` and `approval_action` are: a trail that can
-- be rewritten answers a different question from the one it is asked.
--
-- Both mechanisms, exactly as 001 and 008 use both: a BEFORE UPDATE OR DELETE
-- trigger (the schema stating its intent, and a catch for a misconfigured
-- grant) PLUS `REVOKE UPDATE, DELETE ... FROM capex_app` (the real guarantee,
-- a privilege one). A future migration re-running a blanket
-- `GRANT ... ON ALL TABLES` would silently undo the REVOKE; a superuser
-- bypasses privileges but not triggers. Neither alone.
--
-- `detail` gets the same restricted-key backstop as the two payload columns.
-- An event recording "bill 12345 could not be attributed" is exactly where a
-- well-meaning debug field carrying the offending vendor record would end up.
--
-- A separate append-only function from 001's `assert_append_only()` and 008's
-- `assert_approval_action_append_only()`, deliberately: each raises a message
-- naming its own table, and an operator debugging a refused write here should
-- not be told that `audit_log` is append-only.
CREATE TABLE integration_event (
    event_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    connection_id   text REFERENCES integration_connection (connection_id)
                    ON DELETE CASCADE,
    correlation_id  text,
    kind            text NOT NULL,
    module          text,
    at              timestamptz NOT NULL DEFAULT now(),
    job_id          text REFERENCES job (job_id) ON DELETE SET NULL,
    inbox_id        text REFERENCES integration_inbox (inbox_id) ON DELETE SET NULL,
    outbox_id       text REFERENCES integration_outbox (outbox_id) ON DELETE SET NULL,
    actor           text NOT NULL,
    detail          jsonb,
    CONSTRAINT ck_integration_event_kind_not_blank CHECK (btrim(kind) <> ''),
    CONSTRAINT ck_integration_event_actor_not_blank CHECK (btrim(actor) <> ''),
    CONSTRAINT ck_integration_event_detail_is_object
        CHECK (detail IS NULL OR jsonb_typeof(detail) = 'object'),
    CONSTRAINT ck_integration_event_detail_carries_no_restricted_key
        CHECK (NOT capex_payload_carries_restricted_key(detail))
);

-- The trace: "everything that happened under this correlation id", which is
-- the whole point of the table.
CREATE INDEX ix_integration_event_correlation
    ON integration_event (correlation_id, at)
    WHERE correlation_id IS NOT NULL;
CREATE INDEX ix_integration_event_connection
    ON integration_event (connection_id, at DESC);
CREATE INDEX ix_integration_event_kind ON integration_event (kind, at DESC);
CREATE INDEX ix_integration_event_job
    ON integration_event (job_id) WHERE job_id IS NOT NULL;
CREATE INDEX ix_integration_event_inbox
    ON integration_event (inbox_id) WHERE inbox_id IS NOT NULL;
CREATE INDEX ix_integration_event_outbox
    ON integration_event (outbox_id) WHERE outbox_id IS NOT NULL;

-- ================================ immutability 1: what a receipt recorded
-- `integration_inbox` records what arrived. `connection_id`, `module`,
-- `external_id`, `payload_sha`, `payload`, `received_at` and the three
-- `external_status_*` columns are the receipt; the rest is our processing of
-- it.
--
-- `external_status_raw` in particular is required by contract C3 to be
-- "stored verbatim and never overwritten", and that requirement is what makes
-- an UNMAPPED_EXTERNAL_STATUS reconciliation exception resolvable months
-- later: the raw value is still there, still exactly as the vendor sent it,
-- still next to the product and API version that produced it.
--
-- The identity columns are frozen alongside it for a sharper reason: they are
-- what `uq_integration_inbox_idempotency` is built from. An UPDATE that moved
-- `payload_sha` would move the row out from under the constraint that
-- deduplicated it, and the next delivery of the original payload would insert
-- a second row. Freezing them means the idempotency guarantee holds for the
-- life of the row and not merely at the instant of insert.
--
-- Column-scoped with a WHEN clause, not a table-wide refusal: `state`,
-- `attempts`, `next_attempt_at`, `processed_at`, `quarantine_reason` and
-- `last_error` must all remain updatable, because processing a receipt is the
-- entire purpose of having one. An ordinary state transition never calls this
-- function at all.
CREATE OR REPLACE FUNCTION assert_integration_inbox_receipt_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'integration_inbox %: the receipt columns (connection_id, module, external_id, payload_sha, payload, received_at, external_status_raw, external_status_product, external_status_api_version) are frozen at insert: UPDATE denied. A payload that differs is a NEW receipt with its own payload_sha, never a rewrite of the one already recorded.',
        OLD.inbox_id
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER integration_inbox_receipt_frozen BEFORE UPDATE ON integration_inbox
    FOR EACH ROW WHEN (
        OLD.connection_id                  IS DISTINCT FROM NEW.connection_id
        OR OLD.module                      IS DISTINCT FROM NEW.module
        OR OLD.external_id                 IS DISTINCT FROM NEW.external_id
        OR OLD.payload_sha                 IS DISTINCT FROM NEW.payload_sha
        OR OLD.payload                     IS DISTINCT FROM NEW.payload
        OR OLD.received_at                 IS DISTINCT FROM NEW.received_at
        OR OLD.external_status_raw         IS DISTINCT FROM NEW.external_status_raw
        OR OLD.external_status_product     IS DISTINCT FROM NEW.external_status_product
        OR OLD.external_status_api_version IS DISTINCT FROM NEW.external_status_api_version)
    EXECUTE FUNCTION assert_integration_inbox_receipt_frozen();

-- ==================== immutability 2: a watermark never rewinds in silence
-- Moving `hwm` BACKWARDS re-pulls a window we have already read. That is safe
-- for correctness -- `uq_integration_inbox_idempotency` absorbs every
-- duplicate -- and expensive for capacity, which on ERP Standard is the same
-- thing: re-pulling a month can spend a whole day's 2,000 calls, after which
-- the poller starves silently until midnight and every downstream number is
-- quietly stale.
--
-- So a rewind must be DECLARED. The trigger refuses a decrease unless the same
-- statement writes `rewound_at` and a non-blank `rewind_reason`. A CHECK
-- cannot do this: it cannot see OLD, so it cannot know a decrease happened.
--
-- Forward movement is unconstrained, deliberately, and the asymmetry is worth
-- stating because the forward direction is the one that loses data. There is
-- no value this trigger could compare a forward jump against: the poller is
-- the only thing that knows how far it actually read, and fact 2 says the feed
-- cannot be walked as a keyset, so nothing downstream can detect a skip. The
-- protection against a forward jump is section 11.5's completeness sweeps, not this
-- trigger, and pretending otherwise here would be the more dangerous comment.
CREATE OR REPLACE FUNCTION assert_integration_watermark_no_silent_rewind() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.hwm < OLD.hwm
       AND (NEW.rewound_at IS NULL
            OR NEW.rewound_at IS NOT DISTINCT FROM OLD.rewound_at
            OR btrim(coalesce(NEW.rewind_reason, '')) = '') THEN
        RAISE EXCEPTION
            'integration_watermark (%, %): hwm may not move backwards from % to % without recording rewound_at and a rewind_reason in the same statement. A rewind re-pulls a window we have already read, which on a 2,000-call daily ceiling can starve the poller for the rest of the day.',
            OLD.connection_id, OLD.module, OLD.hwm, NEW.hwm
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER integration_watermark_no_silent_rewind
    BEFORE UPDATE ON integration_watermark
    FOR EACH ROW WHEN (NEW.hwm < OLD.hwm)
    EXECUTE FUNCTION assert_integration_watermark_no_silent_rewind();

-- ================================= immutability 3: the correlation trail
CREATE OR REPLACE FUNCTION assert_integration_event_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'integration_event is append-only: % denied', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER integration_event_no_update BEFORE UPDATE ON integration_event
    FOR EACH ROW EXECUTE FUNCTION assert_integration_event_append_only();
CREATE TRIGGER integration_event_no_delete BEFORE DELETE ON integration_event
    FOR EACH ROW EXECUTE FUNCTION assert_integration_event_append_only();

-- ================================================== privileges for capex_app
-- 004_identity_scope.sql set ALTER DEFAULT PRIVILEGES for the role that runs
-- migrations, so tables created here normally reach capex_app already. Stated
-- explicitly anyway, for the reason 008 states them: default privileges attach
-- to the role that issued them, and a deployment whose 010 is applied by a
-- different identity than its 004 would otherwise leave the application unable
-- to read its own new tables -- a failure that shows up only in that
-- deployment.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    integration_connection, integration_inbox, integration_outbox, job,
    integration_watermark, integration_rate_budget, integration_circuit,
    integration_event
    TO capex_app;

-- ...and immediately narrowed back down for the append-only table, in the same
-- migration rather than trusting every future caller to remember. Mirrors
-- 004's `REVOKE UPDATE, DELETE ON audit_log, audit_anchor FROM capex_app` and
-- 008's on `approval_action`.
REVOKE UPDATE, DELETE ON integration_event FROM capex_app;

-- ================================================================ RLS
-- Every policy calls the frozen `capex_scope_permits` from 004, whose body 007
-- replaced with the mode/ids wire format. An absent session setting denies: a
-- connection that never went through `Database.session()` reads nothing.

-- ------------------------------------------------- the connection itself
-- Carries `entity_id`, so it is scopable directly. plant, location and project
-- are waived because the table has no column for any of them -- the same
-- waiver `entity_scope` uses in 004 and `approval_definition_scope` in 008.
--
-- Scoping this table is not cosmetic: the row names the tenant's Zoho
-- organisation id and its connector, which is the information an attacker
-- needs to know WHERE another entity's financial data lives.
ALTER TABLE integration_connection ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_connection FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_connection_scope ON integration_connection
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

-- -------------------------------------------- everything hung off a connection
-- No dimension column of their own; each reaches one through the connection it
-- belongs to, in the shape `approval_rule_scope` uses in 008 and
-- `budget_line_scope` in 006. A row whose connection cannot be found is
-- DENIED, not permitted -- `EXISTS` is false, so the row is invisible. Fail
-- closed.
--
-- `integration_inbox` is the one where this matters most and is least obvious:
-- the payload is another entity's supplier invoices. Leaving it bare because
-- "it is only integration plumbing" would put every entity's purchase ledger
-- behind a table nobody thought of as financial.
ALTER TABLE integration_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_inbox_scope ON integration_inbox
    USING (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_inbox.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_inbox.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

ALTER TABLE integration_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_outbox_scope ON integration_outbox
    USING (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_outbox.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_outbox.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

ALTER TABLE integration_watermark ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_watermark FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_watermark_scope ON integration_watermark
    USING (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_watermark.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_watermark.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

ALTER TABLE integration_rate_budget ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_rate_budget FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_rate_budget_scope ON integration_rate_budget
    USING (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_rate_budget.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_rate_budget.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

ALTER TABLE integration_circuit ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_circuit FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_circuit_scope ON integration_circuit
    USING (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_circuit.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_circuit.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

-- `integration_event.connection_id` is NULLABLE -- an event can be raised
-- before a connection is resolved, and an estate-wide job's events belong to
-- no connection. A NULL therefore WAIVES the dimension and the row is visible
-- to any established principal, which is the same waiver semantics
-- `capex_dimension_permits` applies to a SQL NULL and the same one
-- `approval_instance.project_id` relies on in 008.
--
-- That waiver is stated rather than hidden because it is a real decision: an
-- event whose connection is unknown carries a kind, a correlation id and a
-- `detail` object, and the restricted-key CHECK is what keeps the last of
-- those from becoming an unscoped copy of somebody's vendor record. The two
-- controls are complementary and neither substitutes for the other.
ALTER TABLE integration_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_event FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_event_scope ON integration_event
    USING (
        integration_event.connection_id IS NULL
        OR EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_event.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        integration_event.connection_id IS NULL
        OR EXISTS (
            SELECT 1 FROM integration_connection c
            WHERE c.connection_id = integration_event.connection_id
              AND capex_scope_permits(c.entity_id, NULL, NULL, NULL)
        )
    );

-- --------------------------------------------------------------------- job
-- `job` carries its own `entity_id` and `project_id`, so it is scoped
-- DIRECTLY rather than through a connection -- which is necessary, because
-- `connection_id` is nullable and an estate-wide sweep would otherwise be
-- scoped by a column that is NULL on exactly the jobs that touch everything.
--
-- plant and location are waived: the table has no column for either, and both
-- are already enforced at `project`, which carries its own policy.
--
-- A job with a NULL `entity_id` waives the entity dimension and is visible to
-- any established principal. That is deliberate for the estate-wide job kinds
-- (`verify_audit_chains`, `sweep_control_totals`, `expire_pr_reservations`),
-- whose rows carry a kind, a checkpoint cursor and an error string rather than
-- any entity's data. A job that DOES belong to an entity must say so, and
-- `app.backend.pg.integration_store.enqueue_job` requires the caller to pass
-- `entity_id` explicitly -- including passing None -- so the waiver is a
-- decision at the call site rather than a forgotten argument.
ALTER TABLE job ENABLE ROW LEVEL SECURITY;
ALTER TABLE job FORCE ROW LEVEL SECURITY;
CREATE POLICY job_scope ON job
    USING      (capex_scope_permits(entity_id, NULL, NULL, project_id))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, project_id));
