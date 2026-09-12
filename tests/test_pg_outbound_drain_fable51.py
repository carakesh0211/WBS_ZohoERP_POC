"""The PostgreSQL half of application-originated emission (Fable 5.1,
2026-09-12): the plan carries the operator's item ids and the resolved WBS
code / budget-head name into `integration_outbox.payload`; the drain sends a
claimable row through the adapter, stamps the local order's external id
once, audits PO_EMITTED, and sends nothing on a second pass; a conflicting
external id is refused and left as it was; the mode route records the
section 11.9 authorisation and refuses LIVE_WRITE while the gate is shut.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)
from outbound_tenant_fake import FakeAdapter, FakeTenant  # noqa: E402
from test_pg_procurement import (  # noqa: E402
    _MetadataOnlyAdapter, _connection, _line, _seed_chain,
)

from app.backend.api import integrations as integrations_api  # noqa: E402
from app.backend.api import integrations_live  # noqa: E402
from app.backend.integration import live_sweep  # noqa: E402
from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement_services as proc  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(not os.environ.get("CAPEX_DB_URL"),
                        reason="PostgreSQL not configured; set CAPEX_DB_URL.")
pytestmark = [PG, pytest.mark.pg]

DOC_DATE = date(2026, 9, 12)
ITEM = "3912780000000080001"


def _approved_po(session, con, ids, *, amount=68_000_00):
    """An approved order, written and read back INSIDE the session's own
    transaction (a second connection cannot see it until the session
    commits)."""
    created = proc.create_po(session, project_id=ids["project"], vendor_name="Civil Co",
                             actor="U-PROC", document_date=DOC_DATE,
                             lines=[_line(ids, amount)])
    session.execute("UPDATE purchase_order SET status = 'Approved' WHERE po_id = %s",
                    (created["po_id"],))
    return created


def _line_ids(session, po_id):
    rows = session.fetchall("SELECT po_line_id FROM po_line WHERE po_id = %s ORDER BY line_no",
                            (po_id,))
    return [r[0] for r in rows]


def _payload(con, outbox_id):
    with con.cursor() as cur:
        cur.execute("SELECT payload, state, external_id FROM integration_outbox WHERE outbox_id = %s",
                    (outbox_id,))
        return cur.fetchone()


def _audit(con, po_id):
    with con.cursor() as cur:
        cur.execute("SELECT action, detail FROM audit_log WHERE object_id = %s ORDER BY audit_id",
                    (po_id,))
        return cur.fetchall()


def _po_external(con, po_id):
    with con.cursor() as cur:
        cur.execute("SELECT external_source, external_id FROM purchase_order WHERE po_id = %s",
                    (po_id,))
        return cur.fetchone()


def _live_write(session, connection_id):
    return store.set_connection_mode(
        session, connection_id=connection_id, mode="LIVE_WRITE", actor="U-ADM",
        live_authorised_by="owner", live_authorisation_note="controlled outbound test")


# ================================================================== the plan
def test_the_plan_carries_the_item_and_the_resolved_codes_into_the_payload(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        po = _approved_po(session, pg_connection, ids)
        line_id, = _line_ids(session, po["po_id"])
        connection_id = _connection(session, ids, suffix=suffix)
        planned = proc.plan_po_emission(
            session, po_id=po["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-1",
            document_date=DOC_DATE, actor="U-PROC", acknowledged=True,
            item_external_ids={line_id: ITEM})
    entry = planned["outbox"][0]
    payload, state, _ = _payload(pg_connection, entry["outbox_id"])
    with pg_connection.cursor() as cur:
        cur.execute("SELECT w.wbs_code, bh.name FROM po_line l JOIN wbs_element w ON w.wbs_id = l.wbs_id "
                    "JOIN budget_head bh ON bh.budget_head_id = l.budget_head_id WHERE l.po_line_id = %s",
                    (line_id,))
        wbs_code, head_name = cur.fetchone()
    line = payload["lines"][0]
    assert state == "PENDING"
    assert line["item_external_id"] == ITEM
    assert line["wbs_code"] == wbs_code and wbs_code
    assert line["budget_head"] == head_name and head_name
    assert payload["reference"] == po["po_number"]


def test_an_item_for_a_line_that_is_not_on_the_order_is_refused_before_anything_is_written(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        po = _approved_po(session, pg_connection, ids)
        connection_id = _connection(session, ids, suffix=suffix)
        for mapping, code in (({"POL-NOT-HERE": ITEM}, "UNKNOWN_PO_LINE"),
                              ({_line_ids(session, po["po_id"])[0]: "  "}, "BLANK_ITEM_EXTERNAL_ID")):
            with pytest.raises(proc.ProcurementError) as exc:
                proc.plan_po_emission(
                    session, po_id=po["po_id"], connection_id=connection_id,
                    adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-1",
                    document_date=DOC_DATE, actor="U-PROC", acknowledged=True,
                    item_external_ids=mapping)
            assert exc.value.code == code and exc.value.status == 422
    with pg_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM integration_outbox WHERE local_id = %s", (po["po_id"],))
        assert cur.fetchone()[0] == 0


# ================================================================= the drain
def _drain_once(session, *, connection_id, adapter, actor="U-ADM"):
    """The sequence `/drain-outbox` runs, on the same functions it calls."""
    source = live_sweep.external_source_for({"product": "ERP"})
    out = []
    for row in store.claim_outbox_batch(session, connection_id=connection_id, limit=25):
        result = proc.send_purchase_order(session, outbox_id=row["outbox_id"],
                                          connection_id=connection_id, adapter=adapter, actor=actor)
        if result.get("sent"):
            result["local"] = proc.record_po_emitted(
                session, po_id=ob.purchase_order_id_of(row["local_id"]), external_source=source,
                external_id=result["external_id"], outbox_id=row["outbox_id"],
                dedupe_key=row["dedupe_key"], actor=actor, created=result["created"])
        out.append(result)
    return out


def test_the_drain_sends_once_stamps_the_order_and_a_second_pass_sends_nothing(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    tenant = FakeTenant()
    adapter = FakeAdapter(tenant=tenant)
    with pg_database.session(Scope.system()) as session:
        po = _approved_po(session, pg_connection, ids)
        line_id, = _line_ids(session, po["po_id"])
        connection_id = _connection(session, ids, suffix=suffix)
        _live_write(session, connection_id)
        planned = proc.plan_po_emission(
            session, po_id=po["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-1",
            document_date=DOC_DATE, actor="U-PROC", acknowledged=True,
            item_external_ids={line_id: ITEM})
        first = _drain_once(session, connection_id=connection_id, adapter=adapter)
    outbox_id = planned["outbox"][0]["outbox_id"]
    assert len(first) == 1 and first[0]["sent"] is True and first[0]["created"] is True
    external_id = first[0]["external_id"]
    assert len(tenant.records) == 1 and external_id in tenant.records
    assert tenant.records[external_id]["cf_capex_ref"] == planned["outbox"][0]["dedupe_key"]
    _, state, recorded = _payload(pg_connection, outbox_id)
    assert state == "SENT" and recorded == external_id
    assert _po_external(pg_connection, po["po_id"]) == ("ZOHO_ERP", external_id)
    audit = _audit(pg_connection, po["po_id"])
    emitted = [d for a, d in audit if a == "PO_EMITTED"]
    assert len(emitted) == 1 and external_id in emitted[0] and outbox_id in emitted[0]
    assert "created" in emitted[0]
    # The identical retry: the plan is idempotent (no second outbox row) and
    # the drain finds nothing claimable. The tenant still holds ONE record.
    with pg_database.session(Scope.system()) as session:
        again = proc.plan_po_emission(
            session, po_id=po["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-1",
            document_date=DOC_DATE, actor="U-PROC", acknowledged=True,
            item_external_ids={line_id: ITEM})
        second = _drain_once(session, connection_id=connection_id, adapter=adapter)
    assert [e["outbox_id"] for e in again["outbox"]] == [outbox_id]
    assert all(e["created"] is False for e in again["outbox"])
    assert second == []
    assert len(tenant.records) == 1 and tenant.create_log == [external_id]
    assert len([d for a, d in _audit(pg_connection, po["po_id"]) if a == "PO_EMITTED"]) == 1


def test_a_different_external_id_on_the_order_is_a_refusal_not_an_overwrite(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        po = _approved_po(session, pg_connection, ids)
        proc.record_po_emitted(session, po_id=po["po_id"], external_source="ZOHO_ERP",
                               external_id="ZPO-1", outbox_id="OUT-1", dedupe_key="K",
                               actor="U-ADM", created=True)
        # The same id again is the repeated-drain case: accepted, no second audit entry needed.
        proc.record_po_emitted(session, po_id=po["po_id"], external_source="ZOHO_ERP",
                               external_id="ZPO-1", outbox_id="OUT-1", dedupe_key="K",
                               actor="U-ADM", created=False)
        with pytest.raises(proc.ProcurementError) as exc:
            proc.record_po_emitted(session, po_id=po["po_id"], external_source="ZOHO_ERP",
                                   external_id="ZPO-2", outbox_id="OUT-2", dedupe_key="K",
                                   actor="U-ADM", created=True)
        assert exc.value.code == "EXTERNAL_ID_CONFLICT" and exc.value.status == 409
        with pytest.raises(proc.ProcurementError) as blank:
            proc.record_po_emitted(session, po_id=po["po_id"], external_source="ZOHO_ERP",
                                   external_id="", outbox_id="OUT-3", dedupe_key="K",
                                   actor="U-ADM", created=True)
        assert blank.value.code == "BLANK_EXTERNAL_ID"
    assert _po_external(pg_connection, po["po_id"]) == ("ZOHO_ERP", "ZPO-1")


# ================================================================= the routes
def _route_app(pg_database, monkeypatch, connection):
    app = FastAPI()
    app.include_router(integrations_live.router)
    app.dependency_overrides[integrations_api._get_database] = lambda: pg_database
    # The router floor populates `request.state.integration_principal`; the
    # per-route `_requires("connector.manage")` then resolves the permission
    # against `auth.PERMISSIONS` for real. The matrix test in test_api_auth.py
    # holds the denied-role half; here the principal is an administrator.
    def _floor(request: Request) -> None:   # `Request` is a module-level import: with
        # postponed annotations FastAPI must resolve the name in module globals
        request.state.integration_principal = {"user_id": "U-ADM", "roles": ["Administrator"]}
    app.dependency_overrides[integrations_api.require_integration_access] = _floor
    monkeypatch.setattr(integrations_api, "_require_visible_connection",
                        lambda request, database, connection_id: connection)
    monkeypatch.setattr(integrations_api, "_scope_for", lambda request, database: Scope.system())
    monkeypatch.setattr(integrations_api, "_actor", lambda request: "U-ADM")
    return app


def test_the_routes_drain_through_the_adapter_and_move_the_mode_with_its_authorisation(
        pg_database, pg_connection, monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    tenant = FakeTenant()
    monkeypatch.setattr(live_sweep, "adapter_for_connection",
                        lambda connection, **kw: FakeAdapter(tenant=tenant))
    with pg_database.session(Scope.system()) as session:
        po = _approved_po(session, pg_connection, ids)
        line_id, = _line_ids(session, po["po_id"])
        connection_id = _connection(session, ids, suffix=suffix)
        row = store.get_connection(session, connection_id, scope=Scope.system())
        proc.plan_po_emission(
            session, po_id=po["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-1",
            document_date=DOC_DATE, actor="U-PROC", acknowledged=True,
            item_external_ids={line_id: ITEM})
    connection = {**row, "mode": "MOCK"}
    client = TestClient(_route_app(pg_database, monkeypatch, connection), raise_server_exceptions=False)
    url = f"/api/integrations/connections/{connection_id}"
    # Gate shut: LIVE_WRITE refused, and the row is untouched.
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    resp = client.post(f"{url}/mode", json={"mode": "LIVE_WRITE", "authorised_by": "owner", "note": "n"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ERP_WRITES_DISABLED"
    with pg_connection.cursor() as cur:
        cur.execute("SELECT mode FROM integration_connection WHERE connection_id = %s", (connection_id,))
        assert cur.fetchone()[0] == "MOCK"
    # Gate open: the mode moves, with the authorisation recorded on the row.
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    resp = client.post(f"{url}/mode", json={"mode": "LIVE_WRITE", "authorised_by": "owner", "note": "n"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode_before"] == "MOCK" and resp.json()["mode"] == "LIVE_WRITE"
    with pg_connection.cursor() as cur:
        cur.execute("SELECT mode, live_authorised_by FROM integration_connection WHERE connection_id = %s",
                    (connection_id,))
        assert cur.fetchone() == ("LIVE_WRITE", "owner")
    # A live mode without its authorisation is refused by the store with its
    # own code (section 11.9), and the row stays LIVE_WRITE.
    resp = client.post(f"{url}/mode", json={"mode": "LIVE_READ"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "LIVE_MODE_UNAUTHORISED"
    with pg_connection.cursor() as cur:
        cur.execute("SELECT mode FROM integration_connection WHERE connection_id = %s", (connection_id,))
        assert cur.fetchone()[0] == "LIVE_WRITE"
    # The drain, against the row as it now is.
    connection["mode"] = "LIVE_WRITE"
    resp = client.post(f"{url}/drain-outbox", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["claimed"] == 1 and body["sent"] == 1
    external_id = body["results"][0]["external_id"]
    assert body["results"][0]["local"]["external_id"] == external_id
    assert _po_external(pg_connection, po["po_id"]) == ("ZOHO_ERP", external_id)
    assert len(tenant.records) == 1
    # Identical call: nothing claimable, nothing sent, still one tenant record.
    resp = client.post(f"{url}/drain-outbox", json={})
    assert resp.status_code == 200 and resp.json()["claimed"] == 0 and resp.json()["sent"] == 0
    assert len(tenant.records) == 1
    # Back to LIVE_READ, and the drain is refused again.
    resp = client.post(f"{url}/mode", json={"mode": "LIVE_READ", "authorised_by": "owner", "note": "done"})
    assert resp.status_code == 200 and resp.json()["mode"] == "LIVE_READ"
    connection["mode"] = "LIVE_READ"
    resp = client.post(f"{url}/drain-outbox", json={})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "CONNECTION_NOT_LIVE_WRITE"
    with pg_connection.cursor() as cur:
        cur.execute("SELECT kind FROM integration_event WHERE connection_id = %s ORDER BY event_id",
                    (connection_id,))
        kinds = [r[0] for r in cur.fetchall()]
    assert kinds.count("CONNECTION_MODE_CHANGED") == 2 and kinds.count("OUTBOX_DRAINED") == 2


# ============================================================= health summary
def test_the_health_summary_counts_connections_through_row_level_security(
        pg_database, pg_app_database, pg_connection):
    """`/api/health` reads the connection catalogue as an aggregate on the
    application role, whose `integration_connection` is under FORCED RLS: the
    summary must apply the system scope for the read or it counts zero and
    the health probe lies about a live tenant."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_00)
    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        _live_write(session, connection_id)
    summary = pg_app_database.integration_summary()
    assert summary["status"] == "ok"
    assert summary["connections"]["ERP"]["LIVE_WRITE"] >= 1
    assert set(summary) == {"status", "connections"}
    # Only aggregates: no id, organisation or name anywhere in the answer.
    assert connection_id not in json.dumps(summary)
