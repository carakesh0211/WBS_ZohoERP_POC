"""Application start-up wiring for the PostgreSQL runtime.

This is the seam that keeps the hosting decision reversible: nothing above
this module -- not the FastAPI app, not ``app/run.py``, not a route -- knows
where the connection string came from, how the pool is shaped, or how the
schema was verified. It only calls :func:`startup` once, at process start,
and :func:`shutdown` once, at process end.

**Never a migration step.** :func:`startup` builds the config, opens the
pool, installs the process-wide :class:`~app.backend.pg.engine.Database`, and
performs a READ-ONLY schema check via
:func:`app.backend.pg.migrate_pg.assert_schema_current`. If the schema is
behind or has drifted -- or the database cannot be reached at all -- it
raises :class:`RuntimeStartupError` with the exact command an operator needs
to run. It never attempts to fix what it finds. That is DEF-01's fix at the
seam where it actually happens: the moment the process would otherwise have
tried to repair its own schema on boot.
"""
from __future__ import annotations

import logging

from . import config as config_mod
from . import migrate_pg
from .engine import Database, Scope, get_database, set_database

log = logging.getLogger("capex")

__all__ = [
    "RuntimeStartupError",
    "build_database",
    "check_schema",
    "startup",
    "shutdown",
]


class RuntimeStartupError(RuntimeError):
    """Boot refuses to proceed.

    The message is safe to print: it is built from :class:`MigrationError`
    text (which never carries a DSN) or from an exception CLASS name only,
    the same discipline :meth:`Database.readiness` uses for the same reason --
    a raw driver message can echo host, user and database.
    """


def build_database(*, open_pool: bool = True, **database_kwargs) -> Database:
    """Construct a ``Database`` from environment configuration.

    Does not check the schema and does not install it as the process-wide
    instance -- callers that want the full boot sequence should use
    :func:`startup` instead. This exists separately so a caller (a test, an
    operator script) can build one without either side effect.
    """
    cfg = config_mod.from_env()
    return Database(cfg, open_pool=open_pool, **database_kwargs)


def check_schema(database: Database, *, timeout: float | None = None) -> None:
    """Read-only. Raises :class:`RuntimeStartupError` if the schema is behind,
    has drifted, or the database cannot be reached at all. Never writes.

    Uses :meth:`Database.session` with :meth:`Scope.system`, the scope
    ``engine.py`` documents as reserved for "migrations and start-up checks
    only" -- never for a request.
    """
    connect_timeout = timeout if timeout is not None else database.config.connect_timeout
    try:
        with database.session(Scope.system()) as session:
            migrate_pg.assert_schema_current(session.connection)
    except migrate_pg.MigrationError as exc:
        log.error("refusing to start: %s", exc)
        raise RuntimeStartupError(str(exc)) from exc
    except RuntimeStartupError:
        raise
    except Exception as exc:
        # Class only -- never the raw driver message. See Database.readiness().
        log.error("refusing to start: could not reach the database to verify "
                   "its schema (%s)", type(exc).__name__)
        raise RuntimeStartupError(
            f"could not reach the database to verify its schema "
            f"({type(exc).__name__}); refusing to start. Check connectivity "
            f"and CAPEX_DB_* configuration before retrying.") from exc


def startup(*, check: bool = True) -> Database:
    """Build the process-wide ``Database``, install it, and -- by default --
    verify the schema before returning. Never migrates, never writes.

    On failure the pool is closed before the exception propagates, so a
    refused start-up does not leak a connection pool.
    """
    database = build_database()
    if check:
        try:
            check_schema(database)
        except RuntimeStartupError:
            database.close()
            raise
    set_database(database)
    log.info("database runtime ready")
    return database


def shutdown() -> None:
    """Close the process-wide pool, if one was installed. Safe to call more
    than once, and safe to call when :func:`startup` was never called."""
    try:
        database = get_database()
    except RuntimeError:
        return
    database.close()
    set_database(None)
