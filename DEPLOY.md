# Showing the POC to Atha Group

Four ways to demonstrate, from least to most exposed. Pick the lowest one that meets the need.

> **Read this first.** This block previously said two things, and both were wrong in
> opposite directions.
>
> **"The POC has no user model" — false since Wave 3.** There is one: server-side
> sessions, thirteen roles, a permission table, row-level data scope, and maker-checker
> that refuses self-approval. Every screen and every API route requires a sign-in.
>
> **"`DEMO_USER` and `DEMO_PASSWORD` … demands a username and password" — never true.**
> Those two variables are read at exactly one place, `app/run.py`, to decide whether to
> print a warning. `grep` finds neither name anywhere in `app/backend/`. No middleware,
> dependency or route consults them. **Setting them changes no behaviour at all.**
>
> **What actually protects a deployment, and why it is not enough for a public URL.**
> The sign-in is real, but in the `local-demo` profile the only accounts that exist are
> seeded development identities whose passwords are the user id followed by `!demo` —
> `U-ADM!demo`, and so on. The sign-in screen lists the user ids, so the passwords are
> guessable by anyone who loads the page.
>
> **Therefore: do not put this on a public URL.** Not because it is ungated, but because
> its gate is a known credential scheme. Option 1 remains the recommendation; anything
> beyond it needs a real access control in front of the application, and that control
> does not exist in this repository today.

---

## Option 1 — Screen share (recommended for the first meeting)

Nothing is exposed, nothing can be clicked by anyone else, and you control the pace.

```bash
python app/run.py
```

Open <http://127.0.0.1:8000> and share your screen. To reset the data between runs, rebuild the
database as its own explicit step first, then start the app:

```bash
python -m app.backend.migrate --db app/data/capex.db --fresh --seed
python app/run.py
```

> **Why two commands, not one.** `app/run.py` used to rebuild the database itself whenever the file
> was missing or you passed `--reseed`. It no longer does — see *"This process never migrates
> itself"* below. Rebuilding is now always a command you run on purpose.

**Suggested demo path (about 12 minutes)**

| # | Screen | Point to make |
|---|---|---|
| 1 | Executive Dashboard | Budget, commitment, actual and available for the whole portfolio |
| 2 | WBS Explorer — expand Plant & Machinery | Figures roll up from work package to project |
| 3 | Budget Availability Check — propose ₹20,00,000 on CAPEX-2026-001.03 | It fails, states the shortfall of ₹90,000, and offers the route forward |
| 4 | Purchase Requests — create one over budget | It routes to exception approval rather than being blocked |
| 5 | Approve it without a reason | Refused. Add a reason; it is written to the audit trail |
| 6 | Commitment Reconciliation | Billed + open commitment = ordered, on every line |
| 7 | Commitments — cancel a PO | The released commitment returns to available budget |
| 8 | Capitalisation | Approval refused until allocations equal the CWIP balance |
| 9 | Zoho ERP Connector — run connectivity tests | Real endpoints, real scopes, honest limitations |
| 10 | Audit Trail | Everything just done is recorded |

---

## Option 2 — Temporary public link (client joins remotely, same day)

A tunnel gives a public HTTPS URL that points at your machine. It lasts as long as the command runs.

```bash
rem DEMO_USER / DEMO_PASSWORD are deliberately NOT set here: nothing reads
rem them, so setting them protects nothing. See the header of this file.
python app/run.py --public
```

Then in a second terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

Cloudflare prints a `https://<random>.trycloudflare.com` address. Send that plus the username and
password **through separate channels** — the link in email, the password by phone or WhatsApp.

Install once with `winget install --id Cloudflare.cloudflared`. `ngrok http 8000` works the same way
if you already have an ngrok account.

Close the terminal and the link dies. That is the point: it cannot be forgotten and left running.

---

## Option 3 — Hosted demo URL (client reviews in their own time, over days)

Use this when Atha Group want to explore without you on the call.

**Render** — free tier, no card required.

1. Push this repository to GitHub (private).
2. In Render: **New → Blueprint**, select the repository. `render.yaml` configures the rest.
3. Do **not** share the URL. `DEMO_USER` / `DEMO_PASSWORD` gate nothing (see the
   header); the deployment is protected only by seeded, guessable credentials.
4. Wait for the first build, then open the `.onrender.com` address.

Two things to expect on the free tier:

- The service sleeps after inactivity, so the first request takes 30-50 seconds. Open it yourself a
  minute before any call.
- There is no persistent disk. `CAPEX_DB_PATH` points at `/tmp`, which is empty on every restart.

> **Operational note (post DEF-01 fix).** `app/run.py` no longer creates a missing database itself
> — see *"This process never migrates itself"* below. `render.yaml`'s `startCommand` and the
> `Dockerfile`'s `CMD` still read `python app/run.py` alone as committed in this repository; against
> an empty `/tmp` that now refuses to start instead of reseeding. Until those two files are updated
> to run the migration command first, deploy with a start command that does both steps, for example:
>
> ```bash
> python -m app.backend.migrate --db /tmp/capex.db --fresh --seed && python app/run.py
> ```
>
> This is flagged here rather than fixed in `render.yaml` / `Dockerfile` directly — both are outside
> this change's file ownership.

Railway, Fly.io and Azure App Service all work the same way. Any host that runs a Dockerfile can use
the one in this repository:

```bash
docker build -t capex-hub .
# The schema is a DEPLOY STEP -- run.py does not migrate itself (DEF-01) --
# so a fresh volume needs this first, or the server starts against nothing:
docker run --rm -v capexdata:/data capex-hub python -m app.backend.migrate --upgrade

# No -e DEMO_USER / -e DEMO_PASSWORD: nothing reads them. They are omitted
# rather than shown with a placeholder, because a placeholder is an
# instruction.
docker run -p 8000:8000 -v capexdata:/data capex-hub
```

---

## Option 4 — Inside Atha Group's own network

Best once they want their own people clicking around, and it avoids any public exposure. Their IT
team runs the container on an internal host; only staff on the network can reach it. Still set
a real access control in front of the app — an internal network is not one, and
neither is `DEMO_USER` / `DEMO_PASSWORD`, which nothing reads.

---

## This process never migrates itself

`app/run.py` used to rebuild or upgrade the database on every boot, unconditionally. A database
that predated the migration runner made that upgrade fail outright, and the whole application
refused to start (DEF-01, `docs/PHASE_0A_FINDINGS.md`).

An application must never migrate itself on boot — it cannot be rolled back, it races when scaled
horizontally, and it turns a schema problem into an outage. So `app/run.py` now only ever **checks**
the schema before serving, and refuses to start — printing the exact command to run, and exiting
non-zero — if it is missing, behind, or drifted. It never writes.

```bash
python -m app.backend.migrate --db <path> --fresh --seed   # first run, or after --reseed's old job
python -m app.backend.migrate --db <path> --upgrade         # every run after that
python app/run.py                                           # then, and only then, start the app
```

The same discipline applies to the PostgreSQL foundation landing alongside SQLite in this phase of
the port (`app/backend/pg/`). It is optional for this POC — unset `CAPEX_DB_URL` / `CAPEX_DB_HOST`
and the process serves purely on SQLite as before — but when configured, `app/run.py` verifies its
schema the same read-only way via `python -m app.backend.pg.migrate_pg --upgrade` as the deploy step.

**Two endpoints, two different questions**, both mounted outside `/api/` so a health probe never
needs a session:

- `GET /healthz` — is the process up? Touches no database at all; answers even if one is completely
  unreachable.
- `GET /readyz` — is the database reachable **and** at a known schema revision? Returns `503` when
  either is false, with `schema_version` on success. Never reports a host, user, database name, DSN
  or raw driver message on failure — only an error class, because a driver's own error text can echo
  connection parameters.

---

## Before you share a link with anyone

- [ ] You have NOT relied on `DEMO_USER` / `DEMO_PASSWORD`, which gate nothing, and
      you have a real access control in front of the application
- [ ] The password went by a different channel from the link
- [ ] The data is the demo dataset only — no real Atha Group figures have been loaded
- [ ] The Zoho connector is in mock mode and holds no real credentials
- [ ] You have agreed how long the link stays up, and who takes it down

## What to say when they ask "can we start using it?"

Be straight about it, because the gap is real and small:

> This proves the control works. Before real money runs through it we need sign-on and role
> enforcement, a production database with locking so two approvers cannot spend the same rupee,
> a live Zoho connection, and migration of your existing CWIP balances and open commitments.

The one item they can start on immediately, because it needs no code and blocks everything else:
**a Zoho administrator must create the CAPEX code, WBS code and budget head custom fields in the
Zoho interface.** There is no API that can create them.
