"""Deterministic NDJSON export of the POC SQLite database.

    python -m tools.migration.export_poc --source data/capex.db --out export/

Produces one ``<table>.ndjson`` per table plus a ``manifest.json`` carrying, for
every file, the row count and the SHA-256 of the bytes on disk. Two exports of
an unchanged source produce byte-identical files and an identical manifest --
which is the only way "the export did not change" is a checkable claim rather
than an assertion.

Four properties, and why each is a property rather than a preference
-------------------------------------------------------------------
**Read-only against the source.** Opened ``mode=ro``, enforced by SQLite. An
export that can write to its source cannot be re-run to compare, and a
migration whose "before" moved is not evidence of anything.

**Deterministic row order.** ``ORDER BY`` the declared primary key, falling back
to every column in declared order for a table without one. Not ``rowid``:
SQLite's rowid is not stable across a ``VACUUM``, so an order that looks stable
on one machine is not.

**Integers stay integers.** ``amount_paise`` is a count of the smallest unit of
currency. There is no rounding to do and no float to route it through, so a
float in a money column is not a small inaccuracy -- it is a defect, and this
module RAISES on one rather than writing it. JSON has one number type, so the
guard is on both ends: :func:`_check_money` refuses a float on the way out, and
:func:`load_ndjson` refuses one on the way back in.

**ISO-8601 UTC timestamps -- except where the bytes are hashed.**
``audit_log.at`` is part of the hashed payload
``prev|at|actor|action|type|id|detail``. Normalising it would change the bytes
and therefore the hash, turning an intact chain into a broken one. So
``audit_log`` is exported VERBATIM and the manifest says so in the table's own
entry, rather than the exception living in a comment nobody reads. Every other
table is normalised, and each normalisation is COUNTED in the manifest --
including the naive-timestamp count, because "assumed UTC" is an assumption and
an auditor is entitled to see how many rows it was applied to.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import (open_source_readonly, sqlite_columns, sqlite_primary_key,
                     sqlite_tables)

#: Tables whose stored bytes are an input to a hash. Never normalised.
HASH_BEARING_TABLES = frozenset({"audit_log"})

#: A money column. The suffix is the repository's own convention and is checked
#: against the schema by tests rather than assumed.
MONEY_SUFFIX = "_paise"

#: SQLite declared types that legitimately hold a non-integer.
#:
#: The POC has exactly three such quantities -- ``quantity``, ``exchange_rate``
#: and ``progress_pct`` -- and none of them is money. Deriving the permitted set
#: from the DECLARED type rather than from the values matters: SQLite is
#: dynamically typed, so an INTEGER-declared column can hold 1.0, and that is
#: precisely the accident this export must refuse rather than carry forward.
FLOAT_DECLARED_TYPES = frozenset({"REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION", "NUMERIC"})

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMPISH = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

EXPORT_FORMAT_VERSION = "1"


class ExportError(RuntimeError):
    """Refusal. The export did not happen; nothing partial was left behind."""


# ------------------------------------------------------------------ conversion
def normalise_timestamp(value: Any) -> tuple[Any, str]:
    """(value, disposition) -- ISO-8601 UTC where that is unambiguous.

    Dispositions, all counted in the manifest:

    ``unchanged``     not a string, or not timestamp-shaped. Left alone.
    ``date_only``     ``YYYY-MM-DD``. Left alone: promoting a date to an instant
                      invents a time of day and a timezone that the source
                      never recorded.
    ``already_utc``   parsed, already ``+00:00``, re-rendered canonically.
    ``offset_to_utc`` parsed with a non-UTC offset, converted.
    ``naive_as_utc``  parsed without a timezone. Assumed UTC -- an assumption,
                      which is why it gets its own disposition and its own line
                      in the manifest.
    ``unparseable``   timestamp-shaped but not ISO-8601. Left alone and
                      reported; silently rewriting it would be worse.
    """
    if not isinstance(value, str):
        return value, "unchanged"
    if _DATE_ONLY.match(value):
        return value, "date_only"
    if not _TIMESTAMPISH.match(value):
        return value, "unchanged"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value, "unparseable"
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc).isoformat(), "naive_as_utc"
    disposition = "already_utc" if parsed.utcoffset() == timezone.utc.utcoffset(None) \
        else "offset_to_utc"
    return parsed.astimezone(timezone.utc).isoformat(), disposition


def _check_integer(table: str, column: str, value: Any, *, money: bool) -> Any:
    """Refuse a float in a column that is not declared to hold one.

    Two distinct refusals, because they mean different things. A float in a
    money column is a correctness defect: paise are a count, there is nothing to
    round, and a value that has been through binary floating point cannot be
    trusted to the paisa. A float in any other integer-declared column is a
    schema violation SQLite permits and PostgreSQL will not, so it is better
    found here than half way through an import.
    """
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if money:
        raise ExportError(
            f"{table}.{column} holds {value!r} ({type(value).__name__}). A paise "
            "column is an integer count of the smallest currency unit; a float "
            "here means the value already passed through binary floating point "
            "and the export refuses to launder it into JSON."
        )
    raise ExportError(
        f"{table}.{column} is declared as an integer type but holds {value!r} "
        f"({type(value).__name__}). SQLite permits this; PostgreSQL will reject "
        "it mid-import. Fix the source row before migrating."
    )


# ---------------------------------------------------------------------- export
def order_by_clause(con: sqlite3.Connection, table: str) -> str:
    keys = sqlite_primary_key(con, table) or sqlite_columns(con, table)
    return ", ".join(f'"{k}"' for k in keys)


def iter_rows(con: sqlite3.Connection, table: str) -> Iterator[sqlite3.Row]:
    yield from con.execute(f'SELECT * FROM "{table}" ORDER BY {order_by_clause(con, table)}')


def float_columns(con: sqlite3.Connection, table: str) -> list[str]:
    """Columns DECLARED as a float type. Everything else must be an integer,
    a string, a blob or NULL."""
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')
            if r[2].upper() in FLOAT_DECLARED_TYPES]


def export_table(con: sqlite3.Connection, table: str, out_path: Path) -> dict[str, Any]:
    columns = sqlite_columns(con, table)
    money_columns = [c for c in columns if c.endswith(MONEY_SUFFIX)]
    floats = float_columns(con, table)
    overlap = sorted(set(money_columns) & set(floats))
    if overlap:
        raise ExportError(
            f"{table}: {overlap} are money columns DECLARED as a float type. "
            "Paise are an integer count; a float-declared paise column is the "
            "defect, not a value to be exported around it."
        )
    verbatim = table in HASH_BEARING_TABLES
    dispositions: dict[str, int] = {}
    rows = 0
    hasher = hashlib.sha256()

    with out_path.open("wb") as fh:
        for row in iter_rows(con, table):
            record: dict[str, Any] = {}
            for column in columns:
                value = row[column]
                if column not in floats:
                    value = _check_integer(table, column, value,
                                           money=column in money_columns) \
                        if isinstance(value, float) or column in money_columns else value
                if not verbatim and column not in money_columns:
                    value, disposition = normalise_timestamp(value)
                    if disposition != "unchanged":
                        dispositions[disposition] = dispositions.get(disposition, 0) + 1
                record[column] = value
            # sort_keys so column order in the file never depends on the
            # dict insertion order of whichever Python built it; ensure_ascii
            # False so the bytes are UTF-8 and a rupee sign stays one character.
            line = json.dumps(record, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False)
            encoded = (line + "\n").encode("utf-8")
            fh.write(encoded)
            hasher.update(encoded)
            rows += 1

    return {
        "file": out_path.name,
        "rows": rows,
        "sha256": hasher.hexdigest(),
        "bytes": out_path.stat().st_size,
        "columns": columns,
        "money_columns": money_columns,
        "float_columns": floats,
        "order_by": order_by_clause(con, table),
        "normalisation": "none (hash-bearing: bytes are an input to entry_hash)"
                         if verbatim else "iso8601_utc",
        "timestamp_dispositions": dict(sorted(dispositions.items())),
    }


def audit_summary(con: sqlite3.Connection) -> dict[str, Any]:
    """Where verifiable history begins.

    The unhashed count is not a footnote. Rows written before POC migration 002
    carry ``entry_hash IS NULL``; nothing about them is cryptographically
    verifiable, and an auditor reading the migration report is entitled to know
    exactly how many rows that is and which ``audit_id`` the verifiable history
    starts at -- not to infer it from a total.
    """
    if "audit_log" not in set(sqlite_tables(con)):
        return {"present": False}
    total = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    unhashed = con.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entry_hash IS NULL").fetchone()[0]
    first_hashed = con.execute(
        "SELECT MIN(audit_id) FROM audit_log WHERE entry_hash IS NOT NULL").fetchone()[0]
    last_unhashed = con.execute(
        "SELECT MAX(audit_id) FROM audit_log WHERE entry_hash IS NULL").fetchone()[0]
    return {
        "present": True,
        "total_rows": total,
        "unhashed_rows": unhashed,
        "hashed_rows": total - unhashed,
        "verifiable_history_begins_at_audit_id": first_hashed,
        "last_unhashed_audit_id": last_unhashed,
        "note": (
            "Unhashed rows predate POC migration 002, which added prev_hash and "
            "entry_hash. They are real history and are migrated, but nothing "
            "about them is cryptographically verifiable. They are NOT placed in "
            "the LEGACY stream -- see tools/migration/audit_chain.py for the "
            "measured reason."
        ),
    }


def export(source: str | Path, out_dir: str | Path,
           tables: list[str] | None = None) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = open_source_readonly(source)
    try:
        selected = tables if tables is not None else sqlite_tables(con)
        missing = sorted(set(selected) - set(sqlite_tables(con)))
        if missing:
            raise ExportError(f"source has no such table(s): {missing}")

        files = {}
        for table in sorted(selected):
            files[table] = export_table(con, table, out / f"{table}.ndjson")

        manifest = {
            "export_format_version": EXPORT_FORMAT_VERSION,
            "generated_by": "tools/migration/export_poc.py",
            "source_database": Path(source).resolve().as_posix(),
            "source_sha256": _file_sha256(Path(source)),
            "source_schema_migrations": _applied_migrations(con),
            "tables": dict(sorted(files.items())),
            "row_count_total": sum(f["rows"] for f in files.values()),
            "audit": audit_summary(con),
            "determinism": (
                "Rows are ordered by declared primary key (or by every column, "
                "in declared order, for a table without one). JSON keys are "
                "sorted. Two exports of an unchanged source are byte-identical."
            ),
            "read_only": "source opened with SQLite URI mode=ro",
        }
    finally:
        con.close()

    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _applied_migrations(con: sqlite3.Connection) -> list[str]:
    try:
        return [f"{r[0]} {r[1]}" for r in con.execute(
            "SELECT version, name FROM schema_migration ORDER BY version")]
    except sqlite3.OperationalError:
        return []


# ------------------------------------------------------------------- read back
def load_ndjson(path: Path,
                permitted_float_columns: frozenset[str] | set[str] | None = None
                ) -> Iterator[dict[str, Any]]:
    """Read an exported file, refusing a float outside the permitted columns.

    JSON has one number type and Python's decoder produces a float for anything
    written with a ``.`` or an exponent. The check is repeated on this side
    rather than trusted from the write side, because the whole point of the
    manifest is that the files can be handed to a different process on a
    different machine -- and that process should not have to take the exporter's
    word for it.

    ``permitted_float_columns`` defaults to the empty set, so a caller that
    forgets to pass it gets the STRICTER behaviour. A default that permitted
    everything would turn a forgotten argument into a silently disabled guard.
    """
    permitted = frozenset(permitted_float_columns or ())
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            for column, value in record.items():
                if isinstance(value, float) and column not in permitted:
                    raise ExportError(
                        f"{path.name} line {number}: column {column!r} decodes to "
                        f"the float {value!r}, and {column!r} is not declared as a "
                        "float column. Money is integer paise; every other numeric "
                        "column here is an integer count."
                    )
            yield record


def verify_manifest(out_dir: str | Path) -> list[str]:
    """Re-hash every exported file against the manifest. ``[]`` means intact."""
    out = Path(out_dir)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    for table, entry in manifest["tables"].items():
        path = out / entry["file"]
        if not path.exists():
            problems.append(f"{table}: {entry['file']} is missing")
            continue
        actual = _file_sha256(path)
        if actual != entry["sha256"]:
            problems.append(
                f"{table}: sha256 {actual} does not match manifest {entry['sha256']}")
        rows = sum(1 for _ in load_ndjson(path, frozenset(entry.get("float_columns", ()))))
        if rows != entry["rows"]:
            problems.append(f"{table}: {rows} rows on disk, manifest says {entry['rows']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="POC SQLite database (read only)")
    ap.add_argument("--out", required=True, help="output directory for NDJSON + manifest")
    ap.add_argument("--verify", action="store_true",
                    help="re-hash an existing export instead of writing one")
    args = ap.parse_args(argv)

    if args.verify:
        problems = verify_manifest(args.out)
        for p in problems:
            print(f"  FAIL {p}")
        print("export verified against its manifest." if not problems
              else f"{len(problems)} problem(s).")
        return 1 if problems else 0

    manifest = export(args.source, args.out)
    print(f"exported {manifest['row_count_total']} rows across "
          f"{len(manifest['tables'])} tables to {args.out}")
    audit = manifest["audit"]
    if audit.get("present"):
        print(f"  audit_log: {audit['hashed_rows']} hashed, "
              f"{audit['unhashed_rows']} unhashed (verifiable history begins at "
              f"audit_id {audit['verifiable_history_begins_at_audit_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
