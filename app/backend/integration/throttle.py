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
is therefore an ``integration_rate_budget`` row, reserved by a single
``UPDATE ... SET used = used + n WHERE used + n <= ceiling ... RETURNING`` so
that the read, the test against the ceiling and the increment are one atomic
statement. Two concurrent Functions racing for the last call in the window
cannot both win, because only one of them gets a row back.

That statement lives in :func:`app.backend.pg.integration_store.reserve_calls`
and **not in this module**, which issues no SQL at all -- see the block comment
above :func:`reserve` for what that repaired.

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

What this module needed from stream 2, and what actually shipped
----------------------------------------------------------------
``migrations/pg/010_integration.sql`` is stream 2's file, and C2 froze
``integration_rate_budget(connection_id, window_kind, window_start, used, ...)``
with a trailing ellipsis. This module was written against a GUESS at what that
ellipsis contained, and the guess was wrong in every particular:

============================  =================================================
this module used to write     what migration 010 actually declares
============================  =================================================
``lane``                      ``allocation`` -- same concept, schema's name wins
``created_by`` / ``updated_by``  neither exists; there is ``updated_at``
``UNIQUE (connection_id,      ``PRIMARY KEY (connection_id, window_kind,
window_kind, lane,            allocation, window_start_key)``
window_start)``
(nothing)                     ``window_start_key``, ``window_seconds``,
                              ``window_tz``, ``ceiling`` -- all NOT NULL,
                              none with a default
============================  =================================================

The concept this module argued for was right and is in the shipped schema:
``allocation`` IS part of the primary key, so "polling is capped at 60" is
enforceable, because enforcing a per-lane ceiling requires knowing what that
lane spent. What was wrong was writing SQL against the guess and never
executing it. Renaming ``lane`` to ``allocation`` would have satisfied a
column-name gate and still failed at runtime on the four NOT NULL columns the
insert omitted.

So the reservation now delegates to
:func:`app.backend.pg.integration_store.reserve_calls`, written by the author
of the migration against its real columns. One implementation, and the
statement is exercised against a live server by
``tests/test_pg_integration_rate_budget.py`` rather than only against a double.

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

from ..pg import integration_store as store
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
#: statement therefore joins the connection and maps `entity` to it.
#:
#: This is `integration_store.VIA_CONNECTION_SCOPE_COLUMNS` **by identity, not
#: by copy**. A second mapping with the same contents was the smaller sibling
#: of the defect this module just had: two files agreeing about a name until
#: one of them changed. There is one mapping, it lives beside the statements it
#: scopes, and this alias exists so a reader of this module can still see which
#: mapping the reservation goes through.
#:
#: The other three dimensions are waived EXPLICITLY (mapped to None) rather
#: than omitted, which `repo.compile_scope` requires and which is the point:
#: an omission compiles to TRUE and nobody notices, a waiver is a decision
#: somebody signed. The decision is that an integration connection is an
#: entity-level object -- it has no plant, project or location -- so there is
#: no column those dimensions could filter on and no narrower predicate to
#: write. A principal restricted to a project sees the connections of the
#: entities it is granted, which is the same answer the connection screen gives.
RATE_BUDGET_SCOPE_COLUMNS: Mapping[str, str | None] = \
    store.VIA_CONNECTION_SCOPE_COLUMNS

#: The zone the budget's window buckets are computed in.
#:
#: `integration_rate_budget.window_start_key` is the bucket identity as the
#: APPLICATION computed it, and `window_tz` records which midnight that
#: computation assumed -- see migration 010's header for why no CHECK in that
#: table derives a boundary (every PostgreSQL timestamptz truncation is STABLE,
#: not IMMUTABLE, so a constraint built on one is true only for the session
#: that evaluates it).
#:
#: This module's `window_start` truncates in UTC, so the two must agree or the
#: verdict would report a window the reservation did not charge. UTC is the
#: choice for the reason `integration_store.window_keys` gives: whose midnight
#: Zoho's daily quota resets at is a tenant fact Phase 0B-2 has not verified,
#: and UTC is the only option that is honestly arbitrary rather than falsely
#: specific.
RATE_BUDGET_TZ = "UTC"

#: Reported in this order: DAY first, because on ERP Standard the daily ceiling
#: is the binding one and it is the window an operator reads first.
_WINDOW_REPORT_ORDER = (WindowKind.DAY, WindowKind.MINUTE)


