"""FastAPI router for the Wave 5 integration platform: `/api/integrations/*`.

The exact contract this module is built to is
``app/frontend/src/features/integration/integration-api.js``, which is a
CONTRACT SURFACE in its own words and which probes ``/openapi.json`` for the
path templates below. The templates are reproduced here character for
character, because that module compares them by string equality: a route that
serves perfectly well under ``{connectionId}`` is reported ABSENT by a
frontend asking for ``{connection_id}``, and the screen then renders an
unavailable state over a working endpoint.

    GET    /api/integrations/connections
    POST   /api/integrations/connections
    POST   /api/integrations/connections/{connection_id}/authorize
    GET    /api/integrations/connections/{connection_id}/organizations
    PUT    /api/integrations/connections/{connection_id}/organization
    GET    /api/integrations/connections/{connection_id}/scopes
    POST   /api/integrations/connections/{connection_id}/validate
    GET    /api/integrations/connections/{connection_id}/health
    GET    /api/integrations/events
    GET    /api/integrations/dead-letters
    POST   /api/integrations/dead-letters/{queue}/{row_id}/retry
    GET    /api/integrations/outbox
    GET    /api/integrations/inbox
    GET    /api/integrations/reconciliation
    GET    /api/integrations/exceptions
    GET    /api/integrations/control-totals

Note the American ``authorize``/``organization`` spellings. They are the
frontend's, and this file matches them rather than correcting them, because
correctness here is agreement, not orthography. The rest of this codebase
spells it ``authorise``; ``/api/zoho/{connection_id}/authorise`` in
``main.py`` still does.

**SIX OF THESE SIXTEEN OPERATIONS HAVE NOTHING BEHIND THEM, AND SAY SO.**

Not 404, which ``core/api-client.js`` collapses into "no records were found"
whenever the OpenAPI probe is unreadable -- the difference between "the
dead-letter queue is empty" and "this build has no dead-letter endpoint" is
the difference between an operator who can stop worrying and one who should
not have. Not a synthesised number either. They mount, they authenticate,
they authorise, and then they answer **503 with a code naming exactly what is
missing**:

  * ``/authorize``      -- no OAuth authorisation-code client exists. Nothing
                           in ``app/backend/integration/`` builds a consent
                           URL or exchanges a code; the only OAuth artefacts
                           in the package are the five accounts-server base
                           URLs.
  * ``/organizations``  -- organisation discovery needs a live tenant.
                           ``GET /organizations`` is listed in ``zoho.py``'s
                           inventory as the call every other call depends on,
                           and it has never been answered here.
  * ``/organization``   -- remapping (PUT) is downstream of discovery, and
                           MSG-INT-005 makes it a re-point of every synced
                           record. Offering it without the discovery that
                           tells an operator what they are choosing between
                           would be offering a blind irreversible write.
  * ``/validate``       -- module-by-module connectivity validation is a live
                           call per module.
  * ``/reconciliation`` -- PostgreSQL holds no purchase order, no GRN and no
                           bill. Commitments and actuals live in the SQLite
                           ledger behind ``/api/reconciliation``, which the
                           frontend already falls back to and LABELS. There is
                           nothing in this database to reconcile, so this
                           router does not pretend there is.
  * ``/control-totals`` -- deliberately, permanently, and for a different
                           reason from the other five. See ``get_control_totals``.

The other ten are real: they read the tables migration 010 and 011 created,
through the caller's own scope, and the one mutation among them writes an
audit entry and an ``integration_event``.

**Scope.** Every read goes through ``repo.query()`` with a literal ``{scope}``
token -- a query missing it is refused before it reaches the database -- and
every ``columns=`` mapping names all four dimensions, waiving by explicit
``None`` rather than by omission. ``integration_connection`` carries
``entity_id`` and nothing else; everything hung off a connection reaches
``entity_id`` through it, inside an ``EXISTS``. An out-of-scope row is
returned as NO ROWS and reported as 404 with the same code as a row that
never existed. It is never 403: a 403 on an id tells the caller the id is
real, which is an existence oracle over another entity's estate.

**Permissions.** ``connector.read`` is the router-level floor;
``connector.manage`` is the mutation permission. Both come from
``auth.PERMISSIONS`` and from nowhere else. There is deliberately NO local
permission table and no fallback: a previous wave's fallback table FAILED
OPEN, granting a role that the authoritative table excluded. If
``auth.PERMISSIONS`` does not carry a permission, ``auth.require`` raises
``UNKNOWN_PERMISSION`` and this router refuses -- which is the safe direction.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import observability
from ..pg import audit as audit_svc
from ..pg import integration_store as store
from ..pg import principal_scope, repo
from ..pg.engine import Database, Scope, get_database

ROOT = Path(__file__).resolve().parents[3]

# ============================================================ message catalogue
#: C10 is FROZEN. `message_id` on any response this router emits must be an id
#: that exists in it, or null -- never an id invented to look helpful. A made-up
#: id is worse than none: the frontend looks it up, finds nothing, and renders
#: an empty string where the explanation should be.
_C10_PATH = ROOT / "research" / "30_contracts" / "C10_messages.json"


def _load_message_ids() -> frozenset[str]:
    try:
        doc = json.loads(_C10_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable catalogue means NO id can be validated, so none is
        # emitted. Failing closed here costs a hint; failing open would put
        # unvalidated ids on the wire, which is the defect this guards.
        return frozenset()
    return frozenset(str(m["id"]) for m in doc.get("messages", []) if m.get("id"))


MESSAGE_IDS: frozenset[str] = _load_message_ids()


def message_id_or_none(candidate: str | None) -> str | None:
    """`candidate` if C10 declares it, else None. Never raises.

    Deliberately total. A helper that raised on an unknown id would turn a
    cosmetic mistake into a 500 on a path whose whole purpose is to report a
    different problem honestly.
    """
    if candidate and candidate in MESSAGE_IDS:
        return candidate
    return None


# ================================================================ access control
class _IntegrationAccess:
    """Router-level dependency: authenticate, and require `connector.read`, on
    EVERY integration route.

    Declared on the ROUTER, exactly as `api/budget.py::_BudgetAccess` is, so a
    route added to this file later inherits the guard instead of shipping open
    by omission. That is not a stylistic preference: the audit router shipped
    with per-route database dependencies and no permission, and the middleware
    in front of it checks only that a session header EXISTS, so `X-Session:
    anything` read the whole estate.

    Mutating routes add `connector.manage` on top of this floor.
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
            auth_mod.require(who, "connector.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.integration_principal = who
        return who


require_integration_access = _IntegrationAccess()


def _requires(permission: str):
    """One route's own permission, on top of the router's floor.

    Runs after the router dependency, so `request.state.integration_principal`
    is already populated and authentication has already happened.

    `permission` is resolved by `auth.require` against `auth.PERMISSIONS`.
    There is no local table here and no fallback branch, deliberately: a
    previous wave's fallback granted a role the authoritative table excluded,
    and it did so silently because the fallback was only consulted when the
    lookup "failed".
    """

    def _dep(request: Request) -> dict:
        from .. import auth as auth_mod

        who = getattr(request.state, "integration_principal", None) or {}
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        return who

    return _dep


router = APIRouter(dependencies=[Depends(require_integration_access)])

_CORRELATION_HEADER = "X-Correlation-Id"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

#: Nothing this router runs may hold a database transaction longer than this.
#: PostgreSQL enforces it, not a Python timer: a `SET LOCAL statement_timeout`
#: is checked by the server mid-statement, where an `asyncio.wait_for` around
#: a blocking driver call is not checked at all.
_STATEMENT_TIMEOUT = "30s"


# ==================================================================== plumbing
def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- same posture as
    ``api/budget.py`` and ``api/audit.py``."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The integration API is mounted but no database "
                               "is configured for this process.",
                    "message_id": None},
        ) from exc


