"""RLS coverage: does every table that must carry row-level security have it?

This file exists because `tests/test_pg_rls.py` structurally cannot answer that
question. Its coverage test walks `rls.RLS_TABLES` -- a registry transcribed
FROM `migrations/pg/004_identity_scope.sql` -- and checks it against that same
migration. Two views of one source. A table absent from both agrees with itself
perfectly and the suite stays green, which is exactly what happened to
`accounting_period`, `budget_line`, `budget_revision`, `budget_transfer`,
`budget_version`, `budget_version_cell`, `item_master` and `vendor_master`
through all of Wave 2.

`app.backend.pg.scope_inventory` is the second, independent source: written by
hand from the schema's `CREATE TABLE` statements, not from any policy. The
database-free tests below cross-check it against the migration text and against
`rls.py`'s registry -- and, crucially, prove the cross-check actually DETECTS an
uncovered table rather than merely passing (see
`test_the_coverage_check_detects_a_table_covered_nowhere`). A guard nobody has
watched fail is not a guard.

The live tests then prove the two enforcement layers each hold ALONE:

  * bypass the application layer -- connect as the restricted `capex_app` role,
    apply the session scope settings directly, and run a RAW query carrying NO
    `repo.compile_scope` predicate at all. RLS must still hide out-of-scope
    rows.
  * bypass RLS -- run as the superuser fixture role (which bypasses RLS
    unconditionally) and let `repo.query` be the only filter. It must hide the
    same rows.

Agreement between them is asserted against a third, pure-Python oracle
(`rls.permits` and its 006 siblings), so neither implementation is assumed
correct and checked against the other.

Live tests carry `@pytest.mark.pg` (registered in `pytest.ini`) plus a
`skipif` on `CAPEX_DB_URL`. There is no local PostgreSQL on the dev machine, so
they SKIP here and run for real in CI's `pg_tests` job.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# Without this the live tests below fail at SETUP with "fixture 'pg_database'
# not found" -- but ONLY where CAPEX_DB_URL is set. Locally they skip, so the
# missing fixture is never resolved and the gap is invisible. CI is the first
# place these run for real, which is the whole point of that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import repo, rls, scope_inventory  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "pg"
COVERAGE_MIGRATION = MIGRATIONS_DIR / "006_rls_coverage.sql"

#: Every numbered migration's text, by filename. Read once -- the coverage
#: check must look across ALL migrations, not just one, or it would report a
#: table protected by 004 as uncovered when the inventory attributes it to 006
#: and vice versa.
MIGRATION_SOURCES: dict[str, str] = {
    path.name: path.read_text(encoding="utf-8")
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
}
ALL_MIGRATION_TEXT = "\n".join(MIGRATION_SOURCES.values())


# ===================================================== the coverage checker
def _protection_defects(table: str, text: str) -> list[str]:
    """What `text` fails to do for `table`. Empty list means fully protected.

    All three statements are required and each catches a different silent
    failure: a policy with no `ENABLE` is inert; an `ENABLE` with no `FORCE`
    is bypassed by the table's owner (the deploy identity, in production);
    an `ENABLE`+`FORCE` with no policy denies everything and would be caught
    the first time anyone read the table -- but only in an environment where
    something reads it.
    """
    defects: list[str] = []
    if f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" not in text:
        defects.append("no ENABLE ROW LEVEL SECURITY")
    if f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" not in text:
        defects.append("no FORCE ROW LEVEL SECURITY")
    if not re.search(rf"CREATE POLICY \w+ ON {table}\b", text):
        defects.append("no CREATE POLICY")
    return defects


def _uncovered(tables) -> dict[str, list[str]]:
    """`{table: [what is missing]}` for every table not fully protected by ANY
    migration. This is the function the meta-test below fires at a known-
    uncovered table to prove it has teeth."""
    return {
        table: defects
        for table in tables
        if (defects := _protection_defects(table, ALL_MIGRATION_TEXT))
    }


# =========================================== inventory vs migration source
def test_every_table_the_inventory_marks_covered_is_protected_in_the_migrations():
    """The primary coverage assertion, and the one Wave 2 had no way to make.

    `scope_inventory` is hand-written from the schema, so a table it lists is
    listed whether or not any migration protects it. That is what makes
    "protected nowhere" a failure here instead of an agreement between two
    copies of the same omission.
    """
    missing = _uncovered(scope_inventory.covered_tables())
    assert missing == {}, (
        "tables the scope inventory requires RLS on are not protected by any "
        f"migration: {missing}")


def test_each_covered_table_is_protected_by_the_migration_the_inventory_names():
    """Not just "protected somewhere" -- protected by the file the inventory
    attributes it to. Catches a table quietly re-homed between migrations,
    which would leave the inventory's own documentation wrong even though
    coverage held."""
    wrong: dict[str, str] = {}
    for entry in scope_inventory.SCOPED_TABLES:
        if entry.status != "covered":
            continue
        assert entry.migration in MIGRATION_SOURCES, (
            f"{entry.table}: inventory names migration {entry.migration!r}, "
            f"which does not exist")
        defects = _protection_defects(entry.table, MIGRATION_SOURCES[entry.migration])
        if defects:
            wrong[entry.table] = f"{entry.migration}: {defects}"
    assert wrong == {}, wrong


def test_the_coverage_check_detects_a_table_covered_nowhere():
    """The guard's own proof. A coverage test that has never been observed to
    fail is an assumption, not a control.

    `budget_head` is a REAL, currently uncovered table (it carries
    `entity_id NOT NULL REFERENCES entity` and no migration policies it -- see
    the reported gap in `scope_inventory`). Firing the same checker the passing
    test above uses at it must produce a defect list. A wholly fictional table
    is checked too, so the test does not depend on that gap staying open
    forever.
    """
    assert _uncovered(["budget_head"]) == {
        "budget_head": ["no ENABLE ROW LEVEL SECURITY",
                        "no FORCE ROW LEVEL SECURITY",
                        "no CREATE POLICY"]}
    assert _uncovered(["a_table_no_migration_has_ever_created"]) != {}
    # ...and the checker is not simply always-failing:
    assert _uncovered(["entity", "budget_line"]) == {}


def test_the_coverage_check_rejects_enable_without_force():
    """`FORCE` is the half most easily dropped, and dropping it is invisible
    from `pg_policies`. Prove the checker fails on that exact shape rather
    than only on a wholly absent table."""
    text = ("ALTER TABLE demo_table ENABLE ROW LEVEL SECURITY;\n"
            "CREATE POLICY demo_scope ON demo_table USING (true);\n")
    assert _protection_defects("demo_table", text) == ["no FORCE ROW LEVEL SECURITY"]


def test_reported_gaps_are_still_gaps():
    """A gap the inventory records must still BE a gap.

    If someone closes `budget_head`'s coverage, this fails and forces the
    inventory entry to be reclassified `covered`. Without it the note rots
    into a false claim that a protected table is unprotected -- the mirror
    image of the defect this whole file exists to prevent.
    """
    for table in scope_inventory.gap_tables():
        assert _protection_defects(table, ALL_MIGRATION_TEXT), (
            f"{table} is now protected by a migration, but scope_inventory "
            f"still records it as a reported gap. Reclassify the entry to "
            f"status='covered' and name the migration.")


# ================================================= inventory vs rls.py registry
def test_rls_registry_and_the_independent_inventory_name_the_same_tables():
    """The two sources must agree on WHICH tables are protected. They are
    written from different things -- `rls.py` from the migrations, the
    inventory from the schema -- so agreement is evidence, not tautology."""
    assert set(scope_inventory.covered_tables()) == set(rls.ALL_RLS_TABLES)


def test_rls_registry_and_the_inventory_agree_on_which_migration_covers_what():
    from_registry = dict(rls.RLS_MIGRATION_BY_TABLE)
    from_inventory = {
        entry.table: entry.migration
        for entry in scope_inventory.SCOPED_TABLES if entry.status == "covered"
    }
    assert from_registry == from_inventory


def test_a_table_in_neither_the_inventory_nor_the_registry_would_fail():
    """The failure mode this file was written for, stated as an executable
    assertion: if a scope-carrying table is missing from BOTH sources, the
    registry-vs-inventory check cannot see it -- only the schema sweep below
    can. This test pins the division of labour so nobody deletes the sweep
    believing this check covers it."""
    invented = "capex_shadow_table"
    assert invented not in rls.ALL_RLS_TABLES
    assert invented not in scope_inventory.tables_requiring_rls()
    # Both sources silently agree -- which is why the schema sweep exists.
    assert set(scope_inventory.covered_tables()) == set(rls.ALL_RLS_TABLES)


def test_every_table_in_the_schema_is_classified_scoped_or_deliberately_not():
    """The sweep: every `CREATE TABLE` in migrations 001..005 must appear in
    `SCOPED_TABLES` or in `UNSCOPED_TABLES` with a written reason. This is the
    only check that can catch a NEW table added with no RLS and no registry
    entry -- the exact shape of the Wave 2 defect."""
    created: set[str] = set()
    for name, text in MIGRATION_SOURCES.items():
        created.update(re.findall(r"^CREATE TABLE (\w+)", text, flags=re.MULTILINE))

    classified = set(scope_inventory.tables_requiring_rls()) | set(
        scope_inventory.UNSCOPED_TABLES)
    unclassified = sorted(created - classified)
    assert unclassified == [], (
        "tables created by a migration but classified by neither "
        f"SCOPED_TABLES nor UNSCOPED_TABLES: {unclassified}. Decide, in "
        "app/backend/pg/scope_inventory.py, whether each carries or reaches a "
        "scope dimension -- and record the reason either way.")

    # ...and no phantom entries pointing at tables that do not exist.
    # `schema_migrations` is created by the runner's BOOTSTRAP, not by a
    # migration file, so it is the one legitimate exception.
    phantom = sorted(classified - created - {"schema_migrations"})
    assert phantom == [], f"inventory names tables no migration creates: {phantom}"


def test_scoped_and_unscoped_are_disjoint():
    overlap = set(scope_inventory.tables_requiring_rls()) & set(
        scope_inventory.UNSCOPED_TABLES)
    assert overlap == set(), (
        f"a table cannot be both scope-carrying and deliberately unscoped: "
        f"{sorted(overlap)}")


def test_every_unscoped_table_carries_a_reason():
    empty = sorted(t for t, why in scope_inventory.UNSCOPED_TABLES.items()
                   if not why.strip())
    assert empty == [], f"no reason recorded for: {empty}"


# ============================================= 006's own contract compliance
def test_006_covers_exactly_the_eight_tables_the_security_gate_names():
    """docs/WAVE3_CONTRACTS.md's security-closure gate names eight tables.
    Reading them from the contract document rather than restating them here
    keeps this test honest if the gate ever changes."""
    expected = {
        "accounting_period", "budget_line", "budget_revision", "budget_transfer",
        "budget_version", "budget_version_cell", "item_master", "vendor_master",
    }
    assert set(rls.RLS_COVERAGE_TABLES) == expected
    assert set(scope_inventory.tables_for_migration("006_rls_coverage.sql")) == expected


def test_006_does_not_redefine_the_frozen_scope_functions():
    """Contract 1 freezes `capex_scope_permits` / `capex_dimension_permits`,
    and stream 3's migration 007 replaces their BODIES. 006 must call them,
    never redefine them: two migrations editing one function body is a silent
    last-writer-wins collision that neither stream's tests would show."""
    text = COVERAGE_MIGRATION.read_text(encoding="utf-8")
    for function in ("capex_scope_permits", "capex_dimension_permits"):
        assert not re.search(
            rf"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+{function}\b", text), (
            f"006 redefines {function}, which Contract 1 freezes and "
            f"007_scope_sentinel.sql replaces.")
    # ...but it must actually USE the frozen predicate.
    assert "capex_scope_permits(" in text


