"""Declarative constraints from migrations/pg/001_foundation.sql, proved live.

A constraint that only exists in a comment is not a constraint. These tests
insert directly against a fully migrated disposable database and assert the
server itself refuses the bad write - never the application layer.
"""
from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.pg


def _make_org_and_entity(con, *, org_id="ORG-01", entity_id="ENT-01"):
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, 'test', 'test')",
        (org_id, org_id, "Test Organisation"))
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'test', 'test')",
        (entity_id, org_id, entity_id, "Test Entity"))


# --------------------------------------------------------- accounting_period gist
def test_overlapping_accounting_periods_for_the_same_entity_are_rejected(pg_connection):
    con = pg_connection
    _make_org_and_entity(con)
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "created_by) VALUES ('P-Q1', 'ENT-01', '2026-01-01', '2026-03-31', 'test')")
    con.commit()

    with pytest.raises(psycopg.errors.ExclusionViolation):
        con.execute(
            "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
            "created_by) VALUES ('P-Q1-OVERLAP', 'ENT-01', '2026-03-01', '2026-06-30', 'test')")
    con.rollback()


def test_adjacent_non_overlapping_accounting_periods_are_accepted(pg_connection):
    """A negative control: the exclusion constraint must not be so broad that
    it rejects legitimate back-to-back quarters."""
    con = pg_connection
    _make_org_and_entity(con)
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "created_by) VALUES ('P-Q1', 'ENT-01', '2026-01-01', '2026-03-31', 'test')")
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "created_by) VALUES ('P-Q2', 'ENT-01', '2026-04-01', '2026-06-30', 'test')")
    con.commit()

    rows = con.execute(
        "SELECT period_id FROM accounting_period WHERE entity_id = 'ENT-01' "
        "ORDER BY period_start").fetchall()
    assert [r[0] for r in rows] == ["P-Q1", "P-Q2"]


def test_the_same_date_range_is_permitted_for_a_different_entity(pg_connection):
    """The exclusion is scoped per entity_id - it must not reject a
    perfectly ordinary overlap between two different entities' calendars."""
    con = pg_connection
    _make_org_and_entity(con, org_id="ORG-01", entity_id="ENT-01")
    _make_org_and_entity(con, org_id="ORG-02", entity_id="ENT-02")
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "created_by) VALUES ('P-A', 'ENT-01', '2026-01-01', '2026-03-31', 'test')")
    con.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
        "created_by) VALUES ('P-B', 'ENT-02', '2026-01-01', '2026-03-31', 'test')")
    con.commit()  # must not raise


def test_period_end_before_period_start_is_rejected(pg_connection):
    con = pg_connection
    _make_org_and_entity(con)
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute(
            "INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
            "created_by) VALUES ('P-BAD', 'ENT-01', '2026-03-31', '2026-01-01', 'test')")
    con.rollback()


# ------------------------------------------------------------------- audit_log
def _insert_audit_row(con, *, stream_key="STREAM-01", seq=1):
    con.execute(
        "INSERT INTO audit_log (stream_key, seq, actor, action, object_type, object_id) "
        "VALUES (%s, %s, 'U-TEST', 'CREATE', 'test_object', 'OBJ-01') RETURNING audit_id",
        (stream_key, seq))
    row = con.execute(
        "SELECT audit_id FROM audit_log WHERE stream_key = %s AND seq = %s",
        (stream_key, seq)).fetchone()
    con.commit()
    return row[0]


def test_audit_log_rejects_update(pg_connection):
    con = pg_connection
    audit_id = _insert_audit_row(con)

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
        con.execute("UPDATE audit_log SET detail = 'tampered' WHERE audit_id = %s", (audit_id,))
    assert "append-only" in str(exc_info.value)
    con.rollback()

    detail = con.execute(
        "SELECT detail FROM audit_log WHERE audit_id = %s", (audit_id,)).fetchone()[0]
    assert detail == "", "the rejected UPDATE must not have partially applied"


def test_audit_log_rejects_delete(pg_connection):
    con = pg_connection
    audit_id = _insert_audit_row(con)

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
        con.execute("DELETE FROM audit_log WHERE audit_id = %s", (audit_id,))
    assert "append-only" in str(exc_info.value)
    con.rollback()

    still_there = con.execute(
        "SELECT 1 FROM audit_log WHERE audit_id = %s", (audit_id,)).fetchone()
    assert still_there is not None, "the rejected DELETE must not have removed the row"


def test_audit_anchor_rejects_update_and_delete(pg_connection):
    con = pg_connection
    con.execute(
        "INSERT INTO audit_anchor (anchor_date, stream_heads, anchor_hash) "
        "VALUES ('2026-08-06', '{}'::jsonb, 'deadbeef')")
    con.commit()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        con.execute(
            "UPDATE audit_anchor SET anchor_hash = 'tampered' WHERE anchor_date = '2026-08-06'")
    con.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        con.execute("DELETE FROM audit_anchor WHERE anchor_date = '2026-08-06'")
    con.rollback()

    row = con.execute(
        "SELECT anchor_hash FROM audit_anchor WHERE anchor_date = '2026-08-06'").fetchone()
    assert row[0] == "deadbeef"


def test_audit_log_stream_seq_must_be_unique(pg_connection):
    con = pg_connection
    _insert_audit_row(con, stream_key="STREAM-DUP", seq=1)

    with pytest.raises(psycopg.errors.UniqueViolation):
        con.execute(
            "INSERT INTO audit_log (stream_key, seq, actor, action, object_type, object_id) "
            "VALUES ('STREAM-DUP', 1, 'U-TEST', 'CREATE', 'test_object', 'OBJ-02')")
    con.rollback()
