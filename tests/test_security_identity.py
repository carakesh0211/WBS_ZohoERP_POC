"""Identity is server-derived. A caller cannot name themselves, or their roles.

AUD-C-006 was closed by moving the acting user out of the request body and
into a server-side session, and `tests/test_api_auth.py` proves the BODY
cannot nominate an actor. Headers are the other half of the same attack
surface, and they are only partly covered: `test_masters_settings_api_guard.py`
proves it for two routers by sending `X-Permissions`, which leaves every other
router, and every future one, resting on the reviewer noticing.

This module closes it structurally rather than route by route:

* behaviourally, a caller who supplies every identity-shaped header anyone has
  ever invented gains nothing at all -- not a role, not a permission, not an
  audit attribution;
* and at the SOURCE, the set of request headers the application is willing to
  read AT ALL is pinned to an allow-list. Adding `x_actor: str = Header(...)`
  to any route fails the suite on the commit that adds it, which is the only
  point at which it is cheap to argue about.

It also extends -- rather than duplicates --
`test_api_auth.py::test_aud_c_006_auditor_is_read_only`. That test asserts the
Auditor holds only four named permissions. What it cannot see is a NEW
permission whose name nobody thought to add to its tuple, so the extension
here asks the question from the other end: is every permission the Auditor
holds a READ, by the shape of its own name?
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.backend import auth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "app" / "backend"

#: Headers a caller might send hoping to be somebody else. Every one of these
#: has been a real privilege-escalation vector in some system; the point is
#: that none of them is read here, so the list can be as long as we like.
FORGED_IDENTITY_HEADERS = {
    "X-Actor": "U-ADM",
    "X-Actor-Id": "U-ADM",
    "X-User": "U-ADM",
    "X-User-Id": "U-ADM",
    "X-Username": "U-ADM",
    "X-Roles": "Administrator,FinanceApprover",
    "X-Role": "Administrator",
    "X-Permissions": "bill.void,admin.reset,audit.read,masters.tax_identity.reveal",
    "X-Principal-Kind": "SERVICE",
    "X-Scope": "*",
    "X-Entity-Ids": "*",
    "X-Read-All": "true",
    "X-Forwarded-User": "U-ADM",
    "X-Remote-User": "U-ADM",
    "X-On-Behalf-Of": "U-ADM",
    "X-Acting-For": "U-ADM",
    "Remote-User": "U-ADM",
}

#: The ONLY request headers any route in `app/backend` may bind as a parameter.
#: Each is a transport concern, and not one of them answers "who is asking" or
#: "what may they do".
ALLOWED_HEADER_PARAMETERS = {
    # authentication material -- the session token itself, which is validated
    # against `app_session` server-side and is not a claim about identity
    "authorization",
    "x_session",
    # request correlation, echoed back for tracing
    "x_correlation_id",
    "correlation_id",
    # write idempotency
    "idempotency_key",
    "x_idempotency_key",
    # the operator's stated reason for a regulated tax-identity reveal, which
    # is RECORDED in the audit entry, never consulted for authorisation
    "x_reveal_reason",
    "reveal_reason",
    # optimistic concurrency
    "if_match",
    "x_expected_version",
}


# ======================================================================
# Behaviour: the forged headers buy nothing
# ======================================================================
def test_forged_identity_headers_do_not_change_who_is_acting(requestor):
    """The session decides. Everything else is noise a caller controls."""
    plain = requestor.get("/api/bootstrap").json()
    forged = requestor.get("/api/bootstrap", headers=FORGED_IDENTITY_HEADERS).json()

    assert forged["me"]["user_id"] == "U-REQ"
    assert forged["me"] == plain["me"], (
        "the identity in the bootstrap response moved when the caller sent "
        "identity-shaped headers.")
    assert set(forged["permissions"]) == set(plain["permissions"]), (
        "a caller widened their own permission set by asking for it in a "
        "header.")


def test_forged_identity_headers_do_not_grant_a_permission_the_role_lacks(requestor):
    """A Requestor claiming FinanceApprover must still be refused a void."""
    resp = requestor.post("/api/bills/BILL-001/void",
                          json={"reason": "escalation attempt"},
                          headers=FORGED_IDENTITY_HEADERS)
    assert resp.status_code == 403, (
        f"a Requestor who claimed Administrator and FinanceApprover in headers "
        f"got {resp.status_code} on a bill void.\n{resp.text}")


def test_forged_identity_headers_do_not_reach_the_audit_trail(requestor, auditor):
    """An audit row that records the caller's claim records nothing.

    This is the failure that makes the others academic: if attribution could be
    set from a header, every entry in the chain would be a statement the actor
    chose about themselves.
    """
    created = requestor.post(
        "/api/purchase-requests",
        json={"project_id": "PRJ-01", "wbs_id": "W-03-01", "budget_head_id": "BH-PM",
              "description": "header attribution check", "amount_rupees": "1000"},
        headers=FORGED_IDENTITY_HEADERS)
    assert created.status_code == 201, created.text

    entries = auditor.get("/api/audit").json()
    mine = [e for e in entries if e["object_id"] == created.json()["pr_id"]]
    assert mine, "precondition: the creation wrote an audit entry"
    assert all(e["actor"] == "U-REQ" for e in mine), (
        f"audit attributed the action to a header-supplied identity: "
        f"{sorted({e['actor'] for e in mine})}")


def test_an_unauthenticated_caller_gains_nothing_from_the_same_headers(client):
    """The headers must not stand IN PLACE of a session either."""
    resp = client.get("/api/bootstrap", headers=FORGED_IDENTITY_HEADERS)
    assert resp.status_code == 401, (
        f"a caller with no session but plenty of confidence got "
        f"{resp.status_code}.\n{resp.text}")


# ======================================================================
# Source: the application will not even READ an identity header
# ======================================================================
def _header_parameters() -> dict[str, list[str]]:
    """{module path: [parameter names bound to a Header(...) default]}.

    AST rather than a regex: a `Header(` in a comment, a docstring or a test
    string is not a binding, and this must not fire on prose about the very
    defect it prevents.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            positional = args.posonlyargs + args.args
            defaults = list(args.defaults)
            paired = list(zip(positional[len(positional) - len(defaults):], defaults))
            paired += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d]
            for arg, default in paired:
                if isinstance(default, ast.Call) and \
                        getattr(default.func, "id", getattr(default.func, "attr", None)) == "Header":
                    names.append(arg.arg)
        if names:
            found[str(path.relative_to(PROJECT_ROOT))] = sorted(names)
    return found


