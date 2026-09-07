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
from app.backend.pg import integration_store as store_module  # noqa: E402
from app.backend.integration.dto import (                     # noqa: E402
    DtoError, EmissionRef, PurchaseOrderEmissionDTO,
)
from outbound_tenant_fake import (                          # noqa: E402
    CONNECTION_ID, DOCUMENT_DATE, MODULE, NOW, FakeAdapter, FakeCapabilities,
    FakeTenant, InMemoryBudget, InMemoryOutboxStore, StubApprovalGate, cell,
    emission_document, enqueued, line,
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
        document_date=DOCUMENT_DATE,
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
        document_date=DOCUMENT_DATE,
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
        document_date=DOCUMENT_DATE,
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
        document_date=DOCUMENT_DATE,
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
        document_date=DOCUMENT_DATE,
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
        document_date=DOCUMENT_DATE,
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
    first = ob.plan_emission(document_date=DOCUMENT_DATE, lines=_three_cell_lines(), **kwargs)
    again = ob.plan_emission(document_date=DOCUMENT_DATE, lines=_three_cell_lines(), **kwargs)

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
        document_date=DOCUMENT_DATE,
            local_id="PR-X", connection_id=CONNECTION_ID,
            vendor_external_id="ZV-77",
            lines=(line("L1", wbs="WBS-1"), line("L2", wbs="WBS-2")),
            header_cell=cell("WBS-1"), line_level_dimensions=False)


def test_a_purchase_order_cannot_be_constructed_already_open():
    """§11.7: emitted as draft, moved to open only by our own later call."""
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(
        document_date=DOCUMENT_DATE,
            local_id="PR-X", connection_id=CONNECTION_ID,
            vendor_external_id="ZV-77", lines=(line(),),
            state=ob.PO_STATE_OPEN)


