"""Row-level security: the backstop, not the primary control.

`app.backend.pg.repo.compile_scope` is the primary enforcement of row-level
scope -- see that module's docstring. The policies created in
`migrations/pg/004_identity_scope.sql` and
`migrations/pg/006_rls_coverage.sql` exist to catch a developer who forgets
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

**Two registries, and the third source that makes them meaningful.**
`RLS_TABLE_COLUMNS` (004) and `RLS_COVERAGE_TABLE_COLUMNS` (006) are both
*derived from the migrations* -- transcriptions of what those files do. A test
that checks one against the other checks a migration against a restatement of
itself, which cannot detect a table missing from BOTH. That is precisely how
eight scope-carrying tables went unprotected through Wave 2 with a green
suite. :mod:`app.backend.pg.scope_inventory` is the independent,
hand-maintained answer to "which tables must carry RLS at all", written from
the schema rather than the policies; `tests/test_pg_rls_coverage.py`
cross-checks the two sources against each other.
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
JOINED_VIA_WBS_ELEMENT = frozenset({
    "budget_control_cell", "budget_ledger_cell",
    # Added by migrations/pg/006_rls_coverage.sql -- same wbs_id ->
    # wbs_element.project_id reach as the two cell tables above.
    "budget_line", "budget_revision", "budget_transfer", "budget_version_cell",
})

#: Every table `migrations/pg/004_identity_scope.sql` enables RLS on.
#:
#: Deliberately still the ELEVEN tables 004 covers, not all nineteen: the name
#: says "004", `tests/test_pg_rls.py` reads it against 004's source text alone,
#: and 006's tables live in `RLS_COVERAGE_TABLE_COLUMNS` below. Use
#: :data:`ALL_RLS_TABLES` for "every RLS-protected table, whichever migration
#: provided it".
RLS_TABLES: tuple[str, ...] = tuple(RLS_TABLE_COLUMNS)

#: Table -> dimension column mapping for the eight tables
#: `migrations/pg/006_rls_coverage.sql` adds, in the same shape as
#: `RLS_TABLE_COLUMNS`.
#:
#: `budget_line`, `budget_revision`, `budget_transfer` and
#: `budget_version_cell` map every dimension to `None` for the same reason the
#: two cell tables do: they carry no dimension column of their own and reach
#: `project_id` through a join (see `JOINED_VIA_WBS_ELEMENT`, and
#: `JOINED_VIA_BUDGET_VERSION` / `TWO_LEGGED` below for the two shapes a plain
#: single-join registry entry cannot express).
#:
#: `item_master` and `vendor_master` are ORGANISATION-WIDE reference data --
#: no dimension column, and no join to one. Their policy is not a scope
#: predicate at all but `capex_principal_present()`, mirrored here by
#: :func:`reference_permits`. They are recorded with every dimension `None`
#: and listed in :data:`REFERENCE_TABLES`, which is what distinguishes
#: "unfiltered because organisation-wide" from "unfiltered because someone
#: forgot".
RLS_COVERAGE_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    "accounting_period": {"entity": "entity_id", "plant": None, "location": None, "project": None},
    "budget_line": {"entity": None, "plant": None, "location": None, "project": None},
    "budget_revision": {"entity": None, "plant": None, "location": None, "project": None},
    "budget_transfer": {"entity": None, "plant": None, "location": None, "project": None},
    "budget_version": {"entity": None, "plant": None, "location": None, "project": "project_id"},
    "budget_version_cell": {"entity": None, "plant": None, "location": None, "project": None},
    "item_master": {"entity": None, "plant": None, "location": None, "project": None},
    "vendor_master": {"entity": None, "plant": None, "location": None, "project": None},
}

#: Tables whose predicate reaches `project_id` through `budget_version` rather
#: than (or, for `budget_version_cell`, in addition to) `wbs_element`.
JOINED_VIA_BUDGET_VERSION = frozenset({"budget_version_cell"})

#: Tables whose predicate names TWO cells and requires BOTH to be in scope.
#: `budget_transfer` is the only one: `AND`, never `OR` -- see the policy's
#: comment in 006 for why the wider choice leaks the far side of a
#: cross-project transfer.
TWO_LEGGED = frozenset({"budget_transfer"})

#: Organisation-wide reference tables. RLS-protected, but by
#: `capex_principal_present()` -- "a principal is established" -- not by any
#: dimension predicate. Mirrored by :func:`reference_permits`.
REFERENCE_TABLES = frozenset({"item_master", "vendor_master"})

#: Every table 006 enables RLS on.
RLS_COVERAGE_TABLES: tuple[str, ...] = tuple(RLS_COVERAGE_TABLE_COLUMNS)

#: Every RLS-protected table, from either migration.
ALL_RLS_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    **RLS_TABLE_COLUMNS, **RLS_COVERAGE_TABLE_COLUMNS,
}

#: Every RLS-protected table, from either migration, in registry order.
ALL_RLS_TABLES: tuple[str, ...] = tuple(ALL_RLS_TABLE_COLUMNS)

#: Which migration file provides each table's coverage. Derived from the two
#: registries above -- this module is the migration-derived side of the
#: two-source check; `app.backend.pg.scope_inventory` is the independent,
#: hand-maintained side, and `tests/test_pg_rls_coverage.py` cross-checks them.
RLS_MIGRATION_BY_TABLE: dict[str, str] = {
    **{table: "004_identity_scope.sql" for table in RLS_TABLES},
    **{table: "006_rls_coverage.sql" for table in RLS_COVERAGE_TABLES},
}


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


def transfer_permits(scope: Scope, *, from_project_id: str | None,
                     to_project_id: str | None) -> bool:
    """Mirror of `budget_transfer_scope` (006): BOTH legs must be in scope.

    `AND`, not `OR`, and the asymmetry is the whole point -- a caller who can
    see only the source project must not see that the destination project
    exists, holds budget, or received any. Either leg being `None` (a
    `from_wbs_id`/`to_wbs_id` matching no `wbs_element` row) is a DENIAL, not a
    waiver: the SQL side expresses this as `EXISTS (...)`, which is false for a
    missing row, so `None` here must not take :func:`permits`' "column waived"
    path.
    """
    if scope.read_all:
        return True
    if from_project_id is None or to_project_id is None:
        return False
    return (permits(scope, project_id=from_project_id)
            and permits(scope, project_id=to_project_id))


def version_cell_permits(scope: Scope, *, version_project_id: str | None,
                         wbs_project_id: str | None) -> bool:
    """Mirror of `budget_version_cell_scope` (006): both reach paths must
    permit the row.

    `version_project_id` comes from `version_id -> budget_version.project_id`
    (FK-enforced); `wbs_project_id` from `wbs_id -> wbs_element.project_id`
    (NOT FK-enforced -- 003_budget_planning.sql declares no foreign key on
    that column). As with :func:`transfer_permits`, a `None` on either side
    means the joined row does not exist and the SQL `EXISTS` is false, so it
    denies rather than waiving.
    """
    if scope.read_all:
        return True
    if version_project_id is None or wbs_project_id is None:
        return False
    return (permits(scope, project_id=version_project_id)
            and permits(scope, project_id=wbs_project_id))


def reference_permits(scope: Scope) -> bool:
    """Mirror of `capex_principal_present()` (006), the predicate protecting
    the organisation-wide reference tables in :data:`REFERENCE_TABLES`.

    `item_master` and `vendor_master` carry no scope dimension and reach none,
    so the only line RLS can draw is between a session with an established
    principal and one with no scope applied at all. Restrictive dimension
    grants do NOT narrow these tables -- an item is an item estate-wide -- but
    an unscoped connection still reads nothing, which is what stops the two
    tables from being effectively unprotected.

    The SQL side reads `capex.user_id`, which `Scope.as_settings()` always
    emits; an empty `user_id` is the Python-side equivalent of the setting
    being absent.
    """
    return bool(scope.read_all or scope.user_id)


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
                      tables: Iterable[str] = ALL_RLS_TABLES) -> dict[str, dict[str, bool]]:
    """`{table: {"enabled": bool, "forced": bool}}` read from `pg_class`, for
    asserting every table in `ALL_RLS_TABLES` actually has RLS on -- a policy
    that exists but was never enabled by an `ALTER TABLE ... ENABLE ROW
    LEVEL SECURITY` is silently inert.

    The default widened from `RLS_TABLES` (004's eleven) to `ALL_RLS_TABLES`
    (all nineteen) when 006 landed. Callers asserting over the default now
    cover strictly more tables than before; none cover fewer.

    `forced` is not a formality. Without `FORCE ROW LEVEL SECURITY` the
    table's OWNER -- the deploy identity that ran the migrations, which in
    production is not a superuser -- bypasses every policy on the table with
    no error and no log line.
    """
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