def test_006_declares_a_rollback_section_naming_every_policy_it_creates():
    text = COVERAGE_MIGRATION.read_text(encoding="utf-8")
    assert "-- ROLLBACK:" in text
    rollback = text.split("-- ROLLBACK:", 1)[1]
    created = re.findall(r"^CREATE POLICY (\w+) ON (\w+)", text, flags=re.MULTILINE)
    assert created, "006 creates no policies at all"
    for policy, table in created:
        assert f"DROP POLICY IF EXISTS {policy} ON {table};" in rollback, (
            f"{policy} on {table} is created but not dropped in the ROLLBACK "
            f"section")
        assert f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;" in rollback, table


def test_reference_tables_are_not_left_on_the_fail_open_all_null_predicate():
    """`capex_scope_permits(NULL, NULL, NULL, NULL)` is the tempting reuse for
    a table with no dimension column, and it returns TRUE for a session
    carrying no settings whatsoever -- fail OPEN. The reference tables must use
    `capex_principal_present()` instead."""
    text = COVERAGE_MIGRATION.read_text(encoding="utf-8")
    assert not re.search(
        r"capex_scope_permits\(\s*NULL\s*,\s*NULL\s*,\s*NULL\s*,\s*NULL\s*\)", text)
    for table in sorted(rls.REFERENCE_TABLES):
        policy = re.search(
            rf"CREATE POLICY \w+ ON {table}\b(.*?);", text, flags=re.DOTALL)
        assert policy, table
        assert "capex_principal_present()" in policy.group(1), table


