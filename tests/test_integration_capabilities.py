"""Capabilities must drive behaviour, not merely describe it.

A capability flag that nothing reads is documentation with a type annotation.
Plan v1.2.1 §11.10 makes a stronger claim -- *"the scheduler reads
``capabilities()`` to choose a strategy; ``receives_listable=False`` selects
PO-anchored discovery"* -- and this file holds that claim to account.

The sharp case is §11.4. **Zoho ERP Purchase Receives has four endpoints and no
list endpoint**: create, update, delete, fetch-one. A caller handed an ERP
adapter must not be able to reach a receives list path *at all* -- not because
it would 404, but because there is nothing to call. So the proof here is a
negative, and negatives need evidence: the cassette transport records every
request the adapter attempted, and the tests assert against that log rather
than against a return value.

No network. No tenant. Cassettes are hand-authored; see
``tests/cassettes/README.md``.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend.integration import (
    BooksInventoryAdapter,
    Capabilities,
    CapabilityError,
    CassetteTransport,
    ErpAdapter,
    ReceiveDTO,
    acquire_receives,
    receives_strategy,
)
from app.backend.integration import books_inventory as bi
from app.backend.integration import erp as erp_module
from app.backend.integration.adapter import window_params

CASSETTES = Path(__file__).resolve().parent / "cassettes"
SINCE = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def erp():
    return ErpAdapter(organization_id="60000000001", dc="IN", per_page=2,
                      transport=CassetteTransport("ERP", CASSETTES))


@pytest.fixture
def books():
    return BooksInventoryAdapter(
        organization_id="60000000002", dc="IN", per_page=2,
        transport=CassetteTransport("BOOKS_INVENTORY", CASSETTES))


# ============================================ what the two products declare
def test_the_two_products_declare_the_gap_the_plan_records(erp, books):
    assert erp.capabilities().receives_listable is False       # §11.4
    assert books.capabilities().receives_listable is True      # Inventory
    assert erp.capabilities().items_delta_filter is False      # weekly full refresh
    assert books.capabilities().items_delta_filter is True     # Inventory true delta
    assert erp.capabilities().bills_delta_filter is True
    assert books.capabilities().bills_delta_filter is True


def test_an_unverified_capability_is_assumed_absent(erp):
    """D-7 is not verified at the tenant, so line-level custom fields are False.

    Assuming a capability present because it is documented is how a design gets
    built on a feature the tenant does not have. The default is the pessimistic
    one and the optimistic one has to be passed in explicitly.
    """
    assert erp.capabilities().line_level_custom_fields is False
    assert ErpAdapter(organization_id="1",
                      line_level_custom_fields=True).capabilities(
                          ).line_level_custom_fields is True


def test_the_binding_ceiling_on_erp_standard_is_the_daily_one(erp):
    """100/min is shared by all three products; 2,000/day is what actually binds.

    A PO-anchored GRN sweep costs one call per open PO plus one per receive, so
    on ERP Standard the daily ceiling -- not the per-minute one -- is what
    decides whether the sweep can run at the cadence the client wants.
    """
    assert erp.capabilities().daily_call_ceiling == 2000
    assert erp_module.DAILY_CALL_CEILING_STANDARD == 2000
    assert erp_module.DAILY_CALL_CEILING_PREMIUM == 10000


# ================================ the list path is unreachable, not just unused
def test_the_erp_adapter_has_no_receives_list_method_at_all(erp):
    """Structural, not defensive. There is no method to call and no guard to skip."""
    assert not hasattr(erp, "list_receives")
    assert getattr(erp, "list_receives", None) is None
    assert receives_strategy(erp) == "PO_ANCHORED"


def test_the_erp_module_defines_no_receives_collection_constant():
    """A constant naming a collection endpoint would be a fabrication.

    ERP's Purchase Receives module documents create, update, delete and
    fetch-one. Writing ``PATH_PURCHASE_RECEIVES = "/purchasereceives"`` here
    would invent a fifth endpoint, and the first person to reach for it would
    have no reason to doubt it.
    """
    source = Path(erp_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert "PATH_PURCHASE_RECEIVE" in assigned, "the fetch-one path must exist"
    assert "PATH_PURCHASE_RECEIVES" not in assigned, (
        "erp.py defines a receives COLLECTION constant. ERP has no such "
        "endpoint; naming one invents it.")
    # And the same fact from the other side: Inventory really does have one.
    assert bi.PATH_PURCHASE_RECEIVES == "/purchasereceives"


def test_acquiring_receives_on_erp_never_touches_a_collection_path(erp):
    """The negative, proved from the request log rather than from a return value.

    Every request the adapter attempted is recorded. Not one of them may be a
    receives collection -- each must be either the PO detail that names the
    receives or a fetch-one for a named receive.
    """
    receives = acquire_receives(erp, open_po_external_ids=["PO-ERP-5001"])
    assert len(receives) == 2
    assert all(isinstance(r, ReceiveDTO) for r in receives)

    paths = [entry["path"] for entry in erp.transport.request_log]
    assert paths == [
        "/purchaseorders/PO-ERP-5001",
        "/purchasereceives/GRN-ERP-9001",
        "/purchasereceives/GRN-ERP-9002",
    ]
    assert "/purchasereceives" not in paths, (
        "An ERP adapter reached a receives collection path, which does not exist.")


def test_po_anchored_discovery_costs_one_call_per_po_plus_one_per_receive(erp):
    """The cost model §11.4 says must be sized against the client's open POs."""
    acquire_receives(erp, open_po_external_ids=["PO-ERP-5001"])
    assert len(erp.transport.request_log) == 1 + 2


