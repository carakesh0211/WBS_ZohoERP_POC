# Full application — delivery status

**Branch:** `full-application/build` · **Wave 7 CLOSED** · **Wave 8 in
progress** · full local suite **3601 passed, 615 skipped, 1 xfailed,
0 failed**

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
| **M3** | **`--warning` fails WCAG AA on every background it is used on** | **OPEN — needs approval, see below** |
| M4 | Six OAS-02 citations should be OAS-03 | **Closed** |
| M5 | Migrations 001–012 orphaned their ledger rows | **Closed** — 022 |
| M6 | Money-SQL gate blind across a nested paren | **Closed** |
| M7 | VRT inventory did not record `closure.spec.js` | **Closed** |
| M8 | A VRT test that passed when its feature was gone | **Closed** |
| M9 | "All four dimensions" true of the call, not the effect | **Documented** |
| L3, L4, L5 | `buildQuery` empty array; `SCOPABLE` gap; attribution | **Closed** |
| L1 | `FilterSet.buckets()` mapped `received_not_billed` to GRN | **Closed** — `0c8253e` moved it to PO, where the branch that emits it lives |
| L2, L6 | SCR-40 reveal gate; datatable's duplicate "Details" names | **Open, owner Wave 8 Agent B**, no live defect today |

### M3 is open by decision, not by oversight

`--warning` measures **4.484:1 on white** and **4.077:1 on its own tint**, and
`.st-warning` renders at 12px/600 — normal text, so AA needs 4.5:1. It fails on
every background it is actually used on; the shortfall is 0.42, not the 0.016
previously recorded in `C6_tokens.json`.

The remedy needs no new colour: `.msg-warning` already uses a darker warning
measuring 8.313:1 and 7.558:1. It is **not applied** because `styles.css` is
byte-frozen behind a SHA-256 pin and darkening an approved token is a visual
change requiring the product owner's approval. The false claim is corrected in
the contract with the measurements and the reason it is unapplied.

### The two LOW findings left open, and why

**L2 — SCR-40's reveal gate is non-discriminating.**
`connector-audit-log.js:80` gates the reveal on `connector.read` while the
route behind it gates on `audit.read`. The holder sets are not merely
overlapping, they are IDENTICAL — `('Administrator', 'Auditor')` for both — so
`state.canReveal` is unconditionally true and the withheld branch is
unreachable by any role in this build. No live defect. It becomes one the day
the two sets diverge, at which point the screen offers a control the API
refuses. One-line fix; deferred rather than taken mid-gate because
`mapping.spec.js` asserts against that constant.

**L6 — every per-row disclosure button is named "Details".**
`capex-datatable.js:113-131` gives them no `aria-label` and no `aria-controls`,
so a screen-reader user hears the same name for every row. It is a shared
Wave-2 component used by many screens, and changing a button's accessible name
mid-gate would move assertions in specs that are currently green.

Both are recorded here rather than fixed because neither produces a wrong
answer today and both touch surfaces the gate is measuring. Owner: Wave 8's
security and audit stream.

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

1. Wave 8's four streams, on migrations 023 (FX) and 024 (audit identity).
2. Final independent adversarial review; fix every high and medium finding.
3. The application gate: full local suite, live PostgreSQL, VRT, five green
   CI jobs, 40/40 screens, clean branch.

## Genuine blockers

**None blocking the build.** Open items, recorded rather than assumed closed:

- **M3 above** — awaiting a visual-change approval.
- **`bill.void` maker-checker is structurally inert.** `services.py:411` passes
  `b.get("created_by")` and the `bill` table has no such column, so
  `require_separation` short-circuits and the raiser of a bill can void it.
  Known, pinned by a **strict** xfail parametrised from `auth.MAKER_CHECKER`
  itself, so it cannot rot silently. The fix — add `created_by` to `bill` and
  populate it — is Wave 8 Agent B's.
- **Repair batches B and C, not yet written.** `rate = amount // units`
  truncates and the adapters emit *rate × quantity* rather than the line total
  (H-3); `currency`/`exchange_rate` stored and never applied (H-4);
  `recompute_commitment` zeroes commitment not originating in PostgreSQL
  `po_line` (H-7); three guards do not cover the code they were written for
  (H-9).
- **No local PostgreSQL.** 615 tests skip here and first execute in CI. A skip
  is not a pass.
- **Zoho remains MOCK.** No sandbox credentials, no live call has been made,
  and nothing is marked LIVE or VERIFIED.

## Test and commit status

- Full local suite: **3601 passed, 615 skipped, 1 xfailed, 0 failed** (`1b7f08e`)
- `closure.spec.js`: **119 passed, exit 0** — the six closure screens' first
  coverage, and it installs no routes
- `spa-routing.spec.js`: 55 passed · `integration.spec.js`: 161 passed against
  the real wiring · `analytics` + `mapping`: 122 passed, axe clean
- Manifest `--check` clean; baseline back at exactly **220**
- Migrations: **001…022, contiguous, no gaps**
- CI run 34309336564: Contract, Supply chain, Regression **green**; PostgreSQL
  **red on two tests**, both failing in the seed of the new RLS matrix and
  neither reaching an assertion about a policy (1517 passed); the fix is
  committed at `1b7f08e` and not yet pushed. Visual regression still running —
  the push is deliberately held, because three earlier pushes each cancelled
  that job before it could finish.
- Wave 7 is **not closed**.

## The new RLS matrix has never passed, and that is stated plainly

`tests/test_pg_rls_wave7_matrix.py` is the coverage for H3. Every test in it is
`@pytest.mark.pg` and skips on every machine here; all eight first executed in
CI, where three failed on the first run and two on the second — every one of
them in the test's own seeding, not in a policy assertion. Three CI cycles were
spent on one INSERT written from memory rather than from migration 018. The
seed is now derived from the migration: every NOT NULL column with no default,
and every CHECK on all seven tables the file seeds.

**No claim is made that these tests pass until CI says so.**

## Local preview

```
CAPEX_PROFILE=local-demo CAPEX_DB_PATH=app/data/capex_demo-8790.db PORT=8790 \
  python -m app.backend.migrate --fresh --seed && python app/run.py
```

**http://127.0.0.1:8790** — demo identities `U-REQ, U-PM, U-PLH, U-PROC, U-FIN,
U-PFC, U-CFO, U-AUD, U-ADM`; the password is the user id followed by `!demo`.
Seeded development credentials, displayed by the app's own sign-in screen; no
production credential exists.

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
