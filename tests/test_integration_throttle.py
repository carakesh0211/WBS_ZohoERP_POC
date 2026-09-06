"""Rate budgeting, retry, backoff and the circuit breaker (plan §11.6).

Wave 5 stream 4, testing ``app/backend/integration/throttle.py``.

**Nothing here touches a network, a tenant, a cloud resource or a real clock.**
Time is a :class:`ManualClock` the test advances by hand and jitter is a
:class:`FrozenRandom` that returns a chosen point of the interval, so every
assertion is on an exact value rather than on a range. A test that sleeps for
real is a test nobody runs, and a backoff test that sleeps 256 seconds to prove
it slept 256 seconds is the worst of them.

The property this file exists to hold
--------------------------------------
§11.6 lists six failure conditions. Each one has to produce its **own**
observable behaviour, and each is forced here by a test that constructs exactly
that condition:

  ==========================  ==================================================
  429 code 44 (per-minute)    checkpoint, resume next tick, circuit untouched
  429 code 45 (daily quota)   circuit OPEN until the UTC day boundary, alert
  429 code 1070 (concurrency) parallelism -1, retry with jitter
  5xx / timeout               exponential backoff, full jitter, counts
  401 / invalid_token         refresh once, then OPEN + P1, counts immediately
  4xx business error          no retry, QUARANTINED + reconciliation exception
  ==========================  ==================================================

``test_the_six_conditions_produce_six_distinguishable_dispositions`` is the one
that matters most: it asserts the six records are pairwise distinct on their
observable fields. A retry policy where two different failures look alike is
how a quota exhaustion gets mistaken for an outage at 3am, and the two failures
most likely to collapse into each other -- 429/45 and 5xx, both "Zoho said no"
-- have their own test as well.

The budget double
-----------------
:class:`FakeBudgetSession` models what the two rate-budget SQL statements *do*
-- insert-or-add-under-a-ceiling, keyed on
``(connection_id, window_kind, lane, window_start)`` -- not what the module
does. It is an independent model of the statement, which is what makes it
worth asserting against: the module is never consulted about the answer.
It also honours the scope predicate ``repo.query`` compiles into the SQL, so
a denied scope really does come back as no rows here, exactly as it would from
PostgreSQL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.integration import throttle
from app.backend.integration.throttle import (
    Action, BudgetVerdict, Circuit, CircuitState, ConnectionNotVisible, Failure,
    FailureKind, InMemoryCircuitStore, Lane, Parallelism, RetryPolicy,
    WindowKind,
)
from app.backend.pg import repo
from app.backend.pg.engine import Scope

CONNECTION = "CONN-1"
ENTITY = "E-ATHA"
T0 = datetime(2026, 9, 6, 11, 30, 15, tzinfo=timezone.utc)


# ==========================================================================
# The doubles
# ==========================================================================
class ManualClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> datetime:
        self._now = self._now + timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment
        return self._now


class FrozenRandom:
    """`random.Random` stand-in returning a fixed point of the interval.

    `fraction=1.0` returns the top of the range and `0.0` the bottom, so a
    test can pin the exact bounds of the jitter window instead of asserting
    that some number falls inside it -- which would pass for a policy that
    never jitters at all.
    """

    def __init__(self, fraction: float = 1.0) -> None:
        self.fraction = fraction
        self.calls: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.calls.append((a, b))
        return a + self.fraction * (b - a)


class FakeBudgetSession:
    """An in-process model of the two rate-budget statements.

    Implements the SQL's semantics, deliberately without reference to the
    module's own logic:

    * the reserve is an insert-or-add guarded by the lane ceiling on BOTH
      halves, so a first-ever call over the ceiling inserts nothing and a
      subsequent one that would cross the ceiling adds nothing;
    * a refused reserve returns no rows, which is how the caller learns;
    * the release subtracts, floored at zero;
    * every statement is filtered by the scope predicate ``repo.query``
      compiled into it, so a denied scope returns nothing here too.
    """

    def __init__(self, *, scope: Scope | None = None,
                 connections: dict[str, str] | None = None) -> None:
        self.scope = scope or Scope.system("SVC-THROTTLE-TEST")
        self.connections = dict(connections or {CONNECTION: ENTITY})
        self.rows: dict[tuple, int] = {}
        self.statements: list[tuple[str, dict]] = []
        self.locks_taken: list = []

    # -- scope, as the compiled predicate expresses it ---------------------
    def _visible(self, flat: str, params: dict) -> bool:
        connection_id = params.get("connection_id")
        if connection_id not in self.connections:
            return False
        if "AND FALSE" in flat:          # an empty-frozenset dimension
            return False
        for key, values in params.items():
            if key.startswith("__scope_entity"):
                return self.connections[connection_id] in values
        return True                       # compiled to TRUE

    @staticmethod
    def _key(params: dict) -> tuple:
        return (params["connection_id"], params["window_kind"],
                params["lane"], params["window_start"])

    def fetchall(self, statement, params=None):
        flat = " ".join(statement.split())
        params = dict(params or {})
        self.statements.append((flat, params))

        if not self._visible(flat, params):
            return []

        if "INSERT INTO integration_rate_budget" in flat:
            key, cost = self._key(params), int(params["cost"])
            ceiling = int(params["lane_ceiling"])
            current = self.rows.get(key)
            proposed = cost if current is None else current + cost
            if proposed > ceiling:
                return []
            self.rows[key] = proposed
            return [(proposed,)]

        if "UPDATE integration_rate_budget" in flat:
            key, cost = self._key(params), int(params["cost"])
            self.rows[key] = max(self.rows.get(key, 0) - cost, 0)
            return [(self.rows[key],)]

        if "SELECT COALESCE(b.used" in flat:
            return [(self.rows.get(self._key(params), 0),)]

        raise AssertionError("unexpected statement: " + flat)

    def fetchone(self, statement, params=None):
        rows = self.fetchall(statement, params)
        return rows[0] if rows else None

    # -- inspection helpers -----------------------------------------------
    def used(self, kind: WindowKind, lane: Lane, moment: datetime,
             connection_id: str = CONNECTION) -> int:
        key = (connection_id, kind.value, lane.value,
               throttle.window_start(kind, moment))
        return self.rows.get(key, 0)

    def issued(self, needle: str) -> list[tuple[str, dict]]:
        return [(sql, params) for sql, params in self.statements if needle in sql]


def reserve(session, clock, *, lane=Lane.POLLING, cost=1,
            scope=None, daily_ceiling=throttle.DEFAULT_DAILY_CALL_CEILING,
            connection_id=CONNECTION) -> BudgetVerdict:
    return throttle.reserve(
        session, connection_id=connection_id, lane=lane, cost=cost,
        actor="SVC-POLLER", clock=clock, scope=scope,
        daily_ceiling=daily_ceiling)


# ==========================================================================
# Windows and lane allocation
# ==========================================================================
def test_the_lane_allocation_is_sixty_thirty_ten():
    """§11.6's split, so an operator never starves behind a backfill."""
    assert throttle.LANE_ALLOCATION[Lane.POLLING] == 60
    assert throttle.LANE_ALLOCATION[Lane.OUTBOUND] == 30
    assert throttle.LANE_ALLOCATION[Lane.INTERACTIVE] == 10
    assert sum(throttle.LANE_ALLOCATION.values()) == 100


