"""The critical financial-control findings.

    AUD-C-001  PO amendment can overspend and races are not serialised
    AUD-C-002  Budget-head control is presentation-only
    AUD-C-003  Document-line relationships are not integrity-safe
    AUD-C-004  Bill lifecycle and reversal semantics do not control actuals
    AUD-C-007  External-document uniqueness and sync idempotency are absent
    AUD-C-008  Closed and unreleased lifecycle states do not reliably block spend

Reference figures from the seeded dataset (integer paise):
    W-03 / BH-PM     budget 900,000,000   available 191,000,000
    W-02 / BH-CIVIL  budget 250,000,000   available 110,000,000
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from app.backend import auth, db, domain, services
from conftest import code_of, detail, insert_bill, insert_grn

PM_OWNER = ("W-03", "BH-PM")


# ============================================================ AUD-C-001 atomicity
def test_aud_c_001_over_budget_amendment_is_refused_with_409(procurement, cell):
    """Available on W-03/BH-PM is 19.1L; a 50L line is a 28L increase."""
    before = cell(*PM_OWNER)
    resp = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "5000000"})
    assert resp.status_code == 409, resp.text
    body = detail(resp)
    assert body["code"] == "AMENDMENT_EXCEEDS_BUDGET"
    assert "has NOT been applied" in body["message"]
    assert body["revalidation"]["verdict"] == "EXCEEDS_BUDGET"
    assert cell(*PM_OWNER) == before, "a refused amendment must not move the ledger"


def test_aud_c_001_a_refused_amendment_does_not_persist(procurement, raw_con):
    """The defect: the API computed EXCEEDS_BUDGET and then wrote anyway."""
    before_line = raw_con.execute(
        "SELECT amount_paise, rate_paise FROM po_line WHERE po_line_id='POL-0004'").fetchone()
    before_po = raw_con.execute(
        "SELECT amendment_no, version_no FROM purchase_order WHERE po_id='PO-004'").fetchone()

    assert procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "5000000"}).status_code == 409

    after_line = raw_con.execute(
        "SELECT amount_paise, rate_paise FROM po_line WHERE po_line_id='POL-0004'").fetchone()
    after_po = raw_con.execute(
        "SELECT amendment_no, version_no FROM purchase_order WHERE po_id='PO-004'").fetchone()
    assert tuple(after_line) == tuple(before_line)
    assert tuple(after_po) == tuple(before_po)


def test_aud_c_001_a_within_budget_amendment_is_applied(procurement, cell, raw_con):
    before = cell(*PM_OWNER)
    resp = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2300000"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert resp.json()["delta_paise"] == 10_000_000
    assert cell(*PM_OWNER)["available"] == before["available"] - 10_000_000
    assert raw_con.execute("SELECT amendment_no FROM purchase_order WHERE po_id='PO-004'"
                           ).fetchone()[0] == 1


def test_aud_c_001_a_decrease_releases_commitment(procurement, cell):
    before = cell(*PM_OWNER)
    resp = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2000000"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["delta_paise"] == -20_000_000
    assert cell(*PM_OWNER)["available"] == before["available"] + 20_000_000


def test_aud_c_001_an_over_budget_amendment_needs_an_approved_independent_exception(
        procurement, finance, cell):
    """PR-010 is a seeded over-budget request on the same control cell (W-03-02 / BH-PM)."""
    approved = finance.post("/api/purchase-requests/PR-010/approve",
                            json={"reason": "Board approval CAPEX-COMM/2026/041"})
    assert approved.status_code == 200, approved.text

    before = cell(*PM_OWNER)
    without = procurement.post("/api/purchase-orders/PO-005/amend", json={
        "po_line_id": "POL-0005", "new_amount_rupees": "5000000"})
    assert without.status_code == 409
    assert code_of(without) == "AMENDMENT_EXCEEDS_BUDGET"
    assert cell(*PM_OWNER) == before

    withref = procurement.post("/api/purchase-orders/PO-005/amend", json={
        "po_line_id": "POL-0005", "new_amount_rupees": "5000000",
        "exception_ref": "PR-010"})
    assert withref.status_code == 200, withref.text
    assert withref.json()["applied"] is True


def test_aud_c_001_an_unapproved_or_foreign_exception_reference_is_refused(procurement):
    unapproved = procurement.post("/api/purchase-orders/PO-005/amend", json={
        "po_line_id": "POL-0005", "new_amount_rupees": "5000000",
        "exception_ref": "PR-010"})           # PR-010 is still 'Exception Pending'
    assert unapproved.status_code == 422
    assert code_of(unapproved) == "EXCEPTION_INVALID"

    nonsense = procurement.post("/api/purchase-orders/PO-005/amend", json={
        "po_line_id": "POL-0005", "new_amount_rupees": "5000000",
        "exception_ref": "PR-DOES-NOT-EXIST"})
    assert nonsense.status_code == 422
    assert code_of(nonsense) == "EXCEPTION_INVALID"


def test_aud_c_001_an_exception_from_another_control_cell_is_refused(procurement, finance):
    """An approved exception on Civil may not authorise an overrun on Plant."""
    exception_pr = finance.post("/api/purchase-requests/PR-010/approve",
                                json={"reason": "approved for W-03-02 / BH-PM only"})
    assert exception_pr.status_code == 200
    # PO-006 line 1 sits on W-04 / BH-ELEC, a different cell entirely.
    resp = procurement.post("/api/purchase-orders/PO-006/amend", json={
        "po_line_id": "POL-0007", "new_amount_rupees": "40000000",
        "exception_ref": "PR-010"})
    assert resp.status_code == 422
    assert code_of(resp) == "EXCEPTION_INVALID"


def test_aud_c_001_optimistic_version_conflict_is_reported(procurement):
    stale = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2300000", "expected_version": 99})
    assert stale.status_code == 409
    assert code_of(stale) == "VERSION_CONFLICT"


def test_aud_c_001_a_cancelled_or_closed_po_cannot_be_amended(procurement):
    for po_id, line_id in (("PO-007", "POL-0008"), ("PO-014", "POL-0015")):
        resp = procurement.post(f"/api/purchase-orders/{po_id}/amend", json={
            "po_line_id": line_id, "new_amount_rupees": "100000"})
        assert resp.status_code == 409, (po_id, resp.text)
        assert code_of(resp) == "PO_NOT_AMENDABLE"


@pytest.mark.slow
def test_aud_c_001_concurrent_amendments_do_not_overspend(capex_db, ledger):
    """Two amendments that each fit alone but not together.

    Available on W-03 / BH-PM is 191,000,000 paise. Each thread asks for a
    100,000,000 increase, so either alone is within budget and both together are
    not. Exactly one must win, and availability must never go negative.
    """
    con = db.connect()
    try:
        actor = auth.principal(con, "U-PLH")
        before = domain.compute_ledger(con, "PRJ-01")["by_id"]["W-03"]["head_totals"]["BH-PM"]
    finally:
        con.close()
    assert before["available"] == 191_000_000

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    outcomes = []

    def amend(po_id, po_line_id, new_amount):
        connection = db.connect()
        try:
            barrier.wait(timeout=10)
            services.amend_po(connection, actor, po_id,
                              po_line_id=po_line_id, new_amount=new_amount)
            with lock:
                outcomes.append(("applied", po_id))
        except services.BusinessError as exc:
            with lock:
                outcomes.append((exc.code, po_id))
        except Exception as exc:                                  # noqa: BLE001
            with lock:
                outcomes.append((f"{type(exc).__name__}: {exc}", po_id))
        finally:
            connection.close()

    threads = [
        threading.Thread(target=amend, args=("PO-004", "POL-0004", "3200000")),
        threading.Thread(target=amend, args=("PO-003", "POL-0003", "4400000")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads), "an amendment thread deadlocked"

    applied = [o for o in outcomes if o[0] == "applied"]
    refused = [o for o in outcomes if o[0] == "AMENDMENT_EXCEEDS_BUDGET"]
    assert len(outcomes) == 2, outcomes
    assert len(applied) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(refused) == 1, f"expected exactly one controlled refusal, got {outcomes}"

    after = ledger("PRJ-01")["by_id"]["W-03"]["head_totals"]["BH-PM"]
    assert after["available"] >= 0, "concurrent amendments drove availability negative"
    assert after["available"] == before["available"] - 100_000_000
    assert after["exposure"] <= after["budget"]


@pytest.mark.slow
def test_aud_c_001_the_ledger_invariant_survives_a_burst_of_amendments(capex_db, ledger):
    """Eight concurrent increases against 19.1L of availability."""
    con = db.connect()
    try:
        actor = auth.principal(con, "U-PLH")
        lines = [r["po_line_id"] for r in con.execute(
            """SELECT pl.po_line_id FROM po_line pl JOIN purchase_order po ON po.po_id=pl.po_id
               WHERE pl.wbs_id IN ('W-03-01','W-03-02') AND po.status NOT IN ('Cancelled','Closed')
               ORDER BY pl.po_line_id""")]
        pos = {r["po_line_id"]: (r["po_id"], r["amount_paise"]) for r in con.execute(
            "SELECT po_line_id, po_id, amount_paise FROM po_line")}
    finally:
        con.close()
    assert len(lines) >= 3

    barrier = threading.Barrier(len(lines))
    results = []
    lock = threading.Lock()

    def bump(line_id):
        po_id, amount = pos[line_id]
        connection = db.connect()
        try:
            barrier.wait(timeout=15)
            services.amend_po(connection, actor, po_id, po_line_id=line_id,
                              new_amount=str((amount + 12_000_000) // 100))
            with lock:
                results.append(("applied", line_id))
        except services.BusinessError as exc:
            with lock:
                results.append((exc.code, line_id))
        except Exception as exc:                                  # noqa: BLE001
            with lock:
                results.append((f"{type(exc).__name__}: {exc}", line_id))
        finally:
            connection.close()

    threads = [threading.Thread(target=bump, args=(line,)) for line in lines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert all(not t.is_alive() for t in threads)
    assert len(results) == len(lines)
    assert all(not str(r[0]).startswith(("RecursionError", "OperationalError", "TypeError"))
               for r in results), results

    after = ledger("PRJ-01")["by_id"]["W-03"]["head_totals"]["BH-PM"]
    assert after["available"] >= 0, f"availability went negative: {results}"


# ==================================================== AUD-C-002 budget head control
def test_aud_c_002_a_head_with_no_budget_on_the_branch_is_refused(requestor):
    """W-03-01 sits under a Plant & Machinery budget; there is no Civil budget above it."""
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-CIVIL",
        "description": "civil spend on a plant element", "amount_rupees": "10000"})
    assert resp.status_code == 422, resp.text
    body = detail(resp)
    assert body["code"] == "NO_BUDGET_FOR_HEAD"
    assert "Civil" in body["message"]


def test_aud_c_002_the_correct_head_succeeds_on_the_same_element(requestor):
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "plant spend on a plant element", "amount_rupees": "10000"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["check"]["verdict"] == "WITHIN_BUDGET"
    assert resp.json()["check"]["budget_head"] == "Plant & Machinery"


def test_aud_c_002_spending_on_one_head_does_not_consume_another(requestor, cell):
    civil_before = cell("W-02", "BH-CIVIL")
    plant_before = cell("W-03", "BH-PM")

    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "plant reservation", "amount_rupees": "100000",
        "reserve_budget": True})
    assert resp.status_code == 201, resp.text

    assert cell("W-02", "BH-CIVIL") == civil_before, "Civil availability moved with Plant spend"
    assert cell("W-03", "BH-PM")["available"] == plant_before["available"] - 10_000_000


def test_aud_c_002_budget_check_is_evaluated_against_the_named_head(requestor):
    plant = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "10000", "budget_head_id": "BH-PM"}).json()
    civil = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "10000", "budget_head_id": "BH-CIVIL"}).json()
    assert plant["ok"] is True
    assert plant["budget_controlled_at"] == "CAPEX-2026-001.03"
    assert civil["ok"] is False
    assert civil["code"] == "NO_BUDGET_FOR_HEAD"


def test_aud_c_002_a_head_that_is_not_an_active_head_is_refused(requestor, raw_con):
    raw_con.execute("UPDATE budget_head SET active=0 WHERE budget_head_id='BH-PM'")
    raw_con.commit()
    resp = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "10000", "budget_head_id": "BH-PM"})
    assert resp.json()["code"] == "HEAD_UNKNOWN"


def test_aud_c_002_an_unknown_head_is_refused(requestor):
    resp = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "10000", "budget_head_id": "BH-NOPE"})
    assert resp.json()["code"] == "HEAD_UNKNOWN"


def test_aud_c_002_the_control_cell_is_the_nearest_ancestor_holding_that_head(requestor):
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-02-01-01", "amount_rupees": "10000",
        "budget_head_id": "BH-CIVIL"}).json()
    assert result["budget_owner_id"] == "W-02"
    assert result["budget_controlled_at"] == "CAPEX-2026-001.02"


def test_aud_c_002_budget_grid_is_keyed_on_wbs_and_head(requestor):
    rows = requestor.get("/api/projects/PRJ-01/budget-grid").json()["rows"]
    keys = [(r["wbs_id"], r["budget_head_id"]) for r in rows]
    assert len(keys) == len(set(keys)), "the budget grid pooled heads together"
    assert ("W-03", "BH-PM") in keys


# =============================================== AUD-C-003 relationship integrity
def test_aud_c_003_a_pr_whose_wbs_belongs_to_another_project_is_422(requestor, raw_con):
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-02", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "cross-project request", "amount_rupees": "10000"})
    assert resp.status_code == 422, resp.text
    assert code_of(resp) == "PROJECT_WBS_MISMATCH"
    assert raw_con.execute(
        "SELECT COUNT(*) FROM purchase_request WHERE description='cross-project request'"
    ).fetchone()[0] == 0


def test_aud_c_003_amending_a_line_that_belongs_to_another_po_is_422(procurement, raw_con):
    before = raw_con.execute(
        "SELECT amount_paise FROM po_line WHERE po_line_id='POL-0003'").fetchone()[0]
    resp = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0003", "new_amount_rupees": "2300000"})
    assert resp.status_code == 422, resp.text
    assert code_of(resp) == "LINE_PO_MISMATCH"
    assert raw_con.execute(
        "SELECT amount_paise FROM po_line WHERE po_line_id='POL-0003'").fetchone()[0] == before


def test_aud_c_003_amending_an_unknown_line_is_404(procurement):
    resp = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-9999", "new_amount_rupees": "100000"})
    assert resp.status_code == 404
    assert code_of(resp) == "LINE_NOT_FOUND"


def test_aud_c_003_a_bill_line_may_not_point_at_another_pos_line(raw_con):
    """Database trigger: BILL-002 is raised against PO-002, POL-0001 belongs to PO-001."""
    with pytest.raises(sqlite3.IntegrityError, match="same purchase order"):
        raw_con.execute("""INSERT INTO bill_line
            (bill_line_id, bill_id, po_line_id, wbs_id, budget_head_id, description,
             quantity, amount_paise, non_creditable_tax_paise, freight_paise)
            VALUES ('BLL-CROSS','BILL-002','POL-0001','W-02-01-01','BH-CIVIL','x',1,1000,0,0)""")
    raw_con.rollback()


def test_aud_c_003_a_bill_line_may_not_post_to_a_different_wbs_than_its_po_line(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="WBS and budget head"):
        raw_con.execute("""INSERT INTO bill_line
            (bill_line_id, bill_id, po_line_id, wbs_id, budget_head_id, description,
             quantity, amount_paise, non_creditable_tax_paise, freight_paise)
            VALUES ('BLL-WRONGWBS','BILL-002','POL-0002','W-03-01','BH-PM','x',1,1000,0,0)""")
    raw_con.rollback()


def test_aud_c_003_a_bill_line_may_not_be_repointed_after_the_fact(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="re-pointed"):
        raw_con.execute("UPDATE bill_line SET po_line_id='POL-0001' WHERE bill_line_id='BLL-0002'")
    raw_con.rollback()


def test_aud_c_003_a_grn_line_must_reference_its_own_purchase_order(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="same purchase order"):
        raw_con.execute("""INSERT INTO grn_line (grn_line_id, grn_id, po_line_id, quantity,
                           amount_paise) VALUES ('GRL-CROSS','GRN-001','POL-0002',1,1000)""")
    raw_con.rollback()


def test_aud_c_003_a_po_line_wbs_must_belong_to_the_pos_project(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="purchase order's project"):
        raw_con.execute("""INSERT INTO po_line VALUES
            ('POL-CROSS','PO-001',99,'W-P2-01','BH-CIVIL','x',1,1000,1000,0,0,0,NULL)""")
    raw_con.rollback()


def test_aud_c_003_a_purchase_request_wbs_must_belong_to_its_project(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="stated project"):
        raw_con.execute("""INSERT INTO purchase_request VALUES
            ('PR-CROSS','PR-CROSS-1','PRJ-02','W-03-01','BH-PM','x',1000,'U-REQ',
             '2026-08-06T00:00:00','Draft',NULL,NULL,NULL,NULL,0)""")
    raw_con.rollback()


def test_aud_c_003_a_wbs_element_cannot_be_its_own_parent(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="own parent"):
        raw_con.execute("""INSERT INTO wbs_element VALUES
            ('W-SELF','PRJ-01','W-SELF','SELF.01','self parent',1,1,'BH-PM','U-PM',
             '2026-04-01','2027-03-31',NULL,NULL,0,'Released',NULL,NULL,1,1,0)""")
    raw_con.rollback()


def test_aud_c_003_reparenting_may_not_create_a_cycle(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        raw_con.execute("UPDATE wbs_element SET parent_wbs_id='W-02-01-01' WHERE wbs_id='W-02'")
    raw_con.rollback()


def test_aud_c_003_a_child_must_share_its_parents_project(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="same project as its parent"):
        raw_con.execute("""INSERT INTO wbs_element VALUES
            ('W-FOREIGN','PRJ-02','W-02','FOREIGN.01','wrong project',2,1,'BH-CIVIL','U-PM',
             '2026-04-01','2027-03-31',NULL,NULL,0,'Released',NULL,NULL,1,1,0)""")
    raw_con.rollback()


@pytest.mark.parametrize("progress", [-1, 101, 250])
def test_aud_c_003_wbs_progress_is_bounded(raw_con, progress):
    with pytest.raises(sqlite3.IntegrityError, match="between 0 and 100"):
        raw_con.execute("UPDATE wbs_element SET progress_pct=? WHERE wbs_id='W-02'", (progress,))
    raw_con.rollback()


def test_aud_c_003_a_po_line_amount_may_not_be_negative(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="must not be negative"):
        raw_con.execute("""INSERT INTO po_line VALUES
            ('POL-NEG','PO-004',99,'W-03-01','BH-PM','x',1,-1000,-1000,0,0,0,NULL)""")
    raw_con.rollback()


# ==================================================== AUD-C-004 bill lifecycle
def test_aud_c_004_voiding_a_bill_removes_actual_and_restores_commitment(finance, cell,
                                                                        reconciliation):
    before = cell("W-02-01-01", "BH-CIVIL")
    assert before["actual"] == 80_000_000
    assert before["commitment"] == 0

    resp = finance.post("/api/bills/BILL-001/void", json={"reason": "Duplicate vendor invoice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["accounting_status"] == "Void"

    after = cell("W-02-01-01", "BH-CIVIL")
    assert after["actual"] == 0, "a void must remove the actual from the ledger"
    assert after["commitment"] == 80_000_000, "a void must restore the open commitment"
    assert after["exposure"] == before["exposure"], "exposure is unchanged by a void"

    line = next(r for r in reconciliation() if r["po_line_id"] == "POL-0001")
    assert line["billed_paise"] == 0
    assert line["open_commitment_paise"] == 80_000_000


def test_aud_c_004_voiding_requires_a_reason(finance):
    resp = finance.post("/api/bills/BILL-001/void", json={"reason": "   "})
    assert resp.status_code == 422
    assert code_of(resp) == "REASON_REQUIRED"


def test_aud_c_004_a_bill_cannot_be_voided_twice(finance):
    assert finance.post("/api/bills/BILL-001/void",
                        json={"reason": "duplicate"}).status_code == 200
    again = finance.post("/api/bills/BILL-001/void", json={"reason": "duplicate"})
    assert again.status_code == 409
    assert code_of(again) == "INVALID_TRANSITION"


def test_aud_c_004_voiding_an_unknown_bill_is_404(finance):
    resp = finance.post("/api/bills/BILL-999/void", json={"reason": "x"})
    assert resp.status_code == 404
    assert code_of(resp) == "BILL_NOT_FOUND"


def test_aud_c_004_a_duplicate_external_bill_id_is_rejected(raw_con):
    """AUD-C-007 external-document uniqueness at the database boundary."""
    existing = raw_con.execute(
        "SELECT external_source, external_id FROM bill WHERE external_id IS NOT NULL LIMIT 1"
    ).fetchone()
    assert existing["external_id"], "the seeded dataset should carry external ids"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        insert_bill(raw_con, bill_id="BILL-DUP", bill_number="BILL-2026-DUP",
                    po_id="PO-001", po_line_id="POL-0001", wbs_id="W-02-01-01",
                    budget_head_id="BH-CIVIL", amount_paise=1000,
                    external_source=existing["external_source"],
                    external_id=existing["external_id"])
    raw_con.rollback()


def test_aud_c_004_a_reversal_bill_negates_rather_than_adds(raw_con, cell):
    before = cell("W-02-01-02", "BH-CIVIL")
    insert_bill(raw_con, bill_id="BILL-REV", bill_number="BILL-2026-REV",
                po_id="PO-002", po_line_id="POL-0002", wbs_id="W-02-01-02",
                budget_head_id="BH-CIVIL", amount_paise=10_000_000,
                accounting_status="Reversal", is_reversal=1)
    raw_con.commit()
    after = cell("W-02-01-02", "BH-CIVIL")
    assert after["actual"] == before["actual"] - 10_000_000, \
        "a reversal document must reduce actual CWIP, not increase it"
    assert after["commitment"] == before["commitment"] + 10_000_000


def test_aud_c_004_a_reversal_stored_with_a_negative_amount_still_negates(raw_con, cell):
    """The sign comes from the document flag, not from whoever keyed the amount."""
    before = cell("W-02-01-02", "BH-CIVIL")
    insert_bill(raw_con, bill_id="BILL-REV2", bill_number="BILL-2026-REV2",
                po_id="PO-002", po_line_id="POL-0002", wbs_id="W-02-01-02",
                budget_head_id="BH-CIVIL", amount_paise=-10_000_000,
                accounting_status="Reversal", is_reversal=1)
    raw_con.commit()
    assert cell("W-02-01-02", "BH-CIVIL")["actual"] == before["actual"] - 10_000_000


@pytest.mark.parametrize("status", ["Void", "Draft", "Superseded"])
def test_aud_c_004_a_non_effective_bill_does_not_move_actual_cwip(raw_con, cell, status):
    before = cell("W-02-01-02", "BH-CIVIL")
    insert_bill(raw_con, bill_id=f"BILL-{status}", bill_number=f"BILL-2026-{status}",
                po_id="PO-002", po_line_id="POL-0002", wbs_id="W-02-01-02",
                budget_head_id="BH-CIVIL", amount_paise=10_000_000,
                accounting_status=status)
    raw_con.commit()
    assert cell("W-02-01-02", "BH-CIVIL")["actual"] == before["actual"]
    assert status not in domain.ACCOUNTING_EFFECTIVE_BILL_STATES


def test_aud_c_004_the_bills_endpoint_reports_accounting_effectiveness(requestor, finance):
    finance.post("/api/bills/BILL-001/void", json={"reason": "duplicate"})
    bills = {b["bill_id"]: b for b in requestor.get("/api/bills").json()}
    assert bills["BILL-001"]["accounting_effective"] is False
    assert bills["BILL-002"]["accounting_effective"] is True


def test_aud_c_004_a_void_is_recorded_in_the_audit_trail(finance, auditor):
    finance.post("/api/bills/BILL-001/void", json={"reason": "Duplicate vendor invoice"})
    entries = auditor.get("/api/audit").json()
    voided = [e for e in entries if e["action"] == "BILL_VOIDED"]
    assert voided, "voiding a bill must leave an audit entry"
    assert voided[0]["actor"] == "U-FIN"
    assert "Duplicate vendor invoice" in voided[0]["detail"]


# ==================================================== AUD-C-007 idempotency
def _pr_body(description="idempotent request"):
    return {"project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
            "description": description, "amount_rupees": "10000"}


def test_aud_c_007_repeating_a_create_with_the_same_key_produces_one_object(requestor, raw_con):
    headers = {"Idempotency-Key": "PR-KEY-001"}
    first = requestor.post("/api/purchase-requests", json=_pr_body(), headers=headers)
    second = requestor.post("/api/purchase-requests", json=_pr_body(), headers=headers)
    third = requestor.post("/api/purchase-requests", json=_pr_body(), headers=headers)

    assert first.status_code == 201, first.text
    assert second.json()["pr_id"] == first.json()["pr_id"] == third.json()["pr_id"]
    assert raw_con.execute(
        "SELECT COUNT(*) FROM purchase_request WHERE description='idempotent request'"
    ).fetchone()[0] == 1


def test_aud_c_007_distinct_keys_create_distinct_objects(requestor, raw_con):
    a = requestor.post("/api/purchase-requests", json=_pr_body(),
                       headers={"Idempotency-Key": "PR-KEY-A"})
    b = requestor.post("/api/purchase-requests", json=_pr_body(),
                       headers={"Idempotency-Key": "PR-KEY-B"})
    assert a.json()["pr_id"] != b.json()["pr_id"]
    assert raw_con.execute(
        "SELECT COUNT(*) FROM purchase_request WHERE description='idempotent request'"
    ).fetchone()[0] == 2


def test_aud_c_007_reusing_a_key_for_a_different_request_is_refused(requestor):
    headers = {"Idempotency-Key": "PR-KEY-CLASH"}
    assert requestor.post("/api/purchase-requests", json=_pr_body("first"),
                          headers=headers).status_code == 201
    clash = requestor.post("/api/purchase-requests", json=_pr_body("second"), headers=headers)
    assert clash.status_code == 409
    assert code_of(clash) == "IDEMPOTENCY_CONFLICT"


def test_aud_c_007_without_a_key_each_call_creates_its_own_object(requestor, raw_con):
    requestor.post("/api/purchase-requests", json=_pr_body())
    requestor.post("/api/purchase-requests", json=_pr_body())
    assert raw_con.execute(
        "SELECT COUNT(*) FROM purchase_request WHERE description='idempotent request'"
    ).fetchone()[0] == 2


def test_aud_c_007_repeating_a_sync_with_the_same_key_does_not_double_count(admin, raw_con):
    assert admin.post("/api/zoho/CONN-01/authorise", json={}).status_code == 200
    headers = {"Idempotency-Key": "SYNC-KEY-001"}
    first = admin.post("/api/zoho/CONN-01/sync/bills", headers=headers)
    second = admin.post("/api/zoho/CONN-01/sync/bills", headers=headers)

    assert first.status_code == 200, first.text
    assert first.json()["records"] == 9
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert "records" not in second.json(), "a replay must not report a second batch"

    events = raw_con.execute(
        """SELECT COUNT(*) FROM integration_event
           WHERE module='bills' AND direction='INBOUND'""").fetchone()[0]
    assert events == 1, "the same sync key created a second integration event"


def test_aud_c_007_external_documents_are_unique_per_source_and_organisation(raw_con):
    raw_con.execute("""INSERT INTO external_document
        (external_source, organization_id, module, external_id, local_type, local_id,
         first_seen_at, last_seen_at, seen_count)
        VALUES ('ZOHO_ERP','60021234567','bills','Z-1','Bill','BILL-001',
                '2026-08-06','2026-08-06',1)""")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        raw_con.execute("""INSERT INTO external_document
            (external_source, organization_id, module, external_id, local_type, local_id,
             first_seen_at, last_seen_at, seen_count)
            VALUES ('ZOHO_ERP','60021234567','bills','Z-1','Bill','BILL-002',
                    '2026-08-06','2026-08-06',1)""")
    raw_con.rollback()


# ==================================================== AUD-C-008 lifecycle gating
def test_aud_c_008_a_draft_project_refuses_procurement(requestor, raw_con):
    raw_con.execute("UPDATE project SET status='Draft' WHERE project_id='PRJ-01'")
    raw_con.commit()
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "1000", "budget_head_id": "BH-PM"}).json()
    assert result["ok"] is False
    assert result["code"] == "PROJECT_STATE"

    created = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "spend on a draft project", "amount_rupees": "1000"})
    assert created.status_code == 422
    assert code_of(created) == "PROJECT_STATE"


@pytest.mark.parametrize("state", ["Draft", "Submitted", "Approved", "Closed", "Capitalised",
                                   "Financially Completed"])
def test_aud_c_008_a_non_releasing_project_state_refuses_procurement(requestor, raw_con, state):
    raw_con.execute("UPDATE project SET status=? WHERE project_id='PRJ-01'", (state,))
    raw_con.commit()
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "1000", "budget_head_id": "BH-PM"}).json()
    assert result["ok"] is False
    assert result["code"] == "PROJECT_STATE"


def test_aud_c_008_a_closed_wbs_refuses_procurement(requestor, raw_con):
    raw_con.execute("UPDATE wbs_element SET status='Closed' WHERE wbs_id='W-03-01'")
    raw_con.commit()
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "1000", "budget_head_id": "BH-PM"}).json()
    assert result["ok"] is False
    assert result["code"] == "WBS_STATE"

    created = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "spend on a closed element", "amount_rupees": "1000"})
    assert created.status_code == 422
    assert code_of(created) == "WBS_STATE"


def test_aud_c_008_an_abandoned_wbs_refuses_procurement(requestor):
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-09", "amount_rupees": "1000", "budget_head_id": "BH-UTIL"}).json()
    assert result["ok"] is False
    assert result["code"] == "WBS_ABANDONED"


def test_aud_c_008_an_unknown_lifecycle_state_denies_by_default(raw_con):
    assert domain.lifecycle_permits(raw_con, "wbs", "Released", "procurement") is True
    assert domain.lifecycle_permits(raw_con, "wbs", "Invented State", "procurement") is False
    assert domain.lifecycle_permits(raw_con, "project", "Invented State", "posting") is False


def test_aud_c_008_permission_flags_follow_the_lifecycle_state(raw_con):
    raw_con.execute("UPDATE wbs_element SET status='Closed' WHERE wbs_id='W-03-01'")
    raw_con.commit()
    row = raw_con.execute(
        "SELECT allow_procurement, allow_posting FROM wbs_element WHERE wbs_id='W-03-01'"
    ).fetchone()
    assert row["allow_procurement"] == 0 and row["allow_posting"] == 0

    raw_con.execute("UPDATE wbs_element SET status='Released' WHERE wbs_id='W-03-01'")
    raw_con.commit()
    row = raw_con.execute(
        "SELECT allow_procurement, allow_posting FROM wbs_element WHERE wbs_id='W-03-01'"
    ).fetchone()
    assert row["allow_procurement"] == 1 and row["allow_posting"] == 1


def test_aud_c_008_technically_completed_permits_posting_but_not_procurement(raw_con):
    assert domain.lifecycle_permits(raw_con, "wbs", "Technically Completed", "procurement") is False
    assert domain.lifecycle_permits(raw_con, "wbs", "Technically Completed", "posting") is True


def test_aud_c_008_an_unknown_wbs_is_refused_rather_than_approved(requestor):
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-NOT-REAL", "amount_rupees": "1000", "budget_head_id": "BH-PM"}).json()
    assert result["ok"] is False
    assert result["code"] == "WBS_NOT_FOUND"


def test_aud_c_008_a_negative_proposed_commitment_is_refused(requestor):
    result = requestor.post("/api/budget-check", json={
        "wbs_id": "W-03-01", "amount_rupees": "-1000", "budget_head_id": "BH-PM"}).json()
    assert result["ok"] is False
    assert result["code"] == "NEGATIVE_AMOUNT"


def test_aud_c_008_a_bad_amount_is_a_controlled_refusal_not_a_500(requestor):
    """AUD-H-008 / AUD-M-007: a business refusal must never surface as a 500.

    ``Infinity`` and ``NaN`` are legal JSON literals to Python's parser, so they
    reach the money layer and must come back as 422 INVALID_AMOUNT.
    """
    headers = {"content-type": "application/json"}
    for literal in ("Infinity", "-Infinity", "NaN"):
        body = ('{"wbs_id":"W-03-01","budget_head_id":"BH-PM","amount_rupees":'
                + literal + "}")
        resp = requestor.post("/api/budget-check", headers=headers, content=body)
        assert resp.status_code == 422, f"{literal} -> {resp.status_code}: {resp.text[:160]}"
        assert code_of(resp) == "INVALID_AMOUNT"


@pytest.mark.parametrize("amount", ["not-a-number", "1.2.3", "", "1e400"])
def test_aud_c_008_an_unparseable_amount_is_a_controlled_refusal(requestor, amount):
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "bad amount", "amount_rupees": amount})
    assert resp.status_code == 422, f"{amount!r} -> {resp.status_code}: {resp.text[:160]}"
    assert code_of(resp) == "INVALID_AMOUNT"


def test_aud_c_008_goods_received_but_unbilled_is_its_own_exposure_bucket(raw_con, cell):
    before = cell("W-03-01", "BH-PM")
    insert_grn(raw_con, grn_id="GRN-TEST", grn_number="GRN-2026-TEST", po_id="PO-004",
               po_line_id="POL-0004", amount_paise=50_000_000)
    raw_con.commit()
    after = cell("W-03-01", "BH-PM")
    assert after["received"] == before["received"] + 50_000_000
    assert after["received_not_billed"] == before["received_not_billed"] + 50_000_000
    # receiving alone does not change exposure: it is still committed, not yet actual
    assert after["exposure"] == before["exposure"]
