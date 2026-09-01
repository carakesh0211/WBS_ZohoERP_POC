"""`app.backend.pg.principal_scope` -- Contract 4's single scope constructor.

**Every test in this file runs with no database.** That is the point. The two
production-shaped defects this wave exists to close were invisible locally
because every test that touched them skipped for want of PostgreSQL, so the
properties that matter here are proven against a fake session that models the
four identity tables in Python dictionaries and runs the REAL
`roles.resolve_scope` over them. Nothing here is mocked away that could hide a
widening: the resolver's own SQL-shaped reads are answered from the fake
tables, so a change to what `resolve_scope` asks for shows up as a fake-session
failure rather than a silently-passing test.

What is proven, and why each matters
------------------------------------
* **The three states survive.** `None` / `frozenset()` / `frozenset({ids})`
  come back distinct. Collapsing the middle into the first turns "no grants"
  into "all rows".
* **Every failure path fails closed.** The failure cases are enumerated in
  :data:`FAILURE_CASES` and each is asserted twice: the returned `Scope` is
  denied, AND `repo.compile_scope` compiles it to the literal ``FALSE``. The
  second assertion is the one that matters -- "denied" is a property of the
  predicate the database will actually run, not of a flag.
* **`read_all` is never synthesised from a role.** A principal carrying every
  whole-estate-looking role name the current routers key on, with no
  `user_access_flag` row, gets `read_all=False`.
* **A SERVICE principal receives no bypass.** It is resolved exactly as a
  human is, and never receives `Scope.system()`.
"""
from __future__ import annotations

import inspect
from typing import Any, Mapping

import pytest

from app.backend.pg import principal_scope, repo, roles as roles_module
from app.backend.pg.engine import Scope
from app.backend.pg.principal_scope import (
    ANONYMOUS_USER_ID, SCOPE_DIMENSION_FIELDS, WILDCARD_LOOKALIKE,
    denied_scope, is_denied, principal_id, resolve_scope_with_reason,
    scope_for_principal,
)

#: A column mapping covering all four dimensions, so `compile_scope` never
#: refuses for want of an expression and every assertion below is about the
#: SCOPE, not about the query shape.
ALL_COLUMNS: dict[str, str] = {
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "project": "p.project_id",
    "location": "p.location_id",
}


# ====================================================================== fake DB
class FakeSession:
    """A `Session` stand-in backed by dictionaries, not a database.

    It answers exactly the reads `roles.resolve_principal_kind` and
    `roles.resolve_scope` issue, matched on a distinctive fragment of each
    statement. An unrecognised statement raises rather than returning `None`:
    a fake that answers "no rows" to a question it did not understand would
    quietly turn a resolver change into "this user has no restrictions",
    i.e. unrestricted, which is the exact failure this suite is here to catch.
    """

    def __init__(self, *, users: Mapping[str, str] | None = None,
                 read_all: Mapping[str, bool] | None = None,
                 restrictions: Mapping[str, set[str]] | None = None,
                 grants: Mapping[tuple[str, str], list[str]] | None = None) -> None:
        self.users = dict(users or {})
        self.read_all = dict(read_all or {})
        self.restrictions = {k: set(v) for k, v in (restrictions or {}).items()}
        self.grants = {k: list(v) for k, v in (grants or {}).items()}
        self.statements: list[tuple[str, Any]] = []

    # -- the Session surface roles.py uses -------------------------------
    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        self.statements.append((statement, params))
        if "FROM app_user" in statement and "principal_kind" in statement:
            (user_id,) = params
            kind = self.users.get(user_id)
            return None if kind is None else (kind,)
        if "FROM user_access_flag" in statement:
            (user_id,) = params
            if user_id not in self.read_all:
                return None
            return (self.read_all[user_id],)
        if "FROM user_scope_restriction" in statement:
            user_id, dimension = params
            return (1,) if dimension in self.restrictions.get(user_id, set()) else None
        raise AssertionError(f"FakeSession got an unmodelled fetchone: {statement!r}")

    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        self.statements.append((statement, params))
        if "FROM user_scope_grant" in statement:
            user_id, dimension = params
            return [(v,) for v in sorted(self.grants.get((user_id, dimension), []))]
        if "FROM role_grant" in statement:
            (user_id,) = params
            return []
        raise AssertionError(f"FakeSession got an unmodelled fetchall: {statement!r}")

    def execute(self, statement: str, params: Any = None):  # pragma: no cover
        raise AssertionError(
            "principal_scope must never write; it attempted: " + statement)


