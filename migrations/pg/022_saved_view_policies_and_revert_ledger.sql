-- 022_saved_view_policies_and_revert_ledger.sql
-- Splits 017's single FOR ALL saved-view policy into the four per-command policies its own comment already claims to be, makes owner_user_id immutable, and owns the twelve ledger deletions migrations 001..012 omit.
--
-- Three separate defects land here because all three share one cause: the
-- file that should carry the fix is frozen. The checksum recorded in
-- `schema_migrations` is `sha256` over the WHOLE FILE, comments included:
--
--     body = self.path.read_bytes().replace(b"\r\n", b"\n")
--     return hashlib.sha256(body).hexdigest()
--                                     -- migrate_pg.py:138-142
--
-- so adding one commented line to 017 moves its checksum exactly as far as
-- rewriting its DDL would, and both readers of that checksum treat the
-- difference as fatal: `assert_schema_current` refuses to BOOT with `schema
-- drift` (migrate_pg.py:933-937) and `upgrade()` refuses to proceed with
-- `Never edit an applied migration -- add a new one.` (migrate_pg.py:876-880).
-- Neither has a repair path -- the adoption branch is reached only for a
-- migration with NO ledger row, never for one whose row disagrees. This is the
-- argument 020's and 021's headers set out at length; it is not restated here
-- beyond what makes this file readable on its own.
--
--
-- ==========================================================================
-- PART 1 -- 017'S USING/WITH CHECK ASYMMETRY DOES NOT HOLD FOR DELETE, AND
--           ITS UPDATE LIMB IS DEFEATABLE
-- ==========================================================================
--
-- `017_reporting.sql:174-187` is a SINGLE policy:
--
--     CREATE POLICY report_saved_view_scope ON report_saved_view
--         USING (capex_scope_permits(entity_id, NULL, NULL, NULL)
--                AND (visibility = 'SHARED'
--                     OR owner_user_id = COALESCE(current_setting('capex.user_id', true), '')))
--         WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL)
--                     AND owner_user_id = COALESCE(current_setting('capex.user_id', true), ''));
--
-- with no `FOR` clause, which means `FOR ALL`. Its comment states the
-- intended property exactly:
--
--     "WITH CHECK is deliberately NARROWER than USING: a principal may READ a
--      shared view somebody else authored, and may WRITE only their own.
--      Without this asymmetry any principal in the entity could UPDATE a
--      shared view -- silently editing the filters under everyone who uses
--      it."
--
-- The property is right. One policy cannot express it, for two reasons.
--
-- (a) DELETE NEVER CONSULTS `WITH CHECK`. PostgreSQL evaluates `WITH CHECK`
--     against a row that will EXIST after the statement; a deleted row does
--     not, so DELETE is decided by `USING` alone. `USING` passes on
--     `visibility = 'SHARED'` regardless of who owns the row, and
--     `017:227` grants DELETE to capex_app -- deliberately, and the comment
--     above that GRANT is a considered argument for why a bookmark is not a
--     ledger. So any principal in the entity may DELETE another user's SHARED
--     saved view. The asymmetry the comment describes is real for INSERT and
--     UPDATE and absent for DELETE, and nothing in the file says so.
--
-- (b) UPDATE IS DEFEATED BY REASSIGNING THE OWNER IN THE SAME STATEMENT.
--     `WITH CHECK` is evaluated against the NEW row. Principal B, in the same
--     entity, meets A's SHARED view through `USING`, and issues
--
--         UPDATE report_saved_view
--            SET owner_user_id = 'U-B', definition = <B's filters>
--          WHERE view_id = '<A''s view>';
--
--     The new row's `owner_user_id` is B's own id, so `WITH CHECK` PASSES.
--     `view_id` is unchanged, so every `report_view_default` still points at
--     it and every colleague who opens it now runs B's filters. That is
--     precisely "silently editing the filters under everyone who uses it",
--     performed through the limb written to prevent it. Nothing in 017 makes
--     `owner_user_id` immutable -- there is no trigger on this table at all.
--
-- SEVERITY, HONESTLY. This is the failed BACKSTOP and not the only control:
-- `app/backend/pg/reporting.py:1896` and `:1926` both carry
-- `AND v.owner_user_id = %(actor)s`, so the product's own update and delete
-- paths already refuse. The defect is that the database-level control the
-- file claims to provide is not the one it provides, and the layer written to
-- catch "a developer who forgot to route a query through the service" is
-- exactly the layer that fails here.
--
-- AGGRAVATED BY ITS TEST. `tests/test_reporting_filterset.py:1157` "asserts"
-- the asymmetry by splitting the migration TEXT on the string "WITH CHECK".
-- A single `FOR ALL` policy containing that substring satisfies it, which is
-- why the property has never been checked against PostgreSQL's actual
-- per-command semantics. The behavioural check is
-- `tests/test_pg_rls_wave7_matrix.py`, which runs under `SET LOCAL ROLE
-- capex_app` and fails if any limb below is widened.
--
-- WHAT PART 1 DOES. Drops the one `FOR ALL` policy and recreates it as four
-- per-command policies with the limbs the comment describes, plus a trigger
-- that makes `owner_user_id` structurally immutable:
--
--   SELECT   entity AND (SHARED OR mine)   -- unchanged from 017's USING
--   INSERT                        mine     -- unchanged from 017's WITH CHECK
--   UPDATE   entity AND mine, both limbs   -- NARROWED: 017 admitted any
--                                          -- shared view through USING
--   DELETE   entity AND mine               -- NARROWED: 017 admitted any
--                                          -- shared view, WITH CHECK unread
--
-- Read access is UNCHANGED in both directions -- the SELECT policy is 017's
-- `USING` verbatim, so a shared view stays readable by the whole entity and a
-- private one stays invisible outside its author. Only the write and delete
-- paths narrow, and they narrow to what `reporting.py` already enforces, so
-- no working call becomes a failing one.
--
--
-- ==========================================================================
-- PART 2 -- MIGRATIONS 001..012 ORPHAN THEIR LEDGER ROWS, AND THE
--           CONSEQUENCE IS A SILENT SKIP RATHER THAN AN ERROR
-- ==========================================================================
--
-- 013 through 021 every one end their revert block with
--
--     DELETE FROM schema_migrations WHERE version = '<their own>';
--
-- (015 via 020, 018 and 019 via 021, for the checksum reason above). The
-- twelve below do not. Each has a commented revert block that drops the
-- objects it created, and none clears its row:
--
--     001_foundation.sql              007_scope_sentinel.sql
--     002_budget_control.sql          008_approval_engine.sql
--     003_budget_planning.sql         009_document_approval_states.sql
--     004_identity_scope.sql          010_integration.sql
--     005_master_data.sql             011_reconciliation_exception.sql
--     006_rls_coverage.sql            012_unattributed_triage.sql
--
-- THE CONSEQUENCE IS NOT A DUPLICATE-KEY ERROR, which is the intuition that
-- makes this easy to dismiss. `_status_from` decides `pending` purely on KEY
-- PRESENCE:
--
--     recorded = known.get(migration.version)
--     if recorded is None:
--         pending.append(migration.version)
--                                     -- migrate_pg.py:182-198
--
-- So after an operator reverts 010, the row saying 010 is applied SURVIVES,
-- `upgrade()` never replays it, and `assert_schema_current` reports
-- `is_current: True`. The application boots and serves a database that is
-- missing `zoho_connection`, the integration tables and every RLS policy
-- those tables carry, while claiming to be fully migrated. Reverting 004 is
-- worse in the same shape: `capex_scope_permits`, `capex_app` and eleven
-- tables' policies are gone, the ledger says otherwise, and the product comes
-- up with row-level security silently absent.
--
-- `tests/test_pg_procurement_schema.py:196`'s guard scoped itself to
-- `>= "013"` and documented this as a known, pre-existing gap. It is widened
-- to 001..022 in the same commit as this file, which is what makes the twelve
-- deletions below load-bearing rather than decorative.
--
-- WHY 022 OWNS ALL TWELVE. Reverts run newest-first, so the newest block runs
-- before every older one -- which makes it the only place from which an older
-- ledger row can be cleared during an ordinary reverse walk, with no operator
-- asked to remember a manual step the blocks exist to spare them. This is the
-- position 020 holds relative to 015 and 021 to 018/019, and the guard states
-- the property as "at or above its own number" for exactly this reason.
--
-- THE INTERMEDIATE STATE IS SOUND, and it is the longest of the three so far,
-- so it is worth being explicit. Revert 022 and stop: every object migrations
-- 001..012 created is still there, and no '001'..'012' row records them. The
-- next `upgrade()` re-runs 001, its `CREATE TABLE organisation` raises
-- `DuplicateTable`, and that is precisely the case the adoption branch exists
-- for. `_adoption_problems` derives what it checks from each migration's own
-- text (migrate_pg.py:779-851) -- its tables, functions, triggers, named
-- constraints, added columns, exclusion constraints, indexes, `*_paise`
-- column TYPES and policies, and that RLS is ENABLEd and FORCEd where the
-- migration says so -- before recording it as satisfied. Each of the twelve
-- is then adopted on that evidence in turn. The database ends adopted and
-- current, not guessing. `tests/test_pg_adoption.py::
-- test_adoption_succeeds_when_the_legacy_schema_is_genuinely_complete` is the
-- control that this mechanism works over the whole directory.
--
--
-- ==========================================================================
-- PART 3 -- THREE CLAIMS IN FROZEN FILES THAT ARE NOT TRUE AS WRITTEN
-- ==========================================================================
--
-- None can be corrected in place, for the checksum reason at the top. They are
-- recorded here, and in `app/backend/pg/rls.py` where that file could be
-- edited, so a reader meets the correction rather than re-deriving it.
--
-- (1) 019_closure.sql:305 -- "reach `project` and pass ALL FOUR of its
--     dimension columns" -- and `rls.py`'s closure registry said the same,
--     both without qualification.
--
--     PASS, YES. RESTRICT, NOT ALWAYS. `project.plant_id` and
--     `project.location_id` are NULLABLE by design
--     (`002_budget_control.sql:41-42`: a CAPEX project can span more than one
--     plant, and the WBS elements underneath carry the finer grain). The two
--     enforcement layers read that NULL in OPPOSITE directions:
--
--       * `capex_dimension_permits` (`004:198-213`) returns TRUE
--         `WHEN p_value IS NULL`, because it is built to waive a dimension the
--         ROW SHAPE lacks. So for a project with no plant, the plant limb of
--         019's policy is TRUE for every principal, however narrow their plant
--         grant. The effect is UNRESTRICTED on that dimension -- NOT
--         "invisible", which is the other guess a reader might make.
--       * `repo.compile_scope` (`repo.py:172`) emits `(<col> = ANY(%(ids)s))`,
--         and SQL `NULL = ANY(...)` is NULL, not TRUE, so the primary control
--         EXCLUDES the same row.
--
--     Same row, opposite directions. The backstop over-permits rather than
--     over-denies, which is the survivable direction: on
--     `capitalisation_request` and `asset_allocation`, a principal granted
--     ENTITY-A but restricted to plant P still cannot read another entity's
--     capital position (the entity limb holds), and CAN read an ENTITY-A
--     project that names no plant.
--
--     THIS IS INHERITED FROM `project`'S OWN 004 POLICY AND IS NOT A WAVE 7
--     REGRESSION. Every table that reaches scope by joining `project` has it
--     -- 013's eight, 014's reservation, 019's three alike. CHANGING THE
--     SEMANTICS IS A SCOPE DECISION about 004 and `capex_dimension_permits`,
--     not a repair belonging to this file, so nothing here changes it. What
--     changes is the claim: `rls.PROJECT_JOIN_NULL_DIMENSIONS_ARE_WAIVED`
--     states it once, and `tests/test_pg_rls_wave7_matrix.py` pins the
--     behaviour in both layers so the next reader inherits a fact instead of
--     a derivation.
--
-- (2) 019_closure.sql:377 -- "a deployment whose 018 is applied by a different
--     identity than its 004" -- means 019. It is 019's own privileges comment,
--     about 019's own GRANTs.
--
-- (3) 017_reporting.sql:216 -- "a deployment whose 016 is applied by a
--     different identity than its 004" -- means 017, for the same reason.
--
--     Neither misnaming changes what its migration DOES; both would send a
--     reader tracing a privilege problem to the wrong file. `rls.py` carried
--     the same "018" slip twice, at its `JOINED_VIA_PROJECT` comment and its
--     closure registry, and those two lines ARE editable and are corrected.
--
--
-- ==========================================================================
-- 022 CREATES NO TABLE, NO COLUMN AND NO INDEX, so it names no `*_paise`
-- column and adds no constraint. It is additive in the sense every migration
-- here must be: it destroys no data and drops no object that outlives it --
-- the one policy it drops is recreated, wider in nothing and narrower in the
-- two commands 017's comment already said should be narrow.

