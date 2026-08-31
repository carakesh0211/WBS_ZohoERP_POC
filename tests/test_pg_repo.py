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
from app.backend.pg.repo import (
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


def test_no_columns_declared_means_no_filter_even_if_scope_is_restrictive():
    """A caller that passes no `columns=` mapping gets no filtering at all --
    this is a statement about what the query shape can express, and callers
    must declare every dimension their target table actually carries."""
    scope = _scope(entity_ids=frozenset())  # would otherwise mean "nothing"
    predicate, params = compile_scope(scope, columns=None)
    assert predicate == "TRUE"
    assert params == {}


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
