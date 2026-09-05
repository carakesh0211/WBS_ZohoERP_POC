"""FastAPI router for budget planning: periods, cells, availability, lines,
revisions, transfers, versions.

Exact contract this module is built to (frozen in ``docs/WAVE2_CONTRACTS.md``
-- the frontend budget stream codes against it in parallel, so it must not
drift)::

    GET  /api/budget/periods?entity_id=&state=
    POST /api/budget/periods/{period_id}/transition   {"to_state"}
    GET  /api/budget/cells?project_id=&wbs_id=&budget_head_id=&cursor=&limit=
    GET  /api/budget/availability?wbs_id=&budget_head_id=&amount_paise=
    GET  /api/budget/lines?project_id=&wbs_id=&version=
    POST /api/budget/revisions   {"wbs_id","budget_head_id","delta_paise",
                                   "effective_from","justification"}
    POST /api/budget/transfers   {"from_wbs_id","from_head_id","to_wbs_id",
                                   "to_head_id","amount_paise","effective_from",
                                   "justification"}
    GET  /api/budget/versions?project_id=
    GET  /api/budget/compare?project_id=&left=&right=

The frozen contract documents only DRAFT *creation* for revisions and
transfers. Domain-controls.md is explicit that "a revision creates no
spending capacity until approved", which needs an approval step somewhere.
This router therefore ADDS, beyond the frozen text::

    POST /api/budget/revisions/{revision_id}/submit
    POST /api/budget/revisions/{revision_id}/approve
    POST /api/budget/revisions/{revision_id}/reject   {"reason"}
    POST /api/budget/transfers/{transfer_id}/submit
    POST /api/budget/transfers/{transfer_id}/approve
    POST /api/budget/transfers/{transfer_id}/reject   {"reason"}

Additive only -- nothing here conflicts with a frontend built against the
documented shape; see ``app/backend/pg/budget.py``'s module docstring for the
same note.

The two ``/submit`` routes are Wave 4's: they raise a DRAFT into the
configurable approval workflow (``pg/approvals.open_instance``), which before
them nothing in the application ever called. ``/approve`` and ``/reject``
remain, for objects no workflow routes; ``pg/budget._assert_not_under_approval``
stops them being used to step around a live instance.

A submission that cannot be ROUTED answers 409 and still commits. That looks
wrong at a glance and is the whole point: the engine records the unroutable
object as an EXCEPTION_PENDING instance and returns rather than raising,
because ``Database.session`` rolls back on an exception and an exception
thrown to report the problem deleted the record of it. See
``_raise_if_refused``.

``router = APIRouter()`` is exported and mounted by ``app/backend/main.py``,
which this module does not touch. Its routes carry their full
``/api/budget/...`` path so mounting needs no prefix.

**Scope note.** Every route resolves the caller's REAL grants through
``pg.principal_scope.scope_for_request`` -- Contract 4's single
scope-construction path. Reads go through ``repo.query()`` with its
scope-token discipline (a query missing the ``{scope}`` token is refused
before it reaches the database), and the predicate that compiles is now the
caller's own.

Two earlier postures are recorded here because both read as reasonable at the
time. The first built an unconditional whole-estate SERVICE scope while
identity was still unbuilt. The second derived ``read_all`` from a set of role
NAMES, which is worse than it looks: ``read_all`` short-circuits
``compile_scope`` to TRUE before any dimension is examined, so every principal
outside that role set was unrestricted on all thirteen routes. ``read_all``
now comes from ``user_access_flag`` and nowhere else.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from ..pg import periods as periods_svc
from ..pg import budget as budget_svc
from ..pg import principal_scope
from ..pg.engine import Database, Scope, get_database

class _BudgetAccess:
    """Router-level dependency: authenticate, and require `budget.read`, on
    EVERY budget route.

    Declared on the ROUTER rather than per route, so a route added later
    cannot ship unguarded by omission. The audit router shipped exactly that
    way in Milestone 1 -- each route declared only its database dependency,
    and the middleware in front of it checks that a session header EXISTS
    without validating it, so `X-Session: anything` was enough to read the
    whole estate.

    Mutating routes add their own permission on top of this floor.
    """

    def __call__(self, request: Request) -> dict:
        # Lazy import: `main` imports this module, so a module-level import
        # would be circular.
        from ..main import principal
        from .. import auth as auth_mod

        who = principal(
            authorization=request.headers.get("Authorization", ""),
            x_session=request.headers.get("X-Session", ""),
        )
        try:
            auth_mod.require(who, "budget.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.budget_principal = who
        return who


require_budget_access = _BudgetAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's floor.

    Runs after the router dependency, so `request.state.budget_principal` is
    already populated and authentication has already happened.
    """

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "budget_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_budget_access)])

