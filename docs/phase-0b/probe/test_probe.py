"""Local verification for the Phase 0B Stage 1 probe.

Run BEFORE any deployment. No network access, no database, no Catalyst.

The security tests are the ones that matter. The probe accepts a token and
handles credentials, and it is reachable on the public internet while deployed.
Three properties must hold or it must not ship:

  1. it cannot be turned into an SSRF or port-scanning primitive;
  2. no secret can reach a response or a log line;
  3. authorisation is decided before any network activity begins.

Run:  python -m pytest docs/phase-0b/probe/test_probe.py -q
"""
from __future__ import annotations

import importlib
import io
import os
import socket
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parent

TOKEN = "test-token-Zx9Q2mK7vL4pR8sT1wY6bN3dF5gH0jC"
PASSWORD = "super-secret-database-password-DO-NOT-LEAK"
DIRECT_HOST = "db.exampleproject.supabase.co"
POOLER_HOST = "aws-0-ap-south-1.pooler.supabase.com"

ENV = {
    "PROBE_TOKEN": TOKEN,
    "PGHOST_DIRECT": DIRECT_HOST,
    "PGHOST_POOLER": POOLER_HOST,
    # Two usernames, not one: the session pooler routes on
    # <role>.<project_ref> while a direct connection uses the bare role.
    "PGUSER_DIRECT": "probe_ephemeral",
    "PGUSER_POOLER": "probe_ephemeral.exampleprojectref",
    "PGPASSWORD": PASSWORD,
    "PGDATABASE": "probe_db",
}


#: A synthetic, self-signed CA committed under fixtures/. It is never a real
#: trust anchor -- it exists so the suite exercises the genuine verification
#: path instead of short-circuiting on the fail-closed gate, and so CI needs
#: neither the real Supabase certificate nor a certificate-parsing dependency.
VALID_CA = PROBE_DIR / "fixtures" / "valid-ca.pem"
EXPIRED_CA = PROBE_DIR / "fixtures" / "expired-ca.pem"


@pytest.fixture()
def probe(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(PROBE_DIR))
    import ca as ca_mod
    # Patch the module global, not an environment variable: the trust anchor
    # must not become an operator-tunable knob just to make tests convenient.
    monkeypatch.setattr(ca_mod, "CA_BUNDLE_PATH", str(VALID_CA))
    mod = importlib.import_module("main")
    mod = importlib.reload(mod)
    yield mod
    sys.path.remove(str(PROBE_DIR))


@pytest.fixture()
def client(probe):
    from starlette.testclient import TestClient
    return TestClient(probe.app)


# ===================================================================== auth
def test_probe_requires_a_token(client):
    r = client.post("/probe", json={"endpoint": "P1"})
    assert r.status_code == 401


def test_probe_rejects_a_wrong_token(client):
    r = client.post("/probe", json={"endpoint": "P1"},
                    headers={"X-Probe-Token": "wrong"})
    assert r.status_code == 401


def test_probe_rejects_a_token_that_is_a_prefix_of_the_real_one(client):
    """Constant-time comparison must not accept a prefix."""
    r = client.post("/probe", json={"endpoint": "P1"},
                    headers={"X-Probe-Token": TOKEN[:-1]})
    assert r.status_code == 401


def test_authorisation_uses_constant_time_comparison(probe):
    import inspect
    src = inspect.getsource(probe.probe)
    assert "compare_digest" in src, "token comparison must be constant-time"
    assert "==" not in src.split("compare_digest")[0].split("expected")[-1]


def test_an_empty_configured_token_denies_everything(monkeypatch, probe):
    """A missing PROBE_TOKEN must fail closed, never open."""
    from starlette.testclient import TestClient
    monkeypatch.setenv("PROBE_TOKEN", "")
    importlib.reload(probe)
    c = TestClient(probe.app)
    assert c.post("/probe", json={"endpoint": "P1"},
                  headers={"X-Probe-Token": ""}).status_code == 401


def test_unauthorised_request_performs_no_network_activity(client, monkeypatch):
    """Authorisation is decided before DNS. Nothing may touch the network first."""
    called = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("DNS attempted before authorisation")))
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("TCP attempted before authorisation")))
    r = client.post("/probe", json={"endpoint": "P1"},
                    headers={"X-Probe-Token": "nope"})
    assert r.status_code == 401
    assert called == []


