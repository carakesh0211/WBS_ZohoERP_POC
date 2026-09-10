"""FastAPI router for the reporting layer: ``/api/reports/*``.

    GET    /api/reports/metrics
    GET    /api/reports/drill-down
    GET    /api/reports/dimensions
    GET    /api/reports/freshness
    GET    /api/reports/views
    POST   /api/reports/views
    PUT    /api/reports/views/{view_id}
    DELETE /api/reports/views/{view_id}
    PUT    /api/reports/views/{view_id}/default
    DELETE /api/reports/defaults/{report_key}

Built to the same shape as ``api/procurement.py`` and ``api/budget.py`` --
router-level authentication floor, per-route permission on top, ``repo``-scoped
service calls underneath, RFC-7807 error bodies -- because a second shape would
be a second set of mistakes.

WHY THE QUERIES ARE `GET` AND NOT `POST`
========================================

A `FilterSet` is a large object and a POST body would carry it more tidily.
Two reasons not to:

  * **A drill-down URL has to be shareable.** "Every card, chart segment and
    total is clickable to the rows behind it" means the drilled state is a
    place, and a place needs an address. A controller who finds an overrun
    sends the link, not a description of the clicks that got there.
  * **A POST is a mutation to every gate that reads the OpenAPI schema.**
    ``test_api_auth.py::MUTATING_ROUTES`` is built from live POST/PUT/PATCH/
    DELETE paths, so a POST-to-read would have to be declared a mutating route
    -- diluting a matrix whose value is that every row in it really does
    change something.

Repeated query parameters (``?entity_ids=E1&entity_ids=E2``) carry the lists.
An absent parameter is `None` (unfiltered); a present but empty one is `()`
(restricted to nothing), and `FilterSet` keeps those distinct all the way to
the wire -- see ``_filters_from``.

WHY THE FLOOR IS `budget.read`
==============================

There is no ``reports.read`` permission and one is not invented here, for the
reason ``api/procurement.py`` gives at length: ``auth.PERMISSIONS`` is a client
sign-off (D-12) and three tests pin parts of it. ``budget.read`` is held by
every role, says exactly what the floor means -- "you may see the control
data" -- and the row-level scope, not the permission, is what decides WHICH
control data. Adding a permission so a router reads tidily is not a change to
make in passing.

Saving a view is likewise not a new permission: it writes no business data,
moves no money and grants nobody anything. It writes a bookmark, under the
caller's own identity, which 017's ``WITH CHECK`` pins to the session
principal.

FOUR STATES, FOUR RESPONSES
===========================

``zero data``, ``denied scope``, ``stale data`` and ``service failure`` never
collapse into one another here:

  * zero data -> **200** with ``state="empty"`` and MSG-GEN-002.
  * denied scope -> **200** with ``state="denied"``. NOT 403: the caller is
    entitled to ask, and the honest answer is "nothing you can see", which is
    a result and not a refusal. A 403 would also make an empty estate and a
    denied one distinguishable by status code, which is an oracle.
  * a filter this schema cannot express -> **422** with ``state="unavailable"``
    and a code naming what is missing. Never a total computed without it.
  * service failure -> **503**, never an empty table.

Only ``message_id`` values present in ``research/30_contracts/C10_messages.json``
are ever emitted. MSG-GEN-002 ("No {object_type} match the current filters")
is the empty state; MSG-SEC-001 is not used for the denied state, because that
message is about a role that cannot perform an action, and this is a scope
that reaches no rows -- a different fact, and there is no frozen message for
it, so none is invented.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..pg import audit as audit_svc
from ..pg import principal_scope
from ..pg import reporting as reporting_svc
from ..pg.engine import Database, Scope, get_database

_CORRELATION_HEADER = "X-Correlation-Id"

#: The floor. See the module docstring for why it is `budget.read`.
ROUTER_FLOOR = "budget.read"

#: The only C10 message this router emits. Present in
#: `research/30_contracts/C10_messages.json`; nothing here invents an id.
MSG_NO_MATCH = "MSG-GEN-002"


class _ReportsAccess:
    """Authenticate, then require the router floor, on EVERY route.

    Declared on the router rather than per route, so a route added later cannot
    ship unguarded by omission -- the failure `api/budget.py`'s own docstring
    records the audit router shipping with.
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
        request.state.reports_principal = who
        return who


require_reports_access = _ReportsAccess()


