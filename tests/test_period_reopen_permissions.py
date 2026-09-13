"""Fable 5.1 / M-3 -- period reopen permissions and maker-checker.

What was wrong
==============
The three reopen routes in `api/budget.py` sat behind `period.transition`
because `auth.PERMISSIONS` had no `period.reopen` key, and `pg/periods.py`'s
call to `auth.require_separation` passed a permission that was not in
`MAKER_CHECKER` -- so that call returned having compared nobody, exactly the
`bill.void` shape. Both facts were stated in docstrings and in
`tests/test_pg_periods_reopen.py::test_the_permission_registration_this_stream_could_not_make`,
which is written to flip the day the registration lands.

What is now true, and proved here
=================================
* `period.reopen` (request) is held by BudgetController and FinanceApprover;
  `period.reopen.apply` (apply / refuse) by FinanceApprover and
  CapitalisationApprover; Auditor holds neither. A per-role negative matrix
  over EVERY role in `auth.ROLES`, at the permission layer and over HTTP.
* `period.reopen.apply` is in `auth.MAKER_CHECKER`, and the apply and refuse
  routes call `require(...)` then `require_separation(...,
  maker_user_id=<requester>, require_maker=True)` BEFORE the engine call.
  Proved at the route seam with the PostgreSQL engine stubbed (so the order of
  enforcement is observable), with a mutation check that neutralises
  `require_separation` and requires the refusal to disappear.
* `pg/periods.py`'s own comparison of applier against CLOSER stays as the
  second enforcement point (`tests/test_pg_periods_reopen.py` covers it).
* Live, on PostgreSQL, through `main.app`: the requester is refused with
  `SELF_APPROVAL` and the period stays CLOSED; an independent applier reopens
  it. Gated on `CAPEX_DB_URL`; a skip is not a pass.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys as _sys
import uuid
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
ROOT = _TESTS_DIR.parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend import auth  # noqa: E402
from app.backend.api import budget as budget_api  # noqa: E402
from app.backend.pg import engine  # noqa: E402
from app.backend.pg import periods  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402
from conftest import code_of  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

REQUEST_ROLES = frozenset({"BudgetController", "FinanceApprover"})
APPLY_ROLES = frozenset({"FinanceApprover", "CapitalisationApprover"})

_REQUEST_URL = "/api/budget/periods/AP-2026-07/reopen-requests"
_APPLY_URL = "/api/budget/period-reopen-requests/RO-1/apply"
_REFUSE_URL = "/api/budget/period-reopen-requests/RO-1/refuse"
_REQUEST_BODY = {"approval_instance_id": "AI-1", "reason": "closed a day early",
                 "idempotency_key": "k1"}
_REFUSE_BODY = {"refusal_code": "NOT_MATERIAL"}


# =========================================================================
# Registration
# =========================================================================
def test_period_reopen_permissions_are_registered_with_the_intended_roles():
    # 2026-09-13 (Stream B): the Administrator holds every permission by the
    # product owner's decision (tests/ADAPTATIONS.md); the intended FINANCE
    # roles are unchanged.
    assert set(auth.PERMISSIONS["period.reopen"]) == set(REQUEST_ROLES) | {"Administrator"}
    assert set(auth.PERMISSIONS["period.reopen.apply"]) == set(APPLY_ROLES) | {"Administrator"}


def test_period_reopen_apply_is_a_maker_checker_permission_and_the_engine_uses_it():
    assert "period.reopen.apply" in auth.MAKER_CHECKER
    assert "period.reopen" not in auth.MAKER_CHECKER, (
        "raising a request is the maker's act; the maker is not checked "
        "against themselves")
    assert periods.REOPEN_PERMISSION == "period.reopen.apply", (
        "the engine's require_separation call must name the REGISTERED key, "
        "or it is inert again")


def test_auditor_holds_neither_reopen_permission():
    assert "Auditor" not in auth.PERMISSIONS["period.reopen"]
    assert "Auditor" not in auth.PERMISSIONS["period.reopen.apply"]


def test_administrator_holds_both_reopen_permissions_but_never_the_same_seat_twice():
    """SUPERSEDES `test_administrator_holds_neither_reopen_permission` by the
    product owner's decision of 2026-09-13 (tests/ADAPTATIONS.md). What
    survives is the control: the reopen separation is enforced by
    `auth.require_separation(require_maker=True)`, by the engine's
    unconditional closer-vs-applier check and by migration 023's CHECK
    constraint, and the Administrator override is DELIBERATELY not offered
    on this path (`pg/admin_override.py` module docstring) -- so an
    Administrator who closed a period still cannot apply its reopening."""
    assert "Administrator" in auth.PERMISSIONS["period.reopen"]
    assert "Administrator" in auth.PERMISSIONS["period.reopen.apply"]
    admin = {"user_id": "U-ADMIN", "roles": ["Administrator"]}
    with pytest.raises(auth.AuthError) as exc:
        auth.require_separation(admin, "period.reopen.apply", "U-ADMIN",
                                object_label="the close of period AP-1", require_maker=True)
    assert exc.value.code == "SELF_APPROVAL"
    from app.backend.api import budget as budget_api
    source = (ROOT / "app" / "backend" / "api" / "budget.py").read_text(encoding="utf-8")
    assert "admin_override_reason" not in source.split("def _refuse_requester_as_checker")[1].split("\ndef ")[0], (
        "the period-reopen route must not offer the administrator override")


# =========================================================================
# Negative matrix per role -- permission layer
# =========================================================================
@pytest.mark.parametrize("role", [r for r in auth.ROLES if r != "Administrator"])
def test_negative_matrix_period_reopen_per_role(role):
    principal = {"user_id": "U-X", "roles": [role]}
    if role in REQUEST_ROLES:
        auth.require(principal, "period.reopen")
        return
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require(principal, "period.reopen")
    assert excinfo.value.status == 403
    assert excinfo.value.code == "FORBIDDEN"


@pytest.mark.parametrize("role", [r for r in auth.ROLES if r != "Administrator"])
def test_negative_matrix_period_reopen_apply_per_role(role):
    principal = {"user_id": "U-X", "roles": [role]}
    if role in APPLY_ROLES:
        auth.require(principal, "period.reopen.apply")
        return
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require(principal, "period.reopen.apply")
    assert excinfo.value.status == 403
    assert excinfo.value.code == "FORBIDDEN"


# =========================================================================
# Negative matrix per role -- over HTTP, every role against every route
# =========================================================================
_ROUTES = [
    pytest.param(_REQUEST_URL, _REQUEST_BODY, REQUEST_ROLES, id="request"),
    pytest.param(_APPLY_URL, {}, APPLY_ROLES, id="apply"),
    pytest.param(_REFUSE_URL, _REFUSE_BODY, APPLY_ROLES, id="refuse"),
]


@pytest.mark.parametrize("url,body,allowed", _ROUTES)
@pytest.mark.parametrize("role", [r for r in auth.ROLES if r != "Administrator"])
def test_negative_matrix_http_per_role(role, url, body, allowed, make_user):
    """A role without the permission is 403 FORBIDDEN at the route's own
    gate. A role with it clears that gate -- and is then answered by the
    database gate, which comes AFTER (503 DATABASE_NOT_CONFIGURED in this
    SQLite-only process). The order matters: the permission is decided
    before any persistence is touched."""
    caller = make_user([role])
    resp = caller.post(url, json=body)
    if role in allowed:
        assert resp.status_code != 403, (
            f"{role} holds the permission for {url} and was refused: {resp.text}")
        assert code_of(resp) != "FORBIDDEN"
    else:
        assert resp.status_code == 403, (
            f"{role} does not hold the permission for {url}; got "
            f"{resp.status_code}: {resp.text}")
        assert code_of(resp) == "FORBIDDEN"


# =========================================================================
# require_separation semantics for the new key
# =========================================================================
def test_the_requester_cannot_settle_their_own_request():
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_separation({"user_id": "U-A"}, "period.reopen.apply", "U-A",
                                require_maker=True)
    assert excinfo.value.code == "SELF_APPROVAL"
    assert excinfo.value.status == 403


def test_an_unknown_requester_is_refused_rather_than_waved_through():
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_separation({"user_id": "U-A"}, "period.reopen.apply", "",
                                require_maker=True)
    assert excinfo.value.code == "MAKER_UNKNOWN"


def test_an_independent_checker_passes_the_requester_check():
    auth.require_separation({"user_id": "U-B"}, "period.reopen.apply", "U-A",
                            require_maker=True)


# =========================================================================
# The route seam: order of enforcement, with the PostgreSQL engine stubbed
# =========================================================================
class _StubDatabase:
    @contextlib.contextmanager
    def session(self, scope):
        yield None


@pytest.fixture()
def stubbed_engine(monkeypatch):
    """`main.app`'s budget router with a stub database underneath it.

    The route's permission dependency, the server-derived actor and the
    `_refuse_requester_as_checker` call all run for real. `recorder` reports
    whether the ENGINE was reached, which is the fact these tests are about:
    the separation refusal must come before it.
    """
    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None
    engine.set_database(_StubDatabase())
    recorder = {"requester": None, "apply_calls": 0, "refuse_calls": 0}

    monkeypatch.setattr(budget_api, "_scope_for",
                        lambda request, database: Scope(user_id="stub"))
    monkeypatch.setattr(periods, "reopen_request_requester",
                        lambda session, reopen_id: recorder["requester"])

    def _apply(session, **kw):
        recorder["apply_calls"] += 1
        return {"reopen_id": kw["reopen_id"], "status": "APPLIED", "stubbed": True}

    def _refuse(session, **kw):
        recorder["refuse_calls"] += 1
        return {"reopen_id": kw["reopen_id"], "status": "REFUSED", "stubbed": True}

    monkeypatch.setattr(periods, "apply_period_reopen", _apply)
    monkeypatch.setattr(periods, "refuse_period_reopen", _refuse)
    try:
        yield recorder
    finally:
        engine.set_database(previous)


def test_apply_route_refuses_the_requester_before_the_engine_is_called(
        make_user, stubbed_engine):
    requester = make_user(["FinanceApprover"])
    stubbed_engine["requester"] = requester.user_id
    resp = requester.post(_APPLY_URL, json={})
    assert resp.status_code == 403, resp.text
    assert code_of(resp) == "SELF_APPROVAL"
    assert stubbed_engine["apply_calls"] == 0, (
        "the engine was reached before the requester check refused")


def test_refuse_route_refuses_the_requester_before_the_engine_is_called(
        make_user, stubbed_engine):
    requester = make_user(["FinanceApprover"])
    stubbed_engine["requester"] = requester.user_id
    resp = requester.post(_REFUSE_URL, json=_REFUSE_BODY)
    assert resp.status_code == 403, resp.text
    assert code_of(resp) == "SELF_APPROVAL"
    assert stubbed_engine["refuse_calls"] == 0


def test_an_independent_checker_reaches_the_engine(make_user, stubbed_engine):
    """A control that refuses everybody is an outage, and it would pass the
    two tests above."""
    stubbed_engine["requester"] = make_user(["BudgetController"]).user_id
    checker = make_user(["CapitalisationApprover"])
    resp = checker.post(_APPLY_URL, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stubbed"] is True
    assert stubbed_engine["apply_calls"] == 1


def test_the_route_refusal_comes_from_require_separation_and_nothing_else(
        make_user, stubbed_engine, monkeypatch):
    """Mutation check: neutralise the control and the refusal must vanish."""
    requester = make_user(["FinanceApprover"])
    stubbed_engine["requester"] = requester.user_id
    monkeypatch.setattr(auth, "require_separation", lambda *a, **k: None)
    resp = requester.post(_APPLY_URL, json={})
    assert resp.status_code == 200, (
        f"require_separation was neutralised and the route still refused: "
        f"{resp.status_code} {resp.text}")
    assert stubbed_engine["apply_calls"] == 1


def test_a_request_with_no_recorded_requester_is_refused_as_maker_unknown(
        make_user, stubbed_engine):
    """`require_maker=True`: a blank maker is a refusal, never a pass."""
    stubbed_engine["requester"] = ""
    checker = make_user(["FinanceApprover"])
    resp = checker.post(_APPLY_URL, json={})
    assert resp.status_code == 403, resp.text
    assert code_of(resp) == "MAKER_UNKNOWN"
    assert stubbed_engine["apply_calls"] == 0


def test_an_invisible_request_is_404_from_the_requester_lookup(
        make_user, stubbed_engine, monkeypatch):
    """The requester lookup is the engine's scoped read, so a request the
    caller cannot see is 404 -- never a 403 that would confirm it exists."""
    def _not_visible(session, reopen_id):
        raise periods.PeriodServiceError(
            "REOPEN_REQUEST_NOT_FOUND", f"No reopen request {reopen_id}.",
            status=404)
    monkeypatch.setattr(periods, "reopen_request_requester", _not_visible)
    checker = make_user(["FinanceApprover"])
    resp = checker.post(_APPLY_URL, json={})
    assert resp.status_code == 404, resp.text
    assert code_of(resp) == "REOPEN_REQUEST_NOT_FOUND"
    assert stubbed_engine["apply_calls"] == 0


# =========================================================================
# Live PostgreSQL, through main.app: the whole chain against real rows
# =========================================================================
CLOSER = "U-RO-CLOSER"
APPROVER = "U-RO-APPROVER"


def _seed(connection, *, suffix: str) -> dict[str, str]:
    """One entity, one CLOSED period, an ACTIVE definition and an APPROVED
    instance bound to the period. Column set follows
    `tests/test_pg_periods_reopen.py::_seed`, which derived it from the
    migrations."""
    ids = {"org": f"O_{suffix}", "entity": f"E_{suffix}",
           "period": f"PER_{suffix}", "definition": f"AD_{suffix}",
           "instance": f"AI_{suffix}"}
    ex = connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    for user_id in (CLOSER, APPROVER):
        ex("INSERT INTO app_user (user_id, email, display_name, created_by, "
           "updated_by) VALUES (%s,%s,%s,'t','t') ON CONFLICT DO NOTHING",
           (user_id, f"{user_id.lower()}@example.test", user_id))
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, "
       "period_end, state, closed_at, closed_by, created_by) "
       "VALUES (%s,%s,'2026-01-01','2026-03-31','CLOSED',now(),%s,'t')",
       (ids["period"], ids["entity"], CLOSER))
    ex("INSERT INTO approval_definition (definition_id, object_type, code, "
       "version, status, entity_id, effective_from, created_by, activated_at, "
       "activated_by) VALUES (%s,'ACCOUNTING_PERIOD',%s,1,'ACTIVE',%s,"
       "'2026-01-01','t',now(),'t')",
       (ids["definition"], f"PERIOD_REOPEN_{suffix}", ids["entity"]))
    ex("INSERT INTO approval_instance (instance_id, object_type, object_id, "
       "object_version, object_content_sha, definition_id, definition_version, "
       "status, snapshot, maker_user_id, entity_id) "
       "VALUES (%s,'ACCOUNTING_PERIOD',%s,1,%s,%s,1,'APPROVED',%s::jsonb,%s,%s)",
       (ids["instance"], ids["period"], f"sha_{suffix}", ids["definition"],
        json.dumps({"period_id": ids["period"]}), CLOSER, ids["entity"]))
    ex("INSERT INTO approval_action (instance_id, actor_user_id, action, seq) "
       "VALUES (%s,%s,'APPROVE',1)", (ids["instance"], APPROVER))
    ex("UPDATE approval_instance SET closed_at = now() WHERE instance_id = %s",
       (ids["instance"],))
    connection.commit()
    return ids


def _pg_identity(connection, user_id: str) -> None:
    """The SQLite session identity must exist in PostgreSQL's `app_user`, or
    `principal_scope` denies it. No restriction rows: unrestricted scope."""
    connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, "
        "updated_by) VALUES (%s,%s,%s,'t','t') ON CONFLICT DO NOTHING",
        (user_id, f"{user_id.lower()}@example.test", user_id))
    connection.commit()


@pytest.fixture()
def pg_wired(pg_database):
    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None
    engine.set_database(pg_database)
    try:
        yield pg_database
    finally:
        engine.set_database(previous)


@PG
@pytest.mark.pg
def test_live_the_requester_is_refused_and_an_independent_applier_reopens(
        pg_connection, pg_wired, make_user):
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(pg_connection, suffix=suffix)
    requester = make_user(["FinanceApprover"], user_id=f"U-RO-REQ-{suffix}")
    applier = make_user(["CapitalisationApprover"], user_id=f"U-RO-APP-{suffix}")
    _pg_identity(pg_connection, requester.user_id)
    _pg_identity(pg_connection, applier.user_id)

    with pg_wired.session(Scope.system()) as session:
        reopen_id = periods.request_period_reopen(
            session, period_id=ids["period"],
            approval_instance_id=ids["instance"],
            reason="closed a day early; two receipts still to post",
            actor=requester.user_id,
            idempotency_key=f"idem-{suffix}")["reopen_id"]

    def state() -> str:
        return pg_connection.execute(
            "SELECT state FROM accounting_period WHERE period_id = %s",
            (ids["period"],)).fetchone()[0]

    # The requester, holding period.reopen.apply, is refused as the checker.
    refused = requester.post(
        f"/api/budget/period-reopen-requests/{reopen_id}/apply", json={})
    assert refused.status_code == 403, refused.text
    assert code_of(refused) == "SELF_APPROVAL"
    assert state() == "CLOSED"

    # ... and may not settle it by refusing it either.
    refused_refuse = requester.post(
        f"/api/budget/period-reopen-requests/{reopen_id}/refuse",
        json={"refusal_code": "CHANGED_MY_MIND"})
    assert refused_refuse.status_code == 403, refused_refuse.text
    assert code_of(refused_refuse) == "SELF_APPROVAL"

    # An independent checker reopens it.
    applied = applier.post(
        f"/api/budget/period-reopen-requests/{reopen_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "APPLIED"
    assert body["applied_by"] == applier.user_id
    assert body["approved_by"] == APPROVER
    assert state() == "OPEN"
