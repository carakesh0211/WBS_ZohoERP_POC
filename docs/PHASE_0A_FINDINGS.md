# Phase 0A Findings

**Project:** CAPEX & WBS Control Hub
**Phase:** 0A — work possible with no Zoho tenant (contract validation, supply chain, observability, test-inventory manifest, visual-regression baselines, provenance backfill)
**Plan reference:** `prompt-1-claude-elegant-milner.md`, v1.2.1
**Branch:** `remediation/audit-2026-08`
**Date:** 2026-08-28

This document records what Phase 0A found. Every entry below was reproduced in this working tree. Nothing here is projected, estimated, or inferred from documentation alone; where a claim could not be proven in this environment it is recorded as unproven in the closing section rather than asserted.

---

## 1. Summary

| ID | Title | Severity | Status | Owner | Phase to resolve |
|---|---|---|---|---|---|
| DEF-01 | Legacy database cannot be upgraded; application fails to boot | High | Open, reproduced, guarded by strict-xfail test | Engineering | Phase 1 (PostgreSQL port) |
| GAP-01 | `Cancelled` is a runtime status absent from the frozen 21 business statuses | High | Open, needs client agreement | Client / Business analysis | Phase 1, after C3 amendment |
| GAP-02 | Nine off-token colours in `styles.css` outside `:root` | Medium | Open, allowlisted; fix is to complete the token registry | Design / Engineering | Phase 1 |
| GAP-03 | `C6_tokens.json` declares `dark_mode`; `styles.css` does not implement `prefers-color-scheme` | Medium | Open | Design / Engineering | Phase 1 |
| GAP-04 | Business roles (13) and technical roles (7) unreconciled | High | Open, blocking decision D-12 | Client / Engineering | Phase 3 (gated) |
| GAP-05 | 107 CLIENT-PDF requirements carry empty traceability | High | Open by design, ratcheted | Business analysis | Phase 1 onward |
| OBS-01 | Namespace collision between circuit-breaker `CLOSED` and business status `CLOSED` | Medium | Resolved in Phase 0A | Engineering | Closed |
| OBS-02 | Test count reconciliation: 220 functions vs 348 collected cases | Informational | Resolved — both figures documented | Engineering | Closed |
| OBS-03 | Environment limitations (no `pip` locally) | Medium | **Resolved.** Supply-chain closure completed in CI; **AppSail FastAPI proof completed on a live deployment** | Engineering / Platform | Closed |

Severity is assessed against a production deployment of this system, not against the POC as currently demonstrated.

---

## 2. DEF-01 — Legacy database cannot be upgraded; application fails to boot

**Severity:** High, for any deployment that carries an existing database file.
**Status:** Open. Reproduced. Recorded as a strict-xfail regression test. Not fixed in Phase 0A.

### Description

`app/backend/migrate.py::upgrade()` replays migration 001 against a database that already carries the v1 schema. Migration 001's `_apply` executes `con.executescript(dbmod.SCHEMA)` (`app/backend/migrate.py` line 74), which fails against an existing schema with:

```
sqlite3.OperationalError: table entity already exists
```

The failure is not confined to the migration runner. `app/run.py` line 32 calls:

```python
migrate.upgrade(db.DB_PATH, backup=False)
```

unconditionally whenever the database file exists. A pre-migration-runner database file therefore makes the whole application fail to start, not merely fail to migrate.

### Reproduction

1. Create a database using the legacy path only — `con.executescript(dbmod.SCHEMA)`. The result is 21 tables and **no** `schema_migration` table, which is exactly the shape of a database created before the migration runner existed.
2. Call `migrate.upgrade()` against that file.
3. Observe `sqlite3.OperationalError: table entity already exists`.

### Control case

The same reproduction was run against a database built through the migration runner:

1. `migrate.fresh(seed=True)`
2. `migrate.upgrade()`

This passes, and is idempotent — the ledger ends with 001 and 002 recorded, and repeated calls do not fail. The defect is therefore specific to **adopting an existing schema**, not a general fault in the migration runner.

### Relationship to commit ce7f3c5

