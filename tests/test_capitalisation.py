"""AUD-C-009 - Capitalization can occur with unresolved and invalid values.

Eligibility must be a gate, not a warning:
  * open commitment or received-but-unbilled value blocks capitalisation;
  * a negative or zero allocation is refused;
  * a write-off without a recorded reason is refused;
  * the allocation total must equal the CWIP balance exactly;
  * an approved request cannot be approved a second time.

PRJ-02 is used for the successful path because it can be brought to a clean
position (no open commitment, nothing received-but-unbilled) by closing its two
purchase orders. Its CWIP balance is 168,000,000 paise.
"""
from __future__ import annotations

import sqlite3

import pytest

from conftest import code_of, detail, insert_grn

PRJ02_CWIP_PAISE = 168_000_000


@pytest.fixture()
def cap_request(raw_con):
    """A Submitted capitalisation request on PRJ-02, raised by U-PFC."""
    raw_con.execute("""INSERT INTO capitalisation_request
        (cap_id, cap_number, project_id, requested_by, requested_at, status, approver,
         approved_at, cap_date, total_paise, version_no)
        VALUES ('CAP-002','CAP-2026-0002','PRJ-02','U-PFC','2026-08-05T00:00:00','Submitted',
                NULL,NULL,'2026-08-31',0,1)""")
    raw_con.commit()
    return "CAP-002"


@pytest.fixture()
def clean_project(procurement, cap_request, ledger):
    """Release PRJ-02's commitment so only the CWIP balance remains."""
    for po_id in ("PO-010", "PO-011"):
        resp = procurement.post(f"/api/purchase-orders/{po_id}/close",
                                json={"reason": "Scope complete; residual released."})
        assert resp.status_code == 200, resp.text
    totals = ledger("PRJ-02")["totals"]
    assert totals["commitment"] == 0
    assert totals["received_not_billed"] == 0
    assert totals["actual"] == PRJ02_CWIP_PAISE
    return cap_request


# =============================================================== the gate
def test_aud_c_009_capitalisation_is_blocked_while_commitment_remains(capitalisation):
    """CAP-001 sits on PRJ-01, which still carries 55,50,000 of open commitment."""
    resp = capitalisation.post("/api/capitalisation/CAP-001/approve", json={})
    assert resp.status_code == 409, resp.text
    body = detail(resp)
    assert body["code"] == "CAPITALISATION_BLOCKED"
    assert body["message_id"] == "MSG-CAP-001"
    assert any("open commitment" in b for b in body["blockers"])


def test_aud_c_009_capitalisation_is_blocked_while_value_is_received_not_billed(
        procurement, capitalisation, raw_con, cap_request, ledger):
    insert_grn(raw_con, grn_id="GRN-RNB", grn_number="GRN-2026-RNB", po_id="PO-011",
               po_line_id="POL-0012", amount_paise=140_000_000)
    raw_con.commit()
    for po_id in ("PO-010", "PO-011"):
        assert procurement.post(f"/api/purchase-orders/{po_id}/close",
                                json={"reason": "closing"}).status_code == 200

    totals = ledger("PRJ-02")["totals"]
    assert totals["commitment"] == 0
    assert totals["received_not_billed"] == 140_000_000

    resp = capitalisation.post(f"/api/capitalisation/{cap_request}/approve", json={})
    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == "CAPITALISATION_BLOCKED"
    assert any("received but not billed" in b for b in body["blockers"])


def test_aud_c_009_capitalisation_is_blocked_by_an_open_reconciliation_exception(
        capitalisation, clean_project, finance, raw_con):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Warehouse shed",
        "amount_rupees": str(PRJ02_CWIP_PAISE // 100)}).status_code == 201
    raw_con.execute("""INSERT INTO reconciliation_exception
        (exception_id, raised_at, object_type, object_id, kind, detail, status)
        VALUES ('EXC-001','2026-08-05T00:00:00','Bill','BILL-010','OVER_BILLED',
                'Bill exceeds PO','Open')""")
    raw_con.commit()

    resp = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={})
    assert resp.status_code == 409
    assert any("reconciliation exception" in b for b in detail(resp)["blockers"])


# =============================================================== allocations
@pytest.mark.parametrize("amount", ["-1000", "-0.01"])
def test_aud_c_009_a_negative_allocation_is_refused(finance, cap_request, amount):
    resp = finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Negative asset", "amount_rupees": amount})
    assert resp.status_code == 422, resp.text
    assert code_of(resp) in ("INVALID_AMOUNT", "NON_POSITIVE_ALLOCATION")


def test_aud_c_009_a_zero_allocation_is_refused(finance, cap_request):
    resp = finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Zero asset", "amount_rupees": "0"})
    assert resp.status_code == 422
    assert code_of(resp) == "NON_POSITIVE_ALLOCATION"


def test_aud_c_009_the_database_also_refuses_a_non_positive_allocation(raw_con, cap_request):
    """Second line of defence, independent of the service layer."""
    for amount in (-100, 0):
        with pytest.raises(sqlite3.IntegrityError, match="must be positive"):
            raw_con.execute("INSERT INTO asset_allocation VALUES (?,?,?,?,?,?,0,NULL,NULL)",
                            (f"ALLOC-BAD-{amount}", cap_request, "W-P2-01", "Bad", "Land", amount))
        raw_con.rollback()


def test_aud_c_009_a_write_off_without_a_reason_is_refused(finance, cap_request):
    resp = finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Abandoned works", "amount_rupees": "1000",
        "is_writeoff": True})
    assert resp.status_code == 422
    assert code_of(resp) == "REASON_REQUIRED"

    blank = finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Abandoned works", "amount_rupees": "1000",
        "is_writeoff": True, "writeoff_reason": "   "})
    assert blank.status_code == 422
    assert code_of(blank) == "REASON_REQUIRED"