BEGIN;

-- ============================================ PART 1: per-command policies
--
-- 017's policy is dropped and replaced in one transaction, so there is no
-- window in which `report_saved_view` carries RLS with no policy -- which
-- would deny every read, not open one, but would still be an outage.
--
-- IF EXISTS, because an operator who has already reverted 022 once and is
-- re-applying it meets a table whose policy set is 017's again; the DROP must
-- not fail on the second pass through.
DROP POLICY IF EXISTS report_saved_view_scope ON report_saved_view;

-- READ. 017's `USING` limb, verbatim and unchanged. A principal reads a view
-- in an entity their scope permits, and within that either a SHARED view
-- whoever authored it or their own PRIVATE one. `report_view_default`'s
-- policy is an `EXISTS` over this table, so it is this policy that decides
-- what a default may point at -- keeping it byte-identical to 017's is what
-- makes that unchanged too.
CREATE POLICY report_saved_view_read ON report_saved_view
    FOR SELECT
    USING (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND (visibility = 'SHARED'
             OR owner_user_id = COALESCE(current_setting('capex.user_id', true), ''))
    );

-- CREATE. 017's `WITH CHECK` limb, verbatim. A principal may author a view
-- only in an entity they may see and only in their own name; without the
-- second conjunct a caller could plant a view attributed to a colleague.
CREATE POLICY report_saved_view_insert ON report_saved_view
    FOR INSERT
    WITH CHECK (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
    );

