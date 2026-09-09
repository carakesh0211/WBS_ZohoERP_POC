# Runbook — POC (SQLite) to PostgreSQL migration

**Status: MIXED. Read the per-step status. The import against a live PostgreSQL
server is `UNVERIFIED`.**

## 0. Read this first: the migration cannot run today, by design

`tools/migration/import_pg.py::COLUMN_DECISIONS` is **empty**, and the preflight
refuses while it is. That refusal is the correct current state, not an
oversight:

```
$ python -m tools.migration.import_pg --export export1 --preflight
...
ready: False
blockers:
  - 66 source column(s) across 19 table(s) have no target column and no recorded
    decision in COLUMN_DECISIONS. Dropping a column silently is how a migration
    loses data that nobody misses until an audit.
  - 1 of those are MONEY columns: {'capitalisation_request': ['total_paise']}.
    Reconciliation cannot balance a total that has nowhere to land.
  - 8 POC table(s) have no target and no entry in SOURCE_ONLY_TABLES:
    ['app_credential', 'app_role', 'app_session', 'external_document',
     'idempotency_key', 'schema_migration', 'user_role', 'zoho_connection']
$ echo $?
1
```

The POC schema and the production schema are not the same schema. 22 of the
POC's 30 tables share a name with a production table; **66 of their columns have
no target**, and one of those is money. Mapping them is analysis work that has
not been done. Until it is, a migration that ran would be dropping data
silently, and the preflight is the control that stops it.

**Do not "unblock" this by filling `COLUMN_DECISIONS` with `drop because
unmapped`.** Each entry is a decision that someone has to be able to defend.

## 1. Export the POC — `VERIFIED-LOCAL`

Read-only against the source. Deterministic. Integers stay integers.

```
$ python -m tools.migration.export_poc --source scratch/poc.db --out export1
exported 187 rows across 30 tables to .../export1
  audit_log: 5 hashed, 1 unhashed (verifiable history begins at audit_id 2)

$ python -m tools.migration.export_poc --source scratch/poc.db --out export2
exported 187 rows across 30 tables to .../export2

$ diff -r export1 export2 && echo BYTE-IDENTICAL
BYTE-IDENTICAL

$ python -m tools.migration.export_poc --source scratch/poc.db --out export1 --verify
export verified against its manifest.
```

Read-only is enforced by SQLite, not by convention:

```
>>> con = open_source_readonly("scratch/poc.db")
>>> con.execute("INSERT INTO audit_log ...")
OperationalError: attempt to write a readonly database
```

`manifest.json` carries, per table: row count, SHA-256 of the file's bytes, the
`ORDER BY` used, the money columns, the float-declared columns, and the
timestamp normalisations applied.

### The one table exported verbatim

`audit_log.at` is inside the frozen hash payload
`prev|at|actor|action|type|id|detail`. Normalising a naive
`2026-08-06T09:00:00` to `...+00:00` changes the bytes, changes the hash, and
turns an intact chain into a broken one. So `audit_log` is exported with **no**
normalisation, and the manifest says so in the table's own entry:

```json
"normalisation": "none (hash-bearing: bytes are an input to entry_hash)"
```

## 2. Preflight and dry run — `VERIFIED-LOCAL`

Both change nothing. The dry run drives the **real** import path against a
recorder, so what it reports is what the import would issue, not a separate
"what would happen" branch that tests only the reporting.

```
python -m tools.migration.import_pg --export export1 --preflight     # no DB at all
python -m tools.migration.import_pg --export export1 --dry-run       # real path, recorded
```

## 3. Import — `UNVERIFIED against PostgreSQL`

The behaviour is `VERIFIED-LOCAL` against a disposable SQLite target with
`PRAGMA foreign_keys = ON` (`tests/test_migration_import.py`, 37 tests). The SQL
running on **PostgreSQL** has never been executed: there is no PostgreSQL and no
Docker on this build host. `tests/test_pg_migration_import.py` holds that half
and skips here.

Two modes, and they are not interchangeable:

| | `--atomic` (default) | `--resumable` |
| --- | --- | --- |
| Transactions | one, for everything | one per table, in topological order |
| After a failure | target untouched | consistent FK-closed prefix |
| Restart | re-run from scratch | skips committed tables |
| Use for | a cutover | a long import over a flaky link |

Foreign keys are **enabled throughout**. `session_replication_role = 'replica'`
is never set, and `tests/test_migration_import.py::test_the_module_never_emits_a_statement_that_would_disable_a_constraint`
parses this tool's own source with `ast` and fails if it ever is. Most of the
import's value is that it doubles as a constraint proof; with the constraints
off it proves only that the rows fit in the columns.

## 4. Reconciliation — `VERIFIED-LOCAL`