def test_acquiring_receives_on_books_inventory_uses_the_list_path(books):
    """The same call, the other product, a different mechanism -- chosen by the flag."""
    receives = acquire_receives(books)
    assert {r.external_id for r in receives} == {"GRN-INV-9501", "GRN-INV-9502"}
    assert receives_strategy(books) == "LIST"
    assert [e["path"] for e in books.transport.request_log] == ["/purchasereceives"]


def test_per_po_selection_on_inventory_is_done_locally_not_by_a_filter(books):
    """The C1 ``receives_for_po`` still works on this product -- differently.

    Inventory documents no ``purchaseorder_id`` filter on the receives list, so
    the selection happens here after the response. Sending an undocumented
    filter parameter would be ignored rather than rejected, and the caller
    would read a full unfiltered page as "the receives for this PO" --
    silently attributing another PO's goods receipts to this one.
    """
    matched = books.receives_for_po("PO-BKS-6001")
    assert [r.external_id for r in matched] == ["GRN-INV-9501"]
    sent = books.transport.request_log[-1]["params"]
    assert "purchaseorder_id" not in sent
    assert set(sent) == {"organization_id", "page", "per_page",
                         "sort_column", "sort_order"}


def test_erp_receives_are_undiscoverable_without_a_known_po(erp):
    """§11.4's first consequence, stated as behaviour.

    With no open POs there is nothing to anchor to, so nothing is found -- and
    that is the correct answer, not a bug. A receive against a PO we do not
    know about surfaces only when its bill arrives, as an
    UNSANCTIONED_COMMITMENT exception.
    """
    assert acquire_receives(erp, open_po_external_ids=[]) == ()
    assert erp.transport.request_log == []


# ================================= a capability flag cannot lie in either direction
class _LyingAdapter:
    """A fake that declares a capability inconsistent with its own surface."""

    product = "ERP"

    def __init__(self, *, receives_listable: bool, expose_list: bool):
        self._caps = Capabilities(
            receives_listable=receives_listable, bills_delta_filter=True,
            po_delta_filter=True, items_delta_filter=False,
            line_level_custom_fields=False, daily_call_ceiling=2000)
        if expose_list:
            self.list_receives = lambda page=1: None

    def capabilities(self) -> Capabilities:
        return self._caps

    def receives_for_po(self, po_external_id: str):
        return []


def test_declaring_a_list_capability_without_one_is_refused():
    with pytest.raises(CapabilityError) as exc:
        acquire_receives(_LyingAdapter(receives_listable=True, expose_list=False))
    assert "no list_receives" in str(exc.value)


def test_exposing_a_list_path_on_a_product_that_has_none_is_refused():
    """The direction that matters: this is how a caller would reach a list path.

    Bolting ``list_receives`` onto the ERP adapter while leaving
    ``receives_listable=False`` is the exact shape of the mistake §11.4 is
    written to prevent -- and it fails loudly here rather than 404-ing against
    a tenant.
    """
    with pytest.raises(CapabilityError) as exc:
        acquire_receives(_LyingAdapter(receives_listable=False, expose_list=True),
                         open_po_external_ids=["PO-1"])
    assert "cannot exist" in str(exc.value)


