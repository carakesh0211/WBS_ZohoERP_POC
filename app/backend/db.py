"""SQLite schema and seed data for the CAPEX & WBS Control Hub POC.

Money is stored as INTEGER PAISE throughout. Never use floats for money.
Display formatting (Indian grouping, lakh/crore) happens in the UI layer only.
"""
import os
import sqlite3

# DB_PATH is overridable so the app can run on a host whose only writable
# location is a mounted disk or a temp directory.
DB_PATH = os.environ.get(
    "CAPEX_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "capex.db"),
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE entity (
  entity_id     TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  base_currency TEXT NOT NULL DEFAULT 'INR',
  zoho_org_id   TEXT
);

CREATE TABLE plant (
  plant_id  TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entity(entity_id),
  name      TEXT NOT NULL,
  location  TEXT
);

CREATE TABLE budget_head (
  budget_head_id TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  asset_category TEXT,
  cwip_gl        TEXT,
  active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE app_user (
  user_id  TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  role     TEXT NOT NULL
);

CREATE TABLE project (
  project_id       TEXT PRIMARY KEY,
  capex_code       TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  entity_id        TEXT NOT NULL REFERENCES entity(entity_id),
  plant_id         TEXT NOT NULL REFERENCES plant(plant_id),
  department       TEXT,
  owner_user_id    TEXT REFERENCES app_user(user_id),
  sponsor          TEXT,
  start_date       TEXT,
  planned_end_date TEXT,
  project_type     TEXT,
  asset_category   TEXT,
  cwip_gl          TEXT,
  status           TEXT NOT NULL DEFAULT 'Draft',
  zoho_project_id  TEXT,
  created_at       TEXT NOT NULL,
  created_by       TEXT
);

-- Multi-level WBS. parent_wbs_id NULL => level 1 directly under the project.
CREATE TABLE wbs_element (
  wbs_id            TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES project(project_id),
  parent_wbs_id     TEXT REFERENCES wbs_element(wbs_id),
  wbs_code          TEXT NOT NULL UNIQUE,
  description       TEXT NOT NULL,
  level             INTEGER NOT NULL,
  sort_order        INTEGER NOT NULL DEFAULT 0,
  budget_head_id    TEXT REFERENCES budget_head(budget_head_id),
  responsible_user  TEXT,
  planned_start     TEXT,
  planned_end       TEXT,
  actual_start      TEXT,
  actual_end        TEXT,
  progress_pct      REAL NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'Draft',
  asset_category    TEXT,
  settlement_receiver TEXT,
  allow_procurement INTEGER NOT NULL DEFAULT 1,
  allow_posting     INTEGER NOT NULL DEFAULT 1,
  is_abandoned      INTEGER NOT NULL DEFAULT 0
);

-- Budget register. The ORIGINAL version is immutable; revisions are separate rows.
CREATE TABLE budget_line (
  budget_line_id  TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES project(project_id),
  wbs_id          TEXT REFERENCES wbs_element(wbs_id),
  budget_head_id  TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  kind            TEXT NOT NULL,          -- ORIGINAL | SUPPLEMENT | RETURN | TRANSFER_IN | TRANSFER_OUT
  amount_paise    INTEGER NOT NULL,       -- signed: RETURN and TRANSFER_OUT are negative
  version_no      INTEGER NOT NULL DEFAULT 1,
  revision_id     TEXT REFERENCES budget_revision(revision_id),
  effective_date  TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  created_by      TEXT
);

CREATE TABLE budget_revision (
  revision_id     TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES project(project_id),
  revision_no     INTEGER NOT NULL,
  kind            TEXT NOT NULL,          -- SUPPLEMENT | RETURN | TRANSFER
  reason          TEXT NOT NULL,
  requested_by    TEXT NOT NULL,
  requested_at    TEXT NOT NULL,
  approver        TEXT,
  approved_at     TEXT,
  approval_ref    TEXT,
  status          TEXT NOT NULL DEFAULT 'Submitted',
  effective_date  TEXT
);

CREATE TABLE purchase_request (
  pr_id           TEXT PRIMARY KEY,
  pr_number       TEXT NOT NULL UNIQUE,
  project_id      TEXT NOT NULL REFERENCES project(project_id),
  wbs_id          TEXT NOT NULL REFERENCES wbs_element(wbs_id),
  budget_head_id  TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  description     TEXT,
  amount_paise    INTEGER NOT NULL,
  requested_by    TEXT NOT NULL,
  requested_at    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'Draft',
  check_result    TEXT,                   -- WITHIN_BUDGET | EXCEEDS_BUDGET
  approver        TEXT,
  approved_at     TEXT,
  exception_reason TEXT,
  reserves_budget INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE purchase_order (
  po_id           TEXT PRIMARY KEY,
  po_number       TEXT NOT NULL UNIQUE,
  pr_id           TEXT REFERENCES purchase_request(pr_id),
  project_id      TEXT NOT NULL REFERENCES project(project_id),
  vendor_name     TEXT NOT NULL,
  currency        TEXT NOT NULL DEFAULT 'INR',
  exchange_rate   REAL NOT NULL DEFAULT 1.0,
  status          TEXT NOT NULL DEFAULT 'Draft',
  ordered_at      TEXT,
  amendment_no    INTEGER NOT NULL DEFAULT 0,
  closed_residual_released INTEGER NOT NULL DEFAULT 0,
  zoho_po_id      TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE po_line (
  po_line_id      TEXT PRIMARY KEY,
  po_id           TEXT NOT NULL REFERENCES purchase_order(po_id),
  line_no         INTEGER NOT NULL,
  wbs_id          TEXT NOT NULL REFERENCES wbs_element(wbs_id),
  budget_head_id  TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  description     TEXT,
  quantity        REAL NOT NULL DEFAULT 1,
  rate_paise      INTEGER NOT NULL,
  amount_paise    INTEGER NOT NULL,
  tax_paise       INTEGER NOT NULL DEFAULT 0,
  non_creditable_tax_paise INTEGER NOT NULL DEFAULT 0,
  freight_paise   INTEGER NOT NULL DEFAULT 0,
  zoho_line_item_id TEXT
);

CREATE TABLE grn (
  grn_id      TEXT PRIMARY KEY,
  grn_number  TEXT NOT NULL UNIQUE,
  po_id       TEXT NOT NULL REFERENCES purchase_order(po_id),
  received_at TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'Approved',
  is_reversal INTEGER NOT NULL DEFAULT 0,
  zoho_receive_id TEXT
);

CREATE TABLE grn_line (
  grn_line_id  TEXT PRIMARY KEY,
  grn_id       TEXT NOT NULL REFERENCES grn(grn_id),
  po_line_id   TEXT NOT NULL REFERENCES po_line(po_line_id),
  quantity     REAL NOT NULL,
  amount_paise INTEGER NOT NULL
);

CREATE TABLE bill (
  bill_id      TEXT PRIMARY KEY,
  bill_number  TEXT NOT NULL UNIQUE,
  po_id        TEXT REFERENCES purchase_order(po_id),
  vendor_name  TEXT NOT NULL,
  bill_date    TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'Approved',
  is_reversal  INTEGER NOT NULL DEFAULT 0,
  doc_type     TEXT NOT NULL DEFAULT 'BILL',   -- BILL | CREDIT_NOTE | DEBIT_NOTE
  zoho_bill_id TEXT,
  created_at   TEXT NOT NULL,
  -- The MAKER. `bill.void` is in auth.MAKER_CHECKER and services.void_bill
  -- reads this column, so without it segregation of duties on a void was
  -- structurally inert: require_separation short-circuits on a falsy maker
  -- and compared nobody, letting the raiser of a bill void it themselves.
  --
  -- Nullable, because bills arrive by ingestion as well as by seeding and a
  -- NOT NULL here would make an unattributed source row unstorable rather
  -- than unvoidable. An ABSENT maker is not a waiver: services.void_bill
  -- passes require_maker=True and refuses the void outright, so "we do not
  -- know who raised this" can never read as "anyone may void it".
  created_by   TEXT
);

CREATE TABLE bill_line (
  bill_line_id TEXT PRIMARY KEY,
  bill_id      TEXT NOT NULL REFERENCES bill(bill_id),
  po_line_id   TEXT REFERENCES po_line(po_line_id),
  wbs_id       TEXT NOT NULL REFERENCES wbs_element(wbs_id),
  budget_head_id TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  description  TEXT,
  quantity     REAL NOT NULL DEFAULT 1,
  amount_paise INTEGER NOT NULL,
  non_creditable_tax_paise INTEGER NOT NULL DEFAULT 0,
  freight_paise INTEGER NOT NULL DEFAULT 0,
  zoho_purchaseorder_item_id TEXT
);

CREATE TABLE capitalisation_request (
  cap_id       TEXT PRIMARY KEY,
  cap_number   TEXT NOT NULL UNIQUE,
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  requested_by TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'Submitted',
  approver     TEXT,
  approved_at  TEXT,
  cap_date     TEXT,
  total_paise  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE asset_allocation (
  allocation_id TEXT PRIMARY KEY,
  cap_id        TEXT NOT NULL REFERENCES capitalisation_request(cap_id),
  wbs_id        TEXT REFERENCES wbs_element(wbs_id),
  asset_name    TEXT NOT NULL,
  asset_category TEXT,
  amount_paise  INTEGER NOT NULL,
  is_writeoff   INTEGER NOT NULL DEFAULT 0,
  writeoff_reason TEXT,
  zoho_fixed_asset_id TEXT
);

CREATE TABLE audit_log (
  audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT NOT NULL,
  detail      TEXT,
  before_json TEXT,
  after_json  TEXT
);

CREATE TABLE zoho_connection (
  connection_id   TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  entity_id       TEXT REFERENCES entity(entity_id),
  environment     TEXT NOT NULL DEFAULT 'Sandbox',
  data_centre     TEXT NOT NULL DEFAULT 'IN',
  accounts_domain TEXT NOT NULL,
  api_domain      TEXT NOT NULL,
  client_id_ref   TEXT,
  client_secret_ref TEXT,
  refresh_token_ref TEXT,
  redirect_uri    TEXT,
  oauth_status    TEXT NOT NULL DEFAULT 'Not Connected',
  access_token_expiry TEXT,
  token_last_refreshed TEXT,
  zoho_org_id     TEXT,
  zoho_org_name   TEXT,
  granted_scopes  TEXT,
  status          TEXT NOT NULL DEFAULT 'Disabled',
  last_success_at TEXT,
  last_failure_at TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE integration_event (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  connection_id TEXT REFERENCES zoho_connection(connection_id),
  direction    TEXT NOT NULL,
  module       TEXT NOT NULL,
  endpoint     TEXT,
  http_method  TEXT,
  status       TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 1,
  record_ref   TEXT,
  correlation_id TEXT,
  message      TEXT
);

CREATE INDEX idx_wbs_project ON wbs_element(project_id);
CREATE INDEX idx_wbs_parent  ON wbs_element(parent_wbs_id);
CREATE INDEX idx_budget_wbs  ON budget_line(wbs_id);
CREATE INDEX idx_poline_wbs  ON po_line(wbs_id);
CREATE INDEX idx_billline_wbs ON bill_line(wbs_id);
CREATE INDEX idx_grnline_poline ON grn_line(po_line_id);
CREATE INDEX idx_audit_object ON audit_log(object_type, object_id);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def L(rupees):
    """Rupees (int or float) -> integer paise."""
    return int(round(float(rupees) * 100))


def reset_and_seed():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = connect()
    con.executescript(SCHEMA)
    _seed(con)
    con.commit()
    con.close()
    return DB_PATH


def _seed(con):
    x = con.execute
    NOW = "2026-08-06T09:00:00"

    # --- Entities, plants, users -------------------------------------------
    x("INSERT INTO entity VALUES (?,?,?,?)", ("ENT-01", "Atha Steel & Power Ltd", "INR", "60021234567"))
    x("INSERT INTO entity VALUES (?,?,?,?)", ("ENT-02", "Atha Infra Projects Ltd", "INR", "60021234999"))
    x("INSERT INTO plant VALUES (?,?,?,?)", ("PL-01", "ENT-01", "Angul Integrated Plant", "Angul, Odisha"))
    x("INSERT INTO plant VALUES (?,?,?,?)", ("PL-02", "ENT-02", "Dhenkanal Unit II", "Dhenkanal, Odisha"))

    users = [
        ("U-REQ", "R. Mohanty", "Requestor"),
        ("U-PM", "S. Panda", "Project Manager"),
        ("U-PLH", "A. Sahoo", "Plant Head"),
        ("U-PROC", "K. Behera", "Procurement"),
        ("U-FIN", "M. Das", "Finance"),
        ("U-PFC", "N. Rout", "Project Finance Controller"),
        ("U-CFO", "V. Agarwal", "CFO"),
        ("U-AUD", "P. Jena", "Internal Auditor"),
        ("U-ADM", "System Administrator", "System Administrator"),
        # Fable 5.1 (2026-09-11): the four users of the Zoho ERP demo
        # organisation, as WBS Administrators by the product owner's decision,
        # so the whole team can test the UAT preview. Passwords are issued by
        # tools/appsail/uat_credentials.py outside the repository; the
        # local-demo `<id>!demo` convention applies to them like every other
        # seeded identity.
        ("U-RAKESH",   "Rakesh Singh",        "Administrator (ERP user)"),
        ("U-PRITHA",   "Pritha Rapguru",      "Administrator (ERP user)"),
        ("U-SURAJ",    "Suraj",               "Administrator (ERP user)"),
        ("U-ABHISHEK", "Abhishek Sonthalia",  "Administrator (ERP user)"),
    ]
    x_many = con.executemany
    x_many("INSERT INTO app_user VALUES (?,?,?)", users)

    # --- Budget heads (10) --------------------------------------------------
    heads = [
        ("BH-CIVIL", "Civil", "Buildings", "CWIP - Civil Works"),
        ("BH-PM", "Plant & Machinery", "Plant & Machinery", "CWIP - Plant & Machinery"),
        ("BH-ELEC", "Electrical", "Electrical Installations", "CWIP - Electrical"),
        ("BH-INST", "Installation", "Plant & Machinery", "CWIP - Installation"),
        ("BH-CONS", "Consultancy & Other", "Intangible / Pre-operative", "CWIP - Consultancy"),
        ("BH-LAND", "Land & Site Development", "Land", "CWIP - Land & Site"),
        ("BH-UTIL", "Utilities", "Plant & Machinery", "CWIP - Utilities"),
        ("BH-IA", "Instrumentation & Automation", "Plant & Machinery", "CWIP - Instrumentation"),
        ("BH-TC", "Testing & Commissioning", "Plant & Machinery", "CWIP - Commissioning"),
        ("BH-CONT", "Contingency", "Unallocated", "CWIP - Contingency"),
    ]
    x_many("INSERT INTO budget_head VALUES (?,?,?,?,1)", heads)

    # --- Projects -----------------------------------------------------------
    projects = [
        ("PRJ-01", "CAPEX-2026-001", "New Production Line", "ENT-01", "PL-01", "Manufacturing",
         "U-PM", "V. Agarwal", "2026-04-01", "2027-03-31", "Greenfield Expansion",
         "Plant & Machinery", "CWIP - Plant & Machinery", "Released", "PRJ-ZOHO-001", NOW, "U-PM"),
        ("PRJ-02", "CAPEX-2026-002", "Warehouse Expansion", "ENT-01", "PL-01", "Logistics",
         "U-PM", "V. Agarwal", "2026-05-01", "2027-01-31", "Brownfield",
         "Buildings", "CWIP - Civil Works", "Released", None, NOW, "U-PM"),
        ("PRJ-03", "CAPEX-2026-003", "Solar Power Project", "ENT-02", "PL-02", "Utilities",
         "U-PM", "V. Agarwal", "2026-06-01", "2027-06-30", "Renewable",
         "Plant & Machinery", "CWIP - Utilities", "Released", None, NOW, "U-PM"),
    ]
    x_many("INSERT INTO project VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", projects)

    # --- WBS: three levels on PRJ-01 ---------------------------------------
    # level 1
    w = []
    def wbs(wid, prj, parent, code, desc, lvl, order, head, allow_proc=1, allow_post=1, abandoned=0,
            asset_cat=None, settle=None, progress=0.0, status="Released"):
        w.append((wid, prj, parent, code, desc, lvl, order, head, "U-PM",
                  "2026-04-01", "2027-03-31", None, None, progress, status, asset_cat, settle,
                  allow_proc, allow_post, abandoned))

    wbs("W-01", "PRJ-01", None, "CAPEX-2026-001.01", "Land and Site Development", 1, 1, "BH-LAND", asset_cat="Land", progress=100.0, status="Technically Completed")
    wbs("W-02", "PRJ-01", None, "CAPEX-2026-001.02", "Civil and Structural Work", 1, 2, "BH-CIVIL", asset_cat="Buildings", progress=62.0)
    wbs("W-03", "PRJ-01", None, "CAPEX-2026-001.03", "Plant and Machinery", 1, 3, "BH-PM", asset_cat="Plant & Machinery", progress=48.0)
    wbs("W-04", "PRJ-01", None, "CAPEX-2026-001.04", "Electrical", 1, 4, "BH-ELEC", asset_cat="Electrical Installations", progress=35.0)
    wbs("W-05", "PRJ-01", None, "CAPEX-2026-001.05", "Installation", 1, 5, "BH-INST", progress=20.0)
    wbs("W-06", "PRJ-01", None, "CAPEX-2026-001.06", "Consultancy and Project Management", 1, 6, "BH-CONS", progress=55.0)
    wbs("W-07", "PRJ-01", None, "CAPEX-2026-001.07", "Instrumentation and Automation", 1, 7, "BH-IA", progress=10.0)
    wbs("W-08", "PRJ-01", None, "CAPEX-2026-001.08", "Pre-operative Expenses", 1, 8, "BH-CONS", progress=30.0)
    # level 2 under Civil
    wbs("W-02-01", "PRJ-01", "W-02", "CAPEX-2026-001.02.01", "Foundation and Piling", 2, 1, "BH-CIVIL", progress=90.0)
    wbs("W-02-02", "PRJ-01", "W-02", "CAPEX-2026-001.02.02", "Structural Steel Erection", 2, 2, "BH-CIVIL", progress=45.0)
    # level 3 under Foundation
    wbs("W-02-01-01", "PRJ-01", "W-02-01", "CAPEX-2026-001.02.01.01", "Site Piling Works", 3, 1, "BH-CIVIL", progress=100.0)
    wbs("W-02-01-02", "PRJ-01", "W-02-01", "CAPEX-2026-001.02.01.02", "Foundation Concreting", 3, 2, "BH-CIVIL", progress=80.0)
    # level 2 under Plant & Machinery
    wbs("W-03-01", "PRJ-01", "W-03", "CAPEX-2026-001.03.01", "Rolling Mill Equipment", 2, 1, "BH-PM", progress=55.0)
    wbs("W-03-02", "PRJ-01", "W-03", "CAPEX-2026-001.03.02", "Material Handling System", 2, 2, "BH-PM", progress=40.0)
    # an abandoned WBS (POC requirement)
    wbs("W-09", "PRJ-01", None, "CAPEX-2026-001.09", "Effluent Treatment - Phase 2 (abandoned)", 1, 9,
        "BH-UTIL", allow_proc=0, allow_post=0, abandoned=1, status="Closed", progress=15.0)

    # other projects, single level
    wbs("W-P2-01", "PRJ-02", None, "CAPEX-2026-002.01", "Warehouse Civil Works", 1, 1, "BH-CIVIL", progress=70.0)
    wbs("W-P2-02", "PRJ-02", None, "CAPEX-2026-002.02", "Racking and Handling", 1, 2, "BH-PM", progress=50.0)
    wbs("W-P3-01", "PRJ-03", None, "CAPEX-2026-003.01", "Solar Modules and Inverters", 1, 1, "BH-PM", progress=45.0)
    wbs("W-P3-02", "PRJ-03", None, "CAPEX-2026-003.02", "Balance of Plant", 1, 2, "BH-ELEC", progress=30.0)
    x_many("INSERT INTO wbs_element VALUES (" + ",".join(["?"] * 20) + ")", w)

    # --- Original budgets ---------------------------------------------------
    # PRJ-01 totals 1,50,00,000 across the five heads used by the client document,
    # distributed onto WBS elements. Figures deliberately reproduce the client's table.
    bl = []
    n = [0]
    def budget(prj, wid, head, kind, rupees, eff="2026-04-01", rev=None, ver=1):
        n[0] += 1
        bl.append((f"BL-{n[0]:04d}", prj, wid, head, kind, L(rupees), ver, rev, eff, NOW, "U-CFO"))

    budget("PRJ-01", "W-02", "BH-CIVIL", "ORIGINAL", 25_00_000)
    budget("PRJ-01", "W-03", "BH-PM", "ORIGINAL", 80_00_000)
    budget("PRJ-01", "W-04", "BH-ELEC", "ORIGINAL", 20_00_000)
    budget("PRJ-01", "W-05", "BH-INST", "ORIGINAL", 15_00_000)
    budget("PRJ-01", "W-06", "BH-CONS", "ORIGINAL", 10_00_000)
    budget("PRJ-02", "W-P2-01", "BH-CIVIL", "ORIGINAL", 50_00_000)
    budget("PRJ-02", "W-P2-02", "BH-PM", "ORIGINAL", 30_00_000)
    budget("PRJ-03", "W-P3-01", "BH-PM", "ORIGINAL", 35_00_000)
    budget("PRJ-03", "W-P3-02", "BH-ELEC", "ORIGINAL", 15_00_000)

    # An approved supplement and an approved transfer on PRJ-01 (POC requirement).
    x("INSERT INTO budget_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      ("REV-001", "PRJ-01", 1, "SUPPLEMENT",
       "Vendor quote for rolling mill exceeded estimate after steel price revision.",
       "U-PM", "2026-06-10T11:00:00", "V. Agarwal", "2026-06-12T16:30:00",
       "CAPEX-COMM/2026/014", "Approved", "2026-06-12"))
    budget("PRJ-01", "W-03", "BH-PM", "SUPPLEMENT", 10_00_000, "2026-06-12", "REV-001", 2)

    x("INSERT INTO budget_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      ("REV-002", "PRJ-01", 2, "TRANSFER",
       "Scope moved from Installation to Electrical after design review.",
       "U-PM", "2026-06-20T10:00:00", "V. Agarwal", "2026-06-22T12:00:00",
       "CAPEX-COMM/2026/019", "Approved", "2026-06-22"))
    budget("PRJ-01", "W-05", "BH-INST", "TRANSFER_OUT", -3_00_000, "2026-06-22", "REV-002", 2)
    budget("PRJ-01", "W-04", "BH-ELEC", "TRANSFER_IN", 3_00_000, "2026-06-22", "REV-002", 2)

    # A pending revision, to show the approval inbox has work in it.
    x("INSERT INTO budget_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      ("REV-003", "PRJ-01", 3, "SUPPLEMENT",
       "Additional instrumentation scope identified during commissioning readiness review.",
       "U-PM", "2026-08-01T09:30:00", None, None, None, "Submitted", None))

    x_many("INSERT INTO budget_line VALUES (?,?,?,?,?,?,?,?,?,?,?)", bl)

    # --- Purchase requests --------------------------------------------------
    prs = []
    def pr(i, prj, wid, head, desc, rupees, status, check=None, approver=None, exc=None):
        prs.append((f"PR-{i:03d}", f"PR-2026-{i:04d}", prj, wid, head, desc, L(rupees),
                    "U-REQ", f"2026-06-{(i % 27) + 1:02d}T10:00:00", status, check, approver,
                    "2026-06-28T15:00:00" if approver else None, exc, 0))

    pr(1, "PRJ-01", "W-02-01-01", "BH-CIVIL", "Piling contractor - 120 piles", 8_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(2, "PRJ-01", "W-02-01-02", "BH-CIVIL", "RCC foundation works", 6_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(3, "PRJ-01", "W-03-01", "BH-PM", "Rolling mill main drive", 30_00_000, "Approved", "WITHIN_BUDGET", "U-CFO")
    pr(4, "PRJ-01", "W-03-01", "BH-PM", "Mill stands and guides", 22_00_000, "Approved", "WITHIN_BUDGET", "U-CFO")
    pr(5, "PRJ-01", "W-03-02", "BH-PM", "Overhead crane 20T", 12_00_000, "Approved", "WITHIN_BUDGET", "U-CFO")
    pr(6, "PRJ-01", "W-04", "BH-ELEC", "HT panels and switchgear", 9_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(7, "PRJ-01", "W-04", "BH-ELEC", "Cabling and trays", 5_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(8, "PRJ-01", "W-05", "BH-INST", "Mechanical erection contractor", 7_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(9, "PRJ-01", "W-06", "BH-CONS", "Detailed engineering consultancy", 6_00_000, "Approved", "WITHIN_BUDGET", "U-FIN")
    pr(10, "PRJ-01", "W-03-02", "BH-PM", "Conveyor system - additional scope", 20_00_000, "Exception Pending", "EXCEEDS_BUDGET", None,
       "Available budget insufficient. Routed for exception approval per PDF section 7.")
    pr(11, "PRJ-02", "W-P2-01", "BH-CIVIL", "Warehouse shed civil", 28_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(12, "PRJ-02", "W-P2-02", "BH-PM", "Pallet racking system", 14_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(13, "PRJ-03", "W-P3-01", "BH-PM", "Solar modules 2 MW", 22_00_000, "Approved", "WITHIN_BUDGET", "U-CFO")
    pr(14, "PRJ-03", "W-P3-02", "BH-ELEC", "Inverters and transformers", 9_00_000, "Approved", "WITHIN_BUDGET", "U-PLH")
    pr(15, "PRJ-01", "W-02-02", "BH-CIVIL", "Structural steel supply", 9_00_000, "Submitted", "WITHIN_BUDGET", None)
    pr(16, "PRJ-01", "W-07", "BH-IA", "PLC and SCADA package", 4_00_000, "Submitted", "WITHIN_BUDGET", None)
    pr(17, "PRJ-01", "W-06", "BH-CONS", "Third-party inspection", 1_50_000, "Draft")
    pr(18, "PRJ-02", "W-P2-01", "BH-CIVIL", "Site drainage", 2_00_000, "Draft")
    pr(19, "PRJ-03", "W-P3-01", "BH-PM", "Module mounting structures", 5_00_000, "Submitted", "WITHIN_BUDGET", None)
    pr(20, "PRJ-01", "W-09", "BH-UTIL", "ETP Phase 2 civil (project abandoned)", 3_00_000, "Rejected", None, "U-PLH")
    x_many("INSERT INTO purchase_request VALUES (" + ",".join(["?"] * 15) + ")", prs)

    # --- Purchase orders + lines -------------------------------------------
    pos, pls = [], []
    ln = [0]
    def po(i, pr_id, prj, vendor, status, amendment=0, currency="INR", rate=1.0, ordered="2026-07-01"):
        pos.append((f"PO-{i:03d}", f"PO-2026-{i:04d}", pr_id, prj, vendor, currency, rate,
                    status, ordered, amendment, 0, f"ZPO-{i:05d}", NOW))
    POL = {}   # po_id -> [line_id, ...]; never hard-code line ids, POs can be multi-line

    def line(po_id, wid, head, desc, rupees, qty=1.0, tax=0, ncr=0, freight=0):
        ln[0] += 1
        lid = f"POL-{ln[0]:04d}"
        pls.append((lid, po_id, ln[0], wid, head, desc, qty,
                    L(rupees) // max(int(qty), 1), L(rupees), L(tax), L(ncr), L(freight),
                    f"ZPOL-{ln[0]:05d}"))
        POL.setdefault(po_id, []).append(lid)
        return lid

    po(1, "PR-001", "PRJ-01", "Odisha Piling Works Pvt Ltd", "Fully Actualised")
    line("PO-001", "W-02-01-01", "BH-CIVIL", "Piling - 120 nos", 8_00_000)

    po(2, "PR-002", "PRJ-01", "Konark Constructions", "Partially Actualised")
    line("PO-002", "W-02-01-02", "BH-CIVIL", "RCC foundation", 6_00_000)

    po(3, "PR-003", "PRJ-01", "Bharat Heavy Engineering Co", "Partially Actualised", amendment=1)
    line("PO-003", "W-03-01", "BH-PM", "Rolling mill main drive", 34_00_000, tax=1_00_000, ncr=40_000)

    po(4, "PR-004", "PRJ-01", "Bharat Heavy Engineering Co", "Fully Committed")
    line("PO-004", "W-03-01", "BH-PM", "Mill stands and guides", 22_00_000)

    po(5, "PR-005", "PRJ-01", "Eastern Cranes Ltd", "Partially Actualised")
    line("PO-005", "W-03-02", "BH-PM", "Overhead crane 20T", 12_00_000, freight=50_000)

    po(6, "PR-006", "PRJ-01", "Utkal Electricals", "Partially Actualised")
    line("PO-006", "W-04", "BH-ELEC", "HT panel", 6_00_000)
    line("PO-006", "W-04", "BH-ELEC", "LT switchgear", 3_00_000)

    po(7, "PR-007", "PRJ-01", "Utkal Electricals", "Cancelled")
    line("PO-007", "W-04", "BH-ELEC", "Cabling and trays", 5_00_000)

    po(8, "PR-008", "PRJ-01", "Sriram Erectors", "Fully Committed")
    line("PO-008", "W-05", "BH-INST", "Mechanical erection", 7_00_000)

    po(9, "PR-009", "PRJ-01", "Delta Engineering Consultants", "Partially Actualised")
    line("PO-009", "W-06", "BH-CONS", "Detailed engineering", 6_00_000)

    po(10, "PR-011", "PRJ-02", "Konark Constructions", "Partially Actualised")
    line("PO-010", "W-P2-01", "BH-CIVIL", "Warehouse shed civil", 28_00_000)

    po(11, "PR-012", "PRJ-02", "Storefit Systems", "Fully Committed")
    line("PO-011", "W-P2-02", "BH-PM", "Pallet racking", 14_00_000)

    po(12, "PR-013", "PRJ-03", "SunPeak Energy GmbH", "Partially Actualised", currency="EUR", rate=92.50)
    line("PO-012", "W-P3-01", "BH-PM", "Solar modules 2 MW", 22_00_000)

    po(13, "PR-014", "PRJ-03", "Utkal Electricals", "Fully Committed")
    line("PO-013", "W-P3-02", "BH-ELEC", "Inverters and transformers", 9_00_000)

    po(14, "PR-001", "PRJ-01", "Odisha Piling Works Pvt Ltd", "Closed")   # closed with residual
    line("PO-014", "W-02-01-01", "BH-CIVIL", "Additional piling - provisional", 2_00_000)

    po(15, "PR-005", "PRJ-01", "Eastern Cranes Ltd", "Fully Committed")
    line("PO-015", "W-03-02", "BH-PM", "Crane rails and stops", 2_00_000)

    x_many("INSERT INTO purchase_order VALUES (" + ",".join(["?"] * 13) + ")", pos)
    x_many("INSERT INTO po_line VALUES (" + ",".join(["?"] * 13) + ")", pls)

    # --- GRNs (incl. partial and one reversal) ------------------------------
    grns, grls = [], []
    g = [0]
    def grn(po_id, date, reversal=0):
        g[0] += 1
        gid = f"GRN-{g[0]:03d}"
        grns.append((gid, f"GRN-2026-{g[0]:04d}", po_id, date, "Approved", reversal, f"ZGR-{g[0]:05d}"))
        return gid
    def grnline(gid, pol, qty, rupees):
        grls.append((f"GRL-{len(grls)+1:04d}", gid, pol, qty, L(rupees)))

    grnline(grn("PO-001", "2026-07-10"), POL["PO-001"][0], 1.0, 8_00_000)    # full receipt
    grnline(grn("PO-002", "2026-07-12"), POL["PO-002"][0], 0.7, 4_20_000)    # partial
    grnline(grn("PO-003", "2026-07-15"), POL["PO-003"][0], 0.5, 17_00_000)   # partial
    grnline(grn("PO-005", "2026-07-18"), POL["PO-005"][0], 1.0, 12_00_000)   # received in full, billed in part
    grnline(grn("PO-006", "2026-07-20"), POL["PO-006"][0], 1.0, 6_00_000)    # first line of a 2-line PO
    grnline(grn("PO-009", "2026-07-22"), POL["PO-009"][0], 0.5, 3_00_000)
    grnline(grn("PO-010", "2026-07-25"), POL["PO-010"][0], 0.6, 16_80_000)
    grnline(grn("PO-012", "2026-07-28"), POL["PO-012"][0], 0.5, 11_00_000)
    gid_rev = grn("PO-002", "2026-07-30", reversal=1)                        # GRN reversal case
    grnline(gid_rev, POL["PO-002"][0], -0.2, -1_20_000)
    x_many("INSERT INTO grn VALUES (?,?,?,?,?,?,?)", grns)
    x_many("INSERT INTO grn_line VALUES (?,?,?,?,?)", grls)

    # --- Bills (partial, full, credit note, reversal) -----------------------
    bills, blines = [], []
    b = [0]
    def bill(po_id, vendor, date, doc="BILL", reversal=0):
        b[0] += 1
        bid = f"BILL-{b[0]:03d}"
        # created_by is the MAKER, and it is seeded deliberately as U-PM rather
        # than left NULL: `bill.void` is a maker-checker permission, and a
        # dataset whose bills have no maker would leave the control with
        # nothing to compare and nothing to demonstrate. U-PM is not the
        # FinanceApprover who voids in the demo script, so the seeded bills
        # stay voidable by an INDEPENDENT approver, which is the behaviour
        # segregation of duties is supposed to leave intact.
        bills.append((bid, f"BILL-2026-{b[0]:04d}", po_id, vendor, date, "Approved", reversal, doc,
                      f"ZBILL-{b[0]:05d}", NOW, "U-PM"))
        return bid
    def billline(bid, pol, wid, head, desc, rupees, qty=1.0, ncr=0, freight=0):
        blines.append((f"BLL-{len(blines)+1:04d}", bid, pol, wid, head, desc, qty, L(rupees),
                       L(ncr), L(freight), f"ZPOL-{pol.split('-')[1]}" if pol else None))

    billline(bill("PO-001", "Odisha Piling Works Pvt Ltd", "2026-07-14"), POL["PO-001"][0], "W-02-01-01", "BH-CIVIL", "Piling - full", 8_00_000)
    billline(bill("PO-002", "Konark Constructions", "2026-07-16"), POL["PO-002"][0], "W-02-01-02", "BH-CIVIL", "RCC foundation - part", 3_00_000, 0.5)
    billline(bill("PO-003", "Bharat Heavy Engineering Co", "2026-07-20"), POL["PO-003"][0], "W-03-01", "BH-PM", "Main drive - 50%", 17_00_000, 0.5, ncr=40_000)
    billline(bill("PO-005", "Eastern Cranes Ltd", "2026-07-24"), POL["PO-005"][0], "W-03-02", "BH-PM", "Crane - part", 7_00_000, 0.6, freight=50_000)
    billline(bill("PO-006", "Utkal Electricals", "2026-07-26"), POL["PO-006"][0], "W-04", "BH-ELEC", "HT panel", 6_00_000)
    billline(bill("PO-009", "Delta Engineering Consultants", "2026-07-28"), POL["PO-009"][0], "W-06", "BH-CONS", "Engineering - 50%", 3_00_000, 0.5)
    billline(bill("PO-010", "Konark Constructions", "2026-07-29"), POL["PO-010"][0], "W-P2-01", "BH-CIVIL", "Warehouse civil - part", 16_80_000, 0.6)
    billline(bill("PO-012", "SunPeak Energy GmbH", "2026-07-30"), POL["PO-012"][0], "W-P3-01", "BH-PM", "Modules - 50%", 11_00_000, 0.5)
    # Credit note reducing an over-billed line: reverses actual CWIP and restores availability.
    billline(bill("PO-005", "Eastern Cranes Ltd", "2026-08-02", doc="CREDIT_NOTE"), POL["PO-005"][0], "W-03-02", "BH-PM", "Rate correction credit", -50_000)
    # A deliberate bill-exceeds-PO exception, so the reconciliation queue has a real case to show.
    billline(bill("PO-008", "Sriram Erectors", "2026-08-03"), POL["PO-008"][0], "W-05", "BH-INST", "Erection - final claim above PO", 7_60_000)
    x_many("INSERT INTO bill VALUES (?,?,?,?,?,?,?,?,?,?,?)", bills)
    x_many("INSERT INTO bill_line VALUES (?,?,?,?,?,?,?,?,?,?,?)", blines)

    # --- Capitalisation case (Land & Site Development, technically complete)
    x("INSERT INTO capitalisation_request VALUES (?,?,?,?,?,?,?,?,?,?)",
      ("CAP-001", "CAP-2026-0001", "PRJ-01", "U-PFC", "2026-08-03T10:00:00",
       "Submitted", None, None, "2026-08-31", 0))

    # --- Zoho connection profile (POC, not connected) -----------------------
    x("INSERT INTO zoho_connection VALUES (" + ",".join(["?"] * 21) + ")",
      ("CONN-01", "Atha Steel - Zoho ERP (Sandbox)", "ENT-01", "Sandbox", "IN",
       "https://accounts.zoho.in", "https://www.zohoapis.in/erp/v3",
       "secretref://kv/zoho/client_id", "secretref://kv/zoho/client_secret",
       "secretref://kv/zoho/refresh_token", "http://localhost:8000/api/zoho/callback",
       "Not Connected", None, None, None, None, None, "Disabled", None, None, NOW))

    # --- Audit seed ---------------------------------------------------------
    x("INSERT INTO audit_log (at,actor,action,object_type,object_id,detail) VALUES (?,?,?,?,?,?)",
      (NOW, "U-ADM", "SEED", "System", "-", "POC dataset loaded: 2 entities, 2 plants, 3 projects, "
       "3-level WBS, 10 budget heads, 20 PRs, 15 POs, partial GRNs, partial bills, PO amendment, "
       "PO cancellation, PO closure with residual, budget supplement, budget transfer, one overrun, "
       "one capitalisation case, one abandoned WBS."))


if __name__ == "__main__":
    print("seeded:", reset_and_seed())
