"""An unattributed reconciliation exception: nobody sees it except triage.

WHAT WENT WRONG, AND WHY BOTH ANSWERS WERE WRONG
================================================

`reconciliation_exception.entity_id` and `.project_id` are nullable on purpose.
An `UNSANCTIONED_COMMITMENT` can be discovered on a purchase order we hold no
local record of, so at the moment it is raised there is no entity to attribute
it to.

Two layers then disagreed about what that NULL means:

* **RLS said EVERYONE.** `capex_dimension_permits` returns `true` when the
  row's value is NULL. Its own comment explains the intent — it is meant to be
  reached "with a literal SQL NULL passed by a policy, never by an absent
  session setting", i.e. to waive a dimension the TABLE does not carry. But
  `entity_id` here is a nullable COLUMN, so a NULL is a DATA null, and the
  function cannot tell the two apart. Every principal could read every
  unattributed exception — including `local_paise` and `source_paise`, the
  precise sums by which somebody's books do not tie out.

* **The application said NOBODY.** `repo.compile_scope` emits
  `entity_id = ANY(...)`, and SQL NULL is not equal to anything, so the
  compiled predicate dropped those rows for every caller.

Neither is acceptable. §11.8 requires an unattributed line to be held at full
value, visible, and blocking capitalisation — a row nobody can see is a silent
drop with extra steps, and a row everybody can see is a leak.

`012_unattributed_triage.sql` resolves it: unattributed rows are visible only
to a principal carrying `capex.triage_unattributed`, which `Scope` emits and
`auth.PERMISSIONS` gates behind `reconciliation.triage`.

WHY THESE TESTS CONNECT AS `capex_app`
======================================

CI's `POSTGRES_USER: capex` is created a cluster SUPERUSER by the official
image, and a superuser bypasses RLS unconditionally — `FORCE ROW LEVEL
SECURITY` governs the table's OWNER and says nothing about superusers. Every
test here therefore goes through `ScopedRoleDatabase`, which issues
`SET LOCAL ROLE capex_app` before applying the scope. Asserting RLS through a
superuser connection proves nothing at all, and this repository has already
shipped a whole file of policy-existence tests that could not have caught an
unenforced policy.
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

import pytest  # noqa: E402

from app.backend import auth  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

_ATTRIBUTED = "RX-ATTRIBUTED-ENT-A"
_UNATTRIBUTED = "RX-UNATTRIBUTED"


def _seed(con) -> None:
    """One exception attributed to ENT-A, and one attributed to nobody."""
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
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type,"
        " object_id, entity_id, status, detail, local_paise, source_paise)"
        " VALUES (%s, 'GRN_LINE_UNATTRIBUTED', 'grn_line', 'GL-1', 'ENT-A',"
        " 'Open', 'attributed', 25000000, 0) ON CONFLICT DO NOTHING",
        (_ATTRIBUTED,))
    con.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type,"
        " object_id, entity_id, status, detail, local_paise, source_paise)"
        " VALUES (%s, 'UNSANCTIONED_COMMITMENT', 'purchase_order', 'PO-X',"
        " NULL, 'Open', 'no local record of this PO', 0, 99900000)"
        " ON CONFLICT DO NOTHING", (_UNATTRIBUTED,))
    con.commit()


def _visible(pg_url, dbname, *, entities, triage: bool) -> list[str]:
    database = scoped_role_database(pg_url, dbname)
    scope = Scope(user_id="U-T", principal_kind="USER",
                  entity_ids=frozenset(entities), plant_ids=None,
                  project_ids=None, location_ids=None, read_all=False,
                  triage_unattributed=triage)
    try:
        with database.session(scope) as session:
            rows = session.fetchall(  # scope-exempt: asserting RLS itself
                "SELECT exception_id FROM reconciliation_exception"
                " ORDER BY exception_id")
    finally:
        database.close()
    return [r[0] for r in rows]


def test_the_triage_flag_defaults_to_denied_and_is_not_read_all():
    """Two properties, neither of which needs a database.

    A `Scope` built without thinking about triage must not get it — the safe
    direction — and the flag must be its OWN setting rather than folded into
    `read_all`, which is documented "for migrations and start-up checks only,
    never for a request". Folding them would make the only route to triaging
    one exception an unrestricted read over the whole estate.
    """
    ordinary = Scope(user_id="U-1", entity_ids=frozenset({"ENT-A"}))
    assert ordinary.triage_unattributed is False
    assert ordinary.as_settings()["capex.triage_unattributed"] == "false"

    reader = Scope(user_id="U-2", read_all=True)
    assert reader.as_settings()["capex.triage_unattributed"] == "false", (
        "read_all granted triage by implication; they are different questions "
        "and a whole-estate reader is a far larger grant than a triager")

    triager = Scope(user_id="U-3", entity_ids=frozenset({"ENT-A"}),
                    triage_unattributed=True)
    assert triager.as_settings()["capex.triage_unattributed"] == "true"


def test_the_setting_is_emitted_explicitly_rather_than_omitted():
    """An absent setting and a denied one must be the same observable state.

    `current_setting(key, true)` returns NULL for a key that was never set. A
    policy that COALESCEd that to 'true' would fail open, and a policy that
    reads it correctly still deserves an unambiguous input. So 'false' is
    written out rather than left absent.
    """
    settings = Scope(user_id="U-1", entity_ids=frozenset({"ENT-A"})).as_settings()
    assert "capex.triage_unattributed" in settings


def test_the_triage_permission_exists_and_is_not_held_by_ordinary_roles():
    """It is a data-triage right over other entities' unattributed
    discrepancies, so it belongs to the roles that already carry estate-wide
    responsibility -- and to nobody else."""
    holders = auth.PERMISSIONS.get("reconciliation.triage")
    assert holders, "reconciliation.triage is not defined in auth.PERMISSIONS"
    assert set(holders) <= set(auth.ROLES)
    for role in ("Requestor", "ProcurementApprover", "FinanceApprover"):
        assert role not in holders, (
            f"{role} can read every unattributed discrepancy in the estate, "
            f"including the paise figures by which another entity's books do "
            f"not tie out")


@PG
@pytest.mark.pg
def test_live_an_ordinary_principal_cannot_read_an_unattributed_exception(
        pg_connection, pg_url, pg_disposable_db_name):
    """The leak, asserted directly.

    Before migration 012 this returned BOTH rows for any principal, because
    `capex_dimension_permits` treats a NULL row value as a waived dimension.
    """
    _seed(pg_connection)
    seen = _visible(pg_url, pg_disposable_db_name,
                    entities={"ENT-A"}, triage=False)
    assert seen == [_ATTRIBUTED], (
        f"an ordinary ENT-A principal saw {seen}. The unattributed row carries "
        f"another party's discrepancy and the exact sum involved; a NULL "
        f"entity must not read as 'this row concerns everyone'.")


@PG
@pytest.mark.pg
def test_live_an_unrelated_entity_sees_neither_row(
        pg_connection, pg_url, pg_disposable_db_name):
    """ENT-B is entitled to nothing here: not ENT-A's exception, and not the
    unattributed one either. Without this, a test asserting only that ENT-B
    cannot see ENT-A's row would also pass against a policy that leaked the
    unattributed one."""
    _seed(pg_connection)
    seen = _visible(pg_url, pg_disposable_db_name,
                    entities={"ENT-B"}, triage=False)
    assert seen == [], f"ENT-B saw {seen}"


@PG
@pytest.mark.pg
def test_live_a_triage_principal_can_read_the_unattributed_row(
        pg_connection, pg_url, pg_disposable_db_name):
    """The other half, and the reason this is not simply a deny-all.

    An exception nobody could attribute must still reach somebody who can
    attribute it, or §11.8's "held at full value, visible, blocking
    capitalisation" is a silent drop with extra steps.
    """
    _seed(pg_connection)
    seen = _visible(pg_url, pg_disposable_db_name,
                    entities={"ENT-A"}, triage=True)
    assert _UNATTRIBUTED in seen, (
        "a triage principal could not see the unattributed exception, so the "
        "row is invisible to everyone -- which is the other failure mode, not "
        "a fix for the first")


@PG
@pytest.mark.pg
def test_live_triage_does_not_widen_ordinary_entity_scope(
        pg_connection, pg_url, pg_disposable_db_name):
    """Triage grants the unattributed rows and NOTHING else.

    A triager scoped to ENT-A must still not see ENT-B's attributed
    exceptions. Otherwise `triage_unattributed` has quietly become `read_all`,
    which is exactly the conflation the separate flag exists to prevent.
    """
    _seed(pg_connection)
    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, object_type,"
        " object_id, entity_id, status, detail) VALUES ('RX-ENT-B',"
        " 'CONTROL_TOTAL_MISMATCH', 'bill', 'B-9', 'ENT-B', 'Open', 'theirs')"
        " ON CONFLICT DO NOTHING")
    pg_connection.commit()

    seen = _visible(pg_url, pg_disposable_db_name,
                    entities={"ENT-A"}, triage=True)
    assert "RX-ENT-B" not in seen, (
        f"a triage principal scoped to ENT-A read ENT-B's attributed "
        f"exception: {seen}. triage_unattributed has become read_all.")
    assert _ATTRIBUTED in seen and _UNATTRIBUTED in seen


@PG
@pytest.mark.pg
def test_live_an_ordinary_principal_cannot_push_a_row_into_the_blind_spot(
        pg_connection, pg_url, pg_disposable_db_name):
    """`WITH CHECK`, which matters as much as `USING` here.

    Without it, a principal could clear `entity_id` on a row it owns and move
    its own discrepancy out of everyone else's sight -- writing itself a blind
    spot rather than reading one.
    """
    _seed(pg_connection)
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    scope = Scope(user_id="U-O", principal_kind="USER",
                  entity_ids=frozenset({"ENT-A"}), plant_ids=None,
                  project_ids=None, location_ids=None, read_all=False,
                  triage_unattributed=False)
    refused = False
    try:
        with database.session(scope) as session:
            try:
                session.execute(  # scope-exempt: asserting the WITH CHECK itself
                    "UPDATE reconciliation_exception SET entity_id = NULL"
                    " WHERE exception_id = %s", (_ATTRIBUTED,))
            except Exception:
                refused = True
    except Exception:
        refused = True
    finally:
        database.close()

    if not refused:
        still_there = _visible(pg_url, pg_disposable_db_name,
                               entities={"ENT-A"}, triage=False)
        assert _ATTRIBUTED in still_there, (
            "an ordinary principal blanked entity_id and moved its own "
            "discrepancy into the unattributed bucket, where only triage can "
            "see it. WITH CHECK is what forbids that.")


# ===========================================================================
# The API layer: the SQL those routes actually run
# ===========================================================================
# `app/backend/api/integrations.py` reaches these rows through
# `UNATTRIBUTED_COLUMNS`, which waives all four dimensions, plus a literal
# `entity_id IS NULL`. Waiving is deliberate -- a compiled
# `entity_id = ANY(...)` predicate excludes a NULL by definition, so a mapped
# dimension would hand a triage principal an empty page from a database
# perfectly willing to serve them.
#
# The statements below are the routes' own, reproduced rather than imported
# because importing the router would drag FastAPI, the session middleware and
# the SQLite identity tables into a PostgreSQL RLS test. What is under test
# here is whether the SQL and the policy agree, and that is exactly what a
# copy of the SQL run under the policy answers.

_UNATTRIBUTED_COLUMNS = {"entity": None, "plant": None, "location": None,
                         "project": None}

_LIST_SQL = """
    SELECT exception_id FROM reconciliation_exception
     WHERE entity_id IS NULL AND {scope}
     ORDER BY exception_id
