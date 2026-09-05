"""Live PostgreSQL proof of the approval engine's concurrency contracts.

Wave 4 stream 2 (M4b). Contracts 7 (lock order), 8 (idempotency and staleness)
and 9 (audit) make claims that a mirror in Python cannot settle: whether
PostgreSQL really serialises two approvers on one stage, whether the ordered
lock set really keeps a randomised load deadlock-free, whether an idempotency
key really applies once when two requests arrive together. Those are here.
Everything provable without a server is in ``tests/test_pg_approvals.py`` and
runs everywhere.

These run only where ``CAPEX_DB_URL`` is set -- CI's ``postgres:16`` service.
They SKIP on the development machine, which has no local PostgreSQL, so a green
local run is **not** evidence that they passed. Said plainly rather than left to
be inferred: the counts CI reports are the ones that count.

A second gate, and why it is a skip rather than a failure
--------------------------------------------------------
The approval tables come from ``migrations/pg/008_approval_engine.sql``, which
belongs to **stream 1** and is not in this worktree. Where the migration has not
landed, :func:`approval_schema` skips with a message naming it, because "the
table does not exist yet" is a statement about the integration order, not about
this engine's behaviour. Once 008 is merged the gate stops firing on its own and
these tests run for real -- there is nothing to remember to switch on. The
distinction that matters is that the gate tests for the *schema*, never for a
result: no assertion below is weakened or skipped past.

Each test builds and removes its own fixture. Nothing depends on demo seed data.
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

import os                                                        # noqa: E402
import random                                                    # noqa: E402
import threading                                                 # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date                                        # noqa: E402

import psycopg                                                   # noqa: E402
import pytest                                                    # noqa: E402
from psycopg.types.json import Jsonb                             # noqa: E402

from app.backend.pg import approval_rules as rules               # noqa: E402
from app.backend.pg import approvals as engine                   # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        not os.environ.get("CAPEX_DB_URL"),
        reason="needs a live PostgreSQL (CAPEX_DB_URL)"),
]


# ==========================================================================
# Gates and fixtures
# ==========================================================================
@pytest.fixture()
def approval_schema(pg_connection):
    """Skip cleanly until stream 1's migration 008 has landed.

    Checks for the tables themselves rather than for a migration version
    string, so it answers the question that actually matters to the statements
    below. It asserts nothing about behaviour: where the schema IS present,
    every test runs in full.
    """
    missing = [name for name in ("approval_definition", "approval_instance",
                                 "approval_stage_instance", "approval_assignment",
                                 "approval_action", "approval_delegation")
               if pg_connection.execute(
                   "SELECT to_regclass(%s)", (name,)).fetchone()[0] is None]
    if missing:
        pytest.skip(
            f"approval schema not present ({', '.join(missing)}). These tables "
            f"come from migrations/pg/008_approval_engine.sql, owned by Wave 4 "
            f"stream 1, which has not been merged into this worktree. The gate "
            f"clears itself once 008 lands; nothing here needs switching on.")

    # Contract 8 needs somewhere to keep the idempotency key and the original
    # outcome. Contract 1's approval_action column list declares neither, and
    # this engine is written against both -- reported to the lead as a schema
    # gap. Fail loudly rather than skipping: unlike the tables above, this is a
    # disagreement between two frozen documents and must not pass unnoticed.
    columns = {row[0] for row in pg_connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'approval_action'").fetchall()}
    assert {"idempotency_key", "outcome"} <= columns, (
        "approval_action is missing idempotency_key and/or outcome. Contract 8 "
        "requires an idempotency_key whose replay returns the ORIGINAL outcome; "
        "contract 1's column list declares no column for either. Stream 1 must "
        "add both (plus UNIQUE (instance_id, idempotency_key)), or name the "
        f"alternative it prefers. Present: {sorted(columns)}")
    return True


def _scope() -> Scope:
    """A deliberately unrestricted scope: these tests are about locking and
    ordering, and a scope refusal here would be a different test failing."""
    return Scope(user_id="U-TEST-APPROVALS", principal_kind="USER", read_all=True)


def _seed_estate(con, suffix, *, budget_paise=100_000_000, commitment=0):
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    wbs, head = f"W_{suffix}", f"H_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (head, ent, f"HC_{suffix}", "Head"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')", (wbs, prj, wbs, "n", wbs))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
       "VALUES (%s,%s,%s,'t')", (wbs, head, budget_paise))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, commitment_paise, "
       "actual_paise, pr_reserved_paise, updated_by) VALUES (%s,%s,%s,0,0,'t')",
       (wbs, head, commitment))
    con.commit()
    return {"entity": ent, "project": prj, "wbs": wbs, "head": head}


def _seed_users(con, users):
    """`users` is {user_id: [role, ...]}, with role names from role_grant's CHECK."""
    # The engine attributes system-initiated actions -- an auto-supersede,
    # an escalation -- to SYSTEM, and approval_action.actor_user_id is a
    # foreign key to app_user. SYSTEM therefore has to BE a principal,
    # not a magic string: plan section 10.3 says background principals
    # are real rows with principal_kind='SERVICE', never an implicit
    # "no user" bypass. Omitting it failed every decision that supersedes
    # or escalates, and only against live PostgreSQL.
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
        "created_by, updated_by) "
        "VALUES ('SYSTEM','system@example.test','System','SERVICE','t','t') "
        "ON CONFLICT (user_id) DO NOTHING")
    for user_id, roles in users.items():
        con.execute(
            "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
            "VALUES (%s,%s,%s,'t','t') ON CONFLICT (user_id) DO NOTHING",
            (user_id, f"{user_id}@example.test", user_id))
        for role in roles:
            con.execute(
                "INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s,%s,'t') "
                "ON CONFLICT (user_id, role) DO NOTHING", (user_id, role))
    con.commit()


