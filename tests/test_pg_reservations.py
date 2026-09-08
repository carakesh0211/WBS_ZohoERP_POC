"""Migration 015: one live reservation per (purchase request x RESOLVED cell).

WHAT IS PROVED HERE, AND WHERE
==============================

Split the way `tests/test_pg_migration_014.py` is, and for the same reason.
The first half runs with NO DATABASE: those properties are properties of the
migration TEXT, of the runner's own adoption parsers, or of this package's
source, and a check whose only coverage is in an environment nobody runs
locally is a check nobody runs. The second half carries `@pytest.mark.pg` plus
a `skipif` on `CAPEX_DB_URL`, SKIPS on every workstation here, and FIRST
EXECUTES IN CI's `pg_tests` job.

A SKIP IS NOT A PASS. Every gated test below says so in the shared skip reason,
because "n skipped" read as "n fine" is exactly how a live-only guard rots.

THE SEVEN LIVE PROPERTIES, AND WHY EACH IS ITS OWN TEST
=======================================================

1. two lines in the SAME resolved cell AGGREGATE into one reservation. Holding
   them separately would put two rows against one pot and let each half be
   checked in isolation -- the over-spend `budget_verdicts` aggregates by
   owning cell to prevent, re-opened one layer down.
2. two lines in DIFFERENT resolved cells produce TWO reservations. This is the
   case migration 014's `UNIQUE (pr_id)` forbade and the feature the decision
   of 2026-09-08 unblocks.
3. insufficient availability in ONE cell ROLLS BACK every reservation in the
   request. All cells or none: a request holding budget for part of itself is
   a hold nobody can reconcile against the document it belongs to.
4. concurrent attempts cannot OVER-RESERVE. Two requests racing the same pot;
   at most one of them may hold money the pot does not have.
5. a RETRY does not duplicate. A replay of the same request finds its own holds
   and reuses them.
6. conversion and release settle each live reservation EXACTLY ONCE, on every
   cell, and re-derive `pr_reserved_paise` on every one of them. A hold that
   stopped holding and was not re-derived REFUSES spending that is genuinely
   available -- quietly, in the direction nobody reports as a bug.
7. THE PREVIOUS DESIGN FAILS THE MULTI-CELL SCENARIO. Asserted against the old
   index shape, rebuilt on the real table inside a transaction that then rolls
   back, so the reason for the change is PROVABLE rather than asserted in a
   commit message.
"""
from __future__ import annotations

import ast
import os
import re
import sys as _sys
import threading
import uuid
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg
# is deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace. Same note as test_pg_migration_014.py.
from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
_015_PATH = MIGRATIONS / "015_reservation_grain.sql"
_015 = _015_PATH.read_text(encoding="utf-8")

#: 015 with its prose removed. Every "the migration does not do X" assertion
#: reads THIS: the trailing `-- ROLLBACK:` block is commented-out DDL, and
#: parsing it as real DDL inverts the meaning of every DROP and CREATE in it.
#: The same device, for the same reason, as `_014_CODE`.
_015_CODE = re.sub(r"--[^\n]*", "", _015)

#: The applied body: everything before the final COMMIT. The ROLLBACK block
#: lives after it and is commented out.
_015_APPLIED = _015_CODE[:_015_CODE.rindex("COMMIT;")]

_SERVICE = (ROOT / "app" / "backend" / "pg"
            / "procurement_services.py").read_text(encoding="utf-8")

#: The one skip reason, so a skipped run reads as a SKIP rather than as a pass.
PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that execute migration 015's grain rather than reading it."),
)


def _migration(version: str) -> migrate_pg.Migration:
    for m in migrate_pg.discover():
        if m.version == version:
            return m
    raise AssertionError(f"migration {version} is not discovered by the runner")


# =========================================================================
# Source-level: the migration is a migration, and it is ADDITIVE
# =========================================================================
def test_015_is_discovered_named_and_numbered():
    versions = [m.version for m in migrate_pg.discover()]
    assert versions == sorted(versions)
    assert versions.count("015") == 1
    assert _migration("015").name == "reservation_grain"
    assert "015_reservation_grain.sql" not in migrate_pg.NON_MIGRATION_FILES


def test_014_is_not_modified_by_this_change():
    """014's checksum is recorded in `schema_migrations` on every database
    where it ran. Editing it makes `assert_schema_current` report drift and
    every existing deployment refuse to boot -- which is why 015 exists at all
    rather than 014 being corrected in place.

    Asserted structurally: 014 must STILL declare the index 015 drops, and must
    still carry the refusal it wrote into its own header. If somebody "fixed"
    014 instead, those declarations would be gone and this fails.
    """
    text = (MIGRATIONS / "014_procurement_corrections.sql").read_text(
        encoding="utf-8")
    assert "CREATE UNIQUE INDEX ux_pr_reservation_live" in text
    assert "MULTI_CELL_RESERVATION_UNSUPPORTED" in text, (
        "014's header reports the contradiction it refused to resolve; "
        "removing that report removes the record of why 015 exists")


