"""Operational readiness: alerts, runbooks, health/readiness, restore drill.

Runs everywhere. No PostgreSQL, no network, no cloud.

The theme is one rule: **a runbook nobody has executed is a comment, and a
status nobody checks is a claim.** So this file does three kinds of work:

* it BINDS the written alert definitions to ``ALERT_CONDITIONS`` in both
  directions, so neither side can grow a condition the other has not heard of;
* it EXERCISES the restore drill against disposable databases it builds itself,
  in both directions -- a good restore must pass and a chain-breaking restore
  must FAIL with a non-zero exit;
* it holds the ``UNVERIFIED`` labels honest, because the failure mode this
  project keeps hitting is a document that is true of the intent and false of
  the code.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import observability                              # noqa: E402
from app.backend.api import health as health_api                   # noqa: E402
from tools import restore_drill                                    # noqa: E402

RUNBOOKS = PROJECT_ROOT / "docs" / "runbooks"
ALERT_DEFINITIONS = RUNBOOKS / "alert-definitions.json"

VALID_STATUSES = {"VERIFIED-LOCAL", "VERIFIED-CI", "UNVERIFIED"}


def _definitions() -> dict:
    return json.loads(ALERT_DEFINITIONS.read_text(encoding="utf-8"))


# ============================================================ alert definitions
def test_every_alert_condition_in_code_has_a_written_response():
    """The direction that was open.

    ``observability.alert()`` already refuses an UNKNOWN condition at runtime,
    so a condition cannot be raised without being declared. Nothing stopped a
    condition being DECLARED with no runbook behind it -- and the docstring on
    ``alert()`` says a condition needs "a severity and a runbook entry before
    raising it", which until now was an instruction with no enforcement.
    """
    written = set(_definitions()["alerts"])
    assert set(observability.ALERT_CONDITIONS) <= written, (
        "alert conditions with no written response: "
        f"{sorted(set(observability.ALERT_CONDITIONS) - written)}")


def test_no_response_is_written_for_a_condition_that_does_not_exist():
    """The other direction. A runbook for a condition nothing raises sends
    someone looking for an alert that cannot arrive."""
    written = set(_definitions()["alerts"])
    assert written <= set(observability.ALERT_CONDITIONS), (
        "written responses for conditions absent from ALERT_CONDITIONS: "
        f"{sorted(written - set(observability.ALERT_CONDITIONS))}")


def test_the_two_p1_conditions_are_the_money_and_the_history_ones():
    """A severity table where everything is P1 is a severity table nobody
    reads. These two are P1 because they are the two that mean a number or a
    history cannot be stood behind."""
    alerts = _definitions()["alerts"]
    p1 = {name for name, spec in alerts.items() if spec["severity"] == "P1"}
    assert p1 == {"LEDGER_DIVERGENCE", "AUDIT_CHAIN_BROKEN"}


def test_every_alert_carries_a_threshold_and_a_first_response():
    for name, spec in _definitions()["alerts"].items():
        for field in ("severity", "summary", "why_it_matters", "detection",
                      "threshold", "notification", "first_response", "escalate_if"):
            assert spec.get(field), f"{name} has no {field}"
        assert isinstance(spec["first_response"], list) and spec["first_response"], name


def test_the_ledger_divergence_threshold_admits_no_tolerance():
    """A paisa tolerance on a derived money column is a paisa of unexplained
    money, and it compounds."""
    spec = _definitions()["alerts"]["LEDGER_DIVERGENCE"]
    assert "ANY non-zero difference" in spec["threshold"]
    assert "no tolerance" in spec["threshold"]


def test_the_audit_chain_response_forbids_recomputing_a_hash_first():
    """The single most dangerous "helpful" reflex in an incident.

    Recomputing makes a tampered chain verify and destroys the only evidence
    there is, so the instruction must be the FIRST line of the response, not a
    caveat further down where someone mid-incident will not reach it.
    """
    first = _definitions()["alerts"]["AUDIT_CHAIN_BROKEN"]["first_response"][0]
    assert "Do NOT recompute" in first


def test_the_alert_delivery_path_is_declared_unverified():
    """``ALERT_CONDITIONS``'s own comment says "Wired to the sink in Phase 1".
    Nothing consumes the log record, so no alert has ever been delivered, and
    the definitions file has to say so rather than imply a working pager."""
    verification = _definitions()["verification"]
    assert verification["firing"].startswith("UNVERIFIED")
    assert "no alert has ever been delivered to a sink" in verification["firing"]


def test_raising_an_undefined_condition_still_fails_loudly():
    """The runtime half of the same binding, re-asserted here so that
    weakening it shows up in the readiness suite too."""
    with pytest.raises(ValueError, match="Unknown alert condition"):
        observability.alert("NOT_A_REAL_CONDITION", "x")


# ==================================================================== runbooks
def test_every_runbook_named_in_the_index_exists():
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    import re
    for match in re.finditer(r"\(([a-z0-9-]+\.md)\)", index):
        assert (RUNBOOKS / match.group(1)).exists(), match.group(1)


def test_every_runbook_declares_a_status_from_the_defined_set():
    """A runbook with no status is a runbook whose reader has to guess whether
    anyone has run it."""
    for path in sorted(RUNBOOKS.glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        assert any(status in text for status in VALID_STATUSES), (
            f"{path.name} declares no verification status")


def test_the_unverified_runbooks_say_plainly_that_they_have_never_been_run():
    """"UNVERIFIED" as a bare word is a label. The runbook has to say what it
    means, in the body, where someone about to follow it will read it."""
    for name in ("credential-rotation.md", "encryption-key-rotation.md"):
        text = (RUNBOOKS / name).read_text(encoding="utf-8")
        assert "UNVERIFIED" in text
        assert "never" in text.lower()
        assert "design" in text.lower()


def test_the_backup_runbook_states_the_failed_restore_rule():
    text = (RUNBOOKS / "backup-restore.md").read_text(encoding="utf-8")
    assert "A restore that breaks the audit chain is a FAILED restore" in text


def test_the_migration_runbook_records_the_measured_strategy_outcomes():
    """The four candidates and their measured results, not an argument."""
    from tools.migration import audit_chain
    text = (RUNBOOKS / "poc-to-postgres-migration.md").read_text(encoding="utf-8")
    assert "seq` is not in the hashed payload" in text or "not in the hashed payload" in text
    assert audit_chain.CHOSEN_STRATEGY.replace("_", " ") in text.replace("-", " ") \
        or "re-assigned 1..n" in text


# ============================================================== restore drill
GOOD_SCHEMA = """
CREATE TABLE schema_migration (
  version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL, checksum TEXT
);
CREATE TABLE audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  object_type TEXT NOT NULL, object_id TEXT NOT NULL, detail TEXT,
  prev_hash TEXT, entry_hash TEXT, correlation_id TEXT
);
"""


def _restored(path: Path, *, entries: int = 3, seed_row: bool = True) -> Path:
    from app.backend import services
    con = sqlite3.connect(path)
    con.executescript(GOOD_SCHEMA)
    con.execute("INSERT INTO schema_migration VALUES ('001','initial_schema','t',NULL)")
    con.execute("INSERT INTO schema_migration VALUES ('002','financial_controls','t',NULL)")
    if seed_row:
        con.execute("INSERT INTO audit_log (at,actor,action,object_type,object_id,detail) "
                    "VALUES ('2026-01-01T00:00:00','U-ADM','SEED','System','-','seeded')")
    con.row_factory = sqlite3.Row
    for i in range(entries):
        services.audit(con, f"U-{i}", "APPROVE", "PurchaseRequest", f"PR-{i}", f"d{i}")
    con.commit()
    con.close()
    return path


def test_a_sound_restore_passes_the_drill(tmp_path: Path):
    report = restore_drill.drill_sqlite(_restored(tmp_path / "ok.db"))
    assert report["passed"] is True
    assert [c["check"] for c in report["checks"]] == [
        "opens", "schema_revision", "row_counts", "audit_chain"]


def test_a_restore_that_broke_the_chain_FAILS_the_drill(tmp_path: Path):
    """The gate. Not a warning, not a note in the report -- a failure."""
    path = _restored(tmp_path / "bad.db")
    con = sqlite3.connect(path)
    con.execute("UPDATE audit_log SET detail='quietly edited' WHERE audit_id=3")
    con.commit()
    con.close()
    with pytest.raises(restore_drill.DrillFailure, match="BROKE THE AUDIT CHAIN"):
        restore_drill.drill_sqlite(path)


def test_the_drill_exits_non_zero_on_a_broken_chain(tmp_path: Path):
    """A scheduled drill is judged by its exit code, not by its prose."""
    path = _restored(tmp_path / "bad2.db")
    con = sqlite3.connect(path)
    con.execute("UPDATE audit_log SET actor='someone else' WHERE audit_id=2")
    con.commit()
    con.close()
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "restore_drill.py"),
         "--sqlite", str(path)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert result.returncode == 1
    assert "RESTORE DRILL FAILED" in result.stdout


def test_a_restore_with_no_schema_revision_is_refused(tmp_path: Path):
    """A restore whose revision is unknown cannot be certified, and reporting
    it as healthy is how the wrong backup gets promoted."""
    path = tmp_path / "norev.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE audit_log (audit_id INTEGER PRIMARY KEY, "
                      "at TEXT, actor TEXT, action TEXT, object_type TEXT, "
                      "object_id TEXT, detail TEXT, prev_hash TEXT, entry_hash TEXT)")
    con.commit()
    con.close()
    with pytest.raises(restore_drill.DrillFailure, match="no schema_migration"):
        restore_drill.drill_sqlite(path)


def test_an_empty_restore_does_not_pass_on_row_counts(tmp_path: Path):
    """An empty database opens perfectly and verifies perfectly. Check 3 is the
    only thing between that and a green drill."""
    path = _restored(tmp_path / "empty.db", entries=0, seed_row=False)
    with pytest.raises(restore_drill.DrillFailure, match="row counts"):
        restore_drill.drill_sqlite(path, expected_counts={"audit_log": 4})


def test_the_drill_reports_the_unhashed_row_count(tmp_path: Path):
    """"intact: true" over a database of entirely unverifiable rows is a pass
    that means nothing. The count is where verifiable history begins."""
    report = restore_drill.drill_sqlite(_restored(tmp_path / "u.db", seed_row=True))
    audit_check = [c for c in report["checks"] if c["check"] == "audit_chain"][0]
    assert audit_check["unhashed_rows"] == 1
    assert "verifiable history begins" in audit_check["unhashed_note"]


def test_the_drill_uses_the_products_verifier_not_a_copy():
    """A drill that checks the chain with its own hash walk proves the two
    copies agree, which is not the question being asked."""
    import inspect
    source = inspect.getsource(restore_drill.check_audit_chain_sqlite)
    assert "from app.backend.services import verify_audit_chain" in source
    assert "sha256" not in source


def test_the_postgres_drill_path_is_declared_unverified():
    assert "UNVERIFIED" in restore_drill.__doc__
    assert "never run" in restore_drill.__doc__ or "never executed" in restore_drill.__doc__


# ======================================================== health and readiness
def test_healthz_touches_no_database_at_all():
    """A property of the HANDLER, not of a response body.

    A ``/healthz`` that quietly touched the database would pass every
    behavioural test run on a machine where the database was up -- and would
    fail in production at exactly the moment its answer mattered most.
    """
    import inspect
    source = inspect.getsource(health_api.healthz)
    for forbidden in ("get_database", "session", "execute", "fetch", "readiness"):
        assert forbidden not in source, f"/healthz references {forbidden!r}"


def test_healthz_answers_with_no_database_configured():
    assert health_api.healthz() == {"status": "ok", "check": "liveness"}


def test_readyz_reports_503_and_a_class_name_when_nothing_is_configured():
    """Not a 500, not a stack trace, and never the driver's message -- a libpq
    error echoes host, user and database name."""
    response = health_api.readyz()
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["check"] == "readiness"
    assert body["error_class"] == "DatabaseNotConfigured"
    assert "password" not in response.body.decode().lower()


def test_readyz_does_no_unbounded_work():
    """The 30-second budget cannot be blown by data volume.

    The cold-start LATENCY is unverified -- there is no deployment here to
    measure. What is checkable is that readiness does no full scan, no
    per-table loop and no migration application, so the budget is not a
    function of how much data the tenant has.
    """
    import inspect
    source = inspect.getsource(health_api)
    for forbidden in ("COUNT(*)", "for table in", "upgrade(", "apply("):
        assert forbidden not in source


def test_the_health_runbook_marks_the_cold_start_timing_unverified():
    text = (RUNBOOKS / "health-and-readiness.md").read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "do not quote a cold-start latency" in text.lower()


# ============================================================== supply chain
def test_the_sbom_declares_whether_its_closure_is_complete():
    """An SBOM of the direct dependencies only, presented as an SBOM, overstates
    what has been scanned. The flag is what stops that."""
    sbom = json.loads((PROJECT_ROOT / "sbom.cyclonedx.json").read_text(encoding="utf-8"))
    properties = {p["name"]: p["value"] for p in sbom["metadata"]["properties"]}
    assert "transitive_closure_complete" in properties
    assert properties["transitive_closure_complete"] in ("true", "false")


def test_the_sbom_covers_the_declared_runtime_dependencies():
    sbom = json.loads((PROJECT_ROOT / "sbom.cyclonedx.json").read_text(encoding="utf-8"))
    names = {c["name"].lower().replace("_", "-") for c in sbom["components"]}
    for required in ("fastapi", "pydantic", "uvicorn", "psycopg"):
        assert required in names, f"{required} missing from the SBOM"
