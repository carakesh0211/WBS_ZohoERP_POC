"""Row-level security on Wave 7's tables: BEHAVIOUR, not declaration.

Why this file exists
====================
Every check that existed on migrations 017, 018 and 019's policies before this
file was a check that a policy EXISTS:

* `tests/test_pg_rls_coverage.py:102` matches the migration text against
  `CREATE POLICY \\w+ ON <table>`. `CREATE POLICY p ON t USING (true)` matches
  that regex perfectly.
* The live half reads `pg_class.relrowsecurity` / `relforcerowsecurity`, which
  are flags. Neither ever evaluates a predicate.
* `tests/test_pg_closure.py:623,635` and `tests/test_pg_export_scope.py:700`
  DO run live, and they run on the `pg_database` fixture -- which connects as
  CI's `POSTGRES_USER: capex`, a cluster SUPERUSER. A superuser bypasses
  row-level security unconditionally, and `FORCE ROW LEVEL SECURITY` does not
  change that: it governs whether the table's OWNER is subject to its own
  policies and says nothing about superusers. Those tests prove the
  APPLICATION predicate in `pg/closure.py` and `pg/exports.py`. They cannot
  prove the policy, because no policy applied to the session.

The consequence was demonstrated rather than argued: changing any 019 policy to
`USING (true)` left the entire suite green.

`tests/test_pg_reporting.py` used to claim this ground was covered by
`test_pg_rls_coverage.py` and `test_pg_rls_integration_matrix.py` "through the
scoped-role fixture". It was not. That matrix derives `MATRIX_TABLES` from
migrations 010 and 011 only, and coverage iterates 006's eight tables. Neither
names a single 017/018/019 table. The claim is corrected there; this file is
the coverage it named.

Everything here runs on `pg_app_database` -- `ScopedRoleDatabase`, which issues
`SET LOCAL ROLE capex_app` first, making `current_user` in the test transaction
exactly what it is in production. Nothing is disabled, relaxed or granted
differently; the only change is an identity that RLS can constrain.

Three points per table, because one is not enough
=================================================
Asserting only "the other principal's row is hidden" passes against a policy
that returns false for everything -- which hides the other row and also hides
your own, and gets found later by a user wondering why their list is empty. So
each table is asserted at

    the owning principal      -> exactly their row
    a widened grant           -> both rows        (the policy is not deny-all)
    an empty grant / a peer   -> nothing          (an empty set is not "all")

A policy weakened to `USING (true)` fails the first and third points. A policy
broken to deny-all fails the second. That is the property that was missing.

A skip is not a pass
====================
There is no PostgreSQL on the machine this file was written on, so every test
here is `@PG` and `@pytest.mark.pg` and SKIPS locally. They first execute in
CI's `pg_tests` job. Nothing below is claimed to have passed until it has.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly. Without this the
# live tests fail at SETUP with "fixture not found" -- but only where
# CAPEX_DB_URL is set, so the gap would be invisible on every workstation and
# would first appear in CI. The sibling matrix does exactly this, for exactly
# that reason.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# =========================================================================
# Seeding. Two entities, two principals, one project per entity.
# =========================================================================

def _seed_estate(con) -> dict:
    """An organisation, two entities, a project in each, and two users.

    Returns the ids so a test never repeats a literal that the seed owns.
    """
    suffix = uuid.uuid4().hex[:8]
    ids = {
        "org": f"ORG-{suffix}",
        "entity_a": f"ENT-A-{suffix}",
        "entity_b": f"ENT-B-{suffix}",
        "project_a": f"PRJ-A-{suffix}",
        "project_b": f"PRJ-B-{suffix}",
        "user_a": f"U-A-{suffix}",
        "user_b": f"U-B-{suffix}",
    }
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES (%s, %s, 'Wave 7 RLS org', 'T', 'T')"
        " ON CONFLICT DO NOTHING", (ids["org"], ids["org"][:12]))
    for key in ("entity_a", "entity_b"):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (ids[key], ids["org"], ids[key][:12], ids[key]))
    for pkey, ekey in (("project_a", "entity_a"), ("project_b", "entity_b")):
        con.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (ids[pkey], ids[ekey], ids[pkey], ids[pkey]))
    for key in ("user_a", "user_b"):
        con.execute(
            "INSERT INTO app_user (user_id, email, display_name,"
            " principal_kind, created_by, updated_by)"
            " VALUES (%s, %s, %s, 'USER', 'T', 'T') ON CONFLICT DO NOTHING",
            (ids[key], f"{ids[key]}@example.test", ids[key]))
    con.commit()
    return ids


def _scope(user_id: str, entity_ids, *, project_ids=None,
           principal_kind: str = "USER") -> Scope:
    """A deliberately restricted principal.

    `entity_ids` and `project_ids` are passed through untouched, so a test can
    hand in `None` (unrestricted), `frozenset()` (nothing) or a populated set
    and all three stay distinguishable end to end -- the distinction the wire
    format exists to keep.
    """
    return Scope(
        user_id=user_id, principal_kind=principal_kind,
        entity_ids=entity_ids, plant_ids=None,
        project_ids=project_ids, location_ids=None, read_all=False)


def _visible(database, scope, probe, params=None) -> list[str]:
    with database.session(scope) as session:
        # scope-exempt: this query IS the assertion about RLS.
        rows = session.fetchall(probe, params or ())
    return sorted(row[0] for row in rows)


# =========================================================================
# 017 -- report_saved_view. Entity scope AND the private/shared disjunction.
# =========================================================================

def _seed_views(con, ids) -> None:
    """Four views: A-private, A-shared, B-private, B-shared."""
    for view_id, entity, owner, visibility in (
            ("RV-A-PRIV", ids["entity_a"], ids["user_a"], "PRIVATE"),
            ("RV-A-SHAR", ids["entity_a"], ids["user_a"], "SHARED"),
            ("RV-B-PRIV", ids["entity_b"], ids["user_b"], "PRIVATE"),
            ("RV-B-SHAR", ids["entity_b"], ids["user_b"], "SHARED")):
        con.execute(
            "INSERT INTO report_saved_view (view_id, entity_id, report_key,"
            " name, owner_user_id, visibility, definition, created_by,"
            " updated_by) VALUES (%s, %s, 'executive_dashboard', %s, %s, %s,"
            " '{}'::jsonb, 'T', 'T')",
            (view_id, entity, view_id, owner, visibility))
    con.commit()


_VIEW_PROBE = "SELECT view_id FROM report_saved_view ORDER BY view_id"


@PG
@pytest.mark.pg
def test_live_a_private_saved_view_is_invisible_to_a_colleague(
        pg_connection, pg_app_database):
    """The half `capex_scope_permits` cannot decide.

    A colleague in the SAME entity passes the entity predicate. What must still
    hide A's private view is the `visibility`/`owner_user_id` disjunction, and
    that is the half no existing test exercised.
    """
    ids = _seed_estate(pg_connection)
    _seed_views(pg_connection, ids)

    owner_sees = _visible(pg_app_database,
                          _scope(ids["user_a"], frozenset({ids["entity_a"]})),
                          _VIEW_PROBE)
    assert owner_sees == ["RV-A-PRIV", "RV-A-SHAR"], (
        f"the owner could not see their own views: {owner_sees}")

    # A second principal, granted the SAME entity. The entity predicate passes
    # for them; only the ownership disjunction can hide the private view.
    peer_sees = _visible(pg_app_database,
                         _scope(ids["user_b"], frozenset({ids["entity_a"]})),
                         _VIEW_PROBE)
    assert peer_sees == ["RV-A-SHAR"], (
        f"a colleague in the same entity read a PRIVATE saved view, or could "
        f"not read a SHARED one. Saw: {peer_sees}")


@PG
@pytest.mark.pg
def test_live_saved_views_are_confined_to_the_principals_entity(
        pg_connection, pg_app_database):
    """Three points, so neither `USING (true)` nor deny-all can pass."""
    ids = _seed_estate(pg_connection)
    _seed_views(pg_connection, ids)

    one = _visible(pg_app_database,
                   _scope(ids["user_a"], frozenset({ids["entity_a"]})),
                   _VIEW_PROBE)
    assert one == ["RV-A-PRIV", "RV-A-SHAR"], (
        f"the policy did not confine the read to the principal's entity; "
        f"another entity's views were visible. Saw: {one}")

    # Widened. A deny-all policy passes the assertion above and fails here.
    both = _visible(
        pg_app_database,
        _scope(ids["user_a"], frozenset({ids["entity_a"], ids["entity_b"]})),
        _VIEW_PROBE)
    assert both == ["RV-A-PRIV", "RV-A-SHAR", "RV-B-SHAR"], (
        f"a principal granted BOTH entities saw the wrong set. Expected their "
        f"own two plus the other entity's SHARED view, and never the other "
        f"entity's PRIVATE one. Saw: {both}")

    # Empty is not "all". `USING (true)` fails here.
    none = _visible(pg_app_database,
                    _scope(ids["user_a"], frozenset()), _VIEW_PROBE)
    assert none == [], (
        f"a principal with an EMPTY entity grant saw rows. An empty set means "
        f"'nothing' and must never be read as 'unrestricted'. Saw: {none}")


@PG
@pytest.mark.pg
def test_live_a_shared_view_cannot_be_deleted_by_someone_who_does_not_own_it(
        pg_connection, pg_app_database):
    """Migration 022's reason for existing.

    017 expressed the whole control as ONE `FOR ALL` policy whose `WITH CHECK`
    was narrower than its `USING`. PostgreSQL never consults `WITH CHECK` for
    DELETE, and 017:227 grants DELETE -- so any principal in the entity could
    delete another user's SHARED view, because `USING` passes on
    `visibility = 'SHARED'`. 022 splits the policy per command.
    """
    ids = _seed_estate(pg_connection)
    _seed_views(pg_connection, ids)

    peer = _scope(ids["user_b"], frozenset({ids["entity_a"]}))
    with pg_app_database.session(peer) as session:
        # scope-exempt: the DELETE is the assertion.
        session.execute(
            "DELETE FROM report_saved_view WHERE view_id = 'RV-A-SHAR'")

    survivors = _visible(pg_app_database,
                         _scope(ids["user_a"], frozenset({ids["entity_a"]})),
                         _VIEW_PROBE)
    assert "RV-A-SHAR" in survivors, (
        "a principal who does not own a SHARED saved view deleted it. RLS "
        "silently filters the rows a DELETE may touch rather than raising, so "
        "the caller saw success and the owner's view is gone.")


@PG
@pytest.mark.pg
def test_live_a_saved_views_owner_cannot_be_reassigned(
        pg_connection, pg_app_database):
    """022's trigger, from the side that matters.

    Nothing in 017 made `owner_user_id` immutable, so `SET owner_user_id = me`
    passed both limbs of the single policy: the row's new owner is the caller,
    so `WITH CHECK` is satisfied by the very edit that steals it.
    """
    ids = _seed_estate(pg_connection)
    _seed_views(pg_connection, ids)

    peer = _scope(ids["user_b"], frozenset({ids["entity_a"]}))
    with pytest.raises(Exception) as excinfo:
        with pg_app_database.session(peer) as session:
            session.execute(
                "UPDATE report_saved_view SET owner_user_id = %s"
                "  WHERE view_id = 'RV-A-SHAR'", (ids["user_b"],))
    assert "owner" in str(excinfo.value).lower(), (
        f"the refusal did not name the reason: {excinfo.value}")

    with pg_connection.cursor() as cur:      # superuser, RLS bypassed on purpose
        cur.execute("SELECT owner_user_id FROM report_saved_view"
                    " WHERE view_id = 'RV-A-SHAR'")
        owner = cur.fetchone()[0]
    assert owner == ids["user_a"], (
        f"the saved view changed hands: {owner}")


# =========================================================================
# 018 -- export_job. The policy alone, with no application predicate.
# =========================================================================

def _seed_jobs(con, ids) -> None:
    for job_id, requester in (("EXP-A", ids["user_a"]), ("EXP-B", ids["user_b"])):
        con.execute(
            "INSERT INTO export_job (export_job_id, dataset, requested_by,"
            " scope_json, scope_digest, column_order, created_by, updated_by)"
            " VALUES (%s, 'wbs_positions', %s, '{}'::jsonb, 'digest',"
            " ARRAY['a'], 'T', 'T')", (job_id, requester))
    con.commit()


_JOB_PROBE = "SELECT export_job_id FROM export_job ORDER BY export_job_id"


@PG
@pytest.mark.pg
def test_live_an_export_job_is_invisible_to_anyone_but_its_requester(
        pg_connection, pg_app_database):
    """NO `requested_by` predicate in the query. That is the whole point.

    `pg/exports.py` filters on the requester itself, and
    `test_pg_export_scope.py` proves that filter. Both would still pass if the
    policy were `USING (true)`, because the application never asks the database
    to be the control. This query asks exactly that and nothing else.
    """
    ids = _seed_estate(pg_connection)
    _seed_jobs(pg_connection, ids)

    a_sees = _visible(pg_app_database,
                      _scope(ids["user_a"], None), _JOB_PROBE)
    assert a_sees == ["EXP-A"], (
        f"the requester saw the wrong set with no application predicate "
        f"applied. Saw: {a_sees}")

    b_sees = _visible(pg_app_database,
                      _scope(ids["user_b"], None), _JOB_PROBE)
    assert b_sees == ["EXP-B"], (
        f"a second principal read another user's export job -- which carries "
        f"that user's resolved scope and the rendered rows. Saw: {b_sees}")

    # Both principals see exactly one row each, and they are different rows.
    # A `USING (true)` policy gives both principals both rows and fails above;
    # a deny-all policy gives neither principal anything and fails too.
    assert a_sees != b_sees


@PG
@pytest.mark.pg
def test_live_the_export_worker_sees_every_job_and_only_as_the_service(
        pg_connection, pg_app_database):
    """`export_job_service` is the second policy, and it is narrow on purpose.

    The worker must advance any job. It gains no business data from that,
    because every business read it issues is compiled from the job's rehydrated
    requester scope. But the exemption is keyed to one named principal, so a
    SERVICE principal that is not `SVC-EXPORT` must not inherit it.
    """
    ids = _seed_estate(pg_connection)
    _seed_jobs(pg_connection, ids)
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-EXPORT', 'svc-export@example.test',"
        " 'Export worker', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-SWEEP', 'svc-sweep@example.test',"
        " 'Sweep', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    pg_connection.commit()

    worker = _scope("SVC-EXPORT", None, principal_kind="SERVICE")
    assert _visible(pg_app_database, worker, _JOB_PROBE) == ["EXP-A", "EXP-B"], (
        "the export worker could not see the queue it exists to drain")

    other_service = _scope("SVC-SWEEP", None, principal_kind="SERVICE")
    assert _visible(pg_app_database, other_service, _JOB_PROBE) == [], (
        "a SERVICE principal that is not SVC-EXPORT inherited the worker's "
        "exemption. The policy names one principal; being a service is not "
        "the qualification.")


# =========================================================================
# 019 -- capitalisation_request, reached through its project.
# =========================================================================

def _seed_cap_requests(con, ids) -> None:
    for cap_id, project in (("CAP-A", ids["project_a"]),
                            ("CAP-B", ids["project_b"])):
        con.execute(
            "INSERT INTO capitalisation_request (cap_id, cap_number,"
            " project_id, requested_by, created_by, updated_by)"
            " VALUES (%s, %s, %s, 'T', 'T', 'T')",
            (cap_id, cap_id, project))
    con.commit()


_CAP_PROBE = "SELECT cap_id FROM capitalisation_request ORDER BY cap_id"


@PG
@pytest.mark.pg
def test_live_a_capitalisation_request_is_confined_to_its_projects_entity(
        pg_connection, pg_app_database):
    """The three points again, on the table carrying the capital position.

    `capitalisation_request` has no dimension column of its own; the policy
    reaches one through `project`. An EXISTS that resolved to true for every
    row would expose one entity's capitalisation pipeline to another, which is
    exactly what a `USING (true)` regression looks like from the outside.
    """
    ids = _seed_estate(pg_connection)
    _seed_cap_requests(pg_connection, ids)

    one = _visible(pg_app_database,
                   _scope(ids["user_a"], frozenset({ids["entity_a"]})),
                   _CAP_PROBE)
    assert one == ["CAP-A"], (
        f"another entity's capitalisation request was visible. Saw: {one}")

    both = _visible(
        pg_app_database,
        _scope(ids["user_a"], frozenset({ids["entity_a"], ids["entity_b"]})),
        _CAP_PROBE)
    assert both == ["CAP-A", "CAP-B"], (
        f"a principal granted BOTH entities saw fewer than both, so the policy "
        f"is denying rows it should permit. Saw: {both}")

    none = _visible(pg_app_database,
                    _scope(ids["user_a"], frozenset()), _CAP_PROBE)
    assert none == [], (
        f"an EMPTY entity grant saw capitalisation requests. Saw: {none}")


@PG
@pytest.mark.pg
def test_live_project_restriction_narrows_within_the_entity(
        pg_connection, pg_app_database):
    """A project grant is not a second entity grant.

    `capex_scope_permits` receives all four dimensions here, so a principal
    entitled to the entity but restricted to one project must not read the
    other project's request. Without this the entity assertions above would
    pass on a policy that only ever consults `entity_id`.
    """
    ids = _seed_estate(pg_connection)
    extra_project = f"PRJ-A2-{uuid.uuid4().hex[:8]}"
    pg_connection.execute(
        "INSERT INTO project (project_id, entity_id, capex_code, name,"
        " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')",
        (extra_project, ids["entity_a"], extra_project, extra_project))
    pg_connection.execute(
        "INSERT INTO capitalisation_request (cap_id, cap_number, project_id,"
        " requested_by, created_by, updated_by)"
        " VALUES ('CAP-A2', 'CAP-A2', %s, 'T', 'T', 'T')", (extra_project,))
    pg_connection.commit()
    _seed_cap_requests(pg_connection, ids)

    scoped = _scope(ids["user_a"], frozenset({ids["entity_a"]}),
                    project_ids=frozenset({ids["project_a"]}))
    seen = _visible(pg_app_database, scoped, _CAP_PROBE)
    assert seen == ["CAP-A"], (
        f"a principal restricted to one project read another project's "
        f"capitalisation request inside the same entity. Saw: {seen}")
