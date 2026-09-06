"""Outbound emission: the dedupe key, the D-7 shape, the budget, draft-to-open.

Wave 5, stream 6. §11.6, §11.7, §18.5 rows Z-01/Z-02/Z-09.

``test_outbound_chaos.py`` holds the survival property. This file holds
everything that has to be true for that property to be worth having: that the
key is bounded and collision-free, that D-7 is read rather than assumed, that
money stays integer paise, that the daily ceiling is the one that binds, and
that a purchase order does not become a commitment before our approval closes.

No network, no tenant, no Catalyst.
"""
from __future__ import annotations

import inspect
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backend.integration import outbound as ob          # noqa: E402
from app.backend.money import MoneyError                    # noqa: E402
from outbound_tenant_fake import (                          # noqa: E402
    CONNECTION_ID, MODULE, NOW, FakeAdapter, FakeCapabilities, FakeTenant,
    InMemoryBudget, InMemoryOutboxStore, StubApprovalGate, cell, enqueued,
    line,
)

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "backend" /
          "integration" / "outbound.py").read_text(encoding="utf-8")


# ======================================================================
# The dedupe key
# ======================================================================

def test_the_dedupe_key_stays_inside_a_zoho_single_line_text_field():
    """A truncated key is a COLLIDING key.

    ``cf_capex_ref`` is single-line text: 255 characters. A key longer than the
    field does not error, it truncates -- and two different requisitions whose
    keys truncate to the same 255 characters would then look like the same
    purchase order to the unique index, so the second would be silently
    refused. That is a *suppressed* commitment, which is the mirror image of
    the duplicate and just as wrong.
    """
    absurd = "PR-" + "X" * 4000
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, absurd)
    assert len(key) <= ob.DEDUPE_KEY_MAX_LENGTH <= 255

    # And two long ids that share their first 4000 characters still differ,
    # because the hash covers the untruncated input.
    other = ob.derive_dedupe_key(CONNECTION_ID, MODULE, absurd + "-2")
    assert key != other


def test_the_dedupe_key_survives_identifiers_zoho_would_not_accept():
    """Whatever the local id contains, the key stays a plain token."""
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR/2026 #17 (rev 2)")
    assert key.startswith("CAPEX-")
    assert all(c.isalnum() or c in "-._" for c in key), key


@pytest.mark.parametrize("connection_id,module,local_id", [
    ("", MODULE, "PR-1"),
    (CONNECTION_ID, "", "PR-1"),
    (CONNECTION_ID, MODULE, ""),
    (CONNECTION_ID, MODULE, "   "),
    (None, MODULE, "PR-1"),
])
def test_a_key_cannot_be_derived_from_a_missing_identifier(connection_id, module,
                                                            local_id):
    """A key built from a blank is not deterministic, it is shared.

    Two rows with an empty ``local_id`` would derive the same key and the
    second purchase order would be refused as a duplicate of the first.
    """
    with pytest.raises(ob.OutboundError):
        ob.derive_dedupe_key(connection_id, module, local_id)


# ======================================================================
# D-7 / Z-02: the emission shape
# ======================================================================

def _three_cell_lines():
    return [
        line("L1", wbs="WBS-1000", head="BH-PLANT", amount_paise=100_000_00),
        line("L2", wbs="WBS-2000", head="BH-PLANT", amount_paise=200_000_00),
        line("L3", wbs="WBS-1000", head="BH-CIVIL", amount_paise=50_000_00),
    ]


def test_with_line_level_custom_fields_one_requisition_is_one_purchase_order():
    """D-7 True: the dimensions ride on the lines and nothing splits."""
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=True))

    assert len(plan.drafts) == 1
    assert plan.line_level_dimensions is True
    assert plan.process_change is None
    assert len(plan.control_cells) == 3

    payload = plan.drafts[0].as_payload("K")
    assert [item["wbs_id"] for item in payload["lines"]] == [
        "WBS-1000", "WBS-2000", "WBS-1000"]
    assert "wbs_id" not in payload, (
        "With line-level dimensions the header must not also carry one; a "
        "header dimension on a multi-WBS purchase order is a lie about which "
        "control cell the money came from.")


