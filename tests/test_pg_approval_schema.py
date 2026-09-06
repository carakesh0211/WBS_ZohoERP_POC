"""The approval engine's schema: what `008_approval_engine.sql` must declare,
and -- against a live database -- what it must actually refuse.

Two layers, and the split matters:

  * **Database-free.** The migration is read as TEXT and checked for every
    table, column, immutability trigger, constraint, RLS statement and
    privilege change Contract 1 of `docs/WAVE4_CONTRACTS.md` requires, plus the
    agreement between that text, `app.backend.pg.approval_schema` and
    `app.backend.pg.scope_inventory`. These run everywhere, including on a
    machine with no PostgreSQL, which is where this repository is developed.

  * **Live.** Text checks prove a trigger is *declared*. They cannot prove it
    *fires*, that a CHECK constraint actually rejects the row it names, or that
    an RLS policy hides anything. Those need a real database, so the tests
    below marked `@pytest.mark.pg` create the illegal state and assert
    PostgreSQL refuses it.

**A skip is not a pass.** Everything in the live section is currently skipped
on the development machine (there is no local PostgreSQL) and runs for the
first time in CI's `pg_tests` job. The database-free section is deliberately
thorough BECAUSE of that: it is the half that is actually executed while the
schema is being written, and it is written to fail loudly on a missing trigger
or a drifted constant rather than to agree with whatever the file happens to
say. It still cannot substitute for the live half -- a declared trigger that
raises the wrong exception, or a policy whose predicate is inverted, passes
every text check in this file and is caught only below.

`app.backend.pg.approval_schema` is checked against the migration in BOTH
directions, so streams 2, 3 and 5 can import a name from it knowing it is the
name the database actually has.
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

from app.backend.pg import approval_schema, scope_inventory  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PROJECT_ROOT = _Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "pg"
MIGRATION_PATH = MIGRATIONS_DIR / "008_approval_engine.sql"
SEED_PART_PATH = MIGRATIONS_DIR / "seed_parts" / "008_approvals.sql"

SQL = MIGRATION_PATH.read_text(encoding="utf-8")
SEED_SQL = SEED_PART_PATH.read_text(encoding="utf-8")

#: The migration with `--` line comments stripped. Every structural assertion
#: uses this: the file's header names almost every object it creates, in prose,
#: so searching the raw text would happily "find" a trigger that exists only in
#: a comment. The ROLLBACK-section tests below are the deliberate exception --
#: they search the raw text, because that section IS a comment.
CODE = re.sub(r"--[^\n]*", "", SQL)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ======================================================= text-reading helpers
def _table_body(table: str, text: str = CODE) -> str:
    """Everything between the outermost parentheses of `table`'s CREATE TABLE.

    Paren-counted rather than regex-matched: a body containing
    ``CHECK (quorum_n >= 1)`` or ``daterange(...)`` nests its own parentheses,
    and "up to the next `)`" would stop in the middle of one.
    """
    match = re.search(rf"^CREATE TABLE {table} \(", text, re.MULTILINE)
    assert match is not None, f"{table}: no CREATE TABLE in 008_approval_engine.sql"
    depth, i = 1, match.end()
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[match.end():i - 1]


def _columns(table: str) -> list[str]:
    """`table`'s column names, in declaration order, from the migration text."""
    body = _table_body(table)
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))

    skip = re.compile(r"^(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|EXCLUDE)\b", re.I)
    out = []
    for field in (p.strip() for p in parts):
        if field and not skip.match(field):
            out.append(field.split()[0])
    return out


def _check_in_values(table: str, column: str) -> tuple[str, ...]:
    """The literal list from ``CHECK (<column> IN ('A', 'B', ...))``."""
    body = _table_body(table)
    match = re.search(
        rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)", body, re.IGNORECASE | re.DOTALL)
    assert match is not None, f"{table}.{column}: no CHECK (... IN (...)) found"
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _has_named_constraint(table: str, name: str) -> bool:
    return bool(re.search(rf"\bCONSTRAINT {name}\b", _table_body(table)))


def _squash(text: str) -> str:
    """`text` with every run of whitespace collapsed to one space, trimmed.

    The migration aligns its SQL into columns for readability, so a literal
    substring search for ``OLD.snapshot IS DISTINCT FROM NEW.snapshot`` misses
    a clause padded to line up with the two beneath it. Comparing squashed
    text asserts what the clause SAYS rather than how it is indented.
    """
    return re.sub(r"\s+", " ", text).strip()


def _frozen_fields_when_clause() -> str:
    """The WHEN clause of the approval_instance frozen-fields trigger."""
    match = re.search(
        r"CREATE TRIGGER approval_instance_frozen_fields BEFORE UPDATE ON "
        r"approval_instance(.*?)EXECUTE FUNCTION", CODE, re.DOTALL)
    assert match is not None, "the frozen-fields trigger is not declared"
    return _squash(match.group(1))


# ===================================================== Contract 1: the tables
def test_every_contract_1_table_is_created():
    """The ten tables Contract 1 names, no more and no fewer.

    Equality, not containment: a table created here that the contract does not
    name is as much a divergence as a missing one, and the streams that import
    `approval_schema.APPROVAL_TABLES` would not know about it.
    """
    created = re.findall(r"^CREATE TABLE (\w+)", CODE, re.MULTILINE)
    assert created == list(approval_schema.APPROVAL_TABLES), (
        "008 creates a different set (or order) of tables than "
        f"approval_schema.APPROVAL_TABLES: {created}")
    assert set(created) == {
        "reason_code", "approval_definition", "approval_rule", "approval_stage",
        "approval_stage_approver", "approval_instance",
        "approval_stage_instance", "approval_assignment", "approval_action",
        "approval_delegation",
    }


@pytest.mark.parametrize(("table", "required"), [
    ("approval_definition", ("definition_id", "object_type", "code", "version",
                             "status", "entity_id", "effective_from",
                             "effective_to")),
    ("approval_rule", ("rule_id", "definition_id", "priority", "predicate")),
    ("approval_stage", ("stage_id", "definition_id", "stage_no", "name",
                        "parallel_group", "quorum_type", "quorum_n",
                        "applies_when", "sla_hours", "escalate_after_hours",
                        "escalate_to", "allow_delegation", "requires_reason",
                        "reason_code_set")),
    ("approval_stage_approver", ("stage_id", "ordinal", "approver_kind",
                                 "approver_ref", "scope_expr")),
    ("approval_instance", ("instance_id", "object_type", "object_id",
                           "object_version", "object_content_sha",
                           "definition_id", "definition_version", "status",
                           "current_stage_no", "snapshot",
                           "supersedes_instance_id", "maker_user_id",
                           "entity_id", "project_id", "opened_at", "closed_at",
                           "correlation_id")),
    ("approval_stage_instance", ("stage_instance_id", "instance_id", "stage_no",
                                 "parallel_group", "status", "skip_reason",
                                 "quorum_required", "quorum_met", "opened_at",
                                 "due_at", "escalated_at", "closed_at")),
    ("approval_assignment", ("assignment_id", "stage_instance_id",
                             "assignee_user_id", "assigned_via",
                             "delegated_from", "state")),
    ("approval_action", ("action_id", "stage_instance_id", "instance_id",
                         "actor_user_id", "acting_for_user_id", "action",
                         "reason_code", "reason_text", "at", "seq",
                         "prev_hash", "entry_hash")),
    ("approval_delegation", ("delegation_id", "delegator_user_id",
                             "delegate_user_id", "scope_key", "active_range",
                             "created_by", "revoked_at", "revoke_reason")),
    ("reason_code", ("code", "applies_to_action", "applies_to_object_type",
                     "label", "requires_free_text", "active")),
])
def test_every_contract_1_column_is_declared(table, required):
    """Each column Contract 1 names, spelled exactly as the contract spells it.

    Containment rather than equality here, deliberately: the contract lists the
    columns that carry meaning, and the house style adds created_at/created_by
    audit columns to almost every table (see 003_budget_planning.sql). Those
    extras are not a divergence. A MISSING contract column is.
    """
    declared = _columns(table)
    missing = [c for c in required if c not in declared]
    assert missing == [], f"{table}: Contract 1 columns missing: {missing}"


