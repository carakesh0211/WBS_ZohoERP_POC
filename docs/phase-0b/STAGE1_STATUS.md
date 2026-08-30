# Phase 0B Stage 1 — status

**Branch:** `phase-0b/connectivity-gate`
**Status: attempt 2 EXECUTED 2026-08-30. Q-A NOT ANSWERED. Window overran by 2 min 18 s.**
**All resources destroyed. No Supabase project exists.**

The CA certificate is **not** a separate blocker: under the approved provenance
rule its only legitimate source is the throwaway project itself, so it is
obtained inside the window, immediately after creation.

| | |
|---|---|
| **Q-A** — can AppSail reach an external PostgreSQL on 5432? | **NOT ANSWERED.** Empirical; answered by deploying the probe |
| **Q-B** — can that path be secured by network controls? | **UNRESOLVED.** Authoritative; needs Zoho question 3, a private path, or client sign-off |

---

## Correction of record: what gates what

Attempt 1's write-up claimed Zoho support **question 6** — may a custom CA
bundle ship inside a deployment bundle — gated the retry. **That was wrong, and
it is corrected here rather than quietly edited away.**

AppSail has already accepted and executed from a deployment ZIP containing
vendored files, twice: the Phase 0A spike and the first Stage 1 deployment.
Whether a `.pem` alongside `main.py` loads is therefore an **empirically
testable property of the deployment**, and Stage 1 tests it by deploying it.

The general principle, now written into `TEST_PLAN.md` section 1:

- **Q-A is empirical.** It is measured. **No support question gates it.**
- **Q-B is authoritative.** Question 3 gates it, and always did.
- The unsent Zoho ticket proceeds **independently** and must never block Q-A.

---

## Attempt 1 — preserved as audit history

Recorded because how a failure was handled is itself evidence.

**P1 and P2 were never run.** Two reasons:

1. **Setting `PGPASSWORD` through the Catalyst Console dialog would have written
   the password into the session transcript.** Catalyst environment variables
   are set through a browser dialog; there is no way to type a value into that
   field without it appearing verbatim in the automation call. No partial
   connectivity result justified breaching that, so **no environment variables
   were ever created.**
2. **The bundle shipped no `ca-bundle.pem`**, and the code silently degraded to
   the system trust store when the file was absent — `if os.path.isfile(bundle):`
   guarded the `load_verify_locations` call, so an absent file meant system CAs.
   Supabase presents its own CA, so the omission surfaced as
   `SSLCertVerificationError` against the live endpoint — indistinguishable from
   a Catalyst egress restriction or a network fault. Reproduced identically from
   a local machine against all three endpoints, which is how the cause was
   isolated to **our packaging**, not the platform.

**What attempt 1 did prove on the live deployment**

| Evidence | Result |
|---|---|
| Probe runs on AppSail | `GET /healthz` returned 200, Python **3.13.9 / x86_64**, cold start **2593 ms** |
| Authorisation precedes network activity | `POST /probe` returned **401** without a token, on the deployed build |
| Catalyst executes from a vendored ZIP | Confirmed — the basis for the Q6 correction above |

**Exposure and cleanup**

| | |
|---|---|
| Project created | 2026-08-29 **00:13:10** local |
| Project deleted | 2026-08-29 **00:27:29** local |
| **Actual exposure** | **14 min 19 s** of an authorised 60 min |
| Database contents throughout | Empty — no schema, no data, no Data API |

Verified, with the control-plane check as the authority: project deleted **by
reference** (`tlqufzaatqfbyundzqvm`), navigation by ref so no other project was
reachable, confirmation dialog required the name typed in full; **the
organisation-level listing for `carakesh` returned to 1 project, that reference
absent** — this is the deletion proof. The project host also stopped resolving,
recorded as **secondary corroboration only**: DNS can cache or persist briefly,
so it can never be the sole evidence. The pre-existing project was never opened,
and its untouched, paused state was read from the organisation listing alone.
Local secret files deleted; **zero environment variables** on
`wbs-platform-spike`.

`wbs-capex-poc` was not accessed, requested, modified, redeployed or inspected
at any point, in either attempt.

---

## Attempt 2 — what has been corrected

### Packaging fails closed

