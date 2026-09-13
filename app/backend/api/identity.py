"""Identity routes (Stream D): providers, Continue with Zoho, forgot / reset
password, change password, and the Administrator's account linking.

Public routes (no session): `GET /api/auth/providers`, `POST
/api/auth/oidc/start`, `GET /api/auth/oidc/callback`, `POST
/api/auth/oidc/complete`, `POST /api/auth/forgot`, `POST /api/auth/reset`.
They are named in `main.PUBLIC_PATHS` and in the authorisation matrix's
`PUBLIC_MUTATING_ROUTES`; every one is rate-limited in PostgreSQL and
answers generically where an answer would disclose whether an account
exists. The rest require a session; the linking routes require
`admin.reset` (Administrator).
"""
from __future__ import annotations

import urllib.request
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import auth as auth_mod
from .. import db
from .. import identity_oidc as oidc
from .. import login_throttle
from ..pg import identity as identity_svc
from ..pg import notifications
from ..pg.engine import Database, get_database

router = APIRouter()
_CORRELATION_HEADER = "X-Correlation-Id"
_JWKS: oidc.JwksCache | None = None


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _database() -> Database | None:
    try:
        return get_database()
    except RuntimeError:
        return None


def _require_database() -> Database:
    database = _database()
    if database is None:
        raise _problem(503, "DATABASE_NOT_CONFIGURED",
                       "This sign-in feature needs the PostgreSQL identity store, which is "
                       "not configured for this process.")
    return database


def _principal_of(request: Request) -> dict:
    from ..main import principal
    return principal(authorization=request.headers.get("Authorization", ""),
                     x_session=request.headers.get("X-Session", ""))


def _actor(request: Request) -> str:
    """SERVER-DERIVED from the session; never a header or a body field."""
    who = _principal_of(request)
    return str(who.get("user_id") or "UNKNOWN")


def _requires(permission: str):
    def _dep(request: Request) -> dict:
        who = _principal_of(request)
        try:
            auth_mod.require(who, permission)
        except auth_mod.AuthError as exc:
            raise _problem(exc.status, exc.code, exc.message)
        return who
    return _dep


def _jwks(cfg: oidc.OidcConfig) -> oidc.JwksCache:
    global _JWKS
    if _JWKS is None or _JWKS.uri != cfg.jwks_uri:
        _JWKS = oidc.JwksCache(uri=cfg.jwks_uri, opener=urllib.request.urlopen)
    return _JWKS


def _service_error(exc: Exception) -> HTTPException:
    return _problem(getattr(exc, "status", 400), getattr(exc, "code", "IDENTITY_ERROR"),
                    getattr(exc, "message", str(exc)))


# ----------------------------------------------------------------- bodies
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ForgotIn(_Strict):
    user_id: str = Field(max_length=100)


class _ResetIn(_Strict):
    token: str = Field(max_length=300)
    new_password: str = Field(max_length=200)


class _ChangeIn(_Strict):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class _CompleteIn(_Strict):
    code: str = Field(max_length=300)


class _LinkIn(_Strict):
    subject: str = Field(max_length=300)
    email: str | None = Field(default=None, max_length=300)
    reason: str = Field(max_length=2000)


class _UnlinkIn(_Strict):
    reason: str = Field(max_length=2000)


