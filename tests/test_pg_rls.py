"""Tests for row-level security -- the BACKSTOP behind `repo.compile_scope`.

Per this stream's brief and `migrations/pg/004_identity_scope.sql`'s header:
RLS is a second, independent enforcement of the same rule `repo.compile_scope`
compiles into SQL, not a replacement for it. A backstop that has never been
proven to agree with the control it backs up is not a backstop -- so most of
this file is dedicated to proving agreement, not just exercising RLS in
isolation:

  * `app.backend.pg.rls.permits` -- a pure-Python, database-free mirror of
    the SQL predicate -- is checked against a matrix of cases first.
  * Live tests then check `repo.compile_scope`'s result set, a live
    RLS-filtered query (as the non-superuser `capex_app` role), and
    `rls.permits`'s computed expectation ALL agree, for the same scope, on
    the same rows, table by table.

Layer split matches `tests/test_pg_locking.py`: pure logic and source-text
checks run unconditionally; anything touching a live database is `@PG`
(`skipif` on `CAPEX_DB_URL`, matching the sibling `pg` marker's semantics --
see that file's module docstring for why `skipif` rather than
`pytest.mark.pg` is used here).
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
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
import re

import pytest

from app.backend.pg import repo, rls
from app.backend.pg.engine import Scope, Session

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
PG_DIR = PROJECT_ROOT / "app" / "backend" / "pg"
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "pg" / "004_identity_scope.sql"


# ======================================================================= source
def test_no_bare_set_statement_in_app_backend_pg():
    """Every ``SET`` issued anywhere in ``app/backend/pg/`` -- including
    THIS stream's own `roles.py` and `rls.py` -- must be ``SET LOCAL``. A
    plain ``SET`` survives the connection's return to the pool and leaks
    scope (or, for `rls.assume_scoped_role`'s ``SET ROLE``, PRIVILEGE) into
    the next request. `tests/test_pg_scope_leakage.py` (lead-owned) already
    covers this directory; this stream's own suite carries the same guard so
    it is self-sufficient, per this stream's brief.
    """
    bare_set = re.compile(r"""(['"])SET(?!\s+LOCAL\b)\s""")
    offenders: list[str] = []
    for path in sorted(PG_DIR.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if bare_set.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "non-LOCAL SET found in app/backend/pg/:\n" + "\n".join(offenders))


def test_migration_registers_rls_and_a_policy_for_every_table_in_the_registry():
    """`rls.RLS_TABLE_COLUMNS` and the migration's actual `ALTER TABLE ...
    ENABLE/FORCE ROW LEVEL SECURITY` + `CREATE POLICY` statements must name
    the same tables -- a table added to one without the other is either an
    inert registry entry or an unregistered, untested policy.
    """
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    for table in rls.RLS_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in text, table
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in text, table
        assert re.search(rf"CREATE POLICY \w+ ON {table}\b", text), table


def test_rls_table_columns_matches_repo_project_scope_columns():
    """`rls.RLS_TABLE_COLUMNS["project"]` must be the SAME mapping
    `repo.PROJECT_SCOPE_COLUMNS` already is (dict key naming differs from
    `Scope`'s field names on purpose -- both use the short dimension names
    `compile_scope` expects). This is the whole basis for expecting the
    `project` table's RLS policy and `repo.compile_scope` to agree."""
    assert rls.RLS_TABLE_COLUMNS["project"] == repo.PROJECT_SCOPE_COLUMNS


def test_rls_table_columns_matches_repo_wbs_element_scope_columns():
    assert rls.RLS_TABLE_COLUMNS["wbs_element"] == repo.WBS_ELEMENT_SCOPE_COLUMNS


# =============================================================== permits() unit
def test_permits_none_dimension_is_unrestricted():
    scope = Scope(user_id="u", entity_ids=None)
    assert rls.permits(scope, entity_id="ENT-ANY") is True
    assert rls.permits(scope, entity_id=None) is True  # waived column too


def test_permits_empty_frozenset_permits_nothing():
    scope = Scope(user_id="u", entity_ids=frozenset())
    assert rls.permits(scope, entity_id="ENT-1") is False
    # ...but a WAIVED column (no value for this row shape) still passes --
    # the empty set restricts what the dimension itself can match, it does
    # not retroactively un-waive a column the table never had.
    assert rls.permits(scope, entity_id=None) is True


def test_permits_restricted_set_matches_membership():
    scope = Scope(user_id="u", entity_ids=frozenset({"ENT-1", "ENT-2"}))
    assert rls.permits(scope, entity_id="ENT-1") is True
    assert rls.permits(scope, entity_id="ENT-3") is False


def test_permits_read_all_overrides_every_restriction():
    scope = Scope(user_id="u", read_all=True, entity_ids=frozenset(),
                   plant_ids=frozenset({"NOPE"}))
    assert rls.permits(scope, entity_id="ANYTHING", plant_id="ANYTHING",
                        location_id="ANYTHING", project_id="ANYTHING") is True


def test_permits_requires_every_dimension_to_agree():
    scope = Scope(user_id="u", entity_ids=frozenset({"ENT-1"}),
                   plant_ids=frozenset({"PLT-1"}))
    # entity matches, plant does not -> AND fails overall
    assert rls.permits(scope, entity_id="ENT-1", plant_id="PLT-OTHER") is False
    assert rls.permits(scope, entity_id="ENT-1", plant_id="PLT-1") is True


# =========================================================================== PG
_ORG_ID = "ORG-RLS-T"
_ENT_A, _ENT_B = "ENT-RLS-A", "ENT-RLS-B"
_PLT_A, _PLT_B = "PLT-RLS-A", "PLT-RLS-B"
_LOC_A, _LOC_B = "LOC-RLS-A", "LOC-RLS-B"
_PRJ_A, _PRJ_B = "PRJ-RLS-A", "PRJ-RLS-B"
_WBS_A, _WBS_B = "WBS-RLS-A", "WBS-RLS-B"
_BH_A, _BH_B = "BH-RLS-A", "BH-RLS-B"


def _seed_rls_fixture(session: Session) -> None:
    """Two fully independent entity/plant/location/project/wbs/cell chains
    (A and B), so a scope restricted to "A" can be proven never to see
    anything from "B" across every RLS-protected table."""
    session.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, 'TEST', 'TEST')", (_ORG_ID, "RLST", "RLS Test Org"))
    for ent in (_ENT_A, _ENT_B):
        session.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (ent, _ORG_ID, ent, ent))
    session.execute(
        "INSERT INTO plant (plant_id, entity_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (_PLT_A, _ENT_A, _PLT_A, _PLT_A))
    session.execute(
        "INSERT INTO plant (plant_id, entity_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (_PLT_B, _ENT_B, _PLT_B, _PLT_B))
    session.execute(
        "INSERT INTO location (location_id, entity_id, plant_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')", (_LOC_A, _ENT_A, _PLT_A, _LOC_A, _LOC_A))
    session.execute(
        "INSERT INTO location (location_id, entity_id, plant_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')", (_LOC_B, _ENT_B, _PLT_B, _LOC_B, _LOC_B))
    session.execute(
        "INSERT INTO project (project_id, entity_id, plant_id, location_id, capex_code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, 'TEST', 'TEST')",
        (_PRJ_A, _ENT_A, _PLT_A, _LOC_A, _PRJ_A, _PRJ_A))
    session.execute(
        "INSERT INTO project (project_id, entity_id, plant_id, location_id, capex_code, name, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, 'TEST', 'TEST')",
        (_PRJ_B, _ENT_B, _PLT_B, _LOC_B, _PRJ_B, _PRJ_B))
    session.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
        (_WBS_A, _PRJ_A, "WBS.A", "WBS A", "wbs_rls_a"))
    session.execute(
        "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
        (_WBS_B, _PRJ_B, "WBS.B", "WBS B", "wbs_rls_b"))
    session.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (_BH_A, _ENT_A, _BH_A, _BH_A))
    session.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (_BH_B, _ENT_B, _BH_B, _BH_B))
    session.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
        "VALUES (%s, %s, 100, 'TEST')", (_WBS_A, _BH_A))
    session.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
        "VALUES (%s, %s, 100, 'TEST')", (_WBS_B, _BH_B))
    session.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
        "VALUES (%s, %s, 'TEST')", (_WBS_A, _BH_A))
    session.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
        "VALUES (%s, %s, 'TEST')", (_WBS_B, _BH_B))


