"""Tests for `app.backend.pg.periods` -- the fiscal period state machine and
the budget roll at period open.

Pure logic (the transition table itself, the reconciliation-exception guard
when the table does not exist yet) runs unconditionally. Live behaviour
(actual state transitions, the roll moving money between budget_paise and
future_budget_paise, idempotency, and the open-exception refusal once the
table exists) needs a live PostgreSQL and is gated below, exactly like
`tests/test_pg_locking.py`.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_locking.py's module docstring for why this import block is
# copied rather than referenced.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import budget, periods  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ============================================================== transition table (pure)
def test_allowed_transitions_are_forward_only():
    assert periods._ALLOWED_TRANSITIONS["FUTURE"] == {"OPEN"}
    assert periods._ALLOWED_TRANSITIONS["OPEN"] == {"SOFT_CLOSED"}
    assert periods._ALLOWED_TRANSITIONS["SOFT_CLOSED"] == {"CLOSED"}
    assert periods._ALLOWED_TRANSITIONS["CLOSED"] == set()


def test_no_transition_skips_a_state():
    """FUTURE -> CLOSED and FUTURE -> SOFT_CLOSED must both be illegal: only
    the single forward step is ever allowed."""
    assert "CLOSED" not in periods._ALLOWED_TRANSITIONS["FUTURE"]
    assert "SOFT_CLOSED" not in periods._ALLOWED_TRANSITIONS["FUTURE"]


def test_no_backward_transition():
    assert "FUTURE" not in periods._ALLOWED_TRANSITIONS["OPEN"]
    assert "OPEN" not in periods._ALLOWED_TRANSITIONS["SOFT_CLOSED"]
    assert "SOFT_CLOSED" not in periods._ALLOWED_TRANSITIONS["CLOSED"]


# ============================================================== error shape (pure)
def test_period_service_error_carries_code_status_message():
    exc = periods.PeriodServiceError("X", "y", status=409)
    assert exc.code == "X"
    assert exc.status == 409
    assert exc.message == "y"


# ============================================================== reconciliation guard, table absent (pure-ish)
def test_table_exists_helper_is_false_for_a_made_up_name():
    class FakeSession:
        def fetchone(self, statement, params=None):
            return None  # information_schema lookup finds nothing

    assert periods._table_exists(FakeSession(), "reconciliation_exception") is False


def test_the_reconciliation_gate_refuses_when_it_cannot_be_evaluated():
    """It used to return False here, and False is what PERMITS the close.

    The original test asserted "vacuously False without the table", per a brief
    asking for a check "trivially satisfied now and correct when the table
    lands". The check was not trivially satisfied. It was trivially BYPASSED:
    `transition_period` reads the return as "nothing blocks this close", the
    `and` short-circuits, and because `reconciliation_exception` existed in no
    migration, §11.8's gate could never fire on the shipped schema. A control
    that is unreachable is not a control.

    The scenario it exists for: a sweep raises GRN_LINE_UNATTRIBUTED for a real
    sum, there is nowhere to write it, finance closes the period, and CWIP
    publishes a number nobody can stand behind.

    So an unevaluable gate now REFUSES. `integration/outbound.py` already did
    this -- it raises `DetectiveControlUnavailable` rather than reporting zero
    unsanctioned commitments -- and the two now agree, which they did not
    before.

    Migration 011 creates the table, so this refusal is the transient state
    between "cannot be evaluated" and "evaluated", not a permanent block.
    """

    class FakeSession:
        """Reports the table as absent, and fails loudly if queried again."""

        def __init__(self):
            self.calls = 0

        def fetchone(self, statement, params=None):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError(
                    "must not query reconciliation_exception once its "
                    "absence has already been established")
            return None

    session = FakeSession()
    with pytest.raises(periods.ReconciliationGateUnavailable) as excinfo:
        periods._has_open_reconciliation_exceptions(session, "ENT-01")

    assert session.calls == 1, (
        "the absence check must short-circuit; querying a table already known "
        "to be absent is a second failure mode, not a fallback")

    message = str(excinfo.value)
    assert "reconciliation_exception" in message, (
        "the refusal must name the missing table, or an operator cannot act "
        "on it")
    assert "UNKNOWN" in message, (
        "the refusal must say the answer is unknown rather than implying "
        "exceptions exist -- those are different facts and only one is true")


def test_no_falsy_return_can_reach_the_close_gate_for_a_missing_table():
    """The guard on the guard: a future 'simplification' back to `return False`
    would restore the fail-open silently, and every other test would still
    pass. This asserts the function raises rather than returning ANY value."""
    class FakeSession:
        def fetchone(self, statement, params=None):
            return None

    try:
        result = periods._has_open_reconciliation_exceptions(FakeSession(), "ENT-01")
    except periods.ReconciliationGateUnavailable:
        return
    raise AssertionError(
        f"the gate returned {result!r} instead of refusing. Any return value "
        f"is read by transition_period as an answer, and the falsy one permits "
        f"the close -- which is how this control became unreachable.")


def _seed_entity_with_periods(con, *, suffix):
    """One organisation, one entity, one FUTURE period -- the minimum a period
    transition needs, and nothing more.

    Restored, not invented. It was deleted by `11977d4` while that commit
    rewrote the reconciliation-guard tests around it, and its three call sites
    stayed. Every test below skips without `CAPEX_DB_URL`, so on the dev
    machine the `NameError` was never raised and the suite reported green; CI's
    `pg_tests` job, which is the only place these bodies execute, raised it
    eight times. The definition here is byte-equivalent in effect to the one
    `4cfa05f` introduced -- same tables, same columns, same FUTURE start state
    -- so nothing about what the tests exercise has changed.

    The period is deliberately left in FUTURE with a `period_start` of
    2026-01-01: every transition test drives the machine forward from the one
    state that has a legal move, and the budget-roll tests need a period whose
    own start date is what `recompute_cell` is asked to evaluate `as_of`.

    `suffix` keys every identifier, so two calls in one test (the entity
    isolation case) cannot collide on a primary key or on
    `accounting_period`'s gist exclusion, which forbids two overlapping
    periods for the SAME entity and would otherwise reject the second seed.
    """
    org, ent = f"O_{suffix}", f"E_{suffix}"
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
        "VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{suffix}", "Entity"))
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "state, created_by) VALUES (%s,%s,'2026-01-01','2026-03-31','FUTURE','t')",
        (f"PER_{suffix}", ent))
    con.commit()
    return {"org": org, "entity": ent, "period": f"PER_{suffix}"}


def _seed_cell_under_entity(con, *, suffix, entity_id):
    prj, wbs, head = f"P_{suffix}", f"W_{suffix}", f"H_{suffix}"
    con.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
        "VALUES (%s,%s,%s,%s,'t','t')", (prj, entity_id, f"C_{suffix}", "Project"))
    con.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
        "VALUES (%s,%s,%s,%s,'t','t')", (head, entity_id, f"HC_{suffix}", "Head"))
    con.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
        "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')", (wbs, prj, wbs, "n", wbs))
    con.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
        "VALUES (%s,%s,0,'t')", (wbs, head))
    con.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
        "VALUES (%s,%s,'t')", (wbs, head))
    con.commit()
    return {"project": prj, "wbs": wbs, "head": head}


@PG
def test_transition_future_to_open_succeeds(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    assert result["state"] == "OPEN"

    row = pg_connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s", (ids["period"],)).fetchone()
    assert row[0] == "OPEN"


@PG
def test_transition_refuses_illegal_skip(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(
                session, period_id=ids["period"], to_state="CLOSED", actor="U-ADM")
    assert excinfo.value.code == "ILLEGAL_TRANSITION"

    row = pg_connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s", (ids["period"],)).fetchone()
    assert row[0] == "FUTURE", "an illegal transition must not have mutated the row"


@PG
def test_transition_refuses_backward_move(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(
                session, period_id=ids["period"], to_state="FUTURE", actor="U-ADM")
    assert excinfo.value.code == "ILLEGAL_TRANSITION"


@PG
def test_transition_to_closed_sets_closed_at_and_closed_by(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="SOFT_CLOSED", actor="U-PFC")
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")

    row = pg_connection.execute(
        "SELECT state, closed_at, closed_by FROM accounting_period WHERE period_id = %s",
        (ids["period"],)).fetchone()
    assert row[0] == "CLOSED"
    assert row[1] is not None
    assert row[2] == "U-CFO"


@PG
def test_period_cannot_close_while_open_reconciliation_exception_exists(pg_database, pg_connection):
    """The gate FIRES: an Open exception for the entity blocks the close.

    This test used to ``CREATE TABLE reconciliation_exception`` inline, because
    no migration created it. Migration 011 does now, so creating it here would
    raise ``DuplicateTable`` -- and, worse, a hand-rolled stand-in would let
    the test pass against a shape the shipped schema does not have. The row is
    therefore inserted into the REAL table, with the real NOT NULL columns and
    the real ``ck_reconciliation_exception_status`` namespace C18 froze.

    Asserting the table came from the migration is part of the test, not
    scaffolding. If 011 is ever dropped from the runner, every close in the
    product starts refusing -- the guard raises rather than permitting -- and
    the failure should name that cause here rather than surface as a puzzling
    ``ReconciliationGateUnavailable`` somewhere unrelated.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)

    exists = pg_connection.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = "
        "current_schema() AND table_name = 'reconciliation_exception'").fetchone()
    assert exists is not None, (
        "migration 011 must have created reconciliation_exception; without it "
        "_has_open_reconciliation_exceptions refuses every close and this test "
        "would be asserting the wrong refusal")

    with pg_database.session(Scope.system()) as session:
        periods.transition_period(session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="SOFT_CLOSED", actor="U-PFC")

    # A real GRN_LINE_UNATTRIBUTED, shaped as migration 011 declares it:
    # `kind`, `object_type` and `detail` are NOT NULL with no default, and
    # status 'Open' must leave resolved_at/resolved_by NULL per
    # ck_reconciliation_exception_resolution. This is the exact scenario 11.8
    # exists for -- a GRN line for a real sum with nowhere to be attributed.
    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type, "
        "object_id, entity_id, status, detail, local_paise, source_paise) "
        "VALUES (%s, 'GRN_LINE_UNATTRIBUTED', 'GRN_LINE', %s, %s, 'Open', "
        "'A GRN line for a real sum has no WBS attribution.', 125000, 130000)",
        (f"EXC_{suffix}", f"GRNL_{suffix}", ids["entity"]))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(
                session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")
    assert excinfo.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"

    row = pg_connection.execute(
        "SELECT state, closed_at, closed_by FROM accounting_period WHERE period_id = %s",
        (ids["period"],)).fetchone()
    assert row[0] == "SOFT_CLOSED", "the refused close must not have mutated the row"
    assert row[1] is None and row[2] is None, (
        "a refused close must not stamp closing metadata either")


@PG
def test_a_resolved_exception_no_longer_blocks_the_close(pg_database, pg_connection):
    """The other half of the gate, and the half that proves it is a gate rather
    than a wall: once the exception is Resolved it stops blocking.

    Without this, `_has_open_reconciliation_exceptions` could return True
    unconditionally and every assertion in the test above would still hold. The
    ``status = 'Open'`` filter in its WHERE clause is load-bearing only if
    something proves a non-Open row is ignored.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="SOFT_CLOSED", actor="U-PFC")

    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type, "
        "object_id, entity_id, status, detail, resolved_at, resolved_by) "
        "VALUES (%s, 'CONTROL_TOTAL_MISMATCH', 'BATCH', %s, %s, 'Resolved', "
        "'Reconciled against the source control total.', now(), 'U-PFC')",
        (f"EXC_{suffix}", f"BATCH_{suffix}", ids["entity"]))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")

    row = pg_connection.execute(
        "SELECT state, closed_by FROM accounting_period WHERE period_id = %s",
        (ids["period"],)).fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == "U-CFO"


@PG
def test_another_entitys_open_exception_does_not_block_this_close(pg_database, pg_connection):
    """The gate is scoped to the closing period's OWN entity.

    An unscoped ``SELECT 1 FROM reconciliation_exception WHERE status = 'Open'``
    would satisfy every assertion in the blocking test above, and would freeze
    every close in the estate the moment any one entity raised an exception.
    Two entities, one exception, and only its owner is blocked.
    """
    suffix_a, suffix_b = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    entity_a = _seed_entity_with_periods(pg_connection, suffix=suffix_a)
    entity_b = _seed_entity_with_periods(pg_connection, suffix=suffix_b)

    for ids in (entity_a, entity_b):
        with pg_database.session(Scope.system()) as session:
            periods.transition_period(
                session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
        with pg_database.session(Scope.system()) as session:
            periods.transition_period(
                session, period_id=ids["period"], to_state="SOFT_CLOSED", actor="U-PFC")

    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type, "
        "object_id, entity_id, status, detail) VALUES (%s, "
        "'UNSANCTIONED_COMMITMENT', 'PO', %s, %s, 'Open', "
        "'A PO we have no local record of.')",
        (f"EXC_{suffix_a}", f"PO_{suffix_a}", entity_a["entity"]))
    pg_connection.commit()

    # A's close is refused...
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(
                session, period_id=entity_a["period"], to_state="CLOSED", actor="U-CFO")
    assert excinfo.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"

    # ...and B's is not.
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=entity_b["period"], to_state="CLOSED", actor="U-CFO")
    row = pg_connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s",
        (entity_b["period"],)).fetchone()
    assert row[0] == "CLOSED", (
        "entity B has no exception of its own; another entity's must not block "
        "its close")


@PG
def test_transition_to_open_rolls_future_budget_into_current(pg_database, pg_connection):
    """Opening a period recomputes every cell in its entity **as of the
    period's own start date**, and that is what moves money between
    ``budget_paise`` and ``future_budget_paise``.

    Two cells, one entity, one roll, opposite outcomes -- which is the part
    that discriminates. A roll that simply zeroed ``budget_paise``, or one that
    simply left every cell alone, would satisfy a single-cell assertion; only a
    pair whose grants straddle the period start can tell those apart.

    Deliberately independent of wall-clock "today". ``record_original``
    recomputes as of ``date.today()``, so what the CURRENT bucket holds BEFORE
    the roll depends on when CI happens to run. What the roll itself produces
    does not: its ``as_of`` is ``period_start`` (2026-01-01), a stored value,
    so the post-roll split below is the same on every run. The earlier version
    of this test asserted the pre-roll figure instead and had to explain in a
    comment why the roll's own effect was not visible.
    """
    suffix = uuid.uuid4().hex[:10]
    entity_ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    # Both cells hang off the same entity, so one roll covers both.
    before_start = _seed_cell_under_entity(
        pg_connection, suffix=f"{suffix}a", entity_id=entity_ids["entity"])
    after_start = _seed_cell_under_entity(
        pg_connection, suffix=f"{suffix}b", entity_id=entity_ids["entity"])

    with pg_database.session(Scope.system()) as session:
        # Effective BEFORE the period opens: spendable the moment it does.
        budget.record_original(
            session, wbs_id=before_start["wbs"], budget_head_id=before_start["head"],
            amount_paise=500000, effective_from=date(2025, 12, 1), actor="U-PFC")
        # Effective mid-period: not yet spendable at the instant of opening.
        budget.record_original(
            session, wbs_id=after_start["wbs"], budget_head_id=after_start["head"],
            amount_paise=900000, effective_from=date(2026, 2, 15), actor="U-PFC")

    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=entity_ids["period"], to_state="OPEN", actor="U-ADM")
    assert result["state"] == "OPEN"

    def _split(cell):
        return pg_connection.execute(
            "SELECT bc.budget_paise, bl.future_budget_paise, bc.updated_by "
            "FROM budget_control_cell bc "
            "JOIN budget_ledger_cell bl ON bl.wbs_id = bc.wbs_id "
            " AND bl.budget_head_id = bc.budget_head_id "
            "WHERE bc.wbs_id = %s AND bc.budget_head_id = %s",
            (cell["wbs"], cell["head"])).fetchone()

    current, future, actor = _split(before_start)
    assert (current, future) == (500000, 0), (
        "a grant effective before the period start is CURRENT at open")
    assert actor == "U-ADM", (
        "the roll must have run inside transition_period, stamping its actor; "
        "'t' here means the cell was never recomputed and the two assertions "
        "above are only describing the seed")

    current, future, actor = _split(after_start)
    assert (current, future) == (0, 900000), (
        "a grant effective after the period start is FUTURE at open -- this is "
        "the money the roll moves, and asserting only the other cell would let "
        "a roll that never looked at effective_from pass")
    assert actor == "U-ADM"


@PG
def test_the_open_roll_covers_every_cell_in_the_entity(pg_database, pg_connection):
    """`_roll_cells_for_entity` is one entity's COMPLETE set, and the audit row
    records how many cells it moved.

    The count is asserted through the audit trail rather than a return value
    because `transition_period` does not return it -- and a roll that silently
    covered one of three cells would leave two positions stale while the
    transition reported success.
    """
    suffix = uuid.uuid4().hex[:10]
    entity_ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    for index in range(3):
        _seed_cell_under_entity(
            pg_connection, suffix=f"{suffix}{index}", entity_id=entity_ids["entity"])

    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=entity_ids["period"], to_state="OPEN", actor="U-ADM")

    row = pg_connection.execute(
        "SELECT detail FROM audit_log WHERE object_id = %s "
        "AND action = 'PERIOD_TRANSITION' ORDER BY seq DESC LIMIT 1",
        (entity_ids["period"],)).fetchone()
    assert row is not None, "the transition must have written an audit row"
    assert "rolled 3 cell(s)" in row[0], (
        f"the open roll must cover all three of the entity's cells; audit "
        f"detail was {row[0]!r}")


@PG
def test_roll_period_effective_budget_is_idempotent(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    entity_ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    cell = _seed_cell_under_entity(pg_connection, suffix=suffix, entity_id=entity_ids["entity"])

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=cell["wbs"], budget_head_id=cell["head"],
            amount_paise=250000, effective_from=date(2026, 1, 1), actor="U-PFC")

    with pg_database.session(Scope.system()) as session:
        first = periods.roll_period_effective_budget(session, entity_ids["period"], actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        second = periods.roll_period_effective_budget(session, entity_ids["period"], actor="U-ADM")

    assert first["cells_recomputed"] == second["cells_recomputed"] == 1

    row = pg_connection.execute(
        "SELECT budget_paise FROM budget_control_cell WHERE wbs_id = %s AND budget_head_id = %s",
        (cell["wbs"], cell["head"])).fetchone()
    assert row[0] == 250000, "two rolls must produce the same figure as one"


@PG
def test_roll_period_effective_budget_only_touches_its_own_entity(pg_database, pg_connection):
    """One entity per invocation: a cell belonging to a DIFFERENT entity must
    be untouched by rolling this period."""
    suffix_a, suffix_b = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    entity_a = _seed_entity_with_periods(pg_connection, suffix=suffix_a)
    entity_b = _seed_entity_with_periods(pg_connection, suffix=suffix_b)
    cell_a = _seed_cell_under_entity(pg_connection, suffix=suffix_a, entity_id=entity_a["entity"])
    cell_b = _seed_cell_under_entity(pg_connection, suffix=suffix_b, entity_id=entity_b["entity"])

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=cell_a["wbs"], budget_head_id=cell_a["head"],
            amount_paise=111100, effective_from=date(2026, 1, 1), actor="U-PFC")
        budget.record_original(
            session, wbs_id=cell_b["wbs"], budget_head_id=cell_b["head"],
            amount_paise=222200, effective_from=date(2026, 1, 1), actor="U-PFC")

    before_b = pg_connection.execute(
        "SELECT updated_at FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
        (cell_b["wbs"], cell_b["head"])).fetchone()[0]

    with pg_database.session(Scope.system()) as session:
        result = periods.roll_period_effective_budget(session, entity_a["period"], actor="U-ADM")
    assert result["entity_id"] == entity_a["entity"]
    assert result["cells_recomputed"] == 1

    after_b = pg_connection.execute(
        "SELECT updated_at FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
        (cell_b["wbs"], cell_b["head"])).fetchone()[0]
    assert before_b == after_b, "rolling entity A's period must not touch entity B's cell"
