# Database migrations

**There are TWO schemas in this repository, on two engines, with two runners.
This file used to document only the first, in the past tense, and only the
second in the future tense. Both were wrong.**

| | SQLite | PostgreSQL |
|---|---|---|
| Purpose | the local demo and the VRT harness | the production target |
| Migration files | `app/backend/migrations/` + `db.SCHEMA` | `migrations/pg/` |
| Versions today | **2** (001, 002) | **25** (001 to 025), all tracked |
| Runner | `python -m app.backend.migrate` | `python -m app.backend.pg.migrate_pg` |

Neither runner migrates on boot. That is DEF-01: an application that migrates
itself cannot be rolled back, races when scaled, and turns a schema error into
an outage. Migration is a DEPLOY STEP on both engines, and `run.py` refuses to
start against a schema it does not recognise rather than repairing it.

## SQLite commands

```bash
python -m app.backend.migrate --db app/data/capex_v2.db --status
python -m app.backend.migrate --db app/data/capex_v2.db --fresh --seed   # build a development DB
python -m app.backend.migrate --db <path> --upgrade                      # apply pending migrations
```

`--upgrade` copies the target to `<db>.pre-<version>.bak` before running and refuses to proceed
without that backup. `--fresh` moves any existing file aside as `<db>.replaced-<timestamp>` rather
than deleting it. **The original demo database `app/data/capex.db` is never altered in place.**

## SQLite versions

The SQLite ledger has exactly two versions, and has had for some time. It is
not behind the PostgreSQL schema by oversight -- it is a different, smaller
schema serving the demo, and it is not the deployment target.

| Version | Name | Contents |
|---|---|---|
| 001 | `initial_schema` | The original POC schema, defined once in `db.SCHEMA` so there is a single source of truth. |
| 002 | `financial_controls` | Constraints and control tables added after the 2026-08-06 audit. |

### 002 — what it does

**Rebuilt** (SQLite cannot add CHECK constraints to an existing table): `budget_line`, gaining
`status`, `approved_by`, `approved_at`, `approval_ref`; a CHECK constraining sign by kind so a RETURN
can never increase budget; and a CHECK requiring an approved line to carry its authorisation evidence.

**Altered**: `bill` (`accounting_status`, `reverses_bill_id`, `external_source`, `external_id`,
`voided_by/at/reason`, `version_no`), `purchase_order` (`version_no`, external id columns),
`capitalisation_request` (`version_no`, `posted_reference`, `posted_at`), `audit_log`
(`prev_hash`, `entry_hash`, `correlation_id`).

**New tables**: `app_role`, `user_role`, `app_credential`, `app_session`, `idempotency_key`,
`external_document`, `pr_reservation`, `reconciliation_exception`, `lifecycle_state`.

**Triggers**: original-budget immutability; append-only audit (UPDATE and DELETE both blocked); WBS
cycle prevention and same-project parenting; bill line → PO line ownership (same PO, same WBS and
head); GRN line → PO line ownership; PO line → project ownership; PR → project/WBS consistency;
non-negative PO amounts; progress within 0–100; positive asset allocations; write-off reason required;
WBS permission flags derived from lifecycle status.

**Unique indexes**: `(external_source, external_id)` on both `bill` and `purchase_order`; one live
reservation per PR.

### Data normalisation performed by 002

- `RETURN` and `TRANSFER_OUT` amounts are normalised to negative, `ORIGINAL`/`SUPPLEMENT`/`TRANSFER_IN`
  to positive, so the new sign CHECK holds.
- Budget lines inherit `status` and `effective_date` from their revision; lines with no revision are
  treated as approved original authorisations.
- `budget_revision.approver` held free text (a display name) with no foreign key — part of why
  maker-checker was unenforceable. It is resolved to a real `user_id`, falling back to `U-CFO` where it
  cannot be matched, so the authorisation chain is at least well-formed and typed.
- `wbs_element.allow_procurement` / `allow_posting` are recomputed from `lifecycle_state` so they
  cannot contradict the recorded status.

## SQLite rollback

