"""Authentication and permission guard on the budget routes.

Written during Wave 2 integration, after the budget router arrived with a
`_scope_for` that returned an unconditional
`Scope(principal_kind="SERVICE", read_all=True)` for every request and an
`_actor` that trusted a caller-supplied `X-User-Id` header. Mounted as
delivered, any caller reaching the middleware -- which checks that a session
header EXISTS without validating it -- could have read the whole estate and
approved a budget revision while naming someone else as the approver.

That is Milestone 1's finding F1 a second time, in a second router, which is
why these guards are declared on the ROUTER rather than route by route: the
failure mode is not a wrong guard, it is a route that ships with no guard
because its author declared only a database dependency.

**No live PostgreSQL required.** Refusal happens in the router dependency,
before any handler and before any database access. That matters: the same
tests behind a `CAPEX_DB_URL` gate would skip silently on developer machines
and in every CI job without a database, which is precisely how nine
end-to-end tests opted themselves out of CI in Milestone 1 while the job
reported green.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backend import auth
from app.backend.api import budget as budget_api
from app.backend.main import app

#: Every route the frozen Wave 2 contract declares, with the permission this
#: integration assigned it. Held as data so that a route added without a line
#: here shows up in review as an omission.
ROUTES: list[tuple[str, str, str | None]] = [
    ("GET", "/api/budget/periods", None),
    ("POST", "/api/budget/periods/P1/transition", "period.transition"),
    ("GET", "/api/budget/cells", None),
    ("GET", "/api/budget/availability?wbs_id=W&budget_head_id=H&amount_paise=1",
     "budget.check"),
    ("GET", "/api/budget/lines", None),
    ("POST", "/api/budget/revisions", "revision.create"),
    ("POST", "/api/budget/revisions/R1/approve", "revision.approve"),
    ("POST", "/api/budget/revisions/R1/reject", "revision.approve"),
    ("POST", "/api/budget/transfers", "revision.create"),
    ("POST", "/api/budget/transfers/T1/approve", "revision.approve"),
    ("POST", "/api/budget/transfers/T1/reject", "revision.approve"),
    ("GET", "/api/budget/versions", None),
    ("GET", "/api/budget/compare", None),
]


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    """Every test here needs the identity tables.

    Without them `resolve_session` raises `sqlite3.OperationalError` and the
    forged-session case returns 500, not 401 -- a test that would have passed
    for entirely the wrong reason had it asserted "not 200" rather than the
    exact refusal status.
    """
    return capex_db


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_every_budget_route_is_actually_served(method, path, _perm):
    """Mount detection as a BEHAVIOURAL probe, never route-table inspection.

    Counting `app.routes` entries whose path starts with `/api/budget`
    reports 4 of these 13 on the FastAPI version in use, because an included
    router is wrapped in a container the naive scan never walks. Two
    Milestone 1 fixtures inspected the route table and both concluded "not
    mounted" while the router served perfectly well. Asking the application
    cannot be wrong about it: an unmounted path is 404, a mounted one is
    anything else.
    """
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path).status_code != 404, (
        f"{method} {path} is not served; main.py mounts the budget router "
        f"unconditionally, so a 404 means include_router did not register it")


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_no_budget_route_answers_an_unauthenticated_caller(method, path, _perm):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path).status_code == 401, (
        "a caller with no session must be refused")


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_a_present_but_invalid_session_is_still_refused(method, path, _perm):
    """Header presence is not authentication.

    The middleware in front of these routes only checks that the header
    exists, so this is the assertion that separates a real guard from the
    appearance of one.
    """
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.request(method, path, headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401, f"{method} {path} accepted a forged session"


def test_the_guard_is_declared_on_the_router_not_route_by_route():
    """Structural tripwire.

    A route added later inherits the router-level dependency automatically.
    If the guard is ever moved onto individual routes, the next route added
    without one ships open -- and every test above would still pass until
    that route exists.
    """
    assert budget_api.router.dependencies, (
        "the budget router carries no router-level dependency; the guard has "
        "been moved onto individual routes and a future route will ship open")


# ================================================================ permissions
def test_period_transition_permission_exists_and_excludes_read_only_roles():
    assert "period.transition" in auth.PERMISSIONS
    roles = auth.PERMISSIONS["period.transition"]
    assert "Auditor" not in roles, "Auditor is read-only"
    assert set(roles) <= set(auth.ROLES)


@pytest.mark.parametrize(
    "method,path,permission", [r for r in ROUTES if r[2] is not None])
def test_a_role_lacking_the_permission_is_refused(make_user, method, path,
                                                  permission):
    """Authenticated is not authorised.

    Each mutating route is called by a real logged-in identity that holds
    `budget.read` -- so it clears the router's floor -- but not the route's
    own permission. A 401 here would mean the identity failed to authenticate
    and the test proved nothing, so only 403 passes.
    """
    holders = set(auth.PERMISSIONS[permission])
    without = [r for r in auth.ROLES if r not in holders]
    assert without, f"every role holds {permission}; nothing to test"

    caller = make_user([without[0]])
    resp = caller.request(method, path)
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for a role lacking "
        f"{permission}; 403 expected")


class _FakeState:
    pass


class _FakeRequest:
    """Carries only what the helpers under test read: `state` and `headers`."""

    def __init__(self, state, headers=None):
        self.state = state
        self.headers = headers or {}


def test_the_actor_is_server_derived_and_ignores_a_caller_supplied_header():
    """`_actor` fed `budget_line.created_by`, approval decisions and the audit
    chain from an `X-User-Id` request header, so any caller could attribute a
    financial record to anyone -- and maker-checker compares exactly those
    identities.
    """
    state = _FakeState()
    state.budget_principal = {"user_id": "U-REAL-ACTOR"}
    request = _FakeRequest(state, {"X-User-Id": "U-IMPERSONATED"})
    assert budget_api._actor(request) == "U-REAL-ACTOR"

    # With no authenticated principal it names nobody, rather than falling
    # back to whatever the caller sent.
    unauthenticated = _FakeRequest(_FakeState(), {"X-User-Id": "U-IMPERSONATED"})
    assert budget_api._actor(unauthenticated) == "UNKNOWN"


def test_scope_is_not_an_unconditional_whole_estate_service_principal():
    """The regression for the delivered `read_all=True` SERVICE scope, which
    made `compile_scope` short-circuit to TRUE on every request.
    """
    def _scope_for_roles(roles):
        state = _FakeState()
        state.budget_principal = {"user_id": "U-1", "roles": roles}
        return budget_api._scope_for(_FakeRequest(state))

    ordinary = _scope_for_roles(["Requestor"])
    assert ordinary.principal_kind == "USER"
    assert ordinary.user_id == "U-1"
    assert ordinary.read_all is False

    assert _scope_for_roles(["Auditor"]).read_all is True
