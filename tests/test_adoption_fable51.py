"""Adopting a tenant-raised Zoho order (Fable 5.1, migration 032).

Two halves.

1. DATABASE-FREE. The pure helpers (`_line_custom_field`, `_quantity_number`,
   `_resolve_status`) against the raw shape `tools/erp_demo/po_snapshot.py`
   verified live; `adopt_tenant_orders`'s two up-front refusals
   (`CONNECTION_NOT_LIVE`, `ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION`)
   against an `_UntouchableSession`, proving neither ever reaches the
   database; and the `/adopt-orders` route's guard (401 / 403), which needs
   only the identity store, never PostgreSQL.

2. LIVE POSTGRESQL, a fake adapter whose only method is
   `get_purchase_order`. `adoption.adopt_tenant_orders` end to end: adopts a
   tenant-raised order and resolves the pre-existing UNSANCTIONED_COMMITMENT
   exception; is idempotent on a second sweep; raises
   ADOPTION_DIMENSION_CONFLICT and leaves the order untouched when the
   tenant's dimensions change; refuses a duplicate cf_capex_ref; refuses a
   line whose cf_wbs_code / cf_budget_head does not resolve, posting
   nothing; holds a JPY order with no active rate under
   FOREIGN_CURRENCY_BASIS_MISSING and adopts one with an active rate at
   paise asserted through `fx.translate_lines`, never a hand-typed float;
   blocks a second concurrent adoption of the same order; and, the whole
   point of finding 1: after adoption, `sweeps.SweepPoAnchored` mirrors a
   receive against the adopted order's line (GRN matching) and
   `pg.procurement.mirror_bill` attributes a bill to it (bill matching).

   A SKIP IS NOT A PASS: this half skips without `CAPEX_DB_URL`.
"""
from __future__ import annotations

import os
import sys as _sys
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path as _Path
from typing import Any, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url, scoped_role_database,
)

from app.backend.api import integrations_live  # noqa: E402
from app.backend.integration import adoption, jobs, live_sweep, sweeps  # noqa: E402
from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.integration.dto import LineDTO, PurchaseOrderDTO, ReceiveDTO, SourceRef  # noqa: E402
from app.backend.pg import fx  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from app.backend.pg import procurement_services as psvc  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL. A SKIP IS NOT A PASS.")

T0 = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
DOC_DATE = date(2026, 9, 1)

ORG_ID = adoption.DEMO_ORGANIZATION_ID
ENTITY, PROJECT = "ENT-AD", "PRJ-AD"
HEAD_ID, HEAD_NAME = "BH-AD", "Electrical"
WBS, WBS_CODE = "WBS-AD-A", "CAPEX-2026-001.04"
ACTOR = "U-AD-ADMIN"
CONN = "CONN-AD-LIVE"
SOURCE = "ZOHO_ERP"


# =============================================================== the fixtures
def _source_ref() -> SourceRef:
    return SourceRef(product="ERP", service="erp", api_version="v3",
                     endpoint="/purchaseorders/X", retrieved_at=T0)


def _line(*, external_line_id: str, wbs_code: str | None = WBS_CODE,
         head: str | None = HEAD_NAME, qty: str = "2",
         rate_paise: int = 50000, total_paise: int = 100000) -> LineDTO:
    """A PO detail line, its custom fields shaped as `po_snapshot.py` verified
    them live: `item_custom_fields: [{"api_name", "value"}, ...]`."""
    fields = []
    if wbs_code is not None:
        fields.append({"api_name": "cf_wbs_code", "value": wbs_code})
    if head is not None:
        fields.append({"api_name": "cf_budget_head", "value": head})
    return LineDTO(
        external_line_id=external_line_id, line_number=1,
        description="Cable tray", quantity=qty, unit_price_paise=rate_paise,
        line_total_paise=total_paise, tax_paise=0,
        raw={"item_custom_fields": fields})


def _order(external_id: str, *, capex_ref: str | None = "CAPEX-REF-1",
          currency: str = "INR", lines: tuple[LineDTO, ...] | None = None,
          total_paise: int = 100000, status: str | None = "open",
          vendor: str = "Acme Cables",
          document_date: date = DOC_DATE) -> PurchaseOrderDTO:
    if lines is None:
        lines = (_line(external_line_id=f"{external_id}L1", total_paise=total_paise),)
    return PurchaseOrderDTO(
        source=_source_ref(), external_id=external_id,
        document_number=f"PO-{external_id}", document_date=document_date,
        last_modified=T0, vendor_external_id="ZV-1", vendor_name=vendor,
        currency_code=currency, subtotal_paise=total_paise, tax_paise=0,
        total_paise=total_paise, external_status_raw=status or "",
        lines=lines, lines_hydrated=True, dedupe_key=capex_ref)