Three checks, and the third catches what the first two cannot:

1. row counts per table,
2. paisa totals per table per money column,
3. **paisa totals per dimension** (`entity_id`, `project_id`, `wbs_id`,
   `budget_head_id`).

Check 3 is why this is not one `SELECT SUM`. Moving ₹100 from `PRJ-1` to
`PRJ-2` leaves every table total identical; only the per-dimension totals see
it. That case is a test:
`test_two_errors_that_cancel_are_caught_by_the_per_dimension_check`.

**Tolerance is zero.** One paisa fails the migration.

## 5. The audit chain — `VERIFIED-LOCAL` for the plan, `UNVERIFIED` on a server

Hashes are **copied**. They are never recomputed. `verify_chain` runs at the
end, and `intact=False` aborts.

### The measured contradiction, and what was done about it

The instruction was: import into `stream_key='LEGACY'` with `seq = original
audit_id`, keep the pre-002 unhashed rows because "the verifier already skips
them", and abort if not intact. Three of those four cannot hold together. Each
candidate below was **executed** against `app/backend/pg/audit.py::verify_chain`
in `tests/test_migration_audit_chain.py`:

| Strategy | `intact` | Why |
| --- | --- | --- |
| all rows, `seq = audit_id`, `prev_hash` `''` kept | **False** | The POC stores `''` for the genesis row; the verifier compares against an `expected_prev` that starts as `None`. `'' != None` → break at seq 1. |
| all rows, `seq = audit_id`, `''` → `NULL` | **False** | The PostgreSQL verifier does **not** skip `entry_hash IS NULL` rows. `services.verify_audit_chain` has an explicit `continue` for them; `pg.audit.verify_chain` has no such branch — it recomputes a hash for every row and compares it against `NULL`. |
| hashed rows only, `seq = audit_id` | **False** | `seq` then starts at 2, and `verify_chain` requires `actual_seqs == list(range(1, checked + 1))`. |
| **hashed rows only, `seq` re-assigned 1..n, `''` → `NULL`** | **True** | The one that works. |

The premise "the verifier already skips unhashed rows" is true of the **POC**
verifier and false of the **PostgreSQL** one. That is the whole difficulty, and
it is a difference between two live functions, not a judgement call.

Re-sequencing is safe because **`seq` is not in the hashed payload** —
`compute_entry_hash` takes `prev, at, actor, action, object_type, object_id,
detail` and nothing else. That is asserted directly, by signature inspection,
because everything else here depends on it.

So:

* **`LEGACY`** carries the hashed rows, re-sequenced 1..n, hashes verbatim.
* **`LEGACY_UNHASHED`** carries the pre-002 rows, `entry_hash IS NULL`
  preserved. They are history; they are not evidence.
* The report records `unhashed_entries`,
  `verifiable_history_begins_at_source_audit_id`, and the full
  `seq_to_source_audit_id` map — without which "the chain is intact" is not a
  checkable claim, because `seq` is no longer `audit_id`.

### Two traps the planner refuses rather than absorbs

* **A naive `at` on a hashed row.** A hash over `2026-08-06T09:00:00` cannot
  verify once `at` is a `timestamptz`, because the column reads back as
  `...+00:00`. Refused in the plan, not discovered by an abort after insertion.
* **A NULL `detail`.** The POC's payload f-string interpolates a NULL `detail`
  as the four characters `None`, so `None` is what the stored hash covers. The
  target column is `NOT NULL`; substituting `''` there would break a hash that
  is otherwise perfect. The planner carries `"None"`.

### The rule, stated once more

`test_recomputing_a_hash_makes_a_TAMPERED_row_verify` edits a committed entry,
recomputes the chain the way a "helpful" migration would, and the result
verifies perfectly. That is why the rule is absolute.

## 6. Rollback — `VERIFIED-LOCAL`

Exercised, not described. `test_a_failure_rolls_the_whole_atomic_import_back`
fails an import part-way through the last table and asserts all three tables are
**empty** afterwards. `test_rerunning_an_atomic_import_after_a_failure_is_clean`
then re-runs it to completion.

In `--resumable`, `test_a_resumable_run_that_is_interrupted_leaves_a_consistent_prefix`
asserts the committed prefix passes `PRAGMA foreign_key_check` — the prefix is
FK-closed because the order is topological.

The checkpoint is written **only after a commit**
(`test_the_checkpoint_is_written_only_after_a_commit` watches every `commit()`
call and asserts the checkpoint still holds only the previously committed
tables). A checkpoint written first tells a restart to skip a table that is not
there.

A checkpoint from a **different** export is refused by manifest hash: resuming
run B's second half on top of run A's first half produces a target that
reconciles against neither.
