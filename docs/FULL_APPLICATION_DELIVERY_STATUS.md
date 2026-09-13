# Full application — delivery status

**Branch:** `full-application/build` · **Wave 7 CLOSED** · **Wave 8 IN
PROGRESS — NOT CLOSED** · full local suite at `64a164c`, after integration:
**4114 passed, 681 skipped, 0 failed**

**Historical figures, kept as the record of how the branch moved:**

| Measured at | Result | What it was |
|---|---|---|
| `1b7f08e` | 3601 passed, 615 skipped, 1 xfailed, 0 failed | Wave 7 gate. The xfail has since been removed along with the defect it pinned |
| mid-Wave-8 | 4109 passed, 679 skipped, **5 failed** | Measured by a stream agent BEFORE the lead applied the registry entries. All five WERE the missing integration: two manifest (two test files unregistered), two `test_pg_fx_ingest`, one `test_pg_rls_coverage` (`fx_translation_event` unregistered) |
| `64a164c` | **4114 passed, 681 skipped, 0 failed** | Current. Every one of those five is closed |

That middle row is kept deliberately. An agent measuring its own stream
correctly, before the lead had done the integration only the lead could do, is
not a defect in the agent's work — and deleting the number would hide that the
five failures had a single cause.

## 2026-09-12 — UAT release candidate (branch `fable-5.1/full-app-hardening-uat`)

**Deployed (latest):** `a65841f` (bundle sha256 `2eb7b06eba6a0adfb645bd736577a5cc651ae5b2efe2ac83c75e071768ef73b7`), records 12–13: outbound PO
creation ENABLED for ongoing UAT by the 2026-09-13 decision (LIVE_WRITE, permanent demo boundaries in code); PO-00009
`3912780000000125001` is the first application-originated order in DEMO WBS. Earlier: `2852be5` (bundle sha256 `c3dffe79012840f80189abd1527d8815a0a67886424496e44ba2503a217617a7`), records 10–11 in
`docs/fable51/STAGE_B_PERSISTENT_UAT.md`; bill sync proven (E2E ordered = received = billed = 185,000,000 paise, open 0). The
application-originated emission path (`/mode`, `/drain-outbox`, item ids and line fields on the wire) is deployed and waits at the
owner's credential checkpoint. Repository public; history scan clean. Earlier: `f19907b` (bundle sha256 `37eee12a56eca3d3d672973d87d753875ba5649979da6014d8526d5fe370372e`), Catalyst AppSail
`wbs-capex-uat` (project 4239000000144371, Development), https://wbs-capex-uat-50045784768.development.catalystappsail.in,
`/readyz` 200 schema 032 on Supabase `capex_tmpl_uat`. The CLI exposes no deployment id; the two deploy logs are the record.
**HEAD:** `532bb9d` (one commit ahead of the deployed build: the bill identity-rate fix, deploy pending the owner).
**Gates:** see `docs/fable51/CONTINUATION.md` "Sprint of 2026-09-12". CI pending GitHub billing.
**Demo cycle:** `docs/fable51/evidence/e2e/` — PR → availability → approval → order → gated emission → tenant PO-00008 /
GRN / bill → adoption link → receive mirrored → reconciliation balanced. Bill sync: blocked until the pending deploy.
**Release-blocking (open):** bill-detail sweep on UAT (fixed at HEAD, not deployed). **Post-UAT:** anchor Cron Function
deploy (audit verify is NEVER_ANCHORED), stray-schema cleanup (stopped at the zero-rows condition), write-scoped ERP
credential for app-side emission, a tester-facing trigger for sweep/adoption, restore drill, pg_dump 17 client tools.

## Fable 5.1 — independent hardening, UAT preview and the budget-creation correction

**Branch `fable-5.1/full-app-hardening-uat`** (from `full-application/build` @ 0b240fa),
pushed to the private origin. Wave 8 stays **OPEN**: the five CI jobs have not
executed since 06:10 on 2026-09-09 and the cause is now **confirmed as GitHub
billing**, not code — the check-run annotation reads "The job was not started
because recent account payments have failed or your spending limit needs to be
increased". Only the account owner can clear it. Local gates are authoritative
until then.

**Live PostgreSQL exists locally now.** A user-local PostgreSQL 16.10 runs the
whole `tests/test_pg_*.py` suite. At 0b240fa the inherited result was **4
failed / 1681 passed / 1 skipped** (36 min) — not the green the file above
claimed: a raw trigger error where a coded refusal was expected, a migration
DDL scanner blind to every `ALTER TABLE … ADD COLUMN` since 014, and two tests
with a stale signature. All four are fixed on this branch.

