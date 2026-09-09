# CAPEX & WBS Control Hub — proof of concept

Project-wise CAPEX and CWIP control for Atha Group: an independent application that monitors approved
budget against **both** committed cost (open purchase orders) and actual cost (vendor bills), so
available budget is visible *before* further procurement is approved.

> **Status: proof of concept, post-remediation. Not production software.**
> An independent quality audit on 2026-08-06 found the original build "technically functional but not
> reliable as a financial-control system" and recommended Production No-Go. Nine of the ten
> Critical findings have since been corrected and regression-tested. **AUD-C-007 is recorded as
> Partially fixed**, not closed — see `FINDINGS_REMEDIATION_STATUS.csv` for the finding-by-finding
> position, including what remains **unresolved**. This paragraph said "all ten" while that file
> said "Partially fixed"; the file is the record and this is now consistent with it.

## Run it

```bash
pip install -r requirements.txt
python -m app.backend.migrate --db app/data/capex_v2.db --fresh --seed
CAPEX_DB_PATH=app/data/capex_v2.db python app/run.py
```

Open <http://127.0.0.1:8000> and sign in. Demo identities are `U-REQ`, `U-PM`, `U-PLH`, `U-PROC`,
`U-FIN`, `U-PFC`, `U-CFO`, `U-AUD`, `U-ADM`; the password is the user id followed by `!demo`
(for example `U-ADM!demo`). These are seeded **development** credentials, not an identity provider.

Requires Python 3.11+. The database is a single SQLite file.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests build their own disposable database and never touch `app/data/`.

## The control logic

```
Exposure         = Open PO Commitment + Actual CWIP + live PR reservation
Available Budget = Current Approved Budget − Exposure
Utilisation %    = Exposure ÷ Current Approved Budget

Current Approved Budget = immutable Original Budget
                        + APPROVED and EFFECTIVE revisions only
```

Control is enforced at the **(WBS element, budget head) cell**, resolved to the nearest ancestor that
carries budget for that head. Four controls sit on top:

| Control | What it does |
|---|---|
| Anti-double-count | A PO line commits only its *unbilled* balance. Billing moves value from commitment to actual, never adding to both. For any PO, `billed + open commitment = ordered`. |
| Anti-under-count | Commitment is relieved only against billing; `received-not-billed` is tracked as its own exposure bucket so value cannot fall between the two. |
| Budget-head isolation | A depleted head cannot be bypassed by labelling a request with a different head. |
| Atomic enforcement | The availability check and the write happen in one locked transaction, so two concurrent approvals cannot consume the same rupee. |

Thresholds: warning at 80%, exception approval at 90%, hard stop when exposure exceeds approved budget.

## Security model

Actor identity is derived from a server-side session. **The request body cannot nominate who is
acting.** Seven roles — Requestor, BudgetController, ProcurementApprover, FinanceApprover,
CapitalisationApprover, Auditor, Administrator — hold a least-privilege permission set, and approvals
are subject to maker-checker: whoever raised something may never approve it.

Audit history is append-only (database triggers block UPDATE and DELETE) and SHA-256 hash-chained, so
tampering by a privileged identity is still detectable. `GET /api/audit/verify` reports chain
integrity. `POST /api/admin/reset` is refused unless `CAPEX_PROFILE=local-demo`.

## Zoho ERP integration — MOCK, and NOT VERIFIED

`app/backend/zoho.py` reads a 869-operation endpoint inventory generated mechanically from Zoho's own
published OpenAPI bundle (`openapi-all.zip`, sha256 `E95A0399…C447F8`, retrieved 2026-08-05), so the
connector cannot advertise an endpoint that does not exist.

**No live call is ever made.** There is no OAuth token lifecycle, HTTP transport, pagination, retry or
rate-limit handling. Every sync response carries `mode: MOCK` and `verified: false`.

> Earlier documentation claimed that going live was "a matter of supplying credentials". **That claim
> is withdrawn.** A live connector is a substantial build, not a configuration change.

Constraints the design already reflects, each evidenced from the specification:

| Finding | Effect |
|---|---|
| The ERP API is not uniformly `/erp/v3` — 14 of 78 modules are on `/erp/v1` | Base path resolves per module |
| Purchase Request has no API anywhere in the specification | PR is owned by this application |
| Purchase-receive lines carry no PO-line link and there is no list endpoint | Received-but-unbilled is reconstructed here. **Matching against live data is unresolved (AUD-H-004).** |
| Bill lines *do* carry `purchaseorder_item_id` | Commitment-to-actual matches at line level |
| No API creates a custom-field definition | Manual Zoho UI prerequisite before any live sync |
| Fixed assets expose no `last_modified_time` | Asset sync would be full-refresh only |

## Known limitations

- **SQLite is not suitable for production.** The demonstrated concurrency overspend is prevented by
  `BEGIN IMMEDIATE`, but there is no HA, no online backup story, and the earlier claim that the schema
  ports unchanged to PostgreSQL is **withdrawn** — the triggers and partial indexes need rewriting.
- **Foreign currency is not converted.** Exchange rates are stored but not applied; this needs a client
  policy on rate source and revaluation date.
- **No ERP or GL posting exists.** Capitalisation approval is a local status change and says so
  (`posting_status: NOT POSTED`).
- **External reconciliation is structural only** (AUD-H-005) — there is no external ledger to reconcile
  against without a tenant.
- Authentication is a development identity provider: no federation, MFA, password policy or lockout.
- Edge browser and screen-reader conformance remain **NOT VERIFIED**.

## Layout

```
app/backend/money.py       exact money (Decimal in, integer paise stored)
app/backend/domain.py      ledger, availability check, reconciliation
app/backend/services.py    guarded operations in locked transactions
app/backend/auth.py        sessions, roles, maker-checker
app/backend/main.py        API routes
app/backend/zoho.py        connector (MOCK)
app/backend/migrate.py     migration runner  ·  migrations/*.sql
app/frontend/              dense desktop UI, no build step
tests/                     regression suite
research/                  evidence base and frozen contracts
```

`research/30_contracts/C5_formulas.json` is the frozen formula registry the engine implements.

## Documents

| File | Contents |
|---|---|
| `CORRECTIVE_ACTION_PLAN.md` | Finding-by-finding approach, order, client decisions |
| `CORRECTION_IMPLEMENTATION_REPORT.md` | What changed, tests added, remaining limitations |
| `FINDINGS_REMEDIATION_STATUS.csv` | Per-finding status, evidence and residual risk |
| `DEPLOY.md` | How to demonstrate this to a client |
| `MIGRATIONS.md` | Schema versions, upgrade and rollback |
