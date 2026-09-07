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
# The frozen five from research/30_contracts/C18_domain_statuses.json. A sweep
# that needs a sixth REPORTS it; it does not invent one here. Gap detection
# therefore lands in CONTROL_TOTAL_MISMATCH -- a gap IS a count disagreement --
# with the missing numbers named in `detail`, rather than under a
# DOCUMENT_NUMBER_GAP kind that no registry declares.
KIND_GRN_LINE_UNATTRIBUTED = "GRN_LINE_UNATTRIBUTED"
KIND_CONTROL_TOTAL_MISMATCH = "CONTROL_TOTAL_MISMATCH"
KIND_LATE_ARRIVAL_CLOSED_PERIOD = "LATE_ARRIVAL_CLOSED_PERIOD"
KIND_UNSANCTIONED_COMMITMENT = "UNSANCTIONED_COMMITMENT"
KIND_UNMAPPED_EXTERNAL_STATUS = "UNMAPPED_EXTERNAL_STATUS"

EXCEPTION_KINDS: frozenset[str] = frozenset({
    KIND_GRN_LINE_UNATTRIBUTED, KIND_CONTROL_TOTAL_MISMATCH,
    KIND_LATE_ARRIVAL_CLOSED_PERIOD, KIND_UNSANCTIONED_COMMITMENT,
    KIND_UNMAPPED_EXTERNAL_STATUS,
})

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
    lines: tuple[Any, ...] = ()

    @property
    def has_line_items(self) -> bool:
        return bool(self.lines)


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
        capex_reference=_first_attr(
            record, ("capex_reference", "cf_capex_ref", "capex_ref",
                     "wbs_code")),
        lines=tuple(lines),
    )


# ===================================================================== ports

@dataclass(frozen=True)
class LocalPurchaseOrder:
    """A PO we know about, as the PO-anchored walk needs it."""

    po_id: str
    external_id: str
    entity_id: str | None = None
    project_id: str | None = None
    document_number: str | None = None


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
            self.store.raise_exception(
                kind=KIND_UNSANCTIONED_COMMITMENT,
                object_type="purchase_order",
                object_id=record.external_id,
                detail=(f"PO {record.document_number or record.external_id} "
                        f"carries CAPEX dimension "
                        f"{record.capex_reference!r} but has no local "
                        f"purchase order. It was created outside this "
                        f"system's budget check."),
                raised_at=ctx.now(), entity_id=record.entity_id,
                project_id=record.project_id,
                source_paise=record.total_paise,
                correlation_id=ctx.correlation_id)
        return inserted


# ======================================================== PO-anchored sweep

@dataclass
class SweepPoAnchored:
    """`sweep_po_anchored` — on ERP, the ONLY way a receive is ever seen.

    Walks locally-open POs 50 at a time (plan §2.2), spending exactly one
    adapter call per PO, and reads the receive references off each PO's detail.
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
    #: `None` is honest rather than convenient. It means the caller did not say,
    #: and the store then writes NULL instead of guessing a source that would
    #: make the row LOOK traceable while pointing at the wrong tenant.
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
            # Cost scales with OPEN PO COUNT, not receive volume. This is the
            # binding constraint on ERP Standard's 2,000 calls/day, so the
            # budget is asked before every single PO and a refusal stops the
            # walk exactly where it is.
            if not ctx.charge(1):
                return
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
    """

    adapter: Any
    store: SweepStore
    connection_id: str
    kind: str = "sweep_bill_detail"
    batch_size: int = CHUNK_SIZES["sweep_bill_detail"]

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
                    source_paise=record.total_paise,
                    correlation_id=ctx.correlation_id)
            self.store.mark_detail_hydrated(connection_id=self.connection_id,
                                            external_id=record.external_id)
            yield Progress(
                checkpoint={"last_hydrated": record.external_id,
                            "drained": False},
                units=1,
                detail={"lines": len(record.lines)})


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
    "AdapterMethodMissing", "ControlTotal", "ControlTotalSource",
    "EXCEPTION_KINDS", "EXCEPTION_OPEN", "JOB_CADENCES",
    "KIND_CONTROL_TOTAL_MISMATCH", "KIND_GRN_LINE_UNATTRIBUTED",
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
