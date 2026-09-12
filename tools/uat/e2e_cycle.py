"""The WBS side of the UAT end-to-end cycle (2026-09-12), against the live UAT AppSail.

    python tools/uat/e2e_cycle.py raise      # steps 1-4a: PR -> availability -> submit -> approve -> PO -> dedupe key -> emit attempt
    python tools/uat/e2e_cycle.py verify     # steps 8-10 read-backs after the tenant side exists

Writes an evidence JSON under docs/fable51/evidence/e2e/ with every response the
routes gave (ids, versions, codes, figures). No credential is printed: passwords
are read from the credentials file outside the repository. Maker and checker are
two different administrators (maker-checker).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "https://wbs-capex-uat-50045784768.development.catalystappsail.in"
CONN = "CONN-32A904F37FEA"
PROJECT, WBS, HEAD = "PRJ-DM-001", "WBS-A-ELEC", "BH-DM1-ELEC"
REFERENCE = "WBS-UAT-E2E-20260912"
VENDOR_NAME = "Demo Vendor - Kalinga Electricals (SYNTHETIC)"
VENDOR_EXTERNAL_ID = "3912780000000075001"
QTY, RATE_PAISE = 1, 185_000_000          # 1 x Rs 18,50,000.00 (the demo switchgear item); the cell holds Rs 30,00,000 available
AMOUNT_PAISE = QTY * RATE_PAISE           # Rs 37,00,000.00
MAKER, CHECKER, CONVERTER, ADMIN = "U-REQ", "U-PLH", "U-PLH", "U-RAKESH"   # Requestor / ProcurementApprover (in PRJ-DM-001 scope) / Administrator
OUT = ROOT / "docs/fable51/evidence/e2e"
USERS = os.path.expanduser("~/.capex-tools/uat-credentials/uat-users.txt")


def password_for(uid: str) -> str:
    for line in open(USERS, encoding="utf-8"):
        parts = line.rstrip("\r\n").split("\t")
        if parts and parts[0] == uid and len(parts) > 1:
            return parts[1]
    raise SystemExit(f"no credential line for {uid}")


def call(method, path, body=None, session=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    if session:
        h["X-Session"] = session
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:400]}


def login(uid: str) -> str:
    st, body = call("POST", "/api/auth/login", {"user_id": uid, "password": password_for(uid)})
    assert st == 200, (uid, st, body)
    return body["session_id"]


def step(ev: dict, name: str, status: int, body: dict) -> dict:
    ev["steps"].append({"step": name, "status": status, "at": datetime.now(timezone.utc).isoformat(),
                        "body": body})
    print(f"{name}: {status} {json.dumps(body, default=str)[:300]}")
    return body


def raise_cycle() -> Path:
    ev = {"reference": REFERENCE, "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}
    maker, checker, converter, admin = login(MAKER), login(CHECKER), login(CONVERTER), login(ADMIN)
    # 0. Negative proof of the availability control: a request for MORE than the
    #    cell can hold is refused with a code and creates nothing.
    st, body = call("POST", "/api/procurement/purchase-requests", {
        "project_id": PROJECT, "description": f"{REFERENCE}: over-budget probe (must be refused)",
        "reserve_budget": True,
        "lines": [{"wbs_id": WBS, "budget_head_id": HEAD, "quantity": 2, "rate_paise": RATE_PAISE,
                   "amount_paise": 2 * RATE_PAISE, "description": "11kV switchgear panels x2 (over budget)"}],
    }, session=maker, headers={"Idempotency-Key": f"{REFERENCE}-PR-OVER"})
    step(ev, "0 over-budget request refused", st, body)
    assert st == 409 and body.get("detail", {}).get("code") == "RESERVATION_EXCEEDS_BUDGET", "the over-budget probe was not refused"
    # 1. PR raised in WBS (maker), reserving budget on the control cell.
    st, body = call("POST", "/api/procurement/purchase-requests", {
        "project_id": PROJECT, "description": f"{REFERENCE}: 11kV switchgear panels, UAT end-to-end cycle",
        "reserve_budget": True,
        "lines": [{"wbs_id": WBS, "budget_head_id": HEAD, "quantity": QTY, "rate_paise": RATE_PAISE,
                   "amount_paise": AMOUNT_PAISE, "description": "11kV switchgear panels (UAT E2E)"}],
    }, session=maker, headers={"Idempotency-Key": f"{REFERENCE}-PR"})
    pr = step(ev, "1 PR raised", st, body)
    pr_id = pr.get("pr_id") or pr.get("purchase_request_id") or (pr.get("purchase_request") or {}).get("pr_id")
    assert st == 201 and pr_id, "PR was not created"
    # 2. Budget availability checked on the cell the PR commits against.
    st, body = call("GET", f"/api/budget/availability?wbs_id={WBS}&budget_head_id={HEAD}&amount_paise={AMOUNT_PAISE}",
                    session=maker)
    step(ev, "2 availability", st, body)
    # 1b. Submit.
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/submit",
                    {"version_no": pr.get("version_no")}, session=maker)
    sub = step(ev, "1b PR submitted", st, body)
    # 3. Approval by a DIFFERENT administrator (maker-checker).
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/approve",
                    {"reason": f"{REFERENCE} UAT approval", "version_no": sub.get("version_no")}, session=checker)
    appr = step(ev, "3 PR approved by checker", st, body)
    # 3b. Convert to a purchase order (maker) -- the local order the tenant side will be linked to.
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/convert", {
        "vendor_name": VENDOR_NAME, "po_number": REFERENCE, "currency": "INR",
        "document_date": date.today().isoformat(), "version_no": appr.get("version_no"),
    }, session=converter, headers={"Idempotency-Key": f"{REFERENCE}-PO"})
    po = step(ev, "3b PO created from PR", st, body)
    po_id = po.get("po_id") or (po.get("purchase_order") or {}).get("po_id")
    assert st in (200, 201) and po_id, "PO was not created"
    # 4a. The deterministic dedupe key the emission would carry as cf_capex_ref.
    from app.backend.integration.outbound import derive_dedupe_key
    key = derive_dedupe_key(CONN, "purchaseorders", po_id)
    ev["dedupe_key"] = key
    ev["po_id"], ev["pr_id"] = po_id, pr_id
    print("4a dedupe key for the tenant order's cf_capex_ref:", key)
    # 4b. Emission through the app: the outbound write gate is SHUT on UAT and the
    #     credential is read-only, so the route must refuse with a code, not send.
    st, body = call("POST", f"/api/procurement/purchase-orders/{po_id}/emit", {
        "connection_id": CONN, "vendor_external_id": VENDOR_EXTERNAL_ID,
        "document_date": date.today().isoformat(),
    }, session=admin, headers={"Idempotency-Key": f"{REFERENCE}-EMIT"})
    step(ev, "4b emit attempt (gate shut, read-only credential)", st, body)
    # 4c. Identical retry: the same refusal, nothing sent twice.
    st, body = call("POST", f"/api/procurement/purchase-orders/{po_id}/emit", {
        "connection_id": CONN, "vendor_external_id": VENDOR_EXTERNAL_ID,
        "document_date": date.today().isoformat(),
    }, session=admin, headers={"Idempotency-Key": f"{REFERENCE}-EMIT"})
    step(ev, "4c identical retry", st, body)
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"raise-{ev['started_at'][:19].replace(':', '')}.json"
    p.write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    print("wrote", p.relative_to(ROOT))
    return p


def verify_cycle(po_id: str) -> Path:
    ev = {"reference": REFERENCE, "po_id": po_id, "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}
    s = login(ADMIN)
    st, body = call("GET", f"/api/integrations/reconciliation?project_id={PROJECT}&limit=500", session=s)
    step(ev, "8 reconciliation", st, {"count": len(body.get("items", [])) if isinstance(body, dict) else None,
                                       "rows_for_reference": [r for r in (body.get("items") or []) if REFERENCE in json.dumps(r)][:5]} if st == 200 else body)
    st, body = call("GET", f"/api/budget/availability?wbs_id={WBS}&budget_head_id={HEAD}&amount_paise=0", session=s)
    step(ev, "9 ledger exposure on the cell", st, body)
    st, body = call("GET", f"/api/reports/metrics?scope=all", session=s)
    step(ev, "9b metrics", st, {k: body.get(k) for k in list(body)[:12]} if isinstance(body, dict) else body)
    st, body = call("GET", "/api/audit/anchors/verify", session=s)
    step(ev, "10 audit chain verify", st, body)
    st, body = call("GET", "/api/integrations/exceptions?limit=50", session=s)
    step(ev, "exceptions", st, {"open": [{k: e.get(k) for k in ("exception_id", "kind", "object_id", "status", "source_paise")}
                                          for e in (body.get("items") or [])]} if st == 200 else body)
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"verify-{ev['started_at'][:19].replace(':', '')}.json"
    p.write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    print("wrote", p.relative_to(ROOT))
    return p


if __name__ == "__main__":
    if sys.argv[1] == "raise":
        raise_cycle()
    elif sys.argv[1] == "verify":
        verify_cycle(sys.argv[2])
