"""Schema migration guarantees.

A financial-control database is only trustworthy if the schema that carries its
constraints can be rebuilt and re-applied deterministically:

  * ``--fresh --seed`` produces a database at the latest version;
  * re-running the runner is a no-op (migrations are idempotent);
  * ``PRAGMA foreign_key_check`` is clean afterwards;
  * every control object migration 002 introduces is actually present.

These tests build their own databases in ``tmp_path`` and never reuse the shared
template, so they exercise the runner rather than its output.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from app.backend import migrate
from conftest import PROJECT_ROOT, _assert_disposable

EXPECTED_VERSIONS = ["001", "002"]

CONTROL_TABLES = [
    "app_role", "user_role", "app_credential", "app_session", "idempotency_key",
    "external_document", "pr_reservation", "reconciliation_exception", "lifecycle_state",
    "budget_line", "schema_migration",
]

CONTROL_TRIGGERS = [
    "budget_line_original_immutable", "budget_line_no_delete",
    "audit_log_append_only_update", "audit_log_append_only_delete",
    "wbs_no_self_parent_insert", "wbs_no_cycle_update", "wbs_parent_same_project",
    "bill_line_po_ownership_insert", "bill_line_po_ownership_update",
    "po_line_project_ownership", "grn_line_po_ownership", "pr_project_wbs_consistency",
    "po_line_non_negative", "wbs_progress_range_insert", "wbs_progress_range_update",
    "wbs_flags_follow_state", "asset_allocation_positive", "asset_allocation_writeoff_reason",
]


def _run_migrate(db_path, *args):
    env = dict(os.environ)
    env["CAPEX_DB_PATH"] = str(db_path)
    env.pop("CAPEX_PROFILE", None)
    return subprocess.run(
        [sys.executable, "-m", "app.backend.migrate", "--db", str(db_path), *args],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True)


@pytest.fixture(scope="session")
def _cli_built_db(tmp_path_factory):
    """Built once by the documented command line, then copied per test."""
    target = _assert_disposable(tmp_path_factory.mktemp("migrate-cli") / "capex.db")
    proc = _run_migrate(target, "--fresh", "--seed")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert target.exists()
    return target


@pytest.fixture()
def fresh_db(_cli_built_db, tmp_path):
    """A private copy of the command-line-built database."""
    target = _assert_disposable(tmp_path / "migrated" / "capex.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_cli_built_db, target)
    return target


def _objects(path, kind):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,))}
    finally:
        con.close()


# ============================================================== fresh + seed
def test_migrations_fresh_seed_produces_a_database_at_the_latest_version(tmp_path):
    """The documented command line, end to end, in its own directory."""
    target = _assert_disposable(tmp_path / "cli" / "capex.db")
    proc = _run_migrate(target, "--fresh", "--seed")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert target.exists()
    applied = [(v, ok) for v, _n, ok in migrate.status(str(target))]
    assert applied == [(v, True) for v in EXPECTED_VERSIONS], applied


def test_migrations_discover_matches_the_migration_files_on_disk():
    versions = [v for v, _n, _p in migrate.discover()]
    assert versions == sorted(versions), "migrations must be discovered in order"
    assert versions == EXPECTED_VERSIONS
    assert len(set(versions)) == len(versions), "duplicate migration version"


def test_migrations_record_themselves_in_schema_migration(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        rows = list(con.execute(
            "SELECT version, name, applied_at, checksum FROM schema_migration ORDER BY version"))
    finally:
        con.close()
    assert [r[0] for r in rows] == EXPECTED_VERSIONS
    for _version, name, applied_at, checksum in rows:
        assert name and applied_at and checksum


def test_migrations_seed_loads_the_demonstration_dataset(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("project", "wbs_element", "budget_line", "purchase_request",
                                "purchase_order", "po_line", "bill", "grn")}
    finally:
        con.close()
    assert counts["project"] == 3
    assert counts["wbs_element"] >= 15
    assert counts["purchase_request"] == 20
    assert counts["purchase_order"] == 15
    assert all(v > 0 for v in counts.values()), counts


# ============================================================== idempotency
def test_migrations_are_idempotent(fresh_db):
    assert migrate.upgrade(str(fresh_db), backup=False) == []
    second = _run_migrate(fresh_db, "--upgrade")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already current" in second.stdout
    assert [ok for _v, _n, ok in migrate.status(str(fresh_db))] == [True] * len(EXPECTED_VERSIONS)


def test_migrations_repeated_upgrade_does_not_duplicate_schema_migration_rows(fresh_db):
    for _ in range(3):
        migrate.upgrade(str(fresh_db), backup=False)
    con = sqlite3.connect(fresh_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == \
            len(EXPECTED_VERSIONS)
    finally:
        con.close()


def test_migrations_upgrade_backs_the_database_up_before_touching_it(tmp_path):
    """A partially migrated database must be recoverable."""
    from app.backend import db as dbmod
    target = _assert_disposable(tmp_path / "v1" / "capex.db")
    target.parent.mkdir(parents=True, exist_ok=True)

    original = dbmod.DB_PATH
    try:
        dbmod.DB_PATH = str(target)
        dbmod.reset_and_seed()                       # migration 001 only
    finally:
        dbmod.DB_PATH = original

    con = sqlite3.connect(target)
    con.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
        version TEXT PRIMARY KEY, name TEXT NOT NULL,
        applied_at TEXT NOT NULL, checksum TEXT)""")
    con.execute("INSERT INTO schema_migration VALUES ('001','initial_schema','now','0')")
    con.commit()
    con.close()

    applied = migrate.upgrade(str(target))
    assert applied == ["002_financial_controls"]
    backup = target.parent / f"{target.name}.pre-002.bak"
    assert backup.exists(), "upgrade must copy the database before altering it"

    backup_objects = _objects(backup, "trigger")
    assert "audit_log_append_only_update" not in backup_objects
    assert "audit_log_append_only_update" in _objects(target, "trigger")


