"""The job contract: bounded, chunked, checkpointed, idempotent.

Plan §2.2 states four requirements for every background activity, because
AppSail has no resident process and a Catalyst Function is killed at fifteen
minutes:

    1. claim a bounded batch with SELECT ... FOR UPDATE SKIP LOCKED LIMIT n
    2. hold a soft deadline of 12 minutes; on reaching it commit and return
    3. persist a resumable cursor -- never rely on finishing in one invocation
    4. be idempotent: re-running from the last checkpoint produces the same
       state

Each of the four has a test below whose name says which one it is. The central
one is
`test_a_job_with_more_work_than_fits_in_twelve_minutes_checkpoints_and_resumes`
and its idempotency twin: they are the difference between a job framework and a
loop that happens to work when the data is small.

No database, no network, no clock the test cannot control.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.integration import jobs, sweeps
from app.backend.pg import repo
from app.backend.pg.engine import Scope
from integration_fakes import (
    ERP,
    T0,
    CountingBudget,
    FakeAdapter,
    FakeClock,
    InMemoryStore,
    bill,
    pages_of,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "research" / "30_contracts"


# ===================================================== the constants are real

def test_the_soft_deadline_is_eighty_percent_of_the_function_ceiling():
    """12 minutes of 15. The remaining 3 are margin, not spare capacity."""
    assert jobs.FUNCTION_CEILING_SECONDS == 900
    assert jobs.SOFT_DEADLINE_SECONDS == 720
    assert jobs.SOFT_DEADLINE_SECONDS == int(0.8 * jobs.FUNCTION_CEILING_SECONDS)


def test_batch_work_is_never_scheduled_through_the_thirty_second_http_tier():
    """§2.2: 'Job Pool -> AppSail service (HTTP) ... never for batch work'."""
    assert jobs.APPSAIL_REQUEST_CEILING_SECONDS == 30
    assert jobs.SOFT_DEADLINE_SECONDS > jobs.APPSAIL_REQUEST_CEILING_SECONDS


def test_a_soft_deadline_past_the_ceiling_is_refused():
    """A deadline after the kill point is not a deadline."""
    store = InMemoryStore()
    store.enqueue("poll_bills")
    with pytest.raises(jobs.JobError):
        jobs.run_job(_NoWork("poll_bills"), store=store, clock=FakeClock(),
                     soft_deadline_seconds=jobs.FUNCTION_CEILING_SECONDS + 1)


def test_job_states_are_exactly_the_c16_job_namespace():
    """States come from the registry, never invented (Wave 5 contracts, C3)."""
    registry = json.loads(
        (CONTRACTS / "C16_integration_statuses.json").read_text(encoding="utf-8"))
    declared = {s["code"] for s in registry["namespaces"]["job"]["statuses"]}
    assert jobs.JOB_STATUSES == declared


def test_the_chunk_sizes_are_the_ones_the_plan_tabulates():
    """§2.2's table, in code. 200-record pages, <=40 pages; 50 POs a sweep."""
    assert jobs.CHUNK_SIZES["poll_bills"] == 200
    assert jobs.CHUNK_SIZES["poll_purchaseorders"] == 200
    assert jobs.MAX_PAGES_PER_POLL == 40
    assert jobs.CHUNK_SIZES["sweep_po_anchored"] == 50
    assert jobs.CHUNK_SIZES["drain_outbox"] == 25


# ================================================== 1. the bounded claim

class _FakeSession:
    """Enough of `pg.engine.Session` for `repo.query` to run against."""

    def __init__(self, rows: list[tuple] | None = None,
                 scope: Scope | None = None) -> None:
        self.rows = rows or []
        self.scope = scope or Scope.system()
        self.statements: list[tuple[str, dict]] = []

    def fetchall(self, statement: str, params=None) -> list[tuple]:
        self.statements.append((statement, dict(params or {})))
        return self.rows


