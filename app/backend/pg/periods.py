"""Fiscal period state machine, the budget-roll at period open, and the
approval-gated reopening of a closed period.

See ``.claude/skills/wbs-full-app-builder/references/domain-controls.md``,
"Fiscal periods": ``accounting_period`` states are ``FUTURE -> OPEN ->
SOFT_CLOSED -> CLOSED``, ``roll_period_effective_budget`` moves amounts between
``budget_paise`` and ``future_budget_paise`` at period open, one entity per
invocation, and a period cannot close while an Open reconciliation exception
exists.

REOPENING (AUD-C-008 residual, migration 023). The ordinary state machine is
still forward-only and CLOSED is still terminal within it -- see
:data:`_ALLOWED_TRANSITIONS`, which is deliberately NOT widened. A reopen is
not a transition a caller may ask :func:`transition_period` for; it is its own
two-step path, :func:`request_period_reopen` then :func:`apply_period_reopen`,
gated on an APPROVED ``approval_instance`` bound to that very period and
refused when the approver or the applier is the person who closed it.

Why it exists at all, given "there is no reopen in this milestone" was a
defensible position: a control that does not exist cannot be defeated, but a
period closed a day early still has to be reopened by somebody, and the shape
that arrives when the product has no path for it is a hand-run UPDATE against
the state column -- which no approval gates, no scope filters and no audit
chain records.

``reconciliation_exception`` does not exist yet in this schema -- it is a
later milestone's table. :func:`_has_open_reconciliation_exceptions` checks
for it via ``information_schema`` rather than assuming it, so the check is
"trivially satisfied now and correct when the table lands" (this stream's
brief), not a hard dependency on a table nobody has created.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from .. import auth as auth_mod
from . import audit as audit_mod
from . import repo
from .approval_rules import ACTION_APPROVE, INST_APPROVED
from .budget import recompute_cell
from .engine import Session
from .locking import lock_affected_cells

#: Forward-only, and CLOSED stays terminal HERE even though migration 023 adds
#: a reopen path.
#:
#: WIDENING THIS MAPPING WITH ``"CLOSED": {"OPEN"}`` WOULD HAVE BEEN THE WHOLE
#: FEATURE AND WOULD ALSO HAVE BEEN THE DEFECT. `transition_period` is reached
#: from `POST /api/budget/periods/{id}/transition` behind the
#: `period.transition` permission, which two roles hold; adding CLOSED -> OPEN
#: to this table would make reopening a closed period exactly as easy as
#: soft-closing an open one, gated by a permission and nothing else. AUD-C-008
#: is that a reopen must be gated by an APPROVED APPROVAL INSTANCE, and an
#: approval cannot be expressed as an entry in a state table.
#:
#: So the reopen is a separate entry point with its own request row, its own
#: approval binding and its own separation-of-duties check, and this mapping is
#: left saying what it has always said. `apply_period_reopen` is the ONLY
#: writer of `state = 'OPEN'` over a CLOSED period, and
#: `tests/test_pg_periods_reopen.py` holds that.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "FUTURE": {"OPEN"},
    "OPEN": {"SOFT_CLOSED"},
    "SOFT_CLOSED": {"CLOSED"},
    "CLOSED": set(),
}

#: The approval object type a reopen instance must be bound to.
#:
#: An instance for ANOTHER object cannot authorise this one: `object_type` and
#: `object_id` are both checked, so an approval granted for a purchase request
#: -- or for a DIFFERENT period -- is refused rather than counted.
REOPEN_OBJECT_TYPE = "ACCOUNTING_PERIOD"

#: The permission name the reopen path passes to `auth.require_separation`.
#:
#: Fable 5.1 / M-3: this is the CHECKER's permission, `period.reopen.apply`,
#: and it is registered in `auth.MAKER_CHECKER`, so the call in
#: :func:`_refuse_self_approval` now compares rather than returning early.
#: The requester's own permission (`period.reopen`) is not a maker-checker
#: key -- raising a request is the maker's act, and the maker is not checked
#: against themselves.
REOPEN_PERMISSION = "period.reopen.apply"

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
    """True if an Open ``reconciliation_exception`` blocks this entity's close.

    Open AND (attributed to `entity_id` OR attributed to NOBODY). The second
    class is not an edge case -- see the comment on the query itself.

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
    # TWO CLASSES OF OPEN EXCEPTION BLOCK THIS CLOSE, NOT ONE.
    #
    # `WHERE entity_id = %s` alone was the whole gate, and 011 makes
    # `reconciliation_exception.entity_id` NULLABLE (011:68) precisely because
    # "some exceptions are raised before the owning entity or project is
    # known". SQL NULL is not equal to anything, so `NULL = 'ENT-1'` is NULL
    # and never TRUE: an UNATTRIBUTED Open exception matched no row here and
    # the close proceeded. Reproduced with one Open unattributed exception
    # worth Rs 1,20,00,000 -- this gate said False, and `closure.py`'s gate on
    # the same table said the capitalisation was blocked.
    #
    # That is the exact scenario this function's own docstring names: a sweep
    # raises GRN_LINE_UNATTRIBUTED for a real sum, nobody can attribute it,
    # finance closes the period, and CWIP publishes a number nobody can stand
    # behind. An exception nobody can attribute is not an exception nobody has
    # to clear.
    #
    # `closure.py:_reconciliation_exposure` counts the same two classes for
    # capitalisation, and migration 012's header (012:24) documents the trap
    # and gives unattributed rows their own `WHEN entity_id IS NULL` RLS
    # branch so they stay visible to whoever can resolve them. The period gate
    # and the capitalisation gate now agree, which is the only way §11.8 can
    # mean one thing.
    row = session.fetchone(  # scope-exempt: table name is a module constant, entity_id comes from a period already scope-gated by transition_period, and an unattributed row matches NO scope predicate at all -- a scoped count would be structurally always zero and this gate would never fire
        f"SELECT 1 FROM {_RECONCILIATION_EXCEPTION_TABLE} "  # noqa: S608 -- fixed name, existence checked above
        f"WHERE status = 'Open' AND (entity_id = %s OR entity_id IS NULL) "
        f"LIMIT 1",
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


# ===========================================================================
# AUD-C-008 residual: reopening a CLOSED period
#
# Two steps, not one, and the split is the control rather than ceremony.
# A request NAMES the approval instance it will be authorised by and FREEZES
# the identity of whoever closed the period; the apply demands that instance be
# APPROVED and refuses if either the approver or the applier is that identity.
# Collapsing the two would mean the separation check read
# `accounting_period.closed_by` at apply time -- a column the NEXT close
# overwrites, which is not an identity a control can be checked against.
# ===========================================================================
def _new_reopen_id() -> str:
    return f"RO-{uuid.uuid4().hex[:16].upper()}"


def _refuse_self_approval(actor: str, closed_by: str, *, what: str,
                          period_id: str) -> None:
    """Segregation of duties on the reopen path: the closer is not the checker.

    TWO LAYERS, AND BOTH FIRE.

    `auth.require_separation` is the product's own maker-checker and is REUSED
    here rather than reimplemented -- it is called below with
    :data:`REOPEN_PERMISSION` (``period.reopen.apply``, registered in
    ``auth.MAKER_CHECKER`` under Fable 5.1 / M-3) and ``require_maker=True``.
    Until that registration, its first line

        if permission not in MAKER_CHECKER:
            return

    made the call return having compared nobody -- the exact shape of the
    ``bill.void`` defect `require_separation`'s own docstring recounts. A
    control whose only limb is a lookup in a set that does not contain its key
    is a control that has never run, and a test asserting "it did not raise"
    would pass for the wrong reason.

    So the refusal below is UNCONDITIONAL and lives here, and it STAYS even
    now that the first layer is live. It is not a second maker-checker
    implementation -- it takes no permission, consults no role table and
    knows nothing about approval routing. It is one comparison, the same one
    ``ck_period_reopen_separation`` and ``ck_period_reopen_applier_separation``
    make as CHECK constraints in migration 023, so the rule holds in three
    places: `auth.require_separation`, this function, and the database.
    `tests/test_pg_periods_reopen.py` neutralises the first and requires the
    second to refuse anyway.

    This function compares the applier against the CLOSER. The route layer
    (`api/budget.py`) separately compares the applier against the REQUESTER,
    through the same `require_separation`, before this engine is reached.

    BOTH LAYERS RAISE THE SAME EXCEPTION TYPE. Now that the first layer is
    live, it raises before the second layer's own comparison ever runs (found
    by this suite's first live-PostgreSQL run: `test_the_closer_may_not_apply_
    the_reopening_either` got a raw `auth.AuthError` instead of the
    `PeriodServiceError` every other refusal in this module raises).
    `auth.require_separation`'s `AuthError` and this function's own `_err` are
    the same rule stated twice, in two modules with two different exception
    hierarchies, so the first is caught here and re-raised through `_err` with
    its own code, message and status carried over verbatim -- a caller of
    this module matches on `PeriodServiceError.code` regardless of which of
    the two layers actually fired.
    """
    principal = {"user_id": actor}
    # Reused, not reimplemented. Live: REOPEN_PERMISSION is in
    # auth.MAKER_CHECKER -- see this function's docstring.
    try:
        auth_mod.require_separation(
            principal, REOPEN_PERMISSION, closed_by,
            object_label=f"the close of period {period_id}", require_maker=True)
    except auth_mod.AuthError as exc:
        _err(exc.code, exc.message, status=exc.status)

    if actor == closed_by:
        _err("SELF_APPROVAL",
             f"{actor} closed period {period_id} and may not also {what} its "
             f"reopening. Segregation of duties requires an independent "
             f"party; ck_period_reopen_separation refuses the same pairing at "
             f"the database.",
             status=403)


def _load_period_for_reopen(session: Session, period_id: str) -> tuple:
    """The period, SCOPED, with the columns the reopen path needs.

    Scoped exactly as `transition_period` scopes its read, and for the same
    reason: authority over reopening is authority over the periods of the
    entities in your scope, and an out-of-scope period answers 404 exactly as a
    non-existent one does.
    """
    scope_sql, scope_columns = period_scope_sql_and_columns(session.scope)
    row = repo.query_one(
        session,
        "SELECT period_id, entity_id, state, closed_at, closed_by, "
        "reopen_count FROM accounting_period "
        "WHERE period_id = %(period_id)s AND " + scope_sql,
        {"period_id": period_id},
        columns=scope_columns,
    )
    if row is None:
        _err("PERIOD_NOT_FOUND", f"Period {period_id} does not exist.", status=404)
    return row


def _load_reopen_request(session: Session, reopen_id: str) -> tuple:
    row = repo.query_one(
        session,
        """
        SELECT reopen_id, period_id, entity_id, approval_instance_id,
               idempotency_key, closed_by_at_request, requested_by, status,
               approved_by, applied_by, applied_at
        FROM period_reopen_request
        WHERE reopen_id = %(reopen_id)s AND {scope}
        """,
        {"reopen_id": reopen_id},
        columns={"entity": "entity_id", "plant": None,
                 "location": None, "project": None},
    )
    if row is None:
        _err("REOPEN_REQUEST_NOT_FOUND",
             f"No reopen request {reopen_id}.", status=404)
    return row


def reopen_request_requester(session: Session, reopen_id: str) -> str:
    """Who raised this reopen request -- the MAKER the route layer checks the
    applier against.

    Fable 5.1 / M-3. Read through the same scoped query as the engine, so a
    request the caller cannot see is 404 here exactly as it is at apply time;
    the route never learns that an out-of-scope request exists by asking who
    made it. Returned as recorded: `requested_by` is `NOT NULL` in 023, and a
    row that somehow carried a blank one is handed to
    `auth.require_separation(require_maker=True)`, which refuses an unknown
    maker rather than waving it through.
    """
    row = _load_reopen_request(session, reopen_id)
    return str(row[6] or "")


def _approval_instance_for(session: Session, instance_id: str,
                           period_id: str) -> tuple[str, str]:
    """``(status, entity_id)`` for an instance BOUND TO THIS PERIOD.

    An approval is authority over the object it names and nothing else. Both
    `object_type` and `object_id` are checked here, so an APPROVED instance for
    a purchase request, or for a different period, is refused rather than
    counted -- which is the whole difference between "gated on an approval" and
    "gated on the existence of an approval somewhere".
    """
    row = repo.query_one(
        session,
        """
        SELECT status, entity_id, object_type, object_id
        FROM approval_instance
        WHERE instance_id = %(instance_id)s AND {scope}
        """,
        {"instance_id": instance_id},
        columns={"entity": "entity_id", "plant": None,
                 "location": None, "project": "project_id"},
    )
    if row is None:
        _err("APPROVAL_INSTANCE_NOT_FOUND",
             f"No approval instance {instance_id}.", status=404)
    status, entity_id, object_type, object_id = row
    if object_type != REOPEN_OBJECT_TYPE or object_id != period_id:
        _err("APPROVAL_INSTANCE_MISBOUND",
             f"Approval instance {instance_id} is for "
             f"{object_type}:{object_id}, not {REOPEN_OBJECT_TYPE}:"
             f"{period_id}. An approval authorises the object it names.",
             status=409)
    return status, entity_id


def _approvers_of(session: Session, instance_id: str) -> list[str]:
    """Every identity that actually approved this instance, effective first.

    `acting_for_user_id` takes precedence where it is set: a decision made
    under delegation is the DELEGATOR's approval, and reading only
    `actor_user_id` would let the closer approve their own reopening through a
    delegate. `pg/approvals.decide` already refuses a delegation that would
    launder a self-approval; this reads the same two columns so the two agree.
    """
    rows = session.fetchall(  # scope-exempt: approval_action carries no dimension column of its own, and the instance it belongs to was scope-gated by _approval_instance_for one statement earlier
        "SELECT actor_user_id, acting_for_user_id FROM approval_action "
        "WHERE instance_id = %s AND action = %s ORDER BY seq",
        (instance_id, ACTION_APPROVE))
    return [(acting_for or actor) for actor, acting_for in rows]


def request_period_reopen(session: Session, *, period_id: str,
                          approval_instance_id: str, reason: str, actor: str,
                          idempotency_key: str) -> dict[str, Any]:
    """Record a request to reopen a CLOSED period. Reopens nothing.

    Writes the row that :func:`apply_period_reopen` later reads, freezing
    ``accounting_period.closed_by`` into ``closed_by_at_request`` -- see the
    section header above for why the freeze is the control.

    IDEMPOTENT on ``idempotency_key``: replaying the same request returns the
    row already stored rather than minting a second one.
    ``ux_period_reopen_one_open_per_period`` then stops two DIFFERENT requests
    being outstanding against one period at once.
    """
    if not str(reason or "").strip():
        _err("REOPEN_REASON_REQUIRED",
             "a reopen request must say why. ck_period_reopen_reason_not_blank "
             "refuses a blank reason at the database.")
    if not str(idempotency_key or "").strip():
        _err("IDEMPOTENCY_KEY_REQUIRED",
             "a reopen request must carry an idempotency key, or a retried "
             "call cannot be told from a second request.")

    existing = repo.query_one(
        session,
        "SELECT reopen_id, period_id, status FROM period_reopen_request "
        "WHERE idempotency_key = %(key)s AND {scope}",
        {"key": idempotency_key},
        columns={"entity": "entity_id", "plant": None,
                 "location": None, "project": None},
    )
    if existing is not None:
        return {"reopen_id": existing[0], "period_id": existing[1],
                "status": existing[2], "replayed": True}

    _pid, entity_id, state, closed_at, closed_by, _count = \
        _load_period_for_reopen(session, period_id)

    if state != "CLOSED":
        _err("PERIOD_NOT_CLOSED",
             f"Period {period_id} is {state}. Only a CLOSED period is "
             f"reopened; {state} moves through transition_period.", status=409)
    if not closed_by:
        # 001 makes closed_by NULLable, and a period whose close recorded
        # nobody cannot have a separation-of-duties rule applied to it. That is
        # a refusal, not a waiver: "we cannot tell who closed it" must never
        # become "anyone may approve reopening it".
        _err("PERIOD_CLOSER_UNKNOWN",
             f"Period {period_id} records no closed_by, so segregation of "
             f"duties on its reopening cannot be verified. A gate that cannot "
             f"be evaluated must refuse rather than permit.", status=409)

    status, instance_entity = _approval_instance_for(
        session, approval_instance_id, period_id)
    if instance_entity != entity_id:
        _err("APPROVAL_INSTANCE_WRONG_ENTITY",
             f"Approval instance {approval_instance_id} belongs to entity "
             f"{instance_entity}; period {period_id} belongs to {entity_id}.",
             status=409)

    reopen_id = _new_reopen_id()
    session.execute(
        """
        INSERT INTO period_reopen_request (
            reopen_id, period_id, entity_id, approval_instance_id,
            idempotency_key, reason, closed_by_at_request,
            closed_at_at_request, requested_by, status)
        VALUES (%(reopen_id)s, %(period_id)s, %(entity_id)s, %(instance)s,
                %(key)s, %(reason)s, %(closed_by)s, %(closed_at)s,
                %(actor)s, 'REQUESTED')
        """,
        {"reopen_id": reopen_id, "period_id": period_id,
         "entity_id": entity_id, "instance": approval_instance_id,
         "key": idempotency_key, "reason": reason.strip(),
         "closed_by": closed_by, "closed_at": closed_at, "actor": actor},
    )
    audit_mod.append(
        session, actor, "PERIOD_REOPEN_REQUESTED", "ACCOUNTING_PERIOD",
        period_id,
        f"reopen {reopen_id} requested under approval {approval_instance_id} "
        f"(currently {status}); closed by {closed_by}; reason: {reason.strip()}")

    return {"reopen_id": reopen_id, "period_id": period_id,
            "entity_id": entity_id, "status": "REQUESTED",
            "approval_instance_id": approval_instance_id,
            "approval_status": status, "replayed": False}


def apply_period_reopen(session: Session, *, reopen_id: str,
                        actor: str) -> dict[str, Any]:
    """Reopen the period, if and only if its approval is APPROVED.

    THE ONLY WRITER OF ``state = 'OPEN'`` OVER A CLOSED PERIOD.
    `transition_period` cannot do it -- `_ALLOWED_TRANSITIONS` makes CLOSED
    terminal and the mapping is deliberately not widened.

    IDEMPOTENCY. A request already APPLIED returns its recorded outcome and
    writes nothing. There is no second reopening and no second audit entry.

    CONCURRENCY. `SELECT ... FOR UPDATE` on the period row, then on the request
    row, then the state is re-read UNDER those locks -- the same shape
    `transition_period` uses and `tests/test_pg_period_concurrency.py` already
    proves for the forward transitions. Of two concurrent applies, the loser
    wakes to find the period OPEN and refuses with PERIOD_NOT_CLOSED; it does
    not reopen an already-open period, and it does not append a second
    PERIOD_REOPEN to the chain.

    THE CLOSE IS NOT ERASED. `closed_at`/`closed_by` keep recording the close
    that is being reversed; `reopened_at`/`reopened_by`/`reopen_count` record
    the reversal. Both are on the row, and the hash-chained PERIOD_REOPEN entry
    goes onto the SAME `ACCOUNTING_PERIOD:<period_id>` stream the closes are
    on, so close and reopen interleave in one verifiable sequence.
    """
    (reopen_id_, period_id, entity_id, instance_id, _key, closed_by_at_request,
     _requested_by, status, approved_by, applied_by, applied_at) = \
        _load_reopen_request(session, reopen_id)

    if status == "APPLIED":
        return {"reopen_id": reopen_id_, "period_id": period_id,
                "entity_id": entity_id, "state": "OPEN", "status": "APPLIED",
                "approved_by": approved_by, "applied_by": applied_by,
                "applied_at": _iso(applied_at), "replayed": True}
    if status == "REFUSED":
        _err("REOPEN_REQUEST_REFUSED",
             f"Reopen request {reopen_id} was refused and is closed. Raise a "
             f"new request; a refused one is kept as the record that a "
             f"reopening was asked for and did not happen.", status=409)

    instance_status, _instance_entity = _approval_instance_for(
        session, instance_id, period_id)
    if instance_status != INST_APPROVED:
        _err("REOPEN_NOT_APPROVED",
             f"Approval instance {instance_id} is {instance_status}, not "
             f"{INST_APPROVED}. A closed period is reopened on an APPROVED "
             f"approval and on nothing else -- not on a permission, not on a "
             f"flag, and not on an approval still in flight.", status=409)

    approvers = _approvers_of(session, instance_id)
    if not approvers:
        _err("REOPEN_APPROVER_UNKNOWN",
             f"Approval instance {instance_id} reports {INST_APPROVED} but "
             f"records no {ACTION_APPROVE} action, so who approved it is "
             f"UNKNOWN and separation of duties cannot be verified. A gate "
             f"that cannot be evaluated must refuse.", status=409)
    if closed_by_at_request in approvers:
        _err("SELF_APPROVAL",
             f"{closed_by_at_request} closed period {period_id} and also "
             f"approved instance {instance_id}. Segregation of duties requires "
             f"an independent approver; ck_period_reopen_separation refuses "
             f"the same pairing at the database.", status=403)

    # The APPLIER limb. Checked through `auth.require_separation` first, then
    # unconditionally -- read `_refuse_self_approval`'s docstring for why the
    # second half is not redundant today.
    _refuse_self_approval(actor, closed_by_at_request,
                          what="apply", period_id=period_id)

    # Document rows, in a declared order: the period first (the older, more
    # contended object), then the request row that points at it. Two callers
    # taking them in the same order cannot deadlock against each other.
    locked = session.execute(  # scope-exempt: locks the period already scope-gated by _load_reopen_request's entity filter, and re-read below
        "SELECT state FROM accounting_period WHERE period_id = %s FOR UPDATE",
        (period_id,)).fetchone()
    if locked is None:
        _err("PERIOD_NOT_FOUND", f"Period {period_id} does not exist.", status=404)
    if locked[0] != "CLOSED":
        _err("PERIOD_NOT_CLOSED",
             f"Period {period_id} is {locked[0]}, not CLOSED. Another reopen "
             f"reached it first, or it was never closed; either way there is "
             f"nothing here to reopen.", status=409)

    locked_request = session.execute(  # scope-exempt: re-reads under lock the request row already scope-gated at the top of this call
        "SELECT status FROM period_reopen_request WHERE reopen_id = %s "
        "FOR UPDATE", (reopen_id,)).fetchone()
    if locked_request is None or locked_request[0] != "REQUESTED":
        _err("REOPEN_ALREADY_SETTLED",
             f"Reopen request {reopen_id} is "
             f"{locked_request[0] if locked_request else 'gone'} and is no "
             f"longer outstanding.", status=409)

    now = datetime.now(timezone.utc)
    approver = approvers[-1]
    session.execute(
        "UPDATE accounting_period SET state = 'OPEN', reopened_at = %s, "
        "reopened_by = %s, reopen_count = reopen_count + 1 "
        "WHERE period_id = %s",
        (now, actor, period_id))
    session.execute(
        "UPDATE period_reopen_request SET status = 'APPLIED', "
        "approved_by = %s, applied_by = %s, applied_at = %s "
        "WHERE reopen_id = %s",
        (approver, actor, now, reopen_id))

    audit_mod.append(
        session, actor, "PERIOD_REOPEN", "ACCOUNTING_PERIOD", period_id,
        f"CLOSED -> OPEN under approval {instance_id} approved by {approver}; "
        f"closed by {closed_by_at_request}; request {reopen_id}")

    return {"reopen_id": reopen_id_, "period_id": period_id,
            "entity_id": entity_id, "state": "OPEN", "status": "APPLIED",
            "approved_by": approver, "applied_by": actor,
            "applied_at": now.isoformat(), "replayed": False}


def refuse_period_reopen(session: Session, *, reopen_id: str, actor: str,
                         refusal_code: str, detail: str = "") -> dict[str, Any]:
    """Close an outstanding request without reopening anything.

    The request is never deleted -- ``trg_period_reopen_request_no_delete``
    refuses that outright. A reopening that was asked for and did not happen is
    part of the close/reopen trail, and its absence would be indistinguishable
    from its never having been asked for.
    """
    (reopen_id_, period_id, entity_id, _instance, _key, _closed_by,
     _requested_by, status, _approved_by, _applied_by, _applied_at) = \
        _load_reopen_request(session, reopen_id)
    if status != "REQUESTED":
        _err("REOPEN_ALREADY_SETTLED",
             f"Reopen request {reopen_id} is {status}.", status=409)
    if not str(refusal_code or "").strip():
        _err("REFUSAL_CODE_REQUIRED",
             "a refusal must carry a code; ck_period_reopen_refused_says_why "
             "refuses a blank one at the database.")

    session.execute(
        "UPDATE period_reopen_request SET status = 'REFUSED', "
        "refusal_code = %s WHERE reopen_id = %s",
        (refusal_code.strip(), reopen_id))
    audit_mod.append(
        session, actor, "PERIOD_REOPEN_REFUSED", "ACCOUNTING_PERIOD",
        period_id, f"request {reopen_id} refused: {refusal_code.strip()}"
                   + (f" -- {detail}" if detail else ""))
    return {"reopen_id": reopen_id_, "period_id": period_id,
            "entity_id": entity_id, "status": "REFUSED",
            "refusal_code": refusal_code.strip()}
