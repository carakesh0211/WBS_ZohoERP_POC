"""Purchase requests, purchase orders, and the emission of a PO to Zoho.

THE SERVICE LAYER, NOT THE LEDGER. There are two similarly named modules in
this package and confusing them will waste an afternoon. ``pg/procurement.py``
is the LEDGER: the SQL that reads and writes procurement rows -- goods
receipts, vendor bills, the reconciliation of received against ordered. THIS
module, ``pg/procurement_services.py``, is the SERVICE layer above it: the
PR -> PO lifecycle, the budget control that gates it, and the emission plan
that carries an approved PO to Zoho. The ledger owns the rows; this owns the
decisions about them. (It was itself called ``pg/procurement.py`` until the
name collided; see ``tests/ADAPTATIONS.md``, Wave 6.)

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

THREE CONTROLS THAT COULD NOT BE PORTED -- ALL THREE NOW CAN
============================================================

Each of these was a REFUSAL rather than a silent omission while the schema
could not carry it, and ``migrations/pg/014_procurement_corrections.sql``
supplies what each was waiting for. The refusals are KEPT, because a database
behind 014 must still refuse rather than report a control it did not run.

1. **`pr_reservation`** -- ``domain.compute_ledger`` reads it for the
   ``pr_reserved`` limb of exposure and ``services.create_pr`` writes it when
   ``reserve=True``. There was nowhere to write it, so ``reserve=True`` was
   REFUSED with :data:`ERR_RESERVATION_UNAVAILABLE` rather than silently
   ignored -- silently ignoring it is the dangerous reading, because the caller
   asked for budget to be held and would have been told it was.

   014 creates the table and :func:`recompute_derived_position` writes
   ``pr_reserved_paise``, so :func:`create_reservation` now takes the hold.
   :func:`_assert_reservable` PROBES rather than assumes, so the old refusal
   still fires on a database that lacks the table.

2. **`lifecycle_state`** -- ``domain.lifecycle_permits`` gates procurement on
   ``project.status`` and ``wbs_element.status``. Both columns existed; the
   table saying which of their values permit procurement did not, and
   hard-coding a value set would have been this module's author's opinion
   standing in for a frozen registry. :func:`lifecycle_gate` PROBES: where the
   table exists the gate runs exactly as ``domain.lifecycle_permits`` runs it,
   and where it does not the check payload carries
   ``lifecycle_gate = LIFECYCLE_UNAVAILABLE`` -- a SENTENCE, so no caller and
   no test can read a skipped gate as a passed one.

   014 seeds the table from the POC's own rows, so the gate now ENFORCES with
   no change to any caller. :func:`assert_transition_permitted` is the other
   half (plan section 12): valid state changes are data, and an unlisted one is
   refused rather than assumed.

3. **There was no document-number sequence.** ``services.create_pr`` derives
   ``PR-2026-0007`` from ``SELECT COUNT(*)``, which is a race that duplicates
   under concurrency and would hit ``ux_purchase_request_number``. This module
   fell back to the surrogate id, which is unique by construction but is not a
   human-facing series.

   NOTHING NEW WAS BUILT. ``numbering_series`` / ``numbering_counter`` /
   ``numbering_issued`` already existed (005_master_data.sql) and
   ``masters.issue_number`` already advances the counter atomically, its row
   lock serialising concurrent issuers; 014 seeds the four series and
   :func:`issue_document_number` is the one line that reaches them.

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
# Decimal appears here for ONE purpose and never touches money: rounding a
# fractional ORDERED QUANTITY under the ROUND_HALF_UP policy (C5). `round()` on
# a binary float is banker's rounding applied to a value that may not be
# representable at all -- `round(2.5)` is 2 -- and a policy named HALF_UP that
# rounds 2.5 down is a policy that lies. Money stays integer paise throughout;
# `_as_paise` still refuses a Decimal amount.
from decimal import ROUND_HALF_UP, Decimal
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

#: Migration 015. A request asked for budget to be held and one of the cells it
#: resolves to cannot hold it. The WHOLE request is refused and NOTHING is
#: created -- the atomicity half of the approved grain. Reserving the cells
#: that fit and refusing the one that does not would leave a purchase request
#: holding budget for part of itself, which is a hold nobody can reconcile
#: against the document it belongs to.
ERR_RESERVATION_EXCEEDS_BUDGET = "RESERVATION_EXCEEDS_BUDGET"

#: Migration 015. A live reservation already exists for this request and this
#: resolved cell, for a DIFFERENT amount. A retry that replays the same request
#: is idempotent and reuses the hold; a retry that replays a DIFFERENT amount
#: is not a retry, and silently moving the hold to the new figure would change
#: how much budget is held without any record that it moved.
ERR_RESERVATION_AMOUNT_CONFLICT = "RESERVATION_AMOUNT_CONFLICT"

#: Migration 015. The request still carries a live 014-grain hold -- one
#: written against a line's OWN cell rather than against the budget-owning
#: ancestor. Adding a resolved-grain hold beside it would hold the same money
#: twice, at two different keys, and `ux_pr_reservation_live_cell` cannot see
#: that because it is not a duplicate key. Settle the old hold first.
ERR_RESERVATION_GRAIN_CONFLICT = "RESERVATION_GRAIN_CONFLICT"

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

# ============================== derived exposure: ALL SIX, IN ONE PASS
#
# THE DEFECT THIS CLOSES, AND WHY IT WAS LIVE RATHER THAN THEORETICAL
#
# `budget_ledger_cell` carries six derived money columns -- `ordered_paise`,
# `commitment_paise`, `actual_paise`, `received_paise`,
# `received_not_billed_paise` and `pr_reserved_paise`. Before migration 014,
# exactly ONE of the six had a writer anywhere in the PostgreSQL path
# (`commitment_paise`, added by this module in Wave 6). `budget.recompute_cell`
# derives only the BUDGET columns from `budget_line`. The other five sat at
# their `DEFAULT 0` for ever.
#
# `check_availability` computes
#
#     available = budget - (commitment + actual + pr_reserved)
#
# `commitment_paise` is `max(0, ordered - billed)` and FALLS when a bill
# arrives. `actual_paise` should RISE by the same amount, and never did. So a
# bill landing made AVAILABLE RISE BY THE BILLED AMOUNT and the same budget
# could be committed again. That is AUD-C-001 re-opened.
#
# It was dormant only because `bill_line` had no PostgreSQL writer at all:
# `billed` was always zero, so the subtraction inside `commitment` was inert
# and neither limb moved. Wave 6's inbound path (`pg/procurement.py`) made
# `billed` non-zero and ACTIVATED it. The earlier note here claimed the two
# limbs would "move in opposite directions correctly" once bills acquired a
# writer; bills acquired one, only one limb moved, and that claim was wrong.
#
# ALL SIX ARE WRITTEN HERE, IN ONE STATEMENT. Not four, not five. Leaving some
# of six derived columns unwritten is exactly how this defect arose, and a
# second function writing a seventh column later would recreate it. One
# statement also means the six can never be observed inconsistent with each
# other: `received_not_billed` is derived from the same per-line `billed` that
# `commitment` is, and computing them in two passes admits a window where a
# concurrent bill makes them disagree.
#
# EVERY FORMULA IS `domain.compute_ledger`'S, VERBATIM
#
# That function is the frozen `C5_formulas.json` registry expressed in code.
# Reimplementing from memory is how the two limbs drift apart again, so each
# line below is transcribed from it and the differences that MATTER are called
# out rather than left to be spotted:
#
#   ordered      SUM over EVERY po_line on the cell of
#                (amount + non_creditable_tax + freight).
#                NO purchase-order status filter. A cancelled order was still
#                ordered.
#
#   received     SUM over grn_line joined to grn WHERE `g.status <> 'Void'`,
#                with `is_reversal` negating BY FLAG (`-ABS(...)`), never by
#                data entry. Again NO purchase-order status filter.
#
#   billed       (per PO line, an input to two of the six, not a column)
#                SUM over bill_line joined to bill of
#                (amount + non_creditable_tax + freight), restricted to
#                `accounting_status IN ('Approved','Reversal')` -- AUD-C-004's
#                accounting-effective set -- with `Reversal` negated by flag.
#
#   commitment   0 when `po.status IN ('Cancelled','Closed')`, else
#                GREATEST(0, ordered_line - billed_line), summed.
#                THE ANTI-DOUBLE-COUNT: a line commits only its unbilled
#                balance.
#
#   received_not_billed
#                GREATEST(0, received_line - billed_line), summed. THE
#                ANTI-UNDER-COUNT: value received but not yet billed is its own
#                bucket and is not commitment.
#
#   actual       THE ONE THAT IS NOT PER-PO-LINE, and the difference is
#                load-bearing. `domain.compute_ledger` groups `bill_line` on
#                `(wbs_id, budget_head_id)` -- the line's OWN control cell --
#                with NO `po_line_id IS NOT NULL` filter, so it counts NON-PO
#                BILL LINES TOO. A non-PO bill is an ordinary document
#                (`bill.po_id` is nullable in 013 precisely for it), it moves
#                actual CWIP, and deriving `actual` from PO lines would miss
#                every one of them -- understating exposure, which is the
#                permissive direction.
#
#   pr_reserved  SUM over `pr_reservation WHERE state = 'Reserved'` on the
#                cell. AUD-H-001: a reservation is Reserved, then Converted or
#                Released or Expired, exactly once.
#
# WHAT THE DATABASE STILL REFUSES, AND WHY THAT IS RIGHT
#
# `002_budget_control.sql` puts a `>= 0` CHECK on five of the six columns;
# `actual_paise` is deliberately unconstrained because a reversal or a credit
# note legitimately drives it negative. Four of the five are non-negative by
# construction here (`ordered` from columns 013 constrains non-negative,
# `commitment` and `received_not_billed` through GREATEST, `pr_reserved` from
# `amount_paise > 0`). `received_paise` is the exception: it is a SIGNED sum,
# and a receipt reversed for MORE than was received would make it negative and
# `ck_ledger_received_nonneg` would refuse this UPDATE.
#
# That refusal is kept. Clamping with GREATEST would deviate from
# `compute_ledger` -- the one thing this function must not do -- and would hide
# an over-reversal, which is a data error somebody needs to see. The write
# fails loudly, in the transaction that caused it, naming the constraint.
#
# `pr_reservation` is referenced directly rather than probed for. Migration 014
# creates it, `assert_schema_current` refuses to serve a database behind the
# migrations, and an absent table therefore raises `UndefinedTable` -- which is
# the correct, loud outcome. A probe that answered "no table, so zero" would be
# a permissive default, and a zero `pr_reserved` OVERSTATES availability.

#: The columns this module derives. Named once so
#: `tests/test_ledger_cell_writers.py` can assert that every money column on
#: `budget_ledger_cell` has a writer somewhere, rather than trusting that
#: whoever adds the seventh remembers to.
DERIVED_LEDGER_COLUMNS: tuple[str, ...] = (
    "ordered_paise", "commitment_paise", "actual_paise", "received_paise",
    "received_not_billed_paise", "pr_reserved_paise",
)

_RECOMPUTE_DERIVED_SQL = """
    WITH po_line_position AS (
        SELECT
            pl.po_line_id,
            po.status AS po_status,
            (pl.amount_paise + pl.non_creditable_tax_paise
             + pl.freight_paise) AS ordered_paise,
            COALESCE((
                SELECT SUM(CASE WHEN g.is_reversal
                                THEN -ABS(gl.amount_paise)
                                ELSE gl.amount_paise END)
                FROM grn_line gl
                JOIN grn g ON g.grn_id = gl.grn_id
                WHERE gl.po_line_id = pl.po_line_id
                  AND g.status <> 'Void'
            ), 0)::bigint AS received_paise,
            COALESCE((
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
            ), 0)::bigint AS billed_paise
        FROM po_line pl
        JOIN purchase_order po ON po.po_id = pl.po_id
        WHERE pl.wbs_id = %(wbs_id)s
          AND pl.budget_head_id = %(head)s
    )
    UPDATE budget_ledger_cell SET
        -- Every po_line on the cell. No status filter: a cancelled order was
        -- still ordered.
        ordered_paise = COALESCE(
            (SELECT SUM(ordered_paise) FROM po_line_position), 0)::bigint,

        -- Anti-double-count: a line commits only its UNBILLED balance, and a
        -- released purchase order commits nothing.
        commitment_paise = COALESCE((
            SELECT SUM(CASE WHEN po_status = ANY(%(releasing)s) THEN 0
                            ELSE GREATEST(0, ordered_paise - billed_paise)
                       END)
            FROM po_line_position), 0)::bigint,

        -- SIGNED, and not clamped. See the header: reversal by flag.
        received_paise = COALESCE(
            (SELECT SUM(received_paise) FROM po_line_position), 0)::bigint,

        -- Anti-under-count: value received but not yet billed is its own
        -- bucket, and it is NOT commitment.
        received_not_billed_paise = COALESCE((
            SELECT SUM(GREATEST(0, received_paise - billed_paise))
            FROM po_line_position), 0)::bigint,

        -- NOT derived from po_line_position, deliberately. `compute_ledger`
        -- groups bill_line on its OWN (wbs_id, budget_head_id) with NO
        -- `po_line_id IS NOT NULL` filter, so a non-PO bill line counts. This
        -- limb is why a bill arriving no longer raises availability.
        actual_paise = COALESCE((
            SELECT SUM(CASE WHEN b.accounting_status = 'Reversal'
                            THEN -ABS(bl.amount_paise
                                      + bl.non_creditable_tax_paise
                                      + bl.freight_paise)
                            ELSE bl.amount_paise
                                 + bl.non_creditable_tax_paise
                                 + bl.freight_paise END)
            FROM bill_line bl
            JOIN bill b ON b.bill_id = bl.bill_id
            WHERE bl.wbs_id = %(wbs_id)s
              AND bl.budget_head_id = %(head)s
              AND b.accounting_status = ANY(%(effective)s)
        ), 0)::bigint,

        -- AUD-H-001. Only a LIVE reservation holds budget.
        pr_reserved_paise = COALESCE((
            SELECT SUM(r.amount_paise)
            FROM pr_reservation r
            WHERE r.wbs_id = %(wbs_id)s
              AND r.budget_head_id = %(head)s
              AND r.state = 'Reserved'
        ), 0)::bigint,

        updated_at = now(), updated_by = %(actor)s,
        version_no = version_no + 1
    WHERE wbs_id = %(wbs_id)s AND budget_head_id = %(head)s
