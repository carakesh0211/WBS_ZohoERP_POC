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

    # Seed the organisation explicitly. This previously did
    # `INSERT INTO entity ... SELECT organisation_id FROM organisation LIMIT 1`,
    # but pg_template runs upgrade() with NO seed data, so `organisation` is
    # empty: the SELECT matched nothing, the INSERT added zero rows, and the
    # next statement died on project.entity_id's foreign key -- before the test
    # reached a single assertion. A live test that cannot run is not coverage.
    org_id = f"ORG_{suffix}"
    pg_connection.execute(
        "INSERT INTO organisation (organisation_id, code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, 'test', 'test')",
        (org_id, f"ORGC_{suffix}", f"Org {suffix}"),
    )
    pg_connection.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
        (entity_id, org_id, f"CODE_{suffix}", f"Entity {suffix}"),
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
                # Recorded INSIDE the block, before __exit__ commits.
                #
                # This previously appended "first-released" AFTER the `with`
                # exited, and raced: the commit inside __exit__ unblocks the
                # waiting thread immediately, so the second thread could append
                # "second-locked" before this thread reached its next line.
                # The database was behaving correctly -- the assertion was
                # comparing two Python appends, not two database events.
                order.append("first-releasing")

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
        # Sound ordering: "first-releasing" is appended before the commit that
        # releases the lock, so it must precede the other thread's acquisition.
        assert order.index("first-releasing") < order.index("second-locked"), (
            f"the second transaction must acquire only after the first "
            f"released; got {order}")

        # The real proof of serialisation is the pair of assertions above:
        # the second transaction had NOT acquired while the first held the
        # cell, and DID acquire once it was released. Those are statements
        # about database behaviour. The ordering check is a secondary
        # consistency check on the same events.
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


# ---------------------------------------------------------------------------
# Lead-added regression: the locking query must remain executable by PostgreSQL.
# ---------------------------------------------------------------------------
def test_locking_query_does_not_combine_distinct_with_for_update():
    """PostgreSQL rejects `SELECT DISTINCT ... FOR UPDATE`.

        ERROR:  FOR UPDATE is not allowed with DISTINCT clause

    A locking clause needs every returned row to map to one identifiable table
    row, and DISTINCT destroys that mapping. The first version of this query
    combined them and would have failed on first contact with a real database --
    the live-Postgres tests that would have caught it are exactly the ones
    skipped when no local database is available, so this asserts it statically.

    De-duplication belongs in a CTE; the outer locking SELECT must be plain.
    """
    from app.backend.pg import locking

    sql = locking._LOCK_SQL
    upper = sql.upper()

    assert "FOR UPDATE" in upper, "the lock query must actually take row locks"

    # The outer query is everything after the final CTE. Locate the last
    # top-level SELECT -- the one carrying FOR UPDATE -- and assert it is not
    # a SELECT DISTINCT.
    for_update_at = upper.index("FOR UPDATE")
    outer_select_at = upper.rindex("SELECT", 0, for_update_at)
    outer_query = upper[outer_select_at:for_update_at]

    assert "DISTINCT" not in outer_query, (
        "the locking SELECT must not use DISTINCT; PostgreSQL refuses to "
        "combine it with FOR UPDATE. Move de-duplication into a CTE."
    )


def test_locking_query_still_deduplicates():
    """Removing DISTINCT from the outer query must not lose de-duplication.

    Two affected cells sharing an ancestor would otherwise try to lock that
    ancestor twice, so the guarantee has to survive the fix, not just the
    syntax error.
    """
    from app.backend.pg import locking

    assert "DISTINCT" in locking._LOCK_SQL.upper(), (
        "de-duplication was removed entirely rather than moved into a CTE"
    )
    assert "WITH" in locking._LOCK_SQL.upper()


def test_locking_query_orders_by_path_then_head():
    """The total order is what makes the deadlock-freedom proof hold."""
    from app.backend.pg import locking

    upper = locking._LOCK_SQL.upper()
    order_at = upper.rindex("ORDER BY")
    order_clause = upper[order_at:upper.index("FOR UPDATE", order_at)]
    assert "PATH" in order_clause and "BUDGET_HEAD_ID" in order_clause, (
        f"lock acquisition must be ordered by (wbs_path, budget_head_id); got {order_clause!r}"
    )
    assert order_clause.index("PATH") < order_clause.index("BUDGET_HEAD_ID"), (
        "path must be the primary sort key"
    )


@pytest.mark.pg
@pytest.mark.skipif(not os.environ.get("CAPEX_DB_URL"),
                    reason="needs a live PostgreSQL (CAPEX_DB_URL)")
def test_lock_set_walks_the_whole_ancestor_chain_live(pg_database, pg_connection):
    """The domain rule, executed against a real server.

    Everything else covering the ancestor chain is either `derive_lock_set` --
    a Python re-implementation that could agree with a wrong SQL query -- or a
    string grep over `_LOCK_SQL`. Neither would notice a query PostgreSQL
    cannot parse, which is exactly the defect that reached integration.

    Three levels, budget owned at levels 1 and 2 and NOT at level 3. A spend on
    the leaf must lock BOTH owning ancestors, in (wbs_path, budget_head_id)
    order. Locking only the nearest owner is the correctness hole the domain
    rules call out by name.
    """
    import uuid
    from app.backend.pg.engine import Scope
    from app.backend.pg.locking import lock_affected_cells

    sfx = uuid.uuid4().hex[:12]
    org, ent, prj, head = f"O_{sfx}", f"E_{sfx}", f"P_{sfx}", f"H_{sfx}"
    root, mid, leaf = f"R_{sfx}", f"M_{sfx}", f"L_{sfx}"

    ex = pg_connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,'t','t')", (org, f"OC_{sfx}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{sfx}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,%s,'t','t')", (prj, ent, f"C_{sfx}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by)"
       " VALUES (%s,%s,%s,%s,'t','t')", (head, ent, f"HC_{sfx}", "Head"))

    for wbs, parent, path in ((root, None, root),
                              (mid, root, f"{root}.{mid}"),
                              (leaf, mid, f"{root}.{mid}.{leaf}")):
        ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, wbs_code,"
           " description, wbs_path, created_by, updated_by)"
           " VALUES (%s,%s,%s,%s,%s,%s,'t','t')",
           (wbs, prj, parent, wbs, "n", path))

    # Budget owned at root and mid; the leaf owns none.
    for wbs, paise in ((root, 500000), (mid, 200000), (leaf, 0)):
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise,"
           " updated_by) VALUES (%s,%s,%s,'t')", (wbs, head, paise))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        locked = lock_affected_cells(session, [(leaf, head)])

    assert locked == [(root, head), (mid, head)], (
        f"a spend on the leaf must lock BOTH budget-owning ancestors in "
        f"wbs_path order, not just the nearest; got {locked}"
    )
    assert (leaf, head) not in locked, (
        "a cell with budget_paise = 0 owns no availability and must be excluded"
    )
    assert session.locks_taken == locked, "lock order must be recorded for audit"
