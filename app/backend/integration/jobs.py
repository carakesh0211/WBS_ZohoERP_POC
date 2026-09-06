"""The chunked, checkpointed job framework every background activity runs on.

Why this module exists at all
----------------------------
There is no resident worker on Catalyst. AppSail instances live **5 minutes**
of total uptime, scale to zero, and answer a request in at most **30 seconds**;
the only place batch work can run is a Cron or Event Function, whose ceiling is
**15 minutes** (plan §2.1, §2.2). Nothing may assume it will finish. So every
job in this system obeys one contract, and this module is that contract made
mechanical rather than remembered:

1. **Claim a bounded batch** with ``SELECT … FOR UPDATE SKIP LOCKED LIMIT n``.
   :class:`PostgresJobStore` carries the claim statement; a second Function
   tick that overlaps the first skips the locked row instead of doubling the
   work.
2. **Hold a soft deadline of 12 minutes** — 80% of the ceiling
   (:data:`SOFT_DEADLINE_SECONDS`). On reaching it, commit progress and return.
   The remaining 3 minutes are the margin a hard kill would otherwise eat.
3. **Persist a resumable cursor.** The checkpoint is a JSON object owned by the
   job, written after *every* step, never only at the end.
4. **Be idempotent.** Re-running from the last checkpoint must produce the same
   state — which is a property of the job's writes (inbox ``payload_sha``
   dedupe, keyed exceptions), and of the runner never advancing a cursor past
   work it did not commit.

The shape a job takes
---------------------
A job is a **generator**: it yields a :class:`Progress` per unit of work it has
finished, carrying the checkpoint that would resume it *after* that unit. The
runner — not the job — owns the clock, the deadline, the persistence of the
cursor and the accounting. That division is the whole point:

* the deadline is checked in exactly one place, **between** steps, so a job
  author cannot forget it and a test can prove it with an injected clock
  instead of a wall-clock wait;
* a job that stops for its own reason (rate budget exhausted, a window fully
  walked) does so by telling the context, so "we finished" and "we ran out of
  room" can never be confused. That distinction matters: reporting DONE when
  work remains loses the work silently, which is the failure mode this whole
  design exists to prevent.

What this module deliberately does not own
------------------------------------------
* **The adapter** (stream 1, `adapter.py`): products differ, and no caller may
  hardcode a product's behaviour. :class:`JobContext` carries
  ``capabilities`` and jobs read it.
* **The rate budget, retry and circuit breaker** (stream 4, `throttle.py`):
  :class:`RateBudget` is the port, :class:`NullBudget` the null object used
  until it lands. Over budget → the job checkpoints and returns, exactly as
  §11.6 requires; the next cron tick resumes.
* **The tables** (stream 2, `migrations/pg/010_integration.sql` and
  `pg/integration_store.py`): :class:`JobStore` is the port this framework
  needs. :class:`PostgresJobStore` implements the *job-claim* half of it
  against the frozen C2 column names, because the claim statement is part of
  the contract above and has to exist somewhere testable.

Money is never touched here. Time is: every timestamp crossing a checkpoint
goes through :func:`iso` / :func:`parse_iso`, because a checkpoint is ``jsonb``
and a ``datetime`` is not JSON.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from ..observability import alert, correlation, log

# ---------------------------------------------------------------- the ceiling

#: Catalyst Cron / Event Function ceiling, in seconds (plan §2.2). A job that
#: reaches this is killed mid-statement; nothing is committed that was not
#: already committed.
FUNCTION_CEILING_SECONDS = 900

#: The soft deadline: 80% of the ceiling. On reaching it a job commits progress
#: and returns. The 180-second remainder is margin for the final commit, the
#: event write and the platform's own teardown -- not spare capacity.
SOFT_DEADLINE_SECONDS = 720

#: The AppSail request ceiling. Present so a caller can assert, in a test, that
#: batch work is never scheduled through the HTTP tier (plan §2.2: "Job Pool →
#: AppSail service (HTTP) ceiling 30 seconds ← never for batch work").
APPSAIL_REQUEST_CEILING_SECONDS = 30

#: Overlap re-read on every windowed poll (plan §11.3). `last_modified_time` is
#: filterable but NOT sortable on ERP/Books bills and POs, so there is no stable
#: resumable keyset walk on modification time; the window is re-entered 300 s
#: behind its own boundary on every run. The re-read is free because
#: `integration_inbox UNIQUE (connection_id, module, external_id, payload_sha)`
#: discards the duplicate.
POLL_OVERLAP_SECONDS = 300

#: Default width of one polling window. Bounded so a run fits the ceiling;
#: narrowed on high volume by the caller, never widened past the ceiling.
DEFAULT_WINDOW_SECONDS = 24 * 60 * 60

#: How long a claim is held before another tick may reap it. Longer than the
#: ceiling on purpose: a Function killed at 15 minutes must not have its row
#: stolen by the tick that starts one minute later while it may still be
#: committing.
DEFAULT_LEASE_SECONDS = FUNCTION_CEILING_SECONDS + 300

#: A job that has checkpointed this many times without finishing is DEAD and
#: alerts, rather than looping forever (plan §2.2).
DEFAULT_MAX_RESUME_COUNT = 20

# ------------------------------------------------- job states (C16, not new)
# Sourced from research/30_contracts/C16_integration_statuses.json, namespace
# "job". Never invented here -- `tests/test_integration_jobs.py` asserts these
# constants are exactly that namespace's codes, so a rename in the registry
# fails the build rather than drifting.
JOB_PENDING = "PENDING"
JOB_CLAIMED = "CLAIMED"
JOB_CHECKPOINTED = "CHECKPOINTED"
JOB_DONE = "DONE"
JOB_FAILED = "FAILED"
JOB_DEAD = "DEAD"

JOB_STATUSES: frozenset[str] = frozenset({
    JOB_PENDING, JOB_CLAIMED, JOB_CHECKPOINTED, JOB_DONE, JOB_FAILED, JOB_DEAD,
})

#: States a tick may claim: never-run, paused-at-a-checkpoint, failed (a
#: transient fault must not be terminal) -- and CLAIMED.
#:
#: CLAIMED is in the list because the state alone cannot tell a running job
#: from a crashed one. A Catalyst Function killed at the ceiling leaves its row
#: CLAIMED with nobody to change it, and a job that could never be re-claimed
#: from that state would be stranded forever. What separates the two is the
#: LEASE: the claim statement also requires `locked_until <= now`, so a live
#: invocation is protected for as long as it could still be running and a dead
#: one is reaped the moment it could not be.
CLAIMABLE_STATES: tuple[str, ...] = (
    JOB_PENDING, JOB_CHECKPOINTED, JOB_FAILED, JOB_CLAIMED)

# ------------------------------------------------------------- stop reasons
#: Why a run ended. Recorded on the `integration_event` so an operator on
#: SCR-38 can tell "finished" from "ran out of room" without reading logs.
STOP_COMPLETED = "COMPLETED"
STOP_SOFT_DEADLINE = "SOFT_DEADLINE"
STOP_RATE_BUDGET = "RATE_BUDGET_EXHAUSTED"
STOP_JOB_REQUESTED = "JOB_REQUESTED_CHECKPOINT"
STOP_MAX_STEPS = "MAX_STEPS"
STOP_MAX_RESUMES = "MAX_RESUME_COUNT_EXCEEDED"
STOP_ERROR = "ERROR"

#: Chunk sizes from the plan §2.2 table. Named here so a job cannot invent its
#: own bound and so a review can compare them to the table in one place.
CHUNK_SIZES: Mapping[str, int] = {
    "poll_bills": 200,               # records per page
    "poll_purchaseorders": 200,
    "poll_items": 200,
    "poll_contacts": 200,
    "sweep_po_anchored": 50,         # open POs per invocation
    "sweep_bill_detail": 50,         # queued bills per invocation
    "sweep_control_totals": 1,       # one period per invocation
    "sweep_completeness": 1,         # one period per invocation
    "drain_outbox": 25,
}

#: Maximum pages one windowed poll walks per invocation (plan §2.2: "200-record
#: pages, ≤ 40 pages"). Beyond this the run checkpoints on the page number and
#: the next tick continues -- the window boundary does NOT advance.
MAX_PAGES_PER_POLL = 40

class JobError(RuntimeError):
    """A fault in the framework's own contract, not in a job's work."""


# --------------------------------------------------------------------- time

@runtime_checkable
class Clock(Protocol):
    """The only source of 'now' anything here is allowed to read.

    Injected, never imported at a call site: the deadline behaviour of this
    module has to be provable in a test that runs in milliseconds, and a test
    that proves a 12-minute deadline by waiting 12 minutes proves nothing
    anybody will run.
    """

    def now(self) -> datetime: ...


class SystemClock:
    """UTC wall clock. The production `Clock`."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    """A datetime rendered for a `jsonb` checkpoint, or None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | datetime | None) -> datetime | None:
    """The inverse of :func:`iso`, tolerant of an already-parsed value.

    A checkpoint read back from `jsonb` carries strings; a checkpoint that has
    not yet been round-tripped through the store carries datetimes. Resuming
    must not depend on which of those it got.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def assert_json_safe(checkpoint: Mapping[str, Any], *, where: str) -> None:
    """Refuse a checkpoint that cannot survive a round trip through `jsonb`.

    A checkpoint carrying a `datetime` serialises fine in memory and fails at
    the database boundary -- in a Function, at minute eleven, with the run's
    progress in it. Raised here instead, on the step that produced it.
    """
    try:
        json.dumps(checkpoint)
    except TypeError as exc:
        raise JobError(
            f"{where}: checkpoint is not JSON-serialisable ({exc}). A "
            f"checkpoint is stored in `job.checkpoint jsonb`; render "
            f"datetimes with jobs.iso() and read them back with "
            f"jobs.parse_iso()."
        ) from exc


# ------------------------------------------------------------------- budget

class RateBudget(Protocol):
    """The port stream 4's `throttle.py` fills.

    `consume` returns False rather than raising: over budget is an ordinary,
    expected outcome that ends in a checkpoint and a resume on the next tick,
    not an error. On ERP Standard the binding ceiling is the **daily** 2,000
    calls, not the per-minute one (plan §11.6), so the implementation behind
    this port tracks both windows; the caller only asks "may I".
    """

    def consume(self, *, connection_id: str | None, calls: int,
                now: datetime) -> bool: ...


class NullBudget:
    """Grants everything. The null object used until `throttle.py` lands.

    Deliberately not a 'sensible default limit': a silent, invented ceiling in
    the framework would be indistinguishable from the real one at the call
    site and would be wrong for every plan tier.
    """

    def consume(self, *, connection_id: str | None, calls: int,
                now: datetime) -> bool:
        return True


# ------------------------------------------------------------ capabilities
# Stream 1 owns `adapter.py`. Until it lands this module must still be
# importable and testable, so the frozen C1 declaration is mirrored here and
# picked up from the real module the moment it exists. The mirror is asserted
# field-for-field against the contract in tests, so the two cannot drift
# unnoticed -- and the import order means the real one always wins.
try:  # pragma: no cover - exercised by whichever of the two is present
    from .adapter import Capabilities  # type: ignore  # noqa: F401
except ImportError:  # pragma: no cover
    @dataclass(frozen=True)
    class Capabilities:  # type: ignore[no-redef]
        """What the target product **cannot** do (C1 / plan §11.10).

        Mirrored from the frozen seam so the scheduler reads a declared
        capability gap instead of assuming a product. `receives_listable=False`
        selects PO-anchored discovery; `po_delta_filter=False` selects full
        re-pull.
        """

        receives_listable: bool
        bills_delta_filter: bool
        po_delta_filter: bool
        items_delta_filter: bool
        line_level_custom_fields: bool
        daily_call_ceiling: int


# -------------------------------------------------------------- store port

@dataclass(frozen=True)
class ClaimedJob:
    """One `job` row, claimed for this invocation."""

    job_id: str
    kind: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    resume_count: int = 0
    correlation_id: str | None = None
    connection_id: str | None = None


class JobStore(Protocol):
    """What the runner needs from persistence, and nothing more.

    Stream 2 owns `pg/integration_store.py`; this is the port it fills.
    :class:`PostgresJobStore` implements it here against the frozen C2 columns
    so the claim statement -- contract item 1 -- is real code with a test on
    it rather than a paragraph.
    """

    def claim_job(self, *, kind: str, now: datetime, lease_seconds: int,
                  soft_deadline_at: datetime, limit: int = 1) -> ClaimedJob | None: ...

    def save_checkpoint(self, job_id: str, *, checkpoint: Mapping[str, Any],
                        state: str, resume_count: int, now: datetime) -> None:
        """Persist the cursor.

        **A state other than CLAIMED releases the lease.** A job that has
        checkpointed has finished its invocation and the next cron tick must be
        able to claim it; holding the lease to expiry would idle the work for
        the whole lease window. Only a *crashed* invocation keeps its lease,
        because only a crash leaves nobody to release it -- which is exactly
        what the lease is for.
        """

    def finish_job(self, job_id: str, *, state: str,
                   checkpoint: Mapping[str, Any], now: datetime,
                   note: str | None = None) -> None: ...

    def record_event(self, *, connection_id: str | None,
                     correlation_id: str | None, kind: str,
                     detail: Mapping[str, Any]) -> None: ...


# ------------------------------------------------------------------ context

@dataclass
class JobContext:
    """Everything a job may read, and the two things it may tell the runner.

    A job never reads a clock, never writes its own `job` row, and never
    decides when to stop for time. It asks :meth:`should_continue` if it wants
    to bail out of an inner loop early, and it calls :meth:`charge` before
    spending an API call.
    """

    job_id: str
    kind: str
    correlation_id: str
    clock: Clock
    started_at: datetime
    soft_deadline_at: datetime
    checkpoint: dict[str, Any]
    resume_count: int
    store: JobStore
    connection_id: str | None = None
    capabilities: Capabilities | None = None
    budget: RateBudget = field(default_factory=NullBudget)
    calls_used: int = 0
    units_done: int = 0
    #: Set by :meth:`charge` when the budget refuses. The runner reports
    #: CHECKPOINTED / RATE_BUDGET rather than DONE, so unfinished work is
    #: never reported as finished.
    budget_exhausted: bool = False
    #: Set by :meth:`request_checkpoint`.
    requested_stop: str | None = None

    # --------------------------------------------------------------- clock
    def now(self) -> datetime:
        return self.clock.now()

    def remaining_seconds(self) -> float:
        return (self.soft_deadline_at - self.clock.now()).total_seconds()

    def soft_deadline_reached(self) -> bool:
        return self.clock.now() >= self.soft_deadline_at

    def should_continue(self) -> bool:
        """False once time or budget is out. For a job's own inner loops."""
        return not (self.soft_deadline_reached()
                    or self.budget_exhausted
                    or self.requested_stop is not None)

    # -------------------------------------------------------------- budget
    def charge(self, calls: int = 1) -> bool:
        """Ask for `calls` API calls. False means stop and checkpoint.

        Called BEFORE the call is made, never after: a budget consulted after
        the fact cannot prevent the request that breaches the daily ceiling,
        and on ERP Standard breaching it costs the rest of the day.
        """
        if calls <= 0:
            raise JobError("charge() must be asked for at least one call")
        if self.budget_exhausted:
            return False
        granted = self.budget.consume(
            connection_id=self.connection_id, calls=calls, now=self.clock.now())
        if not granted:
            self.budget_exhausted = True
            return False
        self.calls_used += calls
        return True

    def request_checkpoint(self, reason: str = STOP_JOB_REQUESTED) -> None:
        """Stop after this step, and report it as a pause, not a completion.

        For a job that has reached a bound of its own -- a page limit, one
        period per invocation -- rather than the clock's. The distinction
        reaches the event, because "paused at 40 pages" and "paused at twelve
        minutes" call for different responses.
        """
        self.requested_stop = reason


