"""Rate budgeting, retry, backoff and the circuit breaker (plan §11.6).

Wave 5 stream 4. Nothing in this module opens a socket, sleeps, or reads a
wall clock it was not handed. It answers three questions and nothing else:

1. **May I spend a call right now?**  :func:`reserve` -- and the budget it
   spends lives in PostgreSQL, not in this process.
2. **What just happened, and what does it mean?**  :func:`classify` then
   :meth:`RetryPolicy.decide`.
3. **Is the connection healthy enough to try at all?**  :class:`Circuit` and
   its pure transitions.

Why the budget is a table and not a counter
-------------------------------------------
AppSail scales to zero and reclaims an instance after five minutes of
inactivity (§2.1). There is no resident process, so there is no place to hold
a token bucket: two cron ticks a minute apart are two different containers, and
between them the only thing that remembers anything is the database. The budget
is therefore an ``integration_rate_budget`` row, reserved with a single
``INSERT ... ON CONFLICT ... DO UPDATE ... WHERE ... RETURNING`` so that the
read, the test against the ceiling and the increment are one atomic statement.
Two concurrent Functions racing for the last call in the window cannot both
win, because only one of them gets a row back.

**Over budget is not an error.** :func:`reserve` returns a falsy
:class:`BudgetVerdict`; the job checkpoints and returns, and the next cron tick
resumes from the checkpoint. Nothing raises, nothing retries in place, nothing
sleeps -- a job that sleeps inside a 15-minute Function is a job that gets
killed holding a claim.

Two windows, and the second is the one people forget
----------------------------------------------------
A per-minute ceiling of 100 calls/organisation, **and a daily ceiling that on
Zoho ERP Standard is 2,000 calls**. On that plan the DAILY budget is the
binding constraint: 100/minute would permit 144,000 calls a day, so the minute
window will essentially never be what stops you, and a design that tracks only
the minute window will discover the day window by exhausting it at 11am. Both
are tracked, with their own rows, and a verdict says which one refused.

The lane allocation, and why it applies to both windows
--------------------------------------------------------
Per organisation: **60 polling / 30 outbound / 10 interactive**
(:data:`LANE_ALLOCATION`), so an operator clicking "Test connection" never
starves behind a backfill. The plan states the split against the per-minute
figure. It is applied here to the daily window **at the same ratio**, which is
a decision and is recorded as one: on ERP Standard the day is the binding
window, so a split that protects the interactive lane for sixty seconds but
lets a backfill eat all 2,000 daily calls by mid-morning does not protect the
operator at all -- it just moves the starvation from minutes to hours. The
ratio lives in one place and both windows read it.

What this module needs from stream 2 (REPORTED, not made)
----------------------------------------------------------
``migrations/pg/010_integration.sql`` is stream 2's file. C2 freezes
``integration_rate_budget(connection_id, window_kind, window_start, used, ...)``.
This module writes the columns below, and the trailing ``...`` of the frozen
signature is where three of them have to live:

* ``lane text NOT NULL`` -- ``POLLING|OUTBOUND|INTERACTIVE``, **part of the
  key**. A single ``used`` counter per window cannot express "polling is capped
  at 60": enforcing a per-lane ceiling requires knowing what that lane spent.
  Without this column the allocation above is undeliverable, and the honest
  consequence is that a backfill can starve an operator.
* ``created_by text NOT NULL`` / ``updated_by text``, ``updated_at timestamptz``
  -- the audit columns every other table in this schema carries.
* ``UNIQUE (connection_id, window_kind, lane, window_start)`` -- the conflict
  target the atomic reserve arbitrates on. Without it the upsert is not atomic
  and two Functions can both spend the last call.

The circuit breaker likewise needs somewhere durable to live -- see
:class:`CircuitStore`. It is expressed here as a port with an explicitly
non-durable in-memory implementation, precisely so that shipping without the
durable one is visible rather than silent.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from ..pg import repo
from ..pg.engine import Scope, Session

# ===========================================================================
# Clocks. Injected, always -- never `datetime.now()` at a decision point.
# ===========================================================================


class Clock(Protocol):
    """The only source of "now" this module will accept."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """The real clock. Always UTC-aware."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _require_aware(moment: datetime, *, what: str) -> datetime:
    """Refuse a naive datetime rather than guessing its zone.

    A naive timestamp here would silently shift every window boundary by the
    host's offset, which on a machine in IST would put the "daily" reset at
    18:30 UTC and make a quota-exhaustion circuit reopen five and a half hours
    early. Guessing UTC would hide that; refusing surfaces it at the call site.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            what + " must be timezone-aware; a naive datetime would move every "
            "window boundary by the host's offset. Use "
            "datetime.now(timezone.utc) or an injected Clock.")
    return moment


# ===========================================================================
# Lanes, windows and ceilings
# ===========================================================================


class Lane(str, Enum):
    """Who is spending the call.

    The lane is not a label: it is the unit the ceiling is enforced against,
    which is the whole point of the allocation.
    """

    POLLING = "POLLING"
    OUTBOUND = "OUTBOUND"
    INTERACTIVE = "INTERACTIVE"


class WindowKind(str, Enum):
    """C2 freezes this enum: ``window_kind in (MINUTE, DAY)``."""

    MINUTE = "MINUTE"
    DAY = "DAY"


#: Per-organisation allocation, as parts of 100 (plan §11.6). Both windows read
#: it -- see the module docstring for why the daily window is included.
LANE_ALLOCATION: Mapping[Lane, int] = {
    Lane.POLLING: 60,
    Lane.OUTBOUND: 30,
    Lane.INTERACTIVE: 10,
}

#: Documented Zoho per-minute ceiling, per organisation.
MINUTE_CALL_CEILING = 100

#: Zoho ERP **Standard** plan daily ceiling. Plan-derived, and the binding
#: constraint on that plan. Callers should pass the tenant's real figure from
#: ``Capabilities.daily_call_ceiling`` (C1) rather than lean on this default.
DEFAULT_DAILY_CALL_CEILING = 2000

_ALLOCATION_TOTAL = 100


def lane_ceiling(window: WindowKind, lane: Lane, *,
                 daily_ceiling: int = DEFAULT_DAILY_CALL_CEILING) -> int:
    """This lane's share of `window`'s ceiling, floored to a whole call.

    Floored, never rounded: the sum of the three lanes must not exceed the
    organisation's ceiling, and rounding up would let the three lanes together
    spend 101 calls in a 100-call minute.
    """
    total = MINUTE_CALL_CEILING if window is WindowKind.MINUTE else int(daily_ceiling)
    if total < 0:
        raise ValueError("a call ceiling cannot be negative; got " + repr(total))
    return total * LANE_ALLOCATION[lane] // _ALLOCATION_TOTAL


def window_start(kind: WindowKind, moment: datetime) -> datetime:
    """The start of the `kind` window containing `moment`, in UTC.

    Truncation happens **after** conversion to UTC, so a caller in any zone
    lands on the same row as every other caller for that organisation. A day
    boundary that moved with the caller would let a job in one region reset a
    quota that a job in another region had already exhausted.
    """
    moment = _require_aware(moment, what="window instant").astimezone(timezone.utc)
    if kind is WindowKind.MINUTE:
        return moment.replace(second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def window_reset_at(kind: WindowKind, moment: datetime) -> datetime:
    """When the `kind` window containing `moment` ends and a fresh one begins.

    This is the answer to "when may the job resume?" for a 429/44, and to
    "until when is the circuit open?" for a 429/45.
    """
    start = window_start(kind, moment)
    if kind is WindowKind.MINUTE:
        return start + timedelta(minutes=1)
    return start + timedelta(days=1)


# ===========================================================================
# The budget verdict
# ===========================================================================


@dataclass(frozen=True)
class WindowState:
    """What one (window, lane) row looked like after a reserve attempt."""

    kind: WindowKind
    lane: Lane
    window_start: datetime
    used: int
    ceiling: int

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.used)

    @property
    def resets_at(self) -> datetime:
        return window_reset_at(self.kind, self.window_start)


@dataclass(frozen=True)
class BudgetVerdict:
    """The answer to "may I spend a call?".

    Falsy when refused, so ``if not throttle.reserve(...): checkpoint()`` reads
    the way the rule does. A refusal is **not an exception** -- §11.6 says the
    job checkpoints and returns, and the next cron tick resumes.
    """

    granted: bool
    lane: Lane
    cost: int
    windows: tuple[WindowState, ...]
    #: Which window refused. ``None`` exactly when `granted`.
    binding: WindowKind | None = None

    def __bool__(self) -> bool:
        return self.granted

    def window(self, kind: WindowKind) -> WindowState | None:
        for state in self.windows:
            if state.kind is kind:
                return state
        return None

    @property
    def resume_at(self) -> datetime | None:
        """When the refusing window resets. ``None`` when granted.

        This is what the job writes into its checkpoint -- not a sleep target,
        a "do not bother before" marker for the next tick.
        """
        if self.granted or self.binding is None:
            return None
        state = self.window(self.binding)
        return None if state is None else state.resets_at


class ConnectionNotVisible(LookupError):
    """The connection does not exist, or is out of the caller's scope.

    Deliberately one exception for both. Distinguishing them would be an
    existence oracle: it would tell a caller who may not see a connection that
    the connection is nevertheless there. The same reasoning `repo.query`
    applies to rows applies here.
    """


# ---------------------------------------------------------------------------
# Scope mapping for the rate-budget tables
# ---------------------------------------------------------------------------
#: `integration_rate_budget` carries no scope dimension of its own; it is
#: reachable from one, through `integration_connection.entity_id` (C2). Every
#: statement below therefore joins the connection and maps `entity` to it.
#:
#: The other three dimensions are waived EXPLICITLY (mapped to None) rather
#: than omitted, which `repo.compile_scope` requires and which is the point:
#: an omission compiles to TRUE and nobody notices, a waiver is a decision
#: somebody signed. The decision here is that an integration connection is an
#: entity-level object -- it has no plant, project or location -- so there is
#: no column those dimensions could filter on and no narrower predicate to
#: write. A principal restricted to a project sees the connections of the
#: entities it is granted, which is the same answer the connection screen gives.
RATE_BUDGET_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "c.entity_id",
    "plant": None,
    "project": None,
    "location": None,
}


# The atomic reserve.
#
# One statement, so that the read of `used`, the test against the ceiling and
# the increment cannot be interleaved. Two Functions racing for the last call
# in a window both execute this; exactly one gets a row back, because the
# second one's `ON CONFLICT ... WHERE` sees the first one's increment.
#
# Both halves guard the ceiling, and they have to:
#   * the `WHERE ... <= lane_ceiling` on the SELECT stops a first-ever call
#     whose cost already exceeds the lane's whole allocation from inserting a
#     row over budget (there is no conflict to arbitrate on that path);
#   * the `WHERE` on the DO UPDATE stops every subsequent one.
# Dropping either leaves a hole that only shows up under exactly one ordering.
#
# Every parameter is cast explicitly. In an `INSERT ... SELECT` nothing gives
# PostgreSQL a column to infer a bare placeholder's type from, and
# `%(cost)s <= %(lane_ceiling)s` compares two placeholders to each other with
# no typed column anywhere near them -- "could not determine data type of
# parameter" is a failure that appears only against a real server, which is
# the one place this suite cannot reach.
#
# No f-string: the literal `{scope}` token must survive to `repo.query`.
_RESERVE_SQL = """
INSERT INTO integration_rate_budget AS b
            (connection_id, window_kind, lane, window_start, used,
             created_by, updated_at, updated_by)
