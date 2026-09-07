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

Table existence alone is not enough evidence: a legacy dump can carry every
table name from a migration while missing the triggers that make
``audit_log`` append-only, or carry a hand-applied ``budget_control_cell``
whose ``budget_paise`` column is ``numeric`` instead of ``bigint`` -- silently
adopting either loses a guarantee the domain depends on (see
``domain-controls.md``, "Money"). :func:`upgrade`'s adoption check therefore
verifies, best-effort via regex against the migration's own SQL, every table,
function, trigger, explicitly *named* constraint, the accounting_period-style
unnamed exclusion constraint, every named index, every row-level-security
policy together with whether RLS is actually ``ENABLE``d and ``FORCE``d, and --
the one that matters most -- that every ``*_paise`` column is ``bigint`` in the
database, not merely present. Where a class of object cannot be parsed reliably
(an unnamed ``UNIQUE``/``CHECK`` with no name to look up), it is not verified
rather than guessed at; this keeps the check honest about what it actually
confirmed.

The index and RLS checks were added with ``013_procurement.sql``, and each
closes a class the earlier ones structurally could not see:

* **Indexes.** Every external-document uniqueness guarantee in this schema --
  ``ux_bill_external``, ``ux_po_external``, ``ux_grn_external``,
  ``ux_po_line_external``, ``ux_grn_line_external``'s sibling
  ``ux_reconciliation_exception_open`` -- is a partial ``CREATE UNIQUE INDEX
  ... WHERE ...``, not a table constraint, so the named-constraint check cannot
  reach any of them. They are what makes an integration that re-walks by
  design (a 300-second sweep overlap, a cycling walk) idempotent instead of
  duplicating receipts. A dump missing one adopted cleanly.
* **RLS.** A database carrying every table with row-level security never
  enabled reads FULLY OPEN and, before this, reported itself adopted and
  current. ``ENABLE`` and ``FORCE`` are checked separately because they fail
  differently: without ``ENABLE`` no policy applies to anyone, and without
  ``FORCE`` every policy applies to everyone except the table's OWNER -- in
  production the deploy identity, the role most likely to be reused by a
  background job.
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

# migrations/pg/ can carry data files that are not migrations at all --
# seed_demo.sql (loaded by :func:`fresh`, on `--seed`, never by :func:`upgrade`
# or :func:`discover`'s callers) is the first one. `discover()` skips these by
# exact name rather than loosening `_FILENAME`: the regex's job is keeping
# real migration ordering unambiguous, and a data file is not a migration, so
# it should never be made to look like a well-formed one just to pass through
# the same filter. Add the next non-migration data file's name here, not a
# bare `if entry.name == ...` buried in the loop below.
NON_MIGRATION_FILES = {"seed_demo.sql"}

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
        if entry.name in NON_MIGRATION_FILES:
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

_CREATE_TABLE_START_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s*\(",
    re.IGNORECASE,
)

_CREATE_FUNCTION_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s*\(",
    re.IGNORECASE,
)

# Non-greedy up to the first `ON <table>` after the trigger name: PostgreSQL's
# CREATE TRIGGER grammar puts exactly one ON clause naming the table between
# the trigger name and its timing/event/FOR EACH ROW clauses, so this is safe
# even spanning the newlines these migrations format triggers across.
_CREATE_TRIGGER_RE = re.compile(
    r"CREATE\s+TRIGGER\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+.*?\bON\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE | re.DOTALL,
)

_NAMED_CONSTRAINT_RE = re.compile(
    r"^CONSTRAINT\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?", re.IGNORECASE,
)

_EXCLUDE_RE = re.compile(r"^EXCLUDE\b", re.IGNORECASE)

# `CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] name ON table`. Unlike
# an anonymous `UNIQUE (col)` inside a table body, an index in these migrations
# always carries a name, so there is always something reliable to look up.
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+ON\s+"
    r"(?:ONLY\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)

_CREATE_POLICY_RE = re.compile(
    r"CREATE\s+POLICY\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+ON\s+"
    r"\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)

_ENABLE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)

