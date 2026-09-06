"""The Zoho integration boundary.

D-14 is unresolved and the target product is provisional, so the platform
above this package is product-blind by construction. It asks
:func:`adapter_for` for an implementation, reads
:class:`~app.backend.integration.adapter.Capabilities` to choose a strategy,
and never learns whether it is talking to Zoho ERP or to a Books + Inventory
estate.

The only two modules that know are :mod:`.erp` and :mod:`.books_inventory`.
Every base URL, OAuth scope and endpoint literal lives in one of them, and
``tests/test_integration_no_hardcoded_endpoints.py`` fails the build if one
escapes.

Nothing in this package talks to a live tenant. Phase 0B has not cleared, so
every collaborator is a cassette or an in-process fake until the lead grants
explicit tenant authorisation.
"""
from __future__ import annotations

from typing import Any

from app.backend.integration.adapter import (
    DEDUPE_CUSTOM_FIELD,
    POLL_OVERLAP_SECONDS,
    Capabilities,
    CapabilityError,
    CassetteError,
    CassetteTransport,
    IntegrationError,
    NetworkForbidden,
    NoNetworkTransport,
    ProcurementAdapter,
    ProductMixingError,
    Transport,
    UnsupportedDataCentre,
    acquire_receives,
    receives_strategy,
    walk_pages,
)
from app.backend.integration.books_inventory import BooksInventoryAdapter
from app.backend.integration.dto import (
    PRODUCTS,
    BillDTO,
    DtoError,
    LineDTO,
    Page,
    Product,
    PurchaseOrderDTO,
    ReceiveDTO,
    SourceRef,
)
from app.backend.integration.erp import ErpAdapter

__all__ = [
    "ADAPTERS",
    "BillDTO",
    "BooksInventoryAdapter",
    "Capabilities",
    "CapabilityError",
    "CassetteError",
    "CassetteTransport",
    "DEDUPE_CUSTOM_FIELD",
    "DtoError",
    "ErpAdapter",
    "IntegrationError",
    "LineDTO",
    "NetworkForbidden",
    "NoNetworkTransport",
    "POLL_OVERLAP_SECONDS",
    "PRODUCTS",
    "Page",
    "ProcurementAdapter",
    "Product",
    "ProductMixingError",
    "PurchaseOrderDTO",
    "ReceiveDTO",
    "SourceRef",
    "Transport",
    "UnsupportedDataCentre",
    "acquire_receives",
    "adapter_for",
    "receives_strategy",
    "walk_pages",
]

#: ``integration_connection.product`` -> implementation. Selection is data, so
#: resolving D-14 is a row in a table, not a redesign.
ADAPTERS = {
    "ERP": ErpAdapter,
    "BOOKS_INVENTORY": BooksInventoryAdapter,
}


def adapter_for(product: str, **kwargs: Any):
    """Build the adapter for one connection's product.

    Raises rather than defaulting. A connection whose product is unknown is a
    connection whose evidence cannot be attributed to a product, and quietly
    picking ERP because the engagement was framed that way is precisely the
    assumption D-14 exists to prevent.
    """
    try:
        implementation = ADAPTERS[product]
    except KeyError:
        raise CapabilityError(
            f"No adapter for product {product!r}. Known products: "
            f"{sorted(ADAPTERS)}. The target product is provisional (D-14) and "
            f"must be read from integration_connection.product, never assumed."
        ) from None
    return implementation(**kwargs)
