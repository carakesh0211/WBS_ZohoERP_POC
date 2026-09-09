"""The POC export: deterministic, read-only, integer-safe, hash-preserving.

Runs everywhere. No PostgreSQL, no network. Every test here builds its own
disposable SQLite database in ``tmp_path``, so nothing in the repository's data
directory is opened, let alone written.

The four properties under test are the four that decide whether a migration is
evidence or a story:

* the export does not modify what it reads,
* two exports of the same source are byte-identical,
* a money value never becomes a float, in either direction,
* the one table whose bytes are hashed is copied verbatim.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.migration import export_poc                              # noqa: E402
from tools.migration.schema import open_source_readonly             # noqa: E402


# --------------------------------------------------------------------- fixture
SMALL_SCHEMA = """
CREATE TABLE entity (
  entity_id TEXT PRIMARY KEY,
  name      TEXT NOT NULL
);
CREATE TABLE budget_line (
  budget_line_id TEXT PRIMARY KEY,
  entity_id      TEXT NOT NULL REFERENCES entity(entity_id),
  project_id     TEXT,
  amount_paise   INTEGER NOT NULL,
  quantity       REAL,
  approved_at    TEXT
);
CREATE TABLE audit_log (
  audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT NOT NULL,
  detail      TEXT,
  prev_hash   TEXT,
  entry_hash  TEXT
);
"""


@pytest.fixture()
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "poc.db"
    con = sqlite3.connect(path)
    con.executescript(SMALL_SCHEMA)
    con.executemany("INSERT INTO entity VALUES (?,?)",
                    [("ENT-2", "Second"), ("ENT-1", "First")])
    con.executemany(
        "INSERT INTO budget_line VALUES (?,?,?,?,?,?)",
        [("BL-2", "ENT-1", "PRJ-1", 250050, 1.5, "2026-01-02T03:04:05+05:30"),
         ("BL-1", "ENT-1", "PRJ-1", 1, 2.0, "2026-01-02T03:04:05"),
         ("BL-3", "ENT-2", "PRJ-2", -99, None, "2026-01-03")])
    con.executemany(
        "INSERT INTO audit_log (at,actor,action,object_type,object_id,detail,"
        "prev_hash,entry_hash) VALUES (?,?,?,?,?,?,?,?)",
        [("2026-01-01T00:00:00", "U-ADM", "SEED", "System", "-", "seeded", None, None),
         ("2026-01-02T00:00:00+00:00", "U-A", "APPROVE", "PR", "PR-1", "d", "", "aa" * 32)])
    con.commit()
    con.close()
    return path


# ------------------------------------------------------------------- read-only
def test_source_is_opened_read_only_and_a_write_is_refused(source_db: Path):
    """A migration that can modify its own source cannot be re-run to compare."""
    con = open_source_readonly(source_db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("INSERT INTO entity VALUES ('ENT-9','Nine')")
    con.close()


def test_export_does_not_change_the_source_file(source_db: Path, tmp_path: Path):
    before = export_poc._file_sha256(source_db)
    export_poc.export(source_db, tmp_path / "out")
    assert export_poc._file_sha256(source_db) == before


# ----------------------------------------------------------------- determinism
def test_two_exports_of_the_same_source_are_byte_identical(source_db: Path, tmp_path: Path):
    """Not 'equivalent'. Identical bytes, manifest included.

    Anything less and "the export has not changed" stops being checkable and
    becomes a claim, which is what this whole exercise exists to avoid.
    """
    first, second = tmp_path / "a", tmp_path / "b"
    export_poc.export(source_db, first)
    export_poc.export(source_db, second)
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_row_order_is_the_primary_key_not_insertion_order(source_db: Path, tmp_path: Path):
    """Rows were inserted BL-2, BL-1, BL-3 and must export BL-1, BL-2, BL-3.

    Insertion order and rowid are both unstable -- rowid across a VACUUM, and
    insertion order across any re-seed. The declared primary key is the only
    order that survives both.
    """
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    ids = [r["budget_line_id"] for r in
           export_poc.load_ndjson(out / "budget_line.ndjson", {"quantity"})]
    assert ids == ["BL-1", "BL-2", "BL-3"]


def test_json_keys_are_sorted_so_column_order_cannot_drift(source_db: Path, tmp_path: Path):
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    line = (out / "entity.ndjson").read_text(encoding="utf-8").splitlines()[0]
    keys = list(json.loads(line).keys())
    assert keys == sorted(keys)


# ------------------------------------------------------------------------ money
def test_paise_survive_as_integers_including_one_and_a_negative(
        source_db: Path, tmp_path: Path):
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    rows = {r["budget_line_id"]: r for r in
            export_poc.load_ndjson(out / "budget_line.ndjson", {"quantity"})}
    for key, expected in (("BL-1", 1), ("BL-2", 250050), ("BL-3", -99)):
        value = rows[key]["amount_paise"]
        assert value == expected
        assert isinstance(value, int) and not isinstance(value, bool)
        # `1.0 == 1` in Python, so equality alone would pass on a float.
        assert type(value) is int


def test_a_float_in_a_paise_column_is_refused_not_rounded(tmp_path: Path):
    """SQLite is dynamically typed: an INTEGER column will happily hold 12.5."""
    path = tmp_path / "bad.db"
    con = sqlite3.connect(path)
    con.executescript(SMALL_SCHEMA)
    con.execute("INSERT INTO entity VALUES ('ENT-1','First')")
    con.execute("INSERT INTO budget_line VALUES ('BL-1','ENT-1','P',12.5,1.0,NULL)")
    con.commit()
    con.close()
    with pytest.raises(export_poc.ExportError, match="paise column is an integer"):
        export_poc.export(path, tmp_path / "out")


def test_a_declared_float_column_is_permitted_and_recorded(source_db: Path, tmp_path: Path):
    """`quantity` is genuinely fractional. The guard must be per column, not
    a blanket 'no floats', or it would refuse the POC's own valid data."""
    out = tmp_path / "out"
    manifest = export_poc.export(source_db, out)
    assert manifest["tables"]["budget_line"]["float_columns"] == ["quantity"]
    rows = list(export_poc.load_ndjson(out / "budget_line.ndjson", {"quantity"}))
    assert any(isinstance(r["quantity"], float) for r in rows)


