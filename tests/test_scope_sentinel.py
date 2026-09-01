"""The `*` scope sentinel is retired, and cannot come back.

WHAT WAS WRONG
--------------
`Scope.as_settings()` rendered an unrestricted dimension as the literal
string ``*``, and `capex_dimension_permits` (migration 004) treated a setting
value of ``*`` as "unrestricted". A `user_scope_grant` row whose
`scope_value` was literally ``*`` was therefore stored as a RESTRICTION but
read by RLS as NO restriction -- and the two enforcement layers disagreed:

  * `repo.compile_scope` read ``*`` as an ordinary id, matching nothing;
  * RLS read ``*`` as a wildcard, matching everything.

One row, two opposite answers, the database's being the wider one.

WHAT REPLACES IT
----------------
`docs/WAVE3_CONTRACTS.md` contract 1, frozen: mode and identity are separate
settings, so **no value an id can take means "unrestricted"**. The ambiguity
is not rejected, it is inexpressible.

WHY ALMOST ALL OF THIS RUNS WITHOUT A DATABASE
----------------------------------------------
The defect lived in a pure string-rendering function and in a SQL predicate.
Only the second needs PostgreSQL. Pushing the rest into database-free tests
is deliberate: `docs/WAVE3_CONTRACTS.md` records that the last two
production-shaped defects were invisible locally because every test touching
them skipped. A skip is not a pass. Everything provable without a database is
proved without one, and runs on every machine, every time.

The live tests additionally assert that the SQL function and the Python
mirror below agree case-for-case -- so the mirror cannot quietly drift into
describing a predicate the database does not actually implement.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly. Without this
# the live tests fail at SETUP with "fixture 'pg_connection' not found" -- but
# only where CAPEX_DB_URL is set. Locally they skip, so the missing fixture is
# never resolved and the gap stays invisible until CI.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import itertools  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import repo, roles  # noqa: E402
from app.backend.pg.engine import (  # noqa: E402
    SCOPE_DIMENSIONS, SCOPE_KEYS, InvalidScopeValue, Scope, validate_scope_value,
)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

#: Values no id may ever take, and why each is a MISREAD rather than a miss.
FORBIDDEN_VALUES = ["*", "", "   ", "\t", "A,B"]


# ============================================================ the wire format
def _all_scopes():
    """Every interesting `Scope` shape: each dimension independently
    unrestricted / empty / populated, plus both `read_all` settings."""
    states = [None, frozenset(), frozenset({"ENT-1"}), frozenset({"B", "A"})]
    for combo in itertools.product(states, repeat=len(SCOPE_DIMENSIONS)):
        for read_all in (False, True):
            yield Scope(
                user_id="U-1", principal_kind="USER", read_all=read_all,
                **{f"{d}_ids": v for d, v in zip(SCOPE_DIMENSIONS, combo)})


def test_as_settings_never_emits_the_star_sentinel_for_any_scope():
    """The headline property: no `Scope`, however shaped, renders `*`.

    Exhaustive over every combination of the three per-dimension states and
    both `read_all` values -- 512 scopes -- rather than the three or four a
    hand-written example set would cover. The old code emitted `*` for
    exactly one of those states, so a test that happened to miss it would
    have reported the defect fixed.
    """
    for scope in _all_scopes():
        for key, value in scope.as_settings().items():
            assert value != "*", (
                f"{key} rendered the retired wildcard for {scope!r}")
            assert "*" not in value, (
                f"{key}={value!r} contains the retired wildcard for {scope!r}")


@pytest.mark.parametrize("dimension", SCOPE_DIMENSIONS)
def test_the_three_states_round_trip_to_the_frozen_representation(dimension):
    """`None` -> all/'', `frozenset()` -> none/'', `{ids}` -> list/sorted.

    This is the table in contract 1, asserted literally. Three streams read
    these strings; if this test and the contract ever disagree, the contract
    wins and this test is the thing that must change.
    """
    def settings_for(values):
        return Scope(user_id="U-1", **{f"{dimension}_ids": values}).as_settings()

    unrestricted = settings_for(None)
    assert unrestricted[f"capex.{dimension}_mode"] == "all"
    assert unrestricted[f"capex.{dimension}_ids"] == ""

    nothing = settings_for(frozenset())
    assert nothing[f"capex.{dimension}_mode"] == "none"
    assert nothing[f"capex.{dimension}_ids"] == ""

    listed = settings_for(frozenset({"Z-9", "A-1", "M-5"}))
    assert listed[f"capex.{dimension}_mode"] == "list"
    assert listed[f"capex.{dimension}_ids"] == "A-1,M-5,Z-9", (
        "ids must be sorted and comma-joined -- a stable rendering is what "
        "lets a test, a log line and a cached plan compare equal")


def test_unrestricted_and_empty_are_distinguishable_on_the_wire():
    """The inversion this whole wave exists to close.

    "No grants" and "no restriction" must never render identically. They
    share an ids string (both ''), so the MODE is the only thing telling them
    apart -- which is precisely why the mode is a separate setting.
    """
    for dimension in SCOPE_DIMENSIONS:
        unrestricted = Scope(user_id="U-1", **{f"{dimension}_ids": None}).as_settings()
        nothing = Scope(user_id="U-1", **{f"{dimension}_ids": frozenset()}).as_settings()
        assert unrestricted != nothing
        assert unrestricted[f"capex.{dimension}_ids"] == nothing[f"capex.{dimension}_ids"] == ""
        assert unrestricted[f"capex.{dimension}_mode"] != nothing[f"capex.{dimension}_mode"]


def test_no_ids_value_whatsoever_can_mean_unrestricted():
    """The invariant stated negatively: `unrestricted` is a MODE, never an id.

    A populated dimension always renders mode='list', no matter what the ids
    are -- including ids that used to be magic. There is no id string that
    flips the mode.
    """
    for hostile in ["*", "%", "ALL", "true", "1", "*,*", "ENT-1"]:
        settings = Scope(user_id="U-1", entity_ids=frozenset({hostile})).as_settings()
        assert settings["capex.entity_mode"] == "list", (
            f"id {hostile!r} changed the MODE; ids must never carry mode")


def test_scope_keys_match_what_as_settings_actually_emits():
    """`rls.current_scope_settings` iterates `SCOPE_KEYS` to prove no scope
    leaked across a pooled connection. A key that `as_settings` sets but
    `SCOPE_KEYS` omits would be invisible to that check -- it could leak
    unnoticed. They must be the same set."""
    emitted = set(Scope(user_id="U-1").as_settings())
    assert emitted == set(SCOPE_KEYS), (
        f"only in as_settings: {sorted(emitted - set(SCOPE_KEYS))}; "
        f"only in SCOPE_KEYS: {sorted(set(SCOPE_KEYS) - emitted)}")


def test_unchanged_settings_are_still_carried():
    """Contract 1 keeps these three exactly as they were."""
    settings = Scope.system("SVC-X").as_settings()
    assert settings["capex.user_id"] == "SVC-X"
    assert settings["capex.principal_kind"] == "SERVICE"
    assert settings["capex.read_all"] == "true"
    assert Scope(user_id="U-1").as_settings()["capex.read_all"] == "false"


# =================================================== a Python mirror of the SQL
def sql_dimension_permits(settings, setting_key, value):
    """Pure-Python mirror of `capex_dimension_permits` as 007 defines it.

    Mirrors the SQL statement for statement, including the mode-key
    derivation, so the live tests below can assert the database agrees with
    it case for case. `settings` maps a setting key to its value; a key
    ABSENT from the mapping models a session that never set it.
    """
    if value is None:
        return True  # the table/query shape waives this dimension
    mode_key = (setting_key[: -len("_ids")] + "_mode"
                if setting_key.endswith("_ids") else setting_key)
    mode = settings.get(mode_key, "")
    if mode == "all":
        return True
    if mode == "list":
        return value in settings.get(setting_key, "").split(",")
    return False  # 'none', absent, or unrecognised -- FAIL CLOSED


def test_mirror_absent_mode_denies():
    """A session that set no scope at all sees NOTHING.

    The single most important line in the function. A backstop that fails
    open on a forgotten `SET LOCAL` protects nothing -- and this is the exact
    case a connection used without `Database.session()` presents.
    """
    assert sql_dimension_permits({}, "capex.entity_ids", "ENT-1") is False
    # Even with ids present, an absent MODE denies: ids alone never permits.
    assert sql_dimension_permits(
        {"capex.entity_ids": "ENT-1"}, "capex.entity_ids", "ENT-1") is False


def test_mirror_mode_none_denies_everything():
    settings = {"capex.entity_mode": "none", "capex.entity_ids": ""}
    for value in ["ENT-1", "", "*", "anything"]:
        assert sql_dimension_permits(settings, "capex.entity_ids", value) is False


def test_mirror_mode_all_permits_everything():
    settings = {"capex.entity_mode": "all", "capex.entity_ids": ""}
    for value in ["ENT-1", "ENT-999", "*"]:
        assert sql_dimension_permits(settings, "capex.entity_ids", value) is True


def test_mirror_mode_list_does_membership():
    settings = {"capex.entity_mode": "list", "capex.entity_ids": "ENT-1,ENT-2"}
    assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-1") is True
    assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-2") is True
    assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-3") is False


def test_mirror_unknown_mode_denies():
    """Anything not in {all, list} denies -- typos included."""
    for mode in ["ALL", "All", "any", "*", "true", "yes", "lst", " all"]:
        settings = {"capex.entity_mode": mode, "capex.entity_ids": "ENT-1"}
        assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-1") is False, (
            f"mode {mode!r} was honoured; only exact 'all'/'list' may permit")


def test_mirror_null_value_waives_the_dimension():
    """A row shape carrying no column for this dimension is waived, exactly
    as `repo.compile_scope`'s `columns={"dim": None}` waiver does."""
    assert sql_dimension_permits({}, "capex.entity_ids", None) is True


