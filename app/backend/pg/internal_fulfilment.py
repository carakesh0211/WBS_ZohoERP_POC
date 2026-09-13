"""Internal material fulfilment and captive consumption (migration 034).

WHAT THIS MODULE DOES
=====================

An APPROVED purchase-request line is met in one of three ways -- bought
outside (EXTERNAL_PURCHASE), drawn from the company's own stock
(INTERNAL_TRANSFER), or both (SPLIT_FULFILMENT). This module records that
decision, raises and runs the INTERNAL MATERIAL REQUEST (IMR) for the internal
part, and keeps the budget honest while it does:

    REQUESTED -> APPROVED -> ALLOCATED -> PARTIALLY_ISSUED / ISSUED
              -> PARTIALLY_CONSUMED / CONSUMED, or RETURNED, or CANCELLED

Every quantity that moves is an append-only `internal_material_movement`; the
request carries running totals of them and no money of its own.

THE MONEY, IN ONE PLACE
=======================

This module WRITES NO LEDGER COLUMN. It inserts movements and moves the
reservation's `internal_moved_paise`, then calls
`procurement_services.recompute_derived_position`, the ONE statement that
derives a ledger column (`tests/test_pg_reservations.py` pins that). The two
internal limbs of exposure are derived there from the movements:

    internal_allocation_paise  = sum over requests of GREATEST(0,
                                   ALLOCATE - ISSUE - CANCEL)
    internal_consumption_paise = sum over requests of GREATEST(0,
                                   ISSUE - RETURN)

and exposure = commitment + actual + pr_reserved + internal_allocation +
internal_consumption, everywhere it is computed (`budget.py`,
`reporting.py`, `exports.py`, `closure.py`, `domain.py`).

Why that never double-counts:

* ALLOCATE moves the part of the request's live hold that covers it out of
  `pr_reserved` (through `pr_reservation.internal_moved_paise`) and into the
  allocation. Exposure rises by only the UNCOVERED part, and that part is
  checked against availability with `budget_verdicts` -- the same verdict a
  purchase order is refused on.
* ISSUE moves money from allocation to consumption. Exposure is unchanged.
* RETURN and CANCEL release. While the request's hold is still Reserved the
  released part goes BACK into the hold, so the purchase request keeps its
  budget for whatever still has to be fulfilled; once the request has
  converted, the release is a release.
* TRANSFER (store to store) and CONSUME (the site confirming use) move no
  money at all. A warehouse-to-warehouse transfer is not consumption.

The external part converts to a purchase order through
`procurement_services.convert_pr_to_po`, which reads the line's EXTERNAL
quantity and paise from `pr_line_fulfilment` -- so an order is never raised
for stock that is being drawn internally.

WHAT IS REFUSED RATHER THAN GUESSED
===================================

* A valuation the ERP has not supplied (see
  `integration/inventory_provider.py`) and nobody has entered with a reason is
  MISSING: the request is raised, an INTERNAL_VALUATION_MISSING exception is
  Open against it, and `allocate` refuses until a rate exists. Never zero.
* A line whose (project, WBS, budget head) resolves to no budget-owning cell
  is INTERNAL_MAPPING_MISSING: raised, Open, and unapprovable until resolved.
* A split that exceeds the approved line, an issue beyond the allocation, a
  return beyond what was issued and not consumed: each a 422 or 409 with the
  figures named.
* Approval is maker-checker (`imr.approve` is in both MAKER_CHECKER sets):
  the requester never approves their own request, whether directly or through
  a delegation.

LOCK ORDER. `tests/test_pg_locking_order.py` analyses this module:
`lock_affected_cells` once, first, before any availability read or ledger
write, in every function that does either.

SCOPE. Every read goes through `repo.query` with the `{scope}` token joined
to `project`; writes are keyed on an id such a read has already gated in the
same transaction and carry the `scope-exempt:` marker, the pattern
`procurement_services.py` documents.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ..integration import inventory_provider as inventory
from . import audit as audit_mod
from . import budget as budget_svc
from . import integration_store as store
from . import repo
from .delegation import DelegatedSelfApproval, assert_delegation_independent
from .engine import Session
from .locking import lock_affected_cells
from .procurement import retract_quarantine
from .procurement_services import (
    ERR_SELF_APPROVAL, ProcurementError, STATUS_APPROVED, _PROJECT_SCOPE_COLUMNS,
    _new_id, _pr_header, budget_verdicts, exceeds_budget, _shortfall_summary,
    issue_document_number, recompute_derived_position,
)

# ------------------------------------------------------------------ vocabulary
MODE_EXTERNAL = "EXTERNAL_PURCHASE"
MODE_INTERNAL = "INTERNAL_TRANSFER"
MODE_SPLIT = "SPLIT_FULFILMENT"
MODES: tuple[str, ...] = (MODE_EXTERNAL, MODE_INTERNAL, MODE_SPLIT)

STATUS_REQUESTED = "REQUESTED"
STATUS_IMR_APPROVED = "APPROVED"
STATUS_ALLOCATED = "ALLOCATED"
STATUS_PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
STATUS_ISSUED = "ISSUED"
STATUS_PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
STATUS_CONSUMED = "CONSUMED"
STATUS_RETURNED = "RETURNED"
STATUS_CANCELLED = "CANCELLED"
STATUSES: tuple[str, ...] = (
    STATUS_REQUESTED, STATUS_IMR_APPROVED, STATUS_ALLOCATED,
    STATUS_PARTIALLY_ISSUED, STATUS_ISSUED, STATUS_PARTIALLY_CONSUMED,
    STATUS_CONSUMED, STATUS_RETURNED, STATUS_CANCELLED,
)
TERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_CONSUMED, STATUS_RETURNED, STATUS_CANCELLED})

KIND_ALLOCATE = "ALLOCATE"
KIND_ISSUE = "ISSUE"
KIND_RETURN = "RETURN"
KIND_CONSUME = "CONSUME"
KIND_TRANSFER = "TRANSFER"
KIND_CANCEL = "CANCEL"

VALUATION_ERP = "ERP_STOCK"
VALUATION_MANUAL = "MANUAL"
VALUATION_MISSING = "MISSING"

KIND_INTERNAL_MAPPING_MISSING = "INTERNAL_MAPPING_MISSING"
KIND_INTERNAL_VALUATION_MISSING = "INTERNAL_VALUATION_MISSING"

OBJECT_TYPE = "InternalMaterialRequest"
EXCEPTION_OBJECT_TYPE = "internal_material_request"
SERIES_CODE = "INTERNAL_MATERIAL_REQUEST"
BASE_CURRENCY = "INR"

#: The permission each verb is held to, restated here so the router and the
#: service agree by construction rather than by two hand-typed strings.
PERMISSION_DECIDE = "fulfilment.decide"
PERMISSION_CREATE = "imr.create"
PERMISSION_APPROVE = "imr.approve"
PERMISSION_ALLOCATE = "imr.allocate"
PERMISSION_ISSUE = "imr.issue"
PERMISSION_CANCEL = "imr.cancel"
PERMISSION_READ = "imr.read"


def _err(code: str, message: str, status: int = 400, **detail: Any) -> None:
    raise ProcurementError(code, message, status=status, detail=detail)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- quantities
def _quantity(value: Any, *, what: str) -> Decimal:
    """A positive quantity as a Decimal. Text and ints are exact; a float is
    refused, because 0.1 + 0.2 is not a quantity of anything."""
    if isinstance(value, bool) or isinstance(value, float):
        _err("QUANTITY_NOT_EXACT",
             f"{what} must be an integer or a decimal string, not a float.", 422)
    try:
        q = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        _err("QUANTITY_INVALID", f"{what} is not a number: {value!r}.", 422)
    if q <= 0:
        _err("QUANTITY_NOT_POSITIVE", f"{what} must be positive; got {q}.", 422)
    return q


def _plain(q: Decimal) -> str:
    """A quantity on the wire: a decimal string, never a float."""
    s = format(q.normalize(), "f")
    return s if s != "-0" else "0"


def _money(quantity: Decimal, unit_rate_paise: int) -> int:
    """Integer paise for `quantity` at `unit_rate_paise`, half-up."""
    return int((quantity * Decimal(unit_rate_paise)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP))


def _portion(total_paise: int, part: Decimal, whole: Decimal) -> int:
    """`part / whole` of `total_paise`, half-up, in integer paise."""
    if whole <= 0:
        return 0
    return int((Decimal(total_paise) * part / whole).quantize(
        Decimal(1), rounding=ROUND_HALF_UP))


def split_line(line_amount_paise: int, line_quantity: Decimal,
               internal_quantity: Decimal) -> tuple[int, int]:
    """The line's paise split into (external, internal) that sum EXACTLY.

    The internal portion is the pro-rata share, half-up; the external portion
    is the remainder -- never a second rounding -- so the two always add back
    to the line and `ck_pr_line_fulfilment_amount_split` holds by
    construction.
    """
    internal = _portion(line_amount_paise, internal_quantity, line_quantity)
    return line_amount_paise - internal, internal


def _status_from(allocated: Decimal, issued: Decimal, returned: Decimal,
                 consumed: Decimal) -> str:
    """The lifecycle status the running totals imply, after an allocation."""
    if allocated > 0 and issued == allocated and returned + consumed == issued:
        return STATUS_CONSUMED if consumed > 0 else STATUS_RETURNED
    if consumed > 0:
        return STATUS_PARTIALLY_CONSUMED
    if issued == 0:
        return STATUS_ALLOCATED
    if issued == allocated:
        return STATUS_ISSUED
    return STATUS_PARTIALLY_ISSUED


# ---------------------------------------------------------------------- reads
_LINE_SQL = """
    SELECT l.pr_line_id, l.pr_id, l.line_no, l.project_id, l.wbs_id,
           l.budget_head_id, l.description, l.quantity, l.amount_paise,
           pr.pr_number, pr.status, pr.requested_by, p.entity_id, w.wbs_code
    FROM pr_line l
    JOIN purchase_request pr ON pr.pr_id = l.pr_id
    JOIN project p ON p.project_id = l.project_id
    JOIN wbs_element w ON w.wbs_id = l.wbs_id
    WHERE l.pr_line_id = %(pr_line_id)s AND {scope}
