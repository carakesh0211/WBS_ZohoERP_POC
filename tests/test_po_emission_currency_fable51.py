"""A foreign-currency purchase order is emitted in its own currency, at the
vendor's own figures -- or refused, never relabelled INR (Fable 5.1).

`PurchaseOrderEmissionDTO.currency_code` defaulted to "INR" and nothing in
the emission path read the purchase order's own `currency`, so a USD order
reached the vendor labelled INR and priced in INR paise. Step 1 made the
boundary carry the currency and render at its exponent; step 2 (migration
029) stores `source_amount_minor` / `source_rate_minor` on `po_line` and
`plan_po_emission` emits those for a foreign order. A foreign order whose
lines carry no source figures -- written before 029, or by a path that
bypassed the writer -- is refused per line with `PO_CURRENCY_SOURCE_MISSING`
before planning, before an outbox row. Database-free: the header and line
readers are stubbed.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.pg import procurement_services as ps  # noqa: E402


class _Session:
    pass


def _legacy_row(po_line_id="L1", **over):
    row = {"po_line_id": po_line_id, "line_no": 1, "wbs_id": "W", "budget_head_id": "H",
           "description": "pump", "quantity": 1, "rate_paise": 1_00_000_00,
           "amount_paise": 1_00_000_00, "tax_paise": 0, "non_creditable_tax_paise": 0,
           "freight_paise": 0, "line_external_id": None,
           "source_rate_minor": None, "source_amount_minor": None}
    row.update(over)
    return row


class _PlainAdapter:
    def capabilities(self):
        return _Caps()


@pytest.mark.parametrize("currency", ["USD", "usd", "EUR", "JPY", "KWD"])
def test_a_foreign_order_without_source_figures_is_refused_before_planning(monkeypatch, currency):
    """The pre-029 shape: a foreign header over lines that hold base paise
    only. Refused per line, before `build_emission_plan`, before an outbox
    row -- never emitted at paise divided back by the rate."""
    monkeypatch.setattr(ps, "_po_header", lambda session, po_id: {
        "po_id": po_id, "po_number": "PO-2026-0001", "project_id": "P", "pr_id": "R",
        "vendor_name": "V", "currency": currency, "status": "Approved", "version_no": 1})
    monkeypatch.setattr(ps, "po_lines", lambda session, po_id: [_legacy_row()])
    monkeypatch.setattr(ps, "fractional_quantity_policy", lambda session: ps.POLICY_REFUSE)
    reached = []
    monkeypatch.setattr(ps.ob, "plan_emission", lambda **kw: reached.append("plan"))
    with pytest.raises(ps.ProcurementError) as exc:
        ps.plan_po_emission(_Session(), po_id="PO-1", connection_id="C", adapter=_PlainAdapter(),
                            vendor_external_id="VX", document_date=date(2026, 9, 1), actor="U")
    assert exc.value.code == "PO_CURRENCY_SOURCE_MISSING" and exc.value.status == 409
    assert currency.upper() in exc.value.message
    assert reached == [], "planning happened before the refusal"


@pytest.mark.parametrize("currency, unit, total, quantity", [
    ("USD", 1999, 5997, 3), ("JPY", 1234, 2468, 2), ("KWD", 1234, 1234, 1)])
def test_a_foreign_order_is_planned_at_its_source_figures_not_the_translated_paise(
        currency, unit, total, quantity):
    """The 029 shape. The emitted unit price and line total are the SOURCE
    figures; the translated `rate_paise` / `amount_paise` are not sent."""
    row = _legacy_row(quantity=quantity, rate_paise=83_00 * unit, amount_paise=83_00 * total,
                      source_rate_minor=unit, source_amount_minor=total)
    plan, planned = ps.build_emission_plan(
        po_id="PO-1", po_number="PO-2026-0001", connection_id="C",
        vendor_external_id="ZV-1", document_date=date(2026, 9, 1),
        capabilities=_Caps(), line_rows=[row], acknowledged=True,
        currency_code=currency)
    line = plan.drafts[0].lines[0]
    assert (line.unit_price_paise, line.amount_paise) == (unit, total)
    assert plan.drafts[0].currency_code == currency
    payload = planned[0]["payload"]
    assert payload["currency_code"] == currency
    assert payload["lines"][0]["unit_price_paise"] == unit
    assert payload["subtotal_paise"] == payload["total_paise"] == total
    dto = ob.emission_dto(payload=payload, connection_id="C", module="purchaseorders",
                          local_id=planned[0]["local_id"], dedupe_key=planned[0]["dedupe_key"])
    assert dto.currency_code == currency and dto.total_paise == total


def test_a_base_order_still_emits_rate_paise_and_ignores_null_source_columns():
    row = _legacy_row(quantity=2, rate_paise=500, amount_paise=1000)
    plan, planned = ps.build_emission_plan(
        po_id="PO-1", po_number="PO-2026-0001", connection_id="C",
        vendor_external_id="ZV-1", document_date=date(2026, 9, 1),
        capabilities=_Caps(), line_rows=[row], acknowledged=True)
    line = plan.drafts[0].lines[0]
    assert (line.unit_price_paise, line.amount_paise) == (500, 1000)
    assert planned[0]["payload"]["currency_code"] == "INR"


def test_a_base_currency_purchase_order_proceeds_to_the_line_read(monkeypatch):
    monkeypatch.setattr(ps, "_po_header", lambda session, po_id: {
        "po_id": po_id, "po_number": "PO-2026-0002", "project_id": "P", "pr_id": "R",
        "vendor_name": "V", "currency": "INR", "status": "Approved", "version_no": 1})
    monkeypatch.setattr(ps, "po_lines", lambda session, po_id: [])
    with pytest.raises(ps.ProcurementError) as exc:
        ps.plan_po_emission(_Session(), po_id="PO-2", connection_id="C", adapter=None,
                            vendor_external_id="VX", document_date=date(2026, 9, 1), actor="U")
    assert exc.value.code == "NO_LINES", "an INR purchase order must pass the currency gate"


def test_a_missing_currency_is_treated_as_base_not_refused(monkeypatch):
    monkeypatch.setattr(ps, "_po_header", lambda session, po_id: {
        "po_id": po_id, "po_number": "PO-2026-0003", "project_id": "P", "pr_id": "R",
        "vendor_name": "V", "currency": None, "status": "Approved", "version_no": 1})
    monkeypatch.setattr(ps, "po_lines", lambda session, po_id: [])
    with pytest.raises(ps.ProcurementError) as exc:
        ps.plan_po_emission(_Session(), po_id="PO-3", connection_id="C", adapter=None,
                            vendor_external_id="VX", document_date=date(2026, 9, 1), actor="U")
    assert exc.value.code == "NO_LINES"


# ===================================================================== step 1
# The boundary: the draft carries the order's currency and each adapter
# renders a wire amount at THAT currency's minor exponent.
from datetime import datetime, timezone  # noqa: E402

from app.backend.integration import books_inventory, erp  # noqa: E402
from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.integration.dto import EmissionRef, LineDTO, PurchaseOrderEmissionDTO  # noqa: E402

RENDERERS = {"erp": erp.render_paise, "books_inventory": books_inventory.render_paise}
BODIES = {"erp": erp._emission_body, "books_inventory": books_inventory._emission_body}


class _Caps:
    line_level_custom_fields = False
    daily_call_ceiling = 2000


class _LineLevelCaps(_Caps):
    line_level_custom_fields = True


def _row(po_line_id, *, wbs="WBS-A", head="BH-CIVIL", quantity=1, amount=250_000_00,
         source=None):
    """A `po_lines()` row. `source` = the line total in the order's own minor
    units for a foreign order (the translated paise stay in `amount`)."""
    return {"po_line_id": po_line_id, "line_no": 1, "wbs_id": wbs,
            "budget_head_id": head, "description": f"line {po_line_id}",
            "quantity": quantity, "rate_paise": amount // quantity,
            "amount_paise": amount, "tax_paise": 0,
            "non_creditable_tax_paise": 0, "freight_paise": 0,
            "line_external_id": None,
            "source_rate_minor": None if source is None else source // quantity,
            "source_amount_minor": source}


@pytest.mark.parametrize("product", sorted(RENDERERS))
@pytest.mark.parametrize("minor, exponent, wire", [
    (1234, 0, "1234"),        # JPY: no minor unit, no decimal point
    (-5, 0, "-5"),
    (1234, 3, "1.234"),       # KWD: three decimals
    (-1, 3, "-0.001"),
    (-150, 2, "-1.50"),       # the measured INR table is unchanged
    (100000, 2, "1000.00"),
    (7, 2, "0.07"),
])
def test_a_wire_amount_is_rendered_at_the_currency_exponent(product, minor, exponent, wire):
    assert RENDERERS[product](minor, minor_exponent=exponent) == wire


@pytest.mark.parametrize("minor, exponent", [(1234, 0), (1234, 3), (-1, 3), (-150, 2), (99, 2)])
def test_both_adapters_render_every_exponent_identically(minor, exponent):
    assert erp.render_paise(minor, minor_exponent=exponent) == \
        books_inventory.render_paise(minor, minor_exponent=exponent)


@pytest.mark.parametrize("product", sorted(RENDERERS))
@pytest.mark.parametrize("bad", [-1, 5, True])
def test_an_exponent_outside_iso_4217_is_refused_not_guessed(product, bad):
    with pytest.raises(ValueError):
        RENDERERS[product](100, minor_exponent=bad)


@pytest.mark.parametrize("product", sorted(RENDERERS))
def test_the_default_exponent_is_still_two_so_every_inr_caller_is_unchanged(product):
    assert RENDERERS[product](-150) == "-1.50"
    assert RENDERERS[product](25_000_00) == "25000.00"


def _dto(currency, unit, quantity, total):
    return PurchaseOrderEmissionDTO(
        origin=EmissionRef(connection_id="C", module="purchaseorders",
                           local_id="PO-1", dedupe_key="K"),
        vendor_external_id="ZV-1", document_date=date(2026, 9, 1),
        currency_code=currency,
        lines=(LineDTO(external_line_id=None, line_number=1, description="pump",
                       quantity=str(quantity), unit_price_paise=unit,
                       line_total_paise=total, tax_paise=0),),
        subtotal_paise=total, tax_paise=0, total_paise=total)


@pytest.mark.parametrize("product", sorted(BODIES))
@pytest.mark.parametrize("currency, unit, quantity, total, wire", [
    ("JPY", 1234, 3, 3702, "1234"),
    ("KWD", 1234, 2, 2468, "1.234"),
    ("USD", 1999, 1, 1999, "19.99"),
    ("INR", 25_000_00, 2, 50_000_00, "25000.00"),
])
def test_the_wire_body_prices_the_line_in_the_orders_own_currency(
        product, currency, unit, quantity, total, wire):
    """Through the real DTO and the real body builder: the currency is
    labelled AND the rate is rendered at that currency's exponent. Before, a
    JPY 1,234 unit price went out as 12.34."""
    body = BODIES[product](_dto(currency, unit, quantity, total), "K")
    assert body["currency_code"] == currency
    assert body["line_items"][0]["rate"] == wire


@pytest.mark.parametrize("caps", [_Caps(), _LineLevelCaps()], ids=["split", "line-level"])
def test_the_plan_stamps_the_orders_currency_on_every_draft_and_payload(caps):
    rows = [_row("L1", wbs="WBS-A", amount=165_917, source=1999),
            _row("L2", wbs="WBS-B", amount=207_500, source=2500)]
    plan, planned = ps.build_emission_plan(
        po_id="PO-1", po_number="PO-2026-0009", connection_id="C",
        vendor_external_id="ZV-1", document_date=date(2026, 9, 1),
        capabilities=caps, line_rows=rows, acknowledged=True, currency_code="USD")
    assert plan.drafts, "a plan with no draft emits nothing"
    for draft in plan.drafts:
        assert draft.currency_code == "USD"
        dto = draft.as_emission_dto("K", module="purchaseorders")
        assert dto.currency_code == "USD"
    for row in planned:
        assert row["payload"]["currency_code"] == "USD"
        rebuilt = ob.emission_dto(payload=row["payload"], connection_id="C",
                                  module="purchaseorders", local_id=row["local_id"],
                                  dedupe_key=row["dedupe_key"])
        assert rebuilt.currency_code == "USD"
        assert erp._emission_body(rebuilt, row["dedupe_key"])["currency_code"] == "USD"


def test_the_plan_defaults_to_the_base_currency():
    plan, planned = ps.build_emission_plan(
        po_id="PO-1", po_number="PO-2026-0009", connection_id="C",
        vendor_external_id="ZV-1", document_date=date(2026, 9, 1),
        capabilities=_Caps(), line_rows=[_row("L1")], acknowledged=True)
    assert {d.currency_code for d in plan.drafts} == {"INR"}
    assert planned[0]["payload"]["currency_code"] == "INR"


@pytest.mark.parametrize("bad", ["usd", "US", "", "INR ", 840])
def test_a_draft_with_a_malformed_currency_is_refused_before_any_outbox_row(bad):
    line = ob.PoLine(line_id="L1", cell=ob.ControlCell(wbs_id="W", budget_head_id="H"),
                     description="pump", quantity=1, unit_price_paise=100, amount_paise=100)
    with pytest.raises(ob.EmissionShapeError):
        ob.PurchaseOrderDraft(local_id="PO-1", connection_id="C", vendor_external_id="ZV-1",
                              lines=(line,), document_date=date(2026, 9, 1), currency_code=bad)


# ===================================================================== step 2
# The line shape rules, database-free: `_normalise_lines(foreign=True)`.
# A mutation that dropped the base-paise refusal on a foreign line survived
# every suite that runs without PostgreSQL; these hold the rules here too.
def _cell(**over):
    return {"wbs_id": "W", "budget_head_id": "H", "quantity": 1, **over}


def test_a_foreign_line_carries_source_minor_units_and_never_base_paise():
    out = ps._normalise_lines([_cell(source_amount_minor=5997, quantity=3)],
                              what="purchase order", foreign=True)
    assert out[0]["source_amount_minor"] == 5997 and out[0]["amount_paise"] is None
    assert out[0]["source_rate_minor"] is None, "the rate is derived later, exactly, or refused"
    for bad, code in ((_cell(source_amount_minor=5997, amount_paise=1), "PO_BASE_AMOUNT_ON_FOREIGN_ORDER"),
                      (_cell(source_amount_minor=5997, rate_paise=1), "PO_BASE_AMOUNT_ON_FOREIGN_ORDER"),
                      (_cell(), "PO_SOURCE_AMOUNT_REQUIRED"),
                      (_cell(source_amount_minor=-1), "NEGATIVE_LINE_AMOUNT"),
                      (_cell(source_amount_minor=10, source_rate_minor=-1), "NEGATIVE_RATE"),
                      (_cell(source_amount_minor=10.5), "NON_INTEGER_MONEY")):
        with pytest.raises(ps.ProcurementError) as exc:
            ps._normalise_lines([bad], what="purchase order", foreign=True)
        assert exc.value.code == code, (bad, exc.value.code)


def test_a_base_line_carries_paise_and_never_source_figures():
    out = ps._normalise_lines([_cell(amount_paise=100)], what="purchase order")
    assert out[0]["amount_paise"] == 100 and out[0]["source_amount_minor"] is None
    for bad, code in ((_cell(amount_paise=100, source_amount_minor=1), "PO_SOURCE_AMOUNT_ON_BASE_ORDER"),
                      (_cell(amount_paise=100, source_rate_minor=1), "PO_SOURCE_AMOUNT_ON_BASE_ORDER"),
                      (_cell(), "LINE_AMOUNT_REQUIRED")):
        with pytest.raises(ps.ProcurementError) as exc:
            ps._normalise_lines([bad], what="purchase order")
        assert exc.value.code == code, (bad, exc.value.code)


def test_the_currency_code_is_normalised_or_refused():
    assert ps._po_currency(" usd ") == "USD" and ps._po_currency(None) == "INR"
    for bad in ("US", "USDX", "12A", ""):
        if bad == "":
            assert ps._po_currency(bad) == "INR"  # empty means the base currency
            continue
        with pytest.raises(ps.ProcurementError) as exc:
            ps._po_currency(bad)
        assert exc.value.code == "PO_CURRENCY_INVALID"


def test_translation_applies_the_rate_once_and_the_lines_sum_to_the_header():
    from decimal import Decimal
    from app.backend.pg import fx
    basis = fx.TranslationBasis(source_currency="USD", minor_exponent=2, rate=Decimal("83.25"),
                                rate_date=date(2026, 8, 5), rate_source="T", fx_rate_id="FXR-1")
    lines = ps._normalise_lines([_cell(source_amount_minor=5997, quantity=3),
                                 _cell(source_amount_minor=701)],
                                what="purchase order", foreign=True)
    out = ps._translate_po_lines(basis, lines)
    assert [l["source_rate_minor"] for l in out] == [1999, 701]
    header = fx.translate_to_base_paise(6698, Decimal("83.25"), source_minor_exponent=2)
    assert sum(l["amount_paise"] for l in out) == header == 557_609
    with pytest.raises(ps.ProcurementError) as exc:
        ps._translate_po_lines(basis, ps._normalise_lines([_cell(source_amount_minor=10, quantity=3)],
                                                          what="purchase order", foreign=True))
    assert exc.value.code == "PO_LINE_RATE_NOT_EXACT"
    identity = ps._translate_po_lines(fx.identity_basis(), ps._normalise_lines([_cell(amount_paise=100)],
                                                                                what="purchase order"))
    assert identity[0]["amount_paise"] == 100
