# Phase 0B Stage 1 — status

**Branch:** `phase-0b/connectivity-gate`, cut from `4759c79`
**Status: BLOCKED at the §6 Supabase stop gate.** Probe built and locally verified; **nothing deployed, nothing created.**

## Halt reason

**No authenticated Supabase account is available.** `https://supabase.com/dashboard/projects` redirects to sign-in.

The approved plan directs: *"If an authenticated Supabase account is unavailable, tell me the exact manual action required and stop."* Creating an account and entering passwords are both outside what I will do unattended, so this is a hard stop. The manual action required is in §5 below.

## Completed

| Item | Status |
|---|---|
| Branch from baseline `4759c79` | Done |
| Probe source (`probe/main.py`) | Done |
| Probe tests (`probe/test_probe.py`) — **36 passing** | Done |
| Dependencies vendored, Linux x86_64 CPython 3.13 | Done |
| Dependency inventory with per-wheel SHA-256 | Done |
| Build manifest and bundle SHA-256 | Done |
| Local unit, security and deadline tests | Done |
| Supabase project / role / database | **NOT STARTED — gated** |
| Deployment to `wbs-platform-spike` | **NOT STARTED — gated** |
| P1 / P2 execution and evidence | **NOT STARTED — gated** |

`wbs-capex-poc` was not accessed, requested, modified, redeployed or inspected at any point.

## Bundle

```
SHA-256: 2DCF27596AC7693CCC5D8832319A97ACBF9D89232C0759C95984AB34B8FDC674
Size:    3.47 MB · 491 files
Root:    main.py, app-config.json, requirements.txt, vendor/
Native:  vendor/pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so
Windows .pyd files: NONE
```

**The bundle itself is not committed** — 491 files of third-party wheel contents including a compiled binary, fully reproducible from `probe/dependency-inventory.json` via `probe/BUILD.md`.

Nineteen packages pinned. `pg8000` 1.31.5 is **pure Python**, so the database driver adds no second compiled wheel and a driver fault can never be mistaken for a network fault.

## Probe properties, and the tests that hold them

**Not an SSRF or port-scanning endpoint.** The caller supplies an endpoint *key* — `P1` or `P2` — and nothing else. Hostnames, ports and DSNs are never accepted from a request; the endpoint table is built from environment variables at start-up. `extra="forbid"` on the request model rejects any additional field outright. Proven by tests that attempt `169.254.169.254`, `localhost`, a supplied port, and injection through the endpoint field.

**P3 / port 6543 is unreachable.** Stage 2 only. Rejected by the request schema, and the endpoint table contains no port other than 5432.

**Authorisation precedes all network activity.** `POST /probe` requires `X-Probe-Token`, compared with `hmac.compare_digest`. A test monkey-patches `socket.getaddrinfo` and `socket.create_connection` to raise, then asserts an unauthorised request still returns 401 — proving nothing touches the network first. A missing token configuration fails closed. `GET /healthz` is public and touches no database. No CORS middleware is installed, so the probe is not browser-callable.

**Secrets cannot reach a response or a log.** Exceptions are reduced to class name plus `errno`/SQLSTATE **at the point of capture**; the message is discarded and the raw object never travels. Tests inject a password and a hostname into an exception message and assert neither appears in the response or stdout, that no traceback appears, and that the full hostname never leaves the service.

**Deadline behaviour matches the plan.** Hard 20 s on a monotonic clock, checked before each stage; budgets DNS 3 s, TCP 4 s, TLS 4 s, auth 5 s, query 3 s, summing to 19 s. An exhausted deadline marks remaining stages `skipped_deadline` and returns partial results. A truncated result with a stated reason is evidence; a platform timeout is not.

**TLS is verified, not merely negotiated.** Chain validation, hostname verification, SNI, TLS 1.2 minimum. A test greps the source for `CERT_NONE`, `check_hostname = False` and `_create_unverified_context` to ensure verification is never disabled. `pg8000` receives the same verified context, and the response records that it did.

**Claims are constrained.** The response always reports `q_b: "unresolved"` with a note that Q-B needs an authoritative Zoho commitment, a private path, or a client-approved architecture. `inet_client_addr()` is returned with `inet_client_addr_authoritative: false` and an explanation that behind a pooler it reports the pooler. Both are asserted by tests, so the probe cannot start over-claiming.

## Local verification limitation

The probe tests run against the **local interpreter's** FastAPI and pydantic, not the vendored tree. The vendored `pydantic_core` is a Linux `.so` and cannot load on Windows — which is the point, and confirms the vendoring is genuinely Linux-targeted rather than accidentally host-native.

Verified separately: vendored `pg8000` 1.31.5 imports and accepts both `ssl_context` and `timeout`.

**The assembled bundle can only be executed on Linux.** Its import path is proven when it runs on AppSail, not before — the same limitation the Phase 0A spike carried, and it was proven correct there.

## Manual action required to proceed

1. **Sign in to Supabase**, or create an account, at `https://supabase.com/dashboard`. Then tell me it is available.
2. **Before I create anything**, I will verify and report:
   - whether Mumbai `ap-south-1` is offered on the free tier
   - whether **network restriction / IP allowlisting** is available without a paid add-on
   - whether a **payment method** is demanded at any point
3. **If network restriction is unavailable**, creating the project means the database is publicly reachable. Per the plan that needs **separate explicit approval** — I will stop and ask, stating the residual risk. I will not apply `0.0.0.0/0` or `::/0` on my own judgement.
4. **I will not** enter a payment method, start a trial, or enable the paid IPv4 add-on.

If IPv4 turns out to be a paid add-on, **P1 (direct, IPv6) may be untestable on a free tier** — that is itself a finding, and P2 via the session pooler becomes the only free path.

## Reminders for when work resumes

- Q-B is reported **unresolved** unless authoritative Zoho evidence exists. Sampled address stability is a hypothesis.
- Stage 1 success is a **partial pass**. Phase 0B does not clear until Stage 2 proves the Cron/Event Function path.
- The Zoho support ticket at `../ZOHO_SUPPORT_TICKET_DRAFT.md` remains **unsent** and is a user action. Question 3 — whether any published range is contractual and whether it may change without notice — decides Q-B.
