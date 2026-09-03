-- 008_approval_engine.sql
-- Wave 4 / M4b, stream 1 (data model): the configurable multilevel approval
-- engine's schema.
--
-- Plan traceability: Phase 4 -> M4b, REQ-PRC-003 (configurable multilevel
-- approvals across all applicable modules) and REQ-REV-008 ("submitted
-- revisions create no spending capacity"). Table and column names are frozen
-- by docs/WAVE4_CONTRACTS.md Contract 1; statuses by Contract 2; the scope
-- columns by Contract 6; the action chain by Contract 9. Streams 2, 3 and 5
-- import `app/backend/pg/approval_schema.py` rather than retyping any name
-- below, and `tests/test_pg_approval_schema.py` asserts that module and this
-- file cannot drift apart.
--
-- WHAT THIS FILE ADDS
--
--   reason_code             the controlled vocabulary a decision may cite.
--                           Created FIRST: approval_action references it.
--   approval_definition     one VERSION of one workflow. Change means a new
--                           version, never an edit -- see IMMUTABILITY below.
--   approval_rule           priority-ordered routing predicates that select a
--                           definition for a given object.
--   approval_stage          one level of a definition: who, how many, by when,
--                           and whether it applies at all.
--   approval_stage_approver the approver slots of a stage (ROLE or USER).
--   approval_instance       one routed document: the workflow it was routed
--                           UNDER and the document content it was routed FOR,
--                           both frozen at creation.
--   approval_stage_instance one stage of one instance, including the stages
--                           that did NOT run (status SKIPPED + skip_reason).
--   approval_assignment     one addressable approver on one stage instance.
--   approval_action         the append-only, hash-chained decision log.
--   approval_delegation     "X may act for Y over this date range".
--
-- IMMUTABILITY, ENFORCED BY THE DATABASE AND NOT BY DISCIPLINE
--
-- Three separate mechanisms, because they protect three different things:
--
--   1. A live workflow's DEFINITION cannot be rewritten under decisions
--      already taken beneath it. An ACTIVE or RETIRED approval_definition, and
--      every approval_rule / approval_stage / approval_stage_approver beneath
--      it, refuses UPDATE and DELETE. A DRAFT is freely editable, which is the
--      point of having a DRAFT status at all. A CHECK constraint cannot see
--      OLD, so each of these is a trigger.
--
--      THE ONE PERMITTED CHANGE TO A LIVE DEFINITION IS ITS RETIREMENT.
--      Contract 1 says both "an ACTIVE or RETIRED definition refuses UPDATE"
--      and "the old row is retired, never edited" -- and retiring a row IS an
--      update to it, so an unconditional refusal would make the contract's own
--      prescribed change mechanism impossible to perform. The trigger
--      therefore permits exactly one transition, ACTIVE -> RETIRED, touching
--      exactly `status`, `retired_at` and `retired_by`; every other column
--      must be byte-identical, RETIRED is terminal, and DELETE is refused
--      unconditionally. Nothing else about a live definition can move. This
--      reading is recorded here rather than left implicit because it is the
--      one place this migration interprets the contract instead of
--      transcribing it.
--
--   2. approval_action is APPEND-ONLY -- exactly the treatment audit_log gets
--      in 001_foundation.sql: a BEFORE UPDATE OR DELETE trigger (the schema
--      stating its intent, and a catch for a misconfigured grant) PLUS
--      `REVOKE UPDATE, DELETE ... FROM capex_app` (the real guarantee, a
--      privilege one). Belt and braces, both, for the same reason 001 uses
--      both.
--
--   3. approval_instance.snapshot, definition_version and object_content_sha
--      cannot change once written. An instance records the workflow it was
--      routed under and the document it was routed for; if either could drift,
--      an approval would stop being evidence of what was actually approved.
--      status, current_stage_no and closed_at REMAIN updatable -- the engine
--      advances an instance through its own lifecycle -- so this is a
--      column-scoped trigger with a WHEN clause, not a table-wide refusal.
--
-- FAIL CLOSED, NEVER AUTO-APPROVE (Contract 2)
--
-- An instance that resolved no definition carries definition_id IS NULL and is
-- constrained to status EXCEPTION_PENDING: `ck_approval_instance_definition`
-- makes "unrouted but approved" unrepresentable rather than merely unlikely.
-- A stage that did not run is recorded SKIPPED with a skip_reason
-- (`ck_approval_stage_instance_skip_reason`), never omitted, because an
-- auditor must be able to see what did not run.
--
-- ROW-LEVEL SECURITY (Contract 6)
--
-- approval_instance carries `entity_id` and `project_id` denormalised at
-- creation, so it is scopable directly; the three child tables reach those
-- columns through it. Every policy calls the frozen `capex_scope_permits`
-- from 004_identity_scope.sql (whose body 007_scope_sentinel.sql replaced), so
-- this migration inherits the mode/ids wire format and its fail-closed
-- treatment of an absent session setting without redefining either function.
-- ENABLE and FORCE both, on every table: ENABLE alone leaves a policy that
-- appears in `pg_policies` and is bypassed silently by the table's OWNER --
-- in production the deploy identity, which is not a superuser.
--
-- The workflow CONFIGURATION tables (definition, rule, stage, stage_approver)
-- are scoped too. approval_definition carries a nullable `entity_id`: NULL
-- means the definition applies organisation-wide and every principal may see
-- it (the dimension is waived, exactly as `capex_dimension_permits` waives a
-- SQL NULL); a non-NULL entity_id confines it. Leaving these bare would repeat
-- the `budget_head` defect that app/backend/pg/scope_inventory.py still
-- reports -- a caller restricted to one entity could enumerate another
-- entity's approval workflow, its thresholds and its named approvers.
--
-- Expand-only. Nothing here alters, drops or redefines anything 001..007
-- created. In particular it does NOT redefine capex_scope_permits,
-- capex_dimension_permits or capex_principal_present: their signatures and
-- bodies are frozen, and every policy below calls them unchanged.
--
-- ROLLBACK:
--   DROP POLICY IF EXISTS approval_definition_scope ON approval_definition;
--   DROP POLICY IF EXISTS approval_rule_scope ON approval_rule;
--   DROP POLICY IF EXISTS approval_stage_scope ON approval_stage;
--   DROP POLICY IF EXISTS approval_stage_approver_scope ON approval_stage_approver;
--   DROP POLICY IF EXISTS approval_instance_scope ON approval_instance;
--   DROP POLICY IF EXISTS approval_stage_instance_scope ON approval_stage_instance;
--   DROP POLICY IF EXISTS approval_assignment_scope ON approval_assignment;
--   DROP POLICY IF EXISTS approval_action_scope ON approval_action;
--   ALTER TABLE approval_definition NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_definition DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_rule NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_rule DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage_approver NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage_approver DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_instance NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_instance DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage_instance NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_stage_instance DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_assignment NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_assignment DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE approval_action NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE approval_action DISABLE ROW LEVEL SECURITY;
--   DROP TRIGGER IF EXISTS approval_definition_immutable_update ON approval_definition;
--   DROP TRIGGER IF EXISTS approval_definition_immutable_delete ON approval_definition;
--   DROP TRIGGER IF EXISTS approval_rule_immutable_update ON approval_rule;
--   DROP TRIGGER IF EXISTS approval_rule_immutable_delete ON approval_rule;
--   DROP TRIGGER IF EXISTS approval_stage_immutable_update ON approval_stage;
--   DROP TRIGGER IF EXISTS approval_stage_immutable_delete ON approval_stage;
--   DROP TRIGGER IF EXISTS approval_stage_approver_immutable_update ON approval_stage_approver;
--   DROP TRIGGER IF EXISTS approval_stage_approver_immutable_delete ON approval_stage_approver;
--   DROP TRIGGER IF EXISTS approval_action_no_update ON approval_action;
--   DROP TRIGGER IF EXISTS approval_action_no_delete ON approval_action;
--   DROP TRIGGER IF EXISTS approval_instance_frozen_fields ON approval_instance;
--   DROP FUNCTION IF EXISTS assert_approval_definition_immutable() CASCADE;
--   DROP FUNCTION IF EXISTS assert_approval_definition_child_immutable() CASCADE;
--   DROP FUNCTION IF EXISTS assert_approval_action_append_only() CASCADE;
--   DROP FUNCTION IF EXISTS assert_approval_instance_frozen_fields() CASCADE;
--   DROP TABLE IF EXISTS approval_delegation, approval_action,
--     approval_assignment, approval_stage_instance, approval_instance,
--     approval_stage_approver, approval_stage, approval_rule,
--     approval_definition, reason_code CASCADE;
--   -- capex_app's table privileges disappear with the tables; the REVOKE
--   -- below needs no separate undo.

-- ============================================================== reason_code
-- The controlled vocabulary a decision may cite. Created first because
-- approval_action carries a foreign key to it: a reason_code recorded against
-- a decision must be a real, declared code, not free text pretending to be
-- one.
--
-- `applies_to_object_type` NULL means "any object type" -- a code such as
-- BUDGET_EXCEEDED is meaningful against a purchase requisition and a budget
-- revision alike, and forcing one row per object type would multiply the
-- vocabulary without adding meaning. `requires_free_text` is the per-code
-- switch behind the REASON_REQUIRED error in Contract 3: some codes are
-- self-explanatory, others (OTHER, in particular) are not.
--
-- `active` retires a code without deleting it. A deleted code would orphan
-- every historical action that cited it; retirement keeps the past readable
-- while removing the code from tomorrow's pick-list.
CREATE TABLE reason_code (
    code                    text PRIMARY KEY,
    applies_to_action       text NOT NULL,
    applies_to_object_type  text,
    label                   text NOT NULL,
    requires_free_text      boolean NOT NULL DEFAULT false,
    active                  boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              text NOT NULL,
    CONSTRAINT ck_reason_code_label_not_blank CHECK (btrim(label) <> ''),
    CONSTRAINT ck_reason_code_applies_to_action CHECK (
        applies_to_action IN ('APPROVE', 'REJECT', 'RETURN', 'RECALL',
                              'CANCEL', 'RESUBMIT', 'DELEGATE', 'ANY')
    )
);

CREATE INDEX ix_reason_code_action ON reason_code (applies_to_action)
    WHERE active;

-- ======================================================= approval_definition
-- One VERSION of one workflow, for one object type.
--
-- `version` is part of the identity, not a mutable attribute: Contract 1's
-- UNIQUE (object_type, code, version) is what makes "definition PR-STANDARD
-- v3" a nameable, permanent thing that an instance can point at forever.
-- `entity_id` is NULLABLE and means organisation-wide when NULL; see the RLS
-- section at the foot of this file for what that implies for visibility.
--
-- `effective_from` / `effective_to` bound when this version may be SELECTED
-- for routing. They do not retroactively invalidate instances already routed
-- under it -- an instance records `definition_version` itself, precisely so
-- that a workflow going out of effect never rewrites a decision taken while it
-- was in effect.
CREATE TABLE approval_definition (
    definition_id   text PRIMARY KEY,
    object_type     text NOT NULL,
    code            text NOT NULL,
    version         integer NOT NULL CHECK (version >= 1),
    status          text NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    entity_id       text REFERENCES entity (entity_id),
    effective_from  date NOT NULL,
    effective_to    date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    activated_at    timestamptz,
    activated_by    text,
    retired_at      timestamptz,
    retired_by      text,
    CONSTRAINT uq_approval_definition_version
        UNIQUE (object_type, code, version),
    CONSTRAINT ck_approval_definition_effective_range
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    -- A DRAFT has never been live, so it can carry no activation stamp; a
    -- RETIRED version must carry one, because it cannot have been retired
    -- without first having been active.
    CONSTRAINT ck_approval_definition_activation CHECK (
        (status = 'DRAFT' AND activated_at IS NULL AND activated_by IS NULL)
        OR (status <> 'DRAFT' AND activated_at IS NOT NULL
            AND activated_by IS NOT NULL)
    ),
    CONSTRAINT ck_approval_definition_retirement CHECK (
        (status = 'RETIRED' AND retired_at IS NOT NULL AND retired_by IS NOT NULL)
        OR (status <> 'RETIRED' AND retired_at IS NULL AND retired_by IS NULL)
    )
);

-- The routing lookup Contract 3's `GET /api/approvals/definitions` and the
-- engine's own definition selection both run: "the ACTIVE definitions for this
-- object type, in effect on this date".
CREATE INDEX ix_approval_definition_lookup
    ON approval_definition (object_type, status, effective_from, effective_to);
CREATE INDEX ix_approval_definition_code ON approval_definition (code, version);
CREATE INDEX ix_approval_definition_entity ON approval_definition (entity_id)
    WHERE entity_id IS NOT NULL;

-- ============================================================= approval_rule
-- Priority-ordered routing predicates. The engine evaluates a definition's
-- rules in `priority` order and takes the first whose `predicate` matches the
-- object; no match anywhere is EXCEPTION_PENDING with
-- APPROVAL_ROUTE_UNRESOLVED, never an auto-approval (Contract 2).
--
-- UNIQUE (definition_id, priority) is a determinism constraint, not
-- housekeeping: two rules sharing a priority would leave "which one matched"
-- decided by whatever order the planner happened to return rows in, and an
-- approval route that depends on a plan is not a control.
CREATE TABLE approval_rule (
    rule_id        text PRIMARY KEY,
    definition_id  text NOT NULL
                   REFERENCES approval_definition (definition_id) ON DELETE CASCADE,
    priority       integer NOT NULL CHECK (priority >= 0),
    predicate      jsonb NOT NULL,
    description    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    CONSTRAINT uq_approval_rule_priority UNIQUE (definition_id, priority)
);

CREATE INDEX ix_approval_rule_definition ON approval_rule (definition_id, priority);

-- ============================================================ approval_stage
-- One level of a definition.
--
-- `parallel_group` NULL means the stage runs alone, in `stage_no` order.
-- Stages sharing a non-NULL `parallel_group` open together and must all
-- resolve before the flow advances past the group -- the shape a "Procurement
-- and Engineering both sign off, in either order" step needs.
--
-- `quorum_type` and `quorum_n` are constrained TOGETHER by
-- `ck_approval_stage_quorum`, so an incoherent pair cannot be stored at all:
--   ALL / ANY   quorum_n MUST be NULL -- the count is implied by the type, and
--               a stray number beside it is an instruction nobody reads and
--               everybody later misreads.
--   N_OF_M      quorum_n IS the N: at least 1.
--   PERCENT     quorum_n is a percentage: 1..100. Not 0 (which would approve
--               with no approver at all) and not >100 (unsatisfiable, so the
--               stage could never close).
--
-- `applies_when` NULL means the stage always applies. A stage whose
-- `applies_when` evaluates false is still RECORDED, as a SKIPPED
-- approval_stage_instance carrying a skip_reason -- see that table.
CREATE TABLE approval_stage (
    stage_id              text PRIMARY KEY,
    definition_id         text NOT NULL
                          REFERENCES approval_definition (definition_id) ON DELETE CASCADE,
    stage_no              integer NOT NULL CHECK (stage_no >= 1),
    name                  text NOT NULL,
    parallel_group        text,
    quorum_type           text NOT NULL
                          CHECK (quorum_type IN ('ALL', 'ANY', 'N_OF_M', 'PERCENT')),
    quorum_n              integer,
    applies_when          jsonb,
    sla_hours             integer,
    escalate_after_hours  integer,
    escalate_to           jsonb,
    allow_delegation      boolean NOT NULL DEFAULT true,
    requires_reason       boolean NOT NULL DEFAULT false,
    reason_code_set       text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL,
    CONSTRAINT uq_approval_stage_stage_no UNIQUE (definition_id, stage_no),
    CONSTRAINT ck_approval_stage_quorum CHECK (
        (quorum_type IN ('ALL', 'ANY') AND quorum_n IS NULL)
        OR (quorum_type = 'N_OF_M' AND quorum_n IS NOT NULL AND quorum_n >= 1)
        OR (quorum_type = 'PERCENT' AND quorum_n IS NOT NULL
            AND quorum_n >= 1 AND quorum_n <= 100)
    ),
    CONSTRAINT ck_approval_stage_sla_hours
        CHECK (sla_hours IS NULL OR sla_hours > 0),
    CONSTRAINT ck_approval_stage_escalate_after_hours
        CHECK (escalate_after_hours IS NULL OR escalate_after_hours > 0),
    -- Escalating to nobody is not an escalation; it is a stage that quietly
    -- stops moving. If a timer is set, a destination must be set with it.
    CONSTRAINT ck_approval_stage_escalation_target CHECK (
        escalate_after_hours IS NULL OR escalate_to IS NOT NULL
    ),
    CONSTRAINT ck_approval_stage_name_not_blank CHECK (btrim(name) <> '')
);

CREATE INDEX ix_approval_stage_definition ON approval_stage (definition_id, stage_no);

-- =================================================== approval_stage_approver
-- The approver slots of a stage. `approver_kind` ROLE resolves through
-- role_grant at routing time; USER names an individual directly. `scope_expr`
-- narrows a ROLE slot ("the Plant Head OF THIS PROJECT'S PLANT", not every
-- Plant Head in the estate).
--
-- Whoever this resolves to, the engine then removes `contributor_set(object)`
-- -- maker, editors, prior actors -- from the result (Contract 5); an empty
-- set after that filter is EXCEPTION_PENDING with NO_INDEPENDENT_APPROVER, not
-- an approval. That filter is the engine's, in stream 2: this table stores the
-- configured slots, and cannot know who contributed to a document it has never
-- seen.
CREATE TABLE approval_stage_approver (
    stage_id       text NOT NULL
                   REFERENCES approval_stage (stage_id) ON DELETE CASCADE,
    ordinal        integer NOT NULL CHECK (ordinal >= 1),
    approver_kind  text NOT NULL CHECK (approver_kind IN ('ROLE', 'USER')),
    approver_ref   text NOT NULL,
    scope_expr     jsonb,
    PRIMARY KEY (stage_id, ordinal),
    CONSTRAINT ck_approval_stage_approver_ref_not_blank
        CHECK (btrim(approver_ref) <> '')
);

CREATE INDEX ix_approval_stage_approver_ref
    ON approval_stage_approver (approver_kind, approver_ref);

-- ========================================================= approval_instance
-- One routed document.
--
-- `object_type` / `object_id` are polymorphic (as audit_log's are) and carry
-- no foreign key: the engine routes purchase requisitions, budget revisions
-- and capitalisation proposals alike, and a column cannot reference ten tables
-- at once.
--
-- `entity_id` and `project_id` are DENORMALISED at creation (Contract 6),
-- which is what makes this table scopable without a join through a
-- polymorphic reference that no index and no RLS predicate could follow.
-- `project_id` is nullable because not every approvable object belongs to a
-- project; a NULL there waives the project dimension for that row, exactly as
-- `capex_dimension_permits` waives a SQL NULL, so such a row is visible to any
-- caller whose ENTITY permits it. That is the documented waiver semantics of
-- this codebase, not a hole: a caller who cannot see the entity sees nothing.
--
-- `object_version` and `object_content_sha` are what Contract 8's staleness
-- check compares against, and `snapshot` is the document as it stood when it
-- was routed. All three, plus `definition_version`, are frozen by trigger --
-- see `assert_approval_instance_frozen_fields`.
CREATE TABLE approval_instance (
    instance_id             text PRIMARY KEY,
    object_type             text NOT NULL,
    object_id               text NOT NULL,
    object_version          integer NOT NULL CHECK (object_version >= 1),
    object_content_sha      text NOT NULL,
    definition_id           text REFERENCES approval_definition (definition_id),
    definition_version      integer,
    status                  text NOT NULL DEFAULT 'OPEN' CHECK (status IN (
                                'OPEN', 'APPROVED', 'REJECTED', 'RETURNED',
                                'RECALLED', 'CANCELLED', 'SUPERSEDED',
                                'EXCEPTION_PENDING')),
    current_stage_no        integer,
    snapshot                jsonb NOT NULL,
    supersedes_instance_id  text REFERENCES approval_instance (instance_id),
    maker_user_id           text NOT NULL REFERENCES app_user (user_id),
    entity_id               text NOT NULL REFERENCES entity (entity_id),
    project_id              text REFERENCES project (project_id),
    opened_at               timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,
    correlation_id          text,
    -- NO ROUTE IS NEVER AN APPROVAL. An instance that resolved no definition
    -- carries neither definition_id nor definition_version, and is pinned to
    -- EXCEPTION_PENDING. This is Contract 2's "no route to auto-approval" made
    -- unrepresentable rather than merely unlikely: there is no UPDATE that can
    -- walk an unrouted instance to APPROVED without first giving it a
    -- definition to have been approved under.
    CONSTRAINT ck_approval_instance_definition CHECK (
        (definition_id IS NULL AND definition_version IS NULL
         AND status = 'EXCEPTION_PENDING')
        OR (definition_id IS NOT NULL AND definition_version IS NOT NULL)
    ),
    CONSTRAINT ck_approval_instance_open_is_not_closed
        CHECK (status <> 'OPEN' OR closed_at IS NULL),
    CONSTRAINT ck_approval_instance_no_self_supersede CHECK (
        supersedes_instance_id IS NULL OR supersedes_instance_id <> instance_id
    ),
    CONSTRAINT ck_approval_instance_content_sha_not_blank
        CHECK (btrim(object_content_sha) <> '')
);

-- "Is there an open instance for this document?" -- asked on every submit,
-- every edit and every decision, and the join key for the timeline endpoint.
CREATE INDEX ix_approval_instance_object
    ON approval_instance (object_type, object_id);
-- The narrower form the staleness/supersede path actually runs: only the
-- instances still in flight for an object.
CREATE INDEX ix_approval_instance_object_open
    ON approval_instance (object_type, object_id)
    WHERE status IN ('OPEN', 'EXCEPTION_PENDING');
CREATE INDEX ix_approval_instance_status ON approval_instance (status);
CREATE INDEX ix_approval_instance_maker ON approval_instance (maker_user_id);
CREATE INDEX ix_approval_instance_scope ON approval_instance (entity_id, project_id);
CREATE INDEX ix_approval_instance_definition ON approval_instance (definition_id)
    WHERE definition_id IS NOT NULL;
CREATE INDEX ix_approval_instance_correlation ON approval_instance (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- =================================================== approval_stage_instance
-- One stage of one instance -- INCLUDING the stages that did not run.
--
-- `ck_approval_stage_instance_skip_reason` is Contract 2's audit rule as a
-- constraint: a SKIPPED stage must say why it was skipped, and a stage that
-- ran must not carry a skip reason it never had. "Never omitted, because an
-- auditor must see what did not run" is only true if the row cannot be stored
-- without its explanation.
--
-- `quorum_required` is resolved from the stage's quorum_type/quorum_n against
-- the ACTUAL assignee count at open time (an N_OF_M stage whose approver set
-- resolved to fewer than N is a configuration defect the engine must catch,
-- not something this table can decide). `quorum_met` counts approvals so far.
CREATE TABLE approval_stage_instance (
    stage_instance_id  text PRIMARY KEY,
    instance_id        text NOT NULL
                       REFERENCES approval_instance (instance_id) ON DELETE CASCADE,
    stage_no           integer NOT NULL CHECK (stage_no >= 1),
    parallel_group     text,
    status             text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
                           'PENDING', 'APPROVED', 'REJECTED', 'RETURNED',
                           'SKIPPED', 'ESCALATED')),
    skip_reason        text,
    quorum_required    integer NOT NULL CHECK (quorum_required >= 0),
    quorum_met         integer NOT NULL DEFAULT 0 CHECK (quorum_met >= 0),
    opened_at          timestamptz NOT NULL DEFAULT now(),
    due_at             timestamptz,
    escalated_at       timestamptz,
    closed_at          timestamptz,
    CONSTRAINT uq_approval_stage_instance_stage_no UNIQUE (instance_id, stage_no),
    CONSTRAINT ck_approval_stage_instance_skip_reason CHECK (
        (status = 'SKIPPED' AND skip_reason IS NOT NULL AND btrim(skip_reason) <> '')
        OR (status <> 'SKIPPED' AND skip_reason IS NULL)
    ),
    CONSTRAINT ck_approval_stage_instance_pending_is_not_closed
        CHECK (status <> 'PENDING' OR closed_at IS NULL),
    CONSTRAINT ck_approval_stage_instance_escalated_at CHECK (
        status <> 'ESCALATED' OR escalated_at IS NOT NULL
    )
);

-- The overdue-SLA sweep behind `GET /api/approvals/sla?overdue=true`: only
-- stages still PENDING can be overdue, and only those carrying a due_at have
-- an SLA at all, so the index carries neither of the other cases.
CREATE INDEX ix_approval_stage_instance_overdue
    ON approval_stage_instance (due_at)
    WHERE status = 'PENDING' AND due_at IS NOT NULL;
CREATE INDEX ix_approval_stage_instance_instance
    ON approval_stage_instance (instance_id, stage_no);
CREATE INDEX ix_approval_stage_instance_group
    ON approval_stage_instance (instance_id, parallel_group)
    WHERE parallel_group IS NOT NULL;

-- ======================================================= approval_assignment
-- One addressable approver on one stage instance.
--
-- UNIQUE (stage_instance_id, assignee_user_id) is what stops one person from
-- occupying two seats at the same stage. Without it a user reachable both
-- directly and through a role -- or through two roles -- would be assigned
-- twice and could satisfy a 2-of-3 quorum alone. A quorum that one person can
-- meet twice is not a quorum.
--
-- `delegated_from` and `assigned_via` are constrained to agree: a DELEGATION
-- assignment must name who it came from, and a non-delegated assignment must
-- not claim a delegator it never had.
CREATE TABLE approval_assignment (
    assignment_id      text PRIMARY KEY,
    stage_instance_id  text NOT NULL
                       REFERENCES approval_stage_instance (stage_instance_id)
                       ON DELETE CASCADE,
    assignee_user_id   text NOT NULL REFERENCES app_user (user_id),
    assigned_via       text NOT NULL CHECK (assigned_via IN (
                           'ROLE', 'USER', 'DELEGATION', 'ESCALATION')),
    delegated_from     text REFERENCES app_user (user_id),
    state              text NOT NULL DEFAULT 'PENDING'
                       CHECK (state IN ('PENDING', 'ACTED', 'WITHDRAWN')),
    assigned_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_approval_assignment_assignee
        UNIQUE (stage_instance_id, assignee_user_id),
    CONSTRAINT ck_approval_assignment_delegated_from CHECK (
        (assigned_via = 'DELEGATION') = (delegated_from IS NOT NULL)
    ),
    -- Delegating to oneself is a no-op that would appear in the audit trail as
    -- a transfer of authority. There was none.
    CONSTRAINT ck_approval_assignment_not_self_delegated CHECK (
        delegated_from IS NULL OR delegated_from <> assignee_user_id
    )
);

-- THE INBOX. `GET /api/approvals/inbox?state=` is the single most-run query in
-- this module: "everything addressed to me, in this state". Assignee first,
-- state second -- the leading column is the one every call filters on.
CREATE INDEX ix_approval_assignment_inbox
    ON approval_assignment (assignee_user_id, state);
CREATE INDEX ix_approval_assignment_stage_instance
    ON approval_assignment (stage_instance_id);
CREATE INDEX ix_approval_assignment_delegated_from
    ON approval_assignment (delegated_from)
    WHERE delegated_from IS NOT NULL;

-- =========================================================== approval_action
-- APPEND-ONLY. The decision log, hash-chained per instance on stream
-- `approval:{instance_id}` using the frozen payload format
-- `prev|at|actor|action|type|id|detail` (Contract 9) -- unchanged, because
-- changing it invalidates every hash already stored.
--
-- `action_id` is NOT the chain order, for exactly the reason 001_foundation's
-- audit_log says so: identity values are assigned before commit and can commit
-- out of order. `seq`, taken under the advisory lock that Contract 7 puts LAST
-- in the lock order, is the order -- and UNIQUE (instance_id, seq) is what
-- makes a gap or a duplicate in a chain a constraint violation rather than a
-- discovery made months later by an auditor.
--
-- `acting_for_user_id` is the delegation's second identity. Contract 5 refuses
-- a decision if EITHER `actor_user_id` OR `acting_for_user_id` is a
-- contributor, so a delegation can never launder a self-approval; both
-- identities are recorded here so that check is auditable after the fact and
-- not merely asserted at the time.
--
-- `stage_instance_id` is nullable: RECALL and CANCEL act on the instance as a
-- whole, at no particular stage.
CREATE TABLE approval_action (
    action_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage_instance_id   text REFERENCES approval_stage_instance (stage_instance_id),
    instance_id         text NOT NULL REFERENCES approval_instance (instance_id),
    actor_user_id       text NOT NULL REFERENCES app_user (user_id),
    acting_for_user_id  text REFERENCES app_user (user_id),
    action              text NOT NULL,
    reason_code         text REFERENCES reason_code (code),
    reason_text         text,
    at                  timestamptz NOT NULL DEFAULT now(),
    seq                 bigint NOT NULL,
    prev_hash           text,
    entry_hash          text,
    -- Contract 8, added by lead amendment A3. The contract required a
    -- decision to carry an idempotency key and a replay to return the
    -- ORIGINAL outcome, and declared nowhere to store either.
    --
    -- NULLable on purpose: not every action is a caller decision. An
    -- escalation or a system-generated supersede carries no key, so the
    -- uniqueness below is a PARTIAL index -- a plain UNIQUE would let exactly
    -- one keyless action exist per instance.
    idempotency_key     text,
    -- The stored original outcome a replay returns verbatim. Also where the
    -- action's narrative lives.
    --
    -- There is deliberately NO `detail` column, though Contract 9's frozen
    -- payload names one. The hashed detail is built from STORED columns only
    -- (stage_instance_id, reason_code, reason_text), so verification
    -- genuinely recomputes each digest from the row it is checking. Hashing a
    -- narrative that is not itself stored yields a chain that can only check
    -- prev_hash links -- and a rewrite of REJECT to APPROVE would pass
    -- verification untouched, on the table recording approval decisions.
    outcome             jsonb,
    CONSTRAINT uq_approval_action_seq UNIQUE (instance_id, seq),
    CONSTRAINT ck_approval_action_seq_positive CHECK (seq >= 1),
    -- Acting for oneself is not delegation; recording it as such would put a
    -- transfer of authority in the audit trail that never happened.
    CONSTRAINT ck_approval_action_acting_for_is_someone_else CHECK (
        acting_for_user_id IS NULL OR acting_for_user_id <> actor_user_id
    )
);

CREATE INDEX ix_approval_action_instance ON approval_action (instance_id, seq);
CREATE INDEX ix_approval_action_stage_instance
    ON approval_action (stage_instance_id)
    WHERE stage_instance_id IS NOT NULL;
CREATE INDEX ix_approval_action_actor ON approval_action (actor_user_id, at DESC);

-- ======================================================= approval_delegation
-- "X may act for Y, over this date range, within this scope."
--
-- `active_range` is a daterange and `ck_approval_delegation_range_not_empty`
-- refuses an EMPTY one. PostgreSQL normalises `[d, d)` to the empty range, so
-- without this check the most natural way to typo a one-day delegation stores
-- a row that looks like a grant, reads like a grant, and authorises nothing --
-- silently, for as long as anybody relies on it. `isempty()` catches both that
-- and a literal 'empty'.
--
-- Revocation is a stamp, not a delete: `revoked_at` plus a mandatory
-- `revoke_reason`. Deleting the row would erase the fact that authority was
-- ever transferred, which is precisely the fact an auditor needs after a
-- decision taken under it.
-- Idempotency lookup: the engine reads (instance_id, idempotency_key)
-- BEFORE taking any lock, so a replay never contends. Partial, because a
-- keyless action is legitimate (see the column comment).
CREATE UNIQUE INDEX ux_approval_action_idempotency
    ON approval_action (instance_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;


CREATE TABLE approval_delegation (
    delegation_id      text PRIMARY KEY,
    delegator_user_id  text NOT NULL REFERENCES app_user (user_id),
    delegate_user_id   text NOT NULL REFERENCES app_user (user_id),
    scope_key          text NOT NULL,
    active_range       daterange NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text NOT NULL,
    revoked_at         timestamptz,
    revoke_reason      text,
    CONSTRAINT ck_approval_delegation_range_not_empty
        CHECK (NOT isempty(active_range)),
    CONSTRAINT ck_approval_delegation_distinct_parties
        CHECK (delegator_user_id <> delegate_user_id),
    CONSTRAINT ck_approval_delegation_revocation CHECK (
        (revoked_at IS NULL AND revoke_reason IS NULL)
        OR (revoked_at IS NOT NULL AND btrim(coalesce(revoke_reason, '')) <> '')
    ),
    CONSTRAINT ck_approval_delegation_scope_key_not_blank
        CHECK (btrim(scope_key) <> '')
);

CREATE INDEX ix_approval_delegation_delegate
    ON approval_delegation (delegate_user_id, scope_key)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_approval_delegation_delegator
    ON approval_delegation (delegator_user_id, scope_key)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_approval_delegation_range
    ON approval_delegation USING gist (active_range);

-- ============================================ immutability 1: live workflows
-- An ACTIVE or RETIRED approval_definition, and everything beneath it, stops
-- being editable. Editing a live workflow retroactively rewrites decisions
-- already taken under it: the stage that approved a document would no longer
-- be the stage that is recorded as having approved it. A CHECK constraint
-- cannot see OLD, so this is a trigger.
--
-- A DRAFT is deliberately NOT covered. The WHEN clauses below fire only for
-- OLD.status IN ('ACTIVE','RETIRED'), so a DRAFT definition and its children
-- remain ordinary, freely editable rows -- which is the entire purpose of
-- having a DRAFT status.
--
-- The single permitted change to a live definition is its RETIREMENT: status
-- ACTIVE -> RETIRED, touching only `status`, `retired_at` and `retired_by`.
-- See this file's header for why refusing even that would contradict
-- Contract 1's own prescribed change mechanism ("the old row is retired").
-- RETIRED is terminal; DELETE is refused for both statuses unconditionally.
CREATE OR REPLACE FUNCTION assert_approval_definition_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'approval_definition % is % and is immutable: DELETE denied. Supersede it with a new version.',
            OLD.definition_id, OLD.status
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF OLD.status = 'RETIRED' THEN
        RAISE EXCEPTION
            'approval_definition % is RETIRED and is immutable: UPDATE denied. Retirement is terminal.',
            OLD.definition_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- OLD.status = 'ACTIVE' from here on.
    IF NEW.status IS DISTINCT FROM 'RETIRED' THEN
        RAISE EXCEPTION
            'approval_definition % is ACTIVE and is immutable: UPDATE denied. The only permitted change is retirement (status -> RETIRED); any other change means a new version.',
            OLD.definition_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.definition_id     IS DISTINCT FROM OLD.definition_id
       OR NEW.object_type    IS DISTINCT FROM OLD.object_type
       OR NEW.code           IS DISTINCT FROM OLD.code
       OR NEW.version        IS DISTINCT FROM OLD.version
       OR NEW.entity_id      IS DISTINCT FROM OLD.entity_id
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.effective_to   IS DISTINCT FROM OLD.effective_to
       OR NEW.created_at     IS DISTINCT FROM OLD.created_at
       OR NEW.created_by     IS DISTINCT FROM OLD.created_by
       OR NEW.activated_at   IS DISTINCT FROM OLD.activated_at
       OR NEW.activated_by   IS DISTINCT FROM OLD.activated_by
    THEN
        RAISE EXCEPTION
            'approval_definition % is ACTIVE: retirement may change only status, retired_at and retired_by; every other column is immutable. UPDATE denied.',
            OLD.definition_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_definition_immutable_update BEFORE UPDATE ON approval_definition
    FOR EACH ROW WHEN (OLD.status IN ('ACTIVE', 'RETIRED'))
    EXECUTE FUNCTION assert_approval_definition_immutable();
CREATE TRIGGER approval_definition_immutable_delete BEFORE DELETE ON approval_definition
    FOR EACH ROW WHEN (OLD.status IN ('ACTIVE', 'RETIRED'))
    EXECUTE FUNCTION assert_approval_definition_immutable();

-- The children have no lifecycle of their own: a rule, a stage and a stage
-- approver are parts of the definition, so their mutability is entirely their
-- parent's. One function serves all three tables, reading the parent id out of
-- `to_jsonb(OLD)` rather than naming a column that only two of the three carry
-- -- approval_stage_approver reaches its definition through `stage_id`.
--
-- A parent that cannot be found is PERMITTED, and this is the one branch worth
-- justifying. It is reached only by an ON DELETE CASCADE from the parent, and
-- a cascade can only have started from a DRAFT parent: the parent's own BEFORE
-- DELETE trigger above fires before the referential action runs and refuses
-- outright for ACTIVE and RETIRED. So "parent gone" here means "a DRAFT
-- definition is being deleted", which is exactly the case that should succeed.
CREATE OR REPLACE FUNCTION assert_approval_definition_child_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_row      jsonb := to_jsonb(OLD);
    v_definition text;
    v_status     text;
BEGIN
    -- `jsonb_exists(...)` rather than the `?` operator, deliberately: `?` is a
    -- parameter placeholder in several drivers, and this file is executed as a
    -- single string by psycopg. The function form is the same test with no
    -- character that any driver might try to bind.
    IF jsonb_exists(old_row, 'definition_id') THEN
        v_definition := old_row ->> 'definition_id';
    ELSE
        SELECT s.definition_id INTO v_definition
          FROM approval_stage s
         WHERE s.stage_id = old_row ->> 'stage_id';
    END IF;

    SELECT d.status INTO v_status
      FROM approval_definition d
     WHERE d.definition_id = v_definition;

    IF v_status IN ('ACTIVE', 'RETIRED') THEN
        RAISE EXCEPTION
            '% belongs to approval_definition %, which is %: % denied. A live workflow is immutable; change means a new version.',
            TG_TABLE_NAME, v_definition, v_status, TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_rule_immutable_update BEFORE UPDATE ON approval_rule
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();
CREATE TRIGGER approval_rule_immutable_delete BEFORE DELETE ON approval_rule
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();
CREATE TRIGGER approval_stage_immutable_update BEFORE UPDATE ON approval_stage
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();
CREATE TRIGGER approval_stage_immutable_delete BEFORE DELETE ON approval_stage
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();
CREATE TRIGGER approval_stage_approver_immutable_update BEFORE UPDATE ON approval_stage_approver
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();
CREATE TRIGGER approval_stage_approver_immutable_delete BEFORE DELETE ON approval_stage_approver
    FOR EACH ROW EXECUTE FUNCTION assert_approval_definition_child_immutable();

-- ======================================== immutability 2: the decision log
-- approval_action is append-only, and gets exactly the treatment audit_log
-- gets in 001_foundation.sql: a trigger AND a REVOKE. The privilege is the
-- real guarantee; the trigger states the intent in the schema and catches a
-- misconfigured grant. Neither alone is enough -- a future migration that
-- re-runs a blanket `GRANT ... ON ALL TABLES` would silently undo the REVOKE,
-- and a superuser bypasses privileges but not triggers.
--
-- A separate function from 001's `assert_append_only()`, deliberately: that
-- one raises a message naming audit_log, and an operator debugging a refused
-- write on approval_action should not be told the wrong table is append-only.
CREATE OR REPLACE FUNCTION assert_approval_action_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'approval_action is append-only: % denied', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER approval_action_no_update BEFORE UPDATE ON approval_action
    FOR EACH ROW EXECUTE FUNCTION assert_approval_action_append_only();
CREATE TRIGGER approval_action_no_delete BEFORE DELETE ON approval_action
    FOR EACH ROW EXECUTE FUNCTION assert_approval_action_append_only();

-- =================================== immutability 3: what an instance froze
-- `snapshot`, `definition_version` and `object_content_sha` record the
-- workflow an instance was routed UNDER and the document it was routed FOR.
-- If either could drift, an approval would stop being evidence of what was
-- actually approved -- the document could be edited under an open instance and
-- the instance would still read as having approved the new text.
--
-- Column-scoped, not table-wide: `status`, `current_stage_no`, `closed_at` and
-- the rest must remain updatable, because advancing an instance through its
-- own lifecycle is the engine's whole job. The WHEN clause is what draws that
-- line, so an ordinary state transition never even calls the function.
CREATE OR REPLACE FUNCTION assert_approval_instance_frozen_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'approval_instance %: snapshot, definition_version and object_content_sha are frozen at creation: UPDATE denied. A document that changed under an open instance is SUPERSEDED and re-routed, never rewritten in place.',
        OLD.instance_id
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER approval_instance_frozen_fields BEFORE UPDATE ON approval_instance
    FOR EACH ROW WHEN (
        OLD.snapshot              IS DISTINCT FROM NEW.snapshot
        OR OLD.definition_version IS DISTINCT FROM NEW.definition_version
        OR OLD.object_content_sha IS DISTINCT FROM NEW.object_content_sha)
    EXECUTE FUNCTION assert_approval_instance_frozen_fields();

-- ================================================== privileges for capex_app
-- 004_identity_scope.sql set ALTER DEFAULT PRIVILEGES for the role that runs
-- migrations, so tables created here normally reach capex_app already. These
-- grants are stated explicitly anyway: default privileges attach to the role
-- that issued them, and a deployment whose 008 is applied by a different
-- identity than its 004 would otherwise leave the application unable to read
-- its own new tables -- a failure that shows up only in that deployment.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    reason_code, approval_definition, approval_rule, approval_stage,
    approval_stage_approver, approval_instance, approval_stage_instance,
    approval_assignment, approval_action, approval_delegation
    TO capex_app;

-- ...and immediately narrowed back down for the append-only table, in the same
-- migration rather than trusting every future caller to remember. This mirrors
-- 004's `REVOKE UPDATE, DELETE ON audit_log, audit_anchor FROM capex_app`
-- exactly.
REVOKE UPDATE, DELETE ON approval_action FROM capex_app;

-- ================================================================ RLS
-- Every policy calls the frozen `capex_scope_permits` from 004, whose body 007
-- replaced with the mode/ids wire format. An absent session setting denies:
-- a connection that never went through `Database.session()` reads nothing.
--
-- ENABLE *and* FORCE on every table. FORCE is the half most easily dropped and
-- its absence is invisible in `pg_policies`: without it the table's OWNER --
-- in production the deploy identity, not a superuser -- bypasses every policy
-- silently. USING *and* WITH CHECK on every policy: USING alone filters reads
-- while leaving a caller free to INSERT a row into a scope they cannot see.

-- ------------------------------------------- configuration: the definition
-- `entity_id` is nullable and that nullability is the feature: a NULL means
-- the definition applies organisation-wide, and `capex_dimension_permits`
-- treats a SQL NULL as "this row does not carry that dimension" -- waived, so
-- every principal sees it. A non-NULL entity_id confines the definition to
-- that entity. plant/location/project are waived because the table has no
-- column for any of them, the same waiver `entity_scope` uses in 004.
ALTER TABLE approval_definition ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_definition FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_definition_scope ON approval_definition
    USING      (capex_scope_permits(entity_id, NULL, NULL, NULL))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, NULL));

