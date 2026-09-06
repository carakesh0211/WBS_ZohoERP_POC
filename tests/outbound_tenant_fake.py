"""In-process fakes for the outbound purchase-order tests (Wave 5, stream 6).

Not a test module -- a helper, so it stays out of ``TEST_MANIFEST.json``.

Three fakes, and the split between them is the whole point of the chaos test:

* :class:`FakeTenant` is **Zoho**. It owns the ``cf_capex_ref`` unique index,
  which is row **Z-01** of the §18.5 matrix. ``unique_capex_ref=False``
  constructs the tenant as it exists *before* Z-01 is configured, which is how
  the tests prove Z-01 is load-bearing rather than decorative.
* :class:`FakeAdapter` is **stream 1's C1 adapter**. It maps our payload onto
  the tenant and implements the update-by-custom-field-unique-value retry.
* :class:`InMemoryOutboxStore` is **stream 2's ``integration_outbox``**,
  including the C2 ``UNIQUE (connection_id, module, local_id)`` and the
  ``SENDING`` lease the store protocol requires.

None of them is the code under test. ``outbound.py`` supplies exactly three
things to this arrangement -- a deterministic key, a write-ahead claim, and a
resolve-before-create -- and the tests remove each in turn to show what breaks.

The kill machinery (:class:`KillSwitch`, :class:`FunctionKilled`) simulates a
Catalyst Function being terminated. It fires between named steps rather than at
a random instruction so that a failure names the ordering that produced it.

**No network, no tenant, no Catalyst.** Everything here is a dict.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.backend.integration import outbound as ob


class FunctionKilled(BaseException):
    """The Function was terminated. Deliberately a ``BaseException``.

    If this inherited from ``Exception`` it would be swallowed by
    ``emit_purchase_order``'s ``except Exception`` arm, which would turn every
    kill into an orderly "attempt failed" -- and the test would then prove that
    orderly failure handling works, not that an abrupt death is survivable. A
    real ``SIGKILL`` does not unwind through an exception handler, and neither
    does this.
    """

    def __init__(self, step: str, index: int):
        self.step = step
        self.index = index
        super().__init__(f"Function killed at step {index} ({step})")


class TenantUniqueViolation(Exception):
    """The tenant's ``cf_capex_ref`` unique index refused a second record."""

    def __init__(self, capex_ref: str, external_id: str):
        self.capex_ref = capex_ref
        self.external_id = external_id
        super().__init__(
            f"cf_capex_ref {capex_ref!r} already on {external_id}")


@dataclass
class KillSwitch:
    """Fires ``FunctionKilled`` when the step counter reaches ``kill_at``.

    ``steps`` records every step reached, so a test can assert that the
    interesting one -- the gap between "request sent" and "response recorded"
    -- was actually exercised rather than merely believed to be.
    """
    kill_at: int | None = None
    counter: int = 0
    steps: list[str] = field(default_factory=list)
    armed: bool = True

    def step(self, name: str) -> None:
        self.counter += 1
        self.steps.append(name)
        if self.armed and self.kill_at is not None and self.counter == self.kill_at:
            self.armed = False          # one kill per life, like a real process
            raise FunctionKilled(name, self.counter)


# --------------------------------------------------------------------------
# The tenant: Zoho, and row Z-01 of the responsibility matrix
# --------------------------------------------------------------------------

@dataclass
class FakeTenant:
    """A Zoho organisation holding purchase orders.

    ``unique_capex_ref`` is **Z-01**. With it True the tenant has the unique
    custom field configured and physically cannot hold two purchase orders
    carrying the same ``cf_capex_ref``. With it False it is a tenant where
    nobody created the field, or created it without ticking Unique -- which
    looks identical from the outside until the day a retry duplicates a
    commitment.
    """
    unique_capex_ref: bool = True
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    create_log: list[str] = field(default_factory=list)

    def create(self, payload: Mapping[str, Any]) -> str:
        ref = payload.get(ob.CF_CAPEX_REF)
        if self.unique_capex_ref and ref is not None:
            existing = self.find_by_capex_ref(ref)
            if existing is not None:
                raise TenantUniqueViolation(ref, existing)
        external_id = f"ZPO-{next(self._seq):05d}"
        self.records[external_id] = dict(payload)
        self.create_log.append(external_id)
        return external_id

    def update(self, external_id: str, payload: Mapping[str, Any]) -> None:
        if external_id not in self.records:
            raise KeyError(external_id)
        self.records[external_id].update(payload)

    def find_by_capex_ref(self, ref: str) -> str | None:
        for external_id, record in self.records.items():
            if record.get(ob.CF_CAPEX_REF) == ref:
                return external_id
        return None

    def count_with_capex_ref(self, ref: str) -> int:
        return sum(1 for r in self.records.values()
                   if r.get(ob.CF_CAPEX_REF) == ref)

    def statuses(self) -> dict[str, str]:
        return {eid: r.get("state", "") for eid, r in self.records.items()}