class _FakeAdapter:
    """Adoption's only call: `get_purchase_order`. Nothing else exists on it,
    so a defect that reached for `list_purchase_orders` or any write verb
    would fail loudly with `AttributeError` rather than silently succeed."""

    def __init__(self, orders: Mapping[str, PurchaseOrderDTO]):
        self.orders = dict(orders)
        self.calls: list[str] = []

    def get_purchase_order(self, external_id: str) -> PurchaseOrderDTO:
        self.calls.append(external_id)
        return self.orders[external_id]


def _connection(mode: str = "LIVE_READ", product: str = "ERP",
               organization_id: str = ORG_ID) -> dict[str, Any]:
    return {"connection_id": CONN, "entity_id": ENTITY, "mode": mode,
           "product": product, "organization_id": organization_id, "dc": "in"}


# ============================================================ 1. database-free
def test_line_custom_field_reads_the_verified_live_shape():
    line = _line(external_line_id="X")
    assert adoption._line_custom_field(line, adoption.CF_WBS_CODE) == WBS_CODE
    assert adoption._line_custom_field(line, adoption.CF_BUDGET_HEAD) == HEAD_NAME
    blank = LineDTO(external_line_id="Y", line_number=1, description="",
                    quantity="1", unit_price_paise=1, line_total_paise=1,
                    tax_paise=0, raw={})
    assert adoption._line_custom_field(blank, adoption.CF_WBS_CODE) is None
    empty_value = LineDTO(
        external_line_id="Z", line_number=1, description="", quantity="1",
        unit_price_paise=1, line_total_paise=1, tax_paise=0,
        raw={"item_custom_fields": [{"api_name": "cf_wbs_code", "value": ""}]})
    assert adoption._line_custom_field(empty_value, adoption.CF_WBS_CODE) is None


def test_quantity_number_is_int_when_whole_and_float_otherwise():
    assert adoption._quantity_number("2") == 2
    assert isinstance(adoption._quantity_number("2"), int)
    assert adoption._quantity_number("2.5") == 2.5
    assert isinstance(adoption._quantity_number("2.5"), float)


@pytest.mark.parametrize("raw,expected", [
    ("draft", "Draft"), ("open", "Released"), ("billed", "Fully Actualised"),
    ("cancelled", "Cancelled"), ("pending_approval", "Draft"),
    ("", "Draft"), (None, "Draft"),
])
def test_resolve_status_mirrors_the_verified_erp_raw_values(raw, expected):
    assert adoption._resolve_status(raw) == expected


class _UntouchableSession:
    """A session any use of which is a test failure."""

    def __getattr__(self, name):  # pragma: no cover - the assertion
        raise AssertionError(
            f"the session was touched ({name}) before adoption's guard "
            f"refused the run")


def test_adopt_tenant_orders_refuses_a_non_live_connection_before_touching_anything():
    for mode in ("MOCK", "SANDBOX"):
        with pytest.raises(adoption.AdoptionError) as refused:
            adoption.adopt_tenant_orders(
                _UntouchableSession(), connection=_connection(mode=mode),
                adapter=_FakeAdapter({}), actor=ACTOR, correlation_id="c")
        assert refused.value.code == "CONNECTION_NOT_LIVE"
        assert refused.value.status == 409


@pytest.mark.parametrize("product,organization_id", [
    ("ERP", "1"), ("BOOKS_INVENTORY", ORG_ID), ("ERP", ""),
])
def test_adopt_tenant_orders_refuses_the_wrong_organisation_before_touching_anything(
        product, organization_id):
    with pytest.raises(adoption.AdoptionError) as refused:
        adoption.adopt_tenant_orders(
            _UntouchableSession(),
            connection=_connection(product=product, organization_id=organization_id),
            adapter=_FakeAdapter({}), actor=ACTOR, correlation_id="c")
    assert refused.value.code == "ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION"
    assert refused.value.status == 403


# --------------------------------------------------------------- the route
def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(integrations_live.router)
    return app


def _client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app or _app(), raise_server_exceptions=False)


ADOPT_URL = f"/api/integrations/connections/{CONN}/adopt-orders"


def _detail(response) -> dict:
    body = response.json()
    assert isinstance(body, dict) and "detail" in body, body
    return body["detail"]


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    return capex_db


def test_route_is_in_the_authorisation_matrix_with_manage_and_a_denied_role():
    from test_api_auth import MUTATING_ROUTES
    rows = [r for r in MUTATING_ROUTES
            if r[0] == "/api/integrations/connections/{connection_id}/adopt-orders"]
    assert len(rows) == 1
    _template, method, _url, _body, permission, denied = rows[0]
    assert method == "POST" and permission == "connector.manage"
    assert denied == "Auditor"


