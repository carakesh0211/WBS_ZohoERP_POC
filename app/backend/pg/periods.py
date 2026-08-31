"""Fiscal period state machine and the budget-roll at period open.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``,
"Fiscal periods": ``accounting_period`` states are ``FUTURE -> OPEN ->
SOFT_CLOSED -> CLOSED`` (forward-only; there is no reopen in this milestone),
``roll_period_effective_budget`` moves amounts between ``budget_paise`` and
``future_budget_paise`` at period open, one entity per invocation, and a
period cannot close while an Open reconciliation exception exists.

``reconciliation_exception`` does not exist yet in this schema -- it is a
later milestone's table. :func:`_has_open_reconciliation_exceptions` checks
for it via ``information_schema`` rather than assuming it, so the check is
"trivially satisfied now and correct when the table lands" (this stream's
brief), not a hard dependency on a table nobody has created.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from . import audit as audit_mod
from . import repo
from .budget import recompute_cell
from .engine import Session
from .locking import lock_affected_cells

#: Forward-only. There is no reopen path in this milestone.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "FUTURE": {"OPEN"},
    "OPEN": {"SOFT_CLOSED"},
    "SOFT_CLOSED": {"CLOSED"},
    "CLOSED": set(),
}

#: Expected shape once it lands: `entity_id` (scoping) and `status` (with an
#: 'Open' value). Checked against `information_schema` rather than caught via
#: exception handling, so a genuine query bug is never silently swallowed as
#: "table not there yet".
_RECONCILIATION_EXCEPTION_TABLE = "reconciliation_exception"

_PERIOD_SCOPE_COLUMNS = {"entity": "entity_id", "plant": None, "project": None, "location": None}


class PeriodServiceError(Exception):
    """Raised for every rejected period-service call. See
    `app.backend.pg.budget.BudgetServiceError` for the same shape and the
    same reasoning: ``code``/``status`` are what callers match on."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400) -> None:
    raise PeriodServiceError(code, message, status=status)


def _table_exists(session: Session, table_name: str) -> bool:
    row = session.fetchone(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table_name,))
    return row is not None


def _has_open_reconciliation_exceptions(session: Session, entity_id: str) -> bool:
    """True only if ``reconciliation_exception`` exists AND carries an Open
    row scoped to ``entity_id``. Vacuously False while the table does not
    exist -- see the module docstring."""
    if not _table_exists(session, _RECONCILIATION_EXCEPTION_TABLE):
        return False
    row = session.fetchone(
        f"SELECT 1 FROM {_RECONCILIATION_EXCEPTION_TABLE} "  # noqa: S608 -- fixed name, existence checked above
        f"WHERE entity_id = %s AND status = 'Open' LIMIT 1",
        (entity_id,))
    return row is not None


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row_to_period(row: tuple) -> dict[str, Any]:
    (period_id, entity_id, period_start, period_end, state, closed_at, closed_by) = row
    return {
        "period_id": period_id, "entity_id": entity_id,
        "period_start": _iso(period_start), "period_end": _iso(period_end),
        "state": state, "closed_at": _iso(closed_at), "closed_by": closed_by,
    }


def list_periods(session: Session, *, entity_id: str | None = None,
                  state: str | None = None) -> list[dict]:
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if entity_id is not None:
        conditions.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if state is not None:
        conditions.append("state = %(state)s")
        params["state"] = state
    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = repo.query(
        session,
        f"""
        SELECT period_id, entity_id, period_start, period_end, state, closed_at, closed_by
        FROM accounting_period
        WHERE {where} AND {{scope}}
        ORDER BY entity_id, period_start
        """,
        params,
        columns=_PERIOD_SCOPE_COLUMNS,
    )
    return [_row_to_period(r) for r in rows]


def _roll_cells_for_entity(session: Session, entity_id: str, as_of: date, actor: str) -> int:
    """Cells first, complete set, exactly once -- shared by
    :func:`transition_period` (opening a period) and the standalone
    :func:`roll_period_effective_budget` entry point, so both go through the
    same lock-then-recompute path rather than two copies that could drift.
    """
    affected = [
        (r[0], r[1]) for r in session.fetchall(  # scope-exempt: period roll must move EVERY cell in the entity, not only the caller's
            """
            SELECT bc.wbs_id, bc.budget_head_id
            FROM budget_control_cell bc
            JOIN wbs_element w ON w.wbs_id = bc.wbs_id
            JOIN project p ON p.project_id = w.project_id
            WHERE p.entity_id = %s
            """,
            (entity_id,))
    ]
    if affected:
        lock_affected_cells(session, affected)
    for wbs_id, head_id in affected:
        recompute_cell(session, wbs_id, head_id, as_of=as_of, actor=actor)
    return len(affected)


