"""Fable 5.1 -- the hosted UAT preview profile (CAPEX_PROFILE=uat-preview).

What a publicly reachable preview must guarantee, each pinned here:

* the derivable `<user-id>!demo` passwords are NEVER installed under the
  profile, and the boot path refuses to start without a credentials file
  rather than falling back to them;
* the credentials file carries PBKDF2 salt+hash pairs only -- a plaintext
  `password` key, an unknown identity or a malformed digest is refused;
* the served sign-in page carries no password hint and a prominent
  `UAT — SYNTHETIC DATA — ERP MOCK` banner, while the local-demo profile
  serves index.html byte-for-byte (so the approved VRT baselines hold);
* the interactive API explorers are absent outside local-demo;
* `/api/admin/reset` stays refused (the profile enables nothing destructive);
* the bundle builder refuses Windows binaries, plaintext credentials, stray
  databases and secret-shaped text;
* the approved `--n500` hovered-row correction measures WCAG AA, recomputed
  from the stylesheet's own hex values.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backend import auth  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ----------------------------------------------------------------- helpers
def _cred_doc(tmp_path: Path, users: dict | None = None, **extra) -> Path:
    if users is None:
        users = {}
        for uid, _ in auth.DEV_USERS[:3]:
            salt, digest = auth.hash_password(f"pw-{uid}-x")
            users[uid] = {"salt": salt, "hash": digest}
    doc = {"format": "capex-uat-credentials/1", "users": users, **extra}
    p = tmp_path / "uat-credentials.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _uat_client(monkeypatch):
    """A TestClient of the SAME app object under the UAT profile."""
    monkeypatch.setenv("CAPEX_PROFILE", "uat-preview")
    from fastapi.testclient import TestClient
    from app.backend import main
    main._uat_index_cache.clear()
    return TestClient(main.app, raise_server_exceptions=False)


# ------------------------------------------------------ credential loading
def test_loader_refuses_a_plaintext_password_key(tmp_path):
    salt, digest = auth.hash_password("whatever")
    p = _cred_doc(tmp_path, users={"U-ADM": {"salt": salt, "hash": digest, "password": "whatever"}})
    with pytest.raises(auth.UatCredentialsError, match="plaintext password"):
        auth.load_uat_credentials(str(p))


@pytest.mark.parametrize("entry", [
    {"salt": "zz", "hash": "0" * 64},
    {"salt": "0" * 32, "hash": "not-hex"},
    {"salt": "0" * 32},
    "U-ADM!demo",
])
def test_loader_refuses_malformed_entries(tmp_path, entry):
    p = _cred_doc(tmp_path, users={"U-ADM": entry})
    with pytest.raises(auth.UatCredentialsError):
        auth.load_uat_credentials(str(p))


def test_loader_refuses_an_unknown_identity(tmp_path):
    salt, digest = auth.hash_password("x")
    p = _cred_doc(tmp_path, users={"U-NOBODY": {"salt": salt, "hash": digest}})
    with pytest.raises(auth.UatCredentialsError, match="unknown identity"):
        auth.load_uat_credentials(str(p))


def test_loader_refuses_a_missing_file(tmp_path):
    with pytest.raises(auth.UatCredentialsError, match="not found"):
        auth.load_uat_credentials(str(tmp_path / "absent.json"))
    with pytest.raises(auth.UatCredentialsError, match="not found"):
        auth.load_uat_credentials("")


# ------------------------------------------------------------ provisioning
def test_uat_provisioning_installs_hashes_and_disables_everyone_else(raw_con):
    auth.provision_dev_identities(raw_con)                       # the state a demo boot leaves
    salt, digest = auth.hash_password("correct horse")
    n = auth.provision_uat_identities(raw_con, {"U-ADM": {"salt": salt, "hash": digest}})
    assert n == 1
    row = raw_con.execute("SELECT password_salt, password_hash, disabled FROM app_credential "
                          "WHERE user_id='U-ADM'").fetchone()
    assert (row["password_salt"], row["password_hash"], row["disabled"]) == (salt, digest, 0)
    # The demo password no longer works for the provisioned user...
    with pytest.raises(auth.AuthError):
        auth.login(raw_con, "U-ADM", "U-ADM!demo")
    assert auth.login(raw_con, "U-ADM", "correct horse")["user"]["user_id"] == "U-ADM"
    # ...and every identity NOT in the file is disabled, not left on !demo.
    others = raw_con.execute("SELECT user_id FROM app_credential WHERE user_id<>'U-ADM' "
                             "AND disabled=0").fetchall()
    assert others == []
    with pytest.raises(auth.AuthError):
        auth.login(raw_con, "U-REQ", "U-REQ!demo")


def test_uat_provisioning_is_idempotent(raw_con):
    salt, digest = auth.hash_password("pw")
    creds = {"U-FIN": {"salt": salt, "hash": digest}}
    assert auth.provision_uat_identities(raw_con, creds) == 1
    assert auth.provision_uat_identities(raw_con, creds) == 1
    assert raw_con.execute("SELECT COUNT(*) FROM app_credential WHERE user_id='U-FIN'").fetchone()[0] == 1


# ----------------------------------------------------------------- boot path
def test_run_py_refuses_to_boot_the_uat_profile_without_credentials(capex_db, monkeypatch, capsys):
    from app import run
    monkeypatch.setenv("CAPEX_PROFILE", "uat-preview")
    monkeypatch.delenv(auth.UAT_CREDENTIALS_ENV, raising=False)
    calls = []
    monkeypatch.setattr(auth, "provision_dev_identities", lambda con, **kw: calls.append("demo"))
    monkeypatch.setattr(run, "check_database_ready", lambda: None)
    assert run.main([]) == 1
    assert calls == [], "the demo passwords must never be installed under the UAT profile"
    assert "REFUSING TO START" in capsys.readouterr().err
    # The fixture's template already holds the demo credentials (the local
    # test harness provisions them); a refused UAT boot must have touched none
    # of them -- and the process exits, so nothing is served on top of them.
    con = sqlite3.connect(capex_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM app_credential WHERE created_at > "
                           "(SELECT MIN(created_at) FROM app_credential)").fetchone()[0] == 0
    finally:
        con.close()


def test_run_py_provisions_uat_hashes_and_never_the_demo_scheme(capex_db, tmp_path, monkeypatch):
    from app import run
    salt, digest = auth.hash_password("pw-adm")
    p = _cred_doc(tmp_path, users={"U-ADM": {"salt": salt, "hash": digest}})
    monkeypatch.setenv("CAPEX_PROFILE", "uat-preview")
    monkeypatch.setenv(auth.UAT_CREDENTIALS_ENV, str(p))
    monkeypatch.setattr(run, "check_database_ready", lambda: None)
    demo_calls = []
    monkeypatch.setattr(auth, "provision_dev_identities", lambda con, **kw: demo_calls.append(1))
    served = {}

    class _Uvicorn:
        @staticmethod
        def run(*a, **kw):
            served.update(kw)
    monkeypatch.setitem(sys.modules, "uvicorn", _Uvicorn)
    assert run.main([]) == 0
    assert demo_calls == []
    assert served.get("port") == 8000
    con = sqlite3.connect(capex_db)
    con.row_factory = sqlite3.Row
    try:
        assert auth.login(con, "U-ADM", "pw-adm")["user"]["user_id"] == "U-ADM"
        with pytest.raises(auth.AuthError):
            auth.login(con, "U-ADM", "U-ADM!demo")
    finally:
        con.close()


# ------------------------------------------------------------- served page
def test_local_demo_serves_the_approved_index_html_byte_for_byte(client, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    on_disk = (ROOT / "app" / "frontend" / "index.html").read_bytes()
    assert client.get("/").content == on_disk


def test_uat_profile_serves_no_password_hint_and_a_prominent_banner(client, monkeypatch):
    c = _uat_client(monkeypatch)
    body = c.get("/").text
    assert "!demo" not in body
    assert "password is the user id" not in body.lower()
    assert 'id="loginHint"' in body                           # aria-describedby still resolves
    assert "UAT — SYNTHETIC DATA — ERP MOCK" in body
    assert body.count('class="uat-banner"') == 1
    assert body.index('class="uat-banner"') < body.index('id="loginForm"') if 'id="loginForm"' in body else True
    # Everything else on the page is the approved markup.
    on_disk = (ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
    stripped = re.sub(r'<p class="muted small" id="loginHint">.*?</p>', "", on_disk, flags=re.S)
    served_stripped = re.sub(r'<p class="muted small" id="loginHint">.*?</p>', "", body, flags=re.S)
    served_stripped = served_stripped.replace(
        '\n<div class="uat-banner" role="status" aria-label="Environment notice">'
        'UAT — SYNTHETIC DATA — ERP MOCK</div>', "")
    assert served_stripped == stripped


def test_uat_banner_css_is_tokens_only_and_declared():
    css = (ROOT / "app" / "frontend" / "extensions.css").read_text(encoding="utf-8")
    block = css[css.index(".uat-banner"):]
    block = block[:block.index("}") + 1]
    assert "#" not in block, "raw hex in the banner rule; the extension stylesheet is tokens-only"
    tokens = set(re.findall(r"var\(--([a-z0-9-]+)\)", block))
    declared = set(re.findall(r"--([a-z0-9-]+)\s*:", (ROOT / "app" / "frontend" / "styles.css").read_text(encoding="utf-8")))
    assert tokens <= declared, tokens - declared


def test_interactive_api_docs_are_absent_outside_local_demo(client, monkeypatch):
    c = _uat_client(monkeypatch)
    # Route registration happened at import under the test session's profile
    # (not local-demo), so the explorers must already be unmounted.
    from app.backend import main
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/docs" not in paths and "/redoc" not in paths
    assert c.get("/docs").status_code == 404
    assert c.get("/redoc").status_code == 404
    # The schema itself stays reachable: the SPA probes it without a session.
    assert c.get("/openapi.json").status_code == 200


def test_admin_reset_stays_refused_under_the_uat_profile(login, monkeypatch):
    admin = login("U-ADM")
    monkeypatch.setenv("CAPEX_PROFILE", "uat-preview")
    r = admin.post("/api/admin/reset")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "RESET_DISABLED"


def test_health_reports_the_uat_profile_and_mock_mode(client, monkeypatch):
    c = _uat_client(monkeypatch)
    body = c.get("/api/health").json()
    assert body["profile"] == "uat-preview"
    assert body["zoho_mode"] == "MOCK"


# ------------------------------------------------------------ the launcher
def test_uat_launcher_prepares_an_ephemeral_copy_of_the_seed(tmp_path, monkeypatch):
    spec_path = ROOT / "tools" / "appsail" / "uat_main.py"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "uat_seed.db").write_bytes(b"seed-bytes")
    (bundle / "uat-credentials.json").write_text("{}", encoding="utf-8")
    src = spec_path.read_text(encoding="utf-8")
    launcher = bundle / "main.py"
    launcher.write_text(src, encoding="utf-8")
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("CAPEX_UAT_SCRATCH_DIR", str(scratch))
    monkeypatch.setenv("CAPEX_DB_URL", "postgresql://should-be-dropped")
    monkeypatch.setenv("CAPEX_ERP_OUTBOUND_WRITES", "1")
    spec = importlib.util.spec_from_file_location("uat_launcher_under_test", launcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db_path = mod.prepare_environment()
    assert Path(db_path).read_bytes() == b"seed-bytes"
    assert Path(db_path).parent == scratch
    assert os.environ["CAPEX_PROFILE"] == "uat-preview"
    assert os.environ["CAPEX_DB_PATH"] == db_path
    assert os.environ[auth.UAT_CREDENTIALS_ENV] == str(bundle / "uat-credentials.json")
    assert "CAPEX_DB_URL" not in os.environ
    assert "CAPEX_ERP_OUTBOUND_WRITES" not in os.environ, "outbound ERP writes must be off in the preview"


def test_uat_launcher_refuses_a_bundle_without_the_seed(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "uat-credentials.json").write_text("{}", encoding="utf-8")
    launcher = bundle / "main.py"
    launcher.write_text((ROOT / "tools" / "appsail" / "uat_main.py").read_text(encoding="utf-8"),
                        encoding="utf-8")
    spec = importlib.util.spec_from_file_location("uat_launcher_no_seed", launcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit):
        mod.prepare_environment()


# ---------------------------------------------------------- the bundle gate
def test_bundle_verifier_refuses_windows_binaries_and_plaintext(tmp_path):
    sys.path.insert(0, str(ROOT / "tools" / "appsail"))
    import build_uat_bundle as b

    def archive(extra: dict[str, bytes]) -> Path:
        out = tmp_path / f"bundle-{len(extra)}-{hash(tuple(extra))}.zip"
        base = {
            "main.py": b"", "uat_seed.db": b"", "uat-credentials.json": b"{}",
            "app/run.py": b"", "app/backend/main.py": b"", "app/frontend/index.html": b"",
            "app/frontend/styles.css": b"", "migrations/pg/001_foundation.sql": b"",
            "app-config.json": json.dumps({"stack": b.STACK, "command": b.COMMAND}).encode(),
            "vendor/pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so": b"",
            "research/30_contracts/C3_statuses.json": b"{}",
            "research/30_contracts/C10_messages.json": b"{}",
            "research/30_contracts/C16_integration_statuses.json": b"{}",
            "research/30_contracts/C17_zoho_status_map.json": b"{}",
            "research/20_verified/zoho_endpoint_inventory.json": b"{}",
            "research/20_verified/openapi_findings.json": b"{}",
        }
        for m in b.REQUIRED_MODULES:
            base[f"vendor/{m}/__init__.py"] = b""
        base.update(extra)
        with zipfile.ZipFile(out, "w") as z:
            for k, v in base.items():
                z.writestr(k, v)
        return out

    assert b.verify_archive(archive({}))["files"] > 0
    with pytest.raises(b.BuildFailed, match="Windows binaries"):
        b.verify_archive(archive({"vendor/pydantic_core/_pydantic_core.cp313-win_amd64.pyd": b""}))
    with pytest.raises(b.BuildFailed, match="forbidden"):
        b.verify_archive(archive({"uat-users.txt": b"U-ADM\tsecret"}))
    with pytest.raises(b.BuildFailed, match="forbidden"):
        b.verify_archive(archive({".claude/settings.local.json": b"{}"}))
    with pytest.raises(b.BuildFailed, match="database files"):
        b.verify_archive(archive({"scratch/capex_copy.db": b""}))
    with pytest.raises(b.BuildFailed, match="secret-shaped"):
        b.verify_archive(archive({"app/backend/x.py": b"token = 'ghp_" + b"A" * 36 + b"'"}))
    with pytest.raises(b.BuildFailed, match="env_variables"):
        b.verify_archive(archive({"app-config.json": json.dumps(
            {"stack": b.STACK, "command": b.COMMAND, "env_variables": {"K": "v"}}).encode()}))
    with pytest.raises(b.BuildFailed, match="contiguous"):
        b.verify_archive(archive({"migrations/pg/003_gap.sql": b""}))


def test_bundle_builder_refuses_an_output_inside_the_repository(tmp_path):
    sys.path.insert(0, str(ROOT / "tools" / "appsail"))
    import build_uat_bundle as b
    with pytest.raises(b.BuildFailed, match="inside the repository"):
        b.build(tmp_path, tmp_path / "c.json", ROOT / "should-not-exist.zip")
    assert not (ROOT / "should-not-exist.zip").exists()


def test_credential_generator_refuses_the_repository_and_never_prints_a_password(tmp_path, capsys):
    sys.path.insert(0, str(ROOT / "tools" / "appsail"))
    import uat_credentials as uc
    with pytest.raises(uc.Refused):
        uc.generate(ROOT / "docs")
    out = uc.generate(tmp_path / "creds")
    captured = capsys.readouterr().out
    plain = (tmp_path / "creds" / uc.PLAINTEXT_NAME).read_text(encoding="utf-8")
    passwords = [line.split("\t")[1] for line in plain.splitlines()
                 if line and not line.startswith("#")]
    assert len(passwords) == len(auth.DEV_USERS) and all(len(p) >= 20 for p in passwords)
    for pw in passwords:
        assert pw not in captured and pw not in json.dumps(out)
    loaded = auth.load_uat_credentials(str(tmp_path / "creds" / uc.CREDENTIALS_NAME))
    assert set(loaded) == {uid for uid, _ in auth.DEV_USERS}
    # --check re-runs the loader and proves nothing is the derivable scheme.
    assert uc.check(tmp_path / "creds" / uc.CREDENTIALS_NAME)["users"] == sorted(loaded)
    salt, digest = auth.hash_password("U-ADM!demo")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"users": {"U-ADM": {"salt": salt, "hash": digest}}}), encoding="utf-8")
    with pytest.raises(uc.Refused, match="derivable"):
        uc.check(bad)


# ------------------------------------------------- the approved n500 change
def _luminance(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    def ch(v):
        c = int(v, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = ch(h[0:2]), ch(h[2:4]), ch(h[4:6])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 4)


def test_the_hovered_row_muted_text_correction_measures_aa():
    css = (ROOT / "app" / "frontend" / "styles.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css))
    before = _contrast(tokens["n500"], tokens["primary-50"])
    after = _contrast(tokens["n700"], tokens["primary-50"])
    assert before == 4.3210 and before < 4.5, "the defect this rule corrects has changed shape"
    assert after == 9.3528 and after >= 4.5
    assert re.search(r"tbody tr:hover \.muted, tbody tr:hover button\.tree-toggle \{ color:var\(--n700\); \}", css), \
        "the approved hover correction is missing from styles.css"
    assert "tbody tr:hover { background:var(--primary-50); }" in css, "the hover tint itself must not change"


# --------------------------------------------- runtime files in every package
def _research_dirs_named_in_backend() -> set[str]:
    """Every `research/<dir>` the backend names in CODE (not comments)."""
    found = set()
    for py in (ROOT / "app" / "backend").rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if '"research"' in code or "'research'" in code:
                m = re.search(r"""research["'\s,/\\]+(\d{2}_[a-z_]+)""", code)
                if m:
                    found.add(m.group(1))
    return found


def test_every_research_directory_the_backend_reads_ships_in_the_bundle_and_the_image():
    sys.path.insert(0, str(ROOT / "tools" / "appsail"))
    import build_uat_bundle as b
    named = _research_dirs_named_in_backend()
    assert {"20_verified", "30_contracts"} <= named, named
    shipped = {Path(p).parts[1] for p in b.RUNTIME_JSON} | {Path(p).parts[1] for p in b.RUNTIME_DIRS}
    assert named <= shipped, f"backend reads research/{sorted(named - shipped)} but the bundle does not ship it"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for d in named:
        assert re.search(rf"^COPY research/{d}", dockerfile, re.M), f"Dockerfile does not COPY research/{d}"