_FORCE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+FORCE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)

_PAISE_COLUMN_RE = re.compile(
    r"^\"?([a-zA-Z_][a-zA-Z0-9_]*_paise)\"?\s+\S", re.IGNORECASE,
)

# Regex parsing is best-effort by nature: it cannot see what the database
# actually decided a statement means, only what the SQL text looks like. A
# single-line `--` comment strip is enough for these migrations (none of
# their string literals contain `--`), but is not a general SQL parser, and
# is not asked to be one -- every helper below either finds a reliable
# anchor (a table's own `CREATE TABLE name (`, an explicit `CONSTRAINT name`)
# or declines to report anything for that object, per the module docstring.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _tables_created_by(migration: "Migration") -> list[str]:
    """Best-effort: the table names a migration's ``CREATE TABLE`` statements
    would produce, in the order they appear. Used only to verify an adoption
    candidate, never to decide what to execute.

    Comments are stripped FIRST, as every other parser in this module already
    does. Without that, a migration whose header prose contains the words
    ``CREATE TABLE`` followed by a word -- "...DELETE on all eight the instant
    each CREATE TABLE returned" -- reports a table named ``returned``, which
    exists nowhere, so ``_missing_tables`` reports it missing and the migration
    can never be adopted. A documentation sentence is not DDL.
    """
    return _CREATE_TABLE_RE.findall(_LINE_COMMENT_RE.sub("", migration.sql))


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


def _missing_tables(con: psycopg.Connection, tables: list[str]) -> list[str]:
    if not tables:
        return []
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (tables,),
    ).fetchall()
    present = {row[0] for row in rows}
    return [t for t in tables if t not in present]


def _extract_table_bodies(migration_sql: str) -> list[tuple[str, str]]:
    """``(table_name, body_text)`` for every ``CREATE TABLE`` statement in
    `migration_sql`, `body_text` being everything between the statement's
    outermost parentheses.

    Found by counting parens rather than a single regex, because a naive
    "match up to the next `)`" is wrong the moment a column or constraint
    definition nests its own parens -- ``CHECK (nlevel(wbs_path) <= 100)``,
    ``EXCLUDE USING gist (... daterange(...) ...)`` -- which every table in
    these migrations that carries a CHECK or EXCLUDE does.
    """
    text = _LINE_COMMENT_RE.sub("", migration_sql)
    bodies: list[tuple[str, str]] = []
    for match in _CREATE_TABLE_START_RE.finditer(text):
        table = match.group(1)
        start = match.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        bodies.append((table, text[start:i - 1]))
    return bodies


