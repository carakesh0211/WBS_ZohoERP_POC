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

#: All four dimensions, expressed through the entity's projects.
#:
#: `accounting_period` carries `entity_id` and nothing else, so plant, project
#: and location are not columns on it. They were previously OMITTED from this
#: mapping so that `compile_scope` would REFUSE a caller restricted on one of
#: them rather than silently widen. That was correct, and fail-closed, while
#: no principal ever carried a resolved restriction.
#:
#: Once Wave 3 wired real grants into the routers, principals did -- and the
#: refusal became an uncaught `ScopeNotExpressible`, which is a RuntimeError
#: no route and no app-level handler catches. Three of the nine seeded demo
#: users got a 500 on a plain read of the period list. The refusal was never
#: wrong; there was simply nothing to refuse until there was.
#:
#: Expressing the dimensions is better than refusing them. A period belongs to
#: an entity, and an entity's projects carry all four, so a period is visible
#: when ANY project in its entity is visible to the caller -- the same
#: join-one-step-further shape migration 006 uses for the budget documents.
#:
#: Consequence worth stating plainly: an entity with no projects has periods
#: no restricted caller can see. Nothing can be posted into such an entity, so
#: there is nothing to reconcile there -- but it is a behaviour change, not a
#: nuance, and a `read_all` principal still sees them.
_PERIOD_SCOPE_COLUMNS = {
    "entity": "sp.entity_id",
    "plant": "sp.plant_id",
    "project": "sp.project_id",
    "location": "sp.location_id",
}

#: The correlated subquery the mapping above reads through.
#:
#: `EXISTS`, not a join in the FROM clause: a period must come back once
#: however many of its entity's projects match. A plain join multiplies rows
#: per project, which would silently corrupt both the row count and any
#: pagination built on it.
_PERIOD_SCOPE_EXISTS = (
    "EXISTS (SELECT 1 FROM project sp "
    "WHERE sp.entity_id = accounting_period.entity_id AND {scope})"
)

#: The direct form, used when nothing needs the project join.
_PERIOD_DIRECT_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id", "plant": None, "project": None, "location": None,
}
_PERIOD_DIRECT_SQL = "{scope}"

#: Dimensions `accounting_period` can only reach through `project`.
_VIA_PROJECT = ("plant_ids", "project_ids", "location_ids")


def period_scope_sql_and_columns(scope) -> tuple[str, dict[str, str | None]]:
    """Which scope form this caller needs, and the columns it reads through.

    The project join is required ONLY when the caller is actually restricted
    on a dimension `accounting_period` cannot reach directly. Applying it
    unconditionally was wrong in a way live PostgreSQL caught and no
    database-free test could: `EXISTS (SELECT 1 FROM project ...)` demands at
    least one project row in the entity even when the inner predicate is
    `TRUE`, so a period in an entity with no projects became invisible to
    EVERY caller -- including `read_all`. The restriction was correct; making
    it unconditional turned it into a data-shape dependency.

    So: unrestricted on all three, or `read_all`, and the period is filtered
    directly on its own `entity_id`, with the other three waived. Waiving a
    dimension the caller is not restricted on is a no-op, not a widening.
    Restricted on any of the three, and the join is required and enforced.
    """
    if scope.read_all or all(
            getattr(scope, field) is None for field in _VIA_PROJECT):
        return _PERIOD_DIRECT_SQL, _PERIOD_DIRECT_COLUMNS
    return _PERIOD_SCOPE_EXISTS, _PERIOD_SCOPE_COLUMNS


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


class ReconciliationGateUnavailable(RuntimeError):
    """The open-exception check could not be evaluated, so the close is refused.

    Raised rather than returning a boolean, because there is no boolean that
    is honest here. See :func:`_has_open_reconciliation_exceptions`.
    """


