"""Wave 7 A1: the reporting layer, EXECUTED against a live PostgreSQL server.

THIS FILE IS THE HALF THAT ONLY A SERVER CAN ANSWER
===================================================

Its sibling, `tests/test_reporting_filterset.py`, runs everywhere and holds
everything decidable from source, module constants or integer arithmetic. What
is left over is what is here, and every one of these needs a real planner and a
real driver:

  * a drill-down summing back to the figure clicked is arithmetic over rows
    the database produced, not over a fixture somebody shaped to agree;
  * a keyset cursor skipping or repeating a row appears only when PostgreSQL
    is free to order tied rows as it likes, which no in-memory list does;
  * `SUM()` over `bigint` returns **numeric**, psycopg maps numeric to
    `decimal.Decimal`, and a `Decimal` reaching integer arithmetic CANNOT
    occur without a server -- it is the defect that took down the availability
    verdict, both approval paths and the concurrency proof, in the PostgreSQL
    CI job only;
  * a fan-out across four document tables multiplies money, and a fan-out is a
    property of a JOIN over real cardinalities.

A SKIP IS NOT A PASS
====================

There is no PostgreSQL on this workstation, so every test below skips here and
FIRST EXECUTES in CI's `pg_tests` job. The shared `PG` marker says so in its
reason, at length and deliberately: "n skipped" read as "n fine" is exactly how
a live-only guard rots. The one non-live test in this file is
`test_this_file_is_gated_and_says_a_skip_is_not_a_pass`, which fails if that
reason is ever softened into something a reader could mistake for a pass.

THE COLUMN NAMES BELOW WERE READ OUT OF THE MIGRATIONS, NOT REMEMBERED
======================================================================

A sibling agent shipped `INSERT INTO wbs_element (... code, name ...)` when the
columns are `wbs_code` and `description`; all 27 of its live tests skipped on
its workstation and errored in CI. Every insert here was written against
`002_budget_control.sql`, `013_procurement.sql`, `014_procurement_corrections.sql`,
`015_reservation_grain.sql` and `017_reporting.sql` -- and
`test_every_column_this_module_names_exists_in_a_migration` in the sibling file
holds the module's own SQL to the same standard on every machine.
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

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg is
# deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace. Same note as test_pg_reservations.py.
from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend.pg import reporting as rp  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

#: The one skip reason, so a skipped run reads as a SKIP rather than as a pass.
PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that EXECUTE the reporting SQL rather than reading it, and the "
            "numeric-to-Decimal defect they exist to catch cannot appear "
            "without a real server."),
)

TODAY = date.today()
EARLIER = TODAY - timedelta(days=30)


# ===========================================================================
# The estate, and the documents over it
# ===========================================================================
#
# EVERY FIGURE BELOW IS COMPUTED IN THIS COMMENT AND ASSERTED AS A LITERAL, so
# a test that fails says which bucket moved rather than "two expressions
# disagree". Amounts are paise and are deliberately un-round: a fixture of
# round rupees is how a floored `divmod` survived a whole wave.
#
# Project A, cell (ROOT_A, HEAD_1), unless said otherwise:
#
#   budget      one Approved ORIGINAL line ............ 1,000,000
#   PO_A        POL1 30,000 + 2,000 tax + 1,000 freight =  33,000
#               POL2 20,000 + 0 + 0 .....................  20,000
#               ordered .................................  53,000
#   GRN_OK      two lines on POL1, 10,000 + 5,000 ......   15,000
#   GRN_VOID    9,999 on POL1, status Void ............. not counted
#   GRN_REV     4,000 on POL1, is_reversal ............. -  4,000
#               received ................................  11,000
#   BILL_OK     on POL1: 8,000 + 500 tax + 200 freight ..    8,700
#   BILL_REV    on POL1: 1,000, accounting_status Reversal - 1,000
#   BILL_DRAFT  on POL1: 6,666, accounting_status Draft . not counted
#               billed against POL1 .....................   7,700
#   BILL_DIRECT no po_id at all, line names the cell ....    2,000
#               actual ..................................   9,700
#   PR_A        THREE pr_lines resolving to ROOT_A, and ONE reservation
#               row of 10,000 held on the resolved cell .   10,000
#               plus a second reservation row on (CHILD_A, HEAD_2) 7,000
#               plus a Released row of 50,000 ........... not counted
#
#   commitment  = GREATEST(0, ordered - billed) per LINE, PO not Cancelled/Closed
#                 POL1 33,000 - 7,700 = 25,300;  POL2 20,000 - 0 = 20,000
#                                                        =  45,300
#   received_not_billed = GREATEST(0, received - billed) per LINE
#                 POL1 11,000 - 7,700 = 3,300;   POL2 0
#                                                        =   3,300
#
# Project B sits on a DIFFERENT PLANT and carries one budget line and one bill,
# so "a plant-scoped principal sees no row from the other plant" has something
# real to exclude rather than an empty set to agree with.

EXPECTED_A = {
    "budget": 1_000_000,
    "original": 1_000_000,
    "revisions": 0,
    "ordered": 53_000,
    "commitment": 45_300,
    "actual": 9_700,
    "received": 11_000,
    "received_not_billed": 3_300,
    "pr_reserved": 17_000,          # 10,000 on ROOT_A + 7,000 on CHILD_A
}

#: Project B's budget line and its one bill.
EXPECTED_B = {"budget": 400_000, "actual": 5_500}

#: How many equal-budget cells `_seed` adds for the pagination test. Every one
#: carries the SAME budget, so a sort on `budget` is ALL ties and the order is
#: decided entirely by the tie-breaker -- which is the point.
TIE_CELLS = 5
TIE_BUDGET = 111_111


def _seed(connection, *, suffix: str) -> dict[str, str]:
    """The whole estate and every document over it, in one transaction.

    Returns the ids, keyed by role. Written with `connection.execute` and
    explicit column lists, deliberately: going through the service layer would
    make this a test of the service layer, and the arithmetic under test is the
    REPORTING SQL's, over rows whose shape is stated here rather than inferred.
    """
    ids = {
        "entity": f"E_{suffix}",
        "org": f"O_{suffix}",
        "plant_a": f"PLA_{suffix}", "plant_b": f"PLB_{suffix}",
        "loc_a": f"LOA_{suffix}", "loc_b": f"LOB_{suffix}",
        "project_a": f"PRJA_{suffix}", "project_b": f"PRJB_{suffix}",
        "root_a": f"RA_{suffix}", "child_a": f"CA_{suffix}",
        "root_b": f"RB_{suffix}",
        "head_1": f"H1_{suffix}", "head_2": f"H2_{suffix}",
        "po": f"PO_{suffix}", "pol1": f"POL1_{suffix}", "pol2": f"POL2_{suffix}",
        "pr": f"PR_{suffix}",
    }
    ex = connection.execute

    # ---------------------------------------------------------- masters
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    for plant, code in (("plant_a", "PA"), ("plant_b", "PB")):
        ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, "
           "updated_by) VALUES (%s,%s,%s,%s,'t','t')",
           (ids[plant], ids["entity"], f"{code}_{suffix}", f"Plant {code}"))
    for location, plant, code in (("loc_a", "plant_a", "LA"),
                                  ("loc_b", "plant_b", "LB")):
        ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
           "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')",
           (ids[location], ids["entity"], ids[plant], f"{code}_{suffix}",
            f"Location {code}"))
    for project, plant, location, code in (
            ("project_a", "plant_a", "loc_a", "CA"),
            ("project_b", "plant_b", "loc_b", "CB")):
        ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, "
           "capex_code, name, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,%s,'Project','Released','t','t')",
           (ids[project], ids["entity"], ids[plant], ids[location],
            f"{code}_{suffix}"))
    for head in ("head_1", "head_2"):
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
           "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
           (ids[head], ids["entity"], f"{head.upper()}_{suffix}",
            f"Head {head}"))

    # ------------------------------------------------------------- WBS
    # `wbs_path` is an ltree. The ids are hex-suffixed, so every label is a
    # legal ltree label (letters, digits, underscore) without escaping.
    for root, project in (("root_a", "project_a"), ("root_b", "project_b")):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
           "wbs_path, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,'root',%s,'Released','t','t')",
           (ids[root], ids[project], ids[root], ids[root]))
    ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, wbs_code, "
       "description, wbs_path, status, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'child',%s,'Released','t','t')",
       (ids["child_a"], ids["project_a"], ids["root_a"], ids["child_a"],
        f"{ids['root_a']}.{ids['child_a']}"))

    def cell(wbs: str, head: str, budget: int = 0) -> None:
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_paise, updated_by) VALUES (%s,%s,%s,'t')",
           (wbs, head, budget))

    cell(ids["root_a"], ids["head_1"], EXPECTED_A["budget"])
    cell(ids["child_a"], ids["head_2"])
    cell(ids["root_b"], ids["head_1"], EXPECTED_B["budget"])

    # ---------------------------------------------------------- budget
    def budget_line(line_id: str, wbs: str, head: str, amount: int,
                    *, kind: str = "ORIGINAL", status: str = "Approved") -> None:
        ex("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, "
           "kind, amount_paise, effective_from, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,'t','t')",
           (line_id, wbs, head, kind, amount, EARLIER, status))

    budget_line(f"BLA_{suffix}", ids["root_a"], ids["head_1"],
                EXPECTED_A["budget"])
    budget_line(f"BLB_{suffix}", ids["root_b"], ids["head_1"],
                EXPECTED_B["budget"])
    # AUD-C-005: a Draft line creates NO capacity. If it were counted, project
    # A's budget would be 1,000,000 + 999,999 and every assertion would move.
    budget_line(f"BLD_{suffix}", ids["root_a"], ids["head_1"], 999_999,
                kind="REVISION", status="Draft")

    # The equal-budget cells the pagination test paginates. Their budget is
    # identical by construction, so the sort is entirely ties.
    for index in range(TIE_CELLS):
        wbs = f"T{index}_{suffix}"
        ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, "
           "wbs_code, description, wbs_path, status, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'tie',%s,'Released','t','t')",
           (wbs, ids["project_a"], ids["root_a"], wbs,
            f"{ids['root_a']}.{wbs}"))
        cell(wbs, ids["head_1"], TIE_BUDGET)
        budget_line(f"BLT{index}_{suffix}", wbs, ids["head_1"], TIE_BUDGET)

    # ------------------------------------------- purchase request + holds
    ex("INSERT INTO purchase_request (pr_id, pr_number, project_id, "
       "requested_by, requested_at, status, reserves_budget, created_by, "
       "updated_by) VALUES (%s,%s,%s,'U-REQ',%s,'Approved',true,'t','t')",
       (ids["pr"], f"PRN_{suffix}", ids["project_a"], EARLIER))
    # THREE lines, all resolving to the SAME cell as the ONE reservation held
    # against it. Migration 015's grain: the hold is per (PR x resolved cell),
    # so a branch that joined `pr_line` to reach "the request's lines" would
    # report 30,000 where 10,000 is held. That is the whole point of these
    # three rows.
    for line_no in (1, 2, 3):
        ex("INSERT INTO pr_line (pr_line_id, pr_id, line_no, project_id, "
           "wbs_id, budget_head_id, quantity, amount_paise, created_by, "
           "updated_by) VALUES (%s,%s,%s,%s,%s,%s,1,%s,'t','t')",
           (f"PRL{line_no}_{suffix}", ids["pr"], line_no, ids["project_a"],
            ids["root_a"], ids["head_1"], 3_000))

    def reservation(reservation_id: str, wbs: str, head: str, amount: int,
                    state: str = "Reserved") -> None:
        settled = (None, None) if state == "Reserved" else (TODAY, "U-SET")
        ex("INSERT INTO pr_reservation (reservation_id, pr_id, wbs_id, "
           "budget_head_id, project_id, amount_paise, state, settled_at, "
           "settled_by, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'t','t')",
           (reservation_id, ids["pr"], wbs, head, ids["project_a"], amount,
            state, settled[0], settled[1]))

    reservation(f"RES1_{suffix}", ids["root_a"], ids["head_1"], 10_000)
    reservation(f"RES2_{suffix}", ids["child_a"], ids["head_2"], 7_000)
    # A Released hold has been GIVEN BACK. Counting it beside `commitment` is
    # the classic double count between two of the four buckets.
    reservation(f"RES3_{suffix}", ids["root_a"], ids["head_1"], 50_000,
                state="Released")

    # ------------------------------------------------- purchase order
    ex("INSERT INTO purchase_order (po_id, po_number, pr_id, project_id, "
       "vendor_name, status, ordered_at, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'Vendor','Released',%s,'t','t')",
       (ids["po"], f"PON_{suffix}", ids["pr"], ids["project_a"], EARLIER))
    for line_id, line_no, amount, tax, freight in (
            (ids["pol1"], 1, 30_000, 2_000, 1_000),
            (ids["pol2"], 2, 20_000, 0, 0)):
        ex("INSERT INTO po_line (po_line_id, po_id, line_no, project_id, "
           "wbs_id, budget_head_id, quantity, rate_paise, amount_paise, "
           "tax_paise, non_creditable_tax_paise, freight_paise, created_by, "
           "updated_by) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,0,%s,%s,'t','t')",
           (line_id, ids["po"], line_no, ids["project_a"], ids["root_a"],
            ids["head_1"], amount, amount, tax, freight))

    # ------------------------------------------------------------ GRNs
    def grn(grn_id: str, *, status: str = "Approved", reversal: bool = False,
            lines: tuple[tuple[str, int], ...] = ()) -> None:
        ex("INSERT INTO grn (grn_id, grn_number, po_id, received_at, status, "
           "is_reversal, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,%s,%s,'t','t')",
           (grn_id, f"GN_{grn_id}", ids["po"], EARLIER, status, reversal))
        for line_id, amount in lines:
            ex("INSERT INTO grn_line (grn_line_id, grn_id, po_id, po_line_id, "
               "quantity, amount_paise, created_by, updated_by) "
               "VALUES (%s,%s,%s,%s,1,%s,'t','t')",
               (line_id, grn_id, ids["po"], ids["pol1"], amount))

    grn(f"GOK_{suffix}", lines=((f"GLA_{suffix}", 10_000),
                                (f"GLB_{suffix}", 5_000)))
    grn(f"GVOID_{suffix}", status="Void", lines=((f"GLV_{suffix}", 9_999),))
    # A reversal stored POSITIVE. `-ABS(...)` is why a reversal stored negative
    # would reduce `received` by the same amount and not increase it.
    grn(f"GREV_{suffix}", reversal=True, lines=((f"GLR_{suffix}", 4_000),))

    # ----------------------------------------------------------- bills
    def bill(bill_id: str, *, accounting_status: str, po_backed: bool,
             line_id: str, amount: int, tax: int = 0, freight: int = 0) -> None:
        ex("INSERT INTO bill (bill_id, bill_number, po_id, project_id, "
           "vendor_name, bill_date, status, accounting_status, created_by, "
           "updated_by) VALUES (%s,%s,%s,%s,'Vendor',%s,'Approved',%s,'t','t')",
           (bill_id, f"BN_{bill_id}", ids["po"] if po_backed else None,
            ids["project_a"], EARLIER, accounting_status))
        ex("INSERT INTO bill_line (bill_line_id, bill_id, po_id, po_line_id, "
           "wbs_id, budget_head_id, quantity, amount_paise, "
           "non_creditable_tax_paise, freight_paise, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'t','t')",
           (line_id, bill_id, ids["po"] if po_backed else None,
            ids["pol1"] if po_backed else None, ids["root_a"], ids["head_1"],
            amount, tax, freight))

    bill(f"BOK_{suffix}", accounting_status="Approved", po_backed=True,
         line_id=f"BLOK_{suffix}", amount=8_000, tax=500, freight=200)
    bill(f"BREV_{suffix}", accounting_status="Reversal", po_backed=True,
         line_id=f"BLREV_{suffix}", amount=1_000)
    # AUD-C-004: a Draft bill is not accounting-effective and moves nothing.
    bill(f"BDRAFT_{suffix}", accounting_status="Draft", po_backed=True,
         line_id=f"BLDRAFT_{suffix}", amount=6_666)
    # A NON-PO bill. 013 makes `po_id`/`po_line_id` nullable for exactly this,
    # and routing actuals through `po_line` would drop it silently.
    bill(f"BDIR_{suffix}", accounting_status="Approved", po_backed=False,
         line_id=f"BLDIR_{suffix}", amount=2_000)
    # Project B's one bill, on the OTHER plant.
    ex("INSERT INTO bill (bill_id, bill_number, project_id, vendor_name, "
       "bill_date, status, accounting_status, created_by, updated_by) "
       "VALUES (%s,%s,%s,'Vendor',%s,'Approved','Approved','t','t')",
       (f"BB_{suffix}", f"BNB_{suffix}", ids["project_b"], EARLIER))
    ex("INSERT INTO bill_line (bill_line_id, bill_id, wbs_id, budget_head_id, "
       "quantity, amount_paise, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,1,%s,'t','t')",
       (f"BLB2_{suffix}", f"BB_{suffix}", ids["root_b"], ids["head_1"],
        EXPECTED_B["actual"]))

    connection.commit()
    return ids


@pytest.fixture()
def estate(pg_connection):
    """A freshly seeded estate in this test's own disposable database."""
    return _seed(pg_connection, suffix=uuid.uuid4().hex[:10])


