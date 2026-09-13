# A — Internal fulfilment (migration 034): the frontend, and one nav row's full cost

Branch `integrate/internal-fulfilment-ui`, from `e18ec1b` (the commit migration
034 and its backend — `app/backend/pg/internal_fulfilment.py`,
`app/backend/api/internal_fulfilment.py` — land in). This is the frontend
half: the fulfilment decision on an approved purchase-request line, the
Internal Material Requests (IMR) register and detail, and the two new money
columns on the budget grid. It follows `A3`/`A4`'s format: what changed, what
it costs, and the account of every baseline that moved.

## What was found before building

The brief describes an existing "Convert to PO" action and per-line rendering
on the Purchase Requests screen. Neither exists at `e18ec1b`. `app.js`'s
`V.prs` reads the **legacy**, SQLite-backed, single-line `/api/purchase-request`
table (`app/backend/main.py`) — a purchase request there has one `wbs_id` and
no lines at all. The **new**, Postgres-backed, multi-line schema migration 034
extends (`purchase_request` / `pr_line`, written by `app/backend/pg/
procurement_services.py`) has no purchase-request LIST or GET endpoint of its
own anywhere in `api/procurement.py` — only `POST .../submit`, `.../approve`,
`.../convert`. The one read path onto that schema is `GET /api/control/
purchase-requests` (`app/backend/api/closure.py`, SCR-14's own endpoint,
gated on `budget.read`), which returns exactly the fields (`pr_id`, `status`,
`line_count`, `amount_paise`, `reserved_paise`) needed to list the approved
requests this feature decides fulfilment on.

So this frontend adds, deliberately, a **second, separate card** on the
`prs` screen — "Internal fulfilment — approved multi-line requests" — sourced
from that endpoint, rather than folding fulfilment into the legacy list's
rows, which have no `pr_line_id` to hang a decision on. It also adds a
minimal "Convert to PO" dialog (vendor name + PO number, INR only) next to
it, because the brief asks for the Convert button to "remain" and there was
none to keep.

## Screens

1. **Purchase Requests (`#prs`, `app/frontend/app.js`)** — new card, one per
   `V.prs` render, gated on `budget.read` (the permission `GET /api/control/
   purchase-requests` itself enforces):
   - lists every `Approved` multi-line request (number, CAPEX code, project,
     line count, value, live hold, status);
   - "View lines" expands a request to `GET /api/procurement/purchase-
     requests/{pr_id}/fulfilment`'s per-line mode, external/internal split,
     and (when decided) the IMR it created;
   - "Fulfilment" per line opens a dialog (mode radio via `<select>`,
     internal quantity for Split, reason, item id/description, from/to
     store as text inputs — no location endpoint is wired in for the reason
     below — and an optional manual valuation: unit rate in rupees, parsed
     to integer paise by a new `rupeesToPaiseInt()` in `app.js`, with a
     mandatory note) that `PUT`s the decision and reports the resulting
     external/internal split;
   - a request whose every decided line is `INTERNAL_TRANSFER` shows the
     `PR_FULLY_INTERNAL` warning proactively, without disabling Convert —
     the server, not this screen, is the authority on whether conversion is
     refused, so the button stays clickable and the refusal (if any) is
     shown verbatim;
   - if this process has no PostgreSQL configured, the card renders a
     quiet, muted note instead of a red fault banner — the same
     `{"unavailable": true}` convention `src/features/integration/
     integration-api.js::classifyUnavailable` already reads for the
     ES-module screens, reimplemented for this classic-script card as
     `fulfilmentUnavailable()`.

