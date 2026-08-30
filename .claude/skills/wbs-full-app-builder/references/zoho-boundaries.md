# Zoho integration boundaries

Verified against official documentation 2026-08-28. **Products are never mixed:
Books or Inventory documentation is inadmissible as evidence for ERP, and vice
versa.** A contract test asserts a Books cassette can never satisfy an ERP
adapter test.

## Purchase Requests stay in WBS

**No Purchase Request or Requisition module exists in the public REST API of
Zoho ERP v3, Books v3 or Inventory v1.** The complete module indexes of all
three were retrieved; `/purchase-requests/`, `/purchaserequests/` and
`/requisitions/` all 404.

WBS is therefore the **system of record for PRs**, and this must not be revisited
unless a verified endpoint is found in the tenant.

**Never infer API support from UI availability.** The client PDF classifies PR
as a "Standard" ERP capability and the client asks for field parity with a PR
screen they can evidently see. Both can be true at once: a module can exist in
the ERP **UI** as standard and still have **no documented public REST API**. The
resolution is "both true", not "one wrong".

The PR is a **pre-commitment control artefact, not an accounting document**.
Nothing in Zoho's ledger needs it. What needs it is `budget_check` — the ability
to refuse a commitment before it becomes one. Move the PR into Zoho and that
refusal becomes advisory and the product's central claim collapses.

## Direction of travel

| Object | Direction | Notes |
|---|---|---|
| Purchase Request | **WBS only** | No API exists |
| Purchase Order | **outbound**, WBS → Zoho | Emitted as `draft`, transitioned to `open` only by our own call after our approval closes |
| GRN / Purchase Receive | **inbound** | ERP has **no list endpoint** — PO-anchored discovery only |
| Vendor Bill | **inbound** | Windowed `last_modified_time` filter + overlap |
| Item, Vendor master | **inbound** | Sync + governed manual additions |

## Outbound PO idempotency

**Zoho documents no idempotency header.** We synthesise one:

- `dedupe_key` written to a Zoho unique custom field **`cf_capex_ref`**
- retries use update-by-custom-field-unique-value
- a Function killed after sending but before recording **updates** rather than
  duplicates
- everything goes through an **outbox** — never a direct call from a request
  handler

`cf_capex_ref` must be **Unique = ON** in the tenant. Without it a retried send
creates a duplicate PO. This is too critical to delegate and is delivered by the
project, not the client.

Chaos test: kill mid-send 100× → **zero duplicate POs**.

## Adapters

One interface, two implementations, selected by
`integration_connection.product`. The target product is **provisional** pending
D-14 — a client saying "Zoho ERP" may be running Books + Inventory, which have
different base URLs, scope prefixes, DC availability and endpoint coverage.

```python
class ProcurementAdapter(Protocol):
    product: Literal["ERP", "BOOKS_INVENTORY"]
    def base_url(self, dc: str) -> str: ...
    def scopes_required(self) -> frozenset[str]: ...
    def list_bills(self, since, until, page) -> Page[BillDTO]: ...
    def get_bill(self, external_id: str) -> BillDTO: ...
    def list_purchase_orders(self, since, until, page) -> Page[PurchaseOrderDTO]: ...
    def create_purchase_order(self, po, dedupe_key: str) -> str: ...
    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]: ...
    def capabilities(self) -> Capabilities: ...
```

`Capabilities` declares what the product **cannot** do, and the scheduler reads
it rather than assuming:

```python
receives_listable        # ERP: False.  Inventory: True
bills_delta_filter       # both: True
po_delta_filter          # ERP/Books: True.  Inventory: False
items_delta_filter       # Inventory: True.  ERP/Books: False
line_level_custom_fields # D-7, tenant-verified
daily_call_ceiling       # plan-derived, tenant-verified
```

DTOs are **ours**, not Zoho's. Raw payloads are preserved in
`integration_inbox` with the product and API version that produced them, so a
mapping error is always traceable to a source document.

## Delta strategy per endpoint

