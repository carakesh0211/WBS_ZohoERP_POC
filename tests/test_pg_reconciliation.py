"""§11.8's reconciliation exceptions, executed against a real PostgreSQL server.

WHY THIS FILE EXISTS
====================

``SweepStore``'s reconciliation half had **no PostgreSQL implementation at
all**. All eight sweeps ran exclusively against
``tests/integration_fakes.py::InMemoryStore``, and a fake written from the
same reading as the code agrees with the code about everything -- including
statements the server cannot parse. That is not a hypothetical here: it is
exactly how three separate rate-budget implementations shipped in Wave 5 with
every unit test passing and two of them unable to execute
(``tests/test_pg_integration_rate_budget.py`` is the file written after that).

Everything below therefore asserts something a double structurally cannot:

  1. **The partial unique index is the idempotency.** ``raise_exception``
     infers ``ux_reconciliation_exception_open`` by naming both its columns and
     its predicate. A dictionary keyed on a tuple agrees with any story about
     what that index does, including a wrong one.
  2. **The resolution biconditional is enforced by the server.**
     ``ck_reconciliation_exception_resolution`` refuses a non-Open row with no
     ``resolved_by``. Proved by trying it, raw, and catching the violation.
  3. **The kind CHECK closes the set at five.** A sixth is refused by the
     database, not by a Python assert that a future caller can route around.
  4. **``SUM()`` over ``bigint`` returns numeric.** ``open_exception_exposure``
     casts ``::bigint`` because psycopg maps numeric to ``Decimal``, and a
     Decimal reaching integer arithmetic is what took down the availability
     verdict, both approval paths and the concurrency proof -- in the
     PostgreSQL CI job only. **No in-memory double can ever produce a
     Decimal**, so this property is untestable anywhere but here.
  5. **Row-level security is real.** The fixtures connect as ``capex_app`` via
     ``pg_app_database``, because CI's superuser bypasses RLS unconditionally
     and a test asserting "the other entity's rows were hidden" through the
     superuser asserts nothing.

THE TWO SILENT DROPS, ASSERTED DIRECTLY
=======================================
Both were found in this surface, and both arrived through the code written to
prevent silent drops:

  * ``test_a_rewalk_does_not_inflate_anything`` -- the unattributed bucket was
    once INCREMENTED, so every re-walk grew it without a receive arriving.
  * ``test_two_identifierless_lines_on_one_receive_are_two_exceptions`` -- two
    lines with no identifier keyed the SAME exception, and the second line's
    value vanished.

A SKIP IS NOT A PASS
====================
There is no PostgreSQL and no Docker on the machine this was written on, so
**every test below skipped here and none has run locally.** CI's ``pg_tests``
job supplies a ``postgres:16`` service container and sets ``CAPEX_DB_URL``;
that job is the only oracle for this file. Until it is green, the honest
summary of this work is "the SQL names real columns and real constraints, and
has not been executed".
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly. Without this
# the tests fail at SETUP with "fixture not found" -- but ONLY where
# CAPEX_DB_URL is set. Locally they skip, so the missing fixture is never
# resolved and the gap stays invisible until CI, which is that job's point.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_app_database, pg_connection, pg_database,
    pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
import re  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import periods as periods_mod  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

ENTITY = "ENT-RECON"
OTHER_ENTITY = "ENT-OTHER"
PROJECT = "PRJ-RECON"
OTHER_PROJECT = "PRJ-OTHER"
#: Mid-day and mid-minute, so an off-by-one in a timestamp cannot pass by
#: landing on the same boundary either way.
T0 = datetime(2026, 9, 7, 11, 30, 15, tzinfo=timezone.utc)


# ==================================================================== fixtures
def _seed(con: psycopg.Connection) -> None:
    """Two entities and two projects, committed.

    Raw SQL rather than the store's own writers: the point of this file is
    what the SERVER does, and routing the fixture through the module under
    test would let a Python-side guard stand in for a schema constraint.

    TWO of each, deliberately. A single entity makes every scope assertion
    vacuous -- a query that ignores its scope predicate entirely still returns
    the right rows when there is only one entity's worth of them, which is how
    a scope test passes while testing nothing.
    """
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES ('ORG-R', 'ORGR', 'Recon Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    for entity in (ENTITY, OTHER_ENTITY):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, 'ORG-R', %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (entity, entity, f"Entity {entity}"))
    for project, entity in ((PROJECT, ENTITY), (OTHER_PROJECT, OTHER_ENTITY)):
        con.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (project, entity, project, f"Project {project}"))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-SWEEP', 's@example.test',"
        " 'Sweep service', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    con.commit()


@pytest.fixture()
def seeded(pg_connection):
    _seed(pg_connection)
    return pg_connection


def _session(con: psycopg.Connection, *, entities=(ENTITY,),
             projects=None) -> Session:
    """A RESTRICTED session, never ``Scope.system()``.

    An unrestricted scope compiles the predicate to the literal ``TRUE``, so
    every scoped statement in this file would pass whether or not its scope
    join is correct. Restricting to the seeded entity is what makes the
    predicate do work.
    """
    return Session(
        connection=con,
        scope=Scope(user_id="U-RECON",
                    entity_ids=None if entities is None else frozenset(entities),
                    project_ids=None if projects is None else frozenset(projects)))


def _raise(session, *, kind=store.EXCEPTION_KINDS[0], object_type="grn_line",
           object_id="GRN-1:#0", detail="unattributed", entity_id=ENTITY,
           project_id=PROJECT, source_paise=None, local_paise=None,
           correlation_id="CORR-1", raised_at=T0, actor="SVC-SWEEP") -> str:
    return store.raise_exception(
        session, kind=kind, object_type=object_type, object_id=object_id,
        detail=detail, raised_at=raised_at, entity_id=entity_id,
        project_id=project_id, source_paise=source_paise,
        local_paise=local_paise, correlation_id=correlation_id, actor=actor)


def _count(con: psycopg.Connection, **where) -> int:
    clause = " AND ".join(f"{k} = %({k})s" for k in where) or "TRUE"
    return con.execute(
        f"SELECT count(*) FROM reconciliation_exception WHERE {clause}",  # noqa: S608
        where).fetchone()[0]


# ======================================================= the write path exists
@PG
def test_an_exception_is_written_with_every_column_the_sweep_supplied(seeded):
    """The baseline: this statement parses, binds and lands a row.

    It had never been executed. `test_integration_sql_matches_schema.py` says
    itself that it is not a SQL parser and cannot check that an `ON CONFLICT`
    target matches a real unique index -- and this statement's whole
    correctness rests on that target.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00, local_paise=100_00,
                          detail="Receive GRN-1 line #0 does not resolve.")
    seeded.commit()

    row = seeded.execute(
        "SELECT kind, object_type, object_id, entity_id, project_id, status,"
        " detail, local_paise, source_paise, correlation_id, raised_at,"
        " resolved_at, resolved_by, resolution_note"
        " FROM reconciliation_exception WHERE exception_id = %s",
        (exception_id,)).fetchone()
    assert row is not None, "the INSERT reported an id for a row that is not there"
    (kind, object_type, object_id, entity_id, project_id, status, detail,
     local_paise, source_paise, correlation_id, raised_at, resolved_at,
     resolved_by, resolution_note) = row
    assert kind == "GRN_LINE_UNATTRIBUTED"
    assert (object_type, object_id) == ("grn_line", "GRN-1:#0")
    assert (entity_id, project_id) == (ENTITY, PROJECT)
    assert status == store.EXCEPTION_OPEN
    assert "does not resolve" in detail
    assert (local_paise, source_paise) == (100_00, 125_00)
    assert correlation_id == "CORR-1"
    assert raised_at == T0
    # An Open row carries NO resolution. The other half of the biconditional.
    assert (resolved_at, resolved_by, resolution_note) == (None, None, None)


