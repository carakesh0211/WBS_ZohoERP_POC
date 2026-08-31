-- 005_master_data.sql
-- Milestone 2 / Wave 2 stream 4: settings & master data backend.
--
-- Item and vendor masters (mirrorable from Zoho, governed locally), the
-- REQ-SEC-007 custom-field engine, and a collision-safe numbering series.
-- Organisation-through-department (organisation, entity, division, branch,
-- zone, plant, location, department) already exist from 001_foundation.sql
-- and are NOT touched here -- this migration only adds new tables.
--
-- Conventions carried forward from 001_foundation.sql:
--   * money is bigint PAISE -- not used in this migration, no money here
--   * timestamps are timestamptz
--   * business keys are separate UNIQUE constraints, not primary keys
--   * every mirrored table carries source / external_source / external_id /
--     external_last_modified / payload_sha, per docs/WAVE2_CONTRACTS.md
--   * constraints are declarative wherever a constraint can express the rule
--
-- India tax identity (gst_no, pan_no on vendor_master) is regulated data.
-- It is NEVER hashed -- hashing destroys the matching and statutory
-- reporting function these fields exist for (see entity.gst_no /
-- entity.pan_no in 001_foundation.sql for the precedent this follows).
-- It is protected instead by masking at the API boundary
-- (app/backend/pg/masters.py::mask_gst_no / mask_pan_no) and by requiring a
-- distinct reveal permission, audited on every use. See
-- .claude/skills/wbs-full-app-builder/references/zoho-boundaries.md for why
-- a Zoho-sourced row is never marked LIVE or VERIFIED in this wave: there is
-- no live Zoho connection, so nothing here can honestly claim to be either.
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS custom_field_value, custom_field_applicability,
--     custom_field_def, vendor_master, item_master, numbering_issued,
--     numbering_counter, numbering_series CASCADE;
--   DROP FUNCTION IF EXISTS capex_normalise_text(text) CASCADE;

-- Shared normalisation for duplicate detection. IMMUTABLE so it can back a
-- GENERATED ALWAYS ... STORED column: the normalised form is then always
-- consistent with `code`/`name`, regardless of which code path wrote them --
-- duplicate detection can never silently drift out of step with an app-layer
-- helper that forgot to call it.
CREATE OR REPLACE FUNCTION capex_normalise_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(regexp_replace(value, '[^a-zA-Z0-9]+', '', 'g'))
$$;

-- ---------------------------------------------------------------- numbering
-- Collision-safe issuance. `numbering_counter` is advanced with a single
-- atomic `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING` (see
-- app/backend/pg/masters.py::issue_number) -- the row lock PostgreSQL takes
-- to resolve the conflict is what makes two concurrent issuers serialise
-- instead of racing. The POC's `SELECT COUNT(*)+1` pattern has no such lock
-- and is exactly the defect this table exists to make structurally
-- impossible: two concurrent counts can read the same value and both
-- proceed, minting the same number twice.
CREATE TABLE numbering_series (
    series_id     text PRIMARY KEY,
    code          text NOT NULL,
    prefix        text NOT NULL DEFAULT '',
    suffix        text NOT NULL DEFAULT '',
    pad_width     smallint NOT NULL DEFAULT 6 CHECK (pad_width BETWEEN 1 AND 12),
    -- Not enforced by triggers in this migration: `period_key` is supplied
    -- by the caller (app/backend/pg/masters.py), and 'NEVER' series always
    -- pass ''. YEARLY/MONTHLY are declared here as the vocabulary a caller
    -- may use; deriving period_key from the clock is application logic.
    reset_policy  text NOT NULL DEFAULT 'NEVER'
                  CHECK (reset_policy IN ('NEVER', 'YEARLY', 'MONTHLY')),
    description   text,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    version_no    integer NOT NULL DEFAULT 1,
    UNIQUE (code)
);

CREATE TABLE numbering_counter (
    series_id   text NOT NULL REFERENCES numbering_series (series_id),
    -- '' for a NEVER-reset series; 'YYYY' or 'YYYY-MM' for YEARLY/MONTHLY.
    period_key  text NOT NULL DEFAULT '',
    last_value  bigint NOT NULL DEFAULT 0 CHECK (last_value >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, period_key)
);