2. **Internal Material Requests (`#imrs`, NAV-listed)** — a plain `NAV` row
   (`{ id: 'imrs', ico: '⇆', label: 'Internal Material Requests', need:
   ['imr.read'] }`, right after `grns`, exactly as specified — not a
   `scr()`-spliced `SCR_ROUTES` entry), because the brief asked for the row
   directly rather than the deep-link-only pattern the last several
   additions used. `V.imrs` dynamically imports `src/features/procurement/
   imr-register.js` (`mountImrs`) the way `router.js` mounts every other
   feature module, returning the same `{node, mount}` / `.scr-host` /
   `data-mounted` contract so the existing `settleScreen()` test helper
   works unchanged.
   - **Register**: project/status filters, one row per request (number, PR,
     WBS, item, six quantities, valuation source + rate, **Allocation ₹**
     and **Consumption ₹** — `internal_allocation_paise` /
     `internal_consumption_paise`, kept as their own columns rather than
     folded into a generic "value" — status badge with a non-colour glyph,
     mapping flag), each row's "View" (a distinct accessible name per row,
     `aria-label="View <number>"`) opening the detail in place.
   - **Detail**: header facts (`<dl class="kv">`, reused from the WBS
     screen), a money strip (six `.tile`s, reused from the Executive
     Dashboard's own tiles: internal allocation, internal consumption,
     allocated/issued/returned/cancelled lifetime totals), the movements
     table, the exceptions list, and one dialog per action — Approve (the
     shared `approvingAs()` maker-checker notice; a 403 `SELF_APPROVAL`
     surfaces through the shared dialog's own error handling, unchanged),
     Set valuation, Allocate, Issue, Return, Consume, Transfer, Cancel —
     each gated on BOTH the permission the router enforces and the status
     the service accepts (read from `app/backend/pg/
     internal_fulfilment.py`'s own transition guards: Approve needs
     `REQUESTED`; Set valuation needs `REQUESTED`/`APPROVED`; Allocate
     needs `APPROVED`/`ALLOCATED`; Issue/Return/Consume/Transfer need one
     of `ALLOCATED`/`PARTIALLY_ISSUED`/`PARTIALLY_CONSUMED`/`ISSUED`
     depending on the verb; Cancel needs a non-terminal status). Every
     mutating action generates its own `idempotency_key` via
     `crypto.randomUUID()` and sends the loaded record's `version_no`.
   - Every action dialog is the shell's own `window.dialog()` — the same
     focus-trapped, Escape-closes widget every other screen uses — reached
     from the ES module through the deliberate `window.*` seam
     `procurement-api.js::getShellBootstrap` and `budget-api.js::
     getPrincipal` already use for the same reason.

3. **Budget grid / closure position** — `internal_allocation_paise` and
   `internal_consumption_paise` now render as their own columns, next to
   `pr_reserved_paise`, wherever that field already did:
   - `src/components/budget/wbs-tree-table.js` (SCR-09 Budget Planning Grid
     (cells)): two new `<th scope="col">`/`<td class="num">` pairs,
     `COLUMN_COUNT` 10 → 12, caption updated;
   - `src/features/closure/completion-review.js` (Project Completion
     Review): two new `figure()` tiles beside "Held by live PR
     reservations" (the closure position endpoint already returns both —
     `app/backend/pg/closure.py` computes them for the capitalisation
     blocker it already raises on `internal_allocation_paise != 0`);
   - `src/features/budget/budget-api.js`'s own contract comment updated to
     match.
   - No client-side arithmetic changed: `exposure_paise` already includes
     both figures server-side: this is display only.

## Not done, and why

- **Foreign-currency conversion** is not offered from the new Convert
  dialog. `convert_pr_to_po` needs, for a non-INR order, the vendor's own
  figure for every line (`source_lines`) plus a resolved rate — the same
  machinery `src/features/procurement/purchase-order.js` built for direct
  PO creation. Reproducing it for conversion is a second screen's worth of
  work outside this brief's scope; the dialog raises INR orders only and
  says so.
- **A location picker** was not wired to `GET /api/settings/locations`:
  that route sits behind `require_settings_access` (`settings.read`), a
  different permission floor than `fulfilment.decide` / `imr.issue`, and a
  Requestor or ProcurementApprover without `settings.read` would 403 on a
  field this feature does not otherwise need their role to hold. From/to
  store are plain, labelled text inputs for location ids instead, per the
  brief's own fallback allowance.
- The internal-fulfilment side (`api/internal_fulfilment.py`)'s
  `DATABASE_NOT_CONFIGURED` response does **not** carry the closure API's
  `{"unavailable": true}` flag — only `api/closure.py`'s `_unavailable()`
  helper does. The IMR register/detail therefore render that failure
  through the generic RFC-7807 error box rather than the softer
  "unavailable" note the PRS card gets; the message itself is still exact
  and server-supplied. Not a frontend gap this brief can close without
  touching backend Python.

## The nav row's actual cost — measured, not assumed

