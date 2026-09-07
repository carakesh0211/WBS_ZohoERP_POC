"""Every column the integration package names must exist in migration 010.

WHY THIS EXISTS
===============

Wave 5's seven streams were file-disjoint and could not see each other. That
prevented edit collisions and prevented nothing else: three of them
independently implemented the same rate-budget reservation, against a column
list that was frozen in `docs/WAVE5_CONTRACTS.md` with a trailing `...`.

Stream 2 owned the migration and filled that ellipsis in correctly -- it added
`allocation` to the primary key, which is exactly what amendment A1 said the
60/30/10 split cannot be enforced without. Streams 4 and 6 had already written
SQL against the shorter frozen list. The result:

* `throttle.py` names `lane`, `created_by` and `updated_by`. **None of the
  three exists.** It also conflicts on `(connection_id, window_kind, lane,
  window_start)`, and no such constraint exists either.
* `outbound.py` conflicts on `(connection_id, window_kind, window_start)`,
  which is not the primary key, and its INSERT omits five NOT NULL columns
  that have no default.

Every unit test over both modules passed throughout, because both talk to
in-memory doubles. The SQL is a string until something executes it.

The lead recorded this as "a naming mismatch that has not bitten yet". That
was wrong, and wrong in the direction that matters: it was checked by looking
at the port (in-memory, so nothing joins the names) and not at the SQL. This
test is what checking the SQL looks like.

WHAT IT DOES NOT DO
===================

It compares column NAMES against the migration. It is not a SQL parser and it
does not verify types, constraint satisfaction, or that an `ON CONFLICT`
target matches a real unique index -- that last one needs a live server, and
`tests/test_pg_integration_schema.py` is where it belongs. A name that exists
can still be used wrongly. This closes the crudest failure, which is the one
that actually happened.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "pg" / "010_integration.sql"
PACKAGE = ROOT / "app" / "backend" / "integration"

#: Words that appear in a column position but are SQL, not identifiers.
_NOT_COLUMNS = {
    "select", "from", "where", "and", "or", "not", "null", "insert", "into",
    "values", "on", "conflict", "do", "update", "set", "returning", "as",
    "case", "when", "then", "else", "end", "true", "false", "default",
    "excluded", "coalesce", "now", "text", "int", "integer", "timestamptz",
    "jsonb", "boolean", "bigint", "interval", "distinct", "order", "by",
    "limit", "offset", "group", "having", "join", "left", "inner", "using",
    "with", "scope", "count", "sum", "min", "max", "greatest", "least",
}


def _tables() -> dict[str, set[str]]:
    """`{table: {column, ...}}` parsed from the migration's CREATE TABLEs."""
    sql = MIGRATION.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
            r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", sql, re.S):
        name, body = match.group(1), match.group(2)
        columns: set[str] = set()
        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if re.match(r"(PRIMARY|CONSTRAINT|CHECK|UNIQUE|FOREIGN|REFERENCES|"
                        r"EXCLUDE|ON)\b", line, re.I):
                continue
            token = line.split()[0]
            if token.isidentifier():
                columns.add(token.lower())
        tables[name.lower()] = columns
    return tables


def _sql_literals(path: Path) -> list[str]:
    """Every triple- or single-quoted string in the module that looks like SQL."""
    source = path.read_text(encoding="utf-8")
    found: list[str] = []
    for match in re.finditer(r'"""(.*?)"""', source, re.S):
        found.append(match.group(1))
    # Adjacent single-line string literals concatenated across lines, which is
    # how `outbound.py` builds its statement.
    for match in re.finditer(r'((?:\s*"[^"\n]*"\s*\n?){2,})', source):
        found.append(" ".join(re.findall(r'"([^"\n]*)"', match.group(1))))
    return [s for s in found if re.search(r"\b(INSERT INTO|UPDATE|SELECT)\b", s)]


#: No module is waived. `throttle.py` used to be, with `xfail(strict=True)`.
#:
#: It named `lane`, `created_by` and `updated_by` -- none of which the shipped
#: table has -- and conflicted on a constraint that did not exist. It was
#: waived rather than patched, because renaming the three columns would have
#: satisfied THIS test and still failed at runtime on four NOT NULL columns
#: with no default. A gate going green on a statement that cannot execute is
#: worse than one that stays red.
#:
#: It was fixed by DELETION: `throttle.reserve` now delegates to
#: `integration_store.reserve_calls` and `throttle.py` contains no SQL at all.
#:
#: The waiver was then held open even after the module passed, because passing
#: this static check is not evidence that the SQL RUNS. It came off only when
#: CI's live PostgreSQL job reported `953 collected, 953 executed, 0 skipped,
#: 0 failed` with all 21 tests in `test_pg_integration_rate_budget.py` among
#: them -- the repaired path executing against a real server.
#:
#: `strict=True` was the load-bearing part: once the module was fixed, the
#: marker XPASSed and FAILED the suite, so it could not be quietly satisfied
#: and forgotten. A non-strict marker would have gone silently green and the
#: waiver would still be here.
_KNOWN_BROKEN: dict[str, str] = {}


