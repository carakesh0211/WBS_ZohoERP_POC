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
This module was written while the streams ran in parallel, so it duck-typed
every seam and imported nothing from ``dto.py`` or ``integration_store.py``:
each was described here as a ``Protocol`` and satisfied structurally. That was
right while the other files were moving. It is no longer, and the cost of
keeping it was three defects that a single import would have made impossible:

* the outbox state vocabulary drifted to six values, three of which the table's
  CHECK rejects, because nothing ever compared the two lists;
* a second implementation of the rate-budget reservation grew here, against
  columns migration 010 does not declare and a conflict target that is not the
  primary key, so it could never have executed;
* the payload was handed to the adapter as a raw ``Mapping`` while both shipped
  adapters read it by attribute, so **the first emission raised
  ``AttributeError``** -- there was no shape in between and nothing typed
  strongly enough to notice.

So the seams that have landed are now imported and checked: ``dto`` for the
emission shape and ``integration_store`` for the reservation and the state
vocabulary (asserted at import, below). The **adapter** stays duck-typed, and
deliberately: ``ProcurementAdapter`` declares the three C1 extensions this
module needs, and an adapter implementing only frozen C1 must still work --
:func:`emit_purchase_order` probes all three with ``getattr`` and says in its
result when it is running without them.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import (Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable)

from app.backend.money import MoneyError

from ..pg import integration_store as store
from .dto import EmissionRef, LineDTO, PurchaseOrderEmissionDTO, freeze

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

#: Stamped into every ``integration_outbox.payload`` this module writes, and
#: required by :func:`emission_dto`.
#:
#: An outbox row can outlive the code that wrote it by days -- it is enqueued
#: in the business transaction and drained by a later cron tick -- so the
#: mapper will one day be asked to read a payload written by a previous shape.
#: Refusing an unrecognised version is the difference between that row landing
#: on SCR-39 as a defect and being mapped on a guess, and the fields it would
#: be guessing about are monetary.
PAYLOAD_VERSION = 1

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

#: Outbox states. **The C16 `outbox` namespace, and nothing else.**
#:
#: These were once six: ``SENDING``, ``RETRY`` and ``DEFERRED`` alongside the
#: four below. All three were invented here. The table's CHECK
#: (``ck_integration_outbox_state``), ``integration_store.OUTBOX_STATES`` and
#: ``research/30_contracts/C16_integration_statuses.json`` agree on exactly
#: four, so every write this module made of the other three would have been
#: refused by PostgreSQL. ``FAILED`` -- the one that does exist -- was unused.
#:
#: ``RETRY`` and ``DEFERRED`` were straightforward synonyms and are gone.
#: ``SENDING`` was the argument, and it is answered in :func:`emit_purchase_order`
#: and in :data:`CLAIMABLE_STATES` below.
OUTBOX_PENDING = "PENDING"
OUTBOX_SENT = "SENT"
OUTBOX_FAILED = "FAILED"
OUTBOX_DEAD = "DEAD"

#: The canonical four, in C16's order, asserted against the store at import.
OUTBOX_STATES = (OUTBOX_PENDING, OUTBOX_SENT, OUTBOX_FAILED, OUTBOX_DEAD)

# The assertion is at import, not in a test, deliberately. A test proves the
# two lists agreed on the day it ran; this refuses to load a module whose
# vocabulary has drifted from the schema's. Drift is exactly how three states
# that PostgreSQL rejects survived a whole wave -- every double in the test
# suite agreed with the module rather than with the table.
if tuple(store.OUTBOX_STATES) != OUTBOX_STATES:  # pragma: no cover - guard
    raise ImportError(
        "outbound.py's outbox vocabulary "
        f"{OUTBOX_STATES!r} has drifted from integration_store.OUTBOX_STATES "
        f"{tuple(store.OUTBOX_STATES)!r}, which mirrors "
        "ck_integration_outbox_state. A state this module writes and the "
        "table refuses is a row that never persists.")

