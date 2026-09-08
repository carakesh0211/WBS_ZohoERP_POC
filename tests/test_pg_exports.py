"""Migration 018 and the export machinery: chunking, money, determinism.

`tests/test_export_scope.py` proves the authorisation property. This file
proves the rest of `docs/WAVE7_CONTRACT.md`'s export contract:

  * 202 with a job id, and no route that can exceed the request budget --
    which here means: the work is BOUNDED per invocation and resumable, so no
    single call's size depends on how large the export is;
  * deterministic column ordering, captured at creation;
  * money formatted exactly from integer paise;
  * progress, cancellation, retry, expiry, downloadable-result metadata;
  * an export audit event.

Split the way `tests/test_pg_reservations.py` is. The first half runs with NO
DATABASE -- those are properties of migration 018's TEXT, of the runner's own
discovery, and of pure functions in `app/backend/pg/exports.py`. The second
half carries `@pytest.mark.pg` plus a `skipif` on `CAPEX_DB_URL`, SKIPS on
every workstation here, and FIRST EXECUTES IN CI's `pg_tests` job.

A SKIP IS NOT A PASS, and the shared skip reason says so.

ON THE 250,000-ROW TARGET
=========================

The approved target is a chunked export of up to 250,000 rows. The live tests
below run tens of rows with a chunk size of 2 and a per-invocation budget of a
handful, which drives the SAME code path harder -- many chunks, many
invocations, many resumes -- in a second rather than in minutes.

What is asserted is the property that makes 250,000 work:
`test_no_invocation_reads_more_than_its_row_budget` and
`test_chunking_is_invisible_in_the_output` together say the file does not
depend on how the work was divided and the work per call does not depend on
how large the export is. A test that actually materialised 250,000 rows would
assert the same two things more slowly. The scale figure is a capacity claim
and is recorded as one, not smuggled in as a passing assertion.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys as _sys
from decimal import Decimal
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import exports as export_svc  # noqa: E402
from app.backend.pg import migrate_pg, principal_scope  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
_018_PATH = MIGRATIONS / "018_export_jobs.sql"
_018 = _018_PATH.read_text(encoding="utf-8")

#: 018 with its prose removed. Every "the migration does not do X" assertion
#: reads THIS: the trailing `-- ROLLBACK:` block is commented-out DDL, and
#: parsing it as real DDL inverts the meaning of every DROP and CREATE in it.
#: The same device, for the same reason, as `test_pg_reservations._015_CODE`.
_018_CODE = re.sub(r"--[^\n]*", "", _018)

#: The applied body: everything before the final COMMIT.
_018_APPLIED = _018_CODE[:_018_CODE.rindex("COMMIT;")]

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that run a chunked export end to end."),
)


def _migration(version: str) -> migrate_pg.Migration:
    for m in migrate_pg.discover():
        if m.version == version:
            return m
    raise AssertionError(f"migration {version} is not discovered by the runner")


# =========================================================================
# Migration 018: it is a migration, it is additive, and it is complete
# =========================================================================
def test_018_is_discovered_named_and_numbered():
    versions = [m.version for m in migrate_pg.discover()]
    assert versions == sorted(versions)
    assert versions.count("018") == 1
    assert _migration("018").name == "export_jobs"
    assert "018_export_jobs.sql" not in migrate_pg.NON_MIGRATION_FILES


# 016 and 018 joined this list at integration: the branch was cut before
# either existed, and an already-applied migration is an already-applied
# migration whatever wave produced it.
@pytest.mark.parametrize("earlier", ["013_procurement.sql",
                                     "014_procurement_corrections.sql",
                                     "015_reservation_grain.sql",
                                     "016_grn_line_ordinal.sql",
                                     "017_reporting.sql"])
def test_018_does_not_touch_an_already_applied_migration(earlier):
    """An applied migration's SHA-256 is in `schema_migrations` on every
    database where it ran. Editing one makes `assert_schema_current` report
    drift and every existing deployment refuse to boot -- which is why 014
    exists rather than 013 having been corrected in place."""
    text = (MIGRATIONS / earlier).read_text(encoding="utf-8")
    assert "export_job" not in text, (
        f"{earlier} mentions export_job; 018 must be additive and must not "
        f"have been produced by editing an applied migration")


def test_018_creates_only_its_own_two_tables():
    created = re.findall(r"^CREATE TABLE (\w+)", _018_APPLIED, flags=re.MULTILINE)
    assert created == ["export_job", "export_job_chunk"]


def test_018_names_every_constraint_it_creates():
    """An unnamed CHECK gets a generated name nobody can look up, and
    `migrate_pg`'s adoption check verifies constraints BY NAME -- an unnamed
    one is not verified, it is skipped."""
    body = _018_APPLIED
    # No bare CHECK/PRIMARY KEY/FOREIGN KEY at the start of a field line: every
    # one of them must be preceded by CONSTRAINT <name>.
    unnamed = re.findall(r"^\s{4}(CHECK|PRIMARY KEY|FOREIGN KEY|UNIQUE)\b",
                         body, flags=re.MULTILINE)
    assert unnamed == [], f"018 carries unnamed constraints: {unnamed}"


@pytest.mark.parametrize("name", [
    "pk_export_job",
    "ck_export_job_state",
    "ck_export_job_output_format",
    "ck_export_job_principal_kind",
    "ck_export_job_requested_by_present",
    "ck_export_job_scope_digest_shape",
    "ck_export_job_scope_json_is_object",
    "ck_export_job_filter_json_is_object",
    "ck_export_job_column_order_nonempty",
    "ck_export_job_chunk_rows_bounded",
    "ck_export_job_counters_nonneg",
    "ck_export_job_attempts",
    "ck_export_job_terminal_is_finished",
    "ck_export_job_failure_is_coded",
    "ck_export_job_succeeded_is_complete",
    "ck_export_job_result_digest_shape",
    "ck_export_job_expires_after_creation",
    "pk_export_job_chunk",
    "fk_export_job_chunk_job",
    "ck_export_job_chunk_no_positive",
    "ck_export_job_chunk_row_count_positive",
    "ck_export_job_chunk_byte_count_nonneg",
    "ck_export_job_chunk_digest_shape",
])
def test_018_declares_the_named_constraint(name):
    assert f"CONSTRAINT {name}" in _018_APPLIED


def test_018_makes_the_captured_authorisation_immutable_in_the_database():
    """A CHECK cannot see OLD, so immutability is a trigger. Every captured
    field must be in it -- an omitted one is editable, and "captured
    immutably" would be a claim rather than a guarantee."""
    assert "CREATE TRIGGER trg_export_job_identity_immutable" in _018_APPLIED
    guard = _018_APPLIED[_018_APPLIED.index("export_job_identity_is_immutable()"):]
    for column in ("export_job_id", "requested_by", "requested_principal_kind",
                   "scope_json", "scope_digest", "dataset", "filter_json",
                   "column_order", "created_at"):
        assert f"NEW.{column}" in guard, (
            f"{column} is captured at creation but the immutability trigger "
            f"does not guard it")