@PG
def test_the_money_columns_come_back_as_int_not_decimal(seeded):
    """`bigint` in, `int` out -- asserted, not assumed.

    Cheap here and impossible in a double: an in-memory store returns whatever
    Python object it was handed, so it can never disagree about a driver's
    type mapping.
    """
    session = _session(seeded)
    _raise(session, source_paise=125_00, local_paise=100_00)
    seeded.commit()
    row = store.open_exceptions(session, entity_id=ENTITY)[0]
    assert isinstance(row["source_paise"], int)
    assert not isinstance(row["source_paise"], bool)
    assert isinstance(row["local_paise"], int)


# ===================================================== idempotent re-walks (1)
@PG
def test_raising_the_same_exception_twice_while_open_is_a_no_op(seeded):
    """The sweeps re-walk. Twice must not mean twice.

    `SweepPurchaseOrderAnchored` resumes from a checkpoint on a 300-second
    overlap and a cycling cursor, so it re-reads purchase orders it has
    already walked -- that is the design, not a fault. Every re-read raises
    the same exception again, and `ux_reconciliation_exception_open` is what
    makes that free.
    """
    session = _session(seeded)
    first = _raise(session, source_paise=125_00)
    second = _raise(session, source_paise=125_00)
    third = _raise(session, source_paise=125_00, detail="a different message")
    seeded.commit()

    assert first == second == third, (
        "a re-walk must get the SAME exception id back: sweeps._attribute "
        "passes it straight to accumulate_unattributed as the bucket's "
        "source_key, and a new id every pass is a new bucket entry every pass")
    assert _count(seeded, object_id="GRN-1:#0") == 1


@PG
def test_a_rewalk_does_not_inflate_anything(seeded):
    """Twenty passes over the same receive line leave one row and one value.

    THE DEFECT THIS HOLDS SHUT: the unattributed bucket was once incremented
    rather than set, so a bucket keyed by nothing climbed on every pass and
    the number that blocks capitalisation became fiction. The exception table
    is the same shape of risk -- a re-raise that appended would make the open
    exception COUNT climb, and that count is what `period_close_blockers`
    reports to finance.
    """
    session = _session(seeded)
    ids = {_raise(session, source_paise=125_00) for _ in range(20)}
    seeded.commit()

    assert len(ids) == 1
    assert _count(seeded, object_id="GRN-1:#0") == 1
    exposure = store.open_exception_exposure(session, project_id=PROJECT)
    assert exposure["open_count"] == 1
    assert exposure["source_paise"] == 125_00, (
        "twenty re-walks of one 125.00 line must total 125.00, not 2500.00")


# ============================================ two identifierless lines (2)
@PG
def test_two_identifierless_lines_on_one_receive_are_two_exceptions(seeded):
    """The second silent drop, asserted at the storage layer.

    `sweeps._attribute` falls back to the line's ORDINAL when the source gives
    it no identifier -- ``#0``, ``#1`` -- because on ERP that is the expected
    case, not the exotic one. Keying both on the same string would collapse
    them into one exception and the second line's value would vanish: a silent
    drop, arriving through the very code that exists to prevent one.

    The index keys on `object_id`, so two ordinals are two rows. This test is
    what stops a future "tidy-up" from keying on the receive id alone.
    """
    session = _session(seeded)
    first = _raise(session, object_id="GRN-9:#0", source_paise=300_00,
                   detail="Receive GRN-9 line (no line identifier) [#0]")
    second = _raise(session, object_id="GRN-9:#1", source_paise=700_00,
                    detail="Receive GRN-9 line (no line identifier) [#1]")
    seeded.commit()

    assert first != second, (
        "two identifierless lines of ONE receive collapsed into one exception; "
        "the second line's value has just disappeared")
    assert _count(seeded, object_type="grn_line") == 2
    exposure = store.open_exception_exposure(session, project_id=PROJECT)
    assert exposure["open_count"] == 2
    assert exposure["source_paise"] == 1000_00, (
        "both lines must be held at FULL value -- 300.00 + 700.00. A total of "
        "700.00 means the first was overwritten; 300.00 means the second was "
        "dropped; anything else means they were spread")