def _has_open_reconciliation_exceptions(session: Session, entity_id: str) -> bool:
    """True if ``reconciliation_exception`` carries an Open row for `entity_id`.

    RAISES when the table is absent. It does not return False.

    It used to, with the docstring "vacuously False while the table does not
    exist". That reasoning is the defect: `False` is not a neutral answer from
    this function, it is the PERMISSIVE one. `transition_period` reads it as
    "nothing blocks this close", and `and` short-circuits, so the whole gate
    became unreachable. The table exists in no migration -- `010_integration.sql`
    records that at its own line 196 -- so on the shipped schema this control
    could never fire even once.

    Section 11.8 is explicit that an Open exception blocks the close, and the
    scenario it exists for is precisely this one: a sweep raises
    GRN_LINE_UNATTRIBUTED for a real sum, there is nowhere to write it, finance
    closes the period, and CWIP publishes a number nobody can stand behind.

    A control that cannot be evaluated must refuse, not proceed.
    `integration/outbound.py` already does exactly this -- it raises
    `DetectiveControlUnavailable` rather than reporting zero unsanctioned
    commitments -- and the two now agree.
    """
    if not _table_exists(session, _RECONCILIATION_EXCEPTION_TABLE):
        raise ReconciliationGateUnavailable(
            f"cannot close: {_RECONCILIATION_EXCEPTION_TABLE!r} does not exist, "
            f"so whether this entity has open reconciliation exceptions is "
            f"UNKNOWN. Section 11.8 blocks a close on an open exception, and a "
            f"gate that cannot be evaluated must refuse rather than permit. "
            f"Create the table (it is referenced by pg/periods.py and by the "
            f"integration sweeps) and re-run the close.")
    row = session.fetchone(  # scope-exempt: table name is a module constant, and entity_id comes from a period already scope-gated by transition_period
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

    scope_sql, scope_columns = period_scope_sql_and_columns(session.scope)
    rows = repo.query(
        session,
        f"""
        SELECT period_id, entity_id, period_start, period_end, state, closed_at, closed_by
        FROM accounting_period
        WHERE {where} AND {scope_sql}
        ORDER BY entity_id, period_start
        """,
        params,
        columns=scope_columns,
    )
    return [_row_to_period(r) for r in rows]


def _roll_cells_for_entity(session: Session, entity_id: str, as_of: date, actor: str) -> int:
    """Cells first, complete set, exactly once -- shared by
    :func:`transition_period` (opening a period) and the standalone
    :func:`roll_period_effective_budget` entry point, so both go through the
    same lock-then-recompute path rather than two copies that could drift.

    This is the sharpest case in the application for the lock rule, and it was
    the one the pre-Wave-3 rule handled worst:

    * It recomputes EVERY cell in the entity, so the affected set is the whole
      entity, not a document's line items.
    * At the moment a roll exists for -- opening a period whose budget is all
      still future-dated -- every ``budget_paise`` is ``0``. The old lock query
      filtered on ``budget_paise <> 0``, so the lock set was **empty**: the
      entire roll ran having taken no cell lock at all, and
      ``session.locks_taken`` stayed empty, which made any ordering assertion
      over it pass vacuously.
    * The driving ``SELECT`` had no ``ORDER BY``, so the recompute order --
      and with it the order those implicit ``UPDATE`` locks were acquired in
      -- was whatever the planner produced. That is the one thing the
      deadlock-freedom proof cannot tolerate.

    Both are closed here. The driving ``SELECT`` now orders by
    ``(w.wbs_path, bc.budget_head_id)``, the same total order
    :func:`~app.backend.pg.locking.lock_affected_cells` acquires in, so the
    recompute walk follows the lock walk; and because the lock set no longer
    filters on ``budget_paise``, every one of these cells is in it. The
    equality is then **asserted** rather than assumed -- an entity whose cells
    exist but do not all get locked is a violation of rule 2, and this is the
    call site with the most to lose from it going unnoticed.
    """
    # ORDER BY is load-bearing, not cosmetic: it is the lock order.
    affected = [
        (r[0], r[1]) for r in session.fetchall(  # scope-exempt: period roll must move EVERY cell in the entity, not only the caller's
            """
            SELECT bc.wbs_id, bc.budget_head_id
            FROM budget_control_cell bc
            JOIN wbs_element w ON w.wbs_id = bc.wbs_id
            JOIN project p ON p.project_id = w.project_id
            WHERE p.entity_id = %s
            ORDER BY w.wbs_path, bc.budget_head_id
            """,
            (entity_id,))
    ]
    if not affected:
        return 0

    # Rule 1: cells first, complete set, exactly once.
    locked = set(lock_affected_cells(session, affected))

    # Rule 2, enforced rather than trusted. Every affected cell was read out of
    # `budget_control_cell` a statement ago, and a cell is its own ancestor, so
    # each one must come back in the lock set. If any does not, the driving
    # query and the lock query disagree about what exists -- and the recompute
    # below would then take that row's UPDATE lock outside the declared set,
    # which is exactly the defect this function exists to have closed. Refuse
    # instead: an unrolled period is recoverable, a silently unlocked roll is
    # not.
    missing = [cell for cell in affected if cell not in locked]
    if missing:
        _err("LOCK_SET_INCOMPLETE",
             f"Period roll for entity {entity_id} would recompute "
             f"{len(missing)} cell(s) the lock set does not cover "
             f"(first: {missing[0]}). Refusing to write cells this "
             f"transaction does not hold locks on.",
             status=500)

    for wbs_id, head_id in affected:
        recompute_cell(session, wbs_id, head_id, as_of=as_of, actor=actor)
    return len(affected)


def transition_period(session: Session, *, period_id: str, to_state: str, actor: str) -> dict:
    if to_state not in _ALLOWED_TRANSITIONS:
        _err("UNKNOWN_STATE", f"{to_state!r} is not a recognised period state.")

    # Scoped, not a bare read: holding `period.transition` is authority over
    # the periods of the entities in your scope, not over every entity's. An
    # out-of-scope period answers 404, exactly as a non-existent one does.
    scope_sql, scope_columns = period_scope_sql_and_columns(session.scope)
    row = repo.query_one(
        session,
        "SELECT period_id, entity_id, period_start, period_end, state "
        "FROM accounting_period WHERE period_id = %(period_id)s AND "
        + scope_sql,
        {"period_id": period_id},
        columns=scope_columns,
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
