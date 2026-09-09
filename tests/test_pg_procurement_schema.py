"""Migration 013: the procurement chain, and the guarantees it is supposed to add.

WHY THIS FILE IS SPLIT THE WAY IT IS
====================================

The source-level tests below run on every machine. The live ones do not: there
is no PostgreSQL on any workstation here, so everything marked `@pytest.mark.pg`
SKIPS locally and first executes in CI's `pg_tests` job. That asymmetry is the
whole reason the source-level half exists at all. A check whose only coverage is
in an environment nobody runs locally is a check nobody runs, and this
repository has already paid for that lesson twice -- a lock query PostgreSQL
could not parse reached integration, and a whole file of RLS *policy-existence*
tests shipped green while proving nothing about enforcement.

So: anything that can be asserted against the migration TEXT is asserted against
the text, and the live tests are reserved for the properties only a database can
answer -- does the constraint actually refuse the write, does the policy actually
hide the row, is the column actually `bigint`.

**A skip is not a pass.** Every live test here is gated on `CAPEX_DB_URL` with a
`skipif` that says so in its reason. Nothing in this file reports success
against a database that was never there.

WHY THE LIVE RLS TESTS CONNECT AS `capex_app`
=============================================

CI's `POSTGRES_USER: capex` is created a cluster SUPERUSER by the official
`postgres` image, and a superuser bypasses row-level security UNCONDITIONALLY --
`FORCE ROW LEVEL SECURITY` governs the table's OWNER and says nothing whatever
about superusers. Every RLS assertion below therefore goes through
`scoped_role_database`, which issues `SET LOCAL ROLE capex_app` before applying
the scope, exactly as `tests/test_pg_unattributed_triage.py` explains at length.

Asserting "the policy exists in pg_policies" would prove nothing at all: a
policy on a table whose RLS was never ENABLEd is inert, and a policy on a table
whose RLS was never FORCEd is bypassed by the deploy identity. Every test below
that claims a policy works does so by inserting a row and failing to read it.

WHAT THIS FILE DOES NOT COVER
=============================

`013_procurement.sql`'s CHECK constraints on `purchase_request.status` and
`bill.accounting_status` reject values that were legal in the SQLite POC
(CONTRACT GAP 3 in the migration header). That is a data-migration concern for
whoever ports POC rows, not a schema property, and it is not asserted here.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, _config_and_provider, pg_admin_connection,
    pg_app_database, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url, scoped_role_database,
)

import os  # noqa: E402
import re  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.pg import migrate_pg, rls, scope_inventory  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "pg" / "013_procurement.sql"
MIGRATION_TEXT = MIGRATION_PATH.read_text(encoding="utf-8")

#: The migration with its `--` comments removed -- the SQL the server actually
#: sees. Used by every assertion about what the migration DOES, because 013's
#: header quotes the fail-open predicate
#: `capex_scope_permits(NULL, NULL, NULL, project_id)` in order to explain why
#: it is NOT used, and a scan of the raw text cannot tell the warning from the
#: mistake it warns about. The rollback assertions deliberately keep
#: `MIGRATION_TEXT`: that whole block IS comments.
MIGRATION_SQL = migrate_pg._LINE_COMMENT_RE.sub("", MIGRATION_TEXT)

#: The eight tables the frozen contract names, in document-chain order.
PROCUREMENT_TABLES = (
    "purchase_request", "pr_line", "purchase_order", "po_line",
    "grn", "grn_line", "bill", "bill_line",
)


def _migration_013() -> migrate_pg.Migration:
    found = [m for m in migrate_pg.discover() if m.version == "013"]
    assert found, "migration 013 is not discovered by the runner"
    return found[0]


# =========================================================================
# The migration exists, is numbered right, and the runner can see it
# =========================================================================
def test_migrations_discover_as_an_unbroken_sequence_and_013_sits_at_its_number():
    """`migrate_pg._FILENAME` is `^(\\d{3})_([a-z0-9_]+)\\.sql$` and `discover()`
    raises on anything that does not match, so a mis-named file is not a silent
    omission -- but a file in the RIGHT shape at the WRONG number is, because it
    would simply sort somewhere else.

    "013 IS THE LAST MIGRATION" WAS TRUE FOR ONE WAVE AND IS NOT A PROPERTY.
    `014_procurement_corrections.sql` exists, so the literal became false for a
    reason that is not a defect: the product grew, which is the one thing this
    assertion was guaranteed to be wrong about eventually.

    THE NAME CARRIED THAT STALE CLAIM AFTER THE BODY STOPPED MAKING IT. It read
    `test_013_is_the_next_migration_and_the_runner_discovers_it` while asserting
    the general sequence property, so a reader scanning names would have
    believed the suite still pinned 013 as last -- and would have gone looking
    for a guard that no longer existed. Renamed to say what it checks. The
    assertions are unchanged and the body was already version-agnostic:
    `[f"{n:03d}" for n in range(1, len(versions) + 1)]` grows with the product.

    Replaced with the property it was reaching for -- 013 discovers exactly
    once, at the position its number gives it, in an unbroken sequence with no
    gap. That catches everything the old form caught (a file numbered 0013, or
    duplicated, or sorting out of order) and one thing it did not: a MISSING
    number, which would mean a migration was deleted rather than superseded.
    """
    versions = [m.version for m in migrate_pg.discover()]
    assert versions == sorted(versions), "migrations must discover in order"
    assert versions.count("013") == 1
    assert versions == [f"{n:03d}" for n in range(1, len(versions) + 1)], (
        f"migration numbers must be an unbroken 001..N sequence; "
        f"discovered {versions}")
    assert versions.index("013") == 12, (
        f"013 must be the thirteenth migration; discovered {versions}")
    assert _migration_013().name == "procurement"


#: `DELETE FROM schema_migrations WHERE version = 'NNN';` as the revert blocks
#: write it, once un-commented. Whitespace around `=` is normalised first so a
#: block that spaces it differently is still recognised rather than silently
#: reported as missing its own deletion.
_LEDGER_DELETE_RE = re.compile(
    r"DELETE\s+FROM\s+schema_migrations\s+WHERE\s+version\s*=\s*'(\d{3})'",
    re.IGNORECASE)


def _ledger_rows_deleted_by(migration: migrate_pg.Migration) -> list[str]:
    """The versions `migration`'s revert block deletes from the ledger.

    Un-commented exactly as `test_the_rollback_block_actually_works_live` does
    it, and for the same reason: the block is a comment until somebody strips
    the prefix, so reading the raw text would find these lines whether or not
    they are inside the block that runs.
    """
    if "-- ROLLBACK:" not in migration.sql:
        return []
    block = migration.sql.split("-- ROLLBACK:", 1)[1]
    statements = [re.sub(r"^--[ ]{0,3}", "", line.strip())
                  for line in block.splitlines() if line.strip().startswith("--")]
    return _LEDGER_DELETE_RE.findall("\n".join(statements))


def test_every_revertible_migrations_ledger_row_is_deleted_at_or_above_its_own_number():
    """A revert that drops the objects and leaves the ledger row is worse than
    no revert at all, and NOTHING SHORT OF A LIVE DATABASE CAUGHT IT.

    `test_the_rollback_block_actually_works_live` catches it, but only where
    PostgreSQL is attached -- so 015, 018 and 019 each shipped with the defect
    and 015's survived long enough to need `020_reservation_revert_ledger.sql`
    written for it alone. This asks the same question of the files, so the
    answer arrives on any machine, on the commit that introduces the block
    rather than on the next live run.

    THE PROPERTY IS "AT OR ABOVE", NOT "SOMEWHERE". Reverts run newest-first,
    so a block numbered above V runs BEFORE V's own. A deletion sitting below V
    would still leave the end state clean, which is all the live assertion can
    see, while leaving an operator who stops part-way holding a ledger that
    claims objects the walk has already dropped -- exactly the unrecoverable
    state 020's header sets out. So the owner must be V itself or something
    later, and this asserts that and not merely that some block mentions V.

    WIDENED FROM "013 AND ABOVE" TO THE WHOLE DIRECTORY, and the twelve
    deletions that made the widening possible are in
    `022_saved_view_policies_and_revert_ledger.sql`'s revert block.

    This test used to start at 013 -- the stack the live test reverts -- and
    said so: the earlier blocks are a different shape (001's is not even a
    trailing block; it sits at the top of the file with no BEGIN/COMMIT) and
    none of them deleted its ledger row, which was recorded here as a real but
    pre-existing gap. It was a gap with teeth. `_status_from` decides `pending`
    purely on KEY PRESENCE (`migrate_pg.py:182-198`), so reverting 010 left the
    '010' row behind, `upgrade()` SKIPPED it, and `assert_schema_current`
    reported `is_current: True` -- the application booting and serving a
    database missing `zoho_connection`, the integration tables and every RLS
    policy they carry while claiming to be fully migrated. Reverting 004 is the
    same shape with `capex_scope_permits`, `capex_app` and eleven tables'
    policies gone.

    The scoping was never a statement that 001..012 were fine; it was a
    statement that nothing yet owned their deletions. 022 owns all twelve, so
    the assertion now covers 001..022 and the exemption is gone rather than
    re-worded. Nothing about 013..021 is relaxed: the same property, the same
    "at or above", over a strictly larger set.
    """
    stack = list(migrate_pg.discover())
    assert len(stack) >= 22, (
        f"the revertible stack collapsed to {[m.version for m in stack]}; "
        f"this test would pass vacuously")
    assert stack[0].version == "001", (
        f"the sweep no longer starts at 001 but at {stack[0].version}; the "
        f"whole point of the widening is that no migration is exempt")

    owners: dict[str, list[str]] = {m.version: [] for m in stack}
    for migration in stack:
        for version in _ledger_rows_deleted_by(migration):
            if version in owners and version <= migration.version:
                owners[version].append(migration.version)

    orphans = sorted(v for v, by in owners.items() if not by)
    assert orphans == [], (
        f"these migrations' revert blocks drop their objects but no block at "
        f"or above their own number deletes their schema_migrations row, so "
        f"`upgrade` will skip them and the objects never come back: {orphans}. "
        f"The file itself cannot be edited to add the line -- its checksum is "
        f"taken over the whole file (migrate_pg.py:138-142) -- so the deletion "
        f"belongs in the revert block of a NEW migration on top, the way 020 "
        f"owns 015's and 021 owns 018's and 019's.")


def test_013_is_not_excluded_as_a_data_file():
    """`NON_MIGRATION_FILES` excludes by EXACT NAME, not by pattern. A new data
    file has to be added there; a new migration must NOT be."""
    assert "013_procurement.sql" not in migrate_pg.NON_MIGRATION_FILES


def test_013_creates_exactly_the_eight_tables_the_contract_names():
    assert migrate_pg._tables_created_by(_migration_013()) == list(PROCUREMENT_TABLES)


def test_the_table_parser_is_not_fooled_by_prose_in_the_header():
    """Regression, found writing this migration.

    `_tables_created_by` scanned the RAW SQL, comments included, so a header
    sentence containing the words CREATE TABLE followed by a word reported a
    table of that name. Nothing creates it, `_missing_tables` reports it
    missing, and the migration can never be adopted -- a documentation sentence
    permanently breaking the adoption path.
    """
    fake = migrate_pg.Migration("999", "prose", MIGRATION_PATH)
    assert "returned" not in migrate_pg._tables_created_by(fake)

    # ...and the strip is what does it, not luck about this file's wording.
    text = ("-- the instant each CREATE TABLE returned.\n"
            "CREATE TABLE real_one (id text);\n")
    assert migrate_pg._CREATE_TABLE_RE.findall(text) == ["returned", "real_one"]
    assert migrate_pg._CREATE_TABLE_RE.findall(
        migrate_pg._LINE_COMMENT_RE.sub("", text)) == ["real_one"]


# =========================================================================
# Money
# =========================================================================
def test_every_paise_column_starts_its_declaration_line_and_is_bigint_in_the_ddl():
    """`_PAISE_COLUMN_RE` is anchored at `^` against each stripped field of a
    CREATE TABLE body. A `*_paise` column the parser cannot see is a money
    column whose TYPE adoption never verifies -- drift to `numeric(18,2)` then
    certifies as adopted while being the one Decimal-leakage path the domain
    forbids.

    Asserted as an exact set, so a paise column added later without being
    parseable fails here rather than being quietly unverified.
    """
    found = set(migrate_pg._paise_columns_by(_migration_013()))
    assert found == {
        ("purchase_request", "amount_paise"),
        ("pr_line", "amount_paise"),
        ("po_line", "rate_paise"),
        ("po_line", "amount_paise"),
        ("po_line", "tax_paise"),
        ("po_line", "non_creditable_tax_paise"),
        ("po_line", "freight_paise"),
        ("grn_line", "amount_paise"),
        ("bill_line", "amount_paise"),
        ("bill_line", "non_creditable_tax_paise"),
        ("bill_line", "freight_paise"),
    }

    # ...and every one of them is declared bigint in the text, so the live
    # information_schema test below is confirming the DDL rather than
    # discovering it.
    for table, column in sorted(found):
        assert re.search(rf"^{column}\s+bigint\b", MIGRATION_SQL, re.MULTILINE), (
            f"{table}.{column} is not declared `bigint` at the start of a line")


def test_no_money_column_in_013_is_numeric_or_a_float():
    """The negative of the above: no `*_paise` column may be declared anything
    but bigint, whatever the parser happens to notice."""
    offenders = re.findall(
        r"^\s*(\w+_paise)\s+(numeric|decimal|real|double|float)\b",
        MIGRATION_SQL, re.MULTILINE | re.IGNORECASE)
    assert offenders == [], f"money declared as a non-integer type: {offenders}"


def test_quantity_is_numeric_never_real():
    """The POC used SQLite REAL. A receipt quantity of 0.2 is not representable
    in binary floating point, so three partial receipts that should total a
    full line would not."""
    for table in ("pr_line", "po_line", "grn_line", "bill_line"):
        body = dict(migrate_pg._extract_table_bodies(MIGRATION_TEXT))[table]
        fields = [" ".join(f.split()) for f in migrate_pg._split_top_level(body)]
        quantity = [f for f in fields if f.startswith("quantity ")]
        assert len(quantity) == 1, f"{table}: {quantity}"
        assert quantity[0].split()[1] == "numeric", f"{table}: {quantity[0]}"


# =========================================================================
# Constraint naming
# =========================================================================
def test_every_table_level_constraint_in_013_is_named():
    """`_named_constraints_by` reports ONLY constraints declared with an
    explicit `CONSTRAINT name`, because an anonymous one has no
    `pg_constraint.conname` to look up. An unnamed constraint is therefore
    invisible to adoption verification: a database missing it certifies as
    adopted while the constraint is absent.

    This walks every top-level field of every CREATE TABLE body and fails on any
    that begins a constraint keyword without a name.
    """
    anonymous: list[str] = []
    for table, body in migrate_pg._extract_table_bodies(MIGRATION_TEXT):
        for field in migrate_pg._split_top_level(body):
            flat = " ".join(field.split())
            if re.match(r"^(UNIQUE|CHECK|FOREIGN\s+KEY|EXCLUDE)\b", flat,
                        re.IGNORECASE):
                anonymous.append(f"{table}: {flat[:80]}")
    assert anonymous == [], (
        "anonymous table constraints certify as adopted while absent: "
        f"{anonymous}")


def test_013_declares_every_constraint_the_frozen_contract_names():
    """The eight names `docs/WAVE6_PROCUREMENT_CONTRACT.md` writes down, spelled
    exactly as it spells them. Three other agents build against these."""
    named = set(migrate_pg._named_constraints_by(_migration_013()))
    for pair in (
            ("pr_line", "ux_pr_line_number"),
            ("purchase_order", "ux_po_id_project"),
            ("po_line", "ux_po_line_po"),
            ("po_line", "ux_po_line_cell"),
            ("po_line", "ck_po_line_non_negative"),
            ("grn_line", "ux_grn_line_external"),
            ("bill", "ck_bill_accounting_status"),
            ("bill", "ck_bill_doc_type"),
    ):
        assert pair in named, f"the frozen contract names {pair[1]} on {pair[0]}"


def test_013_declares_the_composite_foreign_keys_plan_6_2_requires():
    named = set(migrate_pg._named_constraints_by(_migration_013()))
    for pair in (
            ("bill_line", "fk_bill_line_po_line_cell"),   # four columns
            ("bill_line", "fk_bill_line_bill_po"),
            ("po_line", "fk_po_line_po_project"),
            ("po_line", "fk_po_line_wbs_project"),
            ("grn_line", "fk_grn_line_po_line"),
            ("grn_line", "fk_grn_line_grn_po"),
            ("pr_line", "fk_pr_line_wbs_project"),
            ("pr_line", "fk_pr_line_pr_project"),
    ):
        assert pair in named, f"{pair[1]} is missing from {pair[0]}"


def test_the_bill_line_composite_fk_restricts_on_update():
    """§6.2 by name: ON UPDATE RESTRICT is what "additionally stops a po_line
    being re-pointed underneath a bill" means, and it is the one thing the
    SQLite trigger PAIR did not cover."""
    clause = re.search(
        r"CONSTRAINT fk_bill_line_po_line_cell(.*?)(?:,\n\n|\n\n)",
        MIGRATION_SQL, re.DOTALL)
    assert clause, "fk_bill_line_po_line_cell not found"
    flat = " ".join(clause.group(1).split())
    assert "ON UPDATE RESTRICT" in flat, flat
    assert "(po_line_id, po_id, wbs_id, budget_head_id)" in flat, flat


def test_grn_line_money_and_quantity_carry_no_non_negative_check():
    """The POC seeds a reversal line at -0.2 / -1,20,000 paise. Reversal-by-flag
    is the AUD-C-004 contract, so a `>= 0` CHECK here rejects exactly the rows
    the contract requires. This test exists because that constraint is the
    obvious thing to add and adding it is the defect."""
    body = dict(migrate_pg._extract_table_bodies(MIGRATION_TEXT))["grn_line"]
    flat = " ".join(body.split())
    for forbidden in ("amount_paise >= 0", "amount_paise > 0",
                      "quantity >= 0", "quantity > 0"):
        assert forbidden not in flat, (
            f"grn_line declares `{forbidden}`, which rejects the reversal line "
            f"the POC seeds and the AUD-C-004 reversal contract requires")


def test_bill_line_amount_is_signed_for_credit_notes():
    """A credit note's line amounts are negative paise (frozen contract §2.4)."""
    body = dict(migrate_pg._extract_table_bodies(MIGRATION_TEXT))["bill_line"]
    flat = " ".join(body.split())
    assert "amount_paise >= 0" not in flat and "amount_paise > 0" not in flat