"""


def recompute_derived_position(session: Session, wbs_id: str,
                               budget_head_id: str, *, actor: str) -> None:
    """Re-derive ALL SIX of one cell's derived money columns, in one statement.

    ``ordered_paise``, ``commitment_paise``, ``actual_paise``,
    ``received_paise``, ``received_not_billed_paise`` and
    ``pr_reserved_paise``, every one of them from
    ``app.backend.domain.compute_ledger``'s formula verbatim. See the block
    comment above this function for each formula and for why ``actual`` is the
    one that is not per-PO-line.

    Locking. The caller must already hold this cell's ``budget_control_cell``
    lock, taken by :func:`~app.backend.pg.locking.lock_affected_cells` with the
    COMPLETE affected set. This ``UPDATE`` takes the ``budget_ledger_cell``
    row's lock as part of executing -- an UPDATE always does -- and that is
    safe for the reason ``budget.recompute_cell`` gives at length:
    ``fk_ledger_control_cell`` makes the ledger row's existence imply the
    control row's, and every writer of a ledger row holds the control lock, so
    ledger locks are always acquired beneath the control order and add no edge
    to the wait-for graph.

    Idempotent: it recomputes in full from source rows rather than adjusting,
    so running it twice with no intervening change produces the same numbers.

    RLS AND THE DIRECTION OF THE ERROR, stated because it is not obvious. In
    production this runs as ``capex_app``, so each subquery sees only the rows
    the caller's scope permits. Under-counting ``billed`` OVERSTATES commitment
    and under-counting ``actual`` UNDERSTATES exposure -- the second of those
    is the permissive direction, which is why it must not arise, and it does
    not: ``wbs_id`` belongs to exactly one project, so every ``po_line``,
    ``bill_line`` and ``pr_reservation`` on a cell sits in the project the
    caller already had to reach to get here.
    """
    session.execute(  # scope-exempt: derives one already-locked cell from its own source rows
        _RECOMPUTE_DERIVED_SQL,
        {"wbs_id": wbs_id, "head": budget_head_id, "actor": actor,
         "releasing": list(COMMITMENT_RELEASING_STATES),
         "effective": list(ACCOUNTING_EFFECTIVE_BILL_STATES)},
    )


def refresh_cells_after_ingest(session: Session,
                               cells: Sequence[tuple[str, str]], *,
                               actor: str) -> list[tuple[str, str]]:
    """Lock the complete affected set, then re-derive every cell in it.

    THE INBOUND PATH'S ENTRY POINT, and the reason the over-commitment hole is
    actually closed rather than merely closeable. ``pg/procurement.py`` mirrors
    a goods receipt or a vendor bill and then calls this; without it
    ``actual_paise`` would still never move on the path that makes ``billed``
    non-zero, and the earlier note that the staleness was "the smaller, safe
    half" would only have been true while no bill existed.

    Rule 1 of ``pg/locking.py``'s global order -- ``lock_affected_cells`` ONCE,
    FIRST, with the COMPLETE set -- is obeyed here rather than by the caller,
    deliberately: the lock and the write it protects are then one function, and
    ``tests/test_pg_locking_order.py`` verifies that pairing against this
    module's own source. A caller that took the lock itself and then called
    :func:`recompute_derived_position` in a loop would be correct only as long
    as nobody edited it.

    Returns the lock set actually taken, which can be SMALLER than ``cells``:
    an affected pair with no ``budget_control_cell`` row has nothing to lock
    and nothing will write one. The caller is told rather than left to assume.
    """
    if not cells:
        return []
    unique: list[tuple[str, str]] = []
    for cell in cells:
        if cell not in unique:
            unique.append(cell)
    taken = lock_affected_cells(session, unique)
    for wbs_id, head_id in unique:
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
    return taken


def recompute_commitment(session: Session, wbs_id: str, budget_head_id: str,
                         *, actor: str) -> None:
    """Historical name for :func:`recompute_derived_position`.

    It wrote ``commitment_paise`` and only ``commitment_paise``, which is the
    defect described above this module's derived-exposure block: five of the
    six derived columns had no writer, and ``actual_paise`` not having one made
    availability RISE when a bill landed.

    Kept as a name so every existing call site and every existing test keeps
    working, and kept as a THIN DELEGATION rather than a second statement so
    the two can never derive commitment differently. There is exactly one place
    in this package that writes a derived ledger column, and it is
    :data:`_RECOMPUTE_DERIVED_SQL`.
    """
    recompute_derived_position(session, wbs_id, budget_head_id, actor=actor)

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


# ======================================== C4: concurrency-safe document numbers
#
# `services.create_pr` derives `PR-2026-0007` from `SELECT COUNT(*)`, which
# races `ux_purchase_request_number`: two concurrent counts read the same value
# and both proceed. This module used to fall back to the surrogate id, which is
# unique by construction but is not a human-facing series.
#
# NO NEW MECHANISM IS NEEDED AND NONE IS INVENTED. `numbering_series` /
# `numbering_counter` / `numbering_issued` already exist (005_master_data.sql)
# and `masters.issue_number` already advances the counter with a single atomic
# `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING`, whose row lock
# serialises concurrent issuers. Migration 014 seeds the four series.

#: `numbering_series.code` for each procurement document. The series rows are
#: created by 014; these are the codes it seeds, named here so a caller cannot
#: mistype one into a 404 at 3am.
PR_SERIES = "PURCHASE_REQUEST"
PO_SERIES = "PURCHASE_ORDER"
GRN_SERIES = "GOODS_RECEIPT"
BILL_SERIES = "VENDOR_BILL"


def issue_document_number(session: Session, series_code: str, *, actor: str,
                          object_type: str, object_id: str,
                          period_key: str | None = None) -> str:
    """One atomically minted, human-facing document number.

    `period_key` defaults to the calendar year of ``now()``. The series are
    seeded ``YEARLY``, and 005 is explicit that deriving the period key from
    the clock is APPLICATION logic rather than a database trigger -- so it is
    derived here, in one place, and injectable for a test.

    Refuses rather than falling back. A missing series is a schema that has not
    had 014 applied, and answering with the surrogate id would quietly reissue
    the un-numbered documents this closes.
    """
    from . import masters as masters_mod

    key = period_key if period_key is not None else str(datetime.now(
        timezone.utc).year)
    try:
        issued = masters_mod.issue_number(
            session, series_code, actor=actor, period_key=key,
            object_type=object_type, object_id=object_id)
    except masters_mod.MasterDataError as exc:
        raise ProcurementError(
            "NUMBERING_SERIES_UNAVAILABLE",
            f"No active numbering series {series_code!r}: {exc}. Migration "
            f"014 seeds it. Nothing was created -- falling back to the "
            f"surrogate id would mint a document number outside the "
            f"append-only `numbering_issued` log, which is the record that "
            f"makes a number un-reusable.", status=500) from exc
    return issued["formatted_number"]


# ============================================== C1: purchase-request reservations
#
# AUD-H-001, and the reason it matters in one sentence: without reservations
# two requestors can each pass `budget_check` against the same rupees, because
# neither request has taken anything out of availability.
#
# THE GRAIN, AND WHY IT MOVED (migration 015, approved 2026-09-08)
# ================================================================
#
# 014 built `ux_pr_reservation_live UNIQUE (pr_id) WHERE state = 'Reserved'`:
# exactly one live hold per purchase request. That was right while a request
# addressed exactly ONE control cell -- the SQLite POC's header-only
# `purchase_request` -- and wrong the moment `pr_line` arrived, because a
# request whose lines span two pots needs one hold per pot. 014 reported the
# contradiction rather than resolving it and this module REFUSED a multi-cell
# `reserve=True` with MULTI_CELL_RESERVATION_UNSUPPORTED. The product owner
# resolved it on 2026-09-08.
#
# THE APPROVED GRAIN IS ONE LIVE RESERVATION PER (PURCHASE REQUEST x RESOLVED
# BUDGET CONTROL CELL), enforced by `ux_pr_reservation_live_cell UNIQUE
# (pr_id, wbs_id, budget_head_id) WHERE state = 'Reserved'` (migration 015).
#
# A RESOLVED CONTROL CELL IS THE BUDGET-OWNING ANCESTOR, NOT THE LINE'S WBS
# ==========================================================================
#
# This is the part that is easy to get wrong and expensive to get wrong.
# Budget is owned by an ANCESTOR: `budget._owning_ancestor` walks the chain and
# finds the nearest ancestor-or-self whose own `budget_paise` is non-zero for
# the head, and availability is a property of that owner's whole subtree. So
#
#   * two lines on DIFFERENT WBS elements that resolve to the SAME owner for
#     the same head are competing for ONE pot and AGGREGATE INTO ONE
#     reservation. Holding them separately would put two rows against one pot
#     and let each half be checked in isolation -- which is precisely the
#     defect `budget_verdicts` aggregates by owning cell to prevent, re-opened
#     one layer down;
#   * two lines resolving to DIFFERENT owners are competing for DIFFERENT pots
#     and get ONE HOLD EACH. That is the case 014's index forbade.
#
# NOTHING HERE RE-DERIVES THE OWNER. `budget_verdicts` already resolves it --
# it must, to check the SUM against the right pot -- and returns it as
# `owning_wbs_id` alongside the aggregated `requested_paise`.
# :func:`resolved_cell_amounts` reads those verdicts and nothing else. A second
# resolution pass could disagree with the one the refusal was computed from,
# and a hold placed on a different cell than the check was run against is a
# hold against a budget nobody checked.
#
# ATOMICITY, IDEMPOTENCY, EXACTLY-ONCE
# ====================================
#
# * ATOMIC. Every cell reserves or none does. There is no partial commit and no
#   compensating write: the whole request runs in ONE transaction
#   (`engine.Database.session` commits on success and rolls back on ANY
#   exception), so a refusal on the third cell un-writes the first two by
#   rolling back. That is why every refusal below is a raise and never a
#   returned status.
# * IDEMPOTENT. :func:`reserve_pr_cells` inserts through `ON CONFLICT ... DO
#   NOTHING` against the partial unique index, then reads back what is actually
#   live. A replayed request finds its own holds and reuses them; a replay
#   carrying a DIFFERENT amount is not a replay and is refused rather than
#   silently moving the hold.
# * EXACTLY ONCE. Settlement is `UPDATE ... WHERE state = 'Reserved'`, so a
#   second conversion, release or expiry of the same reservation matches zero
#   rows and moves nothing. The row is never DELETEd -- `capex_app` has no
#   DELETE on this table -- because AUD-H-001 is a claim about a row's history
#   and a deleted row has none.

#: `ck_pr_reservation_state`'s four values. Transcribed from migration 014,
#: which transcribed them from `app/backend/migrations/002_financial_controls.sql`.
RESERVATION_STATES: tuple[str, ...] = (
    "Reserved", "Converted", "Released", "Expired")

#: The one state that HOLDS budget. `domain.compute_ledger` and
#: `_RECOMPUTE_DERIVED_SQL` both read exactly this.
RESERVATION_LIVE_STATE = "Reserved"

#: The three states a live reservation may be settled INTO, and the three
#: business events that settle it. Named so a caller cannot invent a fourth by
#: typo: `ck_pr_reservation_state` would refuse it, but at 3am with a stack
#: trace rather than with a sentence.
RESERVATION_SETTLED_STATES: tuple[str, ...] = (
    "Converted", "Released", "Expired")

#: `ck_pr_reservation_cell_grain`'s two values (migration 015).
#:
#: LINE     -- `wbs_id` is the cell a PR LINE named. Every row written before
#:             015 is this, by construction, and every one of them is preserved.
#: RESOLVED -- `wbs_id` is the BUDGET-OWNING ANCESTOR the line resolves to, and
#:             the row may aggregate several lines. Everything this module
#:             writes from 015 onward is this.
#:
#: The distinction is load-bearing rather than documentary: a live LINE hold on
#: a descendant and a live RESOLVED hold on its owner are two rows at two
#: different keys holding the SAME money twice, and no unique index can see
#: that. :func:`reserve_pr_cells` refuses the overlap by reading this column.
RESERVATION_GRAIN_LINE = "LINE"
RESERVATION_GRAIN_RESOLVED = "RESOLVED"


def _assert_reservable(session: Session) -> None:
    """Refuse if `pr_reservation` is absent, rather than reporting a hold.

    Probed, not assumed. `assert_schema_current` should already have refused to
    serve a database behind migration 014, so this cannot normally fire -- but
    "cannot normally" is not a control, and the failure it guards against is a
    caller being told budget was held when nothing holds it, which is the
    single most dangerous shape of answer this module can give.
    """
    if not _table_exists(session, "pr_reservation"):
        _err(ERR_RESERVATION_UNAVAILABLE,
             "reserve=True was requested and this database cannot hold "
             "budget: `pr_reservation` does not exist, so migration 014 has "
             "not been applied. NOTHING was created. Ignoring the flag would "
             "tell you budget was held when nothing holds it.", 501)


def _assert_resolved_grain_available(session: Session) -> None:
    """Refuse if `pr_reservation.cell_grain` is absent -- 015 has not been run.

    The SAME refusal as :func:`_assert_reservable`, one migration later, and
    for the same reason rather than for tidiness. On a database at 014 the
    resolved grain does not exist: `ux_pr_reservation_live` still permits ONE
    live hold per request, so a two-cell reservation would take the first hold,
    then fail on the second with a `UniqueViolation` -- loud and safe, but a
    stack trace rather than a sentence, and the caller would be told nothing
    about WHY. This says why, before anything is written.

    Probed rather than assumed. `assert_schema_current` should already have
    refused to serve a database behind 015, but "cannot normally happen" is not
    a control, and the outcome it guards against -- a caller told budget was
    held across two cells when the schema can only hold one -- is the single
    most dangerous shape of answer this module can give.
    """
    row = session.fetchone(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "  AND table_name = 'pr_reservation' AND column_name = 'cell_grain'")
    if row is None:
        _err(ERR_RESERVATION_UNAVAILABLE,
             "budget was to be held at the RESOLVED control-cell grain and "
             "this database cannot express it: `pr_reservation.cell_grain` "
             "does not exist, so migration 015 has not been applied. NOTHING "
             "was created. At 014 the schema permits exactly one live hold per "
             "purchase request, so a request spanning two budget-owning cells "
             "cannot be held at all -- and taking the first hold and failing "
             "on the second would leave a request holding budget for part of "
             "itself.", 501)


def create_reservation(session: Session, *, pr_id: str, project_id: str,
                       wbs_id: str, budget_head_id: str, amount_paise: int,
                       actor: str) -> str:
    """Hold ``amount_paise`` on one control cell for one purchase request.

    THE 014-GRAIN PRIMITIVE, KEPT EXACTLY AS IT WAS. It writes the cell it is
    GIVEN, with no ancestor resolution and no aggregation, and the row it
    writes therefore carries ``cell_grain = 'LINE'`` -- which is what every row
    written before migration 015 carries, and is the truth about this function.
    :func:`reserve_pr_cells` is the resolved-grain entry point and the one
    :func:`create_pr` uses.

    A duplicate is a `UniqueViolation` from ``ux_pr_reservation_live_cell``,
    raised rather than swallowed. That is deliberate and is not the idempotency
    path: a caller that hands this function a cell twice has made a mistake
    about which cell, and answering it with a silent reuse would hide the
    mistake. Retry-safety lives in :func:`reserve_pr_cells`, which knows what a
    replay of a whole request looks like.

    The caller must already hold the cell's lock (``create_pr`` does, taken
    once with the complete affected set before any decision was read) and must
    call :func:`recompute_derived_position` afterwards -- writing the row does
    not move ``pr_reserved_paise`` on its own, because that column is DERIVED
    and this module has exactly one place that derives it.

    ``amount_paise > 0`` is enforced by ``ck_pr_reservation_amount_positive``
    and re-stated here so the refusal names the value: a reservation of zero is
    not a reservation, and a negative one would INCREASE availability.
    """
    amount = _as_paise(amount_paise, field="reservation amount_paise")
    if amount <= 0:
        _err("NON_POSITIVE_RESERVATION",
             f"A reservation of {amount} paise holds nothing. A reservation of "
             f"zero is not a reservation and a negative one would INCREASE "
             f"availability, which is the direction this control exists to "
             f"prevent.", 422)
    reservation_id = _new_id("PRRES")
    session.execute(  # scope-exempt: pr_id was scope-gated by the caller in this transaction
        """
        INSERT INTO pr_reservation (
            reservation_id, pr_id, wbs_id, budget_head_id, project_id,
            amount_paise, state, created_by, updated_by)
        VALUES (%(id)s, %(pr_id)s, %(wbs_id)s, %(head)s, %(project_id)s,
                %(amount)s, %(state)s, %(actor)s, %(actor)s)
        """,
        {"id": reservation_id, "pr_id": pr_id, "wbs_id": wbs_id,
         "head": budget_head_id, "project_id": project_id, "amount": amount,
         "state": RESERVATION_LIVE_STATE, "actor": actor},
    )
    return reservation_id


def resolved_cell_amounts(verdicts: Sequence[Mapping[str, Any]]
                          ) -> dict[tuple[str, str], int]:
    """``{(owning_wbs_id, budget_head_id): paise}`` from budget verdicts.

    THE RESOLVED CONTROL CELLS OF A DOCUMENT, and the whole of how they are
    computed. :func:`budget_verdicts` has already resolved each line cell's
    budget-OWNING ancestor (``budget._owning_ancestor``, reached through
    ``check_availability``) and already summed the requested paise per
    ``(owner, head)`` -- it has to, or it would check each half of a
    two-line request against the same pot in isolation. This reads that answer
    and derives nothing.

    A SECOND RESOLUTION PASS IS THE DEFECT THIS AVOIDS. Availability moves;
    ``budget_paise`` moves; a re-resolution a few statements later can name a
    DIFFERENT owner than the verdict the request was accepted on, and a hold
    placed on a cell other than the one the check ran against is a hold against
    a budget nobody checked. One resolution, one truth, one pot.

    Money is never rounded here and cannot be: ``requested_paise`` is the
    integer sum :func:`budget_verdicts` accumulated from ``_as_paise``-checked
    line amounts. The ``int`` guard below is not decoration -- a ``Decimal``
    arriving from a ``SUM()`` that forgot its ``::bigint`` cast would otherwise
    ride into an ``amount_paise`` column silently.
    """
    out: dict[tuple[str, str], int] = {}
    for verdict in verdicts:
        owner = verdict.get("owning_wbs_id")
        head = verdict.get("budget_head_id")
        amount = verdict.get("requested_paise")
        if not owner or not head:
            _err("UNRESOLVED_CONTROL_CELL",
                 f"A budget verdict carries no owning control cell "
                 f"(owning_wbs_id={owner!r}, budget_head_id={head!r}), so "
                 f"there is no cell to hold budget on. NOTHING was created. "
                 f"Holding it against the line's own cell instead would "
                 f"reserve money on a budget that does not own it.", 500)
        if not isinstance(amount, int) or isinstance(amount, bool):
            _err("NON_INTEGER_RESERVATION_AMOUNT",
                 f"The verdict for ({owner}, {head}) carries "
                 f"{amount!r} ({type(amount).__name__}) as its requested "
                 f"amount. Money is an integer number of paise end to end; "
                 f"rounding or coercing it here is how a rupee goes missing "
                 f"from a commitment. NOTHING was created.", 500)
        out[(owner, head)] = out.get((owner, head), 0) + amount
    return out


def live_reservations(session: Session, pr_id: str) -> list[dict[str, Any]]:
    """Every LIVE reservation on ``pr_id``, with its cell, amount and grain.

    Read under the caller's existing locks and used for three things: the
    idempotency read-back in :func:`reserve_pr_cells`, the grain-overlap
    refusal beside it, and the cells :func:`release_reservations_for_pr` must
    re-derive. One statement rather than three subtly different ones.
    """
    rows = session.fetchall(  # scope-exempt: pr_id was scope-gated by the caller in this transaction
        """
        SELECT reservation_id, wbs_id, budget_head_id, amount_paise, cell_grain
        FROM pr_reservation
        WHERE pr_id = %(pr_id)s AND state = %(live)s
        ORDER BY wbs_id, budget_head_id, reservation_id
        """,
        {"pr_id": pr_id, "live": RESERVATION_LIVE_STATE},
    )
    return [{"reservation_id": row[0], "wbs_id": row[1],
             "budget_head_id": row[2], "amount_paise": int(row[3]),
             "cell_grain": row[4]} for row in rows]


def reserve_pr_cells(session: Session, *, pr_id: str, project_id: str,
                     resolved_amounts: Mapping[tuple[str, str], int],
                     actor: str, correlation_id: str | None = None
                     ) -> list[str]:
    """Hold budget on EVERY resolved control cell of one request, or on none.

    ``resolved_amounts`` maps ``(owning_wbs_id, budget_head_id)`` to the paise
    to hold there -- :func:`resolved_cell_amounts` computes it from the
    verdicts, and this function does not re-resolve anything.

    ATOMIC. Every cell or no cell. Not by a compensating write and not by a
    savepoint: the caller's whole request is ONE transaction, so any refusal
    below rolls the earlier inserts back with it. That is why an insufficient
    cell is a raise and never a partial success -- see
    :func:`create_pr`, which refuses the request outright before reaching here
    if any verdict exceeds budget.

    IDEMPOTENT. ``ON CONFLICT ... DO NOTHING`` against
    ``ux_pr_reservation_live_cell``, then a read-back of what is actually live.
    A replay of the same request finds its own holds and returns their ids
    unchanged, having written nothing the second time. A replay carrying a
    DIFFERENT amount for a cell that is already held is refused with
    :data:`ERR_RESERVATION_AMOUNT_CONFLICT`: that is not a retry, and quietly
    updating the row would move how much budget is held with no record that it
    moved.

    THE GRAIN OVERLAP, REFUSED. A live 014-grain hold sits on a LINE's cell,
    which may be a DESCENDANT of the owner this function is about to hold. Two
    such rows are not a duplicate key -- the index cannot see them -- and
    together they hold the same money twice. So a request still carrying a live
    ``cell_grain = 'LINE'`` reservation is refused with
    :data:`ERR_RESERVATION_GRAIN_CONFLICT` rather than double-held.

    LOCKING. The caller must already hold every affected cell's lock, taken
    ONCE, FIRST, with the complete set (``create_pr`` does). Nothing here takes
    a cell lock: `pr_reservation` is not a control cell, and the derived
    ``pr_reserved_paise`` is moved by the caller through
    :func:`recompute_derived_position`, which is the module's only deriver.

    AUDIT. One append-only entry per hold taken, INSIDE this transaction, with
    the actor the caller was authenticated as. A hold that rolls back takes its
    audit line with it, which is correct: the audit trail records what
    happened, and nothing happened.
    """
    if not resolved_amounts:
        _err("NO_RESERVABLE_CELLS",
             "reserve=True was requested and the request resolves to no "
             "budget control cell at all. NOTHING was created; a hold on no "
             "cell is not a hold.", 422)

    _assert_reservable(session)
    _assert_resolved_grain_available(session)

    existing = live_reservations(session, pr_id)
    line_grain = [r for r in existing
                  if r["cell_grain"] == RESERVATION_GRAIN_LINE]
    if line_grain:
        named = ", ".join(
            "{0} on ({1}, {2})".format(
                row["reservation_id"], row["wbs_id"], row["budget_head_id"])
            for row in line_grain)
        _err(ERR_RESERVATION_GRAIN_CONFLICT,
             f"{pr_id} still carries {len(line_grain)} live reservation(s) "
             f"written at the LINE grain: {named}. "
             f"A line-grain hold sits on the line's own cell and a resolved "
             f"hold sits on the budget-owning ancestor above it, so the two "
             f"would hold the SAME money twice at two different keys -- which "
             f"`ux_pr_reservation_live_cell` cannot refuse, because it is not "
             f"a duplicate key. NOTHING was created. Settle the existing "
             f"hold(s) first.", 409)

    by_cell = {(r["wbs_id"], r["budget_head_id"]): r for r in existing}
    reservation_ids: list[str] = []

    for (wbs_id, head_id), amount_paise in sorted(resolved_amounts.items()):
        amount = _as_paise(amount_paise,
                           field=f"reservation amount_paise for "
                                 f"({wbs_id}, {head_id})")
        if amount <= 0:
            _err("NON_POSITIVE_RESERVATION",
                 f"A reservation of {amount} paise on ({wbs_id}, {head_id}) "
                 f"holds nothing. A reservation of zero is not a reservation "
                 f"and a negative one would INCREASE availability, which is "
                 f"the direction this control exists to prevent. NOTHING was "
                 f"created.", 422)

        held = by_cell.get((wbs_id, head_id))
        if held is not None:
            if held["amount_paise"] != amount:
                _err(ERR_RESERVATION_AMOUNT_CONFLICT,
                     f"{pr_id} already holds {held['amount_paise']} paise on "
                     f"({wbs_id}, {head_id}) under reservation "
                     f"{held['reservation_id']}, and this request asks to hold "
                     f"{amount}. A retry replays the same request and is a "
                     f"no-op; this is a DIFFERENT request. NOTHING was "
                     f"changed -- moving the hold silently would change how "
                     f"much budget is held with no record that it moved. "
                     f"Release the existing hold and raise the new request.",
                     409)
            # The idempotent path: same request, same cell, same money. The
            # hold that already exists IS the answer.
            reservation_ids.append(held["reservation_id"])
            continue

        candidate = _new_id("PRRES")
        row = session.fetchone(  # scope-exempt: pr_id was scope-gated by the caller in this transaction
            """
            INSERT INTO pr_reservation (
                reservation_id, pr_id, wbs_id, budget_head_id, project_id,
                amount_paise, state, cell_grain, created_by, updated_by)
            VALUES (%(id)s, %(pr_id)s, %(wbs_id)s, %(head)s, %(project_id)s,
                    %(amount)s, %(state)s, %(grain)s, %(actor)s, %(actor)s)
            ON CONFLICT (pr_id, wbs_id, budget_head_id)
                WHERE state = 'Reserved'
            DO NOTHING
            RETURNING reservation_id
            """,
            {"id": candidate, "pr_id": pr_id, "wbs_id": wbs_id,
             "head": head_id, "project_id": project_id, "amount": amount,
             "state": RESERVATION_LIVE_STATE,
             "grain": RESERVATION_GRAIN_RESOLVED, "actor": actor},
        )
        if row is None:
            # A concurrent transaction took this hold between the read-back
            # above and this INSERT. It is not an error and it is not ours to
            # resolve by writing a second row: re-read, and hold the two to the
            # same amount rule as an ordinary replay.
            concurrent = {(r["wbs_id"], r["budget_head_id"]): r
                          for r in live_reservations(session, pr_id)}
            other = concurrent.get((wbs_id, head_id))
            if other is None:
                _err("RESERVATION_LOST",
                     f"The hold on ({wbs_id}, {head_id}) for {pr_id} was "
                     f"refused as a duplicate and then could not be read back. "
                     f"NOTHING is assumed about it: answering with a "
                     f"reservation id this transaction cannot see would report "
                     f"a hold nothing holds.", 409)
            if other["amount_paise"] != amount:
                _err(ERR_RESERVATION_AMOUNT_CONFLICT,
                     f"A concurrent transaction holds {other['amount_paise']} "
                     f"paise on ({wbs_id}, {head_id}) for {pr_id} under "
                     f"reservation {other['reservation_id']}, and this request "
                     f"asks for {amount}. NOTHING was changed.", 409)
            reservation_ids.append(other["reservation_id"])
            continue

        reservation_ids.append(row[0])
        audit_mod.append(
            session, actor, "PR_RESERVATION_HELD", "PurchaseRequest", pr_id,
            f"{amount} paise held on resolved control cell "
            f"({wbs_id}, {head_id}) as reservation {row[0]}.",
            correlation_id=correlation_id)

    return reservation_ids


def release_reservations_for_pr(session: Session, *, pr_id: str, actor: str,
                                state: str = "Released",
                                reason: str | None = None,
                                correlation_id: str | None = None
                                ) -> list[tuple[str, str]]:
    """Release every live hold on ``pr_id`` and re-derive every cell it freed.

    THE REJECTION / CANCELLATION / EXPIRY PATH, and the reason it is one
    function rather than three. All three events do the same three things --
    settle every live reservation exactly once, re-derive
    ``pr_reserved_paise`` on every cell that stopped being held, and say so in
    the audit trail -- and three copies of that would be three chances for one
    of them to forget the second step. ``state`` names which of the three
    happened (``Released`` for a rejection or a cancellation, ``Expired`` for
    an expiry sweep); ``reason`` is carried into the audit entry.

    EXACTLY ONCE PER CELL. :func:`settle_reservations` is
    ``UPDATE ... WHERE state = 'Reserved'``, so a second release of the same
    request matches zero rows, returns no cells, re-derives nothing and appends
    no audit line. Calling it twice is a no-op, not a double release.

    LOCKING, in the order ``pg/locking.py`` fixes. The cells are read first --
    a plain SELECT of the reservations' own rows, which takes no cell lock --
    then ``lock_affected_cells`` is called ONCE with that COMPLETE set, and
    only then is anything written. ``lock_affected_cells`` expands each cell to
    every budget-owning ancestor on its chain, which is the rule §7.3 records
    and the hole that locking only the nearest ancestor leaves open.

    Re-deriving is not optional. A hold that stopped holding and was not
    re-derived leaves ``pr_reserved_paise`` still subtracting it, which REFUSES
    spending that is genuinely available -- quietly, and in the direction
    nobody reports as a bug.
    """
    if state == RESERVATION_LIVE_STATE or state not in RESERVATION_SETTLED_STATES:
        _err("UNKNOWN_RESERVATION_STATE",
             f"{state!r} does not release a hold. A rejection or cancellation "
             f"settles to 'Released' and an expiry to 'Expired'; "
             f"'Converted' belongs to convert_pr_to_po, which names the "
             f"purchase order that consumed the hold.", 422)

    cells = [(r["wbs_id"], r["budget_head_id"])
             for r in live_reservations(session, pr_id)]
    lock_affected_cells(session, cells)

    settled = settle_reservations(session, pr_id=pr_id, state=state,
                                  actor=actor)
    for wbs_id, head_id in settled:
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
    if settled:
        audit_mod.append(
            session, actor, "PR_RESERVATION_RELEASED", "PurchaseRequest",
            pr_id,
            f"{len(settled)} reservation(s) settled to {state} across "
            f"{len(settled)} control cell(s): "
            f"{', '.join(f'({w}, {h})' for w, h in settled)}."
            + (f" Reason: {reason}" if reason else ""),
            correlation_id=correlation_id)
    return settled


def settle_reservations(session: Session, *, pr_id: str, state: str,
                        actor: str, po_id: str | None = None
                        ) -> list[tuple[str, str]]:
    """Move every LIVE reservation on ``pr_id`` to ``state``, and say which
    cells moved so the caller can re-derive them.

    UPDATE, NEVER DELETE. `capex_app` has DELETE revoked on this table by
    migration 014 and that is deliberate: AUD-H-001 is "a reservation is
    Reserved, then Converted or Released or Expired, EXACTLY ONCE", and a
    deleted row has no such history. A released reservation stops holding
    budget because `state <> 'Reserved'`, not because it stopped existing.

    Returns the ``(wbs_id, budget_head_id)`` pairs it touched. The caller must
    already hold their locks and must call
    :func:`recompute_derived_position` for each -- releasing a hold that
    nothing re-derives leaves `pr_reserved_paise` overstating, which refuses
    spending that is now available.
    """
    if state == RESERVATION_LIVE_STATE or state not in RESERVATION_STATES:
        _err("UNKNOWN_RESERVATION_STATE",
             f"{state!r} is not a settled reservation state. "
             f"ck_pr_reservation_state admits "
             f"{', '.join(RESERVATION_STATES)}, and settling to "
             f"{RESERVATION_LIVE_STATE!r} is not settling.", 422)
    if state == "Converted" and not po_id:
        _err("CONVERTED_WITHOUT_PO",
             "A reservation converts INTO a purchase order. "
             "ck_pr_reservation_converted_has_po refuses a Converted row with "
             "no po_id, and so does this.", 422)
    rows = session.fetchall(  # scope-exempt: pr_id was scope-gated by the caller in this transaction
        """
        UPDATE pr_reservation
           SET state = %(state)s, po_id = %(po_id)s,
               settled_at = now(), settled_by = %(actor)s,
               updated_at = now(), updated_by = %(actor)s,
               version_no = version_no + 1
         WHERE pr_id = %(pr_id)s AND state = %(live)s
        RETURNING wbs_id, budget_head_id
        """,
        {"pr_id": pr_id, "state": state, "po_id": po_id, "actor": actor,
         "live": RESERVATION_LIVE_STATE},
    )
    touched = [(row[0], row[1]) for row in rows]

    # ONE APPEND-ONLY ENTRY, INSIDE THIS TRANSACTION, NAMING EVERY CELL.
    #
    # The actor is the caller's authenticated identity, passed down and never
    # derived from the row -- a reservation records who created it, and reusing
    # that here would attribute the settlement to the requestor rather than to
    # whoever settled it.
    #
    # Guarded on `touched` because a settle that matched nothing is a no-op,
    # and an audit line for a no-op is a line that says a hold was released
    # when no hold was released. That guard is also what makes a REPLAYED
    # settlement silent rather than duplicated: the second call's UPDATE
    # matches zero rows, so there is nothing to record.
    #
    # THIS TAKES THE ADVISORY AUDIT LOCK BEFORE THE CALLER'S RECOMPUTE, WHICH
    # LOOKS LIKE A BREACH OF THE GLOBAL LOCK ORDER AND IS NOT.
    #
    # `pg/locking.py`'s order is (1) cells, (2) the document row, (3) the
    # advisory audit lock, LAST -- and every caller of this function follows it
    # with `recompute_derived_position`, which UPDATEs a `budget_ledger_cell`
    # row and so takes that row's lock AFTER this append. The cycle that order
    # exists to prevent would need a second transaction holding one of those
    # ledger rows and waiting on the audit lock while this one holds the audit
    # lock and waits on the ledger row.
    #
    # It cannot arise, for the structural reason `recompute_derived_position`
    # already gives: a transaction holding a `budget_ledger_cell` row lock
    # necessarily holds that cell's `budget_control_cell` lock
    # (`fk_ledger_control_cell`, and every writer takes the control lock
    # first). Every cell this transaction is about to recompute is one it
    # ALREADY LOCKED, via `lock_affected_cells` with the complete affected set,
    # before anything here ran. No other transaction can hold those control
    # locks, so none can hold those ledger rows, so there is no second party to
    # form the cycle with. The audit lock is still the last NEW lock class this
    # transaction acquires; what follows it re-enters locks it owns.
    if touched:
        audit_mod.append(
            session, actor, "PR_RESERVATION_SETTLED", "PurchaseRequest", pr_id,
            f"{len(touched)} live reservation(s) settled to {state}"
            + (f" against {po_id}" if po_id else "")
            + ": " + ", ".join(f"({w}, {h})" for w, h in touched) + ".")
    return touched


# ================================= C5: fractional PO quantity policy, as data
#
# `po_line.quantity` is `numeric`; `outbound.PoLine.quantity` is `int`.
# `_emission_lines` refuses a fractional quantity with NON_INTEGER_QUANTITY
# rather than rounding it, because rounding silently changes what was ordered.
#
# THAT REFUSAL IS THE DEFAULT AND STAYS THE DEFAULT. It is now configurable
# rather than hard-coded, per the standing rule that a configurable business
# choice becomes configuration -- and the alternative is AUDITED, so a rounded
# quantity is never invisible.

POLICY_FRACTIONAL_QUANTITY = "FRACTIONAL_PO_QUANTITY"
POLICY_REFUSE = "REFUSE"
POLICY_ROUND_HALF_UP = "ROUND_HALF_UP"

#: The documented working default, used when `procurement_policy` has no row --
#: which is only possible on a database behind migration 014. REFUSE is the
#: safe value to be wrong with: it emits nothing and tells somebody.
FRACTIONAL_QUANTITY_DEFAULT = POLICY_REFUSE


def fractional_quantity_policy(session: Session) -> str:
    """What this deployment does with a fractional ordered quantity.

    Reads `procurement_policy`. A missing table or a missing row answers
    :data:`FRACTIONAL_QUANTITY_DEFAULT` -- and that is the ONE permissible
    default in this module, because it is the REFUSING value: it stops the
    emission and reports, rather than rounding on the strength of a row nobody
    could find.
    """
    if not _table_exists(session, "procurement_policy"):
        return FRACTIONAL_QUANTITY_DEFAULT
    row = session.fetchone(  # scope-exempt: procurement_policy is an organisation-wide rule table
        "SELECT policy_value FROM procurement_policy WHERE policy_key = %s",
        (POLICY_FRACTIONAL_QUANTITY,))
    return row[0] if row else FRACTIONAL_QUANTITY_DEFAULT


# ==================================== C6: procurement lifecycle transitions
#
# Plan section 12's PR and PO state machines, as data. An unknown transition is
# REFUSED rather than permitted -- the same fail-closed rule
# `domain.lifecycle_permits` follows for an unknown state.

_TRANSITION_OBJECT_TYPES = ("purchase_request", "purchase_order")


def assert_transition_permitted(session: Session, *, object_type: str,
                                from_state: str, to_state: str) -> None:
    """Refuse a state change `procurement_transition` does not carry.

    FAIL CLOSED, and the two ways of failing open are both closed:

    * an ABSENT TABLE does not permit everything. It raises
      TRANSITION_RULES_UNAVAILABLE, so a database behind migration 014 refuses
      the change rather than waving it through -- the same choice
      `periods._has_open_reconciliation_exceptions` made when 011 landed, and
      for the same reason: `False` there was the value that PERMITTED a close.
    * an UNKNOWN transition raises. There is no "not listed, so probably fine".

    A no-op (`from_state == to_state`) is permitted without a lookup:
    `ck_procurement_transition_not_self` means the table cannot carry one, and
    writing a row's own state back is not a transition.
    """
    if object_type not in _TRANSITION_OBJECT_TYPES:
        _err("UNKNOWN_TRANSITION_OBJECT",
             f"{object_type!r} has no state machine in procurement_transition; "
             f"it carries {', '.join(_TRANSITION_OBJECT_TYPES)}.", 422)
    if from_state == to_state:
        return
    if not _table_exists(session, "procurement_transition"):
        _err("TRANSITION_RULES_UNAVAILABLE",
             "procurement_transition does not exist, so no state change can "
             "be evaluated. Migration 014 creates it. The change was REFUSED "
             "rather than permitted: a gate that cannot run has not passed.",
             503)
    row = session.fetchone(  # scope-exempt: procurement_transition is an organisation-wide rule table
        "SELECT 1 FROM procurement_transition "
        "WHERE object_type = %s AND from_state = %s AND to_state = %s",
        (object_type, from_state, to_state))
    if row is None:
        _err("TRANSITION_NOT_PERMITTED",
             f"{object_type} may not move from {from_state!r} to "
             f"{to_state!r}. Valid transitions are DATA, in "
             f"procurement_transition, and an unlisted one is refused rather "
             f"than assumed. Adding it is a migration, not a code change.",
             422)


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

    ``reserve=True`` NOW SPANS SEVERAL CONTROL CELLS (migration 015, approved
    2026-09-08). One live hold per RESOLVED cell -- the budget-owning ancestor
    :func:`budget_verdicts` already checked the aggregated sum against -- so
    lines sharing a pot share ONE hold and lines in different pots get one
    each. All of them or none of them: a request that asks to hold budget it
    does not have is refused outright with
    :data:`ERR_RESERVATION_EXCEEDS_BUDGET` and nothing is created, because a
    partial hold is a hold nobody can reconcile against the document it belongs
    to. The old MULTI_CELL_RESERVATION_UNSUPPORTED refusal is gone; it existed
    only while the schema could not express the grain.
    """
    # `reserve=True` USED TO REFUSE HERE with PR_RESERVATION_NOT_MIGRATED, and
    # refusing was right while there was nowhere to write: silently ignoring
    # the flag would have told the caller budget was held when nothing held it.
    # Migration 014 creates `pr_reservation` and
    # `recompute_derived_position` writes `pr_reserved_paise`, so the
    # reservation is now taken rather than refused. The refusal is KEPT for a
    # database that somehow lacks the table -- see :func:`_assert_reservable`,
    # which probes rather than assumes, because a reservation reported as taken
    # and not taken is the one outcome worse than a refusal.
    if reserve:
        _assert_reservable(session)
        # And migration 015's half of the same question. Asked HERE, before
        # anything is normalised or written, so a database that cannot express
        # the resolved grain refuses the request rather than taking the first
        # hold and failing on the second.
        _assert_resolved_grain_available(session)

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
    number = pr_number or issue_document_number(
        session, PR_SERIES, actor=actor, object_type="PurchaseRequest",
        object_id=pr_id)
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

    # THE RESERVATION, taken AFTER the lines exist because
    # `fk_pr_reservation_pr_project` binds it to the header, and after the cell
    # locks because it changes `pr_reserved_paise` -- the limb
    # `check_availability` subtracts. Without it two requestors each pass
    # `budget_check` against the same rupees, because neither request has taken
    # anything out of availability.
    reservation_ids: list[str] = []
    if reserve:
        # THE GRAIN 014 COULD NOT EXPRESS, AND MIGRATION 015 DOES.
        #
        # 014's `ux_pr_reservation_live UNIQUE (pr_id) WHERE state = 'Reserved'`
        # permitted exactly one live hold per request. That was right for a
        # request addressing ONE control cell -- the SQLite POC's header-only
        # `purchase_request` -- and wrong for a `pr_line`-grained one, whose
        # lines can span two budget-owning cells and need one hold EACH. This
        # branch REFUSED such a request with MULTI_CELL_RESERVATION_UNSUPPORTED
        # rather than bending either rule.
        #
        # The product owner resolved it on 2026-09-08 and migration 015 built
        # `ux_pr_reservation_live_cell UNIQUE (pr_id, wbs_id, budget_head_id)
        # WHERE state = 'Reserved'`. The refusal is therefore GONE, replaced by
        # the two things that make the multi-cell path actually correct rather
        # than merely permitted:
        #
        #   1. the holds are placed on the RESOLVED cells -- the budget-owning
        #      ancestors `budget_verdicts` already checked the aggregated sums
        #      against -- so lines sharing a pot share ONE hold and lines in
        #      different pots get one each. Holding against a line's own cell
        #      would put two rows on one pot and pass each half in isolation;
        #   2. the whole thing is ATOMIC. If ANY resolved cell is short, the
        #      request is refused here, before a single row is written, and no
        #      cell is held. Reserving what fits and refusing the rest would
        #      leave a request holding budget for part of itself.
        #
        # `over` is `budget_verdicts`' answer computed above, INSIDE the cell
        # locks. A request that merely exceeds budget is still creatable as an
        # exception (check_result EXCEEDS_BUDGET, an approver, a reason); a
        # request that exceeds budget AND asks to HOLD it is not, because a
        # hold is not a proposal -- it takes the money out of everyone else's
        # availability the moment it is written.
        if over:
            _err(ERR_RESERVATION_EXCEEDS_BUDGET,
                 f"This request asked for budget to be HELD and at least one "
                 f"of the control cells it resolves to cannot cover it. "
                 f"NOTHING was created -- not the request, not its lines, and "
                 f"not one of its holds: a multi-cell reservation is all cells "
                 f"or none, and holding the cells that fit would leave this "
                 f"request holding budget for part of itself. "
                 + _shortfall_summary(verdicts) +
                 ". Raise it without reserve=True to record it as an exception "
                 f"for approval, or reduce the lines on the short cell.", 409)

        resolved = resolved_cell_amounts(verdicts)
        reservation_ids = reserve_pr_cells(
            session, pr_id=pr_id, project_id=project_id,
            resolved_amounts=resolved, actor=actor,
            correlation_id=correlation_id)
        session.execute(  # scope-exempt: the row this call created, under its own locks
            "UPDATE purchase_request SET reserves_budget = true, "
            "updated_at = now(), updated_by = %(actor)s "
            "WHERE pr_id = %(pr_id)s",
            {"pr_id": pr_id, "actor": actor})
        # Re-derive the RESOLVED cells -- the rows the holds were actually
        # written on, and therefore the rows whose `pr_reserved_paise` moved --
        # and the line cells besides. Every one of them is inside the lock set
        # taken above: `lock_affected_cells` expands each line cell to every
        # budget-owning ancestor on its chain, and a resolved cell IS such an
        # ancestor. Deriving only the line cells would leave the owner's
        # `pr_reserved_paise` at zero while a hold sat on it.
        for wbs_id, head_id in _affected_cells(normalised) + [
                cell for cell in resolved
                if cell not in _affected_cells(normalised)]:
            recompute_derived_position(session, wbs_id, head_id, actor=actor)

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
        "reservation_ids": tuple(reservation_ids),
        "reserves_budget": bool(reserve),
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
    # As `create_pr`: minted atomically from `numbering_series`, whose counter
    # row lock serialises concurrent issuers. `SELECT COUNT(*) + 1` reads under
    # no lock at all and collides on `ux_purchase_order_number`.
    number = po_number or issue_document_number(
        session, PO_SERIES, actor=actor, object_type="PurchaseOrder",
        object_id=po_id)
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
            # DERIVED ONLY WHEN IT IS EXACT. This was
            # `amount // units if units > 0 else amount`, which had two ways of
            # being wrong and no way of saying so:
            #
            #   * integer division TRUNCATED, so a 3-unit Rs 1,000.00 line got
            #     rate 333.33 and the vendor computed Rs 999.99;
            #   * a FRACTIONAL quantity set `units = 0`, and the fallback then
            #     made the rate the whole line total -- so a 2.5-unit line was
            #     invoiced at 2.5x, or 3x once the emission policy quantised
            #     the quantity, the amount the budget check cleared.
            #
            # Both refuse now. Zoho multiplies rate by quantity, so a total not
            # divisible by its quantity has no exact representation, and
            # picking the nearest one is how the two numbers diverged.
            as_float = float(quantity)
            if as_float != int(as_float):
                _err("PO_LINE_QUANTITY_NOT_WHOLE",
                     f"line {line['line_no']} has quantity {quantity}. A "
                     "purchase order line is emitted as rate x quantity, so a "
                     "fractional quantity has no exact per-unit price. Supply "
                     "`rate_paise` explicitly, or express the line in whole "
                     "units.", 422)
            units = int(as_float)
            if units <= 0:
                _err("PO_LINE_QUANTITY_NOT_POSITIVE",
                     f"line {line['line_no']} has quantity {quantity}, which "
                     "cannot price a line.", 422)
            if amount % units != 0:
                _err("PO_LINE_RATE_NOT_EXACT",
                     f"line {line['line_no']}: {amount} paise over {units} "
                     "units does not divide into a whole number of paise. The "
                     "vendor multiplies rate by quantity, so this line would "
                     "be invoiced for a different amount than was committed. "
                     "Split the line or supply `rate_paise`.", 422)
            rate = amount // units
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

    # THE RESERVATION IS SETTLED, NOT LEFT STANDING. A converted request whose
    # hold stays `Reserved` is counted TWICE against the same budget -- once as
    # `pr_reserved_paise` and again as the new order's `commitment_paise` --
    # which refuses spending that is genuinely available. AUD-H-001 is
    # "Reserved, then Converted or Released or Expired, EXACTLY ONCE", and
    # `ux_pr_reservation_live` would refuse a second hold on this request until
    # this one is settled.
    #
    # UPDATE, never DELETE: `capex_app` has no DELETE on this table, and a
    # deleted reservation has no history saying which purchase order consumed
    # it.
    settled = settle_reservations(
        session, pr_id=pr_id, state="Converted", actor=actor,
        po_id=written["po_id"]) if _table_exists(session, "pr_reservation") else []

    # As `create_po`: the derived position, under the locks taken above. The
    # settled cells are folded in, because a hold that stopped holding must be
    # re-derived or `pr_reserved_paise` keeps subtracting it.
    for wbs_id, head_id in _affected_cells(lines) + [
            cell for cell in settled if cell not in _affected_cells(lines)]:
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
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