# ================================== the delta filter is a capability, not a habit
def test_a_disabled_delta_filter_sends_no_filter_at_all():
    """``po_delta_filter=False`` selects a full re-pull, and looks like one.

    Inventory's ``GET /purchaseorders`` accepts ``organization_id``, ``page``
    and ``per_page`` and nothing else. Sending it a ``last_modified_time``
    anyway would be worse than useless: an unrecognised query parameter is
    ignored rather than rejected, so the call would return everything while
    the caller believed it had a delta.
    """
    enabled = window_params(enabled=True, since=SINCE,
                            parameter="last_modified_time", formatter=str)
    disabled = window_params(enabled=False, since=SINCE,
                             parameter="last_modified_time", formatter=str)
    assert set(enabled) == {"last_modified_time"}
    assert disabled == {}


def test_inventorys_purchase_order_list_refuses_a_parameter_it_does_not_document(books):
    """The Inventory constraint is enforced, not just recorded."""
    assert bi.INVENTORY_PURCHASE_ORDER_LIST_PARAMS == frozenset(
        {"organization_id", "page", "per_page"})
    with pytest.raises(CapabilityError) as exc:
        books._get(bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_ORDERS,
                   "ZohoInventory.purchaseorders.READ",
                   {"page": 1, "last_modified_time": "2026-08-26T00:00:00Z"})
    assert "would be ignored rather than rejected" in str(exc.value)


def test_the_enabled_filter_actually_reaches_the_request(erp):
    erp.list_bills(SINCE, UNTIL, page=1)
    sent = erp.transport.request_log[-1]["params"]
    assert sent["last_modified_time"].startswith("2026-08-26T00:00:00")
    assert sent["organization_id"] == "60000000001"


# =========================== filterable is not sortable, and the code knows it
def test_last_modified_time_is_filterable_and_not_sortable_on_bills_and_pos():
    """§11.3. The two are conflated at the cost of a keyset walk that cannot exist."""
    assert erp_module.PATH_BILLS in erp_module.DELTA_FILTERABLE
    assert erp_module.PATH_PURCHASE_ORDERS in erp_module.DELTA_FILTERABLE
    assert "last_modified_time" not in erp_module.SORT_COLUMNS[erp_module.PATH_BILLS]
    assert "last_modified_time" not in erp_module.SORT_COLUMNS[
        erp_module.PATH_PURCHASE_ORDERS]
    assert "last_modified_time" not in bi.SORT_COLUMNS[
        (bi.SERVICE_BOOKS, bi.PATH_BILLS)]
    assert "last_modified_time" not in bi.SORT_COLUMNS[
        (bi.SERVICE_BOOKS, bi.PATH_PURCHASE_ORDERS)]


def test_inventory_items_is_the_one_endpoint_where_it_is_both():
    """The asymmetry, asserted so it cannot be generalised in either direction."""
    assert "last_modified_time" in bi.SORT_COLUMNS[(bi.SERVICE_INVENTORY, bi.PATH_ITEMS)]
    assert bi.SORT_COLUMNS[(bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_ORDERS)] == frozenset()


def test_asking_to_sort_by_modification_time_is_refused(erp, books):
    with pytest.raises(CapabilityError) as exc:
        erp._get(erp_module.PATH_BILLS, "ERP.bills.READ",
                 {"sort_column": "last_modified_time"})
    assert "filterable and NOT sortable" in str(exc.value)

    with pytest.raises(CapabilityError):
        books._get(bi.SERVICE_BOOKS, bi.PATH_PURCHASE_ORDERS,
                   "ZohoBooks.purchaseorders.ALL",
                   {"sort_column": "last_modified_time"})


def test_the_inventory_receives_walk_sorts_by_a_column_that_endpoint_allows(books):
    """And that column is the document date, with the consequence stated.

    Inventory's receives list takes a sort column and no filter, so a
    descending walk with an early stop is the only delta-shaped access it
    offers. It sorts on ``date``, not modification time -- so a back-dated edit
    to an old receive is not caught by the early stop, which is why the nightly
    completeness sweeps are retained rather than replaced.
    """
    books.list_receives(page=1)
    sent = books.transport.request_log[-1]["params"]
    assert sent["sort_column"] == "date"
    assert sent["sort_order"] == "D"
    assert sent["sort_column"] in bi.SORT_COLUMNS[
        (bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_RECEIVES)]
    assert "last_modified_time" not in bi.SORT_COLUMNS[
        (bi.SERVICE_INVENTORY, bi.PATH_PURCHASE_RECEIVES)], (
        "Not confirmed as a sort column on this endpoint; borrowing it from "
        "Inventory items would be exactly the cross-endpoint inference §11 rules out.")
