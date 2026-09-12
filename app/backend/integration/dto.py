"""Our procurement DTOs. Never Zoho's shapes.

Plan v1.2.1 §11.10: "``DTO`` types are ours, not Zoho's. Raw payloads are
preserved in ``integration_inbox`` with the product and API version that
produced them (§8.4), so a mapping error is always traceable to a source
document."

Three rules hold everywhere in this module and are enforced by tests:

**Money is integer paise.** Every monetary field is an ``int`` named
``*_paise``. No float. No ``Decimal``. Zoho sends monetary values as JSON
numbers, so the cassette and transport layers parse JSON with
``parse_float=str`` and the amount reaches :func:`paise` as an exact decimal
string. ``app.backend.money.to_paise`` then rounds once, half-up, the way
AUD-H-007 established.

**Quantity is a string, not a number.** A quantity is decimal but is not
money, so it gets neither ``to_paise`` nor a float. It is carried as the
exact decimal string the source sent, and whoever needs arithmetic on it
declares its own precision.

**Every DTO carries its provenance.** :class:`SourceRef` records the product,
the API version and the endpoint that produced the record. A DTO that cannot
say where it came from is not admissible: §11 opens with "Products are never
mixed. Books and Inventory documentation is inadmissible as evidence for ERP,
and vice versa", and that rule is only enforceable if every value remembers
its origin.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Generic, Literal, Mapping, TypeVar

from app.backend.money import (CURRENCY_EXPONENT, MoneyError,
                               minor_exponent_of, to_paise)

__all__ = [
    "PRODUCTS",
    "BillDTO",
    "ContactDTO",
    "DtoError",
    "EmissionRef",
    "ItemDTO",
    "LineDTO",
    "Page",
    "Product",
    "PurchaseOrderDTO",
    "PurchaseOrderEmissionDTO",
    "ReceiveDTO",
    "SourceRef",
    "emitted_paise",
    "freeze",
    "paise",
    "rate_text",
    "parse_zoho_date",
    "parse_zoho_datetime",
    "quantity",
]

#: The two products the adapter boundary supports. D-14 decides which is real.
Product = Literal["ERP", "BOOKS_INVENTORY"]

#: The same two, as data, for runtime validation.
PRODUCTS: frozenset[str] = frozenset({"ERP", "BOOKS_INVENTORY"})

T = TypeVar("T")


class DtoError(ValueError):
    """A source payload could not be mapped into one of our DTOs."""


# ============================================================ value conversion
#: An immutable empty mapping, built per instance.
#:
#: These were `= MappingProxyType({})` as direct dataclass defaults. Python
#: 3.11's dataclasses reject that: it decides mutability by hashability, and a
#: mappingproxy is unhashable, so every one of these raised
#: `ValueError: mutable default ... use default_factory` AT IMPORT.
#:
#: Nothing local caught it because this machine runs 3.14, where the check was
#: relaxed. CI runs 3.11 -- which the setup instructions specify -- so the
#: ENTIRE integration package failed to import there and every integration
#: test errored during collection. The package had never once been imported in
#: CI. Same class as the `ast.dump` instability this project hit before:
#: a stdlib behaviour that differs across versions, hidden by a version gap
#: between the dev machine and the build.
def _EMPTY_MAPPING() -> Mapping[str, Any]:
    return MappingProxyType({})


def paise(value: Any, *, field: str, allow_missing: bool = False,
          minor_exponent: int = CURRENCY_EXPONENT) -> int:
    """A source monetary value as integer paise.

    ``value`` is expected to be a decimal *string* -- the transport parses JSON
    with ``parse_float=str`` precisely so that a monetary amount never exists
    as a float even momentarily. A float is rejected outright here rather than
    rounded, because silently rounding a float is how AUD-H-007 happened.
    """
    if value is None or value == "":
        if allow_missing:
            return 0
        raise DtoError(f"{field} is missing from the source payload.")
    if isinstance(value, float):
        raise DtoError(
            f"{field} arrived as a float ({value!r}). Money must never exist as "
            f"a float: parse the source JSON with parse_float=str.")
    try:
        return to_paise(value, field=field, allow_negative=True,
                        minor_exponent=minor_exponent)
    except MoneyError as exc:
        raise DtoError(str(exc)) from exc


def rate_text(value: Any, *, field: str) -> str | None:
    """A source exchange rate as the exact decimal STRING it was stated as.

    ``None`` when the source stated none (a receive never states one; a bill
    in the base currency may carry ``1`` or nothing). A float is REFUSED, for
    the reason :func:`paise` refuses one: an exchange rate is multiplied into
    money, and the transport parses JSON with ``parse_float=str`` so that a
    rate, like an amount, never exists as a float even momentarily. Nothing
    here parses the string into a number -- that is `pg.fx.parse_rate`'s job,
    at :data:`pg.fx.RATE_SCALE`, where a rate too precise to store is refused
    rather than rounded.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DtoError(f"{field} must be a number, not a boolean.")
    if isinstance(value, float):
        raise DtoError(
            f"{field} arrived as a float ({value!r}). An exchange rate is "
            f"multiplied into money and must never exist as a float: parse "
            f"the source JSON with parse_float=str.")
    text = str(value).strip()
    return text or None