def test_approval_instance_carries_both_scope_columns_directly():
    """Contract 6: entity_id and project_id are denormalised onto the instance
    so it is scopable without a join. object_type/object_id are polymorphic, so
    no join exists to scope it by -- this is not a convenience."""
    body = _table_body("approval_instance")
    assert re.search(r"entity_id\s+text NOT NULL REFERENCES entity", body), (
        "approval_instance.entity_id must be NOT NULL and reference entity: "
        "an instance with no entity is unscopable, and RLS would waive the "
        "dimension rather than deny it.")
    assert re.search(r"project_id\s+text REFERENCES project", body)


# ================================================== Contract 2: the statuses
@pytest.mark.parametrize(("table", "column", "constant"), [
    ("approval_definition", "status", "DEFINITION_STATUSES"),
    ("approval_instance", "status", "INSTANCE_STATUSES"),
    ("approval_stage_instance", "status", "STAGE_STATUSES"),
    ("approval_assignment", "state", "ASSIGNMENT_STATES"),
    ("approval_stage", "quorum_type", "QUORUM_TYPES"),
    ("approval_stage_approver", "approver_kind", "APPROVER_KINDS"),
    ("approval_assignment", "assigned_via", "ASSIGNED_VIA"),
    ("reason_code", "applies_to_action", "REASON_CODE_ACTIONS"),
])
def test_status_constants_match_the_database_check_constraint(table, column, constant):
    """`approval_schema`'s tuples and the migration's CHECK lists are the same
    set, in the same order. This is the anti-drift assertion the whole
    constants module exists for: a stream importing INSTANCE_STATUSES must be
    importing what the database will actually accept."""
    assert _check_in_values(table, column) == getattr(approval_schema, constant), (
        f"approval_schema.{constant} and 008's CHECK on {table}.{column} "
        f"disagree")


def test_contract_2_statuses_are_exactly_as_frozen():
    """The contract's own lists, retyped here from docs/WAVE4_CONTRACTS.md
    rather than read from either the migration or approval_schema.

    A third, independent copy on purpose: the test above proves the module and
    the migration agree with EACH OTHER, which they would also do if both were
    wrong in the same way. This one proves they agree with the contract."""
    assert approval_schema.INSTANCE_STATUSES == (
        "OPEN", "APPROVED", "REJECTED", "RETURNED", "RECALLED", "CANCELLED",
        "SUPERSEDED", "EXCEPTION_PENDING")
    assert approval_schema.STAGE_STATUSES == (
        "PENDING", "APPROVED", "REJECTED", "RETURNED", "SKIPPED", "ESCALATED")
    assert approval_schema.ASSIGNMENT_STATES == ("PENDING", "ACTED", "WITHDRAWN")
    assert approval_schema.DEFINITION_STATUSES == ("DRAFT", "ACTIVE", "RETIRED")


def test_an_unrouted_instance_cannot_be_anything_but_exception_pending():
    """Contract 2's "no route to auto-approval", as a constraint rather than a
    convention. Without this, an instance that resolved no definition is an
    ordinary OPEN row that any UPDATE could walk to APPROVED."""
    assert _has_named_constraint("approval_instance", "ck_approval_instance_definition")
    body = _table_body("approval_instance")
    clause = re.search(
        r"CONSTRAINT ck_approval_instance_definition CHECK \((.*?)\n    \),",
        body, re.DOTALL)
    assert clause is not None
    assert "EXCEPTION_PENDING" in clause.group(1), (
        "the unrouted branch must pin status to EXCEPTION_PENDING")


def test_a_skipped_stage_must_say_why():
    """Contract 2: a stage that did not run is recorded SKIPPED with a
    skip_reason, "never omitted, because an auditor must see what did not
    run". That is only true if the row cannot be stored without it."""
    assert _has_named_constraint(
        "approval_stage_instance", "ck_approval_stage_instance_skip_reason")


# ============================================ constraints that must exist
@pytest.mark.parametrize(("table", "constraint", "why"), [
    ("approval_definition", "uq_approval_definition_version",
     "UNIQUE (object_type, code, version) -- a version is an identity"),
    ("approval_stage", "ck_approval_stage_quorum",
     "quorum_n must be coherent with quorum_type"),
    ("approval_stage", "uq_approval_stage_stage_no",
     "stage_no unique per definition"),
    ("approval_rule", "uq_approval_rule_priority",
     "priority unique per definition -- a tie leaves routing to the planner"),
    ("approval_stage_instance", "uq_approval_stage_instance_stage_no",
     "one stage instance per (instance, stage_no)"),
    ("approval_assignment", "uq_approval_assignment_assignee",
     "one seat per assignee per stage -- else one person meets a 2-of-3 quorum"),
    ("approval_delegation", "ck_approval_delegation_range_not_empty",
     "an empty daterange authorises nothing while looking like a grant"),
    ("approval_action", "uq_approval_action_seq",
     "the hash chain's per-instance ordering, as audit_log has"),
])
def test_the_named_constraint_exists(table, constraint, why):
    assert _has_named_constraint(table, constraint), f"{table}: missing {constraint} ({why})"


def test_the_quorum_constraint_covers_every_quorum_type():
    """Each of the four quorum types is named in the constraint. A type absent
    from every branch would make the whole CHECK false for it, so the stage
    could never be stored at all -- a different bug, equally silent."""
    body = _table_body("approval_stage")
    clause = re.search(
        r"CONSTRAINT ck_approval_stage_quorum CHECK \((.*?)\n    \),",
        body, re.DOTALL)
    assert clause is not None
    for quorum_type in approval_schema.QUORUM_TYPES:
        assert f"'{quorum_type}'" in clause.group(1), (
            f"ck_approval_stage_quorum says nothing about {quorum_type}")
    assert "100" in clause.group(1), "PERCENT must be bounded at 100"


def test_the_delegation_range_check_uses_isempty():
    """`NOT isempty(...)` specifically: PostgreSQL normalises `[d, d)` to the
    empty range, so a length comparison or a NOT NULL would both miss the most
    natural way to typo a one-day delegation."""
    body = _table_body("approval_delegation")
    assert re.search(r"CHECK\s*\(\s*NOT isempty\(active_range\)\s*\)", body)


# ============================================== immutability, as declared
@pytest.mark.parametrize(("table", "triggers"),
                         sorted(approval_schema.IMMUTABILITY_TRIGGERS.items()))
def test_every_immutability_trigger_is_declared(table, triggers):
    for trigger in triggers:
        assert re.search(
            rf"CREATE TRIGGER {trigger}\b[^;]*?\bON {table}\b", CODE, re.DOTALL), (
            f"{trigger} on {table} is not declared in 008")


def test_every_trigger_function_is_declared():
    declared = re.findall(r"CREATE OR REPLACE FUNCTION (\w+)\(\) RETURNS trigger", CODE)
    assert sorted(declared) == sorted(approval_schema.IMMUTABILITY_FUNCTIONS)