class ExplodingSession(FakeSession):
    """A session whose reads raise, to exercise the resolver-failure paths."""

    def __init__(self, *, fail_on: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on

    def fetchone(self, statement: str, params: Any = None) -> tuple | None:
        if self.fail_on in statement:
            raise RuntimeError("connection reset by peer")
        return super().fetchone(statement, params)

    def fetchall(self, statement: str, params: Any = None) -> list[tuple]:
        if self.fail_on in statement:
            raise RuntimeError("connection reset by peer")
        return super().fetchall(statement, params)


def a_session(**kwargs: Any) -> FakeSession:
    """A session where `U-1` is a known human, plus whatever else is asked for."""
    users = dict(kwargs.pop("users", {}))
    users.setdefault("U-1", "USER")
    return FakeSession(users=users, **kwargs)


# ============================================================ the three states
def test_the_three_states_survive_resolution_distinctly():
    """`None`, `frozenset()` and `frozenset({ids})` come back as three
    different values -- the whole point of Contract 2."""
    session = a_session(
        restrictions={"U-1": {"entity", "plant"}},
        grants={("U-1", "entity"): ["ENT-A", "ENT-B"]},
        # plant: a restriction row and NO grant rows -> restricted to nothing.
        # project / location: no restriction row at all -> unrestricted.
    )
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert scope.entity_ids == frozenset({"ENT-A", "ENT-B"})
    assert scope.plant_ids == frozenset()
    assert scope.project_ids is None
    assert scope.location_ids is None


def test_restricted_to_nothing_is_never_collapsed_into_unrestricted():
    """The single assertion this stream exists for, stated on its own so a
    regression names itself: `frozenset()` must not become `None`."""
    session = a_session(restrictions={"U-1": {"plant"}})
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert scope.plant_ids is not None, (
        "a dimension with a restriction row and zero grants was collapsed to "
        "None (unrestricted) -- 'no grants' has become 'all rows'")
    assert scope.plant_ids == frozenset()


def test_restricted_to_nothing_compiles_to_false_not_true():
    """The state distinction, carried through to the predicate the database
    actually runs -- the only place it has consequences."""
    session = a_session(restrictions={"U-1": {"plant"}})
    scope = scope_for_principal(session, {"user_id": "U-1"})

    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)
    assert predicate == "FALSE"
    assert params == {}


def test_unrestricted_dimension_contributes_no_clause():
    """The positive control: `None` really does mean unrestricted, so the
    failures above are measuring a real distinction rather than a resolver
    that denies everything."""
    session = a_session(
        restrictions={"U-1": {"entity"}},
        grants={("U-1", "entity"): ["ENT-A"]},
    )
    scope = scope_for_principal(session, {"user_id": "U-1"})

    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)
    assert "p.entity_id = ANY" in predicate
    assert "p.plant_id" not in predicate
    assert list(params.values()) == [["ENT-A"]]


def test_grants_are_resolved_not_invented():
    """The ids in the scope come from `user_scope_grant`, and from nowhere
    else -- change the table, the scope changes with it."""
    session = a_session(
        restrictions={"U-1": {"project"}},
        grants={("U-1", "project"): ["PRJ-9", "PRJ-7", "PRJ-9"]},
    )
    scope = scope_for_principal(session, {"user_id": "U-1"})
    assert scope.project_ids == frozenset({"PRJ-7", "PRJ-9"})

    # And the resolver was genuinely consulted, against the real tables.
    asked = " ".join(s for s, _ in session.statements)
    assert "user_scope_restriction" in asked
    assert "user_scope_grant" in asked


