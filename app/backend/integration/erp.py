"""Zoho ERP v3 -- one of the two candidate implementations of seam C1.

**[PROVISIONAL-ERP].** D-14 is unresolved. This module exists as a live
candidate, not as the answer; :mod:`.books_inventory` is its equal. Every
endpoint, scope and host literal for ERP lives here and nowhere else.

The four facts this module exists to encode
-------------------------------------------
1. **Purchase Receives has no list endpoint.** Create, update, delete and
   fetch-one; there is no collection. So :func:`ErpAdapter.receives_for_po` is
   the *only* acquisition path, there is deliberately no ``list_receives``
   method and no receives-collection constant to hardcode, and
   ``Capabilities.receives_listable`` is ``False``. A receive against a PO we
   do not know about is undiscoverable until its bill arrives.
2. **``last_modified_time`` is a filter, not a sort column.** ERP bills and
   POs accept it as a query filter and refuse it as ``sort_column``. So a
   window can be selected but not walked as a stable keyset -- hence the 300 s
   overlap and the retained completeness sweeps. :data:`SORT_COLUMNS` records
   what each endpoint really allows, so nobody re-derives it from Inventory's
   items endpoint, where the same field *is* sortable.
3. **The filter is one-sided.** It selects records modified *after* a time.
   There is no upper bound parameter, so the ``until`` end of a window is
   enforced by us, locally, after the response arrives. Pretending the API
   bounded it is how a poll silently ingests documents from outside its window.
4. **Items live under ``ERP.settings.*``; there is no ``ERP.items`` scope.**
   And ERP's own OAuth scope page omits two scopes the module pages document
   -- ``ERP.purchasereceives.*`` and ``ERP.custommodules.ALL``. The consent
   screen is therefore built from the module pages. :data:`SCOPE_EVIDENCE`
   records which page documents each scope so that defect stays visible and
   re-verifiable at the tenant.

Base URL: ``https://www.zohoapis.in/erp/v3``, India only. Zoho publishes no DC
table for ERP and a ``.com``/``.eu`` ERP host is NOT CONFIRMED, so any other
data centre raises rather than being synthesised from CRM's DC table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.backend.integration.adapter import (
    Capabilities,
    CapabilityError,
    DEDUPE_CUSTOM_FIELD,
    DEDUPE_SCAN_PAGE_LIMIT,
    IntegrationError,
    NoNetworkTransport,
    Transport,
    UnsupportedDataCentre,
    page_context,
    verified_dedupe_match,
    window_params,
)
from app.backend.money import CURRENCY_EXPONENT, minor_exponent_of
from app.backend.integration.dto import (
    BillDTO,
    ContactDTO,
    ItemDTO,
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
    "ACCOUNTS_SERVER",
    "API_HOST_BY_DC",
    "API_VERSION",
    "DAILY_CALL_CEILING_STANDARD",
    "DEDUPE_SEARCH_PARAM",
    "ErpAdapter",
    "PO_STATE_PATHS",
    "PRODUCT",
    "ORGANIZATIONS_PATH",
    "ORGANIZATIONS_SCOPE",
    "SCOPE_EVIDENCE",
    "SERVICE",
    "SERVICE_PATH",
    "SORT_COLUMNS",
    "VALIDATION_PROBES",
    "VERIFIED_LINE_CUSTOM_FIELDS",
    "line_level_custom_fields_for",
]

PRODUCT = "ERP"
SERVICE = "erp"
API_VERSION = "v3"
SERVICE_PATH = "/erp/v3"

#: India only. Zoho publishes no DC table for ERP; every other host is
#: NOT CONFIRMED and must not be borrowed from CRM's multi-DC documentation.
API_HOST_BY_DC: Mapping[str, str] = {"IN": "https://www.zohoapis.in"}
ACCOUNTS_SERVER = "https://accounts.zoho.in"

#: §11.1. India offers ERP Standard and Premium only. Standard's 2,000/day --
#: not the 100/min all three products share -- is the binding constraint on a
#: PO-anchored GRN sweep, whose cost scales with open-PO count.
DAILY_CALL_CEILING_STANDARD = 2000
DAILY_CALL_CEILING_PREMIUM = 10000

# ------------------------------------------------------------------ endpoints
PATH_BILLS = "/bills"
PATH_BILL = "/bills/{external_id}"
PATH_PURCHASE_ORDERS = "/purchaseorders"
PATH_PURCHASE_ORDER = "/purchaseorders/{external_id}"
#: Detail only. There is NO ``PATH_PURCHASE_RECEIVES`` collection constant and
#: adding one would be a fabrication: ERP Purchase Receives has four endpoints
#: and none of them enumerates.
PATH_PURCHASE_RECEIVE = "/purchasereceives/{external_id}"
PATH_CONTACTS = "/contacts"
PATH_ITEMS = "/items"

#: Update one purchase order by id. Vendor-authoritative: Zoho's own published
#: ERP OpenAPI bundle, recorded with its digest in
#: ``research/20_verified/openapi_registry.json``, documents
#: ``PUT /purchaseorders/{purchaseorder_id}``.
#:
#: That bundle ALSO documents ``PUT /purchaseorders`` -- "update a purchase
#: order using a custom field's unique value" -- driven by the
#: ``X-Unique-Identifier-Key`` / ``X-Unique-Identifier-Value`` headers, with an
#: optional ``X-Upsert``. That is section 11.6's mechanism in a single call, and
#: it is deliberately NOT used here: the Transport seam carries no headers, and
#: widening it belongs to the stream that owns it, not to this one.
#: Resolve-then-update-by-id reaches the same outcome with the seam unchanged.
#: Recorded so the better call is not lost.
PATH_PURCHASE_ORDER_UPDATE = "/purchaseorders/{external_id}"

#: Section 11.7: a PO is emitted draft and moved to open **by our own call**,
#: after our approval instance closes. Only the transitions we actually perform
#: are listed; ``billed`` and ``cancelled`` exist in the bundle but are the
#: tenant's to make, and a Zoho-side status change we did not initiate is an
#: exception rather than an outcome.
PO_STATE_PATHS: Mapping[str, str] = {
    "open": "/purchaseorders/{external_id}/status/open",
}

#: Search the purchase-order list by a custom field's value. Documented in the
#: ERP bundle on ``GET /purchaseorders`` (variants ``custom_field_startswith``,
#: ``custom_field_contains``).
#:
#: The bundle documents the parameter but not the exact encoding of its value,
#: so the ``api_name:value`` form built below is **our reading, not a
#: quotation**. That uncertainty is survivable precisely because nothing trusts
#: it: ``verified_dedupe_match`` re-reads the dedupe custom field off each
#: returned row, so a filter that is misencoded -- and therefore ignored --
#: degrades to a scan rather than to a wrong answer.
#: VERIFIED LIVE (Fable 5.1, 2026-09-11, DEMO WBS 60074128927): the documented
#: ``custom_field=cf_capex_ref:<value>`` form is IGNORED by Zoho ERP v3 -- it
#: answered the unfiltered list of every purchase order -- while the field's
#: own api_name as the parameter (``cf_capex_ref=<value>``) answered exactly
#: the one order carrying the value. The parameter IS the api_name; the value
#: is the bare key.
DEDUPE_SEARCH_PARAM = DEDUPE_CUSTOM_FIELD

#: D-7 (line-level custom fields on purchase-order lines), resolved per
#: TENANT and never per product. The product owner decided on 2026-09-11 to
#: carry the WBS code and the budget head on the line, and the two fields
#: were created on DEMO WBS the same day (ids ``3912780000000098001`` and
#: ``3912780000000099001``, entity ``purchaseorder`` line items). An
#: organisation absent from this table gets the pessimistic default, because
#: assuming a capability present for a tenant nobody has looked at is how a
#: design gets built on a feature that tenant does not have.
VERIFIED_LINE_CUSTOM_FIELDS: Mapping[str, tuple[str, ...]] = {
    "60074128927": ("cf_wbs_code", "cf_budget_head"),
}


def line_level_custom_fields_for(organization_id: str) -> bool:
    """True only for a tenant whose line fields were verified (see above)."""
    return str(organization_id) in VERIFIED_LINE_CUSTOM_FIELDS


#: The organisation-discovery call (SCR-33): the one call every other call
#: depends on. Fable 5.1 (2026-09-12), verified live against DEMO WBS.
ORGANIZATIONS_PATH = "/organizations"
ORGANIZATIONS_SCOPE = "ERP.settings.READ"

#: SCR-34 validation probes: one one-row collection read per required scope
#: whose module HAS a collection. Purchase receives have none (receives are
#: read per purchase order -- see ``receives_for_po``) and custom modules are
#: not probed blind; both are absent here on purpose and the validate route
#: reports them from the grant alone.
VALIDATION_PROBES: Mapping[str, str] = {
    "ERP.settings.READ": ORGANIZATIONS_PATH,
    "ERP.contacts.READ": "/contacts",
    "ERP.purchaseorders.ALL": "/purchaseorders",
    "ERP.bills.READ": "/bills",
}

#: What each list endpoint will actually accept as ``sort_column``.
#: ``last_modified_time`` is absent from bills and purchase orders on purpose:
#: it is filterable there and not sortable, and conflating the two is how a
#: resumable keyset walk gets designed for an API that cannot support one.
SORT_COLUMNS: Mapping[str, frozenset[str]] = {
    PATH_BILLS: frozenset(
        {"vendor_name", "bill_number", "date", "due_date", "total", "balance",
         "created_time"}),
    PATH_PURCHASE_ORDERS: frozenset(
        {"vendor_name", "purchaseorder_number", "date", "total", "created_time"}),
    PATH_CONTACTS: frozenset({"contact_name", "created_time", "last_modified_time"}),
    PATH_ITEMS: frozenset({"name", "rate", "created_time"}),
}

#: Which endpoints accept ``last_modified_time`` as a *filter*.
DELTA_FILTERABLE: frozenset[str] = frozenset({PATH_BILLS, PATH_PURCHASE_ORDERS})

#: The filter's value format. ERP/Books take an offset-bearing stamp; a ``+``
#: offset must reach the wire percent-encoded as ``%2B``.
DELTA_FILTER_PARAM = "last_modified_time"
DELTA_FILTER_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


@dataclass(frozen=True)
class ScopeEvidence:
    """One OAuth scope and the Zoho page that documents it.

    ``documented_on_oauth_scope_page`` is ``False`` for exactly two scopes,
    and that is a defect in Zoho's own documentation rather than in ours. It
    is recorded rather than smoothed over because the consequence is
    operational: a consent screen built from the OAuth scope table would omit
    them, and the integration would fail at runtime on the two modules that
    matter most.
    """
    scope: str
    purpose: str
    documented_on_oauth_scope_page: bool
    documented_on_module_page: bool


SCOPE_EVIDENCE: tuple[ScopeEvidence, ...] = (
    ScopeEvidence("ERP.purchaseorders.ALL",
                  "Read POs for reconciliation; create the POs we emit.",
                  True, True),
    ScopeEvidence("ERP.bills.READ",
                  "Actual CWIP.",
                  True, True),
    ScopeEvidence("ERP.purchasereceives.READ",
                  "Fetch one GRN by id -- the only receives operation ERP offers "
                  "us, since there is no collection endpoint.",
                  False, True),      # <- omitted from Zoho's OAuth scope table
    ScopeEvidence("ERP.contacts.READ",
                  "Vendor master.",
                  True, True),
    ScopeEvidence("ERP.settings.READ",
                  "Item master. There is no ERP.items scope: ERP files items "
                  "under settings, unlike Inventory.",
                  True, True),
    ScopeEvidence("ERP.custommodules.ALL",
                  "Carrier for the cf_capex_ref dedupe field and the custom "
                  "module PR fallback.",
                  False, True),      # <- omitted from Zoho's OAuth scope table
)


def _format_delta(moment: datetime) -> str:
    """``2026-08-27T00:00:00+0530``. Naive input is declared UTC, not assumed."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime(DELTA_FILTER_FORMAT)


