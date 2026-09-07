"""The lock-order invariant, checked with NO database.

Wave 3, Contract 3. Two layers, both of which run everywhere -- that is the
point of the file. The defect this closes (`recompute_cell` taking a cell lock
the declared set never contained, and the period roll running with an empty
lock set) lived in code paths whose only coverage was `@pytest.mark.pg`, so it
was invisible on every developer machine and survived to integration. A skip is
not a pass; anything provable without a server is proved here.

Layer 1 -- static analysis of the service modules
    Walks the AST of `app/backend/pg/budget.py` and `app/backend/pg/periods.py`
    and checks the two rules from `locking.py`'s docstring directly against the
    code: `lock_affected_cells` is called exactly once, before any availability
    read and before any cell write, and never after either. The analyser is
    itself tested against a synthetic module that violates each rule, so a
    passing gate cannot mean "the walker found nothing".

Layer 2 -- the derived lock set
    `locking.derive_lock_set` is the pure mirror of `_LOCK_SQL`. These tests
    pin the Wave 3 change (zero-budget cells are IN the set) and the property
    that makes the deadlock-freedom proof hold (the order is total).

What is NOT provable here: that PostgreSQL actually blocks, actually locks the
zero-budget row, and actually does not deadlock. Those need a real server and
live in `tests/test_pg_period_concurrency.py`.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from app.backend.pg import locking
from app.backend.pg.locking import derive_lock_set

_PG_DIR = Path(__file__).resolve().parent.parent / "app" / "backend" / "pg"

#: The call that declares the lock set.
_LOCK = "lock_affected_cells"

#: Calls that WRITE a control/ledger cell. An UPDATE takes its target row's
#: lock implicitly, so every one of those rows must already be in the declared
#: lock set and none of these calls may ever precede `_LOCK`.
#:
#: `recompute_cell` (budget.py) issues the two `UPDATE budget_control_cell` /
#: `UPDATE budget_ledger_cell` statements that derive the BUDGET columns.
#:
#: `recompute_commitment` (procurement_services.py, Wave 6) is the second, and it is
#: not an addition of convenience: `budget_ledger_cell.commitment_paise` had NO
#: writer at all in the PostgreSQL path, so `check_availability`'s exposure
#: limb never moved and every purchase order was invisible to the next budget
#: check. It writes a ledger cell exactly as `recompute_cell` does and is held
#: to exactly the same rule.
_CELL_WRITES = frozenset({"recompute_cell", "recompute_commitment"})

#: Calls that READ availability. Availability is derived from the whole
#: ancestor chain and subtree, so reading it before the chain is locked reads a
#: value a concurrent transaction can invalidate before this one commits.
_AVAILABILITY_READS = frozenset({
    "check_availability", "_owning_ancestor", "_subtree_totals",
})

_TRACKED = frozenset({_LOCK}) | _CELL_WRITES | _AVAILABILITY_READS


# ==========================================================================
# The analyser
# ==========================================================================
def _called_name(node: ast.Call) -> str | None:
    """`f(...)` -> 'f'; `mod.f(...)` -> 'f'; anything else -> None."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_in_source_order(node: ast.AST) -> list[str]:
    """Every call inside `node`, named, in source order.

    Sorting by (lineno, col_offset) rather than relying on `ast.walk`'s
    traversal is what makes "before" and "after" mean what they say.
    """
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
    calls.sort(key=lambda c: (c.lineno, c.col_offset))
    return [name for name in (_called_name(c) for c in calls) if name is not None]


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _events(name: str, functions: dict[str, ast.FunctionDef],
            stack: tuple[str, ...] = ()) -> list[str]:
    """Ordered tracked events for `name`, expanding intra-module helpers.

    Expansion matters: `transition_period` takes no lock itself -- it delegates
    the whole cell-touching part of its work to `_roll_cells_for_entity`. An
    analyser that only looked one level deep would score it "no lock, no
    writes" and wave through a function that does both.

    Two documented simplifications, neither of which can hide a violation:
    branches are flattened (a call in an `if` counts as if it always ran) and a
    call in a loop counts once. Both make the analyser stricter, never laxer --
    a lock inside one branch and a write in another still reads as
    lock-then-write here, so the ONE genuine escape is a function that locks
    conditionally, which `test_lock_is_not_conditional` rules out separately.
    """
    if name in stack or name not in functions:
        return []
    out: list[str] = []
    for called in _calls_in_source_order(functions[name]):
        if called in _TRACKED:
            out.append(called)
        elif called in functions:
            out.extend(_events(called, functions, stack + (name,)))
    return out


def _analyse(source: str) -> dict[str, list[str]]:
    tree = ast.parse(source)
    functions = _module_functions(tree)
    return {name: _events(name, functions) for name in functions}


