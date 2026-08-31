"""``app.backend.pg.migrate_pg`` against a real PostgreSQL server.

ADAPT-002 in ``tests/ADAPTATIONS.md``: retargeted from the SQLite migration
runner (``tests/test_migrations.py``) to the PostgreSQL one. The intent is
unchanged - migrations apply cleanly, are idempotent, drift is detected, and a
boot-time check never writes - but the mechanism (a real server, not a file)
and the specific guarantees (checksums, ``schema_migrations``, PostgreSQL
extensions) are PostgreSQL-specific and have no SQLite analogue.

These tests deliberately do NOT use ``pg_connection``/``pg_database``: both
are seeded from ``pg_template``, which is already fully migrated. Testing the
runner itself needs a database with no schema on it at all.
"""

from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# They deliberately do NOT live in a tests/pg/conftest.py: two conftest.py
# files in non-package directories both import under the bare module name
# `conftest`, and the subdirectory one shadows the root one -- which broke
# `from conftest import code_of, detail` in seven baseline test modules.
# Importing the fixtures by name into this module's namespace makes them
# available to pytest here, with no second conftest to collide.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import psycopg
import pytest
from psycopg import sql

import conftest_pg
from app.backend.pg import migrate_pg

pytestmark = pytest.mark.pg


@pytest.fixture()
def bare_pg_connection(pg_url, pg_admin_connection):
    """A disposable database with NO schema at all - not templated from
    ``pg_template`` - so the migration runner can be exercised from nothing."""
    name = conftest_pg._next_test_db_name()
    pg_admin_connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(
            conftest_pg._replace_dbname(pg_url, name), autocommit=False
        ) as con:
            yield con
    finally:
        pg_admin_connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(conftest_pg._assert_pg_disposable(name))))


def _table_exists(con, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s", (name,)).fetchone()
    return row is not None


def _extension_installed(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (name,)).fetchone()
    return row is not None


# ----------------------------------------------------------------------- discover
def _copy_real_migrations_into(dest: "object") -> None:
    """Every real migration file under migrations/pg/, copied byte-for-byte
    into `dest` (a `pathlib.Path`). Used so these tests exercise discover()
    against real migration content, not a synthetic stand-in, while still
    controlling exactly what else is in the directory."""
    for f in sorted(migrate_pg.MIGRATIONS_DIR.glob("*.sql")):
        (dest / f.name).write_bytes(f.read_bytes())


def test_discover_skips_seed_demo_sql_and_its_presence_does_not_change_the_version_list(tmp_path):
    """seed_demo.sql lives alongside real migrations in migrations/pg/ -- it is
    loaded by `fresh()` on `--seed`, never applied as a migration by
    `upgrade()` -- and does not match NNN_lower_snake.sql. discover() must
    skip it by name, not by loosening `_FILENAME` (which exists to keep real
    migration ordering unambiguous)."""
    without_seed = tmp_path / "without_seed"
    without_seed.mkdir()
    _copy_real_migrations_into(without_seed)

    with_seed = tmp_path / "with_seed"
    with_seed.mkdir()
    _copy_real_migrations_into(with_seed)
    (with_seed / "seed_demo.sql").write_text(
        "-- not a migration -- demo data only, loaded by fresh() on --seed\n"
        "SELECT 1;\n", encoding="utf-8")

    without_versions = [m.version for m in migrate_pg.discover(without_seed)]
    with_migrations = migrate_pg.discover(with_seed)
    with_versions = [m.version for m in with_migrations]

    assert without_versions, "the real migration set copied into the fixture must not be empty"
    assert with_versions == without_versions, (
        "seed_demo.sql's presence must not change which versions discover() reports")
    assert "seed_demo.sql" not in {m.path.name for m in with_migrations}
    assert "seed_demo" not in {m.name for m in with_migrations}


def test_discover_still_rejects_a_genuinely_unrecognised_filename(tmp_path):
    """The NON_MIGRATION_FILES skip-list is not a general escape hatch: a
    filename that is neither a real migration nor a known non-migration data
    file must still fail loudly, exactly as before."""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "not_a_migration.sql").write_text("-- x\n", encoding="utf-8")

    with pytest.raises(migrate_pg.MigrationError, match="does not match"):
        migrate_pg.discover(bad)


