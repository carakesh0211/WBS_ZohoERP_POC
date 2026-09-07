# Wave 6 — the PostgreSQL procurement contract, frozen

**Status: FROZEN before implementation.** Four agents build against this
document. Nothing below is invented: every column, constraint and name is
traced to the SQLite POC (`app/backend/db.py::SCHEMA`), the approved plan
(`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` §6.1/§6.2/§11.7), or a
frozen registry in `research/30_contracts/`. Where something is genuinely
absent from all three it is recorded as a **CONTRACT GAP** and named as such,
per `C4_entities.json`'s own rule: *"A needed value that is absent must be
returned as a contract_gap, never improvised."*

---

## 0. Why this document exists

`FULL_APPLICATION_DELIVERY_STATUS.md` claimed "Phase 1 — PostgreSQL port,
behaviour-identical | complete". It was not. Migrations 001–012 create 53
tables and **not one of them is a procurement document**. Verified three ways:

* `grep -c "CREATE TABLE.*<name>" migrations/pg/*.sql` returns 0 for all eight.
* `/api/integrations/reconciliation` already refuses for this reason.
* `app/backend/integration/sweeps.py` attributes receive lines to a `po_line`
  that has no PostgreSQL home.

The consequence is that the Wave 5 exit criterion — *"ordered, received,
billed and open reconcile to the paisa"* — is unreachable, because three of
those four quantities have nowhere to live. Wave 6 is that schema.

---

## 1. CONTRACT GAPS — read these before building

### GAP-1: `pr_line` exists in no frozen contract

The Wave 6 brief lists `pr_line` among the tables to port. It is not portable,
because it does not exist to port:

| Source | Says |
|---|---|
| `app/backend/db.py::SCHEMA` | No `CREATE TABLE pr_line`. `purchase_request` is **header-only** — one `amount_paise`, one `wbs_id`, one `budget_head_id`. A PR addresses exactly one control cell. |
| `research/30_contracts/C4_entities.json` | 44 frozen entities. `Purchase Request Reference` is present; **there is no `PR Line`.** The other seven tables all have entries. |
| Approved plan §11.7 | *"PR → PO conversion becomes line-level. `convert_pr_to_po(con, actor, pr_id, *, lines: list[PrLineToPoLine])` validates that every converted line lands on the same `(wbs_id, budget_head_id)` control cell as the reservation."* |

So the plan **does** contemplate line-level PR conversion, and the product
owner has explicitly asked for `pr_line`. It is therefore built. But two
things are recorded rather than smoothed over:

1. **This changes the PR grain**, and the PR grain is a financial-control
   surface: `domain.budget_check`, `pr_reservation` and `convert_pr_to_po`
   all currently assume one PR ↔ one control cell. §2.1 below states how the
   header reconciles to its lines so the existing controls keep their meaning.
2. **`C4_entities.json` is frozen at 44 and its QA gate counts 44.** A `PR
   Line` entity is NOT added here — that would break the count and improvise
   inside a frozen registry. It is raised as a contract gap for the client to
   amend. Until then `pr_line` traces to `Purchase Request Reference`.

### GAP-2: `grn` has no external-document columns in the POC

`bill` and `purchase_order` were given `external_source` / `external_id` and a
partial unique index by `app/backend/migrations/002_financial_controls.sql`.
`grn` was not — it carries only a bare `zoho_receive_id`. Plan §6.1 requires
the four-column external block *"on every Zoho-mirrored table"*, and a GRN is
mirrored from Zoho by definition. **The block is added to `grn`.** This is the
plan's rule applied, not a new field: it is recorded here because it is the one
place Wave 6 gives a table something the POC did not have.

### GAP-3: `purchase_request.status` and `bill.accounting_status` have no CHECK

Both are free text in SQLite, with the permitted values written only in a
trailing comment. `C3_statuses.json` and AUD-C-004 both define the real value
sets. Wave 6 adds named CHECK constraints. Recorded because it makes previously
acceptable rows invalid, so the migration must state what it rejects.