def _rls_rows(connection, scope: Scope, sql_text: str) -> list:
    """Rows visible to `scope`, through the LIVE RLS-enforced `capex_app`
    role -- rolled back afterwards so the role/scope session state never
    escapes this call."""
    with rls.scoped_transaction(connection, scope):
        rows = connection.execute(sql_text).fetchall()
    return rows


@PG
def test_rls_enabled_and_forced_for_every_registered_table_live(pg_connection):
    status = rls.fetch_rls_status(pg_connection)
    pg_connection.rollback()
    for table, state in status.items():
        assert state["enabled"], f"{table}: RLS not enabled"
        assert state["forced"], f"{table}: RLS not forced"


@PG
def test_capex_app_role_exists_and_is_not_login_live(pg_connection):
    row = pg_connection.execute(
        "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'capex_app'").fetchone()
    pg_connection.rollback()
    assert row is not None, "capex_app role was not created by 004_identity_scope.sql"
    can_login, is_super = row
    assert can_login is False, "capex_app is NOLOGIN by design -- see the migration's header note"
    assert is_super is False


@PG
def test_project_rls_matches_compiler_and_pure_python_live(pg_database, pg_connection):
    with pg_database.session(Scope.system()) as session:
        _seed_rls_fixture(session)

    scopes = {
        "entity-scoped": Scope(user_id="u", entity_ids=frozenset({_ENT_A})),
        "plant-scoped": Scope(user_id="u", plant_ids=frozenset({_PLT_A})),
        "location-scoped": Scope(user_id="u", location_ids=frozenset({_LOC_A})),
        "project-scoped": Scope(user_id="u", project_ids=frozenset({_PRJ_A})),
        "unrestricted": Scope(user_id="u"),
        "read-all": Scope(user_id="u", read_all=True),
        "empty-entity": Scope(user_id="u", entity_ids=frozenset()),
    }
    fixture_rows = {
        _PRJ_A: {"entity_id": _ENT_A, "plant_id": _PLT_A, "location_id": _LOC_A, "project_id": _PRJ_A},
        _PRJ_B: {"entity_id": _ENT_B, "plant_id": _PLT_B, "location_id": _LOC_B, "project_id": _PRJ_B},
    }

    compiler_session = Session(connection=pg_connection, scope=Scope.system())
    for label, scope in scopes.items():
        expected = {pid for pid, dims in fixture_rows.items() if rls.permits(scope, **dims)}

        compiler_rows = repo.query(
            compiler_session,
            "SELECT project_id FROM project WHERE {scope}",
            scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)
        pg_connection.rollback()
        compiler_seen = {row[0] for row in compiler_rows}

        rls_rows = _rls_rows(
            pg_connection, scope,
            "SELECT project_id FROM project")
        rls_seen = {row[0] for row in rls_rows}

        assert compiler_seen == expected, f"[{label}] compiler disagreed with rls.permits()"
        assert rls_seen == expected, f"[{label}] live RLS disagreed with rls.permits()"
        assert compiler_seen == rls_seen, f"[{label}] compiler and RLS disagree with each other"


