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
from datetime import datetime, timezone
from typing import Any

from .engine import Session
from .locking import advisory_audit_lock


def _payload(prev_hash: str | None, at_iso: str, actor: str, action: str,
             object_type: str, object_id: str, detail: str) -> str:
    return f"{prev_hash or ''}|{at_iso}|{actor}|{action}|{object_type}|{object_id}|{detail}"


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
    at_iso = at.isoformat()
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
        at_iso = at.isoformat() if hasattr(at, "isoformat") else str(at)
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

    return {
        "intact": first_break_seq is None,
        "entries_checked": checked,
        "first_break_seq": first_break_seq,
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