def test_006_forces_row_level_security_on_every_table_it_enables():
    """A table owner bypasses ENABLE-only RLS silently. Asserted at source
    level so it holds even where no database is available; the live test
    below asserts the same thing from `pg_class`."""
    text = COVERAGE_MIGRATION.read_text(encoding="utf-8")
    enabled = set(re.findall(
        r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY", text))
    forced = set(re.findall(
        r"ALTER TABLE (\w+) FORCE ROW LEVEL SECURITY", text))
    assert enabled == set(rls.RLS_COVERAGE_TABLES)
    assert enabled - forced == set(), f"ENABLE without FORCE: {sorted(enabled - forced)}"


# ===================================== pure-Python mirrors of 006's predicates
def test_transfer_permits_requires_both_legs():
    scope = Scope(user_id="u", project_ids=frozenset({"PRJ-A"}))
    assert rls.transfer_permits(scope, from_project_id="PRJ-A", to_project_id="PRJ-A") is True
    # The leak OR would allow: one leg visible, the other not.
    assert rls.transfer_permits(scope, from_project_id="PRJ-A", to_project_id="PRJ-B") is False
    assert rls.transfer_permits(scope, from_project_id="PRJ-B", to_project_id="PRJ-A") is False
    assert rls.transfer_permits(scope, from_project_id="PRJ-B", to_project_id="PRJ-B") is False


def test_transfer_permits_denies_a_missing_join_row_rather_than_waiving_it():
    """`None` here means "no `wbs_element` row for that `wbs_id`" -- the SQL
    side is `EXISTS (...)`, which is FALSE. It must not take `permits`'
    "column waived for this row shape" path, which returns True."""
    scope = Scope(user_id="u", project_ids=frozenset({"PRJ-A"}))
    assert rls.transfer_permits(scope, from_project_id="PRJ-A", to_project_id=None) is False
    assert rls.transfer_permits(scope, from_project_id=None, to_project_id="PRJ-A") is False


def test_transfer_permits_honours_read_all():
    scope = Scope(user_id="svc", read_all=True)
    assert rls.transfer_permits(scope, from_project_id="PRJ-B", to_project_id="PRJ-C") is True


def test_transfer_permits_empty_frozenset_permits_nothing():
    scope = Scope(user_id="u", project_ids=frozenset())
    assert rls.transfer_permits(scope, from_project_id="PRJ-A", to_project_id="PRJ-A") is False


def test_version_cell_permits_requires_both_reach_paths():
    scope = Scope(user_id="u", project_ids=frozenset({"PRJ-A"}))
    assert rls.version_cell_permits(
        scope, version_project_id="PRJ-A", wbs_project_id="PRJ-A") is True
    # A snapshot row carrying a foreign wbs_id riding in on its parent
    # version's visibility -- the shape the missing FK in 003 permits.
    assert rls.version_cell_permits(
        scope, version_project_id="PRJ-A", wbs_project_id="PRJ-B") is False
    assert rls.version_cell_permits(
        scope, version_project_id="PRJ-B", wbs_project_id="PRJ-A") is False
    assert rls.version_cell_permits(
        scope, version_project_id="PRJ-A", wbs_project_id=None) is False


def test_reference_permits_denies_a_session_with_no_principal():
    """The whole point of giving organisation-wide tables a policy: an
    unscoped connection reads nothing. An empty `user_id` is the Python-side
    equivalent of `capex.user_id` being absent."""
    assert rls.reference_permits(Scope(user_id="")) is False


def test_reference_permits_admits_any_established_principal():
    """Reference data is not narrowed by dimension grants -- an item is an
    item estate-wide. A principal restricted to nothing on every dimension
    still reads the item master."""
    scope = Scope(
        user_id="U-1",
        entity_ids=frozenset(), plant_ids=frozenset(),
        project_ids=frozenset(), location_ids=frozenset())
    assert rls.reference_permits(scope) is True
    assert rls.reference_permits(Scope(user_id="svc", read_all=True)) is True


def test_reference_tables_are_registered_as_reference_not_as_unfiltered():
    """`RLS_COVERAGE_TABLE_COLUMNS` records both master tables with every
    dimension `None`, which on its own is indistinguishable from "nobody
    mapped the columns". `REFERENCE_TABLES` is what makes the distinction
    explicit."""
    for table in ("item_master", "vendor_master"):
        assert table in rls.REFERENCE_TABLES
        assert set(rls.RLS_COVERAGE_TABLE_COLUMNS[table].values()) == {None}


def test_joined_registries_cover_every_dimensionless_non_reference_table():
    """Any 006 table with no dimension column must be accounted for by a join
    registry or by REFERENCE_TABLES -- otherwise it is an unexplained blank."""
    for table, columns in rls.RLS_COVERAGE_TABLE_COLUMNS.items():
        if set(columns.values()) != {None}:
            continue
        assert (table in rls.JOINED_VIA_WBS_ELEMENT
                or table in rls.JOINED_VIA_BUDGET_VERSION
                or table in rls.REFERENCE_TABLES), (
            f"{table} maps no dimension column and is in no join or reference "
            f"registry -- is it filtered at all?")


# ================================================================ live fixture
_ORG = "ORG-COV"
_ENT_A, _ENT_B = "ENT-COV-A", "ENT-COV-B"
_PLT_A, _PLT_B = "PLT-COV-A", "PLT-COV-B"
_LOC_A, _LOC_B = "LOC-COV-A", "LOC-COV-B"
_PRJ_A, _PRJ_B = "PRJ-COV-A", "PRJ-COV-B"
_WBS_A, _WBS_B = "WBS-COV-A", "WBS-COV-B"
_BH_A1, _BH_A2 = "BH-COV-A1", "BH-COV-A2"
_BH_B1 = "BH-COV-B1"

#: `{transfer_id: (from_project, to_project)}` for the pure-Python oracle.
_TRANSFERS = {
    "TRF-COV-AA": (_PRJ_A, _PRJ_A),   # wholly inside project A
    "TRF-COV-BB": (_PRJ_B, _PRJ_B),   # wholly inside project B
    "TRF-COV-AB": (_PRJ_A, _PRJ_B),   # crosses -- visible to NEITHER scope
}


def _seed(session: Session) -> None:
    """Two independent entity/plant/location/project/wbs chains, plus one row
    in every table 006 protects, so a scope restricted to chain A can be shown
    never to reach chain B -- and a cross-chain budget transfer never to be
    reachable from either side."""
    ex = session.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s, 'COV', 'Coverage Org', 'TEST', 'TEST')", (_ORG,))
    for ent in (_ENT_A, _ENT_B):
        ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
           "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (ent, _ORG, ent, ent))
    for plant, ent in ((_PLT_A, _ENT_A), (_PLT_B, _ENT_B)):
        ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, updated_by) "
           "VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (plant, ent, plant, plant))
    for loc, ent, plant in ((_LOC_A, _ENT_A, _PLT_A), (_LOC_B, _ENT_B, _PLT_B)):
        ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
           "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (loc, ent, plant, loc, loc))
    for prj, ent, plant, loc in ((_PRJ_A, _ENT_A, _PLT_A, _LOC_A),
                                 (_PRJ_B, _ENT_B, _PLT_B, _LOC_B)):
        ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, capex_code, "
           "name, created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (prj, ent, plant, loc, prj, prj))
    for wbs, prj in ((_WBS_A, _PRJ_A), (_WBS_B, _PRJ_B)):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
           "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (wbs, prj, wbs, wbs, wbs.lower().replace("-", "_")))
    for head, ent in ((_BH_A1, _ENT_A), (_BH_A2, _ENT_A), (_BH_B1, _ENT_B)):
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, "
           "updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')", (head, ent, head, head))
    cells = ((_WBS_A, _BH_A1), (_WBS_A, _BH_A2), (_WBS_B, _BH_B1))
    for wbs, head in cells:
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
           "VALUES (%s, %s, 100000, 'TEST')", (wbs, head))

    # accounting_period -- entity_id direct.
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
       "created_by) VALUES ('AP-COV-A', %s, DATE '2026-04-01', DATE '2026-04-30', 'TEST')",
       (_ENT_A,))
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, "
       "created_by) VALUES ('AP-COV-B', %s, DATE '2026-04-01', DATE '2026-04-30', 'TEST')",
       (_ENT_B,))

    # budget_line -- wbs_id -> wbs_element.project_id.
    for line_id, wbs, head in (("BL-COV-A", _WBS_A, _BH_A1), ("BL-COV-B", _WBS_B, _BH_B1)):
        ex("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, "
           "amount_paise, effective_from, created_by, updated_by) "
           "VALUES (%s, %s, %s, 'ORIGINAL', 100000, DATE '2026-04-01', 'TEST', 'TEST')",
           (line_id, wbs, head))

    # budget_revision -- same reach.
    for rev_id, wbs, head in (("REV-COV-A", _WBS_A, _BH_A1), ("REV-COV-B", _WBS_B, _BH_B1)):
        ex("INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, delta_paise, "
           "effective_from, justification, created_by) "
           "VALUES (%s, %s, %s, 5000, DATE '2026-04-01', 'coverage fixture', 'TEST')",
           (rev_id, wbs, head))

    # budget_transfer needs two DISTINCT cells per document
    # (ck_budget_transfer_distinct_cells), so chain B gets a second head and
    # cell exactly as chain A already has.
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, "
       "updated_by) VALUES ('BH-COV-B2', %s, 'BH-COV-B2', 'BH-COV-B2', 'TEST', 'TEST')",
       (_ENT_B,))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
       "VALUES (%s, 'BH-COV-B2', 100000, 'TEST')", (_WBS_B,))
    # The AB row crosses projects -- visible to NEITHER side under AND-semantics.
    legs = {
        "TRF-COV-AA": (_WBS_A, _BH_A1, _WBS_A, _BH_A2),
        "TRF-COV-BB": (_WBS_B, _BH_B1, _WBS_B, "BH-COV-B2"),
        "TRF-COV-AB": (_WBS_A, _BH_A1, _WBS_B, _BH_B1),
    }
    for transfer_id, (fw, fh, tw, th) in legs.items():
        ex("INSERT INTO budget_transfer (transfer_id, from_wbs_id, from_head_id, to_wbs_id, "
           "to_head_id, amount_paise, effective_from, justification, created_by) "
           "VALUES (%s, %s, %s, %s, %s, 5000, DATE '2026-04-01', 'coverage fixture', 'TEST')",
           (transfer_id, fw, fh, tw, th))

    # budget_version / budget_version_cell.
    for ver_id, prj in (("VER-COV-A", _PRJ_A), ("VER-COV-B", _PRJ_B)):
        ex("INSERT INTO budget_version (version_id, project_id, version_no, label, created_by) "
           "VALUES (%s, %s, 1, %s, 'TEST')", (ver_id, prj, ver_id))
    for ver_id, wbs, head in (("VER-COV-A", _WBS_A, _BH_A1), ("VER-COV-B", _WBS_B, _BH_B1)):
        ex("INSERT INTO budget_version_cell (version_id, wbs_id, budget_head_id, budget_paise) "
           "VALUES (%s, %s, %s, 100000)", (ver_id, wbs, head))

    # Organisation-wide reference data -- no chain, by design.
    ex("INSERT INTO item_master (item_id, code, name, created_by, updated_by) "
       "VALUES ('ITEM-COV-1', 'ITEM-COV-1', 'Coverage item', 'TEST', 'TEST')")
    ex("INSERT INTO vendor_master (vendor_id, code, name, created_by, updated_by) "
       "VALUES ('VEND-COV-1', 'VEND-COV-1', 'Coverage vendor', 'TEST', 'TEST')")


