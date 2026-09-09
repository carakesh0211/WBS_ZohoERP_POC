# Runbook — alert conditions and responses

**Status: definitions `VERIFIED-LOCAL`; delivery `UNVERIFIED`.**

The six conditions below are exactly the six in
`app/backend/observability.py::ALERT_CONDITIONS`, and
`tests/test_ops_readiness.py` asserts that the two sets are equal in both
directions. A condition cannot exist in code without a written response here,
and a response cannot be written here for a condition that does not exist.
`observability.alert()` already refuses an unknown condition at runtime; this
closes the other direction, which was open.

**Nothing has ever been delivered to a sink.** `ALERT_CONDITIONS`'s own comment
says "Wired to the sink in Phase 1". `alert()` writes a log record carrying an
`alert` field and a `severity`, and nothing consumes it. Every notification
route below is intended, not observed.

The machine-readable form is [`alert-definitions.json`](alert-definitions.json);
it is the source of truth for thresholds and this file is the prose.

## The two P1s

### `LEDGER_DIVERGENCE`

A materialised control cell disagrees with the ledger it is derived from.
Threshold: **any non-zero difference in paise.** There is no tolerance — a
one-paisa divergence is a defect in the derivation, not noise.

It matters because availability is what a purchase request is checked against.
If the cell is wrong, spend was authorised against a number that does not exist.
That is AUD-C-001's failure mode.

**Do not repair the cell.** Its stored value is the evidence of what the system
believed when it authorised the spend. Record both values and the difference,
freeze budget approval for the affected WBS subtree, and establish which side is
wrong by recomputing from audit history — not from the cell.

### `AUDIT_CHAIN_BROKEN`

`verify_chain` reported `intact=False`, or a stream that should exist reported
`stream_found=False`.

**Do not recompute any hash.** Recomputation makes a tampered chain verify and
destroys the only evidence there is.

Capture `first_break_seq`, `head_seq` and `sequence_contiguous`, and the rows
either side of the break. Take a backup **before** any further writes to the
stream. Then read the result carefully, because it distinguishes three different
incidents:

* **hash mismatch** — an entry's content was edited;
* **`sequence_contiguous=false` with no mismatch** — entries were **deleted**,
  not edited;
* **a break at seq 1 in a stream containing `entry_hash IS NULL` rows** — the
  pre-002 legacy shape, not tampering. See
  [`poc-to-postgres-migration.md`](poc-to-postgres-migration.md); this is why
  legacy rows are imported into `LEGACY_UNHASHED` rather than `LEGACY`.

Escalate if the break is in a stream carrying approval or capitalisation
decisions.

## The four P2s

### `CIRCUIT_OPEN`

A Zoho connection circuit opened; outbound emission for that connection has
stopped. Ticket immediately, page if it stays open beyond 30 minutes.

Read the recent failures first — an expired refresh token and a Zoho outage need
different responses. **Do not force the circuit closed to drain a queue:** the
queue is durable, and forcing it against a failing tenant burns the daily quota.
Check DLQ depth at the same time; an open circuit usually precedes a
`DLQ_DEPTH`.

### `DLQ_DEPTH`

Warn at 25, page at 100, or on any single item older than 24 hours. Every
dead-lettered message is a business event that has not reached its destination.

Group by failure reason **before** retrying anything: a uniform reason is one
fix, a scattered one is systemic. Retries are idempotent by dedupe key, so a
retry cannot double-count a commitment — but confirm the key is present before
retrying in bulk. Discard only with a recorded reason; a discarded item is a
business event deliberately abandoned.

### `DAILY_QUOTA_EXHAUSTED`

Warn at 85% of the ceiling, page at 100%.

Identify the consumer first — an unbounded sweep and a legitimately busy day
look identical in the counter. **Do not raise the ceiling as a first response:**
if a defect is spending the quota, a larger ceiling spends more of it. Confirm
the reset time, and warn finance if a period close depends on a sync landing
today.

### `JOB_RESUME_LIMIT`

A job hit `max_resume_count` and was paused. The work it was doing is now not
being done at all.

Read the checkpoint: a cursor that has not advanced across resumes means the
chunk itself fails, not that the deadline is tight. **Do not raise
`max_resume_count` to clear the alert** — that converts a stuck job into a
silently stuck job.

## What is missing

* **No sink.** No pager, no ticket queue, no delivery of any kind.
* **No scheduled evaluator.** `LEDGER_DIVERGENCE` and `AUDIT_CHAIN_BROKEN` are
  the two that need a periodic job to detect them at all; there is none, so
  today they are only ever noticed by a request path that happens to check.
* **No `audit_anchor` writer**, so whole-stream deletion remains undetectable.
  Recorded as an open gap in `verify_chain`'s own return value and in
  [`backup-restore.md`](backup-restore.md).
