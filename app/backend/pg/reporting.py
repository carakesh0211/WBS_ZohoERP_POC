"""The server-side reporting layer: one `FilterSet`, one set of metrics.

Dashboards (A3), exports (A2) and closure (A4) all consume THIS module. That
is the point of it, and `docs/WAVE7_CONTRACT.md` says why in one sentence: "A
second filter shape is how a dashboard card and its own drill-down come to
disagree." There is exactly one :class:`FilterSet` in this codebase and it is
defined below.

WHAT THIS MODULE COMPUTES, AND WHERE THE FORMULAS COME FROM
===========================================================

Ten metrics -- ``budget``, ``original``, ``revisions``, ``ordered``,
``commitment``, ``actual``, ``received``, ``received_not_billed``,
``pr_reserved``, ``available`` -- plus ``exposure``, ``utilisation_pct`` and
``band``. Every one is transcribed from :func:`app.backend.domain.compute_ledger`,
which is the frozen ``C5_formulas.json`` registry in code. Not re-derived from
the requirement text, not recalled from memory:

  * **Open commitment is ordered less BILLED**, never ordered less received.
    ``max(0, ordered - billed)`` per PO line, and zero outright for a PO in
    ``Cancelled`` or ``Closed``. An adversarial review found two functions in
    ONE file computing ``open_paise`` differently; this module has one
    definition, in one CTE.
  * **A Void GRN does not count at all**, and a reversal subtracts the
    MAGNITUDE of its line -- so a reversal stored positive and one stored
    negative reduce ``received`` by the same amount. The same review found a
    ``received_paise`` counting Void GRNs for want of a join to ``grn``.
  * **Only accounting-effective bills move actual CWIP** (AUD-C-004):
    ``Approved`` and ``Reversal`` and nothing else, with tax and freight
    included, a ``Reversal`` bill negated by its own status.
  * **Only Approved AND effective budget lines create capacity** (AUD-C-005).

THE FOUR BUCKETS DO NOT OVERLAP, AND TWO OF THEM CANNOT BE SUMMED AT GROUP GRAIN
================================================================================

PR reservation, PO commitment, GRN receipt and bill actual are four distinct
buckets. Two of them -- ``commitment`` and ``received_not_billed`` -- are
``max(0, a - b)`` per PO LINE, and ``SUM(max(0, a-b)) <> max(0, SUM(a)-SUM(b))``
the moment any single line is over-billed. Over-billing is a case this product
reports rather than clamps, so it is not hypothetical. Both are therefore
computed at LINE grain in :data:`_FACT_SQL`'s ``po_fact`` CTE and only then
summed, exactly as ``compute_ledger`` does it in Python.

``pr_reserved`` goes the other way, and migration 015 is why. A reservation is
held per (PR x RESOLVED control cell), so a request spanning two cells has TWO
rows; summing those rows per cell double counts nothing. Summing per LINE
would, because several lines can resolve to one cell. This module never joins
``pr_reservation`` to ``pr_line``.

SCOPE NARROWS, NEVER WIDENS
===========================

Every statement goes through :func:`app.backend.pg.repo.query` with a literal
``{scope}`` token and a ``columns=`` mapping naming all four dimensions -- so
the caller's resolved scope is compiled into the predicate independently of
RLS. A ``FilterSet`` is applied *on top of* that, with ``AND``, and can
therefore only ever make the answer smaller. An entity the caller holds no
grant for yields NO ROWS: never an error, because a 403 on an id an
unauthorised caller named is an existence oracle over another entity's estate.

Nothing here reads a permission or a scope from the request body. The scope
arrives on the :class:`~app.backend.pg.engine.Session`, resolved server-side by
``principal_scope``.

FOUR STATES, NEVER COLLAPSED
============================

``zero data``, ``denied scope``, ``stale data`` and ``service failure`` are
four different answers and this module returns four different shapes. An empty
result under a denied scope carries ``state="denied"``; an empty result under a
real scope carries ``state="empty"``. A filter this schema cannot express
carries ``state="unavailable"`` with a code naming exactly what is missing --
see :data:`UNSUPPORTED_FILTERS`. No total is ever synthesised to fill a gap.

WHAT THIS SCHEMA CANNOT FILTER ON TODAY, NAMED RATHER THAN IGNORED
==================================================================

Several of the contract's filters have no column to bind to across every
bucket, and a filter that is silently dropped is worse than one that is
refused: the reader gets a total that looks right, for a population they did
not ask for.

  * ``item_ids`` -- there is no ``item_id`` on ``pr_line``, ``po_line``,
    ``grn_line`` or ``bill_line``, even after migration 027. ``item_master``
    exists (005) and nothing references it from the document chain. Refused
    outright, always.
  * ``division_ids`` / ``branch_ids`` / ``zone_ids`` -- migration 026 resolves
    these onto ``original_budget_line``, the ORIGINAL BUDGET DOCUMENT's own
    entry line, and onto nothing this module reads. Refused outright, always.
  * ``fiscal_years`` -- a column of ``original_budget`` (026), the document,
    not of ``budget_line``. Use ``period_ids``.
  * ``requestor_ids`` / ``approver_ids`` -- the identities this schema records
    (``purchase_request.requested_by``, ``original_budget.
    approving_authority``, ``approval_instance``) sit on one document type
    each, not on a column shared across all five branches.
  * ``vendor_ids`` -- CONDITIONALLY refused, and this one is Fable 5.1's
    change from a blanket refusal. Migration 014 gave ``bill`` a real
    ``vendor_id`` FK; ``purchase_order`` still carries only ``vendor_name``
    text, so no other bucket has a vendor to filter on. Naming ``vendor_ids``
    (or grouping by ``vendor``) is honoured only when ``document_types``
    narrows the report to buckets :data:`VENDOR_SUPPORTED_BUCKETS` covers --
    in practice, ``document_types=["BILL"]`` -- and refused with
    ``FILTER_UNSUPPORTED_FOR_METRIC``, naming exactly which bucket could not
    honour it, otherwise. Never half-answered: a vendor filter is never
    applied to ``actual`` alone while ``ordered``/``commitment`` stay
    unfiltered across every vendor.

All are declared, not commented: :data:`UNSUPPORTED_FILTERS` carries the
always-refused ones (their own code and sentence); ``FilterSet.validate``
carries the conditional ``vendor`` refusal. ``api/reports.py`` renders both as
an ``unavailable`` state, and a test asserts the refusal rather than the
workaround.

DETERMINISTIC ORDER, SO PAGINATION CANNOT SKIP OR REPEAT
=========================================================

Every ORDER BY ends with the row's own unique key -- the full group-key tuple
for an aggregate, the line's primary key for a drill-down. A sort on
``amount_paise`` alone has ties, and a keyset cursor over a tied sort silently
skips rows on one page and repeats them on the next. The cursor encodes the
whole tie-broken key, so resuming is exact.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import repo
from .engine import Scope, Session

# ===========================================================================
# Constants transcribed from the frozen contracts
# ===========================================================================

_CONTRACTS = Path(__file__).resolve().parents[3] / "research" / "30_contracts"

#: Bill accounting statuses that move actual CWIP. AUD-C-004.
#:
#: Imported from `integration_store` rather than restated, so this module and
#: the procurement ledger cannot drift. `domain.ACCOUNTING_EFFECTIVE_BILL_STATES`
#: is the third copy of the same tuple and the reason all three are pinned
#: together by `tests/test_contracts.py`.
from .integration_store import ACCOUNTING_EFFECTIVE_BILL_STATES  # noqa: E402

#: A PO in one of these states no longer holds commitment.
#: `domain.COMMITMENT_RELEASING_STATES`, verbatim.
COMMITMENT_RELEASING_STATES: tuple[str, ...] = ("Cancelled", "Closed")

#: Exposure thresholds. `domain.WATCH_PCT` / `domain.CRITICAL_PCT`, verbatim.
WATCH_PCT = 80.0
CRITICAL_PCT = 90.0

#: The metric components. `domain._COMPONENTS`, verbatim and in the same order.
COMPONENTS: tuple[str, ...] = (
    "budget", "original", "revisions", "ordered", "commitment",
    "actual", "received", "received_not_billed", "pr_reserved",
    # 034: internal fulfilment, shown APART from the external buckets and
    # summed into exposure beside them (`derive`).
    "internal_allocation", "internal_consumption",
)

#: The document types a `FilterSet` may name, and which buckets each supplies.
#:
#: Filtering to `["PO"]` does NOT mean "show a zero budget"; it means the
#: report is about purchase orders, so the PO-sourced buckets are computed and
#: the others are not summed into the totals. `budget` is sourced from
#: `budget_line`, which is not a document type at all, so it is present for
#: every selection -- a utilisation figure with no denominator is not a
#: utilisation figure.
DOCUMENT_TYPES: tuple[str, ...] = ("PR", "PO", "GRN", "BILL", "IMR")

#: EACH ENTRY NAMES THE BUCKETS ITS OWN BRANCH EMITS, AND NOTHING ELSE.
#: `received_not_billed` reads as a GRN measure and is not one: it is
#: ``GREATEST(0, received - billed)`` PER PO LINE, so it needs the billed
#: figure that offsets it and is computed by `_PO_BRANCH`. `_GRN_BRANCH` emits
#: `received` and nothing more -- the two branches' own comments say why they
#: are separate. Listing it under GRN advertised a bucket that could only ever
#: come back 0 for `document_types=["GRN"]`, which is a wrong number rather
#: than a missing one.
_BUCKETS_BY_DOCUMENT_TYPE: dict[str, tuple[str, ...]] = {
    "PR": ("pr_reserved",),
    "PO": ("ordered", "commitment", "received_not_billed"),
    "GRN": ("received",),
    "BILL": ("actual",),
    # 034: the internal material request supplies both internal buckets.
    "IMR": ("internal_allocation", "internal_consumption"),
}


def _c3_label_by_code() -> dict[str, str]:
    """C3's 21 status codes mapped to the LABEL the document tables store.

    Read from `C3_statuses.json` rather than hand-written, because the two
    forms are both in play and a hand-copied map is a map that drifts:
    migration 013's own header records that it stores "C3 labels ... and not
    the code", so `purchase_request.status` holds ``'Under Review'`` while the
    frozen identifier is ``UNDER_REVIEW``.

    A `FilterSet` names CODES. A filter keyed on display text would break the
    day a label is retitled, and C3's rule is that the code is the identity.
    """
    data = json.loads((_CONTRACTS / "C3_statuses.json").read_text(encoding="utf-8"))
    return {row["code"]: row["label"] for row in data["statuses"]}


#: C3 code -> stored label. See :func:`_c3_label_by_code`.
C3_LABEL_BY_CODE: dict[str, str] = _c3_label_by_code()

#: C15's approval-INSTANCE statuses, which are a DIFFERENT NAMESPACE from C3
#: and must never be interchanged with one.
#:
#: C15's own rule: "An approval status must NEVER be rendered on a business
#: screen as if it were a C3 status." Four codes share a label with a C3
#: status -- APPROVED, REJECTED, RETURNED, EXCEPTION_PENDING -- and that
#: overlap is reviewed and deliberate. They are not the same fact: an approval
#: instance being APPROVED is a decision on one object version; a purchase
#: order being APPROVED is a document lifecycle state.
#:
#: So `FilterSet.lifecycle_statuses` and `FilterSet.approval_statuses` are two
#: fields and not one, they resolve against two different tables, and neither
#: is ever used as a fallback for the other.
APPROVAL_INSTANCE_STATUSES: tuple[str, ...] = (
    "OPEN", "APPROVED", "REJECTED", "RETURNED", "RECALLED", "SUPERSEDED",
    "EXCEPTION_PENDING",
)


# ===========================================================================
# Dimensions
# ===========================================================================

@dataclass(frozen=True)
class Dimension:
    """One reportable axis: how to group by it and how to label it."""

    name: str
    #: The SQL expression selecting the dimension's KEY from the fact CTE.
    key_sql: str
    #: The SQL expression selecting a human label, or None when the key is one.
    label_sql: str | None
    description: str


#: The five dimensions REQ-RPT-010 names, plus the WBS element the control cell
#: is actually keyed on.
#:
#: `category` IS `budget_head`, and that is AMB-04's assumed primary reading
#: written down rather than left implicit. The ambiguity is OPEN -- "category"
#: could mean the budget head, an asset category or an item category, and all
#: three exist in the model. The plan's mitigation is to carry the assumed
#: reading and retain the others as additional dimensions "so the assumption is
#: cheap if wrong". The other two are not served today: there is no asset
#: category on any table 001..017 creates, and `item_master.category` is
#: unreachable from the document chain (see the module docstring's
#: `item_ids`). Aliasing `category` onto the head is therefore the honest
#: single reading, not a silent choice between three.
DIMENSIONS: dict[str, Dimension] = {
    "entity": Dimension(
        "entity", "f.entity_id", "e.name", "Legal entity"),
    "plant": Dimension(
        "plant", "f.plant_id", "pl.name", "Plant"),
    "location": Dimension(
        "location", "f.location_id", "lo.name", "Location"),
    "project": Dimension(
        "project", "f.project_id", "pr.capex_code", "CAPEX project"),
    "wbs": Dimension(
        "wbs", "f.wbs_id", "w.wbs_code", "WBS element"),
    "budget_head": Dimension(
        "budget_head", "f.budget_head_id", "bh.name", "Budget head"),
    "category": Dimension(
        "category", "f.budget_head_id", "bh.name",
        "Category (AMB-04: read as the budget head)"),
    #: FABLE 5.1 / migration 026, resolving AMB-04 for real: a GENUINE
    #: classification master, `budget_category`, independent of `budget_head`
    #: and never aliased to it -- product-owner decision, recorded in 026's own
    #: header. A cell carries exactly one category, assigned on release of the
    #: original budget that grants it, and reporting reads THAT -- the CELL's
    #: `budget_control_cell.budget_category_id` -- not the document line's, so a
    #: cell migrated before 026 (category NULL) renders "Unclassified" rather
    #: than vanishing from a grouped report. `category` (above) stays the
    #: pre-026 alias to `budget_head` for anything that still reads it; this is
    #: the new, real axis, composable WITH `budget_head` in the same `group_by`
    #: because the two are genuinely different columns.
    "budget_category": Dimension(
        "budget_category", "f.budget_category_id", "bcat.name",
        "Budget category (migration 026: a classification dimension separate "
        "from budget head, never an alias of it -- AMB-04 resolved)"),
    #: `bill.vendor_id` (014) is the only vendor identity this schema carries;
    #: `purchase_order` still has only `vendor_name` text. So `vendor` is real
    #: on the `actual` bucket alone -- see `VENDOR_SUPPORTED_BUCKETS` and
    #: `validate()`, which refuses combining it with any bucket that has no
    #: vendor rather than silently leaving those buckets unfiltered.
    "vendor": Dimension(
        "vendor", "f.vendor_id", "vm.name",
        "Vendor (bill.vendor_id only; usable with document_types=['BILL'] -- "
        "see FILTER_UNSUPPORTED_FOR_METRIC)"),
}

#: `category` and `budget_head` select the SAME column, so naming both in one
#: `group_by` would produce two identical columns and a card that appears to
#: break a total down twice. Refused rather than de-duplicated silently: the
#: caller asked for something that cannot mean what they think it means.
#:
#: `budget_category` is DELIBERATELY ABSENT from this table. It is a real,
#: independent column (migration 026) and not a second name for `budget_head`
#: or `category` -- grouping by `budget_head` AND `budget_category` together is
#: exactly what AMB-04's resolution asks reporting to be able to do.
_ALIASED_DIMENSIONS: tuple[frozenset[str], ...] = (
    frozenset({"category", "budget_head"}),
)

#: The only buckets `vendor` can filter or group by, because `bill.vendor_id`
#: is the only vendor identity in this schema (014). `budget`/`original`/
#: `revisions` are included because they are the unconditional denominator
#: (see `buckets()`) and carry no vendor of their own to contradict.
VENDOR_SUPPORTED_BUCKETS: frozenset[str] = frozenset(
    {"budget", "original", "revisions", "actual"})


# ===========================================================================
# Filters this schema cannot express
# ===========================================================================

@dataclass(frozen=True)
class UnsupportedFilter:
    """A `FilterSet` field with no column to bind to, and why."""

    field_name: str
    code: str
    detail: str


#: Declared, not commented. `api/reports.py` renders these as an `unavailable`
#: state naming exactly what is missing; a test asserts the refusal.
#:
#: A DROPPED FILTER IS WORSE THAN A REFUSED ONE. The reader gets a total that
#: looks right, computed over a population they did not ask for, with nothing
#: on screen saying so.
UNSUPPORTED_FILTERS: dict[str, UnsupportedFilter] = {
    "item_ids": UnsupportedFilter(
        "item_ids", "ITEM_DIMENSION_ABSENT",
        "Filtering by item is not available: no item_id column exists on "
        "pr_line, po_line, grn_line or bill_line, even after migration 027. "
        "item_master (migration 005) is not referenced from the procurement "
        "document chain, so there is nothing to join. This filter is refused "
        "rather than ignored -- a report that silently drops it returns a "
        "correct-looking total for a population nobody asked for."),
    # `vendor_ids` is NOT here. `bill.vendor_id` (014) makes it real for the
    # `actual` bucket, so it is now a CONDITIONAL refusal -- see
    # `FilterSet.validate` and `VENDOR_SUPPORTED_BUCKETS` -- rather than a
    # blanket one: naming `vendor_ids` together with `document_types=['BILL']`
    # (or leaving `document_types` unset while grouping is restricted the same
    # way) is honoured; naming it alongside any bucket with no vendor of its
    # own is refused by the SAME "never half-answer" rule, with a 422 naming
    # exactly which buckets could not honour it.
    "division_ids": UnsupportedFilter(
        "division_ids", "DIVISION_DIMENSION_ABSENT",
        "Filtering by division is not available: `division` (migration 001) "
        "is reachable only from `original_budget_line` (migration 026), the "
        "ORIGINAL BUDGET DOCUMENT's own entry line. Nothing in the reporting "
        "fact -- budget_line, po_line, grn_line, bill_line, pr_reservation -- "
        "carries a division_id, so there is no column to join across every "
        "bucket. Refused rather than applied to one bucket and silently "
        "dropped from the rest."),
    "branch_ids": UnsupportedFilter(
        "branch_ids", "BRANCH_DIMENSION_ABSENT",
        "Filtering by branch is not available for the same reason as "
        "division: `branch` (migration 001) is reachable only from "
        "`original_budget_line` (migration 026) and no table in the reporting "
        "fact carries a branch_id."),
    "zone_ids": UnsupportedFilter(
        "zone_ids", "ZONE_DIMENSION_ABSENT",
        "Filtering by zone is not available for the same reason as division "
        "and branch: `zone` (migration 001) is reachable only from "
        "`original_budget_line` (migration 026) and no table in the reporting "
        "fact carries a zone_id."),
    "fiscal_years": UnsupportedFilter(
        "fiscal_years", "FISCAL_YEAR_DIMENSION_ABSENT",
        "Filtering by fiscal year is not available: `fiscal_year` (migration "
        "026) is a column of `original_budget`, the DOCUMENT, not of "
        "`budget_line` or any other table the reporting fact reads. Use "
        "`period_ids` instead -- `accounting_period` is what this schema "
        "actually resolves a date against, per entity."),
    "requestor_ids": UnsupportedFilter(
        "requestor_ids", "REQUESTOR_DIMENSION_ABSENT",
        "Filtering by requestor is not available across the whole report: "
        "purchase_request.requested_by exists (013) but nothing on "
        "budget_line, po_line, grn_line or bill_line names a requestor, so "
        "this filter would narrow pr_reserved alone and leave the other eight "
        "components unfiltered. Refused rather than half-answered."),
    "approver_ids": UnsupportedFilter(
        "approver_ids", "APPROVER_DIMENSION_ABSENT",
        "Filtering by approver is not available: the approving identity this "
        "schema records -- original_budget.approving_authority (026), "
        "approval_instance's own actors -- sits on documents the reporting "
        "fact does not read line-by-line. There is no single column shared "
        "across budget_line, po_line, grn_line, bill_line and pr_reservation "
        "to filter on."),
}


class FilterError(ValueError):
    """A `FilterSet` the reporting layer refuses, with a code and a reason.

    Raised for a filter the schema cannot express, an unknown dimension, a
    contradictory range and an unreadable cursor. Never raised for an
    out-of-scope id: that narrows to no rows, which is the whole point.
    """

    def __init__(self, code: str, message: str, *, status: int = 422,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


# ===========================================================================
# The canonical FilterSet
# ===========================================================================

def _tuple(values: Iterable[str] | None) -> tuple[str, ...] | None:
    """`None` (unfiltered) or a de-duplicated, ordered tuple.

    `None` and `()` are DIFFERENT and the difference is load-bearing, exactly
    as it is on `Scope`: `None` means "do not filter on this", `()` means
    "restrict to nothing", which matches no row. A caller who sends an empty
    list asked for an empty answer and gets one -- collapsing that to
    "unfiltered" would silently widen their query.
    """
    if values is None:
        return None
    return tuple(dict.fromkeys(str(v) for v in values))


@dataclass(frozen=True)
class FilterSet:
    """THE canonical filter object. A2, A3 and A4 import this one.

    Every field is `None` for "unfiltered" or a tuple for "restricted to
    these". An EMPTY tuple restricts to nothing and matches no row; it is not
    the same as `None`, and `_tuple` above says why.

    A `FilterSet` NARROWS. It is ANDed onto the scope predicate compiled from
    the caller's own grants and can never widen it: naming an entity the caller
    has no grant for yields no rows, not an error confirming the entity exists.
    """

    # ---- organisational dimensions ----------------------------------------
    entity_ids: tuple[str, ...] | None = None
    plant_ids: tuple[str, ...] | None = None
    location_ids: tuple[str, ...] | None = None
    project_ids: tuple[str, ...] | None = None

    #: ltree SUBTREE paths, not ids. `wbs_path <@ %(path)s` selects an element
    #: and everything beneath it, which is what "WBS subtree" means and what a
    #: list of ids cannot express without the caller walking the tree first.
    wbs_paths: tuple[str, ...] | None = None

    # ---- control-cell dimensions ------------------------------------------
    budget_head_ids: tuple[str, ...] | None = None
    #: AMB-04. Resolves against `budget_head_id` -- the assumed primary
    #: reading. Given ALONGSIDE `budget_head_ids` the two INTERSECT, because
    #: both narrow and a filter never widens.
    category_ids: tuple[str, ...] | None = None
    #: FABLE 5.1: the REAL category master (migration 026), resolved against
    #: the CELL's `budget_control_cell.budget_category_id` -- never the same
    #: column as `budget_head_ids` or `category_ids`, and freely composable
    #: with both because AMB-04 makes them independent dimensions.
    budget_category_ids: tuple[str, ...] | None = None

    # ---- conditionally refused: see VENDOR_SUPPORTED_BUCKETS / validate() -
    #: Real only on `bill.vendor_id` (014). Naming it together with a bucket
    #: that has no vendor of its own (`ordered`, `commitment`, `received`,
    #: `received_not_billed`, `pr_reserved`) is refused in `validate()` with
    #: `FILTER_UNSUPPORTED_FOR_METRIC` -- never silently left unfiltered on
    #: those buckets.
    vendor_ids: tuple[str, ...] | None = None

    # ---- refused, and refused loudly: see UNSUPPORTED_FILTERS -------------
    item_ids: tuple[str, ...] | None = None
    #: `division` / `branch` / `zone` (migration 001) are reachable only from
    #: `original_budget_line` (026), the document's own entry, and not from
    #: any table the reporting fact reads. Declared as fields so a caller who
    #: names them is REFUSED with a coded reason -- see UNSUPPORTED_FILTERS --
    #: rather than having FastAPI silently ignore an unrecognised parameter.
    division_ids: tuple[str, ...] | None = None
    branch_ids: tuple[str, ...] | None = None
    zone_ids: tuple[str, ...] | None = None
    #: `original_budget.fiscal_year` (026) is a document column; the fact has
    #: no fiscal_year anywhere. Use `period_ids`.
    fiscal_years: tuple[str, ...] | None = None
    #: `purchase_request.requested_by` (013) exists but is PR-only; there is no
    #: shared "requestor" column across the other four buckets.
    requestor_ids: tuple[str, ...] | None = None
    #: The approving identity sits on documents (`original_budget.
    #: approving_authority`, `approval_instance`), not on the fact's own rows.
    approver_ids: tuple[str, ...] | None = None

    # ---- document selection ------------------------------------------------
    document_types: tuple[str, ...] | None = None
    #: C3 CODES (e.g. `FULLY_COMMITTED`), never C3 labels and never C15 codes.
    lifecycle_statuses: tuple[str, ...] | None = None
    #: C15 INSTANCE codes (e.g. `EXCEPTION_PENDING`), a separate namespace.
    approval_statuses: tuple[str, ...] | None = None

    # ---- time --------------------------------------------------------------
    #: Applied to EACH BUCKET'S OWN DOCUMENT DATE -- see `_FACT_SQL`. A bill
    #: dated in March and the purchase order it bills, raised in January, are
    #: not the same event and must not share one date column.
    date_from: date | None = None
    date_to: date | None = None
    #: `accounting_period` ids, NOT a raw date range. A period is per entity and
    #: carries its own start and end, so this is resolved to per-entity ranges
    #: rather than flattened into one span that would be wrong for any entity
    #: whose calendar differs.
    period_ids: tuple[str, ...] | None = None

    # ---- shape -------------------------------------------------------------
    #: Ordered. `("entity", "project")` groups by entity then project, and the
    #: order is preserved into the result rows and into the sort.
    group_by: tuple[str, ...] = ()
    sort: str | None = None
    sort_desc: bool = False
    cursor: str | None = None
    limit: int = 100

    # -------------------------------------------------------------- building
    @classmethod
    def build(cls, **kwargs: Any) -> "FilterSet":
        """Normalise loose input (lists, strings, dates) into a `FilterSet`.

        The one constructor callers should use. It coerces every collection
        through `_tuple`, so `None` versus `[]` survives, and it VALIDATES --
        an unknown dimension or an unsupported filter is refused here rather
        than producing a query that quietly means something else.
        """
        fields = {
            name: _tuple(kwargs.pop(name, None))
            for name in ("entity_ids", "plant_ids", "location_ids",
                         "project_ids", "wbs_paths", "budget_head_ids",
                         "category_ids", "budget_category_ids",
                         "vendor_ids", "item_ids",
                         "division_ids", "branch_ids", "zone_ids",
                         "fiscal_years", "requestor_ids", "approver_ids",
                         "document_types", "lifecycle_statuses",
                         "approval_statuses", "period_ids")
        }
        group_by = _tuple(kwargs.pop("group_by", None)) or ()
        limit = kwargs.pop("limit", None)
        instance = cls(
            **fields,
            group_by=tuple(group_by),
            date_from=_as_date(kwargs.pop("date_from", None), "date_from"),
            date_to=_as_date(kwargs.pop("date_to", None), "date_to"),
            sort=kwargs.pop("sort", None),
            sort_desc=bool(kwargs.pop("sort_desc", False)),
            cursor=kwargs.pop("cursor", None),
            limit=DEFAULT_LIMIT if limit is None else int(limit),
        )
        if kwargs:
            raise FilterError(
                "UNKNOWN_FILTER",
                f"Unknown filter field(s): {', '.join(sorted(kwargs))}. A "
                f"filter this layer does not recognise is refused, never "
                f"ignored -- an ignored filter returns a correct-looking "
                f"total for a population nobody asked for.")
        instance.validate()
        return instance

    # ------------------------------------------------------------ validation
    def unsupported(self) -> tuple[UnsupportedFilter, ...]:
        """Every refused filter this `FilterSet` actually sets.

        Setting a field to `None` is not using it. Only a filter the caller
        genuinely supplied is reported, so a default-constructed `FilterSet`
        is never "unavailable".
        """
        return tuple(
            spec for name, spec in UNSUPPORTED_FILTERS.items()
            if getattr(self, name) is not None
        )

    def validate(self) -> None:
        """Refuse anything that cannot mean what the caller thinks it means."""
        unsupported = self.unsupported()
        if unsupported:
            raise FilterError(
                unsupported[0].code,
                unsupported[0].detail,
                status=422,
                detail={"unsupported_filters": [
                    {"field": u.field_name, "code": u.code, "detail": u.detail}
                    for u in unsupported]})

        unknown = [d for d in self.group_by if d not in DIMENSIONS]
        if unknown:
            raise FilterError(
                "UNKNOWN_DIMENSION",
                f"Cannot group by {', '.join(unknown)}. The reportable "
                f"dimensions are: {', '.join(sorted(DIMENSIONS))}.")

        if len(set(self.group_by)) != len(self.group_by):
            raise FilterError(
                "DUPLICATE_DIMENSION",
                f"group_by names the same dimension twice: "
                f"{', '.join(self.group_by)}.")

        for alias_group in _ALIASED_DIMENSIONS:
            named = alias_group & set(self.group_by)
            if len(named) > 1:
                raise FilterError(
                    "ALIASED_DIMENSION",
                    f"{' and '.join(sorted(named))} select the same column "
                    f"(AMB-04 reads 'category' as the budget head), so "
                    f"grouping by both would break one total down twice into "
                    f"identical columns. Name one.")

        # VENDOR IS REAL ON ONE BUCKET ONLY: `bill.vendor_id` (014).
        # `purchase_order` still carries `vendor_name` text and nothing else in
        # the fact names a vendor at all, so filtering or grouping by vendor
        # while any OTHER bucket is also selected would either apply the
        # filter to `actual` alone and leave `ordered`/`commitment` across
        # every vendor (silently wider), or -- worse -- match no row on a
        # bucket that has no `vendor_id` to compare against (silently empty).
        # Both are refused by NAME: the caller must restrict `document_types`
        # to `['BILL']` (or a subset whose `buckets()` stays inside
        # `VENDOR_SUPPORTED_BUCKETS`) before vendor can be honoured at all.
        if self.vendor_ids is not None or "vendor" in self.group_by:
            unsupported_buckets = tuple(sorted(
                set(self.buckets()) - VENDOR_SUPPORTED_BUCKETS))
            if unsupported_buckets:
                raise FilterError(
                    "FILTER_UNSUPPORTED_FOR_METRIC",
                    f"vendor cannot be applied together with "
                    f"{', '.join(unsupported_buckets)}: only bill.vendor_id "
                    f"(migration 014) carries a vendor, so a vendor filter or "
                    f"grouping is honoured only when document_types narrows to "
                    f"a selection whose buckets are all one of "
                    f"{', '.join(sorted(VENDOR_SUPPORTED_BUCKETS))}. Narrow "
                    f"document_types (e.g. to ['BILL']), or drop the vendor "
                    f"filter/grouping -- never answered with vendor silently "
                    f"unfiltered on the rest.",
                    detail={"dimension": "vendor",
                            "unsupported_buckets": list(unsupported_buckets)})

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise FilterError(
                "INVALID_DATE_RANGE",
                f"date_from ({self.date_from.isoformat()}) is after date_to "
                f"({self.date_to.isoformat()}); the range selects nothing. "
                f"A range that cannot match is refused rather than answered "
                f"with an empty table that looks like 'no activity'.")

        for name, permitted in (("document_types", DOCUMENT_TYPES),
                                ("lifecycle_statuses", tuple(C3_LABEL_BY_CODE)),
                                ("approval_statuses", APPROVAL_INSTANCE_STATUSES)):
            values = getattr(self, name)
            if values is None:
                continue
            bad = [v for v in values if v not in permitted]
            if bad:
                raise FilterError(
                    f"UNKNOWN_{name.upper()}",
                    f"{name} contains value(s) no contract defines: "
                    f"{', '.join(bad)}. Permitted: {', '.join(permitted)}.")

        if self.sort is not None and self.sort not in SORTABLE:
            raise FilterError(
                "UNKNOWN_SORT",
                f"Cannot sort by {self.sort}. Sortable: "
                f"{', '.join(sorted(SORTABLE))}.")

        if self.limit < 1 or self.limit > MAX_LIMIT:
            raise FilterError(
                "INVALID_LIMIT",
                f"limit must be between 1 and {MAX_LIMIT}; got {self.limit}.")

    # ------------------------------------------------------------ derivation
    def buckets(self) -> tuple[str, ...]:
        """The metric components this `FilterSet`'s document types supply.

        `budget` is always present: it is sourced from `budget_line`, which is
        not a document type at all, and a utilisation percentage with no
        denominator is not a utilisation percentage.
        """
        if self.document_types is None:
            return COMPONENTS
        selected = {"budget", "original", "revisions"}
        for document_type in self.document_types:
            selected.update(_BUCKETS_BY_DOCUMENT_TYPE.get(document_type, ()))
        return tuple(c for c in COMPONENTS if c in selected)

    def narrowed_to(self, dimension: str, key: str | None) -> "FilterSet":
        """This `FilterSet` plus one clicked dimension. The drill-down contract.

        "Every card, chart segment and total is clickable to the rows behind
        it, and the drill-down carries the SAME `FilterSet` plus the clicked
        dimension." This is that operation, and it is here rather than in each
        caller so the two can never differ.

        NARROWING IS AN INTERSECTION, never a replacement. Clicking `entity=E2`
        on a report already filtered to `entity in (E1,)` yields `()` -- no
        rows -- because E2 was not in the population the total was computed
        over. Replacing the value would silently widen the query and show rows
        the clicked figure never counted.
        """
        if dimension not in DIMENSIONS:
            raise FilterError(
                "UNKNOWN_DIMENSION",
                f"Cannot drill into {dimension}. The reportable dimensions "
                f"are: {', '.join(sorted(DIMENSIONS))}.")
        if dimension == "wbs":
            # A WBS drill-down narrows a SUBTREE, not an id set -- there is no
            # `wbs_ids` field, because "this element and everything under it"
            # is what a control hierarchy means and an id list cannot say it
            # without the caller walking the tree first.
            return _narrow_wbs(self, key)
        target = _DRILL_FIELD[dimension]
        existing: tuple[str, ...] | None = getattr(self, target)
        if key is None:
            # A NULL group key -- e.g. a project with no plant. Nothing to
            # intersect on, and inventing a sentinel id would be a fabricated
            # filter. The cursor/group key carries the NULL through instead.
            return replace(self, cursor=None)
        if existing is None:
            narrowed: tuple[str, ...] = (key,)
        else:
            narrowed = tuple(v for v in existing if v == key)
        return replace(self, cursor=None, **{target: narrowed})

    # --------------------------------------------------------- serialisation
    def to_json(self) -> dict[str, Any]:
        """The stored form for `report_saved_view.definition`.

        `cursor` is deliberately EXCLUDED. A cursor is a position in one
        reader's pagination of one execution; storing it would make a saved
        view open on page four of a result set that no longer exists.
        """
        out: dict[str, Any] = {}
        for name in _SERIALISED_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, date):
                out[name] = value.isoformat()
            elif isinstance(value, tuple):
                out[name] = list(value)
            else:
                out[name] = value
        return out

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FilterSet":
        """Rebuild from a stored definition, REFUSING an unknown key.

        An unknown key is a definition written by a newer build, or a hand-
        edited row. Ignoring it would apply fewer filters than the view says it
        applies -- the reader sees the saved view's name, believes its filters
        are in force, and reads a wider population than they asked for.
        """
        if not isinstance(payload, Mapping):
            raise FilterError(
                "MALFORMED_VIEW_DEFINITION",
                "A saved view's definition must be a JSON object.")
        unknown = sorted(set(payload) - set(_SERIALISED_FIELDS))
        if unknown:
            raise FilterError(
                "MALFORMED_VIEW_DEFINITION",
                f"This saved view names filter(s) this build does not know: "
                f"{', '.join(unknown)}. It was not applied. Re-save the view, "
                f"or open it on the build that wrote it -- silently dropping "
                f"an unknown filter would show a wider population than the "
                f"view's name promises.")
        return cls.build(**dict(payload))


#: Fields that round-trip through a saved view. `cursor` is absent by design.
_SERIALISED_FIELDS: tuple[str, ...] = (
    "entity_ids", "plant_ids", "location_ids", "project_ids", "wbs_paths",
    "budget_head_ids", "category_ids", "budget_category_ids",
    "vendor_ids", "item_ids",
    "division_ids", "branch_ids", "zone_ids", "fiscal_years",
    "requestor_ids", "approver_ids",
    "document_types", "lifecycle_statuses", "approval_statuses",
    "date_from", "date_to", "period_ids", "group_by", "sort", "sort_desc",
    "limit",
)

#: dimension -> the `FilterSet` field a drill-down on it narrows.
#:
#: `category` and `budget_head` both narrow `budget_head_ids` (AMB-04), which
#: is what makes clicking a "category" segment and clicking the equivalent
#: "budget head" segment produce the SAME rows rather than two populations.
#:
#: `wbs` is deliberately ABSENT: it narrows a subtree PATH, not an id set, and
#: `narrowed_to` routes it to :func:`_narrow_wbs`. Listing it here with a
#: `wbs_ids` target would name a field `FilterSet` does not have.
_DRILL_FIELD: dict[str, str] = {
    "entity": "entity_ids", "plant": "plant_ids", "location": "location_ids",
    "project": "project_ids", "budget_head": "budget_head_ids",
    "category": "budget_head_ids",
    #: FABLE 5.1: `budget_category` narrows its OWN field, `budget_category_ids`
    #: -- never `budget_head_ids`. It is a genuinely separate dimension, not the
    #: `category`/`budget_head` alias above.
    "budget_category": "budget_category_ids",
    "vendor": "vendor_ids",
}

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

#: Sortable measures. Every one is a metric, and every sort is tie-broken on
#: the group key -- see `_order_by`.
SORTABLE: frozenset[str] = frozenset(
    set(COMPONENTS) | {"exposure", "available", "utilisation_pct", "key"})


def _as_date(value: Any, field_name: str) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FilterError(
            "INVALID_DATE",
            f"{field_name} must be an ISO date (YYYY-MM-DD); got "
            f"{value!r}.") from exc


# `wbs_ids` is not a FilterSet field -- a WBS drill-down narrows the SUBTREE
# path, which is what `wbs_paths` carries. Handled in `narrowed_to` via this
# indirection so `_DRILL_FIELD` stays a plain mapping.
def _narrow_wbs(filters: "FilterSet", wbs_path: str | None) -> "FilterSet":
    """Drill into one WBS element by its ltree path, keeping the subtree."""
    if wbs_path is None:
        return replace(filters, cursor=None)
    existing = filters.wbs_paths
    if existing is None:
        return replace(filters, cursor=None, wbs_paths=(wbs_path,))
    # An intersection over SUBTREES: keep the clicked path only if it is
    # inside one already selected, so the drill-down cannot escape the
    # population the total was computed over.
    inside = tuple(
        wbs_path for parent in existing
        if wbs_path == parent or wbs_path.startswith(f"{parent}.")
    )
    return replace(filters, cursor=None, wbs_paths=inside[:1])


# ===========================================================================
# The fact CTE -- every bucket, at control-cell grain
# ===========================================================================
#
# FIVE BRANCHES UNIONed, each emitting the same fifteen columns: the six
# dimension keys and one column per component. They are UNIONed and never
# JOINed, for the reason `procurement.reconcile_po_lines` gives about its own
# scalar subqueries: a PO line with two receipts and two bill lines produces
# four rows under a join and every total doubles. That is the classic fan-out,
# and here it doubles money.
#
# EACH BRANCH FILTERS ON ITS OWN DOCUMENT DATE -- `budget_line.effective_from`,
# `purchase_order.ordered_at`, `grn.received_at`, `bill.bill_date`,
# `purchase_request.requested_at`. A bill dated in March and the purchase order
# it bills, raised in January, are not the same event; one shared date column
# would file a commitment and its own actual in different periods.
#
# THE SCOPE PREDICATE SITS IN THE OUTER QUERY, ONCE, over the unioned fact --
# the shape `procurement.reconciliation_lines` uses and for the same reason.
# Every branch selects `p.entity_id`, `p.plant_id`, `p.location_id` and
# `p.project_id` from a `project` join, so the fact carries all four dimensions
# on every row and one predicate filters all five branches. There is exactly
# one place to read to know that it does, rather than five that must agree.

#: Every branch's SELECT list, in one place, so a new branch cannot emit its
#: columns in a different order and silently write `received` into `actual`.
_FACT_COLUMNS: tuple[str, ...] = (
    "entity_id", "plant_id", "location_id", "project_id",
    "wbs_id", "wbs_path", "budget_head_id",
    #: FABLE 5.1: the cell's category (026), never the document line's -- and
    #: `vendor_id` / `branch_kind`, which exist so a vendor filter can be
    #: refused OR honoured per branch (`VENDOR_SUPPORTED_BUCKETS`) instead of
    #: refused outright.
    "budget_category_id", "vendor_id", "branch_kind",
    *(f"{component}_paise" for component in COMPONENTS),
)


def _zeros_except(**supplied: str) -> str:
    """The component columns (nine before 034, eleven since), with
    `0::bigint` for every bucket a branch does not supply.

    Written once rather than typed out five times: a hand-written branch that
    puts its total in the wrong slot is a defect no test of the SQL's *shape*
    would catch, and every branch here supplies at most three of nine.
    """
    return ",\n           ".join(
        f"{supplied.get(component, '0::bigint')} AS {component}_paise"
        for component in COMPONENTS
    )


def _period_on(date_column: str) -> str:
    """The `period_ids` predicate for one branch, over that branch's own date.

    AN ACCOUNTING PERIOD IS PER ENTITY, AND THIS IS WHY IT IS NOT A DATE RANGE.
    `accounting_period` carries `entity_id`, `period_start` and `period_end`,
    and 001's EXCLUDE constraint only stops one ENTITY's periods overlapping --
    two entities may run different calendars, and several do in a group with a
    transitional financial year. Resolving a period id to a single
    ``from``/``to`` span in Python and applying it estate-wide would therefore
    be right for whichever entity was resolved first and silently wrong for the
    rest, filing another entity's March into its own February.

    So the period stays a JOIN, correlated on `p.entity_id`, and each branch
    matches ITS OWN document date against the period belonging to the row's own
    entity. A period id naming an entity outside the caller's scope contributes
    no rows -- the fact is already scope-filtered in the outer query -- rather
    than an error confirming the period exists.
    """
    return f"""
      AND (%(period_ids)s::text[] IS NULL
           OR EXISTS (SELECT 1 FROM accounting_period ap
                      WHERE ap.period_id = ANY(%(period_ids)s)
                        AND ap.entity_id = p.entity_id
                        AND {date_column}
                            BETWEEN ap.period_start AND ap.period_end))"""


#: `budget` / `original` / `revisions`. AUD-C-005: Approved AND effective only.
#: Draft, Submitted, Rejected, Cancelled and future-dated lines create no
#: spending capacity.
#:
#: `effective_from` / `effective_to` AND NOT `effective_date`. The SQLite
#: `domain.compute_ledger` reads `effective_date <= today`; migration 003
#: created a RANGE with two columns and no `effective_date` at all.
#: Transcribing the SQLite name would have raised `UndefinedColumn` in CI and
#: on no workstation here -- which is exactly how a sibling agent shipped 27
#: live tests that all skipped locally and would all have errored in CI.
_BUDGET_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           bl.wbs_id, w.wbs_path, bl.budget_head_id, bc.budget_category_id,
           NULL::text AS vendor_id, 'BUDGET'::text AS branch_kind,
           {_zeros_except(
               budget="SUM(bl.amount_paise)::bigint",
               original=("SUM(CASE WHEN bl.kind = 'ORIGINAL' "
                         "THEN bl.amount_paise ELSE 0 END)::bigint"),
               revisions=("SUM(CASE WHEN bl.kind <> 'ORIGINAL' "
                          "THEN bl.amount_paise ELSE 0 END)::bigint"))}
    FROM budget_line bl
    JOIN wbs_element w ON w.wbs_id = bl.wbs_id
    JOIN project p ON p.project_id = w.project_id
    -- FABLE 5.1 / migration 026: the CELL's category, read through a LEFT
    -- JOIN so a cell that pre-dates 026 (budget_category_id NULL) still
    -- contributes its budget rather than vanishing from a grouped report --
    -- it renders "Unclassified" (see `_shape`), never absent.
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = bl.wbs_id AND bc.budget_head_id = bl.budget_head_id
    WHERE bl.status = 'Approved'
      AND bl.effective_from <= %(as_of)s
      AND (bl.effective_to IS NULL OR bl.effective_to >= %(as_of)s)
      AND (%(date_from)s::date IS NULL OR bl.effective_from >= %(date_from)s)
      AND (%(date_to)s::date IS NULL OR bl.effective_from <= %(date_to)s)
      {_period_on("bl.effective_from")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             bl.wbs_id, w.wbs_path, bl.budget_head_id, bc.budget_category_id
"""