def test_with_line_level_custom_fields_the_shape_is_marked_unverified():
    """D-7 is unresolved. Using the capability does not resolve it.

    The Definition of Done requires anything unconfirmed to be labelled
    ``UNVERIFIED - REQUIRES ZOHO CONFIRMATION`` in code, UI and documents. The
    line-level path is chosen from a capability flag that has never been probed
    in the client's tenant (§18.5 Z-02's verification step), so the plan says so
    rather than presenting the shape as settled.
    """
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=True))

    assert any(ob.UNVERIFIED in note for note in plan.notes)
    assert any(ob.UNVERIFIED in p for p in plan.drafts[0].provenance)


def test_header_only_dimensions_force_one_purchase_order_per_control_cell():
    """D-7 False: three control cells become three purchase orders.

    §18.5 Z-02 states the consequence and this is it, exercised. The split is
    on ``(wbs_id, budget_head_id)`` -- the grain ``po_line`` is keyed on -- not
    on WBS alone, so two budget heads under one WBS are still two purchase
    orders.
    """
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=False))

    assert len(plan.drafts) == 3
    assert plan.line_level_dimensions is False
    assert {d.header_cell.key for d in plan.drafts} == {
        ("WBS-1000", "BH-PLANT"), ("WBS-2000", "BH-PLANT"),
        ("WBS-1000", "BH-CIVIL")}
    for draft in plan.drafts:
        payload = draft.as_payload("K")
        assert payload["wbs_id"] == draft.header_cell.wbs_id
        assert payload["budget_head_id"] == draft.header_cell.budget_head_id
        assert all("wbs_id" not in item for item in payload["lines"])

    # Nothing is lost or double-counted in the split.
    assert sum(d.total_paise for d in plan.drafts) == 350_000_00


def test_the_header_only_split_is_reported_as_a_procurement_process_change():
    """Not a log line. A consequence the caller cannot discard by accident.

    Three purchase orders where the business expected one means three documents
    for the vendor to acknowledge, receive against and invoice. That is the
    client's process changing, and §18.5 puts Z-02 on the project's side of the
    matrix precisely so it is decided rather than absorbed.
    """
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=False))

    change = plan.process_change
    assert change is not None
    assert change.code == "HEADER_ONLY_PO_SPLIT"
    assert change.purchase_orders == 3
    assert change.depends_on == "D-7"
    assert "process" in change.detail.lower()

    with pytest.raises(ob.EmissionShapeError):
        plan.assert_acknowledged(False)
    plan.assert_acknowledged(True)          # acknowledged: proceeds


def test_a_single_cell_requisition_does_not_pretend_to_be_a_process_change():
    """No split, no claim of one. A control that cries wolf gets ignored."""
    plan = ob.plan_emission(
        local_id="PR-0008", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=[line("L1"), line("L2")],
        capabilities=FakeCapabilities(line_level_custom_fields=False))

    assert len(plan.drafts) == 1
    assert plan.process_change is None
    plan.assert_acknowledged(False)         # nothing to acknowledge


def test_each_split_purchase_order_gets_its_own_dedupe_key():
    """The split's own duplicate hazard, closed.

    If the three purchase orders of one requisition all derived their key from
    the same ``local_id``, Z-01's unique index would refuse two of the three --
    and two thirds of the commitment would silently never reach the vendor. The
    control cell is part of the split's local identity for exactly this reason.
    """
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=False))

    keys = {ob.derive_dedupe_key(CONNECTION_ID, MODULE, d.local_id)
            for d in plan.drafts}
    assert len(keys) == 3


def test_the_plan_is_the_same_every_time_it_is_built():
    """An unstable plan is an unstable key, and an unstable key is a duplicate."""
    caps = FakeCapabilities(line_level_custom_fields=False)
    kwargs = dict(local_id="PR-0007", connection_id=CONNECTION_ID,
                  vendor_external_id="ZV-77", capabilities=caps)
    first = ob.plan_emission(lines=_three_cell_lines(), **kwargs)
    again = ob.plan_emission(lines=_three_cell_lines(), **kwargs)

    assert [d.local_id for d in first.drafts] == [d.local_id for d in again.drafts]
    assert first.control_cells == again.control_cells