**Stage A UAT visual preview is DEPLOYED**: Catalyst project
`wbs-capex-uat-fable51` (id 4239000000144371, India DC, Development only),
AppSail `wbs-capex-uat`, `https://wbs-capex-uat-50045784768.development.catalystappsail.in`.
Synthetic seed, ephemeral SQLite, `UAT — SYNTHETIC DATA — ERP MOCK` banner,
non-derivable per-user credentials (hashes only in the bundle), `/docs`
unmounted, Zoho MOCK, outbound ERP writes disabled. Everything PostgreSQL-backed
answers a uniform 503 there by design. Full record and limitations:
`docs/fable51/STAGE_A_UAT_PREVIEW.md`.

**The budget-creation correction (product-owner brief, 2026-09-11)** — the
application could not create a budget through its own controls; AMB-04 is
resolved by decision: **Budget Category and Budget Head are separate
dimensions**. Delivered on this branch:

| Slice | State |
|---|---|
| Migration 026: `budget_category` master, `original_budget` + lines, category on cell and line, DB-level immutability after release, numbering series, custom fields widened to BUDGET | **applied, RLS-registered (57 coverage tests), 15 live workflow tests** |
| Service + API: draft/edit/submit/cancel/audit/list, category CRUD, governed selectors, CSV template/preview/all-or-nothing import, Idempotency-Key | **done; every mutating route in the auth matrix (209 passed)** |
| Approval engine: `ORIGINAL_BUDGET` binding and write-back → RELEASE creates and classifies cells, writes ORIGINAL lines, captures the approving authority | **done; verified end-to-end over HTTP on local PostgreSQL** |
| Seeded approval workflows | **every seeded predicate since Wave 4 was in a shape the compiler refuses** — rewritten, with a live test that compiles and routes the real seed |
| Local PostgreSQL demo (`tools/demo_pg.py`) | **done** — no core screen answers DATABASE_NOT_CONFIGURED |
| Frontend: Budget Setup screen, New Budget, governed selectors, category master, visible analytics/mapping navigation (only the two new groups collapse; approved groups' markup unchanged) | **integrated**; Budget Setup verified in the browser on the PostgreSQL demo; rail does not overflow at 1440×900 |
| Reporting: `budget_category` as an independent FilterSet dimension, six declared-and-refused dimensions, grouping, exports accepting a REAL FilterSet (they never had) | **integrated** (9 commits); live reporting/export suites 625 passed |
| Guards: write-back registry, 026 rollback ledger line, scope chokepoint (19 exempt reasons), SCOPABLE, money-float guard on the editor | **all six inherited guards green again** |

**Wave 8 items closed on this branch after the correction:** M-1 (audit-anchor Cron Function shim `catalyst/functions/capex_audit_anchor/` sharing one entry point with the CLI; folder built, not deployed), M-2's remaining half (anchors record each stream's genesis; `RECREATED_AFTER_ANCHOR` reported only when an anchor can prove it), M-4 (023 RLS matrix live as `capex_app`, 6 passed), and a foreign-currency purchase order is now REFUSED at emission (`PO_CURRENCY_NOT_EMITTABLE`) instead of relabelled INR — emitting in the PO's own currency needs source-minor amounts on `po_line`, which is remaining work.


Also integrated on this branch: login throttling, the ERP outbound-write gate,
Zoho route 404/audit discipline, period-reopen maker-checker and permissions
(M-3), the H-7 carried-commitment preservation with migration 027, the FX
re-translation ordering fix, and the `--n500` hovered-row contrast correction
(approved; pin moved by the documented procedure).

## Current milestone

**M7 — Reconciliation, dashboards and reports.** Reporting backend, export
jobs, closure services and migrations 017–022 are merged, and the adversarial
review's findings are closed. What is outstanding is CI evidence, not code.

## Completed vertical slices

| Wave | Delivered |
|---|---|
| 1–4 | PostgreSQL foundation, settings/masters, budget control cells and periods, identity/scope/RLS, configurable approval engine |
| 5 | Integration platform: schema, inbox/outbox, chunked jobs, rate budget, circuit breaker, outbound PO emission, `/api/integrations/*` with six coded refusals, unattributed-exception triage |
| 6 | Migration 013's eight procurement tables · PR→PO services · GRN and vendor-bill inbound · PostgreSQL reconciliation · 014's twelve corrections · 015's PR reservation grain |
| 7 | Migrations 016–022 · reporting backend and canonical `FilterSet` · export jobs under the requester's scope · closure services · 11 analytics screens · 4 mapping screens · 6 closure screens · **the SPA registry repair** · **the adversarial review's 8 HIGH and 8 of 9 MEDIUM findings** |

## The Wave 7 finding that mattered most

**Thirty-three screens were built, tested and unreachable.** None was
registered in the running application:

