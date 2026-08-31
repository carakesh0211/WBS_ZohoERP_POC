"""Authentication and permission guard on the masters and settings routes.

Written during Wave 2 integration. Both routers arrived reading their
authorization straight off the request:

    _actor(request)       -> request.headers["X-Actor-Id"]
    _permissions(request) -> request.headers["X-Permissions"].split(",")

so the caller stated who it was and what it was allowed to do. The second is
the serious one: `X-Permissions: masters.tax_identity.reveal` would have
unmasked every GSTIN and PAN in the estate -- Regulated data under the plan's
S10.4 classification, where a full reveal is supposed to be a distinct
permission that also writes an audit entry.

The stream documented this as a provisional stand-in for the identity work
landing in a sibling stream, which is honest. It is still not mountable as
delivered, so the headers are replaced here and these tests hold that.

**No live PostgreSQL required.** Refusal happens in the router dependency,
before any handler and before any database access.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backend import auth
from app.backend.api import masters as masters_api
from app.backend.api import settings as settings_api
from app.backend.main import app

#: (method, path, permission required beyond the router's read floor)
ROUTES: list[tuple[str, str, str | None]] = [
    ("GET", "/api/masters/items", None),
    ("GET", "/api/masters/vendors", None),
    ("GET", "/api/masters/items/duplicates", None),
    ("POST", "/api/masters/items", "masters.write"),
    ("PUT", "/api/masters/items/IT-1", "masters.write"),
    ("POST", "/api/masters/items/IT-1/deactivate", "masters.write"),
    ("GET", "/api/settings/entities", None),
    ("POST", "/api/settings/entities", "settings.write"),
    ("PUT", "/api/settings/entities/ENT-1", "settings.write"),
    ("POST", "/api/settings/entities/ENT-1/deactivate", "settings.write"),
]

READ_FLOOR = {"/api/masters": "masters.read", "/api/settings": "settings.read"}


@pytest.fixture(autouse=True)
def _identity_store(capex_db):
    """Every test here needs the identity tables; without them a forged
    session raises `sqlite3.OperationalError` and returns 500, not 401."""
    return capex_db


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_every_route_is_actually_served(method, path, _perm):
    """Behavioural mount probe -- an unmounted path is 404, a mounted one is
    anything else. Route-table inspection is unreliable on this FastAPI
    version and has twice reported "not mounted" for a router that served."""
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path).status_code != 404, (
        f"{method} {path} is not served; main.py mounts both routers "
        f"unconditionally, so a 404 means include_router did not register it")


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_an_unauthenticated_caller_is_refused(method, path, _perm):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_a_forged_session_is_refused(method, path, _perm):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.request(method, path, headers={"X-Session": "not-a-real-session"})
    assert resp.status_code == 401, f"{method} {path} accepted a forged session"


@pytest.mark.parametrize("method,path,_perm", ROUTES)
def test_caller_supplied_permission_headers_grant_nothing(method, path, _perm):
    """The delivered defect, stated directly.

    A caller sending the headers the routers used to trust must be no better
    off than one sending nothing at all."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.request(method, path, headers={
        "X-Actor-Id": "U-ANYONE",
        "X-Permissions": ("masters.read,masters.write,settings.read,"
                          "settings.write,masters.tax_identity.reveal,"
                          "settings.tax_identity.reveal"),
        "X-Reveal-Reason": "because I said so",
    })
    assert resp.status_code == 401, (
        f"{method} {path} honoured caller-supplied authorization headers")


@pytest.mark.parametrize("router", [masters_api.router, settings_api.router])
def test_the_guard_is_declared_on_the_router(router):
    """A route added later inherits the guard automatically. Moving it onto
    individual routes means the next route added ships open."""
    assert router.dependencies


# ================================================================ permissions
@pytest.mark.parametrize("method,path,permission",
                         [r for r in ROUTES if r[2] is not None])
def test_a_role_lacking_the_write_permission_is_refused(make_user, method, path,
                                                        permission):
    """Authenticated is not authorised. The caller holds the router's read
    floor but not the route's write permission, so only 403 passes -- a 401
    would mean authentication failed and the test proved nothing."""
    floor = READ_FLOOR[path[:len("/api/masters")]] if path.startswith("/api/masters") \
        else READ_FLOOR["/api/settings"]
    holders = set(auth.PERMISSIONS[permission])
    readers = set(auth.PERMISSIONS[floor])
    candidates = [r for r in auth.ROLES if r in readers and r not in holders]
    assert candidates, f"no role holds {floor} without {permission}"

    caller = make_user([candidates[0]])
    resp = caller.request(method, path)
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for a role holding "
        f"{floor} but not {permission}; 403 expected")


@pytest.mark.parametrize("permission", [
    "masters.read", "masters.write", "settings.read", "settings.write",
    "masters.tax_identity.reveal", "settings.tax_identity.reveal",
])
def test_each_new_permission_is_real_and_least_privilege(permission):
    assert permission in auth.PERMISSIONS, (
        f"{permission} is referenced by a router but absent from "
        f"auth.PERMISSIONS; auth.require raises UNKNOWN_PERMISSION (500) "
        f"rather than refusing, so this fails closed but noisily")
    holders = auth.PERMISSIONS[permission]
    assert set(holders) <= set(auth.ROLES)
    assert set(holders) != set(auth.ROLES), f"{permission} grants everyone"


@pytest.mark.parametrize("permission", ["masters.tax_identity.reveal",
                                        "settings.tax_identity.reveal"])
def test_revealing_tax_identity_is_narrower_than_reading(permission):
    """Masking must not be bypassable by holding read alone: the reveal
    permission has to be strictly narrower than the read floor it sits
    behind, or it is decoration."""
    floor = ("masters.read" if permission.startswith("masters")
             else "settings.read")
    revealers = set(auth.PERMISSIONS[permission])
    readers = set(auth.PERMISSIONS[floor])
    assert revealers < readers, (
        f"{permission} is not narrower than {floor}; every reader could "
        f"unmask GSTIN and PAN")


@pytest.mark.parametrize("module", [masters_api, settings_api])
def test_permissions_come_from_the_principal_not_from_headers(module):
    """`_permissions` derives the set from the session's roles. A request
    carrying no authenticated principal must yield nothing, whatever headers
    it sends."""
    class _State:
        pass

    class _Request:
        def __init__(self, state):
            self.state = state
            self.headers = {
                "X-Permissions": "masters.tax_identity.reveal,settings.write",
                "X-Actor-Id": "U-IMPERSONATED",
            }

    assert module._permissions(_Request(_State())) == frozenset()

    state = _State()
    setattr(state, f"{module.__name__.rsplit('.', 1)[1]}_principal",
            {"user_id": "U-1", "roles": ["Requestor"]})
    granted = module._permissions(_Request(state))
    assert "masters.tax_identity.reveal" not in granted
    assert "settings.write" not in granted
    assert granted, "a Requestor holds some permissions; the set is not empty"
