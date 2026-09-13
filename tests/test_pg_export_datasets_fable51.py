"""FABLE 5.1 / integrate/export-datasets: the nine registers added on top of
the four `app/backend/pg/exports.py` shipped with (`budget_ledger_cells`,
`wbs_elements`, `purchase_order_lines`, `internal_material_requests`).

Two halves, the same split `tests/test_pg_exports.py` uses:

  * SOURCE-LEVEL (no database): every dataset -- old and new alike, so a
    future dataset is covered the moment it is registered -- declares every
    `FILTER_FIELDS` entry the export contract recognises exactly once, across
    `filters` and `unsupported` and never both; and names a non-empty
    `key_sql`. `tests/test_pg_exports.py` already parametrises its two
    dataset-iterating contracts (`test_every_dataset_declares_a_non_empty_
    ordered_column_list`, `test_every_declared_filter_actually_compiles_to_
    sql`) over `export_svc.DATASETS`, so those two need no extension here --
    a dataset registered in that dict is exercised by them automatically.

  * LIVE (`@pytest.mark.pg`, SKIPPED without `CAPEX_DB_URL` -- a skip is not a
    pass, exactly as `tests/test_pg_exports.py`'s own marker says): one
    connected estate, seeded once per dataset with raw INSERTs against the
    real schema (a purchase request through to its request line, its
    fulfilment, its purchase order, its receipt, its bill, and beside them a
    capitalisation request, a budget revision, a budget transfer and an
    internal material movement -- plus a reconciliation exception), and each
    of the nine new datasets is driven through `plan_job` / `advance_job` /
    `read_result` to SUCCEEDED, with the CSV header checked against the
    dataset's own declared column names.

`audit_entries` is not among the nine: see the comment in
`app/backend/pg/exports.py` immediately above its `DATASETS` registry for why
it is not a dataset at all, and there is accordingly nothing to test here.
"""
from __future__ import annotations

import os
import sys as _sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend.pg import exports as export_svc  # noqa: E402
from app.backend.pg import principal_scope  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS."),
)

#: The nine datasets this file adds. `audit_entries` is deliberately absent --
#: see the module docstring.
NEW_DATASETS: tuple[str, ...] = (
    "purchase_requests", "purchase_request_lines", "goods_receipt_lines",
    "vendor_bill_lines", "reconciliation_exceptions", "capitalisation_requests",
    "budget_revisions", "budget_transfers", "internal_material_movements",
)


# =========================================================================
# SOURCE-LEVEL: every dataset declares every applicable FILTER_FIELDS entry
# =========================================================================
#: The fields a dataset is actually asked to take a position on. The five in
#: `FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT` are refused by `plan_filters`
#: for EVERY dataset before it ever looks at `dataset.filters` /
#: `dataset.unsupported` (see that function), so no dataset declares them --
#: exactly as none of the four shipped datasets do today.
_APPLICABLE_FIELDS = (
    frozenset(export_svc.FILTER_FIELDS)
    - export_svc.FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT
)


@pytest.mark.parametrize("name", sorted(export_svc.DATASETS))
def test_every_dataset_declares_every_applicable_filter_field_exactly_once(name):
    """Declared in `filters` (it builds SQL) XOR `unsupported` (it is refused
    by name, with a reason) -- never in neither, which `plan_filters` would
    answer with the generic 'declares no column ... and no reason' 500, and
    never in both, which is a dataset definition that cannot decide what it
    means."""
    dataset = export_svc.DATASETS[name]
    declared_filters = frozenset(dataset.filters)
    declared_unsupported = frozenset(dataset.unsupported)

    both = declared_filters & declared_unsupported
    assert not both, (
        f"{name} declares {sorted(both)} in BOTH filters and unsupported")

    missing = _APPLICABLE_FIELDS - declared_filters - declared_unsupported
    assert not missing, (
        f"{name} declares neither a filter nor a refusal for {sorted(missing)}")

    extra_filters = declared_filters - _APPLICABLE_FIELDS
    assert not extra_filters, (
        f"{name}.filters names {sorted(extra_filters)}, which is not an "
        f"applicable FilterSet field")
    extra_unsupported = declared_unsupported - _APPLICABLE_FIELDS
    assert not extra_unsupported, (
        f"{name}.unsupported names {sorted(extra_unsupported)}, which is not "
        f"an applicable FilterSet field")