# -------------------------------------------------------------- job protocol

@dataclass(frozen=True)
class Progress:
    """One committed unit of work, and the cursor that resumes after it.

    `checkpoint` is what the next invocation would start from **having already
    done this step**. Yielding a checkpoint that includes work not yet written
    is the one way to lose data through this framework, so a job yields after
    its writes, never before.
    """

    checkpoint: Mapping[str, Any]
    units: int = 1
    note: str | None = None
    detail: Mapping[str, Any] | None = None


class ChunkedJob(Protocol):
    """A job: a name, and a generator that resumes from a checkpoint."""

    kind: str

    def resume(self, ctx: JobContext) -> Iterator[Progress]: ...


@dataclass(frozen=True)
class JobRun:
    """The outcome of one invocation. Everything an operator needs, no logs."""

    kind: str
    claimed: bool
    state: str | None = None
    job_id: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    units: int = 0
    calls: int = 0
    resume_count: int = 0
    correlation_id: str | None = None
    reason: str | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        """True only for DONE. CHECKPOINTED means resume me."""
        return self.state == JOB_DONE

    @property
    def resumable(self) -> bool:
        return self.state in (JOB_CHECKPOINTED, JOB_FAILED)


# ----------------------------------------------------------------- the runner

def run_job(job: ChunkedJob, *, store: JobStore, clock: Clock | None = None,
            connection_id: str | None = None,
            correlation_id: str | None = None,
            capabilities: Capabilities | None = None,
            budget: RateBudget | None = None,
            soft_deadline_seconds: int = SOFT_DEADLINE_SECONDS,
            lease_seconds: int = DEFAULT_LEASE_SECONDS,
            max_resume_count: int = DEFAULT_MAX_RESUME_COUNT,
            max_steps: int | None = None) -> JobRun:
    """Claim, resume, checkpoint and report one invocation of `job`.

    Returns rather than raises on a job's failure: this is called from a cron
    Function whose only failure channel is a stack trace in a log nobody is
    watching. The run is recorded on the job row and on `integration_event`,
    with the correlation id, which is where an operator can actually see it.

    `soft_deadline_seconds` is a parameter and not a constant read so a test
    can compress twelve minutes into a few simulated ticks -- and so the
    default remains the only value production ever passes.
    """
    clock = clock or SystemClock()
    budget = budget or NullBudget()
    started_at = clock.now()
    soft_deadline_at = started_at + timedelta(seconds=soft_deadline_seconds)

    if soft_deadline_seconds > FUNCTION_CEILING_SECONDS:
        raise JobError(
            f"soft deadline {soft_deadline_seconds}s exceeds the "
            f"{FUNCTION_CEILING_SECONDS}s Function ceiling: a deadline past "
            f"the kill point is not a deadline.")

    claimed = store.claim_job(kind=job.kind, now=started_at,
                              lease_seconds=lease_seconds,
                              soft_deadline_at=soft_deadline_at, limit=1)
    if claimed is None:
        # Nothing to do, or another tick holds it. Both are ordinary.
        return JobRun(kind=job.kind, claimed=False)

    cid = claimed.correlation_id or correlation_id or f"job-{uuid.uuid4()}"

    if claimed.resume_count >= max_resume_count:
        # Alert rather than loop forever (plan §2.2). The checkpoint is left
        # exactly as it was: a DEAD job is one a human resumes, and destroying
        # its cursor would destroy the only record of how far it got.
        store.finish_job(claimed.job_id, state=JOB_DEAD,
                         checkpoint=claimed.checkpoint, now=clock.now(),
                         note=STOP_MAX_RESUMES)
        alert("JOB_RESUME_LIMIT",
              f"job {job.kind} paused or failed {claimed.resume_count} times "
              f"without finishing",
              job_id=claimed.job_id, job_kind=job.kind,
              resume_count=claimed.resume_count)
        store.record_event(connection_id=claimed.connection_id or connection_id,
                           correlation_id=cid, kind="JOB_DEAD",
                           detail={"kind": job.kind,
                                   "resume_count": claimed.resume_count,
                                   "max_resume_count": max_resume_count,
                                   "reason": STOP_MAX_RESUMES})
        return JobRun(kind=job.kind, claimed=True, state=JOB_DEAD,
                      job_id=claimed.job_id, checkpoint=dict(claimed.checkpoint),
                      resume_count=claimed.resume_count, correlation_id=cid,
                      reason=STOP_MAX_RESUMES)

    ctx = JobContext(
        job_id=claimed.job_id, kind=claimed.kind, correlation_id=cid,
        clock=clock, started_at=started_at, soft_deadline_at=soft_deadline_at,
        checkpoint=dict(claimed.checkpoint), resume_count=claimed.resume_count,
        store=store, connection_id=claimed.connection_id or connection_id,
        capabilities=capabilities, budget=budget)

    steps = 0
    reason = STOP_COMPLETED
    error: str | None = None
    state = JOB_DONE
    #: The last step's own account of itself, carried onto the event so an
    #: operator on SCR-38 sees where a run got to, not merely that it stopped.
    last_detail: dict[str, Any] | None = None

    with correlation(cid):
        iterator = job.resume(ctx)
        try:
            while True:
                # ---- the deadline is checked HERE and nowhere else --------
                # Before pulling the next step, never inside one: a step that
                # has begun runs to its own commit point, because abandoning
                # it half-written is exactly the non-idempotency this contract
                # forbids.
                if ctx.soft_deadline_reached():
                    reason, state = STOP_SOFT_DEADLINE, JOB_CHECKPOINTED
                    break
                if ctx.budget_exhausted:
                    reason, state = STOP_RATE_BUDGET, JOB_CHECKPOINTED
                    break
                if ctx.requested_stop is not None:
                    reason, state = ctx.requested_stop, JOB_CHECKPOINTED
                    break
                if max_steps is not None and steps >= max_steps:
                    reason, state = STOP_MAX_STEPS, JOB_CHECKPOINTED
                    break

                try:
                    progress = next(iterator)
                except StopIteration:
                    # The job says it has nothing left. It may still have hit
                    # its budget on the way out -- ask, do not assume.
                    if ctx.budget_exhausted:
                        reason, state = STOP_RATE_BUDGET, JOB_CHECKPOINTED
                    elif ctx.requested_stop is not None:
                        reason, state = ctx.requested_stop, JOB_CHECKPOINTED
                    else:
                        reason, state = STOP_COMPLETED, JOB_DONE
                    break

                steps += 1
                ctx.units_done += progress.units
                if progress.detail is not None or progress.note is not None:
                    last_detail = {**dict(progress.detail or {}),
                                   **({"note": progress.note}
                                      if progress.note else {})}
                # Validated BEFORE it is adopted. A checkpoint that cannot
                # survive `jsonb` must not become the job's cursor even for an
                # instant: the run's terminal write would then carry it too,
                # and the failure path would fail on its way to recording the
                # failure -- leaving the row CLAIMED, leased, and stranded.
                candidate = dict(progress.checkpoint)
                assert_json_safe(candidate, where=f"{job.kind} step {steps}")
                ctx.checkpoint = candidate
                # Committed per step, not per run: a Function killed without
                # warning at minute fourteen keeps everything up to here.
                store.save_checkpoint(claimed.job_id, checkpoint=ctx.checkpoint,
                                      state=JOB_CLAIMED,
                                      resume_count=ctx.resume_count,
                                      now=clock.now())
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            reason, state = STOP_ERROR, JOB_FAILED
            error = f"{type(exc).__name__}: {exc}"
            log.error("integration job failed",
                      extra={"job_kind": job.kind, "job_id": claimed.job_id,
                             "error_class": type(exc).__name__})
        finally:
            # Closes the generator so a job's own `finally` runs even when the
            # runner stopped it at the deadline. A job is free to return a
            # plain iterator instead, which has no close() -- and a crash in
            # this `finally` would mask whatever actually ended the run.
            closer = getattr(iterator, "close", None)
            if callable(closer):
                closer()

        resume_count = ctx.resume_count
        if state in (JOB_CHECKPOINTED, JOB_FAILED):
            # A FAILURE counts toward the same bound as a pause, and is written
            # through the same call, because FAILED is not terminal (C16) and a
            # failed job is claimable again -- a transient fault must not be
            # terminal. But a PERMANENT one would then retry on every tick
            # forever, which is precisely the "looping forever" §2.2 forbids.
            # Counting both against `max_resume_count` means an invocation that
            # cannot succeed eventually goes DEAD and alerts a human, instead
            # of failing quietly every five minutes until somebody notices the
            # data is stale.
            resume_count = ctx.resume_count + 1
            store.save_checkpoint(claimed.job_id, checkpoint=ctx.checkpoint,
                                  state=state,
                                  resume_count=resume_count, now=clock.now())
        else:
            store.finish_job(claimed.job_id, state=state,
                             checkpoint=ctx.checkpoint, now=clock.now(),
                             note=reason)

        detail: dict[str, Any] = {
            "kind": job.kind, "steps": steps, "units": ctx.units_done,
            "calls": ctx.calls_used, "reason": reason,
            "resume_count": resume_count,
        }
        if last_detail is not None:
            detail["last_step"] = last_detail
        if error is not None:
            detail["error"] = error
        store.record_event(connection_id=ctx.connection_id,
                           correlation_id=cid, kind=f"JOB_{state}",
                           detail=detail)

    return JobRun(kind=job.kind, claimed=True, state=state,
                  job_id=claimed.job_id, checkpoint=dict(ctx.checkpoint),
                  steps=steps, units=ctx.units_done, calls=ctx.calls_used,
                  resume_count=resume_count, correlation_id=cid,
                  reason=reason, error=error)


