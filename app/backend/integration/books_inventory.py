"""Zoho Books v3 + Zoho Inventory v1 -- the other candidate implementation of C1.

**This is not a fallback.** D-14 is unresolved, and clients routinely say
"Zoho ERP" about a Books + Inventory estate. §11 keeps the Books and Inventory
rows to the same evidentiary standard as the ERP rows precisely because this
may turn out to be the real target. It is maintained as an equal, not as
background.

One product, two services
-------------------------
``BOOKS_INVENTORY`` is a single product to the platform above and two HTTP
services underneath: ``/books/v3`` carries bills, purchase orders, vendors and
the item master's accounting view; ``/inventory/v1`` carries goods receipts.
:class:`~app.backend.integration.dto.SourceRef` records ``service`` separately
from ``product`` for exactly this reason -- collapsing the two would let a
Books fact become evidence for an Inventory claim inside a single adapter,
which is the same error §11 forbids across adapters.

Where this product differs from ERP, and it matters
---------------------------------------------------
* **Purchase Receives is listable** (Inventory), so ``receives_listable`` is
  ``True`` and :meth:`BooksInventoryAdapter.list_receives` exists. The GRN
  problem that dominates ERP's design is, here, materially easier -- which is
  itself a reason not to treat the target as settled.
* **Inventory has no ``.ALL`` operation type.** Books and ERP document it;
  Inventory's documentation enumerates CRUD individually and never shows
  ``.ALL``. So no ``ZohoInventory.*.ALL`` scope is ever requested, because a
  scope that is not documented is a scope that may not be granted.
* **Inventory ``GET /purchaseorders`` has neither filter nor sort.** Its list
  accepts ``organization_id``, ``page`` and ``per_page`` and nothing else, so
  an Inventory-anchored PO poll can only be a full re-pull. This adapter reads
  POs from Books, where ``last_modified_time`` *is* filterable -- and
  :data:`INVENTORY_PURCHASE_ORDER_LIST_PARAMS` keeps the Inventory constraint
  recorded so nobody later "optimises" the poll onto the service that cannot
  support it.
* **Inventory ``GET /items`` is the one true delta** in the whole matrix:
  ``last_modified_time`` is both filterable and sortable, in
  ``yyyy-MM-ddTHH:mm:ssZ`` -- a different format from the offset-bearing stamp
  Books bills and POs take. Two formats, two constants, no shared guess.
* **Eight data centres**, against ERP's one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.backend.integration.adapter import (
    Capabilities,
    CapabilityError,
    DEDUPE_CUSTOM_FIELD,
    IntegrationError,
    NoNetworkTransport,
    Transport,
    UnsupportedDataCentre,
    page_context,
    window_params,
)
from app.backend.integration.dto import (
    BillDTO,
    LineDTO,
    Page,
    PurchaseOrderDTO,
    ReceiveDTO,
    SourceRef,
    freeze,
    paise,
    parse_zoho_date,
    parse_zoho_datetime,
    quantity,
)

__all__ = [
    "API_HOST_BY_DC",
    "BOOKS_API_VERSION",
    "BooksInventoryAdapter",
    "INVENTORY_API_VERSION",
    "INVENTORY_PURCHASE_ORDER_LIST_PARAMS",
    "PRODUCT",
    "SCOPES",
    "SERVICE_PATHS",
    "SORT_COLUMNS",
]

PRODUCT = "BOOKS_INVENTORY"
SERVICE_BOOKS = "books"
SERVICE_INVENTORY = "inventory"
BOOKS_API_VERSION = "v3"
INVENTORY_API_VERSION = "v1"

SERVICE_PATHS: Mapping[str, str] = {
    SERVICE_BOOKS: "/books/v3",
    SERVICE_INVENTORY: "/inventory/v1",
}

#: The eight documented API hosts. Both services are served from all eight.
#: (The *accounts* servers are a separate question: only five are confirmed for
#: Inventory, with JP/CN/SA NOT CONFIRMED -- see :data:`ACCOUNTS_SERVER_BY_DC`.)
API_HOST_BY_DC: Mapping[str, str] = {
    "US": "https://www.zohoapis.com",
    "EU": "https://www.zohoapis.eu",
    "IN": "https://www.zohoapis.in",
    "AU": "https://www.zohoapis.com.au",
    "JP": "https://www.zohoapis.jp",
    "CA": "https://www.zohoapis.ca",
    "CN": "https://www.zohoapis.com.cn",
    "SA": "https://www.zohoapis.sa",
}

#: Confirmed accounts servers, and the three that are not. A ``None`` here is
#: a refusal to guess, not an omission: an OAuth flow pointed at an unconfirmed
#: accounts host fails at consent time, in front of the client.
ACCOUNTS_SERVER_BY_DC: Mapping[str, str | None] = {
    "US": "https://accounts.zoho.com",
    "EU": "https://accounts.zoho.eu",
    "IN": "https://accounts.zoho.in",
    "AU": "https://accounts.zoho.com.au",
    "CA": "https://accounts.zohocloud.ca",
    "JP": None,     # NOT CONFIRMED for Inventory
    "CN": None,     # NOT CONFIRMED for Inventory
    "SA": None,     # NOT CONFIRMED for Inventory
}

DAILY_CALL_CEILING_BY_PLAN: Mapping[str, int] = {
    "FREE": 1000, "STANDARD": 2000, "PROFESSIONAL": 5000, "PREMIUM": 10000,
}

# ------------------------------------------------------------------ endpoints
PATH_BILLS = "/bills"
PATH_BILL = "/bills/{external_id}"
PATH_PURCHASE_ORDERS = "/purchaseorders"
PATH_PURCHASE_ORDER = "/purchaseorders/{external_id}"
PATH_CONTACTS = "/contacts"
PATH_ITEMS = "/items"
#: Inventory *does* have a collection endpoint for receives. This constant is
#: the whole difference between this product's GRN design and ERP's.
PATH_PURCHASE_RECEIVES = "/purchasereceives"
PATH_PURCHASE_RECEIVE = "/purchasereceives/{external_id}"

#: Everything Inventory's ``GET /purchaseorders`` accepts. Recorded as data so
#: the constraint outlives the sentence in the plan that states it.
INVENTORY_PURCHASE_ORDER_LIST_PARAMS: frozenset[str] = frozenset(
    {"organization_id", "page", "per_page"})

SORT_COLUMNS: Mapping[tuple[str, str], frozenset[str]] = {
    # Books bills/POs: last_modified_time is filterable and NOT sortable.
    (SERVICE_BOOKS, PATH_BILLS): frozenset(
        {"vendor_name", "bill_number", "date", "due_date", "total", "balance",
         "created_time"}),
    (SERVICE_BOOKS, PATH_PURCHASE_ORDERS): frozenset(
        {"vendor_name", "purchaseorder_number", "date", "total", "created_time"}),
    (SERVICE_BOOKS, PATH_CONTACTS): frozenset(
        {"contact_name", "created_time", "last_modified_time"}),
    # Inventory receives: sort only, no filter. `date` is the column used for
    # the descending walk with early stop. `last_modified_time` is deliberately
    # NOT listed -- it is not confirmed as a sort column on this endpoint, and
    # borrowing it from Inventory's items endpoint would be exactly the
    # cross-endpoint inference §11 rules out.
    (SERVICE_INVENTORY, PATH_PURCHASE_RECEIVES): frozenset(
        {"date", "receive_number", "created_time"}),
    # Inventory items: filter AND sort. The one true delta in the matrix.
    (SERVICE_INVENTORY, PATH_ITEMS): frozenset(
        {"name", "rate", "created_time", "last_modified_time"}),
    # Inventory POs: neither. An empty set is a fact, not a gap.
    (SERVICE_INVENTORY, PATH_PURCHASE_ORDERS): frozenset(),
}

DELTA_FILTER_PARAM = "last_modified_time"
#: Books/ERP form: offset-bearing, ``+`` must reach the wire as ``%2B``.
BOOKS_DELTA_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
#: Inventory items form: documented as ``yyyy-MM-ddTHH:mm:ssZ``. A different
#: format on a different service, held apart on purpose.
INVENTORY_DELTA_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: No ``ZohoInventory.*.ALL`` anywhere: Inventory does not document ``.ALL``.
SCOPES: frozenset[str] = frozenset({
    "ZohoBooks.purchaseorders.ALL",     # read for reconciliation, create to emit
    "ZohoBooks.bills.READ",
    "ZohoBooks.contacts.READ",
    "ZohoBooks.settings.READ",          # Books files items under settings too
    "ZohoInventory.purchasereceives.READ",
    "ZohoInventory.items.READ",
    "ZohoInventory.purchaseorders.READ",
})


def _format_books_delta(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime(BOOKS_DELTA_FORMAT)


def _format_inventory_delta(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(INVENTORY_DELTA_FORMAT)


class BooksInventoryAdapter:
    """Zoho Books v3 + Zoho Inventory v1 behind one C1 interface."""

    product = PRODUCT

    def __init__(
        self,
        *,
        organization_id: str,
        dc: str = "IN",
        transport: Transport | None = None,
        per_page: int = 50,
        plan: str = "STANDARD",
        line_level_custom_fields: bool = False,
    ) -> None:
        self.organization_id = str(organization_id)
        self.dc = dc.upper()
        self.per_page = per_page
        self.plan = plan.upper()
        self.line_level_custom_fields = line_level_custom_fields
        self.transport = transport or NoNetworkTransport(PRODUCT)
        if getattr(self.transport, "product", None) != PRODUCT:
            raise IntegrationError(
                f"A Books+Inventory adapter was given a "
                f"{getattr(self.transport, 'product', None)!r} transport. "
                f"Products are never mixed.")
        self.books_base = self.service_base_url(SERVICE_BOOKS, self.dc)
        self.inventory_base = self.service_base_url(SERVICE_INVENTORY, self.dc)

    # -------------------------------------------------------------- metadata
    def service_base_url(self, service: str, dc: str) -> str:
        host = API_HOST_BY_DC.get(dc.upper())
        if host is None:
            raise UnsupportedDataCentre(
                f"{dc!r} is not one of the eight documented Zoho API hosts: "
                f"{sorted(API_HOST_BY_DC)}.")
        try:
            return f"{host}{SERVICE_PATHS[service]}"
        except KeyError:
            raise UnsupportedDataCentre(
                f"{service!r} is not a service of this product; expected one "
                f"of {sorted(SERVICE_PATHS)}.") from None

    def base_url(self, dc: str) -> str:
        """The C1 method. Returns the **Books** base.

        The seam gives one ``base_url`` and this product has two services, so
        one of them has to be the answer. Books is: it is the accounting ledger
        of record, it carries bills and purchase orders, and it is the service
        every C1 method except the receives path uses. Inventory is reached
        through :meth:`service_base_url`, which is explicit at the call site
        rather than implied -- so no caller can end up sending a Books request
        to an Inventory host and read the 404 as "no such document".
        """
        return self.service_base_url(SERVICE_BOOKS, dc)

    def accounts_server(self, dc: str) -> str:
        server = ACCOUNTS_SERVER_BY_DC.get(dc.upper(), "missing")
        if server == "missing":
            raise UnsupportedDataCentre(f"{dc!r} is not a documented data centre.")
        if server is None:
            raise UnsupportedDataCentre(
                f"The accounts server for {dc!r} is NOT CONFIRMED for Zoho "
                f"Inventory. Five of the eight are confirmed; JP, CN and SA are "
                f"not, and an OAuth flow must not be pointed at a guess.")
        return server

    def scopes_required(self) -> frozenset[str]:
        return SCOPES

    def capabilities(self) -> Capabilities:
        return Capabilities(
            # Inventory GET /purchasereceives exists. This is the single
            # largest difference from ERP.
            receives_listable=True,
            bills_delta_filter=True,        # Books bills: last_modified_time filter
            # Purchase orders are read from BOOKS, where the filter exists.
            # Inventory's own PO list has neither filter nor sort; routing the
            # poll there would force a full re-pull and this flag would be
            # False. See INVENTORY_PURCHASE_ORDER_LIST_PARAMS.
            po_delta_filter=True,
            items_delta_filter=True,        # Inventory items: filter AND sort
            line_level_custom_fields=self.line_level_custom_fields,
            daily_call_ceiling=DAILY_CALL_CEILING_BY_PLAN.get(self.plan, 1000),
        )

    # --------------------------------------------------------------- fetching
    def _get(
        self, service: str, path: str, scope: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        query = {"organization_id": self.organization_id}
        query.update(params or {})
        allowed = SORT_COLUMNS.get((service, path))
        sort_column = query.get("sort_column")
        if sort_column is not None and allowed is not None and sort_column not in allowed:
            raise CapabilityError(
                f"{service}{path} does not accept sort_column={sort_column!r}. "
                f"Allowed: {sorted(allowed) or 'none -- this endpoint cannot be sorted'}.")
        if service == SERVICE_INVENTORY and path == PATH_PURCHASE_ORDERS:
            unsupported = set(query) - INVENTORY_PURCHASE_ORDER_LIST_PARAMS
            if unsupported:
                raise CapabilityError(
                    f"Inventory GET /purchaseorders accepts only "
                    f"{sorted(INVENTORY_PURCHASE_ORDER_LIST_PARAMS)}; "
                    f"{sorted(unsupported)} would be ignored rather than "
                    f"rejected, which would make a full scan look like a delta.")
        base = self.books_base if service == SERVICE_BOOKS else self.inventory_base
        return self.transport.request(
            method="GET", base_url=base, path=path, scope=scope, params=query)

    def _source(self, service: str, endpoint: str) -> SourceRef:
        return SourceRef(
            product=PRODUCT, service=service,
            api_version=(BOOKS_API_VERSION if service == SERVICE_BOOKS
                         else INVENTORY_API_VERSION),
            endpoint=endpoint, retrieved_at=datetime.now(timezone.utc))

    # ------------------------------------------------------------------ bills
    def list_bills(self, since: datetime, until: datetime, page: int = 1) -> Page[BillDTO]:
        """Books bills over a modification window.

        As on ERP, the filter is one-sided -- "search bills modified after a
        specific time" -- so ``until`` is enforced here, after the response.
        """
        params = {"page": page, "per_page": self.per_page}
        params.update(window_params(
            enabled=self.capabilities().bills_delta_filter,
            since=since, parameter=DELTA_FILTER_PARAM,
            formatter=_format_books_delta))
        body = self._get(SERVICE_BOOKS, PATH_BILLS, "ZohoBooks.bills.READ", params)
        source = self._source(SERVICE_BOOKS, PATH_BILLS)
        items = tuple(
            bill for bill in
            (_bill(row, source, hydrated=False) for row in body.get("bills", []))
            if bill.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def get_bill(self, external_id: str) -> BillDTO:
        path = PATH_BILL.format(external_id=external_id)
        body = self._get(SERVICE_BOOKS, path, "ZohoBooks.bills.READ")
        return _bill(body.get("bill") or {}, self._source(SERVICE_BOOKS, path),
                     hydrated=True)

    # --------------------------------------------------------- purchase orders
    def list_purchase_orders(
        self, since: datetime, until: datetime, page: int = 1
    ) -> Page[PurchaseOrderDTO]:
        params = {"page": page, "per_page": self.per_page}
        params.update(window_params(
            enabled=self.capabilities().po_delta_filter,
            since=since, parameter=DELTA_FILTER_PARAM,
            formatter=_format_books_delta))
        body = self._get(SERVICE_BOOKS, PATH_PURCHASE_ORDERS,
                         "ZohoBooks.purchaseorders.ALL", params)
        source = self._source(SERVICE_BOOKS, PATH_PURCHASE_ORDERS)
        items = tuple(
            po for po in
            (_purchase_order(row, source, hydrated=False)
             for row in body.get("purchaseorders", []))
            if po.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def get_purchase_order(self, external_id: str) -> PurchaseOrderDTO:
        path = PATH_PURCHASE_ORDER.format(external_id=external_id)
        body = self._get(SERVICE_BOOKS, path, "ZohoBooks.purchaseorders.ALL")
        return _purchase_order(body.get("purchaseorder") or {},
                               self._source(SERVICE_BOOKS, path), hydrated=True)

    def create_purchase_order(self, po: PurchaseOrderDTO, dedupe_key: str) -> str:
        if not dedupe_key:
            raise IntegrationError(
                "A purchase order may not be emitted without a dedupe_key: "
                "Zoho offers no idempotency header on any of the three "
                "products, so this field is the only thing standing between a "
                "retry and a duplicate commitment.")
        body = {
            "vendor_id": po.vendor_external_id,
            "date": po.document_date.isoformat(),
            "currency_code": po.currency_code,
            "line_items": [_outbound_line(line) for line in po.lines],
            "custom_fields": [{"api_name": DEDUPE_CUSTOM_FIELD, "value": dedupe_key}],
        }
        response = self.transport.request(
            method="POST", base_url=self.books_base, path=PATH_PURCHASE_ORDERS,
            scope="ZohoBooks.purchaseorders.ALL",
            params={"organization_id": self.organization_id}, body=body)
        created = (response.get("purchaseorder") or {}).get("purchaseorder_id")
        if not created:
            raise IntegrationError(
                f"Purchase order creation returned no purchaseorder_id: {response!r}")
        return str(created)

    # --------------------------------------------------------------- receives
    def list_receives(self, page: int = 1) -> Page[ReceiveDTO]:
        """Inventory's GRN collection -- the endpoint ERP does not have.

        Sorted descending by ``date`` so the caller can stop early: the
        endpoint takes no ``last_modified_time`` filter, so a descending walk
        with an early stop is the only delta-shaped access it offers. Note the
        sort column is ``date``, the document date, not the modification time
        -- a back-dated edit to an old receive is therefore NOT caught by the
        early stop, which is one of the reasons the nightly completeness sweeps
        are retained rather than replaced by this walk.
        """
        params = {"page": page, "per_page": self.per_page,
                  "sort_column": "date", "sort_order": "D"}
        body = self._get(SERVICE_INVENTORY, PATH_PURCHASE_RECEIVES,
                         "ZohoInventory.purchasereceives.READ", params)
        source = self._source(SERVICE_INVENTORY, PATH_PURCHASE_RECEIVES)
        items = tuple(_receive(row, source, fallback_po=None)
                      for row in body.get("purchasereceives", []))
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]:
        """Receives against one PO.

        Inventory documents no ``purchaseorder_id`` filter on the receives
        list, so this walks the collection and selects locally. That is
        deliberate: sending an undocumented filter parameter would be ignored
        rather than rejected, and the caller would read a full unfiltered page
        as "all the receives for this PO" -- silently attributing another PO's
        goods receipts to this one. Callers wanting the whole population should
        use :func:`~app.backend.integration.adapter.acquire_receives`, which on
        this product takes the list path instead of paying per PO.
        """
        matched: list[ReceiveDTO] = []
        for page in range(1, 51):
            result = self.list_receives(page=page)
            matched.extend(r for r in result.items
                           if r.purchase_order_external_id == po_external_id)
            if not result.has_more:
                break
        return matched


# ================================================================== mapping
def _lines(rows: Sequence[Mapping[str, Any]], *, po_line_key: str) -> tuple[LineDTO, ...]:
    out = []
    for index, row in enumerate(rows, start=1):
        out.append(LineDTO(
            external_line_id=_opt_str(row.get("line_item_id")),
            line_number=int(row.get("line_number") or index),
            description=str(row.get("description") or row.get("name") or ""),
            quantity=quantity(row.get("quantity"), field="line.quantity"),
            unit_price_paise=paise(row.get("rate"), field="line.rate"),
            line_total_paise=paise(row.get("item_total"), field="line.item_total"),
            tax_paise=paise(row.get("tax_total"), field="line.tax_total", allow_missing=True),
            item_external_id=_opt_str(row.get("item_id")),
            # AUD-H-004 again, and the field name differs by service: Books
            # bill lines carry purchaseorder_item_id, Inventory receive lines
            # carry the PO's line_item_id. Documented does not mean populated.
            purchase_order_line_external_id=_opt_str(row.get(po_line_key)),
            dimensions=freeze(row.get("reporting_tags_map")),
            raw=freeze(row),
        ))
    return tuple(out)


def _bill(row: Mapping[str, Any], source: SourceRef, *, hydrated: bool) -> BillDTO:
    return BillDTO(
        source=source,
        external_id=str(row["bill_id"]),
        document_number=str(row.get("bill_number") or ""),
        document_date=parse_zoho_date(row.get("date"), field="bill.date"),
        last_modified=parse_zoho_datetime(
            row.get("last_modified_time"), field="bill.last_modified_time"),
        vendor_external_id=_opt_str(row.get("vendor_id")),
        vendor_name=str(row.get("vendor_name") or ""),
        currency_code=str(row.get("currency_code") or "INR"),
        subtotal_paise=paise(row.get("sub_total"), field="bill.sub_total"),
        tax_paise=paise(row.get("tax_total"), field="bill.tax_total", allow_missing=True),
        total_paise=paise(row.get("total"), field="bill.total"),
        external_status_raw=str(row.get("status") or ""),
        purchase_order_external_ids=tuple(
            str(x) for x in (row.get("purchaseorder_ids") or [])),
        lines=_lines(row.get("line_items") or (), po_line_key="purchaseorder_item_id"),
        lines_hydrated=hydrated,
        raw=freeze(row),
    )


def _purchase_order(row: Mapping[str, Any], source: SourceRef, *, hydrated: bool) -> PurchaseOrderDTO:
    return PurchaseOrderDTO(
        source=source,
        external_id=str(row["purchaseorder_id"]),
        document_number=str(row.get("purchaseorder_number") or ""),
        document_date=parse_zoho_date(row.get("date"), field="po.date"),
        last_modified=parse_zoho_datetime(
            row.get("last_modified_time"), field="po.last_modified_time"),
        vendor_external_id=_opt_str(row.get("vendor_id")),
        vendor_name=str(row.get("vendor_name") or ""),
        currency_code=str(row.get("currency_code") or "INR"),
        subtotal_paise=paise(row.get("sub_total"), field="po.sub_total"),
        tax_paise=paise(row.get("tax_total"), field="po.tax_total", allow_missing=True),
        total_paise=paise(row.get("total"), field="po.total"),
        external_status_raw=str(row.get("status") or ""),
        # Books has no purchase receives module at all, so a Books PO detail
        # never names its receives. The tuple is empty here by fact, not by
        # omission -- receives come from Inventory's own collection.
        receive_external_ids=(),
        lines=_lines(row.get("line_items") or (), po_line_key="line_item_id"),
        lines_hydrated=hydrated,
        dedupe_key=_custom_field(row, DEDUPE_CUSTOM_FIELD),
        raw=freeze(row),
    )


def _receive(row: Mapping[str, Any], source: SourceRef, *, fallback_po: str | None) -> ReceiveDTO:
    return ReceiveDTO(
        source=source,
        external_id=str(row["receive_id"]),
        document_number=str(row.get("receive_number") or ""),
        document_date=parse_zoho_date(row.get("date"), field="receive.date"),
        last_modified=parse_zoho_datetime(
            row.get("last_modified_time"), field="receive.last_modified_time"),
        purchase_order_external_id=_opt_str(row.get("purchaseorder_id")) or fallback_po,
        external_status_raw=str(row.get("status") or ""),
        lines=_lines(row.get("line_items") or (), po_line_key="line_item_id"),
        raw=freeze(row),
    )


def _outbound_line(line: LineDTO) -> dict[str, Any]:
    rupees, sub = divmod(line.unit_price_paise, 100)
    return {
        "item_id": line.item_external_id,
        "description": line.description,
        "quantity": line.quantity,
        "rate": f"{rupees}.{sub:02d}",
    }


def _custom_field(row: Mapping[str, Any], api_name: str) -> str | None:
    for field in row.get("custom_fields") or ():
        if field.get("api_name") == api_name:
            return _opt_str(field.get("value"))
    return None


def _opt_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