def test_mirror_malformed_setting_key_denies():
    """A key not ending `_ids` derives no mode, so it denies -- fail closed
    even on programmer error, never widen."""
    assert sql_dimension_permits(
        {"capex.entity_mode": "all"}, "capex.entity", "ENT-1") is False


# ============================================ layer 1: the grant write path
class _FakeSession:
    """Records writes instead of performing them.

    `set_scope` is a write path; the property under test is that a refused
    request writes NOTHING. That is observable without a database, so it is
    asserted without one.
    """

    def __init__(self, principal_kind="USER"):
        self.principal_kind = principal_kind
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return None

    def fetchone(self, statement, params=None):
        if "principal_kind" in statement:
            return (self.principal_kind,)
        return None

    def fetchall(self, statement, params=None):
        return []


@pytest.mark.parametrize("bad", FORBIDDEN_VALUES)
def test_set_scope_refuses_forbidden_values_and_writes_nothing(bad):
    """The refusal is an EXCEPTION, not a silently dropped value.

    Dropping the offending id and writing the rest would grant less than the
    caller asked for without telling anyone -- a different defect, not a fix.
    And nothing at all may be written, so a refused request cannot leave the
    user's grants half-replaced.
    """
    session = _FakeSession()
    with pytest.raises(InvalidScopeValue) as excinfo:
        roles.set_scope(session, "U-1", {"entity_ids": ["ENT-1", bad]},
                        read_all=False, updated_by="U-ADM")
    assert "entity" in str(excinfo.value)
    assert session.executed == [], (
        f"a refused set_scope wrote {len(session.executed)} statement(s); it "
        f"must validate the whole request before the first write")


