"""The negative authorization and row-scope matrix for the approval API.

Two halves, and the split is deliberate.

**The refusal matrix runs everywhere, with no database.** Every frozen refusal
in Contract 3 is driven through the real router -- real guard, real
dependencies, real response rendering -- with the engine replaced at its single
seam (`approvals._call_engine`). That is the only way to assert
"`SELF_APPROVAL` is a 403 and `STAGE_NOT_OPEN` is a 409" on a developer machine
and in every CI job, rather than only on the one job that has PostgreSQL. A
status map is a contract with the frontend; it should not be provable solely
where a database happens to be running.

**The row-scope matrix needs real rows and is gated on `CAPEX_DB_URL`.** What
protects an approval instance from a caller outside its entity/plant/project/
location is a predicate compiled into SQL and a row-level security policy --
neither of which a fake can evidence. Those tests skip locally (there is no
PostgreSQL on this stream's machine; CI is the first place they run for real)
and are written to FAIL, loudly, rather than skip, if the approval schema is
absent once a database IS configured.

Ownership note: `app/backend/pg/approvals.py`, `approval_rules.py` and
`delegation.py` belong to stream 2 and `migrations/pg/008_approval_engine.sql`
to stream 1. Neither is present in this stream's worktree. The fake below is
therefore written against the *frozen contract*, not against an implementation
this stream has read -- see the accompanying report for every interface
assumption it encodes.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# Without this the live tests below fail at SETUP with "fixture 'pg_database'
# not found" -- but ONLY where CAPEX_DB_URL is set. Locally they skip, so the
# missing fixture is never resolved and the gap is invisible. CI is the first
# place these run for real, which is the whole point of that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import contextlib  # noqa: E402
import os  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.backend.api import approvals as approvals_api  # noqa: E402
from app.backend.pg import principal_scope, repo  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

#: `pg` IS registered in `pytest.ini`'s `markers =` list at this baseline, so
#: these tests carry the real marker and can be selected or deselected with
#: `-m pg`. (Several older files note that it was not registered and use a
#: bare `skipif` stand-in; that note is stale, and copying it forward would
#: have quietly given up the ability to select these tests as a group.)
#:
#: The marker alone does not skip anything -- it only labels -- so the
#: `skipif` is what actually gates, and it names the reason so `-rs` reports
#: it rather than a silent dot.
_NEEDS_DATABASE = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


def PG(test):
    """Label a test as needing PostgreSQL, and gate it on `CAPEX_DB_URL`."""
    return pytest.mark.pg(_NEEDS_DATABASE(test))

#: The four scope dimensions, and the value pair each is tested with. `A` is
#: what the principal is granted; `B` is the neighbouring value it must never
#: see.
DIMENSIONS: list[tuple[str, str, str, str]] = [
    # scope field,     column,        granted A,        forbidden B
    ("entity_ids", "entity", "ENT-A", "ENT-B"),
    ("plant_ids", "plant", "PL-A", "PL-B"),
    ("project_ids", "project", "PRJ-A", "PRJ-B"),
    ("location_ids", "location", "LOC-A", "LOC-B"),
]

#: `approval_instance` carries `entity_id` and `project_id` denormalised
#: (Contract 1); plant and location reach it through the project. Contract 6
#: requires all four dimensions mapped, so the join is part of the contract,
#: not an optimisation.
INSTANCE_SCOPE_COLUMNS: dict[str, str] = {
    "entity": "i.entity_id",
    "plant": "p.plant_id",
    "project": "i.project_id",
    "location": "p.location_id",
}


def _scope_granting(field: str, value: str) -> Scope:
    """A principal restricted to exactly one value on exactly one dimension.

    Every OTHER dimension is `None` -- explicitly unrestricted -- so the test
    isolates the dimension under test. An empty frozenset elsewhere would
    compile to FALSE and every assertion below would pass without the
    dimension under test doing any work at all.
    """
    fields = {name: None for name in principal_scope.SCOPE_DIMENSION_FIELDS}
    fields[field] = frozenset({value})
    return Scope(user_id="U-RESTRICTED", principal_kind="USER",
                 read_all=False, **fields)


# ===========================================================================
# The fake engine -- one seam, driven through the real router
# ===========================================================================
class _EngineRefusal(Exception):
    """What stream 2's engine raises, in the shape the contract implies.

    `code` is the frozen Contract 3 code; `message` is human text. Nothing
    else is assumed -- in particular no status, so the router's own map is
    what is under test rather than a status the fake handed it.
    """

    def __init__(self, code: str, message: str = "refused",
                 outcome: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if outcome is not None:
            self.outcome = outcome


class _FakeSession:
    """Stands in for `pg.engine.Session`. The fake engine never queries."""


class _FakeDatabase:
    """Records the scope every request opens its session with."""

    def __init__(self):
        self.opened_with: list[Scope] = []

    @contextlib.contextmanager
    def session(self, scope):
        self.opened_with.append(scope)
        yield _FakeSession()


class _Harness:
    """The router, served, with the engine and the scope resolver replaced."""

    def __init__(self):
        self.database = _FakeDatabase()
        self.calls: list[dict] = []
        self.behaviour = lambda call: {"ok": True}
        self.scope = _scope_granting("entity_ids", "ENT-A")
        self.scope_calls: list[str] = []

    def engine(self, key, names, *args, request=None, **kwargs):
        call = {"key": key, "names": tuple(names), "kwargs": dict(kwargs)}
        self.calls.append(call)
        return self.behaviour(call)

    def resolve_scope(self, request, database):
        self.scope_calls.append(request.url.path)
        return self.scope

    @property
    def last(self) -> dict:
        assert self.calls, "the engine was never reached"
        return self.calls[-1]


@pytest.fixture()
def harness(monkeypatch):
    h = _Harness()
    monkeypatch.setattr(approvals_api, "_call_engine", h.engine)
    monkeypatch.setattr(approvals_api, "_scope_for", h.resolve_scope)
    return h


@pytest.fixture()
def acting_client(capex_db, harness, make_user):
    """An authenticated caller holding `approval.act`, against the real router.

    `FinanceApprover` holds `approval.read` (the router floor) and
    `approval.act`, so every refusal observed through this client is the
    ENGINE's refusal -- not the permission guard's, which is asserted
    separately in `test_approvals_api_guard.py`.
    """
    app = FastAPI()
    app.include_router(approvals_api.router)
    app.dependency_overrides[approvals_api._get_database] = \
        lambda: harness.database
    client = TestClient(app, raise_server_exceptions=False)
    caller = make_user(["FinanceApprover"])

    class _Caller:
        user_id = caller.user_id
        session_id = caller.session_id

        def request(self, method, path, **kw):
            headers = {"X-Session": caller.session_id}
            headers.update(kw.pop("headers", None) or {})
            return client.request(method, path, headers=headers, **kw)

        def decide(self, instance_id="A1", **overrides):
            body = {"action": "APPROVE", "idempotency_key": "idem-1",
                    "object_version": 3}
            body.update(overrides)
            return self.request("POST", f"/api/approvals/{instance_id}/decide",
                                json=body)

    return _Caller()


# ===========================================================================
# Every frozen refusal renders as the agreed status
# ===========================================================================
@pytest.mark.parametrize("code,status", [
    ("SELF_APPROVAL", 403),
    ("NOT_AN_ASSIGNEE", 403),
    ("STAGE_NOT_OPEN", 409),
    ("OBJECT_VERSION_STALE", 409),
    ("BUDGET_MOVED", 409),
    ("REASON_REQUIRED", 422),
    ("IDEMPOTENCY_KEY_REQUIRED", 422),
])
def test_each_engine_refusal_renders_as_its_agreed_status(harness, acting_client,
                                                          code, status):
    """Driven through the real router, not asserted against a lookup table.

    The distinction matters: a status map that is correct but never consulted
    protects nothing, and the way this router could fail is by catching the
    engine's exception somewhere that flattens it -- into a 500, or into a
    generic 400 that discards `code`.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        _EngineRefusal(code, f"{code} from the engine"))

    resp = acting_client.decide()
    assert resp.status_code == status, (
        f"{code} rendered as {resp.status_code}, expected {status}: {resp.text}")

    body = resp.json()["detail"]
    assert body["code"] == code, f"the frozen code was lost: {body!r}"
    assert body["status"] == status, "the RFC-7807 body contradicts the status"
    assert body["type"] == "about:blank"
    assert resp.headers.get("X-Correlation-Id"), "refusal carried no correlation id"