#: The states a row may be claimed from -- the same two
#: ``integration_store.claim_outbox_batch`` selects, and for the same reason.
#:
#: There is no in-flight state here, because there is no in-flight STATE.
#: Exclusion is `SELECT ... FOR UPDATE SKIP LOCKED`, held by the claiming
#: transaction: a second worker skips a locked row, and a Function that dies
#: mid-request drops its lock with its connection, so the row is immediately
#: re-claimable with no lease to expire and no 15-minute dead zone. That is
#: what the previous lease-plus-``SENDING`` design was reimplementing in a
#: column, less well.
#:
#: What ``SENDING`` was really carrying was "a despatch may already have
#: happened, so do not create blindly". That fact is not needed, because
#: :func:`emit_purchase_order` resolves by dedupe key before EVERY create,
#: never only after a suspicious state -- see its step 3.
CLAIMABLE_STATES = (OUTBOX_PENDING, OUTBOX_FAILED)

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
    """One purchase-order line, carrying the control cell it commits against.

    ``unit_price_paise`` and ``amount_paise`` are integer MINOR UNITS OF THE
    DRAFT'S CURRENCY -- paise for an INR order, cents for USD, yen (exponent
    0) for JPY, fils (exponent 3) for KWD. The names keep the ``_paise``
    suffix because every DTO on both sides of the boundary uses it for "minor
    units of ``currency_code``" (``dto.paise(..., minor_exponent=)`` on the
    inbound side); the adapters render them at the exponent
    ``money.minor_exponent_of(currency_code)`` names, never at a fixed two.
    A foreign-currency order therefore reaches the vendor priced in the
    currency they quoted, at the figures on the order, not at INR paise the
    header's rate was divided back out of.
    """
    line_id: str
    cell: ControlCell
    description: str
    quantity: int
    unit_price_paise: int
    amount_paise: int
    #: An EXISTING tenant item this line is for, named by the operator at
    #: emission (`_EmitIn.item_external_ids`), or None for a description-only
    #: line. Never derived: the local line carries no item and inventing one
    #: would commit against a master record nobody chose.
    item_external_id: str | None = None
    #: The WBS element's own `wbs_code` and the budget head's name, resolved
    #: by the plan so the tenant's line custom fields carry the values the
    #: adoption path reads back. None when the plan had none to give.
    wbs_code: str | None = None
    budget_head: str | None = None

    def __post_init__(self) -> None:
        for name in ("item_external_id", "wbs_code", "budget_head"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise EmissionShapeError(
                    f"PoLine.{name} must be a non-empty string or None; got "
                    f"{value!r}.")
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
    #: The document date the vendor and the ledger will both see. **Required,
    #: and frozen at enqueue rather than read from the clock at send time.**
    #:
    #: This field did not exist, and its absence was load-bearing. Both shipped
    #: adapters send ``po.document_date.isoformat()``, so with no date on the
    #: draft the payload could not carry one and the only way to satisfy the
    #: adapter would have been ``date.today()`` inside the emission. A retry is
    #: not same-day: a row that fails at 23:58 and succeeds on the next tick
    #: would carry a different document date from the one the approval cleared,
    #: and an adopted-then-updated purchase order would have its date REWRITTEN
    #: by the recovery. The commitment would move period without anyone
    #: choosing that. So the date is an input, like the amount.
    document_date: date
    header_cell: ControlCell | None = None
    line_level_dimensions: bool = True
    state: str = PO_STATE_DRAFT
    #: Named ``currency_code`` to match :class:`~app.backend.integration.dto.
    #: PurchaseOrderEmissionDTO` exactly. It was ``currency`` here and
    #: ``currency_code`` there; a rename across a mapping boundary is a field
    #: that gets dropped, and a purchase order emitted with no currency is one
    #: the tenant fills in from its own default.
    currency_code: str = "INR"
    reference: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.document_date, datetime) or not isinstance(
                self.document_date, date):
            raise EmissionShapeError(
                "PurchaseOrderDraft.document_date must be a datetime.date, not "
                f"{type(self.document_date).__name__} "
                f"({self.document_date!r}). datetime subclasses date, so this "
                "check is on the order of the two isinstance calls; a datetime "
                "would put a timestamp in a Zoho DATE field.")
        if self.state != PO_STATE_DRAFT:
            raise EmissionShapeError(
                f"A purchase order is emitted as {PO_STATE_DRAFT!r}, never "
                f"{self.state!r}. The transition to {PO_STATE_OPEN!r} is a "
                "separate call made only after our approval instance closes "
                "(§11.7).")
        if not self.lines:
            raise EmissionShapeError(
                "A purchase order must carry at least one line.")
        # The same rule the DTO enforces, applied at construction so a draft
        # that could never become a DTO is refused before an outbox row exists.
        if (not isinstance(self.currency_code, str)
                or not re.fullmatch(r"[A-Z]{3}", self.currency_code)):
            raise EmissionShapeError(
                "PurchaseOrderDraft.currency_code must be a three-letter "
                f"ISO-4217 code in upper case; got {self.currency_code!r}. "
                "A purchase order emitted with no usable currency is one the "
                "tenant fills in from its own default.")
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
        """The JSON stored in ``integration_outbox.payload`` (C2, ``jsonb``).

        **This is one of the two halves of the mapping and it is now closed.**
        The other is :func:`emission_dto_from_record`, and
        :meth:`as_emission_dto` is the composition of the two --
        ``draft.as_emission_dto(k)`` and the DTO rebuilt from
        ``draft.as_payload(k)`` are asserted equal, so a field added to one
        half and forgotten in the other fails a test rather than silently
        dropping off a purchase order.

        Three shapes exist and each earns its place: :class:`PurchaseOrderDraft`
        is what a caller assembles (control cells, integer paise, our
        vocabulary); this ``dict`` is what survives a process death (C2 froze
        ``payload jsonb``, and JSON has no dates and no tuples); and
        :class:`~app.backend.integration.dto.PurchaseOrderEmissionDTO` is what
        the adapter reads by attribute. Passing this dict straight to the
        adapter -- which is what happened -- raises ``AttributeError`` on
        ``po.vendor_external_id``.

        ``cf_capex_ref`` is placed here, not in the adapter, because it is the
        idempotency key and this module owns idempotency.
        """
        header: dict[str, Any] = {
            # Stamped so a payload written by an older shape is refused loudly
            # by `emission_dto_from_record` rather than mapped on a guess.
            "payload_version": PAYLOAD_VERSION,
            "local_id": self.local_id,
            "connection_id": self.connection_id,
            "vendor_external_id": self.vendor_external_id,
            "state": self.state,
            # ISO-8601 date, never a timestamp: `date.isoformat()` on a
            # `datetime.date` is `YYYY-MM-DD` and `__post_init__` has already
            # refused a `datetime`.
            "document_date": self.document_date.isoformat(),
            "currency_code": self.currency_code,
            "reference": self.reference,
            CF_CAPEX_REF: dedupe_key,
            "line_level_dimensions": self.line_level_dimensions,
            "subtotal_paise": self.total_paise,
            "tax_paise": 0,
            "total_paise": self.total_paise,
        }
        if not self.line_level_dimensions and self.header_cell is not None:
            header["wbs_id"] = self.header_cell.wbs_id
            header["budget_head_id"] = self.header_cell.budget_head_id
        lines = []
        for number, line in enumerate(self.lines, start=1):
            item: dict[str, Any] = {
                "line_number": number,
                "line_id": line.line_id,
                "description": line.description,
                # Integer here, exact decimal STRING in the DTO. `PoLine`
                # counts whole units; `LineDTO.quantity` is a string because a
                # quantity is decimal but is not money and must never acquire a
                # float. The conversion is `str(int)`, which is exact.
                "quantity": line.quantity,
                "unit_price_paise": line.unit_price_paise,
                "amount_paise": line.amount_paise,
            }
            if self.line_level_dimensions:
                item["wbs_id"] = line.cell.wbs_id
                item["budget_head_id"] = line.cell.budget_head_id
            # Optional, and absent rather than null when unknown, so a payload
            # written before these existed reads back unchanged.
            if line.item_external_id is not None:
                item["item_external_id"] = line.item_external_id
            if line.wbs_code is not None:
                item["wbs_code"] = line.wbs_code
            if line.budget_head is not None:
                item["budget_head"] = line.budget_head
            lines.append(item)
        header["lines"] = lines
        return header

    def as_emission_dto(self, dedupe_key: str, *, module: str
                        ) -> PurchaseOrderEmissionDTO:
        """This draft as the DTO the adapter consumes, without going via JSON.

        The forward half of the mapping. It exists so the round trip is
        testable: a draft mapped straight to a DTO must equal the same draft
        mapped to a payload, stored, read back and mapped to a DTO. If those
        two ever differ, a field is being lost in ``payload jsonb`` -- and the
        fields at risk are monetary.
        """
        return emission_dto(
            payload=self.as_payload(dedupe_key),
            connection_id=self.connection_id, module=module,
            local_id=self.local_id, dedupe_key=dedupe_key)

