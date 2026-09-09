"""The audit anchor: the only record that can catch a deleted audit stream.

WHAT WAS MISSING, AND WHY IT MATTERED
-------------------------------------
``migrations/pg/001_foundation.sql`` created ``audit_anchor``, made it
append-only by trigger, and revoked UPDATE/DELETE on it from ``capex_app``.
Three protections around a table nothing ever wrote a row into. The gap was
recorded honestly in the product itself -- ``pg/audit.verify_chain`` returned
the sentence "Deletion of an entire stream is detectable only against the
daily anchors, which are not yet written", and
``tests/test_pg_audit.py::test_tail_truncation_is_NOT_detectable_from_the_stream_alone``
pins that limitation as behaviour -- but a recorded gap is still a gap.

A hash chain is a statement about the rows you are looking at. Deleting the
last k entries of a stream leaves a perfectly linked, perfectly contiguous
prefix; deleting the whole stream leaves nothing to disagree with. Wave 8 adds
``write_anchor`` and ``verify_anchors``, and this module is the proof that
they close exactly those two cases and do not merely say they do.

WHAT WOULD FAIL IF THE CONTROL WERE REMOVED
-------------------------------------------
Every test here is written so that deleting the control makes it red:

* delete the anchor chain check in ``verify_anchors`` and
  ``test_a_forged_anchor_breaks_the_anchor_chain`` /
  ``test_an_anchor_edited_to_hide_a_deletion_is_caught`` go red;
* delete the missing-stream comparison and
  ``test_a_wholly_deleted_stream_is_reported_missing`` goes red;
* delete the head-seq comparison and ``test_a_truncated_tail_is_reported``
  goes red;
* return ``intact=True`` when no anchor exists and
  ``test_a_database_that_was_never_anchored_is_not_reported_intact`` goes red;
* weaken the frozen anchor payload and
  ``test_the_anchor_payload_is_the_frozen_format`` goes red.

WHICH OF THESE HAVE ACTUALLY RUN
--------------------------------
The ``FakeSession`` tests run everywhere, including on a machine with no
PostgreSQL, and were executed before this file was committed. The ``@PG``
tests have NEVER EXECUTED on the authoring machine -- there is no PostgreSQL
here -- and first run in CI's ``pg_tests`` job. Every seed value they use is
derived from the ``CREATE TABLE`` in ``migrations/pg/001_foundation.sql`` and
the constraints in ``migrations/pg/024_audit_anchor_integrity.sql``, column by
column, including the CHECKs and not only the NOT NULLs.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_audit.py's module docstring for why this block is repeated
# rather than referenced.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import hashlib                                                   # noqa: E402
import json                                                      # noqa: E402
import os                                                        # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date                                        # noqa: E402

import pytest                                                    # noqa: E402

from app.backend.pg import audit as audit_mod                    # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ======================================================================
# A session double that models the two tables, so the LOGIC is provable
# without a database
# ======================================================================
class FakeSession:
    """Enough of `Session` to run `write_anchor` and `verify_anchors`.

    Dispatches on distinctive fragments of each statement rather than parsing
    SQL. That coupling is deliberate and is itself asserted
    (``test_the_fake_dispatches_every_statement_the_module_issues``): if the
    module starts issuing a statement this double does not recognise, the
    double raises instead of silently returning ``None`` and turning a real
    behaviour into a vacuous pass.
    """

    def __init__(self, entries=None, anchors=None):
        #: {stream_key: [(seq, entry_hash), ...]} in seq order
        self.entries = {k: list(v) for k, v in (entries or {}).items()}
        #: [(date, stream_heads dict, prev_anchor_hash, anchor_hash)]
        self.anchors = list(anchors or [])
        self.locks: list[str] = []
        self.unrecognised: list[str] = []

    # -- helpers ------------------------------------------------------
    def _heads(self):
        return [
            (stream_key, rows[-1][0], len(rows), rows[-1][1])
            for stream_key, rows in sorted(self.entries.items()) if rows
        ]

    def _dispatch(self, statement, params):
        if "pg_advisory_xact_lock" in statement:
            self.locks.append(params[0] if params else None)
            return "execute"
        if "INSERT INTO audit_anchor" in statement:
            heads = params["stream_heads"]
            self.anchors.append((params["anchor_date"],
                                 getattr(heads, "obj", heads),
                                 params["prev_anchor_hash"],
                                 params["anchor_hash"]))
            self.anchors.sort(key=lambda a: a[0])
            return "execute"
        if "FROM audit_anchor WHERE anchor_date = " in statement:
            return next((a for a in self.anchors if a[0] == params[0]), None)
        if "FROM audit_anchor WHERE anchor_date < " in statement:
            earlier = [a for a in self.anchors if a[0] < params[0]]
            return (earlier[-1][3],) if earlier else None
        if "FROM audit_anchor ORDER BY anchor_date" in statement:
            return list(self.anchors)
        if "GROUP BY stream_key" in statement:
            return self._heads()
        if "FROM audit_log WHERE stream_key = %s AND seq = %s" in statement:
            rows = self.entries.get(params[0], [])
            return next(((h,) for seq, h in rows if seq == params[1]), None)
        self.unrecognised.append(statement)
        raise AssertionError(
            "FakeSession received a statement it does not model:\n" + statement)

    def execute(self, statement, params=None):
        return self._dispatch(statement, params)

    def fetchone(self, statement, params=None):
        result = self._dispatch(statement, params)
        return None if result == "execute" else result

    def fetchall(self, statement, params=None):
        result = self._dispatch(statement, params)
        return [] if result == "execute" else result


def _session_with_two_streams():
    return FakeSession(entries={
        "PROJECT:PRJ-1": [(1, "h1a"), (2, "h1b"), (3, "h1c")],
        "PROJECT:PRJ-2": [(1, "h2a"), (2, "h2b")],
    })


# ======================================================================
# The frozen anchor payload
# ======================================================================
def test_the_anchor_payload_is_the_frozen_format():
    """`prev_anchor_hash|anchor_date|canonical_json(stream_heads)`.

    Frozen for the same reason the entry payload is frozen: an anchor nobody
    can recompute proves nothing, and every anchor already written was
    computed this way. Asserted against an independently written digest, not
    against the function calling itself.
    """
    heads = {"B:2": {"seq": 1, "entries": 1, "entry_hash": "b"},
             "A:1": {"seq": 4, "entries": 4, "entry_hash": "a"}}
    expected = hashlib.sha256(
        ("prevanchor|2026-09-09|"
         '{"A:1":{"entries":4,"entry_hash":"a","seq":4},'
         '"B:2":{"entries":1,"entry_hash":"b","seq":1}}').encode("utf-8")
    ).hexdigest()

    assert audit_mod.compute_anchor_hash("prevanchor", "2026-09-09", heads) == expected


def test_the_anchor_hash_does_not_depend_on_dict_ordering():
    """The heads come back out of jsonb in whatever order the server likes.

    If the digest depended on that ordering, verification would report a
    forged anchor every time psycopg happened to hand the keys back
    differently -- a false alarm on the one control that exists to raise a
    true one.
    """
    a = {"S:1": {"seq": 1, "entries": 1, "entry_hash": "x"},
         "S:2": {"seq": 2, "entries": 2, "entry_hash": "y"}}
    b = {"S:2": {"seq": 2, "entries": 2, "entry_hash": "y"},
         "S:1": {"seq": 1, "entries": 1, "entry_hash": "x"}}

    assert list(a) != list(b), "precondition: the two dicts are ordered differently"
    assert audit_mod.compute_anchor_hash(None, "2026-09-09", a) == \
        audit_mod.compute_anchor_hash(None, "2026-09-09", b)


def test_the_anchor_hash_is_sensitive_to_every_component():
    heads = {"S:1": {"seq": 1, "entries": 1, "entry_hash": "x"}}
    base = audit_mod.compute_anchor_hash("p", "2026-09-09", heads)

    assert audit_mod.compute_anchor_hash("p2", "2026-09-09", heads) != base
    assert audit_mod.compute_anchor_hash("p", "2026-09-10", heads) != base
    assert audit_mod.compute_anchor_hash(
        "p", "2026-09-09", {"S:1": {"seq": 2, "entries": 1, "entry_hash": "x"}}) != base
    assert audit_mod.compute_anchor_hash(
        "p", "2026-09-09", {"S:1": {"seq": 1, "entries": 1, "entry_hash": "z"}}) != base
    assert audit_mod.compute_anchor_hash("p", "2026-09-09", {}) != base


def test_canonical_stream_heads_is_compact_and_sorted():
    """The digest is over this string, so its shape is part of the format."""
    rendered = audit_mod.canonical_stream_heads(
        {"b": {"seq": 2}, "a": {"seq": 1}})
    assert rendered == '{"a":{"seq":1},"b":{"seq":2}}'
    assert json.loads(rendered) == {"a": {"seq": 1}, "b": {"seq": 2}}


# ======================================================================
# write_anchor
# ======================================================================
def test_write_anchor_records_every_streams_head():
    session = _session_with_two_streams()

    result = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    assert result["written"] is True
    assert result["streams_anchored"] == 2
    assert result["stream_heads"] == {
        "PROJECT:PRJ-1": {"seq": 3, "entries": 3, "entry_hash": "h1c"},
        "PROJECT:PRJ-2": {"seq": 2, "entries": 2, "entry_hash": "h2b"},
    }
    assert result["prev_anchor_hash"] is None, "the first anchor chains from nothing"
    assert result["anchor_hash"] == audit_mod.compute_anchor_hash(
        None, "2026-09-09", result["stream_heads"])


def test_write_anchor_serialises_on_an_advisory_lock():
    """Two nightly runs must not both read "no anchor yet" and race the key.

    The lock is taken before anything is read, which is the same ordering
    `append` uses and for the same reason.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    assert session.locks == ["audit_anchor:daily"]


