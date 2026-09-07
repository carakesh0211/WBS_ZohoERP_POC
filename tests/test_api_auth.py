"""AUD-C-006 - No role authorization or maker-checker enforcement.

The audited build accepted an arbitrary bearer token and let the request body
nominate the acting user, so any caller could approve their own request. These
tests assert the corrected contract:

  * every mutating route refuses an unauthenticated caller with 401;
  * every mutating route refuses a role that lacks the permission with 403;
  * the acting user is derived from the server-side session and can never be
    supplied by the caller;
  * no approval route permits self-approval (PR, budget revision, capitalisation).

The route table below is checked against the live FastAPI route list, so a new
mutating endpoint cannot be added without also being authorised here.
"""
from __future__ import annotations

import pytest

from app.backend import auth, main
from conftest import code_of, detail

# ---------------------------------------------------------------- route matrix
# Each entry: the concrete request to make, the permission the route enforces, and
# a role that does NOT hold that permission. A synthetic user is created holding
# exactly that role, so the assertion does not depend on the demo dataset happening
# to contain a suitably narrow account.
MUTATING_ROUTES = [
    # (template, method, url, json body, permission, denied_role)
    ("/api/auth/logout", "POST", "/api/auth/logout", {}, None, None),
    ("/api/budget-check", "POST", "/api/budget-check",
     {"wbs_id": "W-03-01", "amount_rupees": "1000", "budget_head_id": "BH-PM"},
     "budget.check", "CapitalisationApprover"),
    ("/api/purchase-requests", "POST", "/api/purchase-requests",
     {"project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
      "description": "unauthorised attempt", "amount_rupees": "1000"},
     "pr.create", "ProcurementApprover"),
    ("/api/purchase-requests/{pr_id}/approve", "POST", "/api/purchase-requests/PR-015/approve",
     {"reason": "unauthorised attempt"}, "pr.approve", "Requestor"),
    ("/api/purchase-requests/convert", "POST", "/api/purchase-requests/convert",
     {"pr_id": "PR-015", "po_id": "PO-004"}, "po.amend", "Requestor"),
    ("/api/purchase-orders/{po_id}/amend", "POST", "/api/purchase-orders/PO-004/amend",
     {"po_line_id": "POL-0004", "new_amount_rupees": "2200000"}, "po.amend", "Requestor"),
    ("/api/purchase-orders/{po_id}/cancel", "POST", "/api/purchase-orders/PO-004/cancel",
     {"reason": "unauthorised attempt"}, "po.cancel", "Requestor"),
    ("/api/purchase-orders/{po_id}/close", "POST", "/api/purchase-orders/PO-004/close",
     {"reason": "unauthorised attempt"}, "po.close", "Requestor"),
    ("/api/bills/{bill_id}/void", "POST", "/api/bills/BILL-001/void",
     {"reason": "unauthorised attempt"}, "bill.void", "Requestor"),
    ("/api/budget-revisions", "POST", "/api/budget-revisions",
     {"project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
      "kind": "SUPPLEMENT", "amount_rupees": "1000", "reason": "unauthorised attempt"},
     "revision.create", "ProcurementApprover"),
    ("/api/budget-revisions/{rev_id}/approve", "POST", "/api/budget-revisions/REV-003/approve",
     {}, "revision.approve", "Requestor"),
    ("/api/capitalisation/{cap_id}/allocate", "POST", "/api/capitalisation/CAP-001/allocate",
     {"wbs_id": "W-01", "asset_name": "Site", "amount_rupees": "1000"},
     "capitalisation.allocate", "Requestor"),
    ("/api/capitalisation/{cap_id}/approve", "POST", "/api/capitalisation/CAP-001/approve",
     {}, "capitalisation.approve", "Requestor"),
    ("/api/zoho/{connection_id}/authorise", "POST", "/api/zoho/CONN-01/authorise",
     {}, "connector.manage", "Auditor"),
    ("/api/zoho/{connection_id}/refresh", "POST", "/api/zoho/CONN-01/refresh",
     {}, "connector.manage", "Auditor"),
    ("/api/zoho/{connection_id}/test", "POST", "/api/zoho/CONN-01/test",
     {}, "connector.manage", "Auditor"),
    ("/api/zoho/{connection_id}/sync/{module}", "POST", "/api/zoho/CONN-01/sync/bills",
     {}, "connector.manage", "Auditor"),
    ("/api/admin/reset", "POST", "/api/admin/reset", {}, "admin.reset", "Auditor"),

    # ---------------------------------------------------------- Wave 2
    # The PostgreSQL budget, masters, settings and access routers. Each names
    # the permission its route requires and a role that holds the router's
    # read floor -- so it authenticates and gets past the floor -- but not the
    # route's own permission, so only a 403 can pass.
    ("/api/budget/periods/{period_id}/transition", "POST",
     "/api/budget/periods/AP-2026-07/transition", {"to_state": "OPEN"},
     "period.transition", "Requestor"),
    ("/api/budget/revisions", "POST", "/api/budget/revisions",
     {"wbs_id": "WBS-A-CIVIL", "budget_head_id": "BH-DM1-CIVIL",
      "delta_paise": 100000, "effective_from": "2026-09-01",
      "justification": "unauthorised attempt"},
     "revision.create", "ProcurementApprover"),
    # Wave 4 stream A2: raising a document into the approval workflow is a
    # mutation and carries the maker's own permission, not the approver's.
    ("/api/budget/revisions/{revision_id}/submit", "POST",
     "/api/budget/revisions/REV-DM1-ELEC-INC/submit", {},
     "revision.create", "ProcurementApprover"),
    ("/api/budget/revisions/{revision_id}/approve", "POST",
     "/api/budget/revisions/REV-DM1-ELEC-INC/approve", {},
     "revision.approve", "Requestor"),
    ("/api/budget/revisions/{revision_id}/reject", "POST",
     "/api/budget/revisions/REV-DM1-ELEC-INC/reject",
     {"reason": "unauthorised attempt"}, "revision.approve", "Requestor"),
    ("/api/budget/transfers", "POST", "/api/budget/transfers",
     {"from_wbs_id": "WBS-B-PM-MOD", "from_head_id": "BH-DM2-PM",
      "to_wbs_id": "WBS-B-CIVIL", "to_head_id": "BH-DM2-CIVIL",
      "amount_paise": 100000, "effective_from": "2026-09-01",
      "justification": "unauthorised attempt"},
     "revision.create", "ProcurementApprover"),
    ("/api/budget/transfers/{transfer_id}/submit", "POST",
     "/api/budget/transfers/TRF-DM2-PMMOD-TO-CIVIL/submit", {},
     "revision.create", "ProcurementApprover"),
    ("/api/budget/transfers/{transfer_id}/approve", "POST",
     "/api/budget/transfers/TRF-DM2-PMMOD-TO-CIVIL/approve", {},
     "revision.approve", "Requestor"),
    ("/api/budget/transfers/{transfer_id}/reject", "POST",
     "/api/budget/transfers/TRF-DM2-PMMOD-TO-CIVIL/reject",
     {"reason": "unauthorised attempt"}, "revision.approve", "Requestor"),

    ("/api/masters/{kind_name}", "POST", "/api/masters/items",
     {"code": "IT-X", "name": "unauthorised attempt"},
     "masters.write", "Requestor"),
    ("/api/masters/{kind_name}/{item_id}", "PUT", "/api/masters/items/IT-DM-001",
     {"name": "unauthorised attempt", "version_no": 1},
     "masters.write", "Requestor"),
    ("/api/masters/{kind_name}/{item_id}/deactivate", "POST",
     "/api/masters/items/IT-DM-001/deactivate", {},
     "masters.write", "Requestor"),

    ("/api/settings/{collection}", "POST", "/api/settings/entities",
     {"code": "ENT-X", "name": "unauthorised attempt"},
     "settings.write", "Requestor"),
    ("/api/settings/{collection}/{item_id}", "PUT",
     "/api/settings/entities/ENT-DM-01",
     {"name": "unauthorised attempt", "version_no": 1},
     "settings.write", "Requestor"),
    ("/api/settings/{collection}/{item_id}/deactivate", "POST",
     "/api/settings/entities/ENT-DM-01/deactivate", {},
     "settings.write", "Requestor"),

    ("/api/admin/users/{user_id}/grants", "PUT", "/api/admin/users/U-PM/grants",
     {"roles": ["Administrator"], "scopes": {"read_all": True}},
     "admin.reset", "Auditor"),

    # ---------------------------------------------------------- Wave 4 (M4b)
    # The approval engine. Each names the permission its route requires and a
    # role that holds the router's `approval.read` floor -- so it
    # authenticates and gets past the floor -- but not the route's own
    # permission, so only a 403 can pass.
    #
    # Auditor is NOT usable as the denied role here: it is the one role
    # excluded from `approval.read`, so it would be refused at the floor and
    # the case would prove nothing about the route's own permission.
    ("/api/approvals/{instance_id}/decide", "POST",
     "/api/approvals/AI-DEMO-001/decide",
     {"action": "APPROVE", "idempotency_key": "k-1", "object_version": 1},
     "approval.act", "Administrator"),
    ("/api/approvals/{instance_id}/recall", "POST",
     "/api/approvals/AI-DEMO-001/recall", {"reason_text": "unauthorised attempt"},
     "approval.act", "Administrator"),
    ("/api/approvals/{instance_id}/cancel", "POST",
     "/api/approvals/AI-DEMO-001/cancel", {"reason_text": "unauthorised attempt"},
     "approval.act", "Administrator"),
    ("/api/approvals/{instance_id}/resubmit", "POST",
     "/api/approvals/AI-DEMO-001/resubmit", {"reason_text": "unauthorised attempt"},
     "approval.act", "Administrator"),

    ("/api/approvals/definitions", "POST", "/api/approvals/definitions",
     {"object_type": "budget_revision", "code": "X", "stages": []},
     "approval.configure", "Requestor"),
    ("/api/approvals/definitions/{definition_id}/activate", "POST",
     "/api/approvals/definitions/AD-1/activate", {},
     "approval.configure", "Requestor"),
    ("/api/approvals/definitions/{definition_id}/simulate", "POST",
     "/api/approvals/definitions/AD-1/simulate", {"object": {}},
     "approval.configure", "Requestor"),

    ("/api/approvals/delegations", "POST", "/api/approvals/delegations",
     {"delegate_user_id": "U-PM", "scope_key": "*", "from": "2026-09-01",
      "to": "2026-09-30"},
     "approval.delegate", "Requestor"),
    ("/api/approvals/delegations/{delegation_id}/revoke", "POST",
     "/api/approvals/delegations/AD-DEL-1/revoke",
     {"reason_text": "unauthorised attempt"},
     "approval.delegate", "Requestor"),

    # ---------------------------------------------------------- Wave 5
    # `/api/integrations/*`. Every mutating route on that router carries
    # `connector.manage`, and the denied role for all five is Auditor: it
    # holds `connector.read`, so it authenticates and clears the ROUTER's
    # floor, and stops at the route's own permission. A role that failed the
    # floor would produce a 403 from the wrong check and the row would prove
    # nothing about the route.
    #
    # Registered in the same commit that mounts the router in `main.py`. This
    # matrix is built from the live OpenAPI schema, so a mounted route with no
    # row here fails
    # `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
    # on the very next run.
    ("/api/integrations/connections", "POST", "/api/integrations/connections",
     {"entity_id": "ENT-01", "product": "ERP", "data_centre": "in",
      "organization_id": "60000000000"},
     "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/authorize", "POST",
     "/api/integrations/connections/CONN-01/authorize", {},
     "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/organization", "PUT",
     "/api/integrations/connections/CONN-01/organization",
     {"organization_id": "60000000001"},
     "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/validate", "POST",
     "/api/integrations/connections/CONN-01/validate", {},
     "connector.manage", "Auditor"),
    ("/api/integrations/dead-letters/{queue}/{row_id}/retry", "POST",
     "/api/integrations/dead-letters/outbox/OUT-1/retry", {},
     "connector.manage", "Auditor"),
    # The triage attribution. `reconciliation.triage` is Administrator-only
    # (Auditor is excluded deliberately: `test_aud_c_006_auditor_is_read_only`
    # pins Auditor to an allow-list, and widening an audit-finding assertion
    # so a new feature reads tidily is not a thing to do in passing).
    ("/api/integrations/exceptions/{exception_id}/attribute", "POST",
     "/api/integrations/exceptions/RX-1/attribute",
     {"entity_id": "ENT-01", "reason": "unauthorised attempt"},
     "reconciliation.triage", "Auditor"),
    ("/api/integrations/exceptions/{exception_id}/resolve", "POST",
     "/api/integrations/exceptions/RX-1/resolve",
     {"status": "Resolved", "note": "unauthorised attempt"},
     "reconciliation.triage", "Auditor"),

    # ---------------------------------------------------------- Wave 6
    # `/api/procurement/*` -- the PostgreSQL PR -> PO chain over migration
    # 013's tables. The router floor is `budget.read`, which every role holds,
    # so every denied role below authenticates, clears the floor, and stops at
    # the ROUTE's own permission. A denied role that failed the floor would
    # produce a 403 from the wrong check and the row would prove nothing.
    #
    # Registered in the same commit that mounts the router in `main.py`, for
    # the reason the Wave 5 block above states.
    ("/api/procurement/purchase-requests", "POST",
     "/api/procurement/purchase-requests",
     {"project_id": "PRJ-01",
      "lines": [{"wbs_id": "W-03-01", "budget_head_id": "BH-PM",
                 "amount_paise": 100000}]},
     "pr.create", "ProcurementApprover"),
    ("/api/procurement/purchase-requests/{pr_id}/submit", "POST",
     "/api/procurement/purchase-requests/PR-015/submit", {},
     "pr.create", "ProcurementApprover"),
    # The floor here is "holds pr.approve OR pr.approve_exception", because the
    # two are held by different roles and the route serves both -- which one is
    # actually required is decided from the request's own check_result inside
    # the transaction. Requestor holds NEITHER, so only a 403 can pass, and the
    # matrix's own `denied_role not in PERMISSIONS[permission]` assertion still
    # means what it says.
    ("/api/procurement/purchase-requests/{pr_id}/approve", "POST",
     "/api/procurement/purchase-requests/PR-015/approve",
     {"reason": "unauthorised attempt"}, "pr.approve", "Requestor"),
    ("/api/procurement/purchase-requests/{pr_id}/convert", "POST",
     "/api/procurement/purchase-requests/PR-015/convert",
     {"vendor_name": "unauthorised attempt"}, "po.amend", "Requestor"),
    ("/api/procurement/purchase-orders", "POST",
     "/api/procurement/purchase-orders",
     {"project_id": "PRJ-01", "vendor_name": "unauthorised attempt",
      "lines": [{"wbs_id": "W-03-01", "budget_head_id": "BH-PM",
                 "amount_paise": 100000}]},
     "po.amend", "Requestor"),
    # Emission is a connector operation: it writes `integration_outbox` rows
    # against a tenant connection and commits no budget. Auditor holds
    # `connector.read`, so it clears the floor and stops here.
    ("/api/procurement/purchase-orders/{po_id}/emit", "POST",
     "/api/procurement/purchase-orders/PO-004/emit",
     {"connection_id": "CONN-01", "vendor_external_id": "ZV-77",
      "document_date": "2026-09-07"},
     "connector.manage", "Auditor"),
]

