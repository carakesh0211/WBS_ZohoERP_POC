"""Database-free tests for tools/appsail/scan_bundle.py.

Builds small synthetic .zip archives -- a clean one shaped like the real
AppSail bundle `tools/appsail/build_uat_bundle.py` produces, and one archive
per offending file, one rule at a time -- rather than depending on a real
build (which needs a resolved wheel closure and is out of scope for a
database-free, no-network test run).
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.appsail.scan_bundle import CREDENTIAL_RE, main, scan  # noqa: E402

CLEAN_WHEEL_TEXT = (
    "Wheel-Version: 1.0\n"
    "Generator: bdist_wheel (0.42.0)\n"
    "Root-Is-Purelib: false\n"
    "Tag: cp313-cp313-manylinux_2_17_x86_64\n"
)

# Real lines from this codebase that mention credential-shaped KEYWORDS
# without ever carrying a value -- the exact patterns
# tools/appsail/scan_bundle.py's module docstring says must never fire.
BENIGN_CREDENTIAL_LOOKALIKES = "\n".join([
    '        return {"state": "REFUSED", "refresh_token_present": False,',
    '            "refresh_token": token,}',
    "  client_secret_ref TEXT,",
    '    password_secret_name: str = "CAPEX_DB_PASSWORD"',
    "    # user_id, roles  (password = user_id + '!demo')",
    '            headers={**headers, "Authorization": f"Zoho-oauthtoken {self._bearer()}"})',
    r'    r"(Zoho-oauthtoken|Bearer|Basic)\s+[A-Za-z0-9._\-+/=]+"',
    '    "client_secret_present": None,',
    "    password = source.get(self.password_secret_name)",
])


def _write_clean_archive(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.py", "import app.backend.main\n")
        z.writestr("app-config.json", '{"stack": "python_3_13", "command": "python3 -u main.py"}\n')
        z.writestr("requirements.txt", "# informational only\n")
        z.writestr("app/backend/main.py", "def index():\n    return 'ok'\n")
        z.writestr("app/backend/lookalikes.py", BENIGN_CREDENTIAL_LOOKALIKES + "\n")
        z.writestr("app/frontend/index.html", "<html><body>hi</body></html>\n")
        z.writestr("migrations/pg/001_foundation.sql", "CREATE TABLE entity (id text);\n")
        z.writestr("research/30_contracts/C3_statuses.json", "{}\n")
        z.writestr("research/20_verified/zoho_endpoint_inventory.json", "{}\n")
        z.writestr("research/20_verified/openapi_findings.json", "{}\n")
        z.writestr("uat_seed.db", b"sqlite-bytes-not-a-real-db")
        z.writestr("uat-credentials.json",
                   '{"users": {"U-REQ": {"salt": "ab12", "hash": "cd34"}}}\n')
        z.writestr("vendor/pydantic_core/__init__.py", "# extracted wheel contents\n")
        z.writestr("vendor/pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so", b"\x7fELF")
        z.writestr("vendor/pydantic_core-2.14.0.dist-info/WHEEL", CLEAN_WHEEL_TEXT)
        z.writestr("vendor/certifi/__init__.py", "# pure python\n")
        z.writestr("vendor/certifi-2024.2.2.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


# ---------------------------------------------------------------- the clean shape passes
def test_clean_archive_passes_with_no_findings(tmp_path):
    archive = _write_clean_archive(tmp_path / "clean.zip")
    findings, summary = scan(archive)
    assert findings == [], [str(f) for f in findings]
    assert summary["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert summary["files"] > 0
    assert any(w["tags"] == ["cp313-cp313-manylinux_2_17_x86_64"] for w in summary["wheels"])
    assert any(w["tags"] == ["py3-none-any"] for w in summary["wheels"])


def test_main_returns_zero_and_prints_summary_for_a_clean_archive(tmp_path, capsys):
    archive = _write_clean_archive(tmp_path / "clean.zip")
    rc = main([str(archive)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "archive_sha256" in out


# ---------------------------------------------------------------- one offence, one rule
def test_credential_shape_password_literal(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "app/backend/leaky.py": b'ADMIN_PASSWORD_OVERRIDE = "hunter2plain"\n'
                               b'password = "hunter2plain"\n',
    })
    findings, _ = scan(archive)
    codes = {f["code"] for f in findings}
    assert "CREDENTIAL_SHAPE" in codes
    assert any(f["path"] == "app/backend/leaky.py" for f in findings)


def test_credential_shape_pg_dsn(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "app/backend/leaky_dsn.py": b"DSN = 'postgresql://capex:s3cretpw@db.internal:5432/capex'\n",
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings)


def test_credential_shape_pgpassword_and_env_style(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "app/backend/leaky_env.py": b"PGPASSWORD\n",
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings)

    archive2 = _write_clean_archive(tmp_path / "bad2.zip", {
        "app/backend/leaky_env2.py": b"CAPEX_DB_PASSWORD=hunter2plainvalue\n",
    })
    findings2, _ = scan(archive2)
    assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings2)


def test_credential_shape_refresh_token_and_client_secret_with_real_values(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "app/backend/leaky_oauth.py":
            b'refresh_token = "1//0abcDEFghijKLMNOPqrstUVWXYZ0123456789"\n'
            b'client_secret = "abcdefghijklmnopqrstuvwx0123456789"\n',
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings)


def test_credential_shape_zoho_oauthtoken_header(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "app/backend/leaky_header.py":
            b'headers["Authorization"] = "Zoho-oauthtoken 6e80aa11bb22cc33dd44ee55ff6677889900"\n',
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings)


def test_credential_shape_aws_github_slack_shapes_still_caught(tmp_path):
    for content in (b"AKIAABCDEFGHIJKLMNOP\n",
                    b"ghp_" + b"a" * 36 + b"\n",
                    b"xoxb-" + b"1" * 12 + b"\n",
                    b"-----BEGIN RSA PRIVATE KEY-----\n"):
        archive = _write_clean_archive(tmp_path / "bad.zip", {"app/backend/leaky_x.py": content})
        findings, _ = scan(archive)
        assert any(f["code"] == "CREDENTIAL_SHAPE" for f in findings), content


def test_credential_shape_skips_vendor_text_files():
    """Third-party wheel contents are not this codebase's secret to leak;
    scanning them for credential shapes would only flag upstream fixtures
    and test data this project does not control."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        archive = _write_clean_archive(Path(td) / "bad.zip", {
            "vendor/somepkg/fixtures.py": b'password = "hunter2plain"\n',
        })
        findings, _ = scan(archive)
        assert not any(f["path"].startswith("vendor/") and f["code"] == "CREDENTIAL_SHAPE"
                       for f in findings)


