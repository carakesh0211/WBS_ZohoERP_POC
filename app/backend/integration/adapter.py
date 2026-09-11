"""The product-agnostic adapter boundary (plan v1.2.1 §11.10, Wave 5 seam C1).

**The target product is provisional.** D-14 is unresolved: the handwritten
notes say "Zoho ERP", but clients routinely say that about a Books + Inventory
estate, and until ``GET /organizations`` answers in Phase 0B-2 nobody knows
which it is. So the integration layer is built against an interface, and the
two candidates are two implementations behind it -- :mod:`.erp` and
:mod:`.books_inventory`.

Nothing outside those two modules may learn which product is in use. No base
URL, scope string or endpoint literal exists anywhere else in the codebase,
and ``tests/test_integration_no_hardcoded_endpoints.py`` makes that a build
failure rather than a habit.

What this module contains
-------------------------
* :class:`ProcurementAdapter` -- the frozen C1 Protocol.
* :class:`Capabilities` -- the honest part. It declares what a product
  **cannot** do, and the platform reads it instead of assuming.
* :class:`Transport` -- the seam through which a request leaves. The default
  is :class:`NoNetworkTransport`, which refuses, because Phase 0B has not
  cleared and no live call may be made without explicit authorisation.
* :class:`CassetteTransport` -- the in-process fake that replays recorded,
  sanitised cassettes, and which enforces product isolation with three
  independent locks (see :class:`ProductMixingError`).
* :func:`acquire_receives` -- the capability-driven dispatcher. A caller
  cannot reach a receives *list* path on a product that has none, because on
  such a product the method does not exist and this function refuses to
  synthesise one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import quote, urlsplit

from app.backend.integration.dto import (
    PRODUCTS,
    BillDTO,
    ContactDTO,
    ItemDTO,
    Page,
    Product,
    PurchaseOrderDTO,
    ReceiveDTO,
)

__all__ = [
    "CassetteError",
    "DEDUPE_SCAN_PAGE_LIMIT",
    "CassetteTransport",
    "Capabilities",
    "CapabilityError",
    "IntegrationError",
    "NetworkForbidden",
    "NoNetworkTransport",
    "POLL_OVERLAP_SECONDS",
    "ProcurementAdapter",
    "ProductMixingError",
    "RecoverableProcurementAdapter",
    "Transport",
    "UnsupportedDataCentre",
    "acquire_receives",
    "encode_query",
    "verified_dedupe_match",
    "page_context",
    "window_params",
]

#: §11.3. Every poll re-reads 300 s it has already seen, because
#: ``last_modified_time`` is filterable but NOT sortable on ERP/Books bills and
#: POs -- so a window can be selected but not walked as a stable keyset. The
#: overlap is free: ``integration_inbox``'s ``payload_sha`` uniqueness discards
#: the duplicates. This constant belongs to the boundary, not to a caller, so
#: that no scheduler can quietly shrink it.
POLL_OVERLAP_SECONDS = 300

#: §11.6: "Zoho documents no idempotency header" -- on any of the three
#: products. So there is no header constant here to reach for, and outbound
#: dedupe is synthesised into a unique custom field instead (Z-01). A retry
#: after an unrecorded send updates by this field's unique value rather than
#: creating a second commitment.
DEDUPE_CUSTOM_FIELD = "cf_capex_ref"

#: How many purchase-order pages :meth:`resolve_by_dedupe_key` may walk when
#: the product documents no server-side search on the dedupe custom field.
#:
#: A hard stop, for the same reason :func:`walk_pages` has one: a resolve runs
#: on the outbound retry path, and an unbounded scan there would spend the
#: whole daily ceiling (2,000 calls on ERP Standard) trying to recover a single
#: purchase order. Running out of pages returns ``None`` -- "not found within
#: the budget", which the caller must not read as "does not exist".
DEDUPE_SCAN_PAGE_LIMIT = 10


# ================================================================== exceptions
class IntegrationError(RuntimeError):
    """Base for every failure raised by the adapter boundary."""


class CapabilityError(IntegrationError):
    """A caller tried to use a path the product does not have.

    Raised rather than degraded. §11.4's whole point is that a missing
    capability is a *declared* gap the platform routes around, not something a
    caller discovers as a 404 in production.
    """


class UnsupportedDataCentre(IntegrationError):
    """A DC whose host is not documented for this product.

    ERP is the sharp case: ``https://www.zohoapis.in/erp/v3`` is the only host
    Zoho publishes, and a ``.com``/``.eu`` ERP host is NOT CONFIRMED. Guessing
    one would be exactly the "documentation for one product is evidence for
    another" error §11 forbids.
    """


class NetworkForbidden(IntegrationError):
    """A request tried to leave the process.

    Phase 0B has not cleared. No live Zoho tenant, no Catalyst, no cloud
    resource of any kind. This is raised by the default transport so that the
    failure mode of a forgotten test double is a loud error, not a live call.
    """


class CassetteError(IntegrationError):
    """A cassette is missing, malformed, or does not match the request."""


class ProductMixingError(IntegrationError):
    """Evidence from one product was offered to another product's adapter.

    §11, first line: "Products are never mixed. Books and Inventory
    documentation is inadmissible as evidence for ERP, and vice versa."

    :class:`CassetteTransport` enforces that with three *independent* locks, so
    that defeating one does not defeat the rule:

    1. the cassette's declared ``product``, cross-checked against the directory
       it was loaded from;
    2. the ``base_url`` recorded on the cassette against the one the adapter
       actually built -- ``/erp/v3`` can never satisfy ``/books/v3``;
    3. the OAuth ``scope`` recorded on the cassette against the one the adapter
       presented -- an ``ERP.`` scope can never satisfy a ``ZohoBooks.`` one.

    Locks 2 and 3 survive someone editing the ``product`` label, which is the
    point: the rule is enforced by what the evidence *is*, not by what it says
    about itself.
    """


# ================================================================ capabilities
@dataclass(frozen=True)
class Capabilities:
    """What a product cannot do, stated rather than discovered.

    Frozen by the Wave 5 C1 seam at exactly these six fields. The scheduler
    reads this instead of assuming: ``receives_listable=False`` selects
    PO-anchored discovery, ``po_delta_filter=False`` selects a full re-pull.
    So §11.4 -- ERP Purchase Receives having no collection endpoint at all --
    is handled as a declared capability gap, and switching target products
    after D-14 is a configuration change plus one adapter, not a redesign.

    **Amended after the freeze** with a seventh field, ``po_dedupe_search``.
    The six frozen fields keep their names, their order and their positional
    meaning; the new one carries a default, so every existing construction --
    positional or keyword -- still compiles and still means what it meant. The
    freeze existed to stop the first six shifting under callers, and they have
    not. See ``tests/ADAPTATIONS.md``.
    """
    receives_listable: bool          # ERP: False.  Inventory: True
    bills_delta_filter: bool         # both: True
    po_delta_filter: bool            # ERP/Books: True.  Inventory: False
    items_delta_filter: bool         # Inventory: True.  ERP/Books: False
    line_level_custom_fields: bool   # D-7, tenant-verified -- assume False
    daily_call_ceiling: int          # plan-derived -- assume ERP Standard 2000
    # --- added after the freeze; see the class docstring and ADAPTATIONS.md ---
    #: Whether the product documents a *server-side* search of the purchase
    #: order list by the unique dedupe custom field (``cf_capex_ref``).
    #:
    #: ERP: True -- Zoho's own published OpenAPI bundle documents a
    #: ``custom_field`` query parameter on ``GET /purchaseorders``.
    #: BOOKS_INVENTORY: False -- no such parameter is documented for Books in
    #: this repository, and sending one that is not documented would be
    #: *ignored rather than rejected*, turning an unfiltered first page into
    #: something a caller would read as "the PO carrying this key".
    #:
    #: False does NOT mean the key cannot be resolved. It means resolution
    #: costs a bounded scan instead of one call -- and, either way, a candidate
    #: is adopted only after :func:`verified_dedupe_match` confirms it actually
    #: carries the key.
    po_dedupe_search: bool = False


# =============================================================== the transport
@runtime_checkable
class Transport(Protocol):
    """The seam through which a request leaves the adapter.

    The adapter builds the URL, the query and the scope; the transport decides
    whether anything actually happens. That split is what lets every endpoint
    literal stay inside :mod:`.erp` and :mod:`.books_inventory` while the
    boundary above stays product-blind.
    """
    product: str

    def request(
        self,
        *,
        method: str,
        base_url: str,
        path: str,
        scope: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        ...


class NoNetworkTransport:
    """The default. Refuses every request.

    An adapter constructed without a transport can still answer
    :meth:`~ProcurementAdapter.base_url`,
    :meth:`~ProcurementAdapter.scopes_required` and
    :meth:`~ProcurementAdapter.capabilities` -- the pure metadata a consent
    screen and a scheduler need -- and can fetch nothing at all.
    """

    def __init__(self, product: str) -> None:
        self.product = product

    def request(
        self,
        *,
        method: str,
        base_url: str,
        path: str,
        scope: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        raise NetworkForbidden(
            f"Refusing {method} {base_url}{path}. No live Zoho tenant, Catalyst "
            f"or cloud resource may be contacted: Phase 0B has not cleared and "
            f"no live call is made without explicit authorisation. Supply a "
            f"CassetteTransport in tests.")


def encode_query(params: Mapping[str, Any]) -> str:
    """Canonical query string, with ``+`` percent-encoded as ``%2B``.

    Not cosmetic. The Books ``last_modified_time`` filter takes a
    ``YYYY-MM-DDTHH:MM:SS-UTC`` value, and a ``+`` offset that is not encoded
    as ``%2B`` is read as a space by any conforming query parser -- which turns
    ``+05:30`` into a malformed timestamp and silently changes the window a
    poll selects. A silently wrong window is the failure mode that loses
    documents without producing an error.
    """
    parts = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
    return "&".join(parts)


def page_context(body: Mapping[str, Any], *, page: int, per_page: int) -> tuple[int, int, bool]:
    """Read ``page_context`` -- the one shape all three products share.

    §11.2: "Pagination is uniform across all three: ``page`` + ``per_page``,
    response ``page_context{page, per_page, has_more_page}``." There is no
    cursor and no reliable total, so ``has_more_page`` is the only termination
    signal there is. Its absence is treated as "no more pages" rather than as
    an error, because several documented list responses omit the block on a
    single-page result.
    """
    context = body.get("page_context") or {}
    return (
        int(context.get("page", page)),
        int(context.get("per_page", per_page)),
        bool(context.get("has_more_page", False)),
    )


def window_params(
    *,
    enabled: bool,
    since: datetime,
    parameter: str,
    formatter,
) -> dict[str, str]:
    """The delta-filter parameters for one list call, or none.

    This is where a capability turns into behaviour. ``enabled`` is the
    relevant :class:`Capabilities` flag; when it is ``False`` the caller gets
    an empty mapping and therefore a full re-pull, which is exactly what
    Inventory's ``GET /purchaseorders`` forces -- its list accepts only
    ``organization_id``, ``page`` and ``per_page``, so sending a
    ``last_modified_time`` it does not document would be a guess presented as
    a filter, and would silently return everything while looking like a delta.
    """
    if not enabled:
        return {}
    return {parameter: formatter(since)}


# ===================================================== the cassette transport
class CassetteTransport:
    """Replays recorded, sanitised cassettes. Never touches a network.

    Every cassette declares the product, service, API version, base URL and
    OAuth scope that produced it, plus its provenance -- and every cassette in
    this repository is provenance ``INVENTED-SANITISED``, authored by hand,
    because no tenant has been contacted. See ``tests/cassettes/README.md``.

    The transport also keeps a ``request_log`` of everything the adapter tried
    to send. That log is how the capability tests prove a negative: that an
    ERP adapter never reaches a receives collection path, rather than merely
    that it did not this time.
    """

    #: Directory name -> the product whose evidence may live there.
    DIRECTORY_PRODUCT: Mapping[str, str] = {
        "erp": "ERP",
        "books_inventory": "BOOKS_INVENTORY",
    }

    def __init__(self, product: str, root: Path | str) -> None:
        if product not in PRODUCTS:
            raise CassetteError(
                f"Unknown product {product!r}; expected one of {sorted(PRODUCTS)}.")
        self.product = product
        self.root = Path(root)
        self.request_log: list[dict[str, Any]] = []
        self._index: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if not self.root.is_dir():
            raise CassetteError(f"Cassette directory does not exist: {self.root}")
        for path in sorted(self.root.rglob("*.json")):
            # Lock 1a: the directory a cassette lives in declares its product,
            # so a Books recording physically cannot be filed under erp/.
            directory = path.relative_to(self.root).parts[0]
            expected = self.DIRECTORY_PRODUCT.get(directory)
            if expected is None:
                raise CassetteError(
                    f"{path} is not under a recognised product directory. "
                    f"Expected one of {sorted(self.DIRECTORY_PRODUCT)}.")
            # Money must never exist as a float, not even for the microsecond
            # between json.loads and the DTO mapping.
            cassette = json.loads(path.read_text(encoding="utf-8"), parse_float=str)
            declared = cassette.get("product")
            if declared != expected:
                raise ProductMixingError(
                    f"{path} declares product {declared!r} but lives under "
                    f"{directory!r}, which is {expected!r} evidence. Products "
                    f"are never mixed.")
            if declared != self.product:
                continue        # another product's evidence; not ours to serve
            request = cassette.get("request") or {}
            key = (
                str(request.get("method", "GET")).upper(),
                str(request.get("path", "")),
                int(request.get("page", 1)),
            )
            if key in self._index:
                raise CassetteError(
                    f"{path} duplicates cassette key {key}; a request must have "
                    f"exactly one recorded response.")
            cassette["_path"] = str(path)
            self._index[key] = cassette

    # --------------------------------------------------------------- request
    def request(
        self,
        *,
        method: str,
        base_url: str,
        path: str,
        scope: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        params = dict(params or {})
        entry = {
            "method": method.upper(),
            "base_url": base_url,
            "path": path,
            "scope": scope,
            "params": params,
            "query": encode_query(params),
            "body": dict(body or {}),
        }
        self.request_log.append(entry)

        key = (method.upper(), path, int(params.get("page", 1)))
        cassette = self._index.get(key)
        if cassette is None:
            raise CassetteError(
                f"No cassette for {key} under {self.root}. Requests made so far: "
                f"{[ (e['method'], e['path']) for e in self.request_log ]}")

        recorded = cassette["request"]

        # Lock 2: the recorded base URL. `/erp/v3` can never satisfy
        # `/books/v3`, whatever the cassette's product label claims.
        if recorded.get("base_url") != base_url:
            if _service_segment(recorded.get("base_url", "")) != _service_segment(base_url):
                raise ProductMixingError(
                    f"Cassette {cassette['_path']} was recorded against "
                    f"{recorded.get('base_url')!r}; this adapter requested "
                    f"{base_url!r}. Evidence from one product is inadmissible "
                    f"for another.")
            raise CassetteError(
                f"Cassette {cassette['_path']} was recorded against data centre "
                f"{recorded.get('base_url')!r}, not {base_url!r}.")

        # Lock 3: the OAuth scope. The prefixes are the products' own names
        # (`ERP.`, `ZohoBooks.`, `ZohoInventory.`), so a scope mismatch across
        # prefixes is a product mismatch by definition.
        if recorded.get("scope") != scope:
            if _scope_prefix(recorded.get("scope", "")) != _scope_prefix(scope):
                raise ProductMixingError(
                    f"Cassette {cassette['_path']} was authorised by scope "
                    f"{recorded.get('scope')!r}; this adapter presented "
                    f"{scope!r}. Products are never mixed.")
            raise CassetteError(
                f"Cassette {cassette['_path']} records scope "
                f"{recorded.get('scope')!r}, not {scope!r}.")

        response = cassette.get("response") or {}
        status = int(response.get("status", 200))
        if status >= 400:
            raise IntegrationError(
                f"Recorded error response {status} for {key}: "
                f"{response.get('body')!r}")
        return response.get("body") or {}


def _service_segment(base_url: str) -> str:
    """``https://www.zohoapis.in/erp/v3`` -> ``erp``. The product's fingerprint."""
    if not base_url:
        return ""
    parts = [p for p in urlsplit(base_url).path.split("/") if p]
    return parts[0] if parts else ""


