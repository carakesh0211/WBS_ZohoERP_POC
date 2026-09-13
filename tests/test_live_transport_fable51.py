"""The live Zoho ERP transport, proven without a network (Fable 5.1).

Every request goes through an injected opener that records what would have
left the machine and answers what a test says Zoho would answer. What is held:
the India-only and ERP-only refusals, the read-only gate, the scope refusal
before any byte is sent, the per-minute ceiling, one re-mint on a 401, the
token cache -- and that no transcript of any failure contains the access
token, the refresh token or the client secret.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.integration import adapter as ad  # noqa: E402
from app.backend.integration import erp  # noqa: E402
from app.backend.integration import live_transport as lt  # noqa: E402

SECRET = "sekrit-client-secret-0123456789abcdef"
REFRESH = "1000.refresh-token-value.deadbeef"
ACCESS = "1000.access-token-value.cafebabe"
BASE = erp.API_HOST_BY_DC["IN"] + erp.SERVICE_PATH


def _credentials(tmp_path, *, api_domain=erp.API_HOST_BY_DC["IN"],
                 scope="ERP.settings.READ ERP.bills.READ ERP.purchaseorders.READ"):
    p = tmp_path / "erp-demo-credentials.json"
    p.write_text(json.dumps({"client_id": "1000.CLIENT", "client_secret": SECRET,
                             "refresh_token": REFRESH, "api_domain": api_domain,
                             "scope": scope}), encoding="utf-8")
    return str(p)


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Records every request; answers token mints and API calls from a script."""

    def __init__(self, api_answers=None, token_ttl=3600, api_status=200):
        self.requests = []
        self.api_answers = list(api_answers or [])
        self.token_ttl = token_ttl
        self.api_status = api_status
        self.token_mints = 0

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url.startswith(lt.ACCOUNTS_SERVER):
            self.token_mints += 1
            return _Resp(200, {"access_token": ACCESS + str(self.token_mints),
                               "expires_in": self.token_ttl, "api_domain": erp.API_HOST_BY_DC["IN"]})
        answer = self.api_answers.pop(0) if self.api_answers else {"code": 0, "message": "success"}
        if isinstance(answer, tuple):
            status, body = answer
            if status >= 400:
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))
            return _Resp(status, body)
        return _Resp(self.api_status, answer)


def _transport(tmp_path, opener=None, clock=None, **kw):
    ticks = [1000.0]
    return lt.LiveTransport(_credentials(tmp_path, **kw), opener=opener or FakeOpener(),
                            clock=clock or (lambda: ticks[0]))


def test_a_credential_on_another_data_centre_is_refused_at_construction(tmp_path):
    with pytest.raises(ad.UnsupportedDataCentre):
        lt.LiveTransport(_credentials(tmp_path, api_domain="https://www.zohoapis.com"))


def test_a_missing_credential_file_names_the_tool_not_a_traceback(tmp_path):
    with pytest.raises(ad.IntegrationError) as exc:
        lt.LiveTransport(str(tmp_path / "absent.json"))
    assert "tools/erp_demo/connect.py" in str(exc.value)


def test_a_get_carries_the_bearer_and_the_organisation_and_returns_the_body(tmp_path):
    opener = FakeOpener(api_answers=[{"code": 0, "message": "success", "bills": [{"bill_id": "1"}]}])
    t = _transport(tmp_path, opener)
    out = t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ",
                    params={"organization_id": "60074128927", "page": 1})
    assert out["bills"] == [{"bill_id": "1"}]
    api = [r for r in opener.requests if not r.full_url.startswith(lt.ACCOUNTS_SERVER)]
    assert len(api) == 1 and api[0].get_method() == "GET"
    # The separator is asserted: without the `?` the first live run asked
    # for `/contactsorganization_id=...` and Zoho answered 404 Invalid URL.
    assert api[0].full_url.startswith(BASE + "/bills?")
    assert "organization_id=60074128927" in api[0].full_url and "&page=1" in api[0].full_url
    assert api[0].get_header("Authorization") == f"Zoho-oauthtoken {ACCESS}1"
    assert t.describe()["calls_made"] == 1 and t.describe()["token_mints"] == 1