def _scope_a() -> Scope:
    """Restricted to chain A on entity AND project -- the two dimensions 006's
    policies actually filter on. Plant and location are left unrestricted so
    the tables that waive them are not accidentally testing a different rule.
    """
    return Scope(
        user_id="U-COV", principal_kind="USER",
        entity_ids=frozenset({_ENT_A}),
        project_ids=frozenset({_PRJ_A}),
        plant_ids=None, location_ids=None, read_all=False)


#: `SELECT` statements carrying NO scope predicate whatsoever, one per newly
#: covered table, with the ids that chain A's scope must and must not see.
#: These are the RAW queries the RLS-alone test runs: if RLS were absent or
#: inert, each returns both rows.
_RAW_CASES: dict[str, tuple[str, set[str], set[str]]] = {
    "accounting_period": (
        "SELECT period_id FROM accounting_period", {"AP-COV-A"}, {"AP-COV-B"}),
    "budget_line": (
        "SELECT budget_line_id FROM budget_line", {"BL-COV-A"}, {"BL-COV-B"}),
    "budget_revision": (
        "SELECT revision_id FROM budget_revision", {"REV-COV-A"}, {"REV-COV-B"}),
    "budget_transfer": (
        "SELECT transfer_id FROM budget_transfer",
        {"TRF-COV-AA"}, {"TRF-COV-BB", "TRF-COV-AB"}),
    "budget_version": (
        "SELECT version_id FROM budget_version", {"VER-COV-A"}, {"VER-COV-B"}),
    "budget_version_cell": (
        "SELECT version_id FROM budget_version_cell", {"VER-COV-A"}, {"VER-COV-B"}),
}


