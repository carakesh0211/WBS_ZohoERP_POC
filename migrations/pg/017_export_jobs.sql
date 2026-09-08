-- 017_export_jobs.sql
-- Asynchronous, chunked, checkpointed exports -- and the row that makes
-- SVC-EXPORT incapable of being a scope-escalation path.
--
--
-- WHY A TABLE AT ALL
--
-- `docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` section 10.3 gives
-- SVC-EXPORT one sentence and it is the whole design:
--
--     "runs an export under the requesting user's scope, never its own;
--      inherits the requester's Scope, persisted on the export job row"
--
-- A background worker has no request, no session header and no principal of
-- its own to resolve grants from. If it resolved a scope when it ran, it
-- would be resolving SOMEBODY'S -- and the only identity available to it at
-- that moment is the service account's. That is the escalation. The requester's
-- resolved `Scope` is therefore captured HERE, at creation, inside the request
-- that already authenticated the requester, and the worker rehydrates it
-- rather than deriving one.
--
-- Captured means captured. `trg_export_job_identity_immutable` refuses any
-- UPDATE that changes `requested_by`, `requested_principal_kind`, `scope_json`,
-- `scope_digest`, `dataset`, `filter_json`, `column_order` or `created_at`.
-- Progress, state, checkpoints and results are all mutable; the authorisation
-- the job was born with is not. A CHECK constraint cannot see OLD, so this is
-- a trigger, exactly as `assert_original_budget_line_immutable` (003) is.
--
--
-- WHY A DIGEST AS WELL AS A TRIGGER
--
-- `scope_digest` is SHA-256 over the job id and the canonical rendering of
-- `scope_json`. The trigger stops an UPDATE; the digest is what the WORKER
-- checks before it reads a single business row, and it covers the cases the
-- trigger structurally cannot: a row DELETEd and re-INSERTed, a restore from a
-- doctored dump, a session that disabled the trigger, a direct write by a role
-- holding more than `capex_app` does.
--
-- It is a detection control and it is described as one. It is not a MAC: it
-- carries no key, so an actor who can rewrite the row can recompute it. That is
-- why it is the SECOND of two defences and not the only one --
-- `app/backend/pg/exports.py::rehydrate_scope` also MEETS the stored scope with
-- the requester's currently-resolved grants, and a meet can only ever narrow.
-- A widened `scope_json` whose digest was recomputed still reads nothing the
-- requester cannot read today. Neither control is trusted alone.
--
--
-- WHY THE CHUNKS ARE ROWS IN POSTGRESQL
--
-- "No cloud resource" is a hard rule of this wave, and the AppSail request
-- budget is 30 seconds. An export of 250,000 rows fits in neither one
-- invocation nor an object store this build is allowed to create. Each chunk
-- is therefore rendered, hashed and COMMITTED as an `export_job_chunk` row,
-- with the keyset it stopped at recorded on the job. A worker that dies
-- mid-export loses at most the chunk it had not committed, and the next
-- invocation resumes from `resume_key` -- not from an OFFSET, which would
-- skip or duplicate rows the moment anything else wrote to the source table.
--
-- Chunks are append-only: `trg_export_job_chunk_append_only` refuses UPDATE and
-- the REVOKE at the foot of this file removes the privilege as well. An export
-- whose already-delivered bytes can be rewritten is not evidence of anything.
--
--
-- WHY `rows_total` IS NULLABLE
--
-- Because it is genuinely unknown until the last chunk is written, and this
-- product does not fabricate a total. NULL means "not counted yet" and every
-- reader renders it as such. `ck_export_job_succeeded_is_complete` then makes
-- the honest version structural: a job may not reach SUCCEEDED unless
-- `rows_total` is present, equals `rows_written`, and a result digest and byte
-- count exist. There is no state in which this table reports a finished export
-- with a made-up size.
--
--
-- ROW-LEVEL SECURITY, AND THE TWO PRINCIPALS THAT MAY SEE A JOB
--
-- An export job carries no entity, plant, location or project column, and it
-- would be wrong to give it one: its `scope_json` is a SET of ids across all
-- four dimensions, which is not something a per-row predicate can filter on.
-- The line the row shape does support is OWNERSHIP, and it is the right line:
-- an export job is the requester's own artefact, and its `scope_json` and
-- rendered chunks are more sensitive than the job list itself.
--
--   * `export_job_owner`   -- the requester, and nobody else, sees their jobs.
--   * `export_job_service` -- the SVC-EXPORT service principal sees job rows so
--                             it can claim and advance them. It gets the job
--                             METADATA; it never gets a wider view of business
--                             data, because every business read it performs is
--                             compiled from the rehydrated requester scope.
--
-- Policies are PERMISSIVE, so the two are OR'd. `capex_principal_present()` is
-- 006's frozen predicate, reused rather than reinvented: a session with no
-- scope applied at all sees nothing here either.
--
-- `app/backend/pg/scope_inventory.py` records both tables with
-- `status="protected_pending_registry"` -- this migration enables, forces and
-- policies them, and `app/backend/pg/rls.py`'s registry is lead-owned and not
-- edited by this stream. That is the same in-between state 008's and 010's
-- tables carry, for the same reason.