#: `ordered`, `commitment` and `received_not_billed`, at PO LINE grain.
#:
#: THE TWO `GREATEST(0, a - b)` QUANTITIES ARE COMPUTED PER LINE AND ONLY THEN
#: SUMMED, which is the entire reason this branch has an inner subquery instead
#: of aggregating directly.
#: ``SUM(GREATEST(0, a-b)) <> GREATEST(0, SUM(a) - SUM(b))`` the moment one
#: line is over-billed -- and over-billing is a case this product REPORTS
#: rather than clamps (`procurement.reconcile_po_lines` returns
#: `over_billed_paise` positive and leaves the row visible), so it is not
#: hypothetical. Aggregating first would net one line's over-billing against
#: another line's open commitment and understate exposure.
#:
#: OPEN COMMITMENT IS ORDERED LESS BILLED, NEVER ORDERED LESS RECEIVED. A PO in
#: `Cancelled` or `Closed` holds none at all.
_PO_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           line.wbs_id, w.wbs_path, line.budget_head_id, bc.budget_category_id,
           -- `purchase_order` carries `vendor_name` TEXT, not a vendor_master
           -- id (014 only gave `bill` that FK). `vendor` is NULL here by
           -- construction, and `validate()` refuses combining a vendor filter
           -- with the `ordered`/`commitment` buckets this branch supplies
           -- rather than let it silently match nothing.
           NULL::text AS vendor_id, 'PO'::text AS branch_kind,
           {_zeros_except(
               ordered="SUM(line.ordered_paise)::bigint",
               commitment=("SUM(CASE WHEN line.po_status = ANY(%(releasing)s) "
                           "THEN 0 ELSE GREATEST(0, line.ordered_paise "
                           "- line.billed_paise) END)::bigint"),
               received_not_billed=("SUM(GREATEST(0, line.received_paise "
                                    "- line.billed_paise))::bigint"))}
    FROM (
        SELECT pl.po_line_id, pl.wbs_id, pl.budget_head_id, pl.project_id,
               po.status AS po_status, po.ordered_at::date AS ordered_at,
               (pl.amount_paise + pl.non_creditable_tax_paise
                + pl.freight_paise)::bigint AS ordered_paise,
               -- `domain.compute_ledger`, verbatim: a Void GRN does not count
               -- at all, and a reversal subtracts the MAGNITUDE of its line, so
               -- a reversal stored positive and one stored negative reduce
               -- received by the same amount.
               COALESCE((SELECT SUM(CASE WHEN g.is_reversal
                                         THEN -ABS(gl.amount_paise)
                                         ELSE gl.amount_paise END)
                         FROM grn_line gl
                         JOIN grn g ON g.grn_id = gl.grn_id
                         WHERE gl.po_line_id = pl.po_line_id
                           AND g.status <> 'Void'), 0)::bigint
                   AS received_paise,
               -- AUD-C-004, verbatim: accounting-effective bills only, tax and
               -- freight included, a `Reversal` bill negated by its status.
               COALESCE((SELECT SUM(CASE WHEN b.accounting_status = 'Reversal'
                                         THEN -ABS(bl2.amount_paise
                                                   + bl2.non_creditable_tax_paise
                                                   + bl2.freight_paise)
                                         ELSE (bl2.amount_paise
                                               + bl2.non_creditable_tax_paise
                                               + bl2.freight_paise) END)
                         FROM bill_line bl2
                         JOIN bill b ON b.bill_id = bl2.bill_id
                         WHERE bl2.po_line_id = pl.po_line_id
                           AND b.accounting_status = ANY(%(effective)s)),
                        0)::bigint AS billed_paise
        FROM po_line pl
        JOIN purchase_order po ON po.po_id = pl.po_id
        WHERE (%(date_from)s::date IS NULL OR po.ordered_at IS NULL
               OR po.ordered_at::date >= %(date_from)s)
          AND (%(date_to)s::date IS NULL OR po.ordered_at IS NULL
               OR po.ordered_at::date <= %(date_to)s)
          AND (%(lifecycle)s::text[] IS NULL OR po.status = ANY(%(lifecycle)s))
    ) AS line
    JOIN wbs_element w ON w.wbs_id = line.wbs_id
    JOIN project p ON p.project_id = line.project_id
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = line.wbs_id AND bc.budget_head_id = line.budget_head_id
    WHERE TRUE
      {_period_on("line.ordered_at")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             line.wbs_id, w.wbs_path, line.budget_head_id, bc.budget_category_id
"""

#: `received`, as a plain total on its own cell grain.
#:
#: SEPARATE FROM `_PO_BRANCH` even though both read `grn_line`, because the two
#: answer different questions at different grains. `received_not_billed` is a
#: per-PO-line RESIDUAL and has to sit beside the billed figure that offsets
#: it; `received` is a plain sum and does not. Computing the plain sum inside
#: the PO branch would have worked, computing the residual here would not, and
#: keeping them apart makes that asymmetry visible rather than load-bearing and
#: undocumented.
_GRN_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           pl.wbs_id, w.wbs_path, pl.budget_head_id, bc.budget_category_id,
           NULL::text AS vendor_id, 'GRN'::text AS branch_kind,
           {_zeros_except(
               received=("SUM(CASE WHEN g.is_reversal "
                         "THEN -ABS(gl.amount_paise) "
                         "ELSE gl.amount_paise END)::bigint"))}
    FROM grn_line gl
    JOIN grn g ON g.grn_id = gl.grn_id
    JOIN po_line pl ON pl.po_line_id = gl.po_line_id
    JOIN wbs_element w ON w.wbs_id = pl.wbs_id
    JOIN project p ON p.project_id = pl.project_id
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = pl.wbs_id AND bc.budget_head_id = pl.budget_head_id
    WHERE g.status <> 'Void'
      AND (%(date_from)s::date IS NULL OR g.received_at::date >= %(date_from)s)
      AND (%(date_to)s::date IS NULL OR g.received_at::date <= %(date_to)s)
      {_period_on("g.received_at::date")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             pl.wbs_id, w.wbs_path, pl.budget_head_id, bc.budget_category_id
"""

#: `actual` -- CWIP. AUD-C-004: accounting-effective bills only.
#:
#: Reads `bill_line.wbs_id` / `.budget_head_id` DIRECTLY, never through
#: `po_line`. A non-PO bill names no PO line at all -- 013 makes both columns
#: nullable for exactly that case -- so routing actuals through the purchase
#: order would drop every direct bill from CWIP, silently, and only for the
#: entities that raise them.
_BILL_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           bl.wbs_id, w.wbs_path, bl.budget_head_id, bc.budget_category_id,
           -- The ONE real vendor identity in this schema: `bill.vendor_id`
           -- (014). Every other branch's `vendor_id` is NULL by construction.
           b.vendor_id, 'BILL'::text AS branch_kind,
           {_zeros_except(
               actual=("SUM(CASE WHEN b.accounting_status = 'Reversal' "
                       "THEN -ABS(bl.amount_paise "
                       "+ bl.non_creditable_tax_paise + bl.freight_paise) "
                       "ELSE (bl.amount_paise + bl.non_creditable_tax_paise "
                       "+ bl.freight_paise) END)::bigint"))}
    FROM bill_line bl
    JOIN bill b ON b.bill_id = bl.bill_id
    JOIN wbs_element w ON w.wbs_id = bl.wbs_id
    JOIN project p ON p.project_id = w.project_id
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = bl.wbs_id AND bc.budget_head_id = bl.budget_head_id
    WHERE b.accounting_status = ANY(%(effective)s)
      AND (%(date_from)s::date IS NULL OR b.bill_date >= %(date_from)s)
      AND (%(date_to)s::date IS NULL OR b.bill_date <= %(date_to)s)
      {_period_on("b.bill_date")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             bl.wbs_id, w.wbs_path, bl.budget_head_id, bc.budget_category_id,
             b.vendor_id
"""

#: `pr_reserved` -- live PR reservations. AUD-H-001.
#:
#: MIGRATION 015'S GRAIN, AND THE ONE JOIN THIS BRANCH MUST NOT MAKE. A
#: reservation is held per (PR x RESOLVED control cell), so a request spanning
#: two cells has TWO rows and summing those rows per cell double counts
#: nothing. Joining to `pr_line` to reach "the request's lines" WOULD double
#: count: several lines can resolve to one cell, and each would multiply that
#: cell's single reservation row. `pr_line` therefore does not appear here at
#: all, and this comment is why a future reader should not add it.
#:
#: `state = 'Reserved'` and nothing else. A Converted reservation has already
#: become PO commitment and a Released one has been given back; counting either
#: alongside `commitment` is the classic double count between two of the four
#: buckets.
_PR_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           r.wbs_id, w.wbs_path, r.budget_head_id, bc.budget_category_id,
           NULL::text AS vendor_id, 'PR'::text AS branch_kind,
           -- 034: LESS the part of the hold an internal allocation has
           -- moved -- that rupee is in the IMR branch, not here. Transcribes
           -- `_RECOMPUTE_DERIVED_SQL`'s pr_reserved limb.
           {_zeros_except(pr_reserved="SUM(r.amount_paise - r.internal_moved_paise)::bigint")}
    FROM pr_reservation r
    JOIN purchase_request req ON req.pr_id = r.pr_id
    JOIN wbs_element w ON w.wbs_id = r.wbs_id
    JOIN project p ON p.project_id = r.project_id
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = r.wbs_id AND bc.budget_head_id = r.budget_head_id
    WHERE r.state = 'Reserved'
      AND (%(date_from)s::date IS NULL
           OR req.requested_at::date >= %(date_from)s)
      AND (%(date_to)s::date IS NULL OR req.requested_at::date <= %(date_to)s)
      AND (%(lifecycle)s::text[] IS NULL OR req.status = ANY(%(lifecycle)s))
      {_period_on("req.requested_at::date")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             r.wbs_id, w.wbs_path, r.budget_head_id, bc.budget_category_id
"""

#: `internal_allocation` and `internal_consumption` (034), at REQUEST grain.
#:
#: The same per-document-then-sum shape as `_PO_BRANCH`, for the same reason:
#: one request's open allocation is `ALLOCATE - ISSUE - CANCEL` floored at
#: zero, and flooring AFTER summing across requests would net one request's
#: excess against another's balance. Transcribes
#: `procurement_services._RECOMPUTE_DERIVED_SQL`'s two internal limbs.
#: A TRANSFER between stores and a CONSUME confirmation carry no paise.
_IMR_BRANCH = f"""
    SELECT p.entity_id, p.plant_id, p.location_id, p.project_id,
           req.wbs_id, w.wbs_path, req.budget_head_id, bc.budget_category_id,
           NULL::text AS vendor_id, 'IMR'::text AS branch_kind,
           {_zeros_except(
               internal_allocation="SUM(GREATEST(0, req.open_paise))::bigint",
               internal_consumption="SUM(GREATEST(0, req.consumed_paise))::bigint")}
    FROM (
        SELECT imr.imr_id, imr.wbs_id, imr.budget_head_id, imr.project_id,
               imr.created_at::date AS requested_on, imr.status,
               COALESCE(SUM(CASE WHEN m.kind = 'ALLOCATE' THEN m.amount_paise
                                 WHEN m.kind IN ('ISSUE', 'CANCEL')
                                      THEN -m.amount_paise
                                 ELSE 0 END), 0)::bigint AS open_paise,
               COALESCE(SUM(CASE WHEN m.kind = 'ISSUE' THEN m.amount_paise
                                 WHEN m.kind = 'RETURN' THEN -m.amount_paise
                                 ELSE 0 END), 0)::bigint AS consumed_paise
        FROM internal_material_request imr
        LEFT JOIN internal_material_movement m ON m.imr_id = imr.imr_id
        GROUP BY imr.imr_id, imr.wbs_id, imr.budget_head_id, imr.project_id,
                 imr.created_at, imr.status
    ) req
    JOIN wbs_element w ON w.wbs_id = req.wbs_id
    JOIN project p ON p.project_id = req.project_id
    LEFT JOIN budget_control_cell bc
           ON bc.wbs_id = req.wbs_id AND bc.budget_head_id = req.budget_head_id
    WHERE (%(date_from)s::date IS NULL OR req.requested_on >= %(date_from)s)
      AND (%(date_to)s::date IS NULL OR req.requested_on <= %(date_to)s)
      AND (%(lifecycle)s::text[] IS NULL OR req.status = ANY(%(lifecycle)s))
      {_period_on("req.requested_on")}
    GROUP BY p.entity_id, p.plant_id, p.location_id, p.project_id,
             req.wbs_id, w.wbs_path, req.budget_head_id, bc.budget_category_id
"""

#: document type -> the branch supplying its buckets. `budget` has no document
#: type: it comes from `budget_line`, which is not a document, and it is always
#: included -- a utilisation percentage with no denominator is not one.
_BRANCH_BY_DOCUMENT_TYPE: dict[str, str] = {
    "PR": _PR_BRANCH, "PO": _PO_BRANCH, "GRN": _GRN_BRANCH, "BILL": _BILL_BRANCH,
    "IMR": _IMR_BRANCH,
}

#: The filters applied ONCE, in the outer query, over the unioned fact.
#:
#: `{scope}` is here and nowhere else. Every branch carries all four dimension
#: columns, so one predicate over `f` filters all five -- and `repo.query`
#: refuses SQL with no token at all, so a sixth branch cannot be added
#: unscoped.
#:
#: `budget_head_ids` and `category_ids` are two parameters ANDed together, not
#: coalesced. Both narrow (AMB-04 reads `category` as the head), and supplying
#: both means the INTERSECTION -- a filter never widens another filter.
_OUTER_PREDICATES = """
      {scope}
      AND (%(entity_ids)s::text[] IS NULL
           OR f.entity_id = ANY(%(entity_ids)s))
      AND (%(plant_ids)s::text[] IS NULL OR f.plant_id = ANY(%(plant_ids)s))
      AND (%(location_ids)s::text[] IS NULL
           OR f.location_id = ANY(%(location_ids)s))
      AND (%(project_ids)s::text[] IS NULL
           OR f.project_id = ANY(%(project_ids)s))
      AND (%(budget_head_ids)s::text[] IS NULL
           OR f.budget_head_id = ANY(%(budget_head_ids)s))
      AND (%(category_ids)s::text[] IS NULL
           OR f.budget_head_id = ANY(%(category_ids)s))
      AND (%(budget_category_ids)s::text[] IS NULL
           OR f.budget_category_id = ANY(%(budget_category_ids)s))
      AND (%(vendor_ids)s::text[] IS NULL
           OR f.branch_kind = 'BUDGET'
           OR f.vendor_id = ANY(%(vendor_ids)s))
      AND (%(wbs_paths)s::text[] IS NULL
           OR EXISTS (SELECT 1 FROM unnest(%(wbs_paths)s::text[]) AS q(path)
                      WHERE f.wbs_path <@ q.path::ltree))
      AND (%(approval_statuses)s::text[] IS NULL
           OR EXISTS (SELECT 1 FROM approval_instance ai
                      WHERE ai.project_id = f.project_id
                        AND ai.status = ANY(%(approval_statuses)s)))
"""

#: `columns=` for every statement in this module. All four dimensions named,
#: NONE waived.
#:
#: A waiver here would be wrong rather than merely lax: the fact carries every
#: dimension on every row precisely so that none has to be waived, and
#: `repo.compile_scope` refuses a restriction it cannot express -- so an
#: omission would raise `ScopeNotExpressible` rather than silently widen. That
#: is the good failure, and naming all four means it never arises.
SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "f.entity_id", "plant": "f.plant_id",
    "location": "f.location_id", "project": "f.project_id",
}

#: `columns=` for the saved-view statements, which read `report_saved_view v`.
#:
#: THE THREE WAIVERS ARE DELIBERATE AND THEY MATCH 017'S POLICY EXACTLY. A
#: saved view carries `entity_id` and no other dimension -- it belongs to an
#: entity, not to a plant -- so `plant`, `location` and `project` are waived by
#: explicit `None`, which is a visible decision rather than an omission that
#: silently widens. `capex_scope_permits(entity_id, NULL, NULL, NULL)` in the
#: policy waives the same three, so the repository predicate and RLS are the
#: same restriction expressed twice rather than two restrictions that could
#: drift.
#:
#: THE ALTERNATIVE WAS WORSE, and it was the first version of this: joining
#: `project` to borrow all four columns. One view joins EVERY project in its
#: entity, so the join fans out -- `list_views` returned a view once per
#: project and `freshness` counted one watermark many times. Waiving what the
#: table does not have is honest; inventing a join to look strict, and
#: multiplying rows to do it, is not.
VIEW_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "v.entity_id", "plant": None, "location": None, "project": None,
}

