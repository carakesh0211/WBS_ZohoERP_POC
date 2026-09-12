"""Database-free tests for tools/uat/smoke_roles.py.

Two things need proof and neither touches a network or a database: that the
route-map DERIVATION reads the real source correctly (not a hand-copied list
that can drift from it), and that the credential scanner catches every shape
`tools/uat/smoke_roles.py`'s module docstring names without false-positiving
on ordinary JSON. `smoke_roles.py` itself is never invoked against a live
service by this suite -- today's resource rule is no live calls from the
automation that wrote it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import auth  # noqa: E402
from tools.uat.smoke_roles import (  # noqa: E402
    AUTH_ONLY, FIXED_CHECKS, PUBLIC, CREDENTIAL_RE, derive_route_permissions,
    password_for, permitted_and_forbidden, scan_for_credentials, screen_routes,
)


# ---------------------------------------------------------------- derivation, against the real source
def test_derive_route_permissions_public_routes():
    routes = derive_route_permissions()
    for path in ("/", "/healthz", "/readyz", "/api/health"):
        assert routes[path] == PUBLIC, path


def test_derive_route_permissions_auth_only_routes():
    routes = derive_route_permissions()
    # main.py's own GET routes with a bare `Depends(principal)` and no perm() --
    # any authenticated identity, whatever their role.
    for path in ("/api/auth/me", "/api/bootstrap", "/api/dashboard",
                 "/api/purchase-requests", "/api/purchase-orders"):
        assert routes[path] == AUTH_ONLY, path


def test_derive_route_permissions_explicit_permission_routes():
    """Spot-check a route from each resolution path the deriver has to
    handle: a router's floor permission alone, a route's own `_requires(...)`
    on top of a floor, a `perm(...)` default on a main.py route, and a
    module-level constant (`CONFIGURE = "approval.configure"`) resolved
    through consts rather than a bare literal."""
    routes = derive_route_permissions()
    assert routes["/api/budget/periods"] == "budget.read"                 # floor only
    assert routes["/api/budget/availability"] == "budget.check"           # _requires(...) on top of the floor
    assert routes["/api/budget/originals/import/template"] == "budget.create"
    assert routes["/api/admin/roles"] == "audit.read"
    assert routes["/api/fx/rates"] == "fx.read"
    assert routes["/api/masters/{kind_name}"] == "masters.read"
    assert routes["/api/settings/{collection}"] == "settings.read"
    assert routes["/api/approvals/definitions"] == "approval.configure"   # constant, not a literal
    assert routes["/api/approvals/delegations"] == "approval.delegate"
    assert routes["/api/approvals/inbox"] == "approval.read"              # router floor (READ constant)
    assert routes["/api/zoho/connections"] == "connector.read"            # main.py perm(...) default


def test_derive_route_permissions_cross_file_floor_resolution():
    """`budgets_original.py` and `fx_admin.py` import their router's floor
    dependency from `budget.py` rather than defining their own; the deriver
    must follow `from .budget import require_budget_access` to resolve it."""
    routes = derive_route_permissions()
    assert routes["/api/budget/categories"] == "budget.read"
    assert routes["/api/budget/originals"] == "budget.read"


def test_every_derived_permission_is_known_or_public():
    """A route naming a permission absent from `auth.PERMISSIONS` is a real
    inconsistency -- this is the guard that would catch it."""
    routes = derive_route_permissions()
    for path, perm in routes.items():
        assert perm in (PUBLIC, AUTH_ONLY) or perm in auth.PERMISSIONS, (path, perm)


def test_derive_route_permissions_finds_every_get_route_in_source():
    """A floor for the derivation count itself: undercounting silently would
    mean the smoke script skips whole screens. The real backend serves at
    least the following screens' GET routes today."""
    routes = derive_route_permissions()
    assert len(routes) >= 80
    for path in ("/api/reports/metrics", "/api/closure/requests",
                 "/api/integrations/connections", "/api/control/purchase-requests",
                 "/api/exports"):
        assert path in routes


# ---------------------------------------------------------------- screen_routes
def test_screen_routes_excludes_path_parameters_and_fixed_checks():
    routes = derive_route_permissions()
    screens = screen_routes(routes)
    assert all("{" not in p for p in screens)
    assert all(p not in FIXED_CHECKS for p in screens)
    assert "/api/masters/{kind_name}" not in screens
    assert "/api/budget/periods" in screens
    assert "/" not in screens and "/healthz" not in screens


def test_screen_routes_is_deterministic_and_sorted():
    routes = derive_route_permissions()
    screens = screen_routes(routes)
    assert screens == sorted(screens)
    assert screens == screen_routes(routes)  # calling twice gives the same list


