"""Read both schemas: SQLite from the live file, PostgreSQL from the migrations.

Why the PostgreSQL side is scanned and not regexed
--------------------------------------------------
The obvious way to list a table's columns from DDL is a regex over the
``CREATE TABLE x (...)`` body. It does not work, and the reason is the same one
that broke the money-SQL gate this wave: **a regex cannot cross a nested
paren.** ``migrations/pg/*.sql`` is full of them --

    amount_paise  bigint NOT NULL CHECK (amount_paise >= 0),
    state         text   NOT NULL CHECK (state IN ('Reserved','Settled')),
    UNIQUE (stream_key, seq),
    CONSTRAINT fk_line FOREIGN KEY (po_id, line_no) REFERENCES po_line (po_id, line_no)

-- a `[^,]*` column pattern stops inside ``CHECK (a >= 0)``'s comma-free body
by luck and inside ``IN ('a','b')`` by accident, and a `.*?\\)` pattern closes
on the FIRST `)` rather than the matching one. So this module counts parens.

:func:`split_top_level` is the whole trick: walk the body one character at a
time, track paren depth, and treat a comma as a separator only at depth zero.
It also skips over string literals and ``--`` comments, because a comma inside
``'a,b'`` and a paren inside ``-- see (note)`` are not structure.

What is deliberately NOT done here
----------------------------------
This does not evaluate the DDL. It reports what the migration text says, which
is the right answer for a *preflight* -- a preflight that needs a live server to
tell you what it would do is not a preflight. :func:`live_pg_columns` reads the
same information out of ``information_schema`` when a connection is available,
and :func:`compare_scanned_to_live` exists so the scanner can be checked against
the server rather than trusted.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PG_MIGRATIONS = ROOT / "migrations" / "pg"

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"(?:([a-z_][a-z0-9_]*)\.)?([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)

#: Words that begin a table CONSTRAINT clause rather than a column definition.
_CONSTRAINT_LEADERS = frozenset({
    "primary", "unique", "check", "foreign", "constraint", "exclude", "like",
})


# --------------------------------------------------------------- text scanning
def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` blocks, preserving literals.

    A comment is not structure, but a ``--`` inside a string literal is data.
    Scanned rather than regexed for the reason in the module docstring.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":   # escaped '' inside a literal
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j          # keep the newline: it is a separator
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def matching_paren(sql: str, open_index: int) -> int:
    """Index of the ``)`` matching the ``(`` at `open_index`.

    Raises ValueError when the parens do not balance, rather than returning the
    last one it found: an unbalanced body means the scan is wrong, and silently
    truncating a column list is how a migration drops a money column.
    """
    if sql[open_index] != "(":
        raise ValueError(f"index {open_index} is {sql[open_index]!r}, not '('")
    depth, i, n = 0, open_index, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced parentheses from index {open_index}")


def split_top_level(body: str, sep: str = ",") -> list[str]:
    """Split on `sep` at paren depth zero only, ignoring separators in literals."""
    parts: list[str] = []
    depth, cur, i, n = 0, [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if body[j] == "'":
                    if j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(body[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


# ------------------------------------------------------------------ PostgreSQL
@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    not_null: bool
    identity: bool
    has_default: bool


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    #: (local_columns, referenced_table, referenced_columns)
    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)
    source_file: str = ""

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


_FK_REFERENCES = re.compile(
    r"REFERENCES\s+(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)\s*(\([^)]*\))?",
    re.IGNORECASE,
)


def _parse_column(clause: str) -> Column | None:
    clause = clause.strip()
    if not clause:
        return None
    first = clause.split()[0]
    if first.lower() in _CONSTRAINT_LEADERS:
        return None
    tokens = clause.split()
    name = tokens[0]
    sql_type = tokens[1].rstrip(",") if len(tokens) > 1 else ""
    upper = clause.upper()
    return Column(
        name=name,
        sql_type=sql_type.lower(),
        not_null="NOT NULL" in upper,
        identity="GENERATED ALWAYS AS IDENTITY" in upper or "GENERATED BY DEFAULT AS IDENTITY" in upper,
        has_default="DEFAULT" in upper,
    )


def _parse_foreign_keys(clause: str, own_columns: list[str]
                        ) -> list[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    """Column-level ``REFERENCES`` and table-level ``FOREIGN KEY (...)``."""
    out = []
    stripped = clause.strip()
    lowered = stripped.lower()
    for m in _FK_REFERENCES.finditer(stripped):
        target = m.group(1).lower()
        target_cols = tuple(
            c.strip() for c in (m.group(2) or "()").strip("()").split(",") if c.strip()
        )
        if lowered.startswith("foreign key") or lowered.startswith("constraint"):
            fk_open = stripped.lower().find("foreign key")
            paren = stripped.find("(", fk_open)
            close = matching_paren(stripped, paren)
            local = tuple(c.strip() for c in stripped[paren + 1:close].split(","))
        else:
            local = (stripped.split()[0],)
        out.append((local, target, target_cols))
    return out


def scan_pg_schema(migrations_dir: Path | None = None) -> dict[str, Table]:
    """Every table the PostgreSQL migrations create, in application order."""
    directory = migrations_dir or PG_MIGRATIONS
    tables: dict[str, Table] = {}
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        sql = strip_sql_comments(path.read_text(encoding="utf-8"))
        for m in _CREATE_TABLE.finditer(sql):
            open_paren = m.end() - 1
            close = matching_paren(sql, open_paren)
            body = sql[open_paren + 1:close]
            table = Table(name=m.group(2).lower(), source_file=path.name)
            for clause in split_top_level(body):
                col = _parse_column(clause)
                if col is not None:
                    table.columns.append(col)
                table.foreign_keys.extend(
                    _parse_foreign_keys(clause, table.column_names))
            tables[table.name] = table
    return tables


def live_pg_columns(connection) -> dict[str, list[str]]:
    """The same information read from a live server, for checking the scanner."""
    cur = connection.cursor()
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
    )
    out: dict[str, list[str]] = {}
    for table_name, column_name in cur.fetchall():
        out.setdefault(table_name, []).append(column_name)
    return out


def compare_scanned_to_live(scanned: dict[str, Table],
                            live: dict[str, list[str]]) -> dict[str, list[str]]:
    """Differences between the scan and the server. Empty means the scan is right.

    Exists because a scanner nobody checks against a server is a second schema
    that drifts. CI has a server; this is what it should run.
    """
    problems: dict[str, list[str]] = {}
    for name, table in scanned.items():
        if name not in live:
            problems.setdefault(name, []).append("scanned but absent on the server")
            continue
        scanned_cols, live_cols = set(table.column_names), set(live[name])
        for missing in sorted(live_cols - scanned_cols):
            problems.setdefault(name, []).append(f"server has column {missing!r}, scan missed it")
        for extra in sorted(scanned_cols - live_cols):
            problems.setdefault(name, []).append(f"scan invented column {extra!r}")
    for name in sorted(set(live) - set(scanned)):
        problems.setdefault(name, []).append("on the server but not in the migrations")
    return problems


# ---------------------------------------------------------------------- SQLite
def open_source_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the POC database READ ONLY.

    ``mode=ro`` is enforced by SQLite itself, so an export that tries to write
    fails rather than mutating the system it is supposed to be reading. A
    migration that modifies its own source cannot be re-run to compare.
    """
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"source database not found: {path}")
    uri = "file:" + path.as_posix().lstrip("/") + "?mode=ro"
    if not uri.startswith("file:/"):
        uri = "file:/" + uri[len("file:"):]
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def sqlite_tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def sqlite_primary_key(con: sqlite3.Connection, table: str) -> list[str]:
    """PK columns in key order; ``[]`` when the table has no declared PK."""
    rows = [r for r in con.execute(f'PRAGMA table_info("{table}")') if r[5]]
    return [r[1] for r in sorted(rows, key=lambda r: r[5])]