| Change | File |
|---|---|
| CA handling extracted to a **stdlib-only** module so the build gate and the runtime share one implementation and cannot drift | `probe/ca.py` |
| Missing / empty / malformed / expired CA raises `CaBundleUnusable` — **never** a fallback to system CAs | `probe/ca.py` |
| The pinned bundle is the **only** trust anchor — passing `cafile` to `create_default_context` suppresses `load_default_certs()` | `probe/ca.py` |
| `/probe` returns **503 `CA_BUNDLE_UNUSABLE`** — after the 401, before any DNS | `probe/main.py` |
| `/healthz` reports CA state, so a packaging fault is visible **before** a window is spent on it | `probe/main.py` |
| Build gate refuses to produce a bundle without a valid CA, and re-opens the finished ZIP to verify it | `probe/build_bundle.py` |
| CA provenance — source, SHA-256, subject, issuer, expiry | `probe/CA_BUNDLE.md` |

### A real attribution defect, found in evidence output

Stages after a failure were labelled `skipped_deadline` even when the deadline
had not expired — a DNS failure produced four misleading "out of time" verdicts.
Corrected to `skipped_upstream_failure`. Found by reading actual helper output,
not by review. One pre-existing test asserted the wrong label and was
**tightened, not weakened**; the change is annotated in place.

### Secret handling, solved before any database exists

`probe/probe_invoke.py`:

- reads the token via `getpass` (unechoed) or `--token-stdin` (for piping from a
  password manager) — **never** an argv element, so `ps` and shell history
  cannot see it;
- holds the token only as an outbound header, never writes it anywhere;
- sanitises every response in **three layers** — known secret values stripped by
  literal match (including inside free prose), key-name matching, then
  value-shape matching for addresses and hostnames;
- preserves `inet_client_addr`, which is Q-B evidence, while redacting target
  addresses, which are not.

The self-test caught a genuine gap on first run: a secret embedded in free text
survived key-name redaction. Value-based stripping was added in response.

### Test coverage

**92 tests pass**, up from 36.

| Proof | Tests |
|---|---|
| The intended CA file is explicitly loaded, and is the sole anchor | 5 |
| Missing / empty / malformed / expired fails closed; refusal does no network I/O | 9 |
| Hostname verification on; `CERT_REQUIRED`; TLS 1.2 or newer | 3 |
| Verification cannot be disabled anywhere (source scan, 3 files x 6 patterns) | 18 |
| No path, filename, certificate body or secret leaks on failure | 7 |
| P1/P2 remain the only keys; port 5432 only; 6543 unreachable (AST, not grep) | 7 |
| Stage-skip attribution is honest | 1 |
| The in-window build is offline; vendoring is a separate program | 3 |

---

## Second correction of record: CA provenance

An earlier revision of this document claimed the certificate is **"not
project-specific — Supabase's shared production root"**, and concluded it could
be fetched from any project in advance of the test.

**Withdrawn. That was an inference from a filename, not a documented fact.**
Supabase's documentation says the certificate is downloaded from Database
Settings for *your database*; it nowhere states the file is identical across
projects. A shared-looking filename is not evidence that it is shared. This is
the same error class as reading "no published static egress IPs" as "no static
egress IPs" — absence of a contrary statement treated as a guarantee.

The practical consequence was worse than the wording: it would have sourced the
probe's trust anchor from a project that is out of scope.

**Approved provenance rule, now binding:**

1. The CA comes from **the exact newly created throwaway project** used for the test
2. Downloaded from **that project's** Database Settings → SSL Configuration
3. The **project reference is recorded before** the download
4. SHA-256, subject, issuer, validity and download time all recorded
5. **Never** from a TLS handshake or a third-party repository
6. **`praktiq` is never opened or inspected**, by anyone, for this or anything else

## The remaining blocker

**Not the certificate — the approval gate.**

`ca-bundle.pem` cannot exist yet, because under rule 1 its only legitimate
source is a project that has not been created. The build gate correctly refuses
to build without it:

```
BUILD FAILED: CA bundle gate failed: ca_bundle_missing.
```

That is the gate working as designed, not a setback.

**What this costs.** Packaging now happens *inside* the exposure window — create
project, download its CA, build, upload, health-check — where it would otherwise
have been finished in advance. The accepted consequence: **if `/healthz` reports
`ca_bundle_loaded: false`, or a fingerprint that does not match the one recorded
at download, the run aborts to cleanup.** There is no iterating on packaging
under the clock. Provenance integrity is worth that, and the risk is small
because everything except the certificate is already proven.