def _correlation_id(request: Request) -> str:
    """The id the CALLER is actually given.

    Read from the contextvar the middleware bound for this request, so the
    audit row and the response header carry the SAME value. Minting a fresh
    uuid here -- which is what `api/budget.py` does -- tells the caller one id
    in the header (the middleware's, which wins, because `_finalise` overwrites)
    and records another in `audit_log`, leaving the correlation unfollowable in
    exactly the case it exists for.
    """
    return (observability.get_correlation_id()
            or request.headers.get(_CORRELATION_HEADER)
            or uuid4().hex[:12])


def _set_correlation_header(response: Response, request: Request) -> str:
    cid = _correlation_id(request)
    response.headers[_CORRELATION_HEADER] = cid
    return cid


def _problem(status_code: int, code: str, title: str,
             detail: str | None = None, *,
             message_id: str | None = None,
             extra: dict[str, Any] | None = None) -> HTTPException:
    """An RFC-7807-shaped error body carrying `code` and `message_id`.

    `message_id` is passed through `message_id_or_none`, so an id this
    codebase invents cannot reach the wire however it is spelled at the call
    site.
    """
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
    rendered as "no records were found", which on a route that does not exist
    is a lie about data rather than a statement about the build.

    `missing` is a separate machine-readable field rather than prose only,
    because the screens render it verbatim and an operator deciding whether to
    escalate needs the noun, not a sentence about it.
    """
    return _problem(
        503, code, code.replace("_", " ").title(),
        f"{what_is_missing}"
        + (f" {remedy}" if remedy else ""),
        # C10 declares no message for an absent capability. Six routes could
        # have been given a plausible-looking MSG-INT id here; none of them
        # would resolve, so all six send null.
        message_id=None,
        extra={"missing": what_is_missing,
               "remedy": remedy,
               "unavailable": True},
    )


def _store_error_to_http(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "INTEGRATION_ERROR")
    status = getattr(exc, "status", 400)
    message = getattr(exc, "message", str(exc))
    return _problem(status, code, str(code).replace("_", " ").title(), message)


def _principal_of(request: Request) -> dict:
    return getattr(request.state, "integration_principal", None) or {}


def _scope_for(request: Request, database: Database) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    `scope_for_request` is Contract 4's single scope-construction path and it
    fails closed: a principal who cannot be resolved gets a scope that compiles
    to FALSE, not one that compiles to TRUE. This router constructs no scope of
    its own and never sets `read_all`, which comes from `user_access_flag` and
    from nowhere else -- deriving it from a set of role NAMES short-circuits
    `compile_scope` to TRUE before any dimension is examined, which is how
    every principal outside one role set became unrestricted on all thirteen
    budget routes.
    """
    return principal_scope.scope_for_request(database, _principal_of(request))


def _actor(request: Request) -> str:
    """The acting user, SERVER-DERIVED from the session.

    Never a caller-supplied header. This value reaches `audit_log.actor` and
    `integration_event.actor`; a caller-supplied one would make both
    unattributable.
    """
    who = _principal_of(request)
    return str(who.get("user_id") or who.get("username") or "UNKNOWN")


class _Txn:
    """`database.session(scope)` with the statement timeout applied.

    A context manager rather than a helper function because the timeout has to
    be set INSIDE the transaction the caller then uses -- `SET LOCAL` outside
    one is discarded, silently, and the guard would read as present while
    doing nothing.
    """

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


# ============================================================ scope mappings
# Every mapping below names all four dimensions. A dimension a table cannot
# reach is waived by an explicit `None`, which is a decision visible in review;
# omitting the key entirely would make `compile_scope` raise, and omitting the
# COLUMN silently widened `PROJECT_SCOPE_COLUMNS` until `project` was added
# back to it in Wave 3.

#: `integration_connection` carries `entity_id` and nothing else.
CONNECTION_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id", "plant": None, "location": None, "project": None,
}

#: Anything hung off a connection reaches `entity_id` through it; `{scope}`
#: sits inside the `EXISTS` and names the `c` alias.
VIA_CONNECTION_COLUMNS: dict[str, str | None] = {
    "entity": "c.entity_id", "plant": None, "location": None, "project": None,
}

#: `reconciliation_exception` carries BOTH `entity_id` and `project_id`
#: (migration 011), and both are nullable -- an exception raised before the
#: owning project is known must still be visible to whoever can resolve it.
EXCEPTION_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id", "plant": None, "location": None,
    "project": "project_id",
}


def _via_connection(connection_expr: str) -> str:
    """The `EXISTS` clause every child table's query embeds.

    `{scope}` sits INSIDE it, so `repo.query`'s token check is satisfied by the
    clause that actually filters rather than by a token pasted somewhere
    harmless -- a `{scope}` in a comment would pass the check and filter
    nothing.

    Deliberately a local copy of `integration_store._via_connection` rather
    than an import of it: that name is private, and this router must not break
    because a module it does not own renames an underscore.
    """
    return (
        f"EXISTS (SELECT 1 FROM {store.INTEGRATION_CONNECTION} c "
        f"WHERE c.connection_id = {connection_expr} AND {{scope}})"
    )


