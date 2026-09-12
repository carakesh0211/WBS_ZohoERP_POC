"""Application-originated purchase-order emission (Fable 5.1, 2026-09-12):
the minimum-scope write rule, the wire body, the payload round trip, the
two operator routes' refusals, and an honest /api/health. Database-free;
the PostgreSQL-backed half is `test_pg_outbound_drain_fable51.py`.

Held here:
* a POST is satisfied by `ERP.<module>.CREATE` alone, a PUT by `.UPDATE`
  alone, either by `.ALL`; a READ grant still satisfies no write and a
  CREATE grant satisfies no update;
* the create names ERP.purchaseorders.CREATE and the update names
  ERP.purchaseorders.UPDATE, so the credential can be staged at exactly
  what the demonstration needs;
* the wire body carries reference_number, the tenant item id only for a
  line that was given one, and the two verified line custom fields by
  api_name only for a tenant whose line fields are verified;
* PoLine -> payload -> LineDTO keeps item_external_id, wbs_code and
  budget_head through `integration_outbox.payload`;
* `/mode` refuses LIVE_WRITE while the gate is shut and `/drain-outbox`
  refuses before any lookup while the gate is shut, and refuses a LIVE_READ
  connection with the gate open;
* `/api/health` reports the strongest mode an active connection holds and
  falls back to the mock constant with no database.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import main, zoho  # noqa: E402
from app.backend.api import health as health_api  # noqa: E402
from app.backend.api import integrations as integrations_api  # noqa: E402
from app.backend.api import integrations_live  # noqa: E402
from app.backend.integration import adapter as ad  # noqa: E402
from app.backend.integration import erp  # noqa: E402
from app.backend.integration import live_transport as lt  # noqa: E402
from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.integration.dto import LineDTO  # noqa: E402
from tests.test_live_transport_fable51 import BASE, FakeOpener, _credentials  # noqa: E402

PINNED = "60074128927"


def _transport(tmp_path, opener, *, scope):
    return lt.LiveTransport(_credentials(tmp_path, scope=scope), opener=opener)


# ============================================================ 1. scope rule
def test_a_post_is_satisfied_by_the_create_scope_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    opener = FakeOpener(api_answers=[{"code": 0, "purchaseorder": {"purchaseorder_id": "9"}}])
    t = _transport(tmp_path, opener, scope="ERP.purchaseorders.CREATE")
    out = t.request(method="POST", base_url=BASE, path="/purchaseorders",
                    scope="ERP.purchaseorders.CREATE", body={"vendor_id": "1"})
    assert out["purchaseorder"]["purchaseorder_id"] == "9"
    # ...and by the module's ALL when the adapter names ALL on the write.
    t2 = _transport(tmp_path, FakeOpener(api_answers=[{"code": 0}]),
                    scope="ERP.purchaseorders.ALL")
    t2.request(method="POST", base_url=BASE, path="/purchaseorders",
               scope="ERP.purchaseorders.CREATE", body={})


def test_a_create_grant_satisfies_no_update_no_read_and_no_other_module(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    t = _transport(tmp_path, FakeOpener(), scope="ERP.purchaseorders.CREATE")
    for method, scope in (("PUT", "ERP.purchaseorders.UPDATE"),
                          ("PUT", "ERP.purchaseorders.ALL"),
                          ("DELETE", "ERP.purchaseorders.ALL"),
                          ("GET", "ERP.purchaseorders.READ"),
                          ("POST", "ERP.bills.CREATE")):
        with pytest.raises(ad.CapabilityError):
            t.request(method=method, base_url=BASE, path="/purchaseorders/1",
                      scope=scope, body={})
    assert t.describe()["calls_made"] == 0


def test_a_read_grant_still_satisfies_no_write_under_any_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    t = _transport(tmp_path, FakeOpener(), scope="ERP.purchaseorders.READ")
    for scope in ("ERP.purchaseorders.CREATE", "ERP.purchaseorders.ALL",
                  "ERP.purchaseorders.READ"):
        with pytest.raises(ad.CapabilityError):
            t.request(method="POST", base_url=BASE, path="/purchaseorders",
                      scope=scope, body={})


def test_the_gate_is_checked_before_the_scope(tmp_path, monkeypatch):
    """Shut gate: refused as ERP_WRITES_DISABLED even with the exact scope."""
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    t = _transport(tmp_path, FakeOpener(), scope="ERP.purchaseorders.CREATE")
    with pytest.raises(ad.NetworkForbidden, match="ERP_WRITES_DISABLED"):
        t.request(method="POST", base_url=BASE, path="/purchaseorders",
                  scope="ERP.purchaseorders.CREATE", body={})


# ============================================================ 2. wire body
def _line(**over) -> LineDTO:
    base = dict(external_line_id=None, line_number=1, description="Structural steel",
                quantity="10", unit_price_paise=6_800_000, line_total_paise=68_000_000,
                tax_paise=0, item_external_id=None, purchase_order_line_external_id=None,
                dimensions={"wbs_id": "WBS-A-CIVIL", "budget_head_id": "BH-DM1-CIVIL"})
    base.update(over)
    return LineDTO(**base)


class _Po:
    vendor_external_id = "3912780000000074001"
    document_date = date(2026, 9, 12)
    currency_code = "INR"
    reference = "WBS-UAT-OUTBOUND-20260912"

    def __init__(self, lines):
        self.lines = tuple(lines)


def test_the_body_carries_the_reference_and_only_the_items_and_fields_it_was_given():
    plain = _line()
    given = _line(line_number=2, item_external_id="3912780000000080001",
                  dimensions={"wbs_id": "WBS-A-CIVIL", "budget_head_id": "BH-DM1-CIVIL",
                              "wbs_code": "CAPEX-DEMO-001.02", "budget_head": "Civil"})
    body = erp._emission_body(_Po([plain, given]), "CAPEX-K",
                              line_custom_fields=("cf_wbs_code", "cf_budget_head"))
    assert body["reference_number"] == "WBS-UAT-OUTBOUND-20260912"
    assert body["custom_fields"] == [{"api_name": "cf_capex_ref", "value": "CAPEX-K"}]
    first, second = body["line_items"]
    assert "item_id" not in first and "item_custom_fields" not in first
    assert first["rate"] == "68000.00" and first["quantity"] == "10"
    assert second["item_id"] == "3912780000000080001"
    assert second["item_custom_fields"] == [
        {"api_name": "cf_wbs_code", "value": "CAPEX-DEMO-001.02"},
        {"api_name": "cf_budget_head", "value": "Civil"}]


def test_no_line_custom_fields_leave_for_a_tenant_without_them():
    given = _line(dimensions={"wbs_code": "X", "budget_head": "Y"})
    body = erp._emission_body(_Po([given]), "K")   # no line_custom_fields
    assert "item_custom_fields" not in body["line_items"][0]
    assert "reference_number" in body


def test_the_adapter_names_the_verb_scopes_and_its_tenants_line_fields():
    class Recording:
        product = "ERP"

        def __init__(self):
            self.calls = []

        def request(self, **kw):
            self.calls.append(kw)
            return {"purchaseorder": {"purchaseorder_id": "3912780000000123456"}}

    rec = Recording()
    adapter = erp.ErpAdapter(organization_id=PINNED, dc="IN", transport=rec)
    assert adapter.line_custom_field_names() == ("cf_wbs_code", "cf_budget_head")
    line = _line(item_external_id="3912780000000080001",
                 dimensions={"wbs_code": "CAPEX-DEMO-001.02", "budget_head": "Civil"})
    created = adapter.create_purchase_order(_Po([line]), "CAPEX-K")
    assert created == "3912780000000123456"
    call = rec.calls[-1]
    assert call["method"] == "POST" and call["scope"] == "ERP.purchaseorders.CREATE"
    assert call["body"]["line_items"][0]["item_custom_fields"][0]["api_name"] == "cf_wbs_code"
    adapter.update_purchase_order("3912780000000123456", _Po([line]), "CAPEX-K")
    assert rec.calls[-1]["method"] == "PUT"
    assert rec.calls[-1]["scope"] == "ERP.purchaseorders.UPDATE"
    other = erp.ErpAdapter(organization_id="1", dc="IN", transport=rec)
    assert other.line_custom_field_names() == ()


# ===================================================== 3. payload round trip
def _draft(**line_over) -> ob.PurchaseOrderDraft:
    line = ob.PoLine(line_id="POL-1", cell=ob.ControlCell("WBS-A-CIVIL", "BH-DM1-CIVIL"),
                     description="Structural steel", quantity=10,
                     unit_price_paise=6_800_000, amount_paise=68_000_000, **line_over)
    return ob.PurchaseOrderDraft(local_id="PO-1", connection_id="CONN-1",
                                 vendor_external_id="V-1", lines=(line,),
                                 document_date=date(2026, 9, 12),
                                 reference="WBS-UAT-OUTBOUND-20260912")


def test_item_and_codes_survive_the_payload_and_reach_the_dto():
    draft = _draft(item_external_id="3912780000000080001",
                   wbs_code="CAPEX-DEMO-001.02", budget_head="Civil")
    payload = draft.as_payload("K")
    line = payload["lines"][0]
    assert line["item_external_id"] == "3912780000000080001"
    assert line["wbs_code"] == "CAPEX-DEMO-001.02" and line["budget_head"] == "Civil"
    dto = ob.emission_dto(payload=payload, connection_id="CONN-1",
                          module="purchaseorders", local_id="PO-1", dedupe_key="K")
    assert dto.lines[0].item_external_id == "3912780000000080001"
    assert dto.lines[0].dimensions["wbs_code"] == "CAPEX-DEMO-001.02"
    assert dto.lines[0].dimensions["budget_head"] == "Civil"
    assert dto.reference == "WBS-UAT-OUTBOUND-20260912"
    assert draft.as_emission_dto("K", module="purchaseorders") == dto


def test_a_line_without_them_writes_no_keys_and_maps_to_none():
    payload = _draft().as_payload("K")
    assert not {"item_external_id", "wbs_code", "budget_head"} & set(payload["lines"][0])
    dto = ob.emission_dto(payload=payload, connection_id="CONN-1",
                          module="purchaseorders", local_id="PO-1", dedupe_key="K")
    assert dto.lines[0].item_external_id is None
    assert "wbs_code" not in dto.lines[0].dimensions


def test_a_blank_item_id_on_a_line_is_refused():
    with pytest.raises(ob.EmissionShapeError, match="item_external_id"):
        _draft(item_external_id="   ")


# ====================================================== 4. the two routes
def _app(monkeypatch, *, mode: str) -> FastAPI:
    app = FastAPI()
    app.include_router(integrations_live.router)
    app.dependency_overrides[integrations_api._get_database] = lambda: None
    monkeypatch.setattr(
        integrations_api, "_require_visible_connection",
        lambda request, database, connection_id: {
            "connection_id": connection_id, "entity_id": "ENT-DM1",
            "product": "ERP", "mode": mode, "dc": "IN", "organization_id": PINNED,
            "daily_call_ceiling": 2000})
    return app


def _headers(make_user):
    from tests.test_integrations_live_routes_fable51 import _session_headers
    return _session_headers(make_user)


def test_mode_refuses_live_write_while_the_gate_is_shut(monkeypatch, make_user):
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    client = TestClient(_app(monkeypatch, mode="LIVE_READ"), raise_server_exceptions=False)
    resp = client.post("/api/integrations/connections/CONN-1/mode",
                       json={"mode": "LIVE_WRITE", "authorised_by": "owner", "note": "test"},
                       headers=_headers(make_user))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ERP_WRITES_DISABLED"


def test_drain_refuses_before_any_lookup_while_the_gate_is_shut(monkeypatch, make_user):
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    seen = []
    app = _app(monkeypatch, mode="LIVE_WRITE")
    monkeypatch.setattr(integrations_api, "_require_visible_connection",
                        lambda *a: seen.append(a) or {})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/integrations/connections/CONN-1/drain-outbox", json={},
                       headers=_headers(make_user))
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ERP_WRITES_DISABLED"
    assert seen == [], "the connection was looked up before the gate was checked"


def test_drain_refuses_a_live_read_connection_with_the_gate_open(monkeypatch, make_user):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    built = []
    monkeypatch.setattr(integrations_live.live_sweep, "adapter_for_connection",
                        lambda *a, **k: built.append(1))
    client = TestClient(_app(monkeypatch, mode="LIVE_READ"), raise_server_exceptions=False)
    resp = client.post("/api/integrations/connections/CONN-1/drain-outbox", json={},
                       headers=_headers(make_user))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "CONNECTION_NOT_LIVE_WRITE"
    assert built == [], "a transport was built for a connection that may not write"


def test_drain_rejects_an_unknown_body_key(monkeypatch, make_user):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    client = TestClient(_app(monkeypatch, mode="LIVE_WRITE"), raise_server_exceptions=False)
    resp = client.post("/api/integrations/connections/CONN-1/drain-outbox",
                       json={"all": True}, headers=_headers(make_user))
    assert resp.status_code == 422


# ============================================================ 5. /api/health
class _Db:
    def __init__(self, summary):
        self._summary = summary

    def integration_summary(self):
        return self._summary


@pytest.mark.parametrize("connections, expected", [
    ({"ERP": {"LIVE_READ": 1}, "BOOKS_INVENTORY": {"MOCK": 1}}, "LIVE_READ"),
    ({"ERP": {"LIVE_WRITE": 1, "LIVE_READ": 1}}, "LIVE_WRITE"),
    ({"ERP": {"SANDBOX": 2}}, "SANDBOX"),
    ({"ERP": {"MOCK": 1}}, "MOCK"),
    ({}, "MOCK"),
])
def test_health_reports_the_strongest_mode_an_active_connection_holds(
        monkeypatch, connections, expected):
    monkeypatch.setattr(health_api, "get_database",
                        lambda: _Db({"status": "ok", "connections": connections}))
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    body = TestClient(main.app).get("/api/health").json()
    assert body["zoho_mode"] == expected
    assert body["zoho_mode_note"].startswith(expected)
    assert body["integrations"] == {"status": "ok", "connections": connections}
    assert body["outbound_writes_enabled"] is False
    if expected == "MOCK":
        assert "NOT VERIFIED" in body["zoho_mode_note"]
    else:
        assert "NOT VERIFIED" not in body["zoho_mode_note"]


def test_health_falls_back_to_the_constant_without_a_database_or_with_an_unreachable_one(monkeypatch):
    def _none():
        raise RuntimeError("not configured")
    monkeypatch.setattr(health_api, "get_database", _none)
    body = TestClient(main.app).get("/api/health").json()
    assert body["zoho_mode"] == zoho.MODE == "MOCK" and "integrations" not in body
    monkeypatch.setattr(health_api, "get_database",
                        lambda: _Db({"status": "unavailable", "error_class": "OperationalError"}))
    body = TestClient(main.app).get("/api/health").json()
    assert body["zoho_mode"] == "MOCK"
    assert body["integrations"] == {"status": "unavailable", "error_class": "OperationalError"}
    assert "password" not in body["integrations"].get("error_class", "").lower()


def test_health_reports_the_gate_honestly(monkeypatch):
    monkeypatch.setattr(health_api, "get_database",
                        lambda: _Db({"status": "ok", "connections": {"ERP": {"LIVE_WRITE": 1}}}))
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    body = TestClient(main.app).get("/api/health").json()
    assert body["outbound_writes_enabled"] is True and body["zoho_mode"] == "LIVE_WRITE"
