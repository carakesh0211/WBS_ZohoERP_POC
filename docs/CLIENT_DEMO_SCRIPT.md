# Client Demonstration Script — CAPEX & WBS Control Hub

Wave 8, stream D. About 25 minutes, plus a 12-minute short version.

Every figure quoted below was produced by the running application at the commit
carrying this file. Where a screen will not have data, this script says so
**before** you open it, so nobody discovers it in front of a client.

---

## Read this before you demonstrate

### 1. Zoho is a MOCK. Say so out loud, early, in your own words.

No Zoho tenant is connected. No sandbox credential exists. **No live call has
ever been made by this build.** The connector constructs the request it *would*
send from a verified inventory of 869 real ERP endpoints and logs it; the
response is synthesised.

The screens tell the truth if you let them: the connector shows
`oauth_status: "Not Connected"`, `status: "Disabled"`, and credentials as
`secretref://` placeholders. **Point at that rather than talking over it.** It is
a stronger position than it sounds — the endpoint inventory is real and
mechanically derived from Zoho's own OpenAPI bundle, so the integration design is
evidenced even though the integration is not connected.

Never say "connected", "synced", "live" or "verified" about Zoho.

### 2. Know which half of the application has data.

Without PostgreSQL configured — which is how the local demo runs — the **17
legacy shell views serve live data from the seeded ledger**, and the **46 SCR
screens are PostgreSQL-first**. Of 75 GET endpoints probed on a SQLite-only
server, 25 answered `200` and **46 answered `503 DATABASE_NOT_CONFIGURED`**.

The SCR screens do not fail silently. They either say *"unavailable"* explicitly
or fall back to the SQLite ledger with a visible caveat on screen — *"The
reporting endpoint for this screen did not answer. Shown from this application's
own ledger instead."* That honesty is a feature, and if a client sees it you
should name it as one rather than apologise.

**The route below stays in the shell views throughout.** If you want to show the
newer screens, configure PostgreSQL first and run the migrations — see
`docs/DEPLOYMENT_RUNBOOK.md` §3.

### 3. Two things you must not promise.

* **Capitalisation posts nothing.** Approving records the *decision*. There is no
  journal, no GL entry, no fixed-asset record — in this application or in Zoho
  (exclusion X-01). The screen that prints that caveat on itself is one of the
  PostgreSQL screens, so **on this SQLite-only route you must say it yourself**.
  See step 14.
* **Maker-checker cannot be shown refusing a self-approval.** The role model
  separates the duties one layer earlier, so no demo identity can raise a request
  and then attempt to approve it. Demonstrate the *role* refusal instead — it is
  more striking anyway (step 9).

### 4. Set up

```bash
python -m app.backend.migrate --db app/data/capex_demo.db --fresh --seed
CAPEX_PROFILE=local-demo CAPEX_DB_PATH=app/data/capex_demo.db PORT=8871 \
  python app/run.py
```

**Both commands, in that order, every time.** The migration wipes the
credentials and the application start-up re-creates them; migrate without
restarting and every sign-in returns `401`.

Sign in with the identity each step names, and **check the identity before each
step** — this walkthrough deliberately moves between six of them, because the
authority to do each thing genuinely sits with a different person:

| Steps | Identity | Why |
|---|---|---|
| 1–4, 6, 8 | `U-PM` | Project Controller: reads budget, raises requests |
| 5 | `U-FIN` | Only Finance may approve an over-budget request |
| 7 | `U-PLH` | Only Procurement may cancel a purchase order |
| 9 | `U-AUD` | The read-only role |
| 10, 12, 13 | `U-ADM` | Administration and the connector |
| 14 | `U-CFO` | Only the Capitalisation Approver may approve one |

Password is always the user id followed by `!demo`. Keep the `U-PM` and `U-AUD`
windows open side by side so step 9 is a switch of window, not a re-login in
front of the client.

---

## The demonstration

### Part 1 — The control model (8 minutes)

**1. Executive Dashboard** — sign in as `U-PM`.
Budget, commitment, actual and available for the whole portfolio, in one view.
Say: *every figure here is integer paise all the way to the screen; there is no
floating-point money anywhere in this application.*

