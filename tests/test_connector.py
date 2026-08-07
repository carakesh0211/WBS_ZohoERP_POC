"""AUD-H-003 - Connection status and scopes are not enforcement gates.

The connector must refuse to synchronise when it is not connected, when the
required OAuth scope was never granted, or when the module does not exist in the
verified Zoho specification. It must also not claim a live integration
(AUD-H-002): MOCK mode has to be stated in every response that could be mistaken
for evidence.
"""
from __future__ import annotations

import pytest

from app.backend import zoho
from conftest import code_of, detail


# ============================================================== gating on state
def test_aud_h_003_sync_while_disconnected_is_409(admin):
    resp = admin.post("/api/zoho/CONN-01/sync/bills")
    assert resp.status_code == 409, resp.text
    body = detail(resp)
    assert body["code"] == "NOT_CONNECTED"
    assert "authorisation" in body["message"]


def test_aud_h_003_sync_of_an_unknown_module_is_404(admin):
    resp = admin.post("/api/zoho/CONN-01/sync/not-a-real-module")
    assert resp.status_code == 404, resp.text
    assert code_of(resp) == "MODULE_UNKNOWN"


def test_aud_h_003_an_unknown_module_is_404_even_before_the_connection_is_checked(admin):
    """Module existence is a specification fact, not a connection state."""
    assert admin.post("/api/zoho/CONN-01/sync/invented").status_code == 404
    assert admin.post("/api/zoho/DOES-NOT-EXIST/sync/invented").status_code == 404


def test_aud_h_003_sync_against_an_unknown_connection_is_404(admin):
    resp = admin.post("/api/zoho/CONN-NOPE/sync/bills")
    assert resp.status_code == 404
    assert code_of(resp) == "CONNECTION_NOT_FOUND"


def test_aud_h_003_authorise_on_a_nonexistent_connection_is_404(admin):
    """INT-004: this previously returned synthetic success for any id at all."""
    resp = admin.post("/api/zoho/CONN-NOPE/authorise", json={})
    assert resp.status_code == 404, resp.text
    assert code_of(resp) == "CONNECTION_NOT_FOUND"


def test_aud_h_003_module_metadata_for_an_unknown_module_is_404(admin):
    resp = admin.get("/api/zoho/inventory/not-a-real-module")
    assert resp.status_code == 404
    assert detail(resp)["code"] == "MODULE_UNKNOWN"


def test_aud_h_003_a_disabled_connector_refuses_to_sync(admin, raw_con):
    assert admin.post("/api/zoho/CONN-01/authorise", json={}).status_code == 200
    raw_con.execute("UPDATE zoho_connection SET status='Disabled' WHERE connection_id='CONN-01'")
    raw_con.commit()
    resp = admin.post("/api/zoho/CONN-01/sync/bills")
    assert resp.status_code == 409
    assert code_of(resp) == "CONNECTOR_DISABLED"


# ============================================================== gating on scope
def test_aud_h_003_a_withheld_scope_blocks_the_module_it_protects(admin):
    """Authorise while withholding the scope the bills module needs."""
    bills_scope = _first_read_scope("bills")
    granted = admin.post("/api/zoho/CONN-01/authorise", json={"withhold": [bills_scope]})
    assert granted.status_code == 200, granted.text
    assert bills_scope in granted.json()["missing_scopes"]

    resp = admin.post("/api/zoho/CONN-01/sync/bills")
    assert resp.status_code == 403, resp.text
    body = detail(resp)
    assert body["code"] == "SCOPE_MISSING"
    assert bills_scope in body["message"]


def test_aud_h_003_a_granted_scope_allows_the_module(admin):
    assert admin.post("/api/zoho/CONN-01/authorise", json={}).status_code == 200
    resp = admin.post("/api/zoho/CONN-01/sync/bills")
    assert resp.status_code == 200, resp.text
    assert resp.json()["records"] == 9


def test_aud_h_003_connectivity_tests_do_not_report_pass_while_disconnected(admin):
    results = admin.post("/api/zoho/CONN-01/test").json()["results"]
    assert results
    assert all(r["result"] in ("NOT RUN", "NOT AVAILABLE") for r in results)
    assert not any(r["result"] == "PASS" for r in results)


