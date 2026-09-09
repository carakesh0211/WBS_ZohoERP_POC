"""Hash-chained, append-only audit service.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``,
section "Audit chain".

The payload format is **frozen permanently**::

    prev|at|actor|action|type|id|detail

Changing it invalidates every hash already stored -- do not touch
:func:`compute_entry_hash`'s payload assembly for any reason.

Chains are per ``stream_key``, ordered by ``seq`` (``UNIQUE (stream_key,
seq)``). ``audit_id`` -- the table's ``GENERATED ALWAYS AS IDENTITY`` primary
key -- is **not** the chain order: identity values are assigned before commit
and two concurrent transactions can commit out of order, so a later
``audit_id`` can legitimately hold an earlier ``seq``. ``seq``, read and
assigned under :func:`~app.backend.pg.locking.advisory_audit_lock`, is the
real order, which is why :func:`append` takes that lock *before* reading
``prev_hash`` -- otherwise two concurrent writers to the same stream could
both read the same ``prev_hash`` and each mint a next entry chaining from it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .engine import Session
from .locking import advisory_audit_lock

#: The advisory-lock key anchor writing serialises on. Not a stream_key: no
#: audit_log row ever carries it, so it cannot collide with a business stream.
_ANCHOR_LOCK_KEY = "audit_anchor:daily"


def _payload(prev_hash: str | None, at_iso: str, actor: str, action: str,
             object_type: str, object_id: str, detail: str) -> str:
    return f"{prev_hash or ''}|{at_iso}|{actor}|{action}|{object_type}|{object_id}|{detail}"


def canonical_at(value: Any) -> str:
    """Render a timestamp for hashing, independent of session TimeZone.

    `timestamptz` is returned by the server rendered in the CONNECTION's
    TimeZone setting. A connection running with, say, `Asia/Kolkata` yields the
    same instant as `...+05:30` where the appending connection wrote
    `...+00:00`. The bytes differ, so the recomputed hash differs, and a
    perfectly intact chain reports as BROKEN -- a false tamper alarm on the
    guarantee this product is built to make.

    Normalising to UTC on both the append and the verify path makes the hash a
    function of the instant, not of whoever happens to be connected.
    """
    if hasattr(value, "astimezone"):
        if value.tzinfo is None:              # naive: the server said UTC
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def compute_entry_hash(prev_hash: str | None, at_iso: str, actor: str, action: str,
                        object_type: str, object_id: str, detail: str) -> str:
    """The frozen hash function. `at_iso` must be the exact string stored/read
    back for `at` -- recomputing from a re-formatted timestamp is how a chain
    that looks broken actually is not, or the reverse."""
    payload = _payload(prev_hash, at_iso, actor, action, object_type, object_id, detail)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append(session: Session, actor: str, action: str, object_type: str, object_id: str,
           detail: str, *, correlation_id: str | None = None,
           stream_key: str | None = None) -> dict[str, Any]:
    """Append one entry to a hash-chained stream.

    `stream_key` defaults to `f"{object_type}:{object_id}"` -- one chain per
    business object -- but callers that want a coarser or finer stream (for
    example, one chain per document type, or per tenant) may pass their own.

    Must be called from inside an already-open `session` (a transaction). Per
    the global lock order in `locking.py`, this should be the *last* locking
    action of a mutating service function: cells first, then the document row,
    then this.
    """
    stream_key = stream_key or f"{object_type}:{object_id}"
    advisory_audit_lock(session, stream_key)   # BEFORE reading prev_hash -- see module docstring

    prev = session.fetchone(
        "SELECT seq, entry_hash FROM audit_log WHERE stream_key = %s "
        "ORDER BY seq DESC LIMIT 1",
        (stream_key,))
    prev_seq, prev_hash = (prev[0], prev[1]) if prev is not None else (0, None)
    seq = prev_seq + 1

    at = datetime.now(timezone.utc)
    at_iso = canonical_at(at)   # UTC-normalised; see canonical_at()
    entry_hash = compute_entry_hash(prev_hash, at_iso, actor, action,
                                     object_type, object_id, detail)

    row = session.fetchone(
        """
        INSERT INTO audit_log
            (stream_key, seq, at, actor, action, object_type, object_id,
             detail, correlation_id, prev_hash, entry_hash)
        VALUES (%(stream_key)s, %(seq)s, %(at)s, %(actor)s, %(action)s,
                %(object_type)s, %(object_id)s, %(detail)s, %(correlation_id)s,
                %(prev_hash)s, %(entry_hash)s)
        RETURNING audit_id, stream_key, seq, at, actor, action, object_type,
                  object_id, detail, correlation_id, entry_hash
        """,
        {
            "stream_key": stream_key, "seq": seq, "at": at, "actor": actor,
            "action": action, "object_type": object_type, "object_id": object_id,
            "detail": detail, "correlation_id": correlation_id,
            "prev_hash": prev_hash, "entry_hash": entry_hash,
        },
    )
    return _row_to_entry(row)


def verify_chain(session: Session, stream_key: str) -> dict[str, Any]:
    """Recompute every entry's hash for `stream_key` and check the chain.

    Returns `{"intact": bool, "entries_checked": int, "first_break_seq": int|None}`.
    Scans the whole stream regardless of where a break is found, so
    `entries_checked` always reflects the number of rows examined; only the
    *first* seq at which either the recorded `prev_hash` does not match the
    previous entry's `entry_hash`, or the recorded `entry_hash` does not match
    the recomputed one, is reported -- a single corruption early in the chain
    otherwise cascades into a wall of "breaks" that all point back to the same
    root cause.
    """
    rows = session.fetchall(
        """
        SELECT seq, at, actor, action, object_type, object_id, detail,
               prev_hash, entry_hash
        FROM audit_log WHERE stream_key = %s ORDER BY seq
        """,
        (stream_key,))

    expected_prev: str | None = None
    first_break_seq: int | None = None
    checked = 0

    for (seq, at, actor, action, object_type, object_id, detail,
         row_prev_hash, row_entry_hash) in rows:
        checked += 1
        at_iso = canonical_at(at)
        recomputed = compute_entry_hash(row_prev_hash, at_iso, actor, action,
                                         object_type, object_id, detail)
        broken = (row_prev_hash != expected_prev) or (recomputed != row_entry_hash)
        if broken and first_break_seq is None:
            first_break_seq = seq
        # Continue walking the chain as *stored*, not as recomputed: a single
        # corrupted entry should not desynchronise every entry after it from
        # the definition of "expected_prev", or every subsequent row would
        # spuriously report as broken too.
        expected_prev = row_entry_hash

    # Tail truncation and a missing stream both used to report intact=True.
    #
    # The loop above walks the rows that ARE stored and checks each links to
    # the previous one. Deleting the LAST k entries leaves a perfectly linked
    # prefix, so nothing was flagged -- an attacker removing the most recent
    # (most incriminating) entries was invisible. And an empty or misspelled
    # stream_key returned intact=True with entries_checked=0, so a typo in the
    # nightly verification job read as a pass.
    #
    # Contiguity is the available check: seq is assigned 1..n under the
    # advisory lock, so a gap or a short tail is detectable without trusting
    # any external record. It does NOT detect truncation of a whole stream --
    # that needs the daily anchors (`write_anchor` / `verify_anchors` below),
    # and this result deliberately claims nothing about them: a database on
    # which no anchor was ever written has no evidence either way, and saying
    # so is the point.
    expected_seqs = list(range(1, checked + 1))
    actual_seqs = [row[0] for row in rows]
    contiguous = actual_seqs == expected_seqs
    if not contiguous and first_break_seq is None:
        missing = sorted(set(expected_seqs) - set(actual_seqs))
        first_break_seq = missing[0] if missing else (actual_seqs[-1] + 1)

    return {
        "intact": first_break_seq is None and contiguous and checked > 0,
        "entries_checked": checked,
        "first_break_seq": first_break_seq,
        "head_seq": actual_seqs[-1] if actual_seqs else None,
        "sequence_contiguous": contiguous,
        # An empty result is NOT an intact chain. It is either an unknown
        # stream or a wholly deleted one, and both deserve saying so.
        "stream_found": checked > 0,
        "whole_stream_truncation_note": (
            "Contiguity proves no entry is missing from WITHIN this stream. "
            "Deletion of an entire stream is invisible here by construction and "
            "is detectable only against the daily anchors -- see "
            "write_anchor()/verify_anchors(), and note that anchors detect it "
            "only for the days on which one was actually written."
        ),
    }


# ======================================================================
# Anchors -- the only thing that can detect a WHOLE STREAM being deleted
# ======================================================================
#
# `verify_chain` above proves that the rows a stream still HAS link to each
# other and carry no gap. It cannot prove anything about rows that are gone
# from the end, and it cannot prove anything at all about a stream that no
# longer exists: both leave a perfectly self-consistent database, because a
# hash chain is a statement about the rows you are looking at.
#
# An anchor is the external record that makes those two cases detectable. It
# writes down, once per day and immutably (`audit_anchor` is append-only by
# trigger and has UPDATE/DELETE revoked from `capex_app`), the head of every
# stream that existed at that moment. Verification then asks the question the
# chain cannot: is every stream this anchor SAW still present, still at least
# as long, and still carrying the same entry hash at the seq the anchor
# recorded?
#
# Deleting a stream now requires also rewriting an append-only anchor row --
# and because anchors chain to each other through `prev_anchor_hash`, rewriting
# one invalidates every anchor after it.
#
# `audit_anchor` shipped in `migrations/pg/001_foundation.sql` with triggers,
# grants and no writer. Everything below is that missing writer and its
# verifier; `migrations/pg/024_audit_anchor_integrity.sql` adds the constraints
# that stop a degenerate anchor (empty hash, non-object heads, a duplicate of
# another day's) from being written in the first place.

#: FROZEN, exactly as `_payload` is frozen: changing the assembly invalidates
#: every anchor already written, and an anchor nobody can recompute is a record
#: nobody can rely on.
#:
#:     prev_anchor_hash|anchor_date|canonical_json(stream_heads)
#:
#: `canonical_json` is `sort_keys=True` with no whitespace, so the digest is a
#: function of the CONTENT and not of dict ordering or of how psycopg happened
#: to render the jsonb on the way back out.
def canonical_stream_heads(stream_heads: dict[str, Any]) -> str:
    return json.dumps(stream_heads, sort_keys=True, separators=(",", ":"))


def compute_anchor_hash(prev_anchor_hash: str | None, anchor_date_iso: str,
                        stream_heads: dict[str, Any]) -> str:
    payload = (f"{prev_anchor_hash or ''}|{anchor_date_iso}|"
               f"{canonical_stream_heads(stream_heads)}")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stream_heads(session: Session) -> dict[str, dict[str, Any]]:
    """Every stream's head seq, head entry hash and row count, right now.

    One statement, not one per stream: the anchor must be a snapshot of a
    single instant, and n+1 queries across a stream list read at a different
    instant is not one.
    """
    rows = session.fetchall(
        """
        SELECT h.stream_key, h.head_seq, h.entries, l.entry_hash
        FROM (
            SELECT stream_key, max(seq) AS head_seq, count(*) AS entries
            FROM audit_log GROUP BY stream_key
        ) h
        JOIN audit_log l ON l.stream_key = h.stream_key AND l.seq = h.head_seq
        ORDER BY h.stream_key
        """)
    return {
        stream_key: {"seq": int(head_seq), "entries": int(entries),
                     "entry_hash": entry_hash}
        for stream_key, head_seq, entries, entry_hash in rows
    }


def write_anchor(session: Session, *, anchor_date: date | None = None) -> dict[str, Any]:
    """Write one day's anchor over every audit stream. Idempotent per date.

    `anchor_date` is the PRIMARY KEY, and `audit_anchor` is append-only, so a
    second write for the same date cannot overwrite the first and must not
    pretend to. It returns the anchor that is actually stored, with
    ``written=False``, and the caller can see that the day was already
    anchored rather than believing it just anchored it.

    Serialised on the same advisory-lock mechanism the chain itself uses, so
    two concurrent nightly runs cannot both read "no anchor yet" and race into
    the primary key.
    """
    anchor_date = anchor_date or datetime.now(timezone.utc).date()
    advisory_audit_lock(session, _ANCHOR_LOCK_KEY)

    existing = session.fetchone(
        "SELECT anchor_date, stream_heads, prev_anchor_hash, anchor_hash "
        "FROM audit_anchor WHERE anchor_date = %s", (anchor_date,))
    if existing is not None:
        return {"anchor_date": existing[0].isoformat(), "stream_heads": existing[1],
                "prev_anchor_hash": existing[2], "anchor_hash": existing[3],
                "streams_anchored": len(existing[1] or {}), "written": False}

    prev = session.fetchone(
        "SELECT anchor_hash FROM audit_anchor WHERE anchor_date < %s "
        "ORDER BY anchor_date DESC LIMIT 1", (anchor_date,))
    prev_anchor_hash = prev[0] if prev is not None else None

    heads = stream_heads(session)
    anchor_date_iso = anchor_date.isoformat()
    anchor_hash = compute_anchor_hash(prev_anchor_hash, anchor_date_iso, heads)

    session.execute(
        """
        INSERT INTO audit_anchor (anchor_date, stream_heads, prev_anchor_hash, anchor_hash)
        VALUES (%(anchor_date)s, %(stream_heads)s, %(prev_anchor_hash)s, %(anchor_hash)s)
        """,
        {"anchor_date": anchor_date, "stream_heads": Jsonb(heads),
         "prev_anchor_hash": prev_anchor_hash, "anchor_hash": anchor_hash})

    return {"anchor_date": anchor_date_iso, "stream_heads": heads,
            "prev_anchor_hash": prev_anchor_hash, "anchor_hash": anchor_hash,
            "streams_anchored": len(heads), "written": True}


# ---------------------------------------------------------------------
# The five states verification must be able to tell apart
# ---------------------------------------------------------------------
#
# `intact: bool` collapses six materially different situations into two, and
# the two it produces are the wrong two: "not intact" reads as "somebody
# tampered" when the far commoner cause is that nobody has ever run the
# writer. An operator who cannot tell those apart either ignores the alarm or
# investigates a crime that did not happen.
#
# Precedence runs from "you cannot trust the evidence" down to "the evidence
# says something specific", because a broken anchor chain makes every
# finding computed against those anchors unreliable, and reporting a missing
# stream from an anchor set that has itself been edited sends the
# investigation to the wrong place.
ANCHOR_STATE_NEVER_ANCHORED = "NEVER_ANCHORED"
ANCHOR_STATE_ANCHOR_INVALID_OR_STALE = "ANCHOR_INVALID_OR_STALE"
ANCHOR_STATE_MISSING_STREAM = "MISSING_STREAM"
ANCHOR_STATE_TRUNCATED = "TRUNCATED_AFTER_LAST_ANCHOR"
#: NOT one of the five the brief names, and deliberately kept separate rather
#: than folded into TRUNCATED. `verify_anchors` has always distinguished
#: divergence -- a rewritten entry BELOW the anchor point, where the head seq
#: is still right and the hash is not -- and collapsing an already-detected,
#: differently-remediated finding into a neighbouring bucket to hit a count of
#: five would lose information the verifier had.
ANCHOR_STATE_DIVERGED = "DIVERGED_BELOW_ANCHOR"
ANCHOR_STATE_INTACT = "INTACT_ANCHORED"

#: The order findings are reported in. First match wins.
ANCHOR_STATE_PRECEDENCE: tuple[str, ...] = (
    ANCHOR_STATE_NEVER_ANCHORED,
    ANCHOR_STATE_ANCHOR_INVALID_OR_STALE,
    ANCHOR_STATE_MISSING_STREAM,
    ANCHOR_STATE_TRUNCATED,
    ANCHOR_STATE_DIVERGED,
    ANCHOR_STATE_INTACT,
)

#: How old the newest anchor may be before the database is reported STALE.
#:
#: The writer runs daily. One day of slack absorbs a run that fired either
#: side of midnight UTC and a verification that runs before the day's anchor;
#: two days means the writer missed a night, and 024's own COMMENT says what
#: that costs -- "a day on which the writer did not run is a day with no
#: evidence". Stale is not the same finding as tampered and is not reported as
#: one, but it is emphatically not a pass: the control is degrading in exactly
#: the direction that ends in it being inoperative again.
MAX_ANCHOR_AGE_DAYS = 1


def _anchored_seq(anchored: Any) -> int | None:
    """The `seq` an anchor recorded for one stream, or None if unreadable.

    Migration 024 constrains `stream_heads` to a jsonb OBJECT; it does not and
    cannot constrain its VALUES. A head of ``5``, ``null`` or ``"x"`` therefore
    reaches this code, and ``int(anchored["seq"])`` raises on every one of
    them. A verifier that raises inside a scheduled Function does not report a
    finding -- it reports a stack trace to a log nobody is reading, and the
    day's verification silently does not happen. A malformed head is treated
    as what it is: an anchor that cannot be trusted.
    """
    if not isinstance(anchored, dict):
        return None
    try:
        return int(anchored["seq"])
    except (KeyError, TypeError, ValueError):
        return None


def verify_anchors(session: Session, *, as_of: date | None = None,
                   max_anchor_age_days: int = MAX_ANCHOR_AGE_DAYS) -> dict[str, Any]:
    """Verify the anchor chain, and the audit log against EVERY anchor.

    Two independent questions, and both have to be asked:

    1. **Are the anchors themselves intact?** Each anchor's hash is recomputed
       from its own stored heads and its recorded `prev_anchor_hash`, and each
       is required to name its predecessor. Editing one anchor to cover up a
       deletion therefore breaks every anchor after it.
    2. **Does the audit log still contain what the anchors saw?** For every
       stream ANY anchor recorded: it must still exist (`missing` --
       WHOLE-STREAM TRUNCATION, the case `verify_chain` provably cannot see),
       its head seq must not have gone BACKWARDS (`truncated` -- tail
       truncation), and the entry hash at the highest anchored seq must be
       unchanged (`diverged` -- history rewritten below the anchor point).

    **Against every anchor, not merely the newest.** This function used to
    iterate `anchors[-1]` alone, and that left a hole big enough to drive the
    whole attack through: delete a stream on day 0 *after* its anchor is
    written, then let day 1's anchor be written normally. Day 1 legitimately
    does not mention the stream -- it no longer exists -- so `missing_streams`
    came back empty; the anchor chain hashes verify, because nothing was
    edited; and the day-0 anchor, the one row in the database that remembers
    the stream existed, was checked for its own hash and never for whether
    what it recorded is still there. `intact=True`. The union of stream keys
    across all anchors, with the highest seq each was ever anchored at, is the
    floor the log is held to, and a key an earlier anchor recorded that the
    newest one has dropped is reported in its own right
    (`dropped_from_newest_anchor`).

    A stream growing, or a brand-new stream appearing, is normal and is not a
    finding: an anchor is a floor, not an equality.

    Returns ``anchored=False`` when no anchor exists at all. That is NOT
    ``intact=True`` -- "nobody has ever anchored this database" is the state in
    which a whole-stream deletion is undetectable, and reporting it as a pass
    is how the gap this function closes stayed open.

    `as_of` and `max_anchor_age_days` exist so staleness is testable without
    waiting a day; production passes neither.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    anchors = session.fetchall(
        "SELECT anchor_date, stream_heads, prev_anchor_hash, anchor_hash "
        "FROM audit_anchor ORDER BY anchor_date")
    if not anchors:
        return {
            "anchored": False, "intact": False, "anchors_checked": 0,
            "state": ANCHOR_STATE_NEVER_ANCHORED,
            "anchor_chain_intact": False, "first_broken_anchor_date": None,
            "newest_anchor_date": None, "anchor_age_days": None,
            "anchor_stale": False, "streams_anchored": 0,
            "streams_anchored_ever": 0, "malformed_anchor_dates": [],
            "missing_streams": [], "truncated_streams": [], "diverged_streams": [],
            "dropped_from_newest_anchor": [],
            "note": ("No audit anchor has ever been written, so deletion of an "
                     "entire audit stream is not detectable. Run write_anchor()."),
        }

    # ---- 1. the anchor chain ----------------------------------------
    first_broken: str | None = None
    expected_prev: str | None = None
    malformed: list[str] = []
    for anchor_date_value, heads, prev_anchor_hash, anchor_hash in anchors:
        recomputed = compute_anchor_hash(prev_anchor_hash,
                                         anchor_date_value.isoformat(),
                                         heads or {})
        broken = (recomputed != anchor_hash) or (prev_anchor_hash != expected_prev)
        if broken and first_broken is None:
            first_broken = anchor_date_value.isoformat()
        expected_prev = anchor_hash
        if any(_anchored_seq(head) is None for head in (heads or {}).values()):
            malformed.append(anchor_date_value.isoformat())

    # ---- 2. the FLOOR every anchor together establishes ---------------
    # stream_key -> the highest seq any anchor ever recorded, the entry hash
    # that anchor recorded at that seq, and which anchor said so.
    floor: dict[str, dict[str, Any]] = {}
    first_seen: dict[str, str] = {}
    for anchor_date_value, heads, _prev, _hash in anchors:
        iso_date = anchor_date_value.isoformat()
        for stream_key, anchored in (heads or {}).items():
            first_seen.setdefault(stream_key, iso_date)
            seq = _anchored_seq(anchored)
            if seq is None:
                continue
            prior = floor.get(stream_key)
            if prior is None or seq > prior["seq"]:
                floor[stream_key] = {
                    "seq": seq,
                    "entry_hash": anchored.get("entry_hash"),
                    "anchor_date": iso_date,
                }

    newest_date, newest_heads, _prev, _hash = anchors[-1]
    newest_heads = newest_heads or {}
    current = stream_heads(session)

    missing, truncated, diverged, dropped = [], [], [], []
    for stream_key, anchored in sorted(floor.items()):
        now = current.get(stream_key)
        if stream_key not in newest_heads and now is not None:
            # An earlier anchor recorded this stream, the newest one does not,
            # and the stream is STILL THERE. `stream_heads` returns every
            # stream that has rows, so the newest anchor cannot have missed it
            # honestly: that anchor is incomplete, and an incomplete anchor is
            # the record every future verification would be held to.
            #
            # The other half of "in an earlier anchor, absent from the newest"
            # -- the stream is gone from the log too -- is NOT reported here.
            # Dropping a stream that no longer exists is the correct, innocent
            # behaviour of an honest anchor, and the finding in that case is
            # `missing_streams` below, which is the finding an operator has to
            # act on. Reporting it twice, under a heading that accuses the
            # anchor, would send the investigation at the writer instead of at
            # whoever deleted the stream.
            dropped.append({"stream_key": stream_key,
                            "first_anchored_on": first_seen.get(stream_key),
                            "last_anchored_on": anchored["anchor_date"],
                            "newest_anchor_date": newest_date.isoformat()})
        if now is None:
            missing.append(stream_key)
            continue
        if int(now["seq"]) < anchored["seq"]:
            truncated.append({"stream_key": stream_key,
                              "anchored_seq": anchored["seq"],
                              "current_seq": int(now["seq"]),
                              "anchored_on": anchored["anchor_date"]})
            continue
        at_anchor = session.fetchone(
            "SELECT entry_hash FROM audit_log WHERE stream_key = %s AND seq = %s",
            (stream_key, anchored["seq"]))
        if at_anchor is None or at_anchor[0] != anchored.get("entry_hash"):
            diverged.append({"stream_key": stream_key,
                             "seq": anchored["seq"],
                             "anchored_on": anchored["anchor_date"],
                             "anchored_entry_hash": anchored.get("entry_hash"),
                             "current_entry_hash": None if at_anchor is None else at_anchor[0]})

    anchor_age_days = (as_of - newest_date).days
    stale = anchor_age_days > max_anchor_age_days

    if first_broken is not None or malformed or dropped or stale:
        state = ANCHOR_STATE_ANCHOR_INVALID_OR_STALE
    elif missing:
        state = ANCHOR_STATE_MISSING_STREAM
    elif truncated:
        state = ANCHOR_STATE_TRUNCATED
    elif diverged:
        state = ANCHOR_STATE_DIVERGED
    else:
        state = ANCHOR_STATE_INTACT

    return {
        "anchored": True,
        "state": state,
        "anchors_checked": len(anchors),
        "anchor_chain_intact": first_broken is None,
        "first_broken_anchor_date": first_broken,
        "malformed_anchor_dates": malformed,
        "newest_anchor_date": newest_date.isoformat(),
        "anchor_age_days": anchor_age_days,
        "anchor_stale": stale,
        "streams_anchored": len(newest_heads),
        "streams_anchored_ever": len(floor),
        "missing_streams": missing,
        "truncated_streams": truncated,
        "diverged_streams": diverged,
        "dropped_from_newest_anchor": dropped,
        "intact": state == ANCHOR_STATE_INTACT,
        "note": ("Whole-stream deletion and tail truncation are detectable only "
                 "against these anchors; the per-stream chain cannot see either. "
                 "Every anchor is checked, not only the newest: a stream deleted "
                 "after its anchor was written is absent from every later anchor "
                 "for a perfectly innocent-looking reason."),
    }


def _row_to_entry(row: tuple) -> dict[str, Any]:
    (audit_id, stream_key, seq, at, actor, action, object_type, object_id,
     detail, correlation_id, entry_hash) = row
    return {
        "audit_id": audit_id,
        "stream_key": stream_key,
        "seq": seq,
        "at": at.isoformat() if hasattr(at, "isoformat") else at,
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "detail": detail,
        "correlation_id": correlation_id,
        "entry_hash": entry_hash,
    }