def _violations(events_by_function: dict[str, list[str]]) -> list[str]:
    """Every breach of Contract 3, as human-readable strings."""
    problems: list[str] = []
    for fn, events in sorted(events_by_function.items()):
        locks = [i for i, e in enumerate(events) if e == _LOCK]
        writes = [i for i, e in enumerate(events) if e in _CELL_WRITES]
        reads = [i for i, e in enumerate(events) if e in _AVAILABILITY_READS]

        if not writes and not locks:
            continue                      # touches no cell; rule does not apply

        if writes and not locks:
            problems.append(
                f"{fn}: writes a cell ({events[writes[0]]}) but never calls "
                f"{_LOCK} -- the UPDATE takes a lock the declared set does "
                f"not contain")
            continue

        if len(locks) > 1:
            problems.append(
                f"{fn}: calls {_LOCK} {len(locks)} times; it must be called "
                f"exactly once, with the complete affected set")

        first_lock = locks[0]
        if writes and writes[0] < first_lock:
            problems.append(
                f"{fn}: writes a cell before {_LOCK} (event order {events})")
        if reads and reads[0] < first_lock:
            problems.append(
                f"{fn}: reads availability before {_LOCK} (event order "
                f"{events}) -- the value can be invalidated before commit")
        last_lock = locks[-1]
        after = writes + reads
        if after and last_lock > min(after):
            problems.append(
                f"{fn}: calls {_LOCK} after a cell write or availability read "
                f"(event order {events})")
    return problems


# ==========================================================================
# Layer 1a -- the analyser detects what it claims to
# ==========================================================================
_GOOD = """
def lock_affected_cells(session, affected): ...
def recompute_cell(session, w, h): ...
def check_availability(session, w, h, amt): ...

def approve(session, w, h):
    lock_affected_cells(session, [(w, h)])
    check_availability(session, w, h, 1)
    recompute_cell(session, w, h)
"""


def test_analyser_passes_a_correct_function():
    assert _violations(_analyse(_GOOD)) == []


@pytest.mark.parametrize("body,expected_fragment", [
    # writes with no lock at all -- the `_roll_cells_for_entity` shape
    ("""
def bad(session, w, h):
    recompute_cell(session, w, h)
""", "never calls lock_affected_cells"),
    # lock taken after the write
    ("""
def bad(session, w, h):
    recompute_cell(session, w, h)
    lock_affected_cells(session, [(w, h)])
""", "before lock_affected_cells"),
    # availability read before the chain is locked
    ("""
def bad(session, w, h):
    check_availability(session, w, h, 1)
    lock_affected_cells(session, [(w, h)])
    recompute_cell(session, w, h)
""", "reads availability before"),
    # partial set, then more locks later
    ("""
def bad(session, w, h):
    lock_affected_cells(session, [(w, h)])
    lock_affected_cells(session, [(w, 'other')])
    recompute_cell(session, w, h)
""", "exactly once"),
])
def test_analyser_detects_each_violation(body, expected_fragment):
    """A gate nobody has seen fail is not known to be a gate. Each rule is
    injected as a synthetic violation and must be reported."""
    source = _GOOD.rsplit("def approve", 1)[0] + textwrap.dedent(body)
    problems = _violations(_analyse(source))
    assert problems, f"analyser missed the injected violation:\n{body}"
    assert any(expected_fragment in p for p in problems), (
        f"expected a complaint containing {expected_fragment!r}; got {problems}")


def test_analyser_follows_intra_module_helpers():
    """`transition_period` delegates to `_roll_cells_for_entity`. A one-level
    analyser would score the caller clean and the delegate unreachable."""
    source = _GOOD.rsplit("def approve", 1)[0] + textwrap.dedent("""
def _helper(session, w, h):
    recompute_cell(session, w, h)

def caller(session, w, h):
    _helper(session, w, h)
""")
    problems = _violations(_analyse(source))
    assert any(p.startswith("caller:") for p in problems), (
        f"the delegating caller must be held to the rule; got {problems}")


# ==========================================================================
# Layer 1b -- the real service modules
# ==========================================================================
@pytest.mark.parametrize("module_name",
                         ["budget.py", "periods.py",
                          "procurement_services.py", "procurement.py"])
def test_service_module_obeys_the_lock_order(module_name):
    """Contract 3, checked against the shipped code."""
    problems = _violations(_analyse((_PG_DIR / module_name).read_text(encoding="utf-8")))
    assert problems == [], (
        f"{module_name} breaks the Contract 3 lock order:\n  "
        + "\n  ".join(problems))


