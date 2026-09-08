"""Migration 014: the six defects in 013 and the six controls it restores.

WHAT IS PROVED HERE, AND WHERE
==============================

Split the way `tests/test_pg_procurement_schema.py` is, and for the same
reason. Roughly half of this file runs with NO DATABASE: those properties are
properties of the migration TEXT, of module constants, or of integer
arithmetic, and a check whose only coverage is in an environment nobody runs
locally is a check nobody runs. The other half carries `@pytest.mark.pg` plus a
`skipif` on `CAPEX_DB_URL`, skips on every workstation here, and FIRST EXECUTES
IN CI's `pg_tests` job.

A SKIP IS NOT A PASS. Every gated test below says so in the shared skip reason,
because "485 skipped" read as "485 fine" is how a live-only guard rots.

THE ONE THAT MATTERS MOST
=========================

`test_a_bill_landing_does_not_raise_available_live` is the over-commitment
scenario end to end: order, bill, then confirm available did NOT rise.
`check_availability` computes `budget - (commitment + actual + pr_reserved)`.
`commitment_paise` is `max(0, ordered - billed)` and FALLS when a bill arrives;
`actual_paise` must RISE by the same amount, and before this wave it had no
writer anywhere in the PostgreSQL path -- so available rose by the billed
amount and the same budget could be committed again. That is AUD-C-001
re-opened, and it was live rather than theoretical from the moment Wave 6's
inbound path made `billed` non-zero.
"""
from __future__ import annotations

import os
import re
import sys as _sys
from datetime import date
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402

import conftest_pg  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402
from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import procurement as ledger  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402
from app.backend.pg import rls, scope_inventory  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
_013 = (MIGRATIONS / "013_procurement.sql").read_text(encoding="utf-8")
_014_PATH = MIGRATIONS / "014_procurement_corrections.sql"
_014 = _014_PATH.read_text(encoding="utf-8")

#: 014 with its prose removed. Every "the migration does not do X" assertion
#: reads THIS: the trailing `-- ROLLBACK:` block is commented-out DDL, and
#: parsing it as real DDL inverts the meaning of every DROP and CREATE in it.
_014_CODE = re.sub(r"--[^\n]*", "", _014)

#: The one skip reason, so a skipped run reads as a skip rather than as a pass.
PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that execute migration 014 rather than reading it."),
)


def _migration(version: str) -> migrate_pg.Migration:
    for m in migrate_pg.discover():
        if m.version == version:
            return m
    raise AssertionError(f"migration {version} is not discovered by the runner")


# =========================================================================
# Source-level: the migration is a migration, and it is ADDITIVE
# =========================================================================
def test_014_is_discovered_named_and_numbered():
    versions = [m.version for m in migrate_pg.discover()]
    assert versions == sorted(versions)
    assert versions.count("014") == 1
    assert _migration("014").name == "procurement_corrections"
    assert "014_procurement_corrections.sql" not in migrate_pg.NON_MIGRATION_FILES


def test_013_is_not_modified_by_this_change():
    """013's checksum is recorded in `schema_migrations` on every database
    where it ran. Editing it makes `assert_schema_current` report drift and
    every existing deployment refuse to boot -- which is why 014 exists at all
    rather than 013 being corrected in place.

    Asserted structurally: 013 must still declare the four objects 014 drops.
    If somebody "fixed" 013 instead, those declarations would be gone and this
    fails.
    """
    assert "CONSTRAINT ux_grn_number UNIQUE (grn_number)" in _013
    assert "CONSTRAINT ux_bill_number UNIQUE (bill_number)" in _013
    assert "CONSTRAINT ux_grn_line_external" in _013
    assert "CREATE INDEX ix_purchase_order_pr" in _013


def test_014_contains_no_delete_or_truncate_of_a_financial_record():
    """"Preserve every existing row" is not advice here; it is the rule.

    No DELETE, no TRUNCATE, no DROP TABLE anywhere in the applied body. The
    ROLLBACK block is commented-out DDL and is excluded, which is the whole
    reason `_014_CODE` exists.
    """
    body = _014_CODE[:_014_CODE.rindex("COMMIT;")]
    for forbidden in ("DELETE FROM", "TRUNCATE", "DROP TABLE"):
        assert forbidden not in body.upper(), (
            f"014 contains {forbidden}. A corrective migration renumbers, "
            f"merges and deletes nothing: it REFUSES an ambiguity and names "
            f"the rows, so an operator decides which document is real.")


def test_the_preflight_refuses_and_runs_before_any_ddl():
    """An operator must be told WHICH rows block the migration, not handed a
    23505 with an index name in it."""
    preflight = _014_CODE.index("DO $preflight$")
    first_ddl = min(
        _014_CODE.index("ALTER TABLE grn ADD COLUMN"),
        _014_CODE.index("CREATE TABLE pr_reservation"))
    assert preflight < first_ddl, (
        "the preflight runs after DDL has already been issued")
    block = _014_CODE[preflight:first_ddl]
    assert "RAISE EXCEPTION" in block, "the preflight reports and then proceeds"
    for index_name in ("ux_grn_number_scoped", "ux_bill_number_scoped",
                       "ux_grn_external_identity", "ux_bill_external_identity",
                       "ux_grn_line_external_v2", "ux_purchase_order_pr",
                       "ux_bill_line_fingerprint"):
        assert index_name in block, (
            f"{index_name} narrows what 013 allowed, and the preflight does "
            f"not check for rows that would violate it")


def test_every_object_014_drops_is_named_exactly():
    """`ux_grn_number`, `ux_bill_number` and `ux_grn_line_external` are TABLE
    CONSTRAINTS in 013, not indexes -- `DROP INDEX ux_grn_number` fails with
    "cannot drop index ... because constraint ... requires it". Only
    `ix_purchase_order_pr` is a real index. The four names are the spec's; the
    statements are the ones that work."""
    for name in ("ux_grn_number", "ux_bill_number", "ux_grn_line_external"):
        assert f"DROP CONSTRAINT IF EXISTS {name};" in _014_CODE, (
            f"{name} is a table constraint in 013 and must be dropped with "
            f"ALTER TABLE ... DROP CONSTRAINT")
    assert "DROP INDEX IF EXISTS ix_purchase_order_pr;" in _014_CODE
    # ...and the two the spec does not name, without which D1 has no effect.
    assert "DROP INDEX IF EXISTS ux_grn_external;" in _014_CODE
    assert "DROP INDEX IF EXISTS ux_bill_external;" in _014_CODE


def test_every_paise_column_starts_its_declaration_line():
    """`migrate_pg._PAISE_COLUMN_RE` is anchored at `^` against each stripped
    field of a CREATE TABLE body. A paise column the parser cannot see is a
    money column whose bigint-ness never verifies, so drift to `numeric(18,2)`
    certifies as adopted."""
    found = migrate_pg._paise_columns_by(_migration("014"))
    assert ("pr_reservation", "amount_paise") in found, (
        "pr_reservation.amount_paise is invisible to the adoption parser; "
        "indent it to column 0 like 013's paise columns")


def test_every_constraint_014_adds_carries_an_explicit_name():
    """`_named_constraints_by` and `_added_constraints_by` report only
    constraints declared with an explicit `CONSTRAINT name`. An anonymous CHECK
    has no `pg_constraint.conname` to look up and therefore certifies as
    adopted while absent."""
    body = _014_CODE[:_014_CODE.rindex("COMMIT;")]
    anonymous = re.findall(r"(?<!CONSTRAINT )\n\s+CHECK \(", body)
    assert anonymous == [], (
        f"{len(anonymous)} anonymous CHECK constraint(s) in 014; each one "
        f"certifies as adopted while absent")


