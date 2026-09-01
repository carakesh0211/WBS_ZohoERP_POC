"""FastAPI router for `/api/settings/*` -- the organisation hierarchy.

Exact contract this module is built to (`docs/WAVE2_CONTRACTS.md`, "API
contract -- Settings and masters"; the frontend agent codes against it in
parallel, so it must not drift)::

    {collection} in organisations | entities | divisions | branches | zones
                 | plants | locations | departments

    GET  /api/settings/{collection}?cursor=&limit=&q=&is_active=
     -> {"items":[{ "<id field>", "code","name", ...area-specific...,
                    "is_active","created_at","created_by",
                    "updated_at","updated_by","version_no"}],
         "next_cursor","has_more"}
    POST /api/settings/{collection}                       -> the created row
    PUT  /api/settings/{collection}/{id}                  -> the updated row
         body MUST carry version_no; mismatch -> 409 VERSION_CONFLICT
    POST /api/settings/{collection}/{id}/deactivate       -> {"id","is_active":false}

`router = APIRouter()` is exported and mounted by `app/backend/main.py`,
which this module does not touch. The eight tables this router serves
(`organisation`, `entity`, `division`, `branch`, `zone`, `plant`, `location`,
`department`) are defined in `migrations/pg/001_foundation.sql`, which is
READ-ONLY to this stream -- this module only reads and writes rows, it never
alters that schema.

**`entity.gst_no` / `entity.pan_no` are masked by default**, on the same
formula and the same reveal-permission-plus-audit discipline
`docs/WAVE2_CONTRACTS.md` states for `vendor_master`. The contract's masking
clause is written under "Settings and masters" and names vendor tax
identity explicitly; it is extended here to `entity`'s structurally
identical fields (same comment in `001_foundation.sql`: "Regulated:
encrypted or access-restricted... Never hashed") because the underlying
invariant -- never hash, mask by default, reveal is permissioned and audited
-- is a general rule for regulated tax-identity fields, not a vendor-only
one, and leaving `entity.gst_no`/`pan_no` unmasked here while masking the
same shape of data on `vendor_master` would be an inconsistent, easily
missed hole. This is an interpretation beyond the contract's literal text,
flagged as such in this stream's completion report.

**Identity.** No identity/scope wiring has landed for this milestone -- see
`app/backend/api/masters.py`'s module docstring for the full explanation.
The same provisional headers are read here: `X-Actor-Id`, `X-Permissions`,
`X-Reveal-Reason`.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import psycopg.errors
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from ..pg import audit as pg_audit
from ..pg import principal_scope
from ..pg import masters as pg_masters
from ..pg.engine import Database, Scope, get_database

class _SettingsAccess:
    """Router-level dependency: authenticate, and require `settings.read`, on
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
            auth_mod.require(who, "settings.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.settings_principal = who
        return who


require_settings_access = _SettingsAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's read floor."""

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "settings_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_settings_access)])

_CORRELATION_HEADER = "X-Correlation-Id"
_REVEAL_REASON_HEADER = "X-Reveal-Reason"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

#: Distinct from `app.backend.api.masters.REVEAL_PERMISSION` -- an entity
#: administrator and a vendor-master administrator are not necessarily the
#: same role, so this stream keeps the two permission tokens separate even
#: though the masking formula they gate is shared code.
REVEAL_PERMISSION = "settings.tax_identity.reveal"


# ============================================================================
# Collection registry
# ============================================================================
@dataclass(frozen=True)
class CollectionSpec:
    table: str
    id_column: str
    id_prefix: str
    #: Every caller-settable field beyond the governance columns
    #: (is_active/created_*/updated_*/version_no), in insertion order.
    fields: tuple[str, ...]
    #: Fields that must be present (and non-empty) on create, beyond `code`
    #: and `name`, which are always required.
    required_fields: tuple[str, ...] = field(default_factory=tuple)
    #: Fields masked by default and gated behind REVEAL_PERMISSION.
    masked_fields: tuple[str, ...] = field(default_factory=tuple)


COLLECTIONS: dict[str, CollectionSpec] = {
    "organisations": CollectionSpec(
        table="organisation", id_column="organisation_id", id_prefix="ORG",
        fields=("code", "name", "base_currency", "fy_start_month"),
    ),
    "entities": CollectionSpec(
        table="entity", id_column="entity_id", id_prefix="ENT",
        fields=("organisation_id", "code", "name", "gst_no", "pan_no", "fy_start_month"),
        required_fields=("organisation_id",),
        masked_fields=("gst_no", "pan_no"),
    ),
    "divisions": CollectionSpec(
        table="division", id_column="division_id", id_prefix="DIV",
        fields=("entity_id", "code", "name"), required_fields=("entity_id",),
    ),
    "branches": CollectionSpec(
        table="branch", id_column="branch_id", id_prefix="BR",
        fields=("entity_id", "code", "name"), required_fields=("entity_id",),
    ),
    "zones": CollectionSpec(
        table="zone", id_column="zone_id", id_prefix="ZN",
        fields=("entity_id", "code", "name"), required_fields=("entity_id",),
    ),
    "plants": CollectionSpec(
        table="plant", id_column="plant_id", id_prefix="PLT",
        fields=("entity_id", "zone_id", "code", "name"), required_fields=("entity_id",),
    ),
    "locations": CollectionSpec(
        table="location", id_column="location_id", id_prefix="LOC",
        fields=("entity_id", "plant_id", "code", "name", "address_line", "city", "state_code"),
        required_fields=("entity_id",),
    ),
    "departments": CollectionSpec(
        table="department", id_column="department_id", id_prefix="DEPT",
        fields=("entity_id", "code", "name", "cost_centre"), required_fields=("entity_id",),
    ),
}


def spec_for(name: str) -> CollectionSpec:
    spec = COLLECTIONS.get(name)
    if spec is None:
        raise _problem(404, "UNKNOWN_COLLECTION",
                        f"no settings collection {name!r}; expected one of "
                        f"{sorted(COLLECTIONS)}")
    return spec


def _row_columns(spec: CollectionSpec) -> tuple[str, ...]:
    return (spec.id_column,) + spec.fields + (
        "is_active", "created_at", "created_by", "updated_at", "updated_by", "version_no",
    )


def _row_to_dict(spec: CollectionSpec, row: tuple) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column, value in zip(_row_columns(spec), row):
        out[column] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def _select_row(session, spec: CollectionSpec, id_value: str, *, for_update: bool = False):
    columns = ", ".join(_row_columns(spec))
    suffix = " FOR UPDATE" if for_update else ""
    return session.fetchone(
        f"SELECT {columns} FROM {spec.table} WHERE {spec.id_column} = %s{suffix}",  # noqa: S608 -- table/columns from a fixed internal allow-list
        (id_value,))


def _new_id(spec: CollectionSpec) -> str:
    return f"{spec.id_prefix}-{uuid4().hex[:12].upper()}"


def _render(spec: CollectionSpec, row: dict[str, Any], *, reveal: bool) -> dict[str, Any]:
    if not spec.masked_fields:
        return dict(row)
    out = dict(row)
    if not reveal:
        if "gst_no" in spec.masked_fields:
            out["gst_no"] = pg_masters.mask_gst_no(out.get("gst_no"))
        if "pan_no" in spec.masked_fields:
            out["pan_no"] = pg_masters.mask_pan_no(out.get("pan_no"))
    out["tax_identity_revealed"] = bool(reveal)
    return out


# ============================================================================
# HTTP plumbing -- same shapes as app/backend/api/masters.py and
# app/backend/api/audit.py, repeated rather than imported (disjoint file
# ownership across streams).
# ============================================================================
def _get_database() -> Database:
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The settings API is mounted but no database is "
                               "configured for this process."},
        ) from exc


