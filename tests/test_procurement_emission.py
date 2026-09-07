"""Wave 6 agent 2: a purchase order leaving for Zoho, at most once.

Everything in this file runs on every machine and touches NO database and NO
tenant. That is possible because ``pg.procurement.build_emission_plan`` is
pure: how many purchase orders a requisition becomes, what identity each one
carries and what ``cf_capex_ref`` the tenant will index it under depend on the
purchase-order LINES and on ``capabilities``, and on nothing else. The database
half -- the outbox rows, the circuit, the rate budget -- lives in
``tests/test_pg_procurement.py`` behind ``@pytest.mark.pg``.

**The function under test is the one the router calls.** These tests do not
reconstruct the plan from ``outbound.plan_emission`` themselves; they call
``build_emission_plan`` and feed its own ``(local_id, dedupe_key, payload)``
triples to the in-memory outbox, so a defect in the split, in the key
derivation or in the ``po_line`` -> ``PoLine`` mapping fails here rather than
being reproduced by the test alongside the code.

WHAT THE FAKES ARE, AND WHY THEY ARE NOT THE CODE UNDER TEST
============================================================

``tests/outbound_tenant_fake.py``'s three doubles model Zoho (including the
§18.5 Z-01 unique custom field), stream 1's C1 adapter (which reads the
document BY ATTRIBUTE, so a ``Mapping`` raises) and migration 010's
``integration_outbox`` (including its CHECK constraints). None of them is this
stream's code. What this stream contributes is the plan, the key, and the
statement that N control cells are N purchase orders -- and each test below
removes one of those and shows what breaks.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.integration import throttle  # noqa: E402
from app.backend.pg import procurement as proc  # noqa: E402
from outbound_tenant_fake import (  # noqa: E402
    C1OnlyAdapter, FakeAdapter, FakeCapabilities, FakeTenant, FunctionKilled,
    InMemoryOutboxStore, KillSwitch,
)

NOW = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
NEXT_TICK = timedelta(seconds=60)
DOCUMENT_DATE = date(2026, 9, 7)
CONNECTION_ID = "CONN-ATHA-01"
PO_ID = "PO-ABCDEF012345"
PO_NUMBER = "PO-2026-0001"


def po_line_row(po_line_id: str, *, wbs: str = "WBS-A", head: str = "BH-CIVIL",
                quantity: int = 1, amount_paise: int = 250_000_00,
                rate_paise: int | None = None) -> dict:
    """One ``po_line`` row in the shape ``procurement.po_lines`` returns."""
    return {
        "po_line_id": po_line_id, "line_no": 1, "wbs_id": wbs,
        "budget_head_id": head, "description": f"line {po_line_id}",
        "quantity": quantity,
        "rate_paise": (amount_paise // quantity if rate_paise is None
                       else rate_paise),
        "amount_paise": amount_paise, "tax_paise": 0,
        "non_creditable_tax_paise": 0, "freight_paise": 0,
        "line_external_id": None,
    }


TWO_CELLS = [
    po_line_row("POL-1", wbs="WBS-A", head="BH-CIVIL", amount_paise=250_000_00),
    po_line_row("POL-2", wbs="WBS-B", head="BH-ELEC", amount_paise=150_000_00),
]


def plan_for(line_rows, *, caps=None, acknowledged=True):
    return proc.build_emission_plan(
        po_id=PO_ID, po_number=PO_NUMBER, connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", document_date=DOCUMENT_DATE,
        capabilities=caps or FakeCapabilities(), line_rows=line_rows,
        acknowledged=acknowledged)


def seed_outbox(store: InMemoryOutboxStore, planned) -> list[str]:
    """Write the planned purchase orders into the outbox, as the router does.

    The payload and the key come from the PLAN, never from the test, because
    the key is what the tenant will index the record under and a test that
    minted its own would be proving something about itself.
    """
    ids = []
    for index, entry in enumerate(planned, start=1):
        outbox_id = f"OB-{index}"
        store.enqueue(outbox_id=outbox_id, connection_id=CONNECTION_ID,
                      module=proc.PO_MODULE, local_id=entry["local_id"],
                      payload=entry["payload"], dedupe_key=entry["dedupe_key"])
        ids.append(outbox_id)
    return ids


# ======================================================== the emission shape

def test_a_purchase_order_spanning_two_control_cells_becomes_two_emissions():
    """D-7 False: the dimensions can only sit on the header, so N cells are N
    purchase orders. NOT one order with two cells folded behind one field."""
    plan, planned = plan_for(TWO_CELLS)

    assert plan.line_level_dimensions is False
    assert len(plan.drafts) == 2
    assert len(planned) == 2
    assert [entry["control_cells"] for entry in planned] == [
        [["WBS-A", "BH-CIVIL"]], [["WBS-B", "BH-ELEC"]]]


def test_the_split_is_reported_as_a_procurement_process_change():
    """Three documents for the vendor to acknowledge, receive against and
    invoice is a change to how procurement works, not a technical detail."""
    plan, _planned = plan_for(TWO_CELLS)

    assert plan.process_change is not None
    assert plan.process_change.code == "HEADER_ONLY_PO_SPLIT"
    assert plan.process_change.purchase_orders == 2
    assert plan.process_change.depends_on == "D-7"


def test_an_unacknowledged_process_change_refuses_the_whole_plan():
    """It has to be agreed before emission, not discovered afterwards."""
    with pytest.raises(proc.ProcurementError) as excinfo:
        plan_for(TWO_CELLS, acknowledged=False)

    assert excinfo.value.code == "PROCESS_CHANGE_UNACKNOWLEDGED"
    assert excinfo.value.status == 409
    assert excinfo.value.detail["purchase_orders"] == 2


def test_line_level_custom_fields_keep_one_purchase_order_and_no_process_change():
    """The negative control. If D-7 resolves True the split does not happen,
    and a plan that split anyway would be inventing a process change."""
    plan, planned = plan_for(
        TWO_CELLS, caps=FakeCapabilities(line_level_custom_fields=True))

    assert plan.line_level_dimensions is True
    assert len(plan.drafts) == 1 and len(planned) == 1
    assert plan.process_change is None
    assert planned[0]["control_cells"] == [["WBS-A", "BH-CIVIL"],
                                           ["WBS-B", "BH-ELEC"]]


def test_a_single_cell_purchase_order_is_not_reported_as_a_process_change():
    """One cell is one purchase order under either capability, so nothing
    about procurement has changed and nothing must be raised as though it had."""
    plan, planned = plan_for([TWO_CELLS[0]])

    assert len(planned) == 1
    assert plan.process_change is None


# =============================================================== the dedupe key

def test_each_split_purchase_order_carries_its_own_dedupe_key():
    """Without the per-cell ``local_id`` suffix the split orders would all
    claim one ``cf_capex_ref`` and Z-01 would refuse every one but the first --
    turning a legitimate three-order requisition into one order and two
    permanent duplicate refusals."""
    _plan, planned = plan_for(TWO_CELLS)
    keys = [entry["dedupe_key"] for entry in planned]

    assert len(set(keys)) == 2, keys
    assert planned[0]["local_id"] == f"{PO_ID}#WBS-A#BH-CIVIL"
    assert planned[1]["local_id"] == f"{PO_ID}#WBS-B#BH-ELEC"


def test_the_dedupe_key_is_stable_across_replans():
    """A retry that re-plans must produce the SAME key, or step 3 of the
    emission ("resolve before every create") is asking the tenant about a
    record that cannot exist."""
    first = [entry["dedupe_key"] for entry in plan_for(TWO_CELLS)[1]]
    second = [entry["dedupe_key"] for entry in plan_for(TWO_CELLS)[1]]

    assert first == second


def test_the_dedupe_key_is_written_into_the_payload_the_tenant_will_index():
    _plan, planned = plan_for(TWO_CELLS)

    for entry in planned:
        assert entry["payload"][ob.CF_CAPEX_REF] == entry["dedupe_key"]


# =========================================== what cannot cross the boundary

def test_a_fractional_ordered_quantity_is_refused_rather_than_rounded():
    """``po_line.quantity`` is ``numeric``; ``outbound.PoLine.quantity`` is an
    ``int``. Rounding would send the vendor a quantity the commitment was never
    checked against, so the gap is reported instead."""
    with pytest.raises(proc.ProcurementError) as excinfo:
        plan_for([po_line_row("POL-1", quantity=1)] +
                 [dict(po_line_row("POL-2"), quantity=0.5)])

    assert excinfo.value.code == "NON_INTEGER_QUANTITY"
    assert excinfo.value.status == 422


def test_a_negative_unit_price_is_refused():
    """``ck_po_line_non_negative`` deliberately does not cover ``rate_paise``
    (013 constrains the three columns the SQLite trigger named, and no more),
    so the emission boundary is where a negative unit price is caught."""
    with pytest.raises(proc.ProcurementError) as excinfo:
        plan_for([po_line_row("POL-1", rate_paise=-1)])

    assert excinfo.value.code == "NEGATIVE_RATE"


def test_a_line_amount_travels_as_integer_paise_all_the_way_to_the_document():
    """No float, no Decimal, no string anywhere on the wire path."""
    _plan, planned = plan_for([po_line_row("POL-1", amount_paise=250_000_00)])
    payload = planned[0]["payload"]

    assert payload["total_paise"] == 250_000_00
    assert isinstance(payload["total_paise"], int)
    for line in payload["lines"]:
        assert isinstance(line["amount_paise"], int)
        assert isinstance(line["unit_price_paise"], int)


def test_the_planned_payload_maps_to_the_dto_the_adapter_actually_reads():
    """Both shipped adapters read the document by ATTRIBUTE. A payload that
    cannot become a ``PurchaseOrderEmissionDTO`` fails on the FIRST emission,
    not on a retry."""
    _plan, planned = plan_for([po_line_row("POL-1")])
    entry = planned[0]

    document = ob.emission_dto(
        payload=entry["payload"], connection_id=CONNECTION_ID,
        module=proc.PO_MODULE, local_id=entry["local_id"],
        dedupe_key=entry["dedupe_key"])

    assert document.vendor_external_id == "ZV-77"
    assert document.document_date == DOCUMENT_DATE
    assert document.total_paise == 250_000_00


# ==================================================== at most one, over retries

def test_a_function_killed_after_the_send_creates_no_second_purchase_order():
    """The worst case: the request left us, the response never came back, the
    process died. The recovery must ADOPT the record the tenant already holds
    and must never create a second one."""
    tenant = FakeTenant(unique_capex_ref=True)
    kill = KillSwitch(kill_at=None)
    store = InMemoryOutboxStore(kill=kill)
    _plan, planned = plan_for([po_line_row("POL-1")])
    seed_outbox(store, planned)
    key = planned[0]["dedupe_key"]

    # THE window: the tenant has created the record and we have not recorded
    # it. One kill switch shared by the store and the adapter, because the step
    # counter has to be global to name a point in the whole emission:
    #   1 store.claim.before   2 store.claim.after
    #   3 adapter.resolve.before   4 adapter.resolve.after
    #   5 adapter.create.request_sent   [the tenant creates the record]
    #   6 adapter.create.response_received   <- killed here
    store.kill = KillSwitch(kill_at=6)
    with pytest.raises(FunctionKilled) as killed:
        ob.emit_purchase_order(
            adapter=FakeAdapter(tenant=tenant, kill=store.kill),
            store=store, outbox_id="OB-1", now=NOW)
    store.crash()

    assert killed.value.step == "adapter.create.response_received", (
        "The test must die in the send-to-record window or it proves nothing "
        "about recovery; it died at " + killed.value.step)
    assert store.get("OB-1").state == ob.OUTBOX_PENDING
    assert store.get("OB-1").external_id is None
    assert len(tenant.records) == 1, (
        "The purchase order IS in the tenant; only our record of it is missing.")

    recovered = ob.emit_purchase_order(
        adapter=FakeAdapter(tenant=tenant), store=store, outbox_id="OB-1",
        now=NOW + NEXT_TICK)

    assert recovered.adopted is True and recovered.created is False
    assert recovered.external_id == tenant.find_by_capex_ref(key)
    assert tenant.count_with_capex_ref(key) == 1
    assert len(tenant.records) == 1


def test_a_duplicate_retry_yields_one_logical_purchase_order():
    """A cron tick re-reading a chunk it already sent must cost zero calls and
    must report the settled result, not re-send and rely on Z-01."""
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    adapter = FakeAdapter(tenant=tenant)
    _plan, planned = plan_for([po_line_row("POL-1")])
    seed_outbox(store, planned)

    first = ob.emit_purchase_order(adapter=adapter, store=store,
                                   outbox_id="OB-1", now=NOW)
    calls_after_first = len(adapter.calls)
    second = ob.emit_purchase_order(adapter=adapter, store=store,
                                    outbox_id="OB-1", now=NOW + NEXT_TICK)

    assert second.external_id == first.external_id
    assert second.created is False
    assert len(adapter.calls) == calls_after_first
    assert tenant.count_with_capex_ref(planned[0]["dedupe_key"]) == 1


def test_z01_is_load_bearing_and_its_absence_is_a_duplicate_commitment():
    """The negative control for the test above. With the tenant's unique
    custom field NOT configured, and an adapter that cannot resolve by key
    (the frozen C1 surface), a lost response duplicates the commitment. This
    is what §18.5 Z-01 is buying, measured rather than asserted."""
    tenant = FakeTenant(unique_capex_ref=False)
    store = InMemoryOutboxStore()
    _plan, planned = plan_for([po_line_row("POL-1")])
    seed_outbox(store, planned)
    key = planned[0]["dedupe_key"]

    # The C1-only adapter has no resolve step, so the sequence is shorter:
    #   1 store.claim.before   2 store.claim.after
    #   3 adapter.create.request_sent   [the tenant creates the record]
    #   4 adapter.create.response_received   <- killed here
    kill = KillSwitch(kill_at=4)
    store.kill = kill
    with pytest.raises(FunctionKilled) as killed:
        ob.emit_purchase_order(
            adapter=C1OnlyAdapter(tenant=tenant, kill=kill), store=store,
            outbox_id="OB-1", now=NOW)
    store.crash()
    assert killed.value.step == "adapter.create.response_received"
    store.kill = KillSwitch(kill_at=None)

    ob.emit_purchase_order(adapter=C1OnlyAdapter(tenant=tenant), store=store,
                           outbox_id="OB-1", now=NOW + NEXT_TICK)

    assert tenant.count_with_capex_ref(key) == 2, (
        "Without Z-01 and without resolve_by_dedupe_key there is nothing left "
        "to stop the duplicate. This test exists so removing either is a "
        "failing test rather than a silent regression.")


def test_a_partial_failure_mid_multi_po_emission_leaves_the_sent_one_sent():
    """Two control cells, two purchase orders, and the second one fails.

    The first must stay SENT and must not be re-created on the next tick, and
    the second must be retried on its OWN key. A design that treated the pair
    as one unit of work would either roll back a purchase order the vendor
    already has or send it twice.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    _plan, planned = plan_for(TWO_CELLS)
    outbox_ids = seed_outbox(store, planned)
    assert len(outbox_ids) == 2

    first = ob.emit_purchase_order(adapter=FakeAdapter(tenant=tenant),
                                   store=store, outbox_id="OB-1", now=NOW)

    class _Broken(FakeAdapter):
        def create_purchase_order(self, po, dedupe_key):
            raise RuntimeError("Zoho returned 502")

    with pytest.raises(RuntimeError):
        ob.emit_purchase_order(adapter=_Broken(tenant=tenant), store=store,
                               outbox_id="OB-2", now=NOW)

    assert store.get("OB-1").state == ob.OUTBOX_SENT
    assert store.get("OB-2").state == ob.OUTBOX_FAILED
    assert store.get("OB-2").attempts == 1
    assert len(tenant.records) == 1

    resumed = ob.emit_purchase_order(adapter=FakeAdapter(tenant=tenant),
                                     store=store, outbox_id="OB-2",
                                     now=NOW + timedelta(hours=1))

    assert resumed.created is True
    assert resumed.external_id != first.external_id
    assert len(tenant.records) == 2, (
        "Two control cells are two purchase orders; the retry must complete "
        "the pair, not duplicate the half that succeeded.")
    assert tenant.count_with_capex_ref(planned[0]["dedupe_key"]) == 1
    assert tenant.count_with_capex_ref(planned[1]["dedupe_key"]) == 1


