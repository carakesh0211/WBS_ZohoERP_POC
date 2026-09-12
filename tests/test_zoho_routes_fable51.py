"""Fable 5.1 - hardening of every ``/api/zoho/*`` route.

Before this, ``refresh`` and ``test`` answered an unknown connection id with a
200 (``test`` with ``{"error": ...}``, ``refresh`` by updating no rows and
reporting success), ``health`` returned an empty connection for it, ``refresh``
ignored a profile that had never completed OAuth or had been disabled, the
connectivity console recorded ``tested_by = "U-ADM"`` whoever ran it, and only
``authorise`` left a hash-chained audit row.

Now, for every route: the permission is resolved BEFORE any lookup; an unknown
connection id is 404 ``CONNECTION_NOT_FOUND`` and nothing else (never a 403 or
409 that would confirm the id exists -- `api/integrations.py`'s posture; the
SQLite principal carries no entity set, so "out of scope" and "unknown" are the
same answer here); an invalid state or module is a coded 4xx, never a 500;
``Idempotency-Key`` on sync replays the first result; and ``refresh``, ``test``
and ``sync`` write an ``audit()`` row like ``authorise`` (commit 0978636).
"""
from __future__ import annotations

import pytest

from app.backend import main, zoho
from conftest import code_of, detail

UNKNOWN = "CONN-NOPE"


def _first_read_scope(module: str) -> str:
    endpoints = zoho.module_endpoints(module)
    read = next((e for e in endpoints if e["http_method"] == "GET"), endpoints[0])
    return (read["required_oauth_scope"] or [])[0]


def _disable(raw_con):
    raw_con.execute("UPDATE zoho_connection SET status='Disabled' WHERE connection_id='CONN-01'")
    raw_con.commit()


MANAGE_ROUTES = [
    ("POST", "/api/zoho/{cid}/authorise"),
    ("POST", "/api/zoho/{cid}/refresh"),
    ("POST", "/api/zoho/{cid}/test"),
    ("POST", "/api/zoho/{cid}/sync/bills"),
]
READ_ROUTES = [("GET", "/api/zoho/{cid}/health")]


# ================================================= permission before any lookup
@pytest.mark.parametrize("method,template", MANAGE_ROUTES)
def test_manage_routes_refuse_the_auditor_before_looking_the_connection_up(auditor, method, template):
    """Auditor holds connector.read but not connector.manage. An UNKNOWN id gets
    403, not 404: the permission is decided first, so a caller without it cannot
    probe which connection ids exist."""
    resp = auditor.request(method, template.format(cid=UNKNOWN), json={})
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,template", MANAGE_ROUTES + READ_ROUTES)
def test_every_connection_route_refuses_a_role_without_connector_read(make_user, method, template):
    caller = make_user(["Requestor"])
    resp = caller.request(method, template.format(cid=UNKNOWN), json={})
    assert resp.status_code == 403, resp.text
    resp = caller.request(method, template.format(cid="CONN-01"), json={})
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,template", MANAGE_ROUTES + READ_ROUTES)
def test_every_connection_route_refuses_an_unauthenticated_caller(client, method, template):
    assert client.request(method, template.format(cid="CONN-01"), json={}).status_code == 401


# ============================================== unknown connection id is 404
@pytest.mark.parametrize("method,template", MANAGE_ROUTES + READ_ROUTES)
def test_an_unknown_connection_is_404_on_every_route(admin, method, template):
    resp = admin.request(method, template.format(cid=UNKNOWN), json={})
    assert resp.status_code == 404, f"{template}: {resp.status_code} {resp.text}"
    assert code_of(resp) == "CONNECTION_NOT_FOUND"


def test_health_no_longer_fabricates_an_empty_connection(admin):
    resp = admin.get(f"/api/zoho/{UNKNOWN}/health")
    assert resp.status_code == 404
    assert "connection" not in resp.json()


