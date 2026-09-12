"""Tests for `app.backend.pg.seed` -- the demo-profile guard around
`migrations/pg/seed_demo.sql`.

The guard itself (profile check, disposable-name check) is pure logic against
a fake connection and runs unconditionally, without any database. The loader
round trip -- actually applying the seed and checking what it produced --
needs a live PostgreSQL and is gated the same way as the rest of the `pg`
suite: `pytest.mark.pg` (registered in `pytest.ini`) *and* an explicit
`skipif(not os.environ.get("CAPEX_DB_URL"))`, so a run with no PostgreSQL
configured skips cleanly rather than erroring at fixture setup.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly.
#
# Without this the live tests below fail at SETUP with "fixture 'pg_connection'
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

import os  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402

import conftest_pg  # noqa: E402
from app.backend.pg import seed as pgseed  # noqa: E402
from app.backend.pg.audit import verify_chain  # noqa: E402
from app.backend.pg.engine import Session  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


# ============================================================================
# Guard -- no database required.
# ============================================================================
class _FakeCursorResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Just enough of `psycopg.Connection` for the disposable-name check:
    `.execute(sql).fetchone()`. Never touches a real database -- the guard
    tests that reach this far only exercise checks 2/3, never an actual
    seed load."""

    def __init__(self, current_database_name: str):
        self._name = current_database_name
        self.executed: list[str] = []

    def execute(self, statement, params=None):
        assert "current_database" in statement, (
            "the fake only answers current_database(); a guard test reaching "
            "any other statement means the guard ran further than intended")
        self.executed.append(statement)
        return _FakeCursorResult((self._name,))


def test_guard_refuses_when_capex_profile_is_not_local_demo(monkeypatch):
    """No connection is touched at all -- the profile check runs, and
    refuses, before any database interaction. `connection=None` proves it."""
    monkeypatch.delenv("CAPEX_PROFILE", raising=False)
    with pytest.raises(pgseed.SeedGuardError, match="CAPEX_PROFILE"):
        pgseed.seed(None)  # type: ignore[arg-type]


def test_guard_refuses_an_explicitly_wrong_profile_value(monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "production")
    with pytest.raises(pgseed.SeedGuardError, match="CAPEX_PROFILE"):
        pgseed.seed(None)  # type: ignore[arg-type]


def test_guard_refuses_a_non_disposable_database_name(monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    fake = _FakeConnection("production_capex")
    with pytest.raises(pgseed.SeedGuardError, match="disposable"):
        pgseed.seed(fake)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["capex_t12345", "capex_tmpl_abcdef0123456789"])