def _seed_definition(con, suffix, *, entity, stages, object_type="BUDGET_REVISION"):
    """One ACTIVE definition with a catch-all rule and the stages given.

    `stages` is a list of dicts: stage_no, quorum_type, quorum_n,
    parallel_group, approvers ([(kind, ref)]), requires_reason, applies_when.
    """
    definition_id = f"AD_{suffix}"
    con.execute(
        # created_by is NOT NULL with no default on the three configuration
        # tables -- a workflow definition with no recorded author is exactly
        # the row an auditor would ask about. There is NO updated_by on any of
        # them: unlike the org tables, an ACTIVE definition is immutable, so a
        # last-updated column would only ever restate created_by. Adding one to
        # this INSERT was my first correction and it was wrong -- both failures
        # were visible only in CI, there being no local PostgreSQL.
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        # ck_approval_definition_activation: a non-DRAFT row MUST carry an
        # activation stamp, because it cannot have become active without
        # one. Inserting ACTIVE with a null stamp is the state the
        # constraint exists to forbid -- an approval definition that is
        # live with no record of who made it live.
        "status, entity_id, effective_from, created_by, "
        "activated_at, activated_by) "
        "VALUES (%s,%s,%s,1,%s,%s,%s,'TEST',now(),'TEST')",
        (definition_id, object_type, f"CODE_{suffix}", rules.DEF_ACTIVE, entity,
         date(2020, 1, 1)))
    con.execute(
        "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
        "created_by) VALUES (%s,%s,10,%s,'TEST')",
        (f"AR_{suffix}", definition_id, Jsonb({"op": "true"})))
    for spec in stages:
        stage_id = f"AS_{suffix}_{spec['stage_no']}"
        con.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
            "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
            "created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'TEST')",
            (stage_id, definition_id, spec["stage_no"], f"Stage {spec['stage_no']}",
             spec.get("parallel_group"), spec.get("quorum_type", rules.QUORUM_ANY),
             spec.get("quorum_n"),
             Jsonb(spec["applies_when"]) if spec.get("applies_when") else None,
             spec.get("sla_hours"), spec.get("escalate_after_hours"),
             Jsonb(spec["escalate_to"]) if spec.get("escalate_to") else None,
             spec.get("allow_delegation", True), spec.get("requires_reason", False)))
        for ordinal, (kind, ref) in enumerate(spec["approvers"], start=1):
            con.execute(
                "INSERT INTO approval_stage_approver (stage_id, ordinal, "
                "approver_kind, approver_ref, scope_expr) VALUES (%s,%s,%s,%s,NULL)",
                (stage_id, ordinal, kind, ref))
    con.commit()
    return definition_id


