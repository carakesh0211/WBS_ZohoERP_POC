# Role-Based UAT — CAPEX & WBS Control Hub

Wave 8, stream D. What each of the seven business roles can do, **what each one
cannot do**, and the automated evidence for both.

Everything below was measured against a running build at the commit that carries
this file. Where a claim could not be measured, it says so.

---

## 1. The two automated suites, and how to run them

| Suite | What it proves | Command | Real exit code observed |
|---|---|---|---|
| `tests/uat/uat_role_matrix.py` | The **API** admits every holder of a permission and refuses every non-holder, with `403` | `python tests/uat/uat_role_matrix.py --base http://127.0.0.1:8871` | **0** — 358 assertions passed |
| `tests/uat/uat_selftest.py` | The harness above is a **working gate** — it passes what it should and fails what it should | `python tests/uat/uat_selftest.py --base http://127.0.0.1:8871` | **0** |
| `tests/vrt/uat-roles.spec.js` | The **shipped page** never offers a screen a role may not use, and a denied deep link is corrected rather than rendered | `CAPEX_VRT_PORT=8871 node tools/run_vrt.mjs tests/vrt/uat-roles.spec.js` | **0** — 58 passed, 74 skipped |

Both Python files are named `uat_*`, not `test_*`, on purpose:
`tools/build_test_manifest.py` inventories `tests/**/test_*.py` and pins a
baseline of exactly **220** test functions. A `test_`-prefixed file anywhere
under `tests/` inflates that baseline and breaks the removal guard until it is
registered in that tool's `POST_BASELINE_FILES`. That tool belongs to another
stream, so this stream stayed out of its way. **The 220 baseline is unchanged by
this work.** If the lead wants these under pytest instead, see
§7 for the exact entries required.

The Playwright spec is recorded in `tests/vrt/evidence/vrt-inventory.json`
**by hand**. It adds no baseline PNG and no `-snapshots` directory; the 93
recorded baselines are untouched.

### Preconditions

```bash
python -m app.backend.migrate --db app/data/capex_uat.db --fresh --seed
CAPEX_PROFILE=local-demo CAPEX_DB_PATH=app/data/capex_uat.db PORT=8871 python app/run.py
```

**The restart is not optional.** `--fresh --seed` wipes `app_credential`, and the
demo identities are re-provisioned by `app/run.py` at start-up, not by the
migration. A UAT run against a freshly migrated database that was never restarted
fails at sign-in with `401`, and the harness says so and exits `2` rather than
reporting a green run over nine identities that do not exist.

---

## 2. The seven business roles, mapped to the identity that actually holds the grants

Read from `app/backend/auth.py`'s `DEV_USERS` and `PERMISSIONS`, not inferred
from the name — several names mislead.

| Business role | Identity | Roles held | Password |
|---|---|---|---|
| Requestor | `U-REQ` | Requestor | `U-REQ!demo` |
| Approver (purchase requests) | `U-PROC` | ProcurementApprover | `U-PROC!demo` |
| Procurement (PO lifecycle) | `U-PLH` | ProcurementApprover | `U-PLH!demo` |
| Finance | `U-FIN` | FinanceApprover | `U-FIN!demo` |
| Project Controller | `U-PM` | Requestor + BudgetController | `U-PM!demo` |
| Administrator | `U-ADM` | Administrator | `U-ADM!demo` |
| Auditor | `U-AUD` | Auditor | `U-AUD!demo` |
| *(also exercised)* | `U-PFC` | BudgetController + FinanceApprover | `U-PFC!demo` |
| *(also exercised)* | `U-CFO` | FinanceApprover + CapitalisationApprover | `U-CFO!demo` |

These are **seeded development credentials in a demo profile**. They are the only
credentials that appear anywhere in this release package. No production
credential exists in this build, and none may be added to it.

Two mappings that are easy to get wrong and were checked rather than assumed:

* `U-PLH` and `U-PROC` read like different jobs and carry **identical** grants.
  That is what makes them usable as maker and checker of the same object.
* `U-PM` is a **Requestor as well as** a BudgetController, so it can raise the
  purchase requests it also plans the budget for.

---

## 3. What each role CANNOT do

This is the half that matters. Every cell below was measured — a `403` was
actually returned by a running server, not derived from reading the table.

