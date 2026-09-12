"""Role smoke test against a deployed UAT preview.

    python tools/uat/smoke_roles.py --base https://<uat-host> --out <evidence.json>

Signs in as each of the 13 seeded identities (`app.backend.auth.DEV_USERS`),
reads passwords from

    %USERPROFILE%\\.capex-tools\\uat-credentials\\uat-users.txt

TAB-separated `user_id<TAB>password<TAB>note`, the same file and format
`tools/uat/e2e_cycle.py` already reads and never prints. For every identity it
calls `GET /`, `/healthz`, `/readyz`, `/api/auth/me` and `/api/bootstrap` (the
route `app/frontend/app.js` calls first after login: `S.boot = await
api('/bootstrap')`), then every "screen" route -- a GET with no path
parameter, standing in for one screen's list/read call -- the identity's
roles permit (expect 200) and a sample of the ones they do not (expect 403).
Every response body is scanned for credential-shaped content, which must
never appear anywhere in this application's output.

The permission each route requires is DERIVED from the source, not
hand-copied: `derive_route_permissions()` walks `app/backend/main.py` and
every `app/backend/api/*.py` router with `ast`, resolving

  * a router's floor permission -- the `_XxxAccess`/`_RequirePermission`
    callable passed to `APIRouter(dependencies=[Depends(<floor>)])`, which
    `auth_mod.require(who, "<permission>")` or `_authorise(who, <CONST>)`
    inside it names, `<CONST>` resolved through the file's own module-level
    string constants (`READ`, `ROUTER_FLOOR`, ...);
  * a route's OWN permission, on top of that floor -- a
    `dependencies=[Depends(_requires("<permission>"))]` on the decorator, or
    (main.py's shape) a `Depends(perm("<permission>"))` default on the
    function's own parameter;
  * `Depends(principal)` with no permission call at all, meaning any
    authenticated identity may call it (`AUTH_ONLY`);
  * no `Depends` at all, meaning the route needs no session
    (`PUBLIC`) -- true only for `/`, `/healthz`, `/readyz` and `/api/health`.

So the route map tracks `auth.py`'s `PERMISSIONS` table and the routers
themselves; a permission renamed or a dependency added shows up here on the
next run rather than in a list someone forgot to update.

This script is NOT run by the automation that wrote it -- it makes live HTTP
calls against a deployed AppSail preview, and the resource rule for today's
work is to touch no live service. `tests/test_uat_smoke_roles.py` exercises
`derive_route_permissions`, `screen_routes`, `permitted_and_forbidden` and the
credential scanner without a network call or a database.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "app" / "backend"
API_DIR = BACKEND / "api"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import auth  # noqa: E402

CREDENTIALS_DEFAULT = os.path.join(
    os.path.expanduser("~"), ".capex-tools", "uat-credentials", "uat-users.txt")

PUBLIC = "PUBLIC"          # no session required at all
AUTH_ONLY = "AUTH_ONLY"    # any authenticated identity, any role

# Endpoints exercised once per identity, outside the screen-route sweep --
# `/`, `/healthz`, `/readyz` and `/api/health` are PUBLIC (no session sent);
# `/api/auth/me` and `/api/bootstrap` require only authentication and are the
# two candidates the task names for "whatever the bootstrap route is" --
# `/api/bootstrap` is confirmed by `app/frontend/app.js`'s first post-login
# call (`S.boot = await api('/bootstrap')`), `/api/auth/me` is the session
# self-check every route's `principal()` dependency resolves to.
FIXED_CHECKS = ("/", "/healthz", "/readyz", "/api/health", "/api/auth/me", "/api/bootstrap")

# Credential-shaped content that must never appear in a response body.
# Extends `tools/appsail/scan_bundle.py`'s SECRET_RE (AWS/GitHub/Slack token
# shapes, Zoho's `1000.<hex>.<hex>` client id/secret shape, a PEM private key
# header) with the generic key=value and header-name shapes that a leaked
# password, a raw Postgres DSN or a bearer/refresh token would carry.
CREDENTIAL_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}"
    r"|gh[pos]_[A-Za-z0-9]{30,}"
    r"|1000\.[0-9a-f]{32}\.[0-9a-f]{32}"
    r"|-----BEGIN (?:RSA |EC )?PRIVATE KEY"
    r"|xox[baprs]-[0-9A-Za-z-]{10,}"
    r"|postgresql://[^/\s:'\"]+:[^/\s@'\"]+@"
    r"|PGPASSWORD"
    r"|password\s*[:=]\s*['\"]?[^\s'\"{}]"
    r"|refresh_token\s*[:=]\s*['\"]?[A-Za-z0-9._-]"
    r"|client_secret\s*[:=]\s*['\"]?[A-Za-z0-9._-]"
    r"|Zoho-oauthtoken\s+[A-Za-z0-9._-]"
    r")", re.IGNORECASE)


# ---------------------------------------------------------------- route-map derivation
def _call_dotted(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return _dotted(node.func)


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` for an Attribute/Name chain, else None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.insert(0, cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.insert(0, cur.id)
        return ".".join(parts)
    return None


def _lit(node: ast.AST, consts: dict[str, str]) -> str | None:
    """A string literal, or a module-level constant name resolved to one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