def test_a_purchase_order_with_no_lines_is_refused():
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(document_date=DOCUMENT_DATE, local_id="PR-X", connection_id=CONNECTION_ID,
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
        document_date=DOCUMENT_DATE,
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


def test_being_over_budget_leaves_the_row_and_costs_neither_a_call_nor_an_attempt():
    """§11.6: the job checkpoints and the next cron tick resumes.

    Over budget is not a failure, so it must not burn one of the eight attempts
    that stand between a row and DEAD. A row that exhausted its attempts on
    throttling would arrive on SCR-39 as a dead send that was never sent.

    **The row is left exactly as it was found.** It used to be pushed to
    ``DEFERRED`` -- a state ``ck_integration_outbox_state`` rejects -- by a
    ``release()`` call on a row this function had not yet claimed. There is
    nothing to release: an unclaimed row is still PENDING, still due, and the
    next tick picks it up with no transition having been attempted at all.
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
    assert row.state == ob.OUTBOX_PENDING, (
        "An unclaimed row stays PENDING. DEFERRED was never a state this "
        "table could hold.")
    assert store.locked == set(), (
        "and nothing was claimed, so nothing is holding a lock.")

    # And the row is picked up by the next tick unchanged.
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


# ----------------------------------------------------------------------
# The rate budget, delegated. These three tests replace three that asserted
# the SQL `PgOutboundRateBudget` used to carry -- SQL that could not execute:
# it conflicted on `(connection_id, window_kind, window_start)`, which is not
# the primary key, omitted five NOT NULL columns, and used an
# over-reserve-then-refuse strategy that
# `ck_integration_rate_budget_used_within_ceiling` makes structurally
# impossible. The old tests passed because `_RecordingSession` was a dict that
# agreed with the statement instead of a table that would have rejected it.
#
# The statement is gone rather than patched. What is asserted now is the
# DELEGATION -- that the right allocation, window and clock reach
# `integration_store.reserve_calls` -- because the statement itself is already
# executed against a live PostgreSQL by tests/test_pg_integration_rate_budget.py,
# which is the only place SQL of this kind can honestly be tested.
# ----------------------------------------------------------------------

class _RecordingStore:
    """Stands in for ``integration_store``, recording how it was called."""

    RateBudgetExhausted = store_module.RateBudgetExhausted
    IntegrationStoreError = store_module.IntegrationStoreError

    def __init__(self, *, grant=None, refuse=None, read=None):
        self._grant = grant
        self._refuse = refuse
        self._read = read if read is not None else {}
        self.reserve_calls_args = []
        self.read_rate_budget_args = []

    def reserve_calls(self, session, **kwargs):
        self.reserve_calls_args.append(kwargs)
        if self._refuse is not None:
            raise store_module.RateBudgetExhausted(
                self._refuse, f"{self._refuse} window refused")
        return self._grant

    def read_rate_budget(self, session, **kwargs):
        self.read_rate_budget_args.append(kwargs)
        return self._read


def test_the_outbound_budget_delegates_to_the_canonical_reservation(monkeypatch):
    """One implementation of this write, and it is the schema author's.

    The allocation is OUTBOUND and is not a parameter: a caller able to choose
    could spend the POLLING budget the reconciliation sweeps depend on
    (§11.6's 60/30/10 split).
    """
    fake = _RecordingStore(grant={
        "MINUTE": {"used": 4, "ceiling": 30, "remaining": 26},
        "DAY": {"used": 1995, "ceiling": 2000, "remaining": 5},
    })
    monkeypatch.setattr(ob, "store", fake)
    budget = ob.PgOutboundRateBudget(session=object(),
                                     connection_id=CONNECTION_ID)

    decision = budget.reserve(3, now=NOW)

    assert decision.granted is True
    (call,) = fake.reserve_calls_args
    assert call["connection_id"] == CONNECTION_ID
    assert call["allocation"] == "OUTBOUND"
    assert call["count"] == 3
    assert call["tz"] == "UTC"
    assert call["now"] == NOW

    # The honest "how many more may I make" is the SMALLEST remaining, not the
    # window `binding_window` predicts from the plan tier.
    assert decision.window == "DAY"
    assert decision.remaining == 5


def test_a_refused_reservation_reports_the_figures_from_after_the_rollback(
        monkeypatch):
    """The count read back must be the post-rollback one, not the spent one.

    The store refuses inside a savepoint and unwinds it, so nothing was
    actually spent. Reporting the figure from before the rollback would state
    the over-reserved count -- the very number
    ``ck_integration_rate_budget_used_within_ceiling`` exists to stop anyone
    believing.
    """
    fake = _RecordingStore(refuse="DAY", read={
        "DAY": {"used": 1998, "ceiling": 2000, "remaining": 2,
                "exhausted_at": None},
        "MINUTE": {"used": 2, "ceiling": 30, "remaining": 28,
                   "exhausted_at": None},
    })
    monkeypatch.setattr(ob, "store", fake)
    budget = ob.PgOutboundRateBudget(session=object(),
                                     connection_id=CONNECTION_ID)

    decision = budget.reserve(5, now=NOW)

    assert decision.granted is False
    assert decision.window == "DAY"
    assert decision.remaining == 2, (
        "2000 - 1998, read AFTER the rollback. Not 0, and not the 2003 the "
        "refused statement would briefly have implied.")
    assert "2000" in decision.reason and "1998" in decision.reason
    assert fake.read_rate_budget_args, "The re-read is what makes this honest."


def test_a_window_with_no_row_is_not_reported_as_a_window_reading_zero(
        monkeypatch):
    """"No row" and "a row reading zero" are different facts.

    ``read_rate_budget`` OMITS a window whose row does not exist -- it does not
    default it. Indexing blindly would raise ``KeyError``; defaulting to zero
    would report a *seeded window that is fully spent*, which reads as "come
    back tomorrow" when the truth is "this connection was never given a DAY
    window and no amount of waiting will fix it". So ``remaining`` is ``None``
    and the reason says which of the two it is.
    """
    fake = _RecordingStore(refuse="DAY", read={
        "MINUTE": {"used": 2, "ceiling": 30, "remaining": 28,
                   "exhausted_at": None},
    })
    monkeypatch.setattr(ob, "store", fake)
    budget = ob.PgOutboundRateBudget(session=object(),
                                     connection_id=CONNECTION_ID)

    decision = budget.reserve(1, now=NOW)

    assert decision.granted is False
    assert decision.window == "DAY"
    assert decision.remaining is None, (
        "A missing window is unknown, not zero.")
    assert "no DAY row exists" in decision.reason


def test_a_naive_timestamp_is_refused_by_the_budget(monkeypatch):
    """The DAY window is the binding one, so the timezone is not cosmetic.

    A naive ``now`` lands in whichever day the server's local time implies. On
    a machine an hour either side of the boundary that is a different window,
    a different ceiling row, and a reservation charged to the wrong day.
    """
    monkeypatch.setattr(ob, "store", _RecordingStore(grant={}))
    budget = ob.PgOutboundRateBudget(session=object(),
                                     connection_id=CONNECTION_ID)
    with pytest.raises(ob.OutboundError, match="timezone-aware"):
        budget.reserve(1, now=NOW.replace(tzinfo=None))


def test_outbound_writes_no_rate_budget_sql_of_its_own():
    """The property that keeps this fixed: not correct SQL, but NO SQL.

    ``throttle.py`` was consolidated the same way and now contains no SQL at
    all. The reason is stated in ``tests/test_one_rate_budget_implementation.py``
    and it is worth repeating: a statement in a module that cannot see the
    migration will pass every unit test in this repository and fail on the
    first real call. Deleting it is the only durable fix, and this asserts the
    deletion rather than the replacement.
    """
    import ast

    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = " ".join(node.value.split()).upper()
        if "INTEGRATION_RATE_BUDGET" not in text:
            continue
        for verb in ("INSERT INTO", "UPDATE"):
            index = text.find(verb)
            if index == -1:
                continue
            target = text[index + len(verb):].split()[0].strip('"',).rstrip(',;()')
            assert target != "INTEGRATION_RATE_BUDGET", (
                "outbound.py writes integration_rate_budget again. "
                "integration_store.reserve_calls is the one implementation: it "
                "was written by the author of migration 010, supplies every "
                "NOT NULL column and conflicts on the real primary key.")

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
        def create_purchase_order(self, po, dedupe_key):
            raise TimeoutError("gateway timeout")

    adapter = _Broken(tenant=tenant)
    # Each attempt is a LATER tick, clearing the backoff the previous failure
    # scheduled. The claim honours `next_attempt_at` now -- the lease-based one
    # honoured it only for rows in SENDING -- so eight attempts at a single
    # instant would stop claiming after the first and this test would pass for
    # the wrong reason.
    for attempt in range(1, ob.MAX_ATTEMPTS + 1):
        moment = NOW + timedelta(
            seconds=(ob.BACKOFF_CEILING_SECONDS + 60) * attempt)
        with pytest.raises(TimeoutError):
            ob.emit_purchase_order(adapter=adapter, store=store,
                                   outbox_id="OB-1", now=moment)
        row = store.get("OB-1")
        expected = ob.OUTBOX_DEAD if attempt >= ob.MAX_ATTEMPTS else ob.OUTBOX_FAILED
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
                      "/api/v1/purchaseorders", "zohoerp."):
        assert forbidden not in lowered, (
            f"{forbidden!r} appears in outbound.py; product-specific facts "
            "belong behind the C1 adapter (D-14 is unresolved).")

    # NARROWED, on purpose. This list used to include the bare substring
    # `scope=`, which caught the OAuth scope strings it was aimed at
    # (`ERP.purchaseorders.ALL`) and also caught `scope=self.scope` -- the
    # row-level-security Scope that `integration_store.reserve_calls` takes and
    # that every scoped read in this codebase passes. Those are two unrelated
    # things that share a word. Deleting the check would have been the easy
    # move; instead it now names what it actually forbids, which is a Zoho
    # OAuth scope literal, and it is STRICTER than the old substring for that
    # purpose because it matches the shape rather than the assignment.
    import re as _re
    oauth_scopes = _re.findall(
        r"['\"](?:ZohoBooks|ZohoInventory|ERP)\.[A-Za-z_]+\.[A-Z]+['\"]", SOURCE)
    assert not oauth_scopes, (
        f"Zoho OAuth scope literal(s) {oauth_scopes} appear in outbound.py. "
        "The scope a call needs is the adapter's business: D-14 is unresolved "
        "and these strings differ per product.")


def test_outbound_imports_only_the_seams_that_have_landed():
    """**This test used to forbid every one of these imports. That is reversed.**

    The original rule was: the Wave 5 streams run in parallel, no stream edits
    another's files, and ``outbound.py`` duck-types every seam so it compiles
    and its tests run before ``adapter.py``, ``dto.py``, ``throttle.py`` or
    ``integration_store.py`` exist. That was correct while those files were
    moving. It has an expiry date, and the expiry is what this test now checks.

    The cost of keeping it past the expiry was measured, not guessed. All
    THREE defects this stream repaired are direct consequences of the file
    isolation:

    * the outbox state vocabulary drifted to six values, three of which
      ``ck_integration_outbox_state`` rejects, because nothing ever compared
      this module's list to ``integration_store.OUTBOX_STATES``;
    * a second rate-budget implementation grew here, against a conflict target
      that is not the primary key and omitting five NOT NULL columns, because
      nothing here could see migration 010;
    * the outbox payload was handed to the adapter as a ``Mapping`` while both
      shipped adapters read it by attribute, because no type existed on either
      side of the seam for anything to disagree about.

    So the check is inverted rather than deleted: the two landed seams are
    REQUIRED, and everything else is still forbidden. ``dto`` and
    ``integration_store`` are contracts this module must agree with, and an
    import is the cheapest possible enforcement of agreement -- the vocabulary
    assertion in ``outbound.py`` fails at IMPORT if the two lists diverge,
    which no test running later could have done.

    The **adapter** remains forbidden, and that is not inertia. ``erp.py`` and
    ``books_inventory.py`` are product-specific and D-14 is unresolved;
    ``adapter.py``'s ``ProcurementAdapter`` is ``runtime_checkable`` and
    isinstance-checked against both shipped adapters, so importing it would
    make this module's needs part of what "is a procurement adapter" means --
    and an adapter that legitimately cannot resolve a key must still work here.
    That one is a real design boundary rather than a scheduling artefact.

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
    tails = {name.rsplit(".", 1)[-1] for name in imported}

    required = {"dto", "integration_store"}
    missing = required - tails
    assert not missing, (
        f"outbound.py no longer imports {sorted(missing)}. These are not "
        "conveniences: `dto` carries the emission shape the adapters read by "
        "attribute, and `integration_store` carries both the canonical rate "
        "budget and the outbox vocabulary this module asserts against at "
        "import. Duck-typing them back out reopens the exact three defects "
        "listed in this test's docstring.")

    forbidden = {"adapter", "erp", "books_inventory", "throttle", "jobs",
                 "sweeps", "statuses"}
    for name in imported:
        tail = name.rsplit(".", 1)[-1]
        assert tail not in forbidden, (
            f"outbound.py imports {name!r}. The adapter seam stays duck-typed: "
            "an adapter implementing only frozen C1 must still work here, and "
            "the two product modules are behind an unresolved D-14.")


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
    # emission is a RETRY, which is when the missing capability starts to cost.
    #
    # Set up through the store rather than by poking `rows["OB-1"].state`.
    # Poking it left `next_attempt_at` unset, which
    # `ck_integration_outbox_failed_reschedules` forbids -- a retryable failure
    # that scheduled no retry is a row that stops moving and appears on no
    # queue. The fake enforces that constraint now, so the old set-up was
    # building a row PostgreSQL would never have held.
    enqueued(store, outbox_id="OB-2", local_id="PR-0002")
    store.record_attempt_failed("OB-2", error="first attempt timed out",
                                next_attempt_at=NOW + timedelta(seconds=1),
                                terminal=False, now=NOW)
    assert store.get("OB-2").state == ob.OUTBOX_FAILED
    assert store.get("OB-2").attempts == 1
    second = ob.emit_purchase_order(
        adapter=C1OnlyAdapter(tenant=tenant), store=store, outbox_id="OB-2",
        now=NOW + timedelta(seconds=2))
    assert "resolve_by_dedupe_key" in second.note
    assert "Z-01" in second.note
    # Two rows, two requisitions, two purchase orders -- and exactly ONE per
    # dedupe key, which is the property that matters. Counting the tenant's
    # whole population would say nothing about deduplication.
    assert len(tenant.records) == 2
    assert tenant.count_with_capex_ref(first.dedupe_key) == 1
    assert tenant.count_with_capex_ref(second.dedupe_key) == 1
    assert first.dedupe_key != second.dedupe_key


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


# ======================================================================
# The payload <-> DTO mapping. Both directions, and every field validated.
#
# Three shapes are in play and there was no mapping between two of them:
# `PurchaseOrderDraft` (what a caller assembles), the outbox `payload` dict
# (C2's `payload jsonb`, which is what survives a process death), and
# `PurchaseOrderEmissionDTO` (what the adapter reads by attribute). The middle
# one was being handed straight to the adapter.
#
# These tests are about the failure mode that is WORSE than the AttributeError:
# a mapping that runs and produces the wrong number. `PoLine.amount_paise` is a
# LINE TOTAL and `LineDTO` has a separate field for the unit rate; swapping
# them multiplies a commitment by its quantity, on a document a vendor then
# invoices against, with nothing anywhere raising.
# ======================================================================

def _payload(**overrides):
    """A well-formed outbox payload, with fields overridden for the bad cases."""
    from outbound_tenant_fake import draft
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    payload = draft("PR-0001").as_payload(key)
    payload.update(overrides)
    return payload, key


def _map(payload, key):
    return ob.emission_dto(payload=payload, connection_id=CONNECTION_ID,
                           module=MODULE, local_id="PR-0001", dedupe_key=key)


def test_the_mapping_round_trips_through_the_json_the_outbox_actually_stores():
    """Draft -> DTO must equal Draft -> payload -> stored JSON -> DTO.

    The middle shape is a real narrowing: ``payload jsonb`` has no dates, no
    tuples and no Python types, so anything the draft knows that JSON cannot
    carry is lost between the enqueue and the drain. Serialising through
    ``json`` rather than passing the dict along is what makes this a test of
    the storage rather than of two functions in the same process.
    """
    import json
    from outbound_tenant_fake import draft

    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    po = draft("PR-0001")

    direct = po.as_emission_dto(key, module=MODULE)
    stored = json.loads(json.dumps(po.as_payload(key)))
    rebuilt = _map(stored, key)

    assert direct == rebuilt, (
        "A field is being lost in `payload jsonb`. The fields at risk are "
        "monetary, and a purchase order is a commitment.")
    assert direct.total_paise == po.total_paise
    assert [ln.line_total_paise for ln in direct.lines] == [
        line.amount_paise for line in po.lines]
    assert [ln.unit_price_paise for ln in direct.lines] == [
        line.unit_price_paise for line in po.lines]


def test_the_line_total_maps_to_the_line_total_and_not_to_the_rate():
    """The money bug a previous stream refused to risk, pinned down.

    Three units at 100.00 each: the line total is 300.00 and the rate is
    100.00. A mapper that put ``amount_paise`` into ``unit_price_paise`` would
    emit a rate of 300.00 on a quantity of 3 -- a purchase order committing
    900.00 where the budget check cleared 300.00. It would create exactly one
    purchase order and satisfy every duplicate assertion in the chaos test.
    """
    from outbound_tenant_fake import draft

    po = draft("PR-0001", lines=(line("L1", amount_paise=300_00, quantity=3),))
    document = po.as_emission_dto("CAPEX-K", module=MODULE)

    (mapped,) = document.lines
    assert mapped.unit_price_paise == 100_00, "the RATE"
    assert mapped.line_total_paise == 300_00, "the LINE TOTAL"
    assert mapped.quantity == "3"
    assert document.total_paise == 300_00
    assert mapped.unit_price_paise * int(mapped.quantity) == mapped.line_total_paise


def test_the_header_total_is_re_summed_from_the_lines_and_not_trusted():
    """Checked at the boundary, not in a test.

    A test proves the mapper is right today. This refuses the document on the
    day it stops being -- which is the only version of this check that is worth
    anything, because the wrong number would otherwise reach a vendor.

    There are TWO identities and they are independent, so each is provoked on
    its own: ``subtotal == sum(lines)`` and ``subtotal + tax == total``. A
    payload that moves the header total alone breaks only the second; one that
    moves subtotal and total together breaks only the first. Checking them with
    a single mutation would leave whichever fired second unproven.
    """
    # subtotal + tax != total. The lines still sum correctly.
    payload, key = _payload()
    payload["total_paise"] = payload["total_paise"] + 1
    with pytest.raises(DtoError, match="but subtotal"):
        _map(payload, key)

    # subtotal + tax == total, but neither matches what the lines actually add
    # up to -- the case a header computed independently of its lines produces.
    payload, key = _payload()
    payload["subtotal_paise"] = payload["subtotal_paise"] + 1
    payload["total_paise"] = payload["total_paise"] + 1
    with pytest.raises(DtoError, match="lines sum to"):
        _map(payload, key)


@pytest.mark.parametrize("field_name", [
    "total_paise", "subtotal_paise", "tax_paise",
])
@pytest.mark.parametrize("bad", [2500.00, "250000", None, True, -1])
def test_every_monetary_header_field_is_validated(field_name, bad):
    """No float, no string, no Decimal, no bool, no negative -- refused.

    ``2500.00`` is exactly representable and still refused: accepting it lets a
    float travel into a commitment, and the next one will not be exact. ``True``
    is refused explicitly because ``isinstance(True, int)`` is ``True`` in
    Python and it would otherwise be read as one paisa.
    """
    payload, key = _payload(**{field_name: bad})
    with pytest.raises((MoneyError, DtoError)):
        _map(payload, key)


@pytest.mark.parametrize("field_name", ["unit_price_paise", "amount_paise"])
@pytest.mark.parametrize("bad", [2500.00, "250000", None, True, -1])
def test_every_monetary_line_field_is_validated(field_name, bad):
    payload, key = _payload()
    payload["lines"][0][field_name] = bad
    with pytest.raises((MoneyError, DtoError)):
        _map(payload, key)


@pytest.mark.parametrize("field_name", ["vendor_external_id", "currency_code"])
@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_every_identifier_field_is_validated(field_name, bad):
    """A missing identifier makes the commitment unreconcilable, not just odd."""
    payload, key = _payload(**{field_name: bad})
    with pytest.raises((ob.EmissionShapeError, DtoError)):
        _map(payload, key)


def test_a_timestamp_where_a_date_belongs_is_refused_rather_than_truncated():
    """``date.fromisoformat`` would accept it and silently drop the time.

    That tolerance is wrong here. A payload carrying a timestamp was written by
    something that did not know this field is a date, and mapping it anyway
    hides that -- while the adapter sends it into a Zoho DATE field.
    """
    payload, key = _payload(document_date="2026-09-06T10:00:00+00:00")
    with pytest.raises(ob.EmissionShapeError, match="ISO date"):
        _map(payload, key)

    # And the DTO refuses a `datetime` object too, which is the same mistake
    # arriving by the other route. `datetime` subclasses `date`, so the order
    # of the isinstance checks is the check.
    with pytest.raises(DtoError, match="datetime.date"):
        PurchaseOrderEmissionDTO(
            origin=EmissionRef(connection_id=CONNECTION_ID, module=MODULE,
                               local_id="PR-1", dedupe_key="CAPEX-K"),
            vendor_external_id="ZV-77", document_date=NOW,
            currency_code="INR", lines=_map(*_payload()).lines,
            subtotal_paise=250_000_00, tax_paise=0, total_paise=250_000_00)


@pytest.mark.parametrize("field_name,wrong", [
    ("local_id", "PR-9999"),
    ("connection_id", "CONN-SOMEONE-ELSE"),
    (ob.CF_CAPEX_REF, "CAPEX-A-DIFFERENT-KEY"),
])
def test_a_payload_that_disagrees_with_its_own_outbox_row_is_refused(field_name,
                                                                    wrong):
    """Neither side wins, because picking one would be a guess about money.

    The four identifiers are the outbox row's own columns and are passed in
    rather than read out of the payload. A payload whose ``local_id`` or
    ``cf_capex_ref`` disagrees with the row carrying it was written for a
    different document: emitting it attaches one requisition's amount to
    another requisition's dedupe key, which is a duplicate and a mis-link in
    the same call.
    """
    payload, key = _payload(**{field_name: wrong})
    with pytest.raises(ob.EmissionShapeError, match="outbox row"):
        _map(payload, key)


def test_a_payload_from_an_older_shape_of_this_module_is_refused_not_guessed():
    """An outbox row can outlive the code that wrote it by days.

    It is enqueued inside the business transaction and drained by a later cron
    tick, so the mapper will one day be handed a payload written by a previous
    version of `as_payload`. Refusing it puts the row on SCR-39 as a defect;
    mapping it would be a guess, and the fields it would be guessing about are
    monetary.
    """
    payload, key = _payload(payload_version=0)
    with pytest.raises(ob.EmissionShapeError, match="payload_version"):
        _map(payload, key)

    del payload["payload_version"]
    with pytest.raises(ob.EmissionShapeError, match="payload_version"):
        _map(payload, key)


def test_an_unmappable_payload_does_not_burn_eight_attempts_to_learn_it():
    """A malformed row is a defect, not a transient fault.

    Retrying it would spend eight of a 2,000-call daily ceiling reaching the
    same conclusion, and would spend them against a tenant that never sees a
    request -- the mapping fails before any call is made. So the row is
    quarantined on the first attempt, with the mapping error recorded.
    """
    from outbound_tenant_fake import draft

    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    broken = draft("PR-0001").as_payload(key)
    broken["lines"][0]["amount_paise"] = "250000"       # a string, from nowhere
    store.enqueue(outbox_id="OB-1", connection_id=CONNECTION_ID, module=MODULE,
                  local_id="PR-0001", payload=broken, dedupe_key=key)
    adapter = FakeAdapter(tenant=tenant)

    with pytest.raises(MoneyError):
        ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                               now=NOW)

    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_DEAD, "Quarantined, not queued for seven more."
    assert "OUTBOX_PAYLOAD_UNMAPPABLE" in row.last_error
    assert adapter.calls == [], "No call was made; there was nothing to send."
    assert tenant.records == {}