class ErpAdapter:
    """Zoho ERP v3.

    Note the shape of this class as much as its contents: it has
    ``receives_for_po`` and no ``list_receives``, because the product has a
    detail endpoint and no collection. The absent method is the capability
    gap made structural -- a caller cannot reach a list path here even by
    mistake, because there is nothing to call and no constant naming one.
    """

    product = PRODUCT

    def __init__(
        self,
        *,
        organization_id: str,
        dc: str = "IN",
        transport: Transport | None = None,
        per_page: int = 50,
        plan_daily_ceiling: int = DAILY_CALL_CEILING_STANDARD,
        line_level_custom_fields: bool | None = None,
    ) -> None:
        self.organization_id = str(organization_id)
        self.dc = dc
        self.per_page = per_page
        self.plan_daily_ceiling = plan_daily_ceiling
        # D-7 is a TENANT fact. ``None`` (the default) asks the verified-tenant
        # table; an unverified tenant is assumed absent rather than present,
        # because assuming it present would let line-level CAPEX dimensions be
        # designed against a feature that tenant may not have. An explicit
        # bool still wins, so a test can drive either branch on any org.
        self.line_level_custom_fields = (
            line_level_custom_fields_for(self.organization_id)
            if line_level_custom_fields is None else bool(line_level_custom_fields))
        self.transport = transport or NoNetworkTransport(PRODUCT)
        if getattr(self.transport, "product", None) != PRODUCT:
            raise IntegrationError(
                f"An ERP adapter was given a {getattr(self.transport, 'product', None)!r} "
                f"transport. Products are never mixed.")
        self.base = self.base_url(dc)

    # -------------------------------------------------------------- metadata
    def base_url(self, dc: str) -> str:
        host = API_HOST_BY_DC.get(dc.upper())
        if host is None:
            raise UnsupportedDataCentre(
                f"Zoho publishes no ERP host for data centre {dc!r}. Only IN "
                f"({API_HOST_BY_DC['IN']}{SERVICE_PATH}) is documented; a "
                f".com/.eu ERP host is NOT CONFIRMED and must not be inferred "
                f"from Zoho CRM's multi-DC table.")
        return f"{host}{SERVICE_PATH}"

    def accounts_server(self, dc: str) -> str:
        if dc.upper() != "IN":
            raise UnsupportedDataCentre(
                f"Only {ACCOUNTS_SERVER} is documented as the ERP accounts "
                f"server; {dc!r} is NOT CONFIRMED.")
        return ACCOUNTS_SERVER

    def scopes_required(self) -> frozenset[str]:
        """Built from the module pages, not from the OAuth scope table.

        Zoho's ERP OAuth page omits ``ERP.purchasereceives.*`` and
        ``ERP.custommodules.ALL``, which the module pages document. Building
        the consent list from the scope table would silently drop both.
        """
        return frozenset(e.scope for e in SCOPE_EVIDENCE)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            receives_listable=False,        # §11.4 -- no collection endpoint
            bills_delta_filter=True,        # last_modified_time filter
            po_delta_filter=True,           # last_modified_time filter
            items_delta_filter=False,       # ERP items: no filter -> weekly full refresh
            line_level_custom_fields=self.line_level_custom_fields,
            daily_call_ceiling=self.plan_daily_ceiling,
            # Zoho's own ERP bundle documents a `custom_field` search on
            # GET /purchaseorders, so a lost response costs one call to
            # recover here rather than a scan.
            po_dedupe_search=True,
        )

    # --------------------------------------------------------------- fetching
    def _get(self, path: str, scope: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        query = {"organization_id": self.organization_id}
        query.update(params or {})
        sort_column = query.get("sort_column")
        allowed = SORT_COLUMNS.get(path)
        if sort_column is not None and allowed is not None and sort_column not in allowed:
            raise CapabilityError(
                f"{path} does not accept sort_column={sort_column!r}. Allowed: "
                f"{sorted(allowed)}. On ERP bills and purchase orders "
                f"last_modified_time is filterable and NOT sortable, so no "
                f"stable keyset walk on modification time exists.")
        return self.transport.request(
            method="GET", base_url=self.base, path=path, scope=scope, params=query)

    def _source(self, endpoint: str) -> SourceRef:
        return SourceRef(
            product=PRODUCT, service=SERVICE, api_version=API_VERSION,
            endpoint=endpoint, retrieved_at=datetime.now(timezone.utc))

    # ------------------------------------------------------------------ bills
    def list_bills(self, since: datetime, until: datetime, page: int = 1) -> Page[BillDTO]:
        """A modification window of bills.

        The ``until`` bound is applied **here**, after the response arrives.
        Zoho's ``last_modified_time`` filter is one-sided -- "search bills
        modified after a specific time" -- so there is no upper-bound
        parameter to send. Filtering locally is the honest implementation;
        sending a fabricated ``last_modified_time_end`` would be worse than
        useless, because an unrecognised query parameter is ignored rather
        than rejected and the window would silently be unbounded.
        """
        params = {"page": page, "per_page": self.per_page}
        params.update(window_params(
            enabled=self.capabilities().bills_delta_filter,
            since=since, parameter=DELTA_FILTER_PARAM, formatter=_format_delta))
        body = self._get(PATH_BILLS, "ERP.bills.READ", params)
        source = self._source(PATH_BILLS)
        items = tuple(
            bill for bill in
            (_bill(row, source, hydrated=False) for row in body.get("bills", []))
            if bill.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def get_bill(self, external_id: str) -> BillDTO:
        """One bill, with its lines.

        This is ``sweep_bill_detail``'s call. List responses omit
        ``line_items`` on all three products, so a bill is not usable for CWIP
        until it has been through here -- which is why :class:`BillDTO` carries
        ``lines_hydrated`` rather than letting an empty tuple mean two things.
        """
        path = PATH_BILL.format(external_id=external_id)
        body = self._get(path, "ERP.bills.READ")
        return _bill(body.get("bill") or {}, self._source(path), hydrated=True)

    # --------------------------------------------------------- purchase orders
    def list_purchase_orders(
        self, since: datetime, until: datetime, page: int = 1
    ) -> Page[PurchaseOrderDTO]:
        params = {"page": page, "per_page": self.per_page}
        params.update(window_params(
            enabled=self.capabilities().po_delta_filter,
            since=since, parameter=DELTA_FILTER_PARAM, formatter=_format_delta))
        body = self._get(PATH_PURCHASE_ORDERS, "ERP.purchaseorders.ALL", params)
        source = self._source(PATH_PURCHASE_ORDERS)
        items = tuple(
            po for po in
            (_purchase_order(row, source, hydrated=False)
             for row in body.get("purchaseorders", []))
            if po.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def get_purchase_order(self, external_id: str) -> PurchaseOrderDTO:
        path = PATH_PURCHASE_ORDER.format(external_id=external_id)
        body = self._get(path, "ERP.purchaseorders.ALL")
        return _purchase_order(
            body.get("purchaseorder") or {}, self._source(path), hydrated=True)

    def create_purchase_order(self, po: PurchaseOrderDTO, dedupe_key: str) -> str:
        """Emit a PO, carrying our synthesised idempotency key.

        Zoho documents no idempotency header on any of the three products
        (§11.6), so the key is written to the unique custom field
        ``cf_capex_ref`` (Z-01) and a retry after an unrecorded send becomes an
        update-by-unique-custom-field rather than a duplicate commitment. The
        PO is created in Zoho's default draft state and is transitioned to open
        only by our own later call, after our approval instance closes -- so
        nothing here sets a status, and a Zoho-side status change we did not
        initiate is an exception rather than an outcome.
        """
        if not dedupe_key:
            raise IntegrationError(
                "A purchase order may not be emitted without a dedupe_key: "
                "Zoho offers no idempotency header, so this field is the only "
                "thing standing between a retry and a duplicate commitment.")
        body = _emission_body(po, dedupe_key)
        response = self.transport.request(
            method="POST", base_url=self.base, path=PATH_PURCHASE_ORDERS,
            scope="ERP.purchaseorders.ALL",
            params={"organization_id": self.organization_id}, body=body)
        created = (response.get("purchaseorder") or {}).get("purchaseorder_id")
        if not created:
            raise IntegrationError(
                f"Purchase order creation returned no purchaseorder_id: {response!r}")
        return str(created)

    # ------------------------------------------------------------ master data
    def list_items(self, since: datetime, until: datetime, page: int = 1) -> Page[ItemDTO]:
        """The item master. **A full refresh, not a delta.**

        Plan section 11.3 is explicit: on ERP and Books ``GET /items`` has no
        ``last_modified_time`` filter, which is why ``items_delta_filter`` is
        ``False`` here and why the plan schedules a *weekly full refresh*
        rather than the 15-minute delta Inventory gets.

        So ``since`` is accepted and deliberately not sent. It is kept in the
        signature because the seam is shared with ``list_bills`` and the poll
        calls all four the same way -- and because dropping the parameter
        would hide the asymmetry rather than state it. ``until`` is still
        applied locally to records that carry a timestamp, so a caller asking
        for a window never receives records from beyond it; records with no
        timestamp at all are kept, because a full refresh that silently
        discarded undated master data would under-report the item master.
        """
        params = {"page": page, "per_page": self.per_page}
        params.update(window_params(
            enabled=self.capabilities().items_delta_filter,
            since=since, parameter=DELTA_FILTER_PARAM, formatter=_format_delta))
        body = self._get(PATH_ITEMS, "ERP.settings.READ", params)
        source = self._source(PATH_ITEMS)
        items = tuple(
            item for item in
            (_item(row, source) for row in body.get("items", []))
            if item.last_modified is None or item.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=items, page=got_page, per_page=per_page, has_more=has_more)

    def list_contacts(
        self, since: datetime, until: datetime, page: int = 1
    ) -> Page[ContactDTO]:
        """The vendor master. Sort-only, so again a full refresh.

        ``GET /contacts`` is the opposite problem to bills: section 11.3 records
        ``last_modified_time`` as an allowed *sort column* here and not as a
        filter, on all three products. There is deliberately no
        ``contacts_delta_filter`` in ``Capabilities`` to read -- naming a
        capability that does not exist would be a lie that reads as a bug -- so
        no window is sent and the poll performs the full refresh the plan
        prescribes.

        The sort IS requested, descending, because it is documented here and
        it lets a caller stop early. It is checked against ``SORT_COLUMNS``
        like every other sort on this adapter.
        """
        params = {"page": page, "per_page": self.per_page,
                  "sort_column": DELTA_FILTER_PARAM, "sort_order": "D"}
        body = self._get(PATH_CONTACTS, "ERP.contacts.READ", params)
        source = self._source(PATH_CONTACTS)
        contacts = tuple(
            contact for contact in
            (_contact(row, source) for row in body.get("contacts", []))
            if contact.last_modified is None or contact.last_modified <= until)
        got_page, per_page, has_more = page_context(body, page=page, per_page=self.per_page)
        return Page(items=contacts, page=got_page, per_page=per_page, has_more=has_more)

    # ------------------------------------------------------- lost-response recovery
    def resolve_by_dedupe_key(self, dedupe_key: str) -> str | None:
        """The id of the purchase order carrying ``dedupe_key``, or ``None``.

        This is the call C1 was missing, and the reason a lost response used to
        cost a link. Zoho documents no idempotency header (section 11.6), so
        ``cf_capex_ref`` is the only handle on a purchase order we may or may
        not have created; without a way to read a record back by it, the PO
        existed in the tenant and could never be named.

        Two mechanisms, and the second is what makes the first safe to trust:

        1. the documented ``custom_field`` search, which should return the one
           matching purchase order in a single call;
        2. ``verified_dedupe_match``, which re-reads ``cf_capex_ref`` off every
           returned row and returns an id only on **exact** equality.

        If the search returns nothing verifiable -- because the value encoding
        is wrong and the parameter was ignored, because the field is not
        configured unique in this tenant, or because the PO genuinely is not
        there -- this falls back to a **bounded** scan of the list and then
        gives up. ``None`` means "not found within the budget", never "does not
        exist": the caller must not read it as licence to create a second
        commitment, and ``emit_purchase_order`` does not.
        """
        if not dedupe_key:
            raise IntegrationError(
                "resolve_by_dedupe_key() requires a dedupe_key. Resolving on an "
                "empty key would match whatever the tenant returned first.")

        filtered = self._get(PATH_PURCHASE_ORDERS, "ERP.purchaseorders.ALL", {
            "page": 1, "per_page": self.per_page,
            DEDUPE_SEARCH_PARAM: dedupe_key})
        found = verified_dedupe_match(
            filtered.get("purchaseorders") or (),
            dedupe_key=dedupe_key, id_field="purchaseorder_id")
        if found:
            return found

        for page in range(1, DEDUPE_SCAN_PAGE_LIMIT + 1):
            body = self._get(PATH_PURCHASE_ORDERS, "ERP.purchaseorders.ALL",
                             {"page": page, "per_page": self.per_page})
            rows = body.get("purchaseorders") or ()
            found = verified_dedupe_match(
                rows, dedupe_key=dedupe_key, id_field="purchaseorder_id")
            if found:
                return found
            if not page_context(body, page=page, per_page=self.per_page)[2]:
                return None
        return None

    def update_purchase_order(self, external_id: str, payload: Any,
                              dedupe_key: str) -> str:
        """Apply the current draft onto a purchase order we have adopted.

        Called only after ``resolve_by_dedupe_key`` (or the tenant's own
        duplicate error) has named the record, so this updates by id. The
        dedupe key is re-sent in ``custom_fields``: it is already on the record
        by definition, and re-sending it means an update can never be the thing
        that strips the only handle we have on the document.
        """
        if not external_id:
            raise IntegrationError(
                "update_purchase_order() requires an external_id; adopting an "
                "unnamed record is exactly what resolve_by_dedupe_key exists "
                "to prevent.")
        if not dedupe_key:
            raise IntegrationError(
                "update_purchase_order() requires the dedupe_key, so an update "
                "cannot strip the field the retry path depends on.")
        body = _emission_body(payload, dedupe_key)
        path = PATH_PURCHASE_ORDER_UPDATE.format(external_id=external_id)
        response = self.transport.request(
            method="PUT", base_url=self.base, path=path,
            scope="ERP.purchaseorders.ALL",
            params={"organization_id": self.organization_id}, body=body)
        updated = (response.get("purchaseorder") or {}).get("purchaseorder_id")
        return str(updated or external_id)

    def transition_purchase_order(self, external_id: str, state: str,
                                  actor: str) -> str:
        """Section 11.7's draft-to-open step, performed by our own call.

        The state is looked up in ``PO_STATE_PATHS`` rather than interpolated,
        so an unsupported transition is a refusal here and not a POST to a
        synthesised path. ``actor`` is not sent -- Zoho attributes the change
        to the OAuth identity and there is no field to carry ours -- it is
        required so the caller records who asked for it in our own audit trail.
        """
        if not actor:
            raise IntegrationError(
                "transition_purchase_order() requires an actor: a commitment "
                "moving to open is an approved act and must be attributable in "
                "our audit trail, whatever Zoho attributes it to.")
        template = PO_STATE_PATHS.get(state)
        if template is None:
            raise CapabilityError(
                f"This adapter performs only {sorted(PO_STATE_PATHS)} "
                f"transitions, not {state!r}. Other statuses exist on the "
                f"product but are the tenant's to set; a Zoho-side status "
                f"change we did not initiate is an exception, not an outcome.")
        path = template.format(external_id=external_id)
        self.transport.request(
            method="POST", base_url=self.base, path=path,
            scope="ERP.purchaseorders.ALL",
            params={"organization_id": self.organization_id}, body={})
        return state

    # --------------------------------------------------------------- receives
    #
    # There is deliberately no list_receives() here. See the module docstring
    # and adapter.acquire_receives: on ERP the absence is the capability.

    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]:
        """PO-anchored discovery -- the sole GRN mechanism on ERP (§11.4).

        Two levels, and both are unavoidable: the PO detail names its receives,
        and each receive must then be fetched individually because there is no
        endpoint that returns more than one. Cost is therefore
        ``1 + len(receives)`` calls per open PO, against 2,000 calls/day on ERP
        Standard -- which is why §11.4 says GRN sync frequency may have to be
        negotiated down with the client rather than engineered around.
        """
        po = self.get_purchase_order(po_external_id)
        out: list[ReceiveDTO] = []
        for receive_id in po.receive_external_ids:
            path = PATH_PURCHASE_RECEIVE.format(external_id=receive_id)
            body = self._get(path, "ERP.purchasereceives.READ")
            out.append(_receive(
                body.get("purchasereceive") or {}, self._source(path),
                fallback_po=po_external_id))
        return out


# ================================================================== mapping
def _lines(rows: Sequence[Mapping[str, Any]], *, po_line_key: str,
           minor_exponent: int = CURRENCY_EXPONENT) -> tuple[LineDTO, ...]:
    out = []
    for index, row in enumerate(rows, start=1):
        out.append(LineDTO(
            external_line_id=_opt_str(row.get("line_item_id")),
            line_number=int(row.get("line_number") or index),
            description=str(row.get("description") or row.get("name") or ""),
            quantity=quantity(row.get("quantity"), field="line.quantity"),
            unit_price_paise=paise(row.get("rate"), field="line.rate",
                                  minor_exponent=minor_exponent),
            line_total_paise=paise(row.get("item_total"), field="line.item_total",
                                  minor_exponent=minor_exponent),
            tax_paise=paise(row.get("tax_total"), field="line.tax_total",
                            allow_missing=True, minor_exponent=minor_exponent),
            item_external_id=_opt_str(row.get("item_id")),
            # AUD-H-004. Documented does not mean populated: when the source
            # omits the linkage this stays None and becomes a
            # GRN_LINE_UNATTRIBUTED exception upstream. It is never inferred
            # from position, and never spread pro-rata.
            purchase_order_line_external_id=_opt_str(row.get(po_line_key)),
            dimensions=freeze(row.get("reporting_tags_map")),
            raw=freeze(row),
        ))
    return tuple(out)


def _header_subtotal(row: Mapping[str, Any], *, field: str, hydrated: bool, exp: int) -> int:
    """`sub_total` where the row carries it; on a LIST row that does not, the
    stated `total`.

    VERIFIED LIVE (Fable 5.1, 2026-09-11, DEMO WBS): the ERP v3 list rows of
    `/bills` and `/purchaseorders` carry `total` and NO `sub_total` or
    `tax_total`; the detail rows carry all three. The cassettes assumed the
    detail shape on the list, and the first live page refused every document
    with `po.sub_total is missing`. A list row is discovery only -- it is
    marked `lines_hydrated=False` and nothing books it before the detail
    fetch -- so its subtotal is taken as the total the row DOES state (tax on
    such a row is already read as 0 by `allow_missing`). A HYDRATED row still
    requires `sub_total`: a detail fetch that lacks it is a contract change
    and is refused, not papered over.
    """
    if row.get("sub_total") not in (None, "") or hydrated:
        return paise(row.get("sub_total"), field=f"{field}.sub_total", minor_exponent=exp)
    return paise(row.get("total"), field=f"{field}.total", minor_exponent=exp)


def _bill(row: Mapping[str, Any], source: SourceRef, *, hydrated: bool) -> BillDTO:
    # THE DOCUMENT'S OWN SCALE, resolved once and used for both the
    # header and the lines. `to_paise` multiplied by 100 regardless,
    # so a JPY document (exponent 0) was booked a hundred times too
    # high and a KWD one (exponent 3) ten times too low -- and both
    # are seeded as supported in migration 023.
    _exp = minor_exponent_of(str(row.get("currency_code") or "INR"))
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
        subtotal_paise=_header_subtotal(row, field="bill", hydrated=hydrated, exp=_exp),
        tax_paise=paise(row.get("tax_total"), field="bill.tax_total", allow_missing=True, minor_exponent=_exp),
        total_paise=paise(row.get("total"), field="bill.total", minor_exponent=_exp),
        # Stored verbatim and never overwritten (C3/§8.4). An unmapped raw
        # value raises UNMAPPED_EXTERNAL_STATUS upstream; it is never guessed.
        external_status_raw=str(row.get("status") or ""),
        purchase_order_external_ids=tuple(
            str(x) for x in (row.get("purchaseorder_ids") or [])),
        lines=_lines(row.get("line_items") or (), po_line_key="purchaseorder_item_id", minor_exponent=_exp),
        lines_hydrated=hydrated,
        raw=freeze(row),
    )


def _purchase_order(row: Mapping[str, Any], source: SourceRef, *, hydrated: bool) -> PurchaseOrderDTO:
    # THE DOCUMENT'S OWN SCALE, resolved once and used for both the
    # header and the lines. `to_paise` multiplied by 100 regardless,
    # so a JPY document (exponent 0) was booked a hundred times too
    # high and a KWD one (exponent 3) ten times too low -- and both
    # are seeded as supported in migration 023.
    _exp = minor_exponent_of(str(row.get("currency_code") or "INR"))
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
        subtotal_paise=_header_subtotal(row, field="po", hydrated=hydrated, exp=_exp),
        tax_paise=paise(row.get("tax_total"), field="po.tax_total", allow_missing=True, minor_exponent=_exp),
        total_paise=paise(row.get("total"), field="po.total", minor_exponent=_exp),
        external_status_raw=str(row.get("status") or ""),
        receive_external_ids=tuple(
            str(r.get("receive_id")) for r in (row.get("purchasereceives") or [])
            if r.get("receive_id")),
        lines=_lines(row.get("line_items") or (), po_line_key="line_item_id", minor_exponent=_exp),
        lines_hydrated=hydrated,
        dedupe_key=_custom_field(row, DEDUPE_CUSTOM_FIELD),
        raw=freeze(row),
    )