1. **Preferred** — restore the automatic backup:
   ```bash
   cp app/data/capex_v2.db.pre-002.bak app/data/capex_v2.db
   ```
2. **Rebuild from scratch** (development data only):
   ```bash
   python -m app.backend.migrate --db app/data/capex_v2.db --fresh --seed
   ```

There is no down-migration script. Migration 002 drops and recreates `budget_line`, and reversing that
would discard the status and authorisation columns that the financial controls depend on — a silent
downgrade of a control is worse than an explicit restore. Roll back by restoring the backup.

## Verification after migrating (SQLite)

```bash
python -m app.backend.migrate --db <path> --status      # expect [x] on every version
sqlite3 <path> "PRAGMA foreign_key_check;"              # expect no output
python -m pytest tests/test_migrations.py
```

## PostgreSQL

**The port is done, not planned.** `migrations/pg/` holds **25 migrations,
001 to 025, every one tracked in git**, ending at
`025_fx_applied_at_ingestion.sql`. The earlier claim that the SQLite schema
ports unchanged was withdrawn; the paragraph that replaced it then described
the port as work still to do, and stayed that way while the work landed. This
section records what actually exists.

```bash
python -m app.backend.pg.migrate_pg --status
python -m app.backend.pg.migrate_pg --upgrade
python -m app.backend.pg.migrate_pg --fresh --seed     # disposable DBs only
```

Connection settings come from the environment via `CAPEX_DB_URL`, or
`CAPEX_DB_HOST` / `CAPEX_DB_PORT` / `CAPEX_DB_NAME` / `CAPEX_DB_USER` /
`CAPEX_DB_SSLMODE` (`app/backend/pg/config.py`). The password is never read
from the config object; a password embedded in `CAPEX_DB_URL` is lifted into
an in-memory provider immediately.

Each migration is applied once, in a transaction, with its SHA-256 recorded in
`schema_migrations`. **An already-applied file whose contents changed is a hard
error** — tolerating it is how two environments diverge without anyone
noticing. `assert_schema_current()` is the boot-time counterpart: it checks and
refuses to serve, and never writes, not even an idempotent `CREATE TABLE IF NOT
EXISTS`.

### What the port had to solve, and what it did

Each of these was listed here as a requirement of a future port. Each is
implemented and can be read in the files named.

| Requirement | Status in `migrations/pg/` |
|---|---|
| Ownership triggers replaced by real composite foreign keys | Done — composite `FOREIGN KEY` declarations across `002`, `003`, `004`, `013`, `014`, `017`, `018`, `019` |
| `WITH RECURSIVE` cycle-prevention triggers replaced | Done — see `001_foundation.sql` and `002_budget_control.sql` |
| Partial unique indexes re-expressed as PostgreSQL partial indexes | Done — partial `CREATE UNIQUE INDEX` in `002`, `005`, `008`, `011`, `013`, `014` and later |
| `BEGIN IMMEDIATE` replaced by row-level locking | Done — `SELECT ... FOR UPDATE` in `app/backend/pg/approvals.py` and `approval_rules.py`, in the documented lock order |

### Adopting a pre-existing schema

A database can carry a migration's objects without carrying its record in
`schema_migrations` — the shape of DEF-01's legacy database. `upgrade()` treats
the resulting duplicate-object failure as *adoptable*: if every object the
migration would have created is already present, it records the migration as
satisfied. It does not replay partial DDL and does not guess when only some
objects are present. The adoption check verifies tables, functions, triggers,
named constraints, named indexes, RLS policies together with whether RLS is
actually `ENABLE`d and `FORCE`d, and — the one that matters most — that every
`*_paise` column is `bigint`, not merely present. Where an object cannot be
parsed reliably (an unnamed `UNIQUE`/`CHECK`), it is left unverified rather
than guessed at.

### What is NOT proven

**No local PostgreSQL runs here.** The PostgreSQL test suite skips on this
machine and executes only in CI, and CI cannot currently start jobs — an
external blocker whose cause is unconfirmed. So the migrations above are
tracked and reviewed, but the most recent of them have not been executed by a
green CI run. Do not read "the port is done" as "the port is verified".
