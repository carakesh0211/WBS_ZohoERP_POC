"""Review finding F5: ``migrate_pg.upgrade()``'s adoption path must verify
more than table existence.

A legacy database can carry every table name a migration would create while
missing something that matters:

  * A dump taken before the append-only triggers existed carries all 12 of
    001's tables but none of ``audit_log``'s/``audit_anchor``'s triggers --
    silently "adopting" it reports the schema current while
    ``audit_log`` is UPDATE/DELETE-able again.
  * A hand-applied ``budget_control_cell`` can carry ``budget_paise
    numeric(18,2)`` instead of ``bigint`` -- all five of 002's table names
    are present, but every write then stores a ``Decimal`` where the domain
    requires an integer number of paise.

``app/backend/pg/migrate_pg.py``'s adoption check now verifies tables,
functions, triggers, explicitly named constraints, the accounting_period
gist exclusion, and every ``*_paise`` column's actual type before recording a
migration as adopted. This module tests both halves:

  * the regex parsers, against the real migration files, with no database
    required;
  * the live behaviour, against a real disposable PostgreSQL database,
    gated (as `tests/test_pg_locking.py` is) behind ``CAPEX_DB_URL``.
"""
from __future__ import annotations

import os
import re

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see the
# identical note in tests/test_pg_migrations_runner.py and
# tests/test_pg_locking.py: conftest_pg.py is deliberately not auto-discovered,
# so its fixtures must be imported by name into this module's namespace.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402

import conftest_pg  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402

# `pytest.ini` registers the `pg` marker; CAPEX_DB_URL is what actually makes
# the live tests skip cleanly (there is no local PostgreSQL on the dev
# machine), so both are applied to every test below that touches a real
# connection -- mirroring tests/test_pg_locking.py's `PG` exactly.
PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


def _migration(version: str) -> migrate_pg.Migration:
    for m in migrate_pg.discover():
        if m.version == version:
            return m
    raise AssertionError(f"migration {version} not found on disk under {migrate_pg.MIGRATIONS_DIR}")


# =========================================================================
# Parser-level tests -- no database. Run against the real migration files,
# never a synthetic fixture, so a parser bug that only shows up against the
# real SQL's formatting (multi-line triggers, inline comments, nested
# parens in CHECK/EXCLUDE) is actually caught.
# =========================================================================

def test_parser_finds_001_function():
    migration = _migration("001")
    assert migrate_pg._functions_created_by(migration) == ["assert_append_only"]


def test_parser_finds_001_audit_triggers():
    migration = _migration("001")
    triggers = set(migrate_pg._triggers_created_by(migration))
    assert triggers == {
        ("audit_log_no_update", "audit_log"),
        ("audit_log_no_delete", "audit_log"),
        ("audit_anchor_no_update", "audit_anchor"),
        ("audit_anchor_no_delete", "audit_anchor"),
    }, "these four triggers are exactly what failure A (audit immutability) loses"


def test_parser_finds_001_accounting_period_gist_exclusion():
    migration = _migration("001")
    assert migrate_pg._exclusion_constraint_tables(migration) == ["accounting_period"]


def test_parser_finds_002_named_constraints():
    migration = _migration("002")
    named = set(migrate_pg._named_constraints_by(migration))
    # The money non-negative checks in particular -- these are the named
    # constraints an adoption must not silently skip.
    assert ("budget_control_cell", "ck_control_cell_budget_nonneg") in named
    assert ("budget_ledger_cell", "fk_ledger_control_cell") in named
    assert ("wbs_element", "fk_wbs_parent_same_project") in named
    assert ("wbs_element", "ck_wbs_path_depth") in named
    assert ("wbs_element", "ck_wbs_not_own_parent") in named
    for head in ("original", "future", "ordered", "commitment", "received",
                 "received_not_billed", "pr_reserved"):
        assert ("budget_ledger_cell", f"ck_ledger_{head}_nonneg") in named


def test_parser_finds_every_paise_column_across_both_migrations():
    m001, m002 = _migration("001"), _migration("002")

    assert migrate_pg._paise_columns_by(m001) == [], (
        "001_foundation.sql declares no monetary columns")

    paise_002 = set(migrate_pg._paise_columns_by(m002))
    assert paise_002 == {
        ("budget_control_cell", "budget_paise"),
        ("budget_ledger_cell", "original_paise"),
        ("budget_ledger_cell", "revisions_paise"),
        ("budget_ledger_cell", "future_budget_paise"),
        ("budget_ledger_cell", "ordered_paise"),
        ("budget_ledger_cell", "commitment_paise"),
        ("budget_ledger_cell", "actual_paise"),
        ("budget_ledger_cell", "received_paise"),
        ("budget_ledger_cell", "received_not_billed_paise"),
        ("budget_ledger_cell", "pr_reserved_paise"),
    }, "every *_paise column 002 declares must be found, or drift like budget_paise numeric(18,2) is invisible"