def test_bill_line_po_line_id_stays_nullable():
    """Non-PO bills exist, so the four-column FK must bind only when the column
    is present -- exactly as the trigger's `WHEN NEW.po_line_id IS NOT NULL`
    did."""
    body = dict(migrate_pg._extract_table_bodies(MIGRATION_TEXT))["bill_line"]
    fields = [" ".join(f.split()) for f in migrate_pg._split_top_level(body)]
    po_line = [f for f in fields if f.startswith("po_line_id ")]
    assert len(po_line) == 1 and "NOT NULL" not in po_line[0], po_line


def test_a_bill_line_naming_a_po_line_must_also_name_the_po():
    """The hole MATCH SIMPLE leaves, closed.

    A composite FK is not checked AT ALL while any of its columns is NULL. So
    `po_line_id` set with `po_id` NULL names a PO line and is enforced against
    nothing -- a bill line pointing at another purchase order's line, admitted
    silently. Without this CHECK, `bill_line.po_line_id` being nullable would
    not be a documented exemption but a bypass.
    """
    named = set(migrate_pg._named_constraints_by(_migration_013()))
    assert ("bill_line", "ck_bill_line_po_id_accompanies_po_line") in named


# =========================================================================
# The scope-function argument order
# =========================================================================
def test_every_capex_scope_permits_call_in_013_puts_project_in_the_last_slot():
    """Migration 011 passed `project_id` into the `p_location_id` slot and the
    project dimension went entirely unenforced for a whole wave. All four
    parameters are `text`, so PostgreSQL accepts ANY order silently and nothing
    but a reader or this test can tell.
    """
    calls = re.findall(
        r"capex_scope_permits\(([^)]*)\)", MIGRATION_SQL, re.DOTALL)
    assert calls, "013 does not call capex_scope_permits at all"
    for call in calls:
        args = [" ".join(a.split()) for a in call.split(",")]
        assert len(args) == 4, f"wrong arity: {args}"
        assert args == ["p.entity_id", "p.plant_id", "p.location_id",
                        "p.project_id"], (
            f"arguments out of order: {args}. The signature is "
            f"capex_scope_permits(p_entity_id, p_plant_id, p_location_id, "
            f"p_project_id) and every one of them is text.")


