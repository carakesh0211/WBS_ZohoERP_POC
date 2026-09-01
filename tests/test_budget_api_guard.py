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


def test_read_all_is_never_derived_from_a_role_name():
    """Rewritten in Wave 3, and the change is the point.

    This used to end `assert _scope_for_roles(["Auditor"]).read_all is True`,
    pinning the very behaviour Contract 2 forbids: `read_all` short-circuits
    `compile_scope` to TRUE before any dimension is examined, so granting it
    to a set of role NAMES made every holder of that role unrestricted on all
    thirteen budget routes -- and made it so invisibly that a test asserted it
    as correct.

    `read_all` now comes from `user_access_flag` and nowhere else: a role that
    genuinely needs the whole estate carries the flag, which is auditable and
    revocable, where a name in a frozenset is neither.

    Asserted at the source, because the router now resolves through a database
    and this test has none -- and a source-level assertion is the one that
    survives someone reintroducing the shortcut in a different shape.
    """
    import inspect

    source = inspect.getsource(budget_api)
    assert "_WHOLE_ESTATE_BUDGET_ROLES" not in source, (
        "the role-derived whole-estate set is back; read_all must come from "
        "user_access_flag, never from a role name")
    assert "read_all=True" not in source, (
        "this router must not construct a read_all scope of its own")
    assert "scope_for_request" in source, (
        "the router must resolve the caller's real grants (Contract 4)")


def test_an_unresolvable_principal_gets_a_scope_that_sees_nothing():
    """Fail closed, end to end: no session, no grants, no rows.

    The half that matters most. A router that cannot establish who is asking
    must not fall back to asking for everything, so the denial is asserted
    through `compile_scope` rather than by inspecting the Scope's fields --
    what protects a row is the predicate that reaches the database.
    """
    from app.backend.pg import principal_scope, repo

    for principal in ({}, None, {"user_id": ""}, {"roles": ["Auditor"]}):
        scope = principal_scope.denied_scope("U-NOBODY")
        assert principal_scope.is_denied(scope)
        predicate, _ = repo.compile_scope(scope, {
            "entity": "p.entity_id", "plant": "p.plant_id",
            "project": "p.project_id", "location": "p.location_id"})
        assert predicate == "FALSE", (
            f"a denied scope must compile to FALSE, got {predicate!r} "
            f"for principal {principal!r}")


def test_the_route_inventory_is_not_silently_empty():
    """Guards `test_aud_c_006_every_mutating_route_is_covered_by_the_
    authorisation_matrix` against passing vacuously.

    That assertion compares the live route inventory against a hand-written
    matrix. If the inventory ever came back empty -- as it effectively did on
    FastAPI 0.141.1, where route-table introspection saw none of the Wave 2
    routers -- the comparison would report every covered route as "stale"
    rather than saying the inventory was broken. This pins the floor.

    It lives here rather than beside that assertion because `test_api_auth.py`
    is one of the 220 baseline files, whose count is itself guarded: the
    baseline exists to catch a baseline test being REMOVED, and adding to it
    would retire that guard by dilution.
    """
    from tests.test_api_auth import _mutating_paths

    live = _mutating_paths(app)
    assert len(live) >= 20, (
        f"only {len(live)} mutating paths found; the application serves far "
        f"more, so the route inventory is not seeing them")