def test_015_contains_no_delete_truncate_or_drop_of_a_reservation():
    """"Preserve every historical Released, Converted and Expired reservation"
    is not advice here; it is the rule. A settled reservation IS AUD-H-001's
    evidence, and a migration that tidied it away would delete the proof of the
    control while claiming to strengthen it.

    The ROLLBACK block is commented-out DDL and is excluded, which is the whole
    reason `_015_APPLIED` exists.
    """
    body = _015_APPLIED.upper()
    assert "DELETE FROM" not in body
    assert "TRUNCATE" not in body
    assert "DROP TABLE" not in body
    assert "UPDATE PR_RESERVATION" not in body, (
        "015 rewrites no reservation row. The only row-level write it makes is "
        "the DEFAULT that populates a column which did not exist a statement "
        "ago")


def test_the_new_index_is_over_the_pr_and_the_resolved_cell():
    """The grain itself, read out of the migration rather than trusted.

    `(pr_id, wbs_id, budget_head_id)` and PARTIAL on `state = 'Reserved'`.
    Partial is not decoration: once a reservation is Converted, Released or
    Expired, a later hold on the same request and the same cell is a genuinely
    NEW reservation and must be creatable.
    """
    match = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+ux_pr_reservation_live_cell\s*"
        r"ON\s+pr_reservation\s*\(([^)]*)\)\s*"
        r"WHERE\s+state\s*=\s*'Reserved'",
        _015_CODE)
    assert match is not None, (
        "015 must build ux_pr_reservation_live_cell as a PARTIAL unique index "
        f"on state = 'Reserved'; parsed body was:\n{_015_CODE}")
    columns = [c.strip() for c in match.group(1).split(",")]
    assert columns == ["pr_id", "wbs_id", "budget_head_id"]


def test_the_old_whole_pr_index_is_dropped_not_left_alongside():
    """Leaving 014's index in place would keep refusing the second cell's hold
    and the feature would still be blocked, with two indexes disagreeing about
    the grain and the narrower one silently winning."""
    assert re.search(r"DROP\s+INDEX\s+ux_pr_reservation_live\s*;", _015_CODE), (
        "015 must drop ux_pr_reservation_live explicitly")
    # And it must not be recreated in the applied body -- only in the
    # commented-out ROLLBACK block, which `_015_CODE` has already stripped
    # comments from, so this reads the applied half only.
    assert "CREATE UNIQUE INDEX ux_pr_reservation_live\n" not in _015_APPLIED


def test_the_preflight_runs_before_every_ddl_statement():
    """A preflight that ran after a DDL statement would report on a schema the
    migration had already changed, and would have nothing to refuse."""
    preflight = _015_CODE.index("DO $preflight$")
    for statement in ("ALTER TABLE pr_reservation ADD COLUMN",
                      "CREATE UNIQUE INDEX ux_pr_reservation_live_cell",
                      "DROP INDEX ux_pr_reservation_live"):
        assert _015_CODE.index(statement) > preflight, (
            f"{statement!r} precedes the preflight")


def test_the_preflight_refuses_and_never_resolves():
    """It names offending rows and RAISES. It must not delete, merge,
    renumber or choose between held budgets -- an operator is told which rows
    to fix and fixes them.

    The scan reads STATEMENT FORMS (`DELETE FROM`, `UPDATE <table>`,
    `INSERT INTO`) rather than the bare verbs, because the refusal message
    itself says the words "does not delete, merge or choose" -- and a check
    that cannot tell a promise from a statement would fire on the promise.
    """
    start = _015_CODE.index("DO $preflight$")
    end = _015_CODE.index("$preflight$;", start)
    block = _015_CODE[start:end].upper()
    assert "RAISE EXCEPTION" in block
    assert "DELETE FROM" not in block
    assert "INSERT INTO" not in block
    assert not re.search(r"\bUPDATE\s+[A-Z_]+\s+SET\b", block)


def test_the_preflight_names_the_rows_that_would_block_the_index():
    """A refusal that does not say WHICH rows is a refusal nobody can act on.

    The blocking condition is two live reservations already sharing
    `(pr_id, wbs_id, budget_head_id)`, and the NOTICE must carry the
    reservation ids -- `string_agg` over `reservation_id` is what makes that
    true rather than intended.
    """
    start = _015_CODE.index("DO $preflight$")
    end = _015_CODE.index("$preflight$;", start)
    block = _015_CODE[start:end]
    assert "GROUP BY r.pr_id, r.wbs_id, r.budget_head_id" in block
    assert "HAVING count(*) > 1" in block
    assert "string_agg(r.reservation_id" in block


def test_every_sum_over_bigint_money_is_cast_back_to_bigint():
    """`SUM()` over `bigint` returns NUMERIC in PostgreSQL, which reaches
    psycopg as `decimal.Decimal`. Money leaves this database as an integer
    number of paise or it does not leave it."""
    for match in re.finditer(r"SUM\([^)]*paise[^)]*\)", _015_CODE):
        tail = _015_CODE[match.end():match.end() + 20]
        assert "::bigint" in tail, (
            f"{match.group(0)} is not cast back to bigint; it would arrive as "
            f"a Decimal")


def test_the_migration_names_its_grant_and_its_revoke_for_capex_app():
    """004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT ISSUED IT, so a
    deployment whose 015 is applied by a different identity than its 004 would
    otherwise be left guessing. And DELETE is revoked for 014's reason: a
    released reservation stops holding budget because `state <> 'Reserved'`,
    never because it stopped existing."""
    assert re.search(
        r"GRANT\s+SELECT,\s*INSERT,\s*UPDATE\s+ON\s+pr_reservation\s+TO\s+capex_app",
        _015_CODE)
    assert re.search(
        r"REVOKE\s+DELETE\s+ON\s+pr_reservation\s+FROM\s+capex_app", _015_CODE)


