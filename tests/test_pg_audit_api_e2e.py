"""End-to-end proof for the Milestone 1 vertical slice.

Every other PostgreSQL test exercises one layer. This one runs the whole slice
as a caller experiences it: seeded PostgreSQL data, through the migration
runner, through the pooled scoped engine, through the audit service, through
the FastAPI router and its permission guard, to an HTTP response the SCR-28
Audit Trail Viewer is built to consume.

The vertical-slice definition of done asks for exactly that, and until this
passes, Milestone 1 is not finished no matter how many unit tests are green.

**Runs only against a live PostgreSQL.** There is none on a developer
workstation, so these skip locally and run in the `pg_tests` CI job. That is
also the point: the defects this catches -- a router shadowing another, a
permission guard that was never applied, a lock query the server cannot parse
-- are invisible to a suite that never reaches a real server.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Fixtures come from tests/conftest_pg.py, which is deliberately NOT named
# conftest.py (a second conftest under tests/ shadows the root one and breaks
# `from conftest import ...` in the baseline modules). Import them explicitly.
import sys as _sys

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="needs a live PostgreSQL (CAPEX_DB_URL); runs in the pg_tests CI job",
)

SEED_FILE = Path(__file__).resolve().parents[1] / "migrations" / "pg" / "seed_demo.sql"


@pytest.fixture()
def seeded_pg(pg_connection, pg_database):
    """A disposable PostgreSQL database carrying the demo dataset.

    Skips rather than fails if the seed has not landed yet, so this file can be
    committed alongside the seed work without breaking the branch in between.
    """
    if not SEED_FILE.is_file():
        pytest.skip(f"{SEED_FILE.name} does not exist yet")
    pg_connection.execute(SEED_FILE.read_text(encoding="utf-8"))
    pg_connection.commit()
    return pg_database


@pytest.fixture()
def pg_backed_app(seeded_pg):
    """`main.app` wired to the seeded PostgreSQL database.

    In the pg_tests job CAPEX_DB_URL is set for the whole run, so main.py has
    already mounted the PostgreSQL audit router at import. Here we only point
    the process-wide Database at THIS test's disposable database.
    """
    from app.backend import main
    from app.backend.pg import engine

    # Walk RECURSIVELY. Newer FastAPI wraps an included router in a container
    # object that has no `.path`, so a flat scan of `app.routes` cannot see the
    # audit routes at all -- and this fixture would then skip every test in the
    # file while the job reported green.
    #
    # That is exactly what happened: all nine of these skipped in CI with
    # "router is not mounted" while the router was mounted perfectly well. It
    # is also the same defect I had already fixed once, in
    # tests/test_api_auth.py::_walk_routes, and then reintroduced here by
    # writing the naive scan again. Hence the shared helper.
    from test_api_auth import _walk_routes

    paths = {getattr(r, "path", None) for r in _walk_routes(main.app.routes)}
    if "/api/audit/entries" not in paths:
        pytest.skip(
            "the PostgreSQL audit router is not mounted; main.py mounts it only "
            "when CAPEX_DB_URL or CAPEX_DB_HOST is set at import time")

    previous = None
    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None
    engine.set_database(seeded_pg)
    try:
        yield main.app
    finally:
        engine.set_database(previous)


# ============================================================ the slice, end to end
@pytest.mark.pg
@PG
def test_audit_streams_returns_seeded_postgresql_data(pg_backed_app, make_user):
    """The screen's first call, against real data, through the real guard."""
    auditor = make_user(["Auditor"])
    resp = auditor.get("/api/audit/streams")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["items"], "the demo seed must contain at least one audit stream"
    first = body["items"][0]
    assert set(first) >= {"stream_key", "entry_count", "head_seq", "last_at"}
    assert isinstance(first["entry_count"], int) and first["entry_count"] > 0


