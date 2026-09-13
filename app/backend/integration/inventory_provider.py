"""The stock-valuation boundary for internal material fulfilment (034).

WHAT THIS IS
============

An internal material request values the stock it allocates at the ERP's own
stock valuation, or it does not value it at all: a missing valuation is a
visible reconciliation exception (INTERNAL_VALUATION_MISSING) and the
allocation refuses until a rate exists. It is never a silent zero.

The ERP's stock APIs are NOT wired here. The product owner's decision of
2026-09-13 is explicit: "provider boundary for future Zoho stock APIs, no
invented endpoints". Zoho ERP DEMO WBS exposes item-level stock through
endpoints this engagement has not verified live, and every earlier Zoho
"fact" that was assumed rather than verified cost a day. So this module
defines the SEAM and two honest implementations of it:

* :class:`NullInventoryProvider` -- the default. Answers "I do not know" for
  every item, which makes the request's valuation MISSING and raises the
  exception. Honest, and the state of the estate today.
* :class:`RecordingInventoryProvider` -- a table of valuations supplied in
  code (tests, a seeded demo). Answers from the table and records every
  question asked, so a test can assert the service asked for the right
  item at the right store.

A live provider, when the endpoint is verified, implements the same
:class:`InventoryProvider` protocol and is selected by
``CAPEX_INVENTORY_PROVIDER``; nothing in `pg.internal_fulfilment` changes.

MONEY. A valuation is an integer number of MINOR units in a named currency,
never a float. The service converts base-currency (INR) minor units to paise
one-for-one and REFUSES a foreign valuation rather than dividing it -- the
same rule `pg.fx` applies to a foreign-currency order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

#: The estate's base currency. Spelled here rather than imported from
#: `pg.fx` for the reason `sweeps.py` gives: the integration package
#: must not import the ledger. `pg.fx.BASE_CURRENCY` says the same thing.
BASE_CURRENCY = "INR"


@dataclass(frozen=True)
class StockValuation:
    """One answer from the provider: what a unit of `item_external_id` is
    valued at, at `location_external_id`, as of `as_of`, in `currency`.

    `source` names the provider that answered (audited on the request as
    `valuation_reference`), never the number's origin in prose.
    """

    item_external_id: str
    location_external_id: str | None
    unit_rate_minor: int
    currency: str
    as_of: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit_rate_minor, int) or isinstance(self.unit_rate_minor, bool):
            raise TypeError("unit_rate_minor must be an int of minor units, never a float")
        if self.unit_rate_minor <= 0:
            raise ValueError("a stock valuation is positive; zero is a missing valuation")
        if not self.currency:
            raise ValueError("a stock valuation names its currency")


class InventoryProvider(Protocol):
    """The seam. One question, one honest answer or `None`."""

    name: str

    def valuation(self, item_external_id: str,
                  location_external_id: str | None) -> StockValuation | None:
        """The unit valuation, or `None` when this provider does not know."""


class NullInventoryProvider:
    """The default: no stock API is wired, so every valuation is unknown."""

    name = "none"

    def valuation(self, item_external_id: str,
                  location_external_id: str | None) -> StockValuation | None:
        return None


@dataclass
class RecordingInventoryProvider:
    """A provider answering from a supplied table, recording every question.

    Keyed on ``(item_external_id, location_external_id)`` first and on
    ``(item_external_id, None)`` second, so a test can value an item at every
    store with one row or at one store precisely.
    """

    table: dict[tuple[str, str | None], StockValuation] = field(default_factory=dict)
    asked: list[tuple[str, str | None]] = field(default_factory=list)
    name: str = "recording"

    def valuation(self, item_external_id: str,
                  location_external_id: str | None) -> StockValuation | None:
        self.asked.append((item_external_id, location_external_id))
        return (self.table.get((item_external_id, location_external_id))
                or self.table.get((item_external_id, None)))

    def value(self, item_external_id: str, unit_rate_paise: int, *,
              location_external_id: str | None = None,
              currency: str = BASE_CURRENCY,
              as_of: datetime | None = None) -> StockValuation:
        row = StockValuation(
            item_external_id=item_external_id,
            location_external_id=location_external_id,
            unit_rate_minor=unit_rate_paise, currency=currency,
            as_of=as_of or datetime.now(timezone.utc), source=self.name)
        self.table[(item_external_id, location_external_id)] = row
        return row


_PROVIDER: InventoryProvider | None = None


def get_inventory_provider() -> InventoryProvider:
    """The process's provider. ``CAPEX_INVENTORY_PROVIDER`` selects it;
    anything but the two names above is refused at first use rather than
    silently falling back to `none` -- a misspelt live provider must not read
    as "no valuation" on every request."""
    global _PROVIDER
    if _PROVIDER is None:
        name = (os.environ.get("CAPEX_INVENTORY_PROVIDER") or "none").strip().lower()
        if name == "none":
            _PROVIDER = NullInventoryProvider()
        elif name == "recording":
            _PROVIDER = RecordingInventoryProvider()
        else:
            raise RuntimeError(
                f"CAPEX_INVENTORY_PROVIDER={name!r} names no provider this build "
                f"carries (none, recording). A live Zoho stock provider is wired "
                f"here once its endpoint is verified against the DEMO tenant.")
    return _PROVIDER


def set_inventory_provider(provider: InventoryProvider | None) -> None:
    """Install a provider for this process (tests; the seeded demo)."""
    global _PROVIDER
    _PROVIDER = provider
