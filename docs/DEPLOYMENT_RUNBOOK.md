# Deployment Runbook — CAPEX & WBS Control Hub

Wave 8, stream D. Local demo start-up, the deployment artifact, the complete
environment-variable inventory, and the rollback checklist.

Every command in §1 was **actually run** on Windows 11 / Python 3.14.0 at the
commit carrying this file, and the observed output is reproduced. Every variable
in §4 was derived from the code, not from memory.

---

## 1. Local demo start-up — run, not guessed

```bash
# 1. Build the database. This is a DEPLOY STEP, not something the app does.
python -m app.backend.migrate --db app/data/capex_demo.db --fresh --seed

# 2. Start. The identities are provisioned here, not by the migration.
CAPEX_PROFILE=local-demo CAPEX_DB_PATH=app/data/capex_demo.db PORT=8871 \
  python app/run.py
```

Observed:

```
building app/data/capex_demo.db
  applied 002 financial_controls
  [x] 001  initial_schema
  [x] 002  financial_controls
  -> demo identities ready: 9
CAPEX & WBS Control Hub -> http://localhost:8871
INFO:     Uvicorn running on http://127.0.0.1:8871
```

Open <http://127.0.0.1:8871> and sign in as any identity from
`docs/UAT_ROLE_BASED_PLAN.md` §2 — password is the user id followed by `!demo`.

### Two things that will catch you out

**The two commands are two commands.** `app/run.py` refuses to migrate itself
(DEF-01: an application that migrates on boot cannot be rolled back, races when
scaled, and turns a schema error into an outage). It only *looks* at the schema,
read-only, and exits non-zero with the exact command to run if it finds it
missing or behind:

```
$ CAPEX_DB_PATH=/tmp/does-not-exist.db python app/run.py
  REFUSING TO START: no database at /tmp/does-not-exist.db. Run:
  python -m app.backend.migrate --db /tmp/does-not-exist.db --fresh --seed
EXIT: 1
```

**`--fresh --seed` wipes the credentials; restarting restores them.**
`app_credential` and `user_role` are empty immediately after a fresh migration —
`provision_dev_identities()` runs at application start, not in the seed. Migrate
without restarting and every sign-in returns `401`. This bit during UAT
preparation; it is not theoretical.

### Health

| Endpoint | Without PostgreSQL | Meaning |
|---|---|---|
| `GET /api/health` | `200` `{"status":"ok", "zoho_mode":"MOCK", "profile":"local-demo"}` | the process is up |
| `GET /healthz` | `200` | liveness |
| `GET /readyz` | **`503`** `{"status":"unavailable","error_class":"DatabaseNotConfigured"}` | readiness, and it is honest |

`/readyz` returning 503 on a local demo is **correct, not a fault**. Use
`/api/health` as the container health check; using `/readyz` will restart-loop a
SQLite-only demo forever.

---

## 2. The deployment artifact

### What ships

| Item | Contents |
|---|---|
| Application | `app/` — FastAPI backend, static frontend, SQLite migrations |
| PostgreSQL schema | `migrations/pg/001..025_*.sql` — **25 files, all tracked**, applied by `python -m app.backend.pg.migrate_pg --upgrade` |
| Connector data | `research/20_verified/zoho_endpoint_inventory.json`, `openapi_findings.json` — **read at runtime**, so application data, not documentation |
| Dependencies | `requirements.txt` (`requirements.lock` for a pinned build) |
| Entrypoint | `python app/run.py` — binds `X_ZOHO_CATALYST_LISTEN_PORT` → `PORT` → `8000` |

### Defects in the artifact: one still open, one since closed

Both were verified when written. **D-2 has since been fixed and this entry is
kept only so the record shows what changed.** D-1 stands.

**D-1. The container cannot start on a fresh volume.** `Dockerfile` CMD is
`python app/run.py` against an empty `/data`; `render.yaml` `startCommand` is the
same against `/tmp/capex.db`. Neither migrates first, and the process refuses to
migrate itself. Both exit 1 on first boot.

