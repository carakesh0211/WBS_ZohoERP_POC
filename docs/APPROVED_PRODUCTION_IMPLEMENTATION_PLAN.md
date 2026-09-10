# CAPEX & WBS Control Hub — Production Implementation Plan **v1.2.1**

> **Status:** **APPROVED.** Phase 0A authorised to begin; all later phases require separate authorisation at each exit gate.
> **Supersedes:** v1.2, v1.1, v1.0 (all 2026-08-28). Change logs at §0A and §0.

---

## 0AA. What changed in v1.2.1 — provenance correction only

A single attribution correction directed by the product owner. **No requirement changed, no scope changed, no design changed.**

`REQ-GRN-002` (GRN / Purchase Receive inbound synchronisation) and `REQ-BILL-003` (Vendor Bill inbound synchronisation) **do not originate from the two handwritten pages.** They originate from the product owner's separate instruction. Their primary provenance is corrected from `CLIENT-NOTES-2026-08-28` to **`PRODUCT-OWNER-REQUEST-2026-08-28`**, with supporting `CLIENT-PDF` requirements retained as secondary evidence.

**Both requirements remain mandatory and in scope.** This changes attribution only — not priority, not scope, not the acceptance criteria, not the phase they land in.

Updated: the provenance registry (§4), the ID allocation table (§4), Annex A.2 rows 13 and 14, Annex A.4 (AMB-08 closed and replaced by a provenance record), and the §0A discrepancy note below.

---

## 0A. What changed in v1.2

Plan v1.1 was technically accepted subject to a final requirements revision. Four changes:

| # | Direction | Effect |
|---|---|---|
| 1 | Record the 14 client-note requirements as `CLIENT-NOTES-2026-08-28`, transcribed **now**, not deferred to Phase 0A | **Done — Annex A.** Both handwritten pages are transcribed verbatim; 15 requirement IDs allocated across SEC / INT / WBS / BUD / REV / PRC / PO / RPT / GRN / BILL; full traceability rows written; ambiguities registered separately **without removing any requirement** |
| 2 | Zoho ERP v3 is **provisional** pending D-14, not settled | §11 re-framed. Every ERP-specific fact now carries a provisional marker; the matrix keeps Books and Inventory rows live as the alternative target |
| 3 | Zoho tenant configuration (custom fields, unique integration reference, roles, workflow) must be **in scope or in a signed client-responsibility matrix** — it cannot simply be excluded | **Exclusion 11 withdrawn.** Replaced by §18.5, a signed responsibility matrix with per-item setup instructions, verification steps and named owners |
| 4 | Every §18.4 exclusion requires explicit client/product-owner sign-off | §18.4 rebuilt as a sign-off register with named signatories and consequence-if-not-signed |

**Provenance correction of record.** v1.1 stated that `CLIENT-NOTES-2026-08-28` was an empty provenance class because the handwritten pages had not reached me. They have now been supplied and read. **The 14 requirements below are client-sourced and are no longer carried as `MASTER-PROMPT` or `PROPOSED-PENDING-SIGNOFF`.** This materially strengthens the requirement base: much of what v1.1 hedged as master-prompt-derived is in fact confirmed client input.

**One provenance discrepancy — raised in v1.2, RESOLVED in v1.2.1.** The two supplied pages carry six numbered WBS items — Settings, Budget, Approvals, PR, PO, Dashboard. Requirements 13 (GRN / Purchase Receive inbound sync) and 14 (Vendor Bill inbound sync) appear on neither page. v1.2 flagged this rather than guessing. The product owner has since confirmed the correct source: **they originate from a separate product-owner instruction, not from the handwritten notes.** Their provenance is corrected to `PRODUCT-OWNER-REQUEST-2026-08-28` (§0AA). There is no missing third page. AMB-08 is closed.

---

## 0. What changed in v1.1, and why

Fifteen corrections were directed. Four of them required verification that overturned material parts of v1.0. Those corrections are recorded here plainly rather than absorbed silently.

| # | Correction | Effect on the plan |
|---|---|---|
| 1 | AppSail worker assumption | **v1.0 was wrong.** AppSail scales to zero, kills instances after 5 minutes of uptime, caps requests at 30 s, and documents no always-on setting. A resident worker process is not viable. Replaced with a Catalyst Job Scheduling + Cron/Event Function execution model — §2 |
| 2 | AppSail → PostgreSQL connectivity spike | Added as **Phase 0B-1**, and promoted to a **go/no-go gate**: Zoho documents no outbound egress policy and **no static egress IPs**, so the allowlist story cannot be resolved from public docs — §2.4, §16 |
| 3 | Exact Zoho capability matrix, products never mixed | Added as §11. **Target product is Zoho ERP v3, not Books/Inventory.** ERP uses `ERP.*` scopes, `zohoapis.in/erp/v3`, `accounts.zoho.in`, and is **India-only** in the published docs |
| 4 | Bills `last_modified_time` | **You were right; I was wrong.** Books `GET /bills` *and* `GET /purchaseorders` **do** document a `last_modified_time` filter. My v1.0 "NOT CONFIRMED" was a fetch-truncation artifact. Corrected — and the correction is narrower than it looks, see §11.3 |
| 5 | Provenance tagging | `CLIENT-NOTES-2026-08-28` and `PROPOSED-PENDING-SIGNOFF` added as provenance values — §4 |
| 6 | Status registries separated | Four registries with controlled mappings — §8 |
| 7 | Test-migration policy | Reworded; no assertion may be weakened or removed without approval — §15.1 |
| 8 | Phase 0 split | Phase 0A (no tenant) / Phase 0B (tenant-dependent) — §16 |
| 9 | Named identity, session, secrets, OAuth owner | §10.2; Catalyst Connections evaluated and **partially adopted** |
| 10 | Rollback model | Flag-only replaced with expand/contract + compatibility windows + PITR + tested restore — §17 |
| 11 | Multi-cell lock proof | **v1.0 had a correctness hole** — locking only the nearest budget-owning ancestor is insufficient. Corrected to the full budget-owning ancestor chain, with a proof — §7.4 |
| 12 | Vertical slices | Screens now ship inside each phase, not deferred to a Phase 7 — §16 |
| 13 | GSTIN/PAN handling | Hashing **withdrawn**. Replaced with classification, encryption, masking and access restriction — §10.4 |
| 14 | Pool/RLS leakage and worker scope | §10.3, §15 |
| 15 | UAT gates, team, effort, cost, exclusions | §18 |

### Three findings that should be read before anything else

1. **AppSail is a request/response tier only.** Instances live 5 minutes and are killed when idle; requests time out at 30 s; there is no documented always-on option. Every background job — polling, sweeps, outbox drain, reconciliation, audit anchoring — must run on Catalyst **Job Scheduling** targeting **Cron or Event Functions**, whose documented ceiling is **15 minutes**. Any job that could exceed 15 minutes must be chunked with checkpointed state. This is an architectural constraint, not a tuning parameter.

2. **Two Catalyst components are unavailable in the India data centre.** **Circuits** is "not available to Catalyst users accessing from the EU, AU, IN, JP, SA or CA data centers", and **Integration Functions** is "not available … from the EU, AU, IN, or CA data centers". Zoho ERP is India-only, so the India DC is the likely deployment. No orchestration in this plan depends on either.

3. **External PostgreSQL connectivity from AppSail is undocumented, and it is a go/no-go gate.** Zoho's Database Connector CodeLib is documented for *Functions*, not AppSail. There is no published egress policy and — critically — **no documented static egress IP range** to give a managed PostgreSQL's allowlist. This cannot be resolved from public documentation. Phase 0B-1 obtains written confirmation from Zoho support before any code depends on it, and §2.5 carries the fallback if the answer is no.

---

## 1. Context

The POC at `C:\Users\rakes\Fable5\WBS_ZohoERP_POC` is client-approved on its structure, information density, navigation model, colour theme, typography, spacing, tables, status treatment and enterprise feel. That UI is the **approved visual baseline** — preserved, not redesigned.

What it is: FastAPI + SQLite + 3 vanilla frontend files, with a genuinely good control engine — integer-paise money, a per-`(WBS, budget head)` control cell, anti-double-counting, maker-checker, a SHA-256 hash-chained append-only audit, 18 SQLite triggers emulating composite foreign keys, and 220 pytest tests named after the audit findings they close.

What it is not: production software. Open or partially open in `FINDINGS_REMEDIATION_STATUS.csv`:

- **AUD-H-002** connector is MOCK — no OAuth, HTTP, pagination, retry or rate limiting.
- **AUD-H-009** SQLite unsuitable; the "ports unchanged to PostgreSQL" claim was **withdrawn**.
- **AUD-C-006 residual** `auth.py` is explicitly a *development* identity provider.
- **No row-level data scope exists anywhere.** Not partially — at all.
- **AUD-H-004** received-not-billed attribution unproven against live data.
- **AUD-C-007** `external_document` exists but nothing writes to it.
- **AUD-H-005** no external control totals, cut-off or completeness reconciliation.
- **AUD-H-007 residual** foreign currency stored but never applied.
- **AUD-M-001 / M-006 / M-007** no contract validation, no lockfile/SBOM, no durable logs or metrics.
- **AUD-H-010 residual** no CI pipeline.

### Decisions taken by the client (2026-08-28)

| # | Decision | Consequence |
|---|---|---|
| 1 | **Catalyst AppSail hosting, external managed PostgreSQL for data** | Catalyst Data Store rejected: Zoho documents no transactions, row locks, FK/CHECK constraints, triggers or partial indexes — exactly what the audited controls rest on. **Contingent on the §2.4 connectivity gate.** |
| 2 | **WBS Hub is the system of record for Purchase Requests; it emits POs to Zoho** | No Zoho PR object exists. Nothing to reconcile on the PR. |
| 3 | **No Zoho sandbox yet — design defensively** | No design may depend on an unconfirmed capability. Tenant validation is Phase 0B. |
| 4 | **Vite + TypeScript incrementally, vanilla JS / Web Components, no React or Svelte** | Existing DOM and CSS preserved; Playwright visual regression guards approved screens. |

---

## 2. Platform runtime model

### 2.1 The AppSail constraints that shape everything

| Constraint | Documented value |
|---|---|
| Request timeout | **30 seconds** |
| Instance lifetime | **5 minutes total uptime**; inactive instances scaled down after 5 minutes |
| Scale to zero | **Yes**, with cold start on next request |
| Always-on / minimum instances | **No documented option** |
| Port binding | `X_ZOHO_CATALYST_LISTEN_PORT`, must listen within **10 seconds** |
| Default resources | 512 MB memory, 256 MB disk |
| Concurrency | 100 concurrent requests/instance; new instance at 80; **max 5 instances/app** by default |
| Python | 3.10–3.13 managed, plus custom OCI images |
| FastAPI | **NOT CONFIRMED by name.** Docs name Flask, Django, Bottle, CherryPy, Tornado, and state "no framework restrictions". Permitted by the no-restriction clause, not by explicit support — Phase 0A-1 proves it |

