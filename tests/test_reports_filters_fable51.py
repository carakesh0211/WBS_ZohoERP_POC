"""Fable 5.1: everything about the `budget_category` / vendor / declared-and-
refused dimensions that is decidable from source, module constants or integer
arithmetic -- no PostgreSQL needed.

WHY THIS IS A SEPARATE FILE, NOT AN ADDITION TO `test_reporting_filterset.py`
================================================================================
`test_reporting_filterset.py` is owned by another agent's wave (file-ownership
boundary in the Fable 5.1 brief); its style -- pure-Python assertions against
`FilterSet`, `DIMENSIONS` and the raw SQL text -- is copied, not imported.

ONE KNOWN, DELIBERATE CONSEQUENCE FOR THE READER OF THIS FILE
================================================================================
`test_reporting_filterset.py::test_the_refusal_names_the_missing_column_not_
just_a_code` indexes `rp.UNSUPPORTED_FILTERS["vendor_ids"]` directly and now
fails with `KeyError`. That is not a Fable 5.1 defect: it is the OLD design
choice the brief asks to be changed -- `vendor_ids` was blanket-refused
because `purchase_order` carries no vendor id, and Fable 5.1 makes it
CONDITIONALLY refused instead (`FILTER_UNSUPPORTED_FOR_METRIC`, see below),
because `bill.vendor_id` (014) genuinely is a real column for the `actual`
bucket. `vendor_ids` therefore had to leave `UNSUPPORTED_FILTERS` -- staying
there would make it permanently refused again, which is the exact behaviour
being corrected. `test_reporting_filterset.py` is not touched (ownership
boundary), so that one assertion is left for whoever owns that file to update
to the new contract; this docstring is the record of why.
"""
from __future__ import annotations

import pytest

from app.backend.pg import reporting as rp


# ===========================================================================
# 1. budget_category is a REAL, INDEPENDENT dimension -- never an alias
# ===========================================================================

def test_budget_category_is_not_the_same_column_as_budget_head_or_category():
    assert rp.DIMENSIONS["budget_category"].key_sql == "f.budget_category_id"
    assert rp.DIMENSIONS["budget_head"].key_sql == "f.budget_head_id"
    assert rp.DIMENSIONS["category"].key_sql == "f.budget_head_id"
    assert (rp.DIMENSIONS["budget_category"].key_sql
            != rp.DIMENSIONS["budget_head"].key_sql)


def test_budget_category_is_absent_from_the_aliased_dimensions_table():
    """Only `category`/`budget_head` (AMB-04's pre-026 alias) may never be
    grouped together; `budget_category` is a genuinely separate column and
    must be freely composable with both."""
    for alias_group in rp._ALIASED_DIMENSIONS:
        assert "budget_category" not in alias_group


def test_budget_category_and_budget_head_may_be_grouped_together():
    fs = rp.FilterSet.build(group_by=["budget_category", "budget_head"])
    assert fs.group_by == ("budget_category", "budget_head")


def test_budget_category_ids_and_category_ids_are_two_different_fields():
    """`category_ids` (AMB-04's alias) narrows `budget_head_id`;
    `budget_category_ids` (026) narrows `budget_category_id`. Supplying
    both is legal -- both narrow, and they narrow DIFFERENT columns."""
    fs = rp.FilterSet.build(category_ids=["H1"], budget_category_ids=["C1"])
    params = rp._params(fs)
    assert params["category_ids"] == ["H1"]
    assert params["budget_category_ids"] == ["C1"]
    assert "budget_category_id" in rp._OUTER_PREDICATES
    assert "f.budget_head_id = ANY(%(category_ids)s)" in rp._OUTER_PREDICATES
    assert ("f.budget_category_id = ANY(%(budget_category_ids)s)"
            in rp._OUTER_PREDICATES)


def test_budget_category_drills_into_its_own_field_never_budget_head_ids():
    fs = rp.FilterSet.build()
    narrowed = fs.narrowed_to("budget_category", "C1")
    assert narrowed.budget_category_ids == ("C1",)
    assert narrowed.budget_head_ids is None


def test_budget_category_reads_the_cells_own_column_via_a_left_join():
    """"Reporting must read the CELL's category" -- `budget_control_cell`,
    never `budget_line`'s own (denormalised, immutable-once-set) copy. A LEFT
    JOIN, not INNER, so a pre-026 cell (budget_category_id NULL) still
    contributes its row."""
    for branch in (rp._BUDGET_BRANCH, rp._PO_BRANCH, rp._GRN_BRANCH,
                  rp._BILL_BRANCH, rp._PR_BRANCH):
        assert "LEFT JOIN budget_control_cell bc" in branch
        assert "bc.budget_category_id" in branch


