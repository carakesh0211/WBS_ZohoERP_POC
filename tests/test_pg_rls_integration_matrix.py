"""Row-level security on the integration platform: BEHAVIOUR, not declaration.

Why this file exists
====================
`tests/test_pg_integration_schema.py` asserted, for every table 010 creates,
that the migration TEXT contains `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL
SECURITY` and a `CREATE POLICY`. `tests/test_pg_rls_coverage.py` asserted, live,
that `pg_class.relrowsecurity` and `relforcerowsecurity` are true. Both are
useful and both are checks that a policy EXISTS.

None of them could catch what was actually wrong, which was that no policy on
these nine tables had ever been ENFORCED against anything, because every live
test connected as a role no policy applies to:

* `.github/workflows/ci.yml` starts the service container with
  `POSTGRES_USER: capex` and sets
  `CAPEX_DB_URL=postgresql://capex:capex@localhost:5432/postgres`.
* The official `postgres` image creates `POSTGRES_USER` as a cluster
  **SUPERUSER**.
* A superuser **bypasses row-level security unconditionally**. `FORCE ROW LEVEL
  SECURITY` does not change this: it governs whether the table's OWNER is
  subject to its own policies, and says nothing about superusers.
* `tests/conftest_pg.py::_config_and_provider` takes its user from that URL, so
  `pg_database` -- and every `Database` built from it -- is exempt too.
  `Database.session()` applies the `capex.*` settings the policies read and
  then never reaches a policy.

So the first test in this repository that asked a policy on an integration
table to hide a row (`test_live_rls_hides_another_entitys_receipts`) failed the
moment it was written, reporting `['IB-ENT-A', 'IB-ENT-B'] == ['IB-ENT-A']`.
The policy was correct. The session was not.

`tests/conftest_pg.py::ScopedRoleDatabase` closes that by issuing
`SET LOCAL ROLE capex_app` -- making `current_user` in the test transaction
exactly what it is in production, where the application connects as `capex_app`
outright (`app/backend/pg/config.py`'s `DatabaseConfig` default). Nothing about
the policies changed; nothing is disabled, relaxed or granted differently.

What this file proves
=====================
`test_live_the_connecting_role_bypasses_rls_exactly_when_it_claims_to` runs the
direct question -- `SELECT current_user, rolsuper, rolbypassrls` -- and then
ties it to observed behaviour, so the diagnosis is asserted rather than
narrated. It is also the mutation control for everything below: if the
scoped-role sessions saw one row only because the seed were wrong, this test
(reading the same rows with RLS bypassed) would see one row too.

The matrix itself checks THREE points per table, not one. One point is not
enough: a policy that denies everything hides the other entity's rows just as
well as a correct one, and a test that only asserts "ENT-B is invisible" passes
against it. So every table is asserted at

    scope {ENT-A}         -> exactly ENT-A's row
    scope {ENT-A, ENT-B}  -> both rows          (the policy is not deny-all)
    scope frozenset()     -> nothing            (the empty set is not "all")

plus the write side (`WITH CHECK`), plus an unscoped session, plus the payload
and money columns that are the actual reason any of this matters.

A skip is not a pass. There is no PostgreSQL on the machine this file was
written on; everything marked `@PG` below skipped locally and CI is the only
place it has ever executed.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- without this
# the live tests fail at SETUP with "fixture not found", but only where
# CAPEX_DB_URL is set, so the gap would be invisible locally.
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

from app.backend.pg import rls  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "pg"

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# =========================================================================
# Database-free: the argument order every policy passes to the frozen
# scope function. Runs everywhere, including on a machine with no
# PostgreSQL -- which is where this defect class is cheapest to catch.
# =========================================================================

#: `capex_scope_permits(p_entity_id, p_plant_id, p_location_id, p_project_id)`
#: -- `migrations/pg/004_identity_scope.sql`. Positional, four `text`
#: arguments of the same type, so PostgreSQL accepts ANY order and the
#: mistake is invisible to the parser, to `\df`, and to every text-based
#: test that only checks the function is called at all.
_SCOPE_ARGUMENT_ORDER = ("entity_id", "plant_id", "location_id", "project_id")


def _split_top_level(argument_text: str) -> list[str]:
    """`argument_text` split on commas that are not inside parentheses.

    A policy argument can itself be a call, so a plain `.split(",")` would
    tear one in half and silently mis-position everything after it.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in argument_text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts]


