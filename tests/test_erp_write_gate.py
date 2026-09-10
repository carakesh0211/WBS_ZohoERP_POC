"""Fable 5.1 - the ERP outbound write gate.

``CAPEX_ERP_OUTBOUND_WRITES`` (default "0") decides whether any path that would
write to a Zoho tenant may run. Every ERP-mutating path checks it and refuses
with 409 ``ERP_WRITES_DISABLED`` while it is off:

  * ``zoho.sync`` for an OUTBOUND direction on one of the modules the hub writes
    (``zoho.OUTBOUND_MODULES``); inbound reads are untouched;
  * ``POST /api/procurement/purchase-orders/{po_id}/emit``, whose gate is a route
    dependency so it fires after the permission check and before any lookup.

The connector stays MOCK in both states: "enabled" opens the code path, it does
not connect a tenant.
"""
from __future__ import annotations

import pytest

from app.backend import zoho
from conftest import code_of, detail

EMIT_BODY = {"connection_id": "CONN-01", "vendor_external_id": "ZV-77",
             "document_date": "2026-09-07"}


@pytest.fixture()
def writes_off(monkeypatch):
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)


@pytest.fixture()
def writes_on(monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")


# ---------------------------------------------------------------- the switch
def test_outbound_writes_default_off(writes_off):
    assert zoho.outbound_writes_enabled() is False


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "on", " 1x"])
def test_only_the_literal_one_enables_writes(monkeypatch, value):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", value)
    assert zoho.outbound_writes_enabled() is False


def test_one_enables_writes(writes_on):
    assert zoho.outbound_writes_enabled() is True
    assert zoho.MODE == "MOCK", "enabling the gate must not switch the connector live"


def test_outbound_modules_are_the_four_the_hub_writes():
    assert zoho.OUTBOUND_MODULES == {"purchase-order", "journals", "fixed-assets", "custom-modules"}


# ---------------------------------------------------------------- zoho.sync
@pytest.mark.parametrize("module", sorted(zoho.OUTBOUND_MODULES))
def test_outbound_sync_is_refused_while_writes_are_off(admin, writes_off, raw_con, module):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    before = raw_con.execute("SELECT COUNT(*) FROM integration_event").fetchone()[0]
    resp = admin.post(f"/api/zoho/CONN-01/sync/{module}?direction=OUTBOUND")
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "ERP_WRITES_DISABLED"
    after = raw_con.execute("SELECT COUNT(*) FROM integration_event").fetchone()[0]
    assert after == before, "a refused write must leave no integration event"


def test_outbound_sync_runs_when_writes_are_on(admin, writes_on, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    resp = admin.post("/api/zoho/CONN-01/sync/journals?direction=OUTBOUND")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["module"] == "journals" and body["mode"] == "MOCK"
    row = raw_con.execute("""SELECT direction FROM integration_event
                             WHERE correlation_id=?""", (body["correlation_id"],)).fetchone()
    assert row["direction"] == "OUTBOUND"


def test_inbound_sync_is_unaffected_by_the_gate(admin, writes_off):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    assert admin.post("/api/zoho/CONN-01/sync/bills").status_code == 200
    assert admin.post("/api/zoho/CONN-01/sync/journals").status_code == 200
    assert admin.post("/api/zoho/CONN-01/sync/bills?direction=inbound").status_code == 200


def test_a_read_only_module_cannot_be_synced_outbound(admin, writes_on):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    resp = admin.post("/api/zoho/CONN-01/sync/bills?direction=OUTBOUND")
    assert resp.status_code == 422, resp.text
    assert code_of(resp) == "MODULE_NOT_OUTBOUND"


def test_an_invented_direction_is_a_coded_422(admin, writes_on):
    resp = admin.post("/api/zoho/CONN-01/sync/bills?direction=SIDEWAYS")
    assert resp.status_code == 422, resp.text
    assert code_of(resp) == "DIRECTION_INVALID"


def test_the_gate_is_checked_before_connection_state(admin, writes_off):
    """A disconnected connector still reports the gate: the gate is about the
    deployment, the connection state is about one profile."""
    resp = admin.post("/api/zoho/CONN-01/sync/journals?direction=OUTBOUND")
    assert resp.status_code == 409
    assert code_of(resp) == "ERP_WRITES_DISABLED"


def test_the_gate_is_a_function_not_an_import_time_constant(monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    assert zoho.outbound_writes_enabled()
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "0")
    assert not zoho.outbound_writes_enabled()


# ---------------------------------------------------------------- /emit
def test_emit_is_refused_while_writes_are_off(admin, writes_off):
    resp = admin.post("/api/procurement/purchase-orders/PO-004/emit", json=EMIT_BODY)
    assert resp.status_code == 409, resp.text
    assert detail(resp)["code"] == "ERP_WRITES_DISABLED"


def test_emit_refusal_is_the_same_for_an_unknown_purchase_order(admin, writes_off):
    known = admin.post("/api/procurement/purchase-orders/PO-004/emit", json=EMIT_BODY)
    unknown = admin.post("/api/procurement/purchase-orders/PO-NOPE/emit", json=EMIT_BODY)
    assert known.status_code == unknown.status_code == 409
    assert known.json() == unknown.json()


def test_emit_gate_runs_after_the_permission_check(auditor, writes_off):
    """A caller without connector.manage learns nothing about the gate."""
    resp = auditor.post("/api/procurement/purchase-orders/PO-004/emit", json=EMIT_BODY)
    assert resp.status_code == 403, resp.text


def test_emit_proceeds_past_the_gate_when_writes_are_on(admin, writes_on):
    resp = admin.post("/api/procurement/purchase-orders/PO-004/emit", json=EMIT_BODY)
    assert resp.status_code != 409
    assert detail(resp).get("code") != "ERP_WRITES_DISABLED"