def test_no_route_binds_an_identity_bearing_header():
    """The structural gate. Adding one fails here, not in production.

    A parameter name is how FastAPI derives the header name, so this reads the
    exact surface a caller can reach.
    """
    bound = _header_parameters()
    assert bound, (
        "no Header(...) binding was found anywhere in app/backend. Either the "
        "application stopped reading headers entirely -- in which case delete "
        "this test deliberately -- or this scanner has stopped working and is "
        "now passing vacuously.")

    offenders = {
        module: [n for n in names if n not in ALLOWED_HEADER_PARAMETERS]
        for module, names in bound.items()
    }
    offenders = {m: n for m, n in offenders.items() if n}
    assert not offenders, (
        f"a route binds a request header outside the allow-list: {offenders}\n"
        "If it carries identity, roles, permissions or scope, it must not "
        "exist: those come from the server-side session. If it is a genuine "
        "transport concern, add it to ALLOWED_HEADER_PARAMETERS with a reason.")


def test_the_header_scanner_would_catch_a_new_identity_header(tmp_path):
    """Mutation check on the scanner itself.

    Without this, `_header_parameters` returning nothing would look exactly
    like a clean result, and the test above would pass forever.
    """
    module = tmp_path / "rogue.py"
    module.write_text(
        "from fastapi import Header\n"
        "def route(x_actor_id: str = Header(default='')):\n"
        "    return x_actor_id\n",
        encoding="utf-8")

    tree = ast.parse(module.read_text(encoding="utf-8"))
    names = [
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for arg, default in zip(node.args.args[-len(node.args.defaults):], node.args.defaults)
        if isinstance(default, ast.Call)
        and getattr(default.func, "id", getattr(default.func, "attr", None)) == "Header"
    ]
    assert names == ["x_actor_id"]
    assert "x_actor_id" not in ALLOWED_HEADER_PARAMETERS


ROUTER_MODULES = sorted(p for p in (BACKEND / "api").glob("*.py")
                        if p.name != "__init__.py")