# --------------------------------------------------------------- providers
@router.get("/api/auth/providers")
def get_providers(response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    cfg = oidc.config_from_env()
    return {
        "local": {"enabled": True, "label": "Sign in with WBS account",
                  "forgot_password": _database() is not None},
        "oidc": cfg.public() | {"enabled": cfg.enabled and _database() is not None},
    }


# -------------------------------------------------------------------- OIDC
@router.post("/api/auth/oidc/start")
def post_oidc_start(response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    cfg = oidc.config_from_env()
    try:
        with database.session(identity_svc.system_scope()) as session:
            out = identity_svc.start_oidc(
                session, cfg=cfg, requested_from=login_throttle.client_address(request),
                correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    return {"authorization_url": out["authorization_url"], "provider": oidc.PROVIDER}


@router.get("/api/auth/oidc/callback")
def get_oidc_callback(request: Request, code: str = "", state: str = "",
                      error: str = "") -> RedirectResponse:
    """The provider sends the browser here. The session is created server-
    side and handed over through a one-minute, single-use code in the
    fragment; a refusal lands on the sign-in page with its code, never the
    provider's detail."""
    database = _database()
    if database is None:
        return RedirectResponse("/#sso_error=DATABASE_NOT_CONFIGURED", status_code=303)
    if error:
        return RedirectResponse("/#sso_error=OIDC_PROVIDER_ERROR", status_code=303)
    cfg = oidc.config_from_env()
    con = db.connect()
    try:
        with database.session(identity_svc.system_scope()) as session:
            out = identity_svc.complete_oidc(
                session, con, cfg=cfg, state=state, code=code,
                requested_from=login_throttle.client_address(request), jwks=_jwks(cfg),
                correlation_id=_correlation_id(request))
    except (identity_svc.IdentityError, oidc.OidcError) as exc:
        return RedirectResponse(f"/#sso_error={exc.code}", status_code=303)
    finally:
        con.close()
    if not out.get("ok"):
        return RedirectResponse(f"/#sso_error={out['code']}", status_code=303)
    return RedirectResponse(f"/#sso={out['handoff_code']}", status_code=303)


@router.post("/api/auth/oidc/complete")
def post_oidc_complete(body: _CompleteIn, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    con = db.connect()
    try:
        with database.session(identity_svc.system_scope()) as session:
            return identity_svc.complete_handoff(session, con, code=body.code)
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    finally:
        con.close()


# --------------------------------------------------------- forgot / reset
@router.post("/api/auth/forgot", status_code=202)
def post_forgot(body: _ForgotIn, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    con = db.connect()
    try:
        with database.session(identity_svc.system_scope()) as session:
            return identity_svc.request_password_reset(
                session, con, user_id=body.user_id,
                requested_from=login_throttle.client_address(request),
                correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    finally:
        con.close()


@router.post("/api/auth/reset")
def post_reset(body: _ResetIn, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    con = db.connect()
    try:
        with database.session(identity_svc.system_scope()) as session:
            out = identity_svc.reset_password(
                session, con, token=body.token, new_password=body.new_password,
                requested_from=login_throttle.client_address(request),
                correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    finally:
        con.close()
    if not out.get("ok"):
        raise _problem(int(out.get("status") or 400), out["code"], out["message"])
    return {"ok": True, "sessions_revoked": out["sessions_revoked"],
            "message": "Your password was changed. Sign in with the new one."}


@router.post("/api/auth/password")
def post_change_password(body: _ChangeIn, response: Response, request: Request,
                         x_session: str = Header(default="")) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    who = _principal_of(request)
    database = _require_database()
    con = db.connect()
    try:
        with database.session(identity_svc.system_scope()) as session:
            out = identity_svc.change_password(
                session, con, user_id=str(who["user_id"]),
                current_password=body.current_password, new_password=body.new_password,
                keep_session=x_session or None, correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    finally:
        con.close()
    return {"ok": True, "sessions_revoked": out["sessions_revoked"]}


# --------------------------------------------------------- account linking
@router.get("/api/admin/users/{user_id}/identities",
            dependencies=[Depends(_requires("admin.reset"))])
def get_identities(user_id: str, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    with database.session(identity_svc.system_scope()) as session:
        return {"user_id": user_id, "links": identity_svc.list_links(session, user_id)}


@router.post("/api/admin/users/{user_id}/identities", status_code=201,
             dependencies=[Depends(_requires("admin.reset"))])
def post_identity_link(user_id: str, body: _LinkIn, response: Response,
                       request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    con = db.connect()
    try:
        if identity_svc.sqlite_user(con, user_id) is None:
            raise _problem(404, "USER_NOT_FOUND", f"No application user {user_id}.")
        if user_id == identity_svc.break_glass_user():
            raise _problem(409, "OIDC_BREAK_GLASS_REFUSED",
                           "The break-glass account never signs in through Zoho.")
        with database.session(identity_svc.system_scope()) as session:
            return identity_svc.link_identity(
                session, subject=body.subject, user_id=user_id, email=body.email,
                email_verified=False, linked_by=_actor(request), method="ADMIN",
                correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)
    finally:
        con.close()


@router.delete("/api/admin/users/{user_id}/identities",
               dependencies=[Depends(_requires("admin.reset"))])
def delete_identity_link(user_id: str, body: _UnlinkIn, response: Response,
                         request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _require_database()
    try:
        with database.session(identity_svc.system_scope()) as session:
            return identity_svc.unlink_identity(
                session, user_id=user_id, actor=_actor(request), reason=body.reason,
                correlation_id=_correlation_id(request))
    except identity_svc.IdentityError as exc:
        raise _service_error(exc)


__all__ = ["router", "notifications"]