def test_route_an_unauthenticated_caller_is_refused():
    resp = _client().post(ADOPT_URL, json={})
    assert resp.status_code == 401 and _detail(resp)["code"] == "NOT_AUTHENTICATED"


def test_route_a_role_without_connector_read_stops_at_the_floor(make_user):
    caller = make_user(["Requestor"])
    resp = _client().post(ADOPT_URL, json={},
                          headers={"X-Session": caller.session_id})
    assert resp.status_code == 403 and _detail(resp)["code"] == "FORBIDDEN"


def test_route_the_read_only_role_is_refused_adoption(make_user):
    caller = make_user(["Auditor"])
    resp = _client().post(ADOPT_URL, json={},
                          headers={"X-Session": caller.session_id})
    assert resp.status_code == 403 and _detail(resp)["code"] == "FORBIDDEN"


# ============================================== 2. live PostgreSQL, a fake GET
def _seed(con) -> None:
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by,"
       " updated_by) VALUES (%s, 'ORGAD', 'AD Org', 'T', 'T')"
       " ON CONFLICT DO NOTHING", (ENTITY + "-ORG",))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
       " ON CONFLICT DO NOTHING", (ENTITY, ENTITY + "-ORG", ENTITY, ENTITY))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'Released', 'T', 'T')"
       " ON CONFLICT DO NOTHING", (PROJECT, ENTITY, PROJECT, PROJECT))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
       " ON CONFLICT DO NOTHING", (HEAD_ID, ENTITY, HEAD_ID, HEAD_NAME))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description,"
       " wbs_path, status, created_by, updated_by)"
       " VALUES (%s, %s, %s, %s, %s, 'Released', 'T', 'T') ON CONFLICT DO NOTHING",
       (WBS, PROJECT, WBS_CODE, WBS, "a"))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise,"
       " updated_by) VALUES (%s, %s, 1000000000, 'T') ON CONFLICT DO NOTHING",
       (WBS, HEAD_ID))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by)"
       " VALUES (%s, %s, 'T') ON CONFLICT DO NOTHING", (WBS, HEAD_ID))
    ex("INSERT INTO app_user (user_id, email, display_name, principal_kind,"
       " created_by, updated_by) VALUES (%s, 'ad-admin@example.test',"
       " 'AD admin', 'USER', 'T', 'T') ON CONFLICT DO NOTHING", (ACTOR,))
    ex("INSERT INTO integration_connection (connection_id, entity_id, product,"
       " dc, organization_id, connector_name, mode, per_minute_call_ceiling,"
       " daily_call_ceiling, live_authorised_at, live_authorised_by,"
       " live_authorisation_note, created_by, updated_by)"
       " VALUES (%s, %s, 'ERP', 'in', %s, 'zoho_erp', 'LIVE_READ', 100, 2000,"
       " %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
       (CONN, ENTITY, ORG_ID, T0, "U-AD-CFO", "Adoption test"))
    con.commit()


def _connection_row(con) -> dict:
    row = con.execute(
        "SELECT connection_id, entity_id, product, dc, organization_id,"
        " connector_name, mode, per_minute_call_ceiling, daily_call_ceiling,"
        " is_active FROM integration_connection WHERE connection_id = %s",
        (CONN,)).fetchone()
    return store._connection_row(row)


def _scope() -> Scope:
    return Scope(user_id=ACTOR, principal_kind="USER",
                 entity_ids=frozenset({ENTITY}), plant_ids=None,
                 project_ids=None, location_ids=None, read_all=False)


def _record_inbox(session, *, external_id: str, now: datetime = T0) -> None:
    store.record_inbound(
        session, inbox_id=f"INB-{external_id}", connection_id=CONN,
        module="purchaseorders", external_id=external_id,
        raw_payload={"purchaseorder_id": external_id},
        external_status_raw="open", external_status_product="ERP",
        external_status_api_version="v3", correlation_id="c0", now=now)


def _adopt(database, con, adapter, **kwargs) -> dict[str, Any]:
    with database.session(_scope()) as session:
        return adoption.adopt_tenant_orders(
            session, connection=_connection_row(con), adapter=adapter,
            actor=ACTOR, correlation_id=kwargs.pop("correlation_id", "c1"),
            **kwargs)


def _create_local_po(database, *, amount_paise: int = 100000) -> str:
    """A LOCAL purchase order THIS SYSTEM raised, with no `external_id` --
    exactly the shape an emission `CAPEX_ERP_OUTBOUND_WRITES` never
    authorised leaves behind. Two units at half the total each, matching
    `_line`'s own default shape so a LINK candidate built against it lines
    up without extra bookkeeping."""
    with database.session(_scope()) as session:
        written = psvc.create_po(
            session, project_id=PROJECT, vendor_name="Acme Cables",
            lines=[{"wbs_id": WBS, "budget_head_id": HEAD_ID,
                    "description": "Cable tray", "quantity": 2,
                    "amount_paise": amount_paise}],
            actor=ACTOR)
    return written["po_id"]


