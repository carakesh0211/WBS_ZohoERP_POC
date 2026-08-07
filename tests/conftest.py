"""Shared test infrastructure for the CAPEX & WBS Control Hub regression suite.

Remediates AUD-H-010 ("No automated product test suite or CI evidence").

Every test runs against a DISPOSABLE database:

    * one template database is built once per session with the product's own
      migration runner (``python -m app.backend.migrate --db <tmp> --fresh --seed``)
      followed by ``auth.provision_dev_identities``;
    * each test then gets a byte copy of that template in its own ``tmp_path``.

Nothing in this suite may touch ``app/data/capex.db`` or ``app/data/capex_v2.db``.
``_assert_disposable`` enforces that, and ``no_production_database`` re-checks it
around every single test.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:                    # allow `import app.backend...`
    sys.path.insert(0, str(PROJECT_ROOT))

PRODUCTION_DATA_DIR = (PROJECT_ROOT / "app" / "data").resolve()

# auth.provision_dev_identities seeds every development account with this suffix.
PASSWORD_SUFFIX = "!demo"

# Seeded identities, by the role they exist to exercise.
USER_REQUESTOR = "U-REQ"          # Requestor
USER_PROCUREMENT = "U-PLH"        # ProcurementApprover
USER_FINANCE = "U-FIN"            # FinanceApprover
USER_CAPITALISATION = "U-CFO"     # FinanceApprover + CapitalisationApprover
USER_AUDITOR = "U-AUD"            # Auditor
USER_ADMIN = "U-ADM"              # Administrator
USER_CONTROLLER = "U-PFC"         # BudgetController + FinanceApprover (maker-checker probe)


# ======================================================================== guards
def _assert_disposable(path) -> Path:
    """Refuse to run against anything that could be a real database."""
    p = Path(path).resolve()
    if p.parent == PRODUCTION_DATA_DIR:
        raise RuntimeError(
            f"Refusing to use {p}: tests must never touch the application data "
            f"directory {PRODUCTION_DATA_DIR}.")
    return p


@pytest.fixture(scope="session", autouse=True)
def _isolate_database_module(tmp_path_factory):
    """Point the product's database module away from ``app/data`` for the whole run.

    Any test that forgets to request ``capex_db`` therefore fails against a
    non-existent scratch path instead of silently opening the real database.
    """
    from app.backend import db
    sentinel = tmp_path_factory.mktemp("capex-unset") / "DO-NOT-USE.db"
    original, original_env = db.DB_PATH, os.environ.get("CAPEX_DB_PATH")
    db.DB_PATH = str(sentinel)
    os.environ["CAPEX_DB_PATH"] = str(sentinel)
    try:
        yield sentinel
    finally:
        db.DB_PATH = original
        if original_env is None:
            os.environ.pop("CAPEX_DB_PATH", None)
        else:
            os.environ["CAPEX_DB_PATH"] = original_env


@pytest.fixture(autouse=True)
def no_production_database(_isolate_database_module):
    """Fail loudly if any test leaves the module pointing at the real database."""
    yield
    from app.backend import db
    _assert_disposable(db.DB_PATH)


# ==================================================================== the database
@pytest.fixture(scope="session")
def _template_db(tmp_path_factory) -> Path:
    """Build one seeded, fully migrated database for the whole session."""
    target = _assert_disposable(tmp_path_factory.mktemp("capex-template") / "capex.db")

    env = dict(os.environ)
    env["CAPEX_DB_PATH"] = str(target)
    env.pop("CAPEX_PROFILE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "app.backend.migrate",
         "--db", str(target), "--fresh", "--seed"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"migrate --fresh --seed failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert target.exists(), "the migration runner produced no database"

    from app.backend import auth
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    try:
        provisioned = auth.provision_dev_identities(con)
    finally:
        con.close()
    assert provisioned == len(auth.DEV_USERS), "development identities were not provisioned"
    return target


@pytest.fixture()
def capex_db(_template_db, tmp_path, monkeypatch) -> Path:
    """A private copy of the template database for a single test."""
    target = _assert_disposable(tmp_path / "capex.db")
    shutil.copy2(_template_db, target)

    monkeypatch.setenv("CAPEX_DB_PATH", str(target))
    monkeypatch.delenv("CAPEX_PROFILE", raising=False)

    from app.backend import db
    monkeypatch.setattr(db, "DB_PATH", str(target))
    return target


@pytest.fixture()
def connect(capex_db):
    """Factory returning a fresh connection to the disposable database."""
    from app.backend import db

    def _connect() -> sqlite3.Connection:
        return db.connect()

    return _connect


@pytest.fixture()
def raw_con(connect):
    """A single connection for direct schema/constraint assertions."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


