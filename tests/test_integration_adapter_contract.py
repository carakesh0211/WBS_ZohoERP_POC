"""The C1 adapter contract, held identically against both implementations.

Wave 5 seam C1 is frozen and streams 2 through 7 are building against it right
now. What they are entitled to rely on is that *either* implementation
satisfies it -- because D-14 is unresolved and nobody yet knows which one will
be in production. So almost everything here is parametrised over both adapters,
and the handful of tests that are not are the ones asserting a genuine
difference between the products.

No network. No tenant. The default transport refuses to make a call at all;
these tests supply a cassette player, and every cassette is hand-authored and
labelled ``INVENTED-SANITISED``.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.backend.integration import (
    POLL_OVERLAP_SECONDS,
    BillDTO,
    Capabilities,
    DEDUPE_CUSTOM_FIELD,
    IntegrationError,
    LineDTO,
    NetworkForbidden,
    NoNetworkTransport,
    Page,
    ProcurementAdapter,
    PurchaseOrderDTO,
    UnsupportedDataCentre,
    adapter_for,
    walk_pages,
)
from app.backend.integration import books_inventory as bi
from app.backend.integration import erp as erp_module
from app.backend.integration.adapter import CassetteTransport, encode_query
from app.backend.integration.dto import DtoError, SourceRef, paise, quantity

CASSETTES = Path(__file__).resolve().parent / "cassettes"
SINCE = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
FAR_FUTURE = datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)

#: (adapter factory, the bill id its fixtures expose) for the parametrised tests.
PRODUCTS = ["ERP", "BOOKS_INVENTORY"]
FIRST_BILL = {"ERP": "BILL-ERP-1001", "BOOKS_INVENTORY": "BILL-BKS-2001"}


def build(product: str):
    return adapter_for(
        product,
        organization_id="60000000001",
        dc="IN",
        per_page=2,
        transport=CassetteTransport(product, CASSETTES),
    )


@pytest.fixture(params=PRODUCTS)
def adapter(request):
    return build(request.param)


# ================================================================ the C1 shape
C1_METHODS = {
    "base_url": ("dc",),
    "scopes_required": (),
    "list_bills": ("since", "until", "page"),
    "get_bill": ("external_id",),
    "list_purchase_orders": ("since", "until", "page"),
    "create_purchase_order": ("po", "dedupe_key"),
    "receives_for_po": ("po_external_id",),
    "capabilities": (),
}


@pytest.mark.parametrize("product", PRODUCTS)
def test_both_implementations_satisfy_the_frozen_c1_signature(product):
    """Frozen means frozen: same names, same parameter names, both products."""
    instance = build(product)
    assert instance.product == product
    assert isinstance(instance, ProcurementAdapter)
    for name, params in C1_METHODS.items():
        method = getattr(instance, name, None)
        assert callable(method), f"{product} is missing C1 method {name}()"
        actual = tuple(
            p for p in inspect.signature(method).parameters
            if p not in ("self",))
        assert actual[:len(params)] == params, (
            f"{product}.{name}{actual} does not match the frozen C1 seam "
            f"{name}{params}. Streams 2-7 are building against that signature.")


def test_the_capabilities_dataclass_has_exactly_the_six_frozen_fields():
    """Adding a seventh would break every stream constructing one positionally."""
    assert [f for f in Capabilities.__dataclass_fields__] == [
        "receives_listable", "bills_delta_filter", "po_delta_filter",
        "items_delta_filter", "line_level_custom_fields", "daily_call_ceiling",
    ]
    assert Capabilities.__dataclass_params__.frozen is True


def test_an_unknown_product_is_refused_rather_than_defaulted():
    """Quietly picking ERP because the engagement was framed that way is D-14."""
    with pytest.raises(IntegrationError) as exc:
        adapter_for("ZOHO_ONE", organization_id="1")
    assert "provisional" in str(exc.value).lower()


def test_the_poll_overlap_belongs_to_the_boundary_not_to_a_caller():
    """§11.3's 300 s, where a scheduler cannot quietly shrink it."""
    assert POLL_OVERLAP_SECONDS == 300


# ================================================== nothing leaves the process
@pytest.mark.parametrize("product", PRODUCTS)
def test_an_adapter_built_without_a_transport_cannot_fetch_anything(product):
    """The default is refusal. A forgotten test double fails loudly, not live."""
    instance = adapter_for(product, organization_id="1")
    assert isinstance(instance.transport, NoNetworkTransport)
    with pytest.raises(NetworkForbidden) as exc:
        instance.list_bills(SINCE, UNTIL, page=1)
    assert "Phase 0B has not cleared" in str(exc.value)


