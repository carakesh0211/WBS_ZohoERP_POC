"""The live inbound sweep (Fable 5.1): the store mapping, the run, the route.

Three halves, and what each proves.

1. DATABASE-FREE. `PgSweepStore` is held to `sweeps.SweepStore` method by
   method -- every protocol method exists with the protocol's keyword
   parameters -- and the four mappings whose NAMES differ between the two
   sides (`upsert_inbox` -> `record_inbound`, `get_watermark` ->
   `read_watermark`, `set_watermark` -> `advance_watermark`, the two
   purchase-order lookups) are driven against a recording fake session so
   the statement each one issues, the table it names and the value it hands
   back are all asserted. `adapter_for_connection`'s four refusals, the
   connection-bound claim statement, and the `since=None` first-pull fix are
   proved here too.

2. LIVE POSTGRESQL, FAKE TENANT. `run_inbound_sweep` runs the real
   `sweeps.py` jobs on the real `jobs.py` runner against a disposable,
   migrated database through `capex_app` (RLS-subject), with a `Transport`
   whose `request()` answers canned ERP v3 JSON in the shape the tenant
   actually sends -- list rows carry `total` and NO `sub_total`, money as
   strings, `page_context.has_more_page`, a PO detail with
   `purchasereceives`, a receive with one attributable line and one ghost.
   Asserted after the run: inbox rows per module, watermarks advanced, one
   `grn_line`, one Open `GRN_LINE_UNATTRIBUTED` holding the ghost's full
   value, job rows returned to PENDING with their cursors, and that NO
   non-GET request ever reached the fake. Then the same run again:
   idempotent, no duplicate rows, the same exception id. Then a budget so
   small the run cannot finish: CHECKPOINTED / RATE_BUDGET_EXHAUSTED with the
   unfinished module's watermark exactly where it was. And a second
   connection's older job row, which the claim must NOT take.

   A SKIP IS NOT A PASS: this half skips without `CAPEX_DB_URL`.

3. THE ROUTE, in the guard-test style and with no database: 401 with no
   session, 401 with a forged one, 403 at the router floor, 403 for the
   read-only role, 409 `CONNECTION_NOT_LIVE` for the manage role on a MOCK
   connection, 503 `DATABASE_NOT_CONFIGURED` with no PostgreSQL, and the
   `MUTATING_ROUTES` row that keeps the authorisation matrix honest.
"""
from __future__ import annotations

import inspect
import json
import os
import sys as _sys
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from typing import Any, Mapping

import psycopg
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

from app.backend import auth  # noqa: E402
from app.backend.api import integrations as integrations_api  # noqa: E402
from app.backend.api import integrations_live  # noqa: E402
from app.backend.integration import adapter as ad  # noqa: E402
from app.backend.integration import erp, jobs, live_sweep, sweeps  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import repo  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL. A SKIP IS NOT A PASS.")

T0 = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)

ORG, ENTITY, PROJECT, HEAD = "ORG-LS", "ENT-LS", "PRJ-LS", "BH-LS"
WBS = "WBS-LS-A"
ACTOR = "U-LS-ADMIN"
CONN = "CONN-LS-LIVE"
CONN_MOCK = "CONN-LS-MOCK"
SOURCE = "ZOHO_ERP"
ORG_ID = "60000000123"
PO_EXT, POL_EXT, GHOST = "ZPO-1", "ZPOL-1", "ZPOL-GHOST"
RCV_EXT = "ZRCV-1"

LIVE_CONNECTION: dict[str, Any] = {
    "connection_id": CONN, "entity_id": ENTITY, "product": "ERP", "dc": "in",
    "organization_id": ORG_ID, "connector_name": "zoho_erp",
    "mode": "LIVE_READ", "per_minute_call_ceiling": 100,
    "daily_call_ceiling": 2000, "is_active": True,
}


# ============================================================ the fake tenant
def _page(key: str, rows: list[dict], *, page: int, has_more: bool) -> dict:
    return {"code": 0, "message": "success", key: rows,
            "page_context": {"page": page, "per_page": 50,
                             "has_more_page": has_more}}


CONTACT_ROWS = [
    {"contact_id": "ZCT-1", "contact_name": "Acme Cables", "status": "active",
     "contact_type": "vendor", "company_name": "Acme", "currency_code": "INR",
     "last_modified_time": "2026-09-10T09:00:00+0530"},
    {"contact_id": "ZCT-2", "contact_name": "Bharat Steel", "status": "active",
     "contact_type": "vendor", "company_name": "Bharat", "currency_code": "INR",
     "last_modified_time": "2026-09-10T09:05:00+0530"},
    {"contact_id": "ZCT-3", "contact_name": "Chennai Pumps", "status": "inactive",
     "contact_type": "vendor", "company_name": "Chennai", "currency_code": "INR",
     "last_modified_time": "2026-09-09T09:05:00+0530"},
]
ITEM_ROWS = [
    # `rate` as the STRING "0.0": what the first live items page proved Zoho
    # sends, and what `parse_float=str` keeps out of float arithmetic.
    {"item_id": "ZIT-1", "name": "Armoured cable", "status": "active",
     "rate": "0.0", "sku": "CAB-1", "currency_code": "INR",
     "last_modified_time": "2026-09-10T09:00:00+0530"},
]
PO_LIST_ROW = {
    # A LIST row: `total` and NO `sub_total` (verified live 2026-09-11).
    "purchaseorder_id": PO_EXT, "purchaseorder_number": "PO-00001",
    "date": "2026-09-01", "last_modified_time": "2026-09-10T10:00:00+0530",
    "vendor_id": "ZCT-1", "vendor_name": "Acme Cables", "currency_code": "INR",
    "total": "1000.00", "status": "issued",
}
PO_DETAIL_ROW = {
    **PO_LIST_ROW, "sub_total": "1000.00", "tax_total": "0.00",
    "line_items": [
        {"line_item_id": POL_EXT, "item_id": "ZIT-1", "description": "Cable",
         "quantity": "1", "rate": "1000.00", "item_total": "1000.00",
         "tax_total": "0.00"}],
    "purchasereceives": [{"receive_id": RCV_EXT, "receive_number": "RCV-0001"}],
}
RECEIVE_ROW = {
    "receive_id": RCV_EXT, "receive_number": "RCV-0001", "date": "2026-09-08",
    "last_modified_time": "2026-09-08T12:00:00+0530",
    "purchaseorder_id": PO_EXT, "status": "received",
    "line_items": [
        {"line_item_id": POL_EXT, "quantity": "1", "rate": "1000.00",
         "item_total": "1000.00"},
        # Documented is not populated: a line whose linkage names no local
        # po_line. Quarantined at FULL value, never spread.
        {"line_item_id": GHOST, "quantity": "2", "rate": "10.00",
         "item_total": "20.00"}],
}
BILL_LIST_ROW = {
    "bill_id": "ZBL-1", "bill_number": "BILL-0001", "date": "2026-09-09",
    "last_modified_time": "2026-09-10T11:00:00+0530",
    "vendor_id": "ZCT-1", "vendor_name": "Acme Cables", "currency_code": "INR",
    "total": "500.00", "status": "open", "purchaseorder_ids": [PO_EXT],
}