@PG
def test_an_exception_with_no_object_id_is_never_deduplicated(seeded):
    """`ux_...` is partial on ``object_id IS NOT NULL``, and that is deliberate.

    An exception with nothing to key on cannot be recognised as "the same one
    again". Inventing a key for it is precisely how the two identifierless
    lines above collapsed, so the schema declines to, and every such raise
    inserts. Loud duplication beats silent loss.
    """
    session = _session(seeded)
    ids = {_raise(session, object_id=None, kind="CONTROL_TOTAL_MISMATCH",
                  object_type="period_slice", detail=f"slice {n}")
           for n in range(3)}
    seeded.commit()
    assert len(ids) == 3
    assert _count(seeded, object_type="period_slice") == 3


@PG
def test_a_resolved_exception_can_be_raised_again(seeded):
    """The partial index is partial for this reason, stated in 011's header:

        "once an exception is resolved, the same condition recurring is a
         genuinely new exception and must be raisable again."

    Without ``WHERE status = 'Open'`` the first resolution would suppress the
    condition for ever, and a GRN line that failed to attribute in March would
    be invisible when it failed again in April.
    """
    session = _session(seeded)
    first = _raise(session, source_paise=125_00)
    store.act_on_exception(session, exception_id=first, action="retry",
                           actor="U-FIN", reason="vendor re-sent the receive")
    second = _raise(session, source_paise=125_00)
    seeded.commit()

    assert second != first, (
        "the condition recurred after a resolution and no new exception was "
        "raised: the unique index is not partial on status")
    assert _count(seeded, object_id="GRN-1:#0") == 2
    assert _count(seeded, object_id="GRN-1:#0",
                  status=store.EXCEPTION_OPEN) == 1


# ================================================ a resolution names who & when
@PG
@pytest.mark.parametrize("action,expected_status", [
    ("resolve", "Resolved"),
    ("retry", "Resolved"),
    ("ignore", "Accepted"),
    ("write_off", "Written_off"),
])
def test_each_action_lands_on_its_c18_status_and_stamps_who_and_when(
        seeded, action, expected_status):
    """All four verbs, all four columns, one statement.

    `ck_reconciliation_exception_resolution` is a BICONDITIONAL: a non-Open row
    must carry both `resolved_at` and `resolved_by`, and an Open row must carry
    neither. Setting the status in one statement and the actor in another would
    be refused between them, so there is no such path -- proved by the row that
    comes back.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    result = store.act_on_exception(
        session, exception_id=exception_id, action=action, actor="U-FIN",
        reason="reconciled against the vendor statement", now=T0)
    seeded.commit()

    assert result["status"] == expected_status
    assert expected_status in store.EXCEPTION_STATUSES
    assert result["resolved_by"] == "U-FIN"
    assert result["resolved_at"] == T0
    assert result["resolution_note"] == "reconciled against the vendor statement"
    assert store.open_exceptions(session, entity_id=ENTITY) == []


@PG
def test_the_database_refuses_a_status_change_that_names_nobody(seeded):
    """The constraint, not the Python. Attempted raw, and caught.

    If this only held in :func:`act_on_exception`, any future writer -- a
    migration, a support script, a second service -- could leave an exception
    Resolved by nobody, and the audit trail the control rests on would have a
    hole in it that no test would notice.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    seeded.commit()

    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        seeded.execute(
            "UPDATE reconciliation_exception SET status = 'Resolved'"
            " WHERE exception_id = %s", (exception_id,))
    assert "ck_reconciliation_exception_resolution" in str(exc.value)
    seeded.rollback()

    # ... and the mirror half: an Open row may not carry a resolution.
    with pytest.raises(psycopg.errors.CheckViolation):
        seeded.execute(
            "UPDATE reconciliation_exception SET resolved_by = 'U-SNEAK',"
            " resolved_at = now() WHERE exception_id = %s", (exception_id,))
    seeded.rollback()


@PG
@pytest.mark.parametrize("action", sorted(store.EXCEPTION_ACTIONS))
@pytest.mark.parametrize("reason", ["", "   ", "\t\n "])
def test_every_action_refuses_a_blank_reason(seeded, action, reason):
    """`resolution_note` is NULLABLE in 011, so the database would accept this.

    An exception closed with no explanation is indistinguishable from one
    closed by accident. The rule therefore lives in the writer, and this is
    what holds it there. Whitespace-only is refused too -- a space is not a
    reason, and " " is what a required-field check that only tests ``if not
    reason`` lets through.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.act_on_exception(session, exception_id=exception_id,
                               action=action, actor="U-FIN", reason=reason)
    assert exc.value.code == "BLANK_EXCEPTION_REASON"
    seeded.rollback()


@PG
def test_an_action_refuses_a_blank_actor(seeded):
    """A resolution names WHO. An empty string is not a who."""
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.act_on_exception(session, exception_id=exception_id,
                               action="resolve", actor="  ", reason="fixed")
    assert exc.value.code == "BLANK_EXCEPTION_ACTOR"


@PG
def test_a_resolved_exception_cannot_be_resolved_again(seeded):
    """The first reviewer's decision is evidence, not a draft.

    Silently overwriting it would let a second reviewer replace the name and
    the reason on a closed finding, which is the one thing an audit trail must
    not permit.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    store.act_on_exception(session, exception_id=exception_id,
                           action="ignore", actor="U-FIN",
                           reason="immaterial, accepted", now=T0)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.act_on_exception(session, exception_id=exception_id,
                               action="resolve", actor="U-OTHER",
                               reason="actually I fixed it")
    assert exc.value.code == "EXCEPTION_NOT_OPEN"
    assert exc.value.status == 409
    assert "U-FIN" in exc.value.message, (
        "the refusal must name who already decided, or the second reviewer "
        "cannot tell a stale screen from a permissions problem")