@PG
def test_wbs_element_rls_matches_compiler_live(pg_database, pg_connection):
    with pg_database.session(Scope.system()) as session:
        _seed_rls_fixture(session)

    scopes = {
        "project-scoped-A": Scope(user_id="u", project_ids=frozenset({_PRJ_A})),
        # WBS_ELEMENT_SCOPE_COLUMNS waives entity/plant/location -- a caller
        # scoped by entity ALONE (project_ids=None -> unrestricted on the
        # only dimension this table's mapping actually filters by) sees
        # every wbs_element, by the documented, deliberate waiver.
        "entity-scoped-waived": Scope(user_id="u", entity_ids=frozenset({_ENT_A})),
        "empty-project": Scope(user_id="u", project_ids=frozenset()),
    }
    expected_by_label = {
        "project-scoped-A": {_WBS_A},
        "entity-scoped-waived": {_WBS_A, _WBS_B},
        "empty-project": set(),
    }

    compiler_session = Session(connection=pg_connection, scope=Scope.system())
    for label, scope in scopes.items():
        compiler_rows = repo.query(
            compiler_session,
            "SELECT wbs_id FROM wbs_element WHERE {scope}",
            scope=scope, columns=repo.WBS_ELEMENT_SCOPE_COLUMNS)
        pg_connection.rollback()
        compiler_seen = {row[0] for row in compiler_rows}

        rls_rows = _rls_rows(
            pg_connection, scope,
            "SELECT wbs_id FROM wbs_element")
        rls_seen = {row[0] for row in rls_rows}

        assert compiler_seen == expected_by_label[label], label
        assert rls_seen == expected_by_label[label], label


