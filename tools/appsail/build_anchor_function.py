"""Assemble the deployable folder for the `capex_audit_anchor` Cron Function.

    python tools/appsail/build_anchor_function.py --wheels <staging>/wheels --out <outside-repo>/capex_audit_anchor

No network. Reuses the AppSail bundle builder's checks (`build_uat_bundle`):
manylinux-only wheels, no Windows binaries, the runtime `research/` files
present, no secrets, no stray databases. The output is a FOLDER (Catalyst
deploys a function from its folder), verified after assembly by re-walking it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import build_uat_bundle as b  # noqa: E402  (the shared gate)

FUNCTION_DIR = REPO / "catalyst" / "functions" / "capex_audit_anchor"


def build(wheels: Path, out: Path) -> dict:
    b._refuse_inside_repo(out, "output folder")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    wheel_list = b.verify_and_extract_wheels(wheels, out / "vendor")
    b.verify_vendor_tree(out / "vendor")
    for name in ("main.py", "catalyst-config.json", "requirements.txt"):
        shutil.copyfile(FUNCTION_DIR / name, out / name)
    b.copy_tree(REPO / "app", out / "app")
    b.copy_tree(REPO / "migrations", out / "migrations")
    for rel in b.RUNTIME_JSON:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, out / rel)
    for rel in b.RUNTIME_DIRS:
        b.copy_tree(REPO / rel, out / rel)
    # Same refusals as the archive gate, walked over the folder.
    files = [p for p in out.rglob("*") if p.is_file()]
    bad = [str(p.relative_to(out)) for p in files if p.suffix.lower() in (".pyd", ".dll")]
    if bad:
        raise b.BuildFailed(f"Windows binaries in function folder: {bad[:5]}")
    dbs = [str(p.relative_to(out)) for p in files if p.suffix == ".db"]
    if dbs:
        raise b.BuildFailed(f"database files in function folder: {dbs}")
    forbidden = [str(p.relative_to(out)) for p in files
                 if p.name in ("settings.local.json", "uat-users.txt", ".env")
                 or any(part in (".claude", ".git", "tests", "node_modules") for part in p.relative_to(out).parts)]
    if forbidden:
        raise b.BuildFailed(f"forbidden entries: {forbidden[:5]}")
    hits = [str(p.relative_to(out)) for p in files
            if p.suffix.lower() in b.TEXT_SUFFIXES and "vendor" not in p.relative_to(out).parts
            and b.SECRET_RE.search(p.read_bytes())]
    if hits:
        raise b.BuildFailed(f"secret-shaped content in: {hits}")
    cfg = json.loads((out / "catalyst-config.json").read_text(encoding="utf-8"))
    if cfg.get("deployment", {}).get("type") != "cron" or cfg.get("execution", {}).get("main") != "main.py":
        raise b.BuildFailed("catalyst-config.json is not a cron function pointing at main.py")
    manifest = {
        "function": "capex_audit_anchor", "files": len(files), "tree_sha256": b._tree_hash(out),
        "wheels": wheel_list, "git_head": b.subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True).stdout.strip(),
    }
    (out.parent / (out.name + ".manifest.json")).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble the capex_audit_anchor Cron Function folder (no network).")
    ap.add_argument("--wheels", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        manifest = build(Path(args.wheels), Path(args.out))
    except b.BuildFailed as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 2
    shown = dict(manifest, wheels=len(manifest["wheels"]))
    print(json.dumps(shown, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