def test_resolve_scope_is_the_only_source_of_grants(monkeypatch):
    """`scope_for_principal` delegates to `roles.resolve_scope`; it does not
    reimplement grant reading. Proven by replacing the resolver and watching
    the whole scope change."""
    calls: list[tuple] = []

    def fake_resolve(session, user_id, *, principal_kind="USER"):
        calls.append((user_id, principal_kind))
        return Scope(user_id=user_id, principal_kind=principal_kind,
                     entity_ids=frozenset({"FROM-RESOLVER"}))

    monkeypatch.setattr(roles_module, "resolve_scope", fake_resolve)
    scope = scope_for_principal(a_session(), {"user_id": "U-1"})

    assert calls == [("U-1", "USER")]
    assert scope.entity_ids == frozenset({"FROM-RESOLVER"})


# ================================================== every failure fails closed
def _principal_names_nobody() -> tuple[Any, Mapping[str, Any] | None]:
    return a_session(), {}


def _principal_is_none() -> tuple[Any, Mapping[str, Any] | None]:
    return a_session(), None


def _principal_id_is_blank() -> tuple[Any, Mapping[str, Any] | None]:
    return a_session(), {"user_id": "   "}


def _principal_is_not_a_mapping() -> tuple[Any, Mapping[str, Any] | None]:
    return a_session(), "U-1"          # a bare string, not a principal dict


def _user_unknown_to_app_user() -> tuple[Any, Mapping[str, Any] | None]:
    return a_session(), {"user_id": "U-DOES-NOT-EXIST"}


def _app_user_kind_is_garbage() -> tuple[Any, Mapping[str, Any] | None]:
    return FakeSession(users={"U-1": "ROOT"}), {"user_id": "U-1"}


def _kind_lookup_raises() -> tuple[Any, Mapping[str, Any] | None]:
    return ExplodingSession(fail_on="FROM app_user"), {"user_id": "U-1"}


def _grant_read_raises() -> tuple[Any, Mapping[str, Any] | None]:
    session = ExplodingSession(fail_on="FROM user_scope_grant",
                               users={"U-1": "USER"},
                               restrictions={"U-1": {"entity"}})
    return session, {"user_id": "U-1"}


def _access_flag_read_raises() -> tuple[Any, Mapping[str, Any] | None]:
    session = ExplodingSession(fail_on="FROM user_access_flag",
                               users={"U-1": "USER"})
    return session, {"user_id": "U-1"}


#: name -> a callable returning `(session, principal)` that must fail closed.
FAILURE_CASES = {
    "principal names nobody": _principal_names_nobody,
    "principal is None": _principal_is_none,
    "principal id is blank": _principal_id_is_blank,
    "principal is not a mapping": _principal_is_not_a_mapping,
    "user unknown to app_user": _user_unknown_to_app_user,
    "app_user.principal_kind is garbage": _app_user_kind_is_garbage,
    "principal-kind lookup raises": _kind_lookup_raises,
    "grant read raises": _grant_read_raises,
    "access-flag read raises": _access_flag_read_raises,
}


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_every_failure_path_returns_a_scope_that_sees_nothing(case):
    session, principal = FAILURE_CASES[case]()
    scope = scope_for_principal(session, principal)

    assert is_denied(scope), f"[{case}] did not fail closed: {scope!r}"
    assert scope.read_all is False, f"[{case}] fell back to read_all"
    for field in SCOPE_DIMENSION_FIELDS:
        assert getattr(scope, field) == frozenset(), (
            f"[{case}] left {field} at {getattr(scope, field)!r}; every "
            f"dimension of a denied scope must be frozenset(), never None")


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_every_failure_path_compiles_to_a_false_predicate(case):
    """The assertion with teeth: not "the flag says denied" but "the SQL the
    database will run cannot match a row"."""
    session, principal = FAILURE_CASES[case]()
    scope = scope_for_principal(session, principal)

    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)
    assert predicate == "FALSE", f"[{case}] compiled to {predicate!r}"
    assert params == {}


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_every_failure_path_reports_a_reason(case):
    session, principal = FAILURE_CASES[case]()
    scope, reason = resolve_scope_with_reason(session, principal)

    assert reason, f"[{case}] denied silently, with no reason"
    assert is_denied(scope)


