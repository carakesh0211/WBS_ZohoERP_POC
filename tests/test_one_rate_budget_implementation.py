"""There is ONE implementation of writing to `integration_rate_budget`.

WHAT WENT WRONG, AND WHY A GUARD RATHER THAN A REVIEW
=====================================================

Wave 5's seven streams were file-disjoint. That prevented edit collisions and
prevented nothing else: **three of them independently wrote SQL that reserves
calls against `integration_rate_budget`**, against a column list frozen in
`docs/WAVE5_CONTRACTS.md` that ended in a trailing `...`. Stream 2 owned the
migration and filled that ellipsis in correctly. Streams 4 and 6 had already
guessed. Two of the three statements could not execute:

* `throttle.py::_RESERVE_SQL` named `lane`, `created_by` and `updated_by`, and
  conflicted on a constraint that does not exist. **Repaired** -- it now
  delegates to `integration_store.reserve_calls` and issues no SQL at all.
* `outbound.py::PgOutboundRateBudget._UPSERT` conflicted on
  `(connection_id, window_kind, window_start)`, which is not the primary key,
  and omitted five NOT NULL columns that have no default. **Repaired** -- like
  `throttle.py`, by deletion: `PgOutboundRateBudget.reserve` now delegates to
  `integration_store.reserve_calls` and the module issues no rate-budget SQL
  at all. Its waiver has been removed, so it falls under the guard below like
  every other module.

  Worth recording, because it was the third statement's distinguishing
  feature: it could not have been repaired by fixing the column list. Its
  strategy was to add `calls` first and ask afterwards whether the total had
  passed the ceiling, and `ck_integration_rate_budget_used_within_ceiling`
  (`used <= ceiling`) means the row can never HOLD the over-reserved value
  long enough to be read back and refused. A patched column list would have
  produced a statement that still aborts the transaction on the first refusal
  -- which is why "delegate, do not patch" is the rule here rather than a
  preference.

Every unit test over all three passed throughout, because all three talked to
in-memory doubles. A double written from the same misreading as the module it
doubles will agree with that module about anything, including a statement the
server cannot parse. **SQL is a string until something executes it.**

Code review is what was supposed to catch this and did not -- twice, because
the first version of amendment A5 recorded it as "a naming mismatch that has
not bitten yet" after reading the port instead of the SQL. So this is a test.

WHAT THIS FILE DOES, AND WHAT IT DELIBERATELY DOES NOT
======================================================

It fails when any module outside `app/backend/pg/integration_store.py` contains
an INSERT or UPDATE against `integration_rate_budget`. It is a check on the
SOURCE, so it holds whether or not anything ever executes that statement --
which is the point, since the three defects above all shipped with green
suites.

It resolves module-level string constants into f-strings before matching, so
`f"INSERT INTO {INTEGRATION_RATE_BUDGET} ..."` is caught as readily as the
literal. A guard that only matched the spelled-out table name would be evaded
by the very idiom `integration_store` itself uses.

It does **not** check that the exempt module's own SQL is correct -- that is
`tests/test_integration_sql_matches_schema.py` (names, statically) and
`tests/test_pg_integration_rate_budget.py` (execution, against a live server).
Three guards, three different failure modes, and this one is only about
COUNTING implementations.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

#: The one module allowed to write this table. It was written by the author of
#: migration 010, against the columns that migration actually declares.
CANONICAL = "app/backend/pg/integration_store.py"

#: **Empty, and that is the point.** It held one entry --
#: `app/backend/integration/outbound.py`, waived for
#: `PgOutboundRateBudget._UPSERT` -- because the stream that wrote this guard
#: did not own that file.
#:
#: The waiver was deliberately built so it could not outlive the defect:
#: `test_the_waiver_has_not_outlived_the_defect` below asserted that each
#: waived file STILL contained the statement it was waived for, so repairing
#: the file failed that test and forced this entry to be deleted. That is
#: exactly what happened. `outbound.py` now issues no rate-budget SQL and
#: falls under `test_only_the_canonical_store_writes_the_rate_budget` like
#: every other module in `app/`, permanently.
#:
#: The dict stays rather than being deleted, so a future waiver has to be
#: added HERE, next to the machinery that makes waivers expire, rather than
#: invented somewhere with no expiry at all.
KNOWN_UNREPAIRED: dict[str, str] = {}

TABLE = "integration_rate_budget"


def _module_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for f-string resolution."""
    constants: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) \
                and isinstance(node.target, ast.Name):
            constants[node.target.id] = node.value.value
    return constants


