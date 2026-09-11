# Product-owner decisions taken on 2026-09-11 (live UAT round 1)

Each row is a decision the product owner took in conversation, what it
binds, and where it is applied. Nothing here is inferred.

| # | Decision | Applied |
|---|---|---|
| 1 | **Stage B database: Supabase Free tier, Mumbai (ap-south-1), a NEW project.** No paid resource; the existing Supabase project is never touched. Limits accepted: 500 MB, idle pause after 7 days, no automated backups (the restore drill uses `pg_dump`). | Awaits the owner creating the project (steps in `STAGE_B_PERSISTENT_UAT.md`); the launcher/env work is on the engineering list |
| 2 | **Every Zoho ERP demo user is a WBS Administrator**: U-RAKESH, U-PRITHA, U-SURAJ, U-ABHISHEK. | `app/backend/db.py` seed, `auth.DEV_USERS`, credentials rotated outside the repository, Stage A redeployed `8b99c58`; all four sign in live |
| 3 | **A4: accept the administrator's 130px rail scroll** at desktop-1440; every other role fits. | `docs/ui-change-2026-09/A4-fable51-navigation.md` status DECIDED |
| 4 | **The Auditor reads the rate book** (`fx.read`), never `fx.manage`; recorded against D-12 / AUD-C-006. | `auth.PERMISSIONS`, the two Auditor allow-list pins, the FX guard, `tests/vrt/fx-rates.spec.js` |
| 5 | **D-7 resolves TRUE for this tenant: line-level custom fields.** `cf_wbs_code` (id `3912780000000098001`) and `cf_budget_head` (id `3912780000000099001`) exist on purchase-order line items in DEMO WBS. | Fields created; stamping the six orders and switching `Capabilities.line_level_custom_fields` to true for ERP is on the engineering list |
| 6 | **Enable GST on DEMO WBS** so a taxed vendor, item, order and bill can be added. | Awaits the owner (Zoho Settings → Taxes → GST); then the taxed records are loaded |
| 7 | **Ephemeral sessions and shell-side data are acceptable for UAT**: an AppSail restart signs testers out; PostgreSQL-backed data persists. | Documented for testers; no session migration before Stage B |
| 8 | **A receive or bill against a non-INR order is refused with a coded reconciliation exception** until it carries its own currency and rate; nothing is booked at face value. | Engineering list (matching layer + test) |

## Open items after these decisions, and who holds them

| Item | Holder | State |
|---|---|---|
| Create the Supabase Free project in Mumbai and place the connection values outside the repository | owner | open |
| Enable GST on DEMO WBS | owner | open |
| Enter the ERP refresh token, client id and secret into the Catalyst AppSail environment for Stage B (never chat) | owner, at Stage B deploy | open |
| GitHub Actions billing (CI has not run since 2026-09-09) | owner | open |
| Stage B launcher: PostgreSQL env pass-through, provider CA at the bundle root, ERP credentials from environment variables, standalone deploy form | engineering | open |
| LIVE_READ wiring: a connection in LIVE_READ mode builds the ERP adapter on the live transport; sweeps run server-side against the inbox | engineering | open |
| Stamp `cf_wbs_code` / `cf_budget_head` on the six demo orders; `line_level_custom_fields=True` for ERP | engineering | open |
| Foreign-order receive/bill matching refusal (decision 8) | engineering | open |
| Purchase Order screen: currency selector and source-minor entry (API complete) | engineering | open |
| Audit-anchor Cron Function deployed to the UAT project with its job token | engineering + owner (token) | open |
| Backup and restore drill on the free tier (`pg_dump` based) | engineering | open |
| Independent adversarial review of the branch (D1/D2 never completed) | engineering | open |
| Zoho receive/bill matching rule (bill must cite the receive once one exists) reflected in the app's matching model | engineering | open |
