# BCDE — sign-in choices, the Administrator override, export everywhere, notifications

Branch `integrate/frontend-bcde`, from `c15df5e` (`fable-5.1/full-app-hardening-uat`).
This is the frontend half of four backend streams that landed together on
that commit: identity (`app/backend/api/identity.py`, Stream D), the
Administrator's self-approval override (`app/backend/pg/admin_override.py`
+ `auth.require_separation`, Stream B), asynchronous exports
(`app/backend/api/exports.py` / `app/backend/pg/exports.py`, Stream C), and
the notification framework (`app/backend/api/notifications.py` /
`app/backend/pg/notifications.py`, Stream E). No Python was touched; every
change below is `app/frontend/*` and `tests/vrt/*`.

## Stream D — two sign-in choices, forgot / reset, change password

**`app/frontend/index.html`** — `#authScreen` keeps `loginForm` and its five
approved-test ids (`loginForm`, `loginUser`, `loginPass`, `loginBtn`,
`loginMsg`, `loginHint`) byte-identical, and gains, around it:
- `#ssoBtn` ("Continue with Zoho") + `#ssoDivider` ("or"), shown only when
  `GET /api/auth/providers` reports `oidc.enabled`;
- an `<h2>` "Sign in with WBS account" over the existing fields;
- `#forgotLink` ("Forgot password?"), shown only when
  `providers.local.forgot_password`, opening `#forgotForm` (user id →
  `POST /api/auth/forgot`, always shows the returned `message`, never
  whether the account exists);
- `#resetForm` (new password + confirm), shown only when the URL carries
  `#reset?token=…`, → `POST /api/auth/reset`;
- `#ssoErrorMsg`, populated when the hash carries `#sso_error=<CODE>`.

The identity area (`.user-block`, where `paintIdentity()` renders) gains
`#notificationsBtn` and `#changePasswordBtn`, next to the existing
`#signOutBtn`. **This changes the shellbar on every screen** — see
"VRT baselines" below.

**`app/frontend/app.js`** — the boot IIFE fetches `GET /api/auth/providers`
once (cached on `S.authProviders`) before deciding what to show, then calls
`handleAuthHash()`, which recognises `#sso=<code>` (→ `POST /api/auth/oidc/
complete`, `setSession`, `start()`), `#sso_error=<CODE>` (one human sentence
per code — `SSO_ERROR_MESSAGES`, an unlisted code falls back to showing the
code itself) and `#reset?token=…` (shows the reset form), all BEFORE the
ordinary "is there a session" branch, since all three can arrive with no
session yet. `showAuthCard()` toggles between the three forms; `showAuth()`
calls it and `renderAuthProviders()` so returning to sign-in (sign-out, an
expired session) always re-evaluates what to show. `#changePasswordBtn`
opens a `dialog()` (current/new/confirm) → `POST /api/auth/password`,
explaining that other sessions are signed out.

## Stream B — the Administrator's deliberate self-approval override

`auth.require_separation` refuses a self-approval with 403 `SELF_APPROVAL`;
an Administrator may override it by re-sending the same call with
`admin_override_reason` (≥ `auth.ADMIN_OVERRIDE_MIN_REASON` = 10
characters). Every surface below re-opens with the SAME unmissable warning
(`.msg.msg-error`, `role="alert"`, naming `ADMIN_SELF_APPROVAL_OVERRIDE` and
that every other Administrator is notified) and a mandatory reason
textarea, client-validated at 10 characters, and NEVER sends the reason on
the first attempt:

- **`app.js`** — `withAdminOverride(submit)` wraps a dialog's `onOk`;
  rewrites the SAME open `<dialog>` in place (a `<dialog>` cannot be
  `showModal()`ed twice) on 403 `SELF_APPROVAL` for an Administrator. Wired
  into all three native-dialog approve flows: purchase-request approve
  (`data-approve-pr`), budget-revision approve (`data-approve-rev`),
  capitalisation approve (`data-approve-cap`).
- **`src/features/approvals/approval-detail.js`** — the engine's decide
  (`POST /api/approvals/{id}/decide`). `renderAdminOverride()` replaces the
  decision card; the retry reuses the SAME `idempotency_key` and
  `object_version` (Contract 8) — this is one decision made deliberately,
  not a second one.
