"""The inbound half of the procurement chain: receives and vendor bills.

THE LEDGER, NOT THE SERVICE LAYER. There are two similarly named modules in
this package. THIS one, `pg/procurement.py`, is the LEDGER: the SQL that reads
and writes procurement rows -- goods receipts, vendor bills, and the
reconciliation of received against ordered. `pg/procurement_services.py` is the
SERVICE layer: the PR -> PO lifecycle, the budget control that gates it, and
the emission plan that carries an approved PO to Zoho. This module owns the
rows; that one owns the decisions about them, and reads `bill_line` back out of
here to derive `budget_ledger_cell.commitment_paise`.

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

from . import audit as audit_mod
from . import integration_store as store
from . import procurement_services as svc
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

#: The audit actions this module appends. Every one of them names a mutation
#: that MOVES MONEY: a mirrored receive raises `received` and therefore
#: `received_not_billed`; a mirrored bill raises `actual` and relieves
#: `commitment`; a superseded bill line withdraws both again; a retracted
#: quarantine removes a figure from `open_exception_exposure`, which is what
#: the capitalisation gate and the period close quote.
#:
#: WHY THE MIRRORED DOCUMENTS NEEDED THIS AT ALL. Every mutation in
#: `procurement_services.py` appends to the same hash-chained log on the same
#: session. This module -- where the money actually ARRIVES, from outside --
#: appended nothing, so the ledger movement that finance sees on SCR-18 had no
#: entry behind it and §11.9's trace ("one id traces a Zoho bill from HTTP
#: response to ledger movement to audit entry") stopped one step short.
AUDIT_RECEIVE_MIRRORED = "GRN_MIRRORED"
AUDIT_BILL_MIRRORED = "BILL_MIRRORED"
AUDIT_BILL_LINE_SUPERSEDED = "BILL_LINE_SUPERSEDED"
AUDIT_QUARANTINE_RETRACTED = "RECONCILIATION_QUARANTINE_RETRACTED"

#: The two quarantine identities this module raises, and therefore the two it
#: must be able to RETRACT.
#:
#: `ux_reconciliation_exception_open` is UNIQUE on
#: ``(kind, object_type, object_id)`` while the row is Open, so these two
#: triples are the only handle a later success has on the exception an earlier
#: pass raised. Spelled as constants so the raise and the retraction cannot
#: drift into keying on different strings -- which would leave the exception
#: Open for ever while the line posts, and count the same rupees twice.
QUARANTINE_RECEIVE_LINE: tuple[str, str] = ("GRN_LINE_UNATTRIBUTED", "grn_line")
QUARANTINE_BILL_LINE: tuple[str, str] = ("CONTROL_TOTAL_MISMATCH", "bill_line")

#: What a superseded `bill_line`'s description is stamped with.
#:
#: `capex_app` has DELETE revoked and the audit trail matters, so a line that
#: disappeared from a revised source bill is NOT removed: it is made
#: NON-EFFECTIVE -- its money zeroed so it stops contributing to billed and to
#: actual -- and marked, so the row still says what it was and when it stopped
#: counting. The prefix is also the idempotency guard: a re-mirror of the same
#: revision must not stamp the same row twice.
SUPERSEDED_DESCRIPTION_PREFIX = "SUPERSEDED "


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


# ==================================================================== audit
def _audit(session: Session, *, actor: str, action: str, object_type: str,
           object_id: str, detail: str,
           correlation_id: str | None = None) -> dict[str, Any]:
    """One append-only audit entry, INSIDE the caller's transaction (§H-5).

    NOT a second connection and not a deferred write. `audit_mod.append` takes
    the caller's `session`, so a mirror that raises after the entry is written
    rolls the entry back with it: there is no path that leaves an audit line
    claiming a receipt was mirrored when no `grn_line` exists. That is the
    whole reason it is here rather than in the sweep that calls this module --
    the sweep commits per progress step, and an entry written outside the
    document's own transaction would survive its rollback.

    THE ACTOR IS SERVER-DERIVED. It is the service or principal identity the
    calling layer already authenticated -- `SVC-SWEEP` for the cron sweeps --
    never a caller-supplied header, and never blank: an audit entry attributed
    to nobody is indistinguishable from one nobody wrote. `audit_log.actor` is
    `NOT NULL` with no FK, so the refusal has to be here.

    Per the global lock order in `locking.py` this is the LAST locking action
    of a mutating function: the document rows first, then the advisory audit
    lock `audit_mod.append` takes on the stream.
    """
    actor = store._require(actor, code="BLANK_AUDIT_ACTOR", what="actor")
    return audit_mod.append(
        session, actor, action, object_type, object_id, detail,
        correlation_id=correlation_id)


# ==================================================== quarantine retraction
def retract_quarantine(session: Session, *, kind: str, object_type: str,
                       object_id: str, reason: str, actor: str,
                       correlation_id: str | None = None,
                       now: datetime | None = None) -> dict[str, Any] | None:
    """Close the Open exception a line's EARLIER pass raised, now that it posted.

    THE DEFECT THIS CLOSES, AND WHY IT IS THE ORDINARY CASE.
    -------------------------------------------------------
    Both quarantine paths raise a `reconciliation_exception` at the line's FULL
    value and hold it in the project bucket. Nothing resolved that exception
    when the SAME line later attributed -- and raise-then-succeed is not an
    edge case here, it is the normal sequence: the sweeps re-walk by design on
    a 300-second overlap with a cycling cursor, and the order of the PO poll
    against the bill poll is not guaranteed, so a bill line routinely arrives
    before the purchase-order line it names.

    The consequence was double counting that never went away. The same 25,000
    rupees sat in `bill_line.amount_paise` -- feeding `billed` and therefore
    `actual` -- AND in `open_exception_exposure`'s ``SUM(source_paise)``, which
    is the figure the capitalisation gate and the period close quote. The
    exception also blocked capitalisation for ever on a line that was by then
    correctly posted.

    TRANSITIONED, NEVER DELETED. `capex_app` has DELETE revoked and the audit
    trail is the point: the row stays, keeps its `source_paise` as the record
    of what WAS held, and moves to `Resolved` with an actor, a time and a
    reason -- which is exactly what `ck_reconciliation_exception_resolution`
    demands and what makes the value stop counting, because
    `open_exception_exposure` and `open_exceptions` both filter
    ``status = 'Open'``. Zeroing `source_paise` instead would destroy the
    evidence of the quarantine while leaving a row that says nothing happened.

    ONE STATEMENT, keyed on the same three columns `store.raise_exception`
    conflicts on. `ux_reconciliation_exception_open` is UNIQUE on them while
    the row is Open, so at most one row can match and there is no
    read-then-write window to lose a concurrent triage into. A row a human
    already Accepted or Wrote off is NOT reopened or overwritten: the
    ``status = 'Open'`` predicate leaves the first reviewer's decision alone,
    and this returns `None`.

    Returns the retracted row, or `None` when there was no Open exception --
    which is the common case, since most lines attribute on their first pass.
    """
    moment = now or store._utcnow()
    object_id = store._require(object_id, code="BLANK_QUARANTINE_OBJECT_ID",
                               what="object_id")
    actor = store._require(actor, code="BLANK_QUARANTINE_ACTOR", what="actor")
    reason = store._require(reason, code="BLANK_QUARANTINE_REASON",
                            what="a reason for retracting a quarantine")
    row = repo.query_one(
        session,
        f"""
        UPDATE {store.RECONCILIATION_EXCEPTION} AS x
        SET status = %(resolved)s,
            resolved_at = %(now)s,
            resolved_by = %(actor)s,
            resolution_note = %(reason)s
        WHERE x.kind = %(kind)s
          AND x.object_type = %(object_type)s
          AND x.object_id = %(object_id)s
          AND x.status = %(open)s
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        RETURNING x.exception_id, x.entity_id, x.project_id, x.source_paise,
                  x.correlation_id
        """,
        {"kind": kind, "object_type": object_type, "object_id": object_id,
         "reason": reason, "actor": actor, "now": moment,
         "open": store.EXCEPTION_OPEN, "resolved": store.EXCEPTION_RESOLVED},
        columns=store.EXCEPTION_SCOPE_COLUMNS,
    )
    if row is None:
        return None
    exception_id, entity_id, project_id, source_paise, row_correlation = row
    retracted = 0 if source_paise is None else int(source_paise)
    result = {
        "exception_id": exception_id, "kind": kind,
        "object_type": object_type, "object_id": object_id,
        "entity_id": entity_id, "project_id": project_id,
        "status": store.EXCEPTION_RESOLVED,
        # What the bucket STOPS holding. The row keeps the figure; this is the
        # amount that leaves `open_exception_exposure` as a consequence.
        "retracted_paise": retracted,
    }
    store.record_event(
        session, kind="reconciliation.quarantine.retracted", actor=actor,
        correlation_id=correlation_id or row_correlation, now=moment,
        detail=dict(result, reason=reason))
    _audit(session, actor=actor, action=AUDIT_QUARANTINE_RETRACTED,
           object_type=object_type, object_id=object_id,
           detail=(f"{kind} exception {exception_id} retracted: the line "
                   f"attributed on a later pass and is now posted. "
                   f"{retracted} paise leaves the unattributed bucket, which "
                   f"had been counting it a second time alongside the posted "
                   f"row. {reason}"),
           correlation_id=correlation_id or row_correlation)
    return result


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
                        connection_id: str | None = None,
                        line_no: int | None = None,
                        external_status_raw: str | None = None,
                        actor: str = "SVC-SWEEP",
                        now: datetime | None = None) -> None:
    """Mirror one attributed receive line, header included, idempotently.

    See `integration_store.record_receive_line`, the `sweeps.SweepStore`
    surface this backs, for the contract and for why the provenance arguments
    after `amount_paise` are optional but not decorative.

    THREE ARGUMENTS ARRIVED WITH MIGRATION 014, all optional so that no
    existing caller breaks and none of them is ever invented when absent:

    * `connection_id` names the Zoho ORGANISATION this receipt came from.
      `ux_grn_external_identity` is UNIQUE NULLS NOT DISTINCT on
      `(connection_id, external_source, external_id)`, so a NULL here behaves
      exactly as 013's estate-wide `ux_grn_external` did, and two different
      organisations legitimately mirroring the same receive id do not collide.
    * `line_no` is the receive line's ordinal within its receipt.
      `ux_grn_line_external_v2` carries it so two GENUINELY DISTINCT lines on
      one receive stay distinct; NULL keeps the pre-014 behaviour, which is now
      NULLS NOT DISTINCT and therefore idempotent where 013's index was not.
    * `external_status_raw` is C17's verbatim copy on the document row. NOT
      mapped, NOT trimmed, NOT interpreted -- `grn.status` is still never
      written from it, for the reason `_mirror_grn_header` gives.

    THE LEDGER IS REFRESHED AT THE END. A receipt moves `received_paise` and
    `received_not_billed_paise` on its PO line's control cell, and before 014
    nothing on the inbound path recomputed either.
    """
    moment = now or store._utcnow()
    observed = received_at or moment
    receive_external_id = store._require(
        receive_external_id, code="BLANK_RECEIVE_EXTERNAL_ID",
        what="receive_external_id")
    po_line_id = store._require(po_line_id, code="BLANK_PO_LINE_ID",
                                what="po_line_id")
    # PROVENANCE IS MANDATORY, and this is where a NULL used to get in. The
    # check itself sits below, AFTER the control cell has been read: the order
    # of refusals is part of the contract this surface already had, and a
    # missing amount (422) and an unreachable PO line (404) both say more about
    # a document than a missing source label does. Nothing is written before
    # any of them.
    #
    # `external_source` was optional, `sweeps.SweepPoAnchored.external_source`
    # defaults to `None`, and nothing constructs it with a value -- so every
    # real receive took a second INSERT branch that wrote neither
    # `external_last_modified` nor `payload_sha`, and §6.1's four-column
    # provenance block came out ONE of four populated on every mirrored GRN.
    #
    # Three defects followed from that one NULL, and requiring the value here
    # closes all three at once rather than patching them separately:
    #
    #   * §11.10 requires the SOURCE DOCUMENT to be recoverable. A row naming
    #     neither its source nor which VERSION of it we hold is not.
    #   * `ux_grn_external ON grn (external_source, external_id)` is NULLS
    #     DISTINCT in its LEADING column, so with `external_source` NULL the
    #     index constrains nothing at all and a replayed receive could
    #     duplicate past it.
    #   * `grn_id` is derived from `(external_source, receive_external_id)`
    #     while `grn_number` is derived from the receive id alone. The day a
    #     deployment set `external_source`, every already-mirrored receive
    #     would derive a DIFFERENT `grn_id` and the SAME `grn_number`,
    #     violating `ux_grn_number` -- which no `ON CONFLICT` target here
    #     covers -- aborting the transaction and killing the sweep on every
    #     tick, with DELETE revoked so nothing could be tidied.
    #
    # Refused rather than defaulted: a source label we invented would make the
    # row LOOK traceable while pointing at the wrong tenant.

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
    po_id, _project_id, cell_wbs_id, cell_head_id = _po_line_cell(
        session, po_line_id)
    # ...and only now the provenance, for the reason given at the top of this
    # function: still before anything is written, and after the two refusals
    # that say more about the document than a missing source label does.
    external_source = store._require(
        external_source, code="RECEIVE_EXTERNAL_SOURCE_MISSING",
        what="external_source")
    grn_id = _mirror_grn_header(
        session, po_id=po_id, receive_external_id=receive_external_id,
        external_source=external_source, receive_number=receive_number,
        received_at=observed, external_last_modified=external_last_modified,
        payload_sha=payload_sha, is_reversal=is_reversal,
        connection_id=connection_id, external_status_raw=external_status_raw,
        actor=actor)

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
        "line_no": None if line_no is None else int(line_no),
        "external_status_raw": external_status_raw,
        "actor": actor,
    }

    # THE CONFLICT TARGET IS `ux_grn_line_external_v2`'s FOUR columns.
    #
    # 013's `ux_grn_line_external` used PostgreSQL's DEFAULT NULLS DISTINCT,
    # which meant it did not constrain a receive line carrying no external line
    # id -- and on Zoho ERP that is THE ORDINARY CASE, because ERP publishes no
    # receives-list endpoint and lines are discovered PO-anchored. The sweeps
    # re-walk by design, so every walk re-inserted and `received` climbed with
    # no new receive arriving. Migration 014 replaces it with
    # `UNIQUE NULLS NOT DISTINCT (po_line_id, receive_external_id,
    # line_external_id, line_no)`.
    #
    # THE UPDATE-FIRST PATH THAT USED TO STAND HERE IS GONE, and its removal is
    # the fix rather than a tidy-up. It existed only because the old index
    # could not see a NULL `line_external_id`, so a read-then-write window was
    # opened by hand and its safety rested on "one cron worker per connection",
    # which is an operational fact and not a constraint. `ON CONFLICT` now
    # matches those rows, so the window is closed by the database instead --
    # and CONCURRENT duplicate ingestion of the same identifierless line is
    # refused by the index rather than racing.
    #
    # WHAT THAT MEANS FOR THE SETTLEMENT, which the repair batch attached to
    # the deleted branch. `_settle_receive_line` used to be called twice --
    # once on the update-first hit and once after the INSERT -- because there
    # were two ways a line could land. There is now ONE, so it is called once,
    # below, on the row this statement returns. The verb is unchanged and no
    # line reaches the ledger without it; only the duplicate call site went,
    # with the branch that needed it.
    #
    # The index is a plain (non-partial) unique index, so the column list alone
    # infers it; there is no predicate to name, unlike the two partial mirror
    # indexes on `grn`.
    written = repo.query(
        session,
        f"""
        INSERT INTO {GRN_LINE} (
            grn_line_id, grn_id, po_id, po_line_id, receive_external_id,
            line_external_id, line_no, quantity, amount_paise,
            external_status_raw, created_by, updated_by)
        SELECT %(grn_line_id)s, %(grn_id)s, %(po_id)s, pl.po_line_id,
               %(receive_external_id)s, %(line_external_id)s, %(line_no)s,
               %(quantity)s::numeric, %(amount_paise)s,
               %(external_status_raw)s, %(actor)s, %(actor)s
        FROM {PO_LINE} pl
        JOIN project p ON p.project_id = pl.project_id
        WHERE pl.po_line_id = %(po_line_id)s AND {{scope}}
        ON CONFLICT (po_line_id, receive_external_id, line_external_id, line_no)
        DO UPDATE SET quantity = EXCLUDED.quantity,
                      amount_paise = EXCLUDED.amount_paise,
                      external_status_raw = EXCLUDED.external_status_raw,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
                      version_no = {GRN_LINE}.version_no + 1
        RETURNING grn_line_id
        """,
        params, columns=SCOPE_COLUMNS)
    if not written:
        raise ProcurementIngestError(
            "PO_LINE_NOT_FOUND",
            f"po_line {po_line_id!r} vanished from scope between the cell "
            f"lookup and the line INSERT, so receive {receive_external_id!r} "
            f"has NO line and nothing was written. Reported rather than "
            f"skipped: a receipt silently not mirrored is the drop §11.8 "
            f"forbids.", status=404)
    # A receipt moves `received_paise` and `received_not_billed_paise` on this
    # PO line's control cell. Before migration 014 nothing on the inbound path
    # recomputed either, so both sat at their DEFAULT 0 for ever while
    # `check_availability` subtracted them.
    #
    # BEFORE THE SETTLEMENT, NOT AFTER, and the order is Rule 3 of
    # `locking.py`'s global order rather than preference: cells first, the
    # document rows second, the advisory audit lock LAST.
    # `_settle_receive_line` takes that audit lock (and `retract_quarantine`
    # takes it again), so every cell lock this function will ever need must
    # already be held when it is called. The recompute still has to run after
    # the INSERT above -- it derives `received` FROM the row just written --
    # which is why this sits between the write and the settlement rather than
    # at the top of the function.
    svc.refresh_cells_after_ingest(
        session, [(cell_wbs_id, cell_head_id)], actor=actor)
    _settle_receive_line(
        session, grn_id=grn_id, grn_line_id=written[0][0],
        po_line_id=po_line_id, receive_external_id=receive_external_id,
        line_external_id=params["line_external_id"],
        quantity=params["quantity"], amount_paise=params["amount_paise"],
        external_source=external_source, is_reversal=is_reversal,
        actor=actor, now=moment)


def _settle_receive_line(session: Session, *, grn_id: str, grn_line_id: str,
                         po_line_id: str, receive_external_id: str,
                         line_external_id: str | None, quantity: str,
                         amount_paise: int, external_source: str,
                         is_reversal: bool, actor: str,
                         now: datetime) -> None:
    """What happens AFTER a receive line is in the ledger: audit, then retract.

    AUDIT FIRST, RETRACTION SECOND, both inside the caller's transaction. The
    entry records the movement this row makes -- `received` rises, and with it
    `received_not_billed` -- and the retraction closes the quarantine an
    earlier pass raised for the SAME line, which is what stops the value being
    counted once in `grn_line` and again in `open_exception_exposure`.

    THE QUARANTINE KEY IS RECONSTRUCTABLE HERE, and only because of what
    `sweeps.SweepPoAnchored._attribute` does. Its exception's `object_id` is
    ``f"{receive_external_id}:{line_key}"`` where `line_key` falls back to the
    line's ORDINAL when the source gave no identifier -- and an identifierless
    line can never reach this function at all, because `resolve_po_line`
    returns `None` for a null line id and the sweep quarantines it. So for
    every line that CAN make the quarantine-then-attribute transition,
    `line_key` is exactly `line_external_id`, and no ordinal has to be carried
    through the store surface to find the row.
    """
    _audit(
        session, actor=actor, action=AUDIT_RECEIVE_MIRRORED,
        object_type="GoodsReceipt", object_id=grn_id,
        detail=(f"Receive {receive_external_id} line "
                f"{line_external_id or '(no line identifier)'} mirrored from "
                f"{external_source} onto po_line {po_line_id} as "
                f"{grn_line_id}: quantity {quantity}, {amount_paise} paise"
                f"{', REVERSAL' if is_reversal else ''}. Received rises by "
                f"this amount; commitment is unchanged, because a receipt "
                f"does not relieve a commitment -- a bill does."))
    if line_external_id is None:
        return
    kind, object_type = QUARANTINE_RECEIVE_LINE
    retract_quarantine(
        session, kind=kind, object_type=object_type,
        object_id=f"{receive_external_id}:{line_external_id}",
        reason=(f"Receive line resolved to po_line {po_line_id} and is posted "
                f"as {grn_line_id}. Held at full value while the linkage was "
                f"absent; retracted now that it is not."),
        actor=actor, now=now)


def _mirror_grn_header(session: Session, *, po_id: str,
                       receive_external_id: str,
                       external_source: str,
                       receive_number: str | None,
                       received_at: datetime,
                       external_last_modified: datetime | None,
                       payload_sha: str | None,
                       is_reversal: bool,
                       actor: str,
                       connection_id: str | None = None,
                       external_status_raw: str | None = None) -> str:
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

    THERE IS ONE INSERT BRANCH, and there used to be two. The second fired
    whenever `external_source` was absent and wrote NEITHER
    `external_last_modified` NOR `payload_sha` -- so on the only path anything
    actually took, §6.1's four-column block came out one of four populated and
    the source document was not recoverable at all. `record_receive_line` now
    REFUSES a blank `external_source` (see the comment there for the three
    separate defects that NULL caused), which leaves nothing for a second
    branch to handle. Deleting it is what guarantees `grn.external_source` is
    never NULL, and therefore that `ux_grn_external` -- NULLS DISTINCT in its
    leading column -- is a constraint rather than a decoration.
    """
    params = {
        "grn_id": derived_id("GRN", external_source, receive_external_id),
        # Derived from the same two values as `grn_id`, so a replay produces
        # the same number and `ux_grn_number` is satisfied rather than
        # violated. A sequence would mint a second number for one receipt.
        "grn_number": receive_number or f"RCV-{receive_external_id}",
        "po_id": po_id, "received_at": received_at,
        "is_reversal": bool(is_reversal),
        "external_source": external_source,
        "external_id": receive_external_id,
        "external_last_modified": external_last_modified,
        "payload_sha": payload_sha,
        "connection_id": _text(connection_id),
        "external_status_raw": external_status_raw,
        "actor": actor,
    }
    # `grn.entity_id` is NOT NULL from migration 014 and is READ from
    # `po -> project`, never accepted from the caller -- the same rule
    # `_purchase_order` follows for the same column, and for the same reason: a
    # goods receipt filed under the wrong entity is invisible to the people who
    # triage it. It is the first column of `ux_grn_number_scoped`, so getting
    # it from anywhere but the row's own join would scope the number wrongly.
    #
    # THERE IS ONE INSERT BRANCH, and the `if external_source:` that used to
    # choose between two is gone with the second. See this function's
    # docstring: `record_receive_line` now REFUSES a blank `external_source`,
    # so the branch that handled one had nothing left to handle and its
    # deletion is what makes `grn.external_source` never NULL. The 014 columns
    # below therefore land on the ONLY path a receipt can take, rather than on
    # the one real receives never reached.
    #
    # `ux_grn_external_identity` is PARTIAL -- `WHERE external_id IS NOT NULL`
    # -- so the predicate is written out alongside the columns. Inference by
    # columns alone matches no index on this table and PostgreSQL refuses
    # rather than choosing another: the good failure, but only once the
    # predicate is there to be matched.
    #
    # THREE COLUMNS, not 013's two. `connection_id` leads, so the same external
    # receive id under two Zoho organisations no longer collides; the index is
    # NULLS NOT DISTINCT, so a NULL `connection_id` still behaves exactly as
    # 013's estate-wide index did and replay stays idempotent for every
    # existing row. This is `ux_grn_external_identity`, which migration 014
    # created in place of the `ux_grn_external` the batch-A comment named --
    # the refusal above still does what that comment credits it with, because
    # `external_source` remains a non-leading column of the replacement.
    rows = repo.query(
        session,
        f"""
        INSERT INTO {GRN} (
            grn_id, grn_number, po_id, entity_id, received_at, is_reversal,
            connection_id, external_source, external_id,
            external_last_modified, payload_sha, external_status_raw,
            created_by, updated_by)
        SELECT %(grn_id)s, %(grn_number)s, po.po_id, p.entity_id,
               %(received_at)s, %(is_reversal)s, %(connection_id)s,
               %(external_source)s, %(external_id)s,
               %(external_last_modified)s, %(payload_sha)s,
               %(external_status_raw)s, %(actor)s, %(actor)s
        FROM {PURCHASE_ORDER} po
        JOIN project p ON p.project_id = po.project_id
        WHERE po.po_id = %(po_id)s AND {{scope}}
        ON CONFLICT (connection_id, external_source, external_id)
            WHERE external_id IS NOT NULL
        DO UPDATE SET external_last_modified = EXCLUDED.external_last_modified,
                      payload_sha = EXCLUDED.payload_sha,
                      external_status_raw = EXCLUDED.external_status_raw,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
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
                              default_status: str,
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

    `default_status` IS THE CALLER'S DEFAULT AND IS REQUIRED, which is the
    whole of the H-1 correction. Both silent branches -- a blank raw value, and
    a C17 `BUSINESS_STATUS` / `NO_BUSINESS_STATUS` row that maps the value
    without saying anything about accounting effect -- used to return the
    LITERAL ``"Approved"`` while their comments said "the caller's own default
    stands". `mirror_bill` then assigned that back over its own argument, so a
    bill passed ``accounting_status="Void"`` with any non-blank raw status came
    out **Approved**: accounting-effective, relieving commitment and raising
    actual on a voided document. The comment and the code now agree, and the
    value is validated here rather than trusted, because a caller's typo
    reaching `bill.accounting_status` is refused by
    `ck_bill_accounting_status` at 3am instead.

    C17's own words: "Raw Zoho status values are stored VERBATIM and NEVER
    overwritten... An UNMAPPED raw value never guesses: the record is accepted,
    the raw value preserved, and a reconciliation_exception of kind
    UNMAPPED_EXTERNAL_STATUS is raised."
    """
    if default_status not in ACCOUNTING_STATUSES:
        raise ProcurementIngestError(
            "UNKNOWN_ACCOUNTING_STATUS",
            f"default_status {default_status!r} is not one of "
            f"ck_bill_accounting_status's ({', '.join(ACCOUNTING_STATUSES)}). "
            f"Nothing was interpreted: silently substituting a status this "
            f"function is supposed to be preserving is the defect it was "
            f"corrected for.", status=422)
    # Imported inside the function: `integration/statuses.py` loads and
    # validates the C17 contract from `research/` at import time, and the pg
    # layer must not pay that cost -- or acquire that dependency -- merely by
    # being imported.
    from ..integration import statuses as status_registry

    if raw is None or not str(raw).strip():
        return default_status, None

    result = status_registry.zoho_status_map().resolve(
        status_registry.contract_product(adapter_product, object),
        object, field, str(raw), as_of=as_of)

    if result.is_mapped:
        mapped = result.accounting_status
        if mapped is None:
            # `NO_BUSINESS_STATUS` / `BUSINESS_STATUS` rows map a raw value
            # without saying anything about accounting effect. That is a
            # deliberate silence in C17, not a gap, so the caller's own default
            # stands and no exception is raised. THE CALLER'S -- not the
            # literal "Approved" this used to return while saying otherwise.
            return default_status, None
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
                connection_id: str | None = None,
                vendor_id: str | None = None,
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

    A REVISED BILL IS RECONCILED, NOT MERELY UPSERTED. This used to write the
    lines it was given and say nothing about the lines that had DISAPPEARED
    from the source, so a bill revised from three lines to two kept the removed
    line contributing to `billed` -- and therefore to `actual` -- for ever,
    with DELETE revoked so it could not be tidied. Every `bill_line` under this
    bill that this pass did not write is now SUPERSEDED: its money zeroed so it
    stops counting, its row kept and marked so the trail still says what it
    was. See :func:`_supersede_withdrawn_bill_lines`.
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
    # `bill.bill_date` is NOT NULL and there is no honest filler for it either.
    # Today's date would be a claim about when the vendor raised the document
    # -- the date every ageing report, every period assignment and
    # `LATE_ARRIVAL_CLOSED_PERIOD` are computed from. Refused here so the
    # report names the bill, rather than at the NOT NULL inside a cron
    # function.
    if bill_date is None:
        raise ProcurementIngestError(
            "BLANK_BILL_DATE",
            f"Bill {external_id!r} carries no bill_date. `bill.bill_date` is "
            f"NOT NULL and today's date is a claim about when the vendor "
            f"raised the document, not a missing-value convention: it is what "
            f"decides the document's period and therefore whether it is a "
            f"late arrival into a closed one. Nothing was written.",
            status=422)
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
        object_id=external_id,
        # THE CALLER'S DEFAULT, PASSED IN. `resolve_accounting_status` used to
        # return the literal "Approved" on both of its silent branches, and
        # the assignment below then wrote that over a `Void` this caller had
        # explicitly stated -- making a voided document accounting-effective,
        # relieving commitment and raising actual on it. It now hands back
        # exactly this value when C17 says nothing.
        default_status=accounting_status,
        entity_id=entity_id, project_id=project_id,
        correlation_id=correlation_id, actor=actor, now=moment,
        # EXPLICIT, and injectable. C17 rows carry an effective window and the
        # resolver compares it against `date.today()` by default, so a mapping
        # that is correct today silently becomes UNMAPPED the day a window
        # closes -- and a test written without this pins nothing. Same reason
        # the VRT suite injects its clock: a check whose answer depends on the
        # wall clock is a check that reports drift it did not cause.
        as_of=status_as_of)
    # UNCONDITIONAL, and safe only because the resolver now returns the
    # caller's own default for a blank raw value. The guard this replaces
    # existed to stop a blank status being overwritten with "Approved"; with
    # H-1 fixed there is nothing to guard against, and a second rule about
    # when the resolver's answer counts is a second place to get it wrong.
    accounting_status = effective_status

    params = {
        "bill_id": bill_id, "bill_number": bill_number, "po_id": po_id,
        # `entity_id` is deliberately NOT here. `bill.entity_id` is NOT NULL
        # from migration 014 and is read from `p.entity_id` in the INSERT's own
        # join, never bound from the caller: it is the first column of
        # `ux_bill_number_scoped`, and an entity supplied from anywhere but the
        # row's own join would scope the vendor's bill number wrongly. The
        # local `entity_id` variable is still used -- it is what a quarantine
        # exception is raised under, so a triage principal restricted by entity
        # can actually see it.
        "project_id": project_id,
        "vendor_name": vendor_name, "vendor_id": _text(vendor_id),
        "bill_date": bill_date, "accounting_status": accounting_status,
        "is_reversal": bool(is_reversal),
        "reverses_bill_id": reverses_bill_id, "doc_type": doc_type,
        "connection_id": _text(connection_id),
        "external_source": external_source, "external_id": external_id,
        "external_last_modified": external_last_modified,
        "payload_sha": payload_sha,
        # C17's verbatim copy, on the document row. NOT the mapped value:
        # `accounting_status` above is what C17 resolved this to, and this is
        # what the vendor actually sent. `resolve_accounting_status` has
        # already raised UNMAPPED_EXTERNAL_STATUS if it could not interpret it,
        # and the document is accepted either way -- with the raw value
        # preserved, which is what this column is for.
        "external_status_raw": external_status_raw,
        "actor": actor,
    }
    # `ux_bill_external` is PARTIAL. Both the columns AND the predicate, for
    # the reason `_mirror_grn_header` states.
    #
    # `bill.status` is NOT in the SET list: it is our domain status, and the
    # raw external one is preserved verbatim in the inbox. `project_id` is not
    # in it either -- a mirrored bill that changed project would be a different
    # commitment, and `fk_bill_po_project` should be the thing that refuses it,
    # not an UPDATE that quietly succeeds.
    # THREE COLUMNS IN THE CONFLICT TARGET, not 013's two. `connection_id`
    # leads `ux_bill_external_identity`, so the same external bill id under two
    # Zoho organisations no longer collides -- which was the whole of D1. The
    # index is NULLS NOT DISTINCT, so a NULL `connection_id` behaves exactly as
    # 013's estate-wide `ux_bill_external` did and replay stays idempotent for
    # every row written before 014.
    #
    # `entity_id` is NOT NULL from 014 and is READ from `project`, never taken
    # from the caller: it is the first column of `ux_bill_number_scoped`, so an
    # entity supplied from anywhere but the row's own join would scope the
    # vendor's bill number wrongly.
    rows = repo.query(
        session,
        f"""
        INSERT INTO {BILL} (
            bill_id, bill_number, po_id, project_id, entity_id, vendor_name,
            vendor_id, bill_date, accounting_status, is_reversal,
            reverses_bill_id, doc_type, connection_id, external_source,
            external_id, external_last_modified, payload_sha,
            external_status_raw, created_by, updated_by)
        SELECT %(bill_id)s, %(bill_number)s, %(po_id)s, p.project_id,
               p.entity_id, %(vendor_name)s, %(vendor_id)s, %(bill_date)s,
               %(accounting_status)s, %(is_reversal)s, %(reverses_bill_id)s,
               %(doc_type)s, %(connection_id)s,
               %(external_source)s, %(external_id)s,
               %(external_last_modified)s, %(payload_sha)s,
               %(external_status_raw)s, %(actor)s, %(actor)s
        FROM project p
        WHERE p.project_id = %(project_id)s AND {{scope}}
        ON CONFLICT (connection_id, external_source, external_id)
            WHERE external_id IS NOT NULL
        DO UPDATE SET vendor_name = EXCLUDED.vendor_name,
                      vendor_id = EXCLUDED.vendor_id,
                      bill_date = EXCLUDED.bill_date,
                      accounting_status = EXCLUDED.accounting_status,
                      doc_type = EXCLUDED.doc_type,
                      external_last_modified = EXCLUDED.external_last_modified,
                      payload_sha = EXCLUDED.payload_sha,
                      external_status_raw = EXCLUDED.external_status_raw,
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
    attributed_paise = 0
    exceptions: list[str] = []
    touched_cells: list[tuple[str, str]] = []
    written_line_ids: list[str] = []
    # SEEN KEYS, and this is a silent-drop fix rather than bookkeeping.
    #
    # `bill_line_id` is derived from `(bill_id, line_key)` so a replay lands on
    # the same row. Until now `line_key` fell back to the PO LINE's external id,
    # and two bill lines against ONE purchase-order line -- a partial claim and
    # its balance, a wholly ordinary pair -- produced the SAME key, the same
    # derived id, and the second silently overwrote the first. A whole line's
    # value disappeared through the code written to prevent exactly that.
    #
    # The ordinal is appended only to the SECOND and later occurrence of a key
    # within one payload, so every id written before this change is byte
    # identical and no replay of an already-mirrored bill creates a second row
    # -- which matters more here than anywhere else in this module, because
    # `capex_app` has DELETE revoked and a duplicate could not be removed.
    seen_keys: set[str] = set()
    for index, line in enumerate(lines or ()):
        outcome = _mirror_bill_line(
            session, bill_id=bill_id, bill_external_id=external_id,
            po_id=po_id, po_external_id=po_external_id, line=line,
            index=index, project_id=project_id, entity_id=entity_id,
            seen_keys=seen_keys, correlation_id=correlation_id, actor=actor,
            now=moment)
        if outcome["attributed"]:
            attributed += 1
            attributed_paise += int(outcome["amount_paise"])
            written_line_ids.append(outcome["bill_line_id"])
            if outcome["cell"] is not None:
                touched_cells.append(outcome["cell"])
        else:
            quarantined += 1
            quarantined_paise += abs(int(outcome["amount_paise"]))
            exceptions.append(outcome["exception_id"])

    # WITHDRAWN LINES FIRST, because they MOVE MONEY and the refresh below has
    # to see them zeroed. A bill revised from three lines to two leaves the
    # third contributing to `billed` -- and through it to `actual` -- until
    # this runs, so a refresh taken before it would re-derive the cell from
    # the very figure this statement is about to withdraw.
    superseded = _supersede_withdrawn_bill_lines(
        session, bill_id=bill_id, keep=written_line_ids, actor=actor,
        now=moment)

    # A superseded line's cell is an AFFECTED cell even though this pass wrote
    # no line to it. Its money just left; if it is not in the refresh set, the
    # cell keeps quoting the withdrawn amount for ever -- which is the same
    # class of defect as `actual_paise` having no writer, arriving by a
    # different door. Merged into `touched_cells` rather than refreshed
    # separately so `lock_affected_cells` is still called ONCE with the
    # COMPLETE set, per Rule 1 of `locking.py`'s global order.
    for row in superseded:
        cell = (row["wbs_id"], row["budget_head_id"])
        if cell not in touched_cells:
            touched_cells.append(cell)

    # THE HALF THAT MAKES THE OVER-COMMITMENT HOLE ACTUALLY CLOSED.
    #
    # `check_availability` computes `commitment + actual + pr_reserved`.
    # `commitment_paise` is `max(0, ordered - billed)` and FALLS when this bill
    # lands. `actual_paise` must RISE by the same amount, and before migration
    # 014 it had no writer anywhere in the PostgreSQL path -- so available rose
    # by the billed amount and the same budget could be committed again.
    #
    # Both limbs are re-derived here, in one pass, under the cell locks
    # `refresh_cells_after_ingest` takes. A bill that quarantined every line
    # and superseded nothing touches no cell and correctly refreshes nothing.
    #
    # BEFORE EVERY AUDIT APPEND, and that is Rule 3 of `locking.py`'s global
    # order rather than taste: cells first, the document rows second, the
    # advisory audit lock LAST. `audit_mod.append` takes an advisory lock on
    # the stream, so a transaction that appended first and locked cells second
    # would wait on cells while holding the stream, against another that holds
    # the cells and wants the stream. That is the deadlock the order exists to
    # forbid, which is why the supersede entries below are emitted here rather
    # than inside `_supersede_withdrawn_bill_lines` alongside its UPDATE.
    svc.refresh_cells_after_ingest(session, touched_cells, actor=actor)

    _audit_superseded_bill_lines(
        session, bill_id=bill_id, bill_external_id=external_id,
        superseded=superseded, actor=actor, correlation_id=correlation_id)

    _audit(
        session, actor=actor, action=AUDIT_BILL_MIRRORED,
        object_type="VendorBill", object_id=bill_id,
        detail=(f"{doc_type} {bill_number} ({external_source} "
                f"{external_id}) mirrored on project {project_id} for "
                f"{vendor_name}, dated {bill_date}, accounting status "
                f"{accounting_status} "
                f"({'effective' if accounting_status in ACCOUNTING_EFFECTIVE else 'NOT effective'}"
                f"). {attributed} line(s) attributed at {attributed_paise} "
                f"paise; {quarantined} quarantined holding "
                f"{quarantined_paise} paise; "
                f"{len(superseded)} line(s) superseded and withdrawn from "
                f"billed and actual."),
        correlation_id=correlation_id)

    return {
        "bill_id": bill_id, "project_id": project_id, "po_id": po_id,
        "attributed": attributed, "quarantined": quarantined,
        "superseded": len(superseded),
        # The IDS, not the rows. `_supersede_withdrawn_bill_lines` returns each
        # withdrawn line WITH its control cell so the refresh above can include
        # it; this surface's contract is the ids, unchanged.
        "superseded_line_ids": tuple(row["bill_line_id"] for row in superseded),
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
                      seen_keys: set[str] | None = None,
                      correlation_id: str | None = None, actor: str = "SVC-SWEEP",
                      now: datetime | None = None) -> dict[str, Any]:
    """One bill line: attributed to its PO line's control cell, or quarantined.

    The line's identity falls back to its ORDINAL when the source gave it none.
    That is not cosmetic: `ux_reconciliation_exception_open` keys on
    `object_id`, so two identifierless lines keyed on one constant would
    collapse into ONE exception and the second line's value would vanish -- a
    silent drop arriving through the code written to prevent one. The same
    defect was found and fixed in `sweeps._attribute`; this is the same
    fallback, for the same reason.

    TWO IDENTITIES, AND THEY ARE NOT THE SAME THING. `po_line_external_id` is
    the PURCHASE ORDER LINE's id -- what 013 named the column for, because the
    POC's `bill_line.zoho_purchaseorder_item_id` held exactly that.
    `external_line_id` (migration 014) is THIS line's own id, which the source
    may or may not supply. Only the second is an identity for this row, which
    is why `ux_bill_line_external` keys on it and why the fingerprint index
    takes over when it is absent.

    Reading the bill line's own id from explicit names only
    (`bill_line_external_id`, `external_line_id`, `line_id`) is deliberate: the
    PO-line lookup above already claims `line_item_id`, and re-using that name
    for both would make the two identities the same value again.
    """
    line_external_id = _text(_line_field(
        line, "purchase_order_line_external_id", "po_line_external_id",
        "line_external_id", "line_item_id", "purchaseorder_item_id"))
    external_line_id = _text(_line_field(
        line, "bill_line_external_id", "external_line_id", "line_id"))
    line_key = line_external_id or f"#{index}"
    # See `mirror_bill`'s `seen_keys` note: two bill lines against ONE purchase
    # order line are ordinary, and until this they collided on one derived id
    # and the second overwrote the first. Only the repeat is disambiguated, so
    # every id written before migration 014 is unchanged.
    if seen_keys is not None:
        if line_key in seen_keys:
            line_key = f"{line_key}#{index}"
        seen_keys.add(line_key)
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
                    "bill_line_id": None, "amount_paise": amount_paise,
                    "cell": None}

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
        "external_line_id": external_line_id,
        # The ORDINAL within this bill, 1-based. It is an input to
        # `bill_line.line_fingerprint` (a GENERATED column, migration 014),
        # which is what stops two genuinely distinct lines that are identical
        # in every other field from collapsing into one row -- and, equally,
        # what makes a replay of the same payload produce the same fingerprint
        # and therefore the same row.
        "line_no": index + 1,
        "external_status_raw": None,
        "actor": actor,
    }
    # THE CONFLICT TARGET IS STILL THE PRIMARY KEY, and that is deliberate even
    # now that migration 014 gives `bill_line` two unique indexes.
    #
    # `bill_line_id` is DERIVED from `(bill_id, line_key)`, so a replay lands on
    # the same row -- which matters more here than anywhere else in this
    # module, since `capex_app` has DELETE revoked and a duplicated line could
    # not be removed afterwards. Conflicting on the primary key keeps that
    # property for every row written before 014, whose `external_line_id` is
    # NULL and whose fingerprint is computed from columns this statement is
    # about to overwrite.
    #
    # `ux_bill_line_external` and `ux_bill_line_fingerprint` are the BACKSTOP,
    # exactly as RLS is the backstop for `repo.compile_scope`: if a future
    # caller ever derives an id differently, or two writers race on the same
    # identifierless line, the database refuses the second rather than
    # admitting a duplicate the application cannot delete.
    #
    # THE CONTROL CELL IS IN THE SET LIST, and its absence was a defect of its
    # own. `po_id`, `po_line_id`, `wbs_id` and `budget_head_id` are the four
    # columns `fk_bill_line_po_line_cell` binds together, and they were omitted
    # -- so a line re-mirrored after its linkage changed kept its ORIGINAL
    # control cell while every other column moved, and the money stayed posted
    # against a WBS element and budget head the source no longer names.
    written = repo.query(
        session,
        f"""
        INSERT INTO {BILL_LINE} (
            bill_line_id, bill_id, po_id, po_line_id, wbs_id, budget_head_id,
            description, quantity, amount_paise, non_creditable_tax_paise,
            freight_paise, po_line_external_id, external_line_id, line_no,
            external_status_raw, created_by, updated_by)
        SELECT %(bill_line_id)s, %(bill_id)s, %(po_id)s, %(po_line_id)s,
               %(wbs_id)s, %(budget_head_id)s, %(description)s,
               %(quantity)s::numeric, %(amount_paise)s,
               %(non_creditable_tax_paise)s, %(freight_paise)s,
               %(po_line_external_id)s, %(external_line_id)s, %(line_no)s,
               %(external_status_raw)s, %(actor)s, %(actor)s
        FROM {BILL} b
        JOIN project p ON p.project_id = b.project_id
        WHERE b.bill_id = %(bill_id)s AND {{scope}}
        ON CONFLICT (bill_line_id)
        DO UPDATE SET po_id = EXCLUDED.po_id,
                      po_line_id = EXCLUDED.po_line_id,
                      wbs_id = EXCLUDED.wbs_id,
                      budget_head_id = EXCLUDED.budget_head_id,
                      po_line_external_id = EXCLUDED.po_line_external_id,
                      description = EXCLUDED.description,
                      quantity = EXCLUDED.quantity,
                      amount_paise = EXCLUDED.amount_paise,
                      non_creditable_tax_paise = EXCLUDED.non_creditable_tax_paise,
                      freight_paise = EXCLUDED.freight_paise,
                      external_line_id = EXCLUDED.external_line_id,
                      line_no = EXCLUDED.line_no,
                      updated_at = now(),
                      updated_by = EXCLUDED.updated_by,
                      version_no = {BILL_LINE}.version_no + 1
        RETURNING bill_line_id
        """,
        params, columns=SCOPE_COLUMNS)
    if not written:
        raise ProcurementIngestError(
            "BILL_PROJECT_NOT_FOUND",
            f"Bill {bill_id!r} is not visible to this principal, so line "
            f"{line_key!r} was NOT written. Reported rather than counted as "
            f"attributed: a line nobody can read is not a line that posted.",
            status=404)
    bill_line_id = written[0][0]

    # THE OTHER HALF OF THE DOUBLE COUNT. If an earlier pass could not resolve
    # this line -- which is the ORDINARY sequence, not an edge case, because
    # the sweeps re-walk on a 300-second overlap and the PO poll and the bill
    # poll are not ordered against each other -- its full value is sitting in
    # an Open exception AND is now in `bill_line` as well.
    # `open_exception_exposure` sums the first; `billed` and `actual` sum the
    # second; the capitalisation gate and the period close quote both. Closed
    # in the SAME transaction as the row that made it wrong.
    kind, object_type = QUARANTINE_BILL_LINE
    retract_quarantine(
        session, kind=kind, object_type=object_type,
        object_id=f"{bill_external_id}:{line_key}",
        reason=(f"Bill line attributed to control cell "
                f"({wbs_id}, {budget_head_id}) and posted as "
                f"{bill_line_id}. Held at full value while it named neither a "
                f"resolvable po_line nor a cell; retracted now that it does."),
        actor=actor, correlation_id=correlation_id, now=now)
    return {"attributed": True, "exception_id": None,
            "bill_line_id": bill_line_id, "amount_paise": amount_paise,
            "cell": (wbs_id, budget_head_id)}


def _supersede_withdrawn_bill_lines(session: Session, *, bill_id: str,
                                    keep: Sequence[str], actor: str,
                                    now: datetime) -> list[dict[str, str]]:
    """Withdraw the `bill_line` rows a revised source bill no longer carries.

    THE DEFECT. `mirror_bill` upserted the lines it was given and never
    reconciled the ones that had DISAPPEARED. A bill revised at the source from
    three lines to two left the third contributing to `billed` -- and through
    it to `actual` and to the capitalisation base -- for ever, because
    `capex_app` has DELETE revoked and nothing else looked.

    NOT A DELETE, and not because DELETE is merely unavailable. The row is
    evidence: it records that this line was once on this bill and what it was
    worth, and §11.9's trace has to keep working across the revision. So it is
    made NON-EFFECTIVE instead -- `amount_paise`, `non_creditable_tax_paise`,
    `freight_paise` and `quantity` all zeroed, so every formula that sums them
    (`domain.compute_ledger`, :func:`reconciliation_lines`,
    :func:`reconcile_po_lines`) sees nothing without any of them needing a new
    predicate -- and its description is STAMPED, so the row says what happened
    rather than looking like a line that was always worth zero.

    THE STAMP IS ALSO THE IDEMPOTENCY GUARD. The sweeps re-walk, so this runs
    again on every pass with the same result; ``NOT LIKE 'SUPERSEDED %'``
    stops the second pass re-stamping and re-versioning a row it already
    withdrew. `version_no` would otherwise climb every fifteen minutes on a
    row nothing had touched.

    WHY A COLUMN WOULD BE BETTER, AND STILL IS NOT AVAILABLE. `bill_line` had
    no `is_effective` / `superseded_at` column in 013, and migration 014 has
    since landed WITHOUT adding one -- it was scoped to the identity, entity
    and status corrections. Zeroing therefore remains the equivalent that keeps
    the audit trail; the note that this "becomes a flag write when 014 lands
    the column" is left standing as the design it still points at, for
    whichever migration does add it.

    ZEROING MOVES MONEY, so the caller must refresh the affected cells --
    which is why the withdrawn rows are returned WITH their control cell and
    not merely as ids.

    IT NO LONGER APPENDS ITS OWN AUDIT ENTRIES, and that is the lock order
    rather than a change of intent. `audit_mod.append` takes an advisory lock
    on the audit stream, and Rule 3 of `locking.py` puts that LAST -- after the
    cell locks the caller's refresh takes. Emitting here would have appended
    before those locks were held. The entries are unchanged and are emitted by
    :func:`_audit_superseded_bill_lines`, which `mirror_bill` calls once the
    refresh has been done.

    Returns the rows withdrawn by THIS call: `bill_line_id`, `wbs_id` and
    `budget_head_id`.
    """
    rows = repo.query(
        session,
        f"""
        UPDATE {BILL_LINE} bl
        SET amount_paise = 0,
            non_creditable_tax_paise = 0,
            freight_paise = 0,
            quantity = 0,
            description = %(prefix)s || %(at)s || ' | '
                          || coalesce(bl.description, ''),
            updated_at = now(),
            updated_by = %(actor)s,
            version_no = bl.version_no + 1
        FROM {BILL} b
        JOIN project p ON p.project_id = b.project_id
        WHERE b.bill_id = bl.bill_id
          AND bl.bill_id = %(bill_id)s
          AND NOT (bl.bill_line_id = ANY(%(keep)s::text[]))
          AND (bl.description IS NULL
               OR bl.description NOT LIKE %(stamped)s)
          AND {{scope}}
        RETURNING bl.bill_line_id, bl.wbs_id, bl.budget_head_id
        """,
        {"bill_id": bill_id, "keep": list(keep), "actor": actor,
         "prefix": SUPERSEDED_DESCRIPTION_PREFIX, "at": now.isoformat(),
         "stamped": f"{SUPERSEDED_DESCRIPTION_PREFIX}%"},
        columns=SCOPE_COLUMNS)
    return [{"bill_line_id": row[0], "wbs_id": row[1],
             "budget_head_id": row[2]} for row in rows]


def _audit_superseded_bill_lines(session: Session, *, bill_id: str,
                                 bill_external_id: str,
                                 superseded: Sequence[Mapping[str, str]],
                                 actor: str,
                                 correlation_id: str | None) -> None:
    """The audit entries for :func:`_supersede_withdrawn_bill_lines`' rows.

    Split from the UPDATE that produces them for one reason: Rule 3 of
    `locking.py`'s global order takes the advisory audit lock LAST, and the
    caller has cell locks to acquire between the two. Same action, same object,
    same detail, same transaction -- only later in it.
    """
    for row in superseded:
        _audit(
            session, actor=actor, action=AUDIT_BILL_LINE_SUPERSEDED,
            object_type="VendorBill", object_id=bill_id,
            detail=(f"Bill line {row['bill_line_id']} no longer appears on "
                    f"source bill {bill_external_id}. Superseded, not "
                    f"deleted: its money is zeroed so it stops contributing "
                    f"to billed and to actual, and the row is kept and marked "
                    f"so the trail still says what it was."),
            correlation_id=correlation_id)


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

    The three sums are computed in separate scalar subqueries rather than by
    joining both child tables at once: a PO line with two receipts and two bill
    lines would otherwise produce four rows and every total would be doubled.
    That is the classic fan-out, and here it would double money.

    EVERY FORMULA BELOW IS TRANSCRIBED FROM `domain.compute_ledger`, which is
    the frozen `C5_formulas.json` registry in code, and all three of them had
    drifted from it:

      * `received_paise` summed `grn_line.amount_paise` with **no join to
        `grn`** -- so it counted VOID goods receipts, which both
        `compute_ledger` and :func:`reconciliation_lines` exclude, and it did
        not negate a reversal, which both spell
        ``SUM(CASE WHEN g.is_reversal THEN -ABS(...) ...)``. A receipt of
        +12,000,000 with a reversal GRN stored positive at 12,000,000 came out
        as 24,000,000 here and 0 there; add a Void GRN of 5,000,000 and the two
        differed by 2,90,000 rupees on ONE line.
      * `billed_paise` summed `bl.amount_paise` alone, dropping
        `non_creditable_tax_paise` and `freight_paise` -- which ARE part of the
        billed value everywhere else -- and did not negate a `Reversal` bill.
      * `ordered_paise` was `pl.amount_paise` alone, on the same three columns,
        so `open_paise` and `over_billed_paise` were both computed against a
        smaller order than the one the budget was checked against.

    Two derivations of one figure that disagree is worse than either being
    wrong on its own: it makes PostgreSQL and the SQLite ledger quote different
    numbers for the same estate, silently, and neither screen says which.
    """
    rows = repo.query(
        session,
        f"""
        SELECT pl.po_line_id, pl.po_id, pl.project_id, pl.wbs_id,
               pl.budget_head_id, pl.line_external_id,
               (pl.amount_paise
                + pl.non_creditable_tax_paise
                + pl.freight_paise)::bigint AS ordered_paise,
               -- `domain.compute_ledger`, verbatim: a Void GRN does not count
               -- at all, and a reversal subtracts the MAGNITUDE of its line so
               -- a reversal stored positive and one stored negative reduce
               -- received by the same amount.
               coalesce((SELECT SUM(CASE WHEN g.is_reversal
                                         THEN -ABS(gl.amount_paise)
                                         ELSE gl.amount_paise END)
                         FROM {GRN_LINE} gl
                         JOIN {GRN} g ON g.grn_id = gl.grn_id
                         WHERE gl.po_line_id = pl.po_line_id
                           AND g.status <> 'Void'), 0)::bigint
                   AS received_paise,
               -- AUD-C-004, verbatim: only accounting-effective bills, tax and
               -- freight included, a `Reversal` bill negated by its status.
               coalesce((SELECT SUM(CASE WHEN b.accounting_status = 'Reversal'
                                         THEN -ABS(bl.amount_paise
                                                   + bl.non_creditable_tax_paise
                                                   + bl.freight_paise)
                                         ELSE (bl.amount_paise
                                               + bl.non_creditable_tax_paise
                                               + bl.freight_paise) END)
                         FROM {BILL_LINE} bl
                         JOIN {BILL} b ON b.bill_id = bl.bill_id
                         WHERE bl.po_line_id = pl.po_line_id
                           AND b.accounting_status = ANY(%(effective)s)),
                        0)::bigint AS billed_paise
        FROM {PO_LINE} pl
        JOIN project p ON p.project_id = pl.project_id
        WHERE (%(project_id)s::text IS NULL OR pl.project_id = %(project_id)s)
          AND (%(po_id)s::text IS NULL OR pl.po_id = %(po_id)s)
          AND {{scope}}
        ORDER BY pl.po_id, pl.po_line_id
        LIMIT %(limit)s
        """,
        {"project_id": project_id, "po_id": po_id,
         # The SAME constant `reconciliation_lines` and `domain` read, never a
         # second literal list that can drift from it.
         "effective": list(store.ACCOUNTING_EFFECTIVE_BILL_STATES),
         "limit": max(int(limit), 0)},
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
    "AUDIT_BILL_LINE_SUPERSEDED",
    "AUDIT_BILL_MIRRORED",
    "AUDIT_QUARANTINE_RETRACTED",
    "AUDIT_RECEIVE_MIRRORED",
    "DOC_TYPES",
    "QUARANTINE_BILL_LINE",
    "QUARANTINE_RECEIVE_LINE",
    "SUPERSEDED_DESCRIPTION_PREFIX",
    "UNINTERPRETABLE_ACCOUNTING_STATUS",
    "ProcurementIngestError",
    "derived_id",
    "mirror_bill",
    "reconcile_po_lines",
    "reconciliation_lines",
    "record_receive_line",
    "resolve_accounting_status",
    "resolve_po_line",
    "retract_quarantine",
]