_CORRELATION_HEADER = "X-Correlation-Id"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- same posture as
    ``api/audit.py`` and ``api/health.py``."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The PostgreSQL budget API is mounted but no "
                               "database is configured for this process."},
        ) from exc


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str, detail: str | None = None) -> HTTPException:
    """An RFC-7807-shaped error body carrying the `code` field the contract
    requires -- see `api/audit.py::_problem` for the identical reasoning."""
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
    })


def _service_error_to_http(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "BUDGET_ERROR")
    status = getattr(exc, "status", 400)
    message = getattr(exc, "message", str(exc))
    return _problem(status, code, code.replace("_", " ").title(), message)


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "budget_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    Every earlier version of this function invented a scope instead of
    reading one. The first returned an unconditional whole-estate SERVICE
    scope. The second derived `read_all` from a set of role NAMES and left
    every dimension `None`, so any principal outside that role set was
    unrestricted on all thirteen budget routes -- which Contract 2 forbids
    in as many words: `read_all` comes from `user_access_flag`, never from a
    role.

    `scope_for_request` resolves the grants and fails closed. A principal who
    cannot be resolved gets a scope that compiles to FALSE, not one that
    compiles to TRUE.
    """
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    Was `request.headers.get("X-User-Id")`, which let any caller name any
    actor. That value is written into `budget_line.created_by`, into approval
    decisions and into the audit chain, so a caller-supplied actor would have
    made the financial record and its audit trail unattributable -- and
    maker-checker, which compares exactly those identities, trivially
    defeatable.
    """
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


# ============================================================== periods
@router.get("/api/budget/periods")
def get_periods(
    response: Response, request: Request,
    entity_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            items = periods_svc.list_periods(session, entity_id=entity_id, state=state)
    except periods_svc.PeriodServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items}


class _PeriodTransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_state: str