def test_a_self_approval_by_the_maker_is_a_403_and_says_so(harness, acting_client):
    """Contract 5's second enforcement point, seen from the outside.

    The maker-checker decision is the engine's, taken inside the transaction
    against `contributor_set(object)`. What this router owes it is a 403 --
    not a 409, which would invite the client to retry, and not a 422, which
    would suggest the request could be fixed by editing it. Neither is true:
    this caller may never approve this instance.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        _EngineRefusal("SELF_APPROVAL",
                       "You raised this revision and cannot also approve it."))

    resp = acting_client.decide()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "SELF_APPROVAL"

    # And the identity the engine compared is the SESSION's, not anything the
    # request offered -- which is the whole basis on which the refusal stands.
    assert harness.last["kwargs"]["actor"] == acting_client.user_id


def test_a_delegation_cannot_launder_a_self_approval(harness, acting_client):
    """Contract 5: a delegation is refused if EITHER identity is a contributor.

    The router cannot make that check -- it has no rows -- but it must not
    make it impossible either. Both identities have to survive the trip, so
    the actor it passes is the session's under every request shape, including
    one whose body tries to supply an `acting_for_user_id` of its own.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        _EngineRefusal("SELF_APPROVAL", "delegate is also a contributor"))

    forged = acting_client.decide(
        **{"reason_text": "approving on behalf of the requestor"})
    assert forged.status_code == 403
    assert harness.last["kwargs"]["actor"] == acting_client.user_id

    # A body naming the second identity is refused outright by `extra="forbid"`
    # rather than being quietly dropped: a caller must never be able to believe
    # it set `acting_for_user_id`.
    named = acting_client.request(
        "POST", "/api/approvals/A1/decide",
        json={"action": "APPROVE", "idempotency_key": "k", "object_version": 1,
              "acting_for_user_id": "U-SOMEONE-ELSE"})
    assert named.status_code == 422, (
        f"a body naming acting_for_user_id was accepted: {named.text}")