# ============================================================== live: RLS alone
@pytest.mark.pg
@PG
def test_rls_alone_denies_out_of_scope_rows_with_no_application_predicate_live(
        pg_database, pg_connection):
    """**The important case.** Bypass the application layer entirely.

    Connect as the restricted `capex_app` role, apply the session scope
    settings directly, and run a RAW `SELECT` with no `{scope}` token, no
    `repo.compile_scope` predicate, no `WHERE` clause at all -- the shape a
    developer who forgot `repo.query()` produces. RLS must still hide chain B.

    Before 006 every one of these statements returned both rows.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    scope = _scope_a()
    with rls.scoped_transaction(pg_connection, scope):
        for table, (statement, visible, hidden) in _RAW_CASES.items():
            seen = {row[0] for row in pg_connection.execute(statement).fetchall()}
            assert hidden.isdisjoint(seen), (
                f"{table}: RLS alone leaked out-of-scope rows {sorted(hidden & seen)} "
                f"to a raw query carrying no application predicate")
            assert visible <= seen, (
                f"{table}: RLS alone hid IN-scope rows {sorted(visible - seen)}; a "
                f"backstop that over-denies is a different bug, not a safe one")


@pytest.mark.pg
@PG
def test_rls_alone_hides_the_cross_project_transfer_from_both_sides_live(
        pg_database, pg_connection):
    """`budget_transfer_scope` uses AND, not OR. A caller who can see only the
    source project must not learn that the destination project exists, holds
    budget, or received any."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    for label, project, entity in (("A", _PRJ_A, _ENT_A), ("B", _PRJ_B, _ENT_B)):
        scope = Scope(user_id="U-COV", entity_ids=frozenset({entity}),
                      project_ids=frozenset({project}))
        with rls.scoped_transaction(pg_connection, scope):
            seen = {row[0] for row in pg_connection.execute(
                "SELECT transfer_id FROM budget_transfer").fetchall()}
        assert "TRF-COV-AB" not in seen, (
            f"scope {label} saw the cross-project transfer; OR-semantics would "
            f"disclose the far leg's project")


