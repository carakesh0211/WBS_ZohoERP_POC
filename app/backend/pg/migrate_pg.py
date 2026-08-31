"""PostgreSQL migration runner.

    python -m app.backend.pg.migrate_pg --status
    python -m app.backend.pg.migrate_pg --upgrade
    python -m app.backend.pg.migrate_pg --fresh --seed     # disposable DBs only

**This is a deploy step, never a boot step.** DEF-01 was exactly that mistake:
`app/run.py` called `migrate.upgrade()` unconditionally whenever a database file
existed, so a deployment carrying a pre-migration-runner database failed to
start with `table entity already exists`. An application that migrates itself on
boot cannot be rolled back, cannot be scaled horizontally without a race, and
turns a schema error into an outage.

:func:`assert_schema_current` is the boot-time counterpart: it *checks* and
refuses to serve, but never writes. It is built on :func:`read_only_status`,
not :func:`status` -- the difference matters: :func:`status` bootstraps the
``schema_migrations`` ledger with ``CREATE TABLE IF NOT EXISTS`` so the CLI
always has somewhere to write, but that is itself a write, and a boot-time
check must never issue one, not even a idempotent no-op DDL statement.

Each migration is applied once, inside a transaction, with its SHA-256 recorded.
An already-applied file whose contents have changed is a hard error -- silently
tolerating it is how two environments diverge without anyone noticing.

**Adopting a pre-existing schema.** A database can carry a migration's tables
without carrying a record of it in ``schema_migrations`` -- exactly the shape
of DEF-01's legacy SQLite database, and just as possible here if a schema was
ever hand-applied or restored from a dump taken before this runner existed.
Replaying that migration's DDL against such a database fails with a duplicate
object error. :func:`upgrade` treats that failure as adoptable, not fatal:
if every object the migration would have created is already present, it
records the migration as satisfied instead of raising. It does not replay
partial DDL, and it does not guess when only *some* objects are present --
that is a genuine conflict, not an adoption, and it still raises.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
import psycopg.errors

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "pg"
_FILENAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      text PRIMARY KEY,
    name         text NOT NULL,
    checksum     text NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user,
    duration_ms  integer
);
"""


class MigrationError(RuntimeError):
    """Refuses to proceed. Never leaves a half-applied schema behind."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        # Line-ending normalised: the same file must hash identically on a
        # Windows workstation and a Linux CI runner. The styles.css pin learned
        # this the hard way.
        body = self.path.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(body).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    if not directory.is_dir():
        return []
    found: list[Migration] = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".sql":
            continue
        match = _FILENAME.match(entry.name)
        if not match:
            raise MigrationError(
                f"{entry.name} does not match NNN_lower_snake.sql; "
                f"ordering must be unambiguous")
        found.append(Migration(match.group(1), match.group(2), entry))
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    return found


def applied(con: psycopg.Connection) -> dict[str, str]:
    con.execute(BOOTSTRAP)
    rows = con.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {version: checksum for version, checksum in rows}


def _ledger_table_exists(con: psycopg.Connection) -> bool:
    """Pure SELECT. Never CREATE -- that is the whole point of the read-only path."""
    row = con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def _status_from(known: dict[str, str], directory: Path) -> dict:
    migrations = discover(directory)
    pending, drifted = [], []
    for migration in migrations:
        recorded = known.get(migration.version)
        if recorded is None:
            pending.append(migration.version)
        elif recorded != migration.checksum:
            drifted.append(migration.version)
    return {
        "applied": sorted(known),
        "pending": pending,
        "drifted": drifted,
        "current": max(known, default=None),
        "latest_available": migrations[-1].version if migrations else None,
        "is_current": not pending and not drifted,
    }


def status(con: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> dict:
    """Bootstraps the ledger table if it is missing. CLI / deploy-step use only."""
    return _status_from(applied(con), directory)


def read_only_status(con: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> dict:
    """Same shape as :func:`status`, but never writes -- not even the ledger's
    ``CREATE TABLE IF NOT EXISTS``. Used by :func:`assert_schema_current`, the
    boot-time check, which must be safe to call against a database this process
    has no write intent toward at all.
    """
    if not _ledger_table_exists(con):
        migrations = discover(directory)
        return {
            "applied": [],
            "pending": [m.version for m in migrations],
            "drifted": [],
            "current": None,
            "latest_available": migrations[-1].version if migrations else None,
            "is_current": not migrations,
        }
    rows = con.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    known = {version: checksum for version, checksum in rows}
    return _status_from(known, directory)


#: SQLSTATE class 42 "duplicate object" errors -- the shape a migration's DDL
#: raises when the object it would create is already there. Not every class-42
#: error is adoptable (a genuine syntax error is not), so only these specific,
#: narrow "already exists" cases are treated as a possible adoption.
_DUPLICATE_OBJECT_ERRORS = (
    psycopg.errors.DuplicateTable,
    psycopg.errors.DuplicateObject,
    psycopg.errors.DuplicateSchema,
    psycopg.errors.DuplicateColumn,
    psycopg.errors.DuplicateFunction,
)

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)


def _tables_created_by(migration: "Migration") -> list[str]:
    """Best-effort: the table names a migration's ``CREATE TABLE`` statements
    would produce, in the order they appear. Used only to verify an adoption
    candidate, never to decide what to execute."""
    return _CREATE_TABLE_RE.findall(migration.sql)


def _all_tables_present(con: psycopg.Connection, tables: list[str]) -> bool:
    """True only if EVERY named table already exists. Partial overlap is a
    genuine conflict, not an adoption, and must not be papered over."""
    if not tables:
        return False
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (tables,),
    ).fetchall()
    present = {row[0] for row in rows}
    return set(tables) <= present


def _record_migration(con: psycopg.Connection, migration: "Migration", *, duration_ms: int) -> None:
    con.execute(
        "INSERT INTO schema_migrations (version, name, checksum, duration_ms) "
        "VALUES (%s, %s, %s, %s)",
        (migration.version, migration.name, migration.checksum, duration_ms))


def upgrade(con: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration, each in its own transaction.

    Idempotent for an adopted database: if a migration's DDL fails because its
    objects already exist, and every one of those objects is confirmed present,
    the migration is recorded as satisfied rather than replayed. This is the
    Phase 1 counterpart to DEF-01's recommendation -- see the module docstring.
    """
    known = applied(con)
    con.commit()
    performed: list[str] = []

    for migration in discover(directory):
        recorded = known.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise MigrationError(
                    f"migration {migration.version}_{migration.name} was already "
                    f"applied but its contents have changed. Never edit an applied "
                    f"migration -- add a new one. Environments have now diverged.")
            continue

        started = time.monotonic()
        try:
            con.execute(migration.sql)
        except _DUPLICATE_OBJECT_ERRORS as exc:
            con.rollback()
            tables = _tables_created_by(migration)
            if not _all_tables_present(con, tables):
                raise MigrationError(
                    f"migration {migration.version}_{migration.name} could not be "
                    f"applied ({type(exc).__name__}: {exc}), and its expected "
                    f"objects are not all present in the database either -- this "
                    f"is a genuine conflict, not an adoptable legacy schema. "
                    f"Refusing to guess.") from exc
            # Baseline/adopt step: the schema already carries this migration's
            # effect (an adopted legacy database) -- record it as satisfied
            # rather than replaying DDL that has already run.
            try:
                _record_migration(con, migration, duration_ms=0)
                con.commit()
            except Exception:
                con.rollback()
                raise
            performed.append(f"{migration.version} (adopted)")
            continue
        except Exception:
            con.rollback()
            raise

        try:
            _record_migration(con, migration,
                               duration_ms=int((time.monotonic() - started) * 1000))
            con.commit()
        except Exception:
            con.rollback()
            raise
        performed.append(migration.version)
    return performed