# =============================================================== pagination
def _encode_cursor(values: Sequence[Any]) -> str:
    """An opaque, URL-safe cursor over a row's sort key.

    Opaque because a cursor that reads as an offset invites a caller to do
    arithmetic on it, and keyset pagination has no offsets to do arithmetic
    with. The values are the ORDER BY tuple of the last row returned, so the
    next page is `WHERE (key) < (cursor)` -- stable under concurrent inserts,
    which `OFFSET` is not.
    """
    payload = json.dumps([_json_safe(v) for v in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, arity: int) -> list[Any] | None:
    """The cursor's values, or a 400 naming the cursor as the problem.

    A malformed cursor is the caller's, not the server's: it is 400, and it
    says so, rather than 500 out of a base64 decoder.
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:
        raise _problem(400, "INVALID_CURSOR", "Invalid Cursor",
                       "The cursor could not be read. Request the first page "
                       "without a cursor.") from exc
    if not isinstance(values, list) or len(values) != arity:
        raise _problem(400, "INVALID_CURSOR", "Invalid Cursor",
                       "The cursor does not belong to this collection. Request "
                       "the first page without a cursor.")
    return values


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _limit(value: int) -> int:
    return min(max(value, 1), _MAX_LIMIT)


def _page(rows: list[tuple], limit: int, key: Iterable[int]) -> tuple[list[tuple], str | None]:
    """Trim an over-fetched page and mint the next cursor.

    Every list route asks for `limit + 1` rows. The extra row is never
    returned; its existence is the ONLY honest way to say whether there is a
    next page, and it is cheaper and more truthful than a COUNT(*) that races
    with the next insert.
    """
    if len(rows) <= limit:
        return rows, None
    kept = rows[:limit]
    last = kept[-1]
    return kept, _encode_cursor([last[i] for i in key])


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


# ================================================================= connections
def _connection_row(row: tuple) -> dict[str, Any]:
    return {
        "connection_id": row[0], "entity_id": row[1], "product": row[2],
        "dc": row[3], "organization_id": row[4], "connector_name": row[5],
        "mode": row[6], "per_minute_call_ceiling": row[7],
        "daily_call_ceiling": row[8], "is_active": row[9],
        "created_at": _iso(row[10]), "updated_at": _iso(row[11]),
        "version_no": row[12],
        # REQ-INT-024. There is no `client_secret` column on this table and
        # this router selects no column that could carry one. Stated as a
        # positive fact rather than left as an absence, because SCR-32 needs
        # to render whether a secret has been installed server-side and an
        # absent key is indistinguishable from a screen that forgot to ask.
        "client_secret_present": None,
    }


_CONNECTION_SELECT = """
    SELECT connection_id, entity_id, product, dc, organization_id,
           connector_name, mode, per_minute_call_ceiling, daily_call_ceiling,
           is_active, created_at, updated_at, version_no
    FROM integration_connection
"""


@router.get("/api/integrations/connections")
def list_connections(
    response: Response, request: Request,
    entity_id: str | None = Query(default=None),
    product: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-31 / 32 / 33 / 38: the connection profiles this application holds."""
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 2)

    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit + 1}
    if entity_id is not None:
        conditions.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if product is not None:
        conditions.append("product = %(product)s")
        params["product"] = product
    if mode is not None:
        conditions.append("mode = %(mode)s")
        params["mode"] = mode
    if active_only:
        conditions.append("is_active")
    if after is not None:
        conditions.append("(entity_id, connection_id) > (%(after_entity)s, %(after_id)s)")
        params["after_entity"], params["after_id"] = after

    statement = (
        _CONNECTION_SELECT
        + f"WHERE {' AND '.join(conditions)} AND {{scope}}\n"
        + "ORDER BY entity_id, connection_id\nLIMIT %(limit)s"
    )
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=CONNECTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(1, 0))
    return {"items": [_connection_row(r) for r in kept],
            "next_cursor": next_cursor}


class _ConnectionIn(BaseModel):
    """The creation body SCR-31 sends.

    `client_id` is accepted and NOT persisted: `integration_connection` has no
    column for it (migration 010). It is declared rather than rejected because
    the frontend sends it unconditionally and `extra="forbid"` would answer
    422 to a screen that is following its own documented contract. The response
    reports `client_id_persisted: false` so the screen can say so rather than
    letting an operator believe a value was stored.

    There is deliberately NO `client_secret` field. REQ-INT-024: the secret is
    installed server-side and no API accepts, returns or logs one. A field here
    would be a place for one to arrive.

    `mode` is deliberately absent too: the column defaults to MOCK, and a
    screen that could set LIVE_WRITE at creation time could start live traffic
    without the authorisation section 11.9 requires -- which the schema's own
    `ck_integration_connection_live_is_authorised` refuses anyway.
    """

    model_config = ConfigDict(extra="forbid")
    entity_id: str
    product: str
    dc: str
    connector_name: str
    client_id: str | None = None
    organization_id: str | None = None


@router.post("/api/integrations/connections", status_code=201,
             dependencies=[Depends(_requires("connector.manage"))])