# --------------------------------------------------------------------------
# The mapping. `payload jsonb` -> the DTO the adapter reads by attribute.
# --------------------------------------------------------------------------
# THIS IS THE HALF THAT WAS MISSING, and its absence was not a retry defect:
# `emit_purchase_order` passed `record.payload` -- a `Mapping[str, Any]` -- to
# `adapter.create_purchase_order`, and both shipped adapters build their wire
# body from `po.vendor_external_id`, `po.document_date.isoformat()`,
# `po.currency_code` and `po.lines`. A dict raises `AttributeError` on the
# first of those. THE FIRST EMISSION COULD NEVER HAVE SUCCEEDED. The chaos
# test did not catch it because its `FakeAdapter` accepts a Mapping -- it does
# `dict(payload)` -- so every double in the suite agreed with the caller
# instead of with the two adapters that actually ship.
#
# A previous stream declined to invent this mapping and bridged its test with a
# labelled `_PayloadShim` instead. That was the right call at the time: getting
# `PoLine.amount_paise` -> `LineDTO` wrong is not a crash, it is a purchase
# order committing the wrong number, and a wrong number that reconciles against
# nothing is worse than an AttributeError. The mapping is defined here now,
# explicitly, in both directions, with every monetary and identifier field
# validated at the boundary rather than trusted.


