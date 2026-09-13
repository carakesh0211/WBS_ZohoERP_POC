"""The Administrator's deliberate self-approval override (Stream B, 2026-09-13).

THE DECISION THIS TRANSCRIBES
=============================

The product owner decided on 2026-09-13 that the Administrator holds every
permission in both catalogues (`auth.PERMISSIONS`, `pg.roles.PERMISSIONS`)
and may approve an object they themselves raised -- but only DELIBERATELY:

* the request must carry an explicit `admin_override_reason` (at least
  :data:`auth.ADMIN_OVERRIDE_MIN_REASON` characters); without it the same
  call is refused with the ordinary `SELF_APPROVAL` 403, exactly as before;
* the caller must hold the Administrator role; any other role supplying a
  reason is refused with `ADMIN_OVERRIDE_NOT_PERMITTED` -- the override is
  not a reason field that unlocks self-approval for everyone;
* an override is never DELEGATED: an Administrator acting for somebody else
  cannot use it (`ADMIN_OVERRIDE_NOT_DELEGABLE`), and a delegate cannot use
  it on the Administrator's behalf;
* every override writes ONE audit entry, action
  `ADMIN_SELF_APPROVAL_OVERRIDE`, on the object's own audit stream, whose
  detail is a JSON document naming the actor, the object, the previous and
  new state, the reason, the correlation id, the timestamp and the amount
  where the object has one. It is filterable by action in the audit API and
  it sits beside the ordinary approval entry, never instead of it.

WHAT IS NOT CHANGED
===================

`auth.require_separation` still refuses a self-approval by default.
`assert_delegation_independent` still refuses a contributor's decision and
still refuses a delegation that would launder one. The override is a narrow,
audited gate through those two checks for one role, opened one call at a
time by an explicit reason. The period-reopen separation
(`ck_period_reopen_separation`, migration 023) is enforced by the DATABASE
and is deliberately left outside this override: relaxing a finance control
that lives in a CHECK constraint is a migration and an owner decision, not
a side effect of this stream.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .. import auth as auth_mod
from . import audit as audit_mod
from .engine import Session

ACTION = auth_mod.ADMIN_OVERRIDE_ACTION

_REQUIRED_KEYS = ("action", "actor_user_id", "maker_user_id", "permission",
                  "reason")


def validate(override: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """An override record `auth.require_separation` produced, or None.

    Anything else is refused: a hand-built dict that skipped the role and
    reason checks is precisely what this function exists to keep out of the
    services that trust the record.
    """
    if override is None:
        return None
    if not isinstance(override, Mapping) or any(k not in override for k in _REQUIRED_KEYS) \
            or override.get("action") != ACTION:
        raise auth_mod.AuthError(
            403, "ADMIN_OVERRIDE_INVALID",
            "The override record did not come from auth.require_separation; "
            "it is refused.")
    return dict(override)


def resolve(*, principal: Mapping[str, Any] | None, permission: str,
            maker_user_id: str | None, object_label: str,
            admin_override_reason: str | None,
            admin_override: Mapping[str, Any] | None = None,
            acting_for_user_id: str | None = None) -> dict[str, Any] | None:
    """The override for this call, or None when none is needed or offered.

    * `admin_override` already validated (the approval engine's write-back
      hands the record it produced down to the document service): returned
      as is after :func:`validate`.
    * `admin_override_reason` with a `principal`: `auth.require_separation`
      decides -- it returns the record when the caller IS the maker, holds
      the role and gave a sufficient reason; returns None when no separation
      issue exists (the reason is then unused and nothing is recorded);
      raises otherwise.
    * a reason with NO principal is refused: nobody can verify the role.
    * a delegation never carries an override.
    """
    if admin_override is not None:
        record = validate(admin_override)
    elif admin_override_reason is None:
        return None
    elif principal is None:
        raise auth_mod.AuthError(
            403, "ADMIN_OVERRIDE_NOT_PERMITTED",
            "An administrator override needs the signed-in principal to verify "
            "the role; this call carried none.")
    else:
        record = auth_mod.require_separation(
            dict(principal), permission, maker_user_id,
            object_label=object_label, admin_override_reason=admin_override_reason)
    if record is not None and acting_for_user_id:
        raise auth_mod.AuthError(
            403, "ADMIN_OVERRIDE_NOT_DELEGABLE",
            f"An administrator override is personal: {record['actor_user_id']} "
            f"cannot use it while acting for {acting_for_user_id}.")
    return record


def contributors_for_check(override: Mapping[str, Any] | None, actor_user_id: str,
                           contributors: Iterable[str]) -> set[str]:
    """The contributor set `assert_delegation_independent` is given.

    With an override in hand the ACTOR's own contribution is the thing being
    deliberately overridden, so it is removed; every other contributor -- and
    the delegation limb -- is checked exactly as before.
    """
    contributor_set = {c for c in contributors if c}
    if override is not None:
        contributor_set.discard(actor_user_id)
    return contributor_set


def build_detail(*, override: Mapping[str, Any], object_type: str, object_id: str,
                 previous_state: str | None, new_state: str | None,
                 amount_paise: int | None = None,
                 correlation_id: str | None = None,
                 timestamp: datetime | None = None,
                 extra: Mapping[str, Any] | None = None) -> str:
    """The audit entry's detail: one JSON document, keys sorted, every
    required field present even when null, so a reader (or the export)
    parses the same shape every time."""
    body: dict[str, Any] = {
        "override": ACTION,
        "actor_user_id": override["actor_user_id"],
        "maker_user_id": override.get("maker_user_id"),
        "permission": override.get("permission"),
        "object_type": object_type,
        "object_id": object_id,
        "object_label": override.get("object_label"),
        "previous_state": previous_state,
        "new_state": new_state,
        "reason": override["reason"],
        "correlation_id": correlation_id,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "amount_paise": None if amount_paise is None else int(amount_paise),
    }
    if extra:
        body.update({str(k): v for k, v in extra.items()})
    return json.dumps(body, sort_keys=True, default=str)


def record(session: Session, *, override: Mapping[str, Any], object_type: str,
           object_id: str, previous_state: str | None, new_state: str | None,
           amount_paise: int | None = None, correlation_id: str | None = None,
           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Append the ADMIN_SELF_APPROVAL_OVERRIDE entry to the object's stream.

    Called INSIDE the approving transaction, before or after the state
    write: either way a failed approval rolls the entry back with it, and a
    committed approval always carries it.
    """
    detail = build_detail(override=override, object_type=object_type,
                          object_id=object_id, previous_state=previous_state,
                          new_state=new_state, amount_paise=amount_paise,
                          correlation_id=correlation_id, extra=extra)
    entry = audit_mod.append(session, override["actor_user_id"], ACTION,
                             object_type, object_id, detail,
                             correlation_id=correlation_id)
    # Stream E: every OTHER Administrator hears about an override.
    from . import notifications as notifications_mod
    notifications_mod.try_enqueue(
        session, event=ACTION,
        recipients=notifications_mod.recipients_for_roles(
            session, ("System Administrator",), exclude=[override["actor_user_id"]]),
        context={"actor": override["actor_user_id"],
                 "object_label": override.get("object_label") or f"{object_type} {object_id}",
                 "reason": override["reason"], "correlation_id": correlation_id or ""},
        dedupe_key=f"{ACTION}:{object_type}:{object_id}:{override['actor_user_id']}",
        actor=override["actor_user_id"], correlation_id=correlation_id,
        object_type=object_type, object_id=object_id)
    return entry
