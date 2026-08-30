"""Regression tests for the two defects that wasted attempt 2.

Attempt 2 produced **no connectivity data at all**. Both endpoints returned
`404 ENDPOINT_NOT_CONFIGURED`, and that 404 was written into the evidence files
as though it were a result. Two distinct faults caused it:

1. **One username for two endpoints.** Supabase's session pooler routes on a
   ``<role>.<project_ref>`` username while a direct connection uses the bare
   role name. A single ``PGUSER`` could not express both, so P1 and P2 could
   never both succeed in one pass.

2. **Configuration that never reached the running instance.** AppSail binds
   environment variables at *instance start*; changing configuration does not
   recycle a live instance. Six variables were set and then a already-running
   container was queried, whose endpoint table had been built at import from an
   empty environment.

The tests below hold both fixes, plus the rule that a refusal to run is never
recorded as evidence.

Run:  python -m pytest docs/phase-0b/probe/test_attempt3.py -q
"""
from __future__ import annotations

import importlib
import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))

import ca as ca_mod  # noqa: E402
import probe_invoke  # noqa: E402

VALID_CA = PROBE_DIR / "fixtures" / "valid-ca.pem"

TOKEN = "test-token-Zx9Q2mK7vL4pR8sT1wY6bN3dF5gH0jC"
PASSWORD = "super-secret-database-password-DO-NOT-LEAK"
DIRECT_HOST = "db.exampleproject.supabase.co"
POOLER_HOST = "aws-0-ap-south-1.pooler.supabase.com"
DIRECT_USER = "probe_ephemeral"
POOLER_USER = "probe_ephemeral.exampleprojectref"

ENV = {
    "PROBE_TOKEN": TOKEN,
    "PGHOST_DIRECT": DIRECT_HOST,
    "PGHOST_POOLER": POOLER_HOST,
    "PGUSER_DIRECT": DIRECT_USER,
    "PGUSER_POOLER": POOLER_USER,
    "PGPASSWORD": PASSWORD,
    "PGDATABASE": "probe_db",
}


def _load(monkeypatch, env=None):
    for k in ("PGUSER", "PGUSER_DIRECT", "PGUSER_POOLER",
              "PGHOST_DIRECT", "PGHOST_POOLER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env if env is not None else ENV).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ca_mod, "CA_BUNDLE_PATH", str(VALID_CA))
    return importlib.reload(importlib.import_module("main"))


# ===================================================== 1. separate usernames
def test_each_endpoint_carries_its_own_username(monkeypatch):
    main = _load(monkeypatch)
    assert main.ENDPOINTS["P1"]["user"] == DIRECT_USER
    assert main.ENDPOINTS["P2"]["user"] == POOLER_USER


def test_the_pooler_username_is_not_the_direct_username(monkeypatch):
    """If these ever collapse to one value the pooler cannot authenticate."""
    main = _load(monkeypatch)
    assert main.ENDPOINTS["P1"]["user"] != main.ENDPOINTS["P2"]["user"]


def _capture_connect_user(monkeypatch, main, endpoint):
    """Drive a run to the auth stage and record the username pg8000 received."""
    seen = {}

    class _Conn:
        def cursor(self):
            raise RuntimeError("stop after auth")

        def close(self):
            pass

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return _Conn()

    fake = types.ModuleType("pg8000")
    fake.dbapi = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "pg8000", fake)
    monkeypatch.setitem(sys.modules, "pg8000.dbapi", fake.dbapi)

    # skip the network stages; only the auth call is under test
    monkeypatch.setattr(main, "stage_dns",
                        lambda *a, **k: {"outcome": "pass", "ms": 1.0})
    monkeypatch.setattr(main, "stage_tcp",
                        lambda *a, **k: ({"outcome": "pass", "ms": 1.0}, None))
    monkeypatch.setattr(main, "stage_tls",
                        lambda *a, **k: {"outcome": "pass", "ms": 1.0})

    main._run(endpoint, main.ENDPOINTS[endpoint])
    return seen