def _payload_str(payload: Mapping[str, Any], key: str, *, where: str,
                 allow_none: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EmissionShapeError(
            f"{where}: payload[{key!r}] must be a non-empty string; got "
            f"{value!r}. An emission assembled from a payload missing an "
            "identifier cannot be reconciled back to what it commits against.")
    return value


def _payload_paise(payload: Mapping[str, Any], key: str, *, where: str) -> int:
    """A monetary field out of `payload jsonb`, as integer paise.

    JSON has no integer/float distinction that survives every parser, so this
    refuses a float rather than truncating one. `json.loads` turns `25000000.0`
    into a Python float, and a float that reaches a purchase order line is the
    defect AUD-H-007 was about.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"{where}: payload[{key!r}] must be integer paise (bigint), not "
            f"{type(value).__name__} ({value!r}). Money never travels as a "
            "float, a Decimal or a string through the outbound path.")
    if value < 0:
        raise MoneyError(f"{where}: payload[{key!r}] must not be negative; "
                         f"got {value}.")
    return value


def _payload_document_date(payload: Mapping[str, Any], *, where: str) -> date:
    """``document_date`` back out of JSON, as a ``date`` and never a timestamp.

    ``date.fromisoformat`` on Python 3.11+ happily parses a full timestamp and
    returns... a ``date``, silently discarding the time. That is the wrong kind
    of tolerance here: a payload carrying a timestamp was written by something
    that did not know this field is a date, and mapping it anyway hides that.
    """
    raw = payload.get("document_date")
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise EmissionShapeError(
            f"{where}: payload['document_date'] must be an ISO date "
            f"(YYYY-MM-DD); got {raw!r}. The adapter sends this into a Zoho "
            "DATE field, and a purchase order whose date is rejected or "
            "truncated is a commitment landing in a period nobody chose.")
    return date.fromisoformat(raw)


def _emission_line(item: Any, *, number: int, where: str) -> LineDTO:
    """One payload line as a :class:`LineDTO`, field by named field.

    The money mapping, stated once so it can be checked:

    * ``unit_price_paise`` -> ``LineDTO.unit_price_paise``  (paise, unchanged)
    * ``amount_paise``     -> ``LineDTO.line_total_paise``  (paise, unchanged)
    * ``quantity`` (int)   -> ``LineDTO.quantity`` (exact decimal STRING)

    The second of those is the one worth staring at. ``PoLine.amount_paise`` is
    the LINE TOTAL, not a unit rate, and ``LineDTO`` has a field for each; a
    mapping that put the amount in ``unit_price_paise`` would multiply the
    commitment by the quantity, silently, on a document a vendor then invoices
    against. It is checked twice over -- here by name, and by
    ``PurchaseOrderEmissionDTO.__post_init__`` re-summing the lines against the
    header total.

    ``external_line_id`` is ``None``, deliberately. That field is AUD-H-004's
    ZOHO line id, and Zoho has not minted one for a line it has not yet seen.
    Putting our local ``line_id`` there would make an unresolved linkage look
    resolved, which is precisely the ``GRN_LINE_UNATTRIBUTED`` failure §11.8
    exists to keep visible. The local id is preserved in ``raw`` instead, where
    it is traceable without being mistaken for an external handle.
    """
    if not isinstance(item, Mapping):
        raise EmissionShapeError(
            f"{where}: line {number} must be a JSON object; got "
            f"{type(item).__name__}.")

    stated = item.get("line_number")
    if stated is not None and stated != number:
        raise EmissionShapeError(
            f"{where}: line {number} carries line_number {stated!r}. The "
            "payload's line order and its line numbers disagree, so a "
            "reconciliation query naming a line number would resolve to a "
            "different line from the one the operator is looking at.")

    quantity = item.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise MoneyError(
            f"{where}: line {number} quantity must be an integer; got "
            f"{type(quantity).__name__} ({quantity!r}).")
    if quantity <= 0:
        raise EmissionShapeError(
            f"{where}: line {number} quantity must be positive; got "
            f"{quantity}.")

    line_where = f"{where} line {number}"
    unit = _payload_paise(item, "unit_price_paise", where=line_where)
    total = _payload_paise(item, "amount_paise", where=line_where)

    dimensions: dict[str, Any] = {}
    for name in ("wbs_id", "budget_head_id", "wbs_code", "budget_head"):
        if item.get(name) is not None:
            dimensions[name] = item[name]
    item_external_id = _payload_str(item, "item_external_id", where=line_where,
                                    allow_none=True)

    return LineDTO(
        external_line_id=None,
        line_number=number,
        description=_payload_str(item, "description", where=line_where),
        # `str(int)` is exact. A quantity is decimal but is not money, so it
        # gets neither `to_paise` nor a float (dto.py's second rule).
        quantity=str(quantity),
        unit_price_paise=unit,
        line_total_paise=total,
        # Our drafts carry no per-line tax: the CAPEX commitment is the line
        # amount, and tax on a purchase order is the vendor's to state on the
        # bill. Zero rather than absent, so the header identity
        # `subtotal + tax == total` has a real number on both sides.
        tax_paise=0,
        item_external_id=item_external_id,
        purchase_order_line_external_id=None,
        dimensions=freeze(dimensions),
        raw=freeze(dict(item)),
    )


def emission_dto(*, payload: Mapping[str, Any], connection_id: str,
                 module: str, local_id: str, dedupe_key: str
                 ) -> PurchaseOrderEmissionDTO:
    """A stored payload as the DTO both shipped adapters actually read.

    The four identifiers are passed separately and **cross-checked** against
    what the payload claims, rather than being read out of it. They are the
    outbox row's own columns, and the row is authoritative: a payload whose
    ``local_id`` or ``cf_capex_ref`` disagrees with the row carrying it is a
    payload written for a different document, and emitting it would attach one
    requisition's amount to another requisition's dedupe key -- a duplicate and
    a mis-link in one call. That cannot be repaired by preferring either side,
    so it is refused.
    """
    where = f"outbox payload for {local_id!r}"

    version = payload.get("payload_version")
    if version != PAYLOAD_VERSION:
        raise EmissionShapeError(
            f"{where}: payload_version is {version!r}, expected "
            f"{PAYLOAD_VERSION}. This row was written by a different shape of "
            "this module; mapping it would be a guess about monetary fields.")

    for name, expected in (("local_id", local_id),
                           ("connection_id", connection_id),
                           (CF_CAPEX_REF, dedupe_key)):
        found = payload.get(name)
        if found != expected:
            raise EmissionShapeError(
                f"{where}: payload[{name!r}] is {found!r} but the outbox row "
                f"says {expected!r}. The row is authoritative -- it is what "
                "any already-created purchase order was keyed on -- so a "
                "payload that disagrees with it is refused rather than "
                "reconciled to either side.")

    state = payload.get("state")
    if state != PO_STATE_DRAFT:
        raise EmissionShapeError(
            f"{where}: payload['state'] is {state!r}. A purchase order is "
            f"emitted as {PO_STATE_DRAFT!r}; the move to {PO_STATE_OPEN!r} is "
            "a separate call made only after our approval instance closes "
            "(§11.7).")

    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)) \
            or not raw_lines:
        raise EmissionShapeError(
            f"{where}: payload['lines'] must be a non-empty list; got "
            f"{raw_lines!r}.")

    lines = tuple(
        _emission_line(item, number=number, where=where)
        for number, item in enumerate(raw_lines, start=1))

    dimensions: dict[str, Any] = {}
    for name in ("wbs_id", "budget_head_id"):
        if payload.get(name) is not None:
            dimensions[name] = payload[name]

    return PurchaseOrderEmissionDTO(
        origin=EmissionRef(connection_id=connection_id, module=module,
                           local_id=local_id, dedupe_key=dedupe_key),
        vendor_external_id=_payload_str(payload, "vendor_external_id",
                                        where=where),
        document_date=_payload_document_date(payload, where=where),
        currency_code=_payload_str(payload, "currency_code", where=where),
        lines=lines,
        subtotal_paise=_payload_paise(payload, "subtotal_paise", where=where),
        tax_paise=_payload_paise(payload, "tax_paise", where=where),
        total_paise=_payload_paise(payload, "total_paise", where=where),
        reference=_payload_str(payload, "reference", where=where,
                               allow_none=True),
        dimensions=freeze(dimensions),
    )


def emission_dto_from_record(record: OutboxRecord) -> PurchaseOrderEmissionDTO:
    """:func:`emission_dto`, with the identifiers taken from the outbox row.

    This is what :func:`emit_purchase_order` calls, and it is the ONLY thing
    that may be handed to an adapter. Nothing in this module passes a
    ``Mapping`` to ``create_purchase_order`` or ``update_purchase_order`` any
    more, and ``tests/test_outbound_chaos.py`` fails if anything starts to.
    """
    return emission_dto(
        payload=record.payload, connection_id=record.connection_id,
        module=record.module, local_id=record.local_id,
        dedupe_key=record.dedupe_key)



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


def cell_local_id(local_id: str, wbs_id: str, budget_head_id: str) -> str:
    """The outbox `local_id` of the one-purchase-order-per-cell shape (D-7
    False): the ORDER's id, then the cell, `#`-joined. `purchase_order_id_of`
    is its inverse, and the two live together so neither can drift."""
    return f"{local_id}#{wbs_id}#{budget_head_id}"


def purchase_order_id_of(local_id: str) -> str:
    """The local purchase order an outbox row belongs to: the row's own
    `local_id` on the line-level shape, the part before the first `#` on the
    per-cell shape (`cell_local_id`). Purchase-order ids carry no `#`."""
    return str(local_id).split("#", 1)[0]


def plan_emission(*, local_id: str, connection_id: str, vendor_external_id: str,
                  lines: Sequence[PoLine], capabilities: Any,
                  document_date: date,
                  reference: str | None = None,
                  currency_code: str = "INR") -> EmissionPlan:
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

    ``currency_code`` is the purchase order's OWN currency and is stamped on
    every draft the plan produces -- one, or one per control cell -- so the
    split cannot leave a foreign-currency order's second document labelled
    with the default. The line amounts are already in that currency's minor
    units (see :class:`PoLine`); nothing here converts.
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
            document_date=document_date,
            header_cell=None, line_level_dimensions=True, reference=reference,
            currency_code=currency_code,
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
            local_id=cell_local_id(local_id, cell[0], cell[1]),
            connection_id=connection_id, vendor_external_id=vendor_external_id,
            lines=tuple(cell_lines), document_date=document_date,
            header_cell=ControlCell(*cell),
            line_level_dimensions=False, reference=reference,
            currency_code=currency_code,
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

    ``now`` is part of the seam rather than left to the implementation's own
    clock. The binding window is the DAY, so which instant a reservation is
    charged at decides which ceiling row it lands on, and an emission whose
    budget disagrees with it about the date is charging a day it is not in.
    """
    def reserve(self, calls: int, *,
                now: datetime | None = None) -> BudgetDecision: ...


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
    #: C2's own column, defaulted to §11.6's eight. Carried here rather than
    #: assumed from :data:`MAX_ATTEMPTS` because
    #: ``ck_integration_outbox_dead_exhausted_attempts`` compares DEAD against
    #: THIS row's ceiling, and a row whose ceiling was raised by an operator
    #: would otherwise be declared dead by a constant that no longer applies.
    max_attempts: int = MAX_ATTEMPTS
    next_attempt_at: datetime | None = None
    external_id: str | None = None
    #: Moves with ``state == SENT`` and only with it
    #: (``ck_integration_outbox_sent_at``, a biconditional both ways).
    sent_at: datetime | None = None
    last_error: str | None = None
    correlation_id: str | None = None


class OutboxStore(Protocol):
    """Stream 2's ``integration_store``, reduced to the outbound operations.

    Every method is a *durable* step. The ordering of these calls around the
    network call is the whole safety argument, so they are named for what they
    guarantee rather than for the SQL they run.

    ``claim`` carries the one requirement that is not obvious from its name:
    while the claim is held, no other worker may obtain the same row.
    ``integration_store.claim_outbox_batch`` gets that from
    ``SELECT ... FOR UPDATE OF o SKIP LOCKED`` -- the lock lives in the
    claiming transaction, so a concurrent worker skips the row and a Function
    that dies releases it the instant its connection drops. **It does not
    change the row's state and it does not take a lease**, and neither does
    this protocol require it to.

    That is a change. ``claim`` used to promise a move to ``SENDING`` plus a
    900-second lease, and the two together were an in-Python reimplementation
    of a lock the database already provides -- with a failure mode the database
    version does not have: a Function killed while holding a lease leaves the
    row unclaimable for the rest of the lease, which on a one-minute cron is
    fifteen wasted ticks. What ``SENDING`` bought over ``SKIP LOCKED`` was the
    record that a despatch *might* have happened, and
    :func:`emit_purchase_order` no longer needs that record because it resolves
    by dedupe key before every create rather than only after a suspicious
    state.

    A row in ``SENT`` is never claimable. ``CLAIMABLE_STATES`` is the whole
    rule, and it is the same two states the store's own claim selects.

    ``release`` is gone. Its only caller pushed a row it had never claimed into
    a ``DEFERRED`` state the table's CHECK rejects; an unclaimed row needs no
    release, and a claimed one is released by its transaction ending.
    """
    def claim(self, outbox_id: str, *, now: datetime) -> OutboxRecord | None: ...

    def record_sent(self, outbox_id: str, external_id: str, *,
                    now: datetime) -> None: ...

    def record_attempt_failed(self, outbox_id: str, *, error: str,
                              next_attempt_at: datetime | None,
                              terminal: bool, now: datetime) -> None: ...

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
    """The OUTBOUND allocation, delegated to ``integration_store.reserve_calls``.

    **This class used to carry its own SQL, and that SQL could not execute.**
    It is worth stating exactly how, because the failure was invisible to every
    test in the repository:

    * it conflicted ``ON CONFLICT (connection_id, window_kind, window_start)``.
      No such constraint exists. The primary key of ``integration_rate_budget``
      is ``(connection_id, window_kind, allocation, window_start_key)`` -- note
      ``allocation``, which the statement never mentioned at all, so two
      allocations would have fought over one row even if it had parsed;
    * its INSERT supplied four columns. Five more --
      ``window_start_key``, ``window_seconds``, ``window_tz``, ``ceiling`` and
      ``allocation`` -- are NOT NULL with no default;
    * and the strategy was unfixable rather than merely wrong. It added
      ``calls`` first and asked afterwards whether the total had passed the
      ceiling, which is precisely what
      ``ck_integration_rate_budget_used_within_ceiling`` (``used <= ceiling``)
      forbids. The row can never HOLD the over-reserved value long enough to be
      read back and refused. Patching the column list would have left a
      statement that still aborts the transaction on the first refusal.

    So it is deleted, not repaired. ``throttle.py`` went through this same
    consolidation and now contains no SQL at all; this module now contains none
    either, for the rate budget. ``integration_store.reserve_calls`` was
    written by the author of migration 010, names every NOT NULL column,
    conflicts on the real key, and settles the race in the one place it can be
    settled -- inside the UPDATE's own WHERE clause, so an over-budget call is
    refused by the statement that would have spent it. There is no resident
    process (§11.6), so two cron invocations reserving at the same instant have
    nowhere else to arbitrate.

    ``capabilities`` is kept for :func:`binding_window`'s advisory answer only.
    **It is no longer consulted for a ceiling.** The ceiling lives in
    ``integration_rate_budget.ceiling``, seeded from the connection; a second
    opinion here was the other half of the same defect, and a throttle that
    disagrees with its own table is a throttle that reports a budget nobody is
    enforcing.
    """
    session: Any
    connection_id: str
    capabilities: Any = None
    scope: Any = None
    tz: str = "UTC"

    #: §11.6's three-way split. Ours is OUTBOUND, and it is a constant rather
    #: than a parameter because a caller that could choose would be able to
    #: spend the polling budget the reconciliation sweeps depend on.
    ALLOCATION = "OUTBOUND"

    def reserve(self, calls: int, *, now: datetime | None = None
                ) -> BudgetDecision:
        """Reserve ``calls`` requests, or say which window refused.

        §11.6 is explicit that over-budget is a normal outcome a job
        checkpoints on, not an exception -- so the store's
        ``RateBudgetExhausted`` is caught and turned into a falsy
        :class:`BudgetDecision`, exactly as ``throttle.reserve`` does. The two
        ``except`` arms are ordered: ``RateBudgetExhausted`` subclasses
        ``IntegrationStoreError``, so the specific one must come first or the
        general one swallows every refusal.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise OutboundError(
                "PgOutboundRateBudget.reserve needs a timezone-aware `now`; "
                f"got {moment!r}. A naive timestamp lands in whichever window "
                "the server's local time implies, and the DAY window is the "
                "binding one.")

        try:
            granted = store.reserve_calls(
                self.session, connection_id=self.connection_id,
                allocation=self.ALLOCATION, count=calls, tz=self.tz,
                now=moment, scope=self.scope)
        except store.RateBudgetExhausted as refused:
            return self._refused(refused.window_kind, calls, moment)

        # The honest answer to "how many more may I make" is the SMALLEST
        # remaining across the windows, not the one `binding_window` predicts
        # from the plan tier. On ERP Standard those agree; on a tenant whose
        # ceiling has been raised they do not, and reporting the prediction
        # would let a chunk keep going into a window that is already spent.
        least = min(granted.items(), key=lambda item: item[1]["remaining"])
        return BudgetDecision(granted=True, window=least[0],
                              remaining=least[1]["remaining"])

    def _refused(self, window: str, calls: int, moment: datetime
                 ) -> BudgetDecision:
        """The falsy decision, carrying what the window holds AFTER the rollback.

        The re-read runs outside the store's savepoint, on a transaction the
        refusal left healthy, so it sees the figures as they stand once nothing
        was spent. Reading them from before the rollback would report the
        over-reserved count -- the very number the constraint exists to prevent
        anyone believing.
        """
        state = store.read_rate_budget(
            self.session, connection_id=self.connection_id,
            allocation=self.ALLOCATION, tz=self.tz, now=moment,
            scope=self.scope)

        if window not in state:
            # `read_rate_budget` OMITS a window whose row does not exist, and
            # "no row" is not "a row reading zero". A missing row means the
            # window was never seeded for this connection, which is a
            # configuration fault, not an exhausted budget -- so `remaining`
            # stays None rather than becoming a 0 that reads as "spent".
            return BudgetDecision(
                granted=False, window=window, remaining=None,
                reason=(f"The {window} {self.ALLOCATION} budget refused "
                        f"{calls} call(s) and no {window} row exists for "
                        f"connection {self.connection_id}. That is a missing "
                        "window, not a spent one; the figure is unknown rather "
                        "than zero."))

        used = int(state[window]["used"])
        ceiling = int(state[window]["ceiling"])
        return BudgetDecision(
            granted=False, window=window, remaining=max(0, ceiling - used),
            reason=(f"{window} {self.ALLOCATION} budget exhausted: "
                    f"{used}/{ceiling} used, {calls} more requested. The job "
                    "checkpoints and the next tick resumes (§11.6)."))


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
       not consume an attempt (§11.6). A kill here has changed nothing, and --
       corrected -- neither has a refusal: the row is left exactly as it was
       found, ``PENDING`` or ``FAILED``, still due, still owed. It used to be
       pushed to a ``DEFERRED`` state that did not exist, by a ``release()``
       call on a row this function had not yet claimed.
    2. **Claim the row.** Exclusive for the duration of the claiming
       transaction (``SELECT ... FOR UPDATE SKIP LOCKED``, as
       ``integration_store.claim_outbox_batch`` does it). A second worker skips
       it; a Function that dies drops the lock with its connection and the row
       is immediately re-claimable, with no lease to wait out.
    3. **Resolve by dedupe key, if the adapter can. ALWAYS, not conditionally.**
       This is the step that makes step 2 sufficient. Asking the tenant what it
       holds for this ``cf_capex_ref`` converts "may already have been sent"
       into a fact *before* anything is created -- and because it is asked
       every time, no state anywhere has to remember that a despatch might have
       happened. That is why there is no ``SENDING``: it was carrying a fact
       this step re-derives from the tenant, which is the only place the fact
       actually lives.
    4. **Create, carrying the key.** Z-01's unique index is what makes this
       safe: a second create with the same key cannot succeed.
    5. **Record the external id.** A kill between 4 and 5 is the worst case and
       is why 3 exists: the purchase order is out there, we do not know its id,
       and the next attempt finds it by key instead of making another.

    The key never changes between attempts (:func:`derive_dedupe_key`), which is
    what lets step 3 and step 4 talk about the same record.

    Between 2 and 3 -- before the resolve, and so before any branch that could
    reach an adapter -- the stored payload becomes a
    :class:`~app.backend.integration.dto.PurchaseOrderEmissionDTO`
    (:func:`emission_dto_from_record`). Early on purpose: a malformed payload
    is then refused without spending a call. Nothing here hands a ``Mapping``
    to an adapter, because both shipped adapters read the document by
    attribute, so a dict raised ``AttributeError`` on the *first* emission,
    before any question of a retry arose.
    """
    stamp = now or datetime.now(timezone.utc)

    if budget is not None:
        # The SAME clock the rest of the emission uses. The budget's binding
        # window is the DAY (§11.6), so a budget left to read its own wall
        # clock could charge a reservation to a different day from the one this
        # emission believes it is in -- a real difference for a chunk running
        # across midnight, and an untraceable one.
        decision = budget.reserve(1, now=stamp)
        if not decision.granted:
            # Nothing to release. The row was never claimed, so it is still in
            # whichever claimable state it was already in and still due. §11.6:
            # the job checkpoints and the next tick resumes.
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

    # The payload becomes a DTO HERE -- before the resolve, and before any
    # branch that could reach an adapter. Mapping it early means a malformed
    # payload is refused without spending a call, and it means there is exactly
    # one object in this function that an adapter may be given. `document`,
    # not `payload`: the name is part of the fix, because `payload` is what the
    # dict was called when it was being handed to `create_purchase_order`.
    #
    # A mapping failure is a defect in the row, not a transient fault, so it
    # is recorded as a failed attempt and re-raised rather than retried
    # silently -- eight identical AttributeErrors would otherwise burn eight of
    # a 2,000-call daily ceiling to reach the same conclusion.
    try:
        document = emission_dto_from_record(record)
    except Exception as exc:  # noqa: BLE001 - re-raised after recording
        store.record_attempt_failed(
            outbox_id,
            error=(f"OUTBOX_PAYLOAD_UNMAPPABLE: {type(exc).__name__}: {exc}"),
            next_attempt_at=None, terminal=True, now=stamp)
        raise

    adopted_id: str | None = None
    note = ""

    resolve = getattr(adapter, "resolve_by_dedupe_key", None)
    if callable(resolve):
        # Step 3, and it runs UNCONDITIONALLY -- on the first attempt as much
        # as on the ninth. Making it conditional on some "might be in flight"
        # signal is what made an in-flight state look necessary; asking every
        # time costs one call against the same budget as the create, and buys
        # the removal of a whole state from the lifecycle.
        adopted_id = resolve(key)
    # `>= 1`, not `> 1`. The note belongs on a RETRY -- the point at which a
    # missing resolve starts to cost something -- and `attempts` now counts
    # failures rather than claims, so a first attempt reads 0 where it used to
    # read 1. The old `> 1` under the new accounting would have skipped the
    # note on the second attempt, which is exactly the one it exists for.
    elif record.attempts >= 1:
        note = (
            "Adapter exposes no resolve_by_dedupe_key; a lost response is "
            "recoverable only through the tenant refusing the duplicate "
            f"({CF_CAPEX_REF} unique, Z-01). Safe, but it then depends "
            "entirely on Z-01 being configured correctly in this tenant.")

    try:
        if adopted_id:
            update = getattr(adapter, "update_purchase_order", None)
            if callable(update):
                update(adopted_id, document, key)
            external_id, created, adopted = adopted_id, False, True
        else:
            external_id = adapter.create_purchase_order(document, key)
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
            # `record.attempts` is the count BEFORE this failure, and the claim
            # no longer increments it -- `integration_store.mark_outbox_failed`
            # does, in the same statement that decides FAILED-or-DEAD, so that a
            # Function killed between the increment and the decision cannot
            # leave a row retrying for ever one attempt short of DEAD. So the
            # eighth failure is the one where `attempts + 1` reaches the
            # ceiling, and that is exactly how the store computes it.
            #
            # `record.max_attempts`, not the module constant:
            # `ck_integration_outbox_dead_exhausted_attempts` compares DEAD
            # against THIS row's ceiling, and an operator who raised it on one
            # row must not have that row declared dead by a global.
            terminal = record.attempts + 1 >= record.max_attempts
            store.record_attempt_failed(
                outbox_id,
                error=(f"Duplicate {CF_CAPEX_REF}={key!r} refused by the "
                       "tenant, which did not name the existing record. "
                       "Retrying to resolve it; NOT re-creating."
                       + ("" if not terminal else
                          f" ORPHANED after {record.attempts + 1} attempts: a "
                          "purchase order carrying this key EXISTS in the "
                          "tenant and we cannot name it. This is not an "
                          "unsent row.")),
                next_attempt_at=(None if terminal
                                 else stamp + backoff_delay(record.attempts,
                                                            rng=rng)),
                terminal=terminal, now=stamp)
            raise
    except Exception as exc:  # noqa: BLE001 - classified by the caller's throttle
        terminal = record.attempts + 1 >= record.max_attempts
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
            # Not a failure. `emit_purchase_order` refused before claiming, so
            # the row was never touched: it is still PENDING or FAILED, still
            # due, and still owed. It therefore stays in the checkpoint rather
            # than being consumed, and the next tick resumes from here (§11.6).
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
# chokepoint for scoped READS and this is neither. There are now TWO statements
# left in this module -- it was three: an INSERT of an exception whose
# `entity_id` comes from the tenant record the sweep just read (there is no
# local row to be scoped against -- the whole finding is that no local row
# exists), and an `information_schema` existence probe. Neither reads business
# rows on a caller-supplied identifier.
#
# The third was an upsert on `integration_rate_budget`, and it is gone: the
# reservation now delegates to `integration_store.reserve_calls`, which runs
# through `repo.query()` with `VIA_CONNECTION_SCOPE_COLUMNS` and is therefore
# scoped properly rather than exempt. That statement's removal is the point --
# it could not execute, and its scope exemption was the smaller of its two
# problems.
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