def create_connection(
    body: _ConnectionIn, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-31: create a connection profile.

    **A DELIVERY CONFLICT IS REPORTED HERE RATHER THAN PAPERED OVER.**
    `integration_connection.organization_id` is NOT NULL and participates in
    `uq_integration_connection_org`, but `createConnection()` in
    `integration-api.js` deliberately does not send one -- its comment says the
    organisation is chosen on SCR-33 "after discovery", and discovery is one of
    the six routes with nothing behind it. So the documented creation flow
    cannot complete.

    The two dishonest ways out were both available and both refused. Defaulting
    the column would write a fabricated organisation id into a UNIQUE
    constraint, where it would collide with the real one later and be
    indistinguishable from it in the meantime. Making the column nullable is
    not this stream's to do and would remove the constraint that makes a
    connection meaningful.

    So: a request without `organization_id` is refused with a code that names
    the deadlock, and an operator who knows the id can still supply it
    directly. Recorded for the lead.
    """
    cid = _set_correlation_header(response, request)
    if not (body.organization_id or "").strip():
        raise _problem(
            400, "ORGANISATION_ID_REQUIRED", "Organisation Id Required",
            "integration_connection.organization_id is NOT NULL, and Zoho "
            "organisation discovery is not available in this build, so it "
            "cannot be filled in for you. Supply the organization_id of the "
            "Zoho organisation this connection binds to.",
            extra={"missing": "organization_id",
                   "discovery_route": "/api/integrations/connections/"
                                      "{connection_id}/organizations"},
        )

    actor = _actor(request)
    connection_id = f"CONN-{uuid4().hex[:12].upper()}"
    try:
        with _session(request, database) as session:
            created = store.create_connection(
                session, connection_id=connection_id,
                entity_id=body.entity_id, product=body.product, dc=body.dc,
                organization_id=body.organization_id.strip(),
                connector_name=body.connector_name, actor=actor)
            store.record_event(
                session, kind="CONNECTION_CREATED", actor=actor,
                connection_id=connection_id, correlation_id=cid,
                detail={"entity_id": body.entity_id, "product": body.product,
                        "dc": body.dc})
            audit_svc.append(
                session, actor=actor, action="INTEGRATION_CONNECTION_CREATED",
                object_type="integration_connection", object_id=connection_id,
                detail=json.dumps({"entity_id": body.entity_id,
                                   "product": body.product, "dc": body.dc,
                                   "connector_name": body.connector_name,
                                   "mode": created.get("mode")},
                                  sort_keys=True),
                correlation_id=cid)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    return {
        "connection": created,
        # Honest about what was dropped. `client_id` is not a secret -- the
        # secret is `client_secret`, which this API never accepts -- so naming
        # it back is safe and is the only way the screen can tell the operator
        # the value did not land anywhere.
        "client_id_persisted": False,
        "client_id_note": (
            "integration_connection has no client_id column in migration 010, "
            "so this value was not stored." if body.client_id else None),
    }


# ============================================== connection-scoped, unavailable
def _require_visible_connection(request: Request, database: Database,
                                connection_id: str) -> dict:
    """The connection, or 404 -- for BOTH "no such row" and "out of scope".

    Indistinguishable by design. A 403 here would confirm that an id belongs to
    a real connection in an entity the caller cannot see, which is an existence
    oracle over another entity's estate.
    """
    try:
        with _session(request, database) as session:
            found = store.get_connection(session, connection_id)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)
    if found is None:
        raise _problem(404, "CONNECTION_NOT_FOUND", "Connection Not Found",
                       f"No connection {connection_id} is visible to you.")
    return found


@router.post("/api/integrations/connections/{connection_id}/authorize",
             dependencies=[Depends(_requires("connector.manage"))])
def begin_authorisation(
    connection_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-32: the authorisation URL the operator must open.

    UNAVAILABLE. Nothing in `app/backend/integration/` builds a consent URL or
    exchanges an authorisation code; the only OAuth artefacts in the package
    are the five accounts-server base URLs in `erp.py` and
    `books_inventory.py`. A URL synthesised from those alone would be missing
    the client id, the redirect URI and the scope list, and an operator who
    opened it would get a Zoho error page rather than a consent screen.

    The connection is resolved first, so a caller who cannot see this id gets
    404 rather than a 503 that would confirm the id exists.
    """
    _set_correlation_header(response, request)
    _require_visible_connection(request, database, connection_id)
    raise _unavailable(
        "OAUTH_AUTHORISATION_UNAVAILABLE",
        "an OAuth authorisation-code client: no component builds a consent URL "
        "or exchanges an authorisation code for a refresh token.",
        remedy="Authorise the connection out of band and install the refresh "
               "token server-side.")


@router.get("/api/integrations/connections/{connection_id}/organizations")
def list_organisations(
    connection_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-33: organisations visible to the authorised credential.

    UNAVAILABLE. `GET /organizations` is a live call against an authorised
    tenant, and it is the call every other call depends on -- `zoho.py`'s
    inventory says as much. There is no cassette and no tenant here, and a
    list assembled from the connections we already hold would answer a
    different question than the one asked: it would report the organisations
    somebody already typed in, not the ones the credential can actually reach.
    """
    _set_correlation_header(response, request)
    _require_visible_connection(request, database, connection_id)
    raise _unavailable(
        "ORGANISATION_DISCOVERY_UNAVAILABLE",
        "a live authorised Zoho tenant: GET /organizations is a tenant call "
        "and this build has no credential to make it with.",
        remedy="Obtain the organization_id from the Zoho console and supply it "
               "when creating the connection.")


class _OrganisationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str


@router.put("/api/integrations/connections/{connection_id}/organization",
            dependencies=[Depends(_requires("connector.manage"))])
def map_organisation(
    connection_id: str, body: _OrganisationIn,
    response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-33: bind one organisation to this connection.

    UNAVAILABLE, and this one is a refusal to do damage rather than a missing
    dependency alone. MSG-INT-005 states what a remap means: "Changing the
    mapped Zoho organisation from {current} to {proposed} will re-point every
    synced record." Every inbox row, outbox row, watermark, rate-budget window
    and circuit in this schema hangs off `connection_id` and would silently
    come to mean something else. The re-pointing has no implementation, and
    offering the write without it would corrupt the relationship between our
    records and Zoho's while reporting success.
    """
    _set_correlation_header(response, request)
    _require_visible_connection(request, database, connection_id)
    raise _unavailable(
        "ORGANISATION_MAPPING_UNAVAILABLE",
        "the re-pointing of already-synced records that changing a mapped "
        "organisation requires: inbox, outbox, watermark, rate-budget and "
        "circuit rows all hang off this connection and nothing migrates them.",
        remedy="Create a new connection for the other organisation rather than "
               "remapping this one.")


@router.post("/api/integrations/connections/{connection_id}/validate",
             dependencies=[Depends(_requires("connector.manage"))])
def validate_connection(
    connection_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-34: run the module-by-module scope and connectivity validation.

    UNAVAILABLE. Validation means calling each module and reporting what came
    back. Without a tenant there is nothing to call, and a "validation" that
    reported the scopes we intend to request as though they had been granted
    would be the most misleading possible answer: it is precisely the screen an
    operator consults to find out whether the grant is real.
    """
    _set_correlation_header(response, request)
    _require_visible_connection(request, database, connection_id)
    raise _unavailable(
        "SCOPE_VALIDATION_UNAVAILABLE",
        "a live authorised Zoho tenant: validation calls each module and "
        "reports what the tenant answered, and there is no tenant to call.",
        remedy="The required scopes and their documentation evidence are "
               "available from this connection's /scopes route.")


# ===================================================================== scopes
@router.get("/api/integrations/connections/{connection_id}/scopes")
def list_scopes(
    connection_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-34: required scopes and the documented absences.

    BACKED, but only half the question is answerable and the response says
    which half. The REQUIRED scopes are a verified, product-specific inventory
    in the adapter package. What has been GRANTED is a property of a live
    token, and `granted` is therefore `null` with `granted_unavailable_reason`
    naming why -- rather than `[]`, which a screen would render as "no scopes
    granted" and an operator would read as a broken connection.

    The two `documented_on_oauth_scope_page: false` entries are Zoho's
    documentation defect, not ours, and they are surfaced because the
    consequence is operational: a consent screen built from Zoho's OAuth scope
    table alone omits them, and the integration then fails at runtime on
    purchase receives and custom modules.
    """
    _set_correlation_header(response, request)
    connection = _require_visible_connection(request, database, connection_id)
    product = connection.get("product")

    try:
        if product == "ERP":
            from ..integration.erp import SCOPE_EVIDENCE
            required = [
                {"scope": e.scope, "purpose": e.purpose,
                 "documented_on_oauth_scope_page": e.documented_on_oauth_scope_page,
                 "documented_on_module_page": e.documented_on_module_page}
                for e in SCOPE_EVIDENCE
            ]
        elif product == "BOOKS_INVENTORY":
            from ..integration.books_inventory import SCOPES
            required = [
                {"scope": s, "purpose": None,
                 "documented_on_oauth_scope_page": None,
                 "documented_on_module_page": None}
                for s in sorted(SCOPES)
            ]
        else:
            raise _unavailable(
                "SCOPE_INVENTORY_UNAVAILABLE",
                f"a scope inventory for product {product!r}: only ERP and "
                f"BOOKS_INVENTORY have one.")
    except (ImportError, AttributeError) as exc:
        # The adapter modules are owned by other streams. A rename there must
        # degrade this route to an honest unavailable, not to a 500.
        raise _unavailable(
            "SCOPE_INVENTORY_UNAVAILABLE",
            f"the scope inventory for product {product!r}: the adapter module "
            f"no longer exports it.") from exc

    return {
        "connection_id": connection_id,
        "product": product,
        "required": required,
        "granted": None,
        "granted_unavailable_reason": (
            "What a credential has actually been granted is a property of a "
            "live token. This build holds no token, so granted scopes are "
            "unknown rather than empty."),
        "undocumented_by_zoho": [
            r["scope"] for r in required
            if r["documented_on_oauth_scope_page"] is False],
    }


# ===================================================================== health
@router.get("/api/integrations/connections/{connection_id}/health")
def get_health(
    connection_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-38: rate budget, circuit state, watermarks and token health.

    Every number here is read from a table. `token` is the one part that is
    not: nothing in this schema stores token expiry, so it is reported as
    unknown rather than as healthy -- the safe direction to be wrong in, and
    the one an operator can act on.
    """
    _set_correlation_header(response, request)
    connection = _require_visible_connection(request, database, connection_id)

    try:
        with _session(request, database) as session:
            budgets = repo.query(
                session,
                f"""
                SELECT b.window_kind, b.allocation, b.window_start_key,
                       b.window_seconds, b.ceiling, b.used, b.exhausted_at
                FROM {store.INTEGRATION_RATE_BUDGET} b
                WHERE b.connection_id = %(connection_id)s
                  AND {_via_connection('b.connection_id')}
                ORDER BY b.window_kind, b.allocation
                """,
                {"connection_id": connection_id},
                columns=VIA_CONNECTION_COLUMNS,
            )
            circuits = repo.query(
                session,
                f"""
                SELECT r.module, r.state, r.consecutive_counted_failures,
                       r.opened_at, r.opened_reason, r.next_probe_at
                FROM {store.INTEGRATION_CIRCUIT} r
                WHERE r.connection_id = %(connection_id)s
                  AND {_via_connection('r.connection_id')}
                ORDER BY r.module
                """,
                {"connection_id": connection_id},
                columns=VIA_CONNECTION_COLUMNS,
            )
            watermarks = repo.query(
                session,
                f"""
                SELECT w.module, w.hwm, w.overlap_seconds, w.last_polled_at,
                       w.last_page_count, w.rewound_at, w.rewind_reason
                FROM {store.INTEGRATION_WATERMARK} w
                WHERE w.connection_id = %(connection_id)s
                  AND {_via_connection('w.connection_id')}
                ORDER BY w.module
                """,
                {"connection_id": connection_id},
                columns=VIA_CONNECTION_COLUMNS,
            )
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    return {
        "connection": connection,
        "rate_budget": [
            {"window_kind": r[0], "allocation": r[1], "window_start_key": r[2],
             "window_seconds": r[3], "ceiling": r[4], "used": r[5],
             "remaining": max(int(r[4]) - int(r[5]), 0),
             "exhausted_at": _iso(r[6])}
            for r in budgets
        ],
        "circuits": [
            {"module": r[0], "state": r[1], "consecutive_counted_failures": r[2],
             "opened_at": _iso(r[3]), "opened_reason": r[4],
             "next_probe_at": _iso(r[5])}
            for r in circuits
        ],
        "watermarks": [
            {"module": r[0], "hwm": _iso(r[1]), "overlap_seconds": r[2],
             "last_polled_at": _iso(r[3]), "last_page_count": r[4],
             "rewound_at": _iso(r[5]), "rewind_reason": r[6]}
            for r in watermarks
        ],
        "token": {
            "state": "UNKNOWN",
            "reason": "This schema stores no access- or refresh-token expiry, "
                      "so token health is unknown rather than healthy.",
        },
    }


# ===================================================================== events
@router.get("/api/integrations/events")
def list_events(
    response: Response, request: Request,
    connection_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    module: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-26: the integration event log, newest first, cursor-paginated.

    `event_id` is an identity column, so it is a total order and a keyset
    cursor over it needs no tiebreaker.
    """
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 1)

    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit + 1}
    if connection_id is not None:
        conditions.append("e.connection_id = %(connection_id)s")
        params["connection_id"] = connection_id
    if kind is not None:
        conditions.append("e.kind = %(kind)s")
        params["kind"] = kind
    if module is not None:
        conditions.append("e.module = %(module)s")
        params["module"] = module
    if correlation_id is not None:
        conditions.append("e.correlation_id = %(correlation_id)s")
        params["correlation_id"] = correlation_id
    if after is not None:
        conditions.append("e.event_id < %(after_event_id)s")
        params["after_event_id"] = after[0]

    # An event may carry a NULL connection_id -- an estate-wide sweep, for
    # instance. Those rows belong to no entity, so they are visible to a
    # caller whose scope is unrestricted on `entity` and to nobody else; the
    # `{scope}` token still governs, inside the EXISTS, for every row that
    # does name a connection.
    statement = f"""
        SELECT e.event_id, e.connection_id, e.correlation_id, e.kind,
               e.module, e.at, e.job_id, e.inbox_id, e.outbox_id, e.actor,
               e.detail
        FROM {store.INTEGRATION_EVENT} e
        WHERE {' AND '.join(conditions)}
          AND {_via_connection('e.connection_id')}
        ORDER BY e.event_id DESC
        LIMIT %(limit)s
    """
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=VIA_CONNECTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(0,))
    return {
        "items": [
            {"event_id": r[0], "connection_id": r[1], "correlation_id": r[2],
             "kind": r[3], "module": r[4], "at": _iso(r[5]), "job_id": r[6],
             "inbox_id": r[7], "outbox_id": r[8], "actor": r[9],
             "detail": r[10]}
            for r in kept
        ],
        "next_cursor": next_cursor,
    }


# ====================================================================== inbox
@router.get("/api/integrations/inbox")
def list_inbox(
    response: Response, request: Request,
    connection_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Inbound acquisition status.

    `payload` is NOT selected. It is the sanitised payload and it is large, and
    a queue screen needs the state machine, not the document. The redaction
    metadata IS selected, so a screen can say a payload was redacted without
    the payload being on the wire to prove it.
    """
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 2)

    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit + 1}
    if connection_id is not None:
        conditions.append("i.connection_id = %(connection_id)s")
        params["connection_id"] = connection_id
    if module is not None:
        conditions.append("i.module = %(module)s")
        params["module"] = module
    if state is not None:
        conditions.append("i.state = %(state)s")
        params["state"] = state
    if after is not None:
        conditions.append(
            "(i.received_at, i.inbox_id) < (%(after_at)s, %(after_id)s)")
        params["after_at"], params["after_id"] = after

    statement = f"""
        SELECT i.inbox_id, i.connection_id, i.module, i.external_id,
               i.payload_sha, i.external_status_raw, i.external_status_product,
               i.external_status_api_version, i.received_at, i.state,
               i.attempts, i.max_attempts, i.next_attempt_at, i.processed_at,
               i.quarantine_reason, i.last_error, i.correlation_id,
               i.payload_classification, i.redacted_keys
        FROM {store.INTEGRATION_INBOX} i
        WHERE {' AND '.join(conditions)}
          AND {_via_connection('i.connection_id')}
        ORDER BY i.received_at DESC, i.inbox_id DESC
        LIMIT %(limit)s
    """
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=VIA_CONNECTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(8, 0))
    return {"items": [_inbox_row(r) for r in kept], "next_cursor": next_cursor}


def _inbox_row(r: tuple) -> dict[str, Any]:
    return {
        "inbox_id": r[0], "connection_id": r[1], "module": r[2],
        "external_id": r[3], "payload_sha": r[4],
        "external_status_raw": r[5], "external_status_product": r[6],
        "external_status_api_version": r[7], "received_at": _iso(r[8]),
        "state": r[9], "attempts": r[10], "max_attempts": r[11],
        "next_attempt_at": _iso(r[12]), "processed_at": _iso(r[13]),
        "quarantine_reason": r[14], "last_error": r[15],
        "correlation_id": r[16], "payload_classification": r[17],
        "redacted_keys": r[18],
    }


# ===================================================================== outbox
@router.get("/api/integrations/outbox")
def list_outbox(
    response: Response, request: Request,
    connection_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The outbound emission queue: what we have tried to send, and what
    happened. `payload` is not selected, for the same reason as the inbox."""
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 2)

    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit + 1}
    if connection_id is not None:
        conditions.append("o.connection_id = %(connection_id)s")
        params["connection_id"] = connection_id
    if module is not None:
        conditions.append("o.module = %(module)s")
        params["module"] = module
    if state is not None:
        conditions.append("o.state = %(state)s")
        params["state"] = state
    if after is not None:
        conditions.append(
            "(o.created_at, o.outbox_id) < (%(after_at)s, %(after_id)s)")
        params["after_at"], params["after_id"] = after

    statement = f"""
        SELECT o.outbox_id, o.connection_id, o.module, o.local_id,
               o.dedupe_key, o.state, o.attempts, o.max_attempts,
               o.next_attempt_at, o.external_id, o.sent_at, o.last_error,
               o.correlation_id, o.created_at, o.created_by, o.updated_at
        FROM {store.INTEGRATION_OUTBOX} o
        WHERE {' AND '.join(conditions)}
          AND {_via_connection('o.connection_id')}
        ORDER BY o.created_at DESC, o.outbox_id DESC
        LIMIT %(limit)s
    """
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=VIA_CONNECTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(13, 0))
    return {"items": [_outbox_row(r) for r in kept], "next_cursor": next_cursor}