**Consequences.** No resident worker. No in-process scheduler. No in-process token cache that survives (v1.0's "3300 s in-process access-token cache with single-flight" is withdrawn — see §10.2). No long-running export or migration inside a request. Cold starts are normal, so startup must be cheap: the existing `run.py` "migrate → provision → uvicorn" sequence must move migration **out** of the boot path into a deploy step.

### 2.2 The job execution model

```
Catalyst Job Scheduling  (Cron granularity: 1 minute)
  ├─ Job Pool → Cron Function / Event Function      ceiling 15 minutes
  └─ Job Pool → AppSail service (HTTP)              ceiling 30 seconds  ← never for batch work

Catalyst Event Listeners / Signals  → Event Function (15 min), queued, retryable
```

Every background activity is a **bounded, chunked, checkpointed job**. The contract each job obeys:

1. Claim a bounded batch from PostgreSQL with `SELECT … FOR UPDATE SKIP LOCKED LIMIT n`.
2. Maintain a **soft deadline of 12 minutes** (80% of the 15-minute ceiling). On reaching it, commit progress and return.
3. Persist a resumable cursor — never rely on completing in one invocation.
4. Be idempotent: re-running from the last checkpoint must produce the same state.

| Job | Trigger | Cadence | Chunk | Checkpoint |
|---|---|---|---|---|
| `poll_bills`, `poll_purchaseorders` | Job Scheduling Cron | 5 min | 200-record pages, ≤ 40 pages | `integration_watermark.hwm` |
| `sweep_po_anchored` | Cron | 15 min | 50 POs | `last_po_id_swept` |
| `drain_outbox` | Cron | 1 min | 25 outbox rows | row state |
| `sweep_control_totals` | Cron | nightly | one period per invocation | `(module, period)` |
| `roll_period_effective_budget` | Cron | period boundary | one entity | `period_id` |
| `verify_audit_chains` | Cron | nightly | one stream per chunk | `stream_key` |
| `expire_pr_reservations` | Cron | hourly | 500 rows | reservation id |
| `build_export` | Event Function via Signals | on demand | 50k rows | export job cursor |

**500 cron executions per project per day in the development environment** is a documented cap (no production limit). At the cadences above, development runs a reduced schedule.

`job` table gains `checkpoint jsonb`, `soft_deadline_at`, `resume_count`. A job exceeding `max_resume_count` raises an alert rather than looping forever.

### 2.3 What this costs us, honestly

Sync latency floor is the cron granularity, **1 minute**, not seconds. Outbound PO emission is therefore near-real-time but not synchronous; the UI must show `Queued for Zoho` as a first-class state (see the integration status registry, §8.3). This is a genuine product consequence of the platform choice and belongs in the client conversation, not buried.

### 2.4 The connectivity gate — Phase 0B-1

Nothing in Zoho's public documentation confirms an AppSail container can open a TCP connection to an external PostgreSQL on 5432, and nothing publishes egress IPs. The spike must obtain, **in writing from Zoho support**:

1. Is arbitrary outbound TCP from AppSail permitted, on 5432 specifically?
2. Is egress unrestricted, or proxied/allowlisted?
3. **Is there a static egress IP or CIDR range per Catalyst project/DC?** Without this, the PostgreSQL side cannot be IP-allowlisted and must rely on TLS + certificate + strong credentials alone.
4. Is outbound TLS (`sslmode=verify-full`) supported, and can a custom CA bundle be shipped in the image?
5. Documented connection limits or pooling guidance.
6. Same five answers for Cron/Event **Functions**, which is where the Database Connector CodeLib is actually documented.

Also spiked, on the PostgreSQL side: **India-region availability** (Mumbai/Hyderabad), **PITR** support and retention, the required **extensions** (`ltree`, `pgcrypto`, `btree_gist`), TLS enforcement mode, maximum connections by tier, and whether a pooler is provided or must be self-hosted.

**Exit:** written confirmation on all six Zoho questions and all six database questions, plus a working end-to-end proof — an AppSail app performing a `SELECT 1` over TLS against a managed PostgreSQL in the India region, and the same from a Cron Function.

### 2.5 Fallbacks if the gate fails

Ranked, to be exercised only on a negative result:

1. **Functions-fronted data access.** If Functions can reach PostgreSQL but AppSail cannot, AppSail becomes a pure static/API-gateway tier and all data access moves behind Advanced I/O Functions (30 s) and Cron/Event Functions (15 min). Costs a network hop and splits the codebase; preserves every control guarantee.
2. **Move the application tier off Catalyst**, keeping Catalyst only for scheduling and Connections. Contradicts client decision 1 and would be brought back for a decision.
3. **PostgreSQL without IP allowlisting**, secured by `sslmode=verify-full`, client certificates, per-service credentials and rotation. Acceptable only with the client's security sign-off, and recorded as an accepted risk.
4. **Catalyst Data Store.** Still rejected. It cannot carry the audited controls.

**No code depends on the outcome until the gate clears** — Phase 0A is deliberately scoped to work that is valid under every fallback.

### 2.6 Runtime shape

```
[ browser ] ──HTTPS, CSP style-src 'self'──► [ Catalyst AppSail : FastAPI ]
                                                request/response only, ≤30 s, scale-to-zero
                                                serves the Vite static build from /static
[ Catalyst Job Scheduling ] ──1-min cron──► [ Cron / Event Functions ]  ≤15 min, chunked
[ Catalyst Connections ] ─────────────────► Zoho OAuth token lifecycle (§10.2)
                    │
                    ▼
        [ managed PostgreSQL 16, India region ]
          extensions: ltree, pgcrypto, btree_gist
          PITR enabled; restore drills performed (§17)
                    │
        [ secrets manager ]  client secret, DB credentials, encryption keys
```

Shared code lives in one package installed into both the AppSail image and the Function bundles, so `domain.py`, `services.py`, `ledger_cell.py` and `repo.py` have exactly one implementation.

---

## 3. Current-state inventory

### Retain unchanged (substance frozen)

| Asset | File | Why |
|---|---|---|
| Integer-paise money, `to_paise`, `to_rupees`, `format_inr`, `split_pro_rata` | `app/backend/money.py` | Correct, tested, guard-railed. PostgreSQL `bigint` carries it directly |
| `_derive()`, `_blank()`, `_sum_cells()`, `_COMPONENTS`, `WATCH_PCT`, `CRITICAL_PCT` | `app/backend/domain.py` | This *is* the frozen `C5_formulas.json` registry |
| `compute_ledger()` | `app/backend/domain.py` | Becomes the oracle and the as-of reporting path (§7). Never deleted |
| Anti-double-count rules | `domain.compute_ledger` | `max(0, ordered − billed)`, zero when PO Cancelled/Closed; received-not-billed its own bucket |
| Audit payload `prev\|at\|actor\|action\|type\|id\|detail` | `services.audit` | Changing it invalidates every stored hash. Frozen permanently |
| `auth.MAKER_CHECKER`, `auth.require_separation` | `app/backend/auth.py` | The *floor* of the approval engine, not a replacement target |
| `styles.css` | `app/frontend/styles.css` | Approved baseline from `C6_tokens.json`. Byte-frozen behind a checksum gate |
| Constraint **intents** of all 18 triggers | `migrations/002_financial_controls.sql` | Re-expressed declaratively (§6.2), never dropped |
| `migrate.py` CLI contract | `app/backend/migrate.py` | `--status / --upgrade / --fresh --seed` is an operator interface |
| `conftest.py` isolation guarantees | `tests/conftest.py` | Template-DB-per-session, copy-per-test, `_assert_disposable` |

### Harden

`services.critical` → `BEGIN` + explicit multi-cell lock (§7.4). `_bump` → gains `FOR UPDATE`. `services.audit` → `stream_key` + advisory lock; payload unchanged. `verify_audit_chain` → per-stream, ordered by `seq`, run as a nightly chunked job. `auth.PERMISSIONS` → `role_grant` table; `require()` keeps its signature and fail-closed `UNKNOWN_PERMISSION`. `domain.budget_check` → effectivity from the accounting-period calendar; all refusal branches and message IDs preserved. `run.py` → migration removed from the boot path (cold-start budget is 10 s to listen).

### Extend

`compute_ledger` + materialised cells; `purchase_request` → line-level with reservations; `convert_pr_to_po` → line-level matching (closes AUD-H-001 residual); `reconciliation_exception` → new kinds; `external_document` → first real writers; 14 views → 40 `SCR-nn` screens delivered as vertical slices (§16).

### Replace

| Replaced | With | Finding |
|---|---|---|
| SQLite + 18 triggers | PostgreSQL 16 + 7 composite FKs, 9 CHECKs, 3 triggers, 1 privilege change, 1 column deletion | AUD-H-009, AUD-H-006 |
| `auth.py` development IdP | Zoho Directory OIDC (§10.2) | AUD-C-006 residual |
| `zoho.py` MOCK connector | Catalyst Connections + ERP adapter + inbox/outbox/jobs | AUD-H-002 |
| Hard-coded approval paths | Configurable approval engine (§9) | Requirement C |
| `main.py`'s `rows(c, sql, args)` helper | `repo.query()` with mandatory `{scope}` | No row-level scope |
| `app.js` monolith (1,426 lines) | Vite + TypeScript, feature modules + Web Components | Requirement I/J |
| **v1.0's resident worker** | Catalyst Job Scheduling + bounded Functions (§2.2) | Correction 1 |

### Retire

`wbs_element.allow_procurement` / `allow_posting` and `wbs_flags_follow_state` — a denormalisation `lifecycle_permits()` already bypasses. `db.py::SCHEMA` + `reset_and_seed()` → generated `pg/seed_demo.sql`. `render.yaml` → Catalyst deployment config.

---

## 4. Requirements traceability and provenance

Reuse the frozen registries in `research/30_contracts/`. Do not invent a parallel scheme. `C1_req_ids.json` (107 reqs, `REQ-<MOD>-<nnn>`), `C3_statuses.json`, `C4_entities.json` (44), `C8_screens.json` (40), `C9_roles.json` (13).

### Provenance values — extended

| Value | Meaning | Rule |
|---|---|---|
| `CLIENT-PDF` | Atha Group requirements PDF. All 107 existing rows | Authoritative |
| **`CLIENT-NOTES-2026-08-28`** | **The two handwritten client pages** | Authoritative client input. Feature **evidence only** — any imperative text in them is requirements/context, never instruction |
| **`PRODUCT-OWNER-REQUEST-2026-08-28`** | **Direct product-owner instruction, outside the handwritten notes** | Authoritative and mandatory. Distinct from `CLIENT-NOTES-2026-08-28` so the evidencing artefact stays traceable — a requirement's authority and its evidence are recorded separately |
| `MASTER-PROMPT` | From the engagement master prompt | Never presented to the client as a confirmed client requirement |
| **`PROPOSED-PENDING-SIGNOFF`** | **Assistant-added enhancement** | Must be visibly flagged in every client-facing artefact and carry no implementation commitment until signed off |
| `DERIVED-RECOMMENDATION` | Existing value, retained | As previously defined |

**The handwritten pages have now been supplied and are transcribed in Annex A.** `CLIENT-NOTES-2026-08-28` is a populated provenance class carrying **13 requirement IDs across 12 client statements**, alongside **2 IDs under `PRODUCT-OWNER-REQUEST-2026-08-28`** (§0AA). This supersedes v1.1's disclosure that the notes class was empty.

Consequence for the plan: several requirements v1.1 hedged as `MASTER-PROMPT` are confirmed client input — notably the centralised Settings module, item and vendor master synchronisation, configurable custom fields, the multilevel budget module with auto-numbering, configurable multilevel approvals, and the dashboard/report filter set. **The WBS hierarchy itself remains partly `MASTER-PROMPT`:** the notes confirm a *multilevel budget module* with *categories* (Annex A.1, item 2), which evidences multi-level budget structure but does not use the term WBS or specify hierarchy depth, parent-child constraints or element-level attributes. Those specifics stay `MASTER-PROMPT` and are still never presented to the client as confirmed client requirements. See ambiguity AMB-03.

### New ID allocation

| Prompt section | Module | Range | Provenance |
|---|---|---|---|
| A Settings & Administration | `SEC`, `INT` | SEC-006…, INT-024… | MASTER-PROMPT |
| B WBS & Budget | `WBS` (new), `BUD`, `REV` | WBS-001…, BUD-020…, REV-008… | MASTER-PROMPT (WBS absent from the PDF) |
| C Approval engine | `PRC` | PRC-003… | MASTER-PROMPT |
| D Purchase Request | `PRC` | PRC-020… | MASTER-PROMPT |
| E Purchase Order | `PO` | PO-010… | MASTER-PROMPT |
| F GRN sync | `GRN` | GRN-002… | MASTER-PROMPT |
| G Vendor Bill sync | `BILL` | BILL-003… | MASTER-PROMPT |
| H Integration platform | `INT` | INT-024… | MASTER-PROMPT |
| I Dashboards & reports | `RPT` | RPT-009… | MASTER-PROMPT |
| J Production enhancements | `SEC`, `ALT` | SEC-020…, ALT-008… | MASTER-PROMPT |
| **Handwritten client notes** | SEC, INT, WBS, BUD, REV, PRC, PO, RPT | **13 IDs — see Annex A.2** | **`CLIENT-NOTES-2026-08-28`** |
| **Product-owner instruction** | GRN, BILL | **2 IDs** — `REQ-GRN-002`, `REQ-BILL-003` | **`PRODUCT-OWNER-REQUEST-2026-08-28`** |
| Enhancements originating with me | any | suffixed `-P` | **PROPOSED-PENDING-SIGNOFF** |

**Precedence rule.** Where a client-note requirement and a master-prompt requirement cover the same ground, the **client-note ID is authoritative** and the master-prompt ID is recorded as elaborating it via a `elaborates` link in `C14_traceability.json` — never merged away, so the provenance of each statement stays traceable. Annex A.2 records every such link.

### Matrix

`research/30_contracts/C14_traceability.json`, one row per requirement:
`requirement_id | provenance | screens[] | api_routes[] | tables[] | services[] | tests[] | acceptance_criteria | phase | status`

| Req | Screens | API | Tables | Service | Tests | Acceptance |
|---|---|---|---|---|---|---|
| `REQ-BUD-001` availability enforced before commitment | SCR-13, SCR-09 | `POST /api/budget-check` | `budget_ledger_cell`, `budget_control_cell`, `budget_line` | `ledger_cell.check_and_reserve` | `test_aud_c_001_*` (11), `test_aud_c_002_*` (9), `test_available_equals_budget_minus_actual_commitment_and_reservation` | Over-budget → 409 and the line amount provably unchanged; two concurrent amendments at 60% of availability → exactly one succeeds |
| `REQ-BUD-004` original budget immutable | SCR-09, SCR-10 | `POST /api/budget-revisions` | `budget_line` | `services.create_revision` | `test_aud_c_005_original_budget_row_cannot_be_updated / _deleted / _moved_to_another_head` | DB refuses UPDATE and DELETE on `kind='ORIGINAL'` independently of the service layer |
| `REQ-SEC-00n` row-level scope | all 40 | all | all scopable | `scope.Scope.predicate`, `repo.query` | `test_scope_enforcement.py`, `test_pool_isolation.py` | Per role: out-of-scope read → 404, write → 403, across API **and** export **and** search; AST gate fails an injected unscoped `.execute(`; no scope leaks across pooled connections |
| `REQ-GRN-00n` receive→PO-line attribution | SCR-16, SCR-27 | `GET /api/grns` | `grn_line`, `reconciliation_exception` | `integration.sweeps.po_anchored` | `test_grn_attribution.py` | Every receive line resolves to a known `po_line` or lands in `reconciliation_exception`. **Zero pro-rata spreading, zero silent drops** |

**CI gate (Phase 0A):** `tests/test_contracts.py` fails the build on any requirement with an empty `tests[]`, any status literal absent from its registry, any CSS custom property absent from `C6_tokens.json`, any `MSG-*` absent from `C10_messages.json`, any role absent from `C9_roles.json`, or **any requirement lacking a provenance value**. Closes **AUD-M-001**.

---

## 5. Assumptions, conflicts and client decisions

### Two contradictions never to smooth over

1. **The client PDF classifies Purchase Request as a "Standard" Zoho ERP capability; no public PR API exists.** Now settled with evidence: the complete module indexes of Zoho ERP v3 (~86 sections), Zoho Books v3 and Zoho Inventory v1 were retrieved in full and none contains a Purchase Request or Requisition module; `/purchase-requests/`, `/purchaserequests/`, `/requisitions/` all 404 on the ERP docs. **But `C13_conflicts.json::CONF-01` records the correct nuance and it must be preserved: the PDF's "Standard" classification is about product/UI functionality and licensing, while the finding is about public REST API availability. A module can exist in the Zoho ERP UI as standard and still have no documented public API — the resolution may be "both true", not "one wrong".**

**The handwritten notes strengthen the "both true" reading.** Item 4 reads *"PR creation field same as Zoho ERP"* — the client is asking for **field parity with a Zoho ERP Purchase Request screen they can evidently see**. That is independent corroboration that a PR module exists in the ERP **UI**, which is entirely consistent with there being no documented REST endpoint for it. Phase 0B-2 confirms this against the tenant and captures the field list, because that field list is now a requirement input (`REQ-PRC-004`) even though it does not change the integration direction: we still cannot post a PR to Zoho, and WBS remains the system of record.
2. **The client PDF has no WBS at all** (project + budget head only). Multi-level WBS is `MASTER-PROMPT` provenance and is never presented as a confirmed client requirement.

### Blocking decisions

| # | Decision | Blocks | Recommendation |
|---|---|---|---|
| **D-6** | Row-level scope rule per business role: is Project Manager project- or plant-scoped? Does Plant Head cross entities? Is Read-only Management User entity- or globally-scoped? | Phase 3 | — |
| **D-12** | Sign-off mapping the 13 business roles to the permission set | Phase 3 | — |
| **D-9** | Accounting calendar: fiscal year start, period length, soft vs hard close, late-document handling | Phase 2 | Monthly; soft close +5 days; hard close +10; late documents post to the next open period |
| **D-7** | Does the client's Zoho ERP plan support **line-level** custom fields or reporting tags on PO lines? | Phase 5 | Required because `po_line` is keyed on `(wbs_id, budget_head_id)` and multi-WBS POs are normal. Header-only forces **one PO per control cell** — a procurement process change requiring explicit acceptance |
| **D-8** | Can direct-in-Zoho PO creation against CAPEX vendors/accounts be blocked by ERP role configuration? | Phase 5 completeness claim | Detective control ships regardless (§11.7) |
| **D-14** | **Which Zoho product does the client actually run — ERP, or Books+Inventory?** | §11, Phase 0B | **Zoho ERP v3 is the PROVISIONAL target and is not treated as settled.** The handwritten notes say "Zoho ERP" throughout (items 4 and 5), and the engagement has always been framed as Zoho ERP — but a client saying "Zoho ERP" colloquially may be running Books + Inventory, and the two have different base URLs, scope prefixes, data-centre availability, plan ceilings and endpoint coverage. Nothing is built against ERP specifics until `GET /organizations` confirms the product, edition and plan tier |
| **D-15** | **Data classification for GSTIN, PAN and vendor contact details** | Phase 4 | §10.4 proposes a default classification for approval |
| **D-16** | **Outcome of the §2.4 connectivity gate** | Phases 1+ | Not a client preference — a platform fact to be established |

### Non-blocking — defaults applied

| # | Decision | Default |
|---|---|---|
| **D-1** | PR reservations | **ON**, 30-day configurable expiry, `FF_PR_RESERVATION`. Without it two requestors can each pass `budget_check` against the same rupees |
| **D-5** | Foreign currency | Budgets in INR; translate at bill date; no CWIP revaluation. Phase 7 |
| **D-10** | Approval matrix content | Seeded default; configuration not code. Blocks go-live, not build |
| **D-11** | Retention | Audit indefinite; raw inbox payloads 400 days then metadata-only |
| **D-13** | `C6_tokens.json` declares `dark_mode`; `styles.css` implements only `@media (forced-colors:active)` | Confirm deferred vs incomplete |

---

## 6. Data model

### 6.1 Conventions

Surrogate PK; business key as a separate unique constraint; `external_source` + `external_id` + `external_last_modified` + `payload_sha` on every Zoho-mirrored table with `UNIQUE (external_source, external_id)`; `created_at/by`, `updated_at/by`, `version_no`; `effective_from`/`effective_to` where temporal; soft delete only where justified, never on financial documents. Money is `bigint` paise. Timestamps `timestamptz`.

### 6.2 The 18 triggers, re-expressed

**18 triggers → 3 triggers + 9 CHECKs + 7 composite FKs + 1 privilege change + 1 column deletion.** Declarative forms are strictly stronger: a composite FK also covers `UPDATE` and `ON DELETE`, which several triggers did not.

| SQLite trigger | PostgreSQL form |
|---|---|
| `budget_line_original_immutable` | **Trigger retained** — a CHECK cannot see `OLD`. `BEFORE UPDATE … WHEN OLD.kind='ORIGINAL'` |
| `budget_line_no_delete` | Same trigger `BEFORE DELETE` + `REVOKE DELETE … FROM capex_app` |
| `audit_log_append_only_update` / `_delete` | **Privilege.** Owned by `capex_audit_owner`; app has `INSERT, SELECT` only. Triggers retained as belt-and-braces |
| `wbs_no_self_parent_insert` | `CHECK (parent_wbs_id IS NULL OR parent_wbs_id <> wbs_id)` — now covers UPDATE too |
| `wbs_no_cycle_update` | **Trigger reshaped.** `wbs_path ltree NOT NULL` maintained by `wbs_path_maintain()`; cycles structurally impossible. `CREATE INDEX … USING gist (wbs_path)`; also serves §7 |
| `wbs_parent_same_project` | **Composite FK** `(parent_wbs_id, project_id) → wbs_element (wbs_id, project_id)` |
| `bill_line_po_ownership_insert` / `_update` | **Composite FK.** Denormalise `po_id`; FK `(po_line_id, po_id, wbs_id, budget_head_id) → po_line` `ON UPDATE RESTRICT` — additionally stops a `po_line` being re-pointed underneath a bill, which the trigger pair did not cover |
| `po_line_project_ownership` | **Composite FK.** Denormalise `project_id`; FKs to `wbs_element` and `purchase_order` |
| `grn_line_po_ownership` | **Composite FK** `(po_line_id, po_id) → po_line` |
| `pr_project_wbs_consistency` | **Composite FK** `(wbs_id, project_id) → wbs_element` |
| `po_line_non_negative` | `CHECK (amount_paise >= 0 AND non_creditable_tax_paise >= 0 AND freight_paise >= 0)` |
| `wbs_progress_range_insert` / `_update` | `CHECK (progress_pct BETWEEN 0 AND 100)` — one constraint replaces two triggers |
| `wbs_flags_follow_state` | **Delete the columns**; replace with view `v_wbs_permits` |
| `asset_allocation_positive` | `CHECK (amount_paise > 0)` |
| `asset_allocation_writeoff_reason` | `CHECK (NOT is_writeoff OR btrim(coalesce(writeoff_reason,'')) <> '')` |

The three partial unique indexes port **1:1** — PostgreSQL supports `CREATE UNIQUE INDEX … WHERE …` natively. This corrects part of AUD-H-009: the rewrite burden is triggers, and most of it is deletion.

New, with no SQLite equivalent:
```sql
ALTER TABLE approval_delegation ADD CONSTRAINT no_overlapping_delegation
  EXCLUDE USING gist (delegator_user_id WITH =, scope_key WITH =, active_range WITH &&)
  WHERE (revoked_at IS NULL);
```

**Runbook note:** FK/CHECK enforcement can be suppressed by `session_replication_role='replica'`, which needs superuser. `capex_app` is never superuser and never a table owner; migrations run as `capex_migrator`.

### 6.3 New table groups

**Organisation:** `entity`, `plant`, `location`, `zone`, `division`, `branch`, `department`, `reporting_unit`.

**Numbering** — collision-safe and immutable once issued. `numbering_series`, `numbering_counter (series_id, entity_id, fiscal_year, next_value)` incremented by `UPDATE … RETURNING` under the row lock, with issued values written to append-only `numbering_issued`. **This replaces the POC's `SELECT COUNT(*)+1` in `create_pr` and `create_revision`, which is not collision-safe.**

**Config:** `custom_field_def`, `custom_field_applicability`, `custom_field_value`, `accounting_period`.

**Masters:** `item_master`, `vendor_master` — each with `source ∈ ZOHO | LOCAL`, `source_of_truth_status`, `duplicate_of`, `mapping_status`, plus India tax identity on vendors (`gst_no`, `gst_treatment`, `place_of_contact`, `pan_no`) handled per §10.4.

**Approval:** §9. **Integration:** §11.8. **Access:** `role_grant`, `user_scope_grant`, `delegation`.

### 6.4 Audit chain in PostgreSQL

**(a) Per-stream chains.** A single global chain serialises every mutation on one lock. `audit_log` gains `stream_key` + `seq`, `UNIQUE (stream_key, seq)`. `audit()` keeps its signature and **payload format verbatim**, gaining `SELECT pg_advisory_xact_lock(hashtext('capex.audit:'||stream_key))` before reading `prev_hash`. **`audit_id` (identity) is not the chain order** — identity values are assigned before commit and can commit out of order. `seq` under the lock is the order.

**(b) Daily anchors** restore the global property. `audit_anchor(anchor_date PK, stream_heads jsonb, prev_anchor_hash, anchor_hash)`; a nightly chunked job hashes every stream head into one root. A two-level Merkle structure, strictly stronger than the single chain because the anchor can be exported off-platform — making tampering detectable even by someone with full database control.

**(c) A verification job, not just an endpoint.** Nightly across all streams and anchors, P1 alert on `intact=False`. Today it runs only when someone calls `GET /api/audit/verify`.

---

## 7. The control cell, and the multi-cell locking proof

### 7.1 The problem

`domain.budget_check` calls `compute_ledger(con, project_id)` — six whole-table queries plus a Python rollup — to answer one question about one cell, while holding a database-wide write lock. Latency problem, throughput ceiling of roughly one check at a time, and lock-scope problem.

### 7.2 Materialised cells

```sql
CREATE TABLE budget_ledger_cell (
  wbs_id text NOT NULL, budget_head_id text NOT NULL,
  project_id text NOT NULL, entity_id text NOT NULL, plant_id text NOT NULL,
  wbs_path ltree NOT NULL,
  budget_paise bigint NOT NULL DEFAULT 0,          -- approved AND effective in the open period
  original_paise bigint NOT NULL DEFAULT 0,
  revisions_paise bigint NOT NULL DEFAULT 0,
  future_budget_paise bigint NOT NULL DEFAULT 0,   -- approved, effective in a FUTURE period
  ordered_paise bigint NOT NULL DEFAULT 0,
  commitment_paise bigint NOT NULL DEFAULT 0,
  actual_paise bigint NOT NULL DEFAULT 0,
  received_paise bigint NOT NULL DEFAULT 0,
  received_not_billed_paise bigint NOT NULL DEFAULT 0,
  pr_reserved_paise bigint NOT NULL DEFAULT 0,
  recomputed_at timestamptz NOT NULL,
  PRIMARY KEY (wbs_id, budget_head_id));
CREATE INDEX ix_cell_subtree ON budget_ledger_cell USING gist (wbs_path);

CREATE TABLE budget_control_cell (       -- precomputed budget_owner_by_head
  wbs_id text NOT NULL, budget_head_id text NOT NULL,
  owner_wbs_id text NOT NULL, owner_path ltree NOT NULL,
  PRIMARY KEY (wbs_id, budget_head_id));
```

Only own-cell figures are stored. Rolled-up figures are computed at check time by an ltree subtree scan touching only cells (hundreds per project), never transaction history.

### 7.3 v1.0 had a correctness hole

v1.0 locked **only the nearest budget-owning ancestor**. That is insufficient, and the correction matters.

Consider head `H`. `WBS-01` carries budget for `H`. `WBS-01.02` *also* carries budget for `H`. A spend on `WBS-01.02.03` resolves its owner to `WBS-01.02`. But `compute_ledger` rolls **exposure** up the whole tree, so that spend also increases `WBS-01`'s rolled exposure and therefore reduces `WBS-01`'s available — **without ever taking `WBS-01`'s lock**. Two concurrent spends, one under `WBS-01.02` and one directly on `WBS-01`, could each pass their own check and jointly breach `WBS-01`'s budget. That is AUD-C-001 re-opened one level up.

### 7.4 The corrected lock set, and its proof

**Definition.** For a transaction touching affected cells `A = {(w₁,h₁), …, (wₙ,hₙ)}`, the lock set is

```
L = ⋃ over (w,h) ∈ A of  { (a, h) : a is an ancestor-or-self of w
                                    AND budget_ledger_cell(a,h).budget_paise ≠ 0 }
```

— every budget-owning ancestor cell on every affected chain, not just the nearest.

```python
# app/backend/ledger_cell.py
def lock_affected_cells(con, affected: list[tuple[str, str]]) -> dict:
    """Lock every budget-owning ancestor cell of every affected (wbs, head),
    in ascending (wbs_path, budget_head_id) order. Root-first, deterministic."""
    rows = con.execute("""
        WITH affected(wbs_id, budget_head_id) AS (SELECT * FROM unnest(%s::text[], %s::text[])),
        chain AS (
          SELECT c.wbs_id, c.budget_head_id, c.wbs_path
            FROM budget_ledger_cell c
            JOIN affected a ON a.budget_head_id = c.budget_head_id
            JOIN budget_ledger_cell t ON t.wbs_id = a.wbs_id
                                     AND t.budget_head_id = a.budget_head_id
           WHERE c.wbs_path @> t.wbs_path        -- ancestor-or-self
             AND c.budget_paise <> 0)
        SELECT DISTINCT wbs_id, budget_head_id, wbs_path FROM chain
         ORDER BY wbs_path, budget_head_id
           FOR UPDATE""", (wbs_ids, head_ids))
    ...
```

**Proof of deadlock freedom.**
1. *Total order.* `(wbs_path, budget_head_id)` is a total order over `budget_ledger_cell`: `wbs_path` is unique per `wbs_id` (maintained by `wbs_path_maintain()` and enforced by the PK), `budget_head_id` breaks ties, and `ORDER BY … FOR UPDATE` makes PostgreSQL acquire the locks in that order.
2. *Every transaction obeys it.* Every mutating service function calls `lock_affected_cells` **exactly once**, as its first locking action, with the complete affected set. No cell lock is ever acquired afterwards — enforced by `test_lock_ordering.py` (below).
3. *Standard result.* Concurrent transactions acquiring locks in a common total order cannot deadlock: a cycle would require some transaction to hold a higher-ordered lock while waiting on a lower-ordered one, which the order forbids.
4. *Chains sharing a prefix are consistent.* Two chains under a common ancestor include that ancestor's cell, and it sorts first in both — so the shared prefix is always acquired first, in the same order, by both.
5. *Global ordering across lock classes* is unchanged: (1) cells, (2) the document row via `_bump`'s `FOR UPDATE`, (3) `pg_advisory_xact_lock` for the audit stream, taken last inside `audit()`, already the final call in every service function.

**Proof of completeness.** `available` at any cell derives from `budget − exposure` where both are subtree sums. A write changes exposure at exactly the cells in `A`, so it can change `available` **only** at cells whose subtree contains some `(w,h) ∈ A` **and** which carry budget for `h` — precisely `L`. Cells with `budget_paise = 0` carry no availability to breach and are correctly excluded.

**Tests that hold this.**
- `test_lock_ordering.py` — AST-inspects `services.py`: every mutating function calls `lock_affected_cells` exactly once, before any `_derive`/availability read, and never afterwards.
- `test_nested_budget_owner_concurrency.py` — the §7.3 scenario: budget at both `WBS-01` and `WBS-01.02`; concurrent spends at both levels sized so that either alone passes but both together breach `WBS-01`; assert exactly one succeeds and `WBS-01`'s rolled `available` never goes negative. **This test fails against v1.0's design, which is why the correction is load-bearing.**
- `test_multiline_po_deadlock.py` — N workers amending multi-line POs spanning overlapping cell sets in randomised order; assert zero deadlocks over a sustained run.

### 7.5 Why `FOR UPDATE` and not `SERIALIZABLE` + retry

A retry loop must be safe to re-execute, but `services.py` calls `now()`, `uuid4()` and count-derived sequence numbers; making those deterministic across retries is real work with no offsetting benefit. Existing tests distinguish `409 VERSION_CONFLICT`; under `SERIALIZABLE` they would see driver-level `40001` at unpredictable points. And `SERIALIZABLE` predicate locks over tables scanned by `compute_ledger` would have a near-100% false-positive rate — serialising everything, which is what we are removing. `FOR UPDATE` preserves the observable behaviour of `BEGIN IMMEDIATE` with a far smaller blast radius.

### 7.6 Accounting periods — closes AUD-C-005 residual

`budget_paise` depends on `effective_date <= today()`, so a cell's value changes with the wall clock, not only with writes — a naive cache would be wrong at midnight. Fixing the cache and the missing cut-off calendar is one fix.

```sql
CREATE TABLE accounting_period (
  period_id text PRIMARY KEY, entity_id text NOT NULL,
  period_start date NOT NULL, period_end date NOT NULL,
  state text NOT NULL CHECK (state IN ('FUTURE','OPEN','SOFT_CLOSED','CLOSED')),
  closed_at timestamptz, closed_by text,
  EXCLUDE USING gist (entity_id WITH =, daterange(period_start, period_end, '[]') WITH &&));
```

`roll_period_effective_budget(period_id)` moves amounts between `budget_paise` and `future_budget_paise` at period open, in one transaction, one entity per invocation. `domain.budget_check(..., as_of=...)` keeps working against `compute_ledger` for historical questions.

### 7.7 Correctness versus the existing tests

1. `_derive`, `_sum_cells`, `_blank`, `_COMPONENTS`, `WATCH_PCT`, `CRITICAL_PCT` are **untouched**; both the materialised and oracle paths call the same `_derive`.
2. `compute_ledger` remains and remains tested.
3. **`tests/test_ledger_cell_equivalence.py`** is load-bearing: an autouse fixture asserting after *every* mutation in the whole suite that each cell equals `compute_ledger`'s `heads` component-by-component and the subtree sum equals `head_totals`. All 220 tests become equivalence tests for free.
4. **Runtime dual-run** behind `FF_LEDGER_CELL_READ`: while off, `budget_check` derives as today *and* computes from cells, logging any divergence as `LEDGER_DIVERGENCE`. The flag flips only after a clean soak. The derivation path never leaves the codebase.
5. `test_aud_m_005_deep_wbs_ledger_does_not_raise_recursion_error` (1,100-deep): ltree handles it natively; `MAX_WBS_DEPTH = 100` becomes a data-quality `CHECK (nlevel(wbs_path) <= 100)`, turning the deep case from a slow read into a rejected write. **This is a deliberate change in that test's shape** — an improvement, but it must be approved under §15.1, not slipped in.

---

## 8. Status registries — separated

The 21 codes in `C3_statuses.json` are **client-visible business statuses** and remain frozen. v1.0 wrongly implied everything maps into them. Four registries, with controlled mappings.

### 8.1 Business statuses — `C3_statuses.json`, frozen, 21 codes

DRAFT, SUBMITTED, UNDER_REVIEW, APPROVED, REJECTED, RETURNED, RELEASED, PARTIALLY_COMMITTED, FULLY_COMMITTED, PARTIALLY_ACTUALISED, FULLY_ACTUALISED, BUDGET_EXCEEDED, EXCEPTION_PENDING, TECHNICALLY_COMPLETED, FINANCIALLY_COMPLETED, AWAITING_CAPITALISATION, CAPITALISED, CLOSED, REOPENED, INTEGRATION_FAILED, RECONCILIATION_PENDING. No synonyms; each needs a non-colour indicator. **Only these appear on business screens.**

### 8.2 Approval engine statuses — `C15_approval_statuses.json` (new)

Instance: `OPEN, APPROVED, REJECTED, RETURNED, RECALLED, SUPERSEDED, EXCEPTION_PENDING`.
Stage: `PENDING, APPROVED, REJECTED, RETURNED, SKIPPED, ESCALATED`.
Assignment: `PENDING, ACTED, WITHDRAWN`.
Internal. Surfaced on approval screens only, and **mapped** to a business status on the object.

### 8.3 Integration job statuses — `C16_integration_statuses.json` (new)

Inbox: `RECEIVED, PROCESSED, QUARANTINED, DISCARDED, DEAD`.
Outbox: `PENDING, SENT, FAILED, DEAD`.
Job: `PENDING, CLAIMED, CHECKPOINTED, DONE, FAILED, DEAD`.
Circuit: `CLOSED, OPEN, HALF_OPEN`.
Operational. Visible on SCR-26/38/39 to administrators. **Never rendered on a business screen** except through the mapping below.

### 8.4 Raw Zoho statuses — `C17_zoho_status_map.json` (new)

Stored **verbatim, never overwritten**, in `external_status_raw` on every mirrored row, alongside the product and API version that produced it. Example rows (ERP/Books/Inventory kept separate):

| Product | Object | Raw value | → Business status |
|---|---|---|---|
| ERP | Purchase Order | `draft` | DRAFT |
| ERP | Purchase Order | `open` | RELEASED |
| ERP | Purchase Order | `billed` | FULLY_ACTUALISED |
| ERP | Purchase Order | `cancelled` | CLOSED |
| Inventory | Purchase Receive | `received_status=in_transit` | *(no business status — operational only)* |
| Inventory | Purchase Receive | `billed_status=partially_billed` | PARTIALLY_ACTUALISED |
| ERP/Books | Bill | `void` | *(drives `accounting_status=Void`, not a business status)* |

**Registry rules.** Every mapping is data, versioned and effective-dated. An **unmapped** raw value never guesses: the record is accepted, the raw value stored, and a `reconciliation_exception` of kind `UNMAPPED_EXTERNAL_STATUS` is raised — the same fail-closed philosophy as `lifecycle_permits`. A contract test asserts every mapping target exists in `C3_statuses.json` and that no integration or approval status leaks into a business-screen renderer.

**Queued-for-Zoho.** The §2.3 latency consequence needs a user-visible state. Rather than adding a 22nd business status, the object keeps its business status and carries a separate `integration_state` badge (`QUEUED`, `SENT`, `FAILED`) sourced from 8.3 — which is exactly why the registries are separated.

---

## 9. Approval engine

### 9.1 Model

```
approval_definition(definition_id, object_type, code, version, status ∈ DRAFT|ACTIVE|RETIRED,
                    entity_id NULL, effective_from, effective_to, UNIQUE(object_type,code,version))
approval_rule(rule_id, definition_id, priority, predicate jsonb, UNIQUE(definition_id,priority))
approval_stage(stage_id, definition_id, stage_no, name, parallel_group NULL,
               quorum_type ∈ ALL|ANY|N_OF_M|PERCENT, quorum_n, applies_when jsonb,
               sla_hours, escalate_after_hours, escalate_to jsonb,
               allow_delegation, requires_reason, reason_code_set)
approval_stage_approver(stage_id, ordinal, approver_kind ∈ ROLE|USER|ATTRIBUTE,
                        approver_ref, scope_expr jsonb)
approval_instance(instance_id, object_type, object_id, object_version, object_content_sha,
                  definition_id, definition_version, status, current_stage_no,
                  snapshot jsonb, supersedes_instance_id, maker_user_id,
                  opened_at, closed_at, correlation_id)
approval_stage_instance(stage_instance_id, instance_id, stage_no, parallel_group, status,
                        skip_reason, quorum_required, quorum_met, opened_at, due_at,
                        escalated_at, closed_at, UNIQUE(instance_id, stage_no))
approval_assignment(assignment_id, stage_instance_id, assignee_user_id,
                    assigned_via ∈ ROLE|USER|DELEGATION|ESCALATION, delegated_from, state,
                    UNIQUE(stage_instance_id, assignee_user_id))
approval_action(action_id, stage_instance_id, instance_id, actor_user_id, acting_for_user_id,
                action ∈ APPROVE|REJECT|RETURN|RECALL|REASSIGN|COMMENT|ESCALATE|WITHDRAW,
                reason_code, reason_text, at, seq, prev_hash, entry_hash)   -- APPEND-ONLY
approval_delegation(delegation_id, delegator_user_id, delegate_user_id, scope_key,
                    active_range daterange, created_by, revoked_at, revoke_reason)
reason_code(code PK, applies_to_action, applies_to_object_type, label,
            requires_free_text, active)
```

`approval_action` gets the `audit_log` privilege treatment — owned by `capex_audit_owner`, `INSERT`+`SELECT` only, hash-chained on stream `approval:{instance_id}`, anchored daily. **Immutable decision history becomes a property of the database, not of discipline.**

### 9.2 Resolution algorithm

```
1. Candidates: ACTIVE, object_type matches, business_date within effectivity,
   entity_id matches or NULL. Entity-specific ordered before global.
2. First definition whose first matching rule (priority ASC) evaluates true wins.
3. No match -> ApprovalRouteUnresolved. FAIL CLOSED.
   An unroutable object is a configuration defect, NEVER an auto-approval.
4. A stage whose applies_when is false is recorded SKIPPED with skip_reason,
   never omitted — an auditor must see what did not run.
5. Assignees:
   a. expand ROLE refs against role_grant + scope_expr
   b. add live delegates; the delegator's assignment STAYS PENDING
      (delegation adds capacity, never silently removes accountability)
   c. REMOVE every user in contributor_set(object)        <-- self-approval bar
   d. empty set -> EXCEPTION_PENDING + NoIndependentApprover.
      NEVER auto-approve an empty stage.
6. quorum: ALL -> |set|; ANY -> 1; N_OF_M -> n; PERCENT -> ceil(pct * |set|)
```

`predicate` is a restricted JSON AST (`and, or, not, ==, !=, <, <=, >, >=, in, not_in`), operands are dotted snapshot paths or literals. **Money operands are integer paise only** — the compiler rejects a float literal in a `*_paise` comparison, mirroring `to_paise`. No user code, no regex, no arbitrary attribute access.

**Activation-time validation:** no duplicate priorities, no unreachable rule, every `approver_ref` resolves to a live role in `C9_roles.json`, `quorum_n ≤ |approver set|` for every reachable snapshot class, plus a shadow evaluation over 90 days of real objects reporting which rule each would have hit. A definition cannot become ACTIVE unless validation passes.

### 9.3 Worked resolution

`APDEF-POAMD` v3, entity `ENT-01`, ACTIVE from 2026-04-01. Rules: p10 `delta_paise ≥ 50000000 OR asset_category='LAND'` → COMMITTEE; p20 `delta_paise ≥ 10000000` → CFO; p30 `true` → STANDARD.

`amend_po` on `PO-014`/`POL-031`, ₹22,00,000 → ₹40,00,000, `delta_paise = 18000000`; `budget_check` on the delta → `EXCEEDS_BUDGET`, shortfall ₹5,00,000. Maker `U-PROC`.

| Step | Result |
|---|---|
| p10 | `18000000 ≥ 50000000` false; `'PLANT'=='LAND'` false → no match |
| p20 | `18000000 ≥ 10000000` **true → `APDEF-POAMD-CFO` v3** |
| Stage 1 Project Finance Controller | PENDING, quorum ALL, scoped `project_id='PRJ-01'` → {U-PFC}, SLA 24h → escalates to ROLE Finance |
| Stage 2 Plant Head (group G2) | PENDING, scoped `plant_id='PLT-02'` → {U-PLH} |
| Stage 3 Procurement (group G2) | ROLE Procurement → {U-PROC} → **after contributor filter → ∅** |
| Stage 4 CFO exception | `applies_when: check_verdict=='EXCEEDS_BUDGET'` true → PENDING, quorum ANY → {U-CFO}, `requires_reason` |
| Stage 5 CAPEX Committee | `delta_paise ≥ 50000000` false → **SKIPPED**, `skip_reason='RULE_NOT_MET'`, visible on SCR-03 |

Stages 2 and 3 share `parallel_group='G2'` — both open at once, group completes when both meet quorum, in either order. Stage 3 hits 5(d): the only Procurement holder is the maker. The engine **does not skip and does not auto-approve** — instance → `EXCEPTION_PENDING`, `NO_INDEPENDENT_APPROVER`, surfaced on SCR-25.

After remediation and approvals, `amend_po` **still re-runs `budget_check` inside the lock at write time**, because availability may have moved — the pattern `approve_pr` already uses to raise `BUDGET_MOVED`.

### 9.4 Maker-checker folds in without weakening

**`auth.MAKER_CHECKER` and `require_separation` are not modified and not removed.**
1. `approve_pr`, `approve_revision`, `approve_capitalisation`, `void_bill` keep their existing `require()` + `require_separation()` calls in the same order, before any engine call. Every existing self-approval test passes untouched.
2. `approval/engine.py::act()` calls `require_separation` **again** at decision time — a second independent enforcement point.
3. `contributor_set(object)` = maker + editors + prior actors where `bar_repeat_approver`. A **superset**: it can only refuse more, never less.
4. Delegation checks **both** identities — rejected if `actor_user_id` **or** `acting_for_user_id` is in `contributor_set`. A delegation can never launder a self-approval.
5. `tests/test_approval_separation.py`: for every permission in `MAKER_CHECKER`, a stage *configured to allow the maker* still yields `403 SELF_APPROVAL`. **This fails if anyone ever "simplifies" by deleting the `services.py` call** — the protection the existing tests need.

---

## 10. Security, identity, scope and data classification

### 10.1 Row-level scope — compiler primary, RLS backstop

RLS alone is rejected as primary: the model is entity ∪ plant ∪ project ∪ WBS-subtree ∪ role-override, not `row.owner = current_user`, so policies would duplicate the logic in a second language with nothing checking agreement; and RLS returns zero rows silently, whereas this product's character is actionable refusals with message IDs. RLS alone is also insufficient — but it is excellent at catching a developer who forgets.

```python
# app/backend/scope.py
@dataclass(frozen=True)
class Scope:
    principal_kind: Literal["USER", "SERVICE"]
    user_id: str
    entity_ids: frozenset[str] | ALL
    plant_ids: frozenset[str] | ALL
    project_ids: frozenset[str] | ALL
    wbs_paths: tuple[str, ...] | ALL
    read_all: bool
    def predicate(self, alias: str, anchor: str) -> tuple[str, dict]: ...
```

The composite-FK work in §6.2 already puts `project_id` on `po_line`, `bill_line` and `grn_line`, so the anchor exists everywhere with no new denormalisation.

**Chokepoint** — `repo.query()` requires a literal `{scope}` token and raises without it. **Enforcement is a build gate:** `tests/test_scope_enforcement.py` AST-walks `app/backend/`, finds every `Call` whose func is `Attribute(attr='execute')`, and asserts the module is in `{repo.py, migrate.py, ledger_cell.py, db.py}`. A new `.execute(` in a route fails CI. `main.py`'s `rows()` helper is deleted and its ~20 call sites migrated.

`ledger_cell.py` is allowlisted deliberately: the ledger must compute over all cells in a subtree regardless of the requester's scope, because a plant-scoped user still needs the correct project-level available budget in order to be told "budget for this head is held at WBS-001". It never returns raw rows to a caller.

**Not-found over forbidden.** Out-of-scope *reads* → 404 with a generic message (a 403 on a project id is an existence oracle). Out-of-scope *writes* → 403.

### 10.2 Identity, session, secrets and OAuth ownership — named

| Concern | Decision | Rationale |
|---|---|---|
| **Identity provider** | **Zoho Directory via OIDC**, with the client's existing corporate IdP (Entra ID / Google Workspace) federated into it if present. `PROPOSED-PENDING-SIGNOFF` | The client is a Zoho tenant; Zoho Directory already holds the user population that maps to ERP users, so joiner/mover/leaver has one authority. Catalyst also supports its own authentication, but Zoho Directory federates better to an existing corporate IdP |
| **MFA** | Enforced at the IdP, not the app | The app never handles a second factor |
| **Session** | Server-side session row in PostgreSQL keyed by an opaque, high-entropy id in an `HttpOnly; Secure; SameSite=Lax` cookie. Absolute 8 h (retaining `SESSION_HOURS`), idle 30 min, rotation on privilege change, server-side revocation. **No JWT** | AppSail scale-to-zero means no in-process session state can survive; a database session table is required anyway. A stateless JWT would make revocation unenforceable, which is unacceptable where maker-checker depends on identity |
| **Local password auth** | Retained as a disabled-by-default break-glass path with policy and lockout | `auth.py`'s PBKDF2 implementation stays for that path only |
| **Secrets manager** | **Catalyst Secret Management / AppSail environment configuration** for runtime secrets; **`pgcrypto` envelope encryption keyed from that store** for anything persisted. `key_id` recorded on every ciphertext for rotation | Keeps the platform to one vendor for the deployment surface. Spiked in Phase 0B-1 alongside connectivity |
| **OAuth token owner** | **Catalyst Connections**, adopted where supported — see below | It removes the hand-rolled refresh lifecycle entirely |

**Catalyst Connections — evaluated as directed, and partially adopted.**

Confirmed: Connections stores the connection, caches the access token, and *"Each time the Access Token expires, the connector automatically fetches and caches a new token in the background."* It supports **Zoho Books as a default connector**, and arbitrary services via **Custom Service** (API Key / Basic / OAuth2) with scopes declared at setup.

| Target | Mechanism | Confirmed |
|---|---|---|
| Zoho Books | Default connector | YES |
| Zoho Inventory | **Not in the default list** → Custom Service | NOT CONFIRMED as default |
| **Zoho ERP** | **Not in the default list** → Custom Service | NOT CONFIRMED as default |

**Decision: adopt Connections as the token lifecycle owner**, via Custom Service for ERP/Inventory, subject to a Phase 0B-1 proof. This is materially better than v1.0's hand-rolled design because AppSail's scale-to-zero makes the v1.0 "in-process 3300 s token cache with single-flight lock" **unimplementable** — there is no process to cache in. Connections caches in Catalyst Cache, which survives instance death.

What Connections does **not** remove: the initial OAuth grant (we still generate the first refresh token in the Zoho API console), the **20 refresh tokens per user / 5 per minute** ceiling, and the multi-organisation constraint that *"Each user in a multi-user setup requires a distinct connector name to prevent token overwrites in the cache segment"* — so **connector naming is per (entity, Zoho org)** and is part of the connection model.

**Fallback if the Custom Service proof fails:** hand-rolled refresh in a Cron Function with the token in Catalyst Cache and the refresh token in the secrets manager, single-flight enforced by a PostgreSQL advisory lock rather than an in-process lock. Never in an AppSail instance.

### 10.3 Connection pooling, RLS leakage and service-account scope

Three risks specific to this architecture, each with a test.

**Pool leakage.** RLS is driven by `SET LOCAL capex.*` issued by `repo.session()`. `SET LOCAL` is transaction-scoped and reverts on commit or rollback — which is why it, and never `SET`, is used. Risks: (a) a code path issuing plain `SET`; (b) a pooler in *transaction* mode splitting a logical session; (c) a connection returned to the pool mid-transaction.
- `tests/test_pool_isolation.py` — acquire as user A with a narrow scope, run a scoped query, return to pool; acquire as user B and assert `current_setting('capex.project_ids', true)` is empty **before** `repo.session()` runs, and that a raw `SELECT` without a session sees zero rows under RLS.
- `tests/test_no_plain_set.py` — AST gate rejecting any `SET ` that is not `SET LOCAL` outside `migrate.py`.
- Pooler mode is pinned to **session** or **transaction** explicitly in the deploy config, with a test asserting the configured mode, because `SET LOCAL` correctness depends on it.

**Service-account scope.** Jobs run as principals, not as "no user". Each is a row in `app_user` with `principal_kind='SERVICE'`, an explicit `role_grant`, and an explicit scope:

| Principal | Permissions | Scope |
|---|---|---|
| `SVC-INTEGRATION` | `connector.*`, write to inbox/outbox/external_document, upsert mirrored objects | `read_all=False`; entities with an active connection only |
| `SVC-SWEEP` | read all financial tables, write `reconciliation_exception` | `read_all=True`, **read-only** on business tables — enforced by database `GRANT`, not only by scope |
| `SVC-ANCHOR` | `INSERT` on `audit_anchor`, `SELECT` on `audit_log` | `read_all=True`, no business table access |
| `SVC-EXPORT` | runs an export **under the requesting user's scope**, never its own | inherits the requester's `Scope`, persisted on the export job row |

`SVC-EXPORT` is the important one: a background export must not become a scope-escalation path. The requester's `Scope` is serialised onto the job and rehydrated by the worker, and `tests/test_export_scope.py` asserts an export produced for a plant-scoped user contains no out-of-scope row. **No service account holds any `MAKER_CHECKER` permission** — asserted by a test.

### 10.4 Data classification — GSTIN, PAN and vendor contact details

**v1.0 was wrong to hash these.** `sha256(value)[:16]` is irreversible, and GSTIN and PAN are needed for vendor matching, statutory reporting and audit evidence. Hashing them destroys required function. Withdrawn.

Proposed classification for approval as **D-15**:

| Field | Class | At rest | In transit | In UI | In logs | In export |
|---|---|---|---|---|---|---|
| `gst_no` (GSTIN) | **Regulated — Business Identifier** | Stored in clear in a column; **column-level access restricted** to Finance, Procurement, Auditor, Administrator | TLS | Full to entitled roles; **masked `27ABCDE****1Z5`** to others | **Masked** | Full only in a statutory export, which is a distinct permission and writes an audit entry |
| `pan_no` (PAN) | **Regulated — Tax Identifier** | **Encrypted** via `pgcrypto` envelope, `key_id` recorded | TLS | **Masked `ABCDE****F`** by default; full reveal is a separate permission and writes an audit entry | **Never** | Only in a statutory export |
| Vendor contact name, email, phone | **Confidential — Personal Data** | Clear, access-restricted | TLS | Full to entitled roles | **Redacted** | Full only to entitled roles |
| Vendor bank details | **Restricted** | **Encrypted**, `key_id` recorded | TLS | **Never displayed** in this product | **Never** | **Never** — out of scope; this system does not make payments |
| Raw Zoho payloads in `integration_inbox` | Inherits the highest class of any field within | Regulated/Restricted fields **encrypted or nulled** on persist, with the cleartext kept only in the mapped column that needs it | TLS | Administrator only | Redacted | Not exportable |

Rules: **no irreversible transformation of a field needed for matching, compliance or reporting.** Masking is presentation-layer only, applied by a shared formatter so it cannot be forgotten per screen. Encryption is envelope with recorded `key_id` and a documented rotation procedure. Access restriction is a permission plus the row-level scope, so both must pass. Every full reveal and every statutory export writes an audit entry naming the actor, the field and the reason.

`tests/test_data_classification.py` asserts: no classified field appears in any log formatter's output; masked fields render masked without the reveal permission; encrypted fields are ciphertext on disk; and **no classified field is hashed anywhere** — a regression test against v1.0's mistake.

### 10.5 Application security

CSP retained and tightened (`style-src 'self'` is load-bearing — §14); CSRF tokens on state-changing requests; strict CORS allowlist; validation at the Pydantic boundary with money always via `to_paise`; attachments validated by magic bytes not extension, size-capped, malware-scanned, served from a separate origin with `Content-Disposition: attachment`; rate limiting per session and per IP; existing security headers retained.

---

## 11. Zoho API capability matrix

**Products are never mixed. Books and Inventory documentation is inadmissible as evidence for ERP, and vice versa.** Verified against official documentation 2026-08-28.

> ### ⚠ Target product is PROVISIONAL
>
> **Zoho ERP v3 is the provisional target, pending D-14.** The handwritten notes name "Zoho ERP" and the engagement has been framed that way, but that is not the same as a verified tenant fact — clients commonly say "Zoho ERP" for a Books + Inventory estate. The Books and Inventory rows in §11.2 are therefore **live alternatives, not background**, and are maintained to the same evidentiary standard.
>
> Until `GET /organizations` in Phase 0B-2 returns the product, edition and plan tier:
> - No ERP-specific base URL, scope string or endpoint is written into code.
> - The adapter boundary (§11.10) is built product-agnostic, with ERP and Books+Inventory as two implementations behind one interface.
> - Every ERP-derived statement in this section is marked **[PROVISIONAL-ERP]**.
> - Effort in §18.3 for Phase 5 carries its widest range precisely because of this.
>
> Facts that are **not** provisional because they hold across all three products: `organization_id` as a query parameter; `page`/`per_page` + `page_context.has_more_page`; 100 requests/minute per organisation; 429 codes 44 / 45 / 1070; the absence of any idempotency header; the absence of a standalone webhook registration API; and the absence of a Purchase Request module.

### 11.1 Product-level facts

| | **Zoho ERP** | **Zoho Books** | **Zoho Inventory** |
|---|---|---|---|
| Version | v3 | v3 | v1 |
| Base URL | **`https://www.zohoapis.in/erp/v3` — India only.** No DC table published; a `.com`/`.eu` ERP host is **NOT CONFIRMED** | `https://www.zohoapis.{com\|eu\|in\|com.au\|jp\|ca\|com.cn\|sa}/books/v3` | same 8 hosts, `/inventory/v1` |
| Accounts server | **`https://accounts.zoho.in` only** | `accounts.zoho.com` shown; DC table NOT CONFIRMED | 5 hosts confirmed; JP/CN/SA NOT CONFIRMED |
| Org identification | `organization_id` **query parameter**, every request | same | same |
| Auth header | `Authorization: Zoho-oauthtoken <token>` | same | same |
| **Scope prefix** | **`ERP.`** | `ZohoBooks.` | `ZohoInventory.` |
| Scope syntax | `service_name.scope_name.operation_type` | same | same |
| `.ALL` operation | documented | documented | **NOT documented** — CRUD enumerated individually |
| Rate: per minute | 100/min/org | 100/min/org | 100/min/org |
| Rate: per day | **Standard 2,000 · Premium 10,000 — India offers these two plans only** | Free 1,000 · Std 2,000 · Prof 5,000 · Prem+ 10,000 | Free 1,000 … Ent 10,000 |
| Concurrency | Trial 5, plans 10 (soft) | Free 5, paid 10 | Free 5, paid 10 |
| Error codes | 429 + 44 (per-min), 45 (daily), 1070 (concurrency) | same | same |

The identical per-minute figure across all three was read from each product's own page — a verified coincidence, not an inherited assumption.

### 11.2 Object matrix

| Product | Object | Scope | List | Detail | Create/Update | Delta filter | Confirmed |
|---|---|---|---|---|---|---|---|
| **ERP** | Purchase Order | `ERP.purchaseorders.*` | `GET /purchaseorders` | `/{id}` | `POST/PUT /purchaseorders` | **`last_modified_time` filter** | YES |
| **ERP** | Bill | `ERP.bills.*` | `GET /bills` | `/{id}` | `POST/PUT /bills` | **`last_modified_time` filter** | YES |
| **ERP** | Purchase Receive | `ERP.purchasereceives.*` | **NONE — no collection endpoint** | `/{id}` | `POST`, `PUT /{id}` | n/a | YES |
| **ERP** | Contact / Vendor | `ERP.contacts.*` | `GET /contacts` | `/{id}` | `POST/PUT` | sort only | YES |
| **ERP** | Item | **`ERP.settings.*`** — there is no `ERP.items` scope | `GET /items`, `/itemdetails` | `/{id}` | `POST/PUT` | **none** | YES |
| **ERP** | Custom Module | `ERP.settings.*` + `ERP.custommodules.ALL` | `GET /{module_name}` | `/{module_name}/{id}` | `POST/PUT` | sort only | YES |
| **ERP** | Purchase Request | — | — | — | — | — | **module not documented** |
| **Books** | Purchase Order | `ZohoBooks.purchaseorders.*` | `GET /purchaseorders` | `/{id}` | `POST/PUT` | **`last_modified_time` filter** | YES |
| **Books** | Bill | `ZohoBooks.bills.*` | `GET /bills` | `/{id}` | `POST/PUT` | **`last_modified_time` filter** | YES |
| **Books** | Purchase Receive | — | — | — | — | — | **module not documented** |
| **Books** | Contact / Vendor | `ZohoBooks.contacts.*` | `GET /contacts` | `/{id}` | `POST/PUT` | sort only | YES |
| **Books** | Item | **`ZohoBooks.settings.*`** | `GET /items` | `/{id}` | `POST/PUT` | not a list param | YES |
| **Books** | Custom Module | `ZohoBooks.settings.*` | `GET /{module_name}` | `/{module_name}/{id}` | `POST/PUT` | NOT CONFIRMED | partial |
| **Inv** | Purchase Order | `ZohoInventory.purchaseorders.*` | `GET /purchaseorders` | `/{id}` | `POST/PUT` | **NONE — list accepts only `organization_id`, `page`, `per_page`** | YES |
| **Inv** | Purchase Receive | `ZohoInventory.purchasereceives.*` | `GET /purchasereceives` | `/{id}` | `POST`, `PUT /{id}` | sort only | YES |
| **Inv** | Bill | `ZohoInventory.bills.*` | `GET /bills` | `/{id}` | `POST/PUT` | NOT CONFIRMED | partial |
| **Inv** | Contact / Vendor | `ZohoInventory.contacts.*` | `GET /contacts` | `/{id}` | `POST/PUT` | sort only | YES |
| **Inv** | Item | `ZohoInventory.items.*` | `GET /items` | `/{id}` | `POST/PUT` | **`last_modified_time` filter AND sort**, `yyyy-MM-ddTHH:mm:ssZ` | YES |
| **Inv** | Custom Module | — | — | — | — | — | **module not documented** |

Pagination is uniform across all three: `page` + `per_page`, response `page_context{page, per_page, has_more_page}`.

**A documented defect in Zoho's own ERP OAuth page:** its scope table omits two scopes that appear on the module pages — `ERP.purchasereceives.*` and `ERP.custommodules.ALL`. The consent-screen scope list must be built from the module pages, not the scope table, and this must be re-verified at the tenant.

### 11.3 The `last_modified_time` correction — and its real limit

**Correction accepted: Books `GET /bills` and `GET /purchaseorders` both document a `last_modified_time` filter.** Verbatim from the Books Bills query-parameter table: *"Search bills modified after a specific time… Use YYYY-MM-DDTHH:MM:SS-UTC format."* A `+` offset must be URL-encoded as `%2B`. My v1.0 "NOT CONFIRMED" was a fetch-truncation artifact; the record is corrected. The same filter is confirmed on **ERP** Bills and POs.

**But the correction is narrower than it appears, and the plan must not over-claim it.** On Books/ERP Bills and POs, `last_modified_time` is filterable but is **not** an allowed `sort_column` value (Bills allows `vendor_name, bill_number, date, due_date, total, balance, created_time`). So you can *select* a modification window but cannot *order by* modification time — **a stable resumable keyset walk on modification time is not available on any of them.**

Consequences, which is why overlap and completeness reconciliation are retained exactly as directed:

1. Windowed filtering with a **300 s overlap** on every run, made free by the inbox `payload_sha` dedupe.
2. A **bounded window** per invocation (default 24 h, narrowing on high volume) so a run fits the 15-minute Function ceiling, with the window boundary itself as the checkpoint.
3. **Idempotent upsert**, never append.
4. The **completeness sweeps (§11.5) are retained in full** — they are not made redundant by a working delta filter, because a filter cannot prove it returned everything.

| Endpoint | Filter? | sort_column? | Verdict |
|---|---|---|---|
| ERP / Books `GET /bills` | **YES** | No | Windowed filter + overlap |
| ERP / Books `GET /purchaseorders` | **YES** | No | Windowed filter + overlap |
| ERP `GET /purchasereceives` | **no list endpoint at all** | — | PO-anchored only (§11.4) |
| Inventory `GET /purchasereceives` | No | **YES** | Sort-descending walk with early stop |
| Inventory `GET /purchaseorders` | **No — no filter, no sort** | No | Full re-pull only |
| Books / ERP / Inventory `GET /contacts` | No | **YES** | Sort walk or weekly full refresh |
| Inventory `GET /items` | **YES** | **YES** | True delta |
| ERP / Books `GET /items` | No | limited | Weekly full refresh |

### 11.4 The finding that most changes the design **[PROVISIONAL-ERP]**

*This section applies only if D-14 confirms Zoho ERP. On Books + Inventory, `GET /purchasereceives` exists in Inventory with sort-based delta, and this section is replaced by the Inventory row of §11.2 — a materially easier problem. Which is itself a reason not to treat the target as settled.*

**Zoho ERP Purchase Receives has four endpoints and no list endpoint.** You can create, update, delete and fetch one by id — **you cannot enumerate them.** If the client is on Zoho ERP (D-14), there is no delta feed and no full-scan path for GRNs.

The only viable pattern is **PO-anchored discovery**: walk locally-open POs, fetch each PO detail, and read the receive references it carries. This makes Sweep A not a compensating control but **the sole GRN acquisition mechanism** on ERP, which changes its cadence, cost and failure profile.

Three consequences to state plainly:
- A receive against a PO **we do not know about** is undiscoverable on ERP. It surfaces only when its bill arrives, as an `UNSANCTIONED_COMMITMENT` exception (§11.7).
- A **deleted** receive arrives as an absence; only a re-read of the PO detects it.
- Cost scales with **open PO count**, not receive volume. At 100 req/min shared, and **2,000 calls/day on ERP Standard**, this is the binding constraint. Phase 0B-3 must size it against the client's actual open-PO population and plan tier. **If the client is on ERP Standard with 2,000 calls/day, GRN sync frequency may have to be negotiated down** — this is a client conversation, not an engineering workaround.

### 11.5 Polling and sweeps

Each job obeys the §2.2 contract — bounded, chunked, checkpointed, 12-minute soft deadline.

| Job | Mechanism | Cadence |
|---|---|---|
| `poll_bills` | `last_modified_time` window `[hwm − 300s, hwm + 24h]`, paged | 5 min |
| `poll_purchaseorders` | same | 5 min |
| `poll_items` | Inventory: true delta filter. ERP/Books: weekly full refresh | 15 min / weekly |
| `poll_contacts` | sort-descending walk with early stop, or weekly full refresh | hourly |
| `sweep_po_anchored` | **Sole GRN mechanism on ERP.** 50 open POs per invocation | 15 min |
| `sweep_bill_detail` | `GET /bills/{id}` to hydrate `line_items` — list responses omit them | continuous, queue-driven |
| `sweep_control_totals` | per (module, period, status): count + sum, source vs local → `CONTROL_TOTAL_MISMATCH` | nightly |
| `sweep_completeness` | document-number gap detection; documents dated into a CLOSED period → `LATE_ARRIVAL_CLOSED_PERIOD` | nightly |

**A period cannot close while an Open reconciliation exception exists** — extending the gate `approve_capitalisation` already applies.

### 11.6 Idempotency, rate limiting, retry, circuit breaker

**Inbound** idempotency is free: `integration_inbox UNIQUE (connection_id, module, external_id, payload_sha)`.

**Outbound: Zoho documents no idempotency header.** We synthesise one — `dedupe_key` written to a Zoho unique custom field `cf_capex_ref`, with retries using update-by-custom-field-unique-value. A Function killed after sending but before recording *updates* rather than duplicates. **Depends on D-7.**

Rate budget lives in PostgreSQL (`integration_rate_budget` upserted with `RETURNING`) because no process is resident. Over budget → the job checkpoints and returns; the next cron tick resumes. Allocation per org: **60 polling / 30 outbound / 10 interactive**, so an operator clicking "Test connection" never starves behind a backfill. **On ERP Standard (2,000/day) the daily budget, not the per-minute one, is binding** and is tracked with its own window.

| Condition | Action | Counts toward circuit? |
|---|---|---|
| 429 code **44** (per-minute) | checkpoint, resume next tick | **no** — our own throttle failed, not the vendor |
| 429 code **45** (daily quota) | open circuit until next day boundary, alert | no |
| 429 code **1070** (concurrency) | reduce parallelism by 1, retry with jitter | no |
| 5xx / timeout | exponential backoff, full jitter | **yes** |
| 401 / invalid_token | refresh via Connections once; second failure → open circuit + P1 | yes, immediately |
| 4xx business error | no retry → QUARANTINED/DEAD + reconciliation exception | no |

Backoff `next_attempt_at = now() + random(0, min(900s, 2s · 2^attempts))`, `max_attempts=8`, then DEAD and visible on SCR-39 with manual retry. Circuit: 5 consecutive counted failures in 60 s → OPEN 60 s → HALF_OPEN single probe.

### 11.7 PR strategy and its risks

Ranked: **(1) WBS Hub as PR system of record, emitting POs** — recommended and chosen. (2) Zoho Custom Module hosting the PR — documented in ERP and Books, gated to unpublished plan tiers, creates a second record to reconcile. (3) Controlled export — interim degraded mode. Native PR API — **disproven**.

The PR is a **pre-commitment control artefact, not an accounting document.** Nothing in Zoho's ledger requires it. What does require it is `budget_check` — the ability to refuse a commitment before it becomes one. If the PR lives in Zoho, that refusal becomes advisory and the product's central claim collapses.

**Risk: a PO created directly in Zoho, bypassing the budget check.** This is the option's principal weakness and it is stated plainly.
- *Preventive:* Zoho role configuration restricting PO creation on CAPEX vendors/accounts — **D-8**.
- *Detective, always on regardless of D-8:* Sweep A pulls every PO. Any PO carrying a CAPEX dimension with no matching local `external_id` → `UNSANCTIONED_COMMITMENT`, surfaced on SCR-25, **blocking period close**. Even if prevention fails, the number is never silently wrong — it is loudly wrong, which is the acceptable outcome.

POs are emitted as `draft` and transitioned to `open` only by our own call after our approval instance closes; a Zoho-side status change we did not initiate is an exception.

**PR → PO conversion becomes line-level.** `convert_pr_to_po(con, actor, pr_id, *, lines: list[PrLineToPoLine])` validates that every converted line lands on the same `(wbs_id, budget_head_id)` control cell as the reservation and settles the reservation for exactly the converted amount — closing AUD-H-001's residual.

### 11.8 AUD-H-004 — received-not-billed attribution

Documentation says the linkage exists (bill lines carry `purchaseorder_item_id`; Inventory receive lines carry the PO's `line_item_id`). **Documented ≠ populated.** Therefore:

1. **Phase 0B-3 spike:** in the client's tenant, create a PO with two lines, receive one partially, bill part of it, pull all three, and assert the identifiers actually match. Publish the raw JSON as evidence. This either closes AUD-H-004 or escalates it to a blocking commercial issue.
2. **Permanent quarantine mode.** Any receive line whose linkage does not resolve to a known `po_line` → `reconciliation_exception` kind `GRN_LINE_UNATTRIBUTED`, accumulating in a project-level `unattributed_receipts_paise` bucket. **Never spread pro-rata. Never silently dropped.** Displayed on SCR-16 and SCR-27; blocks capitalisation through the existing `blockers` list.

### 11.9 Webhooks and correlation

**Webhooks are an optimisation only.** No standalone webhook registration API exists in any of the three products; outbound webhooks exist only as a Workflow Rule action, one per rule, with plan-dependent daily caps. Bill is a confirmed workflow-triggerable module in Books; **PO and Purchase Receive are NOT CONFIRMED, and ERP workflow-module coverage is NOT CONFIRMED.** Build `POST /api/integrations/zoho/webhook/{connection_id}` doing exactly one thing — insert to `integration_inbox` and enqueue a job, never processing inline. **Polling is never disabled because a webhook exists.**

`correlation_id` propagates from the existing middleware through `job` → inbox/outbox → `integration_event` → `audit_log`, so one id traces a Zoho bill from HTTP response to ledger movement to audit entry.

**Modes.** `zoho.MODE` extends to `MOCK | SANDBOX | LIVE_READ | LIVE_WRITE` per connection. `app.js`'s existing `mockBadge()` / `mockBanner()` honesty indicators become real mode indicators rather than being deleted. **No live call is made during development without explicit user authorisation.**

### 11.10 The product-agnostic adapter boundary

Because the target product is provisional, the integration layer is built against an interface, not a product. One interface, two implementations, selected by `integration_connection.product`.

```python
# app/backend/integration/adapter.py
class ProcurementAdapter(Protocol):
    product: Literal["ERP", "BOOKS_INVENTORY"]
    def base_url(self, dc: str) -> str: ...
    def scopes_required(self) -> frozenset[str]: ...
    def list_bills(self, since: datetime, until: datetime, page: int) -> Page[BillDTO]: ...
    def get_bill(self, external_id: str) -> BillDTO: ...
    def list_purchase_orders(self, since, until, page) -> Page[PurchaseOrderDTO]: ...
    def create_purchase_order(self, po: PurchaseOrderDTO, dedupe_key: str) -> str: ...
    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]: ...
    def capabilities(self) -> Capabilities: ...
```

`Capabilities` is the honest part — it declares what the product **cannot** do, and the platform reads it rather than assuming:

```python
@dataclass(frozen=True)
class Capabilities:
    receives_listable: bool          # ERP: False.  Inventory: True
    bills_delta_filter: bool         # both: True
    po_delta_filter: bool            # ERP/Books: True.  Inventory: False
    items_delta_filter: bool         # Inventory: True.  ERP/Books: False
    line_level_custom_fields: bool   # D-7, tenant-verified
    daily_call_ceiling: int          # plan-derived, tenant-verified
```

The scheduler reads `capabilities()` to choose a strategy — `receives_listable=False` selects PO-anchored discovery; `po_delta_filter=False` selects full re-pull. So the §11.4 problem is handled as a **declared capability gap**, not a hardcoded assumption, and switching target products after D-14 is a configuration change plus one adapter implementation rather than a redesign.

`DTO` types are ours, not Zoho's. Raw payloads are preserved in `integration_inbox` with the product and API version that produced them (§8.4), so a mapping error is always traceable to a source document.

Contract tests use recorded cassettes **per product**, and a test asserts that a Books cassette can never satisfy an ERP adapter test — the executable form of "products are never mixed".

---

## 12. State machines

All business labels from `C3_statuses.json` (§8.1).

**Project / WBS** — `DRAFT → RELEASED → PARTIALLY_COMMITTED ⇄ FULLY_COMMITTED → PARTIALLY_ACTUALISED → FULLY_ACTUALISED → TECHNICALLY_COMPLETED → AWAITING_CAPITALISATION → CAPITALISED → CLOSED`; `REOPENED` reachable only via an approved reopen instance (closes AUD-C-008 residual). `lifecycle_state` remains the single source of truth; unknown states deny by default.

**Budget line** — `DRAFT → SUBMITTED → APPROVED` (effective-dated), `REJECTED`/`RETURNED` terminal-for-version. `kind='ORIGINAL'` rows are immutable and undeletable at the database level and never transition.

**Approval instance** — §8.2, plus `SUPERSEDED` on `object_content_sha` change under an open instance (auto-supersede, never silent approval).

**PR** — `DRAFT → SUBMITTED → UNDER_REVIEW → {APPROVED | REJECTED | RETURNED | EXCEPTION_PENDING} → CONVERTED | CANCELLED`. Reservation `Reserved → {Converted | Released | Expired}` exactly once, enforced by `ux_pr_reservation_live`.

**PO** — `DRAFT → SUBMITTED → APPROVED → RELEASED → PARTIALLY_ACTUALISED → FULLY_ACTUALISED → CLOSED`; `CANCELLED` from any pre-actualised state, releasing residual commitment and settling reservations. Amendment increments `amendment_no` and re-runs `budget_check` on the delta inside the lock.

**GRN** — mirrors the raw Zoho fields via §8.4. **No reversal endpoint is documented in any product — only DELETE** — so a deleted receive arrives as an absence, which is why PO-anchored re-read (not a delta feed) is authoritative.

**Bill** — `accounting_status ∈ {Draft, Approved, Void, Reversal}`; only `{Approved, Reversal}` are accounting-effective; `Reversal` negates by flag, not by data entry. Preserved exactly from AUD-C-004.

**Reconciliation exception** — `OPEN → {RESOLVED | ACCEPTED | WRITTEN_OFF}`; `OPEN` blocks capitalisation (existing) **and period close** (new).

**Capitalisation** — `DRAFT → SUBMITTED → APPROVED → POSTED`. Approval today returns `posting_status = "NOT POSTED"`; that honesty is preserved. `POSTED` is reachable only when a real GL/fixed-asset posting exists, which is out of scope (§18.4).

---

## 13. API surface and screens

Existing routes keep their paths and contracts and gain a `Scope`. New groups: `/api/admin/*` (org hierarchy, users, roles, scope grants, delegation, approval definitions, numbering, custom fields, masters), `/api/approvals/*`, `/api/integrations/*`, `/api/reports/*`. Standards: cursor pagination on every list; `X-Correlation-Id` on every response; RFC-7807 errors carrying the existing `code` + `message_id`; `Idempotency-Key` honoured on every mutating route.

**Every request must complete in under 30 seconds** (§2.1). Any operation that cannot — bulk import, large export, full reconciliation, migration — returns `202` with a job id and is polled. This is a hard architectural rule, tested by a route-level budget assertion.

Growth to the 40 `SCR-nn` screens frozen in `C8_screens.json`, each documented against all 24 required attributes, **delivered as vertical slices inside each phase** (§16), not deferred. The existing 14 map onto SCR-01/02/04/05/07/09/13/14/15/16/17/18/21/28 and are preserved visually unchanged.

---

## 14. Frontend

Per the client's decision: **Vite + TypeScript introduced incrementally, vanilla JS / Web Components retained, existing DOM structure preserved, no React or Svelte.**

```
app/frontend/
  index.html                 shell (unchanged structure)
  styles.css                 UNCHANGED — SHA-256 pinned in CI
  src/
    main.ts  registry.ts     boot; SCR-nn → {module, route, nav group, permissions}
    core/                    state api router format dialog a11y scope mask
    components/              capex-datatable, capex-treetable, capex-filterbar,
                             capex-statuschip, capex-bandbar, capex-moneyinput,
                             capex-approvalpanel, capex-auditpanel, capex-exportbar,
                             capex-messagebar, capex-pagination
    features/                project/ budget/ procurement/ receipts/ billing/
                             closure/ analytics/ integration/ governance/
```

Migration of the existing 14 views is **mechanical**: `app.js` already has the seams — `const V = {}` with each view an async function returning HTML, a hash router, and `NAV` as a declarative table. Each `V.*` becomes a feature module; `NAV` becomes `registry.ts`; `setHeader`, `msg`, `status`, `band`, `bar`, `mockBadge` become components. **No behaviour change in the first step — a pure move, verified by screenshot diff.**

**The CSP constraint is load-bearing and survives this choice.** `main.py` sets `style-src 'self'` with no `'unsafe-inline'`, so a markup `style="…"` attribute is blocked — but CSSOM assignment (`el.style.left = …`) is not governed by CSP at all, and the current `enhance()` depends on exactly that distinction for bar geometry and tree indentation. Web Components preserve this; React/Svelte would not — which independently corroborates the client's decision. New CSS only where a new screen requires it, only from existing tokens.

**CI gates.** (1) `styles.css` SHA-256 pinned — any change fails CI until the pin is updated in the same commit, forcing a deliberate design-approval note. (2) Token gate — every `var(--…)` must exist in `C6_tokens.json`; no raw hex outside `:root`. (3) Screen registry gate — 40 = 40, each naming a module that exists. (4) **CSP gate** — AST check rejecting any `style="` literal in a template string, so the contract cannot be broken by a well-meaning one-line change in eighteen months. (5) **XSS gate** — every interpolation into an `innerHTML` assignment must pass `esc()`, `money()`, `pct()`, `mask()` or an allowlisted formatter; AST-based, not regex. (6) **Playwright visual regression** on all approved screens at 1440/1024/800 px in both densities, against baselines captured **before any refactor begins**; any pixel diff fails CI. (7) **Masking gate** — classified fields (§10.4) render through `mask()` unless the reveal permission is present. (8) Number-format tests against `C12_conventions.md`.

Build output is static, served from `/static` by the existing mount, deployable to Catalyst with no change to `main.py`'s mounting logic. Bundle-size budget enforced in CI, because AppSail cold starts make payload size a user-visible cost.

---

## 15. Test strategy

### 15.1 Policy for the existing 220 tests

**All control intent and traceability must be preserved. Documented PostgreSQL-specific test adaptations are permitted, but no assertion may be weakened or removed without approval.**

Mechanics:
- Every adaptation is recorded in `tests/ADAPTATIONS.md`: test name, original assertion, adapted assertion, the platform reason, and the reviewer who approved it.
- A CI gate compares the current test-function inventory against a committed manifest. A **removed or renamed** test fails the build until the manifest is updated with a reason.
- Adaptations already anticipated, each requiring explicit approval:
  - SQLite-specific error strings (`sqlite3.IntegrityError`) → PostgreSQL SQLSTATE assertions. *Same assertion, different dialect.*
  - `test_migrations_*` targeting the SQLite runner → the PostgreSQL migration runner. *Same intent.*
  - `conftest.py` isolation → template databases. *Same guarantee.*
  - `test_aud_m_005_deep_wbs_*` → deep hierarchy becomes a rejected **write** rather than a controlled **read** (§7.7). **This is a genuine change in observable behaviour and needs explicit sign-off, not a silent adaptation.**
- Tests asserting the retired `allow_procurement`/`allow_posting` columns are re-pointed at `v_wbs_permits`, preserving the assertion.

### 15.2 Layers

| Layer | Content |
|---|---|
| **Unit** | `money.py`; `domain._derive`; scope predicate compiler; approval predicate AST; masking; backoff; status-mapping registries |
| **Property / invariant** | Existing: `test_available_equals_budget_minus_actual_commitment_and_reservation`, `test_exposure_is_the_sum_of_commitment_actual_and_reservation`, `test_po_line_billed_plus_open_commitment_equals_ordered`, `test_rollup_equals_own_plus_children`, `test_money_is_only_ever_integer_paise`, `test_aud_h_007_split_pro_rata_never_loses_a_paisa_property`. **New:** `test_ledger_cell_equivalence.py` (autouse, whole-suite) |
| **Concurrency** | `test_lock_ordering.py`, **`test_nested_budget_owner_concurrency.py`** (§7.4 — fails against v1.0), `test_multiline_po_deadlock.py`, existing AUD-C-001 tests extended to N workers |
| **Migration** | Existing 22 retargeted; per-table row-count and money-sum equality vs SQLite; `verify_audit_chain` on the imported LEGACY stream; **expand/contract compatibility tests** (§17) |
| **Contract** | `test_contracts.py` — code vs C1/C3/C4/C5/C6/C8/C9/C10/C14/C15/C16/C17, including provenance completeness; recorded-cassette tests against captured Zoho responses **per product**, so a Books cassette can never satisfy an ERP test |
| **Security** | Negative scope matrix (13 roles × read/write × API/export/search); `test_pool_isolation.py`; `test_no_plain_set.py`; `test_export_scope.py`; `test_data_classification.py`; AST gate proven by injecting an unscoped `.execute(`; CSRF/CSP/headers; attachment abuse; `pip-audit` |
| **Platform** | **Job chunking**: a job given more work than fits in 12 minutes checkpoints and resumes correctly. **Cold start**: the app listens within 10 s. **Request budget**: no route exceeds 30 s under load. **Token lifecycle**: a Connections token expiring mid-job refreshes without duplicate work |
| **Integration** | Inbox/outbox/job lifecycle; forced 429/44, 429/45, 429/1070, 5xx, 401 each producing its specified distinct behaviour; circuit transitions; DLQ and manual retry |
| **Tenant (Phase 0B, 5, 6)** | AUD-H-004 linkage spike; OAuth on the client DC; 7 consecutive days of clean control-total reconciliation |
| **End-to-end** | PR → approval → PO → Zoho → receive → bill → reconcile → capitalise, with ledger assertions at every step |
| **Performance** | p95 `POST /api/budget-check` < 150 ms; lock-wait p99 < 50 ms; 100k-row virtualisation; 250k-row export as a chunked job |
| **Accessibility** | axe-core on all 40 screens; keyboard-only traversal; dialog focus management; status distinguishable without colour |
| **UAT** | Scripted per role against `C8_screens.json`'s 24 attributes — §18.1 |

CI runs unit + property + contract + migration + scope + platform + a11y on every PR, closing **AUD-H-010**'s residual.

---

## 16. Delivery phases

Relative sizing S/M/L/XL. No calendar promises. **Every phase delivers a working vertical slice — schema, service, API and screens together** — so client-visible value and UAT feedback arrive continuously rather than at a Phase 7 cliff.

### Phase 0A — work possible with no Zoho tenant · S

Executable contract validation (AUD-M-001); lockfile + SBOM + `pip-audit` (AUD-M-006); structured logs, RED metrics, alerts (AUD-M-007); **Playwright baselines captured from the current build**; test-inventory manifest and `ADAPTATIONS.md` scaffolding; the four status registries (§8) with mapping tests; **provenance backfill across all 107 requirements**; **commit Annex A into the repository** — `research/00_intake/client_notes_2026-08-28.md` (transcription), `research/00_intake/product_owner_requests_2026-08-28.md` (the 2 product-owner requirements), and all 15 rows into `C14_traceability.json` and `C1_req_ids.json` under their **two distinct provenance values**, then **walk the AMB-01…AMB-07 and AMB-09 ambiguity register with the client** (AMB-08 already closed) and record each resolution against its requirement; **AppSail runtime proof — a FastAPI app on AppSail binding `X_ZOHO_CATALYST_LISTEN_PORT` within 10 s** (FastAPI is not named in Zoho's docs; this proves it).

*The transcription and ID allocation are already complete (Annex A) — Phase 0A commits them to the repository and closes the ambiguities, rather than doing the analysis.*

**Exit:** contract gate green; SBOM published; visual baselines committed; Annex A committed and every ambiguity either resolved or formally accepted as open with an owner; FastAPI-on-AppSail proven. **Rollback:** n/a, additive.

### Phase 0B — tenant-dependent validation · S–M · gates everything downstream

- **0B-1 Connectivity & platform gate (§2.4).** Written Zoho confirmation on egress/TCP/static IPs/TLS; managed PostgreSQL India region, PITR, extensions, TLS, connection limits; secrets manager; **Catalyst Connections Custom Service proof for ERP/Inventory**. **Go/no-go.**
- **0B-2 Product & entitlement.** Resolve **D-14** — ERP vs Books+Inventory. `GET /organizations`, org ids, DC, edition, plan tier and daily call ceiling. Establish whether a PR module exists in the tenant **UI** (the `CONF-01` nuance, §5). Confirm ERP consent scopes including the two omitted from Zoho's scope table (§11.2).
- **0B-3 Capability spikes.** AUD-H-004 linkage probe with raw JSON published; **open-PO population sizing against the plan's daily call ceiling** (§11.4); D-7 line-level custom fields; D-8 role restriction feasibility.
**Exit:** every gate answered in writing; §11 matrix annotated with tenant-verified availability; a written statement of any capability the tenant does not support.

### Phase 1 — PostgreSQL port, behaviour-identical · L · after 0A+0B-1

Schema §6.2, transactions and multi-cell locking §7.4, audit §6.4, migration §17. No new features. **Slice:** SCR-28 Audit Trail Viewer extended with chain-verification status — small, proves the platform end-to-end.
**Exit:** all 220 tests green with only approved documented adaptations; `verify_audit_chain` intact on LEGACY; per-table money-sum equality with SQLite; **restore drill performed** (§17); SQLite tag still deployable.

### Phase 2 — Control cell, periods, budget slice · M · after 1

`ledger_cell.py`, cell tables, `accounting_period`, period roll job; drop the lifecycle flag columns. **Slice:** SCR-09 Budget Planning Grid, SCR-10 Budget Version Comparison, SCR-13 Budget Availability Check.
**Exit:** zero `LEDGER_DIVERGENCE` over a 14-day dual-run soak at 10× demo volume; `test_nested_budget_owner_concurrency` green; p95 budget-check < 150 ms; lock-wait p99 < 50 ms; **UAT gate U1**.
**Flags:** `FF_LEDGER_CELL_READ`. **Blocking:** D-9.

### Phase 3 — Identity, roles, scope, admin slice · L · after 1

`role_grant`; Zoho Directory OIDC, MFA, lockout, lifecycle; scope compiler + RLS + AST gate + pool-isolation tests; service-account scopes; migrate ~20 routes off `rows()`. **Slice:** SCR-29 Approval Matrix Configuration (read-only), SCR-30 Master Data Configuration, plus org-hierarchy admin screens.
**Exit:** negative matrix green for all 13 roles across API, export and search; AST gate fails an injected unscoped `.execute(`; no scope leaks across pooled connections; **UAT gate U2**.
**Flags:** `FF_DATA_SCOPE`, `FF_FEDERATED_AUTH`. **Blocking:** D-6, D-12.

### Phase 4 — Approval engine + approval screens · L · after 3

§9, shipped per `object_type`. Data classification (§10.4) implemented with masking and encryption. **Slice:** SCR-03 My Approval Inbox, SCR-11 Budget Revision Request, SCR-12 Budget Transfer, SCR-29 made editable with rule simulation.
**Exit:** every existing maker-checker test green **unchanged**; new tests for parallel quorum, N-of-M, delegation-cannot-restore-maker, temporary delegation bounds, SLA escalation, recall, auto-supersede; action chain verifies; classification tests green; **UAT gate U3**.
**Blocking for go-live, not build:** D-10. **Blocking for build:** D-15.

### Phase 5 — Integration inbound + outbound PO · XL · after 2, 4; needs 0B

Inbox, watermarks, chunked jobs, rate budget (per-minute **and** daily), circuit breaker, sweeps, quarantine, Connections-based tokens; then outbound PO emission with `cf_capex_ref` idempotency. **Slice:** SCR-31–34 connection setup and scope validation, SCR-16 GRN view, SCR-17 Vendor Bill view, SCR-18 Reconciliation, SCR-26/38/39 integration monitors, SCR-27 exception queue.
**Exit:** 7 consecutive days of clean control-total reconciliation in the tenant; every unattributed receive visible with **zero silently absorbed**; forced 429/44, 429/45, 429/1070, 5xx each produce their specified behaviour; **chaos test — Function killed mid-send 100× → zero duplicate POs**; job-chunking test green; **UAT gate U4**.
**Flags:** `FF_ZOHO_LIVE` per connection; `LIVE_READ` before `LIVE_WRITE`. **Blocking:** D-7, D-8, D-14, D-16.

*Inbound and outbound are one phase because they share the platform and because outbound without inbound reconciliation would ship an unverifiable write path.*

### Phase 6 — Analytics, closure and remaining screens · L · after 2, 5

Reporting model, saved views, chunked exports with requester-scope inheritance. **Slice:** SCR-01 Executive Dashboard, SCR-06/08 WBS explorer and detail, SCR-19 CWIP Ledger, SCR-20 Project Completion Review, SCR-22 Asset Allocation, SCR-23/24 ageing, SCR-25 Exception Monitor, SCR-35–37, SCR-40.
**Exit:** 40 screens routable and registered; 24-attribute gate green; **`styles.css` checksum unchanged**; Playwright diffs clean; export-scope test green; **UAT gate U5**.

### Phase 7 — Remaining audit closures · M · after 4

Foreign currency (D-5); approval-gated reopen (AUD-C-008 residual) via the Phase-4 engine.
**Exit:** FX applied at bill date with base-currency reporting; reopen requires an approved instance.

**Dependency graph:** `0A → 0B → 1 → {2, 3}`; `3 → 4`; `{2,4} → 5`; `{2,5} → 6`; `4 → 7`.

---

## 17. Migration, rollback and operational readiness

### 17.1 Expand/contract, replacing flag-only rollback

Every schema change ships in three deployments with a **compatibility window** between them, so at no point does a rollback require a data restore.

```
EXPAND     add new structures, dual-write, old readers unaffected
           ── compatibility window: both shapes valid, ≥ 1 full release cycle ──
MIGRATE    backfill; switch reads behind a flag; dual-run and compare
           ── soak: divergence must be zero ──
CONTRACT   remove old structures, only after the window closes with no rollback pending
```

Worked example — retiring `allow_procurement` / `allow_posting`:
1. **Expand:** create `v_wbs_permits`; leave columns and trigger in place. Deployable and rollback-able instantly.
2. **Migrate:** re-point all readers (including tests, per §15.1) at the view; assert the view and columns agree for a full soak.
3. **Contract:** drop the columns and the trigger, only after the window closes.

Rules: no destructive DDL in the same deployment that introduces its replacement; every `pg/*.sql` carries a `-- ROLLBACK:` section, matching the convention already at the top of `002_financial_controls.sql`; a **contract migration is never bundled** with a feature change; and the compatibility window is a stated duration per change, recorded in `MIGRATIONS.md`.

**Feature flags remain, but as traffic control, not as the rollback mechanism.** Flags cannot undo a dropped column; expand/contract can.

### 17.2 POC → production migration

1. **`migrations/003_pre_pg_normalise.sql` (SQLite).** Backfill the keys the composite FKs need — `po_line.project_id`, `bill_line.po_id`/`project_id`, `grn_line.po_id`, `bill.project_id`, `wbs_element.wbs_path` — then assert zero violations of every future FK with probes returning zero rows. Any latent inconsistency is found where it is cheap.
2. **`tools/export_poc.py` (read-only).** Canonical NDJSON: deterministic row order, **integers stay integers** (`amount_paise` must never round-trip through a float), ISO-8601 UTC, plus `manifest.json` with row counts and per-file SHA-256.
3. **`migrations/pg/00N.sql`.** Extensions, composite FKs, CHECKs, 3 retained triggers, partial indexes ported verbatim, cell and period tables, approval and integration tables, RLS policies, role split `capex_migrator` / `capex_app` / `capex_audit_owner`.
4. **`tools/import_pg.py`.** FK-topological order, single transaction, **foreign keys enabled** — `session_replication_role='replica'` deliberately not used, because the import doubling as a constraint proof is most of its value.
5. **Audit chain preservation.** Import in ascending `audit_id` with `stream_key='LEGACY'`, `seq = original audit_id`, `prev_hash`/`entry_hash` **copied verbatim — never recomputed** (the payload includes exact `at` and `detail` bytes). Run `verify_audit_chain(con,'LEGACY')` and **abort if `intact` is false** — a migration that silently launders a broken chain is worse than none. Pre-002 rows carry `entry_hash IS NULL` and are already skipped by the verifier; preserve that and **record the unhashed-row count in the migration report** so the auditor knows where verifiable history begins. Write one anchor for LEGACY; begin each new stream with a genesis entry whose `detail` names the LEGACY head hash, so the linkage is itself hashed.
6. **Post-load verification, all must pass before cutover.** `ledger_selftest()` per project; full pytest on PostgreSQL; `verify_audit_chain` on every stream plus `verify_anchors()`; row counts against `manifest.json`; `sum(amount_paise)` per money table vs SQLite — **a one-paisa difference fails the migration**.
7. **Demo dataset.** `pg/seed_demo.sql` **generated from the exported NDJSON**, so what stakeholders approved (and `docs/screenshots/` shows) is substantively what runs. `auth.is_demo_profile()` generalises to a `CAPEX_PROFILE` gate so demo seeding can never touch a non-demo database.
8. **Test-suite migration.** Session fixture builds `capex_tmpl_<pid>` via `migrate.fresh`; per-test fixture issues `CREATE DATABASE capex_t<n> TEMPLATE …`. `_assert_disposable` becomes a database-*name* check refusing anything not matching `^capex_t\d+$` or `^capex_tmpl_`. Same guarantee, same failure mode. Transaction-rollback isolation is rejected as the default because `services.critical` manages its own transactions.

### 17.3 Backup, restore and disaster recovery

PITR enabled with a **retention period confirmed in Phase 0B-1**. Daily logical backup to separate storage in addition to PITR, because a logical dump survives a platform-level failure that PITR does not.

**Restore drills are performed, not merely configured** — this is a Phase-1 exit criterion and a **quarterly recurring obligation**:
1. Restore to a scratch instance at a chosen point in time.
2. Run `ledger_selftest()` and the full contract suite against the restored data.
3. Run `verify_audit_chain` on every stream and `verify_anchors()` — **a restore that breaks the audit chain is a failed restore.**
4. Record actual RTO and RPO achieved; compare with the proposed **RPO 15 min / RTO 4 h**; feed variance back into the target rather than restating the aspiration.

Cutover: read-only window on the POC → final export → import → verification → DNS/route switch → smoke suite. Rollback during cutover is a route switch back to the POC, which stays running and untouched throughout.

### 17.4 Operational readiness

Health and readiness endpoints distinguishing "process up" from "database and connector reachable" — and both must answer within the 30 s budget on a cold start. Runbooks for: connector outage, DLQ drain, period close, audit-chain break, credential and encryption-key rotation, Connections re-consent, job stuck in resume-loop, and restore. Alerts on `LEDGER_DIVERGENCE`, `AUDIT_CHAIN_BROKEN`, circuit-open, DLQ depth, daily-quota exhaustion and job resume-count breach.

---

## 18. UAT, team, effort, cost and exclusions

### 18.1 Client UAT gates

Each gate is a scheduled session with named client participants, a scripted scenario set, a defect log, and a written accept/reject. **A phase is not complete until its gate is accepted.**

| Gate | After | Scenarios | Client participants |
|---|---|---|---|
| **U1** Budget control | Phase 2 | Create budget; approve revision; availability check at nested WBS; over-budget refusal; period roll | Project Finance Controller, Finance |
| **U2** Access & administration | Phase 3 | Each of 13 roles sees only its scope, in UI **and** export; delegation; user lifecycle | System Administrator, Internal Auditor |
| **U3** Approvals | Phase 4 | Matrix configuration; simulation; parallel and quorum stages; escalation; recall; self-approval refusal | CAPEX Committee, CFO, Plant Head, Procurement |
| **U4** Integration | Phase 5 | Connection setup; PO emission to the tenant; GRN and bill sync; reconciliation; failure and retry | Procurement, Finance, IT |
| **U5** Reporting & closure | Phase 6 | Dashboards; drill-down; exports with filter metadata; capitalisation workbench | Management Approver, Finance, Internal Auditor |
| **U6** Regression & sign-off | Pre-go-live | Full scripted pass across all 40 screens; §19 checklist walked | All |

Defect severity is agreed in advance: **S1 blocks the gate; S2 blocks go-live; S3 is backlog.** No gate is passed with an open S1.

### 18.2 Indicative team composition

| Role | Allocation | Peak phases |
|---|---|---|
| Technical lead / architect | 1 throughout | all |
| Backend engineers (Python, PostgreSQL) | 2, rising to 3 | 1, 4, 5 |
| Integration engineer (Zoho, Catalyst) | 1 | 0B, 5 |
| Frontend engineer (TypeScript, Web Components) | 1, rising to 2 | 3, 4, 6 |
| QA / test automation | 1 | 2 onward |
| Business analyst / functional consultant | 0.5 | 0A, 4, and every UAT gate |
| DevOps / platform | 0.5 | 0B, 1, and each release |

Client-side, and on the critical path: a **decision owner** empowered to close D-6…D-16, a **Zoho tenant administrator**, and **named UAT participants** per gate.

### 18.3 Effort and cost

**These are indicative planning ranges, not a quotation, and they are conditional on the §2.4 gate.** They should be re-baselined after Phase 0B, when the tenant facts are known.

| Phase | Indicative effort (person-weeks) | Principal drivers of variance |
|---|---|---|
| 0A | 3–5 | Volume and legibility of the handwritten notes |
| 0B | 2–4 | **Zoho support response time is outside our control** |
| 1 | 10–16 | Depth of trigger→constraint rework; test adaptations |
| 2 | 6–10 | Soak duration is fixed at 14 days regardless of build effort |
| 3 | 10–16 | IdP federation complexity; D-6 scope-rule complexity |
| 4 | 10–16 | Approval matrix complexity (D-10) |
| 5 | 16–26 | **Widest range.** ERP vs Books+Inventory (D-14); the missing ERP receives list endpoint; daily call ceiling |
| 6 | 10–16 | Report count and export volume |
| 7 | 4–8 | FX policy (D-5) |
| **Total** | **71–117 person-weeks** | ±40% before Phase 0B closes; expected ±20% after |

A negative §2.4 outcome adds an estimated **6–12 person-weeks** for the Functions-fronted fallback (§2.5 option 1), and would trigger a re-plan rather than absorption.

**Platform cost lines — to be quoted, not estimated here.** Inventing prices would be fabrication, so these are named for the client to price against their own contracts:

| Line | Notes |
|---|---|
| Zoho Catalyst | Production environment; AppSail instance-hours (scale-to-zero helps); Function invocations; Job Scheduling executions; Cache; Connections. Development environment is free but capped at 200k API calls |
| Managed PostgreSQL | India region; instance tier; storage; **PITR retention is a direct cost driver**; a non-production instance for restore drills |
| Zoho ERP / Books+Inventory subscription | Existing client cost, but **the plan tier determines the daily API ceiling** and may need upgrading — see §11.4 |
| Secrets manager | If separate from Catalyst |
| Object storage | Attachments and export artefacts |
| Observability | Log sink, metrics, alerting |
| Malware scanning | Attachment scanning service |
| IdP | Zoho Directory seats, if not already licensed |
| CI | Build minutes, including Playwright runners |

**The single most likely cost surprise is the Zoho plan tier.** ERP Standard at 2,000 API calls/day is tight for PO-anchored GRN discovery (§11.4). Phase 0B-3 sizes this explicitly so the decision is made on evidence rather than discovered in production.

### 18.4 Go-live exclusion register — each requires explicit sign-off

**No exclusion below takes effect until it is signed.** An unsigned exclusion is an open scope question, not a settled boundary. Each is presented to the named signatory with its consequence stated, and the register is walked in full at UAT gate U6 before go-live.

Signatories: **PO** = client Product Owner · **CFO** = client CFO or Finance Head · **CIO** = client IT owner · **PM** = engagement Project Manager.

| # | Exclusion | Consequence if signed | Signatory | Consequence if **not** signed |
|---|---|---|---|---|
| **X-01** | **No GL or fixed-asset posting.** Capitalisation approval continues to return `posting_status = "NOT POSTED"` | The system records the capitalisation **decision**; someone must still make the accounting entry in Zoho manually, and the two can drift | **CFO + PO** | Scope increase: GL/FA posting integration, a new phase. **This is the highest-consequence exclusion in the register** and must not be signed casually |
| **X-02** | **No payment processing.** No vendor payment initiated, approved or recorded; bank details never displayed or exported | Payments stay entirely in Zoho | **CFO** | Scope increase, plus a materially higher security classification (§10.4 Restricted) |
| **X-03** | **No foreign-currency revaluation.** FX translation at bill date only, from Phase 7 | Non-INR CAPEX carries no revaluation; period-end FX movement is not reflected in CWIP | **CFO** | Depends on D-5. If any CAPEX is non-INR, this needs a decision before Phase 7 |
| **X-04** | **No Zoho-side PR object.** WBS is the PR system of record | Procurement staff raise PRs in WBS, not in the Zoho ERP PR screen they can currently see. **This is a working-practice change, not just a technical boundary** — and the notes' request for "PR creation field same as Zoho ERP" shows the client is aware of that screen | **PO + CIO** | Reopens the PR strategy (§11.7). Note that signing does **not** create a Zoho PR API — none exists |
| **X-05** | **No statutory or e-invoicing filing.** No GSTR, no e-invoice, no IRN | Statutory filing stays in Zoho or with the tax advisor | **CFO** | Scope increase |
| **X-06** | **No migration of historical Zoho transactions** beyond the agreed opening-balance cut-off, fixed at cutover | Pre-cutover POs, GRNs and bills are not visible in WBS; historical CAPEX reporting stays in Zoho for those periods | **CFO + PO** | Scope increase, sized on the historical volume. **The cut-off date itself must be agreed and recorded even if this is signed** |
| **X-07** | **No mobile application.** Responsive tablet behaviour only | Approvers on phones use the browser at tablet breakpoints | **PO** | Scope increase |
| **X-08** | **WCAG 2.2 AA is a target, not a certification.** No third-party audit included | axe-core and keyboard testing are performed; no external attestation is produced | **PO** | Add a third-party accessibility audit |
| **X-09** | **No offline mode** | Field users need connectivity | **PO** | Scope increase |
| **X-10** | **No real-time sync.** Minimum latency is one minute (§2.3), a platform consequence of Catalyst scale-to-zero | A PO approved in WBS appears in Zoho within ~1 minute, not instantly. The UI shows a `QUEUED` badge (§8.3) | **PO** | Requires leaving Catalyst — reopens client decision 1 |
| **X-11** | ~~No Zoho customisation delivery~~ | **WITHDRAWN in v1.2.** The integration depends on this configuration; it cannot be excluded. Replaced by the signed responsibility matrix at **§18.5** | — | — |
| **X-12** | **Catalyst Circuits and Integration Functions are not used** | Both are unavailable in the India data centre; nothing in the design depends on either | **CIO** (information, not choice) | n/a — a platform fact, recorded for transparency |

### 18.5 Zoho tenant configuration — client responsibility matrix

**v1.1 excluded this as "the client's configuration work". That was wrong: the integration does not function without it, so it cannot be excluded.** It is either performed by this project or performed by the client under a signed matrix with instructions and verification. Each row states an owner; unowned rows block the phase that depends on them.

| # | Configuration item | Why the integration needs it | Default owner | Setup instruction | Verification |
|---|---|---|---|---|---|
| **Z-01** | **Unique custom field `cf_capex_ref` on Purchase Order (header), marked unique** | **The sole outbound idempotency mechanism.** Zoho documents no idempotency header; retries dedupe via update-by-custom-field-unique-value. **Without it, a retried send creates a duplicate PO** | **Project delivers** (in scope) — too critical to delegate | Settings → Preferences → Purchase Order → Custom Fields → single-line text, **Mandatory off, Unique ON** | Automated: create, retry the same `dedupe_key` twice, assert one PO. Part of the Phase 5 chaos test |
| **Z-02** | **Line-level custom fields or reporting tags for WBS code and budget head on PO lines** | `po_line` is keyed on `(wbs_id, budget_head_id)`; a multi-WBS PO is normal. Header-only forces one PO per control cell | **Project delivers**, contingent on **D-7** plan support | Line item custom fields, or reporting tags if the plan restricts custom fields | Phase 0B-3 probe: create a two-line PO with different values per line and read them back |
| **Z-03** | **OAuth client (client ID + secret) registered in the Zoho API Console, with the correct redirect URI** | No connection without it. The notes name this explicitly — `REQ-INT-024` | **Client provides**, project specifies | api-console.zoho.in → Server-based Application → redirect URI supplied by the project | Successful authorisation code exchange in Phase 0B-1 |
| **Z-04** | **Consent granted for the full scope list**, including the two scopes absent from Zoho's own ERP scope table (`ERP.purchasereceives.*`, `ERP.custommodules.ALL`) | A missing scope fails at runtime, not at setup. Zoho's documentation defect (§11.2) makes this easy to get wrong | **Client grants**, project supplies the exact string | Project issues the verbatim scope string; client's Zoho admin consents | `GET` one object per scope; SCR-34 Scope Validation shows all green |
| **Z-05** | **Zoho roles restricting direct PO creation against CAPEX vendors/accounts** | **D-8.** Determines whether budget control is preventive or only detective | **Client configures**, project advises | Zoho role and profile configuration | Attempt a direct PO as a restricted user; expect refusal. If unenforceable, X-04's risk statement changes and the CFO is told |
| **Z-06** | **Zoho user accounts matching WBS users**, with `email` as the join key | Approver identity must reconcile across systems for the audit trail | **Client provides** | Zoho Directory / ERP user administration | Reconciliation report: every WBS user maps to exactly one Zoho user; exceptions listed |
| **Z-07** | **Workflow rule + webhook on Bill** (optimisation only) | Reduces sync latency. **Never a correctness dependency** — polling always runs (§11.9) | **Client configures**, project supplies the payload contract | Settings → Automation → Workflow Rules → Webhook action | Trigger a bill edit; assert an inbox row. **Failure is non-blocking by design** |
| **Z-08** | **Organisation ID(s) and data-centre confirmation** | Required query parameter on every request; determines base URL | **Client provides** | `GET /organizations`, or read from the Zoho UI | Recorded in `integration_connection`; asserted against the connection at every call |
| **Z-09** | **Plan tier confirmation and, if needed, upgrade** | Determines the **daily API ceiling**. ERP Standard = 2,000/day, which may not support PO-anchored GRN discovery (§11.4) | **Client decides**, project sizes | Zoho subscription management | Phase 0B-3 sizing against actual open-PO population, presented as a costed recommendation |
| **Z-10** | **Sandbox or UAT organisation**, or written authorisation for read-only production probing | No integration can be validated without one | **Client provides** | Zoho sandbox provisioning | Phase 0B cannot exit without it |

**Matrix rules.** Signed before Phase 5 begins, with a named individual per row — not a team. Any row the client declines moves to "Project delivers" with a scope and effort impact, or its dependent requirement is formally descoped through the §18.4 register. **Rows Z-01 and Z-02 are deliberately assigned to the project regardless**, because the idempotency guarantee and the control-cell dimensions are correctness properties, not configuration preferences. Every row has an automated or scripted verification, because "configured" and "configured correctly" are different claims and only the second is testable.

---

## 19. Definition of Done

**Financial correctness.** Every invariant in `C5_formulas.json` holds on PostgreSQL, each protected by a named test. `test_ledger_cell_equivalence.py` passes across the whole suite. `test_nested_budget_owner_concurrency.py` passes. Concurrent commitment cannot overspend at N workers, at any level of the hierarchy. No float touches a monetary value. Original budget immutable at the database level. Maker-checker holds at both the service layer and the approval engine.

**Integration honesty.** Every endpoint used is officially documented, with product, version, base URL, scope, source URL and fetch date recorded in `research/20_verified/`. **No endpoint or field is used that has not been verified, and no product's documentation stands as evidence for another's.** Anything unconfirmed is labelled `UNVERIFIED — REQUIRES ZOHO CONFIRMATION` in code, UI and documents. AUD-H-004 closed by tenant evidence, or the quarantine path proven active. Retries cannot create duplicate POs (chaos-tested). Control totals reconcile for 7 consecutive days.

**Platform.** No job exceeds 15 minutes without checkpointing. No route exceeds 30 seconds. Cold start listens within 10 seconds. Token refresh survives instance death. Job chunking, cold-start and request-budget tests green.

**Security.** No secret in source, database columns, logs or browser storage. Classified data encrypted, masked and access-restricted per the approved classification — **and nothing required for matching, compliance or reporting is irreversibly hashed.** Row-level scope enforced at the query layer, proven by the negative matrix across API, export and search, and by pool-isolation tests. Service-account scopes explicitly bounded; no service account holds a maker-checker permission. Audit append-only, hash-chained, anchored, verified nightly. Penetration test and `pip-audit` clean.

**Experience.** All 40 screens documented against 24 attributes. `styles.css` checksum unchanged. Playwright visual diffs clean on every approved screen. axe-core clean; keyboard-only traversal complete; status distinguishable without colour.

**Process.** Every UAT gate U1–U6 accepted in writing with no open S1. All 220 original tests present, with every adaptation documented and approved in `ADAPTATIONS.md`; **no assertion weakened or removed without approval.** Restore drill performed with the audit chain verifying post-restore. CI green on every gate. SBOM published. Runbooks written.

**Requirements.** Every requirement in Annex A.2 — both `CLIENT-NOTES-2026-08-28` (13) and `PRODUCT-OWNER-REQUEST-2026-08-28` (2) — is implemented, traced to screens, APIs, tables, services and tests, and demonstrated at its UAT gate. Every ambiguity AMB-01…AMB-09 is resolved and recorded, or formally accepted as open with a named owner and a stated consequence. **AMB-08 closed 2026-08-28.**

**Scope boundaries.** **Every exclusion in §18.4 is signed by its named signatory** — an unsigned exclusion is an open scope question and blocks go-live. **Every row of the §18.5 responsibility matrix is owned by a named individual and has passed its verification step** — "configured" is not accepted in place of "verified configured".

**Explicitly not claimed:** everything in the §18.4 register, once signed.

---

## 20. Verification

**Baseline before any change.** Run `pytest` from the repo root against SQLite and record the result (currently 220 tests). Capture Playwright baselines of all 14 approved views at 1440/1024/800 px in both densities.

**Per phase.**
- **0A** — contract gate green; provenance complete on all 107 requirements; a FastAPI app on AppSail binds `X_ZOHO_CATALYST_LISTEN_PORT` within 10 s.
- **0B** — written answers to all twelve gate questions; an AppSail app and a Cron Function each performing `SELECT 1` over TLS against managed PostgreSQL in the India region; raw JSON evidence for the AUD-H-004 linkage.
- **1** — `pytest` on PostgreSQL green with only approved adaptations; `migrate.py --status` at latest; `GET /api/audit/verify` returns `intact: true`; per-table `sum(amount_paise)` matches SQLite exactly; restore drill completed with the chain verifying.
- **2** — full suite with `FF_LEDGER_CELL_READ` off and on; zero `LEDGER_DIVERGENCE` in the dual-run log; `test_nested_budget_owner_concurrency` green; p95 budget-check under concurrent load.
- **3** — negative scope matrix as an automated test; inject an unscoped `.execute(` and confirm CI fails; pool-isolation test green.
- **5** — with `FF_ZOHO_LIVE` on against the tenant: one full PR → PO → receive → bill → reconcile cycle with ledger assertions at each step; Function killed mid-send 100× with zero duplicate POs; a job given 30 minutes of work checkpoints and resumes correctly.
- **6** — Playwright diff against Phase-0A baselines pixel-clean on every pre-existing screen; `styles.css` checksum matches its pin; an export produced for a plant-scoped user contains no out-of-scope row.

**End-to-end acceptance.** With the demo dataset loaded: a Requestor raises a PR that exceeds budget; it routes to exception approval; an independent approver approves it with a reason; it converts line-by-line to a PO which is emitted to the Zoho tenant; a purchase receive and a vendor bill sync back; the reconciliation view shows ordered / received / billed / open agreeing to the paisa; and the audit chain verifies across every stream.

---

# Annex A — Client notes, 2026-08-28

**Destined for `research/00_intake/client_notes_2026-08-28.md`**, with A.2 rows destined for `research/30_contracts/C1_req_ids.json` and `C14_traceability.json`. Held in this plan rather than written to the repository only because plan mode permits edits to the plan file alone; the analysis is complete, and Phase 0A commits it (§16).

## A.1 Verbatim transcription

Two pages, handwritten, ruled paper, blue ink. Transcribed as written — original spelling and abbreviations preserved, `[sic]` and `[?]` used where the reading is uncertain. Nothing normalised or interpreted here; interpretation is confined to A.2 and A.4.

### Page 1 of 2 — headed "ZPAS."

```
ZPAS.
1) Other page  -  charts                        [both "Other page" and the item
                                                 number appear struck through]

                        WBS

1) Settings Module  -  Centralized.
     -  User, Role, Division, Branch, Entity, Plant.
                                    ^Zone.       ["Zone." inserted above the line]
     -  API client id & client secret, MCP of the Portal.
     -  Item masters  -  Sync from ERP + Manual addition.
     -  Vendor Master  -  Sync + Manual addition.
     -  Custom field.

2) Budget Module for WBS  -  Multilayer, Auto numbering.
   facilities [sic] of categories -
     -  Custom field.
     -  Budget Revision.

3) Approvals  -  Multi level approval, Custom Approval
                 in all the modules.

4) PR Module  -  API sync with Zoho ERP for
                 Pushing from WBS to Zoho ERP.
              -  PR creation field same as Zoho ERP.

5) PO Module  -  API Sync with Zoho ERP for Pushing
                 from WBS to Zoho ERP - PO creation same as ERP.
```

### Page 2 of 2

```
6) Dashboard  -  Multiple user filter, Date filter.
     -  Project wise Report
     -  Category wise Report
     -  Plant wise Report
     -  Entity wise Report
     -  Location wise Report
     -  all the other granulal [sic] filters
        which are required to view from all aspects.
```

*(On page 2, "good" is written and struck through immediately before "granulal"; read as "granular".)*

## A.2 Requirement allocation

15 IDs across the 14 confirmed statements, under **two distinct provenance values**. Statement 7 splits because budget custom fields and budget revisions are separately testable with different acceptance criteria.

| Provenance | IDs | Source artefact |
|---|---|---|
| `CLIENT-NOTES-2026-08-28` | 13 — statements 1–12 | The two handwritten pages transcribed at A.1 |
| **`PRODUCT-OWNER-REQUEST-2026-08-28`** | **2 — `REQ-GRN-002`, `REQ-BILL-003`** | **Direct product-owner instruction.** Secondary evidence: `CLIENT-PDF` requirements `REQ-GRN-001`, `REQ-BILL-001`, `REQ-BILL-002` |

Both provenance classes are authoritative and mandatory. The distinction records *which artefact evidences the requirement*, not how important it is.

| Client statement | Req ID | Requirement | Note source | Screens | Tables / services | Acceptance criterion |
|---|---|---|---|---|---|---|
| 1 | **REQ-SEC-006** | Centralised Settings module covering users, roles, divisions, branches, zones, entities and plants | P1 §1 | SCR-30, admin org screens | `entity`, `plant`, `division`, `branch`, `zone`, `department`, `app_user`, `role_grant` | Each of the seven object types is creatable, editable, deactivatable and audited; hierarchy is enforced by FK; UAT **U2** |
| 2 | **REQ-INT-024** | Zoho API connection configuration — client ID, secret/connection ownership, organisation and portal mapping | P1 §1 line 2 | SCR-31, SCR-32, SCR-33 | `integration_connection`, `integration_credential`, Catalyst Connections (§10.2) | A connection is configurable per (entity, Zoho org); **secret never returned by any API, never in the DOM, never logged**; token health visible on SCR-38 |
| 3 | **REQ-INT-025** | Item Master synchronisation from Zoho, with governed manual additions | P1 §1 line 3 | SCR-30, SCR-35 | `item_master`, `integration.adapter.list_items` | Sync populates with `source='ZOHO'`; manual additions are `source='LOCAL'`, permission-gated and audited; duplicate detection on code and name |
| 4 | **REQ-INT-026** | Vendor Master synchronisation from Zoho, with governed manual additions | P1 §1 line 4 | SCR-30, SCR-35 | `vendor_master` | As REQ-INT-025, plus India tax identity handled under the §10.4 classification |
| 5 | **REQ-SEC-007** | Configurable custom fields | P1 §1 line 5 | SCR-30 | `custom_field_def`, `custom_field_applicability`, `custom_field_value` | Definable per object with data type, validation, requiredness, display order, scope and Zoho field mapping |
| 6 | **REQ-WBS-001** | Multilevel WBS budget module with automatic numbering and configurable category structure | P1 §2 | SCR-06, SCR-07, SCR-08, SCR-09 | `wbs_element`, `budget_head`, `budget_ledger_cell`, `numbering_series` | Configurable depth; auto-numbering is collision-safe under concurrency and immutable once issued (§6.3); categories configurable, not hardcoded. **See AMB-03** |
| 7a | **REQ-BUD-020** | Budget custom fields | P1 §2 line 2 | SCR-09 | `custom_field_*` scoped to budget | Definable and enforced on budget entry; surfaced in reports and exports |
| 7b | **REQ-REV-008** | Controlled budget revisions | P1 §2 line 3 | SCR-10, SCR-11, SCR-12 | `budget_revision`, `budget_line`, `services.create_revision` / `approve_revision` | **Original budget immutable at database level**; revisions carry effective date, justification, attachments and approval reference; submitted revisions create no spending capacity |
| 8 | **REQ-PRC-003** | Configurable multilevel approvals across all applicable modules | P1 §3 | SCR-03, SCR-29 | approval engine (§9) | One engine serves every module; sequential and parallel stages, quorum, escalation, delegation; **self-approval refused at every stage**; UAT **U3** |
| 9 | **REQ-PRC-004** | Purchase Request creation in WBS with intended posting to Zoho, subject to the confirmed absence of a Zoho PR API and the agreed WBS-system-of-record fallback | P1 §4 | SCR-14 | `purchase_request`, `pr_reservation`, `services.create_pr` | PR raised in WBS with **field parity to the client's Zoho ERP PR screen** (field list captured in Phase 0B-2); budget check enforced at creation and re-checked at approval. **The "push to Zoho ERP" half is not deliverable — no PR API exists in any Zoho product. See AMB-05 and exclusion X-04** |
| 10 | **REQ-PO-010** | Purchase Order creation in WBS and posting to Zoho with field alignment | P1 §5 | SCR-15 | `purchase_order`, `po_line`, `integration_outbox`, adapter `create_purchase_order` | PO created in WBS, approved internally, emitted to Zoho as draft with field alignment; **idempotent — retries never duplicate** (Z-01); Zoho ids and line ids persisted |
| 11 | **REQ-RPT-009** | Role and multi-user dashboards with date filters | P2 §6 line 1 | SCR-01, SCR-02, SCR-03 | reporting model (§13) | Dashboards vary by role; multi-user and date filters cascade to cards, charts, tables **and exports**; row-level scope applied server-side. **See AMB-06** |
| 12 | **REQ-RPT-010** | Project-, category-, plant-, entity- and location-wise reports, plus other granular filters | P2 §6 lines 2–7 | SCR-01, SCR-02, SCR-23, SCR-24, SCR-25 | reporting model, `FilterSet` | All five named dimensions available as filters and groupings, composable in any combination, consistent across cards, charts, tables and exports; drill-down to line-level source. **See AMB-04 and AMB-07** |
| 13 | **REQ-GRN-002** | GRN / Purchase Receive inbound synchronisation from Zoho | **`PRODUCT-OWNER-REQUEST-2026-08-28`** · secondary: `REQ-GRN-001` (`CLIENT-PDF`) | SCR-16, SCR-27 | `grn`, `grn_line`, `sweep_po_anchored` | Receives pulled from Zoho; every line resolves to a known `po_line` or lands in `reconciliation_exception`; **zero pro-rata spreading, zero silent drops** (§11.8) |
| 14 | **REQ-BILL-003** | Vendor Bill inbound synchronisation from Zoho | **`PRODUCT-OWNER-REQUEST-2026-08-28`** · secondary: `REQ-BILL-001`, `REQ-BILL-002` (`CLIENT-PDF`) | SCR-17, SCR-18 | `bill`, `bill_line`, `poll_bills`, `sweep_bill_detail` | Bills pulled with windowed `last_modified_time` filtering plus overlap; PO-line matching via the documented line key; unmatched bills to a controlled reconciliation workflow; commitment converts to actual CWIP without double counting |

### `elaborates` links (v1.1 IDs now subordinate to a client-note ID)

`REQ-SEC-006` ← the master-prompt org-hierarchy requirements · `REQ-INT-024` ← the master-prompt OAuth and connection requirements · `REQ-PRC-003` ← the master-prompt approval-engine block · `REQ-RPT-010` ← the master-prompt reporting block. Master-prompt IDs are **retained, not merged**, so each statement's provenance stays traceable.

## A.3 What the notes do **not** cover

Recorded so the provenance boundary stays honest. These remain `MASTER-PROMPT` or `PROPOSED-PENDING-SIGNOFF` and must not be presented as client-confirmed:

Maker-checker and segregation of duties · row-level data scope · the accounting-period calendar and cut-off · capitalisation and asset allocation · the audit trail and hash chain · reconciliation exception handling · idempotency and retry semantics · numbering-series immutability · multi-currency · WCAG accessibility targets · the WBS *element* model as distinct from the budget hierarchy (AMB-03).

Several of these are load-bearing controls the client has not asked for in writing. That is not an argument for dropping them — it is an argument for showing them at UAT and getting them acknowledged.

## A.4 Ambiguity register

Per direction: ambiguities are identified separately and **no requirement is removed or weakened because of one.** Each is resolved with the client in Phase 0A.

| ID | Ambiguity | Affects | Working assumption |
|---|---|---|---|
| **AMB-01** | **"MCP of the Portal"** (P1 §1 line 2). Unclear. Could mean the Zoho MCP connector, a portal identifier, or an abbreviation local to the client | REQ-INT-024 | Treated as **organisation/portal mapping** — the identifier binding a WBS entity to a Zoho organisation. **Not** assumed to mean an MCP server integration. Confirm |
| **AMB-02** | **"ZPAS."** heading, and the struck-through "1) Other page – charts" above the WBS block | none directly | Read as a prior/discarded topic on the same sheet. No requirement derived. Confirm nothing was intended |
| **AMB-03** | The notes say **"Budget Module for WBS – Multilayer"** but never define WBS structure — no depth, parent-child rules, element attributes or numbering format | REQ-WBS-001 | Multi-level budget hierarchy is **client-confirmed**; the WBS *element* model (depth limits, ordering, owner, dates, status) stays `MASTER-PROMPT`. Configurable depth assumed |
| **AMB-04** | **"Category wise Report"** — is "category" the budget head, an asset category, or an item category? All three exist in the model | REQ-RPT-010, REQ-WBS-001 | Assumed **budget head/category**, consistent with "facilities of categories" in the budget module. Both other readings retained as additional report dimensions so the assumption is cheap if wrong. **RESOLVED 2026-09-11 by the product owner (Fable 5.1 corrective build): the assumption was WRONG in one respect — BUDGET CATEGORY and BUDGET HEAD are separate business dimensions.** Category is neither an alias of `budget_head_id` nor derived from `asset_category`/`item_category`; it is its own master (`budget_category`, migration 026) carried on every control cell (`budget_control_cell.budget_category_id`, one per cell, set on release, immutable) and on every `budget_line`. Both category-wise and head-wise filtering, grouping and reporting are required and implemented independently; the control cell stays keyed by WBS × budget head. The earlier assumption is kept above as history, not deleted. |
| **AMB-05** | **"PR Module – API sync with Zoho ERP for pushing from WBS to Zoho ERP"** conflicts with the verified absence of any Zoho PR API in ERP, Books and Inventory | REQ-PRC-004 | **The requirement is recorded in full and not reduced.** Its deliverable half — PR creation in WBS with ERP field parity — proceeds. Its undeliverable half is escalated as exclusion **X-04** for explicit sign-off. This is the CONF-01 contradiction (§5) reaching implementation |
| **AMB-06** | **"Multiple user filter"** — filter dashboards *by* user (requester, approver, owner), or multi-user/role-specific dashboards? | REQ-RPT-009 | **Both implemented**: a user dimension in the filter set, and role-specific dashboard layouts. Confirm which was meant |
| **AMB-07** | **"Location wise Report"** — "Location" is a report dimension in the notes but is absent from the Settings list (User, Role, Division, Branch, Entity, Plant, Zone) | REQ-SEC-006, REQ-RPT-010 | `location` is added to the org hierarchy so it can be reported on. Confirm whether Location is distinct from Plant and Zone or a synonym for one |
| **AMB-08** | ~~Requirements 13 and 14 do not appear on either supplied page~~ | REQ-GRN-002, REQ-BILL-003 | **CLOSED 2026-08-28.** Resolved by the product owner: both originate from a **separate product-owner instruction**, not the handwritten notes. There is no missing third page. Provenance corrected to `PRODUCT-OWNER-REQUEST-2026-08-28`, with `REQ-GRN-001` / `REQ-BILL-001` / `REQ-BILL-002` (`CLIENT-PDF`) retained as secondary evidence. **Both remain mandatory and in scope — attribution changed, nothing else.** Recorded here rather than deleted, because how a discrepancy was resolved is itself audit evidence |
| **AMB-09** | **"Custom Approval"** (P1 §3) alongside "Multi level approval" — a distinct feature, or a restatement of configurability? | REQ-PRC-003 | Read as **configurable approval routing** — the §9 rule engine. Not read as user-authored approval logic or scripting, which would be a materially larger scope. Confirm |
