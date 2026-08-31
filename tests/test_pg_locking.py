"""Tests for `app.backend.pg.locking` -- ordered ancestor-chain locking.

Two layers are tested:

  * `derive_lock_set` and the guard/shape behaviour of `lock_affected_cells`
    itself (via a `FakeSession` that records calls instead of touching a
    database) are pure logic and run unconditionally.
  * Real lock acquisition against PostgreSQL -- concurrency, actual
    `FOR UPDATE` blocking, deadlock-freedom under contention -- needs a live
    database and is gated below.

A CI agent is expected to add a Postgres service and the `pg_database` /
`pg_connection` / `pg_scope` fixtures this file's database-backed tests
request. Until then (and whenever `CAPEX_DB_URL` is unset locally), those
tests skip rather than error.

Note on markers: the spec for this module calls the database-only tests
`pytest.mark.pg`. `pytest.ini` runs with `--strict-markers` and does not yet
register `pg` in its `markers =` list, and this agent does not own
`pytest.ini` (only the files listed in its brief). Applying an unregistered
mark fails collection outright, unconditionally -- `skipif` does not save it,
because strict-marker validation happens before any skip logic runs. `PG`
below is therefore a `skipif`-based stand-in with identical gating semantics;
whoever owns `pytest.ini` can add `pg: needs a live PostgreSQL database` to
`markers =` and swap `@PG` for `@pytest.mark.pg` in one pass with no behaviour
change.
"""
from __future__ import annotations

import os

import pytest

from app.backend.pg.engine import Scope
from app.backend.pg.locking import advisory_audit_lock, derive_lock_set, lock_affected_cells

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


class FakeSession:
    """Records calls; never touches a database."""

    def __init__(self, scope: Scope | None = None, fetchall_rows=None):
        self.scope = scope
        self.locks_taken: list[tuple[str, str]] = []
        self._fetchall_rows = fetchall_rows if fetchall_rows is not None else []
        self.fetchall_calls: list[tuple[str, object]] = []
        self.execute_calls: list[tuple[str, object]] = []

    def fetchall(self, statement, params=None):
        self.fetchall_calls.append((statement, params))
        return self._fetchall_rows

    def execute(self, statement, params=None):
        self.execute_calls.append((statement, params))
        return None


# ============================================================== derive_lock_set (pure)
def _chain(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    """`pairs` is (wbs_id, wbs_path), root-first."""
    return list(pairs)


def test_derive_lock_set_excludes_non_owning_ancestors():
    """budget_paise = 0 (or absent) cells own no availability and must not
    appear in the lock set, even though they are on the ancestor chain."""
    ancestor_chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
    }
    budget_paise = {("R", "H1"): 100_000}   # C1 and G1 own nothing for H1
    result = derive_lock_set([("G1", "H1")], ancestor_chains, budget_paise)
    assert result == [("R", "H1")]


def test_derive_lock_set_locks_every_budget_owning_ancestor_not_just_nearest():
    """The correctness hole this module exists to close: more than one
    ancestor on the same chain can own budget for the same head, and all of
    them must be locked, not just the nearest one."""
    ancestor_chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
    }
    budget_paise = {("R", "H1"): 100_000, ("C1", "H1"): 20_000}
    result = derive_lock_set([("G1", "H1")], ancestor_chains, budget_paise)
    assert result == [("R", "H1"), ("C1", "H1")]


def test_derive_lock_set_is_per_pair_not_a_cross_product():
    """Two affected pairs for different heads on different branches must not
    lock each other's heads -- the union is over (w, h) pairs, never all
    heads against all wbs ids."""
    ancestor_chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
        "C2": _chain(("R", "R"), ("C2", "R.C2")),
    }
    budget_paise = {("R", "H1"): 100_000, ("C1", "H2"): 50_000}
    result = derive_lock_set([("G1", "H1"), ("G1", "H2")], ancestor_chains, budget_paise)
    # (G1,H1) -> R owns H1.  (G1,H2) -> C1 owns H2.  R never owned H2, so it
    # must not appear paired with H2, and C1 never owned H1.
    assert set(result) == {("R", "H1"), ("C1", "H2")}


