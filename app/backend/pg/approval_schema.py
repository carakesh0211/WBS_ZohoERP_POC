"""Names, statuses and columns of the approval engine's schema, as constants.

**Why this module exists.** ``migrations/pg/008_approval_engine.sql`` is the
only place the approval schema is defined, and four streams build on it at
once. Every one of them needs to name a table, a column and a status value in
Python, and every hand-typed ``"approval_stage_instance"`` is a typo waiting to
become a runtime error that no test catches until the query actually runs.
Streams 2 (engine), 3 (API) and 5 (end-to-end tests) import from here instead,
so there is exactly one spelling of every name in the codebase.

**What it is not.** This is a transcription of the migration, not a second
source of truth and not an ORM. It defines no behaviour, opens no connection
and builds no SQL. When the migration and this module disagree, the migration
is right and this module is broken --
``tests/test_pg_approval_schema.py::test_approval_schema_matches_the_migration``
reads the migration text and fails on any drift in either direction, so the two
cannot quietly diverge.

**Statuses are internal.** Contract 2 of ``docs/WAVE4_CONTRACTS.md`` is
explicit: the tuples below are the engine's own vocabulary
(``C15_approval_statuses.json``) and never render on a business screen except
through a mapping to ``C3_statuses.json``. Importing :data:`INSTANCE_STATUSES`
to build a user-facing filter chip would leak the internal vocabulary into the
UI; that mapping belongs to stream 4.
"""
from __future__ import annotations

# ============================================================== table names
#: The controlled vocabulary a decision may cite.
REASON_CODE = "reason_code"
#: One VERSION of one workflow.
APPROVAL_DEFINITION = "approval_definition"
#: Priority-ordered routing predicates beneath a definition.
APPROVAL_RULE = "approval_rule"
#: One level of a definition.
APPROVAL_STAGE = "approval_stage"
#: The approver slots of a stage.
APPROVAL_STAGE_APPROVER = "approval_stage_approver"
#: One routed document.
APPROVAL_INSTANCE = "approval_instance"
#: One stage of one instance -- including the stages that did not run.
APPROVAL_STAGE_INSTANCE = "approval_stage_instance"
#: One addressable approver on one stage instance.
APPROVAL_ASSIGNMENT = "approval_assignment"
#: The append-only, hash-chained decision log.
APPROVAL_ACTION = "approval_action"
#: "X may act for Y over this date range."
APPROVAL_DELEGATION = "approval_delegation"

#: Every table ``008_approval_engine.sql`` creates, in creation order --
#: which is also an order safe to insert in and the reverse of a safe delete
#: order (``reason_code`` first because ``approval_action`` references it).
APPROVAL_TABLES: tuple[str, ...] = (
    REASON_CODE,
    APPROVAL_DEFINITION,
    APPROVAL_RULE,
    APPROVAL_STAGE,
    APPROVAL_STAGE_APPROVER,
    APPROVAL_INSTANCE,
    APPROVAL_STAGE_INSTANCE,
    APPROVAL_ASSIGNMENT,
    APPROVAL_ACTION,
    APPROVAL_DELEGATION,
)

#: The migration that creates all of the above. Named as a constant because
#: the scope inventory and the schema test both have to agree on the spelling.
APPROVAL_MIGRATION = "008_approval_engine.sql"

# ================================================================= statuses
# Contract 2, frozen. The database CHECK constraints in 008 carry exactly
# these values; the schema test proves it rather than assuming it.

#: ``approval_definition.status``. A DRAFT is freely editable; ACTIVE and
#: RETIRED are immutable by trigger, save for the one ACTIVE -> RETIRED
#: transition that retirement itself requires.
DEFINITION_STATUSES: tuple[str, ...] = ("DRAFT", "ACTIVE", "RETIRED")

#: ``approval_instance.status``.
INSTANCE_STATUSES: tuple[str, ...] = (
    "OPEN", "APPROVED", "REJECTED", "RETURNED", "RECALLED", "CANCELLED",
    "SUPERSEDED", "EXCEPTION_PENDING",
)