def _scope_prefix(scope: str) -> str:
    """``ZohoBooks.bills.READ`` -> ``ZohoBooks``."""
    return scope.split(".", 1)[0] if scope else ""


# ==================================================================== the C1 seam
@runtime_checkable
class ProcurementAdapter(Protocol):
    """Wave 5 seam C1. Streams 2 through 7 build against this.

    Note what is deliberately *absent*: there is no ``list_receives``. A
    product that has one exposes it as an extra method and declares
    ``receives_listable=True``; a product that does not, does not have the
    method at all. :func:`acquire_receives` is the only sanctioned way to ask
    for receives, and it refuses to reconcile a capability flag that disagrees
    with the surface it is looking at.

    **Amended after the freeze, twice, and both were this contract's errors
    rather than a caller's.**

    * ``list_items`` and ``list_contacts`` were missing. Plan §11.5 requires
      ``poll_items`` and ``poll_contacts`` jobs, and two streams independently
      hit the gap; ``sweeps.py`` raised ``AdapterMethodMissing`` with the seam
      named rather than substituting a silent no-op, because a master-data
      poll that fetches nothing is indistinguishable from one that found
      nothing.
    * There was no call that **read a record back by its dedupe key**, even
      though ``create_purchase_order`` took one. Those three calls live on
      :class:`RecoverableProcurementAdapter` rather than here, so that this
      protocol keeps meaning exactly what it meant to the streams already
      isinstance-checking against it.

    Nothing was removed or renamed, so an implementation that satisfied the
    frozen six still satisfies them. See ``tests/ADAPTATIONS.md``.
    """
    product: Product

    def base_url(self, dc: str) -> str: ...
    def scopes_required(self) -> frozenset[str]: ...
    def list_bills(self, since: datetime, until: datetime, page: int) -> Page[BillDTO]: ...
    def get_bill(self, external_id: str) -> BillDTO: ...
    def list_purchase_orders(
        self, since: datetime, until: datetime, page: int
    ) -> Page[PurchaseOrderDTO]: ...
    def create_purchase_order(self, po: PurchaseOrderDTO, dedupe_key: str) -> str: ...
    def receives_for_po(self, po_external_id: str) -> list[ReceiveDTO]: ...
    def capabilities(self) -> Capabilities: ...
    # -- added after the freeze; §11.5 required these all along. See below. --
    def list_items(self, since: datetime, until: datetime, page: int) -> Page[ItemDTO]: ...
    def list_contacts(
        self, since: datetime, until: datetime, page: int
    ) -> Page[ContactDTO]: ...


