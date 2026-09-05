"""The approval engine: instances, stages, assignments and decisions.

``approval_rules.py`` decides *how an object routes* from plain data.
``delegation.py`` decides *who may act for whom*. This module is where those
decisions meet PostgreSQL: it opens instances, materialises stages and
assignments, takes the locks, re-validates the budget, applies decisions and
writes the hash chain -- all inside one transaction per call.

Specification: ``docs/WAVE4_CONTRACTS.md`` contracts 2, 5, 7, 8 and 9, and
``docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`` section 9.

The lock order, in full
-----------------------
Contract 7 extends Wave 3's contract 3 rather than replacing it, so
``locking.py``'s rules still bind and this module obeys them literally::

    1. budget cells      lock_affected_cells(...) -- ONCE, FIRST, complete set,
                         ordered (wbs_path, budget_head_id)
    2. approval instance SELECT ... FROM approval_instance ... FOR UPDATE
    3. document row      SELECT ... FROM <object table> ... FOR UPDATE
    4. advisory          the approval:{instance_id} audit stream, taken LAST

Every mutating function here takes them in that order and takes no cell lock
afterwards, which is `locking.py`'s rule 2 and the reason the deadlock-freedom
proof still holds with a fifth participant in the graph.

One consequence is worth stating because it looks like a violation and is not.
Step 1 needs the affected cell set, which lives in the instance's snapshot, so
the instance must be **read** before it is **locked**. That read is unlocked and
its result is not trusted: every field it produced is re-read after step 2 under
the lock, and the status is re-checked there. This is exactly the shape
``budget.approve_revision`` already uses -- read to find the cells, lock the
cells, lock the row, re-read under the lock -- and the snapshot is immutable by
contract 1, so the cell set the unlocked read produced cannot have changed even
in principle.

What "no auto-approval" means mechanically
-----------------------------------------
Four independent gates, any one of which is enough on its own:

* :func:`~app.backend.pg.approval_rules.resolve_route` raises when no definition
  matches, so :func:`open_instance` cannot reach a stage list without a route.
* :func:`~app.backend.pg.approval_rules.apply_contributor_filter` raises on an
  empty assignee set, so a stage cannot be created with nobody on it.
* :func:`~app.backend.pg.approval_rules.required_quorum` raises rather than
  returning 0, so an empty stage has no satisfiable quorum to satisfy.
* :func:`_recompute_stage` treats a stage as complete only via
  ``quorum_met``, which is False for any non-positive requirement.

A stage recorded SKIPPED is not an approval either: it carries a
``skip_reason``, it records no assignment and no action, and
:func:`_advance` steps over it without counting it toward anything.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from .. import auth as auth_mod
from . import audit as audit_mod
from . import budget as budget_mod
from .approval_rules import (
    ACTION_APPROVE, ACTION_CANCEL, ACTION_ESCALATE, ACTION_RECALL,
    ACTION_REJECT, ACTION_RESUBMIT, ACTION_RETURN, ACTION_SUPERSEDE,
    ASSIGN_ACTED, ASSIGN_PENDING, ASSIGN_WITHDRAWN, CONDITION_DIMENSIONS,
    DEF_ACTIVE, ERR_BUDGET_MOVED, ERR_IDEMPOTENCY_KEY_REQUIRED,
    ERR_IDEMPOTENT_REPLAY, ERR_NOT_AN_ASSIGNEE, ERR_OBJECT_VERSION_STALE,
    ERR_REASON_REQUIRED, ERR_ROUTE_UNRESOLVED, ERR_STAGE_NOT_OPEN,
    INST_APPROVED, INST_CANCELLED,
    INST_EXCEPTION_PENDING, INST_OPEN, INST_RECALLED, INST_REJECTED,
    INST_RETURNED, INST_SUPERSEDED, STAGE_APPROVED, STAGE_ESCALATED,
    STAGE_OPEN_STATUSES, STAGE_PENDING, STAGE_REJECTED, STAGE_RETURNED,
    STAGE_SKIPPED, STAGE_DECISION_ACTIONS, T_ACTION, T_ASSIGNMENT,
    T_DEFINITION, T_INSTANCE, T_RULE, T_STAGE, T_STAGE_APPROVER,
    T_STAGE_INSTANCE, VIA_ESCALATION, VIA_ROLE, ApprovalError,
    ApprovalRouteUnresolved, AssigneeSet, DefinitionSpec,
    NoIndependentApprover, StageSpec, apply_contributor_filter, build_definition,
    build_stage, expand_role_members, quorum_met, required_quorum, resolve_path,
    resolve_route, scope_matches, stage_applies,
)
from .delegation import (
    assert_delegation_independent, expand_delegates, load_delegations_for,
)
from .engine import Session
from .locking import lock_affected_cells

# ==========================================================================
# Which document tables the engine may route, and how to read one
# ==========================================================================


@dataclass(frozen=True)
class ObjectBinding:
    """How one ``object_type`` maps onto a real table.

    The registry is a **closed allow-list**, not a convention. Table and column
    names reach SQL through f-strings -- there is no way to parameterise an
    identifier -- so the only safe way to build these statements is for every
    identifier to come from this dictionary and never from a caller.
    :func:`binding_for` raises on an unknown ``object_type`` before any string
    is formatted, which is what makes that true rather than merely intended.
    """

    object_type: str
    table: str
    pk_column: str
    version_column: str
    maker_column: str
    entity_path: str = "entity_id"
    project_path: str = "project_id"


#: The document types this engine can route today. PR and PO live in the SQLite
#: application (`app/backend/db.py`) and have no PostgreSQL table yet, so they
#: are deliberately absent rather than declared and broken: `binding_for` fails
#: loudly on them, which is a better answer than a statement against a table
#: that does not exist.
OBJECT_BINDINGS: dict[str, ObjectBinding] = {
    "BUDGET_REVISION": ObjectBinding(
        object_type="BUDGET_REVISION", table="budget_revision",
        pk_column="revision_id", version_column="version_no",
        maker_column="created_by"),
    "BUDGET_TRANSFER": ObjectBinding(
        object_type="BUDGET_TRANSFER", table="budget_transfer",
        pk_column="transfer_id", version_column="version_no",
        maker_column="created_by"),
}


def binding_for(object_type: str) -> ObjectBinding:
    binding = OBJECT_BINDINGS.get(object_type)
    if binding is None:
        raise ApprovalError(
            "OBJECT_TYPE_UNKNOWN",
            f"{object_type!r} is not a routable object type. Known types: "
            f"{', '.join(sorted(OBJECT_BINDINGS)) or 'none'}.",
            status=400)
    return binding


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _canonical_json(value: Any) -> str:
    """Stable JSON text, for HASHING only -- never for a bind parameter.

    Sorted keys and no whitespace so that ``content_sha`` is a function of the
    document's content and not of how a serialiser happened to lay it out.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _to_jsonb(value: Any) -> Any:
    """Wrap a dict/list for a ``jsonb`` bind parameter.

    psycopg3 does NOT adapt a plain Python `dict` to `jsonb` -- it raises
    ProgrammingError on an unknown type -- and a `str` bound to a `jsonb`
    column is a text/jsonb type mismatch, not an implicit cast. The same
    wrapper `masters.py::_to_jsonb` uses, for the same reason; `None` passes
    through unwrapped so the column gets a real SQL NULL rather than a jsonb
    `null`.
    """
    return None if value is None else Jsonb(value)


def normalise_snapshot(snapshot: Mapping[str, Any], *, object_type: str,
                        object_id: str) -> dict[str, Any]:
    """The canonical form of a snapshot, used everywhere a sha is taken.

    ``open_instance`` fills in ``object_type`` and ``object_id`` so a predicate
    can route on document type without every caller remembering to include
    them. That fill-in has to happen BEFORE the sha, and it has to happen the
    same way on the comparison path -- otherwise ``supersede_if_changed`` hashes
    the caller's raw snapshot, finds a difference that is nothing but the two
    keys it did not add, and supersedes an unchanged document on every check.
    One function, called from both places, so the two cannot drift apart.
    """
    normalised = dict(snapshot)
    normalised.setdefault("object_type", object_type)
    normalised.setdefault("object_id", object_id)
    return normalised


def content_sha(snapshot: Mapping[str, Any]) -> str:
    """The document fingerprint an instance is pinned to (contract 1).

    Canonical JSON so that a re-serialisation with different key order is not
    mistaken for an edit; ``sha256`` because that is what the audit chain
    already uses and there is no reason for a second digest in one product.
    """
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


# ==========================================================================
# Pure decision helpers -- no database, tested without one
# ==========================================================================


def assert_object_version_fresh(expected: Any, actual: Any, *, instance_id: str) -> None:
    """Contract 8: a decision against a stale ``object_version`` is refused.

    The comparison is on the value the caller SAW when they formed their
    opinion. Approving version 3 of a document that is now at version 4 is not a
    stale click to be tidied away -- it is somebody approving text they never
    read -- so it is refused rather than retried against the current version.
    """
    if str(expected) != str(actual):
        raise ApprovalError(
            ERR_OBJECT_VERSION_STALE,
            f"This decision was taken against version {expected} of the "
            f"document, which is now at version {actual}. The document changed "
            f"after you opened it: re-read it and decide again.",
            status=409,
            detail={"instance_id": instance_id,
                    "submitted_object_version": expected,
                    "current_object_version": actual})


def assert_idempotency_key(idempotency_key: str | None) -> str:
    """Contract 3 makes the key REQUIRED on every decision.

    Required rather than optional-with-a-default, because a generated default
    is a different key on every retry, which is precisely the case idempotency
    exists to handle. A caller that cannot supply one has not decided what a
    retry of their request means.
    """
    if not idempotency_key or not str(idempotency_key).strip():
        raise ApprovalError(
            ERR_IDEMPOTENCY_KEY_REQUIRED,
            "Every approval decision must carry an idempotency_key so that a "
            "retried request cannot apply the decision twice.",
            status=400)
    return str(idempotency_key).strip()


def assert_reason_supplied(stage: Mapping[str, Any] | StageSpec, action: str,
                            reason_code: str | None, reason_text: str | None) -> None:
    """``REASON_REQUIRED`` where the stage demands one, and for every REJECT/RETURN.

    A stage's ``requires_reason`` flag is a floor, not a ceiling. Rejecting or
    returning a document always needs a reason regardless of configuration,
    because the person who raised it has to be told what to fix; an approval
    needs one only where the stage says so.
    """
    requires = bool(getattr(stage, "requires_reason", None)
                    if not isinstance(stage, Mapping) else stage.get("requires_reason"))
    if action in (ACTION_REJECT, ACTION_RETURN):
        requires = True
    if not requires:
        return
    if (reason_code and str(reason_code).strip()) or (reason_text and str(reason_text).strip()):
        return
    raise ApprovalError(
        ERR_REASON_REQUIRED,
        f"A {action} on this stage requires a reason. Supply reason_code, "
        f"reason_text, or both.",
        status=400,
        detail={"action": action})


def budget_moved(routed_check: Mapping[str, Any] | None,
                  current_check: Mapping[str, Any] | None) -> bool:
    """Has availability fallen since the object was routed? (Contract 7.)

    The test is on ``available_paise``, not on ``verdict``. A verdict test would
    be wrong in both directions: an object routed down an exception path is
    *already* ``EXCEEDS_BUDGET`` and its approvers approved it knowing that, so
    blocking on the verdict would make the exception route unusable; and a
    verdict can stay ``OK`` while availability halves, which is exactly the
    stale number contract 7 exists to refuse.

    Availability RISING is not a move that matters -- more room than the
    approvers were shown harms nobody -- so this is one-sided by design.
    """
    if not routed_check or not current_check:
        return False
    try:
        was = int(routed_check["available_paise"])
        now = int(current_check["available_paise"])
    except (KeyError, TypeError, ValueError):
        return False
    return now < was


def compute_waves(stages: Sequence[StageSpec]) -> list[list[int]]:
    """Group stages into the waves that open together.

    A stage with no ``parallel_group`` is a wave of its own. Stages sharing a
    group form one wave that opens together and completes when **every** member
    meets quorum, in either order (section 9.3, stages 2 and 3 in group ``G2``).

    A group whose stages are not contiguous in ``stage_no`` order is refused. It
    has no coherent meaning -- "open 2 and 4 together, but 3 comes after 2 and
    before 4" cannot be satisfied -- and silently reordering somebody's workflow
    to make it work would be worse than telling them it is wrong.
    """
    waves: list[list[int]] = []
    index_of_group: dict[str, int] = {}
    for stage in sorted(stages, key=lambda s: s.stage_no):
        group = stage.parallel_group
        if group is None:
            waves.append([stage.stage_no])
            continue
        if group in index_of_group:
            position = index_of_group[group]
            if position != len(waves) - 1:
                raise ApprovalError(
                    "APPROVAL_DEFINITION_INVALID",
                    f"parallel_group {group!r} is not contiguous: stage "
                    f"{stage.stage_no} rejoins a group that another stage has "
                    f"already been sequenced past. Stages in one parallel group "
                    f"must be consecutive.",
                    status=400)
            waves[position].append(stage.stage_no)
            continue
        index_of_group[group] = len(waves)
        waves.append([stage.stage_no])
    return waves