class _FileInfo:
    """One parsed router module: its string constants, the classes it
    defines, the permission each module-level "access" callable resolves to,
    its import aliases (for a cross-file floor reference such as
    `integrations_live.py` reusing `integrations.require_integration_access`),
    and -- if it declares one -- its router's floor permission."""

    def __init__(self, path: Path):
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.consts: dict[str, str] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.registry: dict[str, str | None] = {}
        self.import_alias: dict[str, str] = {}   # `from . import X as Y` -> {Y: X}
        self.import_from: dict[str, str] = {}     # `from .budget import Z` -> {Z: budget}
        self.router_floor_ref: tuple[str | None, str] | None = None
        self._collect()

    def _collect(self) -> None:
        mod = self.tree
        for node in ast.walk(mod):
            if isinstance(node, ast.ImportFrom) and node.module is None:
                for alias in node.names:
                    self.import_alias[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level >= 1:
                mod_stem = node.module.rsplit(".", 1)[-1]
                for alias in node.names:
                    self.import_from[alias.asname or alias.name] = mod_stem

        for node in mod.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                val = node.value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    self.consts[node.targets[0].id] = val.value
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node

        for node in mod.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            name, val = node.targets[0].id, node.value
            if name == "router" or not isinstance(val, ast.Call):
                continue
            if val.args:
                lit = _lit(val.args[0], self.consts)
                if lit is not None:
                    self.registry[name] = lit
                    continue
            callee = _call_dotted(val)
            if callee and callee in self.classes and not val.args:
                self.registry[name] = self._perm_from_class(self.classes[callee])

        for node in mod.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "router"
                    and isinstance(node.value, ast.Call)):
                continue
            for kw in node.value.keywords or ():
                if kw.arg != "dependencies" or not isinstance(kw.value, ast.List) or not kw.value.elts:
                    continue
                dep_call = kw.value.elts[0]
                if not (isinstance(dep_call, ast.Call) and _call_dotted(dep_call) == "Depends"
                        and dep_call.args):
                    continue
                dep_name = _dotted(dep_call.args[0])
                if not dep_name:
                    continue
                if "." in dep_name:
                    alias, var = dep_name.split(".", 1)
                    self.router_floor_ref = (self.import_alias.get(alias, alias), var)
                else:
                    self.router_floor_ref = (None, dep_name)

    def _perm_from_class(self, cls: ast.ClassDef) -> str | None:
        """The permission literal an `_XxxAccess`/`_RequirePermission` class's
        `__call__` passes to `auth_mod.require(...)` or `_authorise(...)`."""
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and len(node.args) >= 2:
                callee = _call_dotted(node)
                if callee in ("auth_mod.require", "auth.require", "_authorise"):
                    perm = _lit(node.args[1], self.consts)
                    if perm:
                        return perm
        return None


def _build_registry() -> tuple[dict[str, _FileInfo], dict[str, str | None]]:
    files = {p.stem: _FileInfo(p) for p in API_DIR.glob("*.py") if p.stem != "__init__"}
    floor_perm: dict[str, str | None] = {}
    for stem, fi in files.items():
        if fi.router_floor_ref is None:
            floor_perm[stem] = None
            continue
        mod_stem, var = fi.router_floor_ref
        if mod_stem is None and var in fi.import_from:
            mod_stem = fi.import_from[var]
        floor_perm[stem] = fi.registry.get(var) if mod_stem is None else (
            files[mod_stem].registry.get(var) if mod_stem in files else None)
    return files, floor_perm


