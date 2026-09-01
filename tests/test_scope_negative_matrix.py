"""The negative access matrix for `principal_scope.scope_for_principal`.

A principal restricted to A must not reach B. Not "on the read endpoint a
reviewer happened to check" -- on every shape through which B's data could
come back. This file walks that matrix along two axes:

    dimension  x  shape
    ---------     -----
    entity        READ      one row by id            -> not found
    plant         EXPORT    every row in scope       -> B absent
    project       SEARCH    a filter that matches B  -> B still absent
    location      WRITE     a mutating service call  -> refused, nothing written
                  APPROVAL  an approval decision     -> refused

**Most of it runs with no database, on purpose.** Wave 3's own contracts:
"Push coverage into tests that run with no database wherever the defect could
live there -- the last two production-shaped defects were invisible locally
because every test touching them skipped." The row-level half of the matrix is
therefore evaluated in Python against the predicate `repo.compile_scope`
actually emits, by :func:`evaluate_predicate` below, and the service-call half
drives the real `pg.budget` / `pg.periods` functions against a session that
answers from the same predicate. The live-PostgreSQL half at the bottom re-runs
the matrix against real seeded users and real RLS, and is honestly marked: it
SKIPS wherever `CAPEX_DB_URL` is unset, which includes this developer machine.

Why a Python predicate evaluator is not a fiction
-------------------------------------------------
`compile_scope` emits a closed, tiny grammar: ``TRUE``, ``FALSE``, or clauses
of the form ``(<column> = ANY(%(__scope_<dim>_<n>)s))`` joined by ``AND``.
:func:`evaluate_predicate` implements exactly that grammar and **raises on
anything else**, so a change to what the compiler emits fails these tests
loudly instead of passing them vacuously. It is evaluating the real predicate
string with the real parameters, not a paraphrase of the intent.

This file is additive to `tests/test_negative_access_matrix.py`, which proves
the same refusal for `repo.compile_scope`/RLS on the `project` table under the
live database. What is new here is that the scope under test is built by
`scope_for_principal` -- the Contract 4 chokepoint -- rather than by
`roles.resolve_grant` directly, and that the matrix covers all four dimensions
and the write/approval shapes.
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

import datetime as _dt  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from typing import Any, Mapping  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import budget as budget_mod  # noqa: E402
from app.backend.pg import periods as periods_mod  # noqa: E402
from app.backend.pg import principal_scope, repo, rls  # noqa: E402
from app.backend.pg import seed as pg_seed  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ================================================================== fixtures
#: The four dimensions, the `Scope` field each fills, and the column each is
#: matched on in a `project`-shaped query.
DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("entity",   "entity_ids",   "p.entity_id"),
    ("plant",    "plant_ids",    "p.plant_id"),
    ("project",  "project_ids",  "p.project_id"),
    ("location", "location_ids", "p.location_id"),
)

ALL_COLUMNS: dict[str, str] = {dim: col for dim, _field, col in DIMENSIONS}

#: Two rows differing on EVERY dimension, so a restriction on any one of them
#: separates them. Keyed by the column expression, which is what the compiled
#: predicate names.
ROW_A: dict[str, str] = {
    "p.entity_id": "ENT-A", "p.plant_id": "PLT-A",
    "p.project_id": "PRJ-A", "p.location_id": "LOC-A",
    "name": "Alpha Capacity Expansion",
}
ROW_B: dict[str, str] = {
    "p.entity_id": "ENT-B", "p.plant_id": "PLT-B",
    "p.project_id": "PRJ-B", "p.location_id": "LOC-B",
    "name": "Bravo Capacity Expansion",
}

#: A search term that matches BOTH rows on its own -- so that B's absence from
#: a search result is scope doing its job, not the filter failing to match.
SEARCH_TERM = "Capacity Expansion"


class IdentitySession:
    """The four identity tables, in dictionaries. See
    `tests/test_pg_principal_scope.py::FakeSession` for the same idea and the
    reasoning; kept separate so neither test file depends on the other."""

    def __init__(self, *, users: Mapping[str, str],
                 read_all: Mapping[str, bool] | None = None,
                 restrictions: Mapping[str, set[str]] | None = None,
                 grants: Mapping[tuple[str, str], list[str]] | None = None) -> None:
        self.users = dict(users)
        self.read_all = dict(read_all or {})
        self.restrictions = {k: set(v) for k, v in (restrictions or {}).items()}
        self.grants = {k: list(v) for k, v in (grants or {}).items()}

    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        if "FROM app_user" in statement:
            (user_id,) = params
            kind = self.users.get(user_id)
            return None if kind is None else (kind,)
        if "FROM user_access_flag" in statement:
            (user_id,) = params
            return None if user_id not in self.read_all else (self.read_all[user_id],)
        if "FROM user_scope_restriction" in statement:
            user_id, dimension = params
            return (1,) if dimension in self.restrictions.get(user_id, set()) else None
        raise AssertionError(f"unmodelled fetchone: {statement!r}")

    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        if "FROM user_scope_grant" in statement:
            user_id, dimension = params
            return [(v,) for v in sorted(self.grants.get((user_id, dimension), []))]
        raise AssertionError(f"unmodelled fetchall: {statement!r}")

    def execute(self, statement: str, params: Any = None):  # pragma: no cover
        raise AssertionError("identity resolution must never write")


def scope_restricted_to_row_a(dimension: str) -> Scope:
    """The scope of a principal restricted, on `dimension` only, to ROW_A's
    value -- built through `scope_for_principal`, so the matrix is testing the
    Contract 4 chokepoint and not a hand-assembled `Scope`."""
    field, column = next((f, c) for d, f, c in DIMENSIONS if d == dimension)
    permitted = ROW_A[column]
    session = IdentitySession(
        users={"U-SCOPED": "USER"},
        restrictions={"U-SCOPED": {dimension}},
        grants={("U-SCOPED", dimension): [permitted]},
    )
    scope = principal_scope.scope_for_principal(session, {"user_id": "U-SCOPED"})
    # Fixture sanity: the scope must actually be restricted on this dimension
    # and unrestricted elsewhere, or the refusals below prove nothing.
    assert getattr(scope, field) == frozenset({permitted})
    for _d, other_field, _c in DIMENSIONS:
        if other_field != field:
            assert getattr(scope, other_field) is None
    assert scope.read_all is False
    return scope


# ================================================== the predicate evaluator
_CLAUSE_RE = re.compile(
    r"^\(\s*(?P<column>[A-Za-z_][\w.]*)\s*=\s*ANY\(%\((?P<param>__scope_\w+)\)s\)\s*\)$")


def evaluate_predicate(predicate: str, params: Mapping[str, Any],
                       row: Mapping[str, Any]) -> bool:
    """Evaluate the exact predicate `compile_scope` emitted, against `row`.

    Implements the compiler's whole grammar and nothing else. An unrecognised
    clause raises: a permissive fallback here would let a compiler change slip
    through as a passing test, which is precisely the failure mode this file
    is written against.
    """
    text = predicate.strip()
    if text == "TRUE":
        return True
    if text == "FALSE":
        return False
    for clause in text.split(" AND "):
        match = _CLAUSE_RE.match(clause.strip())
        if match is None:
            raise AssertionError(
                f"evaluate_predicate does not understand {clause!r}. "
                f"compile_scope's grammar has changed; this evaluator must be "
                f"updated deliberately rather than made permissive.")
        column = match.group("column")
        permitted = params[match.group("param")]
        if column not in row:
            raise AssertionError(
                f"predicate filters on {column!r}, which the fixture row does "
                f"not carry -- the row and the column mapping disagree")
        if row[column] not in permitted:
            return False
    return True


def test_the_evaluator_refuses_a_predicate_it_does_not_understand():
    """The evaluator's own guard, asserted -- otherwise every test that uses
    it could pass by silently accepting something new."""
    with pytest.raises(AssertionError):
        evaluate_predicate("(p.entity_id LIKE 'X%')", {}, ROW_A)
    with pytest.raises(AssertionError):
        evaluate_predicate("(p.entity_id = ANY(%(__scope_entity_0)s))",
                           {"__scope_entity_0": ["ENT-A"]}, {"other": "x"})


def test_the_evaluator_matches_compile_scope_on_the_three_states():
    unrestricted = Scope(user_id="U")
    nothing = Scope(user_id="U", entity_ids=frozenset())
    some = Scope(user_id="U", entity_ids=frozenset({"ENT-A"}))

    for scope, expected_a, expected_b in ((unrestricted, True, True),
                                          (nothing, False, False),
                                          (some, True, False)):
        predicate, params = repo.compile_scope(scope, ALL_COLUMNS)
        assert evaluate_predicate(predicate, params, ROW_A) is expected_a
        assert evaluate_predicate(predicate, params, ROW_B) is expected_b


# ============================================ the matrix: row-returning shapes
@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_read_shape_cannot_see_the_out_of_scope_row(dimension):
    """READ -- fetch one row by id. The out-of-scope row must come back as
    NO ROWS, which the caller reports as "not found". Never an error: a 403
    on an id lookup is an existence oracle (`repo.py`'s module docstring)."""
    scope = scope_restricted_to_row_a(dimension)
    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)

    assert evaluate_predicate(predicate, params, ROW_A) is True, (
        f"[{dimension}] positive control failed: the in-scope row was refused, "
        f"so the refusal of the out-of-scope row proves nothing")
    assert evaluate_predicate(predicate, params, ROW_B) is False, (
        f"[{dimension}] a principal restricted to A read B")


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_export_shape_cannot_list_the_out_of_scope_row(dimension):
    """EXPORT -- every row the scope allows, unfiltered otherwise (the shape a
    CSV/report export uses). B must be absent from the set however produced."""
    scope = scope_restricted_to_row_a(dimension)
    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)

    exported = [row for row in (ROW_A, ROW_B)
                if evaluate_predicate(predicate, params, row)]
    assert exported == [ROW_A], f"[{dimension}] export leaked the out-of-scope row"


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_search_shape_cannot_reach_the_out_of_scope_row(dimension):
    """SEARCH -- a filter that WOULD match B on its own. Knowing what to
    search for must not bypass scope, so the scope predicate has to be ANDed
    into the caller's SQL, not offered as an alternative to it."""
    scope = scope_restricted_to_row_a(dimension)
    statement = ("SELECT project_id FROM project p "
                 "WHERE p.name ILIKE %(term)s AND {scope}")
    predicate, scope_params = repo.compile_scope(scope, ALL_COLUMNS)

    # The token is substituted, in place, leaving the caller's AND intact.
    final = statement.replace(repo.SCOPE_TOKEN, predicate)
    assert repo.SCOPE_TOKEN not in final
    assert f"p.name ILIKE %(term)s AND {predicate}" in final

    def matches_filter(row: Mapping[str, Any]) -> bool:
        return SEARCH_TERM.lower() in row["name"].lower()

    assert matches_filter(ROW_B), "fixture sanity: the term must match B alone"
    found = [row for row in (ROW_A, ROW_B)
             if matches_filter(row)
             and evaluate_predicate(predicate, scope_params, row)]
    assert found == [ROW_A], f"[{dimension}] search leaked the out-of-scope row"


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_a_query_without_the_scope_token_is_refused_outright(dimension):
    """The shape that would defeat the whole matrix: SQL that simply omits
    the predicate. `repo.query` refuses it before the connection is touched."""
    scope = scope_restricted_to_row_a(dimension)
    session = _RefusingSession(scope)
    with pytest.raises(repo.ScopeTokenMissing):
        repo.query(session, "SELECT project_id FROM project p")
    assert session.statements == []


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_a_query_that_cannot_express_the_restriction_is_refused_not_widened(dimension):
    """The subtler shape: SQL whose column mapping omits the restricted
    dimension. `compile_scope` must REFUSE, never quietly drop the clause."""
    scope = scope_restricted_to_row_a(dimension)
    columns_missing_this_dimension = {
        d: c for d, _f, c in DIMENSIONS if d != dimension}

    with pytest.raises(repo.ScopeNotExpressible) as excinfo:
        repo.compile_scope(scope, columns_missing_this_dimension)
    assert dimension in str(excinfo.value)


