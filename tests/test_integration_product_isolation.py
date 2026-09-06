"""Products are never mixed -- as a build failure, not a convention.

Plan v1.2.1 §11, first line: *"Products are never mixed. Books and Inventory
documentation is inadmissible as evidence for ERP, and vice versa."* §11.10
closes with the requirement this file discharges: *"a test asserts that a Books
cassette can never satisfy an ERP adapter test."*

Why this needs three locks rather than one
------------------------------------------
The obvious implementation is to label each cassette with a product and check
the label. That fails the moment somebody edits the label -- and the reason
somebody would is precisely the reason the rule exists: the payloads look
almost identical. A Books bill and an ERP bill carry the same field names, the
same ``page_context``, the same ``last_modified_time``. Shape proves nothing.

So the transport checks three things that come from *different* places, and any
one of them refuses on its own:

1. the declared ``product``, cross-checked against the directory the cassette
   was loaded from -- so a Books recording cannot simply be filed under ``erp/``;
2. the recorded ``base_url`` -- ``/erp/v3`` is not ``/books/v3``, and no amount
   of relabelling changes which host answered;
3. the recorded OAuth ``scope`` -- ``ERP.``, ``ZohoBooks.`` and
   ``ZohoInventory.`` are the products' own names, so a scope-prefix mismatch
   *is* a product mismatch.

Each of the tests below defeats the locks in front of it and asserts the next
one still fires. The honest limit is stated in
``test_forging_all_three_locks_is_what_it_takes``: a determined forger who
rewrites the product, the host and the scope has not smuggled Books evidence
into an ERP test -- they have written an ERP cassette, and the diff says so.

No network. No tenant. Every cassette in this repository is hand-authored and
labelled ``INVENTED-SANITISED``; see ``tests/cassettes/README.md``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend.integration import (
    BooksInventoryAdapter,
    CassetteError,
    CassetteTransport,
    ErpAdapter,
    IntegrationError,
    ProductMixingError,
)

CASSETTES = Path(__file__).resolve().parent / "cassettes"
ERP_DIR = CASSETTES / "erp"
BOOKS_DIR = CASSETTES / "books_inventory"

SINCE = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _cassette_files() -> list[Path]:
    return sorted(CASSETTES.rglob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(root: Path, product_dir: str, name: str, cassette: dict) -> Path:
    target = root / product_dir
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_text(json.dumps(cassette, indent=2), encoding="utf-8")
    return path


def erp_adapter(root: Path = CASSETTES) -> ErpAdapter:
    return ErpAdapter(organization_id="60000000001", dc="IN", per_page=2,
                      transport=CassetteTransport("ERP", root))


def books_adapter(root: Path = CASSETTES) -> BooksInventoryAdapter:
    return BooksInventoryAdapter(organization_id="60000000002", dc="IN", per_page=2,
                                 transport=CassetteTransport("BOOKS_INVENTORY", root))


# ===================================================================== lock 1
def test_a_books_cassette_filed_under_erp_is_refused_at_load(tmp_path):
    """The directory is evidence. Filing Books traffic under ``erp/`` fails."""
    books = _load(BOOKS_DIR / "books_bills_list_page1.json")
    _write(tmp_path, "erp", "smuggled.json", books)

    with pytest.raises(ProductMixingError) as exc:
        CassetteTransport("ERP", tmp_path)
    assert "products are never mixed" in str(exc.value).lower()


def test_an_erp_cassette_filed_under_books_is_refused_at_load(tmp_path):
    """The same lock in the other direction. Neither product is privileged."""
    erp = _load(ERP_DIR / "bills_list_page1.json")
    _write(tmp_path, "books_inventory", "smuggled.json", erp)

    with pytest.raises(ProductMixingError):
        CassetteTransport("BOOKS_INVENTORY", tmp_path)


def test_an_unlabelled_directory_is_refused(tmp_path):
    """A cassette that is not filed under a product directory has no product."""
    _write(tmp_path, "misc", "loose.json", _load(ERP_DIR / "bills_list_page1.json"))
    with pytest.raises(CassetteError):
        CassetteTransport("ERP", tmp_path)


# ===================================================================== lock 2
def test_a_relabelled_books_cassette_still_cannot_satisfy_an_erp_adapter(tmp_path):
    """Defeat lock 1 and lock 2 fires.

    This is the test §11.10 asks for, in its hardest form: the cassette now
    *claims* to be ERP and sits in the ERP directory, and it is still refused,
    because it was recorded against ``https://www.zohoapis.in/books/v3`` and an
    ERP adapter can only ever request ``https://www.zohoapis.in/erp/v3``.
    """
    books = _load(BOOKS_DIR / "books_bills_list_page1.json")
    books["product"] = "ERP"                       # the forgery
    _write(tmp_path, "erp", "relabelled.json", books)

    adapter = erp_adapter(tmp_path)                # loads cleanly now
    with pytest.raises(ProductMixingError) as exc:
        adapter.list_bills(SINCE, UNTIL, page=1)
    assert "/books/v3" in str(exc.value)
    assert "inadmissible" in str(exc.value).lower()


def test_a_relabelled_erp_cassette_still_cannot_satisfy_a_books_adapter(tmp_path):
    erp = _load(ERP_DIR / "bills_list_page1.json")
    erp["product"] = "BOOKS_INVENTORY"
    _write(tmp_path, "books_inventory", "relabelled.json", erp)

    adapter = books_adapter(tmp_path)
    with pytest.raises(ProductMixingError) as exc:
        adapter.list_bills(SINCE, UNTIL, page=1)
    assert "/erp/v3" in str(exc.value)


# ===================================================================== lock 3
def test_a_relabelled_rehosted_books_cassette_is_still_caught_by_its_scope(tmp_path):
    """Defeat locks 1 and 2 and lock 3 fires.

    The scope prefixes are the products' own names. A body that was authorised
    by ``ZohoBooks.bills.READ`` was, by definition, served by Zoho Books --
    whatever the file now says about its product or its host.
    """
    books = _load(BOOKS_DIR / "books_bills_list_page1.json")
    books["product"] = "ERP"
    books["request"]["base_url"] = "https://www.zohoapis.in/erp/v3"
    _write(tmp_path, "erp", "rehosted.json", books)

    adapter = erp_adapter(tmp_path)
    with pytest.raises(ProductMixingError) as exc:
        adapter.list_bills(SINCE, UNTIL, page=1)
    assert "ZohoBooks.bills.READ" in str(exc.value)


def test_a_relabelled_rehosted_erp_cassette_is_still_caught_by_its_scope(tmp_path):
    erp = _load(ERP_DIR / "bills_list_page1.json")
    erp["product"] = "BOOKS_INVENTORY"
    erp["request"]["base_url"] = "https://www.zohoapis.in/books/v3"
    _write(tmp_path, "books_inventory", "rehosted.json", erp)

    adapter = books_adapter(tmp_path)
    with pytest.raises(ProductMixingError) as exc:
        adapter.list_bills(SINCE, UNTIL, page=1)
    assert "ERP.bills.READ" in str(exc.value)


def test_forging_all_three_locks_is_what_it_takes(tmp_path):
    """The honest limit of this control, stated rather than left implied.

    Rewrite the product, the host *and* the scope and the cassette is accepted
    -- because at that point it is no longer Books evidence being passed off as
    ERP evidence. It is an ERP cassette, authored by whoever made those three
    edits, and every one of them is visible in the diff.

    That is the correct place for the boundary. A control that tried to go
    further would have to judge payload *shape*, and shape is exactly what
    cannot distinguish these two products: the assertion below shows the Books
    body maps cleanly through the ERP mapper, which is why none of the three
    locks looks at it.
    """
    books = _load(BOOKS_DIR / "books_bills_list_page1.json")
    books["product"] = "ERP"
    books["request"]["base_url"] = "https://www.zohoapis.in/erp/v3"
    books["request"]["scope"] = "ERP.bills.READ"
    _write(tmp_path, "erp", "fully_forged.json", books)

    page = erp_adapter(tmp_path).list_bills(SINCE, UNTIL, page=1)
    assert page.items[0].external_id == "BILL-BKS-2001"
    assert page.items[0].source.product == "ERP"


# ================================================= segregation of the real set
def test_the_shipped_cassette_library_is_segregated_on_disk():
    """Every real cassette declares the product of the directory it lives in."""
    for path in _cassette_files():
        cassette = _load(path)
        expected = CassetteTransport.DIRECTORY_PRODUCT[path.parent.name]
        assert cassette["product"] == expected, (
            f"{path} declares {cassette['product']!r} in a {expected!r} directory.")


def test_no_erp_cassette_mentions_another_products_scope_or_host():
    """A single stray ``ZohoBooks.`` in an ERP fixture is a mixed product."""
    for path in sorted(ERP_DIR.glob("*.json")):
        cassette = _load(path)
        request = cassette["request"]
        assert request["scope"].startswith("ERP."), path
        assert request["base_url"].endswith("/erp/v3"), path
        # The response body itself must carry no other product's markers. The
        # provenance note is prose and is exempt: it is allowed -- encouraged --
        # to explain what this fixture is NOT.
        body = json.dumps(cassette["response"])
        for foreign in ("ZohoBooks.", "ZohoInventory.", "/books/v3", "/inventory/v1"):
            assert foreign not in body, f"{path} carries {foreign!r} in its response."


def test_no_books_inventory_cassette_is_authorised_by_an_erp_scope():
    for path in sorted(BOOKS_DIR.glob("*.json")):
        request = _load(path)["request"]
        assert request["scope"].startswith(("ZohoBooks.", "ZohoInventory.")), path
        assert not request["scope"].startswith("ERP."), path
        assert request["base_url"].endswith(("/books/v3", "/inventory/v1")), path


def test_service_and_product_are_kept_distinct_on_every_cassette():
    """``BOOKS_INVENTORY`` is one product served by two services.

    Collapsing ``service`` into ``product`` would let a Books fact become
    evidence for an Inventory claim inside a single adapter -- the same error
    §11 forbids across adapters, one level down.
    """
    allowed = {"ERP": {"erp"}, "BOOKS_INVENTORY": {"books", "inventory"}}
    seen: dict[str, set[str]] = {"ERP": set(), "BOOKS_INVENTORY": set()}
    for path in _cassette_files():
        cassette = _load(path)
        service = cassette["service"]
        assert service in allowed[cassette["product"]], f"{path}: {service}"
        seen[cassette["product"]].add(service)
    assert seen["BOOKS_INVENTORY"] == {"books", "inventory"}, (
        "The Books+Inventory fixtures must exercise both services, or the "
        "two-service design is untested.")


# ================================================ provenance and transports
def test_every_cassette_declares_that_it_was_invented_not_recorded():
    """No tenant has been contacted, and the fixtures must say so.

    If a real recording is ever added, this label has to change -- which makes
    "we called a live tenant" a visible diff rather than a silent one.
    """
    files = _cassette_files()
    assert files, "There are no cassettes; the adapter suite would prove nothing."
    for path in files:
        cassette = _load(path)
        assert cassette["provenance"] == "INVENTED-SANITISED", path
        assert cassette["provenance_note"].strip(), path


def test_an_adapter_refuses_a_transport_belonging_to_the_other_product():
    """The mismatch is caught at construction, before a request is shaped."""
    with pytest.raises(IntegrationError) as exc:
        ErpAdapter(organization_id="1",
                   transport=CassetteTransport("BOOKS_INVENTORY", CASSETTES))
    assert "never mixed" in str(exc.value).lower()

    with pytest.raises(IntegrationError):
        BooksInventoryAdapter(organization_id="1",
                              transport=CassetteTransport("ERP", CASSETTES))


def test_a_transport_serves_only_its_own_products_cassettes():
    """Loading the shared root is fine; serving across it is not.

    Both transports read the same directory, and each ignores the other's
    files entirely -- so an ERP adapter asking for a path only Books has gets
    "no cassette", never another product's body.
    """
    erp_transport = CassetteTransport("ERP", CASSETTES)
    with pytest.raises(CassetteError):
        erp_transport.request(
            method="GET", base_url="https://www.zohoapis.in/inventory/v1",
            path="/purchasereceives", scope="ZohoInventory.purchasereceives.READ")


def test_the_two_adapters_share_no_scope_and_no_base_url():
    """The products' surfaces are disjoint, so nothing can be reused by accident."""
    erp = ErpAdapter(organization_id="1")
    books = BooksInventoryAdapter(organization_id="1")
    assert not (erp.scopes_required() & books.scopes_required())
    assert erp.base_url("IN") != books.base_url("IN")
    assert erp.base_url("IN") != books.service_base_url("inventory", "IN")
    assert all(s.startswith("ERP.") for s in erp.scopes_required())
    assert all(s.startswith(("ZohoBooks.", "ZohoInventory."))
               for s in books.scopes_required())
