"""The audit anchor's INVOCATION PATH -- the half that was missing.

WHAT THIS FILE IS FOR, AND WHY IT IS NOT MORE ANCHOR TESTS
----------------------------------------------------------
``tests/test_security_audit_anchor.py`` proves ``write_anchor`` and
``verify_anchors`` are correct. They always were. The finding this file closes
is different and worse: **nothing called them.** ``write_anchor``'s only
references outside its own module were that test file and a comment in
migration 024 -- no route (deliberately), no CLI, no schedule -- so in every
real deployment ``audit_anchor`` stayed empty and whole-stream truncation, the
one failure a hash chain provably cannot see, was undetectable.

A test that calls ``write_anchor`` itself cannot catch that. It is the shape of
test the module already had, and the table was still empty. So the tests here
are written the other way round: they invoke the **entry point** and assert the
writer ran, and they assert the entry point is the only way in.

    test_the_job_handler_actually_invokes_the_writer
    test_the_cli_entry_point_reaches_the_writer_through_the_handler
    test_no_production_code_reaches_write_anchor_except_the_job_handler

Delete the ``write_anchor`` call from ``jobs/audit_anchor.py`` and the first two
go red while every test in ``test_security_audit_anchor.py`` stays green --
which is exactly the state the product shipped in.

WHICH OF THESE HAVE ACTUALLY RUN
--------------------------------
The tests that need no database were executed on the authoring machine before
this file was committed. **Every ``@pytest.mark.pg`` test in this file has
NEVER EXECUTED anywhere**: there is no PostgreSQL on this machine, so they skip
here, and they first run in CI's ``pg_tests`` job. A skip is not a pass and is
not reported as one.

Every seed INSERT below is derived column by column from the ``CREATE TABLE``
in ``migrations/pg/001_foundation.sql`` -- ``audit_log`` and ``audit_anchor``
-- and satisfies migration 024's CHECK constraints. The anchor rows the live
tests create are written by the product's own writer rather than by hand,
precisely so a seed cannot drift from what the writer produces.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_audit.py's module docstring for why this block is repeated
# rather than referenced.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)
from test_security_audit_anchor import FakeSession  # noqa: E402

import contextlib                                                # noqa: E402
import json                                                      # noqa: E402
import os                                                        # noqa: E402
import re                                                        # noqa: E402
import threading                                                 # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date, datetime, timedelta, timezone         # noqa: E402

import pytest                                                    # noqa: E402

from app.backend.jobs import audit_anchor as job_mod             # noqa: E402
from app.backend.pg import audit as audit_mod                    # noqa: E402
from app.backend.pg.config import MappingSecretProvider          # noqa: E402

PROJECT_ROOT = _TESTS_DIR.parent

PG = pytest.mark.pg
LIVE = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

#: Long enough to satisfy MIN_TOKEN_LENGTH. A test token, never a real one --
#: and the production token is read from a secret provider by NAME, so nothing
#: here is a credential that could be copied into a deployment.
TEST_TOKEN = "t" * job_mod.MIN_TOKEN_LENGTH


def _secrets(token: str | None = TEST_TOKEN) -> MappingSecretProvider:
    return MappingSecretProvider(
        {} if token is None else {job_mod.ANCHOR_JOB_TOKEN_SECRET: token})


class ScriptedClock:
    """A clock that advances a fixed amount per read.

    Injected rather than waited on: proving a twelve-minute deadline by
    sleeping twelve minutes proves nothing anybody will run. The same reasoning
    `integration/jobs.py::Clock` is built on.
    """

    def __init__(self, start: datetime, step_seconds: float = 0.0):
        self._now = start
        self._step = timedelta(seconds=step_seconds)
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        current = self._now
        self._now = self._now + self._step
        return current


class FakeDatabase:
    """Enough of `Database` to run the handler with no PostgreSQL.

    Records the scope every session was opened with, because the scope this
    job runs under is load-bearing: a restricted one would make `stream_heads`
    return fewer streams and the anchor would silently omit the rest.
    """

    def __init__(self, session):
        self._session = session
        self.scopes: list = []

    @contextlib.contextmanager
    def session(self, scope):
        self.scopes.append(scope)
        yield self._session

    def close(self) -> None:                                  # pragma: no cover
        pass


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _populated_session():
    return FakeSession(entries={
        "PROJECT:PRJ-1": [(1, "h1a"), (2, "h1b"), (3, "h1c")],
        "PROJECT:PRJ-2": [(1, "h2a"), (2, "h2b")],
    })


# ======================================================================
# THE FINDING: the writer is reached THROUGH the entry point
# ======================================================================
def test_the_job_handler_actually_invokes_the_writer(monkeypatch):
    """The whole point of this stream, as one assertion.

    Not `write_anchor(session, ...)` -- that was always green while the table
    stayed empty. `run_anchor_job(...)`, and then: did the writer run?
    """
    calls: list[date | None] = []
    real = audit_mod.write_anchor

    def spy(session, *, anchor_date=None):
        calls.append(anchor_date)
        return real(session, anchor_date=anchor_date)

    monkeypatch.setattr(audit_mod, "write_anchor", spy)
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets())

    assert calls == [_today()], (
        "the job entry point did not reach write_anchor. That is the defect "
        "this stream exists to fix: a correct writer nobody calls.")
    assert run.outcome == job_mod.OUTCOME_SUCCEEDED
    assert run.anchor_written is True
    assert run.streams_anchored == 2
    assert len(session.anchors) == 1, "a row was actually written"


def test_the_cli_entry_point_reaches_the_writer_through_the_handler(monkeypatch):
    """`tools/anchor_job.py main()` -> `run_anchor_job` -> `write_anchor`.

    The CLI is one of the two callers that make the control operative (the
    other is a Catalyst Cron Function invoking the same handler). Patched at
    the DATABASE, not at the handler: patching `run_anchor_job` would prove
    the CLI calls something named `run_anchor_job` and nothing about whether
    an anchor is ever written.
    """
    import importlib

    tool = importlib.import_module("tools.anchor_job")
    session = _populated_session()
    calls: list[date | None] = []
    real = audit_mod.write_anchor

    def spy(s, *, anchor_date=None):
        calls.append(anchor_date)
        return real(s, anchor_date=anchor_date)

    monkeypatch.setattr(audit_mod, "write_anchor", spy)
    monkeypatch.setenv(job_mod.ANCHOR_JOB_TOKEN_SECRET, TEST_TOKEN)
    monkeypatch.setattr("app.backend.pg.config.from_env", lambda: object())
    monkeypatch.setattr("app.backend.pg.engine.Database",
                        lambda config, secret_provider=None, **kw: FakeDatabase(session))

    exit_code = tool.main(["--quiet"])

    assert calls == [_today()], (
        "python tools/anchor_job.py wrote no anchor. An entry point that does "
        "not reach the writer leaves the control exactly as inert as no entry "
        "point at all.")
    assert exit_code == 0
    assert len(session.anchors) == 1


def test_no_production_code_reaches_write_anchor_except_the_job_handler():
    """One invocation path, and it is the guarded one.

    A second caller is how one of them ends up without the credential check --
    and the second caller most likely to be added is an HTTP route, which
    `test_no_route_in_the_audit_router_writes_an_anchor` separately forbids.
    """
    definer = PROJECT_ROOT / "app" / "backend" / "pg" / "audit.py"
    callers: set[str] = set()
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        if path == definer:
            # Where it is DEFINED, and where the surrounding docstrings
            # discuss it by name. Excluded by path rather than by a regex that
            # tries to tell prose from code: that module says "write_anchor()"
            # repeatedly and on purpose.
            continue
        # Any MENTION, not just a call. An import of the name is the step
        # before a second call site exists, and is worth failing on.
        if "write_anchor" in path.read_text(encoding="utf-8"):
            callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == {"app/backend/jobs/audit_anchor.py"}, (
        f"write_anchor is called from {sorted(callers)}. Exactly one "
        "production caller is the design: it is the one that authenticates, "
        "authorises, holds the soft deadline and refuses a backdated anchor.")


def test_the_handler_runs_under_an_unrestricted_service_scope():
    """A scoped session would write a truthful-looking, degenerate anchor.

    `stream_heads` is the snapshot the anchor is computed from. If the session
    could see only some streams, every stream it could not see would be
    deletable with no finding -- and the anchor would report a pass, which is
    worse than `NEVER_ANCHORED`, because `NEVER_ANCHORED` at least says so.
    """
    session = _populated_session()
    database = FakeDatabase(session)

    job_mod.run_anchor_job(database, credential=TEST_TOKEN, secrets=_secrets())

    assert database.scopes, "the job opened no session at all"
    for scope in database.scopes:
        assert scope.user_id == job_mod.ANCHOR_JOB_PRINCIPAL
        assert scope.principal_kind == "SERVICE"
        assert scope.read_all is True
        assert scope.entity_ids is None and scope.plant_ids is None
        assert scope.project_ids is None and scope.location_ids is None


# ======================================================================
# Authentication and authorisation on the entry point
# ======================================================================
def test_an_unauthenticated_invocation_writes_nothing():
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=None,
                                 secrets=_secrets())

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    assert run.error_class == "AnchorJobDenied"
    assert session.anchors == [], (
        "an unauthenticated caller wrote into the append-only table the whole "
        "control rests on")


def test_a_wrong_token_is_refused_and_says_nothing_about_why():
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session),
                                 credential="w" * job_mod.MIN_TOKEN_LENGTH,
                                 secrets=_secrets())

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    assert session.anchors == []
    body = json.dumps(run.as_dict())
    assert "w" * job_mod.MIN_TOKEN_LENGTH not in body
    assert TEST_TOKEN not in body, "the result echoed the configured secret"


def test_a_deployment_with_no_configured_token_cannot_run_the_job():
    """Not "runs unauthenticated". Refuses.

    A job that falls back to no credential when none is configured is a job
    that is unauthenticated on exactly the deployment nobody configured.
    """
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets(token=None))

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    assert run.error_class == "AnchorJobNotConfigured"
    assert session.anchors == []


def test_a_short_configured_token_is_refused_as_not_a_credential():
    session = _populated_session()
    short = "s" * (job_mod.MIN_TOKEN_LENGTH - 1)

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=short,
                                 secrets=_secrets(token=short))

    assert run.error_class == "AnchorJobNotConfigured"
    assert session.anchors == []


def test_the_token_is_compared_in_constant_time():
    """`==` on strings returns as soon as two bytes differ, and how long that
    took says how much of the token was right."""
    import inspect

    source = inspect.getsource(job_mod.authorise)
    assert "hmac.compare_digest" in source
    assert not re.search(r"presented_token\s*==", source)


def test_the_cli_has_no_flag_that_puts_the_token_on_a_command_line():
    """A token in argv is in the shell history, in `ps`, and in the CI log."""
    import importlib

    tool = importlib.import_module("tools.anchor_job")
    options = {action.option_strings and action.option_strings[0]
               for action in tool.build_parser()._actions}
    assert "--token" not in options
    assert not any(opt and "token" in opt.lower() for opt in options if opt)


# ======================================================================
# Idempotency, checkpointing and the refusal to manufacture evidence
# ======================================================================
def test_a_second_invocation_on_the_same_day_writes_nothing_and_still_succeeds():
    """A job that crashed on its second run of the day is a job somebody stops
    scheduling, and an anchor nobody writes is the whole finding again."""
    session = _populated_session()
    database = FakeDatabase(session)

    first = job_mod.run_anchor_job(database, credential=TEST_TOKEN,
                                   secrets=_secrets())
    session.entries["PROJECT:PRJ-1"].append((4, "h1d"))
    second = job_mod.run_anchor_job(database, credential=TEST_TOKEN,
                                    secrets=_secrets())

    assert first.anchor_written is True
    assert second.anchor_written is False, (
        "'today is anchored' and 'I just anchored today' must be "
        "distinguishable, or a job that silently stopped running looks "
        "exactly like one that is working")
    assert second.outcome == job_mod.OUTCOME_SUCCEEDED
    assert len(session.anchors) == 1


def test_anchoring_a_past_day_is_refused():
    """An anchor dated last Tuesday would carry heads observed NOW.

    Migration 024's COMMENT ON TABLE states the consequence of a missed run --
    "a day on which the writer did not run is a day with no evidence" -- and
    the only honest response to that gap is to report it. Filling it in
    manufactures the evidence the control exists to be.
    """
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets(),
                                 anchor_date=_today() - timedelta(days=3))

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    assert run.error_class == "AnchorJobRefused"
    assert session.anchors == []


def test_a_gap_since_the_previous_anchor_is_reported_not_filled():
    session = _populated_session()
    database = FakeDatabase(session)
    # An anchor four days old, written by the product's own writer.
    audit_mod.write_anchor(session, anchor_date=_today() - timedelta(days=4))

    run = job_mod.run_anchor_job(database, credential=TEST_TOKEN,
                                 secrets=_secrets())

    assert run.unanchored_days == 3, (
        "three days between the previous anchor and today carry no anchor and "
        "never will; saying so is the point")
    assert len(session.anchors) == 2, "the gap was reported, not backfilled"


def test_a_checkpoint_from_another_date_is_discarded_rather_than_trusted():
    """A stale checkpoint claiming ANCHOR is done would leave today unanchored.

    Both phases are idempotent, so discarding a checkpoint costs repeated work
    and never a wrong answer -- which is the direction to be wrong in.
    """
    session = _populated_session()
    stale = {"anchor_date": (_today() - timedelta(days=1)).isoformat(),
             "phases_completed": [job_mod.PHASE_ANCHOR, job_mod.PHASE_VERIFY]}

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets(), checkpoint=stale)

    assert run.anchor_written is True
    assert len(session.anchors) == 1


def test_a_checkpoint_for_today_skips_the_phase_it_names():
    session = _populated_session()
    first = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                   secrets=_secrets())
    assert first.phases_completed == (job_mod.PHASE_ANCHOR, job_mod.PHASE_VERIFY)

    resumed = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                     secrets=_secrets(),
                                     checkpoint=first.checkpoint())

    assert resumed.outcome == job_mod.OUTCOME_SUCCEEDED
    assert resumed.anchor_written is False
    assert len(session.anchors) == 1


def test_the_checkpoint_survives_json():
    """A Catalyst Function receives its arguments as JSON. A checkpoint that
    cannot round-trip through it is not a checkpoint."""
    session = _populated_session()
    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets())

    assert json.loads(json.dumps(run.checkpoint())) == run.checkpoint()
    assert json.loads(json.dumps(run.as_dict()))["outcome"] == run.outcome


# ======================================================================
# The deadline: PARTIAL is not SUCCEEDED
# ======================================================================
def test_a_run_that_reaches_the_soft_deadline_still_anchors_and_reports_partial():
    """Anchoring first, always.

    Verification is the reporting half; a run that ran out of time must have
    spent it on the half that WRITES the evidence. And "verified nothing" must
    never come back as SUCCEEDED.
    """
    session = _populated_session()
    # Every read of the clock jumps ten minutes, so the deadline has already
    # passed by the time the phase boundary is reached.
    clock = ScriptedClock(datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc),
                          step_seconds=600)

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets(), clock=clock,
                                 soft_deadline_seconds=600)

    assert run.anchor_written is True, "the anchor is the work that matters"
    assert run.outcome == job_mod.OUTCOME_PARTIAL
    assert run.verification_state is None
    assert run.phases_completed == (job_mod.PHASE_ANCHOR,)
    assert run.retry_safe is True
    assert job_mod.PHASE_VERIFY not in run.checkpoint()["phases_completed"]


def test_a_soft_deadline_past_the_platform_ceiling_is_refused():
    """The same refusal `integration.jobs.run_job` makes: a deadline past the
    kill point is not a deadline."""
    session = _populated_session()

    run = job_mod.run_anchor_job(FakeDatabase(session), credential=TEST_TOKEN,
                                 secrets=_secrets(),
                                 soft_deadline_seconds=job_mod.FUNCTION_CEILING_SECONDS + 1)

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    assert session.anchors == []


def test_the_handler_holds_the_platform_contract_it_claims_to():
    """80% of the ceiling, imported and not restated.

    Two definitions of the ceiling in one codebase is one definition that goes
    stale silently.
    """
    from app.backend.integration import jobs as integration_jobs

    assert job_mod.SOFT_DEADLINE_SECONDS is integration_jobs.SOFT_DEADLINE_SECONDS
    assert job_mod.FUNCTION_CEILING_SECONDS is integration_jobs.FUNCTION_CEILING_SECONDS
    assert job_mod.SOFT_DEADLINE_SECONDS < job_mod.FUNCTION_CEILING_SECONDS


# ======================================================================
# Outcomes: success, tampering, partial, retry-safe and permanent are five
# ======================================================================
def test_tampering_found_by_the_job_is_not_reported_as_a_failed_job():
    """The job did its work. The DATABASE is the problem.

    Reporting a tamper finding as a job failure sends an operator to the
    scheduler; reporting it as success loses it entirely. It is its own
    outcome, with its own exit code, because the response is an
    investigation and not a re-run.
    """
    session = _populated_session()
    database = FakeDatabase(session)
    job_mod.run_anchor_job(database, credential=TEST_TOKEN, secrets=_secrets())

    del session.entries["PROJECT:PRJ-2"]
    # Same day: the anchor is already there, so only verification runs.
    run = job_mod.run_anchor_job(database, credential=TEST_TOKEN,
                                 secrets=_secrets())

    assert run.outcome == job_mod.OUTCOME_TAMPER_DETECTED
    assert run.verification_state == audit_mod.ANCHOR_STATE_MISSING_STREAM
    assert run.verification["missing_streams"] == ["PROJECT:PRJ-2"]
    assert run.exit_code == 4
    assert run.retry_safe is True, (
        "the anchor it wrote stands; invoking again re-reports the finding "
        "rather than making it worse")


def test_the_five_outcomes_have_distinct_exit_codes():
    """A scheduler that can only see 'non-zero' cannot tell a database that
    needs investigating from one that needs a retry."""
    assert len(set(job_mod.EXIT_CODES.values())) == len(job_mod.EXIT_CODES)
    assert job_mod.EXIT_CODES[job_mod.OUTCOME_SUCCEEDED] == 0
    assert all(code != 0 for outcome, code in job_mod.EXIT_CODES.items()
               if outcome != job_mod.OUTCOME_SUCCEEDED)
    assert set(job_mod.EXIT_CODES) == {
        job_mod.OUTCOME_SUCCEEDED, job_mod.OUTCOME_PARTIAL,
        job_mod.OUTCOME_TAMPER_DETECTED, job_mod.OUTCOME_FAILED_RETRYABLE,
        job_mod.OUTCOME_FAILED_PERMANENT}


def test_retry_safety_is_stated_per_outcome_and_only_permanent_is_not():
    for outcome in job_mod.EXIT_CODES:
        run = job_mod.AnchorJobRun(outcome=outcome)
        assert run.retry_safe is (outcome != job_mod.OUTCOME_FAILED_PERMANENT)
        assert run.succeeded is (outcome == job_mod.OUTCOME_SUCCEEDED)


def test_a_transient_database_fault_is_retryable_and_a_permanent_one_is_not():
    """Classified from TYPE and SQLSTATE, never from the message.

    An unrecognised fault is retryable on purpose: wrongly refusing to retry
    costs a day of evidence, and wrongly retrying costs one more invocation of
    an idempotent job.
    """
    class FakeDbError(Exception):
        def __init__(self, sqlstate):
            super().__init__("SELECT ... FROM audit_log WHERE detail = 'secret'")
            self.sqlstate = sqlstate

    assert job_mod.classify_fault(FakeDbError("40001")) == job_mod.OUTCOME_FAILED_RETRYABLE
    assert job_mod.classify_fault(FakeDbError("40P01")) == job_mod.OUTCOME_FAILED_RETRYABLE
    assert job_mod.classify_fault(FakeDbError("08006")) == job_mod.OUTCOME_FAILED_RETRYABLE
    assert job_mod.classify_fault(FakeDbError("53300")) == job_mod.OUTCOME_FAILED_RETRYABLE
    assert job_mod.classify_fault(FakeDbError("42501")) == job_mod.OUTCOME_FAILED_PERMANENT
    assert job_mod.classify_fault(FakeDbError("23514")) == job_mod.OUTCOME_FAILED_PERMANENT
    assert job_mod.classify_fault(RuntimeError("who knows")) == job_mod.OUTCOME_FAILED_RETRYABLE
    assert job_mod.classify_fault(job_mod.AnchorJobDenied("no")) == \
        job_mod.OUTCOME_FAILED_PERMANENT


# ======================================================================
# What a run is allowed to say out loud
# ======================================================================
def test_a_failure_never_carries_the_exception_text_or_the_sql():
    """`str()` on a psycopg error echoes the failing statement, and the
    failing statement here is a query over the audit log."""
    class Exploding:
        @contextlib.contextmanager
        def session(self, scope):
            raise RuntimeError(
                "SELECT detail FROM audit_log -- password=hunter2")
            yield  # pragma: no cover

    run = job_mod.run_anchor_job(Exploding(), credential=TEST_TOKEN,
                                 secrets=_secrets())

    body = json.dumps(run.as_dict())
    assert "hunter2" not in body
    assert "audit_log" not in body
    assert "SELECT" not in body
    assert run.error_class == "RuntimeError", (
        "the class name is the diagnosis an operator gets, and it is enough")


def test_a_result_carries_no_audit_payload_and_no_full_hash():
    """A run result is logged and written to a file. Neither is the audit log."""
    session = FakeSession(entries={
        "PROJECT:PRJ-9": [(1, "a" * 64), (2, "b" * 64)]})
    database = FakeDatabase(session)
    job_mod.run_anchor_job(database, credential=TEST_TOKEN, secrets=_secrets())

    session.entries["PROJECT:PRJ-9"] = [(1, "a" * 64)]
    run = job_mod.run_anchor_job(database, credential=TEST_TOKEN,
                                 secrets=_secrets())

    body = json.dumps(run.as_dict())
    assert run.outcome == job_mod.OUTCOME_TAMPER_DETECTED
    assert "b" * 64 not in body, "a full entry hash reached the result"
    assert "PROJECT:PRJ-9" in body, (
        "the stream key must stay: it is what an operator investigates, and "
        "any holder of audit.read already sees it on /api/audit/streams")


# ======================================================================
# LIVE PostgreSQL. NEVER EXECUTED ANYWHERE -- these first run in CI.
# ======================================================================
def _live_run(pg_database, **kwargs):
    return job_mod.run_anchor_job(pg_database, credential=TEST_TOKEN,
                                  secrets=_secrets(), **kwargs)


@PG
@LIVE
def test_the_job_writes_a_real_anchor_row_live(pg_database, pg_scope, pg_connection):
    """End to end, through the entry point, against the real table.

    `audit_anchor` has four columns with no default -- `anchor_date`,
    `stream_heads` and `anchor_hash` are NOT NULL, `prev_anchor_hash` is
    nullable -- and migration 024 adds CHECKs on two of them. Nothing is
    inserted by hand here: the row is the product's own writer's output,
    reached through the job, which is the only way to prove the writer is
    reachable at all.
    """
    from app.backend.pg.audit import append

    stream_key = f"AnchorJob:{uuid.uuid4().hex}"
    with pg_database.session(pg_scope) as session:
        append(session, "tester", "CREATED", "AnchorJob", "1", "first",
               stream_key=stream_key)

    run = _live_run(pg_database)

    assert run.outcome in (job_mod.OUTCOME_SUCCEEDED,
                           job_mod.OUTCOME_TAMPER_DETECTED)
    assert run.anchor_written is True
    assert run.anchor_date == _today().isoformat()

    row = pg_connection.execute(
        "SELECT anchor_date, stream_heads, anchor_hash FROM audit_anchor "
        "WHERE anchor_date = %s", (_today(),)).fetchone()
    pg_connection.rollback()
    assert row is not None, (
        "the job reported an anchor and the table is empty -- which is the "
        "state this entire stream exists to end")
    assert stream_key in row[1]
    assert row[2] != ""


@PG
@LIVE
def test_a_duplicate_invocation_on_one_day_writes_one_row_live(pg_database):
    """The primary key would raise; the job must not let it, and must not
    claim to have anchored either."""
    first = _live_run(pg_database)
    second = _live_run(pg_database)

    assert first.anchor_written is True
    assert second.anchor_written is False
    assert second.outcome in (job_mod.OUTCOME_SUCCEEDED,
                              job_mod.OUTCOME_TAMPER_DETECTED)
    assert second.anchor_hash == first.anchor_hash


@PG
@LIVE
def test_concurrent_invocations_produce_exactly_one_anchor_live(
        pg_url, pg_disposable_db_name, pg_connection):
    """Two Function ticks that overlap must not race the primary key.

    `write_anchor` takes the advisory lock BEFORE reading, so the second
    invocation blocks, then sees the row and reports `written=False`. Without
    the lock both would read "no anchor yet" and one would raise 23505 --
    inside a Function, at whatever hour the schedule fires.
    """
    from conftest_pg import _config_and_provider
    from app.backend.pg.engine import Database

    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def invoke():
        cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
        database = Database(cfg, secret_provider=provider, min_size=1, max_size=2)
        try:
            barrier.wait(timeout=30)
            results.append(_live_run(database))
        except Exception as exc:                     # noqa: BLE001
            errors.append(type(exc).__name__)
        finally:
            database.close()

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"a concurrent invocation raised: {errors}"
    assert len(results) == 2
    assert sum(1 for run in results if run.anchor_written) == 1, (
        "both invocations claimed to have written the day's anchor")
    assert all(run.outcome != job_mod.OUTCOME_FAILED_PERMANENT for run in results)

    count = pg_connection.execute(
        "SELECT count(*) FROM audit_anchor WHERE anchor_date = %s",
        (_today(),)).fetchone()[0]
    pg_connection.rollback()
    assert count == 1


@PG
@LIVE
def test_a_whole_stream_deleted_is_caught_through_the_job_live(
        pg_database, pg_scope, pg_connection):
    """The attack, found by the thing that is actually scheduled.

    The triggers and the REVOKE stop the APPLICATION; they do not stop someone
    holding the database, which is the threat anchors exist for. The test takes
    that position deliberately as the table owner, and restores the trigger so
    nothing else in the disposable database runs unprotected.
    """
    from app.backend.pg.audit import append

    stream_key = f"AnchorJobDelete:{uuid.uuid4().hex}"
    with pg_database.session(pg_scope) as session:
        append(session, "tester", "CREATED", "AnchorJobDelete", "1", "only entry",
               stream_key=stream_key)

    anchored = _live_run(pg_database)
    assert anchored.anchor_written is True

    pg_connection.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_delete")
    try:
        pg_connection.execute("DELETE FROM audit_log WHERE stream_key = %s",
                              (stream_key,))
        pg_connection.commit()
    finally:
        pg_connection.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_delete")
        pg_connection.commit()

    found = _live_run(pg_database)

    assert found.outcome == job_mod.OUTCOME_TAMPER_DETECTED, (
        "an entire audit stream was deleted and the scheduled job reported "
        f"{found.outcome}")
    assert found.verification_state == audit_mod.ANCHOR_STATE_MISSING_STREAM
    assert stream_key in found.verification["missing_streams"]
    assert found.exit_code == 4


@PG
@LIVE
def test_a_partial_run_commits_its_anchor_live(pg_database, pg_connection):
    """A Function killed after phase one keeps phase one.

    The soft deadline is forced past before the phase boundary, so verification
    never starts -- and the row must still be there, because the anchor is the
    work that matters and it committed in its own transaction.
    """
    clock = ScriptedClock(datetime.now(timezone.utc), step_seconds=600)

    run = _live_run(pg_database, clock=clock, soft_deadline_seconds=600)

    assert run.outcome == job_mod.OUTCOME_PARTIAL
    assert run.anchor_written is True
    assert run.verification_state is None

    count = pg_connection.execute(
        "SELECT count(*) FROM audit_anchor WHERE anchor_date = %s",
        (run.anchor_date,)).fetchone()[0]
    pg_connection.rollback()
    assert count == 1, (
        "the anchor phase committed in its own transaction; a run that "
        "stopped afterwards must not have taken it with it")

    resumed = _live_run(pg_database, checkpoint=run.checkpoint())
    assert resumed.anchor_written is False
    assert resumed.verification_state is not None


@PG
@LIVE
def test_a_retryable_failure_leaves_the_day_anchorable_live(pg_database):
    """A transient fault must not consume the day.

    There is no state anywhere saying "today was attempted", precisely so a
    failed attempt cannot stop a later one -- a day the writer missed is a day
    with no evidence, and a lock-out flag would manufacture those.
    """
    class FlakyDatabase:
        def __init__(self, real):
            self.real = real
            self.calls = 0

        @contextlib.contextmanager
        def session(self, scope):
            self.calls += 1
            if self.calls == 1:
                exc = RuntimeError("transient")
                exc.sqlstate = "40001"
                raise exc
            with self.real.session(scope) as session:
                yield session

    flaky = FlakyDatabase(pg_database)
    failed = job_mod.run_anchor_job(flaky, credential=TEST_TOKEN,
                                    secrets=_secrets())
    assert failed.outcome == job_mod.OUTCOME_FAILED_RETRYABLE
    assert failed.retry_safe is True

    recovered = _live_run(pg_database)
    assert recovered.anchor_written is True, (
        "a transient failure consumed the day's only chance to anchor")


@PG
@LIVE
def test_an_unauthenticated_invocation_writes_no_row_live(pg_database, pg_connection):
    """The guard is on the entry point, not on a caller's good manners."""
    run = job_mod.run_anchor_job(pg_database, credential=None,
                                 secrets=_secrets())

    assert run.outcome == job_mod.OUTCOME_FAILED_PERMANENT
    count = pg_connection.execute("SELECT count(*) FROM audit_anchor").fetchone()[0]
    pg_connection.rollback()
    assert count == 0
