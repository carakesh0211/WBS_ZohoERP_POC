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