# ================================================== rate limiting: 44, 45, 1070

class _ZohoHttpError(RuntimeError):
    """What a transport would raise. There is no such class in the product.

    ``grep -rn "status_code" app/backend/integration/`` returns nothing: the
    package ships adapters and a throttle and no HTTP client at all, so
    ``throttle.Failure`` is built by whoever calls it.
    ``procurement.failure_from_exception`` therefore reads duck-typed
    attributes, and this is the shape it reads. Recorded here rather than in
    ``adapter.py``, which this stream does not own.
    """

    def __init__(self, status: int, code: int | str | None = None,
                 message: str = ""):
        self.status_code = status
        self.code = code
        super().__init__(message or f"HTTP {status} code={code}")


def _decide(exc: Exception, *, attempts: int = 1) -> throttle.Disposition:
    return throttle.RetryPolicy().decide(
        proc.failure_from_exception(exc), attempts=attempts, now=NOW,
        rng=__import__("random").Random(7))


def test_429_code_44_checkpoints_and_costs_neither_an_attempt_nor_the_circuit():
    """Per-minute. OUR throttle mis-metered; it is not evidence about Zoho."""
    disposition = _decide(_ZohoHttpError(429, throttle.ZOHO_CODE_PER_MINUTE))

    assert disposition.kind is throttle.FailureKind.RATE_LIMIT_MINUTE
    assert disposition.action is throttle.Action.CHECKPOINT_AND_RESUME
    assert disposition.counts_toward_attempts is False
    assert disposition.counts_toward_circuit is False
    assert disposition.retry is True
    assert disposition.resume_at == throttle.window_reset_at(
        throttle.WindowKind.MINUTE, NOW)