def test_a_live_definition_and_its_children_are_guarded_on_update_and_delete():
    """Contract 1's first immutability rule, table by table. Both operations
    for all four tables -- a table guarded on UPDATE but not DELETE is not
    immutable, it is merely inconvenient."""
    for table in ("approval_definition", "approval_rule", "approval_stage",
                  "approval_stage_approver"):
        for operation in ("UPDATE", "DELETE"):
            assert re.search(
                rf"CREATE TRIGGER \w+ BEFORE {operation} ON {table}\b", CODE), (
                f"{table} has no BEFORE {operation} immutability trigger")


def test_the_definition_trigger_fires_only_for_active_and_retired():
    """A DRAFT must stay freely editable. The WHEN clause is what draws that
    line -- without it the trigger would fire for a DRAFT too and there would
    be no way to author a workflow at all."""
    statements = re.findall(
        r"CREATE TRIGGER approval_definition_immutable_\w+ BEFORE \w+ ON "
        r"approval_definition(.*?)EXECUTE FUNCTION", CODE, re.DOTALL)
    assert len(statements) == 2, "expected an UPDATE and a DELETE trigger"
    for statement in statements:
        assert _squash(statement) == (
            "FOR EACH ROW WHEN (OLD.status IN ('ACTIVE', 'RETIRED'))"), (
            "the guard must fire for ACTIVE and RETIRED and for nothing else; "
            f"found: {_squash(statement)}")


def test_approval_action_is_append_only_by_trigger_and_by_privilege():
    """Exactly the treatment audit_log gets in 001: both mechanisms, because
    neither alone is enough. A superuser bypasses the privilege but not the
    trigger; a future blanket GRANT would undo the privilege but not the
    trigger."""
    assert re.search(r"CREATE TRIGGER approval_action_no_update BEFORE UPDATE ON approval_action", CODE)
    assert re.search(r"CREATE TRIGGER approval_action_no_delete BEFORE DELETE ON approval_action", CODE)
    assert re.search(
        r"REVOKE UPDATE, DELETE ON approval_action FROM capex_app", CODE), (
        "the REVOKE is the real guarantee; the trigger is the statement of "
        "intent. 001_foundation.sql uses both for audit_log and this must too.")


def test_the_frozen_instance_columns_are_exactly_the_three_contract_1_names():
    """snapshot, definition_version and object_content_sha -- and the WHEN
    clause must name all three, or the unnamed one drifts silently."""
    assert approval_schema.INSTANCE_FROZEN_COLUMNS == (
        "snapshot", "definition_version", "object_content_sha")
    clause = _frozen_fields_when_clause()
    assert "WHEN" in clause, "the frozen-fields trigger has no WHEN clause"
    for column in approval_schema.INSTANCE_FROZEN_COLUMNS:
        assert f"OLD.{column} IS DISTINCT FROM NEW.{column}" in clause


def test_the_frozen_fields_trigger_does_not_freeze_the_whole_row():
    """status, current_stage_no and closed_at must stay updatable -- advancing
    an instance is the engine's job. A table-wide refusal would make the
    engine unimplementable, so assert the guard is column-scoped."""
    clause = _frozen_fields_when_clause()
    for column in ("status", "current_stage_no", "closed_at"):
        assert f"OLD.{column}" not in clause, f"{column} must remain updatable"


# ================================================================== RLS
@pytest.mark.parametrize(("table", "policy"),
                         sorted(approval_schema.RLS_POLICIES.items()))
def test_rls_is_enabled_forced_and_policied(table, policy):
    """All three statements, for every protected table.

    Each catches a different silent failure: a policy with no ENABLE is inert;
    an ENABLE with no FORCE is bypassed by the table's OWNER -- in production
    the deploy identity, which is not a superuser -- and that bypass is
    invisible in `pg_policies`; an ENABLE+FORCE with no policy denies
    everything, which is loud but wrong.
    """
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in CODE
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in CODE
    assert re.search(rf"CREATE POLICY {policy} ON {table}\b", CODE)


@pytest.mark.parametrize("table", sorted(approval_schema.RLS_POLICIES))
def test_every_policy_carries_using_and_with_check(table):
    """USING alone filters reads and leaves a caller free to INSERT a row into
    a scope they cannot see -- writing into another entity's estate while
    being unable to read it back."""
    policy = re.search(
        rf"CREATE POLICY \w+ ON {table}\b(.*?);", CODE, re.DOTALL)
    assert policy is not None
    assert "USING" in policy.group(1), f"{table}: policy has no USING"
    assert "WITH CHECK" in policy.group(1), f"{table}: policy has no WITH CHECK"


def test_every_policy_reaches_the_scope_function():
    for table in approval_schema.RLS_POLICIES:
        policy = re.search(rf"CREATE POLICY \w+ ON {table}\b(.*?);", CODE, re.DOTALL)
        assert policy is not None
        assert "capex_scope_permits(" in policy.group(1), (
            f"{table}'s policy does not call the frozen scope predicate")


def test_the_instance_policy_filters_on_both_of_its_own_columns():
    """Contract 6 again, on the policy this time: entity and project, directly,
    with plant and location waived because the table has no column for them."""
    policy = re.search(
        r"CREATE POLICY approval_instance_scope ON approval_instance(.*?);",
        CODE, re.DOTALL)
    assert policy is not None
    assert policy.group(1).count(
        "capex_scope_permits(entity_id, NULL, NULL, project_id)") == 2, (
        "both USING and WITH CHECK must filter on entity_id and project_id")


def test_the_action_policy_reaches_through_instance_id_not_stage_instance_id():
    """stage_instance_id is NULL for RECALL and CANCEL, so a policy joining
    through it would make exactly those two action kinds invisible -- an
    audit-trail hole that only shows up for recalled documents."""
    policy = re.search(
        r"CREATE POLICY approval_action_scope ON approval_action(.*?);",
        CODE, re.DOTALL)
    assert policy is not None
    assert "approval_action.instance_id" in policy.group(1)
    assert "stage_instance_id" not in policy.group(1)


def test_008_does_not_redefine_the_frozen_scope_functions():
    """004 declares them, 007 replaced their bodies, and both are frozen. Two
    migrations editing one function body is the collision the numbering exists
    to avoid."""
    for frozen in ("capex_scope_permits", "capex_dimension_permits",
                   "capex_principal_present"):
        assert not re.search(
            rf"CREATE\s+(OR REPLACE\s+)?FUNCTION\s+{frozen}\b", CODE), (
            f"008 must call {frozen}, never redefine it")


def test_008_alters_no_table_an_earlier_migration_created():
    """Expand-only. The only ALTER TABLE statements permitted are the RLS
    ENABLE/FORCE pair, and only on 008's own tables."""
    for table in re.findall(r"ALTER TABLE (\w+)", CODE):
        assert table in approval_schema.APPROVAL_TABLES, (
            f"008 alters {table}, which it does not create")


# ============================================================ ROLLBACK section
def test_the_migration_declares_a_rollback_section():
    assert "-- ROLLBACK:" in SQL


