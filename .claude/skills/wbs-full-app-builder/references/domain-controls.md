# Domain controls — financial correctness

These are the rules the product exists to enforce. A feature that violates one
is not a feature; it is a defect that happens to render.

Authoritative sources: `research/30_contracts/C5_formulas.json` (frozen),
`app/backend/domain.py`, plan §6–§9.

## Money

- **Integer paise, always.** `bigint` in PostgreSQL, `int` in Python.
- No float ever touches a monetary value — not in arithmetic, not in transport,
  not in JSON, not in a test fixture.
- Use `money.to_paise` / `to_rupees` / `format_inr` / `split_pro_rata`.
- `split_pro_rata` must never lose or invent a paisa; the existing property test
  holds this and must keep passing.

## The control cell

The unit of budget control is the **`(wbs_id, budget_head_id)` cell**. Every
availability question, every lock, every rollup is expressed in those terms.

Stored per cell (own values only — rollups are computed, never stored):

```
budget_paise             approved AND effective in the open period
original_paise           kind='ORIGINAL', immutable
revisions_paise          approved revisions
future_budget_paise      approved, effective in a FUTURE period
ordered_paise            sum of PO line amounts
commitment_paise         max(0, ordered - billed)
actual_paise             billed and accounting-effective
received_paise           goods receipted
received_not_billed_paise
pr_reserved_paise        live PR reservations
```

## Availability

```
exposure  = commitment + actual + pr_reserved
available = budget - exposure
```

Both sides are **subtree sums** over the WBS hierarchy, computed at check time
by an `ltree` subtree scan over cells — never by scanning transaction history.

Rolled-up figures are derived. Never persist a rollup: it will drift.

## Anti-double-count

- `commitment = max(0, ordered - billed)`. As a bill lands, commitment falls and
  actual rises by the same amount. The pair never double-counts.
- A PO in `Cancelled` or `Closed` contributes **zero** commitment.
- `received_not_billed` is its **own bucket**, never added into commitment or
  actual. It exists to make the gap visible, not to inflate exposure.
- A PR reservation is released exactly once — on conversion, release or expiry —
  enforced by a partial unique index, not by service-layer discipline.

## Anti-under-count

Under-counting is the more dangerous direction: it silently authorises spend.

- A receive line that cannot be attributed to a known `po_line` goes to
  `reconciliation_exception` (`GRN_LINE_UNATTRIBUTED`) and accumulates in a
  project-level `unattributed_receipts_paise` bucket. **Never spread pro-rata.
  Never silently drop.**
- A PO found in Zoho carrying a CAPEX dimension with no local `external_id`
  raises `UNSANCTIONED_COMMITMENT` and blocks period close.
- An unmapped external status raises `UNMAPPED_EXTERNAL_STATUS`. Never guess a
  mapping.
- Open exceptions block capitalisation **and** period close.

## Budget-owning ancestor

A cell "owns budget" for a head when its `budget_paise` for that head is
non-zero. The **nearest budget-owning ancestor** of `(w, h)` is the closest
ancestor-or-self of `w` that owns budget for `h`. That is the cell whose
availability governs a spend.

## Locking — the whole ancestor chain, in order

**Locking only the nearest budget-owning ancestor is insufficient and was a
real correctness hole.** Exposure rolls up the entire tree, so a spend deep in
the hierarchy reduces availability at *every* budget-owning ancestor above it.

For affected cells `A = {(w₁,h₁) … (wₙ,hₙ)}`, the lock set is:

```
L = ⋃ over (w,h) ∈ A of
      { (a, h) : a is ancestor-or-self of w AND budget_paise(a,h) ≠ 0 }
```

Acquire with `SELECT … FOR UPDATE ORDER BY wbs_path, budget_head_id` — a total
order, so concurrent transactions cannot deadlock.

Rules that make the proof hold, and must not be broken:

1. Every mutating service function calls `lock_affected_cells` **exactly once**,
   as its **first** locking action, with the **complete** affected set.
2. No cell lock is acquired afterwards.
3. Global lock order is: (1) cells, (2) document row via `FOR UPDATE`,
   (3) `pg_advisory_xact_lock` for the audit stream, taken last.

Cells with `budget_paise = 0` carry no availability and are correctly excluded.

## Re-check at write time

Approval does not freeze availability. `amend_po`, `approve_pr` and every
commitment-creating path **re-run `budget_check` inside the lock at write time**
and raise `BUDGET_MOVED` if availability shifted since approval.

## Maker-checker

- `auth.MAKER_CHECKER` and `require_separation` are **not modified and not
  removed**. Existing calls stay, in the same order, before any engine call.
- The approval engine calls `require_separation` **again** at decision time — a
  second independent enforcement point.
- `contributor_set(object)` = maker + editors + prior actors. It is a
  **superset**: it may refuse more, never less.
- Delegation checks **both** `actor_user_id` and `acting_for_user_id`. A
  delegation can never launder a self-approval.
- An empty approver set after the contributor filter → `EXCEPTION_PENDING` with
  `NO_INDEPENDENT_APPROVER`. **Never auto-approve an empty stage.**

## Approval principles

- No matching route → `ApprovalRouteUnresolved`. **Fail closed.** An unroutable
  object is a configuration defect, never an auto-approval.
- A stage whose `applies_when` is false is recorded `SKIPPED` with a
  `skip_reason` — never omitted. An auditor must see what did not run.
- Predicates are a restricted JSON AST. Money operands are **integer paise
  only**; the compiler rejects a float literal in a `*_paise` comparison.
- `approval_action` is append-only, hash-chained, and owned by
  `capex_audit_owner` — immutability is a database property, not discipline.

## Audit chain

- Payload format `prev|at|actor|action|type|id|detail` is **frozen permanently**.
  Changing it invalidates every stored hash.
- Per-stream chains: `stream_key` + `seq`, `UNIQUE (stream_key, seq)`, with
  `pg_advisory_xact_lock` before reading `prev_hash`.
- **`audit_id` is not the chain order** — identity values are assigned before
  commit and can commit out of order. `seq` under the lock is the order.
- Daily anchors hash every stream head into one root, exportable off-platform.
- Nightly verification job, P1 alert on `intact=False`.
- App role holds `INSERT, SELECT` only. No `UPDATE`, no `DELETE`.

## Fiscal periods

- `accounting_period` states: `FUTURE → OPEN → SOFT_CLOSED → CLOSED`, with a
  `gist` exclusion constraint preventing overlap per entity.
- `budget_paise` depends on `effective_date <= today()`, so a cell's value
  changes with the **wall clock**, not only with writes. Any cache must account
  for that or be wrong at midnight.
- `roll_period_effective_budget` moves amounts between `budget_paise` and
  `future_budget_paise` at period open, one entity per invocation.
- Documents dated into a `CLOSED` period raise `LATE_ARRIVAL_CLOSED_PERIOD`.
- **A period cannot close while an Open reconciliation exception exists.**

## Invariants that must hold after every mutation

Assert these in tests, not just in review:

```
available   == budget - actual - commitment - reservation
exposure    == commitment + actual + reservation
po_line:    billed + open_commitment == ordered
rollup      == own + sum(children)
money       is only ever integer paise
```
