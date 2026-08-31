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
"""
from __future__ import annotations

import base64
import binascii
import uuid
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

_CELL_SCOPE_COLUMNS = {"project": "w.project_id", "entity": None, "plant": None, "location": None}
_PROJECT_SCOPE_COLUMNS = {"project": "p.project_id", "entity": None, "plant": None, "location": None}


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

    Callers must call :func:`~app.backend.pg.locking.lock_affected_cells` for
    the affected ancestor chain before calling this (every mutating function
    in this module and in ``periods.py`` does). This function additionally
    writes each table with a SINGLE ``UPDATE ... SET x = (SELECT ...)``
    statement rather than a separate read-then-write: `lock_affected_cells`
    only locks ancestor-or-self cells that ALREADY own budget
    (``budget_paise <> 0``), so a cell receiving its first-ever money is not
    yet an "owner" by that definition and would not be covered by it -- and
    domain-controls.md forbids taking any *additional* cell lock after
    `lock_affected_cells` returns. Folding the read and the write into one
    atomic statement closes that gap for free: an ``UPDATE`` always takes its
    target row's lock as part of executing, subquery included, regardless of
    whether the row was pre-locked, so two concurrent recomputes of the same
    cell still serialise correctly with no additional, differently-ordered
    lock ever taken.

    Idempotent: recomputing twice for the same ``as_of`` with no intervening
    change to ``budget_line`` produces the same numbers both times.
    """
    as_of = as_of or date.today()
    params = {"wbs_id": wbs_id, "head": budget_head_id, "as_of": as_of, "actor": actor}

    session.execute(
        """
        UPDATE budget_control_cell SET
            budget_paise = COALESCE((
                SELECT SUM(amount_paise) FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND effective_from <= %(as_of)s
            ), 0),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
        """,
        params,
    )
    session.execute(
        """
        UPDATE budget_ledger_cell SET
            original_paise = COALESCE((
                SELECT SUM(amount_paise) FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND kind = 'ORIGINAL' AND effective_from <= %(as_of)s
            ), 0),
            revisions_paise = COALESCE((
                SELECT SUM(amount_paise) FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND kind <> 'ORIGINAL' AND effective_from <= %(as_of)s
            ), 0),
            future_budget_paise = COALESCE((
                SELECT SUM(amount_paise) FROM budget_line
                WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
                  AND status = 'Approved' AND effective_from > %(as_of)s
            ), 0),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
        """,
        params,
    )
    row = session.fetchone(
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
    row = session.fetchone(
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
    row = session.fetchone(
        """
        SELECT
            COALESCE(SUM(bc.budget_paise), 0),
            COALESCE(SUM(bl.commitment_paise), 0),
            COALESCE(SUM(bl.actual_paise), 0),
            COALESCE(SUM(bl.pr_reserved_paise), 0)
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

    if session.fetchone("SELECT 1 FROM wbs_element WHERE wbs_id = %s", (wbs_id,)) is None:
        _err("WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.", status=404)

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
        pct_after = (
            round((totals["exposure_paise"] + amount_paise) / totals["budget_paise"] * 100.0, 1)
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
        WHERE {where} AND {{scope}}
        ORDER BY bl.created_at DESC, bl.budget_line_id
        """,
        params,
        columns=_CELL_SCOPE_COLUMNS,
    )
    return [_line_row_to_dict(r) for r in rows]


def _project_id_for_wbs(session: Session, wbs_id: str) -> str | None:
    row = session.fetchone("SELECT project_id FROM wbs_element WHERE wbs_id = %s", (wbs_id,))
    return row[0] if row else None


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
    if session.fetchone(
        "SELECT 1 FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
        (wbs_id, budget_head_id),
    ) is None:
        _err("CELL_NOT_FOUND",
             f"No control cell exists for ({wbs_id}, {budget_head_id}).", status=404)

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


def approve_revision(session: Session, *, revision_id: str, actor: str) -> dict:
    row = session.fetchone(
        "SELECT wbs_id, budget_head_id, delta_paise, effective_from, justification, "
        "status, created_by FROM budget_revision WHERE revision_id = %s",
        (revision_id,))
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    (wbs_id, head_id, delta_paise, effective_from, justification,
     status, created_by) = row
    if status != "DRAFT":
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
    session.execute(
        "SELECT revision_id FROM budget_revision WHERE revision_id = %s FOR UPDATE",
        (revision_id,))
    # Re-read status under the lock: another transaction could have decided
    # this revision between the unlocked read above and this point.
    status = session.fetchone(
        "SELECT status FROM budget_revision WHERE revision_id = %s", (revision_id,))[0]
    if status != "DRAFT":
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
                     reason: str | None = None) -> dict:
    row = session.fetchone(
        "SELECT status, created_by FROM budget_revision WHERE revision_id = %s FOR UPDATE",
        (revision_id,))
    if row is None:
        _err("REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.", status=404)
    status, _created_by = row
    if status != "DRAFT":
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
        if session.fetchone(
            "SELECT 1 FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (cell_wbs_id, cell_head_id),
        ) is None:
            _err("CELL_NOT_FOUND",
                 f"No control cell exists for the {role} cell ({cell_wbs_id}, {cell_head_id}).",
                 status=404)

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


def approve_transfer(session: Session, *, transfer_id: str, actor: str) -> dict:
    row = session.fetchone(
        "SELECT from_wbs_id, from_head_id, to_wbs_id, to_head_id, amount_paise, "
        "effective_from, justification, status, created_by "
        "FROM budget_transfer WHERE transfer_id = %s", (transfer_id,))
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    (from_wbs, from_head, to_wbs, to_head, amount_paise, effective_from,
     justification, status, created_by) = row
    if status != "DRAFT":
        _err("TRANSFER_NOT_DRAFT", f"Transfer {transfer_id} is {status}, not DRAFT.", status=409)
    if actor == created_by:
        _err("SELF_APPROVAL_FORBIDDEN",
             "The approver of a budget transfer must not be the same user who "
             "drafted it.", status=403)

    # Rule 1: cells first, complete set, exactly once -- BOTH legs, since a
    # transfer moves money out of one cell's ancestor chain and into another's.
    lock_affected_cells(session, [(from_wbs, from_head), (to_wbs, to_head)])

    # Document row -- second in the global order.
    session.execute(
        "SELECT transfer_id FROM budget_transfer WHERE transfer_id = %s FOR UPDATE",
        (transfer_id,))
    status = session.fetchone(
        "SELECT status FROM budget_transfer WHERE transfer_id = %s", (transfer_id,))[0]
    if status != "DRAFT":
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
    for project_id in {p for p in (from_project, to_project) if p}:
        v = create_version_snapshot(
            session, project_id=project_id,
            label=f"After transfer {transfer_id} approved", actor=actor)
        version_note += f"; snapshot v{v['version_no']} taken for {project_id}"

    audit_mod.append(session, actor, "TRANSFER_APPROVE", "BUDGET_TRANSFER", transfer_id,
                      f"Transfer approved: {amount_paise} paise from "
                      f"{from_wbs}:{from_head} to {to_wbs}:{to_head}{version_note}")
    return {"transfer_id": transfer_id, "status": "APPROVED", "amount_paise": amount_paise}


def reject_transfer(session: Session, *, transfer_id: str, actor: str,
                     reason: str | None = None) -> dict:
    row = session.fetchone(
        "SELECT status FROM budget_transfer WHERE transfer_id = %s FOR UPDATE",
        (transfer_id,))
    if row is None:
        _err("TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist.", status=404)
    (status,) = row
    if status != "DRAFT":
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
# Versions: SCR-10 comparison
# ==========================================================================
def create_version_snapshot(session: Session, *, project_id: str, label: str,
                             actor: str) -> dict:
    """Freeze every cell's OWN ``budget_paise`` for ``project_id`` into a new,
    numbered ``budget_version`` -- never a rollup (domain-controls.md:
    "never persist a rollup"). Called after a revision or transfer is
    approved so SCR-10 always has something fresh to compare against, and by
    the seed fragment for the initial baseline."""
    version_id = _new_id("BV")
    (next_no,) = session.fetchone(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM budget_version WHERE project_id = %s",
        (project_id,))
    session.execute(
        "INSERT INTO budget_version (version_id, project_id, version_no, label, created_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (version_id, project_id, next_no, label, actor))
    session.execute(
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
        SELECT bv.version_no, bv.label, bv.created_at, COALESCE(SUM(vc.budget_paise), 0)
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
    left_row = session.fetchone(
        "SELECT version_id FROM budget_version WHERE project_id = %s AND version_no = %s",
        (project_id, left))
    if left_row is None:
        _err("VERSION_NOT_FOUND", f"No version {left} for project {project_id}.", status=404)
    right_row = session.fetchone(
        "SELECT version_id FROM budget_version WHERE project_id = %s AND version_no = %s",
        (project_id, right))
    if right_row is None:
        _err("VERSION_NOT_FOUND", f"No version {right} for project {project_id}.", status=404)
    left_id, right_id = left_row[0], right_row[0]

    rows = session.fetchall(
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