Sequence, timings and the minute-45 cleanup trigger are in
`OPERATOR_RUNBOOK.md`; provenance detail in `probe/CA_BUNDLE.md`.

**Phase A is now complete.** See the vendoring section below.

---

## Unchanged

- **Q-B is UNRESOLVED**, and no work here changes that. Sampled address
  stability remains a hypothesis.
- Stage 1 success is a **partial pass**. Phase 0B does not clear until Stage 2
  proves the Cron/Event Function path.
- The Zoho support ticket at `ZOHO_SUPPORT_TICKET_DRAFT.md` remains **unsent**
  and is a user action. It proceeds independently of Q-A.
- `wbs-platform-spike` still carries the attempt-1 probe, **structurally inert**:
  no `PGHOST_*` variables means the endpoint table is empty, so P1 and P2 do not
  exist as reachable keys, and an unset `PROBE_TOKEN` fails closed. It will be
  replaced by the corrected bundle rather than restored first, to avoid a
  needless extra upload.

---

## Phase A6 — vendored dependency tree

Staged **outside the repository** and never committed: `vendor_deps.py` refuses
a `--dest` inside the repo. The reproducible form is
`dependency-inventory.json` plus that script.

| | |
|---|---|
| Packages | **18** |
| Files staged | **475** |
| Staged size | **11,140,237 bytes** (10.62 MiB) |
| Inventory SHA-256 | `f7081f7cc7cb2195a1124b2fdedb961cc598ab6b540684ebd327c25fe0a7e2ff` |
| Tree SHA-256 | `fffcabd61dcbf09f51334212bea59ae70f29d7a4b3d7feba50526e643a543e40` |
| Shared objects | exactly one — `pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so` |
| Windows binaries | none |
| OSV.dev scan | **no known vulnerabilities**, 18/18 scanned |

### A stale inventory entry, found by resolving rather than trusting

The script resolves the closure from four top-level pins and asserts the result
equals the inventory. It immediately found that **`sniffio` was listed but is
not in the closure**: `anyio` 4.14.2 imports it under
`try/except ModuleNotFoundError`, with every call site handling `sniffio is None`,
so it is genuinely optional and correctly absent from `Requires-Dist`.

The attempt-1 bundle had therefore been shipping a package nothing depends on.
Entry removed. This is exactly what downloading the inventory directly would
never have caught — that would only prove the inventory is downloadable.

### A defect caught by rehearsing the build

A rehearsal build using the **synthetic** CA fixture, in a temporary copy
outside the repository, exposed a real bug in the new out-of-tree vendor
support: archive names were computed relative to the probe directory, so an
external tree produced `../`-prefixed names and the archive had no `vendor/`
prefix. The gate caught it as `vendored module absent: fastapi` — inside the
window that would have cost the run. Fixed; re-rehearsed clean at **480 files,
3.52 MB, ~1.1 s**. The rehearsal artefact was deleted immediately and no
deployable ZIP exists.

### The gate was not bypassed

`build_bundle.py` still exits **1** with `ca_bundle_missing` even when handed a
complete, verified vendor tree, and produces **no ZIP**. The only missing input
is the throwaway project's own certificate.

---

# Attempt 2 — executed 2026-08-30

## Outcome, stated first

**Q-A is NOT ANSWERED.** P1 and P2 both returned
`404 ENDPOINT_NOT_CONFIGURED`. **No DNS, TCP, TLS, authentication or query stage
ever ran against the database.** There is zero connectivity data. Nothing about
Catalyst egress to PostgreSQL was established.

**Q-B remains UNRESOLVED**, as it must.

## Two failures, both mine

### 1. The environment variables never reached the running instance

AppSail binds environment variables **at instance start**. The instance was
already warm from the 21:10 health check, and changing configuration does not
recycle a running instance — a **new deployment** (or a cold start after
scale-down) is required for new variables to be read.

I set the six variables and went straight to running the probe. The container
answered from its existing process, where `_endpoints()` had been evaluated at
import with no `PGHOST_*` present — so the endpoint table was empty and every
request was correctly refused with 404.

The probe behaved exactly as designed. The operator sequence was wrong.

**Fix for attempt 3:** set the environment variables **before** the deployment
upload, or force a fresh instance after setting them and re-verify via
`/healthz` before invoking. A cheap positive check is available and was not
used: with variables loaded, an authorised `POST /probe` returns a stage result
rather than `ENDPOINT_NOT_CONFIGURED`.