@pytest.mark.parametrize("name", sorted(export_svc.DATASETS))
def test_every_dataset_names_a_non_empty_key_sql(name):
    """A chunked export's resume cursor IS `key_sql` -- an empty one cannot
    be a sort key, let alone a unique one, so a dataset with none could never
    actually page."""
    dataset = export_svc.DATASETS[name]
    assert dataset.key_sql, f"{name} names no key_sql"
    assert all(isinstance(expr, str) and expr.strip() for expr in dataset.key_sql), (
        f"{name}.key_sql carries a blank expression: {dataset.key_sql!r}")


@pytest.mark.parametrize("name", NEW_DATASETS)
def test_the_nine_new_datasets_are_registered(name):
    assert name in export_svc.DATASETS, (
        f"{name} is not registered in export_svc.DATASETS")


def test_audit_entries_is_not_a_dataset():
    """Deliberately -- see the comment above `DATASETS` in exports.py: a
    waiver of all four scope dimensions on `audit_log` would let any
    `export.create`-permitted principal export the whole audit trail with no
    `audit.read` check anywhere on that path."""
    assert "audit_entries" not in export_svc.DATASETS


# =========================================================================
# LIVE: plan_job -> advance_job -> read_result, SUCCEEDED, header matches
# =========================================================================
def _seed(connection, *, suffix: str) -> dict:
    """One small, CONNECTED estate: a purchase request that becomes a
    purchase order, a receipt and a bill against the same (wbs, budget head)
    cell; beside it, a capitalisation request, a budget revision, a budget
    transfer between two cells, an internal material request with two
    movements, and a reconciliation exception raised against the receipt
    line. Every one of the nine new datasets finds at least one row here
    under a single `{"project_ids": [ids["project"]]}` filter.

    Deliberately not wired through the application services
    (`procurement.py`, `budget.py`, ...): this is a fixture for the EXPORT
    layer, which reads tables directly, and the point is to exercise the
    real schema's constraints -- not to re-test the services that normally
    populate it.
    """
    ids = {
        "org": f"O_{suffix}", "entity": f"E_{suffix}", "plant": f"PL_{suffix}",
        "loc1": f"L1_{suffix}", "loc2": f"L2_{suffix}", "project": f"PJ_{suffix}",
        "head": f"BH_{suffix}", "wbs1": f"W1_{suffix}", "wbs2": f"W2_{suffix}",
        "pr": f"PR_{suffix}", "pr_line": f"PRL_{suffix}",
        "po": f"PO_{suffix}", "po_line": f"POL_{suffix}",
        "grn": f"GRN_{suffix}", "grn_line": f"GRNL_{suffix}",
        "bill": f"BILL_{suffix}", "bill_line": f"BILLL_{suffix}",
        "cap": f"CAP_{suffix}", "revision": f"REV_{suffix}",
        "transfer": f"XFR_{suffix}", "imr": f"IMR_{suffix}",
        "mov1": f"MOV1_{suffix}", "mov2": f"MOV2_{suffix}",
        "exception": f"EXC_{suffix}",
    }
    ex = connection.execute
    now = datetime.now(timezone.utc)
    today = date.today()

    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s, %s, 'Org', 'T', 'T')",
       (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s, %s, %s, 'Entity', 'T', 'T')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, "
       "updated_by) VALUES (%s, %s, %s, 'Plant', 'T', 'T')",
       (ids["plant"], ids["entity"], f"PLC_{suffix}"))
    ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
       "created_by, updated_by) VALUES (%s, %s, %s, %s, 'From store', 'T', 'T')",
       (ids["loc1"], ids["entity"], ids["plant"], f"LC1_{suffix}"))
    ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
       "created_by, updated_by) VALUES (%s, %s, %s, %s, 'To store', 'T', 'T')",
       (ids["loc2"], ids["entity"], ids["plant"], f"LC2_{suffix}"))
    ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, "
       "capex_code, name, status, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, 'Project', 'Released', 'T', 'T')",
       (ids["project"], ids["entity"], ids["plant"], ids["loc1"], f"CX_{suffix}"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES (%s, %s, %s, 'Head', 'T', 'T')",
       (ids["head"], ids["entity"], f"HC_{suffix}"))

    for wbs, parent in ((ids["wbs1"], None), (ids["wbs2"], None)):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, "
           "description, wbs_path, status, created_by, updated_by) "
           "VALUES (%s, %s, %s, 'element', %s, 'Released', 'T', 'T')",
           (wbs, ids["project"], wbs, wbs))
    for wbs, budget in ((ids["wbs1"], 1_000_00), (ids["wbs2"], 500_00)):
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_paise, updated_by) VALUES (%s, %s, %s, 'T')",
           (wbs, ids["head"], budget))

    # ---------------------------------------------------- purchase request
    ex("INSERT INTO purchase_request (pr_id, pr_number, project_id, "
       "requested_by, status, check_result, approver, approved_at, "
       "amount_paise, created_by, updated_by) "
       "VALUES (%s, %s, %s, 'U-MAKER', 'Approved', 'WITHIN_BUDGET', "
       "'U-CHECKER', %s, %s, 'T', 'T')",
       (ids["pr"], ids["pr"], ids["project"], now, 20000))
    ex("INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id, wbs_id, "
       "budget_head_id, description, quantity, amount_paise, created_by, "
       "updated_by) "
       "VALUES (%s, %s, 1, %s, %s, %s, 'line', 2, 20000, 'T', 'T')",
       (ids["pr_line"], ids["pr"], ids["project"], ids["wbs1"], ids["head"]))
    ex("INSERT INTO pr_line_fulfilment (pr_line_id, pr_id, project_id, mode, "
       "line_quantity, external_quantity, internal_quantity, "
       "line_amount_paise, external_amount_paise, internal_amount_paise, "
       "decided_by, created_by, updated_by) "
       "VALUES (%s, %s, %s, 'EXTERNAL_PURCHASE', 2, 2, 0, 20000, 20000, 0, "
       "'U-CHECKER', 'T', 'T')",
       (ids["pr_line"], ids["pr"], ids["project"]))

    # ------------------------------------------------------ purchase order
    ex("INSERT INTO purchase_order (po_id, po_number, pr_id, project_id, "
       "vendor_name, currency, status, ordered_at, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, 'Vendor A', 'INR', 'Approved', %s, 'T', 'T')",
       (ids["po"], ids["po"], ids["pr"], ids["project"], now))
    ex("INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id, "
       "budget_head_id, description, quantity, rate_paise, amount_paise, "
       "created_by, updated_by) "
       "VALUES (%s, %s, 1, %s, %s, %s, 'line', 2, 10000, 20000, 'T', 'T')",
       (ids["po_line"], ids["po"], ids["project"], ids["wbs1"], ids["head"]))

    # -------------------------------------------------------------- receipt
    # `entity_id` is NOT NULL (014_procurement_corrections.sql backfills it
    # from the project and then sets it NOT NULL) even though 013's own
    # CREATE TABLE never mentions it -- it did not exist yet at 013.
    ex("INSERT INTO grn (grn_id, grn_number, po_id, entity_id, received_at, "
       "status, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, 'Approved', 'T', 'T')",
       (ids["grn"], ids["grn"], ids["po"], ids["entity"], now))
    ex("INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id, "
       "quantity, amount_paise, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, 2, 20000, 'T', 'T')",
       (ids["grn_line"], ids["grn"], ids["po"], ids["po_line"]))

    # ---------------------------------------------------------------- bill
    # `entity_id` is NOT NULL for the same reason as `grn.entity_id` above.
    ex("INSERT INTO bill (bill_id, bill_number, po_id, project_id, "
       "entity_id, vendor_name, bill_date, status, accounting_status, "
       "doc_type, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, 'Vendor A', %s, 'Approved', 'Approved', "
       "'BILL', 'T', 'T')",
       (ids["bill"], ids["bill"], ids["po"], ids["project"], ids["entity"], today))
    ex("INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id, "
       "wbs_id, budget_head_id, quantity, amount_paise, grn_line_id, "
       "created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, %s, 2, 20000, %s, 'T', 'T')",
       (ids["bill_line"], ids["bill"], ids["po"], ids["po_line"], ids["wbs1"],
        ids["head"], ids["grn_line"]))

    # --------------------------------------------------- capitalisation
    ex("INSERT INTO capitalisation_request (cap_id, cap_number, project_id, "
       "status, cwip_balance_paise, requested_by, created_by, updated_by) "
       "VALUES (%s, %s, %s, 'Draft', 20000, 'U-MAKER', 'T', 'T')",
       (ids["cap"], ids["cap"], ids["project"]))

    # ------------------------------------------------- revision / transfer
    ex("INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, "
       "delta_paise, effective_from, justification, status, created_by) "
       "VALUES (%s, %s, %s, 5000, %s, 'test revision', 'DRAFT', 'U-MAKER')",
       (ids["revision"], ids["wbs1"], ids["head"], today))
    ex("INSERT INTO budget_transfer (transfer_id, from_wbs_id, from_head_id, "
       "to_wbs_id, to_head_id, amount_paise, effective_from, justification, "
       "status, created_by) "
       "VALUES (%s, %s, %s, %s, %s, 1000, %s, 'test transfer', 'DRAFT', "
       "'U-MAKER')",
       (ids["transfer"], ids["wbs1"], ids["head"], ids["wbs2"], ids["head"], today))

    # ------------------------------------------------ internal fulfilment
    ex("INSERT INTO internal_material_request (imr_id, imr_number, pr_id, "
       "pr_line_id, project_id, wbs_id, budget_head_id, item_external_id, "
       "item_description, from_location_id, to_location_id, "
       "requested_quantity, approved_quantity, allocated_quantity, "
       "issued_quantity, unit_rate_paise, valuation_source, status, "
       "requested_by, approved_by, approved_at, created_by, updated_by) "
       "VALUES (%s, %s, %s, %s, %s, %s, %s, 'ITEM-1', 'Test item', %s, %s, "
       "5, 5, 2, 1, 500, 'MANUAL', 'PARTIALLY_ISSUED', 'U-MAKER', "
       "'U-CHECKER', %s, 'T', 'T')",
       (ids["imr"], ids["imr"], ids["pr"], ids["pr_line"], ids["project"],
        ids["wbs1"], ids["head"], ids["loc1"], ids["loc2"], now))
    ex("INSERT INTO internal_material_movement (movement_id, imr_id, "
       "project_id, wbs_id, budget_head_id, kind, quantity, amount_paise, "
       "from_location_id, to_location_id, idempotency_key, occurred_at, "
       "created_by) "
       "VALUES (%s, %s, %s, %s, %s, 'ALLOCATE', 2, 1000, %s, NULL, 'k1', "
       "%s, 'T')",
       (ids["mov1"], ids["imr"], ids["project"], ids["wbs1"], ids["head"],
        ids["loc1"], now))
    ex("INSERT INTO internal_material_movement (movement_id, imr_id, "
       "project_id, wbs_id, budget_head_id, kind, quantity, amount_paise, "
       "from_location_id, to_location_id, idempotency_key, occurred_at, "
       "created_by) "
       "VALUES (%s, %s, %s, %s, %s, 'ISSUE', 1, 500, %s, %s, 'k2', %s, 'T')",
       (ids["mov2"], ids["imr"], ids["project"], ids["wbs1"], ids["head"],
        ids["loc1"], ids["loc2"], now))

    # ------------------------------------------------- reconciliation
    ex("INSERT INTO reconciliation_exception (exception_id, kind, "
       "object_type, object_id, entity_id, project_id, status, detail, "
       "local_paise, source_paise, raised_at) "
       "VALUES (%s, 'GRN_LINE_UNATTRIBUTED', 'grn_line', %s, %s, %s, 'Open', "
       "'unattributed on export test', 100, 200, %s)",
       (ids["exception"], ids["grn_line"], ids["entity"], ids["project"], now))

    connection.commit()
    return ids


