# Fable 5.1 — continuation record (written 2026-09-11, usage limit approaching)

Everything below is on branch `fable-5.1/full-app-hardening-uat`, pushed to
the private origin at `6c521ba` or later. Resume from HEAD; nothing valid lives
only in a working tree except what this file names.

## Agents that were still running when this was written

Their commits live on their own branches inside this repository. Cherry-pick
onto the Fable branch after review (`git log --oneline <base>..<branch>`);
regenerate the manifest (`python tools/build_test_manifest.py`) after every
cherry-pick that touches `tools/build_test_manifest.py`, and re-register any
file the conflict resolution drops (see commit `4cbe3eb` for the failure mode).

| Stream | Branch | Base | Owns |
|---|---|---|---|
| Reporting filters/grouping/exports (Sonnet) | `work/reporting-filters` | `cc9c6b1` | `app/backend/pg/reporting.py`, `pg/exports.py`, `api/reports.py`, `api/exports.py`, `app/frontend/src/features/analytics/*`, `tests/test_pg_reporting_fable51.py`, `tests/test_reports_filters_fable51.py` |
| Playwright + axe spec for Budget Setup / Categories (Sonnet) | `work/vrt-budget-setup` | `110e4cb` | `tests/vrt/budget-setup.spec.js` + its snapshots only |

If either branch has no commits, the task must be re-dispatched (the harness
has twice provisioned a worktree on the stale POC commit `ce7f3c5`; every brief
now carries a base-commit guard).

## Background jobs that may still have been running

* Full local (non-PostgreSQL) suite at `c51c2fe`: 2 failed / 4432 passed /
  723 skipped in 18 min; both failures fixed in `bafbae4` (a wall-clock second
  boundary in the throttle-parity test; a stale expectation after `vendor_ids`
  became a conditional refusal).
* UAT bundle rebuilt from `c51c2fe`, smoke-tested locally and REDEPLOYED to
  `wbs-capex-uat` on 2026-09-11 (record in STAGE_A_UAT_PREVIEW.md).

## Local machine state a resumed session relies on

* PostgreSQL 16.10 at `127.0.0.1:5432` (`capex`/`capex`), binaries
  `%USERPROFILE%\.capex-tools\pgsql`, data `%USERPROFILE%\.capex-tools\pgdata16`.
  Start: `pg_ctl -D pgdata16 -l pg16.log -o "-p 5432 -c listen_addresses=127.0.0.1" start`.
* Disposable demo database `capex_tmpl_demo_fable51` (rebuild with
  `python tools/demo_pg.py --create --fresh`; serve with `--serve --port 8790`).