"""


def _line(session: Session, pr_line_id: str) -> dict[str, Any]:
    row = repo.query_one(session, _LINE_SQL, {"pr_line_id": pr_line_id},
                         columns=_PROJECT_SCOPE_COLUMNS)
    if row is None:
        _err("PR_LINE_NOT_FOUND",
             f"Purchase-request line {pr_line_id} does not exist.", 404)
    return {
        "pr_line_id": row[0], "pr_id": row[1], "line_no": row[2],
        "project_id": row[3], "wbs_id": row[4], "budget_head_id": row[5],
        "description": row[6], "quantity": Decimal(str(row[7])),
        "amount_paise": int(row[8]), "pr_number": row[9], "pr_status": row[10],
        "requested_by": row[11], "entity_id": row[12], "wbs_code": row[13],
    }


_IMR_SQL = """
    SELECT imr.imr_id, imr.imr_number, imr.pr_id, imr.pr_line_id,
           imr.project_id, imr.wbs_id, imr.budget_head_id,
           imr.item_external_id, imr.item_description,
           imr.from_location_id, imr.to_location_id,
           imr.requested_quantity, imr.approved_quantity,
           imr.allocated_quantity, imr.issued_quantity,
           imr.returned_quantity, imr.consumed_quantity,
           imr.unit_rate_paise, imr.valuation_source, imr.valuation_reference,
           imr.valuation_note, imr.status, imr.mapping_ok, imr.reason,
           imr.requested_by, imr.approved_by, imr.approved_at,
           imr.cancelled_at, imr.cancel_reason, imr.created_at, imr.created_by,
           imr.updated_at, imr.version_no, p.entity_id, pr.pr_number, w.wbs_code
    FROM internal_material_request imr
    JOIN project p ON p.project_id = imr.project_id
    JOIN purchase_request pr ON pr.pr_id = imr.pr_id
    JOIN wbs_element w ON w.wbs_id = imr.wbs_id
"""


def _imr_from_row(row: tuple) -> dict[str, Any]:
    dec = lambda v: None if v is None else Decimal(str(v))  # noqa: E731
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    return {
        "imr_id": row[0], "imr_number": row[1], "pr_id": row[2],
        "pr_line_id": row[3], "project_id": row[4], "wbs_id": row[5],
        "budget_head_id": row[6], "item_external_id": row[7],
        "item_description": row[8], "from_location_id": row[9],
        "to_location_id": row[10],
        "requested_quantity": dec(row[11]), "approved_quantity": dec(row[12]),
        "allocated_quantity": dec(row[13]), "issued_quantity": dec(row[14]),
        "returned_quantity": dec(row[15]), "consumed_quantity": dec(row[16]),
        "unit_rate_paise": None if row[17] is None else int(row[17]),
        "valuation_source": row[18], "valuation_reference": row[19],
        "valuation_note": row[20], "status": row[21], "mapping_ok": bool(row[22]),
        "reason": row[23], "requested_by": row[24], "approved_by": row[25],
        "approved_at": iso(row[26]), "cancelled_at": iso(row[27]),
        "cancel_reason": row[28], "created_at": iso(row[29]),
        "created_by": row[30], "updated_at": iso(row[31]),
        "version_no": int(row[32]), "entity_id": row[33], "pr_number": row[34],
        "wbs_code": row[35],
    }


def _imr(session: Session, imr_id: str) -> dict[str, Any]:
    """The request, scope-gated. Out of scope answers as absent."""
    row = repo.query_one(session, _IMR_SQL + " WHERE imr.imr_id = %(imr_id)s AND {scope}",
                         {"imr_id": imr_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if row is None:
        _err("IMR_NOT_FOUND",
             f"Internal material request {imr_id} does not exist.", 404)
    return _imr_from_row(row)


def _money_of(session: Session, imr_id: str) -> dict[str, int]:
    """The request's money, derived from its movements exactly as the ledger
    derives it. Read after the row lock; never stored on the request."""
    row = session.fetchone(  # scope-exempt: movements of a request already scope-gated in this call
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind = 'ALLOCATE' THEN amount_paise ELSE 0 END), 0)::bigint,
          COALESCE(SUM(CASE WHEN kind = 'ISSUE'    THEN amount_paise ELSE 0 END), 0)::bigint,
          COALESCE(SUM(CASE WHEN kind = 'RETURN'   THEN amount_paise ELSE 0 END), 0)::bigint,
          COALESCE(SUM(CASE WHEN kind = 'CANCEL'   THEN amount_paise ELSE 0 END), 0)::bigint
        FROM internal_material_movement WHERE imr_id = %s
        """, (imr_id,))
    allocated, issued, returned, cancelled = (int(v) for v in row)
    return {
        "allocated_paise": allocated, "issued_paise": issued,
        "returned_paise": returned, "cancelled_paise": cancelled,
        "internal_allocation_paise": max(0, allocated - issued - cancelled),
        "internal_consumption_paise": max(0, issued - returned),
    }


def _public(imr: Mapping[str, Any], money: Mapping[str, int] | None = None
            ) -> dict[str, Any]:
    out = dict(imr)
    for key in ("requested_quantity", "approved_quantity", "allocated_quantity",
                "issued_quantity", "returned_quantity", "consumed_quantity"):
        if out.get(key) is not None:
            out[key] = _plain(out[key])
    if money is not None:
        out.update(money)
    return out


def _lock_imr_row(session: Session, imr_id: str) -> dict[str, Any]:
    """Rule 3 step 2: the document row, under FOR UPDATE, re-read."""
    session.execute(  # scope-exempt: locks a row already scope-gated in this call
        "SELECT imr_id FROM internal_material_request WHERE imr_id = %s FOR UPDATE",
        (imr_id,))
    row = session.fetchone(  # scope-exempt: re-reads the row just locked above
        """
        SELECT status, approved_quantity, allocated_quantity, issued_quantity,
               returned_quantity, consumed_quantity, unit_rate_paise,
               valuation_source, version_no, requested_by, mapping_ok,
               from_location_id
        FROM internal_material_request WHERE imr_id = %s
        """, (imr_id,))
    if row is None:
        _err("IMR_NOT_FOUND",
             f"Internal material request {imr_id} does not exist.", 404)
    dec = lambda v: Decimal(0) if v is None else Decimal(str(v))  # noqa: E731
    return {
        "status": row[0], "approved_quantity": dec(row[1]),
        "allocated_quantity": dec(row[2]), "issued_quantity": dec(row[3]),
        "returned_quantity": dec(row[4]), "consumed_quantity": dec(row[5]),
        "unit_rate_paise": None if row[6] is None else int(row[6]),
        "valuation_source": row[7], "version_no": int(row[8]),
        "requested_by": row[9], "mapping_ok": bool(row[10]),
        "from_location_id": row[11],
    }


