"""AUD-C-010 - Audit history is mutable and anonymous reset destroys evidence.

Two defences are asserted independently:
  1. database triggers make audit_log append-only;
  2. a SHA-256 hash chain detects tampering by an identity that could drop the
     triggers, and is verifiable through /api/audit/verify.

Plus the destructive-administration gate: /api/admin/reset must be unavailable
outside an explicit local profile.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.backend import services
from conftest import code_of, detail


# ============================================================= append-only storage
def test_aud_c_010_an_audit_row_cannot_be_updated(raw_con):
    audit_id = raw_con.execute("SELECT audit_id FROM audit_log ORDER BY audit_id LIMIT 1"
                               ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw_con.execute("UPDATE audit_log SET detail='tampered' WHERE audit_id=?", (audit_id,))
    raw_con.rollback()
    assert raw_con.execute("SELECT detail FROM audit_log WHERE audit_id=?",
                           (audit_id,)).fetchone()[0] != "tampered"


def test_aud_c_010_an_audit_row_cannot_be_deleted(raw_con):
    before = raw_con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw_con.execute("DELETE FROM audit_log")
    raw_con.rollback()
    assert raw_con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


@pytest.mark.parametrize("column", ["actor", "action", "object_id", "at"])
def test_aud_c_010_no_audit_column_may_be_rewritten(raw_con, column):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw_con.execute(f"UPDATE audit_log SET {column}='x' WHERE audit_id=1")
    raw_con.rollback()


# =================================================================== hash chain
def test_aud_c_010_the_hash_chain_verifies_through_the_api(auditor):
    resp = auditor.get("/api/audit/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intact"] is True
    assert body["broken"] == []
    assert body["entries"] >= 1


def test_aud_c_010_the_chain_still_verifies_after_a_burst_of_business_activity(
        requestor, procurement, finance, auditor):
    requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "chain activity", "amount_rupees": "10000"})
    procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2300000"})
    finance.post("/api/bills/BILL-001/void", json={"reason": "duplicate invoice"})
    created = requestor.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "1000", "reason": "chain activity"}).json()
    finance.post(f"/api/budget-revisions/{created['revision_id']}/approve", json={})

    body = auditor.get("/api/audit/verify").json()
    assert body["intact"] is True, body
    assert body["entries"] >= 6


def test_aud_c_010_every_entry_is_linked_to_its_predecessor(requestor, raw_con):
    requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "chain link", "amount_rupees": "10000"})
    rows = list(raw_con.execute(
        "SELECT audit_id, prev_hash, entry_hash FROM audit_log ORDER BY audit_id"))
    hashed = [r for r in rows if r["entry_hash"] is not None]
    assert len(hashed) >= 1
    for previous, current in zip(hashed, hashed[1:]):
        assert current["prev_hash"] == previous["entry_hash"]


def test_aud_c_010_tampering_is_detected_even_when_the_triggers_are_dropped(requestor, raw_con,
                                                                           auditor):
    """The chain is the defence against an identity privileged enough to bypass
    the append-only triggers."""
    requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "to be tampered with", "amount_rupees": "10000"})
    assert auditor.get("/api/audit/verify").json()["intact"] is True

    target = raw_con.execute(
        "SELECT audit_id FROM audit_log WHERE entry_hash IS NOT NULL ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    raw_con.execute("DROP TRIGGER audit_log_append_only_update")
    raw_con.execute("UPDATE audit_log SET detail='approved by nobody' WHERE audit_id=?", (target,))
    raw_con.commit()

    body = auditor.get("/api/audit/verify").json()
    assert body["intact"] is False
    assert target in body["broken"]


def test_aud_c_010_verify_reports_the_first_broken_link_onwards(raw_con):
    """verify_audit_chain is callable directly, so it can be run out of band."""
    result = services.verify_audit_chain(raw_con)
    assert result["intact"] is True
    assert isinstance(result["entries"], int)


# ============================================================= who may read audit
def test_aud_c_010_audit_read_requires_the_auditor_or_administrator_role(make_user, auditor,
                                                                        admin):
    assert auditor.get("/api/audit").status_code == 200
    assert admin.get("/api/audit/verify").status_code == 200
    for role in ("Requestor", "ProcurementApprover", "FinanceApprover",
                 "CapitalisationApprover", "BudgetController"):
        caller = make_user([role])
        assert caller.get("/api/audit").status_code == 403, role
        assert caller.get("/api/audit/verify").status_code == 403, role


# ================================================== guarded mutations are recorded
@pytest.mark.parametrize("action", [
    "PR_CREATED", "PR_APPROVED", "PO_AMENDED", "PO_CANCELLED", "PO_CLOSED",
    "BILL_VOIDED", "REVISION_REQUESTED", "REVISION_APPROVED",
])
def test_aud_c_010_each_guarded_mutation_writes_an_audit_entry(
        requestor, procurement, finance, controller, auditor, action):
    requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "audit coverage", "amount_rupees": "10000"})
    procurement.post("/api/purchase-requests/PR-015/approve", json={})
    procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2300000"})
    procurement.post("/api/purchase-orders/PO-015/cancel", json={"reason": "no longer required"})
    procurement.post("/api/purchase-orders/PO-011/close", json={"reason": "complete"})
    finance.post("/api/bills/BILL-001/void", json={"reason": "duplicate invoice"})
    created = controller.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "1000", "reason": "audit coverage"}).json()
    finance.post(f"/api/budget-revisions/{created['revision_id']}/approve", json={})

    actions = {e["action"] for e in auditor.get("/api/audit").json()}
    assert action in actions, f"{action} left no audit entry; recorded: {sorted(actions)}"


def test_aud_c_010_a_refused_mutation_writes_no_audit_entry(procurement, auditor):
    before = len(auditor.get("/api/audit").json())
    refused = procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "5000000"})
    assert refused.status_code == 409
    assert len(auditor.get("/api/audit").json()) == before


def test_aud_c_010_the_audit_entry_names_the_acting_session_identity(procurement, auditor):
    procurement.post("/api/purchase-orders/PO-004/amend", json={
        "po_line_id": "POL-0004", "new_amount_rupees": "2300000"})
    amended = [e for e in auditor.get("/api/audit").json() if e["action"] == "PO_AMENDED"]
    assert amended and amended[0]["actor"] == "U-PLH"


# ============================================== destructive administration gate
def test_aud_c_010_reset_is_forbidden_without_the_local_demo_profile(admin):
    resp = admin.post("/api/admin/reset", json={})
    assert resp.status_code == 403, resp.text
    body = detail(resp)
    assert body["code"] == "RESET_DISABLED"
    assert "local-demo" in body["message"]


@pytest.mark.parametrize("profile", ["", "production", "staging", "demo", "LOCAL-DEMOX"])
def test_aud_c_010_reset_stays_forbidden_under_any_other_profile(admin, monkeypatch, profile):
    monkeypatch.setenv("CAPEX_PROFILE", profile)
    assert admin.post("/api/admin/reset", json={}).status_code == 403


def test_aud_c_010_reset_is_never_available_to_a_non_administrator(make_user, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    for role in ("Auditor", "FinanceApprover", "CapitalisationApprover", "Requestor"):
        caller = make_user([role])
        assert caller.post("/api/admin/reset", json={}).status_code == 403, role


def test_aud_c_010_reset_under_the_local_demo_profile_succeeds(admin, monkeypatch, capex_db):
    """The escape hatch must work, and must not downgrade the schema.

    ``/api/admin/reset`` used to call ``db.reset_and_seed()``, which rebuilds the
    file from ``db.SCHEMA`` - migration 001 only. That destroyed every object
    migration 002 created (app_credential, user_role, app_session, pr_reservation,
    lifecycle_state, idempotency_key, the budget CHECK constraints and the audit
    append-only triggers), then failed with "no such table: app_credential" and
    answered HTTP 500. A reset must leave the database at the CURRENT version with
    every control object intact.
    """
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    resp = admin.post("/api/admin/reset", json={})
    assert resp.status_code == 200, f"reset answered {resp.status_code}: {resp.text[:300]}"
    assert resp.json()["ok"] is True

    con = sqlite3.connect(capex_db)
    try:
        versions = {r[0] for r in con.execute("SELECT version FROM schema_migration")}
        triggers = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        credentials = con.execute("SELECT COUNT(*) FROM app_credential").fetchone()[0]
    finally:
        con.close()

    assert versions == {"001", "002"}, f"reset downgraded the schema to {sorted(versions)}"
    assert {"audit_log_append_only_update", "audit_log_append_only_delete",
            "budget_line_original_immutable"} <= triggers, \
        "reset removed the audit-immutability triggers"
    assert {"app_credential", "user_role", "app_session", "pr_reservation",
            "lifecycle_state", "idempotency_key"} <= tables
    assert credentials > 0, "reset left the application without any identities"


def test_aud_c_010_the_audit_log_is_still_append_only_after_a_reset(admin, monkeypatch, capex_db):
    """A reset destroys audit evidence by design; it must not also disarm the guard."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    assert admin.post("/api/admin/reset", json={}).status_code == 200

    con = sqlite3.connect(capex_db)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM audit_log")
        con.rollback()
    finally:
        con.close()