SELECT c.connection_id, %(window_kind)s::text, %(lane)s::text,
       %(window_start)s::timestamptz, %(cost)s::int,
       %(actor)s::text, %(now)s::timestamptz, %(actor)s::text
  FROM integration_connection c
 WHERE c.connection_id = %(connection_id)s
   AND %(cost)s::int <= %(lane_ceiling)s::int
   AND {scope}
ON CONFLICT (connection_id, window_kind, lane, window_start) DO UPDATE
   SET used       = b.used + EXCLUDED.used,
       updated_at = %(now)s::timestamptz,
       updated_by = %(actor)s::text
 WHERE b.used + EXCLUDED.used <= %(lane_ceiling)s::int
RETURNING b.used
"""

# Compensation for a partially-taken reservation. See `reserve`.
#
# GREATEST(..., 0) because a release must never drive a counter negative: a
# negative `used` would silently hand the next caller free calls, which is the
# failure mode a throttle exists to prevent.
_RELEASE_SQL = """
UPDATE integration_rate_budget AS b
   SET used       = GREATEST(b.used - %(cost)s::int, 0),
       updated_at = %(now)s::timestamptz,
       updated_by = %(actor)s::text
  FROM integration_connection c
 WHERE c.connection_id = b.connection_id
   AND b.connection_id = %(connection_id)s
   AND b.window_kind   = %(window_kind)s
   AND b.lane          = %(lane)s
   AND b.window_start  = %(window_start)s
   AND {scope}
