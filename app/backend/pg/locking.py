"""Ordered ancestor-chain locking for budget control cells.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``,
section "Locking -- the whole ancestor chain, in order", for the rule this
module implements. Summarised: locking only the nearest budget-owning
ancestor of a spend is **not sufficient**, because exposure rolls up the
entire WBS tree -- a spend deep in the hierarchy reduces availability at
*every* budget-owning ancestor above it, not just the closest one. Missing
that was a real correctness hole in an earlier version of this product.

For affected cells ``A = {(w1,h1) .. (wn,hn)}``, the lock set is::

    L = union over (w,h) in A of
          { (a,h) : a is ancestor-or-self of w
                    AND a budget_control_cell row exists for (a,h) }

acquired with ``SELECT ... FOR UPDATE ORDER BY wbs_path, budget_head_id`` -- a
total order over every cell any concurrent transaction could ever want to
lock, so two transactions racing different affected sets can never deadlock
against each other, provided both obey the rules below.

Membership in ``L`` does **not** depend on ``budget_paise``
----------------------------------------------------------
This filtered on ``budget_paise <> 0`` until Wave 3, on the reasoning that a
zero-budget cell "carries no availability to breach". That reasoning was
correct about *availability* and wrong about *locking*, in two ways:

* ``recompute_cell`` writes the affected cell's own row with
  ``UPDATE budget_control_cell ... WHERE wbs_id = ... AND budget_head_id = ...``.
  An ``UPDATE`` takes its target row's exclusive lock as part of executing,
  pre-locked or not. A cell receiving its first-ever money has
  ``budget_paise = 0`` at lock time, so the old filter excluded from ``L``
  precisely the row the very next statement was about to lock. Rule 2 below
  was therefore false as written, and this module said so nowhere.
* The predicate was evaluated against a value another transaction can change.
  A cell that owns nothing now is a cell some concurrent ``record_original``
  or ``approve_transfer`` is about to make budget-owning. Deriving the lock
  set from mutable data means two transactions can compute *different* lock
  sets for the same chain. Membership now depends only on the shape of the
  WBS tree and on which ``(wbs_id, budget_head_id)`` rows exist -- neither of
  which any of these mutations changes -- so the set is stable under
  concurrency.

``L`` is consequently a superset of the old one. It is never smaller, so the
availability argument the old filter served is unaffected; it is sometimes
larger, and the extra members are exactly the rows a mutation writes.

Rules that make the deadlock-freedom proof hold, and that every mutating
service function must not break:

1. Call :func:`lock_affected_cells` **exactly once**, as the **first** locking
   action of the function, with the **complete** affected set. Locking a
   partial set and then locking more cells later reintroduces the possibility
   of two transactions acquiring overlapping locks in different orders.
2. No cell lock is acquired after this call, **including implicitly**. This is
   the rule that has to be read carefully, because most of the cell locks this
   application takes are never written down as a ``FOR UPDATE``:

   * Every ``UPDATE budget_control_cell`` takes that row's lock. Such a
     statement is legal only for a row already in ``L`` -- which, since ``L``
     contains every ancestor-or-self cell that exists, means only for a cell
     on a chain that was declared to :func:`lock_affected_cells`. It is not a
     *later* lock, because the row is already held; the statement re-enters a
     lock this transaction owns.
   * Every ``UPDATE budget_ledger_cell`` likewise takes that row's lock, and
     ``budget_ledger_cell`` is **not** in ``L`` -- ``L`` is a set of
     ``budget_control_cell`` rows. That is safe, and the reason is structural
     rather than incidental: ``fk_ledger_control_cell`` (migration 002) makes
     a ledger row's existence imply its control row's existence, and every
     writer of a ledger row holds that control row's lock for the duration.
     Ledger locks are therefore always acquired *underneath* the full control
     lock set, by a transaction no other transaction can be interleaved with
     on those cells, so they inherit the control order and add no new edge to
     the wait-for graph. They do not need their own ordering, and they are not
     recorded in ``locks_taken``.
   * A cell with no ``budget_control_cell`` row is in no lock set and is also
     written by nothing: ``recompute_cell``'s ``UPDATE`` matches zero rows and
     takes zero locks. The two agree by construction.

   No other statement in this application takes a cell lock.
3. Global lock order across an entire mutating function is: (1) cells via
   this function, (2) the document row via its own ``FOR UPDATE``, (3)
   :func:`advisory_audit_lock` for the audit stream, taken last.

What is proven, and where
-------------------------
Rules 1 and 2 are checked statically, with no database, by
``tests/test_pg_locking_order.py``, which walks the AST of ``budget.py`` and
``periods.py``. The blocking behaviour, the zero-budget cell actually being
locked, and the absence of deadlock under concurrent load need a real server
and live in ``tests/test_pg_period_concurrency.py``. No deadlock has ever been
demonstrated against this module; the defect this rewrite closes was that the
invariant overclaimed, not that it was observed to fail.
"""
from __future__ import annotations

from typing import Container, Iterable, Mapping

from .engine import Session