(The second half of this entry — that `render.yaml` carried the comment *"the
demo dataset reseeds automatically on restart"* — **is fixed.** `render.yaml`
now says the opposite and records that the old line was never true. The line
number this runbook used to cite has moved with it; a comment is found by
reading the file, not by line number.)

The corrected start command, for both:

```bash
python -m app.backend.migrate --db "$CAPEX_DB_PATH" --fresh --seed && python app/run.py
```

(`--fresh` is correct for a **disposable demo volume only**. For anything with
data to keep, `--upgrade`, and see §5.)

**D-2. FIXED — do not re-apply this.** This entry used to say the image could
not run a PostgreSQL migration, because `Dockerfile` copied `app/` but not
`migrations/pg/`, and it prescribed adding a `COPY`. That `COPY` is present:

```dockerfile
COPY migrations ./migrations      # Dockerfile, with a comment recording the fix
```

The image therefore carries all three things a PostgreSQL migration needs: the
runner (`COPY app ./app`), the SQL (`COPY migrations ./migrations`), and the
driver (`requirements.txt` pins `psycopg[binary]>=3.2` and `psycopg_pool>=3.2`).
`python -m app.backend.pg.migrate_pg --upgrade` runs inside the image.

Two other release documents already recorded this as fixed while this one still
prescribed the change — and both send deployers HERE for the commands, so this
entry was the one an engineer would have acted on.

### Deployment shape, given the platform

Catalyst AppSail is a request/response tier: instances live 5 minutes of uptime,
are killed when idle, requests time out at 30 s, and there is no resident worker,
in-process scheduler or surviving in-process cache. Background work — the
integration sweeps and reconciliation — therefore belongs on Catalyst **Job
Scheduling** against Cron or Event Functions (15-minute ceiling, chunk and
checkpoint), not inside the web process. See `docs/RELEASE_PACKAGE.md` §4 for the
external dependency this creates.

---

## 3. Deployment procedure

1. **Freeze.** Record the commit SHA. Note the current
   `schema_migration` / PostgreSQL migration versions from the target
   (`SELECT version FROM schema_migration ORDER BY version`).
2. **Back up.** `pg_dump` the PostgreSQL database, or copy the SQLite file.
   §5 depends on this existing; there is no down-migration.
3. **Install dependencies.** `pip install -r requirements.txt` (or
   `requirements.lock`).
4. **Migrate, as its own step, with the application stopped.**
   * SQLite: `python -m app.backend.migrate --db <path> --upgrade`
   * PostgreSQL: `python -m app.backend.pg.migrate_pg --upgrade`
5. **Set the environment** per §4. `CAPEX_DB_SSLMODE` defaults to `verify-full`;
   leave it there and supply `CAPEX_DB_SSLROOTCERT`.
6. **Start.** `python app/run.py`. It verifies the schema read-only and refuses
   to serve if anything is behind, missing or drifted — a non-zero exit here is
   the gate working, not a flaky boot.
7. **Verify.**
   * `GET /api/health` → `200`, and `zoho_mode` reads `MOCK`.
   * `GET /readyz` → `200` **only if** PostgreSQL is configured; `503`
     `DatabaseNotConfigured` otherwise, which is honest and expected on a
     SQLite-only deployment.
   * `python tests/uat/uat_role_matrix.py --base <url>` → exit `0`.
8. **Record** the commit SHA, the migration versions now applied, and the
   `/api/health` payload.

---

## 4. Environment-variable inventory

Derived from the code at this commit. "Secret" means it must come from a secret
store and must never be committed, logged, or written into evidence.

### Read at runtime by the application

