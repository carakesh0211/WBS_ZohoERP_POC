# capex_audit_anchor — Catalyst Cron Function

The scheduled caller of the daily audit anchor. `main.py` is a shim over
`app.backend.jobs.anchor_entry.run_from_environment`, the same function
`tools/anchor_job.py` calls, so the CLI and the schedule cannot diverge.

## Assembling the deployable folder (no network)

    python tools/appsail/build_anchor_function.py \
        --wheels <staging>/wheels --out <outside-repo>/capex_audit_anchor

The wheel set is the one `tools/appsail/build_uat_bundle.py` documents
(`pip download --platform manylinux2014_x86_64 --python-version 313
--implementation cp --only-binary=:all: -r requirements.txt`). The builder
copies `main.py`, `catalyst-config.json`, `app/`, `migrations/`, the two
`research/20_verified` inventories and `research/30_contracts`, extracts the
wheels into `vendor/`, and refuses Windows `.pyd`/`.dll` files, stray
databases, `.claude/`, `tests/`, and secret-shaped text — the same gate as the
AppSail bundle.

## Deploying (a cloud action; needs its own authorisation)

From a directory holding `.catalystrc` bound to the UAT project:

    catalyst deploy --only functions:capex_audit_anchor

Then Console → Job Scheduling → Cron: name `capex-audit-anchor-daily`, daily
02:15 UTC, target this function, timeout 900 s, retry on failure ONLY when the
logged outcome is PARTIAL or FAILED_RETRYABLE (see the runbook). Environment:
`CAPEX_DB_*` exactly as AppSail carries them, plus `CAPEX_ANCHOR_JOB_TOKEN`
(secret). Nothing in this folder carries a value.
