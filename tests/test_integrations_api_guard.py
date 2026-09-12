"""The `/api/integrations/*` router: its guard, its contract, and its silences.

Three properties, and the third is the one this suite exists for.

1. THE GUARD IS ON THE ROUTER, NOT THE ROUTE.
   `api/integrations.py` declares `connector.read` as a router-level
   dependency, so a route added to that file next month inherits it instead of
   shipping open by omission. Two routers in this codebase have already
   shipped the other way. The structural assertion here is backed by MUTATION
   TESTS: the dependency is removed and the router is re-exercised, and the
   test fails if the refusal survives its own guard being deleted. A guard
   assertion that passes with the guard removed is testing nothing, and this
   file would rather find that out here than in an audit.

2. THE PATH TEMPLATES MATCH THE FRONTEND CHARACTER FOR CHARACTER.
   `integration-api.js` probes `/openapi.json` and compares path templates by
   STRING EQUALITY. `{connectionId}` serves exactly as well as
   `{connection_id}` and is reported ABSENT by that probe, so the screen
   renders an unavailable state over a working endpoint and nothing anywhere
   raises. The templates are therefore read OUT OF THE FRONTEND FILE here and
   compared to what the router mounts, rather than retyped into this test --
   a copy in a test is one more place for the same drift to hide.

3. THE SILENCES ARE LOAD-BEARING.
   Six of the sixteen operations have nothing behind them. They answer a coded
   503 naming what is missing -- never 404, which `core/api-client.js`
   collapses into "no records were found", and never a synthesised number.
   `/control-totals` is asserted hardest: this database holds everything
   needed to produce a confident-looking total and nothing that would make it
   true, and a total computed from our ledger alone would compare us with
   ourselves and therefore always balance.

**No live PostgreSQL, and no skips.** Every property here is a property of the
router, the guard or the source, and every one of them is decided before a
database is touched. A `CAPEX_DB_URL` gate would turn this whole file into a
silent skip on every developer machine and in every CI job without a server --
which is how nine end-to-end tests opted themselves out of CI in Milestone 1
while the job reported green. A skip is not a pass.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend import auth
from app.backend.api import integrations as integrations_api

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(integrations_api.__file__)
FRONTEND_API = ROOT / "app" / "frontend" / "src" / "features" / "integration" / "integration-api.js"
C10_PATH = ROOT / "research" / "30_contracts" / "C10_messages.json"


# ------------------------------------------------------------------ the app
def _app() -> FastAPI:
    """A bare application carrying ONLY this router.

    `main.py` mounts the router and this stream does not own `main.py`, so
    these tests cannot reach it through `main.app` -- and must not
    `include_router` onto the global `main.app` either: that would add four
    mutating paths to the live route inventory, and
    `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
    compares that inventory against `MUTATING_ROUTES`, which the lead updates
    in the same commit as the mount. Mutating the shared app from a test would
    fail that assertion from a completely unrelated file.

    Nothing is lost by isolating it. The refusal under test happens in the
    router's own dependency, and `auth.resolve_session(None)` raises the same
    401 `NOT_AUTHENTICATED` that `main.py`'s middleware returns, so an
    unauthenticated caller is refused with the identical code either way.
    """
    app = FastAPI()
    app.include_router(integrations_api.router)
    return app


def _client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app or _app(), raise_server_exceptions=False)


#: (method, concrete url, the permission the route requires beyond the floor)
ROUTES: list[tuple[str, str, str | None]] = [
    ("GET", "/api/integrations/connections", None),
    ("POST", "/api/integrations/connections", "connector.manage"),
    ("POST", "/api/integrations/connections/C1/authorize", "connector.manage"),
    ("GET", "/api/integrations/connections/C1/organizations", None),
    ("PUT", "/api/integrations/connections/C1/organization", "connector.manage"),
    ("GET", "/api/integrations/connections/C1/scopes", None),
    ("POST", "/api/integrations/connections/C1/validate", "connector.manage"),
    ("GET", "/api/integrations/connections/C1/health", None),
    ("GET", "/api/integrations/events", None),
    ("GET", "/api/integrations/dead-letters", None),
    ("POST", "/api/integrations/dead-letters/inbox/R1/retry", "connector.manage"),
    ("POST", "/api/integrations/dead-letters/inbox/R1/discard", "connector.manage"),
    ("GET", "/api/integrations/outbox", None),
    ("GET", "/api/integrations/inbox", None),
    ("GET", "/api/integrations/reconciliation", None),
    ("GET", "/api/integrations/exceptions", None),
    ("GET", "/api/integrations/control-totals", None),
]

MUTATING = [r for r in ROUTES if r[2] is not None]

#: The operations with nothing behind them, and the code each must answer.
#: Held as data so that backing one of them later shows up here as a decision.
#:
#: `/reconciliation` HAS LEFT THIS MAP, and this comment is the decision
#: `test_every_unavailable_route_is_accounted_for` asks for. It refused with
#: `INTEGRATION_RECONCILIATION_UNAVAILABLE` for eleven migrations, and the
#: reason it gave was a fact about the schema -- "PostgreSQL holds no purchase
#: order, GRN or bill". `013_procurement.sql` creates all eight procurement
#: documents, so that sentence stopped being true, and a route that keeps
#: refusing with a reason that is no longer true is not being careful; it is
#: reporting a missing migration that has already landed and sending whoever
#: reads it to the wrong place.
#:
#: `/control-totals` HAS NOT MOVED AND MUST NOT. 013 changes nothing about it:
#: it needs ZOHO's count and value for a window, no endpoint in this build
#: knows them, and every figure 013 added is still ours. The tests below that
#: pin it -- `test_control_totals_never_returns_a_number`,
#: `test_the_source_refuses_to_synthesise_a_control_total` and its half of
#: `test_control_totals_takes_no_database_dependency` -- are untouched.
UNAVAILABLE: dict[str, str] = {
    "/api/integrations/connections/{connection_id}/authorize":
        "OAUTH_AUTHORISATION_UNAVAILABLE",
    "/api/integrations/connections/{connection_id}/organizations":
        "ORGANISATION_DISCOVERY_UNAVAILABLE",
    "/api/integrations/connections/{connection_id}/organization":
        "ORGANISATION_MAPPING_UNAVAILABLE",
    "/api/integrations/connections/{connection_id}/validate":
        "SCOPE_VALIDATION_UNAVAILABLE",
    "/api/integrations/control-totals":
        "CONTROL_TOTALS_UNAVAILABLE",
}


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    """Every test here needs the identity tables.

    Without them `resolve_session` raises `sqlite3.OperationalError` and a
    forged session returns 500, not 401 -- a test that would pass for entirely
    the wrong reason had it asserted "not 200".
    """
    return capex_db


def _detail(response) -> dict:
    body = response.json()
    assert isinstance(body, dict) and "detail" in body, f"unexpected shape: {body!r}"
    return body["detail"]


# ================================================== 1. the frontend contract
def _frontend_templates() -> set[str]:
    """Every `/api/integrations/...` template `integration-api.js` probes for.

    Read from the frontend file itself. Retyping them into this test would
    create a second copy that drifts from the first in exactly the silent way
    the probe cannot report.
    """
    text = FRONTEND_API.read_text(encoding="utf-8")
    return {m for m in re.findall(r"template:\s*'([^']+)'", text)
            if m.startswith("/api/integrations")}


def _mounted_templates() -> set[str]:
    return {r.path for r in integrations_api.router.routes
            if getattr(r, "path", "").startswith("/api/integrations")}


def test_the_frontend_contract_file_is_actually_readable():
    """Guards the two assertions below against passing vacuously.

    If the frontend file moved, `_frontend_templates()` would return an empty
    set and a subset assertion over it would pass while proving nothing.
    """
    assert FRONTEND_API.exists(), f"{FRONTEND_API} is missing"
    assert len(_frontend_templates()) >= 15, (
        "fewer templates found in integration-api.js than it declares; the "
        "regex no longer matches the file and every comparison below is vacuous")


def test_every_template_the_frontend_probes_for_is_mounted():
    """The probe compares by string equality, so this is the whole contract.

    A route mounted under a differently spelled path parameter serves
    perfectly and is reported ABSENT, and the screen renders an unavailable
    state over a working endpoint with nothing raising anywhere.
    """
    missing = _frontend_templates() - _mounted_templates()
    assert not missing, (
        f"integration-api.js probes for templates this router does not mount, "
        f"so those screens will report the endpoint absent while it serves: "
        f"{sorted(missing)}")


def test_this_router_mounts_nothing_the_frontend_does_not_ask_for():
    """The other direction. A route nobody probes for is either dead surface or
    a spelling the frontend will never find, and both are worth seeing."""
    extra = _mounted_templates() - _frontend_templates()
    assert not extra, (
        f"this router mounts templates integration-api.js never probes for: "
        f"{sorted(extra)}")


def test_path_parameters_are_named_exactly_as_the_frontend_spells_them():
    """`{connectionId}` would serve and would never be found."""
    names = set(re.findall(r"\{(\w+)\}", " ".join(sorted(_mounted_templates()))))
    assert names == {"connection_id", "exception_id", "queue", "row_id"}, (
        f"unexpected path-parameter names {sorted(names)}; the frontend "
        f"templates use connection_id, exception_id, queue and row_id")


@pytest.mark.parametrize("method,path,_perm", ROUTES,
                         ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_every_route_is_actually_served(method, path, _perm):
    """Mount detection as a BEHAVIOURAL probe, never route-table inspection.

    Counting `app.routes` entries misreports included routers on some FastAPI
    versions -- two Milestone 1 fixtures concluded "not mounted" while the
    router served perfectly well. Asking the application cannot be wrong: an
    unmounted path is 404, a mounted one is anything else.
    """
    assert _client().request(method, path).status_code != 404, (
        f"{method} {path} is not served")


# ============================================================ 2. the guard
@pytest.mark.parametrize("method,path,_perm", ROUTES,
                         ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_no_route_answers_an_unauthenticated_caller(method, path, _perm):
    resp = _client().request(method, path, json={})
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
    assert _detail(resp)["code"] == "NOT_AUTHENTICATED"


@pytest.mark.parametrize("method,path,_perm", ROUTES,
                         ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_present_but_invalid_session_is_still_refused(method, path, _perm):
    """Header presence is not authentication. `main.py`'s middleware checks
    only that the header EXISTS, so this is the assertion that separates a real
    guard from the appearance of one."""
    resp = _client().request(method, path, json={},
                             headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401, f"{method} {path} accepted a forged session"
    assert _detail(resp)["code"] == "SESSION_INVALID"


def test_a_role_without_connector_read_is_refused_at_the_floor(make_user):
    """`connector.read` is held by Administrator and Auditor only, so a
    Requestor authenticates and is then refused by the router's own floor --
    on the READ routes too, which is the half a per-route guard forgets.

    One identity, looped over every route, rather than one parametrised case
    per route. `make_user` costs two PBKDF2 derivations at 240,000 rounds each
    -- roughly 2.4 seconds -- so parametrising it over sixteen routes spent
    forty seconds proving one property. The route is still named in the
    failure message, which is what a parametrised id was buying.
    """
    caller = make_user(["Requestor"])
    client = _client()
    for method, path, _perm in ROUTES:
        resp = client.request(method, path, json={},
                              headers={"X-Session": caller.session_id})
        assert resp.status_code == 403, (
            f"{method} {path} answered {resp.status_code} to a role without "
            f"connector.read")
        assert _detail(resp)["code"] == "FORBIDDEN", f"{method} {path}"


def test_a_role_with_read_but_not_manage_is_refused_on_every_mutation(make_user):
    """Authenticated is not authorised, and cleared-the-floor is not authorised
    either. Auditor holds `connector.read`, so it gets past the router
    dependency; it does not hold `connector.manage`, so only a 403 can pass.
    A 401 here would mean the identity failed to authenticate and the test
    proved nothing about the route's own permission.
    """
    for _method, _path, permission in MUTATING:
        assert "Auditor" not in auth.PERMISSIONS[permission], (
            "Auditor now holds connector.manage; this test's premise is gone "
            "and test_aud_c_006_auditor_is_read_only should have failed first")
    caller = make_user(["Auditor"])
    client = _client()
    for method, path, _permission in MUTATING:
        resp = client.request(method, path, json={},
                              headers={"X-Session": caller.session_id})
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"
        assert _detail(resp)["code"] == "FORBIDDEN", f"{method} {path}"


def test_the_guard_is_declared_on_the_router_not_route_by_route():
    """Structural tripwire. A route added later inherits the router-level
    dependency automatically; if the guard is ever moved onto individual
    routes, the next route added without one ships open -- and every
    assertion above would still pass until that route exists."""
    assert integrations_api.router.dependencies, (
        "the integrations router carries no router-level dependency; the guard "
        "has been moved onto individual routes and a future route will ship open")


# ------------------------------------------------------------ mutation tests
def test_mutation_removing_the_router_dependency_lets_an_unauthenticated_caller_in():
    """Prove the guard is load-bearing by DELETING it.

    Without this, every assertion above could be passing because of the
    middleware, a stray import, or nothing at all. The router is rebuilt with
    no dependencies and the same unauthenticated request is made; if it is
    still refused, the refusal was never coming from the guard and the guard
    is decoration.
    """
    from fastapi import APIRouter

    # The SAME handler function, re-registered on a router with no
    # dependencies. Copying `router.routes` across does NOT work and the first
    # version of this test did exactly that and passed: `APIRouter.add_api_route`
    # merges the router's dependencies into each APIRoute when the decorator
    # runs, so a copied route carries the guard with it and the "mutation"
    # mutated nothing. A mutation test that cannot fail is worse than no
    # mutation test, because it certifies the thing it never exercised.
    naked = APIRouter()
    naked.get("/api/integrations/control-totals")(
        integrations_api.get_control_totals)
    app = FastAPI()
    app.include_router(naked)

    guarded = _client().get("/api/integrations/control-totals")
    assert guarded.status_code == 401, "baseline: the real router refuses"

    resp = TestClient(app, raise_server_exceptions=False).get(
        "/api/integrations/control-totals")
    assert resp.status_code != 401, (
        "removing the router-level dependency did NOT open the route, so the "
        "401 the guarded router returns is coming from somewhere else and "
        "these tests are not testing the guard")
    assert resp.status_code == 503, (
        f"expected the unguarded route to reach its handler and answer its own "
        f"503, got {resp.status_code}")


def test_mutation_weakening_the_route_permission_removes_the_refusal(make_user):
    """Prove `_requires('connector.manage')` is what refuses the Auditor.

    The same route is rebuilt with `connector.read` -- which the Auditor DOES
    hold -- and must stop answering 403. If it still refuses, the 403 above was
    coming from the floor rather than from the route's own permission, and the
    mutating routes would be no better guarded than the reads.
    """
    from fastapi import APIRouter, Depends

    caller = make_user(["Auditor"])

    weakened = APIRouter(dependencies=[Depends(integrations_api.require_integration_access)])

    @weakened.get("/probe", dependencies=[Depends(integrations_api._requires("connector.read"))])
    def _probe():
        return {"reached": True}

    strict = APIRouter(dependencies=[Depends(integrations_api.require_integration_access)])

    @strict.get("/probe", dependencies=[Depends(integrations_api._requires("connector.manage"))])
    def _probe_strict():
        return {"reached": True}

    weak_app, strict_app = FastAPI(), FastAPI()
    weak_app.include_router(weakened)
    strict_app.include_router(strict)
    headers = {"X-Session": caller.session_id}

    assert TestClient(strict_app, raise_server_exceptions=False).get(
        "/probe", headers=headers).status_code == 403
    assert TestClient(weak_app, raise_server_exceptions=False).get(
        "/probe", headers=headers).status_code == 200, (
        "an Auditor was refused even by a route requiring only connector.read, "
        "so the 403s above are not attributable to connector.manage")


def test_there_is_no_local_permission_table_and_no_fallback():
    """Source tripwire, and it is not hypothetical.

    A previous wave's router carried a local role->permission fallback that
    was consulted whenever the authoritative lookup "failed", and it FAILED
    OPEN: it granted a role `auth.PERMISSIONS` excluded, and it did so
    invisibly because the fallback only ran on the path nobody tested.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # No module-level mapping whose values look like role tuples.
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Dict):
                rendered = ast.dump(value)
                assert "Administrator" not in rendered and "Auditor" not in rendered, (
                    "a module-level dict in api/integrations.py names roles; "
                    "permissions come from auth.PERMISSIONS and nowhere else")

    assert "PERMISSIONS" not in source.replace("auth.PERMISSIONS", ""), (
        "this module references a PERMISSIONS table of its own")
    assert "auth_mod.require(" in source, (
        "the router must resolve permissions through auth.require")


def test_the_permissions_used_exist_in_the_authoritative_table():
    """Every permission string this module names must be one `auth` declares.

    `auth.require` raises `UNKNOWN_PERMISSION` as a 500 for anything else, so a
    typo would turn a guard into an outage -- which is the safe direction, and
    still worth catching here rather than in production.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    used = set(re.findall(r"_requires\(\"([^\"]+)\"\)", source))
    used |= set(re.findall(r"auth_mod\.require\(who, \"([^\"]+)\"\)", source))
    # `reconciliation.triage` joined the set when the unattributed-exception
    # triage routes landed. It is Administrator-only and is NOT folded into
    # `connector.manage`: managing a connector and reading the paise figures by
    # which another entity's books do not tie out are different grants, and one
    # permission covering both would hand every connector administrator the
    # second by implication.
    assert used == {"connector.read", "connector.manage",
                    "reconciliation.triage"}, (
        f"unexpected permission set {sorted(used)}")
    for permission in used:
        assert permission in auth.PERMISSIONS, (
            f"{permission} is not in auth.PERMISSIONS")


def test_the_actor_is_server_derived_and_ignores_a_caller_supplied_header():
    """`_actor` feeds `audit_log.actor` and `integration_event.actor`. A
    caller-supplied one would make every mutation unattributable."""

    class _State:
        pass

    class _Request:
        def __init__(self, state, headers=None):
            self.state, self.headers = state, headers or {}

    state = _State()
    state.integration_principal = {"user_id": "U-REAL-ACTOR"}
    request = _Request(state, {"X-User-Id": "U-IMPERSONATED"})
    assert integrations_api._actor(request) == "U-REAL-ACTOR"

    unauthenticated = _Request(_State(), {"X-User-Id": "U-IMPERSONATED"})
    assert integrations_api._actor(unauthenticated) == "UNKNOWN"


def test_this_router_never_constructs_a_whole_estate_scope():
    """`read_all` short-circuits `compile_scope` to TRUE before any dimension
    is examined. It comes from `user_access_flag` and nowhere else."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "read_all=True" not in source, (
        "this router must not construct a read_all scope of its own")
    assert "scope_for_request" in source, (
        "the router must resolve the caller's real grants (Contract 4)")


# ================================================ 3. the silences (503, coded)
@pytest.fixture()
def _no_database_needed(monkeypatch):
    """Let the four connection-scoped unavailable routes reach their refusal.

    Those four resolve the connection FIRST -- so a caller who cannot see the
    id gets 404 rather than a 503 that would confirm the id is real -- and that
    lookup needs a database this test has none of. The lookup is replaced with
    a visible connection so the route's own refusal is what gets asserted; the
    404-before-503 ordering is asserted separately at the source.
    """
    app = _app()
    app.dependency_overrides[integrations_api._get_database] = lambda: None
    monkeypatch.setattr(
        integrations_api, "_require_visible_connection",
        lambda request, database, connection_id: {
            "connection_id": connection_id, "entity_id": "ENT-1",
            "product": "ERP", "mode": "MOCK"})
    return app


_UNAVAILABLE_CALLS = [
    ("POST", "/api/integrations/connections/C1/authorize",
     "OAUTH_AUTHORISATION_UNAVAILABLE"),
    ("GET", "/api/integrations/connections/C1/organizations",
     "ORGANISATION_DISCOVERY_UNAVAILABLE"),
    ("PUT", "/api/integrations/connections/C1/organization",
     "ORGANISATION_MAPPING_UNAVAILABLE"),
    ("POST", "/api/integrations/connections/C1/validate",
     "SCOPE_VALIDATION_UNAVAILABLE"),
    ("GET", "/api/integrations/control-totals",
     "CONTROL_TOTALS_UNAVAILABLE"),
]


def test_every_unbacked_route_answers_a_coded_503_naming_what_is_missing(
        make_user, _no_database_needed):
    """503, never 404, and never a number.

    `core/api-client.js` collapses a 404 on a read into `notfound`, which the
    shared state host renders as "no records were found" -- a sentence that is
    a LIE on a route that does not exist. The difference between "the queue is
    empty" and "this build has no such endpoint" is the difference between an
    operator who can stop worrying and one who should not have.
    """
    caller = make_user(["Administrator"])
    client = TestClient(_no_database_needed, raise_server_exceptions=False)
    for method, path, code in _UNAVAILABLE_CALLS:
        body = {"organization_id": "60000000000"} if method == "PUT" else {}
        resp = client.request(method, path, json=body,
                              headers={"X-Session": caller.session_id})

        assert resp.status_code != 404, (
            f"{method} {path} answered 404, which the frontend reads as 'not "
            f"built' when the probe works and as 'no records found' when it "
            f"does not")
        assert resp.status_code == 503, (
            f"{method} {path} -> {resp.status_code}: {resp.text}")
        detail = _detail(resp)
        assert detail["code"] == code, f"{method} {path}"
        assert detail["status"] == 503, f"{method} {path}"
        assert detail["unavailable"] is True, f"{method} {path}"
        assert detail["missing"], (
            f"{method} {path}: the 503 must name what is missing, not just "
            f"refuse")
        assert detail["message_id"] is None, (
            f"{method} {path}: C10 declares no message for an absent "
            f"capability, so message_id must be null rather than a "
            f"plausible-looking MSG-INT id that resolves to nothing")


def test_control_totals_takes_no_database_dependency(make_user):
    """It refuses for a structural reason, not a transient one.

    If it declared `Depends(_get_database)` it would answer
    `DATABASE_NOT_CONFIGURED` on a process without a database -- a 503 with the
    WRONG code, which an operator would try to fix by restarting something,
    and which would hide the permanent reason behind a transient-looking one.

    THIS TEST USED TO COVER `/reconciliation` TOO, and no longer can, because
    that route is now backed by `013_procurement.sql` and genuinely does query.
    The property is not dropped, it is SPLIT: the test below asserts the other
    half -- that `/reconciliation` now reports the database as what is missing,
    which for a route that queries is the right code rather than the wrong one.
    Narrowing this one without adding that one would have deleted an assertion.
    """
    caller = make_user(["Administrator"])
    client = _client()                      # no database configured anywhere
    resp = client.get("/api/integrations/control-totals",
                      headers={"X-Session": caller.session_id})
    assert resp.status_code == 503
    assert _detail(resp)["code"] == "CONTROL_TOTALS_UNAVAILABLE", (
        f"/api/integrations/control-totals answered "
        f"{_detail(resp)['code']}; a route that never queries must not report "
        f"a database problem")


def test_reconciliation_reports_the_database_as_what_is_missing(make_user):
    """The other half of the split above.

    `/reconciliation` is backed as of 013 and takes `Depends(_get_database)`,
    so on a process with no PostgreSQL the honest answer is
    `DATABASE_NOT_CONFIGURED` -- a database really is what it lacks. It must
    NOT go back to `INTEGRATION_RECONCILIATION_UNAVAILABLE`, whose sentence
    ("PostgreSQL holds no purchase order, GRN or bill") is now false and would
    send an operator to look for a migration that has already landed.

    `unavailable` must still be True on the envelope: `integration-api.js`
    reads that flag to tell "this build cannot answer" from "something broke",
    and without it SCR-18 renders a red fault banner on a build that simply has
    no database configured.
    """
    caller = make_user(["Administrator"])
    resp = _client().get("/api/integrations/reconciliation",
                         headers={"X-Session": caller.session_id})
    assert resp.status_code == 503
    detail = _detail(resp)
    assert detail["code"] == "DATABASE_NOT_CONFIGURED", (
        f"/api/integrations/reconciliation answered {detail['code']}; it "
        f"queries now, so the missing thing is the database")
    assert detail["unavailable"] is True, (
        "without the unavailable envelope SCR-18 renders a red fault banner "
        "on a build that is simply not configured for PostgreSQL")


def test_reconciliation_never_claims_a_zoho_side_figure():
    """The line between this route and /control-totals, asserted at the source.

    /reconciliation may now answer, and everything it answers with is OURS:
    our purchase orders, our receipts, our bills, our outbox and our inbox. The
    moment it acquired real numbers it also acquired the way to become the
    dangerous one -- a count of ours presented as agreement with theirs. It
    carries a `source_note` saying so in the body, and this pins that.
    """
    source = inspect.getsource(integrations_api.get_reconciliation)
    assert "source_note" in source, (
        "get_reconciliation returns figures without stating whose they are")
    note = source[source.index("source_note"):]
    assert "No " in note and "Zoho" in note, (
        "the source note must say plainly that no figure here is Zoho's own")
    assert '"source": "wave5"' in source, (
        "the response must name itself wave5; SCR-18 renders the source and a "
        "result that did not name one would be data with no provenance")


def test_control_totals_never_returns_a_number(make_user):
    """The one that matters most.

    This database holds our inbox and our outbox and therefore everything
    needed to produce a confident-looking total -- and nothing at all about
    Zoho's side. A total computed from the ledger alone compares us with
    ourselves, and a comparison of a thing with itself ALWAYS BALANCES: it
    would render green permanently, including on the day Zoho silently stopped
    accepting our purchase orders.
    """
    caller = make_user(["Administrator"])
    resp = _client().get("/api/integrations/control-totals",
                         headers={"X-Session": caller.session_id})
    assert resp.status_code == 503

    def _numbers(node):
        if isinstance(node, bool):
            return []
        if isinstance(node, (int, float)):
            return [node]
        if isinstance(node, dict):
            return [n for k, v in node.items() if k != "status" for n in _numbers(v)]
        if isinstance(node, list):
            return [n for v in node for n in _numbers(v)]
        return []

    assert _numbers(resp.json()) == [], (
        f"the control-total refusal carries numbers: {resp.json()!r}; a screen "
        f"could render one of them as a total")

    detail = _detail(resp)
    blob = f"{detail['detail']} {detail['missing']} {detail['remedy']}".lower()
    assert "zoho" in blob, "the refusal must name whose side is missing"
    assert "balance" in blob or "itself" in blob, (
        "the refusal must say WHY no local number is offered")


def test_the_source_refuses_to_synthesise_a_control_total():
    """Source-level, because the assertion above can only see what a refusal
    returns. This one sees what the function could ever return: the handler
    must raise, never compute."""
    source = inspect.getsource(integrations_api.get_control_totals)
    assert "raise _unavailable(" in source
    assert "SUM(" not in source.upper() and "COUNT(" not in source.upper(), (
        "get_control_totals aggregates something; whatever it aggregates is "
        "our own side, and our own side compared with itself always balances")
    assert "return {" not in source, (
        "get_control_totals has a success return; there is no honest one")


def test_every_unavailable_route_is_accounted_for():
    """The map of silences must not drift from the router.

    A route that gains a real implementation should fail this test, so backing
    one is a visible decision rather than a quiet divergence.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    raised = set(re.findall(r'raise _unavailable\(\s*\n?\s*"([A-Z_]+)"', source))
    # SCOPE_INVENTORY_UNAVAILABLE is a degradation path inside a BACKED route,
    # not a route-level silence: /scopes serves a real inventory and answers
    # this only if another stream renames the constant it reads.
    raised.discard("SCOPE_INVENTORY_UNAVAILABLE")
    # DATABASE_NOT_CONFIGURED is the same shape: a DEPENDENCY-level degradation
    # reachable from every backed route when the process has no PostgreSQL, not
    # a statement that any one route has nothing behind it. It uses
    # `_unavailable` rather than a bare 503 because `integration-api.js` reads
    # `detail.unavailable` to tell "this build cannot answer" from "something
    # broke" -- without the envelope a build with no database rendered a red
    # HTTP 503 on twelve screens. Discarded here for the same reason as the
    # line above: this map is the inventory of ROUTES that cannot answer, and
    # adding a dependency failure to it would make "backed" stop meaning
    # anything.
    raised.discard("DATABASE_NOT_CONFIGURED")
    assert raised == set(UNAVAILABLE.values()), (
        f"the unavailable-route codes in the source {sorted(raised)} do not "
        f"match this test's map {sorted(set(UNAVAILABLE.values()))}")


# =================================================== 4. RFC 7807 and message ids
def test_every_error_body_is_rfc_7807_shaped_with_a_code_and_a_message_id(make_user):
    caller = make_user(["Administrator"])
    resp = _client().get("/api/integrations/control-totals",
                         headers={"X-Session": caller.session_id})
    detail = _detail(resp)
    for field in ("type", "title", "status", "code", "detail", "message_id"):
        assert field in detail, f"{field} missing from the problem body"
    assert detail["type"] == "about:blank"
    assert detail["status"] == resp.status_code


def test_a_message_id_is_only_ever_one_c10_declares():
    """C10 is FROZEN. An invented id is worse than none: the frontend looks it
    up, finds nothing, and renders an empty string where the explanation
    should be."""
    declared = {m["id"] for m in
                json.loads(C10_PATH.read_text(encoding="utf-8"))["messages"]}
    assert integrations_api.MESSAGE_IDS == declared

    for good in ("MSG-INT-001", "MSG-SEC-001"):
        assert integrations_api.message_id_or_none(good) == good

    # Mutation: ids that LOOK right are the dangerous ones, not obvious junk.
    for bad in ("MSG-INT-999", "MSG-INT-007", "MSG-CONN-001", "", None, "msg-int-001"):
        assert integrations_api.message_id_or_none(bad) is None, (
            f"{bad!r} was accepted as a message id but C10 does not declare it")


def test_message_id_validation_cannot_be_bypassed_by_the_problem_helper():
    """`_problem` routes its `message_id` through the validator, so a call site
    that spells an id optimistically cannot put it on the wire."""
    exc = integrations_api._problem(400, "X", "X", "d", message_id="MSG-INT-042")
    assert exc.detail["message_id"] is None
    ok = integrations_api._problem(400, "X", "X", "d", message_id="MSG-INT-003")
    assert ok.detail["message_id"] == "MSG-INT-003"


def test_an_unreadable_catalogue_emits_no_message_id_rather_than_an_unchecked_one(
        monkeypatch):
    """Fail closed. If C10 cannot be read, NO id can be validated, so none is
    emitted -- costing a hint. Failing open would put unvalidated ids on the
    wire, which is the defect this guards."""
    monkeypatch.setattr(integrations_api, "_C10_PATH", ROOT / "does-not-exist.json")
    assert integrations_api._load_message_ids() == frozenset()


def test_every_message_id_literal_in_the_source_is_declared_by_c10():
    """Catches an id hard-coded past the helper."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for candidate in set(re.findall(r'"(MSG-[A-Z]+-\d+)"', source)):
        assert candidate in integrations_api.MESSAGE_IDS, (
            f"{candidate} appears in api/integrations.py but C10 does not "
            f"declare it")


# ============================================================== 5. the scope
def _columns_mappings() -> dict[str, dict]:
    """Every `columns=` mapping this module defines, by name."""
    return {name: value for name, value in vars(integrations_api).items()
            if name.endswith("_COLUMNS") and isinstance(value, dict)}


def test_every_scope_mapping_names_all_four_dimensions():
    """Waiving by OMISSION is the defect; waiving by explicit `None` is a
    decision a reviewer can see and challenge.

    `PROJECT_SCOPE_COLUMNS` omitted `project` for a whole milestone, so a
    caller scoped to specific projects -- including one scoped to NO projects
    -- compiled to TRUE instead of a filter.
    """
    mappings = _columns_mappings()
    assert mappings, "no scope mappings found; this test has gone blind"
    for name, mapping in mappings.items():
        assert set(mapping) == {"entity", "plant", "location", "project"}, (
            f"{name} does not name all four dimensions: {sorted(mapping)}")


def test_every_scope_mapping_this_router_uses_matches_the_store_where_they_overlap():
    """Single source of truth, verified rather than assumed.

    These are local copies -- `integration_store.py` belongs to another stream
    and this router must not break because a private name there is renamed --
    so the copies are checked against the originals here instead.
    """
    from app.backend.pg import integration_store as store

    assert integrations_api.CONNECTION_COLUMNS == store.CONNECTION_SCOPE_COLUMNS
    assert integrations_api.VIA_CONNECTION_COLUMNS == store.VIA_CONNECTION_SCOPE_COLUMNS


def _repo_query_statements() -> list[str]:
    """The SQL SOURCE TEXT of every `repo.query` / `repo.query_one` call here.

    The source segment, not the evaluated constant parts. Every scoped query in
    this module reaches its token through `{_via_connection('x.connection_id')}`,
    an f-string INTERPOLATION -- so a scan that joined only the literal chunks
    would see no `{scope}` in any of them and would have to be loosened until
    it proved nothing. The first version of this test was loosened exactly that
    way. The source segment contains the interpolation as written, which is the
    thing under review.

    Scanning call sites rather than every string in the file also stops
    `_CONNECTION_SELECT` -- a deliberate FRAGMENT with no WHERE clause, which
    each caller completes with its own `{scope}` -- from having to be special
    cased, and a special case is where the next unscoped query would hide.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[str] = []

    for function in [n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)]:
        # A list route builds its SQL into a local first -- the WHERE clause is
        # assembled from optional filters -- and passes the NAME to repo.query.
        # Reading only the call site sees the identifier `statement` and
        # nothing else, which is what the first version of this test did: it
        # failed with `'{scope}' in 'statement'`, correctly, because it was
        # inspecting a variable name rather than the SQL.
        locals_: dict[str, str] = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                locals_[node.targets[0].id] = (
                    ast.get_source_segment(source, node.value) or "")

        for node in ast.walk(function):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("query", "query_one")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "repo"):
                continue
            if len(node.args) < 2:
                continue
            argument = node.args[1]
            if isinstance(argument, ast.Name):
                # Resolve the local, and resolve one level of composition
                # (`_CONNECTION_SELECT + f"WHERE ... {scope}"`) by including any
                # module constant it names.
                text = locals_.get(argument.id, "")
                for const in re.findall(r"\b_[A-Z_]+\b", text):
                    text += " " + str(getattr(integrations_api, const, ""))
            else:
                text = ast.get_source_segment(source, argument) or ""
            found.append(text)
    return found


def test_no_statement_carries_a_bare_percent_sign_anywhere_not_even_in_a_comment():
    """psycopg scans the WHOLE statement text for `%` placeholders -- SQL
    comments included -- and refuses anything but `%(name)s`, `%s`, `%b`,
    `%t` or the escaped `%%`. A `'#%'` inside a `--` comment in
    `_emission_state` passed every database-free test (the fakes never parse)
    and answered 500 on the first live reconciliation that had a local order
    to ask about (UAT, 2026-09-12). This holds the property at the source."""
    statements = _repo_query_statements()
    assert statements
    offenders = []
    for statement in statements:
        # `%%` is the escaped literal and is fine; strip the pairs first so
        # the second half of a pair is not read as a bare sign.
        text = statement.replace("%%", "")
        for m in re.finditer(r"%(?![(sbt])", text):
            offenders.append(text[max(0, m.start() - 40):m.start() + 10])
    assert offenders == [], offenders


def test_every_statement_this_module_runs_carries_a_scope_token():
    """`repo.query` refuses a statement without `{scope}` before it reaches the
    database, so this is belt and braces -- but it fails at BUILD time rather
    than on the one request that happens to hit an unscoped query.

    The `_via_connection` helper contributes the token for child tables, so a
    statement whose only token arrives through that helper counts as carrying
    one; the helper itself is asserted to emit `{scope}` separately below.
    """
    statements = _repo_query_statements()
    assert len(statements) >= 8, (
        f"only {len(statements)} repo.query call sites found; the scan has "
        f"gone blind and every assertion below is vacuous")
    for sql in statements:
        assert "{scope}" in sql or "_via_connection" in sql, (
            f"a statement reaches repo.query with no scope token:\n{sql}")


def test_the_via_connection_helper_puts_the_token_inside_the_filtering_clause():
    """A `{scope}` pasted somewhere harmless -- in a comment, or in a SELECT
    list -- satisfies `repo.require_scope_token` and filters nothing.

    The token must sit inside the EXISTS that actually restricts the rows.
    """
    clause = integrations_api._via_connection("i.connection_id")
    assert clause.startswith("EXISTS (")
    assert "{scope}" in clause
    where = clause[clause.upper().index(" WHERE "):]
    assert "{scope}" in where, (
        f"the token is outside the WHERE clause and filters nothing: {clause}")


def test_every_scoped_query_names_its_columns_mapping():
    """A `repo.query` without `columns=` falls back to the caller's default
    mapping, which is not this module's to assume."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("query", "query_one")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "repo"):
            continue
        keywords = {k.arg for k in node.keywords}
        assert "columns" in keywords, (
            "a repo.query call names no columns= mapping:\n"
            + (ast.get_source_segment(source, node) or "")[:400])


def test_an_out_of_scope_row_is_reported_as_absent_and_never_as_forbidden():
    """A 403 on an id is an EXISTENCE ORACLE: it confirms the id names a real
    row in an entity the caller cannot see.

    Asserted at the source, because proving it behaviourally needs two
    principals and a live server. Every id-addressed refusal in this module
    must be a 404, and the codes must not distinguish "no such row" from "not
    yours".
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    for match in re.finditer(r"_problem\(\s*(\d{3})\s*,\s*\"([A-Z_]+)\"", source):
        status, code = int(match.group(1)), match.group(2)
        if "NOT_FOUND" in code:
            assert status == 404, f"{code} answers {status}, not 404"
    # This assertion previously read `"_problem(403" not in
    # source.replace(" ", "")`. It had a hole: `.replace(" ", "")` strips
    # spaces but NOT newlines, so `_problem(\n            403, ...)` -- which
    # is how black-ish formatting wraps a long call -- did not contain the
    # substring and sailed straight through. A guard a line break defeats is
    # not a guard. Normalising all whitespace closes that.
    #
    # It is also narrowed, deliberately and with the reason stated, from "no
    # 403 anywhere" to "no 403 except the one reviewed case":
    #
    #   ENTITY_OUT_OF_SCOPE refuses an attribution whose TARGET ENTITY the
    #   caller named in the request BODY. It is not a permission refusal
    #   (auth.require has already run, at the router dependency) and it is not
    #   an existence oracle -- it discloses nothing about any row, only that
    #   the caller may not write to an entity they themselves supplied. The
    #   original ban was written when every refusal in this file was one of
    #   those two things, and it is kept for every other case.
    #
    # Recorded as ADAPT-INT-403 in tests/ADAPTATIONS.md.
    flat = re.sub(r"\s+", "", source)
    permitted_403 = '_problem(403,"ENTITY_OUT_OF_SCOPE"'
    assert flat.count("_problem(403") == flat.count(permitted_403) == 1, (
        "this router raises a 403 of its own beyond the one reviewed case "
        "(ENTITY_OUT_OF_SCOPE on an attribution's named target entity). "
        "Permission refusals belong to auth.require, and a 403 on an id "
        "would be an existence oracle")
    assert "{exception_id}" not in _out_of_scope_message(source), (
        "the out-of-scope refusal names a path parameter, which turns a "
        "statement about the caller's own grants into one about a row")
    assert "does not exist or is out of scope" not in source, (
        "a refusal message that spells out BOTH cases still distinguishes them "
        "for anyone reading carefully; say only that it is not visible")