# ============================================================ SSRF surface
def test_an_arbitrary_hostname_cannot_be_supplied(client):
    r = client.post("/probe", json={"endpoint": "P1", "host": "169.254.169.254"},
                    headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 422, "extra fields must be rejected outright"


@pytest.mark.parametrize("payload", [
    {"endpoint": "http://169.254.169.254/"},
    {"endpoint": "localhost"},
    {"endpoint": "P1;P2"},
    {"endpoint": ""},
    {"endpoint": 1},
])
def test_only_the_two_configured_endpoint_keys_are_accepted(client, payload):
    r = client.post("/probe", json=payload, headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 422


def test_no_port_can_be_supplied_by_a_caller(client):
    r = client.post("/probe", json={"endpoint": "P2", "port": 22},
                    headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 422


def test_p3_and_port_6543_are_not_reachable_in_stage_1(probe, client):
    """The transaction pooler is Stage 2 and is not authorised now."""
    assert "P3" not in probe.ENDPOINTS
    assert all(e["port"] == 5432 for e in probe.ENDPOINTS.values())
    r = client.post("/probe", json={"endpoint": "P3"}, headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 422  # rejected by the schema before any dispatch


def test_endpoint_table_is_built_from_environment_not_from_requests(probe):
    assert set(probe.ENDPOINTS) <= {"P1", "P2"}
    assert probe.ENDPOINTS["P1"]["endpoint_type"] == "direct"
    assert probe.ENDPOINTS["P2"]["endpoint_type"] == "session-pooled"


def test_no_permissive_cors_middleware_is_installed(probe):
    names = [type(m.cls).__name__ if hasattr(m, "cls") else str(m)
             for m in probe.app.user_middleware]
    assert not any("CORS" in n for n in names), "probe must not be browser-callable"


# ================================================================= secrets
def _all_output(client, monkeypatch) -> str:
    """Drive a full failing run and capture every byte of response and stdout."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(
        socket.gaierror(-2, f"Name or service not known: {DIRECT_HOST} user=probe_ephemeral "
                            f"password={PASSWORD}")))
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = client.post("/probe", json={"endpoint": "P1"},
                        headers={"X-Probe-Token": TOKEN})
    return r.text + "\n" + buf.getvalue()


@pytest.mark.parametrize("secret", [PASSWORD, TOKEN])
def test_no_secret_appears_in_response_or_logs(client, monkeypatch, secret):
    out = _all_output(client, monkeypatch)
    assert secret not in out


def test_an_exception_message_never_reaches_the_output(client, monkeypatch):
    """Driver and socket messages routinely echo host, user and password.
    Only the class name and a safe code may survive."""
    out = _all_output(client, monkeypatch)
    assert "Name or service not known" not in out
    assert "gaierror" in out, "the classification itself must still be reported"


def test_no_traceback_reaches_the_output(client, monkeypatch):
    out = _all_output(client, monkeypatch)
    assert "Traceback" not in out
    assert "File \"" not in out


def test_safe_error_discards_the_message(probe):
    exc = ValueError(f"connection to {DIRECT_HOST} failed for user with password {PASSWORD}")
    red = probe.safe_error(exc)
    assert red == {"error_class": "ValueError"}
    assert PASSWORD not in str(red)


def test_safe_error_keeps_errno_and_sqlstate_only(probe):
    e = OSError()
    e.errno = 111
    assert probe.safe_error(e)["errno"] == "111"

    class DBErr(Exception):
        pass
    assert probe.safe_error(DBErr({"C": "28P01", "M": f"password {PASSWORD}"}))["sqlstate"] == "28P01"
    assert PASSWORD not in str(probe.safe_error(DBErr({"C": "28P01", "M": PASSWORD})))


def test_the_response_never_carries_a_dsn_or_full_hostname(client, monkeypatch):
    out = _all_output(client, monkeypatch)
    assert DIRECT_HOST not in out, "full hostname would let a reader reach the resource"
    assert "postgresql://" not in out


def test_healthz_is_public_and_touches_no_database(client, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("healthz must not touch the database")))
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.text
    assert PASSWORD not in body and TOKEN not in body
    assert DIRECT_HOST not in body and POOLER_HOST not in body


# ================================================================ deadline
def test_stage_budgets_match_the_approved_plan(probe):
    assert probe.DEADLINE_S == 20.0
    assert probe.STAGE_BUDGET_S == {"dns": 3.0, "tcp": 4.0, "tls": 4.0,
                                    "auth": 5.0, "query": 3.0}
    assert sum(probe.STAGE_BUDGET_S.values()) == 19.0
    assert sum(probe.STAGE_BUDGET_S.values()) < probe.DEADLINE_S


def test_deadline_is_well_inside_the_appsail_request_cap(probe):
    """AppSail terminates at 30 s. Hitting that returns nothing at all."""
    assert probe.DEADLINE_S <= 20.0
    assert 30.0 - probe.DEADLINE_S >= 10.0


def test_deadline_object_refuses_a_stage_it_cannot_afford(probe):
    d = probe.Deadline(2.0)
    assert not d.allows("auth")   # needs 5 s
    assert not d.allows("tcp")    # needs 4 s
    assert d.allows("query") is (d.remaining() >= 3.0)


def test_exhausted_deadline_marks_remaining_stages_skipped(probe, monkeypatch):
    """A truncated result with a stated reason is evidence. A platform timeout is not."""
    monkeypatch.setattr(probe, "DEADLINE_S", 0.05)
    out = probe._run("P1", probe.ENDPOINTS["P1"])
    assert out["stages"]["dns"]["outcome"] == "skipped_deadline"
    for s in ("tcp", "tls", "auth", "query"):
        assert out["stages"][s]["outcome"] == "skipped_deadline"
    assert out["q_a"] == "fail"


def test_every_stage_is_always_reported(probe, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-2, "x")))
    out = probe._run("P2", probe.ENDPOINTS["P2"])
    assert set(out["stages"]) == set(probe.STAGE_ORDER)
    assert out["stages"]["dns"]["outcome"] == "fail"
    # Corrected 2026-08-29: previously asserted `skipped_deadline`, which was
    # the label the code emitted and was wrong -- the deadline had not expired,
    # DNS had failed. Tightened, not weakened; see test_ca_bundle.py::
    # test_upstream_failure_is_not_reported_as_a_deadline_skip.
    assert out["stages"]["tcp"]["outcome"] == "skipped_upstream_failure"


def test_a_dns_failure_is_distinguishable_from_an_auth_failure(probe, monkeypatch):
    """The Phase 0A lesson: one opaque failure message costs hours."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-2, "x")))
    out = probe._run("P1", probe.ENDPOINTS["P1"])
    assert out["stages"]["dns"]["outcome"] == "fail"
    assert out["stages"]["dns"]["error_class"] == "gaierror"
    assert out["stages"]["auth"]["outcome"] != "fail"


# ================================================================ concurrency
def test_a_second_concurrent_probe_is_refused(probe, client, monkeypatch):
    import threading
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: time.sleep(0.5) or (_ for _ in ()).throw(
                            socket.gaierror(-2, "x")))
    codes = []

    def fire():
        codes.append(client.post("/probe", json={"endpoint": "P1"},
                                 headers={"X-Probe-Token": TOKEN}).status_code)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert 409 in codes, f"concurrent probes were not serialised: {codes}"


def test_the_lock_is_released_after_a_failing_run(probe, client, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-2, "x")))
    for _ in range(3):
        r = client.post("/probe", json={"endpoint": "P1"},
                        headers={"X-Probe-Token": TOKEN})
        assert r.status_code == 200, "lock leaked after a failure"


# ==================================================================== TLS
def test_tls_context_validates_chain_and_hostname(probe):
    ctx = probe.verified_context()
    import ssl as _ssl
    assert ctx.check_hostname is True
    assert ctx.verify_mode == _ssl.CERT_REQUIRED
    assert ctx.minimum_version >= _ssl.TLSVersion.TLSv1_2


def test_the_probe_never_disables_verification(probe):
    import inspect
    src = inspect.getsource(probe)
    for forbidden in ("CERT_NONE", "check_hostname = False", "_create_unverified_context"):
        assert forbidden not in src, f"verification disabled via {forbidden}"


# ================================================================== claims
def test_q_b_is_always_reported_unresolved(probe, monkeypatch):
    """Q-B cannot be established by observation, and the probe must never imply it."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-2, "x")))
    out = probe._run("P1", probe.ENDPOINTS["P1"])
    assert out["q_b"] == "unresolved"
    assert "authoritative" in out["q_b_note"]


def test_inet_client_addr_is_labelled_non_authoritative(probe):
    import inspect
    src = inspect.getsource(probe.stage_auth_and_query)
    assert "inet_client_addr_authoritative" in src
    assert "False" in src.split("inet_client_addr_authoritative")[1][:40]
