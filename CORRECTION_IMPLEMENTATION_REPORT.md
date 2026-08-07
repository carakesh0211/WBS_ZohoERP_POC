# Correction Implementation Report

**Against:** independent quality audit, cutoff 2026-08-06 11:46 IST (47 scenarios, 3 PASS / 44 FAIL)
**Audit opinion:** "POC is technically functional but not reliable as a financial-control system."
**Audit recommendation:** Production No-Go

---

## 1. Baseline

Current source was **byte-identical** to `audit_output/current_snapshot` when work began — verified by
diff across `db.py`, `domain.py`, `main.py`, `zoho.py` and `app.js` (0 differing lines each). There
were therefore **no third-party changes to preserve**. The one change already present at the cutoff was
the optional shared Basic-auth gate, which the audit correctly rejected as insufficient (AUD-C-006); it
has been replaced entirely.

The original demo database `app/data/capex.db` was **not altered**. All work targets a new
`app/data/capex_v2.db` built through the migration runner.

---

## 2. Files changed

### New

| File | Purpose |
|---|---|
| `app/backend/money.py` | Exact money: Decimal in, integer paise stored, explicit ROUND_HALF_UP |
| `app/backend/auth.py` | Sessions, 7 roles, permission matrix, maker-checker |
| `app/backend/services.py` | Guarded operations inside locked transactions; audit hash chain; idempotency |
| `app/backend/migrate.py` | Migration runner with backup, status and fresh-build modes |
| `app/backend/migrations/002_financial_controls.sql` | Constraints, triggers, control tables |
| `tests/` | Regression suite |
| `CORRECTIVE_ACTION_PLAN.md`, `FINDINGS_REMEDIATION_STATUS.csv`, `MIGRATIONS.md` | Deliverables |

### Rewritten

| File | Change |
|---|---|
| `app/backend/domain.py` | Ledger re-keyed on the (WBS, budget head) control cell; approved+effective budget only; accounting-effective bills only; lifecycle-derived gates; iterative rollup |
| `app/backend/main.py` | Thin routes; session identity; per-route permissions; controlled 4xx; iterative WBS serialiser; security headers |
| `app/frontend/app.js`, `index.html`, `styles.css` | Sign-in, session header, accessibility, responsive layout, MOCK labelling |

### Modified

| File | Change |
|---|---|
| `app/backend/zoho.py` | Connection state and scope are enforcement gates; idempotent sync; MODE_NOTE and `verified:false` on every response |
| `app/backend/db.py` | `DB_PATH` overridable via `CAPEX_DB_PATH` |
| `README.md`, `DEPLOY.md` | Overstated claims withdrawn |

---

## 3. Schema migrations

Version 002, `financial_controls`. Full detail in `MIGRATIONS.md`. Summary:

- **Rebuilt** `budget_line` with `status`, `effective_date`, `approved_by`, a CHECK constraining sign by
  kind, and a CHECK requiring approved lines to carry authorisation evidence.
- **9 new tables**: `app_role`, `user_role`, `app_credential`, `app_session`, `idempotency_key`,
  `external_document`, `pr_reservation`, `reconciliation_exception`, `lifecycle_state`.
- **16 triggers** enforcing ownership, immutability, append-only audit, cycle prevention and value ranges.
- **3 unique indexes** for external-document identity and one-live-reservation-per-PR.

`PRAGMA foreign_key_check` is clean after migration. Rollback is by restoring the automatic
`.pre-002.bak` copy; there is deliberately no down-migration, because reversing it would silently
discard the columns the controls depend on.

A **data-quality defect** surfaced during migration and is worth recording: `budget_revision.approver`
stored a free-text display name with no foreign key. That is a structural reason maker-checker could
not be enforced — there was no typed identity to compare against.

---

## 4. Rules implemented

### Financial invariants now enforced

| # | Invariant | Where |
|---|---|---|
| 1 | Available = Approved Budget − Actual − Open Commitment − live PR reservation | `domain._derive` |
| 2 | Current Approved Budget = immutable original + approved **and effective** revisions | `domain.compute_ledger`, CHECK constraints |
| 3 | Draft/Submitted/Rejected/future-effective revisions affect nothing | ledger filter on `status` + `effective_date` |
| 4 | RETURN never increases budget | CHECK on `budget_line` |
| 5 | Control enforced at the validated (project, WBS, head) tuple | `domain.budget_check`, triggers |
| 6 | Negative amounts rejected outside an explicit reversal path | `money.to_paise`, triggers |
| 7 | Amendments revalidate only the increment, atomically | `services.amend_po` |
| 8 | Two transactions cannot consume the same availability | `services.critical` (BEGIN IMMEDIATE) |
| 9 | A bill relieves commitment only for the PO line it validly references | trigger + ledger join |
| 10 | Bill actuals post to the validated project/WBS/head | `bill_line_po_ownership_*` triggers |
| 11 | Voided/reversed bills leave accounting-effective actuals | `accounting_status` filter |
| 12 | External documents and integration events are idempotent | `idempotency_key`, unique indexes |
| 13 | PR reservations release or convert exactly once | `pr_reservation` + partial unique index |
| 14 | Closed/capitalised structures block procurement | `lifecycle_state` |
| 15 | Capitalisation blocked while commitments or exceptions remain | `services.approve_capitalisation` |
| 16 | Requestors cannot approve their own items | `auth.require_separation` |
| 17 | Audit history is not editable or deletable | append-only triggers + hash chain |