def test_013_does_not_redefine_the_frozen_scope_functions():
    """Contract 1 freezes `capex_scope_permits` / `capex_dimension_permits` and
    007 replaced their bodies. Two migrations editing one function body is a
    last-writer-wins collision neither stream's tests would show."""
    for function in ("capex_scope_permits", "capex_dimension_permits",
                     "capex_principal_present", "capex_may_triage_unattributed"):
        assert not re.search(
            rf"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+{function}\b", MIGRATION_SQL), (
            f"013 redefines {function}, which it must only call")


def test_013_does_not_use_the_fail_open_all_null_predicate():
    """`capex_scope_permits(NULL, NULL, NULL, NULL)` waives every dimension and
    returns TRUE for a session carrying no settings whatsoever."""
    assert not re.search(
        r"capex_scope_permits\(\s*NULL\s*,\s*NULL\s*,\s*NULL\s*,\s*NULL\s*\)",
        MIGRATION_SQL)


def test_013_does_not_settle_for_a_project_only_predicate():
    """The defect this migration deliberately does not repeat.

    Dimensions resolve independently (`principal_scope.py`, property 2), so a
    principal restricted to one ENTITY and to no project carries
    `project_ids=None` -- unrestricted -- and
    `capex_scope_permits(NULL, NULL, NULL, project_id)` returns TRUE for every
    purchase order in the estate. For `wbs_element` that waiver is documented
    and the compiler covers it; for the commitments and the money the backstop
    has to hold alone.
    """
    assert not re.search(
        r"capex_scope_permits\(\s*NULL\s*,\s*NULL\s*,\s*NULL\s*,\s*\w+",
        MIGRATION_SQL)


