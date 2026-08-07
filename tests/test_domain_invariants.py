"""Financial invariants of the control engine.

Covers the frozen formula registry and the budget-governance corrections:

    Exposure         = Open PO Commitment + Actual CWIP + live PR reservation
    Available Budget = Current Approved Budget - Exposure
    Current Approved Budget = immutable ORIGINAL + APPROVED *and* EFFECTIVE revisions

Audit findings exercised here:
    AUD-C-005  approved budget is an approved, effective, immutable value
    AUD-H-001  PR reservations are a real limb of exposure
    AUD-M-005  deep hierarchies must not raise RecursionError
"""
from __future__ import annotations

import sqlite3

import pytest

from app.backend import domain
from conftest import build_deep_wbs, code_of, insert_bill

ALL_PROJECTS = ["PRJ-01", "PRJ-02", "PRJ-03"]


def _all_cells(ledger):
    """Every (project, wbs_id, head, cell) own-level control cell in the dataset."""
    for project_id in ALL_PROJECTS:
        led = ledger(project_id)
        for wbs_id, node in led["by_id"].items():
            for head_id, cell in node["heads"].items():
                yield project_id, wbs_id, head_id, cell


def _all_rollups(ledger):
    for project_id in ALL_PROJECTS:
        led = ledger(project_id)
        for wbs_id, node in led["by_id"].items():
            for head_id, cell in node["head_totals"].items():
                yield project_id, wbs_id, head_id, cell


# ================================================================== the formulas
def test_available_equals_budget_minus_actual_commitment_and_reservation(ledger):
    """Available = Approved Budget - Actual - Open Commitment - live PR reservation."""
    seen = 0
    for project_id, wbs_id, head_id, cell in _all_rollups(ledger):
        seen += 1
        expected = (cell["budget"] - cell["actual"] - cell["commitment"] - cell["pr_reserved"])
        assert cell["available"] == expected, (project_id, wbs_id, head_id, cell)
    assert seen > 0, "the dataset produced no control cells to check"


def test_exposure_is_the_sum_of_commitment_actual_and_reservation(ledger):
    for project_id, wbs_id, head_id, cell in _all_rollups(ledger):
        assert cell["exposure"] == cell["commitment"] + cell["actual"] + cell["pr_reserved"], \
            (project_id, wbs_id, head_id)


def test_utilisation_and_band_derive_from_exposure_and_budget(ledger):
    for project_id, wbs_id, head_id, cell in _all_rollups(ledger):
        if cell["budget"]:
            assert cell["utilisation_pct"] == round(
                cell["exposure"] / cell["budget"] * 100.0, 1)
        else:
            assert cell["utilisation_pct"] == 0.0
        if cell["budget"] and cell["exposure"] > cell["budget"]:
            assert cell["band"] == "breach"
        elif cell["utilisation_pct"] >= domain.CRITICAL_PCT:
            assert cell["band"] == "critical"
        elif cell["utilisation_pct"] >= domain.WATCH_PCT:
            assert cell["band"] == "watch"
        else:
            assert cell["band"] == "safe"


def test_rollup_equals_own_plus_children(ledger):
    """The iterative bottom-up rollup must agree with a direct child sum."""
    for project_id in ALL_PROJECTS:
        led = ledger(project_id)
        for wbs_id, node in led["by_id"].items():
            for component in domain._COMPONENTS:
                expected = node["own"][component] + sum(
                    child["total"][component] for child in node["children"])
                assert node["total"][component] == expected, (wbs_id, component)


def test_money_is_only_ever_integer_paise(ledger):
    """No float may reach a monetary value (AUD-H-007 at the ledger boundary)."""
    monetary = [c for c in domain._COMPONENTS] + ["exposure", "available"]
    for project_id, wbs_id, head_id, cell in _all_rollups(ledger):
        for key in monetary:
            assert isinstance(cell[key], int) and not isinstance(cell[key], bool), \
                (wbs_id, head_id, key, type(cell[key]))


# ================================================= PO line commitment invariants
def test_po_line_billed_plus_open_commitment_equals_ordered(reconciliation):
    """For every live PO line: billed + open commitment == ordered.

    Released (Cancelled/Closed) POs are excluded because closure deliberately
    releases the residual. Over-billed lines are excluded here and asserted
    separately: they are a recorded reconciliation exception, not a silent one.
    """
    checked = 0
    for line in reconciliation():
        if line["po_status"] in domain.COMMITMENT_RELEASING_STATES:
            continue
        if line["billed_paise"] > line["ordered_paise"]:
            continue
        checked += 1
        assert line["billed_paise"] + line["open_commitment_paise"] == line["ordered_paise"], line
    assert checked >= 5, "too few live PO lines to make this invariant meaningful"