def test_018_makes_chunks_append_only_by_privilege_as_well_as_by_trigger():
    """The trigger is the guarantee; the REVOKE is what stops the attempt
    reaching it. 004's ALTER DEFAULT PRIVILEGES already granted everything, so
    the REVOKE -- not the GRANT -- is the line that removes a privilege."""
    assert "CREATE TRIGGER trg_export_job_chunk_append_only" in _018_APPLIED
    assert "REVOKE UPDATE ON export_job_chunk FROM capex_app;" in _018_APPLIED
    assert "REVOKE DELETE ON export_job FROM capex_app;" in _018_APPLIED
    # ...and DELETE on a CHUNK is granted on purpose: expiry purges bytes while
    # the job row recording who exported what stays.
    assert "GRANT DELETE ON export_job_chunk TO capex_app;" in _018_APPLIED


def test_018_enables_forces_and_policies_both_tables():
    for table in ("export_job", "export_job_chunk"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in _018_APPLIED
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in _018_APPLIED
        assert re.search(rf"CREATE POLICY \w+ ON {table}\b", _018_APPLIED)


def test_018_declares_a_rollback_naming_every_policy_and_trigger_it_creates():
    assert "-- ROLLBACK:" in _018
    rollback = _018.split("-- ROLLBACK:", 1)[1]
    for policy, table in re.findall(r"^CREATE POLICY (\w+) ON (\w+)",
                                    _018_APPLIED, flags=re.MULTILINE):
        assert f"DROP POLICY IF EXISTS {policy} ON {table};" in rollback
    for trigger, table in re.findall(
            r"CREATE TRIGGER (\w+) BEFORE UPDATE ON (\w+)", _018_APPLIED):
        assert f"DROP TRIGGER IF EXISTS {trigger} ON {table};" in rollback


def test_018_does_not_redefine_the_frozen_scope_functions():
    """Contract 1 freezes `capex_scope_permits` / `capex_dimension_permits`
    and 007 replaces their bodies. A later migration must CALL them, never
    redefine them: two migrations editing one function body is a silent
    last-writer-wins collision."""
    for function in ("capex_scope_permits", "capex_dimension_permits",
                     "capex_principal_present"):
        assert not re.search(
            rf"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+{function}\b", _018_CODE)
    assert "capex_principal_present()" in _018_APPLIED


def test_018_carries_no_paise_column():
    """Not a rule, an observation worth pinning: money in an export lives in
    the RENDERED chunk as an exact decimal string, never as a second copy of a
    paise figure that could drift from the source."""
    assert "_paise" not in _018_APPLIED


# =========================================================================
# Money: exact, from integer paise, never through a float
# =========================================================================
@pytest.mark.parametrize("paise,expected", [
    (0, "0.00"), (1, "0.01"), (99, "0.99"), (100, "1.00"), (105, "1.05"),
    (123456, "1234.56"), (-1, "-0.01"), (-100, "-1.00"), (-123456, "-1234.56"),
    # Beyond float64's exact-integer range. A float path renders the first of
    # these as "9007199254740994.00" -- a whole rupee out, and the paise gone.
    (900719925474099301, "9007199254740993.01"),
    (99999999999999999, "999999999999999.99"),
    (9223372036854775807, "92233720368547758.07"),   # bigint max
])
def test_money_is_formatted_exactly_from_integer_paise(paise, expected):
    assert export_svc.format_paise(paise) == expected


@pytest.mark.parametrize("paise,float_says", [
    (900719925474099301, "9007199254740994.00"),
    (99999999999999999, "1000000000000000.00"),
    (9223372036854775807, "92233720368547760.00"),
])
def test_the_money_formatter_would_fail_on_a_float_path(paise, float_says):
    """The guard's own proof: the exact values this codebase refuses to produce.

    Each of these is a real `bigint` a paise column can hold, and each renders
    WRONG through `paise / 100` -- the last one loses seven paise and two
    rupees, with no error anywhere. A test that only asserted the correct
    output would pass just as happily against a float implementation on the
    small numbers everybody tries.
    """
    assert f"{paise / 100:.2f}" == float_says
    assert export_svc.format_paise(paise) != float_says


@pytest.mark.parametrize("bad", [1.0, Decimal("1.00"), "100", None, True])
def test_the_money_formatter_refuses_anything_that_is_not_an_int(bad):
    """A Decimal here means a `SUM()` upstream lost its `::bigint` cast; a
    float means somebody divided by 100 early. Both are worth failing on."""
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.format_paise(bad)
    assert caught.value.code == "MONEY_NOT_INTEGER_PAISE"


def test_only_a_paise_column_goes_through_the_money_formatter():
    """A money column's leading `-` is a minus sign, and neutralising it would
    turn every negative figure into text."""
    assert export_svc.render_value("paise", -123456) == "-1234.56"
    assert not export_svc.render_value("paise", -123456).startswith("'")


# =========================================================================
# Rendering: deterministic, header-free chunks, injection-safe text
# =========================================================================
def test_a_chunk_carries_no_header():
    """The header belongs to the assembled file. Repeating it per chunk is how
    a 250,000-row export acquires 50 spurious rows in the middle of it."""
    dataset = export_svc.WBS_ELEMENTS
    row = tuple([None] * len(dataset.columns))
    body = export_svc.render_rows(dataset, [row])
    assert dataset.columns[0].name not in body
    assert body.count("\n") == 1


def test_chunks_use_a_bare_newline_so_byte_counts_and_digests_are_portable():
    """`csv`'s default terminator is CRLF, which makes the byte count and the
    SHA-256 of an identical export differ from what a reader computing them on
    Unix expects."""
    dataset = export_svc.WBS_ELEMENTS
    body = export_svc.render_rows(dataset, [tuple([None] * len(dataset.columns))])
    assert "\r" not in body
    assert body.endswith("\n")


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-2+3", "@import", "\tx"])
def test_a_text_cell_that_a_spreadsheet_would_evaluate_is_neutralised(value):
    """A WBS description reading `=cmd|' /c calc'!A0` is data here and must
    stay data when the file is opened."""
    assert export_svc.render_value("text", value).startswith("'")
    assert export_svc.render_value("text", value)[1:] == value


def test_a_timestamp_renders_in_utc_whatever_the_connection_is_set_to():
    """`timestamptz` comes back rendered in the CONNECTION's TimeZone, so an
    export run from a differently-configured process would otherwise disagree
    with itself about the same instant."""
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    at = datetime(2026, 9, 8, 17, 30, tzinfo=ist)
    assert export_svc.render_value("timestamp", at) == "2026-09-08T12:00:00+00:00"
    naive = datetime(2026, 9, 8, 12, 0)
    assert export_svc.render_value("timestamp", naive) == "2026-09-08T12:00:00+00:00"


def test_a_row_of_the_wrong_width_is_a_refusal_not_a_ragged_file():
    dataset = export_svc.WBS_ELEMENTS
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.render_rows(dataset, [("only", "three", "columns")])
    assert caught.value.code == "COLUMN_COUNT_MISMATCH"


# =========================================================================
# Datasets: ordered columns, a unique sort key, four dimensions
# =========================================================================
def test_every_dataset_declares_a_non_empty_ordered_column_list():
    for name, dataset in sorted(export_svc.DATASETS.items()):
        names = dataset.column_names
        assert names, name
        assert len(set(names)) == len(names), f"{name} repeats a column name"
        # Ordered, and the order is what a job captures -- so it must be a
        # tuple, not a set or a dict view that could reorder.
        assert isinstance(names, tuple)


def test_every_dataset_sort_key_is_backed_by_a_uniqueness_guarantee():
    """A chunked export resumes from the last row it committed, so a
    non-unique sort makes "the row after this one" ambiguous -- which
    duplicates or drops rows at every chunk boundary.

    Each key is checked against the uniqueness the SCHEMA actually declares,
    read from the migration text rather than asserted from memory.
    """
    schema = "\n".join(
        (MIGRATIONS / f).read_text(encoding="utf-8")
        for f in ("002_budget_control.sql", "013_procurement.sql"))
    # budget_ledger_cells: ux_wbs_path makes wbs_path unique; the cell PK makes
    # (wbs_id, budget_head_id) unique.
    assert "CREATE UNIQUE INDEX ux_wbs_path ON wbs_element (wbs_path)" in schema
    assert export_svc.BUDGET_LEDGER_CELLS.key_sql == (
        "w.wbs_path::text", "bc.budget_head_id")
    assert export_svc.WBS_ELEMENTS.key_sql == ("w.wbs_path::text",)
    # purchase_order_lines: po_number unique, line_no unique within a PO.
    assert "CONSTRAINT ux_purchase_order_number UNIQUE (po_number)" in schema
    assert "CONSTRAINT ux_po_line_number UNIQUE (po_id, line_no)" in schema
    assert export_svc.PURCHASE_ORDER_LINES.key_sql == (
        "po.po_number", "pl.line_no::text")


def test_the_ordered_column_is_transcribed_from_compute_ledger_not_re_derived():
    """`docs/WAVE7_CONTRACT.md`: the ten metrics come from
    `domain.compute_ledger` verbatim. `ordered` is the one this dataset
    computes rather than reads, so it is checked against the source
    expression -- creditable tax is NOT in it, because it is recoverable and is
    not a charge against the budget."""
    domain_source = (ROOT / "app" / "backend" / "domain.py").read_text(
        encoding="utf-8")
    assert ("pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise"
            in domain_source)
    ordered = next(c for c in export_svc.PURCHASE_ORDER_LINES.columns
                   if c.name == "ordered")
    assert ordered.sql == (
        "(pl.amount_paise + pl.non_creditable_tax_paise + pl.freight_paise)")
    assert "pl.tax_paise" not in ordered.sql


# =========================================================================
# Filters: applied, or refused by name
# =========================================================================
def test_a_filter_this_dataset_cannot_apply_is_refused_by_name():
    """Dropping it would widen the result, and the recipient of the file has no
    way to notice."""
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.plan_job("budget_ledger_cells", {"period_ids": ["AP-2026-04"]})
    assert caught.value.code == "UNSUPPORTED_FILTER"
    assert caught.value.detail["field"] == "period_ids"


def test_an_unknown_filter_field_is_refused_rather_than_ignored():
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.plan_job("budget_ledger_cells", {"plant_id": ["PL-1"]})
    assert caught.value.code == "UNKNOWN_FILTER"


def test_paging_and_sorting_fields_are_refused_on_an_export():
    """An export delivers the WHOLE result set in a captured order. A cursor
    would truncate it silently and a sort would break the resume."""
    for field in ("cursor", "limit", "sort", "group_by"):
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.plan_job("wbs_elements", {field: "anything"})
        assert caught.value.code == "UNSUPPORTED_FILTER"


def test_every_declared_filter_actually_compiles_to_sql():
    """The mirror of the refusal: a field a dataset CLAIMS to support must
    build a fragment, or the claim in `/api/exports/datasets` is wrong."""
    sample = {
        "entity_ids": ["E1"], "plant_ids": ["P1"], "location_ids": ["L1"],
        "project_ids": ["PR1"], "wbs_paths": ["a.b"], "budget_head_ids": ["BH1"],
        "lifecycle_statuses": ["Released"], "date_from": "2026-01-01",
        "date_to": "2026-12-31",
    }
    for name, dataset in sorted(export_svc.DATASETS.items()):
        supported = {k: v for k, v in sample.items() if k in dataset.filters}
        assert supported, f"{name} supports none of the sample filters"
        conditions, params = export_svc.plan_filters(dataset, supported)
        assert len(conditions) == len(supported), name
        for condition in conditions:
            assert "%(" in condition, (
                f"{name} built a filter fragment with no parameter: "
                f"{condition!r} -- an inlined value is an injection")


def test_no_filter_parameter_can_collide_with_the_scope_predicate():
    """`repo.query` refuses a params dict that collides with `__scope_`, and
    it refuses it by RAISING -- so a colliding name is an outage, not a
    silent widening. Better not to build one."""
    _conditions, params = export_svc.plan_filters(
        export_svc.WBS_ELEMENTS, {"entity_ids": ["E1"], "plant_ids": ["P1"]})
    assert all(not p.startswith("__scope_") for p in params)


def test_an_empty_filter_list_is_not_a_filter():
    """`{"entity_ids": []}` means "did not narrow", not "narrow to nothing".
    Narrowing to nothing is what a DENIED SCOPE means, and a filter must never
    be able to say it -- a filter narrows within a scope; it does not replace
    one."""
    assert export_svc.normalise_filters({"entity_ids": []}) == {}
    assert export_svc.normalise_filters({"date_from": "  "}) == {}
    assert export_svc.normalise_filters(None) == {}


# =========================================================================
# The public view of a job
# =========================================================================
def _fake_job(**overrides):
    job = {
        "export_job_id": "EXP-1", "dataset": "wbs_elements",
        "output_format": "csv", "state": "RUNNING",
        "requested_by": "U-1", "requested_principal_kind": "USER",
        "scope_json": {"user_id": "U-1", "entity_ids": ["SECRET-ENT"]},
        "scope_digest": "a" * 64, "filter_json": {},
        "column_order": ["a", "b"], "chunk_rows": 100, "rows_written": 10,
        "rows_total": None, "chunks_written": 1, "resume_key": None,
        "attempt": 1, "max_attempts": 3, "cancel_requested": False,
        "error_code": None, "error_detail": None, "result_sha256": None,
        "result_bytes": None, "result_filename": None,
        "result_media_type": None, "correlation_id": "c1",
        "created_at": "2026-09-08T00:00:00+00:00", "started_at": None,
        "finished_at": None, "expires_at": "2026-09-09T00:00:00+00:00",
    }
    job.update(overrides)
    return job


def test_the_public_job_never_echoes_the_captured_scope():
    """The resolved scope is the authorisation the job runs under. Echoing it
    tells a caller the exact id set they hold, which is more than the rest of
    this API discloses and more than a poller needs."""
    rendered = json.dumps(export_svc.public_job(_fake_job()))
    assert "SECRET-ENT" not in rendered
    assert "scope_json" not in rendered
    assert "scope_digest" not in rendered


def test_progress_reports_no_percentage_while_the_total_is_unknown():
    """`rows_total` is NULL until the last chunk. A percentage needs a
    denominator, and this build does not invent one."""
    running = export_svc.public_job(_fake_job())["progress"]
    assert running["rows_total"] is None
    assert running["percent"] is None
    assert running["percent_note"]

    done = export_svc.public_job(
        _fake_job(rows_total=40, rows_written=10))["progress"]
    assert done["percent"] == 25
    assert done["percent_note"] is None


def test_a_result_is_advertised_only_once_the_job_has_succeeded():
    assert export_svc.public_job(_fake_job(state="RUNNING"))["result"] is None
    assert export_svc.public_job(_fake_job(state="FAILED",
                                           finished_at="x"))["result"] is None


# =========================================================================
# LIVE
# =========================================================================
_ORG = "ORG-EXPM"
_ENT = "ENT-EXPM"
_PLT = "PL-EXPM"
_LOC = "LOC-EXPM"
_PRJ = "PRJ-EXPM"
_USER = "U-EXPM"
_CELLS = 11


def _seed(session, *, cells: int = _CELLS) -> None:
    """One chain, `cells` budget control cells, and one unrestricted user.

    `cells` is deliberately not a multiple of the chunk size used below, so the
    last chunk is a SHORT one -- the boundary a resume gets wrong.
    """
    ex = session.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s, 'EXPM', 'Export Machinery', 'TEST', 'TEST')",
       (_ORG,))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')",
       (_ENT, _ORG, _ENT, _ENT))
    ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, "
       "updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')",
       (_PLT, _ENT, _PLT, _PLT))
    ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
       "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
       (_LOC, _ENT, _PLT, _LOC, _LOC))
    ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, "
       "capex_code, name, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, %s, 'TEST', 'TEST')",
       (_PRJ, _ENT, _PLT, _LOC, _PRJ, _PRJ))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES ('BH-EXPM', %s, 'BH-EXPM', 'BH-EXPM', "
       "'TEST', 'TEST')", (_ENT,))
    for i in range(cells):
        wbs = f"WBS-EXPM-{i:03d}"
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
           "wbs_path, created_by, updated_by) "
           "VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (wbs, _PRJ, wbs, f"element {i}", f"expm_{i:03d}"))
        # Amounts chosen so the rendered rupee figures are all distinct and a
        # duplicated or dropped row shows up as a changed total.
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_paise, updated_by) VALUES (%s, 'BH-EXPM', %s, 'TEST')",
           (wbs, 100_000 + i))
        ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, "
           "commitment_paise, actual_paise, pr_reserved_paise, updated_by) "
           "VALUES (%s, 'BH-EXPM', %s, %s, %s, 'TEST')",
           (wbs, 100 + i, 200 + i, 300 + i))
    ex("INSERT INTO app_user (user_id, email, display_name, created_by, "
       "updated_by) VALUES (%s, %s, %s, 'TEST', 'TEST')",
       (_USER, f"{_USER}@example.test", _USER))


