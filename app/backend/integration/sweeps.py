"""The polls and the sweeps, all eight of them, on the framework in `jobs.py`.

Read `jobs.py` first: every job here is a generator yielding
:class:`~app.backend.integration.jobs.Progress`, and the 12-minute soft
deadline, the checkpoint persistence and the accounting belong to the runner,
not to any job below.

Three facts shape this file, and none of them is incidental
-----------------------------------------------------------

**1. On Zoho ERP, `sweep_po_anchored` is not a compensating control. It is the
sole GRN acquisition mechanism.** ERP's Purchase Receives object has create,
update, delete and fetch-one — and **no list endpoint** (plan §11.4). You
cannot enumerate receives. The only way to find one is to walk the POs we know
are open and read the receive references off each PO's detail. Three
consequences that this module makes visible rather than hiding:

* cost scales with **open-PO count**, not receive volume, so on ERP Standard's
  2,000 calls/day the open-PO population is the binding constraint —
  :class:`SweepPoAnchored` spends exactly one call per PO and stops at the
  budget rather than breaching it;
* a receive against a PO we do not know about is **undiscoverable** until its
  bill arrives, where it surfaces as `UNSANCTIONED_COMMITMENT`;
* a deleted receive arrives as an absence, so only a re-read of the PO detects
  it — which is why the walk cycles rather than terminating.

The strategy is chosen from `Capabilities.receives_listable`, never from a
product name. On a product where receives ARE listable the same sweep runs as
what it is there — a completeness check — and says so in its checkpoint.

**2. `last_modified_time` is filterable but NOT sortable** on ERP/Books bills
and POs (plan §11.3). A window can be *selected* but not *walked*: there is no
stable resumable keyset on modification time. So every poll uses a bounded
window re-entered with a **300-second overlap** on each run, and the window
boundary itself is the checkpoint. The overlap is free because
`integration_inbox UNIQUE (connection_id, module, external_id, payload_sha)`
discards the re-read. Inventory POs have neither filter nor sort, which is not
a narrower window but a different question — `plan_window` answers it with a
full re-pull, again from the declared capability.

**3. The completeness sweeps are not made redundant by a working delta
filter.** A filter cannot prove it returned everything. `sweep_control_totals`
compares count and sum per (module, period, status) against local;
`sweep_completeness` does document-number gap detection and raises
`LATE_ARRIVAL_CLOSED_PERIOD` for a document dated into a period that has
already closed. Both are retained in full.

And one rule that outranks all of them
--------------------------------------
**Zero pro-rata spreading. Zero silent drops.** (§11.8.) A receive line whose
linkage does not resolve to a known `po_line` is never apportioned, never
guessed at and never dropped: it raises `GRN_LINE_UNATTRIBUTED` and accumulates
in a project-level bucket that blocks capitalisation. An Open exception also
blocks accounting-period close — the gate `approve_capitalisation` already
applies, extended here to the exceptions these sweeps raise
(:func:`period_close_blockers`).

Seams this module consumes but does not own
-------------------------------------------
* the **adapter** (stream 1) — through :class:`ProcurementPort`, a subset of
  the frozen C1 protocol plus the two list calls C1 does not yet declare
  (`list_items`, `list_contacts`); see :func:`_adapter_call`;
* the **store** (stream 2) — through :class:`SweepStore`, the port
  `pg/integration_store.py` fills;
* the **rate budget** (stream 4) — through `jobs.RateBudget`;
* the **status registries** (stream 3) — :data:`EXCEPTION_KINDS` is read from
  the frozen C18 five and no sweep may invent a sixth.

Nothing here opens a socket. Every test drives it with fakes and an injected
clock.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterator, Mapping, Protocol, Sequence

from .jobs import (
    CHUNK_SIZES,
    DEFAULT_WINDOW_SECONDS,
    MAX_PAGES_PER_POLL,
    POLL_OVERLAP_SECONDS,
    Capabilities,
    JobContext,
    Progress,
    Window,
    iso,
    next_watermark,
    parse_iso,
    plan_window,
)

# --------------------------------------------------------------- the modules
#: Module names as they appear in `integration_inbox.module` and
#: `integration_watermark.module`. One spelling, used by the poll, the sweep
#: and the store alike.
MODULE_BILLS = "bills"
MODULE_PURCHASE_ORDERS = "purchaseorders"
MODULE_PURCHASE_RECEIVES = "purchasereceives"
MODULE_ITEMS = "items"
MODULE_CONTACTS = "contacts"

# ------------------------------------------------------- reconciliation kinds
# The five research/30_contracts/C18_domain_statuses.json froze, and the sixth
# it declares since migration 030. A sweep that needs a kind the registry does
# not declare REPORTS it; it does not invent one here. Gap detection therefore
# lands in CONTROL_TOTAL_MISMATCH -- a gap IS a count disagreement -- with the
# missing numbers named in `detail`, rather than under a DOCUMENT_NUMBER_GAP
# kind that no registry declares.
KIND_GRN_LINE_UNATTRIBUTED = "GRN_LINE_UNATTRIBUTED"
KIND_CONTROL_TOTAL_MISMATCH = "CONTROL_TOTAL_MISMATCH"
KIND_LATE_ARRIVAL_CLOSED_PERIOD = "LATE_ARRIVAL_CLOSED_PERIOD"
KIND_UNSANCTIONED_COMMITMENT = "UNSANCTIONED_COMMITMENT"
KIND_UNMAPPED_EXTERNAL_STATUS = "UNMAPPED_EXTERNAL_STATUS"
#: Product owner decision 8 (2026-09-11): "A receive or bill against a non-INR
#: order is refused with a coded reconciliation exception until it carries its
#: own currency and rate; nothing is booked at face value." This is the code.
#: The SAME string is the `.code` of the refusal `pg.procurement` raises, so
#: the bill-detail sweep can recognise the ledger's refusal without importing
#: the ledger. A row of this kind carries NO `local_paise` and NO
#: `source_paise`: the only figure available is the vendor's face value in the
#: vendor's currency, and a column named `_paise` is the wrong place for it.
KIND_FOREIGN_CURRENCY_BASIS_MISSING = "FOREIGN_CURRENCY_BASIS_MISSING"

#: Migration 032 (product owner, 2026-09-12): "Adopt the seven tenant-raised
#: Zoho demo orders into local WBS orders using their stamped line fields."
#: Neither kind is raised by any sweep in THIS module -- adoption is not a
#: sweep, it is `app.backend.integration.adoption.adopt_tenant_orders`, run
#: once per operator request rather than on the poll/watermark cadence -- but
#: both are declared here anyway, because `tests/test_integration_sweeps.py::
#: test_every_kind_a_sweep_can_raise_is_one_the_registry_declares` asserts
#: `sweeps.EXCEPTION_KINDS` equals the FULL set research/30_contracts/
#: C18_domain_statuses.json declares, not merely the subset this module uses.
#: Omitting them here would make that registry a second, competing
#: declaration of what exists rather than the single one C18 is meant to be.
KIND_ADOPTION_DIMENSION_INVALID = "ADOPTION_DIMENSION_INVALID"
KIND_ADOPTION_DIMENSION_CONFLICT = "ADOPTION_DIMENSION_CONFLICT"

EXCEPTION_KINDS: frozenset[str] = frozenset({
    KIND_GRN_LINE_UNATTRIBUTED, KIND_CONTROL_TOTAL_MISMATCH,
    KIND_LATE_ARRIVAL_CLOSED_PERIOD, KIND_UNSANCTIONED_COMMITMENT,
    KIND_UNMAPPED_EXTERNAL_STATUS, KIND_FOREIGN_CURRENCY_BASIS_MISSING,
    KIND_ADOPTION_DIMENSION_INVALID, KIND_ADOPTION_DIMENSION_CONFLICT,
})

#: The estate's base currency. The sweeps do not import `pg.fx` (they run
#: against any `SweepStore`, database or not), so the one currency that is the
#: identity translation is spelled here and compared by every rule that turns
#: on it. `pg.fx.BASE_CURRENCY` says the same thing on the ledger side.
BASE_CURRENCY = "INR"

#: The exception status that blocks. C18: "Open -- unresolved. BLOCKS
#: capitalisation, and blocks accounting-period close."
EXCEPTION_OPEN = "Open"


class SweepError(RuntimeError):
    """A sweep could not proceed for a reason a retry will not fix."""


class AdapterMethodMissing(SweepError):
    """The injected adapter does not implement a call this job needs.

    Raised with the seam named, because the frozen C1 protocol declares
    `list_bills`, `list_purchase_orders`, `get_bill`, `receives_for_po` and
    `create_purchase_order` but NOT `list_items` or `list_contacts`, while
    plan §11.5 requires `poll_items` and `poll_contacts`. That gap is reported
    to stream 1 rather than papered over with a silent no-op, which would make
    a master-data poll that fetches nothing look exactly like one that found
    nothing.
    """


# ============================================================== normalisation
# The C1 DTOs (stream 1's `dto.py`) are not yet in the tree, and the field
# names of `Page[...]` are not part of the frozen seam. Rather than guess in
# eight places, every guess lives here, in one clearly marked layer, with the
# candidate names spelled out. When `dto.py` lands this is the only section
# that changes.

_PAGE_RECORD_FIELDS = ("records", "items", "rows", "data")
_PAGE_MORE_FIELDS = ("has_more", "has_more_page", "more")


def page_records(page: Any) -> list[Any]:
    """The records out of a `Page`-like object, or a bare sequence."""
    if page is None:
        return []
    if isinstance(page, (list, tuple)):
        return list(page)
    for name in _PAGE_RECORD_FIELDS:
        if hasattr(page, name):
            return list(getattr(page, name) or [])
    if isinstance(page, Mapping):
        for name in _PAGE_RECORD_FIELDS:
            if name in page:
                return list(page[name] or [])
    raise SweepError(
        f"cannot read records off {type(page).__name__}: expected one of "
        f"{_PAGE_RECORD_FIELDS} or a sequence")


def page_has_more(page: Any, *, returned: int, page_size: int) -> bool:
    """Whether another page exists.

    Prefers the source's own answer; falls back to "a full page came back, so
    assume there is another". The fallback is deliberately the pessimistic
    one: guessing that a full page is the last page would stop the walk one
    page early and lose every record on the page after it, silently.
    """
    for name in _PAGE_MORE_FIELDS:
        if hasattr(page, name):
            return bool(getattr(page, name))
    if isinstance(page, Mapping):
        for name in _PAGE_MORE_FIELDS:
            if name in page:
                return bool(page[name])
    return returned >= page_size


def _first_attr(obj: Any, names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def raw_payload(record: Any) -> dict[str, Any]:
    """The record as a JSON-able dict, for verbatim preservation in the inbox.

    Prefers a `raw`/`payload` the DTO carried through from the source, because
    §11.10 requires the *source document* to be recoverable, not our rendering
    of it.
    """
    carried = _first_attr(record, ("raw", "payload", "source_payload"))
    if isinstance(carried, Mapping):
        return dict(carried)
    if isinstance(record, Mapping):
        return dict(record)
    if is_dataclass(record) and not isinstance(record, type):
        return json.loads(json.dumps(asdict(record), default=str))
    return json.loads(json.dumps(vars(record), default=str))


def payload_sha(payload: Mapping[str, Any]) -> str:
    """The inbox dedupe key: sha256 over a canonical rendering.

    Canonical — sorted keys, no insignificant whitespace — so two renderings
    of the same source document hash the same and the 300-second poll overlap
    stays free. A non-canonical hash would make every overlap a duplicate row
    and the "free" in "free overlap" would quietly stop being true.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def _as_datetime(value: Any) -> datetime | None:
    """A source `date` as an aware `datetime` at midnight UTC, or `None`.

    Used only where a `date`-typed source field has to reach a `timestamptz`
    column. The timezone is stated rather than left to the server's
    `TimeZone` setting: a naive value would be read as the SERVER's local time,
    which would move a receipt across a day boundary on any deployment not
    running in UTC -- and India is +05:30, so it would move it every time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day,
                        tzinfo=timezone.utc)
    return None


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class SourceRecord:
    """One inbound document, normalised. Ours, never Zoho's shape."""

    external_id: str
    payload: dict[str, Any]
    sha: str
    module: str
    last_modified_time: datetime | None = None
    document_number: str | None = None
    document_date: date | None = None
    status_raw: str | None = None
    total_paise: int | None = None
    entity_id: str | None = None
    project_id: str | None = None
    capex_reference: str | None = None
    #: The vendor the document names. `bill.vendor_name` is NOT NULL and there
    #: is no honest filler for it, so a bill arriving without one is refused by
    #: the ledger rather than mirrored under a placeholder nobody can reconcile
    #: against `vendor_master`.
    vendor_name: str | None = None
    #: The PURCHASE ORDERS a bill is raised against, as the SOURCE names them
    #: -- not our `po_id`. PLURAL and a tuple, because `dto.BillDTO` spells it
    #: `purchase_order_external_ids: tuple[str, ...]` and a reader that took
    #: the singular name would get `None` from every real bill the adapters
    #: emit. That is precisely the defect
    #: `tests/test_integration_dto_reader_contract.py` exists for: a field name
    #: shaped to the reader rather than to the producer, which no double can
    #: catch.
    #:
    #: `mirror_bill` and `bill.po_id` are both singular, so exactly one id is
    #: mirrorable, none is a non-PO bill, and two or more is refused rather
    #: than resolved by picking the first.
    po_external_ids: tuple[str, ...] = ()
    #: THE CURRENCY THE DOCUMENT IS DENOMINATED IN, and the field whose absence
    #: kept AUD-H-007 open through two migrations that were written to close it.
    #:
    #: `dto.BillDTO.currency_code` and `dto.PurchaseOrderDTO.currency_code` have
    #: existed since the adapter boundary was frozen, and both adapters populate
    #: them -- `books_inventory.py` and `erp.py` each read
    #: `row.get("currency_code")` in four places. `normalise` then dropped the
    #: value on the floor building this record, and `_mirror` had nothing to
    #: pass even if `mirror_bill` had had a parameter to receive it, which it
    #: did not. Migration 023 built an entire FX engine downstream of a value
    #: that never arrived.
    #:
    #: DEFAULTS TO THE BASE CURRENCY, so a source that genuinely says nothing
    #: is treated as INR -- which is what every such document has silently been
    #: treated as since the product was written, and is correct for an estate
    #: whose base currency is INR. The difference is that a document that DOES
    #: say EUR is now believed.
    currency_code: str = "INR"
    #: THE RATE THE DOCUMENT ITSELF STATES, as an exact decimal STRING, or
    #: None when the source stated none. Never a float: `dto.rate_text`
    #: refuses one at the adapter and `normalise` refuses one here, for the
    #: reason `dto.paise` gives -- a rate is multiplied into money.
    #:
    #: Only a bill can carry one (`dto.BillDTO.exchange_rate`; a Zoho bill row
    #: carries `currency_code` and `exchange_rate`). A purchase receive carries
    #: no currency and no rate of its own on any product this integrates with,
    #: which is why decision 8 (2026-09-11) refuses a receive against a
    #: non-INR order outright rather than looking for one.
    exchange_rate: str | None = None
    lines: tuple[Any, ...] = ()

    @property
    def has_line_items(self) -> bool:
        return bool(self.lines)