def test_429_code_45_opens_the_circuit_to_the_day_boundary_and_alerts():
    """Daily quota. There is no backoff that helps before midnight UTC, and
    letting every other job spend its retries discovering the same thing is
    what the circuit prevents."""
    disposition = _decide(_ZohoHttpError(429, throttle.ZOHO_CODE_DAILY_QUOTA))

    assert disposition.kind is throttle.FailureKind.RATE_LIMIT_DAILY
    assert disposition.action is throttle.Action.OPEN_CIRCUIT_UNTIL_DAY_BOUNDARY
    assert disposition.counts_toward_attempts is False
    assert disposition.counts_toward_circuit is False
    assert disposition.open_circuit_until == throttle.window_reset_at(
        throttle.WindowKind.DAY, NOW)
    assert disposition.alert is not None


def test_429_code_1070_narrows_parallelism_by_one_and_backs_off():
    """Too many at once, not too many in total. Backing off without narrowing
    the pipe just repeats the collision more politely."""
    disposition = _decide(_ZohoHttpError(429, throttle.ZOHO_CODE_CONCURRENCY),
                          attempts=3)

    assert disposition.kind is throttle.FailureKind.CONCURRENCY_LIMIT
    assert disposition.action is throttle.Action.REDUCE_PARALLELISM_AND_RETRY
    assert disposition.parallelism_delta == -1
    assert disposition.counts_toward_attempts is True
    assert disposition.counts_toward_circuit is False
    assert disposition.next_attempt_at is not None


