"""The live Zoho ERP transport (Fable 5.1) -- the first component on this
branch that can reach a Zoho tenant, and the only one.

WHAT IT IS
  A :class:`~app.backend.integration.adapter.Transport` for
  :class:`~app.backend.integration.erp.ErpAdapter`: the adapter still builds
  every URL, query and scope; this class decides whether the request leaves
  the machine, adds the bearer token, and turns the answer into a mapping.
  Nothing else in the package imports it. The default transport remains
  :class:`~app.backend.integration.adapter.NoNetworkTransport`, so an adapter
  that was not handed THIS object explicitly still fetches nothing.

WHAT IT REFUSES, BEFORE ANY BYTE LEAVES
  * Any data centre but India. Zoho ERP is India-only and the adapter assumes
    `.zohoapis.in`; a credential whose `api_domain` says otherwise is refused
    at construction, and a `base_url` on another host is refused per call.
  * Any product but ERP. `product` is pinned; an adapter for Books or
    Inventory cannot be given this transport (the adapter checks), and a
    Books-shaped base URL is refused here as well.
  * Any method but GET while `CAPEX_ERP_OUTBOUND_WRITES` is not `1` -- the
    same gate `zoho.require_outbound_writes()` and `tests/test_erp_write_gate.py`
    hold. Read-only is the default and the whole of the demo phase.
  * A scope the grant does not carry. The credential file records the scopes
    the consent screen granted; a call whose required scope is missing is a
    coded refusal here, not a 401 from Zoho after the budget was spent.
  * The 101st call in any sliding minute (plan §11.6: 100/minute per
    organisation). Refused, never queued: a caller that wants to wait can.

WHAT IT NEVER DOES
  Print, log, raise or return a token, the client secret or the refresh
  token. Every error message is built from the status, the URL path and
  Zoho's own `code`/`message`; the token is held in a private attribute and
  the credential mapping is never included in an exception. A test proves a
  full failure transcript contains none of the three.

WHERE THE CREDENTIAL COMES FROM
  `CAPEX_ERP_CREDENTIALS` names a JSON file written by
  `tools/erp_demo/connect.py` OUTSIDE the repository (client_id,
  client_secret, refresh_token, api_domain, scope). The access token is
  minted from the refresh token on first use and re-minted two minutes
  before it expires; one 401 that Zoho attributes to the token triggers one
  re-mint and one retry, never a loop.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Mapping
from typing import Any, Callable

from app.backend import zoho as zoho_mod

from .adapter import (CapabilityError, IntegrationError, NetworkForbidden,
                      UnsupportedDataCentre, encode_query)
# Every product fact -- the host, the accounts server, the service path --
# is IMPORTED from the adapter implementation, never restated here:
# tests/test_integration_no_hardcoded_endpoints.py holds that only erp.py and
# books_inventory.py may spell a product literal.
from . import erp as _erp

PRODUCT = _erp.PRODUCT
CREDENTIALS_ENV = "CAPEX_ERP_CREDENTIALS"
#: Stage B: the same four facts as ENVIRONMENT VARIABLES, set once in the
#: Catalyst AppSail configuration (never in the archive, never in a commit).
#: Read only when no credential file is named or present.
ENV_CLIENT_ID = "CAPEX_ERP_CLIENT_ID"
ENV_CLIENT_SECRET = "CAPEX_ERP_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "CAPEX_ERP_REFRESH_TOKEN"
ENV_API_DOMAIN = "CAPEX_ERP_API_DOMAIN"
ENV_SCOPES = "CAPEX_ERP_SCOPES"
DEFAULT_CREDENTIALS = os.path.join(os.path.expanduser("~"), ".capex-tools",
                                   "erp-demo", "erp-demo-credentials.json")
#: `.zohoapis.in`, derived from the adapter's one India host.
API_HOST_SUFFIX = "." + (urllib.parse.urlsplit(_erp.API_HOST_BY_DC["IN"]).hostname or "").split(".", 1)[1]
ACCOUNTS_SERVER = _erp.ACCOUNTS_SERVER
SERVICE_PATH = _erp.SERVICE_PATH
MINUTE_CALL_CEILING = 100
#: Re-mint this many seconds before Zoho's stated expiry.
REFRESH_MARGIN_SECONDS = 120
USER_AGENT = "capex-wbs-control-hub/fable5.1 (read-only demo probe)"


class RateBudgetExhausted(IntegrationError):
    """The sliding-minute ceiling would be exceeded. Refused, not queued."""


class ZohoApiError(IntegrationError):
    """Zoho answered with an error. Carries the HTTP status and Zoho's own
    `code` and `message`; never the request's token."""

    def __init__(self, *, status: int, path: str, code: Any, message: Any) -> None:
        self.status = int(status)
        self.path = path
        self.zoho_code = code
        self.zoho_message = message
        super().__init__(f"Zoho ERP answered HTTP {status} for {path}: "
                         f"code={code!r} message={message!r}")


