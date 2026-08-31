"""Generate tests/TEST_MANIFEST.json - the inventory that protects the suite.

Plan v1.2.1 section 15.1:

    "All control intent and traceability must be preserved. Documented
     PostgreSQL-specific test adaptations are permitted, but no assertion may
     be weakened or removed without approval."

The manifest is how that is enforced mechanically. It records every test
function that exists today. tests/test_manifest.py asserts the live inventory
still matches it, so a removed or renamed test fails the build until the
manifest is updated with a recorded reason in tests/ADAPTATIONS.md.

Usage
-----
    python tools/build_test_manifest.py            # rewrite the manifest
    python tools/build_test_manifest.py --check    # exit 1 if it has drifted

The baseline suite is the audit remediation suite: the tests are named after
the findings they close, so losing one silently loses the evidence that a
critical finding stays closed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MANIFEST = TESTS / "TEST_MANIFEST.json"

# Files added after the POC baseline. Tracked, but excluded from the
# "220 original functions" count so the baseline stays a fixed reference.
POST_BASELINE_FILES = {
    "test_contracts.py", "test_manifest.py", "test_observability.py",
    "test_known_defects.py", "test_middleware_observability.py",
    # --- Milestone 1: production PostgreSQL foundation -------------------
    # The 220 baseline counts the POC's audit-remediation suite, and its
    # purpose is to catch a baseline test being REMOVED. New milestone files
    # belong here rather than inflating that number, or the guard stops
    # meaning anything the moment the product grows.
    "test_pg_repo.py", "test_pg_locking.py", "test_pg_audit.py",
    "test_runtime_startup.py",
    # PostgreSQL integration suite, skipped without CAPEX_DB_URL. Flattened
    # out of tests/pg/ because a second conftest.py there shadowed the root
    # one under the bare module name `conftest`.
    "test_pg_fixture_guard.py", "test_pg_migrations_runner.py",
    "test_pg_constraints.py", "test_pg_transactions.py",
    "test_pg_scope_leakage.py",
    # M1-S2
    "test_pg_audit_api_e2e.py", "test_pg_seed.py", "test_pg_adoption.py",
}


def _assertion_count(node: ast.AST) -> int:
    """Assert statements plus pytest.raises blocks in one test function.

    Recording the NAME alone left the ratchet blind to the failure it exists to
    prevent: replacing every assertion in a test with `pass` kept the name, so
    nothing failed. The count makes a gutted test visible.
    """
    return sum(
        1 for n in ast.walk(node)
        if isinstance(n, ast.Assert)
        or (isinstance(n, ast.With)
            and any("raises" in ast.dump(item.context_expr) for item in n.items))
    )


def _body_hash(node: ast.AST) -> str:
    """Normalised structural hash of one test function body.

    Assertion COUNTS cannot see semantic weakening: replacing

        assert to_paise("0.005") == 1
    with
        assert to_paise("0.005") is not None

    keeps the count at one while destroying the property under test. The hash
    changes, so the gate fires.

    Normalised so that formatting churn does not create false alarms:
      * the docstring is excluded - prose may be improved freely
      * `ast.dump` is taken WITHOUT line/column attributes, so reflowing,
        re-indenting or moving a test does not change the hash
    A comment change is likewise invisible, because comments are not in the AST.
    """
    body = list(getattr(node, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # drop the docstring
    dumped = chr(10).join(
        ast.dump(n, annotate_fields=True, include_attributes=False) for n in body
    )
    return hashlib.sha256(dumped.encode()).hexdigest()[:16]


def collect() -> dict[str, dict[str, dict]]:
    """{filename: {test_name: {assertions, body_sha}}}, recursing subdirectories.

    Recursive because pytest.ini sets `testpaths = tests`, which collects
    recursively - a non-recursive glob left tests/integration/ as a blind spot.
    """
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        key = path.relative_to(TESTS).as_posix()
        out[key] = {
            node.name: {
                "assertions": _assertion_count(node),
                "body_sha": _body_hash(node),
            }
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
    return out


def build() -> dict:
    inventory = collect()
    baseline = {f: n for f, n in inventory.items() if f not in POST_BASELINE_FILES}
    return {
        "generated_from": "tools/build_test_manifest.py",
        "purpose": (
            "Protects the audit remediation suite. Every test is named after the "
            "finding it closes; losing one silently loses the evidence that a "
            "critical finding stays closed."
        ),
        "policy": (
            "All control intent and traceability must be preserved. Documented "
            "PostgreSQL-specific adaptations are permitted, but no assertion may "
            "be weakened or removed without approval recorded in "
            "tests/ADAPTATIONS.md."
        ),
        "baseline": {
            "recorded_at": "2026-08-28",
            "commit_note": "POC baseline, branch phase-0a/foundations, before any port work",
            "test_functions": sum(len(v) for v in baseline.values()),
            "collected_cases": 348,
            "collected_cases_note": (
                "348 is the pytest-collected count after parametrisation expands "
                "6 files; 220 is the count of `def test_` functions. Both are real "
                "and the Definition of Done refers to the function count."
            ),
            "runtime_seconds_approx": 189,
            "database": "SQLite",
        },
        "files": inventory,
        "counts": {f: len(n) for f, n in sorted(inventory.items())},
        "assertions": {
            f: sum(t["assertions"] for t in n.values())
            for f, n in sorted(inventory.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    fresh = build()
    if args.check:
        if not MANIFEST.exists():
            print("TEST_MANIFEST.json does not exist. Run without --check.")
            return 1
        stored = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if stored["files"] != fresh["files"]:
            print("Test inventory has drifted from TEST_MANIFEST.json.")
            return 1
        print("Test inventory matches the manifest.")
        return 0

    MANIFEST.write_text(
        json.dumps(fresh, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {MANIFEST.relative_to(ROOT)} - "
        f"{sum(len(v) for v in fresh['files'].values())} test functions, "
        f"{sum(t['assertions'] for v in fresh['files'].values() for t in v.values())} assertions, "
        f"across {len(fresh['files'])} files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
