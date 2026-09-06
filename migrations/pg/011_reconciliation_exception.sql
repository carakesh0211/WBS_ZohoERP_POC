-- 011_reconciliation_exception.sql
-- The table §11.8's controls have been written against and that has never existed.
--
-- WHAT THIS CLOSES
--
-- `reconciliation_exception` is referenced by `app/backend/pg/periods.py`, by
-- the integration sweeps and by outbound unsanctioned-commitment detection. It
-- is created by no migration. `010_integration.sql` records that fact in its
-- own header rather than creating it.
--
-- The consequence was not a missing feature, it was a control that could never
-- fire. `periods._has_open_reconciliation_exceptions` answered `False` when
-- the table was absent, and `False` is the value that PERMITS a period close.
-- Its docstring called that "vacuously False"; it was not vacuous, it was
-- permissive. The gate §11.8 describes -- "a period cannot close while an Open
-- reconciliation exception exists" -- was unreachable on the shipped schema.
--
-- That function now RAISES rather than returning a permissive default, so the
-- close refuses while the control cannot be evaluated. This migration is what
-- makes the control evaluable, and therefore what makes a close possible again.
--
-- THE STATUS VALUES ARE C18'S, NOT INVENTED
--
-- `research/30_contracts/C18_domain_statuses.json` freezes the
-- `exception_status` namespace as Open / Resolved / Accepted / Written_off,
-- applying to exactly this table. C18 also records the separation that matters:
-- an exception status is NOT a C3 business status. Its business-visible
-- consequence is the C3 code RECONCILIATION_PENDING on the affected object, and
-- no exception status may reach a business screen.
--
-- THE KINDS ARE THE FIVE THE CODE RAISES
--
-- GRN_LINE_UNATTRIBUTED, CONTROL_TOTAL_MISMATCH, LATE_ARRIVAL_CLOSED_PERIOD,
-- UNMAPPED_EXTERNAL_STATUS, UNSANCTIONED_COMMITMENT. Constrained, so a sixth
-- kind is a deliberate contract change rather than a typo that silently
-- creates a category nobody triages. A document-number gap is deliberately
-- folded into CONTROL_TOTAL_MISMATCH with the missing numbers named, rather
-- than inventing a sixth kind outside the frozen set.
--
-- WHY THE MONEY COLUMNS ARE NULLABLE bigint
--
-- `local_paise` and `source_paise` are the two sides of a discrepancy, and
-- both are unknown for kinds that are not arithmetic (an unmapped status has
-- no amounts). Integer paise, never numeric: `SUM()` over bigint returns
-- numeric in PostgreSQL, so any aggregate over these must cast `::bigint`.
--
-- IDEMPOTENCY
--
-- The sweeps re-walk. `ux_reconciliation_exception_open` makes raising the
-- same exception twice while it is still Open a no-op rather than a duplicate,
-- which is what lets a re-walk be safe. It is PARTIAL on `status = 'Open'`
-- deliberately: once an exception is resolved, the same condition recurring is
-- a genuinely new exception and must be raisable again.

BEGIN;

CREATE TABLE reconciliation_exception (
    exception_id    text PRIMARY KEY,
    kind            text NOT NULL,
    object_type     text NOT NULL,
    object_id       text,

    -- Scope. Nullable because some exceptions are raised before the owning
    -- entity or project is known -- an unsanctioned commitment discovered on a
    -- PO we have no local record of, for instance. RLS treats a NULL dimension
    -- as unrestricted-by-that-dimension, which is correct here: an exception
    -- nobody can attribute must be visible to whoever can resolve it.
    entity_id       text REFERENCES entity (entity_id),
    project_id      text REFERENCES project (project_id),

    status          text NOT NULL DEFAULT 'Open',
    detail          text NOT NULL,

    -- The two sides of the discrepancy, in integer paise.
    local_paise     bigint,
    source_paise    bigint,

    correlation_id  text,
    raised_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolved_by     text,
    resolution_note text,

    CONSTRAINT ck_reconciliation_exception_kind CHECK (
        kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
                 'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
                 'UNSANCTIONED_COMMITMENT')
    ),

    -- C18's frozen exception_status namespace, verbatim.
    CONSTRAINT ck_reconciliation_exception_status CHECK (
        status IN ('Open', 'Resolved', 'Accepted', 'Written_off')
    ),

    -- A resolution names WHO and WHEN, or it is not a resolution. An exception
    -- that left Open without an actor is indistinguishable from one that was
    -- never triaged, which defeats the audit trail the whole control rests on.
    CONSTRAINT ck_reconciliation_exception_resolution CHECK (
        (status = 'Open'
             AND resolved_at IS NULL AND resolved_by IS NULL)
        OR
        (status <> 'Open'
             AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL)
    ),

    -- Money is never negative here: these are magnitudes of two sides, and a
    -- sign would silently encode a direction the `kind` is supposed to carry.
    CONSTRAINT ck_reconciliation_exception_paise CHECK (
        (local_paise IS NULL OR local_paise >= 0)
        AND (source_paise IS NULL OR source_paise >= 0)
    )
);

-- Idempotent re-raise while Open. Partial on purpose -- see the header.
CREATE UNIQUE INDEX ux_reconciliation_exception_open
    ON reconciliation_exception (kind, object_type, object_id)
    WHERE status = 'Open' AND object_id IS NOT NULL;

-- `periods._has_open_reconciliation_exceptions` asks exactly this question on
-- every close, and the capitalisation gate asks it per project.
CREATE INDEX ix_reconciliation_exception_open_by_entity
    ON reconciliation_exception (entity_id, status)
    WHERE status = 'Open';

CREATE INDEX ix_reconciliation_exception_open_by_project
    ON reconciliation_exception (project_id, status)
    WHERE status = 'Open';

-- Row-level security, matching the other scoped tables in 004/006/010.
ALTER TABLE reconciliation_exception ENABLE ROW LEVEL SECURITY;
ALTER TABLE reconciliation_exception FORCE ROW LEVEL SECURITY;

-- ARGUMENT ORDER MATTERS AND IS EASY TO GET WRONG.
--
-- `capex_scope_permits(p_entity_id, p_plant_id, p_location_id, p_project_id)`
-- -- see 004. All four parameters are `text`, so PostgreSQL accepts ANY order
-- silently. This policy originally read
-- `(entity_id, NULL, project_id, NULL)`, which put `project_id` in the
-- LOCATION slot and left the project slot NULL: the project dimension was
-- waived entirely, and a principal restricted to one project could read
-- another project's `local_paise` and `source_paise` within the same entity.
-- Caught by a scan that audits every call site against the signature; it is
-- now the only thing standing between this and a silent re-break.
CREATE POLICY reconciliation_exception_scope ON reconciliation_exception
    USING (capex_scope_permits(entity_id, NULL, NULL, project_id))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, project_id));

-- The application role needs its privileges stated, as 008 and 010 state
-- theirs. Omitting this worked only because one superuser runs every
-- migration in CI -- which is the same assumption that hid the RLS bypass.
GRANT SELECT, INSERT, UPDATE ON reconciliation_exception TO capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Dropping this table RE-OPENS the fail-open hole only if
--   -- `periods._has_open_reconciliation_exceptions` is also reverted to
--   -- returning False for an absent table. As it stands it RAISES, so
--   -- dropping the table blocks every period close rather than permitting
--   -- them -- loud, and the safe direction.
--   DROP POLICY IF EXISTS reconciliation_exception_scope ON reconciliation_exception;
--   DROP TABLE IF EXISTS reconciliation_exception;
--   COMMIT;
