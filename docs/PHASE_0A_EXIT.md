# Phase 0A — exit gate report

**Branch:** `phase-0a/foundations` · **Date:** 2026-08-28 · **Plan:** v1.2.1 §16
**Status:** committed and pushed. Hosted CI **green on all four jobs**. **FastAPI-on-AppSail proof COMPLETE** (§8). Remaining open item: client ambiguities AMB-01…07, 09.

Phase 0A is *"work possible with no Zoho tenant"*. Nothing in it depends on the outcome of the §2.4 AppSail→PostgreSQL connectivity gate, so all of it stays valid under every fallback in plan §2.5.

---

## 1. Exit criteria

| Criterion | Status | Evidence |
|---|---|---|
| Contract gate green in CI | **Met** | hosted run 33169733611, job *Contract and inventory gates* |
| Regression suite green in CI | **Met** | **488 passed, 1 xfailed** in 43 s on ubuntu |
| **Hosted Windows visual regression** | **Met** | **60 passed** on `windows-latest` — the baselines' own platform |
| **Complete transitive supply chain** | **Met** | **27 packages, `transitive_closure_complete: True`, 0 known vulnerabilities** |
| SBOM published | **Met** | `sbom.cyclonedx.json`, committed |
| Visual baselines committed | **Met** | 54 PNGs, 3 viewports, pixel-clean on re-run |
| Annex A committed, IDs allocated | **Met** | 15 requirements across two provenance classes |
| Ambiguities resolved or owned | **Partly** | AMB-08 closed; AMB-01…07, 09 open with owners |
| FastAPI-on-AppSail proven | **MET** | `wbs-platform-spike` serves 200; §8 |

---

## 2. What was delivered

### Executable contract validation — closes AUD-M-001

`tests/test_contracts.py` makes the frozen registries in `research/30_contracts/` enforceable rather than aspirational. It is a **ratchet**: every existing divergence is recorded in `CONTRACT_GAPS.md` and asserted as an exact set, so a new divergence fails the build and a closed one fails until the register is updated.

Four status registries were separated, because plan v1.1 wrongly implied everything maps into the frozen 21 business statuses:

| Registry | Namespace |
|---|---|
| `C3_statuses.json` | 21 client-visible business statuses (unchanged, frozen) |
| `C15_approval_statuses.json` | approval instance / stage / assignment |
| `C16_integration_statuses.json` | inbox / outbox / job / circuit |
| `C17_zoho_status_map.json` | raw Zoho values → business statuses, **per product** |
| `C18_domain_statuses.json` | accounting_status, exception_status, lifecycle_state |

**The separation earned its keep on first run**: the gate caught that the circuit-breaker state `CLOSED` collided with the business status `CLOSED`, and the two mean opposite things — a closed circuit is healthy and passing traffic; a closed project is terminal and blocks posting. Renamed to `CIRCUIT_CLOSED`/`CIRCUIT_OPEN`/`CIRCUIT_HALF_OPEN`.

### Test-inventory ratchet — protects plan §15.1

`tests/TEST_MANIFEST.json` + `tests/test_manifest.py` + `tools/build_test_manifest.py` + `tests/ADAPTATIONS.md`.

Records every test function **and its assertion count**. A removed, renamed **or gutted** test fails the build. The assertion count matters: the first version guarded names only, so replacing a test body with `pass` kept the name and nothing failed. Both guards were verified by deliberately breaking them.

The 220-function baseline is pinned. It caught two of my own mistakes during this phase.

### Structured observability — closes AUD-M-007

`app/backend/observability.py`: JSON logging, a correlation `ContextVar`, redaction, bounded RED metrics, and a defined alert vocabulary.

**Redaction is the part that matters** — structured logging lets a caller attach arbitrary fields, which is exactly how a regulated identifier reaches a log sink. Adversarial review found eleven bypasses in the first implementation; all are closed and each has a named regression test:

- secrets passed as lazy `%`-format arguments, or interpolated by the caller
- **secrets inside exception tracebacks** — the highest-probability real leak, since an HTTP client exception routinely echoes the request headers that carried the token
- camelCase, kebab-case and HTTP-header key shapes (`clientSecret`, `client-secret`, `X-Api-Key`, `gstNo`, `panNumber`) — the largest class, and the normal shape in Zoho payloads
- secrets in a list under a benign key
- objects whose `repr` carries a credential, and `bytes` values — both previously stringified *after* redaction had finished
- bare GSTIN and PAN, matched by value shape where there is no key at all

Consistent with plan §10.4, values are **removed**, never hashed: a hash in a log is a correlatable pseudo-identifier with no operational value.

### Supply chain — contributes to AUD-M-006

`tools/supply_chain.py` produces a hash-pinned lockfile, a CycloneDX 1.5 SBOM, and an OSV.dev vulnerability scan. **Real result: 5 direct dependencies scanned, no known vulnerabilities.**

All three artefacts are marked `transitive_closure_complete: false` and say why. That is honest, not complete: a trustworthy full closure needs `pip freeze` from a clean project venv, which CI does. The artefacts are committed rather than left in a 90-day CI artifact, because a retention-limited zip is not durable evidence that "the vulnerability scan is verified".

### Visual regression on the approved UI

54 baselines across 17 views × 3 viewports (1440 / 1024 / 800), plus behavioural assertions: the document never scrolls horizontally at any breakpoint (AUD-M-004), the density toggle behaves, and no status renders without a text label (a `C3` accessibility requirement).

Two layers now protect the approved design, and **both are real**:

1. **`styles.css` SHA-256 pin.** This did not exist when first claimed — my own comments asserted a checksum gate that was fiction, and review caught it. It now exists, is verified to fail on tampering, and hashes line-ending-normalised content so it behaves identically on Windows and Linux.
2. **Pixel-exact screenshots**, which catch a JavaScript regression rendering the same CSS differently — a dropped column, a lost `tabular-nums` alignment.

### Provenance and traceability

`C14_traceability.json` now holds **122 requirements**: 107 `CLIENT-PDF` (pending traceability), 13 `CLIENT-NOTES-2026-08-28`, 2 `PRODUCT-OWNER-REQUEST-2026-08-28`. The 107 carry real metadata but **deliberately empty** screens, tables, services and tests — a plausible-looking wrong traceability row silently reports coverage that does not exist, which is worse than a visibly empty one. A ratchet asserts the pending count never rises.

### CI — closes AUD-H-010's residual

`.github/workflows/ci.yml`: gates ordered cheapest-first, then the full suite, supply chain, and visual regression. The VRT job runs on `windows-latest` because the baselines are Windows-captured; running it on Linux would fail every snapshot on every run, and the job would be disabled within a week.

---

## 3. Verification

| Check | Result |
|---|---|
| Full Python suite | **452 passed, 1 xfail** (DEF-01, by design) |
| Baseline suite intact | **220 functions**, 680 assertions tracked |
| Visual regression | **60 passed**, pixel-clean, after the middleware change |
| Vulnerability scan | **0 findings** across 5 direct dependencies |
| `styles.css` | unchanged; pin verified to bite on tampering |
| Contract gate | 57 tests |

Baseline before this phase: 348 collected cases, ~189 s. After: 452, ~225 s.

---

## 4. What Phase 0A could not complete

Both items recorded here in earlier drafts are now **closed**. They are retained struck through, because how a gap was closed is part of the audit record.

| Item | Status |
|---|---|
| ~~FastAPI-on-AppSail runtime proof~~ | **CLOSED.** Proven on a live Catalyst deployment — `wbs-platform-spike` serves 200 on Python 3.13.9 / x86_64, binding in 0.924 s against a 10 s deadline. Full evidence in §8 and `docs/spikes/appsail-fastapi/EVIDENCE.md` |
| ~~Full transitive dependency closure~~ | **CLOSED in CI.** 27 packages, `transitive_closure_complete: true`, 0 known vulnerabilities. The locally committed artefacts cover direct dependencies only and say so in machine-readable form |

**One item remains open, and it is not an engineering item.**