def test_adoption_verification_reaches_014s_altered_objects():
    """Failure class E. Every parser in `migrate_pg` before this read
    `CREATE TABLE` bodies, so a CORRECTIVE migration -- almost entirely ALTERs
    -- had two table names verified and nothing else."""
    migration = _migration("014")
    columns = dict(migrate_pg._added_columns_by(migration))
    for table, column in (("grn", "entity_id"), ("bill", "vendor_key"),
                          ("bill_line", "line_fingerprint"),
                          ("grn_line", "line_no")):
        assert (table, column) in migrate_pg._added_columns_by(migration), (
            f"{table}.{column} is not verified on adoption")
    assert columns, "the ADD COLUMN parser found nothing"

    constraints = migrate_pg._added_constraints_by(migration)
    for expected in (("purchase_order", "ck_purchase_order_status"),
                     ("grn", "ck_grn_status"), ("bill", "ck_bill_status"),
                     ("grn", "fk_grn_entity"), ("bill", "fk_bill_vendor")):
        assert expected in constraints, (
            f"{expected[1]} is not verified on adoption; a database missing "
            f"it would certify as adopted")


def test_a_dropped_column_is_never_reported_as_one_that_must_be_present():
    """The ROLLBACK block drops every column 014 adds. If the parser collected
    DROPs as well as ADDs, 014 would be permanently unadoptable against the
    schema it itself produces."""
    fake = migrate_pg.Migration("999", "fake", _014_PATH)
    added = migrate_pg._added_columns_by(fake)
    assert ("grn", "connection_id") in added
    assert all(column for _table, column in added)
    # The rollback's `ALTER TABLE grn DROP COLUMN IF EXISTS connection_id` is a
    # comment, and even uncommented it is a DROP, which the regex does not match.
    assert "DROP COLUMN" not in " ".join(c for _t, c in added)


# =========================================================================
# Source-level: the status sets are read from the registries
# =========================================================================
def test_the_status_checks_admit_exactly_what_the_code_and_registries_name():
    """D3. The permitted sets are evidenced, value by value, and the two that
    are NOT C3 labels are admitted because `domain.py` structurally requires
    them -- excluding either would be a control REGRESSION dressed as a
    tightening.
    """
    from app.backend import domain
    po = re.search(
        r"ck_purchase_order_status CHECK \(\s*status IN \(([^)]*)\)", _014_CODE)
    assert po, "ck_purchase_order_status is not declared"
    permitted = {v.strip().strip("'") for v in po.group(1).split(",")}

    # `domain.COMMITMENT_RELEASING_STATES` must be writable, or a purchase
    # order can never be cancelled and its commitment can never be released.
    assert domain.COMMITMENT_RELEASING_STATES <= permitted, (
        f"{sorted(domain.COMMITMENT_RELEASING_STATES - permitted)} is named by "
        f"domain.COMMITMENT_RELEASING_STATES and forbidden by the CHECK; the "
        f"commitment it would release can never be released")
    assert "Draft" in permitted, "013's own DEFAULT is rejected by the CHECK"

    # `compute_ledger` excludes a Void receipt. If 'Void' were forbidden, a
    # voided receipt could never be recorded and `received` never reduced.
    assert "ck_grn_status CHECK (status IN ('Approved', 'Void'))" in _014_CODE
    assert "ck_bill_status CHECK (status IN ('Approved', 'Void'))" in _014_CODE


def test_the_lifecycle_seed_matches_the_poc_row_for_row():
    """C2. Read from `app/backend/migrations/002_financial_controls.sql` at
    implementation time, not retyped from memory. Unknown states DENY by
    default, and no row here permits anything the POC did not."""
    poc = (ROOT / "app" / "backend" / "migrations"
           / "002_financial_controls.sql").read_text(encoding="utf-8")
    block = poc[poc.index("INSERT INTO lifecycle_state VALUES"):]
    block = block[:block.index(";")]
    expected = {
        (m[0], m[1], m[2] == "1", m[3] == "1", m[4] == "1")
        for m in re.findall(
            r"\('(\w+)','([^']+)',(\d),(\d),(\d)\)", block)
    }
    assert len(expected) == 21, f"parsed {len(expected)} POC rows, expected 21"

    ported = {
        (m[0], m[1], m[2] == "true", m[3] == "true", m[4] == "true")
        for m in re.findall(
            r"\('(\w+)',\s*'([^']+)',\s*(true|false),\s*(true|false),\s*"
            r"(true|false)\)", _014_CODE)
    }
    assert ported == expected, (
        f"014's lifecycle_state seed differs from the POC's. Only in 014: "
        f"{sorted(ported - expected)}. Missing: {sorted(expected - ported)}")


def test_the_pr_state_machine_omits_what_013s_check_cannot_hold():
    """C6, and the gap is NAMED rather than invented.

    Plan section 12 gives the PR machine as ending `-> CONVERTED | CANCELLED`.
    Neither is in `ck_purchase_request_status`'s permitted set, which 013 froze
    as the seven C3 labels the POC writes. A transition row naming a target
    state the column cannot hold is worse than absent -- it READS as
    enforcement while being unreachable -- and admitting them would require
    dropping a CHECK 013 created for a stated reason.
    """
    pr_check = re.search(
        r"ck_purchase_request_status CHECK \(\s*status IN \(([^)]*)\)", _013)
    assert pr_check
    holdable = {v.strip().strip("'") for v in pr_check.group(1).split(",")}
    assert "Converted" not in holdable and "Cancelled" not in holdable

    rows = re.findall(
        r"\('purchase_request',\s*'([^']+)',\s*'([^']+)'\)", _014_CODE)
    assert rows, "no purchase_request transitions were seeded"
    for from_state, to_state in rows:
        assert from_state in holdable and to_state in holdable, (
            f"transition {from_state!r} -> {to_state!r} names a state "
            f"ck_purchase_request_status cannot hold; the rule would be "
            f"unreachable and would read as enforcement")


def test_the_rule_tables_are_read_only_to_the_application():
    """"Valid transitions are data, not code" means nothing if the running
    application can rewrite them.

    GRANTING SELECT ALONE REMOVES NOTHING, and that is the trap. 004's
    `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON
    TABLES TO capex_app` attaches to the role that ISSUED it and applies to
    every table created by every LATER migration, from the instant each is
    created. Only an explicit REVOKE takes a privilege away -- which is why
    013 revokes DELETE on its eight rather than relying on omitting it.
    """
    assert re.search(
        r"GRANT SELECT ON lifecycle_state, procurement_transition TO capex_app",
        _014_CODE)
    assert re.search(
        r"REVOKE INSERT, UPDATE ON lifecycle_state, procurement_transition "
        r"FROM capex_app", _014_CODE), (
        "capex_app can still INSERT and UPDATE the rule tables that gate it; a "
        "rule table the gated party may edit is a suggestion, not a control")
    assert "REVOKE DELETE ON" in _014_CODE
    revoke = _014_CODE[_014_CODE.index("REVOKE DELETE ON"):]
    revoke = revoke[:revoke.index(";")]
    for table in ("pr_reservation", "lifecycle_state", "procurement_policy",
                  "procurement_transition"):
        assert table in revoke, (
            f"DELETE is not revoked on {table}. 004's ALTER DEFAULT PRIVILEGES "
            f"already granted it the instant the table came into existence, so "
            f"omitting it from the GRANT achieves nothing")