def reschedule(store: JobStore, run: JobRun, *, clock: Clock) -> bool:
    """Return a finished recurring job to PENDING for its next tick.

    **The cursor survives because the ROW is reused.** A cron tick that
    enqueued a fresh `job` row every time would hand the next invocation an
    empty checkpoint, and a cursor like `sweep_po_anchored`'s
    `last_po_id_swept` would reset to the beginning of the open-PO population
    on every tick -- so the walk would re-read the first 50 POs for ever and
    the 51st would never be swept at all. On ERP, where this walk is the only
    way a receive is ever seen, that is not a performance problem: it is
    receives that are never acquired.

    So a recurring job is one row whose state cycles
    ``PENDING → CLAIMED → (CHECKPOINTED → CLAIMED)* → DONE → PENDING``,
    carrying its checkpoint the whole way. `resume_count` resets, because the
    next tick is fresh work rather than another resume of the old work.

    Returns True when it actually rescheduled. A run that is still
    CHECKPOINTED needs no help -- it is already claimable -- and a DEAD one
    must not be revived without a human, which is the entire point of DEAD.
    """
    if run.state != JOB_DONE or run.job_id is None:
        return False
    store.save_checkpoint(run.job_id, checkpoint=run.checkpoint,
                          state=JOB_PENDING, resume_count=0, now=clock.now())
    return True