#: `columns=` for :func:`freshness`, which reads `integration_connection c`.
#: Same shape and same reason: 010 gives that table `entity_id` and nothing
#: else, and its own RLS policy filters exactly that one dimension.
CONNECTION_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "c.entity_id", "plant": None, "location": None, "project": None,
}


# ===========================================================================
# Metric derivation -- `domain._derive`, transcribed
# ===========================================================================

def derive(components: Mapping[str, int]) -> dict[str, Any]:
    """Apply the frozen formulas to one bucket of components.

    `domain._derive`, transcribed: exposure and available are ALWAYS derived
    and never stored, so there is no path by which a persisted `available` can
    disagree with the budget and exposure it is supposed to be the difference
    of.

    EVERY MONEY VALUE OUT OF HERE IS `int`. PostgreSQL's `SUM()` over a
    `bigint` returns **numeric**, psycopg maps numeric to `decimal.Decimal`,
    and a Decimal reaching arithmetic that expects an int is the defect that
    took down the availability verdict, both approval paths and the concurrency
    proof -- in the PostgreSQL CI job only, because a Decimal cannot appear
    without a real server. The `::bigint` casts in the SQL are the first
    defence; `int()` here is the second, because a cast can be forgotten in a
    branch added later and this cannot.
    """
    out = {name: int(components.get(name, 0) or 0) for name in COMPONENTS}
    # 034: the internal limbs are exposure beside the external ones.
    out["exposure"] = (out["commitment"] + out["actual"] + out["pr_reserved"]
                       + out["internal_allocation"] + out["internal_consumption"])
    out["available"] = out["budget"] - out["exposure"]
    out["utilisation_pct"] = (
        round(out["exposure"] / out["budget"] * 100.0, 1) if out["budget"] else 0.0)
    out["band"] = (
        "breach" if out["budget"] and out["exposure"] > out["budget"]
        else "critical" if out["utilisation_pct"] >= CRITICAL_PCT
        else "watch" if out["utilisation_pct"] >= WATCH_PCT
        else "safe")
    return out


