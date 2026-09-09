# Release Package — CAPEX & WBS Control Hub

Wave 8, stream D. The deterministic demo dataset, the known limitations, and the
external dependencies that are still outstanding.

This is the material a client reads, and therefore the material most likely to
overclaim. Every limitation below is marked **VERIFIED** (this stream measured
it, and the measurement is shown) or **TAKEN ON TRUST** (sourced from prior
verified research, and the source is named). Nothing is marked LIVE. Nothing is
marked VERIFIED for Zoho.

---

## 1. The deterministic demo dataset

**The business data is byte-identical across rebuilds. Measured, not asserted.**

Two independent `python -m app.backend.migrate --db <path> --fresh --seed` runs
were hashed table by table — every row of all 31 tables, sorted, SHA-256:

| Table | Rows | Digest, run 1 | Digest, run 2 |
|---|---|---|---|
| `project` | 3 | `58940f653e74` | `58940f653e74` |
| `wbs_element` | 19 | `6636ccada052` | `6636ccada052` |
| `budget_head` | 10 | `5cf62a54e3b1` | `5cf62a54e3b1` |
| `budget_line` | 12 | `f0b3f9e46f53` | `f0b3f9e46f53` |
| `purchase_request` | 20 | `615d19e4dfee` | `615d19e4dfee` |
| `purchase_order` | 15 | `65e0cf7084d1` | `65e0cf7084d1` |
| `po_line` | 16 | `9f9707c8f25d` | `9f9707c8f25d` |
| `grn` / `grn_line` | 9 / 9 | `c45cf9a953f5` / `e21fb8d4027a` | *identical* |
| `bill` / `bill_line` | 10 / 10 | `bcd2982f1a98` / `07e382c43f41` | *identical* |
| `budget_revision` | 3 | `407dfd749da1` | `407dfd749da1` |
| `capitalisation_request` | 1 | `d4f7f039da54` | `d4f7f039da54` |
| `audit_log` | 1 | `ee9c8023aa98` | `ee9c8023aa98` |
| …and every remaining table | — | *identical* | *identical* |

**Exactly one table differs, and it should.** `schema_migration` records an
`applied_at` wall-clock stamp (`2026-09-09T07:26:54` against
`2026-09-09T07:26:55`). The migration **checksums** in that same table are
identical — `10084` and `17527` in both runs — so the schema itself is pinned;
only the moment it was applied is not. The seeded `audit_log` row carries a
**fixed** timestamp, `2026-08-06T09:00:00`, not `now()`.

`migrations/pg/seed_demo.sql` contains **zero** occurrences of `random()`,
`now()`, `current_timestamp`, `gen_random_*` or `uuid` — grepped, count 0. All
ids are literals.

### The one thing that is not deterministic, and why that is correct

`app_credential` is **empty** after `--fresh --seed`, and `user_role` with it.
Identities are provisioned separately by `auth.provision_dev_identities()` when
`app/run.py` starts, and each run generates a fresh `secrets.token_hex(16)` salt.
So the stored hash differs every time **and the password does not**: it is always
`user_id + '!demo'`. A demo dataset that shipped a fixed password hash would be
shipping a fixed credential.

**Operationally this means: after `--fresh --seed`, restart the application
before signing in.** A migrated-but-not-restarted database returns `401` to every
identity.

### Stable identifiers a demo or a test may rely on

`ENT-DM1`, `ENT-DM2` · `PRJ-01..03` (`CAPEX-2026-001..003`) · `PR-001..020`
(`PR-015` Submitted/within budget, `PR-010` Exception Pending/over budget,
`PR-017` Draft, `PR-020` Rejected) · `PO-001..015` (`PO-004` Fully Committed) ·
`BILL-001..010` · `REV-001..003` (`REV-003` Submitted) · `CAP-001` Submitted.

The dataset deliberately contains a PO amendment, a PO cancellation, a PO closed
with a residual, a budget supplement, a budget transfer, one overrun, one
capitalisation case and one abandoned WBS — so the awkward states are
demonstrable without anyone having to construct them live.

---

## 2. Known limitations

### L-01 — Zoho is MOCK. There is no live integration. **VERIFIED**

`app/backend/zoho.py` sets `MODE = "MOCK"`. `GET /api/health` returns
`"zoho_mode": "MOCK"` and `"integration is NOT VERIFIED"`.
`GET /api/zoho/connections` returns `"oauth_status": "Not Connected"`,
`"status": "Disabled"`, and credential fields carrying `secretref://kv/zoho/...`
placeholders rather than values.