def test_a_header_only_draft_spanning_two_cells_is_refused_outright():
    """The invariant the split exists to preserve, guarded at the type.

    A hand-built draft that puts two control cells on one header-only purchase
    order would attribute the whole commitment to whichever cell the header
    named. That is a wrong number, not a formatting problem.
    """
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(
            local_id="PR-X", connection_id=CONNECTION_ID,
            vendor_external_id="ZV-77",
            lines=(line("L1", wbs="WBS-1"), line("L2", wbs="WBS-2")),
            header_cell=cell("WBS-1"), line_level_dimensions=False)


def test_a_purchase_order_cannot_be_constructed_already_open():
    """§11.7: emitted as draft, moved to open only by our own later call."""
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(
            local_id="PR-X", connection_id=CONNECTION_ID,
            vendor_external_id="ZV-77", lines=(line(),),
            state=ob.PO_STATE_OPEN)


def test_a_purchase_order_with_no_lines_is_refused():
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(local_id="PR-X", connection_id=CONNECTION_ID,
                              vendor_external_id="ZV-77", lines=())


def test_a_line_with_no_control_cell_cannot_exist():
    """An uncheckable commitment is the failure the product exists to prevent."""
    for wbs, head in (("", "BH-1"), ("WBS-1", ""), ("  ", "BH-1")):
        with pytest.raises(ob.EmissionShapeError):
            ob.ControlCell(wbs_id=wbs, budget_head_id=head)


# ======================================================================
# Money
# ======================================================================

@pytest.mark.parametrize("amount", [
    2500.00, 2500.5, Decimal("250000"), "250000", None, True,
])
def test_money_reaching_a_purchase_order_line_must_already_be_integer_paise(amount):
    """Not parsed, not coerced -- refused.

    ``money.to_paise`` exists for user input arriving in rupees. This is not
    that: by the time a line reaches the outbound path the figure has been
    through the budget check as ``bigint`` paise, so anything else is a defect
    upstream. Accepting a float here -- even ``2500.00``, which is exactly
    representable -- would let the float travel into a commitment, and the next
    one would not be exact.
    """
    with pytest.raises(MoneyError):
        ob.PoLine(line_id="L1", cell=cell(), description="d", quantity=1,
                  unit_price_paise=amount, amount_paise=1)
    with pytest.raises(MoneyError):
        ob.PoLine(line_id="L1", cell=cell(), description="d", quantity=1,
                  unit_price_paise=1, amount_paise=amount)


def test_a_negative_line_amount_is_refused():
    with pytest.raises(MoneyError):
        ob.PoLine(line_id="L1", cell=cell(), description="d", quantity=1,
                  unit_price_paise=1, amount_paise=-1)


def test_no_float_reaches_the_payload_handed_to_the_adapter():
    """The end-to-end statement: nothing monetary leaves here as a float."""
    plan = ob.plan_emission(
        local_id="PR-0007", connection_id=CONNECTION_ID,
        vendor_external_id="ZV-77", lines=_three_cell_lines(),
        capabilities=FakeCapabilities(line_level_custom_fields=True))
    payload = plan.drafts[0].as_payload("K")

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            assert not isinstance(node, (float, Decimal)), (
                f"{node!r} left the outbound path as {type(node).__name__}")

    walk(payload)
    assert payload["total_paise"] == 350_000_00
    assert isinstance(payload["total_paise"], int)


def test_the_outbound_source_never_converts_money_through_a_float():
    """A static guard, because the runtime one only sees the paths tests take."""
    for forbidden in ("float(", "Decimal(", "/ 100", "* 0.01", "round("):
        assert forbidden not in SOURCE, (
            f"{forbidden!r} appears in outbound.py; money is integer paise "
            "end to end.")


# ======================================================================
# Rate budget (§11.6, §18.5 Z-09)
# ======================================================================

def test_outbound_gets_thirty_of_the_hundred_calls_a_minute():
    """§11.6's allocation, stated as a constant so it cannot drift silently."""
    assert ob.OUTBOUND_MINUTE_ALLOCATION == 30