@pytest.mark.pg
@PG
def test_an_unscoped_capex_app_session_reads_nothing_live(pg_database, pg_connection):
    """Fail closed. A `capex_app` session that applies NO `capex.*` settings --
    a pooled connection whose `SET LOCAL` was rolled back, or a background job
    that never opened a scoped session -- must read nothing, from the
    dimensioned tables AND from the organisation-wide reference tables.

    The reference tables are the ones this could most easily have got wrong:
    `capex_scope_permits(NULL, NULL, NULL, NULL)` would return TRUE here.

    The fixture is seeded FIRST and every table proven non-empty as the
    superuser before the role switch. Without that, "reads nothing" would be
    satisfied by the tables simply being empty and the test would assert
    nothing at all.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    try:
        populated = {
            table: pg_connection.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in rls.RLS_COVERAGE_TABLES
        }
        empty = sorted(t for t, n in populated.items() if n == 0)
        assert empty == [], (
            f"fixture did not populate {empty}; 'an unscoped session reads "
            f"nothing' would be vacuously true for those tables")

        pg_connection.execute("SET LOCAL ROLE capex_app")
        for table in rls.RLS_COVERAGE_TABLES:
            count = pg_connection.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, (
                f"{table}: an unscoped capex_app session read {count} of "
                f"{populated[table]} rows. RLS must fail CLOSED on a session "
                f"that never applied a scope.")
    finally:
        pg_connection.rollback()


@pytest.mark.pg
@PG
def test_reference_tables_are_readable_by_an_established_principal_live(
        pg_database, pg_connection):
    """The converse of the test above: organisation-wide really does mean
    every principal sees the rows, including one restricted to nothing on
    every dimension. Otherwise the policy is not "organisation-wide", it is
    an outage."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    scope = Scope(
        user_id="U-COV", entity_ids=frozenset(), plant_ids=frozenset(),
        project_ids=frozenset(), location_ids=frozenset())
    assert rls.reference_permits(scope) is True
    with rls.scoped_transaction(pg_connection, scope):
        items = pg_connection.execute("SELECT item_id FROM item_master").fetchall()
        vendors = pg_connection.execute("SELECT vendor_id FROM vendor_master").fetchall()
    assert [row[0] for row in items] == ["ITEM-COV-1"]
    assert [row[0] for row in vendors] == ["VEND-COV-1"]


