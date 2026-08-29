# Phase 0B Stage 1 — operator runbook

Every step that touches a secret. **No secret value in this document, in any
commit, in any automation call, or in any transcript.**

The division of labour is the whole point:

| | Performed by |
|---|---|
| Non-secret configuration, build, deployment checks, evidence review | **Claude** |
| Every secret *value* — generating it, typing it, using it | **You** |
| Catalyst ZIP upload (no CLI path available) | **You** |

Claude never learns `PGPASSWORD` or `PROBE_TOKEN`. That is not a courtesy; it is
what makes the earlier stall unnecessary rather than merely deferred.

## Absolute boundaries

- **`praktiq` is out of scope for everyone.** It is never opened, inspected,
  queried, resumed, modified or used as a source for anything — including the CA
  certificate. Claude will not ask you to open it.
- `wbs-capex-poc` is never accessed or modified.
- Only `wbs-platform-spike` carries the probe.
- No Supabase project is created without explicit per-run approval.

---

## Phase A — preparation, no Supabase project, no exposure clock

Everything that can be finished before the clock starts, is.

| | Item | State |
|---|---|---|
| A1 | Probe code, fail-closed CA handling, build gate | **done** |
| A2 | 89 tests, green on CI (Linux, Python 3.11) | **done** |
| A3 | Invocation helper + sanitiser self-test | **done**, tested with dummy values |
| A4 | Ephemeral-role SQL template with placeholders | **done** |
| A5 | Upload instructions | **done** — `probe/BUILD.md` |
| A6 | Dependency vendoring / build directory | **outstanding** — see below |

**A6 must be finished before the approval gate.** The vendored Linux
x86_64 / CPython 3.13 tree is what makes the in-window build a matter of adding
one file and zipping, rather than resolving dependencies under time pressure.

The certificate is **not** part of Phase A. Under the approved provenance rule it
can only come from the throwaway project, which does not exist yet.

---

## The approval gate

**Claude stops here.** Creating the project starts the exposure clock and
requires your explicit approval for that specific creation.

---

## Phase B — the exposure window

**Hard maximum 60 minutes. Cleanup begins no later than minute 45, even if
testing is incomplete.** An incomplete result is a finding; an overrun is not
acceptable.

### The consequence of sourcing the CA correctly, stated plainly

Packaging now happens **inside** the window: create project → download its CA →
build → upload → health-check. Previously I had proposed doing this in advance,
which was wrong for the reason recorded in `probe/CA_BUNDLE.md`.

So there is a real risk this buys: **if `/healthz` reports
`ca_bundle_loaded: false`, or a fingerprint that does not match the one recorded
at download, we abort and clean up.** There is no time to iterate on packaging
under the clock, and attempting to would be how a 60-minute cap becomes 90. The
run is then re-attempted with the defect fixed. This is the correct trade —
provenance integrity over convenience — and it is cheap because everything
except the certificate is already proven.

### B1. Create the project — Claude · ~4 min

Fresh Mumbai `ap-south-1`, Free plan, **Data API disabled**. No trial, no
upgrade, no paid IPv4 add-on, no payment method.

Claude **records the exact project reference immediately** — before anything
else, per provenance rule 3 — and re-verifies it against that record before
every subsequent write or delete.

**You** set the project's database password at creation. High entropy. Do not
share it, do not paste it into chat, do not reuse it.

### B2. Download the CA — you · ~2 min

**From this new project only:**

Dashboard → **the newly created project** → **Database Settings → SSL
Configuration** → download the certificate.

Save it unmodified as `docs/phase-0b/probe/ca-bundle.pem` and tell Claude.

Not from `praktiq`. Not from any other project. Not from a TLS handshake. Not
from a third-party repository.

### B3. Build and verify — Claude · ~2 min

```bash
python docs/phase-0b/probe/build_bundle.py --out wbs-phase0b-probe.zip
```

Fails closed if the certificate is missing, empty, malformed, expired, or absent
from the finished archive. Claude records SHA-256, subject, issuer, validity and
download time into `probe/CA_BUNDLE.md` §3, against the recorded project
reference.

### B4. Upload — you · ~4 min

Catalyst Console → AppSail → `wbs-platform-spike` → Create Deployment. Python
3.13, `python3 -u main.py`, port 9000, 512 MB, **Development only**. There is no
CLI upload path here, which is why this step is yours.

### B5. Health verification — Claude · ~4 min · **gate**

Before any role exists and before any database variable is set:

- `GET /healthz` → 200, `ca_bundle_loaded: true`
- `ca_bundle_sha256` **equals** the fingerprint recorded at B3
- `POST /probe` without a token → 401

