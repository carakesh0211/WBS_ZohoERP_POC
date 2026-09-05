# A3 — the approval engine's eight screens in the primary navigation

**STATUS (2026-09-05, stream A3): WITHDRAWN FROM THE RAIL, LANDED AS ROUTES.**
**No decision is outstanding for the baselines; one is outstanding for the nav.**

The eight rail entries this document described have been REMOVED. The eight
screens are reachable by hash route, permission-gated and deep-linkable,
exactly as §1 said they would be if the entries were deleted. Consequences,
all measured rather than argued:

- **The 46 baselines in §3 are green again, and NONE was regenerated.** They
  differed *only* because of these entries. Removing them restored every one:
  `approved-ui.spec.js` and `spa-routing.spec.js` now pass at desktop-1440 and
  laptop-1024 with the committed, approved snapshots untouched. That is the
  confinement proof §3 asked for, obtained by reverting the cause instead of
  re-recording the evidence.
- **The rail fits desktop-1440 again**: content 840px against an 856px rail.
  `Settings & Master Data` is back above the fold.
- **laptop-1024 still overflows by 127px.** That is A1, not A3, and it is
  untouched and still open — see §5.
- **A single consolidated "Approvals" entry was measured, and does not fit
  either**: it takes the content to 870px against an 856px rail, overflowing
  desktop-1440 by 14px and pushing `Settings & Master Data` back below the
  fold. That is the cheapest possible version of this change — one row, in an
  existing group, with no new group header — so no smaller nav addition
  exists to try.

**What the lead is asked to decide** is therefore the navigation question
alone, with the numbers above: whether to approve one "Approvals" entry and
accept 14px of desktop-1440 overflow, or to fix the rail first (collapsible
groups, a denser rail), or to leave the eight as routes. The routes work
either way, and adding an entry later is one `scr('…')` splice per row.

The original text follows, unchanged, because it is the record of why the
entries were landed and what they cost.

---

**ORIGINAL STATUS: implemented, NOT signed off, and NOT yet re-baselined. It
needs a decision from the lead and the product owner before it can go green.**

`README.md` in this directory records A1 (five navigation entries) and A2 (the
`#userAvatar` contrast correction). Both were approved in writing on
2026-09-03, and `docs/WAVE4_CONTRACTS.md` names them as "the two APPROVED UI
changes — stream 4 only". **A3 is not one of them.** It is recorded separately,
rather than folded into that document, because the difference between
"approved" and "instructed" is the thing this directory exists to keep visible.

- **Instructed by:** the Wave 4 lead's brief to the frontend-completion stream —
  *"make each reachable from primary navigation, permission-gated, and
  deep-linkable by hash"*.
- **Approved by the product owner:** **no.**
- **Landed in:** `69f45e1` (WIP, stream 4), where it was *invisible* — see below.
- **Reversible in one edit:** delete the eight `scr('approval-…')` splices from
  the `NAV` table in `app/frontend/app.js`. The routes, the permission gate and
  the hash deep-links all survive that deletion untouched; only the rail
  entries go. That is precisely the state Wave 3 shipped the five SCR-nn
  screens in.

---

## 1. What changed

The eight M4b approval screens are listed in the primary navigation rail, each
gated on its own Contract 4 permission through the existing `navAllowed()`
gate — the same mechanism A1 used, with the same `SCR_ROUTES` row spliced into
`NAV` **by reference**, so the rail gate and the route gate cannot drift apart.

| Entry | Group | Hash | Permission |
|---|---|---|---|
| My Approval Inbox | Work | `#approval-inbox` | `approval.read` |
| Approval Request Detail | Work | `#approval-request` | `approval.read` |
| Escalation & SLA Monitor | Work | `#approval-sla` | `approval.read` |
| Approval Timeline | Governance | `#approval-timeline` | `approval.read` |
| Approval Matrix Configuration | Governance | `#approval-matrix` | `approval.configure` |
| Workflow Version History | Governance | `#approval-versions` | `approval.configure` |
| Approval Rule Simulator | Governance | `#approval-simulator` | `approval.configure` |
| Delegation Management | Governance | `#approval-delegations` | `approval.delegate` |

**No principal sees all eight.** Contract 4 splits the permissions so that
Administrator holds `approval.read` and `approval.configure` but neither
`approval.act` nor `approval.delegate`, while the four approver roles hold
`read`/`act`/`delegate` but never `configure`. An Administrator therefore sees
seven of the eight; an approver sees five; and an Auditor — which
`auth.PERMISSIONS` deliberately excludes from all four, recorded against
D-12 — sees none. That is the gate working, not a gap.

## 2. Why the baselines move NOW, when the entries landed two commits ago

The entries were added to `NAV` in `69f45e1`, and at that point they changed
nothing that had a baseline: `WAVE4_FRONTEND_FINDINGS.md` §1 records that no
principal held any approval permission, so the rail rendered exactly as A1 left
it. `1224b83` ("Add the four M4b approval permissions") is **not** an ancestor
of `69f45e1` — it landed on the integration lineage and reached this branch by
merge afterwards — so the stream that wrote these entries never saw them
render. `U-ADM`, the identity every approved screenshot is captured as, now
holds `approval.read` and `approval.configure`.

The same lineage explains why the eight `approvals.spec.js` baselines were
captured with the approval entries visible while the `approved-ui` ones were
not: at `69f45e1` the retired `grantApprovalPermissions` stub put all four
permissions into the bootstrap response, so that suite — and only that suite —
photographed a rail that no real session produced.