---

## 2. The frozen table contract

Conventions applied to every table below, from plan §6.1: money is **`bigint`
paise, never numeric and never a float**; timestamps are `timestamptz`;
`created_at`/`created_by`/`updated_at`/`updated_by`/`version_no` on every
table; **no soft delete on any financial document**; Zoho-mirrored tables carry
`external_source`, `external_id`, `external_last_modified`, `payload_sha` with
`UNIQUE (external_source, external_id)` as a **partial** index `WHERE
external_id IS NOT NULL`.

**Every constraint is explicitly NAMED.** `migrate_pg.py::_named_constraints_by`
detects only named constraints, so an anonymous `CHECK` or `UNIQUE` is
invisible to adoption verification and would certify as adopted while absent.

**Every `*_paise` column must start its declaration line**, because
`_PAISE_COLUMN_RE` is anchored at `^` — a column the parser cannot see is a
money column whose type is never verified.

### 2.1 `purchase_request` and `pr_line`

`purchase_request` keeps the POC's columns, minus the three moved to the line
table. The header retains `amount_paise` as a **derived total**, maintained to
equal `SUM(pr_line.amount_paise)` by a named CHECK-backed trigger, so
`domain.budget_check`'s existing header-level reads keep their meaning while
the lines carry the control-cell grain (GAP-1).

`pr_line` columns: `pr_line_id` (PK), `pr_id`, `line_no`, `wbs_id`,
`budget_head_id`, `project_id` (denormalised for the composite FK),
`description`, `quantity numeric`, `amount_paise bigint`.

* `UNIQUE (pr_id, line_no)` — named `ux_pr_line_number`. The POC had no such
  constraint on `po_line` and duplicate line numbers were possible.
* Composite FK `(wbs_id, project_id) → wbs_element (wbs_id, project_id)`,
  replacing trigger `pr_project_wbs_consistency` per §6.2 — and covering
  `UPDATE`, which the trigger did not.

### 2.2 `purchase_order` and `po_line`

Per §6.2, two triggers become composite FKs, which requires **denormalised
columns the POC does not have** and **unique constraints on the targets**:

* `po_line.project_id` denormalised. FK `(wbs_id, project_id) → wbs_element`
  and `(po_id, project_id) → purchase_order (po_id, project_id)`.
* `purchase_order` gains `UNIQUE (po_id, project_id)` — named
  `ux_po_id_project`, existing only to be an FK target.
* `po_line` gains `UNIQUE (po_line_id, po_id)` — named `ux_po_line_po`, the
  target of `grn_line`'s FK.
* `po_line` gains `UNIQUE (po_line_id, po_id, wbs_id, budget_head_id)` — named
  `ux_po_line_cell`, the target of `bill_line`'s four-column FK.
* `CHECK (amount_paise >= 0 AND non_creditable_tax_paise >= 0 AND freight_paise >= 0)`
  named `ck_po_line_non_negative`, replacing the trigger — and now covering
  `UPDATE`, which the INSERT-only trigger did not.
* `ux_po_external` ports 1:1 as a partial unique index.

**`po_line` is keyed on `(wbs_id, budget_head_id)`** — the grain
`outbound.py:295` already states and emits against. A multi-WBS PO is normal
and is not flattened.

### 2.3 `grn` and `grn_line`

* `grn` gains the §6.1 external block (GAP-2), with `ux_grn_external` partial
  on `external_id IS NOT NULL`.
* `grn_line` FK `(po_line_id, po_id) → po_line (po_line_id, po_id)`, replacing
  trigger `grn_line_po_ownership`. `grn_line` gains a denormalised `po_id`.
* **`grn_line.quantity` and `.amount_paise` stay SIGNED.** The POC seeds a
  reversal line at `-0.2` / `-1_20_000`. A `>= 0` CHECK here would reject
  reversals, and reversal-by-flag is the AUD-C-004 contract.
* `UNIQUE (po_line_id, receive_external_id, line_external_id)` named
  `ux_grn_line_external` — idempotent replay of a receive must not duplicate.