def test_test_route_no_longer_answers_200_with_an_error_body(admin):
    resp = admin.post(f"/api/zoho/{UNKNOWN}/test")
    assert resp.status_code == 404
    assert "error" not in resp.json()


# ============================================== invalid state is a coded 4xx
def test_refresh_before_oauth_is_409_not_connected(admin):
    resp = admin.post("/api/zoho/CONN-01/refresh")
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "NOT_CONNECTED"


def test_refresh_of_a_disabled_connector_is_409(admin, raw_con):
    assert admin.post("/api/zoho/CONN-01/authorise", json={}).status_code == 200
    _disable(raw_con)
    resp = admin.post("/api/zoho/CONN-01/refresh")
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "CONNECTOR_DISABLED"


def test_refresh_of_a_connected_active_profile_succeeds(admin):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    resp = admin.post("/api/zoho/CONN-01/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_in_minutes"] == 60


def test_withholding_a_scope_that_is_not_required_is_422(admin):
    resp = admin.post("/api/zoho/CONN-01/authorise", json={"withhold": ["ZohoERP.invented.ALL"]})
    assert resp.status_code == 422, resp.text
    assert code_of(resp) == "SCOPE_UNKNOWN"
    assert "invented" in detail(resp)["message"]


def test_the_console_still_runs_for_a_disconnected_profile(admin):
    """A diagnostic that refused to run would hide the diagnosis."""
    resp = admin.post("/api/zoho/CONN-01/test")
    assert resp.status_code == 200
    assert all(r["result"] in ("NOT RUN", "NOT AVAILABLE") for r in resp.json()["results"])


@pytest.mark.parametrize("state", ["fresh", "disabled", "connected"])
@pytest.mark.parametrize("method,template", MANAGE_ROUTES + READ_ROUTES + [
    ("POST", "/api/zoho/{cid}/sync/not-a-module"),
    ("POST", "/api/zoho/{cid}/sync/bills?direction=SIDEWAYS"),
    ("POST", "/api/zoho/{cid}/sync/bills?direction=OUTBOUND"),
    ("POST", "/api/zoho/{cid}/sync/journals?direction=OUTBOUND"),
])
def test_no_zoho_route_answers_500_in_any_state(admin, raw_con, state, method, template):
    if state in ("disabled", "connected"):
        admin.post("/api/zoho/CONN-01/authorise", json={})
    if state == "disabled":
        _disable(raw_con)
    for cid in ("CONN-01", UNKNOWN):
        resp = admin.request(method, template.format(cid=cid), json={})
        assert resp.status_code < 500, f"{state} {template} {cid}: {resp.status_code} {resp.text}"
        if resp.status_code >= 400:
            assert code_of(resp), "every refusal carries a code"


