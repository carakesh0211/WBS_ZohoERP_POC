"""Fable 5.1: `budget_category` (migration 026) as a real reporting dimension,
EXECUTED against a live PostgreSQL server.

WHY THIS IS A SEPARATE FILE, NOT AN ADDITION TO `test_pg_reporting.py`
========================================================================
`test_pg_reporting.py` is owned by another agent's wave and is not touched
here (file-ownership boundary in the Fable 5.1 brief). Its fixture style --
one hand-built estate, every expected figure computed in a comment and
asserted as a literal, `pg_database`/`estate` from `conftest_pg.py` -- is
copied rather than imported, because the two files must stand alone.

WHAT THIS FILE PROVES
======================
Migration 026 (`migrations/pg/026_budget_category_and_original_budget.sql`)
gave `budget_control_cell` a real `budget_category_id`, SEPARATE from
`budget_head_id` by product-owner decision (AMB-04, `docs/
FULL_APPLICATION_DELIVERY_STATUS.md`). "Reporting must read the CELL's
category (NULL renders 'Unclassified')" and "budget_category and budget_head
must be independently selectable and composable" are the two claims a live
server is needed to check: independence is a fact about which ROWS a real
query returns, not about the shape of a Python object.

A SKIP IS NOT A PASS
=====================
Every test below is gated on `CAPEX_DB_URL`, exactly as its sibling file
gates -- see `PG` below and
`test_this_file_is_gated_and_says_a_skip_is_not_a_pass`.
"""
from __future__ import annotations

import os
import sys as _sys
import uuid
from datetime import date, timedelta
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend.pg import exports as export_svc  # noqa: E402
from app.backend.pg import reporting as rp  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- category/head "
            "independence, the NULL-renders-Unclassified rule and the "
            "grouped/drill-down/export reconciliation are facts about rows a "
            "real planner produces, not decidable from source."),
)

TODAY = date.today()
EARLIER = TODAY - timedelta(days=30)

# ===========================================================================
# The estate
# ===========================================================================
#
# ONE project, one plant, one location, FIVE cells under it -- every figure
# below is computed in this comment and asserted as a literal.
#
#   head_1 / category_x   budget   100,000   ("W1")
#   head_2 / category_x   budget   200,000   ("W2")
#   head_1 / category_y   budget   300,000   ("W3")
#   head_2 / category_y   budget   400,000   ("W4")
#   head_1 / UNCLASSIFIED budget   500,000   ("W5") -- budget_category_id NULL,
#            the pre-026 / migrated shape.
#
# CATEGORY X spans TWO heads (head_1, head_2) -- {W1, W2}.
# HEAD 1 spans THREE categories (X, Y, and NULL) -- {W1, W3, W5}.
# Neither set is a subset of the other: that is "the two row sets differ",
# proved by construction rather than assumed.
#
# W1 additionally carries a full procurement chain, so the OTHER nine
# components are not all zero and the reconciliation tests below exercise more
# than the `budget` bucket alone:
#
#   PR_1        one line resolving to W1/head_1, one reservation held ...  5,000
#   PO_1        one line, ordered ......................................  20,000
#   GRN_1       one line received .......................................  8,000
#   BILL_1      one line, Approved ......................................  8,000
#               commitment = max(0, 20,000 - 8,000) ......................12,000
#               received_not_billed = max(0, 8,000 - 8,000) ...............   0
#
# CARD TOTALS FOR THE WHOLE PROJECT:
#   budget    100,000+200,000+300,000+400,000+500,000 = 1,500,000
#   original  = budget (every line is kind=ORIGINAL, Approved)
#   revisions = 0
#   ordered              = 20,000
#   commitment           = 12,000
#   actual               =  8,000
#   received             =  8,000
#   received_not_billed  =      0
#   pr_reserved          =  5,000

CELLS = (
    # (key, head, category, budget_paise)
    ("w1", "head_1", "cat_x", 100_000),
    ("w2", "head_2", "cat_x", 200_000),
    ("w3", "head_1", "cat_y", 300_000),
    ("w4", "head_2", "cat_y", 400_000),
    ("w5", "head_1", None,    500_000),
)

CARD_TOTALS = {
    "budget": 1_500_000, "original": 1_500_000, "revisions": 0,
    "ordered": 20_000, "commitment": 12_000, "actual": 8_000,
    "received": 8_000, "received_not_billed": 0, "pr_reserved": 5_000,
}