### 2.4 `bill` and `bill_line`

* `bill` keeps `accounting_status ∈ {Draft, Approved, Void, Reversal}` from
  AUD-C-004, now as named CHECK `ck_bill_accounting_status` (GAP-3). Only
  `{Approved, Reversal}` are accounting-effective; `Reversal` negates **by
  flag, not by data entry**.
* `bill_line` gains denormalised `po_id`. FK
  `(po_line_id, po_id, wbs_id, budget_head_id) → po_line` **`ON UPDATE
  RESTRICT`**, replacing the two ownership triggers and additionally stopping a
  `po_line` being re-pointed underneath a bill — which the trigger pair did not
  cover, and which §6.2 calls out by name.
* `bill_line.po_line_id` stays **nullable** (non-PO bills exist). The FK is
  therefore only enforced when it is present, exactly as the trigger's
  `WHEN NEW.po_line_id IS NOT NULL` was.
* `ux_bill_external` ports 1:1.
* Credit notes: `doc_type ∈ {BILL, CREDIT_NOTE, DEBIT_NOTE}` named
  `ck_bill_doc_type`. A credit note's line amounts are **negative paise** and
  `render_paise` must sign them correctly — `divmod` floors, so `-150` renders
  as `-2.50` unless the magnitude is divided and the sign reapplied.

---

## 3. Migration number and mechanics

**The next migration is `013_`.** Verified: `migrations/pg/` holds 001–012 and
`migrate_pg.py::_FILENAME` is `^(\d{3})_([a-z0-9_]+)\.sql$`. Non-migration
files are excluded by exact name in `NON_MIGRATION_FILES`, not by pattern —
adding a data file means adding to that set.

Every migration carries the house header (filename, one-line thesis, ALL-CAPS
prose sections explaining the defect closed) and a trailing commented
`-- ROLLBACK:` block stating what reverting costs.

Every table needs `GRANT` statements naming `capex_app` explicitly. Omitting
them worked before only because one superuser runs every migration in CI —
the same assumption that hid the RLS bypass.

## 4. RLS and the two registries

`capex_scope_permits(entity_id, plant_id, location_id, project_id)` — **that
argument order**. Migration 011 passed `project_id` into the `p_location_id`
slot and the project dimension went unenforced.

Every new table needs `ENABLE` **and** `FORCE ROW LEVEL SECURITY`, a named
policy with `USING` **and** `WITH CHECK`, and entries in **both** hand-
maintained registries in the same commit:

* `app/backend/pg/rls.py::RLS_COVERAGE_TABLE_COLUMNS` — four dimensions each
  mapped to a column or an explicit `None`; plus membership of
  `JOINED_VIA_WBS_ELEMENT` / `TWO_LEGGED` / `REFERENCE_TABLES` where the shape
  is not a simple single join.
* `app/backend/pg/scope_inventory.py::SCOPED_TABLES` — a `ScopedTable` giving
  `dimensions`, `reach`, `path`, `status`, `migration`.

`tests/test_pg_rls_coverage.py` asserts set equality in **both** directions, so
omitting either registry fails CI.

Line tables (`po_line`, `grn_line`, `bill_line`, `pr_line`) reach their
dimensions through their parent and are `reach="joined"`, following the
`budget_transfer` pattern in `006_rls_coverage.sql`.

## 5. Immutability

Posted or imported financial records are immutable except through an explicit
correction/reversal workflow. Following the `audit_log` precedent, this is a
**privilege**, not only a trigger: the app role gets `SELECT, INSERT, UPDATE`
where it needs it and **no `DELETE`** on any of the eight tables. Reversal is a
new row with `is_reversal = true`, never an edit and never a delete.

## 6. What Wave 6 does NOT do

No dashboards. No production deployment. No UAT. No VM migration. No Catalyst
or Supabase work. **No live Zoho call** — every outbound path is exercised
against contract fixtures and adapter fakes. No access to any existing
database, cloud project or client data.
