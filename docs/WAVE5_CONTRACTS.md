# Wave 5 — integration platform contracts (FROZEN)

**Provenance of this decomposition.** The lead's seven Wave 5 workstreams were
stated in conversation and are not recorded in this repository. These seven are
derived from **§11 of `docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`**, which
is approved, and are labelled as a derivation rather than a transcription. If
the lead's list differs, the streams are file-disjoint and can be redirected
individually.

**Standing boundaries, unchanged and absolute for every stream:**

* **No live Zoho tenant. No Catalyst. No Supabase. No cloud or billable
  resources. No deployment.** Everything is built against recorded, sanitised
  cassettes and in-process fakes until the lead grants explicit tenant
  authorisation. Phase 0B has not cleared.
* **The target product is PROVISIONAL** (D-14, unresolved). Nothing may hardcode
  an ERP base URL, scope string or endpoint. Product-specific facts live behind
  the adapter and its `Capabilities`, never in a caller.
* Money is integer paise (`bigint`). `SUM()` over bigint returns numeric in
  PostgreSQL — cast `::bigint`.
* Every scoped query goes through `repo.query()` with a literal `{scope}` token
  and a `columns=` mapping naming every dimension the table carries.
* No test may be weakened or deleted. Record any change in
  `tests/ADAPTATIONS.md` and run `python tools/build_test_manifest.py`.

---

## The frozen seams

Streams 1 and 2 are prerequisites for the rest, so their interfaces are frozen
**here** rather than discovered, exactly as Wave 4's contracts were. A stream
blocked on one of these implements against the signature below and does not
wait.

### C1 — the adapter boundary (§11.10). Stream 1 owns the implementation.

```python
# app/backend/integration/adapter.py
class ProcurementAdapter(Protocol):
    product: Literal["ERP", "BOOKS_INVENTORY"]
    def base_url(self, dc: str) -> str: ...
    def scopes_required(self) -> frozenset[str]: ...
    def list_bills(self, since, until, page) -> Page[BillDTO]: ...
    def get_bill(self, external_id: str) -> BillDTO: ...
    def list_purchase_orders(self, since, until, page) -> Page[PurchaseOrderDTO]: ...
    def create_purchase_order(self, po: PurchaseOrderDTO, dedupe_key: str) -> str: ...
    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]: ...
    def capabilities(self) -> Capabilities: ...

@dataclass(frozen=True)
class Capabilities:
    receives_listable: bool          # ERP: False.  Inventory: True
    bills_delta_filter: bool         # both: True
    po_delta_filter: bool            # ERP/Books: True.  Inventory: False
    items_delta_filter: bool         # Inventory: True.  ERP/Books: False
    line_level_custom_fields: bool   # D-7, tenant-verified — assume False
    daily_call_ceiling: int          # plan-derived — assume ERP Standard 2000
```

`Capabilities` is the honest part: it declares what the product **cannot** do,
and the scheduler reads it instead of assuming. `receives_listable=False`
selects PO-anchored discovery; `po_delta_filter=False` selects full re-pull.
DTOs are **ours**, never Zoho's shapes.

### C2 — the integration tables. Stream 2 owns `migrations/pg/010_integration.sql`.

Table and column names are frozen here so streams 3–7 can write SQL before the
migration lands:

* `integration_connection(connection_id, entity_id, product, dc, organization_id,
  connector_name, mode, created_*, updated_*)` — `mode ∈ MOCK|SANDBOX|LIVE_READ|LIVE_WRITE`,
  **defaulting to MOCK**; `UNIQUE (entity_id, organization_id)`.
* `integration_inbox(inbox_id, connection_id, module, external_id, payload_sha,
  payload jsonb, external_status_raw, received_at, state, ...)` with
  `UNIQUE (connection_id, module, external_id, payload_sha)` — inbound
  idempotency is free from this constraint (§11.6).
* `integration_outbox(outbox_id, connection_id, module, local_id, dedupe_key,
  payload jsonb, state, attempts, next_attempt_at, ...)`,
  `UNIQUE (connection_id, module, local_id)`.
* `job(job_id, kind, state, checkpoint jsonb, soft_deadline_at, resume_count,
  correlation_id, ...)` (§2.2).
* `integration_watermark(connection_id, module, hwm, ...)`.
* `integration_rate_budget(connection_id, window_kind, window_start, used, ...)`
  — `window_kind ∈ MINUTE|DAY`, because **on ERP Standard the DAILY ceiling is
  the binding one**, not the per-minute.
* `integration_event(event_id, connection_id, correlation_id, ...)`.

States come from `C16_integration_statuses.json` (stream 3), never invented.

### C3 — status registries. Stream 3 owns `research/30_contracts/C16_*.json`,
`C17_*.json` and the mapping module.