def _credentials_from_environment() -> dict[str, Any] | None:
    """The Catalyst form: every value from the process environment, or None
    when the refresh token is absent (then the file is the source)."""
    token = os.environ.get(ENV_REFRESH_TOKEN, "").strip()
    if not token:
        return None
    return {"client_id": os.environ.get(ENV_CLIENT_ID, "").strip(),
            "client_secret": os.environ.get(ENV_CLIENT_SECRET, "").strip(),
            "refresh_token": token,
            "api_domain": os.environ.get(ENV_API_DOMAIN, "").strip(),
            "scope": os.environ.get(ENV_SCOPES, "").strip()}


def _load_credentials(path: str) -> dict[str, Any]:
    rec = _credentials_from_environment() if not os.path.isfile(path) else None
    if rec is None:
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except OSError as exc:
            raise IntegrationError(
                f"No ERP credential file at {path} ({exc.strerror}) and no "
                f"{ENV_REFRESH_TOKEN} in the environment. Run "
                f"tools/erp_demo/connect.py on the operator's machine, or set "
                f"{ENV_CLIENT_ID}/{ENV_CLIENT_SECRET}/{ENV_REFRESH_TOKEN}/"
                f"{ENV_API_DOMAIN}/{ENV_SCOPES} in the platform configuration; "
                f"nothing is read from the repository.") from None
    for key in ("client_id", "client_secret", "refresh_token", "api_domain"):
        if not str(rec.get(key) or "").strip():
            raise IntegrationError(f"The ERP credential file lacks {key}.")
    domain = str(rec["api_domain"]).rstrip("/")
    host = urllib.parse.urlsplit(domain).hostname or ""
    if not host.endswith(API_HOST_SUFFIX):
        raise UnsupportedDataCentre(
            f"The ERP credential's api_domain host is {host!r}, not on "
            f"{API_HOST_SUFFIX}. Zoho ERP is India-only and this transport "
            f"refuses every other data centre.")
    rec["api_domain"] = domain
    rec["scopes"] = frozenset(str(rec.get("scope") or "").split())
    return rec