-- EDIT. NARROWED, and this is defect (b) above. `USING` now requires
-- OWNERSHIP, not merely visibility: under 017 a colleague met a shared view
-- through `USING` and then satisfied `WITH CHECK` by reassigning
-- `owner_user_id` to themselves in the same statement, taking the view over
-- and rewriting its filters while every default still pointed at it.
--
-- BOTH LIMBS carry the same predicate deliberately. `USING` alone would let
-- an owner move their view into an entity they cannot see; `WITH CHECK` alone
-- is what 017 had.
CREATE POLICY report_saved_view_update ON report_saved_view
    FOR UPDATE
    USING (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
    )
    WITH CHECK (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
    );

-- DELETE. NARROWED, and this is defect (a) above. A DELETE policy takes
-- `USING` only -- PostgreSQL never evaluates `WITH CHECK` for DELETE, because
-- there is no resulting row to check -- so 017's narrower limb was simply not
-- consulted and any principal in the entity could delete a colleague's shared
-- view. Ownership is required here for the same reason it is required for
-- UPDATE, and stating it in a DELETE-only policy is the only way to say it.
CREATE POLICY report_saved_view_delete ON report_saved_view
    FOR DELETE
    USING (
        capex_scope_permits(entity_id, NULL, NULL, NULL)
        AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
    );