| Open item | Owner | Consequence |
|---|---|---|
| **Eight client ambiguities — AMB-01…07, AMB-09** | **Client. No named individual is assigned to any of them.** | Each has a documented working assumption so delivery is not blocked, but three carry high cost if the assumption is wrong: **AMB-03** (WBS element model, gates Phase 2), **AMB-05** (PR working-practice change, gates Phase 5), **AMB-09** (approval configurability, gates Phase 4). See `research/00_intake/client_decision_questionnaire.md` |

This does **not** block Phase 0B, which is a platform gate rather than a requirements gate.

---

## 5. Findings raised

Full detail in `PHASE_0A_FINDINGS.md`. Headline:

**DEF-01 (HIGH) — a legacy database cannot be upgraded, and the app boots through the upgrade path.** `migrate.upgrade()` replays migration 001 against a database that already carries the v1 schema, raising `table entity already exists`; `app/run.py` calls it unconditionally whenever the database file exists. **Any deployment carrying a pre-migration-runner database file fails to start.** Commit `ce7f3c5` covered a hosted instance with *no* database, not one with an *old* one.

Not fixed here — migration-runner changes belong with the Phase 1 PostgreSQL work. Recorded as `xfail(strict=True)` with a paired control test proving the defect is narrow (adopting an existing schema) rather than general, so the fix is a baseline/adopt step, not a rewrite. When someone fixes it, the strict marker turns the pass into a failure and forces deliberate removal.

Also open: **GAP-01** `Cancelled` is financially load-bearing but absent from the frozen 21 statuses (needs client agreement); **GAP-04** the 13 business roles and 7 technical roles are unreconciled (**blocking decision D-12**, gates Phase 3); GAP-02/03 token-registry completeness; GAP-05 traceability coverage.

---

## 6. Recommended next

1. **Issue the client decision questionnaire** (`research/00_intake/client_decision_questionnaire.md`) and **assign a named individual per question.** Q3, Q5 and Q8 warrant a meeting; the rest can be answered in writing.
2. **Close D-12 and D-6** — both gate Phase 3 and neither needs a Zoho tenant, so neither is waiting on anything external.
3. **Run Phase 0B.** The AppSail→PostgreSQL connectivity gate (plan §2.4) is go/no-go for the entire data-platform decision, and Zoho support response time is outside our control. Start it early. `wbs-platform-spike` is left deployed and idle specifically so it can be reused for that test.
4. **Decide GAP-01** (`Cancelled` absent from the frozen 21 statuses) at the D-10 approval-matrix conversation, where the status vocabulary is already open.
5. **Build vendoring into the Phase 1 pipeline.** Catalyst does not run `pip install`; dependencies must be vendored for Linux x86_64 / CPython 3.13 as a build step. See §8 and `docs/spikes/appsail-fastapi/BUILD.md`. This is a change to the delivery model, not a detail.

---

## 7. Not done, deliberately

No commit has been made. No code was changed beyond `app/backend/main.py` (correlation binding, bounded metrics, structured request logging) and the one-line `observability.configure()` call that makes the logging real rather than inert. The 220 baseline tests run unmodified, and `tests/ADAPTATIONS.md` records zero adaptations.


---

## 8. FastAPI-on-AppSail proof — COMPLETE

The one Phase 0A exit item that needed a live platform. Zoho's AppSail documentation names Flask, Django, Bottle, CherryPy and Tornado and **never FastAPI**; it is permitted only by a general "no framework restrictions" clause. Plan v1.2.1 Phase 1 assumes FastAPI works, so this settles it by observation.

### Service

| | |
|---|---|
| Project | WBS-ZohoERP-POC, PID `4239000000062001`, **India DC** |
| Service | **`wbs-platform-spike`**, id `4239000000096001` — separate; `wbs-capex-poc` untouched |
| URL | `https://wbs-platform-spike-50044908499.development.catalystappsail.in` |
| Environment | **Development only.** Production never deployed |
| Runtime | Python 3.13 — reports `3.13.9`, CPython, **x86_64** |
| Startup command | `python3 -u main.py` |
| Port / Memory / Disk | 9000 / 512 MB / 256 MB |
| Environment variables | **none** — no credentials, no client data |