def test_aud_h_003_connectivity_tests_fail_the_modules_whose_scope_was_withheld(admin):
    bills_scope = _first_read_scope("bills")
    admin.post("/api/zoho/CONN-01/authorise", json={"withhold": [bills_scope]})
    results = {r["module"]: r for r in admin.post("/api/zoho/CONN-01/test").json()["results"]}
    assert results["bills"]["result"] == "FAIL"
    assert results["bills"]["error_code"] == "MISSING_SCOPE"
    assert results["purchase-order"]["result"] == "PASS"


# =========================================================== honesty about MOCK
def test_aud_h_002_the_connector_declares_itself_a_mock(admin, client):
    assert zoho.MODE == "MOCK"
    health = client.get("/api/health").json()
    assert health["zoho_mode"] == "MOCK"
    assert "NOT VERIFIED" in health["zoho_mode_note"]

    admin.post("/api/zoho/CONN-01/authorise", json={})
    sync = admin.post("/api/zoho/CONN-01/sync/bills").json()
    assert sync["mode"] == "MOCK"
    assert sync["verified"] is False
    assert "NOT VERIFIED" in sync["mode_note"]


def test_aud_h_003_documented_specification_gaps_are_surfaced_not_hidden(admin):
    negatives = admin.get("/api/zoho/scopes").json()["hard_negatives"]
    assert negatives, "the connector must state the documented absences"
    assert all({"id", "severity", "title", "impact"} <= set(n) for n in negatives)


def test_aud_h_003_modules_without_a_list_endpoint_report_a_limitation(admin):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    resp = admin.post("/api/zoho/CONN-01/sync/purchasereceives")
    assert resp.status_code == 200, resp.text
    assert resp.json()["limitation"], "an unlistable module must state why"


def test_aud_h_003_the_endpoint_inventory_is_read_from_the_verified_specification(admin):
    summary = admin.get("/api/zoho/inventory").json()
    assert summary["row_count"] > 0
    assert summary["spec_sha256"]
    assert summary["modules"]
    required = {m["module"] for m in summary["required_modules"]}
    assert {"bills", "purchase-order", "fixed-assets"} <= required


# ============================================================ connector authority
def test_aud_h_003_reading_the_connector_requires_connector_read(make_user, admin, auditor):
    assert admin.get("/api/zoho/connections").status_code == 200
    assert auditor.get("/api/zoho/connections").status_code == 200
    for role in ("Requestor", "FinanceApprover", "ProcurementApprover"):
        caller = make_user([role])
        assert caller.get("/api/zoho/connections").status_code == 403, role


def test_aud_h_003_managing_the_connector_requires_connector_manage(auditor):
    """The Auditor may read the connector but must not be able to change it."""
    assert auditor.get("/api/zoho/connections").status_code == 200
    assert auditor.post("/api/zoho/CONN-01/authorise", json={}).status_code == 403
    assert auditor.post("/api/zoho/CONN-01/refresh").status_code == 403
    assert auditor.post("/api/zoho/CONN-01/sync/bills").status_code == 403


def test_aud_h_003_authorisation_stores_secret_references_not_secrets(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    row = raw_con.execute(
        "SELECT client_id_ref, client_secret_ref, refresh_token_ref FROM zoho_connection "
        "WHERE connection_id='CONN-01'").fetchone()
    for value in row:
        assert value is None or value.startswith("secretref://"), value


def test_aud_h_003_authorisation_and_sync_are_recorded_as_integration_events(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    admin.post("/api/zoho/CONN-01/sync/bills")
    events = raw_con.execute(
        "SELECT COUNT(*) FROM integration_event WHERE connection_id='CONN-01'").fetchone()[0]
    assert events >= 1
    assert raw_con.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='OAUTH_AUTHORISED'").fetchone()[0] == 1


def test_aud_h_003_health_reports_the_missing_scopes(admin):
    bills_scope = _first_read_scope("bills")
    admin.post("/api/zoho/CONN-01/authorise", json={"withhold": [bills_scope]})
    health = admin.get("/api/zoho/CONN-01/health").json()
    assert bills_scope in health["scopes"]["missing"]
    assert health["scopes"]["granted"] < health["scopes"]["required"]
    assert health["mode"] == "MOCK"


# ------------------------------------------------------------------- helpers
def _first_read_scope(module: str) -> str:
    endpoints = zoho.module_endpoints(module)
    read = next((e for e in endpoints if e["http_method"] == "GET"), endpoints[0])
    scopes = read["required_oauth_scope"] or []
    assert scopes, f"{module} has no documented scope to withhold"
    return scopes[0]