def run_until_complete(job: ChunkedJob, *, store: JobStore, clock: Clock,
                       max_invocations: int = 50, **kwargs: Any) -> list[JobRun]:
    """Tick `run_job` until it reports DONE (or stops making progress).

    This is what the cron schedule does over successive minutes, collapsed into
    one call so a test can assert the *end* state of a job that needs many
    invocations without pretending one invocation was enough. Production never
    calls it: on Catalyst each tick is a separate Function invocation.
    """
    runs: list[JobRun] = []
    for _ in range(max_invocations):
        run = run_job(job, store=store, clock=clock, **kwargs)
        runs.append(run)
        if not run.claimed or run.state in (JOB_DONE, JOB_DEAD, JOB_FAILED):
            break
    return runs


# ------------------------------------------------------ the PostgreSQL claim

#: `job` carries no row-level scope dimension: it is a platform table written
#: by SVC-SWEEP under `Scope.system()` (plan §10.3). Every dimension is
#: therefore waived EXPLICITLY -- mapped to None rather than omitted -- so the
#: waiver is visible at the call site and in review. `repo.compile_scope`
#: refuses an omission precisely so this decision cannot be made by accident.
JOB_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": None, "plant": None, "project": None, "location": None,
}

#: Contract item 1, as a statement. The CTE takes the row locks with
#: `FOR UPDATE SKIP LOCKED LIMIT n` so an overlapping tick skips a claimed row
#: instead of blocking on it (blocking would burn the second tick's entire
#: 15 minutes waiting for the first), and the UPDATE stamps the lease so a
#: Function killed mid-run has its row reaped rather than stranded.
CLAIM_JOB_SQL = """
WITH claimable AS (
    SELECT job_id
      FROM job
     WHERE kind = %(kind)s
       AND state = ANY(%(claimable_states)s)
       AND (locked_until IS NULL OR locked_until <= %(now)s)
       AND (next_attempt_at IS NULL OR next_attempt_at <= %(now)s)
     ORDER BY created_at
     FOR UPDATE SKIP LOCKED
     LIMIT %(limit)s
)
UPDATE job AS j
   SET state = %(claimed_state)s,
       locked_until = %(lease_until)s,
       soft_deadline_at = %(soft_deadline_at)s,
       updated_at = %(now)s
  FROM claimable AS c
 WHERE j.job_id = c.job_id
   AND {scope}
RETURNING j.job_id, j.kind, j.checkpoint, j.resume_count,
          j.correlation_id, j.connection_id
"""

