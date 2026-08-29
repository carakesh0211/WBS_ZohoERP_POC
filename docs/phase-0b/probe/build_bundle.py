"""Assemble and verify the Phase 0B probe deployment bundle.

Catalyst never installs dependencies, so every module and data file the probe
needs must be inside the uploaded archive. This script is the gate that proves
it, and it **fails the build** rather than producing a bundle that will only
reveal its defect against a live endpoint inside a timed exposure window.

It validates the pinned CA through `ca.py` -- the same module the running probe
imports -- so the gate cannot certify rules the runtime does not apply.

Usage
-----
    python build_bundle.py --verify-ca-only     # CI-safe; no archive written
    python build_bundle.py --out ../../../bundle.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ca import CaBundleUnusable, _ca_summary  # noqa: E402

CA_FILENAME = "ca-bundle.pem"
#: Every one of these must be at the archive ROOT. `ca-bundle.pem` is listed
#: here, not treated as optional: its omission is the exact defect that cost
#: the first attempt, and an optional file cannot be a gate.
REQUIRED_ROOT = ("main.py", "ca.py", "app-config.json", CA_FILENAME)
REQUIRED_VENDOR_MODULES = ("fastapi", "pg8000", "scramp", "uvicorn", "pydantic")
LINUX_EXT_SUFFIX = "cpython-313-x86_64-linux-gnu.so"


class BuildFailed(RuntimeError):
    """Raised for every gate breach, so no caller can mistake one for a warning."""


def verify_ca() -> dict:
    """Validate the pinned CA. Returns provenance metadata; never cert bytes."""
    path = os.path.join(HERE, CA_FILENAME)
    try:
        summary = _ca_summary(path)
    except CaBundleUnusable as exc:
        raise BuildFailed(
            f"CA bundle gate failed: {exc}. "
            f"Place the Supabase root CA at {CA_FILENAME} (see CA_BUNDLE.md). "
            f"TLS verification is never relaxed to work around this."
        ) from None
    return summary


def _iter_sources():
    for name in REQUIRED_ROOT:
        yield os.path.join(HERE, name), name
    req = os.path.join(HERE, "requirements.txt")
    if os.path.isfile(req):
        yield req, "requirements.txt"
    vendor = os.path.join(HERE, "vendor")
    if not os.path.isdir(vendor):
        raise BuildFailed(
            "vendor/ is absent. Catalyst does not run pip install; build it "
            "first with the recipe in BUILD.md."
        )
    for root, dirs, files in os.walk(vendor):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, f)
            yield full, os.path.relpath(full, HERE).replace(os.sep, "/")


def build(out_path: str) -> dict:
    ca = verify_ca()

    missing = [n for n in REQUIRED_ROOT if not os.path.isfile(os.path.join(HERE, n))]
    if missing:
        raise BuildFailed(f"missing required root files: {missing}")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in _iter_sources():
            z.write(src, arc)

    return verify_zip(out_path, ca)


def verify_zip(path: str, ca: dict) -> dict:
    """Re-open the finished archive and assert its contents.

    Checking the source tree is not the same as checking the artefact; only the
    artefact is uploaded, so only the artefact is evidence.
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        ca_in_zip = z.read(CA_FILENAME) if CA_FILENAME in names else b""

    for required in REQUIRED_ROOT:
        if required not in names:
            raise BuildFailed(f"{required} is missing from the archive root")

    on_disk = open(os.path.join(HERE, CA_FILENAME), "rb").read()
    if hashlib.sha256(ca_in_zip).hexdigest() != hashlib.sha256(on_disk).hexdigest():
        raise BuildFailed("the archived CA bundle differs from the validated one")

    pyd = [n for n in names if n.endswith(".pyd")]
    if pyd:
        raise BuildFailed(f"Windows binaries present, bundle is not Linux-targeted: {pyd[:3]}")
    if not any(n.endswith(LINUX_EXT_SUFFIX) for n in names):
        raise BuildFailed(f"no {LINUX_EXT_SUFFIX} found; pydantic_core is not the Linux wheel")
    for mod in REQUIRED_VENDOR_MODULES:
        if not any(n.startswith(f"vendor/{mod}/") or n == f"vendor/{mod}.py" for n in names):
            raise BuildFailed(f"vendored module absent: {mod}")

    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return {"bundle": os.path.abspath(path), "sha256": digest,
            "file_count": len(names), "size_bytes": os.path.getsize(path),
            "ca_bundle": ca}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--verify-ca-only", action="store_true")
    ap.add_argument("--verify-zip")
    args = ap.parse_args()
    try:
        if args.verify_ca_only:
            print(json.dumps(verify_ca(), indent=2))
        elif args.verify_zip:
            print(json.dumps(verify_zip(args.verify_zip, verify_ca()), indent=2))
        else:
            if not args.out:
                ap.error("--out is required unless --verify-ca-only is given")
            print(json.dumps(build(args.out), indent=2))
    except BuildFailed as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