-- ------------------------------ configuration: rules, stages, approver slots
-- No dimension column of their own; each reaches one through the definition
-- they belong to, in the shape `budget_line_scope` uses in 006. A row whose
-- parent cannot be found is DENIED rather than permitted -- `EXISTS` is false,
-- so the row is invisible. Fail closed.
--
-- Scoping the parent but not the children would be the incoherent option: the
-- thresholds in a rule's predicate and the named approvers in a stage are the
-- sensitive part of a workflow, not the definition header that points at them.
ALTER TABLE approval_rule ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_rule FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_rule_scope ON approval_rule
    USING (
        EXISTS (
            SELECT 1 FROM approval_definition d
            WHERE d.definition_id = approval_rule.definition_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_definition d
            WHERE d.definition_id = approval_rule.definition_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    );

ALTER TABLE approval_stage ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_stage FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_stage_scope ON approval_stage
    USING (
        EXISTS (
            SELECT 1 FROM approval_definition d
            WHERE d.definition_id = approval_stage.definition_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_definition d
            WHERE d.definition_id = approval_stage.definition_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    );

-- Two joins out: stage_approver -> stage -> definition.
ALTER TABLE approval_stage_approver ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_stage_approver FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_stage_approver_scope ON approval_stage_approver
    USING (
        EXISTS (
            SELECT 1 FROM approval_stage s
            JOIN approval_definition d ON d.definition_id = s.definition_id
            WHERE s.stage_id = approval_stage_approver.stage_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_stage s
            JOIN approval_definition d ON d.definition_id = s.definition_id
            WHERE s.stage_id = approval_stage_approver.stage_id
              AND capex_scope_permits(d.entity_id, NULL, NULL, NULL)
        )
    );

-- ------------------------------------------------ the instance, and its tree
-- Contract 6: approval_instance carries entity_id and project_id denormalised
-- at creation, so it is scopable DIRECTLY -- no join, which matters because
-- object_type/object_id are polymorphic and no predicate could follow them.
-- plant and location are waived: the table has no column for either, and both
-- are already enforced at `project`, which carries its own policy.
ALTER TABLE approval_instance ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_instance FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_instance_scope ON approval_instance
    USING      (capex_scope_permits(entity_id, NULL, NULL, project_id))
    WITH CHECK (capex_scope_permits(entity_id, NULL, NULL, project_id));

-- The three child tables reach both dimensions through the instance, so they
-- inherit its filter exactly rather than re-deriving it. An orphaned child --
-- one whose instance_id matches nothing -- is denied, not waived.
ALTER TABLE approval_stage_instance ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_stage_instance FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_stage_instance_scope ON approval_stage_instance
    USING (
        EXISTS (
            SELECT 1 FROM approval_instance ai
            WHERE ai.instance_id = approval_stage_instance.instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_instance ai
            WHERE ai.instance_id = approval_stage_instance.instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    );

-- Two joins out: assignment -> stage_instance -> instance. Note this is a
-- SCOPE filter, not the addressing filter: Contract 6 also requires a caller
-- to see only assignments addressed to them unless they hold
-- approval.configure, and that is an application-layer rule in stream 3's
-- inbox query, not something RLS can express (RLS cannot know what permissions
-- a principal holds). The two are complementary, and neither substitutes for
-- the other.
ALTER TABLE approval_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_assignment FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_assignment_scope ON approval_assignment
    USING (
        EXISTS (
            SELECT 1 FROM approval_stage_instance si
            JOIN approval_instance ai ON ai.instance_id = si.instance_id
            WHERE si.stage_instance_id = approval_assignment.stage_instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_stage_instance si
            JOIN approval_instance ai ON ai.instance_id = si.instance_id
            WHERE si.stage_instance_id = approval_assignment.stage_instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    );

-- approval_action carries instance_id directly, so it reaches the dimensions
-- in one join even though it also carries a stage_instance_id (which is NULL
-- for RECALL and CANCEL -- joining through THAT would make exactly those two
-- action kinds invisible).
ALTER TABLE approval_action ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_action FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_action_scope ON approval_action
    USING (
        EXISTS (
            SELECT 1 FROM approval_instance ai
            WHERE ai.instance_id = approval_action.instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM approval_instance ai
            WHERE ai.instance_id = approval_action.instance_id
              AND capex_scope_permits(ai.entity_id, NULL, NULL, ai.project_id)
        )
    );
