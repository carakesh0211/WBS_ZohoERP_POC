"""Internal material fulfilment and captive consumption (migration 034).

WHAT IS PROVED HERE
===================

Source-level (no database), so the properties of the migration TEXT and of
this package's source are checked on every workstation:

* 034 is discovered, named and numbered; its two kinds are in the CHECK, the
  constants and the C18 registry; grants and revokes are named for capex_app;
  the ROLLBACK block is commented out.
* `_RECOMPUTE_DERIVED_SQL` is STILL the one statement that derives a ledger
  column -- the two internal limbs have exactly one writer.
* Every place exposure is computed sums the two internal limbs.

Live (`@pytest.mark.pg`, skipped without CAPEX_DB_URL -- A SKIP IS NOT A
PASS), the properties the product owner's decision of 2026-09-13 names:

1. the split never exceeds the approved line and the paise split exactly;
2. conversion to a purchase order reads the EXTERNAL portion; a wholly
   internal request refuses to convert;
3. THE LIFECYCLE, END TO END, WITH NO DOUBLE COUNTING: reservation ->
   allocation moves the hold rather than adding to it; issue moves money
   between limbs and changes exposure by nothing; a transfer between stores
   and a consumption confirmation move no money; a return releases into the
   hold; the close-out is exact to the paisa; the cell invariants hold at
   every step;
4. a replay with the same idempotency key moves nothing twice;
5. two concurrent allocations produce exactly one; two concurrent issues
   never exceed the allocation;
6. cancel releases the open allocation back into the request's hold and
   closes the request's exceptions; stock in the field refuses a cancel;
7. a missing valuation is a visible exception and blocks allocation until a
   reasoned manual figure resolves it; the provider boundary is asked and
   answers; a foreign valuation is not divided into rupees;
8. a missing mapping is a visible exception and blocks approval until the
   owning cell exists;
9. maker-checker: the requester never approves their own request, directly
   or through a delegation;
10. row-level security hides another entity's requests;
11. the export dataset, the report aggregate and the closure position each
    show the two internal figures apart from the external ones;
12. every verb appends to the audit chain.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys as _sys
import threading
import uuid
from decimal import Decimal
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)
from test_pg_reservations import _line, _seed  # noqa: E402

import pytest  # noqa: E402

from app.backend import auth as auth_mod  # noqa: E402
from app.backend.integration import inventory_provider as inventory  # noqa: E402
from app.backend.integration import sweeps  # noqa: E402
from app.backend.pg import closure  # noqa: E402
from app.backend.pg import exports as export_svc  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import internal_fulfilment as imr_svc  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402
from app.backend.pg import principal_scope  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402
from app.backend.pg import reporting as rp  # noqa: E402
from app.backend.pg import roles as pg_roles  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
_034 = (MIGRATIONS / "034_internal_fulfilment.sql").read_text(encoding="utf-8")
_034_CODE = re.sub(r"--[^\n]*", "", _034)
_SERVICE = (ROOT / "app" / "backend" / "pg" / "procurement_services.py").read_text(encoding="utf-8")

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that execute migration 034's lifecycle rather than reading it."),
)
pytestmark_live = [pytest.mark.pg, PG]

KIND_MAP = "INTERNAL_MAPPING_MISSING"
KIND_VAL = "INTERNAL_VALUATION_MISSING"
REQ, APPROVER, STORES, BUYER = "U-REQ", "U-APPROVER", "U-STORES", "U-BUYER"


def _migration(version: str) -> migrate_pg.Migration:
    for m in migrate_pg.discover():
        if m.version == version:
            return m
    raise AssertionError(f"migration {version} is not discovered by the runner")


# =========================================================================
# Source-level
# =========================================================================
def test_034_is_discovered_named_and_numbered():
    versions = [m.version for m in migrate_pg.discover()]
    assert versions == sorted(versions)
    assert versions.count("034") == 1
    assert _migration("034").name == "internal_fulfilment"


def test_the_two_kinds_are_one_string_in_every_place_a_kind_lives():
    import json
    registry = json.loads((ROOT / "research" / "30_contracts" / "C18_domain_statuses.json")
                          .read_text(encoding="utf-8"))
    for kind in (KIND_MAP, KIND_VAL):
        assert f"'{kind}'" in _034_CODE
        assert kind in store.EXCEPTION_KINDS and kind in sweeps.EXCEPTION_KINDS
        assert kind in registry["namespaces"]["exception_status"]["kinds"]
    assert set(store.EXCEPTION_KINDS) == sweeps.EXCEPTION_KINDS
    assert imr_svc.KIND_INTERNAL_MAPPING_MISSING == sweeps.KIND_INTERNAL_MAPPING_MISSING == KIND_MAP
    assert imr_svc.KIND_INTERNAL_VALUATION_MISSING == sweeps.KIND_INTERNAL_VALUATION_MISSING == KIND_VAL
    assert "ck_reconciliation_exception_kind" in _034_CODE


def test_the_migration_names_its_grants_and_revokes_for_capex_app():
    for table in ("pr_line_fulfilment", "internal_material_request"):
        assert re.search(rf"GRANT\s+SELECT,\s*INSERT,\s*UPDATE\s+ON\s+{table}\s+TO\s+capex_app", _034_CODE)
        assert re.search(rf"REVOKE\s+DELETE\s+ON\s+{table}\s+FROM\s+capex_app", _034_CODE)
    assert re.search(r"GRANT\s+SELECT,\s*INSERT\s+ON\s+internal_material_movement\s+TO\s+capex_app", _034_CODE)
    assert re.search(r"REVOKE\s+UPDATE,\s*DELETE\s+ON\s+internal_material_movement\s+FROM\s+capex_app", _034_CODE)


def test_the_migration_ends_with_a_commented_rollback_and_every_sum_is_cast():
    # The applied body's COMMIT, not the commented rollback block's.
    tail = _034[_034.index("\nCOMMIT;") + 1:]
    assert "-- ROLLBACK:" in tail
    for line in tail.splitlines()[1:]:
        assert not line.strip() or line.startswith("--")
    for match in re.finditer(r"SUM\([^)]*paise[^)]*\)", _034_CODE):
        assert "::bigint" in _034_CODE[match.end():match.end() + 20]


def test_the_two_internal_limbs_have_exactly_one_deriver():
    """The rule 014 established: `_RECOMPUTE_DERIVED_SQL` is the one statement
    that derives a ledger column. 034 added two columns and no second writer."""
    assert _SERVICE.count("_RECOMPUTE_DERIVED_SQL = ") == 1
    assert _SERVICE.count("internal_allocation_paise = COALESCE") == 1
    assert _SERVICE.count("internal_consumption_paise = COALESCE") == 1
    assert _SERVICE.count("pr_reserved_paise = COALESCE") == 1
    service = (ROOT / "app" / "backend" / "pg" / "internal_fulfilment.py").read_text(encoding="utf-8")
    assert "UPDATE budget_ledger_cell" not in service
    assert "recompute_derived_position(" in service


def test_every_exposure_site_sums_the_two_internal_limbs():
    for rel in ("pg/budget.py", "pg/reporting.py", "pg/exports.py", "pg/closure.py",
                "domain.py"):
        text = (ROOT / "app" / "backend" / rel).read_text(encoding="utf-8")
        assert "internal_consumption" in text and "internal_allocation" in text, rel
    assert rp.derive({"commitment": 1, "actual": 2, "pr_reserved": 3,
                      "internal_allocation": 4, "internal_consumption": 5,
                      "budget": 100})["exposure"] == 15
    assert rp.COMPONENTS[-2:] == ("internal_allocation", "internal_consumption")
    assert "IMR" in rp.DOCUMENT_TYPES
    assert "internal_material_requests" in export_svc.DATASETS


def test_the_permission_is_maker_checker_in_both_catalogues_and_auditor_reads_nothing_new():
    assert "imr.approve" in auth_mod.MAKER_CHECKER and "imr.approve" in pg_roles.MAKER_CHECKER
    for name in ("fulfilment.decide", "imr.read", "imr.create", "imr.approve",
                 "imr.allocate", "imr.issue", "imr.cancel"):
        assert name in auth_mod.PERMISSIONS and name in pg_roles.PERMISSIONS, name
        assert "Auditor" not in auth_mod.PERMISSIONS[name], name
        assert "Internal Auditor" not in pg_roles.PERMISSIONS[name], name


def test_the_split_is_exact_to_the_paisa():
    ext, internal = imr_svc.split_line(1_000_00, Decimal(10), Decimal(3))
    assert (ext, internal) == (700_00, 300_00)
    ext, internal = imr_svc.split_line(100, Decimal(3), Decimal(1))
    assert ext + internal == 100 and internal == 33
    ext, internal = imr_svc.split_line(100, Decimal(3), Decimal(2))
    assert ext + internal == 100 and internal == 67


# =========================================================================
# Live
# =========================================================================
def _system(database):
    return database.session(Scope.system())


def _estate(connection, *, budget: int = 10_000_00) -> dict:
    ids = _seed(connection, suffix=uuid.uuid4().hex[:10], root_budget=budget)
    ids["loc_a"], ids["loc_b"] = f"L_A_{ids['entity']}", f"L_B_{ids['entity']}"
    for key, code in (("loc_a", "STORE-A"), ("loc_b", "STORE-B")):
        connection.execute(
            "INSERT INTO location (location_id, entity_id, code, name, created_by, "
            "updated_by) VALUES (%s, %s, %s, %s, 't', 't')",
            (ids[key], ids["entity"], code, code))
    connection.commit()
    return ids


def _approved_pr(database, ids, *, lines, reserve=True) -> str:
    with _system(database) as session:
        pr_id = svc.create_pr(session, project_id=ids["project"], actor=REQ,
                              reserve=reserve, lines=lines)["pr_id"]
    with _system(database) as session:
        svc.submit_pr(session, pr_id=pr_id, actor=REQ)
    with _system(database) as session:
        svc.approve_pr(session, pr_id=pr_id, actor=APPROVER)
    return pr_id


def _pr_line_ids(connection, pr_id) -> list[str]:
    return [r[0] for r in connection.execute(
        "SELECT pr_line_id FROM pr_line WHERE pr_id = %s ORDER BY line_no", (pr_id,)).fetchall()]


def _ledger(connection, wbs_id, head) -> dict:
    row = connection.execute(
        "SELECT pr_reserved_paise, internal_allocation_paise, internal_consumption_paise, "
        "commitment_paise, actual_paise FROM budget_ledger_cell "
        "WHERE wbs_id = %s AND budget_head_id = %s", (wbs_id, head)).fetchone()
    return {"pr_reserved": int(row[0]), "internal_allocation": int(row[1]),
            "internal_consumption": int(row[2]), "commitment": int(row[3]),
            "actual": int(row[4])}


def _subtree_exposure(connection, ids) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(pr_reserved_paise + internal_allocation_paise + "
        "internal_consumption_paise + commitment_paise + actual_paise), 0)::bigint "
        "FROM budget_ledger_cell WHERE wbs_id IN (%s, %s, %s)",
        (ids["root_a"], ids["child_a1"], ids["child_a2"])).fetchone()
    return int(row[0])


def _invariants(database, wbs_id, head) -> dict:
    with _system(database) as session:
        return svc.cell_position_invariants(session, wbs_id, head)


def _open_exceptions(connection, imr_id) -> list[tuple]:
    return connection.execute(
        "SELECT kind, status FROM reconciliation_exception WHERE object_type = "
        "'internal_material_request' AND object_id = %s ORDER BY kind", (imr_id,)).fetchall()


def _movements(connection, imr_id) -> list[tuple]:
    return connection.execute(
        "SELECT kind, quantity, amount_paise FROM internal_material_movement "
        "WHERE imr_id = %s ORDER BY occurred_at, movement_id", (imr_id,)).fetchall()


def _decide(database, pr_line_id, mode, *, actor=STORES, **kw):
    with _system(database) as session:
        return imr_svc.decide_fulfilment(session, pr_line_id=pr_line_id, mode=mode,
                                         actor=actor, **kw)


def _internal_imr(database, ids, pr_line_id, *, rate=120_00, quantity=None) -> str:
    """One line decided INTERNAL_TRANSFER with a manual valuation, approved."""
    kw = {"unit_rate_paise": rate, "valuation_note": "stores ledger rate, verified",
          "from_location_id": ids["loc_a"]}
    if quantity is None:
        out = _decide(database, pr_line_id, imr_svc.MODE_INTERNAL, **kw)
    else:
        out = _decide(database, pr_line_id, imr_svc.MODE_SPLIT,
                      internal_quantity=quantity, **kw)
    imr_id = out["imr"]["imr_id"]
    with _system(database) as session:
        imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER)
    return imr_id


@pytest.fixture(autouse=True)
def _null_provider():
    inventory.set_inventory_provider(None)
    yield
    inventory.set_inventory_provider(None)


# ------------------------------------------------------------- 1. the split
@pytest.mark.pg
@PG
def test_the_split_never_exceeds_the_line_and_the_paise_split_exactly(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[
        {**_line(ids, "child_a1", 1_000_00), "quantity": 10}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)

    out = _decide(pg_database, line_id, imr_svc.MODE_SPLIT, internal_quantity=3,
                  unit_rate_paise=100_00, valuation_note="stores rate")
    assert (out["external_quantity"], out["internal_quantity"]) == ("7", "3")
    assert (out["external_amount_paise"], out["internal_amount_paise"]) == (700_00, 300_00)
    assert out["imr"]["status"] == "REQUESTED" and out["imr"]["requested_quantity"] == "3"
    row = pg_connection.execute(
        "SELECT mode, external_amount_paise + internal_amount_paise, "
        "external_quantity + internal_quantity FROM pr_line_fulfilment WHERE pr_line_id = %s",
        (line_id,)).fetchone()
    assert (row[0], int(row[1]), Decimal(row[2])) == ("SPLIT_FULFILMENT", 1_000_00, Decimal(10))

    # A second decision while the request is live is refused: its allocation
    # is the thing a new split would orphan.
    with pytest.raises(svc.ProcurementError) as locked:
        _decide(pg_database, line_id, imr_svc.MODE_EXTERNAL)
    assert locked.value.code == "FULFILMENT_LOCKED_BY_IMR"
    with _system(pg_database) as session:
        imr_svc.cancel(session, imr_id=out["imr"]["imr_id"], reason="re-deciding", actor=STORES)

    with pytest.raises(svc.ProcurementError) as too_many:
        _decide(pg_database, line_id, imr_svc.MODE_SPLIT, internal_quantity=10)
    assert too_many.value.code == "SPLIT_QUANTITY_INVALID"
    with pytest.raises(svc.ProcurementError) as more:
        _decide(pg_database, line_id, imr_svc.MODE_SPLIT, internal_quantity=11)
    assert more.value.code == "SPLIT_QUANTITY_INVALID"
    with pytest.raises(svc.ProcurementError) as flt:
        _decide(pg_database, line_id, imr_svc.MODE_SPLIT, internal_quantity=2.5)
    assert flt.value.code == "QUANTITY_NOT_EXACT"

    back = _decide(pg_database, line_id, imr_svc.MODE_EXTERNAL)
    assert back["imr"] is None and back["internal_amount_paise"] == 0


@pytest.mark.pg
@PG
def test_a_decision_needs_an_approved_request(pg_database, pg_connection):
    ids = _estate(pg_connection)
    with _system(pg_database) as session:
        pr_id = svc.create_pr(session, project_id=ids["project"], actor=REQ,
                              lines=[_line(ids, "child_a1", 100_00)])["pr_id"]
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    with pytest.raises(svc.ProcurementError) as exc:
        _decide(pg_database, line_id, imr_svc.MODE_INTERNAL)
    assert exc.value.code == "PR_NOT_APPROVED"


# ------------------------------------------------------ 2. the conversion
@pytest.mark.pg
@PG
def test_conversion_reads_the_external_portion_and_a_wholly_internal_request_refuses(
        pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[
        {**_line(ids, "child_a1", 500_00), "quantity": 10},
        {**_line(ids, "child_b1", 400_00), "quantity": 4}])
    line1, line2 = _pr_line_ids(pg_connection, pr_id)
    split = _decide(pg_database, line1, imr_svc.MODE_SPLIT, internal_quantity=4,
                    unit_rate_paise=60_00, valuation_note="stores rate")
    # Converting BEFORE the internal portion is allocated would release its
    # hold with the settlement (adversarial review 2026-09-13, P0): refused.
    with pytest.raises(svc.ProcurementError) as early:
        with _system(pg_database) as session:
            svc.convert_pr_to_po(session, pr_id=pr_id, actor=BUYER, vendor_name="V")
    assert early.value.code == "INTERNAL_PORTION_NOT_ALLOCATED"
    held_before = _ledger(pg_connection, ids["root_a"], ids["head"])["pr_reserved"]
    assert held_before == 500_00, "line 1's whole hold (its own pot) is still there"
    with _system(pg_database) as session:
        imr_svc.approve_request(session, imr_id=split["imr"]["imr_id"], actor=APPROVER)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=split["imr"]["imr_id"], actor=STORES, idempotency_key="A")
    with _system(pg_database) as session:
        po = svc.convert_pr_to_po(session, pr_id=pr_id, actor=BUYER, vendor_name="V")
    assert po["amount_paise"] == 300_00 + 400_00, "line 1's EXTERNAL 300 + line 2 whole"
    led = _ledger(pg_connection, ids["child_a1"], ids["head"])
    assert led["internal_allocation"] == 240_00, "4 x 60.00 stays allocated after the conversion"
    assert _ledger(pg_connection, ids["root_a"], ids["head"])["pr_reserved"] == 0
    lines = pg_connection.execute(
        "SELECT wbs_id, quantity, amount_paise FROM po_line WHERE po_id = %s ORDER BY line_no",
        (po["po_id"],)).fetchall()
    assert [(r[0], Decimal(r[1]), int(r[2])) for r in lines] == [
        (ids["child_a1"], Decimal(6), 300_00), (ids["child_b1"], Decimal(4), 400_00)]

    other = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a2", 200_00), "quantity": 2}])
    [only] = _pr_line_ids(pg_connection, other)
    whole = _decide(pg_database, only, imr_svc.MODE_INTERNAL, unit_rate_paise=100_00,
                    valuation_note="stores rate")
    with _system(pg_database) as session:
        imr_svc.approve_request(session, imr_id=whole["imr"]["imr_id"], actor=APPROVER)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=whole["imr"]["imr_id"], actor=STORES, idempotency_key="A")
    with pytest.raises(svc.ProcurementError) as exc:
        with _system(pg_database) as session:
            svc.convert_pr_to_po(session, pr_id=other, actor=BUYER, vendor_name="V")
    assert exc.value.code == "PR_FULLY_INTERNAL"
    assert pg_connection.execute(
        "SELECT COUNT(*) FROM purchase_order WHERE pr_id = %s", (other,)).fetchone()[0] == 0


# ---------------------------------------- 3. the lifecycle, no double counting
@pytest.mark.pg
@PG
def test_the_lifecycle_moves_money_between_limbs_and_never_counts_it_twice(
        pg_database, pg_connection):
    ids = _estate(pg_connection)
    head, cell, owner = ids["head"], ids["child_a1"], ids["root_a"]
    pr_id = _approved_pr(pg_database, ids, lines=[
        {**_line(ids, "child_a1", 1_000_00), "quantity": 10}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    assert _ledger(pg_connection, owner, head)["pr_reserved"] == 1_000_00
    assert _subtree_exposure(pg_connection, ids) == 1_000_00

    # Valued at 120/unit: 1,200 of stock against a 1,000 hold. The hold
    # covers 1,000; 200 is newly exposed and checked.
    imr_id = _internal_imr(pg_database, ids, line_id, rate=120_00)
    with _system(pg_database) as session:
        allocated = imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="alloc-1")
    assert (allocated["covered_by_hold_paise"], allocated["newly_exposed_paise"]) == (1_000_00, 200_00)
    assert allocated["status"] == "ALLOCATED"
    assert _ledger(pg_connection, owner, head)["pr_reserved"] == 0
    assert _ledger(pg_connection, cell, head)["internal_allocation"] == 1_200_00
    assert _subtree_exposure(pg_connection, ids) == 1_200_00, "rose by the uncovered 200 only"
    hold = pg_connection.execute(
        "SELECT amount_paise, internal_moved_paise, state FROM pr_reservation WHERE pr_id = %s",
        (pr_id,)).fetchone()
    assert (int(hold[0]), int(hold[1]), hold[2]) == (1_000_00, 1_000_00, "Reserved")

    # ISSUE 4: 480 moves from allocation to consumption; exposure unchanged.
    with _system(pg_database) as session:
        issued = imr_svc.issue(session, imr_id=imr_id, quantity=4, actor=STORES,
                               idempotency_key="issue-1", to_location_id=ids["loc_b"])
    assert issued["status"] == "PARTIALLY_ISSUED"
    assert (issued["internal_allocation_paise"], issued["internal_consumption_paise"]) == (720_00, 480_00)
    led = _ledger(pg_connection, cell, head)
    assert (led["internal_allocation"], led["internal_consumption"]) == (720_00, 480_00)
    assert _subtree_exposure(pg_connection, ids) == 1_200_00

    # TRANSFER 2 between stores: not consumption, no money moves.
    with _system(pg_database) as session:
        moved = imr_svc.transfer(session, imr_id=imr_id, quantity=2,
                                 to_location_id=ids["loc_b"], actor=STORES,
                                 idempotency_key="xfer-1")
    assert moved["from_location_id"] == ids["loc_b"]
    assert _ledger(pg_connection, cell, head) == led
    assert _subtree_exposure(pg_connection, ids) == 1_200_00

    # CONSUME 3: a confirmation; the money was booked at issue.
    with _system(pg_database) as session:
        consumed = imr_svc.consume(session, imr_id=imr_id, quantity=3, actor=STORES,
                                   idempotency_key="consume-1")
    assert consumed["status"] == "PARTIALLY_CONSUMED"
    assert _ledger(pg_connection, cell, head) == led

    # RETURN 1 of the 1 still on site: 120 released from consumption and
    # taken back into the request's hold -- exposure unchanged, but now the
    # hold, not the stock, carries it.
    with _system(pg_database) as session:
        returned = imr_svc.return_material(session, imr_id=imr_id, quantity=1,
                                           actor=STORES, idempotency_key="return-1")
    assert returned["internal_consumption_paise"] == 360_00
    assert _ledger(pg_connection, cell, head)["internal_consumption"] == 360_00
    assert _ledger(pg_connection, owner, head)["pr_reserved"] == 120_00
    assert _subtree_exposure(pg_connection, ids) == 1_200_00
    with pytest.raises(svc.ProcurementError) as too_much:
        with _system(pg_database) as session:
            imr_svc.return_material(session, imr_id=imr_id, quantity=1, actor=STORES,
                                    idempotency_key="return-2")
    assert too_much.value.code == "RETURN_EXCEEDS_ISSUED"

    # ISSUE the remaining 6: the close-out carries EXACTLY what is left of
    # the allocation, so nothing is stranded by rounding.
    with _system(pg_database) as session:
        closed = imr_svc.issue(session, imr_id=imr_id, quantity=6, actor=STORES,
                               idempotency_key="issue-2")
    assert (closed["internal_allocation_paise"], closed["internal_consumption_paise"]) == (0, 1_080_00)
    assert closed["status"] == "PARTIALLY_CONSUMED"
    with pytest.raises(svc.ProcurementError) as beyond:
        with _system(pg_database) as session:
            imr_svc.issue(session, imr_id=imr_id, quantity=1, actor=STORES, idempotency_key="issue-3")
    assert beyond.value.code == "ISSUE_EXCEEDS_ALLOCATION"

    with _system(pg_database) as session:
        done = imr_svc.consume(session, imr_id=imr_id, quantity=6, actor=STORES,
                               idempotency_key="consume-2")
    assert done["status"] == "CONSUMED"
    led = _ledger(pg_connection, cell, head)
    assert (led["internal_allocation"], led["internal_consumption"]) == (0, 1_080_00)
    assert _ledger(pg_connection, owner, head)["pr_reserved"] == 120_00
    assert _subtree_exposure(pg_connection, ids) == 1_200_00

    for wbs in (cell, owner):
        inv = _invariants(pg_database, wbs, head)
        assert inv["exposure_identity"] and inv["available_identity"] and inv["money_is_int"], inv
    kinds = [m[0] for m in _movements(pg_connection, imr_id)]
    assert kinds == ["ALLOCATE", "ISSUE", "TRANSFER", "CONSUME", "RETURN", "ISSUE", "CONSUME"]
    total = pg_connection.execute(
        "SELECT SUM(CASE WHEN kind = 'ALLOCATE' THEN amount_paise WHEN kind IN ('RETURN','CANCEL') "
        "THEN -amount_paise ELSE 0 END)::bigint FROM internal_material_movement WHERE imr_id = %s",
        (imr_id,)).fetchone()[0]
    assert int(total) == 1_080_00 == led["internal_allocation"] + led["internal_consumption"]

    # A terminal request unlocks the line for a fresh decision.
    again = _decide(pg_database, line_id, imr_svc.MODE_EXTERNAL)
    assert again["imr"] is None


# ------------------------------------------------------------ 4. idempotency
@pytest.mark.pg
@PG
def test_a_replayed_movement_moves_nothing_twice(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=100_00)
    for _ in range(2):
        with _system(pg_database) as session:
            imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")
    for _ in range(3):
        with _system(pg_database) as session:
            out = imr_svc.issue(session, imr_id=imr_id, quantity=2, actor=STORES, idempotency_key="I")
    assert out["issued_quantity"] == "2"
    assert _movements(pg_connection, imr_id) == [
        ("ALLOCATE", Decimal(5), 500_00), ("ISSUE", Decimal(2), 200_00)]
    assert _ledger(pg_connection, ids["child_a1"], ids["head"])["internal_consumption"] == 200_00
    with pytest.raises(svc.ProcurementError) as exc:
        with _system(pg_database) as session:
            imr_svc.issue(session, imr_id=imr_id, quantity=1, actor=STORES, idempotency_key="")
    assert exc.value.code == "IDEMPOTENCY_KEY_REQUIRED"


# ------------------------------------------------------------ 5. concurrency
@pytest.mark.pg
@PG
def test_concurrent_allocations_produce_one_and_concurrent_issues_never_exceed(
        pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 1_000_00), "quantity": 10}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=100_00)

    outcomes: list[str] = []
    lock = threading.Lock()

    def run(fn):
        try:
            with _system(pg_database) as session:
                fn(session)
            result = "ok"
        except svc.ProcurementError as exc:
            result = exc.code
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=run, args=(lambda s, k=k: imr_svc.allocate(
        s, imr_id=imr_id, actor=STORES, idempotency_key=k),)) for k in ("a1", "a2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["INVALID_TRANSITION", "ok"], outcomes
    assert [m[0] for m in _movements(pg_connection, imr_id)] == ["ALLOCATE"]

    outcomes.clear()
    threads = [threading.Thread(target=run, args=(lambda s, k=k: imr_svc.issue(
        s, imr_id=imr_id, quantity=6, actor=STORES, idempotency_key=k),)) for k in ("i1", "i2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ISSUE_EXCEEDS_ALLOCATION", "ok"], outcomes
    led = _ledger(pg_connection, ids["child_a1"], ids["head"])
    assert (led["internal_allocation"], led["internal_consumption"]) == (400_00, 600_00)


# ------------------------------------------------------------------ 6. cancel
@pytest.mark.pg
@PG
def test_cancel_releases_into_the_hold_and_refuses_stock_in_the_field(pg_database, pg_connection):
    ids = _estate(pg_connection)
    head, cell, owner = ids["head"], ids["child_a1"], ids["root_a"]
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 1_000_00), "quantity": 10}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=120_00)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")
    assert _subtree_exposure(pg_connection, ids) == 1_200_00
    with _system(pg_database) as session:
        imr_svc.issue(session, imr_id=imr_id, quantity=2, actor=STORES, idempotency_key="I")
    with pytest.raises(svc.ProcurementError) as in_field:
        with _system(pg_database) as session:
            imr_svc.cancel(session, imr_id=imr_id, reason="no longer needed", actor=STORES)
    assert in_field.value.code == "STOCK_IN_THE_FIELD"
    with _system(pg_database) as session:
        imr_svc.return_material(session, imr_id=imr_id, quantity=2, actor=STORES, idempotency_key="R")
    with _system(pg_database) as session:
        out = imr_svc.cancel(session, imr_id=imr_id, reason="no longer needed", actor=STORES)
    assert out["status"] == "CANCELLED"
    led = _ledger(pg_connection, cell, head)
    assert (led["internal_allocation"], led["internal_consumption"]) == (0, 0)
    assert _ledger(pg_connection, owner, head)["pr_reserved"] == 1_000_00, "the hold is whole again"
    assert _subtree_exposure(pg_connection, ids) == 1_000_00
    with pytest.raises(svc.ProcurementError) as twice:
        with _system(pg_database) as session:
            imr_svc.cancel(session, imr_id=imr_id, reason="again", actor=STORES)
    assert twice.value.code == "INVALID_TRANSITION"
    with pytest.raises(svc.ProcurementError) as unreasoned:
        with _system(pg_database) as session:
            imr_svc.cancel(session, imr_id=imr_id, reason="  ", actor=STORES)
    assert unreasoned.value.code == "REASON_REQUIRED"


# --------------------------------------------------------------- 7. valuation
@pytest.mark.pg
@PG
def test_a_missing_valuation_is_visible_and_blocks_allocation_until_resolved(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    out = _decide(pg_database, line_id, imr_svc.MODE_INTERNAL, item_external_id="ITEM-9")
    imr = out["imr"]
    assert (imr["valuation_source"], imr["unit_rate_paise"]) == ("MISSING", None)
    assert _open_exceptions(pg_connection, imr["imr_id"]) == [(KIND_VAL, "Open")]
    with _system(pg_database) as session:
        imr_svc.approve_request(session, imr_id=imr["imr_id"], actor=APPROVER)
    with pytest.raises(svc.ProcurementError) as blocked:
        with _system(pg_database) as session:
            imr_svc.allocate(session, imr_id=imr["imr_id"], actor=STORES, idempotency_key="A")
    assert blocked.value.code == KIND_VAL
    assert _movements(pg_connection, imr["imr_id"]) == []
    with pytest.raises(svc.ProcurementError) as zero:
        with _system(pg_database) as session:
            imr_svc.set_valuation(session, imr_id=imr["imr_id"], unit_rate_paise=0,
                                  note="free", actor=STORES)
    assert zero.value.code == "RATE_NOT_POSITIVE"
    with pytest.raises(svc.ProcurementError) as unreasoned:
        with _system(pg_database) as session:
            imr_svc.set_valuation(session, imr_id=imr["imr_id"], unit_rate_paise=90_00,
                                  note=" ", actor=STORES)
    assert unreasoned.value.code == "VALUATION_NOTE_REQUIRED"
    with _system(pg_database) as session:
        valued = imr_svc.set_valuation(session, imr_id=imr["imr_id"], unit_rate_paise=90_00,
                                       note="stores ledger, verified by U-STORES", actor=STORES)
    assert (valued["valuation_source"], valued["unit_rate_paise"]) == ("MANUAL", 90_00)
    assert _open_exceptions(pg_connection, imr["imr_id"]) == [(KIND_VAL, "Resolved")]
    with _system(pg_database) as session:
        allocated = imr_svc.allocate(session, imr_id=imr["imr_id"], actor=STORES, idempotency_key="A")
    assert allocated["internal_allocation_paise"] == 450_00
    with pytest.raises(svc.ProcurementError) as frozen:
        with _system(pg_database) as session:
            imr_svc.set_valuation(session, imr_id=imr["imr_id"], unit_rate_paise=1,
                                  note="late", actor=STORES)
    assert frozen.value.code == "VALUATION_FROZEN"


@pytest.mark.pg
@PG
def test_the_provider_boundary_is_asked_and_a_foreign_valuation_is_not_divided(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[
        {**_line(ids, "child_a1", 500_00), "quantity": 5},
        {**_line(ids, "child_a2", 500_00), "quantity": 5}])
    line1, line2 = _pr_line_ids(pg_connection, pr_id)
    provider = inventory.RecordingInventoryProvider()
    provider.value("ITEM-INR", 150_00)
    provider.value("ITEM-USD", 200, currency="USD")
    inventory.set_inventory_provider(provider)

    out = _decide(pg_database, line1, imr_svc.MODE_INTERNAL, item_external_id="ITEM-INR",
                  from_location_id=ids["loc_a"])
    assert (out["imr"]["valuation_source"], out["imr"]["unit_rate_paise"]) == ("ERP_STOCK", 150_00)
    assert out["imr"]["valuation_reference"].startswith("recording:")
    assert provider.asked[-1] == ("ITEM-INR", ids["loc_a"])
    assert _open_exceptions(pg_connection, out["imr"]["imr_id"]) == []

    foreign = _decide(pg_database, line2, imr_svc.MODE_INTERNAL, item_external_id="ITEM-USD")
    assert foreign["imr"]["valuation_source"] == "MISSING"
    assert "USD" in foreign["imr"]["valuation_note"]
    assert _open_exceptions(pg_connection, foreign["imr"]["imr_id"]) == [(KIND_VAL, "Open")]

    with pytest.raises(ValueError):
        inventory.StockValuation("X", None, 0, "INR", None, "t")
    with pytest.raises(TypeError):
        inventory.StockValuation("X", None, 1.5, "INR", None, "t")


# ----------------------------------------------------------------- 8. mapping
@pytest.mark.pg
@PG
def test_a_missing_mapping_is_visible_and_blocks_approval_until_the_owner_exists(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, reserve=False,
                         lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    # The pot vanishes after approval: no ancestor owns budget for the head.
    pg_connection.execute("UPDATE budget_control_cell SET budget_paise = 0 WHERE wbs_id = %s",
                          (ids["root_a"],))
    pg_connection.commit()
    out = _decide(pg_database, line_id, imr_svc.MODE_INTERNAL, unit_rate_paise=10_00,
                  valuation_note="rate")
    imr_id = out["imr"]["imr_id"]
    assert out["imr"]["mapping_ok"] is False
    assert _open_exceptions(pg_connection, imr_id) == [(KIND_MAP, "Open")]
    with pytest.raises(svc.ProcurementError) as blocked:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER)
    assert blocked.value.code == KIND_MAP
    pg_connection.execute("UPDATE budget_control_cell SET budget_paise = 10000000 WHERE wbs_id = %s",
                          (ids["root_a"],))
    pg_connection.commit()
    with _system(pg_database) as session:
        approved = imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER)
    assert approved["mapping_ok"] is True and approved["status"] == "APPROVED"
    assert _open_exceptions(pg_connection, imr_id) == [(KIND_MAP, "Resolved")]


# ------------------------------------------------------------ 9. maker-checker
@pytest.mark.pg
@PG
def test_the_requester_never_approves_their_own_request(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    out = _decide(pg_database, line_id, imr_svc.MODE_INTERNAL, actor=STORES,
                  unit_rate_paise=10_00, valuation_note="rate")
    imr_id = out["imr"]["imr_id"]
    with pytest.raises(svc.ProcurementError) as direct:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr_id, actor=STORES)
    assert direct.value.code == "SELF_APPROVAL"
    with pytest.raises(svc.ProcurementError) as delegated:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER,
                                    acting_for_user_id=STORES)
    assert delegated.value.code == "SELF_APPROVAL"
    principal = {"user_id": STORES, "roles": ["ProcurementApprover"]}
    with pytest.raises(svc.ProcurementError) as via_principal:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr_id, actor=STORES, principal=principal)
    assert via_principal.value.code == "SELF_APPROVAL"
    with pytest.raises(svc.ProcurementError) as wrong_role:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER,
                                    principal={"user_id": APPROVER, "roles": ["Requestor"]})
    assert wrong_role.value.status == 403
    assert pg_connection.execute(
        "SELECT status FROM internal_material_request WHERE imr_id = %s", (imr_id,)).fetchone()[0] == "REQUESTED"
    with _system(pg_database) as session:
        ok = imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER,
                                     principal={"user_id": APPROVER, "roles": ["ProcurementApprover"]})
    assert ok["status"] == "APPROVED" and ok["approved_by"] == APPROVER


# --------------------------------------------------------------------- 10. RLS
@pytest.mark.pg
@PG
def test_row_level_security_hides_another_entitys_requests(pg_database, pg_app_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=10_00)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")

    def visible(entity_ids):
        scope = Scope(user_id="U-RLS", principal_kind="USER", entity_ids=entity_ids,
                      plant_ids=None, project_ids=None, location_ids=None, read_all=False)
        with pg_app_database.session(scope) as session:
            # scope-exempt: this query IS the assertion about RLS.
            return [session.fetchone(f"SELECT COUNT(*) FROM {t} WHERE project_id = %s",
                                     (ids["project"],))[0]
                    for t in ("pr_line_fulfilment", "internal_material_request",
                              "internal_material_movement")]

    assert visible(frozenset({ids["entity"]})) == [1, 1, 1]
    assert visible(frozenset({"ENT-SOMEBODY-ELSE"})) == [0, 0, 0]
    stranger = Scope(user_id="U-RLS", principal_kind="USER",
                     entity_ids=frozenset({"ENT-SOMEBODY-ELSE"}), plant_ids=None,
                     project_ids=None, location_ids=None, read_all=False)
    with pytest.raises(svc.ProcurementError) as absent:
        with pg_app_database.session(stranger) as session:
            imr_svc.get_request(session, imr_id)
    assert absent.value.code == "IMR_NOT_FOUND"


# ------------------------------------------- 11. export, report, closure
@pytest.mark.pg
@PG
def test_the_export_the_report_and_the_closure_position_show_the_internal_figures_apart(
        pg_database, pg_connection):
    ids = _estate(pg_connection)
    head, cell = ids["head"], ids["child_a1"]
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 1_000_00), "quantity": 10}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=100_00)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")
    with _system(pg_database) as session:
        imr_svc.issue(session, imr_id=imr_id, quantity=3, actor=STORES, idempotency_key="I")
    number = pg_connection.execute(
        "SELECT imr_number FROM internal_material_request WHERE imr_id = %s", (imr_id,)).fetchone()[0]

    # The export: its own dataset, with the money derived from the movements.
    # The requester is a real principal with `read_all`, as `api/exports.py`
    # resolves one; a user with no grants is refused an empty export.
    user = f"U-EXPORT-{ids['entity']}"
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
        "VALUES (%s, %s, %s, 'T', 'T')", (user, f"{user}@example.test", user))
    pg_connection.execute(
        "INSERT INTO user_access_flag (user_id, read_all, updated_by) VALUES (%s, true, 'T')",
        (user,))
    pg_connection.commit()
    scope = principal_scope.scope_for_request(pg_database, {"user_id": user})
    with pg_database.session(scope) as session:
        job_id = export_svc.create_job(session, dataset="internal_material_requests",
                                       filters={"project_ids": [ids["project"]]},
                                       scope=scope, requested_by=user, chunk_rows=10)["export_job_id"]
    for _ in range(20):
        if not export_svc.advance_job(pg_database, job_id, rows_budget=50).get("more"):
            break
    with pg_database.session(scope) as session:
        text, meta = export_svc.read_result(session, job_id, requester=user)
    rows = list(csv.DictReader(io.StringIO(text)))
    [row] = [r for r in rows if r["imr_number"] == number]
    assert {"internal_allocation", "internal_consumption", "unit_rate"} <= set(row)
    assert row["status"] == "PARTIALLY_ISSUED" and row["issued_quantity"].startswith("3")
    assert row["internal_allocation"] == export_svc.format_paise(700_00)
    assert row["internal_consumption"] == export_svc.format_paise(300_00)

    # The ledger-cells export sums the two limbs into exposure.
    with pg_database.session(scope) as session:
        job2 = export_svc.create_job(session, dataset="budget_ledger_cells",
                                     filters={"project_ids": [ids["project"]]},
                                     scope=scope, requested_by=user, chunk_rows=10)["export_job_id"]
    for _ in range(20):
        if not export_svc.advance_job(pg_database, job2, rows_budget=50).get("more"):
            break
    with pg_database.session(scope) as session:
        text2, _meta = export_svc.read_result(session, job2, requester=user)
    [cell_row] = [r for r in csv.DictReader(io.StringIO(text2)) if r["wbs_id"] == cell]
    assert cell_row["internal_allocation"] == export_svc.format_paise(700_00)
    assert cell_row["internal_consumption"] == export_svc.format_paise(300_00)
    assert cell_row["exposure"] == export_svc.format_paise(1_000_00)

    # The report: the IMR document type supplies both buckets and exposure
    # counts them beside the external ones.
    with _system(pg_database) as session:
        card = rp.aggregate(session, rp.FilterSet.build(
            project_ids=[ids["project"]], document_types=["IMR"], group_by=["project"]))
    assert card["state"] == "ok"
    [group] = card["rows"]
    assert (group["internal_allocation"], group["internal_consumption"]) == (700_00, 300_00)
    assert group["exposure"] == 1_000_00
    with _system(pg_database) as session:
        whole = rp.aggregate(session, rp.FilterSet.build(
            project_ids=[ids["project"]], group_by=["project"]))
    [everything] = whole["rows"]
    assert everything["exposure"] == 1_000_00, "hold moved into the allocation: counted once"

    # The closure position: open allocation is a blocker, consumption is CWIP.
    with _system(pg_database) as session:
        position = closure._project_position(session, ids["project"])
    assert (position["internal_allocation_paise"], position["internal_consumption_paise"]) == (700_00, 300_00)
    assert position["cwip_balance_paise"] == 300_00, "consumed stock IS CWIP (review P1)"

    # The register and the detail read.
    with _system(pg_database) as session:
        listed = imr_svc.list_requests(session, project_id=ids["project"])
        detail = imr_svc.get_request(session, imr_id)
        decisions = imr_svc.fulfilment_of(session, pr_id)
    assert [i["imr_id"] for i in listed] == [imr_id]
    assert [m["kind"] for m in detail["movements"]] == ["ALLOCATE", "ISSUE"]
    assert detail["exceptions"] == []
    assert decisions[0]["mode"] == "INTERNAL_TRANSFER" and decisions[0]["imr_id"] == imr_id


# ------------------------------------------------------------------ 12. audit
@pytest.mark.pg
@PG
def test_every_verb_appends_to_the_audit_chain(pg_database, pg_connection):
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 500_00), "quantity": 5}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    out = _decide(pg_database, line_id, imr_svc.MODE_INTERNAL, from_location_id=ids["loc_a"])
    imr_id = out["imr"]["imr_id"]
    with _system(pg_database) as session:
        imr_svc.set_valuation(session, imr_id=imr_id, unit_rate_paise=10_00, note="rate", actor=STORES)
    with _system(pg_database) as session:
        imr_svc.approve_request(session, imr_id=imr_id, actor=APPROVER)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")
    with _system(pg_database) as session:
        imr_svc.transfer(session, imr_id=imr_id, quantity=1, to_location_id=ids["loc_b"],
                         actor=STORES, idempotency_key="T")
    with _system(pg_database) as session:
        imr_svc.issue(session, imr_id=imr_id, quantity=2, actor=STORES, idempotency_key="I")
    with _system(pg_database) as session:
        imr_svc.consume(session, imr_id=imr_id, quantity=1, actor=STORES, idempotency_key="C")
    with _system(pg_database) as session:
        imr_svc.return_material(session, imr_id=imr_id, quantity=1, actor=STORES, idempotency_key="R")
    with _system(pg_database) as session:
        imr_svc.issue(session, imr_id=imr_id, quantity=3, actor=STORES, idempotency_key="I2")
    with _system(pg_database) as session:
        imr_svc.consume(session, imr_id=imr_id, quantity=3, actor=STORES, idempotency_key="C2")
    actions = [r[0] for r in pg_connection.execute(
        "SELECT action FROM audit_log WHERE object_type = 'InternalMaterialRequest' "
        "AND object_id = %s ORDER BY audit_id", (imr_id,)).fetchall()]
    assert actions == ["IMR_REQUESTED", "IMR_VALUATION_SET", "IMR_APPROVED", "IMR_ALLOCATED",
                       "IMR_TRANSFERRED", "IMR_ISSUED", "IMR_CONSUMED", "IMR_RETURNED",
                       "IMR_ISSUED", "IMR_CONSUMED"]
    decided = pg_connection.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'FULFILMENT_DECIDED' AND object_id = %s",
        (pr_id,)).fetchone()[0]
    assert decided == 1
    status = pg_connection.execute(
        "SELECT status FROM internal_material_request WHERE imr_id = %s", (imr_id,)).fetchone()[0]
    assert status == "CONSUMED"


@pytest.mark.pg
@PG
def test_a_return_split_into_legs_strands_no_paisa(pg_database, pg_connection):
    """Adversarial review 2026-09-13, P2: the last return leg carries exactly
    what consumption still holds, so 0.02 + 0.49 + 0.49 of one unit at 101
    paise returns all 101 paise."""
    ids = _estate(pg_connection)
    pr_id = _approved_pr(pg_database, ids, lines=[{**_line(ids, "child_a1", 101), "quantity": 1}])
    [line_id] = _pr_line_ids(pg_connection, pr_id)
    imr_id = _internal_imr(pg_database, ids, line_id, rate=101)
    with _system(pg_database) as session:
        imr_svc.allocate(session, imr_id=imr_id, actor=STORES, idempotency_key="A")
    with _system(pg_database) as session:
        imr_svc.issue(session, imr_id=imr_id, quantity=1, actor=STORES, idempotency_key="I")
    assert _ledger(pg_connection, ids["child_a1"], ids["head"])["internal_consumption"] == 101
    for n, q in enumerate(("0.02", "0.49", "0.49")):
        with _system(pg_database) as session:
            out = imr_svc.return_material(session, imr_id=imr_id, quantity=q, actor=STORES,
                                          idempotency_key=f"R{n}")
    assert out["status"] == "RETURNED"
    led = _ledger(pg_connection, ids["child_a1"], ids["head"])
    assert (led["internal_allocation"], led["internal_consumption"]) == (0, 0)