def quantity(value: Any, *, field: str) -> str:
    """A source quantity as an exact decimal string.

    Deliberately NOT a number. A quantity is decimal but is not money, so it
    gets neither paise nor a float; the caller that needs arithmetic declares
    its own precision rather than inheriting binary rounding from us.
    """
    if value is None or value == "":
        raise DtoError(f"{field} is missing from the source payload.")
    if isinstance(value, float):
        raise DtoError(
            f"{field} arrived as a float ({value!r}). Parse the source JSON "
            f"with parse_float=str so quantities stay exact.")
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise DtoError(f"{field} is not a decimal quantity: {value!r}")
    return text


#: ``2026-08-27T14:22:31+0530``, ``...+05:30``, ``...Z`` and the bare form.
_DATETIME = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$")


def parse_zoho_datetime(value: Any, *, field: str) -> datetime:
    """Parse the timestamp forms the three products actually emit.

    Zoho is not consistent: ERP and Books emit an offset (``+0530``, sometimes
    ``+05:30``), Inventory's item delta documents ``yyyy-MM-ddTHH:mm:ssZ``, and
    some list responses omit the offset entirely. An offset-naive value is
    treated as UTC and said so here rather than guessed silently at a call site.
    """
    if not isinstance(value, str):
        raise DtoError(f"{field} is not a timestamp: {value!r}")
    match = _DATETIME.match(value.strip())
    if match is None:
        raise DtoError(f"{field} is not a recognised Zoho timestamp: {value!r}")
    stamp = datetime.fromisoformat(match.group("stamp").replace(" ", "T"))
    tz = match.group("tz")
    if tz is None:
        return stamp.replace(tzinfo=timezone.utc)
    if tz == "Z":
        return stamp.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return stamp.replace(tzinfo=timezone(sign * offset))


def parse_zoho_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise DtoError(f"{field} is not a date (YYYY-MM-DD): {value!r}")
    return date.fromisoformat(value.strip())