@pytest.mark.parametrize("product", PRODUCTS)
def test_metadata_is_answerable_without_any_transport(product):
    """A consent screen and a scheduler need these before a connection exists."""
    instance = adapter_for(product, organization_id="1")
    assert instance.base_url("IN").startswith("https://")
    assert instance.scopes_required()
    assert isinstance(instance.capabilities(), Capabilities)


# ========================================================= money is integer paise
def test_every_monetary_field_is_an_int_never_a_float_or_decimal(adapter):
    page = adapter.list_bills(SINCE, UNTIL, page=1)
    bill = page.items[0]
    for field in ("subtotal_paise", "tax_paise", "total_paise"):
        value = getattr(bill, field)
        assert type(value) is int, f"{field} is {type(value).__name__}"
        assert not isinstance(value, (float, Decimal))


def test_the_erp_amounts_map_to_the_exact_paise_expected():
    """Exact values, not "is not None". A rounding defect must be visible here."""
    page = build("ERP").list_bills(SINCE, UNTIL, page=1)
    first, second = page.items
    assert (first.subtotal_paise, first.tax_paise, first.total_paise) == (
        12500050, 2250009, 14750059)
    assert (second.subtotal_paise, second.tax_paise, second.total_paise) == (
        8400000, 1512000, 9912000)


def test_a_half_paise_amount_rounds_half_up_not_through_binary_float():
    """AUD-H-007, re-asserted at the integration boundary.

    ``5000.005`` is the shape that broke before: through ``float`` it rounds to
    ``500000`` by banker's rounding on an inexact binary value. Through
    ``Decimal`` and ROUND_HALF_UP -- which is what Indian accounting expects --
    it is ``500001``.
    """
    page = build("ERP").list_bills(SINCE, FAR_FUTURE, page=2)
    assert page.items[0].subtotal_paise == 500001


def test_a_float_is_rejected_rather_than_rounded():
    """The transport parses with ``parse_float=str`` so this never happens --
    and if that ever regresses, the DTO refuses instead of silently rounding."""
    with pytest.raises(DtoError) as exc:
        paise(147500.59, field="total")
    assert "float" in str(exc.value)


def test_a_quantity_is_an_exact_string_and_never_a_number(adapter):
    """A quantity is decimal but is not money, so it gets neither treatment."""
    with pytest.raises(DtoError):
        quantity(2.5, field="line.quantity")
    assert quantity("2.500", field="q") == "2.500"
    assert quantity(3, field="q") == "3"


# ================================================================== pagination
def test_pagination_terminates_on_has_more_page_and_nothing_else():
    """There is no cursor and no reliable total in any of the three products."""
    erp = build("ERP")
    page1 = erp.list_bills(SINCE, UNTIL, page=1)
    assert isinstance(page1, Page) and len(page1) == 2 and page1.has_more is True
    page2 = erp.list_bills(SINCE, UNTIL, page=2)
    assert page2.has_more is False


def test_walking_the_pages_collects_the_window_and_stops():
    """And the window's upper bound removed a row without breaking termination.

    Page 2 holds one bill, modified after ``until``. It is dropped locally --
    Zoho's filter is one-sided and cannot bound the top of the window -- yet
    the walk still terminates on ``has_more_page``, because the page context is
    read from the response rather than inferred from how many rows survived.
    """
    erp = build("ERP")
    collected = list(walk_pages(lambda page: erp.list_bills(SINCE, UNTIL, page)))
    assert [b.external_id for b in collected] == ["BILL-ERP-1001", "BILL-ERP-1002"]
    assert len(erp.transport.request_log) == 2


def test_a_server_that_never_clears_has_more_page_cannot_burn_the_budget():
    """2,000 calls/day on ERP Standard. An unbounded walk would spend all of them."""
    def never_ends(page):
        return Page(items=(), page=page, per_page=1, has_more=True)

    with pytest.raises(IntegrationError) as exc:
        list(walk_pages(never_ends, page_limit=3))
    assert "did not terminate" in str(exc.value)


# ======================================================== the one-sided filter
def test_the_window_upper_bound_is_enforced_locally(adapter):
    """Zoho filters "modified after"; there is no upper-bound parameter."""
    page = adapter.list_bills(SINCE, UNTIL, page=1)
    assert all(bill.last_modified <= UNTIL for bill in page.items)
    sent = adapter.transport.request_log[-1]["params"]
    assert "last_modified_time" in sent
    assert not any("end" in key or "before" in key for key in sent), (
        "An upper-bound parameter was sent. None is documented, and an "
        "unrecognised query parameter is ignored rather than rejected -- so "
        "the window would be silently unbounded.")