-- OWNERSHIP IS IMMUTABLE, structurally and not only by policy.
--
-- The UPDATE policy above already refuses a reassignment: its `WITH CHECK`
-- demands the new row's owner be the caller, and its `USING` demands the old
-- row's owner be the caller, so no single principal can be both sides of a
-- transfer. The trigger is not redundant with that. RLS does not apply to a
-- superuser at all, and `FORCE ROW LEVEL SECURITY` governs only the owner's
-- own policies -- so a migration job, a maintenance script or a future policy
-- edit can move a view between owners with nothing to stop it. A saved view's
-- owner is the ONLY thing that makes `visibility = 'PRIVATE'` mean anything,
-- and the audit trail in `api/reports.py` records edits to a view, not
-- changes of who a view belongs to.
--
-- Same posture, and the same ERRCODE, as `export_job_identity_is_immutable`
-- (018) and `export_job_chunk_is_append_only` (018).
CREATE OR REPLACE FUNCTION report_saved_view_owner_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
        RAISE EXCEPTION
            'report_saved_view %: owner_user_id is immutable (% -> %). A saved '
            'view''s owner is the only thing that makes visibility=PRIVATE mean '
            'anything, and reassigning it rewrites the filters under everyone '
            'whose default points at the view. Create a new view instead.',
            OLD.view_id, OLD.owner_user_id, NEW.owner_user_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_report_saved_view_owner_immutable
    BEFORE UPDATE ON report_saved_view
    FOR EACH ROW EXECUTE FUNCTION report_saved_view_owner_is_immutable();

