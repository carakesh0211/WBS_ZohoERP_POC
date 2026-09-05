"""Approval delegation: who may act *for* whom, and for how long.

Delegation exists so an approval does not stall when its approver is on leave.
It is a **capacity** mechanism, and the two sentences below are the whole of its
policy; everything in this module is one of them made mechanical.

**Delegation adds capacity; it never removes accountability.**
    Section 9.2 step 5(b): a live delegate is *added* to the stage's assignee
    set and the delegator's own assignment **stays PENDING**. The delegator can
    still act, still appears in the audit trail as an assignee, and is still the
    person the workflow named. Withdrawing the delegator's assignment would let
    a delegation quietly move accountability off the person the configuration
    chose, which is the opposite of what a control is for.

**A delegation can never launder a self-approval.**
    Contract 5 and section 9.4 step 4: a delegated action is refused if
    **either** the acting user **or** the person they are acting for is in the
    object's contributor set. Checking only the actor would let a maker delegate
    to a colleague and have their own decision taken "for" them; checking only
    the principal would let a contributor act as somebody else's delegate. Both
    identities, every time -- see :func:`assert_delegation_independent`.

Window semantics
----------------
``approval_delegation.active_range`` is a PostgreSQL ``daterange``, which is
``[)`` by default: **lower bound inclusive, upper bound exclusive**. That
convention is mirrored exactly in :func:`is_live` rather than being re-invented,
so a delegation that PostgreSQL's ``@>`` considers current is one this module
considers current. A revocation is a separate, earlier cut-off: ``revoked_at``
ends the delegation from that instant regardless of the range, and the row is
kept rather than deleted so the trail shows the delegation existed.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .approval_rules import (
    ERR_DELEGATION_WINDOW_INVALID, ERR_SELF_APPROVAL, VIA_DELEGATION,
    ApprovalError, Assignee, T_DELEGATION,
)
from .engine import Session


class DelegationWindowInvalid(ApprovalError):
    """The requested delegation window is not a window anything can fall in."""

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None):
        super().__init__(ERR_DELEGATION_WINDOW_INVALID, message, status=400,
                          detail=detail)


class DelegatedSelfApproval(ApprovalError):
    """A delegation would have let a contributor approve their own object."""

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None):
        super().__init__(ERR_SELF_APPROVAL, message, status=403, detail=detail)


@dataclass(frozen=True)
class Delegation:
    """One ``approval_delegation`` row, as the pure logic below wants it.

    ``active_from`` is inclusive and ``active_to`` is exclusive, matching the
    ``daterange`` stored in the database. ``active_to = None`` is an open-ended
    delegation.

    ``scope_key`` narrows a delegation to part of the estate -- a project, an
    entity, a document type. ``None`` means the delegation covers everything the
    delegator is asked to approve.
    """

    delegation_id: str
    delegator_user_id: str
    delegate_user_id: str
    scope_key: str | None = None
    active_from: date | None = None
    active_to: date | None = None
    revoked_at: datetime | None = None

    def is_live(self, as_of: date | datetime, *, scope_key: str | None = None) -> bool:
        return is_live(self, as_of, scope_key=scope_key)


def _as_date(value: date | datetime) -> date:
    # `datetime` is a subclass of `date`, so the isinstance order matters:
    # asking about `date` first would answer True for a datetime and skip the
    # conversion, leaving a value that compares against dates inconsistently.
    if isinstance(value, datetime):
        return value.date()
    return value


def validate_window(active_from: date | None, active_to: date | None) -> None:
    """Refuse a window nothing can ever fall inside.

    The upper bound is exclusive, so ``active_to == active_from`` is an EMPTY
    range, not a one-day delegation. It is refused rather than stored, because a
    delegation that silently covers nothing looks configured and is not -- the
    person who set it up would go on leave believing cover exists.
    """
    if active_from is not None and active_to is not None and active_to <= active_from:
        raise DelegationWindowInvalid(
            f"A delegation from {active_from.isoformat()} to "
            f"{active_to.isoformat()} covers no days: the upper bound of a "
            f"daterange is EXCLUSIVE, so 'to' must be strictly after 'from'. "
            f"For a delegation covering only {active_from.isoformat()}, pass "
            f"the following day as 'to'.",
            detail={"active_from": active_from.isoformat(),
                    "active_to": active_to.isoformat()})


def is_live(delegation: Delegation, as_of: date | datetime, *,
             scope_key: str | None = None) -> bool:
    """Is this delegation in force at ``as_of``, for ``scope_key``?

    Four independent conditions, all of which must hold:

    1. not revoked at or before ``as_of``;
    2. ``as_of >= active_from`` (or no lower bound);
    3. ``as_of < active_to``   (or no upper bound) -- **exclusive**, per the
       ``daterange`` convention;
    4. the delegation's ``scope_key`` is ``None`` (covers everything) or equals
       the ``scope_key`` being asked about.

    A revocation timestamp is compared as an instant where one is available,
    because a delegation revoked at 11:00 should not still cover an approval at
    14:00 on the same day; comparing dates alone would leave the whole day
    covered and a revocation is usually made precisely because it must take
    effect now.
    """
    if delegation.revoked_at is not None:
        revoked = delegation.revoked_at
        if isinstance(as_of, datetime):
            moment = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
            if revoked.tzinfo is None:
                revoked = revoked.replace(tzinfo=timezone.utc)
            if moment >= revoked:
                return False
        elif _as_date(revoked) <= as_of:
            return False

    day = _as_date(as_of)
    if delegation.active_from is not None and day < delegation.active_from:
        return False
    if delegation.active_to is not None and day >= delegation.active_to:
        return False
    if delegation.scope_key is not None and scope_key is not None \
            and delegation.scope_key != scope_key:
        return False
    return True


def expand_delegates(base: Iterable[Assignee], delegations: Iterable[Delegation],
                      as_of: date | datetime, *, scope_key: str | None = None,
                      allow_delegation: bool = True) -> list[Assignee]:
    """Section 9.2 step 5(b). Returns ``base`` **plus** each live delegate.

    The delegator is still in the returned list, in their original position,
    with their original ``assigned_via``. That is the "delegation adds capacity,
    never removes accountability" rule expressed as code: this function is
    incapable of removing anybody, so no future edit can turn delegation into
    reassignment by accident.

    ``allow_delegation=False`` (the stage's own flag) means no delegate is added
    at all -- some stages must be acted on by the named approver personally.

    Chains are **not** followed: if A delegates to B and B delegates to C, C is
    not added for A. One hop is the whole rule. A chain would let accountability
    travel arbitrarily far from the person the workflow named, through
    intermediaries who never saw the object, and nothing in the specification
    asks for it.

    A delegate who is already an assignee is not added twice; the existing
    assignment (which may be a direct ROLE one) is kept, because how somebody
    came to be an approver is part of the audit trail and the stronger claim
    wins.
    """
    out = list(base)
    if not allow_delegation:
        return out

    # `base_users` is frozen BEFORE the loop and is what a delegation's
    # delegator is matched against; `present` grows and is only used for
    # de-duplication. Matching the delegator against the growing set is how a
    # chain gets followed by accident: A->B adds B, and the very next iteration
    # sees B "present" and honours B->C. Two sets, two jobs.
    base_users = {a.user_id for a in out}
    present = set(base_users)
    # Sorted for a stable assignee order: two delegations added in database
    # order would give the same stage a different assignee list on different
    # runs, and a reproducible route is worth more than any other ordering.
    for delegation in sorted(delegations, key=lambda d: (d.delegator_user_id,
                                                          d.delegate_user_id,
                                                          d.delegation_id)):
        if delegation.delegator_user_id not in base_users:
            continue                    # not a delegation of anybody on this stage
        if delegation.delegate_user_id in present:
            continue
        if not is_live(delegation, as_of, scope_key=scope_key):
            continue
        present.add(delegation.delegate_user_id)
        out.append(Assignee(user_id=delegation.delegate_user_id,
                            assigned_via=VIA_DELEGATION,
                            delegated_from=delegation.delegator_user_id))
    return out


def assert_delegation_independent(actor_user_id: str, acting_for_user_id: str | None,
                                   contributors: Iterable[str]) -> None:
    """Contract 5: refuse if EITHER identity contributed to the object.

    Called on every decision, delegated or not (with ``acting_for_user_id=None``
    for a direct one), so the "both identities" rule is enforced by a single
    function that cannot be bypassed by taking the non-delegated path.
    """
    contributor_set_ = {c for c in contributors if c}
    if actor_user_id in contributor_set_:
        raise DelegatedSelfApproval(
            f"{actor_user_id} contributed to this object and cannot approve it, "
            f"whether acting personally or as a delegate. Segregation of duties "
            f"requires an independent approver.",
            detail={"actor_user_id": actor_user_id,
                    "acting_for_user_id": acting_for_user_id})
    if acting_for_user_id and acting_for_user_id in contributor_set_:
        raise DelegatedSelfApproval(
            f"{actor_user_id} may not act for {acting_for_user_id}, who "
            f"contributed to this object. A delegation adds capacity; it can "
            f"never launder a self-approval by routing a contributor's decision "
            f"through somebody else.",
            detail={"actor_user_id": actor_user_id,
                    "acting_for_user_id": acting_for_user_id})


# ==========================================================================
# Persistence. Everything above is pure; everything below needs a database.
# ==========================================================================

_SELECT_COLUMNS = (
    "delegation_id, delegator_user_id, delegate_user_id, scope_key, "
    "lower(active_range), upper(active_range), revoked_at"
)


def _row_to_delegation(row: tuple) -> Delegation:
    return Delegation(
        delegation_id=row[0], delegator_user_id=row[1], delegate_user_id=row[2],
        scope_key=row[3], active_from=row[4], active_to=row[5], revoked_at=row[6],
    )


def load_delegations_for(session: Session, delegator_user_ids: Iterable[str],
                          ) -> list[Delegation]:
    """Every non-revoked delegation held by any of ``delegator_user_ids``.

    Liveness is decided in Python by :func:`is_live` rather than by a SQL
    ``@>``, so that the window rule has exactly one implementation and the tests
    that pin it need no database. The query narrows to the delegators actually
    on the stage, which is the part worth pushing into SQL.
    """
    ids = sorted({u for u in delegator_user_ids if u})
    if not ids:
        return []
    rows = session.fetchall(
        f"SELECT {_SELECT_COLUMNS} FROM approval_delegation "
        f"WHERE delegator_user_id = ANY(%s) AND revoked_at IS NULL "
        f"ORDER BY delegator_user_id, delegate_user_id, delegation_id",
        (ids,))
    return [_row_to_delegation(row) for row in rows]


def create_delegation(session: Session, *, delegator_user_id: str,
                       delegate_user_id: str, scope_key: str | None,
                       active_from: date, active_to: date | None,
                       created_by: str) -> Delegation:
    """Record a delegation. Validates the window before writing anything.

    A self-delegation is refused: it would add the delegator to their own stage
    a second time, which changes an ``ALL`` quorum's arithmetic while adding no
    capacity at all.
    """
    validate_window(active_from, active_to)
    if delegator_user_id == delegate_user_id:
        raise DelegationWindowInvalid(
            f"{delegator_user_id} cannot delegate to themselves; that adds no "
            f"approval capacity and would count twice toward an ALL quorum.",
            detail={"delegator_user_id": delegator_user_id})

    delegation_id = f"ADLG-{uuid.uuid4().hex[:12].upper()}"
    upper = active_to.isoformat() if active_to else None
    session.execute(
        f"""
        INSERT INTO approval_delegation
            (delegation_id, delegator_user_id, delegate_user_id, scope_key,
             active_range, created_by)
        VALUES (%(id)s, %(delegator)s, %(delegate)s, %(scope)s,
                daterange(%(lower)s::date, %(upper)s::date, '[)'), %(by)s)
        """,
        {"id": delegation_id, "delegator": delegator_user_id,
         "delegate": delegate_user_id, "scope": scope_key,
         "lower": active_from.isoformat(), "upper": upper, "by": created_by},
    )
    return Delegation(delegation_id=delegation_id,
                       delegator_user_id=delegator_user_id,
                       delegate_user_id=delegate_user_id, scope_key=scope_key,
                       active_from=active_from, active_to=active_to)


def revoke_delegation(session: Session, *, delegation_id: str, actor_user_id: str,
                       reason_text: str | None = None) -> Delegation:
    """End a delegation now. The row is kept; only ``revoked_at`` is set.

    Deleting it would erase the fact that cover existed, which an auditor
    reconstructing why a particular person was an assignee last Tuesday needs.
    Re-revoking is a no-op rather than an error -- the caller's intent (this
    delegation is not in force) already holds.
    """
    row = session.fetchone(
        f"SELECT {_SELECT_COLUMNS} FROM approval_delegation "
        f"WHERE delegation_id = %s FOR UPDATE", (delegation_id,))
    if row is None:
        raise ApprovalError("DELEGATION_NOT_FOUND",
                             f"Delegation {delegation_id} does not exist.",
                             status=404)
    existing = _row_to_delegation(row)
    if existing.revoked_at is not None:
        return existing

    session.execute(
        f"UPDATE approval_delegation SET revoked_at = now(), revoke_reason = %(reason)s "
        f"WHERE delegation_id = %(id)s",
        {"reason": reason_text or f"revoked by {actor_user_id}",
         "id": delegation_id})
    updated = session.fetchone(
        f"SELECT {_SELECT_COLUMNS} FROM approval_delegation WHERE delegation_id = %s",
        (delegation_id,))
    return _row_to_delegation(updated)   # type: ignore[arg-type]


#: Page-size ceiling, mirroring ``api/approvals.py``'s own cap so a caller that
#: reaches the engine directly cannot ask for an unbounded page.
DEFAULT_PAGE = 50
MAX_PAGE = 200


def _page_size(limit: int | None) -> int:
    try:
        value = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        value = DEFAULT_PAGE
    return min(max(value, 1), MAX_PAGE)


def list_delegations(session: Session, *, user_id: str | None = None,
                      include_revoked: bool = False, cursor: str | None = None,
                      limit: int | None = None) -> dict[str, Any]:
    """``GET /api/approvals/delegations`` -- delegations granted BY or TO
    ``user_id`` (both directions), newest first.

    ``user_id`` is a FILTER, not the acting identity, which is why it is not
    called ``actor_user_id``: it names whose delegations are being asked about.
    The router happens to pass the caller's own id, because a caller holding
    ``approval.delegate`` sees the delegations they are a party to and not the
    estate's -- the fail-closed reading, and the one that needs no new decision
    about who may audit somebody else's cover.

    Paginated (Contract 3 requires a cursor on EVERY list) and returning the
    same ``{"items", "next_cursor", "has_more"}`` envelope as every other
    engine list. It used to return a bare ``list``: correct for the two
    in-process callers and unbounded over HTTP, where the delegation table is
    exactly the kind that grows quietly and is never pruned.

    The keyset is ``(lower(active_range), delegation_id)`` descending.
    ``lower(active_range)`` is coalesced to ``-infinity`` so an open-ended
    delegation sorts and pages deterministically rather than landing in a NULL
    ordering that the cursor comparison cannot reproduce.
    """
    size = _page_size(limit)
    clauses: list[str] = []
    params: dict[str, Any] = {"lim": size + 1}
    if user_id:
        clauses.append("(delegator_user_id = %(user)s OR delegate_user_id = %(user)s)")
        params["user"] = user_id
    if not include_revoked:
        clauses.append("revoked_at IS NULL")
    if cursor:
        lower, _, cursor_id = str(cursor).partition("\x1f")
        clauses.append(
            "(COALESCE(lower(active_range), '-infinity'::date), delegation_id) "
            "< (%(c_from)s::date, %(c_id)s)")
        params["c_from"] = lower or "-infinity"
        params["c_id"] = cursor_id
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = session.fetchall(
        f"SELECT {_SELECT_COLUMNS} FROM approval_delegation {where} "
        f"ORDER BY COALESCE(lower(active_range), '-infinity'::date) DESC, "
        f"delegation_id DESC LIMIT %(lim)s",
        params)
    items = [_row_to_delegation(row) for row in rows]
    has_more = len(items) > size
    page = items[:size]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        lower = last.active_from.isoformat() if last.active_from else "-infinity"
        next_cursor = f"{lower}\x1f{last.delegation_id}"
    return {"items": page, "next_cursor": next_cursor, "has_more": has_more}
