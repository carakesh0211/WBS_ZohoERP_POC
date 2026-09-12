"""The four connection routes that answer from the tenant once a connection is
LIVE (Fable 5.1, 2026-09-12): organisations, validate, scopes.granted, health.token.

Held here, without a network:

* a MOCK or SANDBOX connection keeps exactly the refusals it had -- the guard
  test's map of silences is untouched;
* a LIVE_READ ERP connection answers `/organizations` with the PINNED
  organisation only: the other organisations the credential can see are
  COUNTED and never named;
* `/validate` reports what the grant carries against what the inventory
  requires and proves each readable module with one real read;
* `/scopes` carries the granted list; `/health.token` reports a mint, never
  the token;
* no transcript of any answer or refusal contains the client secret, the
  refresh token or the access token.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.api import integrations as integrations_api  # noqa: E402
from app.backend.integration import erp  # noqa: E402
from app.backend.integration import live_transport as lt  # noqa: E402
from tests.test_live_transport_fable51 import (  # noqa: E402
    ACCESS, REFRESH, SECRET, FakeOpener, _credentials)

PINNED = "60074128927"
GRANT = ("ERP.settings.READ ERP.contacts.READ ERP.items.READ ERP.purchaseorders.READ "
         "ERP.purchasereceives.READ ERP.bills.READ ERP.custommodules.READ")

ORGS = {"code": 0, "organizations": [
    {"organization_id": PINNED, "name": "DEMO WBS", "currency_code": "INR",
     "country": "India", "time_zone": "Asia/Kolkata", "plan_name": "Trial",
     "org_type": "ERP", "is_gst_india_version": True, "is_multientity_enabled": False},
    {"organization_id": "1", "name": "OTHER CLIENT ONE", "currency_code": "INR"},
    {"organization_id": "2", "name": "OTHER CLIENT TWO", "currency_code": "USD"},
]}


def _app(monkeypatch, *, mode="LIVE_READ", product="ERP", dc="IN") -> FastAPI:
    app = FastAPI()
    app.include_router(integrations_api.router)
    app.dependency_overrides[integrations_api._get_database] = lambda: None
    monkeypatch.setattr(
        integrations_api, "_require_visible_connection",
        lambda request, database, connection_id: {
            "connection_id": connection_id, "entity_id": "ENT-DM1",
            "product": product, "mode": mode, "dc": dc, "organization_id": PINNED})
    return app


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A LiveTransport whose opener answers from a script; installed for the
    router. The route asks `live_transport.shared_transport()`, not
    `LiveTransport()` directly (2026-09-12 review, item 2), which calls
    `LiveTransport(credentials_path, opener=..., clock=..., minute_ceiling=...,
    timeout=...)` internally -- so `factory` accepts and forwards those same
    keywords rather than the zero-argument shape a direct call used to need.
    `lt.reset()` around the test keeps the process-wide cache from leaking
    into or out of it: each test gets its own tmp_path credential file and
    therefore its own cache key, but nothing here should depend on that.
    """
    state = {"opener": FakeOpener(api_answers=[])}
    monkeypatch.setenv(lt.CREDENTIALS_ENV, _credentials(tmp_path, scope=GRANT))
    real = lt.LiveTransport
    lt.reset()

    def factory(credentials_path=None, **kwargs):
        kwargs["opener"] = state["opener"]
        return real(credentials_path, **kwargs)
    monkeypatch.setattr(lt, "LiveTransport", factory)
    yield state
    lt.reset()


def _session_headers(make_user):
    return {"X-Session": make_user(["Administrator"]).session_id}


def _no_secret_in(text: str) -> None:
    for s in (SECRET, REFRESH, ACCESS):
        assert s not in text, f"a secret reached the wire: {text[:200]}"


# ------------------------------------------------------------ MOCK unchanged
@pytest.mark.parametrize("mode", ["MOCK", "SANDBOX"])
def test_a_non_live_connection_keeps_every_refusal(monkeypatch, make_user, mode):
    client = TestClient(_app(monkeypatch, mode=mode), raise_server_exceptions=False)
    h = _session_headers(make_user)
    r = client.get("/api/integrations/connections/C1/organizations", headers=h)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "ORGANISATION_DISCOVERY_UNAVAILABLE"
    r = client.post("/api/integrations/connections/C1/validate", headers=h)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SCOPE_VALIDATION_UNAVAILABLE"
    r = client.get("/api/integrations/connections/C1/scopes", headers=h)
    assert r.status_code == 200 and r.json()["granted"] is None
    assert r.json()["granted_unavailable_reason"]