def test_scope_permits_arguments_are_in_the_signature_order():
    """`capex_scope_permits(p_entity_id, p_plant_id, p_location_id,
    p_project_id)` -- all four parameters are `text`, so PostgreSQL accepts ANY
    order silently. Migration 011 put `project_id` in the LOCATION slot and the
    project dimension went unenforced for a whole wave."""
    for call in re.findall(
            r"capex_scope_permits\(([^)]*)\)", _014_CODE, re.DOTALL):
        args = [a.strip() for a in call.split(",")]
        assert args == ["p.entity_id", "p.plant_id", "p.location_id",
                        "p.project_id"], (
            f"capex_scope_permits called with {args}; the signature order is "
            f"(entity, plant, location, project) and a transposition is silent")


def test_the_new_tables_are_in_both_registries():
    """`tests/test_pg_rls_coverage.py` asserts set equality in BOTH directions,
    so a table protected by 014 and absent from either registry fails there.
    Asserted here too, naming 014's four, so the failure says which."""
    for table in ("pr_reservation", "lifecycle_state", "procurement_policy",
                  "procurement_transition"):
        assert table in rls.ALL_RLS_TABLES, f"{table} is not in rls.py"
        assert table in scope_inventory.covered_tables(), (
            f"{table} is not in scope_inventory")
        assert rls.RLS_MIGRATION_BY_TABLE[table] == (
            "014_procurement_corrections.sql")


# =========================================================================
# LIVE -- these first execute in CI's pg_tests job
# =========================================================================
def _seed(session) -> dict:
    """One entity, one project, one WBS element with an approved budget, and a
    purchase order against it. Deliberately minimal: every live test below is
    about ONE rule, and a fixture that seeds a plausible estate hides which
    row the rule actually acted on."""
    ids = {
        "org": "ORG-014", "entity": "ENT-014-A", "entity_b": "ENT-014-B",
        "project": "PRJ-014", "project_b": "PRJ-014-B",
        "wbs": "W-014", "wbs_b": "W-014-B", "head": "BH-014",
    }
    session.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, "
        "updated_by) VALUES (%s, '014', 'Migration 014', 't', 't')",
        (ids["org"],))
    for key in ("entity", "entity_b"):
        session.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name, "
            "created_by, updated_by) VALUES (%s, %s, %s, %s, 't', 't')",
            (ids[key], ids["org"], ids[key], ids[key]))
    for pkey, ekey in (("project", "entity"), ("project_b", "entity_b")):
        session.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name, "
            "status, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 'Released', 't', 't')",
            (ids[pkey], ids[ekey], ids[pkey], ids[pkey]))
    session.execute(
        "INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
        "created_by, updated_by) VALUES (%s, %s, 'H014', 'Head', 't', 't')",
        (ids["head"], ids["entity"]))
    for wkey, pkey in (("wbs", "project"), ("wbs_b", "project_b")):
        session.execute(
            # `wbs_code` and `description`, NOT `code`/`name`. Those are
            # `entity`/`budget_head`'s column names, and the mistake was
            # invisible locally because every test in this file is
            # `@pytest.mark.pg` and skips without a server -- it would
            # have errored all 27 of them on the first CI run.
            "INSERT INTO wbs_element (wbs_id, project_id, wbs_code, "
            "description, level, "
            "sort_order, wbs_path, status, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 1, 1, %s, 'Released', 't', 't')",
            (ids[wkey], ids[pkey], ids[wkey], ids[wkey],
             ids[wkey].replace("-", "_")))
    session.execute(
        "INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
        "budget_paise, updated_by) VALUES (%s, %s, 10000000, 't')",
        (ids["wbs"], ids["head"]))
    session.execute(
        "INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by) "
        "VALUES (%s, %s, 't')", (ids["wbs"], ids["head"]))
    return ids


def _purchase_order(session, ids, *, po_id="PO-014", external_id="ZPO-014",
                    amount=4000000, project=None, wbs=None):
    project = project or ids["project"]
    wbs = wbs or ids["wbs"]
    session.execute(
        "INSERT INTO purchase_order (po_id, po_number, project_id, "
        "vendor_name, status, external_source, external_id, created_by, "
        "updated_by) VALUES (%s, %s, %s, 'V', 'Released', 'ZOHO_ERP', %s, "
        "'t', 't')", (po_id, po_id, project, external_id))
    session.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id, "
        "budget_head_id, quantity, rate_paise, amount_paise, "
        "line_external_id, created_by, updated_by) "
        "VALUES (%s, %s, 1, %s, %s, %s, 1, %s, %s, %s, 't', 't')",
        (f"{po_id}-L1", po_id, project, wbs, ids["head"], amount, amount,
         f"{external_id}-L1"))
    return f"{po_id}-L1"


@pytest.mark.pg
@PG
def test_a_bill_landing_does_not_raise_available_live(pg_database):
    """THE OVER-COMMITMENT SCENARIO, END TO END. Order, bill, and confirm that
    available did NOT rise.

    `check_availability` computes `budget - (commitment + actual +
    pr_reserved)`. `commitment_paise` is `max(0, ordered - billed)` and FALLS
    by the billed amount. `actual_paise` must RISE by the same amount. Before
    migration 014 it had no writer anywhere in the PostgreSQL path, so
    available rose and the same rupees could be committed twice -- AUD-C-001
    re-opened, and live from the moment Wave 6's inbound path made `billed`
    non-zero.
    """
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids, amount=4000000)
        svc.refresh_cells_after_ingest(
            session, [(ids["wbs"], ids["head"])], actor="t")

        before = budget_svc.check_availability(session, ids["wbs"], ids["head"], 0)
        assert before["exposure_paise"] == 4000000, (
            "the purchase order did not raise commitment; this test would "
            "then prove nothing about what a bill does to it")

        ledger.mirror_bill(
            session, external_source="ZOHO_ERP", external_id="ZBILL-014",
            bill_number="B-014", vendor_name="V", bill_date=date(2026, 9, 8),
            po_external_id="ZPO-014", accounting_status="Approved",
            lines=[{"po_line_external_id": "ZPO-014-L1",
                    "line_total_paise": 1500000, "quantity": "1"}],
            actor="t")

        after = budget_svc.check_availability(session, ids["wbs"], ids["head"], 0)

    assert after["available_paise"] <= before["available_paise"], (
        f"available ROSE from {before['available_paise']} to "
        f"{after['available_paise']} when a bill for 1500000 paise landed. "
        f"commitment fell by the billed amount and nothing raised actual by "
        f"it, so the same budget can be committed again. This is AUD-C-001.")
    assert after["exposure_paise"] == 4000000, (
        f"exposure moved from 4000000 to {after['exposure_paise']}; billing "
        f"part of an order neither creates nor destroys exposure -- it moves "
        f"it from commitment to actual")