* Wave 5's **twelve integration screens** — `features/integration/manifest.js`
  states the two splices verbatim and says "the lead performs the two splices
  below". The lead never did. `viewAllowed('integration-setup')` returned
  `false` in a real browser for three waves.
* Wave 7's **eleven analytics** and **four mapping** screens — same omission.
* Wave 7's **six closure screens** (SCR-11/12/14/20/21/22) — worse: no manifest
  existed, nothing imported them, and **no test referred to them**, so nothing
  could have failed.

**Why the suite never said so.** `integration.spec.js`, `analytics.spec.js` and
`mapping.spec.js` each performed the missing splices *themselves* with
`page.addInitScript`, pushing rows into `SCR_ROUTES` and overriding `V`. Each
was green against a build that existed only inside the test, and integration's
"the two tables agree" assertion was self-fulfilling — it wrote the rows it then
read back. The one test that caught it reads the registry instead of writing to
it.

All three helpers now count what the **shipped** build declares, so
`waitForFunction(… === 12)` is an assertion about `app.js`. The exact registry
list went 13 → 46, **extended and never relaxed**: its purpose is that a screen
cannot become routable without a human naming it, and naming it is honest only
if the screen has a test.

## Screen inventory — 40 of 40

46 routable screens carrying **37 distinct SCR numbers**; with the three legacy
shell views `pos`, `grns`, `bills` (SCR-15/16/17) that is C8's full forty.
Verified in a browser, not inferred.

## Adversarial review — 8 HIGH, 9 MEDIUM, 6 LOW

| | Finding | Status |
|---|---|---|
| H1 | `sort=utilisation_pct` paginated on a ratio, cursored on a percentage | **Closed** |
| H2 | Period close ignored unattributed reconciliation exceptions | **Closed** |
| H3 | 017–019 policies never enforced (superuser bypass) | **Closed** — behavioural matrix |
| H4 | `export_job` in no RLS registry; guard vacuous | **Closed** |
| H5 | SCR-23 read totals keys the API never returns | **Closed** |
| H6 | Fallback declared filters applied that the routes ignore | **Closed** |
| H7 | WBS tree unusable by keyboard | **Closed** |
| H8 | `closure.spec.js` untracked | **Closed** |
| M1 | Saved-view `FOR ALL` policy defeatable on DELETE and UPDATE | **Closed** — 022 |
| M2 | Reports router had no write permission above the floor | **Closed** |
| **M3** | `--warning` failed WCAG AA as TEXT on every background it is used on | **Closed** — approved and applied in Wave 8, see below |
| M4 | Six OAS-02 citations should be OAS-03 | **Closed** |
| M5 | Migrations 001–012 orphaned their ledger rows | **Closed** — 022 |
| M6 | Money-SQL gate blind across a nested paren | **Closed** |
| M7 | VRT inventory did not record `closure.spec.js` | **Closed** |
| M8 | A VRT test that passed when its feature was gone | **Closed** |
| M9 | "All four dimensions" true of the call, not the effect | **Documented** |
| L3, L4, L5 | `buildQuery` empty array; `SCOPABLE` gap; attribution | **Closed** |
| L1 | `FilterSet.buckets()` mapped `received_not_billed` to GRN | **Closed** — `0c8253e` moved it to PO, where the branch that emits it lives |
| L2, L6 | SCR-40 reveal gate; datatable's duplicate "Details" names | **Closed** — both delivered by Wave 8 stream B, see below |

### M3 was open by decision. The decision has now been made.

`--warning` measured **4.4843:1 on white**, **4.2173:1 on `--n50`**,
**4.0775:1 on its own tint** and **3.9604:1 on `--n100`**, and `.st-warning`
renders at 12px/600 — normal text, so AA needs 4.5:1. It failed on every
background it was actually used on; the shortfall was 0.42, not the 0.016 once
recorded in `C6_tokens.json`.

**The product owner approved the fix and it is applied.** Warning TEXT takes
`#6D4600`, the darker amber `.msg-warning` always used — 8.3125:1 on white,
7.8176:1 on `--n50`, 7.5585:1 on the tint, 7.3413:1 on `--n100`. No new colour
entered the registry, so neither palette gate moved.

