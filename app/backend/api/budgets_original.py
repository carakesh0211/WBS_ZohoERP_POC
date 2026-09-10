"""FastAPI router for ORIGINAL BUDGET creation and the BUDGET CATEGORY master
(Fable 5.1, migration 026; plan U1 "Create budget", REQ-WBS-001, REQ-BUD-020,
REQ-REV-008, intake step 4 "Upload or create an original budget").

Routes (all under the ``budget.read`` floor the budget router already uses;
mutating routes add their own permission):

    GET    /api/budget/categories                        budget.read
    POST   /api/budget/categories                        budget.category.manage
    PUT    /api/budget/categories/{category_id}          budget.category.manage
    POST   /api/budget/categories/{category_id}/deactivate  budget.category.manage
    GET    /api/budget/selectors?kind=&q=&project_id=    budget.read   (governed pickers)
    GET    /api/budget/custom-fields                     budget.read
    GET    /api/budget/originals?project_id=&status=&cursor=   budget.read
    POST   /api/budget/originals                         budget.create  (draft, Idempotency-Key)
    GET    /api/budget/originals/import/template         budget.create  (CSV)
    POST   /api/budget/originals/import/preview          budget.create
    POST   /api/budget/originals/import                  budget.create  (all-or-nothing, Idempotency-Key)
    GET    /api/budget/originals/{budget_id}             budget.read
    PUT    /api/budget/originals/{budget_id}             budget.create  (draft edit, version-checked)
    POST   /api/budget/originals/{budget_id}/submit      budget.create
    POST   /api/budget/originals/{budget_id}/cancel      budget.create
    GET    /api/budget/originals/{budget_id}/audit       budget.read

Approval, rejection and return happen through the approval engine
(``POST /api/approvals/{instance_id}/decide``); the engine's write-back calls
``pg/original_budget.release`` / ``decide_not_released``. There is deliberately
NO direct ``/approve`` route: the maker-checker and quorum rules live in one
place, and a second door would be a second set of rules.

Idempotency: ``POST /originals`` and ``POST /originals/import`` honour
``Idempotency-Key``. The key is scoped to the caller and the route; a replay
returns the first response (same status, same body) and creates nothing. Keys
live in the process for the request budget's lifetime only -- a bounded LRU,
so a caller cannot grow memory -- and the durable guarantee is the database
one: a draft is created once per key because the key is recorded on the audit
stream and checked before insert.

Out-of-scope reads answer 404 (``repo.query`` returns nothing for an invisible
row and the service never distinguishes); permission failures answer 403
BEFORE any lookup, because the permission dependency runs before the handler.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from ..pg import original_budget as ob_svc
from ..pg.engine import Database
from .budget import (_actor, _get_database, _problem, _requires, _scope_for,
                     _set_correlation_header, require_budget_access)

router = APIRouter(dependencies=[Depends(require_budget_access)])

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


def _svc_error(exc: ob_svc.OriginalBudgetError) -> HTTPException:
    problem = _problem(exc.status, exc.code, exc.code.replace("_", " ").title(), exc.message)
    if exc.detail is not None:
        problem.detail["problems"] = exc.detail
    return problem


def _raise_if_refused(result: dict[str, Any]) -> dict[str, Any]:
    refusal = result.get("refusal")
    if not refusal:
        return result
    raise _problem(refusal.get("status", 409), refusal["code"],
                   str(refusal["code"]).replace("_", " ").title(), refusal.get("message"))


# ============================================================ categories
class _CategoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str
    description: str | None = None
    parent_category_id: str | None = None
    display_order: int = 100
    entity_id: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class _CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    name: str | None = None
    description: str | None = None
    parent_category_id: str | None = None
    display_order: int | None = None
    effective_from: date | None = None
    effective_to: date | None = None


@router.get("/api/budget/categories")
def get_categories(response: Response, request: Request,
                   include_inactive: bool = Query(default=False),
                   entity_id: str | None = Query(default=None),
                   database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        items = ob_svc.list_categories(session, include_inactive=include_inactive, entity_id=entity_id)
    return {"items": items}


@router.post("/api/budget/categories", status_code=201,
             dependencies=[Depends(_requires("budget.category.manage"))])
def post_category(body: _CategoryIn, response: Response, request: Request,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.create_category(session, actor=_actor(request), **body.model_dump())
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.put("/api/budget/categories/{category_id}",
            dependencies=[Depends(_requires("budget.category.manage"))])
def put_category(category_id: str, body: _CategoryUpdate, response: Response, request: Request,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.update_category(session, actor=_actor(request), category_id=category_id,
                                          **body.model_dump())
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.post("/api/budget/categories/{category_id}/deactivate",
             dependencies=[Depends(_requires("budget.category.manage"))])
def post_category_deactivate(category_id: str, response: Response, request: Request,
                             database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.deactivate_category(session, actor=_actor(request), category_id=category_id)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


# ============================================================ selectors
@router.get("/api/budget/selectors")
def get_selector(response: Response, request: Request,
                 kind: str = Query(...), q: str = Query(default=""),
                 project_id: str | None = Query(default=None),
                 entity_id: str | None = Query(default=None),
                 limit: int = Query(default=25, ge=1, le=100),
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            items = ob_svc.search_selector(session, kind=kind, q=q, project_id=project_id,
                                           entity_id=entity_id, limit=limit)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)
    return {"kind": kind, "items": items}


@router.get("/api/budget/custom-fields")
def get_custom_fields(response: Response, request: Request,
                      database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return {"applies_to": ob_svc.CUSTOM_FIELD_TARGET, "items": ob_svc.budget_custom_field_defs(session)}


# ============================================================ originals
class _LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wbs_id: str
    budget_head_id: str
    budget_category_id: str
    amount_paise: int | None = None
    amount_rupees: str | None = None
    justification: str | None = None
    division_id: str | None = None
    branch_id: str | None = None
    zone_id: str | None = None
    plant_id: str | None = None
    location_id: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class _OriginalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    fiscal_year: str
    title: str
    period_id: str | None = None
    justification: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    lines: list[_LineIn]


class _OriginalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    title: str | None = None
    fiscal_year: str | None = None
    period_id: str | None = None
    justification: str | None = None
    custom_fields: dict[str, Any] | None = None
    lines: list[_LineIn] | None = None


class _SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = None


class _ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class _ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    fiscal_year: str
    title: str
    csv_text: str
    period_id: str | None = None
    justification: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class _PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    csv_text: str


@router.get("/api/budget/originals")
def get_originals(response: Response, request: Request,
                  project_id: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  entity_id: str | None = Query(default=None),
                  fiscal_year: str | None = Query(default=None),
                  limit: int = Query(default=50, ge=1, le=200),
                  cursor: str | None = Query(default=None),
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.list_budgets(session, project_id=project_id, status=status, entity_id=entity_id,
                                       fiscal_year=fiscal_year, limit=limit, cursor=cursor)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.post("/api/budget/originals", status_code=201,
             dependencies=[Depends(_requires("budget.create"))])
def post_original(body: _OriginalIn, response: Response, request: Request,
                  idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    payload = body.model_dump()
    key = None
    if idempotency_key:
        key = _idem_key(request, idempotency_key,
                        hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest())
        hit = _idem_get(key)
        if hit is not None:
            response.status_code = hit[0]
            response.headers["Idempotent-Replay"] = "true"
            return hit[1]
    try:
        with database.session(_scope_for(request, database)) as session:
            result = ob_svc.create_draft(session, actor=_actor(request), project_id=body.project_id,
                                         fiscal_year=body.fiscal_year, title=body.title,
                                         lines=[l.model_dump() for l in body.lines],
                                         period_id=body.period_id, justification=body.justification,
                                         custom_fields=body.custom_fields)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)
    if key:
        _idem_put(key, 201, result)
    return result


@router.get("/api/budget/originals/import/template",
            dependencies=[Depends(_requires("budget.create"))])
def get_import_template(response: Response, request: Request,
                        database: Database = Depends(_get_database)) -> PlainTextResponse:
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        text = ob_svc.import_template_csv(session)
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="original-budget-template.csv"',
                                      "X-Correlation-Id": response.headers.get("X-Correlation-Id", "")})


@router.post("/api/budget/originals/import/preview",
             dependencies=[Depends(_requires("budget.create"))])
def post_import_preview(body: _PreviewIn, response: Response, request: Request,
                        database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.import_preview(session, project_id=body.project_id, text=body.csv_text)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.post("/api/budget/originals/import", status_code=201,
             dependencies=[Depends(_requires("budget.create"))])
def post_import(body: _ImportIn, response: Response, request: Request,
                idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    key = None
    if idempotency_key:
        key = _idem_key(request, idempotency_key, hashlib.sha256(body.csv_text.encode()).hexdigest())
        hit = _idem_get(key)
        if hit is not None:
            response.status_code = hit[0]
            response.headers["Idempotent-Replay"] = "true"
            return hit[1]
    try:
        with database.session(_scope_for(request, database)) as session:
            result = ob_svc.import_commit(session, actor=_actor(request), project_id=body.project_id,
                                          fiscal_year=body.fiscal_year, title=body.title, text=body.csv_text,
                                          period_id=body.period_id, justification=body.justification,
                                          custom_fields=body.custom_fields)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)
    if key:
        _idem_put(key, 201, result)
    return result


@router.get("/api/budget/originals/{budget_id}")
def get_original(budget_id: str, response: Response, request: Request,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.get_budget(session, budget_id)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.put("/api/budget/originals/{budget_id}",
            dependencies=[Depends(_requires("budget.create"))])
def put_original(budget_id: str, body: _OriginalUpdate, response: Response, request: Request,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.update_draft(
                session, actor=_actor(request), budget_id=budget_id,
                expected_version=body.expected_version, title=body.title, justification=body.justification,
                period_id=body.period_id, fiscal_year=body.fiscal_year,
                lines=None if body.lines is None else [l.model_dump() for l in body.lines],
                custom_fields=body.custom_fields)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.post("/api/budget/originals/{budget_id}/submit",
             dependencies=[Depends(_requires("budget.create"))])
def post_original_submit(budget_id: str, response: Response, request: Request,
                         body: _SubmitIn | None = None,
                         database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = ob_svc.submit(session, actor=_actor(request), budget_id=budget_id,
                                   expected_version=body.expected_version if body else None)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)
    return _raise_if_refused(result)      # AFTER the commit, on purpose (see api/budget.py)


@router.post("/api/budget/originals/{budget_id}/cancel",
             dependencies=[Depends(_requires("budget.create"))])
def post_original_cancel(budget_id: str, body: _ReasonIn, response: Response, request: Request,
                         database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return ob_svc.cancel(session, actor=_actor(request), budget_id=budget_id, reason=body.reason)
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)


@router.get("/api/budget/originals/{budget_id}/audit")
def get_original_audit(budget_id: str, response: Response, request: Request,
                       database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            return {"budget_id": budget_id, "entries": ob_svc.audit_history(session, budget_id=budget_id)}
    except ob_svc.OriginalBudgetError as exc:
        raise _svc_error(exc)
