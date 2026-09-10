"""Row-level security on migration 023's five tables: BEHAVIOUR, not declaration
(Fable 5.1, closing M-4).

Same shape and same reasoning as `tests/test_pg_rls_wave7_matrix.py`: every
assertion runs on `pg_app_database`, whose session issues `SET LOCAL ROLE
capex_app` first, so `current_user` is exactly what production runs as and a
policy weakened to `USING (true)` FAILS here. The superuser `pg_database`
fixture cannot prove a policy, because no policy applies to it.

Three points per entity-scoped table (`fx_revaluation_attempt`,
`period_reopen_request`, both `entity_id NOT NULL`):

    the owning principal    -> exactly their row
    a widened grant         -> both rows      (the policy is not deny-all)
    a peer / an empty grant -> nothing        (an empty set is not "all")

The three reference tables (`currency_denomination`, `fx_rate`, `fx_policy`)
are estate-wide by design, guarded by `capex_principal_present()`: every
established principal -- including one with an EMPTY entity grant -- reads
the same rows. That is asserted too, so the policy cannot silently become an
entity filter that would make the consolidated position depend on the reader.

Fixture note: `fx_revaluation_attempt.bill_id` and
`period_reopen_request.approval_instance_id` are foreign keys into deep
document chains. The rows are inserted by the SUPERUSER fixture under
`session_replication_role = replica`, which suspends FK enforcement for that
transaction only -- a test seam, documented here, that touches no policy and
no grant. What is under test is what `capex_app` can SEE afterwards.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parent))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    ScopedRoleDatabase, pg_admin_connection, pg_app_database, pg_connection,
    pg_database, pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(not os.environ.get("CAPEX_DB_URL"),
                        reason="PostgreSQL not configured; set CAPEX_DB_URL.")
pytestmark = [PG, pytest.mark.pg]

TABLES_023 = ("currency_denomination", "fx_rate", "fx_policy",
              "fx_revaluation_attempt", "period_reopen_request")


def _seed(con) -> dict:
    s = uuid.uuid4().hex[:8]
    ids = {"org": f"ORG-{s}", "ent_a": f"ENT-A-{s}", "ent_b": f"ENT-B-{s}",
           "user_a": f"U-A-{s}", "user_b": f"U-B-{s}",
           "period_a": f"PER-A-{s}", "period_b": f"PER-B-{s}",
           "reval_a": f"RVA-A-{s}", "reval_b": f"RVA-B-{s}",
           "reopen_a": f"RO-A-{s}", "reopen_b": f"RO-B-{s}"}
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,'023 RLS org','T','T') ON CONFLICT DO NOTHING", (ids["org"], ids["org"][:12]))
    for k in ("ent_a", "ent_b"):
        ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'T','T') ON CONFLICT DO NOTHING", (ids[k], ids["org"], ids[k][:12], ids[k]))
    for k in ("user_a", "user_b"):
        ex("INSERT INTO app_user (user_id, email, display_name, principal_kind, created_by, updated_by) "
           "VALUES (%s,%s,%s,'USER','T','T') ON CONFLICT DO NOTHING", (ids[k], f"{ids[k]}@example.test", ids[k]))
    for pk, ek in (("period_a", "ent_a"), ("period_b", "ent_b")):
        ex("INSERT INTO accounting_period (period_id, entity_id, period_start, period_end, state, created_by) "
           "VALUES (%s,%s,DATE '2026-04-01',DATE '2026-04-30','OPEN','T')", (ids[pk], ids[ek]))
    # The two document tables reference bill / approval_instance chains that
    # are not the subject here; suspend FK enforcement for these inserts only.
    ex("SET LOCAL session_replication_role = replica")
    for rk, ek, pk in (("reval_a", "ent_a", "period_a"), ("reval_b", "ent_b", "period_b")):
        ex("INSERT INTO fx_revaluation_attempt (attempt_id, entity_id, period_id, bill_id, source_currency, "
           "booked_rate, booked_rate_date, booked_base_paise, proposed_rate, proposed_rate_date, "
           "proposed_rate_source, proposed_base_paise, delta_paise, outcome, detail, attempted_by) "
           "VALUES (%s,%s,%s,%s,'INR',1,DATE '2026-04-01',100,1,DATE '2026-04-30','TEST',100,0,"
           "'REFUSED','revaluation refused in a test','T')",
           (ids[rk], ids[ek], ids[pk], f"BILL-{rk}"))
    for rk, ek, pk in (("reopen_a", "ent_a", "period_a"), ("reopen_b", "ent_b", "period_b")):
        ex("INSERT INTO period_reopen_request (reopen_id, period_id, entity_id, approval_instance_id, "
           "idempotency_key, reason, closed_by_at_request, requested_by) "
           "VALUES (%s,%s,%s,%s,%s,'reopen for a test','T',%s)",
           (ids[rk], ids[pk], ids[ek], f"INST-{rk}", f"idem-{rk}", ids["user_a"] if rk.endswith("_a") else ids["user_b"]))
    ex("SET LOCAL session_replication_role = origin")
    con.commit()
    return ids


def _scope(user_id, entity_ids) -> Scope:
    return Scope(user_id=user_id, principal_kind="USER", entity_ids=entity_ids,
                 plant_ids=None, project_ids=None, location_ids=None, read_all=False)


def _visible(database, scope, probe, params=()):
    with database.session(scope) as session:
        return sorted(r[0] for r in session.fetchall(probe, params))


@pytest.mark.parametrize("table, pk, key_a, key_b", [
    ("fx_revaluation_attempt", "attempt_id", "reval_a", "reval_b"),
    ("period_reopen_request", "reopen_id", "reopen_a", "reopen_b"),
])
def test_live_entity_scoped_023_tables_confine_reads_to_the_grant(
        pg_connection, pg_app_database, table, pk, key_a, key_b):
    ids = _seed(pg_connection)
    probe = f"SELECT {pk} FROM {table} WHERE {pk} = ANY(%s)"
    both = [ids[key_a], ids[key_b]]

    one = _visible(pg_app_database, _scope(ids["user_a"], frozenset({ids["ent_a"]})), probe, (both,))
    assert one == [ids[key_a]], f"{table}: the owning principal saw {one}, expected only their own row"

    wide = _visible(pg_app_database, _scope(ids["user_a"], frozenset({ids["ent_a"], ids["ent_b"]})), probe, (both,))
    assert wide == sorted(both), f"{table}: a widened grant saw {wide}; the policy is deny-all or wrong"

    peer = _visible(pg_app_database, _scope(ids["user_b"], frozenset({ids["ent_b"]})), probe, (both,))
    assert peer == [ids[key_b]], f"{table}: the peer saw {peer}"

    none = _visible(pg_app_database, _scope(ids["user_a"], frozenset()), probe, (both,))
    assert none == [], f"{table}: an EMPTY grant saw {none}; an empty set is not 'all'"


@pytest.mark.parametrize("table, pk, key_a, key_b, entity_col", [
    ("fx_revaluation_attempt", "attempt_id", "reval_a", "reval_b", "entity_id"),
    ("period_reopen_request", "reopen_id", "reopen_a", "reopen_b", "entity_id"),
])
def test_live_entity_scoped_023_tables_refuse_cross_entity_writes(
        pg_connection, pg_app_database, table, pk, key_a, key_b, entity_col):
    """WITH CHECK: as the application role, an update that would move a row
    out of the caller's entity, and any update to the other entity's row,
    touch nothing -- RLS filters the target set, so the statement is a
    no-op, never a cross-entity write."""
    ids = _seed(pg_connection)
    with pg_app_database.session(_scope(ids["user_a"], frozenset({ids["ent_a"]}))) as session:
        cur = session.execute(f"UPDATE {table} SET {entity_col} = {entity_col} WHERE {pk} = %s", (ids[key_b],))
        assert cur.rowcount == 0, f"{table}: the other entity's row was reachable for UPDATE"
        cur = session.execute(f"DELETE FROM {table} WHERE {pk} = %s", (ids[key_b],))
        assert cur.rowcount == 0, f"{table}: the other entity's row was reachable for DELETE"


def test_live_reference_023_tables_are_estate_wide_for_every_established_principal(
        pg_connection, pg_app_database):
    """`capex_principal_present()`: an EMPTY entity grant still reads the
    reference data, and both entities read the same rows. If someone turned
    the policy into an entity filter, the empty-grant read would be empty."""
    ids = _seed(pg_connection)
    with pg_connection.cursor() as cur:
        cur.execute("INSERT INTO currency_denomination (currency_code, minor_exponent, display_name) "
                    "VALUES ('ZZT', 2, 'Test currency') ON CONFLICT DO NOTHING")
        pg_connection.commit()
    for table, col in (("currency_denomination", "currency_code"), ("fx_rate", "fx_rate_id"), ("fx_policy", "policy_key")):
        try:
            a = _visible(pg_app_database, _scope(ids["user_a"], frozenset({ids["ent_a"]})), f"SELECT {col} FROM {table}")
            b = _visible(pg_app_database, _scope(ids["user_b"], frozenset({ids["ent_b"]})), f"SELECT {col} FROM {table}")
            empty = _visible(pg_app_database, _scope(ids["user_a"], frozenset()), f"SELECT {col} FROM {table}")
        except Exception as exc:  # a column name this test guessed wrong is a test defect, said plainly
            pytest.fail(f"{table}.{col} probe failed: {type(exc).__name__}: {exc}")
        assert a == b == empty, f"{table}: reference rows differ by reader (a={len(a)}, b={len(b)}, empty={len(empty)})"
    assert "ZZT" in _visible(pg_app_database, _scope(ids["user_a"], frozenset()), "SELECT currency_code FROM currency_denomination")


def test_live_every_023_table_forces_rls_with_a_non_trivial_policy(pg_connection):
    """Catalog assertion: RLS enabled AND forced on all five, and no policy
    is the literal `true` -- the weakening the wave-7 finding demonstrated."""
    with pg_connection.cursor() as cur:
        for table in TABLES_023:
            cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", (table,))
            row = cur.fetchone()
            assert row == (True, True), f"{table}: rowsecurity={row}"
            cur.execute("SELECT policyname, qual, with_check FROM pg_policies WHERE tablename = %s", (table,))
            policies = cur.fetchall()
            assert policies, f"{table}: no policy"
            for name, qual, check in policies:
                assert qual and qual.strip().lower() != "true", f"{table}.{name}: USING is trivial ({qual})"
                assert check and check.strip().lower() != "true", f"{table}.{name}: WITH CHECK is trivial ({check})"