@runtime_checkable
class RecoverableProcurementAdapter(Protocol):
    """C1 plus the three calls that make a lost PO response recoverable.

    **Why this is a separate protocol rather than three more methods on C1.**
    :class:`ProcurementAdapter` is ``runtime_checkable`` and is isinstance-
    checked against both shipped adapters. Folding these in would make that
    check a claim about recovery as well as about acquisition, and an adapter
    that legitimately cannot resolve a key would stop being a procurement
    adapter at all -- which is the "invent parity" failure §11 exists to
    prevent. Separating them lets a caller ask the narrower question it
    actually has: ``isinstance(adapter, RecoverableProcurementAdapter)``.

    **What the three calls are for.** ``create_purchase_order`` takes a
    ``dedupe_key`` and C1 had no call that READ a record back by it. Zoho
    documents no idempotency header (§11.6), so ``cf_capex_ref`` is the only
    handle on an emitted purchase order -- and without a read path, a response
    lost between "sent" and "recorded" left a PO that **exists in the tenant
    and can never be linked**. Z-01's unique constraint still prevented the
    duplicate; what was lost was the link, and an unlinked commitment blocks
    period close as ``ORPHANED_EMISSION``.

    * ``resolve_by_dedupe_key(key)`` -- the read half of §11.6's
      "update-by-custom-field-unique-value". Returns the external id of the
      purchase order carrying ``key``, or ``None``. It must return ``None``
      rather than a best guess: adopting the wrong id links a commitment to
      somebody else's document, which is worse than not linking it at all.
    * ``update_purchase_order(external_id, payload, dedupe_key)`` -- applies
      the current draft onto an adopted id.
    * ``transition_purchase_order(external_id, state, actor)`` -- §11.7's
      draft-to-open step, performed by our own call after our approval
      instance closes.
    """
    product: Product

    def resolve_by_dedupe_key(self, dedupe_key: str) -> str | None: ...
    def update_purchase_order(
        self, external_id: str, payload: Any, dedupe_key: str
    ) -> str: ...
    def transition_purchase_order(
        self, external_id: str, state: str, actor: str
    ) -> str: ...