def test_a_scope_the_grant_lacks_is_refused_before_anything_is_sent(tmp_path):
    opener = FakeOpener()
    t = _transport(tmp_path, opener, scope="ERP.settings.READ")
    with pytest.raises(ad.CapabilityError) as exc:
        t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert "ERP.bills.READ" in str(exc.value)
    assert opener.requests == [], "a token was minted or a call was made for a refused scope"


def test_a_read_grant_satisfies_a_get_the_adapter_names_as_all_but_no_write(tmp_path, monkeypatch):
    """erp.py names ERP.purchaseorders.ALL on its purchase-order reads. A
    READ-only grant must serve that GET (Zoho's ALL is the superset) and
    must still refuse a write under the same name."""
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    opener = FakeOpener(api_answers=[{"code": 0, "purchaseorders": []}])
    t = _transport(tmp_path, opener, scope="ERP.purchaseorders.READ")
    out = t.request(method="GET", base_url=BASE, path="/purchaseorders", scope="ERP.purchaseorders.ALL")
    assert out["purchaseorders"] == []
    with pytest.raises(ad.CapabilityError):
        t.request(method="POST", base_url=BASE, path="/purchaseorders",
                  scope="ERP.purchaseorders.ALL", body={})


def test_a_write_is_refused_while_the_outbound_gate_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    opener = FakeOpener()
    t = _transport(tmp_path, opener, scope="ERP.purchaseorders.CREATE")
    with pytest.raises(ad.NetworkForbidden) as exc:
        t.request(method="POST", base_url=BASE, path="/purchaseorders",
                  scope="ERP.purchaseorders.CREATE", body={"x": 1})
    assert "ERP_WRITES_DISABLED" in str(exc.value)
    assert opener.requests == []


def test_a_write_leaves_only_when_the_gate_is_explicitly_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    opener = FakeOpener(api_answers=[{"code": 0, "purchaseorder": {"purchaseorder_id": "9"}}])
    t = _transport(tmp_path, opener, scope="ERP.purchaseorders.CREATE")
    out = t.request(method="POST", base_url=BASE, path="/purchaseorders",
                    scope="ERP.purchaseorders.CREATE", body={"x": 1},
                    params={"organization_id": "60074128927"})   # the write-authorised demo tenant
    assert out["purchaseorder"]["purchaseorder_id"] == "9"
    api = [r for r in opener.requests if not r.full_url.startswith(lt.ACCOUNTS_SERVER)]
    assert api[0].get_method() == "POST" and json.loads(api[0].data) == {"x": 1}


@pytest.mark.parametrize("base_url", [
    "https://www.zohoapis.com/erp/v3",       # another data centre
    "https://www.zohoapis.in/books/v3",      # another product
    "https://www.zohoapis.in/inventory/v1",
])
def test_another_host_or_product_is_refused_per_call(tmp_path, base_url):
    opener = FakeOpener()
    t = _transport(tmp_path, opener)
    with pytest.raises(ad.IntegrationError):
        t.request(method="GET", base_url=base_url, path="/bills", scope="ERP.bills.READ")
    assert opener.requests == []


def test_the_token_is_minted_once_and_reused_until_its_margin(tmp_path):
    ticks = [1000.0]
    opener = FakeOpener(token_ttl=3600)
    t = lt.LiveTransport(_credentials(tmp_path), opener=opener, clock=lambda: ticks[0])
    for _ in range(3):
        t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert opener.token_mints == 1
    ticks[0] += 3600 - lt.REFRESH_MARGIN_SECONDS + 1   # past the margin
    t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert opener.token_mints == 2


def test_one_401_mints_once_and_retries_once_never_loops(tmp_path):
    opener = FakeOpener(api_answers=[(401, {"code": 57, "message": "invalid token"}),
                                     {"code": 0, "bills": []}])
    t = _transport(tmp_path, opener)
    out = t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert out["bills"] == [] and opener.token_mints == 2
    # Two 401s in a row: the second is reported, not retried again.
    opener2 = FakeOpener(api_answers=[(401, {"code": 57, "message": "invalid token"}),
                                      (401, {"code": 57, "message": "invalid token"})])
    t2 = _transport(tmp_path, opener2)
    with pytest.raises(lt.ZohoApiError) as exc:
        t2.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert exc.value.status == 401 and opener2.token_mints == 2


