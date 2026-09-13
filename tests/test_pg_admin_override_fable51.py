"""The Administrator's deliberate self-approval override (Stream B, 2026-09-13).

Source-level (no database): the two catalogues carry the Administrator on
every permission, maker-checker is unchanged, `require_separation`'s four
outcomes, the override record's shape, the audit detail's shape.

Live (`@pytest.mark.pg`, SKIPPED without CAPEX_DB_URL -- a skip is not a
pass): on a purchase request, an internal material request and a budget
revision raised by the Administrator:

* no reason -> 403 SELF_APPROVAL, nothing approved;
* a reason from a non-Administrator -> 403 ADMIN_OVERRIDE_NOT_PERMITTED;
* a reason with no principal to verify the role -> 403;
* a reason too short -> 422 ADMIN_OVERRIDE_REASON_REQUIRED;
* a reason with a delegation -> 403 ADMIN_OVERRIDE_NOT_DELEGABLE;
* a sufficient reason from the Administrator -> approved, and ONE audit
  entry ADMIN_SELF_APPROVAL_OVERRIDE beside the ordinary approval entry,
  whose JSON detail names actor, object, previous and new state, reason,
  correlation id, timestamp and amount;
* the same reason from an Administrator who is NOT the maker approves
  normally and records no override.
"""
from __future__ import annotations

import json
import os
import sys as _sys
import uuid
from datetime import date
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)
from test_pg_reservations import _line, _seed  # noqa: E402

import pytest  # noqa: E402

from app.backend import auth as auth_mod  # noqa: E402
from app.backend.pg import admin_override  # noqa: E402
from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import internal_fulfilment as imr_svc  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402
from app.backend.pg import roles as pg_roles  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS."),
)

ADMIN = "U-ADMIN"
ADMIN_PRINCIPAL = {"user_id": ADMIN, "roles": ["Administrator", "Requestor"]}
OTHER_ADMIN = {"user_id": "U-ADMIN-2", "roles": ["Administrator"]}
NON_ADMIN = {"user_id": ADMIN, "roles": ["ProcurementApprover"]}
REASON = "Sole approver on site during the plant shutdown; owner informed."
ACTION = "ADMIN_SELF_APPROVAL_OVERRIDE"


# =========================================================================
# Source-level
# =========================================================================
def test_both_catalogues_carry_the_administrator_on_every_permission():
    for permission, roles in auth_mod.PERMISSIONS.items():
        assert "Administrator" in roles, permission
    for permission, roles in pg_roles.PERMISSIONS.items():
        assert "System Administrator" in roles, permission
    assert {"pr.approve", "imr.approve", "revision.approve"} <= auth_mod.MAKER_CHECKER
    assert {"pr.approve", "imr.approve", "revision.approve"} <= pg_roles.MAKER_CHECKER


def test_require_separation_has_four_outcomes_and_the_record_shape():
    silent = pytest.raises(auth_mod.AuthError)
    with silent as exc:
        auth_mod.require_separation(ADMIN_PRINCIPAL, "pr.approve", ADMIN, object_label="PR-1")
    assert exc.value.code == "SELF_APPROVAL" and "admin_override_reason" in exc.value.message
    with pytest.raises(auth_mod.AuthError) as short:
        auth_mod.require_separation(ADMIN_PRINCIPAL, "pr.approve", ADMIN, object_label="PR-1",
                                    admin_override_reason="because")
    assert (short.value.status, short.value.code) == (422, "ADMIN_OVERRIDE_REASON_REQUIRED")
    with pytest.raises(auth_mod.AuthError) as other:
        auth_mod.require_separation(NON_ADMIN, "pr.approve", ADMIN, object_label="PR-1",
                                    admin_override_reason=REASON)
    assert (other.value.status, other.value.code) == (403, "ADMIN_OVERRIDE_NOT_PERMITTED")
    record = auth_mod.require_separation(ADMIN_PRINCIPAL, "pr.approve", ADMIN,
                                         object_label="PR-1", admin_override_reason=REASON)
    assert record == {"action": ACTION, "actor_user_id": ADMIN, "maker_user_id": ADMIN,
                      "permission": "pr.approve", "object_label": "PR-1", "reason": REASON}
    # No separation issue: a reason is unused, nothing is returned.
    assert auth_mod.require_separation(OTHER_ADMIN, "pr.approve", ADMIN, object_label="PR-1",
                                       admin_override_reason=REASON) is None
    # Not a maker-checker permission: never a record.
    assert auth_mod.require_separation(ADMIN_PRINCIPAL, "budget.read", ADMIN,
                                       admin_override_reason=REASON) is None


