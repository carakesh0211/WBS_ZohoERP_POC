# Showing the POC to Atha Group

Four ways to demonstrate, from least to most exposed. Pick the lowest one that meets the need.

> **Read this first.** The POC has no user model. Every visitor can approve purchase requests,
> cancel purchase orders and capitalise projects. On a laptop that is fine. On a public URL it is
> not. Anything beyond option 1 must have `DEMO_USER` and `DEMO_PASSWORD` set — the app then
> demands a username and password before a single screen loads.

---

## Option 1 — Screen share (recommended for the first meeting)

Nothing is exposed, nothing can be clicked by anyone else, and you control the pace.

```bash
python app/run.py
```

Open <http://127.0.0.1:8000> and share your screen. Press `--reseed` between runs to reset the data:

```bash
python app/run.py --reseed
```

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
set DEMO_USER=atha
set DEMO_PASSWORD=<choose-a-strong-one>
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
3. In the dashboard, set `DEMO_USER` and `DEMO_PASSWORD` before you share the URL.
4. Wait for the first build, then open the `.onrender.com` address.

Two things to expect on the free tier:

- The service sleeps after inactivity, so the first request takes 30-50 seconds. Open it yourself a
  minute before any call.
- There is no persistent disk. `CAPEX_DB_PATH` points at `/tmp`, so the demo dataset reseeds on
  restart. For a demo that is a feature — it always returns to a clean, known state.

Railway, Fly.io and Azure App Service all work the same way. Any host that runs a Dockerfile can use
the one in this repository:

```bash
docker build -t capex-hub .
docker run -p 8000:8000 -e DEMO_USER=atha -e DEMO_PASSWORD=<strong> -v capexdata:/data capex-hub
```

---

## Option 4 — Inside Atha Group's own network

Best once they want their own people clicking around, and it avoids any public exposure. Their IT
team runs the container on an internal host; only staff on the network can reach it. Still set
`DEMO_USER` and `DEMO_PASSWORD` — an internal network is not an access control.

---

## Before you share a link with anyone

- [ ] `DEMO_USER` and `DEMO_PASSWORD` are set, and you have confirmed the login prompt appears
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
