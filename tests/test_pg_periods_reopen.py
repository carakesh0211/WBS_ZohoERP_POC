"""AUD-C-008 residual: reopening a CLOSED period, gated on an APPROVED approval.

WHAT WAS WRONG, AND WHY "NOTHING" WAS ALSO WRONG
================================================

`pg/periods.py`'s `_ALLOWED_TRANSITIONS` made CLOSED terminal, with the comment
"Forward-only. There is no reopen path in this milestone." A control that does
not exist cannot be defeated, so as a position that was defensible. It is not
survivable in practice: a period closed a day early has to be reopened by
somebody, and the shape that arrives when the product has no path for it is a
hand-run `UPDATE accounting_period SET state = 'OPEN'` -- which no approval
gates, no scope filters and no audit chain records.

So migration 023 builds the path, and everything below is about the ways it
must refuse.

THE CHEAPEST WRONG IMPLEMENTATION, NAMED
========================================

Adding `"CLOSED": {"OPEN"}` to `_ALLOWED_TRANSITIONS` would have been the whole
feature in one line, and it would have shipped a reopen gated by the
`period.transition` permission and nothing else -- exactly as easy as
soft-closing an open period. AUD-C-008 asks for an APPROVED APPROVAL INSTANCE,
and an approval cannot be expressed as an entry in a state table.
`test_transition_period_still_cannot_reopen_a_closed_period` fails the moment
somebody takes that shortcut.

WHICH TESTS HAVE EXECUTED
=========================

The source-level tests below run everywhere and have executed. EVERY
`@pytest.mark.pg` TEST IN THIS FILE HAS NEVER EXECUTED ON THE MACHINE IT WAS
WRITTEN ON -- there is no PostgreSQL here -- and first runs in CI's `pg_tests`
job. That is the whole of the concurrency and idempotency evidence, and a skip
is not a pass.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from app.backend import auth  # noqa: E402
from app.backend.pg import audit as audit_mod  # noqa: E402
from app.backend.pg import periods  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- two concurrent reopens "
            "racing for one row is a question only a real server answers, and "
            "an in-process double is the instrument that would miss it."),
)

CLOSER = "U-CLOSER"
APPROVER = "U-APPROVER"
APPLIER = "U-APPLIER"


# ===========================================================================
# Source-level. These run everywhere.
# ===========================================================================
def test_transition_period_still_cannot_reopen_a_closed_period():
    """THE SHORTCUT, REFUSED. `_ALLOWED_TRANSITIONS` must keep CLOSED
    terminal: widening it would put the reopen behind a permission two roles
    hold and nothing else."""
    assert periods._ALLOWED_TRANSITIONS["CLOSED"] == set(), (
        "CLOSED gained a legal transition. If that is CLOSED -> OPEN, the "
        "reopen is now gated by the period.transition permission alone and "
        "AUD-C-008's approval requirement has been bypassed, not implemented.")


def test_the_reopen_path_reuses_the_products_own_maker_checker():
    """`auth.require_separation` is CALLED, not reimplemented.

    A second maker-checker implementation is how two rules that must agree
    drift apart. This asserts the reuse is real rather than described.
    """
    source = inspect.getsource(periods._refuse_self_approval)
    assert "auth_mod.require_separation(" in source
    assert "require_maker=True" in source, (
        "a falsy maker is PERMISSIVE by default in require_separation, which "
        "is how bill.void shipped a control that never ran")


def test_the_separation_refusal_does_not_depend_on_require_separation_firing(
        monkeypatch):
    """THE LESSON FROM `bill.void`, APPLIED BEFORE IT COSTS ANYTHING.

    `require_separation`'s first line is `if permission not in MAKER_CHECKER:
    return`. "period.reopen" is not in that set today (see the test below), so
    that call is INERT -- it returns having compared nobody. If it were the
    only limb, this control would never have run, and a test asserting "the
    happy path did not raise" would have passed for the wrong reason.

    Here `require_separation` is replaced with a no-op outright. The refusal
    must still happen.
    """
    monkeypatch.setattr(auth, "require_separation",
                        lambda *a, **k: None)
    monkeypatch.setattr(periods.auth_mod, "require_separation",
                        lambda *a, **k: None)

    with pytest.raises(periods.PeriodServiceError) as excinfo:
        periods._refuse_self_approval(CLOSER, CLOSER, what="apply",
                                      period_id="PER-1")
    assert excinfo.value.code == "SELF_APPROVAL"
    assert excinfo.value.status == 403


def test_an_independent_applier_is_not_refused(monkeypatch):
    """A control that refuses everybody is an outage, and it would pass the
    test above."""
    monkeypatch.setattr(periods.auth_mod, "require_separation",
                        lambda *a, **k: None)
    periods._refuse_self_approval(APPLIER, CLOSER, what="apply",
                                  period_id="PER-1")


def test_the_permission_registration_this_stream_could_not_make():
    """`period.reopen` is not registered in `app/backend/auth.py`, which this
    stream does not own.

    STATED AS A TEST rather than only in a report, so the gap is visible on
    every run and closes itself the day the permission lands: once
    `period.reopen` is in `MAKER_CHECKER`, the assertion flips to checking that
    `require_separation` alone refuses.
    """
    if periods.REOPEN_PERMISSION in auth.MAKER_CHECKER:
        with pytest.raises(auth.AuthError) as excinfo:
            auth.require_separation({"user_id": CLOSER},
                                    periods.REOPEN_PERMISSION, CLOSER,
                                    require_maker=True)
        assert excinfo.value.code == "SELF_APPROVAL"
        return

    # The state as shipped. require_separation returns without comparing.
    auth.require_separation({"user_id": CLOSER}, periods.REOPEN_PERMISSION,
                            CLOSER, require_maker=True)
    assert periods.REOPEN_PERMISSION not in auth.PERMISSIONS, (
        "period.reopen is a known permission but not a maker-checker one; "
        "add it to auth.MAKER_CHECKER as well, or the reopen's identity-layer "
        "separation check stays inert.")


def test_the_reopen_binds_the_approval_to_this_very_period():
    """An APPROVED instance for SOME object is not authority over THIS one.

    Both `object_type` and `object_id` are checked, which is the difference
    between "gated on an approval" and "gated on the existence of an approval
    somewhere in the database".
    """
    source = inspect.getsource(periods._approval_instance_for)
    assert "object_type" in source and "object_id" in source
    assert "APPROVAL_INSTANCE_MISBOUND" in source


def test_the_approver_set_reads_the_delegation_column_too():
    """A delegated approval is the DELEGATOR's. Reading only `actor_user_id`
    would let the closer approve their own reopening through a delegate."""
    source = inspect.getsource(periods._approvers_of)
    assert "acting_for_user_id" in source


def test_the_audit_action_names_are_stable_and_the_payload_is_untouched():
    """Contract 9's payload `prev|at|actor|action|type|id|detail` is FROZEN
    permanently: changing it invalidates every hash already stored. The reopen
    path appends through `audit.append` and adds no argument to it."""
    assert audit_mod._payload("p", "a", "ac", "AC", "T", "I", "d") == \
        "p|a|ac|AC|T|I|d"
    source = inspect.getsource(periods.apply_period_reopen)
    assert 'audit_mod.append(' in source
    assert '"PERIOD_REOPEN"' in source
    assert '"ACCOUNTING_PERIOD"' in source, (
        "close and reopen must share one stream key, or they cannot be read "
        "as one interleaved, verifiable sequence")


# ===========================================================================
# Live PostgreSQL. NONE OF THESE HAS EVER EXECUTED ON THIS MACHINE.
# ===========================================================================
def _seed(connection, *, suffix: str, closed_by: str = CLOSER) -> dict[str, str]:
    """One entity, one CLOSED period, one project, and an approval instance.

    Every column is derived from the migration that creates it.
    `approval_instance` (008:380) needs `object_version >= 1`, a non-blank
    `object_content_sha`, a `jsonb` snapshot, a `maker_user_id` that references
    `app_user`, an `entity_id`, and -- through
    `ck_approval_instance_definition` -- either BOTH a `definition_id` and a
    `definition_version` or neither plus `status = 'EXCEPTION_PENDING'`. So a
    definition is seeded too, and `ck_approval_definition_activation` requires
    an ACTIVE one to carry `activated_at` and `activated_by`.
    """
    ids = {
        "org": f"O_{suffix}", "entity": f"E_{suffix}", "project": f"PRJ_{suffix}",
        "period": f"PER_{suffix}", "definition": f"AD_{suffix}",
        "instance": f"AI_{suffix}", "other_instance": f"AX_{suffix}",
    }
    ex = connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    for user_id in (CLOSER, APPROVER, APPLIER):
        ex("INSERT INTO app_user (user_id, email, display_name, created_by, "
           "updated_by) VALUES (%s,%s,%s,'t','t') ON CONFLICT DO NOTHING",
           (user_id, f"{user_id.lower()}@example.test", user_id))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Project','Released','t','t')",
       (ids["project"], ids["entity"], f"C_{suffix}"))
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, "
       "period_end, state, closed_at, closed_by, created_by) "
       "VALUES (%s,%s,'2026-01-01','2026-03-31','CLOSED',now(),%s,'t')",
       (ids["period"], ids["entity"], closed_by))
    ex("INSERT INTO approval_definition (definition_id, object_type, code, "
       "version, status, entity_id, effective_from, created_by, activated_at, "
       "activated_by) VALUES (%s,'ACCOUNTING_PERIOD',%s,1,'ACTIVE',%s,"
       "'2026-01-01','t',now(),'t')",
       (ids["definition"], f"PERIOD_REOPEN_{suffix}", ids["entity"]))
    for instance_id, object_id in ((ids["instance"], ids["period"]),
                                   (ids["other_instance"], f"OTHER_{suffix}")):
        ex("INSERT INTO approval_instance (instance_id, object_type, "
           "object_id, object_version, object_content_sha, definition_id, "
           "definition_version, status, snapshot, maker_user_id, entity_id) "
           "VALUES (%s,'ACCOUNTING_PERIOD',%s,1,%s,%s,1,'OPEN',%s::jsonb,%s,%s)",
           (instance_id, object_id, f"sha_{suffix}", ids["definition"],
            json.dumps({"period_id": object_id}), CLOSER, ids["entity"]))
    connection.commit()
    return ids


def _approve(connection, instance_id: str, *, by: str = APPROVER,
             seq: int = 1) -> None:
    """Walk the instance to APPROVED the way the engine would leave it.

    `approval_action` (008:583) needs `instance_id`, `actor_user_id` (an
    `app_user`), an `action` and a `seq >= 1`; `uq_approval_action_seq` is
    UNIQUE on (instance_id, seq). `ck_approval_instance_open_is_not_closed`
    requires `closed_at` to be NULL only while OPEN, so a settled instance may
    carry one.
    """
    connection.execute(
        "INSERT INTO approval_action (instance_id, actor_user_id, action, seq) "
        "VALUES (%s,%s,'APPROVE',%s)", (instance_id, by, seq))
    connection.execute(
        "UPDATE approval_instance SET status = 'APPROVED', closed_at = now() "
        "WHERE instance_id = %s", (instance_id,))
    connection.commit()


def _state(connection, period_id: str) -> str:
    return connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s",
        (period_id,)).fetchone()[0]


def _request(database, ids, *, actor: str = APPLIER,
             instance_key: str = "instance", key: str | None = None) -> str:
    with database.session(Scope.system()) as session:
        return periods.request_period_reopen(
            session, period_id=ids["period"],
            approval_instance_id=ids[instance_key],
            reason="closed a day early; two receipts still to post",
            actor=actor,
            idempotency_key=key or f"idem-{uuid.uuid4().hex}")["reopen_id"]


@pytest.mark.pg
@PG
def test_a_closed_period_reopens_only_through_the_approved_instance(
        pg_database, pg_connection):
    """THE HAPPY PATH, WHICH IS ALSO THE PROOF THE GATE IS NOT AN OUTAGE."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"])

    with pg_database.session(Scope.system()) as session:
        result = periods.apply_period_reopen(
            session, reopen_id=reopen_id, actor=APPLIER)

    assert result["state"] == "OPEN"
    assert result["approved_by"] == APPROVER
    assert result["applied_by"] == APPLIER
    assert _state(pg_connection, ids["period"]) == "OPEN"

    row = pg_connection.execute(
        "SELECT closed_by, reopened_by, reopen_count FROM accounting_period "
        "WHERE period_id = %s", (ids["period"],)).fetchone()
    assert row == (CLOSER, APPLIER, 1), (
        "the close that was reversed must stay on the row; erasing closed_by "
        "would destroy the identity every future separation check reads")


@pytest.mark.pg
@PG
def test_an_unapproved_instance_reopens_nothing(pg_database, pg_connection):
    """THE CONTROL ITSELF. The instance exists, is bound to this period, and is
    still OPEN. A flag would have said "requested"; an approval says nothing of
    the kind."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.apply_period_reopen(session, reopen_id=reopen_id,
                                        actor=APPLIER)
    assert excinfo.value.code == "REOPEN_NOT_APPROVED"
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_an_approval_for_another_object_is_refused(pg_database, pg_connection):
    """AN APPROVED INSTANCE FOR SOMETHING ELSE IS NOT AUTHORITY OVER THIS.

    Without the `object_id` check, any APPROVED instance in the entity would
    open any closed period in it.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _approve(pg_connection, ids["other_instance"])

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.request_period_reopen(
                session, period_id=ids["period"],
                approval_instance_id=ids["other_instance"],
                reason="wrong instance", actor=APPLIER,
                idempotency_key=f"idem-{suffix}")
    assert excinfo.value.code == "APPROVAL_INSTANCE_MISBOUND"
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_the_closer_may_not_approve_the_reopening(pg_database, pg_connection):
    """SEPARATION OF DUTIES, THE REQUIREMENT VERBATIM.

    The instance really is APPROVED; the approver really is the person who
    closed the period. That is the one combination the control exists for, and
    it is caught by comparing the FROZEN `closed_by_at_request` -- not
    `accounting_period.closed_by`, which the next close overwrites.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"], by=CLOSER)

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.apply_period_reopen(session, reopen_id=reopen_id,
                                        actor=APPLIER)
    assert excinfo.value.code == "SELF_APPROVAL"
    assert excinfo.value.status == 403
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_the_closer_may_not_apply_the_reopening_either(pg_database,
                                                       pg_connection):
    """An independent approver plus the closer pressing the button is still the
    closer reversing their own close."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"])

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.apply_period_reopen(session, reopen_id=reopen_id,
                                        actor=CLOSER)
    assert excinfo.value.code == "SELF_APPROVAL"
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_the_database_refuses_the_same_pairing_a_service_bug_would_admit(
        pg_database, pg_connection):
    """`ck_period_reopen_separation` IS NOT REDUNDANT WITH THE SERVICE CHECK.

    RLS does not apply to a superuser at all, and this connection IS one. A
    maintenance script, a future caller or a policy edit could write the
    forbidden pairing directly; the CHECK is what stops it.
    """
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "UPDATE period_reopen_request SET approved_by = %s "
            "WHERE reopen_id = %s", (CLOSER, reopen_id))
    pg_connection.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "UPDATE period_reopen_request SET applied_by = %s "
            "WHERE reopen_id = %s", (CLOSER, reopen_id))
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_the_approval_binding_cannot_be_repointed_after_the_request(
        pg_database, pg_connection):
    """An approval-gated action whose approval can be swapped afterwards is not
    gated. `trg_period_reopen_request_append_only` refuses the swap."""
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "UPDATE period_reopen_request SET approval_instance_id = %s "
            "WHERE reopen_id = %s", (ids["other_instance"], reopen_id))
    pg_connection.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "UPDATE period_reopen_request SET closed_by_at_request = %s "
            "WHERE reopen_id = %s", ("U-SOMEBODY-ELSE", reopen_id))
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_a_request_can_never_be_deleted(pg_database, pg_connection):
    """A reopening that was asked for and did not happen is part of the trail.
    Its absence would be indistinguishable from its never being asked."""
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "DELETE FROM period_reopen_request WHERE reopen_id = %s",
            (reopen_id,))
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_replaying_the_apply_produces_no_second_effect(pg_database,
                                                       pg_connection):
    """IDEMPOTENCY, MEASURED AND NOT ASSERTED IN PROSE.

    `reopen_count` and the number of PERIOD_REOPEN audit entries are both
    counters, so a second effect is visible as a number rather than inferred.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"])

    with pg_database.session(Scope.system()) as session:
        first = periods.apply_period_reopen(session, reopen_id=reopen_id,
                                            actor=APPLIER)
    with pg_database.session(Scope.system()) as session:
        replay = periods.apply_period_reopen(session, reopen_id=reopen_id,
                                             actor=APPLIER)

    assert first["replayed"] is False and replay["replayed"] is True
    assert replay["status"] == "APPLIED"
    assert replay["approved_by"] == first["approved_by"]

    count = pg_connection.execute(
        "SELECT reopen_count FROM accounting_period WHERE period_id = %s",
        (ids["period"],)).fetchone()[0]
    assert count == 1, "the replay reopened the period a second time"

    entries = pg_connection.execute(
        "SELECT count(*) FROM audit_log WHERE stream_key = %s AND action = %s",
        (f"ACCOUNTING_PERIOD:{ids['period']}", "PERIOD_REOPEN")).fetchone()[0]
    assert entries == 1, "the replay appended a second PERIOD_REOPEN entry"


@pytest.mark.pg
@PG
def test_replaying_the_request_mints_no_second_row(pg_database, pg_connection):
    """`ux_period_reopen_idempotency`. A retried POST is not a second request."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    key = f"idem-{suffix}"
    first = _request(pg_database, ids, key=key)
    second = _request(pg_database, ids, key=key)
    assert first == second
    rows = pg_connection.execute(
        "SELECT count(*) FROM period_reopen_request WHERE period_id = %s",
        (ids["period"],)).fetchone()[0]
    assert rows == 1


@pytest.mark.pg
@PG
def test_two_concurrent_applies_do_not_both_succeed(pg_database, pg_connection):
    """CONCURRENCY, PROVED WITH TWO REAL BACKENDS.

    `pg_database` is a real pool, so the threads below get separate backends
    and a genuine row lock rather than two turns of one connection. One apply
    wins; the other wakes holding the lock, re-reads `state` as OPEN and
    refuses. What must NOT happen is both succeeding, which would show up as
    `reopen_count = 2` and two PERIOD_REOPEN entries on the chain.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"])

    outcomes: list[object] = []
    barrier = threading.Barrier(2, timeout=30)

    def attempt() -> None:
        try:
            barrier.wait()
            with pg_database.session(Scope.system()) as session:
                outcomes.append(periods.apply_period_reopen(
                    session, reopen_id=reopen_id, actor=APPLIER))
        except Exception as exc:                       # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    applied = [o for o in outcomes if isinstance(o, dict)
               and o.get("replayed") is False]
    assert len(applied) == 1, (
        f"exactly one of two concurrent applies must take effect; got "
        f"{outcomes!r}")

    count = pg_connection.execute(
        "SELECT reopen_count FROM accounting_period WHERE period_id = %s",
        (ids["period"],)).fetchone()[0]
    assert count == 1
    entries = pg_connection.execute(
        "SELECT count(*) FROM audit_log WHERE stream_key = %s AND action = %s",
        (f"ACCOUNTING_PERIOD:{ids['period']}", "PERIOD_REOPEN")).fetchone()[0]
    assert entries == 1


@pytest.mark.pg
@PG
def test_two_outstanding_requests_for_one_period_are_refused(pg_database,
                                                             pg_connection):
    """`ux_period_reopen_one_open_per_period`, a PARTIAL unique index. A period
    may be reopened more than once over its life; what is refused is two OPEN
    requests racing to apply."""
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _request(pg_database, ids, key=f"idem-a-{suffix}")

    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg_database.session(Scope.system()) as session:
            periods.request_period_reopen(
                session, period_id=ids["period"],
                approval_instance_id=ids["instance"],
                reason="a second, competing request", actor=APPLIER,
                idempotency_key=f"idem-b-{suffix}")


@pytest.mark.pg
@PG
def test_the_close_and_the_reopen_share_one_intact_hash_chain(pg_database,
                                                              pg_connection):
    """THE IMMUTABLE TRAIL, VERIFIED RATHER THAN DESCRIBED.

    The reopen appends to the SAME `ACCOUNTING_PERIOD:<period_id>` stream the
    request went onto, so the request and the reopening interleave in one
    sequence -- and `audit.verify_chain` recomputes every hash in it. The
    payload format `prev|at|actor|action|type|id|detail` is frozen permanently
    and nothing here changes it.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)
    _approve(pg_connection, ids["instance"])
    with pg_database.session(Scope.system()) as session:
        periods.apply_period_reopen(session, reopen_id=reopen_id, actor=APPLIER)

    with pg_database.session(Scope.system()) as session:
        verdict = audit_mod.verify_chain(
            session, f"ACCOUNTING_PERIOD:{ids['period']}")

    assert verdict["intact"] is True
    assert verdict["entries_checked"] >= 2, (
        "the request and the reopening must both be on the chain")
    actions = pg_connection.execute(
        "SELECT action FROM audit_log WHERE stream_key = %s ORDER BY seq",
        (f"ACCOUNTING_PERIOD:{ids['period']}",)).fetchall()
    assert [a for (a,) in actions] == ["PERIOD_REOPEN_REQUESTED",
                                       "PERIOD_REOPEN"]


@pytest.mark.pg
@PG
def test_a_period_that_records_no_closer_refuses_rather_than_waives(
        pg_database, pg_connection):
    """`accounting_period.closed_by` is NULLable (001). "We cannot tell who
    closed it" must never become "anyone may approve reopening it" -- the same
    rule `_has_open_reconciliation_exceptions` follows for its own gate."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    pg_connection.execute(
        "UPDATE accounting_period SET closed_by = NULL WHERE period_id = %s",
        (ids["period"],))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.request_period_reopen(
                session, period_id=ids["period"],
                approval_instance_id=ids["instance"],
                reason="who closed this?", actor=APPLIER,
                idempotency_key=f"idem-{suffix}")
    assert excinfo.value.code == "PERIOD_CLOSER_UNKNOWN"


@pytest.mark.pg
@PG
def test_a_refused_request_is_kept_and_blocks_nothing_further(pg_database,
                                                              pg_connection):
    """A refusal settles the request without reopening anything, and leaves the
    row behind as the record that a reopening was asked for and declined."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    reopen_id = _request(pg_database, ids)

    with pg_database.session(Scope.system()) as session:
        result = periods.refuse_period_reopen(
            session, reopen_id=reopen_id, actor=APPROVER,
            refusal_code="NOT_MATERIAL", detail="the two receipts are Rs 400")

    assert result["status"] == "REFUSED"
    assert _state(pg_connection, ids["period"]) == "CLOSED"

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.apply_period_reopen(session, reopen_id=reopen_id,
                                        actor=APPLIER)
    assert excinfo.value.code == "REOPEN_REQUEST_REFUSED"

    # ...and a fresh request is now possible, which is what makes the partial
    # index a concurrency guard rather than a one-reopen-ever rule.
    _approve(pg_connection, ids["instance"])
    second = _request(pg_database, ids, key=f"idem-second-{suffix}")
    with pg_database.session(Scope.system()) as session:
        periods.apply_period_reopen(session, reopen_id=second, actor=APPLIER)
    assert _state(pg_connection, ids["period"]) == "OPEN"
