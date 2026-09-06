"""The eight polls and sweeps, and the three facts that shape them.

1. On Zoho ERP, `sweep_po_anchored` is **the sole GRN acquisition mechanism**:
   Purchase Receives has no list endpoint, so receives cannot be enumerated and
   are found only by walking locally-open POs. Cost therefore scales with
   open-PO count, which on ERP Standard's 2,000 calls/day is the binding
   constraint.
2. `last_modified_time` is filterable but **not sortable**, so there is no
   stable resumable keyset walk: every poll uses a bounded window with a
   300-second overlap and the window boundary is the checkpoint.
3. The completeness sweeps are **not** made redundant by a working delta
   filter. A filter cannot prove it returned everything.

And the rule that outranks them: every unattributed receive line is
quarantined at full value. **Zero pro-rata spreading, zero silent drops.**

Every test drives the real jobs through the real runner, with fakes and an
injected clock. No network, no tenant, no database.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.integration import jobs, sweeps
from integration_fakes import (
    BOOKS_INVENTORY,
    ERP,
    T0,
    CountingBudget,
    FakeAdapter,
    FakeClock,
    FakeControlTotals,
    FakeLine,
    FakePage,
    FakeRecord,
    InMemoryStore,
    ReceivesNotListable,
    bill,
    pages_of,
    purchase_order,
    receive,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "research" / "30_contracts"


def _run(job, store, clock, *, capabilities=ERP, **kwargs):
    store.enqueue(job.kind)
    return jobs.run_job(job, store=store, clock=clock,
                        capabilities=capabilities, **kwargs)


# =============================================== fact 1: PO-anchored discovery

def _po_estate(clock, *, pos=3, receives_each=1, resolvable=True,
               capabilities=ERP):
    store = InMemoryStore()
    receives: dict[str, list[FakeRecord]] = {}
    for n in range(pos):
        external = f"PO-EXT-{n:04d}"
        store.open_pos.append(sweeps.LocalPurchaseOrder(
            po_id=f"PO-{n:04d}", external_id=external, entity_id="ENT-1",
            project_id="PRJ-1"))
        made = []
        for r in range(receives_each):
            line = FakeLine(purchase_order_line_external_id=f"POL-{n}", line_total_paise=250_000)
            made.append(receive(n * 10 + r, lines=[line]))
            if resolvable:
                store.po_lines[(external, f"POL-{n}")] = f"POLINE-{n}"
        receives[external] = made
    adapter = FakeAdapter(clock, capabilities=capabilities, receives=receives,
                          seconds_per_call=1)
    job = sweeps.SweepPoAnchored(adapter, store, store.connection_id)
    return store, adapter, job


def test_po_anchored_discovery_is_the_sole_grn_mechanism_when_receives_cannot_be_listed():
    """ERP declares `receives_listable=False`, and that is not a detail.

    There is no list endpoint, no delta feed and no full-scan path for GRNs on
    ERP. Anything this walk does not reach is not acquired late -- it is not
    acquired at all. The sweep records which of the two it was, because a
    sweep falling behind means something very different in each case.
    """
    clock = FakeClock()
    store, _, job = _po_estate(clock)
    run = _run(job, store, clock, capabilities=ERP)
    assert job.sole_grn_mechanism(ERP) is True
    assert run.checkpoint["sole_grn_mechanism"] is True

    clock2 = FakeClock()
    store2, _, job2 = _po_estate(clock2, capabilities=BOOKS_INVENTORY)
    run2 = _run(job2, store2, clock2, capabilities=BOOKS_INVENTORY)
    assert job2.sole_grn_mechanism(BOOKS_INVENTORY) is False
    assert run2.checkpoint["sole_grn_mechanism"] is False


def test_the_sweep_never_tries_to_enumerate_receives():
    """The failure this guards: a caller assuming a list endpoint exists.

    `ReceivesNotListable` raises if anything asks it to enumerate, so a sweep
    that reached for a list call fails loudly here rather than in a tenant.
    """
    clock = FakeClock()
    store, adapter, job = _po_estate(clock)
    _run(job, store, clock)
    assert {name for name, _ in adapter.calls} == {"receives_for_po"}
    with pytest.raises(AssertionError):
        ReceivesNotListable().list_purchase_receives()


def test_the_cost_scales_with_open_po_count_and_not_with_receive_volume():
    """One call per open PO, whether that PO has one receive or twenty.

    This is why the open-PO population is the binding constraint on ERP
    Standard's 2,000 calls/day, and why Phase 0B-3 has to size it against the
    client's real data rather than against receive throughput.
    """
    clock = FakeClock()
    store, adapter, job = _po_estate(clock, pos=4, receives_each=5)
    run = _run(job, store, clock)
    assert run.calls == 4
    assert len(adapter.calls) == 4
    assert len(store.inbox) == 20, "20 receives were still acquired"


def test_the_walk_stops_at_the_rate_budget_instead_of_breaching_the_daily_ceiling():
    """§11.6: over budget -> checkpoint and resume on the next tick.

    Breaching the daily ceiling on ERP Standard costs the rest of the day, so
    the budget is asked before each PO and a refusal stops the walk where it
    stands -- reported as a pause, never as a completion.
    """
    clock = FakeClock()
    store, _, job = _po_estate(clock, pos=6)
    run = _run(job, store, clock, budget=CountingBudget(ceiling=2))
    assert run.state == jobs.JOB_CHECKPOINTED
    assert run.reason == jobs.STOP_RATE_BUDGET
    assert run.checkpoint["last_po_id_swept"] == "PO-0001"
    assert run.calls == 2


def test_the_walk_cycles_so_a_deleted_receive_can_still_be_detected():
    """A deleted receive arrives as an absence: only a RE-READ of the PO finds
    it. A walk that terminated at the last PO would never look again."""
    clock = FakeClock()
    store, _, job = _po_estate(clock, pos=2)
    first = _run(job, store, clock)
    assert first.checkpoint["last_po_id_swept"] == "PO-0001"

    store.job_rows[first.job_id].state = jobs.JOB_CHECKPOINTED
    second = jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert second.checkpoint == {"last_po_id_swept": None, "cycle": 1,
                                 "sole_grn_mechanism": True}


# ================================================= §11.8, the rule that wins

def test_an_unattributed_receive_line_is_quarantined_at_full_value():
    """Zero pro-rata spreading, zero silent drops.

    One PO, two receive lines: one resolves to a known `po_line`, one does not.
    The resolvable one is recorded at its own value; the other is quarantined
    at ITS OWN full value and accumulates in the project bucket. Nothing is
    apportioned between them, and nothing is dropped.
    """
    clock = FakeClock()
    store = InMemoryStore()
    store.open_pos.append(sweeps.LocalPurchaseOrder(
        po_id="PO-1", external_id="PO-EXT-1", entity_id="ENT-1",
        project_id="PRJ-1"))
    store.po_lines[("PO-EXT-1", "GOOD")] = "POLINE-GOOD"
    adapter = FakeAdapter(clock, receives={"PO-EXT-1": [receive(
        1, lines=[FakeLine(purchase_order_line_external_id="GOOD", line_total_paise=300_000),
                  FakeLine(purchase_order_line_external_id="ORPHAN", line_total_paise=700_000)])]},
        seconds_per_call=1)
    job = sweeps.SweepPoAnchored(adapter, store, store.connection_id)
    _run(job, store, clock)

    quarantined = [e for e in store.exceptions.values()
                   if e["kind"] == sweeps.KIND_GRN_LINE_UNATTRIBUTED]
    assert len(quarantined) == 1
    assert quarantined[0]["source_paise"] == 700_000
    assert quarantined[0]["project_id"] == "PRJ-1"
    assert store.unattributed_paise == {"PRJ-1": 700_000}

    recorded = list(store.receive_lines.values())
    assert len(recorded) == 1, "the orphan line is never recorded as attributed"
    assert recorded[0]["amount_paise"] == 300_000, "no pro-rata spreading"


def test_a_receive_line_with_no_line_identifier_is_quarantined_never_guessed():
    """Documented is not populated. Until Phase 0B-3 proves the linkage in the
    client's own tenant, a line with no identifier is the expected case, and
    guessing which `po_line` it belonged to would fabricate an attribution."""
    clock = FakeClock()
    store = InMemoryStore()
    store.open_pos.append(sweeps.LocalPurchaseOrder(
        po_id="PO-1", external_id="PO-EXT-1", entity_id="ENT-1",
        project_id="PRJ-1"))
    store.po_lines[("PO-EXT-1", "POL-1")] = "POLINE-1"   # the only candidate
    adapter = FakeAdapter(clock, receives={"PO-EXT-1": [receive(
        1, lines=[FakeLine(purchase_order_line_external_id=None, line_total_paise=123_456)])]},
        seconds_per_call=1)
    _run(sweeps.SweepPoAnchored(adapter, store, store.connection_id), store,
         clock)

    assert store.receive_lines == {}
    assert store.unattributed_paise == {"PRJ-1": 123_456}


def test_re_walking_the_same_pos_does_not_inflate_the_unattributed_bucket():
    """The walk cycles and the window overlaps, so re-reads are the norm.

    A bucket that added on every pass would climb every fifteen minutes with
    no new receive at all, and the number that blocks capitalisation would be
    fiction.
    """
    clock = FakeClock()
    store, _, job = _po_estate(clock, pos=2, resolvable=False)
    _run(job, store, clock)
    first = dict(store.unattributed_paise)
    snapshot = store.snapshot()

    store.job_rows[list(store.job_rows)[0]].state = jobs.JOB_CHECKPOINTED
    store.job_rows[list(store.job_rows)[0]].checkpoint = {}
    jobs.run_job(job, store=store, clock=clock, capabilities=ERP)

    assert store.unattributed_paise == first
    assert store.snapshot() == snapshot


def test_an_open_exception_blocks_the_period_close():
    """'A period cannot close while an Open reconciliation exception exists' --
    the gate `approve_capitalisation` already applies, extended to these.

    The rows are shaped exactly as `pg/periods.py` queries for them: entity_id
    plus status 'Open'. The two layers read the same rows, so they cannot
    disagree about which exceptions block.
    """
    clock = FakeClock()
    store, _, job = _po_estate(clock, pos=1, resolvable=False)
    assert sweeps.period_close_blockers(store, entity_id="ENT-1") == []

    _run(job, store, clock)
    blockers = sweeps.period_close_blockers(store, entity_id="ENT-1")
    assert blockers and sweeps.KIND_GRN_LINE_UNATTRIBUTED in blockers[0]
    assert all(row["status"] == sweeps.EXCEPTION_OPEN
               for row in store.open_exceptions(entity_id="ENT-1"))
    assert sweeps.period_close_blockers(store, entity_id="ENT-OTHER") == []


def test_the_unattributed_bucket_blocks_capitalisation_at_any_value():
    """No materiality threshold: a threshold is a silent drop with a number
    on it, and §11.8 forbids the drop."""
    store = InMemoryStore()
    assert sweeps.capitalisation_blockers(store, project_id="PRJ-1") == []
    assert sweeps.capitalisation_blockers(store, project_id="PRJ-1",
                                          unattributed_paise=1) != []


def test_every_kind_a_sweep_can_raise_is_one_the_registry_declares():
    """Kinds come from C18's frozen five. A sweep that needs a sixth reports
    it; it does not invent one."""
    registry = json.loads(
        (CONTRACTS / "C18_domain_statuses.json").read_text(encoding="utf-8"))
    declared = set(registry["namespaces"]["exception_status"]["kinds"])
    assert sweeps.EXCEPTION_KINDS == declared
    assert sweeps.EXCEPTION_OPEN in {
        s["code"] for s in
        registry["namespaces"]["exception_status"]["statuses"]}


# ================================ fact 2: windowed polling with a 300s overlap

def _bill_estate(clock, *, records=6, page_size=10, capabilities=ERP,
                 hwm=None, seconds_per_call=1.0):
    store = InMemoryStore()
    if hwm is not None:
        store.watermarks[(store.connection_id, sweeps.MODULE_BILLS)] = hwm
    modified = T0 - timedelta(minutes=10)
    adapter = FakeAdapter(
        clock, capabilities=capabilities,
        bill_pages=pages_of([bill(n, modified=modified) for n in range(records)],
                            page_size=page_size),
        seconds_per_call=seconds_per_call)
    job = sweeps.poll_bills(adapter, store, store.connection_id,
                            page_size=page_size)
    return store, adapter, job


def test_the_poll_re_reads_three_hundred_seconds_behind_the_watermark():
    """There is no stable keyset on modification time, so the window is
    re-entered behind its own boundary rather than resumed exactly."""
    clock = FakeClock()
    hwm = T0 - timedelta(hours=1)
    store, adapter, job = _bill_estate(clock, hwm=hwm)
    _run(job, store, clock)
    _, first_call = adapter.calls[0]
    assert first_call["since"] == hwm - timedelta(seconds=300)
    # Bounded, and clamped at `now`: a window that ran into the future would
    # advance the watermark past records that have not been written yet.
    assert first_call["until"] == T0
    assert first_call["until"] <= hwm + timedelta(seconds=jobs.DEFAULT_WINDOW_SECONDS)


def test_the_overlap_is_free_because_the_inbox_discards_the_duplicate():
    """C2's UNIQUE (connection_id, module, external_id, payload_sha).

    Two runs over the same six bills: twelve write attempts, six rows. If the
    dedupe were not there the overlap would double every record on every run
    and the 'free' in 'free overlap' would quietly stop being true.
    """
    clock = FakeClock()
    store, _, job = _bill_estate(clock, records=6, hwm=T0 - timedelta(hours=1))
    _run(job, store, clock)
    after_first = len(store.inbox)

    store.enqueue("poll_bills")
    jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert len(store.inbox) == after_first == 6
    assert store.inbox_writes == 12


def test_the_window_boundary_is_the_checkpoint_and_the_watermark_follows_it():
    clock = FakeClock()
    hwm = T0 - timedelta(hours=1)
    store, _, job = _bill_estate(clock, hwm=hwm)
    run = _run(job, store, clock)
    assert run.state == jobs.JOB_DONE
    moved = store.watermarks[(store.connection_id, sweeps.MODULE_BILLS)]
    assert moved == T0, "the window's own boundary, never further"
    assert moved > hwm
    assert run.checkpoint["hwm"] == jobs.iso(moved)


def test_a_page_limit_pauses_the_walk_without_moving_the_watermark():
    """<= 40 pages per invocation (§2.2). The window stays open, the watermark
    stays put, and the next tick resumes at the page it stopped on."""
    clock = FakeClock()
    hwm = T0 - timedelta(hours=1)
    store, _, job = _bill_estate(clock, records=30, page_size=1, hwm=hwm)
    job.max_pages = 4
    run = _run(job, store, clock)
    assert run.state == jobs.JOB_CHECKPOINTED
    assert run.reason == "PAGE_LIMIT"
    assert run.checkpoint["page"] == 5
    assert store.watermarks[(store.connection_id, sweeps.MODULE_BILLS)] == hwm


def test_the_strategy_is_read_from_capabilities_never_from_a_product_name():
    """`po_delta_filter=False` -- Inventory POs have neither filter nor sort --
    selects a full re-pull, and `bills_delta_filter=True` a window. The same
    code, two products, no product name anywhere in the decision."""
    clock = FakeClock()
    store = InMemoryStore()
    adapter = FakeAdapter(clock, capabilities=BOOKS_INVENTORY,
                          po_pages=[FakePage(records=(), has_more=False)],
                          seconds_per_call=1)
    store.watermarks[(store.connection_id, sweeps.MODULE_PURCHASE_ORDERS)] = \
        T0 - timedelta(hours=1)
    job = sweeps.poll_purchaseorders(adapter, store, store.connection_id)
    run = _run(job, store, clock, capabilities=BOOKS_INVENTORY)
    assert run.checkpoint["hwm"], "a full re-pull still advances the watermark"
    assert adapter.calls[0][1]["since"] is None

    clock2 = FakeClock()
    store2, adapter2, job2 = _bill_estate(clock2, hwm=T0 - timedelta(hours=1),
                                          capabilities=BOOKS_INVENTORY)
    _run(job2, store2, clock2, capabilities=BOOKS_INVENTORY)
    assert adapter2.calls[0][1]["since"] is not None


def test_items_follow_the_same_declared_capability_split():
    """Inventory: a true delta. ERP/Books: a weekly full refresh."""
    for capabilities, expect_window in ((BOOKS_INVENTORY, True), (ERP, False)):
        clock = FakeClock()
        store = InMemoryStore()
        store.watermarks[(store.connection_id, sweeps.MODULE_ITEMS)] = \
            T0 - timedelta(hours=1)
        adapter = FakeAdapter(clock, capabilities=capabilities,
                              item_pages=[FakePage(records=(), has_more=False)],
                              seconds_per_call=1)
        job = sweeps.poll_items(adapter, store, store.connection_id)
        _run(job, store, clock, capabilities=capabilities)
        assert (adapter.calls[0][1]["since"] is not None) is expect_window


def test_contacts_fall_back_to_a_full_refresh_because_no_filter_exists():
    """`contacts` is sortable but not filterable on all three products, which
    is the opposite problem to bills. There is no `contacts_delta_filter` in
    `Capabilities`, so the honest answer is the full refresh the plan
    prescribes -- not a window nobody promised."""
    clock = FakeClock()
    store = InMemoryStore()
    store.watermarks[(store.connection_id, sweeps.MODULE_CONTACTS)] = \
        T0 - timedelta(hours=1)
    adapter = FakeAdapter(clock, contact_pages=[FakePage((), False)],
                          seconds_per_call=1)
    job = sweeps.poll_contacts(adapter, store, store.connection_id)
    _run(job, store, clock)
    assert adapter.calls[0][1]["since"] is None
    assert not hasattr(ERP, "contacts_delta_filter")


def test_a_record_with_no_external_id_is_refused_rather_than_dropped():
    with pytest.raises(sweeps.SweepError):
        sweeps.normalise({"total": 1}, module="bills")


def test_an_unknown_page_shape_assumes_another_page_rather_than_stopping_early():
    """Guessing that a full page is the last one loses every record on the
    page after it, silently."""
    assert sweeps.page_has_more({"records": [1, 2]}, returned=2, page_size=2)
    assert not sweeps.page_has_more({"records": [1]}, returned=1, page_size=2)
    assert not sweeps.page_has_more(FakePage((1,), False), returned=1, page_size=1)


def test_a_missing_adapter_method_names_the_seam_instead_of_returning_nothing():
    """A poll that fetched nothing must not look like a poll that found
    nothing."""
    clock = FakeClock()
    store = InMemoryStore()

    class _BillsOnly:
        def list_bills(self, since, until, page):
            return FakePage((), False)

    job = sweeps.poll_items(_BillsOnly(), store, store.connection_id)
    run = _run(job, store, clock, capabilities=BOOKS_INVENTORY)
    assert run.state == jobs.JOB_FAILED
    assert "AdapterMethodMissing" in (run.error or "")


# ============================================ §11.7, unsanctioned commitments

def test_a_capex_po_with_no_local_record_is_an_unsanctioned_commitment():
    """The detective control that is on regardless of D-8.

    A PO created directly in Zoho bypasses `budget_check`. Prevention is a
    tenant role configuration we do not control; detection is this, and it
    blocks period close. Even if prevention fails the number is never silently
    wrong -- it is loudly wrong, which is the acceptable outcome.
    """
    clock = FakeClock()
    store = InMemoryStore()
    store.known_pos.add("PO-EXT-0001")
    adapter = FakeAdapter(
        clock,
        po_pages=[FakePage((purchase_order(1, modified=T0, capex_ref="WBS-1"),
                            purchase_order(2, modified=T0, capex_ref="WBS-2"),
                            purchase_order(3, modified=T0)), False)],
        seconds_per_call=1)
    job = sweeps.poll_purchaseorders(adapter, store, store.connection_id)
    _run(job, store, clock)

    raised = [e for e in store.exceptions.values()
              if e["kind"] == sweeps.KIND_UNSANCTIONED_COMMITMENT]
    assert [e["object_id"] for e in raised] == ["PO-EXT-0002"], (
        "PO-0001 is known to us; PO-0003 carries no CAPEX dimension")
    assert raised[0]["source_paise"] == 500_000
    assert sweeps.period_close_blockers(store, entity_id="ENT-1")


# ================================================ sweep_bill_detail (§11.5)

def test_bill_detail_hydration_fetches_the_lines_the_list_response_omits():
    clock = FakeClock()
    store = InMemoryStore()
    store.detail_queue.extend(["BILL-EXT-0001", "BILL-EXT-0002"])
    details = {f"BILL-EXT-{n:04d}": bill(n, modified=T0, with_lines=True)
               for n in (1, 2)}
    adapter = FakeAdapter(clock, bill_details=details, seconds_per_call=1)
    run = _run(sweeps.SweepBillDetail(adapter, store, store.connection_id),
               store, clock)
    assert run.state == jobs.JOB_DONE
    assert store.hydrated == ["BILL-EXT-0001", "BILL-EXT-0002"]
    assert store.detail_queue == []
    assert run.calls == 2


def test_a_detail_fetch_that_returns_no_lines_is_reported_not_marked_done():
    """Hydration that returned no lines is not hydration. The difference is a
    bill nobody can attribute versus a bill everybody believes was
    attributed."""
    clock = FakeClock()
    store = InMemoryStore()
    store.detail_queue.append("BILL-EXT-0001")
    adapter = FakeAdapter(
        clock, bill_details={"BILL-EXT-0001": bill(1, modified=T0)},
        seconds_per_call=1)
    _run(sweeps.SweepBillDetail(adapter, store, store.connection_id), store,
         clock)
    kinds = {e["kind"] for e in store.exceptions.values()}
    assert sweeps.KIND_CONTROL_TOTAL_MISMATCH in kinds


def test_an_empty_detail_queue_is_a_completion_not_a_failure():
    clock = FakeClock()
    store = InMemoryStore()
    run = _run(sweeps.SweepBillDetail(FakeAdapter(clock), store,
                                      store.connection_id), store, clock)
    assert run.state == jobs.JOB_DONE and run.calls == 0


# ================================== fact 3: the completeness sweeps stay

JAN = sweeps.PeriodRef("2026-01", "ENT-1", date(2026, 1, 1), date(2026, 1, 31),
                       state="CLOSED")
FEB = sweeps.PeriodRef("2026-02", "ENT-1", date(2026, 2, 1), date(2026, 2, 28))


def test_control_totals_catch_what_a_working_delta_filter_cannot_prove():
    """The filter works. It still cannot prove it returned everything.

    Source says 10 bills for 1,000,000 paise; we hold 9 for 900,000. Nothing
    in the poll failed, no error was logged, and the difference is only
    visible to an independent count -- which is this sweep, and is why it is
    retained in full alongside a delta filter that works.
    """
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [FEB]
    store.slices = {"2026-02": [(sweeps.MODULE_BILLS, "Approved")]}
    store.local_totals[(sweeps.MODULE_BILLS, "2026-02", "Approved")] = \
        sweeps.ControlTotal(9, 900_000)
    source = FakeControlTotals(
        {(sweeps.MODULE_BILLS, "2026-02", "Approved"):
         sweeps.ControlTotal(10, 1_000_000)})

    assert ERP.bills_delta_filter is True
    run = _run(sweeps.SweepControlTotals(source, store, store.connection_id),
               store, clock)
    assert run.state == jobs.JOB_DONE
    mismatch = [e for e in store.exceptions.values()
                if e["kind"] == sweeps.KIND_CONTROL_TOTAL_MISMATCH]
    assert len(mismatch) == 1
    assert mismatch[0]["local_paise"] == 900_000
    assert mismatch[0]["source_paise"] == 1_000_000
    assert "10 documents" in mismatch[0]["detail"]


def test_matching_totals_raise_nothing():
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [FEB]
    store.slices = {"2026-02": [(sweeps.MODULE_BILLS, "Approved")]}
    store.local_totals[(sweeps.MODULE_BILLS, "2026-02", "Approved")] = \
        sweeps.ControlTotal(10, 1_000_000)
    source = FakeControlTotals(
        {(sweeps.MODULE_BILLS, "2026-02", "Approved"):
         sweeps.ControlTotal(10, 1_000_000)})
    _run(sweeps.SweepControlTotals(source, store, store.connection_id), store,
         clock)
    assert store.exceptions == {}


def test_only_one_period_is_reconciled_per_invocation():
    """§2.2's chunk for this job. Two periods take two nightly ticks, and the
    first run reports a pause rather than a completion it did not achieve."""
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [JAN, FEB]
    store.slices = {"2026-01": [(sweeps.MODULE_BILLS, "Approved")],
                    "2026-02": [(sweeps.MODULE_BILLS, "Approved")]}
    source = FakeControlTotals({})
    job = sweeps.SweepControlTotals(source, store, store.connection_id)
    first = _run(job, store, clock)
    assert first.state == jobs.JOB_CHECKPOINTED
    assert first.reason == "ONE_PERIOD_PER_INVOCATION"
    assert first.checkpoint["periods_done"] == ["2026-01"]

    second = jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert second.checkpoint["periods_done"] == ["2026-01", "2026-02"]
    assert {p for p, _ in [(k[1], k) for k in source.asked]} == {"2026-01",
                                                                 "2026-02"}


def test_document_number_gaps_are_detected_per_series():
    gaps = sweeps.document_number_gaps(
        ["BILL-0001", "BILL-0002", "BILL-0005", "PO-0007", "PO-0008"])
    assert gaps == {"BILL-": ["BILL-0003", "BILL-0004"]}


def test_gap_detection_does_not_cry_wolf():
    """An over-eager detector is switched off within a week, and then detects
    nothing at all. One document is not a series; an unparseable number is not
    a gap."""
    assert sweeps.document_number_gaps(["BILL-0001"]) == {}
    assert sweeps.document_number_gaps([None, "", "DRAFT"]) == {}
    assert sweeps.document_number_gaps(["A-1", "A-2", "A-3"]) == {}


def test_a_gap_in_the_series_is_raised_against_the_period():
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [FEB]
    store.documents[(sweeps.MODULE_BILLS, "2026-02")] = [
        sweeps.LocalDocument("B1", "BILL-0001", date(2026, 2, 3)),
        sweeps.LocalDocument("B3", "BILL-0003", date(2026, 2, 5)),
    ]
    job = sweeps.SweepCompleteness(store, store.connection_id,
                                   modules=(sweeps.MODULE_BILLS,))
    _run(job, store, clock)
    detail = list(store.exceptions.values())[0]["detail"]
    assert "BILL-0002" in detail
    assert "cannot prove it returned everything" in detail


def test_a_document_dated_into_a_closed_period_raises_late_arrival():
    """The number in the closed period's report is now wrong, and somebody has
    to decide what to do about it. It is never quietly posted into the open
    period instead."""
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [JAN]
    store.documents[(sweeps.MODULE_BILLS, "2026-01")] = [
        sweeps.LocalDocument("B9", "BILL-0009", date(2026, 1, 20),
                             received_at=datetime(2026, 2, 14, tzinfo=timezone.utc),
                             entity_id="ENT-1", project_id="PRJ-1",
                             total_paise=450_000),
    ]
    job = sweeps.SweepCompleteness(store, store.connection_id,
                                   modules=(sweeps.MODULE_BILLS,))
    _run(job, store, clock)
    late = [e for e in store.exceptions.values()
            if e["kind"] == sweeps.KIND_LATE_ARRIVAL_CLOSED_PERIOD]
    assert len(late) == 1 and late[0]["object_id"] == "B9"
    assert late[0]["source_paise"] == 450_000
    assert sweeps.period_close_blockers(store, entity_id="ENT-1")


def test_a_document_in_an_open_period_is_not_a_late_arrival():
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [FEB]
    store.documents[(sweeps.MODULE_BILLS, "2026-02")] = [
        sweeps.LocalDocument("B9", "BILL-0009", date(2026, 2, 20),
                             received_at=datetime(2026, 2, 21, tzinfo=timezone.utc)),
    ]
    _run(sweeps.SweepCompleteness(store, store.connection_id,
                                  modules=(sweeps.MODULE_BILLS,)), store, clock)
    assert store.exceptions == {}


def test_the_completeness_sweep_is_idempotent_across_a_resume():
    clock = FakeClock()
    store = InMemoryStore()
    store.periods = [JAN]
    store.documents[(sweeps.MODULE_BILLS, "2026-01")] = [
        sweeps.LocalDocument("B1", "BILL-0001", date(2026, 1, 3),
                             received_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                             entity_id="ENT-1"),
        sweeps.LocalDocument("B3", "BILL-0003", date(2026, 1, 5),
                             received_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                             entity_id="ENT-1"),
    ]
    job = sweeps.SweepCompleteness(store, store.connection_id,
                                   modules=(sweeps.MODULE_BILLS,))
    _run(job, store, clock)
    once = store.snapshot()

    row = store.job_rows[list(store.job_rows)[0]]
    row.state, row.checkpoint = jobs.JOB_CHECKPOINTED, {}
    jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert store.snapshot() == once


# ======================================================== the eight jobs

def test_all_eight_jobs_are_declared_with_the_cadence_the_plan_gives_them():
    assert set(sweeps.JOB_CADENCES) == {
        "poll_bills", "poll_purchaseorders", "poll_items", "poll_contacts",
        "sweep_po_anchored", "sweep_bill_detail", "sweep_control_totals",
        "sweep_completeness"}
    assert sweeps.JOB_CADENCES["poll_bills"] == "5 min"
    assert sweeps.JOB_CADENCES["sweep_po_anchored"] == "15 min"
    assert sweeps.JOB_CADENCES["sweep_control_totals"] == "nightly"
    assert sweeps.JOB_CADENCES["sweep_completeness"] == "nightly"


def test_every_job_kind_has_a_chunk_size_the_framework_knows_about():
    for kind in sweeps.JOB_CADENCES:
        assert kind in jobs.CHUNK_SIZES


def test_the_correlation_id_reaches_the_reconciliation_exception():
    """One id from the HTTP response to the ledger movement (§11.9)."""
    clock = FakeClock()
    store, _, job = _po_estate(clock, pos=1, resolvable=False)
    store.enqueue(job.kind)
    run = jobs.run_job(job, store=store, clock=clock, capabilities=ERP,
                       correlation_id="corr-grn-1")
    assert run.correlation_id == "corr-grn-1"
    assert {e["correlation_id"] for e in store.exceptions.values()} == \
        {"corr-grn-1"}
    assert {row["correlation_id"] for row in store.inbox.values()} == \
        {"corr-grn-1"}


def test_two_identifierless_lines_on_one_receive_are_two_exceptions():
    """The silent drop that nearly arrived through the anti-silent-drop code.

    On ERP a receive line with no `line_item_id` is the expected case, not the
    exotic one. If both such lines on a receive keyed their exception on the
    same string, the second would dedupe into the first and its value would
    disappear from the bucket -- an unattributed amount silently lost by the
    mechanism whose entire purpose is that nothing is silently lost.
    """
    clock = FakeClock()
    store = InMemoryStore()
    store.open_pos.append(sweeps.LocalPurchaseOrder(
        po_id="PO-1", external_id="PO-EXT-1", entity_id="ENT-1",
        project_id="PRJ-1"))
    adapter = FakeAdapter(clock, receives={"PO-EXT-1": [receive(
        1, lines=[FakeLine(purchase_order_line_external_id=None, line_total_paise=100_000),
                  FakeLine(purchase_order_line_external_id=None, line_total_paise=250_000)])]},
        seconds_per_call=1)
    _run(sweeps.SweepPoAnchored(adapter, store, store.connection_id), store,
         clock)

    quarantined = [e for e in store.exceptions.values()
                   if e["kind"] == sweeps.KIND_GRN_LINE_UNATTRIBUTED]
    assert len(quarantined) == 2
    assert sorted(e["source_paise"] for e in quarantined) == [100_000, 250_000]
    assert store.unattributed_paise == {"PRJ-1": 350_000}


def test_a_recurring_sweep_keeps_its_cursor_across_ticks():
    """The row is reused, so `last_po_id_swept` survives to the next tick.

    A cron that enqueued a fresh job row every fifteen minutes would hand each
    invocation an empty checkpoint: the walk would re-read the first 50 open
    POs for ever and the 51st would never be swept. On ERP that is not slow --
    it is receives that are never acquired at all.
    """
    clock = FakeClock()
    store, adapter, job = _po_estate(clock, pos=5)
    job.batch_size = 2

    first = _run(job, store, clock)
    assert first.state == jobs.JOB_DONE
    assert first.checkpoint["last_po_id_swept"] == "PO-0001"

    assert jobs.reschedule(store, first, clock=clock) is True
    second = jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    assert second.checkpoint["last_po_id_swept"] == "PO-0003", (
        "the second tick continues from the cursor, not from the first PO")
    assert [detail for name, detail in adapter.calls] == [
        "PO-EXT-0000", "PO-EXT-0001", "PO-EXT-0002", "PO-EXT-0003"]


def test_a_dead_job_is_never_revived_by_the_scheduler():
    """DEAD means a human looks at it. Rescheduling it would loop for ever,
    which is what DEAD exists to stop."""
    store = InMemoryStore()
    clock = FakeClock()
    dead = jobs.JobRun(kind="sweep_po_anchored", claimed=True,
                       state=jobs.JOB_DEAD, job_id="JOB-X")
    paused = jobs.JobRun(kind="sweep_po_anchored", claimed=True,
                        state=jobs.JOB_CHECKPOINTED, job_id="JOB-Y")
    assert jobs.reschedule(store, dead, clock=clock) is False
    assert jobs.reschedule(store, paused, clock=clock) is False
    assert store.checkpoint_writes == []