def test_the_rollback_section_names_every_object_the_migration_creates():
    """A rollback that forgets an object leaves it behind, and the next attempt
    to re-apply the migration fails on a duplicate the operator cannot see."""
    rollback = SQL.split("-- ROLLBACK:", 1)[1].split("\n\n", 1)[0]
    for table in approval_schema.APPROVAL_TABLES:
        assert table in rollback, f"ROLLBACK does not mention {table}"
    for policy in approval_schema.RLS_POLICIES.values():
        assert f"DROP POLICY IF EXISTS {policy}" in rollback
    for triggers in approval_schema.IMMUTABILITY_TRIGGERS.values():
        for trigger in triggers:
            assert f"DROP TRIGGER IF EXISTS {trigger}" in rollback
    for function in approval_schema.IMMUTABILITY_FUNCTIONS:
        assert f"DROP FUNCTION IF EXISTS {function}" in rollback
    for table in approval_schema.RLS_POLICIES:
        assert f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY" in rollback


# ================================================= approval_schema vs migration
def test_approval_schema_columns_match_the_migration_in_both_directions():
    """The anti-drift assertion. A column added to the migration and not to
    the constants module leaves streams 2/3/5 unable to name it; a column in
    the module that the migration does not have produces SQL that fails only
    when the query finally runs."""
    for table in approval_schema.APPROVAL_TABLES:
        assert approval_schema.COLUMNS[table] == tuple(_columns(table)), (
            f"approval_schema.COLUMNS[{table!r}] and 008 disagree")
    assert set(approval_schema.COLUMNS) == set(approval_schema.APPROVAL_TABLES)


def test_approval_schema_table_constants_are_the_real_table_names():
    for table in approval_schema.APPROVAL_TABLES:
        assert re.search(rf"^CREATE TABLE {table} \(", CODE, re.MULTILINE)


def test_the_migration_constant_names_this_file():
    assert approval_schema.APPROVAL_MIGRATION == MIGRATION_PATH.name


def test_the_audit_stream_key_matches_contract_9():
    assert approval_schema.audit_stream_key("APX-1") == "approval:APX-1"


def test_open_instance_statuses_are_a_subset_of_the_instance_statuses():
    assert set(approval_schema.OPEN_INSTANCE_STATUSES) <= set(
        approval_schema.INSTANCE_STATUSES)


def test_append_only_tables_are_exactly_the_revoked_ones():
    revoked = re.findall(r"REVOKE UPDATE, DELETE ON ([\w, ]+?) FROM capex_app", CODE)
    assert revoked, "no REVOKE found"
    named = {t.strip() for line in revoked for t in line.split(",")}
    assert named == set(approval_schema.APPEND_ONLY_TABLES)


# ================================================== the scope inventory
def test_every_table_008_creates_is_classified_in_the_scope_inventory():
    """The inventory is hand-maintained and independent of the migration; this
    proves it was actually maintained. `tests/test_pg_rls_coverage.py` makes
    the same sweep across every migration -- this narrower one names 008, so a
    failure here says which file was forgotten."""
    classified = set(scope_inventory.tables_requiring_rls()) | set(
        scope_inventory.UNSCOPED_TABLES)
    missing = sorted(set(approval_schema.APPROVAL_TABLES) - classified)
    assert missing == [], (
        f"008 tables classified neither scoped nor deliberately unscoped: {missing}")


def test_the_inventory_lists_every_table_008_actually_protects():
    """Each table with a policy in 008 appears in SCOPED_TABLES, attributed to
    008, and none of them is still recorded as a gap."""
    by_name = {e.table: e for e in scope_inventory.SCOPED_TABLES}
    for table in approval_schema.RLS_POLICIES:
        assert table in by_name, f"{table} is protected by 008 but not in SCOPED_TABLES"
        assert by_name[table].migration == approval_schema.APPROVAL_MIGRATION
        assert by_name[table].status != "gap", (
            f"{table} is protected by 008; recording it as a gap is a lie")


def test_the_unprotected_008_tables_are_deliberate_and_carry_a_reason():
    """reason_code and approval_delegation get no policy. That must be a
    recorded decision, not an omission -- the exact failure the inventory
    exists to make impossible."""
    unprotected = set(approval_schema.APPROVAL_TABLES) - set(
        approval_schema.RLS_POLICIES)
    assert unprotected == {"reason_code", "approval_delegation"}
    for table in unprotected:
        assert scope_inventory.UNSCOPED_TABLES.get(table, "").strip(), (
            f"{table} carries no RLS and no recorded reason")


def test_the_pending_registry_handoff_is_enumerable():
    """008's protected tables are not yet in `rls.py`'s registry, which is
    lead-owned and frozen for this wave. The inventory records that in-between
    state rather than calling it 'covered' (untrue: the registry half is
    missing, and the coverage test asserts set equality against it) or 'gap'
    (untrue: the policies exist). `pending_registry_tables()` is the handoff
    list; when `rls.py` gains them, 008's entries become 'covered' and this
    test's set becomes empty.

    NARROWED TO 008 in Wave 5 (ADAPT: 2026-09-06). It read
    `set(pending_registry_tables()) == set(RLS_POLICIES)`, which asserted two
    things at once: that every table 008 protects is awaiting the registry,
    and that 008 is the ONLY migration with tables awaiting it. The second was
    incidentally true when it was written and stopped being true when
    `010_integration.sql` landed eight more in the same state.

    The equality over 008's own tables is unchanged and still exact -- a
    missing one and a spurious one both still fail. What is gone is a claim
    about other migrations that this file is not the place to make; 010's half
    of the same handoff is asserted by
    `tests/test_pg_integration_schema.py`, and the global invariant that every
    table is classified at all is `test_pg_rls_coverage.py`'s.
    """
    pending_from_008 = set(
        scope_inventory.pending_registry_tables()
    ) & set(scope_inventory.tables_for_migration(
        approval_schema.APPROVAL_MIGRATION))
    assert pending_from_008 == set(approval_schema.RLS_POLICIES)


# ============================================================ the seed fragment
def test_the_seed_fragment_is_discovered_by_the_loader():
    from app.backend.pg import seed
    assert SEED_PART_PATH in seed.seed_part_files()


def test_the_seed_builds_the_contract_scenario():
    """The demo estate must actually contain the shapes the end-to-end
    scenario needs, not merely be syntactically valid SQL."""
    assert "'PG-OPS'" in SEED_SQL, "no parallel group in the demo workflow"
    assert "'N_OF_M', 2" in SEED_SQL, "no N_OF_M stage in the demo workflow"
    assert "INSERT INTO reason_code" in SEED_SQL
    assert "INSERT INTO approval_delegation" in SEED_SQL
    assert "revoked_at, revoke_reason" in SEED_SQL
    # The over-budget exception route, and its Finance gate.
    assert "Finance exception approval" in SEED_SQL
    assert "exceeds_available" in SEED_SQL


def test_the_seed_activates_definitions_rather_than_inserting_them_live():
    """Every definition is inserted DRAFT and then UPDATEd to ACTIVE -- the
    lifecycle the API has. Inserting one ACTIVE would work (the triggers guard
    UPDATE and DELETE, not INSERT) but would quietly depend on that gap."""
    assert "'DRAFT'" in SEED_SQL
    assert SEED_SQL.count("SET status       = 'ACTIVE'") == 3
    assert "INSERT INTO approval_definition" in SEED_SQL
    definition_inserts = re.findall(
        r"INSERT INTO approval_definition.*?VALUES(.*?);", SEED_SQL, re.DOTALL)
    for block in definition_inserts:
        assert "'ACTIVE'" not in block, (
            "a definition is inserted already ACTIVE; insert it DRAFT and "
            "activate it, as the API does")


def test_the_seed_money_is_integer_paise():
    """Rs 50,00,000 as 500000000 paise. No decimal point anywhere near a money
    literal: money is integer paise everywhere in this codebase, and a jsonb
    predicate is not exempt just because the migration runner's *_paise column
    check cannot reach inside one."""
    assert "500000000" in SEED_SQL
    assert not re.search(r'"value":\s*\d+\.\d', SEED_SQL), (
        "a decimal money literal in a rule predicate")