def _requester(pg_connection, ids: dict) -> str:
    """An unrestricted requester -- `read_all=true` -- exactly as
    `tests/test_pg_exports_xlsx_fable51.py::_requester` builds one, so the
    export sees every row `_seed` wrote regardless of which entity/plant/
    location/project it landed under."""
    user = f"U-EXPFAB-{ids['project']}"
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, "
        "updated_by) VALUES (%s, %s, %s, 'T', 'T')",
        (user, f"{user}@example.test", user))
    pg_connection.execute(
        "INSERT INTO user_access_flag (user_id, read_all, updated_by) "
        "VALUES (%s, true, 'T')", (user,))
    pg_connection.commit()
    return user


def _run_dataset(pg_database, scope: Scope, user: str, dataset: str,
                 filters: dict) -> tuple[str, dict]:
    with pg_database.session(scope) as session:
        job_id = export_svc.create_job(
            session, dataset=dataset, filters=filters, scope=scope,
            requested_by=user, chunk_rows=5, output_format="csv"
        )["export_job_id"]
    for _ in range(30):
        outcome = export_svc.advance_job(pg_database, job_id, rows_budget=5)
        assert outcome["state"] != export_svc.STATE_FAILED, outcome
        if not outcome.get("more"):
            break
    else:
        raise AssertionError(f"{dataset} did not finish in 30 invocations")
    with pg_database.session(scope) as session:
        body, meta = export_svc.read_result(session, job_id, requester=user)
    return body, meta


