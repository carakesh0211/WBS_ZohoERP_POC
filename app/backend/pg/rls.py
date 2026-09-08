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

**Three registries, and the fourth source that makes them meaningful.**
`RLS_TABLE_COLUMNS` (004), `RLS_COVERAGE_TABLE_COLUMNS` (006) and
`RLS_PROCUREMENT_TABLE_COLUMNS` (013) are all
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
#: Deliberately still the ELEVEN tables 004 covers, not all twenty-seven: the
#: name says "004", `tests/test_pg_rls.py` reads it against 004's source text
#: alone, 006's tables live in `RLS_COVERAGE_TABLE_COLUMNS` below and 013's in
#: `RLS_PROCUREMENT_TABLE_COLUMNS`. Use :data:`ALL_RLS_TABLES` for "every
#: RLS-protected table, whichever migration provided it".
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

#: Tables whose predicate names TWO reach paths and requires BOTH to permit the
#: row. `AND`, never `OR`.
#:
#: `budget_transfer` (006) names two budget cells and may cross projects: `OR`
#: would disclose the far side of a cross-project transfer to a caller scoped to
#: one side -- see the policy's comment in 006.
#:
#: `bill_line` (013) is a different shape with the same answer. It reaches a
#: project through its `bill` AND through its own `wbs_id`, and nothing ties the
#: two together when `po_line_id` is NULL, because the four-column FK to
#: `po_line` is not checked at all in that case. One path alone would let a
#: non-PO bill line ride in on its bill's visibility while posting to another
#: project's control cell.
TWO_LEGGED = frozenset({"budget_transfer", "bill_line"})

#: Organisation-wide reference tables. RLS-protected, but by
#: `capex_principal_present()` -- "a principal is established" -- not by any
#: dimension predicate. Mirrored by :func:`reference_permits`.
REFERENCE_TABLES = frozenset({
    "item_master", "vendor_master",
    # 014's three rule tables. `lifecycle_state` answers "does this state permit
    # procurement", `procurement_transition` "is this state change legal",
    # `procurement_policy` "what does this deployment do with a fractional
    # ordered quantity". None of the three has an entity, plant, location or
    # project column, and none reaches one: the answers are the same
    # estate-wide by construction. The only line RLS can draw is the one
    # `capex_principal_present()` draws for the two master tables above -- a
    # session with an established principal reads them, a session with no scope
    # applied at all reads nothing.
    "lifecycle_state", "procurement_policy", "procurement_transition",
})

#: Every table 006 enables RLS on.
RLS_COVERAGE_TABLES: tuple[str, ...] = tuple(RLS_COVERAGE_TABLE_COLUMNS)

#: Table -> dimension column mapping for the eight procurement documents
#: `migrations/pg/013_procurement.sql` adds, in the same shape as the two
#: registries above.
#:
#: A SEPARATE DICT, not an extension of `RLS_COVERAGE_TABLE_COLUMNS`. That name
#: means "the eight tables 006 covers" and
#: `tests/test_pg_rls_coverage.py::test_006_covers_exactly_the_eight_tables_the_security_gate_names`
#: asserts exactly that set; folding 013's tables into it would break a passing
#: test that is right to fail, and `RLS_MIGRATION_BY_TABLE` below would then
#: attribute them to 006. `ALL_RLS_TABLE_COLUMNS` is where the three registries
#: meet, and `set(scope_inventory.covered_tables()) == set(ALL_RLS_TABLES)` --
#: the equality CI actually asserts, in both directions -- holds through it.
#:
#: EVERY DIMENSION IS `None` FOR ALL EIGHT, and that is not "nobody mapped the
#: columns" -- see :data:`JOINED_VIA_PROJECT`. Four of these tables do carry a
#: `project_id` column, and their policies still do not filter on it directly.
#: A predicate of the `capex_scope_permits(NULL, NULL, NULL, project_id)` shape
#: filters the PROJECT dimension only, and dimensions are resolved
#: independently (`principal_scope.py`, property 2): a principal restricted to
#: one entity and to no project carries `project_ids=None`, which is
#: unrestricted, so that predicate returns TRUE for every purchase order in the
#: estate. 013's policies therefore reach `project` and pass all four of its
#: dimension columns, which is why no single column of the table's own can be
#: named here.
RLS_PROCUREMENT_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    "purchase_request": {"entity": None, "plant": None, "location": None, "project": None},
    "pr_line": {"entity": None, "plant": None, "location": None, "project": None},
    "purchase_order": {"entity": None, "plant": None, "location": None, "project": None},
    "po_line": {"entity": None, "plant": None, "location": None, "project": None},
    "grn": {"entity": None, "plant": None, "location": None, "project": None},
    "grn_line": {"entity": None, "plant": None, "location": None, "project": None},
    "bill": {"entity": None, "plant": None, "location": None, "project": None},
    "bill_line": {"entity": None, "plant": None, "location": None, "project": None},
}

