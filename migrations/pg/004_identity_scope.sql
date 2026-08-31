-- 004_identity_scope.sql
-- Wave 2 / M4a: identity, roles, scope, and row-level security.
--
-- See `.claude/skills/wbs-full-app-builder/references/domain-controls.md`
-- and `app/backend/pg/repo.py` / `app/backend/pg/engine.py` (both read-only,
-- lead-owned) for the model this migration backs: a `Scope` restricts each
-- of four dimensions (entity, plant, project, location) to either
-- unrestricted (`NULL` / the literal session setting `'*'`) or a specific
-- set of ids, possibly EMPTY -- which means "nothing", never "everything".
-- `repo.compile_scope` is the PRIMARY enforcement of that rule, over SQL
-- `repo.query()` builds. RLS below is the BACKSTOP: it re-enforces the same
-- rule, independently, at the database level, so a query that forgets to go
-- through `repo.query()` still cannot over-read. `app/backend/pg/rls.py`
-- documents why RLS needs a role that is not the connecting superuser, and
-- carries a pure-Python mirror of the predicate below for tests to check
-- both implementations against.
--
-- Expand-only: nothing here alters or drops anything 001_foundation.sql or
-- 002_budget_control.sql created; it adds new tables, two SQL functions, one
-- new database role, and RLS policies on existing tables.
--
-- ROLLBACK:
--   -- Policies (each DROP is a no-op if the table was never RLS-enabled):
--   DROP POLICY IF EXISTS entity_scope ON entity;
--   DROP POLICY IF EXISTS division_scope ON division;
--   DROP POLICY IF EXISTS branch_scope ON branch;
--   DROP POLICY IF EXISTS zone_scope ON zone;
--   DROP POLICY IF EXISTS department_scope ON department;
--   DROP POLICY IF EXISTS plant_scope ON plant;
--   DROP POLICY IF EXISTS location_scope ON location;
--   DROP POLICY IF EXISTS project_scope ON project;
--   DROP POLICY IF EXISTS wbs_element_scope ON wbs_element;
--   DROP POLICY IF EXISTS budget_control_cell_scope ON budget_control_cell;
--   DROP POLICY IF EXISTS budget_ledger_cell_scope ON budget_ledger_cell;
--   ALTER TABLE entity NO FORCE ROW LEVEL SECURITY;   ALTER TABLE entity DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE division NO FORCE ROW LEVEL SECURITY; ALTER TABLE division DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE branch NO FORCE ROW LEVEL SECURITY;   ALTER TABLE branch DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE zone NO FORCE ROW LEVEL SECURITY;     ALTER TABLE zone DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE department NO FORCE ROW LEVEL SECURITY; ALTER TABLE department DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE plant NO FORCE ROW LEVEL SECURITY;    ALTER TABLE plant DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE location NO FORCE ROW LEVEL SECURITY; ALTER TABLE location DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE project NO FORCE ROW LEVEL SECURITY;  ALTER TABLE project DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE wbs_element NO FORCE ROW LEVEL SECURITY; ALTER TABLE wbs_element DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_control_cell NO FORCE ROW LEVEL SECURITY; ALTER TABLE budget_control_cell DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_ledger_cell NO FORCE ROW LEVEL SECURITY;  ALTER TABLE budget_ledger_cell DISABLE ROW LEVEL SECURITY;
--   DROP FUNCTION IF EXISTS capex_scope_permits(text, text, text, text);
--   DROP FUNCTION IF EXISTS capex_dimension_permits(text, text);
--   DROP TABLE IF EXISTS user_scope_grant, user_scope_restriction,
--     user_access_flag, role_grant CASCADE;
--   -- capex_app is left in place: dropping a role requires first revoking
--   -- every privilege and default-privilege entry naming it, across every
--   -- database in the cluster (roles are cluster-wide), which this
--   -- migration's own transaction cannot see or undo safely. An operator
--   -- rolling this back should REVOKE the grants issued below, then
--   -- `DROP ROLE capex_app` by hand once satisfied nothing else depends on
--   -- it.