PUBLIC_MUTATING_ROUTES = {"/api/auth/login"}

READ_ROUTES = [
    "/api/auth/me", "/api/bootstrap", "/api/dashboard", "/api/projects/PRJ-01/wbs",
    "/api/projects/PRJ-01/budget-grid", "/api/purchase-requests", "/api/purchase-orders",
    "/api/grns", "/api/bills", "/api/reconciliation", "/api/budget-revisions",
    "/api/capitalisation", "/api/audit", "/api/audit/verify",
    "/api/reconciliation/exceptions", "/api/zoho/connections", "/api/zoho/inventory",
    "/api/zoho/scopes", "/api/zoho/CONN-01/health",
]


def _ids(entries):
    return [f"{e[1]} {e[0]}" for e in entries]


# ========================================================= completeness of the matrix
def _mutating_paths(app) -> set[str]:
    """Every mutating path the application actually serves, from its OpenAPI
    schema rather than from `app.routes`.

    This walked the route table, descending into router containers, because a
    newer FastAPI puts an `_IncludedRouter` object in `app.routes` that has no
    `.path`. Descending fixed the crash but not the underlying problem: the
    structure of `app.routes` is an internal detail that keeps changing. On
    FastAPI 0.133 (local) all fourteen Wave 2 routes appear directly; on
    0.141.1 (CI) they do not, so this assertion reported every one of them as
    a STALE entry -- reading "these routes were removed" when they were
    mounted and serving perfectly well. One version of this suite passed
    locally while claiming in CI that the entire budget, masters, settings and
    access API had disappeared.

    The OpenAPI schema is the application's own published description of what
    it serves. It is a supported, stable interface, and it cannot disagree
    with the router structure because it is generated from it.
    """
    schema = app.openapi()
    return {
        path
        for path, operations in schema.get("paths", {}).items()
        if path.startswith("/api/")
        for method in operations
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE")
    }