Two further body-size instances the review had listed as unmitigated
(`analytics.css`'s dropped-filter chip and stale-freshness line) and **two more
that measurement found and nobody had listed** (`closure.css`'s posting note at
12px and write-off badge at 11px/600, both 4.0775:1 on the tint) are corrected
in the same change.

`--warning` itself is unchanged and still declared. It is now a NON-TEXT tone
only — dots, tile and border accents, meter fills, chip borders — which need
3:1 and clear it everywhere (3.9604:1 worst case). `C6_tokens.json`'s
`contrast_rule` records that split instead of asserting a conformance that was
not true. The `styles.css` SHA-256 pin was updated in the same commit through
the documented approved-change procedure.

**Not closed by this, and not covered by that approval:** `--n500` (#6B7280)
measures 4.8345:1 on white but **4.3210:1 on a hovered table row**, so muted
text in a hovered row still fails AA application-wide. See `L-06` in
`docs/RELEASE_PACKAGE.md`.

### The two LOW findings that were left open — both are now fixed

This section used to argue at length for leaving them open, and the table two
screens above listed them as `Open, owner Wave 8 Agent B`. That stream
delivered them. Both entries were stale; the code is the record.

**L2 — SCR-40's reveal gate.** It gated the reveal on `connector.read` while
the route behind it gated on `audit.read`. The holder sets were IDENTICAL —
`('Administrator', 'Auditor')` — so there was no live defect, only one waiting
for the day the sets diverged. **Fixed:** `connector-audit-log.js` now declares
`const REVEAL_PERMISSION = 'audit.read';` and consumes it, with a comment
naming this finding.

**L6 — every per-row disclosure button was named "Details".** No `aria-label`
and no `aria-controls`, so a screen-reader user heard the same name for every
row. **Fixed:** `capex-datatable.js` now sets ``aria-label: `Details for
${label}` `` and `aria-controls: detailId`, and updates the label on toggle.

(The line numbers this section used to cite — `connector-audit-log.js:80` and
`capex-datatable.js:113-131` — now point at comment openings and unrelated
skeleton-row rendering. Symbols, not line numbers.)

## Wave 7 gate — MET, on evidence

CI run **34317944466** at commit **`872f57a`**: **all five jobs green.**

| Job | Result |
|---|---|
| Contract and inventory gates | green |
| Supply chain | green |
| Regression suite | green |
| PostgreSQL integration suite | green — **1520 collected, 1519 executed, 1 skipped, 0 failed, 0 errored** |
| Visual regression (approved UI) | green — **1814 passed, 4 skipped** |

The PostgreSQL result is the one worth naming. `tests/test_pg_rls_wave7_matrix.py`
had never passed anywhere: it skips on every machine here, and its first two CI
runs failed — three tests, then two, every one of them in the test's own
seeding rather than in a policy assertion. All eight now execute against a live
database as `capex_app` and pass, so migrations 017–019's policies are enforced
and proven rather than declared. That was the finding where weakening any 019
policy to `USING (true)` left the whole suite green.

The single PostgreSQL skip is pre-existing and not from this wave:
`test_pg_reconciliation.py:821` skips on "got empty parameter set" — a
parametrised test with nothing to run. Recorded as a mild vacuity for a later
pass rather than counted as coverage.

## Current slice

**Wave 8** — foreign-currency and period controls; security and audit closure;
migration and operational hardening; UAT and release package.

## Next three deliverables

1. Wave 8's four streams, on migrations 023 (FX), 024 (audit identity) and
   025 (FX applied at ingestion).
2. Final independent adversarial review; fix every high and medium finding.
3. The application gate: full local suite, live PostgreSQL, VRT, five green
   CI jobs, 40/40 screens, clean branch.

## Genuine blockers

**None blocking the build.** Open items, recorded rather than assumed closed:

- **Five local test failures**, none of them in this stream's files:
  `test_manifest.py` x2 (a new `tests/test_pg_anchor_job.py` carrying 32
  tests is not in the manifest, so the baseline reads 299 against a pinned
  220 — `python tools/build_test_manifest.py` is the recorded remedy),
  `test_pg_fx_ingest.py` x2, and `test_pg_rls_coverage.py` x1. Owners are
  the backend streams.
- ~~**`bill.void` maker-checker is structurally inert.**~~ **NO LONGER TRUE,
  and this file contradicted itself about it** — it was listed here as a
  blocker while the Wave 8 table below credited stream B with closing it. The
  code settles it: `bill` carries `created_by` in `app/backend/db.py`,
  `services.py` passes it with `require_maker=True` so an unattributed bill is
  refused rather than waved through, and the strict xfail is gone —
  `tests/test_approval_maker_checker.py` now reads `KNOWN_INERT: dict[str,
  str] = {}`. (The old line citation, `services.py:411`, had also drifted; that
  line is now a different function's signature.)
- **Repair batches B and C, not yet written.** `rate = amount // units`
  truncates and the adapters emit *rate × quantity* rather than the line total
  (H-3); `currency`/`exchange_rate` stored and never applied (H-4);
  `recompute_commitment` zeroes commitment not originating in PostgreSQL
  `po_line` (H-7); three guards do not cover the code they were written for
  (H-9).
- **No local PostgreSQL.** Re-measured rather than restated: **593 of the
  `test_pg_*` tests skip on this machine**, out of **679 skips** across the
  whole suite. They first execute in CI. A skip is not a pass. (This file
  carried `615` here and `656` further down — two numbers for one measurement,
  neither reproducible today.)
- **Zoho remains MOCK.** `app/backend/zoho.py` declares `MODE = "MOCK"` and
  makes no network call at all: its connectivity screen derives its result from
  a stored `oauth_status` column and a scope comparison. No sandbox credential
  exists, no live call has ever been made, and nothing is marked LIVE or
  VERIFIED.
- **Repair batches H-1 and H-2 are closed, not open** — see the FX section
  below, which used to describe them as outstanding.

## Test and commit status

- Full local suite at `64a164c`: **4114 passed, 681 skipped, 0 failed**. See
  the table at the head of this file for the two earlier measurements and why
  each differed.
- `closure.spec.js`: **119 passed, exit 0** — the six closure screens' first
  coverage, and it installs no routes
- `spa-routing.spec.js`: 55 passed · `integration.spec.js`: 161 passed against
  the real wiring · `analytics` + `mapping`: 122 passed, axe clean
- Manifest `--check`: **clean**, baseline back at exactly **220**.
  `test_pg_anchor_job.py` (32 tests) and `test_pg_fx_ingest.py` (47) are both
  registered post-baseline with the reason each exists. The 299 this line used
  to report was the pre-registration count.
- Migrations: **001…025, contiguous, no gaps** — 25 files under
  `migrations/pg/`, all tracked, ending `025_fx_applied_at_ingestion.sql`
- CI run 34309336564: Contract, Supply chain, Regression **green**; PostgreSQL
  **red on two tests**, both failing in the seed of the new RLS matrix and
  neither reaching an assertion about a policy (1517 passed); the fix is
  committed at `1b7f08e` and not yet pushed. Visual regression still running —
  the push is deliberately held, because three earlier pushes each cancelled
  that job before it could finish.
- Wave 7 is **CLOSED**; Wave 8 is **not**.

## The Wave 7 RLS matrix — it has now passed, and here is the whole arc

> **Scope marker.** Everything in this section, and the all-green CI table
> further up, is about **Wave 7**. It is not evidence about Wave 8 — see "CI IS
> BLOCKED, AND NOT BY THIS CODE" below, which is the current state.

This section previously read "has never passed" and sat two screens below a
table saying the gate was MET. Both were written honestly and one went stale;
carrying a contradiction in the same file is worse than either.

`tests/test_pg_rls_wave7_matrix.py` skips on every machine here. All eight
tests first executed in CI, where **three failed on the first run and two on
the second** — every one of them in the test's own seeding, none reaching a
policy assertion. Three CI cycles went on one INSERT written from memory
rather than from migration 018. **They passed in run 34317944466**, which is
what let Wave 7 close.

## Wave 8 — what each stream delivered, and what the final review found

> Wave 8 is **not closed**. This section records delivery by stream; the
> gate is not met while CI cannot run and five local tests fail.

| Stream | Delivered |
|---|---|
| **A** financial & period | Migration 023: FX translation engine, approval-gated period reopen with concurrency and idempotency tests that bite |
| **B** security & audit | Audit anchor writer and verifier (migration 024), `bill.created_by` closing an inert maker-checker — in `app/backend/db.py`, **not** in 024, which contains no such column; PostgreSQL has carried `bill.created_by` since 013 — secret-provider concurrency, L2 and L6 |
| **C** migration & ops | Deterministic export/import, reconciliation to the paisa, restore drill, SBOM, `pip-audit` clean |
| **D** UAT & release | 358-assertion role matrix, browser UAT, four client documents |

**Two of the four agents died at a usage limit mid-task.** The final
adversarial review was pointed at that seam and found three HIGH defects there.

### H-1 — the FX engine had no caller. SINCE WIRED; this entry was stale.

**The falsifiable claim this finding rested on now returns the opposite
result.** It said: *"`grep` for `source_currency` outside `fx.py` returns
nothing."* It does not. `source_currency` appears in
`app/backend/pg/procurement.py` and `app/backend/integration/sweeps.py`, and
`procurement.py` opens with `from . import fx as fx_svc`, calls
`fx_svc.translate_lines` from its bill-mirror path and reads
`fx_svc.BASE_CURRENCY`. `migrations/pg/025_fx_applied_at_ingestion.sql` was
written specifically to close this, and its own header quotes this grep as the
state it was fixing.

The arithmetic was never the problem and is unchanged — `Decimal` throughout,
exponent honoured per currency, half-away-from-zero symmetric, rate applied
exactly once, every `SUM()` cast `::bigint`. It was a wiring gap, and the
wiring is in.

**One thing here is still true and still needs doing:**
`FINDINGS_REMEDIATION_STATUS.csv` row `AUD-H-007` still carries the note
*"Foreign-currency conversion is still NOT implemented - exchange_rate is
stored but not applied"*. That note is now wrong, and the CSV is the record
other documents defer to. It is not edited from this stream — flagged for its
owner.

### H-2 — the bill-ingestion path could silently un-translate. SINCE FIXED.

Both halves of this finding have been closed, and both are readable in
`app/backend/pg/procurement.py`:

* it no longer *"never sets `source_currency` or `source_amount_minor`"* — the
  bill insert writes `source_currency`, `fx_rate`, `fx_rate_id`,
  `fx_rate_date`, `fx_rate_source` and `fx_translated_at`, and the line insert
  writes `source_amount_minor`, `source_tax_minor` and `source_freight_minor`;
* the `ON CONFLICT DO UPDATE` no longer overwrites a translated row. Each
  affected column is now wrapped `CASE WHEN {BILL}.fx_translated_at IS NULL
  THEN EXCLUDED.… ELSE {BILL}.… END`, and `fx_translated_at` itself is
  `COALESCE`d — which is precisely the overwrite this finding described.

**Still not executed against PostgreSQL**, for the same reason as everything
else here: there is no local instance and CI cannot start jobs. Read as
reviewed, not as verified.

### H-3 — the read-before-authorise fix was applied to one function of three.

`approve_revision` and `approve_capitalisation` had `approve_pr`'s exact shape
behind routes gated on authentication only. Reproduced over HTTP as a read-only
Auditor: 404 = does not exist, 409 = exists and here is its status, 403 =
exists and is approvable. **FIXED**, with a pattern guard that walks the AST of
every `approve_*` so a fourth one fails there rather than in the next review.

### Also open, from the same review

- **M-1** the audit anchor **writer has no invocation path** — no route (by
  design), no CLI, no schedule. `audit_anchor` stays empty, so whole-stream
  truncation detection is inert in every deployment. The verifier is honest
  about it: `anchored: false` is explicitly not a pass.
- **M-2** `verify_anchors` compares only against the **newest** anchor, so a
  stream deleted and then re-anchored reports `intact=True`.
- **M-3** `period.reopen` is in neither `auth.PERMISSIONS` nor `MAKER_CHECKER`,
  so that limb of `require_separation` is inert. Mitigated: `periods.py`
  compares unconditionally, 023 adds two CHECK constraints, and a test
  monkeypatches `require_separation` to a no-op and proves the refusal still
  fires.
- **M-4** no **live** RLS enforcement test for migration 023's five tables.
  Registered correctly on both sides and covered by catalog introspection;
  whether an ENT-A principal can read ENT-B's `period_reopen_request` is
  UNVERIFIED.

## CI IS BLOCKED, AND NOT BY THIS CODE

Every job in both runs since 06:10 fails in 3–4 seconds having executed **zero
steps**, with no logs written at all (`BlobNotFound`). `.github/workflows/` is
byte-identical to run **34317944466**, which was green on all five jobs.

**This is an EXTERNAL BLOCKER whose cause is UNCONFIRMED.** Exhausted GitHub
Actions minutes on a private repo is one hypothesis and it is **not proven** —
the billing endpoint needs an auth scope this session does not hold, so it has
not been checked. It is not the only hypothesis the observed facts fit: an
organisation policy change, a repository or runner-label setting, and a
provider-side incident would all look the same from here. Nothing above ranks
them, and this file should not pretend otherwise.

**Consequence, stated rather than worked around: the Wave 8 batch is
UNVERIFIED in CI.** The local suite is green, but the two jobs that have caught
a real defect in every wave — PostgreSQL and VRT — have not run against it. 656
tests skip locally and have only ever executed in CI; the tests for migrations
023, 024 and 025 are in that set.

## Local preview

```
CAPEX_PROFILE=local-demo CAPEX_DB_PATH=app/data/capex_demo-8790.db PORT=8790 \
  python -m app.backend.migrate --fresh --seed && python app/run.py
```

**http://127.0.0.1:8790** — demo identities `U-REQ, U-PM, U-PLH, U-PROC, U-FIN,
U-PFC, U-CFO, U-AUD, U-ADM`; the password is the user id followed by `!demo`.

**These are VISIBLE SEEDED CREDENTIALS, and they are the whole of the
authentication story.** The sign-in screen lists the user ids and every
password is derivable from the id in one guess. `DEMO_USER` and
`DEMO_PASSWORD` do not change that — they are read at exactly one place,
`app/run.py`'s `main()`, only to decide whether to print a warning, and nothing
in `app/backend/**` reads either. **This build is therefore unsuitable for a
publicly shared or production deployment**; keep it on a trusted network. No
production credential exists in it, which is a statement about what it
contains, not a licence to host it.

**What shows live data without PostgreSQL.** Measured on the running demo, and
both numbers this file used to carry were wrong:

* The shell has **17 NAV entries**, not 14, and `SCR_ROUTES` has **46**. The
  two sets are **disjoint** — no id appears in both — so there is no "other
  32". There are 63 routable view ids: 17 shell views and 46 SCR-nn screens.
  The 14 in the plan is the count of POC views that map onto an SCR number,
  which is a different quantity.
* Of **55 parameterless GET endpoints**: **22 serve live data**, **32 answer
  `503 DATABASE_NOT_CONFIGURED`**, one answers something else. The 503 is
  uniform. **`state: unavailable` is not** — only **4 of those 32** carry a
  `state` field; the rest answer RFC-7807 without one. A frontend keying
  "unavailable" off that field alone would misread 28 endpoints, so screens
  must treat the 503 itself as the signal.

Zoho shows `MOCK — NOT VERIFIED`.

## Flake classification of record — SCR-39, 2026-09-08

CI run 34203327903 failed one VRT test at laptop-1024 only, on a 15 s selector
timeout. **Classified as resource contention on evidence:** the failing run took
54 minutes for a spec that takes ~17, with five agents competing for the
machine; the same test passed at the other two viewports in that run; and it
passed 3/3 sequentially on an idle machine in 37.0 s, 23.2 s and 25.5 s. Nothing
was changed to obtain that. If it recurs on an idle machine the classification
is wrong rather than merely re-asserted.

A second timeout of the same shape appeared at tablet-800 in run 34266699000 —
`#loginForm` hidden for 30 s, 47 minutes into the run. It is **not** classified
here: that commit's `integration.spec.js` still carried the `addInitScript`
route simulation since removed, so the build it failed against no longer exists.

## Corrections of record

Claims previously in this file that were false, withdrawn rather than quietly
edited:

- *"Phase 1 — PostgreSQL port, behaviour-identical | complete"* — the port
  omitted the entire procurement document chain. Migration 013 closed that.
- *"Waves 1-3 COMPLETE … Wave 4 in progress"* — stale by two waves.
- *"19 of C8's 40 screens still unbuilt"* — they were built. Thirty-three were
  unreachable, which this file reported as delivery.

## Fable 5.1 — FX administration

- Migration 028 gives `fx_rate` a lifecycle (`active`, activation/deactivation metadata, `superseded_by`) and a trigger that refuses any change to a recorded quote's value, pair, date, source or provenance, and refuses DELETE: rate history is append-only at the database. `ux_fx_rate_natural` is now partial over ACTIVE rows so a correction can exist beside the row it supersedes. `fx_policy.FX_ACTIVATION_SEPARATION` is seeded `REQUIRED`.
- `pg/fx_admin.py` + `api/fx_admin.py` (`/api/fx/rates`: list with cursor, get, create, activate, deactivate, import/preview, import, lookup, history) under `fx.read` (every role EXCEPT the Auditor: AUD-C-006 pinned that role's exact read set and widening it is a client decision recorded against D-12) / `fx.manage` (FinanceApprover, Administrator). A quote created here starts AWAITING_ACTIVATION (not "PENDING": that word is an integration/approval status and the business-screen gate refuses it) and is activated by a different user (`FX_SELF_ACTIVATION`); activation supersedes the previous active quote for the same key. Rates are exact decimal strings in both directions; exponents come from `money.py`.
- The lookup — `pg/fx.py::resolve_basis` at the bill write and `/api/fx/rates/lookup` on the screen — reads ACTIVE quotes only and refuses a date none covers with `FX_RATE_UNAVAILABLE` (404 on the screen); an inactive `fx_rate_id` is `FX_RATE_INACTIVE`. There is no fallback to another day, a retired row, or 1.
- Screen `fx-rates` ("Exchange Rates", Governance, after Budget Categories): table, create, activate/retire with confirmation, per-pair history, "Test lookup" showing the coded refusal verbatim, and JSON/CSV import with preview → all-or-nothing commit under an Idempotency-Key. Seed `seed_parts/010_fx_rates.sql` quotes USD/EUR/JPY/KWD→INR for 3–7 Aug 2026 with the 6th missing, one superseded EUR quote and one pending JPY quote.
- Proof: `tests/test_pg_fx_admin.py` (16, live PostgreSQL) and `tests/test_api_fx_guard.py` (43, no server), both post-baseline in the manifest; the existing `test_pg_fx.py` / `test_pg_fx_ingest.py` (98) pass unchanged on 028.
- Not done: no Playwright spec for the screen; the import accepts unquoted CSV only (a `source_reference` containing a comma must use the JSON form); rate rows recorded by ingestion (`record_rate`) remain active on creation, by design, and are not subject to activation separation.

## Fable 5.1 — a purchase order in the currency the vendor quoted

- Before: `purchase_order.currency` and `exchange_rate` existed since 013 and the rate was multiplied into nothing; `po_line` held base paise the caller typed; the outbound DTO defaulted `currency_code` to INR, so a USD order reached the vendor labelled INR and priced in INR paise. The first Fable 5.1 step made the connector **refuse** a non-INR order (`PO_CURRENCY_NOT_EMITTABLE`) rather than mislabel it. This stream makes it emittable, exactly.
- Boundary (`5acb270`): both adapters' `render_paise` take the currency's minor exponent (JPY 0, KWD 3; default 2, twin table unchanged) and `_emission_body` renders at `minor_exponent_of(po.currency_code)`; `outbound.plan_emission` stamps `currency_code` on every draft — one, or one per control cell — and `PurchaseOrderDraft` refuses a malformed code before an outbox row exists.
- Migration 029: `po_line.source_amount_minor` / `source_rate_minor` (the vendor's figures, NULL on an INR line, required on every line of a foreign order by a trigger that reads the header; immutable), `purchase_order.fx_rate_id / fx_rate_date / fx_rate_source / source_minor_exponent` with `ck_purchase_order_fx_provenance` (023's bill rule applied to the order), `fk_purchase_order_currency` → `currency_denomination`, a header-basis immutability trigger, and `fx_translation_event` admitting `PURCHASE_ORDER`. Added VALIDATED: a pre-029 foreign order without provenance fails the migration by design and is reviewed by hand (the PostgreSQL seed carries no purchase orders).
- Writer: `create_po` / `convert_pr_to_po` resolve the basis **once**, before the budget check, through `fx.resolve_basis` — the ACTIVE rate on file for the document date (028: a pending or retired quote is not on file), or the caller's rate with its named source, or a named `fx_rate_id`; none → `FX_RATE_UNAVAILABLE` and nothing is written. `fx.translate_lines` applies the rate to the document total and allocates paise without loss, so the lines sum to the header exactly and an order and its bill at the same rate agree to the paisa; the source per-unit price must divide exactly (`PO_LINE_RATE_NOT_EXACT`). Base paise on a foreign line, source figures on an INR line, or a rate on an INR order are each refused by code. A foreign conversion names the vendor's figure per request line (`source_lines`) — a rupee estimate is not divided into a dollar quote.
- Emission: `plan_po_emission` emits a foreign order at its **source** figures under its own `currency_code`; a foreign line without them (pre-029) is `PO_CURRENCY_SOURCE_MISSING` per line, before planning. The DTO's three identities (lines sum to subtotal, subtotal + tax = total, rate × quantity = line total) hold in the order's minor units.
- Proof: `tests/test_pg_po_currency_fable51.py` (9, live PostgreSQL: refusal writes nothing; pending rate not on file; vendor figures + translated paise + provenance + one ledger row; budget checked on the paise the rate produced; ambiguity by source; service and database refusals of both wrong shapes; immutability; planned in USD at 19.99; conversion with and without `source_lines`) and `tests/test_po_emission_currency_fable51.py` (54, no database: exponent rendering on both adapters, currency on every draft/payload/DTO/body, legacy refusal). API: `_LineIn` gains `source_amount_minor` / `source_rate_minor`; the order and conversion bodies gain `rate_source`, `fx_rate_id`, `document_date` (and `source_lines`); `exchange_rate` is now optional (None = the rate on file). No new routes.
- Proof over HTTP on the local PostgreSQL demo (2026-09-11, `tools/demo_pg.py --serve`, signed in as the seeded procurement approver, project PRJ-DM-002): a USD order dated 2026-08-06 (no seeded rate) → 409 `FX_RATE_UNAVAILABLE`; `amount_paise` on a USD line → 422 `PO_BASE_AMOUNT_ON_FOREIGN_ORDER`; dated 2026-08-05 → 201 `PO-0001` at the seeded RBI rate 83.77 (`FXR-DM-USD-0805`): line `source_rate_minor` 1999 × 3 = `source_amount_minor` 5997, translated `amount_paise` 502,369 (5997 × 83.77 = 502,368.69, HALF_UP), header provenance and one `fx_translation_event` row of kind PURCHASE_ORDER carrying the same figures. The demo seed leaves every WBS element Draft, so the smoke released one first; that is a seed choice, not a defect.
- Not done: the Purchase Order screen does not yet offer a currency selector or source-minor entry (the API and the workflow are complete; the UI still raises INR orders); GRN and bill matching against a foreign order still compare base paise (a receipt carries no currency in the contract — reported, not papered over); `reconciliation_lines` still renders `exchange_rate` as a string and does not yet show the provenance.