def _scope(database) -> Scope:
    return principal_scope.scope_for_request(database, {"user_id": _USER})


def _queue(database, *, chunk_rows=2, dataset="budget_ledger_cells",
           filters=None) -> str:
    scope = _scope(database)
    with database.session(scope) as session:
        return export_svc.create_job(
            session, dataset=dataset, filters=filters, scope=scope,
            requested_by=_USER, chunk_rows=chunk_rows)["export_job_id"]


def _drain(database, job_id, *, rows_budget=3, limit=40):
    outcomes = []
    for _ in range(limit):
        outcome = export_svc.advance_job(database, job_id,
                                         rows_budget=rows_budget)
        outcomes.append(outcome)
        if not outcome.get("more"):
            return outcomes
    raise AssertionError(f"{job_id} did not finish in {limit} invocations")


def _result(database, job_id) -> tuple[str, dict]:
    with database.session(_scope(database)) as session:
        return export_svc.read_result(session, job_id, requester=_USER)


@PG
def test_a_chunked_export_completes_and_reports_a_counted_total(pg_database):
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    outcomes = _drain(pg_database, job_id)

    assert outcomes[-1]["state"] == "SUCCEEDED"
    assert outcomes[-1]["rows_total"] == _CELLS
    assert len(outcomes) > 1, (
        "the export finished in one invocation; the chunking path is untested")

    body, meta = _result(pg_database, job_id)
    lines = body.strip().splitlines()
    assert len(lines) == _CELLS + 1                       # header + rows
    assert meta["rows"] == _CELLS
    assert meta["sha256"] == export_svc.sha256_text(body)
    assert meta["bytes"] == len(body.encode("utf-8"))
    assert meta["filename"].endswith(".csv")