@PG
def test_acting_on_an_exception_that_does_not_exist_is_a_404_not_a_409(seeded):
    """The two refusals are told apart, because they send a reviewer to
    different places."""
    session = _session(seeded)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.act_on_exception(session, exception_id="EXC-NOPE",
                               action="resolve", actor="U-FIN", reason="x")
    assert exc.value.code == "EXCEPTION_NOT_FOUND"
    assert exc.value.status == 404


# ======================================================== the frozen namespaces
@PG
def test_the_database_refuses_a_sixth_kind(seeded):
    """Five kinds, closed by `ck_reconciliation_exception_kind`.

    The Python guard in `raise_exception` is the friendly failure; this is the
    one that holds when something writes around it. A sixth kind is a
    deliberate contract change, not a typo that silently creates a category
    nobody triages.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        seeded.execute(
            "INSERT INTO reconciliation_exception (exception_id, kind,"
            " object_type, detail) VALUES ('EXC-X', 'INVENTED_KIND',"
            " 'grn_line', 'nope')")
    assert "ck_reconciliation_exception_kind" in str(exc.value)
    seeded.rollback()


@PG
def test_the_store_refuses_a_sixth_kind_before_it_reaches_the_server(seeded):
    session = _session(seeded)
    with pytest.raises(store.IntegrationStoreError) as exc:
        _raise(session, kind="INVENTED_KIND")
    assert exc.value.code == "UNKNOWN_EXCEPTION_KIND"


@PG
def test_the_database_refuses_a_status_outside_c18(seeded):
    """C18 freezes Open / Resolved / Accepted / Written_off, and 011 transcribes
    it verbatim. 'CLOSED' in particular must be refused: it is a C3 BUSINESS
    status, it means something else entirely, and C18 records that an exception
    status may never reach a business screen."""
    session = _session(seeded)
    exception_id = _raise(session, source_paise=1)
    seeded.commit()
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        seeded.execute(
            "UPDATE reconciliation_exception SET status = 'CLOSED',"
            " resolved_at = now(), resolved_by = 'U-X'"
            " WHERE exception_id = %s", (exception_id,))
    assert "ck_reconciliation_exception_status" in str(exc.value)
    seeded.rollback()


@PG
def test_the_python_constants_transcribe_the_migration_rather_than_invent_it(seeded):
    """Drift between the module's constants and the CHECKs is caught here.

    The constants are a transcription, not a second source of truth: when they
    and the migration disagree, the migration is right and the module is
    broken.
    """
    body = seeded.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'ck_reconciliation_exception_kind'").fetchone()[0]
    for kind in store.EXCEPTION_KINDS:
        assert f"'{kind}'" in body, kind
    assert body.count("'") // 2 == len(store.EXCEPTION_KINDS), (
        "the CHECK permits a kind the module does not know about")

    body = seeded.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'ck_reconciliation_exception_status'").fetchone()[0]
    for status in store.EXCEPTION_STATUSES:
        assert f"'{status}'" in body, status
    assert body.count("'") // 2 == len(store.EXCEPTION_STATUSES)
    assert set(store.EXCEPTION_ACTIONS.values()) <= set(store.EXCEPTION_STATUSES)
    assert store.EXCEPTION_OPEN not in store.EXCEPTION_ACTIONS.values(), (
        "no triage verb may leave an exception Open; that is not a decision")


# ============================================================== money, honestly
@PG
def test_a_negative_source_amount_is_held_at_full_magnitude(seeded):
    """A return or a credit note carries a NEGATIVE line total.

    `dto.paise()` passes ``allow_negative=True`` deliberately, and
    `sweeps._attribute` hands `line_total_paise` straight through. But
    `ck_reconciliation_exception_paise` forbids a negative on either side --
    "these are magnitudes of two sides, and a sign would silently encode a
    direction the `kind` is supposed to carry."

    So the column takes the magnitude, which IS the full value §11.8 demands
    the line be held at, and the signed original goes to the audit trail. The
    alternative -- passing the negative through -- is a CheckViolation at 3am
    inside a cron function whose purpose was to report a different problem.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=-150, local_paise=-25_000)
    seeded.commit()

    row = seeded.execute(
        "SELECT local_paise, source_paise FROM reconciliation_exception"
        " WHERE exception_id = %s", (exception_id,)).fetchone()
    assert row == (25_000, 150), "the magnitude, not the signed value"

    signed = seeded.execute(
        "SELECT detail->>'source_paise_signed', detail->>'local_paise_signed'"
        " FROM integration_event"
        " WHERE kind = 'reconciliation.exception.raised'").fetchone()
    assert signed == ("-150", "-25000"), (
        "the direction must be recoverable from the trail; a magnitude that "
        "cost the sign is information destroyed, not information normalised")


