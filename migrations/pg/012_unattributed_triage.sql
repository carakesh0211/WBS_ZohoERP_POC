-- 012_unattributed_triage.sql
-- An unattributed exception must not be visible to everyone, and must not
-- become invisible to everyone.
--
-- THE CONTRADICTION THIS CLOSES
--
-- `reconciliation_exception.entity_id` and `.project_id` are NULLABLE on
-- purpose: an UNSANCTIONED_COMMITMENT can be discovered on a purchase order we
-- hold no local record of, so there is no entity to attribute it to yet.
--
-- Two layers then disagreed about what that NULL means, and both were wrong:
--
--   * RLS said EVERYONE. `capex_dimension_permits` returns `true` when the
--     row's value is NULL, and its own comment explains why -- it is meant to
--     be reached "with a literal SQL NULL passed by a policy, never by an
--     absent session setting", i.e. it waives a dimension the TABLE does not
--     carry. But `entity_id` here is a nullable COLUMN, so a NULL is a DATA
--     null, not a schema waiver, and the function cannot tell the two apart.
--     Every principal could read every unattributed exception -- including
--     `local_paise` and `source_paise`, the exact sums by which somebody's
--     books do not tie out.
--
--   * The application said NOBODY. `repo.compile_scope` emits
--     `entity_id = ANY(...)`, and SQL NULL is not equal to anything, so the
--     compiled predicate excluded those rows from every caller.
--
-- Neither is acceptable. §11.8 requires an unattributed line to be held at
-- full value, visible, and blocking capitalisation -- a row nobody can see is
-- a silent drop with extra steps, and a row everybody can see is a leak.
--
-- THE RESOLUTION
--
-- Unattributed rows are visible ONLY to a principal explicitly granted
-- triage. Not to `read_all`, not by accident, and not to an ordinary user of
-- any scope. Attributing one is a permission-gated, audited write that moves
-- the row into normal scope, after which it is governed like every other row.

BEGIN;

-- The triage flag is its own session setting, deliberately NOT folded into
-- `read_all`.
--
-- `read_all` is documented as "for migrations and start-up checks only, never
-- for a request". Reusing it here would mean the only way to triage an
-- exception is to hold unrestricted read over the entire estate, which is a
-- far larger grant than "may look at the rows nobody could attribute yet".
-- Two different questions deserve two different settings.
CREATE OR REPLACE FUNCTION capex_may_triage_unattributed()
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT COALESCE(
        current_setting('capex.triage_unattributed', true), 'false') = 'true';
$$;

GRANT EXECUTE ON FUNCTION capex_may_triage_unattributed() TO PUBLIC;

-- Replace 011's policy.
--
-- 011 also had its arguments in the wrong order -- `project_id` sat in the
-- `p_location_id` slot, so the project dimension was never enforced at all.
-- That is fixed here as well as in 011, because a database restored from a
-- point between the two migrations must not silently keep the broken form.
DROP POLICY IF EXISTS reconciliation_exception_scope ON reconciliation_exception;

CREATE POLICY reconciliation_exception_scope ON reconciliation_exception
    USING (
        CASE
            -- Unattributed: triage only. Note this is NOT
            -- `capex_scope_permits(NULL, ...)`, which would waive the
            -- dimension and admit everyone -- the exact defect being closed.
            WHEN entity_id IS NULL THEN capex_may_triage_unattributed()
            ELSE capex_scope_permits(entity_id, NULL, NULL, project_id)
        END
    )
    WITH CHECK (
        CASE
            WHEN entity_id IS NULL THEN capex_may_triage_unattributed()
            ELSE capex_scope_permits(entity_id, NULL, NULL, project_id)
        END
    );

-- `WITH CHECK` matters as much as `USING` here. Without it a triage principal
-- could attribute a row to an entity it has no business writing to, or an
-- ordinary principal could push a row it owns into the unattributed bucket and
-- out of everyone else's sight.

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Reverting REOPENS the leak: unattributed rows become visible to every
--   -- principal again, because capex_dimension_permits treats a NULL row
--   -- value as a waived dimension. Do not revert without also making
--   -- entity_id NOT NULL, which the unsanctioned-commitment case forbids.
--   DROP POLICY IF EXISTS reconciliation_exception_scope ON reconciliation_exception;
--   CREATE POLICY reconciliation_exception_scope ON reconciliation_exception
--       USING (capex_scope_permits(entity_id, NULL, NULL, project_id))
--       WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, project_id));
--   DROP FUNCTION IF EXISTS capex_may_triage_unattributed();
--   COMMIT;