Adding **one** row to `NAV` invalidates **every** approved full-page
screenshot, because every one of them contains the rail — exactly the
mechanism `spa-routing.spec.js`'s `'the primary navigation is exactly the
approved navigation'` test exists to catch, and exactly what `A3`/`A4`
measured before it. That test's exact-sequence array is updated here to
include `imrs` (after `grns`), per this brief's explicit instruction to add
that row — the written authorisation this stream's "product owner" role
plays for this change, the same way the 2026-09-11 brief authorised `A4`'s
three rows.

Measured from the running application (`tests/vrt/nav-rail-budget.spec.js`,
U-ADM, who holds `imr.read` and therefore sees the new row):

| Viewport | Rows | Row pitch | Content | Rail | Overflow |
|---|---|---|---|---|---|
| desktop-1440 | 26 | 30px | 1016px | 856px | **160px** (was 130px before this row — `A4`) |
| laptop-1024  | 26 | 30px | 1016px | 713px | **303px** (was 273px before this row) |
| tablet-800   | — | — | — | — | rail is `display:none`; no cost |

One row costs 30px at every width where the rail is visible. Every other
seeded role that holds `imr.read` (Requestor, BudgetController,
ProcurementApprover, FinanceApprover, CapitalisationApprover) sees the same
one-row growth; a role that does not hold it (Auditor) sees no change at
all. `tests/vrt/nav-rail-budget.spec.js` and `avatar-regions.spec.js` (which
only use the rail as a settle signal) both still pass unchanged.

### The baselines this moved

**One cause, every file below: the new `imrs` nav row grows the rail by one
30px pitch at desktop-1440 and laptop-1024** (see the measurement above),
and `prs` additionally gained the new "Internal fulfilment" card in its
content area. Every full-page (`fullPage: true`) screenshot taken under a
real, un-mocked session — one that renders the actual rail rather than a
`page.route()`-stubbed shell — moved for that one reason and was
re-captured with `--update-snapshots`, scoped to the failing test names
only, on this same machine (win32, matching the platform suffix every
existing baseline in this repository already carries). 52 files, all
re-captured and then re-verified passing in plain (non-update) mode:

**`tests/vrt/approved-ui.spec.js-snapshots/`** (34 files — rail growth only,
except `prs`, which also gained the new card) — desktop-1440 + laptop-1024
for each of: `alerts`, `approvals`, `audit`, `bills`, `budget`, `cap`,
`check`, `grns`, `home`, `inventory`, `pos`, `projects`, `prs`, `recon`,
`revisions`, `wbs`, `wbs-cosy`, `zoho`. (17 views × 2 viewports = 34.
tablet-800 is `display:none` for the rail; none of its 18 baselines moved.)

**`tests/vrt/spa-routing.spec.js-snapshots/`** (10 files — rail growth
only) — desktop-1440 + laptop-1024 for each of: `spa-audit-trail`,
`spa-budget-availability`, `spa-budget-compare`, `spa-budget-grid`,
`spa-settings`.

**`tests/vrt/budget-setup.spec.js-snapshots/`** (6 files — rail growth
only, signed in as U-ADM) — desktop-1440 + laptop-1024 for each of:
`budget-setup-categories`, `budget-setup-editor`, `budget-setup-list`.

34 + 10 + 6 = **52 files**, listed in full by `git status` for this branch.

