"""A foreign-currency purchase order, end to end on PostgreSQL (Fable 5.1,
migration 029).

What is proven, each against a real server:

* No ACTIVE rate on file for the document date -> `FX_RATE_UNAVAILABLE` (409)
  and NOTHING is written: no header, no line, no ledger row, no commitment.
  A PENDING rate (028: recorded by the administration API, not yet activated)
  is not "on file" either.
* With a rate: the lines carry the vendor's figures (`source_amount_minor`,
  `source_rate_minor`) beside translated paise; the paise sum to the header
  EXACTLY; the header carries the rate and its provenance; ONE
  `fx_translation_event` row of kind PURCHASE_ORDER exists; the commitment
  and the budget check are in base paise the rate produced -- so a USD order
  that translates over budget is refused as PO_EXCEEDS_BUDGET.
* The shape rules: source figures on an INR order, or base paise on a foreign
  one, are refused at the service; the database refuses the same two shapes
  (trigger) and a foreign header without provenance (CHECK); the source
  figures and the header basis are immutable (triggers).
* Emission: the outbox payload is labelled with the order's currency and
  prices the vendor at the SOURCE figures, exact per the DTO's identities.
* Conversion: an approved request converts into a USD order only with the
  vendor's figure per request line; missing or stray lines are refused.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parent))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)
from test_pg_procurement import (  # noqa: E402
    _Caps, _MetadataOnlyAdapter, _approved_pr, _connection, _line, _seed_chain,
)

from app.backend.integration import books_inventory  # noqa: E402
from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.pg import fx, fx_admin  # noqa: E402
from app.backend.pg import procurement_services as proc  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(not os.environ.get("CAPEX_DB_URL"),
                        reason="PostgreSQL not configured; set CAPEX_DB_URL.")
pytestmark = [PG, pytest.mark.pg]

DOC_DATE = date(2026, 8, 20)          # no seeded rate on this date
RATE = Decimal("83.25")               # USD/INR, recorded per test


def _usd_line(ids, source_minor, *, quantity=1, wbs=None, rate=None):
    return {"wbs_id": wbs or ids["child"], "budget_head_id": ids["head"],
            "source_amount_minor": source_minor, "quantity": quantity,
            "description": "a dollar line",
            **({"source_rate_minor": rate} if rate is not None else {})}


def _record_usd(session, *, actor="U-FIN", on=DOC_DATE, rate=RATE):
    return fx.record_rate(session, from_currency="USD", rate_date=on, rate=str(rate),
                          rate_source="BANK_ADVICE", actor=actor,
                          source_reference="test advice")["fx_rate_id"]


def _count(con, sql, *params):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# ============================================================ refusal first
def test_no_active_rate_means_no_order_no_line_no_ledger_row(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    before = _count(pg_connection, "SELECT count(*) FROM purchase_order WHERE project_id = %s", ids["project"])
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                           actor="U-PROC", currency="USD", document_date=DOC_DATE,
                           lines=[_usd_line(ids, 10_00)])
    assert exc.value.code == "FX_RATE_UNAVAILABLE" and exc.value.status == 409
    assert "USD" in exc.value.message and DOC_DATE.isoformat() in exc.value.message
    assert _count(pg_connection, "SELECT count(*) FROM purchase_order WHERE project_id = %s", ids["project"]) == before
    assert _count(pg_connection, "SELECT count(*) FROM fx_translation_event WHERE document_type = 'PURCHASE_ORDER' "
                  "AND entity_id = %s", ids["entity"]) == 0


def test_a_pending_rate_is_not_on_file(pg_database, pg_connection):
    """028: a rate recorded through the administration API is INACTIVE until a
    second user activates it. Until then it translates nothing."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    on = date(2026, 8, 21)
    with pg_database.session(Scope.system()) as session:
        pending = fx_admin.create_rate(session, actor="U-FIN", from_currency="USD",
                                       rate_date=on, rate="83.10", rate_source="RBI_REFERENCE")
        assert pending.get("active") in (False, None) or pending.get("status", "").upper() != "ACTIVE"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                           actor="U-PROC", currency="USD", document_date=on,
                           lines=[_usd_line(ids, 10_00)])
    assert exc.value.code == "FX_RATE_UNAVAILABLE"