def _scope(ids: dict[str, str], **restrictions) -> Scope:
    """A restricted principal over this estate. Never `read_all`.

    `Scope.system()` compiles to `TRUE` and would prove nothing about the
    scope predicate, so every test here names its grants.
    """
    return Scope(
        user_id="U-REPORT",
        principal_kind="USER",
        entity_ids=frozenset({ids["entity"]}),
        plant_ids=restrictions.get("plant_ids"),
        project_ids=restrictions.get("project_ids"),
        location_ids=restrictions.get("location_ids"),
        read_all=False,
    )


def _project_a_filters(ids: dict[str, str], **overrides) -> rp.FilterSet:
    return rp.FilterSet.build(project_ids=[ids["project_a"]], **overrides)


# ===========================================================================
# The gate on the gate
# ===========================================================================

def test_this_file_is_gated_and_says_a_skip_is_not_a_pass():
    """The only test here that runs on this workstation.

    Everything else skips without a server, so the reason a reader sees in the
    skip report is the ONLY thing standing between "n skipped" and "n fine".
    If that sentence is ever softened, this fails.
    """
    reason = PG.kwargs["reason"]
    assert "SKIP, NOT A PASS" in reason
    assert "CAPEX_DB_URL" in reason


# ===========================================================================
# 1. The drill-down round trip
# ===========================================================================