def verified_dedupe_match(
    rows: Iterable[Mapping[str, Any]],
    *,
    dedupe_key: str,
    id_field: str,
    custom_field: str = DEDUPE_CUSTOM_FIELD,
) -> str | None:
    """The id of the row that **actually carries** ``dedupe_key``, or ``None``.

    This is the guard that makes :meth:`resolve_by_dedupe_key` safe to call on
    a product whose search parameter we are not certain of, and it is the
    reason the Books adapter can offer the method at all.

    An unrecognised query parameter on these APIs is **ignored, not rejected**
    -- the same fact that makes :data:`INVENTORY_PURCHASE_ORDER_LIST_PARAMS`
    worth recording. So a filtered search that silently degrades to an
    unfiltered one returns page 1 of every purchase order in the tenant, and a
    caller that trusted the filter would adopt an arbitrary document's id and
    link this commitment to it. Re-reading the custom field off the row makes
    that failure mode impossible: the filter becomes an optimisation, and
    correctness comes from the value, not from the query.

    Exact string equality, never a prefix or a substring: Zoho's documented
    ``custom_field`` variants include ``_startswith`` and ``_contains``, and
    ``CAPEX-PO-000117`` is a prefix of ``CAPEX-PO-0001170``.
    """
    if not dedupe_key:
        return None
    for row in rows:
        # THREE PLACES THE VALUE ACTUALLY LIVES, verified live (Fable 5.1,
        # 2026-09-11): a filtered LIST row carries it as a top-level key named
        # by the api_name (`cf_capex_ref`); a DETAIL row carries it in the
        # `custom_fields` array AND in `custom_field_hash`; an unfiltered list
        # row carries none of them. Reading only `custom_fields` made a row
        # that DID carry the key resolve to None. Exact equality in every
        # case; the first exact hit wins.
        candidates = [row.get(custom_field)]
        hashed = row.get("custom_field_hash")
        if isinstance(hashed, Mapping):
            candidates.append(hashed.get(custom_field))
        for field in row.get("custom_fields") or ():
            if field.get("api_name") == custom_field:
                candidates.append(field.get("value"))
        if any(isinstance(v, str) and v == dedupe_key for v in candidates):
            found = row.get(id_field)
            if found:
                return str(found)
    return None