def test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix():
    """A new mutating endpoint must be added to MUTATING_ROUTES to pass this."""
    live = _mutating_paths(main.app)
    covered = {entry[0] for entry in MUTATING_ROUTES} | PUBLIC_MUTATING_ROUTES
    assert live == covered, (
        f"uncovered mutating routes: {sorted(live - covered)}; "
        f"stale entries: {sorted(covered - live)}")


def test_aud_c_006_public_paths_are_only_health_and_login():
    assert main.PUBLIC_PATHS == {"/api/health", "/api/auth/login"}


# =============================================================== authentication
@pytest.mark.parametrize("entry", MUTATING_ROUTES, ids=_ids(MUTATING_ROUTES))
def test_aud_c_006_mutating_route_rejects_an_unauthenticated_caller(client, entry):
    _template, method, url, body, _perm, _denied = entry
    resp = client.request(method, url, json=body)
    assert resp.status_code == 401, f"{method} {url} -> {resp.status_code}"
    assert detail(resp)["code"] == "NOT_AUTHENTICATED"


@pytest.mark.parametrize("url", READ_ROUTES)
def test_aud_c_006_read_route_rejects_an_unauthenticated_caller(client, url):
    resp = client.get(url)
    assert resp.status_code == 401, f"GET {url} -> {resp.status_code}"


