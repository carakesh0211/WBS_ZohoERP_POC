"""FastAPI router for the approval engine (Wave 4, M4b).

Exact contract this module is built to (frozen in ``docs/WAVE4_CONTRACTS.md``
contract 3 -- the frontend approvals stream codes against it in parallel, so it
must not drift)::

    GET  /api/approvals/inbox?state=&object_type=&cursor=&limit=
    POST /api/approvals/{instance_id}/decide
         {"action":"APPROVE|REJECT|RETURN", "reason_code":..., "reason_text":...,
          "idempotency_key": <required>, "object_version": <required>}
    POST /api/approvals/{instance_id}/recall        {"reason_text"}
    POST /api/approvals/{instance_id}/cancel        {"reason_text"}
    POST /api/approvals/{instance_id}/resubmit      {"reason_text"}
    GET  /api/approvals/{instance_id}
    GET  /api/approvals/{instance_id}/timeline
    GET  /api/approvals/definitions?object_type=&status=
    POST /api/approvals/definitions                 (creates a DRAFT version)
    POST /api/approvals/definitions/{id}/activate
    POST /api/approvals/definitions/{id}/simulate   {"object": {...}}
    GET  /api/approvals/definitions/{id}/versions
    GET  /api/approvals/delegations
    POST /api/approvals/delegations   {"delegate_user_id","scope_key","from","to"}
    POST /api/approvals/delegations/{id}/revoke     {"reason_text"}
    GET  /api/approvals/sla?overdue=true

``router = APIRouter()`` is exported and mounted by ``app/backend/main.py``,
which this module does not touch (Wave 4 file ownership: ``main.py`` is
lead-owned). Its routes carry their full ``/api/approvals/...`` path so
mounting needs no prefix.

Route registration ORDER is load-bearing
----------------------------------------
``/api/approvals/{instance_id}`` matches the literal strings ``inbox``,
``definitions``, ``delegations`` and ``sla`` just as happily as it matches a
real instance id, and FastAPI resolves in registration order. Registering the
parameterised route first would therefore have shadowed
``GET /api/approvals/definitions`` -- silently demoting it from
``approval.configure`` to ``approval.read``, which is a privilege escalation
dressed up as a routing detail. Every literal path in this module is
registered BEFORE the ``{instance_id}`` routes, and
``tests/test_approvals_api_guard.py`` asserts the consequence behaviourally
(a caller holding ``approval.read`` but not ``approval.configure`` must still
be refused on ``/api/approvals/definitions``) rather than asserting the order.

The three defect classes this router is written not to repeat
-------------------------------------------------------------
1. **A router with a database dependency and no permission guard.** The guard
   is declared on the ROUTER (``APIRouter(dependencies=[...])``), not per
   route, so a route added later cannot ship unguarded by omission -- which is
   exactly how ``api/audit.py`` shipped in Milestone 1 and ``api/budget.py``
   in Wave 2.
2. **Caller-supplied identity.** ``_actor`` reads the SESSION-derived
   principal. ``X-Actor-Id``, ``X-Permissions`` and every other request header
   are inert here: they are never read, so they can never grant anything.
   Contract 5's maker-checker compares exactly the identity ``_actor``
   returns, so a caller-supplied actor would defeat it outright.
3. **A hand-built ``Scope``.** ``_scope_for`` resolves the caller's real
   grants through ``principal_scope.scope_for_request``. This module
   constructs no ``Scope`` of its own and grants itself no whole-estate read
   flag; a principal who cannot be resolved gets a scope that compiles to
   ``FALSE``.

Authorisation is not only a permission
--------------------------------------
``approval.act`` is the FLOOR, not the answer. Whether *this* caller may act
on *this* instance is assignment plus maker-checker, and Contract 5 requires
it checked inside the transaction, by the engine, against rows -- not here.
This router's job at decision time is narrow and it is deliberately narrow:
pass the server-derived actor down, and render the engine's refusal as the
right status. It never decides that a caller may act.

Statuses the engine's frozen refusal codes map to (Contract 3 + Contract 8)::

    SELF_APPROVAL              403   maker-checker, second enforcement point
    NOT_AN_ASSIGNEE            403   authenticated, permitted, not addressed
    STAGE_NOT_OPEN             409   state conflict, not a bad request
    OBJECT_VERSION_STALE       409   the document moved under the instance
    BUDGET_MOVED               409   availability moved between routing and now
    REASON_REQUIRED            422   well-formed, semantically incomplete
    IDEMPOTENCY_KEY_REQUIRED   422   ditto
    IDEMPOTENT_REPLAY          200   the ORIGINAL outcome, never applied twice

One permission table
--------------------
Contract 4 adds ``approval.read``, ``approval.act``, ``approval.configure``
and ``approval.delegate`` to ``auth.PERMISSIONS``. Those entries have landed,
so ``_holders_of`` reads ``auth.PERMISSIONS`` and nothing else.

A ``_CONTRACT4_PERMISSIONS`` transcription stood in here while ``auth.py`` --
lead-owned and frozen at the Wave 4 baseline -- did not yet carry the four
keys, because ``auth.require`` raises ``UNKNOWN_PERMISSION`` (a 500) for a key
it does not know, which would have turned every guard on this router into a
server fault instead of a refusal.

It is gone, and not merely because it became redundant. It had DRIFTED: it
transcribed Contract 4's "every role" for ``approval.read``, Auditor included,
while the permission that actually landed EXCLUDES Auditor. So the fallback
failed OPEN relative to the authoritative table -- deleting ``approval.read``
from ``auth.PERMISSIONS`` would have WIDENED access rather than removing it,
which is the opposite of what deleting a permission must ever do. A second
table that can disagree with the first is worse than no second table.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import importlib
from datetime import date
from typing import Any, Callable
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..pg import principal_scope
from ..pg.engine import Database, Scope, get_database

# ===========================================================================
# Permissions
# ===========================================================================
#: Contract 4, transcribed verbatim. Consulted ONLY for a permission
#: `auth.PERMISSIONS` does not define -- see the module docstring.
READ = "approval.read"
ACT = "approval.act"
CONFIGURE = "approval.configure"
DELEGATE = "approval.delegate"


def _holders_of(permission: str) -> tuple[str, ...] | None:
    """The roles holding `permission`, or `None` when nothing defines it.

    `auth.PERMISSIONS` is the ONLY source. `None` means nothing defines the
    permission, and every caller treats that as a refusal -- an undefined
    permission has never been an open door.

    One table, so there is nothing left for a second one to disagree with.
    """
    from .. import auth as auth_mod

    holders = auth_mod.PERMISSIONS.get(permission)
    return tuple(holders) if holders is not None else None


def _holds(who: dict, permission: str) -> bool:
    holders = _holders_of(permission)
    if not holders:
        return False
    return bool(set(who.get("roles") or ()) & set(holders))


def _authorise(who: dict, permission: str, request: Request | None = None) -> None:
    """Raise the controlled 403 unless `who` holds `permission`.

    Mirrors `auth.require`'s status, code and message exactly, so a caller
    cannot tell from the response whether the permission came from
    `auth.PERMISSIONS` or from the Contract 4 fallback.

    `request` is carried only so the refusal can be traced: a raised
    `HTTPException` discards the handler's `Response`, so the correlation id
    has to travel on the exception or it does not travel at all.
    """
    from .. import auth as auth_mod

    trace = {_CORRELATION_HEADER: _correlation_id(request)} if request else None
    holders = _holders_of(permission)
    if holders is None:
        raise HTTPException(500, {"code": "UNKNOWN_PERMISSION",
                                  "message": f"Unknown permission {permission}."},
                            headers=trace)
    if permission in auth_mod.PERMISSIONS:
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message},
                                headers=trace) from exc
        return
    if not _holds(who, permission):
        roles = ", ".join(who.get("roles") or ()) or "none"
        raise HTTPException(403, {
            "code": "FORBIDDEN",
            "message": (f"Your role ({roles}) cannot perform '{permission}'. "
                        f"Required: {' or '.join(holders)}.")}, headers=trace)


class _ApprovalAccess:
    """Router-level dependency: authenticate, and require `approval.read`, on
    EVERY approval route.

    Declared on the ROUTER rather than per route, so a route added later
    cannot ship unguarded by omission. Two routers in this codebase have
    already shipped exactly that way -- each declared only its database
    dependency, and the middleware in front of them checks that a session
    header EXISTS without validating it, so `X-Session: anything` was enough
    to read the whole estate.

    Note what is NOT read here: no header names the actor, no header names a
    permission. Identity comes from `main.principal`, which resolves a
    server-side session; permissions come from the principal's roles.

    Routes carrying a heavier permission add it on top of this floor.
    """

    def __call__(self, request: Request) -> dict:
        # Lazy import: `main` imports this module once mounted, so a
        # module-level import would be circular.
        from ..main import principal

        try:
            who = principal(
                authorization=request.headers.get("Authorization", ""),
                x_session=request.headers.get("X-Session", ""),
            )
        except HTTPException as exc:
            # Re-raised only to attach the correlation id. A caller whose
            # session was rejected is exactly the caller most likely to be
            # reporting it, and a 401 with nothing to quote is unanswerable.
            raise HTTPException(
                exc.status_code, exc.detail,
                headers={_CORRELATION_HEADER: _correlation_id(request)}) from exc
        _authorise(who, READ, request)
        request.state.approval_principal = who
        return who


require_approval_access = _ApprovalAccess()


def _requires(permission: str) -> Callable[[Request], dict]:
    """One route's own permission, on top of the router's floor.

    Runs after the router dependency, so `request.state.approval_principal`
    is already populated and authentication has already happened -- which is
    why a role that lacks the permission gets 403 and not 401.
    """

    def _dep(request: Request) -> dict:
        who = getattr(request.state, "approval_principal", None) or {}
        _authorise(who, permission, request)
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_approval_access)])

_CORRELATION_HEADER = "X-Correlation-Id"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


# ===========================================================================
# Request plumbing
# ===========================================================================
def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- same posture as
    ``api/audit.py`` and ``api/budget.py``."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The PostgreSQL approvals API is mounted but no "
                               "database is configured for this process."},
        ) from exc


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str, detail: str | None = None,
             request: Request | None = None) -> HTTPException:
    """An RFC-7807-shaped error body carrying the `code` field the contract
    requires.

    The correlation id is attached as a response HEADER on the exception
    itself. A handler's `Response` object never reaches the client on a raised
    `HTTPException` -- Starlette builds a fresh response from the exception --
    so setting the header in the handler would have covered success responses
    only, and "every response carries X-Correlation-Id" would have been true
    of exactly the responses nobody needs to trace.
    """
    headers = {_CORRELATION_HEADER: _correlation_id(request)} if request else None
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
    }, headers=headers)


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "approval_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    Contract 6: `approval_instance` carries `entity_id` and `project_id`
    denormalised at creation precisely so it is scopable directly, and the
    inbox query compiles this predicate through `repo.query`'s `{scope}`
    token. Two earlier routers in this codebase invented a scope here instead
    of reading one -- one returned an unconditional whole-estate SERVICE
    scope, the other derived `read_all` from a set of role NAMES, which
    short-circuits `compile_scope` to TRUE before any dimension is examined.

    `scope_for_request` resolves the grants and fails closed: an unresolvable
    principal gets a scope that compiles to FALSE, not one that compiles to
    TRUE. A resolution FAILURE (as opposed to a denial) raises
    `ScopeResolutionUnavailable`, which `main.py` renders as a 503 -- and that
    is deliberately not caught here, because "we could not determine what you
    may see" must not render as "you may see nothing".
    """
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    This value is written into `approval_action.actor_user_id`, into the
    hash-chained audit stream, and it is the identity Contract 5's
    maker-checker compares. A caller-supplied actor -- `X-Actor-Id`, a body
    field, anything -- would make the approval record unattributable and
    segregation of duties trivially defeatable, so nothing the caller sends is
    consulted. With no authenticated principal it names nobody.
    """
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


def _limit_of(limit: int) -> int:
    return min(max(limit, 1), _MAX_LIMIT)


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str, request: Request | None = None) -> str:
    """Opaque to the caller by construction.

    A cursor is a server-issued token, not an offset a caller composes, so a
    malformed one is a 400 with a code rather than a 500 -- and rather than a
    silent fall back to page one, which would make a corrupted cursor look
    like the end of the list.
    """
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _problem(400, "INVALID_CURSOR", "Invalid pagination cursor",
                       "the cursor could not be decoded", request) from exc


def _page(result: Any, limit: int) -> dict[str, Any]:
    """Normalise whatever the engine returns into the frozen list envelope.

    Cursor pagination is required on EVERY list (Contract 3), so the envelope
    is produced here rather than trusted from below: an engine function that
    returns a bare list still yields `next_cursor` and `has_more` keys, and a
    client written against the contract keeps working.

    The engine's cursor is PLAIN text -- a keyset like `"2026-09-01T…\\x1fAINS-7"`
    -- and it is encoded HERE, unconditionally, on the way out. It used to be
    encoded only when it was not already a `str`, which meant every cursor the
    engine actually produces travelled to the client raw and came back to
    `_decode_cursor` as un-base64-able text: page one worked and page two was a
    400. The pair is symmetric now -- `_decode_cursor` inbound, `_encode_cursor`
    outbound -- so the token stays opaque and stays round-trippable.
    """
    if isinstance(result, dict):
        items = [_jsonable(item) for item in (result.get("items") or ())]
        next_cursor = result.get("next_cursor")
        has_more = result.get("has_more")
        if has_more is None:
            has_more = next_cursor is not None
        if next_cursor is not None:
            next_cursor = _encode_cursor(str(next_cursor))
        return {"items": items, "next_cursor": next_cursor,
                "has_more": bool(has_more)}
    items = [_jsonable(item) for item in (result or ())]
    return {"items": items, "next_cursor": None, "has_more": False}


def _jsonable(result: Any) -> Any:
    """Render an engine return value as JSON-shaped data.

    The engine speaks in dataclasses where a dataclass is the honest type --
    `approvals.DecisionResult`, `delegation.Delegation` -- and FastAPI cannot
    serialise either against these handlers' `dict[str, Any]` annotation. It is
    converted here rather than in the engine, because the engine's callers
    include tests and other services that want the typed value, and flattening
    it at the source to suit one transport would be the transport dictating the
    domain model.

    `as_dict()` is preferred where a type defines one (it is the shape that
    also goes into `approval_action.outcome`, so the API and the audit record
    agree); `dataclasses.asdict` is the fallback; anything else passes through.
    """
    if isinstance(result, dict) or result is None:
        return result
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return result


# ===========================================================================
# Engine adapter -- the ONLY place streams 1 and 2 are called
# ===========================================================================
#: Streams 1 and 2 own `pg/approvals.py`, `pg/approval_rules.py`,
#: `pg/delegation.py` and `pg/approval_schema.py`. Every call to them is
#: funnelled through :func:`_call_engine`, which exists so a missing engine is
#: a clean 503 rather than an `AttributeError` 500.
#:
#: Each entry is a tuple of names for `_call_engine` to try in order. EVERY
#: tuple below now holds exactly ONE name, and that is the point: the multi-name
#: tolerance is what made this seam fail silently.
#:
#: INTEGRATION NOTE. Written before the engine existed here, this seam guessed
#: both names and keyword ARGUMENTS, and every guess was wrong. The two halves
#: failed differently and neither failed loudly:
#:
#:   * a wrong NAME falls through `_call_engine` to a clean
#:     `503 APPROVAL_ENGINE_UNAVAILABLE`, which reads as "this deployment has
#:     no database" rather than "this route has never worked". Eight routes were
#:     in that state.
#:   * a wrong KEYWORD raises `TypeError`, which carries no `.code`, so
#:     `_raise_for_engine_error` re-raises it and the route answers 500. A
#:     control refusing correctly and a server fault then look identical.
#:
#: The mapper was deliberately NOT widened to swallow `TypeError`: a genuine
#: bug must still be a 500. Instead `tests/test_approvals_api_seam.py` resolves
#: every call site below against the real engine signature statically, with no
#: database, so a rename on either side fails at collection time. That test is
#: why every name tuple here must stay a LITERAL -- a computed name is a call
#: site the check cannot see, and the three routes that used one (recall,
#: cancel, resubmit, through a shared factory) were exactly the three that had
#: been broken longest.
#:
#: TWO NAMING JUDGEMENTS, recorded here because both were open questions.
#:
#: 1. `actor` vs `actor_user_id`. ONE name, no aliases: **`actor_user_id`**, for
#:    the user on whose behalf the request is being made, in every engine
#:    function that takes one. It was already the name on the write path
#:    (`decide`, `recall`, `cancel`, `resubmit`, `supersede_if_changed`,
#:    `_append_action`), it is the COLUMN name in `approval_action`, and it is
#:    the identity Contract 5's maker-checker compares -- so the read and
#:    configuration halves were renamed to it rather than the other way round.
#:    Parameters naming a DIFFERENT person keep their own descriptive names and
#:    are not aliases: `delegator_user_id`, `delegate_user_id`, `maker_user_id`,
#:    `assigned_to`, `created_by`, and `list_delegations`'s `user_id`, which is
#:    a filter ("whose delegations") and not the acting identity.
#:
#: 2. `correlation_id`. Threaded through the engine wherever a STORED column can
#:    hold it, and REMOVED wherever none can:
#:      - `open_instance` writes it to `approval_instance.correlation_id`.
#:      - `decide`, `recall`, `cancel` and `resubmit` now take it and
#:        `_append_action` writes it into `approval_action.outcome`, a real
#:        jsonb column. It is NOT folded into the hash: Contract 9 freezes the
#:        payload as `prev|at|actor|action|type|id|detail` and `chain_detail`
#:        builds `detail` from stored columns only, so that verification
#:        recomputes each digest from the row it is checking.
#:      - `create_definition`, `activate_definition` and `revoke_delegation` no
#:        longer accept one. Those tables carry no correlation column, that path
#:        writes no `audit_log` row, and migration 008 is lead-owned. The
#:        parameter previously existed and was dropped on the floor, which reads
#:        at the call site as though a trace were being recorded. The response's
#:        `X-Correlation-Id` header is honestly the whole trace there is.
#:    §11.9's `audit_log` propagation is about the Zoho integration path
#:    (`job` -> inbox/outbox -> `integration_event` -> `audit_log`); the approval
#:    engine writes `approval_action`, not `audit_log`, and giving it an
#:    `audit_log` stream of its own is a change to a lead-owned contract, not a
#:    wiring fix.
_ENGINE_MODULES: dict[str, str] = {
    "approvals": "app.backend.pg.approvals",
    "rules": "app.backend.pg.approval_rules",
    "delegation": "app.backend.pg.delegation",
}


def _engine_module(key: str, request: Request | None = None):
    try:
        return importlib.import_module(_ENGINE_MODULES[key])
    except ImportError as exc:
        raise _problem(
            503, "APPROVAL_ENGINE_UNAVAILABLE",
            "The approval engine is not available",
            f"{_ENGINE_MODULES[key]} is not importable in this deployment",
            request) from exc


def _call_engine(key: str, names: tuple[str, ...], *args,
                 request: Request | None = None, **kwargs) -> Any:
    """Invoke one engine function, by the first of `names` that exists.

    The single seam between this router and streams 1/2. It exists so that a
    missing engine is a clean 503 rather than an `AttributeError` 500, and so
    that this module's tests can drive the whole refusal matrix without a
    database by replacing exactly one function.
    """
    module = _engine_module(key, request)
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
    raise _problem(
        503, "APPROVAL_ENGINE_UNAVAILABLE",
        "The approval engine is not available",
        f"{_ENGINE_MODULES[key]} defines none of {', '.join(names)}", request)


# ===========================================================================
# Refusals
# ===========================================================================
#: Contract 3's frozen error codes, and the status each is rendered as.
#:
#: 403 vs 409 vs 422 is not cosmetic. `SELF_APPROVAL` and `NOT_AN_ASSIGNEE`
#: are authorisation refusals about WHO is asking -- the request will never
#: succeed for this caller. `STAGE_NOT_OPEN`, `OBJECT_VERSION_STALE` and
#: `BUDGET_MOVED` are conflicts with state that has MOVED -- the same request
#: from the same caller may well succeed once refreshed, which is what 409
#: tells a client and 403 does not. `REASON_REQUIRED` and
#: `IDEMPOTENCY_KEY_REQUIRED` are well-formed requests that are semantically
#: incomplete, which is 422.
_ENGINE_STATUS: dict[str, int] = {
    # --- authorisation, decided against rows inside the transaction
    "SELF_APPROVAL": 403,
    "NOT_AN_ASSIGNEE": 403,
    # --- state conflicts
    "STAGE_NOT_OPEN": 409,
    "OBJECT_VERSION_STALE": 409,
    "BUDGET_MOVED": 409,
    "DEFINITION_NOT_ACTIVE": 409,
    "DEFINITION_IMMUTABLE": 409,
    # --- fail-closed routing outcomes (Contract 2: no route to auto-approval)
    "APPROVAL_ROUTE_UNRESOLVED": 409,
    "NO_INDEPENDENT_APPROVER": 409,
    # --- semantically incomplete
    "REASON_REQUIRED": 422,
    "IDEMPOTENCY_KEY_REQUIRED": 422,
    "DELEGATION_WINDOW_INVALID": 422,
}

#: Not an error at all. Contract 8: a replay of the same idempotency key
#: returns the ORIGINAL outcome and never applies twice, so it is a 200 that
#: happens to be labelled -- rendering it as a 4xx would push clients into
#: retrying, which is the exact behaviour idempotency exists to make safe.
_REPLAY_CODE = "IDEMPOTENT_REPLAY"


def _status_for_code(code: str) -> int:
    """The status one frozen refusal code renders as.

    An UNKNOWN code is 409, not 500: a refusal this router has not been taught
    about is still a refusal by the engine, and rendering it as a server fault
    would blame the deployment for a correctly-enforced control.
    """
    if code == _REPLAY_CODE:
        return 200
    return _ENGINE_STATUS.get(code, 409)


def _replay_outcome(exc: Exception) -> dict[str, Any] | None:
    """The original outcome an `IDEMPOTENT_REPLAY` carries, if it carries one."""
    for attribute in ("outcome", "result", "original", "original_outcome"):
        value = getattr(exc, attribute, None)
        if isinstance(value, dict):
            return dict(value)
    return None


def _raise_for_engine_error(exc: Exception, request: Request | None = None) -> None:
    """Render an engine or database refusal as the right 4xx.

    Anything without a recognisable shape is re-raised untouched, so a genuine
    bug in the engine still surfaces as a 500 and is logged as one. Swallowing
    those into a 4xx would hide real faults behind a plausible refusal.
    """
    if isinstance(exc, HTTPException):
        raise exc

    # --- PostgreSQL. `api/budget.py` has no psycopg handler at all, so a
    # constraint violation there is an unhandled 500; that omission is not
    # copied here. Each of these is caused by the request, not by the server.
    if isinstance(exc, psycopg.errors.UniqueViolation):
        raise _problem(409, "DUPLICATE", "That row already exists",
                       "a uniqueness constraint refused this write", request) from exc
    if isinstance(exc, psycopg.errors.ForeignKeyViolation):
        raise _problem(422, "INVALID_REFERENCE", "A referenced row does not exist",
                       "a foreign key constraint refused this write", request) from exc
    if isinstance(exc, psycopg.errors.CheckViolation):
        raise _problem(422, "INVALID_FIELD",
                       "A field value violates a database constraint",
                       "a check constraint refused this write", request) from exc
    # RLS. A row-level policy that refuses a write raises SQLSTATE 42501, and
    # that is an authorisation answer arriving from the database rather than a
    # server fault -- the deepest of the enforcement points, and the one most
    # likely to be reached only when every layer above it has a gap.
    if isinstance(exc, psycopg.errors.InsufficientPrivilege) or \
            getattr(exc, "sqlstate", None) == "42501":
        raise _problem(403, "FORBIDDEN",
                       "You may not act on that row",
                       "a row-level security policy refused this statement",
                       request) from exc

    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        raise exc
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = _status_for_code(code)
    message = getattr(exc, "message", None) or str(exc)
    raise _problem(status, code, code.replace("_", " ").title(), message,
                   request) from exc


def _engine_result(fn: Callable[[], Any], request: Request) -> Any:
    """Run one engine call, rendering both replay and refusal correctly.

    `IDEMPOTENT_REPLAY` is handled on BOTH paths -- as a returned envelope and
    as a raised error -- because Contract 8 fixes the observable behaviour
    (200, the original outcome) without fixing which way the engine signals
    it, and this stream cannot see the engine to find out.
    """
    try:
        return fn()
    except Exception as exc:                                    # noqa: BLE001
        if getattr(exc, "code", None) == _REPLAY_CODE:
            outcome = _replay_outcome(exc) or {}
            return {**outcome, "code": _REPLAY_CODE, "replayed": True}
        _raise_for_engine_error(exc, request)
        raise  # unreachable; _raise_for_engine_error always raises


# ===========================================================================
# Request bodies -- every model forbids unknown fields
# ===========================================================================
class _Body(BaseModel):
    """`extra="forbid"` on every body in this module.

    A silently-ignored field is how a caller comes to believe it set something
    it did not -- and on this surface the fields worth mis-sending are
    `actor`, `reason_code` and `object_version`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _DecideIn(_Body):
    action: str
    reason_code: str | None = None
    reason_text: str | None = None
    # Contract 3 marks both REQUIRED. They are Optional here and checked in
    # the handler so the refusal carries the frozen `IDEMPOTENCY_KEY_REQUIRED`
    # code; Pydantic's own missing-field 422 has no `code` field at all, and
    # "RFC-7807 errors carrying code" has to hold for the required-field case
    # most of all, since that is the one a client hits first.
    idempotency_key: str | None = None
    object_version: int | None = None
    #: Stream B: the Administrator's deliberate self-approval override.
    admin_override_reason: str | None = None