class LiveTransport:
    """See the module docstring. Construct ONE per process and hand it to the
    adapter explicitly; it is never a default."""

    product = PRODUCT

    def __init__(self, credentials_path: str | None = None, *,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 minute_ceiling: int = MINUTE_CALL_CEILING,
                 timeout: float = 30.0) -> None:
        path = credentials_path or os.environ.get(CREDENTIALS_ENV) or DEFAULT_CREDENTIALS
        self._cred = _load_credentials(path)
        self.api_domain: str = self._cred["api_domain"]
        self.granted_scopes: frozenset[str] = self._cred["scopes"]
        self._opener = opener
        self._clock = clock
        self._timeout = timeout
        self._minute_ceiling = int(minute_ceiling)
        self._calls: deque[float] = deque()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self.token_mints = 0
        self.calls_made = 0

    # ------------------------------------------------------------ evidence
    def describe(self) -> dict[str, Any]:
        """What a report may print: no secret is in it."""
        return {"product": self.product, "api_domain": self.api_domain,
                "granted_scopes": sorted(self.granted_scopes),
                "token_minted": self._token is not None,
                "token_seconds_left": (max(0, int(self._token_expires_at - self._clock()))
                                       if self._token else None),
                "calls_made": self.calls_made, "token_mints": self.token_mints}

    # --------------------------------------------------------------- token
    def _mint(self) -> str:
        form = urllib.parse.urlencode({
            "grant_type": "refresh_token", "client_id": self._cred["client_id"],
            "client_secret": self._cred["client_secret"],
            "refresh_token": self._cred["refresh_token"]}).encode()
        req = urllib.request.Request(
            f"{ACCOUNTS_SERVER}/oauth/v2/token", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT})
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise IntegrationError(
                f"The accounts server refused the token refresh with HTTP {exc.code}.") from None
        except (urllib.error.URLError, ValueError) as exc:
            raise IntegrationError(
                f"The accounts server could not be reached or answered non-JSON "
                f"({type(exc).__name__}).") from None
        token = body.get("access_token") if isinstance(body, Mapping) else None
        if not token:
            raise IntegrationError(
                f"The token refresh yielded no access token "
                f"(Zoho error: {body.get('error') if isinstance(body, Mapping) else 'unreadable'!r}). "
                f"Re-run tools/erp_demo/connect.py if the grant was revoked.")
        ttl = int(body.get("expires_in") or 3600)
        self._token = str(token)
        self._token_expires_at = self._clock() + max(60, ttl - REFRESH_MARGIN_SECONDS)
        self.token_mints += 1
        return self._token

    def _bearer(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_at:
            return self._mint()
        return self._token

    # --------------------------------------------------------------- scope
    def _scope_satisfied(self, method: str, scope: str) -> bool:
        """`ERP.<module>.READ` in the grant satisfies a GET the adapter names as
        `ERP.<module>.ALL` (Zoho treats ALL as the superset); a write needs
        the exact scope or the module's ALL. Never the other way round: a
        READ grant satisfies no write."""
        if scope in self.granted_scopes:
            return True
        parts = scope.split(".")
        if len(parts) != 3:
            return False
        product, module, verb = parts
        if method == "GET" and verb in ("ALL", "READ"):
            return f"{product}.{module}.READ" in self.granted_scopes or                 f"{product}.{module}.ALL" in self.granted_scopes
        return f"{product}.{module}.ALL" in self.granted_scopes

    # -------------------------------------------------------------- budget
    def _spend_one(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        if len(self._calls) >= self._minute_ceiling:
            raise RateBudgetExhausted(
                f"{self._minute_ceiling} calls were made in the last sixty seconds; the "
                f"organisation's per-minute ceiling would be exceeded. Refused, not "
                f"queued -- retry after {60.0 - (now - self._calls[0]):.0f}s.")
        self._calls.append(now)
        self.calls_made += 1

    # ------------------------------------------------------------- request
    def request(self, *, method: str, base_url: str, path: str, scope: str,
                params: Mapping[str, Any] | None = None,
                body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        method = str(method).upper()
        if not base_url.startswith(self.api_domain + "/") and base_url != self.api_domain:
            raise UnsupportedDataCentre(
                f"Refusing {method} {base_url}{path}: this transport is bound to "
                f"{self.api_domain} (India) and to no other host.")
        if not base_url.rstrip("/").endswith(SERVICE_PATH):
            raise IntegrationError(
                f"Refusing {method} {base_url}{path}: not the {PRODUCT} service path "
                f"{SERVICE_PATH}. Products are never mixed.")
        if method != "GET" and not zoho_mod.outbound_writes_enabled():
            raise NetworkForbidden(
                f"Refusing {method} {path}: ERP_WRITES_DISABLED. Outbound writes are "
                f"switched off (CAPEX_ERP_OUTBOUND_WRITES is not '1'); the demo "
                f"phase is read-only and a write needs its own authorisation.")
        if scope and not self._scope_satisfied(method, scope):
            raise CapabilityError(
                f"Refusing {method} {path}: it needs scope {scope}, which the grant "
                f"does not carry. Granted: {', '.join(sorted(self.granted_scopes))}. "
                f"Generate a new Self Client code with that scope; nothing was sent.")
        self._spend_one()
        # `encode_query` returns the bare `a=b&c=d`; the separator is ours to
        # add, and its absence turned every list into `/contactsorganization_id=`
        # -- a 404 "Invalid URL Passed" on the first live run.
        query = encode_query(dict(params or {}))
        url = base_url + path + (f"?{query}" if query else "")
        data = None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            data = json.dumps(dict(body)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self._send(method, url, path, headers, data, retried=False)

    def _send(self, method: str, url: str, path: str, headers: dict[str, str],
              data: bytes | None, *, retried: bool) -> Mapping[str, Any]:
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={**headers, "Authorization": f"Zoho-oauthtoken {self._bearer()}"})
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                raw = resp.read()
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
        except urllib.error.URLError as exc:
            raise IntegrationError(
                f"{method} {path}: the API host could not be reached ({exc.reason}).") from None
        try:
            # parse_float=str: money must never exist as a float. The DTO
            # layer (`dto.paise`) refuses a float on principle, and the first
            # live items page proved it: Zoho sends `"rate": 0.0`.
            parsed = json.loads(raw.decode("utf-8"), parse_float=str) if raw else {}
        except ValueError:
            raise ZohoApiError(status=status, path=path, code=None,
                               message=f"non-JSON body of {len(raw)} bytes") from None
        if status == 401 and not retried:
            # Zoho's own signal that the bearer is stale: mint once, retry once.
            self._token = None
            return self._send(method, url, path, headers, data, retried=True)
        if status >= 400 or (isinstance(parsed, Mapping) and parsed.get("code") not in (0, None, "0")):
            code = parsed.get("code") if isinstance(parsed, Mapping) else None
            message = parsed.get("message") if isinstance(parsed, Mapping) else None
            raise ZohoApiError(status=status, path=path, code=code, message=message)
        if not isinstance(parsed, Mapping):
            raise ZohoApiError(status=status, path=path, code=None,
                               message="the body is not a JSON object")
        return parsed
