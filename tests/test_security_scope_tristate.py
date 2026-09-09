"""Three scope states, kept distinct through every layer that touches them.

    None            -> unrestricted on this dimension
    frozenset()     -> nothing; this principal may see no row at all
    frozenset({..}) -> exactly these

THE FAILURE THIS EXISTS TO PREVENT is the collapse of the middle state into
the first. "No grants" reading as "all rows" is the single most damaging bug
this codebase can have, and it has been written three separate times: an empty
`columns` mapping compiling to an unconditional TRUE (`repo.compile_scope`'s
own comment), the emptiness check sitting after the waiver so a fully denied
scope compiled to TRUE against a table that waived every dimension, and a
maker-checker call that compared `None` and permitted everyone. Each was a
falsy value silently meaning "no restriction".

FOUR LAYERS CARRY THE THREE STATES, and each is asserted here on the same
three inputs so a divergence shows up as a diff rather than as a leak:

1. ``Scope.as_settings`` -- the wire format (`all` / `none` / `list`).
2. ``repo.compile_scope`` -- the primary control, SQL text and params.
3. ``rls.permits`` -- the pure-Python mirror of the RLS predicate.
4. ``capex_scope_permits`` in PostgreSQL -- the backstop itself (live only).

AND ONE PLACE WHERE LAYERS 2 AND 3 DELIBERATELY DISAGREE. A row whose
dimension VALUE is NULL is WAIVED by `capex_dimension_permits` and EXCLUDED by
`= ANY(...)`. That is documented in `rls.PROJECT_JOIN_NULL_DIMENSIONS_ARE_WAIVED`
as inherited behaviour, not a Wave 7 regression, and it is pinned here rather
than smoothed over: a test that asserted the two agree would be asserting
something false, and the first person to "fix" the test would be changing
`004_identity_scope.sql`'s semantics without knowing it.

Everything here runs without a database except the two ``@PG`` tests, which
have NEVER EXECUTED on the authoring machine and first run in CI.
"""
from __future__ import annotations

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

import pytest                                                    # noqa: E402

from app.backend.pg import repo, rls                             # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

UNRESTRICTED = Scope(user_id="U-1", entity_ids=None)
NOTHING = Scope(user_id="U-1", entity_ids=frozenset())
SOME = Scope(user_id="U-1", entity_ids=frozenset({"ENT-01"}))
COLUMNS = {"entity": "t.entity_id"}


# ======================================================================
# Layer 1 -- the wire format
# ======================================================================
def test_the_wire_format_keeps_the_three_states_apart():
    """`none` and `all` must not share a representation.

    They share an EMPTY ids string, which is exactly why the mode is a second
    setting: the ids alone cannot distinguish them, and any format that tried
    to would make the inversion representable.
    """
    assert UNRESTRICTED.as_settings()["capex.entity_mode"] == "all"
    assert NOTHING.as_settings()["capex.entity_mode"] == "none"
    assert SOME.as_settings()["capex.entity_mode"] == "list"

    assert UNRESTRICTED.as_settings()["capex.entity_ids"] == ""
    assert NOTHING.as_settings()["capex.entity_ids"] == ""
    assert SOME.as_settings()["capex.entity_ids"] == "ENT-01"


# ======================================================================
# Layer 2 -- the primary control
# ======================================================================
def test_compile_scope_gives_each_state_a_different_predicate():
    unrestricted, _ = repo.compile_scope(UNRESTRICTED, COLUMNS)
    nothing, nothing_params = repo.compile_scope(NOTHING, COLUMNS)
    some, some_params = repo.compile_scope(SOME, COLUMNS)

    assert unrestricted == "TRUE"
    assert nothing == "FALSE", (
        "an empty grant compiled to something other than FALSE. 'You may see "
        "rows in zero entities' must never compile to a predicate that "
        "returns rows.")
    assert nothing_params == {}
    assert "t.entity_id = ANY(" in some
    assert list(some_params.values()) == [["ENT-01"]]

    assert len({unrestricted, nothing, some}) == 3