# ============================================ the capability-driven dispatcher
def acquire_receives(
    adapter: ProcurementAdapter,
    *,
    open_po_external_ids: Sequence[str] = (),
    page_limit: int = 20,
) -> tuple[ReceiveDTO, ...]:
    """Get goods receipts by whichever mechanism the product actually has.

    This is the executable form of §11.4. On a product where
    ``receives_listable`` is ``False`` there is no collection endpoint --
    Zoho ERP's Purchase Receives has create, update, delete and fetch-one, and
    nothing else -- so PO-anchored discovery is not a fallback, it is the sole
    acquisition mechanism, its cost scales with open-PO count rather than
    receive volume, and a receive against a PO we do not know about is simply
    undiscoverable until its bill arrives.

    The two directions are both enforced, because a capability flag that only
    fails open is not a control:

    * ``receives_listable=True`` with no ``list_receives`` method is a lie
      about a capability the product has;
    * ``receives_listable=False`` *with* a ``list_receives`` method is a lie
      about a capability it does not, and would let a caller reach a list path
      on a product that has none.

    Either raises :class:`CapabilityError` rather than silently choosing the
    other branch.
    """
    caps = adapter.capabilities()
    lister = getattr(adapter, "list_receives", None)

    if caps.receives_listable:
        if lister is None:
            raise CapabilityError(
                f"{type(adapter).__name__} declares receives_listable=True but "
                f"exposes no list_receives(). A declared capability with no "
                f"implementation is worse than a declared gap.")
        collected: list[ReceiveDTO] = []
        for page in range(1, page_limit + 1):
            result = lister(page=page)
            collected.extend(result.items)
            if not result.has_more:
                break
        return tuple(collected)

    if lister is not None:
        raise CapabilityError(
            f"{type(adapter).__name__} declares receives_listable=False but "
            f"exposes list_receives(). On a product with no collection "
            f"endpoint that method cannot exist, and offering it would let a "
            f"caller reach a list path the product does not have.")

    if not open_po_external_ids:
        return ()
    anchored: list[ReceiveDTO] = []
    for po_external_id in open_po_external_ids:
        anchored.extend(adapter.receives_for_po(po_external_id))
    return tuple(anchored)


def receives_strategy(adapter: ProcurementAdapter) -> Literal["LIST", "PO_ANCHORED"]:
    """The name of the mechanism :func:`acquire_receives` will use.

    Exposed so a scheduler can size a job -- a PO-anchored sweep costs one
    call per open PO plus one per receive, against a daily ceiling of 2,000 on
    ERP Standard -- without importing a product module to find out.
    """
    return "LIST" if adapter.capabilities().receives_listable else "PO_ANCHORED"


def walk_pages(fetch, *, page_limit: int = 50) -> Iterable[Any]:
    """Exhaust a paged endpoint using ``has_more_page`` and nothing else.

    ``page_limit`` is a hard stop, not a suggestion: there is no total count in
    any of the three products' page contexts, so a server that never clears
    ``has_more_page`` would otherwise spin until the daily call ceiling is gone.
    """
    for page in range(1, page_limit + 1):
        result = fetch(page)
        yield from result.items
        if not result.has_more:
            return
    raise IntegrationError(
        f"Pagination did not terminate within {page_limit} pages. "
        f"has_more_page is the only termination signal these APIs give, so a "
        f"server that never clears it must not be allowed to burn the budget.")
