"""The scoped-query chokepoint, enforced as a build gate.

The plan makes `repo.query()` the single door through which scopable data is
read: it requires a literal `{scope}` token and raises without one. A gate is
required because the door only works if everyone uses it, and Wave 2 produced
the counter-example -- `budget.compare_versions` read `budget_version` with
three direct `session.fetchone`/`fetchall` calls keyed on a caller-supplied
`project_id`, so any authenticated caller could name any project and read its
budget comparison.

That bypass is also the reason this gate looks for more than the plan's
original `.execute(`: the offending calls were `session.fetchone` and
`session.fetchall`, which an `.execute(`-only walk sails straight past.

Runs with no database, on source alone.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PG_DIR = Path(__file__).resolve().parents[1] / "app" / "backend" / "pg"

#: Modules that are the plumbing itself, or that operate below the scope
#: layer by design. Every one of these is listed deliberately -- adding a name
#: here is how a module opts OUT of the chokepoint, so it should be rare and
#: it should be obvious in review.
INFRASTRUCTURE = {
    "config.py",      # no queries; secret provider boundary
    "engine.py",      # defines Session.fetchall/fetchone
    "repo.py",        # defines the chokepoint
    "migrate_pg.py",  # DDL and the migration ledger, run as capex_migrator
    "seed.py",        # demo seeding, guarded by profile + disposable-name checks
    "locking.py",     # locks cells by primary key; the lock set IS the scope
    "rls.py",         # introspects pg_policy / pg_class, not business rows
}

#: Tables carrying, or reachable from, a row-level scope dimension. A read of
#: one of these from a service module must go through `repo.query`.
SCOPABLE = {
    "entity", "division", "branch", "zone", "department", "plant", "location",
    "project", "wbs_element", "budget_control_cell", "budget_ledger_cell",
    "budget_line", "budget_revision", "budget_transfer", "budget_version",
    "item_master", "vendor_master", "accounting_period",
}

READ_METHODS = {"fetchall", "fetchone", "execute"}

_FROM_OR_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _service_modules() -> list[Path]:
    return sorted(p for p in PG_DIR.glob("*.py")
                  if p.name not in INFRASTRUCTURE and not p.name.startswith("__"))


def _sql_of(node: ast.Call) -> str | None:
    """The SQL text of a call, if it is a literal we can read statically.

    An f-string built from literal pieces is joined; a non-literal piece is
    represented as a placeholder, which is enough to see the FROM clause and
    the `{scope}` token -- both of which are always literal in this codebase.
    """
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        parts = []
        for value in first.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(" <expr> ")
        return "".join(parts)
    return None


def _unscoped_reads(path: Path) -> list[tuple[int, str, str]]:
    """(line, table, method) for every scopable read that skips the chokepoint."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in READ_METHODS:
            continue

        sql = _sql_of(node)
        if sql is None:
            continue
        if "{scope}" in sql:
            continue

        # An explicit, reasoned exemption on the call line.
        line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if "scope-exempt:" in line_text:
            continue

        tables = {t.lower() for t in _FROM_OR_JOIN.findall(sql)}
        for table in sorted(tables & SCOPABLE):
            findings.append((node.lineno, table, func.attr))
    return findings


@pytest.mark.parametrize("module", _service_modules(), ids=lambda p: p.name)
def test_no_service_module_reads_a_scopable_table_off_the_chokepoint(module):
    """Every scopable read carries `{scope}`, is exempted with a stated
    reason, or lives in a module listed as infrastructure."""
    findings = _unscoped_reads(module)
    assert not findings, "\n".join(
        f"  {module.name}:{line} reads {table!r} via session.{method}() with no "
        f"{{scope}} token"
        for line, table, method in findings)


def test_the_gate_actually_catches_a_bypass(tmp_path):
    """A gate nobody has seen fail is a gate nobody knows works.

    This is the shape of the real defect: a scopable read through
    `session.fetchall`, keyed on a caller-supplied id, with no `{scope}`.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def compare(session, project_id):\n"
        "    return session.fetchall(\n"
        '        "SELECT version_id FROM budget_version WHERE project_id = %s",\n'
        "        (project_id,))\n",
        encoding="utf-8")

    findings = _unscoped_reads(planted)
    assert findings, "the gate did not catch a plainly unscoped scopable read"
    assert findings[0][1] == "budget_version"
    assert findings[0][2] == "fetchall"


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

    findings = _unscoped_reads(planted)
    assert len(findings) == 1, (
        "a bare `# scope-exempt` with no reason must not silence the gate")


def test_the_infrastructure_allow_list_names_only_files_that_exist():
    """A stale name in the allow-list would silently exempt nothing -- or,
    worse, exempt a future module that happens to take the same name."""
    for name in INFRASTRUCTURE:
        assert (PG_DIR / name).is_file(), (
            f"{name} is allow-listed as infrastructure but does not exist")


def test_there_are_service_modules_to_check():
    """Guards against the whole gate silently covering nothing."""
    names = {p.name for p in _service_modules()}
    assert "budget.py" in names, (
        "budget.py must be checked; it is where the bypass this gate exists "
        "for was found")
