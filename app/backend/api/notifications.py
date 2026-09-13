"""Notification routes (Stream E): the outbox and its monitoring for the
Administrator, dispatch on demand, and every user's own preferences.

Sending never happens inside a business request: `POST /api/notifications/
dispatch` (Administrator, or a cron with an Administrator session) drains
what is due, and the in-process ticker `main.py` starts when
`CAPEX_NOTIFICATIONS_DISPATCH_SECONDS` is set does the same on a timer.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import auth as auth_mod
from ..pg import notifications as svc
from ..pg.engine import Database, get_database

router = APIRouter()
_CORRELATION_HEADER = "X-Correlation-Id"


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _database() -> Database:
    try:
        return get_database()
    except RuntimeError:
        raise _problem(503, "DATABASE_NOT_CONFIGURED",
                       "The notification outbox needs the PostgreSQL store, which is not "
                       "configured for this process.")


def _principal_of(request: Request) -> dict:
    from ..main import principal
    return principal(authorization=request.headers.get("Authorization", ""),
                     x_session=request.headers.get("X-Session", ""))


def _actor(request: Request) -> str:
    """SERVER-DERIVED from the session."""
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


def _service_error(exc: svc.NotificationError) -> HTTPException:
    return _problem(exc.status, exc.code, exc.message)


class _PreferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(max_length=100)
    enabled: bool


class _DispatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=500)


# ------------------------------------------------------------------ admin
@router.get("/api/notifications", dependencies=[Depends(_requires("admin.reset"))])
def get_outbox(response: Response, request: Request,
               state: str | None = Query(default=None),
               event: str | None = Query(default=None),
               recipient_user_id: str | None = Query(default=None),
               limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _database()
    with database.session(svc.system_scope()) as session:
        return {"items": svc.list_outbox(session, state=state, event=event,
                                         recipient_user_id=recipient_user_id, limit=limit),
                "monitoring": svc.monitoring(session), "events": list(svc.EVENTS)}


@router.get("/api/notifications/{notification_id}",
            dependencies=[Depends(_requires("admin.reset"))])
def get_notification(notification_id: str, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _database()
    try:
        with database.session(svc.system_scope()) as session:
            return svc.get_notification(session, notification_id, actor=_actor(request))
    except svc.NotificationError as exc:
        raise _service_error(exc)


@router.post("/api/notifications/dispatch", dependencies=[Depends(_requires("admin.reset"))])
def post_dispatch(body: _DispatchIn, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    return svc.dispatch_due(_database(), limit=body.limit, actor=_actor(request))


@router.post("/api/notifications/{notification_id}/retry",
             dependencies=[Depends(_requires("admin.reset"))])
def post_retry(notification_id: str, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    database = _database()
    try:
        with database.session(svc.system_scope()) as session:
            return svc.retry_dead(session, notification_id, actor=_actor(request))
    except svc.NotificationError as exc:
        raise _service_error(exc)


# ------------------------------------------------------------ preferences
@router.get("/api/me/notification-preferences")
def get_preferences(response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    who = _principal_of(request)
    database = _database()
    with database.session(svc.system_scope()) as session:
        return {"user_id": who["user_id"],
                "preferences": svc.get_preferences(session, str(who["user_id"]))}


@router.put("/api/me/notification-preferences")
def put_preference(body: _PreferenceIn, response: Response, request: Request) -> dict[str, Any]:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    who = _principal_of(request)
    database = _database()
    try:
        with database.session(svc.system_scope()) as session:
            return svc.set_preference(session, user_id=str(who["user_id"]), event=body.event,
                                      enabled=body.enabled, actor=str(who["user_id"]))
    except svc.NotificationError as exc:
        raise _service_error(exc)


@router.get("/api/me/notifications")
def get_my_notifications(response: Response, request: Request,
                         limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """A user's own delivery history: what was sent to them, when, and what
    was suppressed by their preferences."""
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)
    who = _principal_of(request)
    database = _database()
    with database.session(svc.system_scope()) as session:
        return {"items": svc.list_outbox(session, recipient_user_id=str(who["user_id"]), limit=limit)}
