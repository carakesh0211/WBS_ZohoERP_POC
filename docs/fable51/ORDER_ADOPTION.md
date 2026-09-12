# Adopting a tenant-raised Zoho order

Product owner decision, 2026-09-12: "Adopt the seven tenant-raised Zoho demo
orders into local WBS orders using their stamped line fields."

## Why this exists

`STAGE_B_PERSISTENT_UAT.md`, "Findings from the first live sweeps"
(2026-09-12), finding 1: `sweeps.SweepPoAnchored` -- on Zoho ERP, the *only*
way a goods receipt is ever discovered -- anchors its receive walk on
`purchase_order` rows whose `external_id` is set **for this connection**
(`plan §2.2`: on ERP a receive is reachable only through its order). The seven
DEMO WBS orders (PO-00001 .. PO-00007) were created directly in the tenant,
not emitted by this system, so the walk found nothing to anchor on and the
tenant's own receives (DEMO-GRN-0001/0002) were never mirrored.

Adoption is finding 1's option "(a)": create the local `purchase_order` (and
its lines) for a tenant-raised order, using the line fields the tenant
stamped (`cf_wbs_code`, `cf_budget_head`), so a later receive or bill has a
local `po_line` to resolve against.

## The rule

- **Restricted to organisation `60074128927` on product `ERP`.** Every other
  connection is refused with `ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION`
  (403) before a single GET is issued. This was authorised for the seven
  named demo orders, not for any tenant in any organisation.
- **The connection must be LIVE** (`LIVE_READ` or `LIVE_WRITE`); a MOCK or
  SANDBOX connection is refused with `CONNECTION_NOT_LIVE` (409), the same
  code and reasoning the sweep route uses.
- For each of the connection's `purchaseorders` inbox rows not yet adopted
  (up to `limit`, default 50): one GET for the order's DETAIL
  (`adapter.get_purchase_order`), charged against the same POLLING rate
  budget a sweep's GET would spend (`throttle.reserve`, the POLLING lane).
- **`cf_capex_ref`** (the header custom field) must be present and must not
  already be claimed by another adopted order (`purchase_order.
  external_capex_ref`, unique per `external_source`).
- **Every line's `cf_wbs_code`** must resolve to *exactly one* WBS element in
  the connection's entity, and that WBS must not be abandoned.
- **Every line's `cf_budget_head`** must resolve to *exactly one* budget head
  in the connection's entity (matched by name or code, case-insensitively).
- **Budget-head isolation.** When the resolved WBS element names its own
  `budget_head_id`, the line's resolved head must AGREE with it -- a line
  whose `cf_budget_head` disagrees with its WBS element's own head is
  invalid, never treated as a second head the WBS may also carry. A WBS
  element that names no head of its own enforces nothing.
- **Every line's WBS must belong to the same project.** A purchase order
  spanning two projects is not a shape this ledger admits.
- Any failure of the above raises **`ADOPTION_DIMENSION_INVALID`** naming the
  order and the offending line, and **nothing is written**: no local
  `purchase_order`, no `po_line`, no control-cell exposure.

`cf_wbs_code` / `cf_budget_head` are read off the raw Zoho line row
(`LineDTO.raw["item_custom_fields"]`, the shape `tools/erp_demo/
po_snapshot.py` verified live: `[{"api_name", "value"}, ...]`), not off a new
`LineDTO` field -- the frozen DTO is shared by every document line on both
products, and widening it for one tenant's one document type would put a
field every other line construction has to carry `None` for, forever.

## Currency and face value

Preserved exactly as the tenant stated them, never guessed:

- An **INR** order's lines carry `amount_paise` straight from the detail's
  line totals.