def test_the_override_record_is_validated_never_trusted_and_never_delegated():
    with pytest.raises(auth_mod.AuthError) as forged:
        admin_override.validate({"action": ACTION, "reason": "x"})
    assert forged.value.code == "ADMIN_OVERRIDE_INVALID"
    with pytest.raises(auth_mod.AuthError) as no_principal:
        admin_override.resolve(principal=None, permission="pr.approve", maker_user_id=ADMIN,
                               object_label="PR-1", admin_override_reason=REASON)
    assert no_principal.value.code == "ADMIN_OVERRIDE_NOT_PERMITTED"
    with pytest.raises(auth_mod.AuthError) as delegated:
        admin_override.resolve(principal=ADMIN_PRINCIPAL, permission="pr.approve",
                               maker_user_id=ADMIN, object_label="PR-1",
                               admin_override_reason=REASON, acting_for_user_id="U-PM")
    assert delegated.value.code == "ADMIN_OVERRIDE_NOT_DELEGABLE"
    record = admin_override.resolve(principal=ADMIN_PRINCIPAL, permission="pr.approve",
                                    maker_user_id=ADMIN, object_label="PR-1",
                                    admin_override_reason=REASON)
    assert admin_override.contributors_for_check(record, ADMIN, {ADMIN, "U-X"}) == {"U-X"}
    assert admin_override.contributors_for_check(None, ADMIN, {ADMIN, "U-X"}) == {ADMIN, "U-X"}
    detail = json.loads(admin_override.build_detail(
        override=record, object_type="PurchaseRequest", object_id="PR-1",
        previous_state="Submitted", new_state="Approved", amount_paise=123_45,
        correlation_id="corr-1"))
    assert detail["override"] == ACTION and detail["amount_paise"] == 123_45
    assert {"actor_user_id", "maker_user_id", "permission", "object_type", "object_id",
            "previous_state", "new_state", "reason", "correlation_id", "timestamp",
            "amount_paise"} <= set(detail)


# =========================================================================
# Live
# =========================================================================
def _system(database):
    return database.session(Scope.system())


def _audit(connection, object_type, object_id):
    return connection.execute(
        "SELECT action, actor, detail, correlation_id FROM audit_log "
        "WHERE object_type = %s AND object_id = %s ORDER BY audit_id",
        (object_type, object_id)).fetchall()


def _submitted_pr(database, ids, *, by=ADMIN) -> str:
    with _system(database) as session:
        pr_id = svc.create_pr(session, project_id=ids["project"], actor=by,
                              lines=[_line(ids, "child_a1", 500_00)])["pr_id"]
    with _system(database) as session:
        svc.submit_pr(session, pr_id=pr_id, actor=by)
    return pr_id


def _approve(database, pr_id, **kw):
    with _system(database) as session:
        return svc.approve_pr(session, pr_id=pr_id, actor=ADMIN, correlation_id="corr-pr", **kw)


@pytest.mark.pg
@PG
def test_a_purchase_request_refuses_every_wrong_override_and_accepts_the_right_one(
        pg_database, pg_connection):
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    pr_id = _submitted_pr(pg_database, ids)

    for kwargs, code in (
            ({"principal": ADMIN_PRINCIPAL}, "SELF_APPROVAL"),
            ({}, "SELF_APPROVAL"),
            ({"principal": NON_ADMIN, "admin_override_reason": REASON}, "ADMIN_OVERRIDE_NOT_PERMITTED"),
            ({"admin_override_reason": REASON}, "ADMIN_OVERRIDE_NOT_PERMITTED"),
            ({"principal": ADMIN_PRINCIPAL, "admin_override_reason": "short"}, "ADMIN_OVERRIDE_REASON_REQUIRED"),
            ({"principal": ADMIN_PRINCIPAL, "admin_override_reason": REASON,
              "acting_for_user_id": "U-PM"}, "ADMIN_OVERRIDE_NOT_DELEGABLE"),
    ):
        with pytest.raises(svc.ProcurementError) as refused:
            _approve(pg_database, pr_id, **kwargs)
        assert refused.value.code == code, kwargs
    assert pg_connection.execute(
        "SELECT status FROM purchase_request WHERE pr_id = %s", (pr_id,)).fetchone()[0] == "Submitted"
    assert [a[0] for a in _audit(pg_connection, "PurchaseRequest", pr_id)
            if a[0] == ACTION] == []

    out = _approve(pg_database, pr_id, principal=ADMIN_PRINCIPAL, admin_override_reason=REASON)
    assert out["status"] == "Approved" and out["approver"] == ADMIN
    entries = _audit(pg_connection, "PurchaseRequest", pr_id)
    overrides = [e for e in entries if e[0] == ACTION]
    assert len(overrides) == 1 and any(e[0] == "PR_APPROVED" for e in entries)
    action, actor, detail, correlation = overrides[0]
    body = json.loads(detail)
    assert actor == ADMIN and correlation == "corr-pr"
    assert body["actor_user_id"] == body["maker_user_id"] == ADMIN
    assert (body["object_type"], body["object_id"]) == ("PurchaseRequest", pr_id)
    assert (body["previous_state"], body["new_state"]) == ("Submitted", "Approved")
    assert body["reason"] == REASON and body["amount_paise"] == 500_00
    assert body["permission"] == "pr.approve" and body["timestamp"]