def next_wave(waves: Sequence[Sequence[int]],
               stage_states: Mapping[int, str]) -> list[int] | None:
    """The stages of the next wave to open, or ``None`` when none can open.

    A wave is settled when each of its stages is APPROVED or SKIPPED. A wave
    containing a stage still PENDING or ESCALATED is the current wave and
    nothing beyond it opens; a wave none of whose stages has yet been *opened*
    is the one to open next, and what is returned is the members that still
    need opening -- never the ones already recorded.

    A SKIPPED member does not block its own wave
    --------------------------------------------
    This used to require every member of a candidate wave to have NO state at
    all, which is wrong for the one shape :func:`open_instance` actually
    produces. ``open_instance`` writes the SKIPPED row for every non-applicable
    stage FIRST, and only then asks which wave to open -- so a parallel group
    holding an ``applies_when`` member arrives here as, say, ``{2: SKIPPED}``
    for the wave ``[1, 2]``. The old test was False for "all settled" (stage 1
    has no state) and False for "all unstarted" (stage 2 is SKIPPED), so the
    function fell through to ``None``; ``open_instance`` read that as "every
    stage was skipped" and held the object EXCEPTION_PENDING. A workflow whose
    parallel group contained a conditional stage was therefore unroutable.

    A SKIPPED stage is settled, not in progress: it recorded a control that did
    not fire, took no assignment and can never be acted on. So a wave qualifies
    to open when none of its members is in an OPEN or decided state, and the
    stages returned are exactly those with no row yet -- which is also what
    keeps :func:`_open_wave` from re-inserting a stage instance over the
    SKIPPED row it would collide with on ``UNIQUE (instance_id, stage_no)``.
    """
    for wave in waves:
        states = [stage_states.get(stage_no) for stage_no in wave]
        if all(state in (STAGE_APPROVED, STAGE_SKIPPED) for state in states):
            continue
        if all(state is None or state == STAGE_SKIPPED for state in states):
            return [stage_no for stage_no in wave
                    if stage_states.get(stage_no) is None]
        return None                     # this wave is still in progress
    return None


def all_waves_settled(waves: Sequence[Sequence[int]],
                       stage_states: Mapping[int, str]) -> bool:
    """Every stage in every wave is APPROVED or SKIPPED.

    ``False`` for an empty stage list. A definition with no stages routes to
    nobody, and treating "there was nothing to approve" as "it is approved" is
    the auto-approval this engine refuses everywhere else; it surfaces as
    ``EXCEPTION_PENDING`` at :func:`open_instance` instead.
    """
    if not waves:
        return False
    return all(stage_states.get(stage_no) in (STAGE_APPROVED, STAGE_SKIPPED)
               for wave in waves for stage_no in wave)


def due_at_for(opened_at: datetime, sla_hours: int | None) -> datetime | None:
    if not sla_hours:
        return None
    return opened_at + timedelta(hours=int(sla_hours))


def is_escalation_due(opened_at: datetime, escalate_after_hours: int | None,
                       now: datetime) -> bool:
    """Has a stage been open longer than its escalation threshold?

    Both instants are normalised to UTC before comparison, for the same reason
    ``audit.canonical_at`` normalises before hashing: a naive datetime compared
    against an aware one raises `TypeError`, and a scheduled job that crashes on
    a timezone is a job that silently stops escalating anything.
    """
    if not escalate_after_hours:
        return False
    opened = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc)
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return moment >= opened + timedelta(hours=int(escalate_after_hours))


# ==========================================================================
# Reads
# ==========================================================================


def _load_definitions(session: Session, object_type: str) -> list[DefinitionSpec]:
    rows = session.fetchall(
        f"""
        SELECT definition_id, object_type, code, version, status, entity_id,
               effective_from, effective_to
        FROM approval_definition
        WHERE object_type = %s AND status = %s
        ORDER BY code, version DESC
        """,
        (object_type, DEF_ACTIVE))
    definitions: list[DefinitionSpec] = []
    for row in rows:
        rule_rows = session.fetchall(
            f"SELECT rule_id, priority, predicate FROM approval_rule "
            f"WHERE definition_id = %s ORDER BY priority",
            (row[0],))
        definitions.append(build_definition({
            "definition_id": row[0], "object_type": row[1], "code": row[2],
            "version": row[3], "status": row[4], "entity_id": row[5],
            "effective_from": row[6], "effective_to": row[7],
            "rules": [{"rule_id": r[0], "priority": r[1], "predicate": r[2]}
                      for r in rule_rows],
        }))
    return definitions


def _load_stages(session: Session, definition_id: str) -> list[StageSpec]:
    rows = session.fetchall(
        f"""
        SELECT stage_id, stage_no, name, parallel_group, quorum_type, quorum_n,
               applies_when, sla_hours, escalate_after_hours, escalate_to,
               allow_delegation, requires_reason, reason_code_set
        FROM approval_stage WHERE definition_id = %s ORDER BY stage_no
        """,
        (definition_id,))
    stages: list[StageSpec] = []
    for row in rows:
        approver_rows = session.fetchall(
            f"SELECT ordinal, approver_kind, approver_ref, scope_expr "
            f"FROM approval_stage_approver WHERE stage_id = %s ORDER BY ordinal",
            (row[0],))
        stages.append(build_stage({
            "stage_id": row[0], "stage_no": row[1], "name": row[2],
            "parallel_group": row[3], "quorum_type": row[4], "quorum_n": row[5],
            "applies_when": row[6], "sla_hours": row[7],
            "escalate_after_hours": row[8], "escalate_to": row[9],
            "allow_delegation": row[10], "requires_reason": row[11],
            "reason_code_set": row[12],
            "approvers": [{"ordinal": a[0], "approver_kind": a[1],
                           "approver_ref": a[2], "scope_expr": a[3]}
                          for a in approver_rows],
        }))
    return stages


def _role_members(session: Session, roles: Iterable[str]) -> dict[str, list[str]]:
    wanted = sorted({r for r in roles if r})
    if not wanted:
        return {}
    rows = session.fetchall(
        "SELECT role, user_id FROM role_grant WHERE role = ANY(%s) "
        "ORDER BY role, user_id", (wanted,))
    members: dict[str, list[str]] = {role: [] for role in wanted}
    for role, user_id in rows:
        members.setdefault(role, []).append(user_id)
    return members