def test_the_three_rate_limit_codes_do_not_collapse_into_one_behaviour():
    """The point of the three tests above, stated as one assertion: folding
    any two together is how a quota exhaustion gets read as an outage."""
    actions = {
        code: _decide(_ZohoHttpError(429, code)).action
        for code in (throttle.ZOHO_CODE_PER_MINUTE,
                     throttle.ZOHO_CODE_DAILY_QUOTA,
                     throttle.ZOHO_CODE_CONCURRENCY)
    }

    assert len(set(actions.values())) == 3, actions


def test_an_unreadable_429_code_is_transient_and_flagged_not_guessed():
    disposition = _decide(_ZohoHttpError(429, 9999))

    assert disposition.kind is throttle.FailureKind.TRANSIENT
    assert disposition.unmapped_code is True
    assert disposition.counts_toward_circuit is True


def test_failure_from_exception_reads_a_timeout_as_a_timeout():
    failure = proc.failure_from_exception(TimeoutError("read timed out"))

    assert failure.timed_out is True
    assert throttle.classify(failure).kind is throttle.FailureKind.TRANSIENT


def test_failure_from_exception_falls_back_to_transient_for_a_bare_exception():
    """An adapter error carrying no status is Zoho being unwell, which retries
    and counts toward the circuit -- the conservative reading of the six."""
    failure = proc.failure_from_exception(RuntimeError("connection reset"))

    assert failure.status is None
    assert throttle.classify(failure).kind is throttle.FailureKind.TRANSIENT