def test_an_idempotent_replay_is_a_200_carrying_the_original_outcome(
        harness, acting_client):
    """Contract 8. A replay returns the ORIGINAL outcome and never applies twice.

    Rendered as a 4xx it would push well-behaved clients into retrying -- the
    exact behaviour idempotency exists to make safe -- and a client that
    treated the replay as a failure would report an approval as not taken when
    it had been.
    """
    original = {"instance_id": "A1", "status": "APPROVED",
                "decided_by": "U-FIN", "stage_no": 2}
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        _EngineRefusal("IDEMPOTENT_REPLAY", "already applied", outcome=original))

    resp = acting_client.decide()
    assert resp.status_code == 200, (
        f"a replay rendered as {resp.status_code}: {resp.text}")
    body = resp.json()
    assert body["code"] == "IDEMPOTENT_REPLAY"
    assert body["replayed"] is True
    for key, value in original.items():
        assert body[key] == value, (
            f"the original outcome was not returned: {key} was {body.get(key)!r}")


def test_a_replay_signalled_as_a_RESULT_is_also_a_200(harness, acting_client):
    """The same contract, the other signalling shape.

    Contract 8 fixes the observable behaviour without fixing whether the
    engine raises or returns, and this stream cannot see the engine. Both are
    honoured so integration cannot turn a correct engine into a wrong status.
    """
    harness.behaviour = lambda call: {
        "code": "IDEMPOTENT_REPLAY", "instance_id": "A1", "status": "APPROVED"}
    resp = acting_client.decide()
    assert resp.status_code == 200
    assert resp.json()["code"] == "IDEMPOTENT_REPLAY"