def test_aud_c_006_health_is_reachable_without_a_session(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_aud_c_006_an_arbitrary_bearer_token_is_not_an_identity(client):
    """The audited build accepted any bearer token at all."""
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer let-me-in"})
    assert resp.status_code == 401
    assert detail(resp)["code"] == "SESSION_INVALID"


def test_aud_c_006_an_unknown_session_header_is_rejected(client):
    resp = client.get("/api/auth/me", headers={"X-Session": "not-a-session"})
    assert resp.status_code == 401
    assert detail(resp)["code"] == "SESSION_INVALID"


def test_aud_c_006_a_revoked_session_is_rejected(requestor):
    assert requestor.get("/api/auth/me").status_code == 200
    assert requestor.post("/api/auth/logout").status_code == 200
    after = requestor.get("/api/auth/me")
    assert after.status_code == 401
    assert detail(after)["code"] == "SESSION_INVALID"


def test_aud_c_006_an_expired_session_is_rejected(requestor, raw_con):
    raw_con.execute("UPDATE app_session SET expires_at='2000-01-01T00:00:00' WHERE session_id=?",
                    (requestor.session_id,))
    raw_con.commit()
    resp = requestor.get("/api/auth/me")
    assert resp.status_code == 401
    assert detail(resp)["code"] == "SESSION_EXPIRED"


def test_aud_c_006_bad_password_is_refused(client):
    resp = client.post("/api/auth/login", json={"user_id": "U-CFO", "password": "wrong"})
    assert resp.status_code == 401


def test_aud_c_006_login_does_not_disclose_which_accounts_exist(client):
    unknown = client.post("/api/auth/login", json={"user_id": "U-NOBODY", "password": "x"})
    wrong = client.post("/api/auth/login", json={"user_id": "U-CFO", "password": "x"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_aud_c_006_a_disabled_account_cannot_log_in(client, raw_con):
    raw_con.execute("UPDATE app_credential SET disabled=1 WHERE user_id='U-CFO'")
    raw_con.commit()
    assert client.post("/api/auth/login",
                       json={"user_id": "U-CFO", "password": "U-CFO!demo"}).status_code == 401


# ================================================================ authorisation
@pytest.mark.parametrize(
    "entry", [e for e in MUTATING_ROUTES if e[4]],
    ids=_ids([e for e in MUTATING_ROUTES if e[4]]))
def test_aud_c_006_mutating_route_rejects_a_role_without_the_permission(make_user, entry):
    _template, method, url, body, permission, denied_role = entry
    assert denied_role not in auth.PERMISSIONS[permission], \
        f"{denied_role} actually holds {permission}; the matrix is wrong"
    caller = make_user([denied_role])
    resp = caller.request(method, url, json=body)
    assert resp.status_code == 403, f"{method} {url} as {denied_role} -> {resp.status_code}: {resp.text}"
    assert code_of(resp) == "FORBIDDEN"


def test_aud_c_006_a_caller_with_no_role_at_all_can_mutate_nothing(make_user):
    caller = make_user([])
    for _template, method, url, body, permission, _denied in MUTATING_ROUTES:
        if permission is None:
            continue
        resp = caller.request(method, url, json=body)
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"


def test_aud_c_006_every_permission_maps_to_known_roles():
    for permission, roles in auth.PERMISSIONS.items():
        assert roles, f"{permission} grants nothing"
        for role in roles:
            assert role in auth.ROLES, f"{permission} references unknown role {role}"


def test_aud_c_006_maker_checker_covers_every_approval_permission():
    approvals = {p for p in auth.PERMISSIONS if p.endswith(".approve")
                 or p.endswith(".approve_exception") or p == "bill.void"}
    assert approvals <= auth.MAKER_CHECKER, \
        f"approval permissions outside maker-checker: {sorted(approvals - auth.MAKER_CHECKER)}"


def test_aud_c_006_administrator_holds_no_financial_approval():
    """Administration must not be a route to approving spend."""
    financial = ("pr.approve", "pr.approve_exception", "revision.approve",
                 "capitalisation.approve", "capitalisation.allocate", "bill.void",
                 "po.amend", "po.cancel", "po.close", "pr.create", "revision.create")
    for permission in financial:
        assert "Administrator" not in auth.PERMISSIONS[permission], permission


def test_aud_c_006_auditor_is_read_only():
    for permission, roles in auth.PERMISSIONS.items():
        if "Auditor" in roles:
            assert permission in ("budget.read", "budget.check", "audit.read", "connector.read"), \
                f"Auditor holds mutating permission {permission}"


def test_aud_c_006_bootstrap_reports_only_the_callers_own_permissions(requestor, capitalisation):
    req_perms = set(requestor.get("/api/bootstrap").json()["permissions"])
    cfo_perms = set(capitalisation.get("/api/bootstrap").json()["permissions"])
    assert "pr.create" in req_perms and "pr.create" not in cfo_perms
    assert "capitalisation.approve" in cfo_perms and "capitalisation.approve" not in req_perms


# ============================================ identity cannot be chosen by the caller
def test_aud_c_006_request_body_cannot_nominate_the_acting_user(requestor, raw_con):
    resp = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "impersonation attempt", "amount_rupees": "1000",
        # all of these are attempts to name somebody else as the actor
        "actor": "U-CFO", "user_id": "U-CFO", "requested_by": "U-CFO", "approver": "U-CFO"})
    assert resp.status_code == 201, resp.text
    stored = raw_con.execute("SELECT requested_by, approver FROM purchase_request WHERE pr_id=?",
                             (resp.json()["pr_id"],)).fetchone()
    assert stored["requested_by"] == "U-REQ"
    assert stored["approver"] is None


def test_aud_c_006_audit_records_the_session_identity_not_a_supplied_one(requestor, auditor):
    created = requestor.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "audit actor check", "amount_rupees": "1000",
        "actor": "U-CFO"}).json()
    entries = auditor.get("/api/audit").json()
    match = [e for e in entries if e["object_id"] == created["pr_id"]]
    assert match and all(e["actor"] == "U-REQ" for e in match)