def test_a_row_outside_the_window_is_dropped_and_the_same_row_is_kept_when_it_fits():
    erp = build("ERP")
    assert erp.list_bills(SINCE, UNTIL, page=2).items == ()
    assert len(erp.list_bills(SINCE, FAR_FUTURE, page=2).items) == 1


# ============================================== list responses omit line items
def test_a_listed_bill_is_marked_unhydrated_and_a_fetched_one_is_not(adapter):
    """Otherwise an empty ``lines`` tuple means two different things, and the
    caller that reads "no lines" as "no cost" books zero CWIP."""
    listed = adapter.list_bills(SINCE, UNTIL, page=1).items[0]
    assert listed.lines == () and listed.lines_hydrated is False

    fetched = adapter.get_bill(FIRST_BILL[adapter.product])
    assert fetched.lines and fetched.lines_hydrated is True
    assert all(isinstance(line, LineDTO) for line in fetched.lines)


def test_an_unlinked_bill_line_reports_no_linkage_rather_than_guessing_one():
    """AUD-H-004. Documented is not populated (§11.8).

    The second line of the ERP bill carries no ``purchaseorder_item_id``. It
    must map to ``None`` -- never to the neighbouring line's id, never spread
    pro-rata across the linked lines. The unattributed amount is preserved so
    it can accumulate in ``unattributed_receipts_paise`` and block
    capitalisation, which is the whole point of the quarantine.
    """
    bill = build("ERP").get_bill("BILL-ERP-1001")
    linked, unlinked = bill.lines
    assert linked.purchase_order_line_external_id == "POL-ERP-5001-1"
    assert unlinked.purchase_order_line_external_id is None
    assert unlinked.line_total_paise == 1000000


def test_an_unlinked_receive_line_is_preserved_with_its_amount_intact():
    receives = build("ERP").receives_for_po("PO-ERP-5001")
    unattributed = [line for r in receives for line in r.lines
                    if line.purchase_order_line_external_id is None]
    assert len(unattributed) == 1
    assert unattributed[0].line_total_paise == 17250075


# ============================================================ raw status, source
def test_the_external_status_is_carried_verbatim_and_not_translated(adapter):
    """C16/§8.4: raw Zoho status is stored verbatim and never overwritten.

    An unmapped value is accepted and raises UNMAPPED_EXTERNAL_STATUS upstream;
    it is never guessed here, and an integration status must never reach a
    business screen wearing a C3 code.
    """
    statuses = {b.external_status_raw
                for b in adapter.list_bills(SINCE, UNTIL, page=1).items}
    assert statuses and all(s.islower() or "_" in s for s in statuses)
    assert not any(s.isupper() for s in statuses), (
        "A raw Zoho status was upper-cased, which is the first step towards "
        "silently mapping it onto a C3 business status.")


def test_every_dto_knows_the_product_service_and_api_version_that_made_it(adapter):
    bill = adapter.list_bills(SINCE, UNTIL, page=1).items[0]
    assert isinstance(bill.source, SourceRef)
    assert bill.source.product == adapter.product
    assert bill.source.endpoint == "/bills"
    assert bill.source.api_version in {"v3", "v1"}


def test_the_books_inventory_product_records_two_distinct_services():
    """One product, two services. Collapsing them loses the distinction that
    stops a Books fact becoming evidence for an Inventory claim."""
    books = build("BOOKS_INVENTORY")
    bill = books.list_bills(SINCE, UNTIL, page=1).items[0]
    receive = books.list_receives(page=1).items[0]
    assert (bill.source.service, bill.source.api_version) == ("books", "v3")
    assert (receive.source.service, receive.source.api_version) == ("inventory", "v1")
    assert bill.source.product == receive.source.product == "BOOKS_INVENTORY"


def test_a_source_ref_cannot_name_a_product_that_does_not_exist():
    with pytest.raises(DtoError):
        SourceRef(product="ZOHO_ONE", service="erp", api_version="v3",
                  endpoint="/bills", retrieved_at=datetime.now(timezone.utc))


def test_dtos_are_frozen_and_their_raw_payloads_are_read_only(adapter):
    bill = adapter.list_bills(SINCE, UNTIL, page=1).items[0]
    with pytest.raises(Exception):
        bill.total_paise = 0
    with pytest.raises(TypeError):
        bill.raw["total"] = 0