def test_the_three_lanes_never_oversubscribe_the_organisation():
    """Floored, not rounded: three lanes must not add up to 101 calls in a
    100-call minute, nor to 2001 in an ERP Standard day."""
    for window in WindowKind:
        total = sum(throttle.lane_ceiling(window, lane) for lane in Lane)
        org = (throttle.MINUTE_CALL_CEILING if window is WindowKind.MINUTE
               else throttle.DEFAULT_DAILY_CALL_CEILING)
        assert total <= org


def test_the_daily_ceiling_defaults_to_erp_standards_two_thousand():
    """The plan-derived figure, and the binding one on that plan."""
    assert throttle.DEFAULT_DAILY_CALL_CEILING == 2000
    assert throttle.MINUTE_CALL_CEILING == 100
    assert throttle.lane_ceiling(WindowKind.DAY, Lane.POLLING) == 1200
    assert throttle.lane_ceiling(WindowKind.DAY, Lane.INTERACTIVE) == 200
    assert throttle.lane_ceiling(WindowKind.MINUTE, Lane.POLLING) == 60
    assert throttle.lane_ceiling(WindowKind.MINUTE, Lane.INTERACTIVE) == 10


def test_a_tenants_own_ceiling_overrides_the_default():
    """`Capabilities.daily_call_ceiling` (C1) is the authority, not this
    module's default."""
    assert throttle.lane_ceiling(WindowKind.DAY, Lane.POLLING,
                                 daily_ceiling=10_000) == 6000


def test_window_boundaries_are_utc_whatever_zone_the_caller_is_in():
    """A day boundary that moved with the caller would let a job in one region
    reset a quota a job in another had already exhausted."""
    ist = timezone(timedelta(hours=5, minutes=30))
    same_instant = T0.astimezone(ist)
    assert throttle.window_start(WindowKind.DAY, same_instant) == \
        datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert throttle.window_start(WindowKind.MINUTE, same_instant) == \
        datetime(2026, 9, 6, 11, 30, tzinfo=timezone.utc)
    assert throttle.window_reset_at(WindowKind.DAY, same_instant) == \
        datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert throttle.window_reset_at(WindowKind.MINUTE, same_instant) == \
        datetime(2026, 9, 6, 11, 31, tzinfo=timezone.utc)


def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc():
    """Guessing UTC would silently move every boundary by the host's offset."""
    with pytest.raises(ValueError, match="timezone-aware"):
        throttle.window_start(WindowKind.DAY, datetime(2026, 9, 6, 11, 30))


def test_a_naive_clock_is_refused_by_reserve():
    class NaiveClock:
        def now(self):
            return datetime(2026, 9, 6, 11, 30)

    with pytest.raises(ValueError, match="timezone-aware"):
        reserve(FakeBudgetSession(), NaiveClock())


# ==========================================================================
# The budget lives in PostgreSQL, and is spent in two windows
# ==========================================================================
def test_a_granted_reservation_spends_from_both_windows():
    """Both windows, or neither. Tracking only the minute is how the daily
    ceiling gets discovered by exhausting it at 11am."""
    session, clock = FakeBudgetSession(), ManualClock()
    verdict = reserve(session, clock)

    assert verdict is not None and bool(verdict) is True
    assert verdict.binding is None and verdict.resume_at is None
    assert {w.kind for w in verdict.windows} == {WindowKind.DAY, WindowKind.MINUTE}
    assert session.used(WindowKind.MINUTE, Lane.POLLING, T0) == 1
    assert session.used(WindowKind.DAY, Lane.POLLING, T0) == 1


