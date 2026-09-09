"""§11.8's period-close gate, over rows a real server actually stores.

WHY THIS FILE EXISTS SEPARATELY FROM `tests/test_pg_periods.py`
===============================================================

That file's reconciliation-guard tests are about the table being ABSENT: they
drive `_has_open_reconciliation_exceptions` with a fake session and assert it
REFUSES rather than returning a falsy "nothing blocks this close". Every one
of them passes against a schema in which `reconciliation_exception` has since
landed (migration 011) and in which the gate was, on that schema, still
unreachable for one whole class of row.

The defect they could not see:

    WHERE entity_id = %s AND status = 'Open'

011 makes `entity_id` NULLABLE, at line 68, and says why in its own comment --
"some exceptions are raised before the owning entity or project is known". SQL
NULL is not equal to anything, so `NULL = 'ENT-1'` evaluates to NULL and never
to TRUE. An Open UNATTRIBUTED exception therefore matched no row, the gate
returned False, and the period closed.

Meanwhile Wave 7's `pg/closure.py:_open_exceptions` counts TWO classes
over the same table -- attributed, and `entity_id IS NULL` -- and blocks the
capitalisation on either. Reproduced with a single Open unattributed exception
worth Rs 1,20,00,000: the capitalisation gate said BLOCKED and the period gate
said CLEAR, about the same row, in the same database. Migration 012's header
(012:24) documents this exact trap and gives unattributed rows a dedicated
`WHEN entity_id IS NULL` RLS branch so they stay visible to whoever can
resolve them.

`periods._has_open_reconciliation_exceptions`'s own docstring already named
the scenario as the reason the gate exists: "a sweep raises
GRN_LINE_UNATTRIBUTED for a real sum, there is nowhere to write it, finance
closes the period, and CWIP publishes a number nobody can stand behind." The
sum had somewhere to be written by then. It just did not block anything.

A SKIP IS NOT A PASS
====================

Nothing here can be answered without a server: the question is what
PostgreSQL does with `NULL = 'ENT-1'` inside a real WHERE clause, and a fake
session that returns whatever it was told is precisely the instrument that
missed this. There is no PostgreSQL on the workstation these tests were
written on, so every live test below HAS NEVER EXECUTED HERE and first runs in
CI's `pg_tests` job. The one test that does run everywhere is
`test_the_gate_names_both_classes_of_open_exception`, which reads the SQL.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg
# is deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace. Same note as tests/test_pg_periods.py.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import periods  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- the question these ask is "
            "what a real WHERE clause does with a NULL entity_id, and the fake "
            "session that answers it in-process is the exact instrument that "
            "missed this defect."),
)

#: The unattributed sum, deliberately un-round and deliberately large: it is
#: the figure a reader would have to stand behind if the period closed over it.
UNATTRIBUTED_PAISE = 1_20_00_000


def _seed(connection, *, suffix: str) -> dict[str, str]:
    """One organisation, one entity, one SOFT_CLOSED period, and one project.

    SOFT_CLOSED rather than FUTURE, because `SOFT_CLOSED -> CLOSED` is the only
    transition the exception gate is consulted on -- `transition_period` checks
    it under `if to_state == "CLOSED"`. Seeding FUTURE and driving the machine
    forward would test the same thing through two extra transitions that could
    fail for their own reasons.

    The project exists so a scoped principal could see the period at all
    (`periods._PERIOD_SCOPE_EXISTS` reads the entity's projects); these tests
    use `Scope.system()`, but a fixture that only works for a system scope
    would quietly stop proving anything the day one of them is tightened.
    """
    ids = {
        "org": f"O_{suffix}", "entity": f"E_{suffix}",
        "period": f"PER_{suffix}", "project": f"PRJ_{suffix}",
        "other_entity": f"EO_{suffix}",
    }
    ex = connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    for key, code in (("entity", "EC"), ("other_entity", "EO")):
        ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
           "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
           (ids[key], ids["org"], f"{code}_{suffix}"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Project','Released','t','t')",
       (ids["project"], ids["entity"], f"C_{suffix}"))
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, "
       "period_end, state, created_by) "
       "VALUES (%s,%s,'2026-01-01','2026-03-31','SOFT_CLOSED','t')",
       (ids["period"], ids["entity"]))
    connection.commit()
    return ids


def _raise_exception(connection, *, exception_id: str, entity_id: str | None,
                     object_id: str, paise: int = UNATTRIBUTED_PAISE) -> None:
    """One Open `GRN_LINE_UNATTRIBUTED` row. `entity_id=None` is the point.

    `kind` is one of 011's five permitted values and `object_id` is unique per
    call, because `ux_reconciliation_exception_open` is a partial unique index
    over (kind, object_type, object_id) WHERE status = 'Open' -- two Open rows
    with the same object would be refused by the schema, not by the gate.
    """
    connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, "
        "object_type, object_id, entity_id, status, detail, local_paise) "
        "VALUES (%s,'GRN_LINE_UNATTRIBUTED','GRN_LINE',%s,%s,'Open',%s,%s)",
        (exception_id, object_id, entity_id,
         "a receipt whose line resolves to no control cell", paise))
    connection.commit()


def _resolve(connection, exception_id: str) -> None:
    """011's `ck_reconciliation_exception_resolution`: a resolution names WHO
    and WHEN, or it is not a resolution. Both columns are required by a CHECK,
    so this is what resolving actually costs."""
    connection.execute(
        "UPDATE reconciliation_exception SET status = 'Resolved', "
        "resolved_at = now(), resolved_by = 'U-FIN', "
        "resolution_note = 'attributed to CC-1 and posted' "
        "WHERE exception_id = %s", (exception_id,))
    connection.commit()


def _state(connection, period_id: str) -> str:
    return connection.execute(
        "SELECT state FROM accounting_period WHERE period_id = %s",
        (period_id,)).fetchone()[0]


# ===========================================================================
# Source-only: the gate must name both classes
# ===========================================================================

def test_the_gate_names_both_classes_of_open_exception():
    """The only test in this file that runs on a machine without PostgreSQL.

    A source assertion is weak evidence on its own -- the live tests below are
    the real ones -- but it is the only evidence that exists at all on a
    workstation, and it fails the moment somebody 'simplifies' the predicate
    back to a single equality. `IS NULL` is not a stylistic choice here: it is
    the difference between a control that fires and one that cannot.
    """
    import inspect
    source = inspect.getsource(periods._has_open_reconciliation_exceptions)
    assert "entity_id IS NULL" in source, (
        "the close gate must count unattributed exceptions. `entity_id = %s` "
        "alone is NULL for them, and NULL is not TRUE, so they block nothing "
        "-- while closure.py blocks the capitalisation on the very same row.")
    assert "status = 'Open'" in source


# ===========================================================================
# Live: an unattributed exception blocks the close
# ===========================================================================

@pytest.mark.pg
@PG
def test_an_open_unattributed_exception_blocks_the_period_close(
        pg_database, pg_connection):
    """THE DEFECT, EXACTLY. One Open exception, `entity_id` NULL, worth
    Rs 1,20,00,000. Before the fix this closed the period without complaint."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=None, object_id=f"GL_{suffix}")

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(session, period_id=ids["period"],
                                      to_state="CLOSED", actor="U-CFO")

    assert excinfo.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"
    assert excinfo.value.status == 409
    assert _state(pg_connection, ids["period"]) == "SOFT_CLOSED", (
        "a refused close must not have moved the period; a 409 over a row "
        "that already says CLOSED is a worse outcome than no gate at all")


@pytest.mark.pg
@PG
def test_the_close_succeeds_once_the_unattributed_exception_is_resolved(
        pg_database, pg_connection):
    """The other half, and the half that stops this being a gate that simply
    refuses everything. A control that never lets a close through is not a
    control, it is an outage -- and it would pass the test above."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=None, object_id=f"GL_{suffix}")

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError):
            periods.transition_period(session, period_id=ids["period"],
                                      to_state="CLOSED", actor="U-CFO")

    _resolve(pg_connection, f"RX_{suffix}")

    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")

    assert result["state"] == "CLOSED"
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_an_open_exception_attributed_to_this_entity_still_blocks(
        pg_database, pg_connection):
    """The class the old predicate DID catch, kept -- widening a predicate is
    how the case it already handled stops being handled."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=ids["entity"], object_id=f"GL_{suffix}")

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(periods.PeriodServiceError) as excinfo:
            periods.transition_period(session, period_id=ids["period"],
                                      to_state="CLOSED", actor="U-CFO")

    assert excinfo.value.code == "PERIOD_HAS_OPEN_EXCEPTIONS"
    assert _state(pg_connection, ids["period"]) == "SOFT_CLOSED"


@pytest.mark.pg
@PG
def test_another_entitys_open_exception_does_not_block_this_close(
        pg_database, pg_connection):
    """THE NEGATIVE CASE, WHICH IS WHAT MAKES THE WIDENING HONEST. Adding
    `OR entity_id IS NULL` must admit rows attributed to NOBODY, not rows
    attributed to SOMEBODY ELSE. Without this, the cheapest way to make the
    test above pass would be to drop the entity predicate entirely -- and one
    entity's unresolved exception would then freeze every other entity's
    close, estate-wide, which is a different §11.8 violation and not a lesser
    one."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=ids["other_entity"], object_id=f"GL_{suffix}")

    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")

    assert result["state"] == "CLOSED"
    assert _state(pg_connection, ids["period"]) == "CLOSED"


@pytest.mark.pg
@PG
def test_a_resolved_unattributed_exception_never_blocked_anything(
        pg_database, pg_connection):
    """`status = 'Open'` is the other half of the predicate, and widening on
    `entity_id` must not have quietly widened on `status` too."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=None, object_id=f"GL_{suffix}")
    _resolve(pg_connection, f"RX_{suffix}")

    with pg_database.session(Scope.system()) as session:
        result = periods.transition_period(
            session, period_id=ids["period"], to_state="CLOSED", actor="U-CFO")

    assert result["state"] == "CLOSED"


@pytest.mark.pg
@PG
def test_the_period_gate_and_the_capitalisation_gate_agree_on_one_row(
        pg_database, pg_connection):
    """THE CONTRADICTION ITSELF, ASSERTED GONE.

    One Open unattributed row. `closure.py` counted it and blocked the
    capitalisation; `periods.py` did not count it and let the period close.
    Two controls, one §11.8, one row, two answers -- and the money in question
    is money nobody can attribute, held at FULL value by 012's design.

    This reads `closure`'s counter directly rather than driving a whole
    capitalisation, because what is under test is whether the two gates SEE the
    same row, not what either does about it afterwards.
    """
    from app.backend.pg import closure  # noqa: PLC0415 -- read-only, one test

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    _raise_exception(pg_connection, exception_id=f"RX_{suffix}",
                     entity_id=None, object_id=f"GL_{suffix}")

    with pg_database.session(Scope.system()) as session:
        exposure = closure._open_exceptions(
            session, project_id=ids["project"], entity_id=ids["entity"])
        period_blocked = periods._has_open_reconciliation_exceptions(
            session, ids["entity"])

    assert exposure["unattributed_count"] == 1
    assert exposure["unattributed_paise"] == UNATTRIBUTED_PAISE, (
        "012 holds an unattributed line at FULL value, never pro-rata")
    assert period_blocked is True, (
        f"closure.py sees {exposure['unattributed_count']} unattributed "
        f"exception(s) worth {exposure['unattributed_paise']} paise and "
        f"blocks the capitalisation; the period gate must see the same row. "
        f"It said {period_blocked!r}, which is how a period closes over a sum "
        f"nobody can stand behind.")