@pytest.mark.pg
@PG
def test_the_six_derived_columns_all_move_live(pg_database):
    """Every one of the six is written, not four of them.

    Read straight off the row, because "has a writer" is a source-level claim
    and "the writer actually moved it" is not.
    """
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids, amount=4000000)
        ledger.record_receive_line(
            session, po_line_id="PO-014-L1", receive_external_id="ZRCV-1",
            line_external_id="ZRCV-1-L1", quantity="1", amount_paise=2500000,
            external_source="ZOHO_ERP", actor="t")
        ledger.mirror_bill(
            session, external_source="ZOHO_ERP", external_id="ZBILL-014",
            bill_number="B-014", vendor_name="V", bill_date=date(2026, 9, 8),
            po_external_id="ZPO-014", accounting_status="Approved",
            lines=[{"po_line_external_id": "ZPO-014-L1",
                    "line_total_paise": 1000000, "quantity": "1"}],
            actor="t")
        svc.refresh_cells_after_ingest(
            session, [(ids["wbs"], ids["head"])], actor="t")
        row = session.fetchone(
            "SELECT ordered_paise, commitment_paise, actual_paise, "
            "received_paise, received_not_billed_paise, pr_reserved_paise "
            "FROM budget_ledger_cell WHERE wbs_id = %s AND budget_head_id = %s",
            (ids["wbs"], ids["head"]))

    ordered, commitment, actual, received, rnb, reserved = row
    assert ordered == 4000000, "ordered_paise has no writer"
    assert commitment == 3000000, "commitment is not ordered less billed"
    assert actual == 1000000, "actual_paise has no writer -- THE defect"
    assert received == 2500000, "received_paise has no writer"
    assert rnb == 1500000, "received_not_billed_paise has no writer"
    assert reserved == 0, "pr_reserved_paise should be zero with no reservation"


@pytest.mark.pg
@PG
def test_two_entities_may_share_a_grn_number_live(pg_database):
    """D1. 013's `ux_grn_number` was ESTATE-WIDE, so the second entity's
    receipt was refused with a unique violation rather than accepted."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        for suffix, project, wbs in (("A", ids["project"], ids["wbs"]),
                                     ("B", ids["project_b"], ids["wbs_b"])):
            session.execute(
                "INSERT INTO purchase_order (po_id, po_number, project_id, "
                "vendor_name, status, created_by, updated_by) "
                "VALUES (%s, %s, %s, 'V', 'Released', 't', 't')",
                (f"PO-{suffix}", f"PO-{suffix}", project))
            session.execute(
                "INSERT INTO grn (grn_id, grn_number, po_id, entity_id, "
                "received_at, created_by, updated_by) "
                "VALUES (%s, 'GRN-SHARED', %s, "
                "(SELECT entity_id FROM project WHERE project_id = %s), "
                "now(), 't', 't')",
                (f"GRN-{suffix}", f"PO-{suffix}", project))
        count = session.fetchone(
            "SELECT count(*) FROM grn WHERE grn_number = 'GRN-SHARED'")[0]
    assert count == 2, (
        "two entities may legitimately use the same goods-receipt number; "
        "013's estate-wide ux_grn_number refused the second")


@pytest.mark.pg
@PG
def test_two_vendors_may_share_a_bill_number_but_one_may_not_reuse_it_live(
        pg_database):
    """D1, both halves. Two vendors invoicing `INV-001` is ordinary; ONE vendor
    invoicing `INV-001` twice into one entity is a duplicate import."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        for n, vendor in ((1, "Vendor One"), (2, "Vendor Two")):
            session.execute(
                "INSERT INTO bill (bill_id, bill_number, project_id, "
                "entity_id, vendor_name, bill_date, created_by, updated_by) "
                "VALUES (%s, 'INV-001', %s, %s, %s, DATE '2026-09-08', "
                "'t', 't')",
                (f"BILL-{n}", ids["project"], ids["entity"], vendor))
        assert session.fetchone(
            "SELECT count(*) FROM bill WHERE bill_number = 'INV-001'")[0] == 2

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(psycopg.errors.UniqueViolation):
            session.execute(
                "INSERT INTO bill (bill_id, bill_number, project_id, "
                "entity_id, vendor_name, bill_date, created_by, updated_by) "
                "VALUES ('BILL-3', 'inv 001', %s, %s, 'Vendor One', "
                "DATE '2026-09-08', 't', 't')",
                (ids["project"], ids["entity"]))


@pytest.mark.pg
@PG
def test_one_external_document_under_two_organisations_does_not_collide_live(
        pg_database):
    """D1. 013's `ux_bill_external` was UNIQUE on `(external_source,
    external_id)` estate-wide, so the same Zoho bill id under a SECOND
    organisation was refused. `ux_bill_external_identity` leads with
    `connection_id`."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        for n, entity in ((1, ids["entity"]), (2, ids["entity_b"])):
            session.execute(
                "INSERT INTO integration_connection (connection_id, "
                "entity_id, product, dc, organization_id, connector_name, "
                "created_by, updated_by) VALUES (%s, %s, 'ERP', 'in', %s, "
                "'c', 't', 't')",
                (f"CONN-{n}", entity, f"ORG{n}"))
        for n, project in ((1, ids["project"]), (2, ids["project_b"])):
            session.execute(
                "INSERT INTO bill (bill_id, bill_number, project_id, "
                "entity_id, vendor_name, bill_date, connection_id, "
                "external_source, external_id, created_by, updated_by) "
                "VALUES (%s, %s, %s, "
                "(SELECT entity_id FROM project WHERE project_id = %s), "
                "'V', DATE '2026-09-08', %s, 'ZOHO_ERP', 'SHARED-EXT', "
                "'t', 't')",
                (f"BILL-{n}", f"B-{n}", project, project, f"CONN-{n}"))
        count = session.fetchone(
            "SELECT count(*) FROM bill WHERE external_id = 'SHARED-EXT'")[0]
    assert count == 2

    # ...and the same organisation twice is still refused.
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(psycopg.errors.UniqueViolation):
            session.execute(
                "INSERT INTO bill (bill_id, bill_number, project_id, "
                "entity_id, vendor_name, bill_date, connection_id, "
                "external_source, external_id, created_by, updated_by) "
                "VALUES ('BILL-3', 'B-3', %s, %s, 'V', DATE '2026-09-08', "
                "'CONN-1', 'ZOHO_ERP', 'SHARED-EXT', 't', 't')",
                (ids["project"], ids["entity"]))


@pytest.mark.pg
@PG
@pytest.mark.parametrize("replay", ["identical", "reordered"])
def test_a_bill_line_replay_cannot_duplicate_a_line_live(pg_database, replay):
    """D2. The sweeps re-walk by design, and `capex_app` has DELETE revoked, so
    a duplicated bill line is unrecoverable.

    The REORDERED case matters because the source may send the same two lines
    in a different order. Lines carrying their own `external_line_id` are
    order-independent by construction; that is what this parametrisation
    proves.
    """
    lines = [
        {"po_line_external_id": "ZPO-014-L1", "external_line_id": "BL-1",
         "line_total_paise": 1000000, "quantity": "1"},
        {"po_line_external_id": "ZPO-014-L1", "external_line_id": "BL-2",
         "line_total_paise": 500000, "quantity": "1"},
    ]
    second = list(reversed(lines)) if replay == "reordered" else list(lines)

    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        for payload in (lines, second):
            ledger.mirror_bill(
                session, external_source="ZOHO_ERP", external_id="ZBILL-014",
                bill_number="B-014", vendor_name="V",
                bill_date=date(2026, 9, 8), po_external_id="ZPO-014",
                lines=payload, actor="t")
        count = session.fetchone("SELECT count(*) FROM bill_line")[0]
        externals = session.fetchall(
            "SELECT external_line_id FROM bill_line ORDER BY external_line_id")
    assert count == 2, (
        f"a {replay} replay produced {count} bill lines instead of 2")
    assert [row[0] for row in externals] == ["BL-1", "BL-2"]


@pytest.mark.pg
@PG
def test_two_genuinely_distinct_identical_looking_lines_both_survive_live(
        pg_database):
    """D2's hard case: two bill lines against ONE purchase-order line, with no
    external line id and identical in every other field.

    They must NOT collapse into one row. Until 014 they did -- `bill_line_id`
    was derived from `(bill_id, po_line_external_id)`, so the second silently
    overwrote the first and a whole line's value disappeared through the code
    written to prevent exactly that.
    """
    line = {"po_line_external_id": "ZPO-014-L1", "line_total_paise": 700000,
            "quantity": "1"}
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        ledger.mirror_bill(
            session, external_source="ZOHO_ERP", external_id="ZBILL-014",
            bill_number="B-014", vendor_name="V", bill_date=date(2026, 9, 8),
            po_external_id="ZPO-014", lines=[dict(line), dict(line)],
            actor="t")
        rows = session.fetchall(
            "SELECT line_no, amount_paise, line_fingerprint FROM bill_line "
            "ORDER BY line_no")
    assert len(rows) == 2, (
        f"two genuinely distinct identical-looking lines collapsed into "
        f"{len(rows)} row(s); the second line's 700000 paise vanished")
    assert [r[0] for r in rows] == [1, 2], "the ordinal was not written"
    assert rows[0][2] != rows[1][2], (
        "the fingerprints are equal, so the two lines are indistinguishable "
        "to ux_bill_line_fingerprint and one of them cannot be inserted")


@pytest.mark.pg
@PG
def test_a_credit_note_line_carries_negative_paise_live(pg_database):
    """A credit note's line amounts are negative paise (frozen contract 2.4).
    `bill_line.amount_paise` has no `>= 0` CHECK precisely so it can, and the
    fingerprint must handle a negative as readily as a positive."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        ledger.mirror_bill(
            session, external_source="ZOHO_ERP", external_id="ZCN-014",
            bill_number="CN-014", vendor_name="V", bill_date=date(2026, 9, 8),
            po_external_id="ZPO-014", doc_type="CREDIT_NOTE",
            lines=[{"po_line_external_id": "ZPO-014-L1",
                    "line_total_paise": -50000, "quantity": "1"}],
            actor="t")
        row = session.fetchone(
            "SELECT amount_paise, line_fingerprint FROM bill_line")
    assert row[0] == -50000
    assert row[1], "the fingerprint is null for a negative-amount line"