def test_over_billed_lines_are_flagged_rather_than_silently_absorbed(reconciliation):
    over = [ln for ln in reconciliation() if ln["billed_paise"] > ln["ordered_paise"]]
    assert over, "the seeded dataset is expected to carry one deliberate over-billing"
    for line in over:
        assert line["flag"] == "over-billed"
        assert line["position"] == "Bill exceeds PO"
        assert line["open_commitment_paise"] == 0


def test_released_purchase_orders_hold_no_commitment(reconciliation):
    released = [ln for ln in reconciliation()
                if ln["po_status"] in domain.COMMITMENT_RELEASING_STATES]
    assert released, "the seeded dataset is expected to carry a cancelled and a closed PO"
    for line in released:
        assert line["open_commitment_paise"] == 0
        assert line["residual_released_paise"] == max(
            0, line["ordered_paise"] - line["billed_paise"])


def test_exposure_is_unchanged_by_billing_progress(raw_con, ledger, cell):
    """Billing converts commitment into actual. Total exposure must not move."""
    before_total = ledger("PRJ-01")["totals"]
    before_cell = cell("W-03-01", "BH-PM")

    insert_bill(raw_con, bill_id="BILL-TEST-1", bill_number="BILL-TEST-0001",
                po_id="PO-004", po_line_id="POL-0004", wbs_id="W-03-01",
                budget_head_id="BH-PM", amount_paise=50_000_000)
    raw_con.commit()

    after_total = ledger("PRJ-01")["totals"]
    after_cell = cell("W-03-01", "BH-PM")

    assert after_total["exposure"] == before_total["exposure"]
    assert after_total["available"] == before_total["available"]
    assert after_cell["exposure"] == before_cell["exposure"]
    # ... and it moved between the limbs, so the test is not vacuous
    assert after_cell["actual"] == before_cell["actual"] + 50_000_000
    assert after_cell["commitment"] == before_cell["commitment"] - 50_000_000


def test_aud_h_001_live_pr_reservation_consumes_availability(requestor, cell, raw_con):
    before = cell("W-03", "BH-PM")
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "reserved request", "amount_rupees": "100000",
        "reserve_budget": True})
    assert resp.status_code == 201, resp.text

    after = cell("W-03", "BH-PM")
    assert after["pr_reserved"] == before["pr_reserved"] + 10_000_000
    assert after["available"] == before["available"] - 10_000_000
    assert after["exposure"] == before["exposure"] + 10_000_000
    live = raw_con.execute(
        "SELECT COUNT(*) FROM pr_reservation WHERE state='Reserved'").fetchone()[0]
    assert live == 1


def test_aud_h_001_a_reservation_converts_exactly_once(requestor, procurement, raw_con):
    created = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "reserved request", "amount_rupees": "100000",
        "reserve_budget": True}).json()

    first = procurement.post("/api/purchase-requests/convert",
                             json={"pr_id": created["pr_id"], "po_id": "PO-004"})
    assert first.status_code == 200, first.text
    second = procurement.post("/api/purchase-requests/convert",
                              json={"pr_id": created["pr_id"], "po_id": "PO-004"})
    assert second.status_code == 409
    assert code_of(second) == "NO_LIVE_RESERVATION"
    states = [r[0] for r in raw_con.execute(
        "SELECT state FROM pr_reservation WHERE pr_id=?", (created["pr_id"],))]
    assert states == ["Converted"]


def test_aud_h_001_only_one_live_reservation_per_request(raw_con):
    """The partial unique index is the database-level guarantee."""
    raw_con.execute("""INSERT INTO pr_reservation
        (reservation_id, pr_id, wbs_id, budget_head_id, amount_paise, state, created_at)
        VALUES ('RES-A','PR-015','W-02-02','BH-CIVIL',1000,'Reserved','2026-08-06')""")
    with pytest.raises(sqlite3.IntegrityError):
        raw_con.execute("""INSERT INTO pr_reservation
            (reservation_id, pr_id, wbs_id, budget_head_id, amount_paise, state, created_at)
            VALUES ('RES-B','PR-015','W-02-02','BH-CIVIL',1000,'Reserved','2026-08-06')""")
    raw_con.rollback()