@router.post("/api/budget/periods/{period_id}/transition", dependencies=[Depends(_requires("period.transition"))])
def post_period_transition(
    period_id: str, body: _PeriodTransitionIn,
    response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = periods_svc.transition_period(
                session, period_id=period_id, to_state=body.to_state, actor=_actor(request))
    except periods_svc.PeriodServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== cells
@router.get("/api/budget/cells")
def get_cells(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    wbs_id: str | None = Query(default=None),
    budget_head_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    limit = min(max(limit, 1), _MAX_LIMIT)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.list_cells(
                session, project_id=project_id, wbs_id=wbs_id,
                budget_head_id=budget_head_id, cursor=cursor, limit=limit)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== availability
@router.get("/api/budget/availability", dependencies=[Depends(_requires("budget.check"))])
def get_availability(
    response: Response, request: Request,
    wbs_id: str = Query(...),
    budget_head_id: str = Query(...),
    amount_paise: int = Query(...),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.check_availability(session, wbs_id, budget_head_id, amount_paise)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== lines
@router.get("/api/budget/lines")
def get_lines(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    wbs_id: str | None = Query(default=None),
    version: int | None = Query(default=None),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            items = budget_svc.list_lines(session, project_id=project_id, wbs_id=wbs_id,
                                           version=version)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items}


# ============================================================== revisions
class _RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wbs_id: str
    budget_head_id: str
    delta_paise: int
    effective_from: date
    justification: str


@router.post("/api/budget/revisions", status_code=201, dependencies=[Depends(_requires("revision.create"))])
def post_revision(
    body: _RevisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.create_revision(
                session, wbs_id=body.wbs_id, budget_head_id=body.budget_head_id,
                delta_paise=body.delta_paise, effective_from=body.effective_from,
                justification=body.justification, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


def _raise_if_refused(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a submission ``refusal`` into an HTTP error -- AFTER the commit.

    This must be called OUTSIDE the ``with database.session(...)`` block, and
    the reason is the whole point of the shape.
    ``approvals.open_instance`` does not raise on an unroutable object: it
    writes an EXCEPTION_PENDING instance plus an audit action naming why, and
    returns. That was a deliberate change from raising, because
    ``Database.session`` rolls back on any exception, so an exception thrown to
    report the exception destroyed the evidence of it.

    A refusal is therefore reported by VALUE through the service layer, the
    transaction commits with the instance row in it, and only then does this
    turn it into a 409. Raising a beat earlier would put the rollback back.
    """
    refusal = result.get("refusal")
    if not refusal:
        return result
    raise _problem(refusal.get("status", 409), refusal["code"],
                   str(refusal["code"]).replace("_", " ").title(),
                   refusal.get("message"))


@router.post("/api/budget/revisions/{revision_id}/submit",
             dependencies=[Depends(_requires("revision.create"))])
def post_revision_submit(
    revision_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Raise a DRAFT revision into the configurable approval workflow.

    Additive, like the approve/reject routes above and for the same reason: the
    frozen contract documents only DRAFT creation, and a revision that creates
    no spending capacity until approved needs somewhere for the approval to
    happen. Without this route the whole of ``pg/approvals.py`` was unreachable
    from the application.
    """
    # Resolved ONCE. `_correlation_id` mints a fresh uuid4 when the caller
    # sends no header, so calling it twice -- once for the response header,
    # once for the audit row -- would tell the caller one id and record
    # another, and the correlation would be unfollowable in exactly the case
    # it matters.
    correlation_id = _correlation_id(request)
    response.headers[_CORRELATION_HEADER] = correlation_id
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.submit_revision(
                session, revision_id=revision_id, actor=_actor(request),
                correlation_id=correlation_id)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return _raise_if_refused(result)


class _DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


@router.post("/api/budget/revisions/{revision_id}/approve", dependencies=[Depends(_requires("revision.approve"))])
def post_revision_approve(
    revision_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.approve_revision(
                session, revision_id=revision_id, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/revisions/{revision_id}/reject", dependencies=[Depends(_requires("revision.approve"))])
def post_revision_reject(
    revision_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.reject_revision(
                session, revision_id=revision_id, actor=_actor(request), reason=body.reason)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== transfers
class _TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_wbs_id: str
    from_head_id: str
    to_wbs_id: str
    to_head_id: str
    amount_paise: int
    effective_from: date
    justification: str


@router.post("/api/budget/transfers", status_code=201, dependencies=[Depends(_requires("revision.create"))])
def post_transfer(
    body: _TransferIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.create_transfer(
                session, from_wbs_id=body.from_wbs_id, from_head_id=body.from_head_id,
                to_wbs_id=body.to_wbs_id, to_head_id=body.to_head_id,
                amount_paise=body.amount_paise, effective_from=body.effective_from,
                justification=body.justification, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/transfers/{transfer_id}/submit",
             dependencies=[Depends(_requires("revision.create"))])
def post_transfer_submit(
    transfer_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """As `post_revision_submit`, for a transfer."""
    correlation_id = _correlation_id(request)   # once; see post_revision_submit
    response.headers[_CORRELATION_HEADER] = correlation_id
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.submit_transfer(
                session, transfer_id=transfer_id, actor=_actor(request),
                correlation_id=correlation_id)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return _raise_if_refused(result)


@router.post("/api/budget/transfers/{transfer_id}/approve", dependencies=[Depends(_requires("revision.approve"))])
def post_transfer_approve(
    transfer_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.approve_transfer(
                session, transfer_id=transfer_id, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/transfers/{transfer_id}/reject", dependencies=[Depends(_requires("revision.approve"))])
def post_transfer_reject(
    transfer_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = budget_svc.reject_transfer(
                session, transfer_id=transfer_id, actor=_actor(request), reason=body.reason)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== versions
@router.get("/api/budget/versions")
def get_versions(
    response: Response, request: Request,
    project_id: str = Query(...),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            items = budget_svc.list_versions(session, project_id)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items}


@router.get("/api/budget/compare")
def get_compare(
    response: Response, request: Request,
    project_id: str = Query(...),
    left: int = Query(...),
    right: int = Query(...),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            rows = budget_svc.compare_versions(session, project_id=project_id, left=left, right=right)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return {"rows": rows}