def test_guard_accepts_names_matching_the_disposable_patterns(monkeypatch, name):
    """Positive control: the two patterns this guard accepts are exactly the
    ones tests/conftest_pg.py creates. This only proves check 2 passes -- the
    fake has no `.execute` for anything beyond `current_database()`, so it
    fails loudly (an AssertionError from the fake, not a SeedGuardError) the
    moment the guard tries to go further, which is exactly what should
    happen once the name check is satisfied."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    fake = _FakeConnection(name)
    with pytest.raises(AssertionError):
        pgseed.seed(fake)  # type: ignore[arg-type]
    assert len(fake.executed) == 1, "must have gotten past the name check"


def test_force_does_not_bypass_the_profile_check(monkeypatch):
    monkeypatch.delenv("CAPEX_PROFILE", raising=False)
    with pytest.raises(pgseed.SeedGuardError, match="CAPEX_PROFILE"):
        pgseed.seed(None, force=True)  # type: ignore[arg-type]


def test_force_does_not_bypass_the_disposable_name_check(monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    fake = _FakeConnection("production_capex")
    with pytest.raises(pgseed.SeedGuardError, match="disposable"):
        pgseed.seed(fake, force=True)  # type: ignore[arg-type]


def test_disposable_name_patterns_match_conftest_pg_exactly():
    """The two patterns are deliberately duplicated (application code must
    not import from tests/), not shared. This is the tripwire that keeps
    them from drifting apart silently."""
    assert pgseed._PER_TEST_DB_RE.pattern == conftest_pg._PER_TEST_DB_RE.pattern
    assert pgseed._TEMPLATE_DB_RE.pattern == conftest_pg._TEMPLATE_DB_RE.pattern


def test_seed_guard_error_is_a_runtime_error():
    """The refusal is always an exception -- never a bool, never a logged
    warning that lets the caller carry on."""
    assert issubclass(pgseed.SeedGuardError, RuntimeError)


# ============================================================================
# Seed fragments -- no database required.
#
# These exist because the fragment loader shipped broken and NOTHING caught
# it: `SEED_PARTS_DIR` was computed from a name that did not exist, guarded by
# an `if ... in dir()` that silently evaluated to None, and the loader
# function was never called from `seed()` at all. Every backend stream wrote
# its fragment; none of them would have loaded.
#
# The failure mode is the dangerous kind -- not a crash, but an empty demo
# estate that reads as "no data seeded yet" on every screen built against it.
# So these tests run with NO live PostgreSQL: the defect lived in code that a
# database-gated test would have skipped straight past.
# ============================================================================
class _RecordingConnection:
    """Accepts any statement and records it. Answers `current_database()`
    with a disposable name so the guard lets the load proceed."""

    def __init__(self, current_database_name: str = "capex_t1"):
        self._name = current_database_name
        self.executed: list[str] = []

    def execute(self, statement, params=None):
        self.executed.append(statement)
        return _FakeCursorResult((self._name,))


def test_seed_parts_dir_resolves_to_a_real_directory():
    """The original defect in one assertion.

    `SEED_PARTS_DIR` was None, so the loader had nothing to walk. A path that
    does not resolve is indistinguishable from a directory that is empty --
    both load zero fragments -- which is why this asserts the directory
    exists rather than that the walk returned something."""
    assert pgseed.SEED_PARTS_DIR is not None
    assert pgseed.SEED_PARTS_DIR.is_dir(), (
        f"{pgseed.SEED_PARTS_DIR} does not exist; every stream's seed "
        f"fragment would load nowhere and every screen built on it would "
        f"render an empty state that looks like missing data, not a defect")
    assert pgseed.SEED_PARTS_DIR.name == "seed_parts"
    assert pgseed.SEED_PARTS_DIR.parent == pgseed.SEED_FILE.parent


def test_every_committed_fragment_is_discovered():
    """Whatever is on disk is what loads. No allow-list to forget to update."""
    on_disk = sorted(f.name for f in pgseed.SEED_PARTS_DIR.iterdir()
                     if f.suffix == ".sql")
    discovered = [f.name for f in pgseed.seed_part_files()]
    assert discovered == on_disk
    assert on_disk, (
        "no fragments committed; if that is deliberate, this assertion is "
        "the thing to delete -- deliberately, not by accident")


def test_fragments_load_in_filename_order():
    """Load order is filename order, and the numbering is what encodes the
    dependency: 003_budget.sql needs 003_budget_planning.sql's tables."""
    names = [f.name for f in pgseed.seed_part_files()]
    assert names == sorted(names)


def test_seed_executes_the_base_file_then_every_fragment(tmp_path, monkeypatch):
    """The regression proper: `seed()` must actually CALL the loader.

    The loader existed and was correct; nothing invoked it. A test that only
    checked `seed_part_files()` in isolation would have passed against the
    broken build."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")

    base = tmp_path / "seed_demo.sql"
    base.write_text("-- BASE", encoding="utf-8")
    parts = tmp_path / "seed_parts"
    parts.mkdir()
    (parts / "005_masters.sql").write_text("-- FIVE", encoding="utf-8")
    (parts / "003_budget.sql").write_text("-- THREE", encoding="utf-8")
    (parts / "notes.md").write_text("not sql", encoding="utf-8")

    con = _RecordingConnection()
    pgseed.seed(con, force=True, seed_file=base, parts_dir=parts)  # type: ignore[arg-type]

    loaded = [s for s in con.executed if s.startswith("--")]
    assert loaded == ["-- BASE", "-- THREE", "-- FIVE"], (
        "base first, then fragments in filename order, and nothing else")


def test_a_missing_fragment_directory_is_not_an_error(tmp_path, monkeypatch):
    """seed_demo.sql alone must remain a valid estate -- a fresh checkout
    with no fragments yet still seeds."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    base = tmp_path / "seed_demo.sql"
    base.write_text("-- BASE", encoding="utf-8")

    con = _RecordingConnection()
    pgseed.seed(con, force=True, seed_file=base,
                parts_dir=tmp_path / "nonexistent")  # type: ignore[arg-type]
    assert [s for s in con.executed if s.startswith("--")] == ["-- BASE"]