@pytest.fixture()
def ledger(connect):
    """Compute the control ledger for a project against the disposable database."""
    from app.backend import domain

    def _ledger(project_id: str = "PRJ-01", **kw):
        con = connect()
        try:
            return domain.compute_ledger(con, project_id, **kw)
        finally:
            con.close()

    return _ledger


@pytest.fixture()
def cell(ledger):
    """The rolled-up (WBS element, budget head) control cell."""
    def _cell(wbs_id: str, head_id: str, project_id: str = "PRJ-01"):
        node = ledger(project_id)["by_id"][wbs_id]
        from app.backend import domain
        return node["head_totals"].get(head_id, domain._derive(domain._blank()))

    return _cell


@pytest.fixture()
def reconciliation(connect):
    from app.backend import domain

    def _recon(project_id: str | None = None):
        con = connect()
        try:
            return domain.reconciliation(con, project_id)
        finally:
            con.close()

    return _recon


# ===================================================================== API clients
class RoleClient:
    """An authenticated caller. Identity comes from the server-side session only."""

    def __init__(self, client, user_id: str, session_id: str):
        self._client = client
        self.user_id = user_id
        self.session_id = session_id
        self.headers = {"X-Session": session_id}

    def request(self, method: str, url: str, *, headers=None, **kw):
        merged = dict(self.headers)
        merged.update(headers or {})
        return self._client.request(method, url, headers=merged, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def __repr__(self):
        return f"<RoleClient {self.user_id}>"


@pytest.fixture()
def client(capex_db):
    from fastapi.testclient import TestClient
    from app.backend import main

    # raise_server_exceptions=False so an unhandled product exception is observed
    # as the 500 a real caller would receive, instead of aborting the test.
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def login(client):
    """Log a seeded identity in and return an authenticated RoleClient."""
    def _login(user_id: str, password: str | None = None) -> RoleClient:
        resp = client.post("/api/auth/login",
                           json={"user_id": user_id,
                                 "password": password if password is not None
                                 else user_id + PASSWORD_SUFFIX})
        assert resp.status_code == 200, f"login for {user_id} failed: {resp.text}"
        return RoleClient(client, user_id, resp.json()["session_id"])

    return _login


@pytest.fixture()
def make_user(capex_db, client, login):
    """Create an identity holding exactly the roles given, and log it in.

    Used to prove that a role which lacks a permission is refused, including role
    combinations the demo dataset does not happen to contain.
    """
    counter = {"n": 0}

    def _make(roles, user_id: str | None = None) -> RoleClient:
        from app.backend import auth, db
        counter["n"] += 1
        uid = user_id or f"U-TEST-{counter['n']:02d}"
        con = db.connect()
        try:
            con.execute("INSERT OR REPLACE INTO app_user (user_id, name, role) VALUES (?,?,?)",
                        (uid, f"Test {uid}", "/".join(roles) or "none"))
            salt, hashed = auth.hash_password(uid + PASSWORD_SUFFIX)
            con.execute("""INSERT OR REPLACE INTO app_credential
                           (user_id, password_salt, password_hash, disabled, created_at)
                           VALUES (?,?,?,0,'2026-08-06T00:00:00')""", (uid, salt, hashed))
            con.execute("DELETE FROM user_role WHERE user_id=?", (uid,))
            for role in roles:
                con.execute("INSERT INTO user_role (user_id, role) VALUES (?,?)", (uid, role))
            con.commit()
        finally:
            con.close()
        return login(uid)

    return _make


@pytest.fixture()
def requestor(login):
    return login(USER_REQUESTOR)


@pytest.fixture()
def procurement(login):
    return login(USER_PROCUREMENT)


@pytest.fixture()
def finance(login):
    return login(USER_FINANCE)


@pytest.fixture()
def capitalisation(login):
    return login(USER_CAPITALISATION)


@pytest.fixture()
def auditor(login):
    return login(USER_AUDITOR)


@pytest.fixture()
def admin(login):
    return login(USER_ADMIN)


@pytest.fixture()
def controller(login):
    """U-PFC holds BudgetController AND FinanceApprover: it can raise a revision or
    an over-budget PR and then attempt to approve its own work."""
    return login(USER_CONTROLLER)


# ======================================================================= helpers
def detail(response) -> dict:
    """The controlled error envelope: {"detail": {"code": ..., "message": ...}}."""
    body = response.json()
    assert isinstance(body, dict) and "detail" in body, f"unexpected error shape: {body!r}"
    return body["detail"]


def code_of(response) -> str:
    return detail(response)["code"]


def rupees(paise: int) -> str:
    """Integer paise -> the decimal-string rupee amount the API expects."""
    from app.backend.money import to_rupees
    return str(to_rupees(paise))


def insert_bill(con, *, bill_id, bill_number, po_id, po_line_id, wbs_id, budget_head_id,
                amount_paise, accounting_status="Approved", is_reversal=0,
                external_source=None, external_id=None, vendor="Test Vendor"):
    """Insert a bill and one line straight into the database.

    There is no bill-creation route in the product, so bill-lifecycle invariants
    have to be set up at the data layer. The migration-002 triggers still apply.
    """
    con.execute(
        """INSERT INTO bill (bill_id, bill_number, po_id, vendor_name, bill_date, status,
                             is_reversal, doc_type, created_at, accounting_status,
                             external_source, external_id)
           VALUES (?,?,?,?,'2026-08-05',?,?,'BILL','2026-08-05T00:00:00',?,?,?)""",
        (bill_id, bill_number, po_id, vendor,
         "Void" if accounting_status == "Void" else "Approved",
         is_reversal, accounting_status, external_source, external_id))
    con.execute(
        """INSERT INTO bill_line (bill_line_id, bill_id, po_line_id, wbs_id, budget_head_id,
                                  description, quantity, amount_paise,
                                  non_creditable_tax_paise, freight_paise)
           VALUES (?,?,?,?,?,'test line',1,?,0,0)""",
        (bill_id + "-L1", bill_id, po_line_id, wbs_id, budget_head_id, amount_paise))


def insert_grn(con, *, grn_id, grn_number, po_id, po_line_id, amount_paise, is_reversal=0):
    con.execute("""INSERT INTO grn (grn_id, grn_number, po_id, received_at, status, is_reversal)
                   VALUES (?,?,?,'2026-08-05','Approved',?)""",
                (grn_id, grn_number, po_id, is_reversal))
    con.execute("""INSERT INTO grn_line (grn_line_id, grn_id, po_line_id, quantity, amount_paise)
                   VALUES (?,?,?,1,?)""",
                (grn_id + "-L1", grn_id, po_line_id, amount_paise))


def build_deep_wbs(con, depth: int, *, project_id="PRJ-DEEP", budget_paise=100_000_000):
    """A single chain `depth` elements long, budget held at the root (AUD-M-005)."""
    con.execute("""INSERT INTO project (project_id, capex_code, name, entity_id, plant_id,
                        department, owner_user_id, sponsor, start_date, planned_end_date,
                        project_type, asset_category, cwip_gl, status, zoho_project_id,
                        created_at, created_by)
                   VALUES (?,?,?,'ENT-01','PL-01','Test','U-PM','V. Agarwal','2026-04-01',
                           '2027-03-31','Test','Plant & Machinery','CWIP','Released',NULL,
                           '2026-08-06T00:00:00','U-PM')""",
                (project_id, f"CAPEX-DEEP-{depth}", f"Deep hierarchy ({depth} levels)"))
    parent, rows = None, []
    for i in range(depth):
        wid = f"WD-{i:06d}"
        rows.append((wid, project_id, parent, f"{project_id}.{i:06d}", f"level {i + 1}",
                     i + 1, 1, "BH-PM", "U-PM", "2026-04-01", "2027-03-31", None, None,
                     0.0, "Released", None, None, 1, 1, 0))
        parent = wid
    con.executemany("INSERT INTO wbs_element VALUES (" + ",".join(["?"] * 20) + ")", rows)
    con.execute("""INSERT INTO budget_line (budget_line_id, project_id, wbs_id, budget_head_id,
                        kind, amount_paise, status, version_no, revision_id, effective_date,
                        approved_by, approved_at, approval_ref, created_at, created_by)
                   VALUES ('BL-DEEP',?, 'WD-000000','BH-PM','ORIGINAL',?, 'Approved',1,NULL,
                           '2026-04-01','U-CFO','2026-04-01T00:00:00','ref',
                           '2026-08-06T00:00:00','U-PM')""",
                (project_id, budget_paise))
    con.commit()
    return project_id, f"WD-{depth - 1:06d}"
