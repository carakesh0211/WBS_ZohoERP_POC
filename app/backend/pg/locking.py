"""Ordered ancestor-chain locking for budget-owning cells.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``,
section "Locking -- the whole ancestor chain, in order", for the rule this
module implements. Summarised: locking only the nearest budget-owning
ancestor of a spend is **not sufficient**, because exposure rolls up the
entire WBS tree -- a spend deep in the hierarchy reduces availability at
*every* budget-owning ancestor above it, not just the closest one. Missing
that was a real correctness hole in an earlier version of this product.

For affected cells ``A = {(w1,h1) .. (wn,hn)}``, the lock set is::

    L = union over (w,h) in A of
          { (a,h) : a is ancestor-or-self of w AND budget_paise(a,h) != 0 }

acquired with ``SELECT ... FOR UPDATE ORDER BY wbs_path, budget_head_id`` -- a
total order over every cell any concurrent transaction could ever want to
lock, so two transactions racing different affected sets can never deadlock
against each other, provided both obey the two rules below.

Rules that make the deadlock-freedom proof hold, and that every mutating
service function must not break:

1. Call :func:`lock_affected_cells` **exactly once**, as the **first** locking
   action of the function, with the **complete** affected set. Locking a
   partial set and then locking more cells later reintroduces the possibility
   of two transactions acquiring overlapping locks in different orders.
2. No cell lock is acquired after this call.
3. Global lock order across an entire mutating function is: (1) cells via
   this function, (2) the document row via its own ``FOR UPDATE``, (3)
   :func:`advisory_audit_lock` for the audit stream, taken last.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from .engine import Session

#: The real, atomic, ordered lock query. One round trip: it walks every
#: ancestor-or-self of each affected wbs_id (via `wbs_element.wbs_path`),
#: keeps only the ones that own budget for the paired head
#: (`budget_control_cell.budget_paise <> 0`), and locks exactly that set in
#: `wbs_path, budget_head_id` order. `unnest($1::text[], $2::text[])` pairs the
#: two arrays positionally, so this is the exact per-(wbs,head) union the
#: domain rule specifies -- never a cross product of every wbs id against
#: every head.
_LOCK_SQL = """
    SELECT DISTINCT a.wbs_id, aff.head_id, a.wbs_path
    FROM unnest(%(wbs_ids)s::text[], %(heads)s::text[]) AS aff(wbs_id, head_id)
    JOIN wbs_element w ON w.wbs_id = aff.wbs_id
    JOIN wbs_element a ON w.wbs_path <@ a.wbs_path
    JOIN budget_control_cell bc
        ON bc.wbs_id = a.wbs_id AND bc.budget_head_id = aff.head_id
    WHERE bc.budget_paise <> 0
    ORDER BY a.wbs_path, bc.budget_head_id
    FOR UPDATE OF bc
"""


def lock_affected_cells(session: Session,
                         affected: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Lock every budget-owning ancestor cell on every affected chain.

    `affected` is the complete set of (wbs_id, budget_head_id) pairs the
    mutation is about to touch -- typically the line items of the document
    being written. Returns the actual lock set `L` taken, in acquisition
    order, and appends the same list (in the same order) to
    `session.locks_taken` so a test can assert the ordering directly rather
    than trusting a code review.

    Cells with `budget_paise = 0` carry no availability and are correctly
    excluded -- they are not "owning" ancestors, so no concurrent mutation can
    race on them through this cell.

    An empty `affected` list is a no-op: nothing to lock, nothing recorded.
    """
    if not affected:
        return []

    wbs_ids = [wbs_id for wbs_id, _head in affected]
    heads = [head for _wbs_id, head in affected]

    rows = session.fetchall(_LOCK_SQL, {"wbs_ids": wbs_ids, "heads": heads})
    locked = [(row[0], row[1]) for row in rows]
    session.locks_taken.extend(locked)
    return locked


def advisory_audit_lock(session: Session, stream_key: str) -> None:
    """Take the per-stream advisory lock used to serialise audit chain writes.

    Must be taken **after** all cell locks and the document row lock (see the
    module docstring's global lock order), and, per `audit.py`, before the
    caller reads `prev_hash` for that stream -- otherwise two concurrent
    writers to the same stream could both read the same `prev_hash` and each
    compute a hash chaining from it, corrupting the chain.

    `hashtext()` maps the stream key to a 32-bit int, which PostgreSQL
    implicitly widens to the `bigint` `pg_advisory_xact_lock(bigint)` expects.
    The lock is transaction-scoped: it releases automatically on commit or
    rollback, exactly like the cell locks taken above.
    """
    session.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (stream_key,))


# ============================================================================
# Pure logic -- no database required.
#
# `_LOCK_SQL` above is the source of truth for what actually gets locked; this
# function re-derives the same rule (the domain-controls.md formula) from a
# plain-Python ancestor-chain description, so the *rule itself* -- which cells
# belong in the lock set, and in what order -- can be unit tested without a
# live PostgreSQL connection. Keep the two in step: a change to the locking
# rule belongs in both, and `tests/test_pg_locking.py` should exercise them
# against the same scenarios.
# ============================================================================
def derive_lock_set(
    affected: Iterable[tuple[str, str]],
    ancestor_chains: Mapping[str, list[tuple[str, str]]],
    budget_paise: Mapping[tuple[str, str], int],
) -> list[tuple[str, str]]:
    """Compute `L` from plain Python data, mirroring `_LOCK_SQL`.

    `ancestor_chains[w]` is the ordered list of `(ancestor_wbs_id, wbs_path)`
    for every ancestor-or-self of `w`, root-first (so `wbs_path` values sort
    ascending in the same order the list is given in -- callers building test
    fixtures should supply them that way, matching how `ltree` paths compare).

    `budget_paise[(a, h)]` is the stored `budget_paise` for control cell
    `(a, h)`; a missing key is treated as `0` (not budget-owning), matching
    the database default.

    Returns the deduplicated lock set ordered by `(wbs_path, budget_head_id)`,
    exactly as `ORDER BY wbs_path, budget_head_id` would.
    """
    seen: dict[tuple[str, str], str] = {}   # (ancestor, head) -> wbs_path, for ordering
    for wbs_id, head in affected:
        for ancestor_id, wbs_path in ancestor_chains.get(wbs_id, []):
            if budget_paise.get((ancestor_id, head), 0) == 0:
                continue
            seen[(ancestor_id, head)] = wbs_path

    return [pair for pair, _path in
            sorted(seen.items(), key=lambda item: (item[1], item[0][1]))]