# ================================================== the order, when a rate exists
def test_a_usd_order_stores_the_vendors_figures_and_the_translated_paise(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        rate_id = _record_usd(session)
        created = proc.create_po(
            session, project_id=ids["project"], vendor_name="Acme US", actor="U-PROC",
            currency="USD", document_date=DOC_DATE,
            lines=[_usd_line(ids, 3 * 19_99, quantity=3, wbs=ids["child"]),
                   _usd_line(ids, 7_01, wbs=ids["root"])])
    # Source total 5,997 + 701 = 6,698 cents (USD, exponent 2) x 83.25 -> 557,608.50 -> 557,609 paise (HALF_UP), allocated across the two lines without loss.
    assert created["currency"] == "USD"
    assert created["exchange_rate"] == "83.25000000" or Decimal(created["exchange_rate"]) == RATE
    assert created["fx_rate_id"] == rate_id
    assert created["source_amount_minor"] == 6698
    assert created["amount_paise"] == fx.translate_to_base_paise(6698, RATE, source_minor_exponent=2)

    with pg_connection.cursor() as cur:
        cur.execute("SELECT currency, exchange_rate, fx_rate_id, fx_rate_date, fx_rate_source, "
                    "source_minor_exponent FROM purchase_order WHERE po_id = %s", (created["po_id"],))
        header = cur.fetchone()
        cur.execute("SELECT line_no, quantity, source_rate_minor, source_amount_minor, rate_paise, "
                    "amount_paise FROM po_line WHERE po_id = %s ORDER BY line_no", (created["po_id"],))
        lines = cur.fetchall()
        cur.execute("SELECT source_currency, fx_rate, fx_rate_id, source_total_minor, base_total_paise, "
                    "line_count FROM fx_translation_event WHERE document_type = 'PURCHASE_ORDER' "
                    "AND document_id = %s", (created["po_id"],))
        ledger = cur.fetchone()
    assert header[0] == "USD" and Decimal(header[1]) == RATE and header[2] == rate_id
    assert header[3] == DOC_DATE and header[4] == "BANK_ADVICE" and header[5] == 2
    # The vendor's figures, exact: 3 x 19.99 and 1 x 7.01.
    assert [(l[0], l[2], l[3]) for l in lines] == [(1, 1999, 5997), (2, 701, 701)]
    # The paise sum to the header exactly (rate applied once, allocated without loss).
    assert sum(l[5] for l in lines) == created["amount_paise"]
    assert ledger is not None
    assert (ledger[0], Decimal(ledger[1]), ledger[2], ledger[3], ledger[4], ledger[5]) == \
        ("USD", RATE, rate_id, 6698, created["amount_paise"], 2)


def test_the_budget_is_checked_on_the_paise_the_rate_produced(pg_database, pg_connection):
    """USD 120.00 at 83.25 is Rs 9,990.00; a Rs 9,000 budget refuses it. The
    refusal is PO_EXCEEDS_BUDGET, not a currency error: the check saw rupees."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=9_000_00)
    with pg_database.session(Scope.system()) as session:
        _record_usd(session)
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                           actor="U-PROC", currency="USD", document_date=DOC_DATE,
                           lines=[_usd_line(ids, 120_00)])
        assert exc.value.code == "PO_EXCEEDS_BUDGET"
        # USD 100.00 -> Rs 8,325.00 fits, and the commitment is in rupees.
        created = proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                                 actor="U-PROC", currency="USD", document_date=DOC_DATE,
                                 lines=[_usd_line(ids, 100_00)])
    assert created["amount_paise"] == 8_325_00
    with pg_connection.cursor() as cur:
        cur.execute("SELECT commitment_paise FROM budget_ledger_cell WHERE wbs_id = %s AND budget_head_id = %s",
                    (ids["child"], ids["head"]))
        assert cur.fetchone()[0] == 8_325_00


def test_a_second_source_for_the_same_day_is_ambiguous_unless_named(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    on = date(2026, 8, 22)
    with pg_database.session(Scope.system()) as session:
        fx.record_rate(session, from_currency="USD", rate_date=on, rate="83.00",
                       rate_source="RBI_REFERENCE", actor="U-FIN", source_reference="r")
        fx.record_rate(session, from_currency="USD", rate_date=on, rate="83.40",
                       rate_source="BANK_ADVICE", actor="U-FIN", source_reference="b")
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                           actor="U-PROC", currency="USD", document_date=on,
                           lines=[_usd_line(ids, 10_00)])
        assert exc.value.code == "FX_RATE_AMBIGUOUS"
        # Naming the source resolves it: the dealt rate, and the order carries it.
        created = proc.create_po(session, project_id=ids["project"], vendor_name="Acme US",
                                 actor="U-PROC", currency="USD", document_date=on,
                                 exchange_rate="83.40", rate_source="BANK_ADVICE",
                                 lines=[_usd_line(ids, 10_00)])
    assert Decimal(created["exchange_rate"]) == Decimal("83.40") and created["amount_paise"] == 834_00


# ================================================================= the shape
def test_source_figures_on_an_inr_order_and_paise_on_a_usd_order_are_both_refused(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        _record_usd(session)
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           lines=[{**_line(ids, 10_00), "source_amount_minor": 12}])
        assert exc.value.code == "PO_SOURCE_AMOUNT_ON_BASE_ORDER"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           currency="USD", document_date=DOC_DATE,
                           lines=[{**_usd_line(ids, 10_00), "amount_paise": 832_50}])
        assert exc.value.code == "PO_BASE_AMOUNT_ON_FOREIGN_ORDER"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           currency="USD", document_date=DOC_DATE, lines=[_line(ids, 10_00)])
        assert exc.value.code == "PO_BASE_AMOUNT_ON_FOREIGN_ORDER", "a rupee line on a dollar order"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           currency="USD", document_date=DOC_DATE,
                           lines=[{"wbs_id": ids["child"], "budget_head_id": ids["head"], "quantity": 1}])
        assert exc.value.code == "PO_SOURCE_AMOUNT_REQUIRED"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           exchange_rate="83.25", rate_source="BANK_ADVICE", lines=[_line(ids, 10_00)])
        assert exc.value.code == "PO_IDENTITY_TRANSLATION"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                           currency="USD", document_date=DOC_DATE,
                           lines=[_usd_line(ids, 10_00, quantity=3)])
        assert exc.value.code == "PO_LINE_RATE_NOT_EXACT"


def test_the_database_refuses_the_same_shapes_and_freezes_what_was_written(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        rate_id = _record_usd(session)
        usd = proc.create_po(session, project_id=ids["project"], vendor_name="Acme US", actor="U-PROC",
                             currency="USD", document_date=DOC_DATE, lines=[_usd_line(ids, 10_00)])
        inr = proc.create_po(session, project_id=ids["project"], vendor_name="Acme", actor="U-PROC",
                             lines=[_line(ids, 10_00)])
    import psycopg
    con = pg_connection

    def refused(sql, params, needle):
        con.rollback()
        with pytest.raises(psycopg.Error) as exc:
            with con.cursor() as cur:
                cur.execute(sql, params)
        con.rollback()
        assert needle in str(exc.value), str(exc.value)

    # Source figures are immutable on the USD line ...
    refused("UPDATE po_line SET source_amount_minor = source_amount_minor + 1 WHERE po_id = %s",
            (usd["po_id"],), "immutable")
    # ... an INR line may not acquire them ...
    refused("UPDATE po_line SET source_amount_minor = 1, source_rate_minor = 1 WHERE po_id = %s",
            (inr["po_id"],), "INR order")
    # ... one half of the pair is refused (the trigger speaks first; the CHECK
    # ck_po_line_source_pair stands behind it) ...
    refused("UPDATE po_line SET source_rate_minor = NULL WHERE po_id = %s",
            (usd["po_id"],), "source_rate_minor")
    # ... the header basis is fixed ...
    refused("UPDATE purchase_order SET exchange_rate = 90 WHERE po_id = %s", (usd["po_id"],), "fixed")
    refused("UPDATE purchase_order SET currency = 'INR' WHERE po_id = %s", (usd["po_id"],), "fixed")
    # ... and a foreign header without provenance cannot be inserted at all.
    refused("INSERT INTO purchase_order (po_id, po_number, project_id, vendor_name, currency, exchange_rate, "
            "created_by, updated_by) VALUES (%s, %s, %s, 'V', 'USD', 83.25, 'T', 'T')",
            (f"PO-{suffix}", f"PO-X-{suffix}", ids["project"]), "ck_purchase_order_fx_provenance")
    # An unknown currency, even fully provenanced, is refused by the FK.
    refused("INSERT INTO purchase_order (po_id, po_number, project_id, vendor_name, currency, exchange_rate, "
            "fx_rate_id, fx_rate_date, fx_rate_source, created_by, updated_by) "
            "VALUES (%s, %s, %s, 'V', 'XXX', 83.25, %s, %s, 'BANK_ADVICE', 'T', 'T')",
            (f"PO-{suffix}", f"PO-Y-{suffix}", ids["project"], rate_id, DOC_DATE), "fk_purchase_order_currency")
    # A fully-provenanced foreign header with a base-only line is refused by the trigger.
    with con.cursor() as cur:
        cur.execute("INSERT INTO purchase_order (po_id, po_number, project_id, vendor_name, currency, "
                    "exchange_rate, fx_rate_id, fx_rate_date, fx_rate_source, created_by, updated_by) "
                    "VALUES (%s, %s, %s, 'V', 'USD', 83.25, %s, %s, 'BANK_ADVICE', 'T', 'T')",
                    (f"PO-Z-{suffix}", f"PO-Z-{suffix}", ids["project"], rate_id, DOC_DATE))
    con.commit()
    refused("INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id, budget_head_id, "
            "quantity, rate_paise, amount_paise, created_by, updated_by) "
            "VALUES (%s, %s, 1, %s, %s, %s, 1, 100, 100, 'T', 'T')",
            (f"POL-{suffix}", f"PO-Z-{suffix}", ids["project"], ids["child"], ids["head"]),
            "must carry source_amount_minor")


# ================================================================== emission
def test_the_usd_order_is_planned_in_usd_at_the_vendors_figures(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        _record_usd(session)
        created = proc.create_po(session, project_id=ids["project"], vendor_name="Acme US", actor="U-PROC",
                                 currency="USD", document_date=DOC_DATE,
                                 lines=[_usd_line(ids, 3 * 19_99, quantity=3)])
        with pg_connection.cursor() as cur:
            cur.execute("UPDATE purchase_order SET status = 'Approved' WHERE po_id = %s", (created["po_id"],))
            pg_connection.commit()
        connection_id = _connection(session, ids, suffix=suffix)
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-US-1",
            document_date=DOC_DATE, actor="U-PROC", acknowledged=True)
    assert planned["currency_code"] == "USD" and planned["purchase_orders"] == 1
    entry = planned["outbox"][0]
    assert entry["currency_code"] == "USD" and entry["total_paise"] == 5997
    with pg_connection.cursor() as cur:
        cur.execute("SELECT payload FROM integration_outbox WHERE outbox_id = %s", (entry["outbox_id"],))
        payload = cur.fetchone()[0]
    assert payload["currency_code"] == "USD"
    assert payload["lines"][0]["unit_price_paise"] == 1999 and payload["lines"][0]["amount_paise"] == 5997
    assert payload["subtotal_paise"] == payload["total_paise"] == 5997
    dto = ob.emission_dto(payload=payload, connection_id=connection_id, module="purchaseorders",
                          local_id=entry["local_id"], dedupe_key=entry["dedupe_key"])
    body = books_inventory._emission_body(dto, entry["dedupe_key"])
    assert body["currency_code"] == "USD" and body["line_items"][0]["rate"] == "19.99"


# =============================================================== conversion
def test_a_request_converts_into_a_usd_order_only_with_the_vendors_figures(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_000_00)
    with pg_database.session(Scope.system()) as session:
        _record_usd(session)
        pr_id = _approved_pr(session, ids, amount=900_00)
        pr_line_ids = [l["pr_line_id"] for l in proc._pr_lines(session, pr_id)]
        with pytest.raises(proc.ProcurementError) as exc:
            proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC", vendor_name="Acme US",
                                  currency="USD", document_date=DOC_DATE)
        assert exc.value.code == "PO_CONVERSION_SOURCE_LINES_REQUIRED"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC", vendor_name="Acme US",
                                  currency="USD", document_date=DOC_DATE,
                                  source_lines=[{"pr_line_id": "PRL-NOPE", "source_amount_minor": 10_00}])
        assert exc.value.code == "PO_CONVERSION_SOURCE_LINES_REQUIRED"
        with pytest.raises(proc.ProcurementError) as exc:
            proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC", vendor_name="Acme",
                                  source_lines=[{"pr_line_id": pr_line_ids[0], "source_amount_minor": 10_00}])
        assert exc.value.code == "PO_SOURCE_AMOUNT_ON_BASE_ORDER"
        converted = proc.convert_pr_to_po(
            session, pr_id=pr_id, actor="U-PROC", vendor_name="Acme US", currency="USD",
            document_date=DOC_DATE,
            source_lines=[{"pr_line_id": pr_line_ids[0], "source_amount_minor": 10_00}])
    assert converted["currency"] == "USD" and converted["amount_paise"] == 832_50
    assert converted["source_amount_minor"] == 10_00
    with pg_connection.cursor() as cur:
        cur.execute("SELECT wbs_id, budget_head_id, source_amount_minor, amount_paise FROM po_line "
                    "WHERE po_id = %s", (converted["po_id"],))
        (wbs, head, source, paise), = cur.fetchall()
        cur.execute("SELECT count(*) FROM fx_translation_event WHERE document_type = 'PURCHASE_ORDER' "
                    "AND document_id = %s", (converted["po_id"],))
        ledger_rows = cur.fetchone()[0]
    assert (wbs, head) == (ids["child"], ids["head"]), "the cell is copied from the request line"
    assert (source, paise, ledger_rows) == (10_00, 832_50, 1)