class _ReasonIn(_Body):
    reason_text: str | None = None


class _DefinitionIn(_Body):
    """A DRAFT definition version.

    Shaped from Contract 1's columns. Stream 1 owns the table and stream 2 the
    validation, so this model's job is to forbid what is not in the contract
    and to pass the rest down unread -- not to second-guess a schema it cannot
    see. `version` is deliberately absent: Contract 1 makes a version the
    engine's to mint, never the caller's to choose.
    """

    object_type: str
    code: str
    entity_id: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    rules: list[dict[str, Any]] | None = None
    stages: list[dict[str, Any]] | None = None


class _SimulateIn(_Body):
    object_: dict[str, Any] = Field(alias="object")


class _DelegationIn(_Body):
    delegate_user_id: str
    scope_key: str
    from_: date = Field(alias="from")
    to: date


# ===========================================================================
# Inbox and SLA
#
# Every literal path is registered BEFORE `/api/approvals/{instance_id}`.
# ===========================================================================
@router.get("/api/approvals/inbox")
def get_inbox(
    response: Response, request: Request,
    state: str | None = Query(default=None),
    object_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Instances in the caller's scope AND addressed to them.

    Contract 6, both halves. The scope half is the session's, compiled into
    the query by `repo.query`'s `{scope}` token. The addressed-to-them half is
    `assigned_to`: a caller sees their OWN assignments, not the estate's,
    unless they hold `approval.configure` -- the administrative view, which is
    still scope-bounded. Passing `assigned_to=None` for everyone would have
    turned a personal inbox into an estate-wide one for every authenticated
    caller, since `approval.read` is held by every role.
    """
    _set_correlation_header(response, request)
    who = _principal_of(request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "approvals", ("list_inbox",),
            session, request=request,
            actor_user_id=_actor(request),
            assigned_to=None if _holds(who, CONFIGURE) else _actor(request),
            state=state, object_type=object_type, cursor=decoded, limit=limit,
        ), request)
    return _page(result, limit)


@router.get("/api/approvals/sla")
def get_sla(
    response: Response, request: Request,
    overdue: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    who = _principal_of(request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "approvals", ("list_sla",),
            session, request=request,
            actor_user_id=_actor(request),
            assigned_to=None if _holds(who, CONFIGURE) else _actor(request),
            overdue=overdue, cursor=decoded, limit=limit,
        ), request)
    return _page(result, limit)


# ===========================================================================
# Definitions -- approval.configure
# ===========================================================================
@router.get("/api/approvals/definitions",
            dependencies=[Depends(_requires(CONFIGURE))])
def get_definitions(
    response: Response, request: Request,
    object_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "rules", ("list_definitions",),
            session, request=request, object_type=object_type, status=status,
            cursor=decoded, limit=limit,
        ), request)
    return _page(result, limit)


@router.post("/api/approvals/definitions", status_code=201,
             dependencies=[Depends(_requires(CONFIGURE))])
def post_definition(
    body: _DefinitionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _engine_result(lambda: _call_engine(
            "rules", ("create_definition",),
            session, request=request, actor_user_id=_actor(request),
            payload=body.model_dump(mode="json"),
        ), request)


@router.post("/api/approvals/definitions/{definition_id}/activate",
             dependencies=[Depends(_requires(CONFIGURE))])
def post_definition_activate(
    definition_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _engine_result(lambda: _call_engine(
            "rules", ("activate_definition",),
            session, request=request, definition_id=definition_id,
            actor_user_id=_actor(request),
        ), request)


@router.post("/api/approvals/definitions/{definition_id}/simulate",
             dependencies=[Depends(_requires(CONFIGURE))])
def post_definition_simulate(
    definition_id: str, body: _SimulateIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """A dry run: which stages and approvers this object would route to.

    Read-only by contract. It is still `approval.configure` rather than
    `approval.read`, because the answer describes the approver population of
    an arbitrary hypothetical object -- which is exactly the map you would
    want before choosing what to submit and to whom.
    """
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _engine_result(lambda: _call_engine(
            "rules", ("simulate",),
            session, request=request, definition_id=definition_id,
            obj=body.object_, actor_user_id=_actor(request),
        ), request)


@router.get("/api/approvals/definitions/{definition_id}/versions",
            dependencies=[Depends(_requires(CONFIGURE))])
def get_definition_versions(
    definition_id: str, response: Response, request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "rules", ("list_versions",),
            session, request=request, definition_id=definition_id,
            cursor=decoded, limit=limit,
        ), request)
    return _page(result, limit)


# ===========================================================================
# Delegations -- approval.delegate
# ===========================================================================
@router.get("/api/approvals/delegations",
            dependencies=[Depends(_requires(DELEGATE))])
def get_delegations(
    response: Response, request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "delegation", ("list_delegations",),
            session, request=request, user_id=_actor(request),
            cursor=decoded, limit=limit,
        ), request)
    return _page(result, limit)


@router.post("/api/approvals/delegations", status_code=201,
             dependencies=[Depends(_requires(DELEGATE))])
def post_delegation(
    body: _DelegationIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Delegate the caller's OWN authority, never someone else's.

    `delegator_user_id` is `_actor`, never a body field. A body that could
    name the delegator would let any holder of `approval.delegate` mint
    authority in another user's name -- and Contract 5 then checks both
    identities at decision time against a delegation this caller should never
    have been able to create.
    """
    _set_correlation_header(response, request)
    # `_jsonable`: `create_delegation` returns a `Delegation` dataclass, which
    # is the honest type for the engine and not one FastAPI can serialise
    # against this handler's `dict[str, Any]` annotation.
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "delegation", ("create_delegation",),
            session, request=request,
            delegator_user_id=_actor(request),
            delegate_user_id=body.delegate_user_id, scope_key=body.scope_key,
            active_from=body.from_, active_to=body.to,
            created_by=_actor(request),
        ), request))