**Why `403` and not `503` is the assertion.** Most business surfaces are
PostgreSQL-backed and this POC is routinely run without PostgreSQL, where they
answer `503 DATABASE_NOT_CONFIGURED`. FastAPI resolves the permission dependency
*before* the handler touches a database, so:

* `403` — the permission gate **refused** this identity;
* `503` — the permission gate **admitted** this identity, and the request then
  died on the absent database.

A negative case that could not tell those apart would pass against a server with
no database at all.

| Permission | Held by | **Refused to** — the notable ones |
|---|---|---|
| `pr.create` | Requestor, BudgetController | **the Administrator**, Procurement, Finance, Auditor |
| `pr.approve` | ProcurementApprover | **the Administrator**, the Requestor who raised it, Finance, Auditor |
| `pr.approve_exception` | FinanceApprover | **Procurement** — the approver of an in-budget PR cannot approve an over-budget one |
| `po.amend` / `po.cancel` / `po.close` | ProcurementApprover | **the Administrator**, Finance, Auditor |
| `bill.void` | FinanceApprover | everyone else, incl. the Administrator |
| `period.transition` | BudgetController, FinanceApprover | **the Administrator** and the **Auditor** |
| `revision.approve` | FinanceApprover | Requestor, Procurement, Administrator, Auditor |
| `capitalisation.allocate` | BudgetController, FinanceApprover | Requestor, Procurement, Administrator, Auditor |
| `capitalisation.approve` | CapitalisationApprover | **eight of the nine identities** — only `U-CFO` holds it |
| `approval.read` | five roles | **the Auditor** |
| `approval.configure` | Administrator | everyone else |
| `approval.delegate` | the four approver roles | **the Administrator** and the **Requestor** |
| `audit.read` | Auditor, Administrator | everyone else, incl. Finance and the CFO |
| `connector.read` | Administrator, Auditor | Requestor, Procurement, Finance, Project Controller |
| `connector.manage` | Administrator | everyone else, incl. the Auditor who can *read* connectors |
| `reconciliation.triage` | **Administrator only** | **the Auditor** |
| `export.create` | six roles | **the Auditor** — creating an export job is a row INSERT |
| `report.view.share` | Administrator, BudgetController, FinanceApprover | Requestor, Procurement, **Auditor** |
| `settings.write` | Administrator | everyone else |
| `masters.write` | Administrator, BudgetController | everyone else |
| `settings.read` / `masters.read` | six roles | **the Auditor** |
| `admin.reset` | Administrator **and** the `local-demo` profile | eight of nine identities |

### The pair worth demonstrating

`POST /api/reports/views` with `visibility: "PRIVATE"` and with
`visibility: "SHARED"` — **same route, same body but one field, different
answer**:

| Identity | `PRIVATE` | `SHARED` |
|---|---|---|
| `U-REQ`, `U-PLH`, `U-PROC`, `U-AUD` | `503` (admitted) | **`403`** |
| `U-PM`, `U-FIN`, `U-PFC`, `U-CFO`, `U-ADM` | `503` (admitted) | `503` (admitted) |

A private saved view is a bookmark under the caller's own identity. A shared one
is a publication that other people open and read money through. The permission
model distinguishes them, and this is the cheapest way to show a client that it
does.

### Browser-side refusals

`tests/vrt/uat-roles.spec.js` deep-links each identity into seven gated screens
by real hash against the shipped `SCR_ROUTES` and router wiring. A denied screen
is **corrected to `#home` with `replaceState`, never rendered** — and the spec
presses Back afterwards, because a refusal you can reach with the Back button is
not a refusal.

The refusals chosen are the counter-intuitive ones:

* `#approval-delegations` refuses the **Administrator** — you cannot delegate an
  authority you do not hold, and the Administrator holds no approval authority.
* `#approval-inbox` and `#settings` refuse the **Auditor**.
* `#audit-trail` refuses **Finance** and the **CFO**.
* `#integration-setup` refuses the **Auditor**, who may read connectors but not
  manage them.

The spec also asserts the converse for every screen — a suite that only proved
refusals would be satisfied by an application that refused everything — and that
no navigation entry is ever offered to a role that `viewAllowed()` would refuse.

---

## 4. Business-flow coverage

