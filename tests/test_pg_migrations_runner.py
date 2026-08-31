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


# ------------------------------------------------------------------------ upgrade
def test_upgrade_applies_001_cleanly(bare_pg_connection):
    con = bare_pg_connection
    performed = migrate_pg.upgrade(con)
    con.commit()

    assert performed == ["001"]
    for table in ("organisation", "entity", "app_user", "accounting_period",
                  "audit_log", "audit_anchor", "schema_migrations"):
        assert _table_exists(con, table), f"migration 001 did not create {table}"
    for ext in ("ltree", "pgcrypto", "btree_gist"):
        assert _extension_installed(con, ext), f"migration 001 did not install extension {ext}"

    rows = con.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "001"
    assert rows[0][1] == "foundation"
    assert len(rows[0][2]) == 64  # sha256 hexdigest


def test_upgrade_is_idempotent(bare_pg_connection):
    con = bare_pg_connection
    first = migrate_pg.upgrade(con)
    con.commit()
    assert first == ["001"]

    second = migrate_pg.upgrade(con)
    con.commit()
    assert second == [], "re-running upgrade() against an up-to-date schema must apply nothing"

    rows = con.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert rows[0] == 1, "a re-run must not duplicate the schema_migrations record"


# ------------------------------------------------------------------------- status
def test_status_reports_current_after_upgrade(bare_pg_connection):
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()

    state = migrate_pg.status(con)
    assert state["applied"] == ["001"]
    assert state["pending"] == []
    assert state["drifted"] == []
    assert state["current"] == "001"
    assert state["latest_available"] == "001"
    assert state["is_current"] is True


def test_status_reports_pending_before_upgrade(bare_pg_connection):
    con = bare_pg_connection
    state = migrate_pg.status(con)
    assert state["applied"] == []
    assert state["pending"] == ["001"]
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