@PG
def test_chunking_is_invisible_in_the_output(pg_database):
    """The file must not depend on how the work was divided. This is half of
    what makes the 250,000-row target a capacity question rather than a
    correctness one."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    many = _queue(pg_database, chunk_rows=2)
    _drain(pg_database, many, rows_budget=3)
    one = _queue(pg_database, chunk_rows=export_svc.MAX_CHUNK_ROWS)
    _drain(pg_database, one, rows_budget=export_svc.DEFAULT_ROWS_PER_INVOCATION)

    body_many, meta_many = _result(pg_database, many)
    body_one, meta_one = _result(pg_database, one)
    assert body_many == body_one
    assert meta_many["sha256"] == meta_one["sha256"]

    with pg_database.session(Scope.system()) as session:
        chunks_many, chunks_one = (
            session.fetchone(
                "SELECT COUNT(*) FROM export_job_chunk WHERE export_job_id = %s",
                (job,))[0] for job in (many, one))
    assert chunks_many > chunks_one == 1, (
        "the two runs used the same number of chunks; nothing was compared")


@PG
def test_no_invocation_reads_more_than_its_row_budget(pg_database):
    """The other half: work per call does not depend on how large the export
    is. That is what keeps every route inside the 30-second budget at 250,000
    rows as much as at 11."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=2)
    for outcome in _drain(pg_database, job_id, rows_budget=3):
        assert outcome["rows_written_now"] <= 3, outcome


