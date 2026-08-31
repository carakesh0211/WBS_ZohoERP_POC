"""Tests for `app.backend.pg.audit` -- the hash-chained, append-only audit log.

Payload-format and hash-computation tests, and the wiring of `append()` and
`verify_chain()`, are pure logic against a `FakeSession` and run without a
database. A round trip against real PostgreSQL is gated the same way as
`tests/test_pg_locking.py` -- see that file's module docstring for why a
`skipif`-based `PG` stands in for the spec's `pytest.mark.pg` while
`pytest.ini` (which this agent does not own) has not registered it.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

import pytest

from app.backend.pg.audit import append, compute_entry_hash, verify_chain

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


class FakeSession:
    """Records calls; never touches a database.

    `fetchone_results` is consumed in order: `append()` calls `fetchone`
    twice per entry (the previous-seq lookup, then `INSERT ... RETURNING`).
    """

    def __init__(self, fetchone_results=None, fetchall_rows=None):
        self._fetchone_queue = list(fetchone_results or [])
        self._fetchall_rows = fetchall_rows if fetchall_rows is not None else []
        self.execute_calls: list[tuple[str, object]] = []
        self.fetchone_calls: list[tuple[str, object]] = []
        self.fetchall_calls: list[tuple[str, object]] = []
        self.call_order: list[str] = []

    def execute(self, statement, params=None):
        self.execute_calls.append((statement, params))
        self.call_order.append("execute")
        return None

    def fetchone(self, statement, params=None):
        self.fetchone_calls.append((statement, params))
        self.call_order.append("fetchone")
        if self._fetchone_queue:
            return self._fetchone_queue.pop(0)
        return None

    def fetchall(self, statement, params=None):
        self.fetchall_calls.append((statement, params))
        self.call_order.append("fetchall")
        return self._fetchall_rows


def _canned_insert_row(audit_id=1, stream_key="S", seq=1, at=None, actor="U",
                        action="A", object_type="T", object_id="1", detail="d",
                        correlation_id=None, entry_hash="deadbeef"):
    at = at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (audit_id, stream_key, seq, at, actor, action, object_type,
            object_id, detail, correlation_id, entry_hash)


# ============================================================== the frozen payload format
def test_compute_entry_hash_matches_the_frozen_pipe_format():
    """`prev|at|actor|action|type|id|detail`. This format is frozen
    permanently (domain-controls.md) -- changing it invalidates every hash
    already stored, including any imported legacy chain."""
    expected = hashlib.sha256(
        "prevhash|2026-01-01T00:00:00+00:00|actor1|ACTION_X|Type1|ID-1|some detail"
        .encode("utf-8")
    ).hexdigest()
    actual = compute_entry_hash("prevhash", "2026-01-01T00:00:00+00:00", "actor1",
                                 "ACTION_X", "Type1", "ID-1", "some detail")
    assert actual == expected


def test_compute_entry_hash_treats_none_prev_hash_as_empty_string():
    with_none = compute_entry_hash(None, "AT", "a", "b", "c", "d", "e")
    with_empty = compute_entry_hash("", "AT", "a", "b", "c", "d", "e")
    assert with_none == with_empty
    assert with_none == hashlib.sha256("|AT|a|b|c|d|e".encode("utf-8")).hexdigest()


def test_compute_entry_hash_is_sensitive_to_every_field():
    base = compute_entry_hash("p", "AT", "actor", "action", "type", "id", "detail")
    assert compute_entry_hash("p2", "AT", "actor", "action", "type", "id", "detail") != base
    assert compute_entry_hash("p", "AT2", "actor", "action", "type", "id", "detail") != base
    assert compute_entry_hash("p", "AT", "actor2", "action", "type", "id", "detail") != base
    assert compute_entry_hash("p", "AT", "actor", "action2", "type", "id", "detail") != base
    assert compute_entry_hash("p", "AT", "actor", "action", "type2", "id", "detail") != base
    assert compute_entry_hash("p", "AT", "actor", "action", "type", "id2", "detail") != base
    assert compute_entry_hash("p", "AT", "actor", "action", "type", "id", "detail2") != base


# ============================================================== append()
def test_append_takes_advisory_lock_before_reading_prev_hash():
    session = FakeSession(fetchone_results=[None, _canned_insert_row()])
    append(session, "U-1", "PR_CREATED", "PurchaseRequest", "PR-1", "detail",
           stream_key="S1")
    assert session.call_order[:3] == ["execute", "fetchone", "fetchone"], (
        "advisory_audit_lock (execute) must run before the prev_hash lookup "
        "(fetchone), which must run before the INSERT ... RETURNING "
        "(fetchone) -- otherwise two concurrent writers can both read the "
        "same prev_hash")
    lock_statement, lock_params = session.execute_calls[0]
    assert "pg_advisory_xact_lock" in lock_statement
    assert "hashtext" in lock_statement
    assert lock_params == ("S1",)


def test_append_default_stream_key_is_object_type_colon_object_id():
    session = FakeSession(fetchone_results=[None, _canned_insert_row()])
    append(session, "actor", "ACTION", "PurchaseOrder", "PO-9", "d")
    _statement, lock_params = session.execute_calls[0]
    assert lock_params == ("PurchaseOrder:PO-9",)


def test_append_first_entry_gets_seq_1_and_null_prev_hash():
    session = FakeSession(fetchone_results=[None, _canned_insert_row(seq=1)])
    append(session, "U-1", "X", "T", "1", "d", stream_key="S1")
    _statement, insert_params = session.fetchone_calls[1]
    assert insert_params["seq"] == 1
    assert insert_params["prev_hash"] is None


def test_append_next_entry_increments_seq_and_chains_prev_hash():
    session = FakeSession(
        fetchone_results=[(5, "priorhash123"), _canned_insert_row(seq=6)])
    append(session, "U-1", "X", "T", "1", "d", stream_key="S1")
    _statement, insert_params = session.fetchone_calls[1]
    assert insert_params["seq"] == 6
    assert insert_params["prev_hash"] == "priorhash123"


def test_append_computes_the_frozen_payload_hash_for_the_insert():
    session = FakeSession(fetchone_results=[None, _canned_insert_row(seq=1)])
    append(session, "actor1", "ACTION", "Type", "ID-1", "detail text",
           stream_key="S1")
    _statement, insert_params = session.fetchone_calls[1]
    at_iso = insert_params["at"].isoformat()
    expected = compute_entry_hash(None, at_iso, "actor1", "ACTION", "Type",
                                   "ID-1", "detail text")
    assert insert_params["entry_hash"] == expected


def test_append_returns_the_row_shaped_dict():
    canned = _canned_insert_row(audit_id=42, stream_key="S1", seq=1,
                                 entry_hash="abc123")
    session = FakeSession(fetchone_results=[None, canned])
    entry = append(session, "actor1", "ACTION", "Type", "ID-1", "detail",
                   stream_key="S1")
    assert entry["audit_id"] == 42
    assert entry["stream_key"] == "S1"
    assert entry["seq"] == 1
    assert entry["entry_hash"] == "abc123"
    assert isinstance(entry["at"], str)   # serialised, not a raw datetime


# ============================================================== verify_chain()
def test_verify_chain_reports_intact_for_a_valid_chain():
    at1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    h1 = compute_entry_hash(None, at1.isoformat(), "u1", "A1", "T", "1", "d1")
    h2 = compute_entry_hash(h1, at2.isoformat(), "u2", "A2", "T", "2", "d2")
    rows = [
        (1, at1, "u1", "A1", "T", "1", "d1", None, h1),
        (2, at2, "u2", "A2", "T", "2", "d2", h1, h2),
    ]
    session = FakeSession(fetchall_rows=rows)
    result = verify_chain(session, "S1")
    assert result == {"intact": True, "entries_checked": 2, "first_break_seq": None}


def test_verify_chain_empty_stream_is_trivially_intact():
    session = FakeSession(fetchall_rows=[])
    result = verify_chain(session, "S-empty")
    assert result == {"intact": True, "entries_checked": 0, "first_break_seq": None}


def test_verify_chain_detects_a_tampered_entry_hash():
    at1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    h1 = compute_entry_hash(None, at1.isoformat(), "u1", "A1", "T", "1", "d1")
    rows = [(1, at1, "u1", "A1", "T", "1", "TAMPERED-DETAIL", None, h1)]
    session = FakeSession(fetchall_rows=rows)
    result = verify_chain(session, "S1")
    assert result["intact"] is False
    assert result["first_break_seq"] == 1
    assert result["entries_checked"] == 1


def test_verify_chain_detects_broken_linkage_even_when_each_hash_is_locally_consistent():
    """A row can be internally self-consistent (its `entry_hash` really does
    match its own recorded `prev_hash`) while still not chaining from the
    entry before it -- e.g. a deleted-and-reinserted row. That must still be
    caught."""
    at1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    h1 = compute_entry_hash(None, at1.isoformat(), "u1", "A1", "T", "1", "d1")
    wrong_prev = "not-actually-h1"
    h2 = compute_entry_hash(wrong_prev, at2.isoformat(), "u2", "A2", "T", "2", "d2")
    rows = [
        (1, at1, "u1", "A1", "T", "1", "d1", None, h1),
        (2, at2, "u2", "A2", "T", "2", "d2", wrong_prev, h2),
    ]
    session = FakeSession(fetchall_rows=rows)
    result = verify_chain(session, "S1")
    assert result["intact"] is False
    assert result["first_break_seq"] == 2
    assert result["entries_checked"] == 2


def test_verify_chain_reports_only_the_first_break_but_still_scans_everything():
    at1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    at3 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    h1 = compute_entry_hash(None, at1.isoformat(), "u1", "A1", "T", "1", "d1")
    # Row 2 is corrupted -- its stored entry_hash does not match a recompute.
    bogus_h2 = "0" * 64
    # Row 3 chains from row 2's stored (bogus) hash, and is itself internally
    # consistent, so it does not introduce a *second* break on its own.
    h3 = compute_entry_hash(bogus_h2, at3.isoformat(), "u3", "A3", "T", "3", "d3")
    rows = [
        (1, at1, "u1", "A1", "T", "1", "d1", None, h1),
        (2, at2, "u2", "A2", "T", "2", "d2", h1, bogus_h2),
        (3, at3, "u3", "A3", "T", "3", "d3", bogus_h2, h3),
    ]
    session = FakeSession(fetchall_rows=rows)
    result = verify_chain(session, "S1")
    assert result["intact"] is False
    assert result["first_break_seq"] == 2
    assert result["entries_checked"] == 3


# ============================================================== live database
@PG
def test_append_and_verify_chain_round_trip_against_postgres(pg_database, pg_scope):
    """`audit_log` is append-only, no FKs, and needs no fixture data beyond
    itself, so this round trip is self-contained: two real `append()` calls
    followed by a real `verify_chain()`, against actual PostgreSQL."""
    import uuid

    stream_key = f"IntegrationTest:{uuid.uuid4().hex}"
    with pg_database.session(pg_scope) as session:
        first = append(session, "tester", "CREATED", "IntegrationTest", "1",
                        "first entry", stream_key=stream_key)
        second = append(session, "tester", "UPDATED", "IntegrationTest", "1",
                         "second entry", stream_key=stream_key)

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert second["audit_id"] != first["audit_id"]

    with pg_database.session(pg_scope) as session:
        result = verify_chain(session, stream_key)
    assert result == {"intact": True, "entries_checked": 2, "first_break_seq": None}