def _receive(row: Mapping[str, Any], source: SourceRef, *, fallback_po: str) -> ReceiveDTO:
    return ReceiveDTO(
        source=source,
        external_id=str(row["receive_id"]),
        document_number=str(row.get("receive_number") or ""),
        document_date=parse_zoho_date(row.get("date"), field="receive.date"),
        last_modified=parse_zoho_datetime(
            row.get("last_modified_time"), field="receive.last_modified_time"),
        purchase_order_external_id=_opt_str(row.get("purchaseorder_id")) or fallback_po,
        external_status_raw=str(row.get("status") or ""),
        lines=# NO EXPONENT, because there is nothing to resolve one from.
        # `ReceiveDTO` is the only inbound money-document DTO with no
        # `currency_code` -- a reported contract gap -- so a receive is
        # parsed at the base scale. A foreign-currency receipt is
        # therefore booked at face value into `received_paise`, which is
        # that gap's consequence and not something to paper over by
        # inventing a currency the contract does not carry.
        _lines(row.get("line_items") or (), po_line_key="line_item_id"),
        raw=freeze(row),
    )


def _item(row: Mapping[str, Any], source: SourceRef) -> ItemDTO:
    return ItemDTO(
        source=source,
        external_id=str(row["item_id"]),
        name=str(row.get("name") or ""),
        external_status_raw=str(row.get("status") or ""),
        last_modified=_opt_datetime(row.get("last_modified_time"),
                                    field="item.last_modified_time"),
        sku=_opt_str(row.get("sku")),
        description=str(row.get("description") or ""),
        rate_paise=paise(row.get("rate"), field="item.rate", allow_missing=True),
        currency_code=str(row.get("currency_code") or "INR"),
        item_type=_opt_str(row.get("item_type") or row.get("product_type")),
        raw=freeze(row),
    )


