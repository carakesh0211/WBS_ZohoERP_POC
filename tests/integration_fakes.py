"""In-process fakes for the Wave 5 job framework and sweeps.

**No network, no Zoho tenant, no Catalyst, no PostgreSQL.** Phase 0B has not
cleared, the target product is provisional, and every property the job suites
assert is a property of *our* code, not of a vendor's. So the adapter is a
scripted fake, the store is a dictionary, and time is injected.

The clock is the important one. The contract under test is a **twelve-minute
soft deadline** (80% of the Catalyst Function ceiling), and a test that proves
it by waiting twelve minutes is a test nobody runs. Here the fake adapter
advances the fake clock by a configured number of seconds per call — work
takes time, which is exactly why a job runs out of it — so a run that
checkpoints at the deadline does so for the real reason, in milliseconds.

`InMemoryStore` implements both `jobs.JobStore` and `sweeps.SweepStore`. Where
the real store's behaviour is guaranteed by a database constraint, the fake
implements the same guarantee in Python and the docstring names the constraint:

* ``upsert_inbox`` → ``UNIQUE (connection_id, module, external_id,
  payload_sha)`` (C2), which is what makes the 300-second poll overlap free;
* ``raise_exception`` → idempotent on ``(kind, object_type, object_id)`` while
  an Open row exists, which is what makes a re-run from a checkpoint produce
  the same state rather than a pile of duplicate exceptions;
* ``claim_job`` → ``FOR UPDATE SKIP LOCKED`` plus the lease, so a second tick
  overlapping the first claims nothing rather than doubling the work.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app.backend.integration import jobs, sweeps

T0 = datetime(2026, 9, 6, 1, 0, 0, tzinfo=timezone.utc)

#: ERP, as `Capabilities` declares it: receives are NOT listable (so
#: PO-anchored discovery is the sole GRN mechanism), bills and POs have a
#: delta filter, items do not, and the daily ceiling is ERP Standard's 2,000.
ERP = jobs.Capabilities(
    receives_listable=False, bills_delta_filter=True, po_delta_filter=True,
    items_delta_filter=False, line_level_custom_fields=False,
    daily_call_ceiling=2000)

#: Books + Inventory: receives ARE listable, and Inventory POs have neither a
#: filter nor a sort — so `po_delta_filter=False` selects a full re-pull.
BOOKS_INVENTORY = jobs.Capabilities(
    receives_listable=True, bills_delta_filter=True, po_delta_filter=False,
    items_delta_filter=True, line_level_custom_fields=False,
    daily_call_ceiling=10000)


class FakeClock:
    """A clock that only moves when something takes time."""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class CountingBudget:
    """A rate budget with a hard ceiling. Refuses rather than raising.

    Stands in for stream 4's `throttle.py`. On ERP Standard the binding window
    is the daily 2,000 calls, so this one counts calls and stops — which is the
    behaviour a job must survive by checkpointing, not by failing.
    """

    def __init__(self, ceiling: int) -> None:
        self.ceiling = ceiling
        self.used = 0
        self.refusals = 0

    def consume(self, *, connection_id: str | None, calls: int,
                now: datetime) -> bool:
        if self.used + calls > self.ceiling:
            self.refusals += 1
            return False
        self.used += calls
        return True


# --------------------------------------------------------------- source data

@dataclass(frozen=True)
class FakeLine:
    """One receive or bill line, in the shape the ADAPTERS EMIT.

    The field names are `LineDTO`'s, deliberately. This docstring used to say
    "in the shape `sweeps.normalise` reads", and the fields were named to match
    what the reader looked for -- so every sweeps test asserted that the reader
    reads what the reader expects, and the join between the adapters' output
    and the sweeps' input was never tested at all.

    It hid a real defect: the reader searched for `line_item_id` and
    `amount_paise`, `LineDTO` carries `purchase_order_line_external_id` and
    `line_total_paise`, and against a real line the attribution resolved to
    `None` and `0` -- every receive line quarantined, at zero value.

    A double may be PARTIAL. It may not invent a name the real object lacks.
    `tests/test_integration_dto_reader_contract.py` enforces that.
    """

    purchase_order_line_external_id: str | None
    line_total_paise: int
    quantity: int = 1


@dataclass(frozen=True)
class FakeRecord:
    """A source document. `raw` is what the inbox preserves verbatim."""

    external_id: str
    raw: dict[str, Any]
    last_modified_time: datetime | None = None
    document_number: str | None = None
    document_date: date | None = None
    status: str | None = None
    total_paise: int | None = None
    entity_id: str | None = None
    project_id: str | None = None
    cf_capex_ref: str | None = None
    #: Both names are `dto.BillDTO`'s, not the reader's. `None` means the
    #: double does not state them, and `sweeps.normalise` then falls back to
    #: the base currency and to "no rate" exactly as it does for a real DTO.
    currency_code: str | None = None
    exchange_rate: str | None = None
    lines: tuple[FakeLine, ...] = ()


@dataclass(frozen=True)
class FakePage:
    """A `Page[...]`-alike: the records and whether another page follows."""

    records: tuple[Any, ...]
    has_more: bool


def bill(n: int, *, modified: datetime, total_paise: int = 100_000,
         with_lines: bool = False, entity_id: str = "ENT-1",
         project_id: str = "PRJ-1", number: str | None = None,
         currency_code: str | None = None,
         exchange_rate: str | None = None) -> FakeRecord:
    external_id = f"BILL-EXT-{n:04d}"
    return FakeRecord(
        external_id=external_id,
        raw={"bill_id": external_id, "last_modified_time": modified.isoformat(),
             "total": total_paise, "status": "open"},
        last_modified_time=modified,
        document_number=number if number is not None else f"BILL-{n:04d}",
        document_date=modified.date(), status="open", total_paise=total_paise,
        entity_id=entity_id, project_id=project_id,
        currency_code=currency_code, exchange_rate=exchange_rate,
        lines=(FakeLine(purchase_order_line_external_id=f"BL-{n}", line_total_paise=total_paise),)
        if with_lines else ())


def purchase_order(n: int, *, modified: datetime, capex_ref: str | None = None,
                   total_paise: int = 500_000) -> FakeRecord:
    external_id = f"PO-EXT-{n:04d}"
    return FakeRecord(
        external_id=external_id,
        raw={"purchaseorder_id": external_id,
             "last_modified_time": modified.isoformat()},
        last_modified_time=modified, document_number=f"PO-{n:04d}",
        document_date=modified.date(), status="open", total_paise=total_paise,
        entity_id="ENT-1", project_id="PRJ-1", cf_capex_ref=capex_ref)


def receive(n: int, *, lines: Iterable[FakeLine]) -> FakeRecord:
    external_id = f"GRN-EXT-{n:04d}"
    return FakeRecord(
        external_id=external_id,
        raw={"receive_id": external_id, "n": n},
        document_number=f"GRN-{n:04d}", status="received",
        lines=tuple(lines))


class FakeAdapter:
    """A scripted `ProcurementPort`. Every call costs simulated time.

    `seconds_per_call` is what makes the deadline tests real: a job runs out of
    the twelve minutes because its work consumed them, not because a test
    reached in and moved the hands.
    """

    def __init__(self, clock: FakeClock, *,
                 capabilities: jobs.Capabilities = ERP,
                 bill_pages: list[FakePage] | None = None,
                 po_pages: list[FakePage] | None = None,
                 item_pages: list[FakePage] | None = None,
                 contact_pages: list[FakePage] | None = None,
                 receives: Mapping[str, list[FakeRecord]] | None = None,
                 bill_details: Mapping[str, FakeRecord] | None = None,
                 seconds_per_call: float = 1.0) -> None:
        self.clock = clock
        self._capabilities = capabilities
        self.bill_pages = bill_pages or []
        self.po_pages = po_pages or []
        self.item_pages = item_pages or []
        self.contact_pages = contact_pages or []
        self.receives = dict(receives or {})
        self.bill_details = dict(bill_details or {})
        self.seconds_per_call = seconds_per_call
        #: every call, in order, for cost assertions
        self.calls: list[tuple[str, Any]] = []

    def capabilities(self) -> jobs.Capabilities:
        return self._capabilities

    # -------------------------------------------------------------- helpers
    def _spend(self, name: str, detail: Any) -> None:
        self.calls.append((name, detail))
        self.clock.advance(self.seconds_per_call)

    @staticmethod
    def _page(pages: list[FakePage], page: int) -> FakePage:
        index = page - 1
        if 0 <= index < len(pages):
            return pages[index]
        return FakePage(records=(), has_more=False)

    # ---------------------------------------------------------------- C1
    def list_bills(self, since: datetime | None, until: datetime,
                   page: int) -> FakePage:
        self._spend("list_bills", {"since": since, "until": until, "page": page})
        return self._page(self.bill_pages, page)

    def list_purchase_orders(self, since: datetime | None, until: datetime,
                             page: int) -> FakePage:
        self._spend("list_purchase_orders",
                    {"since": since, "until": until, "page": page})
        return self._page(self.po_pages, page)

    def list_items(self, since: datetime | None, until: datetime,
                   page: int) -> FakePage:
        self._spend("list_items", {"since": since, "until": until, "page": page})
        return self._page(self.item_pages, page)

    def list_contacts(self, since: datetime | None, until: datetime,
                      page: int) -> FakePage:
        self._spend("list_contacts",
                    {"since": since, "until": until, "page": page})
        return self._page(self.contact_pages, page)

    def get_bill(self, external_id: str) -> FakeRecord:
        self._spend("get_bill", external_id)
        return self.bill_details[external_id]

    def receives_for_po(self, po_external_id: str) -> list[FakeRecord]:
        self._spend("receives_for_po", po_external_id)
        return list(self.receives.get(po_external_id, []))


class ReceivesNotListable:
    """An ERP adapter, faithfully: asking to list receives is an error.

    Purchase Receives on ERP has create, update, delete and fetch-one and no
    list endpoint at all. A fake that quietly returned an empty list would let
    a caller that wrongly assumed enumeration pass its tests.
    """

    def list_purchase_receives(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "Zoho ERP Purchase Receives has NO list endpoint (plan §11.4). "
            "Enumeration is not available; PO-anchored discovery is the only "
            "acquisition path.")


class FakeControlTotals:
    """Source-side totals, scripted per (module, period_id, status)."""

    def __init__(self, totals: Mapping[tuple[str, str, str], sweeps.ControlTotal]) -> None:
        self.totals = dict(totals)
        self.asked: list[tuple[str, str, str]] = []

    def source_control_total(self, *, module: str, period: sweeps.PeriodRef,
                             status: str) -> sweeps.ControlTotal:
        key = (module, period.period_id, status)
        self.asked.append(key)
        return self.totals.get(key, sweeps.ControlTotal(0, 0))


# --------------------------------------------------------------- the store

@dataclass
class JobRow:
    job_id: str
    kind: str
    state: str = jobs.JOB_PENDING
    checkpoint: dict[str, Any] = field(default_factory=dict)
    resume_count: int = 0
    correlation_id: str | None = None
    connection_id: str | None = None
    locked_until: datetime | None = None
    soft_deadline_at: datetime | None = None
    note: str | None = None
    created_at: datetime = T0


class InMemoryStore:
    """`jobs.JobStore` + `sweeps.SweepStore`, in dictionaries."""

    def __init__(self, *, connection_id: str = "CONN-1") -> None:
        self.connection_id = connection_id
        self.job_rows: dict[str, JobRow] = {}
        self.inbox: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        #: Every `mirror_bill` call, so a test can assert the sweep
        #: reached the LEDGER and not merely the inbox.
        self.mirrored_bills: list[dict[str, Any]] = []
        self.inbox_writes = 0
        self.watermarks: dict[tuple[str, str], datetime] = {}
        self.exceptions: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.receive_lines: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        #: §11.10's provenance, kept beside `receive_lines` rather than inside
        #: it so `snapshot()` keeps its shape. See `record_receive_line`.
        self.receive_line_provenance: dict[
            tuple[str, str, str | None], dict[str, Any]] = {}
        self.unattributed_contributions: dict[tuple[str, str], int] = {}
        self.open_pos: list[sweeps.LocalPurchaseOrder] = []
        self.po_lines: dict[tuple[str, str], str] = {}
        self.known_pos: set[str] = set()
        self.detail_queue: list[str] = []
        self.hydrated: list[str] = []
        self.periods: list[sweeps.PeriodRef] = []
        self.slices: dict[str, list[tuple[str, str]]] = {}
        self.local_totals: dict[tuple[str, str, str], sweeps.ControlTotal] = {}
        self.documents: dict[tuple[str, str], list[sweeps.LocalDocument]] = {}
        self.checkpoint_writes: list[tuple[str, str, dict[str, Any]]] = []
        self._ids = itertools.count(1)

    # ------------------------------------------------------------- job rows
    def enqueue(self, kind: str, *, checkpoint: Mapping[str, Any] | None = None,
                correlation_id: str | None = None,
                state: str = jobs.JOB_PENDING) -> JobRow:
        row = JobRow(job_id=f"JOB-{next(self._ids)}", kind=kind, state=state,
                     checkpoint=dict(checkpoint or {}),
                     correlation_id=correlation_id,
                     connection_id=self.connection_id)
        self.job_rows[row.job_id] = row
        return row

    def claim_job(self, *, kind: str, now: datetime, lease_seconds: int,
                  soft_deadline_at: datetime,
                  limit: int = 1) -> jobs.ClaimedJob | None:
        """`FOR UPDATE SKIP LOCKED LIMIT n`, in Python.

        A row whose lease has not expired is SKIPPED, never waited for: a
        second tick that blocked on the first would spend its whole fifteen
        minutes queueing.
        """
        for row in sorted(self.job_rows.values(), key=lambda r: r.created_at):
            if row.kind != kind or row.state not in jobs.CLAIMABLE_STATES:
                continue
            if row.locked_until is not None and row.locked_until > now:
                continue                      # SKIP LOCKED
            row.state = jobs.JOB_CLAIMED
            row.locked_until = now + timedelta(seconds=lease_seconds)
            row.soft_deadline_at = soft_deadline_at
            return jobs.ClaimedJob(
                job_id=row.job_id, kind=row.kind, checkpoint=dict(row.checkpoint),
                resume_count=row.resume_count,
                correlation_id=row.correlation_id,
                connection_id=row.connection_id)
        return None

    def save_checkpoint(self, job_id: str, *, checkpoint: Mapping[str, Any],
                        state: str, resume_count: int, now: datetime) -> None:
        jobs.assert_json_safe(checkpoint, where=f"fake save_checkpoint({job_id})")
        row = self.job_rows[job_id]
        row.checkpoint = dict(checkpoint)
        row.state = state
        row.resume_count = resume_count
        if state != jobs.JOB_CLAIMED:
            # The invocation is over: release the lease so the next cron tick
            # can claim the row. Only a crash keeps its lease to expiry.
            row.locked_until = None
        self.checkpoint_writes.append((job_id, state, dict(checkpoint)))

    def finish_job(self, job_id: str, *, state: str,
                   checkpoint: Mapping[str, Any], now: datetime,
                   note: str | None = None) -> None:
        row = self.job_rows[job_id]
        row.checkpoint = dict(checkpoint)
        row.state = state
        row.note = note
        row.locked_until = None

    def record_event(self, *, connection_id: str | None,
                     correlation_id: str | None, kind: str,
                     detail: Mapping[str, Any]) -> None:
        self.events.append({"connection_id": connection_id,
                            "correlation_id": correlation_id, "kind": kind,
                            "detail": dict(detail)})

    # ---------------------------------------------------------------- inbox
    def upsert_inbox(self, *, connection_id: str, module: str,
                     external_id: str, payload_sha: str,
                     payload: Mapping[str, Any],
                     external_status_raw: str | None, received_at: datetime,
                     correlation_id: str | None) -> bool:
        """C2's ``UNIQUE (connection_id, module, external_id, payload_sha)``.

        Returns True only on a genuine insert. The duplicate a 300-second poll
        overlap produces is DISCARDED, which is what makes the overlap free.
        """
        self.inbox_writes += 1
        key = (connection_id, module, external_id, payload_sha)
        if key in self.inbox:
            return False
        self.inbox[key] = {
            "payload": dict(payload),
            "external_status_raw": external_status_raw,
            "received_at": received_at, "correlation_id": correlation_id,
            "state": "RECEIVED"}
        return True

    # ----------------------------------------------------------- watermarks
    def get_watermark(self, *, connection_id: str,
                      module: str) -> datetime | None:
        return self.watermarks.get((connection_id, module))

    def set_watermark(self, *, connection_id: str, module: str,
                      hwm: datetime, now: datetime) -> None:
        self.watermarks[(connection_id, module)] = hwm

    # ------------------------------------------------------- reconciliation
    def raise_exception(self, *, kind: str, object_type: str,
                        object_id: str | None, detail: str,
                        raised_at: datetime, entity_id: str | None = None,
                        project_id: str | None = None,
                        local_paise: int | None = None,
                        source_paise: int | None = None,
                        correlation_id: str | None = None) -> str:
        """Idempotent while an Open row exists for the same key.

        Re-running a sweep from its checkpoint must produce the same state, not
        a second copy of every exception it already raised.
        """
        assert kind in sweeps.EXCEPTION_KINDS, (
            f"{kind} is not one of the kinds C18 declares")
        key = (kind, object_type, object_id)
        existing = self.exceptions.get(key)
        if existing is not None and existing["status"] == sweeps.EXCEPTION_OPEN:
            return existing["exception_id"]
        exception_id = f"EXC-{next(self._ids)}"
        self.exceptions[key] = {
            "exception_id": exception_id, "kind": kind,
            "object_type": object_type, "object_id": object_id,
            "detail": detail, "raised_at": raised_at, "entity_id": entity_id,
            "project_id": project_id, "local_paise": local_paise,
            "source_paise": source_paise, "correlation_id": correlation_id,
            "status": sweeps.EXCEPTION_OPEN}
        return exception_id

    def open_exceptions(self, *, entity_id: str | None = None,
                        project_id: str | None = None) -> list[dict[str, Any]]:
        return [
            row for row in self.exceptions.values()
            if row["status"] == sweeps.EXCEPTION_OPEN
            and (entity_id is None or row["entity_id"] == entity_id)
            and (project_id is None or row["project_id"] == project_id)
        ]

    # ------------------------------------------------------- PO-anchored
    def open_purchase_orders(self, *, connection_id: str,
                             after_po_id: str | None,
                             limit: int) -> list[sweeps.LocalPurchaseOrder]:
        ordered = sorted(self.open_pos, key=lambda p: p.po_id)
        if after_po_id is not None:
            ordered = [p for p in ordered if p.po_id > after_po_id]
        return ordered[:limit]

    def known_purchase_order(self, *, connection_id: str,
                             external_id: str) -> bool:
        return external_id in self.known_pos

    def resolve_po_line(self, *, po_external_id: str,
                        line_external_id: str | None) -> str | None:
        if line_external_id is None:
            return None
        return self.po_lines.get((po_external_id, line_external_id))

    def record_receive_line(self, *, po_line_id: str, receive_external_id: str,
                            line_external_id: str | None, quantity: Any,
                            amount_paise: int | None,
                            external_source: str | None = None,
                            receive_number: str | None = None,
                            received_at: Any = None,
                            external_last_modified: Any = None,
                            payload_sha: str | None = None) -> None:
        """The five that are the contract, plus §11.10's provenance.

        The provenance is kept in a SEPARATE dictionary rather than merged into
        `receive_lines`, deliberately. `snapshot()` renders `receive_lines`
        verbatim, so folding five more keys into it would change the shape of
        every snapshot in the suite -- and a double whose observable shape moves
        when the production code gains a field is a double that reports drift
        it did not cause.
        """
        key = (po_line_id, receive_external_id, line_external_id)
        self.receive_lines[key] = {
            "quantity": quantity, "amount_paise": amount_paise}
        self.receive_line_provenance[key] = {
            "external_source": external_source,
            "receive_number": receive_number,
            "received_at": received_at,
            "external_last_modified": external_last_modified,
            "payload_sha": payload_sha,
        }

    def accumulate_unattributed(self, *, project_id: str | None, paise: int,
                                source_key: str) -> None:
        """Keyed by the exception id, so a re-walk cannot inflate the bucket."""
        self.unattributed_contributions[(project_id or "UNKNOWN", source_key)] = paise

    @property
    def unattributed_paise(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for (project_id, _key), paise in self.unattributed_contributions.items():
            totals[project_id] = totals.get(project_id, 0) + paise
        return totals

    # ---------------------------------------------------------- bill detail
    def bills_awaiting_detail(self, *, connection_id: str,
                              limit: int) -> list[str]:
        return self.detail_queue[:limit]

    def mark_detail_hydrated(self, *, connection_id: str,
                             external_id: str) -> None:
        if external_id in self.detail_queue:
            self.detail_queue.remove(external_id)
        self.hydrated.append(external_id)

    def mirror_bill(self, **kwargs: Any) -> dict[str, Any]:
        """The verb the bill-detail sweep gained when the ledger was wired in.

        WHY THIS EXISTS, AND WHY ITS ABSENCE WAS A REAL FAILURE
        -------------------------------------------------------
        `procurement.mirror_bill` was written with NO PRODUCTION CALLER: the
        bill-detail sweep fetched the document, wrote the inbox row, marked it
        hydrated and stopped, so `bill` and `bill_line` were never written and
        `billed_paise` was structurally 0 for the whole estate. Wiring the
        sweep to the ledger is what fixed it -- and this double, which had no
        `mirror_bill`, then made the sweep refuse.

        That refusal was CORRECT and the test that caught it was right: a
        store which cannot record the bill must not let the queue be drained
        by the step that failed. So this is the double catching up with a real
        contract, not a test being relaxed to accommodate one.

        Records the call and returns the real function's summary shape, with
        the line counts derived from what it was actually handed rather than
        hard-coded -- a fake that always answers "0 quarantined" would make
        the quarantine assertions pass without exercising anything.
        """
        self.mirrored_bills.append(dict(kwargs))
        lines = tuple(kwargs.get("lines") or ())
        attributed = sum(
            1 for line in lines
            if getattr(line, "purchase_order_line_external_id", None)
            or (isinstance(line, dict)
                and line.get("purchase_order_line_external_id")))
        return {
            "bill_id": f"BILL-{kwargs.get('external_id', 'UNKNOWN')}",
            "attributed": attributed,
            "quarantined": len(lines) - attributed,
            "quarantined_paise": 0,
        }


    # -------------------------------------------------------- completeness
    def periods_to_reconcile(self, *,
                             connection_id: str) -> list[sweeps.PeriodRef]:
        return list(self.periods)

    def control_total_slices(self, *, connection_id: str,
                             period: sweeps.PeriodRef) -> list[tuple[str, str]]:
        return list(self.slices.get(period.period_id, []))

    def local_control_total(self, *, module: str, period: sweeps.PeriodRef,
                            status: str) -> sweeps.ControlTotal:
        return self.local_totals.get((module, period.period_id, status),
                                     sweeps.ControlTotal(0, 0))

    def local_documents(self, *, module: str,
                        period: sweeps.PeriodRef) -> list[sweeps.LocalDocument]:
        return list(self.documents.get((module, period.period_id), []))

    # ------------------------------------------------------------ snapshots
    def snapshot(self) -> dict[str, Any]:
        """Everything a re-run must reproduce exactly, in comparable form."""
        return {
            "inbox": {"|".join(k): v["payload"] for k, v in sorted(self.inbox.items())},
            "watermarks": {"|".join(k): jobs.iso(v)
                           for k, v in sorted(self.watermarks.items())},
            "exceptions": {
                "|".join(str(part) for part in k): {
                    "kind": v["kind"], "object_id": v["object_id"],
                    "source_paise": v["source_paise"],
                    "local_paise": v["local_paise"], "status": v["status"]}
                for k, v in sorted(self.exceptions.items(),
                                   key=lambda item: str(item[0]))},
            "receive_lines": {
                "|".join(str(part) for part in k): v
                for k, v in sorted(self.receive_lines.items(),
                                   key=lambda item: str(item[0]))},
            "unattributed_paise": dict(sorted(self.unattributed_paise.items())),
            "hydrated": sorted(self.hydrated),
        }


def pages_of(records: list[FakeRecord], *, page_size: int) -> list[FakePage]:
    """`records` split into `FakePage`s, the last one flagged as the last."""
    chunks = [records[i:i + page_size]
              for i in range(0, len(records), page_size)] or [[]]
    return [FakePage(records=tuple(chunk), has_more=index < len(chunks) - 1)
            for index, chunk in enumerate(chunks)]