def test_on_erp_standard_the_daily_ceiling_binds_and_not_the_per_minute_one():
    """The arithmetic §11.6 asserts, computed.

    30 calls a minute is 43,200 a day. ERP Standard allows 2,000. A throttle
    that watched only the per-minute allocation would let a backfill spend the
    tenant's entire day in about 67 minutes and then fail every call -- polling,
    outbound and interactive alike -- until midnight.
    """
    erp_standard = FakeCapabilities(daily_call_ceiling=2000)
    assert ob.binding_window(erp_standard) == "DAY"
    assert ob.OUTBOUND_MINUTE_ALLOCATION * 60 * 24 == 43_200
    assert 43_200 > erp_standard.daily_call_ceiling

    generous = FakeCapabilities(daily_call_ceiling=1_000_000)
    assert ob.binding_window(generous) == "MINUTE"


def test_being_over_budget_defers_the_row_and_costs_neither_a_call_nor_an_attempt():
    """§11.6: the job checkpoints and the next cron tick resumes.

    Over budget is not a failure, so it must not burn one of the eight attempts
    that stand between a row and DEAD. A row that exhausted its attempts on
    throttling would arrive on SCR-39 as a dead send that was never sent.
    """
    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    adapter = FakeAdapter(tenant=tenant)
    enqueued(store)
    spent = InMemoryBudget(minute_used=ob.OUTBOUND_MINUTE_ALLOCATION)

    with pytest.raises(ob.BudgetExhausted) as exhausted:
        ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                               budget=spent, now=NOW)

    assert exhausted.value.window == "MINUTE"
    assert adapter.calls == []
    assert tenant.records == {}
    row = store.get("OB-1")
    assert row.attempts == 0, "Throttling must not consume a send attempt."
    assert row.state == ob.OUTBOX_DEFERRED

    # And the deferred row is picked up by the next tick unchanged.
    result = ob.emit_purchase_order(adapter=adapter, store=store,
                                    outbox_id="OB-1",
                                    budget=InMemoryBudget(), now=NOW)
    assert result.created is True
    assert len(tenant.records) == 1


def test_the_daily_window_refuses_before_the_minute_window_is_touched():
    """Reported as DAY, not as MINUTE. The operator needs the real ceiling."""
    store = InMemoryOutboxStore()
    enqueued(store)
    spent = InMemoryBudget(day_used=ob.ASSUMED_DAILY_CALL_CEILING)

    with pytest.raises(ob.BudgetExhausted) as exhausted:
        ob.emit_purchase_order(adapter=FakeAdapter(tenant=FakeTenant()),
                               store=store, outbox_id="OB-1", budget=spent,
                               now=NOW)
    assert exhausted.value.window == "DAY"
    assert spent.minute_used == 0


class _RecordingSession:
    """Enough of a ``Session`` for the C2-table budget."""

    def __init__(self, used_by_window):
        self.used_by_window = dict(used_by_window)
        self.statements = []

    def fetchone(self, statement, params=None):
        self.statements.append((statement, params))
        window = params["window_kind"]
        self.used_by_window[window] = self.used_by_window.get(window, 0) + params["calls"]
        return (self.used_by_window[window],)


def test_the_postgres_budget_charges_the_daily_window_first():
    """§11.6 puts the budget in PostgreSQL because no process is resident.

    The order matters: charging the minute window for a call the daily ceiling
    is going to refuse spends an allocation the interactive path may need, and
    on ERP Standard the daily ceiling is the one that refuses.
    """
    session = _RecordingSession({"DAY": ob.ASSUMED_DAILY_CALL_CEILING})
    budget = ob.PgOutboundRateBudget(
        session=session, connection_id=CONNECTION_ID,
        capabilities=FakeCapabilities(daily_call_ceiling=2000))

    decision = budget.reserve(1, now=NOW)

    assert decision.granted is False
    assert decision.window == "DAY"
    assert [p["window_kind"] for _, p in session.statements] == ["DAY"], (
        "The minute window must not be charged once the day has refused.")
    assert "integration_rate_budget" in session.statements[0][0]
    assert "RETURNING used" in session.statements[0][0]


