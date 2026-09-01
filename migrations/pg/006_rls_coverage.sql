-- 006_rls_coverage.sql
-- Wave 3, stream 2 (PostgreSQL RLS): close the RLS coverage gap.
--
-- `migrations/pg/004_identity_scope.sql` enabled row-level security on
-- eleven tables. Eight more tables either carry a scope dimension directly
-- or reach one through a join, and had NONE -- so a query that bypassed
-- `repo.query()` (the primary control) read them unfiltered, with no
-- backstop underneath. docs/WAVE3_CONTRACTS.md's security-closure gate
-- names exactly these eight:
--
--     accounting_period   budget_line       budget_revision   budget_transfer
--     budget_version      budget_version_cell  item_master     vendor_master
--
-- This migration adds `ENABLE` + `FORCE ROW LEVEL SECURITY` and one policy
-- to each. FORCE matters as much as ENABLE: without it the table's OWNER --
-- in production, the deploy identity that ran 001..005, which is NOT a
-- superuser -- bypasses every policy silently. `ENABLE` alone would leave
-- a policy that looks present in `pg_policies` and protects nothing from
-- the one role most likely to be reused by a background job.
--
-- Expand-only. Nothing here alters, drops or redefines anything 001..005
-- created. In particular it does NOT redefine `capex_scope_permits` or
-- `capex_dimension_permits`: their signatures are frozen by
-- docs/WAVE3_CONTRACTS.md Contract 1, migration 007 (stream 3) replaces
-- their BODIES to implement the mode/ids wire format, and every policy
-- below calls them unchanged so it picks that replacement up automatically.
-- Two migrations editing one function body is the collision this ordering
-- exists to avoid.
--
-- ROLLBACK:
--   DROP POLICY IF EXISTS accounting_period_scope ON accounting_period;
--   DROP POLICY IF EXISTS budget_line_scope ON budget_line;
--   DROP POLICY IF EXISTS budget_revision_scope ON budget_revision;
--   DROP POLICY IF EXISTS budget_transfer_scope ON budget_transfer;
--   DROP POLICY IF EXISTS budget_version_scope ON budget_version;
--   DROP POLICY IF EXISTS budget_version_cell_scope ON budget_version_cell;
--   DROP POLICY IF EXISTS item_master_reference_read ON item_master;
--   DROP POLICY IF EXISTS vendor_master_reference_read ON vendor_master;
--   ALTER TABLE accounting_period NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE accounting_period DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_line NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE budget_line DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_revision NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE budget_revision DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_transfer NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE budget_transfer DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_version NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE budget_version DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE budget_version_cell NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE budget_version_cell DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE item_master NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE item_master DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE vendor_master NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE vendor_master DISABLE ROW LEVEL SECURITY;
--   DROP FUNCTION IF EXISTS capex_principal_present();

-- ============================================== reference-data predicate
-- `item_master` and `vendor_master` are ORGANISATION-WIDE REFERENCE DATA.
--
-- The decision, and why. Neither table carries an entity, plant, location
-- or project column, and neither reaches one by any join: an item or a
-- vendor is not owned by an entity in this schema. Nothing in
-- 005_master_data.sql, `app/backend/pg/masters.py` or the duplicate-
-- detection model (`normalised_code` / `normalised_name` are UNIQUE-ish
-- across the WHOLE table, not per entity) partitions them. Inventing an
-- entity column here to make them scopeable would be a schema change this
-- migration is not entitled to make, and guessing an owner for existing
-- rows would be worse: a wrong guess silently HIDES a vendor from the
-- entity that actually uses it, and duplicate detection that cannot see
-- the whole table stops detecting duplicates.
--
-- Organisation-wide is therefore a statement about the data, not an excuse
-- to leave the tables unprotected. Both still get RLS, with a predicate
-- that draws the only line the row shape can support: a session with an
-- established principal may read them; a session with NO scope applied at
-- all may not. That second half is the point. A connection that never went
-- through `Database.session()` -- `capex_app` used directly, a pooled
-- connection whose `SET LOCAL` was rolled back, a background job that
-- forgot to open a scoped session -- carries no `capex.user_id`, and reads
-- nothing. `pg_policies` therefore shows a real predicate for these tables
-- rather than the absence of one, and "unscoped connection sees nothing"
-- holds uniformly across all nineteen RLS-protected tables instead of
-- having two exceptions.
--
-- Fail-closed, like `capex_dimension_permits`: an absent setting is not a
-- missing restriction, it is an unauthenticated session.
--
-- A NEW function, not a redefinition. `capex_scope_permits(NULL, NULL,
-- NULL, NULL)` would be the obvious reuse and is exactly wrong here: every
-- argument NULL means every dimension waived, which returns TRUE for a
-- session carrying no settings whatsoever -- fail OPEN, the one thing the
-- backstop must never do.
CREATE OR REPLACE FUNCTION capex_principal_present()
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT
        COALESCE(current_setting('capex.read_all', true), 'false') = 'true'
        OR COALESCE(current_setting('capex.user_id', true), '') <> '';
$$;

GRANT EXECUTE ON FUNCTION capex_principal_present() TO PUBLIC;

-- ==================================================== accounting_period
-- Carries `entity_id NOT NULL` directly (001_foundation.sql). Filtered on
-- entity alone: plant, location and project are waived because the table
-- has no column for any of them, which is the same waiver `entity_scope`
-- and `division_scope` use in 004 and the same rule `repo.compile_scope`
-- applies to a dimension a query shape cannot express.
--
-- This is the one newly covered table where the gap was directly
-- exploitable without any join: a period close is an entity-level control,
-- and an unfiltered read of `accounting_period` told a caller restricted to
-- one entity the open/closed state of every other entity's books.
ALTER TABLE accounting_period ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounting_period FORCE ROW LEVEL SECURITY;
CREATE POLICY accounting_period_scope ON accounting_period
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

