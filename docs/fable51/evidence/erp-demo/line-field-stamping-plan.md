# Stamping `cf_wbs_code` / `cf_budget_head` on the DEMO WBS purchase-order lines

**State (2026-09-12, later): APPLIED under the owner's written authorisation; see `po-stamping/AUDIT_REPORT_2026-09-12.md` (before/after snapshots, diff, tests, rollback).** The text below is the plan as it stood before.

~~**State (2026-09-12): NOT APPLIED.**~~ The two line fields exist on DEMO WBS
(entity `purchaseorder_item`; `cf_wbs_code` id `3912780000000098001` index 10,
`cf_budget_head` id `3912780000000099001` index 11 — read live via the Zoho
ERP MCP `list_custom_fields`). Every line of the seven demo orders still has
`item_custom_fields: []`.

The write was attempted twice through the Zoho ERP MCP tool
`update_purchase_order` and was refused both times by the Claude Code auto-mode
classifier (not by Zoho). It is a live write to the DEMO tenant, so it is not
retried from the app either: the app's `CAPEX_ERP_OUTBOUND_WRITES` gate stays
unset by decision, and this must not be the write that opens it.

## The exact payloads, ready to send

`PUT /erp/v3/purchaseorders/{id}?organization_id=60074128927`, body
`{"vendor_id": ..., "line_items": [...]}` where each line carries its
`line_item_id`, `item_id`, `account_id` (`3912780000000000465`), `description`,
`item_order`, `rate`, `quantity` exactly as read on 2026-09-12, plus:

```
"item_custom_fields": [{"index": 10, "value": "<WBS>"}, {"index": 11, "value": "<budget head>"}]
```

| Order | purchaseorder_id | vendor_id | line_item_id | WBS (`cf_wbs_code`) | Budget head (`cf_budget_head`) |
|---|---|---|---|---|---|
| PO-00001 | 3912780000000085001 | 3912780000000075001 | 3912780000000085004 (switchgear, rate 1850000 × 2) | CAPEX-2026-001.04 | Electrical |
| PO-00001 | | | 3912780000000085005 (XLPE cable, rate 2400 × 800) | CAPEX-2026-001.04 | Electrical |
| PO-00002 | 3912780000000085009 | (read refused by the classifier; take from the order) | (as read) | from the order's notes | from the order's notes |
| PO-00003 | 3912780000000075008 | 3912780000000076001 | 3912780000000075011 (EOT crane, 4800000 × 1) | CAPEX-2026-001.03.02 | Plant & Machinery |
| PO-00004 | 3912780000000086001 | 3912780000000079001 | 3912780000000086004 (PLC sets, 950000 × 3) | CAPEX-2026-001.07 | Instrumentation & Automation |
| PO-00005 | 3912780000000087001 | 3912780000000074001 | 3912780000000087004 (structural steel, 68000 × 40) | CAPEX-2026-001.02.02 | Civil |
| PO-00005 | | | 3912780000000087005 (RMC M30, 5200 × 150) | CAPEX-2026-001.02.01 | Civil |
| PO-00006 (JPY) | 3912780000000096001 | 3912780000000095001 | 3912780000000096004 (PLC sets, ¥1450000 × 2) | CAPEX-2026-001.07 | Instrumentation & Automation |
| PO-00007 (GST) | 3912780000000099012 | 3912780000000099002 | 3912780000000099013 (platforms, 640000 × 3, tax_id 3912780000000085140) | CAPEX-2026-001.02.02 | Civil |

The values come from each order's own `reference_number` and `notes`, written
when the data was loaded; nothing is invented here.

Cautions from the live facts already recorded: PO-00001, PO-00004, PO-00005
and PO-00007 are partially billed and PO-00001 partially received — send the
line's existing `rate` and `quantity` unchanged so Zoho sees a custom-field
edit and not a quantity change; PO-00007 must keep `tax_id` on its line; the
JPY order keeps its `rate` in JPY.

## After it is applied

Re-read each order and confirm `item_custom_fields` carries both values, then
record the result here. `Capabilities.line_level_custom_fields` is already
TRUE for organisation 60074128927 (`erp.VERIFIED_LINE_CUSTOM_FIELDS`, commit
`d3d8054`), so the sweep and the emission planner read the line dimensions
the moment the tenant carries them.