def test_the_migration_ends_with_a_commented_rollback():
    """House style, and it is not ceremony: the revert path for a grain change
    has to say what happens to the extra holds the new grain permitted."""
    assert _015.rstrip().endswith("COMMIT;")
    assert "-- ROLLBACK:" in _015
    rollback = _015[_015.index("-- ROLLBACK:"):]
    assert "DROP INDEX IF EXISTS ux_pr_reservation_live_cell;" in rollback
    assert "do NOT delete them" in rollback


# =========================================================================
# Source-level: the RUNNER'S adoption verification is extended to 015
# =========================================================================
def test_the_runner_verifies_both_of_015s_indexes():
    """Failure class C. A partial UNIQUE index is not a table constraint, so
    `_named_constraints_by` cannot see it; a legacy dump missing it adopts
    cleanly under the table checks and then admits the duplicate hold the index
    existed to refuse. `_indexes_created_by` is what closes that, and this is
    the assertion that it actually sees 015's."""
    indexes = migrate_pg._indexes_created_by(_migration("015"))
    assert ("ux_pr_reservation_live_cell", "pr_reservation") in indexes
    assert ("ix_pr_reservation_live_by_pr", "pr_reservation") in indexes
    assert ("ux_pr_reservation_live", "pr_reservation") not in indexes, (
        "the ROLLBACK block's commented CREATE must not be parsed as DDL a "
        "database has to carry -- 015 DROPS that index")


def test_the_runner_verifies_015s_added_column_and_named_check():
    """Failure class E. Every CREATE-TABLE-only parser is blind to a corrective
    migration, and 015 is almost entirely ALTERs. `cell_grain` is the column
    that distinguishes a line-grain hold from a resolved one, and a database
    missing it cannot express the approved grain at all."""
    migration = _migration("015")
    assert migrate_pg._added_columns_by(migration) == [
        ("pr_reservation", "cell_grain")]
    assert ("pr_reservation", "ck_pr_reservation_cell_grain") in \
        migrate_pg._added_constraints_by(migration)


def test_015_adds_no_table_no_policy_and_no_rls_statement():
    """It adds no table, so it adds no policy. A SECOND `CREATE POLICY` on
    `pr_reservation` would ADD a permissive alternative to 014's -- widening
    access rather than confirming it, which is the opposite of what restating a
    control is for."""
    migration = _migration("015")
    assert migrate_pg._tables_created_by(migration) == []
    assert migrate_pg._policies_created_by(migration) == []
    assert migrate_pg._rls_tables_by(migration) == ([], [])


def test_015_adds_no_paise_column_so_none_can_be_mis_declared():
    """The `^`-anchored `_PAISE_COLUMN_RE` rule is recorded in 015's header for
    the NEXT migration to touch this table. This asserts the premise: 015
    itself adds no money column, so there is none whose bigint-ness could go
    unverified."""
    assert migrate_pg._paise_columns_by(_migration("015")) == []
    assert not [c for _t, c in migrate_pg._added_columns_by(_migration("015"))
                if c.endswith("_paise")]


# =========================================================================
# Source-level: the service refusal is gone, and what replaced it
# =========================================================================
def test_the_multi_cell_refusal_is_gone_from_the_service():
    """It existed only while the schema could not express the grain, and 015
    now does. Leaving it in place would leave the feature blocked by a string
    rather than by a control.

    The RAISE SITE is what must be gone, and that is what is asserted. The code
    still appears in prose, deliberately: 014's header and this module's own
    comments record what was refused and why, and deleting that record would
    delete the explanation of why migration 015 exists at all.
    """
    assert '_err("MULTI_CELL_RESERVATION_UNSUPPORTED"' not in _SERVICE
    assert "MULTI_CELL_RESERVATION_UNSUPPORTED" in _SERVICE, (
        "the history of the refusal is kept in prose; only the raise is gone")


def test_the_service_still_refuses_a_hold_it_cannot_take():
    """Removing one refusal must not remove the others. All three survive, and
    each names a different way of being told budget was held when nothing holds
    it."""
    for code in ("PR_RESERVATION_NOT_MIGRATED", "RESERVATION_EXCEEDS_BUDGET",
                 "RESERVATION_AMOUNT_CONFLICT", "RESERVATION_GRAIN_CONFLICT"):
        assert code in _SERVICE


def test_the_resolved_cell_is_read_from_the_verdicts_never_re_derived():
    """One resolution, one truth, one pot.

    `budget_verdicts` resolves each line cell's budget-owning ancestor because
    it must -- it checks the aggregated SUM against the right pot.
    `resolved_cell_amounts` reads that answer. A second resolution pass could
    name a DIFFERENT owner than the verdict the request was accepted on, and a
    hold placed on a cell other than the one the check ran against is a hold
    against a budget nobody checked.
    """
    tree = ast.parse(_SERVICE)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "resolved_cell_amounts")
    # The docstring is stripped before the scan. It NAMES `_owning_ancestor`
    # and `check_availability` on purpose -- to say where the resolution
    # actually happens -- and a check that could not tell an explanation from a
    # call would fire on the explanation.
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           ) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "verdict.get('owning_wbs_id')" in code
    for forbidden in ("_owning_ancestor", "check_availability",
                      "_availability", "session"):
        assert forbidden not in code, (
            f"resolved_cell_amounts must not reach {forbidden!r} -- it reads "
            f"the verdicts and derives nothing")


