"""The import: FK order, constraints ENABLED, rollback, resume, reconciliation.

Runs everywhere. There is no PostgreSQL on a workstation here, so the import is
EXECUTED against a disposable SQLite target created by the test itself -- with
``PRAGMA foreign_keys = ON``, so the ordering, the constraint enforcement, the
rollback and the ``ON CONFLICT DO NOTHING`` idempotency are all really exercised
rather than reasoned about.

**What that does and does not prove.** It proves the tool's *behaviour*: the
order it inserts in, that it never disables a constraint, that a failure leaves
nothing behind, that a resumed run duplicates nothing, and that the
reconciliation catches a single paisa. It does NOT prove the SQL runs on
PostgreSQL -- ``ON CONFLICT``, ``bigint`` casts and identity columns are all
close but not identical, and this file must not be read as saying otherwise.
The live half is ``tests/test_pg_migration_import.py``, which is
``@pytest.mark.pg`` and skips here. Its filename starts ``test_pg_`` on purpose:
CI's ``pg_tests`` job collects ``tests/test_pg_*.py`` and nothing else, so a
PostgreSQL-gated file named any other way would skip on every workstation AND
never be collected in the one job that could run it. A skip is not a pass, and a
file that is never collected is not even a skip.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.migration import export_poc, import_pg, reconcile        # noqa: E402
from tools.migration.schema import (Column, Table, split_top_level,  # noqa: E402
                                    matching_paren, scan_pg_schema,
                                    strip_sql_comments, topological_order)

# entity <- project <- budget_line. Three levels, so a wrong order is visible.
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE entity (
  entity_id TEXT PRIMARY KEY,
  name      TEXT NOT NULL
);
CREATE TABLE project (
  project_id TEXT PRIMARY KEY,
  entity_id  TEXT NOT NULL REFERENCES entity(entity_id),
  name       TEXT NOT NULL
);
CREATE TABLE budget_line (
  budget_line_id TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES project(project_id),
  entity_id      TEXT NOT NULL REFERENCES entity(entity_id),
  amount_paise   INTEGER NOT NULL
);
"""

TARGET_TABLES = {
    "entity": Table("entity", [Column("entity_id", "text", True, False, False),
                               Column("name", "text", True, False, False)]),
    "project": Table("project", [Column("project_id", "text", True, False, False),
                                 Column("entity_id", "text", True, False, False),
                                 Column("name", "text", True, False, False)],
                     [(("entity_id",), "entity", ("entity_id",))]),
    "budget_line": Table("budget_line",
                         [Column("budget_line_id", "text", True, False, False),
                          Column("project_id", "text", True, False, False),
                          Column("entity_id", "text", True, False, False),
                          Column("amount_paise", "bigint", True, False, False)],
                         [(("project_id",), "project", ("project_id",)),
                          (("entity_id",), "entity", ("entity_id",))]),
}

ROWS = {
    "entity": [("ENT-1", "First"), ("ENT-2", "Second")],
    "project": [("PRJ-1", "ENT-1", "Alpha"), ("PRJ-2", "ENT-2", "Beta")],
    "budget_line": [("BL-1", "PRJ-1", "ENT-1", 100_00), ("BL-2", "PRJ-1", "ENT-1", 1),
                    ("BL-3", "PRJ-2", "ENT-2", -250_50)],
}