def _contact(row: Mapping[str, Any], source: SourceRef) -> ContactDTO:
    return ContactDTO(
        source=source,
        external_id=str(row["contact_id"]),
        contact_name=str(row.get("contact_name") or ""),
        external_status_raw=str(row.get("status") or ""),
        company_name=str(row.get("company_name") or ""),
        # Kept as the source sent it. Deciding what counts as a vendor is the
        # platform's business rule, not this module's.
        contact_type=_opt_str(row.get("contact_type")),
        last_modified=_opt_datetime(row.get("last_modified_time"),
                                    field="contact.last_modified_time"),
        email=_opt_str(row.get("email")),
        currency_code=str(row.get("currency_code") or "INR"),
        raw=freeze(row),
    )


def _emission_body(po: Any, dedupe_key: str) -> dict[str, Any]:
    """The wire body for a create or an update, built once.

    Shared so a retry that updates cannot drift from the create it is standing
    in for -- if the two built different bodies, an adopted purchase order
    would end up holding different values from the one we thought we sent.
    """
    return {
        "vendor_id": po.vendor_external_id,
        "date": po.document_date.isoformat(),
        "currency_code": po.currency_code,
        # Rendered at the ORDER'S currency exponent: a JPY line has no
        # decimals and a KWD line has three. The DTO's amounts are minor
        # units of `po.currency_code`, and 100 is only right for two of them.
        "line_items": [_outbound_line(line, minor_exponent=minor_exponent_of(po.currency_code))
                       for line in po.lines],
        "custom_fields": [{"api_name": DEDUPE_CUSTOM_FIELD, "value": dedupe_key}],
    }