def _modules() -> list[Path]:
    modules = sorted(p for p in PACKAGE.glob("*.py")
                     if not p.name.startswith("__"))
    return [
        pytest.param(
            p, marks=pytest.mark.xfail(strict=True, reason=_KNOWN_BROKEN[p.name]))
        if p.name in _KNOWN_BROKEN else p
        for p in modules
    ]


@pytest.mark.parametrize("module", _modules(), ids=lambda p: getattr(p, "name", str(p)))
def test_every_column_named_in_sql_exists_in_the_migration(module: Path) -> None:
    tables = _tables()
    problems: list[str] = []

    for statement in _sql_literals(module):
        for table in re.findall(r"(?:INSERT INTO|UPDATE)\s+(\w+)", statement):
            known = tables.get(table.lower())
            if known is None:
                continue                      # not a 010 table; out of scope
            # The parenthesised column list of an INSERT, and the ON CONFLICT
            # target -- the two places a wrong name is fatal rather than
            # merely unused.
            for group in re.findall(
                    r"INSERT INTO\s+%s\s+(?:AS\s+\w+\s+)?\(([^)]*)\)" % table,
                    statement, re.I) + re.findall(
                    r"ON CONFLICT\s*\(([^)]*)\)", statement, re.I):
                for raw in group.split(","):
                    name = raw.strip().split(".")[-1].strip().lower()
                    if (not name or name in _NOT_COLUMNS
                            or not name.isidentifier()):
                        continue
                    if name not in known:
                        problems.append(
                            f"{table}.{name} does not exist "
                            f"(has: {', '.join(sorted(known))})")

    assert not problems, (
        f"{module.name} names columns migration 010 does not define. Every "
        f"unit test over this module passes, because it talks to an in-memory "
        f"double -- the SQL is a string until something executes it:\n  "
        + "\n  ".join(dict.fromkeys(problems)))


def _all_known_columns(tables: dict[str, set[str]]) -> set[str]:
    """Every column name defined anywhere in the migration."""
    known: set[str] = set()
    for columns in tables.values():
        known |= columns
    return known


def _unmarked_modules() -> list[Path]:
    """Plain paths, with no xfail markers.

    `_modules()` marks `throttle.py` xfail for the column-list check it was
    written for. Reusing that list here applied the marker to a DIFFERENT
    assertion, so a module that passes this check reports XPASS(strict) and
    fails for the wrong reason. A waiver must name the claim it waives.
    """
    return sorted(p for p in PACKAGE.glob("*.py") if not p.name.startswith("__"))


@pytest.mark.parametrize("module", _unmarked_modules(), ids=lambda p: p.name)
def test_no_statement_sets_or_filters_on_a_column_no_table_defines(module: Path) -> None:
    """The half the INSERT check does not reach: SET and WHERE columns.

    Two of the five execution-fatal defects an adversarial review found in
    `jobs.py` were exactly this -- `CLAIM_JOB_SQL` filtering on
    `next_attempt_at` and `FINISH_JOB_SQL` setting `note`, neither of which
    `job` has. Both are plain missing column names, the class the INSERT check
    claims to close, sitting in positions it never looked at.

    DELIBERATELY CONSERVATIVE. A name is reported only when NO table in the
    migration defines it, so a legitimate cross-table reference this crude
    parser cannot resolve stays quiet. That under-reports on purpose: a noisy
    gate is a gate somebody switches off, and every statement here is
    multi-table.
    """
    tables = _tables()
    if not tables:
        pytest.skip("no CREATE TABLE found in the migration")
    known = _all_known_columns(tables)
    problems: list[str] = []

    for statement in _sql_literals(module):
        # Only statements that touch a table this migration defines.
        touched = {t.lower() for t in re.findall(
            r"(?:INSERT INTO|UPDATE|FROM|JOIN)\s+(\w+)", statement, re.I)}
        if not (touched & set(tables)):
            continue

        candidates: list[tuple[str, str]] = []
        for chunk in re.findall(r"\bSET\b(.*?)(?:\bWHERE\b|\bRETURNING\b|$)",
                                statement, re.I | re.S):
            for assign in chunk.split(","):
                name = assign.split("=")[0].strip().split(".")[-1].strip().lower()
                if name:
                    candidates.append((name, "SET"))
        for chunk in re.findall(
                r"\b(?:WHERE|AND|OR)\s+([a-z_][a-z0-9_]*)\s*(?:=|<|>|<=|>=|<>|!=|IS\b|IN\b)",
                statement, re.I):
            candidates.append((chunk.strip().lower(), "WHERE"))

        for name, position in candidates:
            if (not name or name in _NOT_COLUMNS or not name.isidentifier()
                    or name in known):
                continue
            problems.append(f"{name!r} in a {position} position is defined by "
                            f"no table in the migration")

    assert not problems, (
        f"{module.name} filters or assigns on names migration 010 defines "
        f"nowhere. PostgreSQL raises 42703 the first time the statement runs, "
        f"and no in-memory double can tell you:\n  "
        + "\n  ".join(dict.fromkeys(problems)))
