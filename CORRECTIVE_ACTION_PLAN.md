# Corrective Action Plan

**Against:** independent quality audit, cutoff 2026-08-06 11:46 IST
**Audit opinion:** "POC is technically functional but not reliable as a financial-control system."
**Audit recommendation:** Production No-Go
**Baseline:** current source was byte-identical to `audit_output/current_snapshot` when work began, so
no third-party changes needed preserving. Verified by diff across all five source files.

---

## Approach

The audit's 44 failures are not 44 independent bugs. They collapse into six structural causes, and
fixing the cause fixes the symptoms. That drives the implementation order below — each phase makes the
next one possible.

| # | Structural cause | Findings it produces |
|---|---|---|
| 1 | Money passed through binary float | AUD-H-007 |
| 2 | No constraints in the database; the ledger could be corrupted by any path | AUD-C-003, C-005, C-009, C-010, H-006 |
| 3 | No server-derived identity, so no authorisation and no maker-checker | AUD-C-006 |
| 4 | Read-compute-write with no transaction, so checks did not bind writes | AUD-C-001, H-009 |
| 5 | The ledger pooled dimensions and ignored document status | AUD-C-002, C-004, C-005, C-008, H-001 |
| 6 | Mock integration reported synthetic success | AUD-C-007, H-002, H-003, H-004, H-005 |

## Implementation order and dependencies

```
1  money.py ......................... foundation; everything downstream stores paise
2  migrations/002 .................. constraints + new tables; needs nothing, blocks 4,5,6
3  auth.py ......................... identity/RBAC/SoD; needs 2 (user_role, app_session)
4  services.py ..................... transactional guarded operations; needs 1,2,3
5  domain.py ....................... ledger keyed on control cell; needs 1,2
6  main.py ......................... thin routes; needs 3,4,5
7  zoho.py ......................... gate on connection + scope; needs 2,4
8  tests/ .......................... regression proof; needs 1-7
9  frontend ........................ login, accessibility, responsive; needs 6
10 documentation ................... needs 1-9 to be accurate
```

## Finding-by-finding approach

### Critical

| Finding | Approach |
|---|---|
| **AUD-C-001** PO amendment overspend + race | Move the operation into `services.amend_po` inside `critical()`, which opens `BEGIN IMMEDIATE` so check and write cannot be separated. Refuse to persist an over-budget increase at all unless an *independently approved* exception reference is supplied and validated against the same control cell. Add optimistic `version_no` on top. |
| **AUD-C-002** budget head presentation-only | Re-key the ledger on the **(WBS element, budget head) control cell**. Budget ownership is resolved per head, so a head with no budget on the branch has no availability to consume. |
| **AUD-C-003** relationship integrity | Database triggers enforcing ownership: bill line → PO line on the same PO and same WBS/head; GRN line → PO line on same PO; PO line WBS → PO's project; PR WBS → PR's project; WBS parent → same project. Service layer rejects the same tuples with 422 before reaching the DB. |
| **AUD-C-004** bill lifecycle | Add `accounting_status` with an explicit effective set (`Approved`, `Reversal`). Only effective bills move actual or relieve commitment. Reversal negates by flag, not by hoping someone typed a negative. Unique index on (external_source, external_id). |
| **AUD-C-005** budget governance | Rebuild `budget_line` with `status`, `effective_date`, `approved_by`; the ledger counts only `Approved` AND effective-on-or-before-today. CHECK constrains sign by kind so a RETURN can never increase budget. Triggers make ORIGINAL rows immutable and undeletable. |
| **AUD-C-006** identity and SoD | `auth.py`: password-based sessions, seven server-side roles, a permission matrix, and `require_separation` which refuses when approver == maker. Actor comes from the session; the request body can no longer name an actor. |
| **AUD-C-007** idempotency | `idempotency_key` table keyed on (key, route, request hash); `external_document` table giving one durable business event per (source, org, module, external id). Sync replays return the first result. |
| **AUD-C-008** lifecycle gates | `lifecycle_state` table is the single source of truth for whether a state permits procurement or posting. Unknown states deny. A trigger derives the WBS boolean flags from status so they cannot disagree. |
| **AUD-C-009** capitalisation | Approval is gated on zero open commitment, zero received-not-billed, zero open reconciliation exceptions, and allocations equalling the CWIP balance exactly. Positive-allocation and write-off-reason triggers. Duplicate approval refused. The response states plainly that nothing has been posted to any ledger. |
| **AUD-C-010** audit evidence | Append-only triggers block UPDATE and DELETE on `audit_log`; entries are SHA-256 hash-chained so tampering by a privileged identity is still detectable; `/api/audit/verify` exposes the check. `/api/admin/reset` requires the Administrator role *and* `CAPEX_PROFILE=local-demo`. |

