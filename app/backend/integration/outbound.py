"""Outbound purchase-order emission, and the idempotency that makes it safe.

Wave 5, stream 6. Implements §11.6 (outbound idempotency), §11.7 (PR strategy
and the unsanctioned-commitment risk) and rows Z-01/Z-02 of the §18.5
responsibility matrix.

The one fact that shapes everything here
----------------------------------------
**Zoho documents no idempotency header.** There is no ``Idempotency-Key``, no
request id the vendor will deduplicate on, nothing. So the guarantee has to be
synthesised, and it is synthesised out of three parts that only work together:

1. a **deterministic** ``dedupe_key``, derived from identifiers we already own
   and written into the outbox row *before* any network call
   (:func:`derive_dedupe_key`, :meth:`OutboxStore.claim`);
2. that key written to a Zoho **unique** custom field, ``cf_capex_ref``
   (**Z-01**) -- which is why Z-01 is delivered by this project rather than
   delegated to the client: it is a correctness property, not a preference;
3. a retry that **resolves an existing record by that key and updates it**,
   rather than creating a second one (:class:`DuplicateDedupeKey` and the
   ``resolve_by_dedupe_key`` extension of the C1 adapter).

Remove any one of the three and a Function killed between "request sent" and
"response recorded" creates a duplicate purchase order.
``tests/test_outbound_chaos.py`` proves that by removing each one in turn: the
negative controls are as much the evidence as the positive one.

What this module does NOT assume
--------------------------------
* **It does not assume prevention exists.** §11.7's preventive control is Zoho
  role configuration (**D-8**), which is unresolved and may be unenforceable in
  the client's tenant. The detective control in
  :func:`detect_unsanctioned_commitments` therefore ships regardless, and
  :func:`record_unsanctioned_commitments` **refuses to run silently** when it
  cannot persist. A detective control that degrades to a no-op is worse than
  none, because it is believed.
* **It does not pick a side on D-7.** ``Capabilities.line_level_custom_fields``
  decides the emission shape (§18.5 Z-02). If it is ``False``, header-only
  fields force **one purchase order per control cell** -- a change to how
  procurement works, not an implementation detail -- so :func:`plan_emission`
  returns that consequence as a :class:`ProcessChange` the caller cannot
  discard by accident.
* **It does not talk to Zoho.** Every call goes through stream 1's C1 adapter.
  No base URL, scope string or endpoint appears here: the target product is
  PROVISIONAL (D-14).

Money is integer paise (``bigint``) throughout. A float, a ``Decimal`` or a
numeric string reaching a line amount is a defect, and :class:`PoLine` raises
rather than converting one.

Seams this module needs from other streams
------------------------------------------
The streams run in parallel, so this module is written against the frozen C1/C2
signatures and **duck-types** everything else. It imports nothing from
``adapter.py``, ``dto.py``, ``throttle.py`` or ``integration_store.py``: each is
described here as a ``Protocol`` and satisfied structurally. Two extensions to
C1 are needed and are **declared, not made** -- see :class:`ProcurementAdapter`.
C1 as frozen cannot express an idempotent retry on its own:
``create_purchase_order(po, dedupe_key)`` takes the key but has no documented
behaviour when the key already exists, and that gap is reported to stream 1
rather than patched into a file this stream does not own.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable)

from app.backend.money import MoneyError

# --------------------------------------------------------------------------
# Constants that are decisions, not defaults
# --------------------------------------------------------------------------

#: §11.6 allocation per organisation: 60 polling / 30 outbound / 10 interactive
#: out of the 100 requests-per-minute Zoho allows. Outbound gets 30 so that a
#: backfill cannot starve an operator clicking "Test connection", and so that a
#: burst of emissions cannot consume the polling budget the reconciliation
#: sweeps depend on.
OUTBOUND_MINUTE_ALLOCATION = 30

#: §11.6 / §18.5 Z-09. ERP Standard allows 2,000 calls per DAY. This is the
#: assumed ceiling until the client's plan tier is confirmed; the real value
#: comes from ``Capabilities.daily_call_ceiling``.
ASSUMED_DAILY_CALL_CEILING = 2000

#: The Zoho unique custom field that carries our dedupe key (§18.5 Z-01).
#: Single-line text, Mandatory off, **Unique ON**.
CF_CAPEX_REF = "cf_capex_ref"

#: Zoho single-line text fields are 255 characters. The key is kept well inside
#: that so a long local id cannot silently truncate -- a truncated key is a
#: COLLIDING key, which would suppress a legitimate second purchase order.
DEDUPE_KEY_MAX_LENGTH = 120

#: Purchase orders are emitted as ``draft`` and moved to ``open`` only by our
#: own call, after our approval instance closes (§11.7). These are our two
#: intended states; the raw Zoho status is stored verbatim elsewhere (C3).
PO_STATE_DRAFT = "draft"
PO_STATE_OPEN = "open"

#: Reconciliation exception kinds this module raises. ``UNSANCTIONED_COMMITMENT``
#: is named in §11.7 and blocks period close; the other two are the ways the
#: draft-to-open contract and the dedupe key can be violated.
KIND_UNSANCTIONED_COMMITMENT = "UNSANCTIONED_COMMITMENT"
KIND_UNSOLICITED_EXTERNAL_TRANSITION = "UNSOLICITED_EXTERNAL_TRANSITION"
KIND_DEDUPE_KEY_COLLISION = "DEDUPE_KEY_COLLISION"
KIND_ORPHANED_EMISSION = "ORPHANED_EMISSION"

#: The marker the Definition of Done requires on anything not yet confirmed in
#: the client's tenant. D-7 (line-level custom fields) is unresolved.
UNVERIFIED = "UNVERIFIED - REQUIRES ZOHO CONFIRMATION"

#: Outbox states. C2 freezes the columns, not the vocabulary; these are the
#: states this module drives.
OUTBOX_PENDING = "PENDING"
OUTBOX_SENDING = "SENDING"
OUTBOX_SENT = "SENT"
OUTBOX_RETRY = "RETRY"
OUTBOX_DEFERRED = "DEFERRED"
OUTBOX_DEAD = "DEAD"

#: The states a claim may move to SENDING from. ``SENDING`` is included on
#: purpose: a row left in SENDING is a Function that died mid-flight, and the
#: recovery path must be able to pick it up. It is the reason resolve-by-key
#: exists.
CLAIMABLE_STATES = (OUTBOX_PENDING, OUTBOX_RETRY, OUTBOX_SENDING, OUTBOX_DEFERRED)

#: How long a claim owns a row before another worker may take it (§2.2's job
#: contract has a **12-minute soft deadline**, so the lease must be longer than
#: that). A shorter lease is not a tuning choice, it is a duplicate: a second
#: worker would claim a row whose first worker is still mid-request, resolve
#: the dedupe key before that request lands, find nothing, and create a second
#: purchase order. Z-01's unique index is what stops that becoming two records
#: -- which is the clearest statement of why Z-01 cannot be replaced by a
#: read-before-write.
SENDING_LEASE_SECONDS = 900

#: §11.6: max_attempts=8, then DEAD and visible on SCR-39 with manual retry.
MAX_ATTEMPTS = 8

#: §11.6: ``next_attempt_at = now() + random(0, min(900s, 2s * 2^attempts))``
BACKOFF_BASE_SECONDS = 2
BACKOFF_CEILING_SECONDS = 900


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class OutboundError(RuntimeError):
    """Base for every refusal this module makes."""


class DuplicateDedupeKey(OutboundError):
    """The tenant already holds a record carrying this ``cf_capex_ref``.

    This is **the** success path of the idempotency design, dressed as an error
    because that is how Zoho reports a unique-field violation. The adapter
    raises it carrying ``external_id`` when it can read the colliding record's
    id back; :func:`emit_purchase_order` then adopts that id rather than
    creating a second purchase order.

    ``external_id`` may be ``None`` when the tenant refused the write without
    naming the existing record. That is recoverable -- the next attempt
    resolves the key explicitly -- but it is *not* treated as a failure to
    send, because the record demonstrably exists.
    """

    def __init__(self, dedupe_key: str, external_id: str | None = None,
                 message: str | None = None):
        self.dedupe_key = dedupe_key
        self.external_id = external_id
        super().__init__(message or (
            f"A record already carries {CF_CAPEX_REF}={dedupe_key!r}"
            + (f" (external_id={external_id!r})" if external_id
               else " and the tenant did not name it")))


class ApprovalNotClosed(OutboundError):
    """Refused a draft-to-open transition because our approval instance is open.

    §11.7: a purchase order becomes a commitment at ``open``. Moving it there
    before the approval closes is exactly the failure the PR-as-system-of-record
    strategy exists to prevent.
    """


class DetectiveControlUnavailable(OutboundError):
    """The unsanctioned-commitment control cannot persist its findings.

    Raised rather than returning quietly. §11.7 accepts that prevention (D-8)
    may be unenforceable *because* the detective control always runs; a
    detective control that no-ops when its table is missing would leave the
    number silently wrong, which is the one outcome §11.7 rules out.
    """


class BudgetExhausted(OutboundError):
    """No outbound rate budget remains in the binding window.

    Not a failure: §11.6 says the job checkpoints and returns, and the next
    cron tick resumes. Carries ``window`` so the caller can say which ceiling
    bound -- on ERP Standard it is nearly always ``DAY``.
    """

    def __init__(self, window: str, message: str | None = None):
        self.window = window
        super().__init__(
            message or f"Outbound rate budget exhausted in the {window} window")


class EmissionShapeError(OutboundError):
    """A draft's lines do not match the shape its capabilities permit."""