@dataclass(frozen=True)
class FakeCapabilities:
    """The C1 ``Capabilities`` shape. Only the two fields outbound reads matter.

    Defaults are C1's own stated assumptions: ``line_level_custom_fields``
    False (D-7, tenant-verified -- assume False) and the ERP Standard daily
    ceiling of 2,000.
    """
    receives_listable: bool = False
    bills_delta_filter: bool = True
    po_delta_filter: bool = True
    items_delta_filter: bool = False
    line_level_custom_fields: bool = False
    daily_call_ceiling: int = ob.ASSUMED_DAILY_CALL_CEILING


# --------------------------------------------------------------------------
# The adapter: stream 1's C1 implementation
# --------------------------------------------------------------------------

@dataclass
class C1OnlyAdapter:
    """An adapter implementing **exactly** the frozen C1 surface and no more.

    ``create_purchase_order(po, dedupe_key)`` and ``capabilities()`` -- nothing
    else. This is not a stunted fake: it is what stream 1 is obliged to deliver
    by the frozen contract, and running the chaos test against it is how the
    gap in C1 is measured rather than asserted.

    ``names_duplicate=False`` models a tenant that refuses the duplicate
    without saying which record already holds the key, which Zoho's error
    payloads do not reliably do.
    """
    tenant: FakeTenant
    caps: FakeCapabilities = field(default_factory=FakeCapabilities)
    kill: KillSwitch = field(default_factory=KillSwitch)
    names_duplicate: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)

    product = "ERP"

    def capabilities(self) -> FakeCapabilities:
        return self.caps

    def create_purchase_order(self, payload: Mapping[str, Any],
                              dedupe_key: str) -> str:
        self.calls.append(("create", dedupe_key))
        # Everything between these two steps is the window §11.6 is about: the
        # request has left us and the response has not come back.
        self.kill.step("adapter.create.request_sent")
        body = dict(payload)
        body[ob.CF_CAPEX_REF] = dedupe_key
        try:
            external_id = self.tenant.create(body)
        except TenantUniqueViolation as violation:
            self.kill.step("adapter.create.duplicate_refused")
            raise ob.DuplicateDedupeKey(
                dedupe_key,
                violation.external_id if self.names_duplicate else None,
            ) from violation
        self.kill.step("adapter.create.response_received")
        return external_id


@dataclass
class FakeAdapter(C1OnlyAdapter):
    """C1 plus the three extensions ``outbound.py`` declares and reports.

    ``resolve_by_dedupe_key`` is the update-by-custom-field-unique-value half
    of §11.6; ``update_purchase_order`` applies the draft onto an adopted id;
    ``transition_purchase_order`` performs the draft-to-open step of §11.7.
    """

    def resolve_by_dedupe_key(self, dedupe_key: str) -> str | None:
        self.calls.append(("resolve", dedupe_key))
        self.kill.step("adapter.resolve.before")
        found = self.tenant.find_by_capex_ref(dedupe_key)
        self.kill.step("adapter.resolve.after")
        return found

    def update_purchase_order(self, external_id: str,
                              payload: Mapping[str, Any],
                              dedupe_key: str) -> None:
        self.calls.append(("update", dedupe_key))
        self.kill.step("adapter.update.request_sent")
        body = dict(payload)
        body[ob.CF_CAPEX_REF] = dedupe_key
        self.tenant.update(external_id, body)
        self.kill.step("adapter.update.response_received")

    def transition_purchase_order(self, external_id: str, state: str, *,
                                  actor: str) -> None:
        self.calls.append(("transition", state))
        self.tenant.update(external_id, {"state": state,
                                         "transitioned_by": actor})


# --------------------------------------------------------------------------
# The store: stream 2's integration_outbox
# --------------------------------------------------------------------------

class OutboxUniqueViolation(Exception):
    """C2's ``UNIQUE (connection_id, module, local_id)``."""