def test_the_claim_takes_a_bounded_batch_with_for_update_skip_locked():
    """Contract item 1, as a statement that actually reaches the database.

    SKIP LOCKED and not plain FOR UPDATE: a second tick that BLOCKED on the
    first would spend its entire fifteen minutes queueing behind it and then
    be killed, so the platform would lose a whole invocation per overlap.
    """
    session = _FakeSession(rows=[("JOB-1", "poll_bills", {"page": 3}, 2,
                                  "corr-1", "CONN-1")])
    store = jobs.PostgresJobStore(session)
    claimed = store.claim_job(kind="poll_bills", now=T0, lease_seconds=1200,
                              soft_deadline_at=T0 + timedelta(seconds=720))

    statement, params = session.statements[0]
    assert "FOR UPDATE" in statement and "SKIP LOCKED" in statement
    assert "LIMIT %(limit)s" in statement
    assert params["limit"] == 1
    assert params["claimable_states"] == list(jobs.CLAIMABLE_STATES)
    assert claimed == jobs.ClaimedJob(
        job_id="JOB-1", kind="poll_bills", checkpoint={"page": 3},
        resume_count=2, correlation_id="corr-1", connection_id="CONN-1")


def test_every_job_statement_carries_the_scope_token_and_waives_all_four():
    """The `{scope}` token is not decoration: `repo.query` refuses without it.

    `job` carries no scope dimension, so all four are waived -- but waived
    EXPLICITLY, mapped to None. `repo.compile_scope` refuses an omission
    precisely so a table with no scope column cannot silently widen a
    restricted principal's access, and this asserts the mapping is complete for
    every restriction a scope can express.
    """
    for statement in (jobs.CLAIM_JOB_SQL, jobs.SAVE_CHECKPOINT_SQL,
                      jobs.FINISH_JOB_SQL, jobs.RECORD_EVENT_SQL):
        repo.require_scope_token(statement)

    restricted = Scope(user_id="u", entity_ids=frozenset({"ENT-1"}),
                       plant_ids=frozenset({"PL-1"}),
                       project_ids=frozenset({"PRJ-1"}),
                       location_ids=frozenset({"LOC-1"}))
    predicate, params = repo.compile_scope(restricted, jobs.JOB_SCOPE_COLUMNS)
    assert predicate == "TRUE" and params == {}
    assert set(jobs.JOB_SCOPE_COLUMNS) == {"entity", "plant", "project", "location"}


def test_a_checkpoint_write_releases_the_lease_unless_the_job_is_still_running():
    """A checkpointed job must be claimable by the next tick, not leased out."""
    session = _FakeSession()
    jobs.PostgresJobStore(session).save_checkpoint(
        "JOB-1", checkpoint={"page": 2}, state=jobs.JOB_CHECKPOINTED,
        resume_count=1, now=T0)
    statement, params = session.statements[0]
    assert "locked_until = CASE WHEN %(state)s = %(claimed_state)s" in statement
    assert params["claimed_state"] == jobs.JOB_CLAIMED


def test_a_second_tick_skips_a_leased_job_instead_of_doubling_the_work():
    store = InMemoryStore()
    store.enqueue("poll_bills")
    first = store.claim_job(kind="poll_bills", now=T0, lease_seconds=1200,
                            soft_deadline_at=T0)
    second = store.claim_job(kind="poll_bills", now=T0 + timedelta(seconds=60),
                             lease_seconds=1200, soft_deadline_at=T0)
    assert first is not None and second is None


def test_an_expired_lease_is_reaped_so_a_killed_function_is_not_stranded():
    store = InMemoryStore()
    store.enqueue("poll_bills")
    store.claim_job(kind="poll_bills", now=T0, lease_seconds=1200,
                    soft_deadline_at=T0)
    later = T0 + timedelta(seconds=1201)
    assert store.claim_job(kind="poll_bills", now=later, lease_seconds=1200,
                           soft_deadline_at=later) is not None


def test_nothing_to_claim_is_an_ordinary_outcome_not_an_error():
    run = jobs.run_job(_NoWork("poll_bills"), store=InMemoryStore(),
                       clock=FakeClock())
    assert run.claimed is False and run.state is None


# ============================================ 2, 3. deadline and checkpoint