def test_p1_receives_only_the_direct_username(monkeypatch):
    main = _load(monkeypatch)
    seen = _capture_connect_user(monkeypatch, main, "P1")
    assert seen.get("user") == DIRECT_USER
    assert seen.get("user") != POOLER_USER


def test_p2_receives_only_the_pooler_username(monkeypatch):
    main = _load(monkeypatch)
    seen = _capture_connect_user(monkeypatch, main, "P2")
    assert seen.get("user") == POOLER_USER
    assert seen.get("user") != DIRECT_USER


def test_the_probe_no_longer_reads_a_single_pguser(monkeypatch):
    """A stray ``PGUSER`` must not silently configure anything."""
    env = {k: v for k, v in ENV.items()}
    env.pop("PGUSER_DIRECT")
    env["PGUSER"] = "legacy_single_user"
    main = _load(monkeypatch, env)
    assert "P1" not in main.ENDPOINTS, "PGUSER must not stand in for PGUSER_DIRECT"


@pytest.mark.parametrize("drop", ["PGHOST_DIRECT", "PGUSER_DIRECT"])
def test_a_half_configured_endpoint_does_not_materialise(monkeypatch, drop):
    """Host without user, or user without host, is a config error, not an endpoint.

    Materialising it would produce an auth failure that reads like a platform
    finding -- exactly the confusion this probe exists to prevent.
    """
    env = {k: v for k, v in ENV.items() if k != drop}
    main = _load(monkeypatch, env)
    assert "P1" not in main.ENDPOINTS
    assert "P2" in main.ENDPOINTS


@pytest.mark.parametrize("username", [DIRECT_USER, POOLER_USER])
def test_no_username_appears_in_a_response_or_log(monkeypatch, username):
    main = _load(monkeypatch)

    def boom(*a, **k):
        raise OSError(-2, f"lookup failed for {DIRECT_HOST} user={username} "
                          f"password={PASSWORD}")

    monkeypatch.setattr(main.socket, "getaddrinfo", boom)
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                      headers={"X-Probe-Token": TOKEN})
    combined = r.text + "\n" + buf.getvalue()
    assert username not in combined
    assert PASSWORD not in combined


# ============================================= 2. configuration is provable
def test_healthz_reports_the_configured_endpoint_keys(monkeypatch):
    main = _load(monkeypatch)
    body = TestClient(main.app).get("/healthz").json()
    assert body["configured_endpoints"] == ["P1", "P2"]
    assert body["endpoint_count"] == 2


def test_healthz_reports_an_empty_table_honestly(monkeypatch):
    """The attempt-2 situation, made visible BEFORE a probe is run."""
    main = _load(monkeypatch, {"PROBE_TOKEN": TOKEN, "PGPASSWORD": PASSWORD,
                               "PGDATABASE": "probe_db"})
    body = TestClient(main.app).get("/healthz").json()
    assert body["configured_endpoints"] == []
    assert body["endpoint_count"] == 0


def test_healthz_carries_a_configuration_timestamp(monkeypatch):
    """Proves WHICH process answered, so a stale instance is identifiable."""
    main = _load(monkeypatch)
    body = TestClient(main.app).get("/healthz").json()
    stamp = body["configuration_loaded_at"]
    assert stamp and stamp.endswith("+00:00")


def test_healthz_still_carries_the_ca_fingerprint(monkeypatch):
    main = _load(monkeypatch)
    body = TestClient(main.app).get("/healthz").json()
    assert body["ca_bundle_loaded"] is True
    assert len(body["ca_bundle_sha256"]) == 64


@pytest.mark.parametrize("forbidden", [
    DIRECT_HOST, POOLER_HOST, DIRECT_USER, POOLER_USER, PASSWORD, TOKEN,
    "probe_db", "postgresql://",
])
def test_healthz_leaks_no_host_username_credential_or_dsn(monkeypatch, forbidden):
    main = _load(monkeypatch)
    text = TestClient(main.app).get("/healthz").text
    assert forbidden not in text