@PG
def test_the_rows_are_contiguous_across_a_resume(pg_database):
    """A keyset resume must neither drop a row nor emit one twice. Every WBS
    code appears exactly once, and all of them appear."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=2)
    _drain(pg_database, job_id, rows_budget=3)

    body, _meta = _result(pg_database, job_id)
    rows = list(csv.DictReader(io.StringIO(body)))
    codes = [r["wbs_code"] for r in rows]
    assert codes == sorted(codes), "the export is not in its declared order"
    assert len(codes) == len(set(codes)) == _CELLS
    assert set(codes) == {f"WBS-EXPM-{i:03d}" for i in range(_CELLS)}


@PG
def test_the_exported_ledger_figures_equal_the_budget_services_own(pg_database):
    """The metrics are DERIVED, never re-implemented. `budget.list_cells` is
    where `exposure` and `available` are defined; this asserts the export
    agrees with it cell for cell, rather than trusting the comment that says
    the expression was transcribed."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=3)
    _drain(pg_database, job_id, rows_budget=5)
    body, _meta = _result(pg_database, job_id)
    exported = {r["wbs_id"]: r for r in csv.DictReader(io.StringIO(body))}

    with pg_database.session(_scope(pg_database)) as session:
        cells = budget_svc.list_cells(session, project_id=_PRJ, limit=200)

    assert len(cells["items"]) == _CELLS
    for cell in cells["items"]:
        row = exported[cell["wbs_id"]]
        for metric in ("budget", "original", "revisions", "ordered",
                       "commitment", "actual", "received",
                       "received_not_billed", "pr_reserved", "exposure",
                       "available"):
            assert row[metric] == export_svc.format_paise(
                cell[f"{metric}_paise"]), (
                f"{cell['wbs_id']}.{metric}: export says {row[metric]}, "
                f"budget.list_cells says {cell[f'{metric}_paise']} paise")


