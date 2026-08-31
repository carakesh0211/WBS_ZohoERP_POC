"""Tests for `app.backend.pg.repo` -- the scoped query chokepoint.

Every test here is pure logic: `compile_scope` and `query()`'s `{scope}` token
enforcement do not touch a database, so these run without PostgreSQL. A
`FakeSession` stands in for `app.backend.pg.engine.Session` and records what
it was called with, so tests can assert on the compiled SQL and params
directly instead of needing a real connection.

Tests that need a live PostgreSQL database are marked `pytest.mark.pg` and
skip when `CAPEX_DB_URL` is not set; none of that applies to this file.
"""
from __future__ import annotations

import pytest

from app.backend.pg.engine import Scope
from app.backend.pg.repo import (  # noqa: F401
    ScopeNotExpressible,
    ScopeTokenMissing,
    compile_scope,
    query,
    query_one,
    require_scope_token,
)


class FakeSession:
    """Records calls; never touches a database."""

    def __init__(self, scope: Scope, rows=None):
        self.scope = scope
        self._rows = rows if rows is not None else []
        self.calls: list[tuple[str, object]] = []

    def fetchall(self, statement, params=None):
        self.calls.append((statement, params))
        return self._rows


def _scope(**overrides) -> Scope:
    defaults = dict(user_id="U-1")
    defaults.update(overrides)
    return Scope(**defaults)


# ============================================================== token enforcement
def test_require_scope_token_raises_without_literal_token():
    with pytest.raises(ScopeTokenMissing):
        require_scope_token("SELECT * FROM project WHERE entity_id = %(e)s")


def test_require_scope_token_accepts_token_anywhere_in_statement():
    require_scope_token("SELECT * FROM project WHERE {scope}")
    require_scope_token("SELECT * FROM project WHERE x = %(x)s AND {scope} AND y = 1")


def test_query_raises_before_touching_the_session_when_token_missing():
    session = FakeSession(_scope())
    with pytest.raises(ScopeTokenMissing):
        query(session, "SELECT * FROM project WHERE entity_id = %(e)s", {"e": "E-1"})
    assert session.calls == [], "fetchall must never be called when the token is missing"


def test_query_runs_when_token_present():
    session = FakeSession(_scope(read_all=True), rows=[(1,)])
    rows = query(session, "SELECT 1 WHERE {scope}")
    assert rows == [(1,)]
    assert len(session.calls) == 1


# ============================================================== compile_scope: None vs empty
def test_none_dimension_is_unrestricted_no_clause_contributed():
    scope = _scope(entity_ids=None)
    predicate, params = compile_scope(scope, {"entity": "entity_id"})
    assert predicate == "TRUE"
    assert params == {}


def test_empty_frozenset_means_nothing_not_unrestricted():
    scope = _scope(entity_ids=frozenset())
    predicate, params = compile_scope(scope, {"entity": "entity_id"})
    assert predicate == "FALSE"
    assert params == {}


def test_empty_frozenset_on_one_dimension_overrides_other_populated_dimensions():
    """An empty set on any single dimension must reject every row outright --
    it must never be diluted by an OR with another, non-empty dimension."""
    scope = _scope(entity_ids=frozenset(), plant_ids=frozenset({"P-1"}))
    predicate, params = compile_scope(
        scope, {"entity": "entity_id", "plant": "plant_id"})
    assert predicate == "FALSE"
    assert params == {}


def test_populated_set_compiles_to_any_clause_with_sorted_params():
    scope = _scope(entity_ids=frozenset({"E-2", "E-1"}))
    predicate, params = compile_scope(scope, {"entity": "entity_id"})
    assert "entity_id = ANY(" in predicate
    (only_param_value,) = params.values()
    assert only_param_value == ["E-1", "E-2"]  # sorted, for deterministic SQL/tests


def test_read_all_short_circuits_to_true_regardless_of_other_fields():
    scope = _scope(entity_ids=frozenset(), plant_ids=frozenset({"P-1"}), read_all=True)
    predicate, params = compile_scope(scope, {"entity": "entity_id", "plant": "plant_id"})
    assert predicate == "TRUE"
    assert params == {}


def test_no_columns_declared_is_REFUSED_when_the_scope_is_restrictive():
    """INVERTED 2026-08-31 after adversarial review. See tests/ADAPTATIONS.md.

    This previously asserted that omitting `columns=` yields "TRUE" -- and so
    locked in the exact hazard the module exists to prevent. A user scoped to
    one entity, run through a query with no mapping, read every entity's rows;
    a user scoped to NOTHING (`frozenset()`) likewise got TRUE rather than
    FALSE, turning "no grants" into "all rows".

    A restriction the query cannot express must fail loudly. Waiving one is
    still possible, but only by mapping it to None -- deliberate and visible in
    review, unlike an omission.
    """
    scope = _scope(entity_ids=frozenset())  # "nothing"
    with pytest.raises(ScopeNotExpressible) as exc:
        compile_scope(scope, columns=None)
    assert "entity" in str(exc.value)