#: Tables whose predicate reaches ALL FOUR dimensions by joining `project`,
#: rather than filtering a dimension column of their own.
#:
#: This is the distinction `RLS_PROCUREMENT_TABLE_COLUMNS`' all-`None` rows
#: cannot make on their own: "unfiltered because someone forgot" versus
#: "filtered on entity, plant, location AND project, through a join". These are
#: filtered harder than any table in 004 or 006, not less.
JOINED_VIA_PROJECT = frozenset(RLS_PROCUREMENT_TABLE_COLUMNS) | {
    # 014. `fk_pr_reservation_pr_project` binds this table's denormalised
    # `project_id` to its purchase request's, so reaching `project` through it
    # reaches the request's project by construction -- the `pr_line` shape: one
    # join, FK-guaranteed.
    "pr_reservation",
    # 018. All three carry a denormalised `project_id` bound by foreign key to
    # a real project, and `asset_allocation` binds its `wbs_id` to the SAME
    # project by `fk_asset_allocation_wbs_project`, so reaching `project`
    # through the column reaches the row's real project by construction.
    "project_completion_review", "capitalisation_request", "asset_allocation",
}

#: Every table 013 enables RLS on.
RLS_PROCUREMENT_TABLES: tuple[str, ...] = tuple(RLS_PROCUREMENT_TABLE_COLUMNS)

#: Table -> dimension column mapping for the four tables
#: `migrations/pg/014_procurement_corrections.sql` adds.
#:
#: A SEPARATE DICT again, for the reason `RLS_PROCUREMENT_TABLE_COLUMNS` gives:
#: `RLS_MIGRATION_BY_TABLE` attributes coverage by which registry a table is
#: in, so folding these into 013's would make the inventory-vs-registry
#: agreement test report the wrong file.
#:
#: `pr_reservation` maps every dimension to `None` and is in
#: :data:`JOINED_VIA_PROJECT` -- it reaches all four through its denormalised
#: `project_id`, exactly as `pr_line` does, and for the same reason it does not
#: filter on that column directly: dimensions resolve independently, so a
#: principal restricted to one entity and to no project carries
#: `project_ids=None`, and a project-only predicate would hand them every other
#: entity's held budget.
#:
#: `lifecycle_state`, `procurement_policy` and `procurement_transition` are
#: ORGANISATION-WIDE RULE TABLES -- no dimension column, no join to one, and
#: nothing entity-specific to say: "a project in state Released permits
#: procurement" is true in every entity. They carry 006's
#: `capex_principal_present()` predicate and are listed in
#: :data:`REFERENCE_TABLES`, which is what distinguishes "unfiltered because
#: organisation-wide" from "unfiltered because someone forgot".
RLS_CORRECTION_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    "pr_reservation": {"entity": None, "plant": None, "location": None, "project": None},
    "lifecycle_state": {"entity": None, "plant": None, "location": None, "project": None},
    "procurement_policy": {"entity": None, "plant": None, "location": None, "project": None},
    "procurement_transition": {"entity": None, "plant": None, "location": None, "project": None},
}

#: Every table 014 enables RLS on.
RLS_CORRECTION_TABLES: tuple[str, ...] = tuple(RLS_CORRECTION_TABLE_COLUMNS)

#: `migrations/pg/015_reservation_grain.sql` DELIBERATELY ADDS NO ROW HERE, AND
#: THAT IS THE FACT WORTH RECORDING.
#:
#: This module's maintenance rule is that a reader must always be able to tell
#: "absent because there is nothing to register" from "absent because somebody
#: forgot", and silence cannot make that distinction. 015 changes the GRAIN of
#: `pr_reservation` -- one live hold per (request x resolved control cell)
#: instead of one per request -- and it creates NO table, NO policy and NO
#: `ENABLE`/`FORCE` statement. There is therefore no new row for any registry
#: in this module, and `RLS_CORRECTION_TABLE_COLUMNS` above still covers
#: `pr_reservation` exactly as 014 left it.
#:
#: The SCOPE REACH IS UNCHANGED, and that is a claim rather than an assumption.
#: 015 moves a reservation's `wbs_id` from the line's own cell to the
#: budget-owning ANCESTOR, and an ancestor could in principle be another
#: project's element -- which would be a scope hole, because `project_id` on
#: this table is denormalised and would then disagree with `wbs_id`. It cannot
#: be: `fk_wbs_parent_same_project` (002) makes a child's `project_id`
#: identical to its parent's for the whole chain, and
#: `fk_pr_reservation_wbs_project` (014) binds the row's `(wbs_id, project_id)`
#: pair to a real `wbs_element`. The resolved ancestor is in the same project
#: as the line by construction, so the join through `project` reaches the same
#: four dimensions it always did.
_RESERVATION_GRAIN_MIGRATION = "015_reservation_grain.sql"

