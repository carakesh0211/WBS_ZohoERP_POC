# Evidence manifest — FastAPI on Catalyst AppSail

**Captured:** 2026-08-28 · **Result: PROVEN** · Narrative: [`../../PHASE_0A_EXIT.md` §8](../../PHASE_0A_EXIT.md)

## Platform

| | |
|---|---|
| Console | `https://console.catalyst.zoho.in` — **India data centre** |
| Project | `WBS-ZohoERP-POC` |
| Project ID | `4239000000062001` |
| Portal ID | `60021033896` |
| Environment | **Development only.** Production never deployed |

## Service

| | |
|---|---|
| AppSail service | `wbs-platform-spike` |
| Service ID | `4239000000096001` |
| URL | `https://wbs-platform-spike-50044908499.development.catalystappsail.in` |
| Created | 2026-08-28 18:21 IST |
| Disposition | Left **deployed but idle at zero instances**, for reuse in the Phase 0B PostgreSQL connectivity test. Not deleted |

`wbs-capex-poc` was **not modified, redeployed, disabled, replaced or sent a request** at any point.

## Deployments

| Deployment ID | Time | Bundle | Outcome |
|---|---|---|---|
| `4239000000096004` | 18:21 IST | no vendored dependencies | Build **Success**, runtime **failed** — 503 on every request |
| **`4239000000096007`** | **19:40 IST** | **vendored, Linux x86_64 CPython 3.13** | **Success — serving** |

### Successful bundle

```
SHA-256: 2F9E6C0655E41A394EC6403C567606088DD1BF430D26872BE39FC5441E9ACA8A
Size:    3,220,617 bytes (3.07 MB)
Files:   413
Root:    main.py, app-config.json, requirements.txt, vendor/
Native:  vendor/pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so
Windows .pyd files: NONE
```

The archive itself is **not committed** — see [`README.md`](README.md). Rebuild from [`vendored-versions.txt`](vendored-versions.txt) per [`BUILD.md`](BUILD.md).

## Runtime configuration

| Setting | Value |
|---|---|
| Deployment type | Catalyst-Managed Runtime |
| Stack | Python 3.13 — reported `3.13.9`, CPython, **x86_64** |
| Startup command | `python3 -u main.py` |
| Port | 9000 |
| Memory / Disk | 512 MB / 256 MB |
| Environment variables | **none** — no credentials, no client data, nothing to redact |

## Endpoint results

`GET /` → **200**

```json
{
  "framework": "fastapi",
  "proof": "FastAPI serves requests on Zoho Catalyst AppSail",
  "fastapi_version": "0.133.1",
  "pydantic_version": "2.13.4",
  "python": "3.13.9",
  "implementation": "CPython",
  "machine": "x86_64",
  "listen_port_env": "9000",
  "bound_port": 9000,
  "seconds_from_process_start": 0.924,
  "dependencies_vendored": true
}
```

`GET /healthz` → **200** `{"status":"ok","framework":"fastapi"}`
`GET /nope` → **404** `{"detail":"Not Found"}` — FastAPI's own router, not a static file server

## Startup deadline

**Bound and served in 0.924 s** against AppSail's documented **10-second** deadline — roughly 10× headroom. Reproduced at **0.938 s** on an independent cold start.

`listen_port_env: "9000"` confirms the port contract: AppSail sets `X_ZOHO_CATALYST_LISTEN_PORT`, and reading it inside Python was necessary because the start command runs without a shell.

## Cold start and scale-to-zero

| Measurement | Value |
|---|---|
| Cold start #1 | **2,224 ms** round trip · process uptime 0.924 s |
| Cold start #2, after ~7 min idle | **1,843 ms** round trip · process uptime 0.938 s |
| Warm `GET /` | 475 / 352 / 419 ms |
| Warm `GET /healthz` | 385 / 443 ms |
| Instances while serving | **1** — `567e7cf5-689b-4515-ae03-12e16bb0d7a7` |
| Instances after ~7 min idle | **0** — *"There are no instances running…"* |

Cold start costs roughly **1.4–1.8 s** over warm.

**Scale-to-zero is confirmed empirically, not inferred from documentation.** The second cold start returned a *fresh* process — uptime 0.938 s, not a continuation of the ~500 s-old one — so the instance was genuinely reclaimed and respawned rather than paused.

This validates the constraint plan §2.1–2.2 is built on: **AppSail is a request/response tier and cannot host a resident worker.** Background work must run on Job Scheduling → Cron/Event Functions.

## CI

Repository CI green on all four jobs at the commit recording this proof:

**https://github.com/carakesh0211/WBS_ZohoERP_POC/actions/runs/33179677365**

| Job | Result |
|---|---|
| Contract and inventory gates | success |
| Regression suite | success — 488 passed, 1 xfailed |
| Supply chain | success — 27 packages, closure complete, 0 vulnerabilities |
| Visual regression (windows-latest) | success — 60 passed |

## Limitations of this evidence

- **Console "View Logs" could not be opened** from the automation browser pane across repeated attempts and viewport sizes — most likely a blocked popup. All startup timings above come from the application's own instrumentation, which is more precise than a log tail but is *not* the console log. If the console log is required for an audit, capture it from a normal browser session.
- **`wbs-capex-poc` health is unverified.** It shows *Live* with *0 instances* — exactly what this service showed while broken — so *Live* means "deployed", not "healthy". It carries `CAPEX_DB_PATH=/tmp/capex.db`, which is precisely the **DEF-01** condition (a database file present at boot). Flagged, not diagnosed; no request was sent to it.
- **The proof covers the runtime contract only.** It does not test outbound network egress, PostgreSQL connectivity, or static egress IPs. Those are the Phase 0B gate (plan §2.4) and remain unproven.