def test_the_postgres_budget_reads_its_daily_ceiling_from_capabilities():
    """Z-09: the plan tier decides the ceiling. It is never hardcoded."""
    session = _RecordingSession({})
    budget = ob.PgOutboundRateBudget(
        session=session, connection_id=CONNECTION_ID,
        capabilities=FakeCapabilities(daily_call_ceiling=25_000))

    assert budget.ceiling("DAY") == 25_000
    assert budget.ceiling("MINUTE") == ob.OUTBOUND_MINUTE_ALLOCATION
    assert budget.reserve(1, now=NOW).granted is True


def test_backoff_stays_inside_the_documented_envelope():
    """§11.6: ``random(0, min(900s, 2s * 2^attempts))``, full jitter."""
    for attempts in range(0, 20):
        for _ in range(20):
            delay = ob.backoff_delay(attempts).total_seconds()
            assert 0 <= delay <= ob.BACKOFF_CEILING_SECONDS
            assert delay <= min(ob.BACKOFF_CEILING_SECONDS,
                                ob.BACKOFF_BASE_SECONDS * 2 ** attempts)


def test_a_row_goes_dead_rather_than_retrying_for_ever():
    """§11.6: max_attempts=8, then DEAD and visible on SCR-39."""
    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    enqueued(store)

    class _Broken(FakeAdapter):
        def create_purchase_order(self, payload, dedupe_key):
            raise TimeoutError("gateway timeout")

    adapter = _Broken(tenant=tenant)
    for attempt in range(1, ob.MAX_ATTEMPTS + 1):
        with pytest.raises(TimeoutError):
            ob.emit_purchase_order(adapter=adapter, store=store,
                                   outbox_id="OB-1", now=NOW)
        row = store.get("OB-1")
        expected = ob.OUTBOX_DEAD if attempt >= ob.MAX_ATTEMPTS else ob.OUTBOX_RETRY
        assert row.state == expected, f"attempt {attempt} left state {row.state}"

    assert store.get("OB-1").state == ob.OUTBOX_DEAD
    assert tenant.records == {}


# ======================================================================
# §11.7 -- draft to open
# ======================================================================

def _emitted():
    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    adapter = FakeAdapter(tenant=tenant)
    enqueued(store)
    ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                           now=NOW)
    return tenant, store, adapter


def test_an_emitted_purchase_order_lands_in_the_tenant_as_a_draft():
    """§11.7. Until our approval closes it is a document, not a commitment."""
    tenant, store, _ = _emitted()
    assert list(tenant.statuses().values()) == [ob.PO_STATE_DRAFT]


def test_a_draft_is_not_opened_while_our_approval_instance_is_still_open():
    """The refusal the whole PR-as-system-of-record strategy rests on.

    §11.7: if the commitment can become real before ``budget_check`` has
    cleared it, the refusal is advisory and the product's central claim
    collapses.
    """
    tenant, store, adapter = _emitted()
    gate = StubApprovalGate(closed=set())

    with pytest.raises(ob.ApprovalNotClosed):
        ob.transition_to_open(adapter=adapter, store=store, outbox_id="OB-1",
                              approval_gate=gate, actor="U-1")

    assert list(tenant.statuses().values()) == [ob.PO_STATE_DRAFT]


def test_a_draft_is_opened_by_our_own_call_once_the_approval_has_closed():
    tenant, store, adapter = _emitted()
    gate = StubApprovalGate(closed={"PR-0001"})

    external_id = ob.transition_to_open(
        adapter=adapter, store=store, outbox_id="OB-1", approval_gate=gate,
        actor="U-1")

    assert tenant.records[external_id]["state"] == ob.PO_STATE_OPEN
    assert tenant.records[external_id]["transitioned_by"] == "U-1"


def test_a_row_that_never_reached_the_tenant_cannot_be_opened():
    """There is nothing there to transition, and saying otherwise is a lie."""
    store = InMemoryOutboxStore()
    enqueued(store)
    with pytest.raises(ob.OutboundError):
        ob.transition_to_open(adapter=FakeAdapter(tenant=FakeTenant()),
                              store=store, outbox_id="OB-1",
                              approval_gate=StubApprovalGate(closed={"PR-0001"}),
                              actor="U-1")