def test_the_hundred_and_first_call_in_a_minute_is_refused_not_queued(tmp_path):
    ticks = [1000.0]
    t = lt.LiveTransport(_credentials(tmp_path), opener=FakeOpener(), clock=lambda: ticks[0],
                         minute_ceiling=5)
    for _ in range(5):
        t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    with pytest.raises(lt.RateBudgetExhausted):
        t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    ticks[0] += 61
    t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")


def test_a_zoho_error_body_is_surfaced_with_its_code_and_message(tmp_path):
    opener = FakeOpener(api_answers=[(400, {"code": 1002, "message": "Organization not found"})])
    t = _transport(tmp_path, opener)
    with pytest.raises(lt.ZohoApiError) as exc:
        t.request(method="GET", base_url=BASE, path="/organizations", scope="ERP.settings.READ")
    assert exc.value.zoho_code == 1002 and "Organization not found" in str(exc.value)


def test_no_failure_transcript_ever_contains_a_secret(tmp_path, monkeypatch):
    """Every refusal and every error, rendered, checked for the three values."""
    monkeypatch.delenv("CAPEX_ERP_OUTBOUND_WRITES", raising=False)
    transcripts = []
    cases = [
        lambda t: t.request(method="GET", base_url=BASE, path="/x", scope="ERP.nope.READ"),
        lambda t: t.request(method="POST", base_url=BASE, path="/x", scope="ERP.bills.READ"),
        lambda t: t.request(method="GET", base_url="https://www.zohoapis.com/erp/v3", path="/x", scope="ERP.bills.READ"),
    ]
    for case in cases:
        t = _transport(tmp_path, FakeOpener())
        with pytest.raises(Exception) as exc:
            case(t)
        transcripts.append(str(exc.value) + repr(exc.value) + json.dumps(t.describe()))
    t = _transport(tmp_path, FakeOpener(api_answers=[(500, {"code": 9, "message": "boom"})]))
    with pytest.raises(lt.ZohoApiError) as exc:
        t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    transcripts.append(str(exc.value) + json.dumps(t.describe()))
    blob = "\n".join(transcripts)
    for secret in (SECRET, REFRESH, ACCESS):
        assert secret not in blob, "a secret reached a message a human would read"


def test_the_adapter_accepts_this_transport_and_refuses_it_for_another_product(tmp_path):
    t = _transport(tmp_path)
    adapter = erp.ErpAdapter(organization_id="60074128927", transport=t)
    assert adapter.transport is t
    from app.backend.integration import books_inventory
    with pytest.raises(ad.IntegrationError):
        books_inventory.BooksInventoryAdapter(organization_id="1", transport=t)


def test_a_decimal_in_the_wire_body_arrives_as_a_string_never_a_float(tmp_path):
    """The first live items page carried `"rate": 0.0`; parsed as a float it
    is refused by dto.paise (money never exists as a float). The transport
    parses with parse_float=str so the DTO layer gets the exact text."""
    opener = FakeOpener(api_answers=[{"code": 0, "items": [{"item_id": "1", "rate": 0.0, "purchase_rate": 68000.5}]}])
    t = _transport(tmp_path, opener)
    out = t.request(method="GET", base_url=BASE, path="/items", scope="ERP.settings.READ")
    row = out["items"][0]
    assert row["rate"] == "0.0" and row["purchase_rate"] == "68000.5"
    assert not any(isinstance(v, float) for v in row.values())


def test_credentials_come_from_the_environment_when_no_file_exists(tmp_path, monkeypatch):
    """Stage B on Catalyst: the four facts are AppSail configuration values,
    never a file in the archive. Read only when the file is absent."""
    monkeypatch.setenv(lt.ENV_CLIENT_ID, "1000.CLIENT")
    monkeypatch.setenv(lt.ENV_CLIENT_SECRET, SECRET)
    monkeypatch.setenv(lt.ENV_REFRESH_TOKEN, REFRESH)
    monkeypatch.setenv(lt.ENV_API_DOMAIN, erp.API_HOST_BY_DC["IN"])
    monkeypatch.setenv(lt.ENV_SCOPES, "ERP.bills.READ ERP.settings.READ")
    opener = FakeOpener(api_answers=[{"code": 0, "bills": []}])
    t = lt.LiveTransport(str(tmp_path / "absent.json"), opener=opener, clock=lambda: 1000.0)
    assert t.granted_scopes == {"ERP.bills.READ", "ERP.settings.READ"}
    assert t.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")["bills"] == []


