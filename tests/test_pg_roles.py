"""Tests for `app.backend.pg.roles` -- role/permission catalog and the
role-and-scope resolver.

Two layers, same split as `tests/test_pg_locking.py`:

  * The role/permission catalog, `assert_role_grantable`'s maker-checker
    guard, and static consistency checks against the frozen sources
    (`research/30_contracts/C9_roles.json`, `migrations/pg/004_identity_scope.sql`,
    `migrations/pg/seed_parts/004_access.sql`) are pure Python / text
    parsing and run unconditionally, no database needed.
  * `resolve_scope`, `resolve_grant`, `set_roles`, `set_scope` and the
    whole-database `assert_no_service_maker_checker` invariant need a live
    PostgreSQL database (the None-vs-empty-frozenset distinction is read
    back from real rows, not merely asserted) and are gated below, exactly
    like `tests/test_pg_locking.py`.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_locking.py's module docstring for why this cannot be a
# tests/pg/conftest.py instead.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import json
import os
import re

import pytest

from app.backend.pg import roles
from app.backend.pg.engine import Scope

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
C9_ROLES_PATH = PROJECT_ROOT / "research" / "30_contracts" / "C9_roles.json"
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "pg" / "004_identity_scope.sql"
SEED_DEMO_PATH = PROJECT_ROOT / "migrations" / "pg" / "seed_demo.sql"
SEED_ACCESS_PATH = PROJECT_ROOT / "migrations" / "pg" / "seed_parts" / "004_access.sql"


# ======================================================================= catalog
def test_roles_match_frozen_c9_contract():
    """`roles.ROLES` must be exactly the 13 roles frozen at
    research/30_contracts/C9_roles.json -- "Every screen's 'Permission
    requirements' attribute must name roles from this list only." A role
    invented here that is not in that file would pass every other test in
    this suite while silently violating the contract the whole product's
    screens are audited against.
    """
    data = json.loads(C9_ROLES_PATH.read_text(encoding="utf-8"))
    assert tuple(data["roles"]) == roles.ROLES
    assert data["count"] == len(roles.ROLES) == 13


def test_migration_check_constraint_matches_roles_py():
    """The `role_grant.role` CHECK list in the migration is hand-kept in
    sync with `roles.ROLES` (SQL cannot import a Python constant). A regex
    over the migration's own text, so a future edit to either one that
    drifts from the other fails here instead of at INSERT time in
    production.
    """
    text = _strip_comments(MIGRATION_PATH.read_text(encoding="utf-8"))
    match = re.search(r"role\s+text\s+NOT NULL\s+CHECK\s*\(role\s+IN\s*\((.*?)\)\)",
                       text, re.DOTALL)
    assert match, "could not find role_grant's role CHECK (...) clause"
    found = tuple(re.findall(r"'([^']+)'", match.group(1)))
    assert found == roles.ROLES


def test_permissions_reference_only_known_roles():
    for permission, holders in roles.PERMISSIONS.items():
        unknown = set(holders) - set(roles.ROLES)
        assert not unknown, f"{permission} names unknown role(s): {unknown}"


def test_maker_checker_is_a_subset_of_known_permissions():
    assert roles.MAKER_CHECKER <= set(roles.PERMISSIONS)


def test_roles_with_maker_checker_is_nonempty_and_known():
    holders = roles.roles_with_maker_checker()
    assert holders, "MAKER_CHECKER carries no roles at all -- suspicious for a " \
                     "financial-controls product"
    assert holders <= set(roles.ROLES)


def test_permissions_for_roles_is_a_union():
    cfo_perms = roles.permissions_for_roles(["CFO"])
    committee_perms = roles.permissions_for_roles(["CAPEX Committee"])
    combined = roles.permissions_for_roles(["CFO", "CAPEX Committee"])
    assert combined == cfo_perms | committee_perms
    assert combined  # neither role holds zero permissions


def test_permissions_for_unknown_role_is_empty_not_an_error():
    # No role holds a permission that does not exist for it -- an unrecognised
    # role name simply matches nothing, it does not raise. (Validating role
    # NAMES is `assert_role_grantable`'s job, not this pure set operation's.)
    assert roles.permissions_for_roles(["Not A Real Role"]) == frozenset()


def test_roles_with_permission_unknown_permission_raises():
    with pytest.raises(roles.UnknownPermission):
        roles.roles_with_permission("not.a.real.permission")


# ============================================================ maker-checker guard
def test_assert_role_grantable_rejects_unknown_role():
    with pytest.raises(roles.UnknownRole):
        roles.assert_role_grantable("USER", "Not A Real Role")


def test_assert_role_grantable_allows_user_any_role():
    for role in roles.ROLES:
        roles.assert_role_grantable("USER", role)  # must not raise


def test_assert_role_grantable_denies_service_maker_checker_role():
    maker_checker_role = sorted(roles.roles_with_maker_checker())[0]
    with pytest.raises(roles.ServiceMakerCheckerDenied):
        roles.assert_role_grantable("SERVICE", maker_checker_role)


def test_assert_role_grantable_allows_service_non_maker_checker_role():
    non_maker_checker = set(roles.ROLES) - roles.roles_with_maker_checker()
    assert non_maker_checker, "every role carries a maker-checker permission?"
    for role in non_maker_checker:
        roles.assert_role_grantable("SERVICE", role)  # must not raise


# =========================================================== seed fragment guard
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_comments(text: str) -> str:
    """`--` line comments stripped, same technique `migrate_pg.py` uses for
    its own best-effort SQL text scans. A comment's prose (which may contain
    a stray `;` or `)`, as this file's own comments do) must never be able
    to confuse a regex that is only meant to see statement text."""
    return _LINE_COMMENT_RE.sub("", text)


def _seed_role_grants(text: str) -> list[tuple[str, str]]:
    """`(user_id, role)` pairs from a `role_grant` VALUES block in seed SQL --
    a light regex, not a SQL parser, matching only the specific
    `('U-X', 'Role Name', 'granted_by')` row shape this seed file uses."""
    text = _strip_comments(text)
    block_match = re.search(r"INSERT INTO role_grant[^;]*VALUES\s*(.*?);",
                             text, re.DOTALL)
    assert block_match, "no `INSERT INTO role_grant ... VALUES` block found"
    rows = re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'[^']*'\s*\)",
                       block_match.group(1))
    return rows


def _seed_service_users(text: str) -> set[str]:
    rows = re.findall(r"\(\s*'([^']+)'[^)]*'SERVICE'[^)]*\)", _strip_comments(text))
    return set(rows)


def test_seed_fragment_grants_no_service_maker_checker_role():
    """The seed data itself must comply with the rule it exists to
    demonstrate: SVC-ZOHO (or any other SERVICE principal seed_demo.sql
    ever adds) must never be granted a maker-checker-carrying role. A
    text-level check, so this catches a regression in the seed FILE without
    needing a live database.
    """
    service_users = _seed_service_users(SEED_DEMO_PATH.read_text(encoding="utf-8"))
    assert service_users, "expected at least one SERVICE principal in seed_demo.sql"

    access_text = SEED_ACCESS_PATH.read_text(encoding="utf-8")
    grants = _seed_role_grants(access_text)
    maker_checker_roles = roles.roles_with_maker_checker()

    offenders = [(user_id, role) for user_id, role in grants
                 if user_id in service_users and role in maker_checker_roles]
    assert offenders == [], (
        f"seed_parts/004_access.sql grants a maker-checker role to a SERVICE "
        f"principal: {offenders}")


def test_seed_fragment_demonstrates_empty_grant_set():
    """`docs/WAVE2_CONTRACTS.md` / this stream's brief require a demo user
    with an EMPTY grant set on at least one dimension, so the "no grants"
    case is demonstrable rather than theoretical. U-NOGRANT is that user --
    assert the seed fragment actually restricts every dimension to zero
    values for it, not just some.
    """
    text = _strip_comments(SEED_ACCESS_PATH.read_text(encoding="utf-8"))
    restriction_rows = re.findall(
        r"\('U-NOGRANT',\s*'(\w+)',\s*'U-ADM'\)", text)
    assert set(restriction_rows) == set(roles.DIMENSIONS), (
        "U-NOGRANT must have a user_scope_restriction row for every "
        "dimension to demonstrate the empty-grant-set case fully")
    # And no user_scope_grant row at all for U-NOGRANT -- zero permitted
    # values on every one of those restricted dimensions.
    for grant_block in re.findall(r"INSERT INTO user_scope_grant.*?;", text, re.DOTALL):
        assert "U-NOGRANT" not in grant_block, (
            "U-NOGRANT has a user_scope_grant row -- it must be restricted "
            "to ZERO values on every dimension to demonstrate the "
            "empty-grant-set case")


# =========================================================================== PG
@PG
def test_resolve_scope_distinguishes_none_from_empty_live(pg_database, pg_scope):
    """The core invariant, proven against real rows: a user with NO
    `user_scope_restriction` row for a dimension resolves to `None`
    (unrestricted); a user WITH a restriction row but zero `user_scope_grant`
    rows resolves to `frozenset()` (nothing) -- never conflated.
    """
    from app.backend.pg import roles as roles_mod

    with pg_database.session(Scope.system()) as session:
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
            "created_by, updated_by) VALUES (%s, %s, %s, 'USER', 'TEST', 'TEST')",
            ("U-UNRESTRICTED", "u1@test.example", "Unrestricted Test User"))
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
            "created_by, updated_by) VALUES (%s, %s, %s, 'USER', 'TEST', 'TEST')",
            ("U-EMPTYSET", "u2@test.example", "Empty-Set Test User"))
        session.execute(
            "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
            "VALUES (%s, 'entity', 'TEST')", ("U-EMPTYSET",))
        # Deliberately no user_scope_grant row for U-EMPTYSET's entity dimension.

        unrestricted = roles_mod.resolve_scope(session, "U-UNRESTRICTED")
        empty = roles_mod.resolve_scope(session, "U-EMPTYSET")

    assert unrestricted.entity_ids is None
    assert empty.entity_ids == frozenset()
    assert empty.entity_ids is not None  # the exact distinction under test


@PG
def test_set_roles_and_set_scope_round_trip_live(pg_database):
    from app.backend.pg import roles as roles_mod

    with pg_database.session(Scope.system()) as session:
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
            "created_by, updated_by) VALUES ('U-ROUNDTRIP', 'rt@test.example', "
            "'Round Trip User', 'USER', 'TEST', 'TEST')")

        roles_mod.set_roles(session, "U-ROUNDTRIP",
                             ["Project Manager", "Finance"], granted_by="U-TESTADMIN")
        roles_mod.set_scope(
            session, "U-ROUNDTRIP",
            {"entity_ids": ["ENT-RT-1"], "plant_ids": None,
             "project_ids": [], "location_ids": None},
            read_all=False, updated_by="U-TESTADMIN")

        grant = roles_mod.resolve_grant(session, "U-ROUNDTRIP")

    assert grant is not None
    assert grant.roles == ("Finance", "Project Manager")  # ORDER BY role
    assert grant.scope.entity_ids == frozenset({"ENT-RT-1"})
    assert grant.scope.plant_ids is None
    assert grant.scope.project_ids == frozenset()  # empty, not unrestricted
    assert grant.scope.location_ids is None
    assert grant.scope.read_all is False
    assert "revision.create" in grant.permissions  # Project Manager holds it


@PG
def test_set_roles_denies_service_maker_checker_and_writes_nothing(pg_database):
    from app.backend.pg import roles as roles_mod

    with pg_database.session(Scope.system()) as session:
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
            "created_by, updated_by) VALUES ('SVC-TEST', 'svc@test.example', "
            "'Test Service', 'SERVICE', 'TEST', 'TEST')")

        maker_checker_role = sorted(roles_mod.roles_with_maker_checker())[0]
        with pytest.raises(roles_mod.ServiceMakerCheckerDenied):
            roles_mod.set_roles(session, "SVC-TEST", [maker_checker_role],
                                 granted_by="U-TESTADMIN")

        # Nothing was written -- validated up front, before any DELETE/INSERT.
        current = roles_mod.resolve_roles(session, "SVC-TEST")

    assert current == ()


@PG
def test_assert_no_service_maker_checker_live(pg_database):
    from app.backend.pg import roles as roles_mod

    with pg_database.session(Scope.system()) as session:
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
            "created_by, updated_by) VALUES ('SVC-VIOLATION', 'v@test.example', "
            "'Violating Service', 'SERVICE', 'TEST', 'TEST')")
        roles_mod.assert_no_service_maker_checker(session)  # clean so far

        maker_checker_role = sorted(roles_mod.roles_with_maker_checker())[0]
        # Bypass set_roles' guard deliberately, to prove the READ-ONLY
        # invariant check catches a violation created some OTHER way.
        session.execute(
            "INSERT INTO role_grant (user_id, role, granted_by) VALUES "
            "('SVC-VIOLATION', %s, 'TEST')", (maker_checker_role,))

        with pytest.raises(AssertionError):
            roles_mod.assert_no_service_maker_checker(session)


@PG
def test_seed_fragment_produces_the_five_archetypes_live(
        pg_url, pg_template, pg_admin_connection, pg_disposable_db_name):
    """Loads seed_demo.sql then seed_parts/004_access.sql against a freshly
    migrated disposable database (bypassing app.backend.pg.seed's profile/
    guard machinery deliberately -- this test is exercising the SEED SQL
    TEXT itself, not the loader), then resolves every one of the five
    documented scope archetypes and checks each is what the seed fragment's
    own header comment claims.
    """
    import psycopg

    from conftest_pg import _config_and_provider, _replace_dbname
    from app.backend.pg import roles as roles_mod
    from app.backend.pg.engine import Database

    with psycopg.connect(_replace_dbname(pg_url, pg_disposable_db_name),
                          autocommit=False) as con:
        con.execute(SEED_DEMO_PATH.read_text(encoding="utf-8"))
        con.execute(SEED_ACCESS_PATH.read_text(encoding="utf-8"))
        con.commit()

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = Database(cfg, secret_provider=provider, min_size=1, max_size=2)
    try:
        with database.session(Scope.system()) as session:
            pfc = roles_mod.resolve_grant(session, "U-PFC")
            plh = roles_mod.resolve_grant(session, "U-PLH")
            pm = roles_mod.resolve_grant(session, "U-PM")
            adm = roles_mod.resolve_grant(session, "U-ADM")
            nogrant = roles_mod.resolve_grant(session, "U-NOGRANT")
            cfo = roles_mod.resolve_grant(session, "U-CFO")
    finally:
        database.close()

    assert pfc.scope.entity_ids == frozenset({"ENT-DM1"})
    assert pfc.scope.plant_ids is None and pfc.scope.project_ids is None

    assert plh.scope.plant_ids == frozenset({"PLT-DM1-A"})
    assert plh.scope.entity_ids is None

    assert pm.scope.project_ids == frozenset({"PRJ-DM-001"})
    assert pm.scope.entity_ids is None and pm.scope.plant_ids is None

    assert adm.scope.read_all is True

    assert nogrant.scope.entity_ids == frozenset()
    assert nogrant.scope.plant_ids == frozenset()
    assert nogrant.scope.project_ids == frozenset()
    assert nogrant.scope.location_ids == frozenset()
    assert nogrant.roles == ("Read-only Management User",)

    # U-CFO: never configured -- unrestricted on every dimension by absence,
    # distinct from U-ADM's explicit read_all.
    assert cfo.scope.read_all is False
    assert cfo.scope.entity_ids is None
    assert cfo.scope.plant_ids is None
    assert cfo.scope.project_ids is None
    assert cfo.scope.location_ids is None
    # SVC-ZOHO's compliance with the maker-checker rule has its own
    # dedicated live test (test_assert_no_service_maker_checker_live) plus
    # the database-free test_seed_fragment_grants_no_service_maker_checker_role.