# ============================================================================
# LIVE TESTS -- everything below needs a real PostgreSQL.
#
# A text check proves a trigger is declared. Only these prove it fires, that a
# CHECK rejects the row it names, and that a policy hides anything. They are
# skipped where CAPEX_DB_URL is unset (there is no local PostgreSQL on the
# development machine) and run for the first time in CI's `pg_tests` job.
# ============================================================================
ENTITY_A, ENTITY_B = "ENT-A", "ENT-B"
PROJECT_A, PROJECT_B = "PRJ-A", "PRJ-B"
MAKER = "U-MAKER"


def _build_estate(conn) -> None:
    """Organisation, two entities, two projects and one user, committed.

    Two entities because a scope test needs an out-of-scope row to fail to see;
    one entity would let a policy that returns TRUE unconditionally pass.
    Runs as the connection's own (superuser) role, which bypasses RLS -- that
    is what makes it usable to plant rows a scoped role must NOT see.
    """
    conn.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
        "VALUES ('ORG-1', 'ORG1', 'Test Org', 'T', 'T')")
    for entity in (ENTITY_A, ENTITY_B):
        conn.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
            "VALUES (%s, 'ORG-1', %s, %s, 'T', 'T')", (entity, entity, entity))
    for project, entity in ((PROJECT_A, ENTITY_A), (PROJECT_B, ENTITY_B)):
        conn.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 'T', 'T')", (project, entity, project, project))
    conn.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
        "VALUES (%s, 'maker@example.test', 'Maker', 'T', 'T')", (MAKER,))
    conn.commit()


def _definition(conn, definition_id: str, *, status: str, entity: str = ENTITY_A,
                version: int = 1, code: str = "WF") -> None:
    """Insert a definition DRAFT, then activate/retire it as asked -- the only
    route to ACTIVE that the triggers permit."""
    conn.execute(
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        "status, entity_id, effective_from, created_by) "
        "VALUES (%s, 'PURCHASE_REQUEST', %s, %s, 'DRAFT', %s, '2026-04-01', 'T')",
        (definition_id, code, version, entity))
    if status != "DRAFT":
        conn.execute(
            "UPDATE approval_definition SET status = 'ACTIVE', activated_at = now(), "
            "activated_by = 'T' WHERE definition_id = %s", (definition_id,))
    if status == "RETIRED":
        conn.execute(
            "UPDATE approval_definition SET status = 'RETIRED', retired_at = now(), "
            "retired_by = 'T' WHERE definition_id = %s", (definition_id,))
    conn.commit()


def _instance(conn, instance_id: str, *, entity: str = ENTITY_A,
              project: str | None = PROJECT_A, definition_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO approval_instance (instance_id, object_type, object_id, "
        "object_version, object_content_sha, definition_id, definition_version, "
        "status, snapshot, maker_user_id, entity_id, project_id) "
        "VALUES (%s, 'PURCHASE_REQUEST', %s, 1, 'sha-1', %s, %s, %s, '{}'::jsonb, "
        "%s, %s, %s)",
        (instance_id, instance_id, definition_id,
         1 if definition_id else None,
         "OPEN" if definition_id else "EXCEPTION_PENDING",
         MAKER, entity, project))
    conn.commit()


def _refused(conn, statement, params=None, *, expect: str = "") -> str:
    """Assert PostgreSQL refuses `statement`; return the message.

    Rolls back either way, so one expected failure never poisons the
    transaction for the next assertion in the same test.
    """
    import psycopg
    try:
        conn.execute(statement, params)
    except psycopg.Error as exc:
        conn.rollback()
        message = str(exc)
        if expect:
            assert expect.lower() in message.lower(), (
                f"refused, but not for the stated reason.\nexpected {expect!r} "
                f"in: {message}")
        return message
    conn.rollback()
    raise AssertionError(
        f"expected PostgreSQL to refuse this, and it did not: {statement}")


def _scoped_to(entity: str) -> Scope:
    """A restricted principal: one entity, nothing else, not read_all."""
    return Scope(
        user_id="U-SCOPED", principal_kind="USER",
        entity_ids=frozenset({entity}), plant_ids=None,
        project_ids=None, location_ids=None, read_all=False)


# ------------------------------------------------- immutability, for real
@pytest.mark.pg
@PG
def test_an_active_definition_refuses_update_and_delete_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-ACTIVE", status="ACTIVE")

    _refused(pg_connection,
             "UPDATE approval_definition SET code = 'CHANGED' WHERE definition_id = 'D-ACTIVE'",
             expect="immutable")
    _refused(pg_connection,
             "UPDATE approval_definition SET effective_to = '2027-01-01' "
             "WHERE definition_id = 'D-ACTIVE'",
             expect="immutable")
    _refused(pg_connection,
             "DELETE FROM approval_definition WHERE definition_id = 'D-ACTIVE'",
             expect="immutable")


@pytest.mark.pg
@PG
def test_a_draft_definition_is_freely_editable_live(pg_connection):
    """The other half of the rule, and the one a blanket trigger would break:
    if a DRAFT could not be edited, no workflow could ever be authored."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-DRAFT", status="DRAFT")

    pg_connection.execute(
        "UPDATE approval_definition SET code = 'EDITED' WHERE definition_id = 'D-DRAFT'")
    pg_connection.commit()
    row = pg_connection.execute(
        "SELECT code FROM approval_definition WHERE definition_id = 'D-DRAFT'").fetchone()
    assert row[0] == "EDITED"

    pg_connection.execute("DELETE FROM approval_definition WHERE definition_id = 'D-DRAFT'")
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT count(*) FROM approval_definition").fetchone()[0] == 0


@pytest.mark.pg
@PG
def test_retirement_is_the_only_permitted_change_to_a_live_definition_live(pg_connection):
    """Contract 1 requires both immutability AND retirement. The trigger
    permits exactly the one transition that retirement needs, and nothing
    rides along with it."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-RET", status="ACTIVE")

    # Retirement alone: permitted.
    pg_connection.execute(
        "UPDATE approval_definition SET status = 'RETIRED', retired_at = now(), "
        "retired_by = 'T' WHERE definition_id = 'D-RET'")
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT status FROM approval_definition WHERE definition_id = 'D-RET'"
    ).fetchone()[0] == "RETIRED"

    # RETIRED is terminal.
    _refused(pg_connection,
             "UPDATE approval_definition SET retired_by = 'SOMEONE' "
             "WHERE definition_id = 'D-RET'",
             expect="terminal")
    _refused(pg_connection,
             "DELETE FROM approval_definition WHERE definition_id = 'D-RET'",
             expect="immutable")