**No sandbox credential exists in this build. No live Zoho call has ever been
made by it.** The request that *would* be issued is constructed from a verified
endpoint inventory (869 endpoints across 78 ERP modules, generated mechanically
from Zoho's published OpenAPI bundle, sha256 `E95A0399…`) and logged; a
representative response is synthesised. The request construction is real; the
network call does not happen.

### L-02 — Capitalisation records the DECISION and posts nothing. **VERIFIED** (exclusion X-01)

Approving a capitalisation writes the decision, the allocation split and the
audit entry. It posts **no journal to any general ledger and creates no fixed-asset
record**, in this application or in Zoho. The server's own approval response
carries the sentence, and the screen renders the server's sentence rather than a
constant of its own:

> `"posting_status": "NOT POSTED"` — *"NOT POSTED — local approval only; no
> ERP/GL or fixed-asset posting exists in this build."*

Recorded at `app/backend/pg/closure.py:16` and `app/backend/api/closure.py:39`.
The note renders **above** the balance on SCR-21 and is restated in the approval
result, so no green success message ever stands alone next to a rupee figure.

### L-03 — No payment processing. **VERIFIED**

There is no payment endpoint in the API — grepped the live OpenAPI schema across
all 135 paths for `pay`: **none**. There is no payment function anywhere in
`app/backend/**`. Zoho's `vendor-payments` module appears in the connector's
required-module list for *identification* of advances and capital advances only,
never for initiating one. Nothing in this build moves money.

### L-04 — No Purchase Request object exists in Zoho. **VERIFIED for ERP; TAKEN ON TRUST beyond it**

The verified inventory covers **Zoho ERP v3 only** — 869 endpoints, 78 modules —
and contains **zero** purchase-request endpoints. The only "purchase" modules are
`purchase-order` and `purchasereceives`; no business object contains "request".
`zoho.py` names `custom-modules` as the "fallback carrier for Purchase Request,
which has no native API", and the Books/Inventory adapter defines no
purchase-request object either.

Scope of the check: measured against the ERP bundle. The wider claim — that no
Zoho product has one — rests on this engagement's prior product research, not on
a Books/Inventory OpenAPI bundle in this repository. **The PR therefore lives in
this application, or in a Zoho custom module, and there is no native object to
map it onto.**

### L-05 — Minimum sync latency is one minute. **TAKEN ON TRUST** (prior verified research)

Catalyst AppSail is a request/response tier with no resident worker and no
in-process scheduler. Background sync runs on Catalyst **Job Scheduling**, whose
cron granularity is **one minute**, with a 15-minute ceiling per invocation.
Nothing can be nearer to real time than that, regardless of what the connector
screens suggest.

Source: this engagement's Catalyst platform research, verified against official
Zoho documentation on 2026-08-28. Not re-derived here — no Catalyst call was
made by this stream.

The application is built for it: `POLL_OVERLAP_SECONDS = 300` re-enters each
window 300 s behind its own boundary and a `UNIQUE (connection_id, module,
external_id, payload_sha)` constraint discards the duplicates.

### L-06 — The `--warning` colour fails WCAG 2.2 AA everywhere it is used. **VERIFIED — independently recomputed**

`--warning: #A66A00` is the only semantic token below the 4.5:1 body-text
minimum, and it fails against **every** background it appears on. Recomputed from
the hex values by this stream, matching the Wave 4 finding to four decimals:

| Foreground | Background | Ratio | |
|---|---|---|---|
| `--warning` #A66A00 | `--n0` #FFFFFF | **4.4843:1** | FAIL |
| `--warning` #A66A00 | `--n50` #F7F8F9 | **4.2173:1** | FAIL |
| `--warning` #A66A00 | `--warning-bg` #FDF3E2 | **4.0775:1** | FAIL |
| `--warning` #A66A00 | `--primary-50` #EAF4F6 (row hover) | **4.0080:1** | FAIL |
| `--warning` #A66A00 | `--n100` #EFF1F3 | **3.9604:1** | FAIL |

`styles.css` inherits it through `.st-warning { color: var(--warning) }`, so
**every warning status anywhere in the application is below AA.** `--warning-bg`
and white are the two backgrounds it is actually used on
(`styles.css:16, 164, 273, 332, 335`).

**The remedy is identified and measured.** A darker amber at approximately
`#8A5800` clears AA on all five backgrounds — 6.04:1 on white, 5.49:1 on its own
tint, 5.40:1 on hover — and no existing token in the registry does.

**It is not applied here.** `app/frontend/styles.css` is byte-frozen behind a
SHA-256 checksum gate, the client has approved the current UI, and inventing a
hex is what `test_no_new_off_token_colour_is_introduced` exists to prevent.
**This awaits the product owner's approval**, not engineering work. An interim
mitigation exists in `approvals.css` only, and is scoped to that file.

Related, same cause, also unfixed: `--n500` #6B7280 measures 4.8345:1 on white
but **4.3210:1 on a hovered table row**, so any muted text inside a table row
fails AA while that row is hovered, application-wide.

### L-07 — Without PostgreSQL, most of the application is honestly unavailable. **VERIFIED by probing every endpoint**

Every `GET` in the live OpenAPI schema was called as `U-ADM` against a running
server with no `CAPEX_DB_URL` and no `CAPEX_DB_HOST`:

* **75 GET endpoints probed**
* **25 answered `200`** — the SQLite legacy ledger, the Zoho mock surfaces, and
  the static health routes
* **46 answered `503 DATABASE_NOT_CONFIGURED`**
* `/readyz` answered `503 DatabaseNotConfigured`
* `/api/integrations/control-totals` answered `503 CONTROL_TOTALS_UNAVAILABLE`
  — deliberately its own code, not the database one
* `/api/projects/{id}/wbs` answered `404` for a placeholder project id

The front end declares **17 legacy shell views** (`NAV`) and **46 SCR screens**
(`SCR_ROUTES`) — counted from `app/frontend/app.js`, and the two tables are
**disjoint**: no id appears in both. The shell views are wholly SQLite-backed
and serve live data with no PostgreSQL. The SCR screens are PostgreSQL-first;
several of them (most analytics screens, part of integration and mapping)
degrade to a **labelled** SQLite fallback that says so on screen — *"The
reporting endpoint for this screen did not answer. Shown from this application's
own ledger instead."* — rather than to a blank unavailable state.

> **Correction for the record.** `docs/FULL_APPLICATION_DELIVERY_STATUS.md`
> states *"the 14 legacy shell views … so the other 32 screens render their
> honest unavailable state"*. Neither number matches the code: it is 17 and 46,
> the tables are disjoint, and `state: "unavailable"` is emitted by exactly one
> router (`api/reports.py`) out of eleven — exports, integrations and closure use
> `unavailable: true` instead, and seven routers emit neither flag. That file is
> lead-owned and is not edited here; the measurement is reported so the lead can
> decide.

**The important half is that an absent database never reads as an empty result.**
A screen that said "no exceptions found" when the route was not mounted would be
telling a controller there is nothing to reconcile when nobody asked. `UAT-OUT-01`
asserts this.

### L-08 — `DEMO_USER` / `DEMO_PASSWORD` protect nothing. **VERIFIED**

`DEPLOY.md:7` says the app "demands a username and password before a single
screen loads" when these are set. They are read at exactly one place —
`app/run.py:182` — inside a condition that only prints a console warning. No
middleware or auth path in `app/backend/**` reads either. Setting them changes
nothing.

The app *does* have a real user model (server-derived sessions, PBKDF2 at 240,000
rounds, roles the caller cannot choose), which `DEPLOY.md` predates. But every
seeded password is `user_id + '!demo'`, published and derivable. **Do not expose
this build on a public URL.** See `docs/DEPLOYMENT_RUNBOOK.md` §4.

### L-09 — The committed deployment artifact cannot start. **VERIFIED**

`Dockerfile` and `render.yaml` both start `python app/run.py` against an empty
volume without migrating first, and the process deliberately refuses to migrate
itself. Both exit 1 on first boot. `Dockerfile` also omits `migrations/pg/`, so
the image cannot apply a PostgreSQL migration at all. Corrected commands are in
`docs/DEPLOYMENT_RUNBOOK.md` §2.

### L-10 — There is no down-migration. **VERIFIED**

`app/backend/pg/migrate_pg.py` has no `downgrade`, no `--down` and no reverse
SQL; migrations `001`–`022` are checksum-frozen and forward-only. **Rollback is
restore-from-backup**, and it depends on a backup having been taken before the
deploy. See `docs/DEPLOYMENT_RUNBOOK.md` §5.

### L-11 — Maker-checker cannot be demonstrated on this dataset. **VERIFIED**

`auth.require_separation` is real and unit-provable, and **no seeded identity can
reach it**: `pr.create` (Requestor, BudgetController) and `pr.approve`
(ProcurementApprover) are held by disjoint role sets, as are every other
create/approve pair, so `auth.require` refuses a would-be self-approver one line
before segregation is consulted. **Do not promise a live self-approval refusal in
a demonstration.** `UAT-SOD-04` asserts the disjointness so that a client role
mapping which breaks it cannot land silently. Detail in
`docs/UAT_ROLE_BASED_PLAN.md` §6, F-02.

### L-12 — The legacy PR approval route discloses existence and status before authorisation. **VERIFIED**

`services.approve_pr` reads the row and validates its status before calling
`auth.require`, so an authenticated caller holding no approval permission gets
`404 PR_NOT_FOUND` for an absent request and `409 INVALID_TRANSITION` — naming
the status — for a Draft one, where an approvable request correctly returns
`403`. Low impact, authenticated callers only, no money moves. The PostgreSQL
route does not have this shape. Detail in `docs/UAT_ROLE_BASED_PLAN.md` §6, F-01.

### L-13 — This is a development identity provider, not an enterprise IdP. **VERIFIED** (stated in the source)

No federation, no MFA, no password policy, no lockout, no lifecycle management.
What it does provide is the property the financial controls depend on: a
trustworthy, server-derived acting user with roles the caller cannot choose.
Production identity remains outstanding work.

### L-14 — The role-to-permission mapping is a placeholder awaiting client sign-off. **VERIFIED** (recorded as D-12)

Several assignments are documented least-privilege defaults, not resolved client
decisions — notably the Auditor's exclusion from `approval.read`,
`reconciliation.triage`, `export.create`, `settings.read`, `masters.read` and
`report.view.share`. They are defensible and they are not signed off. The UAT
reads the live table rather than a copy, so a change to any of them fails the
suite rather than drifting.

---

## 3. What is NOT claimed

* Nothing is marked LIVE or VERIFIED for Zoho.
* No performance, load or concurrency figure is offered. None was measured.
* No security assessment or penetration test has been performed.
* No accessibility conformance statement is offered — L-06 is an open AA failure.
* No claim is made about data-residency or regulatory compliance.
* No PostgreSQL-backed business flow is claimed as end-to-end UAT-tested; those
  scenarios were exercised to the authorisation boundary. See
  `docs/UAT_ROLE_BASED_PLAN.md` §4.

---

## 4. Outstanding external dependencies

None of these can be closed by engineering. Each needs a third party.

| # | Dependency | Blocks | Owner |
|---|---|---|---|
| **E-01** | **Written confirmation from Zoho support that Catalyst AppSail can reach an external PostgreSQL instance.** No published egress policy, no static egress IPs to allowlist, and the Database Connector CodeLib is documented for Functions, not AppSail. This is a **go/no-go architecture gate**, not a detail | the entire PostgreSQL deployment on Catalyst | Zoho support |
| **E-02** | Zoho ERP **sandbox tenant** and OAuth client — client id, client secret and initial refresh token, per data centre | any move off MOCK; every LIVE claim | Client / Zoho admin |
| **E-03** | Confirmation of the target product: **Zoho ERP v3** or **Books v3 + Inventory v1**. Recorded as D-14 and unresolved. Clients routinely say "Zoho ERP" about a Books + Inventory estate, and the two differ materially — Inventory can list purchase receives, ERP cannot | connector implementation, and the GRN design | Client |
| **E-04** | **Zoho ERP plan tier.** ERP is India-only, Standard/Premium only, and rate-limited to **2,000 / 10,000 API calls per day**. ERP Purchase Receives has four endpoints and **no list endpoint**, so PO-anchored discovery is the only GRN mechanism and its cost scales with open-PO count. On Standard this may bind hard enough to require a client conversation about sync frequency | sync frequency, and possibly feasibility | Client |
| **E-05** | Client **sign-off on the role-to-permission matrix** (D-12) | L-14; the final authorisation model | Client |
| **E-06** | **Product owner's approval of the `--warning` token change** (L-06). The remedy is identified and measured; `styles.css` is byte-frozen and the current UI is client-approved | WCAG 2.2 AA conformance | Product owner |
| **E-07** | A **decision on the exposed-demo posture** (L-08). Either provision real identities and remove the seeded ones, or accept that the build stays on a trusted network | any hosted client demo | Client / lead |
| **E-08** | **GL and fixed-asset register integration design** (L-02, X-01). This build records the capitalisation decision; where the journal and the asset record are actually created is unspecified | closing X-01 | Client / Finance |
| **E-09** | Note that **Catalyst Circuits and Integration Functions are not available in the India data centre** (Circuits also excluded in EU/AU/JP/SA/CA). Any design that assumed them needs a different mechanism | background orchestration design | Architecture |

E-01, E-04 and E-09 are sourced from this engagement's Catalyst and Zoho API
research, verified against official documentation on 2026-08-28. **This stream
made no Zoho or Catalyst call and holds no credential**; these are carried
forward, not re-derived.