Three of the new entries sit in the **Work** group, near the TOP of the rail,
so every row beneath them moves down by three — on every screen. Four more sit
inside **Governance**, which pushes the tail of the rail further still. Nothing
outside the 224px `--nav-w` column moves: the rail is a fixed-width flex
column, and `.nav` is its own `overflow-y: auto` scroll container, so a taller
rail cannot change the page height or reflow the content beside it. That is
what makes the confinement proof in §3 the right check.

`tablet-800` is unaffected, and that is a check rather than a coincidence:
below 900px the rail is `display: none` (AUD-M-004), so there is nothing there
to move. All 23 tablet-800 baselines pass untouched, exactly as they did for A1.

## 3. Exactly which baselines differ, measured

Run on 2026-09-04, at HEAD, with **no** `--update-snapshots`:

| Suite | Failing | Which |
|---|---|---|
| `approved-ui.spec.js` | **36** of 60 | all 17 route baselines at desktop-1440 and laptop-1024, plus `wbs-cosy` at both. Every tablet-800 baseline passes. |
| `spa-routing.spec.js` | **10** of 165 | the five SCR-nn screenshots at desktop-1440 and laptop-1024. The other 155 tests pass, including the exact-rail assertion and every keyboard-traversal test. |
| `approvals.spec.js` | **3** of 24 | `approval-delegations` at all three viewports — a different cause, see §4. |

**None of these 49 has been regenerated.** Regenerating a baseline under
`tests/vrt/approved-ui.spec.js-snapshots/` is exactly what the wave rules
forbid without written product-owner approval, and the working environment
blocks `--update-snapshots` outright. So the suite is currently RED on those
49, deliberately, rather than green on evidence that was overwritten.

**What must happen before they are regenerated**

1. The product owner approves A3 in writing, as they did A1 and A2.
2. `npx playwright test tests/vrt/approved-ui.spec.js tests/vrt/spa-routing.spec.js --update-snapshots`
3. `python tests/vrt/evidence/prove-baseline-delta.py --ref <this commit>` —
   which FAILS if a single changed pixel falls outside the nav-rail column or
   the avatar rect, both measured from the running application. Its output is
   saved here as `BASELINE-DELTA-A3.txt`.

Step 3 is the part that matters. A wholesale re-baseline is the single most
dangerous move available to this stream — a previous stream did exactly that
and it concealed a real 694px regression — so the confinement proof is not
optional, and this change must not be accepted on the strength of "the tests
are green again".

## 4. `approval-delegations` is a separate, forced baseline change

The three `approval-delegations` baselines were captured in `69f45e1` under
`U-ADM` **with `approval.delegate` injected into the bootstrap response** by the
`grantApprovalPermissions` stub that has since been retired.

Contract 4 does not grant an Administrator `approval.delegate`. So that
baseline depicts a principal the permission model does not allow to exist, and
it is unreproducible by any real session. The screen is now captured as
`U-PFC` (BudgetController + FinanceApprover), which genuinely holds the
permission — a different user, so a different rail, a different avatar and a
different shell bar.

This one is not a design change and needs no product-owner decision; it is a
stale baseline being replaced with a reproducible one. It does still need the
`--update-snapshots` permission.

**Why only three of the eight stub-captured baselines moved.** All eight were
photographed with a rail carrying all eight approval entries; a real `U-ADM`
session now carries seven. Losing the `approval-delegations` row should have
shifted the rail on the other seven screens too — and it did not, because that
row and the `settings` row beneath it are both **below the fold** of the
clipped `.nav` scroll container at every baseline height. §5 measures that
directly. So the seven surviving baselines are not evidence that nothing
changed; they are evidence that the change happened in a part of the rail
nobody can see.

## 5. A finding that came out of this: the rail no longer fits

Measured from the running application at HEAD, signed in as `U-ADM`
(29 entries):

| Viewport | Rail height | Content height | Overflow | Entries below the fold |
|---|---|---|---|---|
| desktop-1440 (900px tall) | 856px | 1050px | **194px** | `audit`, `audit-trail`, `approval-timeline`, `approval-matrix`, `approval-versions`, `approval-simulator`, `settings` |
| laptop-1024 (768px tall) | 713px | 1050px | **337px** | the above plus `cap`, `zoho`, `inventory` |
| tablet-800 | — | — | — | rail is `display:none`; drawer scrolls |

`.nav` is `overflow-y: auto`, so nothing breaks and nothing widens the page —
the rail simply scrolls. But **the entire Governance group is now below the
fold at both desktop widths**, and that includes `Settings & Master Data`,
which is one of the five entries A1 was approved to add. It is reachable only
by scrolling a column that gives no visual hint it scrolls.

Arithmetic on the same measurements: removing the seven approval entries an
Administrator sees returns the content height to 840px, which fits the
desktop-1440 rail (856px) and still overflows laptop-1024 (713px) by 127px —
so **A1 alone had already pushed laptop-1024 into overflow**, and A3 is what
pushes desktop-1440 over as well.

This is a product decision, not a bug to be quietly patched: the answer is
probably collapsible groups, or a shorter rail, or not listing all eight. It is
reported rather than fixed because inventing a navigation pattern is well
outside "the two approved UI changes".

## 6. What is NOT claimed here

- That the client asked for this. They did not; the lead did.
- That eight more rows are the right information architecture. §5 is evidence
  that they are not.
- That the 49 differing baselines are legitimate. Forty-six of them are
  *explained* — they are the rail, and only the rail — but explained is not
  approved, and the confinement proof in §3 step 3 has not been run because the
  new baselines do not exist.