def _assert_version(expected: int | None, current: int, *, label: str) -> None:
    if expected is not None and expected != current:
        _err("VERSION_CONFLICT",
             f"{label} is at version {current}; you supplied {expected}. "
             f"Re-read it and retry.", 409)


def _existing_movement(session: Session, imr_id: str, idempotency_key: str
                       ) -> tuple | None:
    return session.fetchone(  # scope-exempt: keyed on a request already scope-gated in this call
        "SELECT movement_id, kind, quantity, amount_paise FROM "
        "internal_material_movement WHERE imr_id = %s AND idempotency_key = %s",
        (imr_id, idempotency_key))


def _require_key(idempotency_key: str | None) -> str:
    key = (idempotency_key or "").strip()
    if not key:
        _err("IDEMPOTENCY_KEY_REQUIRED",
             "Every movement names an idempotency_key so a retry lands on the "
             "same row rather than moving the stock twice.", 422)
    return key


def _location_exists(session: Session, location_id: str | None, *, what: str) -> None:
    if location_id is None:
        return
    row = session.fetchone(  # scope-exempt: existence of a master row named by id; RLS still applies
        "SELECT location_id FROM location WHERE location_id = %s", (location_id,))
    if row is None:
        _err("LOCATION_NOT_FOUND", f"{what} {location_id} does not exist.", 404)


def _movement(session: Session, *, imr: Mapping[str, Any], kind: str,
              quantity: Decimal, amount_paise: int, idempotency_key: str,
              actor: str, from_location_id: str | None = None,
              to_location_id: str | None = None, reference: str | None = None,
              note: str | None = None, correlation_id: str | None = None) -> str:
    movement_id = _new_id("IMM")
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call; 034's WITH CHECK policy also applies
        """
        INSERT INTO internal_material_movement (
            movement_id, imr_id, project_id, wbs_id, budget_head_id, kind,
            quantity, amount_paise, from_location_id, to_location_id,
            idempotency_key, reference, note, correlation_id, created_by)
        VALUES (%(movement_id)s, %(imr_id)s, %(project_id)s, %(wbs_id)s,
                %(head)s, %(kind)s, %(quantity)s, %(amount)s, %(from_loc)s,
                %(to_loc)s, %(key)s, %(reference)s, %(note)s, %(corr)s, %(actor)s)
        """,
        {"movement_id": movement_id, "imr_id": imr["imr_id"],
         "project_id": imr["project_id"], "wbs_id": imr["wbs_id"],
         "head": imr["budget_head_id"], "kind": kind, "quantity": quantity,
         "amount": amount_paise, "from_loc": from_location_id,
         "to_loc": to_location_id, "key": idempotency_key,
         "reference": reference, "note": note, "corr": correlation_id,
         "actor": actor})
    return movement_id


def _update_quantities(session: Session, imr_id: str, *, actor: str,
                       status: str, **columns: Any) -> None:
    sets = ", ".join(f"{name} = %({name})s" for name in columns)
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call
        f"UPDATE internal_material_request SET {sets}, status = %(status)s, "
        f"updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1 "
        f"WHERE imr_id = %(imr_id)s",
        {**columns, "status": status, "actor": actor, "imr_id": imr_id})


# --------------------------------------------------------------- the reservation
def _live_reservation(session: Session, *, pr_id: str, owner_wbs_id: str,
                      budget_head_id: str) -> tuple[str, int, int] | None:
    """The request's live hold on the OWNING cell, locked. `(id, amount,
    moved)` or None when the request holds nothing there."""
    row = session.fetchone(  # scope-exempt: the hold of a request already scope-gated in this call
        """
        SELECT reservation_id, amount_paise, internal_moved_paise
        FROM pr_reservation
        WHERE pr_id = %s AND wbs_id = %s AND budget_head_id = %s
          AND state = 'Reserved'
        FOR UPDATE
        """, (pr_id, owner_wbs_id, budget_head_id))
    return None if row is None else (row[0], int(row[1]), int(row[2]))


def _move_hold(session: Session, reservation_id: str, delta: int, *, actor: str) -> None:
    session.execute(  # scope-exempt: keyed on a hold locked above
        "UPDATE pr_reservation SET internal_moved_paise = internal_moved_paise + %s, "
        "updated_at = now(), updated_by = %s, version_no = version_no + 1 "
        "WHERE reservation_id = %s", (delta, actor, reservation_id))


def _owner_of(session: Session, wbs_id: str, budget_head_id: str) -> str | None:
    """The budget-owning ancestor cell, or None when the mapping resolves to
    no owner (the INTERNAL_MAPPING_MISSING case)."""
    try:
        return budget_svc.check_availability(session, wbs_id, budget_head_id, 0)["owning_wbs_id"]
    except budget_svc.BudgetServiceError:
        return None


def _give_back(session: Session, *, imr: Mapping[str, Any], amount_paise: int,
               actor: str) -> tuple[int, str | None]:
    """Return up to `amount_paise` of released allocation to the request's
    live hold, if it still has one. Returns (given back, owner cell)."""
    if amount_paise <= 0:
        return 0, None
    owner = _owner_of(session, imr["wbs_id"], imr["budget_head_id"])
    if owner is None:
        return 0, None
    hold = _live_reservation(session, pr_id=imr["pr_id"], owner_wbs_id=owner,
                             budget_head_id=imr["budget_head_id"])
    if hold is None:
        return 0, owner
    reservation_id, _amount, moved = hold
    back = min(amount_paise, moved)
    if back > 0:
        _move_hold(session, reservation_id, -back, actor=actor)
    return back, owner