@pytest.mark.pg
@PG
def test_retirement_cannot_smuggle_another_change_with_it_live(pg_connection):
    """The narrow permission must be narrow. Retiring a definition while also
    rewriting its code would be exactly the retroactive edit the rule forbids,
    wearing retirement as a disguise."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-SMUGGLE", status="ACTIVE")
    _refused(pg_connection,
             "UPDATE approval_definition SET status = 'RETIRED', retired_at = now(), "
             "retired_by = 'T', code = 'SNEAKY' WHERE definition_id = 'D-SMUGGLE'",
             expect="immutable")


@pytest.mark.pg
@PG
@pytest.mark.parametrize("child", ["approval_rule", "approval_stage"])
def test_children_of_an_active_definition_refuse_update_and_delete_live(
        pg_connection, child):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-KIDS", status="DRAFT")
    if child == "approval_rule":
        pg_connection.execute(
            "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
            "created_by) VALUES ('R-1', 'D-KIDS', 10, '{}'::jsonb, 'T')")
        update = "UPDATE approval_rule SET priority = 20 WHERE rule_id = 'R-1'"
        delete = "DELETE FROM approval_rule WHERE rule_id = 'R-1'"
    else:
        pg_connection.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "quorum_type, created_by) VALUES ('S-1', 'D-KIDS', 1, 'One', 'ALL', 'T')")
        update = "UPDATE approval_stage SET name = 'Two' WHERE stage_id = 'S-1'"
        delete = "DELETE FROM approval_stage WHERE stage_id = 'S-1'"
    pg_connection.commit()

    # While the parent is a DRAFT, the child is an ordinary row.
    pg_connection.execute(update)
    pg_connection.commit()

    pg_connection.execute(
        "UPDATE approval_definition SET status = 'ACTIVE', activated_at = now(), "
        "activated_by = 'T' WHERE definition_id = 'D-KIDS'")
    pg_connection.commit()

    _refused(pg_connection, update, expect="live workflow is immutable")
    _refused(pg_connection, delete, expect="live workflow is immutable")


@pytest.mark.pg
@PG
def test_stage_approvers_of_an_active_definition_are_immutable_live(pg_connection):
    """Two joins from the definition, and the one child that reaches its parent
    through `stage_id` rather than carrying `definition_id` itself -- the case
    the shared trigger function has a separate branch for."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-APPR", status="DRAFT")
    pg_connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "quorum_type, created_by) VALUES ('S-A', 'D-APPR', 1, 'One', 'ALL', 'T')")
    pg_connection.execute(
        "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
        "approver_ref) VALUES ('S-A', 1, 'ROLE', 'Finance')")
    pg_connection.commit()

    pg_connection.execute(
        "UPDATE approval_stage_approver SET approver_ref = 'CFO' WHERE stage_id = 'S-A'")
    pg_connection.commit()

    pg_connection.execute(
        "UPDATE approval_definition SET status = 'ACTIVE', activated_at = now(), "
        "activated_by = 'T' WHERE definition_id = 'D-APPR'")
    pg_connection.commit()

    _refused(pg_connection,
             "UPDATE approval_stage_approver SET approver_ref = 'X' WHERE stage_id = 'S-A'",
             expect="live workflow is immutable")
    _refused(pg_connection,
             "DELETE FROM approval_stage_approver WHERE stage_id = 'S-A'",
             expect="live workflow is immutable")


@pytest.mark.pg
@PG
def test_approval_action_refuses_update_and_delete_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-ACT", status="ACTIVE")
    _instance(pg_connection, "I-ACT", definition_id="D-ACT")
    pg_connection.execute(
        "INSERT INTO approval_action (instance_id, actor_user_id, action, seq) "
        "VALUES ('I-ACT', %s, 'APPROVE', 1)", (MAKER,))
    pg_connection.commit()

    _refused(pg_connection,
             "UPDATE approval_action SET action = 'REJECT' WHERE instance_id = 'I-ACT'",
             expect="append-only")
    _refused(pg_connection,
             "DELETE FROM approval_action WHERE instance_id = 'I-ACT'",
             expect="append-only")


@pytest.mark.pg
@PG
def test_capex_app_holds_no_update_or_delete_on_approval_action_live(pg_connection):
    """The privilege half. The trigger above would refuse the write anyway;
    this asserts the REVOKE actually took, which is the mechanism that also
    binds a caller the trigger might somehow not see."""
    for privilege in ("UPDATE", "DELETE"):
        granted = pg_connection.execute(
            "SELECT has_table_privilege('capex_app', 'approval_action', %s)",
            (privilege,)).fetchone()[0]
        assert granted is False, (
            f"capex_app still holds {privilege} on approval_action")
    assert pg_connection.execute(
        "SELECT has_table_privilege('capex_app', 'approval_action', 'INSERT')"
    ).fetchone()[0] is True, "capex_app must still be able to APPEND"


@pytest.mark.pg
@PG
def test_an_instance_cannot_rewrite_what_it_froze_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-FRZ", status="ACTIVE")
    _instance(pg_connection, "I-FRZ", definition_id="D-FRZ")

    for column, value in (("snapshot", "'{\"x\": 1}'::jsonb"),
                          ("definition_version", "2"),
                          ("object_content_sha", "'sha-2'")):
        _refused(pg_connection,
                 f"UPDATE approval_instance SET {column} = {value} "
                 f"WHERE instance_id = 'I-FRZ'",
                 expect="frozen at creation")


@pytest.mark.pg
@PG
def test_an_instance_still_advances_through_its_lifecycle_live(pg_connection):
    """The frozen-fields trigger must not freeze the row. If it did, the engine
    could never move an instance from OPEN to APPROVED."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-ADV", status="ACTIVE")
    _instance(pg_connection, "I-ADV", definition_id="D-ADV")

    pg_connection.execute(
        "UPDATE approval_instance SET status = 'APPROVED', current_stage_no = 2, "
        "closed_at = now() WHERE instance_id = 'I-ADV'")
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = 'I-ADV'"
    ).fetchone()[0] == "APPROVED"


# --------------------------------------------- constraints, for real
@pytest.mark.pg
@PG
@pytest.mark.parametrize(("quorum_type", "quorum_n"), [
    ("ALL", 2),        # a count beside a type that implies its own
    ("ANY", 1),
    ("N_OF_M", None),  # N_OF_M with no N
    ("N_OF_M", 0),
    ("PERCENT", 0),    # approves with no approver at all
    ("PERCENT", 101),  # unsatisfiable: the stage could never close
])
def test_an_incoherent_quorum_is_refused_live(pg_connection, quorum_type, quorum_n):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-Q", status="DRAFT")
    _refused(pg_connection,
             "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
             "quorum_type, quorum_n, created_by) "
             "VALUES ('S-Q', 'D-Q', 1, 'Q', %s, %s, 'T')",
             (quorum_type, quorum_n),
             expect="ck_approval_stage_quorum")


@pytest.mark.pg
@PG
def test_a_coherent_quorum_is_accepted_live(pg_connection):
    """The constraint must not simply always refuse."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-QOK", status="DRAFT")
    for stage_no, (quorum_type, quorum_n) in enumerate(
            [("ALL", None), ("ANY", None), ("N_OF_M", 2), ("PERCENT", 60)], start=1):
        pg_connection.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "quorum_type, quorum_n, created_by) VALUES (%s, 'D-QOK', %s, 'Q', %s, %s, 'T')",
            (f"S-OK-{stage_no}", stage_no, quorum_type, quorum_n))
    pg_connection.commit()
    assert pg_connection.execute(
        "SELECT count(*) FROM approval_stage").fetchone()[0] == 4


@pytest.mark.pg
@PG
def test_two_stages_cannot_share_a_stage_no_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-SN", status="DRAFT")
    pg_connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "quorum_type, created_by) VALUES ('S-1', 'D-SN', 1, 'One', 'ALL', 'T')")
    pg_connection.commit()
    _refused(pg_connection,
             "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
             "quorum_type, created_by) VALUES ('S-2', 'D-SN', 1, 'Two', 'ALL', 'T')",
             expect="uq_approval_stage_stage_no")