def freeze(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """A read-only view of a source payload, so a DTO cannot be edited in place."""
    return MappingProxyType(dict(mapping or {}))


# ==================================================================== the DTOs
@dataclass(frozen=True)
class SourceRef:
    """Where one DTO came from.

    §11.10 requires that a mapping error always be traceable to a source
    document. ``service`` is the Zoho service that answered (``erp``,
    ``books``, ``inventory``) and is NOT the same as ``product``: the
    ``BOOKS_INVENTORY`` product answers from two different services, and
    losing that distinction is exactly how a Books fact becomes evidence for
    an Inventory claim.
    """
    product: str
    service: str
    api_version: str
    endpoint: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if self.product not in PRODUCTS:
            raise DtoError(
                f"SourceRef.product must be one of {sorted(PRODUCTS)}, "
                f"not {self.product!r}.")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of a paged collection.

    Pagination is the one thing uniform across all three products (§11.2):
    ``page``/``per_page`` in, ``page_context.has_more_page`` out. There is no
    cursor and no total count to rely on.
    """
    items: tuple[T, ...]
    page: int
    per_page: int
    has_more: bool

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class LineDTO:
    """One document line.

    ``purchase_order_line_external_id`` is the AUD-H-004 field. Documentation
    says bill lines carry ``purchaseorder_item_id`` and Inventory receive lines
    carry the PO's ``line_item_id`` -- but §11.8 is explicit that *documented
    is not populated*. So this is ``None`` when the source omitted it, never a
    guess and never spread pro-rata; an unresolved linkage becomes a
    ``GRN_LINE_UNATTRIBUTED`` exception upstream of us.
    """
    external_line_id: str | None
    line_number: int
    description: str
    quantity: str
    unit_price_paise: int
    line_total_paise: int
    tax_paise: int
    item_external_id: str | None = None
    purchase_order_line_external_id: str | None = None
    dimensions: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


@dataclass(frozen=True)
class BillDTO:
    """A vendor bill -- actual CWIP.

    ``lines_hydrated`` is not decoration. List responses omit ``line_items`` on
    every one of the three products, which is why §11.5 carries a separate
    ``sweep_bill_detail`` job. A caller that treats an unhydrated bill's empty
    ``lines`` as "this bill has no lines" books zero CWIP, so the flag makes
    the difference between "no lines" and "lines not fetched yet" explicit.
    """
    source: SourceRef
    external_id: str
    document_number: str
    document_date: date
    last_modified: datetime
    vendor_external_id: str | None
    vendor_name: str
    currency_code: str
    subtotal_paise: int
    tax_paise: int
    total_paise: int
    external_status_raw: str
    purchase_order_external_ids: tuple[str, ...] = ()
    lines: tuple[LineDTO, ...] = ()
    lines_hydrated: bool = False
    #: The rate the bill itself states, source currency -> the organisation's
    #: base currency, as the exact decimal STRING the row carried (Zoho's
    #: ``exchange_rate``; both adapters populate it through :func:`rate_text`).
    #: ``None`` when the row states none. This is the "its own currency and
    #: rate" of product owner decision 8 (2026-09-11): a bill against a
    #: non-INR order books only when it carries both, or an ACTIVE rate is on
    #: file for its date, and is otherwise held as
    #: FOREIGN_CURRENCY_BASIS_MISSING. Defaulted so every existing
    #: construction is unchanged.
    exchange_rate: str | None = None
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


@dataclass(frozen=True)
class PurchaseOrderDTO:
    """A purchase order -- the commitment.

    ``receive_external_ids`` is the PO-anchored discovery payload (§11.4). On
    ERP it is the *only* way a goods receipt is ever discovered, because ERP
    Purchase Receives has no collection endpoint at all.
    """
    source: SourceRef
    external_id: str
    document_number: str
    document_date: date
    last_modified: datetime
    vendor_external_id: str | None
    vendor_name: str
    currency_code: str
    subtotal_paise: int
    tax_paise: int
    total_paise: int
    external_status_raw: str
    receive_external_ids: tuple[str, ...] = ()
    lines: tuple[LineDTO, ...] = ()
    lines_hydrated: bool = False
    dedupe_key: str | None = None
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


@dataclass(frozen=True)
class ReceiveDTO:
    """A goods receipt (GRN) -- received-but-unbilled exposure."""
    source: SourceRef
    external_id: str
    document_number: str
    document_date: date
    last_modified: datetime
    purchase_order_external_id: str | None
    external_status_raw: str
    lines: tuple[LineDTO, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


@dataclass(frozen=True)
class ItemDTO:
    """One row of the item master.

    Master data, not a document: an item has no ``document_date`` and no
    monetary total, so it deliberately carries neither. ``rate_paise`` is the
    item's list rate and is integer paise like every other monetary value here;
    it is ``0`` when the source omitted it, because an item with no rate is
    ordinary (a service line priced per order) whereas a *bill* with no total
    is a mapping failure.

    ``last_modified`` is ``None`` when the source omitted it. That is not
    cosmetic: on ERP and Books ``GET /items`` there is **no**
    ``last_modified_time`` filter (plan §11.3), so those products get a weekly
    full refresh and the field may simply be absent. A poll that treated a
    missing timestamp as "modified now" would advance a watermark past records
    it never saw.
    """
    source: SourceRef
    external_id: str
    name: str
    external_status_raw: str
    last_modified: datetime | None = None
    sku: str | None = None
    description: str = ""
    rate_paise: int = 0
    currency_code: str = "INR"
    item_type: str | None = None
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


@dataclass(frozen=True)
class ContactDTO:
    """One row of the contact master -- for us, the vendor master.

    ``contact_type`` is stored as the source sent it (``vendor``, ``customer``)
    and is **not** filtered on inside the adapter: the poll ingests the whole
    contact population and the platform decides what a vendor is. Deciding it
    here would bury a business rule inside a transport-shaped module, and the
    same tenant can legitimately bill a party it also buys from.

    Like :class:`ItemDTO`, ``last_modified`` may be ``None``: ``GET /contacts``
    is sort-only on all three products (plan §11.3) -- it has no
    ``last_modified_time`` *filter* at all -- so contacts are acquired by a
    full refresh and the timestamp is informational rather than a cursor.
    """
    source: SourceRef
    external_id: str
    contact_name: str
    external_status_raw: str
    company_name: str = ""
    contact_type: str | None = None
    last_modified: datetime | None = None
    email: str | None = None
    currency_code: str = "INR"
    raw: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)


# ======================================================== the OUTBOUND shapes
# Everything above this line describes a document Zoho SENT US. Everything
# below describes one we are about to send Zoho, and the two are deliberately
# different types.
#
# WHY NOT REUSE `PurchaseOrderDTO`.
# It was tried, and it is wrong in a way that matters. `PurchaseOrderDTO`
# requires five fields that do not exist yet at emission time: `external_id`
# (Zoho mints it in the response we have not received), `document_number`,
# `last_modified`, `external_status_raw`, and -- the decisive one --
# `source: SourceRef`. `SourceRef` is a PROVENANCE CLAIM: this module's own
# docstring says a DTO that cannot say where it came from is not admissible,
# and section 11 makes provenance the mechanism that stops a Books fact
# becoming evidence for an Inventory claim. Filling it in for a document we are
# inventing means fabricating a retrieval that never happened, from an endpoint
# we never called. The other four would be empty strings and a placeholder
# timestamp. Five fabricated fields, one of them the audit trail, to reuse a
# class whose only overlap with an emission is four attribute names.
#
# So an emission carries `EmissionRef` instead: the outbound mirror of
# `SourceRef`, naming the outbox row this document came from rather than the
# response it came back in. A mapping error is still always traceable to a
# record -- which is what section 11.10 actually asks for -- but the record is
# ours.


@dataclass(frozen=True)
class EmissionRef:
    """Where one OUTBOUND DTO came from: our outbox row, not Zoho's response.

    The outbound counterpart of :class:`SourceRef`. ``(connection_id, module,
    local_id)`` is C2's uniqueness on ``integration_outbox`` and ``dedupe_key``
    is what the emitted document will carry in ``cf_capex_ref``, so these four
    values locate both halves of the emission -- the row we read and the
    document the tenant now holds -- from either end.
    """
    connection_id: str
    module: str
    local_id: str
    dedupe_key: str

    def __post_init__(self) -> None:
        for name in ("connection_id", "module", "local_id", "dedupe_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DtoError(
                    f"EmissionRef.{name} must be a non-empty string; got "
                    f"{value!r}. An emission that cannot name the outbox row "
                    "it came from is not traceable, and an untraceable "
                    "commitment is the one thing section 11.10 rules out.")


def emitted_paise(value: Any, *, field: str, allow_zero: bool = True) -> int:
    """A monetary value on an OUTBOUND document, as integer paise.

    Deliberately NOT :func:`paise`. That one parses a value Zoho sent us, which
    arrives as a decimal string and gets rounded once. This one validates a
    figure we computed ourselves, which must ALREADY be integer paise -- so a
    string, a float or a ``Decimal`` reaching it is a defect in our own code
    and is refused rather than converted. Converting here would be the last
    place a rounding error could enter a purchase order, and a purchase order
    is a commitment.

    ``bool`` is rejected explicitly because ``isinstance(True, int)`` is
    ``True`` in Python, and ``True`` would otherwise be accepted as one paisa.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise DtoError(
            f"{field} must be integer paise (bigint), not "
            f"{type(value).__name__} ({value!r}). Money never travels as a "
            "float, a Decimal or a string on the outbound path.")
    if value < 0:
        raise DtoError(f"{field} must not be negative; got {value}.")
    if value == 0 and not allow_zero:
        raise DtoError(
            f"{field} must be positive; got 0. A zero here would emit a "
            "purchase order committing nothing, which no approval reviewed.")
    return value


def _emission_identifier(value: Any, *, field: str) -> str:
    """A non-blank identifier on an outbound document."""
    if not isinstance(value, str) or not value.strip():
        raise DtoError(
            f"{field} must be a non-empty string; got {value!r}. An emission "
            "missing an identifier cannot be linked back to what it commits "
            "against, and an unlinked commitment is an unreconcilable one.")
    return value


@dataclass(frozen=True)
class PurchaseOrderEmissionDTO:
    """A purchase order we are about to create, validated field by field.

    This is the type the C1 adapters' ``create_purchase_order`` and
    ``update_purchase_order`` actually consume: both build their wire body by
    ATTRIBUTE access (``po.vendor_external_id``, ``po.document_date``,
    ``po.currency_code``, ``po.lines``, and per line ``unit_price_paise``,
    ``quantity``, ``description``, ``item_external_id``). Handing them a
    ``Mapping`` raises ``AttributeError`` on the first field, which is why the
    FIRST emission -- not merely a retry -- could never have worked.

    ``document_date`` is a :class:`datetime.date` and a :class:`datetime` is
    REFUSED, even though ``datetime`` subclasses ``date``. The adapters send
    ``po.document_date.isoformat()`` into a Zoho ``date`` field; a ``datetime``
    would render ``2026-09-07T10:00:00+00:00`` there, and a purchase order
    whose document date is rejected or silently truncated is a commitment
    landing in a period nobody chose.
    """
    origin: EmissionRef
    vendor_external_id: str
    document_date: date
    currency_code: str
    lines: tuple[LineDTO, ...]
    subtotal_paise: int
    tax_paise: int
    total_paise: int
    reference: str | None = None
    #: Header-level ``(wbs_id, budget_head_id)``, populated only when D-7
    #: resolved False and the dimensions could not go on the lines.
    dimensions: Mapping[str, Any] = field(default_factory=_EMPTY_MAPPING)
    #: Always ``draft`` (section 11.7). The move to ``open`` is a separate call.
    state: str = "draft"

    def __post_init__(self) -> None:
        if not isinstance(self.origin, EmissionRef):
            raise DtoError(
                "PurchaseOrderEmissionDTO.origin must be an EmissionRef; got "
                f"{type(self.origin).__name__}.")
        _emission_identifier(
            self.vendor_external_id,
            field="PurchaseOrderEmissionDTO.vendor_external_id")

        # `datetime` is a subclass of `date`, so the ORDER of these two checks
        # is the check.
        if isinstance(self.document_date, datetime) or not isinstance(
                self.document_date, date):
            raise DtoError(
                "PurchaseOrderEmissionDTO.document_date must be a "
                f"datetime.date, not {type(self.document_date).__name__} "
                f"({self.document_date!r}). The adapter sends "
                "document_date.isoformat() into a Zoho DATE field, so a "
                "datetime would write a timestamp where a date belongs.")

        code = self.currency_code
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code):
            raise DtoError(
                "PurchaseOrderEmissionDTO.currency_code must be a three-letter "
                f"ISO-4217 code in upper case; got {code!r}.")

        if self.state != "draft":
            raise DtoError(
                "A purchase order is emitted as 'draft', never "
                f"{self.state!r}. The transition to 'open' is a separate call "
                "made only after our approval instance closes (section 11.7).")

        if not isinstance(self.lines, tuple) or not self.lines:
            raise DtoError(
                "PurchaseOrderEmissionDTO.lines must be a non-empty tuple; got "
                f"{self.lines!r}. A purchase order with no lines commits "
                "nothing and would still consume a dedupe key.")

        running = 0
        for index, line in enumerate(self.lines, start=1):
            if not isinstance(line, LineDTO):
                raise DtoError(
                    f"PurchaseOrderEmissionDTO.lines[{index - 1}] must be a "
                    f"LineDTO; got {type(line).__name__}. The adapter reads "
                    "line attributes, not keys.")
            where = f"PurchaseOrderEmissionDTO.lines[{index - 1}]"
            if line.line_number != index:
                raise DtoError(
                    f"{where}.line_number is {line.line_number}, expected "
                    f"{index}. Line numbers are the operator's only handle on "
                    "which line of a purchase order a query is about; a gap or "
                    "a repeat makes a reconciliation query silently ambiguous.")
            emitted_paise(line.unit_price_paise,
                          field=f"{where}.unit_price_paise")
            emitted_paise(line.line_total_paise,
                          field=f"{where}.line_total_paise")
            emitted_paise(line.tax_paise, field=f"{where}.tax_paise")
            quantity(line.quantity, field=f"{where}.quantity")
            if (not isinstance(line.description, str)
                    or not line.description.strip()):
                raise DtoError(
                    f"{where}.description is required; got "
                    f"{line.description!r}. A vendor cannot acknowledge an "
                    "unnamed line.")
            running += line.line_total_paise

        emitted_paise(self.subtotal_paise,
                      field="PurchaseOrderEmissionDTO.subtotal_paise")
        emitted_paise(self.tax_paise,
                      field="PurchaseOrderEmissionDTO.tax_paise")
        emitted_paise(self.total_paise,
                      field="PurchaseOrderEmissionDTO.total_paise",
                      allow_zero=False)

        # Integer arithmetic, checked rather than trusted. These two identities
        # are the only thing standing between a line-mapping mistake and a
        # purchase order that commits a different number from the one the
        # budget check cleared. They are asserted HERE, at the boundary, rather
        # than only in a test: a test proves the mapper is right today, whereas
        # this refuses the document on the day it stops being.
        if running != self.subtotal_paise:
            raise DtoError(
                f"PurchaseOrderEmissionDTO.subtotal_paise is "
                f"{self.subtotal_paise} but the lines sum to {running}. A "
                "purchase order whose header disagrees with its own lines "
                "commits an amount no line accounts for.")
        if self.subtotal_paise + self.tax_paise != self.total_paise:
            raise DtoError(
                f"PurchaseOrderEmissionDTO.total_paise is {self.total_paise} "
                f"but subtotal {self.subtotal_paise} + tax {self.tax_paise} is "
                f"{self.subtotal_paise + self.tax_paise}.")

        # THE THIRD IDENTITY, and the one whose absence reached a vendor.
        #
        # Zoho's line carries a RATE and a QUANTITY and no line total, so
        # whatever we send is multiplied at the far end. A line whose total is
        # not divisible by its quantity has no exact per-unit price: a 3-unit
        # Rs 1,000.00 line was sent as 333.33 and invoiced as Rs 999.99 --
        # committing one number here and billing another there, silently,
        # every time.
        #
        # Refusing is the only honest answer. Rounding the rate picks a number
        # the budget check never cleared; rounding the quantity changes what
        # was ordered. Both are decisions this boundary is not entitled to make
        # for the caller, and one of them was being made.
        #
        # Integer arithmetic only -- see this module's header. The quantity is
        # a decimal string, so it is scaled to integers rather than parsed into
        # a Decimal: "2.5" -> (25, 1), and the identity becomes
        # `unit_price * 25 == line_total * 10`.
        for line in self.lines:
            text = str(line.quantity).strip()
            whole, _, frac = text.partition(".")
            digits = f"{whole}{frac}"
            try:
                scaled_quantity = int(digits)
            except ValueError:
                raise DtoError(
                    f"PurchaseOrderEmissionDTO line {line.line_number}: "
                    f"quantity {line.quantity!r} is not a decimal number.")
            scale = 10 ** len(frac)
            if line.unit_price_paise * scaled_quantity != line.line_total_paise * scale:
                implied = line.unit_price_paise * scaled_quantity
                raise DtoError(
                    f"PurchaseOrderEmissionDTO line {line.line_number}: rate "
                    f"{line.unit_price_paise} x quantity {line.quantity} is "
                    f"{implied}/{scale} paise, not the line total "
                    f"{line.line_total_paise}. Zoho multiplies rate by "
                    "quantity at the far end, so this document would invoice a "
                    "different amount from the one the budget check cleared. A "
                    "line total that is not divisible by its quantity has no "
                    "exact per-unit price and must be split or re-quantified "
                    "upstream, never rounded here.")