#: The lease is released by any state other than CLAIMED: the invocation is
#: over and the next cron tick must be able to claim the row. Holding it to
#: expiry would idle the work for the whole lease window; only a crash, which
#: never reaches this statement at all, keeps its lease.
SAVE_CHECKPOINT_SQL = """
UPDATE job
   SET checkpoint = %(checkpoint)s,
       state = %(state)s,
       resume_count = %(resume_count)s,
       locked_until = CASE WHEN %(state)s = %(claimed_state)s
                           THEN locked_until ELSE NULL END,
       updated_at = %(now)s
 WHERE job_id = %(job_id)s
   AND {scope}
RETURNING job_id
"""

FINISH_JOB_SQL = """
UPDATE job
   SET checkpoint = %(checkpoint)s,
       state = %(state)s,
       note = %(note)s,
       locked_until = NULL,
       finished_at = %(now)s,
       updated_at = %(now)s
 WHERE job_id = %(job_id)s
   AND {scope}
RETURNING job_id
"""

RECORD_EVENT_SQL = """
INSERT INTO integration_event
       (event_id, connection_id, correlation_id, kind, detail, created_at)
SELECT %(event_id)s, %(connection_id)s, %(correlation_id)s, %(kind)s,
       %(detail)s, %(now)s
 WHERE {scope}
RETURNING event_id
"""