def test_the_reservation_is_one_atomic_upsert_per_window():
    """Read-test-increment as three statements is a race two Functions win
    together. It has to be one statement with a RETURNING."""
    session, clock = FakeBudgetSession(), ManualClock()
    reserve(session, clock)

    inserts = session.issued("INSERT INTO integration_rate_budget")
    assert len(inserts) == 2
    for sql, _ in inserts:
        assert "ON CONFLICT (connection_id, window_kind, lane, window_start)" in sql
        assert "DO UPDATE" in sql
        assert "RETURNING" in sql


def test_the_minute_lane_refuses_at_its_allocation_not_the_org_ceiling():
    """Polling gets 60 of the 100, and the 61st call in the minute is refused
    -- otherwise "60 polling" is a comment, not a control."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(60):
        assert reserve(session, clock)

    refused = reserve(session, clock)
    assert not refused
    assert refused.binding is WindowKind.MINUTE
    assert refused.window(WindowKind.MINUTE).used == 60
    assert refused.window(WindowKind.MINUTE).remaining == 0


def test_over_budget_is_not_an_error_and_names_when_to_resume():
    """§11.6: the job checkpoints and returns; the next cron tick resumes.
    Nothing raises and nothing sleeps."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(60):
        reserve(session, clock)

    refused = reserve(session, clock)
    assert isinstance(refused, BudgetVerdict)
    assert refused.resume_at == datetime(2026, 9, 6, 11, 31, tzinfo=timezone.utc)


def test_a_refused_reservation_releases_the_window_it_had_already_taken():
    """The day window is reserved first, so a minute refusal happens with a
    day reservation already in hand. Leaving it behind burns one of the day's
    2,000 calls for a request that was never made -- one per throttled attempt,
    which on a backfill is how a daily quota evaporates against nothing."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(60):
        reserve(session, clock)
    assert session.used(WindowKind.DAY, Lane.POLLING, T0) == 60

    refused = reserve(session, clock)
    assert not refused

    releases = session.issued("UPDATE integration_rate_budget")
    assert len(releases) == 1
    assert releases[0][1]["window_kind"] == WindowKind.DAY.value
    assert session.used(WindowKind.DAY, Lane.POLLING, T0) == 60
    # And the verdict says 60 too. Reporting the pre-release 61 would tell an
    # operator the day has spent a call it has not -- an off-by-one that
    # compounds once per throttled attempt across a backfill.
    assert refused.window(WindowKind.DAY).used == 60
    assert refused.window(WindowKind.DAY).remaining == 1140


def test_a_release_can_never_drive_a_counter_negative():
    """A negative `used` would silently hand the next caller free calls --
    the exact failure a throttle exists to prevent."""
    session, clock = FakeBudgetSession(), ManualClock()
    throttle._release(session, connection_id=CONNECTION, kind=WindowKind.DAY,
                      lane=Lane.POLLING,
                      window_start_at=throttle.window_start(WindowKind.DAY, T0),
                      cost=5, actor="SVC", now=T0, scope=None)
    assert session.used(WindowKind.DAY, Lane.POLLING, T0) == 0


def test_a_new_minute_window_starts_a_fresh_budget():
    """The row is keyed on window_start, so the rollover is free -- and the
    exhausted window is left as evidence rather than reset in place."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(60):
        reserve(session, clock)
    assert not reserve(session, clock)

    later = clock.advance(minutes=1)
    assert reserve(session, clock)
    assert session.used(WindowKind.MINUTE, Lane.POLLING, later) == 1
    assert session.used(WindowKind.MINUTE, Lane.POLLING, T0) == 60


def test_the_daily_ceiling_is_the_binding_one_on_erp_standard():
    """1,200 polling calls a day, and the minute window never gets a look in:
    100/minute would permit 144,000 a day. A design that tracks only the
    minute window never refuses at all on this plan."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(20):                       # 20 minutes x 60 = 1200
        for _ in range(60):
            assert reserve(session, clock)
        clock.advance(minutes=1)

    assert session.used(WindowKind.DAY, Lane.POLLING, T0) == 1200
    refused = reserve(session, clock)
    assert not refused
    assert refused.binding is WindowKind.DAY
    assert refused.resume_at == datetime(2026, 9, 7, tzinfo=timezone.utc)
    # The minute window was never touched: the day refused first, so there is
    # nothing to compensate and no minute budget was spent discovering it.
    assert session.used(WindowKind.MINUTE, Lane.POLLING, clock.now()) == 0
    assert session.issued("UPDATE integration_rate_budget") == []


def test_an_exhausted_polling_lane_does_not_starve_the_interactive_operator():
    """The whole reason the allocation exists: an operator clicking "Test
    connection" is answered while a backfill is at its ceiling, in BOTH
    windows."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(20):
        for _ in range(60):
            reserve(session, clock, lane=Lane.POLLING)
        clock.advance(minutes=1)

    assert not reserve(session, clock, lane=Lane.POLLING)
    assert reserve(session, clock, lane=Lane.INTERACTIVE)
    assert reserve(session, clock, lane=Lane.OUTBOUND)


def test_the_daily_lanes_are_separated_too_not_only_the_minute_ones():
    """Applying the split to the minute window alone protects the operator for
    sixty seconds and then lets a backfill eat all 2,000 daily calls by
    mid-morning. The interactive lane keeps its 200 for the whole day."""
    session, clock = FakeBudgetSession(), ManualClock()
    for _ in range(20):
        for _ in range(60):
            reserve(session, clock, lane=Lane.POLLING)
        clock.advance(minutes=1)

    for _ in range(10):
        assert reserve(session, clock, lane=Lane.INTERACTIVE)
    assert session.used(WindowKind.DAY, Lane.INTERACTIVE, clock.now()) == 10