def test_write_anchor_chains_each_day_to_the_previous_one():
    session = _session_with_two_streams()
    first = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    session.entries["PROJECT:PRJ-1"].append((4, "h1d"))

    second = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    assert second["prev_anchor_hash"] == first["anchor_hash"], (
        "an anchor that does not name its predecessor can be replaced on its "
        "own; the chain between anchors is what makes editing one visible.")


def test_writing_the_same_day_twice_returns_the_stored_anchor_and_writes_nothing():
    """`audit_anchor` is append-only, so the second write cannot overwrite.

    It must not pretend to have anchored either: the caller has to be able to
    tell "today is anchored" from "I just anchored today", or a job that has
    silently stopped running looks exactly like one that is working.
    """
    session = _session_with_two_streams()
    first = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    session.entries["PROJECT:PRJ-1"].append((4, "h1d"))

    second = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    assert second["written"] is False
    assert second["anchor_hash"] == first["anchor_hash"]
    assert len(session.anchors) == 1
    assert second["stream_heads"]["PROJECT:PRJ-1"]["seq"] == 3, (
        "the stored anchor must be returned, not a fresh snapshot dressed up "
        "as the stored one")


def test_an_empty_database_can_still_be_anchored():
    """The first anchor must not require a business event to have happened.

    An empty object is a truthful statement -- "no stream existed" -- and the
    next day's anchor chains from it. Refusing to write it would leave the
    earliest, most sensitive period of a system's life unanchored.
    """
    session = FakeSession()
    result = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    assert result["written"] is True
    assert result["stream_heads"] == {}
    assert result["anchor_hash"]