### Security model

Seven server-side roles — Requestor, BudgetController, ProcurementApprover, FinanceApprover,
CapitalisationApprover, Auditor, Administrator — with a least-privilege permission matrix
(`auth.PERMISSIONS`) and maker-checker on all approval permissions (`auth.MAKER_CHECKER`).

Identity comes from a server-side session token; **the request body can no longer nominate an actor**,
which was the specific mechanism the audit used to self-approve. Audit read access is separated from
operational mutation access. Destructive reset requires the Administrator role *and*
`CAPEX_PROFILE=local-demo`.

This is a **development identity provider behind a stable interface**. It is not an enterprise IdP:
no federation, MFA, password policy, lockout, or user lifecycle management. Those remain production
work.

---

## 5. Verification performed

Each audit reproduction was replayed against the corrected build.

| Audit test | Before | After |
|---|---|---|
| API-001 / SEC-001 | anonymous and arbitrary bearer accepted | HTTP 401 both |
| SEC-003 / SEC-004 | requestor self-approved own PR | HTTP 403 FORBIDDEN; independent approver 200 |
| SEC-005 | revision self-approval succeeded | HTTP 403 |
| SEC-006 | anonymous reset reseeded the database | 401 anonymous, 403 RESET_DISABLED authenticated |
| WF-001 / WF-002 | duplicate approvals accepted | HTTP 409 INVALID_TRANSITION |
| FIN-001 | negative proposal accepted | NEGATIVE_AMOUNT |
| FIN-002 | Civil head cleared Plant availability | NO_BUDGET_FOR_HEAD |
| FIN-004 / FIN-005 | over-budget amendment persisted | HTTP 409, line unchanged (340000000 before and after) |
| CON-001 | both concurrent amendments succeeded, exposure ₹100.9L vs ₹90L budget | exactly 1 of 2 succeeded; availability ₹7,64,000 ≥ 0 |
| REV-001 | HTTP 500 | HTTP 201 |
| CAP-001 / CAP-002 | negative allocation and reasonless write-off accepted | 422 INVALID_AMOUNT / 422 REASON_REQUIRED |
| CAP-003 / CAP-004 | capitalised with ₹60.5L open commitment; repeatable | 409 CAPITALISATION_BLOCKED; duplicate 409 |
| INT-001 / INT-003 / INT-004 | synthetic PASS while disconnected | 409 NOT_CONNECTED / 404 MODULE_UNKNOWN / 404 CONNECTION_NOT_FOUND |
| DOM-004 | void ignored | actual ₹52L → ₹34.6L, commitment ₹66.96L → ₹84.36L |
| DOM-010 | Draft project accepted spend | PROJECT_STATE |
| DOM-013 | `0.005`→0, large values lost precision | `0.005`→1, `90071992547409.93`→9007199254740993 |
| SCHEMA-001..006 | all six accepted | all six blocked, plus six more |

### Test suite

```bash
pip install -r requirements-dev.txt
python -m pytest
```

**348 tests, 348 passed, 0 failed.**

| Module | Tests | Covers |
|---|---|---|
| `test_api_auth.py` | 78 | 401/403 on every mutating route, roles, maker-checker, sessions |
| `test_financial_controls.py` | 73 | amendment atomicity, concurrency, budget head, relationships, bills |
| `test_money.py` | 67 | Decimal conversion, rounding, float rejection, pro-rata allocation |
| `test_domain_invariants.py` | 40 | the 17 financial invariants, deep-hierarchy robustness |
| `test_audit.py` | 31 | append-only enforcement, hash chain, reset gating |
| `test_connector.py` | 20 | connection/scope gates, unknown module, idempotent sync |
| `test_migrations.py` | 20 | fresh build, idempotency, foreign-key integrity |
| `test_capitalisation.py` | 19 | eligibility gates, allocation totals, duplicate approval |