# ==================================================== outbound and idempotency
def _draft_po(product: str) -> PurchaseOrderDTO:
    return PurchaseOrderDTO(
        source=SourceRef(product=product, service="erp" if product == "ERP" else "books",
                         api_version="v3", endpoint="/purchaseorders",
                         retrieved_at=datetime.now(timezone.utc)),
        external_id="", document_number="", document_date=date(2026, 9, 1),
        last_modified=datetime.now(timezone.utc), vendor_external_id="VEN-1",
        vendor_name="Northgate Structural Works", currency_code="INR",
        subtotal_paise=1150005, tax_paise=207001, total_paise=1357006,
        external_status_raw="",
        lines=(LineDTO(external_line_id=None, line_number=1,
                       description="Structural steel fabrication",
                       quantity="1", unit_price_paise=1150005,
                       line_total_paise=1150005, tax_paise=207001,
                       item_external_id="ITM-1"),))


@pytest.mark.parametrize("product", PRODUCTS)
def test_an_emitted_po_carries_the_synthesised_dedupe_key(product):
    """§11.6. Zoho documents no idempotency header on any of the three products.

    The dedupe key goes into the unique custom field ``cf_capex_ref`` (Z-01),
    so a function killed after sending but before recording updates by that
    unique value rather than creating a second commitment.
    """
    instance = build(product)
    created = instance.create_purchase_order(_draft_po(product), "CAPEX-PO-000119")
    assert created.endswith("5099") or created.endswith("6099")

    sent = instance.transport.request_log[-1]
    assert sent["method"] == "POST"
    fields = {f["api_name"]: f["value"] for f in sent["body"]["custom_fields"]}
    assert fields[DEDUPE_CUSTOM_FIELD] == "CAPEX-PO-000119"


@pytest.mark.parametrize("product", PRODUCTS)
def test_a_po_cannot_be_emitted_without_a_dedupe_key(product):
    with pytest.raises(IntegrationError) as exc:
        build(product).create_purchase_order(_draft_po(product), "")
    assert "duplicate commitment" in str(exc.value)


@pytest.mark.parametrize("product", PRODUCTS)
def test_an_emitted_po_sets_no_status_and_claims_no_idempotency_header(product):
    """POs are emitted as draft and transitioned to open by our own later call,
    after our approval instance closes. A Zoho-side status change we did not
    initiate is an exception, so nothing here may set one."""
    instance = build(product)
    instance.create_purchase_order(_draft_po(product), "CAPEX-PO-000119")
    body = instance.transport.request_log[-1]["body"]
    assert "status" not in body
    assert not any("idempot" in key.lower() for key in body)


@pytest.mark.parametrize("product", PRODUCTS)
def test_an_emitted_amount_is_a_decimal_string_built_from_integer_paise(product):
    instance = build(product)
    instance.create_purchase_order(_draft_po(product), "CAPEX-PO-000119")
    rate = instance.transport.request_log[-1]["body"]["line_items"][0]["rate"]
    assert rate == "11500.05"
    assert isinstance(rate, str)


# ======================================================= hosts and data centres
def test_erp_publishes_exactly_one_data_centre_and_refuses_to_invent_others():
    """A ``.com``/``.eu`` ERP host is NOT CONFIRMED. Borrowing one from Zoho
    CRM's multi-DC table is the "documentation for one product is evidence for
    another" error, one product further out."""
    erp = build("ERP")
    assert erp.base_url("IN") == "https://www.zohoapis.in/erp/v3"
    assert set(erp_module.API_HOST_BY_DC) == {"IN"}
    for dc in ("US", "EU", "AU", "JP", "CA"):
        with pytest.raises(UnsupportedDataCentre) as exc:
            erp.base_url(dc)
        assert "NOT CONFIRMED" in str(exc.value)


def test_books_inventory_publishes_eight_hosts_across_two_services():
    books = build("BOOKS_INVENTORY")
    assert len(bi.API_HOST_BY_DC) == 8
    assert books.base_url("IN") == "https://www.zohoapis.in/books/v3"
    assert books.service_base_url("inventory", "EU") == "https://www.zohoapis.eu/inventory/v1"
    with pytest.raises(UnsupportedDataCentre):
        books.base_url("XX")