def _seed_revision(con, suffix, ids, *, created_by, delta_paise=1_000_000):
    revision_id = f"BR_{suffix}"
    con.execute(
        "INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, "
        "delta_paise, effective_from, justification, status, created_by) "
        "VALUES (%s,%s,%s,%s,%s,'test','DRAFT',%s)",
        (revision_id, ids["wbs"], ids["head"], delta_paise, date(2026, 1, 1),
         created_by))
    con.commit()
    return revision_id


def _snapshot(ids, revision_id, *, delta_paise=1_000_000, available_paise=100_000_000):
    return {
        "object_type": "BUDGET_REVISION",
        "object_id": revision_id,
        "entity_id": ids["entity"],
        "project_id": ids["project"],
        "budget_head_id": ids["head"],
        "delta_paise": delta_paise,
        "amount_paise": delta_paise,
        "affected_cells": [{"wbs_id": ids["wbs"], "budget_head_id": ids["head"]}],
        "budget_checks": [{"wbs_id": ids["wbs"], "budget_head_id": ids["head"],
                            "requested_paise": delta_paise,
                            "available_paise": available_paise,
                            "verdict": "OK"}],
    }


def _open(pg_database, ids, revision_id, *, maker, snapshot=None):
    with pg_database.session(_scope()) as session:
        return engine.open_instance(
            session, object_type="BUDGET_REVISION", object_id=revision_id,
            object_version=1, snapshot=snapshot or _snapshot(ids, revision_id),
            maker_user_id=maker, business_date=date(2026, 6, 1))


def _run_concurrently(targets):
    """Run each callable on its own thread, released together by a barrier.

    Returns a list of ``(result, exception)`` in the order the callables were
    given. A barrier rather than a sleep: the point is that the two
    transactions genuinely overlap, and a sleep only makes that likely.
    """
    barrier = threading.Barrier(len(targets))
    results: list = [None] * len(targets)

    def wrap(index, fn):
        def inner():
            barrier.wait(timeout=30)
            try:
                results[index] = (fn(), None)
            except BaseException as exc:            # noqa: BLE001 - reported, not swallowed
                results[index] = (None, exc)
        return inner

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for thread in threads:
        assert not thread.is_alive(), (
            "a worker thread did not finish within 60s: the transactions are "
            "deadlocked or blocked indefinitely, which is exactly what the "
            "ordered lock set exists to prevent")
    return results


# ==========================================================================
# Two approvers, one final stage
# ==========================================================================
@pytest.mark.slow
def test_two_simultaneous_decisions_on_the_final_stage_produce_one_transition(
        pg_connection, pg_database, approval_schema):
    """Contract 7: exactly one transition, by construction of the row lock.

    Both approvers are assignees of the same ANY-quorum final stage, so both
    decisions are individually legitimate. They meet at the
    ``SELECT ... FOR UPDATE`` on ``approval_instance``; one waits, and by the
    time it proceeds the stage is closed and it is answered rather than applying
    a second transition on top of the first.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"],
                                 "U-A": ["Finance"], "U-B": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")
    assert opened["status"] == rules.INST_OPEN

    def decide_as(user_id):
        def run():
            with pg_database.session(_scope()) as session:
                return engine.decide(
                    session, instance_id=opened["instance_id"],
                    actor_user_id=user_id, action=rules.ACTION_APPROVE,
                    idempotency_key=f"key-{user_id}", object_version=1)
        return run

    outcomes = _run_concurrently([decide_as("U-A"), decide_as("U-B")])
    succeeded = [r for r, exc in outcomes if exc is None]
    failed = [exc for _r, exc in outcomes if exc is not None]

    assert len(succeeded) == 1, (
        f"exactly one decision must transition the instance; "
        f"{len(succeeded)} succeeded")
    assert succeeded[0].instance_status == rules.INST_APPROVED
    assert len(failed) == 1
    assert isinstance(failed[0], rules.ApprovalError)
    assert failed[0].code in (rules.ERR_STAGE_NOT_OPEN, rules.ERR_NOT_AN_ASSIGNEE)

    status = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (opened["instance_id"],)).fetchone()[0]
    assert status == rules.INST_APPROVED

    applied = pg_connection.execute(
        "SELECT count(*) FROM approval_action WHERE instance_id = %s AND action = %s",
        (opened["instance_id"], rules.ACTION_APPROVE)).fetchone()[0]
    assert applied == 1, "the losing decision must leave no action behind"


@pytest.mark.slow
def test_two_approvers_on_a_parallel_group_both_complete_in_either_order(
        pg_connection, pg_database, approval_schema):
    """Section 9.3: stages sharing a ``parallel_group`` open together and the
    group completes when every stage meets quorum, **in either order**.

    Both decisions must succeed -- they are on different stages -- and the
    instance must be APPROVED once the second lands, whichever that turns out
    to be.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"],
                                 "U-A": ["Finance"], "U-B": ["CFO"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "parallel_group": "G2", "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]},
        {"stage_no": 2, "parallel_group": "G2", "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "CFO")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")
    assert sorted(opened["opened_stage_nos"]) == [1, 2], (
        "both stages of a parallel group must open together")

    def decide_as(user_id):
        def run():
            with pg_database.session(_scope()) as session:
                return engine.decide(
                    session, instance_id=opened["instance_id"],
                    actor_user_id=user_id, action=rules.ACTION_APPROVE,
                    idempotency_key=f"key-{user_id}", object_version=1)
        return run

    outcomes = _run_concurrently([decide_as("U-A"), decide_as("U-B")])
    for result, exc in outcomes:
        assert exc is None, f"a parallel-group decision failed: {exc!r}"
        assert result.stage_status == rules.STAGE_APPROVED

    rows = dict(pg_connection.execute(
        "SELECT stage_no, status FROM approval_stage_instance WHERE instance_id = %s",
        (opened["instance_id"],)).fetchall())
    assert rows == {1: rules.STAGE_APPROVED, 2: rules.STAGE_APPROVED}

    status = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (opened["instance_id"],)).fetchone()[0]
    assert status == rules.INST_APPROVED, (
        "the group completes when every stage meets quorum, in either order")


