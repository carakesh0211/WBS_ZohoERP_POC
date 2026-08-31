"""Liveness and readiness -- two genuinely different questions.

``GET /healthz``  -- is the process up? **Touches no database.** Must answer
                     even when the database is completely unreachable, because
                     that is exactly the moment an orchestrator needs to tell
                     "the process crashed" apart from "the process is fine but
                     its dependency is down."

``GET /readyz``   -- is the database reachable, AND is its schema at a known
                     revision? Returns 503 when either is false. Conflating
                     this with liveness is how a broken deployment reports
                     healthy -- see ``Database.readiness()``'s docstring.

Both delegate to :class:`app.backend.pg.engine.Database`, which is frozen and
already does the hard part correctly: liveness never touches the database,
and readiness reports only an exception CLASS name on failure, never the raw
driver message -- a libpq error can echo host, user and database name. This
module adds nothing that could reopen that leak: no field from the database
config is read or returned here, and no exception is caught and restringified
in a way that could smuggle a driver message back out.

Both routes are simple SELECTs (or no database call at all) against a pooled
connection, so both answer in low milliseconds once the pool is warm, and
within a couple of seconds on a cold start -- comfortably inside the 30 s
request budget the platform allows.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..pg.engine import get_database

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Process is up. Deliberately does not import, construct or call
    anything that could touch a database -- not even to check it is
    configured. If this handler is running, the process is alive."""
    return {"status": "ok", "check": "liveness"}


@router.get("/readyz")
def readyz() -> JSONResponse:
    """Database reachable and schema current. 503 otherwise.

    "Not configured" (no ``set_database()`` call has happened yet -- the
    ordinary state of a process that has not finished, or never attempted,
    PostgreSQL start-up wiring) is reported the same honest way as "configured
    but unreachable": a 503 with an error class, never a 500 and never a
    stack trace.
    """
    try:
        database = get_database()
    except RuntimeError:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "check": "readiness",
                "error_class": "DatabaseNotConfigured",
            },
        )

    result = database.readiness()
    status_code = 200 if result.get("status") == "ok" else 503
    return JSONResponse(status_code=status_code, content=result)