@pytest.mark.pg
@PG
def test_a_grouped_total_equals_the_sum_of_the_rows_its_drill_down_returns(
        pg_database, estate):
    """THE CONTRACT: "a drill-down that does not sum back to the figure clicked
    is a defect, and a test asserts the round trip." This is that test.

    Clicked: one plant's `commitment`. Drilled: the same figure at (wbs,
    budget_head) grain. The parts must sum to the whole for EVERY component,
    not only for the one the eye is on -- a drill-down that reconciles on
    commitment and not on actual is still a screen showing two answers.
    """
    ids = estate
    filters = _project_a_filters(ids, group_by=["plant"])
    with pg_database.session(_scope(ids)) as session:
        card = rp.aggregate(session, filters)
        assert card["state"] == "ok"
        [clicked] = [r for r in card["rows"]
                     if r["key"]["plant"] == ids["plant_a"]]

        drill = rp.drill_down(session, filters, "plant", ids["plant_a"])

    assert drill["state"] == "ok"
    assert drill["drill_of"] == {"dimension": "plant", "key": ids["plant_a"]}
    for component in rp.COMPONENTS:
        assert sum(row[component] for row in drill["rows"]) == clicked[component], (
            f"{component}: the drill-down rows do not sum back to the figure "
            f"clicked")


