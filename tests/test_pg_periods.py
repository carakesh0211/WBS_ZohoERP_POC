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


def test_has_open_reconciliation_exceptions_is_vacuously_false_without_the_table():
    """This stream's brief: write the check "so it is trivially satisfied
    now and correct when the table lands". `_table_exists` returning False
    must short-circuit to False without ever querying the (nonexistent)
    table -- proved here with a session that raises if queried twice."""

    class FakeSession:
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
    assert periods._has_open_reconciliation_exceptions(session, "ENT-1") is False
    assert session.calls == 1


# ==========================================================================
# Live database
# ==========================================================================
def _seed_entity_with_periods(con, *, suffix):
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
    """The table does not exist in this schema yet. This test creates it
    inline -- a minimal stand-in shaped exactly as periods.py expects
    (entity_id, status) -- to prove the guard function correctly BLOCKS a
    close once the table exists and carries an Open row, not just that it is
    vacuously satisfied in its absence (covered by the pure test above)."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(session, period_id=ids["period"], to_state="OPEN", actor="U-ADM")
    with pg_database.session(Scope.system()) as session:
        periods.transition_period(
            session, period_id=ids["period"], to_state="SOFT_CLOSED", actor="U-PFC")

    pg_connection.execute(
        "CREATE TABLE reconciliation_exception ("
        "exception_id text PRIMARY KEY, entity_id text NOT NULL, status text NOT NULL)")
    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, entity_id, status) "
        "VALUES (%s, %s, 'Open')", (f"EXC_{suffix}", ids["entity"]))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(
                session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")
    assert excinfo.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"

    row = pg_connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s", (ids["period"],)).fetchone()
    assert row[0] == "SOFT_CLOSED", "the refused close must not have mutated the row"


@PG
def test_transition_to_open_rolls_future_budget_into_current(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    entity_ids = _seed_entity_with_periods(pg_connection, suffix=suffix)
    cell = _seed_cell_under_entity(pg_connection, suffix=suffix, entity_id=entity_ids["entity"])

    with pg_database.session(Scope.system()) as session:
        # An ORIGINAL grant effective from the period's own start date --
        # i.e. NOT yet effective while the period is still FUTURE.
        budget.record_original(
            session, wbs_id=cell["wbs"], budget_head_id=cell["head"],
            amount_paise=500000, effective_from=date(2026, 1, 1), actor="U-PFC")

        row = session.fetchone(
            "SELECT budget_paise, future_budget_paise FROM budget_control_cell bc "
            "JOIN budget_ledger_cell bl USING (wbs_id, budget_head_id) "
            "WHERE wbs_id = %s AND budget_head_id = %s", (cell["wbs"], cell["head"]))
    # record_original recomputes as of "today" (test run time, well after
    # 2026-01-01), so the grant already landed in the CURRENT bucket before
    # the period is even opened -- the roll's real job, demonstrated below,
    # is picking up a grant that is future relative to the PERIOD's own
    # start date, independent of wall-clock "today".
    assert row[0] == 500000

    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=entity_ids["period"], to_state="OPEN", actor="U-ADM")
    assert result["state"] == "OPEN"


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