@pytest.mark.pg
@PG
def test_an_administrator_who_is_not_the_maker_approves_normally_with_no_override(
        pg_database, pg_connection):
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    pr_id = _submitted_pr(pg_database, ids, by="U-REQ")
    out = _approve(pg_database, pr_id, principal=ADMIN_PRINCIPAL, admin_override_reason=REASON)
    assert out["status"] == "Approved"
    actions = [a[0] for a in _audit(pg_connection, "PurchaseRequest", pr_id)]
    assert "PR_APPROVED" in actions and ACTION not in actions


@pytest.mark.pg
@PG
def test_an_internal_material_request_takes_the_same_gate(pg_database, pg_connection):
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    pr_id = _submitted_pr(pg_database, ids, by="U-REQ")
    with _system(pg_database) as session:
        svc.approve_pr(session, pr_id=pr_id, actor="U-APPROVER")
    [line_id] = [r[0] for r in pg_connection.execute(
        "SELECT pr_line_id FROM pr_line WHERE pr_id = %s", (pr_id,)).fetchall()]
    with _system(pg_database) as session:
        imr = imr_svc.decide_fulfilment(session, pr_line_id=line_id, mode="INTERNAL_TRANSFER",
                                        actor=ADMIN, unit_rate_paise=100_00,
                                        valuation_note="stores rate")["imr"]
    with pytest.raises(svc.ProcurementError) as silent:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr["imr_id"], actor=ADMIN,
                                    principal=ADMIN_PRINCIPAL)
    assert silent.value.code == "SELF_APPROVAL"
    with pytest.raises(svc.ProcurementError) as other:
        with _system(pg_database) as session:
            imr_svc.approve_request(session, imr_id=imr["imr_id"], actor=ADMIN,
                                    principal=NON_ADMIN, admin_override_reason=REASON)
    assert other.value.code == "ADMIN_OVERRIDE_NOT_PERMITTED"
    with _system(pg_database) as session:
        out = imr_svc.approve_request(session, imr_id=imr["imr_id"], actor=ADMIN,
                                      principal=ADMIN_PRINCIPAL, admin_override_reason=REASON,
                                      correlation_id="corr-imr")
    assert out["status"] == "APPROVED"
    overrides = [e for e in _audit(pg_connection, "InternalMaterialRequest", imr["imr_id"])
                 if e[0] == ACTION]
    assert len(overrides) == 1
    body = json.loads(overrides[0][2])
    assert (body["previous_state"], body["new_state"]) == ("REQUESTED", "APPROVED")
    assert body["amount_paise"] == 100_00 and body["correlation_id"] == "corr-imr"


@pytest.mark.pg
@PG
def test_a_budget_revision_takes_the_same_gate_through_the_service(pg_database, pg_connection):
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    with _system(pg_database) as session:
        revision_id = budget_svc.create_revision(
            session, wbs_id=ids["root_a"], budget_head_id=ids["head"], delta_paise=250_00,
            effective_from=date(2026, 9, 1), justification="scope growth", actor=ADMIN)["revision_id"]
    with pytest.raises(budget_svc.BudgetServiceError) as silent:
        with _system(pg_database) as session:
            budget_svc.approve_revision(session, revision_id=revision_id, actor=ADMIN,
                                        principal=ADMIN_PRINCIPAL)
    assert silent.value.code == "SELF_APPROVAL_FORBIDDEN"
    with pytest.raises(budget_svc.BudgetServiceError) as other:
        with _system(pg_database) as session:
            budget_svc.approve_revision(session, revision_id=revision_id, actor=ADMIN,
                                        principal=NON_ADMIN, admin_override_reason=REASON)
    assert other.value.code == "ADMIN_OVERRIDE_NOT_PERMITTED"
    with pytest.raises(budget_svc.BudgetServiceError) as forged:
        with _system(pg_database) as session:
            budget_svc.approve_revision(session, revision_id=revision_id, actor=ADMIN,
                                        admin_override={"action": ACTION, "reason": REASON})
    assert forged.value.code == "ADMIN_OVERRIDE_INVALID"
    # A WELL-FORMED record produced for somebody else's object is not an
    # override of this one (adversarial review 2026-09-13, P1).
    other_record = auth_mod.require_separation(OTHER_ADMIN, "revision.approve", "U-ADMIN-2",
                                               object_label="revision X", admin_override_reason=REASON)
    with pytest.raises(budget_svc.BudgetServiceError) as borrowed:
        with _system(pg_database) as session:
            budget_svc.approve_revision(session, revision_id=revision_id, actor=ADMIN,
                                        admin_override=other_record)
    assert borrowed.value.code == "ADMIN_OVERRIDE_INVALID"
    with _system(pg_database) as session:
        out = budget_svc.approve_revision(session, revision_id=revision_id, actor=ADMIN,
                                          principal=ADMIN_PRINCIPAL,
                                          admin_override_reason=REASON,
                                          correlation_id="corr-rev")
    assert out["status"] == "APPROVED"
    overrides = [e for e in _audit(pg_connection, budget_svc.OBJECT_TYPE_REVISION, revision_id)
                 if e[0] == ACTION]
    assert len(overrides) == 1
    body = json.loads(overrides[0][2])
    assert body["amount_paise"] == 250_00 and body["new_state"] == "APPROVED"