# ---------------------------------------------------------------- permitted_and_forbidden, synthetic
def test_permitted_and_forbidden_partitions_by_role():
    route_permissions = {
        "/api/a": PUBLIC, "/api/b": AUTH_ONLY,
        "/api/c": "audit.read", "/api/d": "admin.reset", "/api/e": "pr.create",
    }
    routes = sorted(route_permissions)
    permitted, forbidden = permitted_and_forbidden({"Auditor"}, route_permissions, routes, sample=5)
    assert set(permitted) == {"/api/a", "/api/b", "/api/c"}
    assert set(forbidden) == {"/api/d", "/api/e"}


def test_permitted_and_forbidden_samples_deterministically():
    route_permissions = {f"/api/x{i}": "admin.reset" for i in range(10)}
    routes = sorted(route_permissions)
    _, forbidden_a = permitted_and_forbidden(set(), route_permissions, routes, sample=3)
    _, forbidden_b = permitted_and_forbidden(set(), route_permissions, routes, sample=3)
    assert forbidden_a == forbidden_b
    assert forbidden_a == sorted(route_permissions)[:3]


def test_permitted_and_forbidden_refuses_an_unknown_permission():
    """A route naming a permission not in auth.PERMISSIONS at all must count
    as forbidden for everyone, not silently pass every role."""
    route_permissions = {"/api/x": "nonexistent.permission"}
    permitted, forbidden = permitted_and_forbidden(
        set(auth.ROLES), route_permissions, ["/api/x"], sample=5)
    assert permitted == [] and forbidden == ["/api/x"]


def test_every_dev_user_role_reaches_at_least_one_screen_route():
    """A sanity floor on the derivation + partition together: every one of
    the 13 seeded identities' role sets can actually call something."""
    routes = derive_route_permissions()
    screens = screen_routes(routes)
    for user_id, roles in auth.DEV_USERS:
        permitted, _ = permitted_and_forbidden(set(roles), routes, screens, sample=5)
        assert permitted, f"{user_id} ({roles}) is permitted no screen route at all"


# ---------------------------------------------------------------- credential scan
def test_scan_for_credentials_catches_every_named_shape():
    cases = [
        "password=hunter2",
        "password: 'hunter2'",
        "PGPASSWORD=supersecret",
        'refresh_token=1//09abcXYZ',
        "client_secret: 'abc123XYZ'",
        "Zoho-oauthtoken 1000.abcdef1234567890.fedcba0987654321",
        "postgresql://capex:s3cret@db.internal:5432/capex",
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "xoxb-" + "1" * 12,
        "1000." + "a" * 32 + "." + "b" * 32,
    ]
    for text in cases:
        assert scan_for_credentials(text), f"missed: {text!r}"


def test_scan_for_credentials_is_clean_on_ordinary_responses():
    benign = [
        '{"user_id": "U-REQ", "name": "Requestor", "roles": ["Requestor"]}',
        '{"items": [{"pr_id": "PR-0001", "status": "Approved", "amount_paise": 3700000}]}',
        '{"detail": {"code": "FORBIDDEN", "message": "Your role cannot perform this."}}',
        "The password field on this form is required.",   # mentions the word, carries no value
        '{"connection_id": "CONN-32A904F37FEA", "mode": "MOCK"}',
    ]
    for text in benign:
        assert scan_for_credentials(text) == [], f"false positive: {text!r}"


def test_scan_for_credentials_dedupes_and_sorts():
    hits = scan_for_credentials("password=abc and password=abc again")
    assert hits == sorted(set(hits))


def test_credential_re_matches_module_docstring_claims():
    """Every pattern the module docstring promises to extend the bundle
    scanner with is actually present in the compiled pattern's source."""
    for fragment in ("PGPASSWORD", "refresh_token", "client_secret", "Zoho-oauthtoken"):
        assert fragment in CREDENTIAL_RE.pattern


# ---------------------------------------------------------------- password_for
def test_password_for_reads_tab_separated_credentials(tmp_path):
    creds = tmp_path / "uat-users.txt"
    creds.write_text(
        "# comment line\n"
        "U-REQ\tabc123XYZ\t# roles: Requestor\n"
        "U-ADM\tzzz999YYY\t# roles: Administrator\n",
        encoding="utf-8",
    )
    assert password_for("U-REQ", str(creds)) == "abc123XYZ"
    assert password_for("U-ADM", str(creds)) == "zzz999YYY"


def test_password_for_raises_for_unknown_user(tmp_path):
    creds = tmp_path / "uat-users.txt"
    creds.write_text("U-REQ\tabc123XYZ\t# roles: Requestor\n", encoding="utf-8")
    try:
        password_for("U-NOBODY", str(creds))
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "U-NOBODY" in str(exc)


def test_password_for_never_prints(tmp_path, capsys):
    creds = tmp_path / "uat-users.txt"
    creds.write_text("U-REQ\tSECRET-VALUE-XYZ\t# roles: Requestor\n", encoding="utf-8")
    pw = password_for("U-REQ", str(creds))
    assert pw == "SECRET-VALUE-XYZ"
    captured = capsys.readouterr()
    assert "SECRET-VALUE-XYZ" not in captured.out
    assert "SECRET-VALUE-XYZ" not in captured.err
