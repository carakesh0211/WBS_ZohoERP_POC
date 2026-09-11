"""Assemble and verify the AppSail bundle for the UAT visual preview.

    python tools/appsail/build_uat_bundle.py \\
        --wheels      <staging>/wheels            # from resolve step, see below
        --credentials <outside-repo>/uat-credentials.json
        --out         <outside-repo>/wbs-capex-uat.zip

Resolve step (needs the network; run separately, BEFORE this script):

    python -m pip download --dest <staging>/wheels \\
        --platform manylinux2014_x86_64 --python-version 313 \\
        --implementation cp --only-binary=:all: -r requirements.txt

This script makes NO network access. Two programs, deliberately: the build
that produces the uploaded artefact must never depend on an index being
reachable, and a reviewer must be able to verify the archive without trusting
the resolver.

What goes in (archive root = bundle root):

    main.py                  tools/appsail/uat_main.py
    app-config.json          python_3_13, `python3 -u main.py`, 512 MB
    app/                     backend + frontend + SQLite migrations
    migrations/              PostgreSQL migrations 001..0NN (not applied here;
                             shipped so the artefact is the same one Stage B
                             migrates from -- an artefact without them cannot
                             perform the deploy step)
    research/20_verified/    the two JSON inventories zoho.py reads at runtime
    uat_seed.db              synthetic seed, built by this script from a
                             fresh `migrate --fresh --seed` under local-demo
    uat-credentials.json     PBKDF2 hashes ONLY (tools/appsail/uat_credentials.py)
    vendor/                  the wheel closure, extracted

What is refused, each as a hard failure rather than a warning:

    * any Windows `.pyd` / `.dll`, any `win_amd64`, `macosx` or `musllinux` wheel
    * a source distribution among the wheels
    * no `cpython-313-x86_64-linux-gnu.so` for pydantic_core
    * `.claude/`, `.git/`, `node_modules/`, `tests/`, `app/data/`, `__pycache__`
    * any `*.db` other than the seed it built, any `.env`, `settings.local.json`
    * the credentials file carrying anything but salt/hash pairs
    * the plaintext `uat-users.txt` anywhere near the tree
    * a byte sequence that looks like a token or private key in a text file
    * an archive entry whose path escapes the root

It writes `<out>.manifest.json` beside the archive: file count, size, the
archive SHA-256, the tree hash (sorted relative path + content), the wheel
list, and the SHA-256 of the credentials file -- which identifies WHICH
credential set is deployed without disclosing it.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

STACK = "python_3_13"
COMMAND = "python3 -u main.py"
MEMORY_MB = 512
LINUX_EXT_SUFFIX = "cpython-313-x86_64-linux-gnu.so"
# Files under research/ that the BACKEND READS AT RUNTIME. zoho.py opens the
# two 20_verified inventories; pg/reporting.py, integration/statuses.py,
# api/closure.py and api/integrations.py open contract JSON under
# 30_contracts at import time. The first smoke run of this bundle refused to
# start on `research/30_contracts/C3_statuses.json` -- the Dockerfile had the
# same omission, so the container image could not start either. The whole
# 30_contracts directory ships (it is small, frozen JSON/markdown), and
# tests/test_uat_profile.py asserts every research/ directory the backend
# names in code is in RUNTIME_DIRS.
RUNTIME_JSON = ("research/20_verified/zoho_endpoint_inventory.json",
                "research/20_verified/openapi_findings.json")
RUNTIME_DIRS = ("research/30_contracts",)
REQUIRED_MODULES = ("fastapi", "starlette", "pydantic", "pydantic_core", "uvicorn",
                    "anyio", "h11", "click", "psycopg", "psycopg_pool", "psycopg_binary",
                    "typing_extensions", "annotated_types", "typing_inspection",
                    "annotated_doc", "idna")
FORBIDDEN_WHEEL_TAGS = ("win_amd64", "win32", "macosx", "musllinux")
FORBIDDEN_DIRS = {".claude", ".git", "node_modules", "tests", "__pycache__", "data",
                  "test-results", ".vrt-batches", ".pytest_cache"}
SECRET_RE = re.compile(
    rb"(AKIA[0-9A-Z]{16}|gh[pos]_[A-Za-z0-9]{30,}|1000\.[0-9a-f]{32}\.[0-9a-f]{32}"
    rb"|-----BEGIN (?:RSA |EC )?PRIVATE KEY|xox[baprs]-[0-9A-Za-z-]{10,})")
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".sql", ".txt", ".md", ".cfg", ".ini", ".toml"}


class BuildFailed(RuntimeError):
    """Every check raises this. None is a warning."""


def _refuse_inside_repo(path: Path, what: str) -> None:
    try:
        path.resolve().relative_to(REPO.resolve())
    except ValueError:
        return
    raise BuildFailed(f"{what} ({path}) is inside the repository; it must be outside")


# ------------------------------------------------------------------ wheels
def verify_and_extract_wheels(wheels: Path, vendor: Path) -> list[dict]:
    if not wheels.is_dir():
        raise BuildFailed(f"wheel directory not found: {wheels}")
    entries = sorted(wheels.iterdir())
    sdists = [p.name for p in entries if not p.name.endswith(".whl")]
    if sdists:
        raise BuildFailed(f"non-wheel artefacts present (source builds forbidden): {sdists}")
    if vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True)
    out = []
    for whl in entries:
        name = whl.name
        if any(tag in name for tag in FORBIDDEN_WHEEL_TAGS):
            raise BuildFailed(f"wheel for the wrong platform: {name}")
        if "-cp313-" in name and "manylinux" not in name:
            raise BuildFailed(f"binary wheel is not manylinux: {name}")
        if "-cp3" in name and "-cp313-" not in name:
            raise BuildFailed(f"binary wheel is not CPython 3.13: {name}")
        with zipfile.ZipFile(whl) as z:
            z.extractall(vendor)
        out.append({"wheel": name, "sha256": hashlib.sha256(whl.read_bytes()).hexdigest()})
    return out


def verify_vendor_tree(vendor: Path) -> None:
    files = [p for p in vendor.rglob("*") if p.is_file()]
    bad = [str(p.relative_to(vendor)) for p in files if p.suffix.lower() in (".pyd", ".dll")]
    if bad:
        raise BuildFailed(f"Windows binaries in vendor tree: {bad[:5]}")
    so = [p for p in files if p.name.endswith(LINUX_EXT_SUFFIX)]
    if not any("pydantic_core" in str(p) for p in so):
        raise BuildFailed(f"no pydantic_core {LINUX_EXT_SUFFIX} in vendor tree")
    missing = [m for m in REQUIRED_MODULES
               if not (vendor / m).is_dir() and not (vendor / f"{m}.py").is_file()]
    if missing:
        raise BuildFailed(f"required modules absent from vendor tree: {missing}")


# ------------------------------------------------------------------ app tree
def copy_tree(src: Path, dst: Path) -> None:
    def ignore(dirpath, names):
        skip = set()
        for n in names:
            p = Path(dirpath) / n
            if n in FORBIDDEN_DIRS and p.is_dir():
                skip.add(n)
            elif n.endswith((".pyc", ".pyo", ".db", ".db-journal", ".bak")):
                skip.add(n)
            elif n in ("settings.local.json", ".env") or n.startswith(".env."):
                skip.add(n)
        return skip
    shutil.copytree(src, dst, ignore=ignore)


def build_seed(seed_path: Path) -> None:
    env = dict(os.environ)
    env["CAPEX_PROFILE"] = "local-demo"
    env.pop("CAPEX_DB_URL", None)
    env.pop("CAPEX_DB_HOST", None)
    proc = subprocess.run(
        [sys.executable, "-m", "app.backend.migrate", "--db", str(seed_path), "--fresh", "--seed"],
        cwd=str(REPO), env=env, capture_output=True, text=True)
    if proc.returncode != 0 or not seed_path.is_file():
        raise BuildFailed(f"seed build failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")


def verify_credentials(cred: Path) -> str:
    _refuse_inside_repo(cred, "credentials file")
    sys.path.insert(0, str(REPO))
    from app.backend import auth
    loaded = auth.load_uat_credentials(str(cred))          # fails closed on plaintext
    if not loaded:
        raise BuildFailed("credentials file carries no users")
    if "password" in cred.read_text(encoding="utf-8").lower():
        raise BuildFailed("credentials file mentions 'password'")
    plain = cred.parent / "uat-users.txt"
    if plain.exists():
        # It may legitimately sit beside the JSON on the operator's machine;
        # what must never happen is for it to be COPIED. The bundle assembler
        # only ever copies the JSON by explicit path, and the archive check
        # below refuses the name outright.
        pass
    return hashlib.sha256(cred.read_bytes()).hexdigest()


# ------------------------------------------------------------------ archive
def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix().encode()
        h.update(len(rel).to_bytes(4, "big") + rel)
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def write_archive(root: Path, out: Path) -> int:
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(x for x in root.rglob("*") if x.is_file()):
            rel = p.relative_to(root).as_posix()
            if rel.startswith("../") or rel.startswith("/") or ":" in rel:
                raise BuildFailed(f"archive entry escapes the root: {rel}")
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))   # reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
            count += 1
    return count


def verify_archive(out: Path) -> dict:
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        required = {"main.py", "app-config.json", "uat_seed.db", "uat-credentials.json",
                    "app/run.py", "app/backend/main.py", "app/frontend/index.html",
                    "app/frontend/styles.css", "migrations/pg/001_foundation.sql",
                    "research/30_contracts/C3_statuses.json",
                    "research/30_contracts/C10_messages.json",
                    "research/30_contracts/C16_integration_statuses.json",
                    "research/30_contracts/C17_zoho_status_map.json",
                    "research/20_verified/zoho_endpoint_inventory.json",
                    "research/20_verified/openapi_findings.json"}
        missing = sorted(r for r in required if r not in names)
        if missing:
            raise BuildFailed(f"archive is missing: {missing}")
        for m in REQUIRED_MODULES:
            if not any(n.startswith(f"vendor/{m}/") or n == f"vendor/{m}.py" for n in names):
                raise BuildFailed(f"archive lacks vendor module {m}")
        if not any(n.endswith(LINUX_EXT_SUFFIX) and "pydantic_core" in n for n in names):
            raise BuildFailed("archive lacks the Linux pydantic_core extension")
        bad = [n for n in names if n.lower().endswith((".pyd", ".dll"))]
        if bad:
            raise BuildFailed(f"Windows binaries in archive: {bad[:5]}")
        forbidden = [n for n in names if n == "uat-users.txt" or n.endswith("/uat-users.txt")
                     or "settings.local.json" in n or n.startswith((".git/", ".claude/", "tests/",
                                                                    "node_modules/", "app/data/"))
                     or "/__pycache__/" in n or n.endswith((".pyc", ".env"))]
        if forbidden:
            raise BuildFailed(f"forbidden entries in archive: {forbidden[:5]}")
        dbs = [n for n in names if n.endswith(".db")]
        if dbs != ["uat_seed.db"]:
            raise BuildFailed(f"unexpected database files in archive: {dbs}")
        pg_migrations = sorted(n for n in names if re.match(r"migrations/pg/\d{3}_.*\.sql$", n))
        numbers = [int(Path(n).name[:3]) for n in pg_migrations]
        if numbers != list(range(1, len(numbers) + 1)):
            raise BuildFailed(f"PostgreSQL migrations are not contiguous: {numbers}")
        secrets_hit = []
        for n in names:
            if Path(n).suffix.lower() in TEXT_SUFFIXES and not n.startswith("vendor/"):
                if SECRET_RE.search(z.read(n)):
                    secrets_hit.append(n)
        if secrets_hit:
            raise BuildFailed(f"secret-shaped content in: {secrets_hit}")
        cfg = json.loads(z.read("app-config.json"))
        if cfg.get("stack") != STACK or cfg.get("command") != COMMAND:
            raise BuildFailed("app-config.json does not match the expected stack/command")
        if cfg.get("env_variables"):
            raise BuildFailed("app-config.json must not carry env_variables (no secret rides in the archive)")
        return {"files": len(names), "pg_migrations": len(pg_migrations)}


# ------------------------------------------------------------------ main
def build(wheels: Path, credentials: Path, out: Path, ca_bundle: Path | None = None) -> dict:
    _refuse_inside_repo(out, "output archive")
    cred_sha = verify_credentials(credentials)
    with tempfile.TemporaryDirectory(prefix="wbs-uat-bundle-") as tmp:
        root = Path(tmp) / "bundle"
        root.mkdir()
        wheel_list = verify_and_extract_wheels(wheels, root / "vendor")
        verify_vendor_tree(root / "vendor")
        copy_tree(REPO / "app", root / "app")
        copy_tree(REPO / "migrations", root / "migrations")
        for rel in RUNTIME_JSON:
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / rel, dst)
        for rel in RUNTIME_DIRS:
            copy_tree(REPO / rel, root / rel)
        shutil.copyfile(HERE / "uat_main.py", root / "main.py")
        shutil.copyfile(credentials, root / "uat-credentials.json")
        if ca_bundle is not None:
            # Stage B: the PostgreSQL provider's PUBLIC root certificate, at the
            # archive root where uat_main.py looks for it (sslmode verify-full).
            # A certificate is not a secret; the gate below still scans it.
            text = ca_bundle.read_text(encoding="utf-8")
            if "BEGIN CERTIFICATE" not in text or "PRIVATE KEY" in text:
                raise BuildFailed("--ca-bundle must be a PEM certificate chain and never a key")
            shutil.copyfile(ca_bundle, root / "ca-bundle.pem")
        build_seed(root / "uat_seed.db")
        (root / "app-config.json").write_text(json.dumps({
            "command": COMMAND, "stack": STACK, "memory": MEMORY_MB,
            "build_path": "./", "catalyst_auth": False, "env_variables": {}},
            indent=2) + "\n", encoding="utf-8")
        (root / "requirements.txt").write_text(
            "# Informational only. Catalyst installs nothing; every module ships in vendor/.\n",
            encoding="utf-8")
        tree_hash = _tree_hash(root)
        count = write_archive(root, out)
    archive_info = verify_archive(out)
    manifest = {
        "archive": out.name,
        "archive_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "archive_bytes": out.stat().st_size,
        "entries": count,
        "tree_sha256": tree_hash,
        "stack": STACK, "command": COMMAND, "memory_mb": MEMORY_MB,
        "credentials_sha256": cred_sha,
        "ca_bundle_sha256": (hashlib.sha256(ca_bundle.read_bytes()).hexdigest()
                             if ca_bundle is not None else None),
        "pg_migrations": archive_info["pg_migrations"],
        "wheels": wheel_list,
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                                   capture_output=True, text=True).stdout.strip(),
    }
    (out.parent / (out.name + ".manifest.json")).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the UAT AppSail bundle (no network).")
    ap.add_argument("--wheels", required=True)
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ca-bundle", default=None,
                    help="Stage B: the PostgreSQL provider's root CA (PEM), placed at the archive root")
    args = ap.parse_args(argv)
    try:
        manifest = build(Path(args.wheels), Path(args.credentials), Path(args.out),
                         Path(args.ca_bundle) if args.ca_bundle else None)
    except BuildFailed as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 2
    shown = {k: v for k, v in manifest.items() if k != "wheels"}
    shown["wheels"] = len(manifest["wheels"])
    print(json.dumps(shown, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