def topological_order(tables: dict[str, Table], subset: set[str] | None = None) -> list[str]:
    """Parents before children, deterministic, and it REFUSES on a cycle.

    Kahn's algorithm with the ready set drained in sorted order, so two runs on
    the same schema produce the same order -- a migration whose insert order
    varies run to run cannot be compared against a previous run's report.

    Self-references (``wbs_element.parent_wbs_id``) are ignored: a row can be
    its own parent's sibling within one table, and the table still has to be
    inserted somewhere. Genuine cycles BETWEEN tables raise, because inserting
    them with foreign keys enabled needs a deferred constraint and a decision
    nobody has made -- and guessing at one is how a migration half-lands.
    """
    names = set(subset) if subset is not None else set(tables)
    deps: dict[str, set[str]] = {n: set() for n in names}
    for name in names:
        for _local, target, _cols in tables[name].foreign_keys:
            if target != name and target in names:
                deps[name].add(target)

    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(n for n, d in remaining.items() if not d)
        if not ready:
            cycle = sorted(remaining)
            raise ValueError(
                "foreign-key cycle between tables, cannot order for an import "
                f"with constraints enabled: {cycle}"
            )
        for name in ready:
            ordered.append(name)
            del remaining[name]
        for d in remaining.values():
            d.difference_update(ready)
    return ordered