def test_a_lane_cannot_be_bought_out_in_one_oversized_reservation():
    """The guard sits on the INSERT as well as the DO UPDATE. Without it a
    first-ever call whose cost exceeds the whole lane inserts a row over
    budget, because there is no conflict to arbitrate on that path."""
    session, clock = FakeBudgetSession(), ManualClock()
    refused = reserve(session, clock, lane=Lane.INTERACTIVE, cost=11)
    assert not refused
    assert session.used(WindowKind.DAY, Lane.INTERACTIVE, T0) == 0
    assert session.used(WindowKind.MINUTE, Lane.INTERACTIVE, T0) == 0


def test_a_reservation_must_be_for_at_least_one_call():
    with pytest.raises(ValueError, match="at least one call"):
        reserve(FakeBudgetSession(), ManualClock(), cost=0)


# ==========================================================================
# The budget goes through the scoped-query chokepoint
# ==========================================================================
def test_every_budget_statement_carries_the_literal_scope_token():
    """`repo.query` refuses SQL without it, so this is belt and braces on the
    three constants -- and it fails at review time, not at runtime."""
    for sql in (throttle._RESERVE_SQL, throttle._RELEASE_SQL, throttle._READ_SQL):
        assert repo.SCOPE_TOKEN in sql
        repo.require_scope_token(sql)


def test_the_column_mapping_names_every_dimension():
    """A dimension omitted from `columns=` compiles to TRUE and nobody
    notices. All four are named, three of them waived deliberately."""
    assert set(throttle.RATE_BUDGET_SCOPE_COLUMNS) == \
        {"entity", "plant", "project", "location"}
    assert throttle.RATE_BUDGET_SCOPE_COLUMNS["entity"] == "c.entity_id"


def test_a_scope_restricted_to_another_entity_sees_no_connection():
    """Not a refusal, not a 403 -- an invisible connection. `ConnectionNotVisible`
    covers "does not exist" and "not yours" with one exception on purpose;
    telling them apart is an existence oracle."""
    session = FakeBudgetSession(
        scope=Scope(user_id="U-OTHER", entity_ids=frozenset({"E-SOMEONE-ELSE"})))
    with pytest.raises(ConnectionNotVisible):
        reserve(session, ManualClock(), scope=session.scope)


def test_a_scope_granting_the_owning_entity_is_served():
    session = FakeBudgetSession(
        scope=Scope(user_id="U-OPS", entity_ids=frozenset({ENTITY})))
    assert reserve(session, ManualClock(), scope=session.scope)


def test_a_scope_granting_no_entity_at_all_sees_nothing():
    """An empty frozenset is "nothing", never "everything"."""
    session = FakeBudgetSession(
        scope=Scope(user_id="U-NEW", entity_ids=frozenset()))
    with pytest.raises(ConnectionNotVisible):
        reserve(session, ManualClock(), scope=session.scope)


def test_a_project_restricted_scope_is_waived_explicitly_not_by_omission():
    """The three non-entity dimensions are mapped to None, so
    `ScopeNotExpressible` is not raised -- and the waiver is a decision in the
    mapping rather than a silent widening."""
    session = FakeBudgetSession(
        scope=Scope(user_id="U-PM", entity_ids=frozenset({ENTITY}),
                    project_ids=frozenset({"P-1"})))
    assert reserve(session, ManualClock(), scope=session.scope)


# ==========================================================================
# Classification: the six conditions, told apart
# ==========================================================================
def test_the_three_rate_limit_codes_are_three_different_conditions():
    assert throttle.classify(Failure(429, 44)).kind is FailureKind.RATE_LIMIT_MINUTE
    assert throttle.classify(Failure(429, 45)).kind is FailureKind.RATE_LIMIT_DAILY
    assert throttle.classify(Failure(429, 1070)).kind is FailureKind.CONCURRENCY_LIMIT


def test_a_string_code_is_read_as_the_number_it_is():
    """Zoho's JSON has been seen with both; a quota exhaustion must not be
    reclassified by the type of its own code field."""
    assert throttle.classify(Failure(429, "45")).kind is FailureKind.RATE_LIMIT_DAILY


def test_an_unrecognised_429_code_is_flagged_rather_than_assumed():
    """Folding it into the per-minute case would claim it does not count
    toward the circuit -- disabling the breaker for a vendor condition we
    cannot read. The same discipline C3 applies to an unmapped status."""
    result = throttle.classify(Failure(429, 9999))
    assert result.kind is FailureKind.TRANSIENT
    assert result.unmapped_code is True


def test_a_5xx_and_a_timeout_are_both_transient():
    assert throttle.classify(Failure(503)).kind is FailureKind.TRANSIENT
    assert throttle.classify(Failure(timed_out=True)).kind is FailureKind.TRANSIENT
    assert throttle.classify(Failure(status=None)).kind is FailureKind.TRANSIENT


def test_invalid_token_is_auth_even_when_it_arrives_as_a_400():
    """Reading it as a business error would quarantine a record over an
    expired token -- permanently, since a business error is never retried."""
    assert throttle.classify(Failure(401)).kind is FailureKind.AUTH
    assert throttle.classify(Failure(400, "invalid_token")).kind is FailureKind.AUTH
    assert throttle.classify(
        Failure(400, 57, "the OAuth invalid_token was rejected")).kind is FailureKind.AUTH


