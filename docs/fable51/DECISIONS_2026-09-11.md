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
| 8 | **A receive or bill against a non-INR order is refused with a coded reconciliation exception** until it carries its own currency and rate; nothing is booked at face value. | DONE 2026-09-12 (`6a81f47`, `73fce37`): `FOREIGN_CURRENCY_BASIS_MISSING` (migration 030) joins the five C18 kinds in `sweeps.py`, `pg/integration_store.py` and `pg/procurement.py`. `SweepPoAnchored._attribute` holds a receive against a non-INR `LocalPurchaseOrder` before the linkage even resolves (no `source_paise`, nothing on `grn_line`, nothing in the unattributed bucket); `pg.procurement.record_receive_line` refuses the same case with the same code for a caller that reaches the ledger another way. A bill's own rate (`dto.rate_text`, never a float) travels through `SweepBillDetail._mirror` into `mirror_bill`'s existing `exchange_rate`/`fx_rate_source` wiring; `pg.procurement.mirror_bill` refuses a bill against a non-INR order that does not present that order's currency with a usable rate, and the sweep files the refusal as the reconciliation exception rather than failing the run. Nineteen database-free tests in `tests/test_foreign_order_matching_fable51.py`. NOT WIRED: `app/backend/integration/live_sweep.py`'s `PgSweepStore` (out of scope here — owned by `work/live-sweep`) does not yet populate `LocalPurchaseOrder.currency_code` on `open_purchase_orders`, nor forward `exchange_rate`/`fx_rate_source` on `mirror_bill`, so the live PG-backed sweep does not yet exercise this refusal end to end; the database-free sweep logic and the ledger's own refusal (`pg.procurement`, reachable directly) are both complete and tested. |

## Open items after these decisions, and who holds them

| Item | Holder | State |
|---|---|---|
| Create the Supabase Free project in Mumbai and place the connection values outside the repository | owner | DONE 2026-09-11 (`wbs-capex-uat`, ref `lmljdkluuqpgjboiejro`) |
| Enable GST on DEMO WBS | owner | DONE 2026-09-11; GST vendor/item/PO-00007/bill loaded |
| Enter the ERP refresh token, client id and secret into the Catalyst AppSail environment for Stage B (never chat) | owner, at Stage B deploy | DONE 2026-09-11 (eleven variables in the console; they survived the standalone redeploy of 2026-09-12) |
| GitHub Actions billing (CI has not run since 2026-09-09) | owner | open |
| Stage B launcher: PostgreSQL env pass-through, provider CA at the bundle root, ERP credentials from environment variables, standalone deploy form | engineering | DONE `6d1eb79`; live 2026-09-12 (`STAGE_B_PERSISTENT_UAT.md` deploy record) |
| LIVE_READ wiring: a connection in LIVE_READ mode builds the ERP adapter on the live transport; sweeps run server-side against the inbox | engineering | routes DONE `fc8c07e`/`314ba9f` (organisations, validate, scopes, health live on UAT); the sweep route is in progress (agent, branch `work/live-sweep`) |
| Stamp `cf_wbs_code` / `cf_budget_head` on the seven demo orders; `line_level_custom_fields=True` for ERP | engineering | DONE 2026-09-12: capability `d3d8054`; all seven orders stamped under the owner's written authorisation, audited in `evidence/erp-demo/po-stamping/AUDIT_REPORT_2026-09-12.md`; the live sweep then raised 7 UNSANCTIONED_COMMITMENT exceptions (the §11.7 control proven live) |
| Foreign-order receive/bill matching refusal (decision 8) | engineering | DONE 2026-09-12 (`d620bb8`, `7aa5d1c`); live-sweep wiring (`PgSweepStore` currency/rate pass-through) still open — lead |
| Purchase Order screen: currency selector and source-minor entry (API complete) | engineering | DONE 2026-09-12 (`a6e940a`…`413600d`, Raise Purchase Order screen + `tests/vrt/po-currency.spec.js`) |
| Audit-anchor Cron Function deployed to the UAT project with its job token | engineering + owner (token) | open |
| Backup and restore drill on the free tier (`pg_dump` based) | engineering | open |
| Independent adversarial review of the branch (D1/D2 never completed) | engineering | open |
| Zoho receive/bill matching rule (bill must cite the receive once one exists) reflected in the app's matching model | engineering | open |
