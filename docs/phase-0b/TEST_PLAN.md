# Phase 0B — AppSail to PostgreSQL connectivity gate

**Status:** PLAN ONLY. Awaiting approval. **Nothing has been created** — no database, account, credential, ticket, Function or deployment.
**Plan reference:** v1.2.1 §2.4 (the gate) and §2.5 (fallbacks).
**Scope rule:** `wbs-capex-poc` is never accessed or modified. All work uses the existing `wbs-platform-spike` service.
**Execution branch:** on approval, work begins on a new branch **`phase-0b/connectivity-gate`**, cut from
`phase-0a/foundations`. Phase 0A remains closed and is not reopened; no Phase 0B commit lands on it.

---

## 1. What is actually unknown

This gate is not "does PostgreSQL work". It is whether Catalyst AppSail can reach an external PostgreSQL **and whether that path can be secured**.

| | Question |
|---|---|
| **Q-A** | Can an AppSail container open an outbound TCP/TLS connection to an external PostgreSQL and authenticate? |
| **Q-B** | Can that path be secured by network controls, rather than by credentials and TLS alone? |

### The epistemic position, stated precisely

**No publicly documented static egress IP or CIDR was found** for Catalyst projects or data centres. Zoho's Database Connector CodeLib is documented for *Functions*, not AppSail, and no page describes an outbound egress policy.

**Absence of documentation is not evidence that the capability does not exist.** Zoho may operate stable egress ranges, or a private-network path, and simply not publish them. That is precisely why §5 raises an authoritative question rather than inferring an answer from silence.

### What can and cannot close Q-B

**Q-B cannot be closed by observation.** Repeated cold-start sampling can *disprove* stability — a single varying address is conclusive — but no finite number of matching samples proves an address is static. Sampling produces a hypothesis, not a commitment.

**Q-B passes only on one of:**

1. an **authoritative Zoho commitment** to a static IP or CIDR, in writing, with its change policy stated; or
2. a **supported private-network path** (peering, private link, or equivalent); or
3. **another security architecture explicitly approved by the client**, accepting the residual risk in writing.

Anything else leaves Q-B **unresolved**, however many green ticks the test produces.

### The two questions have different evidence types, and different blockers

This distinction was got wrong once and is recorded here so it cannot be got
wrong again.

| | **Q-A** | **Q-B** |
|---|---|---|
| Nature | **Empirical.** A property of the running platform | **Authoritative.** A commitment, or an accepted risk |
| Settled by | Deploying a probe and observing five stages | A written Zoho commitment, a private path, or client sign-off |
| Blocked by the Zoho ticket? | **No** | **Yes — question 3** |
| Verdict if the ticket is never answered | Still answerable | **Unresolved**, permanently |

**The unsent Zoho support ticket does not block Q-A, and must never be allowed
to.** Q-A is measured, not asked. The ticket proceeds independently.

**Support question 6 — may a custom CA bundle ship inside the deployment
bundle — does not gate the Stage 1 retry.** AppSail has already accepted and
executed from a deployment ZIP containing vendored files, in the Phase 0A spike
and again in the first Stage 1 deployment. Whether a `.pem` file alongside
`main.py` loads is therefore an **empirically testable deployment property**,
and Stage 1 tests it by deploying it. A written answer to question 6 is still
worth having, and it is still worth asking, but waiting for it would be waiting
for permission to observe something we can simply observe.

**Question 3 remains decisive for Q-B**, exactly as before. Nothing in this
correction relaxes that.

---

## 2. Test design — five checks, independently attributable

The lesson from the Phase 0A FastAPI spike is that AppSail reports any startup failure as one opaque message. Every step is therefore timed and recorded separately, so a DNS failure can never be mistaken for an auth failure.