-- Append-only record of every number ever issued. Immutable: a number, once
-- minted, is never renumbered or reused, even if the document it was issued
-- for is later cancelled -- reusing a gap is how two different objects end
-- up sharing a number. Reuses `assert_append_only()` from 001_foundation.sql
-- rather than redefining it; that function is generic (raises on any
-- UPDATE/DELETE regardless of table) and is already the frozen trigger body
-- audit_log itself uses.
CREATE TABLE numbering_issued (
    issued_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_id        text NOT NULL REFERENCES numbering_series (series_id),
    period_key       text NOT NULL,
    value             bigint NOT NULL CHECK (value > 0),
    formatted_number  text NOT NULL,
    -- What the number was issued for, if anything -- nullable because a
    -- number can legitimately be pre-allocated before the object exists.
    object_type       text,
    object_id         text,
    issued_at         timestamptz NOT NULL DEFAULT now(),
    issued_by         text NOT NULL,
    UNIQUE (series_id, period_key, value),
    UNIQUE (formatted_number)
);

CREATE INDEX ix_numbering_issued_object
    ON numbering_issued (object_type, object_id) WHERE object_type IS NOT NULL;

CREATE TRIGGER numbering_issued_no_update BEFORE UPDATE ON numbering_issued
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();
CREATE TRIGGER numbering_issued_no_delete BEFORE DELETE ON numbering_issued
    FOR EACH ROW EXECUTE FUNCTION assert_append_only();

-- ------------------------------------------------------------------ items
CREATE TABLE item_master (
    item_id                  text PRIMARY KEY,
    code                     text NOT NULL,
    name                     text NOT NULL,
    -- Duplicate detection surfaces through these, never blocks on them --
    -- see app/backend/pg/masters.py::find_duplicate. Two rows may share a
    -- normalised code or name; only the literal `code` is a hard UNIQUE.
    normalised_code           text GENERATED ALWAYS AS (capex_normalise_text(code)) STORED,
    normalised_name           text GENERATED ALWAYS AS (capex_normalise_text(name)) STORED,
    uom                       text,
    category                  text,
    hsn_code                  text,
    description               text,
    source                    text NOT NULL DEFAULT 'LOCAL'
                              CHECK (source IN ('LOCAL', 'IMPORT', 'ZOHO')),
    external_source           text,
    external_id               text,
    external_last_modified    timestamptz,
    payload_sha                text,
    -- Never LIVE or VERIFIED for a ZOHO row in this wave -- there is no live
    -- Zoho connection to have verified anything against. Enforced in
    -- app/backend/pg/masters.py, not by this CHECK: the column has to allow
    -- LIVE/VERIFIED as a VALUE (a later milestone with a real connection
    -- will use them), it is the ingestion path that must never assign them.
    source_of_truth_status     text NOT NULL DEFAULT 'LOCAL'
                              CHECK (source_of_truth_status IN
                                     ('LOCAL', 'MOCK', 'UNVERIFIED', 'LIVE', 'VERIFIED')),
    duplicate_of                text REFERENCES item_master (item_id),
    mapping_status               text NOT NULL DEFAULT 'UNMAPPED'
                              CHECK (mapping_status IN
                                     ('UNMAPPED', 'MAPPED', 'NEEDS_REVIEW', 'DUPLICATE_SUSPECT')),
    is_active                    boolean NOT NULL DEFAULT true,
    created_at                    timestamptz NOT NULL DEFAULT now(),
    created_by                    text NOT NULL,
    updated_at                    timestamptz NOT NULL DEFAULT now(),
    updated_by                    text NOT NULL,
    version_no                    integer NOT NULL DEFAULT 1,
    UNIQUE (code),
    CHECK (duplicate_of IS NULL OR duplicate_of <> item_id)
);

-- Partial: only enforced where an external identity actually exists. A LOCAL
-- row's external_source/external_id are both NULL, and NULL <> NULL in a
-- unique index, so any number of LOCAL rows may coexist.
CREATE UNIQUE INDEX ux_item_master_external
    ON item_master (external_source, external_id)
    WHERE external_source IS NOT NULL AND external_id IS NOT NULL;

CREATE INDEX ix_item_master_norm_code ON item_master (normalised_code);
CREATE INDEX ix_item_master_norm_name ON item_master (normalised_name);
CREATE INDEX ix_item_master_source    ON item_master (source);
CREATE INDEX ix_item_master_active    ON item_master (is_active) WHERE is_active;
CREATE INDEX ix_item_master_dup       ON item_master (duplicate_of) WHERE duplicate_of IS NOT NULL;

