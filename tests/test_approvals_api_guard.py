"""Authentication and permission guard on the approval routes (Wave 4, M4b).

Written alongside `app/backend/api/approvals.py`, against the three defect
classes this project has already shipped twice each:

  1. a router that declared only a database dependency and no permission
     guard, so any caller with a syntactically-present session header could
     read the estate;
  2. a router that trusted `X-Actor-Id` / `X-Permissions` as identity and
     authorisation;
  3. a router that built its own `Scope` instead of resolving grants, leaving
     every dimension unrestricted.

**No live PostgreSQL required, and that is the point.** Every refusal asserted
here happens in a router dependency -- before any handler, before any database
access. The same assertions behind a `CAPEX_DB_URL` gate would skip silently
on developer machines and in every CI job without a database, which is exactly
how nine end-to-end tests opted themselves out of CI in Milestone 1 while the
job reported green. The identity store is SQLite and comes from `capex_db`.

**Mount detection is BEHAVIOURAL, never route-table inspection.** Counting
`app.routes` entries under a prefix reported "not mounted" twice in Milestone 1
for routers that were serving perfectly well, because an included router is
wrapped in a container the naive scan never walks. Asking the application
cannot be wrong about it: an unmounted path is 404, a mounted one is anything
else.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend import auth
from app.backend.api import approvals as approvals_api

#: Every route Contract 3 declares, with the permission this router assigns it
#: ON TOP of the router-level `approval.read` floor. Held as data so a route
#: added without a line here shows up in review as an omission.
ROUTES: list[tuple[str, str, str | None]] = [
    ("GET", "/api/approvals/inbox", None),
    ("GET", "/api/approvals/sla", None),
    ("GET", "/api/approvals/definitions", "approval.configure"),
    ("POST", "/api/approvals/definitions", "approval.configure"),
    ("POST", "/api/approvals/definitions/D1/activate", "approval.configure"),
    ("POST", "/api/approvals/definitions/D1/simulate", "approval.configure"),
    ("GET", "/api/approvals/definitions/D1/versions", "approval.configure"),
    ("GET", "/api/approvals/delegations", "approval.delegate"),
    ("POST", "/api/approvals/delegations", "approval.delegate"),
    ("POST", "/api/approvals/delegations/G1/revoke", "approval.delegate"),
    ("POST", "/api/approvals/A1/decide", "approval.act"),
    ("POST", "/api/approvals/A1/recall", "approval.act"),
    ("POST", "/api/approvals/A1/cancel", "approval.act"),
    ("POST", "/api/approvals/A1/resubmit", "approval.act"),
    ("GET", "/api/approvals/A1", None),
    ("GET", "/api/approvals/A1/timeline", None),
]

#: The four permissions Contract 4 introduces.
APPROVAL_PERMISSIONS = ("approval.read", "approval.act", "approval.configure",
                        "approval.delegate")


def _serving_app() -> FastAPI:
    """The application that serves the approval routes.

    `main.py` is lead-owned and frozen for this wave, so this stream exports
    `router` and does NOT mount it. Until the lead mounts it, the routes are
    served by a local application built exactly the way `main.py` will build
    it -- `include_router(router)`, no prefix, the paths carrying their own
    `/api/approvals/...`. Once the lead lands the mount, these same assertions
    run against the real application, middleware and exception handlers
    included, with no edit here.

    Detecting which is behavioural. It cannot probe `main.app` with no
    credentials, because its middleware answers 401 for any `/api/` path
    lacking a session header BEFORE routing happens -- so an unmounted route
    and a guarded one would look identical. Probing with a syntactically
    present (and deliberately invalid) session gets past the middleware and
    reaches the router table, where 404 means genuinely unmounted.
    """
    from app.backend.main import app as main_app

    probe = TestClient(main_app, raise_server_exceptions=False)
    reached = probe.get("/api/approvals/inbox",
                        headers={"X-Session": "probe-not-a-real-session"})
    if reached.status_code != 404:
        return main_app

    local = FastAPI()
    local.include_router(approvals_api.router)
    return local


@pytest.fixture()
def approvals_client(capex_db):
    """A client for the serving application.

    `capex_db` is not optional decoration: without the identity tables
    `resolve_session` raises `sqlite3.OperationalError` and the forged-session
    case returns 500, not 401 -- a test that would have passed for entirely
    the wrong reason had it asserted "not 200" rather than the exact refusal.
    """
    return TestClient(_serving_app(), raise_server_exceptions=False)


@pytest.fixture()
def as_role(approvals_client, make_user):
    """Call a route as a real, logged-in identity holding exactly `roles`.

    The session is minted through the product's own login route and travels as
    `X-Session`. Nothing about the caller's authority is asserted by a header.
    """

    def _call(roles, method, path, *, headers=None, **kw):
        caller = make_user(list(roles))
        merged = {"X-Session": caller.session_id}
        merged.update(headers or {})
        return approvals_client.request(method, path, headers=merged, **kw)

    return _call


# ===================================================== the routes are served
@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_every_approval_route_is_actually_served(approvals_client, method, path,
                                                 _perm):
    """An unmounted path is 404; a mounted one is anything else."""
    assert approvals_client.request(method, path).status_code != 404, (
        f"{method} {path} is not served; Contract 3 declares it and the "
        f"router must register it")


def test_the_router_exports_every_contract_3_route_and_no_others():
    """The inventory, read from the OpenAPI schema rather than the route table.

    A route the contract does not declare is as much a defect as a missing
    one -- an undeclared route is one no reviewer read and no frontend calls.
    """
    served = FastAPI()
    served.include_router(approvals_api.router)
    paths = served.openapi()["paths"]

    declared = {
        "/api/approvals/inbox": {"get"},
        "/api/approvals/sla": {"get"},
        "/api/approvals/definitions": {"get", "post"},
        "/api/approvals/definitions/{definition_id}/activate": {"post"},
        "/api/approvals/definitions/{definition_id}/simulate": {"post"},
        "/api/approvals/definitions/{definition_id}/versions": {"get"},
        "/api/approvals/delegations": {"get", "post"},
        "/api/approvals/delegations/{delegation_id}/revoke": {"post"},
        "/api/approvals/{instance_id}/decide": {"post"},
        "/api/approvals/{instance_id}/recall": {"post"},
        "/api/approvals/{instance_id}/cancel": {"post"},
        "/api/approvals/{instance_id}/resubmit": {"post"},
        "/api/approvals/{instance_id}": {"get"},
        "/api/approvals/{instance_id}/timeline": {"get"},
    }
    assert set(paths) == set(declared), (
        f"served paths differ from Contract 3: "
        f"missing={sorted(set(declared) - set(paths))} "
        f"unexpected={sorted(set(paths) - set(declared))}")
    for path, methods in declared.items():
        assert {m.lower() for m in paths[path]} == methods, (
            f"{path} serves {sorted(paths[path])}, contract declares {sorted(methods)}")


# ===================================================== authentication
@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_no_approval_route_answers_an_unauthenticated_caller(approvals_client,
                                                              method, path, _perm):
    assert approvals_client.request(method, path).status_code == 401, (
        f"{method} {path} answered a caller with no session")


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_a_present_but_invalid_session_is_still_refused(approvals_client,
                                                        method, path, _perm):
    """Header presence is not authentication.

    The middleware in front of these routes only checks that the header
    EXISTS, so this is the assertion that separates a real guard from the
    appearance of one.
    """
    resp = approvals_client.request(method, path,
                                    headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401, f"{method} {path} accepted a forged session"


# ===================================================== headers grant nothing
@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_caller_supplied_identity_headers_grant_no_access(approvals_client,
                                                          method, path, _perm):
    """`X-Actor-Id` and `X-Permissions` are inert.

    A router in this codebase trusted exactly these two headers as identity
    and authorisation. Sent here with no session at all, they must buy
    nothing: the answer is the same 401 an empty request gets.
    """
    resp = approvals_client.request(method, path, headers={
        "X-Actor-Id": "U-ADM",
        "X-User-Id": "U-ADM",
        "X-Permissions": "approval.read approval.act approval.configure "
                         "approval.delegate",
        "X-Roles": "Administrator",
    })
    assert resp.status_code == 401, (
        f"{method} {path} honoured caller-supplied identity headers")


@pytest.mark.parametrize("method,path,permission",
                         [r for r in ROUTES if r[2] is not None])
def test_a_permission_header_cannot_add_a_permission_a_role_lacks(
        as_role, method, path, permission):
    """Authenticated, but claiming an authority the roles do not carry.

    The dangerous shape: a REAL session (so authentication succeeds) plus a
    header naming the permission. Permissions come from the principal's roles
    and nowhere else, so the answer must still be 403.
    """
    holders = set(approvals_api._holders_of(permission) or ())
    without = [r for r in auth.ROLES if r not in holders]
    assert without, f"every role holds {permission}; nothing to test"

    resp = as_role([without[0]], method, path, headers={
        "X-Permissions": permission,
        "X-Actor-Id": "U-ADM",
        "X-Roles": "Administrator",
    })
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code}; a header must not "
        f"confer {permission}")


# ===================================================== the guard's shape
def test_the_guard_is_declared_on_the_router_not_route_by_route():
    """Structural tripwire.

    A route added later inherits the router-level dependency automatically.
    If the guard is ever moved onto individual routes, the next route added
    without one ships open -- and every test above would still pass until that
    route exists.
    """
    assert approvals_api.router.dependencies, (
        "the approvals router carries no router-level dependency; the guard "
        "has been moved onto individual routes and a future route will ship "
        "open")


def test_the_router_floor_is_approval_read_and_excludes_only_auditor():
    """Contract 4 said "every role". Auditor is the one exclusion, and the
    reason is worth keeping written down.

    `test_aud_c_006_auditor_is_read_only` pins Auditor to an allow-list of
    four permissions. Granting a fifth means widening an audit-finding
    assertion, which is not a change to make in passing so that a router floor
    reads more tidily. An Auditor reads approval history through the audit
    chain -- the record that actually matters for that role, and one this wave
    hash-chains per instance.

    Asserted as an exact set rather than "at least these", so quietly granting
    Auditor later, or quietly dropping a role that needs its own inbox, both
    fail here. Recorded against D-12, the role-to-permission sign-off.
    """
    holders = set(approvals_api._holders_of("approval.read") or ())
    expected = set(auth.ROLES) - {"Auditor"}
    assert holders == expected, (
        f"approval.read holders drifted: missing {sorted(expected - holders)}, "
        f"unexpected {sorted(holders - expected)}")
    assert "Auditor" not in holders, (
        "granting Auditor approval.read requires widening "
        "test_aud_c_006_auditor_is_read_only, which is a D-12 decision")


# ===================================================== authorisation
@pytest.mark.parametrize("method,path,permission",
                         [r for r in ROUTES if r[2] is not None])
def test_a_role_lacking_the_permission_is_refused(as_role, method, path,
                                                  permission):
    """Authenticated is not authorised.

    Each route is called by a real logged-in identity that holds
    `approval.read` -- so it clears the router's floor -- but not the route's
    own permission. A 401 here would mean the identity failed to authenticate
    and the test proved nothing, so only 403 passes.
    """
    holders = set(approvals_api._holders_of(permission) or ())
    without = [r for r in auth.ROLES if r not in holders]
    assert without, f"every role holds {permission}; nothing to test"

    resp = as_role([without[0]], method, path)
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for a role lacking "
        f"{permission}; 403 expected")


def test_definitions_is_not_shadowed_by_the_instance_id_route(as_role):
    """The routing bug that would be a privilege escalation.

    `/api/approvals/{instance_id}` matches the literal string `definitions`
    just as happily as a real instance id, and FastAPI resolves in
    registration order. Registered the wrong way round,
    `GET /api/approvals/definitions` would quietly serve the instance-detail
    handler and be governed by `approval.read` -- which EVERY role holds --
    instead of `approval.configure`.

    Asserted through the consequence rather than the order: a Requestor holds
    `approval.read` and not `approval.configure`, so 403 is the only answer
    that proves the literal route won.
    """
    for path in ("/api/approvals/definitions",
                 "/api/approvals/delegations",
                 "/api/approvals/definitions/D1/versions"):
        resp = as_role(["Requestor"], "GET", path)
        assert resp.status_code == 403, (
            f"GET {path} returned {resp.status_code} for a Requestor; the "
            f"literal path has been shadowed by /api/approvals/{{instance_id}}")


def test_inbox_and_sla_are_not_shadowed_and_stay_on_the_read_floor(as_role):
    """The other half of the same ordering concern.

    `inbox` and `sla` sit on `approval.read`, so a shadowing bug cannot be
    detected by a status change. It is detected by them NOT being refused as
    the instance-detail route would be, and by them reaching the engine: with
    no PostgreSQL configured the honest answer is 503, never 403 or 404.

    Requestor, not Auditor. Auditor is the one role deliberately excluded from
    `approval.read` (see the floor test), so asking as an Auditor would return
    403 for a reason that has nothing to do with shadowing -- the test would
    fail while proving nothing about route order.
    """
    for path in ("/api/approvals/inbox", "/api/approvals/sla"):
        resp = as_role(["Requestor"], "GET", path)
        assert resp.status_code not in (401, 403, 404), (
            f"GET {path} returned {resp.status_code}; every role holds "
            f"approval.read, so this route must reach the handler")


def test_an_unknown_permission_is_refused_rather_than_granted(approvals_client):
    """Fail closed on a name no table defines.

    `_holders_of` returning `None` must never be read as "unrestricted". It is
    the shape a typo takes.
    """
    assert approvals_api._holders_of("approval.not_a_real_permission") is None
    assert approvals_api._holds({"roles": list(auth.ROLES)},
                                "approval.not_a_real_permission") is False
    assert approvals_api._holds({}, "approval.read") is False, (
        "a principal with no roles must hold nothing")


# ===================================================== the actor
class _FakeState:
    pass


class _FakeRequest:
    """Carries only what the helpers under test read: `state` and `headers`."""

    def __init__(self, state, headers=None):
        self.state = state
        self.headers = headers or {}


def test_the_actor_is_server_derived_and_ignores_caller_supplied_headers():
    """`_actor` feeds `approval_action.actor_user_id`, the hash-chained audit
    stream, and the identity Contract 5's maker-checker compares. A
    caller-supplied actor would make maker-checker trivially defeatable."""
    state = _FakeState()
    state.approval_principal = {"user_id": "U-REAL-ACTOR"}
    request = _FakeRequest(state, {"X-Actor-Id": "U-IMPERSONATED",
                                   "X-User-Id": "U-IMPERSONATED"})
    assert approvals_api._actor(request) == "U-REAL-ACTOR"

    # With no authenticated principal it names nobody, rather than falling
    # back to whatever the caller sent.
    unauthenticated = _FakeRequest(_FakeState(),
                                   {"X-Actor-Id": "U-IMPERSONATED"})
    assert approvals_api._actor(unauthenticated) == "UNKNOWN"


def test_no_request_header_is_read_for_identity_or_authorisation():
    """Source-level tripwire, and it earns its place.

    Every behavioural assertion above can only probe the header names someone
    thought to try. This one fails on the NEXT header a future edit decides to
    trust. The two headers the contract does allow -- the correlation id and
    the session/authorization pair the guard forwards to `main.principal` --
    are named explicitly so the check is exact rather than approximate.
    """
    import inspect
    import re

    source = inspect.getsource(approvals_api)
    read = set(re.findall(r'headers\.get\(\s*"([^"]+)"', source))
    allowed = {"X-Correlation-Id", "Authorization", "X-Session"}
    assert read <= allowed, (
        f"this router reads request headers it must not: "
        f"{sorted(read - allowed)}; identity comes from the session and "
        f"permissions from the principal's roles")


# ===================================================== the scope
def test_the_router_resolves_real_grants_and_builds_no_scope_of_its_own():
    """Contract 6 / Wave 3 Contract 2, asserted at the source.

    `read_all` short-circuits `compile_scope` to TRUE before any dimension is
    examined, so a router that constructs one is unrestricted on every route
    at once -- invisibly. It comes from `user_access_flag` and nowhere else.
    A source assertion is the one that survives someone reintroducing the
    shortcut in a different shape, and the router resolves through a database
    this test does not have.
    """
    import inspect

    source = inspect.getsource(approvals_api)
    assert "read_all=True" not in source, (
        "this router must not construct a read_all scope of its own")
    assert "Scope(" not in source, (
        "this router must not construct a Scope at all; "
        "principal_scope.scope_for_request is the single construction path")
    assert "scope_for_request" in source, (
        "the router must resolve the caller's real grants (Contract 6)")


def test_a_scope_that_cannot_be_resolved_sees_nothing(approvals_client):
    """Fail closed, end to end: no session, no grants, no rows.

    Asserted through `compile_scope` rather than by inspecting the `Scope`'s
    fields, because what protects a row is the predicate that reaches the
    database.
    """
    from app.backend.pg import principal_scope, repo

    scope = principal_scope.denied_scope("U-NOBODY")
    assert principal_scope.is_denied(scope)
    predicate, _ = repo.compile_scope(scope, {
        "entity": "i.entity_id", "plant": "i.plant_id",
        "project": "i.project_id", "location": "i.location_id"})
    assert predicate == "FALSE", (
        f"a denied scope must compile to FALSE, got {predicate!r}")


# ===================================================== errors and correlation
@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_every_refusal_carries_a_correlation_id_and_a_code(approvals_client,
                                                            method, path, _perm):
    """"Every response carries `X-Correlation-Id`" has to hold for the
    responses that need tracing most -- the refusals.

    A raised `HTTPException` never returns the handler's `Response` object;
    Starlette builds a fresh one. Setting the header only in the handler would
    have covered success responses and nothing else.
    """
    resp = approvals_client.request(method, path,
                                    headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401
    assert resp.headers.get("X-Correlation-Id"), (
        f"{method} {path} refused without a correlation id")
    body = resp.json()
    assert "detail" in body and body["detail"].get("code"), (
        f"{method} {path} refused without a `code`: {body!r}")


def test_a_supplied_correlation_id_is_echoed_rather_than_replaced(
        approvals_client):
    """A client that already has a correlation id keeps it, so one id spans
    the browser request, this API and the audit row."""
    resp = approvals_client.get(
        "/api/approvals/inbox",
        headers={"X-Session": "not-a-real-session",
                 "X-Correlation-Id": "corr-abcdef123456"})
    assert resp.headers.get("X-Correlation-Id") == "corr-abcdef123456"


def test_a_refusal_body_never_leaks_a_session_or_a_secret(approvals_client,
                                                          make_user):
    """No credential, no session id, no connection string in a response.

    The caller's REAL session id is the interesting one: it is the single
    most valuable string in the request, it is a bearer credential for the
    whole API, and an error message that echoed "session <id> lacks
    approval.act" would put it in every log and screenshot of the failure.
    """
    caller = make_user(["Auditor"])
    resp = approvals_client.request("POST", "/api/approvals/A1/decide",
                                    headers={"X-Session": caller.session_id})
    assert resp.status_code == 403, resp.text

    text = resp.text.lower()
    assert caller.session_id.lower() not in text, (
        "the refusal echoed the caller's session id")
    for leak in ("password", "postgres://", "postgresql://", "password_hash",
                 "secret"):
        assert leak not in text, f"a refusal body leaked {leak!r}: {resp.text}"


# ===================================================== the frozen status map
def test_every_frozen_error_code_has_a_status_and_it_is_the_agreed_one():
    """Contract 3's frozen codes, and Contract 8's replay rule.

    403 vs 409 vs 422 is a contract, not a preference: 403 says the request
    will never succeed for this caller, 409 says state moved and a refreshed
    retry may well succeed, 422 says the request is well formed but
    incomplete. A client retries on exactly one of those.
    """
    expected = {
        "SELF_APPROVAL": 403,
        "NOT_AN_ASSIGNEE": 403,
        "STAGE_NOT_OPEN": 409,
        "OBJECT_VERSION_STALE": 409,
        "BUDGET_MOVED": 409,
        "REASON_REQUIRED": 422,
        "IDEMPOTENCY_KEY_REQUIRED": 422,
        "IDEMPOTENT_REPLAY": 200,
    }
    for code, status in expected.items():
        assert approvals_api._status_for_code(code) == status, (
            f"{code} must render as {status}, got "
            f"{approvals_api._status_for_code(code)}")

    # The rest of Contract 3's frozen list must at least be known refusals.
    for code in ("APPROVAL_ROUTE_UNRESOLVED", "NO_INDEPENDENT_APPROVER",
                 "DEFINITION_NOT_ACTIVE", "DEFINITION_IMMUTABLE",
                 "DELEGATION_WINDOW_INVALID"):
        assert code in approvals_api._ENGINE_STATUS, (
            f"{code} is frozen in Contract 3 but this router does not map it")
        assert 400 <= approvals_api._ENGINE_STATUS[code] < 500


def test_an_unrecognised_engine_refusal_is_a_conflict_not_a_server_fault():
    """A refusal this router has not been taught about is still a refusal.

    Rendering it 500 would blame the deployment for a control the engine
    enforced correctly -- and would hide it from any client that only retries
    on 4xx.
    """
    status = approvals_api._status_for_code("SOME_NEW_ENGINE_REFUSAL")
    assert 400 <= status < 500, f"expected a 4xx, got {status}"


# ===================================================== Contract 4 fallback
def test_the_contract_4_permissions_match_the_frozen_contract():
    """The fallback table is a transcription, so it is checked as one."""
    table = approvals_api._CONTRACT4_PERMISSIONS
    assert set(table) == set(APPROVAL_PERMISSIONS)
    assert set(table["approval.read"]) == set(auth.ROLES)
    assert set(table["approval.act"]) == {
        "Requestor", "BudgetController", "ProcurementApprover",
        "FinanceApprover", "CapitalisationApprover"}
    assert set(table["approval.configure"]) == {"Administrator"}
    assert set(table["approval.delegate"]) == {
        "BudgetController", "ProcurementApprover", "FinanceApprover",
        "CapitalisationApprover"}
    for permission, roles in table.items():
        assert set(roles) <= set(auth.ROLES), (
            f"{permission} names a role that does not exist: "
            f"{sorted(set(roles) - set(auth.ROLES))}")


def test_auth_permissions_wins_wherever_it_defines_an_approval_permission():
    """The fallback exists only until the lead lands Contract 4 in `auth.py`,
    and it must not outlive that. `auth.PERMISSIONS` is authoritative for any
    key it defines, so the table below silently stops being consulted."""
    for permission in APPROVAL_PERMISSIONS:
        if permission in auth.PERMISSIONS:
            assert approvals_api._holders_of(permission) == \
                tuple(auth.PERMISSIONS[permission]), (
                    f"{permission} is defined in auth.PERMISSIONS but this "
                    f"router is not reading it from there")


def test_approval_act_excludes_the_read_only_role(as_role):
    """Auditor is read-only, everywhere in this system.

    A role that can read every approval in the estate and also decide them is
    the segregation failure this permission split exists to prevent.
    """
    assert "Auditor" not in (approvals_api._holders_of("approval.act") or ())
    resp = as_role(["Auditor"], "POST", "/api/approvals/A1/decide")
    assert resp.status_code == 403


def test_approval_configure_is_administrator_only(as_role):
    """Contract 4. A role that can rewrite the workflow can approve anything
    by routing it to itself, so this is the heaviest of the four."""
    holders = approvals_api._holders_of("approval.configure") or ()
    assert set(holders) == {"Administrator"}
    for role in ("FinanceApprover", "CapitalisationApprover", "Auditor"):
        resp = as_role([role], "POST", "/api/approvals/definitions")
        assert resp.status_code == 403, (
            f"{role} reached the definition-creation route")


# ===================================================== bodies
def test_every_request_body_forbids_unknown_fields():
    """A silently-ignored field is how a caller comes to believe it set
    something it did not -- and the fields worth mis-sending on this surface
    are `actor`, `reason_code` and `object_version`."""
    import inspect

    from pydantic import BaseModel

    models = [obj for _, obj in inspect.getmembers(approvals_api, inspect.isclass)
              if issubclass(obj, BaseModel) and obj is not BaseModel
              and obj.__module__ == approvals_api.__name__]
    assert models, "no request models found; this test would pass vacuously"
    for model in models:
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} does not forbid unknown fields")


def test_a_decision_body_cannot_nominate_the_actor(approvals_client, make_user):
    """`extra="forbid"` is what stops a body field from becoming an identity.

    A permitted caller sending `actor` must be refused the field outright,
    not have it quietly dropped -- because "quietly dropped" is
    indistinguishable, to the caller, from "accepted".

    The database dependency is satisfied with a stand-in so that body
    validation is what the response reports. Left unsatisfied it resolves
    first and answers 503, which would have made this test pass on any body
    at all, including a valid one.
    """
    # `approvals_client.app`, not a fresh `_serving_app()`: the latter builds
    # a SECOND application when the router is not yet mounted on `main.app`,
    # and the override would then be installed on an application no request
    # ever reaches.
    app = approvals_client.app
    app.dependency_overrides[approvals_api._get_database] = lambda: None
    try:
        caller = make_user(["FinanceApprover"])
        body = {"action": "APPROVE", "idempotency_key": "k1",
                "object_version": 1}
        resp = approvals_client.request(
            "POST", "/api/approvals/A1/decide",
            headers={"X-Session": caller.session_id},
            json={**body, "actor": "U-SOMEONE-ELSE"})
        assert resp.status_code == 422, (
            f"a body naming an actor was not refused: "
            f"{resp.status_code} {resp.text}")
        assert "actor" in resp.text, "the refusal does not name the extra field"

        # The same body without the extra field clears validation -- so the
        # 422 above is attributable to `extra="forbid"` and not to the body
        # being malformed in some other way.
        accepted = approvals_client.request(
            "POST", "/api/approvals/A1/decide",
            headers={"X-Session": caller.session_id}, json=body)
        assert accepted.status_code != 422, (
            f"the control body was itself rejected: {accepted.text}")
    finally:
        app.dependency_overrides.pop(approvals_api._get_database, None)