**If the fingerprint does not match, or the bundle did not load: abort to
cleanup (B9).** No environment variables, no role, no credentials.

### B6. Ephemeral role — you run the SQL · ~3 min

Claude stages `probe_role.template.sql`. **You** replace the two placeholders in
the SQL Editor of the new project and run it. Claude does not read the editor
contents afterwards.

Replace exactly:

- `<<<REPLACE-WITH-HIGH-ENTROPY-PASSWORD>>>`
- `<<<REPLACE-WITH-UTC-TIMESTAMP-2H-FROM-NOW>>>`

Open a **new query tab** before pasting — a previous attempt hit a merged
statement because Ctrl+A did not select inside the editor.

Claude then verifies the role **by attribute only** — `rolcanlogin`, `rolsuper`,
`rolvaliduntil`, `rolconnlimit` — never by reading a secret.

### B7. Environment variables — ~6 min

Six. Claude enters the four that are not secrets; **you** type the two that are.

| Variable | Who enters it |
|---|---|
| `PGHOST_DIRECT` | Claude |
| `PGHOST_POOLER` | Claude |
| `PGUSER` | Claude |
| `PGDATABASE` | Claude |
| **`PGPASSWORD`** | **You** — the role password from B6 |
| **`PROBE_TOKEN`** | **You** — a fresh high-entropy value you generate now |

Claude opens each dialog, fills the non-secret fields, and **stops with the
value field blank** for the two secret rows. Claude does not screenshot, read
back or otherwise inspect those fields afterwards.

Omitting a host variable simply removes that endpoint from the table — the probe
cannot be asked for an endpoint it was never configured with.

### B8. Run P1 and P2 — you · ~3 min

**You** run this, in your own terminal. Claude does not, because running it would
mean holding the token:

```bash
python docs/phase-0b/probe/probe_invoke.py --base-url https://<service-url> --out docs/phase-0b/evidence
```

The token prompt is hidden. Or pipe it from a password manager, which keeps it
out of shell history too:

```bash
pass show wbs/probe-token | python docs/phase-0b/probe/probe_invoke.py --base-url https://<service-url> --out docs/phase-0b/evidence --token-stdin
```

The helper writes **sanitised** evidence only. Claude reads those files.

### B9. Cleanup — Claude · begins by **minute 45**, without exception

Triggered by the clock, not by completion. It runs even if a probe failed, the
deployment failed, evidence is incomplete, browser automation stopped working,
or P1/P2 timed out.

In order, re-verifying the recorded project reference before each destructive
step:

1. Remove **all six** Catalyst environment variables
2. Delete **only** the recorded throwaway project, **by reference**
3. Confirm its host no longer resolves — this kills the role with the database
4. Confirm `praktiq` is untouched and still paused
5. Confirm zero environment variables remain
6. Record created / deleted timestamps and actual exposure duration

If safe automated cleanup becomes impossible, Claude stops and gives you the
exact project name and reference for manual deletion, and never risks another
project.

### B10. Restore and record — Claude

Restore `wbs-platform-spike` to the plain FastAPI bundle, commit the sanitised
evidence and the CA provenance, push, verify CI.

---

## Indicative timing

| Minute | Step |
|---|---|
| 0–4 | B1 create project, record reference |
| 4–6 | B2 you download the CA |
| 6–8 | B3 build and verify |
| 8–12 | B4 you upload |
| 12–16 | B5 health gate — **abort here if the fingerprint is wrong** |
| 16–19 | B6 role |
| 19–25 | B7 environment variables |
| 25–28 | B8 P1 and P2 |
| 28–45 | slack for retries within scope |
| **45** | **B9 cleanup begins regardless** |
| ~55 | B10 restore, commit, CI |

Expected finish well inside 60. The slack is deliberate: it absorbs a slow
provisioning or a slow deployment without ever touching the cap.

---

## After you have used them

- **`PROBE_TOKEN`** — dead once the Catalyst variable is removed (B9.1)
- **`PGPASSWORD` and the project password** — dead once the project is deleted
  (B9.2); both were `VALID UNTIL` a two-hour horizon regardless

Neither should be stored anywhere afterwards.

---

## What Claude will refuse

- Typing any secret value into any field, form or command
- Reading back a secret field after you have filled it
- Running `probe_invoke.py`, because that requires holding the token
- Opening, querying or modifying `praktiq` or any pre-existing resource — for
  the certificate or for anything else
- Asking you to open `praktiq`
- Touching `wbs-capex-poc`
- Sourcing the CA from a TLS handshake or a third-party repository
- Creating the Supabase project before you explicitly approve it
- Continuing past minute 45 without starting cleanup
- Sending the Zoho support ticket