@PG
@pytest.mark.pg
def test_live_adopts_a_tenant_raised_order_and_resolves_the_unsanctioned_commitment(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-1", capex_ref="CAPEX-REF-1",
                   lines=(_line(external_line_id="ZPOL-1"),))
    adapter = _FakeAdapter({"ZPO-1": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-1")
            store.raise_exception(
                session, kind="UNSANCTIONED_COMMITMENT",
                object_type="purchase_order", object_id="ZPO-1",
                detail="raised by a prior poll", raised_at=T0,
                entity_id=ENTITY, actor="SVC-SWEEP")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert result == {"adopted": 1, "linked": 0, "skipped": 0, "exceptions": 0,
                      "calls": 1, "exception_ids": []}
    assert adapter.calls == ["ZPO-1"]
    row = con.execute(
        "SELECT po_id, commitment_origin, external_source, external_id,"
        " external_capex_ref, status, currency, adopted_by"
        " FROM purchase_order WHERE external_id = 'ZPO-1'").fetchone()
    assert row is not None
    (po_id, origin, ext_source, ext_id, ref, status, currency,
     adopted_by) = row
    assert origin == "EXTERNAL_UNSANCTIONED"
    assert (ext_source, ext_id, ref) == (SOURCE, "ZPO-1", "CAPEX-REF-1")
    assert status == "Released", "'open' maps to Released"
    assert currency == "INR" and adopted_by == ACTOR

    line = con.execute(
        "SELECT wbs_id, budget_head_id, amount_paise, line_external_id"
        " FROM po_line WHERE po_id = %s", (po_id,)).fetchone()
    assert line == (WBS, HEAD_ID, 100000, "ZPOL-1")

    exc = con.execute(
        "SELECT status, resolution_note FROM reconciliation_exception"
        " WHERE kind = 'UNSANCTIONED_COMMITMENT' AND object_id = 'ZPO-1'"
    ).fetchone()
    assert exc == ("Resolved", f"adopted as {po_id}")

    cell = con.execute(
        "SELECT commitment_paise FROM budget_ledger_cell"
        " WHERE wbs_id = %s AND budget_head_id = %s", (WBS, HEAD_ID)).fetchone()
    assert cell == (100000,), "the exposure lands on the control cell"


@PG
@pytest.mark.pg
def test_live_repeat_adoption_is_idempotent(pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-2", capex_ref="CAPEX-REF-2",
                   lines=(_line(external_line_id="ZPOL-2"),))
    adapter = _FakeAdapter({"ZPO-2": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-2")
        first = _adopt(database, con, adapter, correlation_id="c1")
        second = _adopt(database, con, adapter, correlation_id="c2")
    finally:
        database.close()

    assert first["adopted"] == 1
    assert second == {"adopted": 0, "linked": 0, "skipped": 1, "exceptions": 0,
                      "calls": 1, "exception_ids": []}
    assert adapter.calls == ["ZPO-2", "ZPO-2"], "the detail is re-read every sweep"
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-2'"
    ).fetchone() == (1,)


@PG
@pytest.mark.pg
def test_live_changed_dimensions_on_repeat_raise_conflict_and_leave_the_order(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    con.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description,"
        " wbs_path, status, created_by, updated_by) VALUES ('WBS-AD-B', %s,"
        " 'CAPEX-2026-001.05', 'b', 'b', 'Released', 'T', 'T')", (PROJECT,))
    con.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise,"
        " updated_by) VALUES ('WBS-AD-B', %s, 1000000000, 'T')", (HEAD_ID,))
    con.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by)"
        " VALUES ('WBS-AD-B', %s, 'T')", (HEAD_ID,))
    con.commit()

    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order_v1 = _order("ZPO-3", capex_ref="CAPEX-REF-3",
                      lines=(_line(external_line_id="ZPOL-3"),))
    order_v2 = _order("ZPO-3", capex_ref="CAPEX-REF-3",
                      lines=(_line(external_line_id="ZPOL-3",
                                   wbs_code="CAPEX-2026-001.05"),))
    adapter = _FakeAdapter({"ZPO-3": order_v1})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-3")
        first = _adopt(database, con, adapter, correlation_id="c1")
        adapter.orders["ZPO-3"] = order_v2
        second = _adopt(database, con, adapter, correlation_id="c2")
    finally:
        database.close()

    assert first["adopted"] == 1
    assert (second["adopted"], second["skipped"], second["exceptions"]) == (0, 0, 1)
    exc = con.execute(
        "SELECT kind FROM reconciliation_exception WHERE object_id = 'ZPO-3'"
        " ORDER BY raised_at DESC LIMIT 1").fetchone()
    assert exc == ("ADOPTION_DIMENSION_CONFLICT",)
    line = con.execute(
        "SELECT pol.wbs_id FROM po_line pol"
        " JOIN purchase_order po ON po.po_id = pol.po_id"
        " WHERE po.external_id = 'ZPO-3'").fetchone()
    assert line == (WBS,), "the local order is left exactly as it was"


