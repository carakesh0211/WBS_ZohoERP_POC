"""Regression tests for the pinned CA bundle.

These exist because of a specific, costly defect. The first build wrote::

    if os.path.isfile(bundle):
        ctx.load_verify_locations(cafile=bundle)

so when `ca-bundle.pem` was not packaged, verification silently fell back to
the system trust store. Supabase presents its own CA, so the omission surfaced
only as a verification failure against the live endpoint -- indistinguishable
from a Catalyst egress problem or a network fault. It consumed a whole timed
exposure window before the cause was isolated.

Every test below holds one edge of "that cannot happen again":

  1. the pinned file is the ONLY trust anchor, and is genuinely loaded;
  2. absent / empty / malformed / expired all fail CLOSED, never degrade;
  3. hostname verification stays on;
  4. verification cannot be switched off anywhere in the probe;
  5. a CA failure leaks no path, filename, certificate body or secret;
  6. the endpoint allowlist and port 5432 are unaffected by any of it.

Run:  python -m pytest docs/phase-0b/probe/test_ca_bundle.py -q
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import io
import ssl
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))

import ca as ca_mod  # noqa: E402

VALID_CA = PROBE_DIR / "fixtures" / "valid-ca.pem"
EXPIRED_CA = PROBE_DIR / "fixtures" / "expired-ca.pem"

TOKEN = "test-token-Zx9Q2mK7vL4pR8sT1wY6bN3dF5gH0jC"
PASSWORD = "super-secret-database-password-DO-NOT-LEAK"
DIRECT_HOST = "db.exampleproject.supabase.co"
ENV = {
    "PROBE_TOKEN": TOKEN,
    "PGHOST_DIRECT": DIRECT_HOST,
    "PGHOST_POOLER": "aws-0-ap-south-1.pooler.supabase.com",
    # Two usernames, not one: the session pooler routes on
    # <role>.<project_ref> while a direct connection uses the bare role.
    "PGUSER_DIRECT": "probe_ephemeral",
    "PGUSER_POOLER": "probe_ephemeral.exampleprojectref",
    "PGPASSWORD": PASSWORD,
    "PGDATABASE": "probe_db",
}


def _load_main(monkeypatch, ca_path):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ca_mod, "CA_BUNDLE_PATH", str(ca_path))
    return importlib.reload(importlib.import_module("main"))


# ---------------------------------------------------- 1. the file is loaded
def test_the_pinned_bundle_is_actually_loaded_into_the_context():
    ctx = ca_mod.verified_context(str(VALID_CA))
    subjects = [
        {k: v for part in c.get("subject", ()) for k, v in part}.get("commonName")
        for c in ctx.get_ca_certs()
    ]
    assert any("WBS Phase 0B VALID test CA" in (s or "") for s in subjects), subjects


def test_the_pinned_bundle_is_the_only_trust_anchor():
    """Pinning, not augmentation.

    `create_default_context(cafile=...)` suppresses `load_default_certs()`. If
    that ever regressed, this context would carry the machine's system roots
    and the probe would trust anything publicly signed.
    """
    ctx = ca_mod.verified_context(str(VALID_CA))
    assert len(ctx.get_ca_certs()) == 1, "system trust store leaked into the pinned context"


def test_the_summary_fingerprint_matches_the_file_on_disk():
    expected = hashlib.sha256(VALID_CA.read_bytes()).hexdigest()
    assert ca_mod._ca_summary(str(VALID_CA))["sha256"] == expected


def test_the_summary_reports_subject_issuer_and_expiry():
    cert = ca_mod._ca_summary(str(VALID_CA))["certificates"][0]
    assert cert["subject_cn"] and cert["issuer_cn"]
    assert cert["not_before"] and cert["not_after"]
    assert cert["currently_valid"] is True


def test_the_summary_never_returns_certificate_bytes():
    body = VALID_CA.read_text()
    inner = "".join(body.split("-----")[2].split())[:40]
    blob = repr(ca_mod._ca_summary(str(VALID_CA)))
    assert inner not in blob
    assert "BEGIN CERTIFICATE" not in blob


# ---------------------------------------------------- 2. fails closed
@pytest.mark.parametrize(
    "content, reason",
    [
        (None, "ca_bundle_missing"),
        (b"", "ca_bundle_empty"),
        (b"   \n\t  \n", "ca_bundle_empty"),
        (b"this is not a certificate", "ca_bundle_malformed"),
        (b"-----BEGIN CERTIFICATE-----\nbm90LWEtY2VydA==\n-----END CERTIFICATE-----\n",
         "ca_bundle_malformed"),
    ],
)
def test_an_unusable_bundle_fails_closed(tmp_path, content, reason):
    target = tmp_path / "ca-bundle.pem"
    if content is not None:
        target.write_bytes(content)
    with pytest.raises(ca_mod.CaBundleUnusable) as exc:
        ca_mod._ca_summary(str(target))
    assert str(exc.value) == reason


def test_an_expired_bundle_fails_closed():
    with pytest.raises(ca_mod.CaBundleUnusable) as exc:
        ca_mod._ca_summary(str(EXPIRED_CA))
    assert str(exc.value) == "ca_bundle_expired"


def test_verified_context_raises_rather_than_falling_back_to_system_cas(tmp_path):
    """The exact regression. A missing file must not yield a usable context."""
    with pytest.raises(ca_mod.CaBundleUnusable):
        ca_mod.verified_context(str(tmp_path / "absent.pem"))


def test_the_probe_refuses_to_run_when_the_ca_is_unusable(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path / "absent.pem")
    assert main.CA_BUNDLE is None and main.CA_BUNDLE_ERROR == "ca_bundle_missing"
    r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                  headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CA_BUNDLE_UNUSABLE"


def test_the_ca_refusal_performs_no_network_activity(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path / "absent.pem")

    def explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("network touched despite an unusable CA")

    monkeypatch.setattr(main.socket, "getaddrinfo", explode)
    monkeypatch.setattr(main.socket, "create_connection", explode)
    r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                  headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 503


def test_an_unauthorised_caller_learns_nothing_about_ca_state(monkeypatch, tmp_path):
    """The 401 must precede the 503, or the endpoint becomes a packaging oracle."""
    main = _load_main(monkeypatch, tmp_path / "absent.pem")
    r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                  headers={"X-Probe-Token": "wrong"})
    assert r.status_code == 401
    assert "CA_BUNDLE" not in r.text


def test_healthz_surfaces_a_broken_bundle(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path / "absent.pem")
    body = TestClient(main.app).get("/healthz").json()
    assert body["ca_bundle_loaded"] is False
    assert body["ca_bundle_error"] == "ca_bundle_missing"


# ---------------------------------------------------- 3 & 4. verification stays on
def test_hostname_verification_is_enabled():
    assert ca_mod.verified_context(str(VALID_CA)).check_hostname is True


def test_certificate_verification_is_required():
    assert ca_mod.verified_context(str(VALID_CA)).verify_mode is ssl.CERT_REQUIRED


def test_tls_floor_is_1_2():
    assert ca_mod.verified_context(str(VALID_CA)).minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("forbidden", [
    "CERT_NONE",
    "check_hostname = False",
    "check_hostname=False",
    "_create_unverified_context",
    "CERT_OPTIONAL",
    "sslmode=disable",
])
@pytest.mark.parametrize("source", ["main.py", "ca.py", "build_bundle.py"])
def test_verification_can_never_be_disabled_anywhere(source, forbidden):
    text = (PROBE_DIR / source).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert forbidden not in code, f"{forbidden} present in {source}"


def test_pg8000_receives_the_same_verified_context():
    """A verified raw handshake does not license an unverified driver connection."""
    src = (PROBE_DIR / "main.py").read_text(encoding="utf-8")
    start = src.index("def stage_auth_and_query")
    body = src[start:src.index("# ---", start + 10)]
    assert "ctx = verified_context()" in body
    assert "ssl_context=ctx" in body


# ---------------------------------------------------- 5. no leakage
@pytest.mark.parametrize("secret", [TOKEN, PASSWORD, DIRECT_HOST, "probe_ephemeral"])
def test_a_ca_failure_leaks_no_secret(monkeypatch, tmp_path, secret):
    main = _load_main(monkeypatch, tmp_path / "absent.pem")
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                      headers={"X-Probe-Token": TOKEN})
    assert secret not in r.text
    assert secret not in buf.getvalue()


def test_a_ca_failure_leaks_no_path_or_filename(tmp_path):
    secret_dir = tmp_path / "s3cr3t-directory"
    secret_dir.mkdir()
    with pytest.raises(ca_mod.CaBundleUnusable) as exc:
        ca_mod._ca_summary(str(secret_dir / "ca-bundle.pem"))
    assert "s3cr3t-directory" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_a_malformed_bundle_leaks_no_openssl_detail(tmp_path):
    """OpenSSL's own error text embeds the file path; `from None` must sever it."""
    bad = tmp_path / "leaky-path-name.pem"
    bad.write_bytes(b"not a certificate at all")
    with pytest.raises(ca_mod.CaBundleUnusable) as exc:
        ca_mod._ca_summary(str(bad))
    assert exc.value.__cause__ is None
    assert "leaky-path-name" not in str(exc.value)