class PostgresJobStore:
    """The job half of :class:`JobStore`, against the frozen C2 columns.

    Stream 2 owns the migration and `pg/integration_store.py`. This class
    exists because the claim statement is part of the job contract and had to
    be somewhere with a test on it; when `integration_store.py` grows an
    equivalent, this becomes a delegate rather than a second implementation.

    Every statement goes through `repo.query` with a literal ``{scope}`` token
    and an explicit four-dimension waiver -- see :data:`JOB_SCOPE_COLUMNS`.

    Columns this depends on beyond the C2 minimum, reported to stream 2 rather
    than assumed silently: ``job.locked_until``, ``job.next_attempt_at``,
    ``job.created_at``, ``job.updated_at``, ``job.finished_at``, ``job.note``,
    ``job.connection_id``.
    """

    def __init__(self, session: Any) -> None:
        self.session = session

    # `repo` is imported lazily so importing this module in a Function bundle
    # that has no psycopg (the pure-adapter unit suites) still works.
    @staticmethod
    def _repo() -> Any:
        from ..pg import repo  # local import: see above
        return repo

    def claim_job(self, *, kind: str, now: datetime, lease_seconds: int,
                  soft_deadline_at: datetime, limit: int = 1) -> ClaimedJob | None:
        rows = self._repo().query(
            self.session, CLAIM_JOB_SQL,
            {"kind": kind,
             "claimable_states": list(CLAIMABLE_STATES),
             "claimed_state": JOB_CLAIMED,
             "now": now,
             "lease_until": now + timedelta(seconds=lease_seconds),
             "soft_deadline_at": soft_deadline_at,
             "limit": limit},
            columns=JOB_SCOPE_COLUMNS)
        if not rows:
            return None
        job_id, job_kind, checkpoint, resume_count, cid, connection_id = rows[0]
        return ClaimedJob(job_id=job_id, kind=job_kind,
                          checkpoint=dict(checkpoint or {}),
                          resume_count=resume_count or 0,
                          correlation_id=cid, connection_id=connection_id)

    def save_checkpoint(self, job_id: str, *, checkpoint: Mapping[str, Any],
                        state: str, resume_count: int, now: datetime) -> None:
        assert_json_safe(checkpoint, where=f"save_checkpoint({job_id})")
        self._repo().query(
            self.session, SAVE_CHECKPOINT_SQL,
            {"job_id": job_id, "checkpoint": json.dumps(dict(checkpoint)),
             "state": state, "claimed_state": JOB_CLAIMED,
             "resume_count": resume_count, "now": now},
            columns=JOB_SCOPE_COLUMNS)

    def finish_job(self, job_id: str, *, state: str,
                   checkpoint: Mapping[str, Any], now: datetime,
                   note: str | None = None) -> None:
        assert_json_safe(checkpoint, where=f"finish_job({job_id})")
        self._repo().query(
            self.session, FINISH_JOB_SQL,
            {"job_id": job_id, "checkpoint": json.dumps(dict(checkpoint)),
             "state": state, "note": note, "now": now},
            columns=JOB_SCOPE_COLUMNS)

    def record_event(self, *, connection_id: str | None,
                     correlation_id: str | None, kind: str,
                     detail: Mapping[str, Any]) -> None:
        self._repo().query(
            self.session, RECORD_EVENT_SQL,
            {"event_id": f"EVT-{uuid.uuid4()}", "connection_id": connection_id,
             "correlation_id": correlation_id, "kind": kind,
             "detail": json.dumps(dict(detail)),
             "now": datetime.now(timezone.utc)},
            columns=JOB_SCOPE_COLUMNS)