def _seed(connection, *, suffix: str) -> dict[str, str]:
    ids = {
        "entity": f"E_{suffix}", "org": f"O_{suffix}",
        "plant": f"PL_{suffix}", "loc": f"LOC_{suffix}",
        "project": f"PRJ_{suffix}",
        "head_1": f"H1_{suffix}", "head_2": f"H2_{suffix}",
        "cat_x": f"CX_{suffix}", "cat_y": f"CY_{suffix}",
        "period": f"PER_{suffix}",
        "pr": f"PR_{suffix}", "po": f"PO_{suffix}",
        "pol1": f"POL1_{suffix}",
    }
    for key, *_rest in CELLS:
        ids[key] = f"{key.upper()}_{suffix}"
    ex = connection.execute

    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,%s,'Plant','t','t')",
       (ids["plant"], ids["entity"], f"PC_{suffix}"))
    ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'Location','t','t')",
       (ids["loc"], ids["entity"], ids["plant"], f"LC_{suffix}"))
    ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, "
       "capex_code, name, status, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,%s,'Project','Released','t','t')",
       (ids["project"], ids["entity"], ids["plant"], ids["loc"], f"CC_{suffix}"))
    for head in ("head_1", "head_2"):
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
           "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
           (ids[head], ids["entity"], f"{head.upper()}_{suffix}", f"Head {head}"))
    # THE REAL CATEGORY MASTER (026) -- a SEPARATE table from budget_head.
    # `ck_budget_category_code_shape` admits only `[A-Z0-9_-]`, so the code
    # is upper-cased -- the hex suffix's `a`-`f` survive as `A`-`F`.
    for cat in ("cat_x", "cat_y"):
        ex("INSERT INTO budget_category (category_id, code, name, "
           "created_by, updated_by) VALUES (%s,%s,%s,'t','t')",
           (ids[cat], f"{cat.upper()}_{suffix}".upper(), f"Category {cat}"))
    ex("INSERT INTO accounting_period (period_id, entity_id, period_start, "
       "period_end, state, created_by) VALUES (%s,%s,%s,%s,'OPEN','t')",
       (ids["period"], ids["entity"], EARLIER - timedelta(days=60),
        TODAY + timedelta(days=60)))

    for key, head, category, budget in CELLS:
        wbs = ids[key]
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, "
           "description, wbs_path, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,'cell',%s,'Released','t','t')",
           (wbs, ids["project"], wbs, wbs))
        # THE CELL. `budget_category_id` is set at INSERT here to model "set
        # once on release" (026's own immutability trigger only guards
        # UPDATE, so an INSERT establishing the value directly is the correct
        # way to seed a released cell without re-running the release flow).
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_category_id, budget_paise, updated_by) "
           "VALUES (%s,%s,%s,%s,'t')",
           (wbs, ids[head], ids[category] if category else None, budget))
        ex("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, "
           "budget_category_id, kind, amount_paise, effective_from, status, "
           "created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'ORIGINAL',%s,%s,'Approved','t','t')",
           (f"BL_{key}_{suffix}", wbs, ids[head],
            ids[category] if category else None, budget, EARLIER))

    # ------------------------------------------------- the W1 procurement chain
    w1 = ids["w1"]
    ex("INSERT INTO purchase_request (pr_id, pr_number, project_id, "
       "requested_by, requested_at, status, reserves_budget, created_by, "
       "updated_by) VALUES (%s,%s,%s,'U-REQ',%s,'Approved',true,'t','t')",
       (ids["pr"], f"PRN_{suffix}", ids["project"], EARLIER))
    ex("INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id, wbs_id, "
       "budget_head_id, quantity, amount_paise, created_by, updated_by) "
       "VALUES (%s,%s,1,%s,%s,%s,1,%s,'t','t')",
       (f"PRL_{suffix}", ids["pr"], ids["project"], w1, ids["head_1"], 5_000))
    ex("INSERT INTO pr_reservation (reservation_id, pr_id, wbs_id, "
       "budget_head_id, project_id, amount_paise, state, created_by, "
       "updated_by) VALUES (%s,%s,%s,%s,%s,5000,'Reserved','t','t')",
       (f"RES_{suffix}", ids["pr"], w1, ids["head_1"], ids["project"]))

    ex("INSERT INTO purchase_order (po_id, po_number, pr_id, project_id, "
       "vendor_name, status, ordered_at, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'Vendor','Released',%s,'t','t')",
       (ids["po"], f"PON_{suffix}", ids["pr"], ids["project"], EARLIER))
    ex("INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id, "
       "budget_head_id, quantity, rate_paise, amount_paise, tax_paise, "
       "non_creditable_tax_paise, freight_paise, created_by, updated_by) "
       "VALUES (%s,%s,1,%s,%s,%s,1,20000,20000,0,0,0,'t','t')",
       (ids["pol1"], ids["po"], ids["project"], w1, ids["head_1"]))

    ex("INSERT INTO grn (grn_id, grn_number, po_id, entity_id, received_at, "
       "status, is_reversal, created_by, updated_by) "
       "SELECT %s,%s,po.po_id,p.entity_id,%s,'Approved',false,'t','t' "
       "FROM purchase_order po JOIN project p ON p.project_id = po.project_id "
       "WHERE po.po_id = %s",
       (f"GRN_{suffix}", f"GRNN_{suffix}", EARLIER, ids["po"]))
    ex("INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id, "
       "quantity, amount_paise, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,1,8000,'t','t')",
       (f"GRNL_{suffix}", f"GRN_{suffix}", ids["po"], ids["pol1"]))

    ex("INSERT INTO bill (bill_id, bill_number, po_id, project_id, "
       "entity_id, vendor_name, bill_date, status, accounting_status, "
       "created_by, updated_by) "
       "SELECT %s,%s,%s,p.project_id,p.entity_id,'Vendor',%s,'Approved',"
       "'Approved','t','t' FROM project p WHERE p.project_id = %s",
       (f"BILL_{suffix}", f"BILLN_{suffix}", ids["po"], EARLIER, ids["project"]))
    ex("INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id, "
       "wbs_id, budget_head_id, quantity, amount_paise, "
       "non_creditable_tax_paise, freight_paise, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,%s,%s,1,8000,0,0,'t','t')",
       (f"BILLL_{suffix}", f"BILL_{suffix}", ids["po"], ids["pol1"], w1,
        ids["head_1"]))

    connection.commit()
    return ids