def test_load_ndjson_defaults_to_refusing_every_float(source_db: Path, tmp_path: Path):
    """A forgotten argument must make the guard STRICTER, never weaker."""
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    with pytest.raises(export_poc.ExportError, match="not declared as a float column"):
        list(export_poc.load_ndjson(out / "budget_line.ndjson"))


# ------------------------------------------------------------------ timestamps
def test_offsets_are_converted_to_utc_and_naive_values_are_recorded_as_assumed(
        source_db: Path, tmp_path: Path):
    out = tmp_path / "out"
    manifest = export_poc.export(source_db, out)
    rows = {r["budget_line_id"]: r for r in
            export_poc.load_ndjson(out / "budget_line.ndjson", {"quantity"})}
    assert rows["BL-2"]["approved_at"] == "2026-01-01T21:34:05+00:00"   # +05:30 -> UTC
    assert rows["BL-1"]["approved_at"] == "2026-01-02T03:04:05+00:00"   # naive, assumed UTC
    dispositions = manifest["tables"]["budget_line"]["timestamp_dispositions"]
    assert dispositions["naive_as_utc"] == 1
    assert dispositions["offset_to_utc"] == 1


def test_a_date_is_not_promoted_to_an_instant(source_db: Path, tmp_path: Path):
    """`2026-01-03` is a date. Turning it into a timestamp invents a time of
    day and a timezone the source never recorded."""
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    rows = {r["budget_line_id"]: r for r in
            export_poc.load_ndjson(out / "budget_line.ndjson", {"quantity"})}
    assert rows["BL-3"]["approved_at"] == "2026-01-03"


def test_audit_log_timestamps_are_verbatim_because_the_bytes_are_hashed(
        source_db: Path, tmp_path: Path):
    """The single most important line in this file.

    `audit_log.at` is inside the frozen payload `prev|at|actor|action|type|id|
    detail`. Normalising a naive '2026-01-01T00:00:00' to '...+00:00' changes
    the bytes, changes the hash, and turns an intact chain into a broken one --
    a false tamper alarm on the one guarantee this product exists to make.
    """
    out = tmp_path / "out"
    manifest = export_poc.export(source_db, out)
    rows = list(export_poc.load_ndjson(out / "audit_log.ndjson"))
    assert rows[0]["at"] == "2026-01-01T00:00:00"          # NOT '+00:00'
    assert rows[1]["at"] == "2026-01-02T00:00:00+00:00"
    assert manifest["tables"]["audit_log"]["timestamp_dispositions"] == {}
    assert "hash-bearing" in manifest["tables"]["audit_log"]["normalisation"]


def test_the_empty_string_prev_hash_is_exported_verbatim(source_db: Path, tmp_path: Path):
    """The POC writes '' for the genesis row where PostgreSQL writes NULL. The
    EXPORT keeps '', because the export's job is to record what the source says.
    The '' -> NULL decision belongs to the import, where it is documented."""
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    rows = list(export_poc.load_ndjson(out / "audit_log.ndjson"))
    assert rows[0]["prev_hash"] is None
    assert rows[1]["prev_hash"] == ""


# -------------------------------------------------------------------- manifest
def test_manifest_sha256_matches_the_bytes_on_disk(source_db: Path, tmp_path: Path):
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    assert export_poc.verify_manifest(out) == []


def test_a_tampered_export_file_fails_manifest_verification(source_db: Path, tmp_path: Path):
    out = tmp_path / "out"
    export_poc.export(source_db, out)
    target = out / "entity.ndjson"
    target.write_text(target.read_text(encoding="utf-8").replace("First", "Firs7"),
                      encoding="utf-8")
    problems = export_poc.verify_manifest(out)
    assert any("sha256" in p for p in problems)


def test_manifest_row_counts_match_the_source(source_db: Path, tmp_path: Path):
    manifest = export_poc.export(source_db, tmp_path / "out")
    assert manifest["tables"]["budget_line"]["rows"] == 3
    assert manifest["tables"]["entity"]["rows"] == 2
    assert manifest["row_count_total"] == 3 + 2 + 2


def test_manifest_records_where_verifiable_history_begins(source_db: Path, tmp_path: Path):
    """An auditor must be able to read the boundary, not infer it from a total."""
    manifest = export_poc.export(source_db, tmp_path / "out")
    audit = manifest["audit"]
    assert audit["total_rows"] == 2
    assert audit["unhashed_rows"] == 1
    assert audit["hashed_rows"] == 1
    assert audit["verifiable_history_begins_at_audit_id"] == 2