# ====================================================== AUD-C-005 budget governance
def test_aud_c_005_current_budget_is_original_plus_approved_effective_revisions(raw_con, ledger):
    """Cross-check the ledger against the register with the governance rules applied."""
    from datetime import date
    today = date.today().isoformat()
    expected = {}
    for row in raw_con.execute(
            """SELECT wbs_id, budget_head_id, SUM(amount_paise) amt FROM budget_line
               WHERE status='Approved' AND effective_date IS NOT NULL AND effective_date <= ?
               GROUP BY wbs_id, budget_head_id""", (today,)):
        expected[(row["wbs_id"], row["budget_head_id"])] = row["amt"]

    seen = 0
    for project_id in ALL_PROJECTS:
        led = ledger(project_id)
        for wbs_id, node in led["by_id"].items():
            for head_id, own in node["heads"].items():
                assert own["budget"] == expected.get((wbs_id, head_id), 0), (wbs_id, head_id)
                assert own["budget"] == own["original"] + own["revisions"], (wbs_id, head_id)
                seen += 1
    assert seen > 0


def test_aud_c_005_seeded_transfer_moved_budget_without_creating_any(cell):
    """REV-002 transferred 3,00,000 from Installation to Electrical."""
    electrical = cell("W-04", "BH-ELEC")
    installation = cell("W-05", "BH-INST")
    assert electrical["original"] == 200_000_000
    assert electrical["revisions"] == 30_000_000
    assert electrical["budget"] == 230_000_000
    assert installation["original"] == 150_000_000
    assert installation["revisions"] == -30_000_000
    assert installation["budget"] == 120_000_000
    assert electrical["revisions"] + installation["revisions"] == 0


@pytest.mark.parametrize("status", ["Draft", "Submitted", "Rejected", "Cancelled"])
def test_aud_c_005_unapproved_budget_lines_create_no_spending_capacity(raw_con, cell, status):
    before = cell("W-03", "BH-PM")
    raw_con.execute("""INSERT INTO budget_line
        (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
         version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
         created_at, created_by)
        VALUES ('BL-UNAPPROVED','PRJ-01','W-03','BH-PM','SUPPLEMENT',500000000,?,
                1,NULL,'2026-01-01',NULL,NULL,NULL,'2026-08-06T00:00:00','U-PM')""", (status,))
    raw_con.commit()
    after = cell("W-03", "BH-PM")
    assert after["budget"] == before["budget"], f"{status} budget line changed availability"
    assert after["available"] == before["available"]


def test_aud_c_005_future_effective_revision_creates_no_capacity_today(raw_con, cell):
    before = cell("W-03", "BH-PM")
    raw_con.execute("""INSERT INTO budget_line
        (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
         version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
         created_at, created_by)
        VALUES ('BL-FUTURE','PRJ-01','W-03','BH-PM','SUPPLEMENT',500000000,'Approved',
                1,NULL,'2999-01-01','U-CFO','2026-08-06T00:00:00','ref',
                '2026-08-06T00:00:00','U-PM')""")
    raw_con.commit()
    assert cell("W-03", "BH-PM")["budget"] == before["budget"]
    # it does become effective once the date arrives
    from app.backend import domain as _domain
    led = _domain.compute_ledger(raw_con, "PRJ-01", as_of="2999-01-02")
    assert led["by_id"]["W-03"]["head_totals"]["BH-PM"]["budget"] == before["budget"] + 500_000_000


def test_aud_c_005_approved_line_without_evidence_is_rejected(raw_con):
    """An Approved line must carry an effective date and an approver."""
    with pytest.raises(sqlite3.IntegrityError):
        raw_con.execute("""INSERT INTO budget_line
            (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
             version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
             created_at, created_by)
            VALUES ('BL-NOEVID','PRJ-01','W-03','BH-PM','SUPPLEMENT',1000,'Approved',
                    1,NULL,NULL,NULL,NULL,NULL,'2026-08-06T00:00:00','U-PM')""")
    raw_con.rollback()