@pytest.mark.parametrize("returned", [
    None,
    {"entity_ids": None},
    "unrestricted",
    42,
    object(),
])
def test_a_resolver_returning_a_non_scope_fails_closed(monkeypatch, returned):
    """`resolve_scope` returning garbage must not be duck-typed into access."""
    monkeypatch.setattr(roles_module, "resolve_scope",
                        lambda *a, **k: returned)
    scope = scope_for_principal(a_session(), {"user_id": "U-1"})

    assert is_denied(scope)
    assert repo.compile_scope(scope, ALL_COLUMNS)[0] == "FALSE"


def test_a_resolver_that_raises_fails_closed(monkeypatch):
    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("the grants table is gone")

    monkeypatch.setattr(roles_module, "resolve_scope", boom)
    scope, reason = resolve_scope_with_reason(a_session(), {"user_id": "U-1"})

    assert is_denied(scope)
    assert "RuntimeError" in reason


def test_a_resolver_answering_about_another_user_fails_closed(monkeypatch):
    """A resolver that returns someone ELSE's grants is not a smaller
    problem than one that returns none: it is another user's access."""
    monkeypatch.setattr(
        roles_module, "resolve_scope",
        lambda *a, **k: Scope(user_id="U-SOMEONE-ELSE",
                              entity_ids=frozenset({"ENT-A"})))
    scope, reason = resolve_scope_with_reason(a_session(), {"user_id": "U-1"})

    assert is_denied(scope)
    assert "U-SOMEONE-ELSE" in reason


def test_a_bare_string_dimension_fails_closed_rather_than_becoming_letters(monkeypatch):
    """`frozenset("ENT-A")` is the set of its CHARACTERS. Accepting a string
    would both discard the real grant and admit every single-character id --
    a widening bug in the shape of a coercion."""
    monkeypatch.setattr(
        roles_module, "resolve_scope",
        lambda *a, **k: Scope(user_id="U-1", entity_ids="ENT-A"))
    scope = scope_for_principal(a_session(), {"user_id": "U-1"})

    assert is_denied(scope)
    assert scope.entity_ids == frozenset()
    assert "E" not in scope.entity_ids


@pytest.mark.parametrize("dimension_value", [
    {"ENT-A", "ENT-B"},
    ["ENT-A", "ENT-B"],
    ("ENT-B", "ENT-A"),
    frozenset({"ENT-A", "ENT-B"}),
])
def test_set_list_and_tuple_dimensions_normalise_without_blurring(
        monkeypatch, dimension_value):
    monkeypatch.setattr(
        roles_module, "resolve_scope",
        lambda *a, **k: Scope(user_id="U-1", entity_ids=dimension_value))
    scope = scope_for_principal(a_session(), {"user_id": "U-1"})

    assert scope.entity_ids == frozenset({"ENT-A", "ENT-B"})
    assert scope.plant_ids is None          # untouched dimensions stay `None`


# ================================================== read_all is never invented
#: Every role name the current routers treat as whole-estate. None of them may
#: buy `read_all` here -- `api/budget.py::_WHOLE_ESTATE_BUDGET_ROLES` and
#: `api/audit.py::_WHOLE_ESTATE_AUDIT_ROLES` do exactly that today, which is
#: what Contract 2 forbids and what this module must not reproduce.
WHOLE_ESTATE_LOOKING_ROLES = [
    "Administrator", "System Administrator", "Auditor", "Internal Auditor",
    "CFO", "Management Approver", "Read-only Management User",
]


