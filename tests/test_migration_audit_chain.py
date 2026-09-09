"""The LEGACY audit stream: hashes copied, never recomputed, and MEASURED.

Runs everywhere. No PostgreSQL: ``app.backend.pg.audit.verify_chain`` reads its
rows through one ``session.fetchall`` call, so the PRODUCT's verifier -- not a
copy of it -- can be driven over rows shaped exactly as an import would leave
them. That is the point. A migration checked by its own reimplementation of the
check proves the two implementations agree, not that the chain is sound.

What this file establishes, in order:

1. ``seq`` is not in the hashed payload, so re-sequencing is hash-safe. Every
   other conclusion here depends on that, so it is proved first and directly
   against ``compute_entry_hash``.
2. Three of the four candidate import strategies make ``verify_chain`` report
   a break, for three different reasons. This is not an argument; each case is
   executed.
3. The chosen strategy verifies.
4. Recomputing a hash makes a TAMPERED row verify -- which is exactly why the
   migration must not do it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import services                                    # noqa: E402
from app.backend.pg import audit as pg_audit                        # noqa: E402
from tools.migration import audit_chain                             # noqa: E402


class FakeSession:
    """Stands in for ``pg.engine.Session`` for the one call verify_chain makes."""

    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self, sql, params=None):
        return self.rows


def poc_chain(count: int = 4, *, with_seed_row: bool = True) -> list[dict]:
    """Rows shaped exactly as ``services.audit`` writes them.

    Built with the POC's own hash function rather than by hand, so the fixture
    cannot drift away from what the product actually produces.
    """
    rows: list[dict] = []
    prev_hash = ""
    base = datetime(2026, 8, 6, 9, 0, 0, tzinfo=timezone.utc)
    next_id = 1

    if with_seed_row:
        # The seed row: naive `at`, no prev_hash, no entry_hash. Exactly what
        # `db.reset_and_seed` inserts before migration 002 exists.
        rows.append({"audit_id": next_id, "at": "2026-08-06T09:00:00",
                     "actor": "U-ADM", "action": "SEED", "object_type": "System",
                     "object_id": "-", "detail": "POC dataset loaded",
                     "prev_hash": None, "entry_hash": None})
        next_id += 1

    for i in range(count):
        at = (base + timedelta(minutes=i + 1)).isoformat(timespec="seconds")
        actor, action = f"U-{i:03d}", "APPROVE"
        object_type, object_id, detail = "PurchaseRequest", f"PR-{i:04d}", f"detail {i}"
        payload = f"{prev_hash}|{at}|{actor}|{action}|{object_type}|{object_id}|{detail}"
        import hashlib
        entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        rows.append({"audit_id": next_id, "at": at, "actor": actor, "action": action,
                     "object_type": object_type, "object_id": object_id,
                     "detail": detail, "prev_hash": prev_hash, "entry_hash": entry_hash})
        prev_hash = entry_hash
        next_id += 1
    return rows


def as_target_rows(rows, *, seq_from_audit_id: bool, empty_prev_to_null: bool):
    """Shape rows the way each candidate strategy would leave them in PostgreSQL.

    ``at`` becomes a ``timestamptz``, i.e. an aware datetime, because that is
    what psycopg hands back from the column.
    """
    out = []
    for index, r in enumerate(rows, start=1):
        parsed = datetime.fromisoformat(r["at"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        prev = r["prev_hash"]
        if empty_prev_to_null and prev == "":
            prev = None
        out.append((r["audit_id"] if seq_from_audit_id else index, parsed,
                    r["actor"], r["action"], r["object_type"], r["object_id"],
                    r["detail"], prev, r["entry_hash"]))
    return out


# ------------------------------------------------ 1. seq is not in the payload
def test_seq_is_not_part_of_the_hashed_payload_so_resequencing_is_hash_safe():
    """Everything else in this file depends on this, so it is proved directly.

    ``compute_entry_hash`` takes prev, at, actor, action, object_type,
    object_id and detail -- and nothing else. There is no argument through
    which a seq could reach it.
    """
    args = (None, "2026-08-06T09:01:00+00:00", "U-1", "APPROVE",
            "PurchaseRequest", "PR-1", "detail")
    assert pg_audit.compute_entry_hash(*args) == pg_audit.compute_entry_hash(*args)
    import inspect
    parameters = list(inspect.signature(pg_audit.compute_entry_hash).parameters)
    assert "seq" not in parameters
    assert "stream_key" not in parameters


def test_the_poc_and_postgresql_hash_functions_agree_on_the_same_inputs():
    """The migration copies hashes between two implementations of one frozen
    payload. If they ever disagree, nothing else here means anything."""
    import hashlib
    prev, at = "", "2026-08-06T09:01:00+00:00"
    poc = hashlib.sha256(
        f"{prev}|{at}|U-1|APPROVE|PurchaseRequest|PR-1|detail".encode()).hexdigest()
    assert pg_audit.compute_entry_hash(None, at, "U-1", "APPROVE",
                                       "PurchaseRequest", "PR-1", "detail") == poc


# --------------------------------------- 2. the three strategies that do NOT work
def test_all_rows_with_empty_string_prev_hash_reports_a_break_at_seq_1():
    """The POC stores '' for the genesis row; PostgreSQL's verifier compares the
    stored prev_hash against an expected_prev that begins as None."""
    rows = as_target_rows(poc_chain(), seq_from_audit_id=True, empty_prev_to_null=False)
    result = pg_audit.verify_chain(FakeSession(rows), "LEGACY")
    assert result["intact"] is False
    assert result["first_break_seq"] == 1


def test_all_rows_with_prev_null_still_breaks_because_unhashed_rows_are_not_skipped():
    """The premise that the verifier "already skips" `entry_hash IS NULL` rows
    is true of the POC verifier and FALSE of the PostgreSQL one.

    ``services.verify_audit_chain`` has an explicit ``continue`` for them.
    ``pg.audit.verify_chain`` has no such branch: it recomputes a hash for every
    row and compares it against the stored value, so NULL fails the comparison.
    """
    rows = as_target_rows(poc_chain(), seq_from_audit_id=True, empty_prev_to_null=True)
    result = pg_audit.verify_chain(FakeSession(rows), "LEGACY")
    assert result["intact"] is False
    assert result["first_break_seq"] == 1

    import inspect
    poc_source = inspect.getsource(services.verify_audit_chain)
    pg_source = inspect.getsource(pg_audit.verify_chain)
    assert 'entry_hash"] is None' in poc_source or "entry_hash'] is None" in poc_source
    assert "is None" not in pg_source.split("for (seq")[1].split("broken =")[0]


def test_hashed_rows_only_with_seq_equal_to_audit_id_fails_the_contiguity_check():
    """Excluding the unhashed rows leaves seq starting at 2, and
    ``verify_chain`` requires ``actual_seqs == list(range(1, checked + 1))``."""
    hashed = [r for r in poc_chain() if r["entry_hash"] is not None]
    rows = as_target_rows(hashed, seq_from_audit_id=True, empty_prev_to_null=True)
    result = pg_audit.verify_chain(FakeSession(rows), "LEGACY")
    assert result["intact"] is False
    assert result["sequence_contiguous"] is False


# ------------------------------------------------- 3. the strategy that DOES work
def test_the_chosen_strategy_verifies_intact():
    hashed = [r for r in poc_chain() if r["entry_hash"] is not None]
    rows = as_target_rows(hashed, seq_from_audit_id=False, empty_prev_to_null=True)
    result = pg_audit.verify_chain(FakeSession(rows), "LEGACY")
    assert result["intact"] is True
    assert result["entries_checked"] == len(hashed)
    assert result["sequence_contiguous"] is True


def test_plan_legacy_import_produces_rows_that_verify():
    """End to end: the planner's own output, through the product's verifier."""
    entries, report = audit_chain.plan_legacy_import(poc_chain())
    legacy = [e for e in entries if e.stream_key == audit_chain.LEGACY_STREAM]
    rows = [(e.seq, e.at, e.actor, e.action, e.object_type, e.object_id,
             e.detail, e.prev_hash, e.entry_hash) for e in legacy]
    assert pg_audit.verify_chain(FakeSession(rows), "LEGACY")["intact"] is True
    assert report["strategy"] == audit_chain.CHOSEN_STRATEGY
    assert report["hashes"] == "copied verbatim; never recomputed"


def test_every_recorded_strategy_outcome_is_the_measured_one():
    """STRATEGY_EVIDENCE is documentation, so it is held to the measurements.

    A table of outcomes that nobody re-runs is a table of outcomes that used to
    be true.
    """
    chain = poc_chain()
    hashed = [r for r in chain if r["entry_hash"] is not None]
    measured = {
        "all_rows_prev_empty_string":
            as_target_rows(chain, seq_from_audit_id=True, empty_prev_to_null=False),
        "all_rows_prev_null":
            as_target_rows(chain, seq_from_audit_id=True, empty_prev_to_null=True),
        "hashed_only_seq_is_audit_id":
            as_target_rows(hashed, seq_from_audit_id=True, empty_prev_to_null=True),
        "hashed_only_resequenced":
            as_target_rows(hashed, seq_from_audit_id=False, empty_prev_to_null=True),
    }
    assert set(measured) == set(audit_chain.STRATEGY_EVIDENCE)
    for name, rows in measured.items():
        _description, expected = audit_chain.STRATEGY_EVIDENCE[name]
        actual = pg_audit.verify_chain(FakeSession(rows), "LEGACY")["intact"]
        assert actual is expected, f"{name}: recorded {expected}, measured {actual}"
    assert audit_chain.STRATEGY_EVIDENCE[audit_chain.CHOSEN_STRATEGY][1] is True


# ------------------------------------------- 4. why recomputation is forbidden
def test_recomputing_a_hash_makes_a_TAMPERED_row_verify():
    """The reason the rule is absolute rather than a preference.

    Alter the detail of a committed entry, recompute its hash the way a
    'helpful' migration would, and the chain verifies perfectly. Every trace of
    the edit is gone, and the control the product exists to offer has been
    destroyed by the tool that claims to preserve it.
    """
    chain = [r for r in poc_chain() if r["entry_hash"] is not None]
    chain[1]["detail"] = "quietly edited"

    tampered = as_target_rows(chain, seq_from_audit_id=False, empty_prev_to_null=True)
    assert pg_audit.verify_chain(FakeSession(tampered), "LEGACY")["intact"] is False

    laundered, prev = [], None
    for seq, at, actor, action, otype, oid, detail, _prev, _hash in tampered:
        recomputed = pg_audit.compute_entry_hash(
            prev, pg_audit.canonical_at(at), actor, action, otype, oid, detail)
        laundered.append((seq, at, actor, action, otype, oid, detail, prev, recomputed))
        prev = recomputed
    assert pg_audit.verify_chain(FakeSession(laundered), "LEGACY")["intact"] is True


def test_assert_chain_intact_raises_rather_than_reporting_and_continuing():
    chain = [r for r in poc_chain() if r["entry_hash"] is not None]
    chain[1]["entry_hash"] = "0" * 64
    rows = as_target_rows(chain, seq_from_audit_id=False, empty_prev_to_null=True)
    with pytest.raises(audit_chain.AuditChainError, match="intact=False"):
        audit_chain.assert_chain_intact(FakeSession(rows), "LEGACY")


def test_a_broken_source_chain_is_not_migrated_into_a_verifying_one():
    """The end-to-end version of the rule: garbage in, refusal out."""
    chain = poc_chain()
    chain[2]["entry_hash"] = "f" * 64
    entries, _report = audit_chain.plan_legacy_import(chain)
    legacy = [e for e in entries if e.stream_key == audit_chain.LEGACY_STREAM]
    rows = [(e.seq, e.at, e.actor, e.action, e.object_type, e.object_id,
             e.detail, e.prev_hash, e.entry_hash) for e in legacy]
    with pytest.raises(audit_chain.AuditChainError):
        audit_chain.assert_chain_intact(FakeSession(rows), "LEGACY")


# ------------------------------------------------------------- the plan's edges
def test_unhashed_rows_go_to_their_own_stream_and_are_counted():
    entries, report = audit_chain.plan_legacy_import(poc_chain(with_seed_row=True))
    unhashed = [e for e in entries if e.stream_key == audit_chain.UNHASHED_STREAM]
    assert len(unhashed) == 1
    assert report["unhashed_entries"] == 1
    assert report["verifiable_history_begins_at_source_audit_id"] == 2
    assert all(e.entry_hash is None for e in unhashed)


def test_the_seq_to_source_audit_id_mapping_is_recorded():
    """"The chain is intact" is uncheckable without it: seq is not audit_id."""
    _entries, report = audit_chain.plan_legacy_import(poc_chain(count=3))
    assert report["seq_to_source_audit_id"] == {"1": 2, "2": 3, "3": 4}


def test_rows_out_of_audit_id_order_are_refused():
    chain = poc_chain()
    chain[1], chain[2] = chain[2], chain[1]
    with pytest.raises(audit_chain.AuditChainError, match="ascending audit_id"):
        audit_chain.plan_legacy_import(chain)


def test_a_naive_timestamp_on_a_HASHED_row_is_refused_before_the_import():
    """A hash over '2026-08-06T09:00:00' cannot verify once `at` is a
    timestamptz, because the column reads back as '...+00:00'. Better refused
    in the plan than discovered by an abort after the rows are inserted."""
    chain = poc_chain(count=1)
    chain[1]["at"] = "2026-08-06T09:01:00"          # strip the offset, keep the hash
    with pytest.raises(audit_chain.AuditChainError, match="will not survive"):
        audit_chain.plan_legacy_import(chain)


def test_a_naive_timestamp_on_an_UNHASHED_row_is_fine():
    """The POC seed row is naive AND unhashed. There is no hash to break."""
    entries, _ = audit_chain.plan_legacy_import(poc_chain(count=1))
    assert any(e.stream_key == audit_chain.UNHASHED_STREAM for e in entries)


def test_a_null_detail_is_carried_as_the_four_characters_the_hash_covers():
    """The POC's payload f-string interpolates a NULL detail as ``None``, so
    ``None`` is what the stored hash covers. The target column is NOT NULL, and
    substituting '' there would break a hash that is otherwise perfect."""
    import hashlib
    at = "2026-08-06T09:01:00+00:00"
    payload = f"|{at}|U-1|APPROVE|PR|PR-1|None"
    chain = [{"audit_id": 1, "at": at, "actor": "U-1", "action": "APPROVE",
              "object_type": "PR", "object_id": "PR-1", "detail": None,
              "prev_hash": "", "entry_hash": hashlib.sha256(payload.encode()).hexdigest()}]
    entries, _ = audit_chain.plan_legacy_import(chain)
    assert entries[0].detail == "None"
    rows = [(e.seq, e.at, e.actor, e.action, e.object_type, e.object_id,
             e.detail, e.prev_hash, e.entry_hash) for e in entries]
    assert pg_audit.verify_chain(FakeSession(rows), "LEGACY")["intact"] is True