@router.post("/api/approvals/delegations/{delegation_id}/revoke",
             dependencies=[Depends(_requires(DELEGATE))])
def post_delegation_revoke(
    delegation_id: str, body: _ReasonIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "delegation", ("revoke_delegation",),
            session, request=request, delegation_id=delegation_id,
            actor_user_id=_actor(request), reason_text=body.reason_text,
        ), request))


# ===========================================================================
# Instance actions -- approval.act
#
# These are the FLOOR only. Whether this caller may act on THIS instance is
# assignment plus maker-checker, enforced by the engine inside the
# transaction, against rows this router never inspects.
# ===========================================================================
@router.post("/api/approvals/{instance_id}/decide",
             dependencies=[Depends(_requires(ACT))])
def post_decide(
    instance_id: str, body: _DecideIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    action = (body.action or "").strip().upper()
    if action not in {"APPROVE", "REJECT", "RETURN"}:
        raise _problem(422, "INVALID_ACTION", "Unknown decision",
                       "action must be APPROVE, REJECT or RETURN", request)
    if not (body.idempotency_key or "").strip():
        raise _problem(422, "IDEMPOTENCY_KEY_REQUIRED",
                       "An idempotency key is required",
                       "every decision carries an idempotency_key (Contract 8)",
                       request)
    if body.object_version is None:
        raise _problem(422, "OBJECT_VERSION_REQUIRED",
                       "The object version is required",
                       "a decision states the object_version it was taken "
                       "against, so a document that moved cannot be silently "
                       "approved (Contract 8)", request)

    # `actor_user_id`, not `actor`. `approvals.decide` names the identity that
    # goes into `approval_action.actor_user_id` and into Contract 5's
    # maker-checker comparison, and one name for the requesting user is the
    # whole convention -- see the adapter note above.
    #
    # `principal` is passed so the engine's FIRST maker-checker gate --
    # `auth.require_separation`, the product's own, called exactly as
    # `services.py` calls it -- actually runs. It is a second, independent
    # enforcement point, not a replacement for the engine's contributor check,
    # and it is a no-op when the object's permission is not in
    # `auth.MAKER_CHECKER`. It is omitted rather than faked when the session
    # produced no `user_id`: `require_separation` subscripts that key directly,
    # so a principal without one is a 500 rather than a control.
    who = _principal_of(request)
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "approvals", ("decide",),
            session, request=request, instance_id=instance_id,
            actor_user_id=_actor(request), action=action,
            reason_code=body.reason_code, reason_text=body.reason_text,
            idempotency_key=body.idempotency_key.strip(),
            object_version=body.object_version,
            correlation_id=_correlation_id(request),
            principal=dict(who) if who.get("user_id") else None,
            admin_override_reason=body.admin_override_reason,
        ), request))


