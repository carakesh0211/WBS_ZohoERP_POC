"""Asynchronous, chunked exports that run under the REQUESTER's scope.

`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` section 10.3, the SVC-EXPORT
row, in full::

    "runs an export under the requesting user's scope, never its own;
     inherits the requester's Scope, persisted on the export job row"

and `docs/WAVE7_CONTRACT.md`'s export authorisation contract adds the rest:
202 with a job id, no route over the 30-second AppSail budget, immutable
capture of identity and resolved scope, deterministic column order, money
formatted exactly from paise, an audit event per export.

THE PROPERTY THIS MODULE EXISTS FOR
===================================

A background worker has no request. It cannot resolve a scope when it runs,
because the only identity available to it at that moment is its own -- and a
service account that resolved its own scope and then exported "the requested
data" is a scope-escalation path with a job table in front of it.

So the escalation is made unreachable in four independent ways, and no one of
them is trusted alone:

1. **Capture.** :func:`create_job` writes the requester's already-resolved
   `Scope` onto the job row, inside the authenticated request. Migration 017's
   `trg_export_job_identity_immutable` refuses any UPDATE that changes it.

2. **Verification.** :func:`verify_scope_digest` recomputes SHA-256 over the
   job id and the canonical scope document before a single business row is
   read. A mismatch FAILS the job with `SCOPE_DIGEST_MISMATCH` and reads
   nothing. This covers what the trigger structurally cannot: a row deleted
   and re-inserted, restored from a doctored dump, or written by a role
   holding more than `capex_app`.

   It is a DETECTION control and is documented as one. It carries no key, so
   an actor who can rewrite the row can recompute it. Which is why:

3. **Meet.** :func:`meet_scopes` intersects the stored scope with the
   requester's CURRENTLY resolved grants. A meet is a greatest-lower-bound: it
   can only ever narrow, on every dimension and on `read_all`. A `scope_json`
   widened by an attacker who also recomputed the digest still reads nothing
   the requester cannot read today. It also handles the ordinary case nobody
   attacks -- grants revoked between queueing and running.

4. **The session is opened as the requester.** :func:`advance_job` runs the
   chunk inside ``database.session(rehydrated)``, so the `SET LOCAL capex.*`
   settings RLS reads, the predicate `repo.compile_scope` builds, and
   `capex.user_id` on the `export_job` row's own owner policy are all the
   requester's. The SVC-EXPORT identity is used ONLY to find candidate job
   rows -- it holds no dimension grant, so its own scope compiles to FALSE
   against every business table, and `run_due_jobs` never issues a business
   query from it.

WHY A DENIED REQUESTER FAILS RATHER THAN EXPORTING NOTHING
==========================================================

`docs/WAVE7_CONTRACT.md`: "Three states are distinct and must never collapse:
zero data, denied scope, service failure." A job whose requester now resolves
to :func:`principal_scope.denied_scope` finishes `FAILED` with
`REQUESTER_SCOPE_DENIED`, not `SUCCEEDED` with an empty file. An empty CSV
handed to a controller who has lost their grants says "there is no budget";
the truth is "you may no longer see it".

WHY A FILTER THIS DATASET CANNOT APPLY IS A REFUSAL
===================================================

:func:`plan_job` refuses a `FilterSet` naming a field the chosen dataset has
no column for, with `UNSUPPORTED_FILTER` and the field name. Quietly dropping
a filter WIDENS the result -- the caller asked for one plant and got twelve --
and an export is the one surface where the recipient has no way to notice.

MONEY
=====

Integer paise end to end. :func:`format_paise` divides by 100 with integer
`divmod` and formats the remainder with two digits; there is no float and no
`Decimal` anywhere on the path. `SUM()` over `bigint` returns numeric in
PostgreSQL, so any aggregate here casts `::bigint` -- this module currently
aggregates only in `COUNT(*)`, and the discipline is asserted by
`tests/test_money_sql_discipline.py` regardless.

THE FilterSet SEAM
==================

`app/backend/pg/reporting.py` (stream A1) owns the canonical `FilterSet`. This
module imports it when it exists and otherwise falls back to
:class:`_ContractFilterSet`, transcribed field-for-field from
`docs/WAVE7_CONTRACT.md`. Nothing below constructs a `FilterSet`: every
consumer here goes through :func:`normalise_filters`, which reads the contract's
field names off a dataclass, a pydantic model or a plain mapping alike. When
A1's module lands, the import resolves and no other line changes.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from . import audit as audit_svc
from . import principal_scope, repo
from .engine import Database, Scope, Session

log = logging.getLogger(__name__)


# ===========================================================================
# The FilterSet seam -- see the module docstring
# ===========================================================================
#: The contract's field names, in the contract's order. This tuple is the ONLY
#: thing this module knows about a `FilterSet`, which is what keeps A1's merge
#: mechanical.
FILTER_FIELDS: tuple[str, ...] = (
    "entity_ids", "plant_ids", "location_ids", "project_ids",
    "wbs_paths",
    "budget_head_ids",
    "category_ids",
    "vendor_ids", "item_ids",
    "document_types",
    "lifecycle_statuses",
    "approval_statuses",
    "date_from", "date_to",
    "period_ids",
    "group_by",
    "cursor", "limit", "sort",
)

#: Fields an export ignores by construction rather than by omission, because an
#: export has no paging of its own: it delivers the WHOLE result set, and a
#: caller-supplied cursor/limit/sort would silently truncate or reorder the
#: file. `sort` is refused too -- column order and row order are captured at
#: creation so a resumed chunk lands where the previous one stopped.
FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT: frozenset[str] = frozenset(
    {"cursor", "limit", "sort", "group_by"})


@dataclass(frozen=True)
class _ContractFilterSet:
    """The fallback shape, transcribed from `docs/WAVE7_CONTRACT.md`.

    Used only until `app/backend/pg/reporting.py` exists. It is deliberately
    NOT re-exported under a second name: there is one `FilterSet` symbol in
    this module and it is either A1's or this.
    """

    entity_ids: tuple[str, ...] | None = None
    plant_ids: tuple[str, ...] | None = None
    location_ids: tuple[str, ...] | None = None
    project_ids: tuple[str, ...] | None = None
    wbs_paths: tuple[str, ...] | None = None
    budget_head_ids: tuple[str, ...] | None = None
    category_ids: tuple[str, ...] | None = None
    vendor_ids: tuple[str, ...] | None = None
    item_ids: tuple[str, ...] | None = None
    document_types: tuple[str, ...] | None = None
    lifecycle_statuses: tuple[str, ...] | None = None
    approval_statuses: tuple[str, ...] | None = None
    date_from: str | None = None
    date_to: str | None = None
    period_ids: tuple[str, ...] | None = None
    group_by: tuple[str, ...] | None = None
    cursor: str | None = None
    limit: int | None = None
    sort: str | None = None


try:  # pragma: no cover - exercised by whichever branch the tree is in
    from .reporting import FilterSet  # type: ignore[attr-defined]  # noqa: F401

    FILTERSET_SOURCE = "app.backend.pg.reporting"
except Exception:  # noqa: BLE001 - ImportError today; AttributeError if A1 renames
    FilterSet = _ContractFilterSet  # type: ignore[misc,assignment]
    FILTERSET_SOURCE = "app.backend.pg.exports._ContractFilterSet (A1 pending)"


# ===========================================================================
# Errors
# ===========================================================================
class ExportError(Exception):
    """Every refusal this module makes, with the code the router renders.

    Coded, always. "Never fabricate a total or a row count. Refuse with a coded
    error" -- a bare exception message is not a code, and a caller cannot
    branch on prose.
    """

    def __init__(self, code: str, message: str, *, status: int = 400,
                 detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = dict(detail or {})


# ===========================================================================
# Money -- integer paise, formatted exactly
# ===========================================================================
def format_paise(paise: Any) -> str:
    """`123456` -> ``"1234.56"``. `-5` -> ``"-0.05"``.

    Integer `divmod` only. No float, no `Decimal`, no locale, no thousands
    separator (a separator is a rendering choice that changes with locale, and
    this file is parsed by other software).

    A non-integer is a REFUSAL, not a coercion. `float(paise)/100` is how a
    rounded rupee reaches an auditor's spreadsheet, and `Decimal` reaching here
    means a `SUM()` lost its `::bigint` cast upstream -- both are worth failing
    on rather than papering over.
    """
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise ExportError(
            "MONEY_NOT_INTEGER_PAISE",
            f"money must reach the export as an integer number of paise; got "
            f"{type(paise).__name__} ({paise!r}). A Decimal here means a SUM() "
            f"upstream is missing its ::bigint cast; a float means somebody "
            f"divided by 100 before this point.",
            status=500)
    negative = paise < 0
    whole, fraction = divmod(-paise if negative else paise, 100)
    return f"{'-' if negative else ''}{whole}.{fraction:02d}"


# ===========================================================================
# Scope: serialise, digest, deserialise, meet
# ===========================================================================
#: Bumped only if the wire shape below changes. A job carrying an unknown
#: version is refused rather than read with today's reader.
SCOPE_DOC_VERSION = 1

#: The four dimensions, in `engine.Scope`'s order.
_DIMENSIONS: tuple[str, ...] = ("entity_ids", "plant_ids", "project_ids",
                                "location_ids")


def serialise_scope(scope: Scope) -> dict[str, Any]:
    """Render a `Scope` into the job row's `scope_json`.

    THE THREE STATES SURVIVE, and that is the whole job of this function.
    `None` (unrestricted) becomes JSON `null`; `frozenset()` (restricted to
    nothing) becomes `[]`; a populated set becomes a SORTED list. Sorted so the
    digest is a function of the scope and not of set iteration order.

    Collapsing `[]` into `null` here would turn "no grants" into "all rows" on
    every subsequent read of this job. That inversion is the defect the whole
    scope stack is built to make unrepresentable, and JSON is exactly where a
    careless `or None` reintroduces it.
    """
    doc: dict[str, Any] = {
        "version": SCOPE_DOC_VERSION,
        "user_id": scope.user_id,
        "principal_kind": scope.principal_kind,
        "read_all": bool(scope.read_all),
        "triage_unattributed": bool(scope.triage_unattributed),
    }
    for field_name in _DIMENSIONS:
        values = getattr(scope, field_name)
        doc[field_name] = None if values is None else sorted(str(v) for v in values)
    return doc


def deserialise_scope(doc: Mapping[str, Any]) -> Scope:
    """The inverse of :func:`serialise_scope`, failing closed on anything odd.

    Every rejection raises rather than returning a partially-read scope: a
    scope we cannot read is a scope we must not widen, and the caller
    (:func:`advance_job`) turns the raise into a FAILED job with a code.
    """
    if not isinstance(doc, Mapping):
        raise ExportError("SCOPE_UNREADABLE",
                          f"stored scope is {type(doc).__name__}, not an object",
                          status=500)
    version = doc.get("version")
    if version != SCOPE_DOC_VERSION:
        raise ExportError(
            "SCOPE_VERSION_UNSUPPORTED",
            f"stored scope declares version {version!r}; this build reads "
            f"version {SCOPE_DOC_VERSION}. Refusing rather than reading an "
            f"unknown shape with today's reader.",
            status=500)
    user_id = doc.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ExportError("SCOPE_UNREADABLE",
                          "stored scope names no user_id", status=500)
    kind = doc.get("principal_kind")
    if kind not in principal_scope.PRINCIPAL_KINDS:
        raise ExportError(
            "SCOPE_UNREADABLE",
            f"stored scope declares principal_kind {kind!r}", status=500)

    dimensions: dict[str, frozenset[str] | None] = {}
    for field_name in _DIMENSIONS:
        raw = doc.get(field_name, "__absent__")
        if raw == "__absent__":
            raise ExportError(
                "SCOPE_UNREADABLE",
                f"stored scope omits {field_name}. An omitted dimension is not "
                f"an unrestricted one -- refusing rather than guessing.",
                status=500)
        if raw is None:
            dimensions[field_name] = None
            continue
        if not isinstance(raw, list):
            raise ExportError(
                "SCOPE_UNREADABLE",
                f"stored scope's {field_name} is {type(raw).__name__}; expected "
                f"null (unrestricted) or a list of ids",
                status=500)
        values: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ExportError(
                    "SCOPE_UNREADABLE",
                    f"stored scope's {field_name} carries a non-id member "
                    f"{item!r}",
                    status=500)
            values.add(item)
        dimensions[field_name] = frozenset(values)

    # `read_all` must be the literal `true`. A truthy non-bool (1, "yes") is
    # not the flag; treating it as one is how an unrestricted read appears from
    # a JSON type confusion.
    return Scope(
        user_id=user_id,
        principal_kind=kind,
        entity_ids=dimensions["entity_ids"],
        plant_ids=dimensions["plant_ids"],
        project_ids=dimensions["project_ids"],
        location_ids=dimensions["location_ids"],
        read_all=doc.get("read_all") is True,
        triage_unattributed=doc.get("triage_unattributed") is True,
    )


def canonical_scope_bytes(doc: Mapping[str, Any]) -> bytes:
    """The exact bytes the digest is taken over.

    `sort_keys=True`, no whitespace, `ensure_ascii=True`. The digest must be a
    function of the SCOPE, never of how psycopg happened to round-trip the
    jsonb -- PostgreSQL stores jsonb key order and whitespace-normalised, so
    hashing the raw text would compare a value written by Python against a
    value returned by the server and find them different for no reason.
    """
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def scope_digest(export_job_id: str, doc: Mapping[str, Any]) -> str:
    """SHA-256 over the job id and the canonical scope.

    The job id is inside the digest deliberately: without it a scope document
    lifted from one job verifies perfectly on another, so an attacker holding
    any wide-scoped job could paste its scope onto a narrow one.

    UNKEYED, and that is a documented limitation rather than an oversight --
    see the module docstring. It detects a rewritten row; it does not prevent
    one, and :func:`meet_scopes` is what makes prevention unnecessary.
    """
    material = export_job_id.encode("utf-8") + b"\x00" + canonical_scope_bytes(doc)
    return hashlib.sha256(material).hexdigest()


def verify_scope_digest(export_job_id: str, doc: Mapping[str, Any],
                        stored_digest: str) -> None:
    """Raise `ExportError('SCOPE_DIGEST_MISMATCH')` unless the digest agrees."""
    expected = scope_digest(export_job_id, doc)
    # Constant-time, because the comparison is over an attacker-influenced
    # value even though the digest is unkeyed; there is no reason to leak the
    # prefix length of a match.
    if not hmac.compare_digest(expected, str(stored_digest or "")):
        raise ExportError(
            "SCOPE_DIGEST_MISMATCH",
            f"export job {export_job_id} carries a scope whose digest does not "
            f"verify. The captured authorisation has been altered since "
            f"creation; no row has been read and none will be.",
            status=409)


def meet_scopes(a: Scope, b: Scope) -> Scope:
    """The greatest lower bound of two scopes. NARROWING ONLY.

    Per dimension, `None` is the top of the lattice (unrestricted) and
    `frozenset()` the bottom (nothing):

    ==============  ==============  ==================
    ``a``           ``b``           meet
    ==============  ==============  ==================
    ``None``        ``None``        ``None``
    ``None``        ``S``           ``S``
    ``S``           ``None``        ``S``
    ``S``           ``T``           ``S & T``
    ==============  ==============  ==================

    `read_all` and `triage_unattributed` are `AND`ed, so a flag present on one
    side only does not survive. There is NO branch in this function that can
    return a value wider than either argument on any dimension -- which is the
    property `tests/test_export_scope.py` asserts exhaustively rather than by
    inspection.

    `user_id` and `principal_kind` come from `a`, the captured side: the meet
    answers "what may this job read", and the job's requester is a fact about
    the job, not something the live resolution gets to change.
    """
    merged: dict[str, frozenset[str] | None] = {}
    for field_name in _DIMENSIONS:
        left = getattr(a, field_name)
        right = getattr(b, field_name)
        if left is None:
            merged[field_name] = right
        elif right is None:
            merged[field_name] = left
        else:
            merged[field_name] = left & right
    return Scope(
        user_id=a.user_id,
        principal_kind=a.principal_kind,
        entity_ids=merged["entity_ids"],
        plant_ids=merged["plant_ids"],
        project_ids=merged["project_ids"],
        location_ids=merged["location_ids"],
        read_all=bool(a.read_all and b.read_all),
        triage_unattributed=bool(a.triage_unattributed and b.triage_unattributed),
    )


#: The service principal SVC-EXPORT runs its DISCOVERY session as. Every
#: dimension `frozenset()`, `read_all` false: it compiles to FALSE against
#: every business table, and migration 017's `export_job_service` policy is the
#: only thing it can see at all.
SERVICE_USER_ID = "SVC-EXPORT"


def service_scope() -> Scope:
    """The scope the worker's DISCOVERY SESSION is opened with. Denied.

    Every dimension `frozenset()`, `read_all` false. `repo.compile_scope`
    short-circuits that to ``FALSE`` -- for every mapping, including one that
    waives all four dimensions, because the emptiness check runs before the
    waiver. So any query written in that session gets no rows by default, and
    a business query added to the worker later fails closed rather than
    inheriting a permissive predicate.
    """
    return principal_scope.denied_scope(SERVICE_USER_ID, "SERVICE")


def job_registry_scope() -> Scope:
    """The DELIBERATE, NARROW EXCEPTION to :func:`service_scope`.

    The worker has to read `export_job` rows to find work, and a denied scope
    compiles to FALSE against those too. Rather than widening the session --
    which would widen every statement in it, including one somebody adds next
    year -- this scope is passed as `repo.query(..., scope=...)` at the three
    call sites that read or finish a JOB ROW, and nowhere else.

    `repo.query`'s override changes only the compiled predicate; it does NOT
    touch `SET LOCAL`, so ROW-LEVEL SECURITY still sees the denied session's
    settings and still admits these rows through migration 017's
    `export_job_service` policy alone -- which checks the principal is
    SVC-EXPORT and covers `export_job` and `export_job_chunk` and nothing else.
    Two layers, and the wider one is scoped to three statements.

    `read_all` stays FALSE. `read_all` short-circuits `compile_scope` to TRUE
    before it looks at a dimension, so setting it here would hand the override
    a blanket that would apply to any table it were ever passed to.
    """
    return Scope(user_id=SERVICE_USER_ID, principal_kind="SERVICE",
                 entity_ids=None, plant_ids=None, project_ids=None,
                 location_ids=None, read_all=False)


# ===========================================================================
# Datasets -- deterministic columns, a unique sort key, all four dimensions
# ===========================================================================
#: How a column is rendered. `paise` goes through :func:`format_paise`; nothing
#: else may.
ColumnKind = str  # "text" | "paise" | "int" | "numeric" | "timestamp" | "date" | "bool"


@dataclass(frozen=True)
class Column:
    """One output column: its header, its SQL expression and how it renders."""

    name: str
    sql: str
    kind: ColumnKind = "text"


@dataclass(frozen=True)
class Dataset:
    """One exportable result shape.

    `scope_columns` names ALL FOUR dimensions -- a dimension this shape cannot
    reach is waived by an explicit `None`, never by omission, because
    `repo.compile_scope` treats an omission as a refusal and a waiver as a
    decision. Every dataset here reaches all four through `project`, so none of
    them waives anything, and that is worth stating: an export is the widest
    read in the product and the one where a waiver would be least visible.

    `order_by` must be a UNIQUE total order. A chunked export resumes from the
    last row it committed, and a non-unique sort makes "the row after this one"
    ambiguous -- which duplicates or drops rows at every chunk boundary.
    """

    name: str
    title: str
    from_sql: str
    columns: tuple[Column, ...]
    #: SQL expressions forming the unique sort key, in order. Also the keyset
    #: cursor.
    key_sql: tuple[str, ...]
    scope_columns: Mapping[str, str | None]
    #: FilterSet field -> a builder returning (sql_fragment, params).
    filters: Mapping[str, Callable[[Any, str], tuple[str, dict[str, Any]]]]
    #: Filter fields this dataset refuses outright, with the reason.
    unsupported: Mapping[str, str] = dataclass_field(default_factory=dict)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def _in_list(column: str):
    """`column = ANY(%(p)s)` for a list-valued filter."""

    def build(value: Any, param: str) -> tuple[str, dict[str, Any]]:
        values = sorted({str(v) for v in value})
        return f"({column} = ANY(%({param})s))", {param: values}

    return build


def _ltree_subtree(column: str):
    """`column <@ ANY(paths)` -- the contract's `wbs_paths` is a SUBTREE
    filter, not an id list. A caller narrowing to `A.B` means A.B and
    everything under it."""

    def build(value: Any, param: str) -> tuple[str, dict[str, Any]]:
        paths = sorted({str(v) for v in value})
        return (f"({column} <@ ANY(%({param})s::ltree[]))", {param: paths})

    return build


def _date_at_or_after(column: str):
    def build(value: Any, param: str) -> tuple[str, dict[str, Any]]:
        return f"({column} >= %({param})s)", {param: str(value)}

    return build


def _date_at_or_before(column: str):
    def build(value: Any, param: str) -> tuple[str, dict[str, Any]]:
        return f"({column} <= %({param})s)", {param: str(value)}

    return build


#: Every dataset reaches all four dimensions through `project`, aliased `p`.
#: Named once so a new dataset cannot quietly ship a narrower mapping.
_PROJECT_REACHING_COLUMNS: dict[str, str | None] = {
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
    "project": "p.project_id",
}

#: `period_ids` is a real contract field and no dataset here can honour it:
#: none of the three shapes carries an `accounting_period` reference, and
#: mapping a period to a date range would be this module inventing the
#: calendar. Refused by name rather than ignored.
_NO_PERIOD = ("this dataset carries no accounting_period reference. Filtering "
              "by period would mean deriving a date range from a period id "
              "here, which is the period service's answer to give, not this "
              "module's to guess.")
_NO_CATEGORY = ("AMB-04 makes budget head the primary reading and no table in "
                "this schema carries a category id; use budget_head_ids.")
#: FABLE 5.1 / migration 026: `budget_category` is now a real, independent
#: master and `budget_control_cell.budget_category_id` is a real column --
#: datasets that reach a control cell join it and honour `category_ids`
#: against it (see BUDGET_LEDGER_CELLS, PURCHASE_ORDER_LINES). `_NO_CATEGORY`
#: stays for shapes that do not reach a cell at all (WBS_ELEMENTS: a WBS
#: element can carry several cells, one per budget head, each with its own
#: category, so there is no single category id to put on that row).
_NO_ITEM = "no dataset here joins item_master."
_NO_APPROVAL = ("approval status lives on approval_instance, which none of "
                "these shapes joins.")


BUDGET_LEDGER_CELLS = Dataset(
    name="budget_ledger_cells",
    title="Budget control cells with their stored ledger position",
    from_sql="""
        FROM budget_control_cell bc
        JOIN wbs_element w ON w.wbs_id = bc.wbs_id
        JOIN project p ON p.project_id = w.project_id
        JOIN budget_head bh ON bh.budget_head_id = bc.budget_head_id
        -- FABLE 5.1 / migration 026: the CELL's own category, LEFT joined so a
        -- pre-026 cell (budget_category_id NULL) still exports its row --
        -- `budget_category_name` renders empty, which `render_value` already
        -- turns `None` into the empty string for, and never "Unclassified" in
        -- a CSV cell: that label is a REPORTING-SCREEN rendering choice
        -- (`reporting._shape`), not an export one.
        LEFT JOIN budget_category bcat ON bcat.category_id = bc.budget_category_id
        LEFT JOIN budget_ledger_cell bl
               ON bl.wbs_id = bc.wbs_id
              AND bl.budget_head_id = bc.budget_head_id
    """,
    columns=(
        Column("project_id", "p.project_id"),
        Column("project_capex_code", "p.capex_code"),
        Column("project_name", "p.name"),
        Column("entity_id", "p.entity_id"),
        Column("plant_id", "p.plant_id"),
        Column("location_id", "p.location_id"),
        Column("wbs_id", "w.wbs_id"),
        Column("wbs_code", "w.wbs_code"),
        Column("wbs_description", "w.description"),
        Column("wbs_path", "w.wbs_path::text"),
        Column("wbs_level", "w.level", "int"),
        Column("budget_head_id", "bc.budget_head_id"),
        Column("budget_head_code", "bh.code"),
        Column("budget_head_name", "bh.name"),
        Column("budget_category_id", "bc.budget_category_id"),
        Column("budget_category_code", "bcat.code"),
        Column("budget_category_name", "bcat.name"),
        # The ten metrics, read from where they are STORED. Not re-derived in
        # SQL: `docs/WAVE7_CONTRACT.md` -- "Do not re-derive them in SQL from
        # memory". `budget_ledger_cell` is what the ledger writers maintain and
        # `budget_control_cell.budget_paise` is the approved-and-effective
        # budget; both are read verbatim.
        Column("budget", "bc.budget_paise", "paise"),
        Column("original", "COALESCE(bl.original_paise, 0)", "paise"),
        Column("revisions", "COALESCE(bl.revisions_paise, 0)", "paise"),
        Column("future_budget", "COALESCE(bl.future_budget_paise, 0)", "paise"),
        Column("ordered", "COALESCE(bl.ordered_paise, 0)", "paise"),
        Column("commitment", "COALESCE(bl.commitment_paise, 0)", "paise"),
        Column("actual", "COALESCE(bl.actual_paise, 0)", "paise"),
        Column("received", "COALESCE(bl.received_paise, 0)", "paise"),
        Column("received_not_billed",
               "COALESCE(bl.received_not_billed_paise, 0)", "paise"),
        Column("pr_reserved", "COALESCE(bl.pr_reserved_paise, 0)", "paise"),
        # `exposure` and `available` transcribed from
        # `app/backend/pg/budget.py::list_cells`, which is the same arithmetic
        # `_subtree_totals` uses. Written as SQL over the same three stored
        # columns rather than restated in Python so the expression sits beside
        # the columns it sums; `tests/test_pg_exports.py` asserts the exported
        # values equal `budget.list_cells`' for the same cells rather than
        # trusting this comment.
        Column("exposure",
               "(COALESCE(bl.commitment_paise, 0) + COALESCE(bl.actual_paise, 0)"
               " + COALESCE(bl.pr_reserved_paise, 0))", "paise"),
        Column("available",
               "(bc.budget_paise - (COALESCE(bl.commitment_paise, 0)"
               " + COALESCE(bl.actual_paise, 0)"
               " + COALESCE(bl.pr_reserved_paise, 0)))", "paise"),
        Column("recomputed_at", "bc.updated_at", "timestamp"),
    ),
    # `ux_wbs_path` makes `wbs_path` unique, and `(wbs_id, budget_head_id)` is
    # `budget_control_cell`'s primary key -- so this pair is a unique total
    # order and the same one `budget.list_cells` pages by.
    key_sql=("w.wbs_path::text", "bc.budget_head_id"),
    scope_columns=_PROJECT_REACHING_COLUMNS,
    filters={
        "entity_ids": _in_list("p.entity_id"),
        "plant_ids": _in_list("p.plant_id"),
        "location_ids": _in_list("p.location_id"),
        "project_ids": _in_list("p.project_id"),
        "wbs_paths": _ltree_subtree("w.wbs_path"),
        "budget_head_ids": _in_list("bc.budget_head_id"),
        # FABLE 5.1 / migration 026: real, independent of budget_head_ids --
        # both may be supplied together, and the predicates AND (INTERSECT),
        # never coalesce, exactly as `reporting.py`'s outer predicate does.
        "category_ids": _in_list("bc.budget_category_id"),
        "date_from": _date_at_or_after("bc.updated_at"),
        "date_to": _date_at_or_before("bc.updated_at"),
    },
    unsupported={
        "period_ids": _NO_PERIOD,
        "item_ids": _NO_ITEM, "approval_statuses": _NO_APPROVAL,
        "vendor_ids": "a budget cell has no vendor.",
        "document_types": "a budget cell is not a document.",
        "lifecycle_statuses": ("lifecycle status is a property of a project or "
                               "WBS element; export the wbs_elements dataset "
                               "for it."),
    },
)


WBS_ELEMENTS = Dataset(
    name="wbs_elements",
    title="WBS elements with their project and lifecycle status",
    from_sql="""
        FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
    """,
    columns=(
        Column("project_id", "p.project_id"),
        Column("project_capex_code", "p.capex_code"),
        Column("project_name", "p.name"),
        Column("project_status", "p.status"),
        Column("entity_id", "p.entity_id"),
        Column("plant_id", "p.plant_id"),
        Column("location_id", "p.location_id"),
        Column("wbs_id", "w.wbs_id"),
        Column("wbs_code", "w.wbs_code"),
        Column("description", "w.description"),
        Column("wbs_path", "w.wbs_path::text"),
        Column("parent_wbs_id", "w.parent_wbs_id"),
        Column("level", "w.level", "int"),
        Column("sort_order", "w.sort_order", "int"),
        Column("status", "w.status"),
        Column("is_abandoned", "w.is_abandoned", "bool"),
        Column("default_budget_head_id", "w.budget_head_id"),
        Column("updated_at", "w.updated_at", "timestamp"),
    ),
    key_sql=("w.wbs_path::text",),
    scope_columns=_PROJECT_REACHING_COLUMNS,
    filters={
        "entity_ids": _in_list("p.entity_id"),
        "plant_ids": _in_list("p.plant_id"),
        "location_ids": _in_list("p.location_id"),
        "project_ids": _in_list("p.project_id"),
        "wbs_paths": _ltree_subtree("w.wbs_path"),
        "budget_head_ids": _in_list("w.budget_head_id"),
        "lifecycle_statuses": _in_list("w.status"),
        "date_from": _date_at_or_after("w.updated_at"),
        "date_to": _date_at_or_before("w.updated_at"),
    },
    unsupported={
        "period_ids": _NO_PERIOD, "category_ids": _NO_CATEGORY,
        "item_ids": _NO_ITEM, "approval_statuses": _NO_APPROVAL,
        "vendor_ids": "a WBS element has no vendor.",
        "document_types": "a WBS element is not a document.",
    },
)


PURCHASE_ORDER_LINES = Dataset(
    name="purchase_order_lines",
    title="Purchase order lines at their (WBS x budget head) grain",
    from_sql="""
        FROM po_line pl
        JOIN purchase_order po ON po.po_id = pl.po_id
        JOIN project p ON p.project_id = pl.project_id
        -- FABLE 5.1 / migration 026: the line's own CELL's category, read the
        -- same way `reporting.py`'s PO branch reads it -- LEFT joined on
        -- (wbs_id, budget_head_id), so a line whose cell pre-dates 026 still
        -- exports.
        LEFT JOIN budget_control_cell bc
               ON bc.wbs_id = pl.wbs_id AND bc.budget_head_id = pl.budget_head_id
        LEFT JOIN budget_category bcat ON bcat.category_id = bc.budget_category_id
    """,
    columns=(
        Column("po_id", "po.po_id"),
        Column("po_number", "po.po_number"),
        Column("po_status", "po.status"),
        Column("pr_id", "po.pr_id"),
        Column("project_id", "p.project_id"),
        Column("project_capex_code", "p.capex_code"),
        Column("entity_id", "p.entity_id"),
        Column("plant_id", "p.plant_id"),
        Column("location_id", "p.location_id"),
        Column("vendor_name", "po.vendor_name"),
        Column("currency", "po.currency"),
        Column("line_no", "pl.line_no", "int"),
        Column("po_line_id", "pl.po_line_id"),
        Column("wbs_id", "pl.wbs_id"),
        Column("budget_head_id", "pl.budget_head_id"),
        Column("budget_category_id", "bc.budget_category_id"),
        Column("budget_category_code", "bcat.code"),
        Column("budget_category_name", "bcat.name"),
        Column("description", "pl.description"),
        Column("quantity", "pl.quantity", "numeric"),
        Column("rate", "pl.rate_paise", "paise"),
        Column("amount", "pl.amount_paise", "paise"),
        Column("tax", "pl.tax_paise", "paise"),
        Column("non_creditable_tax", "pl.non_creditable_tax_paise", "paise"),
        Column("freight", "pl.freight_paise", "paise"),
        # `ordered` transcribed VERBATIM from `app.backend.domain.compute_ledger`:
        #     pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise
        # Creditable tax is deliberately absent -- it is recoverable and is not
        # a charge against the budget. Re-deriving this from memory is exactly
        # what `docs/WAVE7_CONTRACT.md` forbids, so it is copied, not recalled.
        Column("ordered",
               "(pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise)",
               "paise"),
        Column("ordered_at", "po.ordered_at", "timestamp"),
        Column("amendment_no", "po.amendment_no", "int"),
        Column("updated_at", "pl.updated_at", "timestamp"),
    ),
    # `ux_purchase_order_number` makes `po_number` unique and
    # `ux_po_line_number` makes `line_no` unique within a PO, so the pair is a
    # unique total order.
    key_sql=("po.po_number", "pl.line_no::text"),
    scope_columns=_PROJECT_REACHING_COLUMNS,
    filters={
        "entity_ids": _in_list("p.entity_id"),
        "plant_ids": _in_list("p.plant_id"),
        "location_ids": _in_list("p.location_id"),
        "project_ids": _in_list("p.project_id"),
        "budget_head_ids": _in_list("pl.budget_head_id"),
        # FABLE 5.1 / migration 026: real, via the line's own cell (see the
        # LEFT JOIN above) -- independent of budget_head_ids and composable
        # with it, same as reporting.py's FilterSet.
        "category_ids": _in_list("bc.budget_category_id"),
        "lifecycle_statuses": _in_list("po.status"),
        "date_from": _date_at_or_after("po.ordered_at"),
        "date_to": _date_at_or_before("po.ordered_at"),
    },
    unsupported={
        "period_ids": _NO_PERIOD,
        "item_ids": _NO_ITEM, "approval_statuses": _NO_APPROVAL,
        "wbs_paths": ("po_line carries wbs_id, not wbs_path; a subtree filter "
                      "needs a join this shape does not make."),
        "vendor_ids": ("purchase_order carries vendor_name, not a vendor_master "
                       "id -- filtering by id would silently match nothing."),
        "document_types": ("this dataset is purchase order lines only; the "
                           "document type is 'PO' for every row."),
    },
)


DATASETS: dict[str, Dataset] = {
    d.name: d for d in (BUDGET_LEDGER_CELLS, WBS_ELEMENTS, PURCHASE_ORDER_LINES)
}


def get_dataset(name: str) -> Dataset:
    dataset = DATASETS.get(str(name))
    if dataset is None:
        raise ExportError(
            "UNKNOWN_DATASET",
            f"no export dataset named {name!r}. Known datasets: "
            f"{', '.join(sorted(DATASETS))}.",
            status=404, detail={"known": sorted(DATASETS)})
    return dataset


# ===========================================================================
# Filters
# ===========================================================================
def normalise_filters(filters: Any) -> dict[str, Any]:
    """Read the contract's fields off a `FilterSet`, a mapping, or `None`.

    Returns only the fields that carry a value -- an absent field and an
    explicitly empty one are both "do not filter on this", and the difference
    does not survive JSON anyway. A field NOT in :data:`FILTER_FIELDS` is a
    refusal: it is either a typo (which would otherwise be silently ignored,
    widening the result) or a field this build does not know, and both deserve
    saying so.
    """
    if filters is None:
        return {}
    if isinstance(filters, Mapping):
        raw = dict(filters)
    else:
        raw = {name: getattr(filters, name)
               for name in FILTER_FIELDS if hasattr(filters, name)}
        # A FilterSet carrying a field this build has never heard of is A1's
        # contract having moved. Surface it rather than exporting under a
        # filter that was never applied.
        extra = sorted(
            name for name in vars(filters) if not name.startswith("_")
        ) if hasattr(filters, "__dict__") else []
        unknown = [n for n in extra if n not in FILTER_FIELDS]
        if unknown:
            raise ExportError(
                "UNKNOWN_FILTER",
                f"filter set carries field(s) this build does not know: "
                f"{', '.join(unknown)}. Applying the rest and ignoring these "
                f"would widen the export silently.",
                detail={"unknown": unknown})

    unknown = sorted(set(raw) - set(FILTER_FIELDS))
    if unknown:
        raise ExportError(
            "UNKNOWN_FILTER",
            f"unknown filter field(s): {', '.join(unknown)}. Known fields: "
            f"{', '.join(FILTER_FIELDS)}.",
            detail={"unknown": unknown})

    out: dict[str, Any] = {}
    for name in FILTER_FIELDS:
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            if not value:
                continue
            out[name] = sorted({str(v) for v in value})
        elif isinstance(value, str):
            if not value.strip():
                continue
            out[name] = value
        else:
            out[name] = value
    return out


def plan_filters(dataset: Dataset, filters: Mapping[str, Any]
                 ) -> tuple[list[str], dict[str, Any]]:
    """Compile the caller's filters into SQL, refusing any this shape cannot
    apply.

    THE REFUSAL IS THE POINT. Dropping an unapplicable filter widens the
    result set, and the caller reading the file has no way to tell the export
    ignored half of what they asked for.
    """
    conditions: list[str] = []
    params: dict[str, Any] = {}
    for index, (name, value) in enumerate(sorted(filters.items())):
        if name in FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT:
            raise ExportError(
                "UNSUPPORTED_FILTER",
                f"{name!r} has no meaning for an export: an export delivers the "
                f"whole result set in a captured column and row order. Paging "
                f"and re-sorting belong to the report API.",
                detail={"field": name})
        reason = dataset.unsupported.get(name)
        if reason is not None:
            raise ExportError(
                "UNSUPPORTED_FILTER",
                f"dataset {dataset.name!r} cannot apply the filter {name!r}: "
                f"{reason} Refusing rather than exporting a wider result than "
                f"was asked for.",
                detail={"field": name, "dataset": dataset.name})
        builder = dataset.filters.get(name)
        if builder is None:
            raise ExportError(
                "UNSUPPORTED_FILTER",
                f"dataset {dataset.name!r} declares no column for the filter "
                f"{name!r}, and no reason for refusing it. That is a gap in the "
                f"dataset definition, not a permission to ignore the filter.",
                status=500, detail={"field": name, "dataset": dataset.name})
        fragment, built = builder(value, f"f_{index}_{name}")
        conditions.append(fragment)
        params.update(built)
    return conditions, params


# ===========================================================================
# Rendering
# ===========================================================================
#: Values a spreadsheet may evaluate as a formula when a CSV is opened. Only
#: TEXT columns are neutralised: a `paise` column is produced by
#: :func:`format_paise` and its leading `-` is a minus sign, not an injection.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _render_text(value: str) -> str:
    """Neutralise a leading formula character by prefixing an apostrophe.

    The convention every spreadsheet understands, and it is applied to TEXT
    only. A WBS description reading ``=cmd|' /c calc'!A0`` is data here and
    must stay data when the file is opened.
    """
    return f"'{value}" if value.startswith(_FORMULA_LEADERS) else value


def render_value(kind: ColumnKind, value: Any) -> str:
    """One cell, deterministically. `None` renders as the empty string."""
    if value is None:
        return ""
    if kind == "paise":
        return format_paise(value)
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExportError(
                "COLUMN_TYPE_MISMATCH",
                f"an int column received {type(value).__name__}", status=500)
        return str(value)
    if kind == "bool":
        return "true" if value else "false"
    if kind == "numeric":
        # `format(Decimal, 'f')` renders without an exponent, so 1E+2 and 100
        # do not appear as different quantities in the same column.
        return format(value, "f") if isinstance(value, Decimal) else str(value)
    if kind in ("timestamp", "date"):
        if hasattr(value, "isoformat"):
            if isinstance(value, datetime):
                # UTC always. A timestamptz is rendered in the CONNECTION's
                # TimeZone, so an export run from a differently-configured
                # process would otherwise disagree with itself.
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc).isoformat()
            return value.isoformat()
        return str(value)
    return _render_text(str(value))


def render_rows(dataset: Dataset, rows: Iterable[Sequence[Any]]) -> str:
    """Render rows to CSV, WITHOUT a header.

    The header belongs to the assembled result, not to a chunk: a chunk is a
    fragment and repeating the header inside the file is how a 250,000-row
    export acquires 50 spurious rows.

    `lineterminator="\\n"` explicitly -- `csv`'s default is CRLF, which makes
    the byte count and the SHA-256 of an identical export differ from what a
    reader computing them on Unix expects.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    kinds = [c.kind for c in dataset.columns]
    for row in rows:
        if len(row) != len(kinds):
            raise ExportError(
                "COLUMN_COUNT_MISMATCH",
                f"dataset {dataset.name!r} declares {len(kinds)} columns but a "
                f"row carried {len(row)}", status=500)
        writer.writerow([render_value(kind, value)
                         for kind, value in zip(kinds, row)])
    return buffer.getvalue()