def _depends_permission(node: ast.expr, fi: _FileInfo, files: dict[str, _FileInfo]) -> str | None:
    """The argument to one `Depends(...)` call. A literal permission from a
    `_requires("x")`/`perm("x")` call, `AUTH_ONLY` for `Depends(principal)`,
    a resolved floor/registry permission for a bare or cross-module name, or
    None if this dependency carries no permission information at all."""
    if isinstance(node, ast.Call):
        last = (_call_dotted(node) or "").rsplit(".", 1)[-1]
        if last in ("_requires", "perm") and node.args:
            return _lit(node.args[0], fi.consts)
        return None
    dotted = _dotted(node)
    if dotted is None:
        return None
    if dotted == "principal":
        return AUTH_ONLY
    if "." in dotted:
        alias, var = dotted.split(".", 1)
        other = files.get(fi.import_alias.get(alias, alias))
        return other.registry.get(var) if other else None
    if dotted in fi.registry:
        return fi.registry[dotted]
    if dotted in fi.import_from:
        other = files.get(fi.import_from[dotted])
        if other and dotted in other.registry:
            return other.registry[dotted]
    return None


def _extract_get_routes(fi: _FileInfo, app_var: str,
                        files: dict[str, _FileInfo]) -> dict[str, str | None]:
    """`{path: permission}` for every `@<app_var>.get(...)` in one module.
    A `None` value means no explicit dependency was found on this route --
    the caller applies the router's floor permission, or PUBLIC if it has
    none."""
    out: dict[str, str | None] = {}
    for node in ast.walk(fi.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and _dotted(dec.func) == f"{app_var}.get" and dec.args):
                continue
            path = _lit(dec.args[0], fi.consts)
            if not path:
                continue
            found: str | None = None
            for kw in dec.keywords or ():
                if kw.arg != "dependencies" or not isinstance(kw.value, ast.List):
                    continue
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Call) and _call_dotted(elt) == "Depends" and elt.args:
                        perm = _depends_permission(elt.args[0], fi, files)
                        if perm is not None:
                            found = perm
            if found is None:
                for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                    if isinstance(default, ast.Call) and _call_dotted(default) == "Depends" and default.args:
                        perm = _depends_permission(default.args[0], fi, files)
                        if perm is not None:
                            found = perm
            out[path] = found
    return out


def derive_route_permissions() -> dict[str, str]:
    """`{GET path template: PUBLIC | AUTH_ONLY | <permission in auth.PERMISSIONS>}`
    for every GET route this backend serves, read from the source with `ast`
    rather than maintained by hand. See the module docstring for the
    resolution rules."""
    files, floor_perm = _build_registry()
    result: dict[str, str] = {}
    for stem, fi in files.items():
        floor = floor_perm.get(stem)
        for path, perm in _extract_get_routes(fi, "router", files).items():
            result[path] = perm if perm is not None else (floor or PUBLIC)
    main_fi = _FileInfo(BACKEND / "main.py")
    for path, perm in _extract_get_routes(main_fi, "app", files).items():
        result[path] = perm if perm is not None else PUBLIC
    return result


def screen_routes(route_permissions: dict[str, str]) -> list[str]:
    """The "screen" subset of `derive_route_permissions()`'s output: GET
    routes with no path parameter (a screen's list/summary call, not a
    detail sub-resource) and not one of `FIXED_CHECKS`, which every identity
    calls once regardless of role."""
    return sorted(p for p in route_permissions
                  if "{" not in p and p not in FIXED_CHECKS)


def permitted_and_forbidden(roles: set[str], route_permissions: dict[str, str],
                            routes: list[str], sample: int = 5) -> tuple[list[str], list[str]]:
    """Partition `routes` by whether `roles` may call them, per
    `auth.PERMISSIONS`. `PUBLIC`/`AUTH_ONLY` routes are always permitted.
    Returns `(permitted, forbidden_sample)` -- the forbidden list capped at
    `sample` entries, taken in sorted order for a reproducible run."""
    permitted, forbidden = [], []
    for path in routes:
        perm = route_permissions[path]
        if perm in (PUBLIC, AUTH_ONLY):
            permitted.append(path)
            continue
        holders = auth.PERMISSIONS.get(perm)
        if holders is None:
            # A route names a permission auth.PERMISSIONS does not define --
            # a real inconsistency the smoke run should surface, not hide.
            forbidden.append(path)
            continue
        (permitted if roles & set(holders) else forbidden).append(path)
    return permitted, sorted(forbidden)[:sample]