@pytest.mark.pg
@PG
def test_one_person_cannot_hold_two_seats_at_one_stage_live(pg_connection):
    """The constraint that stops a 2-of-3 quorum being met by one person
    reachable through two roles."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-AS", status="ACTIVE")
    _instance(pg_connection, "I-AS", definition_id="D-AS")
    pg_connection.execute(
        "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
        "stage_no, quorum_required, opened_at) "
        "VALUES ('SI-1', 'I-AS', 1, 2, now())")
    pg_connection.execute(
        "INSERT INTO approval_assignment (assignment_id, stage_instance_id, "
        "assignee_user_id, assigned_via) VALUES ('A-1', 'SI-1', %s, 'ROLE')", (MAKER,))
    pg_connection.commit()
    _refused(pg_connection,
             "INSERT INTO approval_assignment (assignment_id, stage_instance_id, "
             "assignee_user_id, assigned_via) VALUES ('A-2', 'SI-1', %s, 'USER')",
             (MAKER,),
             expect="uq_approval_assignment_assignee")


@pytest.mark.pg
@PG
def test_an_empty_delegation_range_is_refused_live(pg_connection):
    """`[d, d)` normalises to empty: a "one-day" delegation that authorises
    nothing while looking exactly like a grant."""
    _build_estate(pg_connection)
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
        "VALUES ('U-DEL', 'del@example.test', 'Delegate', 'T', 'T')")
    pg_connection.commit()
    _refused(pg_connection,
             "INSERT INTO approval_delegation (delegation_id, delegator_user_id, "
             "delegate_user_id, scope_key, active_range, created_by) "
             "VALUES ('DG-1', %s, 'U-DEL', 'K', daterange('2026-07-01','2026-07-01','[)'), 'T')",
             (MAKER,),
             expect="ck_approval_delegation_range_not_empty")
    # ...and a real range is accepted, so the check is not always-refusing.
    pg_connection.execute(
        "INSERT INTO approval_delegation (delegation_id, delegator_user_id, "
        "delegate_user_id, scope_key, active_range, created_by) "
        "VALUES ('DG-2', %s, 'U-DEL', 'K', daterange('2026-07-01','2026-08-01','[)'), 'T')",
        (MAKER,))
    pg_connection.commit()


@pytest.mark.pg
@PG
def test_a_definition_version_is_unique_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-V1", status="DRAFT", version=1, code="SAME")
    _refused(pg_connection,
             "INSERT INTO approval_definition (definition_id, object_type, code, "
             "version, status, entity_id, effective_from, created_by) "
             "VALUES ('D-DUP', 'PURCHASE_REQUEST', 'SAME', 1, 'DRAFT', %s, "
             "'2026-04-01', 'T')",
             (ENTITY_A,),
             expect="uq_approval_definition_version")


@pytest.mark.pg
@PG
def test_an_unrouted_instance_cannot_be_open_live(pg_connection):
    """Fail closed: an instance with no definition is EXCEPTION_PENDING or it
    is nothing. There is no state in which "no route" reads as "in progress"
    and could later be walked to APPROVED."""
    _build_estate(pg_connection)
    _refused(pg_connection,
             "INSERT INTO approval_instance (instance_id, object_type, object_id, "
             "object_version, object_content_sha, status, snapshot, maker_user_id, "
             "entity_id, project_id) VALUES ('I-BAD', 'PURCHASE_REQUEST', 'X', 1, "
             "'sha', 'OPEN', '{}'::jsonb, %s, %s, %s)",
             (MAKER, ENTITY_A, PROJECT_A),
             expect="ck_approval_instance_definition")


@pytest.mark.pg
@PG
def test_a_skipped_stage_without_a_reason_is_refused_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-SK", status="ACTIVE")
    _instance(pg_connection, "I-SK", definition_id="D-SK")
    _refused(pg_connection,
             "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
             "stage_no, status, quorum_required) VALUES ('SI-SK', 'I-SK', 1, "
             "'SKIPPED', 0)",
             expect="ck_approval_stage_instance_skip_reason")


@pytest.mark.pg
@PG
def test_a_stage_has_an_open_time_if_and_only_if_it_is_not_skipped_live(pg_connection):
    """Both directions of `ck_approval_stage_instance_opened_at`.

    `opened_at` was `NOT NULL DEFAULT now()`, so a SKIPPED stage -- one that
    never ran -- was forced to carry a timestamp saying when it did. That is a
    falsehood on the table an auditor reads specifically to see what did NOT
    happen, so the column became nullable.

    Nullable alone is not enough, and neither is the one-directional check it
    first shipped with. "SKIPPED, or opened_at is present" still permits a
    SKIPPED row that carries a time; and had the DEFAULT been kept, any insert
    that simply forgot to mention `opened_at` would have had one stamped on and
    passed. Hence the biconditional, and hence no default: an insert says when
    the stage opened, or says that it never did, and the database refuses
    anything in between.

    Both halves are asserted here because a one-sided test would pass against
    either form of the constraint, and the weaker form is the one that was
    wrong.
    """
    _build_estate(pg_connection)
    _definition(pg_connection, "D-OA", status="ACTIVE")
    _instance(pg_connection, "I-OA", definition_id="D-OA")

    # A stage that is live must say when it opened.
    _refused(pg_connection,
             "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
             "stage_no, status, quorum_required) VALUES ('SI-OA1', 'I-OA', 1, "
             "'PENDING', 1)",
             expect="ck_approval_stage_instance_opened_at")

    # A stage that never ran must not claim it did.
    _refused(pg_connection,
             "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
             "stage_no, status, skip_reason, quorum_required, opened_at) VALUES "
             "('SI-OA2', 'I-OA', 2, 'SKIPPED', 'RULE_NOT_MET', 0, now())",
             expect="ck_approval_stage_instance_opened_at")

    # And both truthful shapes are accepted.
    pg_connection.execute(
        "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
        "stage_no, status, quorum_required, opened_at) VALUES "
        "('SI-OA3', 'I-OA', 3, 'PENDING', 1, now())")
    pg_connection.execute(
        "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
        "stage_no, status, skip_reason, quorum_required) VALUES "
        "('SI-OA4', 'I-OA', 4, 'SKIPPED', 'RULE_NOT_MET', 0)")
    pg_connection.commit()

    rows = dict(pg_connection.execute(
        "SELECT stage_instance_id, opened_at IS NULL FROM approval_stage_instance "
        "WHERE instance_id = 'I-OA'").fetchall())
    assert rows == {"SI-OA3": False, "SI-OA4": True}, (
        f"the two truthful shapes did not land as written: {rows}")


@pytest.mark.pg
@PG
def test_a_delegated_assignment_must_name_its_delegator_live(pg_connection):
    _build_estate(pg_connection)
    _definition(pg_connection, "D-DA", status="ACTIVE")
    _instance(pg_connection, "I-DA", definition_id="D-DA")
    pg_connection.execute(
        "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
        "stage_no, quorum_required, opened_at) "
        "VALUES ('SI-DA', 'I-DA', 1, 1, now())")
    pg_connection.commit()
    _refused(pg_connection,
             "INSERT INTO approval_assignment (assignment_id, stage_instance_id, "
             "assignee_user_id, assigned_via) VALUES ('A-DA', 'SI-DA', %s, 'DELEGATION')",
             (MAKER,),
             expect="ck_approval_assignment_delegated_from")


# ------------------------------------------------------- RLS, for real
@pytest.mark.pg
@PG
def test_the_instance_policy_hides_another_entitys_instances_live(pg_connection):
    """The backstop, exercised with NO application predicate at all: a raw
    `SELECT *` as the restricted `capex_app` role must still return only the
    in-scope row."""
    from app.backend.pg import rls

    _build_estate(pg_connection)
    _definition(pg_connection, "D-A", status="ACTIVE", entity=ENTITY_A)
    _definition(pg_connection, "D-B", status="ACTIVE", entity=ENTITY_B,
                version=2, code="WF-B")
    _instance(pg_connection, "I-A", entity=ENTITY_A, project=PROJECT_A,
              definition_id="D-A")
    _instance(pg_connection, "I-B", entity=ENTITY_B, project=PROJECT_B,
              definition_id="D-B")

    with rls.scoped_transaction(pg_connection, _scoped_to(ENTITY_A)):
        visible = {r[0] for r in pg_connection.execute(
            "SELECT instance_id FROM approval_instance").fetchall()}
    assert visible == {"I-A"}, (
        f"approval_instance_scope leaked another entity's rows: {visible}")


@pytest.mark.pg
@PG
@pytest.mark.parametrize(("table", "id_column"), [
    ("approval_stage_instance", "stage_instance_id"),
    ("approval_assignment", "assignment_id"),
    ("approval_action", "action_id"),
])
def test_the_child_policies_reach_scope_through_the_instance_live(
        pg_connection, table, id_column):
    """Each child table inherits the instance's filter. Built for both
    entities, then read under a scope that permits only one."""
    from app.backend.pg import rls

    _build_estate(pg_connection)
    for suffix, entity, project in (("A", ENTITY_A, PROJECT_A),
                                    ("B", ENTITY_B, PROJECT_B)):
        _definition(pg_connection, f"D-{suffix}", status="ACTIVE", entity=entity,
                    version=1, code=f"WF-{suffix}")
        _instance(pg_connection, f"I-{suffix}", entity=entity, project=project,
                  definition_id=f"D-{suffix}")
        pg_connection.execute(
            "INSERT INTO approval_stage_instance (stage_instance_id, instance_id, "
            "stage_no, quorum_required, opened_at) "
            "VALUES (%s, %s, 1, 1, now())",
            (f"SI-{suffix}", f"I-{suffix}"))
        pg_connection.execute(
            "INSERT INTO approval_assignment (assignment_id, stage_instance_id, "
            "assignee_user_id, assigned_via) VALUES (%s, %s, %s, 'ROLE')",
            (f"AS-{suffix}", f"SI-{suffix}", MAKER))
        pg_connection.execute(
            "INSERT INTO approval_action (stage_instance_id, instance_id, "
            "actor_user_id, action, seq) VALUES (%s, %s, %s, 'APPROVE', 1)",
            (f"SI-{suffix}", f"I-{suffix}", MAKER))
    pg_connection.commit()

    with rls.scoped_transaction(pg_connection, _scoped_to(ENTITY_A)):
        rows = pg_connection.execute(
            f"SELECT {id_column} FROM {table}").fetchall()  # noqa: S608 -- fixed literals
    assert len(rows) == 1, (
        f"{table}'s policy returned {len(rows)} rows for a single-entity scope")


@pytest.mark.pg
@PG
def test_the_configuration_policies_hide_another_entitys_workflow_live(pg_connection):
    """A caller restricted to one entity must not be able to enumerate another
    entity's workflow, its thresholds or its named approvers."""
    from app.backend.pg import rls

    _build_estate(pg_connection)
    for suffix, entity in (("A", ENTITY_A), ("B", ENTITY_B)):
        pg_connection.execute(
            "INSERT INTO approval_definition (definition_id, object_type, code, "
            "version, status, entity_id, effective_from, created_by) "
            "VALUES (%s, 'PURCHASE_REQUEST', %s, 1, 'DRAFT', %s, '2026-04-01', 'T')",
            (f"D-{suffix}", f"WF-{suffix}", entity))
        pg_connection.execute(
            "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
            "created_by) VALUES (%s, %s, 10, '{}'::jsonb, 'T')",
            (f"R-{suffix}", f"D-{suffix}"))
        pg_connection.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "quorum_type, created_by) VALUES (%s, %s, 1, 'One', 'ALL', 'T')",
            (f"S-{suffix}", f"D-{suffix}"))
        pg_connection.execute(
            "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
            "approver_ref) VALUES (%s, 1, 'ROLE', 'Finance')", (f"S-{suffix}",))
    pg_connection.commit()

    with rls.scoped_transaction(pg_connection, _scoped_to(ENTITY_A)):
        for table, column in (("approval_definition", "definition_id"),
                              ("approval_rule", "rule_id"),
                              ("approval_stage", "stage_id"),
                              ("approval_stage_approver", "stage_id")):
            rows = pg_connection.execute(
                f"SELECT {column} FROM {table}").fetchall()  # noqa: S608 -- fixed literals
            assert len(rows) == 1, f"{table} leaked: {rows}"
            assert rows[0][0].endswith("-A")


