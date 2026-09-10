"""CAPEX & WBS Control Hub - API.

Run:  python app/run.py

Post-audit corrections (2026-08-06):
  * Actor identity is derived from a server-side session. The request body can no
    longer nominate who is acting (AUD-C-006).
  * Every mutation is authorised by role and, where it is an approval, by
    segregation of duties.
  * Business refusals return controlled 4xx with a code and an actionable message;
    they never surface as 500 (AUD-H-008, AUD-M-007).
  * Destructive administration exists only under CAPEX_PROFILE=local-demo (AUD-C-010).
"""
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, domain, observability, services, zoho
from .api import health as health_api
from .pg import principal_scope as pg_principal_scope
from .pg import repo as pg_repo
from .money import MoneyError

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(APP_ROOT, "frontend")

log = logging.getLogger("capex")

# Fable 5.1: the interactive API explorers (/docs, /redoc) are served ONLY in
# the local-demo profile. On a hosted preview they are an unauthenticated,
# clickable map of every route. /openapi.json itself stays reachable in every
# profile because the analytics, closure and integration screens probe it to
# tell an unmounted route from an empty result (see integration-api.js) and
# fetch it without a session header; it exposes route TEMPLATES, never data.
_INTERACTIVE_DOCS = os.environ.get("CAPEX_PROFILE", "").lower() == "local-demo"
app = FastAPI(title="CAPEX & WBS Control Hub", version="0.2.0-poc-hardened",
              docs_url="/docs" if _INTERACTIVE_DOCS else None,
              redoc_url="/redoc" if _INTERACTIVE_DOCS else None)

# AUD-M-007. Without this the "capex" logger has no handler and an effective
# level of WARNING, so every structured record - and every redaction rule that
# lives in the formatter - is silently discarded. Installed at import so it
# covers the AppSail entrypoint and any Function bundle that imports the app.
observability.configure()

# /healthz and /readyz -- process liveness and PostgreSQL-schema readiness.
# Outside /api/, so the auth guard below never intercepts them: an
# orchestrator's health probe carries no session.
app.include_router(health_api.router)

# The PostgreSQL audit API is mounted UNCONDITIONALLY.
#
# It used to mount only when CAPEX_DB_URL/CAPEX_DB_HOST was set -- a decision
# taken ONCE, at import time. That made behaviour depend on whether any earlier
# import happened to see the variable, and it failed twice in CI: the entire
# end-to-end suite skipped because the router was absent while the database was
# configured perfectly well. An import-order-dependent toggle is not
# configuration, it is a race.
#
# The original reason for the toggle was a genuine collision: this router and
# the legacy SQLite endpoints both claimed `/api/audit/verify`, and a router
# included here registers before the decorators further down, so the legacy
# route was shadowed and four passing tests turned into 500s.
#
# That is now fixed at the source: the PostgreSQL chain check lives at
# `/api/audit/chain/verify`, which collides with nothing. `/api/audit/streams`
# and `/api/audit/entries` never collided. With no overlapping path there is
# nothing to gate, so the router mounts always and
# `api/audit.py::_get_database` returns a clean 503 when no database is
# configured -- a runtime answer to a runtime question.
try:
    from .api import audit as audit_api
except ImportError as exc:  # pragma: no cover - only before audit.py lands
    audit_api = None
    logging.getLogger("capex").warning(
        "app.backend.api.audit not available yet; its router is not mounted (%s)", exc)
else:
    app.include_router(audit_api.router)

# The Wave 2 routers are imported and mounted with NO try/except.
#
# They first shipped wrapped in `except Exception: <name> = None`, copied from
# the audit guard above. That guard exists only because audit.py post-dates
# main.py; these modules are product code. The blanket form turned any import
# error into fourteen silently missing routes, logged nothing, and passed
# locally while every one of them was absent in CI -- the authorisation-matrix
# gate is the only reason it was caught at all.
#
# A router that cannot be imported is a broken deployment. It should fail at
# import, loudly, with the real traceback, rather than serve an application
# that is quietly missing its budget, masters, settings and access APIs.
#
# Mounting is unconditional for the reason given above: a router whose
# database is unconfigured answers 503 from its own dependency. Making the
# MOUNT conditional is what let the audit routes vanish in CI twice while the
# job reported green.
from .api import admin_access as admin_access_api
from .api import approvals as approvals_api
from .api import budget as budget_api
from .api import budgets_original as budgets_original_api
from .api import exports as exports_api

from .api import closure as closure_api
from .api import integrations as integrations_api
from .api import masters as masters_api
from .api import procurement as procurement_api
from .api import reports as reports_api
from .api import settings as settings_api