# ---------------------------------------------------------------- windowing

@dataclass(frozen=True)
class Window:
    """One bounded polling window, and how it was chosen.

    `full_repull` is not a detail: on Inventory POs there is neither a filter
    nor a sort (plan §11.3), so "the delta since the watermark" is not a
    question that can be asked. Saying so in the type stops a caller treating
    an unbounded re-pull as if it were a delta.
    """

    since: datetime | None
    until: datetime
    full_repull: bool
    overlap_seconds: int = POLL_OVERLAP_SECONDS
    reason: str = ""

    def as_checkpoint(self) -> dict[str, Any]:
        return {"window_since": iso(self.since), "window_until": iso(self.until),
                "full_repull": self.full_repull, "window_reason": self.reason}


def plan_window(*, now: datetime, hwm: datetime | None, delta_filter: bool,
                window_seconds: int = DEFAULT_WINDOW_SECONDS,
                overlap_seconds: int = POLL_OVERLAP_SECONDS,
                reason: str = "") -> Window:
    """Choose the window for one poll from a **declared capability**.

    `delta_filter` comes from `Capabilities`, never from a product name. When
    it is False the product cannot express "changed since", and the honest
    answer is a full re-pull -- not a narrower window that would silently miss
    everything modified outside it.

    When it is True the window is ``[hwm − overlap, min(hwm + window, now)]``.
    The overlap is re-read on every run because `last_modified_time` is
    filterable but not sortable, so the walk cannot be resumed exactly; the
    inbox's `payload_sha` uniqueness makes the re-read free.
    """
    if not delta_filter:
        return Window(since=None, until=now, full_repull=True,
                      overlap_seconds=0,
                      reason=reason or "no delta filter on this product")
    if hwm is None:
        return Window(since=None, until=now, full_repull=True,
                      overlap_seconds=overlap_seconds,
                      reason=reason or "no watermark yet: first full pull")
    since = hwm - timedelta(seconds=overlap_seconds)
    until = min(hwm + timedelta(seconds=window_seconds), now)
    if until <= since:
        until = now
    return Window(since=since, until=until, full_repull=False,
                  overlap_seconds=overlap_seconds,
                  reason=reason or "windowed delta with overlap")


