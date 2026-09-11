# Stage B — persistent UAT on a managed PostgreSQL (Fable 5.1)

**Status: LIVE since 2026-09-12** on the product owner's decision of
2026-09-11 (decision 1 in `DECISIONS_2026-09-11.md`): **Supabase Free tier,
Mumbai (ap-south-1), a NEW project** — no paid resource, the pre-existing
Supabase project untouched. The deploy record is at the end of this file.
The text below it is the plan as written before the instance existed; where
the plan and the record differ, the record is what happened.

Catalyst offers no managed PostgreSQL; its Data Store was rejected on
2026-08-28 because it documents no transactions, row locks, foreign keys,
CHECK constraints, triggers or partial indexes — the properties every audited
control here rests on. Every India-region managed PostgreSQL other than the
free tier chosen is a paid resource the brief does not authorise.

Free-tier limits the owner accepted: 500 MB, idle pause after 7 days of no
traffic, no automated backups (the restore drill is `pg_dump` based).

## What the product owner must decide and do

1. Choose the provider (recommendation: a managed PostgreSQL **16** in
   **India** with TLS enforced and daily automated backups — Supabase Pro in
   `ap-south-1` is the cheapest that meets all three, and the Phase 0B probe
   already proved AppSail can reach Supabase over TLS with the project's own CA
   bundle at the archive root).
2. Create a **new** project/instance. Do not reuse any existing Supabase project
   or database.
3. Create the application role as the migration expects: the runner connects
   as an owner role to apply DDL; the application connects as the NOLOGIN
   role `capex_app` **through** a login role that is granted it — see
   `migrations/pg/004_identity_scope.sql`'s header for the exact statements
   (they are in the repository, not restated here, so they cannot drift).
4. Put the four values into **Catalyst AppSail environment configuration** for
   `wbs-capex-uat` (Console → AppSail → wbs-capex-uat → Configuration →
   Environment variables), never into chat:

   | Variable | Value |
   |---|---|
   | `CAPEX_DB_HOST` | the instance host |
   | `CAPEX_DB_PORT` | `5432` (session pooler port if the provider requires one) |
   | `CAPEX_DB_NAME` | the database name |
   | `CAPEX_DB_USER` | the login role that is granted `capex_app` |
   | `CAPEX_DB_PASSWORD` | the password (**secret**) |
   | `CAPEX_DB_SSLMODE` | `verify-full` (the default; do not relax it) |
   | `CAPEX_DB_SSLROOTCERT` | `ca-bundle.pem` — the provider's CA, placed at the bundle root by the build step |
   | `CAPEX_ANCHOR_JOB_TOKEN` | the audit-anchor job credential (**secret**; only the Cron Function reads it) |

   Because the AppSail is **linked** in `catalyst.json`, a CLI redeploy applies
   `app-config.json`'s `env_variables` and would wipe Console-set values. The
   Stage B build therefore writes these names into `app-config.json` at build
   time **from the environment of the build machine**, with the bundle gate
   refusing any value that looks like a secret in the archive. The secret
   values themselves are set once in the Console after the first deploy and
   the deploy command for Stage B is `catalyst deploy appsail --name
   wbs-capex-uat --build-path <abs>` with `--except` on env, as documented in
   the CLI reference for standalone deploys — the exact invocation will be recorded here once the provider is known. Use the STANDALONE deploy form for Stage B (`catalyst deploy appsail --name wbs-capex-uat --build-path <absolute path> --stack python_3_13 --command "python3 -u main.py"`), which does not apply `app-config.json` env_variables and so cannot wipe Console-set secrets.

## What runs, in order, once the instance exists

1. **Migrate explicitly** from the build machine (never from the app):
   `python -m app.backend.pg.migrate_pg --upgrade` with the same `CAPEX_DB_*`
   variables — through migration 029. `--status` must report 001…029 applied.
2. **Seed the synthetic estate**: `CAPEX_PROFILE=local-demo python -m
   app.backend.pg.seed` is refused unless the database name matches
   `^capex_tmpl_` — name the UAT database `capex_tmpl_uat` so the guard admits
   it and nothing else ever will.
3. **Build the Stage B bundle.** `tools/appsail/build_uat_bundle.py --ca-bundle <provider CA .pem>` places the provider's public root certificate at the archive root; `uat_main.py` keeps the `CAPEX_DB_*` variables only when it runs under Catalyst (`X_ZOHO_CATALYST_LISTEN_PORT`) with a database host, points `CAPEX_DB_SSLROOTCERT` at that certificate, refuses to start under `verify-full` without it, and unsets `CAPEX_ERP_OUTBOUND_WRITES` in every stage. Without a database host the same archive is Stage A. (Written 2026-09-11, commit `6d1eb79`.)
4. **Deploy**, then prove each of the following on the live URL and record
   the evidence in this file:

