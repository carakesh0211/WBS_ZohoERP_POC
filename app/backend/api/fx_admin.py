"""FastAPI router for exchange-rate administration (Fable 5.1, migration 028;
service ``pg/fx_admin.py``).

Routes, all under the router's ``fx.read`` floor; mutating routes add
``fx.manage``:

    GET    /api/fx/rates?source=&target=&active=&status=&rate_source=
                        &effective_from=&effective_to=&cursor=&limit=   fx.read
    POST   /api/fx/rates                                  fx.manage  (Idempotency-Key)
    GET    /api/fx/rates/lookup?source=&target=&date=&rate_source=&amount_minor=
                                                          fx.read    (404 FX_RATE_UNAVAILABLE)
    GET    /api/fx/rates/history?source=&target=          fx.read
    POST   /api/fx/rates/import/preview                   fx.manage
    POST   /api/fx/rates/import                           fx.manage  (all-or-nothing, Idempotency-Key)
    GET    /api/fx/rates/{fx_rate_id}                     fx.read
    POST   /api/fx/rates/{fx_rate_id}/activate            fx.manage  (FX_SELF_ACTIVATION)
    POST   /api/fx/rates/{fx_rate_id}/deactivate          fx.manage

Authorisation runs BEFORE any lookup: the router dependency authenticates and
checks the floor, the per-route dependency checks ``fx.manage``, and only then
does a handler open a session -- so an unauthenticated caller naming an
unknown id gets 401, a reader gets 403, and only a manager learns it is 404.

Idempotency follows ``api/budgets_original.py`` exactly: the key is scoped to
the caller, the route and the body's hash; a replay returns the first
response and creates nothing; keys live in a bounded in-process LRU. The
durable guarantee is the service's own -- ``create_rate`` is idempotent on
the natural key and value, so a replay that misses the LRU still creates
nothing.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..pg import fx_admin as svc
from ..pg.engine import Database
from .budget import (_actor, _get_database, _problem, _requires, _scope_for,
                     _set_correlation_header)


class _FxAccess:
    """Router-level dependency: authenticate, and require ``fx.read``, on
    EVERY route here. Declared on the ROUTER so a route added later cannot
    ship unguarded by omission -- the same reasoning as ``_BudgetAccess``.
    Populates ``request.state.budget_principal`` so ``_requires``, ``_actor``
    and ``_scope_for`` from ``api/budget.py`` work unchanged."""

    def __call__(self, request: Request) -> dict:
        from ..main import principal
        from .. import auth as auth_mod

        who = principal(
            authorization=request.headers.get("Authorization", ""),
            x_session=request.headers.get("X-Session", ""),
        )
        try:
            auth_mod.require(who, "fx.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message})
        request.state.budget_principal = who
        return who


require_fx_access = _FxAccess()

router = APIRouter(dependencies=[Depends(require_fx_access)])

# ------------------------------------------------------------ idempotency
_IDEMPOTENCY_MAX = 5_000
_idempotency_lock = threading.Lock()
_idempotency: "OrderedDict[str, tuple[int, dict[str, Any]]]" = OrderedDict()


def _idem_key(request: Request, key: str, body_sha: str) -> str:
    return hashlib.sha256(f"{_actor(request)}|{request.url.path}|{key}|{body_sha}".encode()).hexdigest()


def _idem_get(k: str) -> tuple[int, dict[str, Any]] | None:
    with _idempotency_lock:
        hit = _idempotency.get(k)
        if hit is not None:
            _idempotency.move_to_end(k)
        return hit


def _idem_put(k: str, status: int, body: dict[str, Any]) -> None:
    with _idempotency_lock:
        _idempotency[k] = (status, body)
        _idempotency.move_to_end(k)
        while len(_idempotency) > _IDEMPOTENCY_MAX:
            _idempotency.popitem(last=False)


def _body_sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _svc_error(exc: svc.FxAdminError) -> HTTPException:
    problem = _problem(exc.status, exc.code, exc.code.replace("_", " ").title(), exc.message)
    if exc.detail is not None:
        problem.detail["problems"] = exc.detail
    return problem


# ------------------------------------------------------------ models
class _RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_currency: str
    rate_date: date
    rate: str | int          # an exact decimal STRING (or the integer 1); a float is refused
    rate_source: str
    to_currency: str = "INR"
    source_reference: str | None = None
    note: str | None = None


class _ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, Any]] = Field(default_factory=list)


class _ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


# ------------------------------------------------------------ routes
@router.get("/api/fx/rates")
def get_rates(response: Response, request: Request,
              source: str | None = Query(default=None),
              target: str | None = Query(default=None),
              active: bool | None = Query(default=None),
              status: str | None = Query(default=None),
              rate_source: str | None = Query(default=None),
              effective_from: date | None = Query(default=None),
              effective_to: date | None = Query(default=None),
              limit: int = Query(default=50, ge=1, le=200),
              cursor: str | None = Query(default=None),
              database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.list_rates(session, from_currency=source, to_currency=target, active=active,
                                  status=status, rate_source=rate_source, effective_from=effective_from,
                                  effective_to=effective_to, limit=limit, cursor=cursor)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.post("/api/fx/rates", status_code=201, dependencies=[Depends(_requires("fx.manage"))])
def post_rate(body: _RateIn, response: Response, request: Request,
              idempotency_key: str = Header(default="", alias="Idempotency-Key"),
              database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    payload = body.model_dump()
    key = None
    if idempotency_key:
        key = _idem_key(request, idempotency_key, _body_sha(payload))
        hit = _idem_get(key)
        if hit is not None:
            response.status_code = hit[0]
            response.headers["Idempotent-Replay"] = "true"
            return hit[1]
    try:
        with database.session(_scope_for(request, database)) as session:
            result = svc.create_rate(session, actor=_actor(request), **payload)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)
    status = 201 if result.get("created") else 200
    response.status_code = status
    if key:
        _idem_put(key, status, result)
    return result


@router.get("/api/fx/rates/lookup")
def get_lookup(response: Response, request: Request,
               source: str = Query(...),
               date_: date = Query(..., alias="date"),
               target: str = Query(default="INR"),
               rate_source: str | None = Query(default=None),
               amount_minor: int | None = Query(default=None),
               database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.lookup_rate(session, from_currency=source, to_currency=target, on_date=date_,
                                   rate_source=rate_source, amount_minor=amount_minor)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.get("/api/fx/rates/history")
def get_history(response: Response, request: Request,
                source: str = Query(...),
                target: str = Query(default="INR"),
                limit: int = Query(default=200, ge=1, le=1000),
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.history(session, from_currency=source, to_currency=target, limit=limit)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.post("/api/fx/rates/import/preview", dependencies=[Depends(_requires("fx.manage"))])
def post_import_preview(body: _ImportIn, response: Response, request: Request,
                        database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.import_preview(session, body.rows)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.post("/api/fx/rates/import", status_code=201, dependencies=[Depends(_requires("fx.manage"))])
def post_import(body: _ImportIn, response: Response, request: Request,
                idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    key = None
    if idempotency_key:
        key = _idem_key(request, idempotency_key, _body_sha(body.rows))
        hit = _idem_get(key)
        if hit is not None:
            response.status_code = hit[0]
            response.headers["Idempotent-Replay"] = "true"
            return hit[1]
    try:
        with database.session(_scope_for(request, database)) as session:
            result = svc.import_rates(session, actor=_actor(request), rows=body.rows)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)
    if key:
        _idem_put(key, 201, result)
    return result


@router.get("/api/fx/rates/{fx_rate_id}")
def get_rate(fx_rate_id: str, response: Response, request: Request,
             database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.get_rate(session, fx_rate_id)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.post("/api/fx/rates/{fx_rate_id}/activate", dependencies=[Depends(_requires("fx.manage"))])
def post_activate(fx_rate_id: str, response: Response, request: Request,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.activate_rate(session, actor=_actor(request), fx_rate_id=fx_rate_id)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)


@router.post("/api/fx/rates/{fx_rate_id}/deactivate", dependencies=[Depends(_requires("fx.manage"))])
def post_deactivate(fx_rate_id: str, body: _ReasonIn, response: Response, request: Request,
                    database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return svc.deactivate_rate(session, actor=_actor(request), fx_rate_id=fx_rate_id,
                                       reason=body.reason)
    except svc.FxAdminError as exc:
        raise _svc_error(exc)
