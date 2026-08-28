"""Guards the test inventory itself.

Plan v1.2.1 section 15.1 requires that all control intent and traceability be
preserved through the PostgreSQL port. Documented adaptations are permitted;
weakening or removing an assertion without approval is not.

The 220 baseline tests are named after the audit findings they close
(test_aud_c_001_*, test_aud_h_007_*, and so on). Losing one silently loses the
evidence that a critical finding stays closed - which is exactly the failure
mode this suite exists to prevent. So the inventory is itself under test.

To change the inventory legitimately:

    1. record the change in tests/ADAPTATIONS.md with a reason and a reviewer
    2. run  python tools/build_test_manifest.py
    3. commit both together

A test that disappears without step 1 fails the build.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "TEST_MANIFEST.json").read_text(encoding="utf-8"))
ADAPTATIONS = ROOT / "tests" / "ADAPTATIONS.md"

import sys as _sys
_sys.path.insert(0, str(ROOT))
from tools.build_test_manifest import POST_BASELINE_FILES  # noqa: E402


def _live_inventory() -> dict[str, dict[str, int]]:
    """Single source of truth - imported from the generator so the two cannot
    drift. POST_BASELINE_FILES was previously duplicated in both files and had
    already diverged once."""
    import sys

    sys.path.insert(0, str(ROOT))
    from tools.build_test_manifest import collect

    return collect()


def test_no_test_has_vanished_without_a_recorded_adaptation():
    live = _live_inventory()
    for filename, recorded in MANIFEST["files"].items():
        present = set(live.get(filename, []))
        missing = sorted(set(recorded) - present)
        assert not missing, (
            f"{filename}: {len(missing)} test(s) removed or renamed without an "
            f"entry in tests/ADAPTATIONS.md: {missing}\n"
            "No assertion may be weakened or removed without approval. If this "
            "change is intended, record it and regenerate the manifest."
        )


def test_no_test_has_been_gutted_of_its_assertions():
    """The failure the name-only ratchet could not see.

    Replacing every assertion in a test with `pass`, or adding
    @pytest.mark.skip, keeps the function name - so the inventory check passes
    while the protection is gone. Assertion counts make that visible.

    A count may RISE freely (strengthening a test is always welcome). A fall
    requires a recorded adaptation.
    """
    live = _live_inventory()
    for filename, recorded in MANIFEST["files"].items():
        for name, entry in recorded.items():
            live_entry = live.get(filename, {}).get(name)
            if live_entry is None:
                continue  # covered by the vanished-test guard
            count, now = entry["assertions"], live_entry["assertions"]
            assert now >= count, (
                f"{filename}::{name} lost assertions ({count} -> {now}). "
                "No assertion may be weakened or removed without approval "
                "recorded in tests/ADAPTATIONS.md."
            )


def test_new_tests_are_recorded_in_the_manifest():
    """Additions are welcome, but the manifest must stay the inventory of record."""
    live = _live_inventory()
    for filename, names in live.items():
        recorded = set(MANIFEST["files"].get(filename, []))
        added = sorted(set(names) - recorded)
        assert not added, (
            f"{filename}: {len(added)} new test(s) not in the manifest: {added}\n"
            "Run: python tools/build_test_manifest.py"
        )


def test_the_baseline_count_is_exactly_two_hundred_and_twenty():
    """The POC shipped 220 test functions. That number is the reference point
    for the port and must not move by accident."""
    live = _live_inventory()
    baseline = sum(
        len(names) for f, names in live.items() if f not in POST_BASELINE_FILES
    )
    assert baseline == 220, (
        f"Baseline suite is now {baseline} test functions, not 220. "
        "If this is intended, update tests/ADAPTATIONS.md and the manifest baseline."
    )


def test_every_audit_finding_with_tests_still_has_them():
    """Each remediated finding in FINDINGS_REMEDIATION_STATUS.csv that names
    test IDs must still be represented by at least one test function."""
    live = _live_inventory()
    all_names = {n for names in live.values() for n in names}

    findings_with_tests = {
        "aud_c_001", "aud_c_002", "aud_c_003", "aud_c_004", "aud_c_005",
        "aud_c_006", "aud_c_007", "aud_c_008", "aud_c_009", "aud_c_010",
        "aud_h_001", "aud_h_002", "aud_h_003", "aud_h_007", "aud_m_005",
    }
    for finding in sorted(findings_with_tests):
        matching = [n for n in all_names if n.startswith(f"test_{finding}_")]
        assert matching, (
            f"No test remains for {finding.upper().replace('_', '-')}. "
            "That finding's evidence has been lost."
        )


def test_the_adaptations_register_exists_and_states_the_policy():
    assert ADAPTATIONS.exists(), "tests/ADAPTATIONS.md is required by plan section 15.1"
    text = ADAPTATIONS.read_text(encoding="utf-8")
    assert "no assertion may be weakened or removed without approval" in text.lower()


def test_every_adaptation_entry_names_a_reviewer():
    """An adaptation without an approver is not an approved adaptation."""
    text = ADAPTATIONS.read_text(encoding="utf-8")
    # Only the REGISTER counts. The "Anticipated adaptations" table below it is
    # explicitly labelled forecasting, not pre-approval, and its reviewer cells
    # are role names by design - reading those was the original bug here.
    register = text.split("## Register", 1)[-1].split("## Anticipated", 1)[0]
    rows = [ln for ln in register.splitlines() if ln.startswith("| ADAPT-")]
    placeholders = {"-", "—", "tbd", "reviewer at time of change",
                    "product owner", "technical lead", "product owner + technical lead"}
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) >= 6, f"Malformed adaptation row: {row}"
        reviewer = cells[-1].strip("* ").lower()
        assert reviewer and reviewer not in placeholders, (
            f"Adaptation {cells[0]} names no individual reviewer (got {cells[-1]!r}). "
            "A role is not an approver."
        )


def test_manifest_records_both_test_counts_unambiguously():
    """220 functions and 348 collected cases are both real numbers. The
    Definition of Done refers to the function count, so the manifest must say
    which is which rather than leaving it to be misread."""
    b = MANIFEST["baseline"]
    assert b["test_functions"] == 220
    assert b["collected_cases"] == 348
    assert "parametrisation" in b["collected_cases_note"]


# ---------------------------------------------------------------- body hashes
def _reviewed_tests() -> dict[str, str]:
    """{test_name: reviewer} from the ADAPTATIONS register.

    A register row must name the affected test(s) and an individual reviewer.
    A role ("product owner") or a placeholder is not an approver.
    """
    text = ADAPTATIONS.read_text(encoding="utf-8")
    register = text.split("## Register", 1)[-1].split("## Anticipated", 1)[0]
    placeholders = {
        "-", "—", "tbd", "reviewer at time of change",
        "product owner", "technical lead", "product owner + technical lead",
    }
    out: dict[str, str] = {}
    for line in register.splitlines():
        if not line.startswith("| ADAPT-"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        reviewer = cells[-1].strip("* ").lower()
        if not reviewer or reviewer in placeholders:
            continue
        for token in re.split(r"[,\s`]+", cells[1]):
            token = token.strip("`* ")
            if token.startswith("test_"):
                out[token] = cells[-1]
    return out


def test_no_baseline_test_body_changed_without_a_named_reviewer():
    """The gap assertion COUNTS cannot see.

    Replacing

        assert to_paise("0.005") == 1
    with
        assert to_paise("0.005") is not None

    keeps the count at one and destroys the property under test. The
    normalised body hash changes, so this fires.

    The hash excludes the docstring and ignores line numbers, so improving
    prose, reflowing code or moving a test does not trip it. Only the
    executable structure counts.

    A change is permitted ONLY when the ADAPTATIONS register names that
    specific test and an individual reviewer - not a role, not "reviewer at
    time of change".
    """
    live = _live_inventory()
    reviewed = _reviewed_tests()
    offenders = []
    for filename, recorded in MANIFEST["files"].items():
        if filename in POST_BASELINE_FILES:
            continue  # the 220 baseline is what plan section 15.1 protects
        for name, entry in recorded.items():
            live_entry = live.get(filename, {}).get(name)
            if live_entry is None:
                continue  # covered by the vanished-test guard
            if live_entry["body_sha"] != entry["body_sha"] and name not in reviewed:
                offenders.append(f"{filename}::{name}")
    assert not offenders, (
        "Baseline test bodies changed with no individually named reviewer in "
        "tests/ADAPTATIONS.md: " + ", ".join(offenders) + ". "
        "No assertion may be weakened or removed without approval. Add an "
        "ADAPT- row naming the test and a person, then regenerate the manifest."
    )


def test_every_baseline_test_has_a_recorded_body_hash():
    """A missing hash would silently disable the guard for that test."""
    for filename, recorded in MANIFEST["files"].items():
        if filename in POST_BASELINE_FILES:
            continue
        for name, entry in recorded.items():
            assert entry.get("body_sha"), f"{filename}::{name} has no body_sha"
            assert len(entry["body_sha"]) == 16
