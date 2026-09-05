"""What a closed approval instance does to the document it decided.

Wave 4 stream A2. This is the second half of the gap ``budget.submit_revision``
/ ``budget.submit_transfer`` opened: the engine could route an object and reach
a decision, and nothing carried that decision back, so an APPROVED instance sat
next to a document that never left DRAFT.

The whole module is one function and one registry::

    apply_outcome(session, instance) -> None

called by ``approvals.py`` at the single point an instance closes, inside the
engine's own transaction. There is deliberately no second call path: a
write-back that can be triggered from two places is a write-back that can
disagree with itself about which one ran.

Two namespaces, and why they must not touch
-------------------------------------------
``research/30_contracts/C15_approval_statuses.json`` is explicit that
approval-engine statuses are INTERNAL, surfaced on approval screens only, and
"mapped to a C3 business status on the underlying object". So this module is
the boundary: an instance status goes in, a status from the frozen 21 in
``C3_statuses.json`` comes out, and no engine status is ever written onto a
document. :data:`INSTANCE_TO_BUSINESS_STATUS` is that mapping, written down
once so a test can check every target against C3 rather than trusting a
reading of the code.

Authority for each entry, and one deviation
-------------------------------------------
C15 states ``maps_to_business_status`` for six of the seven instance statuses,
and this table follows it exactly except in one place:

===============  =============  ==========================================
instance         written        authority
===============  =============  ==========================================
APPROVED         APPROVED       C15 instance/APPROVED
REJECTED         REJECTED       C15 instance/REJECTED
RETURNED         RETURNED       C15 instance/RETURNED
RECALLED         DRAFT          C15 instance/RECALLED
CANCELLED        DRAFT          not in C15; DRAFT is the only C3 status the
                                schema admits for "back with the maker"
SUPERSEDED       (unchanged)    C15 gives it ``null``: superseding routes a
                                new instance, it decides nothing
OPEN             -- refused --  not a closed instance; a caller bug
EXCEPTION_PENDING -- refused --  not closed either: the object is HELD
===============  =============  ==========================================

**RETURNED was a deviation until migration 009, and is not one now.** 003 gave
both documents ``CHECK (status IN ('DRAFT','APPROVED','REJECTED','CANCELLED'))``,
so RETURNED could not be stored and this module wrote DRAFT, keeping the real
outcome in ``decision_note`` prose. DRAFT was *true* of a returned revision --
editable, no control effect -- but it was less informative than the fact it
replaced: a reader could not tell a revision that was sent back from one never
submitted, and prose is not a status. ``009_document_approval_states.sql``
widens both domains to include SUBMITTED and RETURNED, both already among
C3's frozen 21, so the table now follows C15 without exception.

**CANCELLED is not a C3 status.** The DDL admits ``'CANCELLED'`` on both
documents, and C3's frozen 21 does not contain it. Writing it would leak a
status the business screens have no label for, so an instance cancelled by an
administrator returns the document to DRAFT like a recall does. Also reported.

Idempotency
-----------
A retry, a replayed decision, or a second call from a future call site must
leave the same state. The order of the checks is what buys that:

1. an unknown ``object_type`` is a silent no-op (so adding a routable type
   later cannot crash a flow that predates it);
2. a target of ``None`` (SUPERSEDED) is a no-op;
3. **already at a TERMINAL target status -> return**, before anything else.
   This is the one that makes a second application harmless, and it has to come
   before the staleness check, because the first application bumps
   ``version_no`` and would make the second look stale. It is limited to the
   terminal targets (APPROVED, REJECTED): a DRAFT target on a DRAFT document
   is not "already applied", it is the case where the note still has to be
   written, and that write is made idempotent by its own content instead;
4. any other non-DRAFT status is a conflict and is refused loudly;
5. a ``version_no`` that no longer matches the instance's ``object_version``
   is refused loudly -- the document moved underneath the instance, which is
   what ``approvals.supersede_if_changed`` exists to handle, and quietly
   overwriting it would apply approvers' consent to text they never read.

Nothing about the budget is reimplemented here
----------------------------------------------
``budget.approve_revision`` / ``approve_transfer`` are CALLED, not copied. They
hold the maker-checker refusal, the ordered ancestor-chain lock, the re-read
under that lock, the availability re-check, the effective-dated ``budget_line``
append (the ORIGINAL grant row is immutable at the database level and is never
edited) and the version snapshot. An APPROVED revision therefore becomes
spendable by exactly the same route it always did.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from . import budget as budget_mod
from . import repo
from .approval_rules import (
    ApprovalError, INST_APPROVED, INST_CANCELLED, INST_EXCEPTION_PENDING,
    INST_OPEN, INST_RECALLED, INST_REJECTED, INST_RETURNED, INST_SUPERSEDED,
)
from .engine import Session

__all__ = ["apply_outcome", "INSTANCE_TO_BUSINESS_STATUS", "CLOSED_STATUSES"]

# ==========================================================================
# The namespace boundary
# ==========================================================================

#: C3 business statuses this module may write. Named rather than inlined so the
#: contract test has something to compare against ``C3_statuses.json``.
BIZ_DRAFT = "DRAFT"
BIZ_APPROVED = "APPROVED"
BIZ_REJECTED = "REJECTED"
BIZ_RETURNED = "RETURNED"

#: Approval-instance status -> the C3 business status to put on the document,
#: or ``None`` for "this closure changes no business status".
#:
#: Every value is either ``None`` or one of the frozen 21. An approval-engine
#: status must never appear on the right-hand side; see the module docstring
#: and ``tests/test_approval_writeback.py``.
INSTANCE_TO_BUSINESS_STATUS: dict[str, str | None] = {
    INST_APPROVED: BIZ_APPROVED,
    INST_REJECTED: BIZ_REJECTED,
    INST_RETURNED: BIZ_RETURNED,   # C15 instance/RETURNED, storable since 009
    INST_RECALLED: BIZ_DRAFT,
    INST_CANCELLED: BIZ_DRAFT,
    INST_SUPERSEDED: None,
}

#: The statuses ``apply_outcome`` accepts. OPEN and EXCEPTION_PENDING are
#: absent because neither is closed: an EXCEPTION_PENDING instance is HELD for
#: an administrator, and treating "held" as an outcome is how an unroutable
#: object would become an approved one.
CLOSED_STATUSES = frozenset(INSTANCE_TO_BUSINESS_STATUS)

#: Actions whose actor is the person the resulting document change is
#: attributable to. Ordered by nothing -- the newest matching row wins.
_CLOSING_ACTIONS = ["APPROVE", "REJECT", "RETURN", "RECALL", "CANCEL", "SUPERSEDE"]

#: Attribution of last resort. ``approval_action.actor_user_id`` is a foreign
#: key to ``app_user`` and SYSTEM is a real ``principal_kind='SERVICE'`` row
#: (plan section 10.3), so this is a principal, not a magic string.
_SYSTEM_ACTOR = "SYSTEM"


class WritebackError(ApprovalError):
    """A closed instance whose outcome cannot be applied to its document.

    Subclasses ``ApprovalError`` so it carries ``code``/``message``/``status``/
    ``detail`` and reaches the client through ``api/approvals.py``'s existing
    error mapping rather than as a 500.
    """


ERR_NOT_CLOSED = "APPROVAL_WRITEBACK_NOT_CLOSED"
ERR_STALE = "APPROVAL_WRITEBACK_STALE"
ERR_CONFLICT = "APPROVAL_WRITEBACK_CONFLICT"
ERR_DOCUMENT_UNREACHABLE = "APPROVAL_WRITEBACK_DOCUMENT_UNREACHABLE"
ERR_CELLS_DISAGREE = "APPROVAL_WRITEBACK_CELLS_DISAGREE"


# ==========================================================================
# The public entry point
# ==========================================================================
def apply_outcome(session: Session, instance: Mapping[str, Any], *,
                   closing_actor: str | None = None) -> None:
    """Apply one CLOSED approval instance's outcome to its document.

    Called inside the engine's transaction, at the single point an instance
    closes. A silent no-op for an ``object_type`` this module does not know --
    which is the point of the registry: adding a routable type to
    ``approvals.OBJECT_BINDINGS`` later cannot crash the flows that exist now,
    it just does not write anything back until a handler is added here too.
    """
    handler = _REGISTRY.get(instance.get("object_type"))
    if handler is None:
        return

    status = instance.get("status")
    if status not in CLOSED_STATUSES:
        raise WritebackError(
            ERR_NOT_CLOSED,
            f"Approval instance {instance.get('instance_id')} is {status!r}, "
            f"which is not a closed status. The write-back applies an OUTCOME; "
            f"{INST_OPEN} means no outcome yet and {INST_EXCEPTION_PENDING} "
            f"means the object is held for an administrator, and neither may "
            f"move the document.",
            status=409,
            detail={"instance_id": instance.get("instance_id"),
                    "instance_status": status})

    target = INSTANCE_TO_BUSINESS_STATUS[status]
    if target is None:
        # SUPERSEDED. The document is unchanged on purpose: superseding routes
        # a NEW instance over the same object, and that instance's own closure
        # is what will eventually decide it.
        return

    handler(session, instance, target, closing_actor=closing_actor)


# ==========================================================================
# Shared helpers
# ==========================================================================
def _closing_actor(session: Session, instance: Mapping[str, Any],
                    supplied: str | None = None) -> str:
    """Who the resulting document change is attributed to.

    ``budget_line.created_by`` and ``budget_revision.decided_by`` must name the
    person whose decision released the money, not a service account, so this
    tries two sources in order and only then gives up:

    1. **the instance's action log** -- the last APPROVE/REJECT/... actor. Right
       whenever the engine has already recorded the closing action.
    2. **the assignment that acted** -- ``approval_assignment.state = 'ACTED'``
       on the most recently closed stage. This is not a redundant second guess:
       ``_apply_decision`` marks the assignment ACTED *before* it sets the
       instance status, and the ``approval_action`` row is appended *after*
       ``decide`` returns from it. So a write-back invoked at the moment the
       instance closes sees the assignment and not yet the action, and without
       this branch every approved revision in production would be attributed to
       SYSTEM while every test that called the write-back afterwards showed the
       real approver. The two orderings must not disagree.
    3. SYSTEM -- a real ``principal_kind='SERVICE'`` row, reached for a maker
       recall or an administrative cancel, where no approver acted at all.

    Note what is NOT done here: where the actor turns out to be the document's
    maker, ``budget.approve_revision``'s ``SELF_APPROVAL_FORBIDDEN`` fires and
    the whole decision is refused. That is the correct outcome and it is left
    to fire; substituting SYSTEM to get past it would silence a maker-checker
    breach at the exact moment it was detected.
    """
    if supplied:
        # The engine told us. It knows: `decide` has the acting identity in
        # hand, and neither table below is finished being written at the
        # moment the write-back fires.
        return supplied

    instance_id = instance.get("instance_id")
    row = session.fetchone(  # scope-exempt: reads the action log of an instance get_instance has already scope-checked; returns a user id, never business data
        "SELECT actor_user_id FROM approval_action WHERE instance_id = %s "
        "AND action = ANY(%s) ORDER BY seq DESC LIMIT 1",
        (instance_id, _CLOSING_ACTIONS))
    if row is not None and row[0]:
        return row[0]

    row = session.fetchone(  # scope-exempt: reads the assignments of an instance get_instance has already scope-checked; returns a user id, never business data
        """
        SELECT a.assignee_user_id
        FROM approval_assignment a
        JOIN approval_stage_instance s ON s.stage_instance_id = a.stage_instance_id
        WHERE s.instance_id = %s AND a.state = 'ACTED'
        ORDER BY s.closed_at DESC NULLS LAST, s.stage_no DESC
        LIMIT 1
        """,
        (instance_id,))
    return row[0] if row is not None and row[0] else _SYSTEM_ACTOR


def _assert_version_matches(instance: Mapping[str, Any], current_version: Any,
                             *, label: str, object_id: str) -> None:
    """Refuse loudly when the document moved underneath the instance.

    ``approval_instance.object_version`` is stored as text (the engine writes
    ``str(object_version)``), so the comparison is on the string form of both
    sides rather than on a type that may differ between the two columns.
    """
    expected = str(instance.get("object_version"))
    actual = str(current_version)
    if expected == actual:
        return
    raise WritebackError(
        ERR_STALE,
        f"{label} {object_id} was at version {expected} when approval "
        f"instance {instance.get('instance_id')} was opened and is now at "
        f"version {actual}. The document changed underneath the instance, so "
        f"the approvers decided text that is no longer there. The write-back "
        f"refuses rather than overwriting; `approvals.supersede_if_changed` is "
        f"the path that re-routes a changed document.",
        status=409,
        detail={"object_id": object_id,
                "object_version_at_open": expected,
                "object_version_now": actual,
                "instance_id": instance.get("instance_id")})


def _assert_cells_declared(instance: Mapping[str, Any],
                            required: list[tuple[str, str]], *,
                            object_id: str) -> None:
    """Every cell the service function will lock must be in the snapshot's set.

    ``locking.py``'s rule 1 is that the affected set is declared once, first and
    complete. The engine locked ``snapshot['affected_cells']`` before it took
    the instance lock; ``budget.approve_transfer`` will then lock the set it
    reads off the document. If those two sets ever differ, the second call is a
    cell lock taken AFTER the instance and document locks -- a new edge in the
    wait-for graph and the one thing the deadlock-freedom proof forbids. Where
    they are equal it is a re-entry into locks this transaction already holds,
    which adds no edge at all.

    So the equality is asserted rather than assumed. It can only fail if a
    snapshot was built by something other than ``budget.revision_snapshot`` /
    ``budget.transfer_snapshot``, which is exactly the case worth catching.
    """
    declared = set()
    for cell in (instance.get("snapshot") or {}).get("affected_cells") or []:
        if isinstance(cell, Mapping):
            wbs_id, head = cell.get("wbs_id"), cell.get("budget_head_id")
        elif isinstance(cell, (list, tuple)) and len(cell) == 2:
            wbs_id, head = cell
        else:
            continue
        if wbs_id and head:
            declared.add((str(wbs_id), str(head)))
    missing = sorted(set(required) - declared)
    if missing:
        raise WritebackError(
            ERR_CELLS_DISAGREE,
            f"Approval instance {instance.get('instance_id')} for {object_id} "
            f"declared an affected-cell set that does not cover {missing}. The "
            f"engine locked the declared set; applying the outcome would lock "
            f"more cells after the instance and document rows were already "
            f"locked, which is the lock-order violation `locking.py` rules out.",
            status=500,
            detail={"object_id": object_id, "missing_cells": missing,
                    "declared_cells": sorted(declared)})


def _note(instance: Mapping[str, Any]) -> str:
    """A deterministic decision note.

    Deterministic because it is written on the idempotent path: the same closed
    instance produces the same string, so a second application writes the same
    bytes rather than a second, differently-worded note.
    """
    return (f"Approval instance {instance.get('instance_id')} closed "
            f"{instance.get('status')}")


# ==========================================================================
# BUDGET_REVISION
# ==========================================================================
_REVISION_SCOPE_COLUMNS = {
    "project": "w.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}


def _apply_budget_revision(session: Session, instance: Mapping[str, Any],
                            target: str, *,
                            closing_actor: str | None = None) -> None:
    revision_id = str(instance.get("object_id"))
    row = repo.query_one(
        session,
        """
        SELECT r.status, r.version_no, r.wbs_id, r.budget_head_id, r.decision_note
        FROM budget_revision r
        JOIN wbs_element w ON w.wbs_id = r.wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE r.revision_id = %(id)s AND {scope}
        """,
        {"id": revision_id},
        columns=_REVISION_SCOPE_COLUMNS,
    )
    if row is None:
        # Zero rows means "not found OR out of scope", which repo.query()
        # deliberately does not distinguish. Inside a closing instance that
        # `get_instance` already scope-checked, either reading is a genuine
        # inconsistency, and silently doing nothing would leave an APPROVED
        # instance beside an untouched document.
        raise WritebackError(
            ERR_DOCUMENT_UNREACHABLE,
            f"Budget revision {revision_id} is not readable in this "
            f"transaction's scope, but approval instance "
            f"{instance.get('instance_id')} decided it. The outcome cannot be "
            f"applied to a document this session cannot see.",
            status=409, detail={"object_id": revision_id})
    status, version_no, wbs_id, head_id, decision_note = row

    if status == target and target != BIZ_DRAFT:
        return                              # already applied; idempotent
    if status != BIZ_DRAFT:
        raise WritebackError(
            ERR_CONFLICT,
            f"Budget revision {revision_id} is already {status}, and approval "
            f"instance {instance.get('instance_id')} closed {instance.get('status')} "
            f"which would make it {target}. Two different decisions are "
            f"recorded against one document version; the write-back refuses "
            f"rather than choosing between them.",
            status=409,
            detail={"object_id": revision_id, "document_status": status,
                    "would_write": target})
    _assert_version_matches(instance, version_no, label="Budget revision",
                             object_id=revision_id)

    actor = _closing_actor(session, instance, closing_actor)
    instance_id = str(instance.get("instance_id"))

    if target == BIZ_APPROVED:
        _assert_cells_declared(instance, [(str(wbs_id), str(head_id))],
                                object_id=revision_id)
        budget_mod.approve_revision(session, revision_id=revision_id, actor=actor,
                                     approval_instance_id=instance_id)
        return

    if target == BIZ_REJECTED:
        budget_mod.reject_revision(session, revision_id=revision_id, actor=actor,
                                    reason=_note(instance),
                                    approval_instance_id=instance_id)
        return

    # target is DRAFT: RETURNED, RECALLED or CANCELLED. The document is already
    # DRAFT (the branch above proved it), so there is no status to write. What
    # is written is the reason, because `status` alone cannot say which of
    # "never submitted" and "returned for correction" this is -- see the module
    # docstring's note on the CHECK constraint.
    note = _note(instance)
    if decision_note == note:
        return                              # idempotent
    session.execute(  # scope-exempt: updates the single row the scoped read above returned
        "UPDATE budget_revision SET decision_note = %(note)s "
        "WHERE revision_id = %(id)s",
        {"note": note, "id": revision_id})


# ==========================================================================
# BUDGET_TRANSFER
# ==========================================================================
_TRANSFER_SCOPE_COLUMNS = dict(_REVISION_SCOPE_COLUMNS)


def _apply_budget_transfer(session: Session, instance: Mapping[str, Any],
                            target: str, *,
                            closing_actor: str | None = None) -> None:
    transfer_id = str(instance.get("object_id"))
    row = repo.query_one(
        session,
        """
        SELECT t.status, t.version_no, t.from_wbs_id, t.from_head_id,
               t.to_wbs_id, t.to_head_id, t.decision_note
        FROM budget_transfer t
        JOIN wbs_element w ON w.wbs_id = t.from_wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE t.transfer_id = %(id)s AND {scope}
        """,
        {"id": transfer_id},
        columns=_TRANSFER_SCOPE_COLUMNS,
    )
    if row is None:
        raise WritebackError(
            ERR_DOCUMENT_UNREACHABLE,
            f"Budget transfer {transfer_id} is not readable in this "
            f"transaction's scope, but approval instance "
            f"{instance.get('instance_id')} decided it. The outcome cannot be "
            f"applied to a document this session cannot see.",
            status=409, detail={"object_id": transfer_id})
    (status, version_no, from_wbs, from_head, to_wbs, to_head,
     decision_note) = row

    if status == target and target != BIZ_DRAFT:
        return                              # already applied; idempotent
    if status != BIZ_DRAFT:
        raise WritebackError(
            ERR_CONFLICT,
            f"Budget transfer {transfer_id} is already {status}, and approval "
            f"instance {instance.get('instance_id')} closed {instance.get('status')} "
            f"which would make it {target}. Two different decisions are "
            f"recorded against one document version; the write-back refuses "
            f"rather than choosing between them.",
            status=409,
            detail={"object_id": transfer_id, "document_status": status,
                    "would_write": target})
    _assert_version_matches(instance, version_no, label="Budget transfer",
                             object_id=transfer_id)

    actor = _closing_actor(session, instance, closing_actor)
    instance_id = str(instance.get("instance_id"))

    if target == BIZ_APPROVED:
        _assert_cells_declared(
            instance,
            [(str(from_wbs), str(from_head)), (str(to_wbs), str(to_head))],
            object_id=transfer_id)
        budget_mod.approve_transfer(session, transfer_id=transfer_id, actor=actor,
                                     approval_instance_id=instance_id)
        return

    if target == BIZ_REJECTED:
        budget_mod.reject_transfer(session, transfer_id=transfer_id, actor=actor,
                                    reason=_note(instance),
                                    approval_instance_id=instance_id)
        return

    note = _note(instance)
    if decision_note == note:
        return
    session.execute(  # scope-exempt: updates the single row the scoped read above returned
        "UPDATE budget_transfer SET decision_note = %(note)s "
        "WHERE transfer_id = %(id)s",
        {"note": note, "id": transfer_id})


# ==========================================================================
# The registry
# ==========================================================================
#: ``object_type`` -> the function that moves that document's business status.
#:
#: A CLOSED allow-list, deliberately the same two types
#: ``approvals.OBJECT_BINDINGS`` declares. An object type absent from here is
#: NOT an error -- `apply_outcome` returns silently -- because the engine may
#: legitimately route a type whose write-back has not been written yet, and a
#: crash at the moment a decision is taken is a far worse answer than a
#: document that has to be moved by hand.
_REGISTRY: dict[str, Callable[[Session, Mapping[str, Any], str], None]] = {
    budget_mod.OBJECT_TYPE_REVISION: _apply_budget_revision,
    budget_mod.OBJECT_TYPE_TRANSFER: _apply_budget_transfer,
}