* UAT credentials (hashes shipped; plaintext for the coordinator) under
  `%USERPROFILE%\.capex-tools\uat-credentials\`; wheels under
  `%USERPROFILE%\.capex-tools\appsail-staging\wheels`; bundle output under
  `%USERPROFILE%\.capex-tools\appsail-out\`; CLI project dir bound to the new
  Catalyst project under `%USERPROFILE%\.capex-tools\appsail-deploy\project`.

## Done and verified (see the status file and docs/fable51/*.md)

* Stage A preview deployed and verified; `docs/fable51/STAGE_A_UAT_PREVIEW.md`.
* Migration 026 + 027 applied; original-budget workflow proven end-to-end on
  PostgreSQL over HTTP (create → submit → maker refused → approve → RELEASED →
  grid → audit → revision); 15 live workflow tests; seeded approval predicates
  corrected (they had never compiled); RLS coverage 57 passed; auth matrix
  209 passed; security suites 435 passed; inbound/FX/budget/writeback/
  reconciliation suites 142 passed live.
* Frontend Budget Setup, governed selectors, Budget Categories screen,
  navigation with two collapsible groups (approved groups' markup unchanged),
  measured no rail overflow at 1440×900.

## 2026-09-13: the controlled emission done, and outbound writes left ENABLED by decision

PO-00009 (`3912780000000125001`) was created in DEMO WBS from WBS-UAT-OUTBOUND-20260912 through the
app's own emit + drain, read back and proven unique (`evidence/e2e/OUTBOUND_2026-09-13.md`). The product
owner then decided outbound purchase-order creation STAYS enabled for ongoing UAT: the gate is `1`,
`CONN-32A904F37FEA` is LIVE_WRITE, and the permanent boundaries are enforced in code (DECISIONS file,
last section) and deployed at `a65841f` (records 12–13). Every emission from now on is a real record in
the demo tenant. Two things the day cost: the launcher stripped the gate in every stage (fixed), and a
CREATE-scoped refresh token that did not carry CREATE (token health now names the gap).

## Decisions of 2026-09-12 (evening): public repository, bill sync deployed, outbound path built

The repository is PUBLIC. The full history was scanned before any further
commit (`evidence/release/PUBLIC_HISTORY_SCAN_2026-09-12.md`): nothing
credible, nothing rotated. GitHub Actions runs on every push now; the
route-listing test reads the OpenAPI schema (FastAPI 0.141 in CI) and the
VRT inventory records `po-currency.spec.js`. UAT runs `2852be5` (deploy
records 10–11): bill sync works — WBS-UAT-E2E-20260912 is ordered =
received = billed = ₹18,50,000, open 0. The application-originated
emission path exists and is tested but **has not written to the tenant
yet**: it waits at the manual checkpoint (the owner installs a
CREATE-scoped credential and sets `CAPEX_ERP_OUTBOUND_WRITES=1` in the
Catalyst console), then `tools/uat/e2e_outbound.py raise|emit|close|refused`
runs the proof. The gate stays shut and the connection LIVE_READ until then.

## Sprint of 2026-09-12: release candidate and the demo cycle

UAT runs `f19907b` (bundle `37eee12a…`, schema 032; deploy records 8–9). Local
gates at HEAD `532bb9d`: non-PG suite 4696 passed / 1 failed (fixed), the
database-backed files 2142 passed / 28 failed (every one fixed since: 8 by
the lead, 20 by the closure stream, re-run green), 21 bounded VRT batches
green, bundle scan clean, all-roles smoke 13/13 sign in (one by-design 503 on
control totals). CI: **pending GitHub billing**, not green, not claimed.

The demo cycle (`docs/fable51/evidence/e2e/`): PR-0001 raised, over-budget
probe refused, availability checked, approved by a second role, converted to
WBS-UAT-E2E-20260912 (`PO-15C1EEC1E554`); emission refused twice by the shut
gate (the credential is read-only — write scope is the owner's manual step);
tenant PO-00008 / GRN / bill created under the owner's authorisation; adoption
LINKED PO-00008 to the local order, adopted five demo orders, refused two
(unmapped head); the receive walk mirrored the GRN (received = ordered
₹18,50,000); reconciliation identity balanced; the ELEC cell shows the adopted
exposure (EXCEEDS_BUDGET, by design); audit verify reports NEVER_ANCHORED
honestly (the anchor Cron Function is not deployed). **Bill sync is the open
blocker**: fixed at `532bb9d`, not deployed (third deploy not permitted).
Cleanup of the stray schema: STOPPED at the zero-rows condition.

## Deploys 6 and 7, and the migration incident (2026-09-12)

See `STAGE_B_PERSISTENT_UAT.md` deploy records 6–7 and "Incident 2026-09-12":
UAT is at `39f30e6` / schema 031; an empty 30-migration schema sits in the
project's `postgres` database awaiting the owner's approval to drop; `db.env`
now names `capex_tmpl_uat`. Rule added: check `migrate_pg --status` shows the
expected current version before `--upgrade`.

## VRT note 2026-09-12

In a 186-test run of `spa-routing.spec.js` + `po-currency.spec.js` on the merged
branch, one test failed: axe-core SCR-30 at laptop-1024. Rerun alone it passed
(35 s). Treated as a one-off under load; nothing re-baselined. If it recurs,
read `test-results/` before touching a baseline.

## Decisions of 2026-09-11

See `docs/fable51/DECISIONS_2026-09-11.md` (eight decisions, holders of each open item).

## Not done — in order of value

1. Integrate the reporting stream (filters/grouping/reconciliation/exports).
2. ~~Integrate the Playwright spec~~ ~~bounded VRT~~ — done 2026-09-11: 45 batches,
   11 failed, all answered (`3ca2db3`); 63 baselines re-recorded with the delta
   proof (`1ebf136`, A4 "The account"). A confirmation run of approvals,
   approved-ui, spa-routing, integration and fx-rates on the new baselines
   writes `.vrt-batches-confirm/summary.txt`; if any batch is not `ok` there,
   read its log before touching a baseline.
3. ~~Rebuild and redeploy the Stage A bundle~~ — done 2026-09-11 from `c51c2fe`.
4. ~~FX-rate maintenance API + admin UI~~ — integrated (migration 028,
   `pg/fx_admin.py`, `api/fx_admin.py`, Exchange Rates screen; 16 live + 43
   guard tests). ~~Currency-aware PO emission and DTO exactness~~ — done
   (migration 029, `test_pg_po_currency_fable51.py`; see the delivery status
   section "a purchase order in the currency the vendor quoted"; the PO
   screen's currency selector landed 2026-09-12 as the Raise Purchase Order
   screen, `a6e940a`…`413600d`: route `purchase-order` reached from the
   Commitments header for `po.amend` holders, INR default sends today's body,
   a foreign currency sends `currency`/`document_date`/`exchange_rate`/
   `rate_source`/`fx_rate_id` and per-line `source_amount_minor`, the rate
   lookup's refusal shown verbatim; `tests/vrt/po-currency.spec.js` 21 passed
   at the three viewports). ~~Audit-anchor Cron
   Function shim + recreated-stream detection~~ — done. ~~023 live RLS
   matrix~~ — done (6 live tests).
5. ~~Stage B launcher variant and provider decision~~ — LIVE 2026-09-12 on
   Supabase Free (Mumbai); deploy record and the standalone-deploy rule in
   `docs/fable51/STAGE_B_PERSISTENT_UAT.md`. Open from that table: readiness
   under a paused instance, persistence across a restart, the RLS matrices
   against the UAT instance, the anchor Cron Function, the restore drill.
6. ~~ERP demo OAuth~~ — CONNECTED read-only 2026-09-11 (org 60074128927 DEMO WBS);
   credential at `%USERPROFILE%\.capex-tools\erp-demo\` (never in the repo);
   `python tools/erp_demo/connect.py --check` proves the refresh, `python
   tools/erp_demo/probe.py --org 60074128927` re-runs the read-only probe.
   The org now holds 4 vendors, 8 items, 5 POs, 2 receives, 4 bills (loaded via
   the Zoho MCP server, all SYNTHETIC) plus a JPY vendor and PO-00006 in JPY
   carrying cf_capex_ref (unique field created); the probe passes on data and
   resolve_by_dedupe_key is proven live. The org now also holds a GST vendor,
   item, PO-00007 and bill (owner enabled GST 2026-09-11) and the two PO line
   fields `cf_wbs_code` / `cf_budget_head` (D-7 TRUE for this tenant).
   **LIVE_READ routes done 2026-09-12** (`fc8c07e`, `314ba9f`): connection
   `CONN-32A904F37FEA` (entity ENT-DM1, org 60074128927, mode LIVE_READ) on
   the UAT instance answers `/organizations` (pinned org only, 21 others
   COUNTED never named), `/validate` (4 modules PASS, receives NOT AVAILABLE,
   custom modules NOT RUN), `/scopes` (12 granted) and `/health.token`
   (MINTED) from the tenant; evidence
   `docs/fable51/evidence/erp-demo/live-routes-uat-2026-09-12.txt`.
   **Live sweeps done 2026-09-12**: the sweep stream's five commits are on the
   branch (`8d25bc0`…`4c41ce4` from `work/live-sweep`, cherry-picked with
   `-x`; the last lands as `2f55f78`), the route ran
   three rounds on UAT (evidence `live-sweep-uat-2026-09-12.txt`), and the
   two findings it produced are in `STAGE_B_PERSISTENT_UAT.md` "Findings from
   the first live sweeps": the receive walk anchors on app-raised orders
   (decision needed) and the CAPEX-reference read (fixed `6a3987c`, live proof
   pending an owner action). Next: stamping `cf_wbs_code`/`cf_budget_head` on
   the seven demo orders.
   `Capabilities.line_level_custom_fields` is TRUE for org 60074128927 since
   `d3d8054` (`erp.VERIFIED_LINE_CUSTOM_FIELDS`). The stamping itself is
   BLOCKED: the Zoho ERP MCP `update_purchase_order` call was refused twice by
   the Claude Code auto-mode classifier; the exact per-line payloads are in
   `docs/fable51/evidence/erp-demo/line-field-stamping-plan.md` for the owner
   to allow or apply. The five Budget Setup / Categories defects are on the
   branch as `8adfa12`.
7. GitHub Actions: blocked by account billing (confirmed from the check-run
   annotation); do not consume runs until the owner clears it.
8. Independent adversarial review of the whole branch (D1/D2 reviewers never
   completed — both died at the limit before writing findings).

## Review findings 2026-09-12

A later independent review of `c51c2fe..6a3987c` confirmed four defects (one
P1, three P2), distinct from item 8's incomplete D1/D2 run above. All four
are fixed, one commit per item:

1. **P1 — the PO-anchored receive walk under-charged the polling budget.**
   `SweepPoAnchored` charged the budget 1 call per open PO before calling
   `adapter.receives_for_po(...)`, but that call actually spends
   `1 + len(receives)` requests. `ErpAdapter` gained the split
   `po_receive_refs()` / `get_receive()` pair (additive; `receives_for_po`
   unchanged for existing callers), and the sweep now charges 1 for the PO
   detail and 1 more per receive, each immediately before the GET it pays
   for. Books/Inventory (`receives_listable=True`, a different cost model)
   is unaffected. Commit `40aad73`.
2. **P2 — the live transport was rebuilt per HTTP request**, so its
   100/minute sliding window and its minted-token cache never spanned more
   than one call. `live_transport.py` gained `shared_transport()`, a
   process-wide cache keyed on the resolved credential path and guarded by a
   lock, plus `reset()` for tests; both call sites
   (`api/integrations.py::_live_transport_for`,
   `live_sweep.py::adapter_for_connection`) now ask it instead of
   constructing directly. Commit `4a018df`.
3. **P2 — TOCTOU on job-row creation.** `live_sweep.py::_job_row_for` did a
   plain SELECT-then-INSERT with no lock, so two concurrent sweep ticks on
   one connection could each enqueue a live job row for the same
   `(kind, connection_id)` and race on the shared watermark and checkpoint
   afterwards. `_job_row_for` now takes `pg_advisory_xact_lock` before its
   SELECT (the same primitive `budget.py` already uses for the per-project
   first-revision race), and migration
   `031_job_one_live_row_per_connection.sql` adds a partial UNIQUE index on
   `job (kind, connection_id)` over the non-terminal states as a
   database-level backstop. (Migration number 031, not 030 — 030 was taken
   by the decision-8 stream's `030_foreign_currency_basis_missing.sql`
   landing on the branch concurrently.) Commit `30e535a`.
4. **P2 — Stage B's `verify-full` was a default, not enforced.**
   `tools/appsail/uat_main.py` used
   `os.environ.setdefault("CAPEX_DB_SSLMODE", "verify-full")`, so a weaker
   console value (`require`, `prefer`, `disable`) passed straight through.
   The launcher now refuses to start (naming the variable) unless
   `CAPEX_DB_SSLMODE` is unset or already `verify-full`. Commit `5745e54`.

Each fix carries a test proven to fail against the pre-fix code and pass
against the fix (items 3 and 4 verified by temporarily reverting the change
and re-running; item 1's pinned test was itself the failing assertion). See
`tests/ADAPTATIONS.md`'s 2026-09-12 entry for item 1's assertion-value
change.