### Result

```json
{
  "framework": "fastapi",
  "fastapi_version": "0.133.1",
  "pydantic_version": "2.13.4",
  "python": "3.13.9",
  "machine": "x86_64",
  "listen_port_env": "9000",
  "bound_port": 9000,
  "seconds_from_process_start": 0.924,
  "dependencies_vendored": true
}
```

`GET /healthz` → `200 {"status":"ok","framework":"fastapi"}`.
`GET /nope` → `404 {"detail":"Not Found"}` — FastAPI's own router, not a static file server.

### Startup deadline

**Bound and served in 0.924 s** against AppSail's documented **10-second** deadline — roughly 10x headroom. Reproduced at 0.938 s on a second cold start.

`listen_port_env: "9000"` confirms the port contract. Reading `X_ZOHO_CATALYST_LISTEN_PORT` **inside Python** was necessary: AppSail executes the start command without a shell, so `--port $VAR` would not expand.

### Cold start and scale-to-zero

| Measurement | Value |
|---|---|
| Cold start (1st) | **2,224 ms** round trip, process uptime 0.924 s |
| Cold start (2nd, after ~7 min idle) | **1,843 ms** round trip, process uptime 0.938 s |
| Warm | 352–475 ms |
| Instances while serving | **1** (`567e7cf5-689b-4515-ae03-12e16bb0d7a7`) |
| Instances after ~7 min idle | **0** — *"There are no instances running..."* |

**Scale-to-zero is confirmed empirically**, not merely from documentation. The second cold start returned a *fresh* process (uptime 0.938 s, not a continuation), proving the instance was genuinely reclaimed and respawned. Cold start costs roughly **1.4–1.8 s** over warm.

This validates the constraint plan §2.1–2.2 is built on: AppSail is a request/response tier that cannot host a resident worker, and background work must run on Job Scheduling → Cron/Event Functions.

### Why the first attempt failed — and what it teaches Phase 1

The first deployment (`…096004`) built successfully but every request returned
`503 "Execution failed. Please check the startup command or port."`

Cause, confirmed at source in Zoho's managed-runtime documentation:

> *"You must ensure that you add all modules and configuration files, along with the main file and native client files in the build path."*

**Catalyst does not run `pip install -r requirements.txt`.** The build died on `from fastapi import FastAPI` before it could bind. The command and port were correct throughout.

One hypothesis was eliminated by evidence rather than assumption: `python3` is valid on this runtime — the pre-existing `wbs-capex-poc` uses `python3 app/run.py --public`.

The working deployment (`…096007`) vendors the dependency graph as **Linux x86_64 CPython 3.13 wheels from PyPI**, never copied from Windows `site-packages` — `pydantic_core` ships `_pydantic_core.cpython-313-x86_64-linux-gnu.so`, a platform-specific binary. The archive contains **zero** Windows `.pyd` files.

Two resolution traps that each fail at import and are easy to miss:

- **pydantic 2.13.4 pins `pydantic-core==2.46.4`** — taking the latest (2.48.0) breaks it.
- **fastapi 0.133.1 additionally requires `annotated-doc`.**

**Phase 1 consequence:** the deployment pipeline must vendor dependencies for Linux x86_64 CPython 3.13 as a build step. This is a real change to the delivery model and is not optional.

### Limitations

- **The console "View Logs" panel could not be opened** from the Browser pane across repeated attempts and viewport sizes — likely a blocked popup. Startup evidence comes from the application's own instrumentation instead, which is more precise than a log tail. Console logs need a normal browser session.
- **`wbs-capex-poc` was never sent a request.** It shows *Live* with *0 instances*, exactly as this service did while broken, so "Live" means "deployed", not "healthy". Its health is unverified. Note it carries `CAPEX_DB_PATH=/tmp/capex.db` — a database file present at boot is precisely the **DEF-01** condition. Flagged, not diagnosed.

### Disposition

`wbs-platform-spike` is left **deployed but idle at zero instances**, for reuse in the Phase 0B PostgreSQL connectivity test. **Not deleted. No PostgreSQL test performed** — that needs separate approval.