app.include_router(budget_api.router)
# Fable 5.1. Original-budget creation and the budget CATEGORY master over
# migration 026. Same rule, same commit: its mutating paths are in
# `tests/test_api_auth.py::MUTATING_ROUTES`. Mounted right after the budget
# router because it shares that router's `budget.read` floor and helpers.
app.include_router(budgets_original_api.router)
app.include_router(masters_api.router)
app.include_router(settings_api.router)
app.include_router(admin_access_api.router)
app.include_router(approvals_api.router)
# Wave 5. Mounted in the SAME commit that adds its five mutating paths to
# `tests/test_api_auth.py::MUTATING_ROUTES`, because that matrix is built from
# the live OpenAPI schema: a route that mounts without its row makes
# `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
# fail immediately, and splitting the two across commits leaves a revision in
# history whose test suite cannot pass.
app.include_router(integrations_api.router)
# Wave 6. Same rule, same commit: the six mutating paths this router serves
# are in `tests/test_api_auth.py::MUTATING_ROUTES` in the commit that mounts
# it. `/api/procurement/*` is the PostgreSQL chain over migration 013's eight
# tables; the SQLite `/api/purchase-requests` and `/api/purchase-orders`
# routes above are untouched and stay deployable until the cutover.
app.include_router(procurement_api.router)
# Wave 7 stream A1. Same rule, same commit: the five mutating paths this
# router serves are in `tests/test_api_auth.py::MUTATING_ROUTES` in the commit
# that mounts it. `/api/reports/*` is the server-side reporting layer over the
# whole 002..016 estate; its reads are GET so a drill-down has a shareable
# address and so the mutation matrix keeps meaning "these really do change
# something" -- see `api/reports.py`'s docstring.
app.include_router(reports_api.router)

# Wave 7 stream A2. Same rule, same commit: the four mutating paths this router
# serves are in `tests/test_api_auth.py::MUTATING_ROUTES` in the commit that
# mounts it. `/api/exports/*` answers 202 with a job id and never a file, so no
# route here can exceed the 30-second AppSail budget however large the export.
app.include_router(exports_api.router)

# Wave 7. Same rule, same commit: the eight mutating paths this router serves
# are in `tests/test_api_auth.py::MUTATING_ROUTES` in the commit that mounts
# it. `/api/closure/*` is the PostgreSQL closure chain over migration 018's
# three tables; the SQLite `/api/capitalisation` routes above are untouched and
# stay deployable until the cutover. `/api/control/*` on the same router is
# three READ-ONLY views (SCR-11 / SCR-12 / SCR-14) over tables whose write side
# lives in `api/budget.py` and `api/procurement.py` and which offer no list
# route -- see that module's docstring.
app.include_router(closure_api.router)

PUBLIC_PATHS = {"/api/health", "/api/auth/login"}


def con():
    if not os.path.exists(db.DB_PATH):
        raise HTTPException(503, "Database not initialised. Run: python -m app.backend.migrate --fresh --seed")
    return db.connect()


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- identity
def principal(authorization: str = Header(default=""),
              x_session: str = Header(default="")) -> dict:
    """Server-derived acting user. There is no way for a caller to name someone else."""
    token = x_session or (authorization[7:] if authorization.lower().startswith("bearer ") else "")
    c = db.connect()
    try:
        return auth.resolve_session(c, token or None)
    except auth.AuthError as e:
        raise HTTPException(e.status, {"code": e.code, "message": e.message})
    finally:
        c.close()


def perm(permission: str):
    def _dep(p: dict = Depends(principal)) -> dict:
        try:
            auth.require(p, permission)
        except auth.AuthError as e:
            raise HTTPException(e.status, {"code": e.code, "message": e.message})
        return p
    return _dep


# ---------------------------------------------------------------- middleware
def _route_label(request: Request) -> str:
    """Metric label for a request.

    Always the route TEMPLATE ("/api/projects/{project_id}/wbs"), never the
    concrete path: one series per route, not one per project id. Unmatched
    paths - including anything an unauthenticated caller invents - collapse
    into a single OTHER bucket, so label cardinality can never be driven from
    outside.
    """
    return getattr(request.scope.get("route"), "path", None) or "OTHER"


def _finalise(response, cid: str):
    """Attach the correlation id and the security headers to EVERY response.

    Previously the 401 early-return bypassed this block entirely, so an
    unauthenticated caller received no CSP, no nosniff and no frame-options.
    Every exit path now goes through here.
    """
    response.headers["X-Correlation-Id"] = cid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    return response


