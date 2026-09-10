"""The migration tooling against a LIVE PostgreSQL. Skipped without CAPEX_DB_URL.

A SKIP IS NOT A PASS
====================
There is no PostgreSQL and no Docker on the workstation this file was written
on, so **every test here has been executed exactly zero times as of this
commit.** They first run in CI's ``pg_tests`` job. Nothing in the migration
runbook may be read as "verified" on the strength of this file until that job
has been green with these tests in it.

This file is named ``test_pg_*`` on purpose: CI's ``pg_tests`` job collects
``tests/test_pg_*.py`` and nothing else, and its evidence gate counts executed
tests out of that job's JUnit report. A PostgreSQL-gated file named any other
way skips on every workstation AND is never collected by the one job that could
run it -- which is not a skip, it is invisibility.

The database-free half is ``tests/test_migration_import.py``, which executes the
same import path against a disposable SQLite target and proves the tool's
BEHAVIOUR -- order, rollback, resume, reconciliation to the paisa. What only a
server can answer is here:

* that the depth-aware DDL scanner agrees with ``information_schema``, so the
  preflight is reading the real schema rather than a second one that has
  drifted;
* that the generated ``INSERT ... ON CONFLICT`` is valid PostgreSQL, which
  SQLite's dialect being close is not evidence of;
* that ``SUM(bigint)`` really does come back as a ``Decimal`` without the
  ``::bigint`` cast -- the reconciliation refuses a Decimal, so without that
  cast every target total would be refused, and the cast would otherwise be a
  belief;
* that ``audit_log``'s identity column really does reject a written
  ``audit_id``, which is why the conflict target is ``(stream_key, seq)``;
* that a LEGACY import verifies through the product's own ``verify_chain``
  against rows a server actually stored, including the ``timestamptz``
  round trip that the database-free tests can only simulate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg is
# deliberately not auto-discovered, so its fixtures must be imported by name into
# this module's namespace. Same note as tests/test_pg_reporting.py.
from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

from app.backend.pg.engine import Scope                              # noqa: E402
from tools.migration import audit_chain, import_pg, reconcile        # noqa: E402
from tools.migration.schema import (compare_scanned_to_live,          # noqa: E402
                                    live_pg_columns, scan_pg_schema)

pytestmark = pytest.mark.pg

SKIP_REASON = (
    "requires a live PostgreSQL (CAPEX_DB_URL). This test has never executed on "
    "the workstation this file was written on; it first runs in CI's pg_tests "
    "job. A skip is not a pass."
)


def test_the_ddl_scanner_agrees_with_information_schema(pg_connection):
    """The preflight reads ``migrations/pg/*.sql``. This checks that reading.

    A scanner nobody compares against a server is a second schema that drifts,
    and a preflight built on a drifted schema reports confidently about columns
    that are not there.
    """
    problems = compare_scanned_to_live(scan_pg_schema(), live_pg_columns(pg_connection))
    assert problems == {}, problems


def test_the_generated_insert_is_valid_postgresql(pg_connection):
    """SQLite accepts ``ON CONFLICT (...) DO NOTHING`` too. That is not evidence
    that the statement this tool emits parses on PostgreSQL."""
    sql = import_pg.insert_statement(
        "entity", ["entity_id", "name"], ["entity_id"])
    cur = pg_connection.cursor()
    cur.execute(f"PREPARE migration_probe AS {sql.replace('%(entity_id)s', '$1').replace('%(name)s', '$2')}")
    cur.execute("DEALLOCATE migration_probe")


def test_sum_of_bigint_returns_a_decimal_without_the_cast(pg_connection):
    """The reason ``totals_from_sql`` casts. Without it every target money total
    arrives as a ``Decimal`` and the reconciliation refuses it -- correctly, but
    for a reason that would look like a bug in the reconciliation."""
    from decimal import Decimal
    cur = pg_connection.cursor()
    cur.execute("SELECT SUM(v) FROM (VALUES (1::bigint), (2::bigint)) AS t(v)")
    assert isinstance(cur.fetchone()[0], Decimal)
    cur.execute("SELECT SUM(v)::bigint FROM (VALUES (1::bigint), (2::bigint)) AS t(v)")
    assert isinstance(cur.fetchone()[0], int)


def test_reconciliation_totals_from_a_real_server_are_integers(pg_connection):
    tables = scan_pg_schema()
    def query(sql, params):
        cur = pg_connection.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    totals = reconcile.totals_from_sql(
        "budget_line", tables["budget_line"].column_names, query)
    assert isinstance(totals.rows, int)
    for value in totals.totals.values():
        assert type(value) is int


def test_audit_log_refuses_a_written_identity_value(pg_connection):
    """Why the conflict target is ``(stream_key, seq)`` and not ``audit_id``."""
    import psycopg
    cur = pg_connection.cursor()
    with pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO audit_log (audit_id, stream_key, seq, actor, action, "
            "object_type, object_id) VALUES (1, 'X', 1, 'a', 'b', 'c', 'd')")


def test_a_legacy_import_verifies_through_the_products_own_verifier(pg_database):
    """The end-to-end proof: plan, insert, then ``verify_chain('LEGACY')``.

    Uses the product's verifier, never a copy. The ``at`` values make the round
    trip through a real ``timestamptz`` column, which is the one step the
    database-free tests can only simulate.
    """
    import hashlib
    rows, prev = [], ""
    for i in range(4):
        at = f"2026-08-06T09:0{i}:00+00:00"
        payload = f"{prev}|{at}|U-{i}|APPROVE|PurchaseRequest|PR-{i}|detail {i}"
        entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        rows.append({"audit_id": i + 1, "at": at, "actor": f"U-{i}",
                     "action": "APPROVE", "object_type": "PurchaseRequest",
                     "object_id": f"PR-{i}", "detail": f"detail {i}",
                     "prev_hash": prev, "entry_hash": entry_hash})
        prev = entry_hash

    entries, report = audit_chain.plan_legacy_import(rows)
    assert report["legacy_entries"] == 4

    with pg_database.session(Scope.system()) as session:
        for entry in entries:
            session.execute(
                "INSERT INTO audit_log (stream_key, seq, at, actor, action, "
                "object_type, object_id, detail, correlation_id, prev_hash, "
                "entry_hash) VALUES (%(stream_key)s, %(seq)s, %(at)s, %(actor)s, "
                "%(action)s, %(object_type)s, %(object_id)s, %(detail)s, "
                "%(correlation_id)s, %(prev_hash)s, %(entry_hash)s)",
                entry.as_params())
        result = audit_chain.assert_chain_intact(session, audit_chain.LEGACY_STREAM)
    assert result["intact"] is True
    assert result["entries_checked"] == 4


def test_a_tampered_legacy_row_aborts_the_import(pg_database):
    """The gate that matters: a chain that does not verify stops the migration."""
    import hashlib
    rows, prev = [], ""
    for i in range(3):
        at = f"2026-08-07T09:0{i}:00+00:00"
        payload = f"{prev}|{at}|U-{i}|APPROVE|PR|PR-{i}|d{i}"
        entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        rows.append({"audit_id": i + 1, "at": at, "actor": f"U-{i}", "action": "APPROVE",
                     "object_type": "PR", "object_id": f"PR-{i}", "detail": f"d{i}",
                     "prev_hash": prev, "entry_hash": entry_hash})
        prev = entry_hash
    rows[1]["detail"] = "quietly edited"          # hash NOT recomputed -- the point

    entries, _ = audit_chain.plan_legacy_import(rows)
    with pytest.raises(audit_chain.AuditChainError, match="intact=False"):
        with pg_database.session(Scope.system()) as session:
            for entry in entries:
                session.execute(
                    "INSERT INTO audit_log (stream_key, seq, at, actor, action, "
                    "object_type, object_id, detail, correlation_id, prev_hash, "
                    "entry_hash) VALUES (%(stream_key)s, %(seq)s, %(at)s, "
                    "%(actor)s, %(action)s, %(object_type)s, %(object_id)s, "
                    "%(detail)s, %(correlation_id)s, %(prev_hash)s, %(entry_hash)s)",
                    entry.as_params())
            audit_chain.assert_chain_intact(session, audit_chain.LEGACY_STREAM)


def test_the_skip_reason_says_a_skip_is_not_a_pass():
    """Guards the honesty of this file's own gating.

    Every other test here is invisible on a workstation. If the skip reason is
    ever softened into something a reader could mistake for a pass, this fails
    -- and this one runs everywhere.
    """
    assert "A skip is not a pass." in SKIP_REASON
    assert "never executed" in __doc__ or "zero times" in __doc__
