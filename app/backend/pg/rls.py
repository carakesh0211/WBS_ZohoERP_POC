"""Row-level security: the backstop, not the primary control.

`app.backend.pg.repo.compile_scope` is the primary enforcement of row-level
scope -- see that module's docstring. The policies created in
`migrations/pg/004_identity_scope.sql` exist to catch a developer who forgets
to route a query through `repo.query()`, or who maps a table's scope columns
wrong. A backstop that has never been proven to agree with the control it
backs up is not a backstop, just a second guess -- so this module also hosts
a pure-Python mirror of the SQL predicate (`permits`), so a test can compute
"what should be visible" without touching the database and then check that
number against both `repo.compile_scope` and a live, RLS-enforced query.

**Why RLS needs a role that is not the connecting superuser.** Superusers,
and a table's own owner (absent `FORCE ROW LEVEL SECURITY`, and even then
only for the owner-vs-force interaction), bypass row-level security
unconditionally. The CI service container's admin role (`POSTGRES_USER=capex`
in `.github/workflows/ci.yml`) is created as a Postgres superuser by the
official `postgres` image, and every fixture in `tests/conftest_pg.py`
connects as that role. RLS is therefore only observable in a session that has
`SET LOCAL ROLE capex_app` -- the non-superuser role
`migrations/pg/004_identity_scope.sql` provisions for exactly this purpose.
:func:`assume_scoped_role` does that switch plus the same `SET LOCAL` scope
application `engine.Database._apply_scope` performs, so a raw test connection
can be put into the same session state a real scoped request would run
under.
"""
from __future__ import annotations

import contextlib
from typing import Iterable, Iterator

import psycopg
from psycopg import sql

from .engine import SCOPE_KEYS, Scope

#: The non-superuser role RLS is enforced against. Provisioned (idempotently,
#: NOLOGIN) by migrations/pg/004_identity_scope.sql.
SCOPED_ROLE = "capex_app"

#: Table -> the four dimensions' SQL column expressions, `None` where a
#: dimension is deliberately waived for that table (mirrors
#: `repo.PROJECT_SCOPE_COLUMNS` / `repo.WBS_ELEMENT_SCOPE_COLUMNS` exactly for
#: `project` and `wbs_element` -- those two are reused from `repo.py`
#: directly in the RLS-vs-compiler agreement tests, this registry exists so
#: every OTHER RLS-protected table has an equally explicit, reviewable
#: mapping, and so a test can enumerate "every table RLS should be enabled
#: on" in one place). `budget_control_cell` / `budget_ledger_cell` have no
#: columns of their own for any dimension; their predicate (in the migration)
#: reaches `project_id` through a join to `wbs_element`, matching
#: `wbs_element`'s own project-only waiver one join further out.
RLS_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    "entity": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "division": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "branch": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "zone": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "department": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "plant": {"entity": "entity_id", "plant": "plant_id", "location": None, "project": None},
    "location": {"entity": "entity_id", "plant": "plant_id", "location": "location_id", "project": None},
    "project": {"entity": "entity_id", "plant": "plant_id", "location": "location_id", "project": "project_id"},
    "wbs_element": {"entity": None, "plant": None, "location": None, "project": "project_id"},
    "budget_control_cell": {"entity": None, "plant": None, "location": None, "project": None},
    "budget_ledger_cell": {"entity": None, "plant": None, "location": None, "project": None},
}

#: Tables whose RLS predicate is a join through `wbs_element` rather than a
#: direct column -- `RLS_TABLE_COLUMNS` records them with every dimension
#: `None` because they carry no scope column directly; this set is what
#: distinguishes "no column, no filtering at all" (not true here) from "no
#: column, filtered via a join" (true here) for anything introspecting the
#: registry.
JOINED_VIA_WBS_ELEMENT = frozenset({"budget_control_cell", "budget_ledger_cell"})

#: Every table `migrations/pg/004_identity_scope.sql` enables RLS on.
RLS_TABLES: tuple[str, ...] = tuple(RLS_TABLE_COLUMNS)