def _rate_text(value: Any, *, external_id: str) -> str | None:
    """A stated exchange rate as the exact decimal string, or None. No floats."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise SweepError(
            f"bill {external_id}: exchange_rate arrived as "
            f"{type(value).__name__} ({value!r}). An exchange rate is "
            f"multiplied into money, so it must be an exact decimal string; "
            f"the transport parses JSON with parse_float=str for exactly this "
            f"reason, and nothing here rounds a float into a rate.")
    text = str(value).strip()
    return text or None


def normalise(record: Any, *, module: str) -> SourceRecord:
    """A source DTO (or dict) as a :class:`SourceRecord`."""
    payload = raw_payload(record)
    external_id = _first_attr(
        record, ("external_id", "id", f"{module[:-1]}_id", "record_id"))
    if external_id is None:
        raise SweepError(
            f"{module}: a record arrived with no external id; it cannot be "
            f"made idempotent and must not be accepted silently")
    lines = _first_attr(record, ("lines", "line_items"), ()) or ()
    return SourceRecord(
        external_id=str(external_id),
        payload=payload,
        sha=_first_attr(record, ("payload_sha", "sha")) or payload_sha(payload),
        module=module,
        last_modified_time=parse_iso(
            _first_attr(record, ("last_modified_time", "modified_time",
                                 "last_modified"))),
        document_number=_first_attr(
            record, ("document_number", "bill_number", "purchaseorder_number",
                     "receive_number", "number")),
        document_date=_as_date(_first_attr(record, ("document_date", "date"))),
        status_raw=_first_attr(record, ("status_raw", "external_status_raw",
                                        "status")),
        total_paise=_first_attr(record, ("total_paise", "amount_paise")),
        entity_id=_first_attr(record, ("entity_id",)),
        project_id=_first_attr(record, ("project_id",)),
        # `dedupe_key` FIRST: that is what `dto.PurchaseOrderDTO` actually
        # carries (erp.py reads cf_capex_ref into it). The others are the
        # test fake's and a raw mapping's spellings. Until 2026-09-12 the real
        # name was absent here, so a live order created in the tenant with a
        # CAPEX reference never raised UNSANCTIONED_COMMITMENT.
        capex_reference=_first_attr(
            record, ("dedupe_key", "capex_reference", "cf_capex_ref",
                     "capex_ref", "wbs_code")),
        # `vendor_name` FIRST, because that is what `dto.BillDTO` and
        # `dto.PurchaseOrderDTO` actually carry. The other two are aliases for
        # a raw mapping that has not been through an adapter yet.
        vendor_name=_first_attr(
            record, ("vendor_name", "contact_name", "vendor")),
        # READ ONLY FOR BILLS, and the guard is not fussiness. `external_id`
        # above resolves `f"{module[:-1]}_id"`, which for the purchase-order
        # module IS `purchaseorder_id` -- so reading a PO reference here
        # unconditionally would give every PO a linkage equal to its own id: a
        # self-reference that reads like a linkage and is not one.
        po_external_ids=(_po_external_ids(record)
                         if module == MODULE_BILLS else ()),
        # `currency_code` FIRST for the reason `vendor_name` is: it is what
        # `dto.BillDTO` and `dto.PurchaseOrderDTO` actually carry, and
        # `currency` is the alias a raw Zoho mapping that has not been through
        # an adapter would use. Blank or absent falls back to the base
        # currency, which is what the value has effectively been for every
        # document ever mirrored.
        currency_code=(str(_first_attr(record, ("currency_code", "currency"))
                           or BASE_CURRENCY).strip().upper() or BASE_CURRENCY),
        # `exchange_rate` is the name `dto.BillDTO` carries (and the name the
        # Zoho row carries, so a raw mapping reads the same way). Read for
        # every module and None for every one but bills, because only a bill
        # DTO defines it.
        exchange_rate=_rate_text(_first_attr(record, ("exchange_rate",)),
                                 external_id=str(external_id)),
        lines=tuple(lines),
    )


def _po_external_ids(record: Any) -> tuple[str, ...]:
    """The purchase orders a bill names, as a tuple, however the source spells it.

    `dto.BillDTO.purchase_order_external_ids` is the real name and is already a
    tuple; a raw Zoho mapping spells it `purchaseorder_id` (singular scalar) or
    `purchaseorder_ids` (a list). All three normalise to the same tuple here so
    the caller has one shape to reason about, and blanks are dropped rather
    than carried as an empty-string "linkage".
    """
    value = _first_attr(
        record, ("purchase_order_external_ids", "po_external_ids",
                 "purchaseorder_ids", "purchase_order_external_id",
                 "po_external_id", "purchaseorder_id"))
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        value = [value]
    return tuple(str(item).strip() for item in value if str(item).strip())


# ===================================================================== ports

@dataclass(frozen=True)
class LocalPurchaseOrder:
    """A PO we know about, as the PO-anchored walk needs it."""

    po_id: str
    external_id: str
    entity_id: str | None = None
    project_id: str | None = None
    document_number: str | None = None
    #: THE CURRENCY THE ORDER IS DENOMINATED IN (`purchase_order.currency`,
    #: migration 029). The PO-anchored walk reads it before it resolves a
    #: single receive line, because a receive inherits the order's currency
    #: and carries none of its own: against a non-INR order every line's
    #: amount is a face value in that currency, and decision 8 (2026-09-11)
    #: holds it under FOREIGN_CURRENCY_BASIS_MISSING rather than booking it as
    #: paise. Defaults to the base currency so every existing construction of
    #: this record, all of them INR, is unchanged.
    currency_code: str = BASE_CURRENCY


@dataclass(frozen=True)
class LocalDocument:
    """A document we hold locally, as the completeness sweep needs it."""

    external_id: str
    document_number: str | None = None
    document_date: date | None = None
    received_at: datetime | None = None
    entity_id: str | None = None
    project_id: str | None = None
    total_paise: int | None = None
    status: str | None = None


@dataclass(frozen=True)
class PeriodRef:
    """One accounting period, and whether it is closed."""

    period_id: str
    entity_id: str
    period_start: date
    period_end: date
    state: str = "OPEN"

    @property
    def closed(self) -> bool:
        return self.state.upper() == "CLOSED"

    def contains(self, day: date | None) -> bool:
        return day is not None and self.period_start <= day <= self.period_end


@dataclass(frozen=True)
class ControlTotal:
    """count and sum for one (module, period, status) slice."""

    count: int
    sum_paise: int


class ProcurementPort(Protocol):
    """The subset of C1 the sweeps call. Stream 1 owns the implementation."""

    def list_bills(self, since: datetime | None, until: datetime,
                   page: int) -> Any: ...

    def get_bill(self, external_id: str) -> Any: ...

    def list_purchase_orders(self, since: datetime | None, until: datetime,
                             page: int) -> Any: ...

    def receives_for_po(self, po_external_id: str) -> list[Any]: ...

    def capabilities(self) -> Capabilities: ...


class SweepStore(Protocol):
    """What the sweeps need from persistence. Stream 2 fills it."""

    # ---- inbound idempotency (C2: UNIQUE (connection_id, module,
    # external_id, payload_sha) makes this free)
    def upsert_inbox(self, *, connection_id: str, module: str,
                     external_id: str, payload_sha: str,
                     payload: Mapping[str, Any],
                     external_status_raw: str | None,
                     received_at: datetime,
                     correlation_id: str | None) -> bool: ...

    # ---- watermarks
    def get_watermark(self, *, connection_id: str,
                      module: str) -> datetime | None: ...

    def set_watermark(self, *, connection_id: str, module: str,
                      hwm: datetime, now: datetime) -> None: ...

    # ---- reconciliation
    def raise_exception(self, *, kind: str, object_type: str,
                        object_id: str | None, detail: str,
                        raised_at: datetime, entity_id: str | None = None,
                        project_id: str | None = None,
                        local_paise: int | None = None,
                        source_paise: int | None = None,
                        correlation_id: str | None = None) -> str: ...

    def open_exceptions(self, *, entity_id: str | None = None,
                        project_id: str | None = None) -> list[Mapping[str, Any]]: ...

    # ---- PO-anchored discovery
    def open_purchase_orders(self, *, connection_id: str,
                             after_po_id: str | None,
                             limit: int) -> list[LocalPurchaseOrder]: ...

    def known_purchase_order(self, *, connection_id: str,
                             external_id: str) -> bool: ...

    def resolve_po_line(self, *, po_external_id: str,
                        line_external_id: str | None) -> str | None: ...

    def record_receive_line(self, *, po_line_id: str, receive_external_id: str,
                            line_external_id: str | None, quantity: Any,
                            amount_paise: int | None,
                            external_source: str | None = None,
                            receive_number: str | None = None,
                            received_at: datetime | None = None,
                            external_last_modified: datetime | None = None,
                            payload_sha: str | None = None) -> None:
        """Mirror one attributed receive line.

        THE FIVE ARE THE CONTRACT; the rest is PROVENANCE, and it is declared
        here rather than left to a private arrangement between one sweep and
        one store. §11.10 requires the SOURCE DOCUMENT to be recoverable, not
        our rendering of it, and a mirrored row that names neither its source,
        its version (`payload_sha`), nor when the source last changed
        (`external_last_modified`) is a row nobody can trace back.

        Optional with `None` defaults, because a store is free to hold the
        line and nothing else -- `tests/integration_fakes.py` does exactly that
        -- and because a required argument here would break every existing
        implementation of this protocol at once.
        """
        ...

    def accumulate_unattributed(self, *, project_id: str | None, paise: int,
                                source_key: str) -> None:
        """Add `paise` to the project-level unattributed bucket, ONCE.

        `source_key` is the reconciliation exception's id, and the bucket is
        keyed by it rather than incremented blindly. A sweep resumed from a
        checkpoint re-reads POs it has already walked -- that is the whole
        point of the 300-second overlap and of the cycling walk -- so a bucket
        that simply added on every pass would climb every fifteen minutes
        without a single new receive arriving, and the number that blocks
        capitalisation would be fiction.
        """

    # ---- bill detail hydration
    def mirror_bill(self, *, external_source: str, external_id: str,
                    bill_number: str, vendor_name: str, bill_date: date,
                    lines: Sequence[Any],
                    po_external_id: str | None = None,
                    project_id: str | None = None,
                    entity_id: str | None = None,
                    external_status_raw: str | None = None,
                    external_last_modified: datetime | None = None,
                    payload_sha: str | None = None,
                    source_currency: str = "INR",
                    correlation_id: str | None = None) -> Mapping[str, Any]:
        """Mirror one hydrated vendor bill and its lines into the ledger.

        `source_currency` IS PART OF THE VERB, not an optional extra. A bill's
        amounts are denominated in it, `bill_line.amount_paise` is INR base
        paise, and a store handed the amounts without the currency has no way
        to tell the two apart -- which is the state the product was in for two
        migrations: `BillDTO.currency_code` populated by both adapters, dropped
        by `normalise`, and an FX engine downstream of a value that never
        arrived. It is declared here so a store that ignores it is ignoring
        something the protocol says exists.

        THE VERB THAT WAS MISSING, AND WHAT ITS ABSENCE COST. The ledger has
        been able to do this since 013; nothing called it.
        `SweepBillDetail.resume` fetched `GET /bills/{id}`, wrote the payload
        to the inbox, raised an exception when the response carried no lines,
        marked the bill hydrated -- and stopped. It never wrote `bill` or
        `bill_line`, and this protocol declared no verb that could.

        So `bill_line` stayed empty for the whole estate, and everything
        downstream reported a number that was structurally, permanently wrong
        rather than merely stale: every reconciliation line quoted
        ``billed_paise = 0`` and therefore ``open_commitment == ordered``;
        `recompute_commitment` subtracted a `billed` that was always zero, so
        a commitment never fell when its bill was paid; and
        `/api/integrations/reconciliation` reported ``identity_balanced: true``
        while quoting all of it. An identity between three figures that are all
        derived from the same empty table balances perfectly.

        RETURNS A SUMMARY, and it is not decoration: `attributed`,
        `quarantined` and `quarantined_paise` are what the sweep's progress
        detail reports, so an operator can see that a hydrated bill produced
        lines nobody could attribute rather than inferring it from a silence.

        REFUSALS PROPAGATE. A bill the ledger cannot honour -- no vendor, no
        date, no project to hang it on, a purchase order this system never
        sanctioned -- raises, the queue entry is NOT marked hydrated, and
        `jobs.py` fails the run and eventually marks it DEAD for a human.
        Swallowing it would drain the queue of a document nothing ever wrote,
        which is the silent drop §11.8 forbids wearing a completion badge.
        """
        ...

    def bills_awaiting_detail(self, *, connection_id: str,
                              limit: int) -> list[str]: ...

    def mark_detail_hydrated(self, *, connection_id: str,
                             external_id: str) -> None: ...

    # ---- completeness and control totals
    def periods_to_reconcile(self, *, connection_id: str) -> list[PeriodRef]: ...

    def control_total_slices(self, *, connection_id: str,
                             period: PeriodRef) -> list[tuple[str, str]]: ...

    def local_control_total(self, *, module: str, period: PeriodRef,
                            status: str) -> ControlTotal: ...

    def local_documents(self, *, module: str,
                        period: PeriodRef) -> list[LocalDocument]: ...


class ControlTotalSource(Protocol):
    """Source-side count and sum for one slice.

    Not part of C1: the frozen adapter protocol has no totals call, because
    totals come from a report endpoint rather than from a list walk. Declared
    as its own port so `sweep_control_totals` is testable now and so the gap
    is visible to stream 1 rather than assumed.
    """

    def source_control_total(self, *, module: str, period: PeriodRef,
                             status: str) -> ControlTotal: ...


def _adapter_call(adapter: Any, name: str) -> Any:
    method = getattr(adapter, name, None)
    if method is None:
        raise AdapterMethodMissing(
            f"the adapter does not implement {name}(). Plan §11.5 requires it; "
            f"the frozen C1 protocol in docs/WAVE5_CONTRACTS.md does not yet "
            f"declare it. This is a seam gap for stream 1, reported rather "
            f"than worked around.")
    return method


# ================================================================ the polls

@dataclass
class WindowedPoll:
    """`poll_bills`, `poll_purchaseorders`, `poll_items`, `poll_contacts`.

    One walk, four configurations, and the strategy read from `Capabilities`
    rather than from a product name — `delta_capability` names the flag this
    module reads, and a product that answers False gets a full re-pull instead
    of a window it cannot honour.

    The checkpoint is ``{window_since, window_until, full_repull, page,
    module}``. The window boundary IS the cursor (§11.3): there is no keyset
    to resume on, because `last_modified_time` is not a sortable column.

    The watermark advances only when the window is exhausted, and never inside
    the loop — see :func:`~app.backend.integration.jobs.next_watermark`.
    """

    kind: str
    module: str
    adapter: Any
    store: SweepStore
    connection_id: str
    list_method: str
    delta_capability: str
    page_size: int = 200
    max_pages: int = MAX_PAGES_PER_POLL
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    overlap_seconds: int = POLL_OVERLAP_SECONDS

    # ------------------------------------------------------------- strategy
    def delta_filter(self, capabilities: Capabilities | None) -> bool:
        """Whether this product can express 'changed since' for this module.

        `None` capabilities means we have not asked the adapter yet, and the
        fail-closed answer is False: a full re-pull is expensive and correct,
        a window we were not promised is cheap and wrong.
        """
        if capabilities is None:
            return False
        return bool(getattr(capabilities, self.delta_capability, False))

    def _window_from_checkpoint(self, checkpoint: Mapping[str, Any]) -> Window | None:
        if not checkpoint.get("window_until"):
            return None
        return Window(
            since=parse_iso(checkpoint.get("window_since")),
            until=parse_iso(checkpoint["window_until"]),
            full_repull=bool(checkpoint.get("full_repull")),
            overlap_seconds=self.overlap_seconds,
            reason=str(checkpoint.get("window_reason", "resumed")))

    # ----------------------------------------------------------------- work
    def resume(self, ctx: JobContext) -> Iterator[Progress]:
        hwm = self.store.get_watermark(connection_id=self.connection_id,
                                       module=self.module)
        window = self._window_from_checkpoint(ctx.checkpoint) or plan_window(
            now=ctx.now(), hwm=hwm,
            delta_filter=self.delta_filter(ctx.capabilities),
            window_seconds=self.window_seconds,
            overlap_seconds=self.overlap_seconds,
            reason=f"{self.module}: {self.delta_capability}="
                   f"{self.delta_filter(ctx.capabilities)}")
        page = int(ctx.checkpoint.get("page", 1))
        pages_walked = 0
        lister = _adapter_call(self.adapter, self.list_method)

        while pages_walked < self.max_pages:
            # One call per page, asked for BEFORE it is spent. Refused means
            # checkpoint and resume on the next tick, with the window intact.
            if not ctx.charge(1):
                return
            result = lister(since=window.since, until=window.until, page=page)
            records = page_records(result)
            accepted = 0
            for record in records:
                if self._accept(ctx, normalise(record, module=self.module)):
                    accepted += 1

            more = page_has_more(result, returned=len(records),
                                 page_size=self.page_size)
            pages_walked += 1
            page += 1
            checkpoint = {**window.as_checkpoint(), "module": self.module,
                          "page": page, "exhausted": not more}
            yield Progress(checkpoint=checkpoint, units=len(records),
                           detail={"accepted": accepted, "returned": len(records)})

            if not more:
                # Exhausted: and only now may the watermark move.
                moved = next_watermark(window, exhausted=True, current=hwm)
                if moved is not None and moved != hwm:
                    self.store.set_watermark(connection_id=self.connection_id,
                                             module=self.module, hwm=moved,
                                             now=ctx.now())
                # Clear the window so the next invocation plans a fresh one.
                yield Progress(
                    checkpoint={"module": self.module, "page": 1,
                                "hwm": iso(moved)},
                    units=0, note="window complete",
                    detail={"pages": pages_walked})
                return

        # Page budget spent with the window still open. The window stays in the
        # checkpoint, the watermark does NOT move, and the next tick resumes at
        # this page -- reporting a pause, never a completion.
        ctx.request_checkpoint("PAGE_LIMIT")

    # --------------------------------------------------------------- record
    def _accept(self, ctx: JobContext, record: SourceRecord) -> bool:
        """Persist one record to the inbox. True when it was new.

        The raw external status travels with it, verbatim, because C17 forbids
        overwriting it and an unmapped value must remain recoverable.
        """
        return self.store.upsert_inbox(
            connection_id=self.connection_id, module=self.module,
            external_id=record.external_id, payload_sha=record.sha,
            payload=record.payload, external_status_raw=record.status_raw,
            received_at=ctx.now(), correlation_id=ctx.correlation_id)


def poll_bills(adapter: Any, store: SweepStore, connection_id: str,
               **kwargs: Any) -> WindowedPoll:
    """5-minute windowed poll of bills. `bills_delta_filter` on both products."""
    kwargs.setdefault("page_size", CHUNK_SIZES["poll_bills"])
    return WindowedPoll(
        kind="poll_bills", module=MODULE_BILLS, adapter=adapter, store=store,
        connection_id=connection_id, list_method="list_bills",
        delta_capability="bills_delta_filter", **kwargs)


def poll_purchaseorders(adapter: Any, store: SweepStore, connection_id: str,
                        **kwargs: Any) -> "PollPurchaseOrders":
    """5-minute poll of POs, with unsanctioned-commitment detection."""
    kwargs.setdefault("page_size", CHUNK_SIZES["poll_purchaseorders"])
    return PollPurchaseOrders(
        kind="poll_purchaseorders", module=MODULE_PURCHASE_ORDERS,
        adapter=adapter, store=store, connection_id=connection_id,
        list_method="list_purchase_orders",
        delta_capability="po_delta_filter", **kwargs)


def poll_items(adapter: Any, store: SweepStore, connection_id: str,
               **kwargs: Any) -> WindowedPoll:
    """15-minute poll on Inventory (true delta); weekly full refresh elsewhere.

    The cadence difference is the scheduler's; the *strategy* difference is
    `items_delta_filter`, and it is read here.
    """
    kwargs.setdefault("page_size", CHUNK_SIZES["poll_items"])
    return WindowedPoll(
        kind="poll_items", module=MODULE_ITEMS, adapter=adapter, store=store,
        connection_id=connection_id, list_method="list_items",
        delta_capability="items_delta_filter", **kwargs)


def poll_contacts(adapter: Any, store: SweepStore, connection_id: str,
                  **kwargs: Any) -> WindowedPoll:
    """Hourly vendor-master poll.

    `contacts` has no `last_modified_time` filter on any of the three products
    (plan §11.3) — it is sortable, not filterable, which is the opposite
    problem. There is no `contacts_delta_filter` in `Capabilities`, so the
    lookup below finds nothing and the honest answer is the full refresh the
    plan prescribes. Naming a capability that does not exist would be a lie
    that reads as a bug; naming none would silently pick a window.
    """
    kwargs.setdefault("page_size", CHUNK_SIZES["poll_contacts"])
    return WindowedPoll(
        kind="poll_contacts", module=MODULE_CONTACTS, adapter=adapter,
        store=store, connection_id=connection_id, list_method="list_contacts",
        delta_capability="contacts_delta_filter", **kwargs)


@dataclass
class PollPurchaseOrders(WindowedPoll):
    """`poll_purchaseorders`, plus the detective control §11.7 requires.

    A PO created directly in Zoho bypasses `budget_check` — that is the
    acknowledged weakness of putting the PR in this product rather than in
    Zoho. Prevention is a tenant role configuration (D-8) we do not control.
    Detection is this, and it is always on regardless of D-8: any PO carrying a
    CAPEX dimension with no matching local `external_id` raises
    `UNSANCTIONED_COMMITMENT`, which blocks period close.

    The exception is raised on the poll rather than on a later sweep because
    the poll is where the PO first becomes visible, and a commitment we did not
    sanction should not be a day old before anybody is told.
    """

    def _accept(self, ctx: JobContext, record: SourceRecord) -> bool:
        inserted = super()._accept(ctx, record)
        if inserted and record.capex_reference and not self.store.known_purchase_order(
                connection_id=self.connection_id,
                external_id=record.external_id):
            face_value = _face_value_paise(record)
            foreign = ("" if face_value is not None or record.total_paise is None else
                       f" Face value {record.currency_code} {record.total_paise} "
                       f"(minor units of that currency, not paise; nothing is booked).")
            self.store.raise_exception(
                kind=KIND_UNSANCTIONED_COMMITMENT,
                object_type="purchase_order",
                object_id=record.external_id,
                detail=(f"PO {record.document_number or record.external_id} "
                        f"carries CAPEX dimension "
                        f"{record.capex_reference!r} but has no local "
                        f"purchase order. It was created outside this "
                        f"system's budget check." + foreign),
                raised_at=ctx.now(), entity_id=record.entity_id,
                project_id=record.project_id,
                # A foreign-currency order's total is a face value in its own
                # currency; only an INR figure is paise. See _face_value_paise.
                source_paise=face_value,
                correlation_id=ctx.correlation_id)
        return inserted


# ======================================================== PO-anchored sweep

@dataclass
class SweepPoAnchored:
    """`sweep_po_anchored` — on ERP, the ONLY way a receive is ever seen.

    Walks locally-open POs 50 at a time (plan §2.2) and reads the receive
    references off each PO's detail. Where the adapter exposes the split
    `po_receive_refs` / `get_receive` pair (see `erp.ErpAdapter`, added to
    close a budget under-count — `tests/ADAPTATIONS.md`), the budget is
    charged once for the PO-detail GET and once more per receive id, each
    charge immediately before the GET it pays for, so requests made and calls
    charged agree exactly. An adapter without that pair (Books/Inventory,
    where this walk is a completeness sweep rather than the sole mechanism)
    keeps the one-call-per-PO behaviour through `receives_for_po` unchanged.
    The checkpoint is `last_po_id_swept`; when the walk runs off the end it
    wraps to the beginning and increments `cycle`, because a **deleted receive
    arrives as an absence** and only a re-read of the PO detects it. A sweep
    that terminated at the last PO would never notice a deletion again.

    Every receive line is attributed to a known `po_line` or it is quarantined.
    There is no third branch. Pro-rata spreading and silent drops are both
    forbidden by §11.8, and the absence of an `else` here is that rule.
    """

    adapter: Any
    store: SweepStore
    connection_id: str
    kind: str = "sweep_po_anchored"
    batch_size: int = CHUNK_SIZES["sweep_po_anchored"]
    #: The `external_source` label stamped on every GRN this sweep mirrors --
    #: `ZOHO_ERP`, `ZOHO_BOOKS`. OUR label for where a document came from, not
    #: a product name we invent from the adapter: `002_financial_controls.sql`
    #: and `pg/masters.ingest_from_adapter` already spell it this way, and a
    #: mirrored row whose `external_source` disagrees with theirs is a row the
    #: `(external_source, external_id)` mirror indexes cannot match.
    #:
    #: `None` IS NO LONGER A WRITABLE VALUE, and the correction is worth
    #: stating because this comment used to defend it. It said the store would
    #: "write NULL instead of guessing a source", which was honest about the
    #: guess and wrong about the consequence: nothing ever constructed this
    #: field with a value, so EVERY mirrored GRN took a second INSERT branch
    #: that also dropped `payload_sha` and `external_last_modified` -- §6.1's
    #: four-column provenance block came out one of four populated, and
    #: `ux_grn_external`, NULLS DISTINCT in its leading column, constrained
    #: nothing. `pg.procurement.record_receive_line` now REFUSES a blank
    #: source, so a wiring that omits this fails loudly on its first receive
    #: rather than quietly filling the ledger with untraceable rows. It is
    #: still defaulted only so this dataclass keeps the three-argument shape
    #: every existing caller constructs it with.
    external_source: str | None = None

    def sole_grn_mechanism(self, capabilities: Capabilities | None) -> bool:
        """True when this sweep is the only GRN acquisition path there is.

        `receives_listable=False` — ERP — means yes: there is no list endpoint
        and no delta feed, so anything this walk misses is not acquired at all.
        On a product that can list receives this is a completeness sweep
        instead, and the checkpoint says which it was, because the two have
        very different consequences when the walk falls behind.
        """
        return capabilities is None or not capabilities.receives_listable

    def resume(self, ctx: JobContext) -> Iterator[Progress]:
        sole = self.sole_grn_mechanism(ctx.capabilities)
        after = ctx.checkpoint.get("last_po_id_swept")
        cycle = int(ctx.checkpoint.get("cycle", 0))

        batch = self.store.open_purchase_orders(
            connection_id=self.connection_id, after_po_id=after,
            limit=self.batch_size)
        if not batch:
            # Off the end of the open-PO population: start again next tick.
            yield Progress(
                checkpoint={"last_po_id_swept": None, "cycle": cycle + 1,
                            "sole_grn_mechanism": sole},
                units=0, note="cycle complete",
                detail={"cycle": cycle + 1})
            return

        for po in batch:
            # Cost scales with OPEN PO COUNT plus receive volume: the PO
            # detail is one GET and each receive it names is one more (see
            # `erp.ErpAdapter.receives_for_po`). Charged before the requests
            # it pays for, never after (`JobContext.charge`), and a refusal
            # stops the walk exactly where it is -- mid-PO, if it comes to
            # that, so the PO is retried whole next time rather than
            # checkpointed half-read.
            if not ctx.charge(1):
                return
            po_receive_refs = getattr(self.adapter, "po_receive_refs", None)
            get_receive = getattr(self.adapter, "get_receive", None)
            if po_receive_refs is not None and get_receive is not None:
                receive_ids = po_receive_refs(po_external_id=po.external_id)
                receives = []
                for receive_id in receive_ids:
                    if not ctx.charge(1):
                        return
                    receives.append(get_receive(
                        receive_id, fallback_po=po.external_id))
            else:
                # No split surface on this adapter (Books/Inventory, where
                # this walk is a completeness sweep, not the sole mechanism):
                # unchanged one-charge-per-PO behaviour.
                receives = _adapter_call(self.adapter, "receives_for_po")(
                    po_external_id=po.external_id)
            unattributed = 0
            attributed = 0
            for receive in receives or []:
                record = normalise(receive, module=MODULE_PURCHASE_RECEIVES)
                self.store.upsert_inbox(
                    connection_id=self.connection_id,
                    module=MODULE_PURCHASE_RECEIVES,
                    external_id=record.external_id, payload_sha=record.sha,
                    payload=record.payload,
                    external_status_raw=record.status_raw,
                    received_at=ctx.now(),
                    correlation_id=ctx.correlation_id)
                for index, line in enumerate(record.lines):
                    if self._attribute(ctx, po, record, line, index):
                        attributed += 1
                    else:
                        unattributed += 1

            after = po.po_id
            yield Progress(
                checkpoint={"last_po_id_swept": after, "cycle": cycle,
                            "sole_grn_mechanism": sole},
                units=1,
                detail={"po": po.external_id, "receives": len(receives or []),
                        "attributed": attributed,
                        "unattributed": unattributed})

    # ------------------------------------------------------------ §11.8
    def _attribute(self, ctx: JobContext, po: LocalPurchaseOrder,
                   receive: SourceRecord, line: Any, index: int) -> bool:
        """Attribute one receive line, or quarantine it. Never anything else.

        Documentation says the linkage exists — receive lines carry the PO's
        `line_item_id`. **Documented is not populated**, and until Phase 0B-3
        proves it against the client's own tenant this branch is the one that
        matters: a line that does not resolve is quarantined at full value in a
        project-level bucket, visible on SCR-16 and SCR-27, and blocking
        capitalisation.
        """
        # `purchase_order_line_external_id` FIRST, because that is the field
        # `LineDTO` actually carries. The other spellings are kept only for a
        # raw mapping that has not been through the adapter yet; they are
        # aliases for foreign shapes, NOT for our own DTO, and reading them
        # first is what made this read `None` on every well-formed line.
        line_external_id = _first_attr(
            line, ("purchase_order_line_external_id", "po_line_external_id",
                   "line_item_id", "purchaseorder_item_id", "line_id"))
        # The exception's identity falls back to the line's ORDINAL when the
        # source gave it no identifier -- which on ERP is the expected case,
        # not the exotic one. Keying two identifierless lines of the same
        # receive on the same string would collapse them into one exception,
        # and the second line's value would vanish from the bucket: a silent
        # drop, arriving through the very code that exists to prevent one.
        line_key = str(line_external_id) if line_external_id else f"#{index}"
        # `line_total_paise` FIRST, for the same reason. Reading a name the
        # DTO does not have returned the default 0, so the bucket this whole
        # branch exists to fill accumulated nothing while reporting success.
        amount_paise = _first_attr(
            line, ("line_total_paise", "amount_paise", "total_paise"), 0) or 0

        # THE ORDER'S CURRENCY DECIDES BEFORE THE LINKAGE DOES. Product owner
        # decision 8 (2026-09-11): a receive against a non-INR order is refused
        # with a coded reconciliation exception until it carries its own
        # currency and rate, and nothing is booked at face value. A purchase
        # receive carries neither -- on Zoho ERP it has no `currency_code` and
        # no `exchange_rate` at all; it inherits the order's -- so against a
        # JPY order the figure this line states is YEN, whatever the DTO field
        # is called, and there is no basis on which to make it paise.
        #
        # Checked BEFORE `resolve_po_line`, deliberately. The other two
        # outcomes of this function both write that figure into a paise
        # column: attribution records it on `grn_line.amount_paise`, and the
        # quarantine below holds it "at full value" in the unattributed
        # bucket. Either would book a yen amount as rupees, which is the
        # defect the decision closes. So a foreign order takes neither path:
        # the line is held under its own kind, with NO `source_paise` (the
        # only figure available is not paise) and NO bucket contribution, and
        # the receive stays in the inbox unmatched. `pg.procurement.
        # record_receive_line` refuses the same case with the same code, so a
        # caller that reaches the ledger around this sweep gets the same
        # answer.
        order_currency = str(po.currency_code or BASE_CURRENCY).strip().upper()
        if order_currency != BASE_CURRENCY:
            receive_label = (f" ({receive.document_number})"
                             if receive.document_number else "")
            order_label = (f" ({po.document_number})"
                           if po.document_number else "")
            self.store.raise_exception(
                kind=KIND_FOREIGN_CURRENCY_BASIS_MISSING,
                object_type="grn_line",
                object_id=f"{receive.external_id}:{line_key}",
                detail=(
                    f"Receive {receive.external_id}{receive_label} line "
                    f"{line_external_id or '(no line identifier)'} is against "
                    f"purchase order {po.external_id}{order_label}, which is "
                    f"denominated in {order_currency}. A purchase receive "
                    f"carries no currency and no exchange rate of its own, so "
                    f"its stated line amount, {int(amount_paise)} "
                    f"{order_currency} minor units, is a face value in the "
                    f"order's currency and has NOT been booked as paise. "
                    f"Missing: the receive's own currency and its rate to "
                    f"{BASE_CURRENCY} for its date. Nothing was recorded on "
                    f"grn_line and nothing was added to the unattributed "
                    f"bucket; the receive stays in the inbox, unmatched, until "
                    f"a basis is supplied (decision 8, 2026-09-11)."),
                raised_at=ctx.now(), entity_id=po.entity_id,
                project_id=po.project_id,
                correlation_id=ctx.correlation_id)
            return False

        po_line_id = self.store.resolve_po_line(
            po_external_id=po.external_id,
            line_external_id=str(line_external_id) if line_external_id else None)

        if po_line_id is not None:
            self.store.record_receive_line(
                po_line_id=po_line_id,
                receive_external_id=receive.external_id,
                line_external_id=str(line_external_id) if line_external_id else None,
                quantity=_first_attr(line, ("quantity", "qty")),
                amount_paise=amount_paise,
                # PROVENANCE, §11.10. `payload_sha` says which VERSION of the
                # source document this row was built from, so a later re-read
                # that disagrees is detectable rather than merely different.
                external_source=self.external_source,
                receive_number=receive.document_number,
                # The source gives a DATE and `grn.received_at` is a
                # timestamptz, so a receipt dated 2026-09-07 is stored at
                # midnight UTC. The midnight is an artefact of the two types,
                # NOT a claim about the hour, and the source's own value stays
                # verbatim in the inbox payload either way. When the source
                # gave no date at all this falls back to the observed time,
                # which is a different fact and is the only one we have.
                received_at=_as_datetime(receive.document_date) or ctx.now(),
                external_last_modified=receive.last_modified_time,
                payload_sha=receive.sha)
            return True

        exception_id = self.store.raise_exception(
            kind=KIND_GRN_LINE_UNATTRIBUTED,
            object_type="grn_line",
            object_id=f"{receive.external_id}:{line_key}",
            detail=(f"Receive {receive.external_id} line "
                    f"{line_external_id or '(no line identifier)'} does not "
                    f"resolve to a known po_line on PO {po.external_id}. "
                    f"Quarantined at full value; never spread pro-rata."),
            raised_at=ctx.now(), entity_id=po.entity_id,
            project_id=po.project_id, source_paise=amount_paise,
            correlation_id=ctx.correlation_id)
        self.store.accumulate_unattributed(project_id=po.project_id,
                                           paise=int(amount_paise),
                                           source_key=exception_id)
        return False


# ===================================================== bill detail hydration

@dataclass
class SweepBillDetail:
    """`sweep_bill_detail` — queue-driven, continuous.

    A list response omits `line_items` (plan §11.5), and without lines a bill
    cannot be attributed to a WBS element at all. So every bill the poll
    accepted is queued for a `GET /bills/{id}`, one call each, until the queue
    drains. The checkpoint is the queue itself: the last hydrated id is
    recorded so a resumed run does not re-fetch what it already paid for.

    HYDRATION IS NOT THE END OF THE JOB, AND USED TO BE. This sweep fetched the
    lines, wrote the payload to the inbox and marked the bill hydrated without
    ever writing `bill` or `bill_line` -- so the call was paid for, the lines
    were read, and the ledger stayed empty. See
    :meth:`SweepStore.mirror_bill` for what that cost every figure downstream.
    The mirror now runs BEFORE `mark_detail_hydrated`, so a bill the ledger
    refuses stays on the queue instead of being marked done by the very step
    that failed to record it.
    """

    adapter: Any
    store: SweepStore
    connection_id: str
    kind: str = "sweep_bill_detail"
    batch_size: int = CHUNK_SIZES["sweep_bill_detail"]
    #: OUR label for where these documents came from -- `ZOHO_ERP`,
    #: `ZOHO_BOOKS` -- stamped on every `bill` this sweep mirrors, exactly as
    #: `SweepPoAnchored.external_source` is stamped on every `grn`.
    #: `002_financial_controls.sql` and `pg/masters.ingest_from_adapter`
    #: already spell it this way, and a mirrored row whose `external_source`
    #: disagrees with theirs is a row the `(external_source, external_id)`
    #: mirror index cannot match.
    #:
    #: `None` is not a usable value: the ledger REFUSES a blank source rather
    #: than writing a row whose provenance points nowhere. It is left
    #: defaulted only so this dataclass keeps the three-argument shape every
    #: existing caller constructs it with; a wiring that omits it fails loudly
    #: on its first bill rather than quietly mirroring untraceable documents.
    external_source: str | None = None

    def resume(self, ctx: JobContext) -> Iterator[Progress]:
        queued = self.store.bills_awaiting_detail(
            connection_id=self.connection_id, limit=self.batch_size)
        if not queued:
            yield Progress(checkpoint={"last_hydrated": None, "drained": True},
                           units=0, note="queue empty")
            return

        getter = _adapter_call(self.adapter, "get_bill")
        for external_id in queued:
            if not ctx.charge(1):
                return
            record = normalise(getter(external_id=external_id),
                               module=MODULE_BILLS)
            self.store.upsert_inbox(
                connection_id=self.connection_id, module=MODULE_BILLS,
                external_id=record.external_id, payload_sha=record.sha,
                payload=record.payload,
                external_status_raw=record.status_raw,
                received_at=ctx.now(), correlation_id=ctx.correlation_id)
            mirrored: Mapping[str, Any] | None = None
            if not record.has_line_items:
                # Hydration that returned no lines is not hydration. Saying so
                # is the difference between a bill nobody can attribute and a
                # bill everybody believes was attributed.
                self.store.raise_exception(
                    kind=KIND_CONTROL_TOTAL_MISMATCH,
                    object_type="bill", object_id=record.external_id,
                    detail=(f"Bill {record.external_id} detail fetch returned "
                            f"no line items; it cannot be attributed to a WBS "
                            f"element."),
                    raised_at=ctx.now(), entity_id=record.entity_id,
                    project_id=record.project_id,
                    source_paise=_face_value_paise(record),
                    correlation_id=ctx.correlation_id)
            else:
                mirrored = self._mirror(ctx, record)
            self.store.mark_detail_hydrated(connection_id=self.connection_id,
                                            external_id=record.external_id)
            yield Progress(
                checkpoint={"last_hydrated": record.external_id,
                            "drained": False},
                units=1,
                detail={
                    "lines": len(record.lines),
                    # Reported, not inferred from a silence: a hydrated bill
                    # whose every line quarantined looks exactly like one that
                    # posted cleanly unless these are on the progress record.
                    "attributed": None if mirrored is None
                                  else mirrored.get("attributed"),
                    "quarantined": None if mirrored is None
                                   else mirrored.get("quarantined"),
                    "quarantined_paise": None if mirrored is None
                                         else mirrored.get("quarantined_paise"),
                })

    # -------------------------------------------------------- §11.5 + §11.8
    def _mirror(self, ctx: JobContext,
                record: SourceRecord) -> Mapping[str, Any] | None:
        """Write one hydrated bill into the ledger, or refuse to guess at it.

        THE PURCHASE-ORDER LINKAGE IS SINGULAR OR IT IS NOTHING. `bill.po_id`
        is one column and `mirror_bill` takes one `po_external_id`, while
        `dto.BillDTO.purchase_order_external_ids` is a TUPLE, because a Zoho
        bill may genuinely span several orders. None of them is an ordinary
        non-PO bill. Exactly one is the ordinary case. TWO OR MORE is refused:
        picking the first would attribute a whole document -- and every line's
        control cell through it -- to whichever purchase order the source
        happened to list first, which is the guess §11.8 forbids in the one
        place where guessing wrong moves money into another project's budget.
        The bill is held at its FULL value in an exception instead, visible on
        SCR-16 and SCR-27 and blocking capitalisation, and nothing is written.
        """
        if len(record.po_external_ids) > 1:
            self.store.raise_exception(
                kind=KIND_CONTROL_TOTAL_MISMATCH,
                object_type="bill", object_id=record.external_id,
                detail=(f"Bill {record.external_id} names "
                        f"{len(record.po_external_ids)} purchase orders "
                        f"({', '.join(record.po_external_ids)}). `bill.po_id` "
                        f"holds one, and choosing between them would "
                        f"attribute the whole document -- and every line's "
                        f"control cell -- to whichever the source listed "
                        f"first. Held at full value; nothing was mirrored."),
                raised_at=ctx.now(), entity_id=record.entity_id,
                project_id=record.project_id,
                source_paise=_face_value_paise(record),
                correlation_id=ctx.correlation_id)
            return None

        mirror = getattr(self.store, "mirror_bill", None)
        if mirror is None:
            # REFUSED, not skipped. A store that cannot mirror would let this
            # sweep pay for the call, read the lines, mark the bill hydrated
            # and write nothing -- leaving `bill_line` empty for ever while
            # every reconciliation figure quoted zero billed and claimed the
            # identity balanced. Reported against the seam by name, because
            # that is the wiring defect, not a property of this document.
            raise SweepError(
                "sweep_bill_detail: the injected store implements no "
                "`mirror_bill` verb, so a hydrated bill would be fetched, "
                "paid for and then discarded -- `bill` and `bill_line` would "
                "stay empty, `billed_paise` would be structurally 0 for the "
                "whole estate, and the reconciliation identity would balance "
                "on three figures all derived from an empty table. "
                "SweepStore.mirror_bill is not optional.")
        # THE BILL'S OWN RATE, WHEN IT STATES ONE, travels with its currency.
        # Decision 8 (2026-09-11) lets a bill against a non-INR order book
        # only when it "carries its own currency and rate"; the ledger
        # resolves the rate (`fx.resolve_basis`: the stated rate recorded
        # against the bill's date, else the ACTIVE rate on file for that
        # date) and refuses with FOREIGN_CURRENCY_BASIS_MISSING when there is
        # neither. The rate's SOURCE is the document itself, named per bill:
        # `fx_rate` is unique on (pair, date, source) and never overwritten,
        # so two bills dated the same day at two dealt rates must each cite
        # their own row rather than the second being refused as a conflict
        # with the first.
        stated_rate = record.exchange_rate
        rate_source = (f"{self.external_source} bill {record.external_id}"
                       if stated_rate is not None else None)
        try:
            return mirror(
                external_source=self.external_source,
                external_id=record.external_id,
                bill_number=record.document_number,
                vendor_name=record.vendor_name,
                bill_date=record.document_date,
                lines=record.lines,
                po_external_id=(record.po_external_ids[0]
                                if record.po_external_ids else None),
                project_id=record.project_id,
                entity_id=record.entity_id,
                exchange_rate=stated_rate,
                fx_rate_source=rate_source,
            # VERBATIM, never interpreted here. C17's mapping happens in the
            # ledger, which is also where the UNMAPPED_EXTERNAL_STATUS
            # exception is raised; this sweep only carries the raw value
            # across.
            external_status_raw=record.status_raw,
            # §11.10. `payload_sha` says which VERSION of the source document
            # the mirrored rows were built from, so a later re-read that
            # disagrees is detectable rather than merely different.
            external_last_modified=record.last_modified_time,
            payload_sha=record.sha,
            # THE WIRE THAT WAS MISSING. Everything downstream of this argument
            # -- migration 023's five FX tables, its two immutability triggers,
            # the whole of `pg/fx.py` -- existed and was reachable from
            # nothing, because the currency stopped here. `mirror_bill`
            # resolves the rate for `record.document_date` and refuses the bill
            # outright if none is on file, rather than writing a euro figure
            # into a rupee column, which is what it did before.
                source_currency=record.currency_code,
                correlation_id=ctx.correlation_id)
        except Exception as exc:  # noqa: BLE001 -- matched on `.code`; everything else re-raised
            # ONE refusal is a reconciliation fact rather than a wiring fault,
            # and it is recognised by its code, not its type: the sweeps do
            # not import the ledger. FOREIGN_CURRENCY_BASIS_MISSING means the
            # bill is against a non-INR order and carries no currency-and-rate
            # basis the ledger can honour (decision 8). It is held OPEN under
            # that kind -- blocking capitalisation and period close like every
            # Open exception -- with the ledger's own sentence as its detail,
            # NO `source_paise` (the face value is not paise), and the inbox
            # row untouched, so the source document is recoverable when the
            # basis arrives. The queue entry is marked hydrated by the caller
            # exactly as the other held-at-full-value branches above are: the
            # detail WAS fetched and paid for, and re-fetching it every tick
            # would not change what it lacks. Every OTHER refusal still
            # propagates, for the reason `SweepStore.mirror_bill` gives.
            if getattr(exc, "code", None) != KIND_FOREIGN_CURRENCY_BASIS_MISSING:
                raise
            self.store.raise_exception(
                kind=KIND_FOREIGN_CURRENCY_BASIS_MISSING,
                object_type="bill", object_id=record.external_id,
                detail=str(getattr(exc, "message", None) or exc),
                raised_at=ctx.now(), entity_id=record.entity_id,
                project_id=record.project_id,
                correlation_id=ctx.correlation_id)
            return None


def _face_value_paise(record: SourceRecord) -> int | None:
    """`record.total_paise` when it IS paise, and None when it is not.

    A bill labelled JPY carries its total in yen, whatever `total_paise` is
    called on the DTO. Writing that figure into `reconciliation_exception.
    source_paise` would state a yen amount as rupees in the one column the
    closure gate sums (`closure._open_exceptions`), which is the silent
    relabelling decision 8 forbids. Only a base-currency document's total is
    paise; every other document's face value is named in `detail`, with its
    currency, where it cannot be summed as rupees.
    """
    if str(record.currency_code or BASE_CURRENCY).strip().upper() == BASE_CURRENCY:
        return record.total_paise
    return None


# ======================================================= completeness sweeps

@dataclass
class SweepControlTotals:
    """`sweep_control_totals` — nightly, one period per invocation.

    Per (module, period, status): count and sum at the source against count and
    sum locally. A disagreement raises `CONTROL_TOTAL_MISMATCH` carrying both
    numbers, so the exception says what the difference IS rather than that
    there is one.

    **This is not made redundant by a working `last_modified_time` filter.** A
    filter tells you what changed; it cannot tell you that it told you about
    everything that changed. Only an independent count can, and this is that
    count.
    """

    source: ControlTotalSource
    store: SweepStore
    connection_id: str
    kind: str = "sweep_control_totals"

    def resume(self, ctx: JobContext) -> Iterator[Progress]:
        periods = self.store.periods_to_reconcile(
            connection_id=self.connection_id)
        done = set(ctx.checkpoint.get("periods_done", []))
        remaining = [p for p in periods if p.period_id not in done]
        if not remaining:
            yield Progress(checkpoint={"periods_done": [], "cycle_complete": True},
                           units=0, note="all periods reconciled")
            return

        period = remaining[0]
        for module, status in self.store.control_total_slices(
                connection_id=self.connection_id, period=period):
            if not ctx.charge(1):
                return
            source = self.source.source_control_total(
                module=module, period=period, status=status)
            local = self.store.local_control_total(
                module=module, period=period, status=status)
            if (source.count, source.sum_paise) != (local.count, local.sum_paise):
                self.store.raise_exception(
                    kind=KIND_CONTROL_TOTAL_MISMATCH,
                    object_type=module,
                    object_id=f"{period.period_id}:{status}",
                    detail=(f"{module} {status} in {period.period_id}: source "
                            f"{source.count} documents / {source.sum_paise} "
                            f"paise, local {local.count} / {local.sum_paise}."),
                    raised_at=ctx.now(), entity_id=period.entity_id,
                    local_paise=local.sum_paise, source_paise=source.sum_paise,
                    correlation_id=ctx.correlation_id)
            yield Progress(
                checkpoint={"periods_done": sorted(done),
                            "period": period.period_id, "module": module,
                            "status": status},
                units=1,
                detail={"source_count": source.count, "local_count": local.count})

        done.add(period.period_id)
        yield Progress(checkpoint={"periods_done": sorted(done)}, units=0,
                       note="period complete")
        if len(remaining) > 1:
            # One period per invocation (plan §2.2). More remain, so this run
            # pauses rather than reporting a completion it did not achieve.
            ctx.request_checkpoint("ONE_PERIOD_PER_INVOCATION")


#: `PREFIX-0001` style document numbers: everything up to the final run of
#: digits is the series, the digits are its ordinal.
_DOCUMENT_NUMBER = re.compile(r"^(?P<series>.*?)(?P<ordinal>\d+)$")


def document_number_gaps(numbers: Sequence[str | None]) -> dict[str, list[str]]:
    """Missing ordinals per series, from the numbers we actually hold.

    Zoho document numbers are sequential per series, so a hole in the sequence
    is a document the source has and we do not — the failure a delta filter
    cannot report, because a filter that skipped a record reports success.

    Deliberately conservative about what it will call a gap: only within a
    series we already hold both sides of, and only for numbers that parse.
    An over-eager gap detector that cried wolf on every renumbering would be
    turned off within a week, and then it would detect nothing at all.
    """
    series: dict[str, list[tuple[int, int]]] = {}
    for raw in numbers:
        if not raw:
            continue
        match = _DOCUMENT_NUMBER.match(str(raw).strip())
        if not match:
            continue
        ordinal_text = match.group("ordinal")
        series.setdefault(match.group("series"), []).append(
            (int(ordinal_text), len(ordinal_text)))

    gaps: dict[str, list[str]] = {}
    for prefix, seen in series.items():
        ordinals = sorted({o for o, _ in seen})
        if len(ordinals) < 2:
            continue
        width = max(w for _, w in seen)
        missing = [f"{prefix}{o:0{width}d}"
                   for o in range(ordinals[0], ordinals[-1] + 1)
                   if o not in set(ordinals)]
        if missing:
            gaps[prefix] = missing
    return gaps


@dataclass
class SweepCompleteness:
    """`sweep_completeness` — nightly gap detection and late-arrival checks.

    Two questions per period, neither of which a delta filter can answer:

    1. **Is the document series contiguous?** A hole means a document exists at
       the source that we never received.
    2. **Did anything land in a period that has already closed?** A document
       dated into a CLOSED period raises `LATE_ARRIVAL_CLOSED_PERIOD` — the
       number in the closed period's report is now wrong, and somebody has to
       decide what to do about it. It is never quietly posted into the open
       period instead.
    """

    store: SweepStore
    connection_id: str
    kind: str = "sweep_completeness"
    modules: tuple[str, ...] = (MODULE_BILLS, MODULE_PURCHASE_ORDERS)

    def resume(self, ctx: JobContext) -> Iterator[Progress]:
        periods = self.store.periods_to_reconcile(
            connection_id=self.connection_id)
        done = set(ctx.checkpoint.get("periods_done", []))
        remaining = [p for p in periods if p.period_id not in done]
        if not remaining:
            yield Progress(checkpoint={"periods_done": [], "cycle_complete": True},
                           units=0, note="all periods checked")
            return

        period = remaining[0]
        for module in self.modules:
            documents = self.store.local_documents(module=module, period=period)
            gaps = document_number_gaps([d.document_number for d in documents])
            for series, missing in sorted(gaps.items()):
                self.store.raise_exception(
                    kind=KIND_CONTROL_TOTAL_MISMATCH,
                    object_type=module,
                    object_id=f"{period.period_id}:{series}",
                    detail=(f"{module} series {series!r} in "
                            f"{period.period_id} is missing "
                            f"{len(missing)} document number(s): "
                            f"{', '.join(missing[:20])}"
                            f"{'…' if len(missing) > 20 else ''}. A delta "
                            f"filter cannot prove it returned everything; "
                            f"this gap is the evidence it did not."),
                    raised_at=ctx.now(), entity_id=period.entity_id,
                    correlation_id=ctx.correlation_id)

            late = [d for d in documents if self._is_late_arrival(d, period)]
            for document in late:
                self.store.raise_exception(
                    kind=KIND_LATE_ARRIVAL_CLOSED_PERIOD,
                    object_type=module, object_id=document.external_id,
                    detail=(f"{module} {document.document_number or document.external_id} "
                            f"is dated {document.document_date}, inside "
                            f"{period.period_id}, which is CLOSED. It arrived "
                            f"at {iso(document.received_at)}."),
                    raised_at=ctx.now(), entity_id=document.entity_id or period.entity_id,
                    project_id=document.project_id,
                    source_paise=document.total_paise,
                    correlation_id=ctx.correlation_id)

            yield Progress(
                checkpoint={"periods_done": sorted(done),
                            "period": period.period_id, "module": module},
                units=len(documents),
                detail={"gaps": sum(len(v) for v in gaps.values()),
                        "late_arrivals": len(late)})

        done.add(period.period_id)
        yield Progress(checkpoint={"periods_done": sorted(done)}, units=0,
                       note="period complete")
        if len(remaining) > 1:
            ctx.request_checkpoint("ONE_PERIOD_PER_INVOCATION")

    @staticmethod
    def _is_late_arrival(document: LocalDocument, period: PeriodRef) -> bool:
        return (period.closed and period.contains(document.document_date)
                and document.received_at is not None)


# ============================================================== the gate
# A period cannot close while an Open reconciliation exception exists, and a
# project cannot capitalise while one is outstanding against it.
#
# The PostgreSQL close path already enforces the first, in
# `app/backend/pg/periods.py::_has_open_reconciliation_exceptions`, which reads
# `reconciliation_exception WHERE entity_id = ... AND status = 'Open'`. These
# two functions are the same rule expressed over the store port, for two
# reasons: a Cron Function that never imports the period service can still
# pre-check before proposing a close, and the sweeps' own tests can prove that
# the exceptions they raise are close-blocking rather than merely assuming the
# shape periods.py queries for. The rule is stated once in each layer and both
# read the same rows -- entity_id, status 'Open' -- so they cannot disagree
# about which exceptions block.


def period_close_blockers(store: SweepStore, *, entity_id: str) -> list[str]:
    """Human-readable reasons `entity_id` may not close a period. Empty = may.

    Extends the gate `approve_capitalisation` already applies (plan §11.5):
    the same Open exceptions that block capitalisation block a close, because a
    period closed over an unreconciled difference publishes a number nobody can
    stand behind.
    """
    blockers = []
    for exception in store.open_exceptions(entity_id=entity_id):
        blockers.append(
            f"{exception.get('kind')} open on "
            f"{exception.get('object_type')} {exception.get('object_id')}: "
            f"{exception.get('detail')}")
    return blockers


def capitalisation_blockers(store: SweepStore, *, project_id: str,
                            unattributed_paise: int = 0) -> list[str]:
    """Reasons `project_id` may not capitalise. Empty = it may.

    The unattributed bucket is a blocker at any non-zero value. There is no
    materiality threshold here on purpose: §11.8 says never spread pro-rata and
    never drop, and a threshold is a drop with a number attached to it.
    """
    blockers = [
        f"{exception.get('kind')} open on "
        f"{exception.get('object_type')} {exception.get('object_id')}"
        for exception in store.open_exceptions(project_id=project_id)
    ]
    if unattributed_paise:
        blockers.append(
            f"unattributed receipts of {unattributed_paise} paise on "
            f"{project_id} are not attributed to any po_line")
    return blockers


# ================================================================= registry

#: Every job in this module, with the cadence plan §2.2 and §11.5 schedule it
#: at. The cadence is data, not a comment: the Catalyst cron definitions are
#: generated from it, and the development environment's documented 500
#: executions/project/day cap is checked against it.
JOB_CADENCES: Mapping[str, str] = {
    "poll_bills": "5 min",
    "poll_purchaseorders": "5 min",
    "poll_items": "15 min (Inventory delta) / weekly (full refresh)",
    "poll_contacts": "hourly",
    "sweep_po_anchored": "15 min",
    "sweep_bill_detail": "continuous, queue-driven",
    "sweep_control_totals": "nightly",
    "sweep_completeness": "nightly",
}


__all__ = [
    "AdapterMethodMissing", "BASE_CURRENCY", "ControlTotal", "ControlTotalSource",
    "EXCEPTION_KINDS", "EXCEPTION_OPEN", "JOB_CADENCES",
    "KIND_ADOPTION_DIMENSION_CONFLICT", "KIND_ADOPTION_DIMENSION_INVALID",
    "KIND_CONTROL_TOTAL_MISMATCH", "KIND_FOREIGN_CURRENCY_BASIS_MISSING",
    "KIND_GRN_LINE_UNATTRIBUTED",
    "KIND_LATE_ARRIVAL_CLOSED_PERIOD", "KIND_UNMAPPED_EXTERNAL_STATUS",
    "KIND_UNSANCTIONED_COMMITMENT", "LocalDocument", "LocalPurchaseOrder",
    "MODULE_BILLS", "MODULE_CONTACTS", "MODULE_ITEMS",
    "MODULE_PURCHASE_ORDERS", "MODULE_PURCHASE_RECEIVES", "PeriodRef",
    "PollPurchaseOrders", "ProcurementPort", "SourceRecord", "SweepBillDetail",
    "SweepCompleteness", "SweepControlTotals", "SweepError", "SweepPoAnchored",
    "SweepStore", "WindowedPoll", "capitalisation_blockers",
    "document_number_gaps", "normalise", "page_has_more", "page_records",
    "payload_sha", "period_close_blockers", "poll_bills", "poll_contacts",
    "poll_items", "poll_purchaseorders", "raw_payload",
]