| Variable | Default | Controls | Secret | Read at |
|---|---|---|---|---|
| `CAPEX_DB_PATH` | `<repo>/app/data/capex.db` | SQLite file location. Evaluated at **import**, so changing it after start-up does nothing | no | `app/backend/db.py:11-14` |
| `CAPEX_DB_URL` | *(none — optional)* | Full PostgreSQL DSN. Its presence is what makes PostgreSQL "configured" | **YES** (embeds a password) | `app/run.py` (`_postgres_configured()`), `pg/config.py:199` |
| `CAPEX_DB_HOST` | *(none — required if `CAPEX_DB_URL` unset)* | PostgreSQL host | no | `pg/config.py:203` |
| `CAPEX_DB_PORT` | `5432` | PostgreSQL port | no | `pg/config.py:210` |
| `CAPEX_DB_NAME` | `capex` | Database name | no | `pg/config.py:211` |
| `CAPEX_DB_USER` | `capex_app` | Login role | no | `pg/config.py:212` |
| `CAPEX_DB_SSLMODE` | **`verify-full`** | libpq TLS verification | no | `pg/config.py:213` (and `:243` on the URL path) |
| `CAPEX_DB_SSLROOTCERT` | `None` | CA bundle path for `verify-full` | no | `pg/config.py:214` |
| `CAPEX_DB_PASSWORD` | *(none — required)* | PostgreSQL password, resolved only at DSN-assembly time and never stored on the config object | **YES** | `pg/config.py:39` (`EnvSecretProvider.get`), named by `from_env` |
| `CAPEX_DB_URL_PASSWORD` | *(none)* | Password lifted **out of** `CAPEX_DB_URL` into an in-memory provider, so a `repr()` cannot reach it | **YES** | `pg/config.py:39`, named by `_from_url` |
| `CAPEX_PROFILE` | `""` (`"unset"` at one site) | `local-demo` enables destructive administration (`/api/admin/reset`) and PostgreSQL seeding | no | `auth.is_demo_profile()`, `pg/seed.py`, `main.py` (`/api/health`) |
| `HOST` | `127.0.0.1`, or `0.0.0.0` with `--public` | Bind address | no | `app/run.py`, `main()` |
| `PORT` | `8000` | Listen port | no | `app/run.py`, `main()` |
| `X_ZOHO_CATALYST_LISTEN_PORT` | *(none)* | Catalyst AppSail's assigned port. **Takes precedence over `PORT`** | no | `app/run.py`, `main()` |
| `DEMO_USER` | *(none)* | **Enforces nothing.** See the warning below | credential-adjacent | `app/run.py`, `main()` |
| `DEMO_PASSWORD` | *(none)* | **Enforces nothing.** See the warning below | **YES** | `app/run.py`, `main()` |

Not connection-configurable: `connect_timeout` (10 s) and `application_name`
(`capex-wbs-hub`) are dataclass defaults at `pg/config.py:134-135`, not
environment variables.

### Tooling and tests only — never set these in production

| Variable | Default | Controls |
|---|---|---|
| `CAPEX_VRT_PORT` | `8799` | VRT server port; also derives the VRT database path |
| `CAPEX_VRT_REUSE` | unset (do not reuse) | Attach to a server you started yourself |
| `CAPEX_VRT_WRITE_INVENTORY` | unset | Regenerates `vrt-inventory.json` — a deliberate act with a reviewable diff |
| `CAPEX_VRT_WRITE_REGIONS` | unset | Rewrites region evidence artefacts |
| `CAPEX_EVIDENCE_LABEL` / `CAPEX_EVIDENCE_PORT` | `before` / `8799` | Evidence capture |
| `UAT_BASE` | `http://127.0.0.1:8000` | Default `--base` for the UAT harness |

### Three inconsistencies worth a decision

1. **`CAPEX_DB_SSLMODE` defaults to `verify-full` in the product and `prefer` in
   the test harness** (`tests/conftest_pg.py:118`). `prefer` performs no
   certificate or hostname validation and falls back to plaintext — the exact
   downgrade the comment above `from_env` says was deliberately removed
   from the product path. The test path still has it.
2. **`CAPEX_PROFILE` has two defaults**: `""` at the two gates that compare
   against `local-demo`, `"unset"` where `/api/health` reports it. Behaviourally benign, cosmetically confusing.
