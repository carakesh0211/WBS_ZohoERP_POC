"""Internal material fulfilment routes (migration 034).

Mounted beside `api/procurement.py` and built from its parts on purpose: the
same router floor (`budget.read`, authenticated, on every route), the same
per-route permission dependency, the same server-derived actor, the same
RFC-7807 error shape and `X-Correlation-Id` echo. A fulfilment decision or a
stores movement is a procurement act on a purchase request and is guarded
like one.

Every mutating route here is registered in `tests/test_api_auth.py`'s
authorisation matrix in the same commit; the matrix is built from the live
OpenAPI schema, so an unregistered route fails the next run.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..pg import internal_fulfilment as svc
from ..pg import principal_scope
from ..pg import procurement_services as procurement_svc
from ..pg.engine import Database, Scope
from .procurement import (
    _correlation_id, _get_database, _requires, _service_error_to_http,
    _set_correlation_header, require_procurement_access,
)

router = APIRouter(dependencies=[Depends(require_procurement_access)])

_PREFIX = "/api/procurement"


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "procurement_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL grants, exactly as `api/procurement.py` resolves
    them: `read_all` from `user_access_flag`, never from a role name."""
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """SERVER-DERIVED. Never a header: this value is written into
    `requested_by`, `approved_by`, every movement's `created_by` and the
    audit chain, and maker-checker compares exactly these identities."""
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


def _run(request: Request, response: Response, database: Database, call):
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return call(session)
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


# ------------------------------------------------------------------ bodies
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DecisionIn(_Strict):
    mode: str
    internal_quantity: str | int | None = None
    reason: str | None = Field(default=None, max_length=2000)
    item_external_id: str | None = Field(default=None, max_length=200)
    item_description: str | None = Field(default=None, max_length=500)
    from_location_id: str | None = Field(default=None, max_length=100)
    to_location_id: str | None = Field(default=None, max_length=100)
    unit_rate_paise: int | None = None
    valuation_note: str | None = Field(default=None, max_length=2000)


class _RequestIn(_Strict):
    pr_line_id: str
    quantity: str | int
    reason: str | None = Field(default=None, max_length=2000)
    item_external_id: str | None = Field(default=None, max_length=200)
    item_description: str | None = Field(default=None, max_length=500)
    from_location_id: str | None = Field(default=None, max_length=100)
    to_location_id: str | None = Field(default=None, max_length=100)
    unit_rate_paise: int | None = None
    valuation_note: str | None = Field(default=None, max_length=2000)


class _ApproveIn(_Strict):
    approved_quantity: str | int | None = None
    reason: str | None = Field(default=None, max_length=2000)
    acting_for_user_id: str | None = None
    version_no: int | None = None


class _ValuationIn(_Strict):
    unit_rate_paise: int
    note: str = Field(max_length=2000)
    reference: str | None = Field(default=None, max_length=200)
    version_no: int | None = None


class _AllocateIn(_Strict):
    idempotency_key: str = Field(max_length=200)
    version_no: int | None = None


class _MoveIn(_Strict):
    quantity: str | int
    idempotency_key: str = Field(max_length=200)
    to_location_id: str | None = Field(default=None, max_length=100)
    reference: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    version_no: int | None = None


class _TransferIn(_MoveIn):
    to_location_id: str = Field(max_length=100)


class _CancelIn(_Strict):
    reason: str = Field(max_length=2000)
    version_no: int | None = None


# ---------------------------------------------------------------- decisions
@router.put(_PREFIX + "/purchase-requests/{pr_id}/lines/{pr_line_id}/fulfilment",
            dependencies=[Depends(_requires(svc.PERMISSION_DECIDE))])
def put_fulfilment_decision(pr_id: str, pr_line_id: str, body: _DecisionIn,
                            request: Request, response: Response,
                            database: Database = Depends(_get_database)) -> dict[str, Any]:
    def call(session):
        out = svc.decide_fulfilment(
            session, pr_line_id=pr_line_id, mode=body.mode, actor=_actor(request),
            internal_quantity=body.internal_quantity, reason=body.reason,
            item_external_id=body.item_external_id,
            item_description=body.item_description,
            from_location_id=body.from_location_id,
            to_location_id=body.to_location_id,
            unit_rate_paise=body.unit_rate_paise,
            valuation_note=body.valuation_note,
            correlation_id=_correlation_id(request))
        if out["pr_id"] != pr_id:
            raise HTTPException(404, {"code": "PR_LINE_NOT_FOUND",
                                      "message": f"{pr_line_id} is not a line of {pr_id}."})
        return out
    return _run(request, response, database, call)


@router.get(_PREFIX + "/purchase-requests/{pr_id}/fulfilment",
            dependencies=[Depends(_requires(svc.PERMISSION_READ))])
