-- 001_foundation.sql
-- Milestone 1: production PostgreSQL foundation.
--
-- Organisation hierarchy, the identity table, the hash-chained audit log and
-- the accounting calendar. No financial documents yet; those arrive in 003+
-- once budget control lands.
--
-- Conventions established here and relied on by every later migration:
--   * money is bigint PAISE, never numeric, never float
--   * timestamps are timestamptz
--   * business keys are separate UNIQUE constraints, not primary keys
--   * every mirrored table carries external_source / external_id / payload_sha
--   * constraints are declarative wherever a constraint can express the rule
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS audit_anchor, audit_log, accounting_period,
--     app_user, department, location, plant, zone, branch, division, entity,
--     organisation CASCADE;
--   DROP FUNCTION IF EXISTS assert_append_only() CASCADE;
--   -- extensions are intentionally NOT dropped: other databases in the
--   -- cluster may depend on them, and dropping ltree would cascade to any
--   -- surviving wbs_element.

CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------- organisation
CREATE TABLE organisation (
    organisation_id   text PRIMARY KEY,
    code              text NOT NULL,
    name              text NOT NULL,
    base_currency     char(3) NOT NULL DEFAULT 'INR',
    -- Financial year start month. April (4) is the Indian default and the
    -- client's stated convention, but it is configuration, not an assumption
    -- baked into queries.
    fy_start_month    smallint NOT NULL DEFAULT 4
                      CHECK (fy_start_month BETWEEN 1 AND 12),
    is_active         boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL,
    version_no        integer NOT NULL DEFAULT 1,
    UNIQUE (code)
);

CREATE TABLE entity (
    entity_id         text PRIMARY KEY,
    organisation_id   text NOT NULL REFERENCES organisation (organisation_id),
    code              text NOT NULL,
    name              text NOT NULL,
    -- India tax identity. Regulated: encrypted or access-restricted per the
    -- approved data classification. Never hashed -- hashing destroys the
    -- matching and statutory-reporting function these exist for.
    gst_no            text,
    pan_no            text,
    fy_start_month    smallint CHECK (fy_start_month BETWEEN 1 AND 12),
    is_active         boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL,
    version_no        integer NOT NULL DEFAULT 1,
    UNIQUE (organisation_id, code)
);

CREATE TABLE division (
    division_id   text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    code          text NOT NULL,
    name          text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE TABLE branch (
    branch_id     text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    code          text NOT NULL,
    name          text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE TABLE zone (
    zone_id       text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    code          text NOT NULL,
    name          text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE TABLE plant (
    plant_id      text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    zone_id       text REFERENCES zone (zone_id),
    code          text NOT NULL,
    name          text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

-- Location is a FIRST-CLASS dimension: it is both a reporting axis and an
-- access-control scope, per the client's handwritten note (AMB-07 resolved in
-- favour of a distinct dimension rather than a synonym for plant or zone).
CREATE TABLE location (
    location_id   text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    plant_id      text REFERENCES plant (plant_id),
    code          text NOT NULL,
    name          text NOT NULL,
    address_line  text,
    city          text,
    state_code    text,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE TABLE department (
    department_id text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    code          text NOT NULL,
    name          text NOT NULL,
    cost_centre   text,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE INDEX ix_division_entity   ON division (entity_id);
CREATE INDEX ix_branch_entity     ON branch (entity_id);
CREATE INDEX ix_zone_entity       ON zone (entity_id);
CREATE INDEX ix_plant_entity      ON plant (entity_id);
CREATE INDEX ix_location_entity   ON location (entity_id);
CREATE INDEX ix_location_plant    ON location (plant_id);
CREATE INDEX ix_department_entity ON department (entity_id);

-- ---------------------------------------------------------------- identity
CREATE TABLE app_user (
    user_id         text PRIMARY KEY,
    email           text NOT NULL,
    display_name    text NOT NULL,
    -- USER is a human; SERVICE is a background principal. Service accounts are
    -- real rows with real scope, never an implicit "no user" bypass.
    principal_kind  text NOT NULL DEFAULT 'USER'
                    CHECK (principal_kind IN ('USER', 'SERVICE')),
    -- Break-glass local credential only. The production identity provider is
    -- external (OIDC); this exists so a deployment is never locked out.
    password_hash   text,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL,
    version_no      integer NOT NULL DEFAULT 1,
    UNIQUE (email)
);

CREATE INDEX ix_app_user_active ON app_user (is_active) WHERE is_active;

-- ---------------------------------------------------------------- calendar
CREATE TABLE accounting_period (
    period_id     text PRIMARY KEY,
    entity_id     text NOT NULL REFERENCES entity (entity_id),
    period_start  date NOT NULL,
    period_end    date NOT NULL,
    state         text NOT NULL DEFAULT 'FUTURE'
                  CHECK (state IN ('FUTURE', 'OPEN', 'SOFT_CLOSED', 'CLOSED')),
    closed_at     timestamptz,
    closed_by     text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    CHECK (period_end >= period_start),
    -- Two periods for one entity can never overlap. A gist exclusion states
    -- that as a constraint rather than hoping application code checks it.
    EXCLUDE USING gist (
        entity_id WITH =,
        daterange(period_start, period_end, '[]') WITH &&
    )
);

CREATE INDEX ix_period_entity_state ON accounting_period (entity_id, state);

-- ---------------------------------------------------------------- audit
-- Append-only, hash-chained, per stream.
--
-- The payload format prev|at|actor|action|type|id|detail is FROZEN. Changing it
-- invalidates every hash already stored, including the imported legacy chain.
--
-- audit_id is NOT the chain order: identity values are assigned before commit
-- and can commit out of order. `seq`, taken under an advisory lock, is the
-- order. Getting this wrong produces a chain that verifies locally and is
-- meaningless globally.
CREATE TABLE audit_log (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stream_key   text NOT NULL,
    seq          bigint NOT NULL,
    at           timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,
    action       text NOT NULL,
    object_type  text NOT NULL,
    object_id    text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    correlation_id text,
    prev_hash    text,
    entry_hash   text,
    UNIQUE (stream_key, seq)
);

CREATE INDEX ix_audit_object ON audit_log (object_type, object_id);
CREATE INDEX ix_audit_at     ON audit_log (at DESC);
CREATE INDEX ix_audit_actor  ON audit_log (actor, at DESC);
CREATE INDEX ix_audit_corr   ON audit_log (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- Daily anchors restore the global property that per-stream chains give up.
-- A two-level Merkle structure: exportable off-platform, so tampering is
-- detectable even by someone holding full database control.
CREATE TABLE audit_anchor (
    anchor_date       date PRIMARY KEY,
    stream_heads      jsonb NOT NULL,
    prev_anchor_hash  text,
    anchor_hash       text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Belt and braces. The real guarantee is a privilege one -- the application
-- role holds INSERT and SELECT only, and audit_log is owned by a separate
-- role -- but a trigger states the intent in the schema and catches a
-- misconfigured grant.
CREATE OR REPLACE FUNCTION assert_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % denied', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();
CREATE TRIGGER audit_anchor_no_update BEFORE UPDATE ON audit_anchor
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();
CREATE TRIGGER audit_anchor_no_delete BEFORE DELETE ON audit_anchor
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();