def _out_of_scope_message(source: str) -> str:
    """The prose of the ENTITY_OUT_OF_SCOPE refusal, for the assertion above."""
    start = source.find('"ENTITY_OUT_OF_SCOPE"')
    assert start != -1, "ENTITY_OUT_OF_SCOPE is no longer raised"
    return source[start:start + 800]


# ========================================================= 6. lists and headers
_LIST_ROUTES = [
    "/api/integrations/connections", "/api/integrations/events",
    "/api/integrations/inbox", "/api/integrations/outbox",
    "/api/integrations/dead-letters", "/api/integrations/exceptions",
    "/api/integrations/exceptions/unattributed",
]


@pytest.mark.parametrize("path", _LIST_ROUTES)
def test_every_list_route_is_cursor_paginated(path):
    """From the OpenAPI schema, which is generated from the signatures."""
    schema = _app().openapi()
    params = {p["name"] for p in schema["paths"][path]["get"].get("parameters", [])}
    assert {"cursor", "limit"} <= params, (
        f"{path} is a list route without cursor pagination; it declares "
        f"{sorted(params)}")


def test_a_malformed_cursor_is_the_callers_problem_not_a_500():
    with pytest.raises(Exception) as raised:
        integrations_api._decode_cursor("not-base64!!", 2)
    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "INVALID_CURSOR"