| Proof required by the brief | How it is proven |
|---|---|
| AppSail → PostgreSQL TLS connectivity | **PROVEN 2026-09-12**: `/readyz` 200 with `schema_version: 029` on the live URL; `CAPEX_DB_SSLMODE=verify-full` with the Supabase root CA at the bundle root is the only mode configured (`docs/fable51/evidence/erp-demo/live-routes-uat-2026-09-12.txt`) |
| Explicit migrations through the current version | **PROVEN 2026-09-12**: 001…029 applied from the build machine before the first deploy; `/readyz` reports 029 |
| Readiness fails honestly when PostgreSQL is unavailable | pause the instance (provider console) → `/readyz` 503 with an error CLASS only, `/healthz` still 200 |
| Startup does not self-migrate | `tests/test_runtime_startup.py::test_run_py_contains_no_migration_execution_call` (AST) plus a deploy with the schema one version behind → the process refuses to start and the log names the command |
| Persistence survives an AppSail restart | create a draft original budget, force a redeploy, read it back |
| RLS and scope under the application role | `tests/test_pg_rls_wave7_matrix.py` and `tests/test_pg_rls_fable51_023_matrix.py` executed with `CAPEX_DB_URL` pointing at the UAT instance (they create disposable `capex_t*` databases beside it) |
| Audit anchors through the scheduled-job entry point | Catalyst Job Scheduling cron `capex-audit-anchor-daily` → a Cron Function that calls `app.backend.jobs.audit_anchor.run_anchor_job` (the function directory and its build script are NOT yet written — listed under remaining work; today the only caller is `tools/anchor_job.py`) → `audit_anchor` row per day; `/api/audit/anchors/verify` reports `anchored: true` |
| Backup and restore verification | provider snapshot → restore into `capex_tmpl_restore_<date>` → `python tools/restore_drill.py` compares row counts, ledger totals to the paisa and `verify_chain` on every stream |
| Ledger and audit reconciliation after restore | the restore drill's reconciliation report, committed under `docs/fable51/evidence/` with amounts and hashes only |

The two rows marked PROVEN are done; the remaining rows are still open and
are on the engineering list in `CONTINUATION.md`.

## Deploy record

| # | Date | Bundle (`git_head`, `archive_sha256`) | Deploy form | Outcome |
|---|---|---|---|---|
| 1 | 2026-09-11 | `6d1eb79`, `69c02eb4…` | linked: `catalyst deploy --only appsail:wbs-capex-uat -ni` from the deploy project (`catalyst.json` source `..\bundle`) | first Stage B instance; the eleven environment variables (`CAPEX_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE`, `CAPEX_ERP_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN/API_DOMAIN/SCOPES`) were entered by the owner in the Catalyst console AFTER this deploy; `/readyz` 200 `029`; U-RAKESH login 200; every PostgreSQL-backed screen answered |
| 2 | 2026-09-12 | `314ba9f`, `af5431ed…` | **standalone**: `catalyst deploy appsail --name wbs-capex-uat --build-path <abs bundle dir> --stack python_3_13 --command "python3 -u main.py"` from the deploy project directory | the console-set variables SURVIVED (proof: `/readyz` 029 and a token minted from the platform-held refresh token in the same run); `/organizations`, `/validate`, `/scopes`, `/health` answered live from DEMO WBS (evidence file above) |

Rule from record 2: **redeploy Stage B with the standalone form only.** The
linked form applies the archive's `app-config.json` (`env_variables: {}`) and
would wipe every console-set value.

Where things are on the build machine (nothing of this is in the repository):
the Supabase connection values and password in
`%USERPROFILE%\.capex-tools\stage-b\db.env`, the provider CA beside it as
`ca-bundle.pem` (Supabase Root 2021 CA), bundles and manifests in
`%USERPROFILE%\.capex-tools\appsail-out\`, the deploy project in
`%USERPROFILE%\.capex-tools\appsail-deploy\project` with the archive
extracted to `..\bundle`.

Instance facts: Supabase project `wbs-capex-uat` (ref `lmljdkluuqpgjboiejro`),
PostgreSQL 17.6, database `capex_tmpl_uat`, reached through the session pooler
on port 5432 (the direct host is IPv6-only, which Catalyst cannot reach), user
`postgres.<ref>`, `sslmode=verify-full`. The SQLite shell (identities,
sessions) still resets on every restart — decision 7.