- **`src/features/closure/capitalisation-workbench.js`** (approve AND
  reject — both are maker-checker) and **`.../completion-review.js`**
  (accept/reject) — inline `renderAdminOverride()` into their own result
  panel, matching each screen's existing non-modal, `run()`-based idiom.
- **`src/features/procurement/imr-register.js`** — the IMR approve dialog
  reuses `window.withAdminOverride` (a classic-script top-level `function`
  declaration lands on `window`; `S`, a top-level `const`, does not — see
  the comment where this matters) rather than a fourth implementation.

`app/backend/api/budget.py`'s `POST /api/budget/revisions|transfers/{id}/
approve` also carry `admin_override_reason`, but **no frontend control
calls those routes at all** — `budget-revision-request.js` and
`budget-transfer.js` are deliberately READ-ONLY (SCR-11/SCR-12's own
documented decision, predating this stream: "no control for them is
offered here rather than one that posts a body this screen invented").
Retrofitting an approve control there is a materially bigger change than
"handle 403 on an existing approve dialog" and was left alone; the legacy
`data-approve-rev` dialog in `app.js` is the one existing approve surface
for a (different, SQLite) revision concept and is wired regardless.

**Audit trail** (`src/features/audit/audit-trail.js`, `src/core/api.js`) —
`GET /api/audit/entries` gained `action`/`actor` exact-match filters. The
Action field is a text input with a `<datalist>` naming
`ADMIN_SELF_APPROVAL_OVERRIDE`; an Actor text input sits beside it. An
`ADMIN_SELF_APPROVAL_OVERRIDE` row carries a non-colour-only marker
(`.audit-override-marker`, a glyph plus the uppercase label) next to its
action text, and its row-detail panel parses `admin_override.py::
build_detail()`'s JSON `detail` to show the reason, previous → new state,
amount (paise → rupees via the existing `formatINR`), object and
correlation id — instead of the raw JSON string every other action's
detail shows. The **legacy** `V.audit` (`app.js`, `GET /api/audit` —
SQLite, no query params at all) was left untouched: it has no backend
capability to filter by action/actor, and adding UI for a capability the
route does not have would be a lie.

## Stream C — the export button, everywhere

**`src/components/export-button.js`** (new) —
`mountExportButton({host, dataset, filtersProvider, label})`. Fetches
`GET /api/exports/datasets` once (shared across every mounted instance on a
page), creates a job via `POST /api/exports` with only the fields the
dataset's own `filters_supported` names (dropped fields get a tooltip, not
a silent widening), then **drives the job itself**: `exports.py`'s own
docstring is explicit that nothing on the server advances a job
unattended, so this component calls `POST /api/exports/{id}/advance` in a
loop (2s between calls, `exports.py`'s own advertised interval) until the
state is terminal, then does one final `GET /api/exports/{id}` for the
`result`/`error` fields `/advance`'s own bounded response does not repeat.
Offers Retry (FAILED) and Cancel (active), and downloads the result as a
`Blob` via a raw `fetch()` (never through `core/api-client.js`, which
always parses the body as text — a binary `.xlsx` would be corrupted) named
from the `Content-Disposition` header.

Mounted on all patterns:

| Dataset | Screen(s) |
|---|---|
| `purchase_requests`, `purchase_request_lines` | `app.js` `V.prs` (Purchase Requests) |
| `purchase_order_lines` | `app.js` `V.pos` (Commitments) |
| `goods_receipt_lines` | `app.js` `V.grns` |
| `vendor_bill_lines` | `app.js` `V.bills` |
| `reconciliation_exceptions` | `app.js` `V.recon` |
| `capitalisation_requests` | `app.js` `V.cap` |
| `budget_revisions` | `app.js` `V.revisions` AND `closure/budget-revision-request.js` |
| `budget_transfers` | `closure/budget-transfer.js` |
| `projects` | `app.js` `V.home` (Executive Dashboard) AND `V.projects` |
| `wbs_elements` | `app.js` `V.wbs` |
| `budget_ledger_cells` | `app.js` `V.budget` (Budget Planning Grid) |
| `internal_material_requests`, `internal_material_movements` | `procurement/imr-register.js`, sharing its own project/status filters |

All 14 datasets `GET /api/exports/datasets` registers have at least one
mount. **`audit_entries` is not one of the 14 datasets** (checked against
`app/backend/pg/exports.py::DATASETS`), so no export control is offered on
the audit trail — offering one for a dataset this build does not register
would 404 on every click. The two closure control-view screens
(`budget-revision-request.js`, `budget-transfer.js`) mount their own
export ABOVE `createListScreen()`'s card with `filtersProvider: () => ({})`
rather than reading that factory's internal filter inputs (not exposed to
a caller) — the export therefore covers the caller's full scope rather
than whatever project/status filter is on screen, which the component's
own "not all filters applied" tooltip makes visible.

## Stream E — notification preferences, history, and the Administrator outbox

The identity area's `#notificationsBtn` opens a dialog (`app.js`) with two
sections, fetched from `GET /api/me/notification-preferences` and
`GET /api/me/notifications`:
- **Preferences** — one row per event, a checkbox that saves itself on
  `change` (`PUT /api/me/notification-preferences`, no separate "Save"); a
  `mandatory` event (`PASSWORD_RESET_REQUESTED`, `PASSWORD_CHANGED`,
  `IDENTITY_LINKED`) renders `disabled`, checked, with an explanation.