def test_set_scope_still_accepts_the_three_legitimate_shapes():
    """The guard must not break the contract it protects: `None`
    (unrestricted), `[]` (nothing) and a populated list all still work."""
    session = _FakeSession()
    roles.set_scope(
        session, "U-1",
        {"entity_ids": None, "plant_ids": [], "project_ids": ["PRJ-1"]},
        read_all=False, updated_by="U-ADM")
    written = " ".join(statement for statement, _ in session.executed)
    assert "user_scope_restriction" in written
    assert "user_scope_grant" in written


def test_set_scope_refuses_a_non_string_id():
    session = _FakeSession()
    with pytest.raises(InvalidScopeValue):
        roles.set_scope(session, "U-1", {"entity_ids": ["ENT-1", 7]},
                        read_all=False, updated_by="U-ADM")
    assert session.executed == []


@pytest.mark.parametrize("bad", FORBIDDEN_VALUES)
def test_validate_scope_value_rejects(bad):
    with pytest.raises(InvalidScopeValue):
        validate_scope_value(bad, dimension="entity")


def test_validate_scope_value_accepts_ordinary_ids():
    for good in ["ENT-1", "PLT-DM1-A", "PRJ-DM-001", "a", "x y"]:
        assert validate_scope_value(good, dimension="entity") == good