class FakeErpTransport:
    """A `Transport` answering canned ERP v3 JSON and recording every call.

    Raises on anything but GET -- and the tests assert the raise never
    happened, because a sweep that had tried to write would have failed its
    job rather than crashed the run, and only the transcript can prove the
    difference.
    """

    product = erp.PRODUCT

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.non_get: list[tuple[str, str]] = []

    def describe(self) -> dict[str, Any]:
        return {"product": self.product, "fake": True,
                "calls_made": len(self.requests)}

    def request(self, *, method: str, base_url: str, path: str, scope: str,
                params: Mapping[str, Any] | None = None,
                body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        self.requests.append((method, path, dict(params or {})))
        if method != "GET":
            self.non_get.append((method, path))
            raise AssertionError(f"a {method} reached the tenant: {path}")
        page = int((params or {}).get("page", 1))
        if path == erp.PATH_CONTACTS:
            if page == 1:
                return _page("contacts", CONTACT_ROWS[:2], page=1, has_more=True)
            return _page("contacts", CONTACT_ROWS[2:], page=2, has_more=False)
        if path == erp.PATH_ITEMS:
            return _page("items", ITEM_ROWS, page=1, has_more=False)
        if path == erp.PATH_PURCHASE_ORDERS:
            return _page("purchaseorders", [PO_LIST_ROW], page=1, has_more=False)
        if path == erp.PATH_BILLS:
            return _page("bills", [BILL_LIST_ROW], page=1, has_more=False)
        if path == erp.PATH_PURCHASE_ORDER.format(external_id=PO_EXT):
            return {"code": 0, "purchaseorder": PO_DETAIL_ROW}
        if path == erp.PATH_PURCHASE_RECEIVE.format(external_id=RCV_EXT):
            return {"code": 0, "purchasereceive": RECEIVE_ROW}
        raise AssertionError(f"unexpected path {path}")


class TickingClock:
    """One MILLISECOND per reading, so watermarks are deterministic and
    strictly ordered while the sweep's shared 20-second deadline -- which the
    runner reads the clock against many times per step -- is never consumed
    by the clock itself."""

    def __init__(self, start: datetime = T0) -> None:
        self.moment = start

    def now(self) -> datetime:
        self.moment = self.moment + timedelta(milliseconds=1)
        return self.moment


# ===================================================== 1. the store mapping
class _FakeSession:
    """Records every statement `repo.query` issues; answers a fixed row set."""

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.scope = Scope.system()
        self.statements: list[tuple[str, dict]] = []

    def fetchall(self, statement: str, params=None) -> list[tuple]:
        self.statements.append((statement, dict(params or {})))
        return self.rows

    def fetchone(self, statement: str, params=None):
        rows = self.fetchall(statement, params)
        return rows[0] if rows else None


def _protocol_methods(proto: type) -> dict[str, inspect.Signature]:
    return {name: inspect.signature(member)
            for name, member in vars(proto).items()
            if callable(member) and not name.startswith("_")}


def test_pg_sweep_store_implements_every_sweep_store_method_with_its_keywords():
    """Structural, and exact: the protocol's keyword-only parameters are the
    contract the sweeps call with, so a missing or renamed one is a TypeError
    at 3am inside a cron function."""
    expected = _protocol_methods(sweeps.SweepStore)
    assert expected, "the protocol has no methods; this test is vacuous"
    for name, proto_sig in expected.items():
        impl = getattr(live_sweep.PgSweepStore, name, None)
        assert impl is not None, f"PgSweepStore lacks SweepStore.{name}"
        impl_params = inspect.signature(impl).parameters
        for pname, param in proto_sig.parameters.items():
            if pname == "self":
                continue
            assert pname in impl_params, (
                f"PgSweepStore.{name} lacks parameter {pname!r}")
            assert impl_params[pname].kind == param.kind, (
                f"PgSweepStore.{name}({pname}) is {impl_params[pname].kind}, "
                f"the protocol says {param.kind}")


def _store_under_test(rows=None) -> tuple[live_sweep.PgSweepStore, _FakeSession]:
    session = _FakeSession(rows)
    return live_sweep.PgSweepStore(session=session, connection=LIVE_CONNECTION,
                                   actor=ACTOR, status_api_version="v3"), session


def test_upsert_inbox_maps_to_record_inbound_and_reports_creation():
    """`record_inbound` returns `(inbox_id, inserted)`; the protocol wants a
    bool that is True ONLY when a row was created. The one-row answer shape
    is `(connection_resolved, inbox_id)`: written -> True, duplicate -> False.
    """
    written, session = _store_under_test(rows=[(1, "INB-X")])
    assert written.upsert_inbox(
        connection_id=CONN, module="bills", external_id="ZBL-1",
        payload_sha="irrelevant", payload={"bill_id": "ZBL-1", "total": "1.00"},
        external_status_raw="open", received_at=T0, correlation_id="c-1") is True
    statement, params = session.statements[-1]
    assert store.INTEGRATION_INBOX in statement and "ON CONFLICT" in statement
    assert params["module"] == "bills" and params["external_id"] == "ZBL-1"
    assert params["status_raw"] == "open"
    assert params["status_product"] == "ERP" and params["status_api_version"] == "v3"
    assert params["correlation_id"] == "c-1" and params["now"] == T0
    assert written.created == {"bills": 1} and written.duplicates == {}

    duplicate, _ = _store_under_test(rows=[(1, None)])
    assert duplicate.upsert_inbox(
        connection_id=CONN, module="bills", external_id="ZBL-1",
        payload_sha="irrelevant", payload={"bill_id": "ZBL-1"},
        external_status_raw=None, received_at=T0, correlation_id=None) is False
    assert duplicate.duplicates == {"bills": 1}


def test_upsert_inbox_does_not_read_an_invisible_connection_as_a_duplicate():
    """The store raises CONNECTION_NOT_FOUND rather than answering
    `(None, False)`; the mapping must let that through, because a poller that
    read it as "already seen" would drop every payload on that connection."""
    invisible, _ = _store_under_test(rows=[(0, None)])
    with pytest.raises(store.IntegrationStoreError) as refused:
        invisible.upsert_inbox(
            connection_id="CONN-GHOST", module="bills", external_id="ZBL-1",
            payload_sha="x", payload={}, external_status_raw=None,
            received_at=T0, correlation_id=None)
    assert refused.value.code == "CONNECTION_NOT_FOUND"


def test_watermarks_map_to_read_and_advance_with_greatest_and_the_actor():
    hwm = T0 - timedelta(hours=1)
    present, session = _store_under_test(rows=[(hwm, 300, T0, 3, None, None)])
    assert present.get_watermark(connection_id=CONN, module="bills") == hwm
    assert store.INTEGRATION_WATERMARK in session.statements[-1][0]

    absent, _ = _store_under_test(rows=[])
    assert absent.get_watermark(connection_id=CONN, module="bills") is None

    advanced, session = _store_under_test(rows=[(T0,)])
    advanced.set_watermark(connection_id=CONN, module="bills", hwm=T0, now=T0)
    statement, params = session.statements[-1]
    assert "GREATEST(" in statement, "a watermark must never move backwards"
    assert params["hwm"] == T0 and params["actor"] == ACTOR
    assert params["module"] == "bills"


def test_open_purchase_orders_is_scoped_keyed_on_po_id_and_bound_to_the_source():
    """The PO-anchored walk's population: THIS connection's external source,
    an external id present, not Cancelled/Closed, the connection's entity,
    walked in `po_id` order from the cursor -- and inside `{scope}`."""
    found, session = _store_under_test(
        rows=[("PO-1", PO_EXT, ENTITY, PROJECT, "PO-00001")])
    batch = found.open_purchase_orders(connection_id=CONN, after_po_id="PO-0",
                                       limit=50)
    assert batch == [sweeps.LocalPurchaseOrder(
        po_id="PO-1", external_id=PO_EXT, entity_id=ENTITY,
        project_id=PROJECT, document_number="PO-00001")]
    statement, params = session.statements[-1]
    assert store.PURCHASE_ORDER in statement
    assert "po.external_source = %(source)s" in statement
    assert "po.external_id IS NOT NULL" in statement
    assert "ORDER BY po.po_id" in statement and "LIMIT %(limit)s" in statement
    assert params == {"source": SOURCE,
                      "terminal": list(live_sweep.PO_TERMINAL_STATUSES),
                      "entity_id": ENTITY, "after": "PO-0", "limit": 50}
    assert live_sweep.PO_TERMINAL_STATUSES == ("Cancelled", "Closed")


def test_known_purchase_order_answers_by_external_id_under_this_source():
    known, session = _store_under_test(rows=[(1,)])
    assert known.known_purchase_order(connection_id=CONN, external_id=PO_EXT) is True
    statement, params = session.statements[-1]
    assert "po.external_id = %(external_id)s" in statement
    assert params["source"] == SOURCE and params["external_id"] == PO_EXT
    unknown, _ = _store_under_test(rows=[])
    assert unknown.known_purchase_order(connection_id=CONN, external_id="nope") is False


def test_every_store_statement_this_module_issues_carries_the_scope_token():
    """`repo.query` refuses a statement without `{scope}`; these are the
    statements this module writes itself rather than delegates."""
    sweep_store, session = _store_under_test(rows=[])
    sweep_store.open_purchase_orders(connection_id=CONN, after_po_id=None, limit=1)
    sweep_store.known_purchase_order(connection_id=CONN, external_id="x")
    assert len(session.statements) == 2
    for statement, _ in session.statements:
        # The token has been compiled away by the time the session sees it;
        # the compiled predicate for a system scope is TRUE, and the source
        # is what carries the token. Both are checked.
        assert "TRUE" in statement
    source = inspect.getsource(live_sweep.PgSweepStore.open_purchase_orders)
    assert "{{scope}}" in source
    source = inspect.getsource(live_sweep.PgSweepStore.known_purchase_order)
    assert "{{scope}}" in source
    repo.require_scope_token(live_sweep.CLAIM_CONNECTION_JOB_SQL)


def test_the_control_total_surface_refuses_with_a_code_rather_than_an_empty_list():
    sweep_store, _ = _store_under_test()
    for name in ("periods_to_reconcile", "control_total_slices",
                 "local_control_total", "local_documents"):
        with pytest.raises(live_sweep.LiveSweepError) as refused:
            method = getattr(sweep_store, name)
            kwargs = {"connection_id": CONN} if name in (
                "periods_to_reconcile", "control_total_slices") else {"module": "bills"}
            if name != "periods_to_reconcile":
                kwargs["period"] = None
            if name == "local_control_total":
                kwargs["status"] = "x"
            method(**kwargs)
        assert refused.value.code == "SWEEP_STORE_UNSUPPORTED"
        assert refused.value.status == 501


# ============================================== 2. adapter and claim binding
def test_adapter_for_connection_answers_none_for_everything_that_is_not_live_erp_in():
    fake = FakeErpTransport()
    for connection in (
        {**LIVE_CONNECTION, "mode": "MOCK"},
        {**LIVE_CONNECTION, "mode": "SANDBOX"},
        {**LIVE_CONNECTION, "product": "BOOKS_INVENTORY"},
        {**LIVE_CONNECTION, "dc": "us"},
    ):
        assert live_sweep.adapter_for_connection(connection, transport=fake) is None, connection
    for mode in store.LIVE_MODES:
        adapter = live_sweep.adapter_for_connection(
            {**LIVE_CONNECTION, "mode": mode}, transport=fake)
        assert isinstance(adapter, erp.ErpAdapter)
        assert adapter.organization_id == ORG_ID
        assert adapter.transport is fake
        assert adapter.capabilities().daily_call_ceiling == 2000
        assert adapter.capabilities().receives_listable is False
    assert live_sweep.EXTERNAL_SOURCE_BY_PRODUCT == {"ERP": "ZOHO_ERP"}


def test_adapter_for_connection_builds_a_live_transport_and_it_refuses_without_a_credential(
        monkeypatch, tmp_path):
    """No transport injected means the real one -- and the real one reads
    its credential from outside the repository and refuses to exist without
    it. Nothing is guessed and no default tenant exists."""
    monkeypatch.setenv("CAPEX_ERP_CREDENTIALS", str(tmp_path / "absent.json"))
    monkeypatch.delenv("CAPEX_ERP_REFRESH_TOKEN", raising=False)
    with pytest.raises(ad.IntegrationError) as refused:
        live_sweep.adapter_for_connection(LIVE_CONNECTION)
    text = str(refused.value)
    assert "credential" in text.lower()
    assert "nothing is read from the repository" in text


def test_the_connection_bound_claim_is_derived_from_the_framework_statement():
    """One connection's sweep must never claim another's job row. The bound
    statement is the framework's own with one predicate added; everything
    the framework's tests hold about the claim still holds here."""
    bound = live_sweep.CLAIM_CONNECTION_JOB_SQL
    assert "AND connection_id = %(connection_id)s" in bound
    assert bound.replace(
        "\n       AND connection_id = %(connection_id)s", "") == jobs.CLAIM_JOB_SQL
    assert "FOR UPDATE" in bound and "SKIP LOCKED" in bound
    assert issubclass(live_sweep.ConnectionJobStore, jobs.PostgresJobStore)

    session = _FakeSession(rows=[("JOB-1", "poll_bills", {"page": 3}, 2,
                                  "corr-1", CONN)])
    claim = live_sweep.ConnectionJobStore(session, connection_id=CONN).claim_job(
        kind="poll_bills", now=T0, lease_seconds=1200,
        soft_deadline_at=T0 + timedelta(seconds=20))
    statement, params = session.statements[0]
    assert params["connection_id"] == CONN and params["kind"] == "poll_bills"
    assert "AND connection_id = %(connection_id)s" in statement
    assert claim == jobs.ClaimedJob(job_id="JOB-1", kind="poll_bills",
                                    checkpoint={"page": 3}, resume_count=2,
                                    correlation_id="corr-1", connection_id=CONN)


def test_a_first_pull_with_no_watermark_sends_no_filter_instead_of_crashing():
    """`plan_window` answers "no watermark yet" with `since=None`; before this
    branch `erp._format_delta(None)` raised AttributeError on the very first
    live sweep of a fresh connection. A full pull sends no filter."""
    assert ad.window_params(enabled=True, since=None, parameter="last_modified_time",
                            formatter=erp._format_delta) == {}
    assert ad.window_params(enabled=True, since=T0, parameter="last_modified_time",
                            formatter=erp._format_delta) == {
        "last_modified_time": "2026-09-12T06:00:00+0000"}
    fake = FakeErpTransport()
    adapter = erp.ErpAdapter(organization_id=ORG_ID, transport=fake)
    page = adapter.list_bills(since=None, until=T0 + timedelta(days=1), page=1)
    assert [b.external_id for b in page.items] == ["ZBL-1"]
    _method, path, params = fake.requests[-1]
    assert path == erp.PATH_BILLS
    assert "last_modified_time" not in params
    assert params["organization_id"] == ORG_ID


class _UntouchableSession:
    """A session any use of which is a test failure."""

    def __getattr__(self, name):  # pragma: no cover - the assertion
        raise AssertionError(f"the session was touched ({name}) before the "
                             f"mode check refused the sweep")


def test_run_inbound_sweep_refuses_a_non_live_connection_before_touching_anything():
    for mode in ("MOCK", "SANDBOX"):
        with pytest.raises(live_sweep.LiveSweepError) as refused:
            live_sweep.run_inbound_sweep(
                _UntouchableSession(), connection={**LIVE_CONNECTION, "mode": mode},
                actor=ACTOR, correlation_id="c", transport=FakeErpTransport())
        assert refused.value.code == "CONNECTION_NOT_LIVE"
        assert refused.value.status == 409


def test_run_inbound_sweep_refuses_a_product_with_no_live_transport():
    with pytest.raises(live_sweep.LiveSweepError) as refused:
        live_sweep.run_inbound_sweep(
            _UntouchableSession(),
            connection={**LIVE_CONNECTION, "product": "BOOKS_INVENTORY"},
            actor=ACTOR, correlation_id="c", transport=FakeErpTransport())
    assert refused.value.code == "LIVE_ADAPTER_UNAVAILABLE"
    assert refused.value.status == 503


def test_run_inbound_sweep_refuses_an_unknown_module_before_touching_anything():
    with pytest.raises(live_sweep.LiveSweepError) as refused:
        live_sweep.run_inbound_sweep(
            _UntouchableSession(), connection=LIVE_CONNECTION, actor=ACTOR,
            correlation_id="c", modules=["bills", "invoices"],
            transport=FakeErpTransport())
    assert refused.value.code == "UNKNOWN_SWEEP_MODULE"
    assert refused.value.status == 422


def test_the_module_never_spells_a_product_endpoint_scope_or_host():
    """`tests/test_integration_no_hardcoded_endpoints.py` walks the whole
    backend; this is the same property asserted at the door of the one file
    most tempted to break it."""
    source = _Path(live_sweep.__file__).read_text(encoding="utf-8")
    for literal in ('"/bills"', '"/purchaseorders"', '"/purchasereceives"',
                    "zohoapis", "ERP.bills", "ERP.purchaseorders"):
        assert literal not in source, literal


# ============================================ 3. live PostgreSQL, fake tenant
def _seed(con: psycopg.Connection, *, connection_id: str = CONN,
          mode: str = "LIVE_READ", daily_ceiling: int = 2000,
          minute_ceiling: int = 100, organization_id: str = ORG_ID) -> None:
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by,"
       " updated_by) VALUES (%s, 'ORGLS', 'LS Org', 'T', 'T')"
       " ON CONFLICT DO NOTHING", (ORG,))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
       " ON CONFLICT DO NOTHING", (ENTITY, ORG, ENTITY, ENTITY))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'Released', 'T', 'T')"
       " ON CONFLICT DO NOTHING", (PROJECT, ENTITY, PROJECT, PROJECT))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
       " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
       " ON CONFLICT DO NOTHING", (HEAD, ENTITY, HEAD, HEAD))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description,"
       " wbs_path, status, created_by, updated_by)"
       " VALUES (%s, %s, %s, %s, %s, 'Released', 'T', 'T') ON CONFLICT DO NOTHING",
       (WBS, PROJECT, WBS, WBS, "lsa"))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise,"
       " updated_by) VALUES (%s, %s, 100000000, 'T') ON CONFLICT DO NOTHING",
       (WBS, HEAD))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by)"
       " VALUES (%s, %s, 'T') ON CONFLICT DO NOTHING", (WBS, HEAD))
    ex("INSERT INTO app_user (user_id, email, display_name, principal_kind,"
       " created_by, updated_by) VALUES (%s, 'ls-admin@example.test',"
       " 'LS admin', 'USER', 'T', 'T') ON CONFLICT DO NOTHING", (ACTOR,))
    # The mirrored purchase order the receives walk anchors on, and its one
    # line carrying the external line id the receive's first line names.
    ex("INSERT INTO purchase_order (po_id, po_number, project_id, vendor_name,"
       " currency, status, ordered_at, external_source, external_id,"
       " created_by, updated_by)"
       " VALUES ('PO-LS-1', 'PO-NUM-LS-1', %s, 'Acme Cables', 'INR',"
       " 'Released', %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
       (PROJECT, T0, SOURCE, PO_EXT))
    ex("INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
       " budget_head_id, rate_paise, amount_paise, line_external_id,"
       " created_by, updated_by)"
       " VALUES ('POL-LS-1', 'PO-LS-1', 1, %s, %s, %s, 100000, 100000, %s,"
       " 'T', 'T') ON CONFLICT DO NOTHING", (PROJECT, WBS, HEAD, POL_EXT))
    live = mode in store.LIVE_MODES
    ex("INSERT INTO integration_connection (connection_id, entity_id, product,"
       " dc, organization_id, connector_name, mode, per_minute_call_ceiling,"
       " daily_call_ceiling, live_authorised_at, live_authorised_by,"
       " live_authorisation_note, created_by, updated_by)"
       " VALUES (%s, %s, 'ERP', 'in', %s, 'zoho_erp', %s, %s, %s, %s, %s, %s,"
       " 'T', 'T')",
       (connection_id, ENTITY, organization_id, mode, minute_ceiling,
        daily_ceiling, T0 if live else None, "U-LS-CFO" if live else None,
        "Read-only demo sweep agreed 2026-09-12" if live else None))
    con.commit()