def test_exactly_one_statement_in_this_package_derives_a_ledger_column():
    """The rule migration 014 established and 015 must not break: five of six
    derived ledger columns once had no writer, and a SECOND deriver is how they
    drift apart again. `pr_reserved_paise` is moved by
    `_RECOMPUTE_DERIVED_SQL` and by nothing else."""
    assert _SERVICE.count("pr_reserved_paise = COALESCE") == 1
    assert _SERVICE.count("_RECOMPUTE_DERIVED_SQL = ") == 1


def test_the_insert_targets_the_partial_index_with_its_predicate():
    """An `ON CONFLICT` target that omits the partial index's predicate does
    not infer that index at all -- PostgreSQL raises rather than de-duplicating,
    and the idempotency this test file's property 5 rests on would be a
    coincidence of never having been retried."""
    assert ("ON CONFLICT (pr_id, wbs_id, budget_head_id)\n"
            "                WHERE state = 'Reserved'\n"
            "            DO NOTHING") in _SERVICE


def test_every_reservation_mutation_appends_an_audit_entry():
    """Append-only, inside the transaction, with a server-derived actor. A hold
    that rolls back takes its audit line with it, which is correct: the trail
    records what happened, and nothing happened."""
    for action in ("PR_RESERVATION_HELD", "PR_RESERVATION_SETTLED",
                   "PR_RESERVATION_RELEASED"):
        assert action in _SERVICE


# =========================================================================
# The live half. Everything below needs a real server.
# =========================================================================
def _seed(connection, *, suffix: str, root_budget: int = 10_000_00,
          other_budget: int = 10_000_00) -> dict:
    """Two budget-owning ROOTS, each with two children that own nothing.

    Deliberately this shape and no larger. It is the smallest estate in which
    the grain is observable at all:

        ROOT_A (owns `root_budget`)      ROOT_B (owns `other_budget`)
          |- CHILD_A1 (owns nothing)       |- CHILD_B1 (owns nothing)
          |- CHILD_A2 (owns nothing)

    CHILD_A1 and CHILD_A2 are two DIFFERENT control cells that resolve to the
    SAME pot -- property 1. CHILD_A1 and CHILD_B1 resolve to DIFFERENT pots --
    property 2. A fixture that seeded a plausible estate would hide which row
    each rule acted on.
    """
    ids = {
        "org": f"O_{suffix}", "entity": f"E_{suffix}", "project": f"P_{suffix}",
        "root_a": f"RA_{suffix}", "child_a1": f"CA1_{suffix}",
        "child_a2": f"CA2_{suffix}",
        "root_b": f"RB_{suffix}", "child_b1": f"CB1_{suffix}",
        "head": f"H_{suffix}",
    }
    ex = connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Project','Released','t','t')",
       (ids["project"], ids["entity"], f"C_{suffix}"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Head','t','t')",
       (ids["head"], ids["entity"], f"HC_{suffix}"))

    for root in ("root_a", "root_b"):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, "
           "description, wbs_path, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,'root','Released','t','t')".replace(
               "'root','Released'", "%s,'Released'"),
           (ids[root], ids["project"], ids[root], ids[root], "root"))
    for child, parent in (("child_a1", "root_a"), ("child_a2", "root_a"),
                          ("child_b1", "root_b")):
        ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, "
           "wbs_code, description, wbs_path, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'child',%s,'Released','t','t')",
           (ids[child], ids["project"], ids[parent], ids[child],
            f"{ids[parent]}.{ids[child]}"))

    # The two pots own budget; the four children own none, so every one of them
    # resolves UP to its root. That is the whole point of the fixture.
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, "
       "updated_by) VALUES (%s,%s,%s,'t'), (%s,%s,%s,'t'), "
       "(%s,%s,0,'t'), (%s,%s,0,'t'), (%s,%s,0,'t')",
       (ids["root_a"], ids["head"], root_budget,
        ids["root_b"], ids["head"], other_budget,
        ids["child_a1"], ids["head"], ids["child_a2"], ids["head"],
        ids["child_b1"], ids["head"]))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
       "VALUES (%s,%s,'t'), (%s,%s,'t'), (%s,%s,'t'), (%s,%s,'t'), (%s,%s,'t')",
       (ids["root_a"], ids["head"], ids["root_b"], ids["head"],
        ids["child_a1"], ids["head"], ids["child_a2"], ids["head"],
        ids["child_b1"], ids["head"]))
    connection.commit()
    return ids


def _line(ids: dict, wbs_key: str, amount: int) -> dict:
    return {"wbs_id": ids[wbs_key], "budget_head_id": ids["head"],
            "amount_paise": amount, "quantity": 1,
            "description": f"line on {wbs_key}"}


