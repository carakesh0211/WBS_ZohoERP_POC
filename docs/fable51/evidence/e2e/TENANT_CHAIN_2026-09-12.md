# UAT end-to-end cycle — the tenant side (Zoho ERP DEMO WBS, org 60074128927)

Under the owner's controlled write authorisation of 2026-09-12 (one order, one
receive, one bill; existing demo vendor and item; no payment, credit, bank
transaction, vendor, item, tax or unrelated order touched; no other
organisation). All three writes went through the Zoho ERP MCP connection the
owner holds; the app's own outbound gate stayed shut and its credential is
read-only. No credential appears anywhere.

## The WBS side it answers to (see `raise-*.json`, `raise-continued-*.json`)

| Step | WBS record | Outcome |
|---|---|---|
| 0 | over-budget probe (2 × Rs 18,50,000 on WBS-A-ELEC / Electrical) | **409 RESERVATION_EXCEEDS_BUDGET**, nothing created |
| 1 | PR-0001 (`PR-223A9C8AB6F5`), 1 × Rs 18,50,000, raised by U-REQ (Requestor) | 201, `check_result: WITHIN_BUDGET` |
| 2 | availability on the cell | budget Rs 40,00,000; exposure Rs 18,50,000; available Rs 21,50,000; verdict CRITICAL, shortfall 0 |
| 1b | submitted by U-REQ | Submitted |
| 3 | approved by U-PLH (ProcurementApprover; maker ≠ checker) | Approved, `exception: false` |
| 3b | converted by U-PLH to order `PO-15C1EEC1E554`, number **WBS-UAT-E2E-20260912**, INR, Rs 18,50,000 | 201 |
| 4a | dedupe key the emission would carry as `cf_capex_ref` | `CAPEX-purchaseorders-PO-15C1EEC1E554-d7c8a5bde09810f9` |
| 4b | `POST …/emit` (U-RAKESH, connector.manage) | **409 ERP_WRITES_DISABLED** — "Nothing was planned or queued" |
| 4c | identical retry | 409 again; nothing sent, nothing duplicated |

**Manual step reported, not worked around:** emitting the order from the app
needs `ERP.purchaseorders.CREATE` on the credential (the grant holds READ
scopes only) plus `CAPEX_ERP_OUTBOUND_WRITES=1` on the AppSail and the
connection in LIVE_WRITE. None of the three was changed today. The tenant
order below was therefore created by the owner's MCP connection, carrying the
app's own dedupe key so the app can link it (adoption LINK mode).

## The tenant records (all `reference_number` = WBS-UAT-E2E-20260912)

| Record | Zoho id | Number | Facts |
|---|---|---|---|
| Purchase order | `3912780000000109005` | PO-00008 | vendor Kalinga Electricals `3912780000000075001`; line `3912780000000109007` item DEMO-SWGR-11KV `3912780000000076008` × 1 @ ₹18,50,000; `cf_capex_ref` = the dedupe key above; line `cf_wbs_code` CAPEX-DEMO-001.03, `cf_budget_head` Electrical; created 2026-09-12T15:04:55+0530 as draft, marked Issued at 15:05 |
| Purchase receive (GRN) | `3912780000000116003` | WBS-UAT-E2E-GRN-20260912 | against PO-00008 line `3912780000000109007`, qty 1, received 2026-09-12; receive line `3912780000000116008` |
| Vendor bill | `3912780000000117004` | WBS-UAT-E2E-BILL-20260912 | vendor Kalinga; line `3912780000000117008` cites `purchaseorder_item_id 3912780000000109007`, `receive_id 3912780000000116003`, `receive_item_id 3912780000000116008`; ₹18,50,000; due 2026-10-12; PO-00008 now `billed` / order closed |

**Not executed:** a second order carrying the same `cf_capex_ref` (the Z-01
duplicate probe) — the auto-mode classifier refused that create; the app-side
identical-retry proof (4c) and the field's uniqueness (`is_unique` on
`cf_capex_ref`, recorded when the field was created) stand.

## What the app must now show (verified after the consolidated redeploy)

1. The poll pulls PO-00008; without a link it is an UNSANCTIONED_COMMITMENT.
2. Adoption LINK mode: PO-00008's key equals the local order's derived key →
   `PO-15C1EEC1E554.external_id = 3912780000000109005`, exception resolved.
3. The PO-anchored walk mirrors WBS-UAT-E2E-GRN-20260912 as the local GRN.
4. The bill sweep matches WBS-UAT-E2E-BILL-20260912 to the local order.
5. Ordered ₹18,50,000 = received ₹18,50,000 = billed ₹18,50,000; open 0.
6. The ELEC cell's exposure carries the commitment/actual in INR paise.
7. Audit chain verifies.