-- ============================================================== capex_app
-- The production application role (app/backend/pg/config.py's
-- DatabaseConfig default `user="capex_app"`). No earlier migration creates
-- it. RLS is meaningless without a role that is neither a superuser nor a
-- table owner to test it against: the CI service container's admin role
-- (POSTGRES_USER=capex, .github/workflows/ci.yml) is created as a Postgres
-- superuser by the official `postgres` image, and superusers bypass
-- row-level security unconditionally -- FORCE ROW LEVEL SECURITY changes
-- that only for the table's OWNER, and every table here is owned by
-- whichever role runs migrations (the superuser in CI), not by capex_app.
--
-- NOLOGIN, deliberately. This file ships in git; a login password belongs
-- in a secret store (config.py's SecretProvider contract), never in a
-- migration a reviewer reads. `app/backend/pg/rls.py::assume_scoped_role`
-- reaches this role's restricted context with `SET LOCAL ROLE capex_app`
-- from an already-authenticated connection -- superuser in tests, whatever
-- deploy identity runs migrations in production -- which needs no password.
-- A real deployment issues `ALTER ROLE capex_app LOGIN PASSWORD '...'`
-- out of band, from its own secret manager, as a one-time step this
-- migration does not perform and this repository does not need to know
-- about.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'capex_app') THEN
        CREATE ROLE capex_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO capex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO capex_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO capex_app;