### 2. The exposure window overran

| | |
|---|---|
| Created | **20:55:22** |
| Cleanup should have begun | **21:40:22** |
| Cleanup actually began | **~21:56:30** |
| Project deleted | **21:57:40** |
| **Actual exposure** | **62 min 18 s** against an authorised **60 min** |

**This is a breach of the control, not a rounding error.** Between 21:25 and
21:56 I was blocked waiting on a manual step and checked the clock only when I
happened to run a command. I had no timer. The minute-45 rule was written into
the runbook and then not enforced by anything.

**Fix for attempt 3:** arm an actual background alarm at creation +40 min that
fires regardless of what else is happening. A deadline that depends on
remembering to look at a clock is not a control.

## What was genuinely established

These are real results and they were not free.

| # | Finding | Evidence |
|---|---|---|
| 1 | **Catalyst AppSail loads a custom CA bundle shipped inside the deployment ZIP** | `/healthz` → `ca_bundle_loaded: true`, `ca_bundle_sha256` equal to the fingerprint recorded at download |
| 2 | This **empirically answers Zoho support question 6** | No support ticket was needed. The earlier claim that Q6 gated the retry was wrong, and is now disproved by observation |
| 3 | The fail-closed CA design works on the platform, not just in tests | Deployed build reports the pinned anchor and refuses unauthenticated calls |
| 4 | The operator-assisted secret flow works | `PGPASSWORD` and `PROBE_TOKEN` were entered by the operator; neither ever reached an automation call, log, evidence file or transcript |
| 5 | **Supabase Free offers network restrictions** — "Add restriction" / "Restrict all access" | Database Settings of the throwaway project. The plan had this as unverified |
| 6 | **Direct connections are IPv6 by default**; IPv4 is a paid add-on | Stated on the project's own Connect dialog. Not enabled |
| 7 | **The session pooler requires a `<role>.<project_ref>` username** | Connect dialog. The probe reads a single `PGUSER`, so P1 and P2 cannot run in one pass — a real design gap |
| 8 | Enabling server-side SSL enforcement requires a **database restart with minutes of downtime** | Confirmation dialog. Declined inside a timed window; recorded as a deliberate deviation from plan section 9 |

Finding 7 needs a code change before attempt 3: either a second user variable,
or per-endpoint user derivation.

## Cleanup — complete and verified

| Step | Result |
|---|---|
| All six Catalyst environment variables removed | **Done** — panel back to empty state |
| Supabase project deleted **by recorded reference** `lttaytjmkqsfhgyskamx` | **Done** at 21:57:40, name typed in full to confirm |
| **Control-plane proof** | Navigating to that reference no longer resolves to a project; organisation listing shows **1 project** |
| DNS | Host **still resolved** shortly after deletion — recorded as **secondary evidence only**, exactly why it is never the proof |
| `praktiq` | Present and paused, read **from the organisation listing alone**. Never opened, by anyone, at any point |
| Probe inertness | `/healthz` 200; unauthenticated `POST /probe` → 401; no endpoint variables configured |
| `wbs-capex-poc` | Never accessed, requested, modified, redeployed or inspected |

**Outstanding:** `wbs-platform-spike` still carries the probe build. It is
structurally inert — no `PGHOST_*` means the endpoint table is empty, and an
unset `PROBE_TOKEN` fails closed — but restoring the plain FastAPI bundle needs
a manual ZIP upload by the operator.

## Disclosure

The Catalyst environment-variable list renders values in plain text. A
screenshot taken while deleting the variables therefore rendered the
`PROBE_TOKEN` value into the assistant's context. It was **not transcribed, not
written to any file, and not committed**, and the variable has since been
deleted, so the token is dead. Recorded because a near-miss on a secret-handling
control is worth recording. **Fix for attempt 3:** delete secret-valued
variables using DOM references only, never a screenshot of the list.

## Artefacts

| | |
|---|---|
| CA | `Supabase Root 2021 CA`, sha256 `700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7`, valid to 2031-04-26 |
| Bundle | sha256 `51160f270898b67f2c453380f12baccd8eaf6145583964bf151bacdd1d2d62ef`, 480 files, 3,694,808 bytes |
| Evidence | `evidence/P1-*.json`, `evidence/P2-*.json`, `evidence/summary-*.json` — sanitised, both 404 |