def _reservations(connection, pr_id: str) -> list[tuple]:
    return connection.execute(
        "SELECT wbs_id, budget_head_id, amount_paise, state, cell_grain "
        "FROM pr_reservation WHERE pr_id = %s "
        "ORDER BY wbs_id, budget_head_id", (pr_id,)).fetchall()


# ----------------------------------------------------------------- property 1
@pytest.mark.pg
@PG
def test_two_lines_in_the_same_resolved_cell_aggregate_into_one_reservation(
        pg_database, pg_connection):
    """PROPERTY 1. CHILD_A1 and CHILD_A2 are two different control cells that
    resolve to the SAME budget-owning ancestor, so they are competing for ONE
    pot and must produce ONE hold for the SUM.

    Two holds against one pot is not a cosmetic difference: each would then be
    checkable in isolation, which is exactly the over-spend `budget_verdicts`
    aggregates by owning cell to prevent -- re-opened one layer down.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00),
                   _line(ids, "child_a2", 200_00)])

    rows = _reservations(pg_connection, created["pr_id"])
    assert len(rows) == 1, (
        f"two lines resolving to {ids['root_a']} must share ONE hold; got "
        f"{rows}")
    assert rows[0][0] == ids["root_a"], (
        "the hold sits on the budget-owning ANCESTOR, not on a line's own cell")
    assert rows[0][2] == 500_00, "the hold is the SUM of both lines"
    assert rows[0][3] == "Reserved"
    assert rows[0][4] == "RESOLVED"
    assert len(created["reservation_ids"]) == 1


# ----------------------------------------------------------------- property 2
@pytest.mark.pg
@PG
def test_two_lines_in_different_resolved_cells_produce_two_reservations(
        pg_database, pg_connection):
    """PROPERTY 2, and the feature itself. CHILD_A1 and CHILD_B1 resolve to
    DIFFERENT pots, so each needs its own hold. This is precisely what 014's
    `UNIQUE (pr_id)` forbade and what `MULTI_CELL_RESERVATION_UNSUPPORTED`
    refused rather than bending."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00),
                   _line(ids, "child_b1", 400_00)])

    rows = _reservations(pg_connection, created["pr_id"])
    assert len(rows) == 2, f"two pots, two holds; got {rows}"
    held = {row[0]: row[2] for row in rows}
    assert held == {ids["root_a"]: 300_00, ids["root_b"]: 400_00}
    assert {row[3] for row in rows} == {"Reserved"}
    assert {row[4] for row in rows} == {"RESOLVED"}
    assert len(created["reservation_ids"]) == 2
    assert created["reserves_budget"] is True


# ----------------------------------------------------------------- property 3
@pytest.mark.pg
@PG
def test_insufficient_availability_in_one_cell_rolls_back_every_reservation(
        pg_database, pg_connection):
    """PROPERTY 3. ROOT_B owns only 100 rupees; the request asks to hold 400
    there and 300 on ROOT_A, which is comfortably affordable.

    ALL CELLS OR NONE. Not the request, not its lines, and not the affordable
    hold: a purchase request holding budget for part of itself is a hold nobody
    can reconcile against the document it belongs to.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix, other_budget=100_00)

    with pytest.raises(svc.ProcurementError) as excinfo:
        with pg_database.session(Scope.system()) as session:
            svc.create_pr(
                session, project_id=ids["project"], actor="U-REQ",
                reserve=True,
                lines=[_line(ids, "child_a1", 300_00),
                       _line(ids, "child_b1", 400_00)])

    assert excinfo.value.code == svc.ERR_RESERVATION_EXCEEDS_BUDGET
    assert excinfo.value.status == 409
    assert ids["root_b"] in str(excinfo.value.message), (
        "the refusal must name the cell that is short")

    assert pg_connection.execute(
        "SELECT count(*) FROM pr_reservation").fetchone()[0] == 0, (
        "the affordable hold on ROOT_A must have rolled back with the request")
    assert pg_connection.execute(
        "SELECT count(*) FROM purchase_request").fetchone()[0] == 0
    assert pg_connection.execute(
        "SELECT count(*) FROM pr_line").fetchone()[0] == 0
    assert pg_connection.execute(
        "SELECT COALESCE(SUM(pr_reserved_paise), 0)::bigint "
        "FROM budget_ledger_cell").fetchone()[0] == 0


# ----------------------------------------------------------------- property 4
@pytest.mark.pg
@PG
def test_concurrent_reservations_cannot_over_reserve_one_pot(
        pg_database, pg_connection):
    """PROPERTY 4. Two requests, each asking to hold 600 of a 1000 pot, racing.

    Exactly one may succeed. The second must observe the first's hold -- which
    means it must read availability INSIDE the cell locks, after the first has
    committed -- and refuse. The pot may never end up holding 1200.

    The first thread takes the cell locks and then WAITS, so the second is
    genuinely racing a held lock rather than being serialised by luck. That is
    the shape `test_pg_budget.py` uses for the same class of race, and the
    reason it uses it: a test that only ever runs the two in sequence proves
    nothing about concurrency.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix, root_budget=1000_00)

    results: dict[str, object] = {}
    first_locked = threading.Event()
    release_first = threading.Event()

    def reserve_first():
        with pg_database.session(Scope.system()) as session:
            from app.backend.pg.locking import lock_affected_cells
            lock_affected_cells(session, [(ids["child_a1"], ids["head"])])
            first_locked.set()
            release_first.wait(timeout=10)
            try:
                results["a"] = svc.create_pr(
                    session, project_id=ids["project"], actor="U-A",
                    reserve=True, lines=[_line(ids, "child_a1", 600_00)])
            except svc.ProcurementError as exc:
                results["a"] = exc

    def reserve_second():
        first_locked.wait(timeout=10)
        with pg_database.session(Scope.system()) as session:
            try:
                results["b"] = svc.create_pr(
                    session, project_id=ids["project"], actor="U-B",
                    reserve=True, lines=[_line(ids, "child_a2", 600_00)])
            except svc.ProcurementError as exc:
                results["b"] = exc

    thread_a = threading.Thread(target=reserve_first)
    thread_b = threading.Thread(target=reserve_second)
    thread_a.start()
    first_locked.wait(timeout=10)
    thread_b.start()
    release_first.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)

    outcomes = [results.get("a"), results.get("b")]
    refused = [o for o in outcomes if isinstance(o, svc.ProcurementError)]
    succeeded = [o for o in outcomes if isinstance(o, dict)]
    assert len(succeeded) == 1, f"exactly one may hold the pot; got {outcomes}"
    assert len(refused) == 1
    assert refused[0].code == svc.ERR_RESERVATION_EXCEEDS_BUDGET

    held = pg_connection.execute(
        "SELECT COALESCE(SUM(amount_paise), 0)::bigint FROM pr_reservation "
        "WHERE state = 'Reserved'").fetchone()[0]
    assert held == 600_00, (
        f"{held} paise is held against a 100000 paise pot; two 60% holds both "
        f"landed and the control did not fire")

    with pg_database.session(Scope.system()) as session:
        available = budget_svc.check_availability(
            session, ids["root_a"], ids["head"], 0)["available_paise"]
    assert available == 400_00