#: ``approval_stage_instance.status``. A stage whose ``applies_when`` is false
#: is recorded ``SKIPPED`` with a ``skip_reason`` -- never omitted, because an
#: auditor must see what did not run.
STAGE_STATUSES: tuple[str, ...] = (
    "PENDING", "APPROVED", "REJECTED", "RETURNED", "SKIPPED", "ESCALATED",
)

#: ``approval_assignment.state``.
ASSIGNMENT_STATES: tuple[str, ...] = ("PENDING", "ACTED", "WITHDRAWN")

#: ``approval_stage.quorum_type``. ``quorum_n`` must be NULL for ALL and ANY,
#: >= 1 for N_OF_M, and 1..100 for PERCENT -- enforced by
#: ``ck_approval_stage_quorum``, not by the caller.
QUORUM_TYPES: tuple[str, ...] = ("ALL", "ANY", "N_OF_M", "PERCENT")

#: ``approval_stage_approver.approver_kind``.
APPROVER_KINDS: tuple[str, ...] = ("ROLE", "USER")

#: ``approval_assignment.assigned_via``. ``DELEGATION`` is the only value that
#: may carry a ``delegated_from``, and it must.
ASSIGNED_VIA: tuple[str, ...] = ("ROLE", "USER", "DELEGATION", "ESCALATION")

#: ``reason_code.applies_to_action``. ``ANY`` means the code is valid against
#: every action kind.
REASON_CODE_ACTIONS: tuple[str, ...] = (
    "APPROVE", "REJECT", "RETURN", "RECALL", "CANCEL", "RESUBMIT",
    "DELEGATE", "ANY",
)

#: Instance statuses that are still in flight. The engine looks for one of
#: these when deciding whether a document already has an open approval.
OPEN_INSTANCE_STATUSES: tuple[str, ...] = ("OPEN", "EXCEPTION_PENDING")

# ================================================================== columns
#: Every column of every table, in declaration order. Hand-transcribed from
#: the migration's ``CREATE TABLE`` statements; the schema test compares this
#: mapping against the migration text in both directions, so a column added to
#: one and not the other is a test failure rather than a surprise.
COLUMNS: dict[str, tuple[str, ...]] = {
    REASON_CODE: (
        "code", "applies_to_action", "applies_to_object_type", "label",
        "requires_free_text", "active", "created_at", "created_by",
    ),
    APPROVAL_DEFINITION: (
        "definition_id", "object_type", "code", "version", "status",
        "entity_id", "effective_from", "effective_to", "created_at",
        "created_by", "activated_at", "activated_by", "retired_at",
        "retired_by",
    ),
    APPROVAL_RULE: (
        "rule_id", "definition_id", "priority", "predicate", "description",
        "created_at", "created_by",
    ),
    APPROVAL_STAGE: (
        "stage_id", "definition_id", "stage_no", "name", "parallel_group",
        "quorum_type", "quorum_n", "applies_when", "sla_hours",
        "escalate_after_hours", "escalate_to", "allow_delegation",
        "requires_reason", "reason_code_set", "created_at", "created_by",
    ),
    APPROVAL_STAGE_APPROVER: (
        "stage_id", "ordinal", "approver_kind", "approver_ref", "scope_expr",
    ),
    APPROVAL_INSTANCE: (
        "instance_id", "object_type", "object_id", "object_version",
        "object_content_sha", "definition_id", "definition_version",
        "status", "current_stage_no", "snapshot", "supersedes_instance_id",
        "maker_user_id", "entity_id", "project_id", "opened_at",
        "closed_at", "correlation_id",
    ),
    APPROVAL_STAGE_INSTANCE: (
        "stage_instance_id", "instance_id", "stage_no", "parallel_group",
        "status", "skip_reason", "quorum_required", "quorum_met",
        "opened_at", "due_at", "escalated_at", "closed_at",
    ),
    APPROVAL_ASSIGNMENT: (
        "assignment_id", "stage_instance_id", "assignee_user_id",
        "assigned_via", "delegated_from", "state", "assigned_at",
    ),
    APPROVAL_ACTION: (
        "action_id", "stage_instance_id", "instance_id", "actor_user_id",
        "acting_for_user_id", "action", "reason_code", "reason_text", "at",
        "seq", "prev_hash", "entry_hash",
    ),
    APPROVAL_DELEGATION: (
        "delegation_id", "delegator_user_id", "delegate_user_id",
        "scope_key", "active_range", "created_at", "created_by",
        "revoked_at", "revoke_reason",
    ),
}