def _dimension_permits(values: frozenset[str] | None, value: str | None) -> bool:
    """Pure-Python mirror of the SQL `capex_dimension_permits` function."""
    if value is None:
        return True  # column not applicable to this row shape -- waived
    if values is None:
        return True  # Scope.<dim>=None -- unrestricted
    return value in values  # empty frozenset -> always False, by construction


def permits(scope: Scope, *, entity_id: str | None = None, plant_id: str | None = None,
            location_id: str | None = None, project_id: str | None = None) -> bool:
    """Would `scope` see a row carrying these dimension values?

    Pure Python, no database -- the same predicate
    `migrations/pg/004_identity_scope.sql`'s `capex_scope_permits` SQL
    function computes, and the same one `repo.compile_scope` compiles into a
    `WHERE` clause. Used as the independent oracle a test computes an
    expected row set from, rather than assuming either implementation is
    correct and checking the other against it.
    """
    if scope.read_all:
        return True
    return (
        _dimension_permits(scope.entity_ids, entity_id)
        and _dimension_permits(scope.plant_ids, plant_id)
        and _dimension_permits(scope.location_ids, location_id)
        and _dimension_permits(scope.project_ids, project_id)
    )


def assume_scoped_role(connection: psycopg.Connection, scope: Scope, *,
                        role: str = SCOPED_ROLE) -> None:
    """Put an already-open transaction into the same session state a real
    scoped request would run under: `SET LOCAL ROLE` to the non-superuser
    application role, then the same `SET LOCAL capex.*` settings
    `engine.Database._apply_scope` applies on every `Database.session()`.

    Both `SET LOCAL`, both transaction-scoped, both reverted by the caller's
    own `rollback()`/`commit()` -- never a plain `SET`. See
    `test_no_bare_set_statement_in_pg_backend`-style guards: this module is
    part of `app/backend/pg/`, so its own SQL text is scanned too.
    """
    connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    for key, value in scope.as_settings().items():
        connection.execute(
            sql.SQL("SET LOCAL {} = {}").format(
                sql.Identifier(*key.split(".")), sql.Literal(value)))


def current_scope_settings(connection: psycopg.Connection) -> dict[str, str]:
    """The live `capex.*` session settings, for asserting they are empty
    (never leaked from a prior borrower) before a new scope is applied."""
    out: dict[str, str] = {}
    for key in SCOPE_KEYS:
        row = connection.execute("SELECT current_setting(%s, true)", (key,)).fetchone()
        out[key] = row[0] if row and row[0] is not None else ""
    return out


@contextlib.contextmanager
def scoped_transaction(connection: psycopg.Connection, scope: Scope, *,
                        role: str = SCOPED_ROLE) -> Iterator[psycopg.Connection]:
    """A transaction with `role`/`scope` applied via `assume_scoped_role`,
    always rolled back on exit -- this is a READ-side test helper, never a
    substitute for `Database.session()`, which commits. Rolling back keeps
    RLS-exercising test queries from needing their own fixture cleanup and
    guarantees the role/scope session state cannot leak to the next use of
    `connection`, matching the "scope never survives a pooled connection"
    guarantee `engine.py` states for the real pool.
    """
    try:
        assume_scoped_role(connection, scope, role=role)
        yield connection
    finally:
        connection.rollback()


def fetch_rls_status(connection: psycopg.Connection,
                      tables: Iterable[str] = RLS_TABLES) -> dict[str, dict[str, bool]]:
    """`{table: {"enabled": bool, "forced": bool}}` read from `pg_class`, for
    asserting every table in `RLS_TABLES` actually has RLS on -- a policy
    that exists but was never enabled by an `ALTER TABLE ... ENABLE ROW
    LEVEL SECURITY` is silently inert."""
    out: dict[str, dict[str, bool]] = {}
    for table in tables:
        row = connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = %s AND relnamespace = "
            "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())",
            (table,)).fetchone()
        out[table] = {
            "enabled": bool(row[0]) if row else False,
            "forced": bool(row[1]) if row else False,
        }
    return out


def fetch_policy_names(connection: psycopg.Connection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT policyname FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = %s ORDER BY policyname",
        (table,)).fetchall()
    return [row[0] for row in rows]