# ======================================================================
# verify_anchors -- the four findings, one test each
# ======================================================================
def test_an_intact_database_verifies():
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))

    assert result["intact"] is True
    assert result["state"] == audit_mod.ANCHOR_STATE_INTACT
    assert result["anchored"] is True
    assert result["anchor_chain_intact"] is True
    assert result["missing_streams"] == []
    assert result["truncated_streams"] == []
    assert result["diverged_streams"] == []


def test_a_growing_stream_is_not_a_finding():
    """An anchor is a floor, not an equality.

    A verifier that flagged growth would fire on every normal day, and a
    control that cries wolf daily is a control somebody turns off.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    session.entries["PROJECT:PRJ-1"].append((4, "h1d"))
    session.entries["PROJECT:PRJ-3"] = [(1, "h3a")]

    assert audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))["intact"] is True


def test_a_wholly_deleted_stream_is_reported_missing():
    """THE CASE THE CHAIN CANNOT SEE AT ALL.

    `verify_chain` on a deleted stream returns `stream_found=False` -- but
    only if you already know the stream should exist. Nothing else in the
    product knew. The anchor is what knows.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    del session.entries["PROJECT:PRJ-2"]

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))
    assert result["intact"] is False
    assert result["state"] == audit_mod.ANCHOR_STATE_MISSING_STREAM
    assert result["missing_streams"] == ["PROJECT:PRJ-2"]
    assert result["anchor_chain_intact"] is True, (
        "the anchors themselves were not touched; saying they were would send "
        "the investigation to the wrong place")


