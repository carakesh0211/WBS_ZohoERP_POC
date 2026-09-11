"""Authentication and permission guard on the exchange-rate routes (Fable 5.1,
migration 028; ``app/backend/api/fx_admin.py``).

**No live PostgreSQL required.** Refusal happens in the router dependency,
before any handler and before any database access, and the one test that
needs a handler to run (404 for an unknown id) runs it against a session
double that answers "no such row" -- which is exactly what the real lookup
answers for an id that does not exist.

What is held here:

* every route is served (behavioural mount probe, not route-table inspection);
* an unauthenticated caller and a forged session are refused with 401 on
  every route, including the ones that name an unknown id -- authentication
  comes BEFORE any lookup, so an anonymous caller learns nothing about ids;
* a role holding the ``fx.read`` floor but not ``fx.manage`` is refused with
  403 on every write, again before any lookup;
* the guard is declared on the ROUTER, so a route added later inherits it;
* ``fx.read`` and ``fx.manage`` are real, least-privilege permissions;
* a manager naming an unknown id gets the coded 404, on get, activate and
  deactivate alike.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.backend import auth
from app.backend.api import fx_admin as fx_admin_api
from app.backend.main import app

#: (method, path, permission required beyond the router's read floor, body)
ROUTES: list[tuple[str, str, str | None, dict | None]] = [
    ("GET", "/api/fx/rates", None, None),
    ("GET", "/api/fx/rates/lookup?source=USD&date=2026-08-06", None, None),
    ("GET", "/api/fx/rates/history?source=USD", None, None),
    ("GET", "/api/fx/rates/FXR-NONE", None, None),
    ("POST", "/api/fx/rates", "fx.manage",
     {"from_currency": "USD", "rate_date": "2026-08-06", "rate": "83.80", "rate_source": "TEST"}),
    ("POST", "/api/fx/rates/FXR-NONE/activate", "fx.manage", {}),
    ("POST", "/api/fx/rates/FXR-NONE/deactivate", "fx.manage", {"reason": "test"}),
    ("POST", "/api/fx/rates/import/preview", "fx.manage", {"rows": []}),
    ("POST", "/api/fx/rates/import", "fx.manage", {"rows": []}),
]
READ_FLOOR = "fx.read"


def _ids(entries):
    return [f"{m} {p.split('?')[0]}" for m, p, _perm, _body in entries]


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    """The identity tables; without them a forged session raises rather than
    returning 401."""
    return capex_db


@pytest.mark.parametrize("method,path,_perm,body", ROUTES, ids=_ids(ROUTES))
def test_every_route_is_actually_served(method, path, _perm, body):
    """An unmounted path is 404; a mounted one is anything else. Every route
    here refuses an anonymous caller with 401, so 404 can only mean
    ``main.py`` did not include the router."""
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path, json=body).status_code != 404, (
        f"{method} {path} is not served; main.py must include fx_admin_api.router")


@pytest.mark.parametrize("method,path,_perm,body", ROUTES, ids=_ids(ROUTES))
def test_an_unauthenticated_caller_is_refused_before_any_lookup(method, path, _perm, body):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}: {resp.text}"


@pytest.mark.parametrize("method,path,_perm,body", ROUTES, ids=_ids(ROUTES))
def test_a_forged_session_is_refused(method, path, _perm, body):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.request(method, path, json=body, headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401, f"{method} {path} accepted a forged session"


def test_the_guard_is_declared_on_the_router():
    assert fx_admin_api.router.dependencies, "the fx.read floor must be a ROUTER dependency"


# ================================================================ permissions
@pytest.mark.parametrize("permission", ["fx.read", "fx.manage"])
def test_each_permission_is_real(permission):
    assert permission in auth.PERMISSIONS
    assert set(auth.PERMISSIONS[permission]) <= set(auth.ROLES)


def test_read_is_estate_wide_by_decision_and_manage_is_not():
    """Every role that reads a translated figure reads the rate it cites --
    a recorded decision, not an accident -- and the write stays narrow.

    THE ONE EXCEPTION IS THE AUDITOR, and it is not this feature's to make:
    AUD-C-006 pinned that role's exact read set and
    `tests/test_security_identity.py` holds it. Widening the Auditor, even by
    a read, is a client decision recorded against D-12. Until it is recorded,
    the Auditor reads the rate a bill cites through the bill's own FX
    summary, not through the rate book."""
    assert set(auth.PERMISSIONS["fx.read"]) == set(auth.ROLES) - {"Auditor"}
    assert set(auth.PERMISSIONS["fx.manage"]) != set(auth.ROLES), "fx.manage grants everyone"


def test_manage_is_strictly_narrower_than_read():
    readers = set(auth.PERMISSIONS["fx.read"])
    managers = set(auth.PERMISSIONS["fx.manage"])
    assert managers < readers, "every manager reads; not every reader manages"
    assert "Auditor" not in managers
    assert "Requestor" not in managers
    assert managers == {"FinanceApprover", "Administrator"}


@pytest.mark.parametrize("method,path,permission,body",
                         [r for r in ROUTES if r[2] is not None],
                         ids=_ids([r for r in ROUTES if r[2] is not None]))
def test_a_role_holding_only_the_read_floor_is_refused_on_every_write(make_user, method, path,
                                                                        permission, body):
    """Authenticated is not authorised, and the refusal comes BEFORE any
    lookup: the paths name an id that does not exist, and a 404 here would
    mean the handler ran for a caller who may not write."""
    holders = set(auth.PERMISSIONS[permission])
    readers = set(auth.PERMISSIONS[READ_FLOOR])
    candidates = [r for r in auth.ROLES if r in readers and r not in holders]
    assert candidates, f"no role holds {READ_FLOOR} without {permission}"
    for role in candidates:
        caller = make_user([role])
        resp = caller.request(method, path, json=body)
        assert resp.status_code == 403, (
            f"{method} {path} as {role} -> {resp.status_code}: {resp.text}")
        body_json = resp.json()
        assert (body_json.get("detail") or body_json).get("code") == "FORBIDDEN"


def test_an_identity_with_no_role_is_refused_on_every_route(make_user):
    """Authenticated, holding nothing: 403 everywhere, reads included, and
    never a 404 that would confirm or deny an id."""
    caller = make_user([])
    for method, path, _perm, body in ROUTES:
        resp = caller.request(method, path, json=body)
        assert resp.status_code == 403, f"{method} {path} with no role -> {resp.status_code}"


# ================================================================ unknown id
class _NoRowsSession:
    """A session that answers every read with 'no such row' and refuses to
    be written -- the shape ``pg/fx_admin.get_rate`` sees for an unknown id."""

    def fetchone(self, statement, params=None):
        return None

    def fetchall(self, statement, params=None):
        return []

    def execute(self, statement, params=None):
        raise AssertionError(f"a write reached the database for an unknown id: {statement[:60]}")


class _NoRowsDatabase:
    @contextmanager
    def session(self, scope):
        yield _NoRowsSession()


@pytest.fixture()
def unknown_id_database(monkeypatch):
    app.dependency_overrides[fx_admin_api._get_database] = lambda: _NoRowsDatabase()
    monkeypatch.setattr(fx_admin_api, "_scope_for", lambda request, database: object())
    try:
        yield
    finally:
        app.dependency_overrides.pop(fx_admin_api._get_database, None)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/fx/rates/FXR-NONE", None),
    ("POST", "/api/fx/rates/FXR-NONE/activate", {}),
    ("POST", "/api/fx/rates/FXR-NONE/deactivate", {"reason": "test"}),
])
def test_a_manager_naming_an_unknown_id_gets_the_coded_404(make_user, unknown_id_database,
                                                           method, path, body):
    caller = make_user(["Administrator"])
    resp = caller.request(method, path, json=body)
    assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}: {resp.text}"
    detail = resp.json().get("detail") or resp.json()
    assert detail.get("code") == "FX_RATE_NOT_FOUND"


def test_a_manager_looking_up_a_date_with_no_active_rate_gets_the_coded_refusal(make_user,
                                                                                unknown_id_database):
    """The 'Test lookup' control on the screen shows exactly this: no ACTIVE
    quote covers (pair, date), so the answer is the coded 404 and never a
    stale rate or 1. The currency check reads currency_denomination, which
    the double answers 'unknown' for -- so the refusal here is the currency
    one; the rate one is held against a live server in
    tests/test_pg_fx_admin.py."""
    caller = make_user(["FinanceApprover"])
    resp = caller.get("/api/fx/rates/lookup?source=USD&date=2026-08-06")
    assert resp.status_code in (404, 422), resp.text
    detail = resp.json().get("detail") or resp.json()
    assert str(detail.get("code", "")).startswith("FX_")


def test_a_float_rate_is_refused_at_the_boundary(make_user, unknown_id_database):
    """The model types `rate` as str | int; a JSON float never reaches the
    service, which would refuse it with FX_RATE_IS_A_FLOAT anyway."""
    caller = make_user(["FinanceApprover"])
    resp = caller.post("/api/fx/rates", json={"from_currency": "USD", "rate_date": "2026-08-06",
                                              "rate": 83.8, "rate_source": "TEST"})
    assert resp.status_code == 422, resp.text