@PG
@pytest.mark.pg
def test_live_a_duplicate_capex_ref_across_two_orders_is_refused(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order_a = _order("ZPO-4A", capex_ref="CAPEX-DUP",
                     lines=(_line(external_line_id="ZPOL-4A"),))
    order_b = _order("ZPO-4B", capex_ref="CAPEX-DUP",
                     lines=(_line(external_line_id="ZPOL-4B"),))
    adapter = _FakeAdapter({"ZPO-4A": order_a, "ZPO-4B": order_b})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-4A")
            _record_inbox(session, external_id="ZPO-4B")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["exceptions"]) == (1, 1)
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_capex_ref = 'CAPEX-DUP'"
    ).fetchone() == (1,)
    exc = con.execute(
        "SELECT kind FROM reconciliation_exception WHERE object_id = 'ZPO-4B'"
    ).fetchone()
    assert exc == ("ADOPTION_DIMENSION_INVALID",)


@PG
@pytest.mark.pg
def test_live_a_line_whose_wbs_code_does_not_resolve_posts_nothing(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-5", capex_ref="CAPEX-REF-5",
                   lines=(_line(external_line_id="ZPOL-5",
                                wbs_code="NO-SUCH-WBS-CODE"),))
    adapter = _FakeAdapter({"ZPO-5": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-5")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["exceptions"]) == (0, 1)
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-5'"
    ).fetchone() == (0,)
    exc = con.execute(
        "SELECT kind, detail FROM reconciliation_exception"
        " WHERE object_id = 'ZPO-5'").fetchone()
    assert exc[0] == "ADOPTION_DIMENSION_INVALID"
    assert "line 1" in exc[1]


@PG
@pytest.mark.pg
def test_live_a_line_whose_budget_head_does_not_resolve_posts_nothing(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-5B", capex_ref="CAPEX-REF-5B",
                   lines=(_line(external_line_id="ZPOL-5B",
                                head="No Such Head"),))
    adapter = _FakeAdapter({"ZPO-5B": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-5B")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["exceptions"]) == (0, 1)
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-5B'"
    ).fetchone() == (0,)


@PG
@pytest.mark.pg
def test_live_a_line_whose_head_disagrees_with_its_wbs_elements_own_head_is_invalid(
        pg_connection, pg_url, pg_disposable_db_name):
    """Budget-head isolation: a WBS element that names its OWN budget head is
    not a suggestion. A line stamped with a different one is invalid, never a
    second head the WBS may also carry."""
    con = pg_connection
    _seed(con)
    con.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
        " created_by, updated_by) VALUES ('BH-AD-CIVIL', %s, 'CIVIL',"
        " 'Civil Works', 'T', 'T')", (ENTITY,))
    con.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description,"
        " wbs_path, budget_head_id, status, created_by, updated_by)"
        " VALUES ('WBS-AD-ISO', %s, 'CAPEX-2026-001.06', 'iso', 'iso',"
        " %s, 'Released', 'T', 'T')", (PROJECT, HEAD_ID))
    con.commit()

    database = scoped_role_database(pg_url, pg_disposable_db_name)
    # The WBS element's own head is HEAD_ID ("Electrical"); the line names
    # "Civil Works" instead.
    order = _order("ZPO-5C", capex_ref="CAPEX-REF-5C",
                   lines=(_line(external_line_id="ZPOL-5C",
                                wbs_code="CAPEX-2026-001.06",
                                head="Civil Works"),))
    adapter = _FakeAdapter({"ZPO-5C": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-5C")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["linked"], result["exceptions"]) == (0, 0, 1)
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-5C'"
    ).fetchone() == (0,)
    exc = con.execute(
        "SELECT kind, detail FROM reconciliation_exception"
        " WHERE object_id = 'ZPO-5C'").fetchone()
    assert exc[0] == "ADOPTION_DIMENSION_INVALID"
    assert "WBS-AD-ISO" in exc[1] and "own budget head" in exc[1]


@PG
@pytest.mark.pg
def test_live_jpy_order_without_an_active_rate_is_held_as_foreign_currency_basis_missing(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-6", capex_ref="CAPEX-REF-6", currency="JPY",
                   total_paise=100000,
                   lines=(_line(external_line_id="ZPOL-6", total_paise=100000),))
    adapter = _FakeAdapter({"ZPO-6": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-6")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["exceptions"]) == (0, 1)
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-6'"
    ).fetchone() == (0,)
    exc = con.execute(
        "SELECT kind, local_paise, source_paise FROM reconciliation_exception"
        " WHERE object_id = 'ZPO-6'").fetchone()
    assert exc == ("FOREIGN_CURRENCY_BASIS_MISSING", None, None), (
        "no figure is booked in a paise column for an unbased face value")


@PG
@pytest.mark.pg
def test_live_jpy_order_with_an_active_rate_adopts_at_paise_via_the_shared_helper(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    # 100,000 minor JPY units (whole yen; JPY's exponent is 0) over 2 units.
    order = _order("ZPO-7", capex_ref="CAPEX-REF-7", currency="JPY",
                   total_paise=100000,
                   lines=(_line(external_line_id="ZPOL-7", qty="2",
                                rate_paise=50000, total_paise=100000),))
    adapter = _FakeAdapter({"ZPO-7": order})
    try:
        with database.session(_scope()) as session:
            fx.record_rate(session, from_currency="JPY", rate_date=DOC_DATE,
                           rate="0.55", rate_source="test advice", actor=ACTOR)
            _record_inbox(session, external_id="ZPO-7")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert result["adopted"] == 1
    header = con.execute(
        "SELECT currency, exchange_rate FROM purchase_order"
        " WHERE external_id = 'ZPO-7'").fetchone()
    assert header[0] == "JPY" and Decimal(str(header[1])) == Decimal("0.55")
    line = con.execute(
        "SELECT source_amount_minor, amount_paise FROM po_line pol"
        " JOIN purchase_order po ON po.po_id = pol.po_id"
        " WHERE po.external_id = 'ZPO-7'").fetchone()
    source_minor, paise = line
    assert source_minor == 100000

    # NEVER a hand-typed float: the expected paise comes from the SAME
    # helper `_translate_po_lines` calls, so this assertion would fail the
    # instant that helper's rounding rule changed instead of silently
    # agreeing with a duplicated one.
    basis = fx.TranslationBasis(
        source_currency="JPY", minor_exponent=0, rate=Decimal("0.55"),
        rate_date=DOC_DATE, rate_source="test advice", fx_rate_id=None)
    _header_paise, per_line = fx.translate_lines(basis, [100000])
    assert paise == per_line[0]


@PG
@pytest.mark.pg
def test_live_second_concurrent_adoption_of_the_same_order_is_blocked(
        pg_connection, pg_url, pg_disposable_db_name):
    """The advisory lock, proved the way `test_live_sweep_fable51.py` proves
    `_job_row_for`'s: two real sessions, released together by a barrier so
    they genuinely overlap, must resolve to exactly ONE adopted order."""
    con = pg_connection
    _seed(con)
    order = _order("ZPO-8", capex_ref="CAPEX-REF-8",
                   lines=(_line(external_line_id="ZPOL-8"),))
    with_session = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        with with_session.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-8")
    finally:
        with_session.close()

    database = scoped_role_database(pg_url, pg_disposable_db_name,
                                    min_size=2, max_size=2)
    barrier = threading.Barrier(2)
    results: list[tuple[dict | None, BaseException | None]] = [None, None]

    def attempt(index: int):
        def run() -> None:
            adapter = _FakeAdapter({"ZPO-8": order})
            barrier.wait(timeout=30)
            try:
                result = _adopt(database, con, adapter,
                                correlation_id=f"race-{index}")
                results[index] = (result, None)
            except BaseException as exc:            # noqa: BLE001 - reported
                results[index] = (None, exc)
        return run

    threads = [threading.Thread(target=attempt(i)) for i in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for thread in threads:
            assert not thread.is_alive(), (
                "a worker did not finish within 30s: the advisory lock is "
                "deadlocked or blocked indefinitely")
    finally:
        database.close()

    errors = [exc for _result, exc in results if exc is not None]
    assert not errors, errors
    adopted_counts = sorted(r["adopted"] for r, _exc in results)
    assert adopted_counts == [0, 1], (
        "exactly one of the two concurrent callers adopts the order; the "
        "other finds it already there and is idempotent")
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-8'"
    ).fetchone() == (1,)


@PG
@pytest.mark.pg
def test_live_grn_matching_then_bill_matching_against_the_adopted_order(
        pg_connection, pg_url, pg_disposable_db_name):
    """The point of the whole feature: the Zoho PO id is preserved as the
    EXTERNAL ANCHOR for a receive and a bill, exactly as it would be for an
    order this system had raised itself."""
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    order = _order("ZPO-9", capex_ref="CAPEX-REF-9",
                   lines=(_line(external_line_id="ZPOL-9", qty="2",
                                rate_paise=50000, total_paise=100000),))
    adapter = _FakeAdapter({"ZPO-9": order})
    try:
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-9")
        adopted = _adopt(database, con, adapter)
        assert adopted["adopted"] == 1

        # ---- GRN matching: SweepPoAnchored, driven directly.
        with database.session(_scope()) as session:
            connection = _connection_row(con)
            sweep_store = live_sweep.PgSweepStore(
                session=session, connection=connection, actor=ACTOR)
            receive = ReceiveDTO(
                source=_source_ref(), external_id="ZRCV-9",
                document_number="RCV-9", document_date=DOC_DATE,
                last_modified=T0, purchase_order_external_id="ZPO-9",
                external_status_raw="received",
                lines=(LineDTO(
                    external_line_id="ZRCVL-9", line_number=1, description="",
                    quantity="2", unit_price_paise=50000,
                    line_total_paise=100000, tax_paise=0,
                    purchase_order_line_external_id="ZPOL-9"),))

            class _ReceiveAdapter:
                def receives_for_po(self, po_external_id: str):
                    return [receive] if po_external_id == "ZPO-9" else []

            walk = sweeps.SweepPoAnchored(
                adapter=_ReceiveAdapter(), store=sweep_store,
                connection_id=CONN, external_source=SOURCE)
            ctx = jobs.JobContext(
                job_id="JOB-GRN-9", kind="sweep_po_anchored",
                correlation_id="c-grn-9", clock=jobs.SystemClock(),
                started_at=T0, soft_deadline_at=T0 + timedelta(seconds=30),
                checkpoint={}, resume_count=0, store=None,
                connection_id=CONN)
            list(walk.resume(ctx))

        grn_line = con.execute(
            "SELECT gl.amount_paise, gl.po_line_id FROM grn_line gl"
            " JOIN po_line pol ON pol.po_line_id = gl.po_line_id"
            " JOIN purchase_order po ON po.po_id = pol.po_id"
            " WHERE po.external_id = 'ZPO-9'").fetchone()
        assert grn_line is not None, (
            "the receive line must resolve against the ADOPTED po_line, "
            "proving the tenant's Zoho PO id anchors it")
        assert grn_line[0] == 100000
        assert con.execute(
            "SELECT count(*) FROM reconciliation_exception"
            " WHERE kind = 'GRN_LINE_UNATTRIBUTED'").fetchone() == (0,), (
            "the receive line must NOT quarantine")

        # ---- bill matching: pg.procurement.mirror_bill, directly.
        with database.session(_scope()) as session:
            mirrored = procurement.mirror_bill(
                session, external_source=SOURCE, external_id="ZBL-9",
                bill_number="BILL-9", vendor_name="Acme Cables",
                bill_date=DOC_DATE,
                lines=(LineDTO(
                    external_line_id="ZBLL-9", line_number=1, description="",
                    quantity="2", unit_price_paise=50000,
                    line_total_paise=100000, tax_paise=0,
                    purchase_order_line_external_id="ZPOL-9"),),
                po_external_id="ZPO-9", project_id=PROJECT, entity_id=ENTITY,
                external_status_raw="open", actor=ACTOR)
    finally:
        database.close()

    assert mirrored["attributed"] == 1 and mirrored.get("quarantined", 0) == 0
    bill_line = con.execute(
        "SELECT bl.amount_paise FROM bill_line bl"
        " JOIN po_line pol ON pol.po_line_id = bl.po_line_id"
        " JOIN purchase_order po ON po.po_id = pol.po_id"
        " WHERE po.external_id = 'ZPO-9'").fetchone()
    assert bill_line == (100000,)


# ==================================================================== LINK mode
@PG
@pytest.mark.pg
def test_live_link_mode_links_a_matching_local_order_once(
        pg_connection, pg_url, pg_disposable_db_name):
    """A tenant order whose cf_capex_ref equals a LOCAL order's own
    deterministic dedupe key is LINKED, not adopted as a second,
    EXTERNAL_UNSANCTIONED order -- and a repeat sweep is a no-op."""
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        po_id = _create_local_po(database)
        capex_ref = ob.derive_dedupe_key(CONN, psvc.PO_MODULE, po_id)
        order = _order("ZPO-10", capex_ref=capex_ref,
                       lines=(_line(external_line_id="ZPOL-10", qty="2",
                                    rate_paise=50000, total_paise=100000),))
        adapter = _FakeAdapter({"ZPO-10": order})
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-10")
            store.raise_exception(
                session, kind="UNSANCTIONED_COMMITMENT",
                object_type="purchase_order", object_id="ZPO-10",
                detail="raised by a prior poll", raised_at=T0,
                entity_id=ENTITY, actor="SVC-SWEEP")
        first = _adopt(database, con, adapter, correlation_id="c1")
        second = _adopt(database, con, adapter, correlation_id="c2")
    finally:
        database.close()

    assert first == {"adopted": 0, "linked": 1, "skipped": 0, "exceptions": 0,
                     "calls": 1, "exception_ids": []}
    assert second == {"adopted": 0, "linked": 0, "skipped": 1, "exceptions": 0,
                      "calls": 1, "exception_ids": []}

    row = con.execute(
        "SELECT commitment_origin, external_source, external_id,"
        " external_capex_ref, adopted_by FROM purchase_order"
        " WHERE po_id = %s", (po_id,)).fetchone()
    assert row == ("LOCAL", SOURCE, "ZPO-10", None, None), (
        "a linked order stays LOCAL, keeps external_capex_ref NULL "
        "(ck_purchase_order_adoption_provenance) and gains only its anchor")
    assert con.execute(
        "SELECT count(*) FROM purchase_order WHERE external_id = 'ZPO-10'"
    ).fetchone() == (1,), "no second, EXTERNAL_UNSANCTIONED order was created"

    exc = con.execute(
        "SELECT status, resolution_note FROM reconciliation_exception"
        " WHERE kind = 'UNSANCTIONED_COMMITMENT' AND object_id = 'ZPO-10'"
    ).fetchone()
    assert exc == ("Resolved", f"linked to {po_id}")
    # LINK stamps the tenant's line ids on the local lines, so the PO-anchored
    # receive walk can resolve a receive against a linked order (the first
    # post-deploy cycle quarantined the linked order's receive without this).
    assert con.execute(
        "SELECT line_external_id FROM po_line WHERE po_id = %s ORDER BY line_no",
        (po_id,)).fetchall() == [("ZPOL-10",)]
    # And a linked order whose line ids were lost (linked before the stamping
    # existed) gets them back on the next idempotent run, without re-linking.
    con.execute("UPDATE po_line SET line_external_id = NULL WHERE po_id = %s", (po_id,))
    con.commit()
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        third = _adopt(database, con, adapter, correlation_id="c3")
    finally:
        database.close()
    assert third["linked"] == 0 and third["adopted"] == 0
    assert con.execute(
        "SELECT line_external_id FROM po_line WHERE po_id = %s ORDER BY line_no",
        (po_id,)).fetchall() == [("ZPOL-10",)]


@PG
@pytest.mark.pg
def test_live_link_mode_mismatched_lines_conflict_and_do_not_link(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        po_id = _create_local_po(database, amount_paise=100000)
        capex_ref = ob.derive_dedupe_key(CONN, psvc.PO_MODULE, po_id)
        # A DIFFERENT total than the local order's 100000 paise.
        order = _order("ZPO-11", capex_ref=capex_ref,
                       lines=(_line(external_line_id="ZPOL-11", qty="2",
                                    rate_paise=60000, total_paise=120000),))
        adapter = _FakeAdapter({"ZPO-11": order})
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-11")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert (result["adopted"], result["linked"], result["exceptions"]) == (0, 0, 1)
    row = con.execute(
        "SELECT external_id FROM purchase_order WHERE po_id = %s",
        (po_id,)).fetchone()
    assert row == (None,), "the local order is left exactly as it was"
    exc = con.execute(
        "SELECT kind FROM reconciliation_exception WHERE object_id = 'ZPO-11'"
    ).fetchone()
    assert exc == ("ADOPTION_DIMENSION_CONFLICT",)


@PG
@pytest.mark.pg
def test_live_link_mode_an_unmatched_capex_ref_still_adopts_as_external_unsanctioned(
        pg_connection, pg_url, pg_disposable_db_name):
    """LINK mode changes nothing about the existing path: an order whose key
    matches no local order is adopted exactly as before LINK mode existed --
    even with an unrelated LOCAL order (no external_id) also present."""
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        _create_local_po(database)  # unrelated: a different dedupe key
        order = _order("ZPO-12", capex_ref="CAPEX-NO-LOCAL-MATCH",
                       lines=(_line(external_line_id="ZPOL-12"),))
        adapter = _FakeAdapter({"ZPO-12": order})
        with database.session(_scope()) as session:
            _record_inbox(session, external_id="ZPO-12")
        result = _adopt(database, con, adapter)
    finally:
        database.close()

    assert result == {"adopted": 1, "linked": 0, "skipped": 0, "exceptions": 0,
                      "calls": 1, "exception_ids": []}
    row = con.execute(
        "SELECT commitment_origin FROM purchase_order"
        " WHERE external_id = 'ZPO-12'").fetchone()
    assert row == ("EXTERNAL_UNSANCTIONED",)
