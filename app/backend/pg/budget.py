"""Budget planning service: lines, revisions, transfers, availability, versions.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``
("The control cell", "Availability", "Budget-owning ancestor", "Locking",
"Re-check at write time") and ``docs/WAVE2_CONTRACTS.md`` (the frozen API
shape this module's callers, ``app/backend/api/budget.py``, are built to
match exactly).

Design decisions worth making explicit for a reviewer:

* ``budget_control_cell.budget_paise`` and ``budget_ledger_cell.{original,
  revisions,future_budget}_paise`` are DERIVED from ``budget_line`` history --
  :func:`recompute_cell` is the one place that writes them, and it always
  recomputes fully from source rows rather than adjusting incrementally, so
  it is naturally idempotent (a requirement the periods.py roll depends on).
* ``available``/``exposure`` for a spend location are **subtree sums rooted
  at the nearest budget-owning ancestor** -- matching
  ``app.backend.domain.compute_ledger``'s ``head_totals`` exactly (both
  "budget" and "exposure" are rolled up over the owner's whole subtree, not
  just the owner's own row), computed here via an ``ltree`` scan instead of
  an in-memory tree walk. Never persisted.
* The frozen contract in ``docs/WAVE2_CONTRACTS.md`` documents only
  *creation* of a revision/transfer (``POST .../revisions``,
  ``POST .../transfers``, both returning ``status: "DRAFT"``). Domain-controls.md
  is explicit that "a revision creates no spending capacity until approved",
  which requires an approval step to exist somewhere. This module (and its
  router) therefore ADD ``.../{id}/approve`` and ``.../{id}/reject``, beyond
  the frozen contract text -- additive, not a conflict with anything a
  frontend coding against the documented shape would already be doing.
  Flagged for the lead in the delivery report.
* Wave 4 adds ``submit_revision`` / ``submit_transfer`` (and
  ``POST .../{id}/submit``), which raise a DRAFT into the configurable
  approval engine. Before them ``pg/approvals.open_instance`` was called from
  tests and from nowhere else, so the whole engine decided nothing in
  production. ``approval_writeback.apply_outcome`` is the other end: it calls
  ``approve_revision``/``reject_revision`` here rather than reimplementing
  them, which is why the maker-checker refusal, the ordered lock, the
  availability re-check and the effective-dated ``budget_line`` append cannot
  drift into a second copy. ``_assert_not_under_approval`` stops the direct
  ``approve``/``reject`` route being used to step around a live instance.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

from .. import domain
from . import audit as audit_mod
from . import repo
from .engine import Session
from .locking import lock_affected_cells

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Configuration, not literals -- domain-controls.md, "Availability" /
#: WAVE2_CONTRACTS.md: "verdict thresholds come from domain.WATCH_PCT /
#: CRITICAL_PCT". Read from the frozen formula registry, never redefined here.
WATCH_PCT = domain.WATCH_PCT
CRITICAL_PCT = domain.CRITICAL_PCT

# Every dimension is mapped through the joined `project` row, so none is
# waived. The waived form -- entity/plant/location = None, copied from
# WBS_ELEMENT_SCOPE_COLUMNS -- is correct for `wbs_element`, which carries no
# org columns and is not reachable from one. It is NOT correct here: these
# queries already join out to a row that has entity_id, plant_id and
# location_id, so waiving them meant a caller restricted to one entity, and
# not otherwise restricted by project, read every entity's budget.
#
# `compile_scope` treats an explicit None as "waived", not as "refuse" -- the
# refusal is only for a dimension the mapping omits entirely -- so the waiver
# widened silently rather than failing closed.
_CELL_SCOPE_COLUMNS = {
    "project": "w.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}
_PROJECT_SCOPE_COLUMNS = {
    "project": "p.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}


class BudgetServiceError(Exception):
    """Raised for every rejected budget-service call.

    ``code`` is the RFC-7807 ``code`` field the API contract requires;
    ``status`` is the HTTP status the router should answer with. Callers
    match on ``.code``, never on the message text.
    """

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400) -> None:
    raise BudgetServiceError(code, message, status=status)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ============================================================ cursor helpers
def _encode_cursor(path_text: str, head_id: str) -> str:
    raw = f"{path_text}\x1f{head_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        path_text, head_id = raw.split("\x1f", 1)
        return path_text, head_id
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise BudgetServiceError(
            "INVALID_CURSOR", f"cursor {cursor!r} could not be decoded") from exc


# ==========================================================================
# Derived position: recompute from budget_line history
# ==========================================================================
def recompute_cell(session: Session, wbs_id: str, budget_head_id: str, *,
                    as_of: date | None = None, actor: str = "SYSTEM") -> dict:
    """Recompute a cell's stored position from ``budget_line`` history.

    Source of truth: every ``Approved`` ``budget_line`` row for
    ``(wbs_id, budget_head_id)``. A row counts toward the CURRENT bucket when
    ``effective_from <= as_of`` (default: today); otherwise it counts toward
    FUTURE, regardless of ``kind`` -- an ORIGINAL grant dated ahead is exactly
    as "not yet spendable" as a future-dated revision (domain-controls.md,
    "Fiscal periods": ``budget_paise`` depends on ``effective_date <=
    today()``). ORIGINAL rows sum into ``original_paise``; everything else
    (REVISION, TRANSFER) sums into ``revisions_paise`` -- the same
    ORIGINAL-vs-everything-else split ``app.backend.domain.compute_ledger``
    uses, kept identical here so the two engines agree on what "the current
    approved budget" means.

    Locking. Callers must call
    :func:`~app.backend.pg.locking.lock_affected_cells` for the affected
    ancestor chain before calling this, and must have passed
    ``(wbs_id, budget_head_id)`` itself in the affected set (every mutating
    function in this module and in ``periods.py`` does). Both ``UPDATE``
    statements below take their target row's exclusive lock as part of
    executing -- an ``UPDATE`` always does, subquery included, whether or not
    the row was pre-locked. Say that plainly rather than call it "no
    additional lock":

    * ``budget_control_cell`` for ``(wbs_id, budget_head_id)`` is in the
      declared lock set whenever the row exists, because the lock set is every
      ancestor-or-self cell that exists and a cell is its own ancestor. This
      ``UPDATE`` re-enters a lock this transaction already holds, so it
      acquires nothing new and adds no edge to the wait-for graph. Until
      Wave 3 that was **false** for a cell with ``budget_paise = 0`` -- the
      lock query filtered those out, and this docstring claimed the fold into
      a single statement made the gap harmless. It did not; it only made the
      unlocked write atomic. The filter is now gone (see
      ``locking._LOCK_SQL``), which is what actually closes it.
    * ``budget_ledger_cell`` is a different table and is **not** in the lock
      set. Its row lock is taken here, implicitly, while this transaction
      holds the corresponding ``budget_control_cell`` lock -- which
      ``fk_ledger_control_cell`` guarantees exists. Every writer of a ledger
      row holds that control lock, so ledger locks are always acquired beneath
      the control order and never invert it. They are not recorded in
      ``locks_taken``, which tracks control cells only.

    Folding each read and write into one ``UPDATE ... SET x = (SELECT ...)``
    is still worth doing -- it keeps the recompute atomic against anything
    that could bypass the service layer -- but it is a second line of defence,
    not the reason the lock ordering holds.

    Idempotent: recomputing twice for the same ``as_of`` with no intervening
    change to ``budget_line`` produces the same numbers both times.
    """
    as_of = as_of or date.today()
    params = {"wbs_id": wbs_id, "head": budget_head_id, "as_of": as_of, "actor": actor}

    session.execute(  # scope-exempt: derives one already-locked cell from its own lines
        """
        UPDATE budget_control_cell SET
            budget_paise = COALESCE((
                SELECT SUM(amount_paise)::bigint FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND effective_from <= %(as_of)s
            ), 0),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
        """,
        params,
    )
    session.execute(  # scope-exempt: derives one already-locked cell from its own lines
        """
        UPDATE budget_ledger_cell SET
            original_paise = COALESCE((
                SELECT SUM(amount_paise)::bigint FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND kind = 'ORIGINAL' AND effective_from <= %(as_of)s
            ), 0),
            revisions_paise = COALESCE((
                SELECT SUM(amount_paise)::bigint FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND kind <> 'ORIGINAL' AND effective_from <= %(as_of)s
            ), 0),
            future_budget_paise = COALESCE((
                SELECT SUM(amount_paise)::bigint FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND effective_from > %(as_of)s
            ), 0),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
        """,
        params,
    )
    row = session.fetchone(  # scope-exempt: reads back the cell this call just recomputed
        "SELECT bc.budget_paise, bl.original_paise, bl.revisions_paise, bl.future_budget_paise "
        "FROM budget_control_cell bc "
        "JOIN budget_ledger_cell bl ON bl.wbs_id = bc.wbs_id AND bl.budget_head_id = bc.budget_head_id "
        "WHERE bc.wbs_id = %(wbs_id)s AND bc.budget_head_id = %(head)s",
        params,
    )
    budget_paise, original, revisions, future = row
    return {
        "wbs_id": wbs_id, "budget_head_id": budget_head_id,
        "budget_paise": budget_paise, "original_paise": original,
        "revisions_paise": revisions, "future_budget_paise": future,
    }


# ==========================================================================
# Availability: nearest budget-owning ancestor, subtree rollup
# ==========================================================================
def _owning_ancestor(session: Session, wbs_id: str, budget_head_id: str) -> tuple[str, str] | None:
    """``(owner_wbs_id, owner_wbs_path)`` of the nearest ancestor-or-self of
    ``wbs_id`` whose OWN ``budget_paise`` is non-zero for ``budget_head_id``,
    or ``None`` if no cell on the chain owns budget for this head at all."""
    row = session.fetchone(  # scope-exempt: ancestor rollup must see the whole chain
        """
        SELECT a.wbs_id, a.wbs_path::text
        FROM wbs_element w
        JOIN wbs_element a ON w.wbs_path <@ a.wbs_path
        JOIN budget_control_cell bc
            ON bc.wbs_id = a.wbs_id AND bc.budget_head_id = %(head)s
        WHERE w.wbs_id = %(wbs_id)s AND bc.budget_paise <> 0
        ORDER BY nlevel(a.wbs_path) DESC
        LIMIT 1
        """,
        {"wbs_id": wbs_id, "head": budget_head_id},
    )
    return (row[0], row[1]) if row else None


def _subtree_totals(session: Session, owner_wbs_id: str, budget_head_id: str) -> dict:
    """``SUM(budget_paise)`` and ``SUM(commitment + actual + pr_reserved)``
    over every cell in the subtree rooted at ``owner_wbs_id`` for
    ``budget_head_id`` -- the exact rollup ``app.backend.domain.compute_ledger``
    produces at ``head_totals``, via an ``ltree`` subtree scan instead of an
    in-memory tree walk. Never persisted; recomputed at every call."""
    row = session.fetchone(  # scope-exempt: subtree rollup must see the whole subtree
        """
        -- ::bigint on every SUM. PostgreSQL's SUM(bigint) returns NUMERIC,
        -- which psycopg hands back as decimal.Decimal, and a Decimal reaching
        -- the arithmetic below raised TypeError on Decimal * float. The cast
        -- is the fix at the source: money leaves the database as an integer
        -- number of paise, exactly as it is stored, with no Decimal to
        -- coerce, round or accidentally mix with a float downstream.
        SELECT
            COALESCE(SUM(bc.budget_paise), 0)::bigint,
            COALESCE(SUM(bl.commitment_paise), 0)::bigint,
            COALESCE(SUM(bl.actual_paise), 0)::bigint,
            COALESCE(SUM(bl.pr_reserved_paise), 0)::bigint
        FROM wbs_element o
        JOIN wbs_element x ON x.wbs_path <@ o.wbs_path
        JOIN budget_control_cell bc ON bc.wbs_id = x.wbs_id AND bc.budget_head_id = %(head)s
        LEFT JOIN budget_ledger_cell bl ON bl.wbs_id = x.wbs_id AND bl.budget_head_id = %(head)s
        WHERE o.wbs_id = %(owner)s
        """,
        {"owner": owner_wbs_id, "head": budget_head_id},
    )
    budget_paise, commitment, actual, pr_reserved = row
    exposure = commitment + actual + pr_reserved
    return {
        "budget_paise": budget_paise, "commitment_paise": commitment,
        "actual_paise": actual, "pr_reserved_paise": pr_reserved,
        "exposure_paise": exposure, "available_paise": budget_paise - exposure,
    }


def check_availability(session: Session, wbs_id: str, budget_head_id: str,
                        amount_paise: int) -> dict:
    """Read-only availability verdict for a proposed spend of ``amount_paise``
    at ``(wbs_id, budget_head_id)``. Never scans transaction history, never
    persists anything -- ``WAVE2_CONTRACTS.md``'s
    ``GET /api/budget/availability`` shape exactly."""
    if amount_paise is None or amount_paise < 0:
        _err("NEGATIVE_AMOUNT", "amount_paise must be zero or a positive integer.")

    _assert_wbs_in_scope(session, wbs_id)

    if session.fetchone(
        "SELECT 1 FROM budget_head WHERE budget_head_id = %s AND active", (budget_head_id,)
    ) is None:
        _err("HEAD_UNKNOWN", f"Budget head {budget_head_id} is not a valid active head.", status=404)

    owner = _owning_ancestor(session, wbs_id, budget_head_id)
    if owner is None:
        _err("NO_BUDGET_FOR_HEAD",
             f"No approved budget exists for budget head {budget_head_id!r} on "
             f"{wbs_id!r} or any element above it.", status=404)
    owner_wbs_id, _owner_path = owner  # type: ignore[misc]

    totals = _subtree_totals(session, owner_wbs_id, budget_head_id)
    available = totals["available_paise"]
    exceeds = amount_paise > available

    if exceeds:
        verdict = "EXCEEDS_BUDGET"
    else:
        # Integer numerator and denominator, both paise; the float appears
        # only in the final ratio, which is a display/threshold percentage and
        # never a monetary value.
        pct_after = (
            round((totals["exposure_paise"] + amount_paise) * 100.0
                  / totals["budget_paise"], 1)
            if totals["budget_paise"] else 0.0
        )
        verdict = ("CRITICAL" if pct_after >= CRITICAL_PCT else
                   "WATCH" if pct_after >= WATCH_PCT else "OK")

    return {
        "wbs_id": wbs_id,
        "budget_head_id": budget_head_id,
        "owning_wbs_id": owner_wbs_id,
        "budget_paise": totals["budget_paise"],
        "exposure_paise": totals["exposure_paise"],
        "available_paise": available,
        "requested_paise": amount_paise,
        "verdict": verdict,
        "shortfall_paise": max(0, amount_paise - available),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ==========================================================================
# Cells and lines: read paths
# ==========================================================================
def list_cells(session: Session, *, project_id: str | None = None,
                wbs_id: str | None = None, budget_head_id: str | None = None,
                cursor: str | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    limit = min(max(limit, 1), MAX_LIMIT)
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if project_id is not None:
        conditions.append("w.project_id = %(project_id)s")
        params["project_id"] = project_id
    if wbs_id is not None:
        conditions.append("bc.wbs_id = %(wbs_id)s")
        params["wbs_id"] = wbs_id
    if budget_head_id is not None:
        conditions.append("bc.budget_head_id = %(budget_head_id)s")
        params["budget_head_id"] = budget_head_id
    if cursor:
        path_text, head_id = _decode_cursor(cursor)
        conditions.append(
            "(w.wbs_path::text, bc.budget_head_id) > (%(cursor_path)s, %(cursor_head)s)")
        params["cursor_path"] = path_text
        params["cursor_head"] = head_id
    where = " AND ".join(conditions) if conditions else "TRUE"
    params["fetch_limit"] = limit + 1

    rows = repo.query(
        session,
        f"""
        SELECT w.wbs_id, w.wbs_path::text, bc.budget_head_id, bc.budget_paise,
               COALESCE(bl.original_paise, 0), COALESCE(bl.revisions_paise, 0),
               COALESCE(bl.future_budget_paise, 0), COALESCE(bl.ordered_paise, 0),
               COALESCE(bl.commitment_paise, 0), COALESCE(bl.actual_paise, 0),
               COALESCE(bl.received_paise, 0), COALESCE(bl.received_not_billed_paise, 0),
               COALESCE(bl.pr_reserved_paise, 0), bc.updated_at
        FROM budget_control_cell bc
        JOIN wbs_element w ON w.wbs_id = bc.wbs_id
        JOIN project p ON p.project_id = w.project_id
        LEFT JOIN budget_ledger_cell bl
            ON bl.wbs_id = bc.wbs_id AND bl.budget_head_id = bc.budget_head_id
        WHERE {where} AND {{scope}}
        ORDER BY w.wbs_path::text, bc.budget_head_id
        LIMIT %(fetch_limit)s
        """,
        params,
        columns=_CELL_SCOPE_COLUMNS,
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    items = []
    for (w_id, path_text, head_id, budget_paise, original, revisions, future,
         ordered, commitment, actual, received, rec_not_billed, pr_reserved,
         updated_at) in page:
        exposure = commitment + actual + pr_reserved
        items.append({
            "wbs_id": w_id, "wbs_path": path_text, "budget_head_id": head_id,
            "budget_paise": budget_paise, "original_paise": original,
            "revisions_paise": revisions, "future_budget_paise": future,
            "ordered_paise": ordered, "commitment_paise": commitment,
            "actual_paise": actual, "received_paise": received,
            "received_not_billed_paise": rec_not_billed, "pr_reserved_paise": pr_reserved,
            "exposure_paise": exposure, "available_paise": budget_paise - exposure,
            "recomputed_at": _iso(updated_at),
        })
    next_cursor = _encode_cursor(page[-1][1], page[-1][2]) if has_more and page else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def _line_row_to_dict(row: tuple) -> dict:
    (line_id, wbs_id, head_id, kind, amount_paise, effective_from, effective_to,
     status, justification, created_at, created_by, version_no) = row
    return {
        "budget_line_id": line_id, "wbs_id": wbs_id, "budget_head_id": head_id,
        "kind": kind, "amount_paise": amount_paise,
        "effective_from": _iso(effective_from), "effective_to": _iso(effective_to),
        "status": status, "justification": justification,
        "created_at": _iso(created_at), "created_by": created_by,
        "version_no": version_no,
    }


def list_lines(session: Session, *, project_id: str | None = None,
               wbs_id: str | None = None, version: int | None = None) -> list[dict]:
    """``version``, per the frozen contract's own field naming, filters on
    ``budget_line.version_no`` -- each row's own optimistic-concurrency
    counter -- not on a ``budget_version`` snapshot number (a distinct
    concept the contract keeps in its own table; see ``list_versions``/
    ``compare_versions``). Documented here as the one contract detail this
    stream had to interpret rather than ask about."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if project_id is not None:
        conditions.append("w.project_id = %(project_id)s")
        params["project_id"] = project_id
    if wbs_id is not None:
        conditions.append("bl.wbs_id = %(wbs_id)s")
        params["wbs_id"] = wbs_id
    if version is not None:
        conditions.append("bl.version_no = %(version)s")
        params["version"] = version
    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = repo.query(
        session,
        f"""
        SELECT bl.budget_line_id, bl.wbs_id, bl.budget_head_id, bl.kind,
               bl.amount_paise, bl.effective_from, bl.effective_to, bl.status,
               bl.justification, bl.created_at, bl.created_by, bl.version_no
        FROM budget_line bl
        JOIN wbs_element w ON w.wbs_id = bl.wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE {where} AND {{scope}}
        ORDER BY bl.created_at DESC, bl.budget_line_id
        """,
        params,
        columns=_CELL_SCOPE_COLUMNS,
    )
    return [_line_row_to_dict(r) for r in rows]