| # | Check | Method | Isolates |
|---|---|---|---|
| 1 | **DNS** | `socket.getaddrinfo(host, port)` | Resolver reachability; records address family (A / AAAA) and resolved addresses |
| 2 | **TCP** | `socket.create_connection(..., timeout=4)` | Outbound egress permitted on the port — **this is Q-A** |
| 3 | **TLS** | PostgreSQL `SSLRequest`, expect `S`, then wrap with a **verifying** `SSLContext` (§4) | Certificate chain, hostname, SNI, protocol, cipher |
| 4 | **Auth** | `pg8000` connect, SCRAM-SHA-256, over a verified context | Credentials and `pg_hba` policy |
| 5 | **Query** | `SELECT 1`, `version()`, `current_user`, `inet_client_addr()` | Round trip, and the observed source address (§3) |

### Timeout budget

AppSail terminates a request at **30 seconds**. A test that hits the platform cap returns nothing and is
indistinguishable from a hang, so the run must fail *itself* first and return partial results.

**Hard overall deadline: 20 seconds.** Enforced against a monotonic clock, checked **before each stage
starts**. If the remaining budget is smaller than the next stage's allowance, the run aborts immediately and
returns everything gathered so far, marking the unrun stages `skipped_deadline`. A truncated result with a
clear reason is evidence; a platform timeout is not.

| Stage | Budget | Rationale |
|---|---|---|
| 1 DNS | **3 s** | Resolution succeeds in milliseconds or fails; a long wait means no resolver path |
| 2 TCP | **4 s** | A blocked egress path typically hangs to the timeout rather than refusing |
| 3 TLS | **4 s** | Handshake plus chain validation |
| 4 Auth | **5 s** | SCRAM round trips; the most latency-sensitive stage, and pooler-dependent |
| 5 Query | **3 s** | `SELECT 1` on an established connection |
| **Sum** | **19 s** | 1 s margin inside the deadline, 10 s inside the platform cap |

Socket and driver timeouts are set to the per-stage budget, so no single stage can consume another's
allowance.

**Driver: `pg8000`.** Pure Python, so no second compiled wheel to vendor and one fewer failure mode that could masquerade as a network failure. The Phase 1 driver choice stays open — this gate tests the network path, not the driver.

---

## 3. Source-address observation — and its limits

`inet_client_addr()` returns the address **the database server sees**. That is not necessarily the AppSail container's egress address.

**When a provider proxy or connection pooler sits in the path, `inet_client_addr()` reports the pooler, not the client.** It is then evidence about the provider's internal topology and says nothing about Catalyst egress.

Therefore:

- **Record `inet_client_addr()` always**, but **never treat it as authoritative** for egress identity.
- **Record the endpoint type with every observation** — one of `direct`, `session-pooled`, `transaction-pooled`. An observation without this label is uninterpretable.
- **Obtain the true source address from the provider's own connection or firewall logs** where available. That is the authoritative observation, because it is recorded at the network boundary rather than inside a pooled session.
- Treat every address observation as an **empirical sample**, feeding the Q-B hypothesis. It never closes Q-B by itself (§1).

---

## 4. TLS requirements

Negotiating encryption is not sufficient. An unverified TLS session is vulnerable to interception and proves only that *something* answered.

The test must construct an `ssl.SSLContext` with:

- **Certificate chain validation** — `verify_mode = ssl.CERT_REQUIRED`, against the provider's CA bundle shipped in the deployment bundle.
- **Hostname verification** — `check_hostname = True`.
- **SNI** — the server hostname passed to `wrap_socket(server_hostname=...)`.
- **TLS 1.2 minimum**, 1.3 preferred.

**Recorded:** negotiated protocol version, cipher suite, peer certificate subject, issuer, and validity dates.

### The CA bundle is a required test artefact

Supabase presents a chain rooted in **its own CA**, so a client verifying
against the system trust store fails correctly and uninformatively. The bundle
is therefore not an optimisation; without it the probe cannot reach the auth
stage at all.

