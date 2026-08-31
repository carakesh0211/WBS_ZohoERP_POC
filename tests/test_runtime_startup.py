"""Runtime start-up and health/readiness regression coverage.

Everything here runs with no live database of any kind -- no SQLite file, no
PostgreSQL server. Unreachability is simulated (an unopened connection pool
pointed at a port nothing listens on) or faked outright (a minimal object
implementing exactly the ``execute``/``commit``/``rollback`` surface the
migration runner calls), which is enough to exercise every path this module
is responsible for: liveness never touching the database, readiness failing
closed without leaking connection detail, and boot refusing to serve -- and
writing nothing -- against a schema it does not recognise.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg.errors

from app.backend.pg import engine as engine_mod
from app.backend.pg import migrate_pg
from app.backend.pg import runtime as pg_runtime
from app.backend.pg.config import DatabaseConfig, MappingSecretProvider


# ============================================================== fixtures
@pytest.fixture(autouse=True)
def _reset_process_database():
    """`app.backend.pg.engine` keeps a process-wide singleton by design
    (`set_database` / `get_database`) so routes can be injected without
    patching module globals. Tests that install one must not leak it into
    the next test."""
    yield
    engine_mod.set_database(None)


@pytest.fixture()
def unreachable_database():
    """A real `Database` object whose pool was never opened and points at a
    port nothing listens on. Every call against it fails fast (no timeout to
    wait out) with a clean, class-only error -- exactly the shape a genuinely
    unreachable production database would produce, without needing one."""
    cfg = DatabaseConfig(host="127.0.0.1", port=1, database="capex",
                          user="capex_app", sslmode="disable", connect_timeout=1)
    provider = MappingSecretProvider({"CAPEX_DB_PASSWORD": "unused-in-this-test"})
    database = engine_mod.Database(cfg, secret_provider=provider,
                                    open_pool=False, min_size=0, max_size=1)
    try:
        yield database
    finally:
        database.close()


def _client():
    from fastapi.testclient import TestClient
    from app.backend import main
    return TestClient(main.app, raise_server_exceptions=False)


# ==================================================================== /healthz
def test_healthz_returns_200_with_database_unavailable():
    """No `set_database()` call has happened -- the ordinary state before
    PostgreSQL start-up wiring runs, and indistinguishable here from a
    database that is down. /healthz must not care either way."""
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_returns_200_even_with_a_configured_but_unreachable_database(
    unreachable_database,
):
    engine_mod.set_database(unreachable_database)
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ===================================================================== /readyz
def test_readyz_returns_503_when_database_unavailable():
    response = _client().get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"


def test_readyz_returns_503_and_leaks_nothing_with_an_unreachable_database(
    unreachable_database,
):
    engine_mod.set_database(unreachable_database)
    response = _client().get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "error_class" in body and isinstance(body["error_class"], str)

    # Never a host, a user, a database name, a DSN fragment or a raw driver
    # message -- only the exception class name. Checked against both the
    # decoded body and the raw bytes, so a leak nested in an unexpected key
    # cannot slip past a narrower check.
    raw = response.text.lower()
    forbidden = ("127.0.0.1", "capex_app", "dbname=", "password=",
                 "user=", "host=", "sslmode=", "unused-in-this-test")
    for needle in forbidden:
        assert needle not in raw, f"{needle!r} leaked into /readyz response: {raw!r}"


def test_readyz_reports_ok_and_schema_version_when_database_is_healthy(monkeypatch):
    """The success path, exercised without a real pool: `Database.readiness`
    is the frozen, already-correct implementation this route delegates to, so
    it is monkeypatched here rather than reimplemented -- this test is about
    the route's status-code and pass-through behaviour, not readiness()
    itself."""
    cfg = DatabaseConfig(host="127.0.0.1", port=1, database="capex", user="capex_app")
    provider = MappingSecretProvider({"CAPEX_DB_PASSWORD": "unused"})
    database = engine_mod.Database(cfg, secret_provider=provider,
                                    open_pool=False, min_size=0, max_size=1)
    monkeypatch.setattr(database, "readiness", lambda: {
        "status": "ok", "check": "readiness",
        "schema_version": "003", "schema_applied_at": "2026-08-31T00:00:00+00:00",
    })
    engine_mod.set_database(database)
    try:
        response = _client().get("/readyz")
    finally:
        database.close()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == "003"


# ============================================================ boot refusal
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeBehindConnection:
    """A PostgreSQL connection whose ``schema_migrations`` ledger exists and
    records nothing -- a database with pending migrations. Raises on any
    statement that is not one of the two SELECTs a read-only status check is
    allowed to issue, so an accidental write shows up as a test failure
    rather than something that has to be inferred from a mock's call log.
    """

    def __init__(self, recorded=()):
        self.recorded = list(recorded)
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        norm = " ".join(statement.split()).upper()
        self.statements.append(norm)
        if norm.startswith("SELECT 1 FROM INFORMATION_SCHEMA.TABLES"):
            return _FakeCursor([(1,)])  # the ledger table exists
        if norm.startswith("SELECT VERSION, CHECKSUM FROM SCHEMA_MIGRATIONS"):
            return _FakeCursor(self.recorded)
        raise AssertionError(
            f"read-only schema check attempted a write or unexpected "
            f"statement: {statement!r}")

    def commit(self):
        raise AssertionError("read-only schema check must never commit")

    def rollback(self):
        pass


def test_boot_refuses_when_the_postgres_schema_is_behind_and_writes_nothing():
    con = _FakeBehindConnection(recorded=[])  # nothing applied; migrations pending

    with pytest.raises(migrate_pg.MigrationError, match="behind"):
        migrate_pg.assert_schema_current(con)

    write_verbs = ("CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER")
    for statement in con.statements:
        assert not statement.startswith(write_verbs), (
            f"boot-time check issued a write: {statement!r}")


def test_boot_refuses_when_the_postgres_database_is_unreachable(unreachable_database):
    with pytest.raises(pg_runtime.RuntimeStartupError) as excinfo:
        pg_runtime.check_schema(unreachable_database)

    # The class-only discipline holds at this layer too.
    message = str(excinfo.value)
    assert "127.0.0.1" not in message
    assert "capex_app" not in message
    assert "unused-in-this-test" not in message


def test_run_py_boot_refuses_against_a_legacy_sqlite_database_and_writes_nothing(
    tmp_path, monkeypatch
):
    """The original DEF-01 reproduction, driven through app.run.main() itself:
    a database built the way the pre-runner code did must not crash the
    process. It must refuse, cleanly, and touch nothing."""
    import sqlite3

    import app.run as run_mod
    from app.backend import db as dbmod

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(dbmod.SCHEMA)
    con.commit()
    con.close()
    raw_before = legacy.read_bytes()

    monkeypatch.setattr(dbmod, "DB_PATH", str(legacy))
    monkeypatch.setenv("CAPEX_DB_PATH", str(legacy))
    monkeypatch.delenv("CAPEX_DB_URL", raising=False)
    monkeypatch.delenv("CAPEX_DB_HOST", raising=False)

    rc = run_mod.main([])

    assert rc != 0
    assert legacy.read_bytes() == raw_before, "boot refusal must not write"


# ======================================================= source-level guarantee
def test_run_py_contains_no_migration_execution_call():
    """AST-based, not a comment grep: a string mentioning 'migrate.upgrade()'
    in a docstring or an error message must not trip this, but an actual call
    to a migration-executing function must. This is the source-level half of
    DEF-01's fix -- the boot path that used to reach the migration runner
    unconditionally must no longer exist as code, not merely behave
    differently at runtime today.
    """
    source = (ROOT / "app" / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app/run.py")

    banned_calls = {"upgrade", "fresh"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name in banned_calls:
            offenders.append(f"line {node.lineno}: {ast.unparse(func)}(...)")

    assert not offenders, (
        "app/run.py contains a migration-execution call, which DEF-01's fix "
        f"requires be removed entirely: {offenders}"
    )


# ================================================================= config
def test_database_config_repr_never_contains_the_password():
    secret = "S3cr3t-P@ssw0rd-99"
    provider = MappingSecretProvider({"CAPEX_DB_PASSWORD": secret})
    config = DatabaseConfig(host="db.internal.example", user="capex_app", database="capex")

    assert secret not in repr(config)
    assert secret not in str(config)

    # The secret is genuinely reachable through the one sanctioned path, so
    # this is proving redaction, not an accidentally-broken config.
    dsn = config.dsn(provider)
    assert secret in dsn
    assert secret not in repr(config)
    assert secret not in str(config)


def test_database_config_from_url_does_not_retain_the_password_on_the_object():
    """`CAPEX_DB_URL` embeds the password inline; `from_env` must lift it into
    the secret provider rather than leave it sitting in a dataclass field a
    repr, log line or pickle could reach."""
    import os

    from app.backend.pg import config as config_mod

    original_provider = config_mod.get_secret_provider()
    original_env = {k: os.environ.get(k) for k in
                     ("CAPEX_DB_URL", "CAPEX_DB_HOST", "CAPEX_DB_SSLMODE")}
    try:
        os.environ["CAPEX_DB_URL"] = "postgres://appuser:hunter2@dbhost:5432/capex"
        os.environ.pop("CAPEX_DB_HOST", None)
        config = config_mod.from_env()

        assert "hunter2" not in repr(config)
        assert "hunter2" not in str(config)
        for field_value in vars(config).values():
            assert field_value != "hunter2"

        assert "hunter2" in config.dsn()
    finally:
        config_mod.set_secret_provider(original_provider)
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