Every test builds its own disposable database; none touches `app/data/`.

**Three defects in the corrections themselves were found by the new tests and fixed:**

1. `to_paise(float('inf'))` raised `decimal.InvalidOperation` instead of a controlled `MoneyError` —
   the finiteness check ran after the float branch.
2. The WBS tree endpoint still returned 500 on a deep hierarchy. The ledger rollup had been made
   iterative, but the **route's serialiser was still recursive**. It is now iterative with an explicit
   200-level bound returning a controlled 422.

3. `POST /api/admin/reset` returned 500 under the local-demo profile. It rebuilt only the **v1**
   schema via `db.reset_and_seed()`, so the control tables added by migration 002 vanished and
   identity provisioning then failed. Reset now rebuilds through the migration runner and reports the
   resulting `schema_version`.

A fourth was found by the frontend work: the `style-src 'self'` CSP header added during this
remediation blocked every inline `style=` attribute the UI relied on for exposure bars and tree
indentation. Static styling moved to classes; computed geometry is set through the CSSOM.

That four defects were introduced by the corrections and caught by the new tests is itself the
argument for AUD-H-010: without a suite, each would have shipped silently.

---

## 6. Remaining limitations

**Unresolved, and material:**

- **AUD-H-004** — Zoho purchase-receive lines carry no documented PO-line link. Received-not-billed
  attribution against live data is unproven. `reconciliation_exception` is in place as the quarantine
  landing table; the matching algorithm needs a live tenant to validate against.
- **AUD-H-005** — no external subledger/GL reconciliation. Local arithmetic is internally consistent
  but is not proven against any external ledger.
- **AUD-H-002** — the Zoho connector is **MOCK**. No OAuth lifecycle, HTTP transport, pagination, retry
  or rate-limit handling. Live integration is **NOT VERIFIED**.
- **AUD-H-009** — SQLite is not production-safe. The demonstrated race is prevented, but the claim
  that the schema ports unchanged to PostgreSQL is **withdrawn**.
- **Foreign currency is not converted.** Exchange rates are stored, not applied (client decision D-5).
- **No ERP/GL posting.** Capitalisation approval is a local status change and reports
  `posting_status: NOT POSTED`.

**Partially addressed:** idempotency is proven for API replay but not for a real webhook stream
(AUD-C-007); PR reservation is implemented but **off by default** pending client decision D-1;
dependencies are pinned but not hash-locked and the vulnerability scan is **NOT VERIFIED** (AUD-M-006).

**Not addressed:** executable contract governance (AUD-M-001); encoding artefacts in generated research
text (AUD-L-002); Edge browser and certified screen-reader/contrast conformance remain **NOT VERIFIED**.

**Environment blocker:** git commits fail machine-wide — global config sets `commit.gpgsign=true` with
`user.signingkey=YOUR_GPG_KEY_ID`, an unreplaced placeholder, and no secret key exists (AUD-L-001).

---

## 7. Production-readiness reassessment

| Context | Recommendation | Basis |
|---|---|---|
| Internal demonstration | **Go** | Runs, all 17 screens work, controls demonstrably enforce |
| Client walkthrough | **Go** | Behaviour is honest: refusals are visible, MOCK is labelled, limitations documented |
| Controlled pilot | **Conditional Go** | Conditions: PostgreSQL port with the critical section re-expressed as `SELECT FOR UPDATE`; enterprise identity; client ratification of decisions D-1 to D-5; opening-balance migration |
| Live Zoho connection | **No-Go** | No live adapter exists. AUD-H-002 and AUD-H-004 are unresolved |
| Production | **No-Go** | Depends on all pilot conditions plus AUD-H-005 reconciliation, observability, backup/restore and DR |
| Financial reliance | **No-Go** | Requires everything above plus an external audit of the corrected controls. This build has not been audited since remediation |

The audit's original **Production No-Go stands for production and financial reliance.** What has changed
is that the *control design* is now defensible: the specific defects that made the system unreliable —
overspend on amendment, concurrency, cosmetic budget heads, self-approval, mutable audit history — are
corrected and regression-tested. The remaining barriers are architectural and integration work, not
control-logic defects.

---

## 8. Recommended next steps

1. Ratify client decisions D-1 to D-5 (`CORRECTIVE_ACTION_PLAN.md` §"Client decisions required").
2. Resolve git signing so the remediation history is committed and reviewable.
3. Port to PostgreSQL and re-run the full constraint and concurrency suite against it.
4. Integrate enterprise identity behind the existing `auth` interface.
5. Obtain a Zoho sandbox tenant; only then build and certify the live adapter.
6. Commission an independent re-audit of the corrected build before any financial reliance.