# ---------------------------------------------------------------------------
# recall / cancel / resubmit
#
# Written out one route at a time rather than through a shared factory that
# took the engine function NAME as a parameter. The factory was tidier and it
# defeated the one check that catches this class of bug: with the name
# arriving as a variable, `tests/test_approvals_api_seam.py` cannot resolve the
# call site statically, so the three routes were the three the seam test could
# not see -- and all three were calling the engine with `actor=` and
# `correlation_id=` against functions that take `actor_user_id=`. Three
# near-identical bodies that a test can read are worth more than one body it
# cannot.
# ---------------------------------------------------------------------------
@router.post("/api/approvals/{instance_id}/recall",
             dependencies=[Depends(_requires(ACT))])
def post_recall(
    instance_id: str, body: _ReasonIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The maker withdraws their own object. The engine enforces maker-only."""
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "approvals", ("recall",), session, request=request,
            instance_id=instance_id, actor_user_id=_actor(request),
            reason_text=body.reason_text,
            correlation_id=_correlation_id(request),
        ), request))


@router.post("/api/approvals/{instance_id}/cancel",
             dependencies=[Depends(_requires(ACT))])
def post_cancel(
    instance_id: str, body: _ReasonIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "approvals", ("cancel",), session, request=request,
            instance_id=instance_id, actor_user_id=_actor(request),
            reason_text=body.reason_text,
            correlation_id=_correlation_id(request),
        ), request))


@router.post("/api/approvals/{instance_id}/resubmit",
             dependencies=[Depends(_requires(ACT))])
def post_resubmit(
    instance_id: str, body: _ReasonIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Re-route an object whose ROUTING failed, not one whose content changed.

    Contract 3 freezes this body as ``{"reason_text"}``, so no snapshot can
    reach the engine here, and this router has no business building one -- a
    ``BUDGET_REVISION`` snapshot is the document layer's knowledge. The engine
    therefore re-routes the instance's own snapshot and refuses with
    ``OBJECT_VERSION_STALE`` if the document has moved since, which is the
    honest answer: an edited document is resubmitted through the document
    layer, which has the corrected snapshot to hand.
    """
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return _jsonable(_engine_result(lambda: _call_engine(
            "approvals", ("resubmit",), session, request=request,
            instance_id=instance_id, actor_user_id=_actor(request),
            reason_text=body.reason_text,
            correlation_id=_correlation_id(request),
        ), request))


# ===========================================================================
# Instance reads -- approval.read (the router floor)
#
# Registered LAST: `{instance_id}` would otherwise swallow every literal path
# above it. See the module docstring.
# ===========================================================================
@router.get("/api/approvals/{instance_id}")
def get_instance(
    instance_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """One instance, or a 404.

    Out of scope and nonexistent are the SAME answer here, deliberately: a
    403 would confirm that an instance with this id exists, which is a
    disclosure the scope is there to prevent. `repo.query` documents the same
    rule -- an out-of-scope row comes back as no row, and callers must not
    translate that into a 403.
    """
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "approvals", ("get_instance",),
            session, request=request, instance_id=instance_id,
        ), request)
    if result is None:
        raise _problem(404, "NOT_FOUND", "No such approval instance",
                       "no instance with that id is visible to you", request)
    return result


@router.get("/api/approvals/{instance_id}/timeline")
def get_timeline(
    instance_id: str, response: Response, request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The instance's action history, including what did NOT run.

    Contract 2: a stage whose `applies_when` was false is recorded `SKIPPED`
    with a `skip_reason` rather than omitted, because an auditor must be able
    to see what did not run. This route therefore renders whatever the engine
    returns without filtering it.
    """
    _set_correlation_header(response, request)
    limit = _limit_of(limit)
    decoded = _decode_cursor(cursor, request) if cursor else None
    with database.session(_scope_for(request, database)) as session:
        result = _engine_result(lambda: _call_engine(
            "approvals", ("get_timeline",),
            session, request=request, instance_id=instance_id,
            actor_user_id=_actor(request), cursor=decoded, limit=limit,
        ), request)
    if result is None:
        raise _problem(404, "NOT_FOUND", "No such approval instance",
                       "no instance with that id is visible to you", request)
    return _page(result, limit)