# --------------------------------------------------------------------- helpers
def build_source(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO entity VALUES (?,?)", ROWS["entity"])
    con.executemany("INSERT INTO project VALUES (?,?,?)", ROWS["project"])
    con.executemany("INSERT INTO budget_line VALUES (?,?,?,?)", ROWS["budget_line"])
    con.commit()
    con.close()
    return path


def build_target(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, isolation_level=None)
    con.executescript(SCHEMA)
    con.execute("BEGIN")
    return con


def sqlite_execute(con, sql, params):
    """Adapt psycopg's ``%(name)s`` to SQLite's ``:name``.

    Only the placeholder dialect differs; the statement, the column list, the
    order and the ON CONFLICT clause are the ones the real import issues.
    """
    return con.execute(re.sub(r"%\((\w+)\)s", r":\1", sql), params)


@pytest.fixture()
def exported(tmp_path: Path) -> Path:
    build_source(tmp_path / "source.db")
    export_poc.export(tmp_path / "source.db", tmp_path / "export")
    return tmp_path / "export"


# ------------------------------------------- the scanner that reads the schema
def test_the_column_scanner_crosses_nested_parens_a_regex_cannot():
    """The defect class this whole module was written against.

    ``[^,]*`` stops at the comma inside ``IN ('a','b')``; ``.*?\\)`` closes on
    the first ``)`` inside ``CHECK (amount >= (0))``. A depth counter does not.
    """
    body = ("a text CHECK (a IN ('x','y')), "
            "b bigint CHECK (b >= (0) AND b < (1,2)), "
            "CONSTRAINT c UNIQUE (a, b)")
    parts = [p.strip() for p in split_top_level(body)]
    assert len(parts) == 3
    assert parts[0].startswith("a text")
    assert parts[1].startswith("b bigint")
    assert parts[2].startswith("CONSTRAINT c")


def test_matching_paren_refuses_an_unbalanced_body_rather_than_guessing():
    with pytest.raises(ValueError, match="unbalanced"):
        matching_paren("CREATE TABLE t (a int", len("CREATE TABLE t "))


def test_comment_stripping_does_not_eat_a_double_dash_inside_a_literal():
    stripped = strip_sql_comments("a text DEFAULT '--not a comment', -- but this is\n b int")
    assert "'--not a comment'" in stripped
    assert "but this is" not in stripped


def test_the_scanner_finds_a_column_that_follows_a_multiline_comment():
    """`purchase_request.amount_paise` is preceded by a three-line comment in
    ``013_procurement.sql``, and a naive scan drops it. It is a MONEY column, so
    dropping it would have produced a preflight that reported a money column as
    unmapped when it is not -- and the opposite mistake would be worse."""
    tables = scan_pg_schema()
    assert "amount_paise" in tables["purchase_request"].column_names


def test_topological_order_puts_every_parent_before_every_child_in_the_real_schema():
    tables = scan_pg_schema()
    order = topological_order(tables)
    position = {name: i for i, name in enumerate(order)}
    for name, table in tables.items():
        for _local, target, _cols in table.foreign_keys:
            if target != name and target in position:
                assert position[target] < position[name], f"{target} must precede {name}"


def test_a_foreign_key_cycle_is_refused_not_resolved():
    a = Table("a", [Column("a_id", "text", True, False, False)],
              [(("b_id",), "b", ("b_id",))])
    b = Table("b", [Column("b_id", "text", True, False, False)],
              [(("a_id",), "a", ("a_id",))])
    with pytest.raises(ValueError, match="cycle"):
        topological_order({"a": a, "b": b})


def test_topological_order_is_stable_across_runs():
    tables = scan_pg_schema()
    assert topological_order(tables) == topological_order(tables)


# ------------------------------------------------------- constraints stay ON
def _sql_literals(module) -> list[str]:
    """Every string constant in `module` that is a SQL statement.

    Parsed with ``ast``, not grepped. The prose in this module's own docstring
    says the words ``session_replication_role`` and ``TRUNCATE`` -- explaining
    why they are never emitted -- so a grep over the file would fail on the
    documentation and pass on nothing. What matters is whether a SQL STATEMENT
    contains them, and a statement is a string literal beginning with a SQL
    verb. Recognising that is a job for the parser, which is the same lesson
    the money-SQL gate learned the hard way.
    """
    import ast
    verbs = ("insert", "update", "delete", "select", "set ", "alter", "truncate",
             "drop", "create", "begin", "commit", "grant", "revoke")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    # The FORBIDDEN_STATEMENTS tuple is itself made of SQL-shaped strings. It is
    # the DECLARATION of what must not be emitted, not an emission, so its own
    # subtree is excluded -- otherwise the guard reports the guard.
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "FORBIDDEN_STATEMENTS"
                        for t in node.targets)):
            excluded.update(id(child) for child in ast.walk(node))

    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in excluded):
            text = node.value.strip().lower()
            if any(text.startswith(v) for v in verbs):
                out.append(node.value)
    return out