@pytest.mark.pg
@PG
def test_a_drill_down_intersects_and_cannot_widen_what_was_clicked(
        pg_database, estate):
    """Clicking plant B on a report already filtered to project A must return
    NOTHING, because project A has no row on plant B -- and not project B's
    rows, which is what replacing the filter instead of intersecting it would
    show. The figure clicked never counted them."""
    ids = estate
    filters = _project_a_filters(ids, group_by=["plant"])
    with pg_database.session(_scope(ids)) as session:
        drill = rp.drill_down(session, filters, "plant", ids["plant_b"])

    assert drill["rows"] == []
    assert drill["state"] == "empty", (
        "an entitled query over a population with no rows is `empty`; "
        "`denied` is a different sentence and means a different thing")
    assert drill["totals"]["actual"] == 0


# ===========================================================================
# 2. Scope, negatively
# ===========================================================================

@pytest.mark.pg
@PG
def test_a_plant_scoped_principal_gets_no_row_from_the_other_plant(
        pg_database, estate):
    """The negative case, which is the only one that proves anything.

    Project B exists, carries budget and an approved bill, and sits on plant B.
    A principal granted plant A must not see one paisa of it -- not in a row,
    and not in the grand total, which is computed over the same scoped fact
    and would otherwise report an estate-wide figure under a filtered table.

    WHAT THIS PROVES, EXACTLY: the reporting layer's OWN control, the `{scope}`
    predicate `repo.query` compiles from the caller's grants into every
    statement in the module. `pg_database` connects as CI's superuser, and a
    superuser bypasses row-level security unconditionally, so RLS contributes
    nothing here -- which is the point: this test would still fail if every
    policy in the schema were dropped, and that is the layer it is written
    against. The policies themselves are held by `test_pg_rls_coverage.py`
    and `test_pg_rls_integration_matrix.py`, through the scoped-role fixture.
    """
    ids = estate
    everywhere = rp.FilterSet.build(group_by=["plant"])
    with pg_database.session(
            _scope(ids, plant_ids=frozenset({ids["plant_a"]}))) as session:
        result = rp.aggregate(session, everywhere)

    plants = {row["key"]["plant"] for row in result["rows"]}
    assert plants == {ids["plant_a"]}, f"out-of-scope plant leaked: {plants}"
    assert result["totals"]["actual"] == EXPECTED_A["actual"], (
        "the grand total must be over the SCOPED population; project B's "
        "bill is outside this principal's grant")
    assert result["totals"]["budget"] == (
        EXPECTED_A["budget"] + TIE_CELLS * TIE_BUDGET)


