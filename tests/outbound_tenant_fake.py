"""In-process fakes for the outbound purchase-order tests (Wave 5, stream 6).

Not a test module -- a helper, so it stays out of ``TEST_MANIFEST.json``.

Three fakes, and the split between them is the whole point of the chaos test:

* :class:`FakeTenant` is **Zoho**. It owns the ``cf_capex_ref`` unique index,
  which is row **Z-01** of the §18.5 matrix. ``unique_capex_ref=False``
  constructs the tenant as it exists *before* Z-01 is configured, which is how
  the tests prove Z-01 is load-bearing rather than decorative.
* :class:`FakeAdapter` is **stream 1's C1 adapter**. It takes the same
  ``PurchaseOrderEmissionDTO`` the shipped adapters take and builds its wire
  body the same way, by attribute (:func:`emission_body`). It used to accept a
  ``Mapping`` and do ``dict(payload)``, which is the single reason the chaos
  test could be green while the first emission through a real adapter raised
  ``AttributeError``.
* :class:`InMemoryOutboxStore` is **stream 2's ``integration_outbox``**,
  including the C2 ``UNIQUE (connection_id, module, local_id)``, the row lock
  ``claim_outbox_batch`` takes, and -- new -- migration 010's CHECK
  constraints (:func:`_check_outbox_row`). It used to accept any string as a
  ``state``, which is how ``SENDING``, ``RETRY`` and ``DEFERRED`` survived a
  whole wave despite ``ck_integration_outbox_state`` permitting none of them.

**Both of those corrections are the same correction.** A double that is more
permissive than the thing it doubles will agree with the module that uses it
about anything, including a call the adapter cannot accept and a row the
database will not hold.

None of them is the code under test. ``outbound.py`` supplies exactly three
things to this arrangement -- a deterministic key, a claim held across the
network call, and a resolve before EVERY create -- and the tests remove each in
turn to show what breaks.

The kill machinery (:class:`KillSwitch`, :class:`FunctionKilled`) simulates a
Catalyst Function being terminated. It fires between named steps rather than at
a random instruction so that a failure names the ordering that produced it.

**No network, no tenant, no Catalyst.** Everything here is a dict.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from app.backend.integration import outbound as ob
from app.backend.integration.dto import PurchaseOrderEmissionDTO


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
        record = dict(payload)
        # The tenant's OWN default state, set by the tenant. Nothing we send
        # names a status -- both shipped adapters deliberately omit one, and
        # §11.7 is why: a purchase order is created in Zoho's draft state and
        # is moved to open only by our own later call, after our approval
        # instance closes. This used to arrive in the payload dict we handed
        # over, which quietly made the test assert that WE set the status.
        record.setdefault("state", ob.PO_STATE_DRAFT)
        self.records[external_id] = record
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

def emission_body(po: Any, dedupe_key: str) -> dict[str, Any]:
    """The wire body, built by ATTRIBUTE access -- exactly as the real ones do.

    This mirrors ``erp._emission_body`` and ``books_inventory._emission_body``
    field for field, and that mirroring is the point of this function existing
    at all. The fakes used to take a ``Mapping`` and do ``dict(payload)``, so
    they agreed with the caller rather than with the two adapters that ship --
    which is exactly why the chaos test could pass while the FIRST emission
    through a real adapter raised ``AttributeError`` on
    ``po.vendor_external_id``.

    A double that is more permissive than the thing it doubles does not test
    the seam, it hides it. So this one refuses a ``Mapping`` outright rather
    than merely failing to find attributes on it, because ``Mapping`` is the
    specific wrong shape that was being passed and a plain ``AttributeError``
    would be easy to misread as a missing field.
    """
    if isinstance(po, Mapping):
        raise TypeError(
            "create_purchase_order/update_purchase_order take a "
            "PurchaseOrderEmissionDTO, not a Mapping. Both shipped adapters "
            "read this document by attribute (po.vendor_external_id, "
            "po.document_date.isoformat(), po.currency_code, po.lines), so a "
            "dict raises AttributeError on the first field and the emission "
            "fails before any retry question arises. Map the outbox payload "
            "with outbound.emission_dto_from_record().")
    return {
        "vendor_id": po.vendor_external_id,
        "date": po.document_date.isoformat(),
        "currency_code": po.currency_code,
        "line_items": [_outbound_line(line) for line in po.lines],
        # The tenant fake indexes its unique constraint on this key directly,
        # where the real tenant reads it out of `custom_fields`. Both are
        # carried so the fake stays honest about the wire shape while the
        # Z-01 index stays cheap to model.
        "custom_fields": [{"api_name": ob.CF_CAPEX_REF, "value": dedupe_key}],
        ob.CF_CAPEX_REF: dedupe_key,
    }


def _outbound_line(line: Any) -> dict[str, Any]:
    """One emitted line. Integer division, never a float -- as the real ones do."""
    rupees, sub = divmod(line.unit_price_paise, 100)
    return {
        "item_id": line.item_external_id,
        "description": line.description,
        "quantity": line.quantity,
        "rate": f"{rupees}.{sub:02d}",
        # Not on the real wire body (Zoho computes the line total from rate x
        # quantity). Carried here so a test can assert the LINE TOTAL survived
        # the mapping, which is the field a `amount_paise -> unit_price_paise`
        # slip would silently multiply.
        "line_total_paise": line.line_total_paise,
        "line_number": line.line_number,
    }


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

    def create_purchase_order(self, po: PurchaseOrderEmissionDTO,
                              dedupe_key: str) -> str:
        self.calls.append(("create", dedupe_key))
        # Everything between these two steps is the window §11.6 is about: the
        # request has left us and the response has not come back.
        self.kill.step("adapter.create.request_sent")
        body = emission_body(po, dedupe_key)
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
                              po: PurchaseOrderEmissionDTO,
                              dedupe_key: str) -> None:
        self.calls.append(("update", dedupe_key))
        self.kill.step("adapter.update.request_sent")
        # The SAME builder as the create, for the reason both shipped adapters
        # give: two builders would let an adopted purchase order end up holding
        # different values from the one we believed we sent.
        body = emission_body(po, dedupe_key)
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


class OutboxCheckViolation(Exception):
    """A CHECK constraint on ``integration_outbox`` refused the row.

    **This class is why three invented states survived a whole wave.** The
    fake used to accept any string in ``state``, so ``SENDING``, ``RETRY`` and
    ``DEFERRED`` passed every test in this suite while PostgreSQL would have
    rejected all three on the first write. A double that is more permissive
    than the table it doubles proves the code agrees with itself.

    The constraints modelled here are the ones from migration 010 that the
    emission path can actually violate, named as the migration names them.
    """


def _check_outbox_row(row: ob.OutboxRecord) -> None:
    """``ck_integration_outbox_*``, in Python, on every write."""
    if row.state not in ob.OUTBOX_STATES:
        raise OutboxCheckViolation(
            f"ck_integration_outbox_state: {row.state!r} is not one of "
            f"{list(ob.OUTBOX_STATES)}. The C16 `outbox` namespace, the "
            "table's CHECK and integration_store.OUTBOX_STATES all agree on "
            "these four; anything else is a row that never persists.")
    # Both biconditionals, both directions. SENT without an external id is a
    # claim we cannot substantiate; an external id on any other state is a
    # document we created and then recorded as not sent.
    if (row.state == ob.OUTBOX_SENT) != (row.external_id is not None):
        raise OutboxCheckViolation(
            "ck_integration_outbox_sent_carries_external_id: state="
            f"{row.state!r} with external_id={row.external_id!r}.")
    if (row.state == ob.OUTBOX_SENT) != (row.sent_at is not None):
        raise OutboxCheckViolation(
            "ck_integration_outbox_sent_at: state="
            f"{row.state!r} with sent_at={row.sent_at!r}.")
    if row.state == ob.OUTBOX_FAILED and row.next_attempt_at is None:
        raise OutboxCheckViolation(
            "ck_integration_outbox_failed_reschedules: a retryable failure "
            "that scheduled no retry is not retryable; it is a row that stops "
            "moving and never appears on any queue.")
    if row.state == ob.OUTBOX_DEAD and row.attempts < row.max_attempts:
        raise OutboxCheckViolation(
            "ck_integration_outbox_dead_exhausted_attempts: DEAD with "
            f"attempts={row.attempts} < max_attempts={row.max_attempts}.")
    if not (0 <= row.attempts <= row.max_attempts):
        raise OutboxCheckViolation(
            f"ck_integration_outbox_attempts: attempts={row.attempts}, "
            f"max_attempts={row.max_attempts}.")


@dataclass
class InMemoryOutboxStore:
    """``integration_outbox`` in a dict, with C2's uniqueness and 010's CHECKs.

    Durable in the sense that matters here: it survives a simulated Function
    death, because the "process" is a function call and the store is not
    reconstructed between calls. That is exactly the property PostgreSQL
    provides in production and the reason the outbox exists at all.

    Two changes from the version that let three invented states through:

    * every write goes through :func:`_check_outbox_row`, so this fake now
      refuses exactly what ``migrations/pg/010_integration.sql`` refuses;
    * exclusion is a **lock**, not a lease. ``locked`` models
      ``SELECT ... FOR UPDATE OF o SKIP LOCKED``: a second claim on a held row
      returns ``None``, and :meth:`crash` drops every lock the way a dying
      Function's connection does. There is no ``SENDING`` state and no
      ``lease_seconds``, because the database provides both properties without
      either.
    """
    rows: dict[str, ob.OutboxRecord] = field(default_factory=dict)
    kill: KillSwitch = field(default_factory=KillSwitch)
    #: Outbox ids currently locked by a live claim, i.e. the rows another
    #: worker's `FOR UPDATE ... SKIP LOCKED` would skip.
    locked: set[str] = field(default_factory=set)
    _unique: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def crash(self) -> None:
        """The claiming process died: PostgreSQL drops its locks with it.

        The property the lease design did NOT have. A Function killed while
        holding a 900-second lease left the row unclaimable for fifteen
        one-minute cron ticks; a Function killed while holding a row lock
        blocks nobody, because the lock lived in a connection that no longer
        exists.
        """
        self.locked.clear()

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
        _check_outbox_row(record)
        self.rows[outbox_id] = record
        self._unique[unique] = outbox_id
        return replace(record)

    # ------------------------------------------------------- OutboxStore API
    def claim(self, outbox_id: str, *, now: datetime) -> ob.OutboxRecord | None:
        """`SELECT ... FOR UPDATE OF o SKIP LOCKED`, modelled.

        No state change and no lease. ``attempts`` is NOT incremented here --
        the canonical ``integration_store.mark_outbox_failed`` increments it,
        once, in the same statement that decides FAILED-or-DEAD, so that a
        Function killed between the increment and the decision cannot leave a
        row retrying for ever one attempt short of DEAD.
        """
        self.kill.step("store.claim.before")
        row = self.rows.get(outbox_id)
        if row is None or row.state not in ob.CLAIMABLE_STATES:
            return None
        if outbox_id in self.locked:
            return None                     # another worker holds the row lock
        if row.next_attempt_at is not None and now < row.next_attempt_at:
            return None                     # backing off; not due yet
        self.locked.add(outbox_id)
        self.kill.step("store.claim.after")
        return replace(row)

    def record_sent(self, outbox_id: str, external_id: str, *,
                    now: datetime) -> None:
        self.kill.step("store.record_sent.before")
        row = self.rows[outbox_id]
        candidate = replace(row, state=ob.OUTBOX_SENT, external_id=external_id,
                            sent_at=now, next_attempt_at=None, last_error=None)
        _check_outbox_row(candidate)
        self.rows[outbox_id] = candidate
        self.locked.discard(outbox_id)
        self.kill.step("store.record_sent.after")

    def record_attempt_failed(self, outbox_id: str, *, error: str,
                              next_attempt_at: datetime | None,
                              terminal: bool, now: datetime) -> None:
        """FAILED or DEAD -- the two the table permits, and no third.

        ``terminal`` burns the remaining attempts rather than merely setting
        the state. ``ck_integration_outbox_dead_exhausted_attempts`` says DEAD
        implies ``attempts >= max_attempts``, so a non-retryable failure at
        attempt one cannot be written as DEAD while claiming it still has seven
        attempts left. Burning them is the honest reading: there are no
        attempts remaining, because retrying is forbidden.
        """
        row = self.rows[outbox_id]
        attempts = min(row.attempts + 1, row.max_attempts)
        if terminal:
            candidate = replace(row, state=ob.OUTBOX_DEAD,
                                attempts=row.max_attempts, last_error=error,
                                next_attempt_at=None)
        else:
            candidate = replace(row, state=ob.OUTBOX_FAILED, attempts=attempts,
                                last_error=error,
                                next_attempt_at=next_attempt_at)
        _check_outbox_row(candidate)
        self.rows[outbox_id] = candidate
        self.locked.discard(outbox_id)

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

    def reserve(self, calls: int, *,
                now: datetime | None = None) -> ob.BudgetDecision:
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


#: The document date every builder here uses. A CONSTANT, never `date.today()`:
#: a draft whose date moved between the enqueue and the retry would have its
#: date rewritten by the recovery update, and the commitment would change
#: period without anyone choosing that. The chaos test replays the same
#: emission across simulated days, so a clock-derived default would make the
#: adopted record differ from the created one and the test would be asserting
#: the wrong thing.
DOCUMENT_DATE = date(2026, 9, 6)


def draft(local_id: str = "PR-0001", *, lines=None,
          document_date: date = DOCUMENT_DATE) -> ob.PurchaseOrderDraft:
    return ob.PurchaseOrderDraft(
        local_id=local_id, connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=tuple(lines or (line(),)),
        document_date=document_date, line_level_dimensions=True)


def emission_document(local_id: str = "PR-0001", *,
                      dedupe_key: str | None = None,
                      po: ob.PurchaseOrderDraft | None = None
                      ) -> PurchaseOrderEmissionDTO:
    """The DTO an adapter may be handed, built through the real mapping.

    Deliberately routed through ``draft().as_payload()`` and
    ``outbound.emission_dto`` rather than constructed directly, so a test that
    drives an adapter by hand exercises the SAME mapping
    ``emit_purchase_order`` uses. A hand-built DTO here would let the mapper
    rot while these tests kept passing -- which is the shape of the defect this
    stream was sent to fix.
    """
    po = po or draft(local_id)
    key = dedupe_key or ob.derive_dedupe_key(CONNECTION_ID, MODULE, local_id)
    return ob.emission_dto(payload=po.as_payload(key),
                           connection_id=CONNECTION_ID, module=MODULE,
                           local_id=local_id, dedupe_key=key)


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