def test_parser_ignores_unnamed_table_level_constraints():
    """Documents the deliberate gap: a plain ``UNIQUE (code)`` has no name to
    look up in pg_constraint, so it is not reported by the named-constraint
    parser. Best-effort parsing prefers not verifying an object over
    guessing at its auto-generated name."""
    migration = _migration("001")
    named = migrate_pg._named_constraints_by(migration)
    assert named == [], "001 has no explicitly CONSTRAINT-named table constraints"


# =========================================================================
# Live tests -- a real, disposable PostgreSQL database with NO schema at
# all, mirroring test_pg_migrations_runner.py's bare_pg_connection. Not
# templated from pg_template (which is already fully migrated through the
# normal, non-adoption path): these tests need full control over what
# exists before upgrade() is called.
# =========================================================================

@pytest.fixture()
def bare_pg_connection(pg_url, pg_admin_connection):
    """A disposable database with NO schema at all."""
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


def _recorded_version(con, version: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)).fetchone()
    return row is not None


def _ledger_table_exists(con) -> bool:
    row = con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'schema_migrations'"
    ).fetchone()
    return row is not None


@PG
@pytest.mark.pg
def test_adoption_succeeds_when_schema_is_genuinely_complete(bare_pg_connection):
    con = bare_pg_connection
    m001, m002 = _migration("001"), _migration("002")

    # Simulate a database restored from a dump taken before this runner
    # existed: both migrations' real, unmodified DDL has already run, but
    # neither is recorded in schema_migrations.
    con.execute(m001.sql)
    con.execute(m002.sql)
    con.commit()

    performed = migrate_pg.upgrade(con)
    con.commit()

    assert performed == ["001 (adopted)", "002 (adopted)"]
    assert _recorded_version(con, "001")
    assert _recorded_version(con, "002")


@PG
@pytest.mark.pg
def test_adoption_refused_when_a_trigger_is_missing(bare_pg_connection):
    """Failure A: a legacy dump taken before the append-only triggers
    existed must not be silently adopted -- that would report the schema
    current while audit_log is UPDATE/DELETE-able again."""
    con = bare_pg_connection
    m001 = _migration("001")

    con.execute(m001.sql)
    con.execute("DROP TRIGGER audit_log_no_update ON audit_log")
    con.commit()

    with pytest.raises(migrate_pg.MigrationError, match="audit_log_no_update"):
        migrate_pg.upgrade(con)
    con.rollback()

    assert not _recorded_version(con, "001"), (
        "a migration missing one of its own triggers must not be adopted")


@PG
@pytest.mark.pg
def test_adoption_refused_when_budget_paise_is_numeric(bare_pg_connection):
    """Failure B: a hand-applied budget_control_cell with budget_paise
    numeric(18,2) must not be silently adopted -- every write would then
    store a Decimal where the domain requires bigint paise, the one
    float/Decimal-leakage path in the system."""
    con = bare_pg_connection
    m001, m002 = _migration("001"), _migration("002")

    con.execute(m001.sql)
    tampered_002, replaced = re.subn(
        r"budget_paise\s+bigint", "budget_paise   numeric(18,2)", m002.sql, count=1)
    assert replaced == 1, "fixture bug: expected exactly one budget_paise bigint column to tamper"
    con.execute(tampered_002)
    con.commit()

    with pytest.raises(migrate_pg.MigrationError,
                        match=r"budget_control_cell\.budget_paise.*numeric.*bigint"):
        migrate_pg.upgrade(con)
    con.rollback()

    assert not _recorded_version(con, "002"), (
        "a hand-applied numeric budget_paise column must not be adopted")


@PG
@pytest.mark.pg
def test_partial_table_presence_is_a_genuine_conflict_not_an_adoption(bare_pg_connection):
    con = bare_pg_connection
    m001 = _migration("001")

    con.execute(m001.sql)
    con.execute("DROP TABLE department")
    con.commit()

    with pytest.raises(migrate_pg.MigrationError, match="department"):
        migrate_pg.upgrade(con)
    con.rollback()

    assert not _recorded_version(con, "001"), (
        "partial table presence must raise a genuine conflict, never be adopted")


@PG
@pytest.mark.pg
def test_assert_schema_current_writes_nothing_even_against_an_adoptable_schema(bare_pg_connection):
    """assert_schema_current() is the boot-time, read-only counterpart to
    upgrade() -- it must never create the ledger table, not even against a
    database whose schema happens to already look adoptable."""
    con = bare_pg_connection
    m001, m002 = _migration("001"), _migration("002")

    con.execute(m001.sql)
    con.execute(m002.sql)
    con.commit()

    with pytest.raises(migrate_pg.MigrationError, match="behind"):
        migrate_pg.assert_schema_current(con)
    con.rollback()

    assert not _ledger_table_exists(con), (
        "assert_schema_current() must perform no write at all -- not even the "
        "ledger table's own CREATE TABLE IF NOT EXISTS -- regardless of "
        "whether the schema underneath it happens to be adoptable")
