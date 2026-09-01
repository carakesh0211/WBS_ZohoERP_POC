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
SCOPABLE = {
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
    everything = sorted(PG_DIR.glob("*.py")) + sorted(API_DIR.glob("*.py"))
    return [p for p in everything
            if _key(p) not in INFRASTRUCTURE and not p.name.startswith("__")]


def _key(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def _sql_of(node: ast.Call) -> str | None:
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
    return None


def _findings(path: Path) -> list[str]:
    """Every scopable or unanalysable read that skips the chokepoint."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    name = path.name

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

        sql = _sql_of(node)

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
    """The second blind spot: unanalysable is not the same as safe."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "SQL = 'SELECT * FROM budget_line'\n"
        "def rows(session):\n"
        "    return session.fetchall(SQL)\n",
        encoding="utf-8")
    findings = _findings(planted)
    assert findings and "statically" in findings[0]


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
