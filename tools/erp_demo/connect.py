"""Exchange a Zoho Self Client grant code for a refresh token -- once, on the
operator's own machine, with nothing printed and nothing committed (Fable 5.1).

    python tools/erp_demo/connect.py            # prompts, hidden input
    python tools/erp_demo/connect.py --check    # refresh test: expiry only
    python tools/erp_demo/connect.py --self-test  # no network

WHAT IT DOES
  1. Reads the client id, client secret and the one-time grant code from
     HIDDEN prompts (getpass). None of the three is echoed, logged or written
     anywhere but the credential file below.
  2. POSTs to https://accounts.zoho.in/oauth/v2/token (grant_type
     authorization_code; a Self Client needs no redirect_uri). India data
     centre only -- Zoho ERP is India-only and the adapter assumes it.
  3. Refuses anything that is not a token response carrying a refresh token
     and an api_domain on the .in domain, and says why in one sentence.
  4. Writes ONE file outside the repository:
         %USERPROFILE%\\.capex-tools\\erp-demo\\erp-demo-credentials.json
     holding client_id, client_secret, refresh_token, api_domain, scopes and
     the timestamp, readable by the current user only (icacls / chmod 600).
  5. Prints the api_domain, the granted scopes and the access-token lifetime.
     It never prints a token or the secret, and the access token it received
     is discarded: the application obtains its own at run time from the
     refresh token (app/backend/integration/live_transport.py).

The repository's .gitignore already excludes every *credentials*.json and the
bundle gate refuses one; this file lives outside the tree regardless.
"""
from __future__ import annotations

import argparse
import getpass
import re
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACCOUNTS = "https://accounts.zoho.in"
TOKEN_URL = f"{ACCOUNTS}/oauth/v2/token"
CRED_DIR = os.path.join(os.path.expanduser("~"), ".capex-tools", "erp-demo")
CRED_FILE = os.path.join(CRED_DIR, "erp-demo-credentials.json")
NEVER_PRINT = ("access_token", "refresh_token", "client_secret", "code")


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"REFUSED: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _clean(value: str, label: str) -> str:
    """Strip whitespace, surrounding quotes and a `name=` prefix that a copy
    from a web page or a shell snippet sometimes carries."""
    v = value.strip().strip('"').strip("'").strip()
    for prefix in ("code=", "client_id=", "client_secret="):
        if v.lower().startswith(prefix):
            v = v[len(prefix):].strip()
    if not v:
        _fail(f"{label} is empty")
    return v


def sanitise(body: dict) -> dict:
    """The token response with every secret field removed -- the ONLY shape
    this tool ever prints. Proven by --self-test."""
    return {k: v for k, v in body.items() if k not in NEVER_PRINT}


def _post(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status, ctype, final_url = resp.status, resp.headers.get("Content-Type"), resp.geturl()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status, ctype, final_url = exc.code, exc.headers.get("Content-Type"), exc.geturl()
    except urllib.error.URLError as exc:
        _fail(f"the accounts server could not be reached: {exc.reason}")
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        # The server's own page, not ours: no secret can be in it. A redirect
        # to accounts.zoho.com means the client was created on another data
        # centre's console.
        _fail(f"the accounts server answered something that is not JSON "
              f"(HTTP {status}, {ctype}, final URL {final_url}); nothing stored. "
              f"First bytes: {raw[:160]!r}")
    if not isinstance(body, dict):
        _fail("the accounts server answered a non-object; nothing stored")
    if "error" in body:
        _fail(f"the accounts server refused the exchange: {body.get('error')} "
              f"(a used or expired grant code answers invalid_code; generate a fresh one)")
    return body


def _restrict(path: str) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                           capture_output=True, check=False)