def test_an_empty_grant_is_false_even_when_every_column_is_waived():
    """The regression the emptiness check was moved forward to fix.

    A table with no column for the restricted dimension waives it. Waiving
    answers "can this QUERY express the restriction"; emptiness answers "does
    this PRINCIPAL have any grant at all". A denied principal reading a table
    that waives everything -- `audit_log` was one -- must still see nothing.
    """
    predicate, params = repo.compile_scope(NOTHING, {"entity": None})
    assert predicate == "FALSE", (
        "a fully denied scope compiled to a permissive predicate because the "
        "table waived the dimension it was denied on.")
    assert params == {}


def test_a_restricted_dimension_the_query_cannot_express_is_refused_not_skipped():
    """Silently dropping a restriction widens access; raising does not."""
    with pytest.raises(repo.ScopeNotExpressible):
        repo.compile_scope(SOME, {"project": "t.project_id"})


def test_an_unrestricted_dimension_the_query_cannot_express_is_fine():
    """The mirror image, so the refusal above is not just "raise on anything"."""
    predicate, _ = repo.compile_scope(UNRESTRICTED, {"project": "t.project_id"})
    assert predicate == "TRUE"


@pytest.mark.parametrize("refused", ["*", "", "   "])
def test_a_wildcard_or_blank_scope_value_never_compiles_to_a_predicate(refused):
    """`*` must never be revived as "everything" by a future reader.

    Checked BEFORE the read_all short-circuit in `compile_scope`, so a scope
    carrying a corrupt id is refused whether or not it also happens to be
    unrestricted. A blank id is refused for the neighbouring reason: once
    rendered onto the comma-joined wire format it is indistinguishable from no
    id at all.
    """
    scope = Scope(user_id="U-1", entity_ids=frozenset({refused}))
    with pytest.raises(Exception) as excinfo:
        repo.compile_scope(scope, COLUMNS)
    assert "TRUE" not in str(excinfo.value)


@pytest.mark.parametrize("literal", ["ALL", "%", "_", "ANY"])
def test_a_value_that_merely_looks_like_a_wildcard_is_treated_as_an_id(literal):
    """The other half, and the one that would be dangerous to get wrong.

    Only `*` was ever the sentinel. `%` and `_` are SQL LIKE wildcards and
    `ALL`/`ANY` are SQL keywords, and a reader "hardening" this by widening
    the refusal would be closing nothing: these are compiled as parameters,
    never interpolated, so each restricts to an id that no row carries. What
    must never happen is the reverse -- one of them WIDENING the predicate.
    """
    scope = Scope(user_id="U-1", entity_ids=frozenset({literal}))
    predicate, params = repo.compile_scope(scope, COLUMNS)
    assert predicate != "TRUE"
    assert "= ANY(" in predicate
    assert list(params.values()) == [[literal]], (
        "a wildcard-looking id reached the SQL as anything other than a bound "
        "parameter value")


# ======================================================================
# Layer 3 -- the pure-Python mirror of the RLS predicate
# ======================================================================
def test_permits_gives_each_state_a_different_answer():
    assert rls.permits(UNRESTRICTED, entity_id="ENT-99") is True
    assert rls.permits(NOTHING, entity_id="ENT-99") is False
    assert rls.permits(NOTHING, entity_id="ENT-01") is False, (
        "an empty grant admitted a row. There is no id an empty set contains.")
    assert rls.permits(SOME, entity_id="ENT-01") is True
    assert rls.permits(SOME, entity_id="ENT-99") is False


def test_a_denied_scope_admits_nothing_on_any_dimension():
    """One denied dimension denies the row, whatever the others say."""
    denied_on_plant = Scope(user_id="U-1", plant_ids=frozenset())
    assert rls.permits(denied_on_plant, entity_id="ENT-01", plant_id="PL-01") is False
    assert rls.permits(denied_on_plant, entity_id="ENT-01") is True, (
        "precondition: a row that carries NO plant takes the waiver path -- "
        "that is the documented divergence, asserted directly below")