def _member_scopes(session: Session, user_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Each user's granted scope, in the shape :func:`scope_matches` expects.

    A dimension with **no restriction row** is omitted from the user's mapping,
    which ``scope_matches`` reads as unrestricted -- the same convention
    ``roles.resolve_scope`` uses, deliberately, so an absent grant cannot mean
    "everything" in one layer and "nothing" in the other.
    """
    ids = sorted({u for u in user_ids if u})
    if not ids:
        return {}
    rows = session.fetchall(
        """
        SELECT r.user_id, r.dimension,
               array_remove(array_agg(g.scope_value), NULL) AS values
        FROM user_scope_restriction r
        LEFT JOIN user_scope_grant g
               ON g.user_id = r.user_id AND g.dimension = r.dimension
        WHERE r.user_id = ANY(%s)
        GROUP BY r.user_id, r.dimension
        """,
        (ids,))
    scopes: dict[str, dict[str, Any]] = {}
    for user_id, dimension, values in rows:
        scopes.setdefault(user_id, {})[dimension] = list(values or [])
    return scopes


def contributor_set(session: Session, object_type: str, object_id: str, *,
                     maker_user_id: str | None = None) -> set[str]:
    """Maker + editors + prior actors, per section 9.4 step 3.

    A deliberate **superset**: it can only refuse more approvers, never fewer,
    so widening it is always safe and narrowing it is the change that needs
    argument. Three sources, unioned:

    * the **maker** -- the document's own ``created_by``;
    * the **editors** -- every actor who appears on the document's audit stream,
      which is where an edit leaves its trace;
    * the **prior actors** -- everyone who has already taken an approval action
      on this object, on this instance or an earlier superseded one, counting
      the delegate and the person delegated for as two separate people.

    Prior actors matter for repeat approvals across stages: the same person
    approving stage 1 and then stage 4 is one pair of eyes doing a job the
    configuration asked two to do.
    """
    contributors: set[str] = set()
    if maker_user_id:
        contributors.add(maker_user_id)

    binding = binding_for(object_type)
    row = session.fetchone(  # scope-exempt: reads ONE row by primary key, through a table name that comes only from the closed OBJECT_BINDINGS allow-list, for an object whose approval_instance assert_instance_in_scope has already checked; returns a user id, never business data
        f"SELECT {binding.maker_column} FROM {binding.table} "
        f"WHERE {binding.pk_column} = %s",
        (object_id,))
    if row is not None and row[0]:
        contributors.add(row[0])

    for (actor,) in session.fetchall(
            "SELECT DISTINCT actor FROM audit_log WHERE stream_key = %s",
            (f"{object_type}:{object_id}",)):
        if actor:
            contributors.add(actor)

    for actor, acting_for in session.fetchall(
            f"""
            SELECT a.actor_user_id, a.acting_for_user_id
            FROM approval_action a
            JOIN approval_instance i ON i.instance_id = a.instance_id
            WHERE i.object_type = %s AND i.object_id = %s
            """,
            (object_type, object_id)):
        if actor:
            contributors.add(actor)
        if acting_for:
            contributors.add(acting_for)

    return contributors


def assert_instance_in_scope(session: Session, instance: Mapping[str, Any]) -> None:
    """Refuse an instance outside the caller's row-level scope (contract 6).

    ``approval_instance`` carries ``entity_id`` and ``project_id`` denormalised
    at creation precisely so it is scopable directly, and this is where that
    pays off: the engine reads document rows by primary key through a computed
    table name (``OBJECT_BINDINGS``), which no static gate can attribute to a
    table, so those reads are only defensible if the instance naming them has
    already been checked.

    The convention is `roles.resolve_scope`'s, unchanged: ``None`` on a
    dimension means unrestricted, an empty frozenset means nothing, and
    ``read_all`` bypasses. An instance whose own dimension is NULL is not
    refused -- there is nothing to compare -- which is the same reading
    ``repo.compile_scope`` gives a NULL column.
    """
    scope = getattr(session, "scope", None)
    if scope is None or getattr(scope, "read_all", False):
        return
    for dimension, key in (("entity_ids", "entity_id"), ("project_ids", "project_id")):
        allowed = getattr(scope, dimension, None)
        if allowed is None:
            continue                          # unrestricted on this dimension
        value = instance.get(key)
        if value is None:
            continue                          # the instance carries no such id
        if value not in allowed:
            raise ApprovalError(
                "APPROVAL_INSTANCE_OUT_OF_SCOPE",
                f"Approval instance {instance.get('instance_id')} belongs to "
                f"{key}={value!r}, which is outside your scope.",
                status=403, detail={key: value})


def get_instance(session: Session, instance_id: str, *,
                  for_update: bool = False) -> dict[str, Any]:
    row = session.fetchone(
        f"""
        SELECT instance_id, object_type, object_id, object_version,
               object_content_sha, definition_id, definition_version, status,
               current_stage_no, snapshot, supersedes_instance_id, maker_user_id,
               entity_id, project_id, opened_at, closed_at, correlation_id
        FROM approval_instance WHERE instance_id = %s
        {"FOR UPDATE" if for_update else ""}
        """,
        (instance_id,))
    if row is None:
        raise ApprovalError("APPROVAL_INSTANCE_NOT_FOUND",
                             f"Approval instance {instance_id} does not exist.",
                             status=404)
    keys = ("instance_id", "object_type", "object_id", "object_version",
            "object_content_sha", "definition_id", "definition_version", "status",
            "current_stage_no", "snapshot", "supersedes_instance_id",
            "maker_user_id", "entity_id", "project_id", "opened_at", "closed_at",
            "correlation_id")
    instance = dict(zip(keys, row))
    # Every path into the engine goes through here, so this is the one place a
    # scope check has to be to cover them all.
    assert_instance_in_scope(session, instance)
    return instance


def _stage_states(session: Session, instance_id: str) -> dict[int, dict[str, Any]]:
    rows = session.fetchall(
        f"""
        SELECT stage_instance_id, stage_no, parallel_group, status, skip_reason,
               quorum_required, quorum_met, opened_at, due_at, escalated_at, closed_at
        FROM approval_stage_instance WHERE instance_id = %s ORDER BY stage_no
        """,
        (instance_id,))
    keys = ("stage_instance_id", "stage_no", "parallel_group", "status",
            "skip_reason", "quorum_required", "quorum_met", "opened_at", "due_at",
            "escalated_at", "closed_at")
    return {row[1]: dict(zip(keys, row)) for row in rows}


# ==========================================================================
# The hash chain (contract 9)
# ==========================================================================


#: Field separator inside the hashed detail. `\x1f` (ASCII unit separator) for
#: the same reason `budget._encode_cursor` uses it: it cannot occur in an id, a
#: reason code or a reason text, so the joined string is unambiguous, and it is
#: not `|`, which the frozen payload format itself uses as its own separator.
_DETAIL_SEP = "\x1f"


def chain_detail(stage_instance_id: str | None, reason_code: str | None,
                  reason_text: str | None) -> str:
    """The ``detail`` field the frozen payload hashes, built ONLY from columns
    ``approval_action`` actually stores.

    This is the difference between a hash chain and a decoration. Contract 9
    reuses the frozen payload ``prev|at|actor|action|type|id|detail``, and
    contract 1's ``approval_action`` column list has **no ``detail`` column** --
    so hashing the engine's human-readable narrative would produce entries that
    can never be recomputed, and ``verify_instance_chain`` could only ever check
    that the ``prev_hash`` links agree with each other. An attacker who rewrote
    an entry's ``action`` from REJECT to APPROVE and recomputed nothing would
    pass that check, because nothing tied the stored fields to the digest.

    Every component here is read back from the row, so verification recomputes
    the digest exactly and a tampered ``action``, ``reason_code``,
    ``reason_text``, ``actor`` or ``at`` breaks it. The narrative is not lost:
    it is stored in the ``outcome`` column under ``"detail"``.
    """
    return _DETAIL_SEP.join((stage_instance_id or "", reason_code or "",
                             reason_text or ""))


def _actor_for_hash(actor_user_id: str, acting_for_user_id: str | None) -> str:
    """Both identities in the hashed actor, so a delegation cannot be rewritten
    into a direct action (or the reverse) without breaking the chain."""
    return (f"{actor_user_id} for {acting_for_user_id}"
            if acting_for_user_id else actor_user_id)


def _append_action(session: Session, *, instance_id: str, stage_instance_id: str | None,
                    actor_user_id: str, acting_for_user_id: str | None, action: str,
                    object_type: str, object_id: str, detail: str,
                    reason_code: str | None = None, reason_text: str | None = None,
                    idempotency_key: str | None = None,
                    correlation_id: str | None = None,
                    outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write one ``approval_action``, chained on ``approval:{instance_id}``.

    Contract 9: the **existing frozen payload format**
    ``prev|at|actor|action|type|id|detail`` -- reused through
    ``audit.compute_entry_hash`` rather than reimplemented, because a second
    implementation of a frozen format is a second place for it to drift.

    The advisory lock is taken here and therefore LAST in the enclosing
    function's lock order (contract 7 step 4), and it is taken **before**
    ``prev_hash`` is read, which is the ordering ``audit.append``'s docstring
    explains: two writers to one stream that both read the same ``prev_hash``
    each mint a successor to it and the chain forks.

    Where ``correlation_id`` goes, and where it deliberately does not
    ----------------------------------------------------------------
    It is written into ``outcome``, which is a real stored ``jsonb`` column, so
    the request that caused an action can be traced to the action it caused.

    It is **not** hashed. Contract 9 freezes the payload as
    ``prev|at|actor|action|type|id|detail`` and ``chain_detail`` builds the
    ``detail`` component from stored columns only, precisely so verification
    recomputes each digest from the row it is checking. Folding a correlation
    id into the digest would either change the frozen format or hash a value
    the verifier cannot read back -- and the second is how a hash chain becomes
    a decoration. ``approval_action`` has no ``correlation_id`` column of its
    own (migration 008 is lead-owned and not this stream's to alter), so
    ``outcome`` is where it can live truthfully.
    """
    stream_key = f"approval:{instance_id}"
    audit_mod.advisory_audit_lock(session, stream_key)      # LAST lock, before the read

    prev = session.fetchone(
        f"SELECT seq, entry_hash FROM approval_action WHERE instance_id = %s "
        f"ORDER BY seq DESC LIMIT 1",
        (instance_id,))
    prev_seq, prev_hash = (prev[0], prev[1]) if prev is not None else (0, None)
    seq = prev_seq + 1

    at = datetime.now(timezone.utc)
    at_iso = audit_mod.canonical_at(at)
    entry_hash = audit_mod.compute_entry_hash(
        prev_hash, at_iso, _actor_for_hash(actor_user_id, acting_for_user_id),
        action, object_type, object_id,
        chain_detail(stage_instance_id, reason_code, reason_text))

    # The narrative rides in `outcome`, which IS a stored column, rather than in
    # the digest, which has no column to be recomputed from. See chain_detail().
    outcome_payload = dict(outcome or {})
    outcome_payload.setdefault("detail", detail)
    if correlation_id:
        outcome_payload.setdefault("correlation_id", correlation_id)

    # No fabricated id. The row's identity is whatever the database mints, and
    # returning a made-up one would hand every caller -- the timeline, the API,
    # the audit view -- an id that matches no row.
    row = session.fetchone(
        f"""
        -- action_id is GENERATED ALWAYS AS IDENTITY in migration 008, so it
        -- must NOT be supplied: PostgreSQL rejects a non-DEFAULT value
        -- outright. Letting the database mint it is also the right design for
        -- an append-only ledger -- there is no id to collide, and no caller
        -- can choose where its row lands. Note the ordering guarantee still
        -- comes from `seq` under the advisory lock, never from action_id:
        -- identity values are assigned before commit and can commit out of
        -- order, which is the same trap audit_log documents.
        INSERT INTO approval_action
            (stage_instance_id, instance_id, actor_user_id,
             acting_for_user_id, action, reason_code, reason_text, at, seq,
             prev_hash, entry_hash, idempotency_key, outcome)
        VALUES (%(stage_instance_id)s, %(instance_id)s,
                %(actor)s, %(acting_for)s, %(action)s, %(reason_code)s,
                %(reason_text)s, %(at)s, %(seq)s, %(prev_hash)s, %(entry_hash)s,
                %(idempotency_key)s, %(outcome)s)
        RETURNING action_id
        """,
        {"stage_instance_id": stage_instance_id,
         "instance_id": instance_id, "actor": actor_user_id,
         "acting_for": acting_for_user_id, "action": action,
         "reason_code": reason_code, "reason_text": reason_text, "at": at,
         "seq": seq, "prev_hash": prev_hash, "entry_hash": entry_hash,
         "idempotency_key": idempotency_key,
         "outcome": _to_jsonb(outcome_payload)},
    )
    action_id = row[0] if row else None
    return {"action_id": action_id, "seq": seq, "at": at_iso,
            "entry_hash": entry_hash, "prev_hash": prev_hash}


def verify_instance_chain(session: Session, instance_id: str) -> dict[str, Any]:
    """Recompute the ``approval_action`` chain for one instance.

    Mirrors ``audit.verify_chain`` including its contiguity check, because the
    failure it closes -- deleting the last k entries leaves a perfectly linked
    prefix -- applies identically here, and a decision history is exactly the
    thing somebody would want to truncate.
    """
    instance = get_instance(session, instance_id)
    rows = session.fetchall(
        """
        SELECT seq, at, actor_user_id, acting_for_user_id, action,
               stage_instance_id, reason_code, reason_text, prev_hash, entry_hash
        FROM approval_action WHERE instance_id = %s ORDER BY seq
        """,
        (instance_id,))
    expected_prev: str | None = None
    first_break_seq: int | None = None
    checked = 0
    for (seq, at, actor, acting_for, action, stage_instance_id, reason_code,
         reason_text, row_prev, row_hash) in rows:
        checked += 1
        recomputed = audit_mod.compute_entry_hash(
            row_prev, audit_mod.canonical_at(at),
            _actor_for_hash(actor, acting_for), action,
            instance["object_type"], instance["object_id"],
            chain_detail(stage_instance_id, reason_code, reason_text))
        broken = (row_prev != expected_prev) or (recomputed != row_hash)
        if broken and first_break_seq is None:
            first_break_seq = seq
        # Walk the chain as STORED, not as recomputed, so one corrupted entry
        # does not desynchronise every entry after it -- the same reasoning
        # `audit.verify_chain` spells out.
        expected_prev = row_hash
    actual_seqs = [row[0] for row in rows]
    contiguous = actual_seqs == list(range(1, checked + 1))
    if not contiguous and first_break_seq is None:
        missing = sorted(set(range(1, checked + 1)) - set(actual_seqs))
        first_break_seq = missing[0] if missing else (actual_seqs[-1] + 1)
    return {
        "instance_id": instance_id,
        "stream_key": f"approval:{instance_id}",
        "object_type": instance["object_type"],
        "entries_checked": checked,
        "sequence_contiguous": contiguous,
        "first_break_seq": first_break_seq,
        "intact": first_break_seq is None and contiguous and checked > 0,
        "stream_found": checked > 0,
    }


# ==========================================================================
# Opening an instance (section 9.2)
# ==========================================================================


def _affected_cells(snapshot: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The complete ``(wbs_id, budget_head_id)`` set for `lock_affected_cells`.

    Read from the snapshot, which contract 1 makes immutable, so the set an
    unlocked read produces is the set the locked one would.
    """
    cells = snapshot.get("affected_cells") or []
    out: list[tuple[str, str]] = []
    for cell in cells:
        if isinstance(cell, Mapping):
            wbs_id, head = cell.get("wbs_id"), cell.get("budget_head_id")
        elif isinstance(cell, (list, tuple)) and len(cell) == 2:
            wbs_id, head = cell
        else:
            continue
        if wbs_id and head:
            out.append((str(wbs_id), str(head)))
    return out


def _budget_checks(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The availability checks recorded at routing time, for contract 7's re-run."""
    checks = snapshot.get("budget_checks") or []
    return [dict(check) for check in checks if isinstance(check, Mapping)]


def open_instance(session: Session, *, object_type: str, object_id: str,
                   object_version: Any, snapshot: Mapping[str, Any],
                   maker_user_id: str, business_date: date | None = None,
                   correlation_id: str | None = None,
                   supersedes_instance_id: str | None = None) -> dict[str, Any]:
    """Route an object and materialise its instance, stages and assignments.

    Contract 1: the instance records **the workflow it was routed under**
    (``definition_id`` + ``definition_version``) and **the document it was routed
    for** (``object_content_sha``), and neither may drift afterwards. Both are
    snapshotted here, at the only moment the engine knows them to be true.

    Failure modes, all of which produce an ``EXCEPTION_PENDING`` instance rather
    than an exception with nothing recorded -- because an unroutable object that
    leaves no row behind is an object nobody will ever look at:

    * no definition matched      -> ``APPROVAL_ROUTE_UNRESOLVED``
    * a stage has no independent
      approver after filtering   -> ``NO_INDEPENDENT_APPROVER``
    * the definition has no
      stages at all              -> ``APPROVAL_ROUTE_UNRESOLVED``

    Every applicable stage is created ``PENDING``; every non-applicable stage is
    created ``SKIPPED`` with a ``skip_reason``. Only the first wave is *opened*
    (assignments written, SLA clock started); later waves are materialised when
    the wave before them settles.
    """
    business_date = business_date or datetime.now(timezone.utc).date()
    snapshot = normalise_snapshot(snapshot, object_type=object_type,
                                   object_id=object_id)

    entity_id = _snapshot_str(snapshot, CONDITION_DIMENSIONS["entity"])
    project_id = _snapshot_str(snapshot, CONDITION_DIMENSIONS["project"])
    sha = content_sha(snapshot)
    instance_id = _new_id("AINS")

    definitions = _load_definitions(session, object_type)
    try:
        resolution = resolve_route(definitions, snapshot, object_type=object_type,
                                    business_date=business_date, entity_id=entity_id)
    except ApprovalRouteUnresolved as exc:
        _write_exception_instance(
            session, instance_id=instance_id, object_type=object_type,
            object_id=object_id, object_version=object_version, sha=sha,
            snapshot=snapshot, maker_user_id=maker_user_id, entity_id=entity_id,
            project_id=project_id, correlation_id=correlation_id,
            supersedes_instance_id=supersedes_instance_id, exc=exc)
        # RETURN, do not raise.
        #
        # This used to `raise`, on the reasoning that the row had already been
        # written so the object stayed visible. It did not: the caller owns
        # the transaction, and an exception propagating out of it rolls back
        # the EXCEPTION_PENDING row along with everything else -- destroying
        # the evidence as a direct consequence of reporting it. The object
        # then had no approval instance at all, which is the one outcome
        # Contract 2 exists to prevent.
        #
        # An unroutable object is a recorded OUTCOME, not a control-flow
        # exception: it is held for an administrator, and the caller decides
        # how to present that. The API maps this status to 409
        # APPROVAL_ROUTE_UNRESOLVED, and the row survives because nothing
        # unwound the transaction.
        return get_instance(session, instance_id)

    stages = _load_stages(session, resolution.definition.definition_id)
    if not stages:
        exc = ApprovalRouteUnresolved(
            f"Definition {resolution.definition.code} v"
            f"{resolution.definition.version} matched but defines no stages. A "
            f"workflow with no stages approves nothing and is not a licence to "
            f"approve everything.",
            detail={"definition_id": resolution.definition.definition_id})
        _write_exception_instance(
            session, instance_id=instance_id, object_type=object_type,
            object_id=object_id, object_version=object_version, sha=sha,
            snapshot=snapshot, maker_user_id=maker_user_id, entity_id=entity_id,
            project_id=project_id, correlation_id=correlation_id,
            supersedes_instance_id=supersedes_instance_id, exc=exc,
            definition=resolution.definition)
        # Same reasoning as the branch above: recorded, returned, not raised.
        # A definition that matched but defines no stages is a configuration
        # defect an administrator has to see, and raising would roll away the
        # only trace of it.
        return get_instance(session, instance_id)

    waves = compute_waves(stages)
    session.execute(
        f"""
        INSERT INTO approval_instance
            (instance_id, object_type, object_id, object_version,
             object_content_sha, definition_id, definition_version, status,
             current_stage_no, snapshot, supersedes_instance_id, maker_user_id,
             entity_id, project_id, opened_at, correlation_id)
        VALUES (%(id)s, %(otype)s, %(oid)s, %(over)s, %(sha)s, %(def)s, %(defv)s,
                %(status)s, %(stage)s, %(snapshot)s, %(supersedes)s, %(maker)s,
                %(entity)s, %(project)s, now(), %(corr)s)
        """,
        {"id": instance_id, "otype": object_type, "oid": object_id,
         "over": str(object_version), "sha": sha,
         "def": resolution.definition.definition_id,
         "defv": resolution.definition.version, "status": INST_OPEN,
         "stage": waves[0][0], "snapshot": _to_jsonb(dict(snapshot)),
         "supersedes": supersedes_instance_id, "maker": maker_user_id,
         "entity": entity_id, "project": project_id, "corr": correlation_id},
    )

    contributors = contributor_set(session, object_type, object_id,
                                    maker_user_id=maker_user_id)

    # Every stage gets a row now -- PENDING or SKIPPED -- so the trail is
    # complete from the first moment, rather than growing as stages are reached.
    stages_by_no = {stage.stage_no: stage for stage in stages}
    for stage in stages:
        applies, skip_reason = stage_applies(stage, snapshot)
        if applies:
            continue
        _insert_stage_instance(session, instance_id=instance_id, stage=stage,
                                status=STAGE_SKIPPED, skip_reason=skip_reason,
                                quorum_required=0, opened=False)

    stage_states = {no: state["status"]
                    for no, state in _stage_states(session, instance_id).items()}
    first = next_wave(waves, stage_states)

    detail = (f"Routed under {resolution.definition.code} v"
              f"{resolution.definition.version} on rule priority "
              f"{resolution.rule.priority}")
    if first is None:
        # Every stage was skipped. That is not an approval; it is a definition
        # whose applies_when predicates exclude this snapshot class entirely.
        _set_instance_status(session, instance_id, INST_EXCEPTION_PENDING,
                              closed=False)
        # `outcome`, not just `detail`.
        #
        # The other three fail-closed branches go through
        # `_write_exception_instance`, which records
        # `outcome = {"code": ..., "detail": ...}`. This one recorded its cause
        # in PROSE only, so an administrator triaging SCR-25 could read it but
        # could not filter or group on it, and `_submission_outcome` -- which
        # names the refusal to the caller by reading `outcome.code` -- fell
        # back to a default that named the wrong cause. Four ways to fail
        # closed, three of them queryable, is three too few.
        _append_action(session, instance_id=instance_id, stage_instance_id=None,
                        actor_user_id="SYSTEM", acting_for_user_id=None,
                        action=ACTION_ESCALATE, object_type=object_type,
                        object_id=object_id,
                        detail=f"{detail}; every stage SKIPPED -> EXCEPTION_PENDING "
                               f"({ApprovalRouteUnresolved.__name__})",
                        outcome={
                            "code": ERR_ROUTE_UNRESOLVED,
                            "detail": {
                                "instance_id": instance_id,
                                "definition_id":
                                    resolution.definition.definition_id,
                                "definition_code": resolution.definition.code,
                                "definition_version":
                                    resolution.definition.version,
                                "reason": "EVERY_STAGE_SKIPPED",
                            },
                        })
        # Recorded and RETURNED, not raised -- same reasoning as the two
        # branches above. The instance is already EXCEPTION_PENDING with an
        # audit action explaining why; raising would make the caller's
        # rollback delete both.
        return get_instance(session, instance_id)

    try:
        _open_wave(session, instance_id=instance_id, wave=first,
                    stages_by_no=stages_by_no, snapshot=snapshot,
                    contributors=contributors)
    except NoIndependentApprover as exc:
        _set_instance_status(session, instance_id, INST_EXCEPTION_PENDING,
                              closed=False)
        _append_action(session, instance_id=instance_id, stage_instance_id=None,
                        actor_user_id="SYSTEM", acting_for_user_id=None,
                        action=ACTION_ESCALATE, object_type=object_type,
                        object_id=object_id,
                        detail=f"{detail}; NO_INDEPENDENT_APPROVER -> EXCEPTION_PENDING",
                        outcome={"code": exc.code, "detail": exc.detail})
        exc.detail.setdefault("instance_id", instance_id)
        # Recorded and RETURNED. NO_INDEPENDENT_APPROVER is the outcome an
        # administrator has to act on -- every candidate approver was a
        # contributor -- so it must survive as a row, not evaporate with the
        # exception that announced it.
        return get_instance(session, instance_id)

    # `current_stage_no` was set to `waves[0][0]` at INSERT time, before the
    # SKIPPED rows were known. The wave that actually opened may begin at a
    # later stage_no -- a parallel group whose first member is conditional and
    # did not apply -- so it is corrected here to the first stage a person was
    # actually asked to act on.
    if first[0] != waves[0][0]:
        _set_instance_status(session, instance_id, INST_OPEN, closed=False,
                              current_stage_no=first[0])

    _append_action(session, instance_id=instance_id, stage_instance_id=None,
                    actor_user_id=maker_user_id, acting_for_user_id=None,
                    action=ACTION_RESUBMIT if supersedes_instance_id else "OPEN",
                    object_type=object_type, object_id=object_id, detail=detail)

    return {"instance_id": instance_id, "status": INST_OPEN,
            "definition_id": resolution.definition.definition_id,
            "definition_code": resolution.definition.code,
            "definition_version": resolution.definition.version,
            "matched_rule_priority": resolution.rule.priority,
            "object_content_sha": sha, "current_stage_no": first[0],
            "opened_stage_nos": list(first)}


def _snapshot_str(snapshot: Mapping[str, Any], path: str) -> str | None:
    value = resolve_path(snapshot, path)
    return None if (value is None or not isinstance(value, str)) else value


def _write_exception_instance(session: Session, *, instance_id: str, object_type: str,
                               object_id: str, object_version: Any, sha: str,
                               snapshot: Mapping[str, Any], maker_user_id: str,
                               entity_id: str | None, project_id: str | None,
                               correlation_id: str | None,
                               supersedes_instance_id: str | None,
                               exc: ApprovalError,
                               definition: DefinitionSpec | None = None) -> None:
    """Record the unroutable object as ``EXCEPTION_PENDING`` before re-raising.

    Contract 2 requires the exception to be *visible* (SCR-25), which it cannot
    be if the failure is only an exception in a rolled-back transaction. The row
    carries no ``definition_id`` when nothing matched, which is the honest
    representation of "this object has no workflow".
    """
    session.execute(
        f"""
        INSERT INTO approval_instance
            (instance_id, object_type, object_id, object_version,
             object_content_sha, definition_id, definition_version, status,
             current_stage_no, snapshot, supersedes_instance_id, maker_user_id,
             entity_id, project_id, opened_at, correlation_id)
        VALUES (%(id)s, %(otype)s, %(oid)s, %(over)s, %(sha)s, %(def)s, %(defv)s,
                %(status)s, NULL, %(snapshot)s, %(supersedes)s, %(maker)s,
                %(entity)s, %(project)s, now(), %(corr)s)
        """,
        {"id": instance_id, "otype": object_type, "oid": object_id,
         "over": str(object_version), "sha": sha,
         "def": definition.definition_id if definition else None,
         "defv": definition.version if definition else None,
         "status": INST_EXCEPTION_PENDING, "snapshot": _to_jsonb(dict(snapshot)),
         "supersedes": supersedes_instance_id, "maker": maker_user_id,
         "entity": entity_id, "project": project_id, "corr": correlation_id},
    )
    _append_action(session, instance_id=instance_id, stage_instance_id=None,
                    actor_user_id="SYSTEM", acting_for_user_id=None,
                    action=ACTION_ESCALATE, object_type=object_type,
                    object_id=object_id,
                    detail=f"{exc.code}: {exc.message}",
                    outcome={"code": exc.code, "detail": exc.detail})


def _insert_stage_instance(session: Session, *, instance_id: str, stage: StageSpec,
                            status: str, skip_reason: str | None,
                            quorum_required: int, opened: bool) -> str:
    # `status` and `opened` are two arguments describing one fact, and the
    # database now says so: `ck_approval_stage_instance_opened_at` is
    # biconditional -- a stage has an open time IF AND ONLY IF it is not
    # SKIPPED. Disagreeing here is a `CheckViolation` naming a row, raised
    # three frames from the call site that got it wrong; this says which
    # argument was wrong, at the call that passed it.
    if (status == STAGE_SKIPPED) is not (not opened):
        raise ValueError(
            f"_insert_stage_instance({status=!r}, {opened=!r}) contradicts "
            f"itself: a SKIPPED stage never opened, and a stage in any other "
            f"status did. The database refuses the row either way -- see "
            f"ck_approval_stage_instance_opened_at.")

    stage_instance_id = _new_id("ASTG")
    # None for a stage that never opened. `approval_stage_instance.opened_at`
    # is nullable precisely so a SKIPPED stage can say "this never ran"
    # instead of carrying a timestamp that implies it was live.
    opened_at = datetime.now(timezone.utc) if opened else None
    due_at = due_at_for(opened_at, stage.sla_hours) if opened_at else None
    session.execute(
        f"""
        INSERT INTO approval_stage_instance
            (stage_instance_id, instance_id, stage_no, parallel_group, status,
             skip_reason, quorum_required, quorum_met, opened_at, due_at, closed_at)
        VALUES (%(id)s, %(instance)s, %(no)s, %(group)s, %(status)s, %(skip)s,
                %(req)s, 0, %(opened)s, %(due)s, %(closed)s)
        """,
        {"id": stage_instance_id, "instance": instance_id, "no": stage.stage_no,
         "group": stage.parallel_group, "status": status, "skip": skip_reason,
         "req": quorum_required, "opened": opened_at, "due": due_at,
         "closed": None if opened or status in STAGE_OPEN_STATUSES
                   else datetime.now(timezone.utc)},
    )
    return stage_instance_id


def resolve_stage_assignees(session: Session, stage: StageSpec,
                             snapshot: Mapping[str, Any],
                             contributors: Iterable[str]) -> AssigneeSet:
    """Section 9.2 step 5, in its required order: expand, delegate, then filter.

    The order is the control. Expanding then filtering then delegating would let
    a delegate of a barred contributor be added *after* the bar had been
    applied, and the object would be approved by somebody acting for the person
    who raised it. Filtering last is what closes that, and
    :func:`apply_contributor_filter` raising rather than returning empty is what
    stops the closed case from being read as "nobody needed to approve".
    """
    roles = [a.get("approver_ref") for a in stage.approvers
             if a.get("approver_kind") == VIA_ROLE]
    members = _role_members(session, roles)
    scopes = _member_scopes(session, [u for users in members.values() for u in users])

    base = expand_role_members(stage, snapshot, members, scopes)          # (a)
    delegations = load_delegations_for(session, [a.user_id for a in base])
    scope_key = _snapshot_str(snapshot, CONDITION_DIMENSIONS["project"])
    with_delegates = expand_delegates(                                    # (b)
        base, delegations, datetime.now(timezone.utc),
        scope_key=scope_key, allow_delegation=stage.allow_delegation)
    return apply_contributor_filter(with_delegates, contributors)         # (c)/(d)


def _open_wave(session: Session, *, instance_id: str, wave: Sequence[int],
                stages_by_no: Mapping[int, StageSpec], snapshot: Mapping[str, Any],
                contributors: Iterable[str]) -> list[str]:
    """Open every stage in one wave together, with its assignments and SLA clock."""
    contributors = list(contributors)
    opened: list[str] = []
    for stage_no in wave:
        stage = stages_by_no[stage_no]
        assignees = resolve_stage_assignees(session, stage, snapshot, contributors)
        needed = required_quorum(stage.quorum_type, stage.quorum_n, len(assignees))
        stage_instance_id = _insert_stage_instance(
            session, instance_id=instance_id, stage=stage, status=STAGE_PENDING,
            skip_reason=None, quorum_required=needed, opened=True)
        for assignee in assignees.assignees:
            session.execute(
                f"""
                INSERT INTO approval_assignment
                    (assignment_id, stage_instance_id, assignee_user_id,
                     assigned_via, delegated_from, state)
                VALUES (%(id)s, %(stage)s, %(user)s, %(via)s, %(from)s, %(state)s)
                """,
                {"id": _new_id("AASG"), "stage": stage_instance_id,
                 "user": assignee.user_id, "via": assignee.assigned_via,
                 "from": assignee.delegated_from, "state": ASSIGN_PENDING},
            )
        opened.append(stage_instance_id)
    return opened


def _set_instance_status(session: Session, instance_id: str, status: str, *,
                          closed: bool, current_stage_no: int | None = None,
                          closing_actor: str | None = None) -> None:
    """The ONE place an instance's status moves -- and therefore the one place
    a closure can be observed.

    Every terminal transition in this module funnels through here with
    ``closed=True``: APPROVED and REJECTED/RETURNED from :func:`_apply_decision`,
    RECALLED and CANCELLED from :func:`_terminate`, SUPERSEDED from
    :func:`resubmit` and :func:`supersede_if_changed`. That is why the
    document write-back hangs off this function rather than off each of them:
    a sixth terminal path added later inherits the write-back by construction
    instead of by somebody remembering.
    """
    session.execute(
        f"""
        UPDATE approval_instance
        SET status = %(status)s,
            closed_at = CASE WHEN %(closed)s THEN now() ELSE closed_at END,
            current_stage_no = COALESCE(%(stage)s, current_stage_no)
        WHERE instance_id = %(id)s
        """,
        {"status": status, "closed": closed, "stage": current_stage_no,
         "id": instance_id})
    if closed:
        _apply_writeback(session, instance_id, closing_actor=closing_actor)


def _apply_writeback(session: Session, instance_id: str, *,
                      closing_actor: str | None = None) -> None:
    """Tell the document its approval closed -- in THIS transaction.

    The seam with the document layer (Wave 4 stream A2), which owns
    ``pg/approval_writeback.py`` and exports exactly::

        def apply_outcome(session, instance, *, closing_actor=None) -> None

    It is handed a CLOSED instance -- read back after the UPDATE, so
    ``status`` and ``closed_at`` are the committed-to values and not the ones
    the caller intended -- and maps that to the document's business status
    through a registry keyed on ``object_type``. It is a no-op for an
    ``object_type`` it does not know, which is what lets this engine route a
    document type before the write-back for it exists.

    Same transaction, no exception handling
    ---------------------------------------
    The call is made inside the caller's transaction and its failures are NOT
    swallowed. An approval that commits while the document it approved stays
    DRAFT is precisely the split-brain a write-back exists to prevent, so if
    the write-back raises, the approval rolls back with it.

    The IMPORT is guarded and the failure to import is not
    ------------------------------------------------------
    ``ImportError`` is tolerated because a deployment that has not shipped
    stream A2's module must still be able to approve things -- the module is
    an addition, not a dependency. Everything else propagates: a module that
    exists and is broken is a defect, and degrading it to "no write-back
    happened" would hide it behind exactly the silence this seam is meant to
    remove.
    """
    try:
        from . import approval_writeback
    except ImportError:
        return
    apply_outcome = getattr(approval_writeback, "apply_outcome", None)
    if not callable(apply_outcome):
        return
    # `closing_actor` is passed, not inferred.
    #
    # The write-back can infer an actor from the instance's own rows, and it
    # gets it wrong here if left to: this function runs BEFORE `decide`
    # appends the closing `approval_action` row, so on a multi-stage approval
    # the newest closing action in the log belongs to the PREVIOUS stage's
    # approver. The document would be attributed to somebody who did not close
    # it -- and `budget.approve_revision` compares that attributed actor
    # against the maker, so the self-approval control would be evaluated
    # against the wrong identity.
    #
    # `decide` knows who is acting. Telling the write-back beats making it
    # guess from a table this function has deliberately not finished writing.
    apply_outcome(session, get_instance(session, instance_id),
                   closing_actor=closing_actor)


# ==========================================================================
# Deciding (contracts 5, 7, 8, 9)
# ==========================================================================


@dataclass(frozen=True)
class DecisionResult:
    """What a decision did. Serialised into ``approval_action.outcome``."""

    instance_id: str
    action: str
    stage_no: int | None
    stage_status: str | None
    instance_status: str
    quorum_required: int | None = None
    quorum_met: int | None = None
    opened_stage_nos: tuple[int, ...] = ()
    replayed: bool = False
    code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id, "action": self.action,
            "stage_no": self.stage_no, "stage_status": self.stage_status,
            "instance_status": self.instance_status,
            "quorum_required": self.quorum_required, "quorum_met": self.quorum_met,
            "opened_stage_nos": list(self.opened_stage_nos),
            "replayed": self.replayed, "code": self.code,
        }


def _find_replay(session: Session, instance_id: str,
                  idempotency_key: str) -> dict[str, Any] | None:
    row = session.fetchone(
        f"SELECT action_id, action, outcome, at, seq FROM approval_action "
        f"WHERE instance_id = %s AND idempotency_key = %s ORDER BY seq LIMIT 1",
        (instance_id, idempotency_key))
    if row is None:
        return None
    outcome = row[2]
    if isinstance(outcome, str):
        try:
            outcome = json.loads(outcome)
        except ValueError:
            outcome = None
    return {"action_id": row[0], "action": row[1], "outcome": outcome or {},
            "at": _iso(row[3]), "seq": row[4]}


def decide(session: Session, *, instance_id: str, actor_user_id: str, action: str,
            idempotency_key: str, object_version: Any,
            reason_code: str | None = None, reason_text: str | None = None,
            acting_for_user_id: str | None = None,
            correlation_id: str | None = None,
            principal: Mapping[str, Any] | None = None,
            permission: str | None = None) -> DecisionResult:
    """Apply one APPROVE / REJECT / RETURN to the caller's open stage.

    The full sequence, in the order it happens and why each step is where it is::

        0. idempotency replay      BEFORE anything else: a replay must return the
                                   ORIGINAL outcome even if the world has since
                                   moved, or a retry of a successful request
                                   turns into a spurious BUDGET_MOVED.
        1. lock budget cells       contract 7 step 1 -- once, first, complete set
        2. lock the instance       contract 7 step 2 -- FOR UPDATE
        3. lock the document row   contract 7 step 3
        4. re-read under the lock  status, version, sha: nothing from the
                                   unlocked read is trusted
        5. staleness / supersede   contract 8
        6. assignment + separation contract 5, both identities, twice over
        7. budget revalidation     contract 7 -- inside the transaction, after
                                   the locks, only where capacity is released
        8. state change
        9. append the action       contract 9 -- advisory lock LAST, same
                                   transaction as the state change

    Two approvers deciding the final stage at the same instant both reach step 2
    and one of them waits there. The winner commits the transition; the loser
    then re-reads at step 4 and finds the stage no longer open, and is answered
    ``STAGE_NOT_OPEN``. Exactly one transition, by construction of the row lock
    rather than by timing.
    """
    if action not in STAGE_DECISION_ACTIONS:
        raise ApprovalError(
            "APPROVAL_ACTION_UNKNOWN",
            f"{action!r} is not a stage decision. Use one of "
            f"{', '.join(STAGE_DECISION_ACTIONS)}; recall, cancel and resubmit "
            f"have their own routes.",
            status=400)
    key = assert_idempotency_key(idempotency_key)

    # ---- step 0: replay -------------------------------------------------
    replay = _find_replay(session, instance_id, key)
    if replay is not None:
        outcome = dict(replay["outcome"])
        return DecisionResult(
            instance_id=instance_id,
            action=outcome.get("action", replay["action"]),
            stage_no=outcome.get("stage_no"),
            stage_status=outcome.get("stage_status"),
            instance_status=outcome.get("instance_status", ""),
            quorum_required=outcome.get("quorum_required"),
            quorum_met=outcome.get("quorum_met"),
            opened_stage_nos=tuple(outcome.get("opened_stage_nos") or ()),
            replayed=True, code=ERR_IDEMPOTENT_REPLAY)

    # ---- step 1: budget cells, once, first, complete set ----------------
    # Unlocked read for the cell set only. Everything it returns is re-read
    # under the lock at step 4; the snapshot it reads is immutable by contract 1.
    preview = get_instance(session, instance_id)
    lock_affected_cells(session, _affected_cells(preview["snapshot"] or {}))

    # ---- step 2: the approval instance ----------------------------------
    instance = get_instance(session, instance_id, for_update=True)
    binding = binding_for(instance["object_type"])

    # ---- step 3: the document row ---------------------------------------
    doc = session.fetchone(  # scope-exempt: locks ONE row by primary key, through a table name from the closed OBJECT_BINDINGS allow-list, named by the approval_instance locked at step 2 and scope-checked by assert_instance_in_scope; reads only its version and maker
        f"SELECT {binding.version_column}, {binding.maker_column} "
        f"FROM {binding.table} WHERE {binding.pk_column} = %s FOR UPDATE",
        (instance["object_id"],))
    if doc is None:
        raise ApprovalError(
            "OBJECT_NOT_FOUND",
            f"{instance['object_type']} {instance['object_id']} no longer exists.",
            status=404)
    current_version, maker = doc[0], doc[1]

    # ---- step 4/5: status, staleness, supersession ----------------------
    if instance["status"] not in (INST_OPEN,):
        raise ApprovalError(
            ERR_STAGE_NOT_OPEN,
            f"Approval instance {instance_id} is {instance['status']}, not OPEN; "
            f"no decision can be taken on it.",
            status=409, detail={"instance_status": instance["status"]})
    assert_object_version_fresh(object_version, current_version,
                                 instance_id=instance_id)

    stage_states = _stage_states(session, instance_id)
    assignment = session.fetchone(
        f"""
        SELECT a.assignment_id, a.stage_instance_id, a.state, a.delegated_from,
               s.stage_no, s.status, s.quorum_required, s.quorum_met
        FROM approval_assignment a
        JOIN approval_stage_instance s ON s.stage_instance_id = a.stage_instance_id
        WHERE s.instance_id = %s AND a.assignee_user_id = %s
              AND s.status = ANY(%s)
        ORDER BY s.stage_no LIMIT 1
        """,
        (instance_id, actor_user_id, list(STAGE_OPEN_STATUSES)))
    if assignment is None:
        raise ApprovalError(
            ERR_NOT_AN_ASSIGNEE,
            f"{actor_user_id} is not an assignee of any open stage of "
            f"{instance_id}. Being permitted to approve is not the same as being "
            f"asked to approve this.",
            status=403)
    (assignment_id, stage_instance_id, assignment_state, delegated_from,
     stage_no, stage_status, quorum_required, quorum_met_count) = assignment
    if assignment_state != ASSIGN_PENDING:
        raise ApprovalError(
            ERR_STAGE_NOT_OPEN,
            f"{actor_user_id} has already acted on stage {stage_no} of "
            f"{instance_id}.",
            status=409, detail={"assignment_state": assignment_state})

    stages = {s.stage_no: s for s in _load_stages(session, instance["definition_id"])}
    stage_spec = stages.get(stage_no)
    assert_reason_supplied(stage_spec or {}, action, reason_code, reason_text)

    # ---- step 6: maker-checker, twice, both identities ------------------
    contributors = contributor_set(session, instance["object_type"],
                                    instance["object_id"],
                                    maker_user_id=instance["maker_user_id"] or maker)
    # (i) the product's own segregation-of-duties check, unmodified and called
    #     exactly as services.py calls it -- a second, independent enforcement
    #     point (contract 5), not a replacement for the first.
    if principal is not None:
        auth_mod.require_separation(
            dict(principal), permission or _maker_checker_permission(instance),
            instance["maker_user_id"] or maker,
            object_label=f"{instance['object_type']} {instance['object_id']}")
    # (ii) the engine's own, which is a superset and covers the delegated case
    #      that `require_separation` cannot see.
    assert_delegation_independent(actor_user_id, acting_for_user_id or delegated_from,
                                   contributors)

    # ---- step 7: budget revalidation, inside the transaction ------------
    waves = compute_waves([stages[no] for no in sorted(stages)])
    projected = {no: state["status"] for no, state in stage_states.items()}
    approvals_after = int(quorum_met_count or 0) + 1
    stage_completes = (action == ACTION_APPROVE
                        and quorum_met(int(quorum_required or 0), approvals_after))
    if stage_completes:
        projected[stage_no] = STAGE_APPROVED
    final_approval = action == ACTION_APPROVE and stage_completes \
        and all_waves_settled(waves, projected)

    if final_approval:
        _revalidate_budget(session, instance)

    # ---- step 8/9: state change, then the chained action ----------------
    result = _apply_decision(
        session, instance=instance, stage_no=stage_no,
        stage_instance_id=stage_instance_id, assignment_id=assignment_id,
        action=action, approvals_after=approvals_after,
        quorum_required=int(quorum_required or 0), waves=waves,
        stages=stages, projected=projected,
        # Who is deciding. `_apply_decision` closes the instance, and the
        # write-back that hangs off that closure must attribute the document
        # change to this person -- not to whoever the action log happens to
        # name, which at this moment is the PREVIOUS stage's approver because
        # the row for THIS decision is appended below, after the close.
        actor_user_id=actor_user_id)

    _append_action(
        session, instance_id=instance_id, stage_instance_id=stage_instance_id,
        actor_user_id=actor_user_id, acting_for_user_id=acting_for_user_id or delegated_from,
        action=action, object_type=instance["object_type"],
        object_id=instance["object_id"],
        detail=(f"{action} on stage {stage_no} "
                f"({result.quorum_met}/{result.quorum_required}); "
                f"instance -> {result.instance_status}"),
        reason_code=reason_code, reason_text=reason_text, idempotency_key=key,
        correlation_id=correlation_id, outcome=result.as_dict())
    return result


def _maker_checker_permission(instance: Mapping[str, Any]) -> str:
    """Which ``auth.MAKER_CHECKER`` permission this object's approval is.

    Read from the snapshot where the caller recorded it, falling back to a
    per-object-type default. A type with no mapping yields ``revision.approve``
    only if it genuinely is one; otherwise the fallback is a permission NOT in
    ``MAKER_CHECKER``, so ``require_separation`` becomes a no-op and the
    engine's own contributor check -- which is a superset and always runs -- is
    what enforces separation. Silently claiming a maker-checker permission the
    object does not have would be the more dangerous default.
    """
    snapshot = instance.get("snapshot") or {}
    if isinstance(snapshot, Mapping):
        declared = snapshot.get("maker_checker_permission")
        if isinstance(declared, str) and declared in auth_mod.PERMISSIONS:
            return declared
    return {"BUDGET_REVISION": "revision.approve"}.get(
        instance.get("object_type", ""), "budget.read")


def _revalidate_budget(session: Session, instance: Mapping[str, Any]) -> None:
    """Contract 7: re-run ``check_availability`` under the locks; ``BUDGET_MOVED``.

    Runs only where the decision **releases spending capacity** -- the final
    approval, the moment the document becomes postable. An intermediate approval
    releases nothing, and failing it on a budget movement that a later approver
    might legitimately approve anyway would stall workflows for no control
    benefit.

    ``check_availability`` is called, not reimplemented: it is the same verdict
    the routing snapshot recorded, computed the same way, so the comparison is
    between two like things.
    """
    for check in _budget_checks(instance.get("snapshot") or {}):
        wbs_id = check.get("wbs_id")
        head_id = check.get("budget_head_id")
        amount = check.get("requested_paise", check.get("amount_paise"))
        if not wbs_id or not head_id or amount is None:
            continue
        current = budget_mod.check_availability(session, wbs_id, head_id, int(amount))
        if budget_moved(check, current):
            raise ApprovalError(
                ERR_BUDGET_MOVED,
                f"Availability for ({wbs_id}, {head_id}) moved after this object "
                f"was routed: {check.get('available_paise')} paise were available "
                f"when the approvers were asked, {current['available_paise']} are "
                f"available now. The approval is refused rather than applied "
                f"against stale numbers; re-check and re-submit.",
                status=409,
                detail={"wbs_id": wbs_id, "budget_head_id": head_id,
                        "available_at_routing_paise": check.get("available_paise"),
                        "available_now_paise": current["available_paise"],
                        "requested_paise": int(amount)})


def _apply_decision(session: Session, *, instance: Mapping[str, Any], stage_no: int,
                     stage_instance_id: str, assignment_id: str, action: str,
                     approvals_after: int, quorum_required: int,
                     waves: Sequence[Sequence[int]],
                     stages: Mapping[int, StageSpec],
                     projected: Mapping[int, str],
                     actor_user_id: str) -> DecisionResult:
    instance_id = instance["instance_id"]
    session.execute(
        f"UPDATE approval_assignment SET state = %s WHERE assignment_id = %s",
        (ASSIGN_ACTED, assignment_id))

    if action in (ACTION_REJECT, ACTION_RETURN):
        stage_status = STAGE_REJECTED if action == ACTION_REJECT else STAGE_RETURNED
        instance_status = INST_REJECTED if action == ACTION_REJECT else INST_RETURNED
        session.execute(
            f"UPDATE approval_stage_instance SET status = %s, closed_at = now() "
            f"WHERE stage_instance_id = %s", (stage_status, stage_instance_id))
        # Everybody else's assignment is withdrawn: the decision is taken and
        # asking them to act would be asking for a decision that cannot matter.
        _withdraw_open_assignments(session, instance_id)
        _set_instance_status(session, instance_id, instance_status, closed=True,
                              closing_actor=actor_user_id)
        return DecisionResult(instance_id=instance_id, action=action,
                               stage_no=stage_no, stage_status=stage_status,
                               instance_status=instance_status,
                               quorum_required=quorum_required,
                               quorum_met=approvals_after)

    session.execute(
        f"UPDATE approval_stage_instance SET quorum_met = %s WHERE stage_instance_id = %s",
        (approvals_after, stage_instance_id))
    if not quorum_met(quorum_required, approvals_after):
        return DecisionResult(instance_id=instance_id, action=action,
                               stage_no=stage_no, stage_status=STAGE_PENDING,
                               instance_status=INST_OPEN,
                               quorum_required=quorum_required,
                               quorum_met=approvals_after)

    session.execute(
        f"UPDATE approval_stage_instance SET status = %s, closed_at = now() "
        f"WHERE stage_instance_id = %s", (STAGE_APPROVED, stage_instance_id))
    session.execute(
        f"UPDATE approval_assignment SET state = %s WHERE stage_instance_id = %s "
        f"AND state = %s", (ASSIGN_WITHDRAWN, stage_instance_id, ASSIGN_PENDING))

    if all_waves_settled(waves, projected):
        _set_instance_status(session, instance_id, INST_APPROVED, closed=True,
                              closing_actor=actor_user_id)
        return DecisionResult(instance_id=instance_id, action=action,
                               stage_no=stage_no, stage_status=STAGE_APPROVED,
                               instance_status=INST_APPROVED,
                               quorum_required=quorum_required,
                               quorum_met=approvals_after)

    upcoming = next_wave(waves, projected)
    opened: list[int] = []
    if upcoming:
        snapshot = instance["snapshot"] or {}
        contributors = contributor_set(session, instance["object_type"],
                                        instance["object_id"],
                                        maker_user_id=instance["maker_user_id"])
        try:
            _open_wave(session, instance_id=instance_id, wave=upcoming,
                        stages_by_no=stages, snapshot=snapshot,
                        contributors=contributors)
            opened = list(upcoming)
            _set_instance_status(session, instance_id, INST_OPEN, closed=False,
                                  current_stage_no=upcoming[0])
        except NoIndependentApprover:
            # The NEXT stage has nobody. The stage just approved stands; the
            # instance stops and waits for an administrator. It is emphatically
            # not approved, and the stage is not skipped past.
            _set_instance_status(session, instance_id, INST_EXCEPTION_PENDING,
                                  closed=False)
            return DecisionResult(instance_id=instance_id, action=action,
                                   stage_no=stage_no, stage_status=STAGE_APPROVED,
                                   instance_status=INST_EXCEPTION_PENDING,
                                   quorum_required=quorum_required,
                                   quorum_met=approvals_after)

    return DecisionResult(instance_id=instance_id, action=action, stage_no=stage_no,
                           stage_status=STAGE_APPROVED, instance_status=INST_OPEN,
                           quorum_required=quorum_required,
                           quorum_met=approvals_after,
                           opened_stage_nos=tuple(opened))


def _withdraw_open_assignments(session: Session, instance_id: str) -> None:
    session.execute(
        f"""
        UPDATE approval_assignment SET state = %s
        WHERE state = %s AND stage_instance_id IN (
            SELECT stage_instance_id FROM approval_stage_instance WHERE instance_id = %s)
        """,
        (ASSIGN_WITHDRAWN, ASSIGN_PENDING, instance_id))


# ==========================================================================
# Recall, cancel, resubmit, supersede
# ==========================================================================


def _terminate(session: Session, *, instance_id: str, actor_user_id: str,
                action: str, status: str, reason_text: str | None,
                require_maker: bool,
                correlation_id: str | None = None) -> DecisionResult:
    preview = get_instance(session, instance_id)
    lock_affected_cells(session, _affected_cells(preview["snapshot"] or {}))
    instance = get_instance(session, instance_id, for_update=True)

    if instance["status"] != INST_OPEN:
        raise ApprovalError(
            ERR_STAGE_NOT_OPEN,
            f"Approval instance {instance_id} is {instance['status']}; only an "
            f"OPEN instance can be {action.lower()}ed.",
            status=409, detail={"instance_status": instance["status"]})
    if require_maker and actor_user_id != instance["maker_user_id"]:
        raise ApprovalError(
            "NOT_THE_MAKER",
            f"Only {instance['maker_user_id']}, who raised this object, may "
            f"{action.lower()} it.",
            status=403)
    if not (reason_text and reason_text.strip()):
        raise ApprovalError(
            ERR_REASON_REQUIRED,
            f"A {action.lower()} needs a reason: the approvers already asked to "
            f"act on this must be told why they no longer need to.",
            status=400)

    session.execute(
        f"UPDATE approval_stage_instance SET status = %s, closed_at = now() "
        f"WHERE instance_id = %s AND status = ANY(%s)",
        (STAGE_RETURNED, instance_id, list(STAGE_OPEN_STATUSES)))
    _withdraw_open_assignments(session, instance_id)
    _set_instance_status(session, instance_id, status, closed=True,
                          closing_actor=actor_user_id)
    _append_action(session, instance_id=instance_id, stage_instance_id=None,
                    actor_user_id=actor_user_id, acting_for_user_id=None,
                    action=action, object_type=instance["object_type"],
                    object_id=instance["object_id"],
                    detail=f"{action} by {actor_user_id}: {reason_text}",
                    reason_text=reason_text, correlation_id=correlation_id)
    return DecisionResult(instance_id=instance_id, action=action, stage_no=None,
                           stage_status=None, instance_status=status)


def recall(session: Session, *, instance_id: str, actor_user_id: str,
            reason_text: str,
            correlation_id: str | None = None) -> DecisionResult:
    """The maker withdraws their own object from approval. Maker only."""
    return _terminate(session, instance_id=instance_id, actor_user_id=actor_user_id,
                       action=ACTION_RECALL, status=INST_RECALLED,
                       reason_text=reason_text, require_maker=True,
                       correlation_id=correlation_id)


def cancel(session: Session, *, instance_id: str, actor_user_id: str,
            reason_text: str,
            correlation_id: str | None = None) -> DecisionResult:
    """An administrator ends an instance. Not maker-restricted, by design.

    Cancellation is the escape hatch for an ``EXCEPTION_PENDING`` instance
    nobody can act on, which by definition is one the maker cannot resolve.
    """
    return _terminate(session, instance_id=instance_id, actor_user_id=actor_user_id,
                       action=ACTION_CANCEL, status=INST_CANCELLED,
                       reason_text=reason_text, require_maker=False,
                       correlation_id=correlation_id)


def resubmit(session: Session, *, instance_id: str, actor_user_id: str,
              reason_text: str, snapshot: Mapping[str, Any] | None = None,
              object_version: Any = None, business_date: date | None = None,
              correlation_id: str | None = None) -> dict[str, Any]:
    """Re-route a returned, recalled or superseded object as a NEW instance.

    The old instance is never reopened. Contract 1 pins an instance to the
    document it was routed for; a corrected document is a different document,
    and re-using the instance would silently re-point that pin. So the new
    instance carries ``supersedes_instance_id`` and the two are readable as a
    chain.

    Two callers, one of which cannot supply a snapshot
    --------------------------------------------------
    The document layer calls this after an EDIT and passes the corrected
    ``snapshot`` and its new ``object_version``; that is the case Contract 1 is
    written for and nothing about it changes.

    ``POST /api/approvals/{id}/resubmit`` cannot. Contract 3 freezes that
    request body as ``{"reason_text"}``, the approvals screen codes against it,
    and this router has no way to build a ``BUDGET_REVISION`` snapshot -- that
    is the document layer's knowledge, not the engine's. Its real use is the
    other resubmission: an object held EXCEPTION_PENDING because its
    *configuration* was wrong, re-routed once an administrator has fixed the
    workflow. The document did not change; the route it should take did.

    So both are omissible, and omitting them means "re-route the SAME document,
    unchanged". The engine then re-reads the document row and refuses through
    :func:`assert_object_version_fresh` if its version has moved -- the same
    frozen ``OBJECT_VERSION_STALE`` a decision gets, for the same reason. That
    refusal is the load-bearing part: without it, a document edited since it
    was returned would be re-routed on the stale snapshot the old instance
    carried, which is precisely the "approved the amount nobody looked at"
    failure :func:`supersede_if_changed` exists to prevent. The caller who has
    a fresh snapshot passes one; the caller who has none is told to.
    """
    instance = get_instance(session, instance_id, for_update=True)
    if instance["status"] not in (INST_RETURNED, INST_RECALLED, INST_SUPERSEDED,
                                   INST_EXCEPTION_PENDING):
        raise ApprovalError(
            ERR_STAGE_NOT_OPEN,
            f"Approval instance {instance_id} is {instance['status']}; only a "
            f"RETURNED, RECALLED, SUPERSEDED or EXCEPTION_PENDING instance can "
            f"be resubmitted.",
            status=409, detail={"instance_status": instance["status"]})

    if snapshot is None:
        # "Re-route this document unchanged." Read the document's CURRENT
        # version under the same binding `decide` uses and refuse if it has
        # moved: the snapshot on the old instance describes the document as it
        # was, and re-routing an edited document on it would decide the route
        # from numbers nobody has seen since.
        binding = binding_for(instance["object_type"])
        row = session.fetchone(  # scope-exempt: reads ONE row by primary key, through a table name from the closed OBJECT_BINDINGS allow-list, named by an approval_instance get_instance has already scope-checked; reads only its version
            f"SELECT {binding.version_column} FROM {binding.table} "
            f"WHERE {binding.pk_column} = %s",
            (instance["object_id"],))
        if row is None:
            raise ApprovalError(
                "OBJECT_NOT_FOUND",
                f"{instance['object_type']} {instance['object_id']} no longer "
                f"exists and cannot be resubmitted.",
                status=404)
        assert_object_version_fresh(instance["object_version"], row[0],
                                     instance_id=instance_id)
        snapshot = instance["snapshot"] or {}
        if object_version is None:
            object_version = instance["object_version"]
    elif object_version is None:
        raise ApprovalError(
            "APPROVAL_RESUBMIT_INVALID",
            "A resubmission that supplies a corrected snapshot must also say "
            "which object_version that snapshot is of; an instance pinned to "
            "no version cannot be checked for staleness later (Contract 1).",
            status=400)

    if instance["status"] != INST_SUPERSEDED:
        _set_instance_status(session, instance_id, INST_SUPERSEDED, closed=True)
    _append_action(session, instance_id=instance_id, stage_instance_id=None,
                    actor_user_id=actor_user_id, acting_for_user_id=None,
                    action=ACTION_RESUBMIT, object_type=instance["object_type"],
                    object_id=instance["object_id"],
                    detail=f"Resubmitted by {actor_user_id}: {reason_text}",
                    reason_text=reason_text, correlation_id=correlation_id)
    return open_instance(
        session, object_type=instance["object_type"], object_id=instance["object_id"],
        object_version=object_version, snapshot=snapshot,
        maker_user_id=instance["maker_user_id"], business_date=business_date,
        correlation_id=correlation_id or instance["correlation_id"],
        supersedes_instance_id=instance_id)


def supersede_if_changed(session: Session, *, instance_id: str,
                          current_snapshot: Mapping[str, Any],
                          object_version: Any, actor_user_id: str = "SYSTEM",
                          business_date: date | None = None) -> dict[str, Any] | None:
    """Contract 8: a document that changed under an open instance is re-routed.

    Returns the new instance when a change was detected, ``None`` when the
    document is unchanged. **Never silently approved** and never quietly
    re-pointed: the old instance is closed ``SUPERSEDED``, its assignments are
    withdrawn, and the object goes back through
    :func:`~app.backend.pg.approval_rules.resolve_route` from the top -- because
    an edit can move it across a routing threshold, and approving the new amount
    on the old route is the failure this exists to prevent.
    """
    instance = get_instance(session, instance_id, for_update=True)
    if instance["status"] != INST_OPEN:
        return None
    normalised = normalise_snapshot(current_snapshot,
                                     object_type=instance["object_type"],
                                     object_id=instance["object_id"])
    if content_sha(normalised) == instance["object_content_sha"]:
        return None

    session.execute(
        f"UPDATE approval_stage_instance SET status = %s, closed_at = now() "
        f"WHERE instance_id = %s AND status = ANY(%s)",
        (STAGE_RETURNED, instance_id, list(STAGE_OPEN_STATUSES)))
    _withdraw_open_assignments(session, instance_id)
    _set_instance_status(session, instance_id, INST_SUPERSEDED, closed=True)
    _append_action(
        session, instance_id=instance_id, stage_instance_id=None,
        actor_user_id=actor_user_id, acting_for_user_id=None,
        action=ACTION_SUPERSEDE, object_type=instance["object_type"],
        object_id=instance["object_id"],
        detail=(f"Document content changed under an open instance "
                f"({instance['object_content_sha'][:12]} -> "
                f"{content_sha(normalised)[:12]}); superseded and re-routed"))
    return open_instance(
        session, object_type=instance["object_type"], object_id=instance["object_id"],
        object_version=object_version, snapshot=current_snapshot,
        maker_user_id=instance["maker_user_id"], business_date=business_date,
        correlation_id=instance["correlation_id"], supersedes_instance_id=instance_id)


# ==========================================================================
# SLA and escalation (contract 3's /sla route)
# ==========================================================================


def escalate_overdue(session: Session, *, now: datetime | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
    """Escalate every open stage past its ``escalate_after_hours``.

    The escalation target is added as an assignment with
    ``assigned_via='ESCALATION'`` and the stage moves to ``ESCALATED``. The
    original assignees keep their PENDING assignments -- escalation, like
    delegation, **adds capacity and does not remove accountability**, and the
    quorum is not lowered to make the escalation look like progress.

    A stage is escalated at most once: the guard is ``escalated_at IS NULL``, so
    a job that runs every five minutes does not add the same approver every five
    minutes.
    """
    now = now or datetime.now(timezone.utc)
    rows = session.fetchall(
        f"""
        SELECT s.stage_instance_id, s.instance_id, s.stage_no, s.opened_at,
               i.object_type, i.object_id, i.definition_id, i.snapshot
        FROM approval_stage_instance s
        JOIN approval_instance i ON i.instance_id = s.instance_id
        WHERE s.status = %s AND s.escalated_at IS NULL AND i.status = %s
        ORDER BY s.opened_at
        LIMIT %s
        """,
        (STAGE_PENDING, INST_OPEN, limit))

    escalated: list[dict[str, Any]] = []
    for (stage_instance_id, instance_id, stage_no, opened_at, object_type,
         object_id, definition_id, snapshot) in rows:
        stage = {s.stage_no: s for s in _load_stages(session, definition_id)}.get(stage_no)
        if stage is None or not is_escalation_due(opened_at, stage.escalate_after_hours, now):
            continue
        targets = _escalation_targets(session, stage, snapshot or {})
        if not targets:
            continue
        contributors = contributor_set(session, object_type, object_id)
        added = [t for t in targets if t not in contributors]
        if not added:
            # Escalating to a contributor would hand the decision to somebody
            # already barred from it. The stage stays PENDING and overdue, which
            # is visible on the SLA report, rather than being marked ESCALATED to
            # a person who can never act.
            continue
        for user_id in added:
            session.execute(
                f"""
                INSERT INTO approval_assignment
                    (assignment_id, stage_instance_id, assignee_user_id,
                     assigned_via, delegated_from, state)
                VALUES (%(id)s, %(stage)s, %(user)s, %(via)s, NULL, %(state)s)
                ON CONFLICT (stage_instance_id, assignee_user_id) DO NOTHING
                """,
                {"id": _new_id("AASG"), "stage": stage_instance_id, "user": user_id,
                 "via": VIA_ESCALATION, "state": ASSIGN_PENDING})
        session.execute(
            f"UPDATE approval_stage_instance SET status = %s, escalated_at = %s "
            f"WHERE stage_instance_id = %s",
            (STAGE_ESCALATED, now, stage_instance_id))
        _append_action(
            session, instance_id=instance_id, stage_instance_id=stage_instance_id,
            actor_user_id="SYSTEM", acting_for_user_id=None, action=ACTION_ESCALATE,
            object_type=object_type, object_id=object_id,
            detail=(f"Stage {stage_no} passed {stage.escalate_after_hours}h; "
                    f"escalated to {', '.join(sorted(added))}"))
        escalated.append({"instance_id": instance_id, "stage_no": stage_no,
                          "escalated_to": sorted(added)})
    return escalated


def _escalation_targets(session: Session, stage: StageSpec,
                         snapshot: Mapping[str, Any]) -> list[str]:
    """Resolve ``escalate_to`` -- ``{"role": ...}`` or ``{"user": ...}``."""
    target = stage.escalate_to or {}
    if not isinstance(target, Mapping):
        return []
    user = target.get("user") or target.get("user_id")
    if isinstance(user, str) and user:
        return [user]
    role = target.get("role")
    if isinstance(role, str) and role:
        members = _role_members(session, [role]).get(role, [])
        scopes = _member_scopes(session, members)
        scope_expr = target.get("scope_expr")
        return sorted(u for u in members
                      if scope_matches(scope_expr, snapshot, scopes.get(u)))
    return []


def overdue_stages(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Open stages past ``due_at`` -- what ``GET /api/approvals/sla`` reports."""
    now = now or datetime.now(timezone.utc)
    rows = session.fetchall(
        f"""
        SELECT s.instance_id, s.stage_no, s.status, s.opened_at, s.due_at,
               s.escalated_at, i.object_type, i.object_id, i.entity_id, i.project_id
        FROM approval_stage_instance s
        JOIN approval_instance i ON i.instance_id = s.instance_id
        WHERE s.status = ANY(%s) AND s.due_at IS NOT NULL AND s.due_at < %s
        ORDER BY s.due_at
        """,
        (list(STAGE_OPEN_STATUSES), now))
    keys = ("instance_id", "stage_no", "status", "opened_at", "due_at",
            "escalated_at", "object_type", "object_id", "entity_id", "project_id")
    return [{k: _iso(v) for k, v in zip(keys, row)} for row in rows]


# ==========================================================================
# The read surface: inbox, SLA report, timeline (contract 3 + contract 6)
#
# These three are what `api/approvals.py` calls for `GET /inbox`, `GET /sla`
# and `GET /{id}/timeline`. Each returns the SAME envelope the router's
# `_page` normalises -- `{"items", "next_cursor", "has_more"}` -- with a
# PLAIN-text cursor the router base64-encodes on the way out and decodes on
# the way back in, so a client never composes one.
#
# Contract 6, both halves, and they are different halves:
#
#   * scope    -- compiled by `repo.query`'s `{scope}` token, from the
#                 session's own resolved grants. `approval_instance` carries
#                 `entity_id` and `project_id` denormalised at creation
#                 exactly so this is a direct predicate rather than a join
#                 through the document.
#   * audience -- `assigned_to`. A caller sees their OWN assignments, not the
#                 estate's, unless they hold `approval.configure`; the router
#                 passes `assigned_to=None` only for such a caller, and that
#                 view is still scope-bounded. `approval.read` is held by
#                 every role, so an unfiltered default would have turned a
#                 personal inbox into an estate-wide one for everybody.
#
# Plant and location have no column on `approval_instance`. They are reached
# through a LEFT JOIN to `project` rather than waived, because waiving them
# would let a caller restricted to one plant read every plant's approvals. An
# instance with no `project_id` therefore has a NULL plant and location, and
# `(column = ANY(...))` is NULL -- so it does not match, and a plant-scoped
# caller does not see it. That is fail-closed, which is the direction to fail.
# ==========================================================================

#: The scope mapping every read below compiles. All four dimensions are named:
#: `repo.compile_scope` refuses a restricted dimension the query merely forgot,
#: and that refusal is the reason a forgotten dimension cannot widen access.
_INSTANCE_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "i.entity_id",
    "project": "i.project_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}

DEFAULT_PAGE = 50
MAX_PAGE = 200


def _page_size(limit: int | None) -> int:
    try:
        value = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        value = DEFAULT_PAGE
    return min(max(value, 1), MAX_PAGE)


#: Instance statuses a person can still do something about. `state=` on the
#: inbox accepts either one of these literal statuses or the word `OPEN` used
#: loosely for "anything still live", which is what a screen's default tab
#: means; anything else is passed through as an exact status match.
_LIVE_INSTANCE_STATUSES = (INST_OPEN, INST_EXCEPTION_PENDING)


def list_inbox(session: Session, *, actor_user_id: str | None = None,
                assigned_to: str | None = None, state: str | None = None,
                object_type: str | None = None, cursor: str | None = None,
                limit: int | None = None) -> dict[str, Any]:
    """``GET /api/approvals/inbox`` -- the instances this caller must act on.

    ``assigned_to`` restricts to instances carrying a PENDING assignment for
    that user on a stage that is still open. Not "an assignment of any state":
    an approver who has already acted has nothing left to do, and leaving their
    own acted-on instances in their inbox is how an inbox stops being read.

    ``actor_user_id`` is accepted and used only to report whose inbox this is
    (and, when ``assigned_to`` is omitted, it is NOT silently substituted --
    that decision belongs to the router, which knows whether the caller holds
    ``approval.configure``). It is spelled ``actor_user_id`` and not ``actor``
    because the requesting user has exactly one name across this engine; see
    the note at the top of ``api/approvals.py``'s engine adapter.

    The cursor is a keyset over ``(opened_at, instance_id)`` descending, which
    is unique because ``instance_id`` is the primary key. Newest first, because
    an inbox is read from the top.
    """
    from . import repo

    size = _page_size(limit)
    params: dict[str, Any] = {"lim": size + 1}
    clauses: list[str] = ["{scope}"]

    if object_type:
        clauses.append("i.object_type = %(otype)s")
        params["otype"] = object_type
    if state:
        wanted = str(state).strip().upper()
        if wanted in ("OPEN", "LIVE", "PENDING"):
            clauses.append("i.status = ANY(%(states)s)")
            params["states"] = list(_LIVE_INSTANCE_STATUSES)
        else:
            clauses.append("i.status = %(state)s")
            params["state"] = wanted
    if assigned_to:
        clauses.append(
            """EXISTS (SELECT 1
                       FROM approval_assignment a
                       JOIN approval_stage_instance s
                         ON s.stage_instance_id = a.stage_instance_id
                       WHERE s.instance_id = i.instance_id
                         AND a.assignee_user_id = %(assignee)s
                         AND a.state = %(pending)s
                         AND s.status = ANY(%(open_stages)s))""")
        params["assignee"] = assigned_to
        params["pending"] = ASSIGN_PENDING
        params["open_stages"] = list(STAGE_OPEN_STATUSES)
    if cursor:
        at, _, instance_id = str(cursor).partition("\x1f")
        clauses.append(
            "(i.opened_at, i.instance_id) < (%(c_at)s::timestamptz, %(c_id)s)")
        params["c_at"] = at
        params["c_id"] = instance_id

    rows = repo.query(
        session,
        f"""
        SELECT i.instance_id, i.object_type, i.object_id, i.object_version,
               i.status, i.current_stage_no, i.definition_id,
               i.definition_version, i.maker_user_id, i.entity_id, i.project_id,
               i.opened_at, i.closed_at, i.correlation_id
        FROM approval_instance i
        LEFT JOIN project p ON p.project_id = i.project_id
        WHERE {' AND '.join(clauses)}
        ORDER BY i.opened_at DESC, i.instance_id DESC
        LIMIT %(lim)s
        """,
        params,
        columns=_INSTANCE_SCOPE_COLUMNS,
    )
    keys = ("instance_id", "object_type", "object_id", "object_version",
            "status", "current_stage_no", "definition_id", "definition_version",
            "maker_user_id", "entity_id", "project_id", "opened_at",
            "closed_at", "correlation_id")
    items = [{k: _iso(v) for k, v in zip(keys, row)} for row in rows]
    for item in items:
        item["inbox_of"] = assigned_to or actor_user_id
    return _paginate(items, size,
                      lambda row: f"{row['opened_at']}\x1f{row['instance_id']}")


def list_sla(session: Session, *, actor_user_id: str | None = None,
              assigned_to: str | None = None, overdue: bool = False,
              cursor: str | None = None, limit: int | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """``GET /api/approvals/sla?overdue=true`` -- open stages against their clock.

    ``overdue=False`` reports every open stage that HAS a clock, not only the
    ones that have run out. A stage due in an hour is the one worth chasing;
    reporting only breaches turns an SLA report into a post-mortem.

    ``overdue_at`` is computed against ``due_at`` in the database rather than in
    Python, so the report cannot disagree with itself between two rows read a
    moment apart, and ``escalated_at`` is carried so a reader can tell a stage
    that has already been escalated from one that is simply late.
    """
    from . import repo

    size = _page_size(limit)
    moment = now or datetime.now(timezone.utc)
    params: dict[str, Any] = {"lim": size + 1, "now": moment,
                              "open_stages": list(STAGE_OPEN_STATUSES),
                              "live": list(_LIVE_INSTANCE_STATUSES)}
    clauses = ["{scope}", "s.status = ANY(%(open_stages)s)",
               "i.status = ANY(%(live)s)", "s.due_at IS NOT NULL"]
    if overdue:
        clauses.append("s.due_at < %(now)s")
    if assigned_to:
        clauses.append(
            """EXISTS (SELECT 1 FROM approval_assignment a
                       WHERE a.stage_instance_id = s.stage_instance_id
                         AND a.assignee_user_id = %(assignee)s
                         AND a.state = %(pending)s)""")
        params["assignee"] = assigned_to
        params["pending"] = ASSIGN_PENDING
    if cursor:
        at, _, stage_instance_id = str(cursor).partition("\x1f")
        clauses.append(
            "(s.due_at, s.stage_instance_id) > (%(c_at)s::timestamptz, %(c_id)s)")
        params["c_at"] = at
        params["c_id"] = stage_instance_id

    rows = repo.query(
        session,
        f"""
        SELECT s.stage_instance_id, s.instance_id, s.stage_no, s.status,
               s.opened_at, s.due_at, s.escalated_at, (s.due_at < %(now)s),
               i.object_type, i.object_id, i.entity_id, i.project_id,
               i.maker_user_id
        FROM approval_stage_instance s
        JOIN approval_instance i ON i.instance_id = s.instance_id
        LEFT JOIN project p ON p.project_id = i.project_id
        WHERE {' AND '.join(clauses)}
        ORDER BY s.due_at, s.stage_instance_id
        LIMIT %(lim)s
        """,
        params,
        columns=_INSTANCE_SCOPE_COLUMNS,
    )
    keys = ("stage_instance_id", "instance_id", "stage_no", "status",
            "opened_at", "due_at", "escalated_at", "overdue", "object_type",
            "object_id", "entity_id", "project_id", "maker_user_id")
    items = [{k: _iso(v) for k, v in zip(keys, row)} for row in rows]
    for item in items:
        item["overdue"] = bool(item["overdue"])
        item["reported_for"] = assigned_to or actor_user_id
    return _paginate(items, size,
                      lambda row: f"{row['due_at']}\x1f{row['stage_instance_id']}")


def get_timeline(session: Session, *, instance_id: str,
                  actor_user_id: str | None = None,
                  cursor: str | None = None,
                  limit: int | None = None) -> dict[str, Any]:
    """``GET /api/approvals/{id}/timeline`` -- what happened, and what did not.

    Two kinds of entry, merged chronologically:

    * ``kind="ACTION"``  -- one ``approval_action`` row. The append-only,
      hash-chained record: who acted, for whom, with what reason.
    * ``kind="STAGE"``   -- one ``approval_stage_instance`` row that never ran.
      Contract 2 is explicit that a stage whose ``applies_when`` was false is
      **recorded SKIPPED with a skip_reason, never omitted**, because an
      auditor must see the control that did not fire. A SKIPPED stage writes no
      action, so if the timeline were the action stream alone the one thing
      Contract 2 insists on being visible would be the one thing missing.

    Scope is enforced by reading the instance through :func:`get_instance`
    first, which is where :func:`assert_instance_in_scope` lives -- so an
    out-of-scope instance is refused before any history is assembled, rather
    than being assembled and then filtered.

    The cursor is an OFFSET, not a keyset, and that is defensible here and
    nowhere else in this module: a timeline is one instance's own history,
    ``approval_action`` is append-only by trigger AND by privilege, and a
    SKIPPED stage row is written once at routing and never moved. Entries
    therefore cannot be deleted or reordered underneath a walk, which is the
    only thing offset pagination is unsafe against.
    """
    instance = get_instance(session, instance_id)
    size = _page_size(limit)

    actions = session.fetchall(
        """
        SELECT seq, at, actor_user_id, acting_for_user_id, action, reason_code,
               reason_text, stage_instance_id, idempotency_key, outcome,
               entry_hash
        FROM approval_action WHERE instance_id = %s ORDER BY seq
        """,
        (instance_id,))
    entries: list[dict[str, Any]] = [
        {"kind": "ACTION", "seq": row[0], "at": _iso(row[1]),
         "actor_user_id": row[2], "acting_for_user_id": row[3],
         "action": row[4], "reason_code": row[5], "reason_text": row[6],
         "stage_instance_id": row[7], "idempotency_key": row[8],
         "outcome": row[9], "entry_hash": row[10]}
        for row in actions
    ]

    for stage_no, state in sorted(_stage_states(session, instance_id).items()):
        if state["status"] != STAGE_SKIPPED:
            continue
        entries.append({
            "kind": "STAGE", "seq": None,
            "at": _iso(state["closed_at"] or state["opened_at"]),
            "actor_user_id": None, "acting_for_user_id": None,
            "action": STAGE_SKIPPED, "stage_no": stage_no,
            "stage_instance_id": state["stage_instance_id"],
            "skip_reason": state["skip_reason"],
            "reason_code": None, "reason_text": None,
        })

    # A SKIPPED stage and the routing action that recorded it share an instant,
    # so `seq` breaks the tie and puts the action -- which has one -- first.
    entries.sort(key=lambda e: (e["at"] or "", e["seq"] if e["seq"] else 0))

    offset = 0
    if cursor:
        try:
            offset = max(int(cursor), 0)
        except (TypeError, ValueError):
            offset = 0
    window = entries[offset:offset + size + 1]
    page = _paginate(window, size, lambda _row: str(offset + size))
    page["instance_id"] = instance_id
    page["instance_status"] = instance["status"]
    page["object_type"] = instance["object_type"]
    page["object_id"] = instance["object_id"]
    page["viewed_by"] = actor_user_id
    return page


def _paginate(items: list[dict[str, Any]], limit: int, cursor_of) -> dict[str, Any]:
    """The Contract 3 list envelope. See ``approval_rules.paginate`` -- same
    rule, restated here rather than imported across the two halves of the
    engine, because ``approvals.py`` already imports enough from that module
    that one more name would obscure which half owns what."""
    has_more = len(items) > limit
    page = items[:limit]
    next_cursor = cursor_of(page[-1]) if (has_more and page) else None
    return {"items": page, "next_cursor": next_cursor, "has_more": has_more}