def test_aud_c_005_original_budget_row_cannot_be_updated(raw_con):
    original = raw_con.execute(
        "SELECT budget_line_id, amount_paise FROM budget_line WHERE kind='ORIGINAL' LIMIT 1"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        raw_con.execute("UPDATE budget_line SET amount_paise=? WHERE budget_line_id=?",
                        (original["amount_paise"] + 1, original["budget_line_id"]))
    raw_con.rollback()
    still = raw_con.execute("SELECT amount_paise FROM budget_line WHERE budget_line_id=?",
                            (original["budget_line_id"],)).fetchone()[0]
    assert still == original["amount_paise"]


def test_aud_c_005_original_budget_row_cannot_be_deleted(raw_con):
    original = raw_con.execute(
        "SELECT budget_line_id FROM budget_line WHERE kind='ORIGINAL' LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        raw_con.execute("DELETE FROM budget_line WHERE budget_line_id=?", (original,))
    raw_con.rollback()
    assert raw_con.execute("SELECT COUNT(*) FROM budget_line WHERE budget_line_id=?",
                           (original,)).fetchone()[0] == 1


def test_aud_c_005_original_budget_row_cannot_be_moved_to_another_head(raw_con):
    original = raw_con.execute(
        "SELECT budget_line_id FROM budget_line WHERE kind='ORIGINAL' AND budget_head_id='BH-PM'"
        " LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        raw_con.execute("UPDATE budget_line SET budget_head_id='BH-CIVIL' WHERE budget_line_id=?",
                        (original,))
    raw_con.rollback()


@pytest.mark.parametrize("kind,amount", [("RETURN", 5000), ("TRANSFER_OUT", 5000)])
def test_aud_c_005_a_positive_return_row_is_rejected_by_check(raw_con, kind, amount):
    """A RETURN can never increase budget - the sign is a function of the kind."""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw_con.execute("""INSERT INTO budget_line
            (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
             version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
             created_at, created_by)
            VALUES ('BL-BADSIGN','PRJ-01','W-03','BH-PM',?,?,'Draft',
                    1,NULL,NULL,NULL,NULL,NULL,'2026-08-06T00:00:00','U-PM')""", (kind, amount))
    raw_con.rollback()


@pytest.mark.parametrize("kind", ["ORIGINAL", "SUPPLEMENT", "TRANSFER_IN"])
def test_aud_c_005_an_increasing_kind_may_not_be_negative(raw_con, kind):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw_con.execute("""INSERT INTO budget_line
            (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
             version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
             created_at, created_by)
            VALUES ('BL-BADSIGN2','PRJ-01','W-03','BH-PM',?,-5000,'Draft',
                    1,NULL,NULL,NULL,NULL,NULL,'2026-08-06T00:00:00','U-PM')""", (kind,))
    raw_con.rollback()


def test_aud_c_005_original_budget_is_never_a_revision(raw_con):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw_con.execute("""INSERT INTO budget_line
            (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
             version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
             created_at, created_by)
            VALUES ('BL-ORIGREV','PRJ-01','W-03','BH-PM','ORIGINAL',5000,'Draft',
                    1,'REV-001',NULL,NULL,NULL,NULL,'2026-08-06T00:00:00','U-PM')""")
    raw_con.rollback()


# ------------------------------------------------------- revisions over the API
def test_aud_c_005_revision_creation_returns_201(requestor):
    """AUD-H-008: this endpoint used to answer 500."""
    resp = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "250000",
        "reason": "Rolling mill scope increase after design review."})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "Submitted"
    assert body["amount_paise"] == 25_000_000


def test_aud_c_005_submitted_revision_does_not_move_availability(requestor, cell):
    before = cell("W-03", "BH-PM")
    requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "250000", "reason": "pending approval"})
    after = cell("W-03", "BH-PM")
    assert after["budget"] == before["budget"]
    assert after["available"] == before["available"]


def test_aud_c_005_approved_revision_increases_budget_but_not_the_original(requestor, finance, cell):
    before = cell("W-03", "BH-PM")
    created = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "250000",
        "reason": "Rolling mill scope increase."}).json()

    approved = finance.post(f"/api/budget-revisions/{created['revision_id']}/approve", json={})
    assert approved.status_code == 200, approved.text

    after = cell("W-03", "BH-PM")
    assert after["budget"] == before["budget"] + 25_000_000
    assert after["original"] == before["original"], "the original authorisation must not move"
    assert after["revisions"] == before["revisions"] + 25_000_000
    assert after["available"] == before["available"] + 25_000_000


def test_aud_c_005_return_revision_never_increases_budget(requestor, finance, cell):
    before = cell("W-03", "BH-PM")
    created = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "RETURN", "amount_rupees": "250000",
        "reason": "Scope descoped; returning budget to the pool."}).json()
    assert created["amount_paise"] == -25_000_000

    assert finance.post(f"/api/budget-revisions/{created['revision_id']}/approve",
                        json={}).status_code == 200
    after = cell("W-03", "BH-PM")
    assert after["budget"] == before["budget"] - 25_000_000
    assert after["budget"] < before["budget"]