BEGIN;

-- ================================================================ export_job
CREATE TABLE export_job (
    export_job_id   text NOT NULL,

    -- WHAT is being exported. Resolved against the dataset registry in
    -- `app/backend/pg/exports.py`; stored so a job whose dataset is later
    -- withdrawn reports honestly rather than silently exporting something else.
    dataset         text NOT NULL,
    output_format   text NOT NULL DEFAULT 'csv',

    state           text NOT NULL DEFAULT 'QUEUED',

    -- ---- the immutable authorisation capture ---------------------------
    -- Server-derived from the authenticated session at creation. Never from a
    -- request body: a caller-nominated requester is a caller-nominated scope.
    requested_by             text NOT NULL,
    requested_principal_kind text NOT NULL DEFAULT 'USER',
    -- The requester's RESOLVED `Scope`, in the three-state wire form
    -- (`null` = unrestricted, `[]` = nothing, `[ids]` = restricted). The three
    -- states are preserved in JSON exactly as `engine.Scope` preserves them;
    -- collapsing `[]` into `null` here would turn "no grants" into "all rows",
    -- which is the single defect this whole capture exists to prevent.
    scope_json      jsonb NOT NULL,
    scope_digest    text  NOT NULL,
    -- The caller's FilterSet. Narrows WITHIN `scope_json`; can never widen it.
    filter_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- The column order the result will carry, captured at creation so a
    -- registry change mid-flight cannot reorder a half-written file.
    column_order    text[] NOT NULL,

    -- ---- progress and checkpointing ------------------------------------
    chunk_rows      integer NOT NULL DEFAULT 5000,
    rows_written    bigint  NOT NULL DEFAULT 0,
    -- NULL until the export actually finishes counting. Never a guess.
    rows_total      bigint,
    chunks_written  integer NOT NULL DEFAULT 0,
    -- The keyset the last committed chunk stopped at. NOT an offset: an offset
    -- over a table anything else is writing skips and duplicates rows.
    resume_key      jsonb,

    attempt         integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 3,
    -- Cancellation is a REQUEST, honoured at the next chunk boundary. A worker
    -- mid-chunk is inside a transaction; killing it there would leave the
    -- committed prefix and the counters disagreeing.
    cancel_requested boolean NOT NULL DEFAULT false,

    error_code      text,
    error_detail    text,

    -- ---- the downloadable result ---------------------------------------
    result_sha256    text,
    result_bytes     bigint,
    result_filename  text,
    result_media_type text,

    correlation_id  text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    -- A rendered export is a copy of scoped financial data sitting outside the
    -- tables RLS protects. It expires.
    expires_at      timestamptz NOT NULL,

    CONSTRAINT pk_export_job PRIMARY KEY (export_job_id),

    CONSTRAINT ck_export_job_state CHECK (
        state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED',
                  'EXPIRED')),
    CONSTRAINT ck_export_job_output_format CHECK (output_format IN ('csv')),
    CONSTRAINT ck_export_job_principal_kind CHECK (
        requested_principal_kind IN ('USER', 'SERVICE')),
    CONSTRAINT ck_export_job_requested_by_present CHECK (
        btrim(requested_by) <> ''),
    -- 64 lowercase hex characters. A blank or truncated digest must not be
    -- storable: `exports.py` refuses a job whose digest does not verify, and a
    -- digest that is structurally impossible to verify would refuse forever
    -- for the wrong reason.
    CONSTRAINT ck_export_job_scope_digest_shape CHECK (
        scope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_export_job_scope_json_is_object CHECK (
        jsonb_typeof(scope_json) = 'object'),
    CONSTRAINT ck_export_job_filter_json_is_object CHECK (
        jsonb_typeof(filter_json) = 'object'),
    CONSTRAINT ck_export_job_column_order_nonempty CHECK (
        array_length(column_order, 1) >= 1),

    CONSTRAINT ck_export_job_chunk_rows_bounded CHECK (
        chunk_rows > 0 AND chunk_rows <= 50000),
    CONSTRAINT ck_export_job_counters_nonneg CHECK (
        rows_written >= 0 AND chunks_written >= 0
        AND (rows_total IS NULL OR rows_total >= 0)),
    CONSTRAINT ck_export_job_attempts CHECK (
        attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts),

    -- Terminal exactly when finished. Neither half may drift from the other:
    -- a RUNNING job with a finish time reads as done to anything ordering by
    -- `finished_at`, and a SUCCEEDED job without one cannot be aged out.
    CONSTRAINT ck_export_job_terminal_is_finished CHECK (
        (state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'))
        = (finished_at IS NOT NULL)),
    CONSTRAINT ck_export_job_failure_is_coded CHECK (
        state <> 'FAILED' OR error_code IS NOT NULL),
    -- The structural form of "never fabricate a total or a row count".
    CONSTRAINT ck_export_job_succeeded_is_complete CHECK (
        state <> 'SUCCEEDED' OR (
            rows_total IS NOT NULL
            AND rows_total = rows_written
            AND result_sha256 IS NOT NULL
            AND result_bytes IS NOT NULL
            AND result_filename IS NOT NULL
            AND result_media_type IS NOT NULL)),
    CONSTRAINT ck_export_job_result_digest_shape CHECK (
        result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_export_job_expires_after_creation CHECK (
        expires_at > created_at)
);

CREATE INDEX ix_export_job_requester ON export_job (requested_by, created_at DESC);
-- The claim scan. Partial, because a finished job is never a candidate and the
-- finished rows are the ones that accumulate.
CREATE INDEX ix_export_job_runnable ON export_job (state, created_at)
    WHERE state IN ('QUEUED', 'RUNNING');
-- The expiry sweep, over the only state that holds deliverable bytes.
CREATE INDEX ix_export_job_expiring ON export_job (expires_at)
    WHERE state = 'SUCCEEDED';

-- ---------------------------------------------------------------- immutability
-- The authorisation a job was born with cannot be edited into a different one.
CREATE OR REPLACE FUNCTION export_job_identity_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.export_job_id            IS DISTINCT FROM OLD.export_job_id
       OR NEW.requested_by          IS DISTINCT FROM OLD.requested_by
       OR NEW.requested_principal_kind IS DISTINCT FROM OLD.requested_principal_kind
       OR NEW.scope_json            IS DISTINCT FROM OLD.scope_json
       OR NEW.scope_digest          IS DISTINCT FROM OLD.scope_digest
       OR NEW.dataset               IS DISTINCT FROM OLD.dataset
       OR NEW.filter_json           IS DISTINCT FROM OLD.filter_json
       OR NEW.column_order          IS DISTINCT FROM OLD.column_order
       OR NEW.created_at            IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'export_job % captures its requester and resolved scope immutably '
            'at creation; UPDATE of the captured authorisation is denied',
            OLD.export_job_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_export_job_identity_immutable BEFORE UPDATE ON export_job
    FOR EACH ROW EXECUTE FUNCTION export_job_identity_is_immutable();

-- ========================================================== export_job_chunk
CREATE TABLE export_job_chunk (
    export_job_id text NOT NULL,
    chunk_no      integer NOT NULL,

    row_count     integer NOT NULL,
    byte_count    bigint  NOT NULL,
    sha256        text    NOT NULL,
    -- The rendered fragment. Text, not bytea: every dataset here renders CSV,
    -- and money in it is already an exact decimal string produced from integer
    -- paise -- never a float, and never re-formatted by a reader.
    body          text    NOT NULL,

    -- The keyset boundaries this chunk covers, so a resumed export can be
    -- proved contiguous rather than assumed to be.
    first_key     jsonb,
    last_key      jsonb,

    written_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_export_job_chunk PRIMARY KEY (export_job_id, chunk_no),
    CONSTRAINT fk_export_job_chunk_job FOREIGN KEY (export_job_id)
        REFERENCES export_job (export_job_id) ON DELETE CASCADE,
    CONSTRAINT ck_export_job_chunk_no_positive CHECK (chunk_no >= 1),
    CONSTRAINT ck_export_job_chunk_row_count_positive CHECK (row_count >= 1),
    CONSTRAINT ck_export_job_chunk_byte_count_nonneg CHECK (byte_count >= 0),
    CONSTRAINT ck_export_job_chunk_digest_shape CHECK (
        sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_export_job_chunk_job ON export_job_chunk (export_job_id, chunk_no);

CREATE OR REPLACE FUNCTION export_job_chunk_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'export_job_chunk (%, %) is append-only: % denied. A delivered export '
        'whose bytes can be rewritten is not evidence of anything.',
        OLD.export_job_id, OLD.chunk_no, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER trg_export_job_chunk_append_only BEFORE UPDATE ON export_job_chunk
    FOR EACH ROW EXECUTE FUNCTION export_job_chunk_is_append_only();

-- ================================================== row-level security
ALTER TABLE export_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_job FORCE ROW LEVEL SECURITY;

-- The requester's own jobs. WITH CHECK as well as USING: without it a caller
-- could INSERT a job attributed to somebody else -- which is a job that would
-- then run under somebody else's captured scope.
CREATE POLICY export_job_owner ON export_job
    USING (capex_principal_present()
           AND requested_by = COALESCE(current_setting('capex.user_id', true), ''))
    WITH CHECK (capex_principal_present()
                AND requested_by = COALESCE(current_setting('capex.user_id', true), ''));

-- The worker. It may SEE and ADVANCE any job row; it gains no business data
-- from that, because every business read it issues is compiled from the job's
-- rehydrated requester scope and never from this session's.
CREATE POLICY export_job_service ON export_job
    USING (COALESCE(current_setting('capex.principal_kind', true), '') = 'SERVICE'
           AND COALESCE(current_setting('capex.user_id', true), '') = 'SVC-EXPORT')
    WITH CHECK (COALESCE(current_setting('capex.principal_kind', true), '') = 'SERVICE'
                AND COALESCE(current_setting('capex.user_id', true), '') = 'SVC-EXPORT');

ALTER TABLE export_job_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_job_chunk FORCE ROW LEVEL SECURITY;

-- A chunk is visible to exactly whoever the job is visible to. Expressed as an
-- EXISTS over `export_job`, so the parent's own two policies decide, and there
-- is one definition of "may see this export" rather than two that can drift.
CREATE POLICY export_job_chunk_via_job ON export_job_chunk
    USING (EXISTS (SELECT 1 FROM export_job j
                   WHERE j.export_job_id = export_job_chunk.export_job_id))
    WITH CHECK (EXISTS (SELECT 1 FROM export_job j
                        WHERE j.export_job_id = export_job_chunk.export_job_id));

-- ====================================================== privileges
GRANT SELECT, INSERT, UPDATE ON export_job TO capex_app;
GRANT SELECT, INSERT ON export_job_chunk TO capex_app;

-- 004's `ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO capex_app` means
-- the GRANTs above ADD nothing that was not already there; the REVOKEs are
-- what actually removes a privilege. Same reasoning as 013's and 014's.
--
-- DELETE on `export_job` is revoked because a job row is the audit-visible
-- record that an export of scoped financial data was produced and by whom.
-- Aging one out is `state = 'EXPIRED'` plus the chunk purge below, not a
-- vanished row.
REVOKE DELETE ON export_job FROM capex_app;
-- UPDATE on a chunk is revoked in addition to the trigger. The trigger is the
-- guarantee; the REVOKE is what stops the attempt reaching it.
REVOKE UPDATE ON export_job_chunk FROM capex_app;
-- DELETE on a chunk is DELIBERATELY GRANTED, and it is the one deletion this
-- schema wants: expiry purges the rendered bytes while the job row -- who
-- exported what, under which scope, when -- stays exactly where it is.
GRANT DELETE ON export_job_chunk TO capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Dropping these tables loses the record of which exports were produced
--   -- and under whose scope. Export the rows first if that record is wanted;
--   -- nothing else in the schema references them, so nothing else breaks.
--   DROP POLICY IF EXISTS export_job_chunk_via_job ON export_job_chunk;
--   DROP POLICY IF EXISTS export_job_service ON export_job;
--   DROP POLICY IF EXISTS export_job_owner ON export_job;
--   DROP TRIGGER IF EXISTS trg_export_job_chunk_append_only ON export_job_chunk;
--   DROP TRIGGER IF EXISTS trg_export_job_identity_immutable ON export_job;
--   DROP TABLE IF EXISTS export_job_chunk;
--   DROP TABLE IF EXISTS export_job;
--   DROP FUNCTION IF EXISTS export_job_chunk_is_append_only();
--   DROP FUNCTION IF EXISTS export_job_identity_is_immutable();
--   COMMIT;