def test_an_ordinary_4xx_is_a_business_error():
    assert throttle.classify(Failure(400, 4001, "mandatory field")).kind \
        is FailureKind.BUSINESS
    assert throttle.classify(Failure(404)).kind is FailureKind.BUSINESS


def test_a_success_is_refused_by_classify():
    """Routing a success through the retry policy manufactures an attempt
    count for a call that worked."""
    with pytest.raises(ValueError, match="not one"):
        throttle.classify(Failure(200))


# ==========================================================================
# One test per condition, forcing exactly that condition
# ==========================================================================
def decide(failure, *, attempts=1, now=T0, fraction=1.0, refreshed=False,
           policy=None):
    return (policy or RetryPolicy()).decide(
        failure, attempts=attempts, now=now, rng=FrozenRandom(fraction),
        token_refresh_already_attempted=refreshed)


def test_429_code_44_checkpoints_and_resumes_without_touching_the_circuit():
    """Our own throttle mis-metered the call. Nothing is charged for it: not
    the circuit, not the row's retry allowance."""
    d = decide(Failure(429, 44))
    assert d.action is Action.CHECKPOINT_AND_RESUME
    assert d.counts_toward_circuit is False
    assert d.counts_toward_attempts is False
    assert d.retry is True
    assert d.next_attempt_at is None
    assert d.resume_at == datetime(2026, 9, 6, 11, 31, tzinfo=timezone.utc)
    assert d.open_circuit_until is None
    assert d.terminal_state is None
    assert d.alert is None


def test_429_code_45_opens_the_circuit_until_the_next_day_boundary_and_alerts():
    """There is no backoff that helps: the quota is gone until the UTC day
    rolls. The alert is what keeps it from being read as an outage."""
    d = decide(Failure(429, 45))
    assert d.action is Action.OPEN_CIRCUIT_UNTIL_DAY_BOUNDARY
    assert d.counts_toward_circuit is False
    assert d.open_circuit_until == datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert d.resume_at == datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert d.alert == throttle.ALERT_DAILY_QUOTA_EXHAUSTED
    assert d.next_attempt_at is None
    assert d.terminal_state is None


def test_429_code_1070_reduces_parallelism_by_one_and_retries_with_jitter():
    """Too many at once, not too many in total. Backing off without narrowing
    the pipe repeats the collision more politely."""
    d = decide(Failure(429, 1070), attempts=3)
    assert d.action is Action.REDUCE_PARALLELISM_AND_RETRY
    assert d.parallelism_delta == -1
    assert d.counts_toward_circuit is False
    assert d.retry is True
    # attempts=3 -> uniform(0, min(900, 2*2^3)) = uniform(0, 16); top of range.
    assert d.next_attempt_at == T0 + timedelta(seconds=16)
    assert d.open_circuit_until is None


def test_a_5xx_backs_off_with_full_jitter_and_counts_toward_the_circuit():
    d = decide(Failure(502), attempts=3)
    assert d.action is Action.BACKOFF_AND_RETRY
    assert d.counts_toward_circuit is True
    assert d.counts_toward_attempts is True
    assert d.parallelism_delta == 0
    assert d.next_attempt_at == T0 + timedelta(seconds=16)
    assert d.resume_at is None
    assert d.alert is None


def test_a_401_refreshes_once_then_opens_the_circuit_with_a_p1():
    """"Counts toward the circuit, immediately": the second failure opens it
    on its own rather than waiting for five."""
    first = decide(Failure(401), refreshed=False)
    assert first.action is Action.REFRESH_TOKEN_AND_RETRY
    assert first.counts_toward_circuit is True
    assert first.retry is True
    # No backoff: a refresh is not a backoff situation, and sitting out four
    # minutes over an expired token stalls every job on the connection.
    assert first.next_attempt_at == T0
    assert first.open_circuit_until is None
    assert first.alert is None

    second = decide(Failure(401), refreshed=True)
    assert second.action is Action.OPEN_CIRCUIT_AND_ALERT
    assert second.counts_toward_circuit is True
    assert second.retry is False
    assert second.open_circuit_until == T0 + timedelta(seconds=60)
    assert second.alert == throttle.ALERT_AUTH_FAILED_P1


def test_a_4xx_business_error_quarantines_with_a_reconciliation_exception():
    """Retrying identical content gets an identical refusal. It never retries,
    and it never opens the circuit -- a run of bad records is our data problem,
    and breaking the connection over it would stop the good records too."""
    d = decide(Failure(400, 4001, "mandatory field missing"))
    assert d.action is Action.QUARANTINE
    assert d.retry is False
    assert d.counts_toward_circuit is False
    assert d.terminal_state == throttle.STATE_QUARANTINED
    assert d.exception_kind == throttle.BUSINESS_ERROR_EXCEPTION_KIND
    assert d.next_attempt_at is None
    assert d.open_circuit_until is None


# --------------------------------------------------------------------------
# The property that matters most
# --------------------------------------------------------------------------
SIX_CONDITIONS = {
    "429/44 per-minute": (Failure(429, 44), False),
    "429/45 daily quota": (Failure(429, 45), False),
    "429/1070 concurrency": (Failure(429, 1070), False),
    "5xx / timeout": (Failure(503), False),
    "401 second failure": (Failure(401), True),
    "4xx business": (Failure(422, 4001, "bad"), False),
}