- **Delivery history** — the signed-in user's own outbox rows.

**`src/features/notifications/notifications-admin.js`** (new) + its
**`notifications-api.js`** (new) — a Governance nav row `notifications`
(need: `admin.reset`), on the same footing as the IMR register (a plain
`V.notifications` entry in `app.js`, not an SCR_ROUTES splice):
- the monitoring strip (`GET /api/notifications`'s `monitoring` field —
  counts by state, adapter name, availability reason, oldest due);
- the outbox table, filterable by state/event/recipient user id;
- a detail view (`GET /api/notifications/{id}`) with every delivery
  attempt (attempt no., timestamp, outcome, provider, detail);
- Dispatch (`POST /api/notifications/dispatch`) and, on a DEAD/FAILED row,
  Retry (`POST /api/notifications/{id}/retry`).

## Nav rows added

One new row, `notifications` (Governance group, need `admin.reset`), placed
after `audit-trail` — the same footing `imrs` (migration 034) already
occupies: a plain `NAV` entry, not spliced from `SCR_ROUTES`.
`tests/vrt/spa-routing.spec.js`'s exact-sequence assertion of the approved
rail gained this one row, exactly the way Stream A added `imrs` there.

## New CSS

All in `app/frontend/extensions.css` (loaded globally already; no
`styles.css` byte changed): `.auth-divider` / `.auth-subhead` / `.auth-
links` / `#ssoBtn` (Stream D), `.admin-override-warning` /
`.admin-override-reason-field` / `.audit-override-marker` /
`.notif-override-marker` (Stream B), `.export-actions` / `.export-status` /
`.export-unavailable-note` (Stream C), `.notif-pref-row` / `.notif-pref-
label` / `.notif-pref-note` / `.notif-monitor-strip` / `.notif-monitor-
cell` (Stream E). Every colour reference is `var(--…)` against tokens
`styles.css` already declares.

## VRT baselines — expected, unrecorded drift

Two new buttons in `.user-block` (`#notificationsBtn`,
`#changePasswordBtn`) render on **every** screen's shellbar. Running
`tests/vrt/spa-routing.spec.js --project=desktop-1440` after this branch:
55 tests, **50 passed, 5 failed** — all 5 are `toHaveScreenshot` pixel
diffs on `audit-trail`, `budget-grid`, `budget-compare`,
`budget-availability` and `settings` (the five SCR-nn screens that route
carries its own full-page baseline for), all attributable to the shellbar
change, not a defect. No baseline PNG was re-recorded or added, per brief —
the consolidated re-record happens once, after this branch merges and
every stream's shellbar/rail changes are known.

## How to exercise on UAT with synthetic data

1. Sign in as `U-ADM` / `U-ADM!demo` (Administrator — the only role that
   can reach `#notifications`, and the only role the override retry ever
   offers itself to).