@pytest.mark.pg
@PG
def test_concurrent_duplicate_bill_line_ingestion_is_refused_live(
        pg_database, pg_url, pg_disposable_db_name):
    """D2. Two transactions inserting the same identifierless line must not
    both succeed. `ux_bill_line_fingerprint` is the backstop, and it is the
    only thing standing between a race and a duplicate `capex_app` cannot
    delete."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        session.execute(
            # `po_id`, because `fk_bill_line_bill_po` is COMPOSITE on
            # (bill_id, po_id) -> bill (bill_id, po_id)
            # (013_procurement.sql:684-686, targeting ux_bill_id_po at :617).
            # The line below claims PO-014, so the BILL must be raised against
            # PO-014 too -- that is the half of the ownership trigger pair the
            # FK replaced, and the seed was contradicting it. Leaving `po_id`
            # NULL made the parent (BILL-C, NULL) and the child
            # (BILL-C, PO-014), which has no parent at all.
            "INSERT INTO bill (bill_id, bill_number, project_id, entity_id, "
            "po_id, vendor_name, bill_date, created_by, updated_by) "
            "VALUES ('BILL-C', 'B-C', %s, %s, 'PO-014', 'V', "
            "DATE '2026-09-08', 't', 't')",
            (ids["project"], ids["entity"]))

    insert = (
        "INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id, "
        "wbs_id, budget_head_id, quantity, amount_paise, "
        "po_line_external_id, line_no, created_by, updated_by) "
        "VALUES (%s, 'BILL-C', 'PO-014', 'PO-014-L1', %s, %s, 1, 900000, "
        "'ZPO-014-L1', 1, 't', 't')")
    dsn = conftest_pg._replace_dbname(pg_url, pg_disposable_db_name)
    with psycopg.connect(dsn, autocommit=False) as one, \
            psycopg.connect(dsn, autocommit=False) as two:
        one.execute(insert, ("BLL-A", ids["wbs"], ids["head"]))
        one.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            two.execute(insert, ("BLL-B", ids["wbs"], ids["head"]))
            two.commit()
        two.rollback()


@pytest.mark.pg
@PG
def test_an_identifierless_receive_line_replays_without_duplicating_live(
        pg_database):
    """D5, and it is the most dangerous of the six.

    013's `ux_grn_line_external` used PostgreSQL's default NULLS DISTINCT, so
    it did not constrain a receive line with no external line id -- THE
    ORDINARY case on Zoho ERP, where ERP publishes no receives-list endpoint
    and lines are discovered PO-anchored. The sweeps re-walk on a 300-second
    overlap, so every walk re-inserted and `received` climbed with no new
    receive arriving.
    """
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        for _ in range(3):
            ledger.record_receive_line(
                session, po_line_id="PO-014-L1", receive_external_id="ZRCV-1",
                line_external_id=None, quantity="1", amount_paise=2000000,
                external_source="ZOHO_ERP", actor="t")
        count = session.fetchone("SELECT count(*) FROM grn_line")[0]
        received = session.fetchone(
            "SELECT received_paise FROM budget_ledger_cell "
            "WHERE wbs_id = %s AND budget_head_id = %s",
            (ids["wbs"], ids["head"]))[0]
    assert count == 1, (
        f"three re-walks of one identifierless receive line produced {count} "
        f"rows; `received` climbs with no new receive arriving")
    assert received == 2000000


@pytest.mark.pg
@PG
def test_a_bill_line_with_no_external_id_replays_without_duplicating_live(
        pg_database):
    """D2's fallback path. Where Zoho supplies no stable line id,
    `ux_bill_line_fingerprint` is the identity -- a deterministic digest over
    `po_line_external_id`, `amount_paise`, `quantity` and the ordinal.

    The digest is a GENERATED column rather than an application value for the
    reason `capex_normalise_text` gives about its own: it is always consistent
    with its source REGARDLESS OF WHICH CODE PATH WROTE THE ROW, so a
    fingerprint a writer forgot to compute cannot exist.
    """
    lines = [{"po_line_external_id": "ZPO-014-L1",
              "line_total_paise": 800000, "quantity": "1"}]
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        for _ in range(3):
            ledger.mirror_bill(
                session, external_source="ZOHO_ERP", external_id="ZBILL-014",
                bill_number="B-014", vendor_name="V",
                bill_date=date(2026, 9, 8), po_external_id="ZPO-014",
                lines=[dict(lines[0])], actor="t")
        rows = session.fetchall(
            "SELECT external_line_id, line_fingerprint FROM bill_line")
    assert len(rows) == 1, (
        f"three replays of one identifierless bill line produced {len(rows)} "
        f"rows, and capex_app has no DELETE with which to remove the extras")
    assert rows[0][0] is None and rows[0][1], (
        "the fallback identity is the fingerprint, and it must be populated")


@pytest.mark.pg
@PG
def test_the_lifecycle_gate_now_enforces_rather_than_reporting_unavailable_live(
        pg_database):
    """C2. `lifecycle_gate` PROBED for `lifecycle_state` and reported
    LIFECYCLE_UNAVAILABLE as a SENTENCE, so a skipped gate could not read as a
    passed one. 014 seeds the table, so it enforces -- with no change to any
    caller, which was the point of probing rather than hard-coding.

    Unknown states DENY by default: `domain.lifecycle_permits` returns False
    for a state absent from the table, and that fail-closed behaviour is
    preserved exactly.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)
        assert svc.lifecycle_gate(
            session, project_status="Released", wbs_status="Released"
        ) == svc.LIFECYCLE_PERMITTED

        for project_status, wbs_status in (("Closed", "Released"),
                                           ("Released", "Closed"),
                                           ("Released", "Not A State")):
            with pytest.raises(svc.ProcurementError) as exc:
                svc.lifecycle_gate(session, project_status=project_status,
                                   wbs_status=wbs_status)
            assert exc.value.code == "LIFECYCLE_STATE"


