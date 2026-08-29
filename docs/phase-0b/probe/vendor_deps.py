"""Build the vendored dependency tree for the Phase 0B probe.

Catalyst never runs `pip install`, so every module must be inside the uploaded
archive. This script produces that tree **outside the repository** and proves it
is what the inventory says it is.

Why it resolves rather than just downloading a list
---------------------------------------------------
The top-level pins are the input; the resolved closure is the *output*, and it
is compared against `dependency-inventory.json`. Downloading the inventory
directly would only prove the inventory can be downloaded -- it could not detect
a dependency that has appeared, disappeared or changed since the inventory was
written. Resolving and then asserting set equality catches all three.

Why the tree lives outside Git
------------------------------
It is ~500 files of third-party wheel contents including a compiled binary.
Committing it would put an unreviewable blob under version control; the
inventory plus this script is the reproducible form. Nothing here is committed
except the script and the inventory.

Usage
-----
    py vendor_deps.py --dest <staging-dir-outside-the-repo>
    py vendor_deps.py --dest <dir> --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INVENTORY = HERE / "dependency-inventory.json"

#: The probe's direct imports. Everything else must arrive as a transitive
#: dependency of these, or the inventory is wrong.
TOP_LEVEL = ("fastapi", "uvicorn", "pg8000", "pydantic")

TARGET = {
    "platform": "manylinux2014_x86_64",
    "python_version": "313",
    "implementation": "cp",
}
LINUX_EXT_SUFFIX = "cpython-313-x86_64-linux-gnu.so"
REQUIRED_MODULES = ("fastapi", "pg8000", "scramp", "uvicorn", "pydantic",
                    "pydantic_core", "starlette", "anyio", "h11", "click")


class VendorFailed(RuntimeError):
    """Every check raises this. None of them are warnings."""


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def load_inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def download(dest: Path, inventory: dict) -> Path:
    """Resolve the closure from the top-level pins and fetch wheels for Linux."""
    wheels = dest / "wheels"
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(parents=True)

    pins = [f"{name}=={inventory[name]['version']}" for name in TOP_LEVEL]
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", str(wheels),
        "--platform", TARGET["platform"],
        "--python-version", TARGET["python_version"],
        "--implementation", TARGET["implementation"],
        # Mandatory with --platform, and independently required: a source
        # distribution would be built for the HOST, not the target.
        "--only-binary=:all:",
        *pins,
    ]
    print("$ " + " ".join(cmd[1:]), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VendorFailed(f"pip download failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return wheels


def verify_wheels(wheels: Path, inventory: dict) -> dict:
    """Assert the resolved closure is exactly the inventory, hash for hash."""
    found = {}
    for whl in sorted(wheels.glob("*.whl")):
        digest = hashlib.sha256(whl.read_bytes()).hexdigest()
        dist = _norm(whl.name.split("-")[0])
        found[dist] = {"wheel": whl.name, "sha256": digest, "path": whl}

    sdists = [p.name for p in wheels.iterdir() if not p.name.endswith(".whl")]
    if sdists:
        raise VendorFailed(f"non-wheel artefacts present (source builds are forbidden): {sdists}")

    expected = {_norm(k): v for k, v in inventory.items()}
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    if missing:
        raise VendorFailed(f"inventory lists packages the resolver did not produce: {missing}")
    if extra:
        raise VendorFailed(
            f"resolver produced packages absent from the inventory: {extra}. "
            f"The closure has changed -- update dependency-inventory.json deliberately, "
            f"with hashes, rather than letting it drift.")

    mismatched = []
    for dist, meta in sorted(found.items()):
        want = expected[dist]
        if meta["wheel"] != want["wheel"]:
            mismatched.append(f"{dist}: filename {meta['wheel']} != pinned {want['wheel']}")
        elif meta["sha256"] != want["sha256"]:
            mismatched.append(f"{dist}: sha256 {meta['sha256'][:16]}... != pinned {want['sha256'][:16]}...")
    if mismatched:
        raise VendorFailed("wheel verification failed:\n  " + "\n  ".join(mismatched))

    for dist, meta in found.items():
        kind = expected[dist]["kind"]
        name = meta["wheel"]
        if kind == "pure" and not (name.endswith("-none-any.whl")):
            raise VendorFailed(f"{dist} is declared pure but its wheel is platform-specific: {name}")
        if kind.startswith("linux-") and "manylinux" not in name:
            raise VendorFailed(f"{dist} is declared a Linux binary wheel but is not manylinux: {name}")
        if "win_amd64" in name or "musllinux" in name or "macosx" in name:
            raise VendorFailed(f"wrong platform wheel: {name}")

    return found


def extract(wheels: Path, dest: Path) -> Path:
    vendor = dest / "vendor"
    if vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True)
    for whl in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            z.extractall(vendor)
    for pyc in list(vendor.rglob("__pycache__")):
        shutil.rmtree(pyc, ignore_errors=True)
    return vendor


def verify_tree(vendor: Path) -> dict:
    pyd = [str(p.relative_to(vendor)) for p in vendor.rglob("*.pyd")]
    if pyd:
        raise VendorFailed(f"Windows binaries present -- the tree is not Linux-targeted: {pyd[:5]}")

    dll = [str(p.relative_to(vendor)) for p in vendor.rglob("*.dll")]
    if dll:
        raise VendorFailed(f"Windows DLLs present: {dll[:5]}")

    sos = sorted(str(p.relative_to(vendor)) for p in vendor.rglob("*.so"))
    if not sos:
        raise VendorFailed("no .so present -- pydantic_core's compiled extension is missing")
    wrong = [s for s in sos if not s.endswith(LINUX_EXT_SUFFIX)]
    if wrong:
        raise VendorFailed(f"unexpected shared objects (not cp313 linux x86_64): {wrong}")

    for mod in REQUIRED_MODULES:
        if not ((vendor / mod).is_dir() or (vendor / f"{mod}.py").is_file()):
            raise VendorFailed(f"module absent from the tree: {mod}")

    files = sorted(p for p in vendor.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)

    # Deterministic tree hash: relative path + content, in sorted order. Two
    # independently built trees with the same hash are the same tree.
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(vendor)).replace(os.sep, "/").encode())
        h.update(p.read_bytes())

    return {"file_count": len(files), "total_bytes": total,
            "tree_sha256": h.hexdigest(), "shared_objects": sos}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    repo = HERE.parents[2]
    if repo in dest.parents or dest == repo:
        raise SystemExit(
            f"REFUSED: --dest is inside the repository ({dest}). The vendored tree "
            f"must not be committable. Choose a path outside {repo}.")

    inventory = load_inventory()
    try:
        wheels = dest / "wheels" if args.verify_only else download(dest, inventory)
        if not wheels.is_dir():
            raise VendorFailed(f"no wheels directory at {wheels}; run without --verify-only first")
        found = verify_wheels(wheels, inventory)
        vendor = (dest / "vendor") if args.verify_only else extract(wheels, dest)
        if not vendor.is_dir():
            raise VendorFailed(f"no vendor directory at {vendor}")
        tree = verify_tree(vendor)
    except VendorFailed as exc:
        print(f"\nVENDORING FAILED: {exc}", file=sys.stderr)
        return 1

    manifest = {
        "staging_root": str(dest),
        "vendor_dir": str(vendor),
        "package_count": len(found),
        "inventory_sha256": hashlib.sha256(INVENTORY.read_bytes()).hexdigest(),
        "target": TARGET,
        **tree,
    }
    (dest / "staged-inventory.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print("\nOK: closure resolved, hashes verified, tree is Linux cp313 x86_64.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