def _opt_datetime(value: Any, *, field: str) -> datetime | None:
    """A timestamp, or None when the source omitted it.

    Master-data endpoints on this product carry no delta filter, so a row may
    legitimately arrive with no modification time. That is not a mapping
    failure and must not be turned into one -- nor into "now", which would let
    a watermark advance past records nobody read.
    """
    if value is None or value == "":
        return None
    return parse_zoho_datetime(value, field=field)


def render_paise(paise: int, *, minor_exponent: int = CURRENCY_EXPONENT) -> str:
    """Integer paise -> the decimal string that goes on the wire.

    Amounts leave as decimal strings built from integer paise. There is no
    float here and no ``Decimal``: the value is assembled by integer division
    so the string is exactly what the ledger holds. That claim used to be
    false for every negative amount, and this function is where it becomes
    true.

    THE DEFECT THIS REPLACES
    ------------------------
    The previous rendering was ``divmod(paise, 100)`` straight into
    ``f"{rupees}.{sub:02d}"``. Python's ``divmod`` **floors** -- it rounds
    toward negative infinity and returns a non-negative remainder -- so
    ``divmod(-150, 100)`` is ``(-2, 50)`` and the wire carried ``-2.50`` for
    an amount of minus one rupee fifty::

           paise   true rupees   emitted    verdict
            -150        -1.50      -2.50    WRONG
             -99        -0.99      -1.01    WRONG
              -1        -0.01      -1.99    WRONG
          -25000      -250.00    -250.00    ok

    The last row is why this survived review and every existing test: an exact
    multiple of 100 has a zero remainder, and flooring a value with no
    fractional part is the identity. Every fixture amount was a round rupee,
    so the only cases that could expose it were the only cases nobody wrote.

    Negatives are not a hypothetical. ``dto.paise()`` calls
    ``to_paise(..., allow_negative=True)`` deliberately, because **credit
    notes, returns, reversals and negative adjustments** are ordinary
    documents on both products, and a debit-note line is exactly a negative
    unit price.

    THE FIX
    -------
    Split the sign off FIRST, divide the magnitude, and put the sign back.
    ``abs()`` on an ``int`` is exact and total, so ``rupees`` and ``sub`` are
    always the true whole and fractional parts of the magnitude and the
    ``:02d`` pad can never be reading a borrowed remainder.

    Note the sign is taken from ``paise < 0`` rather than from ``rupees``:
    for ``-1 <= paise <= -99`` the magnitude's rupee part is ``0``, and
    ``-0`` does not exist for an ``int``, so a sign derived from the quotient
    would render ``-0.99`` as ``0.99`` -- the same money lost, in the other
    direction.

    THE EXPONENT (Fable 5.1)
    ------------------------
    ``paise`` is minor units of the document's ``currency_code`` and
    ``minor_exponent`` is how many decimal places that currency has -- 2 for
    INR/USD/EUR, 0 for JPY, 3 for KWD (``money.minor_exponent_of``). Fixed
    ``100`` here sent a JPY 1,234 line as ``12.34`` and a KWD line ten times
    too high; the divisor is now ``10 ** minor_exponent`` and the fraction is
    zero-padded to exactly that many digits, so an exponent of 0 renders with
    no decimal point at all. The default is INR's two, so every existing
    caller and every row of the twin-agreement table is unchanged.
    """
    exponent = int(minor_exponent)
    if isinstance(minor_exponent, bool) or not (0 <= exponent <= 4):
        raise ValueError(
            f"minor_exponent must be 0..4 (ISO 4217 uses 0, 2 and 3); got "
            f"{minor_exponent!r}.")
    sign = "-" if paise < 0 else ""
    units, sub = divmod(abs(paise), 10 ** exponent)
    if exponent == 0:
        return f"{sign}{units}"
    return f"{sign}{units}.{sub:0{exponent}d}"