@pytest.mark.pg
@PG
def test_an_unlisted_state_change_is_refused_rather_than_permitted_live(
        pg_database):
    """C6. Valid transitions are DATA. An unlisted one is refused, and an
    ABSENT rule table refuses too -- a gate that cannot run has not passed."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
        svc.assert_transition_permitted(
            session, object_type="purchase_order",
            from_state="Draft", to_state="Submitted")
        with pytest.raises(svc.ProcurementError) as exc:
            svc.assert_transition_permitted(
                session, object_type="purchase_order",
                from_state="Closed", to_state="Released")
        assert exc.value.code == "TRANSITION_NOT_PERMITTED"


@pytest.mark.pg
@PG
def test_the_fractional_quantity_policy_defaults_to_refuse_live(pg_database):
    """C5. The refusal is KEPT as the default and made configurable, not
    removed. Rounding a quantity silently changes what was ordered."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
        assert svc.fractional_quantity_policy(session) == svc.POLICY_REFUSE
        row = session.fetchone(
            "SELECT policy_value FROM procurement_policy "
            "WHERE policy_key = %s", (svc.POLICY_FRACTIONAL_QUANTITY,))
        assert row[0] == svc.POLICY_REFUSE
        with pytest.raises(psycopg.errors.CheckViolation):
            session.execute(
                "UPDATE procurement_policy SET policy_value = 'ROUND_DOWN' "
                "WHERE policy_key = %s", (svc.POLICY_FRACTIONAL_QUANTITY,))


@pytest.mark.pg
@PG
def test_a_document_number_is_minted_atomically_live(pg_database):
    """C4. `SELECT COUNT(*) + 1` reads under no lock at all, so two concurrent
    counts read the same value and both proceed. `numbering_counter`'s
    `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING` takes the row's lock
    to resolve the conflict, which is what serialises them."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
        first = svc.issue_document_number(
            session, svc.PR_SERIES, actor="t", object_type="PurchaseRequest",
            object_id="PR-1", period_key="2026")
        second = svc.issue_document_number(
            session, svc.PR_SERIES, actor="t", object_type="PurchaseRequest",
            object_id="PR-2", period_key="2026")
        issued = session.fetchall(
            "SELECT formatted_number FROM numbering_issued "
            "ORDER BY formatted_number")
    assert first != second, "two calls minted the same number"
    assert first.startswith("PR-") and second.startswith("PR-")
    assert [row[0] for row in issued] == sorted([first, second]), (
        "a minted number was not recorded in the append-only issuance log")


@pytest.mark.pg
@PG
def test_a_pr_converts_into_exactly_one_purchase_order_live(pg_database):
    """D6. `ix_purchase_order_pr` was a PLAIN index, so "a PR converts exactly
    once" was enforced only under the request's own row lock in application
    code."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        session.execute(
            "INSERT INTO purchase_request (pr_id, pr_number, project_id, "
            "requested_by, created_by, updated_by) "
            "VALUES ('PR-1', 'PR-1', %s, 't', 't', 't')", (ids["project"],))
        session.execute(
            "INSERT INTO purchase_order (po_id, po_number, pr_id, project_id, "
            "vendor_name, created_by, updated_by) "
            "VALUES ('PO-1', 'PO-1', 'PR-1', %s, 'V', 't', 't')",
            (ids["project"],))
        with pytest.raises(psycopg.errors.UniqueViolation):
            session.execute(
                "INSERT INTO purchase_order (po_id, po_number, pr_id, "
                "project_id, vendor_name, created_by, updated_by) "
                "VALUES ('PO-2', 'PO-2', 'PR-1', %s, 'V', 't', 't')",
                (ids["project"],))


@pytest.mark.pg
@PG
def test_exactly_one_live_reservation_per_purchase_request_live(pg_database):
    """C1. One live hold per PR per resolved cell, enforced by a partial unique
    index rather than by a service remembering to check. PARTIAL on
    `state = 'Reserved'` so a later hold, after the first is released, is
    creatable.

    The index doing the work is `ux_pr_reservation_live_cell`
    (015_reservation_grain.sql:263-265); 015 dropped 014's whole-PR
    `ux_pr_reservation_live` at :271. Under the single cell this test uses, the
    two are indistinguishable and the behaviour asserted below is unchanged.
    """
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        session.execute(
            "INSERT INTO purchase_request (pr_id, pr_number, project_id, "
            "requested_by, created_by, updated_by) "
            "VALUES ('PR-1', 'PR-1', %s, 't', 't', 't')", (ids["project"],))
        svc.create_reservation(
            session, pr_id="PR-1", project_id=ids["project"],
            wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=1000000, actor="t")

    # THE REFUSAL GETS ITS OWN TRANSACTION, and it did not before. A
    # UniqueViolation puts the transaction into INFAILEDSQLTRANSACTION, so
    # `Database.session`'s commit on exit executes as a ROLLBACK -- taking the
    # seed, the purchase request and the reservation this test goes on to
    # settle with it. `settle_reservations` then found nothing and returned [].
    # Same reason and same shape as the two-organisations test above.
    with pg_database.session(Scope.system()) as session:
        with pytest.raises(psycopg.errors.UniqueViolation):
            svc.create_reservation(
                session, pr_id="PR-1", project_id=ids["project"],
                wbs_id=ids["wbs"], budget_head_id=ids["head"],
                amount_paise=1, actor="t")

    with pg_database.session(Scope.system()) as session:
        touched = svc.settle_reservations(
            session, pr_id="PR-1", state="Released", actor="t")
        assert touched == [(ids["wbs"], ids["head"])]
        svc.create_reservation(
            session, pr_id="PR-1", project_id=ids["project"],
            wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=2, actor="t")
        states = session.fetchall(
            "SELECT state FROM pr_reservation ORDER BY state")
    assert [row[0] for row in states] == ["Released", "Reserved"], (
        "a released reservation was DELETED rather than settled; AUD-H-001 is "
        "'Reserved then Converted or Released or Expired, exactly once', and a "
        "deleted row has no such history")


