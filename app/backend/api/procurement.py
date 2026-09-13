"""FastAPI router for the PostgreSQL procurement chain.

    POST /api/procurement/purchase-requests
    POST /api/procurement/purchase-requests/{pr_id}/submit
    POST /api/procurement/purchase-requests/{pr_id}/approve
    POST /api/procurement/purchase-requests/{pr_id}/convert
    POST /api/procurement/purchase-orders
    POST /api/procurement/purchase-orders/{po_id}/emit

Built to the same shape as ``api/budget.py`` -- router-level authentication
floor, per-route permission on top, ``repo``-scoped service calls underneath,
RFC-7807 error bodies -- because a second shape would be a second set of
mistakes.

WHY THE FLOOR IS ``budget.read``
================================

There is no ``procurement.read`` permission, and one is not invented here.
``auth.PERMISSIONS`` is a client sign-off (D-12) and three tests pin parts of
it -- ``test_aud_c_006_auditor_is_read_only`` in particular pins Auditor to an
allow-list of four. ``budget.read`` is held by every role, says exactly what
the floor means ("you may see the control data"), and leaves the real decision
to each route's own permission. Adding a permission to make a router read
tidily is not a change to make in passing.

WHY ``/approve`` TAKES EITHER OF TWO PERMISSIONS
================================================

``services.approve_pr`` chooses between ``pr.approve`` and
``pr.approve_exception`` from the request's own ``check_result``, and the two
are held by DIFFERENT roles: ProcurementApprover holds the first,
FinanceApprover the second. A route floor naming one would lock the other role
out of the requests it is the only role permitted to approve. So the floor is
"holds either", and the service then requires the RIGHT one for this request
and refuses if the caller holds only the other. A caller holding neither is
refused at the floor, which is what
``tests/test_api_auth.py``'s matrix row asserts.

WHY ``/emit`` PLANS AND ENQUEUES BUT DOES NOT SEND
==================================================

The route writes ``integration_outbox`` rows and returns. It never calls Zoho,
and it cannot: the adapter it builds is given the default
:class:`~app.backend.integration.adapter.NoNetworkTransport`, which answers
``capabilities()`` -- pure configuration, which is all ``plan_emission`` reads
-- and refuses every request. Draining the outbox is §11.6's cron Function's
job, not a synchronous HTTP request's; a route that sent would hold a web
worker across a vendor call and would put a retry loop behind a browser
refresh.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import zoho
from ..integration.adapter import IntegrationError
from ..integration.books_inventory import BooksInventoryAdapter
from ..integration.erp import ErpAdapter
from ..pg import integration_store as store_svc
from ..pg import principal_scope
from ..pg import fx as fx_svc
from ..pg import procurement_services as procurement_svc
from ..pg.engine import Database, Scope, get_database

_CORRELATION_HEADER = "X-Correlation-Id"

#: The floor. See the module docstring for why it is `budget.read` and not a
#: permission invented for this router.
ROUTER_FLOOR = "budget.read"


class _ProcurementAccess:
    """Authenticate, then require the router floor, on EVERY route.

    Declared on the router rather than per route, so a route added later
    cannot ship unguarded by omission -- the failure ``api/budget.py``'s own
    docstring records the audit router shipping with.
    """

    def __call__(self, request: Request) -> dict:
        # Lazy import: `main` imports this module, so a module-level import
        # would be circular.
        from .. import auth as auth_mod
        from ..main import principal

        who = principal(
            authorization=request.headers.get("Authorization", ""),
            x_session=request.headers.get("X-Session", ""),
        )
        try:
            auth_mod.require(who, ROUTER_FLOOR)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.procurement_principal = who
        return who


require_procurement_access = _ProcurementAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's floor."""

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "procurement_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


def _requires_any(*permissions: str):
    """Holds AT LEAST ONE of these permissions.

    Not a weakening. It exists because ``pr.approve`` and
    ``pr.approve_exception`` are held by different roles and the route serves
    both -- which of the two is actually required is decided inside the
    transaction, from the request's own ``check_result``, by
    ``procurement.approve_pr``. A caller holding neither is refused here; a
    caller holding the wrong one of the two is refused there.
    """

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "procurement_principal", None) or {}
        last: Exception | None = None
        for permission in permissions:
            try:
                auth_mod.require(who, permission)
                return who
            except auth_mod.AuthError as exc:
                last = exc
        assert last is not None
        raise HTTPException(
            getattr(last, "status", 403),
            {"code": getattr(last, "code", "FORBIDDEN"),
             "message": (
                 f"Your role ({', '.join(who.get('roles') or []) or 'none'}) "
                 f"holds none of {', '.join(permissions)}.")})

    return _dep