def test_aud_c_009_the_database_also_requires_a_write_off_reason(raw_con, cap_request):
    with pytest.raises(sqlite3.IntegrityError, match="requires a recorded reason"):
        raw_con.execute("INSERT INTO asset_allocation VALUES (?,?,?,?,?,?,1,NULL,NULL)",
                        ("ALLOC-WO", cap_request, "W-P2-01", "Abandoned", "Land", 1000))
    raw_con.rollback()


def test_aud_c_009_a_write_off_with_a_reason_is_accepted_and_recorded(finance, cap_request,
                                                                     raw_con, auditor):
    resp = finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Abandoned foundation", "amount_rupees": "1000",
        "is_writeoff": True,
        "writeoff_reason": "Design abandoned after geotechnical survey; no future benefit."})
    assert resp.status_code == 201, resp.text
    row = raw_con.execute("SELECT is_writeoff, writeoff_reason FROM asset_allocation "
                          "WHERE allocation_id=?", (resp.json()["allocation_id"],)).fetchone()
    assert row["is_writeoff"] == 1
    assert "geotechnical" in row["writeoff_reason"]
    assert any("write-off" in e["detail"] for e in auditor.get("/api/audit").json())


def test_aud_c_009_allocations_accumulate_into_the_request_total(finance, cap_request, raw_con):
    for amount in ("1000", "2500", "700"):
        assert finance.post(f"/api/capitalisation/{cap_request}/allocate", json={
            "wbs_id": "W-P2-01", "asset_name": f"Asset {amount}",
            "amount_rupees": amount}).status_code == 201
    total = raw_con.execute("SELECT total_paise FROM capitalisation_request WHERE cap_id=?",
                            (cap_request,)).fetchone()[0]
    assert total == 420_000


def test_aud_c_009_allocation_is_closed_once_the_request_is_approved(finance, capitalisation,
                                                                    clean_project):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Warehouse shed",
        "amount_rupees": str(PRJ02_CWIP_PAISE // 100)}).status_code == 201
    assert capitalisation.post(f"/api/capitalisation/{clean_project}/approve",
                               json={}).status_code == 200

    late = finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Late addition", "amount_rupees": "1000"})
    assert late.status_code == 409
    assert code_of(late) == "INVALID_TRANSITION"


# ================================================= allocation must equal CWIP
def test_aud_c_009_allocation_total_must_equal_the_cwip_balance(finance, capitalisation,
                                                                clean_project):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Partial", "amount_rupees": "1000"}).status_code == 201

    resp = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={})
    assert resp.status_code == 422, resp.text
    body = detail(resp)
    assert body["code"] == "ALLOCATION_MISMATCH"
    assert body["message_id"] == "MSG-CAP-002"


def test_aud_c_009_over_allocating_is_refused_too(finance, capitalisation, clean_project):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Too much",
        "amount_rupees": str(PRJ02_CWIP_PAISE // 100 + 1000)}).status_code == 201
    resp = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={})
    assert resp.status_code == 422
    assert code_of(resp) == "ALLOCATION_MISMATCH"


def test_aud_c_009_a_fully_allocated_clean_project_capitalises(finance, capitalisation,
                                                               clean_project, raw_con):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Warehouse shed", "asset_category": "Buildings",
        "amount_rupees": str((PRJ02_CWIP_PAISE - 8_000_000) // 100)}).status_code == 201
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-02", "asset_name": "Racking", "asset_category": "Plant & Machinery",
        "amount_rupees": "80000"}).status_code == 201

    resp = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Approved"
    assert body["capitalised_paise"] == PRJ02_CWIP_PAISE
    assert raw_con.execute("SELECT status FROM project WHERE project_id='PRJ-02'"
                           ).fetchone()[0] == "Capitalised"


def test_aud_c_009_a_second_approval_is_refused(finance, capitalisation, clean_project):
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Warehouse shed",
        "amount_rupees": str(PRJ02_CWIP_PAISE // 100)}).status_code == 201
    assert capitalisation.post(f"/api/capitalisation/{clean_project}/approve",
                               json={}).status_code == 200

    again = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={})
    assert again.status_code == 409
    assert code_of(again) == "INVALID_TRANSITION"


def test_aud_c_009_approval_does_not_claim_a_general_ledger_posting(finance, capitalisation,
                                                                    clean_project, auditor):
    """AUD-M-002: the build must not overstate what it has actually done."""
    assert finance.post(f"/api/capitalisation/{clean_project}/allocate", json={
        "wbs_id": "W-P2-01", "asset_name": "Warehouse shed",
        "amount_rupees": str(PRJ02_CWIP_PAISE // 100)}).status_code == 201
    body = capitalisation.post(f"/api/capitalisation/{clean_project}/approve", json={}).json()
    assert "NOT POSTED" in body["posting_status"]
    detail_text = " ".join(e["detail"] for e in auditor.get("/api/audit").json())
    assert "NOT YET POSTED" in detail_text


def test_aud_c_009_approving_an_unknown_request_is_404(capitalisation):
    resp = capitalisation.post("/api/capitalisation/CAP-999/approve", json={})
    assert resp.status_code == 404
    assert code_of(resp) == "CAP_NOT_FOUND"


def test_aud_c_009_the_capitalisation_listing_reports_readiness_honestly(finance):
    caps = {c["cap_id"]: c for c in finance.get("/api/capitalisation").json()}
    cap = caps["CAP-001"]
    assert cap["ready"] is False
    assert cap["open_commitment_paise"] > 0
    assert "NOT POSTED" in cap["posting_status"]
