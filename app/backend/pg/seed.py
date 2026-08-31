"""Demo-profile guard and loader for ``migrations/pg/seed_demo.sql``.

Modelled on the SQLite side's convention (``app.backend.auth.is_demo_profile``,
gated on ``CAPEX_PROFILE == "local-demo"``) but **stricter**: seeding a
PostgreSQL database additionally requires the database's own NAME to prove it
disposable, and requires the database to be free of business rows this seed
did not itself create.

Three independent checks, in this order, each of which raises
:class:`SeedGuardError` -- never a log-and-continue -- on failure:

1. **Profile.** ``CAPEX_PROFILE`` must literally equal ``local-demo``.
   Unconditional. ``--force`` does not touch this line.
2. **Disposable name.** ``current_database()`` must match
   ``^capex_t\\d+$`` (a per-test database) or ``^capex_tmpl_`` (a session
   template) -- the exact discipline
   ``tests/conftest_pg.py::_assert_pg_disposable`` enforces for the test
   suite. Mirrored here rather than imported: application code under
   ``app/backend`` must not depend on ``tests/``, so the two are kept in
   step by convention, not by a shared import. Unconditional; ``--force``
   does not touch this line either.
3. **Emptiness.** None of the tables this seed populates may already hold a
   row. This is the ONLY check ``force=True`` widens -- and even then it
   never deletes, truncates or otherwise touches existing data itself; it
   only skips the pre-flight refusal, so a database that genuinely does
   still hold rows fails loudly on Postgres's own primary-key violation
   rather than silently merging into someone else's data.

There is deliberately no environment variable that reaches all the way to
"seed production": the profile and name checks are unconditional code in
this module. The only way to make this loader accept a production database
is to edit this file.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

SEED_FILE = Path(__file__).resolve().parents[3] / "migrations" / "pg" / "seed_demo.sql"

# Mirrors tests/conftest_pg.py::_assert_pg_disposable exactly (read, not
# imported -- see module docstring). Keep the two patterns in step by hand;
# they are asserted to match by tests/test_pg_seed.py.
_PER_TEST_DB_RE = re.compile(r"^capex_t\d+$")
_TEMPLATE_DB_RE = re.compile(r"^capex_tmpl_")

#: Tables seed_demo.sql populates, in an order safe to probe (no ordering
#: significance beyond "all must be checked" -- this is a read-only scan).
SEEDED_TABLES = (
    "organisation", "entity", "app_user", "accounting_period", "budget_head",
    "project", "wbs_element", "budget_control_cell", "budget_ledger_cell",
    "audit_log",
)


class SeedGuardError(RuntimeError):
    """Seeding refused. Always raised, never logged-and-continued."""


def is_demo_profile() -> bool:
    """Same convention as ``app.backend.auth.is_demo_profile``."""
    return os.environ.get("CAPEX_PROFILE", "").lower() == "local-demo"


def _is_disposable_name(name: str) -> bool:
    return bool(_PER_TEST_DB_RE.match(name) or _TEMPLATE_DB_RE.match(name))


def _assert_profile() -> None:
    """Check 1. Touches no connection at all, so it can run -- and refuse --
    with no database available whatsoever."""
    if not is_demo_profile():
        raise SeedGuardError(
            "refusing to seed: CAPEX_PROFILE is not 'local-demo'. Seeding is "
            "only ever possible against an explicit local demo profile; set "
            "CAPEX_PROFILE=local-demo if this really is a disposable demo "
            "database. This check is unconditional and is never affected by "
            "--force.")


def _assert_disposable_database(connection: psycopg.Connection) -> str:
    """Check 2. Returns the database name for logging/tests, never a secret."""
    row = connection.execute("SELECT current_database()").fetchone()
    name = row[0] if row else ""
    if not _is_disposable_name(name):
        raise SeedGuardError(
            f"refusing to seed database {name!r}: its name does not match a "
            f"disposable pattern (^capex_t\\d+$ for a per-test database, or "
            f"^capex_tmpl_ for a session template) -- the same discipline "
            f"tests/conftest_pg.py::_assert_pg_disposable enforces for the "
            f"test suite. This check is unconditional and is never affected "
            f"by --force.")
    return name


def _assert_empty(connection: psycopg.Connection) -> None:
    """Check 3. The only check ``force`` can skip -- and even then, force
    never deletes anything; it only lets a genuinely non-empty database reach
    Postgres itself, which then raises its own primary-key violation rather
    than silently merging rows."""
    for table in SEEDED_TABLES:
        exists = connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,)).fetchone()
        if exists is None:
            raise SeedGuardError(
                f"refusing to seed: table {table!r} does not exist. Run "
                f"migrations first "
                f"(python -m app.backend.pg.migrate_pg --upgrade) before "
                f"seeding.")
        row = connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # noqa: S608 -- table from a fixed allow-list, never user input
        if row is not None:
            raise SeedGuardError(
                f"refusing to seed: table {table!r} already contains rows "
                f"this seed did not create. Seeding never merges into "
                f"existing data. Use a fresh disposable database, or pass "
                f"force=True / --force to skip this pre-flight check (a "
                f"database that genuinely still holds conflicting rows will "
                f"then fail on Postgres's own primary-key violation instead "
                f"of silently merging).")


SEED_PARTS_DIR = SEED_FILE.parent / "seed_parts"


def seed_part_files(parts_dir: Path = SEED_PARTS_DIR) -> list[Path]:
    """Fragment files, in filename order, loaded after seed_demo.sql.

    Each backend stream owns one. They live in a SUBDIRECTORY because
    migrate_pg.discover() scans only top-level *.sql entries -- putting a data
    file beside the migrations broke discovery once already.

    Filename order is the load order, which is why the fragments carry their
    migration's number: 003_budget.sql needs the tables 003_budget_planning.sql
    creates, and needs the rows seed_demo.sql inserted.
    """
    if not parts_dir.is_dir():
        return []
    return sorted(f for f in parts_dir.iterdir() if f.suffix == ".sql")


def seed(connection: psycopg.Connection, *, force: bool = False,
          seed_file: Path = SEED_FILE,
          parts_dir: Path = SEED_PARTS_DIR) -> None:
    """Load ``seed_file`` (default: ``migrations/pg/seed_demo.sql``) into
    ``connection``.

    Impossible unless BOTH the profile check and the disposable-name check
    pass -- neither is affected by ``force``. ``force`` only skips the
    pre-flight emptiness check (3); it never deletes, truncates or otherwise
    mutates any pre-existing row, and it never widens checks 1 or 2.

    Does not commit. Callers own the transaction boundary, exactly like
    ``migrate_pg.upgrade``/``migrate_pg.fresh``.
    """
    _assert_profile()
    _assert_disposable_database(connection)
    if not force:
        _assert_empty(connection)

    if not seed_file.is_file():
        raise SeedGuardError(f"seed file not found: {seed_file}")

    connection.execute(seed_file.read_text(encoding="utf-8"))

    # Fragments load AFTER the base seed, in filename order. They are part of
    # the demo estate, not an optional extra: a fragment that silently never
    # loads leaves every screen built against it showing an empty state, which
    # reads as "no data yet" rather than as the defect it is.
    for part in seed_part_files(parts_dir):
        connection.execute(part.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                         help="skip the pre-flight emptiness check only; "
                              "cannot bypass the profile or disposable-name checks")
    args = parser.parse_args(argv)

    from .config import from_env
    config = from_env()
    dsn = config.dsn()  # secret; handed straight to the driver
    try:
        with psycopg.connect(dsn, autocommit=False) as con:
            seed(con, force=args.force)
            con.commit()
            loaded = ["migrations/pg/seed_demo.sql"]
            loaded += [f"migrations/pg/seed_parts/{f.name}"
                       for f in seed_part_files()]
            print("seeded: " + ", ".join(loaded))
    except SeedGuardError as exc:
        print(f"SEED REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[3]))
    raise SystemExit(main())
