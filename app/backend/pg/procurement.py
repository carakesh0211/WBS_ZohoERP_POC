"""The inbound half of the procurement chain: receives and vendor bills.

`013_procurement.sql` finally created `purchase_order`, `po_line`, `grn`,
`grn_line`, `bill` and `bill_line`. This module is what writes to them from
outside: it mirrors a goods receipt and a vendor bill into PostgreSQL,
attributes every line to a known `po_line` and therefore to a control cell,
and quarantines whatever it cannot attribute.

WHY THIS IS NOT IN `integration_store.py`
-----------------------------------------
`tests/test_integration_store.py::test_money_in_this_module_appears_only_on_the_011_exception_table`
states the boundary in one sentence -- "the ledger owns money and this module
owns transport" -- and enforces it: no statement in the integration store may
name a `*_paise` column except against `reconciliation_exception`. `grn_line`
and `bill_line` are money-bearing ledger rows, so their SQL belongs here.
`integration_store.resolve_po_line` and `.record_receive_line` remain as the
`sweeps.SweepStore` surface and delegate across that line rather than blurring
it.

THE RULE THAT GOVERNS EVERYTHING HERE (§11.8)
---------------------------------------------
**Zero pro-rata spreading. Zero silent drops.** A line whose linkage does not
resolve to a known `po_line` is never apportioned, never guessed at and never
dropped. It is quarantined at FULL value in a reconciliation exception that
blocks capitalisation and blocks accounting-period close. `sweeps.py` says it
plainly: "Every receive line is attributed to a known `po_line` or it is
quarantined. There is no third branch." Nothing below adds one.

FIVE THINGS THAT ARE EASY TO GET WRONG HERE, AND WHAT STOPS EACH
----------------------------------------------------------------
1. **Replay.** The sweeps re-walk by design -- a 300-second overlap and a
   cycling cursor -- so every document arrives more than once. Receive lines
   are made idempotent by `ux_grn_line_external`; bill lines have no external
   unique index at all in 013, so their surrogate ids are DERIVED from the
   source keys (:func:`derived_id`) and the conflict target is the primary
   key. A random id would turn every re-walk into a duplicate line, and
   `capex_app` has no DELETE with which to clean up after it.

2. **Signs.** `grn_line.quantity`, `grn_line.amount_paise` and
   `bill_line.amount_paise` are all SIGNED and carry no `>= 0` CHECK, because
   returns, reversals, credit notes and debit notes are ordinary documents.
   Nothing here takes an absolute value of a document amount, and nothing here
   renders paise as rupees -- `divmod` FLOORS, which is how ``-150`` once
   reached the wire as ``-2.50``, and the only rendering in the whole package
   is on the way OUT, in the two adapters.

3. **Over-billing.** It is not clamped, not netted and not warned about at
   write time. It is made VISIBLE: :func:`reconcile_po_lines` reports ordered,
   received and billed side by side per PO line, and the difference is left
   where finance can see it.

4. **Status.** A raw external status is stored VERBATIM (in
   `integration_inbox.external_status_raw`, which the poll writes) and is never
   overwritten. A value C17 cannot interpret raises `UNMAPPED_EXTERNAL_STATUS`
   and the document is accepted as NOT accounting-effective -- see
   :func:`resolve_accounting_status`, which refuses to choose.

5. **Scope.** Every statement goes through `repo.query` with a literal
   ``{scope}`` token and a `columns=` mapping naming all four dimensions. 013's
   own RLS policies call `capex_scope_permits(p.entity_id, p.plant_id,
   p.location_id, p.project_id)`; an application layer that waived a dimension
   would be weaker than the policy it mirrors.

Nothing here opens a socket, names an endpoint or knows what a Zoho response
looks like. Every test drives it with fakes.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from . import integration_store as store
from . import repo
from .engine import Session

# =============================================================== table names
#: 013's document chain. Re-exported from :mod:`integration_store` so there is
#: one spelling of each and one place that says which migration owns them.
PURCHASE_ORDER = store.PURCHASE_ORDER
PO_LINE = store.PO_LINE
GRN = store.GRN
GRN_LINE = store.GRN_LINE
BILL = store.BILL
BILL_LINE = store.BILL_LINE

#: All four dimensions, none waived. See the module docstring, point 5.
SCOPE_COLUMNS = store.PROCUREMENT_SCOPE_COLUMNS

#: `ck_bill_doc_type`'s three values. A credit note and a debit note are
#: ordinary documents here, not exceptions to be special-cased.
DOC_TYPES: tuple[str, ...] = ("BILL", "CREDIT_NOTE", "DEBIT_NOTE")

#: `ck_bill_accounting_status`'s four. AUD-C-004: only `Approved` and
#: `Reversal` are accounting-effective.
ACCOUNTING_STATUSES: tuple[str, ...] = ("Draft", "Approved", "Void", "Reversal")
ACCOUNTING_EFFECTIVE: frozenset[str] = frozenset({"Approved", "Reversal"})

#: Where a document whose external status we cannot interpret lands.
#:
#: NOT a plausible default and not a guess about the vendor's intent: it is the
#: only value in `ck_bill_accounting_status` that is NOT accounting-effective
#: and is not itself a claim (`Void` claims the document was cancelled;
#: `Reversal` claims it reverses another). C17 requires the record to be
#: ACCEPTED with its raw value preserved and an `UNMAPPED_EXTERNAL_STATUS`
#: raised -- accepted, but not accounting-effective until a human triages it.
UNINTERPRETABLE_ACCOUNTING_STATUS = "Draft"


class ProcurementIngestError(store.IntegrationStoreError):
    """A receive or bill could not be mirrored, and nothing was written.

    A distinct type so a caller can tell "this document is wrong" from "this
    principal may not see it" without string-matching a code.
    """


# ================================================================== helpers
def derived_id(prefix: str, *parts: str) -> str:
    """A stable surrogate id for a mirrored row, derived from its source keys.

    DETERMINISTIC, not random, and that is the whole requirement. A replayed
    receive must produce the SAME `grn_id` and the same `grn_number`, or
    `ux_grn_number` turns an idempotent re-walk into a unique violation at 3am.
    A replayed bill must produce the same `bill_line_id`, because `bill_line`
    has no external unique index in 013 and the primary key is therefore the
    only conflict target available -- and because `capex_app` has DELETE
    revoked, so a duplicate line cannot be tidied away afterwards.

    `uuid4` is right for `integration_store._exception_id`, where the caller is
    a cron sweep with nothing to name a row after. It is wrong here, where the
    source document's own identifiers are exactly what the row should be named
    after.

    The separator is US (0x1f), a character no Zoho identifier contains, so
    ``("A", "BC")`` and ``("AB", "C")`` cannot hash alike.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20].upper()}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _line_field(line: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(line, Mapping):
            if name in line and line[name] is not None:
                return line[name]
        elif getattr(line, name, None) is not None:
            return getattr(line, name)
    return default


# ============================================================ po_line lookup
def resolve_po_line(session: Session, *, po_external_id: str,
                    line_external_id: str | None) -> str | None:
    """The local `po_line_id` a line's external linkage names, or `None`.

    See `integration_store.resolve_po_line`, the `sweeps.SweepStore` surface
    this backs, for why `None` is a real answer and why ambiguity is not.
    """
    if not _text(line_external_id) or not _text(po_external_id):
        return None
    rows = repo.query(
        session,
        f"""
        SELECT DISTINCT pl.po_line_id
        FROM {PO_LINE} pl
        JOIN {PURCHASE_ORDER} po ON po.po_id = pl.po_id
        JOIN project p ON p.project_id = pl.project_id
        WHERE po.external_id = %(po_external_id)s
          AND pl.line_external_id = %(line_external_id)s
          AND {{scope}}
        """,
        {"po_external_id": str(po_external_id).strip(),
         "line_external_id": str(line_external_id).strip()},
        columns=SCOPE_COLUMNS,
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise ProcurementIngestError(
            "AMBIGUOUS_PO_LINE_LINKAGE",
            f"External line {line_external_id!r} on external purchase order "
            f"{po_external_id!r} resolves to {len(rows)} local po_line rows "
            f"({', '.join(sorted(r[0] for r in rows))}). Two purchase orders "
            f"carrying one external id under different external_source values "
            f"is permitted by ux_po_external; guessing which one this line "
            f"belongs to is not. Nothing was attributed and nothing was "
            f"quarantined -- quarantining would file this as an ABSENT "
            f"linkage, which it is not.",
            status=409)
    return rows[0][0]


def _po_line_cell(session: Session, po_line_id: str) -> tuple[str, str, str, str]:
    """`(po_id, project_id, wbs_id, budget_head_id)` for one PO line, scoped.

    The control cell is read from the PO LINE, never accepted from the caller.
    `fk_bill_line_po_line_cell` is a four-column FK precisely because a bill
    line's cell must BE its PO line's cell; supplying it separately would mean
    the FK is the only thing standing between a mis-supplied `wbs_id` and a
    posting to another project's budget, and a constraint violation at 3am is a
    worse report than a lookup here.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT pl.po_id, pl.project_id, pl.wbs_id, pl.budget_head_id
        FROM {PO_LINE} pl
        JOIN project p ON p.project_id = pl.project_id
        WHERE pl.po_line_id = %(po_line_id)s AND {{scope}}
        """,
        {"po_line_id": po_line_id},
        columns=SCOPE_COLUMNS,
    )
    if row is None:
        raise ProcurementIngestError(
            "PO_LINE_NOT_FOUND",
            f"po_line {po_line_id!r} does not exist or is out of scope for "
            f"this principal. Nothing was written: a document line attributed "
            f"to a control cell nobody can name is not attributed at all.",
            status=404)
    return row[0], row[1], row[2], row[3]


def _purchase_order(session: Session,
                    po_external_id: str) -> tuple[str, str, str]:
    """`(po_id, project_id, entity_id)` for one external PO, scoped.

    `entity_id` is READ here rather than taken from the caller, and that is not
    convenience. A reconciliation exception raised with `project_id` set but
    `entity_id` NULL is visible to a project-restricted principal and INVISIBLE
    to an entity-restricted one -- `open_exceptions` and
    `accumulate_unattributed` both compile `entity_id = ANY(...)`, and
    ``NULL = ANY(...)`` is NULL, which is falsy. The row would sit in the
    bucket unreachable by the people who triage it.
    """
    rows = repo.query(
        session,
        f"""
        SELECT po.po_id, po.project_id, p.entity_id
        FROM {PURCHASE_ORDER} po
        JOIN project p ON p.project_id = po.project_id
        WHERE po.external_id = %(external_id)s AND {{scope}}
        """,
        {"external_id": str(po_external_id).strip()},
        columns=SCOPE_COLUMNS,
    )
    if not rows:
        raise ProcurementIngestError(
            "PURCHASE_ORDER_NOT_FOUND",
            f"No local purchase order carries external id "
            f"{po_external_id!r}, or it is out of scope. Nothing was written. "
            f"A bill against a purchase order this system never sanctioned is "
            f"what `poll_purchaseorders` raises UNSANCTIONED_COMMITMENT for; "
            f"mirroring it here under an invented header would hide that.",
            status=404)
    if len(rows) > 1:
        raise ProcurementIngestError(
            "AMBIGUOUS_PURCHASE_ORDER",
            f"External purchase order {po_external_id!r} resolves to "
            f"{len(rows)} local rows. ux_po_external is UNIQUE on "
            f"(external_source, external_id), so one id under two sources is "
            f"legal and choosing between them is not.",
            status=409)
    return rows[0][0], rows[0][1], rows[0][2]


def _project_entity(session: Session, project_id: str) -> str:
    """The entity a project belongs to, scoped. See :func:`_purchase_order`."""
    row = repo.query_one(
        session,
        """
        SELECT p.entity_id FROM project p
        WHERE p.project_id = %(project_id)s AND {scope}
        """,
        {"project_id": project_id}, columns=SCOPE_COLUMNS)
    if row is None:
        raise ProcurementIngestError(
            "BILL_PROJECT_NOT_FOUND",
            f"Project {project_id!r} does not exist or is out of scope for "
            f"this principal. Nothing was written.", status=404)
    return row[0]


# ==================================================== the goods receipt (GRN)
def record_receive_line(session: Session, *, po_line_id: str,
                        receive_external_id: str,
                        line_external_id: str | None,
                        quantity: Any,
                        amount_paise: int | None,
                        external_source: str | None = None,
                        receive_number: str | None = None,
                        received_at: datetime | None = None,
                        external_last_modified: datetime | None = None,
                        payload_sha: str | None = None,
                        is_reversal: bool = False,
                        actor: str = "SVC-SWEEP",
                        now: datetime | None = None) -> None:
    """Mirror one attributed receive line, header included, idempotently.

    See `integration_store.record_receive_line`, the `sweeps.SweepStore`
    surface this backs, for the contract and for why the provenance arguments
    after `amount_paise` are optional but not decorative.
    """
    moment = now or store._utcnow()
    observed = received_at or moment
    receive_external_id = store._require(
        receive_external_id, code="BLANK_RECEIVE_EXTERNAL_ID",
        what="receive_external_id")
    po_line_id = store._require(po_line_id, code="BLANK_PO_LINE_ID",
                                what="po_line_id")

    # NEITHER IS DEFAULTED. `grn_line.quantity` and `.amount_paise` are both
    # NOT NULL, and the two available defaults are both claims about a
    # document: zero quantity says "nothing arrived" and zero paise says "the
    # goods were free". A source line carrying neither is a genuine surprise
    # and stops the sweep at the row it cannot honour, where it is visible.
    # `sweeps._attribute` coalesces `amount_paise` to 0 itself, so nothing
    # reaching here through the sweep can trip the second of these.
    if quantity is None or str(quantity).strip() == "":
        raise ProcurementIngestError(
            "RECEIVE_LINE_QUANTITY_MISSING",
            f"Receive {receive_external_id!r} line {line_external_id!r} on "
            f"po_line {po_line_id!r} carries no quantity. Nothing was "
            f"recorded: defaulting it to zero would state that nothing "
            f"arrived, which is a claim about a goods receipt and not a "
            f"missing-value convention.", status=422)
    if amount_paise is None:
        raise ProcurementIngestError(
            "RECEIVE_LINE_AMOUNT_MISSING",
            f"Receive {receive_external_id!r} line {line_external_id!r} on "
            f"po_line {po_line_id!r} carries no amount. Nothing was recorded: "
            f"zero paise states the goods were free, which is not an absence.",
            status=422)

    # `po_id` comes from the PO LINE, never from the caller. It is the column
    # `fk_grn_line_po_line` and `fk_grn_line_grn_po` must BOTH agree with, and
    # taking it from anywhere else is precisely the hole the trigger
    # `grn_line_po_ownership` existed to plug.
    po_id, _project_id, _wbs_id, _head_id = _po_line_cell(session, po_line_id)
    grn_id = _mirror_grn_header(
        session, po_id=po_id, receive_external_id=receive_external_id,
        external_source=external_source, receive_number=receive_number,
        received_at=observed, external_last_modified=external_last_modified,
        payload_sha=payload_sha, is_reversal=is_reversal, actor=actor)

    params = {
        "grn_line_id": derived_id("GRNL", po_line_id, receive_external_id,
                                  line_external_id or ""),
        "grn_id": grn_id, "po_id": po_id, "po_line_id": po_line_id,
        "receive_external_id": receive_external_id,
        "line_external_id": _text(line_external_id),
        # `quantity` is `numeric` and the DTO carries an exact decimal STRING.
        # Passed through as one: float() here would reintroduce the binary
        # rounding `dto.quantity` exists to refuse.
        "quantity": str(quantity).strip(),
        "amount_paise": int(amount_paise),
        "actor": actor,
    }

    # NULLS ARE DISTINCT in `ux_grn_line_external`, which is what 013 wants for
    # a locally-raised line carrying neither external id -- and which means a
    # line with a receive id but NO line id would not conflict and WOULD
    # duplicate on replay. `resolve_po_line` returns None for a null line id,
    # so no such line reaches here through the sweep; a caller that supplies
    # one directly gets this explicit update-first path, whose read-then-write
    # window is documented rather than hidden. §2.2's single cron worker per
    # connection is what makes the window safe, not luck.
    if params["line_external_id"] is None:
        updated = repo.query(
            session,
            f"""
            UPDATE {GRN_LINE} gl
            SET quantity = %(quantity)s::numeric,
                amount_paise = %(amount_paise)s,
                updated_at = now(), updated_by = %(actor)s,
                version_no = gl.version_no + 1
            FROM {PO_LINE} pl
            JOIN project p ON p.project_id = pl.project_id
            WHERE pl.po_line_id = gl.po_line_id
              AND gl.po_line_id = %(po_line_id)s
              AND gl.receive_external_id = %(receive_external_id)s
              AND gl.line_external_id IS NULL
              AND {{scope}}
            RETURNING gl.grn_line_id
            """,
            params, columns=SCOPE_COLUMNS)
        if updated:
            return

    # THE CONFLICT TARGET IS `ux_grn_line_external`'s three columns, and that
    # constraint is a table UNIQUE with NO predicate -- so there is nothing
    # further to name, unlike the two partial mirror indexes below.
    repo.query(
        session,
        f"""
        INSERT INTO {GRN_LINE} (
            grn_line_id, grn_id, po_id, po_line_id, receive_external_id,
            line_external_id, quantity, amount_paise, created_by, updated_by)
        SELECT %(grn_line_id)s, %(grn_id)s, %(po_id)s, pl.po_line_id,
               %(receive_external_id)s, %(line_external_id)s,
               %(quantity)s::numeric, %(amount_paise)s, %(actor)s, %(actor)s
        FROM {PO_LINE} pl
        JOIN project p ON p.project_id = pl.project_id
        WHERE pl.po_line_id = %(po_line_id)s AND {{scope}}
        ON CONFLICT (po_line_id, receive_external_id, line_external_id)
        DO UPDATE SET quantity = EXCLUDED.quantity,
                      amount_paise = EXCLUDED.amount_paise,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
                      version_no = {GRN_LINE}.version_no + 1
        RETURNING grn_line_id
        """,
        params, columns=SCOPE_COLUMNS)


def _mirror_grn_header(session: Session, *, po_id: str,
                       receive_external_id: str,
                       external_source: str | None,
                       receive_number: str | None,
                       received_at: datetime,
                       external_last_modified: datetime | None,
                       payload_sha: str | None,
                       is_reversal: bool, actor: str) -> str:
    """The `grn` header a receive line hangs off: created once, then refreshed.

    `grn.status` IS NOT WRITTEN HERE, and the omission is the point. 013 gives
    the table a `status` column defaulted to `'Approved'` and NO
    `external_status_raw` column at all, so the only place a raw Zoho value
    could land is the domain column -- and C17 is explicit that a raw value is
    kept VERBATIM and never interpreted. The verbatim copy lives in
    `integration_inbox.external_status_raw`, which `sweeps.SweepPoAnchored`
    writes before it ever reaches this function. Mapping a raw value onto
    `grn.status` here would be the guess C17 forbids, and `grn.status` carries
    no CHECK that would refuse it.

    PROVENANCE IS WRITTEN AND THEN REFRESHED, never invented: `external_source`
    and `external_id` identify the source document, `external_last_modified`
    and `payload_sha` say which VERSION of it we hold, and `received_at` is the
    observed time. The organisation is reached through
    `po -> project -> entity`, which is also the path 013's RLS policy takes.
    """
    params = {
        "grn_id": derived_id("GRN", external_source or "", receive_external_id),
        # Derived from the same two values as `grn_id`, so a replay produces
        # the same number and `ux_grn_number` is satisfied rather than
        # violated. A sequence would mint a second number for one receipt.
        "grn_number": receive_number or f"RCV-{receive_external_id}",
        "po_id": po_id, "received_at": received_at,
        "is_reversal": bool(is_reversal),
        "external_source": external_source,
        "external_id": receive_external_id,
        "external_last_modified": external_last_modified,
        "payload_sha": payload_sha, "actor": actor,
    }
    if external_source:
        # `ux_grn_external` is PARTIAL -- `WHERE external_id IS NOT NULL` -- so
        # the predicate is written out alongside the columns. Inference by
        # columns alone matches no index on this table and PostgreSQL refuses
        # rather than choosing another: the good failure, but only once the
        # predicate is there to be matched.
        rows = repo.query(
            session,
            f"""
            INSERT INTO {GRN} (
                grn_id, grn_number, po_id, received_at, is_reversal,
                external_source, external_id, external_last_modified,
                payload_sha, created_by, updated_by)
            SELECT %(grn_id)s, %(grn_number)s, po.po_id, %(received_at)s,
                   %(is_reversal)s, %(external_source)s, %(external_id)s,
                   %(external_last_modified)s, %(payload_sha)s,
                   %(actor)s, %(actor)s
            FROM {PURCHASE_ORDER} po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = %(po_id)s AND {{scope}}
            ON CONFLICT (external_source, external_id)
                WHERE external_id IS NOT NULL
            DO UPDATE SET external_last_modified = EXCLUDED.external_last_modified,
                          payload_sha = EXCLUDED.payload_sha,
                          updated_at = now(),
                          updated_by = EXCLUDED.updated_by,
                          version_no = {GRN}.version_no + 1
            RETURNING grn_id
            """,
            params, columns=SCOPE_COLUMNS)
    else:
        rows = repo.query(
            session,
            f"""
            INSERT INTO {GRN} (
                grn_id, grn_number, po_id, received_at, is_reversal,
                external_id, created_by, updated_by)
            SELECT %(grn_id)s, %(grn_number)s, po.po_id, %(received_at)s,
                   %(is_reversal)s, %(external_id)s, %(actor)s, %(actor)s
            FROM {PURCHASE_ORDER} po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = %(po_id)s AND {{scope}}
            ON CONFLICT (grn_id) DO UPDATE
                SET updated_at = now(), updated_by = EXCLUDED.updated_by,
                    version_no = {GRN}.version_no + 1
            RETURNING grn_id
            """,
            params, columns=SCOPE_COLUMNS)
    if not rows:
        raise ProcurementIngestError(
            "PURCHASE_ORDER_NOT_FOUND",
            f"Purchase order {po_id!r} does not exist or is out of scope, so "
            f"receive {receive_external_id!r} has no header and NOTHING was "
            f"written. Reported rather than skipped: a receipt silently not "
            f"mirrored is the drop §11.8 forbids.", status=404)
    return rows[0][0]


# ====================================================== external status (C17)
def resolve_accounting_status(session: Session, *, adapter_product: str,
                              object: str, field: str,
                              raw: str | None,
                              object_id: str,
                              entity_id: str | None = None,
                              project_id: str | None = None,
                              correlation_id: str | None = None,
                              actor: str = "SVC-SWEEP",
                              now: datetime | None = None,
                              as_of: date | None = None) -> tuple[str, str | None]:
    """`(accounting_status, exception_id)` for one verbatim external status.

    THIS FUNCTION NEVER GUESSES, and the two ways it could are both closed:

      * an unmapped raw value does NOT fall through to the schema default.
        `bill.accounting_status` defaults to `'Approved'`, which AUD-C-004
        makes accounting-effective, so a value we could not interpret would
        otherwise become a document finance acts on. It lands on
        :data:`UNINTERPRETABLE_ACCOUNTING_STATUS` instead -- the one value in
        `ck_bill_accounting_status` that is neither effective nor itself a
        claim -- and an `UNMAPPED_EXTERNAL_STATUS` exception is raised, which
        blocks capitalisation and blocks period close until somebody triages
        it.
      * a MISSING raw value is not treated as an unmapped one. `None` means the
        source did not send a status at all; there is nothing to interpret and
        nothing to raise, and the document keeps the caller's stated default.

    C17's own words: "Raw Zoho status values are stored VERBATIM and NEVER
    overwritten... An UNMAPPED raw value never guesses: the record is accepted,
    the raw value preserved, and a reconciliation_exception of kind
    UNMAPPED_EXTERNAL_STATUS is raised."
    """
    # Imported inside the function: `integration/statuses.py` loads and
    # validates the C17 contract from `research/` at import time, and the pg
    # layer must not pay that cost -- or acquire that dependency -- merely by
    # being imported.
    from ..integration import statuses as status_registry

    if raw is None or not str(raw).strip():
        return "Approved", None

    result = status_registry.zoho_status_map().resolve(
        status_registry.contract_product(adapter_product, object),
        object, field, str(raw), as_of=as_of)

    if result.is_mapped:
        mapped = result.accounting_status
        if mapped is None:
            # `NO_BUSINESS_STATUS` / `BUSINESS_STATUS` rows map a raw value
            # without saying anything about accounting effect. That is a
            # deliberate silence in C17, not a gap, so the caller's own default
            # stands and no exception is raised.
            return "Approved", None
        if mapped not in ACCOUNTING_STATUSES:
            raise ProcurementIngestError(
                "UNKNOWN_ACCOUNTING_STATUS",
                f"C17 maps {raw!r} to accounting status {mapped!r}, which is "
                f"not one of ck_bill_accounting_status's four "
                f"({', '.join(ACCOUNTING_STATUSES)}). The registry and the "
                f"schema disagree; nothing was written.", status=500)
        return mapped, None

    exception_id = store.raise_exception(
        session, kind="UNMAPPED_EXTERNAL_STATUS", object_type=object,
        object_id=object_id,
        detail=(f"{adapter_product} {object}.{field} arrived as {raw!r}, "
                f"which C17 cannot interpret: "
                f"{result.exception.reason if result.exception else 'no mapping row'} "
                f"The document is accepted and the raw value is preserved "
                f"verbatim; it is held at "
                f"{UNINTERPRETABLE_ACCOUNTING_STATUS} and is NOT "
                f"accounting-effective until this is triaged."),
        raised_at=now, entity_id=entity_id, project_id=project_id,
        correlation_id=correlation_id, actor=actor)
    return UNINTERPRETABLE_ACCOUNTING_STATUS, exception_id


# ================================================================ the bill
def mirror_bill(session: Session, *, external_source: str, external_id: str,
                bill_number: str, vendor_name: str, bill_date: date,
                lines: Sequence[Any],
                po_external_id: str | None = None,
                project_id: str | None = None,
                entity_id: str | None = None,
                doc_type: str = "BILL",
                accounting_status: str = "Approved",
                is_reversal: bool = False,
                reverses_bill_id: str | None = None,
                external_status_raw: str | None = None,
                external_last_modified: datetime | None = None,
                payload_sha: str | None = None,
                correlation_id: str | None = None,
                actor: str = "SVC-SWEEP",
                now: datetime | None = None,
                status_as_of: date | None = None) -> dict[str, Any]:
    """Mirror one vendor bill and its lines. Idempotent, never partial.

    Returns a summary: the `bill_id`, how many lines were attributed to a
    `po_line`, how many were quarantined and the total paise quarantined.

    PARTIAL, FINAL, OVER-BILLED AND NEGATIVE ARE ALL ORDINARY HERE. A first
    bill for part of a PO line and a second for the rest are two bills against
    one line and both are written. A third that takes the total past what was
    ordered is written too -- over-billing is NOT clamped, NOT netted and NOT
    silently rejected; :func:`reconcile_po_lines` is where it becomes visible,
    with ordered, received and billed side by side. A credit note carries
    negative `amount_paise` and `bill_line` has no `>= 0` CHECK precisely so it
    can.

    QUARANTINE, NOT PRO-RATA. A line naming a PO line we cannot resolve, and
    carrying no explicit control cell of its own, is NOT spread across the
    lines that did resolve and is NOT dropped. It raises a reconciliation
    exception holding its FULL value, which blocks capitalisation. `bill_line`
    has NOT NULL `wbs_id` and `budget_head_id`, so there is literally nowhere
    to write such a line -- and inventing a cell for it is the one outcome
    §11.8 forbids outright.
    """
    moment = now or store._utcnow()
    external_source = store._require(external_source,
                                     code="BLANK_EXTERNAL_SOURCE",
                                     what="external_source")
    external_id = store._require(external_id, code="BLANK_EXTERNAL_ID",
                                 what="external_id")
    bill_number = store._require(bill_number, code="BLANK_BILL_NUMBER",
                                 what="bill_number")
    # `bill.vendor_name` is NOT NULL and there is no honest filler for it. A
    # placeholder such as "UNKNOWN" is a plausible-looking default of exactly
    # the kind this surface refuses everywhere else: it would satisfy the
    # column, survive review, and put an un-matchable vendor on a document
    # somebody later has to reconcile against `vendor_master`.
    vendor_name = store._require(vendor_name, code="BLANK_VENDOR_NAME",
                                 what="vendor_name")
    if doc_type not in DOC_TYPES:
        raise ProcurementIngestError(
            "UNKNOWN_DOC_TYPE",
            f"doc_type {doc_type!r} is not one of ck_bill_doc_type's "
            f"({', '.join(DOC_TYPES)}). A credit note and a debit note are "
            f"ordinary documents here; a fourth kind is a contract change.",
            status=422)
    if accounting_status not in ACCOUNTING_STATUSES:
        raise ProcurementIngestError(
            "UNKNOWN_ACCOUNTING_STATUS",
            f"accounting_status {accounting_status!r} is not one of "
            f"ck_bill_accounting_status's ({', '.join(ACCOUNTING_STATUSES)}).",
            status=422)

    po_id: str | None = None
    if po_external_id:
        po_id, po_project_id, po_entity_id = _purchase_order(
            session, po_external_id)
        entity_id = entity_id or po_entity_id
        if project_id is not None and project_id != po_project_id:
            raise ProcurementIngestError(
                "BILL_PROJECT_CONTRADICTS_PO",
                f"Bill {external_id!r} claims project {project_id!r} but its "
                f"purchase order {po_external_id!r} belongs to "
                f"{po_project_id!r}. `fk_bill_po_project` would refuse this "
                f"row; refused here instead so the report names the two "
                f"projects rather than a constraint.", status=422)
        project_id = po_project_id
    if not project_id:
        # `bill.project_id` is NOT NULL and a non-PO bill has no purchase order
        # to inherit one from. Deriving it from the first line that happens to
        # resolve would attribute a whole document on the strength of one line.
        raise ProcurementIngestError(
            "BILL_PROJECT_UNKNOWN",
            f"Bill {external_id!r} names no purchase order and no project. "
            f"`bill.project_id` is NOT NULL and there is nothing to derive it "
            f"from: inferring it from whichever line resolved first would "
            f"attribute a whole document on the strength of one line. "
            f"Nothing was written.", status=422)
    if entity_id is None:
        entity_id = _project_entity(session, project_id)

    bill_id = derived_id("BILL", external_source, external_id)
    effective_status, status_exception = resolve_accounting_status(
        session, adapter_product=_adapter_product_of(external_source),
        object="bill", field="status", raw=external_status_raw,
        object_id=external_id, entity_id=entity_id, project_id=project_id,
        correlation_id=correlation_id, actor=actor, now=moment,
        # EXPLICIT, and injectable. C17 rows carry an effective window and the
        # resolver compares it against `date.today()` by default, so a mapping
        # that is correct today silently becomes UNMAPPED the day a window
        # closes -- and a test written without this pins nothing. Same reason
        # the VRT suite injects its clock: a check whose answer depends on the
        # wall clock is a check that reports drift it did not cause.
        as_of=status_as_of)
    if external_status_raw is not None and str(external_status_raw).strip():
        accounting_status = effective_status

    params = {
        "bill_id": bill_id, "bill_number": bill_number, "po_id": po_id,
        "project_id": project_id, "vendor_name": vendor_name,
        "bill_date": bill_date, "accounting_status": accounting_status,
        "is_reversal": bool(is_reversal),
        "reverses_bill_id": reverses_bill_id, "doc_type": doc_type,
        "external_source": external_source, "external_id": external_id,
        "external_last_modified": external_last_modified,
        "payload_sha": payload_sha, "actor": actor,
    }
    # `ux_bill_external` is PARTIAL. Both the columns AND the predicate, for
    # the reason `_mirror_grn_header` states.
    #
    # `bill.status` is NOT in the SET list: it is our domain status, and the
    # raw external one is preserved verbatim in the inbox. `project_id` is not
    # in it either -- a mirrored bill that changed project would be a different
    # commitment, and `fk_bill_po_project` should be the thing that refuses it,
    # not an UPDATE that quietly succeeds.
    rows = repo.query(
        session,
        f"""
        INSERT INTO {BILL} (
            bill_id, bill_number, po_id, project_id, vendor_name, bill_date,
            accounting_status, is_reversal, reverses_bill_id, doc_type,
            external_source, external_id, external_last_modified, payload_sha,
            created_by, updated_by)
        SELECT %(bill_id)s, %(bill_number)s, %(po_id)s, p.project_id,
               %(vendor_name)s, %(bill_date)s, %(accounting_status)s,
               %(is_reversal)s, %(reverses_bill_id)s, %(doc_type)s,
               %(external_source)s, %(external_id)s,
               %(external_last_modified)s, %(payload_sha)s,
               %(actor)s, %(actor)s
        FROM project p
        WHERE p.project_id = %(project_id)s AND {{scope}}
        ON CONFLICT (external_source, external_id)
            WHERE external_id IS NOT NULL
        DO UPDATE SET vendor_name = EXCLUDED.vendor_name,
                      bill_date = EXCLUDED.bill_date,
                      accounting_status = EXCLUDED.accounting_status,
                      doc_type = EXCLUDED.doc_type,
                      external_last_modified = EXCLUDED.external_last_modified,
                      payload_sha = EXCLUDED.payload_sha,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
                      version_no = {BILL}.version_no + 1
        RETURNING bill_id
        """,
        params, columns=SCOPE_COLUMNS)
    if not rows:
        raise ProcurementIngestError(
            "BILL_PROJECT_NOT_FOUND",
            f"Project {project_id!r} does not exist or is out of scope for "
            f"this principal, so bill {external_id!r} was NOT mirrored.",
            status=404)
    bill_id = rows[0][0]

    attributed = 0
    quarantined = 0
    quarantined_paise = 0
    exceptions: list[str] = []
    for index, line in enumerate(lines or ()):
        outcome = _mirror_bill_line(
            session, bill_id=bill_id, bill_external_id=external_id,
            po_id=po_id, po_external_id=po_external_id, line=line,
            index=index, project_id=project_id, entity_id=entity_id,
            correlation_id=correlation_id, actor=actor, now=moment)
        if outcome["attributed"]:
            attributed += 1
        else:
            quarantined += 1
            quarantined_paise += abs(int(outcome["amount_paise"]))
            exceptions.append(outcome["exception_id"])

    return {
        "bill_id": bill_id, "project_id": project_id, "po_id": po_id,
        "attributed": attributed, "quarantined": quarantined,
        # The MAGNITUDE held, matching `reconciliation_exception.source_paise`,
        # which `ck_reconciliation_exception_paise` forbids to be negative.
        # The signed originals are on the exceptions' audit events.
        "quarantined_paise": quarantined_paise,
        "exception_ids": tuple(exceptions),
        "status_exception_id": status_exception,
        "accounting_status": accounting_status,
        "accounting_effective": accounting_status in ACCOUNTING_EFFECTIVE,
    }


def _mirror_bill_line(session: Session, *, bill_id: str,
                      bill_external_id: str, po_id: str | None,
                      po_external_id: str | None, line: Any, index: int,
                      project_id: str, entity_id: str | None,
                      correlation_id: str | None, actor: str,
                      now: datetime) -> dict[str, Any]:
    """One bill line: attributed to its PO line's control cell, or quarantined.

    The line's identity falls back to its ORDINAL when the source gave it none.
    That is not cosmetic: `ux_reconciliation_exception_open` keys on
    `object_id`, so two identifierless lines keyed on one constant would
    collapse into ONE exception and the second line's value would vanish -- a
    silent drop arriving through the code written to prevent one. The same
    defect was found and fixed in `sweeps._attribute`; this is the same
    fallback, for the same reason.
    """
    line_external_id = _text(_line_field(
        line, "purchase_order_line_external_id", "po_line_external_id",
        "line_external_id", "line_item_id", "purchaseorder_item_id"))
    line_key = line_external_id or f"#{index}"
    amount_paise = int(_line_field(
        line, "line_total_paise", "amount_paise", "total_paise", default=0) or 0)

    po_line_id = None
    if po_external_id:
        po_line_id = resolve_po_line(session, po_external_id=po_external_id,
                                     line_external_id=line_external_id)

    if po_line_id is not None:
        line_po_id, _project, wbs_id, budget_head_id = _po_line_cell(
            session, po_line_id)
    else:
        # A non-PO bill line may name its own cell -- that is what
        # `bill_line.po_id`/`po_line_id` being NULLABLE is FOR, and the
        # four-column FK is simply not checked for it. A line that names
        # neither a resolvable PO line NOR a cell has nowhere to go.
        wbs_id = _text(_line_field(line, "wbs_id"))
        budget_head_id = _text(_line_field(line, "budget_head_id"))
        line_po_id = None
        if not wbs_id or not budget_head_id:
            exception_id = store.raise_exception(
                session, kind="CONTROL_TOTAL_MISMATCH", object_type="bill_line",
                object_id=f"{bill_external_id}:{line_key}",
                detail=(f"Bill {bill_external_id} line "
                        f"{line_external_id or '(no line identifier)'} does "
                        f"not resolve to a known po_line on PO "
                        f"{po_external_id or '(none)'} and names no control "
                        f"cell of its own. Quarantined at full value; never "
                        f"spread pro-rata across the lines that did resolve, "
                        f"and never dropped."),
                raised_at=now, entity_id=entity_id, project_id=project_id,
                source_paise=amount_paise, correlation_id=correlation_id,
                actor=actor)
            # SET, never `+=`. See `integration_store.accumulate_unattributed`.
            store.accumulate_unattributed(
                session, project_id=project_id, paise=amount_paise,
                source_key=exception_id)
            return {"attributed": False, "exception_id": exception_id,
                    "amount_paise": amount_paise}

    params = {
        "bill_line_id": derived_id("BLL", bill_id, line_key),
        "bill_id": bill_id, "po_id": line_po_id, "po_line_id": po_line_id,
        "wbs_id": wbs_id, "budget_head_id": budget_head_id,
        "description": _text(_line_field(line, "description")),
        "quantity": str(_line_field(line, "quantity", "qty", default="1")),
        "amount_paise": amount_paise,
        "non_creditable_tax_paise": int(_line_field(
            line, "non_creditable_tax_paise", default=0) or 0),
        "freight_paise": int(_line_field(line, "freight_paise", default=0) or 0),
        "po_line_external_id": line_external_id,
        "actor": actor,
    }
    # THE CONFLICT TARGET IS THE PRIMARY KEY, because 013 gives `bill_line` no
    # external unique index at all. `bill_line_id` is DERIVED from
    # `(bill_id, line_key)` so a replay lands on the same row -- which matters
    # more here than anywhere else in this module, since `capex_app` has DELETE
    # revoked and a duplicated line could not be removed afterwards.
    repo.query(
        session,
        f"""
        INSERT INTO {BILL_LINE} (
            bill_line_id, bill_id, po_id, po_line_id, wbs_id, budget_head_id,
            description, quantity, amount_paise, non_creditable_tax_paise,
            freight_paise, po_line_external_id, created_by, updated_by)
        SELECT %(bill_line_id)s, %(bill_id)s, %(po_id)s, %(po_line_id)s,
               %(wbs_id)s, %(budget_head_id)s, %(description)s,
               %(quantity)s::numeric, %(amount_paise)s,
               %(non_creditable_tax_paise)s, %(freight_paise)s,
               %(po_line_external_id)s, %(actor)s, %(actor)s
        FROM {BILL} b
        JOIN project p ON p.project_id = b.project_id
        WHERE b.bill_id = %(bill_id)s AND {{scope}}
        ON CONFLICT (bill_line_id)
        DO UPDATE SET description = EXCLUDED.description,
                      quantity = EXCLUDED.quantity,
                      amount_paise = EXCLUDED.amount_paise,
                      non_creditable_tax_paise = EXCLUDED.non_creditable_tax_paise,
                      freight_paise = EXCLUDED.freight_paise,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
                      version_no = {BILL_LINE}.version_no + 1
        RETURNING bill_line_id
        """,
        params, columns=SCOPE_COLUMNS)
    return {"attributed": True, "exception_id": None,
            "amount_paise": amount_paise}


def _adapter_product_of(external_source: str) -> str:
    """The C1 adapter product an `external_source` names.

    `external_source` is our own label (`ZOHO_ERP`, `ZOHO_BOOKS`), written by
    `002_financial_controls.sql` and by `pg/masters.ingest_from_adapter`. C17
    is keyed on the adapter's product instead, and
    `statuses.contract_product()` is the only sanctioned bridge from there to
    Books/Inventory. This is the one step before it.

    An unrecognised source RAISES. Defaulting to `ERP` would resolve a Books
    status against ERP's rows, which is exactly the cross-product fallback C17
    forbids -- and it would do so silently, mapping a value that has no
    business being mapped at all.
    """
    normalised = (external_source or "").strip().upper()
    if normalised in ("ZOHO_ERP", "ERP"):
        return "ERP"
    if normalised in ("ZOHO_BOOKS", "ZOHO_INVENTORY", "ZOHO_BOOKS_INVENTORY",
                      "BOOKS", "INVENTORY", "BOOKS_INVENTORY"):
        return "BOOKS_INVENTORY"
    raise ProcurementIngestError(
        "UNKNOWN_EXTERNAL_SOURCE",
        f"external_source {external_source!r} names no C1 adapter product. "
        f"Defaulting to ERP would resolve this document's status against ERP's "
        f"C17 rows whatever product actually sent it, which is the "
        f"cross-product fallback C17 forbids. Nothing was interpreted.",
        status=422)


# ======================================================== ordered vs billed
def reconcile_po_lines(session: Session, *, project_id: str | None = None,
                       po_id: str | None = None,
                       limit: int = 500) -> list[dict[str, Any]]:
    """Ordered, received and billed per PO line. Wave 5's exit criterion.

    "Ordered, received, billed and open reconcile to the paisa" needs four
    quantities and three of them had nowhere to live until 013. This is where
    they are read back, and it is deliberately a REPORT rather than a
    constraint:

      * over-billing is VISIBLE, not clamped. `over_billed_paise` is positive
        when a PO line has been billed past what was ordered, and the row is
        returned rather than suppressed. Clamping would make the ledger agree
        with the budget by lying about the invoice.
      * nothing is netted against anything. A credit note reduces `billed_paise`
        because its lines are genuinely negative; it does not cancel a
        quarantined exception, which stays Open and keeps blocking.

    ``::bigint`` ON EVERY SUM, and not decoration. PostgreSQL's `SUM()` over a
    `bigint` column returns **numeric**, psycopg maps numeric to
    `decimal.Decimal`, and a Decimal reaching arithmetic that expects an int is
    the defect that took down the availability verdict, both approval paths and
    the concurrency proof -- in the PostgreSQL CI job only, because a Decimal
    cannot appear without a real server.

    The two sums are computed in separate scalar subqueries rather than by
    joining both child tables at once: a PO line with two receipts and two bill
    lines would otherwise produce four rows and every total would be doubled.
    That is the classic fan-out, and here it would double money.
    """
    rows = repo.query(
        session,
        f"""
        SELECT pl.po_line_id, pl.po_id, pl.project_id, pl.wbs_id,
               pl.budget_head_id, pl.line_external_id,
               pl.amount_paise::bigint AS ordered_paise,
               coalesce((SELECT SUM(gl.amount_paise)
                         FROM {GRN_LINE} gl
                         WHERE gl.po_line_id = pl.po_line_id), 0)::bigint
                   AS received_paise,
               coalesce((SELECT SUM(bl.amount_paise)
                         FROM {BILL_LINE} bl
                         JOIN {BILL} b ON b.bill_id = bl.bill_id
                         WHERE bl.po_line_id = pl.po_line_id
                           AND b.accounting_status IN ('Approved', 'Reversal')),
                        0)::bigint AS billed_paise
        FROM {PO_LINE} pl
        JOIN project p ON p.project_id = pl.project_id
        WHERE (%(project_id)s::text IS NULL OR pl.project_id = %(project_id)s)
          AND (%(po_id)s::text IS NULL OR pl.po_id = %(po_id)s)
          AND {{scope}}
        ORDER BY pl.po_id, pl.po_line_id
        LIMIT %(limit)s
        """,
        {"project_id": project_id, "po_id": po_id, "limit": max(int(limit), 0)},
        columns=SCOPE_COLUMNS,
    )
    out: list[dict[str, Any]] = []
    for (po_line_id, line_po_id, line_project, wbs_id, budget_head_id,
         line_external_id, ordered, received, billed) in rows:
        ordered, received, billed = int(ordered), int(received), int(billed)
        out.append({
            "po_line_id": po_line_id, "po_id": line_po_id,
            "project_id": line_project, "wbs_id": wbs_id,
            "budget_head_id": budget_head_id,
            "line_external_id": line_external_id,
            "ordered_paise": ordered,
            "received_paise": received,
            "billed_paise": billed,
            # Positive means billed past ordered. Reported, never clamped.
            "over_billed_paise": max(billed - ordered, 0),
            "over_billed": billed > ordered,
            # Positive means ordered but not yet billed -- the "open" of the
            # four quantities. Negative would be the over-billed case, which is
            # why it is floored here and reported separately above rather than
            # letting one signed number mean two different things.
            "open_paise": max(ordered - billed, 0),
        })
    return out



# ============================== commitment against actual (013), the SQL
def reconciliation_lines(session: Session, *, project_id: str | None = None,
                         limit: int = 500) -> list[dict]:
    """Every purchase-order line in scope, with ordered / received / billed.

    THE SQL IS HERE AND NOT IN `integration_store.py`, for the reason the
    module docstring gives: `po_line.amount_paise`, `grn_line.amount_paise` and
    `bill_line.amount_paise` are money on LEDGER rows, and
    `test_integration_store.py::test_money_in_this_module_appears_only_on_the_011_exception_table`
    holds that module to transport. `integration_store.reconciliation_lines`
    remains the name the API router calls and delegates here, exactly as
    `resolve_po_line` and `record_receive_line` do. The boundary is kept by
    moving the statement, not by widening the guard.

    ONE STATEMENT, not one per line. The SQLite original issues three queries
    and joins them in Python dictionaries; that is fine over a POC's row counts
    and is a fan-out here, so the two aggregates are CTEs restricted to the
    lines the driving query already selected.

    THE SCOPE PREDICATE SITS IN THE DRIVING CTE, ONCE, and everything else is
    downstream of it -- `received` and `billed` both filter
    `po_line_id IN (SELECT po_line_id FROM scoped_line)`. That is deliberate
    rather than a token pasted in three places: a receipt or a bill line is
    reachable only through a purchase-order line the caller can already see, so
    filtering the driving set filters all three, and there is exactly one place
    to read to know that it does.

    `columns=` names ALL FOUR dimensions and waives none, through the `project`
    join -- see :data:`SCOPE_COLUMNS` for why a waiver here would be
    wrong rather than merely lax.
    """
    rows = repo.query(
        session,
        f"""
        WITH scoped_line AS (
            SELECT pl.po_line_id, pl.line_no, pl.description,
                   pl.wbs_id, w.wbs_code,
                   pl.budget_head_id, bh.name AS budget_head,
                   pl.project_id,
                   (pl.amount_paise
                    + pl.non_creditable_tax_paise
                    + pl.freight_paise)::bigint AS ordered_paise,
                   po.po_id, po.po_number, po.status AS po_status,
                   po.vendor_name, po.currency, po.exchange_rate,
                   po.amendment_no, po.external_id AS po_external_id
            FROM {PO_LINE} pl
            JOIN {PURCHASE_ORDER} po ON po.po_id = pl.po_id
            JOIN project p ON p.project_id = pl.project_id
            JOIN wbs_element w
              ON w.wbs_id = pl.wbs_id AND w.project_id = pl.project_id
            JOIN budget_head bh ON bh.budget_head_id = pl.budget_head_id
            WHERE (%(project_id)s::text IS NULL
                   OR pl.project_id = %(project_id)s)
              AND {{scope}}
        ),
        received AS (
            -- A reversal GRN subtracts the MAGNITUDE of its line, so a
            -- reversal line stored positive and one stored negative both
            -- reduce received by the same amount. `domain.compute_ledger`
            -- spells it `-ABS(...)` for exactly that reason.
            SELECT gl.po_line_id,
                   SUM(CASE WHEN g.is_reversal THEN -ABS(gl.amount_paise)
                            ELSE gl.amount_paise END)::bigint AS received_paise
            FROM {GRN_LINE} gl
            JOIN {GRN} g ON g.grn_id = gl.grn_id
            WHERE g.status <> 'Void'
              AND gl.po_line_id IN (SELECT po_line_id FROM scoped_line)
            GROUP BY gl.po_line_id
        ),
        billed AS (
            -- ONLY accounting-effective bills relieve commitment (AUD-C-004).
            -- A Draft or Void bill is not money and must not reduce the open
            -- commitment, which is the direction that UNDERSTATES exposure.
            SELECT bl.po_line_id,
                   SUM(CASE WHEN b.accounting_status = 'Reversal'
                            THEN -ABS(bl.amount_paise
                                      + bl.non_creditable_tax_paise
                                      + bl.freight_paise)
                            ELSE (bl.amount_paise
                                  + bl.non_creditable_tax_paise
                                  + bl.freight_paise) END)::bigint
                       AS billed_paise
            FROM {BILL_LINE} bl
            JOIN {BILL} b ON b.bill_id = bl.bill_id
            WHERE bl.po_line_id IS NOT NULL
              AND b.accounting_status = ANY(%(effective)s)
              AND bl.po_line_id IN (SELECT po_line_id FROM scoped_line)
            GROUP BY bl.po_line_id
        )
        SELECT s.po_line_id, s.line_no, s.description, s.wbs_id, s.wbs_code,
               s.budget_head_id, s.budget_head, s.project_id, s.ordered_paise,
               s.po_id, s.po_number, s.po_status, s.vendor_name, s.currency,
               s.exchange_rate, s.amendment_no, s.po_external_id,
               COALESCE(rcv.received_paise, 0)::bigint,
               COALESCE(bld.billed_paise, 0)::bigint
        FROM scoped_line s
        -- `rcv` / `bld`, never `r` / `b`. `b` is `bill` two CTEs above, and a
        -- one-letter alias reused for a table and for an aggregate OVER that
        -- table is how a column check stops being able to tell them apart.
        LEFT JOIN received rcv ON rcv.po_line_id = s.po_line_id
        LEFT JOIN billed   bld ON bld.po_line_id = s.po_line_id
        ORDER BY s.po_number, s.line_no, s.po_line_id
        LIMIT %(limit)s
        """,
        {"project_id": project_id,
         "effective": list(store.ACCOUNTING_EFFECTIVE_BILL_STATES),
         "limit": int(limit)},
        columns=SCOPE_COLUMNS,
    )

    out: list[dict] = []
    for row in rows:
        (po_line_id, line_no, description, wbs_id, wbs_code, budget_head_id,
         budget_head, line_project_id, ordered, po_id, po_number, po_status,
         vendor_name, currency, exchange_rate, amendment_no, po_external_id,
         received, billed) = row
        # `int()` on every one of the three, at the boundary, for the reason in
        # this section's header. Not decoration: `::bigint` protects the SQL
        # side and this protects everything downstream of the driver.
        ordered, received, billed = int(ordered), int(received), int(billed)
        released = po_status in store.COMMITMENT_RELEASING_STATES

        # ORDERED LESS BILLED. Never ordered less received.
        open_commitment = 0 if released else max(0, ordered - billed)
        # Its OWN bucket, never subtracted from the line above.
        received_not_billed = max(0, received - billed)
        # The residual a Cancelled/Closed purchase order gave back, and the
        # amount a bill exceeded its order by. Together with `open_commitment`
        # these make the identity below hold to the paisa in every one of the
        # four cases -- live/released crossed with under/over-billed:
        #
        #     ordered - billed == open_commitment
        #                         + residual_released
        #                         - over_billed
        #
        # Exposed rather than left implicit because without them a reader
        # cannot tell a released residual from an over-bill: both show open
        # commitment 0 against an ordered that does not equal billed, and they
        # mean opposite things.
        residual_released = (ordered - billed) if (released and ordered > billed) else 0
        over_billed = max(0, billed - ordered)
        flag, position = store._reconciliation_position(
            ordered, received, billed, released, str(po_status))

        out.append({
            "po_line_id": po_line_id, "line_no": line_no,
            "description": description,
            "wbs_id": wbs_id, "wbs_code": wbs_code,
            "budget_head_id": budget_head_id, "budget_head": budget_head,
            "project_id": line_project_id,
            "po_id": po_id, "po_number": po_number, "po_status": po_status,
            "po_external_id": po_external_id,
            "vendor_name": vendor_name, "currency": currency,
            "exchange_rate": (None if exchange_rate is None
                              else str(exchange_rate)),
            "amendment_no": amendment_no,
            "ordered_paise": ordered,
            "received_paise": received,
            "billed_paise": billed,
            "open_commitment_paise": open_commitment,
            "received_not_billed_paise": received_not_billed,
            "exposure_paise": open_commitment + billed,
            "residual_released_paise": residual_released,
            "over_billed_paise": over_billed,
            "flag": flag, "position": position,
        })
    return out


__all__ = [
    "ACCOUNTING_EFFECTIVE",
    "ACCOUNTING_STATUSES",
    "DOC_TYPES",
    "UNINTERPRETABLE_ACCOUNTING_STATUS",
    "ProcurementIngestError",
    "derived_id",
    "mirror_bill",
    "reconcile_po_lines",
    "reconciliation_lines",
    "record_receive_line",
    "resolve_accounting_status",
    "resolve_po_line",
]