def test_a_truncated_tail_is_reported():
    """The other case a contiguity check provably cannot catch.

    Removing the last two entries of a three-entry stream leaves seq [1],
    which IS contiguous and IS correctly linked. Only the anchor remembers
    that the head used to be 3.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    session.entries["PROJECT:PRJ-1"] = [(1, "h1a")]

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))
    assert result["intact"] is False
    assert result["state"] == audit_mod.ANCHOR_STATE_TRUNCATED
    # `anchored_on` names WHICH anchor established the floor. It is not
    # decoration: the floor is now the highest seq ANY anchor recorded, so an
    # operator has to be able to see which day is being held against the log
    # without re-deriving it.
    assert result["truncated_streams"] == [
        {"stream_key": "PROJECT:PRJ-1", "anchored_seq": 3, "current_seq": 1,
         "anchored_on": "2026-09-09"}]
    assert result["missing_streams"] == []


def test_a_rewritten_entry_below_the_anchor_point_is_reported_diverged():
    """Delete-and-reinsert: the head seq is right and the hash is not."""
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    session.entries["PROJECT:PRJ-1"][2] = (3, "REWRITTEN")

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))
    assert result["intact"] is False
    assert result["state"] == audit_mod.ANCHOR_STATE_DIVERGED
    assert result["diverged_streams"] == [{
        "stream_key": "PROJECT:PRJ-1", "seq": 3, "anchored_on": "2026-09-09",
        "anchored_entry_hash": "h1c", "current_entry_hash": "REWRITTEN"}]


# ======================================================================
# M-2: the NEWEST anchor is not the only anchor
# ======================================================================
def test_a_stream_deleted_after_its_anchor_and_re_anchored_is_still_missing():
    """THE HOLE THE NEWEST-ANCHOR-ONLY COMPARISON LEFT.

    `verify_anchors` used to iterate ``anchors[-1]`` and nothing else. So:
    anchor day 9, delete a stream, let day 10's anchor be written normally.
    Day 10 legitimately does not mention the stream -- it no longer exists --
    so ``missing_streams`` came back empty. Nothing was edited, so the anchor
    hashes all verify. The day-9 anchor, the one row in the database that
    remembers the stream existed, was checked for its OWN hash and never for
    whether what it recorded is still there.

    ``intact=True``. A whole audit stream gone, and the control built
    specifically to see that reported a pass -- for the cost of waiting one
    day before running the writer again.

    Deleting the union in `verify_anchors` (comparing `anchors[-1]` alone
    again) makes this test red.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    del session.entries["PROJECT:PRJ-2"]
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))

    assert result["anchor_chain_intact"] is True, (
        "no anchor was edited; accusing the anchors sends the investigation "
        "to the wrong place")
    assert result["missing_streams"] == ["PROJECT:PRJ-2"], (
        "a stream deleted between two anchors was invisible: the newest "
        "anchor never saw it, and only the earlier one remembers it existed.")
    assert result["state"] == audit_mod.ANCHOR_STATE_MISSING_STREAM
    assert result["intact"] is False
    assert result["streams_anchored"] == 1, "the newest anchor holds one stream"
    assert result["streams_anchored_ever"] == 2, (
        "two streams have been anchored at some point, and that is the floor "
        "the log is held to")


