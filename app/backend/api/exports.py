"""FastAPI router for asynchronous exports: ``/api/exports/*``.

    GET    /api/exports/datasets
    POST   /api/exports                              -> 202 + job id
    GET    /api/exports
    GET    /api/exports/{export_job_id}
    POST   /api/exports/{export_job_id}/advance
    POST   /api/exports/{export_job_id}/cancel
    POST   /api/exports/{export_job_id}/retry
    GET    /api/exports/{export_job_id}/result

WHY 202 AND NOT A FILE
======================

`docs/WAVE7_CONTRACT.md`: "Long exports answer 202 with a job id; no route
exceeds the 30-second AppSail budget." ``POST /api/exports`` therefore does
three things and stops -- validates the dataset and filters, resolves the
caller's scope, writes the job row -- and answers 202 with a ``Location`` and
a poll interval. Approved targets are 100,000 rows browsed responsively and
250,000 rows exported chunked; neither is a synchronous response.

Nothing here streams the source query either. A streaming response holds the
transaction open for as long as the client takes to read it, which is the same
30-second problem with a longer fuse and a held connection.

``/advance`` IS THE REQUESTER'S OWN, AND ONLY THEIR OWN
======================================================

Every chunk is bounded work: it commits and returns ``more: true`` when there
is more. The SPA drives its own job with it, so an export completes without
waiting for a scheduler, and `exports.run_due_jobs` remains the estate-wide
sweep with no HTTP route of its own.

The ownership check is not the permission check. `export.create` says a role
may export; `exports.get_job(..., requester=...)` says WHICH jobs exist for
this caller, and a job belonging to somebody else comes back as 404 with the
same code as a job that never existed. A 403 there would confirm the id is
real, which is an existence oracle over another user's exports.

**And advancing somebody else's job would still not read their rows.** The
worker opens its session under the JOB's rehydrated scope, never the caller's.
The ownership check is about not letting one user consume another's budget and
observe their timing, not about data.

PERMISSIONS
===========

The router floor is ``budget.read``, which every role holds, so every caller
authenticates and clears the floor and then stops -- or does not -- at the
route's own ``export.create``. `export.create` deliberately excludes Auditor:
`tests/test_api_auth.py::test_aud_c_006_auditor_is_read_only` pins that role to
an allow-list, creating an export job is a row INSERT, and widening an
audit-finding assertion so a new feature reads tidily is not a thing to do in
passing. An Auditor reads through the audit chain and the report API.

Reading a job (``GET``) needs only the floor: a caller can only ever see their
own jobs, and refusing them sight of their own queued work would be a
permission that protects nothing.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import observability
from ..pg import exports as export_svc
from ..pg import principal_scope
from ..pg.engine import Database, Scope, get_database

_CORRELATION_HEADER = "X-Correlation-Id"

#: Every transaction this router opens is bounded by PostgreSQL, not by a
#: Python timer: `SET LOCAL statement_timeout` is checked by the server
#: mid-statement, where an `asyncio.wait_for` around a blocking driver call is
#: not checked at all.
_STATEMENT_TIMEOUT = "25s"

#: How many rows one `/advance` call will write before committing and handing
#: control back. Well inside the 30-second budget with the statement timeout
#: above as the backstop; being wrong about it costs a resume, not a failure.
_ROWS_PER_ADVANCE = 25000

#: What a poller should wait between `GET /api/exports/{id}` calls. Advertised
#: rather than left to the client to guess at.
_POLL_AFTER_SECONDS = 2


# ================================================================ access control
class _ExportAccess:
    """Router-level dependency: authenticate, and require `budget.read`, on
    EVERY export route.

    Declared on the ROUTER, exactly as `api/budget.py::_BudgetAccess` and
    `api/integrations.py::_IntegrationAccess` are, so a route added to this
    file later inherits the guard instead of shipping open by omission.
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
            auth_mod.require(who, "budget.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.export_principal = who
        return who


require_export_access = _ExportAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's floor.

    Resolved by `auth.require` against `auth.PERMISSIONS`. No local table and
    no fallback branch: a previous wave's fallback granted a role the
    authoritative table excluded, and did so silently.
    """

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "export_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_export_access)])


# ==================================================================== plumbing
def _problem(status_code: int, code: str, title: str, detail: str | None = None,
             *, extra: dict[str, Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
        # C10 declares no message id for the export surface. A plausible-looking
        # MSG-EXP-nnn here would not resolve, and the frontend renders an empty
        # string where the explanation should be.
        "message_id": None,
    }
    if extra:
        body.update(extra)
    return HTTPException(status_code=status_code, detail=body)


def _get_database() -> Database:
    """Degrade to a clean, enveloped 503 -- never a 500 traceback.

    Carries `unavailable`, because "this build has no database configured" and
    "something broke" want different actions from whoever is reading, and
    `core/api-client.js` distinguishes them on that flag.
    """
    try:
        return get_database()
    except RuntimeError as exc:
        raise _problem(
            503, "DATABASE_NOT_CONFIGURED", "Database Not Configured",
            "a PostgreSQL database: the export API is mounted, but this "
            "process has none configured, so no export can be queued, run or "
            "downloaded here. Set CAPEX_DB_URL and run the migrations.",
            extra={"unavailable": True, "missing": "CAPEX_DB_URL"},
        ) from exc


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "export_principal", None) or {}


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    Never a caller-supplied header or body field. This value is written to
    `export_job.requested_by`, which decides whose scope the export runs
    under -- a caller-supplied one would be a caller-supplied authorisation.
    """
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    Contract 4's single construction path. Fails closed: a principal who
    cannot be resolved gets a scope that compiles to FALSE. This router builds
    no scope of its own and never sets `read_all`.
    """
    return principal_scope.scope_for_request(database, _principal_of(request))


class _Txn:
    """`database.session(scope)` with the statement timeout applied INSIDE the
    transaction -- `SET LOCAL` outside one is discarded silently, and the guard
    would read as present while doing nothing."""

    def __init__(self, database: Database, scope: Scope):
        self._cm = database.session(scope)

    def __enter__(self):
        session = self._cm.__enter__()
        session.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'")
        return session

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def _session(request: Request, database: Database) -> _Txn:
    return _Txn(database, _scope_for(request, database))


def _correlation_id(request: Request) -> str:
    return (observability.get_correlation_id()
            or request.headers.get(_CORRELATION_HEADER)
            or uuid4().hex[:12])


def _set_correlation_header(response: Response, request: Request) -> str:
    cid = _correlation_id(request)
    response.headers[_CORRELATION_HEADER] = cid
    return cid


def _export_error(exc: export_svc.ExportError) -> HTTPException:
    """Every refusal this module makes reaches the wire with its code."""
    return _problem(exc.status, exc.code, exc.code.replace("_", " ").title(),
                    exc.message, extra=(exc.detail or None))


def _scope_unavailable(exc: Exception) -> HTTPException:
    """`ScopeResolutionUnavailable` is 503, not an empty 200.

    A denial is an authorisation ANSWER and renders as no rows. A resolution
    FAILURE is not an answer at all, and rendering it identically shows a
    caller a healthy, empty job list while the identity tables are unreachable.
    """
    return _problem(
        503, "SCOPE_UNAVAILABLE", "Scope Unavailable",
        "your access could not be determined, so no export was read or "
        "written. This is not an empty result.",
        extra={"unavailable": True, "error_class": type(exc).__name__})


# ==================================================================== models
class CreateExportRequest(BaseModel):
    """The request body. `requested_by`, `scope` and any permission field are
    deliberately ABSENT and are rejected if sent: identity and scope are
    server-derived, and `extra="forbid"` makes an attempt to supply them a 422
    rather than a silently ignored field."""

    model_config = ConfigDict(extra="forbid")

    dataset: str
    filters: dict[str, Any] | None = None
    chunk_rows: int | None = Field(default=None, ge=1,
                                   le=export_svc.MAX_CHUNK_ROWS)
    ttl_hours: int | None = Field(default=None, ge=1, le=168)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


# ================================================================== datasets
# Declared BEFORE `/api/exports/{export_job_id}`: FastAPI matches in
# declaration order, so the parameterised route would otherwise capture
# "datasets" as a job id and answer 404 for the catalogue.
@router.get("/api/exports/datasets")
def list_datasets(response: Response, request: Request) -> dict[str, Any]:
    """The export catalogue: every dataset, its columns IN ORDER, and the
    filters it can and cannot apply.

    The column order is published because it is a contract: a consumer parsing
    the CSV positionally needs to know it is stable, and a job captures this
    exact order at creation so a registry change mid-flight cannot reorder a
    half-written file.

    The refusals are published too. A caller who can see that
    `budget_ledger_cells` will not accept `period_ids` writes a different
    request; a caller who discovers it by receiving an unfiltered file does not
    discover it at all.
    """
    _set_correlation_header(response, request)
    return {
        "items": [
            {
                "dataset": d.name,
                "title": d.title,
                "format": "csv",
                "columns": [
                    {"name": c.name, "kind": c.kind} for c in d.columns
                ],
                "filters_supported": sorted(d.filters),
                "filters_refused": {k: v for k, v in sorted(d.unsupported.items())},
                "sort_key": list(d.key_sql),
            }
            for d in sorted(export_svc.DATASETS.values(), key=lambda x: x.name)
        ],
        "filter_fields": list(export_svc.FILTER_FIELDS),
        "filter_fields_not_applicable": sorted(
            export_svc.FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT),
        "filterset_source": export_svc.FILTERSET_SOURCE,
        "money_note": (
            "every money column is rendered from integer paise as an exact "
            "decimal string; no float, no locale, no thousands separator"),
    }


# =================================================================== creation
@router.post("/api/exports", status_code=202,
             dependencies=[Depends(_requires("export.create"))])
def create_export(response: Response, request: Request,
                  body: CreateExportRequest = Body(...),
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    """Queue an export. 202, always -- never a file, never a wait.

    The scope written onto the job is the one `scope_for_request` resolved for
    THIS request, and `exports.create_job` refuses to store it under any other
    actor's name.
    """
    cid = _set_correlation_header(response, request)
    actor = _actor(request)

    # Validate before opening a transaction: an unknown dataset or an
    # unapplicable filter is a 400 that should not cost a connection.
    try:
        export_svc.plan_job(body.dataset, body.filters)
    except export_svc.ExportError as exc:
        raise _export_error(exc)

    try:
        scope = _scope_for(request, database)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)

    try:
        with _Txn(database, scope) as session:
            job = export_svc.create_job(
                session, dataset=body.dataset, filters=body.filters,
                scope=scope, requested_by=actor,
                principal_kind=str(_principal_of(request).get(
                    "principal_kind") or "USER"),
                correlation_id=cid,
                chunk_rows=(body.chunk_rows or export_svc.DEFAULT_CHUNK_ROWS),
                ttl_hours=(body.ttl_hours or export_svc.DEFAULT_TTL_HOURS))
    except export_svc.ExportError as exc:
        raise _export_error(exc)

    public = export_svc.public_job(job)
    response.headers["Location"] = f"/api/exports/{job['export_job_id']}"
    response.headers["Retry-After"] = str(_POLL_AFTER_SECONDS)
    return {
        **public,
        "poll": {
            "status_url": f"/api/exports/{job['export_job_id']}",
            "advance_url": f"/api/exports/{job['export_job_id']}/advance",
            "result_url": f"/api/exports/{job['export_job_id']}/result",
            "retry_after_seconds": _POLL_AFTER_SECONDS,
        },
        "accepted_note": (
            "the export is queued. It runs under YOUR resolved scope, captured "
            "immutably on the job, and contains no row you could not read "
            "yourself."),
    }


# ==================================================================== reading
@router.get("/api/exports")
def list_exports(response: Response, request: Request,
                 cursor: str | None = Query(default=None),
                 limit: int = Query(default=50, ge=1, le=200),
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    """The caller's OWN export jobs, newest first. There is no route that lists
    anybody else's."""
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            page = export_svc.list_jobs(session, requester=actor,
                                        limit=limit, cursor=cursor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    except export_svc.ExportError as exc:
        raise _export_error(exc)
    return {"items": [export_svc.public_job(j) for j in page["items"]],
            "next_cursor": page["next_cursor"], "has_more": page["has_more"]}


@router.get("/api/exports/{export_job_id}")
def get_export(export_job_id: str, response: Response, request: Request,
               database: Database = Depends(_get_database)) -> dict[str, Any]:
    """Status and progress.

    404 for a job that is not the caller's, with the same code as one that
    never existed -- see the module docstring on the existence oracle.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            job = export_svc.get_job(session, export_job_id, requester=actor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    if job is None:
        raise _problem(404, "EXPORT_JOB_NOT_FOUND", "Export Job Not Found",
                       f"no export job {export_job_id!r}")
    return export_svc.public_job(job)


@router.get("/api/exports/{export_job_id}/result",
            response_class=PlainTextResponse)
def get_export_result(export_job_id: str, request: Request,
                      database: Database = Depends(_get_database)
                      ) -> PlainTextResponse:
    """The rendered file.

    409 while it is still running, 410 once it has expired: "not ready yet" and
    "the rows were purged" are different facts and a caller acts differently on
    each. The digest recorded at completion is re-verified before a byte is
    served.
    """
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            body, meta = export_svc.read_result(session, export_job_id,
                                                requester=actor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    except export_svc.ExportError as exc:
        raise _export_error(exc)
    return PlainTextResponse(
        content=body,
        media_type=meta.get("media_type") or "text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{meta.get("filename")}"',
            "X-Export-Rows": str(meta.get("rows")),
            "X-Export-Sha256": str(meta.get("sha256")),
            _CORRELATION_HEADER: _correlation_id(request),
        })


# ==================================================================== control
@router.post("/api/exports/{export_job_id}/advance",
             dependencies=[Depends(_requires("export.create"))])
def advance_export(export_job_id: str, response: Response, request: Request,
                   database: Database = Depends(_get_database)) -> dict[str, Any]:
    """Run ONE bounded invocation of the caller's own export.

    The ownership check happens first and answers 404 for somebody else's job.
    The work itself then runs in a session opened under the JOB's rehydrated
    scope -- not this request's -- which is why advancing a job could never
    have leaked a row even without the ownership check.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            owned = export_svc.get_job(session, export_job_id, requester=actor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    if owned is None:
        raise _problem(404, "EXPORT_JOB_NOT_FOUND", "Export Job Not Found",
                       f"no export job {export_job_id!r}")

    try:
        outcome = export_svc.advance_job(database, export_job_id,
                                         rows_budget=_ROWS_PER_ADVANCE)
    except export_svc.ExportError as exc:
        raise _export_error(exc)

    if outcome.get("more"):
        response.status_code = 202
        response.headers["Retry-After"] = str(_POLL_AFTER_SECONDS)
    return {
        **outcome,
        "advance_url": f"/api/exports/{export_job_id}/advance",
        "status_url": f"/api/exports/{export_job_id}",
    }


@router.post("/api/exports/{export_job_id}/cancel",
             dependencies=[Depends(_requires("export.create"))])
def cancel_export(export_job_id: str, response: Response, request: Request,
                  body: CancelRequest = Body(default=CancelRequest()),
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    """Ask an export to stop. Honoured at the next chunk boundary.

    Cancelling is the requester's own right over their own job -- it carries
    `export.create` rather than a separate permission, because a role that may
    start an export and may not stop one is the pair the wrong way round.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            job = export_svc.request_cancel(session, export_job_id,
                                            requester=actor, actor=actor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    except export_svc.ExportError as exc:
        raise _export_error(exc)
    return export_svc.public_job(job)


@router.post("/api/exports/{export_job_id}/retry",
             dependencies=[Depends(_requires("export.create"))])
def retry_export(export_job_id: str, response: Response, request: Request,
                 database: Database = Depends(_get_database)) -> dict[str, Any]:
    """Re-queue a FAILED export from scratch.

    Every committed chunk is discarded: a retry that resumed from a partial
    file would splice rows read under one state of the data onto rows read
    under another, and the result would be self-consistent and wrong. The
    captured scope is untouched -- it is immutable, and re-capturing it would
    silently re-authorise the job against grants that may since have widened.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            job = export_svc.retry_job(session, export_job_id,
                                       requester=actor, actor=actor)
    except principal_scope.ScopeResolutionUnavailable as exc:
        raise _scope_unavailable(exc)
    except export_svc.ExportError as exc:
        raise _export_error(exc)
    return export_svc.public_job(job)