- A **non-INR** order's lines carry `source_amount_minor` (minor units of
  the order's own currency) and are translated through the same rate
  resolution `create_po` uses (`pg/fx.py`'s `resolve_basis`): the ACTIVE
  `fx_rate` on file for the order's own document date. A Zoho purchase order
  carries no stated rate of its own (only a bill's `exchange_rate` does), so
  this is the only basis available.
- **No ACTIVE rate on file** -> **`FOREIGN_CURRENCY_BASIS_MISSING`** (the
  kind decision 8, 2026-09-11 already established) rather than booking a
  foreign face value as paise. `local_paise` and `source_paise` are both
  `NULL` on that exception; the amount and its currency are named in
  `detail` instead, where they cannot be summed as rupees.

## Classification

A successfully adopted order is written with
`commitment_origin = 'EXTERNAL_UNSANCTIONED'` (migration 032) -- a detective
record of a commitment the tenant already made, never a commitment this
system proposed or budget-checked. Concretely, `adopt_purchase_order`
(`pg/procurement_services.py`) is `create_po`'s shape with one deliberate
divergence: it never runs `budget_verdicts` / `exceeds_budget`. An adopted
order was already committed outside this system's budget check
(§11.7's acknowledged weakness -- a PO raised directly in Zoho bypasses it),
and re-running the gate after the fact would not un-commit a rupee the
tenant already spent. Everything else is the same: the affected control
cells are locked (`lock_affected_cells`) and `recompute_commitment` runs
under that same lock immediately after the write, so the exposure is visible
to the *next* budget check the moment it is adopted.

Adopting an order that already carries an Open `UNSANCTIONED_COMMITMENT`
exception (raised by `PollPurchaseOrders._accept` when the poll first saw a
CAPEX-tagged order with no local match) **resolves** that exception with the
note `"adopted as <po_id>"`.

## LINK mode: a tenant order that is already one of ours

Before treating an inbox order as brand new, adoption asks whether it is
actually a purchase order **this system already raised**, still waiting for
its outbound emission to give it an external anchor.
`outbound.derive_dedupe_key(connection_id, "purchaseorders", po_id)` is
**deterministic** -- a pure function of `(connection_id, module, po_id)` --
and is exactly the value this system's own emission path would stamp into
the tenant's `cf_capex_ref` were `CAPEX_ERP_OUTBOUND_WRITES` authorised. For
every **LOCAL** order in the connection's entity with no `external_id` yet,
adoption recomputes that key and compares it against the inbox order's
`cf_capex_ref` -- recomputed, not read from a stored `integration_outbox`
row, because a locally-raised order with the write gate closed has never had
an outbox row to store it in.

A match **links** rather than adopts:

- the local order's own `wbs_id` / `budget_head_id` per line, and its total
  amount in its own currency, are compared against the tenant's -- a
  mismatch is **`ADOPTION_DIMENSION_CONFLICT`** and the local order is left
  untouched, exactly as a changed-dimension conflict on an already-adopted
  order is;
- on a match, the local order's `external_source` / `external_id` are set to
  the tenant's. `commitment_origin` **stays `'LOCAL'`**: this order WAS
  proposed and budget-checked by this system, and `ck_purchase_order_
  adoption_provenance` (032) requires `external_capex_ref` stay `NULL` for
  every `LOCAL` row -- linking never reclassifies it as an external,
  unsanctioned commitment;
- a pending `integration_outbox` row for that `po_id`, if one exists, is
  marked `SENT` the way `outbound.emit_purchase_order`'s own
  `resolve_by_dedupe_key` branch marks it (`store.mark_outbox_sent`) --
  looked up by `local_id`, so finding none (the ordinary case with the write
  gate closed) touches nothing;
- the pre-existing `UNSANCTIONED_COMMITMENT` exception is resolved with the
  note `"linked to <po_id>"`, and the order is counted under **`linked`** in
  the summary -- never under `adopted`.

An order whose `cf_capex_ref` matches no local order's derived key is
adopted as `EXTERNAL_UNSANCTIONED`, exactly as before LINK mode existed. A
**repeat** sweep against an already-linked order compares only its lines
(a linked order never carries `external_capex_ref` to compare) and is a
no-op when they still agree.

## Idempotency and concurrency

- A **repeat** adoption sweep finds the local order by
  `(external_source, external_id)`. If the tenant's `cf_capex_ref`,
  `cf_wbs_code` and `cf_budget_head` are **unchanged**, nothing happens
  (counted as `skipped`).
- If they have **changed**, it raises **`ADOPTION_DIMENSION_CONFLICT`** and
  leaves the local order **exactly as it was** -- an adopted commitment is
  never silently re-based, for the same reason
  `trg_purchase_order_fx_basis_immutable` (029) refuses to re-base a created
  one.
- Each order is processed under
  `pg_advisory_xact_lock(hashtext('capex.po_adoption:<connection_id>:<external_id>'))`
  -- the same primitive `live_sweep._job_row_for` uses for its own race --
  plus the database's own `ux_po_external` / `ux_po_external_capex_ref`
  unique indexes as the backstop.

## The route

`POST /api/integrations/connections/{connection_id}/adopt-orders`
(`app/backend/api/integrations_live.py`), on the same router as
`/sweep`, guarded the same way: `connector.read` at the router floor,
`connector.manage` on the route. Body: `{"limit"?: int}`.

Refusal order (mirrors `/sweep`'s, with one insertion):

1. no PostgreSQL -> 503 `DATABASE_NOT_CONFIGURED`
2. invisible connection -> 404 `CONNECTION_NOT_FOUND`
3. not live -> 409 `CONNECTION_NOT_LIVE`
4. no live transport -> 503 `LIVE_TRANSPORT_UNAVAILABLE`
5. not the demo organisation on ERP -> 403
   `ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION`
6. Zoho refused or failed -> 502 `ZOHO_API_ERROR` / 429
   `RATE_BUDGET_EXHAUSTED`

The response is `adoption.adopt_tenant_orders`'s summary verbatim:
`{"adopted", "skipped", "exceptions", "calls", "exception_ids"}`.

## What this does NOT do

- **No outbound write to Zoho, ever.** The only call this path makes is
  `adapter.get_purchase_order` -- a GET. `CAPEX_ERP_OUTBOUND_WRITES` is
  never read, referenced or set anywhere in `app/backend/integration/
  adoption.py`.
- **No budget check.** See "Classification" above.
- **No re-basing of an adopted order.** A repeat sweep that finds the
  tenant's dimensions changed files an exception; it never rewrites the
  posted order.
- **Not a sweep.** Adoption has no `job` row, no watermark and no
  `JobContext`; it is run once, by an operator, against a bounded batch of
  already-discovered inbox rows. What it borrows from the sweep stack is the
  POLLING rate budget (`throttle.reserve`), because its GET reaches the same
  tenant under the same daily ceiling a sweep's GET would.

## Exception kinds (migration 032)

| Kind | Raised when | Written? |
|---|---|---|
| `ADOPTION_DIMENSION_INVALID` | `cf_capex_ref` missing or already claimed; a line's `cf_wbs_code` / `cf_budget_head` is missing, absent, ambiguous or names an abandoned WBS; lines span more than one project | Nothing |
| `ADOPTION_DIMENSION_CONFLICT` | A repeat sweep finds the tenant's `cf_capex_ref` / line dimensions changed since adoption | The local order is left as it was |
| `FOREIGN_CURRENCY_BASIS_MISSING` (030, reused) | A non-INR order has no ACTIVE rate on file for its document date | Nothing |

Both new kinds are registered in `research/30_contracts/C18_domain_
statuses.json` (`namespaces.exception_status.kinds`),
`app/backend/pg/integration_store.EXCEPTION_KINDS`, and
`ck_reconciliation_exception_kind` (migration 032). They are also present in
`app/backend/integration/sweeps.EXCEPTION_KINDS` even though no sweep in
that module raises either -- `tests/test_integration_sweeps.py::
test_every_kind_a_sweep_can_raise_is_one_the_registry_declares` holds that
constant to the *full* C18 registry, not to the subset `sweeps.py` itself
uses.

## Tests

`tests/test_adoption_fable51.py`: database-free (the pure helpers, the two
up-front refusals proved never to touch the session, the route's 401/403
guard) and live-PostgreSQL end to end (adopt, resolve the pre-existing
`UNSANCTIONED_COMMITMENT`, idempotent repeat, changed-dimension conflict,
duplicate `cf_capex_ref`, missing WBS / budget head, budget-head isolation,
JPY with and without an active rate, two concurrent sessions racing the same
order, LINK mode -- link once / idempotent repeat / mismatched lines conflict
/ an unmatched key still adopts as `EXTERNAL_UNSANCTIONED` -- and the whole
point of the feature -- `SweepPoAnchored` mirroring a receive and
`pg.procurement.mirror_bill` attributing a bill against the adopted order's
line).