Commit `ce7f3c5` ("fix: initialize current schema on hosted startup") addressed a hosted instance with **no** database. It does not cover a hosted instance with an **old** database. The two cases are distinct and only the first is currently handled.

### Guard in place

The defect is recorded as:

```
tests/test_known_defects.py::test_def_01_a_legacy_v1_database_can_be_upgraded
```

marked `@pytest.mark.xfail(strict=True)` plus the `product_defect` marker. The test asserts the **desired** behaviour, so it reports XFAIL today and becomes a hard FAILURE the moment someone fixes the defect. That forces deliberate removal of the marker rather than a silent, undocumented fix. The passing control case is recorded alongside it as `test_def_01_control_a_fresh_database_upgrades_cleanly`.

### Recommendation

Add a baseline/adopt step to the migration runner: when the ledger is empty but the schema is present, verify that the existing schema matches migration 001 and record 001 as applied, rather than replaying it. Migration 001 must not be re-executed against a database it has effectively already produced.

This is deferred to Phase 1 deliberately. Migration-runner changes belong with the PostgreSQL port, and making them twice — once for SQLite in Phase 0A and again for PostgreSQL in Phase 1 — would produce two migrations of the migration mechanism itself.

---

## 3. GAP-01 — `Cancelled` is a runtime status absent from the frozen 21 business statuses

**Severity:** High.
**Status:** Open. Requires client agreement, because C3 is frozen.

### Description

`purchase_order.status` takes the value `'Cancelled'` at runtime. `app/backend/domain.py` line 47 reads it:

```python
COMMITMENT_RELEASING_STATES = {"Cancelled", "Closed"}
```

This set is used in commitment calculations, so `Cancelled` is financially load-bearing: it determines whether an open commitment is released.

`C3_statuses.json` freezes exactly 21 status codes. It contains `CLOSED`. It does not contain `CANCELLED`.

### Why the two cannot simply be merged

`Closed` and `Cancelled` are different business events:

- **Closed** — the purchase order reached its end state after partial or full fulfilment, and any residual commitment is released.
- **Cancelled** — the purchase order was abandoned.

Open-PO ageing reporting must distinguish the two. Collapsing `Cancelled` into `Closed` would make abandoned commitments indistinguishable from fulfilled ones in ageing analysis.

### Recommendation

Raise with the client as an amendment to the frozen C3 register: add `CANCELLED` as a 22nd status code with an explicit definition separating it from `CLOSED`. Do not resolve this unilaterally in code — the register is frozen and the distinction is a business decision.

Full detail is already recorded in `CONTRACT_GAPS.md` (repository root).

---

## 4. GAP-02 — Nine off-token colours in `styles.css` outside `:root`

**Severity:** Medium.
**Status:** Open. Allowlisted in the contract test. Fix is to complete the token registry, not to change the stylesheet.

### What is clean

The `:root` block of `styles.css` matches `C6_tokens.json` **exactly — 25 of 25 hex values**. That gate is clean and is asserted strictly.

### What is not

Nine further hex values appear elsewhere in `styles.css`, outside `:root`, and are absent from the token registry. They are message-strip text and border tones plus one chart-bar breach tone:

```
#7D1A15  #F0C4C1
#6D4600  #ECD6A8
#145232  #BFE0CC
#1F3F7A  #C5D4F0
#7A1A15
```

### Constraint

`styles.css` is the client-approved visual baseline and must **not** change. The correct remedy is therefore to complete `C6_tokens.json` so that the registry describes the approved stylesheet, rather than to alter the stylesheet so that it matches an incomplete registry.

### Guard in place

The nine values are allowlisted in the contract test. The allowlist is explicit and finite, so a **new** off-token colour introduced by future work still fails the gate. The allowlist records a known, bounded debt; it does not disable the check.

### Recommendation

Add the nine values to `C6_tokens.json` as named tokens in Phase 1, with the allowlist removed in the same change so that the two cannot drift apart.

---

## 5. GAP-03 — Declared dark mode is not implemented

**Severity:** Medium.
**Status:** Open.

`C6_tokens.json` declares `dark_mode`. `styles.css` implements only:

```css
@media (forced-colors: active)
```

