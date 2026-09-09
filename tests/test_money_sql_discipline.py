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

#: What an f-string's `{...}` becomes while the SQL is scanned.
#:
#: NO PARENTHESES IN IT, deliberately: the scan below matches brackets, and a
#: placeholder carrying one of its own would shift every close after it and
#: make a correct SUM look unbalanced. No `_paise` in it either, so a
#: placeholder can never be mistaken for the column it stands in for.
_INTERPOLATION = "__SQL_EXPR__"


def _sql_strings(path: Path) -> list[str]:
    """EVERY string literal in this module, f-strings reassembled.

    NOT "every SQL string passed to a call", which is what this used to be, and
    it was a hole big enough to drive the whole reporting layer through. That
    version walked `ast.Call` and read `node.args[0]`, so it saw a statement
    only when it was the FIRST POSITIONAL ARGUMENT of a call. Measured against
    the modules this file is the only guard for:

        reporting.py   19 `SUM(` in the file,  0 reachable by the old walk
        closure.py     10 `SUM(` in the file,  2 reachable
        budget.py      13 `SUM(` in the file,  9 reachable

    Reporting's branches are module-level ASSIGNMENTS (`_PO_BRANCH = f"..."`),
    and their money columns arrive through `_zeros_except(received=...)` --
    KEYWORD arguments. Neither is `args[0]` of anything, so the gate scored a
    clean pass over a file it had never read a character of.

    There is no need for the SELECT/UPDATE/INSERT filter that came with it,
    either: it dropped exactly the fragments that matter, because
    `"SUM(GREATEST(0, line.received_paise - line.billed_paise))::bigint"` is a
    complete money expression and contains no verb at all. A Python string in
    `app/backend/pg` containing `SUM(` over a `_paise` column is SQL by
    construction, wherever it is written and whatever it is passed to.

    DOCSTRINGS ARE EXCLUDED, AND THAT EXCLUSION IS NOT A CONVENIENCE. This
    codebase explains its money SQL by QUOTING it: `budget._subtree_totals`
    opens "``SUM(budget_paise)`` and ...", and `procurement` recounts a
    double-counting incident in prose that spells
    ``SUM(CASE WHEN g.is_reversal THEN -ABS(...) ...)`` while the executable
    copy fifty lines below carries its `::bigint`. Reporting prose as an
    uncast sum would make this gate fire on files with nothing wrong with
    them, and a gate that cries wolf is switched off. A docstring is any
    string that is a STATEMENT rather than a value -- module, class, function,
    and the free-standing narrative strings this codebase also uses -- so the
    test is structural and not a guess about wording.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr)
             and isinstance(node.value, (ast.Constant, ast.JoinedStr))}

    #: THE LITERAL TEXT PIECES OF AN F-STRING, WHICH MUST NEVER BE SCANNED ON
    #: THEIR OWN. `ast.walk` reaches them again as bare Constants, and each one
    #: is a statement CUT IN HALF at a `{`: `procurement.py:1773` opens
    #: `coalesce((SELECT SUM(...) FROM ` and the fragment ENDS there, at
    #: `{GRN_LINE}`, with the `coalesce(` that carries the `::bigint` still
    #: unclosed three lines later. Scanned as a fragment it is an uncast sum;
    #: scanned as the reassembled string it is correctly cast, which it is.
    #: Two of these were reported before this exclusion existed, in a file with
    #: nothing wrong with it.
    #:
    #: `values` MEMBERS ONLY, and not every Constant beneath the f-string. A
    #: `{...}` may contain a whole call whose arguments are money SQL --
    #: `reporting.py`'s branches are built from
    #: `_zeros_except(ordered="SUM(line.ordered_paise)::bigint", ...)`, inside
    #: the f-string's braces -- and those Constants are complete, self-standing
    #: expressions that this gate exists to check.
    fragments = {id(part) for node in ast.walk(tree)
                 if isinstance(node, ast.JoinedStr)
                 for part in node.values if isinstance(part, ast.Constant)}

    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in prose or id(node) in fragments:
            continue
        if isinstance(node, ast.JoinedStr):
            # Reassembled with a placeholder per `{...}`, so the brackets of
            # the surrounding SQL stay balanced and matchable.
            found.append("".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str) else _INTERPOLATION
                for part in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def _paren_match(text: str, open_index: int) -> int:
    """The index of the `)` closing the `(` at `open_index`, or -1."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _uncast_paise_sums(path: Path) -> list[str]:
    """Every `SUM(...)` over a paise column that reaches Python uncast.

    A PAREN-MATCHING SCAN AND NOT A REGEX, and the regex it replaces had a
    blind spot with two live shapes sitting in it. It was

        SUM\\s*\\(\\s*[^()]*_paise[^()]*\\)

    -- a character class that CANNOT CROSS A NESTED `(`. So it never matched,
    and therefore never checked, either of the two real forms in this
    codebase:

        COALESCE(SUM(COALESCE(local_paise, source_paise, 0)), 0)   closure:235
        SUM(GREATEST(0, received_paise - billed_paise))            reporting:808

    Fed both with `::bigint` deleted, the old gate reported CLEAN. Nothing was
    wrong with either statement -- every Wave 7 sum is correctly cast today --
    which is worse rather than better: the guard was passing on files whose
    regressions it could not have caught, and passing is the only signal it
    emits. `tests/test_pg_reporting_*.py` and the sibling suites all run
    without a server; this one is the only check standing between a dropped
    `::bigint` and a `Decimal` in the availability verdict.

    THE CAST NEED NOT SIT ON THE `SUM` ITSELF. `SUM` over `bigint` returns
    `numeric`, `COALESCE(numeric, 0)` is still `numeric`, and a `::bigint` on
    the COALESCE makes the value psycopg receives an `int` all the same. The
    modelled implementation is `_uncast_sums` in
    `tests/test_reporting_filterset.py`, which learned this the same way: the
    SUM's own close is checked first, then every bracket enclosing it, outward.
    An uncast sum inside an uncast wrapper has no `::bigint` at any level and
    is still reported.

    A SUM WHOSE BRACKETS DO NOT CLOSE IS REPORTED, NOT SKIPPED. This scan can
    only be run over a string it can parse, and "I could not tell" is not the
    same answer as "it is cast" -- reporting it makes an unparseable shape
    visible rather than silently exempt.
    """
    offenders: list[str] = []
    for sql in _sql_strings(path):
        if "_paise" not in sql or "SUM" not in sql.upper():
            continue
        flat = re.sub(r"\s+", " ", sql)

        # Every open bracket -> the brackets enclosing it, innermost last.
        enclosing: dict[int, tuple[int, ...]] = {}
        stack: list[int] = []
        for index, character in enumerate(flat):
            if character == "(":
                enclosing[index] = tuple(stack)
                stack.append(index)
            elif character == ")" and stack:
                stack.pop()

        def cast_after(open_index: int, text: str = flat) -> bool:
            close = _paren_match(text, open_index)
            return close != -1 and text[close + 1:].startswith("::bigint")

        for match in re.finditer(r"\bSUM\s*\(", flat, re.IGNORECASE):
            open_index = match.end() - 1
            close = _paren_match(flat, open_index)
            call = flat[match.start():close + 1] if close != -1 \
                else flat[match.start():match.start() + 80] + " <unbalanced>"
            if "_paise" not in call:
                continue            # a SUM over something that is not money
            if close == -1:
                offenders.append(call)
                continue
            if cast_after(open_index):
                continue
            if any(cast_after(outer) for outer in enclosing.get(open_index, ())):
                continue
            offenders.append(call)
    return sorted(set(offenders))


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


