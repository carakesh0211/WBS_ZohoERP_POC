# Phase 0B Stage 1 — status

**Branch:** `phase-0b/connectivity-gate`, cut from `4759c79`
**Status: HALTED before probe execution. Exposure window closed early and cleanly.**

Q-A (can AppSail reach an external PostgreSQL on 5432?) is **NOT ANSWERED**.
Q-B (is the path securable by network controls?) remains **UNRESOLVED**, as it must.

## What was built, deployed and proven

| Item | Status |
|---|---|
| Probe source, 36 local tests | Done (unchanged from the prior commit) |
| Vendored Linux x86_64 CPython 3.13 dependencies | Done |
| **Probe deployed to `wbs-platform-spike`** | **Done** — `GET /healthz` → 200, Python **3.13.9 / x86_64**, cold start **2593 ms** |
| **`POST /probe` rejects an unauthenticated request** | **Done** — 401, proving authorisation precedes all network activity in the deployed build |
| Supabase throwaway project, hardened ephemeral role | Done, then destroyed |
| **P1 / P2 execution** | **NOT RUN — deliberately abandoned. See below.** |

`wbs-capex-poc` was not accessed, requested, modified, redeployed or inspected at any point.

## Why P1 and P2 were not run

Two reasons. The second is on its own decisive.

**1 — Setting `PGPASSWORD` through browser automation would have written the password into the session transcript.**
Catalyst environment variables are set through a Console dialog; there is no way to type a value into that field without the value appearing verbatim in the automation call. The governing instruction is that passwords must never be displayed, logged or reproduced. No partial connectivity result is worth breaching that, so the variables were never created — the Configuration panel was left in its empty state, which was verified after the fact.

**2 — The result would have been partial regardless.**
The deployed bundle ships **no `ca-bundle.pem`**. Supabase presents a certificate chain that the bundle cannot verify, and the probe refuses to disable verification (a test greps the source for `CERT_NONE`, `check_hostname = False` and `_create_unverified_context`). P1 and P2 would therefore have answered DNS and TCP and then failed at the TLS stage on a **packaging gap of ours**, not a Catalyst limitation. Reproduced identically from a local machine against all three endpoints, which is how the cause was isolated.

**This is the substantive finding of the attempt, and it is a Stage 1 prerequisite that the test plan did not carry:** a CA bundle for the database provider must be vendored into the deployment bundle before the probe can answer anything past TCP. Zoho support question 6 — whether a custom CA bundle may be shipped inside the deployment bundle — is therefore no longer a nice-to-have; it gates the re-run.

## Endpoint facts established (from the local machine, not from AppSail)

These are **not** answers to Q-A. They characterise the target, not the platform's ability to reach it.

| Endpoint | Family | TLS | Chain verified |
|---|---|---|---|
| Direct, port 5432 | **IPv6 only** | TLS 1.3, `TLS_AES_256_GCM_SHA384` | **No** — no CA bundle |
| Session pooler, port 5432 | **IPv4** | TLS 1.3, `TLS_AES_256_GCM_SHA384` | **No** — no CA bundle |

The direct host being IPv6-only on the free tier is worth carrying forward: if AppSail egress is IPv4-only, P1 is untestable without the paid IPv4 add-on, and the session pooler becomes the only free path. That remains a hypothesis — it was not tested.

## Exposure window and cleanup

| | |
|---|---|
| Project created | 2026-08-29 **00:13:10** local |
| Project deleted | 2026-08-29 **00:27:29** local |
| **Actual public exposure** | **14 min 19 s** of an authorised 60 min |
| Database contents during exposure | Empty. No schema, no data, no Data API |

Cleanup performed and verified:

- **Supabase project deleted** — navigated **by project reference**, so a different project could not be reached; the confirmation dialog required the project name typed in full. Organisation `carakesh` now lists **1 project**.
- **The one pre-existing project was never opened** and remains paused, exactly as found.
- **Ephemeral credentials are dead** — the project host no longer resolves (`getaddrinfo` failure), which destroys the role along with the database.
- **Local secret files deleted** from the scratchpad.
- **Zero environment variables** on `wbs-platform-spike`, verified in the Console after cleanup.

## The deployed probe is inert, and why that is structural rather than a promise

The probe binary remains deployed on `wbs-platform-spike`. It cannot act:

- `_endpoints()` builds its table from `PGHOST_DIRECT` / `PGHOST_POOLER`. Both unset → the table is empty → **P1 and P2 do not exist as reachable keys**, and the request schema accepts nothing else.
- `PROBE_TOKEN` unset → authorisation **fails closed**; the 401 was observed against the live deployment.
- The endpoints it was configured for no longer resolve.

**One outstanding item requiring the user:** restoring the plain FastAPI bundle needs a manual ZIP upload (`wbs-platform-spike-vendored.zip`) and cannot be automated. This is cosmetic, not a security matter, for the reasons above.

## What Stage 1 needs before it is re-attempted

1. **Vendor a CA bundle** into the probe and load it explicitly. Without this the probe cannot pass TLS, and DNS+TCP alone leaves Q-A half-answered.
2. **Supply secrets without a browser.** Setting Catalyst environment variables through the Console dialog is incompatible with the no-secrets-in-transcript rule. Either the user sets the six variables directly, or Stage 1 uses a mechanism that never routes a credential through automation.
3. Only then re-create a throwaway project and run P1 and P2.

Items 1 and 2 are both **plan defects**, not execution failures. The plan specified the variables and the deadline budget but never said who types the password, and it assumed TLS verification would succeed against a provider using its own CA.

## Unchanged

- **Q-B is UNRESOLVED.** No authoritative Zoho evidence was obtained. Sampled address stability remains a hypothesis and nothing observed here changes that.
- Stage 1 success would have been a **partial pass** in any case. Phase 0B does not clear until Stage 2 proves the Cron/Event Function path.
- The Zoho support ticket at `ZOHO_SUPPORT_TICKET_DRAFT.md` remains **unsent** and is a user action. Question 3 decides Q-B; question 6 now also gates the Stage 1 re-run.
