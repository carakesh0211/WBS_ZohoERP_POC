"""The scoped-query chokepoint, enforced as a build gate.

The plan makes `repo.query()` the single door through which scopable data is
read: it requires a literal `{scope}` token and raises without one. A gate is
required because the door only works if everyone uses it, and Wave 2 produced
the counter-example -- `budget.compare_versions` read `budget_version` with
three direct `session.fetchone`/`fetchall` calls keyed on a caller-supplied
`project_id`, so any authenticated caller could name any project and read its
budget comparison.

That bypass is also why this gate looks for more than the plan's original
`.execute(`: the offending calls were `session.fetchone` and
`session.fetchall`, which an `.execute(`-only walk sails straight past.

THE GATE'S OWN BLIND SPOTS, closed after an adversarial review found them
(the first version reported zero findings on `pg/masters.py`, which has
roughly fifteen raw reads of two tables the gate's own SCOPABLE set names):

  * It read only fully-literal SQL. `pg/masters.py` builds every statement as
    `f"... FROM {kind.table} ..."`, so the table name was the placeholder
    `<expr>` and matched nothing. SQL that cannot be read statically is now
    FLAGGED, not skipped -- unanalysable is not the same as safe.
  * It walked `app/backend/pg/*.py` only, so the routers -- which also issue
    raw `session.fetchall` -- were never examined at all. Both directories
    are scanned now.
  * A module with no row-level scope concept had no way to say so, so the
    honest answer was indistinguishable from an oversight. `NO_ROW_SCOPE`
    makes that a single declaration with a stated reason, in one reviewable
    place, rather than fifteen scattered comments or silence.

Runs with no database, on source alone.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
PG_DIR = BACKEND / "pg"
API_DIR = BACKEND / "api"

#: Modules that are the plumbing itself, or that operate below the scope
#: layer by design. Every name here is a deliberate opt-OUT of the
#: chokepoint, so it should be rare and obvious in review.
INFRASTRUCTURE = {
    "pg/config.py",      # no queries; secret provider boundary
    "pg/engine.py",      # defines Session.fetchall/fetchone
    "pg/repo.py",        # defines the chokepoint
    "pg/migrate_pg.py",  # DDL and the migration ledger, run as capex_migrator
    "pg/seed.py",        # demo seeding, behind profile + disposable-name guards
    "pg/locking.py",     # locks cells by primary key; the lock set IS the scope
    "pg/rls.py",         # introspects pg_policy / pg_class, not business rows
    "api/health.py",     # liveness and readiness only
}

#: The POC's SQLite-era modules, which this gate deliberately does not walk.
#:
#: They were exempt before only because nobody walked `app/backend/*.py` at
#: all -- an omission, not a decision, and indistinguishable from an oversight
#: by anyone reading the file. Written down now so the distinction is legible.
#:
#: The reason is that row-level scope is a PostgreSQL construct: it is carried
#: by `repo.query`'s `{scope}` token, by `SET LOCAL capex.*`, and by the RLS
#: policies in migrations 004 and 006. None of that exists on the SQLite path.
#: These modules are retained because the plan retains them -- `compute_ledger`
#: is the oracle the materialised cells are proved against (plan section 7.7)
#: -- and because the SQLite build stays deployable until the cutover.
#:
#: THE MOMENT ANY OF THEM IS PORTED TO POSTGRESQL, ITS ENTRY MUST GO. An entry
#: here on a module that issues `repo`-less PostgreSQL reads would suppress
#: exactly the finding this gate exists to make.
LEGACY_SQLITE = {
    "services.py": "SQLite service layer; retained until cutover",
    "domain.py": "compute_ledger, the oracle the cell path is proved against",
    "db.py": "SQLite schema and connection helper",
    "auth.py": "development identity provider on the SQLite path",
    "zoho.py": "the MOCK connector; superseded by app/backend/integration/",
    "main.py": "the SQLite FastAPI app; the PG app is app/backend/pg + api",
    "migrate.py": "the SQLite migration runner",
    "money.py": "pure arithmetic; no database access at all",
    "observability.py": "logging and metrics; no business rows",
}

#: Modules whose data carries no row-level scope dimension, with the reason.
#: These still authenticate and still check permissions -- the permission IS
#: the control for them -- but there is no per-row restriction to apply.
#:
#: This is a real design decision, not an exemption of convenience: settings
#: and master data are organisation-wide reference data in this milestone. If
#: that changes -- if an entity's vendors become visible only to that entity
#: -- these entries must go, and the reads behind them must move onto the
#: chokepoint.
NO_ROW_SCOPE = {
    "pg/masters.py": "organisation-wide reference data; permission is the control",
    "api/masters.py": "organisation-wide reference data; permission is the control",
    # api/settings.py was here, with the reason "organisation-wide reference
    # data; permission is the control". Wave 3 proved that false: the router
    # serves entity, plant and location -- three of the four scope dimensions
    # -- and the exemption was suppressing the two reads that leaked them.
    # Removed rather than re-worded. Both reads now go through repo.query with
    # a per-collection column mapping.
    "pg/roles.py": "reads and writes the grant tables that DEFINE scope",
    "api/admin_access.py": "administers the grant tables that DEFINE scope",
    "pg/audit.py": "audit rows are scoped by stream key at the API layer",
    "api/audit.py": "audit reads are scoped by the caller's own audit scope",
}

#: Tables carrying, or reachable from, a row-level scope dimension.
#:
#: THE WAVE 5 TABLES WERE MISSING FROM THIS SET UNTIL AN ADVERSARIAL REVIEW
#: PLANTED AN UNSCOPED READ ON `integration_circuit` AND THE GATE REPORTED
#: CLEAN. Commit 9985ccd widened `_modules()` to walk `app/backend/integration/`
#: precisely so that package could not escape -- and then the gate walked it
#: while being blind to its tables, which is the widening proving less than it
#: appeared to. All eight are declared scope-bearing by migration 010 itself,
#: which gives each a `capex_scope_permits(...)` RLS policy, and `job` carries
#: literal `entity_id`/`project_id` columns.
#:
#: Adding them found no live defect. That is the point: a gate is worth having
#: before something exploits the hole, not after.
SCOPABLE = {
    # Migration 010, all eight carrying a capex_scope_permits RLS policy.
    "integration_connection", "integration_inbox", "integration_outbox",
    "integration_watermark", "integration_rate_budget", "integration_event",
    "integration_circuit", "job",
    # Migration 011.
    "reconciliation_exception",
    # Migration 013, all eight carrying a capex_scope_permits RLS policy of
    # their own -- one that reaches `project` and filters all four dimensions.
    # Added with the migration rather than after it, because the Wave 5 tables
    # show what happens otherwise: the gate was widened to WALK
    # `app/backend/integration/` while still being blind to that package's
    # tables, which is a widening that proves less than it appears to.
    #
    # These are the money-bearing document rows -- `po_line.amount_paise`,
    # `grn_line.amount_paise`, `bill_line.amount_paise` -- so an unscoped read
    # of any of them leaks another entity's committed and invoiced spend. Both
    # halves of Wave 6 read them: the ledger (`pg/procurement.py`) writes the
    # receipt and bill rows, the service layer (`pg/procurement_services.py`)
    # writes the request and order rows and reads the bill rows back to derive
    # commitment. Neither package can escape this set.

    "purchase_request", "pr_line", "purchase_order", "po_line",
    "grn", "grn_line", "bill", "bill_line",
    # Migration 018, all three carrying a capex_scope_permits RLS policy that
    # reaches `project` and filters all four dimensions. Added WITH the
    # migration, not after it, for the reason the 013 block above gives.
    #
    # `capitalisation_request.cwip_balance_paise` is another entity's capital
    # position and `asset_allocation.amount_paise` is how it was split; an
    # unscoped read of either is a disclosure, and an unscoped WRITE to
    # `capitalisation_request` is a capitalisation decision recorded against a
    # project the caller cannot see.
    "project_completion_review", "capitalisation_request", "asset_allocation",
    # Migration 017. `report_saved_view` carries `entity_id text NOT NULL` and
    # a `capex_scope_permits` policy of its own. An unscoped read returns
    # another entity's saved views -- and a saved view IS a filter set, so it
    # discloses what that entity measures itself on, not merely that the view
    # exists. It was missing until the Wave 7 review; the completeness check at
    # the foot of this file now derives the requirement rather than trusting
    # the next person to remember.
    "report_saved_view",
    "entity", "division", "branch", "zone", "department", "plant", "location",
    "project", "wbs_element", "budget_control_cell", "budget_ledger_cell",
    "budget_line", "budget_revision", "budget_transfer", "budget_version",
    "item_master", "vendor_master", "accounting_period",
}

READ_METHODS = {"fetchall", "fetchone", "execute"}

_FROM_OR_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_<][a-z0-9_<>]*)", re.IGNORECASE)
_IS_SQL = re.compile(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", re.IGNORECASE)

#: The marker a non-literal f-string piece is rendered as.
UNRESOLVED = "<expr>"


def _modules() -> list[Path]:
    """Every module under `app/backend/`, minus the named exemptions.

    This walked `PG_DIR` and `API_DIR` explicitly, and that shape is what
    produced three blind spots in a row: `pg/` only (routers escaped), then
    `pg/` + `api/` (Wave 5's whole `integration/` package escaped). Naming one
    more directory each time guarantees the next package is missed the same
    way, because the list is maintained by whoever remembers.

    Walking everything inverts the burden: a module is covered unless someone
    writes down why it is not, and `test_every_exemption_names_a_real_module`
    below fails when an exemption stops matching a real file.
    """
    everything = sorted(
        path for path in BACKEND.rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith("__")
    )
    return [p for p in everything
            if _key(p) not in INFRASTRUCTURE
            and _key(p) not in LEGACY_SQLITE]


def _key(path: Path) -> str:
    """`<dir>/<file>.py`, or just `<file>.py` for a module directly in backend.

    Modules at the top of `app/backend/` have `backend` as their parent, and
    keying those as `backend/services.py` would read as though `backend` were
    a package alongside `pg` and `api`. The bare filename is what every
    exemption below already uses.
    """
    if path.parent == BACKEND:
        return path.name
    return f"{path.parent.name}/{path.name}"


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """`{name: value}` for every module- or class-level string constant.

    Only plain literal assignments are resolved -- `X = "..."`, including
    implicit concatenation across lines, which is how `outbound.py` builds its
    statement. A computed value is deliberately NOT resolved: this gate's
    contract is that unreadable SQL is reported, and a resolver that guessed
    would convert a "cannot read" into a confident wrong answer.

    Class and module scopes share one namespace here. That is imprecise in
    principle and sufficient in practice: these are SQL constants, and a name
    collision between two SQL constants in one module would be a defect of its
    own.
    """
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _sql_of(node: ast.Call, constants: dict[str, str] | None = None) -> str | None:
    """The SQL text of a call, with non-literal pieces marked `<expr>`.

    Returns None when the argument is not a string expression at all (a bare
    variable, say) -- the caller treats that as unanalysable, not as safe.
    """
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant)
            and isinstance(value.value, str) else f" {UNRESOLVED} "
            for value in first.values)

    # A named constant: `_UPSERT`, `self._UPSERT`, `Cls._UPSERT`. Resolved only
    # when the module assigns it a literal string; anything else falls through
    # to None and is reported as unreadable, which is the honest answer.
    if constants:
        if isinstance(first, ast.Name):
            return constants.get(first.id)
        if isinstance(first, ast.Attribute):
            return constants.get(first.attr)
    return None


def _findings(path: Path) -> list[str]:
    """Every scopable or unanalysable read that skips the chokepoint."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    name = path.name
    constants = _string_constants(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in READ_METHODS:
            continue

        line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if "scope-exempt:" in line_text:
            continue

        sql = _sql_of(node, constants)

        if sql is None:
            # A bare variable or a computed expression. Only flagged when the
            # call is plainly a query -- `.execute()` on a non-SQL object is
            # not this gate's business.
            if func.attr in {"fetchall", "fetchone"}:
                found.append(
                    f"{name}:{node.lineno} session.{func.attr}() with SQL this "
                    f"gate cannot read statically")
            continue

        if "{scope}" in sql:
            continue
        if not _IS_SQL.search(sql):
            continue

        tables = {t.lower() for t in _FROM_OR_JOIN.findall(sql)}
        if UNRESOLVED.lower() in tables:
            found.append(
                f"{name}:{node.lineno} session.{func.attr}() reads a table whose "
                f"name is computed, so the table cannot be identified")
        for table in sorted(tables & SCOPABLE):
            found.append(
                f"{name}:{node.lineno} reads {table!r} via session.{func.attr}() "
                f"with no {{scope}} token")
    return found


@pytest.mark.parametrize("module", _modules(), ids=_key)
def test_no_module_reads_a_scopable_table_off_the_chokepoint(module):
    """Every scopable read carries `{scope}`, is exempted with a stated
    reason, or lives in a module declared as infrastructure or as carrying no
    row-level scope."""
    if _key(module) in NO_ROW_SCOPE:
        pytest.skip(f"declared NO_ROW_SCOPE: {NO_ROW_SCOPE[_key(module)]}")
    findings = _findings(module)
    assert not findings, "\n  ".join(["scoped-query bypasses:"] + findings)


def test_the_gate_catches_a_literal_bypass(tmp_path):
    """The real defect's shape: a scopable read keyed on a caller-supplied
    id, through `session.fetchall`, with no `{scope}`."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def compare(session, project_id):\n"
        "    return session.fetchall(\n"
        '        "SELECT version_id FROM budget_version WHERE project_id = %s",\n'
        "        (project_id,))\n",
        encoding="utf-8")
    assert any("budget_version" in f for f in _findings(planted))


def test_the_gate_catches_a_computed_table_name(tmp_path):
    """The first blind spot. `FROM {kind.table}` hid roughly fifteen reads of
    two scopable tables, and the gate reported the module clean."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def rows(session, kind, where):\n"
        '    return session.fetchall(f"SELECT * FROM {kind.table} WHERE {where}")\n',
        encoding="utf-8")
    findings = _findings(planted)
    assert findings and "computed" in findings[0], (
        "a table name the gate cannot resolve must be flagged, not skipped")


def test_the_gate_catches_sql_held_in_a_variable(tmp_path):
    """The second blind spot: unanalysable is not the same as safe.

    A literal held in a constant is now RESOLVED rather than merely reported
    as unreadable, so this asserts the stronger finding: the table is named
    and the missing token is named. `outbound.py` builds its statement exactly
    this way, and reporting "cannot read" there was true but useless.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "SQL = 'SELECT * FROM budget_line'\n"
        "def rows(session):\n"
        "    return session.fetchall(SQL)\n",
        encoding="utf-8")
    findings = _findings(planted)
    assert findings, "a constant holding scopable SQL escaped the gate entirely"
    assert "budget_line" in findings[0] and "{scope}" in findings[0], (
        f"expected the resolved table and the missing token, got: {findings[0]}")


def test_sql_the_gate_still_cannot_read_is_still_reported(tmp_path):
    """Resolution must not become a licence to assume.

    Only a plain literal assignment is resolved. A COMPUTED statement stays
    unreadable, and unreadable must stay a finding -- a gate that guessed at a
    value would turn "I cannot see this" into a confident wrong answer, which
    is worse than the false positive it replaced.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def rows(session, table):\n"
        "    sql = build(table)\n"
        "    return session.fetchall(sql)\n",
        encoding="utf-8")
    findings = _findings(planted)
    assert findings and "statically" in findings[0], (
        f"computed SQL was not reported as unreadable: {findings}")


