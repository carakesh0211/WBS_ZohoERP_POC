"""Before/after snapshot of the identified DEMO WBS purchase orders (READ ONLY).

    python tools/erp_demo/po_snapshot.py before
    python tools/erp_demo/po_snapshot.py after
    python tools/erp_demo/po_snapshot.py diff <before.json> <after.json>

Reads through the app's LiveTransport (GET only; the credential file lives
outside the repository and nothing of it is printed) and writes a JSON record
under docs/fable51/evidence/erp-demo/po-stamping/. The record holds, per
order: id, number, status, currency, total, last_modified_time, the header
custom fields, and per line: line_item_id, item_id, rate, quantity, tax_id and
the line custom fields. Amounts are copied as the strings Zoho sent.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.integration import erp  # noqa: E402
from app.backend.integration.live_transport import LiveTransport  # noqa: E402

ORG = "60074128927"
ORG_NAME = "DEMO WBS"
#: The seven identified demo orders (PO-00001 .. PO-00007), and nothing else.
ORDERS = {
    "PO-00001": "3912780000000085001",
    "PO-00002": "3912780000000085009",
    "PO-00003": "3912780000000075008",
    "PO-00004": "3912780000000086001",
    "PO-00005": "3912780000000087001",
    "PO-00006": "3912780000000096001",
    "PO-00007": "3912780000000099012",
}
OUT = ROOT / "docs/fable51/evidence/erp-demo/po-stamping"


def _line(li: dict) -> dict:
    return {
        "line_item_id": li.get("line_item_id"), "item_id": li.get("item_id"),
        "account_id": li.get("account_id"), "item_order": li.get("item_order"),
        "description": li.get("description"), "rate": li.get("rate"),
        "quantity": li.get("quantity"), "tax_id": li.get("tax_id") or None,
        "hsn_or_sac": li.get("hsn_or_sac") or None,
        "quantity_received": li.get("quantity_received"), "quantity_billed": li.get("quantity_billed"),
        "item_custom_fields": [
            {"index": f.get("index"), "api_name": f.get("api_name"), "value": f.get("value")}
            for f in (li.get("item_custom_fields") or [])],
    }


def snapshot(label: str) -> Path:
    t = LiveTransport()
    base = erp.ErpAdapter(organization_id=ORG, transport=t).base_url("IN")
    orgs = t.request(method="GET", base_url=base, path="/organizations",
                     scope="ERP.settings.READ", params={})
    pinned = [o for o in orgs.get("organizations") or [] if str(o.get("organization_id")) == ORG]
    if not pinned or pinned[0].get("name") != ORG_NAME:
        raise SystemExit(f"REFUSED: organisation {ORG} is not visible as {ORG_NAME!r}; nothing read further")
    record = {"label": label, "taken_at": datetime.now(timezone.utc).isoformat(),
              "organization": {"organization_id": ORG, "name": pinned[0].get("name"),
                               "currency_code": pinned[0].get("currency_code")},
              "orders": {}}
    for number, po_id in ORDERS.items():
        body = t.request(method="GET", base_url=base, path=f"/purchaseorders/{po_id}",
                         scope="ERP.purchaseorders.READ", params={"organization_id": ORG})
        po = body["purchaseorder"]
        assert po.get("purchaseorder_number") == number, (number, po.get("purchaseorder_number"))
        record["orders"][number] = {
            "purchaseorder_id": po.get("purchaseorder_id"), "vendor_id": po.get("vendor_id"),
            "status": po.get("status"), "currency_code": po.get("currency_code"),
            "total": po.get("total"), "last_modified_time": po.get("last_modified_time"),
            "reference_number": po.get("reference_number"),
            "custom_fields": [{"customfield_id": f.get("customfield_id"), "api_name": f.get("api_name"),
                               "value": f.get("value")} for f in (po.get("custom_fields") or [])],
            "lines": [_line(li) for li in po.get("line_items") or []],
        }
    record["transport"] = {k: v for k, v in t.describe().items() if k in ("calls_made", "api_domain")}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{label}-{record['taken_at'][:19].replace(':', '')}.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


def diff(before: Path, after: Path) -> int:
    a = json.loads(before.read_text(encoding="utf-8"))["orders"]
    b = json.loads(after.read_text(encoding="utf-8"))["orders"]
    changed = 0
    for number in ORDERS:
        x, y = a[number], b[number]
        for key in ("status", "currency_code", "total", "vendor_id"):
            if x[key] != y[key]:
                print(f"!! {number}.{key}: {x[key]!r} -> {y[key]!r}  (NOT an authorised change)")
        cx = {f["api_name"]: f["value"] for f in x["custom_fields"]}
        cy = {f["api_name"]: f["value"] for f in y["custom_fields"]}
        if cx != cy:
            changed += 1
            print(f"{number} header custom_fields: {cx} -> {cy}")
        for lx, ly in zip(x["lines"], y["lines"]):
            for key in ("rate", "quantity", "item_id", "tax_id", "quantity_received", "quantity_billed"):
                if lx[key] != ly[key]:
                    print(f"!! {number} line {lx['line_item_id']}.{key}: {lx[key]!r} -> {ly[key]!r}  (NOT an authorised change)")
            fx = {f["api_name"]: f["value"] for f in lx["item_custom_fields"]}
            fy = {f["api_name"]: f["value"] for f in ly["item_custom_fields"]}
            if fx != fy:
                changed += 1
                print(f"{number} line {lx['line_item_id']} custom fields: {fx} -> {fy}")
        if len(x["lines"]) != len(y["lines"]):
            print(f"!! {number}: line count {len(x['lines'])} -> {len(y['lines'])}  (NOT an authorised change)")
    print(f"{changed} custom-field change(s) between {before.name} and {after.name}")
    return changed


if __name__ == "__main__":
    if sys.argv[1] == "diff":
        sys.exit(0 if diff(Path(sys.argv[2]), Path(sys.argv[3])) >= 0 else 1)
    p = snapshot(sys.argv[1])
    rec = json.loads(p.read_text(encoding="utf-8"))
    print("wrote", p.relative_to(ROOT))
    print("organisation:", rec["organization"])
    for n, o in rec["orders"].items():
        print(f"{n} {o['purchaseorder_id']} {o['status']:<16} {o['currency_code']} total={o['total']} "
              f"header_cf={[f['api_name'] + '=' + str(f['value']) for f in o['custom_fields']]}")
        for li in o["lines"]:
            print(f"    line {li['line_item_id']} item={li['item_id']} rate={li['rate']} qty={li['quantity']} "
                  f"tax={li['tax_id']} cf={[(f['api_name'], f['value']) for f in li['item_custom_fields']]}")
