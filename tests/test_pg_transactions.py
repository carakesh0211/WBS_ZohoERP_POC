"""Transaction and row-locking guarantees, proved against a real server.

``migrations/pg/001_foundation.sql`` has no financial-document tables yet
(those arrive in 003+ once budget control lands), so these tests exercise row
locking and rollback against ``organisation`` - the earliest table with a
mutable, non-audit row. The mechanism under test (``SELECT ... FOR UPDATE``
serialising concurrent writers; ``ROLLBACK`` leaving no partial state) is
identical regardless of which table carries it.
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

import threading

import psycopg
import pytest

from conftest_pg import _replace_dbname

pytestmark = pytest.mark.pg


def _insert_organisation(con, org_id="ORG-LOCK", version_no=0):
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by, "
        "version_no) VALUES (%s, %s, 'Lock Test Org', 'test', 'test', %s)",
        (org_id, org_id, version_no))
    con.commit()


# ---------------------------------------------------------------- FOR UPDATE
def test_concurrent_transactions_on_the_same_row_serialise_under_for_update(
        pg_url, pg_disposable_db_name):
    """Two threads race to increment the same row's ``version_no``, each
    guarding its read-modify-write with ``SELECT ... FOR UPDATE``.

    If the locking were not serialising the writers, this is a textbook
    lost-update: both threads read the same starting value and one
    increment is silently overwritten. Asserting the exact final count is a
    stronger, more deterministic proof than asserting on wall-clock
    ordering.
    """
    dsn = _replace_dbname(pg_url, pg_disposable_db_name)
    org_id = "ORG-CONCURRENT"

    setup_con = psycopg.connect(dsn, autocommit=False)
    try:
        _insert_organisation(setup_con, org_id=org_id, version_no=0)
    finally:
        setup_con.close()

    iterations_per_thread = 25
    errors: list[BaseException] = []

    def _worker():
        con = psycopg.connect(dsn, autocommit=False)
        try:
            for _ in range(iterations_per_thread):
                current = con.execute(
                    "SELECT version_no FROM organisation WHERE organisation_id = %s "
                    "FOR UPDATE", (org_id,)).fetchone()[0]
                con.execute(
                    "UPDATE organisation SET version_no = %s WHERE organisation_id = %s",
                    (current + 1, org_id))
                con.commit()
        except BaseException as exc:  # surfaced on the main thread below
            errors.append(exc)
        finally:
            con.close()

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker thread(s) raised: {errors}"

    check_con = psycopg.connect(dsn, autocommit=False)
    try:
        final = check_con.execute(
            "SELECT version_no FROM organisation WHERE organisation_id = %s",
            (org_id,)).fetchone()[0]
    finally:
        check_con.close()

    assert final == 2 * iterations_per_thread, (
        f"expected {2 * iterations_per_thread} serialised increments, got {final} - "
        "a lower count means FOR UPDATE did not serialise the two writers "
        "(a lost update occurred)")


def test_for_update_blocks_a_concurrent_reader_of_the_same_row(pg_url, pg_disposable_db_name):
    """A second transaction's ``FOR UPDATE`` on the same row must block until
    the first transaction ends, not read past the lock."""
    dsn = _replace_dbname(pg_url, pg_disposable_db_name)
    org_id = "ORG-BLOCK"

    setup_con = psycopg.connect(dsn, autocommit=False)
    try:
        _insert_organisation(setup_con, org_id=org_id, version_no=0)
    finally:
        setup_con.close()

    holder = psycopg.connect(dsn, autocommit=False)
    waiter = psycopg.connect(dsn, autocommit=False)
    observed: dict[str, object] = {}
    waiter_acquired = threading.Event()

    try:
        holder.execute(
            "SELECT version_no FROM organisation WHERE organisation_id = %s FOR UPDATE",
            (org_id,))

        def _wait_for_lock():
            waiter.execute(
                "SELECT version_no FROM organisation WHERE organisation_id = %s FOR UPDATE",
                (org_id,))
            observed["value"] = waiter.execute(
                "SELECT 1").fetchone()  # trivial statement, proves the lock was granted
            waiter_acquired.set()

        t = threading.Thread(target=_wait_for_lock)
        t.start()

        # The waiter must NOT acquire the lock while the holder still has it.
        acquired_too_early = waiter_acquired.wait(timeout=1.0)
        assert not acquired_too_early, "second transaction acquired FOR UPDATE while the first still held it"

        holder.execute(
            "UPDATE organisation SET version_no = 1 WHERE organisation_id = %s", (org_id,))
        holder.commit()  # releases the lock

        t.join(timeout=10)
        assert waiter_acquired.is_set(), "second transaction never acquired the lock after the first committed"
    finally:
        waiter.rollback()
        holder.close()
        waiter.close()


# -------------------------------------------------------------------- rollback
def test_rollback_leaves_no_partial_state(pg_connection):
    """A transaction that inserts across two related tables and then rolls
    back must leave neither row behind."""
    con = pg_connection
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES ('ORG-ROLLBACK', 'ORG-ROLLBACK', 'Rollback Org', 'test', 'test')")
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
        "VALUES ('ENT-ROLLBACK', 'ORG-ROLLBACK', 'ENT-ROLLBACK', 'Rollback Entity', "
        "'test', 'test')")

    # Sanity: both rows are visible within the still-open transaction.
    assert con.execute(
        "SELECT 1 FROM organisation WHERE organisation_id = 'ORG-ROLLBACK'").fetchone()
    assert con.execute(
        "SELECT 1 FROM entity WHERE entity_id = 'ENT-ROLLBACK'").fetchone()

    con.rollback()

    assert con.execute(
        "SELECT 1 FROM organisation WHERE organisation_id = 'ORG-ROLLBACK'").fetchone() is None
    assert con.execute(
        "SELECT 1 FROM entity WHERE entity_id = 'ENT-ROLLBACK'").fetchone() is None


def test_rollback_after_a_constraint_violation_leaves_no_partial_state(pg_connection):
    """The common real-world shape: an early insert succeeds, a later one in
    the same transaction violates a constraint, and the caller rolls back
    rather than trying to salvage the transaction."""
    con = pg_connection
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES ('ORG-PARTIAL', 'ORG-PARTIAL', 'Partial Org', 'test', 'test')")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, "
            "updated_by) VALUES ('ENT-ORPHAN', 'ORG-DOES-NOT-EXIST', 'ENT-ORPHAN', "
            "'Orphan Entity', 'test', 'test')")
    con.rollback()

    assert con.execute(
        "SELECT 1 FROM organisation WHERE organisation_id = 'ORG-PARTIAL'").fetchone() is None
