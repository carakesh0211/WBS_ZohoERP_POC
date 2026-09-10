# Stage B — persistent UAT on a managed PostgreSQL (Fable 5.1)

**Status: PREPARED, NOT PROVISIONED.** No managed PostgreSQL has been created.
Every managed PostgreSQL offering in the India region that this engagement can
use (Supabase Pro, Neon Scale, AWS RDS ap-south-1, Azure Database for
PostgreSQL Central India, DigitalOcean BLR1) is a paid resource, and the brief
does not authorise creating one. Catalyst offers no managed PostgreSQL; its
Data Store was rejected on 2026-08-28 because it documents no transactions,
row locks, foreign keys, CHECK constraints, triggers or partial indexes — the
properties every audited control here rests on.

**The stop is for one approval only:** which provider, and the go-ahead to
incur its charge. Everything below is ready to execute the moment that lands,
and none of it needs a credential typed into chat, a file or a commit.

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
   the CLI reference for standalone deploys — the exact invocation is in
   `tools/appsail/README.md` once the provider is known.

## What runs, in order, once the instance exists

1. **Migrate explicitly** from the build machine (never from the app):
   `python -m app.backend.pg.migrate_pg --upgrade` with the same `CAPEX_DB_*`
   variables — through migration 027. `--status` must report 001…027 applied.
2. **Seed the synthetic estate**: `CAPEX_PROFILE=local-demo python -m
   app.backend.pg.seed` is refused unless the database name matches
   `^capex_tmpl_` — name the UAT database `capex_tmpl_uat` so the guard admits
   it and nothing else ever will.
3. **Build the Stage B bundle** with `tools/appsail/build_uat_bundle.py
   --stage b --ca-bundle <provider CA>` (the `--stage b` flag switches the
   launcher to `CAPEX_PROFILE=uat` with no SQLite copy and no ERP writes; it is
   the next change on this branch and is gated by the same tests).
4. **Deploy**, then prove each of the following on the live URL and record
   the evidence in this file:

| Proof required by the brief | How it is proven |
|---|---|
| AppSail → PostgreSQL TLS connectivity | `/readyz` 200 with `schema_version: 027`; `CAPEX_DB_SSLMODE=verify-full` is the only mode configured |
| Explicit migrations through the current version | `migrate_pg --status` output captured before deploy; `/readyz` reports the same version |
| Readiness fails honestly when PostgreSQL is unavailable | pause the instance (provider console) → `/readyz` 503 with an error CLASS only, `/healthz` still 200 |
| Startup does not self-migrate | `tests/test_runtime_startup.py::test_run_py_contains_no_migration_execution_call` (AST) plus a deploy with the schema one version behind → the process refuses to start and the log names the command |
| Persistence survives an AppSail restart | create a draft original budget, force a redeploy, read it back |
| RLS and scope under the application role | `tests/test_pg_rls_wave7_matrix.py` and `tests/test_pg_rls_fable51_023_matrix.py` executed with `CAPEX_DB_URL` pointing at the UAT instance (they create disposable `capex_t*` databases beside it) |
| Audit anchors through the scheduled-job entry point | Catalyst Job Scheduling cron `capex-audit-anchor-daily` → Cron Function `capexAuditAnchor` (`catalyst/functions/capex_audit_anchor/`, built by `tools/appsail/build_anchor_function.py`) → `audit_anchor` row per day; `/api/audit/anchors/verify` reports `anchored: true` |
| Backup and restore verification | provider snapshot → restore into `capex_tmpl_restore_<date>` → `python tools/restore_drill.py` compares row counts, ledger totals to the paisa and `verify_chain` on every stream |
| Ledger and audit reconciliation after restore | the restore drill's reconciliation report, committed under `docs/fable51/evidence/` with amounts and hashes only |

Nothing above is claimed as done. The instance does not exist.
