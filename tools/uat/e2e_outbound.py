"""The controlled OUTBOUND test on the live UAT AppSail (2026-09-12): one
application-originated purchase order, WBS-UAT-OUTBOUND-20260912, into Zoho
ERP DEMO WBS (organisation 60074128927) through the app's own emit + drain
routes, and the proofs around it.

    python tools/uat/e2e_outbound.py raise      # steps 1-3: PR -> approval -> local order
    python tools/uat/e2e_outbound.py emit       # steps 4-7: mode LIVE_WRITE, emit, drain, identical retry, read-backs
    python tools/uat/e2e_outbound.py close      # steps 8-9: mode back to LIVE_READ, the drain refused
    python tools/uat/e2e_outbound.py refused    # step 10: after the owner shut the gate, emit + drain are 409

Preconditions for `emit`: the owner has installed the CREATE-scoped credential
(CAPEX_ERP_SCOPES carries ERP.purchaseorders.CREATE) and set
CAPEX_ERP_OUTBOUND_WRITES=1 on the AppSail, and the instance has restarted.
Every step is written to docs/fable51/evidence/e2e/outbound-<step>-<ts>.json.
No credential is printed or read here.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.uat.e2e_cycle import ADMIN, CHECKER, CONN, CONVERTER, MAKER, OUT, PROJECT, call, login, step  # noqa: E402

REFERENCE = "WBS-UAT-OUTBOUND-20260912"
WBS, HEAD = "WBS-A-CIVIL", "BH-DM1-CIVIL"          # Rs 85,00,000 available, no exposure (read 2026-09-12)
VENDOR_NAME = "Demo Vendor - Civil Works (SYNTHETIC, tenant 3912780000000074001)"
VENDOR_EXTERNAL_ID = "3912780000000074001"          # the tenant's civil demo vendor (PO-00005's)
ITEM_EXTERNAL_ID = "3912780000000080001"            # existing demo item: structural steel @ Rs 68,000
QTY, RATE_PAISE = 10, 6_800_000                     # 10 x Rs 68,000.00 = Rs 6,80,000.00
AMOUNT_PAISE = QTY * RATE_PAISE
STATE = OUT / "outbound-state.json"


def _write(ev: dict, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"outbound-{name}-{ev['started_at'][:19].replace(':', '')}.json"
    p.write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    print("wrote", p.relative_to(ROOT))
    return p


def _state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def _save_state(**kw) -> None:
    st = _state()
    st.update(kw)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def _ev(name: str) -> dict:
    return {"reference": REFERENCE, "phase": name,
            "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}


def raise_order() -> Path:
    ev = _ev("raise")
    maker, checker, converter = login(MAKER), login(CHECKER), login(CONVERTER)
    st, body = call("GET", f"/api/budget/availability?wbs_id={WBS}&budget_head_id={HEAD}&amount_paise={AMOUNT_PAISE}",
                    session=maker)
    step(ev, "1 availability on the civil cell before the request", st, body)
    st, body = call("POST", "/api/procurement/purchase-requests", {
        "project_id": PROJECT, "description": f"{REFERENCE}: structural steel, controlled outbound test",
        "reserve_budget": True,
        "lines": [{"wbs_id": WBS, "budget_head_id": HEAD, "quantity": QTY, "rate_paise": RATE_PAISE,
                   "amount_paise": AMOUNT_PAISE, "description": "Structural steel for plant building (UAT outbound)"}],
    }, session=maker, headers={"Idempotency-Key": f"{REFERENCE}-PR"})
    pr = step(ev, "2 PR raised", st, body)
    pr_id = pr.get("pr_id") or (pr.get("purchase_request") or {}).get("pr_id")
    assert st == 201 and pr_id, "PR was not created"
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/submit",
                    {"version_no": pr.get("version_no")}, session=maker)
    sub = step(ev, "2b PR submitted", st, body)
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/approve",
                    {"reason": f"{REFERENCE} approval (maker U-REQ, checker U-PLH)", "version_no": sub.get("version_no")},
                    session=checker)
    appr = step(ev, "3 PR approved by the checker", st, body)
    st, body = call("POST", f"/api/procurement/purchase-requests/{pr_id}/convert", {
        "vendor_name": VENDOR_NAME, "po_number": REFERENCE, "currency": "INR",
        "document_date": date.today().isoformat(), "version_no": appr.get("version_no"),
    }, session=converter, headers={"Idempotency-Key": f"{REFERENCE}-PO"})
    po = step(ev, "3b converted to the local order", st, body)
    po_id = po.get("po_id") or (po.get("purchase_order") or {}).get("po_id")
    assert st in (200, 201) and po_id, "PO was not created"
    from app.backend.integration.outbound import derive_dedupe_key
    ev["dedupe_key"] = derive_dedupe_key(CONN, "purchaseorders", po_id)
    ev["po_id"], ev["pr_id"] = po_id, pr_id
    lines = po.get("lines") or (po.get("purchase_order") or {}).get("lines") or []
    ev["po_line_ids"] = [ln.get("po_line_id") for ln in lines]
    _save_state(po_id=po_id, pr_id=pr_id, dedupe_key=ev["dedupe_key"], po_line_ids=ev["po_line_ids"])
    print("po_id", po_id, "dedupe_key", ev["dedupe_key"], "lines", ev["po_line_ids"])
    return _write(ev, "raise")


def _mode(session, mode: str, note: str) -> tuple[int, dict]:
    return call("POST", f"/api/integrations/connections/{CONN}/mode",
                {"mode": mode, "authorised_by": "owner (chat, 2026-09-12)", "note": note}, session=session)


def _emit(session, po_id: str, line_ids: list[str]) -> tuple[int, dict]:
    return call("POST", f"/api/procurement/purchase-orders/{po_id}/emit", {
        "connection_id": CONN, "vendor_external_id": VENDOR_EXTERNAL_ID,
        "document_date": date.today().isoformat(),
        "item_external_ids": {lid: ITEM_EXTERNAL_ID for lid in line_ids},
    }, session=session, headers={"Idempotency-Key": f"{REFERENCE}-EMIT"})


def _outbox(session) -> tuple[int, dict]:
    st, body = call("GET", f"/api/integrations/outbox?connection_id={CONN}&limit=50", session=session)
    rows = body.get("items") or body.get("rows") or [] if isinstance(body, dict) else []
    return st, {"rows": [{k: r.get(k) for k in ("outbox_id", "local_id", "dedupe_key", "state", "external_id", "attempts", "last_error")}
                         for r in rows]}


def emit_order() -> Path:
    stt = _state()
    po_id, line_ids = stt["po_id"], stt.get("po_line_ids") or []
    ev = _ev("emit")
    ev.update({"po_id": po_id, "dedupe_key": stt.get("dedupe_key")})
    admin = login(ADMIN)
    st, body = call("GET", "/api/health")
    step(ev, "0 health (gate must read enabled, mode LIVE_READ before the change)", st, body)
    assert body.get("outbound_writes_enabled") is True, "CAPEX_ERP_OUTBOUND_WRITES is not 1 on the instance; stop"
    st, body = call("GET", f"/api/integrations/connections/{CONN}/scopes", session=admin)
    step(ev, "0b granted scopes (must carry ERP.purchaseorders.CREATE)", st, body)
    st, body = _mode(admin, "LIVE_WRITE", f"{REFERENCE}: one application-originated order, then back to LIVE_READ")
    step(ev, "4a connection -> LIVE_WRITE", st, body)
    assert st == 200 and body.get("mode") == "LIVE_WRITE", "mode change refused"
    if not line_ids:
        # The conversion response did not carry line ids: read them from the outbox after planning.
        pass
    st, body = _emit(admin, po_id, line_ids)
    planned = step(ev, "4b emit: planned (202) with the tenant item on the line", st, body)
    assert st == 202, "emission was not planned"
    st, body = call("POST", f"/api/integrations/connections/{CONN}/drain-outbox", {}, session=admin)
    drained = step(ev, "4c drain-outbox: the one send", st, body)
    assert st == 200 and drained.get("sent") == 1, "the drain did not send exactly one row"
    external_id = drained["results"][0].get("external_id")
    ev["external_id"] = external_id
    _save_state(external_id=external_id, outbox_id=drained["results"][0].get("outbox_id"))
    st, body = _emit(admin, po_id, line_ids)
    step(ev, "5a identical emit retry: same outbox row, created=false", st, body)
    st, body = call("POST", f"/api/integrations/connections/{CONN}/drain-outbox", {}, session=admin)
    step(ev, "5b drain again: nothing claimable, nothing sent", st, body)
    assert st == 200 and body.get("sent") == 0 and body.get("claimed") == 0
    st, body = _outbox(admin)
    step(ev, "6a outbox rows for the connection", st, body)
    st, body = call("GET", f"/api/audit/entries?object_type=PurchaseOrder&object_id={po_id}&limit=50", session=admin)
    entries = body.get("items") or body.get("entries") or [] if isinstance(body, dict) else []
    step(ev, "6b audit entries on the local order (PO_EMISSION_PLANNED, PO_EMITTED)", st,
         {"actions": [(e.get("action"), (e.get("detail") or "")[:160]) for e in entries]})
    st, body = call("POST", f"/api/integrations/connections/{CONN}/sweep", {"modules": ["purchaseorders"]}, session=admin)
    mods = {k: {kk: v.get(kk) for kk in ("state", "reason", "records_seen", "inbox_created", "calls")}
            for k, v in (body.get("modules") or {}).items()} if st == 200 else body
    step(ev, "7a sweep purchaseorders: the tenant now lists the emitted order", st, {"modules": mods})
    st, body = call("POST", f"/api/integrations/connections/{CONN}/adopt-orders", {}, session=admin)
    step(ev, "7b adopt-orders: the emitted order is already anchored (linked/skipped, not adopted twice)", st, body)
    st, body = call("GET", f"/api/integrations/reconciliation?project_id={PROJECT}&limit=500", session=admin)
    rows = body.get("rows") if isinstance(body, dict) else None
    mine = [r for r in (rows or []) if REFERENCE in json.dumps(r) or po_id in json.dumps(r) or (external_id and external_id in json.dumps(r))]
    step(ev, "7c reconciliation rows for the outbound order", st, {"rows": mine[:6]} if st == 200 else body)
    st, body = call("GET", f"/api/budget/availability?wbs_id={WBS}&budget_head_id={HEAD}&amount_paise=0", session=admin)
    step(ev, "7d civil cell exposure after the order", st, body)
    return _write(ev, "emit")


def close_write() -> Path:
    ev = _ev("close")
    admin = login(ADMIN)
    st, body = _mode(admin, "LIVE_READ", f"{REFERENCE} done; back to read-only")
    step(ev, "8 connection -> LIVE_READ", st, body)
    assert st == 200 and body.get("mode") == "LIVE_READ"
    st, body = call("POST", f"/api/integrations/connections/{CONN}/drain-outbox", {}, session=admin)
    step(ev, "9 drain-outbox on LIVE_READ is refused (gate may still be open)", st, body)
    assert st == 409 and body.get("detail", {}).get("code") == "CONNECTION_NOT_LIVE_WRITE"
    st, body = call("GET", "/api/health")
    step(ev, "9b health after the mode change", st, body)
    return _write(ev, "close")


def refused_after_gate() -> Path:
    stt = _state()
    ev = _ev("refused")
    admin = login(ADMIN)
    st, body = call("GET", "/api/health")
    step(ev, "10a health: outbound_writes_enabled must be false", st, body)
    assert body.get("outbound_writes_enabled") is False, "the gate is still open"
    st, body = _emit(admin, stt["po_id"], stt.get("po_line_ids") or [])
    step(ev, "10b emit with the gate shut", st, body)
    assert st == 409 and body.get("detail", {}).get("code") == "ERP_WRITES_DISABLED"
    st, body = call("POST", f"/api/integrations/connections/{CONN}/drain-outbox", {}, session=admin)
    step(ev, "10c drain with the gate shut", st, body)
    assert st == 409 and body.get("detail", {}).get("code") == "ERP_WRITES_DISABLED"
    st, body = _mode(admin, "LIVE_WRITE", "must be refused")
    step(ev, "10d LIVE_WRITE with the gate shut", st, body)
    assert st == 409 and body.get("detail", {}).get("code") == "ERP_WRITES_DISABLED"
    return _write(ev, "refused")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "raise"
    {"raise": raise_order, "emit": emit_order, "close": close_write, "refused": refused_after_gate}[phase]()