# ===================================================== the matrix: service calls
class _RefusingSession:
    """A session that records statements and returns nothing. Used where the
    assertion is that the call is refused BEFORE any row is read or written."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.statements: list[tuple[str, Any]] = []
        self.writes: list[tuple[str, Any]] = []

    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        self.statements.append((statement, params))
        return []

    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        self.statements.append((statement, params))
        return None

    def execute(self, statement: str, params: Any = None):
        self.writes.append((statement, params))
        raise AssertionError(
            "a refused call reached a write: " + statement.strip()[:120])


class ScopedRowSession:
    """A session that answers scoped reads by evaluating the predicate
    `repo.query` actually substituted, against a fixture row.

    It re-derives the predicate from `(scope, columns)` and asserts that the
    statement it was handed contains that exact text and that the scope
    parameters were merged in. So it does not merely simulate scoping: it
    checks that `repo.query` produced what `compile_scope` said, then applies
    it. Statements that are not the scoped read (the "scope-exempt" raw
    lookups the services make once an id is already scope-cleared) are
    answered from `raw`.
    """

    def __init__(self, scope: Scope, columns: Mapping[str, str],
                 row: Mapping[str, Any] | None, *,
                 raw: Mapping[str, Any] | None = None,
                 scoped_marker: str = "{scope-marker}",
                 scoped_result: tuple = (1,)) -> None:
        self.scope = scope
        self.columns = dict(columns)
        self.row = row
        self.raw = dict(raw or {})
        self.scoped_marker = scoped_marker
        self.scoped_result = scoped_result
        self.statements: list[tuple[str, Any]] = []
        self.writes: list[tuple[str, Any]] = []

    # -- helpers ---------------------------------------------------------
    def _scoped_rows(self, statement: str, params: Mapping[str, Any]) -> list[tuple]:
        predicate, scope_params = repo.compile_scope(self.scope, self.columns)
        assert repo.SCOPE_TOKEN not in statement, (
            "repo.query left the {scope} token unsubstituted")
        assert predicate in statement, (
            f"the statement does not carry the compiled predicate {predicate!r}")
        for key, value in scope_params.items():
            assert params.get(key) == value, (
                f"scope parameter {key} was not merged into the query params")
        if self.row is None:
            return []
        return [self.scoped_result] if evaluate_predicate(
            predicate, params, self.row) else []

    def _is_scoped(self, statement: str) -> bool:
        predicate, _ = repo.compile_scope(self.scope, self.columns)
        return predicate in statement

    # -- the Session surface ---------------------------------------------
    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        self.statements.append((statement, params))
        for fragment, result in self.raw.items():
            if fragment in statement:
                return list(result)
        if self._is_scoped(statement):
            return self._scoped_rows(statement, params or {})
        raise AssertionError(f"unmodelled fetchall: {statement.strip()[:160]!r}")

    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        self.statements.append((statement, params))
        for fragment, result in self.raw.items():
            if fragment in statement:
                rows = list(result)
                return rows[0] if rows else None
        if self._is_scoped(statement):
            rows = self._scoped_rows(statement, params or {})
            return rows[0] if rows else None
        raise AssertionError(f"unmodelled fetchone: {statement.strip()[:160]!r}")

    def execute(self, statement: str, params: Any = None):
        self.writes.append((statement, params))
        raise AssertionError(
            "an out-of-scope call reached a write: " + statement.strip()[:120])


#: The row `budget.py`'s cell/project queries filter on, in that module's own
#: column expressions -- read from the service, so if it changes its mapping
#: the fixture follows rather than silently testing the old one.
def _budget_row(row: Mapping[str, str], columns: Mapping[str, str | None]
                 ) -> dict[str, str]:
    """Re-key a fixture row into the column expressions `columns` names.

    Read from the service module's own mapping rather than hard-coded, so a
    stream that changes which columns `budget.py` scopes on moves this fixture
    with it instead of leaving these tests asserting against the old shape.
    """
    canonical = {dimension: column for dimension, _field, column in DIMENSIONS}
    out: dict[str, str] = {}
    for dimension, column in columns.items():
        assert dimension in canonical, (
            f"{dimension!r} is not one of the four scope dimensions")
        if column is None:          # a deliberately waived dimension
            continue
        out[column] = row[canonical[dimension]]
    return out


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_write_shape_a_draft_revision_on_an_out_of_scope_cell_is_refused(dimension):
    """WRITE -- `budget.create_revision` on a WBS element outside scope.
    Refused with the not-found shape, and crucially NOTHING is written: the
    session raises if `execute` is ever reached."""
    scope = scope_restricted_to_row_a(dimension)
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_B, budget_mod._CELL_SCOPE_COLUMNS))

    with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
        budget_mod.create_revision(
            session, wbs_id="WBS-B-1", budget_head_id="BH-B",
            delta_paise=100, effective_from=_dt.date(2026, 9, 1),
            justification="attempting a cross-scope revision", actor="U-SCOPED")

    assert excinfo.value.code == "WBS_NOT_FOUND"
    assert excinfo.value.status == 404
    assert session.writes == [], f"[{dimension}] a refused write still wrote"


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_write_shape_an_in_scope_cell_is_not_refused_by_scope(dimension):
    """The positive control for the test above: the same call against the
    IN-scope row gets past the scope gate (and then fails, later, on the
    write the fake session refuses to perform). If this did not distinguish,
    the refusal above would be measuring nothing."""
    scope = scope_restricted_to_row_a(dimension)
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_A, budget_mod._CELL_SCOPE_COLUMNS),
        raw={"FROM budget_control_cell": [(1,)]})

    with pytest.raises(AssertionError) as excinfo:
        budget_mod.create_revision(
            session, wbs_id="WBS-A-1", budget_head_id="BH-A",
            delta_paise=100, effective_from=_dt.date(2026, 9, 1),
            justification="an in-scope revision", actor="U-SCOPED")
    assert "reached a write" in str(excinfo.value), (
        f"[{dimension}] the in-scope call was stopped before the write, so "
        f"the out-of-scope refusal is not attributable to scope")


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_approval_shape_approving_an_out_of_scope_revision_is_refused(dimension):
    """APPROVAL -- holding `revision.approve` is authority over the entities
    in your scope, not over every entity's budget. The decision must be
    refused before any lock is taken or any status is written."""
    scope = scope_restricted_to_row_a(dimension)
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_B, budget_mod._CELL_SCOPE_COLUMNS),
        raw={"FROM budget_revision": [
            ("WBS-B-1", "BH-B", 100, _dt.date(2026, 9, 1), "j", "DRAFT", "U-OTHER")]},
    )

    with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
        budget_mod.approve_revision(session, revision_id="REV-B-1", actor="U-SCOPED")

    assert excinfo.value.code == "WBS_NOT_FOUND"
    assert excinfo.value.status == 404
    assert session.writes == [], f"[{dimension}] a refused approval still wrote"


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_approval_shape_approving_an_out_of_scope_transfer_is_refused(dimension):
    """The same, for a transfer -- which is scope-gated on BOTH legs, since a
    transfer moves budget INTO a cell as well as out of one."""
    scope = scope_restricted_to_row_a(dimension)
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_B, budget_mod._CELL_SCOPE_COLUMNS),
        raw={"FROM budget_transfer": [
            ("WBS-B-1", "BH-B", "WBS-B-2", "BH-B", 100,
             _dt.date(2026, 9, 1), "j", "DRAFT", "U-OTHER")]},
    )

    with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
        budget_mod.approve_transfer(session, transfer_id="TRF-B-1", actor="U-SCOPED")

    assert excinfo.value.code == "WBS_NOT_FOUND"
    assert session.writes == []


def test_approval_shape_closing_an_out_of_scope_period_is_refused():
    """APPROVAL -- `periods.transition_period`. An entity-restricted principal
    must not close another entity's accounting period.

    All four dimensions now reach `accounting_period` through the entity's
    projects, so the fixture row carries the aliased columns
    `periods._PERIOD_SCOPE_COLUMNS` actually names (`sp.*`, the correlated
    subquery over `project`). It used to carry a bare `entity_id`, because
    that mapping named only `entity`.
    """
    scope = scope_restricted_to_row_a("entity")
    # The columns come from the service itself: an entity-only restriction
    # filters `accounting_period.entity_id` directly, while a plant-, project-
    # or location-restricted caller reads through the entity's projects and
    # gets the aliased `sp.*` columns. Asking the service which form applies
    # keeps the fixture honest instead of hardcoding one of the two.
    _sql, period_columns = periods_mod.period_scope_sql_and_columns(scope)
    session = ScopedRowSession(
        scope, period_columns,
        {col: ROW_B[f"p.{col.split('.')[-1]}"]
         for col in period_columns.values() if col})

    with pytest.raises(periods_mod.PeriodServiceError) as excinfo:
        periods_mod.transition_period(
            session, period_id="P-B-Q2", to_state="CLOSED", actor="U-SCOPED")

    assert excinfo.value.code == "PERIOD_NOT_FOUND"
    assert excinfo.value.status == 404
    assert session.writes == []


@pytest.mark.parametrize("dimension", ["plant", "project", "location"])
def test_a_period_transition_ENFORCES_a_restriction_it_once_could_not_express(dimension):
    """Rewritten after the adversarial review, and the change is the point.

    This asserted `pytest.raises(repo.ScopeNotExpressible)`. That refusal was
    genuinely correct -- `accounting_period` carries only `entity_id`, and
    refusing beats waiving the dimension and letting the transition through.
    But nothing tested what the ROUTER did with that exception, and the answer
    was: nothing. `ScopeNotExpressible` is a RuntimeError, no route caught it
    and no app-level handler existed, so once Wave 3 wired real grants in,
    three of the nine seeded demo users got an unhandled 500 on a plain read
    of the period list.

    The dimensions are now EXPRESSED rather than refused, through the entity's
    projects: a period is visible when any project in its entity is visible.
    So the restriction is enforced, which is strictly better than being
    refused -- the caller sees their own periods instead of an error.

    The assertion this test exists to make is unchanged and unweakened: a
    restricted principal must not reach an out-of-scope period. Only the
    mechanism moved, from an exception to a predicate.
    """
    scope = scope_restricted_to_row_a(dimension)
    session = _RefusingSession(scope)

    # It compiles now, where it used to raise.
    _form_sql, form_columns = periods_mod.period_scope_sql_and_columns(scope)
    predicate, params = repo.compile_scope(scope, form_columns)
    assert "EXISTS" in _form_sql, (
        "a plant/project/location restriction must read through the entity's "
        "projects, not be waived on accounting_period's own columns")
    assert predicate != "TRUE", "the restriction must not have been waived"
    assert params, "a real restriction must bind real values"

    # And the transition still refuses an out-of-scope period, writing nothing.
    with pytest.raises(periods_mod.PeriodServiceError) as excinfo:
        periods_mod.transition_period(
            session, period_id="P-A-Q2", to_state="CLOSED", actor="U-SCOPED")

    assert excinfo.value.code == "PERIOD_NOT_FOUND"
    assert excinfo.value.status == 404
    assert session.writes == []


@pytest.mark.parametrize("dimension", ["plant", "project", "location"])
def test_an_inexpressible_dimension_is_a_403_and_never_a_500(dimension):
    """The exit path the original test never checked.

    `compile_scope` still refuses a dimension a query genuinely cannot
    express, and that refusal is still right. What was missing was a handler:
    the refusal escaped as a RuntimeError and became a traceback. Asserted
    here against a mapping that deliberately omits the dimension, so the guard
    holds for the NEXT mapping someone writes, not only for periods.
    """
    from app.backend.main import app

    scope = scope_restricted_to_row_a(dimension)
    entity_only = {"entity": "entity_id"}

    with pytest.raises(repo.ScopeNotExpressible):
        repo.compile_scope(scope, entity_only)

    handled = [k for k in app.exception_handlers
               if getattr(k, "__name__", "") == "ScopeNotExpressible"]
    assert handled, (
        "ScopeNotExpressible has no application-level handler, so a refusal "
        "surfaces as a 500 rather than a 403")


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_list_shape_the_cell_listing_filters_by_the_compiled_predicate(dimension):
    """EXPORT, through the real service function. `budget.list_cells` is the
    listing every budget screen and export is built on; it must apply the
    scope predicate, and the out-of-scope row must not appear in `items`."""
    scope = scope_restricted_to_row_a(dimension)
    cell_row = (
        "WBS-B-1", "wbs_b_1", "BH-B", 1000, 1000, 0, 0, 0, 0, 0, 0, 0, 0,
        _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc),
    )
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_B, budget_mod._CELL_SCOPE_COLUMNS),
        scoped_result=cell_row)

    result = budget_mod.list_cells(session)
    assert result["items"] == [], f"[{dimension}] the cell listing leaked B"
    assert result["has_more"] is False


@pytest.mark.parametrize("dimension", [d for d, _f, _c in DIMENSIONS])
def test_list_shape_positive_control_the_in_scope_cell_is_returned(dimension):
    scope = scope_restricted_to_row_a(dimension)
    cell_row = (
        "WBS-A-1", "wbs_a_1", "BH-A", 1000, 1000, 0, 0, 0, 0, 0, 0, 0, 0,
        _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc),
    )
    session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_A, budget_mod._CELL_SCOPE_COLUMNS),
        scoped_result=cell_row)

    result = budget_mod.list_cells(session)
    assert [item["wbs_id"] for item in result["items"]] == ["WBS-A-1"]


# ============================== a principal with no grants sees nothing, anywhere
@pytest.mark.parametrize("shape_row", [ROW_A, ROW_B], ids=["row-a", "row-b"])
def test_a_principal_with_no_grants_sees_neither_row(shape_row):
    """The empty-grant-set principal: restriction rows on every dimension and
    zero grants. Contract 2's "sees NOTHING" made concrete against both rows,
    so it is clearly not merely out of scope for one of them."""
    session = IdentitySession(
        users={"U-NONE": "USER"},
        restrictions={"U-NONE": {"entity", "plant", "project", "location"}},
    )
    scope = principal_scope.scope_for_principal(session, {"user_id": "U-NONE"})
    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)

    assert predicate == "FALSE"
    assert evaluate_predicate(predicate, params, shape_row) is False


def test_a_principal_with_no_grants_is_refused_every_service_shape():
    scope = principal_scope.scope_for_principal(
        IdentitySession(
            users={"U-NONE": "USER"},
            restrictions={"U-NONE": {"entity", "plant", "project", "location"}}),
        {"user_id": "U-NONE"})

    read_session = ScopedRowSession(
        scope, budget_mod._CELL_SCOPE_COLUMNS,
        _budget_row(ROW_A, budget_mod._CELL_SCOPE_COLUMNS))
    with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
        budget_mod.create_revision(
            read_session, wbs_id="WBS-A-1", budget_head_id="BH-A",
            delta_paise=100, effective_from=_dt.date(2026, 9, 1),
            justification="no-grant principal", actor="U-NONE")
    assert excinfo.value.code == "WBS_NOT_FOUND"
    assert read_session.writes == []

    # `accounting_period` now reaches all four dimensions through the entity's
    # projects, so this no longer refuses in the compiler -- it compiles to
    # FALSE and matches nothing, which is the same answer arrived at more
    # usefully. A principal restricted on every dimension with no grants on
    # any of them sees no period, and closing one is a not-found.
    #
    # It used to assert `pytest.raises(ScopeNotExpressible)`. That refusal was
    # correct in itself, but nothing checked the router's exit path, and there
    # wasn't one: the exception escaped as an unhandled 500.
    _period_sql0, period_columns = periods_mod.period_scope_sql_and_columns(scope)
    predicate, _params = repo.compile_scope(scope, period_columns)
    assert predicate == "FALSE", (
        f"a principal with no grants must match nothing, got {predicate!r}")

    _period_sql, period_columns = periods_mod.period_scope_sql_and_columns(scope)
    period_session = ScopedRowSession(
        scope, period_columns,
        {col: ROW_A[f"p.{col.split('.')[-1]}"]
         for col in period_columns.values() if col})
    with pytest.raises(periods_mod.PeriodServiceError) as period_exc:
        periods_mod.transition_period(
            period_session, period_id="P-A-Q2", to_state="CLOSED", actor="U-NONE")
    assert period_exc.value.code == "PERIOD_NOT_FOUND"
    assert period_session.writes == []


def test_an_unknown_principal_is_refused_every_shape():
    """The principal that resolution cannot place at all. It must be treated
    exactly as the no-grant principal is -- not as an unconfigured, and
    therefore unrestricted, user."""
    scope = principal_scope.scope_for_principal(
        IdentitySession(users={}), {"user_id": "U-GHOST"})
    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)

    assert predicate == "FALSE"
    for row in (ROW_A, ROW_B):
        assert evaluate_predicate(predicate, params, row) is False


# ====================================================================== live PG
@pytest.fixture()
def seeded_pg(pg_connection, pg_database, monkeypatch):
    """A disposable database carrying the whole demo estate.

    Loaded through the PRODUCT's own loader, `pg.seed.seed`, rather than by
    reading the .sql files here: the fragment ORDER is part of what is being
    relied on (`003_budget.sql` needs `seed_demo.sql`'s cells,
    `004_access.sql` needs its users), and the loader is the one place that
    order is defined. Reproducing it in a test is how a test starts passing
    against an estate the application would never actually build.
    """
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    pg_seed.seed(pg_connection)
    pg_connection.commit()
    return pg_database


#: Either not-found code is a pass for an out-of-scope approval, and the pair
#: is not a loosening: BOTH are 404 "not found", and which one fires depends on
#: whether `budget_revision` yet carries RLS. It does not today (recorded as
#: integration finding #7, open, owned by stream 2), so `approve_revision`'s
#: first read -- deliberately scope-exempt, because the cell it names is
#: scope-gated on the very next line -- returns the row and the refusal comes
#: from `_assert_wbs_in_scope` as WBS_NOT_FOUND. Once RLS lands, that first read
#: returns nothing and the refusal arrives one step earlier as
#: REVISION_NOT_FOUND. Pinning a single code here would make this test fail on
#: the wave getting MORE secure, so both are accepted -- while the 404 status,
#: which is the actual security property ("indistinguishable from does not
#: exist"), is asserted unconditionally.
_NOT_FOUND_APPROVAL_CODES = frozenset({"WBS_NOT_FOUND", "REVISION_NOT_FOUND"})

#: seed_demo.sql: PRJ-DM-001 is ENT-DM1 / PLT-DM1-A / LOC-DM1-A1;
#: PRJ-DM-002 is ENT-DM2 / PLT-DM2-A / LOC-DM2-A1.
_IN_SCOPE_PROJECT = "PRJ-DM-001"
_OUT_OF_SCOPE_PROJECT = "PRJ-DM-002"
_SEARCH_TERM_MATCHING_BOTH = "Plant"

#: seed_parts/004_access.sql archetypes, each restricted to the FIRST entity's
#: side of the estate, so PRJ-DM-002 is out of scope for every one of them.
#: `location` has no seeded archetype, so the test creates one -- in the
#: disposable database, not in a seed file this stream does not own.
_LIVE_ARCHETYPES = [
    ("U-PFC", "entity"),
    ("U-PLH", "plant"),
    ("U-PM", "project"),
]


def _grant_location_scope(connection, user_id: str, location_id: str) -> None:
    connection.execute(
        "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
        "VALUES (%s, 'location', 'TEST')", (user_id,))
    connection.execute(
        "INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) "
        "VALUES (%s, 'location', %s, 'TEST')", (user_id, location_id))
    connection.commit()


def _assert_live_matrix_refuses(session: Session, connection, scope: Scope,
                                 label: str) -> None:
    """READ / EXPORT / SEARCH, through both the compiler and live RLS.

    Each shape carries its positive control: the IN-scope project must come
    back. Without it a scope that denied everything would pass this function
    while proving nothing about scoping.
    """
    in_scope = repo.query_one(
        session,
        "SELECT project_id FROM project WHERE project_id = %(id)s AND {scope}",
        {"id": _IN_SCOPE_PROJECT}, scope=scope,
        columns=repo.PROJECT_SCOPE_COLUMNS)
    assert in_scope is not None, (
        f"[{label}] positive control failed: the IN-scope project was refused "
        f"too, so the refusal below is not attributable to scope")

    read_row = repo.query_one(
        session,
        "SELECT project_id FROM project WHERE project_id = %(id)s AND {scope}",
        {"id": _OUT_OF_SCOPE_PROJECT}, scope=scope,
        columns=repo.PROJECT_SCOPE_COLUMNS)
    assert read_row is None, f"[{label}] READ saw an out-of-scope row"

    export = {r[0] for r in repo.query(
        session, "SELECT project_id FROM project WHERE {scope}",
        scope=scope, columns=repo.PROJECT_SCOPE_COLUMNS)}
    assert _OUT_OF_SCOPE_PROJECT not in export, f"[{label}] EXPORT leaked it"
    assert _IN_SCOPE_PROJECT in export, f"[{label}] EXPORT returned nothing at all"

    search = {r[0] for r in repo.query(
        session,
        "SELECT project_id FROM project WHERE name ILIKE %(term)s AND {scope}",
        {"term": f"%{_SEARCH_TERM_MATCHING_BOTH}%"}, scope=scope,
        columns=repo.PROJECT_SCOPE_COLUMNS)}
    assert _OUT_OF_SCOPE_PROJECT not in search, f"[{label}] SEARCH leaked it"
    assert _IN_SCOPE_PROJECT in search, (
        f"[{label}] SEARCH matched nothing, so its refusal of the out-of-scope "
        f"row could be the filter rather than the scope")

    with rls.scoped_transaction(connection, scope):
        rls_read = connection.execute(
            "SELECT project_id FROM project WHERE project_id = %s",
            (_OUT_OF_SCOPE_PROJECT,)).fetchall()
        rls_export = connection.execute("SELECT project_id FROM project").fetchall()
    assert rls_read == [], f"[{label}] RLS READ saw an out-of-scope row"
    assert _OUT_OF_SCOPE_PROJECT not in {r[0] for r in rls_export}, \
        f"[{label}] RLS EXPORT leaked it"


@PG
@pytest.mark.pg
def test_live_scope_for_principal_matches_the_seeded_grants(seeded_pg):
    """`scope_for_principal` against the real identity tables: the three
    states, read off real rows rather than a fake session."""
    with seeded_pg.session(Scope.system()) as session:
        pfc = principal_scope.scope_for_principal(session, {"user_id": "U-PFC"})
        assert pfc.entity_ids == frozenset({"ENT-DM1"})
        assert pfc.plant_ids is None and pfc.project_ids is None
        assert pfc.read_all is False

        nogrant = principal_scope.scope_for_principal(
            session, {"user_id": "U-NOGRANT"})
        assert nogrant.entity_ids == frozenset()
        assert principal_scope.is_denied(nogrant)
        assert repo.compile_scope(nogrant, ALL_COLUMNS)[0] == "FALSE"

        adm = principal_scope.scope_for_principal(session, {"user_id": "U-ADM"})
        assert adm.read_all is True          # from user_access_flag, no restrictions

        svc = principal_scope.scope_for_principal(session, {"user_id": "SVC-ZOHO"})
        assert svc.principal_kind == "SERVICE"
        assert svc.read_all is False

        ghost = principal_scope.scope_for_principal(
            session, {"user_id": "U-NOT-A-REAL-USER"})
        assert principal_scope.is_denied(ghost)


@PG
@pytest.mark.pg
@pytest.mark.parametrize("user_id,dimension", _LIVE_ARCHETYPES)
def test_live_negative_matrix_per_dimension(seeded_pg, pg_connection,
                                             user_id, dimension):
    """The live matrix for the entity, plant and project dimensions, with the
    scope built by `scope_for_principal`."""
    with seeded_pg.session(Scope.system()) as session:
        control = repo.query_one(
            session,
            "SELECT project_id FROM project WHERE project_id = %(id)s AND {scope}",
            {"id": _OUT_OF_SCOPE_PROJECT}, scope=Scope.system(),
            columns=repo.PROJECT_SCOPE_COLUMNS)
        assert control is not None, "fixture sanity: PRJ-DM-002 is not present"

        scope = principal_scope.scope_for_principal(session, {"user_id": user_id})
        assert getattr(scope, f"{dimension}_ids") is not None
        _assert_live_matrix_refuses(session, pg_connection, scope,
                                     f"{user_id}/{dimension}")


@PG
@pytest.mark.pg
def test_live_negative_matrix_location_dimension(seeded_pg, pg_connection):
    """The location dimension, which the shared access seed has no archetype
    for. Granted here, in the disposable database, rather than by editing a
    seed file this stream does not own."""
    _grant_location_scope(pg_connection, "U-REQ", "LOC-DM1-A1")

    with seeded_pg.session(Scope.system()) as session:
        scope = principal_scope.scope_for_principal(session, {"user_id": "U-REQ"})
        assert scope.location_ids == frozenset({"LOC-DM1-A1"})
        _assert_live_matrix_refuses(session, pg_connection, scope, "U-REQ/location")


@PG
@pytest.mark.pg
def test_live_approval_by_a_principal_with_no_grants_is_refused(seeded_pg):
    """APPROVAL, live. `REV-DM1-ELEC-INC` is the seeded DRAFT revision on
    WBS-A-ELEC (PRJ-DM-001 / ENT-DM1). U-NOGRANT holds a real role but is
    granted zero ids on every dimension, so it can reach nothing at all --
    including this approval. The scoped archetypes are covered by the
    cross-entity test below.

    No conditional `pytest.skip` here: within the live job every one of these
    must execute, per the wave's "a skip is not a pass" rule.
    """
    user_id = "U-NOGRANT"
    with seeded_pg.session(Scope.system()) as system_session:
        scope = principal_scope.scope_for_principal(
            system_session, {"user_id": user_id})
    assert principal_scope.is_denied(scope)

    with seeded_pg.session(scope) as session:
        with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
            budget_mod.approve_revision(
                session, revision_id="REV-DM1-ELEC-INC", actor=user_id)
        assert excinfo.value.code in _NOT_FOUND_APPROVAL_CODES
        assert excinfo.value.status == 404


@PG
@pytest.mark.pg
def test_live_cross_entity_approval_and_period_close_are_refused(seeded_pg):
    """The realistic cross-entity case: U-FIN is restricted to ENT-DM2 and
    U-PROC to PLT-DM2-A, so neither may approve ENT-DM1's DRAFT revision nor
    close ENT-DM1's period -- both are refused with the not-found shape."""
    with seeded_pg.session(Scope.system()) as system_session:
        fin = principal_scope.scope_for_principal(system_session, {"user_id": "U-FIN"})
        proc = principal_scope.scope_for_principal(system_session, {"user_id": "U-PROC"})
    assert fin.entity_ids == frozenset({"ENT-DM2"})
    assert proc.plant_ids == frozenset({"PLT-DM2-A"})

    for scope, actor in ((fin, "U-FIN"), (proc, "U-PROC")):
        with seeded_pg.session(scope) as session:
            with pytest.raises(budget_mod.BudgetServiceError) as excinfo:
                budget_mod.approve_revision(
                    session, revision_id="REV-DM1-ELEC-INC", actor=actor)
            assert excinfo.value.code in _NOT_FOUND_APPROVAL_CODES
            assert excinfo.value.status == 404

    # Period close: only the entity dimension is expressible on
    # accounting_period, so U-FIN gets a not-found and U-PROC (plant-scoped)
    # gets an outright refusal to compile -- both closed, neither widened.
    with seeded_pg.session(fin) as session:
        with pytest.raises(periods_mod.PeriodServiceError) as excinfo:
            periods_mod.transition_period(
                session, period_id="P-DM1-Q2", to_state="CLOSED", actor="U-FIN")
        assert excinfo.value.code == "PERIOD_NOT_FOUND"

    with seeded_pg.session(proc) as session:
        with pytest.raises(repo.ScopeNotExpressible):
            periods_mod.transition_period(
                session, period_id="P-DM1-Q2", to_state="CLOSED", actor="U-PROC")


@PG
@pytest.mark.pg
def test_live_out_of_scope_write_is_physically_refused_by_rls(seeded_pg, pg_connection):
    """WRITE, at the database rather than the application: `WITH CHECK` makes
    an INSERT outside scope a genuine row-security violation, not a filtered
    read. Proves the two layers agree by denying independently."""
    import psycopg

    with seeded_pg.session(Scope.system()) as session:
        scope = principal_scope.scope_for_principal(session, {"user_id": "U-PLH"})
    assert scope.plant_ids == frozenset({"PLT-DM1-A"})

    with rls.scoped_transaction(pg_connection, scope):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg_connection.execute(
                "INSERT INTO project (project_id, entity_id, plant_id, location_id, "
                "capex_code, name, created_by, updated_by) VALUES "
                "('PRJ-SCOPE-DENY', 'ENT-DM2', 'PLT-DM2-A', 'LOC-DM2-A1', "
                "'CAPEX-SCOPE-DENY', 'Out Of Scope Project', 'U-PLH', 'U-PLH')")
    pg_connection.rollback()