def _distinct(cells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The cells to re-derive, each once. The re-derivation itself is
    written out at every call site rather than wrapped, so
    `tests/test_pg_locking_order.py` sees the write in the function that
    took the lock."""
    seen: list[tuple[str, str]] = []
    for cell in cells:
        if cell not in seen:
            seen.append(cell)
    return seen


# ------------------------------------------------------------------ exceptions
def _raise_exception(session: Session, *, kind: str, imr_id: str, detail: str,
                     entity_id: str, project_id: str, actor: str,
                     correlation_id: str | None) -> str:
    return store.raise_exception(
        session, kind=kind, object_type=EXCEPTION_OBJECT_TYPE, object_id=imr_id,
        detail=detail, entity_id=entity_id, project_id=project_id,
        correlation_id=correlation_id, actor=actor)


def _retract(session: Session, *, kind: str, imr_id: str, reason: str,
             actor: str, correlation_id: str | None) -> None:
    retract_quarantine(session, kind=kind, object_type=EXCEPTION_OBJECT_TYPE,
                       object_id=imr_id, reason=reason, actor=actor,
                       correlation_id=correlation_id)


# ----------------------------------------------------------------- valuation
def _resolve_valuation(item_external_id: str | None,
                       location_external_id: str | None,
                       *, unit_rate_paise: int | None,
                       valuation_note: str | None) -> dict[str, Any]:
    """ERP first; a reasoned manual figure second; MISSING otherwise."""
    if item_external_id:
        found = inventory.get_inventory_provider().valuation(
            item_external_id, location_external_id)
        if found is not None:
            if found.currency != BASE_CURRENCY:
                # A foreign valuation is not divided into rupees here; it is
                # a missing base valuation with the reason recorded.
                return {"unit_rate_paise": None, "valuation_source": VALUATION_MISSING,
                        "valuation_reference": f"{found.source}:{found.currency}",
                        "valuation_note": (f"the provider valued the item in "
                                           f"{found.currency}; a base-currency "
                                           f"valuation is required")}
            return {"unit_rate_paise": int(found.unit_rate_minor),
                    "valuation_source": VALUATION_ERP,
                    "valuation_reference": f"{found.source}:{found.as_of.isoformat()}",
                    "valuation_note": None}
    if unit_rate_paise is not None:
        if isinstance(unit_rate_paise, bool) or not isinstance(unit_rate_paise, int):
            _err("RATE_NOT_INTEGER_PAISE",
                 "unit_rate_paise is integer paise, never a float.", 422)
        if unit_rate_paise <= 0:
            _err("RATE_NOT_POSITIVE",
                 "A manual valuation is positive; a zero valuation is a missing "
                 "one and is recorded as such.", 422)
        if not (valuation_note or "").strip():
            _err("VALUATION_NOTE_REQUIRED",
                 "A manual valuation carries the reason and the source it was "
                 "taken from; it is written into the audit trail.", 422)
        return {"unit_rate_paise": unit_rate_paise,
                "valuation_source": VALUATION_MANUAL,
                "valuation_reference": None, "valuation_note": valuation_note.strip()}
    return {"unit_rate_paise": None, "valuation_source": VALUATION_MISSING,
            "valuation_reference": None, "valuation_note": None}


# =============================================================== the decision
def decide_fulfilment(session: Session, *, pr_line_id: str, mode: str,
                      actor: str, internal_quantity: Any = None,
                      reason: str | None = None,
                      item_external_id: str | None = None,
                      item_description: str | None = None,
                      from_location_id: str | None = None,
                      to_location_id: str | None = None,
                      unit_rate_paise: int | None = None,
                      valuation_note: str | None = None,
                      correlation_id: str | None = None) -> dict[str, Any]:
    """Record how an APPROVED purchase-request line is to be fulfilled.

    Writes `pr_line_fulfilment` (insert or replace) and, for an internal or
    split decision, raises the internal material request for the internal
    quantity in one transaction -- a decision to draw from stores with no
    request behind it is a decision nobody can act on.

    Refused when the request is not Approved, has already converted (the
    order is out; the split is fixed), or the line already has a live
    request (cancel it first -- its allocation is the thing the decision
    would silently orphan).
    """
    if mode not in MODES:
        _err("FULFILMENT_MODE_INVALID",
             f"mode must be one of {', '.join(MODES)}; got {mode!r}.", 422)
    line = _line(session, pr_line_id)
    if line["pr_status"] != STATUS_APPROVED:
        _err("PR_NOT_APPROVED",
             f"{line['pr_number']} is {line['pr_status']}. A fulfilment "
             f"decision is taken on an Approved request.", 409)

    # Rule 1: the cell, once, first -- before the mapping probe below and
    # before anything that could be re-derived.
    lock_affected_cells(session, [(line["wbs_id"], line["budget_head_id"])])
    session.execute(  # scope-exempt: locks the request row scope-gated by _line above
        "SELECT pr_id FROM purchase_request WHERE pr_id = %s FOR UPDATE",
        (line["pr_id"],))
    converted = session.fetchone(  # scope-exempt: asked under the request's row lock
        "SELECT po_number FROM purchase_order WHERE pr_id = %s ORDER BY created_at LIMIT 1",
        (line["pr_id"],))
    if converted is not None:
        _err("PR_ALREADY_CONVERTED",
             f"{line['pr_number']} has already converted to {converted[0]}; "
             f"its fulfilment is fixed.", 409)
    live = session.fetchone(  # scope-exempt: the line's live request, under the request's lock
        "SELECT imr_id, imr_number, status FROM internal_material_request "
        "WHERE pr_line_id = %s AND status NOT IN ('CONSUMED', 'RETURNED', 'CANCELLED')",
        (pr_line_id,))
    if live is not None:
        _err("FULFILMENT_LOCKED_BY_IMR",
             f"Line {line['line_no']} of {line['pr_number']} has a live internal "
             f"material request {live[1]} ({live[2]}); cancel it before "
             f"changing the decision.", 409, imr_id=live[0])

    qty = line["quantity"]
    if mode == MODE_EXTERNAL:
        internal_q = Decimal(0)
    elif mode == MODE_INTERNAL:
        internal_q = qty
    else:
        if internal_quantity is None:
            _err("SPLIT_QUANTITY_REQUIRED",
                 "A split names the internal quantity.", 422)
        internal_q = _quantity(internal_quantity, what="internal_quantity")
        if internal_q >= qty:
            _err("SPLIT_QUANTITY_INVALID",
                 f"internal_quantity {_plain(internal_q)} must be less than the "
                 f"line's {_plain(qty)} for a split; use INTERNAL_TRANSFER for "
                 f"the whole line.", 422)
    external_q = qty - internal_q
    external_paise, internal_paise = split_line(line["amount_paise"], qty, internal_q)

    session.execute(  # scope-exempt: keyed on a line scope-gated by _line above; 034's WITH CHECK policy also applies
        """
        INSERT INTO pr_line_fulfilment (
            pr_line_id, pr_id, project_id, mode, line_quantity,
            external_quantity, internal_quantity, line_amount_paise,
            external_amount_paise, internal_amount_paise, reason,
            decided_by, created_by, updated_by)
        VALUES (%(line)s, %(pr)s, %(project)s, %(mode)s, %(qty)s, %(ext_q)s,
                %(int_q)s, %(amount)s, %(ext_p)s, %(int_p)s, %(reason)s,
                %(actor)s, %(actor)s, %(actor)s)
        ON CONFLICT (pr_line_id) DO UPDATE SET
            mode = EXCLUDED.mode, line_quantity = EXCLUDED.line_quantity,
            external_quantity = EXCLUDED.external_quantity,
            internal_quantity = EXCLUDED.internal_quantity,
            line_amount_paise = EXCLUDED.line_amount_paise,
            external_amount_paise = EXCLUDED.external_amount_paise,
            internal_amount_paise = EXCLUDED.internal_amount_paise,
            reason = EXCLUDED.reason, decided_at = now(),
            decided_by = EXCLUDED.decided_by, updated_at = now(),
            updated_by = EXCLUDED.updated_by,
            version_no = pr_line_fulfilment.version_no + 1
        """,
        {"line": pr_line_id, "pr": line["pr_id"], "project": line["project_id"],
         "mode": mode, "qty": qty, "ext_q": external_q, "int_q": internal_q,
         "amount": line["amount_paise"], "ext_p": external_paise,
         "int_p": internal_paise, "reason": reason, "actor": actor})
    audit_mod.append(
        session, actor, "FULFILMENT_DECIDED", "PurchaseRequest", line["pr_id"],
        f"Line {line['line_no']} of {line['pr_number']}: {mode}; external "
        f"{_plain(external_q)} ({external_paise} paise), internal "
        f"{_plain(internal_q)} ({internal_paise} paise)"
        + (f"; reason: {reason}" if reason else ""),
        correlation_id=correlation_id)

    decision = {
        "pr_line_id": pr_line_id, "pr_id": line["pr_id"],
        "pr_number": line["pr_number"], "line_no": line["line_no"],
        "mode": mode, "line_quantity": _plain(qty),
        "external_quantity": _plain(external_q),
        "internal_quantity": _plain(internal_q),
        "line_amount_paise": line["amount_paise"],
        "external_amount_paise": external_paise,
        "internal_amount_paise": internal_paise, "reason": reason,
        "imr": None,
    }
    if internal_q > 0:
        decision["imr"] = _create_imr(
            session, line=line, quantity=internal_q, actor=actor,
            item_external_id=item_external_id, item_description=item_description,
            from_location_id=from_location_id, to_location_id=to_location_id,
            unit_rate_paise=unit_rate_paise, valuation_note=valuation_note,
            reason=reason, correlation_id=correlation_id)
    return decision


def fulfilment_of(session: Session, pr_id: str) -> list[dict[str, Any]]:
    """Every line of the request with its decision (defaulting to
    EXTERNAL_PURCHASE when none is recorded) and its live request, if any."""
    _pr_header(session, pr_id)  # scope gate; 404 when absent
    rows = repo.query(
        session,
        """
        SELECT l.pr_line_id, l.line_no, l.wbs_id, w.wbs_code, l.budget_head_id,
               l.description, l.quantity, l.amount_paise,
               f.mode, f.external_quantity, f.internal_quantity,
               f.external_amount_paise, f.internal_amount_paise, f.reason,
               f.decided_by, f.decided_at,
               imr.imr_id, imr.imr_number, imr.status
        FROM pr_line l
        JOIN project p ON p.project_id = l.project_id
        JOIN wbs_element w ON w.wbs_id = l.wbs_id
        LEFT JOIN pr_line_fulfilment f ON f.pr_line_id = l.pr_line_id
        LEFT JOIN internal_material_request imr
               ON imr.pr_line_id = l.pr_line_id
              AND imr.status NOT IN ('CONSUMED', 'RETURNED', 'CANCELLED')
        WHERE l.pr_id = %(pr_id)s AND {scope}
        ORDER BY l.line_no
        """,
        {"pr_id": pr_id}, columns=_PROJECT_SCOPE_COLUMNS)
    out = []
    for r in rows:
        qty = Decimal(str(r[6]))
        decided = r[8] is not None
        out.append({
            "pr_line_id": r[0], "line_no": r[1], "wbs_id": r[2], "wbs_code": r[3],
            "budget_head_id": r[4], "description": r[5],
            "line_quantity": _plain(qty), "line_amount_paise": int(r[7]),
            "mode": r[8] if decided else MODE_EXTERNAL, "decided": decided,
            "external_quantity": _plain(Decimal(str(r[9]))) if decided else _plain(qty),
            "internal_quantity": _plain(Decimal(str(r[10]))) if decided else "0",
            "external_amount_paise": int(r[11]) if decided else int(r[7]),
            "internal_amount_paise": int(r[12]) if decided else 0,
            "reason": r[13], "decided_by": r[14],
            "decided_at": r[15].isoformat() if r[15] else None,
            "imr_id": r[16], "imr_number": r[17], "imr_status": r[18],
        })
    return out


# ================================================================ the request
def _create_imr(session: Session, *, line: Mapping[str, Any], quantity: Decimal,
                actor: str, item_external_id: str | None,
                item_description: str | None, from_location_id: str | None,
                to_location_id: str | None, unit_rate_paise: int | None,
                valuation_note: str | None, reason: str | None,
                correlation_id: str | None) -> dict[str, Any]:
    """Insert the request in REQUESTED, with its mapping probed and its
    valuation resolved -- or its two exceptions raised. Called with the
    cell already locked."""
    _location_exists(session, from_location_id, what="from_location_id")
    _location_exists(session, to_location_id, what="to_location_id")
    imr_id = _new_id("IMR")
    owner = _owner_of(session, line["wbs_id"], line["budget_head_id"])
    valuation = _resolve_valuation(
        item_external_id, from_location_id, unit_rate_paise=unit_rate_paise,
        valuation_note=valuation_note)
    number = issue_document_number(session, SERIES_CODE, actor=actor,
                                   object_type=EXCEPTION_OBJECT_TYPE,
                                   object_id=imr_id)
    session.execute(  # scope-exempt: keyed on a line scope-gated by the caller; 034's WITH CHECK policy also applies
        """
        INSERT INTO internal_material_request (
            imr_id, imr_number, pr_id, pr_line_id, project_id, wbs_id,
            budget_head_id, item_external_id, item_description,
            from_location_id, to_location_id, requested_quantity,
            unit_rate_paise, valuation_source, valuation_reference,
            valuation_note, status, mapping_ok, reason, requested_by,
            created_by, updated_by)
        VALUES (%(imr_id)s, %(number)s, %(pr_id)s, %(pr_line_id)s, %(project)s,
                %(wbs)s, %(head)s, %(item)s, %(item_desc)s, %(from_loc)s,
                %(to_loc)s, %(qty)s, %(rate)s, %(source)s, %(reference)s,
                %(note)s, %(status)s, %(mapping_ok)s, %(reason)s, %(actor)s,
                %(actor)s, %(actor)s)
        """,
        {"imr_id": imr_id, "number": number, "pr_id": line["pr_id"],
         "pr_line_id": line["pr_line_id"], "project": line["project_id"],
         "wbs": line["wbs_id"], "head": line["budget_head_id"],
         "item": item_external_id,
         "item_desc": item_description or line["description"],
         "from_loc": from_location_id, "to_loc": to_location_id,
         "qty": quantity, "rate": valuation["unit_rate_paise"],
         "source": valuation["valuation_source"],
         "reference": valuation["valuation_reference"],
         "note": valuation["valuation_note"], "status": STATUS_REQUESTED,
         "mapping_ok": owner is not None, "reason": reason, "actor": actor})
    exceptions: list[str] = []
    if owner is None:
        exceptions.append(_raise_exception(
            session, kind=KIND_INTERNAL_MAPPING_MISSING, imr_id=imr_id,
            detail=(f"{number}: project {line['project_id']} x WBS "
                    f"{line['wbs_code']} x budget head {line['budget_head_id']} "
                    f"resolves to no budget-owning control cell; the request "
                    f"cannot be approved until the mapping exists."),
            entity_id=line["entity_id"], project_id=line["project_id"],
            actor=actor, correlation_id=correlation_id))
    if valuation["unit_rate_paise"] is None:
        exceptions.append(_raise_exception(
            session, kind=KIND_INTERNAL_VALUATION_MISSING, imr_id=imr_id,
            detail=(f"{number}: no stock valuation for item "
                    f"{item_external_id or '(none named)'} -- the inventory "
                    f"provider answered nothing and no reasoned manual "
                    f"valuation was entered; the request cannot be allocated "
                    f"until one exists."
                    + (f" {valuation['valuation_note']}." if valuation["valuation_note"] else "")),
            entity_id=line["entity_id"], project_id=line["project_id"],
            actor=actor, correlation_id=correlation_id))
    audit_mod.append(
        session, actor, "IMR_REQUESTED", OBJECT_TYPE, imr_id,
        f"{number} raised for {_plain(quantity)} of line {line['line_no']} of "
        f"{line['pr_number']} on ({line['wbs_code']}, {line['budget_head_id']}); "
        f"valuation {valuation['valuation_source']}"
        + (f" at {valuation['unit_rate_paise']} paise/unit" if valuation["unit_rate_paise"] else "")
        + (f"; mapping missing" if owner is None else "")
        + (f"; exceptions {exceptions}" if exceptions else ""),
        correlation_id=correlation_id)
    return _public({**_imr(session, imr_id), "exception_ids": exceptions})


def create_request(session: Session, *, pr_line_id: str, quantity: Any,
                   actor: str, item_external_id: str | None = None,
                   item_description: str | None = None,
                   from_location_id: str | None = None,
                   to_location_id: str | None = None,
                   unit_rate_paise: int | None = None,
                   valuation_note: str | None = None,
                   reason: str | None = None,
                   correlation_id: str | None = None) -> dict[str, Any]:
    """Raise a request for (part of) a line's INTERNAL quantity -- after a
    prior request on the line was cancelled or returned, or for a smaller
    tranche than the decision. Never more than the decision's internal
    quantity, and never beside a live request on the same line."""
    line = _line(session, pr_line_id)
    q = _quantity(quantity, what="quantity")
    lock_affected_cells(session, [(line["wbs_id"], line["budget_head_id"])])
    session.execute(  # scope-exempt: locks the request row scope-gated by _line above
        "SELECT pr_id FROM purchase_request WHERE pr_id = %s FOR UPDATE",
        (line["pr_id"],))
    decision = session.fetchone(  # scope-exempt: the line's decision, under the request's lock
        "SELECT mode, internal_quantity FROM pr_line_fulfilment WHERE pr_line_id = %s",
        (pr_line_id,))
    if decision is None or decision[0] == MODE_EXTERNAL:
        _err("FULFILMENT_NOT_INTERNAL",
             f"Line {line['line_no']} of {line['pr_number']} has no internal "
             f"fulfilment decision; decide INTERNAL_TRANSFER or "
             f"SPLIT_FULFILMENT first.", 409)
    internal_q = Decimal(str(decision[1]))
    if q > internal_q:
        _err("QUANTITY_EXCEEDS_DECISION",
             f"{_plain(q)} exceeds the line's internal quantity "
             f"{_plain(internal_q)}.", 422)
    live = session.fetchone(  # scope-exempt: the line's live request, under the request's lock
        "SELECT imr_number, status FROM internal_material_request WHERE "
        "pr_line_id = %s AND status NOT IN ('CONSUMED', 'RETURNED', 'CANCELLED')",
        (pr_line_id,))
    if live is not None:
        _err("IMR_ALREADY_LIVE",
             f"Line {line['line_no']} of {line['pr_number']} already has a live "
             f"request {live[0]} ({live[1]}).", 409)
    return _create_imr(
        session, line=line, quantity=q, actor=actor,
        item_external_id=item_external_id, item_description=item_description,
        from_location_id=from_location_id, to_location_id=to_location_id,
        unit_rate_paise=unit_rate_paise, valuation_note=valuation_note,
        reason=reason, correlation_id=correlation_id)


def approve_request(session: Session, *, imr_id: str, actor: str,
                    principal: Mapping[str, Any] | None = None,
                    acting_for_user_id: str | None = None,
                    approved_quantity: Any = None, reason: str | None = None,
                    expected_version: int | None = None,
                    correlation_id: str | None = None) -> dict[str, Any]:
    """Approve a REQUESTED request. Maker-checker, in `approve_pr`'s order:
    `auth.require`, `auth.require_separation`, then the delegation check
    against every contributor, then the requester re-read UNDER THE LOCK."""
    from .. import auth as auth_mod

    imr = _imr(session, imr_id)
    if imr["status"] != STATUS_REQUESTED:
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {imr['status']} and cannot be approved.", 409)
    if principal is not None:
        try:
            auth_mod.require(dict(principal), PERMISSION_APPROVE)
            auth_mod.require_separation(dict(principal), PERMISSION_APPROVE,
                                        imr["requested_by"],
                                        object_label=imr["imr_number"])
        except auth_mod.AuthError as exc:
            raise ProcurementError(exc.code, exc.message, status=exc.status) from exc
    try:
        assert_delegation_independent(
            actor, acting_for_user_id, {imr["requested_by"], imr["created_by"]})
    except DelegatedSelfApproval as exc:
        raise ProcurementError(
            ERR_SELF_APPROVAL, str(exc), status=403,
            detail={"actor_user_id": actor,
                    "acting_for_user_id": acting_for_user_id}) from exc

    lock_affected_cells(session, [(imr["wbs_id"], imr["budget_head_id"])])
    current = _lock_imr_row(session, imr_id)
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] != STATUS_REQUESTED:
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']} and cannot be approved.", 409)
    if actor == current["requested_by"]:
        _err(ERR_SELF_APPROVAL,
             f"{imr['imr_number']} was raised by {actor}; the requester never "
             f"approves their own request.", 403)

    mapping_ok = current["mapping_ok"]
    if not mapping_ok:
        # Re-probe: the mapping may have been created since the request was
        # raised. If so the exception is retracted; if not, still refused.
        if _owner_of(session, imr["wbs_id"], imr["budget_head_id"]) is not None:
            mapping_ok = True
            _retract(session, kind=KIND_INTERNAL_MAPPING_MISSING, imr_id=imr_id,
                     reason="mapping resolved to a budget-owning cell at approval",
                     actor=actor, correlation_id=correlation_id)
        else:
            _err(KIND_INTERNAL_MAPPING_MISSING,
                 f"{imr['imr_number']}: its project x WBS x budget-head mapping "
                 f"still resolves to no budget-owning cell; resolve the "
                 f"exception before approving.", 409)

    q = (imr["requested_quantity"] if approved_quantity is None
         else _quantity(approved_quantity, what="approved_quantity"))
    if q > imr["requested_quantity"]:
        _err("APPROVED_EXCEEDS_REQUESTED",
             f"approved_quantity {_plain(q)} exceeds the requested "
             f"{_plain(imr['requested_quantity'])}.", 422)
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call
        """
        UPDATE internal_material_request
        SET status = %(status)s, approved_quantity = %(qty)s, approved_by = %(actor)s,
            approved_at = now(), mapping_ok = %(mapping_ok)s,
            reason = COALESCE(%(reason)s, reason),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE imr_id = %(imr_id)s
        """,
        {"status": STATUS_IMR_APPROVED, "qty": q, "actor": actor,
         "mapping_ok": mapping_ok, "reason": reason, "imr_id": imr_id})
    audit_mod.append(
        session, actor, "IMR_APPROVED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} approved for {_plain(q)} of {_plain(imr['requested_quantity'])}"
        + (f" acting for {acting_for_user_id}" if acting_for_user_id else "")
        + (f"; reason: {reason}" if reason else ""),
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id))


def set_valuation(session: Session, *, imr_id: str, unit_rate_paise: int,
                  note: str, actor: str, reference: str | None = None,
                  expected_version: int | None = None,
                  correlation_id: str | None = None) -> dict[str, Any]:
    """A reasoned MANUAL valuation, before allocation freezes the rate."""
    imr = _imr(session, imr_id)
    if imr["status"] not in (STATUS_REQUESTED, STATUS_IMR_APPROVED):
        _err("VALUATION_FROZEN",
             f"{imr['imr_number']} is {imr['status']}; the rate was fixed at "
             f"allocation and the money booked at it.", 409)
    resolved = _resolve_valuation(None, None, unit_rate_paise=unit_rate_paise,
                                  valuation_note=note)
    current = _lock_imr_row(session, imr_id)
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call
        """
        UPDATE internal_material_request
        SET unit_rate_paise = %(rate)s, valuation_source = %(source)s,
            valuation_reference = %(reference)s, valuation_note = %(note)s,
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE imr_id = %(imr_id)s
        """,
        {"rate": resolved["unit_rate_paise"], "source": VALUATION_MANUAL,
         "reference": reference, "note": resolved["valuation_note"],
         "actor": actor, "imr_id": imr_id})
    _retract(session, kind=KIND_INTERNAL_VALUATION_MISSING, imr_id=imr_id,
             reason=f"manual valuation entered: {resolved['valuation_note']}",
             actor=actor, correlation_id=correlation_id)
    audit_mod.append(
        session, actor, "IMR_VALUATION_SET", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} valued at {unit_rate_paise} paise/unit (MANUAL"
        + (f", {reference}" if reference else "") + f"): {resolved['valuation_note']}",
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id))


def allocate(session: Session, *, imr_id: str, actor: str, idempotency_key: str,
             expected_version: int | None = None,
             correlation_id: str | None = None) -> dict[str, Any]:
    """Set the approved quantity aside for the project. The moment the money
    enters exposure -- through the hold it was already in, where it was."""
    key = _require_key(idempotency_key)
    imr = _imr(session, imr_id)
    if imr["status"] not in (STATUS_IMR_APPROVED, STATUS_ALLOCATED):
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {imr['status']}; only an APPROVED request "
             f"allocates.", 409)

    lock_affected_cells(session, [(imr["wbs_id"], imr["budget_head_id"])])
    current = _lock_imr_row(session, imr_id)
    replay = _existing_movement(session, imr_id, key)
    if replay is not None:
        return _public(_imr(session, imr_id), _money_of(session, imr_id))
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] != STATUS_IMR_APPROVED:
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']}; only an APPROVED "
             f"request allocates.", 409)
    if current["unit_rate_paise"] is None:
        _err(KIND_INTERNAL_VALUATION_MISSING,
             f"{imr['imr_number']} has no stock valuation; the allocation would "
             f"commit an unknown amount. Enter a reasoned valuation or resolve "
             f"the provider's answer first.", 409)
    q = current["approved_quantity"]
    amount = _money(q, current["unit_rate_paise"])

    # The hold moves before the verdict is asked, so the verdict sees the
    # cell as it will be: covered rupees are not both held and allocated.
    covered = 0
    owner = _owner_of(session, imr["wbs_id"], imr["budget_head_id"])
    if owner is None:
        _err(KIND_INTERNAL_MAPPING_MISSING,
             f"{imr['imr_number']}: no budget-owning cell behind the mapping.", 409)
    hold = _live_reservation(session, pr_id=imr["pr_id"], owner_wbs_id=owner,
                             budget_head_id=imr["budget_head_id"])
    if hold is not None:
        reservation_id, held, moved = hold
        covered = min(amount, held - moved)
        if covered > 0:
            _move_hold(session, reservation_id, covered, actor=actor)
            recompute_derived_position(session, owner, imr["budget_head_id"], actor=actor)
    uncovered = amount - covered
    verdicts: list[dict[str, Any]] = []
    if uncovered > 0:
        verdicts = budget_verdicts(session, {(imr["wbs_id"], imr["budget_head_id"]): uncovered})
        if exceeds_budget(verdicts):
            _err("BUDGET_EXCEEDED",
                 f"{imr['imr_number']}: allocating {amount} paise ({covered} "
                 f"covered by the request's hold, {uncovered} not) exceeds "
                 f"availability; nothing was allocated. "
                 + _shortfall_summary(verdicts), 409)

    movement_id = _movement(
        session, imr=imr, kind=KIND_ALLOCATE, quantity=q, amount_paise=amount,
        idempotency_key=key, actor=actor,
        from_location_id=current["from_location_id"], correlation_id=correlation_id)
    _update_quantities(session, imr_id, actor=actor, status=STATUS_ALLOCATED,
                       allocated_quantity=q)
    for wbs_id, head_id in _distinct([(imr["wbs_id"], imr["budget_head_id"]),
                                      (owner, imr["budget_head_id"])]):
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
    audit_mod.append(
        session, actor, "IMR_ALLOCATED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} allocated {_plain(q)} at "
        f"{current['unit_rate_paise']} paise/unit = {amount} paise "
        f"({covered} moved from the request's hold, {uncovered} newly exposed); "
        f"movement {movement_id}",
        correlation_id=correlation_id)
    return _public({**_imr(session, imr_id), "verdicts": verdicts,
                    "covered_by_hold_paise": covered,
                    "newly_exposed_paise": uncovered},
                   _money_of(session, imr_id))


def issue(session: Session, *, imr_id: str, quantity: Any, actor: str,
          idempotency_key: str, to_location_id: str | None = None,
          reference: str | None = None, note: str | None = None,
          expected_version: int | None = None,
          correlation_id: str | None = None) -> dict[str, Any]:
    """Stock leaves the store for the project: allocation becomes CWIP.
    Exposure does not change."""
    key = _require_key(idempotency_key)
    q = _quantity(quantity, what="quantity")
    imr = _imr(session, imr_id)
    _location_exists(session, to_location_id, what="to_location_id")
    lock_affected_cells(session, [(imr["wbs_id"], imr["budget_head_id"])])
    current = _lock_imr_row(session, imr_id)
    if _existing_movement(session, imr_id, key) is not None:
        return _public(_imr(session, imr_id), _money_of(session, imr_id))
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] not in (STATUS_ALLOCATED, STATUS_PARTIALLY_ISSUED,
                                 STATUS_PARTIALLY_CONSUMED):
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']}; stock is issued while "
             f"some of the allocation is still in the store.", 409)
    outstanding = current["allocated_quantity"] - current["issued_quantity"]
    if q > outstanding:
        _err("ISSUE_EXCEEDS_ALLOCATION",
             f"{_plain(q)} exceeds the {_plain(outstanding)} still allocated and "
             f"not issued on {imr['imr_number']}.", 422)
    money = _money_of(session, imr_id)
    issued_after = current["issued_quantity"] + q
    if issued_after == current["allocated_quantity"]:
        # Close-out is exact: the last issue carries whatever is left of the
        # allocation, so rounding never leaves a paisa behind.
        amount = money["allocated_paise"] - money["issued_paise"] - money["cancelled_paise"]
    else:
        amount = _money(q, current["unit_rate_paise"] or 0)
    amount = max(0, min(amount, money["internal_allocation_paise"]))
    movement_id = _movement(
        session, imr=imr, kind=KIND_ISSUE, quantity=q, amount_paise=amount,
        idempotency_key=key, actor=actor,
        from_location_id=current["from_location_id"],
        to_location_id=to_location_id or imr["to_location_id"],
        reference=reference, note=note, correlation_id=correlation_id)
    status = _status_from(current["allocated_quantity"], issued_after,
                          current["returned_quantity"], current["consumed_quantity"])
    _update_quantities(session, imr_id, actor=actor, status=status,
                       issued_quantity=issued_after)
    recompute_derived_position(session, imr["wbs_id"], imr["budget_head_id"], actor=actor)
    audit_mod.append(
        session, actor, "IMR_ISSUED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} issued {_plain(q)} ({amount} paise from allocation "
        f"to consumption); now {status}; movement {movement_id}",
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id), _money_of(session, imr_id))


def return_material(session: Session, *, imr_id: str, quantity: Any, actor: str,
                    idempotency_key: str, reference: str | None = None,
                    note: str | None = None, expected_version: int | None = None,
                    correlation_id: str | None = None) -> dict[str, Any]:
    """Issued, unconsumed stock goes back to the store. Consumption is
    released; the release goes back into the request's hold if it still
    has one."""
    key = _require_key(idempotency_key)
    q = _quantity(quantity, what="quantity")
    imr = _imr(session, imr_id)
    lock_affected_cells(session, [(imr["wbs_id"], imr["budget_head_id"])])
    current = _lock_imr_row(session, imr_id)
    if _existing_movement(session, imr_id, key) is not None:
        return _public(_imr(session, imr_id), _money_of(session, imr_id))
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] not in (STATUS_PARTIALLY_ISSUED, STATUS_ISSUED,
                                 STATUS_PARTIALLY_CONSUMED):
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']}; only issued stock "
             f"can be returned.", 409)
    on_site = (current["issued_quantity"] - current["returned_quantity"]
               - current["consumed_quantity"])
    if q > on_site:
        _err("RETURN_EXCEEDS_ISSUED",
             f"{_plain(q)} exceeds the {_plain(on_site)} issued and not yet "
             f"consumed or returned on {imr['imr_number']}.", 422)
    money = _money_of(session, imr_id)
    amount = min(_money(q, current["unit_rate_paise"] or 0),
                 money["internal_consumption_paise"])
    movement_id = _movement(
        session, imr=imr, kind=KIND_RETURN, quantity=q, amount_paise=amount,
        idempotency_key=key, actor=actor,
        to_location_id=current["from_location_id"], reference=reference,
        note=note, correlation_id=correlation_id)
    given_back, owner = _give_back(session, imr=imr, amount_paise=amount, actor=actor)
    returned_after = current["returned_quantity"] + q
    status = _status_from(current["allocated_quantity"], current["issued_quantity"],
                          returned_after, current["consumed_quantity"])
    _update_quantities(session, imr_id, actor=actor, status=status,
                       returned_quantity=returned_after)
    cells = [(imr["wbs_id"], imr["budget_head_id"])]
    if owner is not None:
        cells.append((owner, imr["budget_head_id"]))
    for wbs_id, head_id in _distinct(cells):
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
    audit_mod.append(
        session, actor, "IMR_RETURNED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} returned {_plain(q)} ({amount} paise released from "
        f"consumption, {given_back} back into the request's hold); now {status}; "
        f"movement {movement_id}",
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id), _money_of(session, imr_id))


def consume(session: Session, *, imr_id: str, quantity: Any, actor: str,
            idempotency_key: str, reference: str | None = None,
            note: str | None = None, expected_version: int | None = None,
            correlation_id: str | None = None) -> dict[str, Any]:
    """The site confirms use of issued stock. A status event: the money was
    booked as CWIP when the stock was issued, and moves nowhere here."""
    key = _require_key(idempotency_key)
    q = _quantity(quantity, what="quantity")
    imr = _imr(session, imr_id)
    current = _lock_imr_row(session, imr_id)
    if _existing_movement(session, imr_id, key) is not None:
        return _public(_imr(session, imr_id), _money_of(session, imr_id))
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] not in (STATUS_PARTIALLY_ISSUED, STATUS_ISSUED,
                                 STATUS_PARTIALLY_CONSUMED):
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']}; only issued stock "
             f"is consumed.", 409)
    on_site = (current["issued_quantity"] - current["returned_quantity"]
               - current["consumed_quantity"])
    if q > on_site:
        _err("CONSUME_EXCEEDS_ISSUED",
             f"{_plain(q)} exceeds the {_plain(on_site)} issued and not yet "
             f"consumed or returned on {imr['imr_number']}.", 422)
    movement_id = _movement(
        session, imr=imr, kind=KIND_CONSUME, quantity=q, amount_paise=0,
        idempotency_key=key, actor=actor, reference=reference, note=note,
        correlation_id=correlation_id)
    consumed_after = current["consumed_quantity"] + q
    status = _status_from(current["allocated_quantity"], current["issued_quantity"],
                          current["returned_quantity"], consumed_after)
    _update_quantities(session, imr_id, actor=actor, status=status,
                       consumed_quantity=consumed_after)
    audit_mod.append(
        session, actor, "IMR_CONSUMED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} consumed {_plain(q)} (no money moves: booked at "
        f"issue); now {status}; movement {movement_id}",
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id), _money_of(session, imr_id))


def transfer(session: Session, *, imr_id: str, quantity: Any, to_location_id: str,
             actor: str, idempotency_key: str, reference: str | None = None,
             note: str | None = None, expected_version: int | None = None,
             correlation_id: str | None = None) -> dict[str, Any]:
    """Allocated, unissued stock moves from one store to another. Not
    consumption: no money moves, the request's store changes."""
    key = _require_key(idempotency_key)
    q = _quantity(quantity, what="quantity")
    imr = _imr(session, imr_id)
    _location_exists(session, to_location_id, what="to_location_id")
    current = _lock_imr_row(session, imr_id)
    if _existing_movement(session, imr_id, key) is not None:
        return _public(_imr(session, imr_id), _money_of(session, imr_id))
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] not in (STATUS_ALLOCATED, STATUS_PARTIALLY_ISSUED,
                                 STATUS_PARTIALLY_CONSUMED):
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is {current['status']}; only allocated, "
             f"unissued stock transfers between stores.", 409)
    if current["from_location_id"] is None:
        _err("TRANSFER_SOURCE_UNKNOWN",
             f"{imr['imr_number']} names no source store to transfer from.", 409)
    if to_location_id == current["from_location_id"]:
        _err("TRANSFER_SAME_STORE",
             "A transfer names a different destination store.", 422)
    outstanding = current["allocated_quantity"] - current["issued_quantity"]
    if q > outstanding:
        _err("TRANSFER_EXCEEDS_ALLOCATION",
             f"{_plain(q)} exceeds the {_plain(outstanding)} allocated and "
             f"unissued on {imr['imr_number']}.", 422)
    movement_id = _movement(
        session, imr=imr, kind=KIND_TRANSFER, quantity=q, amount_paise=0,
        idempotency_key=key, actor=actor,
        from_location_id=current["from_location_id"], to_location_id=to_location_id,
        reference=reference, note=note, correlation_id=correlation_id)
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call
        "UPDATE internal_material_request SET from_location_id = %s, updated_at = now(), "
        "updated_by = %s, version_no = version_no + 1 WHERE imr_id = %s",
        (to_location_id, actor, imr_id))
    audit_mod.append(
        session, actor, "IMR_TRANSFERRED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']}: {_plain(q)} moved from store "
        f"{current['from_location_id']} to {to_location_id} (no money moves: a "
        f"transfer is not consumption); movement {movement_id}",
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id), _money_of(session, imr_id))


