"""Money must leave PostgreSQL as an integer number of paise.

PostgreSQL's `SUM()` over a `bigint` column returns **numeric**, not bigint,
and psycopg maps numeric to `decimal.Decimal`. So a rollup written the obvious
way hands Python a Decimal even though every stored value is an integer.

That shipped, and it failed exactly where it hurts: `check_availability`
multiplied the Decimal by a float and raised `TypeError`, taking down the
availability verdict, both approval paths, and -- because both concurrent
workers died the same way -- the concurrency proof that exactly one of two
racing approvals succeeds. It only failed in the PostgreSQL CI job, because a
Decimal cannot appear without a real server.

A `TypeError` is the lucky version. The dangerous one is a Decimal that
silently participates in arithmetic and reaches a caller, a JSON body or a
stored column as a non-integer amount, which is precisely what the plan's
"integer paise everywhere, no float, no Decimal" rule exists to prevent.

Source-only. No database.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PG_DIR = Path(__file__).resolve().parents[1] / "app" / "backend" / "pg"

#: `SUM( ... _paise ... )`, capturing what follows so the cast can be checked.
_PAISE_SUM = re.compile(r"SUM\s*\(\s*[^()]*_paise[^()]*\)(?P<tail>.{0,40})",
                        re.IGNORECASE | re.DOTALL)


def _sql_literals(path: Path) -> list[str]:
    """Every SQL string passed to a call in this module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
        elif isinstance(first, ast.JoinedStr):
            text = "".join(v.value for v in first.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
        else:
            continue
        if re.search(r"\b(SELECT|UPDATE|INSERT)\b", text, re.IGNORECASE):
            found.append(text)
    return found


def _uncast_paise_sums(path: Path) -> list[str]:
    offenders = []
    for sql in _sql_literals(path):
        for match in _PAISE_SUM.finditer(sql):
            if "::bigint" not in match.group("tail"):
                offenders.append(match.group(0).split("\n")[0].strip())
    return offenders


@pytest.mark.parametrize(
    "module", sorted(PG_DIR.glob("*.py")), ids=lambda p: p.name)
def test_every_sum_of_a_paise_column_is_cast_back_to_bigint(module):
    """`SUM(x_paise)` must carry `::bigint`, so the driver returns an int."""
    offenders = _uncast_paise_sums(module)
    assert not offenders, (
        f"{module.name}: SUM over a paise column with no ::bigint cast -- "
        f"psycopg will return decimal.Decimal:\n  " + "\n  ".join(offenders))


def test_the_gate_catches_an_uncast_sum(tmp_path):
    """The defect's exact shape, so this is not a gate nobody has seen fail."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def totals(session):\n"
        '    return session.fetchone("SELECT COALESCE(SUM(bc.budget_paise), 0) '
        'FROM budget_control_cell bc")\n',
        encoding="utf-8")
    assert _uncast_paise_sums(planted), "an uncast SUM over paise went unnoticed"


def test_the_gate_accepts_the_cast_form(tmp_path):
    """A gate that flagged the fix too would just be noise."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def totals(session):\n"
        '    return session.fetchone("SELECT COALESCE(SUM(bc.budget_paise), 0)::bigint '
        'FROM budget_control_cell bc")\n',
        encoding="utf-8")
    assert not _uncast_paise_sums(planted)


def test_no_pg_module_multiplies_or_divides_by_a_float_literal_in_money_code():
    """A float literal next to paise arithmetic is how a rounded rupee value
    gets back into a system that stores integers.

    Utilisation percentage is the one legitimate float, and it appears twice:
    `budget.check_availability` and `reporting.derive`. Both are a display and
    threshold value, never a monetary amount, both are computed from an integer
    numerator and an integer denominator, and both are `domain._derive`'s
    expression transcribed verbatim -- which is the rule the reporting layer is
    held to, so rewriting the arithmetic to dodge this gate would break a
    stronger guarantee than it satisfies.

    THE ALLOW-LIST IS PER (MODULE, LITERAL) AND STAYS THAT WAY. It admits
    `100.0` in two named files; every other float literal in every other
    `pg/` module, and any other float in these two, still fails. Widening it to
    a bare literal or to the whole package would be the weakening this gate
    exists to prevent.
    """
    allowed = {("budget.py", "100.0"), ("reporting.py", "100.0")}
    offenders = []
    for module in sorted(PG_DIR.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            if not isinstance(node.op, (ast.Mult, ast.Div)):
                continue
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, float):
                    if (module.name, repr(side.value)) in allowed:
                        continue
                    offenders.append(
                        f"{module.name}:{node.lineno} {lines[node.lineno - 1].strip()}")
    assert not offenders, "float literal in money arithmetic:\n  " + "\n  ".join(offenders)