def test_an_exempt_comment_is_required_to_state_a_reason(tmp_path):
    """`# scope-exempt:` needs the colon, so the escape hatch cannot be used
    without writing down why."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def a(session):\n"
        '    return session.fetchall("SELECT 1 FROM project")  # scope-exempt\n'
        "def b(session):\n"
        '    return session.fetchall("SELECT 1 FROM project")  # scope-exempt: stated\n',
        encoding="utf-8")
    assert len(_findings(planted)) == 1


def test_the_routers_are_scanned_not_only_the_service_layer():
    """The third blind spot: `app/backend/api/` was never walked, so a raw
    read in a router was invisible to a gate whose whole purpose is finding
    raw reads."""
    scanned = {_key(p) for p in _modules()}
    assert any(k.startswith("api/") for k in scanned)
    assert "pg/budget.py" in scanned


@pytest.mark.parametrize("key", sorted(INFRASTRUCTURE | set(NO_ROW_SCOPE)))
def test_every_declared_exemption_names_a_file_that_exists(key):
    """A stale declaration exempts nothing today and silently exempts
    whatever later takes the name."""
    assert (BACKEND / key).is_file(), f"{key} is declared but does not exist"


def test_no_row_scope_declarations_each_state_a_reason():
    for key, reason in NO_ROW_SCOPE.items():
        assert reason and len(reason) > 20, (
            f"{key} opts out of row-level scope without a usable reason")


def test_scopable_covers_every_rls_table_that_names_a_dimension_column():
    """SCOPABLE is hand-maintained, and that is how `report_saved_view` went
    missing. This derives the requirement instead.

    `app.backend.pg.rls.ALL_RLS_TABLE_COLUMNS` maps every RLS-protected table
    to its dimension columns, and it already has to be right for
    `tests/test_pg_rls_coverage.py` to pass -- so a table that names a real
    column there and is absent here is a gap in THIS gate, reported here rather
    than found by a reviewer two waves later.

    All-`None` entries are deliberately not required. Those tables reach their
    dimension through a join, or are owner-scoped like `export_job`; whether
    the AST walk should cover them is a separate judgement, and a completeness
    check that forced it would be making that judgement silently.
    """
    from app.backend.pg import rls

    dimensioned = {
        table for table, columns in rls.ALL_RLS_TABLE_COLUMNS.items()
        if any(column is not None for column in columns.values())
    }
    missing = sorted(dimensioned - SCOPABLE)
    assert not missing, (
        f"these RLS tables name a dimension column of their own but are not "
        f"in SCOPABLE, so an unscoped read of them passes this gate: {missing}"
    )