@pytest.mark.pg
@PG
def test_an_unscoped_principal_is_denied_and_not_merely_empty(
        pg_database, estate):
    """Four empty frozensets is "you may see nothing", which is a DIFFERENT
    sentence from "there is nothing here" -- and over a quiet month the two
    produce the same empty table. The state carries the difference."""
    ids = estate
    denied = Scope(user_id="U-NONE", principal_kind="USER",
                   entity_ids=frozenset(), plant_ids=frozenset(),
                   project_ids=frozenset(), location_ids=frozenset(),
                   read_all=False)
    with pg_database.session(denied) as session:
        result = rp.aggregate(session, rp.FilterSet.build(group_by=["plant"]))

    assert result["state"] == "denied"
    assert result["rows"] == []
    assert "cannot see" in (result["detail"] or "")


# ===========================================================================
# 3. Pagination
# ===========================================================================

@pytest.mark.pg
@PG
def test_pagination_skips_no_row_and_repeats_none_across_every_page(
        pg_database, estate):
    """A keyset cursor over a NON-DETERMINISTIC order skips a row on one page
    and repeats it on the next, and the pages then do not sum to the total the
    same query reports.

    The seeded estate makes that failure reachable: `TIE_CELLS` cells carry
    an IDENTICAL budget, so a sort on `budget` is all ties and PostgreSQL is
    free to order them however it likes on each execution. Only the group key
    appended to every ordering -- unique per row by construction -- makes the
    walk exact.
    """
    ids = estate
    scope = _scope(ids)
    seen: list[tuple] = []
    cursor = None
    pages = 0
    with pg_database.session(scope) as session:
        while True:
            page = rp.aggregate(session, _project_a_filters(
                ids, group_by=["wbs", "budget_head"], sort="budget",
                sort_desc=True, limit=2, cursor=cursor))
            assert page["state"] == "ok"
            seen.extend((row["key"]["wbs"], row["key"]["budget_head"])
                        for row in page["rows"])
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                assert not page["has_more"]
                break
            assert pages < 50, "cursor is not advancing"

        whole = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], sort="budget",
            sort_desc=True, limit=rp.MAX_LIMIT))

    assert pages > 1, "the fixture must span more than one page to prove anything"
    assert len(seen) == len(set(seen)), f"a row was repeated across pages: {seen}"
    expected = [(row["key"]["wbs"], row["key"]["budget_head"])
                for row in whole["rows"]]
    assert sorted(seen) == sorted(expected), "a row was skipped across pages"
    assert seen == expected, (
        "the paginated walk must be in the SAME order as the unpaginated one; "
        "a cursor that resumes from the wrong place still returns every row")