There is no `prefers-color-scheme` implementation. The contract asserts a capability the stylesheet does not provide.

### Recommendation

Either implement `prefers-color-scheme` against the token set, or amend `C6_tokens.json` to withdraw the `dark_mode` declaration. Both routes require client sign-off because the token file is a contract artefact and the stylesheet is the approved baseline. Do not resolve by silently dropping the declaration.

---

## 6. GAP-04 — Business roles and technical roles are unreconciled

**Severity:** High.
**Status:** Open. Blocking decision D-12. Gates Phase 3.

### Description

Two role registries exist and have not been reconciled:

- **Business roles** — 13 roles in `C9_roles.json`.
- **Technical roles** — 7 roles in `app/backend/auth.py` (`ROLES`, line 29).

Only **"Requestor"** appears in both.

This matters because `C8_screens.json` requires every screen's "Permission requirements" to name C9 roles only. With the two registries unreconciled, screen permission requirements cannot be validated against the roles the application actually enforces, and any mapping written today would be a guess.

### Recommendation

Resolve decision D-12 with the client before Phase 3 begins: produce an explicit, agreed mapping from the 13 business roles to the 7 technical roles, including which business roles collapse onto a shared technical role and which require a new one. Phase 3 must not start against an assumed mapping.

---

## 7. GAP-05 — Traceability coverage

**Severity:** High.
**Status:** Open by design. Ratcheted.

### What was done

107 CLIENT-PDF requirements were migrated from `C1_req_ids.json` into `C14_traceability.json` carrying their real metadata, but with **empty** `screens`, `tables`, `services` and `tests` fields and `traceability_status: "PENDING"`.

The empty fields are deliberate. A plausible-looking but wrong traceability row is worse than an obviously empty one: it silently reports coverage that does not exist, and it does so in exactly the artefact a reviewer would consult to check coverage. Rows were therefore not invented.

### Current totals

| Source | Count | Status |
|---|---|---|
| CLIENT-PDF | 107 | Pending |
| CLIENT-NOTES-2026-08-28 | 13 | Traced |
| PRODUCT-OWNER-REQUEST-2026-08-28 | 2 | Traced |
| **Total** | **122** | — |

### Guard in place

A ratchet test asserts that the pending count never rises. New requirements may be added, and pending requirements may be traced, but the backlog of untraced requirements cannot grow silently.

### Recommendation

Trace the 107 CLIENT-PDF requirements incrementally from Phase 1 onward, each against the screens, tables, services and tests that actually implement it. The ratchet makes progress monotonic; it does not make it automatic.

---

## 8. OBS-01 — Namespace collision caught by the new contract gate on its first run

**Severity:** Medium.
**Status:** Resolved in Phase 0A.

### Description

The circuit-breaker state `CLOSED` collided with the C3 business status `CLOSED`. The two mean opposite things:

- A **closed circuit** is healthy and passing traffic.
- A **closed project** is terminal and blocks posting.

A single string carrying both meanings across the observability and business layers is a defect waiting to happen — the failure mode is a healthy-state string being read as a terminal business state, or the reverse.

### How it was caught

`tests/test_contracts.py::test_integration_statuses_are_disjoint_from_business_statuses` caught it on its first run.

### Resolution

The circuit-breaker states were renamed to `CIRCUIT_CLOSED`, `CIRCUIT_OPEN` and `CIRCUIT_HALF_OPEN`.

### Why this is recorded

This is evidence that separating the status registries and asserting their disjointness earns its keep. The gate found a real collision immediately on introduction, in code that was already written and passing its own tests.

---

## 9. OBS-02 — Test count reconciliation

**Severity:** Informational.
**Status:** Resolved. Both figures are documented.

The suite contains **220 test functions** but collects **348 cases**. Parametrisation in 6 files expands the difference. Both numbers are real and neither is wrong; earlier plan drafts referred to "220 tests" ambiguously without stating which was meant.

**The Definition of Done refers to the function count.**

| Measurement | Result |
|---|---|
| Baseline run (SQLite) | 348 passed in ~189s |
| After Phase 0A additions | 433 passed in ~224s |

### Recommendation