# =========================================================================
# RLS, enablement and rollback -- at source level
# =========================================================================
def test_013_enables_and_forces_row_level_security_on_all_eight_tables():
    """FORCE is the half most easily dropped and its absence is invisible from
    `pg_policies`: without it the table's OWNER -- the deploy identity, in
    production -- bypasses every policy silently."""
    enabled = set(re.findall(
        r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY", MIGRATION_SQL))
    forced = set(re.findall(
        r"ALTER TABLE (\w+) FORCE ROW LEVEL SECURITY", MIGRATION_SQL))
    assert enabled == set(PROCUREMENT_TABLES), sorted(enabled)
    assert enabled - forced == set(), f"ENABLE without FORCE: {sorted(enabled - forced)}"


def test_every_013_policy_carries_both_using_and_with_check():
    """USING filters reads. WITH CHECK filters writes. A policy with only USING
    lets an out-of-scope principal INSERT a row it then cannot see -- which is
    worse than a plain leak, because the write succeeds and disappears."""
    policies = re.findall(
        r"CREATE POLICY (\w+) ON (\w+)(.*?);\n", MIGRATION_SQL, re.DOTALL)
    assert len(policies) == len(PROCUREMENT_TABLES), (
        f"expected one policy per table, found {[p[0] for p in policies]}")
    for name, table, body in policies:
        assert "USING" in body, f"{name} on {table} has no USING"
        assert "WITH CHECK" in body, f"{name} on {table} has no WITH CHECK"


def test_013_declares_a_rollback_section_naming_every_policy_table_and_trigger():
    assert "-- ROLLBACK:" in MIGRATION_TEXT
    rollback = MIGRATION_TEXT.split("-- ROLLBACK:", 1)[1]

    for policy, table in re.findall(
            r"^CREATE POLICY (\w+) ON (\w+)", MIGRATION_SQL, re.MULTILINE):
        assert f"DROP POLICY IF EXISTS {policy} ON {table};" in rollback, policy
        assert f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;" in rollback, table
        assert f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;" in rollback, table

    for table in migrate_pg._tables_created_by(_migration_013()):
        assert f"DROP TABLE IF EXISTS {table};" in rollback, table

    for trigger, table in migrate_pg._triggers_created_by(_migration_013()):
        assert f"DROP TRIGGER IF EXISTS {trigger} ON {table};" in rollback, trigger

    for function in migrate_pg._functions_created_by(_migration_013()):
        assert f"DROP FUNCTION IF EXISTS {function}()" in rollback, function

    assert "DELETE FROM schema_migrations WHERE version = '013';" in rollback, (
        "reverting the DDL without removing the ledger row leaves the runner "
        "believing 013 is applied, so `--upgrade` will never re-apply it")


def test_the_rollback_block_states_what_reverting_costs():
    """House style, and not decoration: dropping these eight tables destroys
    every financial document in them and there is no soft delete to fall back
    on. An operator reading the block must be told before running it."""
    rollback = MIGRATION_TEXT.split("-- ROLLBACK:", 1)[1]
    assert "DESTROYS" in rollback
    assert "dump" in rollback.lower()


def test_the_rollback_block_drops_children_before_parents():
    """Ordered by dependency rather than using CASCADE, so a table the block has
    forgotten raises instead of being silently taken along with something
    else."""
    rollback = MIGRATION_TEXT.split("-- ROLLBACK:", 1)[1]
    order = re.findall(r"DROP TABLE IF EXISTS (\w+);", rollback)
    assert order == ["bill_line", "bill", "grn_line", "grn", "po_line",
                     "purchase_order", "pr_line", "purchase_request"], order


# =========================================================================
# Privileges
# =========================================================================
def test_013_grants_no_delete_and_revokes_the_one_004_handed_out():
    """Immutability is a PRIVILEGE, not only a trigger -- the `audit_log`
    precedent in 001/004 and `approval_action`'s in 008.

    And omitting DELETE from the GRANT is NOT enough, which is the trap. 004
    issued `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
    UPDATE, DELETE ON TABLES TO capex_app`, and that attaches to the role that
    runs the migrations, so it applies to every table a LATER migration creates
    -- these eight included, from the moment each exists. Saying nothing about
    DELETE leaves capex_app holding it. The REVOKE is what removes it.
    """
    grant = re.search(r"GRANT ([A-Z, ]+) ON\s+(.*?)\s+TO capex_app;",
                      MIGRATION_SQL, re.DOTALL)
    assert grant, "013 issues no GRANT to capex_app at all"
    privileges = {p.strip() for p in grant.group(1).split(",")}
    assert privileges == {"SELECT", "INSERT", "UPDATE"}, privileges
    granted = {t.strip() for t in grant.group(2).replace("\n", " ").split(",")}
    assert granted == set(PROCUREMENT_TABLES), sorted(granted)

    revoke = re.search(r"REVOKE ([A-Z, ]+) ON\s+(.*?)\s+FROM capex_app;",
                       MIGRATION_SQL, re.DOTALL)
    assert revoke, (
        "013 does not REVOKE DELETE. 004's ALTER DEFAULT PRIVILEGES already "
        "granted it on every table created after it, so leaving DELETE out of "
        "the GRANT removes nothing.")
    assert {p.strip() for p in revoke.group(1).split(",")} == {"DELETE"}
    revoked = {t.strip() for t in revoke.group(2).replace("\n", " ").split(",")}
    assert revoked == set(PROCUREMENT_TABLES), sorted(revoked)


# =========================================================================
# The two registries, in the same commit
# =========================================================================
def test_both_rls_registries_name_all_eight_procurement_tables():
    """`tests/test_pg_rls_coverage.py` asserts set equality in BOTH directions,
    so omitting either registry fails CI. This says which eight, so a partial
    entry fails HERE with a readable message rather than as a set difference."""
    assert set(rls.RLS_PROCUREMENT_TABLES) == set(PROCUREMENT_TABLES)
    assert set(scope_inventory.tables_for_migration("013_procurement.sql")) \
        == set(PROCUREMENT_TABLES)
    for table in PROCUREMENT_TABLES:
        assert table in rls.ALL_RLS_TABLES
        assert rls.RLS_MIGRATION_BY_TABLE[table] == "013_procurement.sql"
        assert scope_inventory.by_table(table).status == "covered"


def test_the_procurement_registry_is_not_folded_into_006s():
    """`RLS_COVERAGE_TABLE_COLUMNS` means "the eight tables 006 covers", and
    `test_006_covers_exactly_the_eight_tables_the_security_gate_names` asserts
    exactly that set. Folding 013's tables into it would break a passing test
    that is right to fail, and `RLS_MIGRATION_BY_TABLE` would then attribute
    them to the wrong file."""
    assert set(rls.RLS_COVERAGE_TABLE_COLUMNS) & set(PROCUREMENT_TABLES) == set()


def test_every_procurement_table_is_accounted_for_by_a_join_registry():
    """All eight map every dimension to `None`, which on its own is
    indistinguishable from "nobody mapped the columns". `JOINED_VIA_PROJECT` is
    what makes the distinction: these are filtered on entity, plant, location
    AND project, through a join -- harder than any table in 004 or 006, not
    less."""
    for table, columns in rls.RLS_PROCUREMENT_TABLE_COLUMNS.items():
        assert set(columns.values()) == {None}, table
        assert table in rls.JOINED_VIA_PROJECT, table
    assert "bill_line" in rls.TWO_LEGGED


# =========================================================================
# Pure-Python mirrors of 013's predicates -- no database
# =========================================================================
def test_project_join_permits_denies_a_missing_project_rather_than_waiving_it():
    """`None` means the join matched no `project` row. The SQL side is
    `EXISTS (...)`, which is FALSE, so this must not take `permits`' "column
    waived for this row shape" path, which returns True."""
    scope = Scope(user_id="u", entity_ids=frozenset({"ENT-A"}))
    assert rls.project_join_permits(scope, None) is False


def test_project_join_permits_filters_on_entity_not_only_on_project():
    """The property the whole four-dimension predicate exists for: a principal
    restricted to ENT-A and to NO project must not see ENT-B's rows. Under
    `capex_scope_permits(NULL, NULL, NULL, project_id)` this would be True."""
    scope = Scope(user_id="u", entity_ids=frozenset({"ENT-A"}))
    assert rls.project_join_permits(
        scope, ("ENT-A", "PLT-A", "LOC-A", "PRJ-A")) is True
    assert rls.project_join_permits(
        scope, ("ENT-B", "PLT-B", "LOC-B", "PRJ-B")) is False


def test_project_join_permits_honours_read_all_and_empty_grants():
    assert rls.project_join_permits(
        Scope(user_id="svc", read_all=True), ("E", "P", "L", "J")) is True
    assert rls.project_join_permits(
        Scope(user_id="u", entity_ids=frozenset()), ("E", "P", "L", "J")) is False


def test_bill_line_permits_requires_both_reach_paths():
    """A non-PO bill line names any WBS it likes -- the four-column FK is not
    checked while `po_line_id` is NULL -- so the bill path alone would let it
    ride in on its bill's visibility while posting to another project's cell."""
    scope = Scope(user_id="u", project_ids=frozenset({"PRJ-A"}))
    a = (None, None, None, "PRJ-A")
    b = (None, None, None, "PRJ-B")
    assert rls.bill_line_permits(scope, bill_project=a, wbs_project=a) is True
    assert rls.bill_line_permits(scope, bill_project=a, wbs_project=b) is False
    assert rls.bill_line_permits(scope, bill_project=b, wbs_project=a) is False
    assert rls.bill_line_permits(scope, bill_project=a, wbs_project=None) is False


# =========================================================================
# Adoption verification now covers indexes and RLS
# =========================================================================
def test_adoption_parses_013s_indexes():
    """Failure class C. Every external-document uniqueness guarantee in this
    schema is a partial `CREATE UNIQUE INDEX ... WHERE ...`, not a table
    constraint, so `_named_constraints_by` cannot see any of them -- and they
    are what makes an integration that re-walks by design idempotent rather
    than duplicating receipts."""
    indexes = dict((name, table)
                   for name, table in migrate_pg._indexes_created_by(_migration_013()))
    assert indexes["ux_po_external"] == "purchase_order"
    assert indexes["ux_bill_external"] == "bill"
    assert indexes["ux_grn_external"] == "grn"
    assert indexes["ux_po_line_external"] == "po_line"
    assert indexes["ux_purchase_request_external"] == "purchase_request"


def test_adoption_parses_013s_policies_and_rls_statements():
    """Failure class D. A database carrying every table with RLS never enabled
    reads FULLY OPEN and, before this, reported itself adopted and current."""
    policies = dict((name, table)
                    for name, table in migrate_pg._policies_created_by(_migration_013()))
    assert set(policies.values()) == set(PROCUREMENT_TABLES)

    enabled, forced = migrate_pg._rls_tables_by(_migration_013())
    assert set(enabled) == set(PROCUREMENT_TABLES)
    assert set(forced) == set(PROCUREMENT_TABLES)


def test_adoption_parsers_see_the_earlier_migrations_too():
    """Not a 013-only check. These parsers run against every migration on every
    adoption, so a regression in them silently stops verifying 006's and 011's
    objects as well."""
    m006 = [m for m in migrate_pg.discover() if m.version == "006"][0]
    assert set(t for _, t in migrate_pg._policies_created_by(m006)) == {
        "accounting_period", "budget_line", "budget_revision", "budget_transfer",
        "budget_version", "budget_version_cell", "item_master", "vendor_master"}

    m011 = [m for m in migrate_pg.discover() if m.version == "011"][0]
    indexes = {n for n, _ in migrate_pg._indexes_created_by(m011)}
    assert "ux_reconciliation_exception_open" in indexes


def test_adoption_refuses_a_schema_whose_row_level_security_is_switched_off():
    """The refusal proved, without a database -- through the same fake
    `tests/test_known_defects.py` uses for the trigger and money-type refusals.

    A guard nobody has watched fail is an assumption. This one is the shape a
    restore takes: every table there, every constraint there, RLS never enabled,
    the whole product's scope silently waived, and `assert_schema_current`
    reporting the database current.
    """
    from test_known_defects import _FakeAdoptConnection

    tables = set()
    for migration in migrate_pg.discover():
        tables.update(migrate_pg._tables_created_by(migration))

    con = _FakeAdoptConnection(existing_tables=tables,
                               rls_disabled={"purchase_order"})
    with pytest.raises(migrate_pg.MigrationError) as exc:
        migrate_pg.upgrade(con)
    assert "purchase_order" in str(exc.value)
    assert "ENABLED" in str(exc.value)


def test_adoption_refuses_a_schema_whose_row_level_security_is_not_forced():
    """The other half, and the one that is invisible from `pg_policies`: the
    table's OWNER walks past every policy on it."""
    from test_known_defects import _FakeAdoptConnection

    tables = set()
    for migration in migrate_pg.discover():
        tables.update(migrate_pg._tables_created_by(migration))

    con = _FakeAdoptConnection(existing_tables=tables,
                               rls_unforced={"bill_line"})
    with pytest.raises(migrate_pg.MigrationError) as exc:
        migrate_pg.upgrade(con)
    assert "bill_line" in str(exc.value)
    assert "FORCED" in str(exc.value)


def test_adoption_refuses_a_schema_missing_an_external_uniqueness_index():
    """Without this the duplicated receipt is admitted by a database that
    reports itself adopted."""
    from test_known_defects import _FakeAdoptConnection

    tables = set()
    for migration in migrate_pg.discover():
        tables.update(migrate_pg._tables_created_by(migration))

    con = _FakeAdoptConnection(existing_tables=tables,
                               missing_objects={"ux_bill_external"})
    with pytest.raises(migrate_pg.MigrationError) as exc:
        migrate_pg.upgrade(con)
    assert "ux_bill_external" in str(exc.value)


def test_adoption_refuses_a_schema_missing_a_procurement_policy():
    from test_known_defects import _FakeAdoptConnection

    tables = set()
    for migration in migrate_pg.discover():
        tables.update(migrate_pg._tables_created_by(migration))

    con = _FakeAdoptConnection(existing_tables=tables,
                               missing_objects={"po_line_scope"})
    with pytest.raises(migrate_pg.MigrationError) as exc:
        migrate_pg.upgrade(con)
    assert "po_line_scope" in str(exc.value)


# =========================================================================
# Live fixture
# =========================================================================
_ORG = "ORG-PROC"
_ENT_A, _ENT_B = "ENT-PROC-A", "ENT-PROC-B"
_PLT_A, _PLT_B = "PLT-PROC-A", "PLT-PROC-B"
_LOC_A, _LOC_B = "LOC-PROC-A", "LOC-PROC-B"
_PRJ_A, _PRJ_B = "PRJ-PROC-A", "PRJ-PROC-B"
_WBS_A, _WBS_B = "WBS-PROC-A", "WBS-PROC-B"
_BH_A, _BH_B = "BH-PROC-A", "BH-PROC-B"


def _seed_reference(con) -> None:
    """Two entities, each with its own plant, location, project, WBS and budget
    head. Everything below is deliberately symmetrical so a test asserting "A
    cannot see B" is not accidentally asserting "B's row does not exist"."""
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES (%s, 'ORGP', 'Procurement Test Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING", (_ORG,))
    for entity, plant, location, project, wbs, head, suffix in (
            (_ENT_A, _PLT_A, _LOC_A, _PRJ_A, _WBS_A, _BH_A, "A"),
            (_ENT_B, _PLT_B, _LOC_B, _PRJ_B, _WBS_B, _BH_B, "B"),
    ):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (entity, _ORG, entity, entity))
        con.execute(
            "INSERT INTO plant (plant_id, entity_id, code, name, created_by,"
            " updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (plant, entity, plant, plant))
        con.execute(
            "INSERT INTO location (location_id, entity_id, plant_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (location, entity, plant, location, location))
        con.execute(
            "INSERT INTO project (project_id, entity_id, plant_id, location_id,"
            " capex_code, name, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (project, entity, plant, location, project, project))
        con.execute(
            "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (head, entity, head, head))
        con.execute(
            # `wbs_path` is `ltree`, whose labels admit only [A-Za-z0-9_] -- the
            # hyphens in the ids above are fine as text but would be rejected
            # here, so the path is its own value. Passed uncast, matching the
            # pattern the approval suites already use.
            "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description,"
            " wbs_path, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (wbs, project, wbs, wbs, f"proc{suffix.lower()}"))
    con.commit()


def _seed_documents(con) -> None:
    """One complete chain per entity: PR + line, PO + line, GRN + line,
    bill + line."""
    _seed_reference(con)
    for suffix, project, wbs, head in (
            ("A", _PRJ_A, _WBS_A, _BH_A),
            ("B", _PRJ_B, _WBS_B, _BH_B),
    ):
        con.execute(
            "INSERT INTO purchase_request (pr_id, pr_number, project_id,"
            " requested_by, created_by, updated_by)"
            " VALUES (%s, %s, %s, 'U-1', 'T', 'T')",
            (f"PR-{suffix}", f"PR-NUM-{suffix}", project))
        con.execute(
            "INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id,"
            " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES (%s, %s, 1, %s, %s, %s, 500000, 'T', 'T')",
            (f"PRL-{suffix}", f"PR-{suffix}", project, wbs, head))
        con.execute(
            "INSERT INTO purchase_order (po_id, po_number, project_id,"
            " vendor_name, created_by, updated_by)"
            " VALUES (%s, %s, %s, 'Vendor', 'T', 'T')",
            (f"PO-{suffix}", f"PO-NUM-{suffix}", project))
        con.execute(
            "INSERT INTO po_line (po_line_id, po_id, line_no, project_id,"
            " wbs_id, budget_head_id, rate_paise, amount_paise, created_by,"
            " updated_by) VALUES (%s, %s, 1, %s, %s, %s, 100000, 500000, 'T', 'T')",
            (f"POL-{suffix}", f"PO-{suffix}", project, wbs, head))
        # `grn.entity_id` / `bill.entity_id` are NOT NULL from migration 014,
        # and both are read from the row's OWN join rather than supplied --
        # which is exactly what `_mirror_grn_header` and `mirror_bill` do,
        # because the column is the first of `ux_grn_number_scoped` /
        # `ux_bill_number_scoped` and an entity taken from anywhere else would
        # scope the document number wrongly.
        con.execute(
            "INSERT INTO grn (grn_id, grn_number, po_id, entity_id,"
            " received_at, created_by, updated_by)"
            " SELECT %s, %s, %s, p.entity_id, now(), 'T', 'T'"
            " FROM purchase_order po JOIN project p"
            "   ON p.project_id = po.project_id WHERE po.po_id = %s",
            (f"GRN-{suffix}", f"GRN-NUM-{suffix}", f"PO-{suffix}",
             f"PO-{suffix}"))
        con.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " quantity, amount_paise, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, 1, 500000, 'T', 'T')",
            (f"GRNL-{suffix}", f"GRN-{suffix}", f"PO-{suffix}", f"POL-{suffix}"))
        con.execute(
            "INSERT INTO bill (bill_id, bill_number, po_id, project_id,"
            " entity_id, vendor_name, bill_date, created_by, updated_by)"
            " SELECT %s, %s, %s, p.project_id, p.entity_id, 'Vendor',"
            "        current_date, 'T', 'T'"
            " FROM project p WHERE p.project_id = %s",
            (f"BILL-{suffix}", f"BILL-NUM-{suffix}", f"PO-{suffix}", project))
        con.execute(
            "INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id,"
            " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, 500000, 'T', 'T')",
            (f"BLL-{suffix}", f"BILL-{suffix}", f"PO-{suffix}", f"POL-{suffix}",
             wbs, head))
    con.commit()


def _entity_scope(entity: str) -> Scope:
    """A principal restricted on ENTITY ONLY, with `project_ids=None`.

    This is the shape that breaks a project-only predicate: `None` means
    unrestricted on project, so `capex_scope_permits(NULL, NULL, NULL,
    project_id)` returns TRUE for every row in the estate. If 013's policies
    ever regress to that form, every "cannot read" assertion below fails.
    """
    return Scope(user_id="U-PROC", principal_kind="USER",
                 entity_ids=frozenset({entity}), plant_ids=None,
                 project_ids=None, location_ids=None, read_all=False)


def _read_as(pg_url, dbname, scope: Scope, sql: str) -> list[tuple]:
    """Run `sql` through `ScopedRoleDatabase` -- `SET LOCAL ROLE capex_app`
    first, so RLS actually applies. Through `pg_database` (the CI superuser)
    this would return every row and prove nothing."""
    database = scoped_role_database(pg_url, dbname)
    try:
        with database.session(scope) as session:
            return session.fetchall(sql)  # scope-exempt: asserting RLS itself
    finally:
        database.close()


# =========================================================================
# Live: the migration itself
# =========================================================================
@PG
@pytest.mark.pg
def test_013_is_recorded_in_the_ledger_with_its_checksum_live(pg_connection):
    row = pg_connection.execute(
        "SELECT version, name, checksum FROM schema_migrations "
        "WHERE version = '013'").fetchone()
    assert row is not None, "013 was not applied by the fixture's own runner"
    assert row[1] == "procurement"
    assert row[2] == _migration_013().checksum, (
        "the recorded checksum does not match the file, which is schema drift")
    assert len(row[2]) == 64


@PG
@pytest.mark.pg
def test_a_second_upgrade_applies_nothing_live(pg_connection):
    """Idempotency. The template is already fully migrated, so `upgrade()` must
    be a no-op rather than replaying 013's DDL."""
    performed = migrate_pg.upgrade(pg_connection)
    pg_connection.commit()
    assert performed == [], f"a second upgrade performed {performed}"

    count = pg_connection.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = '013'").fetchone()
    assert count[0] == 1, "013 recorded more than once"


@PG
@pytest.mark.pg
def test_every_paise_column_is_bigint_in_the_database_live(pg_connection):
    """Asked of `information_schema`, not of the DDL. A hand-applied or
    restored column has the right name and the right table and only the wrong
    type, and that is the one Decimal-leakage path in the system."""
    wrong: list[str] = []
    for table in PROCUREMENT_TABLES:
        rows = pg_connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name LIKE %s", (table, "%\\_paise")).fetchall()
        for column, data_type in rows:
            if data_type != "bigint":
                wrong.append(f"{table}.{column} is {data_type}")
    assert wrong == [], f"money must be integer paise: {wrong}"

    # ...and the query actually found columns, rather than passing vacuously.
    total = pg_connection.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name LIKE %s "
        "AND table_name = ANY(%s)",
        ("%\\_paise", list(PROCUREMENT_TABLES))).fetchone()
    assert total[0] == 11, f"expected 11 paise columns, found {total[0]}"


@PG
@pytest.mark.pg
def test_the_rollback_block_actually_works_live(pg_connection):
    """The `-- ROLLBACK:` blocks are EXECUTED, not read.

    A rollback section nobody has ever run is a comment. This un-comments them
    exactly as an operator would, runs them, and asserts the eight tables and
    the ledger rows are gone -- then re-applies through the product's own runner
    to prove the revert leaves a database `upgrade()` can still move forward.

    REVERTED IN REVERSE ORDER, AND THAT IS THE CORRECTION THIS TEST NEEDED.
    It used to un-comment 013's block ALONE, which was right while 013 was the
    newest migration and became wrong the moment 014 added `pr_reservation`
    with an FK to `purchase_order`. 013's block then failed with

        cannot drop table purchase_order because other objects depend on it
        DETAIL: constraint pr_reservation_po_id_fkey on table pr_reservation

    and the failure was 013's block being RIGHT. Its own comment says the DROPs
    are ordered by dependency "rather than using CASCADE, so a table this block
    has forgotten raises instead of being silently taken with something else."
    `pr_reservation` is a table 013 cannot know about; raising is the designed
    behaviour and CASCADE would have silently destroyed a table 013 never
    created and 014's block is responsible for.

    You cannot revert a migration that later migrations are stacked on. So this
    reverts the whole stack from the newest down to 013, which is what an
    operator does, and asserts every block in it runs. Derived from
    `migrate_pg.discover()` rather than hard-coded, so a migration added after
    this one is covered the day it lands instead of breaking the assertion.
    """
    stack = [m for m in migrate_pg.discover() if m.version >= "013"]
    assert [m.version for m in stack][:3] == ["013", "014", "015"], (
        f"the procurement stack is not the one this test reverts: "
        f"{[m.version for m in stack]}")

    for migration in reversed(stack):
        assert "-- ROLLBACK:" in migration.sql, (
            f"{migration.version}_{migration.name} carries no ROLLBACK block, "
            f"so the stack cannot be reverted and this test cannot run")
        block = migration.sql.split("-- ROLLBACK:", 1)[1]
        statements: list[str] = []
        for raw in block.splitlines():
            stripped = raw.strip()
            if not stripped.startswith("--"):
                continue
            statements.append(re.sub(r"^--[ ]{0,3}", "", stripped))
        sql = "\n".join(statements)
        if migration.version == "013":
            assert "DROP TABLE IF EXISTS bill_line;" in sql, (
                "the un-comment step produced no SQL; the block's comment "
                "prefix changed and this test would otherwise pass vacuously")
        assert "COMMIT;" in sql, (
            f"{migration.version}'s un-commented block is empty or unterminated")
        pg_connection.execute(sql)
        pg_connection.commit()

    remaining = pg_connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (list(PROCUREMENT_TABLES),)).fetchall()
    assert remaining == [], f"rollback left tables behind: {remaining}"

    reverted = [m.version for m in stack]
    still_recorded = [v for (v,) in pg_connection.execute(
        "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
        (reverted,)).fetchall()]
    assert still_recorded == [], (
        f"a block dropped its objects but left its ledger row, so `upgrade` "
        f"will not re-apply it: {still_recorded}")

    # ...and forward again, through the runner, on the reverted database.
    performed = migrate_pg.upgrade(pg_connection)
    pg_connection.commit()
    assert performed == reverted, (
        f"re-application performed {performed}, expected {reverted}")
    back = pg_connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (list(PROCUREMENT_TABLES),)).fetchall()
    assert len(back) == len(PROCUREMENT_TABLES)


# =========================================================================
# Live: row-level security HIDES ROWS
# =========================================================================
@PG
@pytest.mark.pg
def test_entity_a_cannot_read_entity_bs_po_lines_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """The assertion a policy-existence test cannot make.

    Note the scope: ENTITY only, `project_ids=None`. Under a project-only
    predicate that principal is unrestricted on project and this returns both
    rows.
    """
    _seed_documents(pg_connection)
    seen = _read_as(pg_url, pg_disposable_db_name, _entity_scope(_ENT_A),
                    "SELECT po_line_id FROM po_line ORDER BY po_line_id")
    assert [r[0] for r in seen] == ["POL-A"], (
        f"an ENT-A principal saw {seen}. A PO line is another entity's "
        f"committed value, cell by cell.")


@PG
@pytest.mark.pg
def test_entity_a_cannot_read_entity_bs_grn_lines_live(
        pg_connection, pg_url, pg_disposable_db_name):
    _seed_documents(pg_connection)
    seen = _read_as(pg_url, pg_disposable_db_name, _entity_scope(_ENT_A),
                    "SELECT grn_line_id FROM grn_line ORDER BY grn_line_id")
    assert [r[0] for r in seen] == ["GRNL-A"], seen


@PG
@pytest.mark.pg
def test_entity_a_cannot_read_entity_bs_bill_lines_live(
        pg_connection, pg_url, pg_disposable_db_name):
    _seed_documents(pg_connection)
    seen = _read_as(pg_url, pg_disposable_db_name, _entity_scope(_ENT_A),
                    "SELECT bill_line_id FROM bill_line ORDER BY bill_line_id")
    assert [r[0] for r in seen] == ["BLL-A"], seen


@PG
@pytest.mark.pg
def test_every_procurement_table_hides_the_other_entitys_row_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """All eight, so a table whose policy is subtly wrong cannot hide behind the
    four that are checked individually above."""
    _seed_documents(pg_connection)
    scope = _entity_scope(_ENT_A)
    leaks: dict[str, int] = {}
    for table in PROCUREMENT_TABLES:
        rows = _read_as(pg_url, pg_disposable_db_name, scope,
                        f"SELECT count(*) FROM {table}")
        if rows[0][0] != 1:
            leaks[table] = rows[0][0]
    assert leaks == {}, (
        f"each table holds exactly one ENT-A row and one ENT-B row, so an "
        f"ENT-A principal must see exactly 1 of each; got {leaks}")


@PG
@pytest.mark.pg
def test_an_unscoped_capex_app_session_reads_no_procurement_row_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """A connection that never went through `Database.session()` carries no
    `capex.*` settings at all. Fail closed: an absent setting is an
    unauthenticated session, not a missing restriction."""
    _seed_documents(pg_connection)
    with psycopg.connect(
            _replace_db(pg_url, pg_disposable_db_name), autocommit=False) as raw:
        raw.execute("SET LOCAL ROLE capex_app")
        for table in PROCUREMENT_TABLES:
            count = raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, (
                f"an unscoped capex_app session read {count} rows from {table}")
        raw.rollback()


def _replace_db(url: str, dbname: str) -> str:
    import conftest_pg
    return conftest_pg._replace_dbname(url, dbname)


@PG
@pytest.mark.pg
def test_a_cross_entity_insert_is_refused_by_with_check_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """USING hides rows. WITH CHECK is what stops an out-of-scope principal
    WRITING one -- without it the insert succeeds and then vanishes, which is
    worse than a plain leak because nothing reports a failure."""
    _seed_documents(pg_connection)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with database.session(_entity_scope(_ENT_A)) as session:
                session.execute(
                    "INSERT INTO purchase_order (po_id, po_number, project_id,"
                    " vendor_name, created_by, updated_by)"
                    " VALUES ('PO-CROSS', 'PO-NUM-CROSS', %s, 'V', 'T', 'T')",
                    (_PRJ_B,))
    finally:
        database.close()

    assert pg_connection.execute(
        "SELECT 1 FROM purchase_order WHERE po_id = 'PO-CROSS'").fetchone() is None


@PG
@pytest.mark.pg
def test_a_cross_entity_bill_line_insert_is_refused_by_with_check_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """The two-legged policy's write side: an ENT-A principal may not attach a
    line to ENT-B's bill even naming its own WBS, nor name ENT-B's WBS on its
    own bill."""
    _seed_documents(pg_connection)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with database.session(_entity_scope(_ENT_A)) as session:
                session.execute(
                    "INSERT INTO bill_line (bill_line_id, bill_id, wbs_id,"
                    " budget_head_id, amount_paise, created_by, updated_by)"
                    " VALUES ('BLL-CROSS', 'BILL-B', %s, %s, 1, 'T', 'T')",
                    (_WBS_A, _BH_A))
    finally:
        database.close()


# =========================================================================
# Live: the composite foreign keys refuse what the triggers refused,
#       and the UPDATE case each trigger missed
# =========================================================================
@PG
@pytest.mark.pg
def test_a_bill_line_cannot_name_a_po_line_from_another_purchase_order_live(
        pg_connection):
    """What `bill_line_po_ownership_insert` refused."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id,"
            " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES ('BLL-X', 'BILL-A', 'PO-A', 'POL-B', %s, %s, 1, 'T', 'T')",
            (_WBS_A, _BH_A))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_bill_line_cannot_post_to_a_cell_its_po_line_does_not_carry_live(
        pg_connection):
    """The other half of the same trigger: the four-column FK is the CELL, so a
    bill line naming a valid PO line but a different budget head is refused."""
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
        " created_by, updated_by) VALUES ('BH-OTHER', %s, 'OTHER', 'Other',"
        " 'T', 'T')", (_ENT_A,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id,"
            " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES ('BLL-Y', 'BILL-A', 'PO-A', 'POL-A', %s, 'BH-OTHER', 1,"
            " 'T', 'T')", (_WBS_A,))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_po_line_cannot_be_repointed_underneath_a_bill_live(pg_connection):
    """THE CASE THE TRIGGER PAIR MISSED, and the reason §6.2 names this FK.

    The SQLite triggers checked the BILL LINE on insert and on update. Neither
    fired when the PO LINE moved. Change `po_line.budget_head_id` and every bill
    line already posted against it is silently posting to a cell it was never
    checked against -- a bill sitting on a control cell nobody ever approved it
    for. ON UPDATE RESTRICT is what makes that impossible.
    """
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
        " created_by, updated_by) VALUES ('BH-MOVED', %s, 'MOVED', 'Moved',"
        " 'T', 'T')", (_ENT_A,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "UPDATE po_line SET budget_head_id = 'BH-MOVED' "
            "WHERE po_line_id = 'POL-A'")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_bill_line_cannot_be_repointed_by_update_live(pg_connection):
    """What `bill_line_po_ownership_update` refused, still refused."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "UPDATE bill_line SET po_line_id = 'POL-B' WHERE bill_line_id = 'BLL-A'")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_grn_line_cannot_name_a_po_line_from_another_purchase_order_live(
        pg_connection):
    """What `grn_line_po_ownership` refused."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " quantity, amount_paise, created_by, updated_by)"
            " VALUES ('GRNL-X', 'GRN-A', 'PO-A', 'POL-B', 1, 1, 'T', 'T')")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_grn_line_cannot_claim_a_po_its_own_grn_does_not_have_live(
        pg_connection):
    """The half the composite FK the contract names does NOT cover.

    `(po_line_id, po_id) -> po_line` ties the receive line to a PO line on the
    PO the LINE claims. Nothing in it ties that claim to the GRN's own `po_id`,
    which is what the trigger's `pl.po_id = g.po_id` actually enforced.
    `fk_grn_line_grn_po` is that half.
    """
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " quantity, amount_paise, created_by, updated_by)"
            " VALUES ('GRNL-Y', 'GRN-A', 'PO-B', 'POL-B', 1, 1, 'T', 'T')")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_grn_line_cannot_be_repointed_by_update_live(pg_connection):
    """The UPDATE case `grn_line_po_ownership` (BEFORE INSERT only) missed
    entirely."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "UPDATE grn_line SET po_line_id = 'POL-B' WHERE grn_line_id = 'GRNL-A'")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_two_locally_raised_receipt_lines_on_one_po_line_both_exist_live(
        pg_connection):
    """PROPERTY (b) OF `ux_grn_line_external_v3`, and 013's stated intent.

    013's comment on `ux_grn_line_external` says it out loud: "a locally-raised
    receipt line carries neither external id, and every such line must remain
    insertable rather than all colliding on one NULL row." Both
    `receive_external_id` and `line_external_id` are nullable
    (013_procurement.sql:544-545) and a receipt raised in this product rather
    than mirrored from a tenant has NEITHER.

    014's `ux_grn_line_external_v2` was UNIQUE NULLS NOT DISTINCT with no
    predicate, so every one of those rows keyed as
    `(po_line_id, NULL, NULL, NULL)` and a PO line could hold exactly ONE for
    ever -- the second genuine receipt was refused, or overwrote the first and
    the earlier delivery's value vanished. That is the same loss D5 was written
    to stop, pointed the other way.

    016 makes the index partial on `receive_external_id IS NOT NULL`. A row
    with no external identity has nothing for an identity check to be ABOUT,
    and its identity is its own primary key. Two staged deliveries booked by
    hand against one PO line are two receipts and must be two rows.

    This is the half of the tension that pulls against
    `test_replaying_a_line_without_a_line_id_duplicates_nothing`, which proves
    the replay is still refused. Both must hold.
    """
    _seed_documents(pg_connection)
    # `_seed_documents` has already booked GRNL-A on POL-A with both external
    # ids NULL, so this is the SECOND such row on that PO line -- exactly the
    # insert 014's index refused.
    pg_connection.execute(
        "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
        " quantity, amount_paise, created_by, updated_by)"
        " VALUES ('GRNL-A2', 'GRN-A', 'PO-A', 'POL-A', 2, 250000, 'T', 'T')")
    pg_connection.commit()

    rows = pg_connection.execute(
        "SELECT grn_line_id, amount_paise FROM grn_line"
        " WHERE po_line_id = 'POL-A' ORDER BY grn_line_id").fetchall()
    assert [r[0] for r in rows] == ["GRNL-A", "GRNL-A2"], (
        "a second locally-raised receipt line on one PO line was refused or "
        "overwrote the first; 016's predicate is missing or too narrow")
    assert sum(int(r[1]) for r in rows) == 750000, (
        "the two deliveries do not add up, so one of them was overwritten "
        "rather than inserted -- the silent half of the 014 defect")


@PG
@pytest.mark.pg
def test_a_po_line_cannot_name_a_wbs_in_another_project_live(pg_connection):
    """What `po_line_project_ownership` refused."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO po_line (po_line_id, po_id, line_no, project_id,"
            " wbs_id, budget_head_id, rate_paise, amount_paise, created_by,"
            " updated_by) VALUES ('POL-X', 'PO-A', 2, %s, %s, %s, 1, 1, 'T', 'T')",
            (_PRJ_A, _WBS_B, _BH_A))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_po_line_cannot_be_moved_to_another_projects_wbs_by_update_live(
        pg_connection):
    """The UPDATE case `po_line_project_ownership` (BEFORE INSERT only) missed."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "UPDATE po_line SET wbs_id = %s WHERE po_line_id = 'POL-A'", (_WBS_B,))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_pr_line_cannot_name_a_wbs_in_another_project_live(pg_connection):
    """What `pr_project_wbs_consistency` refused, one level down."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id,"
            " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES ('PRL-X', 'PR-A', 2, %s, %s, %s, 1, 'T', 'T')",
            (_PRJ_A, _WBS_B, _BH_A))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_pr_line_cannot_be_moved_to_another_projects_wbs_by_update_live(
        pg_connection):
    """The UPDATE case `pr_project_wbs_consistency` (BEFORE INSERT only)
    missed."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "UPDATE pr_line SET wbs_id = %s WHERE pr_line_id = 'PRL-A'", (_WBS_B,))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_bill_cannot_claim_a_project_its_purchase_order_does_not_have_live(
        pg_connection):
    """`fk_bill_po_project` -- the constraint that keeps CONTRACT GAP 4's new
    `bill.project_id` from drifting from the purchase order it bills."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_connection.execute(
            "INSERT INTO bill (bill_id, bill_number, po_id, project_id,"
            " entity_id, vendor_name, bill_date, created_by, updated_by)"
            " SELECT 'BILL-X', 'BILL-NUM-X', 'PO-A', p.project_id, p.entity_id,"
            "        'V', current_date, 'T', 'T'"
            " FROM project p WHERE p.project_id = %s", (_PRJ_B,))
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_non_po_bill_and_its_line_are_accepted_live(pg_connection):
    """The other side of every FK above: none of them may block the shape they
    exist to permit. A non-PO bill has no `po_id`, its line names no `po_line`,
    and both must insert."""
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO bill (bill_id, bill_number, project_id, entity_id,"
        " vendor_name, bill_date, created_by, updated_by)"
        " SELECT 'BILL-NOPO', 'BILL-NUM-NOPO', p.project_id, p.entity_id,"
        "        'V', current_date, 'T', 'T'"
        " FROM project p WHERE p.project_id = %s", (_PRJ_A,))
    pg_connection.execute(
        "INSERT INTO bill_line (bill_line_id, bill_id, wbs_id, budget_head_id,"
        " amount_paise, created_by, updated_by)"
        " VALUES ('BLL-NOPO', 'BILL-NOPO', %s, %s, 250000, 'T', 'T')",
        (_WBS_A, _BH_A))
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT po_id, po_line_id FROM bill_line WHERE bill_line_id = 'BLL-NOPO'"
    ).fetchone() == (None, None)


@PG
@pytest.mark.pg
def test_a_bill_line_naming_a_po_line_without_a_po_id_is_refused_live(
        pg_connection):
    """The MATCH SIMPLE hole, closed by
    `ck_bill_line_po_id_accompanies_po_line`. Without it this row is admitted
    and the four-column FK is never evaluated."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "INSERT INTO bill_line (bill_line_id, bill_id, po_line_id, wbs_id,"
            " budget_head_id, amount_paise, created_by, updated_by)"
            " VALUES ('BLL-Z', 'BILL-A', 'POL-B', %s, %s, 1, 'T', 'T')",
            (_WBS_A, _BH_A))
    pg_connection.rollback()