def _outbox_row(r: tuple) -> dict[str, Any]:
    return {
        "outbox_id": r[0], "connection_id": r[1], "module": r[2],
        "local_id": r[3], "dedupe_key": r[4], "state": r[5],
        "attempts": r[6], "max_attempts": r[7],
        "next_attempt_at": _iso(r[8]), "external_id": r[9],
        "sent_at": _iso(r[10]), "last_error": r[11], "correlation_id": r[12],
        "created_at": _iso(r[13]), "created_by": r[14],
        "updated_at": _iso(r[15]),
    }


# =============================================================== dead letters
#: The inbox states a row can be manually retried FROM. `DISCARDED` is absent
#: deliberately: it is an idempotent no-op, not a failure, and re-arming one
#: would re-apply a payload that was correctly skipped.
_RETRYABLE_INBOX = ("DEAD", "QUARANTINED")
#: The outbox states a row can be retried FROM. `SENT` is absent for the reason
#: that matters most in this file: re-sending a purchase order that Zoho already
#: accepted creates a second commitment.
_RETRYABLE_OUTBOX = ("DEAD", "FAILED")


@router.get("/api/integrations/dead-letters")
def list_dead_letters(
    response: Response, request: Request,
    connection_id: str | None = Query(default=None),
    queue: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-39: inbox and outbox rows in DEAD or FAILED.

    One UNION over the two queues so a single cursor walks both. The sort key
    is `(queue, row_id)` rather than a timestamp: the two tables' time columns
    mean different things (`received_at` is when Zoho's payload arrived,
    `created_at` is when we enqueued ours) and ordering a merged list by two
    different clocks would produce an order no operator could predict.
    """
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 2)

    if queue is not None and queue not in ("inbox", "outbox"):
        raise _problem(400, "UNKNOWN_QUEUE", "Unknown Queue",
                       "queue must be 'inbox' or 'outbox'.")

    conditions = ["TRUE"]
    params: dict[str, Any] = {
        "limit": limit + 1,
        "inbox_states": list(_RETRYABLE_INBOX),
        "outbox_states": list(_RETRYABLE_OUTBOX),
    }
    if connection_id is not None:
        conditions.append("d.connection_id = %(connection_id)s")
        params["connection_id"] = connection_id
    if queue is not None:
        conditions.append("d.queue = %(queue)s")
        params["queue"] = queue
    if after is not None:
        conditions.append("(d.queue, d.row_id) > (%(after_queue)s, %(after_id)s)")
        params["after_queue"], params["after_id"] = after

    # `{scope}` appears TWICE here, once per arm of the union, and that is
    # correct rather than a duplication to tidy away: `repo.query` substitutes
    # every occurrence of the token with the same compiled predicate and the
    # same parameters, so both arms are filtered by the caller's own scope.
    # Writing one arm to reach the other's predicate indirectly -- which an
    # earlier draft of this query did -- produced a correlated subquery that
    # was harder to read and no more filtered.
    statement = f"""
        WITH dead AS (
            SELECT 'inbox'::text AS queue, i.inbox_id AS row_id,
                   i.connection_id, i.module, i.state, i.attempts,
                   i.max_attempts, i.last_error, i.correlation_id,
                   i.received_at AS occurred_at, i.external_id AS reference
            FROM {store.INTEGRATION_INBOX} i
            WHERE i.state = ANY(%(inbox_states)s)
              AND {_via_connection('i.connection_id')}
            UNION ALL
            SELECT 'outbox'::text AS queue, o.outbox_id AS row_id,
                   o.connection_id, o.module, o.state, o.attempts,
                   o.max_attempts, o.last_error, o.correlation_id,
                   o.created_at AS occurred_at, o.local_id AS reference
            FROM {store.INTEGRATION_OUTBOX} o
            WHERE o.state = ANY(%(outbox_states)s)
              AND {_via_connection('o.connection_id')}
        )
        SELECT d.queue, d.row_id, d.connection_id, d.module, d.state,
               d.attempts, d.max_attempts, d.last_error, d.correlation_id,
               d.occurred_at, d.reference
        FROM dead d
        WHERE {' AND '.join(conditions)}
        ORDER BY d.queue, d.row_id
        LIMIT %(limit)s
    """
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=VIA_CONNECTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(0, 1))
    return {
        "items": [
            {"queue": r[0], "row_id": r[1], "connection_id": r[2],
             "module": r[3], "state": r[4], "attempts": r[5],
             "max_attempts": r[6], "last_error": r[7], "correlation_id": r[8],
             "at": _iso(r[9]), "reference": r[10]}
            for r in kept
        ],
        "next_cursor": next_cursor,
    }


@router.post("/api/integrations/dead-letters/{queue}/{row_id}/retry",
             dependencies=[Depends(_requires("connector.manage"))])
def retry_dead_letter(
    queue: str, row_id: str, response: Response, request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-39: manual retry of one dead-lettered row.

    **Idempotent by STATE, not by key.** The frontend mints an
    `Idempotency-Key` per retry attempt and reuses it across transport retries
    of that attempt, and that header is recorded in the audit entry and the
    event. It is not used for deduplication, because this schema has no table
    to remember keys in and inventing one is not this stream's to do. The
    operation does not need it: the UPDATE matches only rows still in a
    retryable state, so a replay of the same attempt re-arms nothing and
    reports `already_armed`. That is a weaker guarantee than key-based dedup
    against two DIFFERENT attempts racing, and it is stated rather than
    implied.

    `SENT` is not retryable. Re-emitting a purchase order Zoho has already
    accepted creates a second commitment, which is the exact defect the
    outbox's dedupe key exists to prevent.
    """
    cid = _set_correlation_header(response, request)
    if queue not in ("inbox", "outbox"):
        raise _problem(400, "UNKNOWN_QUEUE", "Unknown Queue",
                       "queue must be 'inbox' or 'outbox'.")

    actor = _actor(request)
    idempotency_key = request.headers.get("Idempotency-Key") or None
    table = store.INTEGRATION_INBOX if queue == "inbox" else store.INTEGRATION_OUTBOX
    id_column = "inbox_id" if queue == "inbox" else "outbox_id"
    retryable = _RETRYABLE_INBOX if queue == "inbox" else _RETRYABLE_OUTBOX
    # RECEIVED re-enters the inbox processor; PENDING re-enters the sender.
    target_state = "RECEIVED" if queue == "inbox" else "PENDING"

    try:
        with _session(request, database) as session:
            # Existence first, in the caller's scope. A row that is invisible
            # and a row that does not exist give the SAME 404 -- never a 403,
            # which would confirm the id is real.
            current = repo.query_one(
                session,
                f"""
                SELECT t.{id_column}, t.connection_id, t.state, t.attempts
                FROM {table} t
                WHERE t.{id_column} = %(row_id)s
                  AND {_via_connection('t.connection_id')}
                """,
                {"row_id": row_id},
                columns=VIA_CONNECTION_COLUMNS,
            )
            if current is None:
                raise _problem(
                    404, "DEAD_LETTER_NOT_FOUND", "Dead Letter Not Found",
                    f"No {queue} row {row_id} is visible to you.")

            connection_id, state, attempts = current[1], current[2], current[3]
            if state not in retryable:
                if state == target_state:
                    # Already re-armed -- the replay case. Not an error.
                    return {"queue": queue, "row_id": row_id,
                            "connection_id": connection_id,
                            "state": state, "already_armed": True,
                            "retried": False}
                raise _problem(
                    409, "NOT_RETRYABLE", "Not Retryable",
                    f"{queue} row {row_id} is in state {state}; only "
                    f"{', '.join(retryable)} may be retried.",
                    extra={"state": state, "retryable_from": list(retryable)})

            if queue == "inbox":
                # `ck_integration_inbox_state_dead_is_exhausted` requires
                # attempts >= max_attempts while DEAD, so leaving DEAD must
                # reset the counter or the row is instantly re-deadable.
                # `processed_at` must be NULL for any non-PROCESSED state.
                repo.query(
                    session,
                    f"""
                    UPDATE {store.INTEGRATION_INBOX} i SET
                        state = %(state)s,
                        attempts = 0,
                        next_attempt_at = %(now)s,
                        processed_at = NULL,
                        quarantine_reason = NULL
                    WHERE i.inbox_id = %(row_id)s
                      AND i.state = ANY(%(retryable)s)
                      AND {_via_connection('i.connection_id')}
                    RETURNING i.inbox_id
                    """,
                    {"row_id": row_id, "state": target_state,
                     "now": datetime.now(timezone.utc),
                     "retryable": list(retryable)},
                    columns=VIA_CONNECTION_COLUMNS,
                )
            else:
                # PENDING requires external_id and sent_at to be NULL
                # (`ck_integration_outbox_sent_has_external_id` /
                # `..._sent_at`). A row that reached DEAD or FAILED was never
                # accepted, so both are already NULL; they are written anyway
                # so the statement cannot depend on that being true.
                repo.query(
                    session,
                    f"""
                    UPDATE {store.INTEGRATION_OUTBOX} o SET
                        state = %(state)s,
                        attempts = 0,
                        next_attempt_at = %(now)s,
                        external_id = NULL,
                        sent_at = NULL,
                        updated_at = %(now)s,
                        updated_by = %(actor)s
                    WHERE o.outbox_id = %(row_id)s
                      AND o.state = ANY(%(retryable)s)
                      AND {_via_connection('o.connection_id')}
                    RETURNING o.outbox_id
                    """,
                    {"row_id": row_id, "state": target_state,
                     "now": datetime.now(timezone.utc), "actor": actor,
                     "retryable": list(retryable)},
                    columns=VIA_CONNECTION_COLUMNS,
                )

            # `last_error` is deliberately NOT cleared: it is the evidence of
            # why the row died, and the attempts counter that is reset above
            # is recorded here before it goes.
            store.record_event(
                session, kind="DEAD_LETTER_RETRIED", actor=actor,
                connection_id=connection_id, correlation_id=cid,
                inbox_id=row_id if queue == "inbox" else None,
                outbox_id=row_id if queue == "outbox" else None,
                detail={"queue": queue, "from_state": state,
                        "to_state": target_state,
                        "attempts_before_reset": attempts,
                        "idempotency_key": idempotency_key})
            audit_svc.append(
                session, actor=actor, action="INTEGRATION_DEAD_LETTER_RETRIED",
                object_type=f"integration_{queue}", object_id=row_id,
                detail=json.dumps({"queue": queue, "from_state": state,
                                   "to_state": target_state,
                                   "attempts_before_reset": attempts,
                                   "connection_id": connection_id,
                                   "idempotency_key": idempotency_key},
                                  sort_keys=True),
                correlation_id=cid)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    return {"queue": queue, "row_id": row_id, "connection_id": connection_id,
            "state": target_state, "already_armed": False, "retried": True,
            "attempts_before_reset": attempts}


# ================================================================= exceptions
@router.get("/api/integrations/exceptions")
def list_reconciliation_exceptions(
    response: Response, request: Request,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """SCR-27: the unmatched-document exception queue.

    `local_paise` and `source_paise` are integer paise straight out of `bigint`
    columns. No SUM, no rollup, no cast needed and none invented: these are two
    stored magnitudes, and a total across them would be the control total this
    router refuses to synthesise.
    """
    _set_correlation_header(response, request)
    limit = _limit(limit)
    after = _decode_cursor(cursor, 2)

    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit + 1}
    if status is not None:
        conditions.append("status = %(status)s")
        params["status"] = status
    if kind is not None:
        conditions.append("kind = %(kind)s")
        params["kind"] = kind
    if entity_id is not None:
        conditions.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if project_id is not None:
        conditions.append("project_id = %(project_id)s")
        params["project_id"] = project_id
    if after is not None:
        conditions.append(
            "(raised_at, exception_id) < (%(after_at)s, %(after_id)s)")
        params["after_at"], params["after_id"] = after

    statement = f"""
        SELECT exception_id, kind, object_type, object_id, entity_id,
               project_id, status, detail, local_paise, source_paise,
               correlation_id, raised_at, resolved_at, resolved_by,
               resolution_note
        FROM reconciliation_exception
        WHERE {' AND '.join(conditions)} AND {{scope}}
        ORDER BY raised_at DESC, exception_id DESC
        LIMIT %(limit)s
    """
    try:
        with _session(request, database) as session:
            rows = repo.query(session, statement, params,
                              columns=EXCEPTION_COLUMNS)
    except store.IntegrationStoreError as exc:
        raise _store_error_to_http(exc)

    kept, next_cursor = _page(rows, limit, key=(11, 0))
    return {
        "items": [
            {"exception_id": r[0], "kind": r[1], "object_type": r[2],
             "object_id": r[3], "entity_id": r[4], "project_id": r[5],
             "status": r[6], "detail": r[7], "local_paise": r[8],
             "source_paise": r[9], "correlation_id": r[10],
             "raised_at": _iso(r[11]), "resolved_at": _iso(r[12]),
             "resolved_by": r[13], "resolution_note": r[14]}
            for r in kept
        ],
        "next_cursor": next_cursor,
    }


# ============================================================= reconciliation
@router.get("/api/integrations/reconciliation")
def get_reconciliation(
    response: Response, request: Request,
    project_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """SCR-18: commitment against actual, with each side's integration state.

    UNAVAILABLE, and for a structural reason rather than a missing tenant.
    PostgreSQL holds no `purchase_order`, no `po_line`, no `grn` and no `bill`
    table -- migrations 001 to 011 create none of them. Commitments and actuals
    live in the SQLite ledger, which `/api/reconciliation` already serves and
    which `integration-api.js` already falls back to under the label
    `ledger-compat`.

    What this route would ADD is the integration state of each side, and that
    is exactly the half this database cannot supply, because there is nothing
    here to join the inbox and outbox rows TO. Serving the ledger's answer from
    this path would relabel a `ledger-compat` result as `wave5` and remove the
    one thing the screen uses to tell an operator which question was answered.

    Takes NO database dependency, deliberately -- as `get_control_totals` does
    not. A route that never queries must not answer `DATABASE_NOT_CONFIGURED`
    on a process without a database: that is a 503 with the WRONG code, and it
    would hide the real reason behind a transient-looking one an operator
    would try to fix by restarting something.
    """
    _set_correlation_header(response, request)
    raise _unavailable(
        "INTEGRATION_RECONCILIATION_UNAVAILABLE",
        "the commitment and actual tables: PostgreSQL holds no purchase order, "
        "GRN or bill, so there is nothing here to pair with the inbox and "
        "outbox rows.",
        remedy="/api/reconciliation serves this application's own ledger "
               "reconciliation, without the integration state of either side.")


# ============================================================= control totals
@router.get("/api/integrations/control-totals")
def get_control_totals(
    response: Response, request: Request,
    connection_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    window_start: str | None = Query(default=None),
    window_end: str | None = Query(default=None),
) -> dict[str, Any]:
    """SCR-26's control totals.

    UNAVAILABLE, PERMANENTLY, AND THIS ONE IS NOT WAITING ON A TENANT.

    A control total is a statement that the count and value on OUR side equals
    the count and value on ZOHO's side for a sync window. This database knows
    the first half perfectly well -- inbox rows, outbox rows, both sides of
    every window are right there -- and knows nothing whatsoever about the
    second.

    Everything needed to produce a confident-looking number is therefore
    present, which is precisely the danger. Counting our own inbox against our
    own outbox and calling the result a control total would compare our ledger
    against itself, and a comparison of a thing with itself ALWAYS BALANCES.
    It would render green, permanently, including on the day Zoho silently
    stopped accepting our purchase orders. That is the single most dangerous
    number this application could show, and it is worth more as an absence.

    So no number is synthesised, no partial "our side only" total is returned
    that a screen might render next to a blank column, and the 503 says which
    half is missing.
    """
    _set_correlation_header(response, request)
    raise _unavailable(
        "CONTROL_TOTALS_UNAVAILABLE",
        "Zoho's side of the comparison: a control total asserts that OUR count "
        "and value equal THEIRS for a window, and no endpoint in this build "
        "knows theirs.",
        remedy="Nothing here is synthesised from the ledger: a control total "
               "that only ever compared us with ourselves would always "
               "balance, including when the integration had stopped.")