| Flow | Scenario ids | Covered to |
|---|---|---|
| Budget planning and revisions | `UAT-BUD-01..05` | authorisation boundary (PostgreSQL) |
| PR reservation and approval | `UAT-PR-01..04` | **end to end** on the SQLite ledger; authorisation boundary on the PostgreSQL route |
| PR → PO, PO amend / cancel / close | `UAT-PO-01..03` | **end to end** on the SQLite ledger |
| Zoho outbound adapter | `UAT-INT-01` | **MOCK only** — see §5 |
| GRN and bill inbound (mock) | `UAT-GRN-01`, `UAT-BIL-01..02` | **end to end** on the SQLite ledger |
| Reconciliation and retry | `UAT-REC-01..04` | authorisation boundary |
| Reporting and export | `UAT-REP-01..03`, `UAT-EXP-01` | authorisation boundary |
| Project completion | `UAT-CLO-01..02` | authorisation boundary |
| Capitalisation and asset allocation | `UAT-CAP-01..02` | authorisation boundary |
| Closure and reopening | `UAT-CLO-01..02` | authorisation boundary |
| Permission negatives | every scenario, both directions | **end to end** |
| Duplicates, retries, outages, conflicts | `UAT-DUP-01`, `UAT-OUT-01`, `UAT-AUTH-01..06`, `UAT-ADM-02` | **end to end** |

**"Authorisation boundary" means what it says.** Without PostgreSQL configured,
those flows are proven to admit exactly the right identities and refuse exactly
the wrong ones, and then stop at `503 DATABASE_NOT_CONFIGURED`. The business
logic behind them is covered by the PostgreSQL pytest suites, not by this UAT.
Run this harness with `CAPEX_DB_URL` set and `--require-postgres` to execute
them end to end; the flag exists so a pipeline cannot silently grade the
shallower run as if it were the deeper one.

### Authentication and session negatives (all measured, all passing)

| Id | Assertion |
|---|---|
| `UAT-AUTH-01` | A wrong password is refused with `INVALID_CREDENTIALS` |
| `UAT-AUTH-02` | An unknown user and a wrong password are **indistinguishable** — no account enumeration |
| `UAT-AUTH-03` | An unauthenticated caller is refused everywhere |
| `UAT-AUTH-04` | A forged session identifier is refused |
| `UAT-AUTH-05` | A session stops working the moment it is signed out |
| `UAT-AUTH-06` | The acting identity comes from the session; naming someone else in the body changes nothing |
| `UAT-DUP-01` | Two identical logins produce two distinct sessions — no replayable credential |
| `UAT-ADM-02` | A demo reset revokes every session that survived it |
| `UAT-OUT-01` | An absent database is reported as **unavailable**, never as an empty result |

---

## 5. Zoho is MOCK

`app/backend/zoho.py` sets `MODE = "MOCK"`. `GET /api/health` reports
`"zoho_mode": "MOCK"` and `"integration is NOT VERIFIED"`. `GET /api/zoho/connections`
returns `"oauth_status": "Not Connected"`, `"status": "Disabled"`, and credential
fields carrying `secretref://kv/zoho/...` placeholders rather than values.

**No sandbox credential exists in this build. No live Zoho call has ever been
made by it.** `UAT-INT-01` asserts the server still says so, so a future build
that flipped to live without telling anyone fails here.

Nothing in this package is marked LIVE or VERIFIED for Zoho, and nothing may be.

---

## 6. Findings raised by this UAT

Four things this stream measured that were not previously written down. None is
fixed here — every one of them is in a file this stream does not own.

### F-01 — `services.approve_pr` checks existence and status *before* authorisation

`app/backend/services.py:178-186` reads the purchase-request row and validates
its status, and only then calls `auth.require`. Measured on the running server:

| Request | Caller | Answer |
|---|---|---|
| `PR-016` (Submitted) | `U-AUD` (Auditor) | `403 FORBIDDEN` ✔ |
| `PR-017` (Draft) | `U-AUD` (Auditor) | **`409 INVALID_TRANSITION` — "PR-2026-0017 is Draft"** |
| `PR-999` (absent) | `U-AUD` (Auditor) | **`404 PR_NOT_FOUND`** |