# ==========================================================================
# Idempotency under concurrency (contract 8)
# ==========================================================================
@pytest.mark.slow
def test_an_idempotent_replay_under_concurrency_applies_exactly_once(
        pg_connection, pg_database, approval_schema):
    """The same key, twice, at the same instant: one application, one row.

    This is the retry a client makes when a response is lost in flight, arriving
    while the original is still in progress. Both callers must be answered with
    the same outcome and the decision must be applied once.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")

    def replay():
        with pg_database.session(_scope()) as session:
            return engine.decide(
                session, instance_id=opened["instance_id"], actor_user_id="U-A",
                action=rules.ACTION_APPROVE, idempotency_key="the-same-key",
                object_version=1)

    outcomes = _run_concurrently([replay, replay])
    results = [r for r, exc in outcomes if exc is None]
    errors = [exc for _r, exc in outcomes if exc is not None]

    # One of two legal shapes: both answered (the second as a replay), or the
    # second refused by the UNIQUE (instance_id, idempotency_key) index. Either
    # way the decision applied ONCE, which is the contract.
    assert not [e for e in errors if not isinstance(
        e, (rules.ApprovalError, psycopg.errors.UniqueViolation))], errors
    assert results, "at least one caller must be answered"
    assert all(r.instance_status == rules.INST_APPROVED for r in results)

    rows = pg_connection.execute(
        "SELECT count(*) FROM approval_action WHERE instance_id = %s "
        "AND idempotency_key = %s", (opened["instance_id"], "the-same-key")).fetchone()[0]
    assert rows == 1, f"the key must apply exactly once, found {rows} actions"

    # A later, sequential replay returns the ORIGINAL outcome, flagged.
    with pg_database.session(_scope()) as session:
        again = engine.decide(
            session, instance_id=opened["instance_id"], actor_user_id="U-A",
            action=rules.ACTION_APPROVE, idempotency_key="the-same-key",
            object_version=1)
    assert again.replayed is True
    assert again.code == rules.ERR_IDEMPOTENT_REPLAY
    assert again.instance_status == rules.INST_APPROVED
    assert pg_connection.execute(
        "SELECT count(*) FROM approval_action WHERE instance_id = %s "
        "AND idempotency_key = %s",
        (opened["instance_id"], "the-same-key")).fetchone()[0] == 1


def test_a_replay_is_answered_even_after_the_instance_has_moved_on(
        pg_connection, pg_database, approval_schema):
    """Contract 8: the replay branch runs BEFORE staleness and budget checks.

    A retry of a request that already succeeded must return what it returned,
    not a fresh OBJECT_VERSION_STALE caused by the very change the original
    decision set in motion.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")

    with pg_database.session(_scope()) as session:
        first = engine.decide(
            session, instance_id=opened["instance_id"], actor_user_id="U-A",
            action=rules.ACTION_APPROVE, idempotency_key="k1", object_version=1)

    # The document moves on underneath -- exactly what makes a naive retry fail.
    pg_connection.execute(
        "UPDATE budget_revision SET version_no = version_no + 1 WHERE revision_id = %s",
        (revision_id,))
    pg_connection.commit()

    with pg_database.session(_scope()) as session:
        replay = engine.decide(
            session, instance_id=opened["instance_id"], actor_user_id="U-A",
            action=rules.ACTION_APPROVE, idempotency_key="k1", object_version=1)
    assert replay.replayed is True
    assert replay.instance_status == first.instance_status

    # A DIFFERENT key against the stale version is refused, which is what shows
    # the replay above was answered on its key and not on a weakened check.
    with pytest.raises(rules.ApprovalError) as excinfo:
        with pg_database.session(_scope()) as session:
            engine.decide(
                session, instance_id=opened["instance_id"], actor_user_id="U-A",
                action=rules.ACTION_APPROVE, idempotency_key="k2", object_version=1)
    assert excinfo.value.code in (rules.ERR_OBJECT_VERSION_STALE,
                                  rules.ERR_STAGE_NOT_OPEN)


