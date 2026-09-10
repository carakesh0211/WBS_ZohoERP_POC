# Zoho ERP demo organisation — connection preparation (Fable 5.1)

**Status: PREPARED. STOPPED at the manual OAuth authorisation, as the brief
requires. No call has been made to any Zoho tenant. `zoho.MODE` is `MOCK`,
`CAPEX_ERP_OUTBOUND_WRITES` is unset (= disabled) everywhere, and nothing on
any branch is labelled LIVE or VERIFIED.**

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