def test_the_required_decision_fields_are_refused_with_their_frozen_codes(
        acting_client):
    """`idempotency_key` and `object_version` are required by Contract 3.

    Enforced in the handler rather than by Pydantic's own required-field
    machinery, because Pydantic's 422 body has no `code` field at all -- and
    "RFC-7807 errors carrying `code`" has to hold for the required-field case
    most of all, since it is the one a client hits first.
    """
    missing_key = acting_client.request(
        "POST", "/api/approvals/A1/decide",
        json={"action": "APPROVE", "object_version": 1})
    assert missing_key.status_code == 422
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    blank_key = acting_client.decide(idempotency_key="   ")
    assert blank_key.status_code == 422
    assert blank_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_version = acting_client.request(
        "POST", "/api/approvals/A1/decide",
        json={"action": "APPROVE", "idempotency_key": "k"})
    assert missing_version.status_code == 422
    assert missing_version.json()["detail"]["code"], "no code on the refusal"

    unknown_action = acting_client.decide(action="RUBBER-STAMP")
    assert unknown_action.status_code == 422
    assert unknown_action.json()["detail"]["code"] == "INVALID_ACTION"


# ===========================================================================
# Database refusals are 4xx, never an unhandled 500
# ===========================================================================
@pytest.mark.parametrize("error,status,code", [
    (psycopg.errors.UniqueViolation, 409, "DUPLICATE"),
    (psycopg.errors.ForeignKeyViolation, 422, "INVALID_REFERENCE"),
    (psycopg.errors.CheckViolation, 422, "INVALID_FIELD"),
    (psycopg.errors.InsufficientPrivilege, 403, "FORBIDDEN"),
])
def test_a_database_refusal_is_a_4xx_not_an_unhandled_500(harness, acting_client,
                                                          error, status, code):
    """`api/budget.py` has no psycopg handler at all, so a constraint
    violation there is an unhandled 500 with a traceback. That omission is not
    copied here.

    `InsufficientPrivilege` (SQLSTATE 42501) is row-level security refusing
    the statement -- the deepest enforcement point in the system, and the one
    reached only when something above it has a gap. It is an authorisation
    answer, so it is a 403, and it must never be a 500: a 500 is how a working
    control gets mistaken for a broken server and then disabled.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(error("boom"))

    resp = acting_client.decide()
    assert resp.status_code == status, (
        f"{error.__name__} rendered as {resp.status_code}: {resp.text}")
    assert resp.json()["detail"]["code"] == code
    assert resp.headers.get("X-Correlation-Id")


def test_a_database_refusal_never_echoes_the_sql_or_the_row(harness,
                                                            acting_client):
    """A driver message can carry the failing statement and its values.

    On this surface those values are financial and identifying, so the
    response says which CLASS of constraint refused and nothing more.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        psycopg.errors.UniqueViolation(
            'duplicate key value violates unique constraint '
            '"approval_action_pkey" DETAIL: Key (actor_user_id)=(U-SECRET-USER) '
            'already exists.'))

    resp = acting_client.decide()
    assert resp.status_code == 409
    assert "U-SECRET-USER" not in resp.text, "the refusal echoed row data"
    assert "DETAIL" not in resp.text.upper() or "detail" in resp.json(), \
        "the driver's DETAIL clause reached the client"


def test_a_genuine_engine_bug_is_still_a_500(harness, acting_client):
    """The other half of "handle database errors as 4xx".

    An exception with no `code` and no SQLSTATE is a defect, not a refusal.
    Flattening it into a plausible 4xx would hide real faults behind a
    convincing business message -- and a client would retry forever against a
    broken engine.
    """
    harness.behaviour = lambda call: (_ for _ in ()).throw(
        RuntimeError("engine exploded"))
    resp = acting_client.decide()
    assert resp.status_code == 500, (
        f"a genuine bug was rendered as {resp.status_code}, which hides it")


