# Runbook — the daily audit anchor

**What it protects.** The per-stream hash chain (`pg/audit.verify_chain`) proves
that the rows a stream *still has* link to each other and carry no gap. It
provably cannot see rows removed from the end of a stream, and it can say
nothing at all about a stream that no longer exists: both leave a perfectly
self-consistent database. The daily anchor is the external record that makes
those two cases answerable. `audit_anchor` is append-only by trigger
(`001_foundation.sql`) with `UPDATE`/`DELETE` revoked from `capex_app`
(`004_identity_scope.sql`), and each day's row hash-chains to the previous
day's, so editing one invalidates every anchor after it.

**The state this runbook exists to prevent.** The writer shipped correct,
tested, and called by nothing — no route, no CLI, no schedule — so the table
stayed empty in every deployment and whole-stream truncation was undetectable.
`024_audit_anchor_integrity.sql`'s `COMMENT ON TABLE` states the operating
rule plainly:

> Anchors detect only what they were written to see: a day on which the writer
> did not run is a day with no evidence.

A missed day cannot be repaired later. **The job refuses to backdate an
anchor**, because an anchor written today under last Tuesday's date would
record today's stream heads and claim they were observed then. Gaps are
reported (`unanchored_days`), never filled in.

---

## 1. The handler

| | |
|---|---|
| Handler | `app.backend.jobs.audit_anchor.run_anchor_job` |
| CLI | `python tools/anchor_job.py` |
| Cadence | once per day |
| Ceiling | 15 min (Catalyst Cron/Event Function); soft deadline 720 s = 80 % |
| Idempotent | yes — `audit_anchor.anchor_date` is the primary key |
| Service principal | `SVC-ANCHOR` |

It runs two phases, in a fixed order, each committed in its own transaction:

1. **ANCHOR** — snapshot every stream's head in one statement and write the
   day's row. Idempotent: a second invocation returns the stored row with
   `anchor_written: false`.
2. **VERIFY** — check the anchor chain and the log against **every** anchor,
   not only the newest.

The soft deadline is checked *between* phases and never inside one. A run that
stops after phase 1 returns `PARTIAL` with a checkpoint; the next invocation
skips the completed phase. **The job keeps no cursor of its own** — the anchor
table is the checkpoint, and a second, mutable record of which day was anchored
would be the softer of two answers to the same question.

## 2. Scheduling it on Catalyst

The scheduler is **not created by this stream**, and nothing here deploys.
What follows is the exact configuration to apply.

### Cron Function

Catalyst Job Scheduling → **Cron**, invoking a **Cron Function** (15-minute
ceiling — never an AppSail HTTP route, whose ceiling is 30 s).

| Field | Value |
|---|---|
| Cron name | `capex-audit-anchor-daily` |
| Type | Cron Function |
| Repetition | Daily |
| Time (UTC) | `02:15` — after midnight so the day is complete, early enough that a `PARTIAL` run has hours of retries left |
| Target function | `capexAuditAnchor` (Cron Function) |
| Timeout | 900 s |

The function body is a thin shim; the whole handler is already written:

```bash
python tools/anchor_job.py --quiet
```

or, in-process:

```python
from app.backend.jobs.audit_anchor import ANCHOR_JOB_TOKEN_SECRET, run_anchor_job

run = run_anchor_job(
    database,
    credential=secrets.get(ANCHOR_JOB_TOKEN_SECRET),
    secrets=secrets,
    checkpoint=payload.get("checkpoint"),   # from a previous PARTIAL run
)
return run.as_dict()          # already redacted; safe to log
```

Retry policy: **on exit code 3 or 5 only.** Code 4 is a tamper finding — the
job worked and the database is the problem; retrying it just re-reports.
Code 6 is a configuration fault; retrying is noise.

### Secret references — names only

Create these in Catalyst Secret Management / AppSail environment
configuration. **No value appears in this repository, in any default, or in any
log.**

| Reference | Purpose |
|---|---|
| `CAPEX_ANCHOR_JOB_TOKEN` | Authorises invocation of the anchor job. ≥ 32 chars; compared with `hmac.compare_digest`. Rotate per `docs/runbooks/credential-rotation.md`. |
| `CAPEX_DB_PASSWORD` | Existing database credential, read by `pg/config.py`. Unchanged by this job. |

`CAPEX_ANCHOR_JOB_TOKEN` grants exactly one capability — `audit.anchor.write` —
and nothing else in the system. There is deliberately **no `--token` flag**: a
token on a command line is in the shell history, in `ps`, and in the CI log
that echoed the command.

## 3. Exit codes and what to do