#: The two REAL shapes the replaced regex could not see, verbatim from the
#: modules this file guards. `[^()]*` cannot cross a nested `(`, so it never
#: matched either one -- and never matched means never checked. Fed both with
#: the cast deleted, the old gate reported CLEAN.
_NESTED_SHAPES = {
    # app/backend/pg/closure.py:235, `_open_exceptions`
    "coalesce_wrapping_sum":
        "COALESCE(SUM(COALESCE(local_paise, source_paise, 0)), 0)",
    # app/backend/pg/reporting.py:808, `_PO_BRANCH`
    "sum_over_greatest":
        "SUM(GREATEST(0, line.received_paise - line.billed_paise))",
}


@pytest.mark.parametrize("shape", sorted(_NESTED_SHAPES),
                         ids=sorted(_NESTED_SHAPES))
def test_the_gate_catches_the_nested_paren_shapes_the_regex_could_not_see(
        tmp_path, shape):
    """THE BLIND SPOT, PLANTED, IN BOTH DIRECTIONS.

    There was never a live defect here -- every Wave 7 sum is correctly cast
    today, which is exactly why nobody noticed. A guard that cannot fail on
    the shapes its files are written in reports the same "clean" whether or
    not those files are clean, and this file is the ONLY guard those two
    modules have for the `::bigint` rule.

    Cast, then uncast, in one test: a checker that learned to look outward and
    forgot how to fail would be worse than the regex it replaced.
    """
    expression = _NESTED_SHAPES[shape]
    planted = tmp_path / "planted.py"

    planted.write_text(
        f'SQL = """SELECT {expression}::bigint AS total FROM t"""\n',
        encoding="utf-8")
    assert not _uncast_paise_sums(planted), (
        f"{shape}: the cast form must be accepted -- the `::bigint` sits on "
        f"the outermost expression, and a SUM whose enclosing bracket carries "
        f"the cast reaches psycopg as an int all the same")

    planted.write_text(
        f'SQL = """SELECT {expression} AS total FROM t"""\n', encoding="utf-8")
    assert _uncast_paise_sums(planted), (
        f"{shape}: an uncast SUM in this shape went unnoticed. This is the "
        f"regression the replaced regex could not have caught: `[^()]*` "
        f"cannot cross the nested `(`, so it never matched this call at all.")


