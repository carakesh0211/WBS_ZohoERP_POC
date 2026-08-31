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

    POST /api/budget/revisions/{revision_id}/approve
    POST /api/budget/revisions/{revision_id}/reject   {"reason"}
    POST /api/budget/transfers/{transfer_id}/approve
    POST /api/budget/transfers/{transfer_id}/reject   {"reason"}

Additive only -- nothing here conflicts with a frontend built against the
documented shape; see ``app/backend/pg/budget.py``'s module docstring for the
same note.

``router = APIRouter()`` is exported and mounted by ``app/backend/main.py``,
which this module does not touch. Its routes carry their full
``/api/budget/...`` path so mounting needs no prefix.

**Scope note.** Identity & scope (Wave 2 stream 3, M4a) has not landed yet,
so there is no authenticated per-caller ``Scope`` to build here -- exactly
``api/audit.py``'s posture for the same reason. Every read still goes through
``repo.query()`` with its scope-token discipline (a query missing the
``{scope}`` token is refused before it reaches the database); only the
*predicate* that compiles is currently unrestricted (``Scope(read_all=True)``).
Tightening this to a real per-caller scope is M4a's job.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from ..pg import periods as periods_svc
from ..pg import budget as budget_svc
from ..pg.engine import Database, Scope, get_database

router = APIRouter()

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


def _scope_for(request: Request) -> Scope:
    """No authenticated per-caller scope exists yet for this milestone -- see
    the module docstring's Scope note."""
    return Scope(user_id="SVC-BUDGET-API", principal_kind="SERVICE", read_all=True)


def _actor(request: Request) -> str:
    return request.headers.get("X-User-Id") or "UNKNOWN"


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
        with database.session(_scope_for(request)) as session:
            items = periods_svc.list_periods(session, entity_id=entity_id, state=state)
    except periods_svc.PeriodServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items}


class _PeriodTransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_state: str


@router.post("/api/budget/periods/{period_id}/transition")
def post_period_transition(
    period_id: str, body: _PeriodTransitionIn,
    response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
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
        with database.session(_scope_for(request)) as session:
            result = budget_svc.list_cells(
                session, project_id=project_id, wbs_id=wbs_id,
                budget_head_id=budget_head_id, cursor=cursor, limit=limit)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


# ============================================================== availability
@router.get("/api/budget/availability")
def get_availability(
    response: Response, request: Request,
    wbs_id: str = Query(...),
    budget_head_id: str = Query(...),
    amount_paise: int = Query(...),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
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
        with database.session(_scope_for(request)) as session:
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


@router.post("/api/budget/revisions", status_code=201)
def post_revision(
    body: _RevisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
            result = budget_svc.create_revision(
                session, wbs_id=body.wbs_id, budget_head_id=body.budget_head_id,
                delta_paise=body.delta_paise, effective_from=body.effective_from,
                justification=body.justification, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


class _DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


@router.post("/api/budget/revisions/{revision_id}/approve")
def post_revision_approve(
    revision_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
            result = budget_svc.approve_revision(
                session, revision_id=revision_id, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/revisions/{revision_id}/reject")
def post_revision_reject(
    revision_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
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


@router.post("/api/budget/transfers", status_code=201)
def post_transfer(
    body: _TransferIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
            result = budget_svc.create_transfer(
                session, from_wbs_id=body.from_wbs_id, from_head_id=body.from_head_id,
                to_wbs_id=body.to_wbs_id, to_head_id=body.to_head_id,
                amount_paise=body.amount_paise, effective_from=body.effective_from,
                justification=body.justification, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/transfers/{transfer_id}/approve")
def post_transfer_approve(
    transfer_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
            result = budget_svc.approve_transfer(
                session, transfer_id=transfer_id, actor=_actor(request))
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return result


@router.post("/api/budget/transfers/{transfer_id}/reject")
def post_transfer_reject(
    transfer_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request)) as session:
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
        with database.session(_scope_for(request)) as session:
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
        with database.session(_scope_for(request)) as session:
            rows = budget_svc.compare_versions(session, project_id=project_id, left=left, right=right)
    except budget_svc.BudgetServiceError as exc:
        raise _service_error_to_http(exc)
    return {"rows": rows}