def _connection_row(con: psycopg.Connection, connection_id: str = CONN) -> dict:
    row = con.execute(
        "SELECT connection_id, entity_id, product, dc, organization_id,"
        " connector_name, mode, per_minute_call_ceiling, daily_call_ceiling,"
        " is_active FROM integration_connection WHERE connection_id = %s",
        (connection_id,)).fetchone()
    return store._connection_row(row)


def _scope() -> Scope:
    """RESTRICTED to the seeded entity, as the route would resolve it. Never
    `Scope.system()`: the run must succeed under RLS as `capex_app`."""
    return Scope(user_id=ACTOR, principal_kind="USER",
                 entity_ids=frozenset({ENTITY}), plant_ids=None,
                 project_ids=None, location_ids=None, read_all=False)


def _run(database, con, fake, clock, *, connection_id: str = CONN, **kwargs):
    connection = _connection_row(con, connection_id)
    with database.session(_scope()) as session:
        return live_sweep.run_inbound_sweep(
            session, connection=connection, actor=ACTOR,
            correlation_id=f"ls-{clock.moment.isoformat()}", transport=fake,
            clock=clock, **kwargs)


def _count(con, sql: str, params=()) -> int:
    return int(con.execute(sql, params).fetchone()[0])


@PG
@pytest.mark.pg
def test_live_a_live_read_connection_sweeps_the_tenant_into_the_inbox(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    fake, clock = FakeErpTransport(), TickingClock()
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        result = _run(database, con, fake, clock)
    finally:
        database.close()

    # ---- the transcript: GET only, and the calls the walk actually costs
    assert fake.non_get == [], fake.non_get
    assert all(m == "GET" for m, _p, _q in fake.requests)
    paths = [p for _m, p, _q in fake.requests]
    assert paths.count(erp.PATH_CONTACTS) == 2, "two contact pages"
    assert paths.count(erp.PATH_ITEMS) == 1
    assert paths.count(erp.PATH_PURCHASE_ORDERS) == 1
    assert paths.count(erp.PATH_BILLS) == 1
    assert paths.count(erp.PATH_PURCHASE_ORDER.format(external_id=PO_EXT)) == 1
    assert paths.count(erp.PATH_PURCHASE_RECEIVE.format(external_id=RCV_EXT)) == 1
    assert all(q.get("organization_id") == ORG_ID for _m, _p, q in fake.requests)

    # ---- every module ran to DONE through the runner
    modules = result["modules"]
    assert list(modules) == list(live_sweep.DEFAULT_MODULES)
    for name, module in modules.items():
        assert module["claimed"] is True, name
        assert module["state"] == jobs.JOB_DONE, (name, module)
        assert module["reason"] == jobs.STOP_COMPLETED, (name, module)
        assert module["error"] is None, (name, module)
    assert modules["contacts"]["records_seen"] == 3
    assert modules["contacts"]["inbox_created"] == 3
    assert modules["contacts"]["pages"] == 2
    assert modules["contacts"]["calls"] == 2
    assert modules["items"]["inbox_created"] == 1
    assert modules["purchaseorders"]["inbox_created"] == 1
    assert modules["bills"]["inbox_created"] == 1
    assert modules["receives"]["inbox_created"] == 1
    assert modules["receives"]["calls"] == 1, "one call charged per open PO"
    assert modules["receives"]["checkpoint"]["last_po_id_swept"] == "PO-LS-1"
    assert modules["receives"]["checkpoint"]["sole_grn_mechanism"] is True
    assert result["receives_strategy"] == "PO_ANCHORED"
    assert result["external_source"] == SOURCE
    assert result["transport"] == {"product": "ERP", "fake": True,
                                   "calls_made": len(fake.requests)}
    assert result["budget"]["asked"] is True and result["budget"]["refusals"] == 0
    # REPORTED, NOT SMOOTHED OVER. Seven requests left the machine and SIX
    # were charged: `SweepPoAnchored` asks the budget for one call per open
    # PO, but `ErpAdapter.receives_for_po` spends `1 + len(receives)` -- the
    # PO detail plus one fetch per receive it names -- so the POLLING lane's
    # daily figure under-counts by one per receive. The frozen C1 seam gives
    # the sweep no way to see the receive count before the detail call. A
    # finding for stream 1 / stream 4; the transport's own sliding-minute
    # ceiling counts every request regardless.
    assert len(fake.requests) == 7
    assert result["budget"]["windows"]["DAY"]["used"] == 6
    assert result["budget"]["windows"]["MINUTE"]["used"] == 6
    assert result["skipped_for_deadline"] == [] and result["dead_jobs"] == {}

    # ---- the inbox, per module, verbatim payloads with the raw status
    rows = con.execute(
        "SELECT module, external_id, external_status_raw,"
        " external_status_product, external_status_api_version, payload"
        " FROM integration_inbox WHERE connection_id = %s"
        " ORDER BY module, external_id", (CONN,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("bills", "ZBL-1", "open"),
        ("contacts", "ZCT-1", "active"), ("contacts", "ZCT-2", "active"),
        ("contacts", "ZCT-3", "inactive"),
        ("items", "ZIT-1", "active"),
        ("purchaseorders", PO_EXT, "issued"),
        ("purchasereceives", RCV_EXT, "received"),
    ]
    assert {(r[3], r[4]) for r in rows} == {("ERP", erp.API_VERSION)}
    po_payload = next(r[5] for r in rows if r[0] == "purchaseorders")
    assert po_payload["total"] == "1000.00" and "sub_total" not in po_payload, (
        "the list row is preserved as received: money as a string, no sub_total")
    item_payload = next(r[5] for r in rows if r[0] == "items")
    assert item_payload["rate"] == "0.0"

    # ---- the watermarks advanced, and only for the four windowed modules
    marks = dict(con.execute(
        "SELECT module, hwm FROM integration_watermark WHERE connection_id = %s",
        (CONN,)).fetchall())
    assert set(marks) == {"contacts", "items", "purchaseorders", "bills"}
    assert all(hwm > T0 for hwm in marks.values())
    assert result["watermarks"] == {m: jobs.iso(h) for m, h in marks.items()}

    # ---- the receive: one line attributed, one quarantined at full value
    grn_lines = con.execute(
        "SELECT po_line_id, receive_external_id, line_external_id, amount_paise"
        " FROM grn_line").fetchall()
    assert grn_lines == [("POL-LS-1", RCV_EXT, POL_EXT, 100000)]
    assert result["receive_lines_recorded"] == 1
    grn = con.execute(
        "SELECT external_source, external_id, payload_sha FROM grn").fetchall()
    assert len(grn) == 1 and grn[0][0] == SOURCE and grn[0][1] == RCV_EXT
    assert grn[0][2] is not None, "provenance: the version of the source document"
    exceptions = con.execute(
        "SELECT exception_id, kind, object_type, object_id, status, entity_id,"
        " project_id, source_paise FROM reconciliation_exception").fetchall()
    assert len(exceptions) == 1
    xid, kind, otype, oid, status, ent, prj, paise = exceptions[0]
    assert (kind, otype, oid, status) == (
        sweeps.KIND_GRN_LINE_UNATTRIBUTED, "grn_line", f"{RCV_EXT}:{GHOST}", "Open")
    assert (ent, prj) == (ENTITY, PROJECT)
    assert paise == 2000, "Rs 20.00 held at FULL value, never spread"
    assert result["exceptions"] == {"raised": 1, "ids": [xid]}

    # ---- the job rows: one per kind, back to PENDING with their cursors
    job_rows = con.execute(
        "SELECT kind, state, connection_id, principal_user_id, checkpoint"
        " FROM job WHERE connection_id = %s ORDER BY kind", (CONN,)).fetchall()
    assert [(r[0], r[1], r[2], r[3]) for r in job_rows] == [
        (k, jobs.JOB_PENDING, CONN, ACTOR)
        for k in sorted(("poll_contacts", "poll_items", "poll_purchaseorders",
                         "poll_bills", "sweep_po_anchored"))]
    po_walk = next(r[4] for r in job_rows if r[0] == "sweep_po_anchored")
    assert po_walk["last_po_id_swept"] == "PO-LS-1"
    events = dict(con.execute(
        "SELECT kind, count(*) FROM integration_event WHERE connection_id = %s"
        " GROUP BY kind", (CONN,)).fetchall())
    assert events.get("JOB_DONE") == 5
    # The exception's own event hangs off the entity, not the connection:
    # `raise_exception` records it with no connection_id, so it is looked for
    # by kind and correlation rather than through the connection.
    raised = con.execute(
        "SELECT correlation_id, detail FROM integration_event"
        " WHERE kind = 'reconciliation.exception.raised'").fetchall()
    assert len(raised) == 1
    assert raised[0][0] == result["correlation_id"]
    assert raised[0][1]["exception_id"] == xid
    assert raised[0][1]["source_paise_signed"] == 2000


@PG
@pytest.mark.pg
def test_live_re_running_the_sweep_creates_nothing_twice(
        pg_connection, pg_url, pg_disposable_db_name):
    """Idempotency is the inbox constraint and the grn_line unique index, not
    a flag. The second run re-reads everything (full pulls on masters, the
    300-second overlap on documents) and creates nothing; the third run has
    cycled the PO walk and re-reads the receive, and STILL creates nothing."""
    con = pg_connection
    _seed(con)
    fake, clock = FakeErpTransport(), TickingClock()
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        first = _run(database, con, fake, clock)
        inbox_after_first = _count(con, "SELECT count(*) FROM integration_inbox")
        marks_after_first = dict(con.execute(
            "SELECT module, hwm FROM integration_watermark").fetchall())
        second = _run(database, con, fake, clock)
        third = _run(database, con, fake, clock)
    finally:
        database.close()

    assert fake.non_get == []
    assert inbox_after_first == 7
    assert _count(con, "SELECT count(*) FROM integration_inbox") == 7
    for name, module in second["modules"].items():
        assert module["state"] == jobs.JOB_DONE, (name, module)
        assert module["inbox_created"] == 0, (name, module)
    assert second["modules"]["contacts"]["inbox_duplicates"] == 3
    assert second["modules"]["bills"]["inbox_duplicates"] == 1
    # The second run found the walk off the end of the population: it
    # reports the cycle and spends no call; the third re-walks the one PO.
    assert second["modules"]["receives"]["calls"] == 0
    assert second["modules"]["receives"]["checkpoint"]["cycle"] == 1
    assert third["modules"]["receives"]["calls"] == 1
    assert third["modules"]["receives"]["inbox_duplicates"] == 1

    assert _count(con, "SELECT count(*) FROM grn_line") == 1
    assert _count(con, "SELECT count(*) FROM grn") == 1
    assert _count(con, "SELECT count(*) FROM reconciliation_exception") == 1
    assert third["exceptions"]["ids"] == first["exceptions"]["ids"], (
        "the re-walk returns the SAME Open exception, not a second one")
    assert _count(con, "SELECT count(*) FROM reconciliation_exception"
                       " WHERE source_paise = 2000") == 1, "SET, never +="
    marks_after_third = dict(con.execute(
        "SELECT module, hwm FROM integration_watermark").fetchall())
    for module, hwm in marks_after_third.items():
        assert hwm > marks_after_first[module], module
    assert _count(con, "SELECT count(*) FROM job WHERE connection_id = %s", (CONN,)) == 5


@PG
@pytest.mark.pg
def test_live_an_exhausted_polling_budget_checkpoints_and_holds_the_watermark(
        pg_connection, pg_url, pg_disposable_db_name):
    """Plan §11.6 through the framework: ceilings of 5 give POLLING 60% ->
    3 calls in each window (`ck_integration_connection_daily_exceeds_minute`
    holds daily >= per-minute, so both are 5 and both lanes are 3). Contacts
    spends 2, items 1, and the PO poll is refused BEFORE its first request:
    CHECKPOINTED / RATE_BUDGET_EXHAUSTED naming DAY -- the binding window on
    ERP Standard, named first when both refuse -- its watermark absent, and
    no request for it in the transcript."""
    con = pg_connection
    _seed(con, daily_ceiling=5, minute_ceiling=5)
    fake, clock = FakeErpTransport(), TickingClock()
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        result = _run(database, con, fake, clock)
    finally:
        database.close()

    assert fake.non_get == []
    modules = result["modules"]
    assert modules["contacts"]["state"] == jobs.JOB_DONE
    assert modules["items"]["state"] == jobs.JOB_DONE
    for name in ("purchaseorders", "bills", "receives"):
        assert modules[name]["state"] == jobs.JOB_CHECKPOINTED, (name, modules[name])
        assert modules[name]["reason"] == jobs.STOP_RATE_BUDGET, (name, modules[name])
        assert modules[name]["calls"] == 0
    assert erp.PATH_PURCHASE_ORDERS not in [p for _m, p, _q in fake.requests]
    assert result["budget"]["refusals"] == 3
    assert result["budget"]["binding"] == "DAY"
    marks = dict(con.execute(
        "SELECT module, hwm FROM integration_watermark").fetchall())
    assert set(marks) == {"contacts", "items"}, (
        "an unfinished window must not move its watermark")
    states = dict(con.execute(
        "SELECT kind, state FROM job WHERE connection_id = %s", (CONN,)).fetchall())
    assert states["poll_purchaseorders"] == jobs.JOB_CHECKPOINTED
    assert states["poll_contacts"] == jobs.JOB_PENDING
    assert _count(con, "SELECT count(*) FROM integration_inbox") == 4


@PG
@pytest.mark.pg
def test_live_the_sweep_claims_only_its_own_connections_job_rows(
        pg_connection, pg_url, pg_disposable_db_name):
    """Two live connections; the OTHER one holds an older PENDING
    `poll_contacts` row. The framework's claim is by kind alone and would
    take it; the connection-bound claim must leave it exactly as it was."""
    con = pg_connection
    _seed(con)
    # A second organisation: C2 holds UNIQUE (entity_id, organization_id).
    _seed(con, connection_id="CONN-LS-OTHER", organization_id="60000000999")
    con.execute(
        "INSERT INTO job (job_id, kind, state, connection_id, entity_id,"
        " principal_user_id, created_at, created_by, updated_at, updated_by)"
        " VALUES ('JOB-OTHER', 'poll_contacts', 'PENDING', 'CONN-LS-OTHER', %s,"
        " %s, %s, 'T', %s, 'T')", (ENTITY, ACTOR, T0 - timedelta(days=1),
                                   T0 - timedelta(days=1)))
    con.commit()
    fake, clock = FakeErpTransport(), TickingClock()
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        result = _run(database, con, fake, clock, modules=["contacts"])
    finally:
        database.close()

    assert result["modules"]["contacts"]["state"] == jobs.JOB_DONE
    assert result["modules"]["contacts"]["job_id"] != "JOB-OTHER"
    other = con.execute(
        "SELECT state, locked_by, checkpoint FROM job WHERE job_id = 'JOB-OTHER'"
    ).fetchone()
    assert other == ("PENDING", None, None), other
    mine = con.execute(
        "SELECT connection_id FROM job WHERE job_id = %s",
        (result["modules"]["contacts"]["job_id"],)).fetchone()
    assert mine == (CONN,)


@PG
@pytest.mark.pg
def test_live_a_caller_without_an_app_user_row_cannot_own_a_job(
        pg_connection, pg_url, pg_disposable_db_name):
    con = pg_connection
    _seed(con)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        with database.session(Scope(user_id="U-NOBODY", principal_kind="USER",
                                    entity_ids=frozenset({ENTITY}))) as session:
            with pytest.raises(live_sweep.LiveSweepError) as refused:
                live_sweep.run_inbound_sweep(
                    session, connection=_connection_row(con), actor="U-NOBODY",
                    correlation_id="c", transport=FakeErpTransport(),
                    clock=TickingClock())
    finally:
        database.close()
    assert refused.value.code == "PRINCIPAL_NOT_PROVISIONED"
    assert refused.value.status == 409
    assert _count(con, "SELECT count(*) FROM job") == 0


# ================================================================ 4. the route
def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(integrations_live.router)
    return app


def _client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app or _app(), raise_server_exceptions=False)


SWEEP_URL = f"/api/integrations/connections/{CONN_MOCK}/sweep"


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    return capex_db


def _detail(response) -> dict:
    body = response.json()
    assert isinstance(body, dict) and "detail" in body, body
    return body["detail"]


def test_route_the_guard_is_on_the_router_and_reused_not_restated():
    assert integrations_live.router.dependencies, "no router-level dependency"
    guards = [d.dependency for d in integrations_live.router.dependencies]
    assert integrations_api.require_integration_access in guards
    routes = [r for r in integrations_live.router.routes
              if getattr(r, "path", "") == "/api/integrations/connections/{connection_id}/sweep"]
    assert len(routes) == 1 and routes[0].methods == {"POST"}
    source = _Path(integrations_live.__file__).read_text(encoding="utf-8")
    for helper in ("_requires", "_session", "_actor", "_problem",
                   "_require_visible_connection", "_get_database",
                   "_set_correlation_header", "_store_error_to_http"):
        assert f"_integrations.{helper}" in source, helper
        assert f"\ndef {helper}(" not in source, f"{helper} was copied, not reused"


def test_route_is_in_the_authorisation_matrix_with_manage_and_a_denied_role():
    from test_api_auth import MUTATING_ROUTES
    rows = [r for r in MUTATING_ROUTES
            if r[0] == "/api/integrations/connections/{connection_id}/sweep"]
    assert len(rows) == 1
    template, method, _url, _body, permission, denied = rows[0]
    assert method == "POST" and permission == "connector.manage"
    assert denied == "Auditor"
    assert "Auditor" not in auth.PERMISSIONS["connector.manage"]
    assert "Auditor" in auth.PERMISSIONS["connector.read"]


def test_route_an_unauthenticated_caller_is_refused():
    resp = _client().post(SWEEP_URL, json={})
    assert resp.status_code == 401 and _detail(resp)["code"] == "NOT_AUTHENTICATED"
    resp = _client().post(SWEEP_URL, json={}, headers={"X-Session": "forged"})
    assert resp.status_code == 401 and _detail(resp)["code"] == "SESSION_INVALID"


def test_route_a_role_without_connector_read_stops_at_the_floor(make_user):
    caller = make_user(["Requestor"])
    resp = _client().post(SWEEP_URL, json={},
                          headers={"X-Session": caller.session_id})
    assert resp.status_code == 403 and _detail(resp)["code"] == "FORBIDDEN"


def test_route_the_read_only_role_is_refused_the_sweep(make_user):
    caller = make_user(["Auditor"])
    resp = _client().post(SWEEP_URL, json={},
                          headers={"X-Session": caller.session_id})
    assert resp.status_code == 403 and _detail(resp)["code"] == "FORBIDDEN"


def test_route_without_a_database_answers_the_coded_503(make_user, monkeypatch):
    """The dependency degrades to 503 DATABASE_NOT_CONFIGURED with the
    `unavailable` envelope, before any connection lookup."""
    from app.backend.pg import engine
    # `main.py` never installs a database; a PG-backed test elsewhere in the
    # same process restores None in its `finally`. Pinned rather than assumed.
    monkeypatch.setattr(engine, "_database", None)
    caller = make_user(["Administrator"])
    resp = _client().post(SWEEP_URL, json={},
                          headers={"X-Session": caller.session_id})
    assert resp.status_code == 503, resp.text
    detail = _detail(resp)
    assert detail["code"] == "DATABASE_NOT_CONFIGURED"
    assert detail["unavailable"] is True and detail["missing"]


@pytest.fixture()
def _mock_connection_app(monkeypatch):
    """The manage role reaching the mode check: the database dependency is
    satisfied and the visible-connection lookup answers a MOCK row."""
    app = _app()
    app.dependency_overrides[integrations_api._get_database] = lambda: None
    seen: list[str] = []

    def _visible(request, database, connection_id):
        seen.append(connection_id)
        return {**LIVE_CONNECTION, "connection_id": connection_id, "mode": "MOCK"}

    monkeypatch.setattr(integrations_api, "_require_visible_connection", _visible)
    app.state.seen = seen
    return app


def test_route_the_manage_role_gets_409_on_a_mock_connection(
        make_user, _mock_connection_app):
    caller = make_user(["Administrator"])
    client = TestClient(_mock_connection_app, raise_server_exceptions=False)
    resp = client.post(SWEEP_URL, json={},
                       headers={"X-Session": caller.session_id})
    assert resp.status_code == 409, resp.text
    detail = _detail(resp)
    assert detail["code"] == "CONNECTION_NOT_LIVE"
    assert "MOCK" in detail["detail"]
    assert detail["message_id"] is None
    assert _mock_connection_app.state.seen == [CONN_MOCK]
    # A body naming modules is accepted; an unexpected field is not.
    resp = client.post(SWEEP_URL, json={"modules": ["bills"]},
                       headers={"X-Session": caller.session_id})
    assert resp.status_code == 409
    resp = client.post(SWEEP_URL, json={"module": "bills"},
                       headers={"X-Session": caller.session_id})
    assert resp.status_code == 422


def test_route_an_invisible_connection_is_404_not_403(make_user, monkeypatch):
    app = _app()
    app.dependency_overrides[integrations_api._get_database] = lambda: None

    def _invisible(request, database, connection_id):
        raise integrations_api._problem(
            404, "CONNECTION_NOT_FOUND", "Connection Not Found",
            f"No connection {connection_id} is visible to you.")

    monkeypatch.setattr(integrations_api, "_require_visible_connection", _invisible)
    caller = make_user(["Administrator"])
    resp = TestClient(app, raise_server_exceptions=False).post(
        SWEEP_URL, json={}, headers={"X-Session": caller.session_id})
    assert resp.status_code == 404 and _detail(resp)["code"] == "CONNECTION_NOT_FOUND"


def test_route_a_live_connection_with_no_credential_is_a_coded_503(
        make_user, monkeypatch, tmp_path):
    """LIVE_READ, visible, and no credential anywhere: the refusal is
    `LiveTransport`'s, mapped to 503 LIVE_TRANSPORT_UNAVAILABLE, and the
    session is never opened because the adapter is built first."""
    app = _app()
    app.dependency_overrides[integrations_api._get_database] = lambda: None
    monkeypatch.setattr(integrations_api, "_require_visible_connection",
                        lambda request, database, connection_id: dict(LIVE_CONNECTION))
    monkeypatch.setenv("CAPEX_ERP_CREDENTIALS", str(tmp_path / "absent.json"))
    monkeypatch.delenv("CAPEX_ERP_REFRESH_TOKEN", raising=False)

    class _NoSession:
        def __init__(self, request, database):
            raise AssertionError("a session was opened before the transport existed")

    monkeypatch.setattr(integrations_api, "_session", _NoSession)
    caller = make_user(["Administrator"])
    resp = TestClient(app, raise_server_exceptions=False).post(
        SWEEP_URL, json={}, headers={"X-Session": caller.session_id})
    assert resp.status_code == 503, resp.text
    assert _detail(resp)["code"] == "LIVE_TRANSPORT_UNAVAILABLE"
    assert "credential" in _detail(resp)["detail"].lower()


def test_route_the_openapi_template_is_spelled_with_connection_id():
    schema = _app().openapi()
    assert "/api/integrations/connections/{connection_id}/sweep" in schema["paths"]
    op = schema["paths"]["/api/integrations/connections/{connection_id}/sweep"]
    assert set(op) == {"post"}
    assert json.dumps(op)  # serialisable, like every other route's


# ============================== decision 8: the currency and the rate reach the store
def test_pg_store_carries_the_orders_currency_and_the_bills_rate(monkeypatch):
    """`PgSweepStore` was the last unwired seam of decision 8: it built
    `LocalPurchaseOrder` without the order's currency (so every order read as
    INR and a JPY receive would have been booked at face value) and dropped a
    bill's own `exchange_rate` on the floor before the ledger. Both wires,
    proven on a fake session and a fake ledger."""
    from app.backend.integration import live_sweep, sweeps
    from app.backend.pg import repo
    from app.backend.pg import procurement as ledger

    seen = {}

    def fake_query(session, sql, params, columns=None):
        seen["sql"] = sql
        return [("PO-1", "EXT-1", "ENT-1", "PRJ-1", "PO-00006", "JPY"),
                ("PO-2", "EXT-2", "ENT-1", "PRJ-1", "PO-00001", None)]
    monkeypatch.setattr(repo, "query", fake_query)
    st = live_sweep.PgSweepStore.__new__(live_sweep.PgSweepStore)
    st.session, st.external_source, st.entity_id, st.actor = object(), "ERP", "ENT-1", "U-T"
    pos = st.open_purchase_orders(connection_id="C", after_po_id=None, limit=50)
    assert "po.currency" in seen["sql"]
    assert [(p.po_id, p.currency_code) for p in pos] == [("PO-1", "JPY"), ("PO-2", sweeps.BASE_CURRENCY)]

    def fake_mirror(session, **kw):
        seen["mirror"] = kw
        return {"bill_id": "B-1"}
    monkeypatch.setattr(ledger, "mirror_bill", fake_mirror)
    from datetime import date
    st.mirror_bill(external_source="ERP", external_id="X", bill_number="B", vendor_name="V",
                   bill_date=date(2026, 9, 12), lines=(), source_currency="JPY",
                   exchange_rate="0.561", fx_rate_source="ERP bill X")
    assert seen["mirror"]["source_currency"] == "JPY"
    assert seen["mirror"]["exchange_rate"] == "0.561"
    assert seen["mirror"]["fx_rate_source"] == "ERP bill X"
    assert seen["mirror"]["actor"] == "U-T"