@pytest.mark.pg
@PG
def test_the_total_is_the_populations_and_never_the_pages(
        pg_database, estate):
    """`totals` is computed over the whole filtered population in the same
    statement. Summing the page instead would make page two report a different
    "total" from page one, and neither would be the answer to the question."""
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        page = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], limit=1))
        whole = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], limit=rp.MAX_LIMIT))

    assert len(page["rows"]) == 1
    assert page["has_more"] is True
    assert page["totals"] == whole["totals"]
    assert page["totals"]["budget"] > page["rows"][0]["budget"], (
        "a one-row page whose total equals its single row is a total summed "
        "from the page")
    for component in rp.COMPONENTS:
        assert sum(row[component] for row in whole["rows"]) \
            == whole["totals"][component]


# ===========================================================================
# 4. Money
# ===========================================================================

@pytest.mark.pg
@PG
def test_every_aggregate_is_an_int_and_never_a_decimal_or_a_float(
        pg_database, estate):
    """`SUM()` over `bigint` returns NUMERIC; psycopg maps numeric to
    `decimal.Decimal`; and a `Decimal` reaching arithmetic that expects an int
    is the defect that took down the availability verdict, both approval paths
    and the concurrency proof -- in the PostgreSQL job only, because it cannot
    appear without a server. THIS IS THAT TEST, and it can only run here.

    `type(...) is int` rather than `isinstance`: `bool` is a subclass of `int`
    and `Decimal` compares equal to an int it equals, so both an `isinstance`
    check and an `== int(...)` check would pass on the very value this is
    written to catch.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], limit=rp.MAX_LIMIT))

    integral = (*rp.COMPONENTS, "exposure", "available")
    for measure in integral:
        assert type(result["totals"][measure]) is int, (
            f"totals[{measure}] is {type(result['totals'][measure])}, not int")
    for row in result["rows"]:
        for measure in integral:
            assert type(row[measure]) is int, (
                f"row {row['key']}[{measure}] is {type(row[measure])}")
    # The one deliberate non-integer: a percentage is a ratio, never money.
    assert isinstance(result["totals"]["utilisation_pct"], float)


@pytest.mark.pg
@PG
def test_the_derived_measures_are_derived_from_the_rows_own_components(
        pg_database, estate):
    """`exposure` and `available` are never stored, so there is no path by
    which a persisted `available` can disagree with the budget and exposure it
    is the difference of. Asserted on live rows, where a Decimal would have
    made the arithmetic silently produce a Decimal."""
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], limit=rp.MAX_LIMIT))

    for row in (*result["rows"], result["totals"]):
        assert row["exposure"] == (row["commitment"] + row["actual"]
                                   + row["pr_reserved"])
        assert row["available"] == row["budget"] - row["exposure"]


# ===========================================================================
# 5. Four buckets, no double counting
# ===========================================================================

@pytest.mark.pg
@PG
def test_the_four_buckets_are_each_counted_exactly_once(pg_database, estate):
    """THE FAN-OUT TEST, and the reason the branches are UNIONed and never
    joined.

    One PO line here carries THREE receipt lines and THREE bill lines. Under a
    join that is nine rows for one order, and `ordered` would be reported nine
    times over. Every figure below is the arithmetic stated in this module's
    header, computed independently of the SQL, and each is asserted as a
    literal so a failure names the bucket that moved.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, _project_a_filters(ids))

    assert result["state"] == "ok"
    totals = result["totals"]
    for component, expected in EXPECTED_A.items():
        if component in ("budget", "original"):
            # The tie cells carry budget too; they are counted here.
            expected += TIE_CELLS * TIE_BUDGET
        assert totals[component] == expected, (
            f"{component} is {totals[component]}, expected {expected}")