# ----------------------------------------------------------------- property 5
@pytest.mark.pg
@PG
def test_a_retry_reuses_its_own_holds_and_does_not_duplicate_them(
        pg_database, pg_connection):
    """PROPERTY 5. The same request, replayed. It must find its own holds and
    reuse them -- not write a second set, and not move the first.

    `ON CONFLICT ... DO NOTHING` against `ux_pr_reservation_live_cell` plus a
    read-back is what makes that true. The second half of the test is the other
    side of the same rule: a replay carrying a DIFFERENT amount is NOT a retry,
    and is refused rather than silently moving the hold. Quietly updating it
    would change how much budget is held with no record that it moved.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00),
                   _line(ids, "child_b1", 400_00)])
    pr_id = created["pr_id"]
    first = sorted(created["reservation_ids"])

    resolved = {(ids["root_a"], ids["head"]): 300_00,
                (ids["root_b"], ids["head"]): 400_00}

    with pg_database.session(Scope.system()) as session:
        replayed = svc.reserve_pr_cells(
            session, pr_id=pr_id, project_id=ids["project"],
            resolved_amounts=resolved, actor="U-REQ")

    assert sorted(replayed) == first, (
        "a retry must return the holds it already has, not new ones")
    assert len(_reservations(pg_connection, pr_id)) == 2, (
        "the retry duplicated a hold")

    # ...and a replay of a DIFFERENT amount is not a replay.
    with pytest.raises(svc.ProcurementError) as excinfo:
        with pg_database.session(Scope.system()) as session:
            svc.reserve_pr_cells(
                session, pr_id=pr_id, project_id=ids["project"],
                resolved_amounts={(ids["root_a"], ids["head"]): 999_00},
                actor="U-REQ")
    assert excinfo.value.code == svc.ERR_RESERVATION_AMOUNT_CONFLICT
    still_held = {row[0]: row[2] for row in _reservations(pg_connection, pr_id)}
    assert still_held == {ids["root_a"]: 300_00, ids["root_b"]: 400_00}, (
        "the refused replay must not have moved the existing hold")


# ----------------------------------------------------------------- property 6
@pytest.mark.pg
@PG
def test_conversion_settles_every_cell_exactly_once(pg_database, pg_connection):
    """PROPERTY 6a. A two-cell request converts, and BOTH holds settle to
    Converted against the new purchase order -- exactly once each.

    A converted request whose hold stays `Reserved` is counted TWICE against
    the same budget: once as `pr_reserved_paise` and again as the new order's
    `commitment_paise`. So `pr_reserved_paise` must be back to zero on BOTH
    cells afterwards, which only happens if the settlement re-derived both.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00),
                   _line(ids, "child_b1", 400_00)])
    pr_id = created["pr_id"]

    with pg_database.session(Scope.system()) as session:
        svc.submit_pr(session, pr_id=pr_id, actor="U-REQ")
    with pg_database.session(Scope.system()) as session:
        svc.approve_pr(session, pr_id=pr_id, actor="U-APPROVER")
    with pg_database.session(Scope.system()) as session:
        converted = svc.convert_pr_to_po(
            session, pr_id=pr_id, actor="U-BUYER", vendor_name="V")

    rows = _reservations(pg_connection, pr_id)
    assert len(rows) == 2, "no hold was created or destroyed by the conversion"
    assert {row[3] for row in rows} == {"Converted"}, (
        f"every live hold must settle exactly once on conversion; got {rows}")
    po_ids = {row[0] for row in pg_connection.execute(
        "SELECT po_id FROM pr_reservation WHERE pr_id = %s", (pr_id,)).fetchall()}
    assert po_ids == {converted["po_id"]}, (
        "a Converted reservation names the purchase order that consumed it")

    reserved = pg_connection.execute(
        "SELECT COALESCE(SUM(pr_reserved_paise), 0)::bigint "
        "FROM budget_ledger_cell").fetchone()[0]
    assert reserved == 0, (
        f"{reserved} paise is still held after conversion; the same money is "
        f"counted as both a reservation and a commitment")

    # Exactly once: settling again moves nothing.
    with pg_database.session(Scope.system()) as session:
        again = svc.settle_reservations(
            session, pr_id=pr_id, state="Released", actor="U-BUYER")
    assert again == [], "a second settlement must match zero rows"
    assert {row[3] for row in _reservations(pg_connection, pr_id)} == {
        "Converted"}