def test_aud_c_005_revision_requires_a_reason_and_a_known_kind(requestor):
    no_reason = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "1000", "reason": "   "})
    assert no_reason.status_code == 422
    assert code_of(no_reason) == "REASON_REQUIRED"

    bad_kind = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "GIFT", "amount_rupees": "1000", "reason": "why not"})
    assert bad_kind.status_code == 422
    assert code_of(bad_kind) == "INVALID_KIND"


def test_aud_c_005_revision_approval_is_not_repeatable(requestor, finance):
    created = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "1000", "reason": "twice?"}).json()
    rev = created["revision_id"]
    assert finance.post(f"/api/budget-revisions/{rev}/approve", json={}).status_code == 200
    again = finance.post(f"/api/budget-revisions/{rev}/approve", json={})
    assert again.status_code == 409
    assert code_of(again) == "INVALID_TRANSITION"


# ============================================== AUD-M-005 deep hierarchy robustness
@pytest.mark.slow
def test_aud_m_005_deep_wbs_ledger_does_not_raise_recursion_error(raw_con):
    """A 1,200-level hierarchy previously raised RecursionError in the rollup."""
    project_id, _deepest = build_deep_wbs(raw_con, 1200)
    led = domain.compute_ledger(raw_con, project_id)          # must not raise
    assert len(led["by_id"]) == 1200
    assert led["totals"]["budget"] == 100_000_000


@pytest.mark.slow
def test_aud_m_005_deep_wbs_budget_check_is_controlled_not_crashing(raw_con):
    """At the far end of a 1,200-level chain the check must answer, not blow up.

    The engine bounds ancestor resolution at MAX_WBS_DEPTH and then REFUSES rather
    than approving something it could not validate - a controlled failure.
    """
    project_id, deepest = build_deep_wbs(raw_con, 1200)
    result = domain.budget_check(raw_con, deepest, 1000, "BH-PM")     # must not raise
    assert result["ok"] is False
    assert result["verdict"] == "BLOCKED"
    assert result["code"] == "NO_BUDGET_FOR_HEAD"


@pytest.mark.slow
def test_aud_m_005_shallow_chain_within_the_depth_bound_still_resolves(raw_con):
    project_id, deepest = build_deep_wbs(raw_con, 40, project_id="PRJ-SHALLOW")
    result = domain.budget_check(raw_con, deepest, 1000, "BH-PM")
    assert result["ok"] is True
    assert result["budget_owner_id"] == "WD-000000"


@pytest.mark.slow
def test_aud_m_005_deep_wbs_tree_endpoint_answers_without_a_500(raw_con, requestor):
    """The API boundary must not reintroduce the recursion the ledger avoids.

    ``main.wbs_tree`` used to serialise the tree with a recursive helper, so a
    1,200-level hierarchy raised RecursionError inside the route and the caller
    received an uncontrolled HTTP 500 with no error envelope. The response must
    now either compute or be a controlled refusal carrying a code and a message.
    """
    project_id, _ = build_deep_wbs(raw_con, 1200)
    resp = requestor.get(f"/api/projects/{project_id}/wbs")
    assert resp.status_code != 500, "deep hierarchy crashed the WBS endpoint"
    assert resp.status_code in (200, 422), resp.status_code
    if resp.status_code == 422:
        body = resp.json()["detail"]
        assert body["code"] == "HIERARCHY_TOO_DEEP"
        assert body["message"].strip(), "a controlled refusal must explain itself"


def test_aud_m_005_a_normal_hierarchy_still_serialises_as_a_nested_tree(requestor):
    """The deep-hierarchy guard must not flatten or truncate ordinary projects."""
    body = requestor.get("/api/projects/PRJ-01/wbs").json()
    tree = body["tree"]
    assert len(tree) == 9, "PRJ-01 has nine level-1 elements"

    civil = next(n for n in tree if n["wbs_id"] == "W-02")
    foundation = next(n for n in civil["children"] if n["wbs_id"] == "W-02-01")
    assert {c["wbs_id"] for c in foundation["children"]} == {"W-02-01-01", "W-02-01-02"}
    assert civil["budget_owner_code"] == "CAPEX-2026-001.02"
    assert civil["total"]["budget"] == 250_000_000
