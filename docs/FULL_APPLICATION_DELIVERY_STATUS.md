# Full application — delivery status

**Branch:** `full-application/build` · **Wave 7 in its completion gate** ·
full local suite **3589 passed, 589 skipped, 1 xfailed** at `824974d`

## Current milestone

**M7 — Reconciliation, dashboards and reports.** Reporting backend, export
jobs, closure services and migrations 017–021 are merged. What remained was
not backend work at all: the screens existed and could not be reached.

## Completed vertical slices

| Wave | Delivered |
|---|---|
| 1–4 | PostgreSQL foundation, settings/masters, budget control cells and periods, identity/scope/RLS, configurable approval engine |
| 5 | Integration platform: schema, inbox/outbox, chunked jobs, rate budget, circuit breaker, outbound PO emission, `/api/integrations/*` with six coded refusals, unattributed-exception triage |
| 6 | Migration 013's eight procurement tables · PR→PO services · GRN and vendor-bill inbound · PostgreSQL reconciliation · migration 014's twelve corrections · migration 015's PR reservation grain |
| 7 | Migrations 016–021 · reporting backend and canonical `FilterSet` · export jobs under the requester's scope · closure services · 11 analytics screens · 4 mapping/connector screens · **the SPA registry repair below** |

## The Wave 7 finding that mattered most

**Thirty-three screens were built, tested and unreachable.** Not one of them
was registered in the running application:

* Wave 5's **twelve integration screens** — `features/integration/manifest.js`
  states the two splices verbatim and says "the lead performs the two splices
  below". The lead never did. `viewAllowed('integration-setup')` returned
  `false` in a real browser for three waves.
* Wave 7's **eleven analytics** and **four mapping/connector** screens — same
  omission, discovered when the registry control finally failed.
* Wave 7's **six closure screens** (SCR-11/12/14/20/21/22) — worse: no
  manifest existed at all, nothing imported them, and **no test referred to
  them**, so nothing could have failed.

**Why the VRT suite never said so.** `integration.spec.js`, `analytics.spec.js`
and `mapping.spec.js` each performed the missing splices *themselves* with
`page.addInitScript`, pushing rows into `SCR_ROUTES` and overriding `V`. Each
suite was green against a build that existed only inside the test, and
integration's "SCR_ROUTES and the manifest agree" assertion was self-fulfilling
— it wrote the rows it then read back.

The one test that caught it was the one that **reads** the registry instead of
writing to it: `spa-routing.spec.js:505`, whose exact-list assertion failed the
moment the screens were spliced in. That list has been **extended, not
relaxed** — from 13 to 46 — because its stated purpose is that a screen cannot
become routable without a human naming it, and naming it is only honest if the
screen has a test. Turning it into an "at least these" check would have deleted
the property rather than satisfied it.

All three specs now count what the **shipped** build declares, so
`waitForFunction(… === 12)` is an assertion about `app.js` rather than a wait on
the test's own side effect. If a splice is ever reverted, the suite fails.

## Screen inventory — 40 of 40

46 routable screens carrying **37 distinct SCR numbers**; with the three legacy
shell views `pos`, `grns` and `bills` (SCR-15/16/17) that is C8's full forty.
Verified in a browser, not inferred: `closure-capitalisation`,
`closure-asset-allocation`, `integration-setup` and `analytics-executive` each
mount with `data-mounted="1"`, their C8 verbatim headings, and real content.

## Current slice

**Wave 7 completion gate.** Outstanding: `tests/vrt/closure.spec.js` (the six
closure screens have wiring but not yet their own spec), the full VRT run at
the current tree, and five green CI jobs.

## Next three deliverables

1. `closure.spec.js`, then the full VRT and local suites at the gate commit.
2. Wave 7 adversarial review; fix every high and medium finding.
3. Wave 8 — foreign-currency and period controls, security and audit closure,
   migration and operational hardening, UAT and release package.

## Genuine blockers

**None blocking the build.** Open items, recorded rather than assumed closed:

- **Repair batch B, not yet written.** `rate = amount // units` truncates and
  the adapters emit *rate × quantity* rather than the line total, so a 3-unit
  ₹1,000.00 line reaches the vendor as ₹999.99 (H-3); `currency` /
  `exchange_rate` are stored and never applied, and the API types the rate as
  `int` (H-4); `recompute_commitment` updates zero rows when a ledger cell is
  absent (H-6) and, being a full re-derive, zeroes any commitment not
  originating in PostgreSQL `po_line` (H-7).
- **Repair batch C, not yet written.** Three guards do not cover the code they
  were written for (H-9), plus M-1 float quantity, M-4 scope waived on NULL,
  M-5 dead `tax_paise`.
- **`core/api-client.js::buildQuery` comma-joins arrays** via `q.set`. Analytics
  worked around it locally with `toSearch()`; **the shared helper is still
  wrong for every other caller.**
- **No local PostgreSQL.** 589 tests skip here and first execute in CI. A skip
  is not a pass.
- **Zoho remains MOCK.** No sandbox credentials, no live call has been made,
  and nothing is marked LIVE or VERIFIED.

## Test and commit status

- Full local suite: **3589 passed, 589 skipped, 1 xfailed** (`824974d`)
- Contract and inventory gates: 61 passed · manifest `--check` clean
- Authorisation matrix: 290 passed
- `integration.spec.js`: **161 passed against the real shipped wiring**, no
  self-installed routes
- `spa-routing.spec.js`: 55 passed at `824974d`; re-running at 46 screens
- Migrations: **001…021, contiguous, no gaps**
- CI run 34266699000 at `824974d`: Contract, Supply chain, PostgreSQL and
  Regression **green**; Visual regression still running
- Wave 7 is **not closed**.

## Corrections of record

Claims previously in this file that were false, withdrawn rather than quietly
edited:

- *"Phase 1 — PostgreSQL port, behaviour-identical | complete"* — the port
  omitted the entire procurement document chain. Migration 013 closed that.
- *"Waves 1-3 COMPLETE … Wave 4 in progress"* — stale by two waves.
- *"19 of C8's 40 screens still unbuilt — Wave 7 delivers 21 of them"* — the
  screens were built. Thirty-three of them were unreachable, which this file
  reported as delivery.

## Flake classification of record — SCR-39, 2026-09-08

CI run 34203327903 failed one VRT test, `SCR-39 is gated on connector.read even
though it offers a write`, at laptop-1024 only, on a 15 s selector timeout.
**Classified as resource contention on evidence:** the failing run took 54
minutes for a spec that takes ~17 with five agents competing for the machine;
the same test passed at desktop-1440 and tablet-800 in that run; and it passed
3/3 sequentially on an idle machine in 37.0 s, 23.2 s and 25.5 s. Nothing was
changed to obtain that — no timeout raised, no assertion weakened, no baseline
touched. If it recurs on an idle machine this classification is wrong rather
than merely re-asserted.