def test_failure_from_exception_refuses_to_classify_a_success_as_a_failure():
    """``throttle.classify`` raises on a 2xx, correctly. A caller handing this
    a 200 has a bug, and reading it as TRANSIENT is the safe answer rather
    than a crash inside the failure handler."""
    failure = proc.failure_from_exception(_ZohoHttpError(200, 0))

    assert failure.status is None
    assert throttle.classify(failure).kind is throttle.FailureKind.TRANSIENT


# ================================================================ the circuit

def test_the_circuit_opens_after_five_consecutive_counted_failures():
    """Five 5xx in the window opens it. Four does not."""
    policy = throttle.RetryPolicy()
    circuit = throttle.Circuit()
    for _ in range(4):
        circuit = throttle.observe_failure(
            circuit, NOW, _decide(_ZohoHttpError(502)), policy=policy)
    assert circuit.state is throttle.CircuitState.CLOSED
    assert circuit.consecutive_failures == 4

    circuit = throttle.observe_failure(
        circuit, NOW, _decide(_ZohoHttpError(502)), policy=policy)

    assert circuit.state is throttle.CircuitState.OPEN
    assert circuit.consecutive_failures == 5


def test_five_rate_limited_calls_do_not_open_the_circuit():
    """The negative control, and the one that matters operationally: a
    per-minute throttle is our arithmetic and a concurrency limit is our
    parallelism. Letting either open the breaker takes the integration down
    over our own bugs, every afternoon, looking like the vendor."""
    policy = throttle.RetryPolicy()
    for code in (throttle.ZOHO_CODE_PER_MINUTE, throttle.ZOHO_CODE_CONCURRENCY):
        circuit = throttle.Circuit()
        for _ in range(5):
            circuit = throttle.observe_failure(
                circuit, NOW, _decide(_ZohoHttpError(429, code)), policy=policy)
        assert circuit.state is throttle.CircuitState.CLOSED, code
        assert circuit.consecutive_failures == 0, code


def test_one_success_resets_the_consecutive_count():
    """"Consecutive" is the operative word: a counter that survived a success
    would open a circuit on five failures spread across a thousand good calls."""
    policy = throttle.RetryPolicy()
    circuit = throttle.Circuit()
    for _ in range(4):
        circuit = throttle.observe_failure(
            circuit, NOW, _decide(_ZohoHttpError(502)), policy=policy)

    circuit = throttle.observe_success(circuit, NOW)

    assert circuit.consecutive_failures == 0
    assert circuit.state is throttle.CircuitState.CLOSED
