"""FastAPI router for `/api/masters/*` -- items and vendors.

Exact contract this module is built to (`docs/WAVE2_CONTRACTS.md`, "API
contract -- Settings and masters"; the frontend agent codes against it in
parallel, so it must not drift)::

    GET  /api/masters/items | /api/masters/vendors
         ?cursor=&limit=&q=&source=&mapping_status=&is_active=
     -> {"items":[{ "item_id"|"vendor_id","code","name",
                    "source":"LOCAL|IMPORT|ZOHO",
                    "external_source","external_id","external_last_modified",
                    "source_of_truth_status","duplicate_of","mapping_status",
                    "is_active","version_no", ...}],
         "next_cursor","has_more"}
    POST /api/masters/{kind}          source is forced to LOCAL
    PUT  /api/masters/{kind}/{id}     refuses to edit a ZOHO-sourced field
    POST /api/masters/{kind}/{id}/deactivate
    GET  /api/masters/{kind}/duplicates
     -> {"items":[{"id","code","name","duplicate_of","reason"}]}

`router = APIRouter()` is exported and mounted by `app/backend/main.py`,
which this module does not touch (see `docs/WAVE2_CONTRACTS.md`, "File
ownership"). Routes carry their full `/api/masters/...` path.

**Vendor tax identity is masked by default.** Every vendor row this module
returns is funnelled through `app.backend.pg.masters.render_vendor` -- never
built by hand -- so masking cannot be forgotten at a call site. A caller
requesting `?reveal=true` must additionally hold the reveal permission (see
`_reveal_context`) and supply a reason; a full reveal always writes an audit
entry naming the actor, the field and the reason (done in
`masters.reveal_vendor_tax_identity`, not here, so the audit write and the
data return can never be separated by a code path that returns early).

**Identity.** No identity/scope wiring has landed for this milestone (Wave 2
stream 3 -- M4a -- owns `/api/admin/users/{id}/grants` and has not merged
into this worktree). Mirroring `app/backend/api/audit.py`'s documented
approach to the same gap, this router reads the acting identity from request
headers a trusted upstream (session middleware / gateway) is expected to set
once M4a lands: `X-Actor-Id` (who), `X-Permissions` (comma-separated
permission tokens already verified upstream). This is a provisional,
self-contained gate -- it does not import stream 3's files, which do not
exist in this worktree, and does not touch the legacy SQLite
`app.backend.auth` module, which is not in this stream's file ownership.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from ..pg import masters as pg_masters
from ..pg.engine import Database, Scope, get_database

class _MastersAccess:
    """Router-level dependency: authenticate, and require `masters.read`, on
    EVERY route of this router.

    Declared on the ROUTER rather than per route, so a route added later
    cannot ship unguarded by omission.
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
            auth_mod.require(who, "masters.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.masters_principal = who
        return who


require_masters_access = _MastersAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's read floor."""

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "masters_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_masters_access)])

_CORRELATION_HEADER = "X-Correlation-Id"
_PERMISSIONS_HEADER = "X-Permissions"
_REVEAL_REASON_HEADER = "X-Reveal-Reason"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

#: The distinct permission `docs/WAVE2_CONTRACTS.md` requires for a full
#: vendor tax-identity reveal. Provisional token pending M4a's real grants
#: model -- see the module docstring.
REVEAL_PERMISSION = "masters.tax_identity.reveal"


# ---------------------------------------------------------------- plumbing
def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- same shape as
    `app/backend/api/audit.py::_get_database`, which this router does not
    import (cross-stream file ownership), so the pattern is repeated here."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The masters API is mounted but no database is "
                               "configured for this process."},
        ) from exc


