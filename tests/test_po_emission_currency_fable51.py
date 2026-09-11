"""A foreign-currency purchase order is refused at emission, not relabelled INR
(Fable 5.1).

`PurchaseOrderEmissionDTO.currency_code` defaults to "INR" and nothing in the
emission path read the purchase order's own `currency`, so a USD purchase
order reached the vendor labelled INR and priced in INR paise. `po_line`
stores base paise only; the source-currency line amounts the boundary needs
do not exist yet, and dividing base paise by the header rate would invent
them. So `plan_po_emission` now refuses a non-base purchase order BEFORE any
planning or outbox row, with a coded 409. Database-free: the header and line
readers are stubbed, and the assertion is that planning is never reached.
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


@pytest.mark.parametrize("currency", ["USD", "usd", "EUR", "JPY", "KWD"])
def test_a_non_base_purchase_order_is_refused_before_planning(monkeypatch, currency):
    monkeypatch.setattr(ps, "_po_header", lambda session, po_id: {
        "po_id": po_id, "po_number": "PO-2026-0001", "project_id": "P", "pr_id": "R",
        "vendor_name": "V", "currency": currency, "status": "Approved", "version_no": 1})
    reached = []
    monkeypatch.setattr(ps, "po_lines", lambda session, po_id: reached.append("lines") or [])
    monkeypatch.setattr(ps, "build_emission_plan", lambda **kw: reached.append("plan"))
    with pytest.raises(ps.ProcurementError) as exc:
        ps.plan_po_emission(_Session(), po_id="PO-1", connection_id="C", adapter=None,
                            vendor_external_id="VX", document_date=date(2026, 9, 1), actor="U")
    assert exc.value.code == "PO_CURRENCY_NOT_EMITTABLE" and exc.value.status == 409
    assert currency.upper() in exc.value.message and "INR" in exc.value.message
    assert reached == [], "planning or line reading happened before the refusal"


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


def _row(po_line_id, *, wbs="WBS-A", head="BH-CIVIL", quantity=1, amount=250_000_00):
    return {"po_line_id": po_line_id, "line_no": 1, "wbs_id": wbs,
            "budget_head_id": head, "description": f"line {po_line_id}",
            "quantity": quantity, "rate_paise": amount // quantity,
            "amount_paise": amount, "tax_paise": 0,
            "non_creditable_tax_paise": 0, "freight_paise": 0,
            "line_external_id": None}


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
    rows = [_row("L1", wbs="WBS-A", amount=1999), _row("L2", wbs="WBS-B", amount=2500)]
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