**2. WBS Explorer** — expand `CAPEX-2026-001` → Plant & Machinery.
Figures roll up from work package to project. The estate has three projects, a
three-level WBS, and one deliberately abandoned work package, so the awkward
shapes are visible rather than hypothetical.

**3. Budget Availability Check** — propose **₹20,00,000** on
**CAPEX-2026-001.03** (Plant & Machinery), budget head Plant & Machinery.

It refuses, and it says why. Verbatim, from the running application:

> The proposed value of ₹20,00,000.00 exceeds the available budget of
> ₹19,10,000.00 for CAPEX-2026-001.03 (Plant & Machinery) by ₹90,000.00. Submit
> a budget revision or request exception approval.

**This is the money slide.** A control that only says "no" creates a workaround.
This one states the shortfall to the rupee and names the two legitimate routes
forward. Pause here.

**4. Purchase Requests** — show `PR-2026-0010`, which is *Exception Pending*
because it exceeds budget. It was not blocked; it was **routed**. Over-budget
spend becomes a Finance decision with a reason attached, instead of a rejection
that gets worked around offline.

**5. Approve it — as `U-FIN`, with no reason.** An over-budget request needs
Finance, not Procurement, so switch identity here; `U-PM` would simply be
refused for the wrong reason and the point would be lost. Verbatim, from the
running application:

> This request exceeds available budget. An exception reason is mandatory and
> will be stored in the audit trail.

Add a reason — *"Board-approved contingency"* — and it is accepted. The reason
goes to the audit trail, not just to the record.

### Part 2 — Commitment and reconciliation (7 minutes)

**6. Commitment Reconciliation.** Billed + open commitment = ordered, on every
line. Say: *this identity is the whole point of the reconciliation screen — if it
ever fails to hold, the discrepancy is surfaced as an exception rather than
absorbed.*

**7. Commitments — cancel a PO** (`PO-2026-0004`, Fully Committed), **as
`U-PLH`**. Cancelling is a Procurement authority; neither `U-PM` nor `U-FIN`
holds it, and neither does the Administrator. The released commitment returns to
available budget immediately — go back to the availability check from step 3 and
show the number has moved.

**8. Goods receipts and bills.** Received-but-unbilled exposure is tracked
separately from billed actuals — the gap most CAPEX reporting loses.

### Part 3 — Who can do what (6 minutes)

This part is worth more than it looks. Most demonstrations show what the software
can do; almost none show what it refuses.

**9. Switch to the `U-AUD` window.** The Auditor is a *read-only* role and the
application means it. The navigation offers fewer entries, and typing the URL
does not help — a deep link to a screen the Auditor may not use is **corrected
back to Home**, and the Back button will not walk into it either.

The Auditor cannot: see the approval inbox, open Settings or master data, start
an export, manage a connector, or triage a reconciliation exception. It can read
the audit trail, which is the record that matters for the role.

**10. The counter-intuitive one, if the client is technical.** Sign in as
`U-ADM`. The **Administrator cannot raise a purchase request, cannot approve one,
cannot cancel a purchase order, cannot close an accounting period, cannot approve
a capitalisation, and cannot delegate an approval authority** — because it holds
none. Administration and financial authority are different things here, and the
separation is enforced by the server, not by hiding a menu.

**11. Private versus shared, on one route.** Saving a report view as **private**
is a bookmark and every role can do it. Publishing the same view as **shared**
changes who reads money through those filters, so it needs a different right —
the Requestor, Procurement and the Auditor are refused. Same screen, one field,
different answer.

Say: *this is asserted by 358 automated checks that run the whole matrix in both
directions — every role that should be allowed, and every role that must be
refused.*

### Part 4 — Integration and evidence (4 minutes)

**12. Zoho ERP Connector.** Open it and **read the status out**: Not Connected,
Disabled, credentials as placeholders. Then show what *is* real — the endpoint
inventory: 869 endpoints across 78 ERP modules, each with its HTTP method, its
required OAuth scope and the documentation page it came from, generated
mechanically from Zoho's published OpenAPI bundle.