@pytest.mark.parametrize("module", ROUTER_MODULES, ids=lambda p: p.name)
def test_every_router_derives_the_actor_from_the_principal_not_the_request(module):
    """`_actor(request)` must read the session, never `request.headers`.

    Every PostgreSQL router has one of these and every `*_by` column and audit
    row in that router is written from it. One of them reading a header would
    make every attribution it writes a caller-supplied claim.
    """
    source = module.read_text(encoding="utf-8")
    if "def _actor(" not in source:
        pytest.skip(f"{module.name} has no _actor()")

    # EXECUTABLE STATEMENTS ONLY. Several of these functions carry a docstring
    # quoting the defect they were written to fix -- `request.headers.get(
    # "X-Actor-Id")` -- and a text search would report the fix as the bug.
    tree = ast.parse(source, filename=str(module))
    func = next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_actor")
    statements = func.body
    if statements and isinstance(statements[0], ast.Expr) \
            and isinstance(getattr(statements[0], "value", None), ast.Constant) \
            and isinstance(statements[0].value.value, str):
        statements = statements[1:]
    body = "\n".join(ast.unparse(node) for node in statements)

    assert "_principal_of" in body, (
        f"{module.name}::_actor no longer resolves the principal; it now "
        f"derives the acting user from something else:\n{body}")
    assert "headers" not in body, (
        f"{module.name}::_actor reads a request header:\n{body}")
    assert not re.search(r"\bbody\b|\bpayload\b|\bjson\b", body), (
        f"{module.name}::_actor reads the request body:\n{body}")


# ======================================================================
# Auditor: read-only, asked from the other end
# ======================================================================
#: Verbs that make a permission a WRITE, however it is spelled. Matched against
#: the permission name, which is the only thing a new permission is guaranteed
#: to have.
MUTATING_TOKENS = (
    "create", "write", "approve", "void", "cancel", "close", "amend", "delete",
    "manage", "configure", "delegate", "act", "transition", "triage", "reset",
    "share", "allocate", "submit", "revert", "retry", "publish", "update",
)

#: The Auditor's allow-list, restated here ON PURPOSE. `test_api_auth.py`'s
#: version is the audit-finding assertion and stays the reference; this copy
#: exists so a change has to be made in two places by somebody who has read
#: both, rather than quietly widened in one.
AUDITOR_PERMISSIONS = {"budget.read", "budget.check", "audit.read", "connector.read",
                       # 2026-09-11: the product owner's decision (D-12 / AUD-C-006):
                       # the Auditor reads the rate book. A read; never fx.manage.
                       "fx.read"}


def test_the_auditor_holds_exactly_the_permissions_the_finding_allows():
    held = {p for p, roles in auth.PERMISSIONS.items() if "Auditor" in roles}
    assert held == AUDITOR_PERMISSIONS, (
        f"the Auditor's permission set has changed.\n"
        f"  gained: {sorted(held - AUDITOR_PERMISSIONS)}\n"
        f"  lost:   {sorted(AUDITOR_PERMISSIONS - held)}\n"
        "AUD-C-006 pinned this role as read-only. Widening it is a client "
        "decision recorded against D-12, not a convenience for a new feature.")


def test_no_permission_the_auditor_holds_is_named_like_a_write():
    """The check the allow-list cannot make.

    An allow-list only rejects what somebody remembered to exclude. This
    rejects a permission by the shape of its own name, so a future
    `report.view.share` or `period.transition` granted to the Auditor fails
    even though nobody updated a tuple.
    """
    for permission, roles in sorted(auth.PERMISSIONS.items()):
        if "Auditor" not in roles:
            continue
        tokens = set(re.split(r"[._]", permission))
        offending = tokens & set(MUTATING_TOKENS)
        assert not offending, (
            f"the Auditor holds {permission!r}, whose name says it mutates "
            f"({sorted(offending)}). An Auditor reads; they do not act.")


def test_the_write_token_list_actually_matches_the_products_permissions():
    """Guards the guard: a token list matching nothing proves nothing."""
    matched = {
        permission for permission in auth.PERMISSIONS
        if set(re.split(r"[._]", permission)) & set(MUTATING_TOKENS)
    }
    assert len(matched) >= 10, (
        f"only {len(matched)} of {len(auth.PERMISSIONS)} permissions match the "
        "mutating-token list, which suggests the naming convention moved and "
        "the Auditor check above has quietly stopped testing anything.")