def test_a_cursor_from_another_collection_is_refused():
    """Arity is checked, so a two-part cursor cannot be replayed against a
    one-part collection and silently filter on the wrong column."""
    two = integrations_api._encode_cursor(["ENT-1", "CONN-1"])
    assert integrations_api._decode_cursor(two, 2) == ["ENT-1", "CONN-1"]
    with pytest.raises(Exception) as raised:
        integrations_api._decode_cursor(two, 1)
    assert raised.value.detail["code"] == "INVALID_CURSOR"


def test_a_cursor_round_trips_and_is_opaque():
    values = ["2026-09-07T00:00:00+00:00", "INB-1"]
    cursor = integrations_api._encode_cursor(values)
    assert integrations_api._decode_cursor(cursor, 2) == values
    assert "/" not in cursor and "+" not in cursor, "cursor must be URL-safe"


def test_the_page_helper_over_fetches_and_never_returns_the_extra_row():
    """`limit + 1` is fetched; the extra row is the ONLY honest way to say
    whether a next page exists, and it must never reach the caller."""
    rows = [("a", 1), ("b", 2), ("c", 3)]
    kept, cursor = integrations_api._page(rows, 2, key=(0,))
    assert kept == rows[:2] and cursor is not None
    assert integrations_api._decode_cursor(cursor, 1) == ["b"], (
        "the cursor must name the LAST RETURNED row, not the over-fetched one; "
        "naming the extra row would skip it on the next page")

    kept, cursor = integrations_api._page(rows[:2], 2, key=(0,))
    assert kept == rows[:2] and cursor is None, "a short page has no next cursor"