def test_the_floor_is_the_highest_seq_any_anchor_ever_recorded():
    """A tail truncated below an OLDER anchor is still a truncation.

    Anchor at seq 3, truncate to seq 1, anchor again at seq 1. The newest
    anchor agrees with the log perfectly -- they were written from the same
    rows -- so a newest-only comparison reports nothing. The floor is the
    highest seq ever anchored, not the most recently anchored one.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    session.entries["PROJECT:PRJ-1"] = [(1, "h1a")]
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))

    assert result["truncated_streams"] == [
        {"stream_key": "PROJECT:PRJ-1", "anchored_seq": 3, "current_seq": 1,
         "anchored_on": "2026-09-09"}]
    assert result["state"] == audit_mod.ANCHOR_STATE_TRUNCATED
    assert result["intact"] is False


def test_an_anchor_that_forgot_a_live_stream_is_reported_as_incomplete():
    """The other direction: the stream is fine, the newest anchor is not.

    `stream_heads` returns every stream that has rows, so a newest anchor
    missing a stream that IS present cannot have missed it honestly. That is
    an incomplete anchor -- the record every future verification would be held
    to -- and it is a different finding from a deleted stream, with a
    different response.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    stored_date, heads, prev, _hash = session.anchors[-1]
    forged = {k: v for k, v in heads.items() if k != "PROJECT:PRJ-2"}
    session.anchors[-1] = (stored_date, forged, prev,
                           audit_mod.compute_anchor_hash(
                               prev, "2026-09-10", forged))

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))

    assert result["missing_streams"] == [], "the stream was never deleted"
    assert result["dropped_from_newest_anchor"] == [{
        "stream_key": "PROJECT:PRJ-2",
        "first_anchored_on": "2026-09-09",
        "last_anchored_on": "2026-09-09",
        "newest_anchor_date": "2026-09-10"}]
    assert result["state"] == audit_mod.ANCHOR_STATE_ANCHOR_INVALID_OR_STALE
    assert result["intact"] is False


def test_a_deleted_stream_is_not_also_blamed_on_the_anchor():
    """An honest anchor drops a stream that no longer exists.

    Both findings are true of the same row, and reporting both would point an
    operator at the writer when the thing that happened is a deletion. Only
    `missing_streams` fires.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    del session.entries["PROJECT:PRJ-2"]
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))

    assert result["missing_streams"] == ["PROJECT:PRJ-2"]
    assert result["dropped_from_newest_anchor"] == []


# ======================================================================
# Staleness: the control degrading back towards inoperative
# ======================================================================
def test_an_anchor_older_than_the_horizon_is_stale_and_not_intact():
    """A writer that stopped running is where this whole finding started.

    `audit_anchor` sat empty for three waves behind triggers and grants. The
    same end state is reached by a writer that ran once and then stopped, and
    a verification that answered `intact=True` about it would be describing a
    database whose newest evidence is a month old.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    fresh = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))
    assert fresh["anchor_stale"] is False, "one day of slack is tolerated"
    assert fresh["anchor_age_days"] == 1
    assert fresh["intact"] is True

    stale = audit_mod.verify_anchors(session, as_of=date(2026, 9, 12))
    assert stale["anchor_stale"] is True
    assert stale["anchor_age_days"] == 3
    assert stale["state"] == audit_mod.ANCHOR_STATE_ANCHOR_INVALID_OR_STALE
    assert stale["intact"] is False