@pytest.mark.pg
@PG
def test_release_frees_every_cell_exactly_once_and_re_derives_both(
        pg_database, pg_connection):
    """PROPERTY 6b. Rejection, cancellation and expiry take the same path, and
    it must free EVERY cell and re-derive EVERY one of them.

    A hold that stopped holding and was not re-derived leaves
    `pr_reserved_paise` still subtracting it, which REFUSES spending that is
    genuinely available -- quietly, and in the direction nobody reports as a
    bug. Availability is read back here rather than the column, because
    availability is the thing a requestor actually experiences.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        before_a = budget_svc.check_availability(
            session, ids["root_a"], ids["head"], 0)["available_paise"]
        before_b = budget_svc.check_availability(
            session, ids["root_b"], ids["head"], 0)["available_paise"]
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00),
                   _line(ids, "child_b1", 400_00)])
    pr_id = created["pr_id"]

    with pg_database.session(Scope.system()) as session:
        during_a = budget_svc.check_availability(
            session, ids["root_a"], ids["head"], 0)["available_paise"]
    assert during_a == before_a - 300_00, (
        "the hold took nothing out of availability, so a second requestor "
        "passes against the same rupees")

    with pg_database.session(Scope.system()) as session:
        freed = svc.release_reservations_for_pr(
            session, pr_id=pr_id, actor="U-APPROVER",
            reason="rejected at review")
    assert sorted(freed) == sorted([(ids["root_a"], ids["head"]),
                                    (ids["root_b"], ids["head"])])

    with pg_database.session(Scope.system()) as session:
        after_a = budget_svc.check_availability(
            session, ids["root_a"], ids["head"], 0)["available_paise"]
        after_b = budget_svc.check_availability(
            session, ids["root_b"], ids["head"], 0)["available_paise"]
    assert (after_a, after_b) == (before_a, before_b), (
        "both cells must be re-derived; a released hold that nothing "
        "re-derives keeps refusing spending that is available")

    # Exactly once. A second release frees nothing and is not an error.
    with pg_database.session(Scope.system()) as session:
        assert svc.release_reservations_for_pr(
            session, pr_id=pr_id, actor="U-APPROVER") == []

    states = [row[3] for row in _reservations(pg_connection, pr_id)]
    assert states == ["Released", "Released"], (
        "a released reservation is UPDATEd, never deleted -- AUD-H-001 is a "
        "claim about a row's history and a deleted row has none")


# ----------------------------------------------------------------- property 7
@pytest.mark.pg
@PG
def test_the_previous_whole_pr_index_fails_the_multi_cell_scenario(
        pg_database, pg_connection):
    """PROPERTY 7, and the reason this migration exists -- PROVED, not asserted.

    014's design was `UNIQUE (pr_id) WHERE state = 'Reserved'`. This rebuilds
    exactly that index on the real table, inside a transaction, and runs the
    real two-pot request against it. It must FAIL with a unique violation: two
    pots need two holds and that index permits one.

    The transaction rolls back, taking the replica index with it, and the
    SECOND half then runs the identical request under 015's grain and succeeds.
    One test, two runs, one difference -- which is what makes the change's
    justification checkable rather than a claim in a commit message.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    lines = [_line(ids, "child_a1", 300_00), _line(ids, "child_b1", 400_00)]

    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg_database.session(Scope.system()) as session:
            # 014's index, verbatim, rebuilt under a name of its own so the
            # real one is untouched. Transaction-scoped: the rollback below
            # removes it.
            session.execute(
                "CREATE UNIQUE INDEX ux_pr_reservation_live_replica_014 "
                "ON pr_reservation (pr_id) WHERE state = 'Reserved'")
            svc.create_pr(session, project_id=ids["project"], actor="U-REQ",
                          reserve=True, lines=lines)

    assert pg_connection.execute(
        "SELECT count(*) FROM pg_class WHERE relname = "
        "'ux_pr_reservation_live_replica_014'").fetchone()[0] == 0, (
        "the replica index must have rolled back with its transaction")
    assert pg_connection.execute(
        "SELECT count(*) FROM pr_reservation").fetchone()[0] == 0

    # The same request, the same rows, 015's grain. It succeeds.
    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(session, project_id=ids["project"],
                                actor="U-REQ", reserve=True, lines=lines)
    assert len(_reservations(pg_connection, created["pr_id"])) == 2