# =============================================== live: application layer alone
@pytest.mark.pg
@PG
def test_application_predicate_alone_denies_the_same_rows_live(pg_database):
    """The converse bypass: RLS is NOT the filter here.

    `pg_database` connects as the fixture's admin role, which is a superuser in
    CI and therefore bypasses row-level security unconditionally (see
    `rls.py`'s module docstring). Every statement below is filtered ONLY by
    `repo.compile_scope`. It must hide exactly what RLS hid.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    scope = _scope_a()
    waived = {"entity": None, "plant": None, "location": None}
    with pg_database.session(scope) as session:
        # Prove RLS really is out of the picture for this connection, so a
        # pass here cannot be RLS quietly doing the work.
        unfiltered = session.fetchall("SELECT count(*) FROM budget_line")[0][0]
        assert unfiltered == 2, (
            "expected the admin fixture role to bypass RLS and see both chains; "
            f"saw {unfiltered}. This test would otherwise prove nothing about "
            "the application layer.")

        periods = {row[0] for row in repo.query(
            session, "SELECT period_id FROM accounting_period WHERE {scope}",
            columns={"entity": "entity_id", "plant": None, "location": None,
                     "project": None})}
        assert periods == {"AP-COV-A"}

        lines = {row[0] for row in repo.query(
            session,
            "SELECT bl.budget_line_id FROM budget_line bl "
            "JOIN wbs_element we ON we.wbs_id = bl.wbs_id WHERE {scope}",
            columns={"project": "we.project_id", **waived})}
        assert lines == {"BL-COV-A"}

        revisions = {row[0] for row in repo.query(
            session,
            "SELECT br.revision_id FROM budget_revision br "
            "JOIN wbs_element we ON we.wbs_id = br.wbs_id WHERE {scope}",
            columns={"project": "we.project_id", **waived})}
        assert revisions == {"REV-COV-A"}

        versions = {row[0] for row in repo.query(
            session, "SELECT version_id FROM budget_version WHERE {scope}",
            columns={"project": "project_id", **waived})}
        assert versions == {"VER-COV-A"}

        # budget_transfer's rule is BOTH legs -- one `compile_scope` predicate
        # maps one column per dimension, so the application layer expresses it
        # as the intersection of the two legs' visible sets. Same rule, same
        # answer as the single SQL policy.
        from_ok = {row[0] for row in repo.query(
            session,
            "SELECT bt.transfer_id FROM budget_transfer bt "
            "JOIN wbs_element we ON we.wbs_id = bt.from_wbs_id WHERE {scope}",
            columns={"project": "we.project_id", **waived})}
        to_ok = {row[0] for row in repo.query(
            session,
            "SELECT bt.transfer_id FROM budget_transfer bt "
            "JOIN wbs_element we ON we.wbs_id = bt.to_wbs_id WHERE {scope}",
            columns={"project": "we.project_id", **waived})}
        assert from_ok & to_ok == {"TRF-COV-AA"}

        # budget_version_cell: both reach paths, intersected, as above.
        via_version = {row[0] for row in repo.query(
            session,
            "SELECT bvc.version_id FROM budget_version_cell bvc "
            "JOIN budget_version bv ON bv.version_id = bvc.version_id WHERE {scope}",
            columns={"project": "bv.project_id", **waived})}
        via_wbs = {row[0] for row in repo.query(
            session,
            "SELECT bvc.version_id FROM budget_version_cell bvc "
            "JOIN wbs_element we ON we.wbs_id = bvc.wbs_id WHERE {scope}",
            columns={"project": "we.project_id", **waived})}
        assert via_version & via_wbs == {"VER-COV-A"}


@pytest.mark.pg
@PG
def test_both_layers_agree_with_the_pure_python_oracle_live(pg_database, pg_connection):
    """Neither implementation is assumed correct. `rls.permits` and its 006
    siblings compute the expected sets in pure Python from the fixture's known
    shape; the live RLS-only result must equal them."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    scope = _scope_a()
    expected_transfers = {
        tid for tid, (src, dst) in _TRANSFERS.items()
        if rls.transfer_permits(scope, from_project_id=src, to_project_id=dst)}
    assert expected_transfers == {"TRF-COV-AA"}, "oracle itself is wrong"

    expected_periods = {
        pid for pid, ent in (("AP-COV-A", _ENT_A), ("AP-COV-B", _ENT_B))
        if rls.permits(scope, entity_id=ent)}
    expected_cells = {
        vid for vid, prj in (("VER-COV-A", _PRJ_A), ("VER-COV-B", _PRJ_B))
        if rls.version_cell_permits(scope, version_project_id=prj, wbs_project_id=prj)}

    with rls.scoped_transaction(pg_connection, scope):
        periods = {row[0] for row in pg_connection.execute(
            "SELECT period_id FROM accounting_period").fetchall()}
        transfers = {row[0] for row in pg_connection.execute(
            "SELECT transfer_id FROM budget_transfer").fetchall()}
        cells = {row[0] for row in pg_connection.execute(
            "SELECT version_id FROM budget_version_cell").fetchall()}

    assert periods == expected_periods
    assert transfers == expected_transfers
    assert cells == expected_cells