@pytest.mark.parametrize("role", WHOLE_ESTATE_LOOKING_ROLES)
def test_read_all_is_never_synthesised_from_a_role_name(role):
    """No `user_access_flag` row -> `read_all` is false, whatever the
    principal claims to be."""
    session = a_session()
    principal = {"user_id": "U-1", "role": role, "roles": [role, "CFO"],
                 "title": role, "read_all": True, "is_admin": True}
    scope = scope_for_principal(session, principal)

    assert scope.read_all is False, (
        f"role {role!r} bought read_all with no user_access_flag row")


@pytest.mark.parametrize("role", WHOLE_ESTATE_LOOKING_ROLES)
def test_a_role_name_never_widens_a_restricted_dimension(role):
    session = a_session(restrictions={"U-1": {"entity", "plant",
                                              "project", "location"}})
    scope = scope_for_principal(
        session, {"user_id": "U-1", "roles": [role]})

    assert is_denied(scope)
    assert repo.compile_scope(scope, ALL_COLUMNS)[0] == "FALSE"


def test_read_all_from_the_access_flag_is_preserved():
    """The positive control for the two tests above: the flag, and only the
    flag, does grant it."""
    session = a_session(read_all={"U-1": True})
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert scope.read_all is True
    assert repo.compile_scope(scope, ALL_COLUMNS)[0] == "TRUE"


def test_read_all_is_cleared_when_restriction_rows_exist():
    """Contract 4: "never returns an unrestricted scope for a principal that
    has restriction rows". `compile_scope` short-circuits to TRUE on
    `read_all` BEFORE examining a dimension, so a read_all flag sitting
    alongside a restriction row is an unrestricted scope in every way that
    matters.
    """
    session = a_session(
        read_all={"U-1": True},
        restrictions={"U-1": {"entity"}},
        grants={("U-1", "entity"): ["ENT-A"]},
    )
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert scope.read_all is False
    assert scope.entity_ids == frozenset({"ENT-A"})
    predicate, params = repo.compile_scope(scope, ALL_COLUMNS)
    assert predicate != "TRUE"
    assert list(params.values()) == [["ENT-A"]]


def test_read_all_is_cleared_even_when_the_restriction_grants_nothing():
    session = a_session(read_all={"U-1": True},
                        restrictions={"U-1": {"project"}})
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert scope.read_all is False
    assert repo.compile_scope(scope, ALL_COLUMNS)[0] == "FALSE"


@pytest.mark.parametrize("truthy", [1, "true", "yes", [1]])
def test_a_non_boolean_read_all_is_not_true(monkeypatch, truthy):
    """`read_all` is a boolean column. A truthy non-boolean reaching here
    means something upstream is wrong, and "wrong" must not read as "grant
    the whole estate"."""
    monkeypatch.setattr(
        roles_module, "resolve_scope",
        lambda *a, **k: Scope(user_id="U-1", read_all=truthy))
    scope = scope_for_principal(a_session(), {"user_id": "U-1"})

    assert scope.read_all is False


# ====================================================== the SERVICE principal
def test_service_principal_is_resolved_exactly_like_a_human():
    """A SERVICE principal gets real grants and no bypass. Its `Scope`
    records `principal_kind='SERVICE'` so RLS and the audit trail can see
    what kind of actor acted -- that label is the ONLY difference."""
    session = FakeSession(
        users={"SVC-1": "SERVICE"},
        restrictions={"SVC-1": {"entity"}},
        grants={("SVC-1", "entity"): ["ENT-A"]},
    )
    scope = scope_for_principal(session, {"user_id": "SVC-1"})

    assert scope.principal_kind == "SERVICE"
    assert scope.entity_ids == frozenset({"ENT-A"})
    assert scope.read_all is False


