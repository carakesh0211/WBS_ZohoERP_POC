# Zoho ERP demo organisation — connection preparation (Fable 5.1)

**Status (2026-09-11): CONNECTED, READ-ONLY, against organisation
`60074128927` "DEMO WBS" only.** The product owner authorised a Self Client on
the India API console and exchanged its grant code on their own machine with
`tools/erp_demo/connect.py` (hidden prompts; the credential file lives outside
the repository; no value has been printed, logged or committed). The live
transport (`app/backend/integration/live_transport.py`) is the only component
that can reach a tenant; it is never a default, refuses every method but GET
while `CAPEX_ERP_OUTBOUND_WRITES` is unset, refuses any host but
`.zohoapis.in`, any product but ERP, and any scope the grant lacks, and holds
the 100/minute ceiling. **No write has been made and none is enabled.**

## What the read-only probe found (`tools/erp_demo/probe.py --org 60074128927`)

Evidence: `docs/fable51/evidence/erp-demo/probe-60074128927-20260911T111207Z.json` (counts, ids, statuses and scope names only; the
21 other organisations the grant can see are counted, never named).

| Check | Result |
|---|---|
| Data centre / product | `https://www.zohoapis.in` + `/erp/v3` — India, ERP v3, confirmed by the organisations call |
| Pinned organisation | `60074128927` DEMO WBS, INR, India, TRIAL plan, live org type, not multi-entity |
| Granted scopes vs the required READ set (12) | all 12 present, none missing |
| Token | refreshed from the stored refresh token; 3,600 s lifetime; one mint per hour |
| Vendors, items, purchase orders, receives, bills | **the organisation is empty**: 0 of each on page 1, `has_more` false — every path answered 200 through the real adapter, so mapping, pagination and the windowed filters are exercised but cannot yet be verified against records |
| Call budget | 5 calls for the whole probe |
| `cf_capex_ref` on purchase orders (step 12) | **absent**. The purchase-order entity carries `cf_mpn`, `cf_drawing_reference`, `cf_technical_spec`; bills carry no custom field; there are no line-level purchase-order custom fields; the custom-field listing exposes no uniqueness flag. This is the client conversation the plan anticipated, not an engineering workaround |
| Reporting tags | `GET /settings/reportingtags` answers 404 on this tenant; line-level WBS/budget dimensions therefore have no confirmed home yet |

Two facts learned on the first live run and now pinned by tests: the transport
must add the `?` before the query (the first run asked for
`/contactsorganization_id=…` and got 404 "Invalid URL Passed"), and a READ
grant must satisfy a GET the adapter names as `ERP.purchaseorders.ALL`. The
settings endpoints additionally need the `X-com-zoho-erp-organizationid`
header when the user belongs to several organisations (Zoho code 6024).

## The demo organisation is loaded (2026-09-11, later) and the probe passes on data

The product owner loaded synthetic records through the Zoho MCP server
(158 ERP tools over ten groups selected in the MCP console; every record is
labelled SYNTHETIC in its name or notes). Nothing was written through the
application; its outbound gate is still closed.

| Loaded | Count | Notes |
|---|---|---|
| Vendors | 4 | three Indian, one overseas (Japan) |
| Items | 8 | 7 goods + 1 service, purchase items, non-taxable (the org is not GST-enabled) |
| Purchase orders | 5 | all issued; totals ₹35.0 L, ₹56.2 L, ₹125.0 L, ₹48.0 L, ₹28.5 L; reference numbers carry the WBS code |
| Purchase receives | 2 | one partial (PO-00001), one full (PO-00002) |
| Vendor bills | 4 | two partial, one full from the receive, one against the overseas vendor |

Second probe (`docs/fable51/evidence/erp-demo/probe-60074128927-20260911T120549Z.json`): vendors 4, items 8, purchase orders 5
(statuses raw: partially_billed 3, open 1, billed 1), receives discovered
PO-anchored (2 of 5 orders carry one), bills 4, one bill hydrated by detail
fetch with its line, 13 calls.

**Two contract facts the first live pages proved, both now pinned by tests:**

1. Zoho sends money as JSON numbers (`"rate": 0.0`). The transport parses
   with `parse_float=str`, so `dto.paise`'s refusal of floats holds
   (`test_live_transport_fable51.py`).
2. The ERP v3 **list** rows of `/bills` and `/purchaseorders` carry `total`
   and no `sub_total` / `tax_total`; the **detail** rows carry all three. The
   cassettes assumed the detail shape on the list. A list row (discovery
   only, `lines_hydrated=False`) now maps its subtotal from the total it
   states; a hydrated row without `sub_total` is still refused
   (`test_erp_list_row_shape_fable51.py`, `erp._header_subtotal`).

**Zoho's receive/bill matching rule, observed:** once a receive exists
unbilled, a bill for that order must reference the receive
(`receive_id` / `receive_item_id` on the line); once a bill exists without a
receive, no receive can be added for that order. The seed reflects both
orders of events.

**Still open, both the product owner's:** `cf_capex_ref` (the create call was
blocked by this session's permission classifier; create it under Settings →
Custom Fields → Purchase Orders, label "CAPEX Ref", type string, unique), and
multi-currency (the organisation has INR only, so the overseas order is in
INR; enabling JPY is a Zoho setting).

## What is next, in order

