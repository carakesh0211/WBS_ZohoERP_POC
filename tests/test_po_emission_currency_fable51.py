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