# ======================================== layer 2: the repository boundary
@pytest.mark.parametrize("bad", FORBIDDEN_VALUES)
def test_compile_scope_refuses_forbidden_values(bad):
    """A `Scope` can reach the repository without passing `set_scope` --
    resolved from a database restored from before this wave, or built
    directly. The repository refuses it too, so no route compiles it."""
    scope = Scope(user_id="U-1", entity_ids=frozenset({bad}))
    with pytest.raises(InvalidScopeValue):
        repo.compile_scope(scope, {"entity": "e.entity_id"})


def test_compile_scope_refuses_a_star_even_under_read_all():
    """Checked before the `read_all` short-circuit: a corrupt scope is
    corrupt whether or not it also happens to be unrestricted. Returning
    TRUE without looking would let the bad row travel on to somewhere that
    does look."""
    scope = Scope(user_id="U-1", read_all=True, entity_ids=frozenset({"*"}))
    with pytest.raises(InvalidScopeValue):
        repo.compile_scope(scope, {"entity": "e.entity_id"})


def test_compile_scope_refuses_a_star_even_where_the_dimension_is_waived():
    """Waiving the COLUMN (`columns={"entity": None}`) says this query shape
    cannot express the dimension. It does not make a corrupt id acceptable."""
    scope = Scope(user_id="U-1", entity_ids=frozenset({"*"}))
    with pytest.raises(InvalidScopeValue):
        repo.compile_scope(scope, {"entity": None})


def test_compile_scope_still_compiles_legitimate_scopes():
    """The three states still compile as contract 2 requires."""
    unrestricted = Scope(user_id="U-1")
    assert repo.compile_scope(unrestricted, {"entity": "e.entity_id"})[0] == "TRUE"

    nothing = Scope(user_id="U-1", entity_ids=frozenset())
    assert repo.compile_scope(nothing, {"entity": "e.entity_id"})[0] == "FALSE"

    listed = Scope(user_id="U-1", entity_ids=frozenset({"ENT-1"}))
    predicate, params = repo.compile_scope(listed, {"entity": "e.entity_id"})
    assert "ANY" in predicate
    assert list(params.values()) == [["ENT-1"]]


# ============================================ the regression this closes
def test_a_star_grant_can_no_longer_disable_application_scoping():
    """The defect, asserted dead at the application layer.

    Before: a `*` grant reached `compile_scope` as an ordinary id while RLS
    read it as a wildcard -- the layers disagreed. Now the repository refuses
    to compile it at all, so there is no predicate for the two layers to
    disagree about.
    """
    starred = Scope(user_id="U-1", entity_ids=frozenset({"*"}))
    with pytest.raises(InvalidScopeValue):
        repo.compile_scope(starred, {"entity": "e.entity_id"})


def test_a_star_grant_can_no_longer_disable_rls():
    """The same defect, asserted dead at the RLS layer.

    A `*` that somehow reaches the wire (this bypasses the two Python guards
    deliberately -- `as_settings` does not validate) is now just an ordinary
    id in a `list`. It matches a row whose value is literally `*` and nothing
    else. Under the old rule this same session permitted EVERY row.
    """
    settings = Scope(user_id="U-1", entity_ids=frozenset({"*"})).as_settings()
    assert settings["capex.entity_mode"] == "list"
    assert settings["capex.entity_ids"] == "*"

    assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-REAL") is False
    assert sql_dimension_permits(settings, "capex.entity_ids", "ENT-OTHER") is False
    # It is an id like any other -- it matches only itself.
    assert sql_dimension_permits(settings, "capex.entity_ids", "*") is True


def test_a_pre_wave3_client_setting_only_ids_is_denied_not_widened():
    """The precise old exploit, replayed.

    A caller rendering the OLD format sets `capex.entity_ids='*'` and no
    mode at all. Under 004 that permitted every row. Under 007 the absent
    mode denies. The failure mode of a stale client is denial, never
    exposure -- which is the right direction to fail.
    """
    old_format = {"capex.entity_ids": "*"}
    assert sql_dimension_permits(old_format, "capex.entity_ids", "ENT-1") is False


