#!/usr/bin/env python
"""Prove that `uat_role_matrix.py` can fail.

    python tests/uat/uat_selftest.py --base http://127.0.0.1:8871

A gate that cannot go red is not a gate. This repository has been bitten by
that exact defect twice in the visual suite -- `tools/run_vrt.mjs` carries the
account -- and a role-based UAT is a worse place to have it, because a
permission suite that always passes reads as proof that authorisation works.

So this runs the harness TWICE against the same live server:

  1. UNCHANGED. Must exit 0. A self-test that only proved the harness could
     fail would be satisfied by a harness that always failed.
  2. With ONE permission widened IN MEMORY -- `approval.configure` is claimed
     for the Auditor, which the running server does not honour. Must exit 1,
     and the failure must NAME the identity and the permission.

Nothing on disk is modified. `app/backend/auth.py` is not edited; the mutation
lives in this process's imported copy of `auth.PERMISSIONS` and dies with it.
The server under test is never reconfigured, which is the point: the harness
must notice that the model it reads and the server it calls disagree.

Exit code: 0 both checks behaved, 1 the harness is not a working gate.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from app.backend import auth  # noqa: E402


def load_harness():
    """Import uat_role_matrix by path, so this works with no package layout and
    adds no `__init__.py` that pytest would then treat as a package root."""
    spec = importlib.util.spec_from_file_location(
        "uat_role_matrix", os.path.join(HERE, "uat_role_matrix.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("UAT_BASE", "http://127.0.0.1:8000"))
    args = ap.parse_args(argv)

    harness = load_harness()
    problems: list[str] = []

    # ---- 1. unchanged: must pass -----------------------------------------
    buf = io.StringIO()
    with redirect_stdout(buf):
        clean = harness.main(["--base", args.base])
    if clean != 0:
        problems.append(
            f"the UNMODIFIED harness exited {clean}, not 0. Either the server is "
            f"not in its seeded state or a real assertion is failing -- run it "
            f"directly to see which:\n"
            + "\n".join("      " + line for line in buf.getvalue().splitlines()[-12:]))

    # ---- 2. one permission widened in memory: must fail -------------------
    original = auth.PERMISSIONS["approval.configure"]
    auth.PERMISSIONS["approval.configure"] = original + ("Auditor",)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            dirty = harness.main(["--base", args.base])
        output = buf.getvalue()
    finally:
        auth.PERMISSIONS["approval.configure"] = original

    if dirty != 1:
        problems.append(
            f"the harness exited {dirty} when the permission model and the server "
            f"disagreed. It must exit 1. As it stands it would report a broken "
            f"authorisation model as a pass.")
    elif "U-AUD" not in output or "approval.configure" not in output:
        problems.append(
            "the harness failed, but its report named neither the identity nor "
            "the permission. A UAT failure nobody can act on is barely better "
            "than no failure at all.")

    print("=" * 78)
    print("UAT HARNESS SELF-TEST")
    print("=" * 78)
    print(f"  server              : {args.base}")
    print(f"  unmodified run      : exit {clean}  (expected 0)")
    print(f"  model-vs-server run : exit {dirty}  (expected 1)")
    print()
    if problems:
        for p in problems:
            print(f"  x {p}")
        print("=" * 78)
        return 1
    print("  The harness passes what it should and fails what it should, and its "
          "failure\n  names the identity and the permission. It is a working gate.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
