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

* Full local (non-PostgreSQL) suite: `python -m pytest -q --ignore=tests/vrt`
  — result not yet recorded; re-run to get the authoritative number.
* UAT bundle rebuild from HEAD + local smoke (`tools/appsail/build_uat_bundle.py`)
  — re-run; the deployed Stage A artifact is still the `8f39bb7` build.

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

## Not done — in order of value

1. Integrate the reporting stream (filters/grouping/reconciliation/exports).
2. Integrate the Playwright spec; then run the bounded VRT
   (`tools/run_vrt_batches.sh`, sequential, one Playwright process) — the rail
   gained rows, so every screen's baseline moves by the rail region only;
   inspect each diff image, then re-capture deliberately with a
   before/after record under `docs/ui-change-2026-09/`-style evidence.
3. Rebuild and redeploy the Stage A bundle (adds the login throttle, the ERP
   write gate, the new screens) — same project/service only.
4. FX-rate maintenance API + admin UI (never started: the first agent died at
   the limit before committing). Currency-aware PO emission and DTO
   exactness (same). Audit-anchor Cron Function shim + deleted-and-recreated
   stream detection (same). The 023 live RLS matrix (`test_pg_rls_fable51_023_matrix.py`)
   was never delivered by the security agent either.
5. Stage B launcher variant and provider decision (billing stop);
   `docs/fable51/STAGE_B_PERSISTENT_UAT.md`.
6. ERP demo OAuth: stopped at the manual authorisation;
   `docs/fable51/ERP_DEMO_CONNECTION_PREP.md`.
7. GitHub Actions: blocked by account billing (confirmed from the check-run
   annotation); do not consume runs until the owner clears it.
8. Independent adversarial review of the whole branch (D1/D2 reviewers never
   completed — both died at the limit before writing findings).