# =========================================================================
# Live: reversals, immutability, and the derived header total
# =========================================================================
@PG
@pytest.mark.pg
def test_a_negative_grn_line_is_accepted_live(pg_connection):
    """The POC seeds exactly this: a reversal receipt at -0.2 / -1,20,000 paise.
    Reversal-by-flag is the AUD-C-004 contract, so this row must insert."""
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO grn (grn_id, grn_number, po_id, entity_id, received_at,"
        " is_reversal, created_by, updated_by)"
        " SELECT 'GRN-REV', 'GRN-NUM-REV', 'PO-A', p.entity_id, now(), true,"
        "        'T', 'T'"
        " FROM purchase_order po JOIN project p"
        "   ON p.project_id = po.project_id WHERE po.po_id = 'PO-A'")
    pg_connection.execute(
        "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
        " quantity, amount_paise, created_by, updated_by)"
        " VALUES ('GRNL-REV', 'GRN-REV', 'PO-A', 'POL-A', -0.2, -120000,"
        " 'T', 'T')")
    pg_connection.commit()
    row = pg_connection.execute(
        "SELECT quantity, amount_paise FROM grn_line "
        "WHERE grn_line_id = 'GRNL-REV'").fetchone()
    assert row[1] == -120000, row
    assert float(row[0]) == pytest.approx(-0.2)