def test_fragments_never_load_when_a_guard_refuses(monkeypatch):
    """Fragment loading sits behind every guard, not beside them."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    con = _RecordingConnection("production_capex")
    with pytest.raises(pgseed.SeedGuardError, match="disposable"):
        pgseed.seed(con, force=True)  # type: ignore[arg-type]
    assert not [s for s in con.executed if s.startswith("--")]


# ============================================================================
# Live loader -- needs a real, disposable PostgreSQL database.
# ============================================================================
def _seed_base_only(connection) -> None:
    """Load `seed_demo.sql` WITHOUT the `seed_parts/` fragments.

    The assertions below pin the demo estate exactly -- row counts per table,
    audit-chain lengths, rollup totals to the paisa. That precision is the
    point: it catches a seed edit that silently changes the dataset every
    screenshot and screen was approved against.

    It only works against a fixed input. `seed()` loads `seed_demo.sql` plus
    every fragment each backend stream contributes, so those numbers move
    whenever a stream lands. Pointing `parts_dir` at a directory that does not
    exist gives these tests the one stable input they need, and
    `test_the_fragments_load_on_top_of_the_base_seed` covers the composed
    estate separately.
    """
    pgseed.seed(connection, parts_dir=_Path("no-such-seed-parts-directory"))


EXPECTED_COUNTS = {
    "organisation": 1,
    "entity": 2,
    "division": 2,
    "branch": 2,
    "zone": 3,
    "plant": 3,
    "location": 5,
    # 10 synthetic identities + the four Zoho ERP demo users seeded as
    # administrators on the owner's decision of 2026-09-11 (218b544).
    "app_user": 14,
    "accounting_period": 7,
    "budget_head": 5,
    "project": 2,
    "wbs_element": 14,
    "budget_control_cell": 14,
    "budget_ledger_cell": 14,
    "audit_log": 7,
}

MONEY_COLUMNS_BY_TABLE = {
    "budget_control_cell": ["budget_paise"],
    "budget_ledger_cell": [
        "original_paise", "revisions_paise", "future_budget_paise",
        "ordered_paise", "commitment_paise", "actual_paise", "received_paise",
        "received_not_billed_paise", "pr_reserved_paise",
    ],
}


@pytest.mark.pg
@PG
def test_seed_loads_the_full_demo_dataset(pg_connection, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    _seed_base_only(pg_connection)
    pg_connection.commit()

    for table, expected in EXPECTED_COUNTS.items():
        n = pg_connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()[0]
        assert n == expected, f"{table}: expected {expected} rows, got {n}"


@pytest.mark.pg
@PG
def test_every_money_column_is_an_exact_int_never_float_or_decimal(pg_connection, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    _seed_base_only(pg_connection)
    pg_connection.commit()

    checked = 0
    for table, columns in MONEY_COLUMNS_BY_TABLE.items():
        col_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        rows = pg_connection.execute(
            sql.SQL("SELECT {} FROM {}").format(col_list, sql.Identifier(table))
        ).fetchall()
        for row in rows:
            for value in row:
                assert isinstance(value, int), (
                    f"{table} money column produced {value!r} of type "
                    f"{type(value).__name__}, not int")
                assert not isinstance(value, bool), (
                    f"{table} money column produced a bool, not a real int")
                checked += 1
    assert checked > 0, "the test itself found nothing to check"


@pytest.mark.pg
@PG
def test_seeded_audit_streams_verify_intact(pg_connection, pg_scope, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    _seed_base_only(pg_connection)
    pg_connection.commit()

    session = Session(connection=pg_connection, scope=pg_scope)

    result_1 = verify_chain(session, "PROJECT:PRJ-DM-001")
    assert result_1["intact"] is True
    assert result_1["stream_found"] is True
    assert result_1["entries_checked"] == 4
    assert result_1["first_break_seq"] is None
    assert result_1["sequence_contiguous"] is True

    result_2 = verify_chain(session, "PROJECT:PRJ-DM-002")
    assert result_2["intact"] is True
    assert result_2["stream_found"] is True
    assert result_2["entries_checked"] == 3
    assert result_2["first_break_seq"] is None

    assert result_1 != result_2, "two genuinely distinct stream_keys, not one stream read twice"


@pytest.mark.pg
@PG
def test_po_line_invariant_holds_for_every_ledger_cell_with_spend(pg_connection, monkeypatch):
    """domain-controls.md: `billed + open_commitment == ordered`, i.e.
    `actual_paise + commitment_paise == ordered_paise` for every cell that
    has any ordered amount at all."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    _seed_base_only(pg_connection)
    pg_connection.commit()

    rows = pg_connection.execute(
        "SELECT wbs_id, budget_head_id, ordered_paise, commitment_paise, actual_paise "
        "FROM budget_ledger_cell WHERE ordered_paise <> 0"
    ).fetchall()
    assert rows, "expected at least one ledger cell carrying spend"
    for wbs_id, head_id, ordered, commitment, actual in rows:
        assert commitment + actual == ordered, (
            f"({wbs_id}, {head_id}): commitment {commitment} + actual {actual} "
            f"!= ordered {ordered}")
        assert commitment >= 0 and actual is not None


