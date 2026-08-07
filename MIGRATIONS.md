# Database migrations

## Commands

```bash
python -m app.backend.migrate --db app/data/capex_v2.db --status
python -m app.backend.migrate --db app/data/capex_v2.db --fresh --seed   # build a development DB
python -m app.backend.migrate --db <path> --upgrade                      # apply pending migrations
```

`--upgrade` copies the target to `<db>.pre-<version>.bak` before running and refuses to proceed
without that backup. `--fresh` moves any existing file aside as `<db>.replaced-<timestamp>` rather
than deleting it. **The original demo database `app/data/capex.db` is never altered in place.**

## Versions

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

## Rollback

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

## Verification after migrating

```bash
python -m app.backend.migrate --db <path> --status      # expect [x] on every version
sqlite3 <path> "PRAGMA foreign_key_check;"              # expect no output
python -m pytest tests/test_migrations.py
```

## Porting to PostgreSQL

The earlier claim that this schema ports unchanged is **withdrawn**. A port must at minimum:

- replace ownership triggers with real composite foreign keys;
- replace `WITH RECURSIVE` cycle-prevention triggers with a constraint trigger or an `ltree`/closure
  table;
- re-express partial unique indexes (`WHERE state='Reserved'`) as PostgreSQL partial indexes — the
  syntax is compatible but must be re-tested;
- replace `BEGIN IMMEDIATE` in `services.critical()` with `SELECT … FOR UPDATE` on the budget-owner row
  or `SERIALIZABLE` isolation with retry;
- re-run the full constraint test suite against the target engine, because none of the above is proven
  by the SQLite tests.