def test_the_module_never_emits_a_statement_that_would_disable_a_constraint():
    """Most of the import's value is that it doubles as a constraint proof.

    ``session_replication_role = 'replica'`` would make it faster and would
    prove only that the rows fit in the columns. A comment saying so is not a
    control; this is.
    """
    statements = _sql_literals(import_pg)
    assert statements, "found no SQL literals at all -- the check would be vacuous"
    for statement in statements:
        lowered = statement.lower()
        for forbidden in import_pg.FORBIDDEN_STATEMENTS:
            token = forbidden.split()[0].split(".")[0].lower()
            assert token not in lowered, f"{forbidden!r} in statement: {statement!r}"


def test_the_generated_insert_statement_carries_no_constraint_escape():
    sql = import_pg.insert_statement("budget_line", ["a", "amount_paise"], ["a"])
    for forbidden in import_pg.FORBIDDEN_STATEMENTS:
        assert forbidden.split()[0].lower() not in sql.lower()


def test_the_forbidden_list_actually_names_session_replication_role():
    """A guard over an empty list passes forever."""
    assert "session_replication_role" in import_pg.FORBIDDEN_STATEMENTS


def test_foreign_keys_really_are_enforced_by_the_target_fixture(tmp_path: Path):
    """If the fixture did not enforce them, every ordering test below would
    pass whatever order the import chose."""
    con = build_target(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO project VALUES ('PRJ-9','ENT-NOPE','X')")
    con.close()


# ------------------------------------------------------------------- preflight
def test_preflight_writes_nothing_and_says_so(exported: Path, tmp_path: Path):
    before = sorted(p.name for p in exported.iterdir())
    report = import_pg.preflight(exported, TARGET_TABLES)
    assert report["would_change_anything"] is False
    assert sorted(p.name for p in exported.iterdir()) == before


def test_preflight_orders_parents_before_children(exported: Path):
    order = import_pg.preflight(exported, TARGET_TABLES)["insert_order"]
    assert order.index("entity") < order.index("project") < order.index("budget_line")


def test_preflight_refuses_an_unmapped_column_rather_than_dropping_it(
        exported: Path, monkeypatch):
    trimmed = dict(TARGET_TABLES)
    trimmed["budget_line"] = Table(
        "budget_line",
        [c for c in TARGET_TABLES["budget_line"].columns if c.name != "amount_paise"],
        TARGET_TABLES["budget_line"].foreign_keys)
    report = import_pg.preflight(exported, trimmed)
    assert report["ready"] is False
    assert report["unmapped_columns"]["budget_line"] == ["amount_paise"]
    assert report["unmapped_money_columns"]["budget_line"] == ["amount_paise"]
    assert any("MONEY" in b for b in report["blockers"])


def test_the_real_poc_export_is_refused_today_and_the_reason_is_enumerated(tmp_path: Path):
    """The current, honest state of the column mapping.

    ``COLUMN_DECISIONS`` is empty, so a real POC export cannot be imported. That
    is correct: the POC and the production schema differ by dozens of columns,
    the analysis has not been done, and a migration that silently drops them is
    far worse than one that refuses. This test exists so that emptying the
    refusal by accident fails the build.
    """
    assert import_pg.COLUMN_DECISIONS == {}
    build_source(tmp_path / "s.db")
    export_poc.export(tmp_path / "s.db", tmp_path / "e")
    trimmed = {"entity": TARGET_TABLES["entity"]}
    report = import_pg.preflight(tmp_path / "e", trimmed)
    assert report["ready"] is False
    assert set(report["source_only_tables"]) == {"project", "budget_line"}


def test_import_refuses_to_start_when_the_preflight_refuses(exported: Path, tmp_path: Path):
    trimmed = {"entity": TARGET_TABLES["entity"]}
    con = build_target(tmp_path / "t.db")
    with pytest.raises(import_pg.ImportError_, match="preflight refused"):
        import_pg.import_tables(con, exported, pg_tables=trimmed, execute=sqlite_execute)
    con.close()


# --------------------------------------------------------------------- dry run
def test_a_dry_run_drives_the_real_path_and_sends_nothing(exported: Path):
    recorder = import_pg.Recorder()
    result = import_pg.import_tables(recorder, exported, pg_tables=TARGET_TABLES,
                                     dry_run=True)
    assert result.dry_run is True
    assert sum(result.tables_written.values()) == 7
    assert len(recorder.statements) == 7
    assert all(s.startswith("INSERT INTO") for s, _ in recorder.statements)
    assert all("ON CONFLICT" in s for s, _ in recorder.statements)


def test_the_dry_run_statement_order_is_the_topological_order(exported: Path):
    recorder = import_pg.Recorder()
    import_pg.import_tables(recorder, exported, pg_tables=TARGET_TABLES, dry_run=True)
    tables = [s.split('"')[1] for s, _ in recorder.statements]
    assert tables.index("entity") < tables.index("project") < tables.index("budget_line")


# ------------------------------------------------------------- the real import
def test_the_import_lands_every_row_with_foreign_keys_enforced(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES,
                            execute=sqlite_execute)
    con.close()

    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 3
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    check.close()


def test_inserting_in_the_wrong_order_really_does_fail(exported: Path, tmp_path: Path):
    """Proves the ordering is load-bearing and not decoration.

    If the target did not enforce foreign keys, every ordering assertion here
    would be vacuous. This inserts a child first and requires the failure.
    """
    con = build_target(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO budget_line VALUES ('BL-9','PRJ-1','ENT-1',1)")
    con.close()


def test_money_arrives_as_an_integer_including_the_negative_and_the_single_paisa(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES,
                            execute=sqlite_execute)
    con.close()
    check = sqlite3.connect(target)
    amounts = dict(check.execute("SELECT budget_line_id, amount_paise FROM budget_line"))
    assert amounts == {"BL-1": 10000, "BL-2": 1, "BL-3": -25050}
    for value in amounts.values():
        assert type(value) is int
    check.close()


# -------------------------------------------------------- rollback and restart
def test_a_failure_rolls_the_whole_atomic_import_back(exported: Path, tmp_path: Path):
    """--atomic means the target is only ever untouched or fully migrated."""
    target = tmp_path / "t.db"
    con = build_target(target)

    def failing(connection, sql, params):
        if '"budget_line"' in sql and params.get("budget_line_id") == "BL-3":
            raise sqlite3.IntegrityError("simulated failure part way through")
        return sqlite_execute(connection, sql, params)

    with pytest.raises(sqlite3.IntegrityError):
        import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=failing)
    con.close()

    check = sqlite3.connect(target)
    for table in ("entity", "project", "budget_line"):
        assert check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    check.close()


def test_rerunning_an_atomic_import_after_a_failure_is_clean(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)

    def failing(connection, sql, params):
        if '"budget_line"' in sql and params.get("budget_line_id") == "BL-3":
            raise sqlite3.IntegrityError("simulated")
        return sqlite_execute(connection, sql, params)

    with pytest.raises(sqlite3.IntegrityError):
        import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=failing)
    con.execute("BEGIN")
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.close()

    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 3
    check.close()


