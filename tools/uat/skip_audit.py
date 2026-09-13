"""Prove every skip in a pytest JUnit report is an INTENDED one.

    python tools/uat/skip_audit.py <junit.xml> [--expect-min-executed N]

A green run with silent skips proves less than it claims: a live-PostgreSQL
suite that skipped itself, or a test that declared itself inapplicable on
the wrong condition, reads as a pass. This reads the JUnit report pytest
wrote (`--junitxml`), groups every skipped test by its recorded reason, and
matches each reason against the allow-list below -- the reasons this
repository has DECLARED as intended. Any reason outside the list fails the
audit with the test ids that carried it. Exit code 0 only when every skip is
intended and at least `--expect-min-executed` tests actually executed.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

#: Each entry: a regular expression over the skip reason, and why it is
#: intended. Adding a line here is a reviewable decision, not a convenience.
INTENDED = [
    (r"declared NO_ROW_SCOPE",
     "tests/test_scope_enforcement.py: tables that DECLARE no row scope (audit reads, "
     "organisation-wide reference data, the grant tables) are exercised by their own tests"),
    (r"approval schema not present",
     "tests that read the approval tables skip on a database built without migration 008; "
     "the live suite builds every migration, so this must not appear there"),
    (r"PostgreSQL not configured; set CAPEX_DB_URL|CAPEX_DB_URL is not set|needs a live PostgreSQL \(CAPEX_DB_URL\)",
     "the live-PostgreSQL gate in its three wordings: intended ONLY in a run without a database, "
     "never in the live run"),
    (r"got empty parameter set for \((name|relative)\)",
     "an EMPTIED register (store.UNBACKED_SWEEP_SURFACE, KNOWN_UNREPAIRED): the parametrised "
     "test has no cases because nothing remains; a companion test asserts the register is empty"),
    (r"has no _actor\(\)",
     "tests/test_security_identity.py: the actor-derivation guard applies only to routers that "
     "derive an actor from the session; the others are named as they are skipped"),
    (r"no live Zoho|requires the live tenant|CAPEX_ERP_LIVE",
     "tests that need the live tenant are opt-in by environment"),
    (r"windows|win32|not on this platform|posix only",
     "platform-specific tests"),
    (r"psycopg\[binary\]|psycopg not installed",
     "driver-absent guard; the live run installs the driver"),
    (r"provider-specific|Supabase|pooler",
     "hosted-provider behaviour verified against the UAT instance, not locally"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("report", help="the --junitxml file")
    ap.add_argument("--expect-min-executed", type=int, default=0)
    ap.add_argument("--live", action="store_true",
                    help="the run had CAPEX_DB_URL: the PostgreSQL gate and the "
                         "approval-schema skips are NOT intended in it")
    args = ap.parse_args(argv)
    root = ET.parse(Path(args.report)).getroot()
    suites = list(root.iter("testsuite")) if root.tag == "testsuites" else [root]
    total = executed = 0
    by_reason: dict[str, list[str]] = defaultdict(list)
    for suite in suites:
        for case in suite.iter("testcase"):
            total += 1
            skipped = case.find("skipped")
            if skipped is None:
                executed += 1
                continue
            reason = (skipped.get("message") or skipped.text or "").strip()
            by_reason[reason].append(f"{case.get('classname')}::{case.get('name')}")
    intended = list(INTENDED)
    if args.live:
        intended = [(rx, why) for rx, why in intended
                    if "CAPEX_DB_URL" not in rx and "approval schema" not in rx]
    unintended: dict[str, list[str]] = {}
    print(f"tests: {total} collected, {executed} executed, "
          f"{total - executed} skipped across {len(by_reason)} reason(s)")
    for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        match = next((why for rx, why in intended if re.search(rx, reason, re.I)), None)
        tag = "intended" if match else "UNINTENDED"
        print(f"  [{tag}] {len(ids):4d}  {reason[:110]}")
        if not match:
            unintended[reason] = ids
    if unintended:
        print("\nUNINTENDED skips:")
        for reason, ids in unintended.items():
            for tid in ids[:20]:
                print(f"    {tid}  --  {reason[:120]}")
            if len(ids) > 20:
                print(f"    ... and {len(ids) - 20} more")
    if executed < args.expect_min_executed:
        print(f"\nonly {executed} executed, below the expected minimum "
              f"{args.expect_min_executed}")
        return 2
    return 1 if unintended else 0


if __name__ == "__main__":
    sys.exit(main())