@PG
def test_an_export_writes_its_audit_events(pg_database):
    """`docs/WAVE7_CONTRACT.md`: every export writes an audit event. Two, in
    fact -- the request and the completion -- and the completion records whose
    scope it ran under, which is the fact an auditor needs."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    _drain(pg_database, job_id)

    with pg_database.session(Scope.system()) as session:
        entries = session.fetchall(
            "SELECT action, actor, detail FROM audit_log "
            "WHERE object_type = 'export_job' AND object_id = %s ORDER BY seq",
            (job_id,))
    actions = [a for a, _actor, _d in entries]
    assert actions == ["export.requested", "export.completed"]
    assert all(actor == _USER for _a, actor, _d in entries)
    completion = json.loads(entries[-1][2])
    assert completion["rows"] == _CELLS
    assert completion["ran_under_scope_of"] == _USER


@PG
def test_cancelling_stops_the_export_at_a_chunk_boundary(pg_database):
    """A RUNNING job stops after its current chunk COMMITS. Killing it
    mid-chunk would leave the committed prefix and the counters disagreeing,
    and the counters are what `read_result` verifies the file against."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=2)

    first = export_svc.advance_job(pg_database, job_id, rows_budget=2)
    assert first["more"] is True
    with pg_database.session(_scope(pg_database)) as session:
        job = export_svc.request_cancel(session, job_id, requester=_USER,
                                        actor=_USER)
    assert job["cancel_requested"] is True
    assert job["state"] == "RUNNING"

    after = export_svc.advance_job(pg_database, job_id, rows_budget=2)
    assert after["state"] == "CANCELLED"

    with pg_database.session(_scope(pg_database)) as session:
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.read_result(session, job_id, requester=_USER)
    assert caught.value.code == "EXPORT_NOT_READY"
    assert caught.value.status == 409