@pytest.mark.pg
@PG
def test_a_reservation_lowers_availability_live(pg_database):
    """Without reservations two requestors each pass `budget_check` against the
    same rupees, because neither request has taken anything out of
    availability. `pr_reserved_paise` is the limb that stops it."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        session.execute(
            "INSERT INTO purchase_request (pr_id, pr_number, project_id, "
            "requested_by, created_by, updated_by) "
            "VALUES ('PR-1', 'PR-1', %s, 't', 't', 't')", (ids["project"],))
        before = budget_svc.check_availability(
            session, ids["wbs"], ids["head"], 0)["available_paise"]
        svc.create_reservation(
            session, pr_id="PR-1", project_id=ids["project"],
            wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=3000000, actor="t")
        svc.refresh_cells_after_ingest(
            session, [(ids["wbs"], ids["head"])], actor="t")
        after = budget_svc.check_availability(
            session, ids["wbs"], ids["head"], 0)["available_paise"]
    assert after == before - 3000000, (
        f"available went {before} -> {after}; a 3000000 paise hold took "
        f"nothing out of availability, so a second requestor passes against "
        f"the same rupees")


@pytest.mark.pg
@PG
@pytest.mark.parametrize("table,column,bad", [
    ("purchase_order", "status", "cancelled"),
    ("purchase_order", "status", "open"),
    ("grn", "status", "received"),
    ("bill", "status", "paid"),
])
def test_a_raw_or_unmapped_status_is_refused_live(pg_database, table, column, bad):
    """D3. Any string could be written before, including a raw unmapped Zoho
    value -- precisely the guess C17 forbids, with nothing in the schema to
    refuse it. A typo'd 'cancelled' holds commitment FOREVER, silently, because
    `compute_ledger` releases on 'Cancelled'."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        if table == "grn":
            statement = (
                "INSERT INTO grn (grn_id, grn_number, po_id, entity_id, "
                "received_at, status, created_by, updated_by) "
                "VALUES ('G-1', 'G-1', 'PO-014', %s, now(), %s, 't', 't')")
            params = (ids["entity"], bad)
        elif table == "bill":
            statement = (
                "INSERT INTO bill (bill_id, bill_number, project_id, "
                "entity_id, vendor_name, bill_date, status, created_by, "
                "updated_by) VALUES ('B-1', 'B-1', %s, %s, 'V', "
                "DATE '2026-09-08', %s, 't', 't')")
            params = (ids["project"], ids["entity"], bad)
        else:
            statement = ("UPDATE purchase_order SET status = %s "
                         "WHERE po_id = 'PO-014'")
            params = (bad,)
        with pytest.raises(psycopg.errors.CheckViolation):
            session.execute(statement, params)


@pytest.mark.pg
@PG
def test_external_status_raw_is_stored_byte_for_byte_live(pg_database):
    """D4. VERBATIM means verbatim: not trimmed, not title-cased, not
    translated, not normalised. The document row's copy must agree with the
    inbox's, and the whole point of the column is that a mirrored document's
    status can be reconciled against its source ON THE ROW."""
    raw = "  Partially_Paid  "
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        session.execute(
            "INSERT INTO bill (bill_id, bill_number, project_id, entity_id, "
            "vendor_name, bill_date, external_status_raw, created_by, "
            "updated_by) VALUES ('B-1', 'B-1', %s, %s, 'V', "
            "DATE '2026-09-08', %s, 't', 't')",
            (ids["project"], ids["entity"], raw))
        stored = session.fetchone(
            "SELECT external_status_raw FROM bill WHERE bill_id = 'B-1'")[0]
    assert stored == raw, (
        f"stored {stored!r}, sent {raw!r}. C17: raw Zoho status values are "
        f"stored VERBATIM and NEVER overwritten")


@pytest.mark.pg
@PG
def test_an_unknown_external_status_raises_and_has_no_accounting_effect_live(
        pg_database):
    """C17. An UNMAPPED raw value never guesses: the record is ACCEPTED, the
    raw value preserved, and a `reconciliation_exception` of kind
    `UNMAPPED_EXTERNAL_STATUS` raised. It must NOT fall through to
    `bill.accounting_status`'s DEFAULT, which is accounting-effective."""
    with pg_database.session(Scope.system()) as session:
        ids = _seed(session)
        _purchase_order(session, ids)
        result = ledger.mirror_bill(
            session, external_source="ZOHO_ERP", external_id="ZBILL-X",
            bill_number="B-X", vendor_name="V", bill_date=date(2026, 9, 8),
            po_external_id="ZPO-014",
            external_status_raw="quantum_superposed",
            lines=[{"po_line_external_id": "ZPO-014-L1",
                    "line_total_paise": 900000, "quantity": "1"}],
            actor="t")
        svc.refresh_cells_after_ingest(
            session, [(ids["wbs"], ids["head"])], actor="t")
        actual = session.fetchone(
            "SELECT actual_paise FROM budget_ledger_cell "
            "WHERE wbs_id = %s AND budget_head_id = %s",
            (ids["wbs"], ids["head"]))[0]
        kinds = session.fetchall(
            "SELECT kind FROM reconciliation_exception WHERE status = 'Open'")

    assert result["accounting_effective"] is False
    assert "UNMAPPED_EXTERNAL_STATUS" in {row[0] for row in kinds}
    assert actual == 0, (
        f"a bill whose status C17 cannot interpret moved actual CWIP by "
        f"{actual} paise. It must be accepted and held NOT "
        f"accounting-effective until somebody triages the exception")


@pytest.mark.pg
@PG
def test_no_correction_path_can_delete_a_financial_record_live(pg_connection):
    """"Reversal is a NEW row with a flag. Never an edit, never a delete."

    Read back from `information_schema.role_table_grants` rather than from the
    migration text, because 004's `ALTER DEFAULT PRIVILEGES ... GRANT ...
    DELETE` attaches to the role that ISSUED it and applies to every table
    created by every LATER migration. Omitting DELETE from a GRANT removes
    nothing; only the explicit REVOKE does.
    """
    tables = [
        "purchase_request", "pr_line", "purchase_order", "po_line",
        "grn", "grn_line", "bill", "bill_line",
        "pr_reservation", "lifecycle_state", "procurement_policy",
        "procurement_transition",
    ]
    rows = pg_connection.execute(
        "SELECT table_name FROM information_schema.role_table_grants "
        "WHERE grantee = 'capex_app' AND privilege_type = 'DELETE' "
        "AND table_name = ANY(%s)", (tables,)).fetchall()
    pg_connection.rollback()
    assert [row[0] for row in rows] == [], (
        f"capex_app holds DELETE on {sorted({r[0] for r in rows})}. A "
        f"financial record is corrected by a reversal, never removed, and "
        f"`capex_app` must not be able to remove one")

    # ...and the two rule tables that GATE the application are read-only to it.
    writable = pg_connection.execute(
        "SELECT DISTINCT table_name FROM information_schema.role_table_grants "
        "WHERE grantee = 'capex_app' "
        "AND privilege_type IN ('INSERT', 'UPDATE') "
        "AND table_name = ANY(%s) ORDER BY table_name",
        (["lifecycle_state", "procurement_transition"],)).fetchall()
    pg_connection.rollback()
    assert [row[0] for row in writable] == [], (
        f"capex_app can write {sorted({r[0] for r in writable})}. Granting "
        f"SELECT alone removes nothing: 004's ALTER DEFAULT PRIVILEGES already "
        f"handed it INSERT and UPDATE, so only an explicit REVOKE takes them "
        f"away -- and a rule table the gated party may edit is a suggestion")


@pytest.mark.pg
@PG
def test_rls_is_enabled_and_forced_on_every_table_014_adds_live(pg_connection):
    """FORCE is the half most easily dropped and the one invisible from
    `pg_policies`: without it the table's OWNER -- in production the deploy
    identity -- bypasses every policy silently."""
    status = rls.fetch_rls_status(pg_connection, rls.RLS_CORRECTION_TABLES)
    pg_connection.rollback()
    for table in rls.RLS_CORRECTION_TABLES:
        assert status[table]["enabled"], f"{table}: RLS not enabled"
        assert status[table]["forced"], f"{table}: RLS enabled but NOT forced"