-- ---------------------------------------------------------------- vendors
CREATE TABLE vendor_master (
    vendor_id                text PRIMARY KEY,
    code                     text NOT NULL,
    name                     text NOT NULL,
    normalised_code           text GENERATED ALWAYS AS (capex_normalise_text(code)) STORED,
    normalised_name           text GENERATED ALWAYS AS (capex_normalise_text(name)) STORED,
    -- India tax identity. NEVER hashed -- see the migration-header note and
    -- app/backend/pg/masters.py::mask_gst_no / mask_pan_no. Format-checked
    -- loosely (length/shape, not the full checksum algorithm) so demo data
    -- stays realistic without this migration re-implementing GSTIN/PAN
    -- validation.
    gst_no                    text CHECK (gst_no IS NULL OR gst_no ~ '^[0-9]{2}[A-Z0-9]{13}$'),
    gst_treatment              text CHECK (gst_treatment IS NULL OR gst_treatment IN
                              ('registered_business', 'composition', 'unregistered_business',
                               'consumer', 'overseas', 'sez', 'deemed_export')),
    place_of_contact            text,
    pan_no                       text CHECK (pan_no IS NULL OR pan_no ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'),
    description                  text,
    source                       text NOT NULL DEFAULT 'LOCAL'
                              CHECK (source IN ('LOCAL', 'IMPORT', 'ZOHO')),
    external_source               text,
    external_id                   text,
    external_last_modified         timestamptz,
    payload_sha                     text,
    source_of_truth_status          text NOT NULL DEFAULT 'LOCAL'
                              CHECK (source_of_truth_status IN
                                     ('LOCAL', 'MOCK', 'UNVERIFIED', 'LIVE', 'VERIFIED')),
    duplicate_of                     text REFERENCES vendor_master (vendor_id),
    mapping_status                    text NOT NULL DEFAULT 'UNMAPPED'
                              CHECK (mapping_status IN
                                     ('UNMAPPED', 'MAPPED', 'NEEDS_REVIEW', 'DUPLICATE_SUSPECT')),
    is_active                         boolean NOT NULL DEFAULT true,
    created_at                         timestamptz NOT NULL DEFAULT now(),
    created_by                         text NOT NULL,
    updated_at                         timestamptz NOT NULL DEFAULT now(),
    updated_by                         text NOT NULL,
    version_no                         integer NOT NULL DEFAULT 1,
    UNIQUE (code),
    CHECK (duplicate_of IS NULL OR duplicate_of <> vendor_id)
);

CREATE UNIQUE INDEX ux_vendor_master_external
    ON vendor_master (external_source, external_id)
    WHERE external_source IS NOT NULL AND external_id IS NOT NULL;

CREATE INDEX ix_vendor_master_norm_code ON vendor_master (normalised_code);
CREATE INDEX ix_vendor_master_norm_name ON vendor_master (normalised_name);
CREATE INDEX ix_vendor_master_source    ON vendor_master (source);
CREATE INDEX ix_vendor_master_active    ON vendor_master (is_active) WHERE is_active;
CREATE INDEX ix_vendor_master_dup       ON vendor_master (duplicate_of) WHERE duplicate_of IS NOT NULL;

-- --------------------------------------------------------- custom fields
-- REQ-SEC-007. A field definition, the object types it applies to, and the
-- values recorded against individual objects -- kept as three tables rather
-- than one wide one so a field can be scoped to ITEM, VENDOR, or both
-- without a schema change, and so removing an applicability never touches
-- values already recorded under it.
CREATE TABLE custom_field_def (
    field_def_id  text PRIMARY KEY,
    code          text NOT NULL,
    label         text NOT NULL,
    data_type     text NOT NULL
                  CHECK (data_type IN ('TEXT', 'NUMBER', 'DATE', 'BOOLEAN', 'SELECT')),
    -- Only meaningful for data_type = 'SELECT'; a JSON array of option
    -- strings. Left NULL for every other data_type.
    select_options jsonb,
    is_required    boolean NOT NULL DEFAULT false,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,
    UNIQUE (code)
);

CREATE TABLE custom_field_applicability (
    applicability_id  text PRIMARY KEY,
    field_def_id       text NOT NULL REFERENCES custom_field_def (field_def_id),
    applies_to          text NOT NULL CHECK (applies_to IN ('ITEM', 'VENDOR')),
    is_active            boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL,
    UNIQUE (field_def_id, applies_to)
);

CREATE TABLE custom_field_value (
    value_id      text PRIMARY KEY,
    field_def_id   text NOT NULL REFERENCES custom_field_def (field_def_id),
    object_type     text NOT NULL CHECK (object_type IN ('ITEM', 'VENDOR')),
    object_id        text NOT NULL,
    -- A single jsonb slot rather than one typed column per data_type: the
    -- value's shape is governed by custom_field_def.data_type at the
    -- application layer (app/backend/pg/masters.py), and jsonb lets one
    -- column hold a string, a number, a boolean or a date without a CHECK
    -- per data_type duplicating that governance in SQL.
    value             jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text NOT NULL,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text NOT NULL,
    version_no         integer NOT NULL DEFAULT 1,
    UNIQUE (field_def_id, object_type, object_id)
);

CREATE INDEX ix_custom_field_applicability_field ON custom_field_applicability (field_def_id);
CREATE INDEX ix_custom_field_value_object ON custom_field_value (object_type, object_id);
