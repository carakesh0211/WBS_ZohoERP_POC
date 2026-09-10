# Stage A — UAT visual preview on Zoho Catalyst (Fable 5.1)

**Status: DEPLOYED and verified on 2026-09-10. Visual and workflow review only.
Not financial UAT. Not production.**

## What is deployed

| | |
|---|---|
| Catalyst project | `wbs-capex-uat-fable51` — project id `4239000000144371`, org `60021033896`, **India data centre, Development environment only** |
| AppSail service | `wbs-capex-uat` — AppSail id `4239000000136436`, deployment id `4239000000136439`, deployed from the CLI |
| Development URL | `https://wbs-capex-uat-50045784768.development.catalystappsail.in` |
| Runtime | managed `python_3_13`, command `python3 -u main.py`, port `9000` (bound via `X_ZOHO_CATALYST_LISTEN_PORT`), 512 MB memory, 256 MB disk |
| Artifact | `wbs-capex-uat.zip`, 1,551 entries, 12,899,176 bytes, built at commit `8f39bb7`; SHA-256 `ff020494dcade7991dfc671a4cd8a9cf2c9ada383c895aaa727f1a623524ebe5`; the builder's manifest (`wbs-capex-uat.zip.manifest.json`, kept outside the repository) records the tree hash and the credential-set hash `e60679c8…f24262` |
| Application profile | `CAPEX_PROFILE=uat-preview` (set by the launcher, not by Catalyst configuration); `/api/health` reports it and `zoho_mode: MOCK` |
| Environment variables in Catalyst | **none** — `app-config.json` ships `env_variables: {}` and the bundle gate refuses otherwise, so no secret rides in the archive or the console |

No other Catalyst project or service was opened, changed or redeployed. The
existing `WBS-ZohoERP-POC` (4239000000062001), `wbs-capex-poc` and
`wbs-platform-spike` were not touched.

## What was verified on the live URL (2026-09-10)

| Check | Result |
|---|---|
| Root page | 200, cold start 3.8 s; banner `UAT — SYNTHETIC DATA — ERP MOCK` rendered; **no** `!demo` hint in the served page |
| Security headers | CSP `default-src 'self'` …, `X-Frame-Options: DENY`, `nosniff`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`, `X-Correlation-Id` on every response |
| `/api/health` | 200 `{"profile":"uat-preview","zoho_mode":"MOCK"}` |
| `/healthz` | 200 |
| `/readyz` | **503 `DatabaseNotConfigured`** — honest: no PostgreSQL is attached to the preview |
| `/docs`, `/redoc` | 404 (unmounted outside local-demo); `/openapi.json` 200 (route templates only; the analytics, closure and integration screens probe it) |
| Sign-in with the seeded `U-ADM!demo` | **401 INVALID_CREDENTIALS** |
| Sign-in with the UAT credential | 200; session-bearing calls to bootstrap, dashboard, WBS, PRs, POs, GRNs, bills, reconciliation, revisions, capitalisation, audit verify, Zoho connections (`mode: MOCK`) all 200 |
| PostgreSQL-backed routes (`/api/budget/cells`, `/api/approvals/inbox`, `/api/integrations/connections`, `/api/reports/metrics`) | uniform **503 `DATABASE_NOT_CONFIGURED`** |
| `/api/admin/reset` | 403 `RESET_DISABLED` |
| Navigation | 17 rail entries across 6 groups render for an administrator; screens deep-link |
| Session persistence | a signed-in session polled every 25 s stayed valid for the full 9-minute measurement window |
| Scale-to-zero recovery | after an idle gap the first request took ~0.45–3.8 s and served normally; **the instance restarts with a fresh copy of the seed** (see limitations) |

## Limitations — read before sharing the link

1. **Data is ephemeral.** The preview runs on a SQLite copy of the synthetic seed
   made at process start. An idle gap (minutes), a redeploy or a scale event
   starts a new process with a fresh copy: **anything entered is lost and every
   session is signed out.** This is disclosed on the sign-in page.
2. **It is not the PostgreSQL financial workflow.** Every screen backed by
   PostgreSQL (approval engine, budget cells, integrations, reports, closure,
   the new original-budget creation) answers a uniform 503 and renders its
   "unavailable" state. Stage B moves the preview to a managed PostgreSQL.
3. **Zoho is MOCK.** No call is made to any Zoho tenant; outbound ERP writes are
   disabled (`CAPEX_ERP_OUTBOUND_WRITES` unset = off, enforced server-side since
   commit `f725c5f`).
4. **`/openapi.json` is reachable without a session.** It exposes route
   templates, never data; the SPA needs it to tell an unmounted route from an
   empty one.
5. **Single instance assumed.** Sessions live in the per-process SQLite copy;
   Catalyst may run up to five instances under load, and a session created on
   one instance is unknown to another. Keep the AppSail instance ceiling at 1
   for the preview (Console → AppSail → wbs-capex-uat → Configuration).
6. **No accessibility or VRT claim is made about the hosted copy**; those run
   locally against the same commit.

## Credentials — how they are issued and rotated

The seeded `<user-id>!demo` passwords are **not installed** under the preview
profile; the boot refuses to start without a credential file rather than fall
back to them. Nine UAT identities exist (`U-REQ`, `U-PM`, `U-PLH`, `U-PROC`,
`U-FIN`, `U-PFC`, `U-CFO`, `U-AUD`, `U-ADM`) with the same roles as the demo.

The plaintext passwords are in `uat-users.txt` in the credential directory on
the build machine (`%USERPROFILE%\.capex-tools\uat-credentials\`), which is
outside the repository and was never printed, committed or screenshotted.
Only PBKDF2 hashes ship in the bundle.

To rotate:

```bash
python tools/appsail/uat_credentials.py --rotate %USERPROFILE%\.capex-tools\uat-credentials
python tools/appsail/build_uat_bundle.py --wheels %USERPROFILE%\.capex-tools\appsail-staging\wheels --credentials %USERPROFILE%\.capex-tools\uat-credentials\uat-credentials.json --out %USERPROFILE%\.capex-tools\appsail-out\wbs-capex-uat.zip
```

then extract the archive over `%USERPROFILE%\.capex-tools\appsail-deploy\bundle`
and run, from `%USERPROFILE%\.capex-tools\appsail-deploy\project`:

```bash
catalyst deploy --only appsail:wbs-capex-uat -ni
```

The old passwords stop working the moment the new deployment starts. The
credentials file's SHA-256 is printed by the tool and recorded in the bundle
manifest, so a deployment can be tied to a credential set without revealing it.

## Safe team access

* Share the URL and the credentials through **separate channels**; never paste
  a password into chat, a ticket or a commit.
* Give reviewers the identity that matches their UAT role; `U-AUD` is
  read-only.
* Tell reviewers the data resets on idle and that PostgreSQL-backed screens are
  intentionally unavailable in this stage.
* Sign-in is throttled (10 failures per user id or per address in 15 minutes ⇒
  429 with `Retry-After`) since commit `9e0c2cf`; the deployed artifact predates
  that commit and must be rebuilt to carry it.