def _split_top_level(body: str) -> list[str]:
    """`body` split on commas at paren-depth 0 -- the column- and
    constraint-defining fields of a ``CREATE TABLE`` body, without being
    fooled by the commas inside a nested ``CHECK (...)``, ``EXCLUDE (...)``,
    or ``REFERENCES table (col)`` clause."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _functions_created_by(migration: "Migration") -> list[str]:
    """Best-effort: the function names a migration's ``CREATE [OR REPLACE]
    FUNCTION`` statements would produce."""
    text = _LINE_COMMENT_RE.sub("", migration.sql)
    return _CREATE_FUNCTION_RE.findall(text)


def _triggers_created_by(migration: "Migration") -> list[tuple[str, str]]:
    """Best-effort: ``(trigger_name, table_name)`` for every ``CREATE
    TRIGGER`` statement -- the specific thing failure A (audit immutability)
    loses if it is skipped: table presence alone says nothing about whether
    ``audit_log`` is still append-only."""
    text = _LINE_COMMENT_RE.sub("", migration.sql)
    return _CREATE_TRIGGER_RE.findall(text)


def _named_constraints_by(migration: "Migration") -> list[tuple[str, str]]:
    """Best-effort: ``(table_name, constraint_name)`` for every table
    constraint declared with an explicit ``CONSTRAINT name`` -- the form
    that gives a reliable ``pg_constraint.conname`` to look up. A plain
    ``UNIQUE (col)`` or inline column ``CHECK (...)`` has no name to find,
    so it is deliberately not reported here -- see the module docstring."""
    found: list[tuple[str, str]] = []
    for table, body in _extract_table_bodies(migration.sql):
        for field in _split_top_level(body):
            match = _NAMED_CONSTRAINT_RE.match(field)
            if match:
                found.append((table, match.group(1)))
    return found


def _exclusion_constraint_tables(migration: "Migration") -> list[str]:
    """Best-effort: tables carrying an ``EXCLUDE`` constraint. These
    migrations never name their exclusion constraints explicitly, so
    PostgreSQL auto-generates the name -- nothing reliable to look up by
    ``conname``. What IS reliable is that an exclusion constraint
    (``pg_constraint.contype = 'x'``) must exist on that table; that is
    what is checked. ``accounting_period``'s overlap-prevention exclusion is
    exactly this shape, and is the "at minimum" case this exists for."""
    found: list[str] = []
    for table, body in _extract_table_bodies(migration.sql):
        for field in _split_top_level(body):
            if _EXCLUDE_RE.match(field):
                found.append(table)
    return found


def _paise_columns_by(migration: "Migration") -> list[tuple[str, str]]:
    """Best-effort: ``(table_name, column_name)`` for every column whose name
    ends ``_paise`` in a ``CREATE TABLE`` this migration issues. Money-type
    drift (failure B) is invisible to table-existence checks alone -- a
    hand-applied ``numeric`` column has the right name and the right table,
    just the wrong type."""
    found: list[tuple[str, str]] = []
    for table, body in _extract_table_bodies(migration.sql):
        for field in _split_top_level(body):
            match = _PAISE_COLUMN_RE.match(field)
            if match:
                found.append((table, match.group(1)))
    return found


def _indexes_created_by(migration: "Migration") -> list[tuple[str, str]]:
    """Best-effort: ``(index_name, table_name)`` for every ``CREATE INDEX`` /
    ``CREATE UNIQUE INDEX`` a migration issues.

    Failure class C, the one table/constraint/type checks all miss: a partial
    UNIQUE index is the ONLY thing standing between an integration that
    re-walks by design and a duplicated financial document. ``ux_bill_external``,
    ``ux_po_external``, ``ux_grn_external``, ``ux_po_line_external`` and
    ``ux_reconciliation_exception_open`` are every one of them a
    ``CREATE UNIQUE INDEX ... WHERE ...``, not a table constraint, so
    :func:`_named_constraints_by` cannot see any of them. A legacy dump missing
    one adopts cleanly under the old checks and then admits the duplicate
    receipt the index existed to refuse.
    """
    text = _LINE_COMMENT_RE.sub("", migration.sql)
    return _CREATE_INDEX_RE.findall(text)


def _policies_created_by(migration: "Migration") -> list[tuple[str, str]]:
    """Best-effort: ``(policy_name, table_name)`` for every ``CREATE POLICY``.

    Failure class D. A row-level-security policy is not a schema object any
    existing check looks at, and its absence is silent in exactly the direction
    that matters: with ``ENABLE ROW LEVEL SECURITY`` still on and no policy, the
    table denies everything and somebody notices immediately -- but a database
    carrying the tables with RLS never enabled reads FULLY OPEN, and reports
    itself adopted and current while every scope restriction in the product is
    waived. That is the Wave 2 defect's exact shape, arriving through a restore
    instead of through a missing migration.
    """
    text = _LINE_COMMENT_RE.sub("", migration.sql)
    return _CREATE_POLICY_RE.findall(text)


def _rls_tables_by(migration: "Migration") -> tuple[list[str], list[str]]:
    """``(enabled, forced)``: the tables a migration issues ``ENABLE`` and
    ``FORCE ROW LEVEL SECURITY`` for.

    Both halves, because they fail differently and only one of them is
    observable from ``pg_policies``: ``ENABLE`` missing means no policy applies
    to anyone, and ``FORCE`` missing means every policy applies to everyone
    EXCEPT the table's owner -- which in production is the deploy identity that
    ran the migrations, and is the role most likely to be reused by a background
    job.
    """
    text = _LINE_COMMENT_RE.sub("", migration.sql)
    return _ENABLE_RLS_RE.findall(text), _FORCE_RLS_RE.findall(text)


def _indexes_present(con: psycopg.Connection,
                      indexes: list[tuple[str, str]]) -> list[str]:
    """``"name on table"`` for every `(index, table)` pair NOT found as a real
    index in the current schema. Empty means every one is present."""
    missing: list[str] = []
    for name, table in indexes:
        row = con.execute(
            "SELECT 1 FROM pg_class i "
            "JOIN pg_index x ON x.indexrelid = i.oid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = i.relnamespace "
            "WHERE n.nspname = current_schema() AND i.relname = %s "
            "AND t.relname = %s",
            (name, table),
        ).fetchone()
        if row is None:
            missing.append(f"{name} on {table}")
    return missing


def _policies_present(con: psycopg.Connection,
                       policies: list[tuple[str, str]]) -> list[str]:
    """``"name on table"`` for every `(policy, table)` pair NOT found in
    ``pg_policies``. Empty means every one is present."""
    missing: list[str] = []
    for name, table in policies:
        row = con.execute(
            "SELECT 1 FROM pg_policies "
            "WHERE schemaname = current_schema() AND tablename = %s "
            "AND policyname = %s",
            (table, name),
        ).fetchone()
        if row is None:
            missing.append(f"{name} on {table}")
    return missing


def _rls_problems(con: psycopg.Connection, enabled: list[str],
                   forced: list[str]) -> list[str]:
    """One message per table whose row-level security is not actually on in the
    database, distinguishing the two halves so the operator is told which."""
    problems: list[str] = []
    for table in dict.fromkeys(enabled):
        row = con.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = %s",
            (table,),
        ).fetchone()
        if row is None:
            problems.append(f"{table} is absent, so RLS cannot be verified")
            continue
        if not row[0]:
            problems.append(
                f"row level security is not ENABLED on {table} -- the table "
                f"reads fully open and no policy on it applies to anyone")
        if table in forced and not row[1]:
            problems.append(
                f"row level security is not FORCED on {table} -- the table's "
                f"OWNER bypasses every policy on it, silently")
    return problems


def _functions_present(con: psycopg.Connection, functions: list[str]) -> list[str]:
    """Names in `functions` that are NOT visible in the current schema's
    ``pg_proc``. Empty means every one is present."""
    if not functions:
        return []
    rows = con.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname = ANY(%s)",
        (functions,),
    ).fetchall()
    present = {row[0] for row in rows}
    return [f for f in functions if f not in present]


def _triggers_present(con: psycopg.Connection,
                       triggers: list[tuple[str, str]]) -> list[str]:
    """``"name on table"`` for every `(name, table)` pair in `triggers` that
    is NOT a real, non-internal trigger in the database. Empty means every
    one is present -- this is what failure A (audit immutability lost)
    depends on catching."""
    missing: list[str] = []
    for name, table in triggers:
        row = con.execute(
            "SELECT 1 FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = %s "
            "AND t.tgname = %s AND NOT t.tgisinternal",
            (table, name),
        ).fetchone()
        if row is None:
            missing.append(f"{name} on {table}")
    return missing


def _named_constraints_present(con: psycopg.Connection,
                                constraints: list[tuple[str, str]]) -> list[str]:
    """``"name on table"`` for every `(table, name)` pair NOT found in
    ``pg_constraint``. Empty means every one is present."""
    missing: list[str] = []
    for table, name in constraints:
        row = con.execute(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = %s "
            "AND c.conname = %s",
            (table, name),
        ).fetchone()
        if row is None:
            missing.append(f"{name} on {table}")
    return missing


def _exclusion_constraints_present(con: psycopg.Connection,
                                    tables: list[str]) -> list[str]:
    """Tables in `tables` that do NOT carry an exclusion constraint
    (``contype = 'x'``) in the database. Empty means every one is present."""
    missing: list[str] = []
    for table in tables:
        row = con.execute(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = %s "
            "AND c.contype = 'x'",
            (table,),
        ).fetchone()
        if row is None:
            missing.append(f"exclusion constraint on {table}")
    return missing


def _paise_column_problems(con: psycopg.Connection,
                            columns: list[tuple[str, str]]) -> list[str]:
    """One message per `(table, column)` pair in `columns` that is either
    missing or not ``bigint``. Empty means every ``*_paise`` column is
    present and correctly typed -- this is failure B (money type drift), the
    one float/``Decimal``-leakage path table-existence checks cannot see."""
    problems: list[str] = []
    for table, column in columns:
        row = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = %s",
            (table, column),
        ).fetchone()
        if row is None:
            problems.append(f"{table}.{column} is missing")
        elif row[0] != "bigint":
            problems.append(
                f"{table}.{column} is {row[0]}, not bigint -- money must be "
                f"integer paise, never {row[0]}")
    return problems


def _adoption_problems(con: psycopg.Connection, migration: "Migration") -> list[str]:
    """Every reason `migration` cannot be safely adopted against the current
    database -- empty means adoption is safe. Checks tables, functions,
    triggers, explicitly named constraints, the unnamed-exclusion case, and
    (the one that matters most) every ``*_paise`` column's actual type, so
    "adopted" certifies more than "the table names exist" -- see the module
    docstring and review finding F5.

    Deliberately does not stop at the first failing check once tables are
    confirmed present: an operator reading the raised error should see
    everything else wrong in one pass, not fix one object and be surprised by
    the next. Missing tables IS a stopping condition, though -- with the
    foundation absent, a function/trigger/constraint/column query against a
    table that was never there answers a question nobody asked; the missing
    tables are reason enough on their own to refuse.
    """
    problems: list[str] = []

    tables = _tables_created_by(migration)
    missing_tables = _missing_tables(con, tables)
    if missing_tables:
        return [f"tables missing: {missing_tables}"]

    missing_functions = _functions_present(con, _functions_created_by(migration))
    if missing_functions:
        problems.append(f"functions missing: {missing_functions}")

    missing_triggers = _triggers_present(con, _triggers_created_by(migration))
    if missing_triggers:
        problems.append(f"triggers missing: {missing_triggers}")

    missing_constraints = _named_constraints_present(
        con, _named_constraints_by(migration))
    if missing_constraints:
        problems.append(f"constraints missing: {missing_constraints}")

    missing_exclusions = _exclusion_constraints_present(
        con, _exclusion_constraint_tables(migration))
    if missing_exclusions:
        problems.append(f"exclusion constraints missing: {missing_exclusions}")

    paise_problems = _paise_column_problems(con, _paise_columns_by(migration))
    if paise_problems:
        problems.append(f"money column type mismatch: {paise_problems}")

    # Failure class C: a partial UNIQUE index is not a table constraint, so
    # nothing above can see one. It is also the only thing that makes an
    # integration which re-walks by design idempotent.
    missing_indexes = _indexes_present(con, _indexes_created_by(migration))
    if missing_indexes:
        problems.append(f"indexes missing: {missing_indexes}")

    # Failure class D: row-level security. A policy is checked, and separately
    # whether RLS is actually ENABLEd and FORCEd -- a policy present on a table
    # with RLS switched off is inert, and reads open.
    missing_policies = _policies_present(con, _policies_created_by(migration))
    if missing_policies:
        problems.append(f"row level security policies missing: {missing_policies}")

    rls_problems = _rls_problems(con, *_rls_tables_by(migration))
    if rls_problems:
        problems.append(f"row level security not enforced: {rls_problems}")

    return problems


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
            problems = _adoption_problems(con, migration)
            if problems:
                raise MigrationError(
                    f"migration {migration.version}_{migration.name} could not be "
                    f"applied ({type(exc).__name__}: {exc}), and its expected "
                    f"objects are not all present and correctly shaped in the "
                    f"database either -- this is a genuine conflict, not an "
                    f"adoptable legacy schema. Refusing to guess. "
                    f"Problems: " + "; ".join(problems)) from exc
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