def test_the_known_mutating_functions_are_actually_analysed():
    """Guards the guard: if the walker silently found no functions -- a bad
    path, a renamed call, an `ast` change -- every assertion above would pass
    on an empty dictionary."""
    budget = _analyse((_PG_DIR / "budget.py").read_text(encoding="utf-8"))
    periods = _analyse((_PG_DIR / "periods.py").read_text(encoding="utf-8"))

    for fn in ("record_original", "approve_revision", "approve_transfer"):
        assert _LOCK in budget.get(fn, []), (
            f"budget.{fn} must declare its lock set; analysed events: "
            f"{budget.get(fn)!r}")
    for fn in ("_roll_cells_for_entity", "roll_period_effective_budget",
               "transition_period"):
        assert _LOCK in periods.get(fn, []), (
            f"periods.{fn} must declare its lock set; analysed events: "
            f"{periods.get(fn)!r}")

    # Wave 6. Every procurement mutation that reads availability or moves
    # commitment declares its lock set FIRST and with the complete affected
    # set -- every `(wbs_id, budget_head_id)` on every line, which
    # `lock_affected_cells` then expands to every budget-owning ancestor on
    # each chain. Locking only the nearest ancestor is the correctness hole
    # plan section 7.3 records.
    procurement = _analyse(
        (_PG_DIR / "procurement_services.py").read_text(encoding="utf-8"))
    for fn in ("create_pr", "submit_pr", "approve_pr", "create_po",
               "convert_pr_to_po"):
        assert _LOCK in procurement.get(fn, []), (
            f"procurement_services.{fn} must declare its lock set; analysed "
            f"events: "
            f"{procurement.get(fn)!r}")
        assert procurement[fn][0] == _LOCK, (
            f"procurement_services.{fn} must lock FIRST; analysed events: "
            f"{procurement[fn]!r}")


def test_lock_is_not_conditional_in_the_period_roll():
    """The one shape `_violations` flattens away.

    `_roll_cells_for_entity` may return early when the entity has no cells,
    but it must not reach a `recompute_cell` down a path that skipped the
    lock. Asserted structurally: the lock call is a statement of the function
    body itself, not nested inside an `if`/`try` that a recompute can sidestep.
    """
    tree = ast.parse((_PG_DIR / "periods.py").read_text(encoding="utf-8"))
    fn = _module_functions(tree)["_roll_cells_for_entity"]

    top_level_calls = []
    for stmt in fn.body:
        if isinstance(stmt, (ast.If, ast.Try, ast.While, ast.For, ast.With)):
            continue
        top_level_calls.extend(_calls_in_source_order(stmt))
    assert _LOCK in top_level_calls, (
        "lock_affected_cells must be taken unconditionally in the body of "
        "_roll_cells_for_entity, not inside a branch a recompute can bypass")


def test_period_roll_orders_its_driving_query():
    """The roll recomputes every cell in the entity. Without an ORDER BY the
    walk order -- and so the order those implicit UPDATE locks are acquired in
    -- is whatever the planner chose. That is the one thing the
    deadlock-freedom proof cannot tolerate."""
    source = (_PG_DIR / "periods.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = _module_functions(tree)["_roll_cells_for_entity"]

    sql_literals = [n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and "budget_control_cell" in n.value]
    assert sql_literals, "could not find the driving query"
    driver = sql_literals[0].upper()
    assert "ORDER BY" in driver, (
        "the period roll's driving SELECT must impose a deterministic order")
    order_clause = driver[driver.index("ORDER BY"):]
    assert "WBS_PATH" in order_clause and "BUDGET_HEAD_ID" in order_clause, (
        f"the roll must walk in (wbs_path, budget_head_id) order -- the same "
        f"total order the locks are acquired in; got {order_clause!r}")
    assert order_clause.index("WBS_PATH") < order_clause.index("BUDGET_HEAD_ID"), (
        "wbs_path must be the primary sort key")


def test_lock_query_no_longer_filters_on_budget_paise():
    """The Wave 3 fix itself, asserted statically.

    `budget_paise <> 0` in the lock query is what excluded from the lock set
    the exact rows `recompute_cell` was about to UPDATE. Its absence is the
    difference between rule 2 being true and being an unstated exception.
    """
    assert "budget_paise" not in locking._LOCK_SQL, (
        "the lock set must not depend on budget_paise: a zero-budget cell is "
        "still written by recompute_cell, and the value is one a concurrent "
        "transaction can change between two transactions computing their "
        "lock sets")
    assert "budget_control_cell" in locking._LOCK_SQL, (
        "existence of the control cell row is now the only membership test")


# ==========================================================================
# Layer 2 -- the derived lock set
# ==========================================================================
def _chain(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    """`pairs` is (wbs_id, wbs_path), root-first."""
    return list(pairs)


_THREE_LEVEL = {"G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1"))}