# =========================================================================
# LIVE -- both upgrade paths, rerun idempotency, checksum drift
# =========================================================================
@pytest.fixture()
def bare_pg_connection(pg_url, pg_admin_connection):
    """A disposable database with NO schema at all -- not templated from
    `pg_template`, which is already fully migrated."""
    name = conftest_pg._next_test_db_name()
    pg_admin_connection.execute(
        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(
            conftest_pg._replace_dbname(pg_url, name), autocommit=False
        ) as con:
            yield con
    finally:
        pg_admin_connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(conftest_pg._assert_pg_disposable(name))))


@pytest.mark.pg
@PG
def test_a_clean_database_upgrades_001_to_014_live(bare_pg_connection):
    """The first upgrade path: nothing, to everything, in one run."""
    con = bare_pg_connection
    performed = migrate_pg.upgrade(con)
    con.commit()
    assert "014" in performed, f"014 was not applied; performed {performed}"
    state = migrate_pg.read_only_status(con)
    assert state["is_current"], state
    assert state["pending"] == [] and state["drifted"] == []


@pytest.mark.pg
@PG
def test_a_database_already_at_013_upgrades_to_014_live(bare_pg_connection):
    """The SECOND upgrade path, and the one every existing deployment takes.

    Applied 001..013 first and recorded, then 014 alone. This is where the
    backfills, the `SET NOT NULL` and the preflight actually run against rows
    that existed before the migration was written.
    """
    con = bare_pg_connection
    for migration in migrate_pg.discover():
        if migration.version == "014":
            break
        con.execute(migration.sql)
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, name text NOT NULL, "
            "checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now(), "
            "applied_by text NOT NULL DEFAULT current_user, "
            "duration_ms integer)")
        con.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (%s, %s, %s)",
            (migration.version, migration.name, migration.checksum))
    con.commit()

    # A goods receipt and a bill that predate 014, so the backfills have
    # something to backfill and `SET NOT NULL` has something to fail on.
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, "
        "updated_by) VALUES ('O', 'O', 'O', 't', 't')")
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, "
        "created_by, updated_by) VALUES ('E', 'O', 'E', 'E', 't', 't')")
    con.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name, "
        "created_by, updated_by) VALUES ('P', 'E', 'P', 'P', 't', 't')")
    con.execute(
        "INSERT INTO purchase_order (po_id, po_number, project_id, "
        "vendor_name, created_by, updated_by) "
        "VALUES ('PO', 'PO', 'P', 'V', 't', 't')")
    con.execute(
        "INSERT INTO grn (grn_id, grn_number, po_id, received_at, created_by, "
        "updated_by) VALUES ('G', 'G', 'PO', now(), 't', 't')")
    con.execute(
        "INSERT INTO bill (bill_id, bill_number, project_id, vendor_name, "
        "bill_date, created_by, updated_by) "
        "VALUES ('B', 'B', 'P', 'V', DATE '2026-09-08', 't', 't')")
    con.commit()

    performed = migrate_pg.upgrade(con)
    con.commit()
    # 014 IS THE STEP THIS TEST IS ABOUT, NOT THE LAST STEP THERE IS.
    # `upgrade` applies EVERY pending migration, and the setup above stops
    # recording at 013 -- so it returns 014 and everything after it. Asserting
    # equality with ["014"] asserted that 014 happened to be the newest
    # migration, which stopped being true when 015 landed and would stop being
    # true again with every wave. The properties actually being tested are that
    # the 013 -> 014 step RAN, that it ran FIRST, and that nothing already
    # recorded was replayed.
    assert performed and performed[0] == "014", (
        f"the 013 -> 014 step did not run first; performed {performed}")
    assert all(version > "013" for version in performed), (
        f"a migration already recorded as applied was re-applied; "
        f"performed {performed}")

    row = con.execute(
        "SELECT (SELECT entity_id FROM grn WHERE grn_id = 'G'), "
        "(SELECT entity_id FROM bill WHERE bill_id = 'B'), "
        "(SELECT count(*) FROM grn), (SELECT count(*) FROM bill)").fetchone()
    con.rollback()
    assert row[0] == "E" and row[1] == "E", (
        "entity_id was not backfilled from the row's own join")
    assert row[2] == 1 and row[3] == 1, (
        "a pre-existing row was lost; 014 preserves every row")


@pytest.mark.pg
@PG
def test_rerunning_the_upgrade_is_a_no_op_live(bare_pg_connection):
    """Idempotency. The second run must apply nothing, not re-apply 014."""
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.commit()
    again = migrate_pg.upgrade(con)
    con.commit()
    assert again == [], f"a second upgrade performed {again}"


@pytest.mark.pg
@PG
def test_checksum_drift_on_014_is_a_hard_error_live(bare_pg_connection):
    """An applied migration whose contents have changed is a hard error.
    Silently tolerating it is how two environments diverge without anyone
    noticing -- and 014 is the migration most likely to be "just tweaked",
    because it is the one that corrects another."""
    con = bare_pg_connection
    migrate_pg.upgrade(con)
    con.execute(
        "UPDATE schema_migrations SET checksum = 'tampered' "
        "WHERE version = '014'")
    con.commit()

    state = migrate_pg.read_only_status(con)
    assert state["drifted"] == ["014"], state
    with pytest.raises(migrate_pg.MigrationError, match="drift"):
        migrate_pg.assert_schema_current(con)
    with pytest.raises(migrate_pg.MigrationError, match="contents have changed"):
        migrate_pg.upgrade(con)
    con.rollback()


@pytest.mark.pg
@PG
def test_the_preflight_refuses_an_ambiguous_duplicate_rather_than_resolving_it_live(
        bare_pg_connection):
    """The rule that makes this migration safe to run on a live estate.

    Two purchase orders converted from one purchase request is an ambiguity
    nothing here is entitled to resolve: choosing one would decide which
    commitment is real. The migration REFUSES, names the rows, and changes
    nothing.
    """
    con = bare_pg_connection
    for migration in migrate_pg.discover():
        if migration.version == "014":
            break
        con.execute(migration.sql)
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, "
        "updated_by) VALUES ('O', 'O', 'O', 't', 't')")
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name, "
        "created_by, updated_by) VALUES ('E', 'O', 'E', 'E', 't', 't')")
    con.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name, "
        "created_by, updated_by) VALUES ('P', 'E', 'P', 'P', 't', 't')")
    con.execute(
        "INSERT INTO purchase_request (pr_id, pr_number, project_id, "
        "requested_by, created_by, updated_by) "
        "VALUES ('PR', 'PR', 'P', 't', 't', 't')")
    for n in (1, 2):
        con.execute(
            "INSERT INTO purchase_order (po_id, po_number, pr_id, project_id, "
            "vendor_name, created_by, updated_by) "
            "VALUES (%s, %s, 'PR', 'P', 'V', 't', 't')", (f"PO-{n}", f"PO-{n}"))
    con.commit()

    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        con.execute(_014)
    con.rollback()
    assert "PR" in str(excinfo.value) or "preflight refused" in str(excinfo.value)

    # NOTHING was changed, and both purchase orders are still there.
    survivors = con.execute(
        "SELECT count(*) FROM purchase_order WHERE pr_id = 'PR'").fetchone()[0]
    assert survivors == 2, (
        "the preflight resolved the ambiguity instead of refusing it")
    absent = con.execute(
        "SELECT to_regclass('public.pr_reservation')").fetchone()[0]
    con.rollback()
    assert absent is None, "DDL ran despite the preflight refusing"