@pytest.mark.pg
@PG
def test_an_organisation_wide_definition_is_visible_to_every_scope_live(pg_connection):
    """entity_id NULL waives the dimension, so an org-wide workflow is visible
    to everyone. This is the documented waiver semantics, and it is asserted
    rather than assumed because it is the one branch where the policy returns
    TRUE for a row carrying no entity at all."""
    from app.backend.pg import rls

    _build_estate(pg_connection)
    pg_connection.execute(
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        "status, entity_id, effective_from, created_by) "
        "VALUES ('D-ORG', 'BUDGET_REVISION', 'ORGWIDE', 1, 'DRAFT', NULL, "
        "'2026-04-01', 'T')")
    pg_connection.commit()

    for entity in (ENTITY_A, ENTITY_B):
        with rls.scoped_transaction(pg_connection, _scoped_to(entity)):
            rows = pg_connection.execute(
                "SELECT definition_id FROM approval_definition").fetchall()
        assert [r[0] for r in rows] == ["D-ORG"]


@pytest.mark.pg
@PG
def test_an_unscoped_session_reads_no_approval_row_live(pg_connection):
    """Fail closed. A connection that never went through `Database.session()`
    -- capex_app used directly, a pooled connection whose SET LOCAL was rolled
    back -- carries no scope settings and must read nothing."""
    _build_estate(pg_connection)
    _definition(pg_connection, "D-NS", status="ACTIVE")
    _instance(pg_connection, "I-NS", definition_id="D-NS")

    pg_connection.execute("SET LOCAL ROLE capex_app")
    for table in approval_schema.RLS_POLICIES:
        count = pg_connection.execute(
            f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 -- fixed literals
        assert count == 0, (
            f"{table} returned {count} rows to a session carrying no scope at all")
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_the_with_check_half_refuses_an_out_of_scope_insert_live(pg_connection):
    """USING hides rows; WITH CHECK is what stops a caller writing INTO a scope
    they cannot see. Without it, a principal confined to entity A could create
    an approval instance in entity B and simply not be able to read it back."""
    from app.backend.pg import rls
    import psycopg

    _build_estate(pg_connection)
    _definition(pg_connection, "D-WC", status="ACTIVE", entity=ENTITY_B,
                code="WF-B")

    with pytest.raises(psycopg.Error):
        with rls.scoped_transaction(pg_connection, _scoped_to(ENTITY_A)):
            pg_connection.execute(
                "INSERT INTO approval_instance (instance_id, object_type, object_id, "
                "object_version, object_content_sha, definition_id, "
                "definition_version, status, snapshot, maker_user_id, entity_id, "
                "project_id) VALUES ('I-WC', 'PURCHASE_REQUEST', 'X', 1, 'sha', "
                "'D-WC', 1, 'OPEN', '{}'::jsonb, %s, %s, %s)",
                (MAKER, ENTITY_B, PROJECT_B))