def test_an_environment_without_a_refresh_token_still_needs_the_file(tmp_path, monkeypatch):
    for name in (lt.ENV_CLIENT_ID, lt.ENV_CLIENT_SECRET, lt.ENV_REFRESH_TOKEN, lt.ENV_API_DOMAIN, lt.ENV_SCOPES):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ad.IntegrationError) as exc:
        lt.LiveTransport(str(tmp_path / "absent.json"))
    assert lt.ENV_REFRESH_TOKEN in str(exc.value)


# ============================================== the process-wide cache (P2)
#
# 2026-09-12 review, item 2: both call sites used to build a fresh
# `LiveTransport` per call, so its 100/minute window and its minted-token
# cache lived for exactly one HTTP request or one sweep tick.
# `shared_transport()` is a process-wide cache keyed on the resolved
# credential path. `reset()` is provided so these tests -- and any other
# module exercising the cache -- do not leak state into each other.

@pytest.fixture(autouse=True)
def _reset_shared_transport_cache():
    lt.reset()
    yield
    lt.reset()


def test_shared_transport_returns_the_same_instance_across_calls(tmp_path):
    path = _credentials(tmp_path)
    opener = FakeOpener()
    first = lt.shared_transport(path, opener=opener, clock=lambda: 1000.0)
    second = lt.shared_transport(path, opener=FakeOpener(), clock=lambda: 2000.0)
    assert second is first, (
        "a second call with the same credential path must return the SAME "
        "instance, not a fresh one -- otherwise the minute window and the "
        "token cache never span more than one call")


def test_shared_transport_shares_the_minute_window_and_the_minted_token(tmp_path):
    path = _credentials(tmp_path)
    opener = FakeOpener(api_answers=[{"code": 0, "bills": []}, {"code": 0, "bills": []}])
    ticks = [1000.0]
    first = lt.shared_transport(path, opener=opener, clock=lambda: ticks[0])
    first.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    # A later call asking for the SAME path needs no opener/clock of its own
    # -- it gets back the already-built, already-authenticated instance.
    second = lt.shared_transport(path)
    second.request(method="GET", base_url=BASE, path="/bills", scope="ERP.bills.READ")
    assert second is first
    # One token mint across two requests (the token is cached on the shared
    # instance, not re-minted per call), and both requests landed in the
    # SAME sliding-minute window because they share one `_calls` deque.
    assert opener.token_mints == 1
    assert first.calls_made == 2
    assert len(first._calls) == 2


def test_shared_transport_keys_on_the_resolved_credential_path(tmp_path):
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    path_a = _credentials(dir_a)
    path_b = _credentials(dir_b)
    first = lt.shared_transport(path_a, opener=FakeOpener())
    second = lt.shared_transport(path_b, opener=FakeOpener())
    assert first is not second, (
        "a different credential must not silently reuse another tenant's "
        "cached transport, window or token")
    assert first.api_domain == second.api_domain  # both fixtures use IN
    # Re-asking for path_a still returns the FIRST instance.
    assert lt.shared_transport(path_a) is first


def test_reset_clears_the_cache_so_a_later_call_builds_fresh(tmp_path):
    path = _credentials(tmp_path)
    before = lt.shared_transport(path, opener=FakeOpener())
    lt.reset()
    after = lt.shared_transport(path, opener=FakeOpener())
    assert after is not before, (
        "reset() must drop the cache so the next call builds and caches a "
        "genuinely new instance, not the one from before the reset")


def test_live_transport_stays_directly_constructible_for_tests(tmp_path):
    """`shared_transport()` is additive: `LiveTransport()` on its own -- what
    every other test in this file uses -- keeps working unshared."""
    path = _credentials(tmp_path)
    a = lt.LiveTransport(path, opener=FakeOpener())
    b = lt.LiveTransport(path, opener=FakeOpener())
    assert a is not b
