"""FastAPI router for access administration -- roles and row-level scope.

Exact contract this module is built to (`docs/WAVE2_CONTRACTS.md`, "API
contract -- Access administration")::

    GET  /api/admin/roles
      -> {"items":[{"role","permissions":[...]}]}
    GET  /api/admin/users/{user_id}/grants
      -> {"user_id","roles":[...],
          "scopes":{"entity_ids":[...],"plant_ids":[...],
                    "project_ids":[...],"location_ids":[...],"read_all":bool}}
    PUT  /api/admin/users/{user_id}/grants
         {"roles":[...],"scopes":{...}}   -> the updated grant

`null` on a scope dimension means unrestricted; `[]` means nothing -- see
`app/backend/pg/roles.py::resolve_scope` / `set_scope` for the enforcement.

`router = APIRouter()` is exported and mounted by `app/backend/main.py`,
which this module does not touch.

**Permission gating.** `app/backend/auth.py` is frozen (Wave 2 contract) and
is not extended with new permission keys here. Its existing catalog already
carries two permissions administration naturally maps onto:

    * `audit.read`  (Auditor, Administrator) gates the two READ endpoints --
      an auditor should be able to see who has what access without being
      able to change it.
    * `admin.reset` (Administrator only) gates the WRITE endpoint -- granting
      roles/scope is itself a privileged operation, restricted to the same
      role that already holds every other destructive admin action.

This is a deliberate reuse of `auth.py`'s SQLite-session-derived legacy
identity to authorise access to the NEW PostgreSQL-native role/scope model --
the two identity systems are different (see `roles.py`'s module docstring),
but there is exactly one authenticated actor per request, and that actor's
permission to manage access has to come from *some* enforced source. Reusing
frozen, already-audited permission keys is safer than inventing new ones this
milestone cannot get independently reviewed.

Every route on this router carries its OWN permission dependency
(`require_access_read` / `require_access_grant`, both `_RequirePermission`
instances), not a single router-level one, because the two endpoints need
different permissions. `tests/test_negative_access_matrix.py` asserts every
route in `router.routes` carries one of the two -- so a future route added
here without a dependency fails a test immediately, the same failure mode
`app/backend/api/audit.py` documents having shipped once.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth as auth_mod
from ..pg import roles as roles_mod
from ..pg.audit import append as audit_append
from ..pg.engine import Database, Scope, get_database

_CORRELATION_HEADER = "X-Correlation-Id"


def _principal(request: Request) -> dict:
    # Imported lazily: `main` imports this module (to mount `router`), so a
    # module-level import would be circular -- the same pattern
    # `app/backend/api/audit.py` uses for the same reason.
    from ..main import principal as resolve_principal

    return resolve_principal(
        authorization=request.headers.get("Authorization", ""),
        x_session=request.headers.get("X-Session", ""),
    )


class _RequirePermission:
    """Router dependency: resolve the caller, then require `permission`.

    A class (not a bare function) so each instance's `permission` is fixed
    at declaration time and visible in a route's `dependencies=[...]` list --
    the same shape `audit.py`'s `_AuditRead` uses, generalised to take the
    permission as a parameter since this router needs two different ones.
    """

    def __init__(self, permission: str) -> None:
        self.permission = permission

    def __call__(self, request: Request) -> dict:
        who = _principal(request)
        try:
            auth_mod.require(who, self.permission)
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message})
        request.state.access_principal = who
        return who


require_access_read = _RequirePermission("audit.read")
require_access_grant = _RequirePermission("admin.reset")

# A router-level floor as well as the per-route permissions below: the
# per-route guards are correct and complete for the three routes that
# exist, but a route added later would inherit nothing without this.
router = APIRouter(dependencies=[Depends(require_access_read)])


def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback -- mirrors
    `api/audit.py::_get_database`. Defence in depth behind the conditional
    mount in `main.py`."""
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The access administration API is mounted "
                               "but no database is configured for this "
                               "process."},
        ) from exc


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str,
             detail: str | None = None) -> HTTPException:
    """RFC-7807-shaped error body carrying `code`, matching the convention
    `docs/WAVE2_CONTRACTS.md` states for the Budget API and `api/audit.py`
    already applies here."""
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "code": code,
        "detail": detail,
    })


def _service_scope(actor_user_id: str) -> Scope:
    """Scope for opening a `Database.session()` to read/write the admin
    tables (`role_grant`, `user_access_flag`, `user_scope_restriction`,
    `user_scope_grant`, `app_user`). None of those tables carry an entity/
    plant/project/location column -- they are not what row-level scope
    guards -- so `read_all=True` here does not widen access to any business
    data; it only lets this router's own queries run unfiltered against
    tables `repo.compile_scope`'s dimensions were never meant to apply to.
    Row-level authorisation for THIS router is the permission dependency
    (`require_access_read` / `require_access_grant`), not `Scope`.
    """
    return Scope(user_id=actor_user_id, principal_kind="USER", read_all=True)