@PG
def test_the_exposure_sum_is_an_int_and_not_a_decimal(seeded):
    """THE test that cannot exist without a real server.

    PostgreSQL's ``SUM()`` over a `bigint` column returns **numeric**, and
    psycopg maps numeric to `decimal.Decimal`. `open_exception_exposure`
    casts ``::bigint`` for exactly that reason. Remove the cast and this fails
    here and nowhere else -- which is precisely how a Decimal reached
    `check_availability`, met a float, raised `TypeError`, and took down the
    availability verdict, both approval paths and the concurrency proof.

    A `TypeError` was the lucky version. The dangerous one is a Decimal that
    participates in arithmetic quietly and reaches a JSON body as a
    non-integer amount.
    """
    session = _session(seeded)
    for n in range(5):
        _raise(session, object_id=f"GRN-SUM:#{n}", source_paise=1_00,
               local_paise=2_00)
    seeded.commit()

    exposure = store.open_exception_exposure(session, entity_id=ENTITY)
    assert exposure == {"open_count": 5, "source_paise": 500,
                        "local_paise": 1000}
    for key, value in exposure.items():
        assert isinstance(value, int), (key, type(value))
        assert not isinstance(value, Decimal), (
            f"{key} came back as a Decimal: the ::bigint cast is missing")


@PG
def test_an_exposure_over_no_rows_is_zero_and_not_none(seeded):
    """``coalesce(..., 0)``. A bare SUM over an empty set is NULL, and a NULL
    exposure reaching a screen reads as "unknown", which is the one thing this
    number must never be."""
    session = _session(seeded)
    assert store.open_exception_exposure(session, entity_id=ENTITY) == {
        "open_count": 0, "source_paise": 0, "local_paise": 0}


# =========================================================== period close gate
@PG
def test_an_open_exception_blocks_the_period_close(seeded):
    """§11.8's gate, end to end, through the real `transition_period`.

    The scenario it exists for: a sweep raises GRN_LINE_UNATTRIBUTED for a
    real sum, and finance closes the period anyway, and CWIP publishes a
    number nobody can stand behind. Until 011 landed this control was
    unreachable -- `_has_open_reconciliation_exceptions` answered `False` when
    the table was absent, and `False` is the value that PERMITS a close.
    """
    session = _session(seeded)
    seeded.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start,"
        " period_end, state, created_by) VALUES ('PER-1', %s, %s, %s,"
        " 'SOFT_CLOSED', 'T')",
        (ENTITY, date(2026, 8, 1), date(2026, 8, 31)))
    seeded.commit()

    exception_id = _raise(session, source_paise=125_00)
    seeded.commit()

    with pytest.raises(periods_mod.PeriodServiceError) as exc:
        periods_mod.transition_period(session, period_id="PER-1",
                                      to_state="CLOSED", actor="U-FIN")
    assert exc.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"
    seeded.rollback()

    # Resolve it, and the same close now proceeds -- so the gate is the
    # exception, not something else that happens to refuse.
    store.act_on_exception(session, exception_id=exception_id,
                           action="resolve", actor="U-FIN",
                           reason="matched to GRN-1 by hand", now=T0)
    seeded.commit()
    result = periods_mod.transition_period(session, period_id="PER-1",
                                           to_state="CLOSED", actor="U-FIN")
    assert result["state"] == "CLOSED"


@PG
def test_an_accepted_exception_does_not_block_a_close(seeded):
    """`ignore` lands on `Accepted`: nothing was fixed, a person decided to
    live with it, and the close may proceed. The gate is on `Open` alone --
    which is why `ignore` and `resolve` must land on different statuses, or a
    close report could not tell "corrected" from "tolerated"."""
    session = _session(seeded)
    seeded.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start,"
        " period_end, state, created_by) VALUES ('PER-2', %s, %s, %s,"
        " 'SOFT_CLOSED', 'T')",
        (ENTITY, date(2026, 8, 1), date(2026, 8, 31)))
    exception_id = _raise(session, source_paise=1_00)
    store.act_on_exception(session, exception_id=exception_id, action="ignore",
                           actor="U-FIN", reason="immaterial", now=T0)
    seeded.commit()
    assert periods_mod.transition_period(
        session, period_id="PER-2", to_state="CLOSED",
        actor="U-FIN")["state"] == "CLOSED"


# ==================================================================== the trail
@PG
def test_every_mutation_leaves_an_event_under_its_correlation_id(seeded):
    """One id traces the whole life of an exception (§11.9).

    A re-walk is recorded distinctly from a first raise: an operator looking
    at "why is this still open" needs to see that the sweep kept finding it,
    and a trail that recorded nothing on a re-walk looks identical to a sweep
    that stopped running.
    """
    session = _session(seeded)
    exception_id = _raise(session, source_paise=125_00)
    _raise(session, source_paise=125_00)             # the re-walk
    store.act_on_exception(session, exception_id=exception_id, action="retry",
                           actor="U-FIN", reason="vendor re-sent", now=T0)
    seeded.commit()

    kinds = [event["kind"] for event in store.trace(session, "CORR-1")]
    assert kinds == ["reconciliation.exception.raised",
                     "reconciliation.exception.rewalked",
                     "reconciliation.exception.retry"]

    detail = seeded.execute(
        "SELECT detail FROM integration_event"
        " WHERE kind = 'reconciliation.exception.retry'").fetchone()[0]
    assert detail["exception_id"] == exception_id
    assert detail["reason"] == "vendor re-sent"
    assert detail["status"] == "Resolved"
    assert detail["action"] == "retry"


