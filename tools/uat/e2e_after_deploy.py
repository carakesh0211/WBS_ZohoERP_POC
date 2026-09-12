"""Steps 5-11 of the UAT end-to-end cycle, against the live UAT AppSail, AFTER the
release carrying adoption is deployed.

    python tools/uat/e2e_after_deploy.py

Sequence (all through the app's own routes, as the seeded identities; every
ERP call the app makes is a GET; nothing here writes to Zoho):
  a. sweep            -> the tenant's PO-00008, receive and bill are in the inbox
  b. adopt-orders     -> LINK: PO-00008's cf_capex_ref == the local order's derived key
                         (also adopts/refuses the seven demo orders: two unmapped heads ->
                         ADOPTION_DIMENSION_INVALID, the JPY one -> FOREIGN_CURRENCY_BASIS_MISSING)
  c. sweep            -> the PO-anchored walk mirrors the receive; the bill sweep matches the bill
  d. adopt-orders     -> second run: nothing new (idempotent)
  e. read-backs       -> reconciliation, cell exposure, control totals, audit verify, exceptions
Writes docs/fable51/evidence/e2e/after-deploy-<ts>.json. No credential is printed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.uat.e2e_cycle import ADMIN, CONN, HEAD, OUT, PROJECT, REFERENCE, WBS, call, login, step  # noqa: E402

LOCAL_PO = "PO-15C1EEC1E554"
TENANT_PO = "3912780000000109005"


def main() -> Path:
    ev = {"reference": REFERENCE, "local_po": LOCAL_PO, "tenant_po": TENANT_PO,
          "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}
    s = login(ADMIN)
    st, body = call("GET", "/readyz")
    step(ev, "readyz", st, body)

    def sweep(name):
        st, body = call("POST", f"/api/integrations/connections/{CONN}/sweep", {}, session=s)
        mods = {k: {kk: v.get(kk) for kk in ("state", "reason", "records_seen", "inbox_created", "calls")}
                for k, v in (body.get("modules") or {}).items()} if st == 200 else body
        step(ev, name, st, {"modules": mods, "receive_lines_recorded": body.get("receive_lines_recorded"),
                            "exceptions": body.get("exceptions"), "transport": body.get("transport")} if st == 200 else body)
        return body

    def adopt(name):
        st, body = call("POST", f"/api/integrations/connections/{CONN}/adopt-orders", {}, session=s)
        step(ev, name, st, body)
        return body

    sweep("a sweep: tenant records into the inbox")
    adopt("b adopt-orders: LINK PO-00008 to the local order; demo orders adopted or refused")
    sweep("c sweep: receive walk and bill match against the linked order")
    adopt("d adopt-orders again: idempotent")

    st, body = call("GET", f"/api/integrations/reconciliation?project_id={PROJECT}&limit=500", session=s)
    rows = body.get("items") if isinstance(body, dict) else None
    mine = [r for r in (rows or []) if REFERENCE in json.dumps(r) or LOCAL_PO in json.dumps(r) or TENANT_PO in json.dumps(r)]
    step(ev, "8 reconciliation rows for the cycle", st, {"count_all": len(rows or []), "rows": mine[:6]} if st == 200 else body)
    st, body = call("GET", f"/api/budget/availability?wbs_id={WBS}&budget_head_id={HEAD}&amount_paise=0", session=s)
    step(ev, "9 cell exposure after the cycle", st, body)
    st, body = call("GET", "/api/integrations/control-totals", session=s)
    step(ev, "9b control totals", st, body)
    st, body = call("GET", "/api/audit/anchors/verify", session=s)
    step(ev, "10 audit chain verify", st, body)
    st, body = call("GET", "/api/integrations/exceptions?limit=50", session=s)
    step(ev, "11 open exceptions (expect ADOPTION_DIMENSION_INVALID x2, FOREIGN_CURRENCY_BASIS_MISSING for the JPY order)", st,
         {"open": [{k: e.get(k) for k in ("exception_id", "kind", "object_id", "status", "source_paise")}
                   for e in (body.get("items") or [])]} if st == 200 else body)
    st, body = call("GET", "/api/integrations/exceptions/unattributed?limit=50", session=s)
    step(ev, "11b unattributed", st, {"count": len(body.get("items") or [])} if st == 200 else body)
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"after-deploy-{ev['started_at'][:19].replace(':', '')}.json"
    p.write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    print("wrote", p.relative_to(ROOT))
    return p


if __name__ == "__main__":
    main()