def get_fulfilment(pr_id: str, request: Request, response: Response,
                   database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database,
                lambda session: {"pr_id": pr_id,
                                 "lines": svc.fulfilment_of(session, pr_id)})


# ----------------------------------------------------------------- requests
@router.post(_PREFIX + "/internal-material-requests", status_code=201,
             dependencies=[Depends(_requires(svc.PERMISSION_CREATE))])
def post_request(body: _RequestIn, request: Request, response: Response,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.create_request(
        session, pr_line_id=body.pr_line_id, quantity=body.quantity,
        actor=_actor(request), item_external_id=body.item_external_id,
        item_description=body.item_description,
        from_location_id=body.from_location_id,
        to_location_id=body.to_location_id, unit_rate_paise=body.unit_rate_paise,
        valuation_note=body.valuation_note, reason=body.reason,
        correlation_id=_correlation_id(request)))


@router.get(_PREFIX + "/internal-material-requests",
            dependencies=[Depends(_requires(svc.PERMISSION_READ))])
def list_requests(request: Request, response: Response,
                  project_id: str | None = Query(default=None),
                  pr_id: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  limit: int = Query(default=100, ge=1, le=1000),
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: {
        "items": svc.list_requests(session, project_id=project_id, pr_id=pr_id,
                                   status=status, limit=limit),
        "statuses": list(svc.STATUSES), "modes": list(svc.MODES)})


@router.get(_PREFIX + "/internal-material-requests/{imr_id}",
            dependencies=[Depends(_requires(svc.PERMISSION_READ))])
def get_request(imr_id: str, request: Request, response: Response,
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database,
                lambda session: svc.get_request(session, imr_id))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/approve",
             dependencies=[Depends(_requires(svc.PERMISSION_APPROVE))])
def post_approve(imr_id: str, body: _ApproveIn, request: Request,
                 response: Response,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.approve_request(
        session, imr_id=imr_id, actor=_actor(request),
        principal=_principal_of(request),
        acting_for_user_id=body.acting_for_user_id,
        approved_quantity=body.approved_quantity, reason=body.reason,
        expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.put(_PREFIX + "/internal-material-requests/{imr_id}/valuation",
            dependencies=[Depends(_requires(svc.PERMISSION_ALLOCATE))])
def put_valuation(imr_id: str, body: _ValuationIn, request: Request,
                  response: Response,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.set_valuation(
        session, imr_id=imr_id, unit_rate_paise=body.unit_rate_paise,
        note=body.note, reference=body.reference, actor=_actor(request),
        expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/allocate",
             dependencies=[Depends(_requires(svc.PERMISSION_ALLOCATE))])
def post_allocate(imr_id: str, body: _AllocateIn, request: Request,
                  response: Response,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.allocate(
        session, imr_id=imr_id, actor=_actor(request),
        idempotency_key=body.idempotency_key, expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/issue",
             dependencies=[Depends(_requires(svc.PERMISSION_ISSUE))])
def post_issue(imr_id: str, body: _MoveIn, request: Request, response: Response,
               database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.issue(
        session, imr_id=imr_id, quantity=body.quantity, actor=_actor(request),
        idempotency_key=body.idempotency_key, to_location_id=body.to_location_id,
        reference=body.reference, note=body.note,
        expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/return",
             dependencies=[Depends(_requires(svc.PERMISSION_ISSUE))])
def post_return(imr_id: str, body: _MoveIn, request: Request, response: Response,
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.return_material(
        session, imr_id=imr_id, quantity=body.quantity, actor=_actor(request),
        idempotency_key=body.idempotency_key, reference=body.reference,
        note=body.note, expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/consume",
             dependencies=[Depends(_requires(svc.PERMISSION_ISSUE))])
def post_consume(imr_id: str, body: _MoveIn, request: Request, response: Response,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.consume(
        session, imr_id=imr_id, quantity=body.quantity, actor=_actor(request),
        idempotency_key=body.idempotency_key, reference=body.reference,
        note=body.note, expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/transfer",
             dependencies=[Depends(_requires(svc.PERMISSION_ISSUE))])
def post_transfer(imr_id: str, body: _TransferIn, request: Request,
                  response: Response,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.transfer(
        session, imr_id=imr_id, quantity=body.quantity,
        to_location_id=body.to_location_id, actor=_actor(request),
        idempotency_key=body.idempotency_key, reference=body.reference,
        note=body.note, expected_version=body.version_no,
        correlation_id=_correlation_id(request)))


@router.post(_PREFIX + "/internal-material-requests/{imr_id}/cancel",
             dependencies=[Depends(_requires(svc.PERMISSION_CANCEL))])
def post_cancel(imr_id: str, body: _CancelIn, request: Request, response: Response,
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    return _run(request, response, database, lambda session: svc.cancel(
        session, imr_id=imr_id, reason=body.reason, actor=_actor(request),
        expected_version=body.version_no,
        correlation_id=_correlation_id(request)))