def test_the_gate_reads_a_module_level_assignment_and_not_only_a_call_argument():
    """`reporting.py` holds every one of its money branches in a module-level
    f-string assignment, and the replaced extraction read only `args[0]` of a
    call -- so it scored a clean pass over a file it had read no character of.
    Zero of that module's SUMs were reachable; the sibling numbers were 2 of 10
    for `closure.py` and 9 of 13 for `budget.py`."""
    reporting = PG_DIR / "reporting.py"
    strings = " ".join(_sql_strings(reporting))
    assert "_PO_BRANCH" not in strings          # the name, not the value
    assert "SUM(GREATEST(0, line.received_paise" in re.sub(r"\s+", " ", strings), (
        "the PO branch's residual sum must be reachable by this gate; it lives "
        "in a module-level assignment, which is where reporting.py keeps all "
        "five of its branches")
    assert "SUM(r.amount_paise)" in re.sub(r"\s+", " ", strings), (
        "the reservation branch's sum arrives as a KEYWORD argument to "
        "`_zeros_except`, inside an f-string's braces -- neither `args[0]` nor "
        "a literal text fragment, and it must still be read")


def test_prose_that_quotes_money_sql_is_not_reported_but_the_code_beside_it_is(
        tmp_path):
    """This codebase explains its SQL BY QUOTING IT. `budget._subtree_totals`
    opens with ``SUM(budget_paise)``; `procurement` recounts a double-counting
    incident by spelling ``SUM(source_paise)`` out in prose while the
    executable copy below carries its cast. Reporting those would make the gate
    fire on modules with nothing wrong with them, and a gate that cries wolf is
    a gate somebody switches off.

    THE SECOND HALF IS THE POINT: the exclusion is for STATEMENTS, not for
    strings that look explanatory, so a real uncast sum in the same function
    is still reported. An exclusion that swallowed its neighbours would be a
    hole rather than a filter."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        'def totals(session):\n'
        '    """Returns ``SUM(budget_paise)`` over the subtree."""\n'
        '    return session.fetchone("SELECT SUM(bc.budget_paise)::bigint FROM c bc")\n',
        encoding="utf-8")
    assert not _uncast_paise_sums(planted), (
        "the docstring quotes an uncast sum and the statement below it is cast")

    planted.write_text(
        'def totals(session):\n'
        '    """Returns ``SUM(budget_paise)`` over the subtree."""\n'
        '    return session.fetchone("SELECT SUM(bc.budget_paise) FROM c bc")\n',
        encoding="utf-8")
    assert _uncast_paise_sums(planted) == ["SUM(bc.budget_paise)"], (
        "excluding the docstring must not excuse the statement beside it")


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