def test_a_null_budget_category_key_renders_unclassified_not_a_blank():
    """`_shape`'s own rule, exercised without a database: build the row shape
    by hand and check the substitution fires."""
    grouped = ["budget_category_id"]
    group_by = ["budget_category"]
    terms = [("1", "ASC")]
    rows = [(None, 0, 0, 0, 0, 0, 0, 0, 0, 0, None, {})]
    shaped = rp._shape(rows, rp.FilterSet.build(group_by=group_by),
                       grouped, group_by, terms)
    assert shaped["rows"][0]["labels"]["budget_category"] == "Unclassified"
    assert shaped["rows"][0]["key"]["budget_category"] is None


# ===========================================================================
# 2. Vendor: conditionally refused, never a blanket UNSUPPORTED_FILTERS entry
# ===========================================================================

def test_vendor_ids_no_longer_lives_in_unsupported_filters():
    """UNSUPPORTED_FILTERS means "always refused, no matter what". Vendor is
    real on `bill.vendor_id` (014) for the `actual` bucket, so it cannot be
    an unconditional member any more -- see `validate()`'s own check."""
    assert "vendor_ids" not in rp.UNSUPPORTED_FILTERS
    assert "item_ids" in rp.UNSUPPORTED_FILTERS, (
        "item stays unconditionally refused: no table anywhere carries an "
        "item id, even for the actual bucket")


def test_vendor_ids_alone_is_refused_because_the_default_buckets_include_more_than_bill():
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(vendor_ids=["V1"])
    assert caught.value.code == "FILTER_UNSUPPORTED_FOR_METRIC"
    assert caught.value.status == 422
    assert caught.value.detail["dimension"] == "vendor"
    # `ordered`/`commitment`/`received`/`received_not_billed`/`pr_reserved`
    # have no vendor of their own -- named, not merely counted.
    for bucket in ("ordered", "commitment", "received",
                   "received_not_billed", "pr_reserved"):
        assert bucket in caught.value.detail["unsupported_buckets"]
    assert "actual" not in caught.value.detail["unsupported_buckets"]


def test_vendor_ids_is_honoured_once_document_types_narrows_to_bill():
    fs = rp.FilterSet.build(vendor_ids=["V1"], document_types=["BILL"])
    assert fs.vendor_ids == ("V1",)
    assert set(fs.buckets()) <= rp.VENDOR_SUPPORTED_BUCKETS


def test_grouping_by_vendor_is_refused_the_same_way_filtering_is():
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(group_by=["vendor"])
    assert caught.value.code == "FILTER_UNSUPPORTED_FOR_METRIC"


def test_grouping_by_vendor_is_honoured_under_document_types_bill():
    fs = rp.FilterSet.build(group_by=["vendor"], document_types=["BILL"])
    assert fs.group_by == ("vendor",)


def test_only_the_bill_branch_carries_a_real_vendor_id():
    """Every other branch's `vendor_id` is NULL by construction -- the outer
    predicate's `f.branch_kind = 'BUDGET' OR ...` guard only needs to exempt
    the unconditional budget denominator, because document_types has already
    narrowed the union to {BUDGET, BILL} by the time vendor_ids survives
    validate()."""
    assert "b.vendor_id" in rp._BILL_BRANCH
    for branch in (rp._BUDGET_BRANCH, rp._PO_BRANCH, rp._GRN_BRANCH,
                  rp._PR_BRANCH):
        assert "NULL::text AS vendor_id" in branch
    assert "branch_kind = 'BUDGET'" in rp._OUTER_PREDICATES


# ===========================================================================
# 3. Declared-and-refused: division / branch / zone / fiscal_year / requestor
#    / approver -- never silently ignored by FastAPI or by FilterSet
# ===========================================================================

@pytest.mark.parametrize("field_name,code", [
    ("division_ids", "DIVISION_DIMENSION_ABSENT"),
    ("branch_ids", "BRANCH_DIMENSION_ABSENT"),
    ("zone_ids", "ZONE_DIMENSION_ABSENT"),
    ("fiscal_years", "FISCAL_YEAR_DIMENSION_ABSENT"),
    ("requestor_ids", "REQUESTOR_DIMENSION_ABSENT"),
    ("approver_ids", "APPROVER_DIMENSION_ABSENT"),
    ("item_ids", "ITEM_DIMENSION_ABSENT"),
])
def test_the_schema_absent_dimensions_are_refused_with_their_own_code(
        field_name, code):
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(**{field_name: ["X"]})
    assert caught.value.code == code
    assert caught.value.status == 422


