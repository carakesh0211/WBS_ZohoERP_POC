-- 031_job_one_live_row_per_connection.sql
-- One live `job` row per (kind, connection_id): a database-enforced fact,
-- not just an application-level convention.
--
-- WHAT WAS MISSING
-- ================
--
-- `integration/live_sweep.py::_job_row_for` reuses a connection's existing
-- job row for a given `kind` rather than enqueueing a fresh one per call,
-- because the checkpoint (a poll's open window, the PO-anchored walk's
-- cursor) lives on the row and a new row per call would reset it every
-- time. It does this with a plain SELECT-then-INSERT: look for a row in
-- `jobs.CLAIMABLE_STATES` (PENDING, CLAIMED, CHECKPOINTED, FAILED -- every
-- non-terminal state; DONE and DEAD are the only terminal ones), and
-- `enqueue_job` a new one only if none is found.
--
-- Nothing serialised that read against a concurrent identical read. Two
-- sweep ticks -- or a manual sweep trigger racing the scheduled one -- on
-- the SAME connection and the SAME job `kind` could both find no live row,
-- both proceed to `enqueue_job`, and both succeed: `010_integration.sql`
-- put no uniqueness on `(kind, connection_id)`, only a plain non-unique
-- index (`ix_job_connection`) for lookups. Two job rows for one
-- (kind, connection_id) then race on the single watermark row
-- `integration_watermark` and on the PO-anchored walk's checkpoint, each
-- claiming and advancing state the other cannot see.
--
-- WHAT THIS ADDS
-- ==============
--
-- `ux_job_one_live_row_per_connection`: a UNIQUE index on `job (kind,
-- connection_id)`, partial on the row being both connection-bound
-- (`connection_id IS NOT NULL` -- `verify_audit_chains` and
-- `sweep_control_totals` are estate-wide and legitimately have no
-- connection, so they are outside this index entirely, as
-- `ux_reconciliation_exception_open` and `ux_grn_external` are partial on
-- their own governing predicates) and LIVE (`state` in the same four
-- values `jobs.CLAIMABLE_STATES` names: PENDING, CLAIMED, CHECKPOINTED,
-- FAILED). A DONE or DEAD row -- the two terminal states -- never appears
-- in the index, so history accumulates without limit while the invariant
-- holds only over what is still active, matching `_job_row_for`'s own
-- reuse condition exactly.
--
-- This is enforced independent of any application-level fix: even a caller
-- that never took a lock, or a second process that skipped `_job_row_for`
-- entirely and called `enqueue_job` directly, cannot create a second live
-- row for a (kind, connection_id) that already has one -- the INSERT is
-- refused by the database with `unique_violation` rather than merely
-- discouraged by convention.
--
-- WHAT THIS DOES NOT DO
-- ======================
--
-- It does not touch `enqueue_job`'s SQL, `_job_row_for`'s SELECT, or the
-- job table's columns or CHECK constraints. It does not change what
-- `_job_row_for` does when it finds no live row and calls `enqueue_job`;
-- that call, plus the `pg_advisory_xact_lock` `_job_row_for` now takes
-- before its SELECT (`app/backend/integration/live_sweep.py`), closes the
-- race at the application level for the one caller this module has today.
-- This index is the fact that holds even if a future caller forgets the
-- lock.
--
-- A database carrying two already-conflicting live rows for the same
-- (kind, connection_id) -- possible only if the race this migration closes
-- has already been hit -- makes this migration FAIL with the index's own
-- `unique_violation`, by design: the ambiguity of which of the two rows is
-- the one to keep is not resolved here. List them first with:
--   SELECT kind, connection_id, job_id, state, created_at FROM job
--   WHERE connection_id IS NOT NULL
--     AND state IN ('PENDING', 'CLAIMED', 'CHECKPOINTED', 'FAILED')
--   ORDER BY kind, connection_id, created_at;
-- and either finish/kill the older of each pair or mark it DEAD (which
-- `ck_job_dead_is_alerted`, 010, requires `alerted_at` for) before this
-- migration runs. The demonstration seed carries no live jobs, so a seeded
-- database is unaffected.
--
-- Additive: one index, no column, no table, no trigger.

BEGIN;

CREATE UNIQUE INDEX ux_job_one_live_row_per_connection
    ON job (kind, connection_id)
    WHERE connection_id IS NOT NULL
      AND state IN ('PENDING', 'CLAIMED', 'CHECKPOINTED', 'FAILED');

COMMENT ON INDEX ux_job_one_live_row_per_connection IS
    'At most one non-terminal job per (kind, connection_id). Partial on connection_id IS NOT NULL (estate-wide jobs such as verify_audit_chains carry no connection and are exempt) and on state being one of jobs.CLAIMABLE_STATES (PENDING, CLAIMED, CHECKPOINTED, FAILED) -- DONE and DEAD, the terminal states, never appear here. Closes the TOCTOU in live_sweep._job_row_for: a plain SELECT-then-INSERT with no lock could let two concurrent sweep ticks each find no live row and each enqueue one, racing on the same watermark and checkpoint thereafter.';

-- Migration 031 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS. The database stops refusing a second live job
--   -- row for the same (kind, connection_id); _job_row_for's advisory lock
--   -- (live_sweep.py) is then the ONLY thing preventing the duplicate this
--   -- migration exists to make impossible at the database as well.
--   DROP INDEX IF EXISTS ux_job_one_live_row_per_connection;
--   DELETE FROM schema_migrations WHERE version = '031';
--   COMMIT;