1. ~~Load demo records~~ — done (table above); the probe passes on data.
2. Create `cf_capex_ref` (Unique = ON) on purchase orders in the demo org and
   decide where line-level WBS/budget dimensions live (D-7) — both are client
   decisions recorded in the ADRs, not made here.
3. Run the sweeps against the inbox on the local PostgreSQL demo with the
   connection in LIVE_READ.
4. Only then, and on separate authorisation, one controlled test purchase
   order with `CAPEX_ERP_OUTBOUND_WRITES=1` on that single connection.

---

The product owner has a Zoho ERP demo organisation and authorises preparing
its connection **after** the secure UAT application is hosted (Stage A is
hosted; Stage B is prepared). The 2026-09-11 budget-correction brief
additionally says: *do not connect to the Zoho ERP demo tenant during this
budget correction.* Both are honoured: this document readies the flow and
stops.

## The flow that will be used

Catalyst **Connections** owns the OAuth token lifecycle (refresh into Catalyst
Cache, which suits scale-to-zero). Zoho **ERP is not a default connector**, so
a **Custom Service** is created in the `wbs-capex-uat-fable51` project:

| Setting | Value |
|---|---|
| Data centre | **India** — `https://accounts.zoho.in` for OAuth, `https://www.zohoapis.in/erp/v3` for the API (Zoho ERP is India-only; see `research/20_verified/`) |
| Product / API | **Zoho ERP v3** (`/erp/v3`), never Books or Inventory; a Books cassette can never evidence an ERP claim (contract test `tests/test_integration_product_isolation.py`) |
| Scope list | built from the **module pages**, not the OAuth scope table, because Zoho's own ERP OAuth page omits `ERP.purchasereceives.*` and `ERP.custommodules.ALL` (`references/zoho-boundaries.md`, "Documented Zoho defect") — the full list is `app/backend/zoho.py::required_scopes()`, derived from the verified endpoint inventory, and is what the Connection Setup Wizard screen (SCR integration-setup) displays |
| Redirect | the Catalyst Connections callback for the project |
| Client id / secret | created by the product owner in the Zoho API Console (Server-based application) and entered **only** into the Catalyst Connections dialog — never into chat, files or GitHub |
| Refresh token | issued by the consent screen the product owner completes in their own browser |
| Connector name | one per organisation (`wbs-uat-erp-demo`) so the cached token cannot be overwritten by a second connector |

The application-side record is `integration_connection` (migration 010) with
`product = 'ERP'`, `data_centre = 'IN'`, `mode = 'MOCK'`; the secrets are
`secretref://` pointers, never values (`tests/test_connector.py`).

## The exact manual step that unblocks the next phase

The product owner, signed in to the Zoho demo organisation as an administrator:

1. Zoho API Console → Add client → **Server-based Applications** → homepage
   and redirect as shown in Catalyst → Connections → the custom service.
2. Catalyst Console → `wbs-capex-uat-fable51` → Connections → Create →
   Custom Service → paste the client id and secret **there**, scopes from the
   wizard's list, authorisation URL `https://accounts.zoho.in/oauth/v2/auth`,
   token URL `https://accounts.zoho.in/oauth/v2/token`.
3. Click **Authorize** and complete the consent screen in the browser.
4. Tell the lead "authorised" — nothing else is needed from the owner.

## What happens after authorisation, in this order, read-only

Each step is a screen or a job that already exists; each is executed under
`CAPEX_ERP_OUTBOUND_WRITES` unset and with `zoho.MODE` moved to `LIVE_READ`
for that connection only. Evidence goes to `docs/fable51/evidence/erp-demo/`
sanitised by the same rules as Phase 0B (`probe_invoke.py --self-test`).

1. Confirm India DC and the exact product/API by calling `GET /erp/v3/organizations` and matching the base URL — a Books-shaped response is a stop.
2. Confirm the demo organisation id from that response and write it to `integration_connection.zoho_org_id`.
3. Confirm the granted scopes against `required_scopes()` on the scope validation screen; any missing scope is a stop, not a warning.
4. Test token refresh through Catalyst Connections without printing the token (the screen reports expiry timestamps only).
5. Organisation lookup, read-only.
6. Read vendors (`/contacts`) and items (`/items`, `ERP.settings.*` scope) into `integration_inbox` — sync + governed manual additions, nothing written to Zoho.
7. Read purchase orders with the windowed `last_modified_time` filter and overlap.
8. Read purchase receives **through PO-anchored discovery** (ERP has no list endpoint).
9. Read vendor bills with the windowed filter.
10. Validate mappings, pagination (`page`/`per_page`/`has_more_page`), modification filters, the 100 req/min and 2,000/day budgets (`integration_rate_budget`), and freshness.
11. Reconcile the imported demo records through the sweeps (`sweep_control_totals`, `sweep_completeness`) — no ERP transaction is created.
12. Confirm `cf_capex_ref` exists and is **Unique = ON**, and that line-level WBS/budget dimensions exist as reporting tags or custom fields; if not, that is a client conversation, not an engineering workaround.
13. **Only then** request separate authorisation before enabling outbound PO creation (`CAPEX_ERP_OUTBOUND_WRITES=1` on one connection, `LIVE_WRITE`), starting with a single controlled test PO.

Until step 13 is separately authorised, the outbound gate stays closed and
every emit answers 409 `ERP_WRITES_DISABLED` (`tests/test_erp_write_gate.py`).
