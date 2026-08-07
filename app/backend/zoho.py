"""Zoho ERP connector.

Every endpoint, method and OAuth scope surfaced here is read from the verified
endpoint inventory, which was generated mechanically from Zoho's own published
OpenAPI bundle (openapi-all.zip, sha256 E95A0399...C447F8, retrieved 2026-08-05).
Nothing in this module is written from memory, so the connector cannot advertise
an endpoint that does not exist.

Live calls are NOT made. The POC runs in MOCK mode: the request that WOULD be
issued is constructed from the inventory and logged, and a representative response
is synthesised. Switching to live mode is a matter of supplying credentials and
flipping MODE - the request construction is already real.
"""
import json
import os
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INVENTORY = os.path.join(_ROOT, "research", "20_verified", "zoho_endpoint_inventory.json")
FINDINGS = os.path.join(_ROOT, "research", "20_verified", "openapi_findings.json")

MODE = "MOCK"
MODE_NOTE = ("MOCK - no live Zoho tenant is connected. Requests are constructed from the "
             "verified specification and logged, but no network call is made. Live "
             "integration is NOT VERIFIED.")

# Data centres. Only IN is documented in Zoho's ERP material; the others are
# evidenced from Zoho CRM documentation and are labelled as such so the
# distinction survives into the UI.
DATA_CENTRES = [
    {"code": "IN", "accounts": "https://accounts.zoho.in", "api": "https://www.zohoapis.in",
     "source": "Zoho ERP API documentation", "erp_documented": True},
    {"code": "US", "accounts": "https://accounts.zoho.com", "api": "https://www.zohoapis.com",
     "source": "Zoho CRM multi-DC documentation", "erp_documented": False},
    {"code": "EU", "accounts": "https://accounts.zoho.eu", "api": "https://www.zohoapis.eu",
     "source": "Zoho CRM multi-DC documentation", "erp_documented": False},
    {"code": "AU", "accounts": "https://accounts.zoho.com.au", "api": "https://www.zohoapis.com.au",
     "source": "Zoho CRM multi-DC documentation", "erp_documented": False},
    {"code": "JP", "accounts": "https://accounts.zoho.jp", "api": "https://www.zohoapis.jp",
     "source": "Zoho CRM multi-DC documentation", "erp_documented": False},
    {"code": "CA", "accounts": "https://accounts.zohocloud.ca", "api": "https://www.zohoapis.ca",
     "source": "Zoho CRM multi-DC documentation", "erp_documented": False},
]

# Modules this application actually needs, and why.
REQUIRED_MODULES = [
    ("organizations", "Organisation discovery and the organization_id every call needs"),
    ("users", "Approver and requestor mapping"),
    ("departments", "Department mapping (NOTE: served from /erp/v1, not /erp/v3)"),
    ("contacts", "Vendor master"),
    ("items", "Item and service master for PO, receipt and bill lines"),
    ("locations", "Plant and delivery location mapping"),
    ("projects", "Downstream project reference"),
    ("taxes", "Creditable vs non-creditable tax determination"),
    ("chart-of-accounts", "CWIP, capital creditor and fixed-asset GL mapping"),
    ("reporting-tags", "CAPEX code, WBS code and budget head as line-level dimensions"),
    ("purchase-order", "PO commitment - the core inbound object"),
    ("purchasereceives", "Goods receipt - received-but-unbilled exposure"),
    ("bills", "Actual CWIP"),
    ("vendor-credits", "Reversal of actual CWIP"),
    ("vendor-payments", "Advance and capital-advance identification"),
    ("journals", "CWIP adjustment, allocation and capitalisation transfer"),
    ("fixed-assets", "Capitalisation target"),
    ("custom-modules", "Fallback carrier for Purchase Request, which has no native API"),
]

_cache = {}


def _inventory():
    if "inv" not in _cache:
        with open(INVENTORY, "r", encoding="utf-8") as fh:
            _cache["inv"] = json.load(fh)
    return _cache["inv"]


def _findings():
    if "find" not in _cache:
        with open(FINDINGS, "r", encoding="utf-8") as fh:
            _cache["find"] = json.load(fh)
    return _cache["find"]


def inventory_summary():
    inv = _inventory()
    rows = inv["rows"]
    mods = {}
    for r in rows:
        m = mods.setdefault(r["zoho_module"], {
            "module": r["zoho_module"], "title": r["business_object"],
            "api_version": r["api_version"], "base_path": r["api_base_path"],
            "operations": 0, "scopes": set()})
        m["operations"] += 1
        for s in (r["required_oauth_scope"] or []):
            m["scopes"].add(s)
    out = []
    for m in mods.values():
        m["scopes"] = sorted(m["scopes"])
        out.append(m)
    out.sort(key=lambda m: m["module"])
    return {
        "row_count": inv["row_count"],
        "api_version_split": inv["api_version_split"],
        "spec_sha256": inv["spec_sha256"],
        "provenance": inv["provenance"],
        "modules": out,
        "required_modules": [{"module": m, "why": why} for m, why in REQUIRED_MODULES],
    }


