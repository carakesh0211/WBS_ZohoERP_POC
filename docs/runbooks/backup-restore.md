# Runbook — backup, point-in-time recovery, and the restore drill

**Status: MIXED, and mostly `UNVERIFIED`.** The restore *drill* is
`VERIFIED-LOCAL` against SQLite. Everything involving PostgreSQL, WAL archiving
or PITR has **never been executed** — there is no PostgreSQL server, no managed
instance and no Docker on this build host. Read those sections as a design.

## The gate

> **A restore that breaks the audit chain is a FAILED restore.**

Not a restore with a caveat, not something to look at on Monday. A failure, with
a non-zero exit code, treated exactly as "the dump would not load at all".

The two are indistinguishable in consequence. A database that loads but whose
history no longer verifies cannot answer the one question the product exists to
answer — *was this approval edited after the fact?* — and it will go on serving
traffic while being unable to answer it. A dump that will not load at least
announces itself.

This is encoded, not asserted: `tools/restore_drill.py` exits 1 and prints
`THE RESTORE BROKE THE AUDIT CHAIN` when `verify_audit_chain` reports
`intact=False`.

## 1. The restore drill — `VERIFIED-LOCAL` (SQLite path only)

Four checks, in order, stopping at the first failure:

1. the restored database **opens** and passes `PRAGMA integrity_check`;
2. its **schema revision** is known and, if expectations were supplied, matches
   — a restore from before a migration is a different product;
3. **row counts** are plausible — an empty restore opens perfectly and verifies
   perfectly;
4. **every audit stream verifies**, through the *product's* verifier, never a
   copy.

### Executed: a good restore

```
$ cp scratch/poc.db scratch/restored.db
$ python tools/restore_drill.py --sqlite scratch/restored.db
restore drill PASSED for .../scratch/restored.db
  [ok] opens
  [ok] schema_revision
  [ok] row_counts
  [ok] audit_chain
$ echo $?
0
```

### Executed: a restore that lost one row of history

Simulating a restore from a dump that lost the append-only triggers, followed by
a single edited `detail`:

```
$ python tools/restore_drill.py --sqlite scratch/bad_restore.db
RESTORE DRILL FAILED

THE RESTORE BROKE THE AUDIT CHAIN. Broken audit_id(s): [3]. This is a FAILED
restore, not a restore with a caveat: a database that serves traffic while
unable to prove its own history is worse than one that will not start. Do not
promote it.
$ echo $?
1
```

Both directions are also held by `tests/test_ops_readiness.py`, which builds its
own disposable databases.

### The unhashed rows are reported, not hidden

The drill records `unhashed_rows` — the pre-002 rows the verifier skips. They
are history, not evidence, and the count is where verifiable history begins. A
drill that reported only `intact: true` would let a database consisting
*entirely* of unverifiable rows pass as sound.

## 2. PostgreSQL backup — `UNVERIFIED`

Never executed. No server here.

Two mechanisms, and they answer different questions:

**Logical (`pg_dump`)** — portable, restorable into a different major version,
and slow to restore at size. This is the one to use for the migration
rehearsals, because it can be loaded into a scratch cluster.

```
pg_dump --format=custom --no-owner --no-privileges --dbname "$CAPEX_DB_URL" \
        --file "capex-$(date -u +%Y%m%dT%H%M%SZ).dump"
sha256sum capex-*.dump > capex-*.dump.sha256
```

Record the SHA-256 **at creation**. A backup whose integrity is only checked at
restore time has been unverifiable for however long it sat there.

**Physical (base backup + WAL)** — the only mechanism that supports
point-in-time recovery.

```
pg_basebackup --pgdata=/backup/base --wal-method=stream --checkpoint=fast \
              --progress --verbose
```

### What is NOT decided

The VM migration is on hold and no managed PostgreSQL exists, so none of the
following is settled and none of it should be presented to a client as
configured: the WAL archive destination, the retention window, whether backups
are encrypted at rest and with which key, and who holds that key. Each is a
prerequisite for PITR, not a detail of it.

## 3. Point-in-time recovery — `UNVERIFIED`

Never executed. The procedure below is a design.

1. Stop the target cluster. **Never recover over the primary.**
2. Restore the base backup into an empty data directory.
3. Set the recovery target:
   ```
   restore_command = 'cp /wal-archive/%f %p'
   recovery_target_time = '2026-09-09 07:20:00+00'
   recovery_target_action = 'promote'
   ```
4. Start, and wait for recovery to complete.
5. **Run the restore drill against the recovered cluster before it takes any
   traffic.** A recovery target chosen mid-transaction is exactly the shape of
   event that leaves a truncated audit stream, and `verify_chain`'s contiguity
   check is what sees it.

### The PITR failure mode this product is specifically exposed to

`verify_chain` reports `sequence_contiguous` separately from a hash mismatch. A
PITR that stops mid-stream leaves a perfectly linked prefix with a **short
tail** — every hash correct, entries missing. Before the contiguity check
existed, that reported as intact. It no longer does, and it is the single most
likely way a recovered database is silently short of history.

Note the limit, stated in `verify_chain`'s own return value: contiguity proves
nothing is missing from *within* a stream. Deletion of an entire stream is
detectable only against the daily anchors in `audit_anchor`, and **that table
has no writer**. Until it does, a recovered database cannot prove that a whole
stream was not dropped. This is an open gap, recorded here rather than implied
away.

## 4. Schedule and retention — `UNVERIFIED`

Proposed, not configured:

| | Frequency | Retention |
| --- | --- | --- |
| Logical dump | daily | 30 days |
| Base backup | weekly | 4 weeks |
| WAL archive | continuous | 30 days |
| Restore drill | weekly, from the most recent backup, into a scratch cluster | drill reports kept 1 year |

**A backup nobody has restored is not a backup.** The drill is the part of this
table that must not be dropped when the schedule is trimmed.

## 5. What to do when the drill fails

1. **Do not promote the restore.** Do not point traffic at it.
2. **Do not recompute any hash.** That makes a tampered chain verify and
   destroys the only evidence there is.
3. Keep the failed restore. It is the artefact.
4. Try the previous backup, and record how far back you had to go — that
   distance is the real RPO, whatever the schedule says.
5. If every backup fails the same way, the corruption predates them: the source
   was already broken, and this is an `AUDIT_CHAIN_BROKEN` P1 on the primary,
   not a backup problem.
