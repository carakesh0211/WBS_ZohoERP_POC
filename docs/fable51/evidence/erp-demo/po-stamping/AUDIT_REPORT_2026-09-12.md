# Audit report — custom-field stamping on the DEMO WBS demo purchase orders

**Authorisation.** Given by the product owner in conversation on 2026-09-12:
controlled write operations only in Zoho ERP demo organisation "DEMO WBS"
(organisation id `60074128927`), only on the identified demo purchase orders,
only the fields `cf_capex_ref`, `cf_wbs_code`, `cf_budget_head`; no bills,
GRNs, payments, vendors or items; before-state record, idempotent updates,
verification, the approved sync/reconciliation tests, this report and
rollback instructions.

**Scope actually touched.** Seven purchase orders, PO-00001 … PO-00007, and
nothing else. No other organisation, module or record was read for writing
or written. No credential appears in any file or transcript: the reads went
through the app's `LiveTransport` (credential file outside the repository)
and the writes through the Zoho ERP MCP connection the owner holds.

## 1. Organisation verified before any write

`GET /organizations` answered organisation `60074128927` with name
`DEMO WBS`, base currency INR (recorded in the before-state file). The
snapshot tool refuses to continue if that check fails.

## 2. Before-state record

`before-2026-09-11T212010.json` (taken 2026-09-11T21:20:10Z, read-only):
every order's id, vendor, status, currency, total, `last_modified_time`,
header custom fields and, per line, `line_item_id`, `item_id`, `account_id`,
`rate`, `quantity`, `tax_id`, received and billed quantities and line custom
fields. Before the writes: PO-00001…PO-00005 carried no header custom field;
PO-00006 and PO-00007 already carried `cf_capex_ref`; **no line on any order
carried `cf_wbs_code` or `cf_budget_head`.**

## 3. Writes performed (Zoho ERP MCP `update_purchase_order`, one call per order)

Each call sent the order's `vendor_id`, the header `custom_fields` (only
`cf_capex_ref`, id `3912780000000093002`) and every existing line by its
`line_item_id` with the SAME `item_id`, `account_id`, `description`,
`item_order`, `rate`, `quantity` (and `tax_id`/`hsn_or_sac` on PO-00007) as
read, plus `item_custom_fields` index 10 (`cf_wbs_code`) and index 11
(`cf_budget_head`). Sending the same body again produces the same state, so
every call is idempotent.

| Order | Zoho id | `cf_capex_ref` (header) | Line(s) → `cf_wbs_code` / `cf_budget_head` | Zoho answer |
|---|---|---|---|---|
| PO-00001 | 3912780000000085001 | DEMO-CAPEX-2026-001.04-INR-0001 (new) | …85004, …85005 → CAPEX-2026-001.04 / Electrical | updated 2026-09-12T02:50:46+0530 |
| PO-00002 | 3912780000000085009 | DEMO-CAPEX-2026-001.03.01-INR-0001 (new) | …85012 → CAPEX-2026-001.03.01 / Plant & Machinery | updated 2026-09-12T08:41:54+0530 |
| PO-00003 | 3912780000000075008 | DEMO-CAPEX-2026-001.03.02-INR-0001 (new) | …75011 → CAPEX-2026-001.03.02 / Plant & Machinery | updated 2026-09-12T02:51:07+0530 |
| PO-00004 | 3912780000000086001 | DEMO-CAPEX-2026-001.07-INR-0001 (new) | …86004 → CAPEX-2026-001.07 / Instrumentation & Automation | updated 2026-09-12T02:51:12+0530 |
| PO-00005 | 3912780000000087001 | DEMO-CAPEX-2026-001.02-INR-0001 (new) | …87004 → CAPEX-2026-001.02.02 / Civil; …87005 → CAPEX-2026-001.02.01 / Civil | updated 2026-09-12T02:51:41+0530 |
| PO-00006 (JPY) | 3912780000000096001 | DEMO-CAPEX-2026-001.07-JPY-0001 (unchanged) | …96004 → CAPEX-2026-001.07 / Instrumentation & Automation | updated 2026-09-12T08:41:44+0530 |
| PO-00007 (GST) | 3912780000000099012 | DEMO-CAPEX-2026-001.02.02-GST-0001 (unchanged) | …99013 → CAPEX-2026-001.02.02 / Civil | updated 2026-09-12T08:41:49+0530 |

