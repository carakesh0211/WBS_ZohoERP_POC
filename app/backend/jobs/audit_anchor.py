"""The invocation path for the audit anchor. Without this, the control is INERT.

WHAT WAS WRONG
==============
``pg/audit.write_anchor`` was correct, tested, and **called by nothing**. Not a
route -- deliberately, and ``api/audit.py`` says why: "Writing one is a
scheduled job's work." Not a CLI entry point. Not a schedule. Its only
references outside its own module were ``tests/`` and a comment in migration
024. So in every deployment ``audit_anchor`` stayed empty, ``verify_anchors``
returned ``anchored=False, intact=False`` for ever, and whole-stream truncation
-- the one failure a hash chain provably cannot see -- was undetectable.

The control was not broken. It was never switched on. This module is the
switch, and the point of it is that it is INDEPENDENTLY CALLABLE: a Catalyst
Cron Function, ``tools/anchor_job.py``, and a test all reach the writer through
:func:`run_anchor_job` and through nothing else.

WHY THIS IS NOT AN ``integration.jobs`` JOB
==========================================
``integration/jobs.py`` is the right framework for work that queues: it claims
a ``job`` row with ``FOR UPDATE SKIP LOCKED``, persists a cursor in
``job.checkpoint``, and reschedules. The audit anchor does not queue, and
running it through that framework would cost more than it bought:

* ``job.principal_user_id`` is ``NOT NULL REFERENCES app_user`` and
  ``created_by``/``updated_by`` are ``NOT NULL``, so a queued anchor job needs
  a seeded service identity row -- schema and seed this stream does not own.
* The framework's durable checkpoint would become a **second** record of which
  day was anchored, weaker than the first. ``audit_anchor`` is append-only by
  trigger with UPDATE and DELETE revoked from ``capex_app``; ``job.checkpoint``
  is an ordinary mutable jsonb column. Two answers to "was yesterday anchored",
  and the softer one is the one an attacker edits.

The anchor's checkpoint IS ``audit_anchor``. ``anchor_date`` is the primary
key, the table cannot be overwritten, and ``write_anchor`` returns
``written=False`` when the day is already there. That is a stronger idempotency
key than any cursor this module could keep, so this module keeps none.

THE CONTRACT IT DOES HOLD
=========================
Everything the platform imposes still applies, and is imported from
``integration.jobs`` rather than restated, so there is one definition of the
ceiling in the codebase:

1. **Bounded.** Two phases, run in a fixed order, each committed in its own
   transaction. No unbounded loop, no in-process scheduler, nothing resident.
2. **A soft deadline at 80% of the ceiling** (:data:`SOFT_DEADLINE_SECONDS`,
   720s of 900s), checked BETWEEN phases and never inside one -- abandoning a
   phase half-written is exactly the non-idempotency the contract forbids.
3. **Explicit checkpoints.** Each phase reports itself done; a run that stops
   at the deadline returns the phases it completed, and a later invocation
   given that checkpoint skips them. Both phases are individually idempotent,
   so a lost checkpoint costs work, never correctness.
4. **Idempotent.** Anchoring is idempotent by primary key; verifying is
   read-only. Invoking this handler twice in one day writes one anchor.

WHAT IT REFUSES TO DO
=====================
**It will not anchor a day that is not today.** An anchor records the stream
heads *at the moment it is written*; writing one dated last Tuesday would
record today's heads under last Tuesday's date and claim they were observed
then. Migration 024's ``COMMENT ON TABLE`` states the consequence of a missed
run plainly -- "a day on which the writer did not run is a day with no
evidence" -- and the only honest response to a gap is to report it, which
:func:`run_anchor_job` does (``unanchored_days``). Manufacturing the missing
evidence is refused, permanently, and is not made available behind a flag.

SECRETS AND OUTPUT
==================
Nothing here reads a credential value from source, a default, or an argument
default. The job token is fetched from a :class:`SecretProvider` by NAME
(:data:`ANCHOR_JOB_TOKEN_SECRET`) and compared with ``hmac.compare_digest``. It
is never logged, never returned, and never placed in a result.

No result or log line from this module carries an audit payload, a ``detail``
column, a full entry hash, or exception text. Hashes are truncated to a prefix
(:data:`_HASH_PREFIX`) -- enough to correlate two findings, not enough to
reconstruct anything -- and every exception handler emits a STATIC message with
the exception's class name and nothing else. ``str(exc)`` on a psycopg error
echoes the failing SQL, and the failing SQL here is a query over the audit log.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from ..integration.jobs import (
    FUNCTION_CEILING_SECONDS, SOFT_DEADLINE_SECONDS, Clock, SystemClock,
)
from ..observability import alert, correlation, log
from ..pg import audit as audit_mod
from ..pg.config import SecretProvider
from ..pg.engine import Database, Scope

# ---------------------------------------------------------------- identity
#: The service principal this job runs as. A real identity on the session, not
#: an implicit "no user": every statement it issues is attributable.
ANCHOR_JOB_PRINCIPAL = "SVC-ANCHOR"

#: What holding the job token authorises, and the ONLY thing it authorises.
#:
#: Deliberately NOT added to ``auth.PERMISSIONS``. That map is role ->
#: permission for HTTP principals, and deciding which human ROLE may cause an
#: anchor to exist is the D-12 role-mapping decision `api/audit.py` already
#: refuses to make in passing -- ``test_aud_c_006_auditor_is_read_only`` pins
#: the Auditor to an allow-list of four, and widening an audit-finding
#: assertion so a new feature reads tidily is not a call to make while adding a
#: scheduler. This is capability authorisation for a machine principal: the
#: token IS the grant, it names one operation, and holding it confers nothing
#: else in the system. When the role mapping is signed off, a human-facing
#: permission can be added on top without changing anything here.
ANCHOR_JOB_CAPABILITY = "audit.anchor.write"

#: The NAME of the secret holding the job token. Never a value, and never a
#: default value: a fallback token in source is a credential in source.
ANCHOR_JOB_TOKEN_SECRET = "CAPEX_ANCHOR_JOB_TOKEN"

#: Below this a shared secret is guessable, and a guessable credential on the
#: one write path into the tamper-evidence table is not a credential.
MIN_TOKEN_LENGTH = 32

# ---------------------------------------------------------------- outcomes
#: The run finished: today is anchored and verification completed.
OUTCOME_SUCCEEDED = "SUCCEEDED"
#: Anchoring succeeded; verification did not finish inside the deadline. The
#: work that matters is committed and the next invocation resumes. Distinct
#: from SUCCEEDED because "verified nothing" must never read as "verified".
OUTCOME_PARTIAL = "PARTIAL"
#: Verification found a missing stream, a truncated tail, a rewritten entry, or
#: an anchor that does not verify. The job DID its work; the database is the
#: problem. Its own outcome because an operator's response is an investigation,
#: not a re-run.
OUTCOME_TAMPER_DETECTED = "TAMPER_DETECTED"
#: A transient fault. Running the job again is safe and is the fix.
OUTCOME_FAILED_RETRYABLE = "FAILED_RETRYABLE"
#: A fault re-running cannot fix: a bad or missing credential, a refused
#: backdate, a privilege the application does not hold. Retrying is noise.
OUTCOME_FAILED_PERMANENT = "FAILED_PERMANENT"

#: Process exit codes, for `tools/anchor_job.py` and for a Function wrapper
#: that reports by status rather than by reading a dict. Distinct per outcome
#: on purpose: a scheduler that can only see "non-zero" cannot tell a database
#: that needs an investigation from one that needs a retry.
EXIT_CODES: Mapping[str, int] = {
    OUTCOME_SUCCEEDED: 0,
    OUTCOME_PARTIAL: 3,
    OUTCOME_TAMPER_DETECTED: 4,
    OUTCOME_FAILED_RETRYABLE: 5,
    OUTCOME_FAILED_PERMANENT: 6,
}

# ------------------------------------------------------------------ phases
PHASE_ANCHOR = "ANCHOR"
PHASE_VERIFY = "VERIFY"

#: The fixed order. Anchoring first, always: verification is the reporting
#: half, and a run that ran out of time must have spent it on the half that
#: writes the evidence. Reversing this would mean a busy day produces a
#: verification and no anchor -- a report about a record that was not made.
PHASES: tuple[str, ...] = (PHASE_ANCHOR, PHASE_VERIFY)

#: Seconds of the soft deadline reserved for the verification phase. Below
#: this, verification is not started at all and the run returns PARTIAL rather
#: than being killed in the middle of a scan it cannot finish.
VERIFY_RESERVE_SECONDS = 60

#: Entry and anchor hashes are truncated to this many characters in results and
#: logs. Enough to correlate the same finding across two runs; not enough to be
#: a copy of the evidence.
_HASH_PREFIX = 12


class AnchorJobError(RuntimeError):
    """A fault this job classifies for itself. Carries no external text."""

    outcome = OUTCOME_FAILED_PERMANENT


class AnchorJobDenied(AnchorJobError):
    """Authentication or authorisation refused. Never says which, or why."""


class AnchorJobNotConfigured(AnchorJobError):
    """No usable job token is configured for this deployment."""


class AnchorJobRefused(AnchorJobError):
    """The job was asked to do something it will not do -- see `run_anchor_job`."""


# ====================================================================== auth
def authorise(presented_token: str | None, *,
              secrets: SecretProvider) -> str:
    """Authenticate the caller and authorise it for one capability.

    Returns the service principal id on success; raises otherwise. There is no
    "anonymous" path and no environment in which the check is skipped: a job
    that writes into the tamper-evidence table with no credential is a job
    anyone who can reach the entry point can run.

    Every refusal raises the same class with the same static message. Telling a
    caller *which* of "no token configured", "wrong length" and "does not
    match" it hit is telling an attacker how far in they are.
    """
    configured = secrets.get(ANCHOR_JOB_TOKEN_SECRET)
    if not configured or not configured.strip():
        raise AnchorJobNotConfigured(
            f"No anchor job token is configured. Provide the secret named "
            f"{ANCHOR_JOB_TOKEN_SECRET}; this build reads it by name and has "
            f"no default.")
    if len(configured.strip()) < MIN_TOKEN_LENGTH:
        raise AnchorJobNotConfigured(
            f"The configured anchor job token is shorter than "
            f"{MIN_TOKEN_LENGTH} characters. A guessable secret on the only "
            f"write path into the audit anchor is not a credential.")
    if not presented_token:
        raise AnchorJobDenied("Anchor job invocation was not authorised.")
    # compare_digest, not ==: `==` on strings returns as soon as two bytes
    # differ, and the time it took says how much of the token was right.
    if not hmac.compare_digest(presented_token.strip(), configured.strip()):
        raise AnchorJobDenied("Anchor job invocation was not authorised.")
    return ANCHOR_JOB_PRINCIPAL


def anchor_job_scope() -> Scope:
    """The session scope the job runs under, and why it is unrestricted.

    ``audit_log`` and ``audit_anchor`` carry no scope dimension at all --
    ``pg/scope_inventory.py`` classifies both, with the reason -- so there is
    no row this scope reaches that a narrower one would legitimately hide.

    It matters that it is unrestricted rather than merely permitted to be:
    ``stream_heads`` is the snapshot the anchor is computed from, and a session
    that could see only some streams would write an anchor that silently omits
    the rest. Every stream it omitted would then be deletable with no finding
    -- a degenerate anchor that reports a pass, which is worse than no anchor,
    because no anchor at least reports ``NEVER_ANCHORED``.
    """
    return Scope(user_id=ANCHOR_JOB_PRINCIPAL, principal_kind="SERVICE",
                 read_all=True)


# =================================================================== results
def _short(value: Any) -> Any:
    """A hash truncated for reporting, or the value unchanged if it is not one."""
    if isinstance(value, str) and len(value) > _HASH_PREFIX:
        return value[:_HASH_PREFIX] + "..."
    return value


def _redact_finding(finding: Any) -> Any:
    """One verification finding, safe to log.

    Stream keys stay: they are what an operator investigates, and they are
    already visible to any holder of `audit.read` through `/api/audit/streams`.
    Hashes are truncated. Nothing else from the audit row -- actor, action,
    detail -- is in a finding to begin with, and nothing is added here.
    """
    if isinstance(finding, str):
        return finding
    if isinstance(finding, Mapping):
        return {key: (_short(value) if "hash" in key else value)
                for key, value in finding.items()}
    return finding


@dataclass(frozen=True)
class AnchorJobRun:
    """The outcome of one invocation. Everything an operator needs, no logs."""

    outcome: str
    anchor_date: str | None = None
    #: True only when THIS invocation wrote the row. False means the day was
    #: already anchored, which is a success and is not the same event.
    anchor_written: bool = False
    streams_anchored: int = 0
    anchor_hash: str | None = None
    #: The five-state verdict from `pg.audit.verify_anchors`, or None when
    #: verification did not run.
    verification_state: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    #: Days between the previous anchor and this one that carry no anchor and
    #: never will. Reported, never filled in.
    unanchored_days: int = 0
    phases_completed: tuple[str, ...] = ()
    correlation_id: str | None = None
    #: Class name only. Never a message, never SQL, never a payload.
    error_class: str | None = None
    reason: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]

    @property
    def succeeded(self) -> bool:
        """SUCCEEDED alone. PARTIAL means resume; TAMPER means investigate."""
        return self.outcome == OUTCOME_SUCCEEDED

    @property
    def retry_safe(self) -> bool:
        """May a scheduler simply invoke this again?

        True for everything except a permanent failure, because every phase is
        idempotent. A PARTIAL run resumes, a TAMPER run re-reports (and the
        anchor it wrote stays), a retryable failure retries.
        """
        return self.outcome != OUTCOME_FAILED_PERMANENT

    def checkpoint(self) -> dict[str, Any]:
        """What a later invocation would be given to skip completed phases.

        JSON-only, because a Catalyst Function receives its arguments as JSON
        and a checkpoint that cannot survive that is not a checkpoint.
        """
        return {"anchor_date": self.anchor_date,
                "phases_completed": list(self.phases_completed)}

    def as_dict(self) -> dict[str, Any]:
        """The result, safe to log or return. No credential, no payload."""
        return {
            "job": "audit_anchor",
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "retry_safe": self.retry_safe,
            "anchor_date": self.anchor_date,
            "anchor_written": self.anchor_written,
            "streams_anchored": self.streams_anchored,
            "anchor_hash": _short(self.anchor_hash),
            "verification_state": self.verification_state,
            "verification": self.verification,
            "unanchored_days": self.unanchored_days,
            "phases_completed": list(self.phases_completed),
            "checkpoint": self.checkpoint(),
            "correlation_id": self.correlation_id,
            "error_class": self.error_class,
            "reason": self.reason,
        }


# ============================================================ fault classing
#: SQLSTATE classes a retry can clear. `40001` serialization failure, `40P01`
#: deadlock, `55P03` lock not available, `57014` query cancelled, class `08`
#: connection exceptions, class `53` insufficient resources. Every one of them
#: describes a moment, not a state.
_RETRYABLE_SQLSTATES: frozenset[str] = frozenset({
    "40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03",
})
_RETRYABLE_SQLSTATE_CLASSES: frozenset[str] = frozenset({"08", "53"})


def classify_fault(exc: BaseException) -> str:
    """Which failure outcome an exception is, from its TYPE and SQLSTATE only.

    Never from its message. A psycopg error's ``str()`` carries the failing
    statement, and the failing statement here is a query over the audit log.

    An unrecognised fault is classified **retryable**, and that direction is
    deliberate: migration 024 says a day with no writer run is a day with no
    evidence, so wrongly refusing to retry costs a day of evidence, while
    wrongly retrying costs one more invocation of an idempotent job.
    """
    if isinstance(exc, AnchorJobError):
        return exc.outcome
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if isinstance(sqlstate, str) and sqlstate:
        if sqlstate in _RETRYABLE_SQLSTATES:
            return OUTCOME_FAILED_RETRYABLE
        if sqlstate[:2] in _RETRYABLE_SQLSTATE_CLASSES:
            return OUTCOME_FAILED_RETRYABLE
        # 23xxx integrity, 42xxx syntax/undefined object, 42501 insufficient
        # privilege: all state, not weather. A retry reproduces them exactly.
        return OUTCOME_FAILED_PERMANENT
    return OUTCOME_FAILED_RETRYABLE


# ================================================================ the handler
def run_anchor_job(
    database: Database,
    *,
    credential: str | None,
    secrets: SecretProvider,
    clock: Clock | None = None,
    anchor_date: date | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    soft_deadline_seconds: int = SOFT_DEADLINE_SECONDS,
    correlation_id: str | None = None,
    verify: bool = True,
) -> AnchorJobRun:
    """Write today's audit anchor and verify the anchor set. One invocation.

    THIS IS THE ONLY WAY ``write_anchor`` IS REACHED IN A DEPLOYMENT. A
    Catalyst Cron Function calls it, ``tools/anchor_job.py`` calls it, and
    ``tests/test_pg_anchor_job.py`` proves the writer runs through it rather
    than being poked directly.

    Returns, never raises. A cron Function's only failure channel is a stack
    trace in a log nobody is watching, so every fault becomes an outcome with
    an exit code and a class name -- and never a message, because the message
    would carry the SQL.

    ``anchor_date``, if given, must equal the clock's UTC date. Anchoring a
    past day would record TODAY's stream heads under that day's date and claim
    they were observed then; the gap is reported as ``unanchored_days`` and
    left as a gap. ``checkpoint`` is a previous run's :meth:`AnchorJobRun.
    checkpoint`, and phases it names as done for the same date are skipped.
    """
    clock = clock or SystemClock()
    started_at = clock.now()
    soft_deadline_at = started_at + timedelta(seconds=soft_deadline_seconds)
    cid = correlation_id or f"anchor-{started_at.date().isoformat()}"

    if soft_deadline_seconds > FUNCTION_CEILING_SECONDS:
        # The same refusal `integration.jobs.run_job` makes, for the same
        # reason: a deadline past the kill point is not a deadline.
        return AnchorJobRun(
            outcome=OUTCOME_FAILED_PERMANENT, correlation_id=cid,
            error_class="AnchorJobRefused",
            reason=(f"soft deadline {soft_deadline_seconds}s exceeds the "
                    f"{FUNCTION_CEILING_SECONDS}s Function ceiling"))

    today = started_at.astimezone(timezone.utc).date()
    with correlation(cid):
        try:
            principal = authorise(credential, secrets=secrets)
            if anchor_date is not None and anchor_date != today:
                raise AnchorJobRefused(
                    "This job anchors today and nothing else. An anchor "
                    "records the stream heads at the moment it is written, so "
                    "dating one in the past would record today's heads and "
                    "claim they were observed then. A day the writer missed "
                    "is a day with no evidence and is reported as one.")
            target = today

            done = _phases_done(checkpoint, target)
            run = _execute(database, target=target, principal=principal,
                           clock=clock, soft_deadline_at=soft_deadline_at,
                           already_done=done, cid=cid, verify=verify)
            # Reported INSIDE the correlation context, so the alert a tamper
            # finding raises carries the same correlation id as the run that
            # found it. An alert an operator cannot tie back to a run is an
            # alert they cannot investigate.
            _report(run)
            return run
        except Exception as exc:  # noqa: BLE001 -- classified, never echoed
            outcome = classify_fault(exc)
            # STATIC message. `str(exc)` on a psycopg error echoes the failing
            # statement, and the failing statement is a query over the audit
            # log. The class name is the diagnosis an operator gets.
            log.error("audit anchor job failed",
                      extra={"job": "audit_anchor", "outcome": outcome,
                             "error_class": type(exc).__name__})
            return AnchorJobRun(outcome=outcome, correlation_id=cid,
                                error_class=type(exc).__name__,
                                reason=_static_reason(exc))


def _static_reason(exc: BaseException) -> str:
    """A reason line an operator can act on, from this module's own vocabulary.

    Only :class:`AnchorJobError` text is used, because only that text was
    written here. Anything else is reported by class name alone.
    """
    if isinstance(exc, AnchorJobError):
        return str(exc)
    return "The anchor job failed. See the runbook: docs/runbooks/audit-anchor.md"


def _phases_done(checkpoint: Mapping[str, Any] | None,
                 target: date) -> frozenset[str]:
    """Phases a previous invocation completed FOR THIS DATE.

    A checkpoint from another day is discarded rather than trusted: it would
    tell today's run that today is already anchored, which is precisely the
    lie that leaves a day with no evidence. Both phases are idempotent, so
    discarding one costs a repeat, never a wrong answer.
    """
    if not checkpoint:
        return frozenset()
    if checkpoint.get("anchor_date") != target.isoformat():
        return frozenset()
    completed = checkpoint.get("phases_completed") or []
    if not isinstance(completed, (list, tuple)):
        return frozenset()
    return frozenset(str(phase) for phase in completed) & frozenset(PHASES)


def _execute(database: Database, *, target: date, principal: str,
             clock: Clock, soft_deadline_at: datetime,
             already_done: frozenset[str], cid: str,
             verify: bool) -> AnchorJobRun:
    """The two phases, in order, each in its own transaction."""
    scope = anchor_job_scope()
    completed: list[str] = []
    anchor_written = False
    streams_anchored = 0
    anchor_hash: str | None = None
    unanchored_days = 0

    # ---------------------------------------------------- phase 1: ANCHOR
    if PHASE_ANCHOR in already_done:
        completed.append(PHASE_ANCHOR)
    else:
        with database.session(scope) as session:
            previous = session.fetchone(
                "SELECT anchor_date FROM audit_anchor WHERE anchor_date < %s "
                "ORDER BY anchor_date DESC LIMIT 1", (target,))
            # THE WRITER IS CALLED HERE, AND NOWHERE ELSE IN THE PRODUCT.
            written = audit_mod.write_anchor(session, anchor_date=target)
        anchor_written = bool(written["written"])
        streams_anchored = int(written["streams_anchored"])
        anchor_hash = written["anchor_hash"]
        if previous is not None:
            # Days strictly between the previous anchor and this one. Never
            # filled in: an anchor dated then would carry heads observed now.
            unanchored_days = max((target - previous[0]).days - 1, 0)
        completed.append(PHASE_ANCHOR)
        log.info("audit anchor written" if anchor_written
                 else "audit anchor already present for today",
                 extra={"job": "audit_anchor", "actor": principal,
                        "anchor_date": target.isoformat(),
                        "streams_anchored": streams_anchored,
                        "unanchored_days": unanchored_days})

    # -------------------------------------------- the deadline, checked HERE
    # Between phases, never inside one. A phase that has begun runs to its own
    # commit point; abandoning it half-written is the non-idempotency the
    # whole contract exists to forbid.
    remaining = (soft_deadline_at - clock.now()).total_seconds()
    if not verify:
        reason = "verification not requested"
    elif PHASE_VERIFY in already_done:
        reason = "verification already completed for this date"
    elif remaining <= VERIFY_RESERVE_SECONDS:
        reason = "soft deadline reached before verification could start"
    else:
        reason = None

    if reason is not None:
        outcome = (OUTCOME_SUCCEEDED
                   if PHASE_VERIFY in already_done or not verify
                   else OUTCOME_PARTIAL)
        if PHASE_VERIFY in already_done:
            completed.append(PHASE_VERIFY)
        return AnchorJobRun(
            outcome=outcome, anchor_date=target.isoformat(),
            anchor_written=anchor_written, streams_anchored=streams_anchored,
            anchor_hash=anchor_hash, unanchored_days=unanchored_days,
            phases_completed=tuple(completed), correlation_id=cid,
            reason=reason)

    # ---------------------------------------------------- phase 2: VERIFY
    with database.session(scope) as session:
        result = audit_mod.verify_anchors(session, as_of=target)
    completed.append(PHASE_VERIFY)

    verification = {
        "state": result["state"],
        "anchored": result["anchored"],
        "intact": result["intact"],
        "anchors_checked": result["anchors_checked"],
        "anchor_chain_intact": result["anchor_chain_intact"],
        "first_broken_anchor_date": result["first_broken_anchor_date"],
        "malformed_anchor_dates": list(result["malformed_anchor_dates"]),
        "newest_anchor_date": result["newest_anchor_date"],
        "anchor_age_days": result["anchor_age_days"],
        "anchor_stale": result["anchor_stale"],
        "streams_anchored": result["streams_anchored"],
        "streams_anchored_ever": result["streams_anchored_ever"],
        "missing_streams": [_redact_finding(f) for f in result["missing_streams"]],
        "truncated_streams": [_redact_finding(f) for f in result["truncated_streams"]],
        "diverged_streams": [_redact_finding(f) for f in result["diverged_streams"]],
        "dropped_from_newest_anchor":
            [_redact_finding(f) for f in result["dropped_from_newest_anchor"]],
    }

    outcome = (OUTCOME_SUCCEEDED if result["intact"]
               else OUTCOME_TAMPER_DETECTED)
    return AnchorJobRun(
        outcome=outcome, anchor_date=target.isoformat(),
        anchor_written=anchor_written, streams_anchored=streams_anchored,
        anchor_hash=anchor_hash, verification_state=result["state"],
        verification=verification, unanchored_days=unanchored_days,
        phases_completed=tuple(completed), correlation_id=cid)


def _report(run: AnchorJobRun) -> None:
    """Say what happened, once, at the severity the outcome deserves.

    `AUDIT_CHAIN_BROKEN` is the P1 already defined for "verification reported
    intact=False" and is exactly this finding; no new alert condition is
    invented here, because `alert()` refuses one that has no runbook behind it
    and `observability.py` is not this stream's file to edit.
    """
    if run.outcome == OUTCOME_TAMPER_DETECTED:
        alert("AUDIT_CHAIN_BROKEN",
              "audit anchor verification found the log does not match the anchors",
              job="audit_anchor", state=run.verification_state,
              anchor_date=run.anchor_date,
              missing=len(run.verification.get("missing_streams", [])),
              truncated=len(run.verification.get("truncated_streams", [])),
              diverged=len(run.verification.get("diverged_streams", [])))
        return
    if run.unanchored_days:
        # Not an alert: there is no ALERT_CONDITIONS entry for "the writer
        # stopped running", and raising one that has no runbook is refused by
        # design. Reported at WARNING with the count, and named in the report
        # to the lead as the alert condition worth adding.
        log.warning("audit anchor gap: days with no anchor and no evidence",
                    extra={"job": "audit_anchor",
                           "anchor_date": run.anchor_date,
                           "unanchored_days": run.unanchored_days})
    if run.outcome == OUTCOME_PARTIAL:
        log.warning("audit anchor job stopped before verifying",
                    extra={"job": "audit_anchor", "reason": run.reason,
                           "anchor_date": run.anchor_date})


__all__ = [
    "ANCHOR_JOB_CAPABILITY", "ANCHOR_JOB_PRINCIPAL", "ANCHOR_JOB_TOKEN_SECRET",
    "AnchorJobDenied", "AnchorJobError", "AnchorJobNotConfigured",
    "AnchorJobRefused", "AnchorJobRun", "EXIT_CODES", "MIN_TOKEN_LENGTH",
    "OUTCOME_FAILED_PERMANENT", "OUTCOME_FAILED_RETRYABLE", "OUTCOME_PARTIAL",
    "OUTCOME_SUCCEEDED", "OUTCOME_TAMPER_DETECTED", "PHASES", "PHASE_ANCHOR",
    "PHASE_VERIFY", "VERIFY_RESERVE_SECONDS", "anchor_job_scope", "authorise",
    "classify_fault", "run_anchor_job",
]
