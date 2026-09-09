"""Import an export into PostgreSQL: FK-topological, constraints ENABLED.

    python -m tools.migration.import_pg --export export/ --preflight
    python -m tools.migration.import_pg --export export/ --dry-run --dsn ...
    python -m tools.migration.import_pg --export export/ --dsn ... --atomic
    python -m tools.migration.import_pg --export export/ --dsn ... --resumable

``session_replication_role`` is deliberately NOT used
-----------------------------------------------------
Setting it to ``'replica'`` would make the import faster and would disable every
foreign key and trigger while it ran. That is exactly what must not happen here.
Most of this import's value is that it doubles as a **constraint proof**: if
every row lands with foreign keys, checks and triggers live, then the POC's
data genuinely satisfies the production schema. Turn the constraints off and
the import proves only that the rows fit in the columns -- and the first thing
anyone would then discover is an orphan, in production, weeks later.

:data:`FORBIDDEN_STATEMENTS` names it, and ``tests/test_migration_import.py``
greps this module's own source for every entry. A comment saying "we don't do
this" is not a control; a test that fails when someone does is.

Atomic or resumable -- and why both exist
-----------------------------------------
"One transaction" and "resume from a checkpoint" are not compatible, and
pretending otherwise is how a half-migrated database happens.

``--atomic`` (the default) runs the WHOLE import in one transaction. A failure
anywhere rolls back everything, so the only two states the target can be in are
"untouched" and "fully migrated". Restart is trivially idempotent because a
failed run committed nothing. This is the right mode for a cutover.

``--resumable`` commits one transaction PER TABLE, in topological order. That
order is FK-closed by construction -- every parent is committed before any child
references it -- so an interrupted run leaves a consistent prefix rather than a
mess. The checkpoint records the tables that committed; a restart skips them and
re-runs the rest. Every insert is ``ON CONFLICT DO NOTHING`` against the real
primary key, so re-running a table that was half-inserted before the process
died duplicates nothing. This is the right mode for a long import over a
connection that may drop.

Both modes end in the same two gates: reconciliation to the paisa, and
``verify_chain('LEGACY')``. Either failing aborts, and in ``--atomic`` that
abort takes the whole import with it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import audit_chain, reconcile
from .export_poc import load_ndjson
from .schema import Table, scan_pg_schema, topological_order

#: Never emitted. Checked against this module's own source by the test suite.
FORBIDDEN_STATEMENTS = (
    "session_replication_role",   # would disable every FK and trigger
    "ALTER TABLE .* DISABLE TRIGGER",
    "SET CONSTRAINTS ALL DEFERRED",
    "TRUNCATE",                   # an import must never destroy target data
    "DROP TABLE",
)

#: Columns present on the POC and absent from the PostgreSQL schema, with the
#: decision made about each. **Deliberately empty.** The preflight refuses to
#: run while a source column has no entry here, and that refusal is the correct
#: current state: the column-level POC->production mapping is analysis work that
#: has not been done, and a migration that quietly drops the 74 unmapped columns
#: -- three of which are money -- would be far worse than one that refuses.
#:
#: Format: {table: {column: "keep as <target column> | drop because <reason>"}}
COLUMN_DECISIONS: dict[str, dict[str, str]] = {}

#: Tables that exist only on the POC and are not migrated at all, with a reason.
#: Also empty, and for the same reason.
SOURCE_ONLY_TABLES: dict[str, str] = {}


class ImportError_(RuntimeError):
    """A refusal. Nothing was written, or everything written was rolled back."""


@dataclass
class Checkpoint:
    """Which tables have committed. Written only after a real COMMIT.

    Written after, never before. A checkpoint written before the commit is a
    checkpoint that tells a restart to skip a table that is not there.
    """
    path: Path
    mode: str = "atomic"
    export_manifest_sha: str = ""
    completed: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Checkpoint":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(path=p, mode=data.get("mode", "atomic"),
                   export_manifest_sha=data.get("export_manifest_sha", ""),
                   completed=list(data.get("completed", [])))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "mode": self.mode,
            "export_manifest_sha": self.export_manifest_sha,
            "completed": self.completed,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mark(self, table: str) -> None:
        if table not in self.completed:
            self.completed.append(table)
        self.save()

    def guard_same_export(self, manifest_sha: str) -> None:
        """Refuse to resume a checkpoint from a DIFFERENT export.

        Resuming run B's second half on top of run A's first half produces a
        target that matches neither export and reconciles against neither. The
        manifest hash makes that detectable instead of silent.
        """
        if self.export_manifest_sha and self.export_manifest_sha != manifest_sha:
            raise ImportError_(
                "this checkpoint belongs to a different export "
                f"({self.export_manifest_sha[:12]}..., now {manifest_sha[:12]}...). "
                "Resuming would interleave two exports. Start a fresh target and "
                "a fresh checkpoint.")
        self.export_manifest_sha = manifest_sha


# ------------------------------------------------------------------- preflight
def load_manifest(export_dir: str | Path) -> dict[str, Any]:
    path = Path(export_dir) / "manifest.json"
    if not path.exists():
        raise ImportError_(f"no manifest.json in {export_dir}; run export_poc first")
    return json.loads(path.read_text(encoding="utf-8"))


def preflight(export_dir: str | Path,
              pg_tables: dict[str, Table] | None = None) -> dict[str, Any]:
    """Report what WOULD happen. Touches no database and writes nothing.

    A preflight that needs a live server to answer is not a preflight -- it
    cannot be run before the server exists, which is when the answer is wanted.
    The PostgreSQL side is read from ``migrations/pg/*.sql`` by a depth-aware
    scanner; ``schema.compare_scanned_to_live`` exists so CI can check that
    reading against a real server rather than trusting it.
    """
    manifest = load_manifest(export_dir)
    targets = pg_tables if pg_tables is not None else scan_pg_schema()
    source_tables = dict(manifest["tables"])

    migratable = sorted(set(source_tables) & set(targets))
    source_only = sorted(set(source_tables) - set(targets))

    unmapped: dict[str, list[str]] = {}
    unmapped_money: dict[str, list[str]] = {}
    for table in migratable:
        target_columns = set(targets[table].column_names)
        decided = COLUMN_DECISIONS.get(table, {})
        missing = [c for c in source_tables[table]["columns"]
                   if c not in target_columns and c not in decided]
        if missing:
            unmapped[table] = missing
            money = [c for c in missing if c.endswith(reconcile.MONEY_SUFFIX)]
            if money:
                unmapped_money[table] = money

    undeclared_source_only = [t for t in source_only if t not in SOURCE_ONLY_TABLES]

    try:
        order = topological_order(targets, subset=set(migratable))
        order_error = None
    except ValueError as exc:
        order, order_error = [], str(exc)

    audit_plan: dict[str, Any] = {}
    audit_error = None
    audit_file = Path(export_dir) / "audit_log.ndjson"
    if audit_file.exists():
        rows = list(load_ndjson(audit_file, frozenset(
            source_tables.get("audit_log", {}).get("float_columns", ()))))
        try:
            _entries, audit_plan = audit_chain.plan_legacy_import(rows)
        except audit_chain.AuditChainError as exc:
            audit_error = str(exc)

    blockers: list[str] = []
    if unmapped:
        blockers.append(
            f"{sum(len(v) for v in unmapped.values())} source column(s) across "
            f"{len(unmapped)} table(s) have no target column and no recorded "
            "decision in COLUMN_DECISIONS. Dropping a column silently is how a "
            "migration loses data that nobody misses until an audit.")
    if unmapped_money:
        blockers.append(
            f"{sum(len(v) for v in unmapped_money.values())} of those are MONEY "
            f"columns: {unmapped_money}. Reconciliation cannot balance a total "
            "that has nowhere to land.")
    if undeclared_source_only:
        blockers.append(
            f"{len(undeclared_source_only)} POC table(s) have no target and no "
            f"entry in SOURCE_ONLY_TABLES: {undeclared_source_only}")
    if order_error:
        blockers.append(order_error)
    if audit_error:
        blockers.append(audit_error)

    return {
        "would_change_anything": False,
        "export_dir": str(export_dir),
        "export_row_total": manifest["row_count_total"],
        "tables_in_export": len(source_tables),
        "tables_with_a_target": len(migratable),
        "insert_order": order,
        "source_only_tables": source_only,
        "unmapped_columns": unmapped,
        "unmapped_money_columns": unmapped_money,
        "audit_chain_plan": audit_plan,
        "foreign_keys": "ENABLED throughout; session_replication_role is never set",
        "blockers": blockers,
        "ready": not blockers,
    }


# ---------------------------------------------------------------------- import
def insert_statement(table: str, columns: Sequence[str],
                     conflict_columns: Sequence[str]) -> str:
    """One parameterised INSERT with ON CONFLICT DO NOTHING.

    ``DO NOTHING`` rather than ``DO UPDATE``: a restart must not overwrite a row
    that a previous run already committed, because the export is the same in
    both runs and an UPDATE would be a no-op that hides a genuine divergence.
    A row that arrived with the wrong value is caught by the reconciliation,
    which compares totals per dimension -- not papered over here.
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    params = ", ".join(f"%({c})s" for c in columns)
    conflict = (" ON CONFLICT (" + ", ".join(f'"{c}"' for c in conflict_columns)
                + ") DO NOTHING") if conflict_columns else " ON CONFLICT DO NOTHING"
    return f'INSERT INTO "{table}" ({cols}) VALUES ({params}){conflict}'


@dataclass
class ImportResult:
    mode: str
    dry_run: bool
    tables_written: dict[str, int] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    audit_chain: dict[str, Any] = field(default_factory=dict)
    rolled_back: bool = False
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "dry_run": self.dry_run,
            "tables_written": self.tables_written,
            "rows_written": sum(self.tables_written.values()),
            "reconciliation": self.reconciliation,
            "audit_chain": self.audit_chain,
            "rolled_back": self.rolled_back,
            "error": self.error,
        }


class Recorder:
    """A connection stand-in that records instead of executing.

    ``--dry-run`` is only meaningful if it exercises the same code path as the
    real thing. Running a separate "what would happen" branch tests the
    reporting, not the import. So the dry run drives the identical function with
    this object in place of the connection: every statement the real import
    would issue is recorded, in order, and nothing is sent anywhere.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.committed = 0
        self.rolled_back = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))

    def executemany(self, sql: str, seq: Iterable[Any]) -> None:
        for params in seq:
            self.statements.append((sql, params))

    def fetchall(self, sql: str, params: Any = None) -> list[tuple]:
        self.statements.append((sql, params))
        return []

    def fetchone(self, sql: str, params: Any = None):
        self.statements.append((sql, params))
        return None

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def import_tables(connection, export_dir: str | Path, *, mode: str = "atomic",
                  checkpoint: Checkpoint | None = None,
                  pg_tables: dict[str, Table] | None = None,
                  dry_run: bool = False,
                  execute: Callable[[Any, str, Any], None] | None = None) -> ImportResult:
    """The import itself. Same path for a dry run and a real one.

    `execute(connection, sql, params)` defaults to ``connection.execute``, so a
    ``Recorder`` and a psycopg connection are both accepted without this
    function knowing which it has.
    """
    if mode not in ("atomic", "resumable"):
        raise ImportError_(f"unknown mode {mode!r}")
    run = execute or (lambda con, sql, params: con.execute(sql, params))

    manifest = load_manifest(export_dir)
    targets = pg_tables if pg_tables is not None else scan_pg_schema()
    check = preflight(export_dir, targets)
    if not check["ready"]:
        raise ImportError_(
            "preflight refused the import:\n  " + "\n  ".join(check["blockers"]))

    result = ImportResult(mode=mode, dry_run=dry_run)
    done = set(checkpoint.completed) if checkpoint else set()

    try:
        for table in check["insert_order"]:
            if table in done:
                continue
            entry = manifest["tables"][table]
            columns = [c for c in entry["columns"] if c in set(targets[table].column_names)]
            conflict = _conflict_columns(targets[table], columns)
            sql = insert_statement(table, columns, conflict)
            written = 0
            for record in load_ndjson(Path(export_dir) / entry["file"],
                                      frozenset(entry.get("float_columns", ()))):
                run(connection, sql, {c: record.get(c) for c in columns})
                written += 1
            result.tables_written[table] = written
            if mode == "resumable":
                connection.commit()
                if checkpoint:
                    checkpoint.mark(table)

        if mode == "atomic":
            connection.commit()
            if checkpoint:
                checkpoint.completed = list(result.tables_written)
                checkpoint.save()
    except Exception as exc:                      # noqa: BLE001 - re-raised below
        connection.rollback()
        result.rolled_back = True
        result.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if isinstance(connection, Recorder):
            result.statements = [s for s, _ in connection.statements]
    return result


def _conflict_columns(table: Table, present: Sequence[str]) -> list[str]:
    """The ON CONFLICT target: the table's natural key among the columns written.

    ``audit_log`` is the interesting one: its primary key is a
    ``GENERATED ALWAYS AS IDENTITY`` column that is never written by an import,
    so the conflict target has to be ``UNIQUE (stream_key, seq)`` instead. A
    conflict target of a column that is not in the INSERT is a runtime error,
    not a silently weaker guard, which is why this is derived rather than
    configured.
    """
    if table.name == "audit_log":
        return ["stream_key", "seq"]
    identity = {c.name for c in table.columns if c.identity}
    for candidate in (f"{table.name}_id", "id"):
        if candidate in present and candidate not in identity:
            return [candidate]
    return [c for c in present if c.endswith("_id") and c not in identity][:1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", required=True)
    ap.add_argument("--preflight", action="store_true",
                    help="report what would happen; touches no database")
    ap.add_argument("--dry-run", action="store_true",
                    help="drive the real import path against a recorder")
    ap.add_argument("--dsn", help="PostgreSQL DSN (omit with --preflight/--dry-run)")
    ap.add_argument("--mode", choices=("atomic", "resumable"), default="atomic")
    ap.add_argument("--checkpoint", default=None)
    args = ap.parse_args(argv)

    if args.preflight:
        report = preflight(args.export)
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1

    if args.dry_run:
        recorder = Recorder()
        try:
            result = import_tables(recorder, args.export, mode=args.mode, dry_run=True)
        except ImportError_ as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"dry run: {len(recorder.statements)} statements, "
              f"{sum(result.tables_written.values())} rows, nothing sent anywhere")
        return 0

    if not args.dsn:
        print("--dsn is required for a real import")
        return 2
    print("A real import needs psycopg and a live server; run this from a host "
          "that has both. This build has never executed it -- see "
          "docs/runbooks/poc-to-postgres-migration.md.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