The WBS codes and budget heads are the ones each order's own
`reference_number` and `notes` already stated; the new `cf_capex_ref` values
follow the pattern of the two that existed.

The sequence was interrupted once (PO-00001, -03, -04, -05 first; the owner
paused; PO-00006, -07, -02 after "start"). The pause changed nothing: each
update is independent and idempotent.

## 4. Verification (read-only, after-state)

`after-2026-09-12T031246.json` and `python tools/erp_demo/po_snapshot.py diff`:

- **14 custom-field changes, exactly the intended ones** (5 header
  `cf_capex_ref` additions + 9 line stampings across the 9 lines).
- **Zero unauthorised changes**: for every order, `status`, `currency_code`,
  `total`, `vendor_id`, line count and, per line, `item_id`, `rate`,
  `quantity`, `tax_id`, `quantity_received`, `quantity_billed` are identical
  before and after. (The diff prints any such change prefixed `!!`; none was
  printed.)

## 5. Approved sync / reconciliation tests

Local (PostgreSQL 16, no network), all green:
`tests/test_live_sweep_fable51.py`, `tests/test_integration_sweeps.py`,
`tests/test_erp_list_row_shape_fable51.py`, `tests/test_live_transport_fable51.py`,
`tests/test_integrations_live_routes_fable51.py`,
`tests/test_integration_adapter_contract.py`, `tests/test_integration_capabilities.py`
→ **242 passed**.

Live on UAT (`live-sweep-after-stamping-2026-09-12.txt`): one sweep round as
U-RAKESH pulled the 7 modified orders (new payload hash → re-accepted) and the
poll raised **7 `UNSANCTIONED_COMMITMENT` exceptions, one per order**
(EXC-BBEE13DD2175, EXC-5FB22649AD06, EXC-C3910B8D4A4B, EXC-752A458C26B8,
EXC-4E606BEB0753, EXC-9813326166DD, EXC-678656FFA6A5), each naming the order
and its CAPEX reference — the detective control of §11.7 proven live after
the fix in `6a3987c`. They sit in the unattributed queue for triage. Four GET
calls, zero writes from the app; the app's write gate stayed unset.

**Finding recorded, not smoothed over:** the JPY order's exception carries
`source_paise = 2900000` — the order's JPY total in minor units labelled as
paise. Nothing books it, but the label is wrong for a foreign order; the
decision-8 stream (foreign-currency basis) must give the exception record the
source currency and minor units, or translate through the rate book. **Closed the same day:** `sweeps._face_value_paise` (decision-8 stream) leaves `source_paise` NULL for a non-INR order, and the lead's follow-up names the face value with its currency in `detail`; pinned in `tests/test_foreign_order_matching_fable51.py`. The one live row raised before the fix (EXC-9813326166DD) still carries 2900000 and should be resolved with a note citing this report.

## 6. Rollback instructions (exact)

To restore the before-state for any order, send the same
`update_purchase_order` call with the line's existing facts and the custom
fields cleared:

```
custom_fields: [{"customfield_id": "3912780000000093002", "value": ""}]      # PO-00001..05 only; PO-00006/07 keep their original value
line_items[*].item_custom_fields: [{"index": 10, "value": ""}, {"index": 11, "value": ""}]
```

The exact `vendor_id`, `line_item_id`, `item_id`, `account_id`, `rate`,
`quantity`, `tax_id` and `hsn_or_sac` to send back are in
`before-2026-09-11T212010.json`. Then run `python tools/erp_demo/po_snapshot.py after`
and `diff` against the before-state: it must print `0 custom-field change(s)`.
Rolling back does not remove the 7 exceptions on the UAT database; resolve
them on the reconciliation screen with a note citing this report.