2. **Sign-in choices**: sign out to reach `#authScreen`. On a deployment
   with `CAPEX_OIDC_*` configured and a PostgreSQL store, "Continue with
   Zoho" and "Forgot password?" render; without either, neither does
   (`GET /api/auth/providers` is the single source of truth — nothing here
   needs seeding).
3. **Admin override**: no seeded identity in `auth.DEV_USERS` lets an
   Administrator self-approve for real (Administrator holds none of
   `pr.approve` / `revision.approve` / `capitalisation.approve` /
   `approval.act` — see `tests/vrt/admin-override.spec.js`'s header
   comment for the full reasoning). To see it live rather than stubbed: an
   Administrator who ALSO holds an approve permission (e.g. a UAT identity
   with `["Administrator", "ProcurementApprover"]`) raises then approves
   their own purchase request.
4. **Exports**: open any of the fourteen screens in the table above and
   click "Export CSV" / "Export XLSX" — this needs `CAPEX_DB_URL` (or
   `CAPEX_DB_HOST`) configured, since `app/backend/pg/exports.py` is
   PostgreSQL-only.
5. **Notifications**: the identity area's "Notifications" button works for
   any signed-in user; the Governance → Notifications screen needs
   `admin.reset`. `POST /api/notifications/dispatch` needs a configured
   mail adapter (`CAPEX_MAIL_ADAPTER`) to actually send — with none
   configured, the monitoring strip's `adapter_available: false` and
   `adapter_reason` explain why, rather than silently doing nothing.

## Tests

New specs (no screenshots), run individually per spec-file convention:

- `tests/vrt/signin-choices.spec.js` — **15 passed**
- `tests/vrt/admin-override.spec.js` — **2 passed**
- `tests/vrt/export-button.spec.js` — **3 passed** (caught and fixed a real
  bug in `export-button.js`: `setButtonsForIdle()` ran AFTER the
  state-specific branches and re-hid the Retry button the instant a job
  failed)
- `tests/vrt/notifications.spec.js` — **4 passed**
- `tests/vrt/spa-routing.spec.js` — **50 passed, 5 failed** (VRT baseline
  drift from the shellbar change — see above; not a behavioural failure)
- `tests/vrt/nav-groups.spec.js` — **8 passed**

`node --check` passed on every `.js` file this branch touched.
`tests/vrt/evidence/vrt-inventory.json` regenerated
(`CAPEX_VRT_WRITE_INVENTORY=1 npx playwright test tests/vrt/vrt-inventory.spec.js
--project=desktop-1440`): the four new spec files are listed, no baseline
added or removed.

`python -m pytest tests/test_security_frontend.py
tests/test_frontend_budget_setup_registry.py -q`: **160 passed, 1 failed**.
The one failure, `test_nav_groups_are_collapsible_buttons`, asserts nav
group expand/collapse state is persisted via `sessionStorage`; the actual
(and, per `app.js`'s own Stream F comment, deliberate) implementation uses
`localStorage` — "the choice survives past the tab that made it". This
mismatch predates this branch: verified against `app/frontend/app.js` at
`c15df5e` directly (`git show c15df5e:app/frontend/app.js`), before any
commit on this branch, where `NAV_GROUP_STATE_PREFIX` already reads/writes
`localStorage`. Neither this test file nor `app.js`'s nav-group code was
touched by this branch; left as found, per "do not weaken, skip or delete
tests" and "do not modify Python" (the test is Python; the mismatch is
between the test and Stream F's own prior JS, neither of which this
branch's brief covers).

## Left undone, and why

- No approve/reject control was added to `budget-revision-request.js` /
  `budget-transfer.js` — see the Stream B section above.
- `budget_revisions` / `budget_transfers` export filters are `{}`
  (caller's full scope) on the two closure control-view screens, not the
  screen's own project/status filter — see the Stream C section above.
- The audit trail (both the legacy `V.audit` and the PG-backed
  `audit-trail.js`) offers no export control: `audit_entries` is not a
  registered export dataset.
- `V.check` (Budget Availability Check) has no export button: it is a
  single simulate-and-show-a-result tool, not a register/report/tabular
  screen, and `budget_ledger_cells` is already covered via `V.budget`.