# ====================================================================== scope
@PG
def test_a_restricted_principal_cannot_see_another_entitys_exceptions(
        seeded, pg_url, pg_disposable_db_name):
    """Under `capex_app`, not the CI superuser.

    A superuser bypasses row-level security unconditionally, so this assertion
    made through `pg_database` would be satisfied by rows that were in fact
    visible. `pg_app_database` issues `SET LOCAL ROLE capex_app` so
    `current_user` in the transaction is what it is in production.
    """
    session = _session(seeded, entities=(ENTITY, OTHER_ENTITY))
    _raise(session, object_id="GRN-A:#0", entity_id=ENTITY,
           project_id=PROJECT, source_paise=100)
    _raise(session, object_id="GRN-B:#0", entity_id=OTHER_ENTITY,
           project_id=OTHER_PROJECT, source_paise=200)
    seeded.commit()

    from conftest_pg import scoped_role_database
    database = scoped_role_database(pg_url, pg_disposable_db_name)
    try:
        with database.session(Scope(user_id="U-ONE",
                                    entity_ids=frozenset({ENTITY}))) as scoped:
            visible = store.open_exceptions(scoped)
            assert [r["object_id"] for r in visible] == ["GRN-A:#0"]
            assert store.open_exception_exposure(scoped)["source_paise"] == 100
    finally:
        database.close()


@PG
def test_a_restricted_principal_cannot_resolve_another_entitys_exception(seeded):
    """Reading is not the only leak. An exception a principal cannot see must
    also be one they cannot close -- otherwise the gate on their own entity's
    period could be lifted by someone else's action."""
    wide = _session(seeded, entities=(ENTITY, OTHER_ENTITY))
    other = _raise(wide, object_id="GRN-B:#0", entity_id=OTHER_ENTITY,
                   project_id=OTHER_PROJECT, source_paise=200)
    seeded.commit()

    narrow = _session(seeded, entities=(ENTITY,))
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.act_on_exception(narrow, exception_id=other, action="resolve",
                               actor="U-ONE", reason="not mine to close")
    assert exc.value.code == "EXCEPTION_NOT_FOUND"
    assert exc.value.status == 404, (
        "an out-of-scope row answers 404, exactly as a non-existent one does; "
        "a 403 would confirm the row exists to someone who may not see it")
    seeded.rollback()
    assert _count(seeded, exception_id=other,
                  status=store.EXCEPTION_OPEN) == 1


@PG
def test_raising_into_an_entity_out_of_scope_is_refused_loudly(seeded):
    """And returns NO id.

    A fabricated id would be handed straight to `accumulate_unattributed` as
    a bucket key, and the value would be attributed to a row that does not
    exist -- the silent drop, one layer down, produced by the refusal itself.
    """
    session = _session(seeded, entities=(ENTITY,))
    with pytest.raises(store.IntegrationStoreError) as exc:
        _raise(session, entity_id=OTHER_ENTITY, project_id=OTHER_PROJECT)
    assert exc.value.code == "EXCEPTION_OUT_OF_SCOPE"
    seeded.rollback()
    assert _count(seeded) == 0


@PG
def test_an_unattributable_exception_is_visible_to_whoever_can_resolve_it(seeded):
    """011 makes both scope columns nullable and says why:

        "an exception nobody can attribute must be visible to whoever can
         resolve it"

    -- an UNSANCTIONED_COMMITMENT found on a purchase order we hold no local
    record of has no entity and no project to file it under. If scope hid it,
    the row that most needs a human would be the one nobody could see.
    """
    session = _session(seeded, entities=(ENTITY,))
    orphan = _raise(session, kind="UNSANCTIONED_COMMITMENT",
                    object_type="purchase_order", object_id="PO-UNKNOWN",
                    entity_id=None, project_id=None, source_paise=500_00,
                    detail="PO exists in the tenant and in no local record")
    _raise(session, object_id="GRN-A:#0", source_paise=100)
    seeded.commit()

    visible = {r["exception_id"] for r in store.open_exceptions(session)}
    assert orphan in visible, (
        "the unattributable exception was filtered out by the scope "
        "predicate; NULL = ANY(...) is NULL, which is falsy, and the "
        "disjunction that restores this case has been lost")
    assert len(visible) == 2


# ================================================= the surface with no schema
#
# NOT gated on CAPEX_DB_URL: these five refuse before they touch a session, so
# a live database would add nothing and the machine with no PostgreSQL is
# exactly where the refusal most needs to be observable.
@pytest.mark.parametrize("name", sorted(store.UNBACKED_SWEEP_SURFACE))
def test_the_unbacked_sweep_functions_refuse_rather_than_returning_a_default(
        name):
    """Every one of these has a "harmless" default that is not harmless.

    `resolve_po_line` -> None reads as "no matching PO line", sending every
    receive line to quarantine and blocking capitalisation estate-wide for a
    reason that is not true. `accumulate_unattributed` -> None reads as "the
    value is held"; it would be held nowhere, which is the silent drop this
    whole surface exists to prevent, produced by the function that promises to
    prevent it. `bills_awaiting_detail` -> [] reads as "queue empty", so the
    hydration sweep reports success having fetched nothing and every bill
    stays line-less -- no attribution, zero CWIP, green build.

    So they raise, naming the table that does not exist. Same call
    `periods._has_open_reconciliation_exceptions` makes, and the same one
    `outbound.DetectiveControlUnavailable` makes: a control that cannot be
    evaluated must refuse, not proceed.
    """
    # A recorder, so the assertion is also that NO statement was issued: a
    # function that reached the database before refusing would have had
    # somewhere to write after all.
    session = _Recorder()
    args = {
        "resolve_po_line": {"po_external_id": "PO-1", "line_external_id": "L-1"},
        "record_receive_line": {"po_line_id": "POL-1",
                                "receive_external_id": "GRN-1",
                                "line_external_id": None, "quantity": "1",
                                "amount_paise": 100},
        "accumulate_unattributed": {"project_id": PROJECT, "paise": 100,
                                    "source_key": "EXC-1"},
        "bills_awaiting_detail": {"connection_id": "CONN-1", "limit": 10},
        "mark_detail_hydrated": {"connection_id": "CONN-1",
                                 "external_id": "BILL-1"},
    }[name]
    with pytest.raises(store.SchemaNotYetMigrated) as exc:
        getattr(store, name)(session, **args)
    assert exc.value.code == "SCHEMA_NOT_YET_MIGRATED"
    assert exc.value.status == 501
    assert store.UNBACKED_SWEEP_SURFACE[name] in exc.value.message, (
        "the refusal must name the table the lead needs to create, or it is "
        "just an error someone will paper over with a try/except")
    assert session.statements == [], (
        f"{name} issued SQL before refusing; it has a table after all, and "
        f"this entry in UNBACKED_SWEEP_SURFACE is stale")


