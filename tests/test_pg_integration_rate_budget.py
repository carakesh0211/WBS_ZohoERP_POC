"""The rate-budget reservation, executed against a real PostgreSQL server.

WHY THIS FILE EXISTS
====================

Wave 5 shipped three independent implementations of reserving a call against
`integration_rate_budget`. Two of them could not execute:

* `throttle.py::_RESERVE_SQL` named `lane`, `created_by` and `updated_by` --
  none of which the shipped table has -- and conflicted on
  `(connection_id, window_kind, lane, window_start)`, which is not a constraint
  that exists;
* `outbound.py::_UPSERT` conflicted on a non-key and omitted five NOT NULL
  columns with no defaults.

**Every unit test over both modules passed, throughout.** Both talked to
in-memory doubles, and a double written from the same misreading as the module
agrees with the module about everything, including a statement the server
cannot parse. `tests/test_integration_sql_matches_schema.py` closes the crudest
half of that by comparing column NAMES against migration 010. It says so
itself: it is not a SQL parser, it cannot check that an `ON CONFLICT` target
matches a real unique index, and it cannot tell a name that exists from a name
used wrongly.

This file is the other half. It runs the repaired path -- `throttle.reserve`
delegating to `integration_store.reserve_calls` -- against a live server, so
that "the SQL executes" stops being a claim and becomes an observation. The
four things it proves are the four the static gate structurally cannot:

  1. **reserve** -- the statement parses, binds, and charges both windows;
  2. **conflict / upsert** -- the second reservation in the same window finds
     the row the first one opened, rather than inserting a duplicate or
     raising on the primary key;
  3. **ceiling refusal** -- the guard refuses at the boundary and
     `ck_integration_rate_budget_used_within_ceiling` is never reached, and
     `ck_integration_rate_budget_exhausted_stamp` -- a BICONDITIONAL -- is
     satisfied by the same statement that fills the window;
  4. **release** -- a reservation refused by one window survives in NEITHER,
     because the savepoint rolls it back, and the counter never goes backwards
     past what was genuinely spent.

A SKIP IS NOT A PASS
====================

There is no PostgreSQL and no Docker on the machine this file was written on,
so **every test below skipped here and none of them has ever run locally.** CI's
`pg_tests` job supplies a `postgres:16` service container and sets
`CAPEX_DB_URL`; that job is the only oracle for this file, and until it is green
the correct summary of this work is "the SQL now names real columns and has not
been executed". The `xfail(strict=True)` on `throttle.py` in
`tests/test_integration_sql_matches_schema.py` should be removed only after
this file has passed there.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly. Without this
# the tests below fail at SETUP with "fixture 'pg_connection' not found" -- but
# ONLY where CAPEX_DB_URL is set. Locally they skip, so the missing fixture is
# never resolved and the gap is invisible until CI, which is the whole point of
# that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    _config_and_provider, pg_admin_connection, pg_connection, pg_database,
    pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.integration import throttle  # noqa: E402
from app.backend.integration.throttle import Lane, WindowKind  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

CONNECTION = "CONN-BUDGET"
ENTITY = "ENT-BUDGET"
#: Mid-day and mid-minute on purpose: a boundary instant would let an off-by-one
#: in the bucket key pass by landing on the same string either way.
T0 = datetime(2026, 9, 6, 11, 30, 15, tzinfo=timezone.utc)


class _Clock:
    """The injected clock `throttle.reserve` takes, so the window under test is
    chosen by the test rather than by when CI happens to run."""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> datetime:
        self._now = self._now + timedelta(**delta)
        return self._now


def _seed(con: psycopg.Connection, *, per_minute: int = 100,
          daily: int = 2000) -> None:
    """Organisation, entity and one `integration_connection`, committed.

    Raw SQL rather than `integration_store.create_connection`: the point of
    this file is what the SERVER does with the reservation, and routing the
    fixture through the module under test would let a Python-side guard stand
    in for the schema.

    The two ceilings are parameters because they are what
    `ensure_rate_budget_windows` copies onto the budget row -- the ceiling is a
    per-row snapshot of the connection's, not a join, so that raising a plan
    tier at noon cannot retroactively rewrite what the morning's budget was.
    """
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES ('ORG-B', 'ORGB', 'Budget Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name,"
        " created_by, updated_by) VALUES (%s, 'ORG-B', %s, 'Budget Entity',"
        " 'T', 'T') ON CONFLICT DO NOTHING", (ENTITY, ENTITY))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-BUDGET', 'b@example.test',"
        " 'Budget service', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    con.execute(
        "INSERT INTO integration_connection (connection_id, entity_id,"
        " product, dc, organization_id, connector_name,"
        " per_minute_call_ceiling, daily_call_ceiling, created_by, updated_by)"
        " VALUES (%s, %s, 'ERP', 'in', '60000000001', 'zoho_erp', %s, %s,"
        " 'T', 'T')",
        (CONNECTION, ENTITY, per_minute, daily))
    con.commit()


def _session(con: psycopg.Connection) -> Session:
    """A `Session` over the raw test connection.

    The scope RESTRICTS to the seeded entity rather than using
    `Scope.system()`: an unrestricted scope compiles the predicate to `TRUE`,
    which would let a statement whose scope join is wrong pass every test in
    this file.
    """
    return Session(connection=con,
                   scope=Scope(user_id="U-BUDGET", entity_ids=frozenset({ENTITY})))


def _rows(con: psycopg.Connection) -> dict[tuple, tuple]:
    """`{(window_kind, allocation): (used, ceiling, exhausted_at, key)}`."""
    return {
        (r[0], r[1]): (r[2], r[3], r[4], r[5])
        for r in con.execute(
            "SELECT window_kind, allocation, used, ceiling, exhausted_at,"
            " window_start_key FROM integration_rate_budget"
            " WHERE connection_id = %s", (CONNECTION,)).fetchall()
    }


# ==========================================================================
# 1. RESERVE -- the statement executes at all
# ==========================================================================
@PG
@pytest.mark.pg
def test_live_a_reservation_executes_and_charges_both_windows(pg_connection):
    """The whole point of this file.

    `throttle._RESERVE_SQL` would have raised `UndefinedColumn` on the word
    `lane` here, and no unit test in the repository would have noticed.
    """
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    verdict = throttle.reserve(session, connection_id=CONNECTION,
                               lane=Lane.POLLING, actor="SVC-BUDGET",
                               clock=clock)
    con.commit()

    assert bool(verdict) is True
    assert verdict.binding is None and verdict.resume_at is None
    assert {w.kind for w in verdict.windows} == {WindowKind.DAY, WindowKind.MINUTE}

    rows = _rows(con)
    assert set(rows) == {("MINUTE", "POLLING"), ("DAY", "POLLING")}
    # 60/30/10 applied to BOTH windows: 60 of 100 a minute, 1,200 of 2,000 a day.
    assert rows[("MINUTE", "POLLING")][:2] == (1, 60)
    assert rows[("DAY", "POLLING")][:2] == (1, 1200)
    # Not exhausted, so the biconditional stamp must be NULL, not "unset".
    assert rows[("MINUTE", "POLLING")][2] is None
    assert rows[("DAY", "POLLING")][2] is None
    # The bucket identity the application computed, in the zone it recorded.
    assert rows[("MINUTE", "POLLING")][3] == "2026-09-06T11:30"
    assert rows[("DAY", "POLLING")][3] == "2026-09-06"


@PG
@pytest.mark.pg
def test_live_every_not_null_column_is_supplied(pg_connection):
    """`window_start_key`, `window_seconds`, `window_tz` and `ceiling` are NOT
    NULL with no default. The old statement omitted all four; renaming its
    three wrong columns would have satisfied the name gate and still failed
    here, which is why that gate stays red until this passes."""
    con = pg_connection
    _seed(con)
    throttle.reserve(_session(con), connection_id=CONNECTION,
                     lane=Lane.OUTBOUND, actor="SVC-BUDGET", clock=_Clock())
    con.commit()

    for kind, seconds in (("MINUTE", 60), ("DAY", 86400)):
        row = con.execute(
            "SELECT window_start_key, window_seconds, window_tz, ceiling,"
            " window_start, updated_at FROM integration_rate_budget"
            " WHERE connection_id = %s AND window_kind = %s",
            (CONNECTION, kind)).fetchone()
        assert row is not None, f"no {kind} row was opened"
        assert all(value is not None for value in row), f"{kind}: {row}"
        assert row[1] == seconds, "ck_..._window_seconds is immutable-by-integer"
        assert row[2] == "UTC"


@PG
@pytest.mark.pg
def test_live_the_scope_predicate_really_filters(pg_connection):
    """A scope that does not grant the owning entity sees no connection, so
    nothing is seeded and nothing is charged -- and the caller is told the
    connection is invisible rather than that the budget is exhausted."""
    con = pg_connection
    _seed(con)
    stranger = Session(
        connection=con,
        scope=Scope(user_id="U-OTHER", entity_ids=frozenset({"ENT-SOMEONE-ELSE"})))

    with pytest.raises(throttle.ConnectionNotVisible):
        throttle.reserve(stranger, connection_id=CONNECTION, lane=Lane.POLLING,
                         actor="SVC-BUDGET", clock=_Clock())
    con.rollback()
    assert _rows(con) == {}, "an invisible connection must charge nothing"


# ==========================================================================
# 2. CONFLICT / UPSERT -- the second call finds the first call's row
# ==========================================================================
@PG
@pytest.mark.pg
def test_live_a_second_reservation_in_the_same_window_adds_to_the_same_row(
        pg_connection):
    """The `ON CONFLICT (connection_id, window_kind, allocation,
    window_start_key) DO NOTHING` target is the table's REAL primary key.

    The old statement's target -- `(connection_id, window_kind, lane,
    window_start)` -- matches no unique index, and PostgreSQL raises
    `InvalidColumnReference` for that rather than silently inserting. This is
    the assertion `test_integration_sql_matches_schema.py` says in its own
    docstring it cannot make.
    """
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for expected in (1, 2, 3):
        verdict = throttle.reserve(session, connection_id=CONNECTION,
                                   lane=Lane.POLLING, actor="SVC-BUDGET",
                                   clock=clock)
        assert verdict, f"reservation {expected} was refused"
        assert verdict.window(WindowKind.DAY).used == expected
    con.commit()

    count = con.execute(
        "SELECT count(*) FROM integration_rate_budget WHERE connection_id = %s",
        (CONNECTION,)).fetchone()[0]
    assert count == 2, "three reservations, two window rows -- one per window"
    rows = _rows(con)
    assert rows[("MINUTE", "POLLING")][0] == 3
    assert rows[("DAY", "POLLING")][0] == 3


@PG
@pytest.mark.pg
def test_live_a_new_minute_window_is_a_new_row_and_the_old_one_survives(
        pg_connection):
    """The row is keyed on `window_start_key`, so the rollover is free -- and
    the elapsed window is left as evidence rather than reset in place."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    throttle.reserve(session, connection_id=CONNECTION, lane=Lane.POLLING,
                     actor="SVC-BUDGET", clock=clock)
    clock.advance(minutes=1)
    throttle.reserve(session, connection_id=CONNECTION, lane=Lane.POLLING,
                     actor="SVC-BUDGET", clock=clock)
    con.commit()

    minutes = con.execute(
        "SELECT window_start_key, used FROM integration_rate_budget"
        " WHERE connection_id = %s AND window_kind = 'MINUTE'"
        " ORDER BY window_start_key", (CONNECTION,)).fetchall()
    assert [tuple(r) for r in minutes] == [("2026-09-06T11:30", 1),
                                           ("2026-09-06T11:31", 1)]
    # One day row, carrying both calls.
    days = con.execute(
        "SELECT window_start_key, used FROM integration_rate_budget"
        " WHERE connection_id = %s AND window_kind = 'DAY'",
        (CONNECTION,)).fetchall()
    assert [tuple(r) for r in days] == [("2026-09-06", 2)]


@PG
@pytest.mark.pg
def test_live_the_lanes_do_not_share_a_row(pg_connection):
    """`allocation` is in the primary key, which is what makes "polling is
    capped at 60" enforceable rather than decorative. Amendment A1 said the
    60/30/10 split cannot be enforced without it; this is that claim executed."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for lane in (Lane.POLLING, Lane.OUTBOUND, Lane.INTERACTIVE):
        assert throttle.reserve(session, connection_id=CONNECTION, lane=lane,
                                actor="SVC-BUDGET", clock=clock)
    con.commit()

    rows = _rows(con)
    assert len(rows) == 6, "three lanes x two windows, no sharing"
    assert rows[("MINUTE", "POLLING")][:2] == (1, 60)
    assert rows[("MINUTE", "OUTBOUND")][:2] == (1, 30)
    assert rows[("MINUTE", "INTERACTIVE")][:2] == (1, 10)
    assert rows[("DAY", "POLLING")][:2] == (1, 1200)
    assert rows[("DAY", "OUTBOUND")][:2] == (1, 600)
    assert rows[("DAY", "INTERACTIVE")][:2] == (1, 200)


# ==========================================================================
# 3. CEILING REFUSAL -- the guard, and the biconditional stamp
# ==========================================================================
@PG
@pytest.mark.pg
def test_live_the_minute_lane_refuses_at_its_allocation(pg_connection):
    """Sixty polling calls fill the minute; the sixty-first is refused by the
    `WHERE`, not by the CHECK. The distinction matters: a CHECK violation
    poisons the transaction, and section 2.2's contract is that a throttled job
    COMMITS its progress and returns."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for _ in range(60):
        assert throttle.reserve(session, connection_id=CONNECTION,
                                lane=Lane.POLLING, actor="SVC-BUDGET",
                                clock=clock)

    refused = throttle.reserve(session, connection_id=CONNECTION,
                               lane=Lane.POLLING, actor="SVC-BUDGET",
                               clock=clock)
    assert not refused
    assert refused.binding is WindowKind.MINUTE
    assert refused.window(WindowKind.MINUTE).used == 60
    assert refused.window(WindowKind.MINUTE).remaining == 0
    assert refused.resume_at == datetime(2026, 9, 6, 11, 31, tzinfo=timezone.utc)

    # The transaction survived the refusal, which is the property that lets a
    # poller checkpoint. If the CHECK had fired instead, this commit would
    # raise InFailedSqlTransaction.
    con.commit()
    rows = _rows(con)
    assert rows[("MINUTE", "POLLING")][0] == 60


@PG
@pytest.mark.pg
def test_live_a_full_window_stamps_exhausted_at_in_the_same_statement(
        pg_connection):
    """`ck_integration_rate_budget_exhausted_stamp` is
    `(exhausted_at IS NOT NULL) = (used >= ceiling)` -- an EQUALITY. A
    reservation that filled the window without stamping would be refused by
    the database, and one that stamped early would be too. Only the statement
    that makes it true may record it."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for index in range(1, 61):
        verdict = throttle.reserve(session, connection_id=CONNECTION,
                                   lane=Lane.POLLING, actor="SVC-BUDGET",
                                   clock=clock)
        assert verdict, f"call {index} refused"
    con.commit()

    used, ceiling, exhausted_at, _ = _rows(con)[("MINUTE", "POLLING")]
    assert (used, ceiling) == (60, 60)
    assert exhausted_at is not None, "the window that filled must say so"
    # The day window is nowhere near full, so it must NOT be stamped.
    day_used, day_ceiling, day_stamp, _ = _rows(con)[("DAY", "POLLING")]
    assert (day_used, day_ceiling) == (60, 1200)
    assert day_stamp is None


@PG
@pytest.mark.pg
def test_live_an_oversized_reservation_cannot_buy_out_a_lane(pg_connection):
    """A first-ever call whose cost exceeds the whole lane must insert nothing
    over budget: there is no conflict to arbitrate on that path, so the guard
    has to hold on the opening reservation too."""
    con = pg_connection
    _seed(con)
    session = _session(con)

    refused = throttle.reserve(session, connection_id=CONNECTION,
                               lane=Lane.INTERACTIVE, cost=11,
                               actor="SVC-BUDGET", clock=_Clock())
    assert not refused
    assert refused.binding is WindowKind.MINUTE   # 11 > the interactive 10
    con.commit()

    rows = _rows(con)
    assert rows[("MINUTE", "INTERACTIVE")][0] == 0
    assert rows[("DAY", "INTERACTIVE")][0] == 0, (
        "the day had room for 11 and must not keep them")


@PG
@pytest.mark.pg
def test_live_the_daily_ceiling_is_the_binding_one_on_erp_standard(pg_connection):
    """1,200 polling calls a day, and the minute window never gets a look in:
    100/minute would permit 144,000 a day. This is the plan's claim executed
    against the real table rather than asserted against a double."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for _ in range(20):                        # 20 minutes x 60 = 1,200
        for _ in range(60):
            assert throttle.reserve(session, connection_id=CONNECTION,
                                    lane=Lane.POLLING, actor="SVC-BUDGET",
                                    clock=clock)
        clock.advance(minutes=1)

    refused = throttle.reserve(session, connection_id=CONNECTION,
                               lane=Lane.POLLING, actor="SVC-BUDGET",
                               clock=clock)
    assert not refused
    assert refused.binding is WindowKind.DAY
    assert refused.resume_at == datetime(2026, 9, 7, tzinfo=timezone.utc)
    con.commit()

    assert _rows(con)[("DAY", "POLLING")][:2] == (1200, 1200)


@PG
@pytest.mark.pg
def test_live_an_exhausted_polling_lane_does_not_starve_the_operator(
        pg_connection):
    """The whole reason the allocation exists, in both windows at once."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for _ in range(20):
        for _ in range(60):
            throttle.reserve(session, connection_id=CONNECTION,
                             lane=Lane.POLLING, actor="SVC-BUDGET", clock=clock)
        clock.advance(minutes=1)

    assert not throttle.reserve(session, connection_id=CONNECTION,
                                lane=Lane.POLLING, actor="SVC-BUDGET",
                                clock=clock)
    assert throttle.reserve(session, connection_id=CONNECTION,
                            lane=Lane.INTERACTIVE, actor="SVC-BUDGET",
                            clock=clock)
    assert throttle.reserve(session, connection_id=CONNECTION,
                            lane=Lane.OUTBOUND, actor="SVC-BUDGET", clock=clock)
    con.commit()


@PG
@pytest.mark.pg
def test_live_the_ceiling_check_still_refuses_a_hand_written_overspend(
        pg_connection):
    """The `WHERE` guard turns the ordinary case into a verdict a screen can
    render. `ck_integration_rate_budget_used_within_ceiling` is what still
    holds against a psql session, a future caller and a bug in `reserve_calls`
    -- so it is asserted separately, exactly as migration 010 argues."""
    con = pg_connection
    _seed(con)
    throttle.reserve(_session(con), connection_id=CONNECTION,
                     lane=Lane.POLLING, actor="SVC-BUDGET", clock=_Clock())
    con.commit()

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        con.execute("UPDATE integration_rate_budget SET used = ceiling + 1,"
                    " exhausted_at = now() WHERE window_kind = 'MINUTE'")
    con.rollback()
    assert "used_within_ceiling" in str(caught.value)

    with pytest.raises(psycopg.errors.CheckViolation) as negative:
        con.execute("UPDATE integration_rate_budget SET used = -1"
                    " WHERE window_kind = 'MINUTE'")
    con.rollback()
    assert "used_within_ceiling" in str(negative.value), (
        "the non-negative floor is the same constraint, and it is the one "
        "standing in for throttle._release's deleted GREATEST(..., 0)")


# ==========================================================================
# 4. RELEASE -- refused in one window, spent in neither
# ==========================================================================
@PG
@pytest.mark.pg
def test_live_a_day_reservation_is_released_when_the_minute_refuses(
        pg_connection):
    """The property `throttle._release` used to compensate for, now structural.

    The day window is nowhere near full, so the single `UPDATE` charges it and
    refuses the minute -- and the SAVEPOINT rolls the day charge back. Leaving
    it would burn one of the day's 2,000 calls for a request that was never
    made: one per throttled attempt, which on a backfill is how a daily quota
    evaporates against nothing.
    """
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for _ in range(60):
        assert throttle.reserve(session, connection_id=CONNECTION,
                                lane=Lane.POLLING, actor="SVC-BUDGET",
                                clock=clock)
    assert _rows(con)[("DAY", "POLLING")][0] == 60

    # Ten refusals in a row. Each one charges the day inside the savepoint and
    # gives it back; a leak of one call per attempt would show up as 70.
    for _ in range(10):
        refused = throttle.reserve(session, connection_id=CONNECTION,
                                   lane=Lane.POLLING, actor="SVC-BUDGET",
                                   clock=clock)
        assert not refused
        assert refused.binding is WindowKind.MINUTE
        # The verdict reports the POST-release count. Reporting 61 would tell
        # an operator the day had spent a call it had not.
        assert refused.window(WindowKind.DAY).used == 60
        assert refused.window(WindowKind.DAY).remaining == 1140
    con.commit()

    assert _rows(con)[("DAY", "POLLING")][0] == 60, (
        "ten refused reservations leaked day budget")
    assert _rows(con)[("MINUTE", "POLLING")][0] == 60


@PG
@pytest.mark.pg
def test_live_a_refusal_leaves_the_transaction_usable(pg_connection):
    """section 2.2's contract is "commit progress and return". A function that
    can only report exhaustion by poisoning the transaction cannot honour it,
    so the refusal must leave the session able to do more work -- and to
    COMMIT the work it had already done."""
    con = pg_connection
    _seed(con)
    session, clock = _session(con), _Clock()

    for _ in range(60):
        throttle.reserve(session, connection_id=CONNECTION, lane=Lane.POLLING,
                         actor="SVC-BUDGET", clock=clock)
    assert not throttle.reserve(session, connection_id=CONNECTION,
                                lane=Lane.POLLING, actor="SVC-BUDGET",
                                clock=clock)

    # Still usable: a different lane still reserves, on the same transaction.
    assert throttle.reserve(session, connection_id=CONNECTION,
                            lane=Lane.INTERACTIVE, actor="SVC-BUDGET",
                            clock=clock)
    con.commit()

    rows = _rows(con)
    assert rows[("MINUTE", "POLLING")][0] == 60
    assert rows[("MINUTE", "INTERACTIVE")][0] == 1


@PG
@pytest.mark.pg
def test_live_two_concurrent_reservations_cannot_both_take_the_last_call(
        pg_connection, pg_disposable_db_name, pg_url):
    """Section 2.1: there is no resident process, so the race between two cron
    invocations has nowhere to be arbitrated but the statement itself.

    Two connections to the SAME database, both reserving the last call in the
    lane. Exactly one may win. A read-then-check in Python is stale by the time
    it is acted on, and stale in the direction that overspends.
    """
    from conftest_pg import _replace_dbname

    con = pg_connection
    _seed(con, per_minute=100)
    session, clock = _session(con), _Clock()

    for _ in range(59):                        # one call left in the lane
        assert throttle.reserve(session, connection_id=CONNECTION,
                                lane=Lane.POLLING, actor="SVC-BUDGET",
                                clock=clock)
    con.commit()

    other = psycopg.connect(_replace_dbname(pg_url, pg_disposable_db_name),
                            autocommit=False)
    try:
        first = throttle.reserve(session, connection_id=CONNECTION,
                                 lane=Lane.POLLING, actor="SVC-BUDGET",
                                 clock=clock)
        con.commit()
        second = throttle.reserve(_session(other), connection_id=CONNECTION,
                                  lane=Lane.POLLING, actor="SVC-BUDGET",
                                  clock=clock)
        other.commit()
    finally:
        other.close()

    assert bool(first) != bool(second), (
        "exactly one of two racing reservations may take the last call; "
        f"got first={bool(first)} second={bool(second)}")
    assert _rows(con)[("MINUTE", "POLLING")][0] == 60


# ==========================================================================
# The gate this file is the precondition for
# ==========================================================================
@PG
@pytest.mark.pg
def test_live_the_repaired_module_names_only_columns_that_exist(pg_connection):
    """The static gate compares names against the migration TEXT. This asks the
    SERVER, which is the only thing that settles it, and it is the evidence the
    lead needs before removing the `xfail(strict=True)` on `throttle.py` in
    `tests/test_integration_sql_matches_schema.py`."""
    con = pg_connection
    _seed(con)
    throttle.reserve(_session(con), connection_id=CONNECTION,
                     lane=Lane.POLLING, actor="SVC-BUDGET", clock=_Clock())
    con.commit()

    declared = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'integration_rate_budget'").fetchall()}
    assert "allocation" in declared
    for absent in ("lane", "created_by", "updated_by"):
        assert absent not in declared, (
            f"{absent!r} exists after all; re-read migration 010 before "
            f"trusting this repair")

    key = {
        r[0] for r in con.execute(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid"
            "  AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'integration_rate_budget'::regclass"
            "   AND i.indisprimary").fetchall()}
    assert key == {"connection_id", "window_kind", "allocation",
                   "window_start_key"}, (
        "the ON CONFLICT target in integration_store must be this key")