| Endpoint | Filter | Sortable | Strategy |
|---|---|---|---|
| ERP/Books `GET /bills` | **yes** | no | windowed filter + 300 s overlap |
| ERP/Books `GET /purchaseorders` | **yes** | no | windowed filter + overlap |
| ERP `GET /purchasereceives` | **no list endpoint** | — | **PO-anchored only** |
| Inventory `GET /purchasereceives` | no | yes | sort-descending walk, early stop |
| Inventory `GET /purchaseorders` | no | no | full re-pull |
| `GET /contacts` | no | yes | sort walk or weekly refresh |
| Inventory `GET /items` | **yes** | **yes** | true delta |
| ERP/Books `GET /items` | no | limited | weekly full refresh |

`last_modified_time` is **filterable but not sortable** on ERP/Books bills and
POs, so a stable resumable keyset walk on modification time is **not available**.
That is why overlap and completeness sweeps are retained in full — a filter
cannot prove it returned everything.

## PO-anchored GRN discovery (ERP)

With no list endpoint, the only viable pattern is walking locally-open POs and
reading the receive references on each PO detail. Consequences, stated plainly:

- a receive against a PO **we do not know about** is undiscoverable — it
  surfaces only when its bill arrives, as `UNSANCTIONED_COMMITMENT`
- a **deleted** receive arrives as an absence; only a re-read detects it
- cost scales with **open PO count**, not receive volume

On ERP Standard (**2,000 calls/day**) this is the binding constraint. If sizing
exceeds it, GRN sync frequency is a **client conversation**, not an engineering
workaround.

## Rate limits, retry, circuit breaker

100 req/min/org across all three products. Daily: ERP Standard 2,000 /
Premium 10,000.

Rate budget lives in PostgreSQL (`integration_rate_budget`, upserted with
`RETURNING`) because no process is resident. Allocation per org: **60 polling /
30 outbound / 10 interactive**, so an operator clicking "Test connection" never
starves behind a backfill.

| Condition | Action | Counts toward circuit? |
|---|---|---|
| 429 code **44** (per-minute) | checkpoint, resume next tick | **no** — our throttle failed, not the vendor |
| 429 code **45** (daily quota) | open circuit until day boundary, alert | no |
| 429 code **1070** (concurrency) | reduce parallelism by 1, retry with jitter | no |
| 5xx / timeout | exponential backoff, full jitter | **yes** |
| 401 / invalid_token | refresh once; second failure → open circuit + P1 | yes, immediately |
| 4xx business error | no retry → QUARANTINED/DEAD + exception | no |

Backoff `next_attempt_at = now() + random(0, min(900s, 2s · 2^attempts))`,
`max_attempts=8`, then DEAD with manual retry. Circuit: 5 consecutive counted
failures in 60 s → OPEN 60 s → HALF_OPEN single probe.

## Completeness

Inbound idempotency is free:
`integration_inbox UNIQUE (connection_id, module, external_id, payload_sha)`.

Sweeps are **retained even when a delta filter works**, because a filter cannot
prove completeness:

- `sweep_control_totals` — per (module, period, status): count + sum, source vs
  local → `CONTROL_TOTAL_MISMATCH`
- `sweep_completeness` — document-number gap detection; late arrivals into a
  closed period → `LATE_ARRIVAL_CLOSED_PERIOD`

Exit criterion for the integration milestone: **7 consecutive days of clean
control-total reconciliation**.

## Honesty rules

- `zoho.MODE` per connection: `MOCK | SANDBOX | LIVE_READ | LIVE_WRITE`
- **No live call without authorised sandbox credentials.** Not "to check", not
  "read-only just once".
- **Never mark a path LIVE or VERIFIED without authorised sandbox evidence.**
  `MOCK` is an honest state; a false `VERIFIED` is a lie the whole product rests
  on.
- Anything unconfirmed is labelled `UNVERIFIED — REQUIRES ZOHO CONFIRMATION` in
  code, UI and documents.
- `LIVE_READ` before `LIVE_WRITE`, always.
- Webhooks are an **optimisation only** — no standalone registration API exists
  in any of the three products. **Polling is never disabled because a webhook
  exists.**

## Documented Zoho defect to work around

Zoho's own ERP OAuth page omits two scopes that appear on the module pages:
`ERP.purchasereceives.*` and `ERP.custommodules.ALL`. Build the consent scope
list from the **module pages**, not the scope table, and re-verify at the tenant.
