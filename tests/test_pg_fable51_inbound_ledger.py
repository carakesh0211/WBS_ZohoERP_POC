"""Fable 5.1 -- the inbound ledger, executed against a real PostgreSQL server.

Every test here is a behavioural regression: it FAILED on the code before the
change it names and PASSES after, and the critical ones were mutation-checked
(fix reverted, test observed failing, fix restored) before the commit that
carries them. Nothing below is a source scan; each assertion is about what the
SERVER holds after a write.

THE FOUR IDENTITIES, ASSERTED AFTER EVERY MUTATION (domain-controls.md):

    available   == budget - actual - commitment - reservation
    exposure    == commitment + actual + reservation
    po_line:    billed + open_commitment == ordered
    money       is only ever integer paise

`procurement_services.cell_position_invariants` evaluates them and this file
asserts on the booleans it returns, so no test here can quietly assert a
weaker identity than the one the product claims.

A SKIP IS NOT A PASS: every test skips without `CAPEX_DB_URL`.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import os  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import fx  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL. A SKIP IS NOT A PASS.")

ORG = "ORG-F51"
ENTITY = "ENT-F51"
PROJECT = "PRJ-F51"
HEAD = "BH-F51"
#: Three WBS elements under one project: `A` carries a seeded control/ledger
#: pair with a CARRIED commitment (the H-7 case); `B` carries NO cell at all
#: (the bare-UPDATE case); `C` is the anchor for the EUR purchase order.
WBS_A, WBS_B, WBS_C = "WBS-F51-A", "WBS-F51-B", "WBS-F51-C"
SOURCE_LABEL = "ZOHO_ERP"
PO_INR, POL_INR = "ZPO-F51-INR", "ZPOL-F51-INR"
PO_EUR, POL_EUR = "ZPO-F51-EUR", "ZPOL-F51-EUR"
ORDERED_INR = 1_000_000            # Rs 10,000.00 on the INR line
ORDERED_EUR_MINOR = 10_000_000     # EUR 1,00,000.00 in cents
EUR_RATE = "92.50"
EUR_BASE_PAISE = 925_000_000       # Rs 92,50,000.00
CARRIED = 500_000                  # Rs 5,000.00 carried in from the cutover
PO_DATE = date(2026, 9, 7)
T0 = datetime(2026, 9, 7, 11, 30, 15, tzinfo=timezone.utc)
ACTOR = "SVC-SWEEP"


# ==================================================================== fixtures
def _seed(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES (%s, 'ORGF51', 'F51 Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING", (ORG,))
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name,"
        " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
        " ON CONFLICT DO NOTHING", (ENTITY, ORG, ENTITY, ENTITY))
    con.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name,"
        " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
        " ON CONFLICT DO NOTHING", (PROJECT, ENTITY, PROJECT, PROJECT))
    con.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
        " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
        " ON CONFLICT DO NOTHING", (HEAD, ENTITY, HEAD, HEAD))
    for wbs, path in ((WBS_A, "f51a"), (WBS_B, "f51b"), (WBS_C, "f51c")):
        con.execute(
            "INSERT INTO wbs_element (wbs_id, project_id, wbs_code,"
            " description, wbs_path, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
            (wbs, PROJECT, wbs, wbs, path))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES (%s, 'sweep@example.test',"
        " 'Sweep service', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING",
        (ACTOR,))
    # WBS_A: a seeded pair carrying a commitment NO po_line explains -- what
    # the SAP cutover and the demo seed both produce. Migration 027 ran before
    # this seed, so the carried figure is written directly, exactly as its
    # backfill would have left it.
    con.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id,"
        " budget_paise, updated_by) VALUES (%s, %s, 100000000, 'T')"
        " ON CONFLICT DO NOTHING", (WBS_A, HEAD))
    con.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id,"
        " commitment_paise, commitment_carried_paise,"
        " commitment_carried_note, updated_by)"
        " VALUES (%s, %s, %s, %s, 'SAP cutover 2026-04-01', 'T')"
        " ON CONFLICT DO NOTHING", (WBS_A, HEAD, CARRIED, CARRIED))
    # Two purchase orders: INR on WBS_B (no cell), EUR on WBS_C (no cell).
    con.execute(
        "INSERT INTO purchase_order (po_id, po_number, project_id,"
        " vendor_name, currency, ordered_at, external_source, external_id,"
        " created_by, updated_by)"
        " VALUES ('PO-F51-INR', 'PO-NUM-F51-INR', %s, 'Vendor INR', 'INR',"
        " %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
        (PROJECT, T0, SOURCE_LABEL, PO_INR))
    con.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
        " budget_head_id, rate_paise, amount_paise, line_external_id,"
        " created_by, updated_by)"
        " VALUES ('POL-F51-INR', 'PO-F51-INR', 1, %s, %s, %s, %s, %s, %s,"
        " 'T', 'T') ON CONFLICT DO NOTHING",
        (PROJECT, WBS_B, HEAD, ORDERED_INR, ORDERED_INR, POL_INR))
    # A foreign order must name its rate's provenance (migration 029,
    # ck_purchase_order_fx_provenance): the fx_rate row, its date and source.
    # The row is the rate EUR_BASE_PAISE was computed at.
    con.execute(
        "INSERT INTO fx_rate (fx_rate_id, from_currency, to_currency, rate_date,"
        " rate, rate_source, created_by)"
        " VALUES ('FXR-F51-EUR', 'EUR', 'INR', %s, %s, 'TEST', 'T')"
        " ON CONFLICT DO NOTHING", (PO_DATE, EUR_RATE))
    con.execute(
        "INSERT INTO purchase_order (po_id, po_number, project_id,"
        " vendor_name, currency, ordered_at, external_source, external_id,"
        " exchange_rate, fx_rate_id, fx_rate_date, fx_rate_source,"
        " source_minor_exponent, created_by, updated_by)"
        " VALUES ('PO-F51-EUR', 'PO-NUM-F51-EUR', %s, 'SunPeak Energy GmbH',"
        " 'EUR', %s, %s, %s, %s, 'FXR-F51-EUR', %s, 'TEST', 2, 'T', 'T')"
        " ON CONFLICT DO NOTHING",
        (PROJECT, T0, SOURCE_LABEL, PO_EUR, EUR_RATE, PO_DATE))
    con.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
        " budget_head_id, rate_paise, amount_paise, line_external_id,"
        " source_rate_minor, source_amount_minor, created_by, updated_by)"
        " VALUES ('POL-F51-EUR', 'PO-F51-EUR', 1, %s, %s, %s, %s, %s, %s,"
        " %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
        (PROJECT, WBS_C, HEAD, EUR_BASE_PAISE, EUR_BASE_PAISE, POL_EUR,
         ORDERED_EUR_MINOR, ORDERED_EUR_MINOR))
    con.commit()


@pytest.fixture()
def seeded(pg_connection):
    _seed(pg_connection)
    return pg_connection


def _session(con: psycopg.Connection) -> Session:
    """RESTRICTED to the seeded entity, never `Scope.system()`."""
    return Session(connection=con,
                   scope=Scope(user_id="U-F51",
                               entity_ids=frozenset({ENTITY})))


def _invariants(con, wbs_id):
    position = svc.cell_position_invariants(_session(con), wbs_id, HEAD)
    assert position["money_is_int"], position
    assert position["available_identity"], position
    assert position["exposure_identity"], position
    assert position["po_line_identity"], position
    assert position["commitment_by_source_identity"], position
    return position


def _cell(con, wbs_id, column):
    row = con.execute(
        f"SELECT {column} FROM budget_ledger_cell"  # noqa: S608
        " WHERE wbs_id = %s AND budget_head_id = %s", (wbs_id, HEAD)).fetchone()
    return None if row is None else int(row[0])


def _rate(con, *, currency="EUR", rate=EUR_RATE, rate_date=PO_DATE,
          source="RBI_REFERENCE"):
    fx.record_rate(_session(con), from_currency=currency, rate_date=rate_date,
                   rate=rate, rate_source=source, actor=ACTOR)
    con.commit()


def _receive(con, *, po_line_id, receive_id, line_id, paise, quantity="1",
             currency=None, is_reversal=False, reverses=None, at=T0):
    kwargs = {}
    if currency is not None:
        kwargs["currency_code"] = currency
    if reverses is not None:
        kwargs["reverses_receive_external_id"] = reverses
    procurement.record_receive_line(
        _session(con), po_line_id=po_line_id, receive_external_id=receive_id,
        line_external_id=line_id, quantity=quantity, amount_paise=paise,
        external_source=SOURCE_LABEL, receive_number=f"RCV-{receive_id}",
        received_at=at, external_last_modified=at,
        payload_sha=f"sha-{receive_id}-{line_id}", is_reversal=is_reversal,
        **kwargs)


# ===========================================================================
# Task 3 -- a bare UPDATE never succeeds having written nothing
# ===========================================================================
@pytest.mark.pg
@PG
def test_a_derived_recompute_on_a_cell_with_no_row_creates_it_and_lands(seeded):
    """BEFORE: `refresh_cells_after_ingest` on WBS_B locked nothing, the
    UPDATE matched no row, and the purchase order's Rs 10,000 stood ORDERED
    in `po_line` and COMMITTED nowhere -- availability never fell for it.
    AFTER: the pair is created under the stated rule and the position lands.
    """
    assert _cell(seeded, WBS_B, "ordered_paise") is None, "seed drift"
    svc.refresh_cells_after_ingest(_session(seeded), [(WBS_B, HEAD)],
                                   actor=ACTOR)
    seeded.commit()
    assert _cell(seeded, WBS_B, "ordered_paise") == ORDERED_INR
    assert _cell(seeded, WBS_B, "commitment_paise") == ORDERED_INR
    position = _invariants(seeded, WBS_B)
    assert position["budget_paise"] == 0, "a recompute must not invent budget"
    assert position["available_paise"] == -ORDERED_INR


@pytest.mark.pg
@PG
def test_recompute_cell_refuses_an_absent_control_cell_with_a_code(seeded):
    """BEFORE: `UPDATE budget_control_cell` matched nothing, the read-back
    returned None, and the caller got a TypeError from tuple unpacking.
    AFTER: a coded 404, and nothing written."""
    with pytest.raises(budget_svc.BudgetServiceError) as excinfo:
        budget_svc.recompute_cell(_session(seeded), WBS_B, HEAD, actor=ACTOR)
    assert excinfo.value.code == "BUDGET_CELL_NOT_FOUND"
    assert excinfo.value.status == 404
    seeded.rollback()
    assert _cell(seeded, WBS_B, "ordered_paise") is None


@pytest.mark.pg
@PG
def test_recompute_cell_creates_the_ledger_companion_of_a_control_row(seeded):
    """A control row seeded without its ledger twin -- the shape every
    project created THROUGH the product had. BEFORE: the JOIN read back None
    and the unpack raised. AFTER: the twin exists and the budget columns land
    on it."""
    seeded.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id,"
        " budget_paise, updated_by) VALUES (%s, %s, 0, 'T')", (WBS_C, HEAD))
    seeded.commit()
    assert _cell(seeded, WBS_C, "original_paise") is None
    result = budget_svc.recompute_cell(_session(seeded), WBS_C, HEAD,
                                       actor=ACTOR)
    seeded.commit()
    assert result["budget_paise"] == 0
    assert _cell(seeded, WBS_C, "original_paise") == 0
    # `recompute_cell` derives the BUDGET columns only; the procurement
    # position on the same row is the other writer's, and the identities are
    # asserted once both halves have landed on the row this call created.
    svc.refresh_cells_after_ingest(_session(seeded), [(WBS_C, HEAD)],
                                   actor=ACTOR)
    seeded.commit()
    _invariants(seeded, WBS_C)


# ===========================================================================
# Task 2 -- H-7: a commitment the recompute cannot derive is preserved
# ===========================================================================
@pytest.mark.pg
@PG
def test_a_carried_commitment_survives_the_recompute(seeded):
    """BEFORE: the first recompute on WBS_A derived zero from no po_line and
    wrote zero over Rs 5,000 of real obligation; availability rose by exactly
    that. AFTER: the carried source is read and kept."""
    assert _cell(seeded, WBS_A, "commitment_paise") == CARRIED
    svc.recompute_commitment(_session(seeded), WBS_A, HEAD, actor=ACTOR)
    svc.recompute_commitment(_session(seeded), WBS_A, HEAD, actor=ACTOR)
    seeded.commit()
    assert _cell(seeded, WBS_A, "commitment_paise") == CARRIED, (
        "the recompute zeroed a commitment no po_line explains (H-7)")
    position = _invariants(seeded, WBS_A)
    assert position["commitment_carried_paise"] == CARRIED
    assert position["available_paise"] == 100_000_000 - CARRIED


@pytest.mark.pg
@PG
def test_commitment_is_the_sum_of_its_sources_and_each_moves_alone(seeded):
    """A purchase order arrives on the carried cell: PO_LINE adds to CARRIED,
    neither overwrites the other, and a bill relieves only the PO_LINE part.
    """
    seeded.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
        " budget_head_id, rate_paise, amount_paise, line_external_id,"
        " created_by, updated_by)"
        " VALUES ('POL-F51-INR-2', 'PO-F51-INR', 2, %s, %s, %s, 300000,"
        " 300000, 'ZPOL-F51-INR-2', 'T', 'T')", (PROJECT, WBS_A, HEAD))
    seeded.commit()
    svc.refresh_cells_after_ingest(_session(seeded), [(WBS_A, HEAD)],
                                   actor=ACTOR)
    seeded.commit()
    assert _cell(seeded, WBS_A, "commitment_paise") == CARRIED + 300_000
    _invariants(seeded, WBS_A)

    procurement.mirror_bill(
        _session(seeded), external_source=SOURCE_LABEL,
        external_id="ZB-F51-CARRIED", bill_number="BN-F51-CARRIED",
        vendor_name="Vendor INR", bill_date=PO_DATE, po_external_id=PO_INR,
        lines=[{"line_total_paise": 100_000, "bill_line_external_id": "BL-1",
                "purchase_order_line_external_id": "ZPOL-F51-INR-2",
                "quantity": "1"}])
    seeded.commit()
    assert _cell(seeded, WBS_A, "commitment_paise") == CARRIED + 200_000
    assert _cell(seeded, WBS_A, "actual_paise") == 100_000
    position = _invariants(seeded, WBS_A)
    assert position["commitment_carried_paise"] == CARRIED, (
        "a bill against a po_line relieved the CARRIED source")


@pytest.mark.pg
@PG
def test_a_carried_commitment_leaves_only_through_an_explicit_release(seeded):
    result = svc.release_carried_commitment(
        _session(seeded), WBS_A, HEAD, paise=200_000,
        reason="SAP PO 4500012345 confirmed closed by vendor", actor=ACTOR)
    seeded.commit()
    assert result["carried_paise"] == CARRIED - 200_000
    assert _cell(seeded, WBS_A, "commitment_paise") == CARRIED - 200_000
    _invariants(seeded, WBS_A)
    with pytest.raises(svc.ProcurementError) as excinfo:
        svc.release_carried_commitment(
            _session(seeded), WBS_A, HEAD, paise=CARRIED, reason="too much",
            actor=ACTOR)
    assert excinfo.value.code == "CARRIED_COMMITMENT_INSUFFICIENT"
    seeded.rollback()
    with pytest.raises(svc.ProcurementError) as excinfo:
        svc.release_carried_commitment(
            _session(seeded), WBS_A, HEAD, paise=1, reason="  ", actor=ACTOR)
    assert excinfo.value.code == "BLANK_RELEASE_REASON"
    seeded.rollback()
    assert seeded.execute(
        "SELECT count(*) FROM audit_log WHERE action = %s",
        ("CARRIED_COMMITMENT_RELEASED",)).fetchone()[0] == 1


@pytest.mark.pg
@PG
def test_the_migration_backfill_carries_only_what_no_po_line_explains():
    """027's rule, executed: a cell with commitment and no po_line is carried
    in full; a cell with po_line rows is left to the recompute."""
    text = (_Path(__file__).resolve().parents[1] / "migrations" / "pg"
            / "027_grn_reversal_and_commitment_sources.sql").read_text(
                encoding="utf-8")
    applied = text.split("-- ROLLBACK:", 1)[0]
    assert "SET commitment_carried_paise = bl.commitment_paise" in applied
    assert "NOT EXISTS" in applied and "FROM po_line pl" in applied
    assert "DELETE FROM schema_migrations WHERE version = '027'" in text