def test_windows_binary(tmp_path):
    for name in ("vendor/pydantic_core/_pydantic_core.pyd",
                 "vendor/somepkg/lib.dll", "main_helper.exe"):
        archive = _write_clean_archive(tmp_path / "bad.zip", {name: b"MZ\x90\x00"})
        findings, _ = scan(archive)
        assert any(f["code"] == "WINDOWS_BINARY" and f["path"] == name for f in findings), name


def test_non_manylinux_wheel_via_dist_info_tag(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "vendor/psycopg_binary-3.1.18.dist-info/WHEEL":
            "Wheel-Version: 1.0\nTag: cp313-cp313-win_amd64\n",
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "NON_MANYLINUX_WHEEL" for f in findings)


def test_non_manylinux_wheel_via_raw_whl_filename(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {
        "vendor/psycopg_binary-3.1.18-cp313-cp313-win_amd64.whl": b"PK\x03\x04",
    })
    findings, _ = scan(archive)
    assert any(f["code"] == "NON_MANYLINUX_WHEEL" for f in findings)


def test_macosx_and_musllinux_wheel_tags_also_refused(tmp_path):
    for tag in ("cp313-cp313-macosx_11_0_arm64", "cp313-cp313-musllinux_1_1_x86_64"):
        archive = _write_clean_archive(tmp_path / "bad.zip", {
            "vendor/somepkg.dist-info/WHEEL": f"Tag: {tag}\n",
        })
        findings, _ = scan(archive)
        assert any(f["code"] == "NON_MANYLINUX_WHEEL" for f in findings), tag


def test_unexpected_top_level(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {"stray_dir/file.txt": b"hi\n"})
    findings, _ = scan(archive)
    assert any(f["code"] == "UNEXPECTED_TOP_LEVEL" and f["path"] == "stray_dir" for f in findings)


def test_forbidden_paths(tmp_path):
    for name in (".claude/settings.local.json", ".git/config", "app/__pycache__/main.cpython-313.pyc",
                "app/.env", "app/backend/settings.local.json"):
        archive = _write_clean_archive(tmp_path / "bad.zip", {name: b"x"})
        findings, _ = scan(archive)
        assert any(f["code"] == "FORBIDDEN_PATH" and f["path"] == name for f in findings), name


def test_archive_path_escape(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {"../evil.txt": b"x"})
    findings, _ = scan(archive)
    assert any(f["code"] == "ARCHIVE_PATH_ESCAPE" for f in findings)


def test_stray_database(tmp_path):
    archive = _write_clean_archive(tmp_path / "bad.zip", {"app/data/extra.db": b"x"})
    findings, _ = scan(archive)
    assert any(f["code"] == "STRAY_DATABASE" and f["path"] == "app/data/extra.db" for f in findings)


def test_stray_python_file(tmp_path):
    for name in ("stray_script.py", "research/extra_tool.py", "tools_leftover.py"):
        archive = _write_clean_archive(tmp_path / "bad.zip", {name: b"print('hi')\n"})
        findings, _ = scan(archive)
        assert any(f["code"] == "STRAY_PYTHON_FILE" and f["path"] == name for f in findings), name


def test_main_returns_nonzero_and_prints_coded_findings(tmp_path, capsys):
    archive = _write_clean_archive(tmp_path / "bad.zip", {".git/config": b"x"})
    rc = main([str(archive)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "FORBIDDEN_PATH" in err


def test_main_refuses_a_missing_file(tmp_path, capsys):
    rc = main([str(tmp_path / "does-not-exist.zip")])
    assert rc == 2
    assert "SCAN FAILED" in capsys.readouterr().err


# ---------------------------------------------------------------- the regex itself
def test_credential_re_does_not_match_the_lookalike_corpus():
    assert CREDENTIAL_RE.search(BENIGN_CREDENTIAL_LOOKALIKES.encode()) is None