# ---------------------------------------------------------------- credential scan
def scan_for_credentials(text: str) -> list[str]:
    """Every credential-shaped match in `text`, deduplicated. Empty means
    clean. Matches the *shape* only -- this never decides whether a value is
    a real secret, only whether it looks enough like one that a response
    body must never carry it."""
    return sorted({m.group(0) for m in CREDENTIAL_RE.finditer(text)})


# ---------------------------------------------------------------- HTTP + credentials
def password_for(user_id: str, credentials_path: str) -> str:
    """The one field this function returns and NOTHING ELSE touches: no
    caller of this function may log, print or persist the return value.
    Mirrors `tools/uat/e2e_cycle.py::password_for`."""
    with open(credentials_path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\r\n").split("\t")
            if parts and parts[0] == user_id and len(parts) > 1:
                return parts[1]
    raise SystemExit(f"no credential line for {user_id} in {credentials_path}")


def _call(base: str, method: str, path: str, session: str | None = None,
         body: dict | None = None, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if session:
        headers["X-Session"] = session
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


# ---------------------------------------------------------------- run
def run(base: str, credentials_path: str, out_path: str, sample: int = 5) -> int:
    route_permissions = derive_route_permissions()
    routes = screen_routes(route_permissions)
    evidence = {
        "base": base, "started_at": datetime.now(timezone.utc).isoformat(),
        "route_count": len(route_permissions), "screen_route_count": len(routes),
        "identities": [],
    }
    ok = True
    table = []
    for user_id, roles in auth.DEV_USERS:
        role_set = set(roles)
        record = {"user_id": user_id, "roles": roles, "checks": [], "credential_hits": []}

        def hit(name: str, status: int, expected: int, body: str) -> None:
            nonlocal ok
            passed = status == expected
            ok = ok and passed
            leaks = scan_for_credentials(body)
            if leaks:
                ok = False
                record["credential_hits"].append({"check": name, "matches": leaks})
            record["checks"].append({"name": name, "status": status, "expected": expected, "ok": passed})

        pw = password_for(user_id, credentials_path)
        st, body = _call(base, "POST", "/api/auth/login", body={"user_id": user_id, "password": pw})
        del pw
        if st != 200:
            hit("login", st, 200, body)
            evidence["identities"].append(record)
            table.append((user_id, ",".join(roles), "LOGIN FAILED", "-", "-"))
            ok = False
            continue
        session = json.loads(body)["session_id"]

        for path in FIXED_CHECKS:
            st, body = _call(base, "GET", path, session=None if path in ("/", "/healthz", "/readyz") else session)
            hit(path, st, 200, body)

        permitted, forbidden_sample = permitted_and_forbidden(role_set, route_permissions, routes, sample)
        for path in permitted:
            st, body = _call(base, "GET", path, session=session)
            hit(f"GET {path} (permitted)", st, 200, body)
        for path in forbidden_sample:
            st, body = _call(base, "GET", path, session=session)
            hit(f"GET {path} (forbidden)", st, 403, body)

        _call(base, "POST", "/api/auth/logout", session=session)
        n_ok = sum(1 for c in record["checks"] if c["ok"])
        table.append((user_id, ",".join(roles), f"{n_ok}/{len(record['checks'])}",
                     str(len(permitted)), str(len(forbidden_sample))))
        evidence["identities"].append(record)

    evidence["ok"] = ok
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    header = f"{'user':10} {'roles':45} {'checks':10} {'permitted':10} {'forbidden_sampled':18}"
    print(header)
    print("-" * len(header))
    for row in table:
        print(f"{row[0]:10} {row[1]:45} {row[2]:10} {row[3]:10} {row[4]:18}")
    print()
    print(("ALL CHECKS PASSED" if ok else "FAILURES -- see") + f" {out_path}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Role smoke test against a deployed UAT preview.")
    ap.add_argument("--base", required=True, help="e.g. https://wbs-capex-uat-....catalystappsail.in")
    ap.add_argument("--out", required=True, help="evidence JSON path")
    ap.add_argument("--credentials", default=CREDENTIALS_DEFAULT,
                    help="TAB-separated user_id/password/note file (default: %(default)s)")
    ap.add_argument("--sample", type=int, default=5,
                    help="forbidden routes sampled per identity (default: 5)")
    args = ap.parse_args(argv)
    return run(args.base, args.credentials, args.out, args.sample)


if __name__ == "__main__":
    raise SystemExit(main())