def test_a_malformed_head_does_not_crash_the_verifier():
    """Migration 024 constrains `stream_heads` to an object, not its VALUES.

    ``int(anchored["seq"])`` raises on a head of ``5`` or ``null``, and a
    verifier that raises inside a scheduled Function does not report a
    finding: it reports a stack trace to a log nobody is reading, and the
    day's verification silently does not happen.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    stored_date, heads, prev, anchor_hash = session.anchors[0]
    session.anchors[0] = (stored_date, {**heads, "PROJECT:PRJ-1": 5},
                          prev, anchor_hash)

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))

    assert result["malformed_anchor_dates"] == ["2026-09-09"]
    assert result["state"] == audit_mod.ANCHOR_STATE_ANCHOR_INVALID_OR_STALE
    assert result["intact"] is False


def test_every_state_name_is_distinct_and_ordered():
    """Five states, not two -- and the precedence is part of the contract.

    A verifier that reported `intact: bool` alone collapsed "nobody ever ran
    the writer" into the same answer as "somebody deleted a stream", and those
    two call for entirely different responses.
    """
    assert len(set(audit_mod.ANCHOR_STATE_PRECEDENCE)) == \
        len(audit_mod.ANCHOR_STATE_PRECEDENCE)
    assert audit_mod.ANCHOR_STATE_PRECEDENCE[0] == \
        audit_mod.ANCHOR_STATE_NEVER_ANCHORED, (
            "an unanchored database cannot support any other finding, so it "
            "is reported first")
    assert audit_mod.ANCHOR_STATE_PRECEDENCE[-1] == audit_mod.ANCHOR_STATE_INTACT
    assert {audit_mod.ANCHOR_STATE_NEVER_ANCHORED,
            audit_mod.ANCHOR_STATE_ANCHOR_INVALID_OR_STALE,
            audit_mod.ANCHOR_STATE_MISSING_STREAM,
            audit_mod.ANCHOR_STATE_TRUNCATED,
            audit_mod.ANCHOR_STATE_INTACT}.issubset(
                set(audit_mod.ANCHOR_STATE_PRECEDENCE))


def test_a_forged_anchor_breaks_the_anchor_chain():
    """Editing an anchor to match a deleted stream must not launder it.

    The attacker's obvious next move once anchors exist is to edit the anchor
    rather than the log. The row is append-only in PostgreSQL, so this needs
    privileges the application does not have -- and even then the recomputed
    hash no longer matches what the row claims.
    """
    session = _session_with_two_streams()
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    stored_date, heads, prev, anchor_hash = session.anchors[0]
    forged = {k: v for k, v in heads.items() if k != "PROJECT:PRJ-2"}
    session.anchors[0] = (stored_date, forged, prev, anchor_hash)
    del session.entries["PROJECT:PRJ-2"]

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))
    assert result["intact"] is False
    assert result["state"] == audit_mod.ANCHOR_STATE_ANCHOR_INVALID_OR_STALE
    assert result["anchor_chain_intact"] is False
    assert result["first_broken_anchor_date"] == "2026-09-09"
    assert result["missing_streams"] == [], (
        "the forgery worked at the stream level, which is exactly why the "
        "anchor's own hash has to be checked as well")


def test_an_anchor_edited_to_hide_a_deletion_is_caught():
    """The complete attack, end to end: edit the anchor AND rehash it.

    Recomputing the forged anchor's own hash makes it self-consistent again.
    It does not make the DAY AFTER consistent: that anchor named the original
    hash as its predecessor, so the tampering surfaces one row later. This is
    why anchors chain to each other and not merely to the log.
    """
    session = _session_with_two_streams()
    day1 = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    audit_mod.write_anchor(session, anchor_date=date(2026, 9, 10))

    forged_heads = {k: v for k, v in day1["stream_heads"].items()
                    if k != "PROJECT:PRJ-2"}
    session.anchors[0] = (date(2026, 9, 9), forged_heads, None,
                          audit_mod.compute_anchor_hash(None, "2026-09-09", forged_heads))
    del session.entries["PROJECT:PRJ-2"]

    result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 10))
    assert result["anchor_chain_intact"] is False, (
        "a re-hashed anchor was accepted. The day-2 anchor still names the "
        "ORIGINAL day-1 hash as its predecessor, and that mismatch is the "
        "whole reason anchors form a chain.")
    assert result["first_broken_anchor_date"] == "2026-09-10"
    assert result["intact"] is False


def test_a_database_that_was_never_anchored_is_not_reported_intact():
    """"Nobody ever anchored this" is the state the gap lives in.

    Returning intact=True here would be the same defect as an empty scope
    compiling to TRUE, or a maker-checker call comparing None: a missing input
    silently reading as a pass.
    """
    session = _session_with_two_streams()

    result = audit_mod.verify_anchors(session)

    assert result["anchored"] is False
    assert result["intact"] is False
    assert result["state"] == audit_mod.ANCHOR_STATE_NEVER_ANCHORED
    assert result["anchors_checked"] == 0
    assert "write_anchor" in result["note"]


def test_the_fake_dispatches_every_statement_the_module_issues():
    """Guards the double itself.

    A session double that silently returns None for an unrecognised statement
    turns "the module asked a question I do not model" into "the answer was
    nothing", and every test above would keep passing while proving less. The
    double raises instead, and this asserts that it does.
    """
    session = FakeSession()
    with pytest.raises(AssertionError, match="does not model"):
        session.fetchone("SELECT something FROM a_table_the_double_never_heard_of")


# ======================================================================
# Live PostgreSQL. NEVER EXECUTED on the authoring machine -- first run in CI.
# ======================================================================
@PG
def test_write_anchor_and_verify_anchors_round_trip_live(pg_database, pg_scope):
    """Two real appends, a real anchor, a real verification."""
    from app.backend.pg.audit import append

    stream_key = f"AnchorTest:{uuid.uuid4().hex}"
    with pg_database.session(pg_scope) as session:
        append(session, "tester", "CREATED", "AnchorTest", "1", "first",
               stream_key=stream_key)
        append(session, "tester", "UPDATED", "AnchorTest", "1", "second",
               stream_key=stream_key)

    with pg_database.session(pg_scope) as session:
        written = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    assert written["written"] is True
    assert written["stream_heads"][stream_key]["seq"] == 2
    assert written["stream_heads"][stream_key]["entries"] == 2

    with pg_database.session(pg_scope) as session:
        result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))

    assert result["anchored"] is True
    assert result["intact"] is True
    assert result["anchor_chain_intact"] is True
    assert result["newest_anchor_date"] == "2026-09-09"


@PG
def test_a_second_anchor_for_the_same_day_does_not_raise_live(pg_database, pg_scope):
    """The primary key would raise; `write_anchor` must not let it.

    A nightly job that crashes on its second run of the same day is a job that
    stops running, and an anchor nobody writes is the gap all over again.
    """
    with pg_database.session(pg_scope) as session:
        first = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))
    with pg_database.session(pg_scope) as session:
        second = audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    assert first["written"] is True
    assert second["written"] is False
    assert second["anchor_hash"] == first["anchor_hash"]


@PG
def test_a_whole_stream_deleted_behind_the_triggers_is_still_caught_live(
        pg_database, pg_scope, pg_connection):
    """The attack in full: disable the append-only trigger, delete a stream.

    The triggers and the REVOKE stop the APPLICATION. They do not stop someone
    holding the database, which is the threat anchors exist for -- so the test
    takes that position deliberately, as a table owner, and the anchor must
    still find it. The triggers are restored afterwards so nothing else in the
    disposable database runs unprotected.
    """
    from app.backend.pg.audit import append

    stream_key = f"AnchorDelete:{uuid.uuid4().hex}"
    with pg_database.session(pg_scope) as session:
        append(session, "tester", "CREATED", "AnchorDelete", "1", "only entry",
               stream_key=stream_key)
        audit_mod.write_anchor(session, anchor_date=date(2026, 9, 9))

    pg_connection.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_delete")
    try:
        pg_connection.execute("DELETE FROM audit_log WHERE stream_key = %s", (stream_key,))
        pg_connection.commit()
    finally:
        pg_connection.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_delete")
        pg_connection.commit()

    with pg_database.session(pg_scope) as session:
        result = audit_mod.verify_anchors(session, as_of=date(2026, 9, 9))

    assert result["intact"] is False
    assert stream_key in result["missing_streams"], (
        "an entire audit stream was deleted and the anchor did not notice. "
        f"missing_streams={result['missing_streams']}")


@PG
def test_migration_024_refuses_a_degenerate_anchor_live(pg_connection):
    """The three constraints, each proved by the row it must refuse.

    Every column is supplied because `audit_anchor` has four with no default
    (`anchor_date`, `stream_heads`, `anchor_hash` NOT NULL; `prev_anchor_hash`
    is nullable and is passed anyway), and each case must fail on the CHECK
    under test rather than on a missing value.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    good = Jsonb({"S:1": {"seq": 1, "entries": 1, "entry_hash": "h"}})

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, "
            "anchor_hash) VALUES (%s, %s, %s, %s)",
            (date(2026, 1, 2), good, None, ""))
    pg_connection.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, "
            "anchor_hash) VALUES (%s, %s, %s, %s)",
            (date(2026, 1, 3), Jsonb([]), None, "abc"))
    pg_connection.rollback()

    pg_connection.execute(
        "INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, "
        "anchor_hash) VALUES (%s, %s, %s, %s)",
        (date(2026, 1, 4), good, None, "duplicate-hash"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_connection.execute(
            "INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, "
            "anchor_hash) VALUES (%s, %s, %s, %s)",
            (date(2026, 1, 5), good, None, "duplicate-hash"))
    pg_connection.rollback()


@PG
def test_an_empty_object_of_heads_is_still_accepted_live(pg_connection):
    """The permitted degenerate case, so the CHECK is not read as forbidding it."""
    from psycopg.types.json import Jsonb

    pg_connection.execute(
        "INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, "
        "anchor_hash) VALUES (%s, %s, %s, %s)",
        (date(2026, 1, 6), Jsonb({}), None, "empty-heads-is-legal"))
    pg_connection.rollback()


# ======================================================================
# The verifier has to be REACHABLE, or it is code nobody runs
# ======================================================================
def test_the_anchor_verifier_is_exposed_as_a_read_only_route():
    """A control an operator cannot invoke is the empty-table problem again.

    `audit_anchor` had triggers, grants and no writer for three waves. A
    verifier with no route would be the same story with a different noun, so
    the route is asserted to exist, to be a GET, and to sit under the router's
    own `audit.read` dependency rather than carrying a guard of its own that a
    future route could forget to copy.
    """
    from app.backend.api import audit as audit_api

    paths = {
        route.path: sorted(route.methods)
        for route in audit_api.router.routes
        if hasattr(route, "methods")
    }
    assert "/api/audit/anchors/verify" in paths, (
        f"the anchor verifier is not reachable. Routes: {sorted(paths)}")
    assert paths["/api/audit/anchors/verify"] == ["GET"], (
        "verification is a read; it must not be reachable by any writing verb.")

    # BY IDENTITY, not by name. `require_audit_read` is an instance of
    # `_AuditRead`, so it has no `__name__` and `str()` of it renders
    # `_AuditRead` -- a name-based check misses the guard that is actually
    # there, which is how the first version of this test failed against a
    # correct router. Identity is also the stronger check: a name match would
    # still pass if the router were handed a different callable that happened
    # to be called `require_audit_read`, which is the substitution this
    # assertion exists to catch.
    guards = [getattr(d, "dependency", None) for d in audit_api.router.dependencies]
    assert audit_api.require_audit_read in guards, (
        "the audit router no longer carries its own `audit.read` dependency, "
        f"so every route in it -- including this one -- is open. Guards: {guards}")


def test_no_route_in_the_audit_router_writes_an_anchor():
    """Writing one is a scheduled job's work, not an HTTP verb.

    Giving it a route would mean deciding which role may cause an anchor to
    exist. `test_aud_c_006_auditor_is_read_only` pins the Auditor to four
    permissions, and widening an audit-finding assertion so a new feature
    reads tidily is not a call to make in passing -- the same reasoning
    `approval.read` and `export.create` are recorded under.
    """
    from app.backend.api import audit as audit_api

    writing = [
        route.path for route in audit_api.router.routes
        if hasattr(route, "methods") and set(route.methods) - {"GET", "HEAD", "OPTIONS"}
    ]
    assert writing == [], (
        f"the audit router now has writing routes: {writing}. If one of them "
        "writes an anchor, the permission it needs is a role-mapping decision "
        "and belongs in auth.PERMISSIONS with a reason.")