def test_the_limit_is_clamped_at_both_ends():
    assert integrations_api._limit(0) == 1
    assert integrations_api._limit(-5) == 1
    assert integrations_api._limit(10_000) == integrations_api._MAX_LIMIT


def test_a_route_that_returns_normally_sets_the_correlation_header_itself(
        make_user, _no_database_needed):
    """The router does not rely on the middleware for the success path.

    `/scopes` is the one backed route that reaches a 200 without a database:
    it resolves the connection (stubbed here) and then reads a static scope
    inventory out of the adapter package.
    """
    caller = make_user(["Administrator"])
    resp = TestClient(_no_database_needed, raise_server_exceptions=False).get(
        "/api/integrations/connections/C1/scopes",
        headers={"X-Session": caller.session_id,
                 "X-Correlation-Id": "cid-under-test"})
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Correlation-Id") == "cid-under-test", (
        "the route did not echo the caller's correlation id")


def test_every_route_sets_the_correlation_header_before_it_can_refuse():
    """Source-level, and the reason is worth stating.

    On a REFUSAL the header this router writes onto its `Response` is
    discarded: FastAPI builds a fresh response from the raised
    `HTTPException`, and it is `main.py`'s middleware -- through `_finalise`,
    which every exit path including the 401 early return goes through -- that
    puts `X-Correlation-Id` back on. So the refusals genuinely do carry one in
    the mounted application, and asserting that HERE, against a bare router
    with no middleware, would assert someone else's code and fail.

    What IS this router's own is that every handler resolves and sets the id as
    its first act, so the success path needs no middleware and the id used for
    an audit row is the same one the caller is given. That is what this checks.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "router"
                for d in node.decorator_list)
    }
    # 16 at first delivery; 18 with the two unattributed-exception triage
    # routes. The count is asserted rather than merely iterated so a handler
    # added without the header is caught even if it is also added to some
    # other allow-list -- the loop below only checks the handlers it finds.
    # 16 at first delivery; 18 with the unattributed-exception triage list and
    # attribution; 19 with the resolve verb that closes one; 20 with the
    # dead-letter discard that Wave 6 added. The count is asserted rather than
    # merely iterated so a handler added without the header is caught even if
    # it is also added to some other allow-list -- the loop below only checks
    # the handlers it finds.
    assert len(handlers) == 20, (
        f"expected 20 route handlers, found {len(handlers)}: {sorted(handlers)}")
    for name in sorted(handlers):
        body = inspect.getsource(getattr(integrations_api, name))
        assert "_set_correlation_header(response, request)" in body, (
            f"{name} does not set the correlation header")


def test_the_correlation_id_returned_is_the_one_recorded(monkeypatch):
    """`_correlation_id` reads the id the middleware BOUND for this request,
    not a fresh uuid.

    Minting one here would tell the caller the middleware's id in the header
    -- which wins, because `_finalise` overwrites -- and record a different one
    in `audit_log`, leaving the correlation unfollowable in exactly the case it
    exists for.
    """
    from app.backend import observability

    class _Request:
        headers = {"X-Correlation-Id": "from-the-header"}

    with observability.correlation("bound-by-middleware"):
        assert integrations_api._correlation_id(_Request()) == "bound-by-middleware"

    # With nothing bound, the caller's own header is honoured before a new id.
    assert integrations_api._correlation_id(_Request()) == "from-the-header"


def test_no_statement_may_outlive_the_thirty_second_ceiling():
    """Enforced by PostgreSQL, inside the transaction.

    `SET LOCAL` outside a transaction is discarded silently, so the timeout
    must be applied to the session the route then uses -- which is why `_Txn`
    exists rather than a helper that sets it and hands back a connection.
    """
    assert integrations_api._STATEMENT_TIMEOUT == "30s"
    source = inspect.getsource(integrations_api._Txn)
    assert "SET LOCAL statement_timeout" in source
    assert "__enter__" in source, (
        "the timeout must be applied inside the transaction the caller uses")


# ================================================ 7. the handoff to the lead
#: The EXACT entries the lead must add to `tests/test_api_auth.py`, in the same
#: commit as the `include_router` line. Held here so the handoff is verified by
#: a test rather than by a paragraph in a report:
#: `test_aud_c_006_every_mutating_route_is_covered_by_the_authorisation_matrix`
#: fails the instant the router lands without them.
DELIVERED_MUTATING_ROUTES = [
    ("/api/integrations/connections", "POST", "/api/integrations/connections",
     {"entity_id": "ENT-DM-01", "product": "ERP", "dc": "IN",
      "connector_name": "capex", "organization_id": "60000000000"},
     "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/authorize", "POST",
     "/api/integrations/connections/CONN-01/authorize", {},
     "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/organization", "PUT",
     "/api/integrations/connections/CONN-01/organization",
     {"organization_id": "60000000000"}, "connector.manage", "Auditor"),
    ("/api/integrations/connections/{connection_id}/validate", "POST",
     "/api/integrations/connections/CONN-01/validate", {},
     "connector.manage", "Auditor"),
    ("/api/integrations/dead-letters/{queue}/{row_id}/retry", "POST",
     "/api/integrations/dead-letters/outbox/OBX-01/retry", {},
     "connector.manage", "Auditor"),
    # Discarding a dead-lettered inbox payload -- the operator verb that
    # ends a payload's life instead of re-arming it. The SAME permission as
    # retry, deliberately: a role that can re-arm something failing must not
    # be unable to stop something that will never succeed.
    ("/api/integrations/dead-letters/{queue}/{row_id}/discard", "POST",
     "/api/integrations/dead-letters/inbox/IBX-01/discard",
     {"reason": "unauthorised attempt"},
     "connector.manage", "Auditor"),
    # Attributing an unattributed exception. `reconciliation.triage` is
    # Administrator-only, so Auditor is the denied role here for the same
    # reason as every row above -- it holds `connector.read` and clears the
    # ROUTER's floor, then stops at the route's own permission.
    ("/api/integrations/exceptions/{exception_id}/attribute", "POST",
     "/api/integrations/exceptions/RX-1/attribute",
     {"entity_id": "ENT-DM-01", "reason": "unauthorised attempt"},
     "reconciliation.triage", "Auditor"),
    # Closing an exception releases the capitalisation and period-close gate,
    # so it carries the same Administrator-only permission as attributing one.
    ("/api/integrations/exceptions/{exception_id}/resolve", "POST",
     "/api/integrations/exceptions/RX-1/resolve",
     {"status": "Resolved", "note": "unauthorised attempt"},
     "reconciliation.triage", "Auditor"),
]


def test_the_delivered_matrix_entries_cover_every_mutating_path_exactly():
    """The assertion that makes the handoff safe.

    `_mutating_paths` reads the OpenAPI schema, so this compares what the lead
    will mount against what the lead must paste. A mismatch here is the same
    failure `test_aud_c_006_every_mutating_route_is_covered_by_the_
    authorisation_matrix` would report after the mount -- found now, in the
    stream that owns the router, instead of in the lead's merge.
    """
    from tests.test_api_auth import _mutating_paths

    live = _mutating_paths(_app())
    covered = {entry[0] for entry in DELIVERED_MUTATING_ROUTES}
    assert live == covered, (
        f"uncovered: {sorted(live - covered)}; stale: {sorted(covered - live)}")


def test_every_delivered_matrix_entry_actually_refuses_its_denied_role(make_user):
    """The entries are not merely well-shaped; each one's denied role really is
    refused with the 403 that `test_api_auth.py` will assert.

    A denied role that turned out to HOLD the permission would make the matrix
    entry pass vacuously -- it would be asserting that a caller who is allowed
    to do something is refused, which cannot happen, so the entry would have to
    be wrong in the other direction to be noticed.
    """
    denied_roles = {e[5] for e in DELIVERED_MUTATING_ROUTES}
    assert denied_roles == {"Auditor"}, (
        f"the delivered entries now name more than one denied role "
        f"{sorted(denied_roles)}; this test creates only Auditor")

    for _t, _m, _u, _b, permission, denied_role in DELIVERED_MUTATING_ROUTES:
        assert denied_role not in auth.PERMISSIONS[permission], (
            f"{denied_role} holds {permission}; the matrix entry is wrong")

    caller = make_user(["Auditor"])
    client = _client()
    for _template, method, url, body, _permission, _denied in DELIVERED_MUTATING_ROUTES:
        resp = client.request(method, url, json=body,
                              headers={"X-Session": caller.session_id})
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"
        assert _detail(resp)["code"] == "FORBIDDEN", f"{method} {url}"


def test_a_caller_with_no_role_at_all_can_mutate_nothing(make_user):
    """Mirrors `test_aud_c_006_a_caller_with_no_role_at_all_can_mutate_nothing`
    for this router, before the router reaches that suite."""
    caller = make_user([])
    for _template, method, url, body, _perm, _denied in DELIVERED_MUTATING_ROUTES:
        resp = _client().request(method, url, json=body,
                                 headers={"X-Session": caller.session_id})
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"


def test_the_mount_line_the_lead_needs_is_the_ordinary_one():
    """`main.py` mounts this router with no prefix: every route carries its own
    full `/api/integrations/...` path, exactly as the budget router does."""
    for route in integrations_api.router.routes:
        assert route.path.startswith("/api/integrations/"), (
            f"{route.path} would need a prefix at mount time; every route must "
            f"carry its full path so `include_router(router)` is sufficient")


# ======================================================== 8. secrets (REQ-INT-024)
def test_no_route_accepts_or_returns_a_client_secret():
    """REQ-INT-024: the secret is installed server-side and no API accepts,
    returns or logs one.

    The creation model is checked directly rather than by grepping: a field
    named anything secret-shaped would be a place for one to ARRIVE, and
    `extra='forbid'` means one cannot arrive unnamed either.
    """
    fields = set(integrations_api._ConnectionIn.model_fields)
    assert not any("secret" in f or "token" in f or "password" in f
                   for f in fields), f"secret-shaped field on the creation model: {fields}"
    assert integrations_api._ConnectionIn.model_config["extra"] == "forbid", (
        "an unnamed extra field could carry a secret into the request body")

    # No SQL this module runs may SELECT or write a secret-shaped column.
    # Asserted over the executable SQL rather than over the whole file: the
    # first version of this test grepped the source and failed on the module's
    # own docstring explaining that there is no such column, which is exactly
    # the kind of assertion that gets deleted rather than fixed.
    for sql in _repo_query_statements():
        for forbidden in ("client_secret", "refresh_token", "access_token",
                          "password", "private_key"):
            assert forbidden not in sql, (
                f"a statement names {forbidden}:\n{sql}")


def test_no_response_builder_emits_a_secret_bearing_key():
    """The keys this router puts on the wire, checked at their source.

    `client_secret_present` is the one permitted mention and it carries no
    secret -- it is the flag SCR-32 needs in order to render whether a secret
    has been installed server-side, and it is on the frontend's own
    `SECRET_METADATA` allow-list.
    """
    row = integrations_api._connection_row(
        ("C1", "E1", "ERP", "IN", "60000", "capex", "MOCK", 100, 2000, True,
         None, None, 1))
    for key in row:
        assert key == "client_secret_present" or "secret" not in key, key
        assert "token" not in key and "password" not in key, key


def test_the_creation_model_rejects_an_unexpected_field(make_user,
                                                        _no_database_needed):
    """`extra='forbid'` proven behaviourally, not just read off the config.

    Runs against the app whose database dependency is overridden: with no
    database, `_get_database` refuses with 503 during dependency solving and
    the body is never validated, so the 422 this asserts could never appear
    and the test would prove nothing about the model.
    """
    caller = make_user(["Administrator"])
    resp = TestClient(_no_database_needed, raise_server_exceptions=False).post(
        "/api/integrations/connections",
        json={"entity_id": "E", "product": "ERP", "dc": "IN",
              "connector_name": "c", "organization_id": "1",
              "client_secret": "shhh"},
        headers={"X-Session": caller.session_id})
    assert resp.status_code == 422, (
        f"a body carrying client_secret was not rejected: {resp.status_code}")