# ============================================================== idempotency
def test_sync_dedupes_on_idempotency_key(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    first = admin.post("/api/zoho/CONN-01/sync/bills", headers={"Idempotency-Key": "IDEM-F51"})
    second = admin.post("/api/zoho/CONN-01/sync/bills", headers={"Idempotency-Key": "IDEM-F51"})
    assert first.status_code == second.status_code == 200
    assert first.json()["correlation_id"] == "IDEM-F51"
    assert second.json()["idempotent_replay"] is True
    assert second.json()["message"] == "9 records received."
    events = raw_con.execute("""SELECT COUNT(*) FROM integration_event
                                WHERE correlation_id='IDEM-F51' AND module='bills'""").fetchone()[0]
    assert events == 1, "a replay performs no second sync"


def test_the_same_key_on_another_module_is_not_a_replay(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    admin.post("/api/zoho/CONN-01/sync/bills", headers={"Idempotency-Key": "IDEM-X"})
    other = admin.post("/api/zoho/CONN-01/sync/items", headers={"Idempotency-Key": "IDEM-X"})
    assert other.status_code == 200
    assert "idempotent_replay" not in other.json()


def test_a_replay_leaves_no_second_audit_row(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    admin.post("/api/zoho/CONN-01/sync/bills", headers={"Idempotency-Key": "IDEM-A"})
    admin.post("/api/zoho/CONN-01/sync/bills", headers={"Idempotency-Key": "IDEM-A"})
    rows = raw_con.execute("""SELECT COUNT(*) FROM audit_log
                              WHERE action='MODULE_SYNCED' AND correlation_id='IDEM-A'""").fetchone()[0]
    assert rows == 1


# ============================================================== audit rows
def _audit_rows(raw_con, action):
    return [dict(r) for r in raw_con.execute(
        "SELECT actor, object_type, object_id, detail, entry_hash, prev_hash FROM audit_log "
        "WHERE action=? ORDER BY audit_id", (action,)).fetchall()]


def test_refresh_is_audited_with_the_session_identity(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    admin.post("/api/zoho/CONN-01/refresh")
    rows = _audit_rows(raw_con, "OAUTH_REFRESHED")
    assert len(rows) == 1
    assert rows[0]["actor"] == admin.user_id
    assert rows[0]["object_id"] == "CONN-01" and rows[0]["object_type"] == "ZohoConnection"
    assert rows[0]["entry_hash"], "the row is hash-chained, not a raw insert"


def test_connectivity_test_is_audited_and_names_who_ran_it(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    body = admin.post("/api/zoho/CONN-01/test").json()
    assert {r["tested_by"] for r in body["results"] if "tested_by" in r} == {admin.user_id}
    rows = _audit_rows(raw_con, "CONNECTIVITY_TESTED")
    assert len(rows) == 1 and rows[0]["actor"] == admin.user_id
    assert f"{body['passed']} of {body['tested']}" in rows[0]["detail"]


def test_sync_is_audited_with_direction_module_and_correlation(admin, raw_con):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    body = admin.post("/api/zoho/CONN-01/sync/bills").json()
    rows = _audit_rows(raw_con, "MODULE_SYNCED")
    assert len(rows) == 1 and rows[0]["actor"] == admin.user_id
    assert "INBOUND bills" in rows[0]["detail"]
    assert body["correlation_id"] in rows[0]["detail"]


def test_a_refused_route_leaves_no_audit_row(admin, raw_con):
    admin.post(f"/api/zoho/{UNKNOWN}/refresh")
    admin.post(f"/api/zoho/{UNKNOWN}/test")
    admin.post(f"/api/zoho/{UNKNOWN}/sync/bills")
    admin.post("/api/zoho/CONN-01/refresh")  # NOT_CONNECTED
    for action in ("OAUTH_REFRESHED", "CONNECTIVITY_TESTED", "MODULE_SYNCED"):
        assert _audit_rows(raw_con, action) == []


def test_the_audit_chain_still_verifies_after_the_new_rows(admin, auditor):
    admin.post("/api/zoho/CONN-01/authorise", json={})
    admin.post("/api/zoho/CONN-01/refresh")
    admin.post("/api/zoho/CONN-01/test")
    admin.post("/api/zoho/CONN-01/sync/bills")
    verify = auditor.get("/api/audit/verify")
    assert verify.status_code == 200, verify.text
    assert verify.json().get("ok") is True or verify.json().get("intact") is True, verify.json()


# ================================================ the authorisation matrix
def test_every_mutating_zoho_route_is_in_the_authorisation_matrix():
    from test_api_auth import MUTATING_ROUTES
    listed = {t for t, *_ in MUTATING_ROUTES}
    # From the OpenAPI schema, not `app.routes`: on FastAPI 0.141 / starlette
    # 1.6 (CI) an included router sits in `app.routes` as an `_IncludedRouter`
    # with no `.path`, and the route table's shape is an internal detail (see
    # `test_api_auth._mutating_paths`). The schema is the application's own
    # published description of what it serves.
    live = {path for path, operations in main.app.openapi().get("paths", {}).items()
            if path.startswith("/api/zoho/") and "post" in operations}
    assert live, "no /api/zoho POST routes found"
    assert live <= listed, f"unlisted: {sorted(live - listed)}"