| Requirement | Enforcement |
|---|---|
| `ca-bundle.pem` obtained **only from the exact throwaway project under test**, from its own Database Settings, with the project reference recorded first; never from a TLS handshake, a third-party repository, or any pre-existing project | `probe/CA_BUNDLE.md` provenance rule; SHA-256, subject, issuer, validity and download time recorded, and the fingerprint re-checked against `/healthz` before the ephemeral role is created |
| Present at the archive root and byte-identical to the validated file | `build_bundle.py::verify_zip` — **the build fails otherwise** |
| Missing, empty, malformed or expired → **fail closed** | `ca.py::CaBundleUnusable`; never a fallback to system CAs |
| The pinned bundle is the **only** trust anchor | `test_ca_bundle.py` |
| Verification never disabled to work around a packaging fault | source scan across `main.py`, `ca.py`, `build_bundle.py` |

The first attempt shipped no bundle **and** degraded silently to system CAs when
the file was absent, so a packaging omission presented as a live-endpoint
verification failure — indistinguishable from an egress restriction. Both halves
are now closed: the artefact is mandatory, and its absence is loud.

**Explicitly recorded:** whether the `pg8000` connection in step 4 uses **the same verified context** as step 3, or a separate one. If pg8000 cannot be given the verified context, that is a finding in its own right and must be reported rather than glossed — a verified handshake in step 3 does not license an unverified one in step 4.

---

## 5. Questions for Zoho support

To be raised as a single ticket. Context to include so the answer is unambiguous:

| Field | Value |
|---|---|
| Data centre | **India (IN)** — `console.catalyst.zoho.in` |
| Project | `WBS-ZohoERP-POC` |
| Project ID | `4239000000062001` |
| AppSail service ID | `4239000000096001` (`wbs-platform-spike`) |
| Environment | Development |

**Questions:**

1. Is arbitrary **outbound TCP** from an AppSail container permitted, on **5432** and **6543** specifically? Any port, protocol, proxy or allowlist restriction?
2. **Is there a static egress IP address or CIDR range** — per project, per data centre, or per account — that a customer may allowlist on a third-party service?
3. If yes: **is that range contractual or best-effort**, and **may published CIDRs change without notice**? What notice period, if any, applies to a change?
4. If no: is a **private-network path** (peering, private link, VPC-style connectivity, or equivalent) available or on the roadmap for outbound database access?
5. Are the answers to 1–4 **identical for Cron and Event Functions**, or do those surfaces have a different egress policy? *(The Database Connector CodeLib is documented for Functions, not AppSail, so we cannot assume parity.)*
6. Is outbound **TLS with full certificate and hostname verification** supported, and may a custom CA bundle be shipped inside the deployment bundle? *(Confirmation only — **this does not gate Q-A or the Stage 1 retry**; see §1. Shipping the bundle is tested empirically.)*
7. Documented **outbound connection limits** — concurrent sockets per instance, connection lifetime, idle timeout?
8. Does the platform **terminate long-lived outbound connections**, and after how long? *(Bears directly on connection pooling in Phase 1.)*

**Question 3 is the one that determines whether Q-B can ever pass.** An IP range that may change without notice is not a basis for an allowlist in a financial control system.

**No question in this ticket gates Q-A.** Q-A is settled by deploying the probe and observing it. The ticket and the Stage 1 retry proceed in parallel and neither waits on the other.

---

## 6. Provider recommendation

**Decided by the client: Supabase, Mumbai `ap-south-1`.**

India region keeps the test consistent with the recommended production data-residency baseline (§10) and avoids introducing a second variable.

The provider is settled. What remains to verify is **entitlement**, not choice.

**Subject to verification before anything is created:** the free account's **actual network-restriction entitlement**. Network restrictions, IP allowlisting and IPv4 addressing are commonly paid or plan-gated features. If the free tier cannot restrict network access at all, that materially changes §9 and must be established first, not discovered mid-test.

### Three endpoint probes

Supabase exposes different endpoints with different network characteristics. Each must be probed separately, because a failure on one says nothing about the others.