def test_three_inventory_accounts_servers_are_refused_as_unconfirmed():
    """Five of the eight are confirmed. An OAuth flow pointed at a guess fails
    at consent time, in front of the client."""
    books = build("BOOKS_INVENTORY")
    assert books.accounts_server("IN") == "https://accounts.zoho.in"
    for dc in ("JP", "CN", "SA"):
        with pytest.raises(UnsupportedDataCentre) as exc:
            books.accounts_server(dc)
        assert "NOT CONFIRMED" in str(exc.value)


def test_the_erp_accounts_server_is_india_only():
    erp = build("ERP")
    assert erp.accounts_server("IN") == "https://accounts.zoho.in"
    with pytest.raises(UnsupportedDataCentre):
        erp.accounts_server("US")


# ================================================================ OAuth scopes
def test_the_two_scopes_zohos_own_oauth_page_omits_are_still_requested():
    """A documented defect in Zoho's ERP OAuth page (§11.2).

    Its scope table omits ``ERP.purchasereceives.*`` and
    ``ERP.custommodules.ALL``, which the module pages do document. A consent
    screen built from the scope table would silently drop both -- and the
    integration would then fail at runtime on the two modules that matter
    most. The evidence for each scope is recorded per page so the defect stays
    visible and re-verifiable at the tenant.
    """
    omitted = {e.scope for e in erp_module.SCOPE_EVIDENCE
               if not e.documented_on_oauth_scope_page}
    assert omitted == {"ERP.purchasereceives.READ", "ERP.custommodules.ALL"}
    assert all(e.documented_on_module_page for e in erp_module.SCOPE_EVIDENCE)
    assert omitted <= build("ERP").scopes_required(), (
        "The consent list must be built from the module pages, not the scope table.")


def test_erp_items_live_under_settings_and_there_is_no_erp_items_scope():
    scopes = build("ERP").scopes_required()
    assert "ERP.settings.READ" in scopes
    assert not any(s.startswith("ERP.items") for s in scopes), (
        "There is no ERP.items scope. ERP files items under settings; only "
        "Inventory has a ZohoInventory.items scope.")


def test_inventory_never_asks_for_an_all_scope_because_it_documents_none():
    """Books and ERP document ``.ALL``; Inventory's documentation enumerates
    CRUD individually and never shows it. Requesting a scope that is not
    documented is requesting one that may not be granted."""
    scopes = build("BOOKS_INVENTORY").scopes_required()
    inventory = {s for s in scopes if s.startswith("ZohoInventory.")}
    assert inventory
    assert not any(s.endswith(".ALL") for s in inventory)
    assert "ZohoInventory.items.READ" in scopes


# ================================================================ query encoding
def test_a_plus_offset_reaches_the_wire_percent_encoded():
    """Books documents ``YYYY-MM-DDTHH:MM:SS-UTC`` and requires ``+`` as ``%2B``.

    An unencoded ``+`` is read as a space by any conforming query parser, which
    malforms the timestamp and silently changes the window a poll selects. A
    silently wrong window loses documents without producing an error.
    """
    encoded = encode_query({"last_modified_time": "2026-08-26T00:00:00+0530"})
    assert "%2B0530" in encoded
    assert "+" not in encoded


def test_the_two_delta_formats_are_held_apart():
    """ERP/Books take an offset-bearing stamp; Inventory items documents
    ``yyyy-MM-ddTHH:mm:ssZ``. Two formats on two services, no shared guess."""
    assert erp_module.DELTA_FILTER_FORMAT == "%Y-%m-%dT%H:%M:%S%z"
    assert bi.BOOKS_DELTA_FORMAT == "%Y-%m-%dT%H:%M:%S%z"
    assert bi.INVENTORY_DELTA_FORMAT == "%Y-%m-%dT%H:%M:%SZ"
    assert bi.INVENTORY_DELTA_FORMAT != bi.BOOKS_DELTA_FORMAT


# =========================================================== organization_id
def test_every_request_carries_organization_id_as_a_query_parameter(adapter):
    """True of all three products, on every single request."""
    adapter.list_bills(SINCE, UNTIL, page=1)
    adapter.get_bill(FIRST_BILL[adapter.product])
    adapter.list_purchase_orders(SINCE, UNTIL, page=1)
    assert adapter.transport.request_log
    for entry in adapter.transport.request_log:
        assert entry["params"]["organization_id"] == "60000000001"


def test_a_bill_dto_is_what_comes_back_not_a_zoho_dictionary(adapter):
    page = adapter.list_bills(SINCE, UNTIL, page=1)
    assert all(isinstance(b, BillDTO) for b in page.items)
    assert all(isinstance(b.document_date, date) for b in page.items)
    assert all(b.last_modified.tzinfo is not None for b in page.items)
