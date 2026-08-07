"""Schema migration runner.

    python -m app.backend.migrate --status
    python -m app.backend.migrate --upgrade            # apply pending migrations
    python -m app.backend.migrate --fresh --seed       # rebuild a development DB

Migration 001 is the original POC schema (kept in db.SCHEMA so there is a single
definition). 002 onwards are SQL files in ``migrations/``.

The original demo database is never altered in place: ``--upgrade`` copies the
target to ``<db>.pre-<version>.bak`` before running, and refuses to touch a file
it has not backed up.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(HERE, "migrations")

if __package__ in (None, ""):                       # allow direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from app.backend import db as dbmod                 # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
        version     TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        applied_at  TEXT NOT NULL,
        checksum    TEXT
    )""")


def discover() -> list[tuple[str, str, str]]:
    """[(version, name, sql_or_None)] in order. 001 comes from db.SCHEMA."""
    out = [("001", "initial_schema", None)]
    for path in sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))):
        base = os.path.basename(path)
        version, _, rest = base.partition("_")
        out.append((version, rest.replace(".sql", ""), path))
    return out


def applied(con: sqlite3.Connection) -> set[str]:
    _ensure_table(con)
    return {r[0] for r in con.execute("SELECT version FROM schema_migration")}


def status(path: str) -> list[tuple[str, str, bool]]:
    if not os.path.exists(path):
        return [(v, n, False) for v, n, _ in discover()]
    con = sqlite3.connect(path)
    try:
        done = applied(con)
        return [(v, n, v in done) for v, n, _ in discover()]
    finally:
        con.close()


def _apply(con: sqlite3.Connection, version: str, name: str, sql_path: str | None) -> None:
    if sql_path is None:
        con.executescript(dbmod.SCHEMA)
        checksum = str(len(dbmod.SCHEMA))
    else:
        with open(sql_path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        con.executescript(sql)
        checksum = str(len(sql))
    con.execute("INSERT INTO schema_migration (version, name, applied_at, checksum) VALUES (?,?,?,?)",
                (version, name, _now(), checksum))


def upgrade(path: str, *, backup: bool = True) -> list[str]:
    """Apply pending migrations to an existing database, backing it up first."""
    if not os.path.exists(path):
        raise SystemExit(f"No database at {path}. Use --fresh to create one.")
    con = sqlite3.connect(path)
    try:
        pending = [(v, n, p) for v, n, p in discover() if v not in applied(con)]
    finally:
        con.close()
    if not pending:
        return []
    if backup:
        tag = pending[-1][0]
        dest = f"{path}.pre-{tag}.bak"
        shutil.copy2(path, dest)
        print(f"  backup -> {dest}")

    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = OFF")        # table rebuilds re-point FKs
    done = []
    try:
        for version, name, sql_path in pending:
            con.execute("BEGIN")
            _apply(con, version, name, sql_path)
            con.commit()
            done.append(f"{version}_{name}")
            print(f"  applied {version} {name}")
        con.execute("PRAGMA foreign_keys = ON")
        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"foreign key violations after migration: {broken[:5]}")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return done


def fresh(path: str, *, seed: bool = True) -> str:
    """Build a new database from migration 001 upward. Never touches an existing file."""
    if os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved = f"{path}.replaced-{stamp}"
        shutil.move(path, moved)
        print(f"  existing database preserved -> {moved}")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if seed:
        # Seed against the v1 schema, then migrate, so the migration path itself
        # is exercised on realistic data rather than an empty database.
        prev = dbmod.DB_PATH
        try:
            dbmod.DB_PATH = path
            dbmod.reset_and_seed()
        finally:
            dbmod.DB_PATH = prev
        con = sqlite3.connect(path)
        try:
            _ensure_table(con)
            con.execute("INSERT OR IGNORE INTO schema_migration VALUES ('001','initial_schema',?,?)",
                        (_now(), str(len(dbmod.SCHEMA))))
            con.commit()
        finally:
            con.close()
        upgrade(path, backup=False)
    else:
        con = sqlite3.connect(path)
        try:
            con.execute("BEGIN")
            _ensure_table(con)
            for version, name, sql_path in discover():
                _apply(con, version, name, sql_path)
            con.commit()
        finally:
            con.close()
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="CAPEX & WBS Control Hub schema migrations")
    ap.add_argument("--db", default=dbmod.DB_PATH)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--upgrade", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--seed", action="store_true", help="with --fresh, load the demo dataset")
    args = ap.parse_args()

    if args.status or not (args.upgrade or args.fresh):
        print(f"database: {args.db}")
        for v, n, ok in status(args.db):
            print(f"  [{'x' if ok else ' '}] {v}  {n}")
        return
    if args.fresh:
        print(f"building {args.db}")
        fresh(args.db, seed=args.seed)
    elif args.upgrade:
        print(f"upgrading {args.db}")
        if not upgrade(args.db):
            print("  already current")
    for v, n, ok in status(args.db):
        print(f"  [{'x' if ok else ' '}] {v}  {n}")


if __name__ == "__main__":
    main()