@dataclass
class InMemoryOutboxStore:
    """``integration_outbox`` in a dict, with C2's uniqueness and the lease.

    Durable in the sense that matters here: it survives a simulated Function
    death, because the "process" is a function call and the store is not
    reconstructed between calls. That is exactly the property PostgreSQL
    provides in production and the reason the outbox exists at all.
    """
    rows: dict[str, ob.OutboxRecord] = field(default_factory=dict)
    kill: KillSwitch = field(default_factory=KillSwitch)
    lease_seconds: int = ob.SENDING_LEASE_SECONDS
    _unique: dict[tuple[str, str, str], str] = field(default_factory=dict)

    # ---------------------------------------------------------------- setup
    def enqueue(self, *, outbox_id: str, connection_id: str, module: str,
                local_id: str, payload: Mapping[str, Any],
                dedupe_key: str | None = None,
                correlation_id: str | None = None) -> ob.OutboxRecord:
        unique = (connection_id, module, local_id)
        if unique in self._unique:
            raise OutboxUniqueViolation(
                f"integration_outbox already holds {unique} as "
                f"{self._unique[unique]!r}")
        key = dedupe_key or ob.derive_dedupe_key(connection_id, module, local_id)
        record = ob.OutboxRecord(
            outbox_id=outbox_id, connection_id=connection_id, module=module,
            local_id=local_id, dedupe_key=key, payload=dict(payload),
            state=ob.OUTBOX_PENDING, attempts=0, correlation_id=correlation_id)
        self.rows[outbox_id] = record
        self._unique[unique] = outbox_id
        return replace(record)

    # ------------------------------------------------------- OutboxStore API
    def claim(self, outbox_id: str, *, now: datetime) -> ob.OutboxRecord | None:
        self.kill.step("store.claim.before")
        row = self.rows.get(outbox_id)
        if row is None or row.state not in ob.CLAIMABLE_STATES:
            return None
        if (row.state == ob.OUTBOX_SENDING and row.next_attempt_at is not None
                and now < row.next_attempt_at):
            return None                      # another worker's lease is live
        row.state = ob.OUTBOX_SENDING
        row.attempts += 1
        row.next_attempt_at = now + timedelta(seconds=self.lease_seconds)
        self.kill.step("store.claim.after")
        return replace(row)

    def record_sent(self, outbox_id: str, external_id: str, *,
                    now: datetime) -> None:
        self.kill.step("store.record_sent.before")
        row = self.rows[outbox_id]
        row.state = ob.OUTBOX_SENT
        row.external_id = external_id
        row.next_attempt_at = None
        row.last_error = None
        self.kill.step("store.record_sent.after")

    def record_attempt_failed(self, outbox_id: str, *, error: str,
                              next_attempt_at: datetime | None,
                              terminal: bool, now: datetime) -> None:
        row = self.rows[outbox_id]
        row.state = ob.OUTBOX_DEAD if terminal else ob.OUTBOX_RETRY
        row.last_error = error
        row.next_attempt_at = next_attempt_at

    def release(self, outbox_id: str, *, state: str, now: datetime) -> None:
        row = self.rows.get(outbox_id)
        if row is not None and row.state != ob.OUTBOX_SENT:
            row.state = state

    def get(self, outbox_id: str) -> ob.OutboxRecord | None:
        row = self.rows.get(outbox_id)
        return replace(row) if row is not None else None


@dataclass
class InMemoryBudget:
    """A :class:`~app.backend.integration.outbound.RateBudget` for tests."""
    minute_allocation: int = ob.OUTBOUND_MINUTE_ALLOCATION
    daily_ceiling: int = ob.ASSUMED_DAILY_CALL_CEILING
    minute_used: int = 0
    day_used: int = 0

    def reserve(self, calls: int) -> ob.BudgetDecision:
        if self.day_used + calls > self.daily_ceiling:
            return ob.BudgetDecision(granted=False, window="DAY",
                                     remaining=self.daily_ceiling - self.day_used,
                                     reason="daily ceiling")
        if self.minute_used + calls > self.minute_allocation:
            return ob.BudgetDecision(
                granted=False, window="MINUTE",
                remaining=self.minute_allocation - self.minute_used,
                reason="minute allocation")
        self.day_used += calls
        self.minute_used += calls
        return ob.BudgetDecision(granted=True, window="DAY")


@dataclass
class StubApprovalGate:
    closed: set[str] = field(default_factory=set)

    def is_closed(self, local_id: str) -> bool:
        return local_id in self.closed


# --------------------------------------------------------------------------
# Convenience builders
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 6, 10, 0, 0, tzinfo=timezone.utc)

CONNECTION_ID = "CONN-ATHA-01"
MODULE = "purchaseorders"


def cell(wbs: str = "WBS-1000", head: str = "BH-PLANT") -> ob.ControlCell:
    return ob.ControlCell(wbs_id=wbs, budget_head_id=head)


def line(line_id: str = "L1", *, wbs: str = "WBS-1000",
         head: str = "BH-PLANT", amount_paise: int = 250_000_00,
         quantity: int = 1) -> ob.PoLine:
    return ob.PoLine(
        line_id=line_id, cell=cell(wbs, head), description=f"line {line_id}",
        quantity=quantity,
        unit_price_paise=amount_paise // quantity,
        amount_paise=amount_paise)


def draft(local_id: str = "PR-0001", *, lines=None) -> ob.PurchaseOrderDraft:
    return ob.PurchaseOrderDraft(
        local_id=local_id, connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=tuple(lines or (line(),)),
        line_level_dimensions=True)


def enqueued(store: InMemoryOutboxStore, *, outbox_id: str = "OB-1",
             local_id: str = "PR-0001",
             po: ob.PurchaseOrderDraft | None = None) -> ob.OutboxRecord:
    """Enqueue one outbox row the way the emission job would.

    The dedupe key is derived at enqueue and written into the payload, so the
    key the tenant will see is fixed before any call is made -- the write-ahead
    half of §11.6.
    """
    po = po or draft(local_id)
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, local_id)
    return store.enqueue(outbox_id=outbox_id, connection_id=CONNECTION_ID,
                         module=MODULE, local_id=local_id,
                         payload=po.as_payload(key), dedupe_key=key)