router = APIRouter(dependencies=[Depends(require_procurement_access)])


def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- the posture
    ``api/budget.py``, ``api/audit.py`` and ``api/health.py`` all take."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The PostgreSQL procurement API is mounted but "
                               "no database is configured for this process."},
        ) from exc


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str,
             detail: str | None = None,
             extra: dict[str, Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
    }
    if extra:
        body.update(extra)
    return HTTPException(status_code=status_code, detail=body)


def _service_error_to_http(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "PROCUREMENT_ERROR")
    status = getattr(exc, "status", 400)
    message = getattr(exc, "message", str(exc))
    return _problem(status, code, code.replace("_", " ").title(), message,
                    extra=getattr(exc, "detail", None) or None)


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "procurement_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL grants. ``read_all`` comes from ``user_access_flag``
    and never from a role name -- see ``api/budget.py::_scope_for`` for the two
    earlier postures that got this wrong."""
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """SERVER-DERIVED. Never a header: this value is written into
    ``purchase_request.requested_by`` and into the audit chain, and
    maker-checker compares exactly these identities."""
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


# =================================================================== payloads

class _LineIn(BaseModel):
    """One line. Base paise on an INR document; source minor units on a
    foreign-currency purchase order (029) -- the service refuses the one that
    does not belong, so a caller cannot commit one number and quote another."""
    model_config = ConfigDict(extra="forbid")
    wbs_id: str
    budget_head_id: str
    amount_paise: int | None = None
    quantity: float = 1
    description: str | None = None
    rate_paise: int | None = None
    #: The vendor's figures in the ORDER'S currency's minor units (cents,
    #: yen, fils). Required on every line of a non-INR purchase order.
    source_amount_minor: int | None = None
    source_rate_minor: int | None = None


class _SourceLineIn(BaseModel):
    """A foreign-currency conversion names the vendor's figure per request line."""
    model_config = ConfigDict(extra="forbid")
    pr_line_id: str
    source_amount_minor: int
    source_rate_minor: int | None = None


class _PurchaseRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    lines: list[_LineIn] = Field(min_length=1)
    description: str | None = None
    pr_number: str | None = None
    #: Accepted so it can be REFUSED with a coded error rather than ignored.
    #: `pr_reservation` has no PostgreSQL table; see
    #: `procurement.ERR_RESERVATION_UNAVAILABLE`.
    reserve_budget: bool = False


class _VersionedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_no: int | None = None


class _ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None
    version_no: int | None = None
    #: The delegator, when this decision is made on somebody else's behalf.
    #: Checked against the contributor set alongside the actor, so a delegation
    #: can never launder a self-approval.
    acting_for_user_id: str | None = None
    #: Stream B: the Administrator's deliberate self-approval override. Ignored
    #: for every other role (refused, in fact) and never with a delegation.
    admin_override_reason: str | None = None



# ------------------------------------------------------- exchange rate (H-4)
# `exchange_rate` was `int = 1` on both models below, which is review finding
# H-4 and is the second, independent reason AUD-H-007's rate has never been
# applied to anything: Pydantic REJECTED 92.50 outright, so no non-integer rate
# could reach the database at all. The POC's own seed carries EUR at 92.50
# (`app/backend/db.py:540`), so the one foreign-currency purchase order in the
# fixtures could not have been created through this API.
#
# `Decimal`, parsed by `app/backend/pg/fx.parse_rate`, which is the SAME parser
# the FX service and migration 023's `numeric(18,8)` columns agree on: exact,
# scale 8, positive, and a FLOAT REFUSED rather than silently rounded. Declaring
# the field `float` would have been the other obvious repair and is the defect
# `app/backend/money.py` opens by recounting -- a rate is multiplied into money,
# so a float rate makes the product a float.
#
# `mode="before"`, so the raw JSON value is inspected BEFORE Pydantic coerces
# it. Without that, a JSON `92.5` arrives as a Python float, Pydantic converts
# it to Decimal, and the float refusal never sees the float it exists to refuse.
def _validate_exchange_rate(value: Any) -> Decimal | None:
    """`fx.parse_rate`, with its refusal re-raised as a ValueError.

    Pydantic v2 turns a ValueError inside a validator into a 422 with the
    message attached; any other exception type propagates and becomes a 500.
    An unparseable rate is a bad request, not a server fault, so the FxError is
    translated rather than allowed through.

    ``None`` means "not supplied" (029): the rate is then the ACTIVE one on
    file for the document date, or the named ``fx_rate_id``. A supplied rate
    must name its ``rate_source`` -- the FX engine refuses one without.
    """
    if value is None:
        return None
    try:
        return fx_svc.parse_rate(value)
    except fx_svc.FxError as exc:
        raise ValueError(exc.message) from exc