def cancel(session: Session, *, imr_id: str, reason: str, actor: str,
           expected_version: int | None = None,
           correlation_id: str | None = None) -> dict[str, Any]:
    """Cancel a request with no stock in the field. Whatever is still
    allocated is released, back into the request's hold if it still has one;
    its Open exceptions close."""
    if not (reason or "").strip():
        _err("REASON_REQUIRED", "A cancellation carries its reason.", 422)
    imr = _imr(session, imr_id)
    if imr["status"] in TERMINAL_STATUSES:
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is already {imr['status']}.", 409)
    lock_affected_cells(session, [(imr["wbs_id"], imr["budget_head_id"])])
    current = _lock_imr_row(session, imr_id)
    _assert_version(expected_version, current["version_no"], label=imr["imr_number"])
    if current["status"] in TERMINAL_STATUSES:
        _err("INVALID_TRANSITION",
             f"{imr['imr_number']} is already {current['status']}.", 409)
    on_site = (current["issued_quantity"] - current["returned_quantity"]
               - current["consumed_quantity"])
    if on_site > 0 or current["consumed_quantity"] > 0:
        _err("STOCK_IN_THE_FIELD",
             f"{imr['imr_number']} has {_plain(on_site)} issued and "
             f"{_plain(current['consumed_quantity'])} consumed; return the "
             f"unconsumed stock first. Consumed stock is CWIP and is not "
             f"cancelled.", 409)
    money = _money_of(session, imr_id)
    open_paise = money["internal_allocation_paise"]
    open_quantity = current["allocated_quantity"] - current["issued_quantity"]
    movement_id = None
    given_back = 0
    owner = None
    if open_quantity > 0:
        movement_id = _movement(
            session, imr=imr, kind=KIND_CANCEL, quantity=open_quantity,
            amount_paise=open_paise, idempotency_key=f"cancel:{imr_id}",
            actor=actor, note=reason, correlation_id=correlation_id)
        given_back, owner = _give_back(session, imr=imr, amount_paise=open_paise,
                                       actor=actor)
    session.execute(  # scope-exempt: keyed on a request already scope-gated in this call
        """
        UPDATE internal_material_request
        SET status = %(status)s, cancel_reason = %(reason)s, cancelled_at = now(),
            updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
        WHERE imr_id = %(imr_id)s
        """,
        {"status": STATUS_CANCELLED, "reason": reason.strip(), "actor": actor,
         "imr_id": imr_id})
    for kind in (KIND_INTERNAL_MAPPING_MISSING, KIND_INTERNAL_VALUATION_MISSING):
        _retract(session, kind=kind, imr_id=imr_id,
                 reason=f"request cancelled: {reason.strip()}", actor=actor,
                 correlation_id=correlation_id)
    cells = [(imr["wbs_id"], imr["budget_head_id"])]
    if owner is not None:
        cells.append((owner, imr["budget_head_id"]))
    for wbs_id, head_id in _distinct(cells):
        recompute_derived_position(session, wbs_id, head_id, actor=actor)
    audit_mod.append(
        session, actor, "IMR_CANCELLED", OBJECT_TYPE, imr_id,
        f"{imr['imr_number']} cancelled: {reason.strip()}; {open_paise} paise of "
        f"allocation released ({given_back} back into the request's hold)"
        + (f"; movement {movement_id}" if movement_id else ""),
        correlation_id=correlation_id)
    return _public(_imr(session, imr_id), _money_of(session, imr_id))