def test_service_principal_with_no_grants_sees_nothing():
    session = FakeSession(
        users={"SVC-1": "SERVICE"},
        restrictions={"SVC-1": {"entity", "plant", "project", "location"}},
    )
    scope = scope_for_principal(session, {"user_id": "SVC-1"})

    assert scope.principal_kind == "SERVICE"
    assert is_denied(scope)
    assert repo.compile_scope(scope, ALL_COLUMNS)[0] == "FALSE"


def test_service_principal_never_receives_the_system_scope():
    """`Scope.system()` is `read_all=True` and its own docstring says
    "for migrations and start-up checks only. Never for a request.". A
    SERVICE principal arriving at a router IS a request."""
    session = FakeSession(users={"SVC-1": "SERVICE"})
    scope = scope_for_principal(
        session, {"user_id": "SVC-1"}, principal_kind="SERVICE")

    assert scope != Scope.system("SVC-1")
    assert scope.read_all is False


def test_unknown_service_principal_fails_closed():
    session = FakeSession(users={})
    scope = scope_for_principal(
        session, {"user_id": "SVC-GHOST"}, principal_kind="SERVICE")

    assert is_denied(scope)
    assert scope.principal_kind == "SERVICE"


def test_app_user_is_authoritative_for_principal_kind_not_the_caller():
    """The `principal_kind` argument is the caller's ASSERTION. `app_user` is
    the server's own record, and wins -- a caller cannot relabel itself."""
    session = FakeSession(users={"SVC-1": "SERVICE", "U-1": "USER"})

    claimed_user = scope_for_principal(
        session, {"user_id": "SVC-1"}, principal_kind="USER")
    assert claimed_user.principal_kind == "SERVICE"

    claimed_service = scope_for_principal(
        session, {"user_id": "U-1"}, principal_kind="SERVICE")
    assert claimed_service.principal_kind == "USER"


def test_a_principal_kind_claimed_in_the_payload_is_ignored():
    session = FakeSession(users={"U-1": "USER"})
    scope = scope_for_principal(
        session, {"user_id": "U-1", "principal_kind": "SERVICE",
                  "read_all": True})

    assert scope.principal_kind == "USER"
    assert scope.read_all is False


# ============================================ a literal '*' is never a wildcard
def test_a_stored_star_grant_denies_rather_than_widening_or_raising():
    """A pre-Wave-3 `*` row, met at resolution time.

    This test previously asserted `*` survived as an ordinary id matching only
    a row literally named `*`. That was correct at the Wave 3 baseline and is
    wrong now: stream 3 made `Scope` REFUSE the value outright, so the id can
    no longer reach a predicate at all.

    Refusing is the stronger position, but it must not surface as a 500 from
    every route the principal touches. Contract 2 says an unresolvable scope
    fails closed, so the denial is the assertion -- and denial is the only
    safe reading of the row, because `*` meant "unrestricted" to the old RLS
    predicate and "an id matching nothing" to `compile_scope`. Those are
    opposites; migration 007 refuses to guess between them for the same
    reason."""
    session = a_session(
        restrictions={"U-1": {"entity"}},
        grants={("U-1", "entity"): [WILDCARD_LOOKALIKE]},
    )
    scope, reason = resolve_scope_with_reason(session, {"user_id": "U-1"})

    assert is_denied(scope), "a stored '*' must deny, never widen"
    assert reason is not None and "Scope" in reason

    predicate, _params = repo.compile_scope(scope, ALL_COLUMNS)
    assert predicate == "FALSE", (
        f"a denied scope must compile to FALSE, not {predicate!r}")

    # And it must not escape as an exception to the router.
    assert is_denied(scope_for_principal(session, {"user_id": "U-1"}))