#: Table -> dimension column mapping for the three closure documents
#: `migrations/pg/019_closure.sql` adds, in the same shape as the registries
#: above.
#:
#: A SEPARATE DICT again, for the reason `RLS_PROCUREMENT_TABLE_COLUMNS` gives:
#: `RLS_MIGRATION_BY_TABLE` attributes coverage by which registry a table is
#: in, so folding these into 013's would make the inventory-vs-registry
#: agreement test report the wrong file.
#:
#: EVERY DIMENSION IS `None` FOR ALL THREE, and all three are in
#: :data:`JOINED_VIA_PROJECT`. Each carries its own `project_id` column and
#: each policy still does NOT filter on it directly: a
#: `capex_scope_permits(NULL, NULL, NULL, project_id)` predicate filters the
#: PROJECT dimension only, and dimensions resolve independently
#: (`principal_scope.py`, property 2), so a principal restricted to one entity
#: and to no project carries `project_ids=None` -- unrestricted -- and would
#: read every other entity's CWIP balance and capitalisation decision. 018's
#: policies reach `project` and pass all four of its dimension columns.
RLS_CLOSURE_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    "project_completion_review": {
        "entity": None, "plant": None, "location": None, "project": None},
    "capitalisation_request": {
        "entity": None, "plant": None, "location": None, "project": None},
    "asset_allocation": {
        "entity": None, "plant": None, "location": None, "project": None},
}

#: Every table 018 enables RLS on.
RLS_CLOSURE_TABLES: tuple[str, ...] = tuple(RLS_CLOSURE_TABLE_COLUMNS)

#: Every RLS-protected table, from any of the five registries.
ALL_RLS_TABLE_COLUMNS: dict[str, dict[str, str | None]] = {
    **RLS_TABLE_COLUMNS, **RLS_COVERAGE_TABLE_COLUMNS,
    **RLS_PROCUREMENT_TABLE_COLUMNS, **RLS_CORRECTION_TABLE_COLUMNS,
    **RLS_CLOSURE_TABLE_COLUMNS,
}

#: Every RLS-protected table, from any migration, in registry order.
ALL_RLS_TABLES: tuple[str, ...] = tuple(ALL_RLS_TABLE_COLUMNS)

#: Which migration file provides each table's coverage. Derived from the three
#: registries above -- this module is the migration-derived side of the
#: two-source check; `app.backend.pg.scope_inventory` is the independent,
#: hand-maintained side, and `tests/test_pg_rls_coverage.py` cross-checks them.
RLS_MIGRATION_BY_TABLE: dict[str, str] = {
    **{table: "004_identity_scope.sql" for table in RLS_TABLES},
    **{table: "006_rls_coverage.sql" for table in RLS_COVERAGE_TABLES},
    **{table: "013_procurement.sql" for table in RLS_PROCUREMENT_TABLES},
    **{table: "014_procurement_corrections.sql"
       for table in RLS_CORRECTION_TABLES},
    **{table: "019_closure.sql" for table in RLS_CLOSURE_TABLES},
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


#: The four dimension values of a joined `project` row, in
#: `capex_scope_permits`' own argument order:
#: ``(entity_id, plant_id, location_id, project_id)``. `None` in place of the
#: whole tuple means the join found no row.
ProjectDimensions = tuple[str | None, str | None, str | None, str | None] | None


def project_join_permits(scope: Scope, dimensions: ProjectDimensions) -> bool:
    """Mirror of every 013 policy's inner predicate: reach `project`, then pass
    ALL FOUR of its dimension columns to `capex_scope_permits`.

    `dimensions` is the joined row as
    ``(entity_id, plant_id, location_id, project_id)`` -- the SAME left-to-right
    order as the SQL call, so a transposition here is as visible as one there.
    Migration 011 put `project_id` in the location slot and the project
    dimension went unenforced for a whole wave; a mirror that scrambled the
    order would agree with a broken policy and prove nothing.

    `None` means the join matched no `project` row. That DENIES, like
    :func:`transfer_permits`, and does not take :func:`permits`' "column waived
    for this row shape" path: the SQL side is `EXISTS (...)`, which is false for
    a missing row.
    """
    if scope.read_all:
        return True
    if dimensions is None:
        return False
    entity_id, plant_id, location_id, project_id = dimensions
    return permits(scope, entity_id=entity_id, plant_id=plant_id,
                   location_id=location_id, project_id=project_id)


def bill_line_permits(scope: Scope, *, bill_project: ProjectDimensions,
                      wbs_project: ProjectDimensions) -> bool:
    """Mirror of `bill_line_scope` (013): BOTH reach paths must permit the row.

    `bill_project` comes from `bill_id -> bill.project_id -> project`
    (FK-enforced); `wbs_project` from `wbs_id -> wbs_element.project_id ->
    project`. Nothing ties the two together when `po_line_id` is NULL, because
    the four-column FK to `po_line` is not checked at all in that case -- so the
    bill path alone would let a non-PO bill line ride in on its bill's
    visibility while posting to another project's control cell.

    Either side being `None` denies, for the same reason it does in
    :func:`project_join_permits`.
    """
    if scope.read_all:
        return True
    return (project_join_permits(scope, bill_project)
            and project_join_permits(scope, wbs_project))


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
    when 006 landed -- nineteen then, twenty-seven since 013 added the eight
    procurement documents. Callers asserting over the default cover strictly
    more tables each time; none cover fewer.

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