@PG
def test_cancelling_a_queued_export_finishes_it_without_running_anything(
        pg_database):
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    with pg_database.session(_scope(pg_database)) as session:
        job = export_svc.request_cancel(session, job_id, requester=_USER,
                                        actor=_USER)
    assert job["state"] == "CANCELLED"
    assert job["rows_written"] == 0
    # A cancelled job is terminal: advancing it does nothing rather than
    # resurrecting it.
    assert export_svc.advance_job(pg_database, job_id)["state"] == "CANCELLED"


@PG
def test_a_retry_discards_every_committed_chunk(pg_database):
    """A retry that resumed from a partial file would splice rows read under
    one state of the data onto rows read under another, and the result would be
    self-consistent and wrong."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=2)
    export_svc.advance_job(pg_database, job_id, rows_budget=4)

    with pg_database.session(Scope.system()) as session:
        before = session.fetchone(
            "SELECT COUNT(*) FROM export_job_chunk WHERE export_job_id = %s",
            (job_id,))[0]
        assert before > 0
        # A retry is only offered on a FAILED job; put it there the way the
        # worker would, without touching a captured column.
        session.execute(
            "UPDATE export_job SET state = 'FAILED', finished_at = now(), "
            "error_code = 'TEST_INDUCED' WHERE export_job_id = %s", (job_id,))

    with pg_database.session(_scope(pg_database)) as session:
        job = export_svc.retry_job(session, job_id, requester=_USER,
                                   actor=_USER)
    assert job["state"] == "QUEUED"
    assert (job["rows_written"], job["chunks_written"], job["resume_key"]) \
        == (0, 0, None)
    assert job["rows_total"] is None

    with pg_database.session(Scope.system()) as session:
        assert session.fetchone(
            "SELECT COUNT(*) FROM export_job_chunk WHERE export_job_id = %s",
            (job_id,))[0] == 0

    _drain(pg_database, job_id)
    body, _meta = _result(pg_database, job_id)
    assert len(body.strip().splitlines()) == _CELLS + 1


@PG
def test_a_retry_does_not_recapture_the_scope(pg_database):
    """Re-capturing would silently re-authorise the job against grants that may
    since have WIDENED. The captured scope is immutable and stays exactly as it
    was."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    with pg_database.session(Scope.system()) as session:
        before = session.fetchone(
            "SELECT scope_json, scope_digest FROM export_job "
            "WHERE export_job_id = %s", (job_id,))
        session.execute(
            "UPDATE export_job SET state = 'FAILED', finished_at = now(), "
            "error_code = 'TEST_INDUCED' WHERE export_job_id = %s", (job_id,))
    with pg_database.session(_scope(pg_database)) as session:
        export_svc.retry_job(session, job_id, requester=_USER, actor=_USER)
    with pg_database.session(Scope.system()) as session:
        after = session.fetchone(
            "SELECT scope_json, scope_digest FROM export_job "
            "WHERE export_job_id = %s", (job_id,))
    assert before == after