# ======================================================================
# The documented divergence, pinned rather than smoothed over
# ======================================================================
def test_a_null_row_value_is_waived_by_rls_and_excluded_by_the_compiler():
    """THE CONTRADICTION, ASSERTED IN BOTH DIRECTIONS.

    `capex_dimension_permits` returns TRUE for a NULL row value, because it
    was built to waive a dimension the ROW SHAPE lacks; `permits` mirrors it.
    `compile_scope` emits `= ANY(...)`, and SQL `NULL = ANY(...)` is NULL, not
    TRUE, so the same row is excluded by the primary control.

    On a project carrying no plant the backstop is therefore UNRESTRICTED on
    the plant dimension while `repo.query` is fully restrictive. The backstop
    over-permits rather than over-denies, which is the survivable direction --
    and it is survivable only because the primary control is the one that runs
    on every request.

    This test asserts the divergence EXISTS. Making the two agree is a change
    to `004_identity_scope.sql`'s frozen semantics, not a repair to this test.
    """
    scoped_to_one_plant = Scope(user_id="U-1", plant_ids=frozenset({"PL-01"}))

    # the backstop waives the row with no plant ...
    assert rls.permits(scoped_to_one_plant, plant_id=None) is True

    # ... while the compiler emits a predicate that excludes it, because
    # `NULL = ANY(ARRAY['PL-01'])` is NULL, which is not TRUE.
    predicate, params = repo.compile_scope(scoped_to_one_plant, {"plant": "p.plant_id"})
    assert "= ANY(" in predicate
    assert list(params.values()) == [["PL-01"]]

    assert rls.PROJECT_JOIN_NULL_DIMENSIONS_ARE_WAIVED == ("plant", "location"), (
        "the registry of dimensions this divergence applies to has changed. "
        "That is a scope decision about capex_dimension_permits, and this "
        "test is where it has to be re-argued.")


def test_the_divergence_is_documented_where_a_reader_will_meet_it():
    """A contradiction nobody wrote down is a trap for the next reader."""
    import inspect

    source = inspect.getsource(rls)
    assert "PROJECT_JOIN_NULL_DIMENSIONS_ARE_WAIVED" in source
    assert "capex_dimension_permits" in source
    assert "= ANY(" in source, (
        "the module documents the waiver but no longer states the compiler's "
        "opposite reading, which is the half that makes it a divergence.")


# ======================================================================
# Layer 4 -- live PostgreSQL. NEVER EXECUTED locally; first run in CI.
# ======================================================================
@PG
def test_capex_scope_permits_agrees_with_the_python_mirror_live(pg_connection):
    """The SQL predicate and `rls.permits`, on the same three states.

    Driven through the session settings the application actually sets, so this
    exercises the real `capex.entity_mode` / `capex.entity_ids` reading rather
    than a hand-written SQL literal.
    """
    cases = [
        ("all", "", "ENT-01", True),
        ("none", "", "ENT-01", False),
        ("list", "ENT-01", "ENT-01", True),
        ("list", "ENT-01", "ENT-99", False),
        # a NULL row value: waived, and this is the documented divergence
        ("list", "ENT-01", None, True),
    ]
    for mode, ids, row_value, expected in cases:
        pg_connection.execute("SELECT set_config('capex.entity_mode', %s, false)", (mode,))
        pg_connection.execute("SELECT set_config('capex.entity_ids', %s, false)", (ids,))
        got = pg_connection.execute(
            "SELECT capex_dimension_permits('entity', %s)", (row_value,)).fetchone()[0]
        assert got is expected, (
            f"capex_dimension_permits(mode={mode!r}, ids={ids!r}, "
            f"row={row_value!r}) returned {got!r}, expected {expected!r}")

    scope_none = Scope(user_id="U-1", entity_ids=frozenset())
    assert rls.permits(scope_none, entity_id="ENT-01") is False


@PG
def test_a_denied_scope_reads_no_row_through_the_repository_live(pg_database):
    """End to end: an empty grant returns zero rows, not the whole table."""
    denied = Scope(user_id="U-DENIED", entity_ids=frozenset())
    with pg_database.session(Scope.system()) as session:
        everything = repo.query(
            session, "SELECT entity_id FROM entity WHERE {scope}",
            scope=Scope.system(), columns={"entity": "entity_id"})
    with pg_database.session(denied) as session:
        nothing = repo.query(
            session, "SELECT entity_id FROM entity WHERE {scope}",
            scope=denied, columns={"entity": "entity_id"})

    assert len(everything) > 0, "precondition: the table has rows to hide"
    assert nothing == [], (
        f"a principal granted no entity read {len(nothing)} rows. An empty "
        "grant became an unrestricted one somewhere between the Scope and the "
        "SQL.")
