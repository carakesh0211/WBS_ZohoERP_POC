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
    CapabilityError,
    ContactDTO,
    DEDUPE_CUSTOM_FIELD,
    DEDUPE_SCAN_PAGE_LIMIT,
    IntegrationError,
    ItemDTO,
    LineDTO,
    NetworkForbidden,
    NoNetworkTransport,
    Page,
    ProcurementAdapter,
    PurchaseOrderDTO,
    RecoverableProcurementAdapter,
    UnsupportedDataCentre,
    adapter_for,
    verified_dedupe_match,
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
    # Added after the freeze. Plan §11.5 required poll_items and poll_contacts
    # all along; C1 declared neither, so sweeps.py routed around them by
    # raising AdapterMethodMissing. Same (since, until, page) shape as the
    # other two list calls, because the poll invokes all four identically.
    "list_items": ("since", "until", "page"),
    "list_contacts": ("since", "until", "page"),
}

#: The recovery surface. Separate from C1 because it answers a different
#: question -- see RecoverableProcurementAdapter.
RECOVERY_METHODS = {
    "resolve_by_dedupe_key": ("dedupe_key",),
    "update_purchase_order": ("external_id", "payload", "dedupe_key"),
    "transition_purchase_order": ("external_id", "state", "actor"),
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


#: The six fields frozen by the Wave 5 C1 seam, in their frozen order.
FROZEN_CAPABILITY_FIELDS = [
    "receives_listable", "bills_delta_filter", "po_delta_filter",
    "items_delta_filter", "line_level_custom_fields", "daily_call_ceiling",
]


def test_the_six_frozen_capability_fields_are_still_the_first_six_in_order():
    """The freeze, stated as what it was actually protecting.

    The original assertion was "exactly six fields", and its stated reason was
    that "adding a seventh would break every stream constructing one
    positionally". That reason is the real requirement, and it is the one kept
    here: the six may not be renamed, reordered, or displaced.

    A seventh field was added after the freeze (``po_dedupe_search``) because
    C1 had no way to say whether a product can search purchase orders by the
    dedupe custom field, and a caller that assumed one either burns a scan it
    did not need or sends a filter that is not documented. See
    ``tests/ADAPTATIONS.md``.
    """
    fields = list(Capabilities.__dataclass_fields__)
    assert fields[:6] == FROZEN_CAPABILITY_FIELDS, (
        "The six frozen C1 capability fields have been renamed, reordered or "
        "displaced. Streams 2-7 construct these positionally.")
    assert Capabilities.__dataclass_params__.frozen is True


def test_every_capability_field_added_after_the_freeze_carries_a_default():
    """Which is what makes adding one safe, and is now enforced rather than argued.

    A field appended *with* a default cannot break a positional construction:
    every existing call site still supplies exactly the six it always did, in
    the same order, and still means what it meant. A field appended *without*
    one breaks every such call site at import time. So the rule is not "never
    add a field", it is "never add one that can silently reposition or newly
    require an argument" -- and that is checkable.
    """
    import dataclasses

    for field in dataclasses.fields(Capabilities)[len(FROZEN_CAPABILITY_FIELDS):]:
        has_default = (field.default is not dataclasses.MISSING
                       or field.default_factory is not dataclasses.MISSING)
        assert has_default, (
            f"Capabilities.{field.name} was added after the C1 freeze with no "
            f"default. Every stream that constructs Capabilities with the six "
            f"frozen fields would fail at import.")


def test_the_six_frozen_fields_can_still_be_supplied_positionally():
    """The freeze's promise, exercised rather than asserted about.

    If this ever fails, a stream that wrote ``Capabilities(False, True, True,
    False, False, 2000)`` in Wave 5 is broken -- which is precisely the
    breakage the original "exactly six" test existed to prevent.
    """
    caps = Capabilities(False, True, True, False, False, 2000)
    assert caps.receives_listable is False
    assert caps.bills_delta_filter is True
    assert caps.po_delta_filter is True
    assert caps.items_delta_filter is False
    assert caps.line_level_custom_fields is False
    assert caps.daily_call_ceiling == 2000


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


def test_a_receive_line_is_attributed_through_its_item_and_its_amount_is_intact():
    """VERIFIED LIVE 2026-09-12 (DEMO WBS receive 3912780000000116003): a Zoho
    ERP purchase-receive line carries NO order-line reference -- only its own
    line_item_id, item_id, item_order and quantity. The cassette's receive
    line (no line id at all, item ITM-ERP-7001) was therefore never
    "unlinked" in the sense this test once asserted; the adapter now
    attributes it through the ORDER'S item -> line map it learns from the PO
    detail, and its amount travels untouched. An item absent from the order,
    or present on two of its lines, still yields None and the sweep
    quarantines it (`tests/test_erp_list_row_shape_fable51.py`)."""
    receives = build("ERP").receives_for_po("PO-ERP-5001")
    lines = [line for r in receives for line in r.lines]
    attributed = [line for line in lines if line.purchase_order_line_external_id is not None]
    assert len(lines) >= 1 and len(attributed) == len(lines), [
        (line.item_external_id, line.purchase_order_line_external_id) for line in lines]
    # The line the old assertion called "unlinked" (17250075 paise): still
    # there, amount intact, and now attributed through its item.
    kept = [line for line in lines if line.line_total_paise == 17250075]
    assert len(kept) == 1 and kept[0].purchase_order_line_external_id is not None


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


# ============================================== §11.5 master data: the C1 gap
#
# Two Wave 5 streams independently reported that C1 declared no `list_items`
# and no `list_contacts` while plan §11.5 requires `poll_items` and
# `poll_contacts`. `sweeps.py` raised `AdapterMethodMissing` rather than
# papering over it with a no-op, because a master-data poll that fetches
# nothing looks exactly like one that found nothing. These close that gap.

@pytest.mark.parametrize("product", PRODUCTS)
def test_both_products_can_list_items_and_contacts(product):
    """The gap sweeps.py reported, closed on both implementations."""
    instance = build(product)
    items = instance.list_items(SINCE, FAR_FUTURE, page=1)
    contacts = instance.list_contacts(SINCE, FAR_FUTURE, page=1)
    assert items.items and contacts.items
    assert all(isinstance(i, ItemDTO) for i in items.items)
    assert all(isinstance(c, ContactDTO) for c in contacts.items)


@pytest.mark.parametrize("product", PRODUCTS)
def test_master_data_pages_carry_the_uniform_page_context(product):
    """§11.2: `page`/`per_page` in, `has_more_page` out. The one uniform thing."""
    instance = build(product)
    for page in (instance.list_items(SINCE, FAR_FUTURE, page=1),
                 instance.list_contacts(SINCE, FAR_FUTURE, page=1)):
        assert isinstance(page, Page)
        assert page.page == 1
        assert isinstance(page.has_more, bool)
        for entry in instance.transport.request_log:
            assert "page" in entry["params"] and "per_page" in entry["params"]


@pytest.mark.parametrize("product", PRODUCTS)
def test_master_data_requests_carry_organization_id(product):
    """The boundary rule, held on the two new endpoints as well."""
    instance = build(product)
    instance.list_items(SINCE, FAR_FUTURE, page=1)
    instance.list_contacts(SINCE, FAR_FUTURE, page=1)
    assert instance.transport.request_log
    for entry in instance.transport.request_log:
        assert entry["params"]["organization_id"] == "60000000001"


def test_erp_items_are_a_full_refresh_and_inventory_items_are_a_true_delta():
    """The asymmetry §11.3 records, asserted rather than assumed equal.

    This is the "do not invent parity" rule in one test. ERP `GET /items` has
    no `last_modified_time` filter, so the poll is a weekly full refresh and
    the adapter must NOT send a window it was not promised. Inventory's items
    endpoint is the one true delta in the whole matrix and does send one.
    """
    erp = build("ERP")
    erp.list_items(SINCE, FAR_FUTURE, page=1)
    erp_params = erp.transport.request_log[-1]["params"]
    assert erp.capabilities().items_delta_filter is False
    assert "last_modified_time" not in erp_params, (
        "ERP GET /items documents no last_modified_time filter. An "
        "unrecognised parameter is ignored rather than rejected, so sending "
        "one would return everything while looking like a delta.")

    books = build("BOOKS_INVENTORY")
    books.list_items(SINCE, FAR_FUTURE, page=1)
    inv_params = books.transport.request_log[-1]["params"]
    assert books.capabilities().items_delta_filter is True
    assert "last_modified_time" in inv_params
    # Inventory's own format, not the offset-bearing Books/ERP one.
    assert inv_params["last_modified_time"].endswith("Z")


@pytest.mark.parametrize("product", PRODUCTS)
def test_contacts_never_send_a_delta_filter_on_any_product(product):
    """§11.3: contacts are sort-only on all three. There is no filter to send.

    And there is deliberately no `contacts_delta_filter` in `Capabilities` to
    read: naming a capability that does not exist would be a lie that reads as
    a bug, so `sweeps.poll_contacts` looks one up, finds nothing, and gets the
    full refresh the plan prescribes.
    """
    instance = build(product)
    instance.list_contacts(SINCE, FAR_FUTURE, page=1)
    params = instance.transport.request_log[-1]["params"]
    assert "last_modified_time" not in params or params.get("sort_column")
    assert not hasattr(instance.capabilities(), "contacts_delta_filter")
    # Sort IS documented here, and is what makes an early stop possible.
    assert params["sort_column"] == "last_modified_time"


def test_inventory_purchase_orders_still_accept_neither_filter_nor_sort():
    """The constraint that must survive somebody "optimising" the PO poll.

    Inventory's `GET /purchaseorders` takes `organization_id`, `page` and
    `per_page` and nothing else, which is why POs are read from Books. Adding
    master-data calls to this adapter must not have loosened it.
    """
    assert bi.SORT_COLUMNS[(bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_ORDERS)] == frozenset()
    books = build("BOOKS_INVENTORY")
    with pytest.raises(CapabilityError):
        books._get(bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_ORDERS,
                   "ZohoInventory.purchaseorders.READ",
                   {"last_modified_time": "2026-08-26T00:00:00Z"})


@pytest.mark.parametrize("product", PRODUCTS)
def test_a_master_data_row_with_no_timestamp_maps_to_none_not_to_now(product):
    """Because these endpoints have no delta filter, a row may carry no stamp.

    Turning that into `now` would let a watermark advance past records nobody
    read -- the silent-loss failure mode. `None` is the honest answer and the
    poll treats it as "keep".
    """
    instance = build(product)
    items = instance.list_items(SINCE, FAR_FUTURE, page=1)
    for item in items.items:
        assert item.last_modified is None or item.last_modified.tzinfo is not None


def test_the_erp_item_fixture_actually_exercises_the_missing_timestamp():
    """A guard nobody has seen fire is a guard nobody trusts."""
    erp = build("ERP")
    items = erp.list_items(SINCE, FAR_FUTURE, page=1)
    assert any(i.last_modified is None for i in items.items), (
        "The ERP items cassette no longer contains a row without "
        "last_modified_time, so the None-not-now mapping is untested.")


@pytest.mark.parametrize("product", PRODUCTS)
def test_master_data_money_is_integer_paise(product):
    """Never a float, never a Decimal -- on the new DTOs too."""
    instance = build(product)
    for item in instance.list_items(SINCE, FAR_FUTURE, page=1).items:
        assert isinstance(item.rate_paise, int)
        assert not isinstance(item.rate_paise, bool)
    # 48500.00 -> 4_850_000 paise, exactly, via the decimal string.
    erp_rates = {i.external_id: i.rate_paise
                 for i in build("ERP").list_items(SINCE, FAR_FUTURE, page=1).items}
    assert erp_rates["ITM-ERP-7001"] == 4_850_000
    assert erp_rates["ITM-ERP-7002"] == 27_500_050


@pytest.mark.parametrize("product", PRODUCTS)
def test_the_adapter_does_not_decide_what_a_vendor_is(product):
    """`contact_type` is carried, not filtered on.

    The same tenant can legitimately bill a party it also buys from, and
    burying that business rule inside a transport-shaped module is how it stops
    being reviewable.
    """
    instance = build(product)
    contacts = instance.list_contacts(SINCE, FAR_FUTURE, page=1)
    assert all(c.contact_type is not None for c in contacts.items)


def test_the_erp_contact_fixture_includes_a_non_vendor():
    """Otherwise the previous test proves nothing."""
    contacts = build("ERP").list_contacts(SINCE, FAR_FUTURE, page=1)
    types = {c.contact_type for c in contacts.items}
    assert "vendor" in types and "customer" in types


# ======================================== the money-relevant gap: lost responses
#
# `create_purchase_order(po, dedupe_key)` took the key and C1 had NO call that
# read a record back by it. So a response lost between "sent" and "recorded"
# left a purchase order that EXISTS in the tenant and could never be linked.
# Z-01's unique `cf_capex_ref` still prevented the duplicate; the LINK was what
# was lost, and an unlinked commitment blocks period close.

@pytest.mark.parametrize("product", PRODUCTS)
def test_both_products_expose_the_recovery_surface(product):
    """Same names, same parameter names, both implementations."""
    instance = build(product)
    assert isinstance(instance, RecoverableProcurementAdapter)
    for name, params in RECOVERY_METHODS.items():
        method = getattr(instance, name, None)
        assert callable(method), f"{product} cannot recover: no {name}()"
        actual = tuple(p for p in inspect.signature(method).parameters
                       if p != "self")
        assert actual[:len(params)] == params


@pytest.mark.parametrize("product,key,expected", [
    ("ERP", "CAPEX-PO-000117", "PO-ERP-5001"),
    ("BOOKS_INVENTORY", "CAPEX-PO-000044", "PO-BKS-6001"),
])
def test_a_purchase_order_can_be_found_by_its_dedupe_key(product, key, expected):
    """The one call that closes the gap, on both products."""
    assert build(product).resolve_by_dedupe_key(key) == expected


@pytest.mark.parametrize("product", PRODUCTS)
def test_resolving_an_absent_key_returns_none_rather_than_a_candidate(product):
    """`None` means "not found", and must never be the nearest row.

    Adopting the wrong id links this commitment to somebody else's document.
    That is worse than not linking it: the orphan is loud and blocks period
    close, the mislink is silent and wrong.
    """
    assert build(product).resolve_by_dedupe_key("CAPEX-PO-NOT-PRESENT") is None


@pytest.mark.parametrize("product,prefix_of_real", [
    ("ERP", "CAPEX-PO-00011"),
    ("BOOKS_INVENTORY", "CAPEX-PO-00004"),
])
def test_a_prefix_of_a_real_key_never_matches(product, prefix_of_real):
    """Exact equality, never a prefix.

    Zoho's documented `custom_field` variants include `_startswith` and
    `_contains`, and `CAPEX-PO-000117` is a prefix of `CAPEX-PO-0001170`. A
    resolver that accepted either would adopt the wrong purchase order for a
    key that merely looks similar.
    """
    assert build(product).resolve_by_dedupe_key(prefix_of_real) is None


@pytest.mark.parametrize("product", PRODUCTS)
def test_resolving_on_an_empty_key_is_refused_outright(product):
    """An empty key would match whatever the tenant happened to return first."""
    with pytest.raises(IntegrationError):
        build(product).resolve_by_dedupe_key("")


def test_only_erp_claims_a_documented_server_side_dedupe_search():
    """The honest cost difference, declared instead of assumed.

    Zoho's published ERP bundle documents a `custom_field` parameter on
    `GET /purchaseorders`. Nothing in this repository documents one for Books,
    and §11's first line forbids reading ERP documentation as Books evidence.
    So Books resolves by a bounded scan and SAYS so, rather than sending a
    parameter that would be ignored rather than rejected.
    """
    assert build("ERP").capabilities().po_dedupe_search is True
    assert build("BOOKS_INVENTORY").capabilities().po_dedupe_search is False


def test_the_books_resolver_sends_no_undocumented_search_parameter():
    """The rule, checked on the wire rather than in the docstring."""
    books = build("BOOKS_INVENTORY")
    books.resolve_by_dedupe_key("CAPEX-PO-000044")
    assert books.transport.request_log
    for entry in books.transport.request_log:
        assert set(entry["params"]) <= {"organization_id", "page", "per_page"}, (
            f"Books resolve sent {sorted(entry['params'])}. An undocumented "
            f"query parameter is ignored, not rejected -- which would make an "
            f"unfiltered first page look like a filtered match.")


def test_the_erp_resolver_does_use_its_documented_search_parameter():
    """Otherwise ERP would silently be paying for a scan it does not need."""
    erp = build("ERP")
    erp.resolve_by_dedupe_key("CAPEX-PO-000117")
    first = erp.transport.request_log[0]
    # VERIFIED LIVE (2026-09-11): the parameter is the field's api_name and
    # the value is the bare key. The documented `custom_field=cf:value` form
    # this assertion used to pin is ignored by the tenant (it answers the
    # unfiltered list), which the verifier then rightly refused -- so a
    # present key resolved to None. See test_erp_list_row_shape_fable51.py.
    assert erp_module.DEDUPE_SEARCH_PARAM == DEDUPE_CUSTOM_FIELD
    assert first["params"][erp_module.DEDUPE_SEARCH_PARAM] == "CAPEX-PO-000117"


@pytest.mark.parametrize("product", PRODUCTS)
def test_a_resolve_is_bounded_and_cannot_burn_the_daily_ceiling(product):
    """A resolve runs on the retry path against 2,000 calls/day on ERP Standard.

    An unbounded scan there would spend the whole budget trying to recover one
    purchase order. The bound is a hard stop, and the fixtures terminate long
    before it -- so this asserts the ceiling exists and that the walk honours
    `has_more_page` rather than always paying for it.
    """
    instance = build(product)
    instance.resolve_by_dedupe_key("CAPEX-PO-NOT-PRESENT")
    assert len(instance.transport.request_log) <= DEDUPE_SCAN_PAGE_LIMIT + 1
    assert DEDUPE_SCAN_PAGE_LIMIT > 0


def test_verified_dedupe_match_is_what_makes_an_ignored_filter_survivable():
    """The guard, exercised directly on the failure it exists to stop.

    If a search parameter is misencoded the API ignores it and returns page 1
    of everything. Re-reading the custom field off the row makes the filter an
    optimisation and the value the authority.
    """
    unfiltered_page = [
        {"purchaseorder_id": "PO-OTHER-1",
         "custom_fields": [{"api_name": DEDUPE_CUSTOM_FIELD, "value": "CAPEX-X"}]},
        {"purchaseorder_id": "PO-OTHER-2", "custom_fields": []},
        {"purchaseorder_id": "PO-WANTED",
         "custom_fields": [{"api_name": DEDUPE_CUSTOM_FIELD, "value": "CAPEX-WANT"}]},
    ]
    assert verified_dedupe_match(
        unfiltered_page, dedupe_key="CAPEX-WANT",
        id_field="purchaseorder_id") == "PO-WANTED"
    assert verified_dedupe_match(
        unfiltered_page, dedupe_key="CAPEX-ABSENT",
        id_field="purchaseorder_id") is None
    # A different custom field carrying the same value is not a match.
    assert verified_dedupe_match(
        [{"purchaseorder_id": "PO-X",
          "custom_fields": [{"api_name": "cf_other", "value": "CAPEX-WANT"}]}],
        dedupe_key="CAPEX-WANT", id_field="purchaseorder_id") is None


def test_the_verification_guard_actually_fires_when_broken():
    """MUTATION TEST. Break the guard deliberately; prove the suite notices.

    Substring matching is the plausible wrong implementation -- Zoho documents
    `custom_field_contains`, so reaching for it is a small step. This shows
    what it would cost.
    """
    rows = [{"purchaseorder_id": "PO-LONGER",
             "custom_fields": [{"api_name": DEDUPE_CUSTOM_FIELD,
                                "value": "CAPEX-PO-0001170"}]}]

    def broken_substring_match(rows, *, dedupe_key, id_field):
        for row in rows:
            for field in row.get("custom_fields") or ():
                if dedupe_key in str(field.get("value")):
                    return str(row[id_field])
        return None

    # The broken version adopts a DIFFERENT purchase order for a key that is
    # merely a prefix. That is the mislink.
    assert broken_substring_match(
        rows, dedupe_key="CAPEX-PO-000117", id_field="purchaseorder_id") == "PO-LONGER"
    # The real one refuses.
    assert verified_dedupe_match(
        rows, dedupe_key="CAPEX-PO-000117", id_field="purchaseorder_id") is None


@pytest.mark.parametrize("product,external_id,key", [
    ("ERP", "PO-ERP-5001", "CAPEX-PO-000117"),
    ("BOOKS_INVENTORY", "PO-BKS-6001", "CAPEX-PO-000044"),
])
def test_an_update_re_sends_the_dedupe_key(product, external_id, key):
    """An update must never strip the only handle the retry path has.

    If it did, the next lost response would have nothing to resolve by and the
    purchase order would become unlinkable again -- the same defect, reopened
    by the very call that was meant to close it.
    """
    instance = build(product)
    instance.update_purchase_order(external_id, _draft_po(product), key)
    sent = instance.transport.request_log[-1]
    assert sent["method"] == "PUT"
    assert external_id in sent["path"]
    assert {"api_name": DEDUPE_CUSTOM_FIELD, "value": key} in sent["body"]["custom_fields"]


@pytest.mark.parametrize("product", PRODUCTS)
def test_an_update_without_an_id_or_a_key_is_refused(product):
    """Both refusals are the same refusal: never act on an unnamed record."""
    instance = build(product)
    with pytest.raises(IntegrationError):
        instance.update_purchase_order("", _draft_po(product), "CAPEX-1")
    with pytest.raises(IntegrationError):
        instance.update_purchase_order("PO-1", _draft_po(product), "")


@pytest.mark.parametrize("product,external_id", [
    ("ERP", "PO-ERP-5001"),
    ("BOOKS_INVENTORY", "PO-BKS-6001"),
])
def test_the_draft_to_open_transition_is_ours_to_make(product, external_id):
    """§11.7: emitted draft, moved to open by OUR call after approval closes."""
    instance = build(product)
    assert instance.transition_purchase_order(external_id, "open", actor="U-1") == "open"
    sent = instance.transport.request_log[-1]
    assert sent["method"] == "POST"
    assert sent["path"].endswith("/status/open")


@pytest.mark.parametrize("product,external_id", [
    ("ERP", "PO-ERP-5001"),
    ("BOOKS_INVENTORY", "PO-BKS-6001"),
])
def test_an_unsupported_transition_is_refused_not_synthesised(product, external_id):
    """`billed` and `cancelled` exist on the product and are the tenant's to set.

    A Zoho-side status change we did not initiate is an exception, not an
    outcome -- so this adapter must not offer a path to make one.
    """
    instance = build(product)
    with pytest.raises(CapabilityError):
        instance.transition_purchase_order(external_id, "billed", actor="U-1")
    assert not instance.transport.request_log, (
        "A refused transition must not have reached the wire first.")


@pytest.mark.parametrize("product,external_id", [
    ("ERP", "PO-ERP-5001"),
    ("BOOKS_INVENTORY", "PO-BKS-6001"),
])
def test_a_transition_requires_an_attributable_actor(product, external_id):
    """Zoho attributes the change to the OAuth identity; our audit needs ours."""
    with pytest.raises(IntegrationError):
        build(product).transition_purchase_order(external_id, "open", actor="")


def test_the_books_transition_path_is_labelled_not_confirmed():
    """The evidence status, kept where it cannot be mistaken for a fact.

    §11.2's Books row confirms create and update and is silent on status
    operations, and the ERP bundle that documents `status/open` is inadmissible
    for Books. The control is implemented so it exists; the label says it needs
    the tenant.
    """
    assert "NOT CONFIRMED" in bi.PO_TRANSITION_EVIDENCE
    assert "0B" in bi.PO_TRANSITION_EVIDENCE


# ================================================ nothing sensitive is logged
def test_no_adapter_call_puts_a_token_or_a_full_payload_in_an_exception(adapter):
    """Errors are read by people who are not entitled to the payload.

    The cassette transport's own error names the request key, never the body,
    and no adapter error interpolates a credential. Asserted because the
    tempting debug aid -- printing the response -- is exactly the leak.
    """
    with pytest.raises(IntegrationError) as exc:
        adapter.get_bill("NO-SUCH-BILL")
    message = str(exc.value)
    for secret in ("Zoho-oauthtoken", "refresh_token", "client_secret",
                   "access_token", "Authorization"):
        assert secret not in message


def test_the_adapter_modules_never_log_and_never_hold_a_credential():
    """Source-level, so it holds for paths no test exercised.

    A logger reached for during a 3 a.m. incident is how a token ends up in a
    log aggregator. There is no logging in these modules at all, which is the
    easiest version of this rule to keep true.
    """
    import app.backend.integration.adapter as adapter_mod

    for module in (adapter_mod, erp_module, bi):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("logging.getLogger", "logger.", "print(",
                       "client_secret", "refresh_token", "access_token"):
            assert banned not in source, (
                f"{Path(module.__file__).name} contains {banned!r}. Tokens, "
                f"credentials and full payloads must never be logged.")


def test_no_cassette_contains_a_credential():
    """The fixtures are checked in. A token in one is a token in the repository."""
    import json

    for path in sorted(CASSETTES.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        for banned in ("Zoho-oauthtoken", "client_secret", "refresh_token",
                       "access_token", "Authorization"):
            assert banned not in text, f"{path} carries {banned!r}."
        # And every one still declares it was invented, including the new ones.
        assert json.loads(text)["provenance"] == "INVENTED-SANITISED", path