# ===========================================================================
# What the router hands the engine
# ===========================================================================
def test_every_route_opens_its_session_with_the_resolved_scope(harness,
                                                                acting_client):
    """Contract 6's single scope-construction path, asserted per route.

    The refusal tests above replace `_scope_for`, which is exactly the
    substitution that would hide a route that never called it. This counts the
    calls instead: one per request that reaches a handler, on every route.
    """
    harness.behaviour = lambda call: {"items": []}
    probes = [
        ("GET", "/api/approvals/inbox", None),
        ("GET", "/api/approvals/sla", None),
        ("GET", "/api/approvals/A1", None),
        ("GET", "/api/approvals/A1/timeline", None),
        ("POST", "/api/approvals/A1/recall", {"reason_text": "changed my mind"}),
        ("POST", "/api/approvals/A1/cancel", {"reason_text": "withdrawn"}),
        ("POST", "/api/approvals/A1/resubmit", {"reason_text": "corrected"}),
    ]
    for method, path, body in probes:
        before = len(harness.scope_calls)
        resp = acting_client.request(method, path,
                                     **({"json": body} if body is not None else {}))
        assert resp.status_code < 500, f"{method} {path}: {resp.text}"
        assert len(harness.scope_calls) == before + 1, (
            f"{method} {path} did not resolve the caller's scope")

    # And the session was opened with the scope that was resolved, not with
    # one the handler invented afterwards.
    assert harness.database.opened_with, "no session was opened"
    assert all(s is harness.scope for s in harness.database.opened_with), (
        "a handler opened its session with a scope other than the resolved one")


def test_the_inbox_is_limited_to_the_callers_own_assignments(harness, capex_db,
                                                              make_user):
    """Contract 6, second half: scope AND addressed to them.

    `approval.read` is held by EVERY role, so if the inbox did not also filter
    by assignee, every authenticated caller would receive every instance in
    their scope -- an estate-wide queue rendered as a personal one. Only
    `approval.configure` lifts that, and even then the scope still binds.
    """
    app = FastAPI()
    app.include_router(approvals_api.router)
    app.dependency_overrides[approvals_api._get_database] = lambda: harness.database
    client = TestClient(app, raise_server_exceptions=False)
    harness.behaviour = lambda call: {"items": []}

    approver = make_user(["FinanceApprover"])          # act, not configure
    resp = client.get("/api/approvals/inbox",
                      headers={"X-Session": approver.session_id})
    assert resp.status_code == 200, resp.text
    assert harness.last["kwargs"]["assigned_to"] == approver.user_id, (
        "a non-administrative caller received an inbox that was not filtered "
        "to their own assignments")

    administrator = make_user(["Administrator"])       # holds approval.configure
    resp = client.get("/api/approvals/inbox",
                      headers={"X-Session": administrator.session_id})
    assert resp.status_code == 200, resp.text
    assert harness.last["kwargs"]["assigned_to"] is None, (
        "approval.configure did not lift the assignee filter")
    # The scope still binds for the administrator: lifting the assignee filter
    # is not lifting the row scope.
    assert harness.database.opened_with[-1] is harness.scope


def test_a_delegation_is_always_created_in_the_callers_own_name(harness,
                                                                capex_db,
                                                                make_user):
    """`delegator_user_id` is the session's actor, never a body field.

    A body that could name the delegator would let any holder of
    `approval.delegate` mint authority in someone else's name -- and Contract
    5 would then check both identities at decision time against a delegation
    that should never have existed.
    """
    app = FastAPI()
    app.include_router(approvals_api.router)
    app.dependency_overrides[approvals_api._get_database] = lambda: harness.database
    client = TestClient(app, raise_server_exceptions=False)
    harness.behaviour = lambda call: {"delegation_id": "G1"}

    caller = make_user(["FinanceApprover"])
    body = {"delegate_user_id": "U-OTHER", "scope_key": "ENT-A",
            "from": "2026-09-01", "to": "2026-09-30"}
    resp = client.post("/api/approvals/delegations", json=body,
                       headers={"X-Session": caller.session_id})
    assert resp.status_code == 201, resp.text
    assert harness.last["kwargs"]["delegator_user_id"] == caller.user_id

    forged = client.post(
        "/api/approvals/delegations",
        json={**body, "delegator_user_id": "U-SOMEONE-ELSE"},
        headers={"X-Session": caller.session_id})
    assert forged.status_code == 422, (
        f"a body naming the delegator was accepted: {forged.text}")