def test_an_adapter_that_cannot_transition_says_so_instead_of_pretending():
    """Reported, not worked around: stream 1 owns ``adapter.py``."""
    tenant, store, _ = _emitted()

    class _NoTransition:
        def capabilities(self):
            return FakeCapabilities()

        def create_purchase_order(self, payload, key):  # pragma: no cover
            raise AssertionError("not reached")

    with pytest.raises(ob.OutboundError, match="transition_purchase_order"):
        ob.transition_to_open(adapter=_NoTransition(), store=store,
                              outbox_id="OB-1",
                              approval_gate=StubApprovalGate(closed={"PR-0001"}),
                              actor="U-1")
    assert list(tenant.statuses().values()) == [ob.PO_STATE_DRAFT]


# ======================================================================
# D-14: the target product is provisional
# ======================================================================

def test_no_zoho_endpoint_base_url_or_scope_string_appears_in_outbound():
    """The target product is PROVISIONAL (D-14).

    Product-specific facts live behind the adapter and its ``Capabilities``,
    never in a caller. A base URL or a scope string here would have to be
    rewritten if D-14 resolves to Books + Inventory rather than ERP -- and
    would be wrong in the meantime.
    """
    lowered = SOURCE.lower()
    for forbidden in ("http://", "https://", "zohoapis", ".zoho.",
                      "/api/v1/purchaseorders", "zohoerp.", "scope="):
        assert forbidden not in lowered, (
            f"{forbidden!r} appears in outbound.py; product-specific facts "
            "belong behind the C1 adapter (D-14 is unresolved).")


def test_outbound_imports_nothing_from_the_other_streams_files():
    """The streams run in parallel and no stream edits another's files.

    Written against the frozen C1/C2 signatures and duck-typed, so this module
    compiles and its tests run before ``adapter.py``, ``dto.py``,
    ``throttle.py`` or ``integration_store.py`` exist.

    Checked over the parsed import statements, not the source text: the names
    are all over the prose above, where they belong.
    """
    import ast

    imported: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)

    forbidden = {"adapter", "dto", "erp", "books_inventory", "throttle", "jobs",
                 "sweeps", "statuses", "integration_store"}
    for name in imported:
        tail = name.rsplit(".", 1)[-1]
        assert tail not in forbidden, (
            f"outbound.py imports {name!r}, a file another Wave 5 stream owns. "
            "The seams are duck-typed so the streams stay file-disjoint and "
            "none blocks on another.")


def test_the_adapter_extensions_outbound_needs_are_declared_not_assumed():
    """C1 as frozen cannot express an idempotent retry, and that is recorded.

    ``create_purchase_order(po, dedupe_key)`` takes the key but says nothing
    about what happens when the key already exists. The two calls that close
    the gap are optional at runtime and probed, so an adapter implementing only
    frozen C1 still works -- less safely, and the result says so.
    """
    assert "resolve_by_dedupe_key" in SOURCE
    assert "update_purchase_order" in SOURCE
    assert "declared here, not added to" in SOURCE

    source = inspect.getsource(ob.emit_purchase_order)
    assert 'getattr(adapter, "resolve_by_dedupe_key"' in source
    assert "callable(resolve)" in source


def test_an_adapter_with_only_frozen_c1_still_emits_and_says_what_it_lost():
    """Degraded, and honest about being degraded."""
    from outbound_tenant_fake import C1OnlyAdapter

    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    adapter = C1OnlyAdapter(tenant=tenant)
    enqueued(store)

    first = ob.emit_purchase_order(adapter=adapter, store=store,
                                   outbox_id="OB-1", now=NOW)
    assert first.created is True and first.note == ""

    # A second row whose first attempt failed: the note appears once the
    # emission is a retry, which is when the missing capability starts to cost.
    store.rows["OB-1"].state = ob.OUTBOX_RETRY
    second = ob.emit_purchase_order(
        adapter=C1OnlyAdapter(tenant=tenant), store=store, outbox_id="OB-1",
        now=NOW + timedelta(seconds=1))
    assert "resolve_by_dedupe_key" in second.note
    assert "Z-01" in second.note
    assert len(tenant.records) == 1