def next_watermark(window: Window, *, exhausted: bool,
                   current: datetime | None) -> datetime | None:
    """The watermark after this run, or the unchanged one.

    Separated out and given a test of its own because it is the single
    decision in the whole framework that can lose data. The watermark moves to
    ``window.until`` **only** when every page of the window was consumed. A run
    that stopped at the soft deadline, or ran out of rate budget, leaves it
    exactly where it was: the next tick re-enters the same window, re-reads
    what it already has (free, via the inbox `payload_sha` dedupe) and
    continues. Advancing on a partial run would skip whatever the run did not
    reach, and nothing downstream would ever notice.

    It also never moves BACKWARDS. Two ticks can overlap -- the claim skips a
    locked row, but a reaped lease and a manual re-run can both be in flight --
    and a stale window that finished late must not undo a newer one.
    """
    if not exhausted:
        return current
    if current is not None and window.until <= current:
        return current
    return window.until


__all__ = [
    "APPSAIL_REQUEST_CEILING_SECONDS", "CHUNK_SIZES", "CLAIMABLE_STATES",
    "CLAIM_JOB_SQL", "Capabilities", "ChunkedJob", "ClaimedJob", "Clock",
    "DEFAULT_LEASE_SECONDS", "DEFAULT_MAX_RESUME_COUNT",
    "DEFAULT_WINDOW_SECONDS", "FUNCTION_CEILING_SECONDS", "JOB_CHECKPOINTED",
    "JOB_CLAIMED", "JOB_DEAD", "JOB_DONE", "JOB_FAILED", "JOB_PENDING",
    "JOB_SCOPE_COLUMNS", "JOB_STATUSES", "JobContext", "JobError", "JobRun",
    "JobStore", "MAX_PAGES_PER_POLL", "NullBudget", "POLL_OVERLAP_SECONDS",
    "PostgresJobStore", "Progress", "RateBudget", "SOFT_DEADLINE_SECONDS",
    "STOP_COMPLETED", "STOP_ERROR", "STOP_JOB_REQUESTED", "STOP_MAX_RESUMES",
    "STOP_MAX_STEPS", "STOP_RATE_BUDGET", "STOP_SOFT_DEADLINE", "SystemClock",
    "Window", "assert_json_safe", "iso", "next_watermark", "parse_iso",
    "plan_window", "reschedule", "run_job", "run_until_complete",
]