def test_a_ca_failure_response_carries_no_certificate_body(monkeypatch):
    main = _load_main(monkeypatch, EXPIRED_CA)
    r = TestClient(main.app).post("/probe", json={"endpoint": "P1"},
                                  headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 503
    assert "BEGIN CERTIFICATE" not in r.text


# ---------------------------------------------------- 6. allowlist intact
def test_endpoint_keys_remain_exactly_p1_and_p2(monkeypatch):
    main = _load_main(monkeypatch, VALID_CA)
    assert set(main.ENDPOINTS) == {"P1", "P2"}
    assert main.ProbeRequest.model_fields["endpoint"].annotation.__args__ == ("P1", "P2")


def test_every_configured_endpoint_is_port_5432(monkeypatch):
    main = _load_main(monkeypatch, VALID_CA)
    assert {e["port"] for e in main.ENDPOINTS.values()} == {5432}


@pytest.mark.parametrize("payload", [
    {"endpoint": "P3"},
    {"endpoint": "P1", "port": 6543},
    {"endpoint": "P1", "host": "169.254.169.254"},
    {"endpoint": "http://169.254.169.254/latest/meta-data/"},
])
def test_the_ca_work_did_not_widen_the_request_surface(monkeypatch, payload):
    main = _load_main(monkeypatch, VALID_CA)
    r = TestClient(main.app).post("/probe", json=payload,
                                  headers={"X-Probe-Token": TOKEN})
    assert r.status_code == 422


def _docstrings(tree):
    """Every docstring node in a module, so prose can be excluded from code checks."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def test_port_6543_is_unreachable_in_executable_code():
    """AST, not grep.

    The transaction pooler is discussed in the module docstring on purpose -- a
    reader must know it is deliberately excluded. Prose is not reachability, so
    the assertion is about literals the interpreter can actually evaluate.
    """
    for source in ("main.py", "ca.py"):
        tree = ast.parse((PROBE_DIR / source).read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in skip:
                assert "6543" not in str(node.value), (
                    f"port 6543 is reachable in {source} line {node.lineno}")


# ------------------------------------------------- stage-skip attribution
def test_upstream_failure_is_not_reported_as_a_deadline_skip(monkeypatch):
    """A DNS failure must not make later stages read `skipped_deadline`.

    Caught by inspecting real evidence output, not by a review. The two are
    different findings -- out of time versus never eligible -- and conflating
    them is the ambiguity this probe exists to remove.
    """
    main = _load_main(monkeypatch, VALID_CA)

    def no_such_host(*a, **k):
        raise OSError(11001, "getaddrinfo failed")

    monkeypatch.setattr(main.socket, "getaddrinfo", no_such_host)
    body = TestClient(main.app).post(
        "/probe", json={"endpoint": "P1"},
        headers={"X-Probe-Token": TOKEN}).json()

    assert body["stages"]["dns"]["outcome"] == "fail"
    for later in ("tcp", "tls", "auth", "query"):
        assert body["stages"][later]["outcome"] == "skipped_upstream_failure", later


# ------------------------------------------------- in-window build is offline
NETWORK_MODULES = {
    "urllib", "urllib2", "urllib3", "http", "httplib", "socket", "ssl_socket",
    "requests", "httpx", "ftplib", "telnetlib", "smtplib", "asyncio",
    "subprocess", "pip",
}


def _imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_in_window_build_cannot_reach_the_network():
    """The build that runs inside the exposure window must be purely offline.

    Dependency resolution happens beforehand, in `vendor_deps.py`, which is
    allowed to use pip and the network. `build_bundle.py` only adds the CA and
    zips a pre-staged tree -- if it could download, a slow or unavailable index
    would burn exposure minutes, and the artefact would stop being reproducible
    from the pinned inventory.
    """
    used = _imported_modules(PROBE_DIR / "build_bundle.py")
    offenders = sorted(used & NETWORK_MODULES)
    assert not offenders, f"build_bundle.py imports network-capable modules: {offenders}"


def test_the_runtime_ca_module_is_stdlib_and_offline():
    """`ca.py` is imported by the build gate; it must not drag in a network stack."""
    used = _imported_modules(PROBE_DIR / "ca.py")
    offenders = sorted(used & (NETWORK_MODULES - {"ssl_socket"}))
    assert not offenders, f"ca.py imports network-capable modules: {offenders}"


def test_the_vendoring_step_is_separate_from_the_build():
    """Resolution and assembly are different programs, deliberately.

    Keeping them apart is what lets the build be offline while the vendoring
    step is free to use pip. If they merged, the in-window step would inherit
    the network dependency.
    """
    assert (PROBE_DIR / "vendor_deps.py").is_file()
    vendoring = _imported_modules(PROBE_DIR / "vendor_deps.py")
    assert "subprocess" in vendoring, "vendor_deps.py is expected to shell out to pip"
    build = _imported_modules(PROBE_DIR / "build_bundle.py")
    assert "subprocess" not in build