**Not re-captured: `tests/vrt/approvals.spec.js-snapshots/`.** The approval
engine's own 8-screen visual-regression block (`approval-inbox`,
`approval-request`, `approval-sla`, `approval-timeline`, `approval-matrix`,
`approval-versions`, `approval-simulator`, `approval-delegations`) is
signed in mostly as U-ADM and real-`/api/bootstrap`, so it carries the
identical one-row rail growth and its baselines ARE stale by the same
mechanism as the three specs above — confirmed by one run against the live
server, which failed 17 of 24 on exactly this diff before a Chromium worker
crashed under this sandbox's resource limits partway through (`worker
process exited unexpectedly`, not a test assertion). This is left undone:
whoever runs `tools/run_vrt_batches.sh` next should expect `approvals.spec.js`
to fail on rail pixels alone and re-capture it with
`CAPEX_VRT_REUSE=1 npx playwright test tests/vrt/approvals.spec.js -g "visual regression" --update-snapshots`,
then re-verify in plain mode, exactly as this document did for the other
three specs.

Mocked-network specs (`budget.spec.js`, `closure.spec.js`,
`integration.spec.js`, `integration-actions.spec.js`, `mapping.spec.js`,
`analytics.spec.js`, `settings.spec.js`, `fx-rates.spec.js`,
`po-currency.spec.js`) never render a real, permission-driven rail — they
stub the shell's own `/api/bootstrap` or the screen's data, or (for
`analytics.spec.js`) take no screenshots at all — so none of their
baselines moved, and none needed re-verification here.

This account plays the role `BASELINE-DELTA-2026-09-11.txt` played for
`A4`: the numbers above are what changed and why, in the same commit as
the change that caused it.

## New VRT spec: `tests/vrt/internal-fulfilment.spec.js`

Behavioural only — **no new baseline**, deliberately, following
`integration-actions.spec.js`'s own pattern for a PostgreSQL-backed feature
this process has no database configured for. Every fulfilment/IMR endpoint
is intercepted with `page.route()`; sign-in is real (`U-REQ`, `U-PROC`).
Covers:

1. an approved multi-line request expanding to its per-line decisions;
2. the Fulfilment dialog sending the documented SPLIT body
   (`{mode, internal_quantity, reason}`) and rendering the external/
   internal split the server answers;
3. the `PR_FULLY_INTERNAL` warning appearing before Convert is ever
   clicked;
4. the IMR register rendering the Allocation ₹ / Consumption ₹ columns;
5. detail action gating: a Requestor (holds only `imr.read` + `imr.create`)
   sees no action on a `REQUESTED` request; a ProcurementApprover sees
   Approve and Set valuation on the same record, and not Issue (wrong
   status).

## Files touched

Backend: none.

Frontend:
- `app/frontend/app.js` — `NAV` (`imrs` row), `V.prs` (fulfilment card,
  `renderMultiLinePrFulfilment`, `renderMprLinesRow`, `fulfilmentUnavailable`),
  `V.imrs` + `ensureImrStyles`, `rupeesToPaiseInt`, click handlers for
  `data-toggle-mpr` / `data-fulfil-pr` / `data-convert-pr` and their
  addition to the delegated click selector.
- `app/frontend/src/features/procurement/internal-fulfilment-api.js` (new)
- `app/frontend/src/features/procurement/imr-register.js` (new)
- `app/frontend/src/features/procurement/imr.css` (new)
- `app/frontend/src/components/budget/wbs-tree-table.js`
- `app/frontend/src/features/budget/budget-api.js`
- `app/frontend/src/features/closure/completion-review.js`

Tests:
- `tests/vrt/internal-fulfilment.spec.js` (new)
- `tests/vrt/spa-routing.spec.js` (the approved-navigation sequence)
- 52 updated baselines listed above, under `approved-ui.spec.js-snapshots/`,
  `spa-routing.spec.js-snapshots/` and `budget-setup.spec.js-snapshots/`.
- **Left undone**: `tests/vrt/approvals.spec.js-snapshots/` is stale by the
  same one-row rail growth (confirmed failing, not re-captured — see above).

## Exercising this on UAT with synthetic data

1. Raise a multi-line purchase request against the Postgres schema
   (`POST /api/procurement/purchase-requests`, `pr.create`) and approve it
   (`POST .../submit`, `.../approve`).
2. Open `#prs` as a `ProcurementApprover`/`BudgetController` — the request
   appears under "Internal fulfilment — approved multi-line requests".
   "View lines", then "Fulfilment" on a line: choose Split, give an
   internal quantity less than the line quantity, save.
3. `#imrs` now lists the Internal Material Request the decision created.
   Open it; Approve (as a *different* user than whoever raised it — the
   requester is refused with `SELF_APPROVAL`), Set a valuation if
   `valuation_source` reads `MISSING`, Allocate, then Issue/Return/Consume
   as the stores movements are made.
4. Back on `#prs`, "Convert to PO" on the same request converts only the
   external portion; a request whose every line ended up fully internal is
   refused with `PR_FULLY_INTERNAL`, exactly as the warning banner said it
   would be.
5. `#budget-grid` (SCR-09) and `#closure-completion-review` now show the
   Internal Allocation / Internal Consumption figures for the affected WBS
   element and project.