#: Publishing a view into an entity is a write that affects other principals.
SHARE_PERMISSION = "report.view.share"


def _require_share_permission(request: Request, visibility: str | None) -> None:
    """Refuse a SHARED view to a caller who may only keep private ones.

    Checked in the handler, not on the route: `visibility` arrives in the BODY,
    which a route-level dependency cannot see. Anything that is not SHARED --
    including `None` on a PATCH that does not mention visibility -- passes
    untouched, so saving and editing a private view stays at the router floor.
    """
    if visibility is None or visibility.upper() != "SHARED":
        return
    from .. import auth as auth_mod

    who = getattr(request.state, "reports_principal", None) or {}
    try:
        auth_mod.require(who, SHARE_PERMISSION)
    except auth_mod.AuthError as exc:
        raise HTTPException(exc.status,
                            {"code": exc.code, "message": exc.message})

router = APIRouter(dependencies=[Depends(require_reports_access)])


def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- the posture
    `api/budget.py`, `api/procurement.py` and `api/health.py` all take.

    THIS IS THE `service failure` STATE, and it is a status code of its own
    precisely so it can never be read as an empty report.
    """
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "state": "unavailable",
                    "message": "The reporting API is mounted but no database "
                               "is configured for this process. This is a "
                               "service failure, not an empty report."},
        ) from exc


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "reports_principal", None) or {}


def _actor(request: Request) -> str:
    """SERVER-DERIVED. Never a header: this value is written into
    `report_saved_view.owner_user_id`, and 017's `WITH CHECK` compares exactly
    this identity against the session principal."""
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL grants. `read_all` comes from `user_access_flag` and
    never from a role name."""
    return principal_scope.scope_for_request(database, _principal_of(request))


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


def _filter_error_to_http(exc: reporting_svc.FilterError) -> HTTPException:
    """A refused filter is 422 with `state="unavailable"` and the reason.

    NOT a 200 with a total computed as if the filter had been supplied, and not
    a 200 with an empty table either -- both of those are answers, and the
    honest response to "I cannot apply what you asked for" is not an answer.
    """
    return _problem(
        exc.status, exc.code, exc.code.replace("_", " ").title(), exc.message,
        extra={"state": "unavailable", **exc.detail})


# =================================================================== filters

def _filters_from(
    *,
    entity_ids: list[str] | None, plant_ids: list[str] | None,
    location_ids: list[str] | None, project_ids: list[str] | None,
    wbs_paths: list[str] | None, budget_head_ids: list[str] | None,
    category_ids: list[str] | None, budget_category_ids: list[str] | None,
    vendor_ids: list[str] | None,
    item_ids: list[str] | None,
    division_ids: list[str] | None, branch_ids: list[str] | None,
    zone_ids: list[str] | None, fiscal_years: list[str] | None,
    requestor_ids: list[str] | None, approver_ids: list[str] | None,
    document_types: list[str] | None,
    lifecycle_statuses: list[str] | None, approval_statuses: list[str] | None,
    period_ids: list[str] | None, group_by: list[str] | None,
    date_from: str | None, date_to: str | None,
    sort: str | None, sort_desc: bool, cursor: str | None, limit: int,
) -> reporting_svc.FilterSet:
    """One `FilterSet` from the query string. Refuses, never coerces.

    `vendor_ids`, `item_ids` and the Fable 5.1 additions (`division_ids`,
    `branch_ids`, `zone_ids`, `fiscal_years`, `requestor_ids`, `approver_ids`)
    are ACCEPTED as parameters so they can be REFUSED with a coded reason.
    Omitting them from the signature would make FastAPI ignore them silently,
    which is the exact failure the refusal exists to prevent: the caller asks
    for one vendor, the parameter is dropped, and the report shows the whole
    estate under the vendor's name.
    """
    return reporting_svc.FilterSet.build(
        entity_ids=entity_ids, plant_ids=plant_ids,
        location_ids=location_ids, project_ids=project_ids,
        wbs_paths=wbs_paths, budget_head_ids=budget_head_ids,
        category_ids=category_ids, budget_category_ids=budget_category_ids,
        vendor_ids=vendor_ids, item_ids=item_ids,
        division_ids=division_ids, branch_ids=branch_ids,
        zone_ids=zone_ids, fiscal_years=fiscal_years,
        requestor_ids=requestor_ids, approver_ids=approver_ids,
        document_types=document_types, lifecycle_statuses=lifecycle_statuses,
        approval_statuses=approval_statuses, period_ids=period_ids,
        group_by=group_by, date_from=date_from, date_to=date_to,
        sort=sort, sort_desc=sort_desc, cursor=cursor, limit=limit,
    )