-- The correction from PART 3 (1), recorded where an operator will meet it.
-- `COMMENT ON` is how 007 puts a rationale in front of the person who needs
-- it (`007_scope_sentinel.sql:174`), and how 020 and 021 record theirs;
-- `\d+ capitalisation_request` is where someone reasoning about who can read
-- a capital position is already looking.
COMMENT ON COLUMN capitalisation_request.project_id IS
    'Reached through `project` by capitalisation_request_scope (019), which passes all four of that row''s dimension columns to capex_scope_permits. SCOPE NOTE: passing four is not restricting on four. project.plant_id and project.location_id are NULLABLE (002_budget_control.sql:41-42) and capex_dimension_permits WAIVES a NULL row value (004_identity_scope.sql:198-213), so for a project naming no plant this policy is UNRESTRICTED on the plant dimension for every principal -- not invisible. repo.compile_scope emits `= ANY(...)`, which is NULL for a NULL column and therefore EXCLUDES the same row, so the two layers disagree in the permissive direction on exactly these projects. Inherited from project''s own 004 policy, not introduced by 019; 019''s header says "ALL FOUR" without this qualification and cannot be edited (its checksum covers the whole file). See app/backend/pg/rls.py::PROJECT_JOIN_NULL_DIMENSIONS_ARE_WAIVED and tests/test_pg_rls_wave7_matrix.py.';

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS: nothing that is data. The four policies below are
--   -- replaced by 017's single FOR ALL policy, restoring `report_saved_view`
--   -- to exactly the state 017 left it in -- including the DELETE and UPDATE
--   -- holes this migration closed, which is what "revert" has to mean.
--   -- Every saved view, every default and every ownership row is untouched.
--   DROP TRIGGER IF EXISTS trg_report_saved_view_owner_immutable
--       ON report_saved_view;
--   DROP FUNCTION IF EXISTS report_saved_view_owner_is_immutable();
--   COMMENT ON COLUMN capitalisation_request.project_id IS NULL;
--   DROP POLICY IF EXISTS report_saved_view_delete ON report_saved_view;
--   DROP POLICY IF EXISTS report_saved_view_update ON report_saved_view;
--   DROP POLICY IF EXISTS report_saved_view_insert ON report_saved_view;
--   DROP POLICY IF EXISTS report_saved_view_read ON report_saved_view;
--   -- 017's policy, restored verbatim. Without this the table would carry RLS
--   -- with no policy at all, which denies every read -- an outage, not a
--   -- revert.
--   CREATE POLICY report_saved_view_scope ON report_saved_view
--       USING (
--           capex_scope_permits(entity_id, NULL, NULL, NULL)
--           AND (visibility = 'SHARED'
--                OR owner_user_id = COALESCE(current_setting('capex.user_id', true), ''))
--       )
--       WITH CHECK (
--           capex_scope_permits(entity_id, NULL, NULL, NULL)
--           AND owner_user_id = COALESCE(current_setting('capex.user_id', true), '')
--       );
--   DELETE FROM schema_migrations WHERE version = '022';
--   -- AND the twelve below, which their own blocks omit and which cannot be
--   -- edited to add. See PART 2 of this file's header. Reverting 022 alone
--   -- leaves all of 001..012's objects in place with no ledger row, which
--   -- upgrade() re-adopts rather than replays; continuing down the stack into
--   -- each older block drops them for real. Newest first, as the walk runs.
--   DELETE FROM schema_migrations WHERE version = '012';
--   DELETE FROM schema_migrations WHERE version = '011';
--   DELETE FROM schema_migrations WHERE version = '010';
--   DELETE FROM schema_migrations WHERE version = '009';
--   DELETE FROM schema_migrations WHERE version = '008';
--   DELETE FROM schema_migrations WHERE version = '007';
--   DELETE FROM schema_migrations WHERE version = '006';
--   DELETE FROM schema_migrations WHERE version = '005';
--   DELETE FROM schema_migrations WHERE version = '004';
--   DELETE FROM schema_migrations WHERE version = '003';
--   DELETE FROM schema_migrations WHERE version = '002';
--   DELETE FROM schema_migrations WHERE version = '001';
--   COMMIT;