def _emission_lines(po_line_rows: Sequence[Mapping[str, Any]],
                    *, fractional_quantity_policy: str = FRACTIONAL_QUANTITY_DEFAULT,
                    rounded: list[dict[str, Any]] | None = None
                    ) -> tuple[ob.PoLine, ...]:
    """``po_line`` rows as ``outbound.PoLine`` values, or a refusal.

    TWO SHAPES DO NOT SURVIVE THE CROSSING:

    * ``po_line.quantity`` is ``numeric`` and ``outbound.PoLine.quantity`` is
      ``int``. A fractional ordered quantity therefore has no representation on
      the emission path. Rounding it would send the vendor a different quantity
      from the one the commitment was checked against, so the DEFAULT is to
      refuse -- and REFUSE remains the default after migration 014 made this a
      setting rather than a hard-coded rule. `ROUND_HALF_UP` is the explicit
      alternative, and every line it rounds is appended to ``rounded`` so the
      caller can AUDIT it: a rounded quantity that nobody records is the silent
      change the refusal exists to prevent.
    * ``rate_paise`` carries no non-negative CHECK (013 constrains
      ``amount_paise``, ``non_creditable_tax_paise`` and ``freight_paise``
      only, deliberately), and ``outbound._require_paise`` refuses a negative.
      A negative unit price is refused here with a code rather than as a
      ``MoneyError`` from three frames down. That one is NOT configurable: a
      purchase order is a commitment and does not carry a negative unit price
      under any policy.

    PURE, still. The policy arrives as a VALUE, not as a session -- the whole
    emission decision is testable on a machine with no PostgreSQL, which is why
    `tests/test_procurement_emission.py` can prove the split, the dedupe keys
    and the at-most-once behaviour everywhere rather than only in CI.
    """
    if fractional_quantity_policy not in (POLICY_REFUSE, POLICY_ROUND_HALF_UP):
        _err("UNKNOWN_QUANTITY_POLICY",
             f"{fractional_quantity_policy!r} is not a fractional-quantity "
             f"policy. ck_procurement_policy_known admits {POLICY_REFUSE} and "
             f"{POLICY_ROUND_HALF_UP}. Nothing was emitted -- guessing which "
             f"was meant is how a quantity gets changed silently.", 500)
    out: list[ob.PoLine] = []
    for row in po_line_rows:
        quantity = row["quantity"]
        as_float = float(quantity)
        if not as_float.is_integer():
            if fractional_quantity_policy == POLICY_REFUSE:
                _err("NON_INTEGER_QUANTITY",
                     f"PO line {row['po_line_id']} has quantity {quantity}, "
                     f"which the emission path cannot carry: "
                     f"outbound.PoLine.quantity is an integer count. Rounding "
                     f"it would send the vendor a quantity the commitment was "
                     f"never checked against. This is a contract gap between "
                     f"`po_line.quantity numeric` and the emission DTO, not a "
                     f"data error. The deployment policy is {POLICY_REFUSE}; "
                     f"set procurement_policy.{POLICY_FRACTIONAL_QUANTITY} to "
                     f"{POLICY_ROUND_HALF_UP} to round and audit instead.",
                     422)
            # Decimal, not float: `round()` on a binary float is
            # banker's rounding on a value that may not be representable at
            # all, and 2.5 would go to 2. HALF_UP is the policy's own name and
            # is what it does.
            rounded_qty = int(Decimal(str(quantity)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP))
            if rounded is not None:
                rounded.append({
                    "po_line_id": row["po_line_id"],
                    "ordered_quantity": str(quantity),
                    "emitted_quantity": rounded_qty,
                })
            as_float = float(rounded_qty)
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
                        fractional_quantity_policy: str = FRACTIONAL_QUANTITY_DEFAULT,
                        rounded: list[dict[str, Any]] | None = None,
                        ) -> tuple[ob.EmissionPlan, list[dict[str, Any]]]:
    """The whole emission decision, with NO database anywhere in it.

    Returns the plan and, for each purchase order it decided on, the exact
    ``(local_id, dedupe_key, payload)`` triple that will be written to
    ``integration_outbox``.

    ``fractional_quantity_policy`` DEFAULTS TO REFUSE and arrives as a value so
    this function stays pure; ``plan_po_emission`` reads it from
    ``procurement_policy`` and passes it in. Any line the ROUND_HALF_UP policy
    rounds is appended to ``rounded``, which the caller audits.

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
        lines=_emission_lines(
            line_rows,
            fractional_quantity_policy=fractional_quantity_policy,
            rounded=rounded),
        capabilities=capabilities,
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
                     correlation_id: str | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
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

    # C5. REFUSE unless this deployment has explicitly chosen otherwise, and
    # every rounded line is audited below -- so a quantity that reached the
    # vendor differing from the one the commitment was checked against is on
    # the record with both values, rather than being a number nobody can
    # reconstruct.
    rounded_lines: list[dict[str, Any]] = []
    plan, planned = build_emission_plan(
        po_id=po_id, po_number=header["po_number"],
        connection_id=connection_id, vendor_external_id=vendor_external_id,
        document_date=document_date, capabilities=adapter.capabilities(),
        line_rows=rows, acknowledged=acknowledged,
        fractional_quantity_policy=fractional_quantity_policy(session),
        rounded=rounded_lines)

    for entry in rounded_lines:
        audit_mod.append(
            session, actor, "PO_QUANTITY_ROUNDED", "PurchaseOrder", po_id,
            f"Line {entry['po_line_id']} was ordered at quantity "
            f"{entry['ordered_quantity']} and emitted at "
            f"{entry['emitted_quantity']}. procurement_policy."
            f"{POLICY_FRACTIONAL_QUANTITY} is {POLICY_ROUND_HALF_UP}; under "
            f"the default {POLICY_REFUSE} this emission would have been "
            f"refused with NON_INTEGER_QUANTITY and nothing sent.",
            correlation_id=correlation_id)

    enqueued: list[dict[str, Any]] = []
    for entry in planned:
        # `now` IS FORWARDED, and the missing seam was a real defect rather
        # than an untidiness. `enqueue_outbound` stamps `created_at` from it,
        # and `PgOutboxStore.claim` will not claim a row until
        # `coalesce(next_attempt_at, created_at) <= now`. With the plan reading
        # the wall clock and the send reading an injected instant, every
        # planned row sat in the future of its own sender and refused as
        # NOT_EMITTABLE -- a caller that drives the two halves from one clock
        # could not make the pair agree, and neither could a test.
        outbox_id, created = store.enqueue_outbound(
            session, outbox_id=_new_id("OUT"), connection_id=connection_id,
            module=PO_MODULE, local_id=entry["local_id"],
            dedupe_key=entry["dedupe_key"], payload=entry["payload"],
            actor=actor, correlation_id=correlation_id, now=now)
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
