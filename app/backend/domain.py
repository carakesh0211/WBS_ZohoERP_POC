"""The control engine.

Implements the frozen formula registry (research/30_contracts/C5_formulas.json):

    Exposure         = Open PO Commitment + Actual CWIP + valid PR reservation
    Available Budget = Current Approved Budget - Exposure
    Utilisation %    = Exposure / Current Approved Budget

Corrections applied after the independent audit of 2026-08-06:

  AUD-C-002  Budget head is a real control dimension. The ledger is keyed on the
             (WBS element, budget head) CELL. Previously all heads under a WBS
             owner were pooled, so a depleted head could be bypassed by labelling
             a request with a different head.

  AUD-C-004  Only accounting-effective bills contribute to actual cost. Void,
             Draft and superseded bills are excluded; a reversal document is
             negated by its own flag rather than relying on someone entering a
             negative amount.

  AUD-C-005  Only Approved AND effective budget lines contribute to the current
             approved budget. Draft, Submitted, Rejected, Cancelled and
             future-dated lines create no spending capacity.

  AUD-C-008  Procurement and posting permission is derived from the lifecycle_state
             table for both project and WBS, rather than from two free-standing
             boolean flags that could disagree with the recorded status.

  AUD-H-001  PR reservations come from the pr_reservation ledger, where a
             reservation is Reserved, Converted or Released exactly once.

  AUD-M-005  The rollup is iterative. A 1,100-deep hierarchy previously raised
             RecursionError and failed the request.

All money is integer paise (see money.py). No float touches a monetary value.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

# Exposure thresholds, from the client document's recommended alerts.
WATCH_PCT = 80.0
CRITICAL_PCT = 90.0

# A PO in one of these states no longer holds commitment.
COMMITMENT_RELEASING_STATES = {"Cancelled", "Closed"}

# Bill states that are accounting-effective, i.e. that move actual CWIP.
ACCOUNTING_EFFECTIVE_BILL_STATES = {"Approved", "Reversal"}

MAX_WBS_DEPTH = 100          # sanity bound; deeper structures are a data error


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _today(as_of: str | None = None) -> str:
    return as_of or date.today().isoformat()


# ==========================================================================
# Ledger
# ==========================================================================
def compute_ledger(con, project_id=None, *, as_of: str | None = None):
    """Per-WBS and per-(WBS, budget head) financial position.

    Returns {"wbs": [roots], "by_id": {...}, "totals": {...}} where every node
    carries:
        own    - this element only, summed across heads
        total  - this element plus all descendants, summed across heads
        heads  - {budget_head_id: cell} for this element only
        head_totals - {budget_head_id: cell} rolled up over descendants
    """
    today = _today(as_of)
    where = "WHERE w.project_id = ?" if project_id else ""
    args = (project_id,) if project_id else ()

    wbs = _rows(con, f"""
        SELECT w.*, p.capex_code, p.name AS project_name, p.status AS project_status
        FROM wbs_element w JOIN project p ON p.project_id = w.project_id
        {where} ORDER BY w.level, w.sort_order
    """, args)
    if not wbs:
        return {"wbs": [], "by_id": {}, "totals": _empty()}

    ids = {w["wbs_id"] for w in wbs}
    cell = lambda: defaultdict(int)          # noqa: E731
    budget, original, revisions = cell(), cell(), cell()
    ordered, commitment, actual = cell(), cell(), cell()
    received, rec_not_billed, pr_reserved = cell(), cell(), cell()

    # ---- budget: approved AND effective only (AUD-C-005) -------------------
    for r in _rows(con, """
        SELECT wbs_id, budget_head_id, kind, SUM(amount_paise) amt
        FROM budget_line
        WHERE status = 'Approved'
          AND effective_date IS NOT NULL
          AND effective_date <= ?
        GROUP BY wbs_id, budget_head_id, kind
    """, (today,)):
        k = (r["wbs_id"], r["budget_head_id"])
        if r["wbs_id"] not in ids:
            continue
        budget[k] += r["amt"]
        (original if r["kind"] == "ORIGINAL" else revisions)[k] += r["amt"]

    # ---- per PO line: ordered / received / billed ---------------------------
    lines = _rows(con, """
        SELECT pl.po_line_id, pl.wbs_id, pl.budget_head_id,
               pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise AS ordered_paise,
               po.status AS po_status
        FROM po_line pl JOIN purchase_order po ON po.po_id = pl.po_id
    """)

    received_by_line = defaultdict(int)
    for r in _rows(con, """
        SELECT gl.po_line_id,
               SUM(CASE WHEN g.is_reversal = 1 THEN -ABS(gl.amount_paise)
                        ELSE gl.amount_paise END) amt
        FROM grn_line gl JOIN grn g ON g.grn_id = gl.grn_id
        WHERE g.status <> 'Void'
        GROUP BY gl.po_line_id
    """):
        received_by_line[r["po_line_id"]] = r["amt"]

    # Only accounting-effective bills relieve commitment (AUD-C-004).
    billed_by_line = defaultdict(int)
    for r in _rows(con, f"""
        SELECT bl.po_line_id,
               SUM(CASE WHEN b.accounting_status = 'Reversal'
                        THEN -ABS(bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise)
                        ELSE bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise END) amt
        FROM bill_line bl JOIN bill b ON b.bill_id = bl.bill_id
        WHERE bl.po_line_id IS NOT NULL
          AND b.accounting_status IN ({','.join('?' * len(ACCOUNTING_EFFECTIVE_BILL_STATES))})
        GROUP BY bl.po_line_id
    """, tuple(sorted(ACCOUNTING_EFFECTIVE_BILL_STATES))):
        billed_by_line[r["po_line_id"]] = r["amt"]

    for ln in lines:
        k = (ln["wbs_id"], ln["budget_head_id"])
        if ln["wbs_id"] not in ids:
            continue
        o = ln["ordered_paise"]
        billed = billed_by_line.get(ln["po_line_id"], 0)
        recd = received_by_line.get(ln["po_line_id"], 0)
        ordered[k] += o
        received[k] += recd
        # ANTI-DOUBLE-COUNT: a line commits only its unbilled balance.
        open_c = 0 if ln["po_status"] in COMMITMENT_RELEASING_STATES else max(0, o - billed)
        commitment[k] += open_c
        # ANTI-UNDER-COUNT: value received but not yet billed is its own bucket.
        rec_not_billed[k] += max(0, recd - billed)

    # ---- actual CWIP, accounting-effective only ----------------------------
    for r in _rows(con, f"""
        SELECT bl.wbs_id, bl.budget_head_id,
               SUM(CASE WHEN b.accounting_status = 'Reversal'
                        THEN -ABS(bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise)
                        ELSE bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise END) amt
        FROM bill_line bl JOIN bill b ON b.bill_id = bl.bill_id
        WHERE b.accounting_status IN ({','.join('?' * len(ACCOUNTING_EFFECTIVE_BILL_STATES))})
        GROUP BY bl.wbs_id, bl.budget_head_id
    """, tuple(sorted(ACCOUNTING_EFFECTIVE_BILL_STATES))):
        if r["wbs_id"] in ids:
            actual[(r["wbs_id"], r["budget_head_id"])] = r["amt"]

    # ---- live PR reservations (AUD-H-001) ----------------------------------
    if _has_table(con, "pr_reservation"):
        for r in _rows(con, """
            SELECT wbs_id, budget_head_id, SUM(amount_paise) amt
            FROM pr_reservation WHERE state = 'Reserved'
            GROUP BY wbs_id, budget_head_id
        """):
            if r["wbs_id"] in ids:
                pr_reserved[(r["wbs_id"], r["budget_head_id"])] = r["amt"]

    # ---- build nodes -------------------------------------------------------
    all_keys = set(budget) | set(ordered) | set(actual) | set(received) | set(pr_reserved)
    by_id = {}
    for w in wbs:
        i = w["wbs_id"]
        heads = {}
        for (wid, head) in [k for k in all_keys if k[0] == i]:
            heads[head] = _derive({
                "budget": budget[(i, head)], "original": original[(i, head)],
                "revisions": revisions[(i, head)], "ordered": ordered[(i, head)],
                "commitment": commitment[(i, head)], "actual": actual[(i, head)],
                "received": received[(i, head)],
                "received_not_billed": rec_not_billed[(i, head)],
                "pr_reserved": pr_reserved[(i, head)],
            })
        by_id[i] = {**w, "heads": heads, "own": _sum_cells(heads.values()), "children": []}

    roots = []
    for w in wbs:
        node, p = by_id[w["wbs_id"]], w["parent_wbs_id"]
        (by_id[p]["children"] if p and p in by_id else roots).append(node)

    # ---- budget ownership, per head (AUD-C-002) ---------------------------
    for wid, node in by_id.items():
        owners, cur, depth = {}, node, 0
        chain = []
        while cur is not None and depth < MAX_WBS_DEPTH:
            chain.append(cur)
            p = cur.get("parent_wbs_id")
            cur = by_id.get(p) if p else None
            depth += 1
        for anc in chain:
            for head, c in anc["heads"].items():
                if head not in owners and c["budget"] != 0:
                    owners[head] = anc["wbs_id"]
        node["budget_owner_by_head"] = owners
        # backwards-compatible single owner: the element itself if it carries any
        # budget, else the nearest ancestor that does
        node["budget_owner_id"] = next(
            (a["wbs_id"] for a in chain if a["own"]["budget"] != 0), None)
        node["carries_budget"] = node["own"]["budget"] != 0

    # ---- iterative bottom-up rollup (AUD-M-005) ---------------------------
    order, stack = [], list(roots)
    seen = set()
    while stack:
        n = stack.pop()
        if n["wbs_id"] in seen:            # defensive: a cycle would otherwise hang
            continue
        seen.add(n["wbs_id"])
        order.append(n)
        stack.extend(n["children"])
    for n in reversed(order):              # children before parents
        agg_heads = {h: dict(c) for h, c in n["heads"].items()}
        for ch in n["children"]:
            for h, c in ch["head_totals"].items():
                tgt = agg_heads.setdefault(h, _blank())
                for k in _COMPONENTS:
                    tgt[k] += c[k]
        n["head_totals"] = {h: _derive(c) for h, c in agg_heads.items()}
        n["total"] = _sum_cells(n["head_totals"].values())

    totals = _sum_cells([r["total"] for r in roots])
    return {"wbs": roots, "by_id": by_id, "totals": totals}


_COMPONENTS = ("budget", "original", "revisions", "ordered", "commitment",
               "actual", "received", "received_not_billed", "pr_reserved",
               # 034: internal fulfilment. The SQLite estate has no internal
               # material requests, so both are zero here; the PostgreSQL
               # path (`pg.reporting`) derives them from the movements.
               "internal_allocation", "internal_consumption")


def _blank():
    return {k: 0 for k in _COMPONENTS}


def _empty():
    return _derive(_blank())


def _sum_cells(cells):
    out = _blank()
    for c in cells:
        for k in _COMPONENTS:
            out[k] += c[k]
    return _derive(out)


def _derive(d):
    """Apply the frozen formulas. Exposure and available are always derived."""
    d = {**_blank(), **d}
    d["exposure"] = (d["commitment"] + d["actual"] + d["pr_reserved"]
                     + d["internal_allocation"] + d["internal_consumption"])
    d["available"] = d["budget"] - d["exposure"]
    d["utilisation_pct"] = round((d["exposure"] / d["budget"] * 100.0), 1) if d["budget"] else 0.0
    d["band"] = ("breach" if d["budget"] and d["exposure"] > d["budget"]
                 else "critical" if d["utilisation_pct"] >= CRITICAL_PCT
                 else "watch" if d["utilisation_pct"] >= WATCH_PCT
                 else "safe")
    return d


def _has_table(con, name) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ==========================================================================
# Lifecycle permission (AUD-C-008)
# ==========================================================================
def lifecycle_permits(con, object_type: str, state: str, action: str) -> bool:
    """Unknown states deny by default; the table is the single source of truth."""
    col = "allows_procurement" if action == "procurement" else "allows_posting"
    row = con.execute(
        f"SELECT {col} FROM lifecycle_state WHERE object_type=? AND state=?",
        (object_type, state)).fetchone()
    return bool(row[0]) if row else False


# ==========================================================================
# Budget availability check
# ==========================================================================
def budget_check(con, wbs_id, amount_paise, budget_head_id=None, *, as_of=None):
    """Evaluate a proposed commitment against the (WBS, budget head) control cell.

    Rejects rather than approves whenever the request cannot be validated: an
    unknown element, an unknown head, a head with no approved budget anywhere on
    the branch, or a lifecycle state that does not permit procurement.
    """
    if amount_paise is None or amount_paise < 0:
        return _blocked("NEGATIVE_AMOUNT",
                        "A proposed commitment must be a positive amount.", wbs_code=None)

    row = con.execute("SELECT * FROM wbs_element WHERE wbs_id = ?", (wbs_id,)).fetchone()
    if not row:
        return _blocked("WBS_NOT_FOUND", f"WBS element {wbs_id} does not exist.")
    w = dict(row)
    prj = dict(con.execute("SELECT * FROM project WHERE project_id = ?",
                           (w["project_id"],)).fetchone())

    # ---- lifecycle gates, derived from state (AUD-C-008) -------------------
    if not lifecycle_permits(con, "project", prj["status"], "procurement"):
        return _blocked("PROJECT_STATE",
                        f"{prj['capex_code']} is {prj['status']}. Procurement is not permitted. "
                        f"An approved reopen is required before further commitment.",
                        w["wbs_code"], "MSG-PRC-006")
    if w["is_abandoned"]:
        return _blocked("WBS_ABANDONED",
                        f"{w['wbs_code']} is abandoned and cannot receive procurement.",
                        w["wbs_code"], "MSG-PRC-006")
    if not lifecycle_permits(con, "wbs", w["status"], "procurement"):
        return _blocked("WBS_STATE",
                        f"{w['wbs_code']} is {w['status']} and does not permit procurement.",
                        w["wbs_code"], "MSG-PRC-006")

    # ---- budget head must be a real, active head on this project ----------
    head_id = budget_head_id or w["budget_head_id"]
    if not head_id:
        return _blocked("HEAD_REQUIRED",
                        f"A budget head is mandatory for procurement on {w['wbs_code']}.",
                        w["wbs_code"], "MSG-BUD-004")
    head_row = con.execute("SELECT * FROM budget_head WHERE budget_head_id=? AND active=1",
                           (head_id,)).fetchone()
    if not head_row:
        return _blocked("HEAD_UNKNOWN", f"Budget head {head_id} is not a valid active head.",
                        w["wbs_code"], "MSG-BUD-004")
    head_name = head_row["name"]

    led = compute_ledger(con, w["project_id"], as_of=as_of)
    node = led["by_id"].get(wbs_id)

    # ---- the control cell: nearest ancestor carrying budget FOR THIS HEAD --
    owner_id = node["budget_owner_by_head"].get(head_id)
    if not owner_id:
        return _blocked(
            "NO_BUDGET_FOR_HEAD",
            f"No approved budget exists for budget head '{head_name}' on {w['wbs_code']} "
            f"or any element above it. Approve a budget for this head before raising procurement.",
            w["wbs_code"], "MSG-BUD-004", head=head_name)

    owner = led["by_id"][owner_id]
    pos = owner["head_totals"].get(head_id, _blank())
    pos = _derive(dict(pos))
    available = pos["available"]
    after = available - amount_paise
    exceeds = after < 0

    result = {
        "ok": not exceeds,
        "wbs_id": wbs_id,
        "wbs_code": w["wbs_code"],
        "wbs_description": w["description"],
        "project_code": prj["capex_code"],
        "budget_head": head_name,
        "budget_head_id": head_id,
        "budget_controlled_at": owner["wbs_code"],
        "budget_owner_id": owner_id,
        "proposed_paise": amount_paise,
        "position": pos,
        "own_position": node["own"],
        "rolled_up": owner["total"],
        "available_paise": available,
        "available_after_paise": after,
        "shortfall_paise": max(0, -after),
        "verdict": "EXCEEDS_BUDGET" if exceeds else "WITHIN_BUDGET",
    }

    via = "" if owner_id == wbs_id else f" (budget for this head is held at {owner['wbs_code']})"
    if exceeds:
        result.update({
            "severity": "error", "message_id": "MSG-BUD-001",
            "message": (f"The proposed value of {_r(amount_paise)} exceeds the available budget of "
                        f"{_r(available)} for {w['wbs_code']}{via} ({head_name}) by {_r(-after)}. "
                        f"Submit a budget revision or request exception approval."),
            "next_actions": ["Submit budget revision", "Request exception approval",
                             "Reduce the request"],
        })
    else:
        pct_after = round(((pos["exposure"] + amount_paise) / pos["budget"] * 100.0), 1) \
            if pos["budget"] else 0.0
        if pct_after >= CRITICAL_PCT:
            result.update({"severity": "warning", "message_id": "MSG-BUD-003",
                           "message": (f"{owner['wbs_code']} ({head_name}) will reach {pct_after}% of its "
                                       f"approved budget of {_r(pos['budget'])}. Only {_r(after)} would "
                                       f"remain. Further commitments will require exception approval.")})
        elif pct_after >= WATCH_PCT:
            result.update({"severity": "warning", "message_id": "MSG-BUD-002",
                           "message": (f"{owner['wbs_code']} ({head_name}) will reach {pct_after}% of its "
                                       f"approved budget of {_r(pos['budget'])}. Exposure would be "
                                       f"{_r(pos['exposure'] + amount_paise)} "
                                       f"({_r(pos['commitment'])} committed, {_r(pos['actual'])} actual).")})
        else:
            result.update({"severity": "success", "message_id": None,
                           "message": (f"Within budget. {_r(after)} would remain available on "
                                       f"{owner['wbs_code']} ({head_name}){via} after this commitment.")})
    return result


def _blocked(code, message, wbs_code=None, message_id=None, head=None):
    return {"ok": False, "code": code, "severity": "error", "message": message,
            "message_id": message_id, "wbs_code": wbs_code, "budget_head": head,
            "verdict": "BLOCKED"}


# ==========================================================================
# Commitment-to-actual reconciliation
# ==========================================================================
def reconciliation(con, project_id=None):
    where = "WHERE po.project_id = ?" if project_id else ""
    args = (project_id,) if project_id else ()
    lines = _rows(con, f"""
        SELECT pl.po_line_id, pl.line_no, pl.description, pl.wbs_id, w.wbs_code,
               pl.budget_head_id, bh.name AS budget_head,
               pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise AS ordered_paise,
               po.po_id, po.po_number, po.status AS po_status, po.vendor_name,
               po.currency, po.exchange_rate, po.amendment_no
        FROM po_line pl
        JOIN purchase_order po ON po.po_id = pl.po_id
        JOIN wbs_element w ON w.wbs_id = pl.wbs_id
        JOIN budget_head bh ON bh.budget_head_id = pl.budget_head_id
        {where} ORDER BY po.po_number, pl.line_no
    """, args)

    recd = {r["po_line_id"]: r["amt"] for r in _rows(con, """
        SELECT gl.po_line_id, SUM(CASE WHEN g.is_reversal = 1 THEN -ABS(gl.amount_paise)
                                       ELSE gl.amount_paise END) amt
        FROM grn_line gl JOIN grn g ON g.grn_id = gl.grn_id
        WHERE g.status <> 'Void' GROUP BY gl.po_line_id""")}
    bild = {r["po_line_id"]: r["amt"] for r in _rows(con, f"""
        SELECT bl.po_line_id,
               SUM(CASE WHEN b.accounting_status='Reversal'
                        THEN -ABS(bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise)
                        ELSE bl.amount_paise + bl.non_creditable_tax_paise + bl.freight_paise END) amt
        FROM bill_line bl JOIN bill b ON b.bill_id = bl.bill_id
        WHERE bl.po_line_id IS NOT NULL
          AND b.accounting_status IN ({','.join('?' * len(ACCOUNTING_EFFECTIVE_BILL_STATES))})
        GROUP BY bl.po_line_id""", tuple(sorted(ACCOUNTING_EFFECTIVE_BILL_STATES)))}

    out = []
    for ln in lines:
        o = ln["ordered_paise"]
        r = recd.get(ln["po_line_id"], 0)
        b = bild.get(ln["po_line_id"], 0)
        released = ln["po_status"] in COMMITMENT_RELEASING_STATES
        commitment = 0 if released else max(0, o - b)
        if released and ln["po_status"] == "Cancelled":
            position = "PO cancelled before billing" if b == 0 else "PO cancelled after partial billing"
        elif released:
            position = "PO closed - residual released"
        elif b == 0:
            position = "PO approved, not billed"
        elif b < o:
            position = "Partially billed"
        elif b == o:
            position = "Fully billed"
        else:
            position = "Bill exceeds PO"
        out.append({
            **ln,
            "received_paise": r, "billed_paise": b,
            "open_commitment_paise": commitment,
            "received_not_billed_paise": max(0, r - b),
            "exposure_paise": commitment + b,
            "residual_released_paise": (o - b) if released and o > b else 0,
            "position": position,
            "flag": ("over-billed" if b > o else
                     "received-unbilled" if r > b else
                     "released" if released else "ok"),
        })
    return out


def _r(paise):
    from .money import format_inr
    return format_inr(paise)