def test_zero_budget_cell_is_now_in_the_lock_set():
    """The Wave 3 behaviour change, stated as an assertion.

    Before: `{("R","H1"): 100_000, ("C1","H1"): 0, ("G1","H1"): 0}` derived
    `[("R","H1")]`, and the two zero cells were written unlocked.
    """
    cells = {("R", "H1"): 100_000, ("C1", "H1"): 0, ("G1", "H1"): 0}
    assert derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells) == [
        ("R", "H1"), ("C1", "H1"), ("G1", "H1")]


def test_the_affected_cell_itself_is_always_locked():
    """A cell is its own ancestor. This is what makes `recompute_cell`'s
    `UPDATE budget_control_cell` a re-entry into a held lock rather than a new
    acquisition -- the single most important consequence of the change."""
    cells = {("G1", "H1"): 0}
    assert derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells) == [("G1", "H1")]


def test_all_zero_chain_still_locks_the_whole_chain():
    """The period-open case: every cell's budget is future-dated, so every
    `budget_paise` is 0. The old rule produced an EMPTY lock set here and the
    whole roll ran unlocked, leaving `locks_taken` empty and any ordering
    assertion over it vacuous."""
    cells = {("R", "H1"): 0, ("C1", "H1"): 0, ("G1", "H1"): 0}
    result = derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells)
    assert result, "an all-future-dated chain must still be locked"
    assert result == [("R", "H1"), ("C1", "H1"), ("G1", "H1")]


def test_a_cell_with_no_row_is_still_excluded():
    """Existence, not budget, is the membership test. A pair with no
    `budget_control_cell` row cannot be locked -- and is written by nothing,
    since `recompute_cell`'s UPDATE matches no row."""
    cells = {("R", "H1"): 100_000}          # C1 and G1 have no row for H1
    assert derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells) == [("R", "H1")]


def test_new_lock_set_is_a_superset_of_the_old_one():
    """The widening never loses a cell the old rule held, so every property
    the old set gave the availability argument still holds."""
    cells = {("R", "H1"): 100_000, ("C1", "H1"): 0, ("G1", "H1"): 7}
    old = [pair for pair in derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells)
           if cells[pair] != 0]
    new = derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells)
    assert set(old) <= set(new)
    assert set(new) - set(old) == {("C1", "H1")}


def test_membership_accepts_a_bare_set_of_pairs():
    """`control_cells` is a container, not a `{pair: paise}` mapping -- the
    stored amount is not consulted at all any more."""
    cells = {("R", "H1"), ("C1", "H1")}
    assert derive_lock_set([("G1", "H1")], _THREE_LEVEL, cells) == [
        ("R", "H1"), ("C1", "H1")]


def test_order_is_by_path_then_head_and_is_total():
    cells = {("R", "H3"): 0, ("R", "H1"): 0, ("C1", "H1"): 0, ("G1", "H2"): 0}
    result = derive_lock_set(
        [("G1", "H1"), ("G1", "H2"), ("G1", "H3")], _THREE_LEVEL, cells)
    assert result == [("R", "H1"), ("R", "H3"), ("C1", "H1"), ("G1", "H2")]
    assert len(set(result)) == len(result), "a lock set must not repeat a cell"


def test_order_is_deterministic_regardless_of_input_order():
    """Two transactions presenting the same affected cells in different orders
    must acquire the same locks in the same sequence. This is the property the
    deadlock-freedom proof rests on."""
    cells = {("R", "H1"): 0, ("C1", "H1"): 0, ("G1", "H1"): 0,
             ("R", "H2"): 0}
    forward = derive_lock_set([("G1", "H1"), ("G1", "H2")], _THREE_LEVEL, cells)
    reverse = derive_lock_set([("G1", "H2"), ("G1", "H1")], _THREE_LEVEL, cells)
    assert forward == reverse


def test_dedupes_a_shared_ancestor_across_affected_pairs():
    chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
        "C2": _chain(("R", "R"), ("C2", "R.C2")),
    }
    cells = {("R", "H1"): 0}
    assert derive_lock_set([("G1", "H1"), ("C2", "H1")], chains, cells) == [
        ("R", "H1")]


def test_union_is_per_pair_not_a_cross_product():
    chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
        "C2": _chain(("R", "R"), ("C2", "R.C2")),
    }
    cells = {("R", "H1"): 0, ("C1", "H2"): 0}
    result = derive_lock_set([("G1", "H1"), ("G1", "H2")], chains, cells)
    assert set(result) == {("R", "H1"), ("C1", "H2")}


def test_empty_affected_is_empty():
    assert derive_lock_set([], {}, {}) == []