@pytest.mark.pg
@PG
def test_ancestor_chain_rollup_matches_own_plus_children(pg_connection, monkeypatch):
    """domain-controls.md: `rollup == own + sum(children)`, computed by an
    ltree subtree scan -- never by summing transaction history directly.

    Exercises the exact scenario the ancestor-chain locking proof depends on:
    WBS-A-CIVIL and its child WBS-A-CIVIL-FOUND both own budget for the same
    head (BH-DM1-CIVIL), and exposure at the leaves rolls up through BOTH.
    """
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    _seed_base_only(pg_connection)
    pg_connection.commit()

    def subtree_exposure(root_wbs_id: str, head_id: str) -> int:
        row = pg_connection.execute(
            """
            -- ::bigint for the same reason the service layer casts: SUM over bigint
            -- returns numeric, which psycopg hands back as Decimal, and a
            -- Decimal compared against an int total silently fails the
            -- money rule this suite exists to enforce.
            SELECT coalesce(sum(l.commitment_paise + l.actual_paise
                                + l.pr_reserved_paise), 0)::bigint
            FROM wbs_element w
            JOIN wbs_element root ON root.wbs_id = %s
            JOIN budget_ledger_cell l ON l.wbs_id = w.wbs_id AND l.budget_head_id = %s
            WHERE w.wbs_path <@ root.wbs_path
            """,
            (root_wbs_id, head_id)).fetchone()
        return row[0]

    def own_exposure(wbs_id: str, head_id: str) -> int:
        row = pg_connection.execute(
            "SELECT commitment_paise + actual_paise + pr_reserved_paise "
            "FROM budget_ledger_cell WHERE wbs_id = %s AND budget_head_id = %s",
            (wbs_id, head_id)).fetchone()
        return row[0] if row else 0

    def budget_of(wbs_id: str, head_id: str) -> int:
        row = pg_connection.execute(
            "SELECT budget_paise FROM budget_control_cell "
            "WHERE wbs_id = %s AND budget_head_id = %s",
            (wbs_id, head_id)).fetchone()
        return row[0]

    head = "BH-DM1-CIVIL"

    # Leaf-level own exposure, computed independently for the rollup check below.
    pil = own_exposure("WBS-A-CIVIL-FOUND-PIL", head)
    conc = own_exposure("WBS-A-CIVIL-FOUND-CONC", head)
    struct = own_exposure("WBS-A-CIVIL-STRUCT", head)
    assert pil == 70_000_000     # Rs 4,00,000 commitment + Rs 2,00,000 actual + Rs 1,00,000 PR
    assert conc == 40_000_000    # Rs 0 commitment + Rs 4,00,000 actual + Rs 0 PR
    assert struct == 60_000_000  # Rs 5,00,000 commitment + Rs 0 actual + Rs 1,00,000 PR

    # rollup(FOUND) == own(FOUND) + own(PIL) + own(CONC), since FOUND's only
    # descendants are the two leaves.
    rollup_found = subtree_exposure("WBS-A-CIVIL-FOUND", head)
    assert rollup_found == own_exposure("WBS-A-CIVIL-FOUND", head) + pil + conc
    assert rollup_found == 110_000_000

    # rollup(CIVIL) == own(CIVIL) + rollup(FOUND) + own(STRUCT) -- the whole
    # chain, matching the ancestor-chain locking scenario: a spend at
    # FOUND-PIL/-CONC reduces availability at BOTH WBS-A-CIVIL-FOUND (the
    # nearest budget-owning ancestor) AND WBS-A-CIVIL (a further one), because
    # BOTH own budget for the same head.
    rollup_civil = subtree_exposure("WBS-A-CIVIL", head)
    assert rollup_civil == own_exposure("WBS-A-CIVIL", head) + rollup_found + struct
    assert rollup_civil == 170_000_000

    # available == budget - exposure at each budget-owning level in the chain.
    assert budget_of("WBS-A-CIVIL-FOUND", head) - rollup_found == 90_000_000
    assert budget_of("WBS-A-CIVIL", head) - rollup_civil == 630_000_000

    # Both ancestors in the chain genuinely own budget for this head -- the
    # precondition for the multi-level lock set the seed exists to exercise.
    assert budget_of("WBS-A-CIVIL", head) != 0
    assert budget_of("WBS-A-CIVIL-FOUND", head) != 0