def _observable(d):
    """Everything a caller, an operator or SCR-39 can actually see."""
    return (d.action, d.counts_toward_circuit, d.counts_toward_attempts, d.retry,
            d.next_attempt_at, d.resume_at, d.parallelism_delta,
            d.open_circuit_until, d.terminal_state, d.exception_kind, d.alert)


def test_the_six_conditions_produce_six_distinguishable_dispositions():
    """A retry policy where two different failures look alike is how a quota
    exhaustion gets mistaken for an outage at 3am. Six conditions in, six
    pairwise-distinct records out."""
    seen: dict[tuple, str] = {}
    for label, (failure, refreshed) in SIX_CONDITIONS.items():
        observable = _observable(decide(failure, refreshed=refreshed))
        assert observable not in seen, (
            "'" + label + "' is indistinguishable from '" + seen.get(observable, "")
            + "': both present as " + repr(observable))
        seen[observable] = label
    assert len(seen) == 6


def test_the_six_conditions_map_to_six_distinct_kinds():
    kinds = {decide(f, refreshed=r).kind for f, r in SIX_CONDITIONS.values()}
    assert kinds == set(FailureKind)


def test_a_daily_quota_is_never_reported_as_an_outage():
    """The two most likely to collapse into each other: both are "Zoho said
    no", and the operational responses are opposites."""
    quota = decide(Failure(429, 45))
    outage = decide(Failure(503))
    assert quota.kind is not outage.kind
    assert quota.action is not outage.action
    assert quota.counts_toward_circuit != outage.counts_toward_circuit
    assert quota.alert != outage.alert
    assert quota.resume_at is not None and outage.resume_at is None
    assert quota.next_attempt_at is None and outage.next_attempt_at is not None


def test_our_own_throttle_failing_is_never_charged_to_the_vendor():
    """The third column of §11.6's table. Three conditions are ours, and none
    of them may open the breaker."""
    for failure in (Failure(429, 44), Failure(429, 1070), Failure(400, 4001)):
        assert decide(failure).counts_toward_circuit is False


def test_only_our_own_minute_throttle_spares_the_attempt_counter():
    """A 429/44 and a 429/45 do not advance the row toward DEAD; everything
    that is a real retry does. Spending the allowance on our own arithmetic
    would march a good record to DEAD over nothing."""
    assert decide(Failure(429, 44)).counts_toward_attempts is False
    assert decide(Failure(429, 45)).counts_toward_attempts is False
    for failure in (Failure(429, 1070), Failure(503), Failure(401),
                    Failure(400, 4001)):
        assert decide(failure).counts_toward_attempts is True


# ==========================================================================
# Backoff
# ==========================================================================
def test_backoff_is_full_jitter_over_two_seconds_times_two_to_the_attempts():
    """Full jitter, not exponential-with-noise: eight rows that failed together
    otherwise retry together and re-create the burst that failed them."""
    policy = RetryPolicy()
    for attempts, interval in ((1, 4), (2, 8), (3, 16), (7, 256)):
        top = FrozenRandom(1.0)
        bottom = FrozenRandom(0.0)
        assert policy.backoff_delay(attempts, rng=top) == pytest.approx(interval)
        assert policy.backoff_delay(attempts, rng=bottom) == 0.0
        assert top.calls[-1] == (0.0, interval)


def test_the_jitter_floor_is_zero_not_half_the_interval():
    """The lower bound is what decorrelates the convoy."""
    assert RetryPolicy().backoff_delay(5, rng=FrozenRandom(0.0)) == 0.0


def test_the_nine_hundred_second_cap_binds_once_attempts_are_raised():
    """With max_attempts=8 the ceiling only reaches 256s, so the cap never
    fires in production -- it is the guard for the day someone raises the
    limit, and it is asserted rather than assumed."""
    policy = RetryPolicy()
    assert policy.backoff_delay(20, rng=FrozenRandom(1.0)) == pytest.approx(900.0)
    assert policy.backoff_delay(10_000, rng=FrozenRandom(1.0)) == pytest.approx(900.0)


def test_negative_attempts_are_refused():
    with pytest.raises(ValueError, match="negative"):
        RetryPolicy().backoff_delay(-1, rng=FrozenRandom())


def test_the_eighth_attempt_is_dead_and_visible_for_manual_retry():
    """max_attempts=8, then DEAD on SCR-39. The seventh still retries."""
    seventh = decide(Failure(503), attempts=7)
    assert seventh.action is Action.BACKOFF_AND_RETRY
    assert seventh.terminal_state is None

    eighth = decide(Failure(503), attempts=8)
    assert eighth.action is Action.GIVE_UP_DEAD
    assert eighth.retry is False
    assert eighth.terminal_state == throttle.STATE_DEAD
    assert eighth.exception_kind == throttle.ATTEMPTS_EXHAUSTED_EXCEPTION_KIND
    assert eighth.next_attempt_at is None


def test_dead_is_not_quarantined():
    """Attempt exhaustion and a refused payload are different states, and
    SCR-39 offers manual retry for one of them."""
    assert throttle.STATE_DEAD != throttle.STATE_QUARANTINED
    assert decide(Failure(503), attempts=8).terminal_state == throttle.STATE_DEAD
    assert decide(Failure(400, 4001)).terminal_state == throttle.STATE_QUARANTINED