# ---------------------------------------------------------------------- reads
def get_request(session: Session, imr_id: str) -> dict[str, Any]:
    imr = _imr(session, imr_id)
    rows = repo.query(
        session,
        """
        SELECT m.movement_id, m.kind, m.quantity, m.amount_paise,
               m.from_location_id, m.to_location_id, m.idempotency_key,
               m.reference, m.note, m.occurred_at, m.created_by, m.correlation_id
        FROM internal_material_movement m
        JOIN project p ON p.project_id = m.project_id
        WHERE m.imr_id = %(imr_id)s AND {scope}
        ORDER BY m.occurred_at, m.movement_id
        """,
        {"imr_id": imr_id}, columns=_PROJECT_SCOPE_COLUMNS)
    movements = [{
        "movement_id": r[0], "kind": r[1], "quantity": _plain(Decimal(str(r[2]))),
        "amount_paise": int(r[3]), "from_location_id": r[4],
        "to_location_id": r[5], "idempotency_key": r[6], "reference": r[7],
        "note": r[8], "occurred_at": r[9].isoformat(), "created_by": r[10],
        "correlation_id": r[11],
    } for r in rows]
    exceptions = repo.query(
        session,
        """
        SELECT e.exception_id, e.kind, e.status, e.detail, e.raised_at
        FROM reconciliation_exception e
        JOIN project p ON p.project_id = e.project_id
        WHERE e.object_type = %(object_type)s AND e.object_id = %(imr_id)s AND {scope}
        ORDER BY e.raised_at
        """,
        {"object_type": EXCEPTION_OBJECT_TYPE, "imr_id": imr_id},
        columns=_PROJECT_SCOPE_COLUMNS)
    return _public({
        **imr, "movements": movements,
        "exceptions": [{"exception_id": e[0], "kind": e[1], "status": e[2],
                        "detail": e[3], "raised_at": e[4].isoformat()}
                       for e in exceptions],
    }, _money_of(session, imr_id))


def list_requests(session: Session, *, project_id: str | None = None,
                  pr_id: str | None = None, status: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        _err("STATUS_INVALID", f"status must be one of {', '.join(STATUSES)}.", 422)
    limit = max(1, min(int(limit), 1000))
    rows = repo.query(
        session,
        _IMR_SQL + """
        WHERE (%(project_id)s::text IS NULL OR imr.project_id = %(project_id)s)
          AND (%(pr_id)s::text IS NULL OR imr.pr_id = %(pr_id)s)
          AND (%(status)s::text IS NULL OR imr.status = %(status)s)
          AND {scope}
        ORDER BY imr.created_at DESC, imr.imr_number DESC
        LIMIT %(limit)s
        """,
        {"project_id": project_id, "pr_id": pr_id, "status": status, "limit": limit},
        columns=_PROJECT_SCOPE_COLUMNS)
    items = []
    for r in rows:
        imr = _imr_from_row(r)
        items.append(_public(imr, _money_of(session, imr["imr_id"])))
    return items