# ==================================================================== live
@pytest.mark.pg
@PG
def test_star_grant_cannot_be_inserted_live(pg_connection):
    """Layer 3: the database itself refuses, binding `psql`, a restored
    dump, and any future write path that forgets to call `set_scope`."""
    import psycopg

    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
        "VALUES ('U-SENT', 'sent@example.test', 'Sentinel', 'T', 'T')")
    pg_connection.execute(
        "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
        "VALUES ('U-SENT', 'entity', 'T')")
    pg_connection.commit()

    for bad in ["*", "", "   ", "A,B"]:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg_connection.execute(
                "INSERT INTO user_scope_grant "
                "(user_id, dimension, scope_value, granted_by) "
                "VALUES ('U-SENT', 'entity', %s, 'T')", (bad,))
        pg_connection.rollback()

    # An ordinary id is still accepted -- the constraint refuses sentinels,
    # not grants.
    pg_connection.execute(
        "INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) "
        "VALUES ('U-SENT', 'entity', 'ENT-OK', 'T')")
    pg_connection.commit()


@pytest.mark.pg
@PG
def test_absent_mode_sees_zero_rows_live(pg_connection):
    """A session that sets no scope at all is denied by the new function.

    This is the fail-closed property asserted against the real predicate
    rather than the mirror: no `SET LOCAL` has run, so every mode setting is
    absent.
    """
    row = pg_connection.execute(
        "SELECT capex_dimension_permits('capex.entity_ids', 'ENT-1')").fetchone()
    assert row[0] is False, (
        "a session with no scope set was PERMITTED; the backstop must fail "
        "closed on a forgotten SET LOCAL")
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_sql_function_agrees_with_the_python_mirror_live(pg_connection):
    """The mirror above is only useful if it describes the real predicate.

    Every case the database-free tests rely on is replayed against the
    actual SQL function inside one transaction, using `SET LOCAL` exactly as
    `Database.session()` does.
    """
    cases = [
        ({"capex.entity_mode": "all", "capex.entity_ids": ""}, "ENT-1"),
        ({"capex.entity_mode": "none", "capex.entity_ids": ""}, "ENT-1"),
        ({"capex.entity_mode": "list", "capex.entity_ids": "ENT-1,ENT-2"}, "ENT-1"),
        ({"capex.entity_mode": "list", "capex.entity_ids": "ENT-1,ENT-2"}, "ENT-9"),
        ({"capex.entity_mode": "list", "capex.entity_ids": "*"}, "ENT-1"),
        ({"capex.entity_mode": "list", "capex.entity_ids": "*"}, "*"),
        ({"capex.entity_mode": "bogus", "capex.entity_ids": "ENT-1"}, "ENT-1"),
        ({"capex.entity_ids": "*"}, "ENT-1"),          # old format, no mode
        ({"capex.entity_ids": "ENT-1"}, "ENT-1"),      # ids without a mode
    ]
    for settings, value in cases:
        # Reset both keys every case so a previous case cannot leak in, then
        # apply only what this case sets -- an unset key must read as absent.
        #
        # `set_config(key, value, true)` rather than `SET LOCAL key = %s`:
        # SET does not accept bind parameters, so the parameterised form is a
        # syntax error. `is_local => true` gives identical transaction-scoped
        # semantics to SET LOCAL, which is what `Database.session()` applies.
        for key in ("capex.entity_mode", "capex.entity_ids"):
            pg_connection.execute("SELECT set_config(%s, '', true)", (key,))
        for key, setting_value in settings.items():
            pg_connection.execute(
                "SELECT set_config(%s, %s, true)", (key, setting_value))

        actual = pg_connection.execute(
            "SELECT capex_dimension_permits('capex.entity_ids', %s)",
            (value,)).fetchone()[0]
        # '' models "absent" here: both read as not-'all'/not-'list', deny.
        expected = sql_dimension_permits(settings, "capex.entity_ids", value)
        assert actual is expected, (
            f"SQL and the Python mirror disagree for settings={settings}, "
            f"value={value!r}: SQL said {actual}, mirror said {expected}")
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_null_value_waives_the_dimension_live(pg_connection):
    """A policy passing a literal SQL NULL for a column the table does not
    carry is waived, matching `repo.compile_scope`'s explicit waiver."""
    row = pg_connection.execute(
        "SELECT capex_dimension_permits('capex.entity_ids', NULL)").fetchone()
    assert row[0] is True
    pg_connection.rollback()
