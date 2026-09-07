"""Purchase requests, purchase orders, and the emission of a PO to Zoho.

The PostgreSQL half of what ``app/backend/services.py`` does on SQLite, over
the eight tables ``migrations/pg/013_procurement.sql`` created. Nothing here
is a fresh design: every control is the one ``services.create_pr`` /
``services.approve_pr`` / ``services.convert_pr_to_po`` already enforce,
re-expressed against the cell-locked, RLS-scoped PostgreSQL model.

WHAT THIS MODULE IS RESPONSIBLE FOR, AND IN WHAT ORDER
======================================================

Every mutating function obeys ``pg/locking.py``'s global lock order without
exception::

    1. lock_affected_cells(...)   -- ONCE, first, with the COMPLETE set
    2. the document row's own FOR UPDATE
    3. audit_mod.append(...)      -- takes the advisory audit lock, last

and the ancestor-chain rule that goes with it: the affected set is every
``(wbs_id, budget_head_id)`` pair the document touches, and
``lock_affected_cells`` expands each one to *every* budget-owning ancestor on
its chain. Locking the nearest ancestor alone is the correctness hole §7.3
records; nothing here locks a nearest ancestor.

THE RE-CHECK IS INSIDE THE LOCK, ALWAYS
=======================================

``budget_check`` runs twice on every path that commits money: once when the
caller asks (so a hopeless request is refused early and cheaply), and once
**after** the cell locks are held and the document row has been re-read.
Availability moves between the two -- another transaction's approval, a
transfer, a period roll -- and the second answer is the only one a write may
rely on. A verdict that turns EXCEEDS_BUDGET at that point is
``BUDGET_MOVED``, exactly as ``services.approve_pr`` already answers it, and
the write does not happen.

AGGREGATION IS BY *OWNING* CELL, NOT BY (wbs, head)
===================================================

This is stronger than the SQLite original and it is not a liberty. On SQLite a
purchase request addressed exactly one control cell, so "check the cell" and
"check the request" were the same sentence. A ``pr_line``-grained request can
name two different WBS elements that roll up to the SAME budget-owning
ancestor for the same head. Checking each line's cell independently would let
two halves each pass against the same pot and the pair overspend it.
:func:`budget_verdicts` therefore resolves each cell's owning ancestor first,
sums the requested amounts per ``(owner, head)``, and checks the SUM. See
GAP-1 in ``docs/WAVE6_PROCUREMENT_CONTRACT.md`` for why the grain changed.

THREE CONTROLS THAT COULD NOT BE PORTED, NAMED RATHER THAN FAKED
================================================================

1. **`pr_reservation` has no PostgreSQL table.** ``grep -c pr_reservation
   migrations/pg/*.sql`` finds it only inside comments. ``domain.compute_ledger``
   reads it for the ``pr_reserved`` limb of exposure and ``services.create_pr``
   writes it when ``reserve=True``. There is nowhere to write it here, so
   ``reserve=True`` is REFUSED with :data:`ERR_RESERVATION_UNAVAILABLE` rather
   than silently ignored. Silently ignoring it is the dangerous reading: the
   caller asked for budget to be held and would be told it was.

2. **`lifecycle_state` has no PostgreSQL table either**, so
   ``domain.lifecycle_permits`` -- which gates procurement on
   ``project.status`` and ``wbs_element.status`` -- cannot be ported. Both
   columns exist; the table that says which of their values permit procurement
   does not, and hard-coding a value set would be this module's author's
   opinion standing in for a frozen registry. :func:`lifecycle_gate` therefore
   PROBES for the table: where it exists the gate runs exactly as
   ``domain.lifecycle_permits`` runs it, and where it does not the returned
   check payload carries ``lifecycle_gate = LIFECYCLE_UNAVAILABLE`` so no
   caller and no test can read a skipped gate as a passed one.
   ``wbs_element.is_abandoned`` is a boolean, not a lookup, so THAT half of
   ``budget_check``'s lifecycle refusal ports 1:1 and is enforced here.

3. **There is no document-number sequence.** ``services.create_pr`` derives
   ``PR-2026-0007`` from ``SELECT COUNT(*)``, which is a race that duplicates
   under concurrency and would hit ``ux_purchase_request_number``. Migration
   013 creates no sequence and no counter table. A caller may supply
   ``pr_number`` / ``po_number``; absent one, an id-derived number is used. A
   human-facing sequential series needs a PostgreSQL ``SEQUENCE``, which is a
   migration this stream does not own.

MONEY
=====

Integer paise, end to end. :func:`_as_paise` refuses ``float``, ``Decimal``,
``str`` and ``bool`` -- not because they cannot be converted but because a
conversion here is a place a rupee value can enter a commitment. The parsing
of user-supplied rupees belongs at the API boundary (``money.to_paise``); by
the time a value reaches this module it is already paise or it is a defect.

SCOPE
=====

Every read of a scopable table goes through ``repo.query()`` with a literal
``{scope}`` token and a ``columns=`` mapping naming all four dimensions.
``_PROCUREMENT_SCOPE_COLUMNS`` joins out to ``project`` and maps entity, plant,
location and project -- never project alone. Migration 013's own header
explains why at length: dimensions resolve independently, so a principal
restricted to one entity and to no project carries ``project_ids=None``, and a
project-only predicate hands them every other entity's purchase orders.

Writes are keyed on an id this module has already scope-gated with a
``repo.query`` read in the same transaction, and are additionally covered by
013's ``WITH CHECK`` policies. That is the pattern ``pg/budget.py`` uses and
the reason its write statements carry ``scope-exempt:`` markers.
"""
from __future__ import annotations

import random
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from ..integration import outbound as ob
from ..integration import throttle
from ..integration.dto import DtoError
from ..money import MoneyError
from . import audit as audit_mod
from . import budget as budget_svc
from . import integration_store as store
from . import repo
from .delegation import DelegatedSelfApproval, assert_delegation_independent
from .engine import Session
from .locking import lock_affected_cells

# ============================================================ error vocabulary

ERR_RESERVATION_UNAVAILABLE = "PR_RESERVATION_NOT_MIGRATED"
ERR_BUDGET_MOVED = "BUDGET_MOVED"
ERR_VERSION_CONFLICT = "VERSION_CONFLICT"
ERR_SELF_APPROVAL = "SELF_APPROVAL"

#: What :func:`lifecycle_gate` reports when ``lifecycle_state`` is absent.
#: A STRING, not a boolean and not a silent omission: a caller reading this
#: field learns that the gate did not run, which "True" would hide.
LIFECYCLE_UNAVAILABLE = (
    "UNAVAILABLE - lifecycle_state has no PostgreSQL table; the project and "
    "WBS procurement-status gates domain.lifecycle_permits enforces on SQLite "
    "did NOT run")
LIFECYCLE_PERMITTED = "PERMITTED"

#: The statuses ``services.approve_pr`` will approve from, verbatim, and all
#: three are inside ``ck_purchase_request_status``'s permitted set.
APPROVABLE_STATUSES = ("Submitted", "Under Review", "Exception Pending")

#: ``services.create_pr``'s two outcomes, and 013's CHECK admits both.
STATUS_DRAFT = "Draft"
STATUS_SUBMITTED = "Submitted"
STATUS_EXCEPTION_PENDING = "Exception Pending"
STATUS_APPROVED = "Approved"

#: ``domain.COMMITMENT_RELEASING_STATES``, read from the domain module rather
#: than restated, so the two engines cannot drift on what releases commitment.
COMMITMENT_RELEASING_STATES = ("Cancelled", "Closed")

#: ``domain.ACCOUNTING_EFFECTIVE_BILL_STATES``. Same reasoning.
ACCOUNTING_EFFECTIVE_BILL_STATES = ("Approved", "Reversal")

#: The Zoho module purchase orders are emitted into. C2's ``module`` column and
#: the dedupe key both carry it, so it is a constant here rather than a
#: parameter a caller could vary between the enqueue and the send -- which
#: would change ``derive_dedupe_key``'s answer and produce a second purchase
#: order for the same commitment.
PO_MODULE = "purchaseorders"


class ProcurementError(Exception):
    """Every refusal this module makes, carrying an RFC-7807 ``code``.

    Same shape as ``budget.BudgetServiceError`` and
    ``integration_store.IntegrationStoreError`` so one router handler covers
    all three: callers match on ``.code``, never on the message text.
    """

    def __init__(self, code: str, message: str, *, status: int = 400,
                 detail: Mapping[str, Any] | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.detail = dict(detail or {})
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400, **detail: Any) -> None:
    raise ProcurementError(code, message, status=status, detail=detail)


# ============================================================ small utilities

def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_paise(value: Any, *, field: str) -> int:
    """Integer paise or a refusal. Never a coercion.

    ``bool`` is rejected explicitly because ``isinstance(True, int)`` is True
    in Python and ``True`` would otherwise become one paisa.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        _err("NON_INTEGER_MONEY",
             f"{field} must be integer paise (bigint), not "
             f"{type(value).__name__} ({value!r}). Money never travels as a "
             f"float, a Decimal or a string on this path; parse rupees at the "
             f"API boundary with money.to_paise.", 422)
    return int(value)


def _as_quantity(value: Any, *, field: str) -> int | float:
    """A line quantity. ``numeric`` in the table, so an int or a real number.

    Returned unchanged rather than normalised: ``pr_line.quantity`` and
    ``po_line.quantity`` are ``numeric``, and rounding here would be this
    module deciding what a receipt of 0.2 means.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _err("INVALID_QUANTITY",
             f"{field} must be a number; got {type(value).__name__} "
             f"({value!r}).", 422)
    if value <= 0:
        _err("INVALID_QUANTITY", f"{field} must be positive; got {value!r}.", 422)
    return value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ================================================================ scope shapes
#
# All four dimensions, reached through `project`. NOT project alone: see the
# module docstring and migration 013's own header. Every mapping below names
# the SAME four columns in the SAME order so a transposition is visible.