@pytest.fixture()
def estate(pg_connection):
    return _seed(pg_connection, suffix=uuid.uuid4().hex[:10])


def _scope(ids: dict[str, str], *, user_id: str = "U-REPORT") -> Scope:
    return Scope(
        user_id=user_id, principal_kind="USER",
        entity_ids=frozenset({ids["entity"]}),
        plant_ids=None, project_ids=None, location_ids=None, read_all=False,
    )


def _project_filters(ids: dict[str, str], **overrides) -> rp.FilterSet:
    return rp.FilterSet.build(project_ids=[ids["project"]], **overrides)


# ===========================================================================
# The gate on the gate
# ===========================================================================

def test_this_file_is_gated_and_says_a_skip_is_not_a_pass():
    reason = PG.kwargs["reason"]
    assert "SKIP, NOT A PASS" in reason
    assert "CAPEX_DB_URL" in reason


# ===========================================================================
# 1. Category and head are independent and composable
# ===========================================================================

@pytest.mark.pg
@PG
def test_category_and_head_are_independent_and_the_two_row_sets_differ(
        pg_database, estate):
    """The exact claim REQ-RPT-009/010 asks for a live proof of.

    Filtering to category_x returns rows spanning head_1 AND head_2 -- several
    heads under one category. Filtering to head_1 returns rows spanning
    category_x, category_y AND "no category" -- several categories under one
    head. The two row sets are neither equal nor one a subset of the other.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        by_category = rp.aggregate(
            session, _project_filters(
                ids, budget_category_ids=[ids["cat_x"]],
                group_by=["wbs", "budget_head", "budget_category"]))
        by_head = rp.aggregate(
            session, _project_filters(
                ids, budget_head_ids=[ids["head_1"]],
                group_by=["wbs", "budget_head", "budget_category"]))

    assert by_category["state"] == "ok"
    assert by_head["state"] == "ok"

    category_wbs = {row["key"]["wbs"] for row in by_category["rows"]}
    head_wbs = {row["key"]["wbs"] for row in by_head["rows"]}

    assert category_wbs == {ids["w1"], ids["w2"]}, (
        "category_x must span BOTH head_1 (w1) and head_2 (w2)")
    assert head_wbs == {ids["w1"], ids["w3"], ids["w5"]}, (
        "head_1 must span category_x (w1), category_y (w3) and Unclassified (w5)")
    assert category_wbs != head_wbs
    assert not category_wbs <= head_wbs and not head_wbs <= category_wbs, (
        "neither set may be a subset of the other -- otherwise the two "
        "filters would not be proven independent, only differently sized")

    heads_under_category_x = {row["labels"]["budget_head"] for row in by_category["rows"]}
    assert len(heads_under_category_x) == 2, "category_x must show 2 distinct heads"
    categories_under_head_1 = {row["labels"]["budget_category"] for row in by_head["rows"]}
    assert len(categories_under_head_1) == 3, (
        "head_1 must show 3 distinct category labels (X, Y, Unclassified)")


@pytest.mark.pg
@PG
def test_budget_category_and_budget_head_compose_in_one_group_by(
        pg_database, estate):
    """Both dimensions in the SAME `group_by` -- proving they are not aliased
    (unlike `category`, which IS aliased to `budget_head` and refused
    together with it)."""
    ids = estate
    filters = _project_filters(
        ids, group_by=["budget_head", "budget_category"], sort="budget",
        sort_desc=True)
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, filters)
    assert result["state"] == "ok"
    # Five cells, five distinct (head, category) pairs -- none collapsed.
    pairs = {(r["key"]["budget_head"], r["key"]["budget_category"])
             for r in result["rows"]}
    assert len(pairs) == 5
    assert sum(r["budget"] for r in result["rows"]) == CARD_TOTALS["budget"]


@pytest.mark.pg
@PG
def test_category_and_budget_head_still_refuse_to_be_grouped_together(
        pg_database, estate):
    """The PRE-026 alias (`category` == `budget_head`) is untouched by the
    new, real `budget_category` axis -- this refusal must still fire."""
    ids = estate
    with pytest.raises(rp.FilterError) as exc:
        _project_filters(ids, group_by=["category", "budget_head"])
    assert exc.value.code == "ALIASED_DIMENSION"


# ===========================================================================
# 2. NULL renders "Unclassified"
# ===========================================================================

@pytest.mark.pg
@PG
def test_a_cell_with_no_category_renders_unclassified(pg_database, estate):
    ids = estate
    filters = _project_filters(
        ids, budget_head_ids=[ids["head_1"]], group_by=["budget_category"])
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, filters)
    assert result["state"] == "ok"
    unclassified_rows = [r for r in result["rows"] if r["key"]["budget_category"] is None]
    assert len(unclassified_rows) == 1
    [row] = unclassified_rows
    assert row["labels"]["budget_category"] == "Unclassified"
    assert row["budget"] == 500_000, "w5's budget, and w5 alone, is Unclassified"


# ===========================================================================
# 3. Reconciliation: grouped rows and drill-down rows sum back to the card
# ===========================================================================

@pytest.mark.pg
@PG
def test_grouped_rows_reconcile_to_the_card_across_every_component(
        pg_database, estate):
    """Project + Plant + Location + Category + Head, filtered by period --
    the exact combination the brief names. TWO SEPARATE STATEMENTS: the
    ungrouped card and the fine-grained grouping, and their totals are
    compared -- not one query's internal consistency, but two queries
    agreeing.
    """
    ids = estate
    card_filters = _project_filters(ids, period_ids=[ids["period"]])
    grouped_filters = _project_filters(
        ids, period_ids=[ids["period"]],
        group_by=["project", "plant", "location", "budget_category", "budget_head"])

    with pg_database.session(_scope(ids)) as session:
        card = rp.aggregate(session, card_filters)
        grouped = rp.aggregate(session, grouped_filters)

    assert card["state"] == "ok" and grouped["state"] == "ok"
    for component, expected in CARD_TOTALS.items():
        assert card["totals"][component] == expected, component
        assert card["totals"][component] == sum(
            row[component] for row in grouped["rows"]), (
            f"{component}: grouped rows do not sum back to the card total")
    # Five cells, five distinct (project, plant, location, category, head)
    # combinations -- one project and one plant/location for all of them, so
    # the only real variation is category x head.
    assert len(grouped["rows"]) == 5


@pytest.mark.pg
@PG
def test_drill_down_into_budget_category_sums_back_and_cannot_widen(
        pg_database, estate):
    ids = estate
    filters = _project_filters(ids)
    with pg_database.session(_scope(ids)) as session:
        drill = rp.drill_down(session, filters, "budget_category", ids["cat_x"])
        # Drilling from a report ALREADY narrowed to category_x into
        # category_y must return nothing: category_y was never in the
        # population category_x's total was computed over.
        narrowed = rp.FilterSet.build(
            project_ids=[ids["project"]], budget_category_ids=[ids["cat_x"]])
        cannot_widen = rp.drill_down(session, narrowed, "budget_category", ids["cat_y"])

    assert drill["state"] == "ok"
    assert drill["drill_of"] == {"dimension": "budget_category", "key": ids["cat_x"]}
    assert sum(row["budget"] for row in drill["rows"]) == 300_000  # w1 + w2

    assert cannot_widen["rows"] == []
    assert cannot_widen["state"] == "empty"
    assert cannot_widen["totals"]["budget"] == 0


# ===========================================================================
# 4. Export: category filter, category AND head columns, total reconciles
# ===========================================================================

def _run_export_to_completion(database, job_id: str, *, limit: int = 20) -> dict:
    outcome: dict = {}
    for _ in range(limit):
        outcome = export_svc.advance_job(database, job_id, rows_budget=1000)
        if not outcome.get("more"):
            return outcome
    raise AssertionError(f"export {job_id} did not finish in {limit} invocations")


@pytest.mark.pg
@PG
def test_an_export_under_a_category_filter_contains_only_that_categorys_rows(
        pg_database, pg_connection, estate):
    """DELIVER 4: the export captures the FilterSet including budget_category,
    the CSV carries category AND head columns, and its total reconciles to
    the card total for the SAME filter."""
    ids = estate
    scope = _scope(ids, user_id="U-EXPORT")
    filters = rp.FilterSet.build(
        project_ids=[ids["project"]], budget_category_ids=[ids["cat_x"]])

    # `advance_job` re-resolves the requester's CURRENT grants (the "meet"
    # step -- see exports.py's own module docstring) via
    # `principal_scope.scope_for_request`, which needs a real `app_user` row
    # and an unrestricted `user_access_flag`. The Scope passed to `create_job`
    # only captures the requester's identity at QUEUE time; the row still has
    # to exist for the job to be able to RUN.
    pg_connection.execute(
        "INSERT INTO app_user (user_id, email, display_name, created_by, "
        "updated_by) VALUES ('U-EXPORT', 'u-export@example.test', "
        "'U-EXPORT', 't', 't')")
    pg_connection.execute(
        "INSERT INTO user_access_flag (user_id, read_all, updated_by) "
        "VALUES ('U-EXPORT', true, 't')")
    pg_connection.commit()

    with pg_database.session(scope) as session:
        job = export_svc.create_job(
            session, dataset="budget_ledger_cells", filters=filters,
            scope=scope, requested_by="U-EXPORT", chunk_rows=10)
    job_id = job["export_job_id"]
    outcome = _run_export_to_completion(pg_database, job_id)
    assert outcome["state"] == "SUCCEEDED"

    with pg_database.session(scope) as session:
        body, _meta = export_svc.read_result(session, job_id, requester="U-EXPORT")

    lines = body.strip("\n").splitlines()
    header = lines[0].split(",")
    assert "budget_category_id" in header
    assert "budget_category_code" in header
    assert "budget_head_id" in header
    data_rows = lines[1:]
    assert len(data_rows) == 2, "only w1 and w2 (category_x) may appear"

    budget_index = header.index("budget")
    exported_wbs = {row.split(",")[header.index("wbs_id")] for row in data_rows}
    assert exported_wbs == {ids["w1"], ids["w2"]}
    exported_total = sum(int(round(float(row.split(",")[budget_index]) * 100))
                         for row in data_rows)
    assert exported_total == 300_000

    with pg_database.session(scope) as session:
        card = rp.aggregate(session, filters)
    assert card["totals"]["budget"] == exported_total, (
        "the export's total must reconcile to the card's total for the same "
        "FilterSet")