_Q = Query(default=None)


def _common_filters(
    entity_ids: list[str] | None = _Q, plant_ids: list[str] | None = _Q,
    location_ids: list[str] | None = _Q, project_ids: list[str] | None = _Q,
    wbs_paths: list[str] | None = _Q, budget_head_ids: list[str] | None = _Q,
    category_ids: list[str] | None = _Q,
    budget_category_ids: list[str] | None = _Q,
    vendor_ids: list[str] | None = _Q,
    item_ids: list[str] | None = _Q,
    division_ids: list[str] | None = _Q, branch_ids: list[str] | None = _Q,
    zone_ids: list[str] | None = _Q, fiscal_years: list[str] | None = _Q,
    requestor_ids: list[str] | None = _Q, approver_ids: list[str] | None = _Q,
    document_types: list[str] | None = _Q,
    lifecycle_statuses: list[str] | None = _Q,
    approval_statuses: list[str] | None = _Q,
    period_ids: list[str] | None = _Q, group_by: list[str] | None = _Q,
    date_from: str | None = _Q, date_to: str | None = _Q,
    sort: str | None = _Q, sort_desc: bool = False,
    cursor: str | None = _Q,
    limit: int = Query(default=reporting_svc.DEFAULT_LIMIT, ge=1,
                       le=reporting_svc.MAX_LIMIT),
) -> reporting_svc.FilterSet:
    """FastAPI dependency building the one `FilterSet` every read route uses.

    Declared ONCE and depended on by each route, so `/metrics` and
    `/drill-down` cannot come to parse the same query string differently --
    which is the disagreement this whole wave is organised against, one layer
    down from the `FilterSet` itself.
    """
    try:
        return _filters_from(
            entity_ids=entity_ids, plant_ids=plant_ids,
            location_ids=location_ids, project_ids=project_ids,
            wbs_paths=wbs_paths, budget_head_ids=budget_head_ids,
            category_ids=category_ids,
            budget_category_ids=budget_category_ids,
            vendor_ids=vendor_ids,
            item_ids=item_ids, document_types=document_types,
            division_ids=division_ids, branch_ids=branch_ids,
            zone_ids=zone_ids, fiscal_years=fiscal_years,
            requestor_ids=requestor_ids, approver_ids=approver_ids,
            lifecycle_statuses=lifecycle_statuses,
            approval_statuses=approval_statuses, period_ids=period_ids,
            group_by=group_by, date_from=date_from, date_to=date_to,
            sort=sort, sort_desc=sort_desc, cursor=cursor, limit=limit)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc


def _annotate(result: dict[str, Any]) -> dict[str, Any]:
    """Attach the frozen message id for the empty state, and nothing else.

    ONLY the empty state gets one. MSG-GEN-002 is "No {object_type} match the
    current filters. Clear filters, or adjust the {primary_filter}", which is
    exactly and only the zero-data case. The denied state has no frozen message
    -- MSG-SEC-001 is about a ROLE that cannot perform an ACTION, which is a
    different fact from a scope that reaches no rows -- so none is attached
    rather than one being invented or borrowed.
    """
    if result.get("state") == "empty":
        result["message_id"] = MSG_NO_MATCH
    return result


# ==================================================================== reads