@app.middleware("http")
async def guard(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    # AUD-M-007: bind the correlation id for the whole request so it reaches the
    # service layer and audit_log rather than only the response header. The
    # context manager resets on exit; a bare set() would never unwind.
    with observability.correlation(cid):
        if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_PATHS:
            has_token = bool(request.headers.get("x-session") or
                             request.headers.get("authorization"))
            if not has_token:
                # Deliberately NOT recorded as a metric. This branch runs before
                # authentication and before route matching, so the only label
                # available is the raw path - which an unauthenticated caller
                # controls. Labelling it would be an unbounded-cardinality
                # memory vector on a public endpoint.
                return _finalise(JSONResponse(
                    status_code=401,
                    content={"detail": {"code": "NOT_AUTHENTICATED",
                                        "message": "Sign in to continue."}}), cid)
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception is the single most important thing to
            # observe, and it was the one path with no duration, no error
            # metric and no structured detail. Business refusals map to
            # controlled 4xx via the exception handlers below, so anything
            # reaching here is a genuine 500.
            elapsed_ms = (time.perf_counter() - started) * 1000
            label = _route_label(request)
            observability.metrics.observe(request.method, label, 500, elapsed_ms)
            # exc_info=True routes the traceback through the formatter, which
            # scrubs it. An unredacted traceback is the highest-probability
            # credential leak in this system.
            observability.log.error(
                "unhandled exception", exc_info=True,
                extra={"method": request.method, "route": label,
                       "status": 500, "duration_ms": round(elapsed_ms, 2)})
            # A controlled 500 rather than a re-raise. Re-raising skipped the
            # header block, so the response carried no X-Correlation-Id and the
            # failure could not be traced back to the request that caused it -
            # a user reporting "it broke" had nothing to quote. This also
            # guarantees no internal detail reaches the client: the traceback
            # is logged server-side, redacted; the caller gets an id.
            return _finalise(JSONResponse(
                status_code=500,
                content={"detail": {
                    "code": "INTERNAL_ERROR",
                    "message": "The request could not be completed. Quote the "
                               "correlation id when reporting this.",
                    "correlation_id": cid,
                }}), cid)
        elapsed_ms = (time.perf_counter() - started) * 1000
        label = _route_label(request)
        observability.metrics.observe(
            request.method, label, response.status_code, elapsed_ms)
        observability.log.info(
            "request",
            extra={"method": request.method, "route": label,
                   "status": response.status_code, "duration_ms": round(elapsed_ms, 2)})
        return _finalise(response, cid)


@app.exception_handler(services.BusinessError)
async def business_error(request: Request, exc: services.BusinessError):
    body = {"code": exc.code, "message": exc.message, **exc.extra}
    return JSONResponse(status_code=exc.status, content={"detail": body})


@app.exception_handler(auth.AuthError)
async def auth_error(request: Request, exc: auth.AuthError):
    return JSONResponse(status_code=exc.status,
                        content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(pg_repo.ScopeNotExpressible)
async def scope_not_expressible(request: Request,
                                 exc: "pg_repo.ScopeNotExpressible"):
    """A restriction the query cannot express is a REFUSAL, not a crash.

    `compile_scope` deliberately raises rather than widening when a caller is
    restricted on a dimension the query has no column for -- refusing is the
    fail-closed answer and it is the right one. But `ScopeNotExpressible` is a
    RuntimeError, so with no handler it surfaced as a 500 with a traceback:
    `periods._PERIOD_SCOPE_COLUMNS` mapped only `entity`, and once Wave 3
    wired real grants into the routers, every plant-, project- or
    location-restricted caller got that 500 on a plain read of the period
    list. Three of the nine seeded demo users.

    That specific mapping is fixed. This handler exists so the NEXT one is a
    clean 403 that names the dimension, rather than a server fault -- the
    refusal was always correct, only its exit path was missing.
    """
    log.warning("scope not expressible on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=403,
        content={"detail": {
            "code": "SCOPE_NOT_EXPRESSIBLE",
            "message": ("Your access is restricted on a dimension this view "
                         "cannot filter by, so it cannot be shown to you "
                         "safely."),
            "detail": str(exc),
        }})


@app.exception_handler(pg_principal_scope.ScopeResolutionUnavailable)
async def scope_resolution_unavailable(
        request: Request, exc: "pg_principal_scope.ScopeResolutionUnavailable"):
    """503, not an empty 200.

    A denial and a resolution failure both end with the caller seeing no
    rows, and both used to render identically as `200 {"items": []}`. On a
    financial control surface those are very different statements: "you have
    no budget" versus "we could not determine what you are allowed to see".
    A database whose identity tables were missing would have shown every user
    a healthy, empty budget grid, with `/readyz` still green because it only
    reads `schema_migrations`.

    Still fail-closed -- no scope was produced, so no row is returned.
    """
    log.error("scope resolution unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": {
            "code": "SCOPE_RESOLUTION_UNAVAILABLE",
            "message": ("Your access could not be determined right now, so "
                         "nothing is shown rather than showing you an empty "
                         "result that looks like an answer."),
        }})


@app.exception_handler(MoneyError)
async def money_error(request: Request, exc: MoneyError):
    return JSONResponse(status_code=422,
                        content={"detail": {"code": "INVALID_AMOUNT", "message": str(exc)}})


# ---------------------------------------------------------------- auth routes
class LoginIn(BaseModel):
    user_id: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn):
    c = db.connect()
    try:
        return auth.login(c, body.user_id, body.password)
    finally:
        c.close()


@app.post("/api/auth/logout")
def logout(x_session: str = Header(default=""), p: dict = Depends(principal)):
    c = db.connect()
    try:
        auth.logout(c, x_session)
        return {"ok": True}
    finally:
        c.close()


@app.get("/api/auth/me")
def me(p: dict = Depends(principal)):
    return p


# ---------------------------------------------------------------- read routes
@app.get("/api/bootstrap")
def bootstrap(p: dict = Depends(principal)):
    c = con()
    try:
        return {
            "me": p,
            "entities": rows(c, "SELECT * FROM entity"),
            "plants": rows(c, "SELECT * FROM plant"),
            "budget_heads": rows(c, "SELECT * FROM budget_head WHERE active=1"),
            "users": rows(c, "SELECT user_id, name, role FROM app_user"),
            "projects": rows(c, """SELECT p.*, e.name entity_name, pl.name plant_name
                                   FROM project p JOIN entity e ON e.entity_id=p.entity_id
                                   JOIN plant pl ON pl.plant_id=p.plant_id ORDER BY p.capex_code"""),
            "permissions": sorted({k for k, v in auth.PERMISSIONS.items()
                                   if set(p["roles"]) & set(v)}),
        }
    finally:
        c.close()


