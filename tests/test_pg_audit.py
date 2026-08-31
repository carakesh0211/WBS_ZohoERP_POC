"""Tests for `app.backend.pg.audit` -- the hash-chained, append-only audit log.

Payload-format and hash-computation tests, and the wiring of `append()` and
`verify_chain()`, are pure logic against a `FakeSession` and run without a
database. A round trip against real PostgreSQL is gated the same way as
`tests/test_pg_locking.py` -- see that file's module docstring for why a
`skipif`-based `PG` stands in for the spec's `pytest.mark.pg` while
`pytest.ini` (which this agent does not own) has not registered it.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# Without this the live tests below fail at SETUP with "fixture 'pg_database'
# not found" -- but ONLY where CAPEX_DB_URL is set. Locally they skip, so the
# missing fixture is never resolved and the gap is invisible. CI is the first
# place these run for real, which is the whole point of that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

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


def _chain_rows(n: int):
    """`n` correctly-linked audit rows, in verify_chain's SELECT column order:
    (seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash).
    """
    rows, prev = [], None
    for i in range(1, n + 1):
        at = datetime(2026, 1, i, tzinfo=timezone.utc)
        h = compute_entry_hash(prev, at.isoformat(), f"u{i}", f"A{i}", "T", str(i), f"d{i}")
        rows.append((i, at, f"u{i}", f"A{i}", "T", str(i), f"d{i}", prev, h))
        prev = h
    return rows


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
    assert result["intact"] is True
    assert result["entries_checked"] == 2
    assert result["first_break_seq"] is None
    assert result["head_seq"] == 2
    assert result["sequence_contiguous"] is True


def test_verify_chain_does_NOT_report_an_unknown_stream_as_intact():
    """INVERTED 2026-08-31 after adversarial review. See tests/ADAPTATIONS.md.

    This asserted that an empty stream is "trivially intact". It is not: an
    empty result means either an unknown stream_key or a wholly deleted one,
    and reporting either as intact turns a typo in the nightly verification job
    into a green tick. `stream_found` distinguishes them.
    """
    session = FakeSession(fetchall_rows=[])
    result = verify_chain(session, "S-empty")
    assert result["intact"] is False
    assert result["stream_found"] is False
    assert result["entries_checked"] == 0


def test_tail_truncation_is_NOT_detectable_from_the_stream_alone():
    """Documents a REAL LIMIT rather than asserting a capability we lack.

    Deleting the LAST k entries leaves a perfectly linked, perfectly
    contiguous prefix: 1,2 after removing 3,4 is indistinguishable from a
    stream that only ever had two entries. Nothing inside the stream can
    reveal it -- not the link walk, not seq contiguity.

    Detecting it requires an external record of the head, which is exactly what
    the daily anchors in `audit_anchor` are for. That table exists with no
    writer and no reader, so this guarantee is NOT yet in place, and
    `verify_chain` says so in `whole_stream_truncation_note` rather than
    letting `intact: true` imply more than it can support.

    This test exists so the limit is recorded and cannot be quietly forgotten
    once the anchor writer lands -- at which point it should be replaced by one
    that asserts detection.
    """
    full = verify_chain(FakeSession(fetchall_rows=_chain_rows(4)), "S-1")
    assert full["intact"] is True and full["head_seq"] == 4

    truncated = verify_chain(FakeSession(fetchall_rows=_chain_rows(4)[:2]), "S-1")
    assert truncated["intact"] is True, (
        "documenting the current limit: a truncated tail still reports intact"
    )
    assert truncated["head_seq"] == 2, (
        "head_seq is exposed precisely so an external anchor can catch this"
    )
    assert "anchors" in truncated["whole_stream_truncation_note"], (
        "the result must state the limit rather than implying full coverage"
    )


def test_verify_chain_detects_a_gap_in_the_middle():
    """A missing interior seq must be reported even if hashes were recomputed."""
    rows = _chain_rows(4)
    rows = [r for r in rows if r[0] != 3]   # remove seq 3
    result = verify_chain(FakeSession(fetchall_rows=rows), "S-1")
    assert result["intact"] is False
    assert result["sequence_contiguous"] is False


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
    # Field-by-field, not an exact dict. verify_chain grew head_seq,
    # sequence_contiguous, stream_found and a truncation note; an exact-equality
    # assertion turns every added piece of EVIDENCE into a failure, which
    # pressures the next person to drop the evidence rather than the assertion.
    assert result["intact"] is True
    assert result["entries_checked"] == 2
    assert result["first_break_seq"] is None
    assert result["stream_found"] is True
    assert result["sequence_contiguous"] is True


# ---------------------------------------------------------------------------
# Lead-added regression: the chain must not depend on session TimeZone.
# ---------------------------------------------------------------------------
def test_chain_hash_is_independent_of_connection_timezone():
    """A timestamptz is rendered in the CONNECTION's TimeZone.

    An appending connection running UTC writes `...+00:00`; a verifying
    connection running Asia/Kolkata reads the SAME INSTANT back as `...+05:30`.
    Hashing the rendered string would then recompute a different digest and
    report a perfectly intact chain as BROKEN -- a false tamper alarm on the
    guarantee this product exists to make.

    canonical_at() normalises to UTC on both paths, so the hash is a function
    of the instant rather than of whoever happens to be connected.
    """
    from datetime import datetime, timedelta, timezone
    from app.backend.pg import audit

    instant_utc = datetime(2026, 8, 31, 12, 34, 56, 789012, tzinfo=timezone.utc)
    same_instant_ist = instant_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))

    assert instant_utc == same_instant_ist, "precondition: the same instant"
    assert instant_utc.isoformat() != same_instant_ist.isoformat(), (
        "precondition: naive isoformat() renders them differently"
    )

    assert audit.canonical_at(instant_utc) == audit.canonical_at(same_instant_ist), (
        "canonical_at must erase the rendering difference"
    )

    # prev_hash, then actor, action, object_type, object_id, detail
    prev_hash = "prevhash"
    rest = ("u-approver", "APPROVE", "PO", "PO-1", "amount 500000 paise")
    assert audit.compute_entry_hash(prev_hash, audit.canonical_at(instant_utc), *rest) == \
           audit.compute_entry_hash(prev_hash, audit.canonical_at(same_instant_ist), *rest), (
        "the same instant must produce the same entry hash from any timezone"
    )


def test_canonical_at_treats_a_naive_timestamp_as_utc():
    """A driver configured without tzinfo must not silently shift the instant."""
    from datetime import datetime, timezone
    from app.backend.pg import audit

    naive = datetime(2026, 8, 31, 12, 34, 56, 789012)
    aware = naive.replace(tzinfo=timezone.utc)
    assert audit.canonical_at(naive) == audit.canonical_at(aware)


def test_different_instants_still_produce_different_hashes():
    """Canonicalisation must not flatten genuinely different timestamps."""
    from datetime import datetime, timezone
    from app.backend.pg import audit

    a = datetime(2026, 8, 31, 12, 34, 56, 789012, tzinfo=timezone.utc)
    b = datetime(2026, 8, 31, 12, 34, 56, 789013, tzinfo=timezone.utc)
    assert audit.canonical_at(a) != audit.canonical_at(b)