# ================================================================ maker-checker
def test_aud_c_006_a_purchase_request_cannot_be_self_approved(controller):
    """U-PFC holds BudgetController (may raise) and FinanceApprover (may approve
    exceptions), so it is the one identity that can attempt self-approval."""
    created = controller.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "self approval attempt", "amount_rupees": "90000000"})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "Exception Pending"

    resp = controller.post(f"/api/purchase-requests/{created.json()['pr_id']}/approve",
                           json={"reason": "I approve my own request"})
    assert resp.status_code == 403
    assert code_of(resp) == "SELF_APPROVAL"


def test_aud_c_006_an_independent_approver_may_approve_the_same_request(controller, finance,
                                                                       raw_con):
    created = controller.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "independent approval", "amount_rupees": "90000000"}).json()
    resp = finance.post(f"/api/purchase-requests/{created['pr_id']}/approve",
                        json={"reason": "Board-approved overrun, ref CAPEX-COMM/2026/031"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["exception"] is True
    assert raw_con.execute("SELECT approver FROM purchase_request WHERE pr_id=?",
                           (created["pr_id"],)).fetchone()[0] == "U-FIN"


def test_aud_c_006_an_exception_approval_requires_a_recorded_reason(controller, finance):
    created = controller.post("/api/purchase-requests", json={
        "project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
        "description": "no reason given", "amount_rupees": "90000000"}).json()
    resp = finance.post(f"/api/purchase-requests/{created['pr_id']}/approve", json={"reason": "  "})
    assert resp.status_code == 422
    assert code_of(resp) == "REASON_REQUIRED"


def test_aud_c_006_a_budget_revision_cannot_be_self_approved(controller):
    created = controller.post("/api/budget-revisions", json={
        "project_id": "PRJ-01", "wbs_id": "W-03", "budget_head_id": "BH-PM",
        "kind": "SUPPLEMENT", "amount_rupees": "500000",
        "reason": "self approval attempt"}).json()
    resp = controller.post(f"/api/budget-revisions/{created['revision_id']}/approve", json={})
    assert resp.status_code == 403
    assert code_of(resp) == "SELF_APPROVAL"


def test_aud_c_006_a_capitalisation_cannot_be_self_approved(capitalisation, raw_con):
    """Capitalisation requests have no creation route, so the maker is seeded here."""
    raw_con.execute("""INSERT INTO capitalisation_request
        (cap_id, cap_number, project_id, requested_by, requested_at, status, approver,
         approved_at, cap_date, total_paise, version_no)
        VALUES ('CAP-SELF','CAP-2026-9999','PRJ-02','U-CFO','2026-08-05T00:00:00','Submitted',
                NULL,NULL,'2026-08-31',0,1)""")
    raw_con.commit()
    resp = capitalisation.post("/api/capitalisation/CAP-SELF/approve", json={})
    assert resp.status_code == 403
    assert code_of(resp) == "SELF_APPROVAL"


def test_aud_c_006_a_bill_void_is_subject_to_segregation_of_duties():
    """bill.void is registered as a maker-checker permission."""
    assert "bill.void" in auth.MAKER_CHECKER
