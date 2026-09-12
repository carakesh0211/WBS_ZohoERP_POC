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

#: The four row-level scope dimensions, in the order their settings are
#: rendered. `roles.DIMENSIONS` and `repo._DIMENSION_FIELDS` name the same
#: four; this is the definition the wire format is generated from.
SCOPE_DIMENSIONS: tuple[str, ...] = ("entity", "plant", "project", "location")

#: dimension -> the `Scope` field carrying it.
_DIMENSION_FIELDS: dict[str, str] = {d: f"{d}_ids" for d in SCOPE_DIMENSIONS}

#: Session variables carrying row-level scope. Read by RLS policies and by the
#: repository's predicate compiler. Always SET LOCAL, never SET.
#:
#: Mode and identity are SEPARATE settings (docs/WAVE3_CONTRACTS.md, contract
#: 1). Before this wave a single `capex.<d>_ids` setting carried both: the
#: literal string `*` meant "unrestricted". That made a grant whose
#: `scope_value` was literally `*` indistinguishable from no restriction at
#: all, and the two enforcement layers disagreed about it -- `compile_scope`
#: read it as an id matching nothing, RLS read it as a wildcard matching
#: everything. Splitting the mode out means **no value an id can take means
#: unrestricted**, so the ambiguity cannot be expressed, let alone exploited.
SCOPE_KEYS = (
    "capex.user_id",
    "capex.principal_kind",
    *(f"capex.{d}_{suffix}" for d in SCOPE_DIMENSIONS for suffix in ("mode", "ids")),
    "capex.read_all",
    # Read by `capex_may_triage_unattributed()` (migration 012). It is NOT a
    # dimension and deliberately not folded into `read_all`: it answers "may
    # this principal see rows that could not be attributed to an entity yet",
    # which is a far narrower question than unrestricted read over the estate.
    #
    # `reconciliation_exception.entity_id` is nullable by design -- an
    # unsanctioned commitment is found on a PO we hold no record of -- and
    # `capex_dimension_permits` returns TRUE for a NULL row value, because it
    # is built to waive a dimension the TABLE lacks. A nullable COLUMN is a
    # different thing, and without this flag every principal could read every
    # unattributed discrepancy and the exact paise involved.
    "capex.triage_unattributed",
)

#: The three legal values of `capex.<d>_mode`. Anything else -- including an
#: absent setting -- denies, in SQL and in Python alike.
SCOPE_MODE_ALL = "all"
SCOPE_MODE_NONE = "none"
SCOPE_MODE_LIST = "list"

#: The historical wildcard. Never a legal id anywhere: not in a grant row, not
#: in a `Scope`, not on the wire.
SENTINEL_SCOPE_VALUE = "*"


class InvalidScopeValue(ValueError):
    """A scope id was one no id may ever be.

    Four rejected shapes, each because some layer would MISREAD the value
    rather than simply not match it:

    * ``'*'``   -- the pre-Wave-3 wildcard. A database restored from before
      this wave, or a caller reaching past `roles.set_scope`, could still
      carry one; refusing it here means it can never be revived as a
      wildcard by a future reader.
    * empty, or whitespace only -- indistinguishable from "no ids at all"
      once rendered into a comma-joined list.
    * containing a comma -- would split into two ids on the wire, so one
      grant would silently become two.
    * not a `str` -- renders by `repr`/`str` into something nobody granted.

    Raised, never dropped: silently discarding a grant value the caller
    asked for would change access without telling anyone.
    """