def _service_scope(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    This returned `Scope(user_id=actor)` -- every dimension `None`, i.e.
    unrestricted -- on the stated grounds that "no row-level scope dimension
    applies to the organisation hierarchy itself".

    That was wrong, and it was a live leak. This router serves `entity`,
    `plant` and `location`, and those ARE three of the four scope dimensions;
    all of them carry RLS from migration 004. An entity-restricted caller
    reading `GET /api/settings/entities` saw every entity, and because the
    all-`None` scope rendered as the old `*` wildcard, RLS waved it through
    too. Both layers agreed, and both were wrong.
    """
    return principal_scope.scope_for_request(
        database, getattr(request.state, "settings_principal", None) or {})


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str, detail: str | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank", "title": title, "status": status_code,
        "code": code, "detail": detail,
    })


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "settings_principal", None) or {}


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
    `X-Permissions: settings.tax_identity.reveal` and unmask every
    GSTIN and PAN in the estate, which is Regulated data under the plan's
    data classification.
    """
    from .. import auth as auth_mod

    roles = set(_principal_of(request).get("roles") or ())
    return frozenset(name for name, holders in auth_mod.PERMISSIONS.items()
                     if roles & set(holders))


def _reveal_context(request: Request) -> tuple[bool, str]:
    if REVEAL_PERMISSION not in _permissions(request):
        raise _problem(403, "REVEAL_PERMISSION_REQUIRED",
                        "Tax identity reveal requires a distinct permission",
                        f"the caller must hold {REVEAL_PERMISSION!r} to request "
                        f"?reveal=true")
    reason = request.headers.get(_REVEAL_REASON_HEADER, "").strip()
    if not reason:
        raise _problem(400, "REVEAL_REASON_REQUIRED", "A reveal reason is required",
                        f"supply {_REVEAL_REASON_HEADER} naming why the reveal is needed; "
                        f"it is written into the audit entry")
    return True, reason


def _reveal_entity_tax_identity(session, entity_id: str, *, actor: str, reason: str,
                                 correlation_id: str | None) -> None:
    """One audit entry per revealed field, naming the actor, the field and
    the reason -- the same discipline as
    `app.backend.pg.masters.reveal_vendor_tax_identity`, repeated here since
    `entity` is not a master-data table this stream's `pg/masters.py` owns."""
    row = _select_row(session, COLLECTIONS["entities"], entity_id)
    if row is None:
        return
    data = _row_to_dict(COLLECTIONS["entities"], row)
    for f in ("gst_no", "pan_no"):
        if data.get(f):
            pg_audit.append(session, actor, "REVEAL_TAX_IDENTITY", "entity", entity_id,
                             f"field={f} reason={reason!r}", correlation_id=correlation_id)


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _problem(400, "INVALID_CURSOR", "Invalid pagination cursor",
                        f"cursor {cursor!r} could not be decoded") from exc


# ============================================================================
# Routes
# ============================================================================
@router.get("/api/settings/{collection}")
def list_collection(
    collection: str, response: Response, request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    q: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    reveal: bool = Query(default=False),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    spec = spec_for(collection)
    limit = min(max(limit, 1), _MAX_LIMIT)
    after_id = _decode_cursor(cursor) if cursor else None

    reveal_granted, reveal_reason = False, None
    if reveal:
        if not spec.masked_fields:
            raise _problem(422, "NOT_MASKABLE", f"{collection} has no maskable fields")
        reveal_granted, reveal_reason = _reveal_context(request)

    conditions: list[str] = []
    params: dict[str, Any] = {}
    if after_id is not None:
        conditions.append(f"{spec.id_column} > %(after_id)s")
        params["after_id"] = after_id
    if q:
        conditions.append("(code ILIKE %(q)s OR name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if is_active is not None:
        conditions.append("is_active = %(is_active)s")
        params["is_active"] = is_active
    where = " AND ".join(conditions) if conditions else "TRUE"
    params["fetch_limit"] = limit + 1

    columns = ", ".join(_row_columns(spec))
    actor = _actor(request)
    with database.session(_service_scope(request, database)) as session:
        rows = session.fetchall(
            f"SELECT {columns} FROM {spec.table} WHERE {where} "  # noqa: S608
            f"ORDER BY {spec.id_column} LIMIT %(fetch_limit)s",
            params,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [_render(spec, _row_to_dict(spec, row), reveal=reveal_granted) for row in page]
        if reveal_granted:
            for row in page:
                data = _row_to_dict(spec, row)
                _reveal_entity_tax_identity(
                    session, data[spec.id_column], actor=actor,
                    reason=reveal_reason,
                    correlation_id=_correlation_id(request))

    next_cursor = _encode_cursor(page[-1][0]) if has_more and page else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.post("/api/settings/{collection}", status_code=201, dependencies=[Depends(_requires("settings.write"))])
def create_collection_row(collection: str, response: Response, request: Request,
                           payload: dict[str, Any] = Body(...),
                           database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    spec = spec_for(collection)
    actor = _actor(request)

    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code:
        raise _problem(422, "MISSING_FIELD", "code is required")
    if not name:
        raise _problem(422, "MISSING_FIELD", "name is required")
    for required in spec.required_fields:
        if not payload.get(required):
            raise _problem(422, "MISSING_FIELD", f"{required} is required")
    unknown = set(payload) - set(spec.fields)
    if unknown:
        raise _problem(422, "UNKNOWN_FIELD", f"unknown field(s): {sorted(unknown)}")

    row_id = _new_id(spec)
    provided = {f: payload[f] for f in spec.fields if f in payload}
    provided["code"], provided["name"] = code, name
    columns = [spec.id_column] + list(provided.keys()) + ["is_active", "created_by", "updated_by"]
    placeholders = ", ".join(f"%({c})s" for c in columns)
    params: dict[str, Any] = dict(provided)
    params.update({spec.id_column: row_id, "is_active": True,
                    "created_by": actor, "updated_by": actor})

    with database.session(_service_scope(request, database)) as session:
        try:
            session.execute(
                f"INSERT INTO {spec.table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
                params)
        except psycopg.errors.UniqueViolation as exc:
            raise _problem(409, "DUPLICATE_CODE",
                            f"{collection} code {code!r} already exists in this scope") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise _problem(422, "INVALID_REFERENCE",
                            "a referenced parent row does not exist") from exc
        except psycopg.errors.CheckViolation as exc:
            raise _problem(422, "INVALID_FIELD",
                            "a field value violates a database constraint") from exc

        pg_audit.append(session, actor, "CREATE", spec.table, row_id,
                         f"code={code!r} name={name!r}", correlation_id=_correlation_id(request))
        row = _select_row(session, spec, row_id)

    assert row is not None
    return _render(spec, _row_to_dict(spec, row), reveal=False)


@router.put("/api/settings/{collection}/{item_id}", dependencies=[Depends(_requires("settings.write"))])
def update_collection_row(collection: str, item_id: str, response: Response, request: Request,
                           payload: dict[str, Any] = Body(...),
                           database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    spec = spec_for(collection)
    actor = _actor(request)

    if "version_no" not in payload:
        raise _problem(422, "MISSING_FIELD", "version_no is required",
                        "the request body must carry version_no for optimistic concurrency")
    try:
        expected_version = int(payload["version_no"])
    except (TypeError, ValueError) as exc:
        raise _problem(422, "INVALID_FIELD", "version_no must be an integer") from exc

    unknown = set(payload) - set(spec.fields) - {"version_no"}
    if unknown:
        raise _problem(422, "UNKNOWN_FIELD", f"unknown field(s): {sorted(unknown)}")

    with database.session(_service_scope(request, database)) as session:
        current_row = _select_row(session, spec, item_id, for_update=True)
        if current_row is None:
            raise _problem(404, "NOT_FOUND", f"{collection} {item_id} does not exist")
        current = _row_to_dict(spec, current_row)

        if current["version_no"] != expected_version:
            raise _problem(
                409, "VERSION_CONFLICT",
                f"{item_id} was modified by someone else "
                f"(have version {current['version_no']}, expected {expected_version})")

        changes = {f: payload[f] for f in spec.fields if f in payload}
        if changes:
            set_parts = [f"{f} = %({f})s" for f in changes] + [
                "updated_by = %(updated_by)s", "updated_at = now()", "version_no = version_no + 1"]
            params: dict[str, Any] = dict(changes)
            params.update({"item_id": item_id, "updated_by": actor})
            try:
                session.execute(
                    f"UPDATE {spec.table} SET {', '.join(set_parts)} "  # noqa: S608
                    f"WHERE {spec.id_column} = %(item_id)s",
                    params)
            except psycopg.errors.UniqueViolation as exc:
                raise _problem(409, "DUPLICATE_CODE",
                                f"{collection} code already exists in this scope") from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise _problem(422, "INVALID_REFERENCE",
                                "a referenced parent row does not exist") from exc
            except psycopg.errors.CheckViolation as exc:
                raise _problem(422, "INVALID_FIELD",
                                "a field value violates a database constraint") from exc

            pg_audit.append(session, actor, "UPDATE", spec.table, item_id,
                             f"fields={sorted(changes)}", correlation_id=_correlation_id(request))

        row = _select_row(session, spec, item_id)

    assert row is not None
    return _render(spec, _row_to_dict(spec, row), reveal=False)


@router.post("/api/settings/{collection}/{item_id}/deactivate", dependencies=[Depends(_requires("settings.write"))])
def deactivate_collection_row(collection: str, item_id: str, response: Response,
                               request: Request,
                               database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    spec = spec_for(collection)
    actor = _actor(request)

    with database.session(_service_scope(request, database)) as session:
        current_row = _select_row(session, spec, item_id, for_update=True)
        if current_row is None:
            raise _problem(404, "NOT_FOUND", f"{collection} {item_id} does not exist")

        session.execute(
            f"""
            UPDATE {spec.table}
            SET is_active = false, updated_by = %(actor)s, updated_at = now(),
                version_no = version_no + 1
            WHERE {spec.id_column} = %(item_id)s
            """,  # noqa: S608
            {"actor": actor, "item_id": item_id},
        )
        pg_audit.append(session, actor, "DEACTIVATE", spec.table, item_id, "",
                         correlation_id=_correlation_id(request))

    return {"id": item_id, "is_active": False}