def test_the_unbacked_surface_is_exactly_what_sweepstore_still_needs():
    """The list is data so it cannot rot silently.

    The day the migration lands, these five stop raising and this test is the
    one that says which of them are done.
    """
    from app.backend.integration import sweeps

    implemented = {"upsert_inbox", "get_watermark", "set_watermark",
                   "raise_exception", "open_exceptions"}
    for name in store.UNBACKED_SWEEP_SURFACE:
        assert hasattr(sweeps.SweepStore, name), (
            f"{name} is declared unbacked but SweepStore no longer needs it")
        assert name not in implemented


# ============================================================= the SQL itself
#
# NO DATABASE. These run everywhere, including on the machine with no
# PostgreSQL -- which matters because everything above this line skips there.
#
# `tests/test_integration_sql_matches_schema.py` does this for
# `app/backend/integration/*.py` and says plainly what it cannot do: it is not
# a SQL parser, it cannot check that an `ON CONFLICT` target matches a real
# unique index, and it cannot tell a name that exists from a name used wrongly.
# It also does not walk `app/backend/pg/` at all, so the statements added for
# §11.8 had no static gate of any kind.
#
# These close the part that can be closed without a server, and they do it
# against the RENDERED statement -- the f-string evaluated, the `{scope}` token
# already substituted by `repo.query` -- rather than against source text with
# its interpolations blanked out. A recorder session captures exactly what
# would have gone to the wire.

_MIGRATION_011 = (_Path(__file__).resolve().parents[1] / "migrations" / "pg"
                  / "011_reconciliation_exception.sql")


def _ddl_columns() -> set:
    """The column names ``CREATE TABLE reconciliation_exception`` declares."""
    text = _MIGRATION_011.read_text(encoding="utf-8")
    start = text.index("CREATE TABLE reconciliation_exception")
    body = text[text.index("(", start) + 1:text.index("\n);", start)]
    columns = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("--", "CONSTRAINT", ")")):
            continue
        name = line.split()[0]
        if name.isidentifier():
            columns.add(name)
    return columns


class _Recorder:
    """A `Session` stand-in that records statements and returns no rows.

    Returning nothing makes every writer take its refusal branch, so one call
    per function captures the statement AND proves the refusal exists -- see
    the module's own rule about never returning a fabricated id.
    """

    def __init__(self) -> None:
        self.statements: list = []
        self.scope = Scope(user_id="U-SQL", entity_ids=frozenset({ENTITY}),
                           project_ids=frozenset({PROJECT}))

    def fetchall(self, statement, params=None):
        self.statements.append(statement)
        return []

    def fetchone(self, statement, params=None):
        self.statements.append(statement)
        return None

    def execute(self, statement, params=None):
        self.statements.append(statement)


def _rendered() -> dict:
    """Every reconciliation statement, as it would reach the server."""
    out = {}
    calls = (
        ("raise_exception", lambda s: store.raise_exception(
            s, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
            object_id="GRN-1:#0", detail="d", raised_at=T0, entity_id=ENTITY,
            project_id=PROJECT, source_paise=1, local_paise=2)),
        ("open_exceptions", lambda s: store.open_exceptions(
            s, entity_id=ENTITY, project_id=PROJECT,
            kinds=["GRN_LINE_UNATTRIBUTED"])),
        ("open_exception_exposure", lambda s: store.open_exception_exposure(
            s, entity_id=ENTITY, project_id=PROJECT)),
        ("act_on_exception", lambda s: store.act_on_exception(
            s, exception_id="EXC-1", action="resolve", actor="U-FIN",
            reason="r", now=T0)),
    )
    for name, call in calls:
        recorder = _Recorder()
        try:
            call(recorder)
        except store.IntegrationStoreError:
            pass                  # the refusal branch; the SQL is what we want
        out[name] = recorder.statements
    return out


def test_every_reconciliation_statement_names_only_real_columns():
    """A column that does not exist is a 3am runtime error in a cron function.

    Checked against the migration text, so the migration stays the single
    source of truth and this module's constants stay a transcription of it.
    """
    columns = _ddl_columns()
    assert {"exception_id", "resolved_by", "source_paise"} <= columns, (
        "the DDL parser has stopped finding columns; it is asserting nothing")
    problems = []
    for name, statements in _rendered().items():
        for statement in statements:
            for a, b in re.findall(
                    r"\bx\.(\w+)|reconciliation_exception\.(\w+)", statement):
                column = a or b
                if column not in columns:
                    problems.append(f"{name}: reconciliation_exception.{column}")
    assert not problems, (
        f"these columns are named in SQL and do not exist in "
        f"{_MIGRATION_011.name}: {sorted(set(problems))}")