RETURNING b.used
"""

# What the refusing window currently holds, so a refusal can say how much is
# left and when it resets rather than just "no". Also the probe that tells a
# refusal apart from an invisible connection: this returns a row (with used 0)
# for a visible connection that has never spent a call in this window.
_READ_SQL = """
SELECT COALESCE(b.used, 0)
  FROM integration_connection c
  LEFT JOIN integration_rate_budget b
         ON b.connection_id = c.connection_id
        AND b.window_kind   = %(window_kind)s
        AND b.lane          = %(lane)s
        AND b.window_start  = %(window_start)s
 WHERE c.connection_id = %(connection_id)s
   AND {scope}
"""

#: Reserved in this order deliberately. On ERP Standard the DAY window is the
#: binding one, so trying it first means the common exhaustion case refuses
#: without ever touching the minute window -- and there is nothing to
#: compensate. The order also decides which window can be left holding a
#: reservation for a call that never happens; see `reserve`.
_RESERVE_ORDER = (WindowKind.DAY, WindowKind.MINUTE)


def reserve(session: Session, *, connection_id: str, lane: Lane, cost: int = 1,
            actor: str, clock: Clock, scope: Scope | None = None,
            daily_ceiling: int = DEFAULT_DAILY_CALL_CEILING) -> BudgetVerdict:
    """Atomically reserve `cost` calls in **both** windows, or reserve nothing.

    Returns a falsy :class:`BudgetVerdict` when either window refuses. It does
    not raise, does not sleep and does not retry: over budget is a normal
    outcome, and the job's response to it is to checkpoint and return.

    The two windows are two statements, so a reservation can be taken in the
    day window and then refused by the minute window. That partial reservation
    is **released**, not left behind. Leaving it would burn a call from the
    day's 2,000 for a request that was never made -- a leak of exactly one
    call per throttled attempt, which on a busy backfill is how a daily quota
    evaporates against nothing.
    """
    if cost < 1:
        raise ValueError("a reservation must be for at least one call; got "
                         + repr(cost))
    now = _require_aware(clock.now(), what="clock.now()")

    taken: list[tuple[WindowKind, datetime]] = []
    states: list[WindowState] = []

    for kind in _RESERVE_ORDER:
        start = window_start(kind, now)
        ceiling = lane_ceiling(kind, lane, daily_ceiling=daily_ceiling)
        row = repo.query_one(
            session, _RESERVE_SQL,
            {"connection_id": connection_id, "window_kind": kind.value,
             "lane": lane.value, "window_start": start, "cost": cost,
             "lane_ceiling": ceiling, "actor": actor, "now": now},
            scope=scope, columns=RATE_BUDGET_SCOPE_COLUMNS)

        if row is not None:
            taken.append((kind, start))
            states.append(WindowState(kind=kind, lane=lane, window_start=start,
                                      used=int(row[0]), ceiling=ceiling))
            continue

        # Refused. Give back whatever was already taken FIRST -- before the
        # read that works out why -- so that no path between here and the
        # return can leave a reservation standing for a call that will not be
        # made.
        released: dict[WindowKind, int] = {}
        for done_kind, done_start in taken:
            released[done_kind] = _release(
                session, connection_id=connection_id, kind=done_kind,
                lane=lane, window_start_at=done_start, cost=cost,
                actor=actor, now=now, scope=scope)
        # The states recorded on the way in are now stale for any window that
        # was rolled back: they hold the count including a reservation that no
        # longer exists. Reporting that would tell an operator the day has
        # spent a call it has not.
        states = [replace(state, used=released[state.kind])
                  if state.kind in released else state
                  for state in states]

        # Refused, or invisible? One scoped read tells those apart, and it
        # only runs on the refusal path.
        used = _read_used(session, connection_id=connection_id, kind=kind,
                          lane=lane, window_start_at=start, scope=scope)
        states.append(WindowState(kind=kind, lane=lane, window_start=start,
                                  used=used, ceiling=ceiling))
        return BudgetVerdict(granted=False, lane=lane, cost=cost,
                             windows=tuple(states), binding=kind)

    return BudgetVerdict(granted=True, lane=lane, cost=cost,
                         windows=tuple(states), binding=None)


def _read_used(session: Session, *, connection_id: str, kind: WindowKind,
               lane: Lane, window_start_at: datetime,
               scope: Scope | None) -> int:
    row = repo.query_one(
        session, _READ_SQL,
        {"connection_id": connection_id, "window_kind": kind.value,
         "lane": lane.value, "window_start": window_start_at},
        scope=scope, columns=RATE_BUDGET_SCOPE_COLUMNS)
    if row is None:
        raise ConnectionNotVisible(
            "no integration_connection " + repr(connection_id)
            + " is visible to this scope")
    return int(row[0])


def _release(session: Session, *, connection_id: str, kind: WindowKind,
             lane: Lane, window_start_at: datetime, cost: int, actor: str,
             now: datetime, scope: Scope | None) -> int:
    """Give `cost` calls back to a window, and report what it now holds."""
    row = repo.query_one(
        session, _RELEASE_SQL,
        {"connection_id": connection_id, "window_kind": kind.value,
         "lane": lane.value, "window_start": window_start_at, "cost": cost,
         "actor": actor, "now": now},
        scope=scope, columns=RATE_BUDGET_SCOPE_COLUMNS)
    return 0 if row is None else int(row[0])


# ===========================================================================
# Classification: the six conditions, kept apart
# ===========================================================================


class FailureKind(str, Enum):
    """The six rows of §11.6's response table, and nothing else.

    They are separate members because they demand separate behaviour. Folding
    any two together is how a quota exhaustion gets read as an outage at 3am:
    ``RATE_LIMIT_DAILY`` means "we have no calls left until midnight UTC and
    someone should know", ``TRANSIENT`` means "Zoho is unwell, back off and
    try again" -- opposite operational responses to superficially similar
    HTTP.
    """

    RATE_LIMIT_MINUTE = "RATE_LIMIT_MINUTE"      # 429, Zoho code 44
    RATE_LIMIT_DAILY = "RATE_LIMIT_DAILY"        # 429, Zoho code 45
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"      # 429, Zoho code 1070
    TRANSIENT = "TRANSIENT"                      # 5xx or timeout
    AUTH = "AUTH"                                # 401 / invalid_token
    BUSINESS = "BUSINESS"                        # any other 4xx


#: Zoho's documented rate-limit codes. Named, because `44` in a branch is
#: unreadable and `45` next to it is a bug waiting to be written.
ZOHO_CODE_PER_MINUTE = 44
ZOHO_CODE_DAILY_QUOTA = 45
ZOHO_CODE_CONCURRENCY = 1070

_RATE_LIMIT_CODES: Mapping[int, FailureKind] = {
    ZOHO_CODE_PER_MINUTE: FailureKind.RATE_LIMIT_MINUTE,
    ZOHO_CODE_DAILY_QUOTA: FailureKind.RATE_LIMIT_DAILY,
    ZOHO_CODE_CONCURRENCY: FailureKind.CONCURRENCY_LIMIT,
}


@dataclass(frozen=True)
class Failure:
    """One failed call, as the transport saw it.

    `code` is Zoho's body-level code, which is what distinguishes the three
    429s from each other. `timed_out` covers the case with no status at all.
    """

    status: int | None = None
    code: int | str | None = None
    message: str = ""
    timed_out: bool = False

    @property
    def code_as_int(self) -> int | None:
        try:
            return int(self.code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Classification:
    kind: FailureKind
    #: True when a 429 arrived carrying a code we do not recognise. The kind
    #: is then a fallback, and saying so is the difference between a decision
    #: and a guess.
    unmapped_code: bool = False


def classify(failure: Failure) -> Classification:
    """Map a failed call onto exactly one row of the response table.

    Order matters. ``invalid_token`` is checked before the generic 4xx branch
    because Zoho has been observed returning it with a 400 as well as a 401,
    and reading it as a business error would quarantine a record over an
    expired token -- permanently, since a business error is not retried.

    An unrecognised 429 code is classified ``TRANSIENT`` and **flagged**. It is
    not silently folded into the per-minute case: the per-minute case does not
    count toward the circuit (§11.6 -- "our own throttle failed, not the
    vendor"), and claiming that about a code we cannot read would disable the
    breaker for an unknown vendor condition. Treating it as the vendor's
    problem is the conservative reading, and `unmapped_code` makes it visible
    rather than assumed -- the same discipline C3 applies to an unmapped
    external status.
    """
    if failure.timed_out or failure.status is None:
        return Classification(FailureKind.TRANSIENT)

    status = int(failure.status)
    if status < 400:
        raise ValueError(
            "classify() is for failures; HTTP " + str(status) + " is not one. "
            "A success must not be routed through the retry policy -- it would "
            "manufacture an attempt count for a call that worked.")

    if status >= 500:
        return Classification(FailureKind.TRANSIENT)

    if status == 429:
        mapped = _RATE_LIMIT_CODES.get(failure.code_as_int)  # type: ignore[arg-type]
        if mapped is not None:
            return Classification(mapped)
        return Classification(FailureKind.TRANSIENT, unmapped_code=True)

    if status == 401 or _looks_like_invalid_token(failure):
        return Classification(FailureKind.AUTH)

    return Classification(FailureKind.BUSINESS)


def _looks_like_invalid_token(failure: Failure) -> bool:
    haystack = (str(failure.code or "") + " " + (failure.message or "")).lower()
    return "invalid_token" in haystack


# ===========================================================================
# Dispositions: what each condition actually does
# ===========================================================================


class Action(str, Enum):
    """The observable consequence. One per row of the table, no sharing."""

    CHECKPOINT_AND_RESUME = "CHECKPOINT_AND_RESUME"
    OPEN_CIRCUIT_UNTIL_DAY_BOUNDARY = "OPEN_CIRCUIT_UNTIL_DAY_BOUNDARY"
    REDUCE_PARALLELISM_AND_RETRY = "REDUCE_PARALLELISM_AND_RETRY"
    BACKOFF_AND_RETRY = "BACKOFF_AND_RETRY"
    REFRESH_TOKEN_AND_RETRY = "REFRESH_TOKEN_AND_RETRY"
    OPEN_CIRCUIT_AND_ALERT = "OPEN_CIRCUIT_AND_ALERT"
    QUARANTINE = "QUARANTINE"
    GIVE_UP_DEAD = "GIVE_UP_DEAD"


#: Terminal row states (C16 is stream 3's registry; these are the two §11.6
#: names). DEAD is attempt exhaustion; QUARANTINED is a refusal that retrying
#: cannot fix.
STATE_DEAD = "DEAD"
STATE_QUARANTINED = "QUARANTINED"

#: Reconciliation-exception kinds raised alongside a terminal disposition.
#: Overridable on `RetryPolicy` so that stream 3's C16/C17 registry, when it
#: lands, is the authority rather than these strings.
BUSINESS_ERROR_EXCEPTION_KIND = "INTEGRATION_BUSINESS_ERROR"
ATTEMPTS_EXHAUSTED_EXCEPTION_KIND = "INTEGRATION_ATTEMPTS_EXHAUSTED"

#: Alert conditions. The daily-quota one is not an outage and must not be
#: paged as one; the auth one is a P1 because nothing at all will work.
ALERT_DAILY_QUOTA_EXHAUSTED = "INTEGRATION_DAILY_QUOTA_EXHAUSTED"
ALERT_AUTH_FAILED_P1 = "INTEGRATION_AUTH_FAILED_P1"
ALERT_UNMAPPED_RATE_LIMIT_CODE = "INTEGRATION_UNMAPPED_RATE_LIMIT_CODE"


@dataclass(frozen=True)
class Disposition:
    """Everything the caller does about one failure, and nothing implied.

    Every field is set for every kind, so no two rows of the table can produce
    the same record by omission. That is the property the tests hold: six
    conditions, six distinguishable dispositions.
    """

    kind: FailureKind
    action: Action
    counts_toward_circuit: bool
    #: Whether this failure advances the `integration_outbox.attempts` counter
    #: toward `max_attempts`. A 429/44 does NOT: our own throttle mis-metered
    #: the call, and spending the row's retry allowance on our own arithmetic
    #: would march a perfectly good record to DEAD.
    counts_toward_attempts: bool
    retry: bool
    #: When to try again, for the backoff paths. `None` when the next attempt
    #: is governed by a window boundary (`resume_at`) or when there is none.
    next_attempt_at: datetime | None = None
    #: When the blocking window reopens, for the two throttle paths.
    resume_at: datetime | None = None
    parallelism_delta: int = 0
    open_circuit_until: datetime | None = None
    terminal_state: str | None = None
    exception_kind: str | None = None
    alert: str | None = None
    attempts: int = 0
    unmapped_code: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    """§11.6's numbers, in one place, all overridable for a test.

    ``next_attempt_at = now() + random(0, min(900s, 2s * 2^attempts))``,
    ``max_attempts = 8``, then DEAD and visible on SCR-39 for manual retry.
    Circuit: 5 consecutive counted failures in 60s -> OPEN 60s -> HALF_OPEN
    single probe.
    """

    max_attempts: int = 8
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 900.0
    circuit_failure_threshold: int = 5
    circuit_failure_window_seconds: float = 60.0
    circuit_open_seconds: float = 60.0
    business_exception_kind: str = BUSINESS_ERROR_EXCEPTION_KIND
    exhausted_exception_kind: str = ATTEMPTS_EXHAUSTED_EXCEPTION_KIND

    # -------------------------------------------------------------- backoff
    def backoff_delay(self, attempts: int, *, rng: random.Random) -> float:
        """Full jitter: uniform over ``[0, min(cap, base * 2^attempts))``.

        Full jitter, not "exponential with a bit of noise". The point is to
        break the convoy: eight outbox rows that failed together will otherwise
        retry together, re-creating the burst that failed them. Spreading them
        uniformly across the whole interval is what decorrelates them, and it
        is why the lower bound is 0 rather than half the interval.

        Note honestly: with ``max_attempts=8`` the ceiling reaches only
        ``2 * 2^7 = 256s``, so the 900s cap never actually binds. It is kept
        because it is the plan's number and because it is the guard that
        matters if `max_attempts` is ever raised.
        """
        if attempts < 0:
            raise ValueError("attempts cannot be negative; got " + repr(attempts))
        # Bounded before exponentiating: 2 ** 10000 is a number no test wants
        # to compute, and the cap makes every exponent past ~10 identical.
        exponent = min(attempts, 32)
        interval = min(self.backoff_cap_seconds,
                       self.backoff_base_seconds * (2 ** exponent))
        return rng.uniform(0.0, interval)

    def next_attempt_at(self, attempts: int, *, now: datetime,
                        rng: random.Random) -> datetime:
        return now + timedelta(seconds=self.backoff_delay(attempts, rng=rng))

    # -------------------------------------------------------------- decide
    def decide(self, failure: Failure, *, attempts: int, now: datetime,
               rng: random.Random,
               token_refresh_already_attempted: bool = False) -> Disposition:
        """Turn one failure into one disposition.

        `attempts` counts the failed attempts for this unit of work **including
        the one being decided** -- the shape `integration_outbox.attempts`
        already has, incremented before the policy is consulted. So the first
        failure arrives as ``attempts=1``, and ``attempts >= max_attempts``
        is exhaustion.

        Pure. It reads no clock and no global state: `now` and `rng` are
        arguments precisely so a test can force an exact `next_attempt_at`
        instead of asserting that some number is roughly in some range.
        """
        now = _require_aware(now, what="decide(now=...)")
        classification = classify(failure)
        kind = classification.kind

        if kind is FailureKind.RATE_LIMIT_MINUTE:
            # OUR throttle mis-metered, not Zoho's outage. Nothing is counted:
            # not the circuit, not the attempts. The job checkpoints and the
            # next tick picks it up when the minute rolls over.
            return Disposition(
                kind=kind, action=Action.CHECKPOINT_AND_RESUME,
                counts_toward_circuit=False, counts_toward_attempts=False,
                retry=True, resume_at=window_reset_at(WindowKind.MINUTE, now),
                attempts=attempts)

        if kind is FailureKind.RATE_LIMIT_DAILY:
            # There is no backoff that helps: the quota is gone until the UTC
            # day rolls. Opening the circuit stops every other job in this
            # organisation from spending its retries discovering the same
            # thing, and the alert is what stops it being read as an outage.
            boundary = window_reset_at(WindowKind.DAY, now)
            return Disposition(
                kind=kind, action=Action.OPEN_CIRCUIT_UNTIL_DAY_BOUNDARY,
                counts_toward_circuit=False, counts_toward_attempts=False,
                retry=True, resume_at=boundary, open_circuit_until=boundary,
                alert=ALERT_DAILY_QUOTA_EXHAUSTED, attempts=attempts)

        if kind is FailureKind.CONCURRENCY_LIMIT:
            # Too many calls at once, not too many in total. The fix is to
            # narrow the pipe by one and try again; backing off without
            # narrowing it just repeats the collision more politely.
            return Disposition(
                kind=kind, action=Action.REDUCE_PARALLELISM_AND_RETRY,
                counts_toward_circuit=False, counts_toward_attempts=True,
                retry=True,
                next_attempt_at=self.next_attempt_at(attempts, now=now, rng=rng),
                parallelism_delta=-1, attempts=attempts)

        if kind is FailureKind.AUTH:
            if token_refresh_already_attempted:
                # A refresh already happened and the call failed again. That is
                # not a stale token, it is a broken grant -- revoked, rescoped
                # or misconfigured -- and no amount of retrying fixes it.
                return Disposition(
                    kind=kind, action=Action.OPEN_CIRCUIT_AND_ALERT,
                    counts_toward_circuit=True, counts_toward_attempts=True,
                    retry=False,
                    open_circuit_until=now + timedelta(seconds=self.circuit_open_seconds),
                    alert=ALERT_AUTH_FAILED_P1, attempts=attempts)
            return Disposition(
                kind=kind, action=Action.REFRESH_TOKEN_AND_RETRY,
                counts_toward_circuit=True, counts_toward_attempts=True,
                # Immediately: a refresh is not a backoff situation, and
                # sitting out 256 seconds over an expired token would stall
                # every job on the connection for no reason.
                retry=True, next_attempt_at=now, attempts=attempts)

        if kind is FailureKind.BUSINESS:
            # Zoho refused the content. Retrying identical content gets an
            # identical refusal, so this never retries and never counts toward
            # the circuit -- a run of bad records is our data problem, and
            # opening the circuit over it would stop the good records too.
            return Disposition(
                kind=kind, action=Action.QUARANTINE,
                counts_toward_circuit=False, counts_toward_attempts=True,
                retry=False, terminal_state=STATE_QUARANTINED,
                exception_kind=self.business_exception_kind, attempts=attempts)

        # TRANSIENT: 5xx, timeout, or a 429 whose code we could not read.
        alert = ALERT_UNMAPPED_RATE_LIMIT_CODE if classification.unmapped_code else None
        if attempts >= self.max_attempts:
            return Disposition(
                kind=kind, action=Action.GIVE_UP_DEAD,
                counts_toward_circuit=True, counts_toward_attempts=True,
                retry=False, terminal_state=STATE_DEAD,
                exception_kind=self.exhausted_exception_kind, alert=alert,
                attempts=attempts, unmapped_code=classification.unmapped_code)
        return Disposition(
            kind=kind, action=Action.BACKOFF_AND_RETRY,
            counts_toward_circuit=True, counts_toward_attempts=True, retry=True,
            next_attempt_at=self.next_attempt_at(attempts, now=now, rng=rng),
            alert=alert, attempts=attempts,
            unmapped_code=classification.unmapped_code)


# ===========================================================================
# The circuit breaker
# ===========================================================================


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class Circuit:
    """The breaker, as a value. Every transition returns a new one.

    Immutable on purpose: this state has to survive an instance being
    reclaimed, so it must be something that can be written to a row and read
    back, not an object with a lifetime. See :class:`CircuitStore`.
    """

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    window_started_at: datetime | None = None
    open_until: datetime | None = None
    #: A HALF_OPEN circuit permits exactly one probe. This is the flag that
    #: makes "one" true across two callers rather than one.
    probe_in_flight: bool = False


def allow(circuit: Circuit, now: datetime, *,
          policy: RetryPolicy | None = None) -> tuple[bool, Circuit]:
    """May a call be attempted? Returns the answer and the updated circuit.

    Returns the circuit as well because asking is itself a transition: an OPEN
    circuit whose timer has expired becomes HALF_OPEN and hands out its single
    probe. A caller that discards the returned circuit hands out that probe
    over and over, which is a thundering herd against a service that has just
    told us it is unwell.
    """
    del policy  # accepted for symmetry; no policy input is needed to ask
    now = _require_aware(now, what="allow(now=...)")

    if circuit.state is CircuitState.CLOSED:
        return True, circuit

    if circuit.state is CircuitState.OPEN:
        if circuit.open_until is not None and now < circuit.open_until:
            return False, circuit
        return True, replace(circuit, state=CircuitState.HALF_OPEN,
                             open_until=None, probe_in_flight=True)

    # HALF_OPEN: exactly one probe in flight at a time.
    if circuit.probe_in_flight:
        return False, circuit
    return True, replace(circuit, probe_in_flight=True)


def observe_success(circuit: Circuit, now: datetime) -> Circuit:
    """A call succeeded: the breaker closes and the counter resets.

    "Consecutive" is the operative word in "5 consecutive failures". One
    success in the middle means the service is answering, and a counter that
    survived it would eventually open a circuit on five failures spread across
    a thousand successful calls.
    """
    _require_aware(now, what="observe_success(now=...)")
    return Circuit()


def observe_failure(circuit: Circuit, now: datetime, disposition: Disposition,
                    *, policy: RetryPolicy | None = None) -> Circuit:
    """Fold one disposition into the breaker.

    Three distinct paths, and they are why the response table's third column
    is not decoration:

    * ``open_circuit_until`` set (daily quota, second auth failure) -- the
      circuit opens **now**, on one failure, to that instant. No counting.
    * ``counts_toward_circuit`` -- the ordinary 5-in-60s ratchet.
    * neither (429/44, 429/1070, business error) -- the breaker does not move.
      A minute-throttle is our arithmetic, a concurrency limit is our
      parallelism, a business error is our payload. None of them is evidence
      about Zoho's health, and letting them open the breaker would take the
      integration down over our own bugs.
    """
    policy = policy or RetryPolicy()
    now = _require_aware(now, what="observe_failure(now=...)")

    if disposition.open_circuit_until is not None:
        return Circuit(state=CircuitState.OPEN,
                       consecutive_failures=circuit.consecutive_failures,
                       window_started_at=circuit.window_started_at,
                       open_until=disposition.open_circuit_until,
                       probe_in_flight=False)

    if not disposition.counts_toward_circuit:
        return circuit

    if circuit.state is CircuitState.HALF_OPEN:
        # The single probe failed. Straight back to OPEN for a full interval;
        # it does not get four more chances first.
        return Circuit(
            state=CircuitState.OPEN, consecutive_failures=0,
            window_started_at=None,
            open_until=now + timedelta(seconds=policy.circuit_open_seconds),
            probe_in_flight=False)

    started = circuit.window_started_at
    within = (started is not None
              and (now - started).total_seconds() <= policy.circuit_failure_window_seconds)
    if within:
        failures = circuit.consecutive_failures + 1
    else:
        # Either the first failure, or the previous run is older than the
        # window. Five failures spread over an hour are not a burst, and
        # opening on them would trip the breaker on ordinary background noise.
        started, failures = now, 1

    if failures >= policy.circuit_failure_threshold:
        return Circuit(
            state=CircuitState.OPEN, consecutive_failures=failures,
            window_started_at=started,
            open_until=now + timedelta(seconds=policy.circuit_open_seconds),
            probe_in_flight=False)

    return Circuit(state=CircuitState.CLOSED, consecutive_failures=failures,
                   window_started_at=started, open_until=None,
                   probe_in_flight=False)


class CircuitStore(Protocol):
    """Where a circuit lives between invocations.

    **This has to be durable, and the in-memory implementation below is not.**
    A daily-quota circuit stays open until midnight UTC -- hours during which
    the AppSail instance that opened it has been reclaimed many times over. A
    circuit that forgets on the way out is a circuit that reopens the tap the
    moment the process restarts, which is exactly when it must not.

    Stream 2 owns the schema. Reported, not made: a durable implementation
    needs somewhere to keep ``(connection_id, state, consecutive_failures,
    window_started_at, open_until, probe_in_flight)`` -- five columns on
    ``integration_connection`` or a small ``integration_circuit`` table, either
    is fine, and either must be written in the same transaction as the failure
    it records.
    """

    def load(self, connection_id: str) -> Circuit:  # pragma: no cover - protocol
        ...

    def save(self, connection_id: str, circuit: Circuit) -> None:  # pragma: no cover
        ...


class InMemoryCircuitStore:
    """A circuit store that lasts exactly as long as this process does.

    Correct within one Function invocation and useful in tests. **Not a
    production store**: see :class:`CircuitStore`. It is here so that a
    caller wiring the durable one later has something to type against, and so
    that its absence is a named object rather than a silent gap.
    """

    def __init__(self) -> None:
        self._by_connection: dict[str, Circuit] = {}

    def load(self, connection_id: str) -> Circuit:
        return self._by_connection.get(connection_id, Circuit())

    def save(self, connection_id: str, circuit: Circuit) -> None:
        self._by_connection[connection_id] = circuit


# ===========================================================================
# Parallelism
# ===========================================================================


@dataclass(frozen=True)
class Parallelism:
    """How many calls this connection makes at once, and its bounds.

    Reduced by one on a 429/1070 and never below `floor`: at zero the
    integration stops entirely, which is a worse outcome than a slow one.
    """

    current: int
    floor: int = 1
    ceiling: int = 4

    def __post_init__(self) -> None:
        if self.floor < 1:
            raise ValueError("parallelism floor must be at least 1; a floor of "
                             "0 stops the integration rather than slowing it")
        if self.ceiling < self.floor:
            raise ValueError("parallelism ceiling cannot be below its floor")
        if not (self.floor <= self.current <= self.ceiling):
            raise ValueError(
                "parallelism " + repr(self.current) + " is outside ["
                + repr(self.floor) + ", " + repr(self.ceiling) + "]")

    def apply(self, disposition: Disposition) -> "Parallelism":
        """The parallelism after `disposition`, clamped to the bounds."""
        if not disposition.parallelism_delta:
            return self
        target = self.current + disposition.parallelism_delta
        clamped = max(self.floor, min(self.ceiling, target))
        return replace(self, current=clamped)

    def restored(self) -> "Parallelism":
        """One step back up, after a clean run. Recovery is slower than the
        reduction on purpose -- one at a time in both directions, but the
        reduction is driven by a signal and the restoration by an absence."""
        return replace(self, current=min(self.ceiling, self.current + 1))


# ===========================================================================
# Reporting
# ===========================================================================


def alert_fields(disposition: Disposition, *, connection_id: str,
                 **extra: Any) -> dict[str, Any]:
    """The structured payload for `observability.alert`, if one is due.

    Returns ``{}`` when the disposition carries no alert, so the caller writes
    ``if fields := alert_fields(...)`` rather than reaching into the record.
    Kept separate from `decide` so that the decision stays pure and a test can
    assert on it without capturing log output.
    """
    if disposition.alert is None:
        return {}
    fields: dict[str, Any] = {
        "connection_id": connection_id,
        "failure_kind": disposition.kind.value,
        "action": disposition.action.value,
        "attempts": disposition.attempts,
    }
    if disposition.open_circuit_until is not None:
        fields["open_until"] = disposition.open_circuit_until.isoformat()
    if disposition.resume_at is not None:
        fields["resume_at"] = disposition.resume_at.isoformat()
    fields.update(extra)
    return fields