def test_an_unset_new_field_is_not_reported_as_unavailable():
    """Setting a field to `None` is not using it -- a default `FilterSet`
    still works."""
    assert rp.FilterSet.build().unsupported() == ()


def test_the_new_refused_fields_are_accepted_as_api_parameters_so_they_can_refuse():
    """Omitting them from `_common_filters` would make FastAPI drop them
    silently, which is exactly the failure the refusal exists to prevent."""
    import inspect

    from app.backend.api import reports as reports_api
    parameters = inspect.signature(reports_api._common_filters).parameters
    for field_name in ("budget_category_ids", "division_ids", "branch_ids",
                       "zone_ids", "fiscal_years", "requestor_ids",
                       "approver_ids", "vendor_ids"):
        assert field_name in parameters, (
            f"{field_name} is not a query parameter, so it would be ignored "
            f"rather than refused")


def test_the_dimensions_route_still_publishes_every_unconditional_refusal():
    from app.backend.api import reports as reports_api
    published = reports_api.get_dimensions()
    codes = {row["code"] for row in published["unavailable_filters"]}
    assert codes == {spec.code for spec in rp.UNSUPPORTED_FILTERS.values()}
    for code in ("DIVISION_DIMENSION_ABSENT", "BRANCH_DIMENSION_ABSENT",
                "ZONE_DIMENSION_ABSENT", "FISCAL_YEAR_DIMENSION_ABSENT",
                "REQUESTOR_DIMENSION_ABSENT", "APPROVER_DIMENSION_ABSENT",
                "ITEM_DIMENSION_ABSENT"):
        assert code in codes
    assert "VENDOR_DIMENSION_INCOMPLETE" not in codes, (
        "vendor is no longer a blanket refusal")
    dimension_names = {d["name"] for d in published["dimensions"]}
    assert "budget_category" in dimension_names
    assert "vendor" in dimension_names


# ===========================================================================
# 4. Round-trip: saved views carry the new fields, and refuse an unknown one
# ===========================================================================

def test_budget_category_ids_round_trips_through_a_saved_view():
    fs = rp.FilterSet.build(project_ids=["P1"], budget_category_ids=["C1", "C2"])
    payload = fs.to_json()
    assert payload["budget_category_ids"] == ["C1", "C2"]
    restored = rp.FilterSet.from_json(payload)
    assert restored.budget_category_ids == ("C1", "C2")
    assert restored.project_ids == ("P1",)


def test_a_saved_view_naming_an_unknown_field_is_refused_not_silently_applied():
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.from_json({"budget_category_ids": ["C1"], "supplier_ids": ["S1"]})
    assert caught.value.code == "MALFORMED_VIEW_DEFINITION"


# ===========================================================================
# 5. Exports: normalise_filters no longer chokes on a real FilterSet's own
#    baseline shape fields (cursor/limit/sort/sort_desc/group_by)
# ===========================================================================

def test_a_default_filterset_normalises_cleanly_for_an_export():
    from app.backend.pg import exports as export_svc

    fs = rp.FilterSet.build(project_ids=["P1"])
    normalised = export_svc.normalise_filters(fs)
    assert "limit" not in normalised
    assert "sort_desc" not in normalised
    assert "cursor" not in normalised
    assert "group_by" not in normalised
    assert normalised["project_ids"] == ["P1"]


def test_a_filterset_with_budget_category_reaches_the_budget_ledger_cells_dataset():
    from app.backend.pg import exports as export_svc

    fs = rp.FilterSet.build(project_ids=["P1"], budget_category_ids=["C1"])
    dataset, normalised = export_svc.plan_job("budget_ledger_cells", fs)
    conditions, params = export_svc.plan_filters(dataset, normalised)
    assert any("budget_category_id" in c for c in conditions)


def test_a_mapping_that_explicitly_asks_for_limit_is_still_refused():
    """The other half of the fix: a caller who deliberately writes
    `{"limit": 50}` into a raw mapping is still told no -- only a live
    FilterSet's OWN baseline fields are exempted, never a caller's explicit
    ask carried in a plain dict."""
    from app.backend.pg import exports as export_svc

    dataset = export_svc.get_dataset("budget_ledger_cells")
    normalised = export_svc.normalise_filters({"limit": 50})
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.plan_filters(dataset, normalised)
    assert caught.value.code == "UNSUPPORTED_FILTER"
