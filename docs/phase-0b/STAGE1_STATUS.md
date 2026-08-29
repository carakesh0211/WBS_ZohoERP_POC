# Phase 0B Stage 1 — status

**Branch:** `phase-0b/connectivity-gate`
**Status: attempt 2 prepared. Stopped at the fresh-project approval gate.**
**No Supabase project exists. No exposure clock is running.**

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

Verified: project deleted **by reference** (`tlqufzaatqfbyundzqvm`), navigation
by ref so no other project was reachable, confirmation dialog required the name
typed in full; organisation `carakesh` returned to **1 project**; the
pre-existing project was never opened and remains paused; the project host no
longer resolves, which kills the ephemeral role with it; local secret files
deleted; **zero environment variables** on `wbs-platform-spike`.

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

**89 tests pass**, up from 36.

| Proof | Tests |
|---|---|
| The intended CA file is explicitly loaded, and is the sole anchor | 5 |
| Missing / empty / malformed / expired fails closed; refusal does no network I/O | 9 |
| Hostname verification on; `CERT_REQUIRED`; TLS 1.2 or newer | 3 |
| Verification cannot be disabled anywhere (source scan, 3 files x 6 patterns) | 18 |
| No path, filename, certificate body or secret leaks on failure | 7 |
| P1/P2 remain the only keys; port 5432 only; 6543 unreachable (AST, not grep) | 7 |
| Stage-skip attribution is honest | 1 |

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

**One Phase A item is still outstanding:** the vendored Linux x86_64 /
CPython 3.13 dependency tree is not built. It must be, before the approval gate,
so that the in-window build is one file plus a zip rather than dependency
resolution under time pressure.

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