def exchange() -> int:
    print("Zoho ERP demo -- one-time Self Client exchange (India DC). Input is hidden.")
    client_id = _clean(getpass.getpass("Client ID: "), "Client ID")
    client_secret = _clean(getpass.getpass("Client secret: "), "Client secret")
    code = _clean(getpass.getpass("Grant code (from 'Generate Code'): "), "Grant code")
    # SHAPE, NOT VALUE. The accounts server answers an HTML 400 page -- not a
    # JSON error -- when the grant code is wrapped in quotes, prefixed with
    # "code=", or over-long (the same value pasted twice into a hidden
    # prompt is the usual cause). Say which, in characters, never the value.
    if not re.fullmatch(r"1000\.[0-9a-f]{32}\.[0-9a-f]{32}", code):
        _fail(f"the grant code does not have Zoho's shape 1000.<32 hex>.<32 hex>: "
              f"{len(code)} characters, {code.count('.')} dots. A generated code "
              f"is exactly 70 characters; if this is longer it was pasted twice "
              f"(the prompt shows nothing while you paste -- paste ONCE, press Enter). "
              f"Generate a fresh code and rerun.")
    if not re.fullmatch(r"1000\.[A-Z0-9]{20,40}", client_id):
        _fail(f"the Client ID does not have Zoho's shape 1000.<letters/digits>: "
              f"{len(client_id)} characters. Copy it from the Self Client's Client Secret tab.")
    if not re.fullmatch(r"[0-9a-f]{30,64}", client_secret):
        _fail(f"the Client secret is not a plain hex string: {len(client_secret)} characters.")
    print(f"read: client id {len(client_id)} chars, secret {len(client_secret)} chars, code {len(code)} chars")
    body = _post({"grant_type": "authorization_code", "client_id": client_id,
                  "client_secret": client_secret, "code": code})
    refresh = body.get("refresh_token")
    api_domain = str(body.get("api_domain") or "")
    if not refresh:
        _fail("no refresh_token in the response (a Self Client grant must be exchanged "
              "within its time limit and only once); nothing stored")
    if not api_domain.endswith(".zoho.in") and not api_domain.endswith(".zohoapis.in"):
        _fail(f"api_domain is {api_domain!r}, not the India data centre; the adapter "
              f"assumes .in and this tenant would be misread. Nothing stored")
    os.makedirs(CRED_DIR, exist_ok=True)
    record = {
        "dc": "IN", "accounts_server": ACCOUNTS, "api_domain": api_domain,
        "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh,
        "scope": body.get("scope"), "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(CRED_FILE, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    _restrict(CRED_FILE)
    print(json.dumps({"stored": CRED_FILE, **sanitise(body)}, indent=2))
    print("Stored. The access token was discarded; the application refreshes its own.")
    return 0


def check() -> int:
    """Refresh once and report the lifetime -- never the token."""
    try:
        with open(CRED_FILE, encoding="utf-8") as fh:
            rec = json.load(fh)
    except OSError:
        _fail(f"{CRED_FILE} is missing; run the exchange first")
    body = _post({"grant_type": "refresh_token", "client_id": rec["client_id"],
                  "client_secret": rec["client_secret"], "refresh_token": rec["refresh_token"]})
    if not body.get("access_token"):
        _fail("the refresh did not yield an access token")
    print(json.dumps({"refresh": "ok", **sanitise(body)}, indent=2))
    return 0


def self_test() -> int:
    sample = {"access_token": "A", "refresh_token": "R", "api_domain": "https://www.zohoapis.in",
              "token_type": "Bearer", "expires_in": 3600, "scope": "ERP.settings.READ", "code": "C"}
    out = json.dumps(sanitise(sample))
    for secret in ("A", "R", "C"):
        assert f'"{secret}"' not in out, "a secret survived sanitise()"
    for key in NEVER_PRINT:
        assert key not in sanitise(sample)
    print("self-test ok: access_token, refresh_token, client_secret and code are never printed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="refresh once; report lifetime only")
    ap.add_argument("--self-test", action="store_true", help="prove the sanitiser; no network")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.check:
        return check()
    return exchange()


if __name__ == "__main__":
    raise SystemExit(main())