So an authenticated caller holding **no** approval permission can distinguish an
existing purchase request from an absent one, and learn its status, through an
endpoint it may not use. The ordering is defensible in itself — the required
permission depends on the row's `check_result`, so the row must be read first —
but the disclosure is real. The PostgreSQL route
`/api/procurement/purchase-requests/{id}/approve` does not have this shape: it
gates at the router with `_requires_any("pr.approve", "pr.approve_exception")`.

*Impact: low (authenticated callers only, no money moves). Owner: whoever owns
`app/backend/services.py`.*

### F-02 — maker-checker is structurally unreachable by any seeded identity

`auth.require_separation` is a genuine second line of defence and **no demo user
can reach it**, because the role model already separates the duties one layer
earlier. `pr.create` is held by Requestor and BudgetController; `pr.approve` by
ProcurementApprover alone. The sets are disjoint, so nobody who can raise a
request holds the permission to approve one, and `auth.require` refuses them one
line before `require_separation` is consulted. The same is true of
`revision.create`/`revision.approve` and
`capitalisation.allocate`/`capitalisation.approve`.

This is reassuring today and fragile tomorrow: D-12 (the client's role-to-permission
sign-off) is still open, and a mapping that grants one principal both permissions
moves the entire weight of segregation onto `require_separation`, which no live
path exercises. `UAT-SOD-04` asserts the disjointness so that change cannot land
silently. **A demonstrator cannot show maker-checker refusing a self-approval on
this dataset**; do not promise one.

### F-03 — `DEMO_USER` / `DEMO_PASSWORD` gate nothing

`DEPLOY.md:7` states the app "demands a username and password before a single
screen loads" when these are set, and `render.yaml:3` instructs the operator to
set them before sharing the URL. They are read at exactly one place —
`app/run.py`'s `main()` (the non-loopback bind warning) — inside a condition that **only prints a warning**. No
middleware, dependency or auth path in `app/backend/**` reads either variable.
Setting them changes nothing except suppressing the console warning.

The application does now have a real user model (`app/backend/auth.py`, sessions
+ PBKDF2), which `DEPLOY.md` predates and contradicts. But the seeded demo
passwords are published and derivable (`user_id + '!demo'`), so **a public URL is
protected by nothing an attacker could not guess.**

*Impact: high if anyone follows `DEPLOY.md` option 2 or 3. See
`docs/DEPLOYMENT_RUNBOOK.md` §6.*

### F-04 — the shipped deployment artifact cannot start

Verified by running the same code path:

```
$ CAPEX_DB_PATH=/tmp/does-not-exist.db python app/run.py
  REFUSING TO START: no database at /tmp/does-not-exist.db. Run:
  python -m app.backend.migrate --db /tmp/does-not-exist.db --fresh --seed
EXIT: 1
```

`Dockerfile` CMD is `python app/run.py` against an empty `/data` volume, and
`render.yaml` `startCommand` is the same against `/tmp/capex.db`. Neither runs a
migration first, and `app/run.py` deliberately no longer migrates itself (DEF-01).
Both therefore exit 1 on a fresh volume. `render.yaml:16` still carries the
comment "the demo dataset reseeds automatically on restart", which stopped being
true when DEF-01 was fixed.

Separately, `Dockerfile` copies `app/` (which contains the SQLite migrations) but
**not** `migrations/pg/`. **FIXED** — `Dockerfile` now copies `migrations`, and
the migrate-then-serve sequence is documented in the file itself.

*See `docs/DEPLOYMENT_RUNBOOK.md` §2 for the corrected start command.*

---

## 7. If the lead wants these under pytest

Adding `tests/uat/test_*.py` requires entries in
`tools/build_test_manifest.py::POST_BASELINE_FILES`, or the 220 baseline inflates
and `tests/test_manifest.py` fails. The manifest keys are
`path.relative_to(TESTS).as_posix()`, so a file in a subdirectory carries its
subdirectory:

```python
    # --- Wave 8 stream D: role-based UAT ---------------------------------
    # The 220 counts the POC's audit-remediation suite; letting a new file
    # inflate it makes the removal guard stop meaning anything.
    "uat/test_uat_role_matrix.py",
    "uat/test_uat_selftest.py",
```

No existing key in `tests/TEST_MANIFEST.json` has a subdirectory prefix today, so
this would be the first — worth a look before it lands. **This stream did not
edit `tools/**` and did not change the baseline.**
