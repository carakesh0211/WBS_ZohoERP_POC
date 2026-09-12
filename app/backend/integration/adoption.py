"""Adopting a tenant-raised Zoho order (Fable 5.1, product owner 2026-09-12).

WHAT THIS CLOSES
  `docs/fable51/STAGE_B_PERSISTENT_UAT.md`, "Findings from the first live
  sweeps" (2026-09-12), finding 1: `sweeps.SweepPoAnchored` anchors the
  receive walk on `purchase_order` rows THIS SYSTEM raised (`external_id` set
  for the connection). The seven DEMO WBS orders were created directly in the
  tenant, so the walk found nothing to anchor on and the tenant's receives
  were never mirrored. This module is finding 1's option "(a)": adopt the
  tenant's own order into a local `purchase_order`, using the line fields the
  tenant stamped (`cf_wbs_code`, `cf_budget_head`), so a later receive or bill
  against it has something local to resolve onto.

RESTRICTED TO THE DEMO ORGANISATION, ON PURPOSE
  Product owner, 2026-09-12: adopt "the SEVEN tenant-raised Zoho demo
  orders" -- not any order in any tenant. `DEMO_ORGANIZATION_ID` is the one
  organisation this was authorised for; every other connection is refused
  with `ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION` before a single GET is
  issued.

NOT A SWEEP, AND DELIBERATELY NOT ONE
  `sweeps.py`'s jobs are windowed, checkpointed and re-run on a cadence
  through `jobs.py`. Adoption is none of those: it is run once, by an
  operator, against a bounded batch of inbox rows this connection already
  discovered, and it either creates a local order or files a coded
  exception -- it never partially writes one and resumes later. `job` rows,
  watermarks and `JobContext` therefore do not apply; what DOES apply from
  that stack is the polling rate budget (`throttle.reserve`, the same
  POLLING lane `live_sweep.PollingBudget` spends), charged once per PO
  DETAIL GET this module issues, because that GET reaches the same tenant
  under the same daily ceiling a sweep's GET would.

READ-ONLY. Every call this module makes is `adapter.get_purchase_order`, a
GET. Nothing here writes to Zoho, and `CAPEX_ERP_OUTBOUND_WRITES` is never
read, referenced or set.

VALIDATION, THEN THE WRITE, NEVER THE OTHER WAY ROUND
  `cf_capex_ref` must be present and not already claimed by another adopted
  order; every line's `cf_wbs_code` must resolve to EXACTLY ONE WBS element in
  the connection's entity, every line's `cf_budget_head` to EXACTLY ONE
  budget head, and every line's WBS must belong to the SAME project. Any
  failure raises `ADOPTION_DIMENSION_INVALID` naming the order and the line,
  and NOTHING is written -- no local purchase order, no `po_line`, no control-
  cell exposure. Currency is preserved exactly as the tenant stated it
  (`pg.procurement_services.adopt_purchase_order` resolves the rate through
  the SAME `fx.py` path a created order uses); a non-INR order with no ACTIVE
  rate on file is held under `FOREIGN_CURRENCY_BASIS_MISSING` -- decision 8's
  existing kind -- rather than booked at face value.

IDEMPOTENT AND CONCURRENCY-SAFE
  A second sweep finds the local order by `(external_source, external_id)`
  and, if the tenant's own dimensions are UNCHANGED, does nothing. If they
  have changed since adoption, it raises `ADOPTION_DIMENSION_CONFLICT` and
  leaves the local order exactly as it was -- an adopted commitment is never
  silently re-based, for the same reason `trg_purchase_order_fx_basis_
  immutable` (029) refuses to re-base a created one. Each order is processed
  under `pg_advisory_xact_lock(hashtext(...))`, keyed on this connection and
  this external id -- the same primitive `live_sweep._job_row_for` uses for
  its own race -- plus the database's own `ux_po_external` / `ux_po_external_
  capex_ref` unique indexes as the backstop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..pg import integration_store as store
from ..pg import procurement_services as psvc
from ..pg import repo
from ..pg.engine import Session
from . import jobs, live_sweep, throttle
from .dto import LineDTO, PurchaseOrderDTO

#: The one organisation this adoption path is authorised for (product owner,
#: 2026-09-12). Not a capability flag: an authorisation this narrow belongs
#: as a literal an operator can read, not as configuration that could widen
#: silently.
DEMO_ORGANIZATION_ID = "60074128927"

#: The custom field API names D-7 verified on DEMO WBS purchase-order LINES
#: (`erp.VERIFIED_LINE_CUSTOM_FIELDS["60074128927"]`), read straight off the
#: raw line row rather than through a new `LineDTO` field -- see the module
#: docstring's note on `_line_custom_field` for why.
CF_WBS_CODE = "cf_wbs_code"
CF_BUDGET_HEAD = "cf_budget_head"

#: `reconciliation_exception.object_type` for every exception this module
#: raises. The order, never a line: a line-specific failure is named in
#: `detail`, exactly as `SweepBillDetail._mirror`'s multi-PO refusal names
#: its line count in `detail` while keying the exception on the whole bill.
OBJECT_TYPE = "purchase_order"

#: `purchase_order.status` mapped from the RAW ERP value, mirroring the four
#: values research/30_contracts/C17_zoho_status_map.json's ERP/purchase_order
#: block records as `DOCUMENTED_RAW_VALUE` / active (draft, open, billed,
#: cancelled -- the other two rows there are UNVERIFIED and inactive).
#: `pending_approval`/`approved` are visibly ABSENT rather than guessed at,
#: and any status this map does not carry falls back to 'Draft': a status is
#: presentation, not a financial control point, so a conservative default
#: costs nothing that ADOPTION_DIMENSION_INVALID / FOREIGN_CURRENCY_BASIS_
#: MISSING do not already guard.
_PO_STATUS_FROM_RAW: Mapping[str, str] = {
    "draft": "Draft", "open": "Released", "billed": "Fully Actualised",
    "cancelled": "Cancelled",
}


class AdoptionError(store.IntegrationStoreError):
    """A coded refusal from this module. Same shape as every other integration
    error, so the route maps it to HTTP the way `LiveSweepError` is mapped."""


@dataclass
class _AdoptionCounters:
    adopted: int = 0
    skipped: int = 0
    exceptions: int = 0
    calls: int = 0
    exception_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"adopted": self.adopted, "skipped": self.skipped,
                "exceptions": self.exceptions, "calls": self.calls,
                "exception_ids": list(self.exception_ids)}


# ============================================================ line dimensions
def _line_custom_field(line: LineDTO, api_name: str) -> str | None:
    """One line-level custom field's value, read off the RAW Zoho row.

    `LineDTO` carries no `cf_wbs_code` / `cf_budget_head` field: the frozen DTO
    is shared by every document line on both products (a bill line, a receive
    line, an order line), and this pair exists on exactly one tenant's
    purchase-order lines (`erp.VERIFIED_LINE_CUSTOM_FIELDS`). Widening the DTO
    for one adopter's need would put a field every OTHER line construction --
    `tests/test_integration_dto_reader_contract.py` among them -- has to carry
    `None` for, forever.

    `line.raw` already carries what `_lines()` built the DTO from verbatim
    (dto.py: "every DTO carries its provenance"), and `tools/erp_demo/
    po_snapshot.py` -- which read the DEMO WBS tenant live -- confirms the
    shape: a detail line item's own custom fields arrive as
    `item_custom_fields: [{"index", "api_name", "value"}, ...]`, a sibling of
    the header's `custom_fields` list `erp._custom_field` already reads. This
    reads the same list at the line grain, verbatim, never guessing at a
    shape `po_snapshot.py` has not verified.
    """
    for entry in (line.raw.get("item_custom_fields") or ()):
        if isinstance(entry, Mapping) and entry.get("api_name") == api_name:
            value = entry.get("value")
            if value not in (None, ""):
                return str(value)
    return None


def _quantity_number(text: str) -> int | float:
    """A DTO quantity STRING as the int/float `_as_quantity` accepts.

    Mirrors `procurement_services._plain_quantity` (the same conversion, the
    other direction: that reads a stored `numeric` back out, this reads a
    DTO's decimal string in) rather than importing it, because the two sides
    are private to their own modules by convention here and the conversion is
    three lines.
    """
    as_decimal = Decimal(text)
    if as_decimal == as_decimal.to_integral_value():
        return int(as_decimal)
    return float(as_decimal)


@dataclass(frozen=True)
class _ResolvedLine:
    line_no: int
    wbs_id: str
    project_id: str
    budget_head_id: str
    description: str
    quantity: int | float
    amount_minor: int
    #: `LineDTO.external_line_id` -- the tenant's OWN line identifier,
    #: carried through to `po_line.line_external_id` so a later receive
    #: resolves against it (`sweeps.resolve_po_line` matches a receive line
    #: by exactly this pair). Without it every receive against an adopted
    #: order would quarantine as `GRN_LINE_UNATTRIBUTED` no matter how
    #: correctly the header was adopted -- the PO id alone anchors the
    #: header, not its lines.
    line_external_id: str | None


def _resolve_wbs(session: Session, *, entity_id: str,
                 wbs_code: str) -> list[tuple[str, str, bool]]:
    """Every WBS element in this entity carrying `wbs_code`, however many."""
    rows = repo.query(
        session,
        """
        SELECT w.wbs_id, w.project_id, w.is_abandoned
        FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
        WHERE p.entity_id = %(entity_id)s AND w.wbs_code = %(code)s AND {scope}
        """,
        {"entity_id": entity_id, "code": wbs_code},
        columns=store.PROCUREMENT_SCOPE_COLUMNS,
    )
    return [(str(r[0]), str(r[1]), bool(r[2])) for r in rows]


def _resolve_budget_head(session: Session, *, entity_id: str,
                         value: str) -> list[str]:
    """Every active budget head in this entity named or coded `value`.

    `budget_head` carries its own `entity_id` and no plant/location/project
    dimension (`original_budget.search_selector`'s own "budget_head" branch
    reads it the same way, scope-exempt): a reference master under its own
    RLS waiver, narrowed to the entity in the WHERE clause rather than
    through `{scope}`.
    """
    rows = session.fetchall(  # scope-exempt: RLS-filtered reference master, entity-narrowed exactly as original_budget.search_selector's "budget_head" branch
        "SELECT budget_head_id FROM budget_head WHERE active"
        " AND entity_id = %s AND (lower(name) = lower(%s) OR lower(code) = lower(%s))",
        (entity_id, value, value))
    return [str(r[0]) for r in rows]


def _resolve_lines(session: Session, *, entity_id: str,
                   lines: Sequence[LineDTO], order_label: str
                   ) -> tuple[list[_ResolvedLine] | None, str | None]:
    """Every line's `(wbs_id, budget_head_id)`, or the reason none exist.

    Returns `(resolved, None)` on success or `(None, reason)` naming the
    order and the offending line -- the caller files `reason` as
    `ADOPTION_DIMENSION_INVALID` and adopts nothing. ALL-OR-NOTHING: a single
    bad line invalidates the whole order rather than adopting the lines that
    did resolve, because a purchase order with half its lines missing is not
    the order the tenant raised.
    """
    resolved: list[_ResolvedLine] = []
    for index, line in enumerate(lines, start=1):
        wbs_code = _line_custom_field(line, CF_WBS_CODE)
        head_value = _line_custom_field(line, CF_BUDGET_HEAD)
        if not wbs_code:
            return None, (
                f"{order_label} line {index}: no {CF_WBS_CODE!r} custom "
                f"field. Every line of an adopted order must name a WBS.")
        if not head_value:
            return None, (
                f"{order_label} line {index}: no {CF_BUDGET_HEAD!r} custom "
                f"field. Every line of an adopted order must name a budget "
                f"head.")
        wbs_matches = _resolve_wbs(session, entity_id=entity_id, wbs_code=wbs_code)
        if len(wbs_matches) != 1:
            return None, (
                f"{order_label} line {index}: {CF_WBS_CODE}={wbs_code!r} "
                f"resolves to {len(wbs_matches)} WBS element(s) in this "
                f"connection's entity, not exactly one.")
        wbs_id, project_id, is_abandoned = wbs_matches[0]
        if is_abandoned:
            return None, (
                f"{order_label} line {index}: {CF_WBS_CODE}={wbs_code!r} "
                f"names an ABANDONED WBS element ({wbs_id}), which cannot "
                f"receive procurement.")
        head_matches = _resolve_budget_head(session, entity_id=entity_id,
                                            value=head_value)
        if len(head_matches) != 1:
            return None, (
                f"{order_label} line {index}: {CF_BUDGET_HEAD}="
                f"{head_value!r} resolves to {len(head_matches)} budget "
                f"head(s) in this connection's entity, not exactly one.")
        amount_minor = int(line.line_total_paise)
        resolved.append(_ResolvedLine(
            line_no=index, wbs_id=wbs_id, project_id=project_id,
            budget_head_id=head_matches[0],
            description=line.description or "",
            quantity=_quantity_number(line.quantity),
            amount_minor=amount_minor,
            line_external_id=line.external_line_id))

    projects = {r.project_id for r in resolved}
    if len(projects) > 1:
        return None, (
            f"{order_label}: lines resolve to {len(projects)} different "
            f"projects ({sorted(projects)}); every line of one order must "
            f"belong to the same project.")
    return resolved, None


# ================================================================ the lookup
def _find_local_po(session: Session, *, external_source: str,
                   external_id: str) -> Mapping[str, Any] | None:
    row = repo.query_one(
        session,
        """
        SELECT po.po_id, po.external_capex_ref, po.currency, po.status
        FROM purchase_order po
        JOIN project p ON p.project_id = po.project_id
        WHERE po.external_source = %(source)s AND po.external_id = %(external_id)s
          AND {scope}
        """,
        {"source": external_source, "external_id": external_id},
        columns=store.PROCUREMENT_SCOPE_COLUMNS,
    )
    if row is None:
        return None
    return {"po_id": row[0], "external_capex_ref": row[1],
           "currency": row[2], "status": row[3]}


def _stored_lines(session: Session, *, po_id: str) -> list[tuple[str, str]]:
    rows = repo.query(
        session,
        """
        SELECT pol.wbs_id, pol.budget_head_id
        FROM po_line pol
        JOIN purchase_order po ON po.po_id = pol.po_id
        JOIN project p ON p.project_id = po.project_id
        WHERE pol.po_id = %(po_id)s AND {scope}
        ORDER BY pol.line_no
        """,
        {"po_id": po_id}, columns=store.PROCUREMENT_SCOPE_COLUMNS,
    )
    return [(str(r[0]), str(r[1])) for r in rows]


def _pending_inbox_rows(session: Session, *, connection_id: str,
                        limit: int) -> list[tuple[str, str]]:
    """`(external_id, inbox_id)` for the connection's purchaseorders inbox,
    one row per external id -- its LATEST inbox row, so `adopted_from_
    inbox_id` names the most recent version this connection has actually
    seen."""
    rows = repo.query(
        session,
        f"""
        SELECT DISTINCT ON (i.external_id) i.external_id, i.inbox_id
        FROM {store.INTEGRATION_INBOX} i
        JOIN {store.INTEGRATION_CONNECTION} c ON c.connection_id = i.connection_id
        WHERE i.connection_id = %(connection_id)s AND i.module = %(module)s
          AND {{scope}}
        ORDER BY i.external_id, i.received_at DESC
        LIMIT %(limit)s
        """,
        {"connection_id": connection_id, "module": "purchaseorders",
         "limit": max(int(limit), 0)},
        columns=store.VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return [(str(r[0]), str(r[1])) for r in rows]


# ================================================================== the verb
def _consume_budget(session: Session, *, connection_id: str, actor: str,
                    clock: jobs.Clock) -> bool:
    """One POLLING-lane reservation for one PO detail GET, or False."""
    verdict = throttle.reserve(
        session, connection_id=connection_id, lane=throttle.Lane.POLLING,
        cost=1, actor=actor, clock=clock)
    return bool(verdict)


def _resolve_status(raw_status: str | None) -> str:
    return _PO_STATUS_FROM_RAW.get(str(raw_status or "").strip().lower(), "Draft")


def adopt_tenant_orders(session: Session, *, connection: Mapping[str, Any],
                        adapter: Any, actor: str,
                        correlation_id: str | None = None,
                        limit: int = 50,
                        clock: jobs.Clock | None = None) -> dict[str, Any]:
    """Adopt this LIVE connection's tenant-raised purchase orders.

    Reads at most `limit` distinct purchase-order external ids already in
    this connection's inbox, and for each: fetches the order's DETAIL (one
    GET, charged against the POLLING budget), validates its dimensions, and
    either writes a local `purchase_order` (+ lines) with `commitment_origin
    = 'EXTERNAL_UNSANCTIONED'` or raises a coded reconciliation exception.
    Returns ``{adopted, skipped, exceptions, calls, exception_ids}``.

    Refuses outright, before a single GET, when the connection is not LIVE
    or is not the demo organisation on ERP -- see the module docstring.
    """
    clock = clock or jobs.SystemClock()
    connection_id = str(connection["connection_id"])
    entity_id = str(connection["entity_id"])
    if not live_sweep.is_live(connection):
        raise AdoptionError(
            "CONNECTION_NOT_LIVE",
            f"Connection {connection_id!r} is in mode "
            f"{connection.get('mode')!r}; adoption reaches the tenant and "
            f"only {list(store.LIVE_MODES)} may. Nothing was adopted.",
            status=409)
    product = str(connection.get("product") or "").strip().upper()
    organization_id = str(connection.get("organization_id") or "")
    if product != "ERP" or organization_id != DEMO_ORGANIZATION_ID:
        raise AdoptionError(
            "ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION",
            f"Adoption is authorised only for organisation "
            f"{DEMO_ORGANIZATION_ID!r} on product 'ERP' (the product owner's "
            f"2026-09-12 decision named the seven DEMO WBS orders "
            f"specifically); this connection is product {product!r}, "
            f"organisation {organization_id!r}. Nothing was adopted.",
            status=403)
    external_source = live_sweep.external_source_for(connection)

    counters = _AdoptionCounters()
    pending = _pending_inbox_rows(session, connection_id=connection_id,
                                  limit=limit)
    for external_id, inbox_id in pending:
        # `_job_row_for`'s own primitive: transaction-scoped, needs no row to
        # lock, and serialises a concurrent second adoption of the SAME order
        # on THIS connection.
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"capex.po_adoption:{connection_id}:{external_id}",))

        existing = _find_local_po(session, external_source=external_source,
                                  external_id=external_id)

        if not _consume_budget(session, connection_id=connection_id,
                               actor=actor, clock=clock):
            # Over budget: stop the whole batch here rather than skip this
            # order and try the next -- the next order's GET would be
            # refused too, and reporting each as a silent "skipped" would
            # look identical to "already adopted, unchanged".
            break
        detail: PurchaseOrderDTO = adapter.get_purchase_order(
            external_id=external_id)
        counters.calls += 1

        outcome = _process_one(
            session, connection_id=connection_id, entity_id=entity_id,
            external_source=external_source, external_id=external_id,
            inbox_id=inbox_id, detail=detail, existing=existing, actor=actor,
            correlation_id=correlation_id, now=clock.now())
        if outcome == "adopted":
            counters.adopted += 1
        elif outcome == "skipped":
            counters.skipped += 1
        else:
            counters.exceptions += 1
            counters.exception_ids.append(outcome)

    return counters.as_dict()


def _process_one(session: Session, *, connection_id: str, entity_id: str,
                 external_source: str, external_id: str, inbox_id: str,
                 detail: PurchaseOrderDTO, existing: Mapping[str, Any] | None,
                 actor: str, correlation_id: str | None,
                 now: datetime) -> str:
    """Adopt, skip (idempotent no-op) or file an exception for one order.

    Returns ``"adopted"``, ``"skipped"``, or the raised exception's id.
    """
    order_label = f"purchase order {detail.document_number or external_id}"
    capex_ref = detail.dedupe_key

    if not capex_ref:
        return _raise(session, kind="ADOPTION_DIMENSION_INVALID",
                      external_id=external_id, entity_id=entity_id,
                      detail=f"{order_label} carries no cf_capex_ref. "
                              f"Nothing was adopted.",
                      actor=actor, correlation_id=correlation_id, now=now)

    resolved, reason = _resolve_lines(session, entity_id=entity_id,
                                      lines=detail.lines, order_label=order_label)

    if existing is not None:
        return _reconcile_existing(
            session, existing=existing, capex_ref=capex_ref,
            resolved=resolved, reason=reason, external_id=external_id,
            entity_id=entity_id, order_label=order_label, actor=actor,
            correlation_id=correlation_id, now=now)

    if reason is not None:
        return _raise(session, kind="ADOPTION_DIMENSION_INVALID",
                      external_id=external_id, entity_id=entity_id,
                      detail=reason, actor=actor,
                      correlation_id=correlation_id, now=now)
    assert resolved is not None  # reason is None iff resolved is not None

    duplicate = repo.query_one(
        session,
        """
        SELECT po.po_id FROM purchase_order po
        JOIN project p ON p.project_id = po.project_id
        WHERE po.external_source = %(source)s
          AND po.external_capex_ref = %(ref)s
          AND po.external_id <> %(external_id)s
          AND {scope}
        """,
        {"source": external_source, "ref": capex_ref,
         "external_id": external_id}, columns=store.PROCUREMENT_SCOPE_COLUMNS,
    )
    if duplicate is not None:
        return _raise(
            session, kind="ADOPTION_DIMENSION_INVALID",
            external_id=external_id, entity_id=entity_id,
            detail=(f"{order_label}: cf_capex_ref={capex_ref!r} is already "
                    f"claimed by adopted order {duplicate[0]}. A CAPEX "
                    f"reference identifies exactly one commitment; nothing "
                    f"was adopted."),
            actor=actor, correlation_id=correlation_id, now=now)

    lines_payload = [{
        "wbs_id": r.wbs_id, "budget_head_id": r.budget_head_id,
        "description": r.description, "quantity": r.quantity,
        "line_external_id": r.line_external_id,
        **({"amount_paise": r.amount_minor}
           if detail.currency_code.strip().upper() == psvc.BASE_CURRENCY
           else {"source_amount_minor": r.amount_minor}),
    } for r in resolved]

    try:
        written = psvc.adopt_purchase_order(
            session, project_id=resolved[0].project_id,
            vendor_name=detail.vendor_name or "(unnamed vendor)",
            lines=lines_payload, actor=actor,
            po_number=detail.document_number or external_id,
            currency=detail.currency_code, document_date=detail.document_date,
            status=_resolve_status(detail.external_status_raw),
            external_source=external_source, external_id=external_id,
            external_capex_ref=capex_ref, adopted_from_inbox_id=inbox_id,
            correlation_id=correlation_id)
    except psvc.ProcurementError as exc:
        if exc.code in ("FX_RATE_UNAVAILABLE", "FX_RATE_SOURCE_REQUIRED"):
            return _raise(
                session, kind="FOREIGN_CURRENCY_BASIS_MISSING",
                external_id=external_id, entity_id=entity_id,
                detail=(f"{order_label} is denominated in "
                        f"{detail.currency_code}: {exc.message} Its face "
                        f"value has NOT been booked as paise; nothing was "
                        f"adopted."),
                actor=actor, correlation_id=correlation_id, now=now)
        return _raise(
            session, kind="ADOPTION_DIMENSION_INVALID",
            external_id=external_id, entity_id=entity_id,
            detail=f"{order_label}: {exc.code}: {exc.message}", actor=actor,
            correlation_id=correlation_id, now=now)

    _resolve_unsanctioned_commitment(
        session, external_id=external_id, entity_id=entity_id,
        po_id=written["po_id"], actor=actor, correlation_id=correlation_id,
        now=now)
    return "adopted"


def _reconcile_existing(session: Session, *, existing: Mapping[str, Any],
                        capex_ref: str | None,
                        resolved: list[_ResolvedLine] | None,
                        reason: str | None, external_id: str, entity_id: str,
                        order_label: str, actor: str,
                        correlation_id: str | None, now: datetime) -> str:
    """Already adopted: idempotent no-op, or `ADOPTION_DIMENSION_CONFLICT`.

    Never re-writes the local order either way -- an adopted commitment is
    posted, and a repeat sweep's job is to notice drift, not to correct it in
    place.
    """
    if reason is not None:
        return _raise(
            session, kind="ADOPTION_DIMENSION_CONFLICT",
            external_id=external_id, entity_id=entity_id,
            detail=(f"{order_label} was adopted as {existing['po_id']} and "
                    f"the tenant's dimensions no longer resolve: {reason} "
                    f"The local order is left as it was."),
            actor=actor, correlation_id=correlation_id, now=now)
    assert resolved is not None

    stored_lines = _stored_lines(session, po_id=existing["po_id"])
    current_lines = [(r.wbs_id, r.budget_head_id) for r in resolved]
    if (existing["external_capex_ref"] != capex_ref
            or stored_lines != current_lines):
        return _raise(
            session, kind="ADOPTION_DIMENSION_CONFLICT",
            external_id=external_id, entity_id=entity_id,
            detail=(f"{order_label} was adopted as {existing['po_id']} with "
                    f"cf_capex_ref={existing['external_capex_ref']!r} and "
                    f"lines {stored_lines}; the tenant now presents "
                    f"cf_capex_ref={capex_ref!r} and lines {current_lines}. "
                    f"The local order is left as it was; a changed "
                    f"commitment is not silently re-based."),
            actor=actor, correlation_id=correlation_id, now=now)
    return "skipped"


def _raise(session: Session, *, kind: str, external_id: str, entity_id: str,
          detail: str, actor: str, correlation_id: str | None,
          now: datetime) -> str:
    return store.raise_exception(
        session, kind=kind, object_type=OBJECT_TYPE, object_id=external_id,
        detail=detail, raised_at=now, entity_id=entity_id,
        correlation_id=correlation_id, actor=actor)


def _resolve_unsanctioned_commitment(session: Session, *, external_id: str,
                                     entity_id: str, po_id: str, actor: str,
                                     correlation_id: str | None,
                                     now: datetime) -> None:
    """Close the UNSANCTIONED_COMMITMENT `PollPurchaseOrders._accept` raised
    for this order, if it is still Open. Adoption is the resolution."""
    row = repo.query_one(
        session,
        """
        SELECT x.exception_id FROM reconciliation_exception x
        WHERE x.kind = %(kind)s AND x.object_type = %(object_type)s
          AND x.object_id = %(object_id)s AND x.status = %(open)s
          AND {scope}
        ORDER BY x.raised_at DESC LIMIT 1
        """,
        {"kind": "UNSANCTIONED_COMMITMENT", "object_type": OBJECT_TYPE,
         "object_id": external_id, "open": store.EXCEPTION_OPEN},
        columns=store.EXCEPTION_SCOPE_COLUMNS,
    )
    if row is None:
        return
    store.act_on_exception(
        session, exception_id=row[0], action="resolve", actor=actor,
        reason=f"adopted as {po_id}")


__all__ = [
    "AdoptionError", "CF_BUDGET_HEAD", "CF_WBS_CODE", "DEMO_ORGANIZATION_ID",
    "OBJECT_TYPE", "adopt_tenant_orders",
]