# ======================================================================
# The chunk stream 5's job drains
# ======================================================================

def _chunk_of(n):
    store = InMemoryOutboxStore()
    for i in range(n):
        enqueued(store, outbox_id=f"OB-{i}", local_id=f"PR-{i:04d}")
    return store, [f"OB-{i}" for i in range(n)]


def test_a_chunk_sends_every_row_it_is_given():
    tenant = FakeTenant()
    store, ids = _chunk_of(5)

    outcome = ob.emit_chunk(adapter=FakeAdapter(tenant=tenant), store=store,
                            outbox_ids=ids, budget=InMemoryBudget(), now=NOW)

    assert len(outcome.sent) == 5
    assert outcome.failed == ()
    assert outcome.checkpoint == ()
    assert len(tenant.records) == 5


def test_running_out_of_budget_checkpoints_the_chunk_rather_than_failing_it():
    """§11.6: over budget, the job checkpoints and returns; the next tick resumes.

    On ERP Standard the daily ceiling ends most busy chunks this way. That is
    the design working, so SCR-39 must be able to say "waiting on the daily
    quota" rather than "failed" -- which is what ``stopped_on`` is for.
    """
    tenant = FakeTenant()
    store, ids = _chunk_of(5)
    budget = InMemoryBudget(day_used=ob.ASSUMED_DAILY_CALL_CEILING - 2)

    outcome = ob.emit_chunk(adapter=FakeAdapter(tenant=tenant), store=store,
                            outbox_ids=ids, budget=budget, now=NOW)

    assert len(outcome.sent) == 2
    assert outcome.stopped_on == "BUDGET_DAY"
    assert outcome.exhausted_budget is True
    assert outcome.checkpoint == ("OB-2", "OB-3", "OB-4"), (
        "The row that could not be sent stays in the checkpoint; it is still "
        "owed, not consumed.")
    assert len(tenant.records) == 2

    # The next tick, with budget, finishes exactly the remainder.
    resumed = ob.emit_chunk(adapter=FakeAdapter(tenant=tenant), store=store,
                            outbox_ids=outcome.checkpoint,
                            budget=InMemoryBudget(), now=NOW)
    assert len(resumed.sent) == 3
    assert len(tenant.records) == 5
    assert len({r.external_id for r in outcome.sent + resumed.sent}) == 5


def test_one_bad_row_does_not_abandon_the_rest_of_the_chunk():
    """The other purchase orders in the chunk are still owed to the vendor."""
    tenant = FakeTenant()
    store, ids = _chunk_of(4)
    store.rows["OB-2"].dedupe_key = "CAPEX-not-what-the-derivation-makes"

    outcome = ob.emit_chunk(adapter=FakeAdapter(tenant=tenant), store=store,
                            outbox_ids=ids, budget=InMemoryBudget(), now=NOW)

    assert len(outcome.sent) == 3
    assert [row for row, _ in outcome.failed] == ["OB-2"]
    assert ob.KIND_DEDUPE_KEY_COLLISION in outcome.failed[0][1]
    assert outcome.checkpoint == ()
    assert len(tenant.records) == 3


def test_a_chunk_re_run_over_rows_it_already_sent_creates_nothing():
    """A resumed job re-reading its chunk is the ordinary case, not an edge one."""
    tenant = FakeTenant()
    store, ids = _chunk_of(3)
    adapter = FakeAdapter(tenant=tenant)

    ob.emit_chunk(adapter=adapter, store=store, outbox_ids=ids,
                  budget=InMemoryBudget(), now=NOW)
    calls = len(adapter.calls)
    again = ob.emit_chunk(adapter=adapter, store=store, outbox_ids=ids,
                          budget=InMemoryBudget(), now=NOW)

    assert len(tenant.records) == 3
    assert all(r.created is False for r in again.sent)
    assert len(adapter.calls) == calls, (
        "Re-observing settled rows must cost zero API calls; on ERP Standard "
        "the daily ceiling is only 2,000 and a job that re-sends its chunk "
        "every tick would spend it on nothing.")