class _NoWork:
    """A job with nothing to do."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def resume(self, ctx):
        return iter(())


class _CountingJob:
    """`total` steps, each costing `seconds` of simulated time."""

    def __init__(self, kind: str, *, total: int, seconds: float) -> None:
        self.kind = kind
        self.total = total
        self.seconds = seconds
        self.closed = 0

    def resume(self, ctx):
        done = int(ctx.checkpoint.get("done", 0))
        try:
            while done < self.total:
                ctx.clock.advance(self.seconds)
                done += 1
                yield jobs.Progress(checkpoint={"done": done}, units=1)
        finally:
            self.closed += 1


def test_a_job_that_finishes_inside_the_deadline_is_done():
    clock = FakeClock()
    store = InMemoryStore()
    row = store.enqueue("sweep_bill_detail")
    run = jobs.run_job(_CountingJob("sweep_bill_detail", total=3, seconds=1),
                       store=store, clock=clock)
    assert (run.state, run.units, run.reason) == (
        jobs.JOB_DONE, 3, jobs.STOP_COMPLETED)
    assert store.job_rows[row.job_id].state == jobs.JOB_DONE


def test_a_job_with_more_work_than_fits_in_twelve_minutes_checkpoints_and_resumes():
    """THE test. Twenty units of work, ninety seconds each: thirty minutes.

    The 12-minute soft deadline fits exactly eight of them, so a correct
    framework produces three invocations of 8 + 8 + 4 and a DONE at the end,
    with no unit done twice and none skipped. Proven with an injected clock:
    the job advances it as it works, exactly as real work would.

    The three failures this is really watching for:
      * reporting DONE at the deadline, which silently loses the remaining 12;
      * resuming from zero, which does all 20 again -- harmless only if every
        write happens to be idempotent, and catastrophic the day one is not;
      * carrying on past the deadline, which is a Function killed mid-write.
    """
    clock = FakeClock()
    store = InMemoryStore()
    row = store.enqueue("sweep_po_anchored")
    job = _CountingJob("sweep_po_anchored", total=20, seconds=90)

    first = jobs.run_job(job, store=store, clock=clock)
    assert first.state == jobs.JOB_CHECKPOINTED
    assert first.reason == jobs.STOP_SOFT_DEADLINE
    assert first.units == 8, "12 minutes / 90 seconds = 8 units, not 9"
    assert first.checkpoint == {"done": 8}
    assert first.resume_count == 1
    assert store.job_rows[row.job_id].state == jobs.JOB_CHECKPOINTED
    assert job.closed == 1, "the generator must be closed at the deadline"

    second = jobs.run_job(job, store=store, clock=clock)
    assert (second.state, second.units, second.checkpoint) == (
        jobs.JOB_CHECKPOINTED, 8, {"done": 16})
    assert second.resume_count == 2

    third = jobs.run_job(job, store=store, clock=clock)
    assert (third.state, third.units, third.checkpoint, third.reason) == (
        jobs.JOB_DONE, 4, {"done": 20}, jobs.STOP_COMPLETED)

    assert first.units + second.units + third.units == 20
    assert store.job_rows[row.job_id].state == jobs.JOB_DONE
    # And the whole thing took the thirty minutes it should have.
    assert clock.now() == T0 + timedelta(seconds=20 * 90)


def test_the_cursor_is_committed_after_every_step_not_only_at_the_end():
    """A Function killed without warning keeps everything up to the last step.

    If the checkpoint were written only when the deadline arrived, a hard kill
    at minute fourteen would lose the whole invocation's work -- and the job
    would redo it on every subsequent tick, forever, never finishing.
    """
    clock = FakeClock()
    store = InMemoryStore()
    store.enqueue("poll_bills")
    jobs.run_job(_CountingJob("poll_bills", total=5, seconds=1), store=store,
                 clock=clock)
    per_step = [cp for _, state, cp in store.checkpoint_writes
                if state == jobs.JOB_CLAIMED]
    assert per_step == [{"done": 1}, {"done": 2}, {"done": 3}, {"done": 4},
                        {"done": 5}]


def test_a_checkpoint_that_could_not_survive_jsonb_is_refused_at_the_step():
    """`job.checkpoint` is jsonb. A datetime in it fails at the boundary --
    in a Function, at minute eleven, with the run's progress inside it."""

    class _BadCheckpoint:
        kind = "poll_bills"

        def resume(self, ctx):
            yield jobs.Progress(checkpoint={"until": datetime.now(timezone.utc)})

    store = InMemoryStore()
    store.enqueue("poll_bills")
    run = jobs.run_job(_BadCheckpoint(), store=store, clock=FakeClock())
    assert run.state == jobs.JOB_FAILED
    assert "JobError" in (run.error or "")