def render_header(column_order: Sequence[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n",
               quoting=csv.QUOTE_MINIMAL).writerow(list(column_order))
    return buffer.getvalue()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# The job row
# ===========================================================================
#: The states, and which of them are terminal. Mirrors 017's
#: `ck_export_job_state` and `ck_export_job_terminal_is_finished`.
STATE_QUEUED = "QUEUED"
STATE_RUNNING = "RUNNING"
STATE_SUCCEEDED = "SUCCEEDED"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"
STATE_EXPIRED = "EXPIRED"
TERMINAL_STATES: frozenset[str] = frozenset(
    {STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED})
ACTIVE_STATES: frozenset[str] = frozenset({STATE_QUEUED, STATE_RUNNING})

DEFAULT_CHUNK_ROWS = 5000
MAX_CHUNK_ROWS = 50000
DEFAULT_TTL_HOURS = 24
DEFAULT_MAX_ATTEMPTS = 3

#: How many rows one invocation of :func:`advance_job` will write before it
#: commits and returns, leaving the rest to the next call. Sized against the
#: 30-second AppSail budget with room to spare -- the point of a checkpoint is
#: that being wrong about this costs a resume, not a failure.
DEFAULT_ROWS_PER_INVOCATION = 25000

#: `export_job` carries no dimension column. All four are waived by EXPLICIT
#: None -- a decision, per `repo.compile_scope`, rather than an omission, which
#: that function refuses. The row's authorisation is `requested_by`, which
#: every statement below filters on in its own right, and RLS enforces the same
#: line independently through `export_job_owner`.
#:
#: A DENIED scope still compiles to FALSE against this mapping:
#: `compile_scope` checks emptiness BEFORE it honours a waiver, deliberately,
#: so a principal who may see rows in zero entities sees no export jobs either.
JOB_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": None, "plant": None, "location": None, "project": None,
}