# ============================================== immutability and protection
#: The database objects that make a live workflow immutable. Named here so a
#: test -- or an operator's health check -- can assert their presence without
#: re-reading the migration, and so nobody re-implements the rule in Python
#: believing the database does not already enforce it.
IMMUTABILITY_TRIGGERS: dict[str, tuple[str, ...]] = {
    APPROVAL_DEFINITION: (
        "approval_definition_immutable_update",
        "approval_definition_immutable_delete",
    ),
    APPROVAL_RULE: (
        "approval_rule_immutable_update",
        "approval_rule_immutable_delete",
    ),
    APPROVAL_STAGE: (
        "approval_stage_immutable_update",
        "approval_stage_immutable_delete",
    ),
    APPROVAL_STAGE_APPROVER: (
        "approval_stage_approver_immutable_update",
        "approval_stage_approver_immutable_delete",
    ),
    APPROVAL_ACTION: (
        "approval_action_no_update",
        "approval_action_no_delete",
    ),
    APPROVAL_INSTANCE: (
        "approval_instance_frozen_fields",
    ),
}

#: The trigger functions 008 defines.
IMMUTABILITY_FUNCTIONS: tuple[str, ...] = (
    "assert_approval_definition_immutable",
    "assert_approval_definition_child_immutable",
    "assert_approval_action_append_only",
    "assert_approval_instance_frozen_fields",
)

#: Columns of ``approval_instance`` frozen at creation by
#: ``approval_instance_frozen_fields``. Every other column stays updatable --
#: advancing an instance through its lifecycle is the engine's job.
INSTANCE_FROZEN_COLUMNS: tuple[str, ...] = (
    "snapshot", "definition_version", "object_content_sha",
)

#: Tables 008 puts behind row-level security, mapped to their policy name.
#: ``approval_delegation`` and ``reason_code`` are deliberately absent -- see
#: ``app/backend/pg/scope_inventory.py``, which records the reason.
RLS_POLICIES: dict[str, str] = {
    APPROVAL_DEFINITION: "approval_definition_scope",
    APPROVAL_RULE: "approval_rule_scope",
    APPROVAL_STAGE: "approval_stage_scope",
    APPROVAL_STAGE_APPROVER: "approval_stage_approver_scope",
    APPROVAL_INSTANCE: "approval_instance_scope",
    APPROVAL_STAGE_INSTANCE: "approval_stage_instance_scope",
    APPROVAL_ASSIGNMENT: "approval_assignment_scope",
    APPROVAL_ACTION: "approval_action_scope",
}

#: Append-only by trigger AND by privilege, exactly as ``audit_log`` is.
APPEND_ONLY_TABLES: tuple[str, ...] = (APPROVAL_ACTION,)

#: The audit stream key an instance's action chain is written under
#: (Contract 9). The payload format ``prev|at|actor|action|type|id|detail`` is
#: frozen and lives in ``app/backend/pg/audit.py``; only the stream name is
#: this module's business.
AUDIT_STREAM_PREFIX = "approval:"


def audit_stream_key(instance_id: str) -> str:
    """The hash-chain stream key for `instance_id` -- ``approval:{id}``.

    A function rather than a format string at each call site so that every
    stream key in the system is produced by one expression. Contract 9 fixes
    this shape; ``approval_action.seq`` is unique per instance, which is the
    same per-stream ordering ``audit_log`` uses.
    """
    return f"{AUDIT_STREAM_PREFIX}{instance_id}"