def test_a_job_that_raises_is_recorded_failed_rather_than_crashing_the_tick():
    class _Boom:
        kind = "poll_bills"

        def resume(self, ctx):
            yield jobs.Progress(checkpoint={"done": 1})
            raise ValueError("upstream said no")

    store = InMemoryStore()
    row = store.enqueue("poll_bills")
    run = jobs.run_job(_Boom(), store=store, clock=FakeClock())
    assert run.state == jobs.JOB_FAILED
    assert run.error is not None and "upstream said no" in run.error
    assert run.checkpoint == {"done": 1}, "progress before the failure is kept"
    assert store.job_rows[row.job_id].state == jobs.JOB_FAILED
    assert store.events[-1]["kind"] == "JOB_FAILED"


def test_a_failed_job_is_claimable_again_so_a_transient_fault_is_not_terminal():
    assert jobs.JOB_FAILED in jobs.CLAIMABLE_STATES
    assert jobs.JOB_DONE not in jobs.CLAIMABLE_STATES
    assert jobs.JOB_DEAD not in jobs.CLAIMABLE_STATES


def test_a_claimed_row_is_reclaimable_only_through_its_expired_lease():
    """CLAIMED is claimable, but only once the lease says the invocation
    cannot still be running. The state alone cannot tell a live Function from
    one the platform killed at the ceiling; the lease can."""
    assert jobs.JOB_CLAIMED in jobs.CLAIMABLE_STATES
    assert "locked_until IS NULL OR locked_until <= %(now)s" in jobs.CLAIM_JOB_SQL
    assert jobs.DEFAULT_LEASE_SECONDS > jobs.FUNCTION_CEILING_SECONDS


# ================================================== rate budget and resumes

def test_running_out_of_rate_budget_checkpoints_and_never_reports_done():
    """§11.6: over budget -> the job checkpoints and returns; the next cron
    tick resumes. Reporting DONE here would abandon the rest of the work."""

    class _Spender:
        kind = "sweep_po_anchored"

        def resume(self, ctx):
            done = int(ctx.checkpoint.get("done", 0))
            while done < 10:
                if not ctx.charge(1):
                    return
                done += 1
                yield jobs.Progress(checkpoint={"done": done})

    store = InMemoryStore()
    store.enqueue("sweep_po_anchored")
    budget = CountingBudget(ceiling=3)
    run = jobs.run_job(_Spender(), store=store, clock=FakeClock(),
                       budget=budget)
    assert run.state == jobs.JOB_CHECKPOINTED
    assert run.reason == jobs.STOP_RATE_BUDGET
    assert run.checkpoint == {"done": 3}
    assert run.calls == 3 and budget.refusals == 1


def test_a_job_that_checkpoints_too_many_times_goes_dead_and_alerts():
    """§2.2: 'A job exceeding max_resume_count raises an alert rather than
    looping forever.' The checkpoint is preserved -- a DEAD job is one a human
    resumes, and destroying its cursor destroys the record of how far it got."""
    store = InMemoryStore()
    row = store.enqueue("poll_bills", checkpoint={"page": 7})
    row.resume_count = 20
    run = jobs.run_job(_CountingJob("poll_bills", total=5, seconds=1),
                       store=store, clock=FakeClock(), max_resume_count=20)
    assert run.state == jobs.JOB_DEAD
    assert run.reason == jobs.STOP_MAX_RESUMES
    assert store.job_rows[row.job_id].checkpoint == {"page": 7}
    assert store.events[-1]["kind"] == "JOB_DEAD"


def test_a_permanently_failing_job_goes_dead_rather_than_retrying_forever():
    """FAILED is claimable again, which is right for a transient fault and
    wrong for a permanent one.

    A job that cannot succeed would otherwise fail on every tick for ever,
    quietly, while the data it maintains goes stale and nobody is told. So a
    failure counts toward the same bound as a pause, and the job eventually
    goes DEAD and alerts.
    """

    class _AlwaysBroken:
        kind = "poll_bills"

        def resume(self, ctx):
            raise RuntimeError("the vendor changed the payload shape")
            yield  # pragma: no cover - unreachable, keeps this a generator

    store = InMemoryStore()
    row = store.enqueue("poll_bills")
    clock = FakeClock()
    states = []
    for _ in range(5):
        states.append(jobs.run_job(_AlwaysBroken(), store=store, clock=clock,
                                   max_resume_count=3).state)
    assert states == [jobs.JOB_FAILED, jobs.JOB_FAILED, jobs.JOB_FAILED,
                      jobs.JOB_DEAD, None]
    assert row.resume_count == 3
    assert store.events[-1]["kind"] == "JOB_DEAD"


