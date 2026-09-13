"""`tools/uat/skip_audit.py`: every skip in a JUnit report must carry a reason
this repository has declared as intended; a live run must not carry the
PostgreSQL gate or the approval-schema skip; too few executed tests fail."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.uat import skip_audit  # noqa: E402


def _report(tmp_path, cases):
    body = "".join(
        f'<testcase classname="t" name="{name}">'
        + (f'<skipped message="{reason}"/>' if reason else "")
        + "</testcase>"
        for name, reason in cases)
    p = tmp_path / "r.xml"
    p.write_text(f'<testsuite tests="{len(cases)}">{body}</testsuite>', encoding="utf-8")
    return str(p)


def test_intended_reasons_pass_and_the_count_is_reported(tmp_path, capsys):
    p = _report(tmp_path, [("a", None), ("b", "declared NO_ROW_SCOPE: audit reads"),
                           ("c", "PostgreSQL not configured; set CAPEX_DB_URL.")])
    assert skip_audit.main([p]) == 0
    out = capsys.readouterr().out
    assert "3 collected, 1 executed, 2 skipped" in out and "UNINTENDED" not in out


def test_an_undeclared_reason_fails_with_the_test_ids(tmp_path, capsys):
    p = _report(tmp_path, [("a", None), ("b", "flaky on Tuesdays")])
    assert skip_audit.main([p]) == 1
    out = capsys.readouterr().out
    assert "UNINTENDED" in out and "t::b" in out and "flaky on Tuesdays" in out


def test_the_live_run_does_not_accept_the_database_gate_or_the_schema_skip(tmp_path):
    p = _report(tmp_path, [("a", None), ("b", "PostgreSQL not configured; set CAPEX_DB_URL.")])
    assert skip_audit.main([p]) == 0
    assert skip_audit.main([p, "--live"]) == 1
    q = _report(tmp_path, [("a", None), ("b", "approval schema not present (x); skipped")])
    assert skip_audit.main([q, "--live"]) == 1


def test_too_few_executed_tests_fail_even_with_no_skips(tmp_path):
    p = _report(tmp_path, [("a", None), ("b", None)])
    assert skip_audit.main([p, "--expect-min-executed", "3"]) == 2
    assert skip_audit.main([p, "--expect-min-executed", "2"]) == 0