Quote both figures wherever suite size is reported, and state which one a target refers to. A target expressed against the collected-case count and measured against the function count (or the reverse) will silently drift.

---

## 10. OBS-03 — Environment limitations encountered

**Severity:** Medium.
**Status:** **Resolved.** Supply-chain closure completed in CI; the AppSail FastAPI proof was completed on a live Catalyst deployment. See `PHASE_0A_EXIT.md` §8.

### 10.1 No `pip` in the active interpreter

The active Python interpreter is a shared agent virtualenv with 122 packages and **no `pip`**. `pip-audit`, `pip-compile` and `cyclonedx-py` could not be installed.

**Workaround:** `tools/supply_chain.py` was written to query the PyPI JSON API for hashes and OSV.dev for vulnerabilities directly. These are the same data sources the unavailable tools use, so the result is equivalent in substance for the dependencies actually scanned.

**Result:** 5 direct dependencies scanned — fastapi 0.133.1, httpx 0.28.1, pydantic 2.13.4, pytest 9.1.1, uvicorn 0.41.0. **No known vulnerabilities.**

**Known limitation, recorded in the artefacts themselves:** the lockfile and SBOM are marked `transitive_closure_complete: false`. A trustworthy full transitive closure requires `pip freeze` from a clean project virtualenv, which this environment cannot produce. CI performs this properly. The flag is machine-readable so that a consumer of the SBOM cannot mistake a direct-dependency scan for a complete one.

### 10.2 AppSail FastAPI runtime proof — COMPLETED

*Earlier drafts recorded this as blocked on Catalyst access. That is no longer true and the statement has been withdrawn.*

Proven on a live deployment. Full evidence: **`PHASE_0A_EXIT.md` §8** and `docs/spikes/appsail-fastapi/EVIDENCE.md`.

| | |
|---|---|
| Service | `wbs-platform-spike` (`4239000000096001`), Development, India DC |
| Working deployment | `4239000000096007` |
| Result | `GET /` → **200**, `fastapi 0.133.1` on **Python 3.13.9 / x86_64** |
| Startup | **bound and served in 0.924 s** against AppSail's 10-second deadline; reproduced at 0.938 s |
| Scale-to-zero | 1 instance while serving, **0 after ~7 min idle**, second cold start returned a *fresh* process |

The concern that motivated this spike was real and is now retired: Zoho's documentation names Flask, Django, Bottle, CherryPy and Tornado and never FastAPI, permitting it only by a general "no framework restrictions" clause. **FastAPI is now verified by observation rather than inferred from a general clause.**

**The spike also produced a finding that changes Phase 1.** The first deployment (`4239000000096004`) built successfully and then failed every request with `503 "Execution failed. Please check the startup command or port."` — a misleading message, because both the command and the port were correct. Catalyst's managed-runtime documentation states:

> "You must ensure that you add all modules and configuration files, along with the main file and native client files in the build path."

**Catalyst does not run `pip install -r requirements.txt`.** The process died on `from fastapi import FastAPI` before it could bind. The working bundle vendors the dependency graph as Linux x86_64 CPython 3.13 wheels — never copied from Windows `site-packages`, because `pydantic_core` ships a compiled `.so` and a Windows `.pyd` reproduces the identical opaque 503.

Two resolution traps, each of which fails at import: **pydantic 2.13.4 pins `pydantic-core==2.46.4`** (latest 2.48.0 breaks it), and **fastapi 0.133.1 additionally requires `annotated-doc`**.

**Consequence for Phase 1:** the deployment pipeline must vendor dependencies for Linux x86_64 / CPython 3.13 as a build step. Not optional. Procedure in `docs/spikes/appsail-fastapi/BUILD.md`.

**Residual limitation:** the console "View Logs" panel could not be opened from the automation browser pane. Startup timings come from the application's own instrumentation, which is more precise than a log tail but is not the console log. Capture that from a normal browser session if an audit requires it.

---

## 11. What Phase 0A could not complete, and why

*Two rows below are struck through: both were closed during Phase 0A. They are retained because how a gap was closed is part of the audit record.*