# --------------------------------------------------------------------------
# The dedupe key
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def derive_dedupe_key(connection_id: str, module: str, local_id: str) -> str:
    """The synthetic idempotency key. **Deterministic, and that is the point.**

    ``(connection_id, module, local_id)`` is exactly the C2 uniqueness of
    ``integration_outbox``, so one outbox row has one key for its whole life:
    across attempts, across processes, across a Function that died and a cron
    tick that picked the work back up ten minutes later.

    Generating this at send time -- a uuid4, a timestamp, an attempt counter --
    is the single change that turns a safe retry into a duplicate purchase
    order, because the second attempt then writes a ``cf_capex_ref`` the tenant
    has never seen and Z-01's unique index has nothing to refuse.
    ``tests/test_outbound_chaos.py`` holds that as an explicit negative control.

    The readable prefix is deliberate: an operator looking at a purchase order
    in Zoho can see which local record it came from without a lookup. The hash
    suffix carries ``connection_id`` and the untruncated identifiers, so two
    keys whose prefixes truncate to the same text still differ.
    """
    for name, value in (("connection_id", connection_id), ("module", module),
                        ("local_id", local_id)):
        if not isinstance(value, str) or not value.strip():
            raise OutboundError(
                f"derive_dedupe_key requires a non-empty {name}; got {value!r}. "
                "A key derived from a missing identifier is not deterministic.")

    canonical = "\x1f".join(
        (connection_id.strip(), module.strip(), local_id.strip()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    readable = _UNSAFE.sub("-", f"{module.strip()}-{local_id.strip()}").strip("-")
    budget = DEDUPE_KEY_MAX_LENGTH - len("CAPEX--") - len(digest)
    if len(readable) > budget:
        readable = readable[:budget].rstrip("-")

    key = f"CAPEX-{readable}-{digest}"
    if len(key) > DEDUPE_KEY_MAX_LENGTH:  # pragma: no cover - arithmetic guard
        raise OutboundError(
            f"dedupe key {key!r} exceeds {DEDUPE_KEY_MAX_LENGTH} characters; a "
            "truncated key collides and would suppress a real purchase order.")
    return key


# --------------------------------------------------------------------------
# Our DTOs. Never Zoho's shapes (C1).
# --------------------------------------------------------------------------

def _require_paise(value: Any, field_name: str) -> int:
    """Integer paise or nothing.

    Deliberately not ``money.to_paise``: this is not a user-supplied rupee
    amount being parsed, it is an internal figure that must already be paise.
    Accepting a float here -- even an exactly-representable one -- would let a
    float travel from a caller into a purchase order, and a purchase order is a
    commitment.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"{field_name} must be integer paise (bigint), not "
            f"{type(value).__name__} ({value!r}). Money never travels as a "
            "float, a Decimal or a string through the outbound path.")
    if value < 0:
        raise MoneyError(f"{field_name} must not be negative; got {value}.")
    return value


@dataclass(frozen=True)
class ControlCell:
    """``(wbs_id, budget_head_id)`` -- the grain ``po_line`` is keyed on.

    A multi-WBS purchase order is normal (§18.5 Z-02), which is why this is a
    value type rather than a pair of columns on the line: the emission shape is
    decided by counting *distinct cells*, and that count is the number the
    header-only path turns into purchase orders.
    """
    wbs_id: str
    budget_head_id: str

    def __post_init__(self) -> None:
        for name in ("wbs_id", "budget_head_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise EmissionShapeError(
                    f"ControlCell.{name} is required; got {value!r}. A line "
                    "with no control cell cannot be budget-checked, and an "
                    "unchecked commitment is the failure this product exists "
                    "to prevent.")

    @property
    def key(self) -> tuple[str, str]:
        return (self.wbs_id, self.budget_head_id)


@dataclass(frozen=True)
class PoLine:
    """One purchase-order line, carrying the control cell it commits against."""
    line_id: str
    cell: ControlCell
    description: str
    quantity: int
    unit_price_paise: int
    amount_paise: int

    def __post_init__(self) -> None:
        _require_paise(self.unit_price_paise, "PoLine.unit_price_paise")
        _require_paise(self.amount_paise, "PoLine.amount_paise")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise MoneyError(
                f"PoLine.quantity must be an integer; got {self.quantity!r}.")
        if self.quantity <= 0:
            raise EmissionShapeError(
                f"PoLine.quantity must be positive; got {self.quantity}.")


@dataclass(frozen=True)
class PurchaseOrderDraft:
    """One purchase order as we intend it, before the adapter shapes it.

    ``state`` is always ``draft`` at construction: §11.7 emits draft and moves
    to open only after our approval instance closes. The field exists so the
    invariant is visible in the object rather than implied by the call order.
    """
    local_id: str
    connection_id: str
    vendor_external_id: str
    lines: tuple[PoLine, ...]
    header_cell: ControlCell | None = None
    line_level_dimensions: bool = True
    state: str = PO_STATE_DRAFT
    currency: str = "INR"
    reference: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state != PO_STATE_DRAFT:
            raise EmissionShapeError(
                f"A purchase order is emitted as {PO_STATE_DRAFT!r}, never "
                f"{self.state!r}. The transition to {PO_STATE_OPEN!r} is a "
                "separate call made only after our approval instance closes "
                "(§11.7).")
        if not self.lines:
            raise EmissionShapeError(
                "A purchase order must carry at least one line.")
        if not self.line_level_dimensions:
            cells = {line.cell.key for line in self.lines}
            if len(cells) != 1:
                raise EmissionShapeError(
                    "Header-only dimensions (D-7 False) permit exactly one "
                    f"control cell per purchase order; this draft spans "
                    f"{len(cells)}. Use plan_emission(), which performs the "
                    "split and reports it as a process change.")
            if self.header_cell is None:
                raise EmissionShapeError(
                    "Header-only dimensions require header_cell to be set.")

    @property
    def total_paise(self) -> int:
        """Sum of the line amounts. Integer arithmetic end to end."""
        return sum(line.amount_paise for line in self.lines)

    @property
    def cells(self) -> tuple[tuple[str, str], ...]:
        seen: list[tuple[str, str]] = []
        for line in self.lines:
            if line.cell.key not in seen:
                seen.append(line.cell.key)
        return tuple(seen)

    def as_payload(self, dedupe_key: str) -> dict[str, Any]:
        """The product-agnostic payload handed to the adapter.

        ``cf_capex_ref`` is placed here, not in the adapter, because it is the
        idempotency key and this module owns idempotency. The adapter maps the
        rest onto whichever product D-14 resolves to.
        """
        header: dict[str, Any] = {
            "local_id": self.local_id,
            "vendor_external_id": self.vendor_external_id,
            "state": self.state,
            "currency": self.currency,
            "reference": self.reference,
            CF_CAPEX_REF: dedupe_key,
            "total_paise": self.total_paise,
        }
        if not self.line_level_dimensions and self.header_cell is not None:
            header["wbs_id"] = self.header_cell.wbs_id
            header["budget_head_id"] = self.header_cell.budget_head_id
        lines = []
        for line in self.lines:
            item: dict[str, Any] = {
                "line_id": line.line_id,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price_paise": line.unit_price_paise,
                "amount_paise": line.amount_paise,
            }
            if self.line_level_dimensions:
                item["wbs_id"] = line.cell.wbs_id
                item["budget_head_id"] = line.cell.budget_head_id
            lines.append(item)
        header["lines"] = lines
        return header


# --------------------------------------------------------------------------
# D-7: the emission shape, and the consequence of getting it as False
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessChange:
    """A consequence of a capability, stated so it cannot be absorbed silently.

    §18.5 Z-02: ``po_line`` is keyed on ``(wbs_id, budget_head_id)`` and a
    multi-WBS purchase order is normal. If D-7 resolves to "no line-level
    custom fields", the dimensions can only live on the header, and one
    requisition spanning three control cells becomes **three purchase orders
    sent to the vendor**. That is a change to how procurement works -- three
    documents to acknowledge, three to receive against, three to invoice -- and
    the client has to agree to it. It is not a technical detail, so it is not
    logged as one.
    """
    code: str
    summary: str
    detail: str
    purchase_orders: int
    control_cells: tuple[tuple[str, str], ...]
    depends_on: str = "D-7"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "summary": self.summary, "detail": self.detail,
            "purchase_orders": self.purchase_orders,
            "control_cells": [list(c) for c in self.control_cells],
            "depends_on": self.depends_on,
        }


@dataclass(frozen=True)
class EmissionPlan:
    """What :func:`plan_emission` decided, and why.

    ``process_change`` is populated **exactly when** the header-only path turned
    one requisition into more than one purchase order. A caller that ignores it
    is ignoring a procurement decision, which is why
    :meth:`assert_acknowledged` exists and why the API layer surfaces it rather
    than the plan's drafts alone.
    """
    drafts: tuple[PurchaseOrderDraft, ...]
    line_level_dimensions: bool
    control_cells: tuple[tuple[str, str], ...]
    process_change: ProcessChange | None
    notes: tuple[str, ...] = ()

    @property
    def is_split(self) -> bool:
        return len(self.drafts) > 1

    def assert_acknowledged(self, acknowledged: bool) -> None:
        """Refuse to proceed with an unacknowledged process change."""
        if self.process_change is not None and not acknowledged:
            raise EmissionShapeError(
                f"{self.process_change.summary} {self.process_change.detail} "
                "This is a procurement process change and must be acknowledged "
                "before emission, not discovered afterwards.")


def plan_emission(*, local_id: str, connection_id: str, vendor_external_id: str,
                  lines: Sequence[PoLine], capabilities: Any,
                  reference: str | None = None) -> EmissionPlan:
    """Decide the purchase-order shape from ``Capabilities`` -- never by default.

    Both paths are implemented, and which one runs is read from
    ``capabilities.line_level_custom_fields`` (**D-7**, unresolved, assumed
    ``False`` by C1's own comment):

    * ``True``  -- one purchase order, each line carrying its own
      ``(wbs_id, budget_head_id)``. Marked ``UNVERIFIED`` because D-7 has not
      been probed in the client's tenant (§18.5 Z-02's verification step).
    * ``False`` -- dimensions live on the header, so **one purchase order per
      control cell**. The plan carries a :class:`ProcessChange` saying so.

    Ordering is deterministic -- cells in first-appearance order, lines in input
    order -- because a plan that reorders under the same input makes the dedupe
    keys unstable, and an unstable dedupe key is a duplicate purchase order.
    """
    if not lines:
        raise EmissionShapeError("plan_emission requires at least one line.")

    line_level = bool(getattr(capabilities, "line_level_custom_fields", False))

    grouped: dict[tuple[str, str], list[PoLine]] = {}
    for line in lines:
        grouped.setdefault(line.cell.key, []).append(line)
    cells = tuple(grouped)

    if line_level:
        draft = PurchaseOrderDraft(
            local_id=local_id, connection_id=connection_id,
            vendor_external_id=vendor_external_id, lines=tuple(lines),
            header_cell=None, line_level_dimensions=True, reference=reference,
            provenance=(
                f"D-7 line-level custom fields assumed available: {UNVERIFIED}",),
        )
        return EmissionPlan(
            drafts=(draft,), line_level_dimensions=True, control_cells=cells,
            process_change=None,
            notes=(f"Line-level dimensions used for {len(cells)} control "
                   f"cell(s). {UNVERIFIED} (D-7, §18.5 Z-02).",),
        )

    drafts = tuple(
        PurchaseOrderDraft(
            # The suffix is part of the local identity, so the dedupe key of
            # each split purchase order differs. Without it the three POs of a
            # three-cell requisition would all claim one cf_capex_ref and two
            # would be refused as duplicates of the first.
            local_id=f"{local_id}#{cell[0]}#{cell[1]}",
            connection_id=connection_id, vendor_external_id=vendor_external_id,
            lines=tuple(cell_lines), header_cell=ControlCell(*cell),
            line_level_dimensions=False, reference=reference,
            provenance=(f"Header-only dimensions (D-7 False); split {local_id} "
                        f"on control cell {cell[0]}/{cell[1]}",),
        )
        for cell, cell_lines in grouped.items()
    )

    change = None
    if len(drafts) > 1:
        change = ProcessChange(
            code="HEADER_ONLY_PO_SPLIT",
            summary=f"One requisition became {len(drafts)} purchase orders.",
            detail=(
                "D-7 resolved False: the Zoho plan carries no line-level custom "
                "fields, so the WBS code and budget head can only sit on the "
                "purchase-order header. `po_line` is keyed on "
                f"(wbs_id, budget_head_id) and this requisition spans "
                f"{len(cells)} control cells, so it is emitted as {len(drafts)} "
                "separate purchase orders - separate documents for the vendor "
                "to acknowledge, receive against and invoice. This is a change "
                "to the procurement process and needs the client's agreement, "
                "not an engineering workaround (§18.5 Z-02)."),
            purchase_orders=len(drafts),
            control_cells=cells,
        )

    return EmissionPlan(
        drafts=drafts, line_level_dimensions=False, control_cells=cells,
        process_change=change,
        notes=(f"Header-only dimensions; {len(drafts)} purchase order(s) for "
               f"{len(cells)} control cell(s).",),
    )


# --------------------------------------------------------------------------
# Seams: what this module needs from streams 1, 2 and 4
# --------------------------------------------------------------------------

@runtime_checkable
class ProcurementAdapter(Protocol):
    """The slice of C1 this module uses, plus the extensions it needs.

    ``create_purchase_order`` and ``capabilities`` are frozen in C1 and taken
    verbatim. The other three are **declared here, not added to**
    ``adapter.py`` -- stream 1 owns that file and a needed change is reported,
    not made (WAVE5_CONTRACTS.md).

    * ``resolve_by_dedupe_key(key) -> str | None`` is the
      "update-by-custom-field-unique-value" half of §11.6. Without it, a retry
      after a lost response has no way to learn the id of the purchase order it
      may already have created, and the design degrades from "updates rather
      than duplicates" to "relies on the tenant to refuse".
      :func:`emit_purchase_order` works without it -- see
      :class:`DuplicateDedupeKey` -- but less well, and says so in its result.
    * ``update_purchase_order(external_id, payload, key)`` applies the current
      draft onto an adopted id.
    * ``transition_purchase_order(external_id, state, actor)`` performs the
      draft-to-open step of §11.7.

    All three are optional at runtime and probed with ``getattr``, so an
    adapter implementing only frozen C1 still works.
    """
    def create_purchase_order(self, po: Any, dedupe_key: str) -> str: ...
    def capabilities(self) -> Any: ...


@dataclass(frozen=True)
class BudgetDecision:
    granted: bool
    window: str = ""
    remaining: int | None = None
    reason: str = ""


class RateBudget(Protocol):
    """Stream 4's throttle, reduced to the one question this module asks.

    Deliberately narrow: ``reserve`` either grants ``calls`` requests or names
    the window that refused. §11.6 says over-budget is not a failure -- the job
    checkpoints and the next tick resumes -- so the refusal carries the window
    rather than raising inside the budget.
    """
    def reserve(self, calls: int) -> BudgetDecision: ...


@dataclass
class OutboxRecord:
    """One ``integration_outbox`` row, in the shape C2 freezes."""
    outbox_id: str
    connection_id: str
    module: str
    local_id: str
    dedupe_key: str
    payload: Mapping[str, Any]
    state: str = OUTBOX_PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    external_id: str | None = None
    last_error: str | None = None
    correlation_id: str | None = None


class OutboxStore(Protocol):
    """Stream 2's ``integration_store``, reduced to the outbound operations.

    Every method is a *durable* step. The ordering of these calls around the
    network call is the whole safety argument, so they are named for what they
    guarantee rather than for the SQL they run.

    ``claim`` carries the one requirement that is not obvious from its name: it
    must move the row to ``SENDING`` and take a **lease** of
    :data:`SENDING_LEASE_SECONDS`, atomically, returning ``None`` when another
    worker's lease is still live. A row in ``SENDING`` with an expired lease is
    claimable -- that is a Function that died mid-flight and must be recovered
    -- and a row in ``SENT`` never is.
    """
    def claim(self, outbox_id: str, *, now: datetime) -> OutboxRecord | None: ...

    def record_sent(self, outbox_id: str, external_id: str, *,
                    now: datetime) -> None: ...

    def record_attempt_failed(self, outbox_id: str, *, error: str,
                              next_attempt_at: datetime | None,
                              terminal: bool, now: datetime) -> None: ...

    def release(self, outbox_id: str, *, state: str, now: datetime) -> None: ...

    def get(self, outbox_id: str) -> OutboxRecord | None: ...


class ApprovalGate(Protocol):
    """Whether *our* approval instance for a local record has closed (§11.7)."""
    def is_closed(self, local_id: str) -> bool: ...


# --------------------------------------------------------------------------
# The rate budget, over the C2-frozen table
# --------------------------------------------------------------------------

def binding_window(capabilities: Any) -> str:
    """Which ceiling actually binds outbound emission.

    §11.6 states it and it is worth computing rather than asserting: 30 calls a
    minute is 43,200 a day, so against ERP Standard's 2,000/day the **daily**
    window binds first and by a factor of more than twenty. A throttle tracking
    only the per-minute allocation would let a backfill exhaust the tenant's
    whole day in just over an hour and then fail every call until midnight.
    """
    daily = int(getattr(capabilities, "daily_call_ceiling",
                        ASSUMED_DAILY_CALL_CEILING))
    return "DAY" if daily < OUTBOUND_MINUTE_ALLOCATION * 60 * 24 else "MINUTE"


@dataclass
class PgOutboundRateBudget:
    """``integration_rate_budget`` (C2), tracking both windows.

    The budget lives in PostgreSQL rather than in the process because no
    process is resident (§11.6): a Catalyst Function starts, does its chunk and
    dies, so an in-memory counter would reset on every invocation and the
    allocation would mean nothing.

    Written against C2's frozen column names. Stream 4 owns ``throttle.py``; if
    it exposes an equivalent this is the fallback rather than a competitor --
    the caller injects whichever it has, since both satisfy :class:`RateBudget`.
    """
    session: Any
    connection_id: str
    capabilities: Any
    minute_allocation: int = OUTBOUND_MINUTE_ALLOCATION

    _UPSERT = (
        "INSERT INTO integration_rate_budget "
        "  (connection_id, window_kind, window_start, used) "
        "VALUES (%(connection_id)s, %(window_kind)s, %(window_start)s, %(calls)s) "
        "ON CONFLICT (connection_id, window_kind, window_start) DO UPDATE "
        "  SET used = integration_rate_budget.used + EXCLUDED.used "
        "RETURNING used"
    )

    def ceiling(self, window: str) -> int:
        if window == "MINUTE":
            return self.minute_allocation
        return int(getattr(self.capabilities, "daily_call_ceiling",
                           ASSUMED_DAILY_CALL_CEILING))

    def reserve(self, calls: int, *, now: datetime | None = None) -> BudgetDecision:
        now = now or datetime.now(timezone.utc)
        starts = {
            "MINUTE": now.replace(second=0, microsecond=0),
            "DAY": now.replace(hour=0, minute=0, second=0, microsecond=0),
        }
        # DAY first: on ERP Standard it is the binding window, and consuming
        # minute budget for a call the daily ceiling will refuse wastes an
        # allocation the interactive path may need.
        for window in ("DAY", "MINUTE"):
            row = self.session.fetchone(self._UPSERT, {
                "connection_id": self.connection_id, "window_kind": window,
                "window_start": starts[window], "calls": calls,
            })
            used = int(row[0]) if row else calls
            limit = self.ceiling(window)
            if used > limit:
                return BudgetDecision(
                    granted=False, window=window,
                    remaining=max(0, limit - (used - calls)),
                    reason=(f"{window} outbound budget exhausted: {used}/{limit}. "
                            "The job checkpoints and the next tick resumes "
                            "(§11.6)."))
        return BudgetDecision(granted=True,
                              window=binding_window(self.capabilities))


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EmissionResult:
    outbox_id: str
    dedupe_key: str
    external_id: str
    created: bool
    adopted: bool
    attempts: int
    state: str = PO_STATE_DRAFT
    note: str = ""


def backoff_delay(attempts: int, *, rng: random.Random | None = None) -> timedelta:
    """§11.6: ``random(0, min(900s, 2s * 2^attempts))``, full jitter."""
    source = rng or random
    ceiling = min(BACKOFF_CEILING_SECONDS,
                  BACKOFF_BASE_SECONDS * (2 ** max(0, min(attempts, 32))))
    return timedelta(seconds=source.uniform(0, ceiling))


def emit_purchase_order(
    *, adapter: Any, store: OutboxStore, outbox_id: str,
    budget: RateBudget | None = None, now: datetime | None = None,
    rng: random.Random | None = None,
) -> EmissionResult:
    """Send one outbox row, at most once, whatever happens mid-flight.

    The ordering below is the design; each step sits where it does because of
    what a kill immediately after it must leave behind.

    1. **Reserve budget before claiming.** Over budget is not a failure and must
       not consume an attempt (§11.6). A kill here has changed nothing.
    2. **Claim the row** -- to ``SENDING``, attempts incremented, durably,
       *before the network call*. This is the write-ahead: a kill any time after
       it leaves a row saying "a send may be in flight", and the recovery path
       treats ``SENDING`` as *unknown*, never as "not sent".
    3. **Resolve by dedupe key, if the adapter can.** A row already in
       ``SENDING`` from a previous life is the lost-response case; asking the
       tenant what it holds for this ``cf_capex_ref`` converts the unknown into
       a fact before anything is created.
    4. **Create, carrying the key.** Z-01's unique index is what makes this
       safe: a second create with the same key cannot succeed.
    5. **Record the external id.** A kill between 4 and 5 is the worst case and
       is why 3 exists: the purchase order is out there, we do not know its id,
       and the next attempt finds it by key instead of making another.

    The key never changes between attempts (:func:`derive_dedupe_key`), which is
    what lets step 3 and step 4 talk about the same record.
    """
    stamp = now or datetime.now(timezone.utc)

    if budget is not None:
        decision = budget.reserve(1)
        if not decision.granted:
            store.release(outbox_id, state=OUTBOX_DEFERRED, now=stamp)
            raise BudgetExhausted(decision.window or "MINUTE", decision.reason)

    record = store.claim(outbox_id, now=stamp)
    if record is None:
        existing = store.get(outbox_id)
        if existing is not None and existing.state == OUTBOX_SENT:
            # Already done, by us or by a concurrent worker. Idempotent by
            # construction: report the settled result rather than re-sending.
            return EmissionResult(
                outbox_id=outbox_id, dedupe_key=existing.dedupe_key,
                external_id=existing.external_id or "", created=False,
                adopted=True, attempts=existing.attempts,
                note="Outbox row was already SENT; no call made.")
        raise OutboundError(
            f"Outbox row {outbox_id!r} is not claimable "
            f"(state={getattr(existing, 'state', None)!r}). Another worker "
            "holds it, or it is DEAD and awaiting manual retry on SCR-39.")

    expected = derive_dedupe_key(record.connection_id, record.module,
                                 record.local_id)
    if record.dedupe_key != expected:
        # The persisted key is authoritative -- it is what any already-created
        # purchase order carries -- but a mismatch means the derivation moved
        # under a live row, and that is the exact shape of a duplicate defect.
        store.record_attempt_failed(
            outbox_id,
            error=(f"{KIND_DEDUPE_KEY_COLLISION}: persisted key "
                   f"{record.dedupe_key!r} != derived {expected!r}"),
            next_attempt_at=None, terminal=True, now=stamp)
        raise OutboundError(
            f"{KIND_DEDUPE_KEY_COLLISION}: outbox row {outbox_id!r} carries "
            f"dedupe_key {record.dedupe_key!r} but the current derivation "
            f"produces {expected!r}. Sending would risk a duplicate purchase "
            "order; the row is quarantined for manual review instead.")

    key = record.dedupe_key
    payload = record.payload
    adopted_id: str | None = None
    note = ""

    resolve = getattr(adapter, "resolve_by_dedupe_key", None)
    if callable(resolve):
        # Step 3. Cheap when the row is fresh, decisive when it is a resumed
        # SENDING row. Costs one call against the same budget as the create,
        # which is the price of not guessing.
        adopted_id = resolve(key)
    elif record.attempts > 1:
        note = (
            "Adapter exposes no resolve_by_dedupe_key; a lost response is "
            "recoverable only through the tenant refusing the duplicate "
            f"({CF_CAPEX_REF} unique, Z-01). Safe, but it then depends "
            "entirely on Z-01 being configured correctly in this tenant.")

    try:
        if adopted_id:
            update = getattr(adapter, "update_purchase_order", None)
            if callable(update):
                update(adopted_id, payload, key)
            external_id, created, adopted = adopted_id, False, True
        else:
            external_id = adapter.create_purchase_order(payload, key)
            created, adopted = True, False
    except DuplicateDedupeKey as exc:
        # Z-01 did its job: the tenant refused a second record for this key.
        if exc.external_id:
            external_id, created, adopted = exc.external_id, False, True
        else:
            # The record exists but we cannot name it. Not a send failure --
            # recording it as one would let a later attempt create a second.
            #
            # An adapter with no `resolve_by_dedupe_key` can never learn the id,
            # so this is permanent rather than transient for that adapter, and
            # retrying it for ever would burn outbound budget against a tenant
            # whose daily ceiling may be 2,000 calls. After MAX_ATTEMPTS the row
            # goes DEAD -- with an explicit statement that the purchase order
            # EXISTS and is orphaned, because "dead" must not be read as "never
            # sent". :func:`orphaned_emission_finding` turns that row into an
            # exception that blocks period close.
            terminal = record.attempts >= MAX_ATTEMPTS
            store.record_attempt_failed(
                outbox_id,
                error=(f"Duplicate {CF_CAPEX_REF}={key!r} refused by the "
                       "tenant, which did not name the existing record. "
                       "Retrying to resolve it; NOT re-creating."
                       + ("" if not terminal else
                          f" ORPHANED after {record.attempts} attempts: a "
                          "purchase order carrying this key EXISTS in the "
                          "tenant and we cannot name it. This is not an "
                          "unsent row.")),
                next_attempt_at=(None if terminal
                                 else stamp + backoff_delay(record.attempts,
                                                            rng=rng)),
                terminal=terminal, now=stamp)
            raise
    except Exception as exc:  # noqa: BLE001 - classified by the caller's throttle
        terminal = record.attempts >= MAX_ATTEMPTS
        store.record_attempt_failed(
            outbox_id, error=f"{type(exc).__name__}: {exc}",
            next_attempt_at=(None if terminal
                             else stamp + backoff_delay(record.attempts, rng=rng)),
            terminal=terminal, now=stamp)
        raise

    store.record_sent(outbox_id, external_id, now=stamp)
    return EmissionResult(
        outbox_id=outbox_id, dedupe_key=key, external_id=external_id,
        created=created, adopted=adopted, attempts=record.attempts,
        state=PO_STATE_DRAFT, note=note)


@dataclass(frozen=True)
class ChunkOutcome:
    """What one drain of the outbox did, and where the next one resumes.

    ``checkpoint`` is the §2.2 job checkpoint: the ids this chunk did not reach.
    Empty means the chunk finished. ``stopped_on`` names why it stopped early,
    so SCR-39 can say "waiting on the daily quota" rather than "failed".
    """
    sent: tuple[EmissionResult, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    checkpoint: tuple[str, ...] = ()
    stopped_on: str = ""

    @property
    def exhausted_budget(self) -> bool:
        return self.stopped_on.startswith("BUDGET_")


def emit_chunk(*, adapter: Any, store: OutboxStore, outbox_ids: Sequence[str],
               budget: RateBudget | None = None, now: datetime | None = None,
               rng: random.Random | None = None) -> ChunkOutcome:
    """Drain part of the outbox. **Stream 5 owns the job; this is what it calls.**

    Three behaviours the job depends on, all of them §11.6:

    * **Over budget stops the chunk, it does not fail it.** The remaining ids
      come back as the checkpoint and the next cron tick resumes from there. On
      ERP Standard the daily ceiling will end most busy chunks this way, and
      that is the design working rather than an incident.
    * **One row's failure does not abandon the rest.** A 4xx business error on
      one purchase order is that row's problem; the other 29 in the chunk are
      still owed to the vendor.
    * **A kill mid-chunk is safe** for the same reason a kill mid-send is: every
      row already sent is ``SENT`` with its external id, and every row not
      reached is untouched. The chunk carries no state of its own that a
      restart could lose.
    """
    stamp = now or datetime.now(timezone.utc)
    sent: list[EmissionResult] = []
    failed: list[tuple[str, str]] = []
    remaining = list(outbox_ids)

    while remaining:
        outbox_id = remaining[0]
        try:
            sent.append(emit_purchase_order(
                adapter=adapter, store=store, outbox_id=outbox_id,
                budget=budget, now=stamp, rng=rng))
        except BudgetExhausted as exhausted:
            # Not a failure. The row was released back to DEFERRED by
            # emit_purchase_order and is still owed, so it stays in the
            # checkpoint rather than being consumed.
            return ChunkOutcome(sent=tuple(sent), failed=tuple(failed),
                                checkpoint=tuple(remaining),
                                stopped_on=f"BUDGET_{exhausted.window}")
        except Exception as exc:  # noqa: BLE001 - recorded per row, chunk continues
            failed.append((outbox_id, f"{type(exc).__name__}: {exc}"))
        remaining.pop(0)

    return ChunkOutcome(sent=tuple(sent), failed=tuple(failed))


def transition_to_open(*, adapter: Any, store: OutboxStore, outbox_id: str,
                       approval_gate: ApprovalGate, actor: str) -> str:
    """Move an emitted draft to ``open`` -- our call, after our approval closes.

    §11.7. The purchase order becomes a commitment at ``open``; everything
    before that is a document Zoho will happily hold and nobody will act on. So
    this refuses unless *our* approval instance has closed, and it refuses for a
    row that never reached ``SENT``, because there is nothing in the tenant to
    transition.
    """
    record = store.get(outbox_id)
    if record is None:
        raise OutboundError(f"No outbox row {outbox_id!r}.")
    if record.state != OUTBOX_SENT or not record.external_id:
        raise OutboundError(
            f"Outbox row {outbox_id!r} is {record.state!r} with external_id "
            f"{record.external_id!r}; there is no emitted draft to open.")

    if not approval_gate.is_closed(record.local_id):
        raise ApprovalNotClosed(
            f"Approval for {record.local_id!r} has not closed, so the purchase "
            f"order stays {PO_STATE_DRAFT!r}. Moving it to {PO_STATE_OPEN!r} "
            "would make it a commitment the budget check has not cleared "
            "(§11.7).")

    transition = getattr(adapter, "transition_purchase_order", None)
    if not callable(transition):
        raise OutboundError(
            "The adapter exposes no transition_purchase_order; the draft-to-open "
            "step of §11.7 cannot be performed. Reported to stream 1 rather "
            "than worked around here.")
    transition(record.external_id, PO_STATE_OPEN, actor=actor)
    return record.external_id


# --------------------------------------------------------------------------
# §11.7 -- the detective control. Ships regardless of D-8.
# --------------------------------------------------------------------------

#: Field names on an observed purchase order that mark it as carrying a CAPEX
#: dimension. Configurable because Z-02 may land as reporting tags rather than
#: custom fields, but never empty -- an empty set would make every purchase
#: order look innocent.
DEFAULT_CAPEX_DIMENSION_FIELDS = ("cf_wbs_code", "cf_budget_head",
                                  "wbs_id", "budget_head_id")


@dataclass(frozen=True)
class ObservedPurchaseOrder:
    """A purchase order as Sweep A found it in the tenant."""
    external_id: str
    dimensions: Mapping[str, Any] = field(default_factory=dict)
    capex_ref: str | None = None
    status_raw: str | None = None
    entity_id: str | None = None
    total_paise: int | None = None
    vendor_external_id: str | None = None
    created_by_raw: str | None = None


@dataclass(frozen=True)
class ReconciliationFinding:
    """One exception, in the shape ``reconciliation_exception`` stores.

    ``status`` is ``Open`` and ``entity_id`` is carried because those two
    columns are precisely what
    ``app.backend.pg.periods._has_open_reconciliation_exceptions`` reads to
    refuse a period close. The link between "we found an unsanctioned
    commitment" and "the period cannot close" is those two values and nothing
    else, which is why a test asserts the shape against ``periods.py`` rather
    than trusting this docstring.
    """
    kind: str
    object_type: str
    object_id: str
    detail: str
    entity_id: str | None
    local_paise: int | None = None
    source_paise: int | None = None
    status: str = "Open"
    screen: str = "SCR-25"
    blocks_period_close: bool = True

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "object_type": self.object_type,
            "object_id": self.object_id, "detail": self.detail,
            "entity_id": self.entity_id, "local_paise": self.local_paise,
            "source_paise": self.source_paise, "status": self.status,
        }


def carries_capex_dimension(
    po: ObservedPurchaseOrder, *,
    dimension_fields: Sequence[str] = DEFAULT_CAPEX_DIMENSION_FIELDS,
) -> bool:
    """True when the observed purchase order commits against a CAPEX cell.

    Blank strings and ``None`` do not count: Zoho returns empty custom fields on
    every record, so "the field is present" would flag the whole tenant and the
    exception queue would be noise within a day. Noise is how a detective
    control gets switched off.
    """
    if not dimension_fields:
        raise OutboundError(
            "carries_capex_dimension needs at least one dimension field. An "
            "empty set makes every purchase order look innocent, which is the "
            "silent failure §11.7 forbids.")
    for name in dimension_fields:
        value = po.dimensions.get(name)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def detect_unsanctioned_commitments(
    observed: Iterable[ObservedPurchaseOrder], *,
    known_external_ids: Iterable[str],
    dimension_fields: Sequence[str] = DEFAULT_CAPEX_DIMENSION_FIELDS,
) -> list[ReconciliationFinding]:
    """§11.7's detective control. **Assume prevention failed.**

    A purchase order created directly in Zoho bypasses ``budget_check``
    entirely. Prevention is Zoho role configuration (**D-8**), unresolved and
    possibly unenforceable in the client's tenant -- so this does not depend on
    it. Every purchase order Sweep A returns that carries a CAPEX dimension and
    has no matching local ``external_id`` becomes an
    ``UNSANCTIONED_COMMITMENT``, surfaces on SCR-25, and blocks period close.

    The point is not to stop the commitment; by the time we see it, it exists.
    The point is that the CAPEX number is never *silently* wrong. It is loudly
    wrong, and someone has to resolve the exception before the period closes.

    ``known_external_ids`` is materialised into a set once, deliberately: this
    runs over every open purchase order in the tenant on every sweep, and a
    per-item membership test against a generator is how a nightly job becomes a
    job that does not finish.
    """
    known = {e for e in known_external_ids if e}
    findings: list[ReconciliationFinding] = []
    for po in observed:
        if po.external_id in known:
            continue
        if not carries_capex_dimension(po, dimension_fields=dimension_fields):
            continue
        dims = ", ".join(
            f"{name}={po.dimensions[name]!r}" for name in dimension_fields
            if po.dimensions.get(name) not in (None, "")
        )
        findings.append(ReconciliationFinding(
            kind=KIND_UNSANCTIONED_COMMITMENT,
            object_type="purchase_order",
            object_id=po.external_id,
            entity_id=po.entity_id,
            source_paise=po.total_paise,
            local_paise=None,
            detail=(
                f"Purchase order {po.external_id} carries a CAPEX dimension "
                f"({dims}) but has no matching local record. It was created "
                "directly in Zoho and bypassed the budget check (§11.7). "
                "Committed value is present in the ERP and absent from the "
                "control hub until this is resolved. Blocks period close."),
        ))
    return findings


def detect_unsolicited_transitions(
    observed: Iterable[ObservedPurchaseOrder], *,
    intended_states: Mapping[str, str],
    status_map: Mapping[str, str] | None = None,
) -> list[ReconciliationFinding]:
    """A Zoho-side status change we did not initiate is an exception (§11.7).

    ``intended_states`` maps ``external_id`` to the state *we* last put the
    purchase order in -- ``draft`` at emission, ``open`` after
    :func:`transition_to_open`. Anything else observed is somebody moving our
    commitment behind us, and it is recorded, never absorbed as a state update.
    An unmapped raw status is not guessed at (C3): it is reported as a
    difference, because guessing is how a cancelled purchase order stays
    counted as committed.
    """
    findings: list[ReconciliationFinding] = []
    for po in observed:
        intended = intended_states.get(po.external_id)
        if intended is None or po.status_raw is None:
            continue
        mapped = (status_map or {}).get(po.status_raw, po.status_raw)
        if str(mapped).strip().lower() == intended.strip().lower():
            continue
        findings.append(ReconciliationFinding(
            kind=KIND_UNSOLICITED_EXTERNAL_TRANSITION,
            object_type="purchase_order",
            object_id=po.external_id,
            entity_id=po.entity_id,
            source_paise=po.total_paise,
            detail=(
                f"Purchase order {po.external_id} is {po.status_raw!r} in Zoho "
                f"but we last set it to {intended!r}. We did not initiate this "
                "transition. Purchase orders are emitted as draft and moved to "
                "open only by our own call after our approval instance closes; "
                "a change we did not make is an exception, not a state update "
                "(§11.7)."),
        ))
    return findings


_RECONCILIATION_EXCEPTION_TABLE = "reconciliation_exception"

# scope-exempt, deliberately, and stated here because `repo.query()` is the
# chokepoint for scoped READS and this is neither. The three statements in this
# module are: an INSERT of an exception whose `entity_id` comes from the tenant
# record the sweep just read (there is no local row to be scoped against -- the
# whole finding is that no local row exists); an upsert on
# `integration_rate_budget`, keyed on `connection_id`, which carries no
# row-level scope dimension; and an `information_schema` existence probe. None
# reads business rows on a caller-supplied identifier.
#
# REPORTED, not fixed: `tests/test_scope_enforcement.py` walks
# `app/backend/pg/` and `app/backend/api/` only, so `app/backend/integration/`
# is outside its gate entirely. That file belongs to no Wave 5 stream and a
# needed change is reported rather than made.
_INSERT_EXCEPTION = (
    "INSERT INTO reconciliation_exception "
    "  (exception_id, raised_at, object_type, object_id, kind, detail, "
    "   local_paise, source_paise, status, entity_id) "
    "VALUES (%(exception_id)s, %(raised_at)s, %(object_type)s, %(object_id)s, "
    "        %(kind)s, %(detail)s, %(local_paise)s, %(source_paise)s, "
    "        %(status)s, %(entity_id)s) "
    "ON CONFLICT (exception_id) DO NOTHING"
)


def _table_exists(session: Any, table_name: str) -> bool:
    row = session.fetchone(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table_name,))
    return row is not None


def exception_id_for(finding: ReconciliationFinding) -> str:
    """Deterministic id, so re-running the sweep does not re-raise the finding.

    The same reasoning as the dedupe key one layer up: a nightly sweep minting a
    fresh uuid per run would fill SCR-25 with one row per night for the same
    purchase order, and an operator would stop reading it.
    """
    canonical = "\x1f".join(
        (finding.kind, finding.object_type, finding.object_id))
    return "RX-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def record_unsanctioned_commitments(
    session: Any, findings: Sequence[ReconciliationFinding], *,
    now: datetime | None = None,
) -> int:
    """Persist findings so period close blocks. **Never degrades to a no-op.**

    If ``reconciliation_exception`` is absent, this raises
    :class:`DetectiveControlUnavailable` rather than returning ``0``.
    ``periods.py`` treats a missing table as "no open exceptions" -- correct for
    the period gate, catastrophic here: the sweep would report success, the
    exceptions would evaporate and the period would close over an unsanctioned
    commitment. §11.7's whole bargain is that the number is loudly wrong rather
    than silently wrong, and a silent write is a silent wrong number.
    """
    if not findings:
        return 0
    if not _table_exists(session, _RECONCILIATION_EXCEPTION_TABLE):
        raise DetectiveControlUnavailable(
            f"{_RECONCILIATION_EXCEPTION_TABLE} does not exist, so "
            f"{len(findings)} {KIND_UNSANCTIONED_COMMITMENT} finding(s) cannot "
            "be recorded and period close would not be blocked. Refusing to "
            "report success: §11.7 accepts that prevention (D-8) may fail only "
            "because detection always runs.")

    stamp = now or datetime.now(timezone.utc)
    written = 0
    for finding in findings:
        row = finding.as_row()
        row["exception_id"] = exception_id_for(finding)
        row["raised_at"] = stamp
        session.execute(_INSERT_EXCEPTION, row)
        written += 1
    return written


def orphaned_emission_finding(record: OutboxRecord, *, entity_id: str | None
                              ) -> ReconciliationFinding | None:
    """A DEAD outbox row whose purchase order demonstrably reached the tenant.

    The one case where "we failed to send" and "we sent and lost it" look the
    same on SCR-39 and are opposite facts. It arises when the adapter
    implements only frozen C1 -- no ``resolve_by_dedupe_key`` -- and the tenant
    refuses the duplicate without naming the record it already holds. The
    purchase order exists, we cannot link it, and reading the row as an unsent
    one would understate committed value by its whole amount.

    So it is not left as a retry queue entry for an operator to interpret. It
    becomes an exception that **blocks period close**, on the same reasoning as
    the unsanctioned commitment: a commitment we cannot account for is a number
    that is loudly wrong rather than silently wrong.
    """
    if record.state != OUTBOX_DEAD or record.external_id:
        return None
    if "ORPHANED" not in (record.last_error or ""):
        return None
    return ReconciliationFinding(
        kind=KIND_ORPHANED_EMISSION,
        object_type="integration_outbox",
        object_id=record.outbox_id,
        entity_id=entity_id,
        detail=(
            f"Outbox row {record.outbox_id} for {record.local_id} is DEAD with "
            f"no external_id, but the tenant refused its {CF_CAPEX_REF}="
            f"{record.dedupe_key!r} as a duplicate -- so a purchase order "
            "carrying that key EXISTS and we cannot name it. Committed value "
            "is present in the ERP and unlinked here. Resolve by locating the "
            "purchase order in the tenant by its custom field and recording "
            "its id, or by cancelling it. Blocks period close."),
    )


def period_close_blockers(
    findings: Sequence[ReconciliationFinding],
) -> list[ReconciliationFinding]:
    """The subset that stops a period closing -- Open, and flagged as blocking."""
    return [f for f in findings if f.blocks_period_close and f.status == "Open"]