| Probe | Endpoint | Port | Family | Purpose |
|---|---|---|---|---|
| **P1** | Direct database | 5432 | **IPv6** | Does AppSail have outbound IPv6 at all? Unknown and untested |
| **P2** | Shared **session** pooler | 5432 | **IPv4** | The realistic AppSail path if IPv6 is unavailable |
| **P3** | **Transaction** pooler | 6543 | IPv4 | For the Stage 2 Function test — the right mode for short-lived serverless invocations |

**P1 is the informative one.** If AppSail has no outbound IPv6, direct connection is impossible on a free tier without a paid IPv4 add-on — a cost and architecture finding, not merely a test result.

Every observation in §3 must record which probe produced it.

---

## 7. Two execution surfaces — Stage 1 does not clear the gate

Plan §2.2 places request handling on AppSail and **all background work on Cron/Event Functions**: polling, sweeps, outbox drain, reconciliation, audit anchoring. Both surfaces need database access. They are different execution environments and the CodeLib is documented only for Functions.

| Stage | Surface | Resource | Approval |
|---|---|---|---|
| **1** | AppSail | New deployment on existing `wbs-platform-spike` | This plan |
| **2** | Cron or Event Function | **Creates a Function** | **Separate approval, requested after Stage 1 reports** |

**Stage 1 success is a partial pass only.** Phase 0B cannot clear until Stage 2 proves the Function path, because Phase 1's entire background-job architecture depends on it. Reporting Stage 1 as "the gate passed" would be false.

---

## 8. Logging and response safety

**The spike is standalone.** It deliberately contains no CAPEX application code, and therefore **none of the Phase 0A observability layer** — no `StructuredFormatter`, no redaction, no protected root keys. Those protections do not apply here and must not be assumed.

The spike therefore carries its own minimal rules, enforced by construction rather than by a formatter.

**Never logged or returned, under any circumstance:**

- environment variable **values** of any kind
- the DSN or connection string, whole or partial
- passwords, or any credential material
- raw exception objects, exception messages, or tracebacks

**Permitted in logs and responses:**

- stage name and outcome (`dns`, `tcp`, `tls`, `auth`, `query` → `pass` / `fail`)
- timings, in milliseconds
- a **sanitised error classification** — exception class name and, where available, a driver error code. Never the message text, which routinely echoes host, user and connection parameters
- endpoint type (`direct` / `session-pooled` / `transaction-pooled`) and probe id
- non-secret host metadata — hostname, port, resolved address family
- TLS protocol, cipher, certificate subject/issuer/validity
- `SELECT 1` result, `version()`, `current_user`

**Construction rule:** exceptions are caught per stage and reduced to `type(exc).__name__` plus an optional driver code **at the point of capture**. The raw exception is never carried forward into a variable that could reach a log or a response. This is the same class of leak the Phase 0A review found in tracebacks, and the fix is the same: never let the object travel.

---

## 9. Database hardening

To be created only after approval, and only as an isolated throwaway.

- **Dedicated, empty database.** No schema, no tables, no data, no CAPEX objects. `SELECT 1` requires none.
- **Ephemeral login role**, created for this test alone.
- **`CONNECT` only.** No `CREATE`, no schema creation rights, no object ownership.
- **Revoke unnecessary `PUBLIC` privileges** — notably `REVOKE ALL ON DATABASE … FROM PUBLIC` and `REVOKE ALL ON SCHEMA public FROM PUBLIC`, so the role cannot reach anything by default inheritance.
- **High-entropy password**, generated at creation, never reused, never committed, never logged.
- **SSL enforced** at the server, so an unencrypted connection is refused rather than silently accepted.
- **Short validity** — `VALID UNTIL` set to a few hours, so the credential expires even if teardown is interrupted.
- **Immediate revocation** at the end of the test (§11), not deferred.

### The deadline must be enforced by something other than Claude

Attempt 2 overran its authorised window by 2 min 18 s. The minute-45 rule was
written into the runbook and enforced by nothing: Claude checked the clock only
when it happened to run a command, and spent 31 minutes blocked on a manual step.