def test_an_out_of_scope_instance_is_a_404_and_not_a_403(harness, acting_client):
    """"No such row" and "not yours" must be the same answer.

    A 403 here would confirm that an instance with this id exists -- which is
    precisely the disclosure the row scope exists to prevent, and it leaks on
    the cheapest possible probe. `repo.query` documents the same rule: an
    out-of-scope row comes back as no row.
    """
    harness.behaviour = lambda call: None
    for path in ("/api/approvals/A-INVISIBLE", "/api/approvals/A-INVISIBLE/timeline"):
        resp = acting_client.request("GET", path)
        assert resp.status_code == 404, (
            f"GET {path} returned {resp.status_code}; an invisible instance "
            f"must be indistinguishable from a nonexistent one")
        assert resp.json()["detail"]["code"] == "NOT_FOUND"


def test_every_list_route_returns_the_cursor_envelope(harness, acting_client):
    """Cursor pagination on EVERY list (Contract 3).

    Normalised by the router rather than trusted from the engine, so a client
    written against the contract keeps working even where an engine function
    returns a bare list.
    """
    lists = [("GET", "/api/approvals/inbox"), ("GET", "/api/approvals/sla")]
    for shape in ({"items": [{"instance_id": "A1"}], "next_cursor": "c2",
                   "has_more": True},
                  [{"instance_id": "A1"}]):
        harness.behaviour = lambda call, shape=shape: shape
        for method, path in lists:
            body = acting_client.request(method, path).json()
            assert set(body) >= {"items", "next_cursor", "has_more"}, (
                f"{path} returned {sorted(body)}, not the cursor envelope")
            assert isinstance(body["items"], list)


def test_a_malformed_cursor_is_refused_rather_than_silently_reset(acting_client):
    """A cursor is a server-issued token, not an offset a caller composes.

    Falling back to page one on a corrupted cursor would make truncation look
    like the end of the list -- the failure mode where a reviewer believes
    they have seen every pending approval.
    """
    resp = acting_client.request("GET", "/api/approvals/inbox?cursor=!!!not-base64!!!")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_CURSOR"


# ===========================================================================
# Row scope, dimension by dimension -- the predicate half, no database needed
# ===========================================================================
@pytest.mark.parametrize("field,dimension,granted,forbidden", DIMENSIONS)
def test_a_principal_restricted_to_a_cannot_express_a_query_that_returns_b(
        field, dimension, granted, forbidden):
    """The compiled predicate is what protects the row, so it is what is
    asserted -- not the `Scope`'s fields, which a bug could leave correct
    while the predicate went wrong.

    Checked on all four dimensions because Wave 3 shipped a `project` mapping
    that was simply absent, and an absent dimension compiles to TRUE: a caller
    scoped to specific projects -- including one scoped to NO projects -- saw
    every row.
    """
    scope = _scope_granting(field, granted)
    predicate, params = repo.compile_scope(scope, INSTANCE_SCOPE_COLUMNS)

    assert predicate != "TRUE", (
        f"a principal restricted on {dimension} compiled to an unrestricted "
        f"query; the {dimension} dimension is not being applied")
    assert INSTANCE_SCOPE_COLUMNS[dimension] in predicate, (
        f"the predicate does not mention {INSTANCE_SCOPE_COLUMNS[dimension]}: "
        f"{predicate}")

    bound = {v for value in params.values()
             for v in (value if isinstance(value, (list, tuple, set, frozenset))
                       else [value])}
    assert granted in bound, f"{granted} was not bound into the query: {params}"
    assert forbidden not in bound, (
        f"{forbidden} reached the query for a principal granted only "
        f"{granted}: {params}")


@pytest.mark.parametrize("field,dimension,granted,_forbidden", DIMENSIONS)
def test_a_principal_granted_nothing_on_a_dimension_sees_nothing(
        field, dimension, granted, _forbidden):
    """Zero grants is not the same as no restriction.

    `frozenset()` means "nothing" and `None` means "all". A bug that turns the
    first into the second is the single most expensive mistake available on
    this surface, and it is invisible in a passing read.
    """
    fields = {name: None for name in principal_scope.SCOPE_DIMENSION_FIELDS}
    fields[field] = frozenset()
    scope = Scope(user_id="U-NO-GRANTS", principal_kind="USER", read_all=False,
                  **fields)
    predicate, _ = repo.compile_scope(scope, INSTANCE_SCOPE_COLUMNS)
    assert predicate != "TRUE", (
        f"zero grants on {dimension} compiled to an unrestricted query")