def test_migrations_fresh_preserves_an_existing_database_instead_of_deleting_it(fresh_db):
    before = fresh_db.stat().st_size
    migrate.fresh(str(fresh_db), seed=True)
    preserved = list(fresh_db.parent.glob("capex.db.replaced-*"))
    assert preserved, "an existing database must be moved aside, not destroyed"
    assert preserved[0].stat().st_size == before


def test_migrations_upgrade_refuses_a_database_that_does_not_exist(tmp_path):
    missing = _assert_disposable(tmp_path / "nowhere" / "capex.db")
    with pytest.raises(SystemExit):
        migrate.upgrade(str(missing))


# ============================================================== integrity
def test_migrations_foreign_key_check_is_clean(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()
    assert violations == [], violations[:10]


def test_migrations_integrity_check_is_ok(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_migrations_create_every_control_table(fresh_db):
    missing = set(CONTROL_TABLES) - _objects(fresh_db, "table")
    assert not missing, f"missing control tables: {sorted(missing)}"


def test_migrations_create_every_control_trigger(fresh_db):
    missing = set(CONTROL_TRIGGERS) - _objects(fresh_db, "trigger")
    assert not missing, f"missing control triggers: {sorted(missing)}"


def test_migrations_create_the_external_document_unique_indexes(fresh_db):
    indexes = _objects(fresh_db, "index")
    assert {"ux_bill_external", "ux_po_external", "ux_pr_reservation_live"} <= indexes


def test_migrations_populate_the_lifecycle_state_table(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        rows = {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            "SELECT object_type, state, allows_procurement, allows_posting FROM lifecycle_state")}
    finally:
        con.close()
    assert rows[("project", "Released")] == (1, 1)
    assert rows[("project", "Draft")] == (0, 0)
    assert rows[("wbs", "Closed")] == (0, 0)
    assert rows[("wbs", "Technically Completed")] == (0, 1)


def test_migrations_normalise_historical_revision_signs(fresh_db):
    """Migration 002 rewrites RETURN/TRANSFER_OUT rows to a negative amount."""
    con = sqlite3.connect(fresh_db)
    try:
        rows = list(con.execute("SELECT kind, amount_paise FROM budget_line"))
    finally:
        con.close()
    for kind, amount in rows:
        if kind in ("RETURN", "TRANSFER_OUT"):
            assert amount < 0, (kind, amount)
        else:
            assert amount > 0, (kind, amount)


def test_migrations_derive_the_permission_flags_from_lifecycle_state(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        rows = list(con.execute(
            """SELECT w.wbs_id, w.status, w.is_abandoned, w.allow_procurement,
                      COALESCE(l.allows_procurement, 0)
               FROM wbs_element w
               LEFT JOIN lifecycle_state l
                 ON l.object_type='wbs' AND l.state = w.status"""))
    finally:
        con.close()
    for wbs_id, _status, abandoned, flag, expected in rows:
        assert flag == (0 if abandoned else expected), wbs_id


# ============================================================== fresh, unseeded
def test_migrations_fresh_without_seed_is_empty_but_current(tmp_path):
    target = _assert_disposable(tmp_path / "empty" / "capex.db")
    migrate.fresh(str(target), seed=False)
    assert [ok for _v, _n, ok in migrate.status(str(target))] == [True] * len(EXPECTED_VERSIONS)
    con = sqlite3.connect(target)
    try:
        assert con.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 0
        # the control furniture is still there
        assert con.execute("SELECT COUNT(*) FROM lifecycle_state").fetchone()[0] > 0
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_migrations_status_on_a_missing_database_reports_nothing_applied(tmp_path):
    missing = _assert_disposable(tmp_path / "absent" / "capex.db")
    assert [ok for _v, _n, ok in migrate.status(str(missing))] == [False] * len(EXPECTED_VERSIONS)


def test_migrations_cli_status_is_reportable(fresh_db):
    proc = _run_migrate(fresh_db, "--status")
    assert proc.returncode == 0, proc.stderr
    for version in EXPECTED_VERSIONS:
        assert f"[x] {version}" in proc.stdout