class _ConvertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_name: str
    po_number: str | None = None
    currency: str = "INR"
    #: 029: None = the ACTIVE rate on file for `document_date` (or the named
    #: `fx_rate_id`); a supplied figure must name `rate_source`.
    exchange_rate: Decimal | None = None
    rate_source: str | None = None
    fx_rate_id: str | None = None
    document_date: date | None = None
    #: Required when `currency` is not INR: the vendor's figure per request line.
    source_lines: list[_SourceLineIn] | None = None
    version_no: int | None = None

    _rate = field_validator("exchange_rate", mode="before")(
        classmethod(lambda cls, v: _validate_exchange_rate(v)))


class _PurchaseOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    vendor_name: str
    lines: list[_LineIn] = Field(min_length=1)
    po_number: str | None = None
    currency: str = "INR"
    #: 029: None = the ACTIVE rate on file for `document_date` (or the named
    #: `fx_rate_id`); a supplied figure must name `rate_source`.
    exchange_rate: Decimal | None = None
    rate_source: str | None = None
    fx_rate_id: str | None = None
    document_date: date | None = None

    _rate = field_validator("exchange_rate", mode="before")(
        classmethod(lambda cls, v: _validate_exchange_rate(v)))


class _EmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str
    vendor_external_id: str
    document_date: date
    #: A header-only tenant (D-7 False) turns one requisition spanning N
    #: control cells into N purchase orders the vendor acknowledges, receives
    #: against and invoices separately. That is a procurement PROCESS change
    #: and the plan refuses to proceed until it is acknowledged here.
    acknowledge_process_change: bool = False
    #: `{po_line_id: <tenant item id>}` -- the EXISTING tenant item each line
    #: is for, named by the operator like the vendor above. A line absent
    #: here is emitted as a description-only line; a key that is not a line
    #: of this order is refused (422 UNKNOWN_PO_LINE). Nothing is created in
    #: the tenant's item master from here.
    item_external_ids: dict[str, str] = Field(default_factory=dict)


# ========================================================= purchase requests

@router.post("/api/procurement/purchase-requests", status_code=201,
             dependencies=[Depends(_requires("pr.create"))])