@PG
@pytest.mark.pg
def test_a_negative_bill_line_is_accepted_for_a_credit_note_live(pg_connection):
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO bill (bill_id, bill_number, po_id, project_id, entity_id,"
        " vendor_name, bill_date, doc_type, created_by, updated_by)"
        " SELECT 'BILL-CN', 'BILL-NUM-CN', 'PO-A', p.project_id, p.entity_id,"
        "        'V', current_date, 'CREDIT_NOTE', 'T', 'T'"
        " FROM project p WHERE p.project_id = %s", (_PRJ_A,))
    pg_connection.execute(
        "INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id,"
        " wbs_id, budget_head_id, amount_paise, created_by, updated_by)"
        " VALUES ('BLL-CN', 'BILL-CN', 'PO-A', 'POL-A', %s, %s, -150, 'T', 'T')",
        (_WBS_A, _BH_A))
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT amount_paise FROM bill_line WHERE bill_line_id = 'BLL-CN'"
    ).fetchone()[0] == -150


@PG
@pytest.mark.pg
def test_an_unknown_accounting_status_is_refused_live(pg_connection):
    """GAP-3. AUD-C-004's four values, and no fifth."""
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "UPDATE bill SET accounting_status = 'Posted' WHERE bill_id = 'BILL-A'")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_an_unknown_purchase_request_status_is_refused_live(pg_connection):
    _seed_documents(pg_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "UPDATE purchase_request SET status = 'DRAFT' WHERE pr_id = 'PR-A'")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_capex_app_cannot_delete_any_procurement_document_live(
        pg_connection, pg_url, pg_disposable_db_name):
    """Immutability as a PRIVILEGE. 004's ALTER DEFAULT PRIVILEGES handed
    capex_app DELETE on every table created after it; 013's REVOKE takes it
    back. Asserted as an actual refused DELETE, because a test reading
    `information_schema.role_table_grants` would pass against a grant that was
    never exercised."""
    _seed_documents(pg_connection)
    scope = _entity_scope(_ENT_A)
    for table, column, value in (
            ("bill_line", "bill_line_id", "BLL-A"),
            ("bill", "bill_id", "BILL-A"),
            ("grn_line", "grn_line_id", "GRNL-A"),
            ("grn", "grn_id", "GRN-A"),
            ("po_line", "po_line_id", "POL-A"),
            ("purchase_order", "po_id", "PO-A"),
            ("pr_line", "pr_line_id", "PRL-A"),
            ("purchase_request", "pr_id", "PR-A"),
    ):
        database = scoped_role_database(pg_url, pg_disposable_db_name)
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with database.session(scope) as session:
                    session.execute(
                        f"DELETE FROM {table} WHERE {column} = %s", (value,))
        finally:
            database.close()

    still_there = pg_connection.execute(
        "SELECT count(*) FROM bill_line").fetchone()[0]
    assert still_there == 2, "a delete got through"