Say plainly: *the integration is designed and evidenced; it is not connected. To
connect it we need a sandbox tenant and an OAuth client from you.*

If asked about purchase requests in Zoho: **there is no Purchase Request object
in Zoho ERP.** Checked against all 869 endpoints — none. It lives here, or in a
Zoho custom module.

If asked how fast sync is: **one minute is the floor**, set by the scheduler
granularity of the platform, not by this application.

**13. Audit Trail.** Everything done in the last twenty minutes is there, in
order, with the actor the server derived — not the actor a request claimed to be.
Run the chain verification in front of them.

**14. Capitalisation** — close on this, carefully, and read the next two
paragraphs before you open the screen.

On the shell view (the one this route uses), approval of `CAP-2026-0001` is
**refused**, and the refusal enumerates its blockers. Verbatim, from the running
application:

> CAP-2026-0001 cannot be capitalised: ₹55,50,000.00 of open commitment remains;
> ₹5,00,000.00 received but not billed. Resolve these, or record an explicit
> write-off, before capitalising.

That is the point worth making: **a project is not capitalised while money is
still in flight against it**, and the system will not let a controller close it
by wishing. The residual has to be resolved or explicitly written off, and the
write-off is a recorded decision with a reason.

**Say the X-01 caveat yourself here.** The Capitalisation Workbench that renders
the *"NOT POSTED — local approval only; no ERP/GL or fixed-asset posting exists
in this build"* note on screen is one of the PostgreSQL screens, so on a
SQLite-only demo you will not see it and the client will not either. Do not let
that omission do the talking:

*Even once the blockers are cleared, approving records the decision, the
allocation split and the audit entry — and posts nothing. There is no journal and
no fixed-asset record, here or in Zoho. Where that posting happens is the next
conversation, and it needs your finance team.*

---

## The 12-minute short version

Steps **1, 3, 4, 5, 6, 7, 9, 12, 13** — the control model, one refusal, one
reconciliation, the role refusals, the honest connector, the audit trail. Skip
Part 3 step 10 and step 11 unless the audience is technical; skip step 14 unless
you have time to deliver the caveat properly. **Never show capitalisation without
the posting note.**

---

## Questions you will be asked, and the honest answer

| Question | Answer |
|---|---|
| "Is it connected to Zoho?" | No. Nothing is connected. We have verified 869 ERP endpoints and built the requests against them; we have never sent one. We need a sandbox tenant and an OAuth client from you. |
| "Can we see it live next week?" | Not against Zoho. We can host this build for you to explore — but only on a trusted network today, because the demo credentials are published. Making it safely public is a small, separate piece of work. |
| "Does it post to our GL?" | No. It records the capitalisation decision and the allocation. Posting the journal and creating the asset record is unspecified and needs your finance team. |
| "How near real-time is the sync?" | One minute is the floor, set by the platform's scheduler. Anything faster would need a different architecture. |
| "Can we run it on our own database?" | Yes — PostgreSQL, with row-level security per entity. There is one open question with Zoho about whether their hosting tier can reach an external database; we are waiting on their written answer, and it is a go/no-go for that deployment shape. |
| "Is it accessible?" | Mostly. One colour token — the amber used for warning statuses — measures 4.48:1 against a 4.5:1 requirement, so it fails AA on every background it is used on. We have measured a compliant replacement; applying it changes an approved visual, so it is waiting on your sign-off, not on us. |
| "Is the role model final?" | No. It is a documented least-privilege default and it needs your sign-off — particularly what the Auditor may see. Changing it is a configuration decision, and our tests read the live table, so nothing drifts quietly. |
| "How many people can use it?" | We have not measured that. No load or concurrency testing has been done, and we would rather say so than guess. |
| "Has it been security tested?" | Not by an external party. The financial controls, segregation of duties and audit chain are covered by an automated suite; a penetration test is separate work that has not happened. |

If you do not know, say you do not know and write it down. Every question in that
table was answerable because somebody measured it; the ones nobody has measured
are worth more as an honest gap than as a confident guess.