def test_derive_lock_set_dedupes_shared_ancestor_across_affected_pairs():
    ancestor_chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
        "C2": _chain(("R", "R"), ("C2", "R.C2")),
    }
    budget_paise = {("R", "H1"): 100_000}
    result = derive_lock_set([("G1", "H1"), ("C2", "H1")], ancestor_chains, budget_paise)
    assert result == [("R", "H1")]   # locked once, not twice


def test_derive_lock_set_orders_by_wbs_path_then_head_a_total_order():
    ancestor_chains = {
        "G1": _chain(("R", "R"), ("C1", "R.C1"), ("G1", "R.C1.G1")),
    }
    budget_paise = {("R", "H3"): 1, ("R", "H1"): 1, ("C1", "H1"): 1}
    result = derive_lock_set([("G1", "H1"), ("G1", "H3")], ancestor_chains, budget_paise)
    # Same wbs_path ("R") sorts its two heads lexicographically; "R" sorts
    # before "R.C1" as a path prefix.
    assert result == [("R", "H1"), ("R", "H3"), ("C1", "H1")]


def test_derive_lock_set_empty_affected_is_empty():
    assert derive_lock_set([], {}, {}) == []


# ============================================================== lock_affected_cells shape
def test_lock_affected_cells_empty_affected_never_touches_session():
    session = FakeSession()
    result = lock_affected_cells(session, [])
    assert result == []
    assert session.fetchall_calls == []
    assert session.locks_taken == []


def test_lock_affected_cells_appends_to_locks_taken_in_returned_order():
    rows = [("R", "H1", "R"), ("C1", "H1", "R.C1")]
    session = FakeSession(fetchall_rows=rows)
    result = lock_affected_cells(session, [("G1", "H1")])
    assert result == [("R", "H1"), ("C1", "H1")]
    assert session.locks_taken == [("R", "H1"), ("C1", "H1")]


def test_lock_affected_cells_appends_without_clearing_prior_locks():
    """`locks_taken` accumulates across the whole transaction -- a second
    call within the same session (which should not happen per the "exactly
    once" rule, but the data structure itself must not silently drop history)
    still extends rather than replaces."""
    session = FakeSession(fetchall_rows=[("R", "H1", "R")])
    session.locks_taken.append(("PRE-EXISTING", "H0"))
    lock_affected_cells(session, [("G1", "H1")])
    assert session.locks_taken == [("PRE-EXISTING", "H0"), ("R", "H1")]


def test_lock_affected_cells_query_is_a_single_ordered_for_update():
    session = FakeSession(fetchall_rows=[])
    lock_affected_cells(session, [("G1", "H1"), ("C2", "H2")])
    assert len(session.fetchall_calls) == 1, "must be one round trip, not one per cell"
    statement, params = session.fetchall_calls[0]
    assert "FOR UPDATE" in statement
    assert "ORDER BY" in statement
    assert "budget_paise <> 0" in statement
    assert params == {"wbs_ids": ["G1", "C2"], "heads": ["H1", "H2"]}


def test_lock_affected_cells_pairs_are_positional_not_a_cross_product_in_sql():
    """The SQL uses `unnest(a, b)` to pair wbs_ids[i] with heads[i]
    positionally; it must not build the cartesian product of every wbs id
    against every head."""
    session = FakeSession(fetchall_rows=[])
    lock_affected_cells(session, [("G1", "H1"), ("C2", "H2")])
    statement, _params = session.fetchall_calls[0]
    assert "unnest(" in statement
    assert "CROSS JOIN" not in statement.upper()


