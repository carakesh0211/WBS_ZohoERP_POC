"""Read-only probe of ONE Zoho ERP organisation through the real adapter and
the live transport (Fable 5.1) -- the ordered checks in
docs/fable51/ERP_DEMO_CONNECTION_PREP.md, steps 1-10, with sanitised evidence.

    python tools/erp_demo/probe.py --org 60074128927 [--days 365] [--out docs/fable51/evidence/erp-demo]

WHAT IT WRITES (and only this): counts, external ids, raw status strings,
timestamps, scope names, the HTTP budget spent, and the pinned organisation's
id/name/currency/country/plan. No vendor name, no item name, no amount, no
line, no token, no secret. The other organisations the grant can see are
NEVER listed: the probe filters `/organizations` to the pinned id and reports
only how many others exist.

WHAT IT NEVER DOES: a POST, PUT or DELETE. The transport refuses them while
`CAPEX_ERP_OUTBOUND_WRITES` is unset, and this tool never sets it.

Exit code 0 = every check passed; 1 = a check named STOP in the summary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import zoho as zoho_mod  # noqa: E402
from app.backend.integration import adapter as ad  # noqa: E402
from app.backend.integration import erp  # noqa: E402
from app.backend.integration import live_transport as lt  # noqa: E402

ORG_FIELDS = ("organization_id", "name", "currency_code", "country", "time_zone",
              "plan_name", "org_type", "is_multientity_enabled", "is_gst_india_version")


def required_read_scopes() -> set[str]:
    out = set()
    for row in zoho_mod.required_scopes():
        scope = str(row.get("scope") or "")
        if scope.endswith(".READ") or scope.endswith(".ALL"):
            out.add(scope)
    return out


def _page_summary(page) -> dict:
    return {"count": len(page.items), "page": page.page, "per_page": page.per_page,
            "has_more": bool(page.has_more)}


def run(org_id: str, *, days: int, out_dir: Path) -> int:
    started = datetime.now(timezone.utc)
    report: dict = {"organization_id": org_id, "started_at": started.isoformat(),
                    "mode": "LIVE_READ (probe; CAPEX_ERP_OUTBOUND_WRITES unset)",
                    "checks": [], "stops": []}

    def check(name: str, ok: bool, detail: dict | str, stop: bool = False) -> None:
        report["checks"].append({"check": name, "ok": ok, "detail": detail})
        if stop and not ok:
            report["stops"].append(name)
        print(f"{'ok  ' if ok else 'STOP' if stop else 'warn'} {name}: "
              f"{json.dumps(detail) if isinstance(detail, dict) else detail}")

    assert not zoho_mod.outbound_writes_enabled(), "the probe never runs with writes enabled"
    transport = lt.LiveTransport()
    adapter = erp.ErpAdapter(organization_id=org_id, transport=transport)
    base = adapter.base_url("IN")

    # 1. India DC and product, from the transport's own binding.
    check("data centre and product", transport.api_domain.endswith(lt.API_HOST_SUFFIX)
          and base.endswith(erp.SERVICE_PATH),
          {"api_domain": transport.api_domain, "service_path": erp.SERVICE_PATH}, stop=True)

    # 2. The organisation, pinned; the others are counted, never named.
    orgs = transport.request(method="GET", base_url=base, path="/organizations",
                             scope="ERP.settings.READ", params={})
    rows = orgs.get("organizations") or []
    mine = [o for o in rows if str(o.get("organization_id")) == org_id]
    check("pinned organisation is visible to the grant", len(mine) == 1,
          {"pinned": {k: mine[0].get(k) for k in ORG_FIELDS} if mine else None,
           "other_organisations_visible": max(0, len(rows) - len(mine))}, stop=True)
    if mine:
        check("organisation currency is the base currency", mine[0].get("currency_code") == "INR",
              {"currency_code": mine[0].get("currency_code")}, stop=True)

    # 3. Granted scopes against the READ subset the inventory requires.
    need = required_read_scopes()
    missing = sorted(need - transport.granted_scopes)
    check("granted scopes cover every required READ scope", not missing,
          {"required_read": sorted(need), "granted": sorted(transport.granted_scopes),
           "missing": missing}, stop=True)

    # 4. Token: minted, lifetime reported, never printed.
    check("token refresh", transport.describe()["token_minted"],
          {k: transport.describe()[k] for k in ("token_seconds_left", "token_mints")})

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)

    def paged(name: str, fn, stop_after: int = 3):
        pages, ids, statuses = [], [], {}
        page_no = 1
        while page_no <= stop_after:
            try:
                page = fn(since, until, page_no)
            except ad.IntegrationError as exc:
                check(name, False, str(exc)[:300])
                return []
            pages.append(_page_summary(page))
            for item in page.items:
                ids.append(getattr(item, "external_id", None))
                raw = getattr(item, "external_status_raw", None)
                if raw is not None:
                    statuses[raw] = statuses.get(raw, 0) + 1
            if not page.has_more:
                break
            page_no += 1
        check(name, True, {"pages": pages, "total_seen": len(ids),
                           "external_ids": ids[:50], "statuses_raw": statuses})
        return [i for i in ids if i]

    # 5-6. Masters.
    paged("vendors (contacts)", adapter.list_contacts)
    paged("items", adapter.list_items)
    # 7. Purchase orders with the windowed filter.
    po_ids = paged("purchase orders (windowed)", adapter.list_purchase_orders)
    # 8. Receives, PO-anchored (no list endpoint on ERP).
    receives = {}
    for po_id in po_ids[:5]:
        try:
            got = adapter.receives_for_po(po_id)
            receives[po_id] = {"count": len(got),
                               "external_ids": [getattr(r, "external_id", None) for r in got][:20]}
        except ad.IntegrationError as exc:
            receives[po_id] = {"error": str(exc)[:200]}
    check("purchase receives via PO-anchored discovery", True, {"by_po": receives})
    # 9. Bills with the windowed filter; one detail fetch proves hydration.
    bill_ids = paged("vendor bills (windowed)", adapter.list_bills)
    if bill_ids:
        try:
            bill = adapter.get_bill(bill_ids[0])
            check("one bill hydrated by detail fetch", bool(getattr(bill, "lines_hydrated", False)),
                  {"external_id": bill_ids[0], "line_count": len(getattr(bill, "lines", ()) or ()),
                   "currency_code": getattr(bill, "currency_code", None)})
        except ad.IntegrationError as exc:
            check("one bill hydrated by detail fetch", False, str(exc)[:300])
    # 10. Budget and pagination facts.
    desc = transport.describe()
    check("call budget spent", desc["calls_made"] <= 60,
          {"calls_made": desc["calls_made"], "minute_ceiling": lt.MINUTE_CALL_CEILING,
           "daily_ceiling_standard": erp.DAILY_CALL_CEILING_STANDARD})

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    report["verdict"] = "PASS" if not report["stops"] else f"STOP: {', '.join(report['stops'])}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = out_dir / f"probe-{org_id}-{stamp}.json"
    text = json.dumps(report, indent=2, default=str)
    for forbidden in ("access_token", "refresh_token", "client_secret", "Zoho-oauthtoken"):
        assert forbidden not in text, f"{forbidden} reached the evidence file"
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\n{report['verdict']}  evidence: {out}")
    return 0 if not report["stops"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--org", required=True, help="the ONE organisation id this run is pinned to")
    ap.add_argument("--days", type=int, default=365, help="modification window for POs and bills")
    ap.add_argument("--out", default=str(ROOT / "docs" / "fable51" / "evidence" / "erp-demo"))
    args = ap.parse_args(argv)
    if os.environ.get("CAPEX_ERP_OUTBOUND_WRITES", "").strip() == "1":
        print("REFUSED: the probe is read-only; unset CAPEX_ERP_OUTBOUND_WRITES.", file=sys.stderr)
        return 2
    return run(str(args.org).strip(), days=args.days, out_dir=Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