_JOB_COLUMNS = (
    "export_job_id", "dataset", "output_format", "state", "requested_by",
    "requested_principal_kind", "scope_json", "scope_digest", "filter_json",
    "column_order", "chunk_rows", "rows_written", "rows_total",
    "chunks_written", "resume_key", "attempt", "max_attempts",
    "cancel_requested", "error_code", "error_detail", "result_sha256",
    "result_bytes", "result_filename", "result_media_type", "correlation_id",
    "created_at", "started_at", "finished_at", "expires_at",
)
_JOB_SELECT = ", ".join(f"j.{c}" for c in _JOB_COLUMNS)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _job_row_to_dict(row: Sequence[Any]) -> dict[str, Any]:
    # WIDTH IS ASSERTED BECAUSE `zip` WILL NOT. Every one of the ten statements
    # that feeds this function selects exactly `_JOB_SELECT`, so a row of any
    # other width is a caller defect -- and `dict(zip(...))` answers a short row
    # by silently dropping the trailing columns, which is how `create_job` came
    # to hand over a row with `expires_at` sliced off and take a KeyError four
    # lines later. Named here, the error says which column set disagreed and
    # who asked; discovered by `zip`, it says 'expires_at' from inside a loop
    # that is not the mistake.
    if len(row) != len(_JOB_COLUMNS):
        raise ExportError(
            "EXPORT_JOB_ROW_SHAPE",
            f"an export job row of {len(row)} columns cannot be read against "
            f"the {len(_JOB_COLUMNS)} of _JOB_COLUMNS; the statement that "
            f"produced it does not select _JOB_SELECT",
            status=500)
    job = dict(zip(_JOB_COLUMNS, row))
    for key in ("created_at", "started_at", "finished_at", "expires_at"):
        job[key] = _iso(job[key])
    job["column_order"] = list(job["column_order"] or ())
    return job