def _scope_permits_call_sites() -> list[tuple[str, int, list[str]]]:
    """`(migration filename, line number, [four argument expressions])` for
    every `capex_scope_permits(...)` CALL in every migration, with `--` line
    comments stripped so a call quoted in a ROLLBACK note is not audited as if
    it were live SQL.

    Two non-calls name the function with four arguments and must not be audited
    as call sites: its own `CREATE OR REPLACE FUNCTION` (whose "arguments" are
    parameter DECLARATIONS -- `p_entity_id text`) and the `GRANT EXECUTE ON
    FUNCTION` naming its signature (`text, text, text, text`). Both are
    preceded by the keyword `FUNCTION`, which is what distinguishes them; the
    first draft of this scan flagged all four positions of each and buried the
    one real finding under eight false ones.
    """
    sites: list[tuple[str, int, list[str]]] = []
    for path in sorted(MIGRATIONS_DIR.glob("0*.sql")):
        code = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for match in re.finditer(r"capex_scope_permits\s*\(", code):
            preceding = code[max(0, match.start() - 40):match.start()]
            if re.search(r"\bFUNCTION\s+$", preceding, re.IGNORECASE):
                continue
            opening = match.end() - 1
            depth = 0
            for index in range(opening, len(code)):
                if code[index] == "(":
                    depth += 1
                elif code[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            arguments = _split_top_level(code[opening + 1:index])
            if len(arguments) != 4:          # the CREATE FUNCTION itself
                continue
            line = code.count("\n", 0, match.start()) + 1
            sites.append((path.name, line, arguments))
    return sites


def test_the_call_site_scan_finds_the_policies_it_is_meant_to_audit():
    """The scan is the guard; this is the guard on the guard.

    A regex that silently matched nothing would make every assertion below
    vacuously true -- the exact failure mode this whole file exists to
    eliminate, so it is not repeated here.
    """
    sites = _scope_permits_call_sites()
    files = {name for name, _, _ in sites}
    assert len(sites) >= 60, (
        f"the capex_scope_permits scan found only {len(sites)} call sites; "
        f"the migrations declare far more, so the scan is broken and every "
        f"assertion built on it is vacuous")
    for expected in ("004_identity_scope.sql", "006_rls_coverage.sql",
                     "008_approval_engine.sql", "010_integration.sql",
                     "011_reconciliation_exception.sql"):
        assert expected in files, (
            f"{expected} declares scope policies but the scan found none in "
            f"it")


def test_every_policy_passes_capex_scope_permits_its_arguments_in_order():
    """`capex_scope_permits` takes four same-typed positional arguments, so a
    swapped pair is accepted by PostgreSQL, runs without error, and enforces
    the WRONG DIMENSION -- silently, forever.

    The consequence is not a denial (which someone would report) but a LEAK: a
    dimension passed into a slot the principal is unrestricted on evaluates
    `true` for every row, and the dimension it was supposed to restrict is
    never checked at all.

    Every argument must therefore be either a literal `NULL` -- the deliberate
    "this table carries no such column" waiver -- or an expression whose column
    name is the one that position means. Qualified names (`c.entity_id`,
    `we.project_id`) are compared on their final component.
    """
    offenders: list[str] = []
    for filename, line, arguments in _scope_permits_call_sites():
        for position, (argument, expected) in enumerate(
                zip(arguments, _SCOPE_ARGUMENT_ORDER)):
            if argument.upper() == "NULL":
                continue
            column = argument.split(".")[-1].strip()
            if column != expected:
                offenders.append(
                    f"{filename}:{line} argument {position + 1} is "
                    f"{argument!r}, but position {position + 1} of "
                    f"capex_scope_permits is p_{expected}. The value is "
                    f"being checked against capex.{expected[:-3]}_ids "
                    f"instead of capex.{column[:-3]}_ids.")
    assert not offenders, (
        "a policy passes capex_scope_permits its arguments out of order.\n\n"
        + "\n\n".join(offenders)
        + "\n\nBoth halves of the mistake leak. The mis-placed value is "
          "tested against a dimension the principal is typically "
          "unrestricted on (so it permits every row), and the dimension it "
          "should have restricted receives NULL (so it is waived). The net "
          "effect is that the dimension is not enforced at all.")


# =========================================================================
# Database-free: the fixture wiring itself. `ScopedRoleDatabase` is the only
# thing standing between these tests and the vacuous state they were written
# to end, so what it emits is asserted directly rather than inferred from a
# live result -- and, unlike everything below, this runs on a machine with no
# PostgreSQL, which is where the wiring is edited.
# =========================================================================

class _RecordingConnection:
    """Captures the SQL a `_apply_scope` call would send, without a server."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(
            statement.as_string(None) if hasattr(statement, "as_string")
            else str(statement))
        return self


def test_the_scoped_role_database_switches_role_before_applying_scope():
    """Three properties, each of which has its own failure mode:

    * a `SET ROLE` is issued at all -- without it the session stays the
      connecting superuser and every policy is bypassed, which is the entire
      defect this file exists to close;
    * it is `SET LOCAL`, never a bare `SET` -- a bare one would survive the
      transaction and leak a privilege change into the next borrower of the
      pooled connection, the mirror image of the scope-leak guarantee
      `engine.py` states;
    * the scope settings the policies read are still applied, all of them, so
      switching role did not quietly replace the scope application it wraps.
    """
    con = _RecordingConnection()
    scope = _scope(frozenset({"ENT-A"}))
    ScopedRoleDatabase._apply_scope(
        ScopedRoleDatabase.__new__(ScopedRoleDatabase), con, scope)

    assert con.statements, "no SQL was emitted at all"
    assert con.statements[0] == f'SET LOCAL ROLE "{rls.SCOPED_ROLE}"', (
        f"the first statement must be the role switch, and it must be SET "
        f"LOCAL. Got {con.statements[0]!r}. A missing or late role switch "
        f"leaves the session as the connecting superuser, which bypasses "
        f"every policy -- the exact failure this class exists to prevent, and "
        f"one that shows up as tests PASSING.")
    for statement in con.statements:
        assert not re.match(r"^\s*SET(?!\s+LOCAL\b)", statement), (
            f"{statement!r} is a bare SET; it would survive the transaction "
            f"and leak into the next borrower of the pooled connection")
    emitted = " ".join(con.statements)
    for key in scope.as_settings():
        assert f'"{key.split(".")[0]}"."{key.split(".")[1]}"' in emitted, (
            f"{key} was not applied; switching role replaced the scope "
            f"application instead of wrapping it, so the policies would read "
            f"an absent setting and (correctly) deny everything")


# =========================================================================
# The matrix. One entry per table carrying a `capex_scope_permits` policy in
# 010 or 011: nine tables, nine entries, and a completeness test below that
# refuses to let a tenth be added without one.
# =========================================================================

#: A supplier invoice, as it arrives from Zoho. No key from
#: `integration_store.RESTRICTED_PAYLOAD_KEYS` appears in it -- the CHECK
#: constraint in 010 would refuse the row, and this file is about RLS, not
#: about that constraint.
_PAYLOAD = {
    "ENT-A": ('{"bill_number": "INV-A-0001", "vendor_name": "Acme Supplies", '
              '"total_paise": 4500000}'),
    "ENT-B": ('{"bill_number": "INV-B-0777", "vendor_name": "Beta Fabricators", '
              '"total_paise": 9900000}'),
}

#: table -> the SELECT whose single text column identifies which entity's row
#: is visible. Every one of them returns `CONN-A` / `CONN-B` or an id derived
#: from the entity, so one comparison shape serves all nine.
_PROBE: dict[str, str] = {
    "integration_connection":
        "SELECT connection_id FROM integration_connection ORDER BY 1",
    "integration_inbox":
        "SELECT connection_id FROM integration_inbox ORDER BY 1",
    "integration_outbox":
        "SELECT connection_id FROM integration_outbox ORDER BY 1",
    "integration_watermark":
        "SELECT connection_id FROM integration_watermark ORDER BY 1",
    "integration_rate_budget":
        "SELECT connection_id FROM integration_rate_budget ORDER BY 1",
    "integration_circuit":
        "SELECT connection_id FROM integration_circuit ORDER BY 1",
    "integration_event":
        "SELECT connection_id FROM integration_event ORDER BY 1",
    "job":
        "SELECT connection_id FROM job ORDER BY 1",
    "reconciliation_exception":
        "SELECT 'CONN-' || substring(entity_id from 5) "
        "FROM reconciliation_exception ORDER BY 1",
}

#: The nine tables, in probe order. Named explicitly rather than derived, so
#: the completeness test below has something independent to compare against.
MATRIX_TABLES: tuple[str, ...] = tuple(_PROBE)

#: Tables carrying a `project_id` column that their policy must enforce.
PROJECT_DIMENSION_TABLES: tuple[str, ...] = ("job", "reconciliation_exception")


def test_the_matrix_covers_every_scope_policied_table_in_010_and_011():
    """Derived from the migrations, compared against the hand-written list
    above. A table added to 010/011 with a scope policy and no matrix entry
    fails here rather than shipping with a policy nobody ever asked to hide a
    row."""
    declared: set[str] = set()
    for name in ("010_integration.sql", "011_reconciliation_exception.sql"):
        code = re.sub(r"--[^\n]*", "",
                      (MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        for match in re.finditer(r"CREATE POLICY \w+ ON (\w+)(.*?);", code,
                                 re.DOTALL):
            if "capex_scope_permits(" in match.group(2):
                declared.add(match.group(1))
    assert declared == set(MATRIX_TABLES), (
        f"the behavioural matrix and the migrations disagree about which "
        f"tables carry a scope policy. Only in the migrations: "
        f"{sorted(declared - set(MATRIX_TABLES))} -- these have a policy no "
        f"test has ever asked to hide a row. Only in the matrix: "
        f"{sorted(set(MATRIX_TABLES) - declared)}.")


def test_the_project_dimension_list_names_every_table_whose_policy_uses_one():
    """`job` and `reconciliation_exception` are the only two integration tables
    whose policy passes a `project_id`; the other seven waive it with a literal
    NULL because they carry no such column."""
    using_project: set[str] = set()
    for name in ("010_integration.sql", "011_reconciliation_exception.sql"):
        code = re.sub(r"--[^\n]*", "",
                      (MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        for match in re.finditer(r"CREATE POLICY \w+ ON (\w+)(.*?);", code,
                                 re.DOTALL):
            if re.search(r"\bproject_id\b", match.group(2)):
                using_project.add(match.group(1))
    assert using_project == set(PROJECT_DIMENSION_TABLES)


# =========================================================================
# Seeding. Runs on the raw `pg_connection`, which is the CI superuser and
# therefore bypasses RLS -- deliberately, and this is the ONE place that is
# the right thing: the fixture has to be able to create rows in both
# entities, which no scoped principal may do.
# =========================================================================

def _seed_estate(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES ('ORG-1', 'ORG1', 'Test Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    for entity in ("ENT-A", "ENT-B"):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, 'ORG-1', %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (entity, entity, entity))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-INTEGRATION',"
        " 'svc@example.test', 'Integration service', 'SERVICE', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    # PRJ-A and PRJ-B are BOTH inside ENT-A, on purpose. A project-dimension
    # assertion whose two projects sat in different entities would pass with
    # the project dimension doing nothing at all -- the entity dimension would
    # already have hidden the far row, which is precisely the mistake that
    # makes a leak look enforced.
    for project, entity in (("PRJ-A", "ENT-A"), ("PRJ-B", "ENT-A")):
        con.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (project, entity, project, project))
    con.commit()


def _seed_one_row_per_table_per_entity(con: psycopg.Connection) -> None:
    """One row in each of the nine tables, for each of ENT-A and ENT-B.

    `project_id` is left NULL on the two tables that carry it: this is the
    ENTITY matrix, and a project value here would let the project dimension
    mask an entity-dimension failure. The project matrix seeds its own rows.
    """
    _seed_estate(con)
    for entity in ("ENT-A", "ENT-B"):
        suffix = entity[-1]                     # 'A' / 'B'
        connection_id = f"CONN-{suffix}"
        payload = _PAYLOAD[entity]
        con.execute(
            "INSERT INTO integration_connection (connection_id, entity_id,"
            " product, dc, organization_id, connector_name, created_by,"
            " updated_by) VALUES (%s, %s, 'ERP', 'in', %s, 'zoho', 'T', 'T')",
            (connection_id, entity, f"ORG-{entity}"))
        con.execute(
            "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
            " external_id, payload_sha, payload, redaction_policy_version)"
            " VALUES (%s, %s, 'bills', %s, %s, %s::jsonb, 'v1')",
            (f"IB-{suffix}", connection_id, f"Z-{suffix}",
             f"sha-{suffix}", payload))
        con.execute(
            "INSERT INTO integration_outbox (outbox_id, connection_id, module,"
            " local_id, dedupe_key, payload, created_by, updated_by)"
            " VALUES (%s, %s, 'purchaseorders', %s, %s, %s::jsonb, 'T', 'T')",
            (f"OB-{suffix}", connection_id, f"PO-{suffix}",
             f"CAPEX-PO-{suffix}", payload))
        con.execute(
            "INSERT INTO integration_watermark (connection_id, module, hwm,"
            " updated_by) VALUES (%s, 'bills', now(), 'T')", (connection_id,))
        con.execute(
            "INSERT INTO integration_rate_budget (connection_id, window_kind,"
            " allocation, window_start, window_start_key, window_seconds,"
            " window_tz, ceiling, used) VALUES (%s, 'MINUTE', 'POLLING',"
            " timestamptz '2026-09-06 14:23:00+05:30', '2026-09-06T14:23',"
            " 60, 'Asia/Kolkata', 100, 0)", (connection_id,))
        con.execute(
            "INSERT INTO integration_circuit (connection_id, module)"
            " VALUES (%s, 'bills')", (connection_id,))
        con.execute(
            "INSERT INTO integration_event (connection_id, kind, actor)"
            " VALUES (%s, 'RECEIVED', 'SVC')", (connection_id,))
        con.execute(
            "INSERT INTO job (job_id, kind, connection_id, entity_id,"
            " principal_user_id, created_by, updated_by) VALUES (%s,"
            " 'poll_bills', %s, %s, 'SVC-INTEGRATION', 'T', 'T')",
            (f"JOB-{suffix}", connection_id, entity))
        con.execute(
            "INSERT INTO reconciliation_exception (exception_id, kind,"
            " object_type, object_id, entity_id, status, detail, local_paise,"
            " source_paise) VALUES (%s, 'CONTROL_TOTAL_MISMATCH', 'bill', %s,"
            " %s, 'Open', %s, %s, %s)",
            (f"RX-{suffix}", f"OBJ-{suffix}", entity,
             f"control total mismatch on {entity}",
             4500000 if entity == "ENT-A" else 9900000,
             4499000 if entity == "ENT-A" else 9899000))
    con.commit()


def _scope(entity_ids, project_ids=None) -> Scope:
    """A deliberately restricted principal. `entity_ids`/`project_ids` are
    passed through untouched so a test can hand in `None` (unrestricted),
    `frozenset()` (nothing) or a populated set, and the three stay
    distinguishable end to end."""
    return Scope(
        user_id="U-RLS", principal_kind="USER",
        entity_ids=entity_ids, plant_ids=None,
        project_ids=project_ids, location_ids=None, read_all=False)


def _visible(database: ScopedRoleDatabase, scope: Scope, probe: str) -> list[str]:
    with database.session(scope) as session:
        rows = session.fetchall(probe)  # scope-exempt: asserting RLS itself
    return [row[0] for row in rows]


# =========================================================================
# The hypothesis, asserted rather than narrated.
# =========================================================================

@PG
@pytest.mark.pg
def test_live_the_connecting_role_bypasses_rls_exactly_when_it_claims_to(
        pg_connection, pg_url, pg_disposable_db_name):
    """The direct question, and then the behaviour it predicts.

    `SELECT current_user, rolsuper, rolbypassrls` says whether the role the
    suite connects as can be constrained by a policy at all. This test then
    reads the same two rows through a plain `Database` (scope settings applied,
    no `SET LOCAL ROLE`) and asserts the result AGREES with that answer:

        bypasses RLS  -> both entities' rows are visible
        subject to RLS -> only the scoped entity's row is

    Both directions are asserted, so the test stays true if the wiring is ever
    changed to connect as a non-superuser -- it does not hard-code today's
    answer. What it will not tolerate is the two disagreeing, which would mean
    something other than the role is deciding what is visible.

    This is also the mutation control for the whole matrix below. Every test
    after this one asserts that ENT-B's rows are HIDDEN; if the seed were
    silently inserting nothing for ENT-B, they would all pass for the wrong
    reason. Here the same rows are read with RLS out of the way, and both must
    be there.
    """
    from app.backend.pg import engine as pg_engine

    con = pg_connection
    _seed_one_row_per_table_per_entity(con)

    row = con.execute(
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user").fetchone()
    con.rollback()
    assert row is not None, "current_user has no pg_roles row"
    role_name, is_super, has_bypass = row
    bypasses = bool(is_super or has_bypass)

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    plain = pg_engine.Database(cfg, secret_provider=provider,
                               min_size=1, max_size=2)
    try:
        with plain.session(_scope(frozenset({"ENT-A"}))) as session:
            seen = [r[0] for r in session.fetchall(  # scope-exempt: asserting RLS
                _PROBE["integration_inbox"])]
    finally:
        plain.close()

    diagnosis = (
        f"current_user={role_name!r} rolsuper={is_super!r} "
        f"rolbypassrls={has_bypass!r}; a plain Database.session() scoped to "
        f"ENT-A saw {seen!r}")
    if bypasses:
        assert seen == ["CONN-A", "CONN-B"], (
            f"{diagnosis}.\nThis role bypasses row-level security, so a plain "
            f"Database.session() must see BOTH entities. Seeing fewer means "
            f"the seed did not create both rows -- and every 'ENT-B is "
            f"hidden' assertion in this file would then be passing for the "
            f"wrong reason.")
    else:
        assert seen == ["CONN-A"], (
            f"{diagnosis}.\nThis role does NOT bypass row-level security, so "
            f"the policy should have confined the read to ENT-A. It did not, "
            f"which is a genuine policy failure rather than a wiring one.")


@PG
@pytest.mark.pg
def test_live_the_scoped_role_is_neither_superuser_nor_bypassrls(pg_connection):
    """`SET LOCAL ROLE capex_app` is only worth anything if `capex_app` is a
    role policies apply to. A future `ALTER ROLE capex_app BYPASSRLS` -- issued
    to make some unrelated job work -- would turn every assertion in this file
    vacuous without changing a line of it."""
    row = pg_connection.execute(
        "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
        "WHERE rolname = %s", (rls.SCOPED_ROLE,)).fetchone()
    pg_connection.rollback()
    assert row is not None, (
        f"{rls.SCOPED_ROLE} does not exist; migrations/pg/004_identity_scope.sql "
        f"is meant to create it")
    is_super, has_bypass, can_login = row
    assert is_super is False, (
        f"{rls.SCOPED_ROLE} is a superuser and therefore bypasses every "
        f"policy; the RLS suite would pass without enforcing anything")
    assert has_bypass is False, (
        f"{rls.SCOPED_ROLE} holds BYPASSRLS and therefore bypasses every "
        f"policy; the RLS suite would pass without enforcing anything")
    assert can_login is False, (
        "capex_app is NOLOGIN by design -- see 004_identity_scope.sql's header")


# =========================================================================
# Entity restriction: the three-point matrix, every table.
# =========================================================================

@PG
@pytest.mark.pg
@pytest.mark.parametrize("table", MATRIX_TABLES)
def test_live_entity_restriction_confines_every_scope_policied_table(
        table, pg_connection, pg_app_database):
    """Three points, because one is not enough.

    Asserting only that ENT-B is hidden would pass against a policy that
    returns false for everything -- which hides ENT-B and also hides ENT-A, and
    would be found later by an operator wondering why the queue was empty. The
    widened scope pins the policy's other side; the empty scope pins that an
    empty grant set is not silently read as "all".
    """
    _seed_one_row_per_table_per_entity(pg_connection)
    probe = _PROBE[table]

    assert _visible(pg_app_database, _scope(frozenset({"ENT-A"})), probe) == \
        ["CONN-A"], (
        f"{table}: the policy did not confine the read to the principal's "
        f"entity. Another entity's rows were visible.")
    assert _visible(pg_app_database,
                    _scope(frozenset({"ENT-A", "ENT-B"})), probe) == \
        ["CONN-A", "CONN-B"], (
        f"{table}: a principal granted BOTH entities saw fewer than both. The "
        f"policy is denying rows it should permit -- a deny-all policy passes "
        f"the hiding assertion above and fails here, which is why this point "
        f"exists.")
    assert _visible(pg_app_database, _scope(frozenset()), probe) == [], (
        f"{table}: a principal with an EMPTY entity grant saw rows. An empty "
        f"set means 'nothing' and must never be read as 'unrestricted' -- the "
        f"inversion the mode/ids wire format exists to make unrepresentable.")


@PG
@pytest.mark.pg
def test_live_an_unscoped_session_reads_nothing_from_any_integration_table(
        pg_connection):
    """`SET LOCAL ROLE capex_app` with NO `capex.*` settings at all -- a
    connection that never went through `Database.session()`.

    Every policy must FAIL CLOSED: an absent mode setting denies. A backstop
    that fails open on a forgotten `SET LOCAL` protects nothing, and this is
    the one state in which the application's own `WHERE` predicates are
    certainly absent too.
    """
    con = pg_connection
    _seed_one_row_per_table_per_entity(con)
    try:
        con.execute(f"SET LOCAL ROLE {rls.SCOPED_ROLE}")
        for table in MATRIX_TABLES:
            count = con.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, (
                f"{table}: an unscoped {rls.SCOPED_ROLE} session read {count} "
                f"rows. With no scope applied the policy must deny "
                f"everything.")
    finally:
        con.rollback()


# =========================================================================
# Project restriction, where the table carries the dimension.
# =========================================================================

def _seed_two_projects_in_one_entity(con: psycopg.Connection) -> None:
    """`job` and `reconciliation_exception` rows on PRJ-A and PRJ-B, both
    inside ENT-A, so only the PROJECT dimension can tell them apart."""
    _seed_estate(con)
    con.execute(
        "INSERT INTO integration_connection (connection_id, entity_id,"
        " product, dc, organization_id, connector_name, created_by,"
        " updated_by) VALUES ('CONN-A', 'ENT-A', 'ERP', 'in', 'ORG-ENT-A',"
        " 'zoho', 'T', 'T') ON CONFLICT DO NOTHING")
    for project in ("PRJ-A", "PRJ-B"):
        con.execute(
            "INSERT INTO job (job_id, kind, connection_id, entity_id,"
            " project_id, principal_user_id, created_by, updated_by)"
            " VALUES (%s, 'poll_bills', 'CONN-A', 'ENT-A', %s,"
            " 'SVC-INTEGRATION', 'T', 'T')", (f"JOB-{project}", project))
        con.execute(
            "INSERT INTO reconciliation_exception (exception_id, kind,"
            " object_type, object_id, entity_id, project_id, status, detail,"
            " local_paise, source_paise) VALUES (%s,"
            " 'CONTROL_TOTAL_MISMATCH', 'project', %s, 'ENT-A', %s, 'Open',"
            " %s, 7700000, 7699000)",
            (f"RX-{project}", f"OBJ-{project}", project,
             f"control total mismatch on {project}"))
    con.commit()


@PG
@pytest.mark.pg
@pytest.mark.parametrize("table,probe", [
    ("job", "SELECT job_id FROM job ORDER BY 1"),
    ("reconciliation_exception",
     "SELECT exception_id FROM reconciliation_exception ORDER BY 1"),
])
def test_live_project_restriction_confines_the_tables_that_carry_one(
        table, probe, pg_connection, pg_app_database):
    """Both rows are in ENT-A, so the entity dimension permits both and the
    project dimension is the only thing that can hide one.

    `reconciliation_exception` is expected to FAIL here until
    `migrations/pg/011_reconciliation_exception.sql` is corrected: its policy
    reads

        capex_scope_permits(entity_id, NULL, project_id, NULL)

    and `capex_scope_permits`' third parameter is `p_location_id`. `project_id`
    is therefore checked against `capex.location_ids` -- on which this
    principal (like most) is unrestricted, so it permits every row -- while the
    project slot receives NULL and is waived. The project dimension is not
    enforced at all, and the leaked columns are `local_paise` /
    `source_paise`: another project's reconciliation amounts. The fix is one
    line, in a file this stream does not own; see the report.
    """
    _seed_two_projects_in_one_entity(pg_connection)
    prefix = "JOB" if table == "job" else "RX"

    seen = _visible(pg_app_database,
                    _scope(frozenset({"ENT-A"}), frozenset({"PRJ-A"})), probe)
    assert seen == [f"{prefix}-PRJ-A"], (
        f"{table}: the policy did not confine the read to the principal's "
        f"PROJECT. Both rows are in ENT-A, so the entity dimension cannot "
        f"have been what was tested -- another project's rows were visible. "
        f"Saw {seen!r}.")

    both = _visible(pg_app_database,
                    _scope(frozenset({"ENT-A"}),
                           frozenset({"PRJ-A", "PRJ-B"})), probe)
    assert both == [f"{prefix}-PRJ-A", f"{prefix}-PRJ-B"], (
        f"{table}: a principal granted both projects saw fewer than both; the "
        f"policy is denying rows it should permit.")

    assert _visible(pg_app_database,
                    _scope(frozenset({"ENT-A"}), frozenset()), probe) == [], (
        f"{table}: a principal with an EMPTY project grant saw rows.")


@PG
@pytest.mark.pg
def test_live_no_integration_table_carries_a_plant_or_location_column(
        pg_connection):
    """Why plant and location are waived with a literal NULL in all nine
    policies, stated as a fact about the schema rather than as a claim in a
    comment.

    If one of these tables ever gains a `plant_id` or `location_id`, its policy
    must start enforcing it and this file must grow a matrix for it -- so the
    absence is asserted, and the test fails the day the assumption stops being
    true.
    """
    rows = pg_connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = ANY(%s) "
        "AND column_name IN ('plant_id', 'location_id') "
        "ORDER BY 1, 2", (list(MATRIX_TABLES),)).fetchall()
    pg_connection.rollback()
    assert rows == [], (
        f"these integration tables now carry a plant or location column: "
        f"{rows}. Their policies pass NULL in that position, so the dimension "
        f"is WAIVED -- which was correct while the column did not exist and is "
        f"a leak now that it does. Add the column to the policy and a case to "
        f"this file's matrix.")


# =========================================================================
# The columns that are the reason any of this matters.
# =========================================================================

@PG
@pytest.mark.pg
def test_live_another_entitys_supplier_invoices_are_unreadable(
        pg_connection, pg_app_database):
    """`integration_inbox.payload` holds another entity's supplier invoices
    verbatim -- vendor names, bill numbers and amounts, straight off the Zoho
    API. It is the sharpest case in the schema, and the least obvious: the
    table reads as integration plumbing, so a reviewer scanning for "financial"
    tables walks past it.

    Asserted on the PAYLOAD and its metadata, not on the row count. A policy
    that filtered `inbox_id` while leaving `payload` reachable through some
    other path would pass a count-based test.
    """
    _seed_one_row_per_table_per_entity(pg_connection)
    scope = _scope(frozenset({"ENT-A"}))

    with pg_app_database.session(scope) as session:
        rows = session.fetchall(  # scope-exempt: asserting RLS itself
            "SELECT inbox_id, external_id, payload_sha,"
            " payload ->> 'bill_number', payload ->> 'vendor_name',"
            " payload ->> 'total_paise' FROM integration_inbox ORDER BY 1")
        outbox = session.fetchall(  # scope-exempt: asserting RLS itself
            "SELECT outbox_id, dedupe_key, payload ->> 'vendor_name'"
            " FROM integration_outbox ORDER BY 1")

    assert rows == [("IB-A", "Z-A", "sha-A", "INV-A-0001", "Acme Supplies",
                     "4500000")], (
        f"ENT-A read something other than exactly its own receipt. Anything "
        f"naming Beta Fabricators, INV-B-0777 or 9900000 is ENT-B's supplier "
        f"ledger. Saw {rows!r}")
    assert outbox == [("OB-A", "CAPEX-PO-A", "Acme Supplies")], (
        f"ENT-A read another entity's outbound purchase-order payload: "
        f"{outbox!r}")

    flat = repr(rows) + repr(outbox)
    for secret in ("Beta Fabricators", "INV-B-0777", "9900000", "CONN-B",
                   "CAPEX-PO-B"):
        assert secret not in flat, (
            f"{secret!r} -- ENT-B's data -- reached an ENT-A principal")


@PG
@pytest.mark.pg
def test_live_another_entitys_reconciliation_amounts_are_unreadable(
        pg_connection, pg_app_database):
    """`local_paise` and `source_paise` are the two sides of a control-total
    mismatch: the amount we booked and the amount the source system says. Read
    across an entity boundary they disclose the counterparty's book value for a
    transaction the reader has no part in."""
    _seed_one_row_per_table_per_entity(pg_connection)

    with pg_app_database.session(_scope(frozenset({"ENT-A"}))) as session:
        rows = session.fetchall(  # scope-exempt: asserting RLS itself
            "SELECT exception_id, entity_id, local_paise, source_paise,"
            " detail FROM reconciliation_exception ORDER BY 1")
        totals = session.fetchone(  # scope-exempt: asserting RLS itself
            "SELECT count(*), coalesce(sum(local_paise), 0)"
            " FROM reconciliation_exception")

    assert rows == [("RX-A", "ENT-A", 4500000, 4499000,
                     "control total mismatch on ENT-A")], (
        f"ENT-A read another entity's reconciliation exception: {rows!r}")
    assert totals == (1, 4500000), (
        f"an AGGREGATE over reconciliation_exception saw rows the row-by-row "
        f"read did not: {totals!r}. A policy that filters a SELECT list but "
        f"not a count leaks the amount through sum() just as effectively.")


# =========================================================================
# The write side. USING filters reads; WITH CHECK is what stops a principal
# writing INTO a scope it cannot see.
# =========================================================================

@PG
@pytest.mark.pg
def test_live_with_check_refuses_a_write_into_another_entitys_scope(
        pg_connection, pg_app_database):
    """A receipt filed against ENT-B's connection, and a job claiming ENT-B,
    both written by an ENT-A principal.

    Without `WITH CHECK` these succeed and then vanish from the writer's own
    view -- a row nobody can see, holding data nobody meant to store there.
    """
    _seed_one_row_per_table_per_entity(pg_connection)
    scope = _scope(frozenset({"ENT-A"}))

    for statement, params, what in (
            ("INSERT INTO integration_inbox (inbox_id, connection_id, module,"
             " external_id, payload_sha, payload, redaction_policy_version)"
             " VALUES ('IB-X', 'CONN-B', 'bills', 'Z-X', 'sha-X',"
             " '{}'::jsonb, 'v1')", None,
             "a receipt filed against another entity's connection"),
            ("INSERT INTO job (job_id, kind, entity_id, principal_user_id,"
             " created_by, updated_by) VALUES ('JOB-X', 'poll_bills',"
             " 'ENT-B', 'SVC-INTEGRATION', 'T', 'T')", None,
             "a job claiming another entity"),
            ("INSERT INTO reconciliation_exception (exception_id, kind,"
             " object_type, object_id, entity_id, status, detail) VALUES"
             " ('RX-X', 'CONTROL_TOTAL_MISMATCH', 'bill', 'OBJ-X', 'ENT-B',"
             " 'Open', 'planted')", None,
             "an exception raised against another entity"),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
            with pg_app_database.session(scope) as session:
                session.execute(statement, params)
        assert "row-level security" in str(excinfo.value).lower(), (
            f"{what} was refused, but not by RLS: {excinfo.value}")

    # ...and nothing was left behind by the refused writes.
    surviving = pg_connection.execute(
        "SELECT (SELECT count(*) FROM integration_inbox WHERE inbox_id = 'IB-X'),"
        " (SELECT count(*) FROM job WHERE job_id = 'JOB-X'),"
        " (SELECT count(*) FROM reconciliation_exception"
        "  WHERE exception_id = 'RX-X')").fetchone()
    pg_connection.rollback()
    assert surviving == (0, 0, 0), (
        f"a write refused by RLS still left a row behind: {surviving!r}")


@PG
@pytest.mark.pg
def test_live_a_principal_may_still_write_inside_its_own_scope(
        pg_connection, pg_app_database):
    """The other half of the previous test, and the reason it is not enough on
    its own: a `WITH CHECK` that refuses EVERY write also refuses the
    cross-entity one, and would pass above while making the application
    unusable."""
    _seed_one_row_per_table_per_entity(pg_connection)
    with pg_app_database.session(_scope(frozenset({"ENT-A"}))) as session:
        session.execute(
            "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
            " external_id, payload_sha, payload, redaction_policy_version)"
            " VALUES ('IB-OK', 'CONN-A', 'bills', 'Z-OK', 'sha-OK',"
            " '{}'::jsonb, 'v1')")
    assert pg_connection.execute(
        "SELECT count(*) FROM integration_inbox WHERE inbox_id = 'IB-OK'"
    ).fetchone()[0] == 1, (
        "a principal could not write a receipt into its OWN entity's "
        "connection; the WITH CHECK is refusing everything")


# =========================================================================
# The one documented waiver, pinned so it stays a decision.
# =========================================================================

@PG
@pytest.mark.pg
def test_live_an_event_with_no_connection_is_visible_to_any_scoped_principal(
        pg_connection, pg_app_database):
    """`integration_event.connection_id` is NULLABLE and 010's policy waives
    the dimension when it is NULL -- an estate-wide sweep's events belong to no
    connection.

    That waiver is real and deliberate, and it is exactly the kind of decision
    that rots into a leak once nobody remembers it was one. Pinned here so a
    future change that starts writing entity-identifying `detail` onto
    connection-less events has to walk past this test. It also confirms the
    waiver is not accidentally wider than stated: an event that DOES name a
    connection is still confined.
    """
    con = pg_connection
    _seed_one_row_per_table_per_entity(con)
    con.execute(
        "INSERT INTO integration_event (connection_id, kind, actor)"
        " VALUES (NULL, 'SWEEP_STARTED', 'SVC')")
    con.commit()

    with pg_app_database.session(_scope(frozenset({"ENT-A"}))) as session:
        rows = session.fetchall(  # scope-exempt: asserting RLS itself
            # ORDER BY the raw column with NULLS FIRST, not by the coalesced
            # placeholder: sorting `'<none>'` against `'CONN-A'` depends on the
            # server's collation, and glibc's en_US.UTF-8 ignores punctuation
            # at the primary level -- so `ORDER BY 1` would compare "none"
            # against "CONNA" and put them the other way round. NULLS FIRST is
            # collation-independent.
            "SELECT coalesce(connection_id, '<none>'), kind"
            " FROM integration_event ORDER BY connection_id NULLS FIRST")

    assert rows == [("<none>", "SWEEP_STARTED"), ("CONN-A", "RECEIVED")], (
        f"either the connection-less event was hidden (010 says it is "
        f"visible to any established principal) or a connection-bound event "
        f"from another entity was visible (010 says it is not). Saw {rows!r}")