# ===========================================================================
# Cursor -- keyset, over the whole tie-broken key
# ===========================================================================

def encode_cursor(key: Sequence[Any]) -> str:
    """The last row's full ordering key, base64url'd.

    KEYSET AND NOT OFFSET, deliberately. An OFFSET cursor re-executes the whole
    query and counts forward, so a row inserted or deleted between two page
    reads shifts every later page -- one row is skipped and another repeats,
    silently, and a report paginated that way does not sum to its own total.
    A keyset cursor resumes from a POSITION, so concurrent writes can only add
    rows the reader has not reached.

    The encoded key is the WHOLE ordering tuple including the unique
    tie-breaker, which is what makes resuming exact rather than approximate.
    """
    return base64.urlsafe_b64encode(
        json.dumps(list(key), default=str).encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> list[Any]:
    try:
        return list(json.loads(base64.urlsafe_b64decode(cursor.encode("ascii"))))
    except Exception as exc:                                   # noqa: BLE001
        raise FilterError(
            "INVALID_CURSOR",
            "The pagination cursor could not be read. Start from the first "
            "page; a cursor is only valid for the query that produced it.",
            status=400) from exc


# ===========================================================================
# Freshness -- rendered, never assumed
# ===========================================================================

#: A figure computed from rows this application wrote itself. There is no
#: last-sync time for it because nothing syncs it -- and reporting `None` with
#: this label is the honest answer, not a missing value to be filled in.
SOURCE_LOCAL = "capex-control-hub"


def freshness(session: Session, filters: "FilterSet") -> dict[str, Any]:
    """Last-sync time and source label for the data behind a report.

    "Every screen showing synced data shows its last-sync time and source
    label. A number with no provenance is not shown."

    THREE ANSWERS, NOT TWO. A report can be built entirely from locally raised
    documents (`source_label` is this application, `last_sync_at` is None and
    that is COMPLETE, not missing); partly from mirrored ones (a real
    watermark); or from mirrored ones whose connection has never polled
    (`last_sync_at` None with `mirrored` true -- which is STALE and must not
    render as fresh). `state` distinguishes them so a screen cannot show the
    third as the first.
    """
    rows = repo.query(
        session,
        """
        SELECT MAX(wm.last_polled_at) AS last_polled_at,
               MAX(wm.hwm) AS hwm,
               COUNT(*)::bigint AS watermark_count,
               COUNT(wm.last_polled_at)::bigint AS polled_count
        FROM integration_watermark wm
        JOIN integration_connection c ON c.connection_id = wm.connection_id
        WHERE (%(entity_ids)s::text[] IS NULL
               OR c.entity_id = ANY(%(entity_ids)s))
          AND {scope}
        """,
        {"entity_ids": list(filters.entity_ids)
                       if filters.entity_ids is not None else None},
        columns=CONNECTION_SCOPE_COLUMNS,
    )
    last_polled, hwm, watermarks, polled = (rows[0] if rows else (None, None, 0, 0))
    watermarks, polled = int(watermarks or 0), int(polled or 0)
    if watermarks == 0:
        return {"state": "local", "source_label": SOURCE_LOCAL,
                "last_sync_at": None, "high_water_mark": None,
                "detail": "Every figure on this report is ours: it is computed "
                          "from documents raised in this application, and no "
                          "connector supplies any part of it."}
    if polled == 0:
        return {"state": "never_synced", "source_label": SOURCE_LOCAL,
                "last_sync_at": None, "high_water_mark": None,
                "detail": "A connector is configured for this data but has "
                          "never completed a poll, so any mirrored document is "
                          "absent rather than out of date. This is not the "
                          "same as up to date."}
    return {"state": "synced", "source_label": SOURCE_LOCAL,
            "last_sync_at": last_polled.isoformat() if last_polled else None,
            "high_water_mark": hwm.isoformat() if hwm else None,
            "detail": None}


# ===========================================================================
# The aggregate
# ===========================================================================

def _fact_sql(filters: "FilterSet") -> str:
    """The UNION of the branches this `FilterSet`'s document types need.

    `budget` is unconditional -- it comes from `budget_line`, which is not a
    document type, and a utilisation percentage with no denominator is not a
    utilisation percentage. Selecting `document_types=["BILL"]` therefore still
    shows what the actuals are being spent AGAINST.

    Branches are emitted in a FIXED order (`DOCUMENT_TYPES`) rather than in the
    caller's, so two `FilterSet`s naming the same types in different orders
    produce byte-identical SQL and therefore identical plans.
    """
    selected = (DOCUMENT_TYPES if filters.document_types is None
                else tuple(d for d in DOCUMENT_TYPES
                           if d in filters.document_types))
    branches = [_BUDGET_BRANCH] + [_BRANCH_BY_DOCUMENT_TYPE[d] for d in selected]
    return "\n        UNION ALL\n".join(branches)


def _params(filters: "FilterSet", *, as_of: date | None = None) -> dict[str, Any]:
    """Every named parameter both the branches and the outer query read.

    ONE dict for all five branches. They share `date_from`, `date_to`,
    `period_ids`, `effective` and `lifecycle`, and building a dict per branch
    is how two branches come to filter on different dates for one request.

    `lifecycle_statuses` are C3 CODES on the wire and stored LABELS in the
    database -- migration 013's header records that choice -- so they are
    translated here, from `C3_LABEL_BY_CODE`, which is read from the frozen
    contract rather than hand-copied.
    """
    def array(values: tuple[str, ...] | None) -> list[str] | None:
        # `None` -> SQL NULL -> the `IS NULL OR ...` guard passes, unfiltered.
        # `()` -> an empty array -> `= ANY('{}')` is false for every row, which
        # is "restricted to nothing" and is NOT the same answer. The FilterSet
        # docstring's distinction has to survive all the way to the wire.
        return None if values is None else list(values)

    lifecycle = (None if filters.lifecycle_statuses is None
                 else [C3_LABEL_BY_CODE[code] for code in filters.lifecycle_statuses])
    return {
        "as_of": (as_of or date.today()),
        "date_from": filters.date_from,
        "date_to": filters.date_to,
        "period_ids": array(filters.period_ids),
        "effective": list(ACCOUNTING_EFFECTIVE_BILL_STATES),
        "releasing": list(COMMITMENT_RELEASING_STATES),
        "lifecycle": lifecycle,
        "entity_ids": array(filters.entity_ids),
        "plant_ids": array(filters.plant_ids),
        "location_ids": array(filters.location_ids),
        "project_ids": array(filters.project_ids),
        "budget_head_ids": array(filters.budget_head_ids),
        "category_ids": array(filters.category_ids),
        "budget_category_ids": array(filters.budget_category_ids),
        "vendor_ids": array(filters.vendor_ids),
        "wbs_paths": array(filters.wbs_paths),
        "approval_statuses": array(filters.approval_statuses),
    }


#: dimension -> (label table, join predicate). Joined LEFT, always: a project
#: with no plant must still appear in a plant-grouped report, under a NULL key,
#: rather than vanishing from a total it contributes to. An INNER join here
#: would make the grouped total smaller than the ungrouped one -- the exact
#: drill-down mismatch a test in this wave asserts against.
_LABEL_JOINS: dict[str, str] = {
    "entity": "LEFT JOIN entity e ON e.entity_id = g.entity_id",
    "plant": "LEFT JOIN plant pl ON pl.plant_id = g.plant_id",
    "location": "LEFT JOIN location lo ON lo.location_id = g.location_id",
    "project": "LEFT JOIN project pr ON pr.project_id = g.project_id",
    "wbs": "LEFT JOIN wbs_element w ON w.wbs_id = g.wbs_id",
    "budget_head": "LEFT JOIN budget_head bh ON bh.budget_head_id = g.budget_head_id",
    "category": "LEFT JOIN budget_head bh ON bh.budget_head_id = g.budget_head_id",
    #: FABLE 5.1: `budget_category`'s OWN join, against its OWN master table --
    #: `bcat`, never `bh`. A NULL key (a pre-026 cell) renders no row here, so
    #: `_shape` substitutes "Unclassified" rather than showing a blank label.
    "budget_category": "LEFT JOIN budget_category bcat "
                       "ON bcat.category_id = g.budget_category_id",
    "vendor": "LEFT JOIN vendor_master vm ON vm.vendor_id = g.vendor_id",
}

#: dimension -> the fact column carrying its key, INSIDE the grouped CTE.
_GROUP_KEY_COLUMN: dict[str, str] = {
    "entity": "entity_id", "plant": "plant_id", "location": "location_id",
    "project": "project_id", "wbs": "wbs_id",
    "budget_head": "budget_head_id", "category": "budget_head_id",
    "budget_category": "budget_category_id", "vendor": "vendor_id",
}


def aggregate(session: Session, filters: "FilterSet", *,
              as_of: date | None = None) -> dict[str, Any]:
    """Grouped totals over the control-cell fact. The figure a card shows.

    Returns ``{"state", "rows", "totals", "group_by", "next_cursor", ...}``.

    THE GRAND TOTAL IS COMPUTED FROM THE SAME FACT AS THE ROWS, in the same
    statement, and NOT by summing the page. Summing the page would make the
    total agree with whatever happened to be on screen -- so page two of a
    paginated report would show a different "total" from page one, and neither
    would be the answer. `totals` here is over the whole filtered population;
    `rows` is one page of it. A test asserts the grouped rows sum back to it.

    An empty `rows` under a denied scope is `state="denied"`, under a real
    scope `state="empty"`. Those are two different sentences on screen and this
    layer never collapses them.
    """
    filters.validate()
    from . import principal_scope             # local: principal_scope imports engine

    if principal_scope.is_denied(session.scope):
        # Short-circuited BEFORE the query, not after. The query would return
        # zero rows anyway -- `compile_scope` renders four empty frozensets as
        # FALSE -- so this is not the enforcement, it is the LABEL. Running it
        # and inferring "denied" from emptiness is exactly the collapse the
        # contract forbids: it cannot tell a denied scope from a real one over
        # a quiet month.
        return _empty_result(filters, state="denied",
                             detail="You have no grant that reaches any of "
                                    "this data. This is not an empty report; "
                                    "it is a report you cannot see.")

    group_by = filters.group_by
    key_columns = [_GROUP_KEY_COLUMN[d] for d in group_by]
    # `dict.fromkeys` keeps first-seen order and de-duplicates: `category` and
    # `budget_head` resolve to one column, and `validate` has already refused
    # naming both, but a duplicate here would produce invalid SQL rather than a
    # clear error.
    grouped = list(dict.fromkeys(key_columns))
    select_keys = ", ".join(f"f.{c}" for c in grouped)
    group_clause = ", ".join(f"f.{c}" for c in grouped) if grouped else ""

    sums = ",\n               ".join(
        f"COALESCE(SUM(f.{c}_paise), 0)::bigint AS {c}_paise"
        for c in COMPONENTS)

    params = _params(filters, as_of=as_of)
    terms = _order_expressions(filters, grouped)
    cursor_predicate = _cursor_predicate(filters, terms, params)
    order_terms = ", ".join(f"{expression} {direction}"
                            for expression, direction in terms)

    statement = f"""
        WITH fact AS (
        {_fact_sql(filters)}
        ),
        scoped AS (
            SELECT * FROM fact f
            WHERE {_OUTER_PREDICATES.strip()}
        ),
        grouped AS (
            SELECT {select_keys + ',' if select_keys else ''}
               {sums}
            FROM scoped f
            {('GROUP BY ' + group_clause) if group_clause else ''}
        ),
        overall AS (
            SELECT {sums}
            FROM scoped f
        )
        SELECT {(', '.join(f'g.{c}' for c in grouped) + ',') if grouped else ''}
               {', '.join(f'g.{c}_paise' for c in COMPONENTS)},
               {_label_select(group_by)}
               (SELECT to_jsonb(overall) FROM overall) AS overall_totals
        FROM grouped g
        {_label_join_clause(group_by)}
        WHERE {cursor_predicate}
        ORDER BY {order_terms}
        LIMIT %(limit)s
    """

    params["limit"] = filters.limit + 1          # one extra: is there a next page?

    rows = repo.query(session, statement, params, columns=SCOPE_COLUMNS)
    return _shape(rows, filters, grouped, group_by, terms)


# ------------------------------------------------------------ ordering
def _order_expressions(filters: "FilterSet",
                       grouped: Sequence[str]) -> list[tuple[str, str]]:
    """The ORDER BY terms, as ``(sql_expression, direction)``, tie-broken.

    THE TIE-BREAKER IS NOT OPTIONAL AND IS NOT DECORATION. A sort on
    `commitment_paise` alone has ties -- two budget heads with identical
    exposure is ordinary, not exotic -- and PostgreSQL is free to return tied
    rows in a different order on each execution. A keyset cursor over a
    non-deterministic order skips rows on one page and repeats them on the
    next, so the pages do not sum to the total the same query reports. Every
    ordering here therefore ends with the FULL group key, which is unique per
    row by construction: it is what the rows were grouped by.

    KEYS ARE COALESCED TO `''`. A group key is NULL wherever the dimension does
    not apply -- a project with no plant -- and NULL breaks both the ordering
    (NULLS FIRST/LAST differs by direction) and the cursor comparison (any
    comparison with NULL is NULL, so the row is dropped, silently, mid-page).
    `''` is safe as the sentinel because no id in this schema can be blank:
    every one of these columns is a `text` primary key, and
    `engine.validate_scope_value` refuses a blank id outright as
    "indistinguishable from no id at all".
    """
    direction = "DESC" if filters.sort_desc else "ASC"
    terms: list[tuple[str, str]] = []
    if filters.sort and filters.sort != "key":
        terms.append((_sort_expression(filters.sort), direction))
    terms.extend((f"COALESCE(g.{column}, '')", direction) for column in grouped)
    if not terms:
        # No grouping at all: one row, the grand total. Ordering is trivially
        # deterministic and there is nothing to paginate.
        terms.append(("1", "ASC"))
    return terms


def _sort_expression(measure: str) -> str:
    """The SQL for a sortable measure, derived and never stored.

    `exposure`, `available` and `utilisation_pct` are DERIVED (`domain._derive`),
    so sorting on them means sorting on the expression, not on a column. A
    stored copy could disagree with the computed one, which is the whole reason
    `derive` recomputes them for every row rather than reading them back.
    """
    if measure == "exposure":
        return EXPOSURE_SQL
    if measure == "available":
        return f"(g.budget_paise - {EXPOSURE_SQL})"
    if measure == "utilisation_pct":
        return UTILISATION_PCT_SQL
    return f"g.{measure}_paise"


#: `utilisation_pct` in SQL, and it must equal
#: :func:`_utilisation_ordering_value` EXACTLY -- not merely order the same way.
#:
#: THIS EXPRESSION IS HALF OF A KEYSET CURSOR, WHICH IS WHY "IT SORTS THE SAME"
#: IS NOT ENOUGH. It used to be the bare ratio
#: ``exposure::numeric / NULLIF(budget, 0)`` with the comment "the ratio is
#: only an ORDERING here". That was wrong twice over, and both defects were
#: reproduced:
#:
#:   * THE SCALE. The cursor written to the wire is a PERCENTAGE -- `derive`
#:     returns ``round(ratio * 100, 1)`` -- so the resume compared ``20.0``
#:     against column values near ``0.2``. Ordering survived (x100 is
#:     monotonic) and pagination did not: over six rows at ``limit=2``, ASC
#:     returned page 1 and then nothing (4 of 6 rows unreachable), and DESC
#:     re-returned page 1 forever.
#:
#:   * THE `NULLIF` ALONE. A zero-budget group divided to SQL NULL, every
#:     comparison against NULL is NULL rather than TRUE, and the row vanished
#:     from page 2 onward -- while `derive` reports ``0.0`` for it and page 1
#:     shows it. Zero-budget cells are exactly the over-commitment cases
#:     SCR-25 exists to surface, so the rows silently dropped were the ones
#:     most worth seeing. `COALESCE(..., 0)` restores them, and 0 is the same
#:     value `derive` already reports.
#:
#: THE ROUNDING IS LOAD-BEARING, not cosmetic: without it a ``76.7`` cursor
#: never equals -- and, descending, sorts below -- a ``76.6666...`` column
#: value, so the tie-continuation clause of the keyset predicate can never
#: fire and every row sharing that rounded percentage is skipped.
#: `exposure` in SQL: the five stored limbs `derive` sums (034 added the two
#: internal ones). One spelling, used by the sort expression and the cursor.
EXPOSURE_SQL = (
    "(g.commitment_paise + g.actual_paise + g.pr_reserved_paise"
    " + g.internal_allocation_paise + g.internal_consumption_paise)")

UTILISATION_PCT_SQL = (
    f"COALESCE(round({EXPOSURE_SQL}"
    "::numeric * 100 / NULLIF(g.budget_paise, 0), 1), 0)")


def _utilisation_ordering_value(components: Mapping[str, int]) -> float:
    """`utilisation_pct` for the CURSOR, computed as PostgreSQL computes it.

    NOT `derive()["utilisation_pct"]`, and the difference is deliberate.
    `derive` transcribes `domain._derive` verbatim -- ``round(exposure / budget
    * 100.0, 1)`` -- which rounds a binary float to nearest, so an exact
    two-decimal midpoint goes DOWN whenever the double falls just below it.
    PostgreSQL's `round(numeric, 1)` is exact decimal arithmetic and rounds a
    midpoint UP. The two disagree by 0.1 on exactly half of all midpoints: a
    ``budget`` of 20,00,000 paise against an exposure of 15,33,000 is 76.65%,
    which `derive` reports as 76.6 and PostgreSQL as 76.7.

    A cursor value that is 0.1 away from the column it resumes against is the
    SAME defect this expression was just fixed for -- descending, every row at
    76.7 is skipped; ascending, the cursor row repeats forever -- so the
    ordering key is computed here in exact decimal, half-up, matching
    :data:`UTILISATION_PCT_SQL` digit for digit.

    THIS DOES NOT CHANGE ANY REPORTED NUMBER. The `utilisation_pct` in a row,
    in `totals`, and on every screen is still `derive`'s, still
    `domain._derive`'s verbatim. This value is never displayed: it exists only
    inside the base64 cursor, where its one job is to equal the SQL.

    Returned as `float` because the cursor is JSON, and because the parameter
    is compared against the expression above exactly as `derive`'s float
    already was. A one-decimal `Decimal` and its nearest `float` convert to
    each other without loss, so the round trip through JSON is exact.
    """
    budget = int(components.get("budget", 0) or 0)
    if not budget:
        # `COALESCE(..., 0)` in the SQL; `derive` reports 0.0 for the same
        # case. All three agree, which is the whole point.
        return 0.0
    exposure = sum(int(components.get(name, 0) or 0)
                   for name in ("commitment", "actual", "pr_reserved"))
    return float((Decimal(exposure) * 100 / Decimal(budget)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP))


def _cursor_predicate(filters: "FilterSet", terms: Sequence[tuple[str, str]],
                      params: dict[str, Any]) -> str:
    """The keyset resume condition, expanded lexicographically.

    NOT a row-value comparison (`ROW(a,b) > ROW(x,y)`). That form is exact only
    when every column shares one direction, and a report sorted by descending
    commitment with an ascending key tie-breaker mixes them -- so the row
    comparison would quietly resume from the wrong place. The OR-expansion
    below is direction-per-column and therefore correct for any mix:

        (a > x) OR (a = x AND b > y) OR (a = x AND b = y AND c > z)
    """
    if not filters.cursor:
        return "TRUE"
    key = decode_cursor(filters.cursor)
    if len(key) != len(terms):
        raise FilterError(
            "INVALID_CURSOR",
            "The pagination cursor does not match this query's sort. A cursor "
            "is only valid for the filter set and ordering that produced it; "
            "start from the first page.",
            status=400)
    clauses: list[str] = []
    for index, (expression, direction) in enumerate(terms):
        operator = ">" if direction == "ASC" else "<"
        equalities = " AND ".join(
            f"{terms[j][0]} IS NOT DISTINCT FROM %(cursor_{j})s"
            for j in range(index))
        comparison = f"{expression} {operator} %(cursor_{index})s"
        clauses.append(f"({equalities} AND {comparison})" if equalities
                       else f"({comparison})")
    for index, value in enumerate(key):
        params[f"cursor_{index}"] = value
    return "(" + " OR ".join(clauses) + ")"


def _label_select(group_by: Sequence[str]) -> str:
    """One label column per grouped dimension, or nothing.

    Always ends in a trailing comma when non-empty, because the caller splices
    it before `overall_totals`.
    """
    parts = [f"{DIMENSIONS[d].label_sql} AS {d}_label"
             for d in group_by if DIMENSIONS[d].label_sql]
    return (", ".join(parts) + ",") if parts else ""


def _label_join_clause(group_by: Sequence[str]) -> str:
    """The LEFT JOINs for the grouped dimensions' labels, de-duplicated.

    `category` and `budget_head` share the `bh` alias, so emitting both would
    be a duplicate-alias error. `validate` already refuses grouping by both;
    `dict.fromkeys` here means a future caller cannot turn that into a SQL
    syntax error instead of a clear refusal.
    """
    return "\n        ".join(
        dict.fromkeys(_LABEL_JOINS[d] for d in group_by if d in _LABEL_JOINS))


# ------------------------------------------------------------ result shaping
def _empty_result(filters: "FilterSet", *, state: str,
                  detail: str | None = None,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A result with no rows, LABELLED with which of the four states it is.

    `zero data`, `denied scope`, `stale data` and `service failure` produce
    four different sentences on screen, and the only way that can be true is if
    they are four different values here. `state` is never inferred downstream
    from `rows == []`.
    """
    out: dict[str, Any] = {
        "state": state,
        "detail": detail,
        "rows": [],
        "totals": derive({}),
        "group_by": list(filters.group_by),
        "next_cursor": None,
        "has_more": False,
    }
    if extra:
        out.update(extra)
    return out


def _shape(rows: Sequence[tuple], filters: "FilterSet",
           grouped: Sequence[str], group_by: Sequence[str],
           terms: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Turn the driver's tuples into rows, totals, and the next cursor.

    THE GRAND TOTAL COMES FROM `overall_totals`, WHICH IS COMPUTED OVER THE
    WHOLE FILTERED POPULATION IN THE SAME STATEMENT -- never by summing the
    page. Summing the page would make "total" mean "total of what you can
    currently see", so page two of a report would report a different total from
    page one and neither would be the answer to the question asked.

    An empty result here is `state="empty"` and NOT `state="denied"`: the
    denied case was short-circuited before the query ran, with its own label.
    """
    if not rows:
        return _empty_result(
            filters, state="empty",
            detail="No data matches these filters. This is an empty result "
                   "from a query you are entitled to run -- not a permission "
                   "problem, and not a failure.")

    has_more = len(rows) > filters.limit
    page = list(rows[:filters.limit])

    key_count = len(grouped)
    component_count = len(COMPONENTS)
    label_names = [d for d in group_by if DIMENSIONS[d].label_sql]

    out_rows: list[dict[str, Any]] = []
    for row in page:
        keys = row[:key_count]
        components = row[key_count:key_count + component_count]
        labels = row[key_count + component_count:
                     key_count + component_count + len(label_names)]
        measures = derive(dict(zip(COMPONENTS, components)))
        key_by_dimension = {
            dimension: keys[grouped.index(_GROUP_KEY_COLUMN[dimension])]
            for dimension in group_by
        }
        # FABLE 5.1: a NULL `budget_category` key is a cell that migration 026
        # never classified (or that pre-dates it). "Unclassified" is rendered
        # rather than a blank string -- REQ-RPT-009/010's own rule that NULL
        # renders as a real label, not an absence a reader has to interpret.
        label_values = list(labels)
        for index, name in enumerate(label_names):
            if name == "budget_category" and label_values[index] is None:
                label_values[index] = "Unclassified"
        out_rows.append({
            "key": key_by_dimension,
            "labels": dict(zip(label_names, label_values)),
            **measures,
        })

    # `overall_totals` is the same jsonb on every row -- a scalar subquery over
    # the whole scoped fact, repeated by the join. Read from the FIRST row, and
    # deliberately not from the last: the last row of `rows` may be the extra
    # one fetched only to answer "is there more", and reading a total from a
    # row that is not shown is how a total comes to describe a different page
    # from the one under it. (It carries the same value either way; taking it
    # from a row that is always displayed means that stays true if the
    # look-ahead ever changes.)
    overall = rows[0][-1]
    totals = derive({
        component: (overall or {}).get(f"{component}_paise", 0)
        for component in COMPONENTS
    })

    next_cursor = None
    if has_more and out_rows:
        # The cursor is the LAST RETURNED ROW's full ordering key, recomputed in
        # Python from the same expressions the SQL ordered by -- so the resume
        # point is the row the reader actually saw, not the extra row fetched
        # only to answer "is there more".
        next_cursor = encode_cursor(
            _ordering_key_of(page[filters.limit - 1], filters, grouped, terms))

    return {
        "state": "ok",
        "detail": None,
        "rows": out_rows,
        "totals": totals,
        "group_by": list(group_by),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _ordering_key_of(row: Sequence[Any], filters: "FilterSet",
                     grouped: Sequence[str],
                     terms: Sequence[tuple[str, str]]) -> list[Any]:
    """The ordering key of one returned row, in the SAME order as `terms`.

    Recomputed from the row's own values rather than selected as extra columns,
    because the derived measures (`exposure`, `available`, `utilisation_pct`)
    are expressions and not columns -- and a second SQL copy of an expression
    is a second definition that can drift from the first.

    `exposure` and `available` are integer paise on both sides and `derive`'s
    value is the SQL's, exactly. `utilisation_pct` is NOT: it is rounded, and
    Python rounds a float where PostgreSQL rounds a decimal. See
    :func:`_utilisation_ordering_value`, which is what the cursor carries --
    the displayed percentage is still `derive`'s and is untouched.
    """
    key_count = len(grouped)
    keys = list(row[:key_count])
    components = dict(zip(COMPONENTS, row[key_count:key_count + len(COMPONENTS)]))
    measures = derive(components)

    key: list[Any] = []
    if filters.sort and filters.sort != "key":
        if filters.sort == "utilisation_pct":
            key.append(_utilisation_ordering_value(components))
        elif filters.sort in ("exposure", "available"):
            key.append(measures[filters.sort])
        else:
            key.append(int(components.get(filters.sort, 0) or 0))
    # `COALESCE(..., '')` in the SQL; the same coalesce here, or the resume
    # comparison would be made against a NULL the query never ordered on.
    key.extend("" if value is None else value for value in keys)
    if not key:
        key.append(1)
    assert len(key) == len(terms), (
        "the cursor key must have one element per ORDER BY term, or a resume "
        "compares the wrong columns")
    return key


# ===========================================================================
# Drill-down
# ===========================================================================

#: The grain a drill-down lands on when the caller names no finer one: the
#: control cell itself, which is what every metric here is keyed on.
DEFAULT_DRILL_GRAIN: tuple[str, ...] = ("wbs", "budget_head")


def drill_down(session: Session, filters: "FilterSet", dimension: str,
               key: str | None, *, grain: Sequence[str] | None = None,
               as_of: date | None = None) -> dict[str, Any]:
    """The rows behind one grouped figure. `FilterSet` + the clicked dimension.

    THE ROUND TRIP IS THE CONTRACT: "a drill-down that does not sum back to the
    figure clicked is a defect, and a test asserts the round trip."
    `tests/test_pg_reporting.py` asserts exactly that, and this function is
    built so it holds BY CONSTRUCTION rather than by two implementations
    agreeing:

      * The clicked dimension is applied through
        :meth:`FilterSet.narrowed_to`, which INTERSECTS with what was already
        filtered. It cannot widen the population the clicked total was computed
        over -- so the drill-down can never contain a row the total did not
        count.
      * The result comes from :func:`aggregate` over the SAME fact SQL, with
        only `group_by` changed. There is no second query, no second set of
        formulas and no second definition of a bucket to drift from the first.

    Given those two, "the parts sum to the whole" is arithmetic over one
    aggregation, not an agreement between two of them.
    """
    narrowed = filters.narrowed_to(dimension, key)
    finer = tuple(grain) if grain is not None else DEFAULT_DRILL_GRAIN
    # The clicked dimension stays in `group_by` so the drill-down still says
    # WHICH group it is under; the finer dimensions are appended. Duplicates are
    # dropped, keeping first-seen order, so drilling into `wbs` and landing on a
    # `wbs`-grained view does not group by it twice (which `validate` refuses).
    drill_group = tuple(dict.fromkeys((dimension, *finer)))
    # `category` and `budget_head` are one column; if the click was on
    # `category`, appending `budget_head` would be the aliased-dimension
    # refusal. Drop the alias rather than raise -- the caller clicked a
    # legitimate segment and asked a legitimate question.
    for alias_group in _ALIASED_DIMENSIONS:
        named = [d for d in drill_group if d in alias_group]
        if len(named) > 1:
            drill_group = tuple(d for d in drill_group
                                if d not in named[1:])
    result = aggregate(session, replace(narrowed, group_by=drill_group),
                       as_of=as_of)
    result["drill_of"] = {"dimension": dimension, "key": key}
    return result


# ===========================================================================
# Saved views (migration 017)
# ===========================================================================

#: The screens a view may be saved against. An allow-list in the application,
#: not a foreign key: there is no table of screens in this schema, and
#: inventing one in a migration would make it the registry of the UI. 017's
#: `ck_report_saved_view_key_shape` stops the column becoming free text; this
#: stops it becoming a typo.
REPORT_KEYS: frozenset[str] = frozenset({
    "executive_dashboard",        # SCR-01
    "controller_workbench",       # SCR-02
    "project_list",               # SCR-04
    "cwip_ledger",                # SCR-19
    "open_commitment_ageing",     # SCR-23
    "cwip_ageing",                # SCR-24
    "exception_monitor",          # SCR-25
})

VISIBILITIES: tuple[str, ...] = ("PRIVATE", "SHARED")


def list_views(session: Session, *, report_key: str | None = None,
               user_id: str | None = None) -> list[dict[str, Any]]:
    """Every saved view this principal may open, own and shared alike.

    RLS DOES THE PRIVACY, and the predicate here does not repeat it. 017's
    policy admits a row only when the caller's scope reaches its entity AND
    (the view is SHARED or the caller owns it), so a private view belonging to
    a colleague is simply not there. Re-stating that condition in this
    statement would create a second definition of "may open", and the two would
    eventually disagree -- with the SQL one, which nobody reviews as a security
    control, winning.
    """
    rows = repo.query(
        session,
        """
        SELECT v.view_id, v.entity_id, v.report_key, v.name, v.description,
               v.owner_user_id, v.visibility, v.definition, v.updated_at,
               v.version_no,
               (d.view_id IS NOT NULL) AS is_default
        FROM report_saved_view v
        LEFT JOIN report_view_default d
               ON d.view_id = v.view_id
              AND d.user_id = %(user_id)s
        WHERE (%(report_key)s::text IS NULL OR v.report_key = %(report_key)s)
          AND {scope}
        ORDER BY v.report_key, lower(v.name), v.view_id
        """,
        {"report_key": report_key, "user_id": user_id or session.scope.user_id},
        columns=VIEW_SCOPE_COLUMNS,
    )
    return [_view_row(row) for row in rows]


def _view_row(row: Sequence[Any]) -> dict[str, Any]:
    (view_id, entity_id, report_key, name, description, owner, visibility,
     definition, updated_at, version_no, is_default) = row
    return {
        "view_id": view_id, "entity_id": entity_id, "report_key": report_key,
        "name": name, "description": description, "owner_user_id": owner,
        "visibility": visibility, "definition": definition,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "version_no": int(version_no), "is_default": bool(is_default),
    }


def load_view(session: Session, view_id: str, *,
              user_id: str | None = None) -> tuple[dict[str, Any], "FilterSet"]:
    """One saved view and the `FilterSet` it stores.

    A view the caller may not open comes back as ``ViewNotFound``, which the
    router renders as 404 -- the SAME answer a view id that never existed gets.
    Never 403: a 403 on an id tells an unauthorised caller the id is real,
    which is an existence oracle over another entity's estate.

    The stored definition is rebuilt through :meth:`FilterSet.from_json`, which
    REFUSES an unknown key rather than dropping it. Executing the returned
    `FilterSet` re-applies the OPENER'S scope, so a view saved by someone with
    wider grants yields the opener's own, narrower rows -- honestly narrower,
    never an error and never the author's numbers.
    """
    rows = repo.query(
        session,
        """
        SELECT v.view_id, v.entity_id, v.report_key, v.name, v.description,
               v.owner_user_id, v.visibility, v.definition, v.updated_at,
               v.version_no,
               (d.view_id IS NOT NULL) AS is_default
        FROM report_saved_view v
        LEFT JOIN report_view_default d
               ON d.view_id = v.view_id AND d.user_id = %(user_id)s
        WHERE v.view_id = %(view_id)s
          AND {scope}
        """,
        {"view_id": view_id, "user_id": user_id or session.scope.user_id},
        columns=VIEW_SCOPE_COLUMNS,
    )
    if not rows:
        raise ViewNotFound(view_id)
    view = _view_row(rows[0])
    return view, FilterSet.from_json(view["definition"])


class ViewNotFound(FilterError):
    """A saved view that does not exist, or that this caller may not open.

    ONE EXCEPTION FOR BOTH, DELIBERATELY. The two must be indistinguishable:
    an error that separates them confirms an id is real to a caller who was
    refused it.
    """

    def __init__(self, view_id: str) -> None:
        super().__init__(
            "VIEW_NOT_FOUND",
            f"No saved view {view_id} is available to you.",
            status=404)


def default_view_id(session: Session, report_key: str, *,
                    user_id: str | None = None) -> str | None:
    """This user's default view for one report, or None.

    None means "no default set" and nothing else. A default pointing at a view
    the caller may no longer open is filtered out by 017's policy -- its EXISTS
    runs against `report_saved_view`, which is itself under RLS -- so it also
    reads as None rather than as a dangling id.
    """
    rows = repo.query(
        session,
        """
        SELECT d.view_id
        FROM report_view_default d
        JOIN report_saved_view v ON v.view_id = d.view_id
        WHERE d.user_id = %(user_id)s
          AND d.report_key = %(report_key)s
          AND {scope}
        LIMIT 1
        """,
        {"user_id": user_id or session.scope.user_id, "report_key": report_key},
        columns=VIEW_SCOPE_COLUMNS,
    )
    return rows[0][0] if rows else None


def save_view(session: Session, *, view_id: str, entity_id: str,
              report_key: str, name: str, filters: "FilterSet", actor: str,
              description: str | None = None,
              visibility: str = "PRIVATE") -> dict[str, Any]:
    """Create a saved view owned by `actor`.

    `entity_id` IS CHECKED AGAINST THE CALLER'S SCOPE BY THE WRITE ITSELF, not
    by a lookup first. The INSERT's source row is a scoped SELECT from
    `entity`, so an entity the caller has no grant for produces NO ROW TO
    INSERT and the statement writes nothing -- which surfaces as
    `EntityNotInScope`, the same answer an entity id that never existed gets.
    Checking with a separate SELECT would leave a window between the check and
    the write, and would make the check the control rather than the write.
    017's `WITH CHECK` is the third layer and refuses an `owner_user_id` that
    is not the session's principal, so a caller cannot author a view in someone
    else's name even by reaching past this function.
    """
    if report_key not in REPORT_KEYS:
        raise FilterError(
            "UNKNOWN_REPORT_KEY",
            f"{report_key} is not a report this build serves. Known reports: "
            f"{', '.join(sorted(REPORT_KEYS))}.")
    if visibility not in VISIBILITIES:
        raise FilterError(
            "UNKNOWN_VISIBILITY",
            f"visibility must be one of {', '.join(VISIBILITIES)}; got "
            f"{visibility!r}.")
    if not name or not name.strip():
        raise FilterError(
            "VIEW_NAME_REQUIRED",
            "A saved view needs a name: a blank one cannot be picked out of a "
            "list.")
    filters.validate()

    rows = repo.query(
        session,
        """
        INSERT INTO report_saved_view (
            view_id, entity_id, report_key, name, description,
            owner_user_id, visibility, definition,
            created_by, updated_by)
        SELECT %(view_id)s, v.entity_id, %(report_key)s, %(name)s,
               %(description)s, %(actor)s, %(visibility)s,
               %(definition)s::jsonb, %(actor)s, %(actor)s
        FROM entity v
        WHERE v.entity_id = %(entity_id)s
          AND {scope}
        RETURNING view_id, entity_id, report_key, name, description,
                  owner_user_id, visibility, definition, updated_at,
                  version_no, false AS is_default
        """,
        {"view_id": view_id, "entity_id": entity_id, "report_key": report_key,
         "name": name.strip(), "description": description, "actor": actor,
         "visibility": visibility,
         "definition": json.dumps(filters.to_json())},
        columns=VIEW_SCOPE_COLUMNS,
    )
    if not rows:
        raise EntityNotInScope(entity_id)
    return _view_row(rows[0])


class EntityNotInScope(FilterError):
    """The entity a view was to be saved against is not one this caller holds.

    404 and not 403, for the reason `load_view` gives: a 403 on an id confirms
    the id is real to a caller who was refused it.
    """

    def __init__(self, entity_id: str) -> None:
        super().__init__(
            "ENTITY_NOT_FOUND",
            f"No entity {entity_id} is available to you to save a view "
            f"against.",
            status=404)


def update_view(session: Session, view_id: str, *, actor: str,
                name: str | None = None, description: str | None = None,
                visibility: str | None = None,
                filters: "FilterSet | None" = None) -> dict[str, Any]:
    """Edit a saved view. Only its OWNER can, and 017's `WITH CHECK` is why.

    The UPDATE names `owner_user_id = %(actor)s` in its WHERE as well. That is
    not redundancy for its own sake: the policy's `WITH CHECK` refuses the
    resulting ROW, which would raise a policy violation, and this predicate
    makes the same case return NO ROWS instead -- rendered as 404, the same
    answer a view that does not exist gets. A caller who is refused must not
    learn from the shape of the refusal that the view is real.
    """
    if visibility is not None and visibility not in VISIBILITIES:
        raise FilterError(
            "UNKNOWN_VISIBILITY",
            f"visibility must be one of {', '.join(VISIBILITIES)}; got "
            f"{visibility!r}.")
    if name is not None and not name.strip():
        raise FilterError(
            "VIEW_NAME_REQUIRED",
            "A saved view needs a name: a blank one cannot be picked out of a "
            "list.")
    if filters is not None:
        filters.validate()

    rows = repo.query(
        session,
        """
        UPDATE report_saved_view v
           SET name        = COALESCE(%(name)s, v.name),
               description = COALESCE(%(description)s, v.description),
               visibility  = COALESCE(%(visibility)s, v.visibility),
               definition  = COALESCE(%(definition)s::jsonb, v.definition),
               updated_at  = now(),
               updated_by  = %(actor)s,
               version_no  = v.version_no + 1
         WHERE v.view_id = %(view_id)s
           AND v.owner_user_id = %(actor)s
           AND {scope}
        RETURNING v.view_id, v.entity_id, v.report_key, v.name, v.description,
                  v.owner_user_id, v.visibility, v.definition, v.updated_at,
                  v.version_no, false AS is_default
        """,
        {"view_id": view_id, "actor": actor,
         "name": name.strip() if name else None,
         "description": description, "visibility": visibility,
         "definition": (json.dumps(filters.to_json())
                        if filters is not None else None)},
        columns=VIEW_SCOPE_COLUMNS,
    )
    if not rows:
        raise ViewNotFound(view_id)
    return _view_row(rows[0])


def delete_view(session: Session, view_id: str, *, actor: str) -> str:
    """Delete a saved view. Owner only; the default pointing at it CASCADEs.

    Returns the deleted id so the router can write an audit entry naming it.
    A saved view is a bookmark, not a ledger row -- see 017's grant block for
    why DELETE is granted here and nowhere else in this schema.
    """
    rows = repo.query(
        session,
        """
        DELETE FROM report_saved_view v
         WHERE v.view_id = %(view_id)s
           AND v.owner_user_id = %(actor)s
           AND {scope}
        RETURNING v.view_id
        """,
        {"view_id": view_id, "actor": actor},
        columns=VIEW_SCOPE_COLUMNS,
    )
    if not rows:
        raise ViewNotFound(view_id)
    return rows[0][0]


def set_default_view(session: Session, view_id: str, *, actor: str) -> dict[str, Any]:
    """Make one view this user's default for its own report.

    THE REPORT KEY COMES FROM THE VIEW, never from the caller. Taking it from
    the request would let a caller register a `cwip_ledger` view as their
    default for `executive_dashboard`, and the dashboard would then open on a
    filter set built for a different screen -- with `group_by` dimensions that
    screen does not render.

    Upsert on the primary key, which IS the "one default per (user, report)"
    rule: setting a second default replaces the first rather than adding one.
    """
    rows = repo.query(
        session,
        """
        INSERT INTO report_view_default (user_id, report_key, view_id, set_by)
        SELECT %(actor)s, v.report_key, v.view_id, %(actor)s
        FROM report_saved_view v
        WHERE v.view_id = %(view_id)s
          AND {scope}
        ON CONFLICT ON CONSTRAINT pk_report_view_default
        DO UPDATE SET view_id = EXCLUDED.view_id,
                      set_at  = now(),
                      set_by  = EXCLUDED.set_by
        RETURNING user_id, report_key, view_id
        """,
        {"view_id": view_id, "actor": actor},
        columns=VIEW_SCOPE_COLUMNS,
    )
    if not rows:
        raise ViewNotFound(view_id)
    user_id, report_key, resolved = rows[0]
    return {"user_id": user_id, "report_key": report_key, "view_id": resolved}


def clear_default_view(session: Session, report_key: str, *,
                       actor: str) -> bool:
    """Drop this user's default for one report. Idempotent.

    Returns whether a row was removed. `False` is "there was no default", not
    a failure -- clearing something already clear is the caller's intent
    satisfied.
    """
    rows = repo.query(
        session,
        """
        DELETE FROM report_view_default d
         USING report_saved_view v
         WHERE d.view_id = v.view_id
           AND d.user_id = %(actor)s
           AND d.report_key = %(report_key)s
           AND {scope}
        RETURNING d.view_id
        """,
        {"actor": actor, "report_key": report_key},
        columns=VIEW_SCOPE_COLUMNS,
    )
    return bool(rows)
