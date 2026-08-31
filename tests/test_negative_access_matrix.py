"""The negative access matrix -- the point of this whole stream.

Per role/scope archetype, an out-of-scope subject must be refused
EVERYWHERE that subject's data could otherwise be reached, not merely at
whichever single endpoint a reviewer happened to test. This file proves
refusal across three different QUERY SHAPES over the shared, RLS-protected
tables (`project`, and by extension anything joined through it):

  * "read"    -- fetch one row by id (the shape a detail screen uses).
    Out-of-scope -> EMPTY result, never an error. A 403/404 distinction on
    an id lookup is an existence oracle (`repo.py`'s own module docstring);
    the only safe answer is "not found", indistinguishable from "does not
    exist".
  * "export"  -- fetch every row the caller's scope allows, unfiltered
    otherwise (the shape a CSV/report export uses). Out-of-scope rows must
    never appear in the set, however the set is produced.
  * "search"  -- fetch rows matching a filter that WOULD independently
    match an out-of-scope row (the shape a search box uses). A filter
    matching by name/keyword must never let scope be bypassed by knowing
    what to search for.

Each shape is run through BOTH `repo.compile_scope` (the primary control)
and live RLS (the backstop, as the non-superuser `capex_app` role) so a gap
in either layer shows up as a matrix failure, not just a slow leak.

**Why this file cannot exercise `budget.py` / `masters.py` directly.**
Those routers belong to OTHER Wave 2 streams and do not exist in this
worktree (each stream builds in an isolated worktree; the lead merges all
five). Every future endpoint those files add is REQUIRED to be built on
`repo.query()` and `Database.session()` -- both frozen, both already proven
here -- so proving the row-scope invariant at THAT chokepoint is what makes
every endpoint built on top of it inherit the guarantee "for free". This
file additionally exercises the ONE real HTTP API this stream does own,
`app/backend/api/admin_access.py`, mounted into a throwaway `FastAPI()`
instance (never `main.app`, which is lead-owned and does not mount this
router until integration) -- the permission-based "API" leg, complementary
to the row-scope leg above.

**"Out-of-scope write yields forbidden."** At the RLS layer specifically,
an UPDATE targeting an id outside `USING` simply matches zero rows -- the
same "not found" shape as a read, not a raised error; deciding whether that
becomes an HTTP 403 (row exists, caller may not touch it) or 404 (row does
not exist) is an application-layer judgment the not-yet-written business
endpoints make, not something RLS alone can express. What RLS DOES make
into a hard, unambiguous refusal -- proven below -- is `WITH CHECK`: an
INSERT of a new row outside scope, or an UPDATE that would move an
in-scope row OUT of scope, raises a genuine PostgreSQL row-security policy
violation. That is "forbidden" in the sense that matters: the write is
physically impossible, regardless of which status code a future layer
chooses to report it as.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os

import psycopg
import pytest

from app.backend.pg import repo, rls
from app.backend.pg.engine import Scope, Session

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
SEED_DEMO_PATH = PROJECT_ROOT / "migrations" / "pg" / "seed_demo.sql"
SEED_ACCESS_PATH = PROJECT_ROOT / "migrations" / "pg" / "seed_parts" / "004_access.sql"


# =============================================================== router guard
def test_every_admin_access_route_carries_a_permission_dependency():
    """A future route added to `admin_access.router` without a permission
    dependency must fail here immediately -- the exact failure mode
    `app/backend/api/audit.py` documents having shipped once (a route with
    only a database dependency, no auth check).
    """
    from app.backend.api import admin_access

    guarded = {admin_access.require_access_read, admin_access.require_access_grant}
    for route in admin_access.router.routes:
        dependency_calls = {dep.dependency for dep in route.dependencies}
        assert dependency_calls & guarded, (
            f"route {route.path!r} carries no recognised permission dependency")


# =========================================================================== PG
@pytest.fixture()
def seeded_pg(pg_connection, pg_database):
    """A disposable database carrying seed_demo.sql AND
    seed_parts/004_access.sql -- the real 13-role grants this matrix tests
    against, not a synthetic fixture."""
    pg_connection.execute(SEED_DEMO_PATH.read_text(encoding="utf-8"))
    pg_connection.execute(SEED_ACCESS_PATH.read_text(encoding="utf-8"))
    pg_connection.commit()
    return pg_database


# Out-of-scope subject for every archetype below except U-ADM (read_all) and
# U-NOGRANT (which is out-of-scope for BOTH): "Plant Gamma Solar Park",
# PRJ-DM-002, entity ENT-DM2 / plant PLT-DM2-A / location LOC-DM2-A1.
_OUT_OF_SCOPE_ID = "PRJ-DM-002"
_OUT_OF_SCOPE_NAME_TERM = "Solar"
_IN_SCOPE_ID = "PRJ-DM-001"

# (user_id, role archetype) -- see migrations/pg/seed_parts/004_access.sql's
# header for what each demonstrates.
_ARCHETYPES = [
    ("U-PFC", "entity-scoped"),
    ("U-PLH", "plant-scoped"),
    ("U-PM", "project-scoped"),
]


def _assert_matrix_refuses(session: Session, connection: psycopg.Connection,
                            scope: Scope, out_of_scope_id: str, label: str) -> None:
    # --- "read": fetch by id -> not found, never an error.
    read_row = repo.query_one(
        session, "SELECT project_id FROM project WHERE project_id = %(id)s AND {scope}",
        {"id": out_of_scope_id}, scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)
    assert read_row is None, f"[{label}] read-shaped query saw an out-of-scope row"

    # --- "export": fetch everything in scope -> the out-of-scope id is absent.
    export_rows = repo.query(
        session, "SELECT project_id FROM project WHERE {scope}",
        scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)
    export_ids = {row[0] for row in export_rows}
    assert out_of_scope_id not in export_ids, f"[{label}] export-shaped query leaked it"

    # --- "search": a filter that WOULD match it on its own -> still absent.
    search_rows = repo.query(
        session,
        "SELECT project_id FROM project WHERE name ILIKE %(term)s AND {scope}",
        {"term": f"%{_OUT_OF_SCOPE_NAME_TERM}%"},
        scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)
    search_ids = {row[0] for row in search_rows}
    assert out_of_scope_id not in search_ids, f"[{label}] search-shaped query leaked it"

    # --- same three, through LIVE RLS (capex_app), independently of the compiler.
    with rls.scoped_transaction(connection, scope):
        rls_read = connection.execute(
            "SELECT project_id FROM project WHERE project_id = %s",
            (out_of_scope_id,)).fetchall()
        rls_export = connection.execute("SELECT project_id FROM project").fetchall()
        rls_search = connection.execute(
            "SELECT project_id FROM project WHERE name ILIKE %s",
            (f"%{_OUT_OF_SCOPE_NAME_TERM}%",)).fetchall()
    assert rls_read == [], f"[{label}] RLS read-shaped query saw an out-of-scope row"
    assert out_of_scope_id not in {r[0] for r in rls_export}, f"[{label}] RLS export leaked it"
    assert out_of_scope_id not in {r[0] for r in rls_search}, f"[{label}] RLS search leaked it"


@PG
def test_negative_matrix_per_role_archetype_live(seeded_pg, pg_connection):
    """Read / export / search, per real seeded role, through both the
    compiler and RLS. The core of this stream's brief."""
    from app.backend.pg import roles as roles_mod

    with seeded_pg.session(Scope.system()) as session:
        # Sanity: the out-of-scope project must genuinely exist and be
        # findable by an unrestricted viewer -- otherwise its absence for a
        # restricted one proves nothing.
        control = repo.query_one(
            session, "SELECT project_id FROM project WHERE project_id = %(id)s AND {scope}",
            {"id": _OUT_OF_SCOPE_ID}, scope=Scope.system(), columns=repo.PROJECT_SCOPE_COLUMNS)
        assert control is not None, "fixture sanity check failed: PRJ-DM-002 not found at all"

        for user_id, label in _ARCHETYPES:
            grant = roles_mod.resolve_grant(session, user_id)
            assert grant is not None, user_id
            _assert_matrix_refuses(session, pg_connection, grant.scope, _OUT_OF_SCOPE_ID, label)

        # U-NOGRANT: out of scope for BOTH seeded projects -- the empty-set
        # case, demonstrated against real business data, not a synthetic
        # frozenset() in isolation.
        nogrant = roles_mod.resolve_grant(session, "U-NOGRANT")
        assert nogrant is not None
        _assert_matrix_refuses(session, pg_connection, nogrant.scope, _OUT_OF_SCOPE_ID, "U-NOGRANT")
        _assert_matrix_refuses(session, pg_connection, nogrant.scope, _IN_SCOPE_ID, "U-NOGRANT (also PRJ-DM-001)")

        # Positive control: U-ADM (read_all) sees BOTH -- proves the matrix
        # above is measuring a real refusal, not a fixture that never had
        # PRJ-DM-002 visible to anyone.
        adm = roles_mod.resolve_grant(session, "U-ADM")
        assert adm is not None
        adm_rows = repo.query(session, "SELECT project_id FROM project WHERE {scope}",
                               scope=adm.scope, columns=repo.PROJECT_SCOPE_COLUMNS)
        adm_ids = {row[0] for row in adm_rows}
        assert {_IN_SCOPE_ID, _OUT_OF_SCOPE_ID} <= adm_ids


