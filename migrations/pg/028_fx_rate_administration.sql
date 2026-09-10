-- 028_fx_rate_administration.sql
-- The exchange-rate table gets a lifecycle, so an operator can maintain it
-- through a supported screen instead of through an ingestion side effect.
--
-- WHAT WAS MISSING
-- ================
--
-- Migration 023 created `fx_rate` and `pg/fx.py::record_rate` was its only
-- writer -- a rate arrived either on a vendor's own document or not at all.
-- There was no way to load a month of reference rates ahead of the bills that
-- need them, no way to retire a rate that was keyed wrongly, and no record of
-- who put a rate into force as distinct from who typed it. `resolve_basis`
-- already REFUSED a missing rate (AUD-H-007 is closed at that seam), so the
-- gap was operational: the refusal named a date and nobody had a door to
-- record the rate for it.
--
-- WHAT THIS ADDS, AND WHAT IT DOES NOT
-- ====================================
--
-- * `active` and the activation/deactivation metadata. A rate created through
--   the administration API starts INACTIVE and is put into force by a
--   DIFFERENT user (`fx_policy.FX_ACTIVATION_SEPARATION`, seeded REQUIRED).
--   The lookup in `pg/fx.py` and the new `pg/fx_admin.py` read ACTIVE rows
--   only, so a pending or retired rate translates nothing.
--
-- * `superseded_by`. A correction is a NEW row that, on activation, marks the
--   row it replaces inactive and points it at itself. The old value stays on
--   the old row: every bill that cites that `fx_rate_id` still cites the rate
--   it was actually translated at.
--
-- * `trg_fx_rate_history_immutable`. The numeric value, the pair, the date,
--   the source and the provenance of a rate row can never change once written,
--   and no row can be deleted -- at the DATABASE, not by discipline. The only
--   columns an UPDATE may touch are the lifecycle ones.
--
-- * `ux_fx_rate_natural` becomes a PARTIAL unique index over ACTIVE rows. 023
--   made it a table constraint over every row, which would have refused the
--   correction row while the row it corrects was still active. One ACTIVE
--   quote per (pair, date, source) is the invariant that matters; history may
--   hold as many superseded ones as it took to get there.
--
-- It does NOT add a policy that makes a missing rate fall back to 1, to the
-- previous day, or to a stale row. There is no such switch, deliberately.
--
-- Rows already present (ingestion-recorded rates) default to `active = true`
-- with no activation metadata: they were in force when 023 recorded them and
-- this migration does not rewrite history it did not witness.

BEGIN;

-- ============================================================ lifecycle
ALTER TABLE fx_rate
    ADD COLUMN active              boolean     NOT NULL DEFAULT true,
    ADD COLUMN activated_at        timestamptz,
    ADD COLUMN activated_by        text,
    ADD COLUMN deactivated_at      timestamptz,
    ADD COLUMN deactivated_by      text,
    ADD COLUMN deactivation_reason text,
    ADD COLUMN superseded_by       text REFERENCES fx_rate (fx_rate_id),
    ADD COLUMN note                text,
    ADD COLUMN updated_at          timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_by          text;

ALTER TABLE fx_rate
    ADD CONSTRAINT ck_fx_rate_not_self_superseded
        CHECK (superseded_by IS NULL OR superseded_by <> fx_rate_id),
    -- A superseded row is never active: the pointer means "read that one".
    ADD CONSTRAINT ck_fx_rate_superseded_is_inactive
        CHECK (superseded_by IS NULL OR active = false),
    -- Deactivation is recorded with its actor, or not at all.
    ADD CONSTRAINT ck_fx_rate_deactivation_attributed
        CHECK ((deactivated_at IS NULL) = (deactivated_by IS NULL)),
    ADD CONSTRAINT ck_fx_rate_activation_attributed
        CHECK ((activated_at IS NULL) = (activated_by IS NULL));

COMMENT ON COLUMN fx_rate.active IS
    'Whether this quote is in force. pg/fx.py::resolve_basis and pg/fx_admin.py::lookup_rate read ACTIVE rows only; a pending (never activated) or retired row translates nothing and the lookup refuses with FX_RATE_UNAVAILABLE. Administration-created rows start false and are activated by a different user (fx_policy.FX_ACTIVATION_SEPARATION).';
COMMENT ON COLUMN fx_rate.superseded_by IS
    'The fx_rate_id of the correction that replaced this row. The value on THIS row never changes (trg_fx_rate_history_immutable): a bill that cites this id was translated at this rate and stays verifiable against it.';

-- ================================================ one ACTIVE quote per key
ALTER TABLE fx_rate DROP CONSTRAINT ux_fx_rate_natural;
CREATE UNIQUE INDEX ux_fx_rate_natural
    ON fx_rate (from_currency, to_currency, rate_date, rate_source)
    WHERE active;

CREATE INDEX ix_fx_rate_active_lookup
    ON fx_rate (from_currency, to_currency, rate_date DESC)
    WHERE active;