# ==========================================================================
# Budget revalidation inside the transaction (contract 7)
# ==========================================================================
def test_a_decision_racing_a_budget_change_fails_budget_moved(
        pg_connection, pg_database, approval_schema):
    """Contract 7: availability is re-checked INSIDE the transaction, after the
    locks, and a decision is refused rather than applied against stale numbers.

    The commitment is raised between routing and the final approval, so the
    availability the approvers were shown no longer exists. The approval must
    not land.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix, budget_paise=100_000_000)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    snapshot = _snapshot(ids, revision_id, delta_paise=1_000_000,
                          available_paise=100_000_000)
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER", snapshot=snapshot)

    # Somebody else commits spend against the same cell.
    pg_connection.execute(
        "UPDATE budget_ledger_cell SET commitment_paise = 99_500_000 "
        "WHERE wbs_id = %s AND budget_head_id = %s", (ids["wbs"], ids["head"]))
    pg_connection.commit()

    with pytest.raises(rules.ApprovalError) as excinfo:
        with pg_database.session(_scope()) as session:
            engine.decide(
                session, instance_id=opened["instance_id"], actor_user_id="U-A",
                action=rules.ACTION_APPROVE, idempotency_key="k1", object_version=1)
    assert excinfo.value.code == rules.ERR_BUDGET_MOVED
    assert excinfo.value.detail["available_now_paise"] < \
        excinfo.value.detail["available_at_routing_paise"]

    status = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (opened["instance_id"],)).fetchone()[0]
    assert status == rules.INST_OPEN, (
        "a refused decision must leave the instance untouched, not half-applied")


def test_unchanged_availability_lets_the_decision_through(
        pg_connection, pg_database, approval_schema):
    """The negative control for the test above.

    Without it, BUDGET_MOVED could be firing on everything and the test above
    would still pass -- which would block every approval in the product.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix, budget_paise=100_000_000)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER",
                    snapshot=_snapshot(ids, revision_id, available_paise=100_000_000))

    with pg_database.session(_scope()) as session:
        result = engine.decide(
            session, instance_id=opened["instance_id"], actor_user_id="U-A",
            action=rules.ACTION_APPROVE, idempotency_key="k1", object_version=1)
    assert result.instance_status == rules.INST_APPROVED


