"""Guarded business operations.

Every function here runs inside a single IMMEDIATE transaction so the check and
the write cannot be separated by a concurrent request. This is the correction for
AUD-C-001, where the API computed EXCEEDS_BUDGET and then wrote anyway, and where
two simultaneous amendments each passed their own check and both committed.

Routes stay thin: they authenticate, authorise, and delegate here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import auth, domain
from .money import format_inr, to_paise


class BusinessError(Exception):
    """A controlled, expected refusal. Maps to 4xx, never 500."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- transactions
class critical:
    """Serialise a check-then-write section.

    BEGIN IMMEDIATE takes SQLite's write lock at the start of the transaction, so a
    second connection attempting the same section blocks (and, on timeout, fails)
    rather than reading stale availability and overspending it.
    """

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self):
        self.con.isolation_level = None
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.con.execute("COMMIT")
        else:
            self.con.execute("ROLLBACK")
        self.con.isolation_level = ""
        return False


# ---------------------------------------------------------------- audit
def audit(con, actor: str, action: str, obj_type: str, obj_id: str, detail: str,
          correlation_id: str | None = None) -> None:
    """Append-only, hash-chained. Triggers prevent update and delete."""
    prev = con.execute("SELECT entry_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
    prev_hash = prev[0] if prev and prev[0] else ""
    at = now()
    payload = f"{prev_hash}|{at}|{actor}|{action}|{obj_type}|{obj_id}|{detail}"
    entry_hash = hashlib.sha256(payload.encode()).hexdigest()
    con.execute("""INSERT INTO audit_log
                   (at,actor,action,object_type,object_id,detail,prev_hash,entry_hash,correlation_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (at, actor, action, obj_type, obj_id, detail, prev_hash, entry_hash, correlation_id))


def verify_audit_chain(con) -> dict:
    """Detect tampering even by an identity that could bypass the triggers."""
    prev_hash, broken, n = "", [], 0
    for r in con.execute("""SELECT audit_id,at,actor,action,object_type,object_id,detail,
                                   prev_hash,entry_hash FROM audit_log ORDER BY audit_id"""):
        n += 1
        if r["entry_hash"] is None:
            continue                      # pre-migration rows carry no hash
        payload = f"{prev_hash}|{r['at']}|{r['actor']}|{r['action']}|{r['object_type']}|{r['object_id']}|{r['detail']}"
        if hashlib.sha256(payload.encode()).hexdigest() != r["entry_hash"]:
            broken.append(r["audit_id"])
        prev_hash = r["entry_hash"]
    return {"entries": n, "broken": broken, "intact": not broken}


# ---------------------------------------------------------------- idempotency
def idempotent(con, idem_key: str | None, route: str, user_id: str, request_body: dict):
    """Return a previously recorded response for a repeated key, else None.

    AUD-C-007: retries must produce one durable business event.
    """
    if not idem_key:
        return None
    sha = hashlib.sha256(json.dumps(request_body, sort_keys=True, default=str).encode()).hexdigest()
    row = con.execute("SELECT * FROM idempotency_key WHERE idem_key=?", (idem_key,)).fetchone()
    if row:
        if row["request_sha"] != sha or row["route"] != route:
            raise BusinessError(409, "IDEMPOTENCY_CONFLICT",
                                "This idempotency key was already used for a different request.")
        return json.loads(row["response"])
    return None


def remember(con, idem_key: str | None, route: str, user_id: str, request_body: dict,
             status_code: int, response: dict) -> None:
    if not idem_key:
        return
    sha = hashlib.sha256(json.dumps(request_body, sort_keys=True, default=str).encode()).hexdigest()
    con.execute("""INSERT OR IGNORE INTO idempotency_key
                   (idem_key,route,user_id,request_sha,status_code,response,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (idem_key, route, user_id, sha, status_code,
                 json.dumps(response, default=str), now()))


# ---------------------------------------------------------------- helpers
def _row(con, sql, args, code, msg):
    r = con.execute(sql, args).fetchone()
    if not r:
        raise BusinessError(404, code, msg)
    return dict(r)


def _bump(con, table, pk_col, pk, expected_version: int | None):
    """Optimistic concurrency on top of the pessimistic lock."""
    cur = con.execute(f"SELECT version_no FROM {table} WHERE {pk_col}=?", (pk,)).fetchone()
    if cur is None:
        raise BusinessError(404, "NOT_FOUND", f"{table} {pk} does not exist.")
    if expected_version is not None and cur[0] != expected_version:
        raise BusinessError(409, "VERSION_CONFLICT",
                            f"{pk} was modified by someone else. Reload and try again.")
    con.execute(f"UPDATE {table} SET version_no = version_no + 1 WHERE {pk_col}=?", (pk,))


# ==========================================================================
# Purchase requests
# ==========================================================================
def create_pr(con, actor: dict, *, project_id, wbs_id, budget_head_id, description,
              amount, reserve: bool = False):
    amount_paise = to_paise(amount, field="amount_rupees")
    with critical(con):
        wbs = _row(con, "SELECT * FROM wbs_element WHERE wbs_id=?", (wbs_id,),
                   "WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.")
        if wbs["project_id"] != project_id:
            raise BusinessError(422, "PROJECT_WBS_MISMATCH",
                                f"{wbs['wbs_code']} belongs to {wbs['project_id']}, "
                                f"not {project_id}. The request was not created.")
        chk = domain.budget_check(con, wbs_id, amount_paise, budget_head_id)
        if chk.get("verdict") == "BLOCKED":
            raise BusinessError(422, chk.get("code", "BLOCKED"), chk["message"])

        n = con.execute("SELECT COUNT(*) FROM purchase_request").fetchone()[0] + 1
        pid, num = f"PR-{n:03d}", f"PR-2026-{n:04d}"
        status = "Exception Pending" if chk["verdict"] == "EXCEEDS_BUDGET" else "Submitted"
        con.execute("INSERT INTO purchase_request VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, num, project_id, wbs_id, budget_head_id, description, amount_paise,
                     actor["user_id"], now(), status, chk["verdict"], None, None,
                     chk["message"] if chk["verdict"] == "EXCEEDS_BUDGET" else None,
                     1 if reserve else 0))
        if reserve and chk["verdict"] == "WITHIN_BUDGET":
            con.execute("""INSERT INTO pr_reservation
                           (reservation_id,pr_id,wbs_id,budget_head_id,amount_paise,state,created_at)
                           VALUES (?,?,?,?,?,'Reserved',?)""",
                        (f"RES-{uuid.uuid4().hex[:8].upper()}", pid, wbs_id, budget_head_id,
                         amount_paise, now()))
        audit(con, actor["user_id"], "PR_CREATED", "PurchaseRequest", pid,
              f"{num} for {chk.get('wbs_code')} value {format_inr(amount_paise)}. "
              f"Budget check: {chk['verdict']}.")
        return {"pr_id": pid, "pr_number": num, "status": status, "check": chk}


def actor_holds(actor: dict, permission: str) -> bool:
    """Whether `actor` holds `permission`, as a question rather than a refusal.

    `auth.require` raises, which is right at an enforcement point and wrong
    when the caller needs to CHOOSE between permissions. Same source of truth:
    `auth.PERMISSIONS`, read through the same role intersection, so this cannot
    drift from what `require` would decide.
    """
    allowed = auth.PERMISSIONS.get(permission)
    if allowed is None:
        return False
    return bool(set(actor.get("roles", ())) & set(allowed))


#: The two permissions either of which entitles a caller to ASK about a
#: purchase request's approvability. Which one is actually REQUIRED depends on
#: the row -- see `approve_pr` -- so this coarse set exists to gate the read
#: that determines it.
_PR_APPROVAL_PERMISSIONS = ("pr.approve", "pr.approve_exception")


def approve_pr(con, actor: dict, pr_id: str, *, reason: str | None):
    with critical(con):
        # COARSE GATE FIRST, so the reads below cannot answer a caller who was
        # never entitled to ask. `_row` raises 404 for an unknown id and the
        # status check raises 409 naming the pr_number and status -- both of
        # which told a principal holding NEITHER approval permission whether an
        # id exists and what state it is in.
        #
        # It has to be coarse: the permission this call really requires is
        # `pr.approve` or `pr.approve_exception` depending on the row's
        # `check_result`, which is not knowable until the row is read. So a
        # caller must hold at least one of the two to get that far, and the
        # specific requirement is enforced unchanged below. A holder of only
        # `pr.approve_exception` approving a within-budget request is still
        # refused there, with the same code and message as before.
        if not any(actor_holds(actor, p) for p in _PR_APPROVAL_PERMISSIONS):
            auth.require(actor, _PR_APPROVAL_PERMISSIONS[0])

        pr = _row(con, "SELECT * FROM purchase_request WHERE pr_id=?", (pr_id,),
                  "PR_NOT_FOUND", f"Purchase request {pr_id} does not exist.")
        if pr["status"] not in ("Submitted", "Under Review", "Exception Pending"):
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{pr['pr_number']} is {pr['status']} and cannot be approved again.")

        is_exception = pr["check_result"] == "EXCEEDS_BUDGET"
        permission = "pr.approve_exception" if is_exception else "pr.approve"
        auth.require(actor, permission)
        auth.require_separation(actor, permission, pr["requested_by"],
                                object_label=pr["pr_number"])
        if is_exception and not (reason or "").strip():
            raise BusinessError(422, "REASON_REQUIRED",
                                "This request exceeds available budget. An exception reason is "
                                "mandatory and will be stored in the audit trail.")

        # Re-check at approval time: availability may have moved since submission.
        chk = domain.budget_check(con, pr["wbs_id"], pr["amount_paise"], pr["budget_head_id"])
        if chk["verdict"] == "EXCEEDS_BUDGET" and not is_exception:
            raise BusinessError(409, "BUDGET_MOVED",
                                "Available budget has changed since this request was raised. "
                                + chk["message"])

        con.execute("UPDATE purchase_request SET status='Approved', approver=?, approved_at=? "
                    "WHERE pr_id=?", (actor["user_id"], now(), pr_id))
        audit(con, actor["user_id"], "PR_APPROVED", "PurchaseRequest", pr_id,
              (f"Exception approval. Reason: {reason}" if is_exception else "Within-budget approval.")
              + f" Requested by {pr['requested_by']}.")
        return {"pr_id": pr_id, "status": "Approved",
                "exception": is_exception, "approver": actor["user_id"]}


# ==========================================================================
# Purchase orders
# ==========================================================================
def amend_po(con, actor: dict, po_id: str, *, po_line_id: str, new_amount,
             exception_ref: str | None = None, expected_version: int | None = None):
    """AUD-C-001. Atomic: validate and write inside one locked transaction, and
    refuse to persist an over-budget increase without an approved exception."""
    new_paise = to_paise(new_amount, field="new_amount_rupees")
    with critical(con):
        po = _row(con, "SELECT * FROM purchase_order WHERE po_id=?", (po_id,),
                  "PO_NOT_FOUND", f"Purchase order {po_id} does not exist.")
        if po["status"] in ("Cancelled", "Closed"):
            raise BusinessError(409, "PO_NOT_AMENDABLE",
                                f"{po['po_number']} is {po['status']} and cannot be amended.")
        ln = con.execute("SELECT * FROM po_line WHERE po_line_id=?", (po_line_id,)).fetchone()
        if not ln:
            raise BusinessError(404, "LINE_NOT_FOUND", f"PO line {po_line_id} does not exist.")
        ln = dict(ln)
        # DATA-002: a line from another purchase order may not be amended here.
        if ln["po_id"] != po_id:
            raise BusinessError(422, "LINE_PO_MISMATCH",
                                f"Line {po_line_id} belongs to {ln['po_id']}, not {po_id}.")

        delta = new_paise - ln["amount_paise"]
        revalidation = None
        if delta > 0:
            revalidation = domain.budget_check(con, ln["wbs_id"], delta, ln["budget_head_id"])
            if revalidation.get("verdict") in ("BLOCKED",):
                raise BusinessError(422, revalidation.get("code", "BLOCKED"), revalidation["message"])
            if revalidation["verdict"] == "EXCEEDS_BUDGET":
                if not exception_ref:
                    # The defect: previously this computed the verdict and wrote anyway.
                    raise BusinessError(
                        409, "AMENDMENT_EXCEEDS_BUDGET",
                        revalidation["message"] +
                        " The amendment has NOT been applied. Supply an approved exception "
                        "reference to proceed.",
                        revalidation=revalidation)
                if not _exception_is_valid(con, exception_ref, ln["wbs_id"], ln["budget_head_id"]):
                    raise BusinessError(422, "EXCEPTION_INVALID",
                                        f"Exception reference {exception_ref} is not an approved, "
                                        f"independent exception for this WBS and budget head.")

        _bump(con, "purchase_order", "po_id", po_id, expected_version)
        con.execute("UPDATE po_line SET amount_paise=?, rate_paise=? WHERE po_line_id=?",
                    (new_paise, new_paise // max(int(ln["quantity"] or 1), 1), po_line_id))
        con.execute("UPDATE purchase_order SET amendment_no=amendment_no+1 WHERE po_id=?", (po_id,))
        audit(con, actor["user_id"], "PO_AMENDED", "PurchaseOrder", po_id,
              f"Line {ln['line_no']} {format_inr(ln['amount_paise'])} -> {format_inr(new_paise)} "
              f"(delta {format_inr(delta)}). "
              + (f"Exception {exception_ref}." if exception_ref else
                 "Within budget." if delta > 0 else "Decrease; commitment released."))
        return {"po_id": po_id, "delta_paise": delta, "revalidation": revalidation,
                "applied": True, "message_id": "MSG-PRC-001"}


def _exception_is_valid(con, ref: str, wbs_id: str, head_id: str) -> bool:
    """An exception reference must be an approved PR exception on the same control
    cell, approved by somebody other than the person now amending."""
    r = con.execute("""SELECT pr.* FROM purchase_request pr
                       WHERE (pr.pr_number = ? OR pr.pr_id = ?)
                         AND pr.status='Approved' AND pr.check_result='EXCEEDS_BUDGET'
                         AND pr.wbs_id = ? AND pr.budget_head_id = ?""",
                    (ref, ref, wbs_id, head_id)).fetchone()
    return bool(r and r["approver"])


def cancel_po(con, actor: dict, po_id: str, *, reason: str | None):
    with critical(con):
        po = _row(con, "SELECT * FROM purchase_order WHERE po_id=?", (po_id,),
                  "PO_NOT_FOUND", f"Purchase order {po_id} does not exist.")
        if po["status"] in ("Cancelled", "Closed"):
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{po['po_number']} is already {po['status']}.")
        if not (reason or "").strip():
            raise BusinessError(422, "REASON_REQUIRED",
                                "Cancellation requires a reason for the audit trail.")
        released = sum(r["open_commitment_paise"] for r in domain.reconciliation(con)
                       if r["po_id"] == po_id)
        _bump(con, "purchase_order", "po_id", po_id, None)
        con.execute("UPDATE purchase_order SET status='Cancelled' WHERE po_id=?", (po_id,))
        _settle_reservations_for_po(con, actor, po_id, "Released")
        audit(con, actor["user_id"], "PO_CANCELLED", "PurchaseOrder", po_id,
              f"Commitment of {format_inr(released)} released. Reason: {reason}")
        return {"po_id": po_id, "status": "Cancelled", "released_paise": released,
                "message_id": "MSG-PRC-002"}


def close_po(con, actor: dict, po_id: str, *, reason: str | None):
    with critical(con):
        po = _row(con, "SELECT * FROM purchase_order WHERE po_id=?", (po_id,),
                  "PO_NOT_FOUND", f"Purchase order {po_id} does not exist.")
        if po["status"] in ("Cancelled", "Closed"):
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{po['po_number']} is already {po['status']}.")
        rows = [r for r in domain.reconciliation(con) if r["po_id"] == po_id]
        residual = sum(max(0, r["ordered_paise"] - r["billed_paise"]) for r in rows)
        _bump(con, "purchase_order", "po_id", po_id, None)
        con.execute("UPDATE purchase_order SET status='Closed', closed_residual_released=1 "
                    "WHERE po_id=?", (po_id,))
        _settle_reservations_for_po(con, actor, po_id, "Released")
        audit(con, actor["user_id"], "PO_CLOSED", "PurchaseOrder", po_id,
              f"Closed with residual {format_inr(residual)} released. Reason: {reason or 'not stated'}")
        return {"po_id": po_id, "status": "Closed", "residual_released_paise": residual,
                "message_id": "MSG-PRC-003"}


def _settle_reservations_for_po(con, actor, po_id, state):
    con.execute("""UPDATE pr_reservation SET state=?, settled_at=?, settled_by=?, po_id=?
                   WHERE state='Reserved' AND po_id=?""",
                (state, now(), actor["user_id"], po_id, po_id))


def convert_pr_to_po(con, actor: dict, pr_id: str, po_id: str):
    """AUD-H-001: a reservation converts exactly once."""
    with critical(con):
        res = con.execute("SELECT * FROM pr_reservation WHERE pr_id=? AND state='Reserved'",
                          (pr_id,)).fetchone()
        if not res:
            raise BusinessError(409, "NO_LIVE_RESERVATION",
                                f"{pr_id} has no live reservation to convert.")
        con.execute("""UPDATE pr_reservation SET state='Converted', po_id=?, settled_at=?, settled_by=?
                       WHERE reservation_id=?""",
                    (po_id, now(), actor["user_id"], res["reservation_id"]))
        audit(con, actor["user_id"], "PR_CONVERTED", "PurchaseRequest", pr_id,
              f"Reservation {res['reservation_id']} converted to {po_id}; "
              f"{format_inr(res['amount_paise'])} released from reservation into commitment.")
        return {"pr_id": pr_id, "po_id": po_id, "converted_paise": res["amount_paise"]}


# ==========================================================================
# Budget revisions
# ==========================================================================
def create_revision(con, actor: dict, *, project_id, wbs_id, budget_head_id, kind,
                    amount, reason, effective_date=None):
    if kind not in ("SUPPLEMENT", "RETURN", "TRANSFER_IN", "TRANSFER_OUT"):
        raise BusinessError(422, "INVALID_KIND", f"{kind} is not a valid revision kind.")
    if not (reason or "").strip():
        raise BusinessError(422, "REASON_REQUIRED", "A revision requires a justification.")
    magnitude = to_paise(amount, field="amount_rupees")
    signed = -magnitude if kind in ("RETURN", "TRANSFER_OUT") else magnitude

    with critical(con):
        n = con.execute("SELECT COUNT(*) FROM budget_revision").fetchone()[0] + 1
        rid = f"REV-{n:03d}"
        con.execute("INSERT INTO budget_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, project_id, n, kind, reason, actor["user_id"], now(),
                     None, None, None, "Submitted", effective_date))
        # Draft/Submitted lines exist but are NOT effective, so they create no
        # spending capacity until approval (AUD-C-005 / DOM-006).
        con.execute("""INSERT INTO budget_line
            (budget_line_id,project_id,wbs_id,budget_head_id,kind,amount_paise,status,
             version_no,revision_id,effective_date,approved_by,approved_at,approval_ref,
             created_at,created_by)
            VALUES (?,?,?,?,?,?,'Submitted',1,?,NULL,NULL,NULL,NULL,?,?)""",
                    (f"BL-R{n:04d}", project_id, wbs_id, budget_head_id, kind, signed,
                     rid, now(), actor["user_id"]))
        audit(con, actor["user_id"], "REVISION_REQUESTED", "BudgetRevision", rid,
              f"{kind} of {format_inr(signed)}. Reason: {reason}")
        return {"revision_id": rid, "status": "Submitted", "amount_paise": signed,
                "note": "Submitted revisions do not affect available budget until approved "
                        "and effective."}


def approve_revision(con, actor: dict, revision_id: str, *, effective_date=None):
    with critical(con):
        r = _row(con, "SELECT * FROM budget_revision WHERE revision_id=?", (revision_id,),
                 "REVISION_NOT_FOUND", f"Revision {revision_id} does not exist.")
        if r["status"] != "Submitted":
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{revision_id} is {r['status']} and cannot be approved again.")
        auth.require(actor, "revision.approve")
        auth.require_separation(actor, "revision.approve", r["requested_by"],
                                object_label=revision_id)
        eff = effective_date or r["effective_date"] or now()[:10]
        ref = f"CAPEX-COMM/2026/{uuid.uuid4().hex[:4].upper()}"
        con.execute("""UPDATE budget_revision SET status='Approved', approver=?, approved_at=?,
                       approval_ref=?, effective_date=? WHERE revision_id=?""",
                    (actor["user_id"], now(), ref, eff, revision_id))
        con.execute("""UPDATE budget_line SET status='Approved', effective_date=?, approved_by=?,
                       approved_at=?, approval_ref=?, version_no=version_no+1
                       WHERE revision_id=?""", (eff, actor["user_id"], now(), ref, revision_id))
        audit(con, actor["user_id"], "REVISION_APPROVED", "BudgetRevision", revision_id,
              f"Approved by {actor['user_id']} (requested by {r['requested_by']}). "
              f"Reference {ref}, effective {eff}. Original budget unchanged.")
        return {"revision_id": revision_id, "status": "Approved", "approval_ref": ref,
                "effective_date": eff, "message_id": "MSG-BUD-005"}


# ==========================================================================
# Bills
# ==========================================================================
def void_bill(con, actor: dict, bill_id: str, *, reason: str | None):
    """AUD-C-004: a void must actually remove the actual from the ledger."""
    with critical(con):
        b = _row(con, "SELECT * FROM bill WHERE bill_id=?", (bill_id,),
                 "BILL_NOT_FOUND", f"Bill {bill_id} does not exist.")
        if b["accounting_status"] == "Void":
            raise BusinessError(409, "INVALID_TRANSITION", f"{b['bill_number']} is already void.")
        if not (reason or "").strip():
            raise BusinessError(422, "REASON_REQUIRED", "Voiding a bill requires a reason.")
        # `created_by` is the MAKER of the bill (db.py CREATE TABLE bill). The
        # column was added in Wave 8: until then this call read a column that
        # did not exist, always passed None, and enforced nothing at all -- a
        # FinanceApprover could void a bill they had raised themselves and the
        # segregation-of-duties report stayed clean.
        #
        # `require_maker=True` is what keeps that from silently recurring: if a
        # bill row carries no maker, the void is REFUSED rather than waved
        # through, so a future ingestion path that forgets to populate the
        # column fails loudly instead of disabling the control.
        auth.require_separation(actor, "bill.void", b.get("created_by"),
                                object_label=b["bill_number"], require_maker=True)
        con.execute("""UPDATE bill SET accounting_status='Void', status='Void', voided_by=?,
                       voided_at=?, void_reason=?, version_no=version_no+1 WHERE bill_id=?""",
                    (actor["user_id"], now(), reason, bill_id))
        audit(con, actor["user_id"], "BILL_VOIDED", "Bill", bill_id,
              f"{b['bill_number']} voided. Actual CWIP reversed and commitment restored. "
              f"Reason: {reason}")
        return {"bill_id": bill_id, "accounting_status": "Void"}


# ==========================================================================
# Capitalisation
# ==========================================================================
def allocate(con, actor: dict, cap_id: str, *, wbs_id, asset_name, asset_category,
             amount, is_writeoff: bool, writeoff_reason: str | None):
    amount_paise = to_paise(amount, field="amount_rupees")
    if amount_paise <= 0:
        raise BusinessError(422, "NON_POSITIVE_ALLOCATION",
                            "An allocation must be a positive amount.")
    if is_writeoff and not (writeoff_reason or "").strip():
        raise BusinessError(422, "REASON_REQUIRED", "A write-off requires a recorded reason.")
    with critical(con):
        cap = _row(con, "SELECT * FROM capitalisation_request WHERE cap_id=?", (cap_id,),
                   "CAP_NOT_FOUND", f"Capitalisation request {cap_id} does not exist.")
        if cap["status"] != "Submitted":
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{cap['cap_number']} is {cap['status']}; allocations are closed.")
        aid = f"ALLOC-{uuid.uuid4().hex[:6].upper()}"
        con.execute("INSERT INTO asset_allocation VALUES (?,?,?,?,?,?,?,?,?)",
                    (aid, cap_id, wbs_id, asset_name, asset_category, amount_paise,
                     1 if is_writeoff else 0, writeoff_reason, None))
        con.execute("""UPDATE capitalisation_request SET total_paise =
                       (SELECT COALESCE(SUM(amount_paise),0) FROM asset_allocation WHERE cap_id=?)
                       WHERE cap_id=?""", (cap_id, cap_id))
        audit(con, actor["user_id"], "CAP_ALLOCATED", "CapitalisationRequest", cap_id,
              f"{asset_name}: {format_inr(amount_paise)}"
              + (f" (write-off: {writeoff_reason})" if is_writeoff else ""))
        return {"allocation_id": aid}


def approve_capitalisation(con, actor: dict, cap_id: str, *, override_ref: str | None = None):
    """AUD-C-009: eligibility is a gate, not a warning."""
    with critical(con):
        cap = _row(con, "SELECT * FROM capitalisation_request WHERE cap_id=?", (cap_id,),
                   "CAP_NOT_FOUND", f"Capitalisation request {cap_id} does not exist.")
        if cap["status"] != "Submitted":
            raise BusinessError(409, "INVALID_TRANSITION",
                                f"{cap['cap_number']} is already {cap['status']}.")
        auth.require(actor, "capitalisation.approve")
        auth.require_separation(actor, "capitalisation.approve", cap["requested_by"],
                                object_label=cap["cap_number"])

        led = domain.compute_ledger(con, cap["project_id"])
        t = led["totals"]
        blockers = []
        if t["commitment"] != 0:
            blockers.append(f"{format_inr(t['commitment'])} of open commitment remains")
        if t["received_not_billed"] != 0:
            blockers.append(f"{format_inr(t['received_not_billed'])} received but not billed")
        open_exc = con.execute(
            "SELECT COUNT(*) FROM reconciliation_exception WHERE status='Open'").fetchone()[0]
        if open_exc:
            blockers.append(f"{open_exc} open reconciliation exception(s)")
        if blockers:
            raise BusinessError(
                409, "CAPITALISATION_BLOCKED",
                f"{cap['cap_number']} cannot be capitalised: " + "; ".join(blockers) +
                ". Resolve these, or record an explicit write-off, before capitalising.",
                blockers=blockers, message_id="MSG-CAP-001")

        alloc = con.execute("SELECT COALESCE(SUM(amount_paise),0) FROM asset_allocation "
                            "WHERE cap_id=?", (cap_id,)).fetchone()[0]
        if alloc != t["actual"]:
            raise BusinessError(
                422, "ALLOCATION_MISMATCH",
                f"The allocation totals {format_inr(alloc)} against a CWIP balance of "
                f"{format_inr(t['actual'])}, a difference of {format_inr(t['actual'] - alloc)}. "
                f"Allocate the full balance, or record the remainder as a write-off.",
                message_id="MSG-CAP-002")

        _bump(con, "capitalisation_request", "cap_id", cap_id, None)
        con.execute("""UPDATE capitalisation_request SET status='Approved', approver=?, approved_at=?
                       WHERE cap_id=?""", (actor["user_id"], now(), cap_id))
        con.execute("UPDATE project SET status='Capitalised' WHERE project_id=?",
                    (cap["project_id"],))
        audit(con, actor["user_id"], "CAP_APPROVED", "CapitalisationRequest", cap_id,
              f"{format_inr(t['actual'])} approved for capitalisation by {actor['user_id']} "
              f"(requested by {cap['requested_by']}). NOT YET POSTED to any general ledger or "
              f"fixed-asset register - local approval only.")
        return {"cap_id": cap_id, "status": "Approved", "capitalised_paise": t["actual"],
                "posting_status": "NOT POSTED - local approval only; no ERP/GL posting exists",
                "message_id": "MSG-CAP-003"}