def test_an_unmapped_rate_limit_code_still_alerts_on_the_backoff_path():
    d = decide(Failure(429, 9999))
    assert d.action is Action.BACKOFF_AND_RETRY
    assert d.unmapped_code is True
    assert d.alert == throttle.ALERT_UNMAPPED_RATE_LIMIT_CODE


def test_decide_refuses_a_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        RetryPolicy().decide(Failure(503), attempts=1,
                             now=datetime(2026, 9, 6, 11, 30),
                             rng=FrozenRandom())


# ==========================================================================
# The circuit breaker
# ==========================================================================
def counted_failure():
    return decide(Failure(503))


def test_five_counted_failures_in_sixty_seconds_open_the_circuit():
    clock = ManualClock()
    circuit = Circuit()
    for _ in range(4):
        circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
        assert circuit.state is CircuitState.CLOSED
        clock.advance(seconds=10)

    circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
    assert circuit.state is CircuitState.OPEN
    assert circuit.open_until == clock.now() + timedelta(seconds=60)


def test_four_counted_failures_leave_the_circuit_closed():
    clock = ManualClock()
    circuit = Circuit()
    for _ in range(4):
        circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
        clock.advance(seconds=5)
    assert circuit.state is CircuitState.CLOSED
    assert circuit.consecutive_failures == 4
    assert throttle.allow(circuit, clock.now())[0] is True


def test_failures_spread_wider_than_the_window_never_open_it():
    """Five failures over an hour are not a burst. Opening on them would trip
    the breaker on ordinary background noise."""
    clock = ManualClock()
    circuit = Circuit()
    for _ in range(10):
        circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
        assert circuit.state is CircuitState.CLOSED
        clock.advance(seconds=61)
    assert circuit.consecutive_failures == 1


def test_a_success_resets_the_consecutive_counter():
    """"Consecutive" is the operative word: a counter that survived a success
    would eventually open on five failures spread across a thousand good
    calls."""
    clock = ManualClock()
    circuit = Circuit()
    for _ in range(4):
        circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
    circuit = throttle.observe_success(circuit, clock.now())
    assert circuit.state is CircuitState.CLOSED
    assert circuit.consecutive_failures == 0

    circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
    assert circuit.state is CircuitState.CLOSED
    assert circuit.consecutive_failures == 1


def test_the_three_uncounted_conditions_never_move_the_breaker():
    """A minute throttle is our arithmetic, a concurrency limit is our
    parallelism, a business error is our payload. None is evidence about
    Zoho's health."""
    clock = ManualClock()
    for failure in (Failure(429, 44), Failure(429, 1070), Failure(400, 4001)):
        circuit = Circuit()
        for _ in range(50):
            circuit = throttle.observe_failure(circuit, clock.now(),
                                               decide(failure))
        assert circuit == Circuit(), "moved by " + repr(failure)


def test_a_daily_quota_opens_the_circuit_on_a_single_failure():
    """One failure, straight to OPEN, and until midnight UTC rather than for
    sixty seconds. No counting toward five."""
    circuit = throttle.observe_failure(Circuit(), T0, decide(Failure(429, 45)))
    assert circuit.state is CircuitState.OPEN
    assert circuit.open_until == datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert throttle.allow(circuit, T0)[0] is False


def test_a_second_auth_failure_opens_the_circuit_on_its_own():
    circuit = throttle.observe_failure(Circuit(), T0,
                                       decide(Failure(401), refreshed=True))
    assert circuit.state is CircuitState.OPEN
    assert circuit.open_until == T0 + timedelta(seconds=60)


def test_an_open_circuit_refuses_until_its_timer_then_allows_one_probe():
    clock = ManualClock()
    circuit = Circuit()
    for _ in range(5):
        circuit = throttle.observe_failure(circuit, clock.now(), counted_failure())
    assert circuit.state is CircuitState.OPEN

    clock.advance(seconds=59)
    allowed, circuit = throttle.allow(circuit, clock.now())
    assert allowed is False and circuit.state is CircuitState.OPEN

    clock.advance(seconds=1)
    allowed, circuit = throttle.allow(circuit, clock.now())
    assert allowed is True
    assert circuit.state is CircuitState.HALF_OPEN
    assert circuit.probe_in_flight is True


def test_half_open_permits_exactly_one_probe():
    """A caller that discards the returned circuit hands the probe out
    repeatedly -- a thundering herd against a service that has just said it is
    unwell. Asking is itself a transition, which is why `allow` returns the
    circuit."""
    circuit = Circuit(state=CircuitState.OPEN,
                      open_until=T0 - timedelta(seconds=1))
    first, circuit = throttle.allow(circuit, T0)
    second, circuit = throttle.allow(circuit, T0)
    third, circuit = throttle.allow(circuit, T0 + timedelta(minutes=5))
    assert first is True
    assert second is False
    assert third is False


def test_a_failed_probe_reopens_for_a_full_interval():
    """It does not get four more chances first."""
    circuit = Circuit(state=CircuitState.HALF_OPEN, probe_in_flight=True,
                      consecutive_failures=4)
    circuit = throttle.observe_failure(circuit, T0, counted_failure())
    assert circuit.state is CircuitState.OPEN
    assert circuit.open_until == T0 + timedelta(seconds=60)
    assert circuit.probe_in_flight is False
    assert throttle.allow(circuit, T0)[0] is False


def test_a_successful_probe_closes_the_circuit():
    circuit = Circuit(state=CircuitState.HALF_OPEN, probe_in_flight=True)
    circuit = throttle.observe_success(circuit, T0)
    assert circuit.state is CircuitState.CLOSED
    assert circuit.probe_in_flight is False
    assert throttle.allow(circuit, T0)[0] is True