| Item | Reason not completed | Consequence |
|---|---|---|
| ~~**AppSail FastAPI runtime proof**~~ | **COMPLETED.** Proven on a live Catalyst deployment. | **Closed.** `wbs-platform-spike` serves 200 on Python 3.13.9 / x86_64, binding in 0.924 s against a 10 s deadline. Phase 1 may depend on the runtime. See §10.2 and `PHASE_0A_EXIT.md` §8. **New obligation:** the Phase 1 pipeline must vendor dependencies for Linux x86_64 / CPython 3.13. |
| ~~**Complete transitive dependency closure**~~ | **COMPLETED in CI.** The local interpreter has no `pip`; the hosted job resolves the real closure. | **Closed.** 27 packages, `transitive_closure_complete: true`, 0 known vulnerabilities. The locally committed artefacts cover direct dependencies only and say so in machine-readable form. |
| **DEF-01 fix** | Migration-runner changes belong with the Phase 1 PostgreSQL port; fixing twice would mean re-doing the work against a different database. | Defect is open and reproduced, held by a strict-xfail test that will fail loudly when fixed. High severity for any deployment carrying an existing database file. |
| **GAP-01 resolution (`CANCELLED` status)** | C3 is a frozen client artefact; adding a status code is a client decision, not an engineering one. | Requires client agreement. `Cancelled` remains financially load-bearing at runtime while absent from the frozen register. |
| **GAP-04 resolution (role reconciliation)** | Blocking decision D-12 is unresolved; the mapping between 13 business roles and 7 technical roles is a client decision. | Gates Phase 3. Any mapping written now would be an assumption. |
| **GAP-05 traceability for 107 CLIENT-PDF requirements** | Rows were deliberately not invented. A plausible but wrong traceability row reports coverage that does not exist. | 107 requirements remain `PENDING`. A ratchet test prevents the pending count from rising. |
| **GAP-02 and GAP-03 fixes** | `styles.css` is the client-approved baseline and must not change; the correct remedy is to amend the token registry, which is a contract artefact requiring sign-off. | Off-token colours are allowlisted so new ones still fail; declared dark mode remains unimplemented. |

---

## 12. Standing guards introduced in Phase 0A

These are the mechanisms that keep the findings above from decaying quietly:

- **Strict-xfail defect register** (`tests/test_known_defects.py`) — asserts desired behaviour, with `xfail(strict=True)` and the `product_defect` marker. A fix turns the test into a hard failure, forcing deliberate marker removal.
- **Status-registry disjointness gate** (`tests/test_contracts.py::test_integration_statuses_are_disjoint_from_business_statuses`) — caught OBS-01 on first run.
- **Strict token gate** — `:root` must match `C6_tokens.json` exactly (25 of 25). Off-token colours elsewhere are held by a finite allowlist, so new ones fail.
- **Traceability ratchet** — the pending requirement count may fall but never rise.
- **Machine-readable supply-chain completeness flag** — `transitive_closure_complete: false` on the lockfile and SBOM prevents a direct-dependency scan being mistaken for a full closure.

---

## 13. Open at Phase 0A close

One item, and it is not an engineering item.

**Eight client ambiguities — AMB-01…07 and AMB-09 — remain open, and none has a named individual assigned.** A proposed role owner is recorded against each in `research/00_intake/client_notes_2026-08-28.md` §4, but a role cannot answer a question. Until a person is named these are unowned, and no artefact in this repository claims otherwise.

Each carries a documented working assumption so delivery is not blocked. Three are expensive if the assumption is wrong:

| ID | Question | Gate | Cost if wrong |
|---|---|---|---|
| **AMB-03** | WBS element model — depth, parent-child rules, attributes, numbering | **Phase 2** | Reworks the ledger spine: `budget_ledger_cell`, `wbs_path`, every rollup |
| **AMB-05** | PR is raised in WBS, not the Zoho screen staff use today | **Phase 5**, sign-off at go-live | A change to how procurement staff work, not a technical detail |
| **AMB-09** | "Custom Approval" — configurable routing, or user-written logic? | **Phase 4** | User-written logic is a different product: scripting runtime, sandboxing, safety model |

The instrument for closing these is `research/00_intake/client_decision_questionnaire.md`.
