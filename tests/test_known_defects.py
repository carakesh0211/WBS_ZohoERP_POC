"""Defects found but deliberately NOT fixed in this phase.

Each test asserts the DESIRED behaviour and is marked xfail(strict=True). So:

  * while the defect exists  -> xfail, the suite stays green, the defect is on record
  * once someone fixes it    -> XPASS, which strict=True turns into a FAILURE,
                                forcing the marker to be removed deliberately

That is the opposite of a skip. A skipped test rots; this one tells you the
moment the behaviour changes, in either direction.

Findings are also recorded in docs/PHASE_0A_FINDINGS.md with a reproduction.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ===========================================================================
# DEF-01  Legacy database cannot be upgraded, and run.py boots through upgrade
# ===========================================================================
@pytest.mark.product_defect
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEF-01: migrate.upgrade() replays migration 001 against a database that "
        "already carries the v1 schema, raising 'table entity already exists'. "
        "app/run.py calls upgrade() unconditionally whenever the database file "
        "exists, so a pre-migration-runner database makes the application fail "
        "to boot. Scheduled for Phase 1 with the PostgreSQL migration work."
    ),
)
def test_def_01_a_legacy_v1_database_can_be_upgraded(tmp_path):
    """A database created before the migration runner existed must be adoptable.

    Reproduction
    ------------
    1. Build the v1 schema the way the pre-runner code did: executescript(SCHEMA).
       The result has the business tables and NO schema_migration ledger.
    2. Call migrate.upgrade() - which is exactly what app/run.py line 32 does on
       every boot when the database file already exists.

    Expected: the runner recognises the existing schema, records 001 as already
    applied (a baseline/adopt step), and proceeds to 002.

    Actual: sqlite3.OperationalError: table entity already exists.

    Why this matters beyond the POC
    -------------------------------
    Commit ce7f3c5 "initialize current schema on hosted startup" addressed a
    hosted instance with NO database. It does not cover a hosted instance with an
    OLD one. Any deployment carrying a pre-runner database file is bricked on
    restart, and the failure surfaces at boot rather than as a handled error.

    The Phase 1 fix is a baseline/adopt step: when the ledger is empty but the
    schema is present, verify the schema matches migration 001 and record it as
    applied rather than replaying it.
    """
    from app.backend import db as dbmod
    from app.backend import migrate

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(dbmod.SCHEMA)
    con.commit()
    con.close()

    migrate.upgrade(str(legacy), backup=False)

    con = sqlite3.connect(legacy)
    try:
        applied = {r[0] for r in con.execute("SELECT version FROM schema_migration")}
    finally:
        con.close()
    assert applied == {"001", "002"}, (
        "The runner should adopt the existing schema as 001 and then apply 002."
    )


def test_def_01_control_a_fresh_database_upgrades_cleanly(tmp_path):
    """The control for DEF-01. This passes today and must keep passing.

    It establishes that the defect is specific to ADOPTING an existing schema,
    not a general fault in the runner - which is what makes the Phase 1 fix a
    narrow baseline step rather than a rewrite.
    """
    from app.backend import migrate

    fresh = tmp_path / "fresh.db"
    migrate.fresh(str(fresh), seed=True)
    migrate.upgrade(str(fresh), backup=False)  # idempotent second pass

    con = sqlite3.connect(fresh)
    try:
        applied = {r[0] for r in con.execute("SELECT version FROM schema_migration")}
    finally:
        con.close()
    assert applied == {"001", "002"}