@app.get("/api/dashboard")
def dashboard(entity_id: str = None, plant_id: str = None, p: dict = Depends(principal)):
    c = con()
    try:
        prj = rows(c, """SELECT p.*, e.name entity_name, pl.name plant_name
                         FROM project p JOIN entity e ON e.entity_id=p.entity_id
                         JOIN plant pl ON pl.plant_id=p.plant_id ORDER BY p.capex_code""")
        cards, tot = [], {k: 0 for k in domain._COMPONENTS}
        for pr in prj:
            if entity_id and pr["entity_id"] != entity_id:
                continue
            if plant_id and pr["plant_id"] != plant_id:
                continue
            t = domain.compute_ledger(c, pr["project_id"])["totals"]
            cards.append({"project_id": pr["project_id"], "capex_code": pr["capex_code"],
                          "name": pr["name"], "entity": pr["entity_name"],
                          "plant": pr["plant_name"], "status": pr["status"], **t})
            for k in tot:
                tot[k] += t[k]
        tot = domain._derive(tot)
        recon = domain.reconciliation(c)
        return {"projects": cards, "totals": tot, "alerts": {
            "budget_exceptions": rows(c, """SELECT pr_id, pr_number, project_id, wbs_id,
                                            amount_paise, status, exception_reason
                                            FROM purchase_request WHERE status='Exception Pending'"""),
            "pending_revisions": rows(c, "SELECT * FROM budget_revision WHERE status='Submitted'"),
            "awaiting_capitalisation": rows(c, """SELECT c.*, p.capex_code, p.name
                                                  FROM capitalisation_request c
                                                  JOIN project p ON p.project_id=c.project_id
                                                  WHERE c.status='Submitted'"""),
            "over_billed_lines": [r for r in recon if r["flag"] == "over-billed"],
            "received_not_billed_lines": [r for r in recon if r["flag"] == "received-unbilled"],
            "reconciliation_exceptions": rows(c, "SELECT * FROM reconciliation_exception WHERE status='Open'"),
        }}
    finally:
        c.close()


@app.get("/api/projects/{project_id}/wbs")
def wbs_tree(project_id: str, p: dict = Depends(principal)):
    c = con()
    try:
        led = domain.compute_ledger(c, project_id)
        if not led["by_id"]:
            raise HTTPException(404, {"code": "PROJECT_NOT_FOUND",
                                      "message": f"{project_id} has no WBS structure."})
        heads = {h["budget_head_id"]: h["name"] for h in rows(c, "SELECT * FROM budget_head")}

        # AUD-M-005: build the response iteratively and bound the depth. A recursive
        # serialiser reintroduced the RecursionError that the ledger rollup was fixed to
        # avoid, and a deeply nested payload also exhausts the JSON encoder.
        MAX_DEPTH = 200
        stack, flat = [(n, 1) for n in led["wbs"]], []
        while stack:
            n, d = stack.pop()
            if d > MAX_DEPTH:
                raise HTTPException(422, {
                    "code": "HIERARCHY_TOO_DEEP",
                    "message": (f"This project's WBS is deeper than the supported limit of "
                                f"{MAX_DEPTH} levels. Correct the hierarchy before viewing it.")})
            flat.append(n)
            for ch in n["children"]:
                stack.append((ch, d + 1))

        def shallow(n):
            return {
                "wbs_id": n["wbs_id"], "wbs_code": n["wbs_code"], "description": n["description"],
                "level": n["level"], "status": n["status"], "progress_pct": n["progress_pct"],
                "budget_head": heads.get(n["budget_head_id"]),
                "budget_head_id": n["budget_head_id"],
                "responsible_user": n["responsible_user"],
                "planned_start": n["planned_start"], "planned_end": n["planned_end"],
                "asset_category": n["asset_category"], "settlement_receiver": n["settlement_receiver"],
                "allow_procurement": bool(n["allow_procurement"]),
                "allow_posting": bool(n["allow_posting"]),
                "is_abandoned": bool(n["is_abandoned"]),
                "carries_budget": n.get("carries_budget", False),
                "budget_owner_id": n.get("budget_owner_id"),
                "budget_owner_code": (led["by_id"][n["budget_owner_id"]]["wbs_code"]
                                      if n.get("budget_owner_id") else None),
                "heads": {heads.get(h, h): v for h, v in n.get("head_totals", {}).items()},
                "own": n["own"], "total": n.get("total", n["own"]),
                "children": [],
            }

        built = {n["wbs_id"]: shallow(n) for n in flat}
        for n in flat:                        # link children without recursing
            built[n["wbs_id"]]["children"] = [built[ch["wbs_id"]] for ch in n["children"]]
        return {"project_id": project_id, "totals": led["totals"],
                "tree": [built[n["wbs_id"]] for n in led["wbs"]]}
    finally:
        c.close()


@app.get("/api/projects/{project_id}/budget-grid")
def budget_grid(project_id: str, p: dict = Depends(principal)):
    c = con()
    try:
        led = domain.compute_ledger(c, project_id)
        heads = {h["budget_head_id"]: h["name"] for h in rows(c, "SELECT * FROM budget_head")}
        out = []
        for wid, n in led["by_id"].items():
            for head_id, cell in n["heads"].items():
                if cell["budget"] == 0 and cell["exposure"] == 0:
                    continue
                out.append({"wbs_id": wid, "wbs_code": n["wbs_code"],
                            "description": n["description"], "level": n["level"],
                            "budget_head": heads.get(head_id, head_id),
                            "budget_head_id": head_id, **cell})
        out.sort(key=lambda r: (r["wbs_code"], r["budget_head"] or ""))
        return {"rows": out, "totals": led["totals"]}
    finally:
        c.close()


class CheckIn(BaseModel):
    wbs_id: str
    amount_rupees: str | float | int
    budget_head_id: str | None = None


@app.post("/api/budget-check")
def check(body: CheckIn, p: dict = Depends(perm("budget.check"))):
    from .money import to_paise
    c = con()
    try:
        return domain.budget_check(c, body.wbs_id,
                                   to_paise(body.amount_rupees, field="amount_rupees",
                                            allow_negative=True),
                                   body.budget_head_id)
    finally:
        c.close()


@app.get("/api/purchase-requests")
def prs(status: str = None, p: dict = Depends(principal)):
    c = con()
    try:
        sql = """SELECT pr.*, w.wbs_code, pj.capex_code, bh.name budget_head_name
                 FROM purchase_request pr
                 JOIN wbs_element w ON w.wbs_id=pr.wbs_id
                 JOIN project pj ON pj.project_id=pr.project_id
                 JOIN budget_head bh ON bh.budget_head_id=pr.budget_head_id"""
        args = ()
        if status:
            sql += " WHERE pr.status=?"; args = (status,)
        return rows(c, sql + " ORDER BY pr.pr_number", args)
    finally:
        c.close()


class PRIn(BaseModel):
    project_id: str
    wbs_id: str
    budget_head_id: str
    description: str
    amount_rupees: str | float | int
    reserve_budget: bool = False


@app.post("/api/purchase-requests", status_code=201)
def create_pr(body: PRIn, p: dict = Depends(perm("pr.create")),
              idempotency_key: str = Header(default="")):
    c = con()
    try:
        cached = services.idempotent(c, idempotency_key or None, "create_pr", p["user_id"],
                                     body.model_dump())
        if cached:
            return cached
        out = services.create_pr(c, p, project_id=body.project_id, wbs_id=body.wbs_id,
                                 budget_head_id=body.budget_head_id,
                                 description=body.description, amount=body.amount_rupees,
                                 reserve=body.reserve_budget)
        services.remember(c, idempotency_key or None, "create_pr", p["user_id"],
                          body.model_dump(), 201, out)
        c.commit()
        return out
    finally:
        c.close()


class ApproveIn(BaseModel):
    reason: str | None = None


@app.post("/api/purchase-requests/{pr_id}/approve")
def approve_pr(pr_id: str, body: ApproveIn = Body(default=ApproveIn()),
               p: dict = Depends(principal)):
    c = con()
    try:
        return services.approve_pr(c, p, pr_id, reason=body.reason)
    finally:
        c.close()


@app.get("/api/purchase-orders")
def pos(p: dict = Depends(principal)):
    c = con()
    try:
        po = rows(c, """SELECT po.*, pj.capex_code FROM purchase_order po
                        JOIN project pj ON pj.project_id=po.project_id ORDER BY po.po_number""")
        lines = rows(c, """SELECT pl.*, w.wbs_code FROM po_line pl
                           JOIN wbs_element w ON w.wbs_id=pl.wbs_id ORDER BY pl.line_no""")
        by_po = {}
        for l in lines:
            by_po.setdefault(l["po_id"], []).append(l)
        recon = {r["po_line_id"]: r for r in domain.reconciliation(c)}
        for x in po:
            x["lines"] = [{**l, **{k: recon[l["po_line_id"]][k] for k in
                                   ("received_paise", "billed_paise", "open_commitment_paise",
                                    "received_not_billed_paise", "exposure_paise", "position", "flag")}}
                          for l in by_po.get(x["po_id"], []) if l["po_line_id"] in recon]
            x["ordered_paise"] = sum(l["amount_paise"] + l["non_creditable_tax_paise"]
                                     + l["freight_paise"] for l in x["lines"])
            x["open_commitment_paise"] = sum(l["open_commitment_paise"] for l in x["lines"])
            x["billed_paise"] = sum(l["billed_paise"] for l in x["lines"])
        return po
    finally:
        c.close()


class AmendIn(BaseModel):
    po_line_id: str
    new_amount_rupees: str | float | int
    exception_ref: str | None = None
    expected_version: int | None = None


@app.post("/api/purchase-orders/{po_id}/amend")
def amend_po(po_id: str, body: AmendIn, p: dict = Depends(perm("po.amend"))):
    c = con()
    try:
        return services.amend_po(c, p, po_id, po_line_id=body.po_line_id,
                                 new_amount=body.new_amount_rupees,
                                 exception_ref=body.exception_ref,
                                 expected_version=body.expected_version)
    finally:
        c.close()


class ReasonIn(BaseModel):
    reason: str | None = None


@app.post("/api/purchase-orders/{po_id}/cancel")
def cancel_po(po_id: str, body: ReasonIn = Body(default=ReasonIn()),
              p: dict = Depends(perm("po.cancel"))):
    c = con()
    try:
        return services.cancel_po(c, p, po_id, reason=body.reason)
    finally:
        c.close()


@app.post("/api/purchase-orders/{po_id}/close")
def close_po(po_id: str, body: ReasonIn = Body(default=ReasonIn()),
             p: dict = Depends(perm("po.close"))):
    c = con()
    try:
        return services.close_po(c, p, po_id, reason=body.reason)
    finally:
        c.close()


class ConvertIn(BaseModel):
    pr_id: str
    po_id: str


@app.post("/api/purchase-requests/convert")
def convert_pr(body: ConvertIn, p: dict = Depends(perm("po.amend"))):
    c = con()
    try:
        return services.convert_pr_to_po(c, p, body.pr_id, body.po_id)
    finally:
        c.close()


@app.get("/api/grns")
def grns(p: dict = Depends(principal)):
    c = con()
    try:
        g = rows(c, """SELECT g.*, po.po_number FROM grn g
                       JOIN purchase_order po ON po.po_id=g.po_id ORDER BY g.grn_number""")
        gl = rows(c, """SELECT gl.*, pl.line_no, w.wbs_code FROM grn_line gl
                        JOIN po_line pl ON pl.po_line_id=gl.po_line_id
                        JOIN wbs_element w ON w.wbs_id=pl.wbs_id""")
        by = {}
        for l in gl:
            by.setdefault(l["grn_id"], []).append(l)
        for r in g:
            r["lines"] = by.get(r["grn_id"], [])
            r["amount_paise"] = sum(l["amount_paise"] for l in r["lines"])
        return g
    finally:
        c.close()


@app.get("/api/bills")
def bills(p: dict = Depends(principal)):
    c = con()
    try:
        b = rows(c, """SELECT b.*, po.po_number FROM bill b
                       LEFT JOIN purchase_order po ON po.po_id=b.po_id ORDER BY b.bill_number""")
        bl = rows(c, """SELECT bl.*, w.wbs_code, bh.name budget_head_name FROM bill_line bl
                        JOIN wbs_element w ON w.wbs_id=bl.wbs_id
                        JOIN budget_head bh ON bh.budget_head_id=bl.budget_head_id""")
        by = {}
        for l in bl:
            by.setdefault(l["bill_id"], []).append(l)
        for r in b:
            r["lines"] = by.get(r["bill_id"], [])
            r["amount_paise"] = sum(l["amount_paise"] + l["non_creditable_tax_paise"]
                                    + l["freight_paise"] for l in r["lines"])
            r["accounting_effective"] = r["accounting_status"] in domain.ACCOUNTING_EFFECTIVE_BILL_STATES
        return b
    finally:
        c.close()


@app.post("/api/bills/{bill_id}/void")
def void_bill(bill_id: str, body: ReasonIn, p: dict = Depends(perm("bill.void"))):
    c = con()
    try:
        return services.void_bill(c, p, bill_id, reason=body.reason)
    finally:
        c.close()


@app.get("/api/reconciliation")
def recon(project_id: str = None, p: dict = Depends(principal)):
    c = con()
    try:
        r = domain.reconciliation(c, project_id)
        return {"rows": r, "summary": {
            "lines": len(r),
            "open_commitment_paise": sum(x["open_commitment_paise"] for x in r),
            "billed_paise": sum(x["billed_paise"] for x in r),
            "received_not_billed_paise": sum(x["received_not_billed_paise"] for x in r),
            "exceptions": [x for x in r if x["flag"] in ("over-billed", "received-unbilled")],
        }}
    finally:
        c.close()


@app.get("/api/budget-revisions")
def revisions(p: dict = Depends(principal)):
    c = con()
    try:
        rev = rows(c, """SELECT r.*, pj.capex_code FROM budget_revision r
                         JOIN project pj ON pj.project_id=r.project_id ORDER BY r.revision_no""")
        for r in rev:
            r["lines"] = rows(c, """SELECT bl.*, w.wbs_code FROM budget_line bl
                                    LEFT JOIN wbs_element w ON w.wbs_id=bl.wbs_id
                                    WHERE bl.revision_id=?""", (r["revision_id"],))
            r["affects_budget"] = r["status"] == "Approved"
        return rev
    finally:
        c.close()


class RevisionIn(BaseModel):
    project_id: str
    wbs_id: str
    budget_head_id: str
    kind: str = "SUPPLEMENT"
    amount_rupees: str | float | int
    reason: str
    effective_date: str | None = None


@app.post("/api/budget-revisions", status_code=201)
def create_revision(body: RevisionIn, p: dict = Depends(perm("revision.create"))):
    c = con()
    try:
        return services.create_revision(c, p, project_id=body.project_id, wbs_id=body.wbs_id,
                                        budget_head_id=body.budget_head_id, kind=body.kind,
                                        amount=body.amount_rupees, reason=body.reason,
                                        effective_date=body.effective_date)
    finally:
        c.close()


class RevApproveIn(BaseModel):
    effective_date: str | None = None


@app.post("/api/budget-revisions/{rev_id}/approve")
def approve_revision(rev_id: str, body: RevApproveIn = Body(default=RevApproveIn()),
                     p: dict = Depends(perm("revision.approve"))):
    c = con()
    try:
        return services.approve_revision(c, p, rev_id, effective_date=body.effective_date)
    finally:
        c.close()


@app.get("/api/capitalisation")
def capitalisation(p: dict = Depends(principal)):
    c = con()
    try:
        caps = rows(c, """SELECT c.*, pj.capex_code, pj.name project_name
                          FROM capitalisation_request c
                          JOIN project pj ON pj.project_id=c.project_id""")
        for cap in caps:
            cap["allocations"] = rows(c, "SELECT * FROM asset_allocation WHERE cap_id=?",
                                      (cap["cap_id"],))
            t = domain.compute_ledger(c, cap["project_id"])["totals"]
            cap["cwip_balance_paise"] = t["actual"]
            cap["open_commitment_paise"] = t["commitment"]
            cap["received_not_billed_paise"] = t["received_not_billed"]
            cap["ready"] = t["commitment"] == 0 and t["received_not_billed"] == 0
            cap["posting_status"] = "NOT POSTED - no ERP/GL posting exists in this build"
        return caps
    finally:
        c.close()


class AllocIn(BaseModel):
    wbs_id: str | None = None
    asset_name: str
    asset_category: str | None = None
    amount_rupees: str | float | int
    is_writeoff: bool = False
    writeoff_reason: str | None = None


@app.post("/api/capitalisation/{cap_id}/allocate", status_code=201)
def allocate(cap_id: str, body: AllocIn, p: dict = Depends(perm("capitalisation.allocate"))):
    c = con()
    try:
        return services.allocate(c, p, cap_id, wbs_id=body.wbs_id, asset_name=body.asset_name,
                                 asset_category=body.asset_category, amount=body.amount_rupees,
                                 is_writeoff=body.is_writeoff,
                                 writeoff_reason=body.writeoff_reason)
    finally:
        c.close()


class CapApproveIn(BaseModel):
    override_ref: str | None = None


@app.post("/api/capitalisation/{cap_id}/approve")
def approve_cap(cap_id: str, body: CapApproveIn = Body(default=CapApproveIn()),
                p: dict = Depends(perm("capitalisation.approve"))):
    c = con()
    try:
        return services.approve_capitalisation(c, p, cap_id, override_ref=body.override_ref)
    finally:
        c.close()


@app.get("/api/audit")
def audit_trail(limit: int = 300, p: dict = Depends(perm("audit.read"))):
    c = con()
    try:
        return rows(c, "SELECT * FROM audit_log ORDER BY audit_id DESC LIMIT ?", (limit,))
    finally:
        c.close()


@app.get("/api/audit/verify")
def audit_verify(p: dict = Depends(perm("audit.read"))):
    c = con()
    try:
        return services.verify_audit_chain(c)
    finally:
        c.close()


@app.get("/api/reconciliation/exceptions")
def recon_exceptions(p: dict = Depends(principal)):
    c = con()
    try:
        return rows(c, "SELECT * FROM reconciliation_exception ORDER BY raised_at DESC")
    finally:
        c.close()


# ---------------------------------------------------------------- Zoho
@app.get("/api/zoho/connections")
def zoho_connections(p: dict = Depends(perm("connector.read"))):
    c = con()
    try:
        # COLUMNS NAMED EXPLICITLY, and the `SELECT *` this replaced is the
        # reason. `zoho_connection` is the one table in the schema whose column
        # names are secret-SHAPED -- `client_id_ref`, `client_secret_ref`,
        # `refresh_token_ref` -- and `SELECT *` publishes whatever the table
        # grows next with no code change and no review. The three `_ref`
        # columns are POINTERS (`secretref://...`), never values, which
        # `tests/test_connector.py::test_aud_h_003_authorisation_stores_secret_
        # references_not_secrets` pins; a column called `client_secret` added
        # tomorrow would have travelled the same path silently.
        #
        # Adding a column here is now a deliberate act. That is the whole
        # control: a list somebody has to edit, rather than a wildcard nobody
        # re-reads.
        connections = rows(c, """
            SELECT connection_id, name, entity_id, environment, data_centre,
                   accounts_domain, api_domain, client_id_ref, client_secret_ref,
                   refresh_token_ref, redirect_uri, oauth_status,
                   access_token_expiry, token_last_refreshed, zoho_org_id,
                   zoho_org_name, granted_scopes, status, last_success_at,
                   last_failure_at, created_at
            FROM zoho_connection""")
        return {"mode": zoho.MODE, "connections": connections,
                "data_centres": zoho.DATA_CENTRES}
    finally:
        c.close()


@app.get("/api/zoho/inventory")
def zoho_inventory(p: dict = Depends(perm("connector.read"))):
    return zoho.inventory_summary()


@app.get("/api/zoho/inventory/{module}")
def zoho_module(module: str, p: dict = Depends(perm("connector.read"))):
    eps = zoho.module_endpoints(module)
    if not eps:
        raise HTTPException(404, {"code": "MODULE_UNKNOWN",
                                  "message": f"No endpoint for '{module}' in the official specification."})
    return eps


@app.get("/api/zoho/scopes")
def zoho_scopes(p: dict = Depends(perm("connector.read"))):
    return {"required": zoho.required_scopes(), "hard_negatives": zoho.hard_negatives()}


class AuthIn(BaseModel):
    withhold: list[str] = Field(default_factory=list)


@app.post("/api/zoho/{connection_id}/authorise")
def zoho_auth(connection_id: str, body: AuthIn = Body(default=AuthIn()),
              p: dict = Depends(perm("connector.manage"))):
    c = con()
    try:
        return zoho.authorise(c, connection_id, withhold=body.withhold, actor=p["user_id"])
    finally:
        c.close()


@app.post("/api/zoho/{connection_id}/refresh")
def zoho_refresh(connection_id: str, p: dict = Depends(perm("connector.manage"))):
    c = con()
    try:
        return zoho.refresh_token(c, connection_id)
    finally:
        c.close()


@app.post("/api/zoho/{connection_id}/test")
def zoho_test(connection_id: str, p: dict = Depends(perm("connector.manage"))):
    c = con()
    try:
        return zoho.run_connectivity_tests(c, connection_id)
    finally:
        c.close()


@app.post("/api/zoho/{connection_id}/sync/{module}")
def zoho_sync(connection_id: str, module: str, p: dict = Depends(perm("connector.manage")),
              idempotency_key: str = Header(default="")):
    c = con()
    try:
        return zoho.sync(c, connection_id, module, idem_key=idempotency_key or None,
                         actor=p["user_id"])
    finally:
        c.close()


@app.get("/api/zoho/{connection_id}/health")
def zoho_health(connection_id: str, p: dict = Depends(perm("connector.read"))):
    c = con()
    try:
        return zoho.health(c, connection_id)
    finally:
        c.close()


# ---------------------------------------------------------------- admin
@app.post("/api/admin/reset")
def reset(p: dict = Depends(perm("admin.reset"))):
    """AUD-C-010: destructive reset is unavailable outside an explicit local profile."""
    if not auth.is_demo_profile():
        raise HTTPException(403, {
            "code": "RESET_DISABLED",
            "message": "Database reset is only available under CAPEX_PROFILE=local-demo. "
                       "It is disabled in every deployed environment because it destroys "
                       "audit evidence."})
    # Rebuild through the migration runner, not db.reset_and_seed(), which only
    # creates the v1 schema. A reset must leave the database at the CURRENT version,
    # otherwise the control tables added by 002 disappear and the app is left in a
    # state where identity provisioning fails.
    from . import migrate
    migrate.fresh(db.DB_PATH, seed=True)
    c = db.connect()
    try:
        auth.provision_dev_identities(c)
        version = c.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0]
    finally:
        c.close()
    return {"ok": True, "db": db.DB_PATH, "schema_version": version}


@app.get("/api/health")
def health():
    return {"status": "ok", "app": "CAPEX & WBS Control Hub", "version": app.version,
            "zoho_mode": zoho.MODE,
            "zoho_mode_note": "MOCK - no live Zoho tenant is connected; integration is NOT VERIFIED",
            "profile": os.environ.get("CAPEX_PROFILE", "unset")}


# ---------------------------------------------------------------- frontend
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    # Fable 5.1 -- the hosted UAT preview serves a TRANSFORMED index.html.
    #
    # index.html on disk is the client-approved sign-in screen and carries the
    # seeded-credential hint ("password is the user id followed by !demo").
    # Under CAPEX_PROFILE=uat-preview those passwords are not installed (see
    # app/run.py), so the hint would be both false and a brute-force map. The
    # file is not edited -- the VRT baselines and the local-demo profile keep
    # the approved bytes -- it is rewritten at serve time, once, with both
    # anchors asserted so a future edit to index.html cannot silently drop the
    # substitution. Everything else on the page, and every other static file,
    # is served unchanged.
    UAT_BANNER_TEXT = "UAT — SYNTHETIC DATA — ERP MOCK"
    UAT_LOGIN_HINT = (
        "UAT preview. The data is a synthetic seed and resets when the service "
        "restarts. Credentials are issued by the UAT coordinator; passwords are "
        "not derivable from user ids. The ERP connector is MOCK and outbound "
        "writes are disabled.")
    _HINT_RE = None

    def _uat_index_html() -> bytes:
        import re
        global _HINT_RE
        if _HINT_RE is None:
            _HINT_RE = re.compile(
                r'<p class="muted small" id="loginHint">.*?</p>', re.S)
        with open(os.path.join(FRONTEND, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        rewritten, n = _HINT_RE.subn(
            f'<p class="muted small" id="loginHint">{UAT_LOGIN_HINT}</p>', html)
        if n != 1:
            raise RuntimeError("index.html: the sign-in hint anchor was not found exactly once")
        body_at = rewritten.find("<body")
        body_end = rewritten.find(">", body_at)
        if body_at < 0 or body_end < 0:
            raise RuntimeError("index.html: no <body> tag to anchor the UAT banner on")
        banner = (f'\n<div class="uat-banner" role="status" aria-label="Environment notice">'
                  f'{UAT_BANNER_TEXT}</div>')
        rewritten = rewritten[:body_end + 1] + banner + rewritten[body_end + 1:]
        if "!demo" in rewritten:
            raise RuntimeError("index.html still carries the demo-password hint after rewriting")
        return rewritten.encode("utf-8")

    _uat_index_cache: dict[str, bytes] = {}

    @app.get("/")
    def index():
        if auth.is_uat_profile():
            if "html" not in _uat_index_cache:
                _uat_index_cache["html"] = _uat_index_html()
            return Response(content=_uat_index_cache["html"], media_type="text/html")
        return FileResponse(os.path.join(FRONTEND, "index.html"))