def _service_scope(actor: str) -> Scope:
    """Settings/masters carry no row-level scope dimension in this milestone
    -- item/vendor masters are organisation-wide reference data, and the
    frozen contract's query params (`cursor`, `limit`, `q`, `source`,
    `mapping_status`, `is_active`) name no entity/plant/project/location
    filter. A `Scope` with every dimension left `None` (the default) already
    compiles to an unrestricted predicate in `repo.compile_scope` -- this is
    NOT `read_all=True`("for migrations and start-up checks only", per
    `engine.py`); it is the ordinary "nothing restricted" scope, exactly as
    it would be for the anonymous default `Scope`."""
    return Scope(user_id=actor, principal_kind="USER")


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str,
             detail: str | None = None) -> HTTPException:
    """RFC-7807-shaped error body carrying the `code` field the frontend
    contract requires -- identical shape to `app/backend/api/audit.py`'s
    `_problem`, repeated here rather than imported (disjoint ownership)."""
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
    })


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "masters_principal", None) or {}


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    Was `request.headers.get("X-Actor-Id")`, which let any caller attribute a
    master-data change -- and the audit entry written for a tax-identity
    reveal -- to anyone at all.
    """
    who = _principal_of(request)
    actor = str(who.get("user_id") or who.get("username") or "")
    if not actor:
        raise _problem(401, "NOT_AUTHENTICATED", "Authentication required",
                        "A mutating request must identify its actor for the "
                        "audit trail, and the actor is taken from the "
                        "session, never from a request header.")
    return actor


def _permissions(request: Request) -> frozenset[str]:
    """The caller's permissions, derived from the AUTHENTICATED principal.

    Was `request.headers.get("X-Permissions")` split on commas -- the caller
    stating its own authorization. Any client could send
    `X-Permissions: masters.tax_identity.reveal` and unmask every
    GSTIN and PAN in the estate, which is Regulated data under the plan's
    data classification.
    """
    from .. import auth as auth_mod

    roles = set(_principal_of(request).get("roles") or ())
    return frozenset(name for name, holders in auth_mod.PERMISSIONS.items()
                     if roles & set(holders))


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _problem(400, "INVALID_CURSOR", "Invalid pagination cursor",
                        f"cursor {cursor!r} could not be decoded") from exc


def _from_master_error(exc: pg_masters.MasterDataError) -> HTTPException:
    return _problem(exc.status, exc.code, exc.message)


def _render(kind: pg_masters.MasterKind, row: dict[str, Any], *, reveal: bool) -> dict[str, Any]:
    if kind is pg_masters.VENDOR:
        return pg_masters.render_vendor(row, reveal=reveal)
    return pg_masters.render_item(row)


def _reveal_context(request: Request) -> tuple[bool, str]:
    """`(granted, reason)` for a `?reveal=true` request. Raises 403 if
    reveal was requested but the caller does not hold `REVEAL_PERMISSION` --
    fail-closed, never silently falling back to masked data for a caller who
    explicitly asked to see it (that would hide a permissions bug as a
    formatting quirk). Raises 400 if reveal is granted but no reason was
    supplied: the audit entry `masters.reveal_vendor_tax_identity` writes
    requires one."""
    if REVEAL_PERMISSION not in _permissions(request):
        raise _problem(403, "REVEAL_PERMISSION_REQUIRED",
                        "Tax identity reveal requires a distinct permission",
                        f"the caller must hold {REVEAL_PERMISSION!r} (via the "
                        f"{_PERMISSIONS_HEADER} header) to request ?reveal=true")
    reason = request.headers.get(_REVEAL_REASON_HEADER, "").strip()
    if not reason:
        raise _problem(400, "REVEAL_REASON_REQUIRED",
                        "A reveal reason is required",
                        f"supply {_REVEAL_REASON_HEADER} naming why the reveal is needed; "
                        f"it is written into the audit entry")
    return True, reason


# ---------------------------------------------------------------- list/duplicates
@router.get("/api/masters/{kind_name}")
def list_masters(
    kind_name: str, response: Response, request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    q: str | None = Query(default=None),
    source: str | None = Query(default=None),
    mapping_status: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    reveal: bool = Query(default=False),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    if kind_name == "duplicates":
        # /api/masters/duplicates has no kind of its own; the contract's
        # duplicates route is per-kind (/api/masters/{kind}/duplicates), and
        # FastAPI's path-matching already routes that separately below. This
        # branch only guards against a caller hitting this collection route
        # with the literal segment "duplicates" as if it were a kind name.
        raise _problem(404, "UNKNOWN_MASTER_KIND", "duplicates is not a master kind")

    _set_correlation_header(response, request)
    try:
        kind = pg_masters.kind_for(kind_name)
    except pg_masters.MasterDataError as exc:
        raise _from_master_error(exc) from exc

    limit = min(max(limit, 1), _MAX_LIMIT)
    after_code = _decode_cursor(cursor) if cursor else None

    reveal_granted, _reason = (False, None)
    if reveal:
        reveal_granted, _reason = _reveal_context(request)

    conditions: list[str] = []
    params: dict[str, Any] = {}
    if after_code is not None:
        conditions.append("code > %(after_code)s")
        params["after_code"] = after_code
    if q:
        conditions.append("(code ILIKE %(q)s OR name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if source is not None:
        conditions.append("source = %(source)s")
        params["source"] = source
    if mapping_status is not None:
        conditions.append("mapping_status = %(mapping_status)s")
        params["mapping_status"] = mapping_status
    if is_active is not None:
        conditions.append("is_active = %(is_active)s")
        params["is_active"] = is_active
    where = " AND ".join(conditions) if conditions else "TRUE"
    params["fetch_limit"] = limit + 1  # one extra row decides has_more with no 2nd query

    columns = ", ".join(pg_masters.row_columns(kind))
    actor = _actor(request)
    with database.session(_service_scope(actor)) as session:
        rows = session.fetchall(
            f"SELECT {columns} FROM {kind.table} WHERE {where} "  # noqa: S608 -- table/columns from a fixed internal allow-list
            f"ORDER BY code LIMIT %(fetch_limit)s",
            params,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [
            _render(kind, pg_masters.row_to_dict(kind, row), reveal=reveal_granted)
            for row in page
        ]
        if reveal_granted and kind is pg_masters.VENDOR:
            # Each row's reveal is individually audited by name, field and
            # reason -- see masters.reveal_vendor_tax_identity's docstring
            # for why the list endpoint cannot skip this just because it is
            # rendering many rows at once.
            for row in page:
                data = pg_masters.row_to_dict(kind, row)
                pg_masters.reveal_vendor_tax_identity(
                    session, data["vendor_id"], actor=actor, reason=_reason,
                    correlation_id=_correlation_id(request))

    next_cursor = _encode_cursor(page[-1][list(pg_masters.row_columns(kind)).index("code")]) \
        if has_more and page else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/api/masters/{kind_name}/duplicates")
def list_duplicates(kind_name: str, response: Response, request: Request,
                     database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        kind = pg_masters.kind_for(kind_name)
    except pg_masters.MasterDataError as exc:
        raise _from_master_error(exc) from exc

    actor = _actor(request)
    with database.session(_service_scope(actor)) as session:
        rows = session.fetchall(
            f"""
            SELECT a.{kind.id_column}, a.code, a.name, a.duplicate_of,
                   CASE WHEN a.normalised_code = b.normalised_code THEN 'code' ELSE 'name' END
            FROM {kind.table} a JOIN {kind.table} b ON b.{kind.id_column} = a.duplicate_of
            WHERE a.duplicate_of IS NOT NULL
            ORDER BY a.code
            """,  # noqa: S608
        )
    items = [
        {"id": row[0], "code": row[1], "name": row[2], "duplicate_of": row[3], "reason": row[4]}
        for row in rows
    ]
    return {"items": items}


# ---------------------------------------------------------------- create/update/deactivate
@router.post("/api/masters/{kind_name}", status_code=201, dependencies=[Depends(_requires("masters.write"))])
def create_master(kind_name: str, response: Response, request: Request,
                   payload: dict[str, Any] = Body(...),
                   database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        kind = pg_masters.kind_for(kind_name)
    except pg_masters.MasterDataError as exc:
        raise _from_master_error(exc) from exc

    actor = _actor(request)
    # `source` is NOT stripped here. It used to be, which meant a caller who
    # sent `source: "ZOHO"` got a 201 and a LOCAL row, believing they had set
    # a field they had not. `create_local` refuses the field instead, so the
    # caller is told. A ZOHO row is still never created through this API
    # (docs/WAVE2_CONTRACTS.md); it is refused rather than silently rewritten.
    with database.session(_service_scope(actor)) as session:
        try:
            row = pg_masters.create_local(session, kind, actor=actor, payload=payload,
                                           correlation_id=_correlation_id(request))
        except pg_masters.MasterDataError as exc:
            raise _from_master_error(exc) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise _problem(409, "DUPLICATE_CODE",
                            "that code already exists for this master kind") from exc
        except psycopg.errors.CheckViolation as exc:
            raise _problem(422, "INVALID_FIELD",
                            "a field value violates a database constraint") from exc
    return _render(kind, row, reveal=False)


@router.put("/api/masters/{kind_name}/{item_id}", dependencies=[Depends(_requires("masters.write"))])
def update_master(kind_name: str, item_id: str, response: Response, request: Request,
                   payload: dict[str, Any] = Body(...),
                   database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        kind = pg_masters.kind_for(kind_name)
    except pg_masters.MasterDataError as exc:
        raise _from_master_error(exc) from exc

    actor = _actor(request)
    if "version_no" not in payload:
        raise _problem(422, "MISSING_FIELD", "version_no is required",
                        "the request body must carry version_no for optimistic concurrency")
    try:
        expected_version = int(payload["version_no"])
    except (TypeError, ValueError) as exc:
        raise _problem(422, "INVALID_FIELD", "version_no must be an integer") from exc
    fields = {k: v for k, v in payload.items() if k != "version_no"}

    with database.session(_service_scope(actor)) as session:
        try:
            row = pg_masters.update_master(
                session, kind, actor=actor, id_value=item_id,
                expected_version=expected_version, payload=fields,
                correlation_id=_correlation_id(request))
        except pg_masters.MasterDataError as exc:
            raise _from_master_error(exc) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise _problem(409, "DUPLICATE_CODE",
                            "that code already exists for this master kind") from exc
        except psycopg.errors.CheckViolation as exc:
            raise _problem(422, "INVALID_FIELD",
                            "a field value violates a database constraint") from exc
    return _render(kind, row, reveal=False)


@router.post("/api/masters/{kind_name}/{item_id}/deactivate", dependencies=[Depends(_requires("masters.write"))])
def deactivate_master(kind_name: str, item_id: str, response: Response, request: Request,
                       database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    try:
        kind = pg_masters.kind_for(kind_name)
    except pg_masters.MasterDataError as exc:
        raise _from_master_error(exc) from exc

    actor = _actor(request)
    with database.session(_service_scope(actor)) as session:
        try:
            row = pg_masters.deactivate_master(session, kind, actor=actor, id_value=item_id,
                                                correlation_id=_correlation_id(request))
        except pg_masters.MasterDataError as exc:
            raise _from_master_error(exc) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise _problem(409, "DUPLICATE_CODE",
                            "that code already exists for this master kind") from exc
        except psycopg.errors.CheckViolation as exc:
            raise _problem(422, "INVALID_FIELD",
                            "a field value violates a database constraint") from exc
    return {"id": row[kind.id_column], "is_active": row["is_active"]}