def test_a_resumable_run_that_is_interrupted_leaves_a_consistent_prefix(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    checkpoint = import_pg.Checkpoint.load(tmp_path / "ck.json")
    checkpoint.mode = "resumable"
    con = build_target(target)

    def failing(connection, sql, params):
        if '"budget_line"' in sql:
            raise sqlite3.IntegrityError("connection dropped")
        return sqlite_execute(connection, sql, params)

    with pytest.raises(sqlite3.IntegrityError):
        import_pg.import_tables(con, exported, mode="resumable", checkpoint=checkpoint,
                                pg_tables=TARGET_TABLES, execute=failing)
    con.close()

    assert checkpoint.completed == ["entity", "project"]
    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 0
    # The prefix is FK-closed: every committed row's parents are committed too.
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    check.close()


def test_resuming_skips_completed_tables_and_duplicates_nothing(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    checkpoint = import_pg.Checkpoint.load(tmp_path / "ck.json")
    checkpoint.mode = "resumable"
    con = build_target(target)

    def failing(connection, sql, params):
        if '"budget_line"' in sql:
            raise sqlite3.IntegrityError("connection dropped")
        return sqlite_execute(connection, sql, params)

    with pytest.raises(sqlite3.IntegrityError):
        import_pg.import_tables(con, exported, mode="resumable", checkpoint=checkpoint,
                                pg_tables=TARGET_TABLES, execute=failing)
    con.close()

    resumed = sqlite3.connect(target, isolation_level=None)
    resumed.execute("PRAGMA foreign_keys = ON")
    resumed.execute("BEGIN")
    result = import_pg.import_tables(resumed, exported, mode="resumable",
                                     checkpoint=checkpoint, pg_tables=TARGET_TABLES,
                                     execute=sqlite_execute)
    resumed.close()

    assert "entity" not in result.tables_written        # skipped, not re-inserted
    assert result.tables_written == {"budget_line": 3}
    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 3
    check.close()


def test_re_running_a_completed_table_inserts_no_duplicates(
        exported: Path, tmp_path: Path):
    """The checkpoint could be lost. ON CONFLICT DO NOTHING is the second line."""
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, mode="resumable", pg_tables=TARGET_TABLES,
                            execute=sqlite_execute)
    con.execute("BEGIN")
    import_pg.import_tables(con, exported, mode="resumable", pg_tables=TARGET_TABLES,
                            execute=sqlite_execute)
    con.close()
    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM budget_line").fetchone()[0] == 3
    check.close()


def test_a_checkpoint_from_a_different_export_is_refused(tmp_path: Path):
    checkpoint = import_pg.Checkpoint.load(tmp_path / "ck.json")
    checkpoint.guard_same_export("a" * 64)
    checkpoint.save()
    reloaded = import_pg.Checkpoint.load(tmp_path / "ck.json")
    with pytest.raises(import_pg.ImportError_, match="different export"):
        reloaded.guard_same_export("b" * 64)


def test_the_checkpoint_is_written_only_after_a_commit(exported: Path, tmp_path: Path):
    """A checkpoint written before the commit tells a restart to skip a table
    that is not there."""
    con = build_target(tmp_path / "t.db")
    checkpoint = import_pg.Checkpoint.load(tmp_path / "ck.json")
    seen: list[list[str]] = []

    class WatchedCommit:
        """sqlite3.Connection.commit is read-only, so wrap rather than patch."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def commit(self):
            seen.append(list(checkpoint.completed))
            return self._wrapped.commit()

    import_pg.import_tables(WatchedCommit(con), exported, mode="resumable",
                            checkpoint=checkpoint, pg_tables=TARGET_TABLES,
                            execute=sqlite_execute)
    con.close()
    # At each commit the checkpoint still holds only the PREVIOUSLY committed
    # tables -- never the one being committed.
    assert seen == [[], ["entity"], ["entity", "project"]]


# ------------------------------------------------------------- reconciliation
def _source_totals(export_dir: Path) -> dict[str, reconcile.TableTotals]:
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    return {
        table: reconcile.totals_from_records(
            table, entry["columns"],
            export_poc.load_ndjson(export_dir / entry["file"],
                                   frozenset(entry.get("float_columns", ()))))
        for table, entry in manifest["tables"].items()
    }


def _target_totals(db: Path, tables: dict) -> dict[str, reconcile.TableTotals]:
    con = sqlite3.connect(db)
    out = {}
    for name, table in tables.items():
        def query(sql, params, _con=con):
            return _con.execute(sql.replace("::bigint", ""), params).fetchall()
        out[name] = reconcile.totals_from_sql(name, table.column_names, query)
    con.close()
    return out


def test_reconciliation_passes_on_a_faithful_import(exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.close()
    result = reconcile.reconcile(_source_totals(exported), _target_totals(target, TARGET_TABLES))
    assert result["ok"] is True, result["differences"]
    assert result["source_rows"] == result["target_rows"] == 7


def test_one_paisa_of_difference_fails_the_reconciliation(exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.execute("UPDATE budget_line SET amount_paise = amount_paise + 1 "
                "WHERE budget_line_id = 'BL-2'")
    con.close()
    result = reconcile.reconcile(_source_totals(exported), _target_totals(target, TARGET_TABLES))
    assert result["ok"] is False
    assert any("+1 paise" in d for d in result["differences"])
    with pytest.raises(reconcile.ReconciliationError):
        reconcile.assert_reconciled(result)


def test_two_errors_that_cancel_are_caught_by_the_per_dimension_check(
        exported: Path, tmp_path: Path):
    """The reason the reconciliation is not one SELECT SUM.

    A hundred rupees moved from PRJ-1 to PRJ-2 leaves every table total
    identical. Only the per-dimension totals see it.
    """
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.execute("UPDATE budget_line SET amount_paise = amount_paise - 10000 "
                "WHERE budget_line_id = 'BL-1'")
    con.execute("UPDATE budget_line SET amount_paise = amount_paise + 10000 "
                "WHERE budget_line_id = 'BL-3'")
    con.close()

    source, target_totals = _source_totals(exported), _target_totals(target, TARGET_TABLES)
    assert source["budget_line"].totals == target_totals["budget_line"].totals   # unchanged!
    result = reconcile.reconcile(source, target_totals)
    assert result["ok"] is False
    assert any("project_id=PRJ-1" in d for d in result["differences"])
    assert any("project_id=PRJ-2" in d for d in result["differences"])


def test_a_missing_row_is_caught_by_the_row_count(exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.execute("DELETE FROM budget_line WHERE budget_line_id = 'BL-2'")
    con.close()
    result = reconcile.reconcile(_source_totals(exported), _target_totals(target, TARGET_TABLES))
    assert any("row count 3 -> 2" in d for d in result["differences"])


def test_reconciliation_refuses_a_float_rather_than_averaging_over_it():
    with pytest.raises(reconcile.ReconciliationError, match="integer count of paise"):
        reconcile.totals_from_records("t", ["amount_paise"], [{"amount_paise": 12.5}])


def test_reconciliation_refuses_a_decimal_from_an_uncast_sum():
    """psycopg returns PostgreSQL ``SUM(bigint)`` as a Decimal. A migration that
    silently ``int()``s it would be absorbing a schema defect."""
    from decimal import Decimal
    with pytest.raises(reconcile.ReconciliationError):
        reconcile.totals_from_records("t", ["amount_paise"], [{"amount_paise": Decimal(5)}])


def test_the_reconciliation_reports_every_difference_not_just_the_first(
        exported: Path, tmp_path: Path):
    target = tmp_path / "t.db"
    con = build_target(target)
    import_pg.import_tables(con, exported, pg_tables=TARGET_TABLES, execute=sqlite_execute)
    con.execute("UPDATE budget_line SET amount_paise = 0")
    con.execute("UPDATE entity SET name = 'x'")
    con.close()
    result = reconcile.reconcile(_source_totals(exported), _target_totals(target, TARGET_TABLES))
    assert len(result["differences"]) > 1


def test_the_declared_tolerance_is_zero():
    result = reconcile.reconcile({}, {})
    assert "One paisa" in result["tolerance"]


# --------------------------------------------------------- the audit_log target
def test_the_audit_log_conflict_target_is_the_unique_key_not_the_identity_column():
    """``audit_id`` is GENERATED ALWAYS AS IDENTITY and is never written, so an
    ON CONFLICT on it would be a runtime error rather than a weaker guard."""
    tables = scan_pg_schema()
    columns = ["stream_key", "seq", "at", "actor", "action", "object_type",
               "object_id", "detail", "prev_hash", "entry_hash"]
    assert import_pg._conflict_columns(tables["audit_log"], columns) == ["stream_key", "seq"]
    sql = import_pg.insert_statement("audit_log", columns, ["stream_key", "seq"])
    assert "audit_id" not in sql
    assert 'ON CONFLICT ("stream_key", "seq") DO NOTHING' in sql


# --------------------------------------------------------------- Fable 5.1
def test_the_ddl_scanner_sees_columns_added_by_alter_table():
    """Migrations 014 onwards add columns with ``ALTER TABLE ... ADD COLUMN``.
    The scanner read only ``CREATE TABLE`` bodies, so ten such columns were
    invisible to the preflight and the live comparison failed the first time
    it executed against a server. Database-free: the scan reads the SQL."""
    from tools.migration.schema import scan_pg_schema
    tables = scan_pg_schema()
    assert "reopen_count" in tables["accounting_period"].column_names
    assert "reopened_by" in tables["accounting_period"].column_names
    assert "connection_id" in tables["bill"].column_names
    assert "bill_number_normalised" in tables["bill"].column_names
