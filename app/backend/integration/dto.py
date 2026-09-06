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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Generic, Literal, Mapping, TypeVar

from app.backend.money import MoneyError, to_paise

__all__ = [
    "PRODUCTS",
    "BillDTO",
    "DtoError",
    "LineDTO",
    "Page",
    "Product",
    "PurchaseOrderDTO",
    "ReceiveDTO",
    "SourceRef",
    "freeze",
    "paise",
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
def paise(value: Any, *, field: str, allow_missing: bool = False) -> int:
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
        return to_paise(value, field=field, allow_negative=True)
    except MoneyError as exc:
        raise DtoError(str(exc)) from exc


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
    dimensions: Mapping[str, Any] = MappingProxyType({})
    raw: Mapping[str, Any] = MappingProxyType({})


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
    raw: Mapping[str, Any] = MappingProxyType({})


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
    raw: Mapping[str, Any] = MappingProxyType({})


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
    raw: Mapping[str, Any] = MappingProxyType({})
