# Phase 0B Stage 1 — operator runbook (attempt 3)

Every step that touches a secret, and every step that attempt 2 got wrong.

| | Performed by |
|---|---|
| Non-secret configuration, build, verification, evidence review | **Claude** |
| Every secret *value* — generating it, typing it, deleting it | **You** |
| Catalyst ZIP upload (no CLI path available) | **You** |
| Running the probe helper | **You** |
| The two deadline alarms | **You** |

Claude never learns `PGPASSWORD` or `PROBE_TOKEN`.

## Absolute boundaries

- **`praktiq` is out of scope for everyone.** Never opened, inspected, queried,
  resumed, modified, or used as a source for anything — including the CA.
  Claude will not ask you to open it. Verified **only** from the
  organisation-level project listing.
- `wbs-capex-poc` is never accessed or modified.
- Only `wbs-platform-spike` carries the probe.
- No Supabase project is created without explicit per-run approval **and** both
  alarms armed.

---

## What attempt 2 cost, and what is fixed

Attempt 2 produced **no connectivity data**. Both endpoints returned
`404 ENDPOINT_NOT_CONFIGURED`, and the run overran its window by 2 min 18 s.
Root causes, now fixed in code rather than in prose:

| Fault | Fix |
|---|---|
| One `PGUSER` for two endpoints — the pooler needs `<role>.<project_ref>` | `PGUSER_DIRECT` and `PGUSER_POOLER`; an endpoint materialises only if **both** its host and user are set |
| Variables set **after** deployment, so the running instance never saw them | Deploy **last**; `/healthz` now publishes `configured_endpoints`, `endpoint_count` and `configuration_loaded_at` |
| A 404 was written into the evidence files as a result | `ENDPOINT_NOT_CONFIGURED` now **aborts** the helper. A refusal to run is not a result |
| The 45-minute rule depended on Claude remembering to look at a clock | **Two operator alarms**, armed before creation. Claude cannot be the timekeeper — it may be blocked waiting for you |

---

## Phase A — offline preparation, no project, no clock

| | Item | State |
|---|---|---|
| A1 | Probe code, fail-closed CA handling, build gate | done |
| A2 | **120 tests**, green on CI | done |
| A3 | Invocation helper, operator gate, sanitiser self-test | done |
| A4 | Ephemeral-role SQL template | done |
| A5 | Vendored Linux tree, staged outside Git | done |
| A6 | Per-endpoint usernames + `/healthz` configuration proof | done |

`ca-bundle.pem` is **deliberately absent**. Attempt 2's certificate belonged to a
project that no longer exists, and provenance rule 1 requires the anchor to come
from the project actually under test. The build gate therefore fails closed
until you download the new one — the rule enforcing itself.

---

## The approval gate — two things must be true

**1. You approve this specific project creation.**

**2. Both alarms are armed, by you, and you have said so.**

| Alarm | Fires at | Meaning |
|---|---|---|
| **Warning** | creation **+40 min** | Wrap up whatever is in flight. No new work starts. |
| **Mandatory cleanup** | creation **+45 min** | You tell Claude. Cleanup begins **immediately**, whatever is in progress. |

Use a phone timer — anything that rings without you watching it. **Claude must
not be the timekeeper.** Attempt 2 overran precisely because Claude was blocked
waiting on a manual step and nothing independent was watching the clock.

**Project creation is forbidden until you confirm both alarms are armed.**

**60 minutes remains an absolute breach threshold**, not a target.

---

## Phase B — the exposure window

### B1. Create the project — Claude · ~4 min

Fresh Mumbai `ap-south-1`, Free, **Data API disabled**. No trial, no upgrade, no
paid IPv4 add-on, no payment method.

Claude records the **exact project reference immediately**, before anything else,
and re-checks it before every later write or delete.

**You** set the project/database administrator password. High entropy, not
reused. **The probe never uses this credential** — it exists only to create the
role in B3.

### B2. Download the CA — you · ~2 min

**From this new project only:** Database Settings → SSL Configuration →
Download certificate. Save unmodified as `docs/phase-0b/probe/ca-bundle.pem`.

Not from `praktiq`. Not from any other project. Not from a TLS handshake. Not
from a third-party repository.

Claude verifies it and records reference, download time, SHA-256, subject,
issuer and validity into `probe/CA_BUNDLE.md` §3.

### B3. Create the restricted role — you run the SQL · ~3 min

Claude stages `probe_role.template.sql` in a **new query tab**. You replace the
password placeholder (expiry is pre-filled and non-secret) and Run.

Claude verifies **non-secret attributes only**, from its own separate query tab:
`rolcanlogin` true; `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolreplication`,
`rolbypassrls` all false; `rolconnlimit` 5; `rolvaliduntil` ≈ +2 h.

### B4. Configure ALL Catalyst variables — ~7 min · **before any deployment**

Seven. Claude enters five; **you** enter two.