# ============================================================== advisory_audit_lock
def test_advisory_audit_lock_uses_hashtext_and_xact_scope():
    session = FakeSession()
    advisory_audit_lock(session, "PurchaseOrder:PO-1")
    assert len(session.execute_calls) == 1
    statement, params = session.execute_calls[0]
    assert "pg_advisory_xact_lock" in statement
    assert "hashtext" in statement
    assert params == ("PurchaseOrder:PO-1",)


# ============================================================== live database
@PG
def test_lock_affected_cells_actually_blocks_a_concurrent_transaction(
    pg_database, pg_connection, pg_scope
):
    """Two transactions racing the *same* budget-owning ancestor cell must
    serialise: the second `lock_affected_cells` call must not return until
    the first transaction commits or rolls back. This is the property the
    whole ancestor-chain design exists to guarantee, and it can only be
    proven against real PostgreSQL row locks -- a fake session cannot fake
    blocking.

    Self-contained: inserts its own minimal entity/project/wbs/budget_head/
    control-cell fixture over `pg_connection` (rather than depending on
    demo-seed data this agent does not own) and removes it afterwards.
    """
    import threading
    import time
    import uuid

    suffix = uuid.uuid4().hex[:12]
    entity_id, project_id = f"E_{suffix}", f"PRJ_{suffix}"
    wbs_id, head_id = f"W_{suffix}", f"H_{suffix}"

    pg_connection.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, "
        "created_by, updated_by) SELECT %s, organisation_id, %s, %s, 'test', "
        "'test' FROM organisation LIMIT 1",
        (entity_id, f"CODE_{suffix}", f"Entity {suffix}"),
    )
    pg_connection.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
        (project_id, entity_id, f"CAPEX_{suffix}", f"Project {suffix}"),
    )
    pg_connection.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
        "wbs_path, created_by, updated_by) VALUES (%s, %s, %s, %s, %s, "
        "'test', 'test')",
        (wbs_id, project_id, f"WBS_{suffix}", "root", wbs_id),
    )
    pg_connection.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
        (head_id, entity_id, f"HEAD_{suffix}", f"Head {suffix}"),
    )
    pg_connection.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
        "budget_paise, updated_by) VALUES (%s, %s, 100000, 'test')",
        (wbs_id, head_id),
    )
    pg_connection.commit()

    try:
        second_locked = threading.Event()
        release_first = threading.Event()
        order: list[str] = []

        def hold_first():
            with pg_database.session(pg_scope) as session:
                lock_affected_cells(session, [(wbs_id, head_id)])
                order.append("first-locked")
                release_first.wait(timeout=5)
            order.append("first-released")

        def take_second():
            with pg_database.session(pg_scope) as session:
                lock_affected_cells(session, [(wbs_id, head_id)])
                order.append("second-locked")
                second_locked.set()

        first = threading.Thread(target=hold_first)
        first.start()
        time.sleep(0.3)   # let the first transaction actually take the lock

        second = threading.Thread(target=take_second)
        second.start()
        time.sleep(0.3)
        assert not second_locked.is_set(), (
            "the second transaction acquired the same cell while the first "
            "still held it -- the lock did not serialise the two")

        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert second_locked.is_set(), "second transaction never got the lock"
        assert order[0] == "first-locked"
        assert order.index("first-released") < order.index("second-locked")
    finally:
        pg_connection.execute(
            "DELETE FROM budget_control_cell WHERE wbs_id = %s", (wbs_id,))
        pg_connection.execute(
            "DELETE FROM budget_head WHERE budget_head_id = %s", (head_id,))
        pg_connection.execute(
            "DELETE FROM wbs_element WHERE wbs_id = %s", (wbs_id,))
        pg_connection.execute(
            "DELETE FROM project WHERE project_id = %s", (project_id,))
        pg_connection.execute(
            "DELETE FROM entity WHERE entity_id = %s", (entity_id,))
        pg_connection.commit()