@PG
@pytest.mark.pg
def test_the_purchase_request_header_total_follows_its_lines_live(pg_connection):
    """GAP-1's reconciliation. `domain.budget_check` reads the HEADER's
    `amount_paise`; the lines now carry the control-cell grain. If the two can
    disagree, the budget check is checking a number that means nothing."""
    _seed_documents(pg_connection)

    def total() -> int:
        return pg_connection.execute(
            "SELECT amount_paise FROM purchase_request WHERE pr_id = 'PR-A'"
        ).fetchone()[0]

    assert total() == 500000, "the seeded line did not reach the header"

    pg_connection.execute(
        "INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id, wbs_id,"
        " budget_head_id, amount_paise, created_by, updated_by)"
        " VALUES ('PRL-A2', 'PR-A', 2, %s, %s, %s, 250000, 'T', 'T')",
        (_PRJ_A, _WBS_A, _BH_A))
    assert total() == 750000

    pg_connection.execute(
        "UPDATE pr_line SET amount_paise = 100000 WHERE pr_line_id = 'PRL-A2'")
    assert total() == 600000

    pg_connection.execute("DELETE FROM pr_line WHERE pr_line_id = 'PRL-A2'")
    assert total() == 500000
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_a_duplicate_receive_line_is_refused_live(pg_connection):
    """`ux_grn_line_external`. The sweeps re-walk by design -- a 300-second
    overlap and a cycling walk -- so the same receive line arrives more than
    once and must not become two receipts."""
    _seed_documents(pg_connection)
    pg_connection.execute(
        "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
        " receive_external_id, line_external_id, quantity, amount_paise,"
        " created_by, updated_by)"
        " VALUES ('GRNL-E1', 'GRN-A', 'PO-A', 'POL-A', 'RCV-1', 'L-1', 1,"
        " 100, 'T', 'T')")
    pg_connection.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_connection.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " receive_external_id, line_external_id, quantity, amount_paise,"
            " created_by, updated_by)"
            " VALUES ('GRNL-E2', 'GRN-A', 'PO-A', 'POL-A', 'RCV-1', 'L-1', 1,"
            " 100, 'T', 'T')")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_two_locally_raised_receive_lines_without_external_ids_both_insert_live(
        pg_connection):
    """The other side of the receive-line key: two GENUINELY DISTINCT lines
    carrying no external id must both stay insertable, rather than all of them
    colliding on one NULL row.

    THE REQUIREMENT IS UNCHANGED; MIGRATION 014 CHANGED WHAT KEEPS THEM APART,
    and it had to. 013 relied on PostgreSQL's default NULLS DISTINCT, which
    kept these two rows apart at the cost of not constraining them AT ALL --
    and on Zoho ERP a receive line with no external line id is THE ORDINARY
    case, because ERP publishes no receives-list endpoint and lines are
    discovered PO-anchored. The sweeps re-walk on a 300-second overlap, so
    every walk re-inserted the same line and `received` climbed with no new
    receive arriving. That is D5, the most dangerous of the six defects
    `014_procurement_corrections.sql` closes.

    `ux_grn_line_external_v2` is `NULLS NOT DISTINCT` over four columns, the
    fourth being the receive line's ORDINAL. So distinctness now comes from the
    ordinal -- which is a real property of the two lines -- instead of from the
    absence of a value, which was a property of nothing.

    AND THEN MIGRATION 016 CHANGED IT AGAIN, WHICH IS WHY THE CONVERSE BELOW
    MOVED. 014's index keyed every locally-raised line as
    `(po_line_id, NULL, NULL, NULL)` with NULLs grouped, so a PO line could
    hold EXACTLY ONE of them for ever and the second genuine receipt was
    refused or silently overwritten -- D5 pointed the other way.
    `016_grn_line_ordinal.sql:134-137` therefore made the index PARTIAL,
    `WHERE receive_external_id IS NOT NULL`, and named it
    `ux_grn_line_external_v3`. A row with no receive id is now outside the
    index entirely and "its identity is its own primary key, exactly as 013
    intended" (016's header). `line_no` is still in the index and still
    NULLABLE, and 016 states at length why it is deliberately not populated
    and not made NOT NULL.
    So a third line carrying `line_no = 2` and no receive id is no longer
    refused, and asserting that it is would be asserting the defect 016 exists
    to close. The converse is asserted where 016 kept it instead: over the rows
    the index still governs. The case chosen is the ORDINARY Zoho ERP shape --
    a receive id, no line id, no ordinal -- which is the one 016's header names
    as the reason `NULLS NOT DISTINCT` is retained, and which
    `test_a_duplicate_receive_line_is_refused_live` above does not reach
    because it supplies a `line_external_id`. That is a stronger converse: it
    pins the exact population and the exact NULL-grouping that a further
    relaxation of this index would break.
    """
    _seed_documents(pg_connection)
    for line_id, line_no in (("GRNL-N1", 1), ("GRNL-N2", 2)):
        pg_connection.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " line_no, quantity, amount_paise, created_by, updated_by)"
            " VALUES (%s, 'GRN-A', 'PO-A', 'POL-A', %s, 1, 100, 'T', 'T')",
            (line_id, line_no))
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT count(*) FROM grn_line WHERE grn_id = 'GRN-A'").fetchone()[0] == 3

    # A THIRD locally-raised line indistinguishable from the second on every
    # indexed column is nonetheless INSERTABLE, because 016's predicate puts
    # all three outside the index. This is 013's rule restored, and it is
    # asserted rather than left implicit so that narrowing the predicate later
    # is caught here rather than in production.
    pg_connection.execute(
        "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
        " line_no, quantity, amount_paise, created_by, updated_by)"
        " VALUES ('GRNL-N3', 'GRN-A', 'PO-A', 'POL-A', 2, 1, 100, 'T', 'T')")
    pg_connection.commit()

    # ...and the half 013 could not enforce, over the rows 016 still governs.
    # A receive id with NO line id and NO ordinal is the ordinary Zoho ERP
    # shape; under NULLS NOT DISTINCT its re-walk collides with itself instead
    # of being re-inserted on every 300-second overlap.
    pg_connection.execute(
        "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
        " receive_external_id, quantity, amount_paise, created_by, updated_by)"
        " VALUES ('GRNL-N4', 'GRN-A', 'PO-A', 'POL-A', 'RCV-N', 1, 100,"
        " 'T', 'T')")
    pg_connection.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_connection.execute(
            "INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id,"
            " receive_external_id, quantity, amount_paise, created_by,"
            " updated_by)"
            " VALUES ('GRNL-N5', 'GRN-A', 'PO-A', 'POL-A', 'RCV-N', 1, 100,"
            " 'T', 'T')")
    pg_connection.rollback()


@PG
@pytest.mark.pg
def test_row_level_security_is_enabled_and_forced_on_all_eight_live(pg_connection):
    """Read from `pg_class`, not from the migration text. `ENABLE` without
    `FORCE` looks identical in `pg_policies` and is bypassed by the table's
    owner."""
    status = rls.fetch_rls_status(pg_connection, PROCUREMENT_TABLES)
    for table in PROCUREMENT_TABLES:
        assert status[table]["enabled"], f"RLS not enabled on {table}"
        assert status[table]["forced"], f"RLS not FORCED on {table}"