def _project_id_for_wbs(session: Session, wbs_id: str) -> str | None:
    row = session.fetchone(  # scope-exempt: internal lookup; callers scope-gate the wbs_id first
        "SELECT project_id FROM wbs_element WHERE wbs_id = %s", (wbs_id,))
    return row[0] if row else None


def _assert_wbs_in_scope(session: Session, wbs_id: str) -> None:
    """Refuse a WBS element the caller's scope does not reach.

    NOT-FOUND OVER FORBIDDEN: an out-of-scope element answers exactly as a
    non-existent one does. A 403 on an id the caller cannot see is an
    existence oracle -- it confirms the element is real.
    """
    row = repo.query_one(
        session,
        """
        SELECT 1 FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
        WHERE w.wbs_id = %(wbs_id)s AND {scope}
        """,
        {"wbs_id": wbs_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.", status=404)


def _assert_project_in_scope(session: Session, project_id: str) -> None:
    """As `_assert_wbs_in_scope`, for a caller-supplied project id."""
    row = repo.query_one(
        session,
        "SELECT 1 FROM project p WHERE p.project_id = %(project_id)s AND {scope}",
        {"project_id": project_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    if row is None:
        _err("PROJECT_NOT_FOUND", f"Project {project_id} does not exist.", status=404)


def _assert_cell_in_scope(session: Session, wbs_id: str, budget_head_id: str,
                           *, label: str = "") -> None:
    """Scope first, then existence -- in that order, for the same reason."""
    _assert_wbs_in_scope(session, wbs_id)
    row = session.fetchone(  # scope-exempt: the wbs_id above is already scope-cleared
        "SELECT 1 FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
        (wbs_id, budget_head_id))
    if row is None:
        prefix = f"the {label} cell " if label else ""
        _err("CELL_NOT_FOUND",
             f"No control cell exists for {prefix}({wbs_id}, {budget_head_id}).",
             status=404)


# ==========================================================================
# Original grant (not exposed via the API this milestone -- see module
# docstring; used by the seed fragment and by tests).
# ==========================================================================
def record_original(session: Session, *, wbs_id: str, budget_head_id: str,
                     amount_paise: int, effective_from: date, actor: str,
                     justification: str | None = None) -> dict:
    """Establish the ORIGINAL grant for a cell as a ``budget_line`` row and
    recompute its stored position.

    Not exposed via the API: the frozen contract for this milestone has no
    endpoint for it -- the very first grant on a cell is presently a seed/
    migration concern (``budget_control_cell``/``budget_ledger_cell`` rows
    already exist from 001/002, seeded directly). Used so the invariant
    "budget_control_cell.budget_paise is derived from budget_line" holds
    uniformly, including for the first row on a cell.
    """
    if amount_paise is None or amount_paise <= 0:
        _err("INVALID_AMOUNT", "An ORIGINAL grant must be a positive amount.")
    lock_affected_cells(session, [(wbs_id, budget_head_id)])
    line_id = _new_id("BL")
    session.execute(
        """
        INSERT INTO budget_line
            (budget_line_id, wbs_id, budget_head_id, kind, amount_paise,
             effective_from, status, justification, created_by, updated_by)
        VALUES (%(id)s, %(wbs_id)s, %(head)s, 'ORIGINAL', %(amount)s, %(eff)s,
                'Approved', %(just)s, %(actor)s, %(actor)s)
        """,
        {"id": line_id, "wbs_id": wbs_id, "head": budget_head_id,
         "amount": amount_paise, "eff": effective_from,
         "just": justification, "actor": actor},
    )
    result = recompute_cell(session, wbs_id, budget_head_id, actor=actor)
    audit_mod.append(
        session, actor, "ORIGINAL_BUDGET_SET", "BUDGET_CELL", f"{wbs_id}:{budget_head_id}",
        f"Original budget recorded: {amount_paise} paise (budget_line {line_id})")
    result["budget_line_id"] = line_id
    return result


# ==========================================================================
# Revisions
# ==========================================================================
def create_revision(session: Session, *, wbs_id: str, budget_head_id: str,
                     delta_paise: int, effective_from: date, justification: str,
                     actor: str) -> dict:
    """DRAFT only. Creates no spending capacity and touches no cell -- no
    lock is taken, per domain-controls.md: "a revision creates no spending
    capacity until approved"."""
    if delta_paise is None or delta_paise == 0:
        _err("INVALID_DELTA", "delta_paise must be a non-zero integer.")
    if not justification or not justification.strip():
        _err("JUSTIFICATION_REQUIRED", "A justification is required for a budget revision.")
    _assert_cell_in_scope(session, wbs_id, budget_head_id)

    revision_id = _new_id("REV")
    session.execute(
        """
        INSERT INTO budget_revision
            (revision_id, wbs_id, budget_head_id, delta_paise, effective_from,
             justification, status, created_by)
        VALUES (%(id)s, %(wbs_id)s, %(head)s, %(delta)s, %(eff)s, %(just)s, 'DRAFT', %(actor)s)
        """,
        {"id": revision_id, "wbs_id": wbs_id, "head": budget_head_id,
         "delta": delta_paise, "eff": effective_from, "just": justification, "actor": actor},
    )
    audit_mod.append(session, actor, "REVISION_CREATE", "BUDGET_REVISION", revision_id,
                      f"Revision drafted for {wbs_id}:{budget_head_id}: {delta_paise:+d} paise")
    return {"revision_id": revision_id, "status": "DRAFT", "delta_paise": delta_paise}


def approve_revision(session: Session, *, revision_id: str, actor: str,
                      approval_instance_id: str | None = None) -> dict:
    """Write the revision into the budget.

    ``approval_instance_id`` names the approval instance whose closure is
    causing this call -- ``approval_writeback.apply_outcome`` supplies it and
    nothing else does. Its only effect is to exempt that one instance from
    ``_assert_not_under_approval``: a revision with a live instance must not be
    approvable by the direct route, or submitting it would be a formality
    anybody holding ``revision.approve`` could step around.

    Every other control here is unchanged and is deliberately NOT reimplemented
    in the write-back: maker-checker, the DRAFT precondition, the ordered cell
    lock, the re-read under the lock, the availability re-check on a cut, the
    effective-dated ``budget_line`` (the original grant stays immutable -- this
    appends, it never edits) and the version snapshot. The write-back calls
    this function precisely so none of that can drift into a second copy.
    """
    row = session.fetchone(  # scope-exempt: the cell it names is scope-gated immediately below
        "SELECT wbs_id, budget_head_id, delta_paise, effective_from, justification, "
        "status, created_by FROM budget_revision WHERE revision_id = %s",
        (revision_id,))
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    # Holding `revision.approve` is not authority over every entity's budget.
    _assert_wbs_in_scope(session, row[0])
    # AFTER the scope gate, before any lock. Scope first, then state -- the
    # same order `_assert_cell_in_scope` uses, and for the same reason: a
    # caller who cannot see this revision must be told it does not exist, not
    # told that it is under approval.
    _assert_not_under_approval(session, OBJECT_TYPE_REVISION, revision_id,
                                approval_instance_id=approval_instance_id,
                                label="Revision")
    (wbs_id, head_id, delta_paise, effective_from, justification,
     status, created_by) = row
    if status not in _DECIDABLE_STATUSES:
        _err("REVISION_NOT_DRAFT",
             f"Revision {revision_id} is {status}, not DRAFT.", status=409)

    # Maker-checker (domain-controls.md). A real role-based
    # `require_separation` lands with Identity & scope (Wave 2 stream 3,
    # M4a); until then this is the one independent enforcement point this
    # stream can make -- a SUPERSET check (refuses more, never less), never a
    # replacement for the real thing.
    if actor == created_by:
        _err("SELF_APPROVAL_FORBIDDEN",
             "The approver of a budget revision must not be the same user "
             "who drafted it.", status=403)

    # Rule 1: cells first, complete set, exactly once.
    lock_affected_cells(session, [(wbs_id, head_id)])

    # Document row -- second in the global order.
    session.execute(  # scope-exempt: locks a row already scope-gated in this call
        "SELECT revision_id FROM budget_revision WHERE revision_id = %s FOR UPDATE",
        (revision_id,))
    # Re-read status under the lock: another transaction could have decided
    # this revision between the unlocked read above and this point.
    status = session.fetchone(  # scope-exempt: re-reads the row just locked above
        "SELECT status FROM budget_revision WHERE revision_id = %s", (revision_id,))[0]
    if status not in _DECIDABLE_STATUSES:
        _err("REVISION_NOT_DRAFT",
             f"Revision {revision_id} is {status}, not DRAFT.", status=409)

    # Re-check availability inside the lock. Approval does not freeze
    # availability: only a CUT (delta_paise < 0) can push a cell over its own
    # exposure -- an increase can never make a cell less available, so only
    # that direction needs re-validating.
    if delta_paise < 0:
        check = check_availability(session, wbs_id, head_id, -delta_paise)
        if check["verdict"] == "EXCEEDS_BUDGET":
            _err("BUDGET_MOVED",
                 f"Availability for ({wbs_id}, {head_id}) shifted since this "
                 f"revision was drafted: only {check['available_paise']} paise "
                 f"remain available, but the cut removes {-delta_paise} paise.",
                 status=409)

    line_id = _new_id("BL")
    session.execute(
        """
        INSERT INTO budget_line
            (budget_line_id, wbs_id, budget_head_id, kind, amount_paise,
             effective_from, status, justification, created_by, updated_by)
        VALUES (%(id)s, %(wbs_id)s, %(head)s, 'REVISION', %(amount)s, %(eff)s,
                'Approved', %(just)s, %(actor)s, %(actor)s)
        """,
        {"id": line_id, "wbs_id": wbs_id, "head": head_id, "amount": delta_paise,
         "eff": effective_from, "just": justification, "actor": actor},
    )
    recompute_cell(session, wbs_id, head_id, actor=actor)

    session.execute(
        """
        UPDATE budget_revision
        SET status = 'APPROVED', budget_line_id = %(line_id)s,
            decided_at = now(), decided_by = %(actor)s, version_no = version_no + 1
        WHERE revision_id = %(id)s
        """,
        {"line_id": line_id, "actor": actor, "id": revision_id},
    )

    project_id = _project_id_for_wbs(session, wbs_id)
    version_note = ""
    if project_id:
        v = create_version_snapshot(
            session, project_id=project_id,
            label=f"After revision {revision_id} approved", actor=actor)
        version_note = f"; snapshot v{v['version_no']} taken"

    audit_mod.append(session, actor, "REVISION_APPROVE", "BUDGET_REVISION", revision_id,
                      f"Revision approved for {wbs_id}:{head_id}: {delta_paise:+d} paise "
                      f"(budget_line {line_id}){version_note}")
    return {"revision_id": revision_id, "status": "APPROVED",
            "delta_paise": delta_paise, "budget_line_id": line_id}


def reject_revision(session: Session, *, revision_id: str, actor: str,
                     reason: str | None = None,
                     approval_instance_id: str | None = None) -> dict:
    """Reject the revision.

    Guarded exactly as `approve_revision` is, and for a reason that is not
    symmetric with it: a direct rejection under a live instance would move the
    document to a terminal status while leaving the instance OPEN, so approvers
    would keep being asked to decide something already decided.
    """
    row = session.fetchone(  # scope-exempt: scope-gated immediately below
        "SELECT wbs_id, status, created_by FROM budget_revision "
        "WHERE revision_id = %s FOR UPDATE",
        (revision_id,))
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    _assert_wbs_in_scope(session, row[0])
    _assert_not_under_approval(session, OBJECT_TYPE_REVISION, revision_id,
                                approval_instance_id=approval_instance_id,
                                label="Revision")
    row = row[1:]
    status, _created_by = row
    if status not in _DECIDABLE_STATUSES:
        _err("REVISION_NOT_DRAFT", f"Revision {revision_id} is {status}, not DRAFT.", status=409)
    session.execute(
        "UPDATE budget_revision SET status = 'REJECTED', decided_at = now(), "
        "decided_by = %(actor)s, decision_note = %(reason)s, version_no = version_no + 1 "
        "WHERE revision_id = %(id)s",
        {"actor": actor, "reason": reason, "id": revision_id})
    audit_mod.append(session, actor, "REVISION_REJECT", "BUDGET_REVISION", revision_id,
                      "Revision rejected" + (f": {reason}" if reason else ""))
    return {"revision_id": revision_id, "status": "REJECTED"}


# ==========================================================================
# Transfers
# ==========================================================================
def create_transfer(session: Session, *, from_wbs_id: str, from_head_id: str,
                     to_wbs_id: str, to_head_id: str, amount_paise: int,
                     effective_from: date, justification: str, actor: str) -> dict:
    if amount_paise is None or amount_paise <= 0:
        _err("INVALID_AMOUNT", "amount_paise must be a positive integer.")
    if not justification or not justification.strip():
        _err("JUSTIFICATION_REQUIRED", "A justification is required for a budget transfer.")
    if from_wbs_id == to_wbs_id and from_head_id == to_head_id:
        _err("SAME_CELL_TRANSFER", "A transfer must move budget between two different cells.")
    for cell_wbs_id, cell_head_id, role in (
        (from_wbs_id, from_head_id, "from"), (to_wbs_id, to_head_id, "to"),
    ):
        _assert_cell_in_scope(session, cell_wbs_id, cell_head_id, label=role)

    transfer_id = _new_id("TRF")
    session.execute(
        """
        INSERT INTO budget_transfer
            (transfer_id, from_wbs_id, from_head_id, to_wbs_id, to_head_id,
             amount_paise, effective_from, justification, status, created_by)
        VALUES (%(id)s, %(fw)s, %(fh)s, %(tw)s, %(th)s, %(amount)s, %(eff)s,
                %(just)s, 'DRAFT', %(actor)s)
        """,
        {"id": transfer_id, "fw": from_wbs_id, "fh": from_head_id,
         "tw": to_wbs_id, "th": to_head_id, "amount": amount_paise,
         "eff": effective_from, "just": justification, "actor": actor},
    )
    audit_mod.append(session, actor, "TRANSFER_CREATE", "BUDGET_TRANSFER", transfer_id,
                      f"Transfer drafted: {amount_paise} paise from "
                      f"{from_wbs_id}:{from_head_id} to {to_wbs_id}:{to_head_id}")
    return {"transfer_id": transfer_id, "status": "DRAFT", "amount_paise": amount_paise}


def approve_transfer(session: Session, *, transfer_id: str, actor: str,
                      approval_instance_id: str | None = None) -> dict:
    """As `approve_revision`, for a transfer. Same exemption, same reasoning."""
    row = session.fetchone(  # scope-exempt: both cells are scope-gated immediately below
        "SELECT from_wbs_id, from_head_id, to_wbs_id, to_head_id, amount_paise, "
        "effective_from, justification, status, created_by "
        "FROM budget_transfer WHERE transfer_id = %s", (transfer_id,))
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    # BOTH ends, not just the source: a transfer moves budget into a cell too.
    _assert_wbs_in_scope(session, row[0])
    _assert_wbs_in_scope(session, row[2])
    _assert_not_under_approval(session, OBJECT_TYPE_TRANSFER, transfer_id,
                                approval_instance_id=approval_instance_id,
                                label="Transfer")
    (from_wbs, from_head, to_wbs, to_head, amount_paise, effective_from,
     justification, status, created_by) = row
    if status not in _DECIDABLE_STATUSES:
        _err("TRANSFER_NOT_DRAFT", f"Transfer {transfer_id} is {status}, not DRAFT.", status=409)
    if actor == created_by:
        _err("SELF_APPROVAL_FORBIDDEN",
             "The approver of a budget transfer must not be the same user who "
             "drafted it.", status=403)

    # Rule 1: cells first, complete set, exactly once -- BOTH legs, since a
    # transfer moves money out of one cell's ancestor chain and into another's.
    lock_affected_cells(session, [(from_wbs, from_head), (to_wbs, to_head)])

    # Document row -- second in the global order.
    session.execute(  # scope-exempt: locks a row already scope-gated in this call
        "SELECT transfer_id FROM budget_transfer WHERE transfer_id = %s FOR UPDATE",
        (transfer_id,))
    status = session.fetchone(  # scope-exempt: re-reads the row just locked above
        "SELECT status FROM budget_transfer WHERE transfer_id = %s", (transfer_id,))[0]
    if status not in _DECIDABLE_STATUSES:
        _err("TRANSFER_NOT_DRAFT", f"Transfer {transfer_id} is {status}, not DRAFT.", status=409)

    # Re-check the SOURCE cell's availability inside the lock: reducing its
    # budget can push its exposure past the (now smaller) budget if spend
    # accrued against it since this transfer was drafted. Approval does not
    # freeze availability.
    check = check_availability(session, from_wbs, from_head, amount_paise)
    if check["verdict"] == "EXCEEDS_BUDGET":
        _err("BUDGET_MOVED",
             f"Availability for ({from_wbs}, {from_head}) shifted since this "
             f"transfer was drafted: only {check['available_paise']} paise "
             f"remain available, but the transfer moves out {amount_paise} paise.",
             status=409)

    from_line_id = _new_id("BL")
    to_line_id = _new_id("BL")
    session.execute(
        """
        INSERT INTO budget_line
            (budget_line_id, wbs_id, budget_head_id, kind, amount_paise,
             effective_from, status, justification, created_by, updated_by)
        VALUES
            (%(from_id)s, %(from_wbs)s, %(from_head)s, 'TRANSFER', %(neg_amount)s,
             %(eff)s, 'Approved', %(just)s, %(actor)s, %(actor)s),
            (%(to_id)s, %(to_wbs)s, %(to_head)s, 'TRANSFER', %(amount)s,
             %(eff)s, 'Approved', %(just)s, %(actor)s, %(actor)s)
        """,
        {"from_id": from_line_id, "from_wbs": from_wbs, "from_head": from_head,
         "to_id": to_line_id, "to_wbs": to_wbs, "to_head": to_head,
         "neg_amount": -amount_paise, "amount": amount_paise,
         "eff": effective_from, "just": justification, "actor": actor},
    )
    recompute_cell(session, from_wbs, from_head, actor=actor)
    recompute_cell(session, to_wbs, to_head, actor=actor)

    session.execute(
        """
        UPDATE budget_transfer
        SET status = 'APPROVED', from_line_id = %(from_id)s, to_line_id = %(to_id)s,
            decided_at = now(), decided_by = %(actor)s, version_no = version_no + 1
        WHERE transfer_id = %(id)s
        """,
        {"from_id": from_line_id, "to_id": to_line_id, "actor": actor, "id": transfer_id},
    )

    version_note = ""
    from_project = _project_id_for_wbs(session, from_wbs)
    to_project = _project_id_for_wbs(session, to_wbs)
    # SORTED, not a set. Iterating a set of strings takes an order that varies
    # with PYTHONHASHSEED across worker processes, so two concurrent
    # cross-project transfers between the same pair could take the two
    # projects' advisory locks in opposite orders and deadlock. A total order
    # over the project ids removes the cycle, exactly as the cell lock order
    # does -- see locking.py's proof of deadlock freedom, which this now obeys
    # rather than sitting outside.
    for project_id in sorted({p for p in (from_project, to_project) if p}):
        v = create_version_snapshot(
            session, project_id=project_id,
            label=f"After transfer {transfer_id} approved", actor=actor)
        version_note += f"; snapshot v{v['version_no']} taken for {project_id}"

    audit_mod.append(session, actor, "TRANSFER_APPROVE", "BUDGET_TRANSFER", transfer_id,
                      f"Transfer approved: {amount_paise} paise from "
                      f"{from_wbs}:{from_head} to {to_wbs}:{to_head}{version_note}")
    return {"transfer_id": transfer_id, "status": "APPROVED", "amount_paise": amount_paise}


def reject_transfer(session: Session, *, transfer_id: str, actor: str,
                     reason: str | None = None,
                     approval_instance_id: str | None = None) -> dict:
    """As `reject_revision`, for a transfer."""
    row = session.fetchone(  # scope-exempt: scope-gated immediately below
        "SELECT from_wbs_id, to_wbs_id, status FROM budget_transfer "
        "WHERE transfer_id = %s FOR UPDATE",
        (transfer_id,))
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    _assert_wbs_in_scope(session, row[0])
    _assert_wbs_in_scope(session, row[1])
    _assert_not_under_approval(session, OBJECT_TYPE_TRANSFER, transfer_id,
                                approval_instance_id=approval_instance_id,
                                label="Transfer")
    status = row[2]
    if status not in _DECIDABLE_STATUSES:
        _err("TRANSFER_NOT_DRAFT", f"Transfer {transfer_id} is {status}, not DRAFT.", status=409)
    session.execute(
        "UPDATE budget_transfer SET status = 'REJECTED', decided_at = now(), "
        "decided_by = %(actor)s, decision_note = %(reason)s, version_no = version_no + 1 "
        "WHERE transfer_id = %(id)s",
        {"actor": actor, "reason": reason, "id": transfer_id})
    audit_mod.append(session, actor, "TRANSFER_REJECT", "BUDGET_TRANSFER", transfer_id,
                      "Transfer rejected" + (f": {reason}" if reason else ""))
    return {"transfer_id": transfer_id, "status": "REJECTED"}


# ==========================================================================
# Submission for approval (Wave 4 stream A2)
# ==========================================================================
# Until this section existed, ``app/backend/pg/approvals.py`` was reachable
# only from tests: nothing in the application ever called ``open_instance``, so
# a revision or transfer went straight from DRAFT to ``approve_revision`` /
# ``approve_transfer`` and the configurable workflow decided nothing. These
# functions are the missing front end. ``approval_writeback.apply_outcome`` is
# the back end, called by the engine at the single point an instance closes.
#
# The document's own status stays DRAFT for the whole time an instance is
# open. There is no SUBMITTED state to move it to: migration 003's
# ``ck_budget_revision_decision`` / ``ck_budget_transfer_decision`` admit
# exactly {DRAFT, APPROVED, REJECTED, CANCELLED}, and DRAFT is C3's "editable,
# no control effect" -- which is the truth about a revision awaiting approval.
# It creates no spending capacity, and ``_assert_not_under_approval`` below
# stops the DRAFT being approved by the direct route while an instance is live.

#: The document types this module can submit. Deliberately the two keys of
#: ``approvals.OBJECT_BINDINGS`` and no more: PR and PO live in the SQLite
#: application and have no PostgreSQL table, so a binding for them would name a
#: table that does not exist.
OBJECT_TYPE_REVISION = "BUDGET_REVISION"
OBJECT_TYPE_TRANSFER = "BUDGET_TRANSFER"

#: Approval-instance statuses that mean "this object is still with the
#: workflow". EXCEPTION_PENDING is included on purpose: an unroutable object is
#: held for an administrator, and it must not be approvable by the direct route
#: while it is held -- that would be exactly the bypass the exception exists to
#: prevent.
LIVE_INSTANCE_STATUSES = ("OPEN", "EXCEPTION_PENDING")


def _as_paise(value: Any, *, field: str) -> int:
    """Refuse anything but a Python ``int`` in a money path.

    ``SUM(bigint)`` comes back from PostgreSQL as ``numeric``, which psycopg
    hands over as ``decimal.Decimal``, and a ``float`` can arrive from any
    caller that did arithmetic on the way here. Both are refused rather than
    coerced: the predicate compiler rejects a float compared against a
    ``*_paise`` path by design (``approval_rules._assert_money_discipline``),
    and rounding one here to get past that would be defeating the control
    rather than satisfying it. ``bool`` is an ``int`` in Python and is excluded
    explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        _err("MONEY_NOT_INTEGER",
             f"{field} reached the approval snapshot as "
             f"{type(value).__name__} ({value!r}), not an integer number of "
             f"paise. Rounding it here would hide the defect rather than fix "
             f"it; the value must be an integer before it gets this far.",
             status=500)
    return value


def _routing_dimensions(session: Session, wbs_id: str) -> dict[str, Any]:
    """The org dimensions section 9.2's routing conditions read.

    ``approval_rules.CONDITION_DIMENSIONS`` names entity, plant, project and
    location among the eight dimensions a rule may test, and
    ``approvals.open_instance`` denormalises ``entity_id`` / ``project_id``
    onto the instance from exactly these snapshot keys -- which is what makes
    ``assert_instance_in_scope`` able to scope an instance at all. A snapshot
    without them routes on a null and produces an instance nobody's scope
    reaches.
    """
    row = repo.query_one(
        session,
        """
        SELECT w.project_id, p.entity_id, p.plant_id, p.location_id
        FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
        WHERE w.wbs_id = %(wbs_id)s AND {scope}
        """,
        {"wbs_id": wbs_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.", status=404)
    return {"project_id": row[0], "entity_id": row[1],
            "plant_id": row[2], "location_id": row[3]}


def _routing_budget_check(session: Session, wbs_id: str, budget_head_id: str,
                           requested_paise: int) -> dict[str, Any] | None:
    """One ``budget_checks`` entry: availability as the approvers were shown it.

    Contract 7 re-runs ``check_availability`` under the locks at the final
    approval and refuses with ``BUDGET_MOVED`` if availability has fallen since
    (``approvals.budget_moved``). That comparison needs a recorded baseline,
    and this is where it is recorded.

    Returns ``None`` where availability cannot be computed at all -- a cell
    whose head owns no budget anywhere on its ancestor chain raises
    ``NO_BUDGET_FOR_HEAD``. That is a legitimate state for a revision about to
    establish the first budget on a chain, and it must not stop the object
    being submitted. No baseline is recorded, so contract 7's check skips this
    cell (``_revalidate_budget`` skips an entry with missing fields) rather
    than comparing against a number nobody ever computed.

    ``checked_at`` is deliberately NOT copied in. It would make the snapshot's
    content hash a function of the clock, and ``supersede_if_changed`` compares
    that hash to decide whether the document changed underneath the instance --
    a timestamp in there would supersede every object on every check.
    """
    try:
        check = check_availability(session, wbs_id, budget_head_id, requested_paise)
    except BudgetServiceError:
        return None
    return {
        "wbs_id": wbs_id,
        "budget_head_id": budget_head_id,
        "requested_paise": _as_paise(check["requested_paise"], field="requested_paise"),
        "available_paise": _as_paise(check["available_paise"], field="available_paise"),
        "budget_paise": _as_paise(check["budget_paise"], field="budget_paise"),
        "exposure_paise": _as_paise(check["exposure_paise"], field="exposure_paise"),
        "verdict": check["verdict"],
    }


def revision_snapshot(session: Session, *, revision_id: str) -> dict[str, Any]:
    """The immutable document the approval rules are evaluated against.

    Contract 1 pins an instance to ``content_sha(snapshot)``, so everything a
    rule may need has to be in here at submission time -- there is no second
    read later. Money is integer paise throughout, checked by ``_as_paise``.
    """
    row = repo.query_one(
        session,
        """
        SELECT r.wbs_id, r.budget_head_id, r.delta_paise, r.effective_from,
               r.justification, r.created_by, r.version_no
        FROM budget_revision r
        JOIN wbs_element w ON w.wbs_id = r.wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE r.revision_id = %(id)s AND {scope}
        """,
        {"id": revision_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    (wbs_id, head_id, delta_paise, effective_from, justification,
     created_by, _version_no) = row

    delta = _as_paise(delta_paise, field="delta_paise")
    dimensions = _routing_dimensions(session, wbs_id)

    # Only a CUT consumes availability. An increase can never make a cell less
    # available, which is why `approve_revision` re-checks in that direction
    # only; the recorded baseline mirrors that exactly, so contract 7 compares
    # like with like.
    checks = []
    routed = _routing_budget_check(session, wbs_id, head_id, max(0, -delta))
    if routed is not None:
        checks.append(routed)

    return {
        "object_type": OBJECT_TYPE_REVISION,
        "object_id": revision_id,
        "wbs_id": wbs_id,
        "budget_head_id": head_id,
        "delta_paise": delta,
        # `amount_paise` is `CONDITION_DIMENSIONS["amount"]` -- the path every
        # threshold rule is written against. A cut of five crore needs the same
        # approval weight as an increase of five crore, so the magnitude, not
        # the signed delta, is what routes.
        "amount_paise": abs(delta),
        "direction": "INCREASE" if delta > 0 else "CUT",
        "effective_from": effective_from.isoformat(),
        "justification": justification,
        "maker_user_id": created_by,
        "affected_cells": [{"wbs_id": wbs_id, "budget_head_id": head_id}],
        "budget_checks": checks,
        **dimensions,
    }


def transfer_snapshot(session: Session, *, transfer_id: str) -> dict[str, Any]:
    """As `revision_snapshot`, for a transfer. Both legs, in the lock order."""
    row = repo.query_one(
        session,
        """
        SELECT t.from_wbs_id, t.from_head_id, t.to_wbs_id, t.to_head_id,
               t.amount_paise, t.effective_from, t.justification, t.created_by,
               t.version_no
        FROM budget_transfer t
        JOIN wbs_element w ON w.wbs_id = t.from_wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE t.transfer_id = %(id)s AND {scope}
        """,
        {"id": transfer_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    (from_wbs, from_head, to_wbs, to_head, amount_paise, effective_from,
     justification, created_by, _version_no) = row

    amount = _as_paise(amount_paise, field="amount_paise")
    # BOTH ends are scope-gated, as `approve_transfer` does: a transfer moves
    # budget INTO a cell as well as out of one, and the destination's project
    # is not necessarily the source's.
    from_dimensions = _routing_dimensions(session, from_wbs)
    _routing_dimensions(session, to_wbs)

    # Only the SOURCE leg can breach: the destination gains budget.
    checks = []
    routed = _routing_budget_check(session, from_wbs, from_head, amount)
    if routed is not None:
        checks.append(routed)

    return {
        "object_type": OBJECT_TYPE_TRANSFER,
        "object_id": transfer_id,
        "from_wbs_id": from_wbs, "from_head_id": from_head,
        "to_wbs_id": to_wbs, "to_head_id": to_head,
        "amount_paise": amount,
        "effective_from": effective_from.isoformat(),
        "justification": justification,
        "maker_user_id": created_by,
        # The complete set `approve_transfer` locks, in the same order it
        # declares it. `approval_writeback` asserts the two agree before it
        # calls through, because the engine locks from THIS list while the
        # service function locks from its own reading of the document.
        "affected_cells": [{"wbs_id": from_wbs, "budget_head_id": from_head},
                            {"wbs_id": to_wbs, "budget_head_id": to_head}],
        "budget_checks": checks,
        # `budget_head_id` is a routing dimension; on a transfer the source
        # head is the one whose budget is being reduced, so it is the one a
        # head-specific rule must see.
        "budget_head_id": from_head,
        **from_dimensions,
    }


def live_approval_instance(session: Session, object_type: str,
                            object_id: str) -> dict[str, Any] | None:
    """The OPEN or EXCEPTION_PENDING instance for this document, if any."""
    row = session.fetchone(  # scope-exempt: keyed on a document the caller was already scope-gated against, and returns an instance id and status only -- never business data
        "SELECT instance_id, status FROM approval_instance "
        "WHERE object_type = %s AND object_id = %s AND status = ANY(%s) "
        "ORDER BY opened_at DESC LIMIT 1",
        (object_type, object_id, list(LIVE_INSTANCE_STATUSES)))
    return None if row is None else {"instance_id": row[0], "status": row[1]}


def _assert_not_under_approval(session: Session, object_type: str, object_id: str,
                                *, approval_instance_id: str | None,
                                label: str) -> None:
    """An object with a live instance may only be approved BY that instance.

    Without this, submitting would be optional in the worst possible way: the
    workflow would open, and a holder of ``revision.approve`` could still walk
    past it through ``POST .../approve`` and write the budget_line anyway.
    ``approval_writeback`` passes the closing instance's own id, which is the
    one exemption -- and it passes it explicitly rather than relying on call
    order, so this holds whether the engine closes the instance before or after
    it calls the write-back.
    """
    live = live_approval_instance(session, object_type, object_id)
    if live is None or live["instance_id"] == approval_instance_id:
        return
    _err("APPROVAL_IN_PROGRESS",
         f"{label} {object_id} is under approval instance "
         f"{live['instance_id']} ({live['status']}). It can only be approved "
         f"by a decision on that instance, never by the direct route -- that "
         f"route exists for objects no workflow routes.",
         status=409)


#: Which table each submission result key belongs to. A CLOSED allow-list: the
#: identifiers below reach SQL through an f-string, because an identifier cannot
#: be parameterised, so the only safe source for one is this dictionary.
_SUBMIT_TABLES: dict[str, tuple[str, str]] = {
    "revision_id": ("budget_revision", "revision_id"),
    "transfer_id": ("budget_transfer", "transfer_id"),
}


def _touch_status(session: Session, *, id_key: str, object_id: str,
                   new_status: str) -> None:
    """Move a submitted document's own status, in the submitting transaction.

    Scope is not re-applied here and does not need to be: the caller
    (`submit_revision` / `submit_transfer`) has already read this exact row
    through `repo.query_one` with the cell scope mapping, and refused with a
    404 if it was not visible. Re-deriving a predicate for a row already proven
    in scope, on a primary key already established, would add a second place
    for the two derivations to disagree.
    """
    table, pk = _SUBMIT_TABLES[id_key]
    session.execute(  # noqa: S608 -- identifiers come from _SUBMIT_TABLES, never a caller
        f"UPDATE {table} SET status = %(status)s WHERE {pk} = %(id)s",
        {"status": new_status, "id": object_id})


#: The document states a decision may be applied to.
#:
#: The six decision gates read `status != "DRAFT"`, which was correct while a
#: document under approval stayed DRAFT. Migration 009 ended that: `submit_*`
#: writes SUBMITTED, and the approval write-back then calls `approve_revision`
#: / `reject_revision` on exactly that SUBMITTED row -- so every gate refused
#: the decision it exists to admit, and NO approval could be applied at all.
#:
#: SUBMITTED is added, not substituted. A direct DRAFT approval (no instance)
#: stays permitted, and `_assert_not_under_approval` is what stops that path
#: being used to step around an open instance. APPROVED, REJECTED and RETURNED
#: stay refused: a second decision on an already-decided document is precisely
#: what these gates exist for.
#:
#: The two SUBMISSION gates keep `status != "DRAFT"` and are deliberately not
#: included -- only a draft may be submitted, and a SUBMITTED document being
#: resubmittable is the double-routing this whole design refuses.
#:
#: Same pair as `approval_writeback._UNDECIDED_STATUSES` and as 009's
#: `ck_*_decision`. "Has not been decided yet" is one idea and should not be
#: spelled three different ways.
_DECIDABLE_STATUSES = frozenset({"DRAFT", "SUBMITTED"})


def _submission_outcome(session: Session, *, id_key: str, object_id: str,
                         instance: Mapping[str, Any],
                         label: str) -> dict[str, Any]:
    """Shape ``open_instance``'s two possible answers into one result.

    ``open_instance`` RETURNS rather than raises on an unroutable object,
    deliberately: it has already written an EXCEPTION_PENDING instance and an
    audit action explaining why, and raising would make the caller's rollback
    delete both. So this must not raise either. It reports the refusal in the
    returned value and leaves the transaction intact; the router commits, and
    only then turns ``refusal`` into a 409.
    """
    status = instance.get("status")
    instance_id = instance.get("instance_id")
    document_status = "SUBMITTED" if status == "OPEN" else "DRAFT"
    result: dict[str, Any] = {
        id_key: object_id,
        "status": document_status,
        "approval_instance_id": instance_id,
        "approval_status": status,
        "submitted": status == "OPEN",
        "refusal": None,
    }
    if status == "OPEN":
        # The document says so too, and this is the point of migration 009.
        #
        # Until 009 widened the domain, a routed document stayed DRAFT while
        # its instance was open -- the status ASSERTED SOMETHING FALSE, and
        # only `_assert_not_under_approval` stood between that row and a second
        # approval down the direct route. One guard, in one language, holding a
        # property the schema can state. It still holds it; it is no longer
        # holding it alone.
        #
        # DRAFT is kept for EXCEPTION_PENDING deliberately. SUBMITTED means
        # routed and progressing (plan §12: DRAFT -> SUBMITTED -> ...), and an
        # object held for an administrator is progressing through nothing. The
        # `live_approval_instance` guard still blocks a second submission and
        # the direct approval route for it, so nothing is loosened by leaving
        # the status honest about what did not happen.
        #
        # No `decided_at`/`decided_by` is written: `ck_*_decision` puts
        # SUBMITTED on the undecided side, because a submission is not a
        # decision.
        _touch_status(session, id_key=id_key, object_id=object_id,
                       new_status="SUBMITTED")
        result["current_stage_no"] = instance.get("current_stage_no")
        return result

    # EXCEPTION_PENDING. Name the reason: `_write_exception_instance` and the
    # two in-line exception branches all record it as the LAST approval_action
    # on the instance, in `outcome.code`.
    reason = session.fetchone(  # scope-exempt: reads the action just written for the instance created in this same call; returns a machine code, never business data
        "SELECT outcome FROM approval_action WHERE instance_id = %s "
        "ORDER BY seq DESC LIMIT 1",
        (instance_id,))
    code = "APPROVAL_ROUTE_UNRESOLVED"
    if reason is not None and reason[0]:
        outcome = reason[0]
        if isinstance(outcome, str):
            try:
                outcome = json.loads(outcome)
            except ValueError:
                outcome = {}
        if isinstance(outcome, Mapping) and outcome.get("code"):
            code = str(outcome["code"])
    result["refusal"] = {
        "code": code,
        "status": 409,
        "message": (
            f"{label} {object_id} could not be routed for approval: {code}. "
            f"Approval instance {instance_id} has been recorded as "
            f"EXCEPTION_PENDING and is held for an administrator. The "
            f"{label.lower()} is unchanged and is NOT approved."),
        "instance_id": instance_id,
    }
    return result


def submit_revision(session: Session, *, revision_id: str, actor: str,
                     correlation_id: str | None = None,
                     business_date: date | None = None) -> dict[str, Any]:
    """Raise a DRAFT revision into the configurable approval workflow.

    Takes NO cell lock and writes no cell: submitting creates no spending
    capacity, so there is nothing to serialise. ``open_instance`` runs in this
    same transaction, so either the instance exists and the revision is under
    approval, or neither is true.

    An unroutable revision comes back with ``refusal`` set and ``submitted``
    False. That is a recorded outcome, not an error: the caller must NOT roll
    back, or the EXCEPTION_PENDING evidence goes with it.
    """
    from . import approvals as approvals_mod   # deferred: approvals imports this module

    row = repo.query_one(
        session,
        """
        SELECT r.status, r.created_by, r.version_no
        FROM budget_revision r
        JOIN wbs_element w ON w.wbs_id = r.wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE r.revision_id = %(id)s AND {scope}
        """,
        {"id": revision_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    status, created_by, version_no = row
    # The live-instance check comes FIRST, and the order is the message.
    #
    # Since migration 009 a routed document is SUBMITTED, not DRAFT, so a
    # second submission now fails the status check too -- and would be
    # refused as "REVISION_NOT_DRAFT", which is true but useless: it names the
    # state without naming the cause. "ALREADY_SUBMITTED" names the instance the
    # caller is actually waiting on. A refusal should say the most
    # specific true thing, not the first true thing.
    live = live_approval_instance(session, OBJECT_TYPE_REVISION, revision_id)
    if live is not None:
        _err("ALREADY_SUBMITTED",
             f"Revision {revision_id} is already under approval instance "
             f"{live['instance_id']} ({live['status']}).", status=409)
    if status != "DRAFT":
        _err("REVISION_NOT_DRAFT",
             f"Revision {revision_id} is {status}, not DRAFT; only a draft can "
             f"be submitted for approval.", status=409)

    snapshot = revision_snapshot(session, revision_id=revision_id)

    # BEFORE open_instance, on purpose. `contributor_set` unions every actor on
    # the document's audit stream into the set no approver may be drawn from,
    # so writing this entry first is what makes a submitter who is not the
    # drafter ineligible to approve their own submission.
    audit_mod.append(session, actor, "REVISION_SUBMIT", OBJECT_TYPE_REVISION,
                      revision_id,
                      f"Revision submitted for approval by {actor}",
                      correlation_id=correlation_id)

    instance = approvals_mod.open_instance(
        session, object_type=OBJECT_TYPE_REVISION, object_id=revision_id,
        object_version=version_no, snapshot=snapshot, maker_user_id=created_by,
        business_date=business_date, correlation_id=correlation_id)
    return _submission_outcome(
        session, id_key="revision_id", object_id=revision_id,
        instance=instance, label="Revision")


def submit_transfer(session: Session, *, transfer_id: str, actor: str,
                     correlation_id: str | None = None,
                     business_date: date | None = None) -> dict[str, Any]:
    """As `submit_revision`, for a transfer."""
    from . import approvals as approvals_mod   # deferred: approvals imports this module

    row = repo.query_one(
        session,
        """
        SELECT t.status, t.created_by, t.version_no
        FROM budget_transfer t
        JOIN wbs_element w ON w.wbs_id = t.from_wbs_id
        JOIN project p ON p.project_id = w.project_id
        WHERE t.transfer_id = %(id)s AND {scope}
        """,
        {"id": transfer_id},
        columns=_CELL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    status, created_by, version_no = row
    # The live-instance check comes FIRST, and the order is the message.
    #
    # Since migration 009 a routed document is SUBMITTED, not DRAFT, so a
    # second submission now fails the status check too -- and would be
    # refused as "TRANSFER_NOT_DRAFT", which is true but useless: it names the
    # state without naming the cause. "ALREADY_SUBMITTED" names the instance the
    # caller is actually waiting on. A refusal should say the most
    # specific true thing, not the first true thing.
    live = live_approval_instance(session, OBJECT_TYPE_TRANSFER, transfer_id)
    if live is not None:
        _err("ALREADY_SUBMITTED",
             f"Transfer {transfer_id} is already under approval instance "
             f"{live['instance_id']} ({live['status']}).", status=409)
    if status != "DRAFT":
        _err("TRANSFER_NOT_DRAFT",
             f"Transfer {transfer_id} is {status}, not DRAFT; only a draft can "
             f"be submitted for approval.", status=409)

    snapshot = transfer_snapshot(session, transfer_id=transfer_id)
    audit_mod.append(session, actor, "TRANSFER_SUBMIT", OBJECT_TYPE_TRANSFER,
                      transfer_id,
                      f"Transfer submitted for approval by {actor}",
                      correlation_id=correlation_id)
    instance = approvals_mod.open_instance(
        session, object_type=OBJECT_TYPE_TRANSFER, object_id=transfer_id,
        object_version=version_no, snapshot=snapshot, maker_user_id=created_by,
        business_date=business_date, correlation_id=correlation_id)
    return _submission_outcome(
        session, id_key="transfer_id", object_id=transfer_id,
        instance=instance, label="Transfer")


# ==========================================================================
# Versions: SCR-10 comparison
# ==========================================================================
def create_version_snapshot(session: Session, *, project_id: str, label: str,
                             actor: str) -> dict:
    """Freeze every cell's OWN ``budget_paise`` for ``project_id`` into a new,
    numbered ``budget_version`` -- never a rollup (domain-controls.md:
    "never persist a rollup"). Called after a revision or transfer is
    approved so SCR-10 always has something fresh to compare against, and by
    the seed fragment for the initial baseline."""
    _assert_project_in_scope(session, project_id)
    version_id = _new_id("BV")

    # Serialise version minting per project.
    #
    # `MAX(version_no) + 1` is a read-then-write against
    # `UNIQUE (project_id, version_no)`, and nothing above serialises it: two
    # revisions in the same project on DIFFERENT budget heads hold disjoint
    # ancestor-chain lock sets, so `lock_affected_cells` does not make them
    # wait for each other. Both computed the same next number and the second
    # died on the unique index -- an unhandled 500 that rolled back a
    # legitimate approval, its budget line, its cell recompute and its audit
    # entry, and reported the loss as a server fault.
    #
    # An advisory lock keyed on the project, rather than `FOR UPDATE`: there
    # is no row to lock when a project has no versions yet, which is exactly
    # the first-approval case. It is transaction-scoped, so it releases on
    # commit or rollback without a code path to forget.
    #
    # It sits AFTER the cell locks and before the document row, so it does not
    # disturb the global order in locking.py's rule 3, and the key is
    # namespaced apart from the audit chain's.
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"capex.budget_version:{project_id}",))

    (next_no,) = session.fetchone(  # scope-exempt: project_id scope-cleared just above
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM budget_version WHERE project_id = %s",
        (project_id,))
    session.execute(
        "INSERT INTO budget_version (version_id, project_id, version_no, label, created_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (version_id, project_id, next_no, label, actor))
    session.execute(  # scope-exempt: project_id scope-cleared at the top of this call
        """
        INSERT INTO budget_version_cell (version_id, wbs_id, budget_head_id, budget_paise)
        SELECT %s, bc.wbs_id, bc.budget_head_id, bc.budget_paise
        FROM budget_control_cell bc
        JOIN wbs_element w ON w.wbs_id = bc.wbs_id
        WHERE w.project_id = %s
        """,
        (version_id, project_id),
    )
    return {"version_id": version_id, "project_id": project_id,
            "version_no": next_no, "label": label}


def list_versions(session: Session, project_id: str) -> list[dict]:
    rows = repo.query(
        session,
        """
        SELECT bv.version_no, bv.label, bv.created_at, COALESCE(SUM(vc.budget_paise), 0)::bigint
        FROM budget_version bv
        JOIN project p ON p.project_id = bv.project_id
        LEFT JOIN budget_version_cell vc ON vc.version_id = bv.version_id
        WHERE bv.project_id = %(project_id)s AND {scope}
        GROUP BY bv.version_id, bv.version_no, bv.label, bv.created_at
        ORDER BY bv.version_no
        """,
        {"project_id": project_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    return [
        {"version": version_no, "label": label, "created_at": _iso(created_at),
         "total_paise": total_paise}
        for version_no, label, created_at, total_paise in rows
    ]


def compare_versions(session: Session, *, project_id: str, left: int, right: int) -> list[dict]:
    # Both version lookups go through the scoped chokepoint. They previously
    # used `session.fetchone` directly, so any authenticated caller could name
    # any project_id and read its budget comparison -- and because the bypass
    # was `session.fetchone` rather than a raw `.execute(`, no gate caught it.
    #
    # Scope resolves BEFORE existence: an out-of-scope project returns the same
    # 404 as a project that does not exist, which is the not-found-over-
    # forbidden rule -- a 403 on a project id is an existence oracle.
    def _version_id(version_no: int) -> str:
        row = repo.query_one(
            session,
            """
            SELECT bv.version_id
            FROM budget_version bv
            JOIN project p ON p.project_id = bv.project_id
            WHERE bv.project_id = %(project_id)s
              AND bv.version_no = %(version_no)s
              AND {scope}
            """,
            {"project_id": project_id, "version_no": version_no},
            columns=_PROJECT_SCOPE_COLUMNS,
        )
        if row is None:
            _err("VERSION_NOT_FOUND",
                 f"No version {version_no} for project {project_id}.", status=404)
        return row[0]

    left_id, right_id = _version_id(left), _version_id(right)

    # The row set is keyed on two version_ids that scope has already cleared,
    # and budget_version_cell carries no scope columns of its own.
    rows = session.fetchall(  # scope-exempt: keyed on scope-cleared version ids
        """
        SELECT COALESCE(l.wbs_id, r.wbs_id), COALESCE(l.budget_head_id, r.budget_head_id),
               COALESCE(l.budget_paise, 0), COALESCE(r.budget_paise, 0)
        FROM (SELECT * FROM budget_version_cell WHERE version_id = %(left)s) l
        FULL OUTER JOIN (SELECT * FROM budget_version_cell WHERE version_id = %(right)s) r
            ON l.wbs_id = r.wbs_id AND l.budget_head_id = r.budget_head_id
        ORDER BY 1, 2
        """,
        {"left": left_id, "right": right_id},
    )
    return [
        {"wbs_id": wbs_id, "budget_head_id": head_id,
         "left_paise": left_paise, "right_paise": right_paise,
         "delta_paise": right_paise - left_paise}
        for wbs_id, head_id, left_paise, right_paise in rows
    ]