@pytest.mark.pg
@PG
def test_the_fragments_load_on_top_of_the_base_seed(pg_connection, monkeypatch):
    """The composed estate, which is what `seed()` actually produces.

    The counterpart to the exact-count tests above: those pin
    `seed_demo.sql` alone, this proves the `seed_parts/` fragments are applied
    on top of it. Deliberately asserts the RELATIONSHIP rather than new fixed
    numbers, so a stream adding a fragment does not have to edit this file --
    and so this test cannot be quietly satisfied by fragments that fail to
    load.

    The loader shipped broken once, computing its directory from a name that
    did not exist and never being called at all, which showed up as screens
    rendering an empty state that read as "no data yet" rather than as a
    defect.
    """
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    pgseed.seed(pg_connection)
    pg_connection.commit()

    def count(table: str) -> int:
        return pg_connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()[0]

    for table, base in EXPECTED_COUNTS.items():
        assert count(table) >= base, (
            f"{table}: the composed estate has fewer rows than seed_demo.sql "
            f"alone, so a fragment deleted base data")

    # Tables only a fragment populates. Non-empty here is the proof that
    # fragments ran; each is empty under `_seed_base_only`.
    for table in ("budget_line", "budget_revision", "budget_transfer",
                   "item_master", "vendor_master"):
        assert count(table) > 0, (
            f"{table} is empty, so migrations/pg/seed_parts/ did not load")

    assert pgseed.seed_part_files(), "no fragments are committed to load"


@pytest.mark.pg
@PG
def test_seed_refuses_when_the_database_already_contains_rows(pg_connection, monkeypatch):
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    pgseed.seed(pg_connection)
    pg_connection.commit()

    with pytest.raises(pgseed.SeedGuardError, match="already contains rows"):
        pgseed.seed(pg_connection)


@pytest.mark.pg
@PG
def test_force_skips_the_emptiness_precheck_but_still_never_silently_merges(pg_connection, monkeypatch):
    """`force` widens ONLY the emptiness pre-flight. It must never delete or
    merge -- a database that genuinely still holds the first seed's rows must
    fail on Postgres's own primary-key violation, loudly, not succeed."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    pgseed.seed(pg_connection)
    pg_connection.commit()

    with pytest.raises(psycopg.Error):
        pgseed.seed(pg_connection, force=True)
    pg_connection.rollback()

    # Nothing was duplicated or corrupted by the failed attempt.
    n = pg_connection.execute("SELECT count(*) FROM organisation").fetchone()[0]
    assert n == 1


@pytest.mark.pg
@PG
def test_seed_refuses_against_an_unmigrated_database(pg_url, pg_admin_connection, monkeypatch):
    """Check 3 also refuses cleanly (not with a bare psycopg error) when the
    seeded tables do not exist at all -- e.g. a disposable database that was
    created but never migrated."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    name = conftest_pg._next_test_db_name()
    pg_admin_connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(conftest_pg._replace_dbname(pg_url, name), autocommit=False) as con:
            with pytest.raises(pgseed.SeedGuardError, match="does not exist"):
                pgseed.seed(con)
    finally:
        pg_admin_connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(conftest_pg._assert_pg_disposable(name))))