**A deadline that depends on the assistant remembering to look at a clock is not
a control.** From attempt 3, project creation is forbidden until the operator
confirms **two independent alarms** are armed:

| Alarm | Fires at | Effect |
|---|---|---|
| Warning | creation **+40 min** | No new work starts |
| Mandatory cleanup | creation **+45 min** | Operator notifies; teardown begins immediately, whatever is in progress |

60 minutes remains an **absolute breach threshold**, not a target.

### Network exposure requires its own approval

If the verified free-tier configuration cannot restrict network access, then running this test means **temporarily allowing public connectivity to the database**.

**That requires separate, explicit approval — even with an empty database and ephemeral credentials.** It will not be done silently, and `0.0.0.0/0` or `::/0` will not be applied on my own judgement. If restriction is unavailable, I will report that and ask, stating the residual risk, before proceeding.

---

## 10. Cost

**Expected $0, if the verified free-tier configuration requires no payment method and no paid add-on.**

That is a conditional, not a promise. Two things commonly break it: **IPv4 addressing** and **network restriction / IP allowlisting** are frequently paid features.

**Before creating anything, the actual account and checkout screens will be captured as evidence** — showing whether a payment method is demanded and whether the needed features sit behind a paid tier. No Catalyst billing will be configured, and no payment method entered anywhere.

**Data residency.** The throwaway database holds no real data, so its region is a low-risk choice for the gate.

For production, an **India region is the recommended baseline** — the client is India-based and Zoho ERP is
India-only — but that is a **recommendation pending written client and compliance confirmation**, not a
settled requirement. Data residency obligations may be stricter than the recommendation (a specific region,
a named provider, or on-premises), or looser. Neither is for engineering to decide. The confirmation should
be obtained in writing and recorded against plan decision **D-16** before Phase 1 selects a provider.

---

## 11. Cleanup

1. Remove the temporary environment variables from `wbs-platform-spike`.
2. Delete the Supabase project **by its recorded reference**. This drops the ephemeral role and the database with it.
3. **Confirm deletion against the control plane:** the exact recorded project reference must be **absent from the organisation-level project listing**, or resolve to not-found. This is the proof.
4. DNS non-resolution of the project host is **secondary corroboration only, never the sole proof** — records can be cached or persist briefly after deletion, so a non-resolving host is consistent with both outcomes.
5. Confirm the pre-existing project is untouched **from the organisation-level listing alone**. No project is opened to check it.
6. Redeploy the plain FastAPI bundle, returning the spike to its Phase 0A proven state.
7. Confirm zero live instances.
8. Record teardown in the evidence manifest **with timestamps**, including explicit confirmation that credentials died with the project and any temporary network exposure was withdrawn.

**Two credentials, two different lifetimes, and they must not be conflated.** The
**ephemeral role** password carries `VALID UNTIL` (§9), so it expires on its own even
if teardown is interrupted. The **project administrator** password has **no expiry**;
it becomes unusable only when the project is deleted, which is why step 2 is the
load-bearing control rather than a tidy-up. The probe never uses the administrator
credential — it connects only as the `CONNECT`-only ephemeral role.

The spike service itself remains, idle. **`wbs-capex-poc` is not touched at any point.**

---

## 12. Evidence checklist

- Service and deployment IDs; bundle SHA-256; exact vendored versions
- Per-step results with timings, per probe (P1 / P2 / P3)
- TLS protocol, cipher, certificate subject / issuer / validity, and **whether pg8000 used the verified context**
- `SELECT 1`, `version()`, `current_user`
- `inet_client_addr()` observations, **each labelled with endpoint type**, and marked non-authoritative where pooled
- **Provider firewall/connection log extracts** giving the source address at the network boundary
- Cold-start versus warm connection latency
- Account/checkout screens captured before creation (§10)
- Zoho ticket reference and **verbatim** answers
- CI run URL
- Teardown confirmation with timestamps
- **Negative results recorded as fully as positive ones.** A failure at step 2 is a more valuable finding than a success at step 5