def test_the_on_conflict_target_is_the_partial_index_that_actually_exists():
    """The check `test_integration_sql_matches_schema.py` says it cannot make.

    Idempotency IS this index. Inferring it needs BOTH its column list and its
    predicate: a target of `(kind, object_type, object_id)` alone matches no
    index on this table, and PostgreSQL refuses the statement outright -- the
    good failure, but only if it is caught before production. A target that
    matched a DIFFERENT index would be the bad one, and is what this rules out.
    """
    migration = _MIGRATION_011.read_text(encoding="utf-8")
    index = re.search(
        r"CREATE UNIQUE INDEX ux_reconciliation_exception_open\s*"
        r"ON reconciliation_exception\s*\(([^)]*)\)\s*WHERE ([^;]+);",
        migration)
    assert index, "ux_reconciliation_exception_open is not in the migration"
    index_columns = [c.strip() for c in index.group(1).split(",")]
    index_predicate = " ".join(index.group(2).split())

    inserts = [s for s in _rendered()["raise_exception"] if "ON CONFLICT" in s]
    assert len(inserts) == 1, "raise_exception no longer issues exactly one upsert"
    flat = " ".join(inserts[0].split())
    target = re.search(r"ON CONFLICT \(([^)]*)\) WHERE (.+?) DO ", flat)
    assert target, f"the ON CONFLICT target is not inferrable: {flat}"

    assert [c.strip() for c in target.group(1).split(",")] == index_columns, (
        f"the ON CONFLICT column list {target.group(1)!r} does not match the "
        f"unique index {index.group(1)!r}")
    assert target.group(2).strip() == index_predicate, (
        f"the ON CONFLICT predicate {target.group(2)!r} does not match the "
        f"index predicate {index_predicate!r}; a partial index is inferrable "
        f"only by a statement that repeats its WHERE clause exactly")
    assert "DO UPDATE" in flat, (
        "DO NOTHING returns no row, so the existing exception_id would have to "
        "be recovered by a follow-up SELECT -- a read-after-write with a "
        "window a concurrent sweep lands in")


def test_every_paise_sum_is_cast_back_to_bigint():
    """`SUM()` over `bigint` returns numeric; psycopg maps numeric to Decimal.

    The live test proves the value is an `int`; this one fails on the dev
    machine, in a second, without a server -- which matters because the cast is
    exactly the sort of thing removed during a tidy-up by someone who has never
    seen it fail.
    """
    seen = 0
    for name, statements in _rendered().items():
        for statement in statements:
            # A 40-character tail, matching `test_money_sql_discipline.py`'s
            # own window: the cast sits AFTER the `coalesce(...)` that wraps
            # the SUM, not immediately after the SUM's own paren.
            #
            # The tail is a LOOKAHEAD, so it is not consumed. Captured
            # normally it swallowed the next `SUM(` forty characters later,
            # and a statement with two paise sums reported one -- the second
            # could have been uncast and this gate would have said nothing.
            for tail in re.findall(
                    r"SUM\s*\([^()]*_paise[^()]*\)(?=(.{0,40}))",
                    statement, re.I | re.DOTALL):
                seen += 1
                assert "::bigint" in tail, (
                    f"{name}: a SUM over a paise column is not cast ::bigint")
    assert seen >= 2, (
        "no paise SUM was found at all; this gate is asserting nothing")


def test_every_reconciliation_statement_carries_a_compiled_scope_predicate():
    """`{scope}` must have done real work, not been pasted somewhere harmless.

    `repo.query` substitutes the token before execution, so a rendered
    statement still containing it never reached `query()` at all, and one that
    compiled to a bare `TRUE` was handed a mapping waiving every dimension the
    caller restricts. The recorder's scope restricts entity AND project.
    """
    for name, statements in _rendered().items():
        assert statements, f"{name} issued no statement at all"
        for statement in statements:
            assert "{scope}" not in statement, (
                f"{name}: the scope token was never substituted")
            assert "= ANY(%(__scope_" in statement, (
                f"{name}: the scope predicate compiled to nothing, so the "
                f"token is decorative")


def test_the_writer_never_returns_an_id_for_a_row_it_did_not_write():
    """The recorder writes nothing, so `raise_exception` must refuse.

    A fabricated id goes straight to `accumulate_unattributed` as the bucket's
    `source_key`, and the quarantined value is attributed to a row that does
    not exist. That is the silent drop, one layer below the code written to
    prevent it.
    """
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.raise_exception(
            _Recorder(), kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
            object_id="GRN-1:#0", detail="d", raised_at=T0, entity_id=ENTITY,
            project_id=PROJECT, source_paise=125_00)
    assert exc.value.code == "EXCEPTION_OUT_OF_SCOPE"
    assert exc.value.status == 403


def test_the_magnitude_helper_keeps_full_value_and_loses_no_sign():
    """§11.8: an unattributed line is held at FULL value, never spread.

    Arithmetic, so it runs without a server -- and the arithmetic has to be
    right before the column ever sees it.
    """
    assert store._magnitude(-150, side="source") == 150
    assert store._magnitude(150, side="source") == 150
    assert store._magnitude(0, side="source") == 0
    assert store._magnitude(None, side="source") is None
    # Full value, not a share of it: two quarantined lines total both.
    assert (store._magnitude(-300_00, side="source")
            + store._magnitude(-700_00, side="source")) == 1000_00


def test_no_triage_verb_can_leave_an_exception_open():
    """Every action maps to a terminal C18 status, checked without a server.

    An action landing back on `Open` would satisfy
    `ck_reconciliation_exception_resolution` only by carrying no actor -- and
    would then look exactly like an exception nobody had triaged.
    """
    assert set(store.EXCEPTION_ACTIONS.values()) <= set(store.EXCEPTION_STATUSES)
    assert store.EXCEPTION_OPEN not in store.EXCEPTION_ACTIONS.values()
    assert set(store.EXCEPTION_ACTIONS) == {"resolve", "retry", "ignore",
                                            "write_off"}
