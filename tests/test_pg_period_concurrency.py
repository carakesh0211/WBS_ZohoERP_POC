"""Live PostgreSQL proof of the Wave 3 lock set, Contract 3.

Everything provable without a server is in `tests/test_pg_locking_order.py`.
These are the claims that cannot be: that PostgreSQL really takes the row lock
on a zero-budget cell, that a period roll really serialises against a
concurrent approval, and that concurrent transactions really do not deadlock.
A Python mirror of the rule and a grep over `_LOCK_SQL` can both agree with a
query the server rejects, or with a lock the server never takes.

These run only where `CAPEX_DB_URL` is set -- CI's `postgres:16` service. They
SKIP on the development machine, which has no local PostgreSQL, so a green run
here is not evidence they passed. That is stated rather than hidden: the
counts CI reports from its JUnit report are the ones that count.

Each test builds and removes its own fixture over `pg_connection`, and takes
sessions from `pg_database` (a real pool, so threads get real, separate
backends). Nothing depends on demo seed data.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# Without this the live tests below fail at SETUP with "fixture 'pg_database'
# not found" -- but ONLY where CAPEX_DB_URL is set. Locally they skip, so the
# missing fixture is never resolved and the gap is invisible. CI is the first
# place these run for real, which is the whole point of that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os                                                        # noqa: E402
import random                                                    # noqa: E402
import threading                                                 # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date, timedelta                             # noqa: E402

import psycopg                                                   # noqa: E402
import pytest                                                    # noqa: E402

from app.backend.pg import budget as budget_mod                  # noqa: E402
from app.backend.pg import periods as periods_mod                # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402
from app.backend.pg.locking import lock_affected_cells           # noqa: E402

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        not os.environ.get("CAPEX_DB_URL"),
        reason="needs a live PostgreSQL (CAPEX_DB_URL)"),
]


# ==========================================================================
# Fixture construction
# ==========================================================================
class Tree:
    """Ids for one disposable entity: a WBS chain plus its heads and cells."""

    def __init__(self, sfx: str, wbs: list[str], heads: list[str]):
        self.sfx = sfx
        self.org = f"O_{sfx}"
        self.entity = f"E_{sfx}"
        self.project = f"P_{sfx}"
        self.period = f"PER_{sfx}"
        self.wbs = wbs                    # root-first
        self.heads = heads

    @property
    def root(self) -> str:
        return self.wbs[0]

    @property
    def leaf(self) -> str:
        return self.wbs[-1]

    @property
    def head(self) -> str:
        return self.heads[0]


def _build(conn, *, depth: int = 3, heads: int = 1,
           budgets: list[int] | None = None) -> Tree:
    """Seed org -> entity -> project -> a `depth`-deep WBS chain -> heads ->
    control and ledger cells, plus one FUTURE accounting period.

    `budgets[i]` is the `budget_paise` for level `i`; the default is all
    zeroes, which is the period-open shape this file exists to test -- every
    cell present, none of them owning budget yet.

    A `budget_ledger_cell` row is created alongside every control cell.
    `recompute_cell` reads both back through a join and unpacks the result, so
    a missing ledger row is a `TypeError`, not a quiet no-op.
    """
    sfx = uuid.uuid4().hex[:12]
    wbs_ids = [f"W{i}_{sfx}" for i in range(depth)]
    head_ids = [f"H{i}_{sfx}" for i in range(heads)]
    tree = Tree(sfx, wbs_ids, head_ids)
    budgets = budgets if budgets is not None else [0] * depth

    ex = conn.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,'t','t')", (tree.org, f"OC_{sfx}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,%s,'t','t')", (tree.entity, tree.org, f"EC_{sfx}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,%s,'t','t')", (tree.project, tree.entity, f"C_{sfx}", "Project"))

    path = ""
    for i, wbs in enumerate(wbs_ids):
        path = wbs if i == 0 else f"{path}.{wbs}"
        ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, wbs_code,"
           " description, wbs_path, created_by, updated_by)"
           " VALUES (%s,%s,%s,%s,%s,%s,'t','t')",
           (wbs, tree.project, wbs_ids[i - 1] if i else None, wbs, "n", path))

    for head in head_ids:
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
           " created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
           (head, tree.entity, f"HC_{head}", "Head"))
        for i, wbs in enumerate(wbs_ids):
            ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id,"
               " budget_paise, updated_by) VALUES (%s,%s,%s,'t')",
               (wbs, head, budgets[i]))
            ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by)"
               " VALUES (%s,%s,'t')", (wbs, head))

    start = date.today()
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start,"
       " period_end, state, created_by) VALUES (%s,%s,%s,%s,'FUTURE','t')",
       (tree.period, tree.entity, start, start + timedelta(days=30)))
    conn.commit()
    return tree


def _teardown(conn, tree: Tree) -> None:
    ex = conn.execute
    for stmt, params in (
        ("DELETE FROM audit_event WHERE entity_type IS NOT NULL AND actor LIKE %s",
         (f"%{tree.sfx}%",)),
        ("DELETE FROM accounting_period WHERE entity_id = %s", (tree.entity,)),
        ("DELETE FROM budget_version_cell WHERE wbs_id = ANY(%s)", (tree.wbs,)),
        ("DELETE FROM budget_version WHERE project_id = %s", (tree.project,)),
        ("DELETE FROM budget_revision WHERE wbs_id = ANY(%s)", (tree.wbs,)),
        ("DELETE FROM budget_line WHERE wbs_id = ANY(%s)", (tree.wbs,)),
        ("DELETE FROM budget_ledger_cell WHERE wbs_id = ANY(%s)", (tree.wbs,)),
        ("DELETE FROM budget_control_cell WHERE wbs_id = ANY(%s)", (tree.wbs,)),
        ("DELETE FROM budget_head WHERE entity_id = %s", (tree.entity,)),
        ("DELETE FROM wbs_element WHERE project_id = %s", (tree.project,)),
        ("DELETE FROM project WHERE project_id = %s", (tree.project,)),
        ("DELETE FROM entity WHERE entity_id = %s", (tree.entity,)),
        ("DELETE FROM organisation WHERE organisation_id = %s", (tree.org,)),
    ):
        try:
            ex(stmt, params)
            conn.commit()
        except psycopg.Error:
            conn.rollback()          # table not in this schema yet; not the subject


def _draft_revision(conn, tree: Tree, wbs: str, head: str, delta: int) -> str:
    revision_id = f"REV_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id,"
        " delta_paise, effective_from, justification, status, created_by)"
        " VALUES (%s,%s,%s,%s,%s,%s,'DRAFT','drafter')",
        (revision_id, wbs, head, delta, date.today(), "concurrency fixture"))
    conn.commit()
    return revision_id


def _budget_of(conn, wbs: str, head: str) -> int:
    return conn.execute(
        "SELECT budget_paise::bigint FROM budget_control_cell"
        " WHERE wbs_id = %s AND budget_head_id = %s", (wbs, head)).fetchone()[0]


# ==========================================================================
# 1. A zero-budget cell is actually locked
# ==========================================================================
def test_zero_budget_cell_is_in_the_lock_set(pg_database, pg_connection):
    """The defect, inverted into an assertion.

    Every cell on the chain has `budget_paise = 0` -- the period-open shape,
    where all budget is still future-dated. The old `_LOCK_SQL` filtered on
    `budget_paise <> 0` and returned NOTHING here, so `recompute_cell`'s
    UPDATE took each row's lock outside the declared set and `locks_taken`
    stayed empty.
    """
    tree = _build(pg_connection, depth=3)
    try:
        with pg_database.session(Scope.system()) as session:
            locked = lock_affected_cells(session, [(tree.leaf, tree.head)])
            assert locked == [(w, tree.head) for w in tree.wbs], (
                f"every ancestor-or-self cell must be locked regardless of "
                f"budget_paise; got {locked}")
            assert session.locks_taken == locked, (
                "locks_taken must record what was actually locked -- an empty "
                "list lets an ordering assertion pass vacuously")
    finally:
        _teardown(pg_connection, tree)


def test_zero_budget_cell_lock_blocks_a_concurrent_transaction(
        pg_database, pg_connection):
    """Being returned by the query is not the same as being locked. This
    proves the row lock is real by making a second transaction wait for it."""
    tree = _build(pg_connection, depth=2)
    try:
        second_locked = threading.Event()
        release_first = threading.Event()

        def hold_first():
            with pg_database.session(Scope.system()) as session:
                lock_affected_cells(session, [(tree.leaf, tree.head)])
                release_first.wait(timeout=10)

        def take_second():
            with pg_database.session(Scope.system()) as session:
                lock_affected_cells(session, [(tree.leaf, tree.head)])
                second_locked.set()

        first = threading.Thread(target=hold_first)
        first.start()
        release_first.wait(0.3)                      # let the lock be taken
        second = threading.Thread(target=take_second)
        second.start()
        second_locked.wait(0.5)
        assert not second_locked.is_set(), (
            "a zero-budget cell was NOT locked: the second transaction "
            "acquired it while the first still held it")

        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert second_locked.is_set(), "second transaction never got the lock"
    finally:
        _teardown(pg_connection, tree)


# ==========================================================================
# 2. Multiple budget-owning ancestors on one chain
# ==========================================================================
def test_locks_every_owning_ancestor_and_the_zero_leaf_in_path_order(
        pg_database, pg_connection):
    """The original correctness hole (lock ALL owning ancestors, not just the
    nearest) and the Wave 3 widening (the zero-budget leaf too), together.

    This is the live counterpart of
    `test_pg_locking.py::test_lock_set_walks_the_whole_ancestor_chain_live`,
    whose `(leaf, head) not in locked` assertion described the OLD rule.
    """
    tree = _build(pg_connection, depth=3, budgets=[500_000, 200_000, 0])
    root, mid, leaf = tree.wbs
    try:
        with pg_database.session(Scope.system()) as session:
            locked = lock_affected_cells(session, [(leaf, tree.head)])
        assert locked == [(root, tree.head), (mid, tree.head), (leaf, tree.head)], (
            f"both owning ancestors AND the zero-budget leaf must be locked, "
            f"in wbs_path order; got {locked}")
    finally:
        _teardown(pg_connection, tree)


def test_lock_order_is_identical_from_either_end_of_the_chain(
        pg_database, pg_connection):
    """Two transactions whose affected sets differ must still acquire the
    cells they share in the same relative order. That is the whole proof."""
    tree = _build(pg_connection, depth=3, budgets=[500_000, 0, 200_000])
    root, mid, leaf = tree.wbs
    try:
        with pg_database.session(Scope.system()) as session:
            from_leaf = lock_affected_cells(session, [(leaf, tree.head)])
        with pg_database.session(Scope.system()) as session:
            from_mid = lock_affected_cells(session, [(mid, tree.head)])

        assert from_leaf == [(root, tree.head), (mid, tree.head), (leaf, tree.head)]
        assert from_mid == [(root, tree.head), (mid, tree.head)]
        shared = [c for c in from_leaf if c in set(from_mid)]
        assert shared == from_mid, (
            f"the shared prefix must be acquired in the same order by both; "
            f"{shared} vs {from_mid}")
    finally:
        _teardown(pg_connection, tree)


# ==========================================================================
# 3. Two concurrent approvals over overlapping cells: exactly one wins
# ==========================================================================
def test_two_concurrent_approvals_of_one_revision_exactly_one_wins(
        pg_database, pg_connection):
    """Both transactions lock the same chain, then race the same DRAFT row.
    The re-read of `status` under the document lock is what makes the loser
    lose; without the cell locks ordering them first, the two could interleave
    their recomputes."""
    tree = _build(pg_connection, depth=3, budgets=[1_000_000, 0, 0])
    try:
        revision_id = _draft_revision(pg_connection, tree, tree.leaf, tree.head, 50_000)
        start = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def approve(actor: str):
            start.wait(timeout=10)
            try:
                with pg_database.session(Scope.system()) as session:
                    budget_mod.approve_revision(
                        session, revision_id=revision_id, actor=actor)
                result = "won"
            except budget_mod.BudgetServiceError as exc:
                result = f"lost:{exc.code}"
            except psycopg.errors.DeadlockDetected:
                result = "deadlock"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=approve, args=(f"approver{i}",))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert "deadlock" not in outcomes, f"deadlock detected: {outcomes}"
        assert outcomes.count("won") == 1, (
            f"exactly one approval must win; got {outcomes}")
        assert any(o.startswith("lost:REVISION_NOT_DRAFT") for o in outcomes), (
            f"the loser must be rejected on the re-read under the lock; got "
            f"{outcomes}")

        status = pg_connection.execute(
            "SELECT status FROM budget_revision WHERE revision_id = %s",
            (revision_id,)).fetchone()[0]
        assert status == "APPROVED"
        assert _budget_of(pg_connection, tree.leaf, tree.head) == 50_000, (
            "the winning approval must be applied exactly once")
    finally:
        _teardown(pg_connection, tree)


def test_concurrent_approvals_on_overlapping_chains_both_apply_once(
        pg_database, pg_connection):
    """Two DIFFERENT revisions on two cells sharing an ancestor. Neither
    should lose, both should apply exactly once, and the shared ancestor lock
    must serialise them rather than deadlock them."""
    tree = _build(pg_connection, depth=3, heads=1, budgets=[1_000_000, 0, 0])
    root, mid, leaf = tree.wbs
    try:
        rev_a = _draft_revision(pg_connection, tree, mid, tree.head, 30_000)
        rev_b = _draft_revision(pg_connection, tree, leaf, tree.head, 40_000)
        start = threading.Barrier(2)
        errors: list[str] = []
        lock = threading.Lock()

        def approve(revision_id: str, actor: str):
            start.wait(timeout=10)
            try:
                with pg_database.session(Scope.system()) as session:
                    budget_mod.approve_revision(
                        session, revision_id=revision_id, actor=actor)
            except Exception as exc:                          # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=approve, args=(rev_a, "approver-a")),
            threading.Thread(target=approve, args=(rev_b, "approver-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert errors == [], f"overlapping chains must serialise, not fail: {errors}"
        assert _budget_of(pg_connection, mid, tree.head) == 30_000
        assert _budget_of(pg_connection, leaf, tree.head) == 40_000
        assert _budget_of(pg_connection, root, tree.head) == 1_000_000, (
            "the shared ancestor owns its own budget only -- rollups are "
            "computed at read time and never persisted")
    finally:
        _teardown(pg_connection, tree)


# ==========================================================================
# 4. A period opening, concurrent with an approval
# ==========================================================================
def test_period_open_concurrent_with_an_approval_does_not_deadlock(
        pg_database, pg_connection):
    """The asymmetric case the old rule handled worst.

    The roll locks EVERY cell in the entity; the approval locks ONE chain --
    a strict subset, and one that includes the root the roll takes first. Two
    transactions taking a set and a subset of it in a common total order
    cannot deadlock; taking them in planner order could.
    """
    tree = _build(pg_connection, depth=4, heads=2, budgets=[900_000, 0, 0, 0])
    try:
        revision_id = _draft_revision(
            pg_connection, tree, tree.leaf, tree.head, 25_000)
        start = threading.Barrier(2)
        failures: list[str] = []
        lock = threading.Lock()

        def open_period():
            start.wait(timeout=10)
            try:
                with pg_database.session(Scope.system()) as session:
                    periods_mod.transition_period(
                        session, period_id=tree.period, to_state="OPEN",
                        actor="period-opener")
            except Exception as exc:                          # noqa: BLE001
                with lock:
                    failures.append(f"open: {type(exc).__name__}: {exc}")

        def approve():
            start.wait(timeout=10)
            try:
                with pg_database.session(Scope.system()) as session:
                    budget_mod.approve_revision(
                        session, revision_id=revision_id, actor="approver")
            except Exception as exc:                          # noqa: BLE001
                with lock:
                    failures.append(f"approve: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=open_period),
                   threading.Thread(target=approve)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert failures == [], f"period open raced the approval: {failures}"

        state = pg_connection.execute(
            "SELECT state FROM accounting_period WHERE period_id = %s",
            (tree.period,)).fetchone()[0]
        assert state == "OPEN"
        assert pg_connection.execute(
            "SELECT status FROM budget_revision WHERE revision_id = %s",
            (revision_id,)).fetchone()[0] == "APPROVED"
        assert _budget_of(pg_connection, tree.leaf, tree.head) == 25_000, (
            "whichever order they serialised in, the approved revision must "
            "be reflected in the cell -- the roll recomputes from budget_line "
            "history, so it cannot lose a line committed before it ran")
    finally:
        _teardown(pg_connection, tree)


def test_period_roll_records_a_non_empty_lock_set(pg_database, pg_connection):
    """The roll must not run having locked nothing.

    Every cell is zero-budget here, which is exactly when the old lock set was
    empty. `locks_taken` is the observable, and an ordering assertion over an
    empty list proves nothing.
    """
    tree = _build(pg_connection, depth=3, heads=2)
    try:
        with pg_database.session(Scope.system()) as session:
            rolled = periods_mod.roll_period_effective_budget(
                session, tree.period, actor="roller")
            expected = len(tree.wbs) * len(tree.heads)
            assert rolled["cells_recomputed"] == expected
            assert len(session.locks_taken) == expected, (
                f"the roll recomputed {expected} cells but recorded "
                f"{len(session.locks_taken)} locks")
            assert session.locks_taken == sorted(
                session.locks_taken,
                key=lambda c: (tree.wbs.index(c[0]), c[1])), (
                "locks must be taken in (wbs_path, budget_head_id) order")
    finally:
        _teardown(pg_connection, tree)


# ==========================================================================
# 5. Sustained randomised run: zero deadlocks
# ==========================================================================
@pytest.mark.slow
def test_sustained_random_concurrency_produces_no_deadlock(
        pg_database, pg_connection):
    """Mixed workload against overlapping lock sets, repeatedly.

    Four workers interleave three shapes with deliberately different affected
    sets -- a whole-entity roll, a single-chain grant, and a bare lock of a
    random cell -- over a tree with several heads, so the sets genuinely
    overlap in varying ways. Under one total order this cannot deadlock; a
    single planner-ordered scan among them eventually will.

    Seeded, so a failure is reproducible.
    """
    tree = _build(pg_connection, depth=4, heads=3, budgets=[2_000_000, 0, 0, 0])
    workers, iterations = 4, 12
    deadlocks: list[str] = []
    other: list[str] = []
    completed: list[int] = []
    guard = threading.Lock()

    def worker(seed: int):
        rng = random.Random(seed)
        done = 0
        for _ in range(iterations):
            wbs = rng.choice(tree.wbs)
            head = rng.choice(tree.heads)
            action = rng.choice(("roll", "grant", "lock"))
            try:
                with pg_database.session(Scope.system()) as session:
                    if action == "roll":
                        periods_mod.roll_period_effective_budget(
                            session, tree.period, actor=f"w{seed}")
                    elif action == "grant":
                        budget_mod.record_original(
                            session, wbs_id=wbs, budget_head_id=head,
                            amount_paise=rng.randint(1, 1000),
                            effective_from=date.today(), actor=f"w{seed}")
                    else:
                        lock_affected_cells(session, [(wbs, head)])
                done += 1
            except psycopg.errors.DeadlockDetected as exc:
                with guard:
                    deadlocks.append(f"w{seed} {action} {wbs}/{head}: {exc}")
            except Exception as exc:                          # noqa: BLE001
                with guard:
                    other.append(f"w{seed} {action}: {type(exc).__name__}: {exc}")
        with guard:
            completed.append(done)

    try:
        threads = [threading.Thread(target=worker, args=(s,)) for s in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert not any(t.is_alive() for t in threads), (
            "a worker never finished -- a lock wait that never resolved is "
            "the symptom a deadlock detector would otherwise have reported")

        assert deadlocks == [], (
            f"{len(deadlocks)} deadlock(s) under a supposedly total lock "
            f"order:\n  " + "\n  ".join(deadlocks))
        assert other == [], f"unexpected failures: {other}"
        assert sum(completed) == workers * iterations, (
            f"only {sum(completed)} of {workers * iterations} operations "
            f"completed")
    finally:
        _teardown(pg_connection, tree)