### Evidence sanitisation — before anything reaches Git

Evidence is committed to a repository. Screenshots and vendor correspondence routinely carry material that
must not live there.

**Never committed:**

- account identifiers, organisation IDs at the provider, or billing references
- email addresses, personal names of support staff, or any personal data
- passwords, connection strings, DSNs, API keys, tokens, or any credential fragment
- database hostnames that embed a project reference usable to reach the instance
- support-ticket content marked confidential, or vendor material whose licence or terms forbid redistribution

**Committed instead:**

- screenshots **cropped and redacted** to the specific field being evidenced, with redaction applied to the
  image itself — never a black rectangle drawn over recoverable pixels, and never a resized original
- Zoho answers **quoted verbatim only where the content is technical**; commercial or confidential passages
  summarised with a note that the full text is held outside the repository
- the ticket **reference number** rather than the ticket body, where the body cannot be sanitised
- host metadata reduced to what the finding requires — region and endpoint *type*, not the reachable hostname

**Rule of thumb:** if a screenshot would let a reader reach the resource, it is not sanitised. When in doubt,
transcribe the finding rather than capture the screen.

---

## 13. Outcome interpretation

| Outcome | Status |
|---|---|
| Reachable; source address **sampled** stable | **Q-A passed. Q-B unresolved.** Sampling cannot establish a static address (§1) |
| Reachable; **authoritative** allowlist commitment or supported private path | **Network-security gate passed for that execution surface** |
| **Stage 1 (AppSail) passed, Stage 2 (Function) untested** | **Phase 0B partial.** The gate has not cleared |
| **Both surfaces pass securely** | **Phase 0B complete.** Architecture proceeds as planned |
| Reachable, but no restriction possible and no authoritative commitment | Q-A passed; Q-B fails. Escalates to a **client security decision** — plan §2.5 option 3, database secured by TLS and credentials alone |
| TCP fails on AppSail, succeeds on Functions | Plan §2.5 option 1 — AppSail becomes a pure request tier, data access moves behind Functions. **Effort impact provisional** (§14) |
| TCP fails on both surfaces | **Go/no-go failure.** Re-plan required. Catalyst Data Store remains rejected on control-semantics grounds (plan §1.1) |

---

## 14. Provisional effort note

Plan v1.2.1 §2.5 estimates the Functions-fronted fallback at **6–12 person-weeks**. That figure is **provisional** and rests on assumptions not yet tested:

- that Functions can reach PostgreSQL at all — itself the Stage 2 question
- that the 30-second Advanced I/O limit accommodates the interactive query path
- that splitting the codebase across two execution surfaces does not force a redesign of the transaction boundary in `services.critical`
- that connection pooling behaves acceptably under scale-to-zero, where every cold start opens a new connection

**It should not be quoted as a committed estimate.** Re-baseline it once Stage 2 has run.

---

## 15. Approval gate

**Nothing has been created.** Approval is requested for:

1. Verifying the Supabase free-tier **entitlement** — specifically whether network restriction and IPv4 addressing are available without a paid add-on — and capturing the account/checkout evidence
2. Creating a throwaway Mumbai `ap-south-1` database and an ephemeral role, hardened per §9
3. Building and deploying a Stage 1 bundle to `wbs-platform-spike`
4. Setting temporary environment variables on that service
5. Raising the Zoho support ticket (§5)

**Requested separately, after Stage 1 reports:** Stage 2, which creates a Function.
**Requested separately if needed:** any temporary public network exposure (§9).

Two decisions would help before starting:

- ~~Provider and region~~ — **decided: Supabase Mumbai `ap-south-1`.**
- **Who raises the Zoho ticket.** Their response time is outside our control and question 3 gates Q-B entirely, so it is the long pole. Worth opening first, in parallel with everything else.