def test_a_checkpoint_that_cannot_be_stored_never_becomes_the_cursor():
    """The terminal write must not fail on its way to recording a failure."""

    class _GoodThenBad:
        kind = "poll_bills"

        def resume(self, ctx):
            yield jobs.Progress(checkpoint={"page": 1})
            yield jobs.Progress(checkpoint={"page": datetime.now(timezone.utc)})

    store = InMemoryStore()
    row = store.enqueue("poll_bills")
    run = jobs.run_job(_GoodThenBad(), store=store, clock=FakeClock())
    assert run.state == jobs.JOB_FAILED
    assert row.checkpoint == {"page": 1}, "the last GOOD cursor survives"


def test_run_until_complete_drives_a_long_job_to_done_over_many_ticks():
    clock = FakeClock()
    store = InMemoryStore()
    store.enqueue("sweep_po_anchored")
    runs = jobs.run_until_complete(
        _CountingJob("sweep_po_anchored", total=30, seconds=90),
        store=store, clock=clock)
    assert [r.state for r in runs][-1] == jobs.JOB_DONE
    assert sum(r.units for r in runs) == 30
    assert len(runs) == 4, "8 + 8 + 8 + 6"


# ===================================================== 4. idempotency

def _poll_estate(clock: FakeClock, *, records=60, page_size=5,
                 seconds_per_call=90.0):
    store = InMemoryStore()
    store.enqueue("poll_bills")
    modified = T0 - timedelta(minutes=30)
    adapter = FakeAdapter(
        clock,
        bill_pages=pages_of([bill(n, modified=modified + timedelta(seconds=n))
                             for n in range(records)], page_size=page_size),
        seconds_per_call=seconds_per_call)
    job = sweeps.poll_bills(adapter, store, store.connection_id,
                            page_size=page_size)
    return store, adapter, job


def test_re_running_from_the_last_checkpoint_produces_identical_state():
    """Contract item 4, and the reason the 300-second overlap costs nothing.

    Two estates walk the same 60 bills. The first is interrupted at the soft
    deadline and resumed; the second runs the same checkpoints again from
    scratch -- including replaying an already-completed invocation, which is
    what a Function killed after its work but before its acknowledgement
    produces. The inbox, the watermark and the exception set must be identical,
    not merely similar.
    """
    clock_a = FakeClock()
    store_a, _, job_a = _poll_estate(clock_a)
    runs_a = jobs.run_until_complete(job_a, store=store_a, clock=clock_a,
                                     capabilities=ERP)
    assert runs_a[-1].state == jobs.JOB_DONE
    assert len(runs_a) > 1, "the estate must actually span invocations"

    clock_b = FakeClock()
    store_b, _, job_b = _poll_estate(clock_b)
    runs_b = jobs.run_until_complete(job_b, store=store_b, clock=clock_b,
                                     capabilities=ERP)
    # Replay the last invocation a second time from its own checkpoint: the
    # crash-after-work case.
    last_row = list(store_b.job_rows.values())[0]
    last_row.state = jobs.JOB_CHECKPOINTED
    replay = jobs.run_job(job_b, store=store_b, clock=clock_b, capabilities=ERP)
    assert replay.state in (jobs.JOB_DONE, jobs.JOB_CHECKPOINTED)

    assert store_a.snapshot() == store_b.snapshot()
    assert len(store_a.inbox) == 60
    assert runs_b  # silence the unused-name lint without weakening anything


def test_the_watermark_never_moves_on_a_run_that_only_checkpointed():
    """The one decision in the framework that can lose data.

    Advancing the high-water mark on a partial run skips whatever the run did
    not reach, and nothing downstream ever notices: the next window starts
    after the records that were missed.
    """
    clock = FakeClock()
    store, _, job = _poll_estate(clock, records=100, page_size=5)
    store.watermarks[(store.connection_id, sweeps.MODULE_BILLS)] = T0 - timedelta(hours=1)

    first = jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert first.state == jobs.JOB_CHECKPOINTED
    assert store.watermarks[(store.connection_id, sweeps.MODULE_BILLS)] == \
        T0 - timedelta(hours=1)