# ==========================================================================
# Lock order and the audit chain
# ==========================================================================
def test_a_decision_takes_the_cell_locks_first_and_in_path_order(
        pg_connection, pg_database, approval_schema):
    """Contract 7 step 1, observed rather than reviewed.

    ``Session.locks_taken`` records the cell locks in acquisition order, which
    is what makes "cells first, once, ordered" something a test can assert
    instead of something a reader has to believe.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")

    with pg_database.session(_scope()) as session:
        engine.decide(
            session, instance_id=opened["instance_id"], actor_user_id="U-A",
            action=rules.ACTION_APPROVE, idempotency_key="k1", object_version=1)
        taken = list(session.locks_taken)

    assert taken, "the decision must lock the affected cells, not zero of them"
    assert taken[0] == (ids["wbs"], ids["head"])
    assert taken == sorted(set(taken), key=taken.index), (
        "no cell is locked twice; lock_affected_cells is called exactly once")


def test_every_action_is_hash_chained_on_the_approval_stream(
        pg_connection, pg_database, approval_schema):
    """Contract 9: chained on ``approval:{instance_id}``, in the same
    transaction as the state change, using the frozen payload format."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"],
                                 "U-A": ["Finance"], "U-B": ["CFO"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]},
        {"stage_no": 2, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "CFO")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")

    for user_id, key in (("U-A", "k1"), ("U-B", "k2")):
        with pg_database.session(_scope()) as session:
            engine.decide(
                session, instance_id=opened["instance_id"], actor_user_id=user_id,
                action=rules.ACTION_APPROVE, idempotency_key=key, object_version=1)

    with pg_database.session(_scope()) as session:
        verdict = engine.verify_instance_chain(session, opened["instance_id"])
    assert verdict["intact"] is True, verdict
    assert verdict["sequence_contiguous"] is True
    assert verdict["entries_checked"] >= 3, (
        "the opening and both approvals must each be chained")
    assert verdict["stream_key"] == f"approval:{opened['instance_id']}"


# ==========================================================================
# No auto-approval, against a real database
# ==========================================================================
def test_a_stage_whose_only_approver_is_the_maker_goes_to_exception_pending(
        pg_connection, pg_database, approval_schema):
    """Section 9.3 stage 3, end to end. The engine does not skip the stage and
    does not approve it -- the instance is EXCEPTION_PENDING and visible.

    RETURNED, not raised, for the same reason as
    `test_an_unroutable_object_is_recorded_and_never_approved` below: the
    caller owns the transaction, so an exception leaving it rolled back the
    EXCEPTION_PENDING row the engine had just written, and the object ended
    with no approval instance at all. This test used to expect the raise and
    then assert the row survived it -- two claims that could not both hold, and
    only a live database could show which one gave way.

    The safety property is asserted more directly than the exception type ever
    asserted it: the returned status is EXCEPTION_PENDING, the row says so too,
    and neither is APPROVED.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor", "Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")

    instance = _open(pg_database, ids, revision_id, maker="U-MAKER")
    assert instance["status"] == rules.INST_EXCEPTION_PENDING, (
        "a stage whose only approver is the maker is held for an "
        "administrator, never approved")

    row = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE object_id = %s", (revision_id,)
    ).fetchone()
    assert row is not None, (
        "an unroutable object must leave a row behind, or nobody will ever see it")
    assert row[0] == rules.INST_EXCEPTION_PENDING
    assert row[0] != rules.INST_APPROVED


def test_an_unroutable_object_is_recorded_and_never_approved(
        pg_connection, pg_database, approval_schema):
    """No ACTIVE definition matches: APPROVAL_ROUTE_UNRESOLVED, fail closed."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"]})
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")

    # RETURNED, not raised -- and that distinction is the whole point.
    #
    # open_instance used to write the EXCEPTION_PENDING row and then raise. The
    # caller owns the transaction, so the exception rolled the row back: the
    # object ended with no approval instance at all, which is the single
    # outcome Contract 2 exists to prevent. The evidence was destroyed as a
    # direct consequence of reporting it, and only a live database could show
    # that -- in-memory, nothing rolls back.
    instance = _open(pg_database, ids, revision_id, maker="U-MAKER")
    assert instance["status"] == rules.INST_EXCEPTION_PENDING, (
        "an unroutable object is held for an administrator, never approved")

    row = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE object_id = %s", (revision_id,)
    ).fetchone()
    assert row is not None, (
        "an unroutable object must leave a row behind, or nobody will ever "
        "see it -- this is the assertion that caught the rollback")
    assert row[0] == rules.INST_EXCEPTION_PENDING