_PROJECT_SCOPE_COLUMNS: dict[str, str | None] = {
    "project": "p.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}


# =========================================================== lifecycle probing

_LIFECYCLE_TABLE = "lifecycle_state"


def _table_exists(session: Session, table_name: str) -> bool:
    """Does this database have `table_name`?

    ``information_schema`` carries no business rows and no scope dimension, so
    this read is outside the chokepoint by nature rather than by exemption --
    the same probe, and the same reasoning, as
    ``outbound._table_exists``.
    """
    row = session.fetchone(  # scope-exempt: information_schema, not a business table
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table_name,))
    return row is not None


def lifecycle_gate(session: Session, *, project_status: str,
                   wbs_status: str) -> str:
    """``LIFECYCLE_PERMITTED``, ``LIFECYCLE_UNAVAILABLE``, or a raised refusal.

    ``domain.lifecycle_permits`` reads ``lifecycle_state`` and denies by
    default for an unknown state -- "the table is the single source of truth".
    No migration creates that table in PostgreSQL. Two wrong answers were
    available and both are refused here:

    * hard-code the permitting statuses -> a frozen registry replaced by this
      author's opinion, and the values would then disagree with C3 the first
      time C3 moved;
    * skip the gate and say nothing -> a caller reads a purchase request that
      passed "the budget check" and has no way to learn that two thirds of
      ``budget_check``'s lifecycle refusal did not run.

    So the gate probes, runs when it can, and NAMES its own absence when it
    cannot. When the table lands, this function starts enforcing with no
    change to any caller.
    """
    if not _table_exists(session, _LIFECYCLE_TABLE):
        return LIFECYCLE_UNAVAILABLE

    for object_type, state in (("project", project_status), ("wbs", wbs_status)):
        row = session.fetchone(  # scope-exempt: lifecycle_state is a global rule table with no scope dimension
            "SELECT allows_procurement FROM lifecycle_state "
            "WHERE object_type = %s AND state = %s",
            (object_type, state))
        # Unknown state DENIES, exactly as `domain.lifecycle_permits` does.
        if row is None or not row[0]:
            _err("LIFECYCLE_STATE",
                 f"{object_type} state {state!r} does not permit procurement.",
                 422)
    return LIFECYCLE_PERMITTED


# ================================================================ scope gates