def _strings(path: Path) -> list[str]:
    """Every string a module could execute, with simple constants resolved.

    Docstrings are EXCLUDED. This file, `throttle.py` and the migration all
    quote the very SQL they are about, at length; a text search would report
    the documentation of a fix as the defect it documents.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _module_constants(tree)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue) \
                        and isinstance(value.value, ast.Name):
                    parts.append(constants.get(value.value.id, " "))
                else:
                    parts.append(" ")
            found.append("".join(parts))
    return found


def _writes_the_budget(path: Path) -> list[str]:
    """The INSERT/UPDATE statements in `path` that target the budget table."""
    offenders: list[str] = []
    for text in _strings(path):
        flat = " ".join(text.split())
        upper = flat.upper()
        if TABLE not in flat:
            continue
        for verb in ("INSERT INTO", "UPDATE"):
            index = upper.find(verb)
            if index == -1:
                continue
            # The table must be what the verb targets, not merely mentioned
            # somewhere later in the same statement (a SELECT ... FROM it, for
            # instance, which is a read and is fine).
            target = flat[index + len(verb):].lstrip().split()[:1]
            if target and target[0].strip('"').rstrip(",;()") == TABLE:
                offenders.append(flat[:160])
                break
    return offenders


def _modules() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def test_only_the_canonical_store_writes_the_rate_budget():
    """A fourth implementation fails here, before it can reach a server.

    This is the requirement in one sentence: Wave 5 got three, two of which
    could not execute, and the mechanism that produced them -- disjoint files,
    a frozen column list with an ellipsis, and doubles that agreed with
    whatever their own module believed -- has not gone away.
    """
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        relative = path.relative_to(ROOT).as_posix()
        if relative == CANONICAL or relative in KNOWN_UNREPAIRED:
            continue
        statements = _writes_the_budget(path)
        if statements:
            offenders[relative] = statements

    assert not offenders, (
        "a second implementation of the rate-budget write has appeared. "
        "`integration_store.reserve_calls` is the only one: it was written by "
        "the author of migration 010, it supplies every NOT NULL column, it "
        "conflicts on the real primary key, and it is the only one executed "
        "against a live server by tests/test_pg_integration_rate_budget.py. "
        "Delegate to it rather than writing a statement that will pass every "
        "unit test in this repository and fail on the first real call:\n  "
        + "\n  ".join(f"{name}: {stmts}" for name, stmts in offenders.items()))


def test_the_canonical_store_really_does_write_it():
    """The guard above is worthless if it is guarding nothing.

    If `integration_store` stops writing the table, either the exemption is
    pointing at the wrong file or the implementation has moved -- and in both
    cases every other module's exemption-by-absence becomes vacuous.
    """
    statements = _writes_the_budget(ROOT / CANONICAL)
    assert statements, (
        f"{CANONICAL} contains no INSERT/UPDATE against {TABLE}. The "
        f"exemption above names it as the one implementation; if the "
        f"implementation moved, move the exemption.")


def test_no_waiver_remains():
    """`KNOWN_UNREPAIRED` is EMPTY on purpose: every second implementation was
    repaired, so the parametrised test below has no cases and pytest reports
    it as a skip. Asserted here so the empty allowlist is a fact, not a
    silence; the parametrised test stays for the day a waiver is added."""
    assert KNOWN_UNREPAIRED == {}


@pytest.mark.parametrize("relative", sorted(KNOWN_UNREPAIRED))
def test_the_waiver_has_not_outlived_the_defect(relative: str):
    """Each waived file must STILL contain the statement it was waived for.

    This is what stops the allowlist becoming permission. When the lead rewires
    `outbound.py` onto `integration_store.reserve_calls`, this test fails and
    the entry must be deleted -- at which point the file falls under the guard
    above like every other module, permanently.
    """
    path = ROOT / relative
    if not path.exists():                     # deleted outright; waiver is spent
        pytest.fail(f"{relative} no longer exists; remove its KNOWN_UNREPAIRED "
                    f"entry so the guard covers the rest of the tree.")
    assert _writes_the_budget(path), (
        f"{relative} no longer writes {TABLE} -- the defect it was waived for "
        f"is fixed. DELETE its KNOWN_UNREPAIRED entry now, so that a future "
        f"regression in this file is caught by "
        f"test_only_the_canonical_store_writes_the_rate_budget rather than "
        f"waved through by a stale waiver.\n\nWaiver text was: "
        + KNOWN_UNREPAIRED[relative])


def test_throttle_no_longer_writes_the_budget_at_all():
    """The specific repair this stream shipped, pinned.

    `throttle.py` used to carry `_RESERVE_SQL`, `_RELEASE_SQL` and `_READ_SQL`.
    It now issues no SQL whatever -- not even a read -- and delegates to the
    store. Named separately from the sweep above so that a regression here
    fails with the reason rather than as one entry in a list.
    """
    throttle = APP / "backend" / "integration" / "throttle.py"
    assert _writes_the_budget(throttle) == []
    assert throttle.as_posix().endswith("throttle.py")
    for text in _strings(throttle):
        upper = " ".join(text.split()).upper()
        assert not any(verb in upper for verb in
                       ("INSERT INTO", "UPDATE ", "SELECT ", "ON CONFLICT")), (
            f"throttle.py has grown SQL again: {text[:120]!r}. The reservation "
            f"belongs in integration_store.reserve_calls.")


def test_the_detector_resolves_f_strings_through_module_constants():
    """The detector's own escape hatch, closed and proved closed.

    `integration_store` writes `f"INSERT INTO {INTEGRATION_RATE_BUDGET} ..."`,
    so a guard that only matched the spelled-out table name would miss exactly
    the idiom a fourth implementation would most likely copy. This asserts the
    resolution works, on a synthetic module, rather than trusting that it does.
    """
    module = ast.parse(
        'TBL = "integration_rate_budget"\n'
        'SQL = f"INSERT INTO {TBL} (used) VALUES (1)"\n')
    assert _module_constants(module)["TBL"] == TABLE

    scratch = Path(__file__).parent / "_rate_budget_detector_probe.py"
    scratch.write_text(
        'TBL = "integration_rate_budget"\n'
        'SQL = f"INSERT INTO {TBL} (used) VALUES (1)"\n', encoding="utf-8")
    try:
        assert _writes_the_budget(scratch), (
            "the detector cannot see through an f-string built from a "
            "module-level constant, which is the exact idiom it must catch")
    finally:
        scratch.unlink()


def test_the_detector_ignores_reads_and_prose():
    """A `SELECT ... FROM` the budget is a read and must not trip the guard,
    and neither may a docstring that quotes a statement -- this file, and
    `throttle.py`, both do so at length."""
    scratch = Path(__file__).parent / "_rate_budget_detector_probe2.py"
    scratch.write_text(
        '"""INSERT INTO integration_rate_budget (used) VALUES (1)"""\n'
        'READ = "SELECT used FROM integration_rate_budget"\n'
        'OTHER = "UPDATE integration_outbox SET used = 1"\n', encoding="utf-8")
    try:
        assert _writes_the_budget(scratch) == []
    finally:
        scratch.unlink()