@PG
def test_expiry_purges_the_bytes_and_keeps_the_record(pg_database):
    """Who exported what, under which scope and when, is the record that
    matters after the rendered rows are gone -- and 018 revokes DELETE on
    `export_job` so it cannot be otherwise."""
    from datetime import datetime, timedelta, timezone

    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    _drain(pg_database, job_id)

    later = datetime.now(timezone.utc) + timedelta(days=400)
    assert export_svc.expire_due(pg_database, now=later) == [job_id]

    with pg_database.session(Scope.system()) as session:
        state, requested_by, sha = session.fetchone(
            "SELECT state, requested_by, result_sha256 FROM export_job "
            "WHERE export_job_id = %s", (job_id,))
        chunks = session.fetchone(
            "SELECT COUNT(*) FROM export_job_chunk WHERE export_job_id = %s",
            (job_id,))[0]
    assert (state, requested_by, sha, chunks) == ("EXPIRED", _USER, None, 0)

    with pg_database.session(_scope(pg_database)) as session:
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.read_result(session, job_id, requester=_USER)
    # 410, not 409 and not 404: "the rows were purged" is a different fact from
    # "not ready yet" and from "no such export", and a caller acts differently
    # on each.
    assert caught.value.code == "EXPORT_EXPIRED"
    assert caught.value.status == 410


@PG
def test_the_result_is_not_served_if_its_bytes_no_longer_match_the_digest(
        pg_database):
    """018 makes chunks append-only, so a mismatch means something reached past
    `capex_app`. Handing over the bytes anyway would be handing over evidence
    of unknown provenance."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=100)
    _drain(pg_database, job_id, rows_budget=100)

    with pg_database.session(Scope.system()) as session:
        # Around the append-only trigger, the way a direct writer would.
        session.execute(
            "DELETE FROM export_job_chunk WHERE export_job_id = %s AND "
            "chunk_no = 1", (job_id,))
        session.execute(
            "INSERT INTO export_job_chunk (export_job_id, chunk_no, row_count, "
            "byte_count, sha256, body) VALUES (%s, 1, %s, 1, %s, %s)",
            (job_id, _CELLS, "0" * 64, "tampered\n"))

    with pg_database.session(_scope(pg_database)) as session:
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.read_result(session, job_id, requester=_USER)
    assert caught.value.code == "EXPORT_RESULT_DIGEST_MISMATCH"


@PG
def test_a_succeeded_job_cannot_carry_a_total_its_file_does_not_contain(
        pg_database):
    """The structural half of "never fabricate a total":
    `ck_export_job_succeeded_is_complete` refuses the row outright."""
    import psycopg

    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    _drain(pg_database, job_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_database.session(Scope.system()) as session:
            session.execute(
                "UPDATE export_job SET rows_total = rows_written + 1 "
                "WHERE export_job_id = %s", (job_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_database.session(Scope.system()) as session:
            session.execute(
                "UPDATE export_job SET result_sha256 = NULL "
                "WHERE export_job_id = %s", (job_id,))


@PG
def test_a_filter_narrows_the_export_within_the_scope(pg_database):
    """A `FilterSet` narrows WITHIN the caller's resolved scope. Here the scope
    is unrestricted, so the filter is the only thing acting -- which is what
    makes the count meaningful."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, chunk_rows=50,
                    filters={"wbs_paths": ["expm_003"]})
    _drain(pg_database, job_id, rows_budget=50)
    body, meta = _result(pg_database, job_id)
    assert meta["rows"] == 1
    assert "WBS-EXPM-003" in body
    assert "WBS-EXPM-004" not in body


@PG
def test_the_column_order_captured_at_creation_is_the_files_header(pg_database):
    """A registry change mid-flight must not reorder a half-written file, so
    the order is captured on the job and the header is rendered from THAT."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database)
    _drain(pg_database, job_id)
    body, _meta = _result(pg_database, job_id)

    header = next(csv.reader(io.StringIO(body)))
    assert tuple(header) == export_svc.BUDGET_LEDGER_CELLS.column_names
    with pg_database.session(Scope.system()) as session:
        stored = session.fetchone(
            "SELECT column_order FROM export_job WHERE export_job_id = %s",
            (job_id,))[0]
    assert list(stored) == header