@PG
def test_negative_matrix_out_of_scope_insert_is_refused_live(seeded_pg, pg_connection):
    """WITH CHECK: a plant-scoped caller (U-PLH, restricted to PLT-DM1-A)
    cannot INSERT a new project under a DIFFERENT plant -- the write-side
    "forbidden", made into a real, unambiguous database error.
    """
    from app.backend.pg import roles as roles_mod

    with seeded_pg.session(Scope.system()) as session:
        grant = roles_mod.resolve_grant(session, "U-PLH")
    assert grant is not None

    with rls.scoped_transaction(pg_connection, grant.scope):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg_connection.execute(
                "INSERT INTO project (project_id, entity_id, plant_id, location_id, "
                "capex_code, name, created_by, updated_by) VALUES "
                "('PRJ-MATRIX-DENY', 'ENT-DM2', 'PLT-DM2-A', 'LOC-DM2-A1', "
                "'CAPEX-MATRIX-DENY', 'Out Of Scope Project', 'U-PLH', 'U-PLH')")
    pg_connection.rollback()  # the failed INSERT aborts the transaction


@PG
def test_negative_matrix_moving_a_row_out_of_scope_is_refused_live(seeded_pg, pg_connection):
    """WITH CHECK again, the more realistic shape: U-PLH CAN see PRJ-DM-001
    (it is in PLT-DM1-A), but cannot UPDATE it to move it to a plant outside
    their scope -- the new row must satisfy the same predicate as any other
    write.
    """
    from app.backend.pg import roles as roles_mod

    with seeded_pg.session(Scope.system()) as session:
        grant = roles_mod.resolve_grant(session, "U-PLH")
    assert grant is not None

    with rls.scoped_transaction(pg_connection, grant.scope):
        visible = pg_connection.execute(
            "SELECT project_id FROM project WHERE project_id = %s",
            (_IN_SCOPE_ID,)).fetchall()
        assert visible == [(_IN_SCOPE_ID,)], "fixture sanity: PRJ-DM-001 must start visible"

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg_connection.execute(
                "UPDATE project SET plant_id = 'PLT-DM2-A' WHERE project_id = %s",
                (_IN_SCOPE_ID,))
    pg_connection.rollback()


