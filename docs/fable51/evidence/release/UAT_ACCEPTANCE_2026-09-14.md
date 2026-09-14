# UAT acceptance pass — 2026-09-14

Deployed application `63b07c6` (deploy record 15), https://wbs-capex-uat-50045784768.development.catalystappsail.in,
Supabase `capex_tmpl_uat`. Synthetic testers only; no ERP purchase order was created; the standing LIVE_WRITE
organisation boundary (60074128927) was not touched; no other database, project, service or organisation
was touched. Every figure below was read from the instance on 2026-09-14 (IST morning).

## Health

| Check | Result |
|---|---|
| `/` | 200 |
| `/healthz` | 200 |
| `/readyz` | 200, `schema_version` **036** (applied 2026-09-13 20:14 UTC) |
| `/api/health` | ok, version `0.2.0-poc-hardened`, profile `uat-preview`, `zoho_mode` LIVE_WRITE, `outbound_writes_enabled` true (the standing state, unchanged) |
| `/docs` | 404 |
| Banner | `UAT — SYNTHETIC DATA — ZOHO ERP DEMO TENANT 60074128927 — WRITES ENABLED` |
| `/api/auth/providers` | local enabled with forgot-password; Zoho `enabled: false` (not configured — the button is hidden) |

## Role smoke (`smoke-roles-acceptance-2026-09-14.json`)

13 identities signed in and were checked against the route permission matrix: U-REQ 52/52, U-PM 53/53,
U-PLH 52/52, U-PROC 52/52, U-FIN 53/53, U-PFC 53/53, U-CFO 53/53, U-AUD 67/67, and the five Administrators
(U-ADM, U-RAKESH, U-PRITHA, U-SURAJ, U-ABHISHEK) 69/69 each. No credential appeared in any response.

## Feature smoke (browser and API, as U-ADM unless stated)

| Feature | What was done | Result |
|---|---|---|
| Local sign-in | browser sign-in as U-ADM; 13 API sign-ins in the role smoke | shell rendered; all 13 succeeded |
| Forgot password | `POST /api/auth/forgot` for synthetic `U-REQ`, and for a non-existent id | both 202 with the same generic sentence; one `PASSWORD_RESET_REQUESTED` outbox row; body REDACTED in the Administrator's outbox detail (61 characters, no token) |
| Collapsible navigation | read every group's `aria-expanded`; clicked Closure; read storage; clicked Expand all | 8 groups, Work/Project Control open by default; Closure false → true; ONE `sessionStorage` key `capex.nav.group.U-ADM.Closure`, NO localStorage key; expand-all opened every group; 42 nav items |
| Internal fulfilment | `PUT …/lines/{line}/fulfilment` SPLIT_FULFILMENT (2 of 5 internal, manual valuation with note) on a synthetic PR line; the decision raised `IMR-0001`; approve as maker; approve with override; cancel | decision 200 (internal 2, external 3); IMR-0001 REQUESTED; maker refused 403 `SELF_APPROVAL`; override 200 APPROVED; cancel 200 CANCELLED, no movements remain; IMR register screen mounts with two export captions |
| Administrator override | synthetic PR `PR-B72A0B61E58E` (₹5,000, WBS-A-ELEC / BH-DM1-ELEC) raised and submitted by U-ADM (routed Exception Pending: the cell is over budget); approve as maker; approve with a ≥10-character reason | 403 `SELF_APPROVAL`, then 200 Approved; audit trail holds two `ADMIN_SELF_APPROVAL_OVERRIDE` entries (PurchaseRequest, InternalMaterialRequest); four override notifications queued for U-RAKESH, U-PRITHA, U-SURAJ, U-ABHISHEK |
| XLSX download | `purchase_requests` export created, driven through `/advance`, downloaded (`exports-2026-09-14/uat-purchase_requests.xlsx`) | `application/vnd.openxmlformats…sheet`, four sheets, typed cells, UTC datetimes, Indian money mask, SUM totals; the register shows two captioned controls (headers, lines) |
| Notification outbox | Administrator `POST /api/notifications/dispatch` on 3 queued rows; a second and third dispatch; the preferences dialog; the Governance outbox screen | 3 claimed / 3 SENT via the recording adapter, one delivery row each, later dispatches claimed 0; dialog opens with 9 event toggles; outbox screen mounts with state/event/recipient filters |

## ERP, read-only

`tools/uat/e2e_after_deploy.py` (`e2e/after-deploy-2026-09-14T072710.json`): sweeps DONE/COMPLETED (contacts 6,
items 9 records seen), adopt-orders adopted 0 / linked 0 / skipped 7 / the same two exceptions, idempotent on
repeat; reconciliation 10 rows; cell exposure read; control totals 503 `CONTROL_TOTALS_UNAVAILABLE` (by
design); audit chain verify `NEVER_ANCHORED` (the anchor Cron is the owner's blocker — verification itself
unchanged); 8 unattributed lines. No purchase order was raised, converted or emitted.

## Not proven here, by design

Zoho sign-in (no client configured), real e-mail delivery (recording adapter, no verified sender), the
break-glass account, the dispatch ticker, the audit anchor. Each is an owner action in
`docs/fable51/UAT_OWNER_ENABLEMENT_2026-09-14.md`.