-- ========================================================== budget_line
-- Keyed on `(wbs_id, budget_head_id)`; no dimension column of its own.
-- Reaches `project_id` through `wbs_element`, exactly as
-- `budget_control_cell_scope` / `budget_ledger_cell_scope` do in 004, and
-- inherits that policy's documented project-only waiver: a project the
-- caller cannot see is already excluded at the `project` table, so every
-- row reachable through a visible project is reachable through an
-- entity/plant/location the caller could see too.
--
-- A `wbs_id` with no matching `wbs_element` row is DENIED rather than
-- permitted -- `EXISTS` is false, so the row is invisible. Fail closed.
ALTER TABLE budget_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_line FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_line_scope ON budget_line
    USING (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_line.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_line.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );

-- ====================================================== budget_revision
-- Same shape as budget_line: the maker-checker document for a single-cell
-- change, keyed on `(wbs_id, budget_head_id)`, reached through
-- `wbs_element` to `project_id`.
ALTER TABLE budget_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_revision FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_revision_scope ON budget_revision
    USING (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_revision.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_revision.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );

-- ====================================================== budget_transfer
-- A transfer names TWO cells, and may cross projects. BOTH legs must be in
-- scope for the document to be visible -- `AND`, deliberately, not `OR`.
--
-- `OR` would be the wider and more "helpful" choice and is a leak: a
-- caller restricted to project A, shown a transfer whose other leg is
-- project B, learns that project B exists, that it holds budget, and how
-- much moved into or out of it. The document is a single object describing
-- a movement between two places; a caller who cannot see one of those
-- places cannot see the movement. A transfer both of whose legs a caller
-- can see is fully visible, which is the case that matters operationally
-- -- transfers within one project.
--
-- Consequence, stated so it is not discovered as a bug later: a
-- cross-project transfer is invisible to anyone scoped to only one side.
-- Presenting such a document to a partially-authorised approver would be
-- worse -- they would be approving a movement half of which they cannot
-- audit.
ALTER TABLE budget_transfer ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_transfer FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_transfer_scope ON budget_transfer
    USING (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_transfer.from_wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_transfer.to_wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_transfer.from_wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_transfer.to_wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );

-- ======================================================= budget_version
-- Carries `project_id NOT NULL REFERENCES project` directly, so no join is
-- needed. Filtered on project alone, matching `wbs_element_scope`'s
-- documented waiver rather than re-deriving entity/plant/location through
-- `project` -- the `project` table's own policy already applies those.
ALTER TABLE budget_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_version FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_version_scope ON budget_version
    USING      (capex_scope_permits(NULL, NULL, NULL, project_id))
    WITH CHECK (capex_scope_permits(NULL, NULL, NULL, project_id));

-- ================================================== budget_version_cell
-- Two independent paths to a project, and BOTH must permit the row:
--
--   * `version_id` -> `budget_version.project_id`   (FK-enforced)
--   * `wbs_id`     -> `wbs_element.project_id`      (NOT FK-enforced --
--     003_budget_planning.sql declares no foreign key on this column)
--
-- In well-formed data the two paths name the same project and the second
-- clause costs nothing. They are both required because neither is
-- sufficient alone: the version path alone would let a snapshot row
-- carrying another project's `wbs_id` ride in on its parent version's
-- visibility, and the wbs path alone rests on a column no constraint
-- guarantees points anywhere real. Each clause is an `EXISTS`, so a row
-- whose `wbs_id` matches no `wbs_element` -- the shape the missing FK
-- permits -- is denied, not admitted.
ALTER TABLE budget_version_cell ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_version_cell FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_version_cell_scope ON budget_version_cell
    USING (
        EXISTS (
            SELECT 1 FROM budget_version bv
            WHERE bv.version_id = budget_version_cell.version_id
              AND capex_scope_permits(NULL, NULL, NULL, bv.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_version_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM budget_version bv
            WHERE bv.version_id = budget_version_cell.version_id
              AND capex_scope_permits(NULL, NULL, NULL, bv.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            WHERE we.wbs_id = budget_version_cell.wbs_id
              AND capex_scope_permits(NULL, NULL, NULL, we.project_id)
        )
    );

-- ============================================ item_master / vendor_master
-- Organisation-wide reference data -- see `capex_principal_present()`
-- above for the decision and its justification. The policy permits every
-- session that carries a resolved principal and denies every session that
-- does not, so neither table is left simply unprotected.
--
-- `vendor_master` additionally holds regulated India tax identity (gst_no,
-- pan_no). RLS is not the control for those columns and this policy does
-- not pretend to be: 005_master_data.sql is explicit that they are
-- protected by masking at the API boundary
-- (`masters.mask_gst_no` / `mask_pan_no`) plus a distinct, audited reveal
-- permission. Column-level protection is a different mechanism from
-- row-level security, and conflating them would leave the reveal path
-- looking covered when it is not.
ALTER TABLE item_master ENABLE ROW LEVEL SECURITY;
ALTER TABLE item_master FORCE ROW LEVEL SECURITY;
CREATE POLICY item_master_reference_read ON item_master
    USING      (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE vendor_master ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendor_master FORCE ROW LEVEL SECURITY;
CREATE POLICY vendor_master_reference_read ON vendor_master
    USING      (capex_principal_present())
    WITH CHECK (capex_principal_present());