def test_a_star_grant_alongside_a_real_id_denies_the_whole_dimension():
    """The mixed case, which is the one that could have been argued either way.

    A dimension holding both `PLT-A` and `*` could plausibly be narrowed to
    just `PLT-A` -- drop the bad value, keep the good one. It is not, and
    should not be: the `*` row is evidence that this grant set was written
    against semantics the system no longer honours, so trusting the rest of it
    is a guess. Deny, and make an operator look."""
    session = a_session(
        restrictions={"U-1": {"plant"}},
        grants={("U-1", "plant"): ["PLT-A", WILDCARD_LOOKALIKE]},
    )
    scope = scope_for_principal(session, {"user_id": "U-1"})

    assert is_denied(scope)
    predicate, _params = repo.compile_scope(scope, ALL_COLUMNS)
    assert predicate == "FALSE"


# ============================================================== helper contracts
@pytest.mark.parametrize("principal,expected", [
    ({"user_id": "U-1"}, "U-1"),
    ({"username": "U-2"}, "U-2"),
    ({"user_id": "U-1", "username": "U-2"}, "U-1"),   # user_id wins
    ({"user_id": "  U-3  "}, "U-3"),
    ({"user_id": ""}, None),
    ({"user_id": None, "username": "U-4"}, "U-4"),
    ({}, None),
    (None, None),
    ("U-5", None),
    (["U-6"], None),
])
def test_principal_id_reads_only_the_declared_keys(principal, expected):
    assert principal_id(principal) == expected


def test_denied_scope_is_denied_and_anonymous_by_default():
    scope = denied_scope()
    assert scope.user_id == ANONYMOUS_USER_ID
    assert is_denied(scope)
    assert repo.compile_scope(scope, ALL_COLUMNS) == ("FALSE", {})


def test_is_denied_is_a_property_of_the_value_not_a_flag():
    assert is_denied(Scope(user_id="U-1", entity_ids=frozenset(),
                           plant_ids=frozenset(), project_ids=frozenset(),
                           location_ids=frozenset()))
    # read_all re-opens everything, so such a scope is NOT denied however
    # empty its dimensions look.
    assert not is_denied(Scope(user_id="U-1", entity_ids=frozenset(),
                               plant_ids=frozenset(), project_ids=frozenset(),
                               location_ids=frozenset(), read_all=True))
    assert not is_denied(Scope(user_id="U-1"))                     # all None
    assert not is_denied(Scope(user_id="U-1", entity_ids=frozenset(),
                               plant_ids=frozenset(), project_ids=frozenset()))


def test_denied_scope_rejects_an_unknown_principal_kind_label():
    assert denied_scope("U-1", "ROOT").principal_kind == "USER"


def test_the_module_never_writes():
    """A scope constructor is a read. The fake session raises on `execute`,
    so this is proven by the resolution succeeding at all -- asserted
    explicitly so the guarantee is named, not incidental."""
    session = a_session(restrictions={"U-1": {"entity"}})
    scope_for_principal(session, {"user_id": "U-1"})
    assert all("INSERT" not in s and "UPDATE" not in s and "DELETE" not in s
               for s, _ in session.statements)


def test_the_module_source_contains_no_widening_literal():
    """A source-level guard against the regression that would be easiest to
    reintroduce and hardest to see: a `read_all=True` fallback, or handing a
    request `Scope.system()`.

    Belt-and-braces beside the behavioural tests above -- those prove today's
    branches, this one refuses tomorrow's.
    """
    source = inspect.getsource(principal_scope)
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    # The docstrings mention both by name; strip the module docstring, which
    # is the only place they legitimately appear.
    body = code.split('"""', 2)[-1]
    assert "read_all=True" not in body, (
        "principal_scope sets read_all=True somewhere; read_all may only ever "
        "be carried over from user_access_flag, and only ever turned off")
    assert "Scope.system" not in body, (
        "principal_scope references Scope.system, which is documented "
        "'never for a request'")