def test_a_closed_circuit_always_allows():
    assert throttle.allow(Circuit(), T0) == (True, Circuit())


def test_the_circuit_thresholds_are_the_plans_numbers():
    policy = RetryPolicy()
    assert policy.circuit_failure_threshold == 5
    assert policy.circuit_failure_window_seconds == 60.0
    assert policy.circuit_open_seconds == 60.0
    assert policy.max_attempts == 8


def test_the_circuit_is_a_value_that_survives_a_round_trip():
    """AppSail reclaims the instance; a daily-quota circuit must outlive it.
    The state is a plain value with no lifetime, so a store can write it."""
    store = InMemoryCircuitStore()
    assert store.load(CONNECTION) == Circuit()
    opened = throttle.observe_failure(Circuit(), T0, decide(Failure(429, 45)))
    store.save(CONNECTION, opened)
    assert store.load(CONNECTION) == opened
    assert store.load("CONN-OTHER") == Circuit()


# ==========================================================================
# Parallelism
# ==========================================================================
def test_a_concurrency_limit_narrows_the_pipe_by_one():
    parallelism = Parallelism(current=4)
    reduced = parallelism.apply(decide(Failure(429, 1070)))
    assert reduced.current == 3
    assert parallelism.current == 4          # immutable; the original stands


def test_parallelism_never_falls_below_its_floor():
    """At zero the integration stops entirely, which is worse than slow."""
    parallelism = Parallelism(current=1)
    for _ in range(10):
        parallelism = parallelism.apply(decide(Failure(429, 1070)))
    assert parallelism.current == 1


def test_only_the_concurrency_condition_moves_parallelism():
    parallelism = Parallelism(current=3)
    for failure, refreshed in SIX_CONDITIONS.values():
        d = decide(failure, refreshed=refreshed)
        if d.kind is FailureKind.CONCURRENCY_LIMIT:
            continue
        assert parallelism.apply(d) == parallelism


def test_recovery_is_one_step_at_a_time_and_capped():
    parallelism = Parallelism(current=1, ceiling=3)
    assert parallelism.restored().current == 2
    assert parallelism.restored().restored().current == 3
    assert parallelism.restored().restored().restored().current == 3


def test_a_parallelism_outside_its_own_bounds_is_refused():
    with pytest.raises(ValueError, match="outside"):
        Parallelism(current=9, ceiling=4)
    with pytest.raises(ValueError, match="at least 1"):
        Parallelism(current=0, floor=0)
    with pytest.raises(ValueError, match="below its floor"):
        Parallelism(current=2, floor=2, ceiling=1)


# ==========================================================================
# Reporting
# ==========================================================================
def test_alert_fields_are_produced_only_when_an_alert_is_due():
    assert throttle.alert_fields(decide(Failure(503)),
                                 connection_id=CONNECTION) == {}
    fields = throttle.alert_fields(decide(Failure(429, 45)),
                                   connection_id=CONNECTION, correlation_id="C-1")
    assert fields["connection_id"] == CONNECTION
    assert fields["failure_kind"] == FailureKind.RATE_LIMIT_DAILY.value
    assert fields["open_until"] == "2026-09-07T00:00:00+00:00"
    assert fields["correlation_id"] == "C-1"


def test_the_auth_p1_and_the_quota_alert_are_different_conditions():
    """One is a page, the other is a plan-capacity problem. Naming them the
    same would put a quota exhaustion on the 3am rota."""
    p1 = throttle.alert_fields(decide(Failure(401), refreshed=True),
                               connection_id=CONNECTION)
    quota = throttle.alert_fields(decide(Failure(429, 45)),
                                  connection_id=CONNECTION)
    assert p1["failure_kind"] != quota["failure_kind"]
    assert throttle.ALERT_AUTH_FAILED_P1 != throttle.ALERT_DAILY_QUOTA_EXHAUSTED


# ==========================================================================
# No live anything
# ==========================================================================
def test_this_module_opens_no_socket_and_sleeps_for_nothing():
    """The standing Wave 5 boundary, asserted on source: no tenant, no cloud,
    no sleeping test."""
    import inspect

    source = inspect.getsource(throttle)
    for banned in ("import requests", "import httpx", "import socket",
                   "urllib.request", "time.sleep", "asyncio.sleep"):
        assert banned not in source, banned + " in throttle.py"


def test_the_module_reads_no_clock_it_was_not_handed():
    """Every decision point takes a `Clock` or a `now`.

    Asserted on the AST, not on the text, so that prose in a docstring saying
    "use datetime.now(timezone.utc)" does not count and a real call cannot
    hide inside a string. The only `datetime.now()` CALL in the module is
    `SystemClock.now`'s own body -- everywhere else, time is an argument.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(throttle))
    owners: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "now"
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "datetime"):
                owners.append(node.name)
    assert owners == ["now"], owners
    assert "datetime.now(timezone.utc)" in inspect.getsource(throttle.SystemClock)


def test_replace_keeps_a_circuit_frozen():
    """The dataclasses are frozen so a caller cannot mutate shared state into
    a different verdict after the fact."""
    circuit = Circuit()
    with pytest.raises(Exception):
        circuit.state = CircuitState.OPEN          # type: ignore[misc]
    assert replace(circuit, state=CircuitState.OPEN).state is CircuitState.OPEN