| Code | Outcome | Meaning | Action |
|---|---|---|---|
| 0 | `SUCCEEDED` | Today is anchored; the anchors verify | none |
| 2 | usage | argparse rejected the arguments | fix the invocation |
| 3 | `PARTIAL` | Anchored; verification did not finish | invoke again with `--resume-from`; safe |
| 4 | `TAMPER_DETECTED` | The log does not match the anchors | **§4** |
| 5 | `FAILED_RETRYABLE` | Transient (connection, deadlock, resources) | invoke again |
| 6 | `FAILED_PERMANENT` | Bad/missing credential, refused backdate, privilege | **§5** |

A `TAMPER_DETECTED` run also raises the `AUDIT_CHAIN_BROKEN` P1 alert.

## 4. Verification states — five things, not two

Read `verification_state`, not `intact`. `intact: false` collapses situations
whose responses have nothing in common.

| State | What happened | First action |
|---|---|---|
| `INTACT_ANCHORED` | Anchored and verified | none |
| `NEVER_ANCHORED` | No anchor has ever been written | **Not tampering.** The scheduler is not running. Fix §2. Whole-stream deletion is undetectable until the first anchor exists. |
| `ANCHOR_INVALID_OR_STALE` | An anchor's own hash does not verify, an anchor holds a malformed head, the newest anchor has dropped a stream that still exists, or the newest anchor is older than the horizon | Check the scheduler first — staleness is far commoner than forgery. If `anchor_chain_intact: false`, treat as tampering with the evidence itself and escalate. |
| `MISSING_STREAM` | A stream some anchor recorded is entirely gone from `audit_log` | **Escalate.** Deleting a stream requires privileges the application does not hold (`INSERT`/`SELECT` only, append-only triggers). Preserve the database; take a snapshot before anything else. |
| `TRUNCATED_AFTER_LAST_ANCHOR` | A stream's head `seq` is below the highest any anchor recorded | **Escalate**, as above. |
| `DIVERGED_BELOW_ANCHOR` | The head seq is right and the entry hash at the anchored seq is not — history rewritten in place | **Escalate**, as above. |

Verification compares against **every** anchor. A stream deleted the day after
its anchor was written, and re-anchored the next day, is absent from every
later anchor for a perfectly innocent-looking reason; only the earlier anchor
remembers it existed.

Read-only verification is also available without running the job:
`GET /api/audit/anchors/verify`, guarded by `audit.read` (Auditor,
Administrator).

## 5. Common faults

**`AnchorJobNotConfigured`.** No `CAPEX_ANCHOR_JOB_TOKEN`, or one shorter than
32 characters. The job refuses rather than running unauthenticated — a
fallback to "no credential when none is configured" would be unauthenticated
on exactly the deployment nobody configured.

**`AnchorJobDenied`.** The presented token does not match. The message never
says which check failed; telling a caller how far in they are is telling an
attacker.

**`AnchorJobRefused`.** A backdate was requested. See the top of this runbook.

**`unanchored_days > 0`.** The writer missed one or more nights. Those days
carry no evidence and cannot be given any. Fix the schedule, record the gap,
and note that whole-stream truncation occurring within the gap is detectable
only if the stream still existed when the next anchor was written.

**Never** repaired by hand-inserting an anchor row: the row would carry heads
observed at insert time, the unique index on `anchor_hash` and the append-only
triggers exist to refuse exactly that, and a fabricated anchor is worse than a
gap because the gap is honest.

## 6. Verification status

Mixed, and stated per section rather than as one label, because the sections
differ by more than the labels would admit.

| Section | Status | What that means |
|---|---|---|
| §1 the handler, §3 exit codes, §4 states | `VERIFIED-LOCAL` | Exercised by `tests/test_pg_anchor_job.py` and `tests/test_security_audit_anchor.py` on a machine with no PostgreSQL. Authentication, the backdate refusal, the deadline behaviour, the outcome/exit-code mapping and the redaction of results all ran. |
| §1 behaviour against a real database | `UNVERIFIED` | **Every `@pytest.mark.pg` test in `tests/test_pg_anchor_job.py` has never executed anywhere** — concurrent invocation, duplicate invocation, partial failure, retryable failure and truncation found through the job. There is no PostgreSQL on the build host; they first run in CI's `pg_tests` job. A skip is not a pass. |
| §2 Catalyst scheduling | `UNVERIFIED` | The cron, the function and the secret references have **never been created**. This stream creates no cloud resource, applies no configuration and deploys nothing. The values in §2 are what to apply, not a record of anything applied. |
| §5 fault handling | `VERIFIED-LOCAL` for the classification; `UNVERIFIED` for the live faults | The SQLSTATE classification is unit-tested. No real deadlock, connection loss or privilege refusal has been observed through this job. |