#: The real, atomic, ordered lock query. One round trip: it walks every
#: ancestor-or-self of each affected wbs_id (via `wbs_element.wbs_path`), keeps
#: every one of them that has a control cell for the paired head, and locks
#: exactly that set in `wbs_path, budget_head_id` order.
#: `unnest($1::text[], $2::text[])` pairs the two arrays positionally, so this
#: is the exact per-(wbs,head) union the domain rule specifies -- never a cross
#: product of every wbs id against every head.
#:
#: There is deliberately NO `budget_paise <> 0` predicate here. See the module
#: docstring: that filter excluded the very rows `recompute_cell` was about to
#: UPDATE, and made lock-set membership depend on a value a concurrent
#: transaction can change. The JOIN to `budget_control_cell` is now the only
#: membership test, and it asks a question no mutation in this application
#: alters: does this cell exist?
#:
#: The de-duplication happens in a CTE, NOT in the locking SELECT, because
#: PostgreSQL rejects `SELECT DISTINCT ... FOR UPDATE` outright:
#:
#:     ERROR:  FOR UPDATE is not allowed with DISTINCT clause
#:
#: A locking clause requires every returned row to map to one identifiable
#: table row, and DISTINCT destroys that mapping. The first version of this
#: query combined the two and would have failed on its first contact with a
#: real database -- the two tests that would have caught it are precisely the
#: ones skipped when no PostgreSQL is available locally. The outer query
#: therefore locks plain rows of `budget_control_cell`, ordered by the path
#: carried out of the CTE.
_LOCK_SQL = """
    WITH affected(wbs_id, head_id) AS (
        SELECT * FROM unnest(%(wbs_ids)s::text[], %(heads)s::text[])
    ),
    target AS (
        SELECT DISTINCT a.wbs_id AS lock_wbs_id,
                        aff.head_id AS lock_head_id,
                        a.wbs_path AS lock_path
        FROM affected aff
        JOIN wbs_element w ON w.wbs_id = aff.wbs_id
        JOIN wbs_element a ON w.wbs_path <@ a.wbs_path
        JOIN budget_control_cell bc
            ON bc.wbs_id = a.wbs_id AND bc.budget_head_id = aff.head_id
    )
    SELECT bc.wbs_id, bc.budget_head_id
    FROM budget_control_cell bc
    JOIN target t
        ON t.lock_wbs_id = bc.wbs_id AND t.lock_head_id = bc.budget_head_id
    ORDER BY t.lock_path, bc.budget_head_id
    FOR UPDATE OF bc
"""


def lock_affected_cells(session: Session,
                         affected: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Lock every existing ancestor-or-self cell on every affected chain.

    `affected` is the complete set of (wbs_id, budget_head_id) pairs the
    mutation is about to touch -- typically the line items of the document
    being written. Returns the actual lock set `L` taken, in acquisition
    order, and appends the same list (in the same order) to
    `session.locks_taken` so a test can assert the ordering directly rather
    than trusting a code review.

    Cells with `budget_paise = 0` **are** locked. They own no availability,
    but they are rows this transaction may write and rows a concurrent
    transaction may turn into budget owners, and both of those are reasons to
    hold them. See the module docstring for why the old `budget_paise <> 0`
    filter made rule 2 false.

    An affected pair with no `budget_control_cell` row contributes nothing --
    correctly, since there is no row to lock and nothing will write one.
    `L` can therefore be smaller than `affected`, and a caller that must not
    proceed without holding a particular cell should compare the two rather
    than assume (`periods._roll_cells_for_entity` does exactly that).

    An empty `affected` list is a no-op: nothing to lock, nothing recorded.
    Because `locks_taken` is what the ordering assertions read, this is the
    one case where those assertions pass vacuously -- so callers that must
    lock something check that they did, rather than leaving an empty list to
    satisfy an ordering test by default.
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
# rule belongs in both, and `tests/test_pg_locking.py` and
# `tests/test_pg_locking_order.py` should exercise them against the same
# scenarios.
# ============================================================================
def derive_lock_set(
    affected: Iterable[tuple[str, str]],
    ancestor_chains: Mapping[str, list[tuple[str, str]]],
    control_cells: Container[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Compute `L` from plain Python data, mirroring `_LOCK_SQL`.

    `ancestor_chains[w]` is the ordered list of `(ancestor_wbs_id, wbs_path)`
    for every ancestor-or-self of `w`, root-first (so `wbs_path` values sort
    ascending in the same order the list is given in -- callers building test
    fixtures should supply them that way, matching how `ltree` paths compare).

    `control_cells` is the set of `(wbs_id, budget_head_id)` pairs that have a
    `budget_control_cell` row: membership, tested with `in`, mirrors
    `_LOCK_SQL`'s `JOIN budget_control_cell`. Any container works -- a set of
    pairs, or a mapping keyed by pair (a `{pair: budget_paise}` dict reads
    naturally and is what the older fixtures pass).

    **The stored `budget_paise` is not consulted.** Before Wave 3 this argument
    was a `{pair: budget_paise}` mapping whose *values* decided membership,
    with zero meaning "not locked". A zero-budget cell that exists is now
    locked like any other; only a cell with no row at all is excluded. Fixtures
    that expressed "does not own budget" by omitting the key still describe a
    non-lockable cell and still behave the same; fixtures that expressed it as
    an explicit `0` now describe a lockable one.

    Returns the deduplicated lock set ordered by `(wbs_path, budget_head_id)`,
    exactly as `ORDER BY wbs_path, budget_head_id` would.
    """
    seen: dict[tuple[str, str], str] = {}   # (ancestor, head) -> wbs_path, for ordering
    for wbs_id, head in affected:
        for ancestor_id, wbs_path in ancestor_chains.get(wbs_id, []):
            if (ancestor_id, head) not in control_cells:
                continue
            seen[(ancestor_id, head)] = wbs_path

    return [pair for pair, _path in
            sorted(seen.items(), key=lambda item: (item[1], item[0][1]))]