-- audit_log / audit_anchor are append-only by product rule
-- (domain-controls.md, "Audit chain": "App role holds INSERT, SELECT only.
-- No UPDATE, no DELETE."). The blanket grant above is narrowed back down
-- immediately, in the same migration, rather than trusting every future
-- caller to remember -- the trigger in 001_foundation.sql is belt, this is
-- braces.
REVOKE UPDATE, DELETE ON audit_log, audit_anchor FROM capex_app;
-- Runs as whatever role executes migrate_pg.upgrade() (the deploy identity,
-- or a test/CI superuser). ALTER DEFAULT PRIVILEGES with no `FOR ROLE`
-- clause defaults to "objects THIS role creates from now on" -- exactly the
-- role that will run 005_master_data.sql next, so capex_app is not left
-- without access to tables a later migration introduces.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO capex_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO capex_app;

-- ============================================================== role_grant
-- User -> business role. Many-to-many: a user may hold more than one of the
-- 13 roles frozen in research/30_contracts/C9_roles.json ("Every screen's
-- 'Permission requirements' attribute must name roles from this list
-- only."). app.backend.pg.roles.ROLES is hand-kept in sync with the CHECK
-- list below; tests/test_pg_roles.py asserts both match the JSON.
CREATE TABLE role_grant (
    user_id     text NOT NULL REFERENCES app_user (user_id),
    role        text NOT NULL CHECK (role IN (
        'Requestor', 'Project Manager', 'Plant Head', 'Department Head',
        'Procurement', 'Finance', 'Project Finance Controller',
        'CAPEX Committee', 'CFO', 'Management Approver',
        'System Administrator', 'Internal Auditor',
        'Read-only Management User'
    )),
    granted_at  timestamptz NOT NULL DEFAULT now(),
    granted_by  text NOT NULL,
    PRIMARY KEY (user_id, role)
);

CREATE INDEX ix_role_grant_role ON role_grant (role);

-- ========================================================= user_access_flag
-- `read_all` -- the whole-estate override mapping directly onto
-- `Scope.read_all` (engine.py). Deliberately independent of role
-- membership: docs/WAVE2_CONTRACTS.md's grants contract sets it explicitly
-- as part of a user's scope, not derived from role, so an Administrator and
-- a Read-only Management User can each be read_all or not on their own
-- terms.
CREATE TABLE user_access_flag (
    user_id     text PRIMARY KEY REFERENCES app_user (user_id),
    read_all    boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    version_no  integer NOT NULL DEFAULT 1
);

-- =================================================== user_scope_restriction
-- Whether a dimension is restricted for a user AT ALL. This table's sole
-- purpose is to make "restricted to zero ids" observably different from
-- "not restricted" -- a design that inferred restriction purely from
-- "does user_scope_grant have any rows for this dimension" cannot express
-- the empty-set case: zero rows would always read as unrestricted, which is
-- exactly the None-vs-empty-frozenset inversion `repo.compile_scope`
-- (app/backend/pg/repo.py) exists to prevent, reintroduced one layer up.
-- A row here for `(user_id, dimension)` means restricted (see
-- user_scope_grant for the permitted values, if any); no row means
-- unrestricted, i.e. `Scope.<dimension>_ids = None`.
CREATE TABLE user_scope_restriction (
    user_id     text NOT NULL REFERENCES app_user (user_id),
    dimension   text NOT NULL CHECK (dimension IN ('entity', 'plant', 'project', 'location')),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    PRIMARY KEY (user_id, dimension)
);

-- ========================================================== user_scope_grant
-- The permitted ids for a RESTRICTED `(user_id, dimension)` pair -- see
-- user_scope_restriction above. A dimension with a restriction row but zero
-- rows here is the empty-set case: restricted to nothing. `scope_value` is
-- deliberately untyped (no FK to entity/plant/project/location): one table
-- serving four different id spaces cannot carry four different FK targets
-- at once, so app.backend.pg.roles validates values against the live
-- dimension tables at grant time instead (API-layer validation, not a
-- database constraint, for this one column).
CREATE TABLE user_scope_grant (
    user_id     text NOT NULL,
    dimension   text NOT NULL,
    scope_value text NOT NULL,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    granted_by  text NOT NULL,
    PRIMARY KEY (user_id, dimension, scope_value),
    FOREIGN KEY (user_id, dimension)
        REFERENCES user_scope_restriction (user_id, dimension) ON DELETE CASCADE
);

CREATE INDEX ix_user_scope_grant_value ON user_scope_grant (dimension, scope_value);

-- ================================================== scope predicate (SQL)
-- The RLS-side counterpart to `repo.compile_scope`. Two independent
-- implementations of one rule, in two languages, on purpose -- see this
-- file's header and `app/backend/pg/rls.py`, which carries a THIRD, pure
-- Python mirror (`rls.permits`) tests use as the oracle to check both
-- against.
--
-- Session settings are rendered by `Scope.as_settings()` (engine.py):
--   '*'            -> that dimension is None on the Scope -- unrestricted
--   ''  (empty)    -> that dimension is an EMPTY frozenset -- nothing
--   'a,b,c'        -> that dimension is restricted to exactly {a, b, c}
-- A NULL/missing session setting (a connection that never went through
-- `Database.session()`, e.g. capex_app used directly without
-- `rls.assume_scoped_role`) is treated the same as '' -- FAIL CLOSED. A
-- backstop that fails OPEN on a forgotten `SET LOCAL` protects nothing.
CREATE OR REPLACE FUNCTION capex_dimension_permits(p_setting_key text, p_value text)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT
        CASE
            -- The row carries no value for this dimension at all -- the
            -- table/query shape waives it (mirrors `repo.compile_scope`'s
            -- `columns={"dim": None}` waiver). Only ever reached with a
            -- literal SQL NULL passed by a policy below, never by an absent
            -- session setting.
            WHEN p_value IS NULL THEN true
            WHEN COALESCE(current_setting(p_setting_key, true), '') = '*' THEN true
            ELSE p_value = ANY(string_to_array(
                     COALESCE(current_setting(p_setting_key, true), ''), ','))
        END;
$$;

CREATE OR REPLACE FUNCTION capex_scope_permits(
    p_entity_id   text,
    p_plant_id    text,
    p_location_id text,
    p_project_id  text
) RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    -- read_all short-circuits everything, exactly as `scope.read_all` does
    -- at the top of `repo.compile_scope` -- checked FIRST, before any
    -- per-dimension predicate, in both implementations.
    SELECT
        COALESCE(current_setting('capex.read_all', true), 'false') = 'true'
        OR (
            capex_dimension_permits('capex.entity_ids',   p_entity_id)
            AND capex_dimension_permits('capex.plant_ids',   p_plant_id)
            AND capex_dimension_permits('capex.location_ids', p_location_id)
            AND capex_dimension_permits('capex.project_ids',  p_project_id)
        );
$$;

GRANT EXECUTE ON FUNCTION capex_dimension_permits(text, text) TO PUBLIC;
GRANT EXECUTE ON FUNCTION capex_scope_permits(text, text, text, text) TO PUBLIC;

-- ================================================================ RLS: org
-- Column mappings below match `app/backend/pg/rls.py::RLS_TABLE_COLUMNS`
-- exactly -- that registry exists so a test can enumerate them without
-- reading this file, and so the two never silently drift.
--
-- `organisation` deliberately carries NO policy: it has no entity/plant/
-- location/project column of its own to filter by (it is the root of the
-- hierarchy, not a leaf of it), and `repo.compile_scope`'s own rule is that
-- a dimension a table's query shape cannot express is not filtered, never
-- filtered-as-if-restricted. The same applies here: there is no predicate
-- that would not simply be `TRUE` for every row.
ALTER TABLE entity ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity FORCE ROW LEVEL SECURITY;
CREATE POLICY entity_scope ON entity
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE division ENABLE ROW LEVEL SECURITY;
ALTER TABLE division FORCE ROW LEVEL SECURITY;
CREATE POLICY division_scope ON division
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE branch ENABLE ROW LEVEL SECURITY;
ALTER TABLE branch FORCE ROW LEVEL SECURITY;
CREATE POLICY branch_scope ON branch
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE zone ENABLE ROW LEVEL SECURITY;
ALTER TABLE zone FORCE ROW LEVEL SECURITY;
CREATE POLICY zone_scope ON zone
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE department ENABLE ROW LEVEL SECURITY;
ALTER TABLE department FORCE ROW LEVEL SECURITY;
CREATE POLICY department_scope ON department
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

ALTER TABLE plant ENABLE ROW LEVEL SECURITY;
ALTER TABLE plant FORCE ROW LEVEL SECURITY;
CREATE POLICY plant_scope ON plant
    USING      (capex_scope_permits(entity_id, plant_id, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, plant_id, NULL, NULL));

ALTER TABLE location ENABLE ROW LEVEL SECURITY;
ALTER TABLE location FORCE ROW LEVEL SECURITY;
CREATE POLICY location_scope ON location
    USING      (capex_scope_permits(entity_id, plant_id, location_id, NULL))
    WITH CHECK (capex_scope_permits(entity_id, plant_id, location_id, NULL));

-- ============================================================ RLS: project
-- Matches `repo.PROJECT_SCOPE_COLUMNS` exactly (entity, plant, location,
-- project all mapped) -- `tests/test_pg_rls.py` proves this table's RLS
-- result set equals `repo.compile_scope`'s for the same scope, live.
ALTER TABLE project ENABLE ROW LEVEL SECURITY;
ALTER TABLE project FORCE ROW LEVEL SECURITY;
CREATE POLICY project_scope ON project
    USING      (capex_scope_permits(entity_id, plant_id, location_id, project_id))
    WITH CHECK (capex_scope_permits(entity_id, plant_id, location_id, project_id));

-- ========================================================= RLS: wbs_element
-- Matches `repo.WBS_ELEMENT_SCOPE_COLUMNS` exactly: entity/plant/location
-- are waived here (NULL) because `wbs_element` carries no columns for them
-- -- they are reachable only through `project` -- and `repo.py`'s own
-- comment states that waiver is deliberate, not an oversight, for any
-- caller using that mapping. Filtering `wbs_element` by `project_id` alone
-- is not weaker than filtering by all four: a project a caller cannot see
-- at all is already excluded at the `project` table itself, so every
-- `wbs_element` reachable via a visible project is by construction
-- reachable via an entity/plant/location the caller could see too.
ALTER TABLE wbs_element ENABLE ROW LEVEL SECURITY;
ALTER TABLE wbs_element FORCE ROW LEVEL SECURITY;
CREATE POLICY wbs_element_scope ON wbs_element
    USING      (capex_scope_permits(NULL, NULL, NULL, project_id))
    WITH CHECK (capex_scope_permits(NULL, NULL, NULL, project_id));

-- ============================================ RLS: budget control/ledger cells
-- Neither table carries a `project_id` column (the control cell's key is
-- `(wbs_id, budget_head_id)`); reached one join further out than
-- `wbs_element`, through it, to the same project-only waiver
-- `wbs_element_scope` uses. `wbs_element` itself carries RLS too, so this
-- join is additionally filtered by `wbs_element_scope` when evaluated as
-- capex_app -- redundant with the explicit `capex_scope_permits` call here,
-- not in conflict with it: both express the same rule.
ALTER TABLE budget_control_cell ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_control_cell FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_control_cell_scope ON budget_control_cell
    USING (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_control_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_control_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );

ALTER TABLE budget_ledger_cell ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_ledger_cell FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_ledger_cell_scope ON budget_ledger_cell
    USING (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_ledger_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_ledger_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );
