"""FastAPI router for Wave 7's closure surface: `/api/closure/*`, `/api/control/*`.

    GET  /api/closure/projects/{project_id}/position
    GET  /api/closure/reviews
    POST /api/closure/reviews
    POST /api/closure/reviews/{review_id}/submit
    POST /api/closure/reviews/{review_id}/decide
    GET  /api/closure/requests
    GET  /api/closure/requests/{cap_id}
    POST /api/closure/requests
    POST /api/closure/requests/{cap_id}/submit
    POST /api/closure/requests/{cap_id}/approve
    POST /api/closure/requests/{cap_id}/reject
    GET  /api/closure/requests/{cap_id}/allocations
    POST /api/closure/requests/{cap_id}/allocations
    GET  /api/control/budget-revisions
    GET  /api/control/budget-transfers
    GET  /api/control/purchase-requests

WHY THREE `/api/control/` READS LIVE IN A ROUTER CALLED `closure`
-----------------------------------------------------------------
SCR-11 (Budget Revision Request), SCR-12 (Budget Transfer) and SCR-14
(Purchase Request Control View) need to LIST what already exists. The routers
that own the write side -- `api/budget.py` and `api/procurement.py` -- offer
POST create / submit / approve / reject and **no list route at all**, and
neither file belongs to this stream. Rather than edit another stream's router
mid-wave, the three read-only control views are served here, under a path
family that says what they are.

They are reads. They mutate nothing, they hold the same `budget.read` floor as
the rest of this router, and they go through `repo.query` with a mapping that
names all four dimensions -- the same discipline as everything else here. If
`api/budget.py` later grows its own list routes, these become redundant and
should be withdrawn; that is a merge, not a migration.

**NOTHING ON THIS ROUTER POSTS TO A GENERAL LEDGER, AND EVERY RESPONSE THAT
COULD BE MISREAD SAYS SO.**

Exclusion X-01. `posting_status` comes off the `capitalisation_request` row,
where `ck_capitalisation_request_not_posted` (migration 018) pins it to
`'NOT POSTED'`. This module does not synthesise the string and does not
default it: a row that somehow carried something else would be reported
faithfully, and the constraint is what makes that impossible.

**Permissions.** `budget.read` is the router-level floor -- every role holds
it, and it is the same floor `api/budget.py` uses for the position screens
this one continues. Two route permissions sit on top, both from
`auth.PERMISSIONS` and from nowhere else:

  * `capitalisation.allocate` -- raising and submitting a completion review,
    raising and submitting a capitalisation request, recording an allocation.
    Held by BudgetController and FinanceApprover.
  * `capitalisation.approve`  -- deciding a completion review, approving or
    rejecting a capitalisation. Held by CapitalisationApprover, and it is in
    `auth.MAKER_CHECKER`.

There is deliberately NO local permission table and no fallback. A previous
wave's fallback FAILED OPEN, granting a role the authoritative table excluded,
and it did so silently because the fallback was only consulted when the lookup
"failed". If `auth.PERMISSIONS` does not carry a permission,
`auth.require` raises `UNKNOWN_PERMISSION` and this router refuses.

**Maker-checker runs in the order the rest of this codebase runs it**:
`auth.require(actor, permission)` and then
`auth.require_separation(actor, permission, maker_user_id)`. It is enforced
TWICE on purpose -- here against the row this router read, and again inside
`pg/closure.py` against the row it holds a `FOR UPDATE` lock on. The router's
check gives the caller a clean 403 before any work is done; the service's is
the one that cannot be raced.

**Scope.** Every read and write goes through `repo.query()` with a literal
`{scope}` token; a query missing it is refused before it reaches the database.
An out-of-scope row is returned as NO ROWS and reported as 404 with the same
code as a row that never existed. Never 403: a 403 on an id tells the caller
the id is real, which is an existence oracle over another entity's estate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import observability
from ..pg import closure as closure_svc
from ..pg import principal_scope, repo
from ..pg.engine import Database, Scope, get_database

ROOT = Path(__file__).resolve().parents[3]

# ============================================================ message catalogue
#: C10 is FROZEN. A `message_id` on any response this router emits must exist
#: in it, or be null -- never an id invented to look helpful. A made-up id is
#: worse than none: the frontend looks it up, finds nothing, and renders an
#: empty string where the explanation should be.
_C10_PATH = ROOT / "research" / "30_contracts" / "C10_messages.json"


def _load_message_ids() -> frozenset[str]:
    try:
        doc = json.loads(_C10_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable catalogue means NO id can be validated, so none is
        # emitted. Failing closed costs a hint; failing open puts unvalidated
        # ids on the wire, which is the defect this guards.
        return frozenset()
    return frozenset(str(m["id"]) for m in doc.get("messages", []) if m.get("id"))


MESSAGE_IDS: frozenset[str] = _load_message_ids()


def message_id_or_none(candidate: str | None) -> str | None:
    """`candidate` if C10 declares it, else None. Never raises."""
    if candidate and candidate in MESSAGE_IDS:
        return candidate
    return None


# ================================================================ access control
class _ClosureAccess:
    """Router-level dependency: authenticate, and require `budget.read`, on
    EVERY closure route.

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
        request.state.closure_principal = who
        return who


require_closure_access = _ClosureAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's floor."""

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "closure_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_closure_access)])

_CORRELATION_HEADER = "X-Correlation-Id"

#: Nothing this router runs may hold a database transaction longer than this.
#: PostgreSQL enforces it, not a Python timer: a `SET LOCAL statement_timeout`
#: is checked by the server mid-statement, where an `asyncio.wait_for` around a
#: blocking driver call is not checked at all.
_STATEMENT_TIMEOUT = "30s"


# ==================================================================== plumbing
def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- the same posture as
    `api/budget.py`, `api/audit.py` and `api/integrations.py`.

    CARRIES THE `unavailable` ENVELOPE. The closure screens read
    `detail.unavailable` to tell "this build cannot answer" from "something
    broke"; without the flag every screen in a process with no PostgreSQL
    renders a red fault banner for a build that is simply not configured for
    this.
    """
    try:
        return get_database()
    except RuntimeError as exc:
        raise _unavailable(
            "DATABASE_NOT_CONFIGURED",
            "a PostgreSQL database: the closure API is mounted, but this "
            "process has none configured, so no completion review, "
            "capitalisation request or asset allocation can be read or "
            "written here.",
            remedy="Set CAPEX_DB_URL and run the migrations, including "
                   "019_closure.sql. The SQLite ledger still answers at "
                   "/api/capitalisation.",
        ) from exc


def _correlation_id(request: Request) -> str:
    """The id the CALLER is actually given -- read from the contextvar the
    middleware bound for this request, so the audit row and the response header
    carry the SAME value."""
    return (observability.get_correlation_id()
            or request.headers.get(_CORRELATION_HEADER)
            or "")


def _set_correlation_header(response: Response, request: Request) -> str:
    cid = _correlation_id(request)
    if cid:
        response.headers[_CORRELATION_HEADER] = cid
    return cid


def _problem(status_code: int, code: str, title: str, detail: str | None = None,
             *, message_id: str | None = None,
             extra: dict[str, Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
        "message_id": message_id_or_none(message_id),
    }
    if extra:
        body.update(extra)
    return HTTPException(status_code=status_code, detail=body)


def _unavailable(code: str, what_is_missing: str, *,
                 remedy: str | None = None) -> HTTPException:
    """The coded 503 an unbacked route answers with.

    503, never 404. A 404 is read by `core/api-client.js` as `notfound` and
    rendered as "no records were found", which on a route that cannot answer is
    a lie about data rather than a statement about the build.
    """
    return _problem(
        503, code, code.replace("_", " ").title(),
        f"{what_is_missing}" + (f" {remedy}" if remedy else ""),
        message_id=None,
        extra={"missing": what_is_missing, "remedy": remedy,
               "unavailable": True},
    )


def _service_error_to_http(exc: closure_svc.ClosureServiceError) -> HTTPException:
    """A `ClosureServiceError` as an RFC-7807 body, with its blockers intact.

    `blockers` travels as its own array rather than only inside the sentence.
    SCR-21 renders one line per blocker with the figure in it, and splitting a
    prose sentence on semicolons in the browser is how a screen starts
    disagreeing with the server about what is blocking.
    """
    extra: dict[str, Any] = {}
    if exc.blockers:
        extra["blockers"] = list(exc.blockers)
    return _problem(exc.status, exc.code,
                    str(exc.code).replace("_", " ").title(), exc.message,
                    message_id=exc.message_id, extra=extra or None)


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "closure_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    `scope_for_request` is Contract 4's single scope-construction path and it
    fails closed: a principal who cannot be resolved gets a scope that compiles
    to FALSE, not one that compiles to TRUE. This router constructs no scope of
    its own and never sets `read_all`.
    """
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    Never a caller-supplied header. This value reaches `audit_log.actor` and
    every `*_by` column below; a caller-supplied one would make all of them
    unattributable.
    """
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


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


def _limit(value: int, *, default: int = 100, maximum: int = 500) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ============================================================ scope mappings
# Every mapping names all four dimensions and waives none: each query joins a
# `project` row that carries entity, plant and location, so all four ARE
# expressible, and waiving one would hand a caller restricted to one entity
# every other entity's rows.
_PROJECT_COLUMNS: dict[str, str | None] = {
    "project": "p.project_id", "entity": "p.entity_id",
    "plant": "p.plant_id", "location": "p.location_id",
}


# ==================================================================== SCR-20/21
@router.get("/api/closure/projects/{project_id}/position")
def get_closure_position(
    project_id: str, response: Response, request: Request,
    cap_id: str | None = Query(default=None),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The CWIP position and the blocker list, for SCR-20 and SCR-21.

    The blockers here are computed by the SAME function the approval gate
    calls, so the list a screen shows before the button is pressed cannot
    disagree with the list that refuses when it is.
    """
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.closure_position(
                session, project_id=project_id, cap_id=cap_id)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


@router.get("/api/closure/reviews")
def list_reviews(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            items = closure_svc.list_completion_reviews(
                session, project_id=project_id, status=status,
                limit=_limit(limit))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items, "source": "wave7-closure"}


class _ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    summary: str | None = None


@router.post("/api/closure/reviews", status_code=201,
             dependencies=[Depends(_requires("capitalisation.allocate"))])
def post_review(
    body: _ReviewIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.create_completion_review(
                session, project_id=body.project_id,
                summary=body.summary or "", actor=_actor(request))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


class _ReviewSubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_date: str


@router.post("/api/closure/reviews/{review_id}/submit",
             dependencies=[Depends(_requires("capitalisation.allocate"))])
def post_review_submit(
    review_id: str, body: _ReviewSubmitIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.submit_completion_review(
                session, review_id=review_id,
                completion_date=body.completion_date, actor=_actor(request))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


class _ReviewDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    note: str


@router.post("/api/closure/reviews/{review_id}/decide",
             dependencies=[Depends(_requires("capitalisation.approve"))])
def post_review_decision(
    review_id: str, body: _ReviewDecisionIn, response: Response,
    request: Request, database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Accept or reject a submitted completion review.

    MAKER-CHECKER, in the codebase's order: `auth.require` has already run as
    the route dependency, `auth.require_separation` runs here against the
    submitter this router reads, and `pg/closure.py` re-checks it against the
    row it holds a lock on. The middle check gives a clean 403 before work is
    done; the last one is the one that cannot be raced.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            row = repo.query_one(
                session,
                """
                SELECT r.submitted_by, r.review_id
                FROM project_completion_review r
                JOIN project p ON p.project_id = r.project_id
                WHERE r.review_id = %(review_id)s AND {scope}
                """,
                {"review_id": review_id}, columns=_PROJECT_COLUMNS)
            _require_separation(actor, "capitalisation.approve",
                                row[0] if row else None,
                                object_label=review_id)
            return closure_svc.decide_completion_review(
                session, review_id=review_id, decision=body.decision,
                note=body.note, actor=actor)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


def _require_separation(actor: str, permission: str, maker: str | None, *,
                        object_label: str) -> None:
    """`auth.require_separation`, translated into this router's error shape.

    `capitalisation.approve` is in `auth.MAKER_CHECKER`, so this is a real
    check and not a no-op. It is called only AFTER `_requires(...)` has run as
    a route dependency, which is the order `auth.require` then
    `auth.require_separation` that the rest of this codebase uses.
    """
    from .. import auth as auth_mod

    try:
        auth_mod.require_separation(
            {"user_id": actor, "roles": []}, permission, maker,
            object_label=object_label)
    except auth_mod.AuthError as exc:
        raise _problem(exc.status, exc.code,
                       str(exc.code).replace("_", " ").title(), exc.message)


@router.get("/api/closure/requests")
def list_requests(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            items = closure_svc.list_capitalisation_requests(
                session, project_id=project_id, status=status,
                limit=_limit(limit))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)
    return {
        "items": items,
        # Restated on the COLLECTION as well as on every row. A list rendered
        # without opening a row must not be able to imply a posting either.
        "posting_status": "NOT POSTED",
        "posting_note": (
            "This application records the capitalisation DECISION. No general "
            "ledger or fixed-asset posting exists in this build."),
        "source": "wave7-closure",
    }


@router.get("/api/closure/requests/{cap_id}")
def get_request(
    cap_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.get_capitalisation_request(session, cap_id=cap_id)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


class _RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str


@router.post("/api/closure/requests", status_code=201,
             dependencies=[Depends(_requires("capitalisation.allocate"))])
def post_request(
    body: _RequestIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.create_capitalisation_request(
                session, project_id=body.project_id, actor=_actor(request))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


@router.post("/api/closure/requests/{cap_id}/submit",
             dependencies=[Depends(_requires("capitalisation.allocate"))])
def post_request_submit(
    cap_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.submit_capitalisation_request(
                session, cap_id=cap_id, actor=_actor(request))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


class _DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str


@router.post("/api/closure/requests/{cap_id}/approve",
             dependencies=[Depends(_requires("capitalisation.approve"))])
def post_request_approve(
    cap_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Approve the capitalisation DECISION.

    RETURNS `posting_status = "NOT POSTED"` AND THAT IS NOT A PLACEHOLDER. The
    value is read off the row, where migration 018's
    `ck_capitalisation_request_not_posted` pins it. Nothing in this build posts
    to a general ledger or a fixed-asset register, and the response says so
    twice -- once machine-readably, once in a sentence -- because this is the
    single most misreadable moment in the application.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            row = repo.query_one(
                session,
                """
                SELECT c.requested_by, c.cap_number
                FROM capitalisation_request c
                JOIN project p ON p.project_id = c.project_id
                WHERE c.cap_id = %(cap_id)s AND {scope}
                """,
                {"cap_id": cap_id}, columns=_PROJECT_COLUMNS)
            _require_separation(actor, "capitalisation.approve",
                                row[0] if row else None,
                                object_label=(row[1] if row else cap_id))
            return closure_svc.approve_capitalisation(
                session, cap_id=cap_id, note=body.note, actor=actor)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


@router.post("/api/closure/requests/{cap_id}/reject",
             dependencies=[Depends(_requires("capitalisation.approve"))])
def post_request_reject(
    cap_id: str, body: _DecisionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with _session(request, database) as session:
            row = repo.query_one(
                session,
                """
                SELECT c.requested_by, c.cap_number
                FROM capitalisation_request c
                JOIN project p ON p.project_id = c.project_id
                WHERE c.cap_id = %(cap_id)s AND {scope}
                """,
                {"cap_id": cap_id}, columns=_PROJECT_COLUMNS)
            _require_separation(actor, "capitalisation.approve",
                                row[0] if row else None,
                                object_label=(row[1] if row else cap_id))
            return closure_svc.reject_capitalisation(
                session, cap_id=cap_id, note=body.note, actor=actor)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


# ======================================================================= SCR-22
@router.get("/api/closure/requests/{cap_id}/allocations")
def list_allocations(
    cap_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            items = closure_svc.list_allocations(session, cap_id=cap_id)
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)
    return {"items": items, "source": "wave7-closure"}


class _AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wbs_id: str
    asset_name: str
    amount_paise: int
    asset_category: str | None = None
    is_writeoff: bool = False
    writeoff_reason: str | None = None


@router.post("/api/closure/requests/{cap_id}/allocations", status_code=201,
             dependencies=[Depends(_requires("capitalisation.allocate"))])
def post_allocation(
    cap_id: str, body: _AllocationIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Record one asset allocation, or one write-off.

    `amount_paise` is an INTEGER on the wire. There is no rupee field on this
    surface and no float anywhere near it: a rupee string parsed in the browser
    is where the paise go missing.
    """
    _set_correlation_header(response, request)
    try:
        with _session(request, database) as session:
            return closure_svc.create_allocation(
                session, cap_id=cap_id, wbs_id=body.wbs_id,
                asset_name=body.asset_name,
                asset_category=body.asset_category,
                amount_paise=body.amount_paise,
                is_writeoff=body.is_writeoff,
                writeoff_reason=body.writeoff_reason,
                actor=_actor(request))
    except closure_svc.ClosureServiceError as exc:
        raise _service_error_to_http(exc)


# =========================================================== SCR-11 / 12 / 14
# Read-only control views. See the module docstring for why they are here.
@router.get("/api/control/budget-revisions")
def list_budget_revisions(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-11. Budget revision requests, with the cell each one moves.

    `delta_paise` is SIGNED and is rendered as signed: a supplement and a
    surrender are different events and collapsing them to a magnitude would
    lose which one this is.
    """
    _set_correlation_header(response, request)
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": _limit(limit)}
    if project_id is not None:
        conditions.append("w.project_id = %(project_id)s")
        params["project_id"] = project_id
    if status is not None:
        conditions.append("r.status = %(status)s")
        params["status"] = status
    where = (" AND ".join(conditions) + " AND ") if conditions else ""
    with _session(request, database) as session:
        rows = repo.query(
            session,
            f"""
            SELECT r.revision_id, r.wbs_id, w.wbs_code, w.description,
                   w.project_id, p.capex_code, r.budget_head_id, bh.name,
                   r.delta_paise, r.effective_from, r.justification, r.status,
                   r.created_by, r.created_at, r.decided_by, r.decided_at,
                   r.decision_note, r.budget_line_id
            FROM budget_revision r
            JOIN wbs_element w ON w.wbs_id = r.wbs_id
            JOIN project p ON p.project_id = w.project_id
            JOIN budget_head bh ON bh.budget_head_id = r.budget_head_id
            WHERE {where}{{scope}}
            ORDER BY r.created_at DESC, r.revision_id DESC
            LIMIT %(limit)s
            """,
            params, columns=_PROJECT_COLUMNS)
    return {
        "items": [
            {"revision_id": r[0], "wbs_id": r[1], "wbs_code": r[2],
             "wbs_description": r[3], "project_id": r[4], "capex_code": r[5],
             "budget_head_id": r[6], "budget_head": r[7],
             "delta_paise": r[8], "effective_from": _iso(r[9]),
             "justification": r[10], "status": r[11], "created_by": r[12],
             "created_at": _iso(r[13]), "decided_by": r[14],
             "decided_at": _iso(r[15]), "decision_note": r[16],
             "budget_line_id": r[17]}
            for r in rows
        ],
        "source": "wave7-control",
    }


@router.get("/api/control/budget-transfers")
def list_budget_transfers(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-12. Budget transfers, with BOTH legs named.

    THE SCOPE PREDICATE IS APPLIED TO THE `FROM` LEG'S PROJECT AND THE `TO`
    LEG'S, AND BOTH MUST PASS. `006_rls_coverage.sql` makes the same choice at
    the policy level and `scope_inventory` records why: an OR would disclose
    the far side of a cross-project transfer to a caller scoped to one side,
    which is exactly the thing a transfer screen would otherwise leak.

    It is expressed as `NOT EXISTS (... WHERE NOT ({scope}))` over a two-row
    `VALUES` of the legs, rather than as the predicate written twice.
    `repo.query` substitutes ONE compiled predicate for EVERY occurrence of the
    token, so two occurrences would both bind to whichever alias the `columns=`
    mapping names -- the same project row checked twice, which is not what
    "both legs" means. One occurrence over both legs cannot drift that way.
    """
    _set_correlation_header(response, request)
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": _limit(limit)}
    if project_id is not None:
        conditions.append(
            "(fw.project_id = %(project_id)s OR tw.project_id = %(project_id)s)")
        params["project_id"] = project_id
    if status is not None:
        conditions.append("t.status = %(status)s")
        params["status"] = status
    where = (" AND ".join(conditions) + " AND ") if conditions else ""
    with _session(request, database) as session:
        rows = repo.query(
            session,
            f"""
            SELECT t.transfer_id,
                   t.from_wbs_id, fw.wbs_code, t.from_head_id, fh.name,
                   t.to_wbs_id, tw.wbs_code, t.to_head_id, th.name,
                   t.amount_paise, t.effective_from, t.justification,
                   t.status, t.created_by, t.created_at, t.decided_by,
                   t.decided_at, t.decision_note,
                   fw.project_id, tw.project_id, fp.capex_code
            FROM budget_transfer t
            JOIN wbs_element fw ON fw.wbs_id = t.from_wbs_id
            JOIN wbs_element tw ON tw.wbs_id = t.to_wbs_id
            JOIN budget_head fh ON fh.budget_head_id = t.from_head_id
            JOIN budget_head th ON th.budget_head_id = t.to_head_id
            JOIN project fp ON fp.project_id = fw.project_id
            WHERE {where}NOT EXISTS (
                SELECT 1
                FROM (VALUES (fw.project_id), (tw.project_id))
                         AS legs (project_id)
                JOIN project p ON p.project_id = legs.project_id
                WHERE NOT ({{scope}})
            )
            ORDER BY t.created_at DESC, t.transfer_id DESC
            LIMIT %(limit)s
            """,
            params, columns=_PROJECT_COLUMNS)
    return {
        "items": [
            {"transfer_id": r[0],
             "from_wbs_id": r[1], "from_wbs_code": r[2],
             "from_head_id": r[3], "from_head": r[4],
             "to_wbs_id": r[5], "to_wbs_code": r[6],
             "to_head_id": r[7], "to_head": r[8],
             "amount_paise": r[9], "effective_from": _iso(r[10]),
             "justification": r[11], "status": r[12], "created_by": r[13],
             "created_at": _iso(r[14]), "decided_by": r[15],
             "decided_at": _iso(r[16]), "decision_note": r[17],
             "from_project_id": r[18], "to_project_id": r[19],
             "capex_code": r[20]}
            for r in rows
        ],
        "source": "wave7-control",
    }


@router.get("/api/control/purchase-requests")
def list_purchase_requests(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-14. Purchase requests with their budget verdict and live hold.

    `reserved_paise` is the sum of this request's LIVE reservations only --
    `state = 'Reserved'`. A Converted or Released hold is history and holds no
    budget; counting it would overstate what this request is still consuming.
    """
    _set_correlation_header(response, request)
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": _limit(limit)}
    if project_id is not None:
        conditions.append("pr.project_id = %(project_id)s")
        params["project_id"] = project_id
    if status is not None:
        conditions.append("pr.status = %(status)s")
        params["status"] = status
    where = (" AND ".join(conditions) + " AND ") if conditions else ""
    with _session(request, database) as session:
        rows = repo.query(
            session,
            f"""
            SELECT pr.pr_id, pr.pr_number, pr.project_id, p.capex_code, p.name,
                   pr.requested_by, pr.requested_at, pr.status,
                   pr.check_result, pr.approver, pr.approved_at,
                   pr.exception_reason, pr.reserves_budget, pr.amount_paise,
                   (SELECT COUNT(*)::bigint FROM pr_line l
                     WHERE l.pr_id = pr.pr_id),
                   COALESCE((SELECT SUM(res.amount_paise)::bigint
                             FROM pr_reservation res
                             WHERE res.pr_id = pr.pr_id
                               AND res.state = 'Reserved'), 0)
            FROM purchase_request pr
            JOIN project p ON p.project_id = pr.project_id
            WHERE {where}{{scope}}
            ORDER BY pr.requested_at DESC, pr.pr_id DESC
            LIMIT %(limit)s
            """,
            params, columns=_PROJECT_COLUMNS)
    return {
        "items": [
            {"pr_id": r[0], "pr_number": r[1], "project_id": r[2],
             "capex_code": r[3], "project_name": r[4], "requested_by": r[5],
             "requested_at": _iso(r[6]), "status": r[7],
             "check_result": r[8], "approver": r[9],
             "approved_at": _iso(r[10]), "exception_reason": r[11],
             "reserves_budget": r[12], "amount_paise": r[13],
             "line_count": r[14], "reserved_paise": r[15]}
            for r in rows
        ],
        "source": "wave7-control",
    }