def test_the_wildcard_lookalike_is_refused_rather_than_read_as_a_wildcard():
    """A pre-Wave-3 grant might carry `*`. It is not "all" -- it is refused.

    Read as a wildcard it would silently promote a legacy grant row into a
    whole-estate grant on the approval inbox. The engine is stricter than
    "treat it as a literal id": it raises, because the two enforcement layers
    once disagreed about what `*` meant, and a value that two layers read
    differently must not be allowed to reach either.
    """
    from app.backend.pg.engine import InvalidScopeValue

    with pytest.raises(InvalidScopeValue):
        scope = _scope_granting("entity_ids", principal_scope.WILDCARD_LOOKALIKE)
        repo.compile_scope(scope, INSTANCE_SCOPE_COLUMNS)


# ===========================================================================
# Row scope against real rows -- gated on CAPEX_DB_URL
# ===========================================================================
def _approval_schema_present(connection) -> bool:
    row = connection.execute(
        "SELECT to_regclass('public.approval_instance') IS NOT NULL").fetchone()
    return bool(row and row[0])


@PG
def test_the_approval_schema_exists_once_a_database_is_configured(pg_connection):
    """A precondition the environment guarantees must ASSERT, not skip.

    `migrations/pg/008_approval_engine.sql` is stream 1's and was not present
    in this stream's worktree. Once `CAPEX_DB_URL` is set, the migration
    runner has run and the table must be there; a skip here would let the
    whole live matrix below opt itself out silently, which is exactly how nine
    end-to-end tests left CI green while testing nothing.
    """
    assert _approval_schema_present(pg_connection), (
        "approval_instance is absent. migrations/pg/008_approval_engine.sql "
        "(stream 1) has not landed, so the row-scope matrix below cannot run.")


@pytest.fixture()
def seeded_instances(pg_connection):
    """Two approval instances, identical but for their scope dimensions.

    A in (ENT-A, PL-A, PRJ-A, LOC-A); B in the B values. Written with direct
    SQL because instance creation belongs to the object's own workflow, not to
    Contract 3's routes -- there is no API that opens an instance.

    Columns are Contract 1's, frozen. This is the single most likely place to
    need a one-line correction at integration, and it is deliberately all in
    one fixture for that reason.
    """
    if not _approval_schema_present(pg_connection):
        pytest.fail(
            "approval_instance is absent; migration 008 (stream 1) has not landed")

    with pg_connection.cursor() as cur:
        for suffix in ("A", "B"):
            cur.execute(
                """INSERT INTO project (project_id, entity_id, plant_id,
                                        location_id, name)
                   VALUES (%(project_id)s, %(entity_id)s, %(plant_id)s,
                           %(location_id)s, %(name)s)
                   ON CONFLICT (project_id) DO NOTHING""",
                {"project_id": f"PRJ-{suffix}", "entity_id": f"ENT-{suffix}",
                 "plant_id": f"PL-{suffix}", "location_id": f"LOC-{suffix}",
                 "name": f"Scope probe {suffix}"})
            cur.execute(
                """INSERT INTO approval_instance
                       (instance_id, object_type, object_id, object_version,
                        status, current_stage_no, maker_user_id,
                        entity_id, project_id, opened_at)
                   VALUES (%(instance_id)s, 'BUDGET_REVISION', %(object_id)s, 1,
                           'OPEN', 1, %(maker)s, %(entity_id)s, %(project_id)s,
                           now())""",
                {"instance_id": f"AI-{suffix}", "object_id": f"REV-{suffix}",
                 "maker": "U-MAKER", "entity_id": f"ENT-{suffix}",
                 "project_id": f"PRJ-{suffix}"})
    pg_connection.commit()
    return {"A": "AI-A", "B": "AI-B"}