def test_a_live_books_connection_gets_no_transport(monkeypatch):
    assert integrations_api._live_transport_for(
        {"product": "BOOKS_INVENTORY", "mode": "LIVE_READ", "dc": "IN"}) is None
    assert integrations_api._live_transport_for(
        {"product": "ERP", "mode": "LIVE_READ", "dc": "US"}) is None
    assert integrations_api._live_transport_for(
        {"product": "ERP", "mode": "MOCK", "dc": "IN"}) is None


# ---------------------------------------------------------- organisations
def test_organisations_names_only_the_pinned_one_and_counts_the_rest(monkeypatch, make_user, live):
    live["opener"].api_answers = [ORGS]
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.get("/api/integrations/connections/C1/organizations", headers=_session_headers(make_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pinned_organisation"]["organization_id"] == PINNED
    assert body["pinned_organisation"]["name"] == "DEMO WBS"
    # org-mapping.js reads `organizations`: exactly the pinned one, so the
    # screen can render it and never another client's row.
    assert [o["organization_id"] for o in body["organizations"]] == [PINNED]
    assert body["pinned_organisation_visible"] is True
    assert body["other_organisations_visible"] == 2
    assert "OTHER CLIENT" not in r.text, "another client's organisation was named"
    assert body["source"]["endpoint"] == "/organizations"
    sent = [q.full_url for q in live["opener"].requests if "/organizations" in q.full_url]
    assert sent and sent[0].startswith(erp.API_HOST_BY_DC["IN"] + erp.SERVICE_PATH + "/organizations")
    _no_secret_in(r.text)


def test_organisations_reports_a_pinned_org_the_credential_cannot_see(monkeypatch, make_user, live):
    live["opener"].api_answers = [{"code": 0, "organizations": [{"organization_id": "1", "name": "X"}]}]
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.get("/api/integrations/connections/C1/organizations", headers=_session_headers(make_user))
    assert r.status_code == 200
    assert r.json()["pinned_organisation"] is None
    assert r.json()["organizations"] == []
    assert r.json()["pinned_organisation_visible"] is False
    assert r.json()["other_organisations_visible"] == 1
    assert '"X"' not in r.text


def test_a_tenant_error_is_a_coded_502_without_the_token(monkeypatch, make_user, live):
    live["opener"].api_answers = [(400, {"code": 57, "message": "You are not authorized"})]
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.get("/api/integrations/connections/C1/organizations", headers=_session_headers(make_user))
    assert r.status_code == 502, r.text
    d = r.json()["detail"]
    assert d["code"] == "ERP_TENANT_ERROR" and d["zoho_code"] == 57 and d["status"] == 400
    _no_secret_in(r.text)


def test_a_transport_refusal_is_a_coded_503(monkeypatch, make_user, tmp_path):
    """No credential anywhere: the route says so in the transport's words, never a 500."""
    monkeypatch.setenv(lt.CREDENTIALS_ENV, str(tmp_path / "absent.json"))
    monkeypatch.delenv(lt.ENV_REFRESH_TOKEN, raising=False)
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.get("/api/integrations/connections/C1/organizations", headers=_session_headers(make_user))
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "ERP_LIVE_TRANSPORT_UNAVAILABLE"
    assert "connect.py" in r.json()["detail"]["detail"]


# ---------------------------------------------------------------- validate
def test_validate_reports_the_grant_and_proves_each_readable_module(monkeypatch, make_user, live):
    live["opener"].api_answers = [
        {"code": 0, "bills": [{"bill_id": "1"}]},
        {"code": 0, "contacts": []},
        {"code": 0, "purchaseorders": [{"purchaseorder_id": "1"}]},
        ORGS,
    ]
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.post("/api/integrations/connections/C1/validate", headers=_session_headers(make_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["mode"] == "LIVE_READ"
    assert set(body["granted_scopes"]) == set(GRANT.split())
    by = {e["scope"]: e for e in body["results"]}
    required = {e.scope for e in erp.SCOPE_EVIDENCE}
    assert set(by) == required
    # Every READ scope the grant carries is granted; the two .ALL scopes are
    # NOT granted (a read-only credential) but their modules are readable.
    for scope in required:
        assert by[scope]["readable"], scope
        assert by[scope]["granted"] is scope.endswith(".READ"), scope
    assert body["missing"] == ["ERP.custommodules.ALL", "ERP.purchaseorders.ALL"]
    assert body["unreadable"] == [] and body["read_complete"] is True
    assert body["note"] and "read-only" in body["note"]
    probes = {e["scope"]: e["probe"] for e in body["results"] if e["probe"]}
    assert probes["ERP.bills.READ"] == {"path": "/bills", "answered": True, "rows": 1}
    assert probes["ERP.contacts.READ"]["rows"] == 0
    assert probes["ERP.purchaseorders.ALL"] == {"path": "/purchaseorders", "answered": True, "rows": 1}
    assert probes["ERP.settings.READ"]["answered"] is True
    assert "ERP.purchasereceives.READ" not in probes, "receives have no collection endpoint"
    assert "ERP.custommodules.ALL" not in probes
    # scope-validation.js reads module/result/endpoint/http_method/why per row.
    results = {e["scope"]: e["result"] for e in body["results"]}
    assert results == {"ERP.bills.READ": "PASS", "ERP.contacts.READ": "PASS",
                       "ERP.purchaseorders.ALL": "PASS", "ERP.settings.READ": "PASS",
                       "ERP.purchasereceives.READ": "NOT AVAILABLE",
                       "ERP.custommodules.ALL": "NOT RUN"}
    assert by["ERP.bills.READ"]["endpoint"] == "/bills" and by["ERP.bills.READ"]["http_method"] == "GET"
    assert by["ERP.bills.READ"]["module"] == "bills" and by["ERP.bills.READ"]["why"]
    assert by["ERP.purchasereceives.READ"]["endpoint"] is None
    # every probe went to the pinned organisation, one row, GET
    for q in live["opener"].requests:
        if q.full_url.startswith(lt.ACCOUNTS_SERVER):
            continue
        assert f"organization_id={PINNED}" in q.full_url and "per_page=1" in q.full_url
        assert q.get_method() == "GET"
    assert body["calls_made"] == len(probes)
    _no_secret_in(r.text)


def test_validate_reports_a_module_that_refuses_without_failing_the_whole_run(monkeypatch, make_user, live):
    live["opener"].api_answers = [
        (403, {"code": 57, "message": "You are not authorized to perform this operation"}),
    ]
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.post("/api/integrations/connections/C1/validate", headers=_session_headers(make_user))
    assert r.status_code == 200, r.text
    by = {e["scope"]: e for e in r.json()["results"]}
    first = by["ERP.bills.READ"]["probe"]
    assert first["answered"] is False and "ZohoApiError" in first["error"]
    assert by["ERP.bills.READ"]["result"] == "FAIL"
    assert by["ERP.bills.READ"]["error_code"] == "ZohoApiError"
    assert "not authorized" in by["ERP.bills.READ"]["error_message"]
    _no_secret_in(r.text)


# ------------------------------------------------------------------ scopes
def test_scopes_carries_the_granted_list_on_a_live_connection(monkeypatch, make_user, live):
    client = TestClient(_app(monkeypatch), raise_server_exceptions=False)
    r = client.get("/api/integrations/connections/C1/scopes", headers=_session_headers(make_user))
    assert r.status_code == 200, r.text
    assert r.json()["granted"] == sorted(GRANT.split())
    assert r.json()["granted_unavailable_reason"] is None
    assert r.json()["required"], "the inventory is still served"


# ------------------------------------------------------------------ health
def test_token_health_is_a_mint_and_never_the_token(live):
    facts = integrations_api._token_health(
        {"product": "ERP", "mode": "LIVE_READ", "dc": "IN", "organization_id": PINNED})
    assert facts["state"] == "MINTED"
    assert facts["seconds_left"] > 0
    assert facts["refresh_token_present"] is True
    assert facts["access_token_expires_at"] > facts["token_last_refreshed"]
    assert facts["granted_scopes"] == sorted(GRANT.split())
    _no_secret_in(json.dumps(facts))
    assert live["opener"].token_mints == 1


def test_token_health_stays_unknown_off_live():
    facts = integrations_api._token_health({"product": "ERP", "mode": "MOCK", "dc": "IN"})
    assert facts["state"] == "UNKNOWN"


def test_token_health_reports_a_refused_mint_in_the_transports_words(monkeypatch, tmp_path):
    monkeypatch.setenv(lt.CREDENTIALS_ENV, str(tmp_path / "absent.json"))
    monkeypatch.delenv(lt.ENV_REFRESH_TOKEN, raising=False)
    facts = integrations_api._token_health({"product": "ERP", "mode": "LIVE_READ", "dc": "IN"})
    assert facts["state"] == "REFUSED" and "IntegrationError" in facts["reason"]
    assert facts["refresh_token_present"] is False
