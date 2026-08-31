"""Connection pool and the transactional session boundary.

Every database interaction in the production application goes through
:meth:`Database.session`. It exists to make three guarantees mechanical rather
than remembered:

1. **A transaction is always explicit.** No autocommit surprises, no half-applied
   financial mutation.
2. **Row-level scope travels with the transaction, not the connection.** Scope is
   applied with ``SET LOCAL``, which reverts on commit or rollback. Plain ``SET``
   would survive the connection's return to the pool and leak one user's scope
   into the next user's request. That is the single most dangerous mistake this
   layer can make, and `tests/test_pg_foundation.py` asserts against it.
3. **Lock ordering is observable.** The session records the order in which cell
   locks were taken, so a test can assert the ancestor-chain rule instead of
   trusting a code review.

Pool mode matters: ``SET LOCAL`` correctness depends on the transaction not
being split across backends, so a transaction-mode pooler in front of this is
fine while a statement-mode one is not. The configured mode is recorded in
:attr:`Database.pool_mode` and asserted in deployment tests.
"""
from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from .config import DatabaseConfig, SecretProvider

#: Session variables carrying row-level scope. Read by RLS policies and by the
#: repository's predicate compiler. Always SET LOCAL, never SET.
SCOPE_KEYS = (
    "capex.user_id",
    "capex.principal_kind",
    "capex.entity_ids",
    "capex.plant_ids",
    "capex.project_ids",
    "capex.location_ids",
    "capex.read_all",
)


@dataclass(frozen=True)
class Scope:
    """Who is asking, and what they are allowed to see.

    ``ALL`` is expressed as ``None`` on a collection: an explicitly unrestricted
    dimension. An **empty frozenset means "nothing"**, which is different, and
    the distinction is deliberate -- a bug that turns "no grants" into "all rows"
    is exactly the failure this type prevents.
    """

    user_id: str
    principal_kind: str = "USER"          # USER | SERVICE
    entity_ids: frozenset[str] | None = None
    plant_ids: frozenset[str] | None = None
    project_ids: frozenset[str] | None = None
    location_ids: frozenset[str] | None = None
    read_all: bool = False

    @classmethod
    def system(cls, user_id: str = "SVC-SYSTEM") -> "Scope":
        """For migrations and start-up checks only. Never for a request."""
        return cls(user_id=user_id, principal_kind="SERVICE", read_all=True)

    def as_settings(self) -> dict[str, str]:
        def render(values: frozenset[str] | None) -> str:
            return "*" if values is None else ",".join(sorted(values))

        return {
            "capex.user_id": self.user_id,
            "capex.principal_kind": self.principal_kind,
            "capex.entity_ids": render(self.entity_ids),
            "capex.plant_ids": render(self.plant_ids),
            "capex.project_ids": render(self.project_ids),
            "capex.location_ids": render(self.location_ids),
            "capex.read_all": "true" if self.read_all else "false",
        }


@dataclass
class Session:
    """One transaction, with its scope and its lock history."""

    connection: psycopg.Connection
    scope: Scope
    #: Ordered record of cell locks taken, for the ancestor-chain proof.
    locks_taken: list[tuple[str, str]] = field(default_factory=list)

    def execute(self, statement: str, params: Any = None) -> psycopg.Cursor:
        return self.connection.execute(statement, params)

    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        with self.connection.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall()

    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        with self.connection.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchone()

    def current_setting(self, key: str) -> str:
        row = self.fetchone("SELECT current_setting(%s, true)", (key,))
        return (row[0] if row and row[0] is not None else "")


class Database:
    """Owns the pool. One instance per process."""

    def __init__(self, config: DatabaseConfig, *,
                 secret_provider: SecretProvider | None = None,
                 min_size: int = 1, max_size: int = 10,
                 pool_mode: str = "session",
                 open_pool: bool = True) -> None:
        self.config = config
        self.pool_mode = pool_mode
        self._lock = threading.Lock()
        # The DSN is a secret. It is handed to the pool and not retained on self.
        self._pool = ConnectionPool(
            conninfo=config.dsn(secret_provider),
            min_size=min_size, max_size=max_size,
            open=False, kwargs={"autocommit": False},
        )
        if open_pool:
            self._pool.open(wait=True, timeout=config.connect_timeout)

    # ------------------------------------------------------------------ health
    def liveness(self) -> dict[str, Any]:
        """Process is up. Deliberately does NOT touch the database."""
        return {"status": "ok", "check": "liveness"}

    def readiness(self) -> dict[str, Any]:
        """Database is reachable and the schema is at a known revision.

        Distinct from liveness on purpose: "the process started" and "the
        process can do its job" are different questions, and conflating them is
        how a broken deployment reports healthy.
        """
        try:
            with self._pool.connection(timeout=self.config.connect_timeout) as con:
                con.execute("SELECT 1")
                row = con.execute(
                    "SELECT version, applied_at FROM schema_migrations "
                    "ORDER BY version DESC LIMIT 1").fetchone()
                con.rollback()
        except Exception as exc:
            # Class only. A libpq message can echo host, user and database.
            return {"status": "unavailable", "check": "readiness",
                    "error_class": type(exc).__name__}
        return {"status": "ok", "check": "readiness",
                "schema_version": row[0] if row else None,
                "schema_applied_at": row[1].isoformat() if row else None}

    # ------------------------------------------------------------------ session
    @contextlib.contextmanager
    def session(self, scope: Scope) -> Iterator[Session]:
        """A scoped transaction. Commits on success, rolls back on any exception."""
        with self._pool.connection() as con:
            session = Session(connection=con, scope=scope)
            try:
                self._apply_scope(con, scope)
                yield session
                con.commit()
            except Exception:
                con.rollback()
                raise

    @staticmethod
    def _apply_scope(con: psycopg.Connection, scope: Scope) -> None:
        """SET LOCAL, always.

        Transaction-scoped by definition, so it cannot survive into the next
        borrower of this pooled connection.
        """
        for key, value in scope.as_settings().items():
            con.execute(
                sql.SQL("SET LOCAL {} = {}").format(
                    sql.Identifier(*key.split(".")), sql.Literal(value)))

    def close(self) -> None:
        with self._lock:
            self._pool.close()


#: Process-wide instance, installed at start-up. Nothing imports the pool
#: directly; routes take a Database through dependency injection so tests can
#: substitute one without patching module globals.
_database: Database | None = None


def set_database(database: Database | None) -> None:
    global _database
    _database = database


def get_database() -> Database:
    if _database is None:
        raise RuntimeError(
            "database is not configured; call set_database() during start-up")
    return _database