@PG
@pytest.mark.parametrize("field,dimension,granted,forbidden", DIMENSIONS)
def test_a_principal_restricted_to_a_never_reads_an_instance_in_b(
        pg_database, seeded_instances, field, dimension, granted, forbidden):
    """The row-scope matrix, against real rows and a real predicate.

    One assertion per dimension, both directions: the granted instance IS
    visible (so the query is not simply broken) and the neighbouring one is
    NOT. A one-directional test passes just as happily against a query that
    returns nothing at all.
    """
    statement = """
        SELECT i.instance_id
        FROM approval_instance i
        JOIN project p ON p.project_id = i.project_id
        WHERE {scope}
    """
    scope = _scope_granting(field, granted)
    with pg_database.session(scope) as session:
        visible = {row[0] for row in repo.query(
            session, statement, columns=INSTANCE_SCOPE_COLUMNS)}

    assert seeded_instances["A"] in visible, (
        f"a principal granted {granted} on {dimension} could not see its own "
        f"instance; the query is broken, so the exclusion below proves nothing")
    assert seeded_instances["B"] not in visible, (
        f"a principal granted only {granted} on {dimension} read an instance "
        f"in {forbidden}")


@PG
@pytest.mark.parametrize("field,dimension,granted,forbidden", DIMENSIONS)
def test_the_detail_and_timeline_reads_are_scoped_the_same_way(
        pg_database, seeded_instances, field, dimension, granted, forbidden):
    """The inbox is the route people remember to scope.

    Detail and timeline are the ones that get missed -- and they are worse,
    because an instance id is guessable and a timeline carries every actor,
    reason code and decision on the object.
    """
    scope = _scope_granting(field, granted)
    for statement in (
        """SELECT i.instance_id FROM approval_instance i
           JOIN project p ON p.project_id = i.project_id
           WHERE i.instance_id = %(id)s AND {scope}""",
        """SELECT i.instance_id, i.status FROM approval_instance i
           JOIN project p ON p.project_id = i.project_id
           WHERE i.instance_id = %(id)s AND {scope}""",
    ):
        with pg_database.session(scope) as session:
            mine = repo.query_one(session, statement,
                                  {"id": seeded_instances["A"]},
                                  columns=INSTANCE_SCOPE_COLUMNS)
            theirs = repo.query_one(session, statement,
                                    {"id": seeded_instances["B"]},
                                    columns=INSTANCE_SCOPE_COLUMNS)
        assert mine is not None, (
            f"the {dimension}-granted instance was not readable; the query is "
            f"broken and the exclusion below proves nothing")
        assert theirs is None, (
            f"a principal granted only {granted} on {dimension} read the "
            f"detail of an instance in {forbidden}")


@PG
@pytest.mark.parametrize("field,dimension,granted,forbidden", DIMENSIONS)
def test_an_out_of_scope_instance_cannot_be_locked_for_a_decision(
        pg_database, seeded_instances, field, dimension, granted, forbidden):
    """The act path, at the layer that decides it.

    Contract 7 puts `SELECT ... FOR UPDATE on approval_instance` second in the
    lock order, so a decision begins by locking the instance row. If that
    SELECT is scoped -- and it must be -- an out-of-scope instance cannot be
    locked, so it cannot be decided, whatever the caller's permissions say.
    """
    statement = """
        SELECT i.instance_id
        FROM approval_instance i
        JOIN project p ON p.project_id = i.project_id
        WHERE i.instance_id = %(id)s AND {scope}
        FOR UPDATE OF i
    """
    scope = _scope_granting(field, granted)
    with pg_database.session(scope) as session:
        locked = repo.query_one(session, statement,
                                {"id": seeded_instances["B"]},
                                columns=INSTANCE_SCOPE_COLUMNS)
    assert locked is None, (
        f"a principal granted only {granted} on {dimension} locked an "
        f"instance in {forbidden} for decision")


@PG
def test_a_denied_principal_reads_no_instance_at_all(pg_database,
                                                     seeded_instances):
    """The fail-closed end of the matrix.

    An unresolvable principal gets `denied_scope`, and a denied scope must see
    nothing -- not "everything it was not explicitly denied".
    """
    with pg_database.session(principal_scope.denied_scope("U-NOBODY")) as session:
        rows = repo.query(session, """
            SELECT i.instance_id FROM approval_instance i
            JOIN project p ON p.project_id = i.project_id
            WHERE {scope}
        """, columns=INSTANCE_SCOPE_COLUMNS)
    assert rows == [], f"a denied principal read {len(rows)} approval instances"
