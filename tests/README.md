# CAPEX & WBS Control Hub — automated regression suite

Remediates audit finding **AUD-H-010** ("No automated product test suite or CI
evidence"). Every test name carries the finding it proves, so a failure points
straight at the control it protects.

---

## Run everything — one command

```bash
python -m pytest
```

Run it from the repository root (`WBS_ZohoERP_POC/`). `pytest.ini` supplies
`testpaths`, `pythonpath` and the marker registry, so no flags are needed.

First time only:

```bash
python -m pip install -r requirements-dev.txt
```

### Useful variants

| Command | What it does |
| --- | --- |
| `python -m pytest` | the whole suite |
| `python -m pytest -m "not product_defect"` | skip the tests that prove open product defects (see below) |
| `python -m pytest -m "not slow"` | skip the deep-hierarchy and threaded-contention tests |
| `python -m pytest tests/test_financial_controls.py -k aud_c_001` | one finding |
| `python -m pytest -v` | one line per test, with the finding id in the name |

Expect roughly four minutes for a full run on a laptop. Most of that is
PBKDF2: every authenticated fixture performs a real 240,000-round password
verification, which is deliberate — the suite exercises the shipped credential
path rather than a weakened one.

---

## What the suite guarantees about your data

**It never touches `app/data/capex.db` or `app/data/capex_v2.db`.**

Three independent guards enforce that:

1. A session-scoped autouse fixture repoints `app.backend.db.DB_PATH` at a
   scratch file before any test runs, so a test that forgets to request a
   database fails loudly instead of opening the real one.
2. `_assert_disposable()` refuses any path whose parent is `app/data`.
3. An autouse per-test check re-asserts the same thing after every test.

### How a test database is built

| Step | Where |
| --- | --- |
| `python -m app.backend.migrate --db <tmp> --fresh --seed` (a real subprocess) | once per session |
| `auth.provision_dev_identities(con)` | once per session |
| byte-copy of that template into the test's own `tmp_path` | once per test |

Every test therefore starts from an identical, fully migrated, fully seeded
database and can mutate it freely. Tests are independent and re-runnable in any
order; `pytest -p xdist` style sharding is safe because nothing is shared but
the read-only template.

### Authenticated callers

Fixtures return an authenticated client per role. The password is always the
user id plus `!demo`, and the session id travels in the `X-Session` header.

| Fixture | User | Roles |
| --- | --- | --- |
| `requestor` | `U-REQ` | Requestor |
| `procurement` | `U-PLH` | ProcurementApprover |
| `finance` | `U-FIN` | FinanceApprover |
| `capitalisation` | `U-CFO` | FinanceApprover + CapitalisationApprover |
| `auditor` | `U-AUD` | Auditor |
| `admin` | `U-ADM` | Administrator |
| `controller` | `U-PFC` | BudgetController + FinanceApprover — the one identity able to *attempt* self-approval |
| `make_user([...])` | synthetic | exactly the roles you name |

Supporting fixtures: `client` (unauthenticated `TestClient`), `raw_con` (direct
SQLite connection for constraint assertions), `ledger`, `cell`, `reconciliation`.

---

## Module map

| Module | Findings covered |
| --- | --- |
| `test_money.py` | AUD-H-007 — half-up rounding, mantissa exhaustion, float rejection, `split_pro_rata` conservation |
| `test_domain_invariants.py` | the frozen formulas, AUD-C-005 budget governance and immutability, AUD-H-001 reservations, AUD-M-005 deep hierarchies |
| `test_api_auth.py` | AUD-C-006 — 401/403 matrix over **every** mutating route, session handling, maker-checker |
| `test_financial_controls.py` | AUD-C-001 atomic amendment and concurrency, AUD-C-002 budget head as a control dimension, AUD-C-003 relationship integrity, AUD-C-004 bill lifecycle, AUD-C-007 idempotency, AUD-C-008 lifecycle gating |
| `test_capitalisation.py` | AUD-C-009 — eligibility gate, allocation validity, allocation-equals-CWIP, duplicate approval |
| `test_audit.py` | AUD-C-010 — append-only storage, hash chain, `/api/admin/reset` gating |
| `test_connector.py` | AUD-H-003 connector gating, AUD-H-002 honesty about MOCK mode |
| `test_migrations.py` | `--fresh --seed` reaches the latest version, migrations are idempotent, `PRAGMA foreign_key_check` is clean, every control table/trigger exists |

`test_api_auth.py::test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
compares the authorisation matrix with the live FastAPI route list. A new
mutating endpoint cannot be merged without an authorisation test.

---

## Known failures — open product defects

These tests fail on purpose. They are **not** to be weakened or deleted; they
are the evidence that the defect is still open. Deselect them with
`-m "not product_defect"` if you need a green board while the fixes are queued.

### 1. Deep WBS crashes the tree endpoint (AUD-M-005, partially unremediated)

`test_domain_invariants.py::test_aud_m_005_deep_wbs_tree_endpoint_answers_without_a_500`

`domain.compute_ledger` was made iterative, but `main.wbs_tree` still serialises
the tree with the recursive helper `strip()` (`app/backend/main.py:210-229`). A
1,200-level hierarchy raises `RecursionError` inside the route and the caller
gets an uncontrolled **HTTP 500** with no error envelope.
*Fix:* make `strip()` iterative, or bound the depth and return a controlled 422.

### 2. `/api/admin/reset` 500s and downgrades the schema (AUD-C-010)

`test_audit.py::test_aud_c_010_reset_under_the_local_demo_profile_succeeds`

With `CAPEX_PROFILE=local-demo` the gate opens and the route calls
`db.reset_and_seed()`, which rebuilds the file from `db.SCHEMA` — **migration 001
only**. Everything migration 002 created is destroyed: `app_credential`,
`user_role`, `app_session`, `pr_reservation`, `lifecycle_state`,
`idempotency_key`, the `budget_line` CHECK constraints and the audit
append-only triggers. `auth.provision_dev_identities` then fails with
`sqlite3.OperationalError: no such table: app_credential` and the caller gets an
uncontrolled **HTTP 500**. The documented recovery path therefore both fails and
silently removes the audit immutability AUD-C-010 exists to guarantee.
*Fix:* run `migrate.fresh(db.DB_PATH, seed=True)` instead of
`db.reset_and_seed()`, then provision identities.

### 3. A non-finite amount escapes as a 500 (AUD-H-007 / AUD-H-008)

`test_money.py::test_aud_h_007_infinite_float_amount_raises_a_controlled_money_error`

`money.to_paise` quantises an incoming float (`app/backend/money.py:45`) **before**
the `is_finite()` guard (`app/backend/money.py:58`), so `Infinity` escapes as
`decimal.InvalidOperation`. No handler maps it, so `{"amount_rupees": Infinity}`
answers **HTTP 500** where `NaN` correctly answers 422 `INVALID_AMOUNT`.
*Fix:* move the `is_finite()` check ahead of the float quantisation.

---

## Reference figures

The seeded dataset is fixed, so assertions can be exact. All figures are
integer paise.

| Control cell | Budget | Available |
| --- | --- | --- |
| `W-03` / `BH-PM` (Plant & Machinery) | 900,000,000 | 191,000,000 |
| `W-02` / `BH-CIVIL` (Civil) | 250,000,000 | 110,000,000 |
| `W-04` / `BH-ELEC` (Electrical) | 230,000,000 | 140,000,000 |
| `W-05` / `BH-INST` (Installation) | 120,000,000 | 44,000,000 |
| `PRJ-02` CWIP once its POs are closed | — | 168,000,000 actual |

`POL-000n` is the *n*-th purchase-order line in seed order; `PO-006` is the only
multi-line order (`POL-0006`, `POL-0007`).