def validate_scope_value(value: object, *, dimension: str | None = None) -> str:
    """Return `value` unchanged, or raise `InvalidScopeValue`.

    One predicate, enforced at three independent boundaries (`roles.set_scope`
    on the write path, `repo.compile_scope` at the repository boundary, and a
    CHECK constraint in `migrations/pg/007_scope_sentinel.sql`). One
    definition so the three cannot drift; three call sites so deleting any one
    of them still leaves the value refused.
    """
    where = f" for dimension {dimension!r}" if dimension else ""
    if not isinstance(value, str):
        raise InvalidScopeValue(
            f"scope value{where} must be a string, got {type(value).__name__}: "
            f"{value!r}")
    if value == SENTINEL_SCOPE_VALUE:
        raise InvalidScopeValue(
            f"scope value{where} may not be the literal {SENTINEL_SCOPE_VALUE!r}. "
            f"It was the pre-Wave-3 wildcard meaning 'unrestricted', and the "
            f"two enforcement layers disagreed about what it meant. To grant "
            f"an unrestricted dimension, pass None for that dimension (no "
            f"restriction row) -- never a wildcard id.")
    if not value.strip():
        raise InvalidScopeValue(
            f"scope value{where} may not be empty or whitespace-only "
            f"({value!r}); it is indistinguishable from no id at all once "
            f"rendered onto the wire. To restrict a dimension to nothing, "
            f"pass an empty list, not a blank id.")
    if "," in value:
        raise InvalidScopeValue(
            f"scope value{where} may not contain a comma ({value!r}); "
            f"`capex.{dimension or '<d>'}_ids` is comma-joined, so one grant "
            f"would split into two on the wire.")
    return value


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

    #: May this principal see reconciliation exceptions that could not be
    #: attributed to an entity yet?
    #:
    #: Its own flag, deliberately NOT folded into `read_all`. `read_all` is
    #: documented as "for migrations and start-up checks only, never for a
    #: request", so reusing it would make the only route to triage an
    #: exception an unrestricted read over the whole estate -- a far larger
    #: grant than "may look at the rows nobody could attribute yet".
    #:
    #: Defaults to False, which is the safe direction: a scope constructed
    #: without thinking about triage does not get it.
    triage_unattributed: bool = False

    @classmethod
    def system(cls, user_id: str = "SVC-SYSTEM") -> "Scope":
        """For migrations and start-up checks only. Never for a request."""
        return cls(user_id=user_id, principal_kind="SERVICE", read_all=True)

    def as_settings(self) -> dict[str, str]:
        """Render this scope into the frozen session-setting wire format.

        Per dimension ``d`` two settings are emitted, per
        `docs/WAVE3_CONTRACTS.md` contract 1:

        ==================  ==============  ==================
        `Scope` field       ``<d>_mode``    ``<d>_ids``
        ==================  ==============  ==================
        ``None``            ``'all'``       ``''``
        ``frozenset()``     ``'none'``      ``''``
        ``frozenset({..})`` ``'list'``      sorted, comma-joined
        ==================  ==============  ==================

        The ids setting is meaningful ONLY when the mode is ``'list'``, and
        **no value it can take means "unrestricted"** -- that is the whole
        point of the split. The three states are therefore distinguishable
        end to end, and the empty set (``'none'``) can never be misread as
        the unrestricted case (``'all'``), which is the inversion this
        format exists to make unrepresentable.
        """
        settings = {
            "capex.user_id": self.user_id,
            "capex.principal_kind": self.principal_kind,
        }
        for dimension in SCOPE_DIMENSIONS:
            values: frozenset[str] | None = getattr(self, _DIMENSION_FIELDS[dimension])
            if values is None:
                mode, ids = SCOPE_MODE_ALL, ""
            elif not values:
                mode, ids = SCOPE_MODE_NONE, ""
            else:
                mode, ids = SCOPE_MODE_LIST, ",".join(sorted(values))
            settings[f"capex.{dimension}_mode"] = mode
            settings[f"capex.{dimension}_ids"] = ids
        settings["capex.read_all"] = "true" if self.read_all else "false"
        # Read by `capex_may_triage_unattributed()` (migration 012). Emitted
        # unconditionally as an explicit 'false' rather than omitted, so an
        # absent setting and a denied one are the same observable state --
        # `current_setting(..., true)` returns NULL for an unset key, and a
        # policy that COALESCEd that to 'true' would fail open.
        settings["capex.triage_unattributed"] = (
            "true" if self.triage_unattributed else "false")
        return settings


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

    def integration_summary(self) -> dict[str, Any]:
        """Active integration connections, as `{product: {mode: count}}`.

        A start-up-check-class read like `readiness()`, on a pooled
        connection and outside any scope: it is an AGGREGATE over the
        connection catalogue (no row, no id, no organisation, no secret --
        the table holds none) and it exists so `/api/health` can say whether
        a live tenant is connected instead of repeating a constant. The same
        honesty rule as `readiness()`: an unreachable database is reported by
        error class, never by message.
        """
        try:
            with self._pool.connection(timeout=self.config.connect_timeout) as con:
                # `integration_connection` is under FORCED row-level security
                # (010) and `capex_scope_permits` fails closed with no session
                # settings, so the count would read zero. `Scope.system()` is
                # documented for exactly this class of read -- a start-up /
                # health check, never a request -- and it is SET LOCAL, so it
                # dies with the transaction below.
                self._apply_scope(con, Scope.system("SVC-HEALTH"))
                rows = con.execute(  # scope-exempt: aggregate over the connection catalogue for the unauthenticated health probe; no row-level data leaves
                    "SELECT product, mode, count(*) FROM integration_connection "
                    "WHERE is_active GROUP BY product, mode").fetchall()
                con.rollback()
        except Exception as exc:
            return {"status": "unavailable", "error_class": type(exc).__name__}
        summary: dict[str, dict[str, int]] = {}
        for product, mode, count in rows:
            summary.setdefault(str(product), {})[str(mode)] = int(count)
        return {"status": "ok", "connections": summary}

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