@pytest.mark.pg
@PG
def test_open_commitment_is_ordered_less_billed_and_never_less_received(
        pg_database, estate):
    """The defect an adversarial review found twice: two functions in one file
    computing `open_paise` differently.

    The fixture separates the two answers by construction -- POL1 is billed
    7,700 and received 11,000 -- so ordered-less-received would report 22,000
    against POL1 where ordered-less-billed reports 25,300, and the totals
    differ by 3,300.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        totals = rp.aggregate(session, _project_a_filters(ids))["totals"]

    assert totals["commitment"] == 45_300
    assert totals["commitment"] != 53_000 - totals["received"], (
        "commitment has been computed as ordered less RECEIVED")
    assert totals["received_not_billed"] == 3_300, (
        "received-not-billed is its own bucket and is not netted into "
        "commitment")


@pytest.mark.pg
@PG
def test_a_void_grn_is_not_received_and_a_reversal_subtracts_its_magnitude(
        pg_database, estate):
    """The other found defect: a `received_paise` that counted Void GRNs for
    want of a join to `grn`, and did not negate a reversal.

    15,000 was genuinely received, 9,999 was voided and 4,000 reversed. A
    reading that counted the Void GRN would report 24,999; one that added the
    reversal instead of subtracting it, 19,000.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        totals = rp.aggregate(session, _project_a_filters(ids))["totals"]

    assert totals["received"] == 11_000
    assert totals["received"] != 24_999, "a Void GRN was counted"
    assert totals["received"] != 19_000, "a reversal was added, not subtracted"


@pytest.mark.pg
@PG
def test_a_multi_cell_reservation_is_held_once_per_cell_and_not_once_per_line(
        pg_database, estate):
    """MIGRATION 015'S GRAIN, live.

    The seeded request holds 10,000 on (ROOT_A, HEAD_1) and 7,000 on
    (CHILD_A, HEAD_2) -- two rows for ONE request, which is exactly what 014's
    `UNIQUE (pr_id)` forbade -- and carries THREE `pr_line` rows all resolving
    to the first of those cells. A branch that joined `pr_line` would multiply
    that cell's single hold by three and report 30,000 against it.

    The Released 50,000 is in the fixture for the other half: a hold that has
    been given back is not exposure, and counting it beside `commitment` is
    the classic double count between two of the four buckets.
    """
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs", "budget_head"], limit=rp.MAX_LIMIT))

    held = {(row["key"]["wbs"], row["key"]["budget_head"]): row["pr_reserved"]
            for row in result["rows"]}
    assert held[(ids["root_a"], ids["head_1"])] == 10_000, (
        "three pr_lines against one resolved cell must not multiply its "
        "single hold")
    assert held[(ids["child_a"], ids["head_2"])] == 7_000
    assert result["totals"]["pr_reserved"] == 17_000
    assert result["totals"]["pr_reserved"] != 67_000, (
        "the Released hold has been counted")