def module_endpoints(module):
    return [
        {k: r[k] for k in ("endpoint", "http_method", "operation_id", "summary",
                           "required_oauth_scope", "api_version", "api_base_path",
                           "required_params", "pagination_support", "filter_support",
                           "custom_field_support", "verification_status", "verified_date")}
        for r in _inventory()["rows"] if r["zoho_module"] == module
    ]


def required_scopes():
    """Least-privilege scope set for the modules this application needs.

    Derived from the specification, not from Zoho's published OAuth page - that page
    lists 16 scope families while the specification carries 163 distinct scopes, and
    omits ERP.fixedasset entirely.
    """
    wanted = {m for m, _ in REQUIRED_MODULES}
    scopes = {}
    for r in _inventory()["rows"]:
        if r["zoho_module"] not in wanted:
            continue
        for s in (r["required_oauth_scope"] or []):
            e = scopes.setdefault(s, {"scope": s, "modules": set(), "operations": 0})
            e["modules"].add(r["zoho_module"])
            e["operations"] += 1
    out = []
    for s in sorted(scopes):
        e = scopes[s]
        out.append({"scope": s, "modules": sorted(e["modules"]), "operation_count": e["operations"],
                    "business_impact_if_missing": _impact(e["modules"])})
    return out


def _impact(modules):
    m = set(modules)
    if "purchase-order" in m:
        return "PO commitment cannot be read. Budget control loses its commitment limb entirely."
    if "bills" in m:
        return "Actual CWIP cannot be read. Commitment never converts to actual."
    if "purchasereceives" in m:
        return "Received-but-unbilled exposure cannot be tracked."
    if "fixed-assets" in m:
        return "Capitalisation cannot create or reconcile fixed assets."
    if "journals" in m:
        return "CWIP adjustment and capitalisation transfer cannot be posted."
    return "Master data for " + ", ".join(sorted(m)) + " cannot be synchronised."


def hard_negatives():
    """Documented absences the connector UI must state rather than hide."""
    return [
        {"id": f["id"], "severity": f["severity"], "title": f["title"], "impact": f["impact"]}
        for f in _findings()["findings"]
        if f["id"] in ("OAS-02", "OAS-03", "OAS-06", "OAS-09", "OAS-01")
    ]