# ==================================================================== HTTP API
@pytest.fixture()
def local_admin_client():
    """`admin_access.router` mounted into a THROWAWAY FastAPI app -- never
    `main.app`, which does not mount this router yet (mounting is the
    lead's job at integration, per the Wave 2 contract). This is the only
    way to exercise the router's real HTTP behaviour from an isolated
    worktree.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.backend.api import admin_access

    app = FastAPI()
    app.include_router(admin_access.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def pg_wired(seeded_pg):
    """Installs `seeded_pg` as the process-wide database singleton
    `admin_access.py`'s `get_database()` dependency reads, for the duration
    of one test -- mirrors `tests/test_pg_audit_api_e2e.py::pg_backed_app`.
    """
    from app.backend.pg import engine

    previous = None
    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None
    engine.set_database(seeded_pg)
    try:
        yield seeded_pg
    finally:
        engine.set_database(previous)


def _with_session(role_client, **kw):
    """Request kwargs carrying `role_client`'s `X-Session` header, for use
    against `local_admin_client` (a DIFFERENT TestClient instance than the
    one `role_client` was minted against -- the session lives in the shared
    SQLite dev-identity database, not in the client object)."""
    headers = dict(role_client.headers)
    headers.update(kw.pop("headers", {}))
    return {"headers": headers, **kw}


@PG
def test_api_read_grants_requires_audit_read_permission_live(
        pg_wired, local_admin_client, make_user):
    unauthorised = make_user(["Requestor"])   # no audit.read
    auditor = make_user(["Auditor"])          # audit.read, not admin.reset

    denied = local_admin_client.get("/api/admin/users/U-PM/grants",
                                     **_with_session(unauthorised))
    assert denied.status_code == 403

    allowed = local_admin_client.get("/api/admin/users/U-PM/grants",
                                      **_with_session(auditor))
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["user_id"] == "U-PM"
    assert "Project Manager" in body["roles"]
    assert body["scopes"]["project_ids"] == ["PRJ-DM-001"]


@PG
def test_api_read_grants_for_unknown_user_is_not_found_live(
        pg_wired, local_admin_client, make_user):
    auditor = make_user(["Auditor"])
    resp = local_admin_client.get("/api/admin/users/U-DOES-NOT-EXIST/grants",
                                   **_with_session(auditor))
    assert resp.status_code == 404
    assert resp.json()["code"] == "USER_NOT_FOUND"


@PG
def test_api_write_grants_requires_admin_reset_permission_live(
        pg_wired, local_admin_client, make_user):
    auditor = make_user(["Auditor"])          # audit.read, NOT admin.reset
    administrator = make_user(["Administrator"])

    body = {"roles": ["Requestor"], "scopes": {"read_all": False}}

    denied = local_admin_client.put("/api/admin/users/U-REQ/grants", json=body,
                                     **_with_session(auditor))
    assert denied.status_code == 403

    allowed = local_admin_client.put("/api/admin/users/U-REQ/grants", json=body,
                                      **_with_session(administrator))
    assert allowed.status_code == 200
    assert allowed.json()["roles"] == ["Requestor"]


@PG
def test_api_write_grants_denies_service_maker_checker_role_live(
        pg_wired, local_admin_client, make_user):
    administrator = make_user(["Administrator"])
    body = {"roles": ["Finance"], "scopes": {"read_all": False}}  # Finance IS maker-checker

    resp = local_admin_client.put("/api/admin/users/SVC-ZOHO/grants", json=body,
                                   **_with_session(administrator))
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERVICE_MAKER_CHECKER_DENIED"


@PG
def test_api_write_grants_is_audited_live(pg_wired, local_admin_client, make_user):
    """Granting roles/scope is itself a privileged, audited operation --
    docs/WAVE2_CONTRACTS.md / this stream's brief. One successful PUT must
    append one audit.append entry recording who granted what to whom."""
    from app.backend.pg import roles as roles_mod
    from app.backend.pg.audit import verify_chain

    administrator = make_user(["Administrator"])
    body = {"roles": ["Requestor"], "scopes": {"entity_ids": ["ENT-DM1"], "read_all": False}}

    resp = local_admin_client.put("/api/admin/users/U-REQ/grants", json=body,
                                   **_with_session(administrator))
    assert resp.status_code == 200

    with pg_wired.session(Scope.system()) as session:
        rows = session.fetchall(
            "SELECT actor, action, object_type, object_id, detail FROM audit_log "
            "WHERE object_type = 'USER_ACCESS' AND object_id = 'U-REQ' "
            "ORDER BY seq DESC LIMIT 1")
        chain = verify_chain(session, "USER_ACCESS:U-REQ")

    assert rows, "no audit entry was written for the grant change"
    actor, action, object_type, object_id, detail = rows[0]
    assert action == "GRANT_UPDATE"
    assert object_type == "USER_ACCESS" and object_id == "U-REQ"
    assert "Requestor" in detail
    assert chain["intact"] is True