def _outbound_line(line: LineDTO, *, minor_exponent: int = CURRENCY_EXPONENT) -> dict[str, Any]:
    """One line of an emitted PO, its rate in the order's own minor units."""
    return {
        "item_id": line.item_external_id,
        "description": line.description,
        "quantity": line.quantity,
        "rate": render_paise(line.unit_price_paise, minor_exponent=minor_exponent),
    }


def _custom_field(row: Mapping[str, Any], api_name: str) -> str | None:
    """A custom field's value in any of the three shapes Zoho ERP v3 uses.

    VERIFIED LIVE (2026-09-11/12, DEMO WBS): a LIST row carries the field
    top-level under its api_name (`"cf_capex_ref": "..."`), a DETAIL row
    carries `custom_field_hash` {api_name: value} and the `custom_fields`
    list. Reading only the list -- as this did until the first live sweep --
    left every list-row key invisible, and with it the unsanctioned-commitment
    control on orders the tenant raised outside this system.
    """
    top = row.get(api_name)
    if top not in (None, ""):
        return _opt_str(top)
    hashed = row.get("custom_field_hash")
    if isinstance(hashed, Mapping) and hashed.get(api_name) not in (None, ""):
        return _opt_str(hashed.get(api_name))
    for field in row.get("custom_fields") or ():
        if field.get("api_name") == api_name:
            return _opt_str(field.get("value"))
    return None


def _opt_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