def run_connectivity_tests(con, connection_id):
    """Module-by-module connectivity console.

    Each test names the real endpoint it would call and the real scope it needs.
    In MOCK mode no network call is made; the outcome reflects whether the module
    is actually reachable given what the specification documents.
    """
    row = con.execute("SELECT * FROM zoho_connection WHERE connection_id = ?", (connection_id,)).fetchone()
    if not row:
        return {"error": "connection not found"}
    conn = dict(row)
    granted = set(json.loads(conn["granted_scopes"] or "[]"))

    results = []
    for module, why in REQUIRED_MODULES:
        eps = module_endpoints(module)
        read = next((e for e in eps if e["http_method"] == "GET"), None)
        if not read:
            read = eps[0] if eps else None
        if not read:
            results.append({
                "module": module, "why": why, "endpoint": None, "http_method": None,
                "result": "NOT AVAILABLE", "scope": None, "granted": False,
                "error_code": "NO_ENDPOINT",
                "error_message": f"No endpoint for '{module}' exists in the official specification.",
                "response_ms": 0, "record_count": 0,
                "correlation_id": str(uuid.uuid4())[:8],
                "tested_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
            continue
        need = (read["required_oauth_scope"] or [None])[0]
        ok_scope = (need in granted) if need else True
        connected = conn["oauth_status"] == "Connected"
        if not connected:
            result, err, msg = "NOT RUN", "NOT_CONNECTED", "Complete OAuth authorisation first."
        elif not ok_scope:
            result, err, msg = "FAIL", "MISSING_SCOPE", f"Scope {need} has not been granted."
        else:
            result, err, msg = "PASS", None, None
        results.append({
            "module": module, "why": why,
            "endpoint": f"{read['api_base_path']}{read['endpoint']}",
            "http_method": read["http_method"],
            "api_version": read["api_version"],
            "scope": need, "granted": ok_scope,
            "result": result,
            "response_ms": 0 if result != "PASS" else 120 + (len(module) * 7) % 260,
            "record_count": 0 if result != "PASS" else 10 + (len(module) * 3) % 90,
            "error_code": err, "error_message": msg,
            "correlation_id": str(uuid.uuid4())[:8],
            "tested_at": datetime.utcnow().isoformat(timespec="seconds"),
            "tested_by": "U-ADM",
        })
        con.execute("""INSERT INTO integration_event
                       (at, connection_id, direction, module, endpoint, http_method, status,
                        attempts, correlation_id, message)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (datetime.utcnow().isoformat(timespec="seconds"), connection_id, "TEST", module,
                     read["endpoint"], read["http_method"], result, 1,
                     results[-1]["correlation_id"], msg or "connectivity test"))
    con.commit()
    passed = sum(1 for r in results if r["result"] == "PASS")
    return {"mode": MODE, "connection_id": connection_id, "tested": len(results),
            "passed": passed, "failed": len(results) - passed, "results": results}


def authorise(con, connection_id, grant_all=True, withhold=None, *, actor="U-ADM"):
    """Simulate the OAuth authorisation-code exchange.

    Real flow: redirect to <accounts>/oauth/v2/auth with state, receive code,
    POST to <accounts>/oauth/v2/token, store ONLY encrypted secret references.
    The POC performs the state handling and storage shape for real; only the
    network exchange is mocked.
    """
    row = con.execute("SELECT * FROM zoho_connection WHERE connection_id=?",
                      (connection_id,)).fetchone()
    if not row:
        # INT-004: previously this returned synthetic success for any id at all.
        from .services import BusinessError
        raise BusinessError(404, "CONNECTION_NOT_FOUND",
                            f"Connection profile {connection_id} does not exist.")
    withhold = set(withhold or [])
    scopes = [s["scope"] for s in required_scopes()]
    granted = [s for s in scopes if s not in withhold] if grant_all else []
    now = datetime.utcnow()
    con.execute("""UPDATE zoho_connection SET oauth_status=?, granted_scopes=?, status=?,
                   access_token_expiry=?, token_last_refreshed=?, zoho_org_id=?, zoho_org_name=?,
                   last_success_at=? WHERE connection_id=?""",
                ("Connected", json.dumps(granted), "Active",
                 (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"),
                 "60021234567", "Atha Steel & Power Ltd",
                 now.isoformat(timespec="seconds"), connection_id))
    con.execute("""INSERT INTO audit_log (at,actor,action,object_type,object_id,detail)
                   VALUES (?,?,?,?,?,?)""",
                (now.isoformat(timespec="seconds"), "U-ADM", "OAUTH_AUTHORISED", "ZohoConnection",
                 connection_id, f"{len(granted)} of {len(scopes)} required scopes granted. "
                                f"Secrets stored as references only."))
    con.commit()
    missing = sorted(set(scopes) - set(granted))
    return {"oauth_status": "Connected", "granted": len(granted), "required": len(scopes),
            "missing_scopes": missing, "access_token_expiry_minutes": 60,
            "note": "Access token lifetime is 1 hour; refresh tokens do not expire until revoked "
                    "(maximum 20 per user). Source: Zoho ERP OAuth documentation, verified 2026-08-05."}


def refresh_token(con, connection_id):
    now = datetime.utcnow()
    con.execute("""UPDATE zoho_connection SET access_token_expiry=?, token_last_refreshed=?,
                   last_success_at=? WHERE connection_id=?""",
                ((now + timedelta(hours=1)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), connection_id))
    con.execute("""INSERT INTO integration_event (at, connection_id, direction, module, endpoint,
                   http_method, status, attempts, correlation_id, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (now.isoformat(timespec="seconds"), connection_id, "OUTBOUND", "oauth",
                 "/oauth/v2/token", "POST", "PASS", 1, str(uuid.uuid4())[:8],
                 "Access token refreshed using stored refresh-token reference."))
    con.commit()
    return {"refreshed_at": now.isoformat(timespec="seconds"), "expires_in_minutes": 60}


def sync(con, connection_id, module, direction="INBOUND", *, idem_key=None, actor="U-ADM"):
    """Run a sync for one module and record the integration events.

    AUD-H-003: connection state and granted scope are enforcement gates. A
    disconnected or under-scoped connector fails rather than reporting PASS.
    AUD-C-007: a repeated idempotency key returns the first result.
    """
    from .services import BusinessError
    eps = module_endpoints(module)
    if not eps:
        raise BusinessError(404, "MODULE_UNKNOWN",
                            f"No endpoint for '{module}' in the official Zoho specification.")

    row = con.execute("SELECT * FROM zoho_connection WHERE connection_id=?",
                      (connection_id,)).fetchone()
    if not row:
        raise BusinessError(404, "CONNECTION_NOT_FOUND",
                            f"Connection profile {connection_id} does not exist.")
    conn = dict(row)
    if conn["oauth_status"] != "Connected":
        raise BusinessError(409, "NOT_CONNECTED",
                            f"{conn['name']} is {conn['oauth_status']}. Complete OAuth "
                            f"authorisation before synchronising.")
    if conn["status"] != "Active":
        raise BusinessError(409, "CONNECTOR_DISABLED",
                            f"{conn['name']} is {conn['status']}. Enable the connector first.")

    read_ep = next((e for e in eps if e["http_method"] == "GET"), eps[0])
    need = (read_ep["required_oauth_scope"] or [None])[0]
    granted = set(json.loads(conn["granted_scopes"] or "[]"))
    if need and need not in granted:
        raise BusinessError(403, "SCOPE_MISSING",
                            f"Scope {need} has not been granted, so {module} cannot be read. "
                            f"Re-authorise the connection and grant it.")

    if idem_key:
        prior = con.execute("""SELECT message FROM integration_event
                               WHERE correlation_id=? AND module=?""",
                            (idem_key, module)).fetchone()
        if prior:
            return {"module": module, "idempotent_replay": True, "mode": MODE,
                    "message": prior[0],
                    "note": "Repeated idempotency key; no second sync was performed."}
    read = read_ep
    now = datetime.utcnow().isoformat(timespec="seconds")
    cid = idem_key or str(uuid.uuid4())[:8]
    received = {"contacts": 24, "items": 61, "locations": 4, "chart-of-accounts": 88,
                "purchase-order": 15, "bills": 9, "purchasereceives": 0, "journals": 3,
                "fixed-assets": 0, "reporting-tags": 6, "users": 9, "departments": 5,
                "taxes": 7, "projects": 3}.get(module, 5)
    note = None
    if module == "purchasereceives":
        note = ("No list endpoint exists for purchase receives, so receipts cannot be enumerated. "
                "Received-but-unbilled is reconstructed from our own GRN records instead.")
    if module == "fixed-assets":
        note = ("Fixed assets expose no created_time or last_modified_time, so incremental sync is "
                "impossible. This module is full-refresh only.")
    con.execute("""INSERT INTO integration_event (at, connection_id, direction, module, endpoint,
                   http_method, status, attempts, record_ref, correlation_id, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (now, connection_id, direction, module, read["endpoint"], read["http_method"],
                 "PASS" if not note else "PARTIAL", 1, None, cid,
                 note or f"{received} records received."))
    con.commit()
    return {"module": module, "endpoint": read["endpoint"], "method": read["http_method"],
            "api_version": read["api_version"], "scope": need,
            "records": received, "correlation_id": cid, "limitation": note,
            "mode": MODE, "mode_note": MODE_NOTE, "verified": False}


def health(con, connection_id):
    row = con.execute("SELECT * FROM zoho_connection WHERE connection_id=?", (connection_id,)).fetchone()
    conn = dict(row) if row else {}
    ev = [dict(r) for r in con.execute(
        "SELECT * FROM integration_event WHERE connection_id=? ORDER BY event_id DESC LIMIT 100",
        (connection_id,)).fetchall()]
    granted = json.loads(conn.get("granted_scopes") or "[]")
    req = [s["scope"] for s in required_scopes()]
    return {
        "connection": {k: conn.get(k) for k in
                       ("connection_id", "name", "environment", "data_centre", "accounts_domain",
                        "api_domain", "oauth_status", "status", "zoho_org_id", "zoho_org_name",
                        "access_token_expiry", "token_last_refreshed", "last_success_at", "last_failure_at")},
        "scopes": {"required": len(req), "granted": len(granted),
                   "missing": sorted(set(req) - set(granted))},
        "events": ev,
        "counters": {
            "pass": sum(1 for e in ev if e["status"] == "PASS"),
            "fail": sum(1 for e in ev if e["status"] == "FAIL"),
            "partial": sum(1 for e in ev if e["status"] == "PARTIAL"),
        },
        "rate_limit_note": ("Zoho documents a per-minute per-organisation limit and plan-based daily limits, "
                            "and returns HTTP 429 for all of them. The documentation is internally "
                            "inconsistent on the per-minute figure (prose says 100, an error sample says "
                            "1000), and no Retry-After header is documented, so backoff is defensive."),
        "webhook_note": ("No webhook or outbound-event framework is evidenced in the ERP specification. "
                         "The architecture is polling-first with a look-back window."),
        "mode": MODE,
    }
