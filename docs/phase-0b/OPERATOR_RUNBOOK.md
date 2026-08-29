# Phase 0B Stage 1 — operator runbook

Every step that touches a secret. **No secret value in this document, in any
commit, in any automation call, or in any transcript.**

The division of labour is the whole point:

| | Performed by |
|---|---|
| Non-secret configuration, build, deployment, evidence review | **Claude** |
| Every secret *value* — generating it, typing it, using it | **You** |

Claude never learns `PGPASSWORD` or `PROBE_TOKEN`. That is not a courtesy; it is
what makes the earlier stall unnecessary rather than merely deferred.

---

## Phase A — before any Supabase project exists (no exposure clock)

### A1. Download the CA certificate — **the current blocker**

1. Supabase dashboard → any project you own → **Database Settings → SSL Configuration**
2. Download `prod-ca-2021.crt`
3. Save it **unmodified** as `docs/phase-0b/probe/ca-bundle.pem`
4. Tell Claude it is in place

It is Supabase's **shared production root**, identical for every project, so it
does not require the throwaway project to exist. Doing it now keeps packaging
out of the exposure window entirely.

*Boundary: Claude will not open `praktiq` to fetch this. If you take the file
from that project's settings page that is your call as its owner — the file is a
public root certificate and carries no project data, credential or connection
string.*

### A2. Claude builds, you upload, Claude verifies

Claude runs the gate and produces the ZIP, then hands you the path. Catalyst has
no CLI upload here, so **you** upload it through the Console. Claude then
confirms:

- `GET /healthz` → 200, `ca_bundle_loaded: true`, and the CA fingerprint
- `POST /probe` without a token → 401
- `POST /probe` with a token → **503 `CA_BUNDLE_UNUSABLE`** if the CA failed to
  ship, which is the packaging proof we did not have last time
- no endpoint variables set, so the service is structurally inert

**All of Phase A happens with no database in existence.** If the CA does not
load on AppSail, we find out here, for free.

---

## Phase B — requires your explicit approval to begin

Creating the throwaway project starts the 60-minute exposure clock.
**Claude will not create it until you say so.**

### B1. Project creation — Claude

New project, Mumbai `ap-south-1`, Free, Data API disabled. Claude records the
project reference immediately and re-verifies it before every subsequent write
or delete.

**You** set the project's own database password at creation. Choose a
high-entropy value. Do not share it, do not paste it into chat, and do not
reuse it anywhere.

### B2. Ephemeral role — you run the SQL

Claude stages `probe_role.template.sql` with placeholders. **You** replace the
two placeholders in the Supabase SQL Editor and run it. Claude does not read the
editor contents afterwards.

Replace exactly:

- `<<<REPLACE-WITH-HIGH-ENTROPY-PASSWORD>>>`
- `<<<REPLACE-WITH-UTC-TIMESTAMP-2H-FROM-NOW>>>` — e.g. `2026-08-29 09:30:00+00`

Claude then verifies the role **by attribute only** — `rolcanlogin`,
`rolsuper`, `rolvaliduntil`, `rolconnlimit` — never by reading a secret.

### B3. Catalyst environment variables

Six variables. Claude stages the four that are not secrets; **you** type the two
that are.

| Variable | Who enters it |
|---|---|
| `PGHOST_DIRECT` | Claude |
| `PGHOST_POOLER` | Claude |
| `PGUSER` | Claude |
| `PGDATABASE` | Claude |
| **`PGPASSWORD`** | **You** — the role password from B2 |
| **`PROBE_TOKEN`** | **You** — a fresh high-entropy value you generate now |

Claude will open each dialog, fill the non-secret fields, and **stop with the
value field blank and focused** for the two secret rows. Claude does not
screenshot, read back, or otherwise inspect those fields after you type them.

Omitting a host variable simply removes that endpoint from the table — the probe
cannot be asked for an endpoint it was never configured with.

### B4. You run the probe

**You** run this, in your own terminal, not Claude:

```bash
python docs/phase-0b/probe/probe_invoke.py --base-url https://<service-url> --out docs/phase-0b/evidence
```

It prompts for `X-Probe-Token` with the input hidden. Or pipe it from a password
manager, which keeps it out of shell history too:

```bash
pass show wbs/probe-token | python docs/phase-0b/probe/probe_invoke.py --base-url https://<service-url> --out docs/phase-0b/evidence --token-stdin
```

Claude does not run this command, because running it would mean holding the
token. The helper writes **sanitised** evidence only — Claude reads those files.

### B5. Teardown — Claude, and non-negotiable

In order, verifying the recorded project reference before each destructive step:

1. Remove all six Catalyst environment variables
2. Delete **only** the recorded throwaway project, **by reference**
3. Confirm its host no longer resolves — this kills the role with the database
4. Confirm `praktiq` is untouched and still paused
5. Confirm zero environment variables remain
6. Record created / deleted timestamps and the actual exposure duration

Teardown happens **even if** a probe fails, the deployment fails, evidence is
incomplete, browser automation stops working, or P1/P2 time out. If safe
automated cleanup becomes impossible, Claude stops and gives you the exact
project name and reference for manual deletion, and never risks another project.

### B6. Restore and record — Claude

Restore `wbs-platform-spike` to the plain FastAPI bundle, commit the sanitised
evidence, push, verify CI.

---

## After you have used them

- **`PROBE_TOKEN`** — dead once the Catalyst variable is removed (B5.1).
- **`PGPASSWORD` and the project password** — dead once the project is deleted
  (B5.2). Both were `VALID UNTIL` a two-hour horizon regardless.
- Neither should be stored anywhere afterwards.

---

## What Claude will refuse

- Typing any secret value into any field, form or command
- Reading back a secret field after you have filled it
- Running `probe_invoke.py`, because that requires holding the token
- Opening, querying or modifying `praktiq` or any pre-existing resource
- Touching `wbs-capex-poc`
- Creating the Supabase project before you explicitly approve it
- Sending the Zoho support ticket