@PG
def test_budget_cell_rls_matches_join_compiler_live(pg_database, pg_connection):
    """`budget_control_cell` / `budget_ledger_cell` carry no scope column of
    their own; both the RLS policy (in the migration) and the compiler query
    built here reach `project_id` the same way -- one join out, through
    `wbs_element` -- mirroring `WBS_ELEMENT_SCOPE_COLUMNS`'s project-only
    waiver one level further down the hierarchy.
    """
    with pg_database.session(Scope.system()) as session:
        _seed_rls_fixture(session)

    scope_a = Scope(user_id="u", project_ids=frozenset({_PRJ_A}))
    scope_empty = Scope(user_id="u", project_ids=frozenset())

    compiler_session = Session(connection=pg_connection, scope=Scope.system())
    join_columns = {"project": "we.project_id", "entity": None, "plant": None, "location": None}
    join_sql = (
        "SELECT bcc.wbs_id FROM budget_control_cell bcc "
        "JOIN wbs_element we ON we.wbs_id = bcc.wbs_id "
        "WHERE {scope}"
    )

    for scope, expected in ((scope_a, {_WBS_A}), (scope_empty, set())):
        compiler_rows = repo.query(compiler_session, join_sql, scope=scope, columns=join_columns)
        pg_connection.rollback()
        compiler_seen = {row[0] for row in compiler_rows}

        rls_rows = _rls_rows(
            pg_connection, scope,
            "SELECT wbs_id FROM budget_control_cell")
        rls_seen = {row[0] for row in rls_rows}

        assert compiler_seen == expected
        assert rls_seen == expected

        ledger_rows = _rls_rows(
            pg_connection, scope,
            "SELECT wbs_id FROM budget_ledger_cell")
        assert {row[0] for row in ledger_rows} == expected