def test_healthz_publishes_keys_but_never_values(monkeypatch):
    """Endpoint KEYS are safe to publish; everything behind them is not."""
    main = _load(monkeypatch)
    body = TestClient(main.app).get("/healthz").json()
    flat = repr(body)
    assert "P1" in flat and "P2" in flat
    for value in (DIRECT_HOST, POOLER_HOST, DIRECT_USER, POOLER_USER):
        assert value not in flat


# ==================================== 3. a refusal to run is not a result
class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        import json
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_helper(monkeypatch, tmp_path, health_body, probe_body=None, status=200):
    monkeypatch.setattr(probe_invoke, "health",
                        lambda url: {"http_status": status, "body": health_body})
    monkeypatch.setattr(probe_invoke, "call",
                        lambda url, ep, tok: {"http_status": 200,
                                              "body": probe_body or {}})
    monkeypatch.setattr(sys, "argv",
                        ["probe_invoke.py", "--base-url", "https://example.invalid",
                         "--out", str(tmp_path), "--token-stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("dummy-token\n"))
    return probe_invoke.main()


HEALTHY = {"ca_bundle_loaded": True, "ca_bundle_sha256": "a" * 64,
           "configured_endpoints": ["P1", "P2"], "endpoint_count": 2,
           "configuration_loaded_at": "2026-08-30T16:00:00+00:00"}


def test_the_helper_refuses_when_an_endpoint_is_missing(monkeypatch, tmp_path, capsys):
    body = dict(HEALTHY, configured_endpoints=["P1"], endpoint_count=1)
    rc = _run_helper(monkeypatch, tmp_path, body)
    assert rc == 3
    assert not list(tmp_path.iterdir()), "no evidence file may be written on a refusal"
    assert "ABORTED" in capsys.readouterr().err


def test_the_helper_refuses_when_no_endpoint_is_configured(monkeypatch, tmp_path):
    body = dict(HEALTHY, configured_endpoints=[], endpoint_count=0)
    assert _run_helper(monkeypatch, tmp_path, body) == 3
    assert not list(tmp_path.iterdir())


def test_the_helper_refuses_when_the_ca_did_not_load(monkeypatch, tmp_path):
    body = dict(HEALTHY, ca_bundle_loaded=False)
    assert _run_helper(monkeypatch, tmp_path, body) == 3
    assert not list(tmp_path.iterdir())


def test_the_helper_refuses_against_a_build_without_the_gate_fields(monkeypatch, tmp_path):
    """An older deployed build cannot prove its configuration, so it is refused."""
    body = {"ca_bundle_loaded": True, "ca_bundle_sha256": "a" * 64}
    assert _run_helper(monkeypatch, tmp_path, body) == 3
    assert not list(tmp_path.iterdir())


def test_endpoint_not_configured_aborts_instead_of_becoming_evidence(monkeypatch, tmp_path):
    """The exact attempt-2 artefact, now impossible to produce."""
    rc = _run_helper(monkeypatch, tmp_path, HEALTHY,
                     probe_body={"detail": {"code": "ENDPOINT_NOT_CONFIGURED",
                                            "endpoint": "P1"}})
    assert rc == 3
    assert not list(tmp_path.iterdir()), \
        "a 404 with no stage executed must never be written as a result"


def test_a_healthy_configuration_is_allowed_to_run(monkeypatch, tmp_path):
    """The gate must not be so strict that a correct deployment cannot proceed."""
    rc = _run_helper(monkeypatch, tmp_path, HEALTHY,
                     probe_body={"probe": "P1", "q_a": "pass", "q_b": "unresolved",
                                 "stages": {"dns": {"outcome": "pass"}}})
    assert rc == 0
    written = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.startswith("P1-") for n in written)
    assert any(n.startswith("P2-") for n in written)