-- =================================================== append-only history
CREATE OR REPLACE FUNCTION fx_rate_history_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'fx_rate ' || OLD.fx_rate_id || ' cannot be deleted: rate history is append-only. Deactivate it, or supersede it with a corrected row.';
    END IF;
    IF NEW.rate             IS DISTINCT FROM OLD.rate
       OR NEW.from_currency    IS DISTINCT FROM OLD.from_currency
       OR NEW.to_currency      IS DISTINCT FROM OLD.to_currency
       OR NEW.rate_date        IS DISTINCT FROM OLD.rate_date
       OR NEW.rate_source      IS DISTINCT FROM OLD.rate_source
       OR NEW.source_reference IS DISTINCT FROM OLD.source_reference
       OR NEW.captured_at      IS DISTINCT FROM OLD.captured_at
       OR NEW.created_by       IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'fx_rate ' || OLD.fx_rate_id || ' is immutable: the rate, pair, date, source and provenance of a recorded quote never change. Record the correction as a new row and activate it; the old row is superseded, not edited.';
    END IF;
    -- Activation and supersession are written once each.
    IF OLD.activated_at IS NOT NULL AND NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = 'fx_rate ' || OLD.fx_rate_id || ': activation metadata is written once.';
    END IF;
    IF OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation',
            MESSAGE = 'fx_rate ' || OLD.fx_rate_id || ': a superseded row stays superseded by the row that replaced it.';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fx_rate_history_immutable
    BEFORE UPDATE OR DELETE ON fx_rate
    FOR EACH ROW EXECUTE FUNCTION fx_rate_history_is_immutable();

-- ================================================ activation separation
-- The same shape as 023's three keys: every permitted value is named by the
-- CHECK, so an unknown value is refused by the database. WAIVED exists for a
-- single-controller deployment that has recorded the decision; REQUIRED is
-- the seeded default and the one every test below runs under.
ALTER TABLE fx_policy DROP CONSTRAINT ck_fx_policy_known;
ALTER TABLE fx_policy ADD CONSTRAINT ck_fx_policy_known CHECK (
        (policy_key = 'FX_PERIOD_END_REVALUATION'
         AND policy_value IN ('REFUSE_AND_RECORD', 'RECORD_ONLY'))
        OR (policy_key = 'FX_RATE_ROUNDING'
            AND policy_value IN ('HALF_UP'))
        OR (policy_key = 'FX_UNKNOWN_CURRENCY'
            AND policy_value IN ('REFUSE', 'ASSUME_MINOR_EXPONENT_2'))
        OR (policy_key = 'FX_ACTIVATION_SEPARATION'
            AND policy_value IN ('REQUIRED', 'WAIVED'))
    );

INSERT INTO fx_policy (policy_key, policy_value, description, updated_by)
VALUES
    ('FX_ACTIVATION_SEPARATION', 'REQUIRED',
     'Whether the user who ACTIVATES an exchange rate must differ from the '
     'user who CREATED it. REQUIRED (the seeded default) refuses a self-'
     'activation with FX_SELF_ACTIVATION. WAIVED permits it, for a deployment '
     'with a single controller that has recorded the decision. Neither value '
     'lets an inactive rate translate anything.',
     'SYSTEM');

-- Migration 028 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS. Rows created through the administration API in a
--   -- pending or retired state carry no meaning without `active`: dropping
--   -- the column makes every one of them a live quote, and the partial index
--   -- cannot be restored as a full constraint while a superseded row and its
--   -- correction share a natural key. Deactivated and superseded rows must be
--   -- reviewed by hand before this runs.
--   DELETE FROM fx_policy WHERE policy_key = 'FX_ACTIVATION_SEPARATION';
--   ALTER TABLE fx_policy DROP CONSTRAINT ck_fx_policy_known;
--   ALTER TABLE fx_policy ADD CONSTRAINT ck_fx_policy_known CHECK (
--           (policy_key = 'FX_PERIOD_END_REVALUATION'
--            AND policy_value IN ('REFUSE_AND_RECORD', 'RECORD_ONLY'))
--           OR (policy_key = 'FX_RATE_ROUNDING'
--               AND policy_value IN ('HALF_UP'))
--           OR (policy_key = 'FX_UNKNOWN_CURRENCY'
--               AND policy_value IN ('REFUSE', 'ASSUME_MINOR_EXPONENT_2'))
--       );
--   DROP TRIGGER IF EXISTS trg_fx_rate_history_immutable ON fx_rate;
--   DROP FUNCTION IF EXISTS fx_rate_history_is_immutable();
--   DROP INDEX IF EXISTS ix_fx_rate_active_lookup;
--   DROP INDEX IF EXISTS ux_fx_rate_natural;
--   ALTER TABLE fx_rate
--       ADD CONSTRAINT ux_fx_rate_natural
--           UNIQUE (from_currency, to_currency, rate_date, rate_source);
--   COMMENT ON COLUMN fx_rate.superseded_by IS NULL;
--   COMMENT ON COLUMN fx_rate.active IS NULL;
--   ALTER TABLE fx_rate
--       DROP CONSTRAINT IF EXISTS ck_fx_rate_activation_attributed,
--       DROP CONSTRAINT IF EXISTS ck_fx_rate_deactivation_attributed,
--       DROP CONSTRAINT IF EXISTS ck_fx_rate_superseded_is_inactive,
--       DROP CONSTRAINT IF EXISTS ck_fx_rate_not_self_superseded,
--       DROP COLUMN IF EXISTS updated_by,
--       DROP COLUMN IF EXISTS updated_at,
--       DROP COLUMN IF EXISTS note,
--       DROP COLUMN IF EXISTS superseded_by,
--       DROP COLUMN IF EXISTS deactivation_reason,
--       DROP COLUMN IF EXISTS deactivated_by,
--       DROP COLUMN IF EXISTS deactivated_at,
--       DROP COLUMN IF EXISTS activated_by,
--       DROP COLUMN IF EXISTS activated_at,
--       DROP COLUMN IF EXISTS active;
--   DELETE FROM schema_migrations WHERE version = '028';
--   COMMIT;