@PG
def test_org_table_rls_matches_pure_python_permits_live(pg_database, pg_connection):
    """`entity` / `plant` / `location` carry no `repo.py` mapping to compare
    against (no other stream reads them through `repo.query()` yet), so
    `rls.permits` is the oracle here instead -- still an independent
    implementation from the SQL policy under test, in a different language.
    """
    with pg_database.session(Scope.system()) as session:
        _seed_rls_fixture(session)

    fixtures = {
        "entity": {_ENT_A: {"entity_id": _ENT_A}, _ENT_B: {"entity_id": _ENT_B}},
        "plant": {_PLT_A: {"entity_id": _ENT_A, "plant_id": _PLT_A},
                  _PLT_B: {"entity_id": _ENT_B, "plant_id": _PLT_B}},
        "location": {_LOC_A: {"entity_id": _ENT_A, "plant_id": _PLT_A, "location_id": _LOC_A},
                     _LOC_B: {"entity_id": _ENT_B, "plant_id": _PLT_B, "location_id": _LOC_B}},
    }
    id_column = {"entity": "entity_id", "plant": "plant_id", "location": "location_id"}
    scope_a = Scope(user_id="u", entity_ids=frozenset({_ENT_A}))

    for table, rows in fixtures.items():
        expected = {rid for rid, dims in rows.items() if rls.permits(scope_a, **dims)}
        seen = {row[0] for row in _rls_rows(
            pg_connection, scope_a,
            f"SELECT {id_column[table]} FROM {table}")}
        assert seen == expected, table


@PG
def test_empty_scope_yields_no_rows_via_compiler_and_rls_live(pg_database, pg_connection):
    """The headline invariant: `Scope(entity_ids=frozenset())` -- no grants
    -- must yield NO rows, through both the compiler and RLS, never all
    rows. Checked against the full, un-filtered seed_demo.sql-shaped table
    (not just this file's own fixture), so there is no chance a narrow
    fixture happens to be small enough to look empty by coincidence.
    """
    with pg_database.session(Scope.system()) as session:
        _seed_rls_fixture(session)

    scope = Scope(user_id="u", entity_ids=frozenset())
    compiler_session = Session(connection=pg_connection, scope=Scope.system())

    compiler_rows = repo.query(
        compiler_session, "SELECT project_id FROM project WHERE {scope}",
        scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)
    pg_connection.rollback()
    assert compiler_rows == []

    rls_rows = _rls_rows(pg_connection, scope, "SELECT project_id FROM project")
    assert rls_rows == []


@PG
def test_role_and_scope_do_not_leak_across_pool_reuse_live(pg_url, pg_disposable_db_name):
    """Extends `tests/test_pg_scope_leakage.py`'s pool-reuse proof to cover
    THIS stream's addition: `rls.assume_scoped_role`'s `SET LOCAL ROLE`, not
    only the `capex.*` scope settings. A size-1 pool forces the second
    acquisition to reuse the exact same backend the first one used.
    """
    import psycopg

    from conftest_pg import _config_and_provider, _replace_dbname
    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider, min_size=1, max_size=1)
    try:
        scope_a = pg_engine.Scope(user_id="user-a", entity_ids=frozenset({"ENT-ROLE-LEAK-A"}))
        with database._pool.connection() as con:
            rls.assume_scoped_role(con, scope_a)
            role_row = con.execute("SELECT current_user").fetchone()
            assert role_row[0] == "capex_app"
            con.commit()

        with database._pool.connection() as con2:
            role_after = con2.execute("SELECT current_user").fetchone()[0]
            leaked_scope = con2.execute(
                "SELECT current_setting('capex.entity_ids', true)").fetchone()[0]
            con2.rollback()
    finally:
        database.close()

    assert role_after != "capex_app", (
        f"SET LOCAL ROLE leaked across pool reuse: current_user was still "
        f"{role_after!r} on a reused connection before any new role was set")
    assert leaked_scope in (None, ""), (
        f"scope leaked across pool reuse alongside the role: "
        f"capex.entity_ids={leaked_scope!r}")


@PG
def test_no_service_maker_checker_across_seeded_and_fixture_users_live(pg_database):
    """Whole-database invariant, run against a database carrying both this
    file's own fixture rows and (if loaded) the demo seed -- belt-and-braces
    alongside `tests/test_pg_roles.py`'s dedicated live test."""
    from app.backend.pg import roles as roles_mod

    with pg_database.session(Scope.system()) as session:
        roles_mod.assert_no_service_maker_checker(session)  # must not raise