def public_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """The job as a caller sees it.

    `scope_json` and `scope_digest` are NOT in it. The resolved scope is the
    authorisation the job runs under; echoing it back tells a caller the exact
    id set they hold, which is more than the rest of this API discloses and
    more than they need to poll a job.

    `progress` is honest about not knowing: `rows_total` is NULL until the last
    chunk, so `percent` is `None` rather than a number invented from a guessed
    denominator.
    """
    rows_total = job.get("rows_total")
    rows_written = job.get("rows_written") or 0
    percent: int | None = None
    if isinstance(rows_total, int) and rows_total > 0:
        percent = (int(rows_written) * 100) // rows_total
    elif isinstance(rows_total, int) and rows_total == 0:
        percent = 100
    return {
        "export_job_id": job["export_job_id"],
        "dataset": job["dataset"],
        "format": job["output_format"],
        "state": job["state"],
        "requested_by": job["requested_by"],
        "columns": list(job.get("column_order") or ()),
        "filters": dict(job.get("filter_json") or {}),
        "progress": {
            "rows_written": int(rows_written),
            "rows_total": rows_total,
            "chunks_written": int(job.get("chunks_written") or 0),
            "percent": percent,
            "percent_note": (
                None if percent is not None else
                "the total is not known until the export finishes; this build "
                "does not estimate one"),
        },
        "attempt": job.get("attempt"),
        "max_attempts": job.get("max_attempts"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "error": (None if not job.get("error_code") else
                  {"code": job["error_code"], "detail": job.get("error_detail")}),
        "result": (None if job["state"] != STATE_SUCCEEDED else {
            "filename": job.get("result_filename"),
            "media_type": job.get("result_media_type"),
            "bytes": job.get("result_bytes"),
            "sha256": job.get("result_sha256"),
            "rows": job.get("rows_total"),
            "expires_at": job.get("expires_at"),
        }),
        "correlation_id": job.get("correlation_id"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "expires_at": job.get("expires_at"),
    }


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _new_job_id() -> str:
    return f"EXP-{uuid.uuid4().hex[:16].upper()}"


# ------------------------------------------------------------------ creation
def plan_job(dataset_name: str, filters: Any) -> tuple[Dataset, dict[str, Any]]:
    """Resolve the dataset and validate the filters WITHOUT touching the
    database. Separated so a router can refuse a bad request before it opens a
    transaction, and so the validation is testable with no PostgreSQL."""
    dataset = get_dataset(dataset_name)
    normalised = normalise_filters(filters)
    plan_filters(dataset, normalised)   # raises on anything unapplicable
    return dataset, normalised


def create_job(session: Session, *, dataset: str, filters: Any, scope: Scope,
               requested_by: str, principal_kind: str = "USER",
               correlation_id: str | None = None,
               chunk_rows: int = DEFAULT_CHUNK_ROWS,
               ttl_hours: int = DEFAULT_TTL_HOURS,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               now: datetime | None = None,
               job_id: str | None = None) -> dict[str, Any]:
    """Queue an export. Returns the job; the caller answers 202 with its id.

    `scope` MUST be the scope `principal_scope.scope_for_request` resolved for
    this request, and `requested_by` must be the server-derived acting user. A
    caller-supplied scope or actor here is a caller-supplied authorisation, and
    the whole capture is worthless. The signature takes them separately so the
    mismatch below is checkable rather than assumed.
    """
    resolved, normalised = plan_job(dataset, filters)
    actor = str(requested_by or "").strip()
    if not actor:
        raise ExportError("REQUESTER_UNKNOWN",
                          "an export job must name the requester; the acting "
                          "user is server-derived and was empty.", status=401)
    if str(scope.user_id) != actor:
        # The scope belongs to somebody else. Refuse rather than store it: a
        # job carrying A's scope under B's name runs A's authorisation for B.
        raise ExportError(
            "SCOPE_ACTOR_MISMATCH",
            f"the resolved scope is for {scope.user_id!r} but the job would be "
            f"attributed to {actor!r}. An export job's captured scope must be "
            f"the requester's own.",
            status=500)
    if principal_scope.is_denied(scope):
        # Not an empty export -- a refusal. See the module docstring: zero
        # data, denied scope and service failure are three distinct states.
        raise ExportError(
            "SCOPE_DENIED",
            "your resolved scope permits no rows on any dimension, so this "
            "export would contain nothing. That is a permission state, not an "
            "empty result, and it is reported as one.",
            status=403)
    if not isinstance(chunk_rows, int) or not (0 < chunk_rows <= MAX_CHUNK_ROWS):
        raise ExportError("INVALID_CHUNK_SIZE",
                          f"chunk_rows must be 1..{MAX_CHUNK_ROWS}")

    at = _now(now)
    export_job_id = job_id or _new_job_id()
    scope_doc = serialise_scope(scope)
    digest = scope_digest(export_job_id, scope_doc)

    # INSERT ... SELECT ... WHERE {scope}, not INSERT ... VALUES.
    #
    # The row does not exist yet, so there is nothing for a predicate to
    # filter -- but "may this principal create an export at all" IS a scope
    # question, and the `SELECT ... WHERE {scope}` form asks it: a DENIED
    # scope compiles to FALSE and the statement inserts nothing, exactly as it
    # returns nothing for a read. `create_job` also refuses a denied scope
    # explicitly above; this is the same answer enforced at the statement
    # rather than at the branch, and RLS's `export_job_owner` WITH CHECK is a
    # third, independent enforcement that the row is attributed to the session's
    # own principal.
    row = repo.query_one(
        session,
        f"""
        INSERT INTO export_job
            (export_job_id, dataset, output_format, state, requested_by,
             requested_principal_kind, scope_json, scope_digest, filter_json,
             column_order, chunk_rows, max_attempts, correlation_id,
             created_at, expires_at)
        SELECT %(export_job_id)s, %(dataset)s, 'csv', 'QUEUED',
               %(requested_by)s, %(principal_kind)s, %(scope_json)s,
               %(scope_digest)s, %(filter_json)s, %(column_order)s,
               %(chunk_rows)s, %(max_attempts)s, %(correlation_id)s,
               %(created_at)s, %(expires_at)s
        WHERE {{scope}}
        RETURNING {_JOB_SELECT.replace('j.', '')}
        """,
        {
            "export_job_id": export_job_id,
            "dataset": resolved.name,
            "requested_by": actor,
            "principal_kind": (principal_kind
                               if principal_kind in principal_scope.PRINCIPAL_KINDS
                               else "USER"),
            "scope_json": Jsonb(scope_doc),
            "scope_digest": digest,
            "filter_json": Jsonb(json.loads(
                json.dumps(normalised, sort_keys=True, default=str))),
            "column_order": list(resolved.column_names),
            "chunk_rows": chunk_rows,
            "max_attempts": max_attempts,
            "correlation_id": correlation_id,
            "created_at": at,
            "expires_at": at + timedelta(hours=max(1, int(ttl_hours))),
        },
        columns=JOB_SCOPE_COLUMNS,
    )
    if row is None:                                    # pragma: no cover
        raise ExportError("EXPORT_JOB_NOT_CREATED",
                          "the export job insert returned no row", status=500)
    # RETURNING lists exactly `_JOB_COLUMNS`, so the row is passed whole --
    # as every other `_job_row_to_dict` call site passes it. Dropping a
    # trailing element here severed `expires_at`, the last column, and
    # `_job_row_to_dict` then raised KeyError on its own timestamp loop.
    job = _job_row_to_dict(row)

    audit_svc.append(
        session, actor=actor, action="export.requested",
        object_type="export_job", object_id=export_job_id,
        detail=json.dumps({
            "dataset": resolved.name,
            "columns": list(resolved.column_names),
            "filters": normalised,
            "scope_digest": digest,
            "expires_at": job["expires_at"],
        }, sort_keys=True, default=str),
        correlation_id=correlation_id)
    return job


# ------------------------------------------------------------------ reading
def get_job(session: Session, export_job_id: str, *,
            requester: str) -> dict[str, Any] | None:
    """The job, or `None` for both "no such job" and "not yours".

    `None` for both, deliberately. A 403 on an id tells the caller the id is
    real, which is an existence oracle over somebody else's export.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT {_JOB_SELECT}
        FROM export_job j
        WHERE j.export_job_id = %(export_job_id)s
          AND j.requested_by = %(requester)s
          AND {{scope}}
        """,
        {"export_job_id": str(export_job_id), "requester": str(requester)},
        columns=JOB_SCOPE_COLUMNS,
    )
    return None if row is None else _job_row_to_dict(row)


def list_jobs(session: Session, *, requester: str, limit: int = 50,
              cursor: str | None = None) -> dict[str, Any]:
    """The caller's own jobs, newest first, keyset-paginated on
    `(created_at, export_job_id)` -- `created_at` alone is not unique."""
    limit = min(max(int(limit), 1), 200)
    conditions = ["j.requested_by = %(requester)s"]
    params: dict[str, Any] = {"requester": str(requester), "fetch": limit + 1}
    if cursor:
        try:
            at_text, after_id = json.loads(cursor)
        except Exception as exc:                       # noqa: BLE001
            raise ExportError("INVALID_CURSOR",
                              "the cursor is not one this endpoint issued") from exc
        conditions.append(
            "(j.created_at, j.export_job_id) < (%(cur_at)s, %(cur_id)s)")
        params["cur_at"] = at_text
        params["cur_id"] = after_id

    rows = repo.query(
        session,
        f"""
        SELECT {_JOB_SELECT}
        FROM export_job j
        WHERE {' AND '.join(conditions)} AND {{scope}}
        ORDER BY j.created_at DESC, j.export_job_id DESC
        LIMIT %(fetch)s
        """,
        params, columns=JOB_SCOPE_COLUMNS)

    has_more = len(rows) > limit
    page = [_job_row_to_dict(r) for r in rows[:limit]]
    next_cursor = (json.dumps([page[-1]["created_at"], page[-1]["export_job_id"]])
                   if has_more and page else None)
    return {"items": page, "next_cursor": next_cursor, "has_more": has_more}


def read_result(session: Session, export_job_id: str, *,
                requester: str) -> tuple[str, dict[str, Any]]:
    """The assembled export, with its metadata. Header plus every chunk in
    order.

    The digest is RECOMPUTED here and compared with the one recorded when the
    job finished. A result whose bytes changed after delivery was promised is
    not served -- migration 017 makes chunks append-only, so a mismatch means
    something reached past `capex_app`, and handing over the bytes anyway would
    be handing over evidence of unknown provenance.
    """
    job = get_job(session, export_job_id, requester=requester)
    if job is None:
        raise ExportError("EXPORT_JOB_NOT_FOUND",
                          f"no export job {export_job_id!r}", status=404)
    if job["state"] == STATE_EXPIRED:
        raise ExportError(
            "EXPORT_EXPIRED",
            f"export {export_job_id} expired at {job['expires_at']}; its "
            f"rendered rows have been purged. Request it again.",
            status=410)
    if job["state"] != STATE_SUCCEEDED:
        raise ExportError(
            "EXPORT_NOT_READY",
            f"export {export_job_id} is {job['state']}; there is no result to "
            f"download.",
            status=409, detail={"state": job["state"]})

    chunks = repo.query(
        session,
        """
        SELECT c.chunk_no, c.body, c.row_count
        FROM export_job_chunk c
        WHERE c.export_job_id = %(export_job_id)s
          AND EXISTS (SELECT 1 FROM export_job j
                      WHERE j.export_job_id = c.export_job_id
                        AND j.requested_by = %(requester)s
                        AND {scope})
        ORDER BY c.chunk_no
        """,
        {"export_job_id": str(export_job_id), "requester": str(requester)},
        columns=JOB_SCOPE_COLUMNS)

    expected = [n for n, _b, _r in chunks]
    if expected != list(range(1, len(chunks) + 1)):
        raise ExportError(
            "EXPORT_RESULT_INCOMPLETE",
            f"export {export_job_id} is recorded SUCCEEDED but its chunks are "
            f"not contiguous ({expected!r}). Refusing to serve a partial file "
            f"as a whole one.",
            status=500)
    rows_present = sum(int(r) for _n, _b, r in chunks)
    if rows_present != job["rows_total"]:
        raise ExportError(
            "EXPORT_RESULT_INCOMPLETE",
            f"export {export_job_id} records {job['rows_total']} rows but its "
            f"chunks hold {rows_present}. Refusing to serve a row count this "
            f"file does not contain.",
            status=500)

    body = render_header(job["column_order"]) + "".join(b for _n, b, _r in chunks)
    digest = sha256_text(body)
    if digest != job["result_sha256"]:
        raise ExportError(
            "EXPORT_RESULT_DIGEST_MISMATCH",
            f"export {export_job_id}'s bytes no longer match the digest recorded "
            f"when it completed. Not served.",
            status=500)
    return body, public_job(job)["result"] | {"export_job_id": job["export_job_id"]}


# ------------------------------------------------------------------ control
def _finish(session: Session, job: Mapping[str, Any], state: str, *,
            now: datetime, error_code: str | None = None,
            error_detail: str | None = None) -> dict[str, Any]:
    row = repo.query_one(
        session,
        f"""
        UPDATE export_job j
        SET state = %(state)s, finished_at = %(now)s,
            error_code = %(error_code)s, error_detail = %(error_detail)s
        WHERE j.export_job_id = %(export_job_id)s AND {{scope}}
        RETURNING {_JOB_SELECT.replace('j.', '')}
        """,
        {"state": state, "now": now, "error_code": error_code,
         "error_detail": error_detail,
         "export_job_id": job["export_job_id"]},
        columns=JOB_SCOPE_COLUMNS)
    if row is None:                                    # pragma: no cover
        raise ExportError("EXPORT_JOB_NOT_FOUND",
                          f"export job {job['export_job_id']} vanished mid-update",
                          status=500)
    return _job_row_to_dict(row)


def _purge_chunks(session: Session, export_job_id: str, *,
                  scope: Scope | None = None) -> int:
    """Delete a job's rendered chunks, THROUGH the scope predicate.

    A bare `DELETE FROM export_job_chunk WHERE export_job_id = ...` carries no
    `{scope}` token, so `repo.query` would refuse it -- correctly. The EXISTS
    below puts the token where it actually decides something: the chunks go
    only if the caller can see the job they belong to.
    """
    return len(repo.query(
        session,
        """
        DELETE FROM export_job_chunk c
        WHERE c.export_job_id = %(id)s
          AND EXISTS (SELECT 1 FROM export_job j
                      WHERE j.export_job_id = c.export_job_id AND {scope})
        RETURNING c.chunk_no
        """,
        {"id": str(export_job_id)}, scope=scope, columns=JOB_SCOPE_COLUMNS))


def request_cancel(session: Session, export_job_id: str, *, requester: str,
                   actor: str, now: datetime | None = None) -> dict[str, Any]:
    """Ask a job to stop. Honoured at the next chunk boundary.

    A QUEUED job is cancelled outright -- nothing has run. A RUNNING job has
    `cancel_requested` set and stops after its current chunk commits: killing
    it mid-chunk would leave the committed prefix and the counters disagreeing,
    and the counters are what `read_result` verifies the file against.
    """
    at = _now(now)
    job = get_job(session, export_job_id, requester=requester)
    if job is None:
        raise ExportError("EXPORT_JOB_NOT_FOUND",
                          f"no export job {export_job_id!r}", status=404)
    if job["state"] in TERMINAL_STATES:
        raise ExportError(
            "EXPORT_ALREADY_FINISHED",
            f"export {export_job_id} is already {job['state']}",
            status=409, detail={"state": job["state"]})

    if job["state"] == STATE_QUEUED:
        updated = _finish(session, job, STATE_CANCELLED, now=at,
                          error_code="CANCELLED_BY_REQUESTER",
                          error_detail=f"cancelled by {actor} before it started")
    else:
        row = repo.query_one(
            session,
            f"""
            UPDATE export_job j SET cancel_requested = true
            WHERE j.export_job_id = %(export_job_id)s AND {{scope}}
            RETURNING {_JOB_SELECT.replace('j.', '')}
            """,
            {"export_job_id": export_job_id}, columns=JOB_SCOPE_COLUMNS)
        updated = _job_row_to_dict(row)

    audit_svc.append(session, actor=actor, action="export.cancelled",
                     object_type="export_job", object_id=export_job_id,
                     detail=json.dumps({"state": updated["state"],
                                        "cancel_requested": True}),
                     correlation_id=job.get("correlation_id"))
    return updated


def retry_job(session: Session, export_job_id: str, *, requester: str,
              actor: str, now: datetime | None = None) -> dict[str, Any]:
    """Re-queue a FAILED job, from scratch.

    Every committed chunk is DISCARDED. A retry that resumed from a partial
    file would splice rows produced under one reading of the schema onto rows
    produced under another, and the resulting file would be self-consistent and
    wrong. The captured scope is untouched -- it is immutable, and re-capturing
    it would silently re-authorise the job against grants that may since have
    widened.
    """
    at = _now(now)
    job = get_job(session, export_job_id, requester=requester)
    if job is None:
        raise ExportError("EXPORT_JOB_NOT_FOUND",
                          f"no export job {export_job_id!r}", status=404)
    if job["state"] != STATE_FAILED:
        raise ExportError(
            "EXPORT_NOT_RETRYABLE",
            f"export {export_job_id} is {job['state']}; only a FAILED export "
            f"can be retried.",
            status=409, detail={"state": job["state"]})
    if job["attempt"] >= job["max_attempts"]:
        raise ExportError(
            "EXPORT_ATTEMPTS_EXHAUSTED",
            f"export {export_job_id} has used all {job['max_attempts']} "
            f"attempts. Request a new export rather than retrying this one.",
            status=409)

    _purge_chunks(session, export_job_id)
    row = repo.query_one(
        session,
        f"""
        UPDATE export_job j
        SET state = 'QUEUED', finished_at = NULL, started_at = NULL,
            error_code = NULL, error_detail = NULL,
            rows_written = 0, rows_total = NULL, chunks_written = 0,
            resume_key = NULL, cancel_requested = false,
            result_sha256 = NULL, result_bytes = NULL,
            result_filename = NULL, result_media_type = NULL
        WHERE j.export_job_id = %(export_job_id)s AND {{scope}}
        RETURNING {_JOB_SELECT.replace('j.', '')}
        """,
        {"export_job_id": export_job_id}, columns=JOB_SCOPE_COLUMNS)
    updated = _job_row_to_dict(row)
    audit_svc.append(session, actor=actor, action="export.retried",
                     object_type="export_job", object_id=export_job_id,
                     detail=json.dumps({"attempt": updated["attempt"],
                                        "max_attempts": updated["max_attempts"]}),
                     correlation_id=job.get("correlation_id"))
    return updated


def expire_due(database: Database, *, now: datetime | None = None,
               limit: int = 100) -> list[str]:
    """Purge the rendered rows of every SUCCEEDED export past its expiry.

    The CHUNKS go; the JOB ROW STAYS, moved to EXPIRED. Who exported what,
    under which scope and when, is the record that matters after the bytes are
    gone, and 017 revokes DELETE on `export_job` so it cannot be otherwise.

    Runs in the worker's own DENIED session with the narrow
    :func:`job_registry_scope` override on the three statements that touch the
    export tables -- the same arrangement, and for the same reason, as
    :func:`_claim_candidates`. Expiry is estate-wide; it is not any one
    requester's action and must not need one of them to be signed in.
    """
    at = _now(now)
    registry = job_registry_scope()
    expired: list[str] = []
    with database.session(service_scope()) as session:
        rows = repo.query(
            session,
            """
            SELECT j.export_job_id FROM export_job j
            WHERE j.state = 'SUCCEEDED' AND j.expires_at <= %(now)s AND {scope}
            ORDER BY j.expires_at
            LIMIT %(limit)s
            """,
            {"now": at, "limit": int(limit)},
            scope=registry, columns=JOB_SCOPE_COLUMNS)
        for (job_id,) in rows:
            _purge_chunks(session, job_id, scope=registry)
            repo.query(
                session,
                """
                UPDATE export_job j
                SET state = 'EXPIRED', finished_at = %(now)s,
                    error_code = 'EXPIRED',
                    error_detail = 'the rendered rows were purged at expiry',
                    result_sha256 = NULL, result_bytes = NULL,
                    result_filename = NULL, result_media_type = NULL
                WHERE j.export_job_id = %(id)s AND {scope}
                RETURNING j.export_job_id
                """,
                {"id": job_id, "now": at},
                scope=registry, columns=JOB_SCOPE_COLUMNS)
            audit_svc.append(
                session, actor=SERVICE_USER_ID, action="export.expired",
                object_type="export_job", object_id=job_id,
                detail=json.dumps({"expired_at": at.isoformat()}))
            expired.append(job_id)
    return expired


# ===========================================================================
# The worker
# ===========================================================================
def rehydrate_scope(database: Database, job: Mapping[str, Any]) -> Scope:
    """The scope one chunk of `job` will run under. NEVER the worker's own.

    1. Verify the digest. A mismatch raises before anything is read.
    2. Deserialise the captured scope, three states intact.
    3. MEET it with the requester's currently-resolved grants.

    Step 3 is what makes step 1's unkeyed digest sufficient. It is also the
    ordinary path: grants change, and a job queued yesterday must not export
    what its requester could see yesterday but cannot see today.
    """
    doc = job["scope_json"]
    if isinstance(doc, str):                           # some drivers hand back text
        doc = json.loads(doc)
    verify_scope_digest(job["export_job_id"], doc, job["scope_digest"])
    captured = deserialise_scope(doc)

    live = principal_scope.scope_for_request(
        database, {"user_id": job["requested_by"]},
        principal_kind=job.get("requested_principal_kind") or "USER")
    return meet_scopes(captured, live)


def _claim_candidates(database: Database, *, now: datetime, limit: int
                      ) -> list[dict[str, Any]]:
    """Job rows the worker should look at, read under the SERVICE scope.

    This is the ONLY thing SVC-EXPORT is used for, and it reads exactly one
    table. Its scope has every dimension `frozenset()`, so any business query
    issued from this session would compile to FALSE -- there is no query here
    to make that a near miss.
    """
    with database.session(service_scope()) as session:
        rows = repo.query(
            session,
            f"""
            SELECT {_JOB_SELECT}
            FROM export_job j
            WHERE j.state IN ('QUEUED', 'RUNNING')
              AND j.expires_at > %(now)s
              AND {{scope}}
            ORDER BY j.created_at
            LIMIT %(limit)s
            """,
            {"now": now, "limit": int(limit)},
            scope=job_registry_scope(), columns=JOB_SCOPE_COLUMNS)
        return [_job_row_to_dict(r) for r in rows]


def _fail_under_service_scope(database: Database, job_id: str, code: str,
                              detail: str, *, now: datetime) -> None:
    """Mark a job FAILED when the requester's own scope cannot even read the
    job row -- which is exactly the `REQUESTER_SCOPE_DENIED` case."""
    with database.session(service_scope()) as session:
        repo.query(
            session,
            """
            UPDATE export_job j
            SET state = 'FAILED', finished_at = %(now)s,
                error_code = %(code)s, error_detail = %(detail)s
            WHERE j.export_job_id = %(id)s AND j.state IN ('QUEUED', 'RUNNING')
              AND {scope}
            RETURNING j.export_job_id
            """,
            {"id": job_id, "now": now, "code": code, "detail": detail},
            scope=job_registry_scope(), columns=JOB_SCOPE_COLUMNS)
        audit_svc.append(
            session, actor=SERVICE_USER_ID, action="export.failed",
            object_type="export_job", object_id=job_id,
            detail=json.dumps({"code": code, "detail": detail}))


def advance_job(database: Database, export_job_id: str, *,
                now: datetime | None = None,
                rows_budget: int = DEFAULT_ROWS_PER_INVOCATION,
                job: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Do ONE invocation's worth of work on a job, then commit and return.

    THE SESSION IS OPENED AS THE REQUESTER. That single line is the whole
    security property: `SET LOCAL capex.*` carries the requester's dimensions,
    so RLS filters as them; `repo.query` compiles the same scope, so the
    predicate filters as them; and `capex.user_id` is theirs, so migration
    017's `export_job_owner` policy admits this job and no other principal's.

    Bounded by `rows_budget`, not by a clock. A row budget is a property of the
    work; a timer is a race with the platform's own 30-second kill, and a
    worker killed mid-transaction commits nothing while a worker that stopped
    at a chunk boundary has committed everything up to it.
    """
    at = _now(now)

    # --- who this job runs as, decided before anything is read -----------
    if job is None:
        with database.session(service_scope()) as session:
            row = repo.query_one(
                session,
                f"SELECT {_JOB_SELECT} FROM export_job j "
                f"WHERE j.export_job_id = %(id)s AND {{scope}}",
                {"id": str(export_job_id)},
                scope=job_registry_scope(), columns=JOB_SCOPE_COLUMNS)
        if row is None:
            raise ExportError("EXPORT_JOB_NOT_FOUND",
                              f"no export job {export_job_id!r}", status=404)
        job = _job_row_to_dict(row)

    try:
        scope = rehydrate_scope(database, job)
    except ExportError as exc:
        _fail_under_service_scope(database, job["export_job_id"], exc.code,
                                  exc.message, now=at)
        raise

    if principal_scope.is_denied(scope):
        # A permission state, reported as one. NOT an empty export -- see the
        # module docstring on the three states.
        detail = (f"the requester {job['requested_by']!r} now resolves to a "
                  f"scope that permits no rows. An export of nothing is not "
                  f"the same statement as an export you may not have.")
        _fail_under_service_scope(database, job["export_job_id"],
                                  "REQUESTER_SCOPE_DENIED", detail, now=at)
        return {"export_job_id": job["export_job_id"], "state": STATE_FAILED,
                "error_code": "REQUESTER_SCOPE_DENIED", "rows_written_now": 0}

    dataset = get_dataset(job["dataset"])
    filters = dict(job["filter_json"] or {})
    if isinstance(job["filter_json"], str):            # pragma: no cover
        filters = json.loads(job["filter_json"])

    with database.session(scope) as session:
        session.execute("SET LOCAL statement_timeout = '25s'")
        return _advance_within_session(
            session, dataset=dataset, job_id=job["export_job_id"],
            filters=filters, now=at, rows_budget=rows_budget)


def _advance_within_session(session: Session, *, dataset: Dataset, job_id: str,
                            filters: Mapping[str, Any], now: datetime,
                            rows_budget: int) -> dict[str, Any]:
    """The chunk loop, inside a transaction already scoped to the requester."""
    # Re-read under FOR UPDATE: `advance_job` may be racing another invocation
    # or the requester's own poll, and two workers writing chunk N is the one
    # way this design can produce a corrupt file.
    row = repo.query_one(
        session,
        f"SELECT {_JOB_SELECT} FROM export_job j "
        f"WHERE j.export_job_id = %(id)s AND {{scope}} FOR UPDATE",
        {"id": job_id}, columns=JOB_SCOPE_COLUMNS)
    if row is None:
        raise ExportError(
            "EXPORT_JOB_NOT_FOUND",
            f"export job {job_id!r} is not visible under its own requester's "
            f"scope. It is not exported.",
            status=404)
    job = _job_row_to_dict(row)

    if job["state"] in TERMINAL_STATES:
        return {"export_job_id": job_id, "state": job["state"],
                "rows_written_now": 0, "note": "already finished"}
    if job["cancel_requested"]:
        _finish(session, job, STATE_CANCELLED, now=now,
                error_code="CANCELLED_BY_REQUESTER",
                error_detail="cancelled at a chunk boundary")
        return {"export_job_id": job_id, "state": STATE_CANCELLED,
                "rows_written_now": 0}

    if job["state"] == STATE_QUEUED:
        repo.query(
            session,
            """
            UPDATE export_job j
            SET state = 'RUNNING', started_at = COALESCE(j.started_at, %(now)s),
                attempt = j.attempt + 1
            WHERE j.export_job_id = %(id)s AND {scope}
            RETURNING j.export_job_id
            """,
            {"id": job_id, "now": now}, columns=JOB_SCOPE_COLUMNS)
        job["state"] = STATE_RUNNING
        job["attempt"] = int(job["attempt"]) + 1

    conditions, params = plan_filters(dataset, filters)
    resume = job["resume_key"]
    if isinstance(resume, str):                        # pragma: no cover
        resume = json.loads(resume)

    chunk_rows = int(job["chunk_rows"])
    chunk_no = int(job["chunks_written"])
    rows_written = int(job["rows_written"])
    written_now = 0
    exhausted = False

    while written_now < rows_budget:
        want = min(chunk_rows, rows_budget - written_now)
        page, resume = _read_page(session, dataset, conditions, params,
                                  resume=resume, limit=want)
        if not page:
            exhausted = True
            break
        chunk_no += 1
        # The page carries the dataset's own columns followed by its key
        # expressions; only the former are rendered. Slicing here rather than
        # in `render_rows` keeps that function's column-count assertion
        # meaningful.
        width = len(dataset.columns)
        body = render_rows(dataset, [row[:width] for row in page])
        first_key = _key_of(dataset, page[0])
        last_key = _key_of(dataset, page[-1])
        # INSERT ... SELECT ... WHERE {scope}: the chunk is written only if the
        # job it belongs to is visible under the scope this session is running
        # as -- which is the requester's, not the worker's.
        written = repo.query(
            session,
            """
            INSERT INTO export_job_chunk
                (export_job_id, chunk_no, row_count, byte_count, sha256, body,
                 first_key, last_key, written_at)
            SELECT %(id)s, %(chunk_no)s, %(row_count)s, %(byte_count)s,
                   %(sha256)s, %(body)s, %(first_key)s, %(last_key)s, %(now)s
            FROM export_job j
            WHERE j.export_job_id = %(id)s AND {scope}
            RETURNING chunk_no
            """,
            {"id": job_id, "chunk_no": chunk_no, "row_count": len(page),
             "byte_count": len(body.encode("utf-8")),
             "sha256": sha256_text(body), "body": body,
             "first_key": Jsonb(first_key),
             "last_key": Jsonb(last_key), "now": now},
            columns=JOB_SCOPE_COLUMNS)
        if not written:                                # pragma: no cover
            raise ExportError(
                "EXPORT_CHUNK_NOT_WRITTEN",
                f"chunk {chunk_no} of export {job_id} was not written: the job "
                f"row is not visible under its own requester's scope.",
                status=500)
        rows_written += len(page)
        written_now += len(page)
        if len(page) < want:
            exhausted = True
            break

    repo.query(
        session,
        """
        UPDATE export_job j
        SET rows_written = %(rows_written)s, chunks_written = %(chunks)s,
            resume_key = %(resume)s
        WHERE j.export_job_id = %(id)s AND {scope}
        RETURNING j.export_job_id
        """,
        {"id": job_id, "rows_written": rows_written, "chunks": chunk_no,
         "resume": Jsonb(resume) if resume is not None else None},
        columns=JOB_SCOPE_COLUMNS)

    if not exhausted:
        return {"export_job_id": job_id, "state": STATE_RUNNING,
                "rows_written_now": written_now, "rows_written": rows_written,
                "chunks_written": chunk_no, "more": True}

    # --- completion: the total is COUNTED, never estimated ---------------
    body = render_header(list(job["column_order"]))
    chunks = repo.query(
        session,
        """
        SELECT c.chunk_no, c.body, c.row_count FROM export_job_chunk c
        WHERE c.export_job_id = %(id)s
          AND EXISTS (SELECT 1 FROM export_job j
                      WHERE j.export_job_id = c.export_job_id AND {scope})
        ORDER BY c.chunk_no
        """,
        {"id": job_id}, columns=JOB_SCOPE_COLUMNS)
    if [n for n, _b, _r in chunks] != list(range(1, chunk_no + 1)):
        raise ExportError(
            "EXPORT_RESULT_INCOMPLETE",
            f"export {job_id} wrote {chunk_no} chunks but they are not "
            f"contiguous; refusing to record it as complete.", status=500)
    counted = sum(int(r) for _n, _b, r in chunks)
    if counted != rows_written:
        raise ExportError(
            "EXPORT_ROW_COUNT_DISAGREEMENT",
            f"export {job_id} counted {rows_written} rows while its chunks hold "
            f"{counted}. Refusing to publish a total this file does not "
            f"contain.", status=500)
    body += "".join(b for _n, b, _r in chunks)

    filename = f"{dataset.name}-{job_id}.csv"
    repo.query(
        session,
        """
        UPDATE export_job j
        SET state = 'SUCCEEDED', finished_at = %(now)s,
            rows_total = %(rows_total)s, result_sha256 = %(sha)s,
            result_bytes = %(bytes)s, result_filename = %(filename)s,
            result_media_type = 'text/csv; charset=utf-8'
        WHERE j.export_job_id = %(id)s AND {scope}
        RETURNING j.export_job_id
        """,
        {"id": job_id, "now": now, "rows_total": counted,
         "sha": sha256_text(body), "bytes": len(body.encode("utf-8")),
         "filename": filename},
        columns=JOB_SCOPE_COLUMNS)

    audit_svc.append(
        session, actor=job["requested_by"], action="export.completed",
        object_type="export_job", object_id=job_id,
        detail=json.dumps({
            "dataset": dataset.name, "rows": counted, "chunks": chunk_no,
            "bytes": len(body.encode("utf-8")), "sha256": sha256_text(body),
            "ran_under_scope_of": job["requested_by"],
        }, sort_keys=True),
        correlation_id=job.get("correlation_id"))

    return {"export_job_id": job_id, "state": STATE_SUCCEEDED,
            "rows_written_now": written_now, "rows_written": rows_written,
            "rows_total": counted, "chunks_written": chunk_no, "more": False}


def _key_of(dataset: Dataset, row: Sequence[Any]) -> list[str]:
    """The keyset value of a row, read from the trailing key columns the page
    query appends after the dataset's own."""
    n = len(dataset.key_sql)
    return [None if v is None else str(v) for v in row[len(dataset.columns):
                                                       len(dataset.columns) + n]]


def _read_page(session: Session, dataset: Dataset, conditions: Sequence[str],
               params: Mapping[str, Any], *, resume: Sequence[Any] | None,
               limit: int) -> tuple[list[tuple], list[str] | None]:
    """One page of the dataset, KEYSET-paginated, through `repo.query`.

    Keyset, never OFFSET. An OFFSET over a table anything else is writing skips
    and duplicates rows across chunk boundaries, and a financial export that
    silently drops a purchase order line is worse than one that fails.

    The dataset's key expressions are SELECTed after its own columns so the
    checkpoint is read from the row rather than parsed back out of it.
    """
    where = list(conditions)
    query_params = dict(params)
    query_params["page_limit"] = int(limit)
    if resume:
        placeholders = ", ".join(
            f"%(resume_{i})s" for i in range(len(dataset.key_sql)))
        where.append(f"(({', '.join(dataset.key_sql)}) > ({placeholders}))")
        for i, value in enumerate(resume):
            query_params[f"resume_{i}"] = value

    select = ", ".join([c.sql for c in dataset.columns] + list(dataset.key_sql))
    statement = f"""
        SELECT {select}
        {dataset.from_sql}
        WHERE {' AND '.join(where) if where else 'TRUE'} AND {{scope}}
        ORDER BY {', '.join(dataset.key_sql)}
        LIMIT %(page_limit)s
    """
    rows = repo.query(session, statement, query_params,
                      columns=dataset.scope_columns)
    if not rows:
        return [], resume if resume is None else list(resume)
    return rows, _key_of(dataset, rows[-1])


def run_due_jobs(database: Database, *, now: datetime | None = None,
                 max_jobs: int = 5,
                 rows_budget: int = DEFAULT_ROWS_PER_INVOCATION
                 ) -> list[dict[str, Any]]:
    """One scheduled invocation: advance up to `max_jobs` due exports.

    There is deliberately NO HTTP route that calls this. A route that advanced
    an arbitrary job would let any authenticated caller drive somebody else's
    export, and while it could not read their rows -- the job runs under its
    own requester's scope -- it would let them consume the budget and observe
    the timing. The requester drives their OWN job through
    `/api/exports/{id}/advance`, which checks ownership; the estate-wide sweep
    is this function, invoked by the scheduled Function.
    """
    at = _now(now)
    results: list[dict[str, Any]] = []
    for job in _claim_candidates(database, now=at, limit=max_jobs):
        try:
            results.append(advance_job(database, job["export_job_id"], now=at,
                                       rows_budget=rows_budget, job=job))
        except ExportError as exc:
            log.warning("export %s refused: %s", job["export_job_id"], exc.code)
            results.append({"export_job_id": job["export_job_id"],
                            "state": STATE_FAILED, "error_code": exc.code})
    return results