# ------------------------------------------------------------------------ upgrade
def test_upgrade_applies_001_cleanly(bare_pg_connection):
    con = bare_pg_connection
    performed = migrate_pg.upgrade(con)
    con.commit()

    assert performed == _all_versions()
    for table in ("organisation", "entity", "app_user", "accounting_period",
                  "audit_log", "audit_anchor", "schema_migrations"):
        assert _table_exists(con, table), f"migration 001 did not create {table}"
    for ext in ("ltree", "pgcrypto", "btree_gist"):
        assert _extension_installed(con, ext), f"migration 001 did not install extension {ext}"

    rows = con.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    assert len(rows) == len(_all_versions())
    assert [r[0] for r in rows] == _all_versions()
    assert rows[0][1] == "foundation"
    assert len(rows[0][2]) == 64  # sha256 hexdigest


def _all_versions():
    """Every migration version on disk, in order.

    These assertions were written against a one-migration world and pinned to
    the literal "001". migrations/pg/ now holds 002, and will hold 003 at the
    next milestone, so a literal makes the runner's own regression suite fail
    every time the product grows -- which invites relaxing the assertions
    rather than parameterising them.
    """
    from app.backend.pg import migrate_pg
    return [m.version for m in migrate_pg.discover()]


def test_upgrade_is_idempotent(bare_pg_connection):
    con = bare_pg_connection
    first = migrate_pg.upgrade(con)
    con.commit()
    assert first == _all_versions()

    second = migrate_pg.upgrade(con)
    con.commit()
    assert second == [], "re-running upgrade() against an up-to-date schema must apply nothing"

    rows = con.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert rows[0] == len(_all_versions()), (
        "a re-run must not duplicate any schema_migrations record")


# ------------------------------------------------------------------------- status
def test_status_reports_current_after_upgrade(bare_pg_connection):
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    state = migrate_pg.status(con)
    assert state["applied"] == _all_versions()
    assert state["pending"] == []
    assert state["drifted"] == []
    assert state["current"] == _all_versions()[-1]
    assert state["latest_available"] == _all_versions()[-1]
    assert state["is_current"] is True


def test_status_reports_pending_before_upgrade(bare_pg_connection):
    con = bare_pg_connection
    state = migrate_pg.status(con)
    assert state["applied"] == []
    assert state["pending"] == _all_versions()
    assert state["is_current"] is False


# --------------------------------------------------------------------------- drift
def test_changed_checksum_after_apply_raises_migration_error(bare_pg_connection):
    """A migration whose recorded checksum no longer matches the file on disk
    is a hard error - environments must never be allowed to silently diverge."""
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    con.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '001'",
                ("0" * 64,))
    con.commit()

    with pytest.raises(migrate_pg.MigrationError, match="checksum|changed|diverged"):
        migrate_pg.upgrade(con)


def test_drift_is_reported_by_status_without_raising(bare_pg_connection):
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    con.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '001'",
                ("f" * 64,))
    con.commit()

    state = migrate_pg.status(con)
    assert state["drifted"] == ["001"]
    assert state["is_current"] is False


# ------------------------------------------------------------------ boot-time check
def test_assert_schema_current_raises_when_behind_and_writes_nothing(bare_pg_connection):
    con = bare_pg_connection

    with pytest.raises(migrate_pg.MigrationError, match="behind"):
        migrate_pg.assert_schema_current(con)
    con.commit()

    # The counterpart to DEF-01's fix: a boot-time check reads and refuses,
    # it never applies the pending migration itself.
    assert not _table_exists(con, "organisation"), (
        "assert_schema_current() must never apply a pending migration - it "
        "checks and refuses to serve, it does not write")
    state = migrate_pg.status(con)
    assert state["applied"] == [], "no migration content should have been applied"


def test_assert_schema_current_raises_on_drift_and_writes_nothing(bare_pg_connection):
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    con.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '001'",
                ("a" * 64,))
    con.commit()

    with pytest.raises(migrate_pg.MigrationError, match="drift"):
        migrate_pg.assert_schema_current(con)
    con.commit()

    # Reading again must show the same (drifted) recorded checksum - nothing
    # was rewritten or "repaired" by the check.
    row = con.execute("SELECT checksum FROM schema_migrations WHERE version = '001'").fetchone()
    assert row[0] == "a" * 64


def test_assert_schema_current_passes_silently_when_current(bare_pg_connection):
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    migrate_pg.assert_schema_current(con)  # must not raise