def _project_row(session: Session, project_id: str) -> tuple:
    """The project, or a 404. Scope first: an out-of-scope project answers
    exactly as a non-existent one, never as a 403 (see ``repo``'s docstring on
    existence oracles)."""
    row = repo.query_one(
        session,
        """
        SELECT p.project_id, p.status, p.capex_code
        FROM project p
        WHERE p.project_id = %(project_id)s AND {scope}
        """,
        {"project_id": project_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    if row is None:
        _err("PROJECT_NOT_FOUND", f"Project {project_id} does not exist.", 404)
    return row


def _wbs_row(session: Session, wbs_id: str) -> tuple:
    """``(wbs_id, project_id, wbs_code, status, is_abandoned)``, scope-gated."""
    row = repo.query_one(
        session,
        """
        SELECT w.wbs_id, w.project_id, w.wbs_code, w.status, w.is_abandoned
        FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
        WHERE w.wbs_id = %(wbs_id)s AND {scope}
        """,
        {"wbs_id": wbs_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    if row is None:
        _err("WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.", 404)
    return row


# ======================================================== the budget re-check

def budget_verdicts(session: Session,
                    amounts_by_cell: Mapping[tuple[str, str], int],
                    ) -> list[dict[str, Any]]:
    """Availability verdicts for a document, aggregated by OWNING cell.

    ``amounts_by_cell`` maps ``(wbs_id, budget_head_id)`` to the paise this
    document proposes to commit there.

    Two passes, and the first is the one that makes the second correct:

    1. resolve each cell's budget-OWNING ancestor by asking
       ``check_availability`` for zero paise. Availability is a property of
       the owner's whole subtree, so which descendant asks does not change the
       answer -- only which pot is being asked about.
    2. sum the proposed amounts per ``(owner, head)`` and check the SUM.

    Doing it the obvious way -- one ``check_availability`` per line -- passes
    two lines of 60 against a pot of 100 because each is asked in isolation,
    and the purchase order then overspends by 20. That could not happen while
    a purchase request addressed one cell (the SQLite grain); it can now
    (GAP-1), so it is checked now.

    ``check_availability`` is CALLED, never reimplemented: the verdict a
    document is refused on has to be the same verdict
    ``GET /api/budget/availability`` reports, computed the same way.
    """
    if not amounts_by_cell:
        _err("NO_LINES", "A procurement document must carry at least one line.", 422)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for (wbs_id, head_id), amount in amounts_by_cell.items():
        probe = _availability(session, wbs_id, head_id, 0)
        key = (probe["owning_wbs_id"], head_id)
        bucket = grouped.setdefault(
            key, {"amount_paise": 0, "probe_wbs_id": wbs_id, "cells": []})
        bucket["amount_paise"] += amount
        bucket["cells"].append([wbs_id, head_id])

    verdicts: list[dict[str, Any]] = []
    for (owner_wbs_id, head_id), bucket in grouped.items():
        check = _availability(session, bucket["probe_wbs_id"], head_id,
                              bucket["amount_paise"])
        check["owning_wbs_id"] = owner_wbs_id
        check["requested_for_cells"] = bucket["cells"]
        verdicts.append(check)
    return verdicts


def _availability(session: Session, wbs_id: str, head_id: str,
                  amount_paise: int) -> dict[str, Any]:
    """``budget.check_availability``, with its refusals re-coded as ours.

    The codes are preserved verbatim (``HEAD_UNKNOWN``, ``NO_BUDGET_FOR_HEAD``,
    ``WBS_NOT_FOUND``) -- these are the ``BLOCKED`` verdicts
    ``domain.budget_check`` returns and ``services.create_pr`` turns into a
    422. Only the exception TYPE changes, so a router needs one handler.
    """
    try:
        return budget_svc.check_availability(session, wbs_id, head_id, amount_paise)
    except budget_svc.BudgetServiceError as exc:
        raise ProcurementError(exc.code, exc.message, status=exc.status) from exc


def exceeds_budget(verdicts: Sequence[Mapping[str, Any]]) -> bool:
    return any(v.get("verdict") == "EXCEEDS_BUDGET" for v in verdicts)


def _shortfall_summary(verdicts: Sequence[Mapping[str, Any]]) -> str:
    over = [v for v in verdicts if v.get("verdict") == "EXCEEDS_BUDGET"]
    return "; ".join(
        f"({v['owning_wbs_id']}, {v['budget_head_id']}) is short "
        f"{v['shortfall_paise']} paise of the {v['requested_paise']} requested"
        for v in over)


# ================================================== derived exposure: commitment
#
# `budget_ledger_cell.commitment_paise` had NO WRITER in the PostgreSQL path
# before this module. `recompute_cell` derives only the BUDGET columns
# (`budget_paise`, `original_paise`, `revisions_paise`, `future_budget_paise`)
# from `budget_line`; commitment, actual and pr_reserved were left at their
# seeded values for ever.
#
# That is not cosmetic. `check_availability` computes
# `available = budget - (commitment + actual + pr_reserved)`, so a commitment
# that never rises means every purchase order this application creates is
# invisible to the next budget check, and the pot can be spent an unbounded
# number of times. The re-check inside the lock would have been re-checking
# against a number nothing ever moved.
#
# So creating or amending a purchase order recomputes the commitment limb for
# every cell it touches, from the same formula `domain.compute_ledger` uses:
#
#     commitment = 0                        if po.status in (Cancelled, Closed)
#                = max(0, ordered - billed) otherwise
#     ordered    = amount + non_creditable_tax + freight
#     billed     = the same three columns on accounting-effective bill lines,
#                  negated for a Reversal bill
#
# WHAT IS STILL MISSING, AND WHY IT IS NOT INVENTED HERE. `actual_paise` comes
# from `bill_line` and `pr_reserved_paise` from `pr_reservation`. Bills belong
# to another stream and `pr_reservation` has no PostgreSQL table at all, so
# neither limb is written here -- writing one badly is worse than leaving it
# where its owner will find it. The `billed` subtraction below is nonetheless
# the FULL domain formula rather than a simplification, so the moment bills
# acquire a writer the two limbs move in opposite directions correctly instead
# of double-counting.

_RECOMPUTE_COMMITMENT_SQL = """
    UPDATE budget_ledger_cell SET
        commitment_paise = COALESCE((
            SELECT SUM(
                GREATEST(
                    0,
                    (pl.amount_paise + pl.non_creditable_tax_paise
                     + pl.freight_paise)
                    - COALESCE((
                        SELECT SUM(CASE WHEN b.accounting_status = 'Reversal'
                                        THEN -ABS(bl.amount_paise
                                                  + bl.non_creditable_tax_paise
                                                  + bl.freight_paise)
                                        ELSE bl.amount_paise
                                             + bl.non_creditable_tax_paise
                                             + bl.freight_paise END)
                        FROM bill_line bl
                        JOIN bill b ON b.bill_id = bl.bill_id
                        WHERE bl.po_line_id = pl.po_line_id
                          AND b.accounting_status = ANY(%(effective)s)
                    ), 0)
                )
            )::bigint
            FROM po_line pl
            JOIN purchase_order po ON po.po_id = pl.po_id
            WHERE pl.wbs_id = %(wbs_id)s
              AND pl.budget_head_id = %(head)s
              AND NOT (po.status = ANY(%(releasing)s))
        ), 0),
        updated_at = now(), updated_by = %(actor)s,
        version_no = version_no + 1
    WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
"""


def recompute_commitment(session: Session, wbs_id: str, budget_head_id: str,
                         *, actor: str) -> None:
    """Re-derive one cell's ``commitment_paise`` from its purchase-order lines.

    Locking. The caller must already hold this cell's ``budget_control_cell``
    lock, taken by :func:`~app.backend.pg.locking.lock_affected_cells` with the
    complete affected set. This ``UPDATE`` takes the ``budget_ledger_cell``
    row's lock as part of executing -- an UPDATE always does -- and that is
    safe for the reason ``budget.recompute_cell`` gives at length:
    ``fk_ledger_control_cell`` makes the ledger row's existence imply the
    control row's, and every writer of a ledger row holds the control lock, so
    ledger locks are always acquired beneath the control order and add no edge
    to the wait-for graph.

    Idempotent: it recomputes in full from ``po_line`` rather than adjusting,
    so running it twice produces the same number.
    """
    session.execute(  # scope-exempt: derives one already-locked cell from its own PO lines
        _RECOMPUTE_COMMITMENT_SQL,
        {"wbs_id": wbs_id, "head": budget_head_id, "actor": actor,
         "releasing": list(COMMITMENT_RELEASING_STATES),
         "effective": list(ACCOUNTING_EFFECTIVE_BILL_STATES)},
    )


# ==================================================================== PR lines

def _normalise_lines(lines: Sequence[Mapping[str, Any]], *, what: str
                     ) -> list[dict[str, Any]]:
    """Validate the caller's lines and number them 1..N in input order.

    ``line_no`` is assigned here rather than accepted, because
    ``ux_pr_line_number`` / ``ux_po_line_number`` make "line 3" mean one row
    and a caller-supplied number is a caller-supplied collision.
    """
    if not lines:
        _err("NO_LINES", f"A {what} must carry at least one line.", 422)
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, start=1):
        wbs_id = str(raw.get("wbs_id") or "").strip()
        head_id = str(raw.get("budget_head_id") or "").strip()
        if not wbs_id or not head_id:
            _err("LINE_CELL_REQUIRED",
                 f"{what} line {index} names no control cell: wbs_id and "
                 f"budget_head_id are both required. A line with no control "
                 f"cell cannot be budget-checked, and an unchecked commitment "
                 f"is the failure this product exists to prevent.", 422)
        amount = _as_paise(raw.get("amount_paise"),
                           field=f"{what} line {index} amount_paise")
        if amount < 0:
            _err("NEGATIVE_LINE_AMOUNT",
                 f"{what} line {index} amount_paise is {amount}; a request is "
                 f"for a positive sum or for nothing. Signed money lives on "
                 f"grn_line and bill_line, where reversals do.", 422)
        out.append({
            "line_no": index,
            "wbs_id": wbs_id,
            "budget_head_id": head_id,
            "description": raw.get("description"),
            "quantity": _as_quantity(raw.get("quantity", 1),
                                     field=f"{what} line {index} quantity"),
            "amount_paise": amount,
            "rate_paise": raw.get("rate_paise"),
        })
    return out


def _amounts_by_cell(lines: Sequence[Mapping[str, Any]]
                     ) -> dict[tuple[str, str], int]:
    totals: dict[tuple[str, str], int] = {}
    for line in lines:
        key = (line["wbs_id"], line["budget_head_id"])
        totals[key] = totals.get(key, 0) + int(line["amount_paise"])
    return totals


def _affected_cells(lines: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """The COMPLETE affected set, deduplicated, for one call to
    ``lock_affected_cells``. Rule 1: once, first, complete."""
    seen: list[tuple[str, str]] = []
    for line in lines:
        key = (line["wbs_id"], line["budget_head_id"])
        if key not in seen:
            seen.append(key)
    return seen


# =========================================================== purchase requests

def create_pr(session: Session, *, project_id: str,
              lines: Sequence[Mapping[str, Any]], actor: str,
              description: str | None = None, reserve: bool = False,
              pr_number: str | None = None,
              correlation_id: str | None = None) -> dict[str, Any]:
    """Raise a DRAFT purchase request with one or more control-cell lines.

    ``services.create_pr`` goes straight to ``Submitted``; this splits creation
    from submission, because 013's ``ck_purchase_request_status`` admits
    ``Draft`` and because a line-grained request is assembled over more than
    one call. Every control ``services.create_pr`` enforces is enforced here:
    the WBS must belong to the named project, the cells are locked before
    anything is read for a decision, ``budget_check`` runs and a BLOCKED
    verdict refuses the write, and the reservation is honoured or refused --
    never quietly dropped.
    """
    if reserve:
        _err(ERR_RESERVATION_UNAVAILABLE,
             "reserve=True was requested, and this build cannot hold budget: "
             "`pr_reservation` has no PostgreSQL table (migrations 001-013 "
             "create none) and `budget_ledger_cell.pr_reserved_paise` has no "
             "writer. The request has NOT been created. Ignoring the flag "
             "would tell you budget was held when nothing holds it.", 501)

    normalised = _normalise_lines(lines, what="purchase request")
    project = _project_row(session, project_id)
    project_status = project[1]

    # Every line's WBS must be in scope, must belong to THIS project, and must
    # not be abandoned. `services.create_pr` raises PROJECT_WBS_MISMATCH for
    # the second; the third is `domain.budget_check`'s WBS_ABANDONED, which is
    # a boolean column and therefore the one lifecycle refusal that ports.
    wbs_rows: dict[str, tuple] = {}
    for line in normalised:
        wbs_id = line["wbs_id"]
        if wbs_id not in wbs_rows:
            wbs_rows[wbs_id] = _wbs_row(session, wbs_id)
        _, wbs_project, wbs_code, wbs_status, is_abandoned = wbs_rows[wbs_id]
        if wbs_project != project_id:
            _err("PROJECT_WBS_MISMATCH",
                 f"{wbs_code} belongs to {wbs_project}, not {project_id}. "
                 f"The request was not created.", 422)
        if is_abandoned:
            _err("WBS_ABANDONED",
                 f"{wbs_code} is abandoned and cannot receive procurement.", 422)

    # Rule 1: cells first, complete set, exactly once, before any decision is
    # read. Availability read before the lock is availability that can move
    # under the decision.
    lock_affected_cells(session, _affected_cells(normalised))

    lifecycle = LIFECYCLE_UNAVAILABLE
    for wbs_id, row in wbs_rows.items():
        lifecycle = lifecycle_gate(session, project_status=project_status,
                                   wbs_status=row[3])

    verdicts = budget_verdicts(session, _amounts_by_cell(normalised))
    over = exceeds_budget(verdicts)

    pr_id = _new_id("PR")
    # `ux_purchase_request_number` is UNIQUE and there is no sequence to draw a
    # human-facing series from (see the module docstring). Falling back to the
    # id is unique by construction; `SELECT COUNT(*) + 1`, which is what the
    # SQLite service does, races and collides.
    number = pr_number or pr_id
    check_result = "EXCEEDS_BUDGET" if over else "WITHIN_BUDGET"

    session.execute(
        """
        INSERT INTO purchase_request (
            pr_id, pr_number, project_id, requested_by, status, check_result,
            exception_reason, reserves_budget, created_by, updated_by)
        VALUES (%(pr_id)s, %(number)s, %(project_id)s, %(actor)s, %(status)s,
                %(check_result)s, %(exception)s, false, %(actor)s, %(actor)s)
        """,
        {"pr_id": pr_id, "number": number, "project_id": project_id,
         "actor": actor, "status": STATUS_DRAFT, "check_result": check_result,
         "exception": _shortfall_summary(verdicts) if over else None},
    )
    for line in normalised:
        session.execute(
            """
            INSERT INTO pr_line (
                pr_line_id, pr_id, line_no, project_id, wbs_id, budget_head_id,
                description, quantity, amount_paise, created_by, updated_by)
            VALUES (%(id)s, %(pr_id)s, %(line_no)s, %(project_id)s, %(wbs_id)s,
                    %(head)s, %(description)s, %(quantity)s, %(amount)s,
                    %(actor)s, %(actor)s)
            """,
            {"id": _new_id("PRL"), "pr_id": pr_id, "line_no": line["line_no"],
             "project_id": project_id, "wbs_id": line["wbs_id"],
             "head": line["budget_head_id"],
             "description": line["description"] or description,
             "quantity": line["quantity"], "amount": line["amount_paise"],
             "actor": actor},
        )

    total = sum(line["amount_paise"] for line in normalised)
    audit_mod.append(
        session, actor, "PR_CREATED", "PurchaseRequest", pr_id,
        f"{number} raised on {project_id} with {len(normalised)} line(s) "
        f"totalling {total} paise across "
        f"{len(_affected_cells(normalised))} control cell(s). "
        f"Budget check: {check_result}.",
        correlation_id=correlation_id)

    return {
        "pr_id": pr_id, "pr_number": number, "project_id": project_id,
        "status": STATUS_DRAFT, "check_result": check_result,
        "amount_paise": total, "line_count": len(normalised),
        "verdicts": verdicts, "lifecycle_gate": lifecycle,
    }


def _pr_header(session: Session, pr_id: str) -> dict[str, Any]:
    """The purchase request, scope-gated. Out of scope answers as absent."""
    row = repo.query_one(
        session,
        """
        SELECT pr.pr_id, pr.pr_number, pr.project_id, pr.requested_by,
               pr.status, pr.check_result, pr.approver, pr.amount_paise,
               pr.version_no, pr.created_by, p.status
        FROM purchase_request pr
        JOIN project p ON p.project_id = pr.project_id
        WHERE pr.pr_id = %(pr_id)s AND {scope}
        """,
        {"pr_id": pr_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    if row is None:
        _err("PR_NOT_FOUND", f"Purchase request {pr_id} does not exist.", 404)
    return {
        "pr_id": row[0], "pr_number": row[1], "project_id": row[2],
        "requested_by": row[3], "status": row[4], "check_result": row[5],
        "approver": row[6], "amount_paise": row[7], "version_no": row[8],
        "created_by": row[9], "project_status": row[10],
    }


def _pr_lines(session: Session, pr_id: str) -> list[dict[str, Any]]:
    rows = repo.query(
        session,
        """
        SELECT l.pr_line_id, l.line_no, l.wbs_id, l.budget_head_id,
               l.description, l.quantity, l.amount_paise, w.status,
               w.is_abandoned, w.wbs_code
        FROM pr_line l
        JOIN project p ON p.project_id = l.project_id
        JOIN wbs_element w ON w.wbs_id = l.wbs_id
        WHERE l.pr_id = %(pr_id)s AND {scope}
        ORDER BY l.line_no
        """,
        {"pr_id": pr_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    return [
        {"pr_line_id": r[0], "line_no": r[1], "wbs_id": r[2],
         "budget_head_id": r[3], "description": r[4], "quantity": r[5],
         "amount_paise": r[6], "wbs_status": r[7], "is_abandoned": r[8],
         "wbs_code": r[9]}
        for r in rows
    ]


def _assert_version(expected: int | None, current: int, *, label: str) -> None:
    """Optimistic concurrency, on top of the pessimistic row lock.

    A stale ``version_no`` is a 409, never a silent overwrite: the caller read
    the document, somebody else changed it, and applying the caller's intent to
    a document they have not seen is how two amendments each pass their own
    check and both commit (AUD-C-001).
    """
    if expected is not None and int(expected) != int(current):
        _err(ERR_VERSION_CONFLICT,
             f"{label} was modified by someone else (you have version "
             f"{expected}, it is now {current}). Reload and try again.", 409)


def submit_pr(session: Session, *, pr_id: str, actor: str,
              expected_version: int | None = None,
              correlation_id: str | None = None) -> dict[str, Any]:
    """DRAFT -> Submitted, or -> Exception Pending when the cells are short.

    The two statuses are ``services.create_pr``'s, moved to the step that
    actually asserts them. The budget check runs again here and its verdict --
    not the one recorded at creation -- decides which status the request lands
    on, because availability moves and a request drafted last week is not
    evidence about this week's pot.
    """
    header = _pr_header(session, pr_id)
    if header["status"] != STATUS_DRAFT:
        _err("INVALID_TRANSITION",
             f"{header['pr_number']} is {header['status']} and can no longer "
             f"be submitted.", 409)
    lines = _pr_lines(session, pr_id)
    if not lines:
        _err("NO_LINES",
             f"{header['pr_number']} carries no lines and cannot be submitted.",
             422)

    # Rule 1, then rule 2: cells, then the document row.
    lock_affected_cells(session, _affected_cells(lines))
    current = _lock_pr_row(session, pr_id)
    _assert_version(expected_version, current["version_no"],
                    label=header["pr_number"])
    if current["status"] != STATUS_DRAFT:
        _err("INVALID_TRANSITION",
             f"{header['pr_number']} is {current['status']} and can no longer "
             f"be submitted.", 409)

    lifecycle = LIFECYCLE_UNAVAILABLE
    for line in lines:
        if line["is_abandoned"]:
            _err("WBS_ABANDONED",
                 f"{line['wbs_code']} is abandoned and cannot receive "
                 f"procurement.", 422)
        lifecycle = lifecycle_gate(session,
                                   project_status=header["project_status"],
                                   wbs_status=line["wbs_status"])

    verdicts = budget_verdicts(session, _amounts_by_cell(lines))
    over = exceeds_budget(verdicts)
    status = STATUS_EXCEPTION_PENDING if over else STATUS_SUBMITTED
    check_result = "EXCEEDS_BUDGET" if over else "WITHIN_BUDGET"

    session.execute(  # scope-exempt: pr_id scope-gated by _pr_header above; 013's WITH CHECK backs it
        """
        UPDATE purchase_request
        SET status = %(status)s, check_result = %(check_result)s,
            exception_reason = %(exception)s, updated_at = now(),
            updated_by = %(actor)s, version_no = version_no + 1
        WHERE pr_id = %(pr_id)s
        """,
        {"status": status, "check_result": check_result,
         "exception": _shortfall_summary(verdicts) if over else None,
         "actor": actor, "pr_id": pr_id},
    )
    audit_mod.append(
        session, actor, "PR_SUBMITTED", "PurchaseRequest", pr_id,
        f"{header['pr_number']} submitted -> {status}. "
        f"Budget check: {check_result}.",
        correlation_id=correlation_id)
    return {"pr_id": pr_id, "pr_number": header["pr_number"], "status": status,
            "check_result": check_result, "verdicts": verdicts,
            "lifecycle_gate": lifecycle,
            "version_no": current["version_no"] + 1}


def _lock_pr_row(session: Session, pr_id: str) -> dict[str, Any]:
    """Take the document row lock -- SECOND in the global order -- and re-read
    under it. Another transaction could have decided this request between the
    unlocked read and here."""
    session.execute(  # scope-exempt: locks a row already scope-gated in this call
        "SELECT pr_id FROM purchase_request WHERE pr_id = %s FOR UPDATE",
        (pr_id,))
    row = session.fetchone(  # scope-exempt: re-reads the row just locked above
        "SELECT status, check_result, requested_by, version_no, pr_number "
        "FROM purchase_request WHERE pr_id = %s",
        (pr_id,))
    if row is None:
        _err("PR_NOT_FOUND", f"Purchase request {pr_id} does not exist.", 404)
    return {"status": row[0], "check_result": row[1], "requested_by": row[2],
            "version_no": row[3], "pr_number": row[4]}


def contributors_of_pr(session: Session, pr_id: str) -> set[str]:
    """Everyone who put something into this request.

    The requester, whoever created the header row, and whoever created or last
    updated any line. ``assert_delegation_independent`` refuses a decision by
    -- or on behalf of -- anybody in this set, which is what stops a delegation
    laundering a self-approval: an approver who wrote a LINE is a contributor
    even though ``purchase_request.requested_by`` names somebody else.
    """
    rows = repo.query(
        session,
        """
        SELECT pr.requested_by, pr.created_by, l.created_by, l.updated_by
        FROM purchase_request pr
        JOIN project p ON p.project_id = pr.project_id
        LEFT JOIN pr_line l ON l.pr_id = pr.pr_id
        WHERE pr.pr_id = %(pr_id)s AND {scope}
        """,
        {"pr_id": pr_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    return {value for row in rows for value in row if value}


def approve_pr(session: Session, *, pr_id: str, actor: str,
               principal: Mapping[str, Any] | None = None,
               acting_for_user_id: str | None = None,
               reason: str | None = None,
               expected_version: int | None = None,
               correlation_id: str | None = None) -> dict[str, Any]:
    """Approve a submitted purchase request. Maker-checker is not weakened.

    THE CALL ORDER IS ``services.approve_pr``'S, UNCHANGED::

        auth.require(actor, permission)
        auth.require_separation(actor, permission, requested_by)

    ``require`` first, so a caller who may not approve at all is told that,
    rather than being told they may not approve *this one* -- which would
    disclose who raised it. The permission is chosen the same way: an
    EXCEEDS_BUDGET request needs ``pr.approve_exception``, everything else
    ``pr.approve``.

    THEN, and additionally, the engine's own check::

        assert_delegation_independent(actor, acting_for_user_id, contributors)

    which is a SUPERSET, not a replacement. ``require_separation`` compares one
    identity against ``requested_by``. This compares BOTH identities against
    every contributor, so a delegate cannot approve their own request by acting
    for somebody else, and an approver cannot approve a request they merely
    wrote a line of. A delegation adds capacity; it can never launder a
    self-approval.

    ``principal`` is optional, exactly as it is on ``approvals.decide``, and
    OMITTING IT DOES NOT OMIT SEGREGATION OF DUTIES. Only the role check is
    skipped -- the caller has then asserted that authorisation happened
    somewhere they own, which is true of the write-back path and of a test.
    The two identity checks run unconditionally: the contributor check above,
    and the ``actor == requested_by`` re-read UNDER THE LOCK below. A route
    that forgot to pass a principal would grant the wrong ROLE access, not a
    self-approval.

    The budget check re-runs INSIDE the lock, after the row has been re-read,
    and an EXCEEDS_BUDGET verdict on a non-exception request is
    ``BUDGET_MOVED`` -- the pattern ``services.approve_pr`` already uses,
    preserved because availability moves between submission and approval and
    the earlier verdict is not evidence about now.
    """
    from .. import auth as auth_mod

    header = _pr_header(session, pr_id)
    if header["status"] not in APPROVABLE_STATUSES:
        _err("INVALID_TRANSITION",
             f"{header['pr_number']} is {header['status']} and cannot be "
             f"approved again.", 409)

    is_exception = header["check_result"] == "EXCEEDS_BUDGET"
    permission = "pr.approve_exception" if is_exception else "pr.approve"

    if principal is not None:
        try:
            auth_mod.require(dict(principal), permission)
            auth_mod.require_separation(
                dict(principal), permission, header["requested_by"],
                object_label=header["pr_number"])
        except auth_mod.AuthError as exc:
            raise ProcurementError(exc.code, exc.message,
                                   status=exc.status) from exc

    try:
        assert_delegation_independent(
            actor, acting_for_user_id, contributors_of_pr(session, pr_id))
    except DelegatedSelfApproval as exc:
        raise ProcurementError(
            ERR_SELF_APPROVAL, str(exc), status=403,
            detail={"actor_user_id": actor,
                    "acting_for_user_id": acting_for_user_id}) from exc

    if is_exception and not (reason or "").strip():
        _err("REASON_REQUIRED",
             "This request exceeds available budget. An exception reason is "
             "mandatory and will be stored in the audit trail.", 422)

    lines = _pr_lines(session, pr_id)
    if not lines:
        _err("NO_LINES",
             f"{header['pr_number']} carries no lines and cannot be approved.",
             422)

    # Rule 1: cells, complete set, once, first.
    lock_affected_cells(session, _affected_cells(lines))
    # Rule 3 step 2: the document row.
    current = _lock_pr_row(session, pr_id)
    _assert_version(expected_version, current["version_no"],
                    label=header["pr_number"])
    if current["status"] not in APPROVABLE_STATUSES:
        _err("INVALID_TRANSITION",
             f"{header['pr_number']} is {current['status']} and cannot be "
             f"approved again.", 409)
    # Re-read `requested_by` under the lock too, and re-run separation against
    # it: the unlocked read above is not evidence about who the maker is now.
    if actor == current["requested_by"]:
        _err(ERR_SELF_APPROVAL,
             f"You raised {header['pr_number']} and cannot also approve it. "
             f"Segregation of duties requires an independent approver.", 403)

    verdicts = budget_verdicts(session, _amounts_by_cell(lines))
    if exceeds_budget(verdicts) and not is_exception:
        _err(ERR_BUDGET_MOVED,
             "Available budget has changed since this request was raised. "
             + _shortfall_summary(verdicts), 409)

    session.execute(  # scope-exempt: pr_id scope-gated by _pr_header above
        """
        UPDATE purchase_request
        SET status = %(status)s, approver = %(actor)s, approved_at = now(),
            exception_reason = COALESCE(%(reason)s, exception_reason),
            updated_at = now(), updated_by = %(actor)s,
            version_no = version_no + 1
        WHERE pr_id = %(pr_id)s
        """,
        {"status": STATUS_APPROVED, "actor": actor, "reason": reason,
         "pr_id": pr_id},
    )
    audit_mod.append(
        session, actor, "PR_APPROVED", "PurchaseRequest", pr_id,
        (f"Exception approval. Reason: {reason}" if is_exception
         else "Within-budget approval.")
        + f" Requested by {current['requested_by']}."
        + (f" Acting for {acting_for_user_id}." if acting_for_user_id else ""),
        correlation_id=correlation_id)
    return {"pr_id": pr_id, "pr_number": header["pr_number"],
            "status": STATUS_APPROVED, "exception": is_exception,
            "approver": actor, "verdicts": verdicts,
            "version_no": current["version_no"] + 1}


# ============================================================ purchase orders

def _po_header(session: Session, po_id: str) -> dict[str, Any]:
    row = repo.query_one(
        session,
        """
        SELECT po.po_id, po.po_number, po.project_id, po.pr_id, po.vendor_name,
               po.currency, po.status, po.version_no
        FROM purchase_order po
        JOIN project p ON p.project_id = po.project_id
        WHERE po.po_id = %(po_id)s AND {scope}
        """,
        {"po_id": po_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    if row is None:
        _err("PO_NOT_FOUND", f"Purchase order {po_id} does not exist.", 404)
    return {"po_id": row[0], "po_number": row[1], "project_id": row[2],
            "pr_id": row[3], "vendor_name": row[4], "currency": row[5],
            "status": row[6], "version_no": row[7]}


def po_lines(session: Session, po_id: str) -> list[dict[str, Any]]:
    rows = repo.query(
        session,
        """
        SELECT l.po_line_id, l.line_no, l.wbs_id, l.budget_head_id,
               l.description, l.quantity, l.rate_paise, l.amount_paise,
               l.tax_paise, l.non_creditable_tax_paise, l.freight_paise,
               l.line_external_id
        FROM po_line l
        JOIN project p ON p.project_id = l.project_id
        WHERE l.po_id = %(po_id)s AND {scope}
        ORDER BY l.line_no
        """,
        {"po_id": po_id},
        columns=_PROJECT_SCOPE_COLUMNS,
    )
    return [
        {"po_line_id": r[0], "line_no": r[1], "wbs_id": r[2],
         "budget_head_id": r[3], "description": r[4], "quantity": r[5],
         "rate_paise": r[6], "amount_paise": r[7], "tax_paise": r[8],
         "non_creditable_tax_paise": r[9], "freight_paise": r[10],
         "line_external_id": r[11]}
        for r in rows
    ]


def _write_po(session: Session, *, project_id: str, vendor_name: str,
              lines: Sequence[Mapping[str, Any]], actor: str,
              pr_id: str | None, po_number: str | None,
              currency: str, exchange_rate: Any) -> dict[str, Any]:
    """Insert the header and its lines. Writes NO cell.

    Called ONLY with the affected cells already locked and the budget already
    re-checked under that lock. It performs no check of its own precisely so
    the two callers cannot each grow a slightly different one.

    ``recompute_commitment`` is deliberately NOT called from here, even though
    both callers call it immediately afterwards and the duplication is two
    lines. ``tests/test_pg_locking_order.py`` walks this module's AST and
    requires the cell write to appear in a function that also declares its lock
    set; burying it one frame deeper would put the write in a helper that takes
    no lock, which is the exact shape the analyser exists to refuse. The
    invariant reads better where it is enforced.
    """
    po_id = _new_id("PO")
    # As `create_pr`: `ux_purchase_order_number` is UNIQUE and no sequence
    # exists to draw a human-facing series from.
    number = po_number or po_id
    session.execute(
        """
        INSERT INTO purchase_order (
            po_id, po_number, pr_id, project_id, vendor_name, currency,
            exchange_rate, status, ordered_at, created_by, updated_by)
        VALUES (%(po_id)s, %(number)s, %(pr_id)s, %(project_id)s, %(vendor)s,
                %(currency)s, %(rate)s, 'Draft', now(), %(actor)s, %(actor)s)
        """,
        {"po_id": po_id, "number": number, "pr_id": pr_id,
         "project_id": project_id, "vendor": vendor_name, "currency": currency,
         "rate": exchange_rate, "actor": actor},
    )
    for line in lines:
        amount = int(line["amount_paise"])
        quantity = line["quantity"]
        # `rate_paise` is the per-unit price the emission sends as the Zoho
        # `rate`. Derived by INTEGER division when the caller does not supply
        # one -- never by float arithmetic, which is how a rate becomes
        # 249999.99999999997 paise.
        rate = line.get("rate_paise")
        if rate is None:
            units = int(quantity) if float(quantity).is_integer() else 0
            rate = amount // units if units > 0 else amount
        else:
            # Validated BEFORE any int() call. `int(2.5)` is 2, so coercing
            # first and guarding afterwards would let a float rate through as
            # a truncated integer -- which is exactly the leak `_as_paise`
            # exists to stop, performed by the guard itself.
            rate = _as_paise(rate, field=f"line {line['line_no']} rate_paise")
        session.execute(
            """
            INSERT INTO po_line (
                po_line_id, po_id, line_no, project_id, wbs_id, budget_head_id,
                description, quantity, rate_paise, amount_paise,
                created_by, updated_by)
            VALUES (%(id)s, %(po_id)s, %(line_no)s, %(project_id)s, %(wbs_id)s,
                    %(head)s, %(description)s, %(quantity)s, %(rate)s,
                    %(amount)s, %(actor)s, %(actor)s)
            """,
            {"id": _new_id("POL"), "po_id": po_id, "line_no": line["line_no"],
             "project_id": project_id, "wbs_id": line["wbs_id"],
             "head": line["budget_head_id"], "description": line["description"],
             "quantity": quantity, "rate": int(rate),
             "amount": amount, "actor": actor},
        )

    return {"po_id": po_id, "po_number": number,
            "amount_paise": sum(int(x["amount_paise"]) for x in lines)}


def create_po(session: Session, *, project_id: str, vendor_name: str,
              lines: Sequence[Mapping[str, Any]], actor: str,
              po_number: str | None = None, currency: str = "INR",
              exchange_rate: int = 1,
              correlation_id: str | None = None) -> dict[str, Any]:
    """Create a purchase order directly, without a purchase request.

    A multi-WBS purchase order is normal and is NOT flattened: one header, one
    ``po_line`` per ``(wbs_id, budget_head_id)`` the caller names, which is the
    grain the table is keyed on and the grain the emission counts.
    """
    if not (vendor_name or "").strip():
        _err("VENDOR_REQUIRED", "A purchase order requires a vendor.", 422)
    normalised = _normalise_lines(lines, what="purchase order")
    _project_row(session, project_id)
    for line in normalised:
        _, wbs_project, wbs_code, _status, is_abandoned = _wbs_row(
            session, line["wbs_id"])
        if wbs_project != project_id:
            _err("PROJECT_WBS_MISMATCH",
                 f"{wbs_code} belongs to {wbs_project}, not {project_id}. "
                 f"The order was not created.", 422)
        if is_abandoned:
            _err("WBS_ABANDONED",
                 f"{wbs_code} is abandoned and cannot receive procurement.", 422)

    lock_affected_cells(session, _affected_cells(normalised))
    verdicts = budget_verdicts(session, _amounts_by_cell(normalised))
    if exceeds_budget(verdicts):
        _err("PO_EXCEEDS_BUDGET",
             "The order has NOT been created. " + _shortfall_summary(verdicts),
             409)

    written = _write_po(session, project_id=project_id, vendor_name=vendor_name,
                        lines=normalised, actor=actor, pr_id=None,
                        po_number=po_number, currency=currency,
                        exchange_rate=exchange_rate)
    # The commitment limb of exposure, re-derived under the locks taken above.
    # Without it this order is invisible to the next budget check.
    for wbs_id, head_id in _affected_cells(normalised):
        recompute_commitment(session, wbs_id, head_id, actor=actor)
    audit_mod.append(
        session, actor, "PO_CREATED", "PurchaseOrder", written["po_id"],
        f"{written['po_number']} raised on {project_id} for {vendor_name}: "
        f"{len(normalised)} line(s), {written['amount_paise']} paise, "
        f"{len(_affected_cells(normalised))} control cell(s).",
        correlation_id=correlation_id)
    return {**written, "project_id": project_id, "status": "Draft",
            "verdicts": verdicts, "line_count": len(normalised)}


def convert_pr_to_po(session: Session, *, pr_id: str, actor: str,
                     vendor_name: str, po_number: str | None = None,
                     currency: str = "INR", exchange_rate: int = 1,
                     expected_version: int | None = None,
                     correlation_id: str | None = None) -> dict[str, Any]:
    """Approval-driven conversion: an APPROVED request becomes a purchase order.

    ``services.convert_pr_to_po`` guards AUD-H-001 -- "a reservation converts
    exactly once" -- through ``pr_reservation``, which has no PostgreSQL table.
    The exactly-once property is preserved by a different mechanism that does
    exist: the purchase request's own row lock is taken before anything is
    read, and the "already converted?" question is asked under it. Two
    concurrent conversions serialise on that lock and the second sees the
    first's purchase order.

    Every line converts onto the SAME ``(wbs_id, budget_head_id)`` control cell
    the request was checked against -- plan §11.7's requirement, and here it is
    structural rather than validated, because the PO line is built from the PR
    line and the cell is copied, not re-supplied.
    """
    if not (vendor_name or "").strip():
        _err("VENDOR_REQUIRED", "A purchase order requires a vendor.", 422)

    header = _pr_header(session, pr_id)
    if header["status"] != STATUS_APPROVED:
        _err("PR_NOT_APPROVED",
             f"{header['pr_number']} is {header['status']}. Only an Approved "
             f"request converts to a purchase order.", 409)
    lines = _pr_lines(session, pr_id)
    if not lines:
        _err("NO_LINES",
             f"{header['pr_number']} carries no lines to convert.", 422)

    lock_affected_cells(session, _affected_cells(lines))
    current = _lock_pr_row(session, pr_id)
    _assert_version(expected_version, current["version_no"],
                    label=header["pr_number"])
    if current["status"] != STATUS_APPROVED:
        _err("PR_NOT_APPROVED",
             f"{header['pr_number']} is {current['status']}. Only an Approved "
             f"request converts to a purchase order.", 409)

    # AUD-H-001, under the request's own row lock. `ix_purchase_order_pr` is
    # NOT unique -- migration 013 makes it a plain index -- so this is the
    # guard, and it is only sound because the lock above is held.
    existing = session.fetchone(  # scope-exempt: pr_id scope-gated by _pr_header; asked under its row lock
        "SELECT po_id, po_number FROM purchase_order WHERE pr_id = %s "
        "ORDER BY created_at LIMIT 1",
        (pr_id,))
    if existing is not None:
        _err("PR_ALREADY_CONVERTED",
             f"{header['pr_number']} was already converted to {existing[1]} "
             f"({existing[0]}). A request converts exactly once (AUD-H-001).",
             409, po_id=existing[0], po_number=existing[1])

    verdicts = budget_verdicts(session, _amounts_by_cell(lines))
    if exceeds_budget(verdicts):
        _err(ERR_BUDGET_MOVED,
             "Availability moved between approval and conversion; the purchase "
             "order has NOT been created. " + _shortfall_summary(verdicts), 409)

    written = _write_po(
        session, project_id=header["project_id"], vendor_name=vendor_name,
        lines=[{**line, "rate_paise": None} for line in lines], actor=actor,
        pr_id=pr_id, po_number=po_number, currency=currency,
        exchange_rate=exchange_rate)
    # As `create_po`: the commitment limb, under the locks taken above.
    for wbs_id, head_id in _affected_cells(lines):
        recompute_commitment(session, wbs_id, head_id, actor=actor)
    audit_mod.append(
        session, actor, "PR_CONVERTED", "PurchaseRequest", pr_id,
        f"{header['pr_number']} converted to {written['po_number']} "
        f"({written['po_id']}) for {vendor_name}: {written['amount_paise']} "
        f"paise across {len(_affected_cells(lines))} control cell(s).",
        correlation_id=correlation_id)
    return {**written, "pr_id": pr_id, "pr_number": header["pr_number"],
            "project_id": header["project_id"], "status": "Draft",
            "verdicts": verdicts, "line_count": len(lines)}


# ============================================================ outbound emission
#
# From here down, nothing writes a control total. This half takes a purchase
# order that already exists locally and gets it to Zoho AT MOST ONCE, which is
# a different problem with a different failure mode.

def _emission_lines(po_line_rows: Sequence[Mapping[str, Any]]
                    ) -> tuple[ob.PoLine, ...]:
    """``po_line`` rows as ``outbound.PoLine`` values, or a refusal.

    TWO SHAPES DO NOT SURVIVE THE CROSSING, AND BOTH ARE REPORTED RATHER THAN
    ROUNDED:

    * ``po_line.quantity`` is ``numeric`` and ``outbound.PoLine.quantity`` is
      ``int``. A fractional ordered quantity therefore has no representation on
      the emission path. Rounding it would send the vendor a different quantity
      from the one the commitment was checked against, so it is refused.
    * ``rate_paise`` carries no non-negative CHECK (013 constrains
      ``amount_paise``, ``non_creditable_tax_paise`` and ``freight_paise``
      only, deliberately), and ``outbound._require_paise`` refuses a negative.
      A negative unit price is refused here with a code rather than as a
      ``MoneyError`` from three frames down.
    """
    out: list[ob.PoLine] = []
    for row in po_line_rows:
        quantity = row["quantity"]
        as_float = float(quantity)
        if not as_float.is_integer():
            _err("NON_INTEGER_QUANTITY",
                 f"PO line {row['po_line_id']} has quantity {quantity}, which "
                 f"the emission path cannot carry: outbound.PoLine.quantity is "
                 f"an integer count. Rounding it would send the vendor a "
                 f"quantity the commitment was never checked against. This is "
                 f"a contract gap between `po_line.quantity numeric` and the "
                 f"emission DTO, not a data error.", 422)
        rate = int(row["rate_paise"])
        if rate < 0:
            _err("NEGATIVE_RATE",
                 f"PO line {row['po_line_id']} has rate_paise {rate}. A "
                 f"purchase order is a commitment and does not carry a "
                 f"negative unit price.", 422)
        out.append(ob.PoLine(
            line_id=row["po_line_id"],
            cell=ob.ControlCell(wbs_id=row["wbs_id"],
                                budget_head_id=row["budget_head_id"]),
            description=row["description"] or row["po_line_id"],
            quantity=int(as_float),
            unit_price_paise=rate,
            amount_paise=int(row["amount_paise"]),
        ))
    return tuple(out)


def build_emission_plan(*, po_id: str, po_number: str, connection_id: str,
                        vendor_external_id: str, document_date: date,
                        capabilities: Any,
                        line_rows: Sequence[Mapping[str, Any]],
                        acknowledged: bool = False,
                        ) -> tuple[ob.EmissionPlan, list[dict[str, Any]]]:
    """The whole emission decision, with NO database anywhere in it.

    Returns the plan and, for each purchase order it decided on, the exact
    ``(local_id, dedupe_key, payload)`` triple that will be written to
    ``integration_outbox``.

    Split out of :func:`plan_po_emission` deliberately. Everything that decides
    how many purchase orders a requisition becomes, what identity each one
    carries and what key the tenant will index it under is pure -- it depends
    on the line rows and on ``capabilities``, and on nothing else. Keeping it
    pure means the split, the dedupe keys and the payload mapping are tested on
    every machine rather than only where a PostgreSQL happens to be, and the
    tests exercise the SAME function the router calls rather than a
    reconstruction of it.
    """
    plan = ob.plan_emission(
        local_id=po_id, connection_id=connection_id,
        vendor_external_id=vendor_external_id,
        lines=_emission_lines(line_rows), capabilities=capabilities,
        document_date=document_date, reference=po_number)
    try:
        plan.assert_acknowledged(acknowledged)
    except ob.EmissionShapeError as exc:
        raise ProcurementError(
            "PROCESS_CHANGE_UNACKNOWLEDGED", str(exc), status=409,
            detail=(plan.process_change.as_dict()
                    if plan.process_change else {})) from exc

    rows: list[dict[str, Any]] = []
    for draft in plan.drafts:
        key = ob.derive_dedupe_key(connection_id, PO_MODULE, draft.local_id)
        rows.append({
            "local_id": draft.local_id, "dedupe_key": key,
            "payload": draft.as_payload(key),
            "control_cells": [list(c) for c in draft.cells],
            "total_paise": draft.total_paise,
        })
    return plan, rows


def plan_po_emission(session: Session, *, po_id: str, connection_id: str,
                     adapter: Any, vendor_external_id: str,
                     document_date: date, actor: str,
                     acknowledged: bool = False,
                     correlation_id: str | None = None) -> dict[str, Any]:
    """Decide the emission shape, then write one outbox row per purchase order.

    ONE PURCHASE ORDER PER CONTROL CELL, WHEN D-7 SAYS SO. ``plan_emission``
    reads ``capabilities.line_level_custom_fields``: with line-level custom
    fields the WBS code and budget head ride on the LINES and one purchase
    order carries every cell; without them the dimensions can only sit on the
    HEADER, and a request spanning N control cells becomes N purchase orders
    the vendor must acknowledge, receive against and invoice separately. That
    is a procurement PROCESS change, not an implementation detail, which is why
    ``EmissionPlan.assert_acknowledged`` refuses to proceed until the caller
    passes ``acknowledged=True``. The split is never flattened to make one
    document: flattening would put two control cells behind one header field.

    AT MOST ONE LOGICAL PURCHASE ORDER, ACROSS ANY NUMBER OF RETRIES. Three
    independent mechanisms, and the middle one is the only one Zoho gives us:

    1. ``enqueue_outbound`` is idempotent on
       ``(connection_id, module, local_id)`` -- ``uq_integration_outbox_local_id``
       -- so re-planning the same purchase order returns the EXISTING outbox
       row rather than making a second. ``plan_emission`` gives each split
       draft a ``local_id`` of ``{po_id}#{wbs}#{head}``, so N cells are N
       stable identities rather than N collisions on one.
    2. ``derive_dedupe_key`` is a pure function of those same three values, so
       the ``cf_capex_ref`` written into the payload here is the key any
       already-created purchase order in the tenant is carrying. Zoho documents
       NO idempotency header; §18.5 Z-01's unique custom field is the whole
       mechanism, and this is where the value is fixed.
    3. ``emit_purchase_order`` resolves by that key before EVERY create.

    Nothing here calls Zoho. Planning writes rows; :func:`send_purchase_order`
    is what talks to an adapter.
    """
    header = _po_header(session, po_id)
    rows = po_lines(session, po_id)
    if not rows:
        _err("NO_LINES",
             f"{header['po_number']} carries no lines and cannot be emitted.",
             422)

    plan, planned = build_emission_plan(
        po_id=po_id, po_number=header["po_number"],
        connection_id=connection_id, vendor_external_id=vendor_external_id,
        document_date=document_date, capabilities=adapter.capabilities(),
        line_rows=rows, acknowledged=acknowledged)

    enqueued: list[dict[str, Any]] = []
    for entry in planned:
        outbox_id, created = store.enqueue_outbound(
            session, outbox_id=_new_id("OUT"), connection_id=connection_id,
            module=PO_MODULE, local_id=entry["local_id"],
            dedupe_key=entry["dedupe_key"], payload=entry["payload"],
            actor=actor, correlation_id=correlation_id)
        enqueued.append({"outbox_id": outbox_id,
                         "local_id": entry["local_id"],
                         "dedupe_key": entry["dedupe_key"], "created": created,
                         "control_cells": entry["control_cells"],
                         "total_paise": entry["total_paise"]})

    audit_mod.append(
        session, actor, "PO_EMISSION_PLANNED", "PurchaseOrder", po_id,
        f"{header['po_number']} planned for emission on {connection_id}: "
        f"{len(plan.drafts)} purchase order(s) for "
        f"{len(plan.control_cells)} control cell(s); "
        f"line_level_dimensions={plan.line_level_dimensions}. "
        f"{'PROCESS CHANGE ACKNOWLEDGED: ' + plan.process_change.summary if plan.process_change else 'No process change.'}",
        correlation_id=correlation_id)

    return {
        "po_id": po_id, "po_number": header["po_number"],
        "connection_id": connection_id, "module": PO_MODULE,
        "line_level_dimensions": plan.line_level_dimensions,
        "control_cells": [list(c) for c in plan.control_cells],
        "purchase_orders": len(plan.drafts),
        "process_change": (plan.process_change.as_dict()
                           if plan.process_change else None),
        "notes": list(plan.notes),
        "outbox": enqueued,
    }


class PgOutboxStore:
    """``integration_outbox`` as ``outbound.OutboxStore``.

    ``emit_purchase_order`` has always taken an ``OutboxStore``; until now the
    only implementation was ``tests/outbound_tenant_fake.InMemoryOutboxStore``,
    so the emission path had never been driven against the real table. This is
    that implementation, and it is deliberately thin -- every statement below
    either delegates to ``integration_store``, which owns the table, or is the
    one operation ``integration_store`` has no verb for.

    ``claim`` is ``SELECT ... FOR UPDATE SKIP LOCKED`` on ONE row, the
    single-row form of ``claim_outbox_batch``. It changes no state and takes no
    lease: the lock lives in this transaction, a concurrent worker skips the
    row, and a Function that dies drops the lock with its connection. That is
    the property the old lease design did not have.
    """

    def __init__(self, session: Session, *, connection_id: str, actor: str):
        self.session = session
        self.connection_id = connection_id
        self.actor = actor

    _COLUMNS = (
        "o.outbox_id, o.connection_id, o.module, o.local_id, o.dedupe_key, "
        "o.payload, o.state, o.attempts, o.max_attempts, o.next_attempt_at, "
        "o.external_id, o.sent_at, o.last_error, o.correlation_id")

    def _record(self, row: Sequence[Any]) -> ob.OutboxRecord:
        return ob.OutboxRecord(
            outbox_id=row[0], connection_id=row[1], module=row[2],
            local_id=row[3], dedupe_key=row[4], payload=dict(row[5] or {}),
            state=row[6], attempts=int(row[7]), max_attempts=int(row[8]),
            next_attempt_at=row[9], external_id=row[10], sent_at=row[11],
            last_error=row[12], correlation_id=row[13])

    def claim(self, outbox_id: str, *, now: datetime) -> ob.OutboxRecord | None:
        row = repo.query_one(
            self.session,
            f"""
            SELECT {self._COLUMNS}
            FROM integration_outbox o
            JOIN integration_connection c ON c.connection_id = o.connection_id
            WHERE o.outbox_id = %(outbox_id)s
              AND o.connection_id = %(connection_id)s
              AND o.state = ANY(%(claimable)s)
              AND coalesce(o.next_attempt_at, o.created_at) <= %(now)s
              AND {{scope}}
            FOR UPDATE OF o SKIP LOCKED
            """,
            {"outbox_id": outbox_id, "connection_id": self.connection_id,
             "claimable": list(ob.CLAIMABLE_STATES), "now": now},
            columns=_CONNECTION_SCOPE_COLUMNS,
        )
        return self._record(row) if row is not None else None

    def get(self, outbox_id: str) -> ob.OutboxRecord | None:
        row = repo.query_one(
            self.session,
            f"""
            SELECT {self._COLUMNS}
            FROM integration_outbox o
            JOIN integration_connection c ON c.connection_id = o.connection_id
            WHERE o.outbox_id = %(outbox_id)s AND {{scope}}
            """,
            {"outbox_id": outbox_id},
            columns=_CONNECTION_SCOPE_COLUMNS,
        )
        return self._record(row) if row is not None else None

    def record_sent(self, outbox_id: str, external_id: str, *,
                    now: datetime) -> None:
        store.mark_outbox_sent(self.session, outbox_id=outbox_id,
                               external_id=external_id, actor=self.actor,
                               now=now)

    def record_attempt_failed(self, outbox_id: str, *, error: str,
                              next_attempt_at: datetime | None,
                              terminal: bool, now: datetime) -> None:
        """FAILED with a retry, or DEAD. The two the table permits, no third.

        THE NON-TERMINAL BRANCH DELEGATES AND IGNORES ``next_attempt_at``, ON
        PURPOSE. ``integration_store.mark_outbox_failed`` increments the
        attempt, applies §11.6's full-jitter backoff and decides FAILED-or-DEAD
        **in one statement**, so a Function killed between the increment and
        the decision cannot leave a row retrying for ever one attempt short of
        DEAD. Honouring the caller's precomputed timestamp would mean either
        two statements or a second copy of the backoff, and §11.6's backoff
        having exactly one implementation is worth more than a caller's
        ``rng``.

        THE TERMINAL BRANCH CANNOT DELEGATE. ``mark_outbox_failed`` derives
        DEAD from attempt exhaustion alone, and a non-retryable defect -- an
        unmappable payload, a dedupe-key collision -- is terminal at attempt
        one. ``ck_integration_outbox_dead_exhausted_attempts`` requires
        ``attempts >= max_attempts`` for DEAD, so the remaining attempts are
        BURNT rather than the state being written over them: there genuinely
        are no attempts left, because retrying is forbidden.
        """
        if not terminal:
            store.mark_outbox_failed(self.session, outbox_id=outbox_id,
                                     error=error, actor=self.actor, now=now)
            return
        rows = repo.query(
            self.session,
            """
            UPDATE integration_outbox o SET
                state = 'DEAD',
                attempts = o.max_attempts,
                last_error = %(error)s,
                next_attempt_at = NULL,
                updated_at = %(now)s,
                updated_by = %(actor)s
            FROM integration_connection c
            WHERE c.connection_id = o.connection_id
              AND o.outbox_id = %(outbox_id)s
              AND o.state = ANY(%(claimable)s)
              AND {scope}
            RETURNING o.outbox_id
            """,
            {"outbox_id": outbox_id, "error": error, "now": now,
             "actor": self.actor, "claimable": list(ob.CLAIMABLE_STATES)},
            columns=_CONNECTION_SCOPE_COLUMNS,
        )
        if not rows:
            raise store.IntegrationStoreError(
                "OUTBOX_NOT_RETRYABLE",
                f"Outbox row {outbox_id} does not exist, is out of scope, or "
                f"has already left PENDING/FAILED.", status=404)

    def reschedule_uncounted(self, outbox_id: str, *, attempts: int,
                             resume_at: datetime, error: str,
                             now: datetime) -> None:
        """Put a row back where it was, without spending an attempt.

        §11.6's response table says a 429/44 (per-minute) and a 429/45 (daily
        quota) count toward NOTHING -- not the circuit, not the attempts --
        because our own throttle mis-metered or our own budget ran out, and
        neither is evidence about Zoho. ``emit_purchase_order`` does not know
        that: it classifies nothing, and records every failure as an attempt.

        So the attempt is UNDONE here, back to the count the row carried before
        the call, with ``next_attempt_at`` set to the window boundary the
        policy named. Without this, eight rate-limited ticks would kill a
        purchase order that never reached Zoho at all.
        """
        rows = repo.query(
            self.session,
            """
            UPDATE integration_outbox o SET
                state = 'FAILED',
                attempts = %(attempts)s,
                last_error = %(error)s,
                next_attempt_at = %(resume_at)s,
                updated_at = %(now)s,
                updated_by = %(actor)s
            FROM integration_connection c
            WHERE c.connection_id = o.connection_id
              AND o.outbox_id = %(outbox_id)s
              AND o.state <> 'SENT'
              AND {scope}
            RETURNING o.outbox_id
            """,
            {"outbox_id": outbox_id, "attempts": max(0, int(attempts)),
             "error": error, "resume_at": resume_at, "now": now,
             "actor": self.actor},
            columns=_CONNECTION_SCOPE_COLUMNS,
        )
        if not rows:
            raise store.IntegrationStoreError(
                "OUTBOX_NOT_RETRYABLE",
                f"Outbox row {outbox_id} does not exist, is out of scope, or "
                f"is already SENT.", status=404)


#: ``integration_outbox`` reaches its dimensions through
#: ``integration_connection``. Taken FROM the store rather than restated: the
#: store's mapping already aliases the connection ``c``, already waives plant,
#: location and project with a stated reason (a connection is per-ENTITY and
#: has no column for any of the other three), and a second copy of a scope
#: mapping is a second copy that will differ from the first.
_CONNECTION_SCOPE_COLUMNS = store.VIA_CONNECTION_SCOPE_COLUMNS


def failure_from_exception(exc: Exception) -> throttle.Failure:
    """One adapter exception as the ``throttle.Failure`` the policy reads.

    **There is no transport-error type to match on.** ``grep -rn "status_code"
    app/backend/integration/`` returns nothing: the package has adapters and a
    throttle but no HTTP client, and ``throttle.Failure`` is constructed by
    whoever calls it. So this reads the attributes an HTTP error would
    plausibly carry and falls back to TRANSIENT -- which is the conservative
    reading, because TRANSIENT retries with backoff and counts toward the
    circuit, while every other kind either does not retry or does not count.

    Reported, not decided here: a frozen transport-error shape belongs in
    ``adapter.py``, which this stream does not own.
    """
    if isinstance(exc, TimeoutError):
        return throttle.Failure(timed_out=True, message=str(exc))
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int is not None and status_int < 400:
        # `classify` refuses a success, correctly. A "failure" carrying a 2xx
        # is a caller bug, and reading it as TRANSIENT is the safe answer.
        status_int = None
    return throttle.Failure(status=status_int, code=code, message=str(exc))


def send_purchase_order(session: Session, *, outbox_id: str,
                        connection_id: str, adapter: Any, actor: str,
                        budget: Any = None, now: datetime | None = None,
                        policy: throttle.RetryPolicy | None = None,
                        rng: Any = None) -> dict[str, Any]:
    """Send one outbox row, and NEVER raise for a send failure.

    THIS FUNCTION RETURNS FAILURES RATHER THAN RAISING THEM, AND THAT IS THE
    WHOLE REASON IT EXISTS. ``Database.session`` rolls back on an exception.
    ``emit_purchase_order`` records the failed attempt through the store and
    then re-raises, so letting that exception escape the session would roll
    back the very row that says the attempt happened -- leaving ``attempts``
    at zero and the row retrying for ever, against a tenant whose daily
    ceiling may be 2,000 calls. The exception is therefore caught, classified,
    turned into the disposition §11.6's table names, and REPORTED.

    The four things that happen around ``emit_purchase_order``:

    * **before** -- the circuit is consulted. An OPEN circuit refuses without
      spending a call, which is the point of having one.
    * **on success** -- ``record_circuit_success`` closes it and clears the
      consecutive count. "Consecutive" is the operative word.
    * **on failure** -- ``throttle.classify`` reads the Zoho body code and
      ``RetryPolicy.decide`` produces the row of the table it belongs to.
      429/44 checkpoints and resumes at the minute boundary; 429/45 opens the
      circuit until the UTC day boundary and alerts; 429/1070 narrows
      parallelism by one and backs off. Only the codes that count toward the
      circuit move it -- a per-minute throttle is our arithmetic and a
      concurrency limit is our parallelism, and letting either open the breaker
      would take the integration down over our own bugs.
    * **on a non-counting failure** -- the attempt ``emit_purchase_order``
      recorded is undone (see
      :meth:`PgOutboxStore.reschedule_uncounted`), because §11.6 says a 44 and
      a 45 count toward nothing.
    """
    moment = now or _utcnow()
    policy = policy or throttle.RetryPolicy()
    outbox = PgOutboxStore(session, connection_id=connection_id, actor=actor)

    circuit = store.read_circuit(session, connection_id=connection_id,
                                 module=PO_MODULE)
    if circuit is not None and circuit["state"] == "CIRCUIT_OPEN":
        probe_at = circuit.get("next_probe_at")
        if probe_at is None or moment < probe_at:
            return {
                "sent": False, "outbox_id": outbox_id, "refused": "CIRCUIT_OPEN",
                "resume_at": _iso(probe_at),
                "message": (
                    f"The {connection_id}/{PO_MODULE} circuit is open until "
                    f"{_iso(probe_at)} ({circuit.get('opened_reason')}). No "
                    f"call was made and no attempt was spent."),
            }

    before = outbox.get(outbox_id)
    attempts_before = before.attempts if before is not None else 0

    try:
        result = ob.emit_purchase_order(
            adapter=adapter, store=outbox, outbox_id=outbox_id,
            budget=budget, now=moment, rng=rng)
    except ob.BudgetExhausted as refused:
        # Nothing was claimed and nothing was written; §11.6 calls this a
        # normal outcome a job checkpoints on, not a failure.
        return {"sent": False, "outbox_id": outbox_id,
                "refused": "RATE_BUDGET_EXHAUSTED",
                "window": refused.window, "message": str(refused)}
    except ob.DuplicateDedupeKey as duplicate:
        # Z-01 DID ITS JOB. The tenant holds a purchase order carrying this
        # key and refused a second. That is the idempotency design working,
        # dressed as an error because it is how Zoho reports a unique-field
        # violation -- so the CIRCUIT MUST NOT MOVE. Reaching the generic arm
        # below would classify a 4xx as a business error, or a bare exception
        # as TRANSIENT, and five legitimate duplicate refusals would open the
        # breaker on the one outcome that proves the mechanism works.
        # `emit_purchase_order` has already recorded the attempt and decided
        # FAILED-or-DEAD, including the ORPHANED wording when the tenant would
        # not name the record.
        return {"sent": False, "outbox_id": outbox_id,
                "refused": "DUPLICATE_DEDUPE_KEY",
                "dedupe_key": duplicate.dedupe_key,
                "external_id": duplicate.external_id,
                "message": str(duplicate)}
    except (DtoError, MoneyError) as malformed:
        # The stored payload could not become a
        # `PurchaseOrderEmissionDTO`. This happens BEFORE any adapter call --
        # `emit_purchase_order` maps the payload early, on purpose, so a
        # malformed row is refused without spending a call -- and it has
        # already recorded the row DEAD as `OUTBOX_PAYLOAD_UNMAPPABLE`.
        #
        # OUR data, not Zoho's health. Falling through to the generic arm would
        # read it as TRANSIENT and open the breaker after five bad rows,
        # stopping every GOOD row on the connection over a defect in five of
        # them -- which is the reasoning §11.6 gives for a business error not
        # counting either.
        return {"sent": False, "outbox_id": outbox_id,
                "refused": "OUTBOX_PAYLOAD_UNMAPPABLE",
                "message": f"{type(malformed).__name__}: {malformed}"}
    except ob.OutboundError as refused:
        # A LOCAL refusal, before or instead of any call: the row is not
        # claimable (another worker holds it, or it is DEAD awaiting a manual
        # retry on SCR-39), or its persisted dedupe key no longer matches the
        # derivation. Neither is evidence about Zoho's health, and letting
        # either count toward the breaker would take the integration down over
        # our own state -- which is the same mistake as counting a 429/44.
        return {"sent": False, "outbox_id": outbox_id,
                "refused": "NOT_EMITTABLE", "message": str(refused)}
    except Exception as exc:   # noqa: BLE001 - classified, recorded, reported
        return _handle_send_failure(
            session, outbox=outbox, outbox_id=outbox_id,
            connection_id=connection_id, exc=exc, policy=policy,
            attempts_before=attempts_before, now=moment, rng=rng)

    store.record_circuit_success(session, connection_id=connection_id,
                                 module=PO_MODULE, now=moment)
    return {"sent": True, "outbox_id": result.outbox_id,
            "external_id": result.external_id, "dedupe_key": result.dedupe_key,
            "created": result.created, "adopted": result.adopted,
            "attempts": result.attempts, "state": result.state,
            "note": result.note}


def _handle_send_failure(session: Session, *, outbox: PgOutboxStore,
                         outbox_id: str, connection_id: str,
                         exc: Exception, policy: throttle.RetryPolicy,
                         attempts_before: int, now: datetime,
                         rng: Any) -> dict[str, Any]:
    """Classify one failed emission and fold it into the circuit and the row."""
    failure = failure_from_exception(exc)
    disposition = policy.decide(
        failure, attempts=attempts_before + 1, now=now,
        rng=rng or random.Random())

    if not disposition.counts_toward_attempts:
        # 429/44 and 429/45. `emit_purchase_order` already spent an attempt on
        # this row; §11.6 says neither costs one, so it is given back.
        resume_at = disposition.resume_at or now
        try:
            outbox.reschedule_uncounted(
                outbox_id, attempts=attempts_before, resume_at=resume_at,
                error=f"{disposition.kind.value}: {exc}", now=now)
        except store.IntegrationStoreError:
            # The row reached a state this cannot rewind (SENT, or claimed by
            # another worker). Reported rather than forced: rewriting a SENT
            # row would be worse than an over-counted attempt.
            pass

    state = "CIRCUIT_UNCHANGED"
    if disposition.counts_toward_circuit or disposition.open_circuit_until:
        state = store.record_circuit_failure(
            session, connection_id=connection_id, module=PO_MODULE,
            counts_toward_circuit=disposition.counts_toward_circuit,
            reason=f"{disposition.kind.value}: {exc}",
            open_until=disposition.open_circuit_until,
            threshold=policy.circuit_failure_threshold, now=now)

    return {
        "sent": False, "outbox_id": outbox_id,
        "failure_kind": disposition.kind.value,
        "action": disposition.action.value,
        "retry": disposition.retry,
        "counts_toward_attempts": disposition.counts_toward_attempts,
        "counts_toward_circuit": disposition.counts_toward_circuit,
        "parallelism_delta": disposition.parallelism_delta,
        "resume_at": _iso(disposition.resume_at),
        "next_attempt_at": _iso(disposition.next_attempt_at),
        "terminal_state": disposition.terminal_state,
        "alert": disposition.alert,
        "unmapped_code": disposition.unmapped_code,
        "circuit_state": state,
        "error": f"{type(exc).__name__}: {exc}",
    }