def transition_period(session: Session, *, period_id: str, to_state: str, actor: str) -> dict:
    if to_state not in _ALLOWED_TRANSITIONS:
        _err("UNKNOWN_STATE", f"{to_state!r} is not a recognised period state.")

    # Scoped, not a bare read: holding `period.transition` is authority over
    # the periods of the entities in your scope, not over every entity's. An
    # out-of-scope period answers 404, exactly as a non-existent one does.
    row = repo.query_one(
        session,
        """
        SELECT period_id, entity_id, period_start, period_end, state
        FROM accounting_period WHERE period_id = %(period_id)s AND {scope}
        """,
        {"period_id": period_id},
        columns=_PERIOD_SCOPE_COLUMNS,
    )
    if row is None:
        _err("PERIOD_NOT_FOUND", f"Period {period_id} does not exist.", status=404)
    _pid, entity_id, period_start, _period_end, current_state = row

    legal = _ALLOWED_TRANSITIONS.get(current_state, set())
    if to_state not in legal:
        _err("ILLEGAL_TRANSITION",
             f"Period {period_id} is {current_state}; {current_state} -> {to_state} "
             f"is not a legal transition. Legal moves: {sorted(legal) or 'none (terminal state)'}.",
             status=409)

    if to_state == "CLOSED" and _has_open_reconciliation_exceptions(session, entity_id):
        _err("PERIOD_HAS_OPEN_EXCEPTIONS",
             f"Period {period_id} cannot close: entity {entity_id} has at least "
             f"one Open reconciliation exception.", status=409)

    # Rule 1: cells first, complete set, exactly once -- ONLY when this
    # transition also rolls the budget (opening a period). Every other
    # transition touches no cell, so no cell lock is taken for it.
    rolled = 0
    if to_state == "OPEN":
        rolled = _roll_cells_for_entity(session, entity_id, period_start, actor)

    # Document row -- second in the global order.
    session.execute(  # scope-exempt: locks the period already scope-gated at the top of this call
        "SELECT period_id FROM accounting_period WHERE period_id = %s FOR UPDATE",
        (period_id,))
    # Re-verify under the lock: another transaction could have moved this
    # period between the unlocked read above and here.
    current_state_locked = session.fetchone(  # scope-exempt: re-reads the row just locked above
        "SELECT state FROM accounting_period WHERE period_id = %s", (period_id,))[0]
    if current_state_locked != current_state:
        _err("ILLEGAL_TRANSITION",
             f"Period {period_id} moved to {current_state_locked} while this "
             f"transition was in flight; retry against its current state.",
             status=409)

    now = datetime.now(timezone.utc)
    if to_state == "CLOSED":
        session.execute(
            "UPDATE accounting_period SET state = %s, closed_at = %s, closed_by = %s "
            "WHERE period_id = %s",
            (to_state, now, actor, period_id))
    else:
        session.execute(
            "UPDATE accounting_period SET state = %s WHERE period_id = %s",
            (to_state, period_id))

    audit_mod.append(
        session, actor, "PERIOD_TRANSITION", "ACCOUNTING_PERIOD", period_id,
        f"{current_state} -> {to_state}" + (f"; rolled {rolled} cell(s)" if rolled else ""))

    return {"period_id": period_id, "state": to_state, "transitioned_at": now.isoformat()}


def roll_period_effective_budget(session: Session, period_id: str, *,
                                  actor: str = "SYSTEM") -> dict:
    """Move amounts between ``budget_paise`` and ``future_budget_paise`` for
    every cell under ``period_id``'s entity, as of the period's own start
    date. One entity per invocation.

    Idempotent: :func:`~app.backend.pg.budget.recompute_cell` always
    recomputes fully from ``budget_line`` history rather than adjusting
    incrementally, so calling this twice for the same period with no
    intervening change produces the same result both times.
    """
    row = session.fetchone(  # scope-exempt: job entry point, run by a SERVICE principal, not a user request
        "SELECT entity_id, period_start FROM accounting_period WHERE period_id = %s",
        (period_id,))
    if row is None:
        _err("PERIOD_NOT_FOUND", f"Period {period_id} does not exist.", status=404)
    entity_id, period_start = row

    cells_recomputed = _roll_cells_for_entity(session, entity_id, period_start, actor)

    audit_mod.append(
        session, actor, "PERIOD_BUDGET_ROLL", "ACCOUNTING_PERIOD", period_id,
        f"Rolled effective budget for entity {entity_id}: {cells_recomputed} cell(s) "
        f"recomputed as of {period_start}")

    return {"period_id": period_id, "entity_id": entity_id, "cells_recomputed": cells_recomputed}
