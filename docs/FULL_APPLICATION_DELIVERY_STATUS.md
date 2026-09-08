# Full application — delivery status

**Branch:** `full-application/build` · **Wave 6 in release gate** · last commit
`e326648` · full local suite **3246 passed, 525 skipped, 1 xfailed**

## Current milestone

**M6 — Zoho adapters, GRN and Vendor Bill sync.** Schema, services, inbound
ledger, reconciliation and the corrective migration are merged. The gate is
VRT + push + five green CI jobs.

## Completed vertical slices

| Wave | Delivered |
|---|---|
| 1–4 | PostgreSQL foundation, settings/masters, budget control cells and periods, identity/scope/RLS, configurable approval engine |
| 5 | Integration platform: schema, inbox/outbox, chunked jobs, rate budget, circuit breaker, outbound PO emission, `/api/integrations/*` mounted with six coded refusals, unattributed-exception triage (view / attribute / resolve) |
| 6 | **Migration 013** — the eight procurement tables PostgreSQL never had · PR→PO services · GRN and Vendor Bill inbound · PostgreSQL reconciliation · **migration 014**, twelve corrections |

## Current slice

**Migration 015 — PR reservation grain.** Approved 2026-09-08: one live
reservation per **(PR × resolved budget control cell)**, where the resolved
cell is the budget-owning WBS ancestor plus `budget_head_id`. 014's
`UNIQUE (pr_id)` was correct for a header-only PR and wrong for the
line-grained PR Wave 6 introduced; multi-cell `reserve=True` currently refuses
with a coded error rather than guessing.

## Next three deliverables

1. Push Wave 6; five CI jobs green; confirm the live PostgreSQL job applied
   014 and executed its 27 new tests including the over-commitment regression.
2. Migration 015 and the multi-cell reservation service.
3. Wave 7 — reporting backend, exports, dashboards, closure screens
   (migrations 016/017/018).

## Genuine blockers

**None blocking the build.** Open items, each recorded rather than assumed
closed:

- **Repair batch B, not yet written.** From the adversarial review:
  `rate = amount // units` truncates and the adapters emit *rate × quantity*
  rather than the line total, so a 3-unit ₹1,000.00 line reaches the vendor as
  ₹999.99 and "reconcile to the paisa" fails arithmetically (H-3);
  `currency`/`exchange_rate` are stored and never applied, and the API types
  the rate as `int` (H-4); `recompute_commitment` updates zero rows when a
  ledger cell is absent (H-6) and, being a full re-derive, **zeroes any
  commitment not originating in PostgreSQL `po_line`** — ₹3,99,000 vanishing
  on the shipped seed (H-7).
- **Repair batch C, not yet written.** Three guards do not cover the code they
  were written for (H-9), plus M-1 float quantity, M-4 scope waived on NULL,
  M-5 dead `tax_paise`.
- **19 of C8's 40 screens still unbuilt** — Wave 7 delivers 21 of them.
- **No local PostgreSQL.** ~525 tests skip here and first execute in CI. A
  skip is not a pass, and no live claim is made from a local run.
- **Zoho remains MOCK.** No sandbox credentials, no live call has been made,
  and nothing is marked LIVE or VERIFIED.

## Test and commit status

- Full local suite: **3246 passed, 525 skipped, 1 xfailed** (`e326648`)
- Manifest `--check`: clean
- Last full VRT: **1004 passed, 4 skipped**, exit 0 — re-running at `e326648`
- Last green CI: Wave 5 (all five jobs). **Wave 6 not yet pushed.**

## Corrections of record

Two claims previously in this file were false and are withdrawn:

- *"Phase 1 — PostgreSQL port, behaviour-identical | complete"* — the port
  omitted the entire procurement document chain. Migration 013 closed that.
- *"Waves 1-3 COMPLETE … Wave 4 in progress"* — stale by two waves.