def test_a_skipped_stage_is_recorded_with_its_reason_not_omitted(
        pg_connection, pg_database, approval_schema):
    """Contract 2: an auditor must see what did not run, and why."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]},
        {"stage_no": 2, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "CFO")],
         "applies_when": {"op": ">=", "left": {"path": "delta_paise"},
                          "right": {"value": 50_000_000}}}])
    revision_id = _seed_revision(pg_connection, suffix, ids, created_by="U-MAKER")
    opened = _open(pg_database, ids, revision_id, maker="U-MAKER")

    row = pg_connection.execute(
        "SELECT status, skip_reason FROM approval_stage_instance "
        "WHERE instance_id = %s AND stage_no = 2", (opened["instance_id"],)).fetchone()
    assert row is not None, "a skipped stage is recorded, never omitted"
    assert row[0] == rules.STAGE_SKIPPED
    assert row[1] == rules.SKIP_RULE_NOT_MET

    # And the skip does not shortcut the instance to APPROVED on its own.
    assert pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (opened["instance_id"],)).fetchone()[0] == rules.INST_OPEN


# ==========================================================================
# Sustained randomised load
# ==========================================================================
@pytest.mark.slow
def test_no_deadlock_over_a_sustained_randomised_run(
        pg_connection, pg_database, approval_schema):
    """Contract 7's real claim: the ordered lock set is deadlock-free.

    Eight instances over four shared cells, decided by six threads in a
    randomised order, so transactions routinely want overlapping lock sets in
    different sequences -- the shape that deadlocks when an ordering is wrong.
    Every cell lock is taken through ``lock_affected_cells``, so all of them
    arrive in ``(wbs_path, budget_head_id)`` order and the wait-for graph stays
    acyclic.

    A deadlock is a hard failure, reported with the transaction that lost.
    Contention (``STAGE_NOT_OPEN``, ``NOT_AN_ASSIGNEE``) is the expected,
    correct outcome of losing a race and is not counted as an error.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix, budget_paise=1_000_000_000)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"],
                                 "U-A": ["Finance"], "U-B": ["Finance"],
                                 "U-C": ["CFO"]})
    _seed_definition(pg_connection, suffix, entity=ids["entity"], stages=[
        {"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "Finance")]},
        {"stage_no": 2, "quorum_type": rules.QUORUM_ANY,
         "approvers": [(rules.APPROVER_ROLE, "CFO")]}])

    instances = []
    for n in range(8):
        revision_id = _seed_revision(pg_connection, f"{suffix}_{n}", ids,
                                      created_by="U-MAKER")
        instances.append(_open(pg_database, ids, revision_id, maker="U-MAKER")
                          ["instance_id"])

    deadlocks: list[BaseException] = []
    unexpected: list[BaseException] = []
    applied = 0
    lock = threading.Lock()

    def worker(seed):
        nonlocal applied
        rng = random.Random(seed)
        for _ in range(12):
            instance_id = rng.choice(instances)
            user_id = rng.choice(["U-A", "U-B", "U-C"])
            try:
                with pg_database.session(_scope()) as session:
                    engine.decide(
                        session, instance_id=instance_id, actor_user_id=user_id,
                        action=rules.ACTION_APPROVE,
                        idempotency_key=f"{seed}-{user_id}-{instance_id}",
                        object_version=1)
                with lock:
                    applied += 1
            except psycopg.errors.DeadlockDetected as exc:
                with lock:
                    deadlocks.append(exc)
            except rules.ApprovalError:
                pass                    # losing a race is the correct outcome
            except BaseException as exc:                # noqa: BLE001
                with lock:
                    unexpected.append(exc)

    _run_concurrently([lambda s=seed: worker(s) for seed in range(6)])

    assert not deadlocks, f"{len(deadlocks)} deadlock(s) detected: {deadlocks[:3]}"
    assert not unexpected, f"unexpected failures: {unexpected[:3]}"
    assert applied > 0, (
        "no decision was applied at all: the run proved nothing about deadlocks")

    statuses = dict(pg_connection.execute(
        "SELECT status, count(*) FROM approval_instance WHERE instance_id = ANY(%s) "
        "GROUP BY status", (instances,)).fetchall())
    assert sum(statuses.values()) == len(instances)
    assert set(statuses) <= {rules.INST_OPEN, rules.INST_APPROVED}, (
        f"no instance may end in an unexpected state: {statuses}")
