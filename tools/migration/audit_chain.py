"""Carry the POC audit chain into PostgreSQL without recomputing a single hash.

The rule, and the reason it is absolute
---------------------------------------
``prev_hash`` and ``entry_hash`` are COPIED. They are never recomputed. A
migration that recomputes them produces a chain that verifies perfectly and
proves nothing: every tampered row would be re-blessed on the way through, and
the one control the product exists to offer -- "this history has not been
edited" -- would have been quietly destroyed by the tool that claims to
preserve it. So the import verifies and ABORTS if the chain is not intact. A
migration that silently launders a broken chain is worse than no migration.

What the payload contains
-------------------------
``app/backend/pg/audit.py`` freezes it::

    prev|at|actor|action|type|id|detail

Note what is NOT in it: ``seq``, ``stream_key``, ``correlation_id``,
``audit_id``. That matters, and it is measured in
``tests/test_migration_audit_chain.py`` rather than asserted here: **the seq a
row is given on the target does not change its hash.** Re-sequencing is
therefore hash-safe, and it is the only reason the strategy below is possible
at all.

The measured contradiction in the obvious strategy
--------------------------------------------------
The instruction this module was written to was: import into ``stream_key =
'LEGACY'`` with ``seq = original audit_id``, hashes verbatim, keeping the
pre-002 rows (which carry ``entry_hash IS NULL``) because "the verifier already
skips them" -- then run the verifier and abort if it is not intact.

Three of those four cannot hold together, and the reason is not a judgement
call. ``app/backend/pg/audit.py::verify_chain`` is a DIFFERENT function from the
POC's ``app/backend/services.py::verify_audit_chain``:

* The POC verifier ``continue``\\ s past ``entry_hash IS NULL``. **The
  PostgreSQL verifier does not.** It recomputes a hash for every row and
  compares it against the stored value, so a NULL ``entry_hash`` fails the
  comparison and reports a break at that seq.
* The PostgreSQL verifier additionally requires ``seq`` to be contiguous from
  1: ``actual_seqs == list(range(1, checked + 1))``. Excluding the unhashed
  rows to satisfy the first point makes ``seq = audit_id`` start at 2, and the
  contiguity check then fails.
* The POC stores ``prev_hash = ''`` for the genesis row; the PostgreSQL
  appender stores ``NULL``. The verifier compares the stored ``prev_hash``
  against an ``expected_prev`` that begins as ``None``, so ``'' != None``
  reports a break at seq 1 -- while the HASH itself is unaffected, because
  ``compute_entry_hash`` does ``prev_hash or ''``.

:data:`STRATEGY_EVIDENCE` records the outcome of every candidate, each one
executed against the real ``verify_chain`` in
``tests/test_migration_audit_chain.py``. Exactly one works, and it is the one
:func:`plan_legacy_import` builds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

#: The stream carrying verifiable POC history.
LEGACY_STREAM = "LEGACY"

#: The stream carrying pre-002 rows, which are history but not evidence.
UNHASHED_STREAM = "LEGACY_UNHASHED"

#: Each entry is (description, expected_intact). Proved in the test module by
#: running app.backend.pg.audit.verify_chain over rows shaped that way.
STRATEGY_EVIDENCE: dict[str, tuple[str, bool]] = {
    "all_rows_prev_empty_string": (
        "every row into LEGACY, seq = audit_id, prev_hash '' kept as ''",
        False),
    "all_rows_prev_null": (
        "every row into LEGACY, seq = audit_id, prev_hash '' -> NULL",
        False),
    "hashed_only_seq_is_audit_id": (
        "hashed rows only into LEGACY, seq = audit_id (leaves a gap at 1)",
        False),
    "hashed_only_resequenced": (
        "hashed rows only into LEGACY, seq re-assigned 1..n, prev_hash '' -> NULL",
        True),
}

#: The one that works.
CHOSEN_STRATEGY = "hashed_only_resequenced"


class AuditChainError(RuntimeError):
    """The chain did not verify. The import is aborted, not reported and continued."""


@dataclass(frozen=True)
class LegacyEntry:
    """One row on its way into ``audit_log``.

    ``source_audit_id`` is carried so the migration report can map every target
    ``seq`` back to the POC row it came from. ``seq`` is not ``audit_id``, and
    an auditor who is told only "the chain is intact" cannot check that claim
    against the source without this mapping.
    """
    stream_key: str
    seq: int
    at: datetime
    actor: str
    action: str
    object_type: str
    object_id: str
    detail: str
    correlation_id: str | None
    prev_hash: str | None
    entry_hash: str | None
    source_audit_id: int

    def as_params(self) -> dict[str, Any]:
        return {
            "stream_key": self.stream_key, "seq": self.seq, "at": self.at,
            "actor": self.actor, "action": self.action,
            "object_type": self.object_type, "object_id": self.object_id,
            "detail": self.detail, "correlation_id": self.correlation_id,
            "prev_hash": self.prev_hash, "entry_hash": self.entry_hash,
        }


def parse_source_at(value: str) -> datetime:
    """The POC ``at`` string as an aware UTC datetime.

    A naive value is assumed UTC because ``services.now()`` writes
    ``datetime.now(timezone.utc)`` and the only naive values in the POC are the
    hand-written seed rows -- all of which are unhashed, so no hash depends on
    the assumption. :func:`hash_survives_round_trip` proves that separately for
    every row that IS hashed, rather than leaving it as an argument.
    """
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None \
        else parsed.astimezone(timezone.utc)


def hash_survives_round_trip(row: dict[str, Any]) -> bool:
    """Would this row still verify after ``at`` becomes a ``timestamptz``?

    The hashed payload contains ``at`` as a STRING. On the POC that string is
    whatever ``services.now()`` wrote. On PostgreSQL ``at`` is a ``timestamptz``
    and ``audit.canonical_at`` re-renders it as
    ``value.astimezone(utc).isoformat()``.

    Those two strings are equal for anything ``services.now()`` produced
    (``isoformat(timespec="seconds")`` with a UTC offset), and UNEQUAL for a
    naive seed value: ``'2026-08-06T09:00:00'`` comes back as
    ``'2026-08-06T09:00:00+00:00'``. A row whose hash covers the first string
    cannot verify once the column is a timestamptz, no matter how faithfully
    the hash itself was copied.

    So this is checked per row BEFORE the import, not discovered by an abort
    afterwards. An unhashed row trivially passes -- there is no hash to break.
    """
    if row.get("entry_hash") is None:
        return True
    original = row["at"]
    if not isinstance(original, str):
        return False
    try:
        round_tripped = parse_source_at(original).isoformat()
    except ValueError:
        return False
    return round_tripped == original


def plan_legacy_import(rows: Iterable[dict[str, Any]]) -> tuple[list[LegacyEntry], dict[str, Any]]:
    """Build the LEGACY and LEGACY_UNHASHED entries, and the report about them.

    `rows` are the exported ``audit_log`` records in ascending ``audit_id``.
    Ascending order is required and CHECKED: the chain's meaning is the order,
    and re-sorting it later would silently rewrite which entry follows which.
    """
    ordered = list(rows)
    ids = [r["audit_id"] for r in ordered]
    if ids != sorted(ids):
        raise AuditChainError(
            "audit rows are not in ascending audit_id order. The chain's meaning "
            "IS the order; re-ordering it changes which entry each prev_hash "
            "refers to.")

    hashed = [r for r in ordered if r.get("entry_hash") is not None]
    unhashed = [r for r in ordered if r.get("entry_hash") is None]

    cannot_round_trip = [r["audit_id"] for r in hashed if not hash_survives_round_trip(r)]
    if cannot_round_trip:
        raise AuditChainError(
            "these hashed rows have an `at` that will not survive becoming a "
            f"timestamptz, so their hash cannot verify on the target: "
            f"{cannot_round_trip}. The hashes are correct; the timestamps are "
            "not round-trippable. Do not 'fix' this by recomputing the hashes.")

    entries: list[LegacyEntry] = []
    for seq, row in enumerate(hashed, start=1):
        prev = row.get("prev_hash")
        entries.append(LegacyEntry(
            stream_key=LEGACY_STREAM,
            seq=seq,
            at=parse_source_at(row["at"]),
            actor=row["actor"], action=row["action"],
            object_type=row["object_type"], object_id=row["object_id"],
            # `detail` is NOT NULL on the target and nullable on the POC. The
            # POC's own hash payload interpolates a NULL as the four characters
            # "None", so an f-string None is what the hash covers -- and only
            # that exact string reproduces it.
            detail="None" if row.get("detail") is None else row["detail"],
            correlation_id=row.get("correlation_id"),
            # '' -> NULL. The hash is unaffected (`prev_hash or ''`), the
            # verifier's expected_prev comparison is not.
            prev_hash=(prev or None),
            entry_hash=row["entry_hash"],
            source_audit_id=row["audit_id"],
        ))
    for seq, row in enumerate(unhashed, start=1):
        entries.append(LegacyEntry(
            stream_key=UNHASHED_STREAM,
            seq=seq,
            at=parse_source_at(row["at"]),
            actor=row["actor"], action=row["action"],
            object_type=row["object_type"], object_id=row["object_id"],
            detail="" if row.get("detail") is None else row["detail"],
            correlation_id=row.get("correlation_id"),
            prev_hash=None, entry_hash=None,
            source_audit_id=row["audit_id"],
        ))

    report = {
        "strategy": CHOSEN_STRATEGY,
        "strategy_rationale": STRATEGY_EVIDENCE[CHOSEN_STRATEGY][0],
        "legacy_stream": LEGACY_STREAM,
        "legacy_entries": len(hashed),
        "unhashed_stream": UNHASHED_STREAM,
        "unhashed_entries": len(unhashed),
        "verifiable_history_begins_at_source_audit_id": hashed[0]["audit_id"] if hashed else None,
        "seq_to_source_audit_id": {
            str(e.seq): e.source_audit_id for e in entries
            if e.stream_key == LEGACY_STREAM},
        "seq_is_not_audit_id": (
            "seq is re-assigned 1..n over the HASHED rows only. The PostgreSQL "
            "verifier requires contiguity from 1 and does not skip unhashed "
            "rows, so seq = audit_id cannot be preserved. seq is not part of "
            "the hashed payload, so re-sequencing changes no hash. The mapping "
            "back to the source audit_id is above."
        ),
        "hashes": "copied verbatim; never recomputed",
    }
    return entries, report


def assert_chain_intact(session, stream_key: str = LEGACY_STREAM) -> dict[str, Any]:
    """Run the product's own verifier and ABORT if it is not intact.

    Imports ``verify_chain`` lazily so this module is usable -- and testable --
    without psycopg installed. Uses the PRODUCT's verifier, never a copy: a
    migration checked by its own reimplementation of the check proves the two
    implementations agree, not that the chain is sound.
    """
    from app.backend.pg.audit import verify_chain

    result = verify_chain(session, stream_key)
    if not result.get("intact"):
        raise AuditChainError(
            f"verify_chain({stream_key!r}) reported intact=False: {result}. The "
            "migration is aborted and the transaction rolled back. Do NOT "
            "recompute the hashes to make this pass -- a chain that verifies "
            "because it was re-blessed on the way through proves nothing."
        )
    return result


def unhashed_row_count(rows: Sequence[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("entry_hash") is None)