def test_next_watermark_holds_still_unless_the_window_was_exhausted():
    window = jobs.Window(since=T0 - timedelta(hours=1), until=T0,
                         full_repull=False)
    previous = T0 - timedelta(hours=1)
    assert jobs.next_watermark(window, exhausted=False, current=previous) == previous
    assert jobs.next_watermark(window, exhausted=True, current=previous) == T0
    assert jobs.next_watermark(window, exhausted=True, current=None) == T0


def test_a_stale_window_can_never_drag_the_watermark_backwards():
    """Two overlapping runs: the older one must not undo the newer one."""
    stale = jobs.Window(since=None, until=T0 - timedelta(hours=2),
                        full_repull=False)
    assert jobs.next_watermark(stale, exhausted=True, current=T0) == T0


# ================================================= correlation propagation

def test_one_correlation_id_traces_the_job_through_inbox_and_event():
    """§11.9: one id traces a bill from HTTP response to ledger movement.

    The id is minted (or inherited) once by the runner and reaches every row
    the job writes -- so an operator holding a correlation id from an HTTP
    response can find the job, the payload it accepted and the event it
    emitted, without joining on timestamps and hoping.
    """
    clock = FakeClock()
    store, _, job = _poll_estate(clock, records=3, page_size=5,
                                 seconds_per_call=1)
    run = jobs.run_job(job, store=store, clock=clock, capabilities=ERP,
                       correlation_id="corr-tracing-1")
    assert run.correlation_id == "corr-tracing-1"
    assert {row["correlation_id"] for row in store.inbox.values()} == \
        {"corr-tracing-1"}
    assert {event["correlation_id"] for event in store.events} == \
        {"corr-tracing-1"}


def test_a_job_row_carrying_a_correlation_id_keeps_it_rather_than_minting_one():
    """A job enqueued by a request inherits that request's id."""
    clock = FakeClock()
    store = InMemoryStore()
    store.enqueue("poll_bills", correlation_id="corr-from-http")
    run = jobs.run_job(_CountingJob("poll_bills", total=1, seconds=1),
                       store=store, clock=clock, correlation_id="corr-other")
    assert run.correlation_id == "corr-from-http"


def test_a_job_with_no_correlation_id_anywhere_mints_one():
    store = InMemoryStore()
    store.enqueue("poll_bills")
    run = jobs.run_job(_CountingJob("poll_bills", total=1, seconds=1),
                       store=store, clock=FakeClock())
    assert run.correlation_id and run.correlation_id.startswith("job-")
    assert store.events[-1]["correlation_id"] == run.correlation_id


# ==================================================== windowing (§11.3)

def test_a_windowed_poll_re_reads_three_hundred_seconds_behind_its_watermark():
    """`last_modified_time` is filterable but NOT sortable, so the walk cannot
    be resumed exactly. The overlap is the answer, and it is free because the
    inbox's payload_sha uniqueness discards the re-read."""
    hwm = T0 - timedelta(hours=2)
    window = jobs.plan_window(now=T0, hwm=hwm, delta_filter=True)
    assert window.since == hwm - timedelta(seconds=300)
    assert jobs.POLL_OVERLAP_SECONDS == 300
    assert window.full_repull is False


def test_a_window_is_bounded_so_one_invocation_can_finish_it():
    hwm = T0 - timedelta(days=30)
    window = jobs.plan_window(now=T0, hwm=hwm, delta_filter=True)
    assert window.until == hwm + timedelta(seconds=jobs.DEFAULT_WINDOW_SECONDS)
    assert window.until < T0, "a 30-day backlog is walked a window at a time"


def test_no_delta_filter_means_a_full_repull_and_says_so():
    """Inventory POs have neither filter nor sort. A narrower window would be
    a delta we were never promised."""
    window = jobs.plan_window(now=T0, hwm=T0 - timedelta(hours=1),
                              delta_filter=False)
    assert window.full_repull is True and window.since is None


def test_the_first_ever_run_is_a_full_pull_not_an_empty_window():
    window = jobs.plan_window(now=T0, hwm=None, delta_filter=True)
    assert window.full_repull is True and window.since is None


def test_a_window_survives_a_round_trip_through_a_jsonb_checkpoint():
    window = jobs.plan_window(now=T0, hwm=T0 - timedelta(hours=1),
                              delta_filter=True)
    restored = json.loads(json.dumps(window.as_checkpoint()))
    assert jobs.parse_iso(restored["window_since"]) == window.since
    assert jobs.parse_iso(restored["window_until"]) == window.until