@pytest.mark.pg
@PG
@pytest.mark.parametrize("dataset_name", NEW_DATASETS)
def test_new_dataset_runs_to_succeeded_with_the_declared_csv_header(
        pg_database, pg_connection, dataset_name):
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    user = _requester(pg_connection, ids)
    scope = principal_scope.scope_for_request(pg_database, {"user_id": user})

    body, meta = _run_dataset(pg_database, scope, user, dataset_name,
                              {"project_ids": [ids["project"]]})

    assert meta["bytes"] and meta["sha256"]
    lines = body.splitlines()
    assert lines, f"{dataset_name} produced an empty file"
    header = lines[0].split(",")
    dataset = export_svc.DATASETS[dataset_name]
    assert header == list(dataset.column_names), (
        f"{dataset_name}'s CSV header does not match its declared columns:\n"
        f"  file:    {header}\n  dataset: {list(dataset.column_names)}")
    assert len(lines) - 1 >= 1, f"{dataset_name} exported zero data rows"


@pytest.mark.pg
@PG
def test_reconciliation_exceptions_is_scoped_through_entity_and_project_only(
        pg_database, pg_connection):
    """The one new dataset whose `scope_columns` waives two dimensions --
    proof the waiver is real (a plant/location-only-restricted view still
    sees the row) and not merely declared."""
    ids = _seed(pg_connection, suffix=uuid.uuid4().hex[:10])
    user = _requester(pg_connection, ids)
    scope = principal_scope.scope_for_request(pg_database, {"user_id": user})

    body, _meta = _run_dataset(pg_database, scope, user,
                               "reconciliation_exceptions",
                               {"entity_ids": [ids["entity"]]})
    lines = body.splitlines()
    assert len(lines) - 1 == 1, "the entity filter alone must find the row"
    header = lines[0].split(",")
    row = dict(zip(header, lines[1].split(",")))
    assert row["exception_id"] == ids["exception"]
    assert row["project_id"] == ids["project"]
