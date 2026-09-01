"""Defects found in earlier phases, and their regression coverage.

Each DEF-01 test used to assert the DESIRED behaviour and was marked
``xfail(strict=True)``:

  * while the defect existed  -> xfail, the suite stayed green, the defect
                                  was on record
  * once someone fixed it     -> XPASS, which strict=True turns into a
                                  FAILURE, forcing the marker to be removed
                                  deliberately

DEF-01 is now fixed (Phase 1, the PostgreSQL runtime work), so its tests below
assert the fixed behaviour directly, with no xfail marker. See
docs/PHASE_0A_FINDINGS.md section 2 for the original reproduction.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg.errors


# ===========================================================================
# DEF-01  Legacy database cannot be upgraded, and run.py boots through upgrade
# ===========================================================================
#
# The defect had two parts:
#
#   1. app/run.py called migrate.upgrade() unconditionally whenever the
#      database file existed. An application must never migrate itself on
#      boot -- it cannot be rolled back, it races when scaled horizontally,
#      and it turns a schema problem into an outage. Fixed by removing all
#      migration execution from app/run.py and replacing it with a read-only
#      check-and-refuse (test_def_01_a_... and test_def_01_control_a_...
#      below, and see tests/test_runtime_startup.py for the source-level
#      guarantee that app/run.py contains no migration-execution call at
#      all).
#
#   2. The migration runner itself could not adopt a database that already
#      carried a migration's schema without a ledger entry for it -- exactly
#      the shape of a hand-applied or restored-from-dump database. Fixed in
#      the Phase 1 runner, app/backend/pg/migrate_pg.py::upgrade(), which now
#      recognises that shape and records the migration as satisfied instead
#      of replaying DDL that has already run (test_def_01_pg_adopts_a_...
#      below).
#
# app/backend/migrate.py (the legacy SQLite runner) is unowned by this phase
# of work and is not modified; its replay-on-adopt behaviour is superseded,
# not patched, by (1) and (2) above -- the boot path that used to reach it
# unconditionally no longer exists.


def test_def_01_a_legacy_v1_database_can_be_upgraded(tmp_path):
    """A database created before the migration runner existed is now handled
    without crashing the process.

    Reproduction (unchanged from the original finding)
    ----------------------------------------------------
    1. Build the v1 schema the way the pre-runner code did: executescript(SCHEMA).
       The result has the business tables and NO schema_migration ledger --
       this is what app/run.py used to hand straight to migrate.upgrade() on
       every boot when the database file already existed.

    Fixed behaviour
    ----------------
    app/run.py no longer executes any migration at boot at all. Instead it
    performs a read-only check (app.run.check_sqlite_schema) that recognises
    this exact shape -- schema present, no ledger -- and raises a clear,
    typed error naming the command an operator must run, rather than letting
    an unhandled sqlite3.OperationalError crash the process. And it is
    genuinely read-only: the database file is byte-for-byte unchanged, and no
    schema_migration table is created as a side effect of looking for one.
    """
    from app.backend import db as dbmod
    import app.run as run_mod

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(dbmod.SCHEMA)
    con.commit()
    con.close()
    raw_bytes_before = legacy.read_bytes()

    with pytest.raises(run_mod.SchemaNotCurrent) as excinfo:
        run_mod.check_sqlite_schema(str(legacy))

    # The refusal must name the exact remediation command, not just complain.
    assert "python -m app.backend.migrate" in str(excinfo.value)
    assert "--upgrade" in str(excinfo.value)

    # Refusing to serve must never write -- not even the ledger table.
    assert legacy.read_bytes() == raw_bytes_before, (
        "check_sqlite_schema must be read-only: the database file changed "
        "while only being inspected."
    )
    con = sqlite3.connect(legacy)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "schema_migration" not in tables, (
        "the boot-time check must not create the ledger table as a side "
        "effect of looking for it."
    )


def test_def_01_control_a_fresh_database_upgrades_cleanly(tmp_path):
    """The control for DEF-01. This passed before the fix and must keep
    passing after it: a fully migrated database is reported current, with no
    refusal and no write."""
    from app.backend import migrate
    import app.run as run_mod

    fresh = tmp_path / "fresh.db"
    migrate.fresh(str(fresh), seed=True)
    raw_bytes_before = fresh.read_bytes()

    run_mod.check_sqlite_schema(str(fresh))  # must not raise

    assert fresh.read_bytes() == raw_bytes_before, (
        "a read-only schema check must not modify a current database either."
    )


def test_def_01_run_py_boots_cleanly_against_a_legacy_database(tmp_path, monkeypatch):
    """End-to-end: the process that used to crash with
    sqlite3.OperationalError now exits cleanly, non-zero, and explains itself
    -- the exact outcome the original finding demanded ("Expected: the runner
    recognises the existing schema ... Actual: sqlite3.OperationalError:
    table entity already exists.")."""
    from app.backend import db as dbmod
    import app.run as run_mod

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(dbmod.SCHEMA)
    con.commit()
    con.close()

    monkeypatch.setattr(dbmod, "DB_PATH", str(legacy))
    monkeypatch.setenv("CAPEX_DB_PATH", str(legacy))
    monkeypatch.delenv("CAPEX_DB_URL", raising=False)
    monkeypatch.delenv("CAPEX_DB_HOST", raising=False)

    rc = run_mod.main([])

    assert rc == 1, "boot against a legacy database must refuse, not crash"


# ---------------------------------------------------------------------- (2)
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeAdoptConnection:
    """Simulates a PostgreSQL connection whose database already carries a
    migration's tables (an adopted legacy schema) but has never recorded that
    fact in ``schema_migrations`` -- the PostgreSQL-side shape of DEF-01's
    reproduction. No real PostgreSQL server is required: this fake implements
    exactly the ``execute`` surface ``migrate_pg`` calls.
    """

    def __init__(self, existing_tables: set[str], *,
                 missing_objects: set[str] | None = None,
                 paise_types: dict[str, str] | None = None):
        self.existing_tables = set(existing_tables)
        #: Function / trigger / constraint names this legacy database LACKS.
        self.missing_objects = set(missing_objects or ())
        #: Column name -> actual SQL type, for modelling money-type drift.
        self.paise_types = dict(paise_types or {})
        self.recorded: list[tuple] = []
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        norm = " ".join(statement.split()).upper()
        self.statements.append(norm)
        if norm.startswith("CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS"):
            return _FakeCursor([])
        if norm.startswith("SELECT VERSION, CHECKSUM FROM SCHEMA_MIGRATIONS"):
            return _FakeCursor(self.recorded)
        if norm.startswith("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"):
            wanted = set(params[0])
            return _FakeCursor([(t,) for t in wanted & self.existing_tables])

        # --- schema-shape queries added when F5 closed the adoption gap ------
        #
        # Adoption used to check table EXISTENCE only, which meant a legacy
        # database dumped before the append-only triggers existed could be
        # adopted and reported "current" with audit_log freely UPDATE-able --
        # and a hand-applied `budget_paise numeric` could be adopted as valid,
        # the one float-leakage path in the system.
        #
        # This fake modelled exactly that old contract: it answered the table
        # query and raised DuplicateTable at everything else. Once adoption
        # started asking about functions, triggers, constraints and column
        # types, the fake refused the questions and the test failed -- the fake
        # encoding an obsolete definition of "complete schema", not a defect in
        # the new checks.
        #
        # It now answers as a genuinely COMPLETE legacy schema would: every
        # object present, every *_paise column bigint. `missing_objects` and
        # `paise_types` let a test model an INCOMPLETE one instead.
        if norm.startswith("SELECT P.PRONAME FROM PG_PROC"):
            wanted = set(params[0]) if params else set()
            return _FakeCursor([(n,) for n in wanted - self.missing_objects])
        if norm.startswith("SELECT 1 FROM PG_TRIGGER") or                 norm.startswith("SELECT 1 FROM PG_CONSTRAINT"):
            # Order-independent on purpose: the trigger lookup binds
            # (table, name) and the constraint lookup binds (table, name) too,
            # so keying on params[0] silently matched the TABLE and every
            # object looked present. A fake that answers the wrong parameter is
            # worse than no fake -- it makes a refusal test pass while
            # exercising nothing.
            supplied = {str(p) for p in (params or ())}
            absent = bool(supplied & self.missing_objects)
            return _FakeCursor([] if absent else [(1,)])
        if norm.startswith("SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"):
            # params carry (table, column) in some order; look up by column.
            column = next((p for p in (params or ()) if str(p).endswith("_paise")), None)
            return _FakeCursor([(self.paise_types.get(column, "bigint"),)])
        if norm.startswith("INSERT INTO SCHEMA_MIGRATIONS"):
            version, name, checksum, duration_ms = params
            self.recorded.append((version, checksum))
            return _FakeCursor([])
        # Anything else is treated as replaying migration DDL against a
        # database that already has these objects -- exactly what a legacy
        # adopted schema produces.
        raise psycopg.errors.DuplicateTable("relation already exists")

    def commit(self):
        pass

    def rollback(self):
        pass


def test_def_01_pg_adopts_a_preexisting_legacy_schema():
    """migrate_pg.upgrade() is idempotent for an adopted legacy schema: when
    a migration's DDL fails because its tables already exist, and every one
    of those tables is genuinely present, the migration is recorded as
    satisfied instead of raising -- so `--upgrade` on a database that already
    carries the schema succeeds, rather than failing the way the SQLite
    runner did in the original DEF-01 reproduction."""
    from app.backend.pg import migrate_pg

    # Adopt EVERY discovered migration, not just the first.
    #
    # This test originally pinned itself to `discover()[0]` and seeded the fake
    # with only migration 001's tables. That passed while 001 was the only
    # migration and broke the moment 002 was merged: upgrade() reached 002,
    # the fake raised DuplicateTable as it does for any DDL, 002's tables were
    # absent from the seeded set, and adoption correctly refused -- so the test
    # failed for a reason that had nothing to do with the behaviour it asserts.
    #
    # A test of "adoption works" must not also encode "there is exactly one
    # migration", or it breaks on every future milestone. The realism of using
    # the real migration files is worth keeping; the coupling to how many there
    # are is not.
    migrations = migrate_pg.discover()
    assert migrations, "the real migration set must not be empty"

    all_tables: set[str] = set()
    for migration in migrations:
        # A migration may legitimately create NO table. 006 is RLS policies
        # plus one function; 007 replaces a function body and adds a CHECK
        # constraint. The per-migration `assert tables` that stood here
        # encoded "every migration creates a table" -- the same class of
        # coupling this test's own comment above warns against, one step
        # along. It was fixture seeding, not the behaviour under test.
        all_tables.update(migrate_pg._tables_created_by(migration))

    # The guard that actually matters is kept: if NOTHING declared a table the
    # fake connection below would be seeded empty and the adoption path would
    # never fire, so the test would pass while proving nothing.
    assert all_tables, (
        "no migration declares a table; the adoption fixture would be empty "
        "and this test would pass vacuously")

    con = _FakeAdoptConnection(existing_tables=all_tables)

    performed = migrate_pg.upgrade(con)

    assert performed == [f"{m.version} (adopted)" for m in migrations]
    assert con.recorded == [(m.version, m.checksum) for m in migrations]


def test_def_01_pg_refuses_a_genuine_conflict_rather_than_guessing():
    """The adoption path must not fire when only SOME of a migration's tables
    are present -- that is a genuine conflict (something else created part of
    the schema), not an adoptable legacy database, and papering over it would
    recreate DEF-01's failure mode in a new shape: an application repairing a
    database it does not actually understand."""
    from app.backend.pg import migrate_pg

    migration = migrate_pg.discover()[0]
    tables = migrate_pg._tables_created_by(migration)
    partial = set(tables[:1]) if len(tables) > 1 else set()

    con = _FakeAdoptConnection(existing_tables=partial)

    with pytest.raises(migrate_pg.MigrationError):
        migrate_pg.upgrade(con)

    assert con.recorded == []