"""

_ATTRIBUTE_SQL = """
    UPDATE reconciliation_exception
       SET entity_id = %(entity_id)s
     WHERE exception_id = %(exception_id)s
       AND entity_id IS NULL
       AND {scope}
    RETURNING exception_id, entity_id
"""


def _run(pg_url, dbname, statement, params, *, entities, triage):
    from app.backend.pg import repo

    database = scoped_role_database(pg_url, dbname)
    scope = Scope(user_id="U-T", principal_kind="USER",
                  entity_ids=frozenset(entities), plant_ids=None,
                  project_ids=None, location_ids=None, read_all=False,
                  triage_unattributed=triage)
    try:
        with database.session(scope) as session:
            return repo.query(session, statement, params,
                              columns=_UNATTRIBUTED_COLUMNS)
    finally:
        database.close()


def test_the_route_mapping_waives_every_dimension_explicitly():
    """No database needed, and worth its own test.

    `compile_scope` REFUSES a restricted dimension the caller did not map --
    but a dimension mapped to `None` is waived silently and deliberately. The
    difference between "waived" and "forgotten" is one line in a dict, and the
    whole safety of this route rests on the literal `entity_id IS NULL` that
    accompanies the waiver. If a future edit maps `entity` back to a column,
    the triage queue silently empties; if it drops the key entirely,
    `compile_scope` raises. Neither should happen unnoticed.
    """
    from app.backend.api import integrations

    assert integrations.UNATTRIBUTED_COLUMNS == {
        "entity": None, "plant": None, "location": None, "project": None}
    # Resolved from this file, not from the working directory: pytest can be
    # invoked from anywhere and a relative path would make this pass or fail
    # depending on where somebody stood when they ran it.
    module = (_TESTS_DIR.parent / "app" / "backend" / "api" / "integrations.py")
    source = module.read_text(encoding="utf-8")
    assert source.count("entity_id IS NULL") >= 2, (
        "a route reads through UNATTRIBUTED_COLUMNS without also constraining "
        "entity_id IS NULL, so its only limit is RLS -- which a superuser "
        "connection does not have")


@PG
@pytest.mark.pg
def test_live_the_triage_list_statement_returns_the_unattributed_row(
        pg_connection, pg_url, pg_disposable_db_name):
    """The policy and the compiled predicate agree, which is the whole point.

    Before the API layer existed, migration 012 permitted this read and
    `repo.compile_scope` discarded it -- RLS said yes and the application said
    no, so the row was still invisible to everyone.
    """
    _seed(pg_connection)
    rows = _run(pg_url, pg_disposable_db_name, _LIST_SQL, {},
                entities={"ENT-A"}, triage=True)
    assert [r[0] for r in rows] == [_UNATTRIBUTED]


@PG
@pytest.mark.pg
def test_live_the_triage_list_statement_returns_nothing_without_the_flag(
        pg_connection, pg_url, pg_disposable_db_name):
    """The same statement, run by a principal the router would never give the
    flag to. The waived mapping does NOT make this route open -- RLS is what
    stops it, and this is the test that proves RLS is still the thing standing
    there after all four dimensions were waived."""
    _seed(pg_connection)
    rows = _run(pg_url, pg_disposable_db_name, _LIST_SQL, {},
                entities={"ENT-A"}, triage=False)
    assert rows == [], (
        f"a non-triage principal read {[r[0] for r in rows]} through the "
        f"triage route's own statement. Every scope dimension is waived here, "
        f"so RLS is the only thing left; if it is not enforcing, this route is "
        f"an unrestricted read of every unattributed discrepancy.")


@PG
@pytest.mark.pg
def test_live_attribution_moves_the_row_into_ordinary_scope(
        pg_connection, pg_url, pg_disposable_db_name):
    """Attribute it, and it stops needing triage to be seen.

    That is the workflow completing: the row leaves the bucket only triage can
    read and becomes an ordinary ENT-A row governed like every other.
    """
    _seed(pg_connection)
    rows = _run(pg_url, pg_disposable_db_name, _ATTRIBUTE_SQL,
                {"exception_id": _UNATTRIBUTED, "entity_id": "ENT-A"},
                entities={"ENT-A"}, triage=True)
    assert [(r[0], r[1]) for r in rows] == [(_UNATTRIBUTED, "ENT-A")]

    seen = _visible(pg_url, pg_disposable_db_name,
                    entities={"ENT-A"}, triage=False)
    assert _UNATTRIBUTED in seen, (
        "the row was attributed to ENT-A but an ordinary ENT-A principal "
        "still cannot see it")


@PG
@pytest.mark.pg
def test_live_with_check_alone_refuses_an_out_of_scope_attribution(
        pg_connection, pg_url, pg_disposable_db_name):
    """THE LAYER THAT SURVIVES A DELETED ROUTE.

    The router checks the named entity against the caller's grants before it
    writes, and that check is one edit away from being removed as redundant.
    This test does not go through the router at all: it runs the UPDATE
    directly as a triage principal scoped to ENT-A, naming ENT-B.

    Migration 012's `WITH CHECK` evaluates the NEW row, which is no longer
    NULL-entity, so the CASE falls through to `capex_scope_permits('ENT-B',
    ...)` and refuses. If this ever passes, a triage principal can file any
    entity's discrepancy against any other entity's books.
    """
    _seed(pg_connection)
    refused = False
    try:
        rows = _run(pg_url, pg_disposable_db_name, _ATTRIBUTE_SQL,
                    {"exception_id": _UNATTRIBUTED, "entity_id": "ENT-B"},
                    entities={"ENT-A"}, triage=True)
    except Exception:
        refused = True
    else:
        refused = rows == []

    assert refused, (
        "a triage principal scoped to ENT-A attributed an exception to ENT-B "
        "with the router's own check bypassed entirely. WITH CHECK is what "
        "forbids that, and it is the only layer left once somebody deletes "
        "the Python one as duplicated logic.")

    still_unattributed = _visible(pg_url, pg_disposable_db_name,
                                  entities={"ENT-A"}, triage=True)
    assert _UNATTRIBUTED in still_unattributed


@PG
@pytest.mark.pg
def test_live_attribution_cannot_re_point_an_already_attributed_row(
        pg_connection, pg_url, pg_disposable_db_name):
    """The write is one-way, and the WHERE is what makes it so.

    `entity_id IS NULL` means the statement can only move a row OUT of the
    unattributed bucket. Re-attribution from one entity to another is a
    different decision with a different audit story, and it must not arrive as
    a side effect of an UPDATE that happens to accept any id.
    """
    _seed(pg_connection)
    rows = _run(pg_url, pg_disposable_db_name, _ATTRIBUTE_SQL,
                {"exception_id": _ATTRIBUTED, "entity_id": "ENT-A"},
                entities={"ENT-A"}, triage=True)
    assert rows == [], (
        f"{_ATTRIBUTED} is already attributed to ENT-A and was updated anyway; "
        f"the route would report a fresh attribution and write an audit entry "
        f"for a decision nobody took")