3. **`X_ZOHO_CATALYST_LISTEN_PORT` falls back to 8000 in `app/run.py` and 9000
   in the two probe/spike services** under `docs/`. Three processes for the same
   AppSail runtime disagree.

### ⚠ `DEMO_USER` and `DEMO_PASSWORD` protect nothing

`DEPLOY.md` once stated that the app "demands a username and password before a
single screen loads" when these are set. **It does not — and DEPLOY.md no
longer says so**: that sentence survives there only inside its own retraction,
so do not go hunting for it as a live claim. The facts are unchanged. They are
read once, at
`app/run.py`'s `main()` (the non-loopback bind warning), inside a condition that only prints a console warning. No
middleware, dependency or auth path in `app/backend/**` reads either variable.

The application *does* have a real user model — server-derived sessions, PBKDF2
at 240,000 rounds, roles the caller cannot choose — which `DEPLOY.md` predates.
But every seeded identity's password is `user_id + '!demo'` and both are
published. **A public URL is therefore protected by a credential an attacker can
derive in one guess.**

Do not expose this build beyond a trusted network until either the seeded
identities are removed and real ones provisioned, or an actual gate is
implemented. `DEPLOY.md` options 2 and 3 should not be followed as written.

---

## 5. Rollback checklist

**There is no down-migration.** `app/backend/pg/migrate_pg.py` has no
`downgrade`, no `--down`, and no reverse SQL; migrations `001`–`025` are
checksum-frozen and applied forward only. Rollback is therefore
**restore-from-backup plus redeploy the previous code**, and it depends
entirely on step 2 of §3 having been done.

Work top to bottom and stop at the first step that resolves the incident.

- [ ] **Confirm the symptom.** `GET /api/health`, `GET /readyz`, and the process
      exit code. `REFUSING TO START` in the log is a *schema* problem, not a code
      problem — go to the schema steps.
- [ ] **Is it code-only?** If the deploy applied **no** migration (compare the
      recorded pre-deploy versions with `SELECT version FROM schema_migration`),
      redeploy the previous commit and stop. **No data action is needed or
      wanted.**
- [ ] **Did a migration apply?** If yes, the previous code will refuse to start
      against the newer schema — that refusal is the gate working. You must
      restore.
- [ ] **Stop the application** before touching the database. Do not roll back
      under live traffic.
- [ ] **Restore the backup** — `pg_restore` the pre-deploy dump, or replace the
      SQLite file with the pre-deploy copy. Never hand-edit `schema_migration`
      to make a version disappear; the checksum gate exists to catch exactly
      that, and defeating it hides the drift rather than removing it.
- [ ] **Redeploy the previous commit.**
- [ ] **Verify the schema check passes.** The process starting at all is the
      assertion — it verifies the schema read-only before it serves.
- [ ] **Verify authorisation is intact.**
      `python tests/uat/uat_role_matrix.py --base <url>` → exit `0`. A rollback
      that restored data but left the role model wrong is not a completed
      rollback.
- [ ] **Verify the audit chain.** `GET /api/audit/verify` (SQLite) or
      `GET /api/audit/chain/verify` (PostgreSQL). A restore that broke the
      hash chain must be recorded, not quietly accepted.
- [ ] **Confirm Zoho is still MOCK.** `GET /api/health` → `"zoho_mode": "MOCK"`.
- [ ] **Record** what was restored, the backup timestamp, and the window of data
      lost between that backup and the incident. That window is real and someone
      will ask.

### Rollback risks specific to this build

* **Data written between the backup and the rollback is lost.** There is no
  reverse migration that could preserve it.
* **Sessions do not survive** a SQLite restore: `app_session` is in the same
  file. Every user is signed out. That is safe, and it will generate calls.
* **`/api/admin/reset` is not a rollback.** It rebuilds the demo dataset and
  revokes every session. It is gated on the Administrator role *and* the
  `local-demo` profile — leave `CAPEX_PROFILE` unset anywhere real, and it
  cannot be invoked at all.