@router.get("/api/reports/metrics")
def get_metrics(
    request: Request, response: Response,
    filters: reporting_svc.FilterSet = Depends(_common_filters),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Grouped totals. The figure a card shows, and the total it drills from."""
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = reporting_svc.aggregate(session, filters)
            result["freshness"] = reporting_svc.freshness(session, filters)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return _annotate(result)


@router.get("/api/reports/drill-down")
def get_drill_down(
    request: Request, response: Response,
    dimension: str = Query(...),
    key: str | None = Query(default=None),
    grain: list[str] | None = Query(default=None),
    filters: reporting_svc.FilterSet = Depends(_common_filters),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The rows behind one grouped figure.

    Takes the SAME query string as `/metrics` plus `dimension` and `key`, which
    is the drill-down contract stated as a URL: the caller re-sends the filters
    the card was built from and names the segment they clicked. The narrowing
    is an INTERSECTION (`FilterSet.narrowed_to`), so the drill-down cannot
    contain a row the clicked total did not count.
    """
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            result = reporting_svc.drill_down(
                session, filters, dimension, key, grain=grain)
            result["freshness"] = reporting_svc.freshness(session, filters)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return _annotate(result)


@router.get("/api/reports/dimensions")
def get_dimensions() -> dict[str, Any]:
    """What this build can group by, sort by, and what it cannot filter on.

    THE REFUSALS ARE PUBLISHED, not discovered. A dashboard that learns
    `vendor_ids` is unavailable only by sending one and getting a 422 will
    offer the control anyway and fail at the moment of use; one that reads this
    can grey it out with the reason attached. Needs no database: it describes
    the build, not the data.
    """
    return {
        "state": "ok",
        "dimensions": [
            {"name": d.name, "description": d.description}
            for d in reporting_svc.DIMENSIONS.values()
        ],
        "metrics": list(reporting_svc.COMPONENTS) + [
            "exposure", "available", "utilisation_pct"],
        "sortable": sorted(reporting_svc.SORTABLE),
        "document_types": list(reporting_svc.DOCUMENT_TYPES),
        "lifecycle_statuses": sorted(reporting_svc.C3_LABEL_BY_CODE),
        "approval_statuses": list(reporting_svc.APPROVAL_INSTANCE_STATUSES),
        "report_keys": sorted(reporting_svc.REPORT_KEYS),
        "unavailable_filters": [
            {"field": u.field_name, "code": u.code, "detail": u.detail}
            for u in reporting_svc.UNSUPPORTED_FILTERS.values()
        ],
    }


@router.get("/api/reports/freshness")
def get_freshness(
    request: Request, response: Response,
    filters: reporting_svc.FilterSet = Depends(_common_filters),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Last-sync time and source label. "A number with no provenance is not
    shown", so this is served separately as well as inline, for a screen that
    renders the freshness chip before its figures arrive."""
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        return reporting_svc.freshness(session, filters)


# =============================================================== saved views

class _ViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    report_key: str
    name: str = Field(min_length=1)
    description: str | None = None
    visibility: str = "PRIVATE"
    #: The stored `FilterSet`, in `FilterSet.to_json`'s shape. Rebuilt through
    #: `FilterSet.from_json`, which REFUSES an unknown key rather than dropping
    #: it -- a view whose name promises filters it does not apply is worse than
    #: one that will not open.
    definition: dict[str, Any] = Field(default_factory=dict)


class _ViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    visibility: str | None = None
    definition: dict[str, Any] | None = None


@router.get("/api/reports/views")
def list_views(
    request: Request, response: Response,
    report_key: str | None = Query(default=None),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Every saved view this caller may open, own and shared alike.

    A colleague's PRIVATE view is not in the list and produces no marker that
    it exists. 017's policy does that, not a predicate here.
    """
    _set_correlation_header(response, request)
    with database.session(_scope_for(request, database)) as session:
        views = reporting_svc.list_views(
            session, report_key=report_key, user_id=_actor(request))
        default_id = (reporting_svc.default_view_id(
            session, report_key, user_id=_actor(request))
            if report_key else None)
    return {"state": "ok" if views else "empty",
            "message_id": None if views else MSG_NO_MATCH,
            "views": views, "default_view_id": default_id}


@router.get("/api/reports/views/{view_id}")
def get_view(
    request: Request, response: Response, view_id: str,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """One saved view and the results of running it under the CALLER'S scope.

    A view saved by someone with wider grants yields the opener's own, narrower
    rows: honestly narrower, never an error and never the author's numbers.
    That is what makes sharing a view safe -- it stores the question, not the
    answer.
    """
    _set_correlation_header(response, request)
    try:
        with database.session(_scope_for(request, database)) as session:
            view, filters = reporting_svc.load_view(
                session, view_id, user_id=_actor(request))
            result = reporting_svc.aggregate(session, filters)
            result["freshness"] = reporting_svc.freshness(session, filters)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return {"view": view, "result": _annotate(result)}


@router.post("/api/reports/views", status_code=201)
def post_view(
    body: _ViewIn, request: Request, response: Response,
) -> dict[str, Any]:
    """Save a view. Owned by the caller, and shared only with permission.

    `_get_database()` is called here rather than declared as
    `Depends(_get_database)` deliberately: FastAPI resolves every parameter
    dependency BEFORE the handler body, so the 503 for an unconfigured
    database preceded the permission check and an unauthorised SHARED view
    answered 503 instead of 403. A gate that only fires when the database
    happens to be up is not a gate.
    """
    _set_correlation_header(response, request)
    _require_share_permission(request, body.visibility)
    database = _get_database()
    actor = _actor(request)
    view_id = f"RV-{uuid4().hex[:16]}"
    try:
        filters = reporting_svc.FilterSet.from_json(body.definition)
        with database.session(_scope_for(request, database)) as session:
            view = reporting_svc.save_view(
                session, view_id=view_id, entity_id=body.entity_id,
                report_key=body.report_key, name=body.name, filters=filters,
                actor=actor, description=body.description,
                visibility=body.visibility)
            _audit(session, actor, "report.view.created", view_id, view)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return {"state": "ok", "view": view}


@router.put("/api/reports/views/{view_id}")
def put_view(
    view_id: str, body: _ViewPatch, request: Request, response: Response,
) -> dict[str, Any]:
    """Edit a saved view. Owner only.

    A view the caller does not own answers 404 -- the same answer a view id
    that never existed gets -- rather than 403. A 403 on an id confirms the id
    is real to a caller who was refused it.
    """
    _set_correlation_header(response, request)
    # Before `_get_database()`, for the reason given on `post_view`.
    _require_share_permission(request, body.visibility)
    database = _get_database()
    actor = _actor(request)
    try:
        filters = (reporting_svc.FilterSet.from_json(body.definition)
                   if body.definition is not None else None)
        with database.session(_scope_for(request, database)) as session:
            view = reporting_svc.update_view(
                session, view_id, actor=actor, name=body.name,
                description=body.description, visibility=body.visibility,
                filters=filters)
            _audit(session, actor, "report.view.updated", view_id, view)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return {"state": "ok", "view": view}


@router.delete("/api/reports/views/{view_id}")
def delete_view(
    view_id: str, request: Request, response: Response,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Delete a saved view. Owner only; any default pointing at it CASCADEs."""
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with database.session(_scope_for(request, database)) as session:
            deleted = reporting_svc.delete_view(session, view_id, actor=actor)
            _audit(session, actor, "report.view.deleted", deleted, None)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return {"state": "ok", "view_id": deleted}


@router.put("/api/reports/views/{view_id}/default")
def put_default(
    view_id: str, request: Request, response: Response,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Make one view this caller's default for its own report.

    The report key comes from the VIEW, never from the request -- otherwise a
    `cwip_ledger` view could be registered as the default for
    `executive_dashboard`, and that screen would open on a filter set built for
    another one.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    try:
        with database.session(_scope_for(request, database)) as session:
            row = reporting_svc.set_default_view(session, view_id, actor=actor)
            _audit(session, actor, "report.default.set", view_id, row)
    except reporting_svc.FilterError as exc:
        raise _filter_error_to_http(exc) from exc
    return {"state": "ok", **row}


@router.delete("/api/reports/defaults/{report_key}")
def delete_default(
    report_key: str, request: Request, response: Response,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Clear this caller's default for one report. Idempotent.

    `cleared: false` means there was no default, which is the caller's intent
    already satisfied -- not a failure, and not a 404.
    """
    _set_correlation_header(response, request)
    actor = _actor(request)
    with database.session(_scope_for(request, database)) as session:
        cleared = reporting_svc.clear_default_view(
            session, report_key, actor=actor)
        if cleared:
            _audit(session, actor, "report.default.cleared", report_key, None)
    return {"state": "ok", "cleared": cleared}


def _audit(session, actor: str, action: str, object_id: str,
           detail: dict[str, Any] | None) -> None:
    """Append an audit entry for one saved-view mutation.

    `detail` IS A STRING to `audit.append`, not a mapping -- the chain hashes
    the canonical text, so handing it a dict would hash `repr` output and make
    the entry's hash depend on Python's dict formatting.

    A FAILURE HERE IS NOT SWALLOWED, and the first draft of this function
    swallowed it. That would not have saved the mutation it was meant to
    protect: psycopg aborts the whole transaction on any statement error, so
    catching the exception and carrying on reaches `commit` on a poisoned
    transaction and fails there instead -- later, and with a worse message.
    Letting it propagate rolls the view back, which is the safe direction and
    the posture every other mutating router here takes.
    """
    audit_svc.append(
        session, actor=actor, action=action,
        object_type="report_saved_view", object_id=object_id,
        detail=json.dumps(detail or {}, default=str, sort_keys=True))