def test_an_unmapped_restricted_dimension_is_refused_even_when_others_map():
    """The dangerous shape: some dimensions mapped, one silently missing."""
    scope = _scope(entity_ids=frozenset({"ENT-A"}), project_ids=frozenset({"PRJ-1"}))
    with pytest.raises(ScopeNotExpressible):
        compile_scope(scope, columns={"project": "project_id"})


def test_a_dimension_can_be_waived_explicitly_with_none():
    """Waiving must remain possible -- but only as a visible act."""
    scope = _scope(entity_ids=frozenset({"ENT-A"}), project_ids=frozenset({"PRJ-1"}))
    predicate, params = compile_scope(
        scope, columns={"project": "project_id", "entity": None,
                        "plant": None, "location": None})
    assert "project_id" in predicate
    assert "entity" not in predicate
    assert list(params.values()) == [["PRJ-1"]]


def test_an_unrestricted_scope_needs_no_mapping():
    """A scope that restricts nothing is expressible by any query."""
    predicate, params = compile_scope(_scope(), columns=None)
    assert predicate == "TRUE"
    assert params == {}


def test_the_shipped_mappings_express_every_dimension():
    """Both shipped mappings must cover all four dimensions, mapped or waived.

    PROJECT_SCOPE_COLUMNS omitted `project` entirely, so a project-scoped user
    was unfiltered against the project table itself.
    """
    from app.backend.pg.repo import (PROJECT_SCOPE_COLUMNS,
                                     WBS_ELEMENT_SCOPE_COLUMNS, _DIMENSION_FIELDS)
    for name, mapping in (("PROJECT_SCOPE_COLUMNS", PROJECT_SCOPE_COLUMNS),
                          ("WBS_ELEMENT_SCOPE_COLUMNS", WBS_ELEMENT_SCOPE_COLUMNS)):
        missing = sorted(set(_DIMENSION_FIELDS) - set(mapping))
        assert not missing, f"{name} does not express {missing}"


def test_multiple_dimensions_are_anded_together():
    scope = _scope(entity_ids=frozenset({"E-1"}), project_ids=frozenset({"PR-1", "PR-2"}))
    predicate, params = compile_scope(
        scope, {"entity": "entity_id", "project": "w.project_id"})
    assert " AND " in predicate
    assert len(params) == 2


def test_unknown_dimension_name_is_rejected():
    scope = _scope()
    with pytest.raises(ValueError):
        compile_scope(scope, {"bogus": "bogus_id"})


# ============================================================== query(): param handling
def test_query_merges_scope_params_and_substitutes_token():
    scope = _scope(entity_ids=frozenset({"E-1"}))
    session = FakeSession(scope)
    query(session, "SELECT * FROM project WHERE {scope}", None,
          columns={"entity": "entity_id"})
    statement, params = session.calls[0]
    assert "{scope}" not in statement
    assert "entity_id = ANY(" in statement
    assert list(params.values()) == [["E-1"]]


def test_query_defaults_to_session_scope_when_scope_kwarg_omitted():
    scope = _scope(entity_ids=frozenset({"E-9"}))
    session = FakeSession(scope)
    query(session, "SELECT 1 WHERE {scope}", columns={"entity": "entity_id"})
    _statement, params = session.calls[0]
    assert list(params.values()) == [["E-9"]]


def test_query_scope_kwarg_overrides_session_scope():
    session_scope = _scope(entity_ids=frozenset({"E-SESSION"}))
    override_scope = _scope(entity_ids=frozenset({"E-OVERRIDE"}))
    session = FakeSession(session_scope)
    query(session, "SELECT 1 WHERE {scope}", scope=override_scope,
          columns={"entity": "entity_id"})
    _statement, params = session.calls[0]
    assert list(params.values()) == [["E-OVERRIDE"]]


def test_query_caller_params_survive_alongside_scope_params():
    scope = _scope(entity_ids=frozenset({"E-1"}))
    session = FakeSession(scope)
    query(session, "SELECT * FROM project WHERE code = %(code)s AND {scope}",
          {"code": "PRJ-1"}, columns={"entity": "entity_id"})
    _statement, params = session.calls[0]
    assert params["code"] == "PRJ-1"
    assert any(v == ["E-1"] for k, v in params.items() if k != "code")


def test_query_rejects_positional_params():
    session = FakeSession(_scope(read_all=True))
    with pytest.raises(TypeError):
        query(session, "SELECT 1 WHERE {scope}", ("PRJ-1",))


def test_query_one_returns_none_for_out_of_scope_or_missing_row():
    session = FakeSession(_scope(read_all=True), rows=[])
    assert query_one(session, "SELECT 1 WHERE {scope}") is None


def test_query_one_returns_first_row():
    session = FakeSession(_scope(read_all=True), rows=[("a",), ("b",)])
    assert query_one(session, "SELECT 1 WHERE {scope}") == ("a",)