@pytest.mark.pg
@PG
def test_the_shipped_index_is_the_resolved_cell_one_and_the_old_one_is_gone(
        pg_connection):
    """The premise of property 7, checked against the live schema rather than
    against the migration text. `ux_pr_reservation_live` must be absent and
    `ux_pr_reservation_live_cell` must be present WITH its predicate -- an
    index built without the `WHERE state = 'Reserved'` clause would refuse a
    second hold after the first was released, which the partial one permits by
    design."""
    definitions = dict(pg_connection.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'pr_reservation'").fetchall())
    assert "ux_pr_reservation_live" not in definitions, (
        "015 drops it; leaving it alongside would keep refusing the second "
        "cell's hold, with the narrower index silently winning")
    assert "ux_pr_reservation_live_cell" in definitions
    definition = definitions["ux_pr_reservation_live_cell"]
    assert "UNIQUE" in definition
    for column in ("pr_id", "wbs_id", "budget_head_id"):
        assert column in definition
    assert "state = 'Reserved'" in definition


@pytest.mark.pg
@PG
def test_a_settled_reservation_does_not_block_a_later_hold_on_the_same_cell(
        pg_database, pg_connection):
    """PARTIAL is not decoration. Once a hold is Released, a later hold on the
    same request and the same cell is a genuinely NEW reservation and must be
    creatable -- and the settled row stays exactly where it is."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ", reserve=True,
            lines=[_line(ids, "child_a1", 300_00)])
    pr_id = created["pr_id"]

    with pg_database.session(Scope.system()) as session:
        svc.release_reservations_for_pr(session, pr_id=pr_id, actor="U-APP")
    with pg_database.session(Scope.system()) as session:
        svc.reserve_pr_cells(
            session, pr_id=pr_id, project_id=ids["project"],
            resolved_amounts={(ids["root_a"], ids["head"]): 250_00},
            actor="U-REQ")

    rows = _reservations(pg_connection, pr_id)
    assert sorted((row[2], row[3]) for row in rows) == [
        (250_00, "Reserved"), (300_00, "Released")], (
        "the released hold must survive unchanged beside the new one")


@pytest.mark.pg
@PG
def test_a_live_line_grain_hold_refuses_a_resolved_hold_beside_it(
        pg_database, pg_connection):
    """The overlap the unique index CANNOT see, refused by the service.

    A live 014-grain hold sits on a LINE's cell -- a descendant of the owner. A
    resolved hold sits on the owner. Those are two rows at two DIFFERENT keys
    holding the SAME money twice, so `ux_pr_reservation_live_cell` has nothing
    to object to. `cell_grain` is what makes the refusal possible.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ",
            lines=[_line(ids, "child_a1", 300_00)])
        # The 014-grain primitive, on the LINE's own cell, exactly as it was
        # called before migration 015.
        svc.create_reservation(
            session, pr_id=created["pr_id"], project_id=ids["project"],
            wbs_id=ids["child_a1"], budget_head_id=ids["head"],
            amount_paise=300_00, actor="U-REQ")

    assert _reservations(pg_connection, created["pr_id"])[0][4] == "LINE", (
        "create_reservation writes the cell it is GIVEN, so its rows are "
        "LINE-grain -- which is what every row written before 015 is")

    with pytest.raises(svc.ProcurementError) as excinfo:
        with pg_database.session(Scope.system()) as session:
            svc.reserve_pr_cells(
                session, pr_id=created["pr_id"], project_id=ids["project"],
                resolved_amounts={(ids["root_a"], ids["head"]): 300_00},
                actor="U-REQ")
    assert excinfo.value.code == svc.ERR_RESERVATION_GRAIN_CONFLICT
    assert len(_reservations(pg_connection, created["pr_id"])) == 1


@pytest.mark.pg
@PG
def test_historical_settled_reservations_survive_the_migration(
        pg_database, pg_connection):
    """"Preserve every historical Released, Converted and Expired reservation"
    checked against a live database rather than against the migration text.

    The rows are written at 015's shape and then read back with their grain,
    their settlement identity and their amounts intact. `cell_grain` defaults
    to LINE for a row the resolved path did not write, which is the truthful
    answer for every row that existed when 015 ran.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = svc.create_pr(
            session, project_id=ids["project"], actor="U-REQ",
            lines=[_line(ids, "child_a1", 100_00)])
    pr_id = created["pr_id"]

    for state in ("Released", "Expired"):
        with pg_database.session(Scope.system()) as session:
            svc.create_reservation(
                session, pr_id=pr_id, project_id=ids["project"],
                wbs_id=ids["child_a1"], budget_head_id=ids["head"],
                amount_paise=100_00, actor="U-REQ")
            svc.settle_reservations(session, pr_id=pr_id, state=state,
                                    actor="U-APP")

    rows = pg_connection.execute(
        "SELECT state, cell_grain, amount_paise, settled_by "
        "FROM pr_reservation WHERE pr_id = %s ORDER BY state", (pr_id,)
    ).fetchall()
    assert [row[0] for row in rows] == ["Expired", "Released"]
    assert {row[1] for row in rows} == {"LINE"}
    assert {row[2] for row in rows} == {100_00}
    assert {row[3] for row in rows} == {"U-APP"}, (
        "a settled reservation names WHO settled it, or it is not settled")