def post_purchase_request(
    body: _PurchaseRequestIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return procurement_svc.create_pr(
                session, project_id=body.project_id,
                lines=[line.model_dump() for line in body.lines],
                actor=_actor(request), description=body.description,
                reserve=body.reserve_budget, pr_number=body.pr_number,
                correlation_id=_correlation_id(request))
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


@router.post("/api/procurement/purchase-requests/{pr_id}/submit",
             dependencies=[Depends(_requires("pr.create"))])
def post_purchase_request_submit(
    pr_id: str, body: _VersionedIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return procurement_svc.submit_pr(
                session, pr_id=pr_id, actor=_actor(request),
                expected_version=body.version_no,
                correlation_id=_correlation_id(request))
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


@router.post(
    "/api/procurement/purchase-requests/{pr_id}/approve",
    dependencies=[Depends(_requires_any("pr.approve", "pr.approve_exception"))])
def post_purchase_request_approve(
    pr_id: str, body: _ApproveIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return procurement_svc.approve_pr(
                session, pr_id=pr_id, actor=_actor(request),
                principal=_principal_of(request),
                acting_for_user_id=body.acting_for_user_id,
                reason=body.reason, expected_version=body.version_no,
                correlation_id=_correlation_id(request),
                admin_override_reason=body.admin_override_reason)
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


@router.post("/api/procurement/purchase-requests/{pr_id}/convert",
             status_code=201,
             dependencies=[Depends(_requires("po.amend"))])
def post_purchase_request_convert(
    pr_id: str, body: _ConvertIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return procurement_svc.convert_pr_to_po(
                session, pr_id=pr_id, actor=_actor(request),
                vendor_name=body.vendor_name, po_number=body.po_number,
                currency=body.currency, exchange_rate=body.exchange_rate,
                rate_source=body.rate_source, fx_rate_id=body.fx_rate_id,
                document_date=body.document_date,
                source_lines=([s.model_dump() for s in body.source_lines]
                              if body.source_lines is not None else None),
                expected_version=body.version_no,
                correlation_id=_correlation_id(request))
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


# =========================================================== purchase orders

@router.post("/api/procurement/purchase-orders", status_code=201,
             dependencies=[Depends(_requires("po.amend"))])
def post_purchase_order(
    body: _PurchaseOrderIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return procurement_svc.create_po(
                session, project_id=body.project_id,
                vendor_name=body.vendor_name,
                lines=[line.model_dump() for line in body.lines],
                actor=_actor(request), po_number=body.po_number,
                currency=body.currency, exchange_rate=body.exchange_rate,
                rate_source=body.rate_source, fx_rate_id=body.fx_rate_id,
                document_date=body.document_date,
                correlation_id=_correlation_id(request))
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)


def _metadata_only_adapter(connection: dict[str, Any]) -> Any:
    """The product adapter, wired to no transport at all.

    ``plan_emission`` reads exactly one thing off the adapter --
    ``capabilities().line_level_custom_fields`` -- and capabilities are
    configuration, not a fetch. Constructing the adapter without a transport
    gives it :class:`NoNetworkTransport`, which answers the metadata calls and
    REFUSES every request. So this route cannot reach Zoho even if a later
    edit asked it to, and the refusal is structural rather than a promise in a
    comment.
    """
    product = str(connection.get("product") or "")
    kwargs = {
        "organization_id": str(connection.get("organization_id") or ""),
        "dc": str(connection.get("dc") or "IN"),
    }
    if product == "ERP":
        return ErpAdapter(**kwargs)
    if product == "BOOKS_INVENTORY":
        return BooksInventoryAdapter(**kwargs)
    raise _problem(
        422, "UNKNOWN_PRODUCT", "Unknown Product",
        f"Connection {connection.get('connection_id')!r} declares product "
        f"{product!r}, for which this build ships no adapter. The emission was "
        f"not planned.")


def _outbound_writes_open() -> None:
    """409 ERP_WRITES_DISABLED before any lookup: the gate is a route dependency
    so it runs after the permission check and BEFORE the database is resolved,
    and the reply is the same whether or not the purchase order exists."""
    if not zoho.outbound_writes_enabled():
        raise _problem(
            409, "ERP_WRITES_DISABLED", "ERP Writes Disabled",
            "Outbound ERP writes are disabled in this deployment "
            "(CAPEX_ERP_OUTBOUND_WRITES is not 1). Nothing was planned or queued.")


@router.post("/api/procurement/purchase-orders/{po_id}/emit", status_code=202,
             dependencies=[Depends(_requires("connector.manage")),
                           Depends(_outbound_writes_open)])
def post_purchase_order_emit(
    po_id: str, body: _EmitIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Plan the emission and write the outbox rows. Sends nothing.

    202, not 201: the purchase order has been ACCEPTED for emission and the
    outbox rows exist; whether Zoho has it yet is a question only the drain
    Function can answer. A 201 here would claim a creation that has not
    happened.
    """
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            connection = store_svc.get_connection(session, body.connection_id)
            if connection is None:
                raise _problem(
                    404, "CONNECTION_NOT_FOUND", "Connection Not Found",
                    f"Connection {body.connection_id} does not exist.")
            adapter = _metadata_only_adapter(connection)
            return procurement_svc.plan_po_emission(
                session, po_id=po_id, connection_id=body.connection_id,
                adapter=adapter,
                vendor_external_id=body.vendor_external_id,
                document_date=body.document_date, actor=_actor(request),
                acknowledged=body.acknowledge_process_change,
                correlation_id=_correlation_id(request),
                item_external_ids=body.item_external_ids)
    except procurement_svc.ProcurementError as exc:
        raise _service_error_to_http(exc)
    except store_svc.IntegrationStoreError as exc:
        raise _service_error_to_http(exc)
    except IntegrationError as exc:
        raise _problem(422, "ADAPTER_REFUSED", "Adapter Refused", str(exc))