def assert_schema_current(con: psycopg.Connection,
                          directory: Path = MIGRATIONS_DIR) -> None:
    """Boot-time check. Reads only; never migrates; never writes -- not even
    the ledger table's own bootstrap DDL, which is why this calls
    :func:`read_only_status` and not :func:`status`.

    The counterpart to DEF-01's fix: the application refuses to serve against a
    schema it does not recognise, and says what to run, rather than trying to
    repair the database underneath itself.
    """
    state = read_only_status(con, directory)
    if state["drifted"]:
        raise MigrationError(
            f"schema drift: applied migrations {state['drifted']} no longer match "
            f"the files on disk")
    if state["pending"]:
        raise MigrationError(
            f"database is behind: {state['pending']} not applied. "
            f"Run `python -m app.backend.pg.migrate_pg --upgrade` as a deploy "
            f"step. The application does not migrate itself.")


def fresh(con: psycopg.Connection, directory: Path = MIGRATIONS_DIR,
          *, seed: bool = False, allow_non_disposable: bool = False) -> list[str]:
    """Drop and rebuild. Refuses anything that does not look disposable."""
    name = con.execute("SELECT current_database()").fetchone()[0]
    if not allow_non_disposable and not _is_disposable(name):
        raise MigrationError(
            f"refusing --fresh against database {name!r}: name does not match a "
            f"disposable pattern (capex_test*, capex_t<n>, capex_tmpl_*, *_dev)")
    con.execute("DROP SCHEMA IF EXISTS public CASCADE")
    con.execute("CREATE SCHEMA public")
    con.commit()
    performed = upgrade(con, directory)
    if seed:
        seed_file = directory / "seed_demo.sql"
        if seed_file.is_file():
            con.execute(seed_file.read_text(encoding="utf-8"))
            con.commit()
    return performed


def _is_disposable(name: str) -> bool:
    return bool(
        re.match(r"^capex_test", name)
        or re.match(r"^capex_t\d+$", name)
        or re.match(r"^capex_tmpl_", name)
        or name.endswith("_dev")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--allow-non-disposable", action="store_true")
    args = parser.parse_args(argv)

    from .config import from_env
    config = from_env()
    dsn = config.dsn()  # secret; handed straight to the driver
    try:
        with psycopg.connect(dsn, autocommit=False) as con:
            if args.fresh:
                done = fresh(con, seed=args.seed,
                             allow_non_disposable=args.allow_non_disposable)
                print(f"rebuilt: applied {done or 'nothing'}")
            elif args.upgrade:
                done = upgrade(con)
                print(f"applied: {done or 'nothing pending'}")
            else:
                import json
                print(json.dumps(status(con), indent=2))
    except MigrationError as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[3]))
    raise SystemExit(main())