| Variable | Value | Who |
|---|---|---|
| `PGHOST_DIRECT` | `db.<ref>.supabase.co` | Claude |
| `PGHOST_POOLER` | `aws-0-ap-south-1.pooler.supabase.com` | Claude |
| `PGUSER_DIRECT` | `probe_ephemeral` | Claude |
| `PGUSER_POOLER` | `probe_ephemeral.<ref>` | Claude |
| `PGDATABASE` | `postgres` | Claude |
| **`PGPASSWORD`** | the **role** password from B3 | **You** |
| **`PROBE_TOKEN`** | a fresh high-entropy token | **You** |

Claude opens each secret dialog with the key filled and the **value field blank**,
and does not screenshot or read it back.

Keep the token — you type it into the helper at B7.

### B5. Build and deploy — Claude builds, you upload · ~6 min

```bash
py docs/phase-0b/probe/build_bundle.py --out wbs-phase0b-probe.zip --vendor <staging>/vendor
```

Offline, about a second. Then **you** upload via Catalyst → AppSail →
`wbs-platform-spike` → **Create Deployment**: Python 3.13, `python3 -u main.py`,
port 9000, 512 MB, Development.

**Deployment happens after the variables exist.** AppSail binds environment
variables at *instance start*, and a configuration change does not recycle a live
instance. Deploying last is what makes the new instance read them. This single
ordering change is the fix for attempt 2's total failure.

### B6. Verification gate — Claude · ~3 min · **abort on any failure**

- `GET /healthz` → 200
- `ca_bundle_loaded: true`, `ca_bundle_sha256` **equals** the B2 fingerprint
- **`configured_endpoints` contains both `P1` and `P2`**, `endpoint_count` 2
- `configuration_loaded_at` is **after** the B4 variable writes
- unauthenticated `POST /probe` → 401

**Any failure aborts to cleanup (B8).** No iterating on packaging or
configuration under the clock.

### B7. Run P1 and P2 — you · ~3 min

```bash
python docs/phase-0b/probe/probe_invoke.py --base-url https://<service-url> --out docs/phase-0b/evidence
```

Hidden token prompt, or pipe from a password manager with `--token-stdin`.

The helper **re-checks the gate itself** and exits 3 without writing anything if
either endpoint is missing — so attempt 2's artefact cannot reach the evidence
directory even if the earlier gate were skipped.

### B8. Cleanup — starts at your +45 alarm, no exceptions

**Secret-safe order. You go first.**

1. **You delete `PGPASSWORD` and `PROBE_TOKEN`**, and say so.
   Claude does **not** open, inspect or screenshot the environment-variable list
   while either secret exists — the Catalyst list renders values in plain text,
   which is how attempt 2 rendered a token into context.
2. Claude removes the five non-secret variables and confirms the empty state.
3. Claude deletes **only** the recorded throwaway project, **by reference**.
4. **Deletion proof:** the exact reference is absent from the
   **organisation-level listing**, or resolves to not-found. This is the proof.
5. DNS non-resolution is **secondary corroboration only** — records cache or
   persist briefly, so it can never be the sole evidence.
6. `praktiq` confirmed present and paused **from the listing alone**.
7. Timestamps and actual exposure duration recorded.

Cleanup runs even if a probe failed, deployment failed, evidence is incomplete,
automation broke, or P1/P2 timed out. If safe automated deletion becomes
impossible, Claude stops everything and gives you the exact project name and
reference for manual deletion, and never risks another project.

### B9. Restore and record — Claude, plus one upload from you

Restore `wbs-platform-spike` to the plain FastAPI bundle
(`wbs-platform-spike-vendored.zip`, 3,214,269 bytes — **not** the 1.4 KB
un-vendored one, which is the build that 503'd). Commit sanitised evidence and
the CA provenance record, push, verify CI.

---

## Indicative timing

| Minute | Step |
|---|---|
| 0–4 | B1 create, record reference |
| 4–6 | B2 you download the CA |
| 6–9 | B3 role |
| 9–16 | B4 **all** variables |
| 16–22 | B5 build, you upload |
| 22–25 | B6 verification gate — **abort here on any mismatch** |
| 25–28 | B7 P1 and P2 |
| 28–40 | slack |
| **40** | **your warning alarm** |
| **45** | **your cleanup alarm — teardown begins** |
| 60 | absolute breach threshold |

---

## After you have used them

- **`PGPASSWORD`** — the *ephemeral role* password. Carries `VALID UNTIL` ≈ +2 h,
  so it expires on its own even if teardown fails, and dies with the project.
- **The project/database administrator password** — **no expiry, and must never
  be described as having one.** It becomes unusable only when the project is
  deleted, which is why deletion is the load-bearing control. **The probe never
  uses it.**
- **`PROBE_TOKEN`** — dead once you delete the variable.

---

## What Claude will refuse

- Typing any secret value into any field, form or command
- Reading back, screenshotting or listing environment variables while a secret
  value exists among them
- Running `probe_invoke.py`, which would require holding the token
- Opening, querying or modifying `praktiq` or any pre-existing resource — for
  the certificate or anything else — or asking you to
- Touching `wbs-capex-poc`
- Sourcing the CA from a TLS handshake, a third-party repository, or any project
  other than the one under test
- Creating the project before you approve **and** confirm both alarms armed
- Treating `ENDPOINT_NOT_CONFIGURED` as evidence
- Sending the Zoho support ticket