# ================================================== live: ENABLE, FORCE, policy
@pytest.mark.pg
@PG
def test_rls_is_enabled_and_forced_for_every_newly_covered_table_live(pg_connection):
    """`FORCE` read back from `pg_class`, not from the migration text.

    Without it the table's OWNER bypasses every policy silently -- and in
    production the owner is the deploy identity that ran the migrations, the
    role most likely to be reused by a background job.
    """
    status = rls.fetch_rls_status(pg_connection, rls.RLS_COVERAGE_TABLES)
    pg_connection.rollback()
    for table in rls.RLS_COVERAGE_TABLES:
        assert status[table]["enabled"], f"{table}: RLS not enabled"
        assert status[table]["forced"], (
            f"{table}: RLS enabled but NOT forced -- the table owner bypasses "
            f"every policy on it with no error and no log line")


@pytest.mark.pg
@PG
def test_every_newly_covered_table_actually_carries_a_policy_live(pg_connection):
    """`ENABLE` + `FORCE` with no policy denies everything; `ENABLE` with a
    policy that failed to create denies nothing. Read the policies back."""
    for table in rls.RLS_COVERAGE_TABLES:
        names = rls.fetch_policy_names(pg_connection, table)
        assert names, f"{table}: RLS enabled but no policy exists"
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_all_nineteen_rls_tables_are_enabled_and_forced_live(pg_connection):
    """004's eleven and 006's eight together. Guards against 006 accidentally
    disturbing 004's coverage."""
    status = rls.fetch_rls_status(pg_connection, rls.ALL_RLS_TABLES)
    pg_connection.rollback()
    assert len(status) == 19
    unprotected = sorted(t for t, s in status.items()
                         if not (s["enabled"] and s["forced"]))
    assert unprotected == [], unprotected