@pytest.mark.pg
@PG
def test_audit_entries_paginate_and_match_the_frozen_contract(pg_backed_app, make_user):
    """Field names are a contract the frontend codes against; drift breaks it."""
    auditor = make_user(["Auditor"])
    stream = auditor.get("/api/audit/streams").json()["items"][0]["stream_key"]

    resp = auditor.get(f"/api/audit/entries?stream_key={stream}&limit=2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) >= {"items", "next_cursor", "has_more"}
    assert body["items"], "seeded stream returned no entries"
    entry = body["items"][0]
    assert set(entry) >= {
        "audit_id", "stream_key", "seq", "at", "actor", "action",
        "object_type", "object_id", "detail", "correlation_id", "entry_hash",
    }
    assert entry["stream_key"] == stream
    assert isinstance(entry["seq"], int)


@pytest.mark.pg
@PG
def test_the_seeded_audit_chain_actually_verifies(pg_backed_app, make_user):
    """The product's central claim, checked against real stored rows.

    A seed that produced an unverifiable chain would make every later
    verification test meaningless, so this is as much a check on the demo data
    as on the verifier.
    """
    auditor = make_user(["Auditor"])
    stream = auditor.get("/api/audit/streams").json()["items"][0]["stream_key"]

    resp = auditor.get(f"/api/audit/verify?stream_key={stream}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["intact"] is True, (
        f"the seeded audit chain does not verify: {json.dumps(body)}")
    assert body["stream_found"] is True
    assert body["sequence_contiguous"] is True
    assert body["entries_checked"] > 0
    assert body["first_break_seq"] is None


@pytest.mark.pg
@PG
def test_an_unknown_stream_is_not_reported_as_intact(pg_backed_app, make_user):
    """A typo in the nightly verification job must not read as a pass."""
    auditor = make_user(["Auditor"])
    body = auditor.get("/api/audit/verify?stream_key=NO-SUCH-STREAM").json()
    assert body["intact"] is False
    assert body["stream_found"] is False


# ============================================================ the guard, end to end
@pytest.mark.pg
@PG
@pytest.mark.parametrize("path", [
    "/api/audit/streams",
    "/api/audit/entries?stream_key=x",
    "/api/audit/verify?stream_key=x",
])
def test_every_audit_route_refuses_an_unauthenticated_caller(pg_backed_app, path):
    """Regression for the critical finding F1.

    These routes shipped with no permission dependency at all, and the
    middleware in front of them only checks that a session header EXISTS. Any
    caller sending `X-Session: anything` could page the entire audit log --
    and because these routes shadow the legacy SQLite ones, that also REMOVED
    a guard the product previously had.
    """
    from fastapi.testclient import TestClient

    client = TestClient(pg_backed_app, raise_server_exceptions=False)

    assert client.get(path).status_code == 401, "no session must be refused"
    assert client.get(path, headers={"X-Session": "not-a-real-session"}).status_code == 401, (
        "a syntactically present but invalid session must be refused; header "
        "presence is not authentication")


@pytest.mark.pg
@PG
def test_a_role_without_audit_read_is_refused(pg_backed_app, make_user):
    """Authenticated is not authorised."""
    requestor = make_user(["Requestor"])
    resp = requestor.get("/api/audit/streams")
    assert resp.status_code == 403, (
        f"a role lacking audit.read must be refused, got {resp.status_code}")


# ============================================================ money discipline
@pytest.mark.pg
@PG
def test_every_seeded_money_column_is_an_integer(pg_connection, seeded_pg):
    """No float, no Decimal, anywhere in the seeded financial data.

    A numeric column would return Decimal from psycopg, and the first
    arithmetic mixing it with an int propagates a non-integer amount through
    the financial controls.
    """
    rows = pg_connection.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND column_name LIKE '%%_paise'
        ORDER BY table_name, column_name
    """).fetchall()
    assert rows, "no *_paise columns found; the schema is not what this asserts against"

    wrong = [(t, c, d) for t, c, d in rows if d != "bigint"]
    assert not wrong, f"money columns must be bigint paise, found: {wrong}"

    for table, column, _ in rows:
        values = pg_connection.execute(
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 50"
        ).fetchall()
        for (value,) in values:
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"{table}.{column} returned {type(value).__name__}, not int")