Raw Zoho status is stored **verbatim and never overwritten** in
`external_status_raw`, alongside the product and API version that produced it.
An **unmapped** raw value never guesses: the record is accepted, the raw value
stored, and a `reconciliation_exception` of kind `UNMAPPED_EXTERNAL_STATUS` is
raised. Mapping targets must exist in `C3_statuses.json`'s frozen 21; an
integration or approval status must never reach a business screen.

---

## The seven streams and their file ownership

| # | Stream | Owns |
|---|---|---|
| 1 | Adapter boundary, DTOs, capabilities, cassette fakes | `app/backend/integration/adapter.py`, `dto.py`, `erp.py`, `books_inventory.py`, `tests/cassettes/**` |
| 2 | Schema: inbox, outbox, jobs, watermarks, connections | `migrations/pg/010_integration.sql`, `app/backend/pg/integration_store.py` |
| 3 | Status registries and mapping | `research/30_contracts/C16_*.json`, `C17_*.json`, `app/backend/integration/statuses.py` |
| 4 | Rate budget, retry, backoff, circuit breaker | `app/backend/integration/throttle.py` |
| 5 | Chunked jobs: polling and the sweeps | `app/backend/integration/jobs.py`, `sweeps.py` |
| 6 | Outbound PO emission and idempotency | `app/backend/integration/outbound.py` |
| 7 | Integration screens (SCR-31–34, 26, 38, 39) | `app/frontend/src/features/integration/**` |

No stream edits another's files. A needed change is **reported**, not made.

---

## The three facts most likely to be got wrong

1. **Zoho ERP Purchase Receives has NO list endpoint.** Create, update, delete
   and fetch-one only. On ERP there is no delta feed and no full-scan path for
   GRNs; **PO-anchored discovery is the sole acquisition mechanism**, its cost
   scales with open-PO count rather than receive volume, and a receive against a
   PO we do not know about is undiscoverable until its bill arrives.
2. **`last_modified_time` is filterable but NOT sortable** on ERP/Books bills
   and POs. So a window can be selected but not walked as a stable keyset —
   which is why every poll uses a bounded window with a **300 s overlap** and
   why the completeness sweeps are retained. A filter cannot prove it returned
   everything.
3. **Zoho documents no idempotency header.** Outbound dedupe is synthesised via
   a unique custom field `cf_capex_ref` (Z-01) with retries using
   update-by-custom-field-unique-value. A function killed after sending but
   before recording must UPDATE, not duplicate.

---

# Amendments raised by streams in flight

## A1 — `integration_rate_budget` needs a `lane` in its key (raised by stream 4)

C2 froze the column list with a trailing `…`; this pins what has to be there.

```
UNIQUE (connection_id, window_kind, lane, window_start)
```
plus `created_by`, `updated_at`, `updated_by`.

**Why it is not optional.** A single `used` counter per window cannot express
"polling is capped at 60 of the 100". Enforcing a per-lane ceiling requires
knowing what that lane has spent. Without the column, the 60/30/10 allocation
is undeliverable and `throttle.reserve()` fails at runtime — an operator
clicking "Test connection" would starve behind a backfill, which is the exact
thing the allocation exists to prevent.

**Stream 4 did not touch the migration.** Stream 2 owns it. If the migration
lands without `lane`, the integration pass adds it.

## A2 — the circuit breaker has no durable home (raised by stream 4)

C2 froze no table for it, so stream 4 shipped a `CircuitStore` protocol with an
explicitly **non-durable** in-memory implementation — the gap is a named object
rather than silence.

It needs five columns — `state`, `consecutive_failures`, `window_started_at`,
`open_until`, `probe_in_flight` — on `integration_connection` or a small table
of its own.

**Why in-memory cannot stand.** AppSail scales to zero and reclaims instances
after five minutes. A daily-quota circuit (429 code 45) stays open until the
next day boundary — hours after the instance that opened it is gone. An
in-memory breaker therefore forgets it is open and resumes hammering a quota
that is still exhausted.

## A3 — lane allocation applies to BOTH windows (decided by stream 4)

The plan states 60/30/10 against the per-minute figure. Stream 4 applied the
same ratio to the daily window and recorded the reasoning: protecting the
operator for sixty seconds while a backfill eats all 2,000 daily calls by
mid-morning **moves** the starvation rather than preventing it. Accepted.

## A4 — worktrees may be created from a stale base (observed by stream 4)

Stream 4's worktree was created at `ce7f3c5`, a pre-Wave-4 commit, and it reset
to the tip before starting. Every stream should verify its base is the intended
commit rather than assume it, and the integration pass should check what each
branch is actually rooted on before merging.
