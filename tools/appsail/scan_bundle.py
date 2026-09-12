"""Scan a built AppSail UAT bundle archive and refuse anything the release
gate does not allow.

    python tools/appsail/scan_bundle.py <archive.zip>

Independent of `tools/appsail/build_uat_bundle.py`'s own gate (read that
file's checks first: its manifest, `verify_archive`, and the archive layout
`main.py`, `app-config.json`, `app/`, `migrations/`, `research/20_verified/`,
`uat_seed.db`, `uat-credentials.json`, `vendor/` wheels, `ca-bundle.pem`).
That script's checks run DURING assembly, against a temp directory it built
itself and controls end to end. This script opens an ALREADY-BUILT `.zip` --
handed off after the build step, copied, or built somewhere else entirely --
and re-checks it from scratch with no assumption that it came from a trusted
build, so a release candidate gets the same scrutiny no matter where the
archive came from.

Exits non-zero and prints one coded finding per line for every rule
violated. Finds nothing wrong: prints a summary (file count, the wheel
platform tags found, the archive's own SHA-256) and exits 0.

Findings, each independent -- an archive can carry more than one:

    CREDENTIAL_SHAPE      a password/token/private-key-shaped byte sequence
                          in a text file
    WINDOWS_BINARY        a `.exe`, `.dll` or `.pyd` anywhere in the archive
    NON_MANYLINUX_WHEEL   a wheel -- a `.whl` entry, or a `*.dist-info/WHEEL`
                          tag -- built for anything but `manylinux*` or `any`
    UNEXPECTED_TOP_LEVEL  an archive-root entry outside the expected set
    FORBIDDEN_PATH        `.claude/`, `.git/`, `__pycache__/`, `.env`, or
                          `settings.local.json` anywhere in the archive
    ARCHIVE_PATH_ESCAPE   an entry whose path would land outside the
                          extraction root (`../`, a leading `/`, a drive
                          letter)
    STRAY_DATABASE        a `.db` (or `.db-journal`) file other than
                          `uat_seed.db`
    STRAY_PYTHON_FILE     a `.py` file outside `app/`, `migrations/`,
                          `vendor/`, or the root `main.py`

The credential patterns extend `build_uat_bundle.py`'s `SECRET_RE` (AWS
access keys, GitHub/Slack tokens, a Zoho client id/secret's
`1000.<hex32>.<hex32>` shape, a PEM private key header) with generic ones a
release archive built from THIS codebase can actually carry without a false
alarm: this source discusses `refresh_token`, `client_secret` and
`Zoho-oauthtoken` constantly (redaction rules, DTOs, comments explaining what
is deliberately never stored) without ever assigning one a literal value, so
those three keywords only fire on an ACTUAL assignment -- `name` immediately
followed by `:`/`=` and then a quoted literal or a long unquoted token, never
on a dict key, an identifier suffix (`client_secret_ref`,
`refresh_token_present`) or a regex pattern string naming them. `password=`
matches only a quoted literal value, for the same reason: `password: str` is
a type hint and `password = body.password` is a variable, neither is a
secret. `PGPASSWORD` and an ALL-CAPS `..._PASSWORD=<value>` line (typical of
a shell/.env fragment) match as bare keywords, because neither shape occurs
anywhere in this codebase's own source today.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

# ------------------------------------------------------------------ expectations
EXPECTED_TOP_LEVEL = {
    "main.py", "app-config.json", "app", "migrations", "research",
    "uat_seed.db", "uat-credentials.json", "vendor", "requirements.txt",
    "ca-bundle.pem",   # Stage B only (the PostgreSQL provider's root CA); optional
}
ALLOWED_PY_PREFIXES = ("app/", "migrations/", "vendor/")
WINDOWS_BINARY_SUFFIXES = (".exe", ".dll", ".pyd")
FORBIDDEN_WHEEL_TAG_SUBSTRINGS = ("win_amd64", "win32", "macosx", "musllinux")
FORBIDDEN_PATH_RE = re.compile(
    r"(^|/)\.claude(/|$)|(^|/)\.git(/|$)|(^|/)__pycache__(/|$)"
    r"|(^|/)\.env($|[./])|(?:^|/)settings\.local\.json$")
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".sql", ".txt", ".md",
                 ".cfg", ".ini", ".toml", ".yml", ".yaml"}

# ------------------------------------------------------------------ credential shapes
CREDENTIAL_RE = re.compile(
    rb"(AKIA[0-9A-Z]{16}"                                  # AWS access key id
    rb"|gh[pos]_[A-Za-z0-9]{30,}"                           # GitHub token
    rb"|1000\.[0-9a-f]{32}\.[0-9a-f]{32}"                  # Zoho client id.secret shape
    rb"|-----BEGIN (?:RSA |EC )?PRIVATE KEY"                # PEM private key
    rb"|xox[baprs]-[0-9A-Za-z-]{10,}"                       # Slack token
    rb"|postgresql://[^/\s:'\"]+:[^/\s@'\"]+@"              # DSN with an embedded password
    rb"|PGPASSWORD\b"
    rb"|(?m:^[ \t]*[A-Z][A-Z0-9_]*_PASSWORD\s*=\s*\S+)"     # ALL_CAPS_PASSWORD=<value> (env/shell shape)
    rb"|password\s*=\s*['\"][^'\"]{3,}['\"]"                # password = "<literal>" (never `password:` -- a type hint)
    rb"|refresh_token\s*[:=]\s*(['\"][^'\"]{6,}['\"]|[A-Za-z0-9._-]{16,})"
    rb"|client_secret\s*[:=]\s*(['\"][^'\"]{6,}['\"]|[A-Za-z0-9._-]{16,})"
    rb"|Zoho-oauthtoken\s+[A-Za-z0-9._-]{10,}"
    rb")")


class Finding(dict):
    def __init__(self, code: str, path: str, detail: str):
        super().__init__(code=code, path=path, detail=detail)

    def __str__(self) -> str:
        return f"{self['code']}: {self['path']}: {self['detail']}"


def _top_level(name: str) -> str:
    return name.split("/", 1)[0]


def _check_path_escape(names: list[str]) -> list[Finding]:
    out = []
    for name in names:
        if name.startswith("/") or name.startswith("../") or "/../" in name or ":" in name:
            out.append(Finding("ARCHIVE_PATH_ESCAPE", name, "entry path escapes the archive root"))
    return out


def _check_forbidden_paths(names: list[str]) -> list[Finding]:
    return [Finding("FORBIDDEN_PATH", name, "matches a forbidden path pattern")
            for name in names if FORBIDDEN_PATH_RE.search(name)]


def _check_top_level(names: list[str]) -> list[Finding]:
    unexpected = sorted({_top_level(n) for n in names} - EXPECTED_TOP_LEVEL)
    return [Finding("UNEXPECTED_TOP_LEVEL", name, "not in the expected top-level set")
            for name in unexpected]


def _check_windows_binaries(names: list[str]) -> list[Finding]:
    return [Finding("WINDOWS_BINARY", name, "Windows/native binary suffix")
            for name in names if name.lower().endswith(WINDOWS_BINARY_SUFFIXES)]


def _check_stray_database(names: list[str]) -> list[Finding]:
    return [Finding("STRAY_DATABASE", name, "a database file other than uat_seed.db")
            for name in names
            if (name.endswith(".db") or name.endswith(".db-journal")) and name != "uat_seed.db"]


def _check_stray_python(names: list[str]) -> list[Finding]:
    out = []
    for name in names:
        if not name.endswith(".py"):
            continue
        if name == "main.py" or name.startswith(ALLOWED_PY_PREFIXES):
            continue
        out.append(Finding("STRAY_PYTHON_FILE", name,
                           "a .py file outside app/, migrations/, vendor/ or root main.py"))
    return out


_WHEEL_TAG_RE = re.compile(r"^Tag:\s*(\S+)\s*$", re.MULTILINE)


def _platform_of_tag(tag: str) -> str:
    """The platform component of a wheel compatibility tag
    `{python}-{abi}-{platform}`, e.g. `manylinux_2_17_x86_64` out of
    `cp313-cp313-manylinux_2_17_x86_64`."""
    return tag.rsplit("-", 1)[-1]


def _tag_is_forbidden(platform: str) -> bool:
    if platform == "any" or platform.startswith("manylinux"):
        return False
    return True


def _check_wheels(zf: zipfile.ZipFile, names: list[str]) -> tuple[list[Finding], list[dict]]:
    findings: list[Finding] = []
    report: list[dict] = []
    for name in names:
        if name.endswith(".whl"):
            # The platform tag is always the last `-`-separated component of
            # the filename, per the wheel filename spec (PEP 427): a wheel's
            # distribution name and version are normalised to carry no
            # literal hyphen, so this split is unambiguous.
            tag = Path(name).name[:-len(".whl")].rsplit("-", 1)[-1]
            if _tag_is_forbidden(tag):
                findings.append(Finding("NON_MANYLINUX_WHEEL", name,
                                        f"wheel filename tag {tag!r} is not manylinux/any"))
            report.append({"source": name, "tags": [tag]})
        elif name.endswith(".dist-info/WHEEL"):
            text = zf.read(name).decode("utf-8", errors="replace")
            tags = _WHEEL_TAG_RE.findall(text)
            package = name.split("/")[-2]
            report.append({"source": package, "tags": tags})
            for tag in tags:
                platform = _platform_of_tag(tag)
                if _tag_is_forbidden(platform):
                    findings.append(Finding("NON_MANYLINUX_WHEEL", name,
                                            f"tag {tag!r} is not manylinux/any"))
    return findings, report


def _check_credentials(zf: zipfile.ZipFile, names: list[str]) -> list[Finding]:
    findings = []
    for name in names:
        if name.startswith("vendor/"):
            continue   # third-party wheel contents; not this codebase's secret to leak
        if Path(name).suffix.lower() not in TEXT_SUFFIXES:
            continue
        data = zf.read(name)
        match = CREDENTIAL_RE.search(data)
        if match:
            findings.append(Finding("CREDENTIAL_SHAPE", name,
                                    f"matches a credential shape near byte {match.start()}"))
    return findings


def scan(archive: Path) -> tuple[list[Finding], dict]:
    """`(findings, summary)`. `findings` is empty exactly when the archive
    passes every rule; `summary` is printable either way."""
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        findings: list[Finding] = []
        findings += _check_path_escape(names)
        findings += _check_forbidden_paths(names)
        findings += _check_top_level(names)
        findings += _check_windows_binaries(names)
        findings += _check_stray_database(names)
        findings += _check_stray_python(names)
        wheel_findings, wheel_report = _check_wheels(zf, names)
        findings += wheel_findings
        findings += _check_credentials(zf, names)
    summary = {
        "archive": str(archive),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "files": len(names),
        "wheels": wheel_report,
    }
    return findings, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scan a built AppSail UAT bundle archive.")
    ap.add_argument("archive", help="the .zip built by tools/appsail/build_uat_bundle.py")
    args = ap.parse_args(argv)
    archive = Path(args.archive)
    if not archive.is_file():
        print(f"SCAN FAILED: no such file: {archive}", file=sys.stderr)
        return 2
    findings, summary = scan(archive)
    if findings:
        print(f"REFUSED: {len(findings)} finding(s)", file=sys.stderr)
        for f in findings:
            print(str(f), file=sys.stderr)
        return 2
    shown = dict(summary)
    shown["wheels"] = len(summary["wheels"])
    print(json.dumps(shown, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