### High

| Finding | Approach |
|---|---|
| **AUD-H-001** PR reservation | `pr_reservation` ledger with states Reserved/Converted/Released/Expired and a partial unique index allowing one live reservation per PR. Cancelling or closing a PO settles its reservations. |
| **AUD-H-002** mock connector | Keep MOCK. Label it in `/api/health`, every sync response and the UI. Do **not** claim live readiness. A live adapter is scoped but not built — no sandbox tenant is available. |
| **AUD-H-003** connector gating | Sync enforces connection state and granted scope, returning 409/403 rather than a synthetic PASS. Unknown module → 404. Authorising a nonexistent connection → 404. |
| **AUD-H-004** receipt-to-PO-line | **Not resolved.** Requires a documented composite matching algorithm plus a quarantine queue. `reconciliation_exception` is the landing table; the algorithm is deferred pending a live tenant to validate against. |
| **AUD-H-005** external reconciliation | Structure added (`reconciliation_exception`), population deferred — there is no external ledger to reconcile against without a tenant. |
| **AUD-H-006** DB constraints | Covered by migration 002 (see Critical rows above). |
| **AUD-H-007** money | `money.py` with Decimal parsing, explicit ROUND_HALF_UP, integer paise storage, float rejection, and largest-remainder allocation that never loses a paisa. |
| **AUD-H-008** transitions | Every approval checks current status first and returns 409 `INVALID_TRANSITION`. Revision creation no longer 500s (NULL effective_date is now legitimate for a Submitted revision). |
| **AUD-H-009** SQLite architecture | Partially mitigated: `BEGIN IMMEDIATE` serialises the critical section and the concurrency scenario is now prevented. **SQLite remains unsuitable for production.** A PostgreSQL port is designed, not delivered. The claim that the schema ports unchanged is withdrawn. |
| **AUD-H-010** no test suite | `tests/` with domain, API, authorisation, invariant, concurrency, migration and constraint coverage, runnable with one command. |

### Medium / Low

Addressed where they do not compete with control integrity: accessibility and 800px overflow
(AUD-M-003, M-004), deep-hierarchy RecursionError (AUD-M-005, fixed by iterative rollup), structured
error handling and correlation IDs (AUD-M-007), corrected claims in documentation (AUD-M-002),
repository under version control (AUD-L-001). Dependency locking with hashes and a completed
vulnerability scan (AUD-M-006) and executable contracts (AUD-M-001) remain open.

---

## Client decisions required

These are implemented with a **blocking default** as instructed. Each needs ratification.

| # | Decision | Default implemented | Why this default |
|---|---|---|---|
| D-1 | Should an approved PR reserve budget before a PO exists? | Reservation is **available but off** (`reserve_budget=false`). When on, the reservation is counted in exposure and settled exactly once. | The client document mandates a check at PR but defines commitment using open POs only. Counting both without a conversion event would double-count. |
| D-2 | Who may approve an over-budget exception? | `FinanceApprover` only, and never the requestor. | The audit showed the requestor self-approving. A stricter default is safe; loosening is a client choice. |
| D-3 | May capitalisation proceed with open commitment? | **No.** Hard block; the remainder must be written off with a reason. | Capitalising with unresolved commitment misstates asset value. |
| D-4 | Is a RETURN ever an increase? | **No.** CHECK constraint forbids it. | The audit found a positive RETURN raising the budget. |
| D-5 | Base currency and FX date/rate policy | Not implemented. FX exposure is **not** converted. | Requires a client policy on rate source and revaluation date; guessing would produce confidently wrong numbers. |

## Assumptions

1. The nine seeded users map to the seven roles as coded in `auth.DEV_USERS`. Real role assignment is a
   client decision.
2. `today()` governs revision effectivity; there is no separate accounting period or cut-off calendar.
3. The demo dataset is illustrative. No real Atha Group financial data has been loaded.
4. The development identity provider is not an enterprise IdP and is not proposed as one.

## Out of scope for this remediation

Live Zoho connectivity (no tenant), ERP/GL posting, PostgreSQL migration, enterprise SSO/MFA, opening
balance migration, SBOM and completed vulnerability scanning.