def _grant_to_dict(grant: roles_mod.Grant) -> dict[str, Any]:
    scope = grant.scope
    return {
        "user_id": grant.user_id,
        "roles": list(grant.roles),
        "scopes": {
            "entity_ids": None if scope.entity_ids is None else sorted(scope.entity_ids),
            "plant_ids": None if scope.plant_ids is None else sorted(scope.plant_ids),
            "project_ids": None if scope.project_ids is None else sorted(scope.project_ids),
            "location_ids": None if scope.location_ids is None else sorted(scope.location_ids),
            "read_all": scope.read_all,
        },
    }


@router.get("/api/admin/roles", dependencies=[Depends(require_access_read)])
def list_roles(response: Response, request: Request) -> dict[str, Any]:
    """Static: the 13 roles frozen in `research/30_contracts/C9_roles.json`
    and this module's own `roles.PERMISSIONS` catalog. No database access
    needed."""
    _set_correlation_header(response, request)
    items = [
        {"role": role, "permissions": sorted(roles_mod.permissions_for_roles([role]))}
        for role in roles_mod.ROLES
    ]
    return {"items": items}


@router.get("/api/admin/users/{user_id}/grants",
            dependencies=[Depends(require_access_read)])
def get_grants(
    user_id: str,
    response: Response,
    request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    who = getattr(request.state, "access_principal", {})
    actor = str(who.get("user_id") or "UNKNOWN")

    with database.session(_service_scope(actor)) as session:
        grant = roles_mod.resolve_grant(session, user_id)

    if grant is None:
        raise _problem(404, "USER_NOT_FOUND", "User not found",
                       f"no app_user row for {user_id!r}")
    return _grant_to_dict(grant)


class ScopesIn(BaseModel):
    entity_ids: list[str] | None = None
    plant_ids: list[str] | None = None
    project_ids: list[str] | None = None
    location_ids: list[str] | None = None
    read_all: bool = False


class GrantsIn(BaseModel):
    roles: list[str] = Field(default_factory=list)
    scopes: ScopesIn = Field(default_factory=ScopesIn)


@router.put("/api/admin/users/{user_id}/grants",
            dependencies=[Depends(require_access_grant)])
def put_grants(
    user_id: str,
    body: GrantsIn,
    response: Response,
    request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """Granting roles/scope is itself a privileged, audited operation: every
    successful call appends one `audit.append` entry recording who granted
    what to whom, in the SAME transaction as the grant write -- either both
    land or neither does.
    """
    _set_correlation_header(response, request)
    who = getattr(request.state, "access_principal", {})
    actor = str(who.get("user_id") or "UNKNOWN")

    unknown_roles = sorted(set(body.roles) - set(roles_mod.ROLES))
    if unknown_roles:
        raise _problem(400, "UNKNOWN_ROLE", "Unknown role(s)",
                       f"not among the 13 frozen roles: {unknown_roles}")

    with database.session(_service_scope(actor)) as session:
        if roles_mod.resolve_principal_kind(session, user_id) is None:
            raise _problem(404, "USER_NOT_FOUND", "User not found",
                           f"no app_user row for {user_id!r}")

        try:
            roles_mod.set_roles(session, user_id, body.roles, granted_by=actor)
        except roles_mod.ServiceMakerCheckerDenied as exc:
            raise _problem(409, "SERVICE_MAKER_CHECKER_DENIED",
                           "Service principal cannot hold this role", str(exc))
        except roles_mod.UnknownRole as exc:
            raise _problem(400, "UNKNOWN_ROLE", "Unknown role", str(exc))

        scopes_dict = {
            "entity_ids": body.scopes.entity_ids,
            "plant_ids": body.scopes.plant_ids,
            "project_ids": body.scopes.project_ids,
            "location_ids": body.scopes.location_ids,
        }
        roles_mod.set_scope(session, user_id, scopes_dict,
                             read_all=body.scopes.read_all, updated_by=actor)

        audit_append(
            session, actor=actor, action="GRANT_UPDATE",
            object_type="USER_ACCESS", object_id=user_id,
            detail=json.dumps({
                "roles": sorted(body.roles),
                "scopes": scopes_dict,
                "read_all": body.scopes.read_all,
            }, sort_keys=True),
            correlation_id=_correlation_id(request),
        )

        grant = roles_mod.resolve_grant(session, user_id)

    assert grant is not None  # just written inside the same transaction
    return _grant_to_dict(grant)