# ===========================================================================
# The reservation
# ===========================================================================
#
# THIS MODULE ISSUES NO SQL OF ITS OWN, AND THAT IS THE FIX.
#
# It used to. `_RESERVE_SQL` inserted into `integration_rate_budget` naming
# `lane`, `created_by` and `updated_by` and conflicting on
# `(connection_id, window_kind, lane, window_start)`. The shipped table
# (migration 010) has `allocation`, has `updated_at`, has neither `_by` column,
# and its primary key is `(connection_id, window_kind, allocation,
# window_start_key)`. The statement could not execute. Every test over this
# module passed anyway, because they all ran against an in-memory double:
# **SQL is a string until something executes it.**
#
# Wave 5 shipped THREE such statements -- here, in `outbound.py`, and (fixed)
# in `jobs.py` -- because three file-disjoint streams each wrote SQL against a
# frozen column list that ended in an ellipsis. The repair is not a fourth
# patched statement. Renaming the three columns would have satisfied the
# column-name gate and STILL failed at runtime, because the insert omitted
# `window_start_key`, `window_seconds`, `window_tz` and `ceiling` -- all NOT
# NULL with no default. A gate that goes green on a statement that cannot
# execute is worse than one that stays red.
#
# `integration_store.reserve_calls` was written by the author of migration 010,
# against its actual columns, and it is now the only implementation. What it
# gives this module beyond correct names:
#
#   * ONE `UPDATE` touching BOTH windows, not two statements. A partial
#     reservation is therefore not something to compensate for -- it is not
#     representable. The old code took the day window, then discovered the
#     minute window refused, then issued a compensating `UPDATE` to give the
#     day's call back. Every step of that was correct and the whole of it was
#     unnecessary.
#   * The whole reservation runs inside a SAVEPOINT, so a reservation that
#     cannot be granted in both windows survives in neither -- and the caller's
#     transaction is still usable afterwards, which is what lets a poller
#     checkpoint and return rather than lose the work it had already done.
#   * `ceiling` comes from `integration_connection`'s own
#     `per_minute_call_ceiling` / `daily_call_ceiling`, copied onto the budget
#     row when the window opened. That is why `reserve` no longer takes a
#     `daily_ceiling`: it had nowhere honest to put one. A parameter that looks
#     like it sets a limit and does not is the same class of defect as SQL that
#     looks like it executes. `lane_ceiling()` keeps the argument, because it
#     is a pure calculation a caller may legitimately want to do without a
#     database.
#
# What this module still owns is the VERDICT: section 11.6 says over-budget is
# a normal outcome a job checkpoints and returns on, not an exception. The
# store raises `RateBudgetExhausted`; that is the right shape for the store's
# other callers and the wrong shape here, so `reserve` catches it and answers
# with a falsy `BudgetVerdict` naming the binding window and when it resets.


def reserve(session: Session, *, connection_id: str, lane: Lane, cost: int = 1,
            actor: str, clock: Clock,
            scope: Scope | None = None) -> BudgetVerdict:
    """Atomically reserve `cost` calls in **both** windows, or reserve nothing.

    Returns a falsy :class:`BudgetVerdict` when either window refuses. It does
    not raise, does not sleep and does not retry: over budget is a normal
    outcome, and the job's response to it is to checkpoint and return.

    Delegates the reservation to `integration_store.reserve_calls`, which is
    the one implementation of this write -- see the block comment above for
    what that fixed and what it removed. Because that function reserves both
    windows in a single statement inside a savepoint, a day reservation taken
    ahead of a minute refusal is released by the rollback rather than by a
    compensating `UPDATE`, and the figures this verdict reports are read
    afterwards, on a healthy transaction, so they are the post-release ones.
    Reporting the pre-release count would tell an operator the day had spent a
    call it had not -- an off-by-one that compounds once per throttled attempt
    across a backfill.

    `actor` is accepted and deliberately not written to the budget row: the
    shipped table has no `created_by`/`updated_by` column, and inventing one
    here is how this module got into trouble. Who spent the call belongs on the
    `integration_event` trail, which is append-only and already carries it.

    Raises :class:`ConnectionNotVisible` when the connection does not exist or
    is out of `scope` -- one exception for both, on purpose.
    """
    if cost < 1:
        raise ValueError("a reservation must be for at least one call; got "
                         + repr(cost))
    now = _require_aware(clock.now(), what="clock.now()")

    try:
        granted = store.reserve_calls(
            session, connection_id=connection_id, allocation=lane.value,
            count=cost, tz=RATE_BUDGET_TZ, now=now, scope=scope)
    except store.RateBudgetExhausted as refused:
        return _refused_verdict(
            session, connection_id=connection_id, lane=lane, cost=cost,
            now=now, scope=scope, binding=WindowKind(refused.window_kind))
    except store.IntegrationStoreError as exc:
        # Both windows absent after `ensure_rate_budget_windows` ran means the
        # seeding `INSERT ... SELECT FROM integration_connection` matched no
        # connection. Out of scope and nonexistent are the same answer here for
        # the reason `ConnectionNotVisible` exists: telling them apart is an
        # existence oracle. Anything else the store raises is not ours to
        # reinterpret.
        if getattr(exc, "code", None) != "RATE_BUDGET_WINDOWS_MISSING":
            raise
        if store.get_connection(session, connection_id, scope=scope) is None:
            raise ConnectionNotVisible(
                "no integration_connection " + repr(connection_id)
                + " is visible to this scope") from exc
        raise

    return BudgetVerdict(
        granted=True, lane=lane, cost=cost, binding=None,
        windows=tuple(
            WindowState(kind=kind, lane=lane,
                        window_start=window_start(kind, now),
                        used=int(granted[kind.value]["used"]),
                        ceiling=int(granted[kind.value]["ceiling"]))
            for kind in _WINDOW_REPORT_ORDER if kind.value in granted))


def _refused_verdict(session: Session, *, connection_id: str, lane: Lane,
                     cost: int, now: datetime, scope: Scope | None,
                     binding: WindowKind) -> BudgetVerdict:
    """The falsy verdict, carrying what each window holds AFTER the rollback.

    The read runs outside the store's savepoint, on a transaction the refusal
    left healthy, so it sees the figures as they stand once nothing was spent.
    """
    state = store.read_rate_budget(
        session, connection_id=connection_id, allocation=lane.value,
        tz=RATE_BUDGET_TZ, now=now, scope=scope)
    return BudgetVerdict(
        granted=False, lane=lane, cost=cost, binding=binding,
        windows=tuple(
            WindowState(kind=kind, lane=lane,
                        window_start=window_start(kind, now),
                        used=int(state[kind.value]["used"]),
                        ceiling=int(state[kind.value]["ceiling"]))
            for kind in _WINDOW_REPORT_ORDER if kind.value in state))


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