@pytest.mark.pg
@PG
def test_a_non_po_bill_reaches_cwip_through_its_own_cell(pg_database, estate):
    """A non-PO bill names no PO line -- 013 makes both columns nullable for
    exactly that case -- so routing actuals through `po_line` drops every
    direct bill from CWIP, silently, and only for the entities that raise
    them. 2,000 of this estate's 9,700 actual is such a bill."""
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        totals = rp.aggregate(session, _project_a_filters(
            ids, document_types=["BILL"]))["totals"]

    assert totals["actual"] == 9_700
    assert totals["actual"] != 7_700, "the direct bill was dropped"
    # Selecting one document type still supplies the denominator: a
    # utilisation percentage with no budget is not a utilisation percentage.
    assert totals["budget"] == EXPECTED_A["budget"] + TIE_CELLS * TIE_BUDGET


# ===========================================================================
# 6. The filters, executed
# ===========================================================================

@pytest.mark.pg
@PG
def test_a_draft_budget_line_creates_no_spending_capacity(pg_database, estate):
    """AUD-C-005, executed. The fixture carries a Draft REVISION of 999,999
    against the same cell; if status were not filtered it would appear in
    `budget` and in `revisions`, and every availability figure over this cell
    would be wrong by that amount."""
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        result = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs"], limit=rp.MAX_LIMIT))

    [root] = [r for r in result["rows"] if r["key"]["wbs"] == ids["root_a"]]
    assert root["budget"] == EXPECTED_A["budget"]
    assert root["revisions"] == 0


@pytest.mark.pg
@PG
def test_an_empty_filter_tuple_restricts_to_nothing_rather_than_widening(
        pg_database, estate):
    """`None` is "unfiltered", `()` is "restricted to nothing". A bug that
    turns "no ids" into "all ids" is the failure the whole scope layer exists
    to prevent, and this is the one place it can be observed end to end: the
    empty array reaches the driver and `= ANY('{}')` is false for every row."""
    ids = estate
    with pg_database.session(_scope(ids)) as session:
        unfiltered = rp.aggregate(
            session, rp.FilterSet.build(group_by=["project"]))
        restricted = rp.aggregate(
            session, rp.FilterSet.build(group_by=["project"], project_ids=[]))

    assert unfiltered["totals"]["actual"] == (
        EXPECTED_A["actual"] + EXPECTED_B["actual"])
    assert restricted["state"] == "empty"
    assert restricted["totals"]["actual"] == 0


@pytest.mark.pg
@PG
def test_a_period_is_matched_against_the_entitys_own_calendar(
        pg_database, pg_connection, estate):
    """`accounting_period` carries `entity_id`, and 001's EXCLUDE constraint
    only stops ONE entity's periods overlapping. Resolving a period id to a
    single span in Python and applying it estate-wide would be right for
    whichever entity was resolved first and silently wrong for the rest.

    Executed here as the simpler observable half: a period that CONTAINS every
    document's date returns the whole population, and one that contains none of
    them returns nothing -- the join is correlated, so a period belonging to
    another entity contributes no rows rather than an error.
    """
    ids = estate
    covering = f"AP1_{ids['entity']}"[:60]
    missing = f"AP2_{ids['entity']}"[:60]
    pg_connection.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, "
        "period_end, state, created_by) VALUES (%s,%s,%s,%s,'OPEN','t')",
        (covering, ids["entity"], EARLIER - timedelta(days=1),
         TODAY + timedelta(days=1)))
    pg_connection.execute(
        "INSERT INTO accounting_period (period_id, entity_id, period_start, "
        "period_end, state, created_by) VALUES (%s,%s,%s,%s,'OPEN','t')",
        (missing, ids["entity"], TODAY + timedelta(days=10),
         TODAY + timedelta(days=20)))
    pg_connection.commit()

    with pg_database.session(_scope(ids)) as session:
        inside = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs"], period_ids=[covering], limit=rp.MAX_LIMIT))
        outside = rp.aggregate(session, _project_a_filters(
            ids, group_by=["wbs"], period_ids=[missing], limit=rp.MAX_LIMIT))

    assert inside["totals"]["actual"] == EXPECTED_A["actual"]
    assert outside["state"] == "empty"
    assert outside["totals"]["actual"] == 0
