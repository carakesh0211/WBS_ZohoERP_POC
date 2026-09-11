"""Wave 7 A1: the canonical `FilterSet`, and the SQL it builds, without a database.

EVERY TEST IN THIS FILE RUNS EVERYWHERE. That is the whole reason it is a
separate file from `test_pg_reporting.py`, whose live half skips on every
workstation here and first executes in CI's `pg_tests` job. A property that is
only checked where nobody runs it is a property nobody checks, so everything
assertable against source, module constants or integer arithmetic is asserted
here.

WHAT THIS FILE IS WRITTEN AGAINST
=================================

Three defect classes, each one already realised in this repository:

  * **A second filter shape.** `docs/WAVE7_CONTRACT.md`: "A second filter shape
    is how a dashboard card and its own drill-down come to disagree." So the
    drill-down narrowing is asserted to be an INTERSECTION, and the refusals
    are asserted to be refusals rather than silent drops.
  * **A formula re-derived instead of transcribed.** An adversarial review
    found two functions in ONE file computing `open_paise` differently, and a
    `received_paise` that counted Void GRNs for want of a join. So the SQL is
    read as text and asserted to contain the guards `domain.compute_ledger`
    contains.
  * **SQL written against remembered column names.** A sibling agent shipped
    `INSERT INTO wbs_element (... code, name ...)` when the columns are
    `wbs_code` / `description`; all 27 of its live tests skipped locally and
    would have errored in CI. `test_every_column_this_module_names_exists_in_a_migration`
    reads the migrations and checks every qualified column reference in this
    module's SQL against them.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.backend import domain
from app.backend.pg import reporting as rp
from app.backend.pg import repo
from app.backend.pg.engine import Scope

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
REPORTING_PY = ROOT / "app" / "backend" / "pg" / "reporting.py"
CONTRACTS = ROOT / "research" / "30_contracts"


# ===========================================================================
# The FilterSet's own semantics
# ===========================================================================

def test_none_and_empty_are_different_and_stay_different():
    """`None` is "unfiltered", `()` is "restricted to nothing".

    The same distinction `Scope` draws, and for the same reason: a bug that
    turns "no ids" into "all ids" is the failure the whole scope layer exists
    to prevent. A caller who sends an empty list asked for an empty answer.
    """
    assert rp.FilterSet.build().entity_ids is None
    assert rp.FilterSet.build(entity_ids=[]).entity_ids == ()
    assert rp.FilterSet.build(entity_ids=["E1"]).entity_ids == ("E1",)


def test_the_wire_preserves_none_versus_empty():
    """...all the way into the query parameters.

    `None` becomes SQL NULL, so the `IS NULL OR ...` guard passes and nothing
    is filtered. `()` becomes an empty array, and `= ANY('{}')` is false for
    every row. Collapsing the second into the first would silently widen.
    """
    unfiltered = rp._params(rp.FilterSet.build())
    restricted = rp._params(rp.FilterSet.build(entity_ids=[]))
    assert unfiltered["entity_ids"] is None
    assert restricted["entity_ids"] == []


def test_duplicate_ids_are_collapsed_but_order_is_kept():
    assert rp.FilterSet.build(
        project_ids=["P2", "P1", "P2"]).project_ids == ("P2", "P1")


def test_an_unknown_filter_field_is_refused_not_ignored():
    """The core rule. An ignored filter returns a correct-looking total for a
    population nobody asked for."""
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(supplier_ids=["V1"])
    assert caught.value.code == "UNKNOWN_FILTER"
    assert "supplier_ids" in caught.value.message


@pytest.mark.parametrize("field_name", sorted(rp.UNSUPPORTED_FILTERS))
def test_a_filter_with_no_column_is_refused_with_a_code(field_name):
    """`vendor_ids` and `item_ids` have nothing to bind to, and say so.

    NOT silently dropped, which is what makes this worth a test: a dropped
    vendor filter shows the whole estate's commitments under one vendor's name,
    and nothing on screen says so.
    """
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(**{field_name: ["X"]})
    spec = rp.UNSUPPORTED_FILTERS[field_name]
    assert caught.value.code == spec.code
    assert caught.value.status == 422


def test_the_refusal_names_the_missing_column_not_just_a_code():
    """"Never fabricate a total -- return an explicit coded unavailable state
    naming what is missing." A code alone does not name anything.

    `vendor_ids` left UNSUPPORTED_FILTERS when `bill.vendor_id` (014) made it
    real for the `actual` bucket; its refusal is now CONDITIONAL and lives in
    `FilterSet.validate`, so the wording is asserted on the raised error."""
    assert "item_id" in rp.UNSUPPORTED_FILTERS["item_ids"].detail
    assert "vendor_ids" not in rp.UNSUPPORTED_FILTERS
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(vendor_ids=["V-1"])
    assert caught.value.code == "FILTER_UNSUPPORTED_FOR_METRIC"
    assert "bill.vendor_id" in caught.value.message
    assert "BILL" in caught.value.message
    assert caught.value.detail["dimension"] == "vendor"
    # Narrowed to the one bucket that carries a vendor, the filter is honoured.
    honoured = rp.FilterSet.build(vendor_ids=["V-1"], document_types=["BILL"])
    assert honoured.vendor_ids == ("V-1",)


def test_an_unset_unsupported_filter_is_not_reported_as_unavailable():
    """Setting a field to `None` is not using it. A default `FilterSet` must
    not render an unavailable state over a report that works."""
    assert rp.FilterSet.build().unsupported() == ()


def test_an_unknown_dimension_is_refused():
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(group_by=["supplier"])
    assert caught.value.code == "UNKNOWN_DIMENSION"


def test_category_and_budget_head_cannot_both_be_grouped():
    """AMB-04 reads `category` as the budget head, so the two select ONE
    column. Grouping by both would break a total down twice into identical
    columns, which is a card that looks broken rather than one that is wrong --
    but it is still not what the caller meant, so it is refused."""
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(group_by=["category", "budget_head"])
    assert caught.value.code == "ALIASED_DIMENSION"


def test_category_and_budget_head_resolve_to_the_same_column():
    """The other half of AMB-04: they must be the SAME column, or a card
    grouped by category and one grouped by budget head would disagree."""
    assert (rp.DIMENSIONS["category"].key_sql
            == rp.DIMENSIONS["budget_head"].key_sql)
    assert rp._GROUP_KEY_COLUMN["category"] == rp._GROUP_KEY_COLUMN["budget_head"]


def test_an_inverted_date_range_is_refused_rather_than_answered_empty():
    """An impossible range answered with an empty table reads as "no
    activity", which is a different and wrong sentence."""
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.build(date_from="2026-06-01", date_to="2026-01-01")
    assert caught.value.code == "INVALID_DATE_RANGE"


def test_a_status_no_contract_defines_is_refused():
    with pytest.raises(rp.FilterError):
        rp.FilterSet.build(lifecycle_statuses=["MADE_UP"])
    with pytest.raises(rp.FilterError):
        rp.FilterSet.build(approval_statuses=["MADE_UP"])


def test_c3_and_c15_are_separate_namespaces_on_the_filter_set():
    """C15's own rule: "An approval status must NEVER be rendered on a business
    screen as if it were a C3 status." They are two fields here, and neither
    accepts the other's codes where they do not overlap.

    `OPEN` is C15-only and `RELEASED` is C3-only, so each is rejected by the
    other's field -- which is what proves the two are not one list behind two
    names.
    """
    rp.FilterSet.build(approval_statuses=["OPEN"])          # C15: fine
    with pytest.raises(rp.FilterError):
        rp.FilterSet.build(lifecycle_statuses=["OPEN"])     # C15 code, C3 field
    rp.FilterSet.build(lifecycle_statuses=["RELEASED"])     # C3: fine
    with pytest.raises(rp.FilterError):
        rp.FilterSet.build(approval_statuses=["RELEASED"])  # C3 code, C15 field


def test_lifecycle_codes_are_translated_to_the_labels_the_tables_store():
    """Migration 013 stores C3 LABELS ('Under Review'), the contract's identity
    is the CODE ('UNDER_REVIEW'). A filter keyed on display text breaks the day
    a label is retitled, so the wire takes codes and `_params` translates."""
    params = rp._params(rp.FilterSet.build(lifecycle_statuses=["UNDER_REVIEW"]))
    assert params["lifecycle"] == ["Under Review"]


def test_the_label_map_is_read_from_the_frozen_contract_not_hand_copied():
    frozen = json.loads((CONTRACTS / "C3_statuses.json").read_text(encoding="utf-8"))
    assert rp.C3_LABEL_BY_CODE == {r["code"]: r["label"] for r in frozen["statuses"]}
    assert len(rp.C3_LABEL_BY_CODE) == frozen["count"] == 21


def test_limit_is_bounded_at_both_ends():
    for bad in (0, -1, rp.MAX_LIMIT + 1):
        with pytest.raises(rp.FilterError) as caught:
            rp.FilterSet.build(limit=bad)
        assert caught.value.code == "INVALID_LIMIT"


# ===========================================================================
# The drill-down contract
# ===========================================================================

def test_drilling_in_narrows_and_never_widens():
    """Clicking a segment the total did not include yields NO rows, not that
    segment's rows.

    This is the property that makes "the parts sum to the whole" true. If
    narrowing REPLACED the filter value, drilling into E2 on a report filtered
    to E1 would return E2's rows -- rows the clicked total never counted.
    """
    filters = rp.FilterSet.build(entity_ids=["E1"])
    assert filters.narrowed_to("entity", "E1").entity_ids == ("E1",)
    assert filters.narrowed_to("entity", "E2").entity_ids == ()


def test_drilling_into_an_unfiltered_dimension_pins_it():
    filters = rp.FilterSet.build()
    assert filters.narrowed_to("project", "P9").project_ids == ("P9",)


def test_drilling_carries_every_other_filter_unchanged():
    """"the drill-down carries the SAME `FilterSet` plus the clicked
    dimension" -- so nothing else may move."""
    filters = rp.FilterSet.build(
        project_ids=["P1"], date_from="2026-01-01", date_to="2026-03-31",
        document_types=["PO"], budget_head_ids=["BH1"], limit=25)
    drilled = filters.narrowed_to("entity", "E1")
    for field_name in ("project_ids", "date_from", "date_to",
                       "document_types", "budget_head_ids", "limit"):
        assert getattr(drilled, field_name) == getattr(filters, field_name)


def test_drilling_resets_the_cursor():
    """A cursor is a position in the PARENT query's ordering. Carried into a
    drill-down it resumes a pagination of a different result set, skipping the
    first page of the rows the reader just clicked to see."""
    filters = rp.FilterSet.build(entity_ids=["E1"], cursor=rp.encode_cursor([1]))
    assert filters.narrowed_to("entity", "E1").cursor is None


def test_drilling_into_category_narrows_the_budget_head():
    """AMB-04 again: clicking a "category" segment and clicking the equivalent
    "budget head" segment must produce the SAME rows."""
    filters = rp.FilterSet.build()
    assert filters.narrowed_to("category", "BH1").budget_head_ids == ("BH1",)
    assert (filters.narrowed_to("category", "BH1").budget_head_ids
            == filters.narrowed_to("budget_head", "BH1").budget_head_ids)


def test_drilling_into_wbs_narrows_the_subtree_and_cannot_escape_it():
    """A WBS drill-down is a SUBTREE, not an id. Clicking a path outside the
    subtree already selected yields nothing, for the same non-widening reason
    every other dimension does."""
    filters = rp.FilterSet.build(wbs_paths=["ROOT.A"])
    assert filters.narrowed_to("wbs", "ROOT.A.1").wbs_paths == ("ROOT.A.1",)
    assert filters.narrowed_to("wbs", "ROOT.B").wbs_paths == ()


def test_a_null_group_key_drills_without_inventing_a_sentinel_id():
    """A project with no plant groups under a NULL key. Narrowing on it must
    not invent an id -- a fabricated filter value is a fabricated population."""
    filters = rp.FilterSet.build(plant_ids=["PL1"])
    assert filters.narrowed_to("plant", None).plant_ids == ("PL1",)


# ===========================================================================
# Money
# ===========================================================================

def test_every_derived_money_value_is_int_and_never_float():
    """"Money is integer paise through every aggregation." `SUM()` over
    `bigint` returns numeric and psycopg maps numeric to Decimal, so `derive`
    coerces -- the `::bigint` casts are the first defence and this is the
    second."""
    from decimal import Decimal
    out = rp.derive({"budget": Decimal("1000"), "commitment": Decimal("250"),
                     "actual": Decimal("100"), "pr_reserved": Decimal("50")})
    for name in (*rp.COMPONENTS, "exposure", "available"):
        assert isinstance(out[name], int), f"{name} is {type(out[name])}"
        assert not isinstance(out[name], bool)


def test_derive_matches_domain_derive_exactly():
    """The frozen `C5_formulas.json` registry, transcribed and not re-derived.

    `domain._derive` is the registry in code. Asserting equality against it --
    rather than against a hand-written expectation -- is what makes this a
    transcription check instead of a second opinion.
    """
    for components in (
        {"budget": 10_000_00, "commitment": 3_000_00, "actual": 2_000_00,
         "pr_reserved": 1_000_00},
        {"budget": 0, "commitment": 5_00, "actual": 0, "pr_reserved": 0},
        {"budget": 100_00, "commitment": 90_00, "actual": 5_00},   # critical
        {"budget": 100_00, "commitment": 80_00, "actual": 1_00},   # watch
        {"budget": 100_00, "commitment": 150_00},                  # breach
    ):
        mine = rp.derive(components)
        theirs = domain._derive(dict(components))
        for key in ("exposure", "available", "utilisation_pct", "band"):
            assert mine[key] == theirs[key], (
                f"{key} disagrees with domain._derive for {components}: "
                f"{mine[key]} vs {theirs[key]}")


def test_exposure_is_commitment_plus_actual_plus_reserved_and_nothing_else():
    """`received` and `received_not_billed` are NOT in exposure. Adding either
    would double count: received value is already inside the ordered figure
    commitment is derived from."""
    out = rp.derive({"commitment": 100, "actual": 200, "pr_reserved": 300,
                     "received": 999, "received_not_billed": 888,
                     "ordered": 777})
    assert out["exposure"] == 600


def test_the_thresholds_are_the_frozen_ones():
    assert rp.WATCH_PCT == domain.WATCH_PCT == 80.0
    assert rp.CRITICAL_PCT == domain.CRITICAL_PCT == 90.0


def test_the_bill_states_and_releasing_states_match_domain():
    """Three copies of these tuples exist -- `domain`, `integration_store` and
    this module, which imports one of them. Pinned together so none drifts."""
    assert set(rp.ACCOUNTING_EFFECTIVE_BILL_STATES) == \
        domain.ACCOUNTING_EFFECTIVE_BILL_STATES
    assert set(rp.COMMITMENT_RELEASING_STATES) == \
        domain.COMMITMENT_RELEASING_STATES


def test_the_components_are_domains_components_in_the_same_order():
    assert rp.COMPONENTS == domain._COMPONENTS


# ===========================================================================
# The SQL, read as text
# ===========================================================================

ALL_SQL = "\n".join([
    rp._BUDGET_BRANCH, rp._PO_BRANCH, rp._GRN_BRANCH, rp._BILL_BRANCH,
    rp._PR_BRANCH, rp._OUTER_PREDICATES,
])


def test_open_commitment_is_ordered_less_billed_never_less_received():
    """The defect an adversarial review found twice: two functions in one file
    computing `open_paise` differently. There is one definition here and it is
    the frozen one."""
    assert "GREATEST(0, line.ordered_paise - line.billed_paise)" in rp._PO_BRANCH
    assert "ordered_paise - line.received_paise" not in rp._PO_BRANCH


def test_a_released_purchase_order_holds_no_commitment():
    assert "line.po_status = ANY(%(releasing)s)" in rp._PO_BRANCH
    assert set(rp._params(rp.FilterSet.build())["releasing"]) == \
        {"Cancelled", "Closed"}


def test_received_excludes_void_grns_and_negates_a_reversal():
    """The other found defect: a `received_paise` that counted Void GRNs for
    want of a join to `grn`, and did not negate a reversal."""
    for branch in (rp._PO_BRANCH, rp._GRN_BRANCH):
        assert "JOIN grn g ON g.grn_id = gl.grn_id" in branch
        assert "g.status <> 'Void'" in branch
        assert "-ABS(gl.amount_paise)" in branch


def test_billed_and_actual_include_tax_and_freight_and_negate_a_reversal():
    """AUD-C-004. A `billed_paise` that summed `amount_paise` alone dropped
    non-creditable tax and freight, which ARE part of the billed value
    everywhere else -- so `open_paise` was computed against a smaller order
    than the budget was checked against."""
    for branch in (rp._PO_BRANCH, rp._BILL_BRANCH):
        assert "non_creditable_tax_paise" in branch
        assert "freight_paise" in branch
        assert "accounting_status = 'Reversal'" in branch


def test_only_accounting_effective_bills_move_actual():
    assert "b.accounting_status = ANY(%(effective)s)" in rp._BILL_BRANCH
    assert rp._params(rp.FilterSet.build())["effective"] == ["Approved", "Reversal"]


def test_ordered_includes_tax_and_freight():
    assert ("pl.amount_paise + pl.non_creditable_tax_paise"
            in re.sub(r"\s+", " ", rp._PO_BRANCH))


def test_only_approved_and_effective_budget_lines_create_capacity():
    """AUD-C-005. Draft, Submitted, Rejected, Cancelled and future-dated lines
    create no spending capacity."""
    assert "bl.status = 'Approved'" in rp._BUDGET_BRANCH
    assert "bl.effective_from <= %(as_of)s" in rp._BUDGET_BRANCH
    assert "bl.effective_to IS NULL OR bl.effective_to >= %(as_of)s" \
        in rp._BUDGET_BRANCH


def test_the_budget_branch_does_not_use_the_sqlite_column_name():
    """`domain.compute_ledger` reads `effective_date`; migration 003 created
    `effective_from`/`effective_to` and no such column. Transcribing the SQLite
    name would raise UndefinedColumn in CI and on no workstation here."""
    assert "effective_date" not in ALL_SQL


def test_the_reservation_branch_never_joins_pr_line():
    """Migration 015's grain. A reservation is held per (PR x resolved control
    cell); joining `pr_line` to reach "the request's lines" would multiply each
    cell's single reservation row by the number of lines resolving to it."""
    assert "pr_line" not in rp._PR_BRANCH
    assert "r.state = 'Reserved'" in rp._PR_BRANCH


def test_only_live_reservations_count():
    """A Converted reservation is already PO commitment and a Released one has
    been given back. Counting either alongside `commitment` is the classic
    double count between two of the four buckets."""
    for settled in ("'Converted'", "'Released'", "'Expired'"):
        assert settled not in rp._PR_BRANCH


def test_actual_reads_the_bill_lines_own_cell_not_the_po_lines():
    """A non-PO bill names no PO line -- 013 makes both columns nullable for
    exactly that case -- so routing actuals through `po_line` would drop every
    direct bill from CWIP, silently."""
    assert "bl.wbs_id" in rp._BILL_BRANCH
    assert "JOIN po_line" not in rp._BILL_BRANCH


def test_every_paise_sum_is_cast_to_bigint():
    """`SUM()` over `bigint` returns numeric in PostgreSQL, psycopg maps
    numeric to Decimal, and a Decimal reaching int arithmetic is a defect that
    can only appear against a real server."""
    assert not _uncast_sums(ALL_SQL), \
        f"uncast SUM over paise: {_uncast_sums(ALL_SQL)}"


def _uncast_sums(sql: str) -> list[str]:
    """Every `SUM(...)` whose value reaches Python without a `::bigint`.

    A PAREN-MATCHING SCAN AND NOT A REGEX. The first version of this was
    `SUM\\((?:[^()]|\\([^()]*\\))*\\)(?!::bigint)`, and it reported two
    correctly-cast sums as uncast: the negative lookahead makes the engine
    BACKTRACK to a shorter match ending at an inner `)`, which is of course not
    followed by `::bigint`. A regex that can be satisfied by matching less than
    the whole call cannot answer "is the whole call cast".

    THE CAST NEED NOT SIT ON THE `SUM` ITSELF, and the second version of this
    did not know that -- so it reported the PO branch's two subquery sums, both
    of which are written

        COALESCE((SELECT SUM(...) FROM ...), 0)::bigint

    A `SUM` over `bigint` returns `numeric`; `COALESCE(numeric, 0)` is still
    `numeric`; and the `::bigint` on the COALESCE makes the value PostgreSQL
    hands psycopg a `bigint` all the same. The property this file is defending
    is that no `Decimal` reaches integer arithmetic, and a cast on any
    enclosing expression defends it exactly as well as a cast on the SUM.

    So: the SUM's own close is checked first, then every expression enclosing
    it, outward. An uncast sum inside an uncast wrapper still has no `::bigint`
    at any level and is still reported -- see the tests below, which plant
    both.
    """
    flat = re.sub(r"\s+", " ", sql)
    enclosing: dict[int, tuple[int, ...]] = {}
    stack: list[int] = []
    for index, character in enumerate(flat):
        if character == "(":
            enclosing[index] = tuple(stack)
            stack.append(index)
        elif character == ")" and stack:
            stack.pop()

    def cast_after(open_index: int) -> bool:
        return flat[_paren_match(flat, open_index) + 1:].startswith("::bigint")

    offenders: list[str] = []
    for match in re.finditer(r"\bSUM\(", flat):
        open_index = match.end() - 1
        if cast_after(open_index):
            continue
        if any(cast_after(outer) for outer in enclosing.get(open_index, ())):
            continue
        offenders.append(flat[match.start():_paren_match(flat, open_index) + 1])
    return offenders


def test_the_cast_check_catches_an_uncast_sum():
    """A gate reporting clean proves nothing until it has been shown to fail --
    and this one's first version DID report clean on a real miss, by matching
    less than the whole call."""
    assert _uncast_sums("SELECT SUM(x.amount_paise) FROM t")
    assert not _uncast_sums("SELECT SUM(x.amount_paise)::bigint FROM t")
    assert not _uncast_sums(
        "SELECT SUM(CASE WHEN a THEN -ABS(x.p) ELSE (x.p + x.q) END)::bigint")


def test_the_cast_check_sees_a_cast_on_the_wrapper_and_still_catches_a_missing_one():
    """The two halves of the fix, both planted.

    The PO branch's real shape is the first line: the cast is on the COALESCE
    and the value is a `bigint` regardless. The second line is the same shape
    with the cast REMOVED, and it must still be reported -- a checker that
    learned to look outward and forgot how to fail would be worse than the
    one that cried wolf.
    """
    wrapped = ("COALESCE((SELECT SUM(CASE WHEN g.is_reversal "
               "THEN -ABS(gl.amount_paise) ELSE gl.amount_paise END) "
               "FROM grn_line gl), 0)")
    assert not _uncast_sums(f"{wrapped}::bigint AS received_paise")
    assert _uncast_sums(f"{wrapped} AS received_paise") == [
        "SUM(CASE WHEN g.is_reversal THEN -ABS(gl.amount_paise) "
        "ELSE gl.amount_paise END)"]


def test_each_bucket_filters_on_its_own_document_date():
    """A bill dated in March and the purchase order it bills, raised in
    January, are not the same event. One shared date column would file a
    commitment and its own actual in different periods."""
    assert "bl.effective_from >= %(date_from)s" in rp._BUDGET_BRANCH
    assert "po.ordered_at::date >= %(date_from)s" in rp._PO_BRANCH
    assert "g.received_at::date >= %(date_from)s" in rp._GRN_BRANCH
    assert "b.bill_date >= %(date_from)s" in rp._BILL_BRANCH
    assert "req.requested_at::date >= %(date_from)s" in rp._PR_BRANCH


def test_a_period_is_matched_per_entity_and_not_flattened_to_one_range():
    """`accounting_period` carries `entity_id`, and 001's EXCLUDE constraint
    only stops ONE entity's periods overlapping. Two entities may run different
    calendars, so resolving a period to a single span and applying it
    estate-wide files another entity's March into its own February."""
    for branch in (rp._BUDGET_BRANCH, rp._PO_BRANCH, rp._GRN_BRANCH,
                   rp._BILL_BRANCH, rp._PR_BRANCH):
        assert "ap.entity_id = p.entity_id" in branch
        assert "BETWEEN ap.period_start AND ap.period_end" in branch


def _select_list(sql: str) -> str:
    """A statement's OUTERMOST select list: from its first `SELECT` to the
    `FROM` that closes it at paren depth zero.

    NOT THE WHOLE STATEMENT, which is what the first version of this read, and
    it was wrong about `_PO_BRANCH` for a reason worth recording: that branch
    has an inner subquery whose OWN select list aliases `ordered_paise`,
    `received_paise` and `billed_paise` -- per-PO-line quantities that exist so
    the two `GREATEST(0, a - b)` residuals can be computed per line before
    being summed. They are inputs to the branch's output, not output columns,
    and counting them made a correct branch look like one emitting twelve
    columns for nine buckets.
    """
    flat = re.sub(r"\s+", " ", sql).strip()
    start = flat.index("SELECT") + len("SELECT")
    depth, index = 0, start
    while index < len(flat):
        if flat[index] == "(":
            depth += 1
        elif flat[index] == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\bFROM\b", flat[index:]):
            return flat[start:index]
        index += 1
    return flat[start:]


def test_every_branch_selects_the_columns_in_one_fixed_order():
    """A branch emitting its columns in a different order would write
    `received` into `actual` -- a defect no test of the SQL's shape would
    catch, because both are valid `bigint`s."""
    for branch in (rp._BUDGET_BRANCH, rp._PO_BRANCH, rp._GRN_BRANCH,
                   rp._BILL_BRANCH, rp._PR_BRANCH):
        aliases = re.findall(r"AS (\w+_paise)", _select_list(branch))
        assert aliases == [f"{c}_paise" for c in rp.COMPONENTS], (
            f"branch emits {aliases}")


def test_the_select_list_reader_stops_at_the_branchs_own_from():
    """A gate reporting clean proves nothing until it has been shown to fail.

    The PO branch is the case that broke the first version: its select list
    must end at the `FROM (` that opens the per-line subquery, so the three
    aliases inside that subquery are not read as branch output columns.
    """
    assert _select_list("SELECT a AS x FROM (SELECT b AS y FROM t) s").strip() \
        == "a AS x"
    po = _select_list(rp._PO_BRANCH)
    assert "ordered_paise" in po           # the branch's own output column
    assert "AS billed_paise" not in po     # the subquery's, and only its
    assert "FROM po_line" not in po


def test_the_branches_are_unioned_and_never_joined():
    """A PO line with two receipts and two bill lines produces four rows under
    a join, and every total doubles. That is the classic fan-out and here it
    doubles money."""
    fact = rp._fact_sql(rp.FilterSet.build())
    assert fact.count("UNION ALL") == len(rp.DOCUMENT_TYPES)


def test_selecting_document_types_still_includes_the_budget_denominator():
    """`budget` comes from `budget_line`, which is not a document type. A
    utilisation percentage with no denominator is not a utilisation
    percentage."""
    fact = rp._fact_sql(rp.FilterSet.build(document_types=["BILL"]))
    assert "FROM budget_line bl" in fact
    assert "FROM bill_line bl" in fact
    assert "FROM po_line pl" not in fact


def test_branch_order_does_not_depend_on_the_callers_order():
    """Two `FilterSet`s naming the same types in different orders must produce
    byte-identical SQL, or they produce different plans for one question."""
    assert (rp._fact_sql(rp.FilterSet.build(document_types=["BILL", "PO"]))
            == rp._fact_sql(rp.FilterSet.build(document_types=["PO", "BILL"])))


# ===========================================================================
# Scope
# ===========================================================================

def test_every_scope_mapping_names_all_four_dimensions():
    """`repo.compile_scope` refuses a restriction it cannot express, so an
    omitted dimension raises rather than widening -- but naming all four means
    it never arises, and a waiver is a visible decision rather than an
    omission."""
    for mapping in (rp.SCOPE_COLUMNS, rp.VIEW_SCOPE_COLUMNS,
                    rp.CONNECTION_SCOPE_COLUMNS):
        assert set(mapping) == {"entity", "plant", "location", "project"}


def test_the_fact_mapping_waives_nothing():
    """The fact carries every dimension on every row precisely so that none has
    to be waived."""
    assert all(column is not None for column in rp.SCOPE_COLUMNS.values())


def test_the_view_mapping_waives_exactly_what_017s_policy_waives():
    """The repository predicate and RLS must be the same restriction expressed
    twice, not two restrictions that can drift. 017's policy is
    `capex_scope_permits(entity_id, NULL, NULL, NULL)`."""
    assert rp.VIEW_SCOPE_COLUMNS == {
        "entity": "v.entity_id", "plant": None, "location": None,
        "project": None}
    policy = (MIGRATIONS / "017_reporting.sql").read_text(encoding="utf-8")
    assert "capex_scope_permits(entity_id, NULL, NULL, NULL)" in policy


def test_the_outer_predicate_carries_the_scope_token():
    assert repo.SCOPE_TOKEN in rp._OUTER_PREDICATES


def test_every_statement_in_this_module_carries_the_scope_token():
    """`repo.query` refuses SQL without it, but that is a runtime failure on a
    path a test may not reach. This is the same guarantee at build time, over
    every string literal in the module that is recognisably SQL."""
    tree = ast.parse(REPORTING_PY.read_text(encoding="utf-8"))
    statements = [
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("query", "query_one")
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ]
    assert statements, "no repo.query call with a literal statement was found"
    missing = [s[:70] for s in statements if repo.SCOPE_TOKEN not in s]
    assert not missing, f"statements with no {{scope}} token: {missing}"


def test_a_filter_set_cannot_widen_a_scope():
    """The compiled predicate is ANDed with the FilterSet's own, so naming an
    entity the caller has no grant for can only ever intersect to nothing.

    Asserted at the compiler, not by inspection: a scope restricted to E1
    compiles to a predicate on E1 regardless of what any FilterSet says.
    """
    predicate, params = repo.compile_scope(
        Scope(user_id="U", entity_ids=frozenset({"E1"})), rp.SCOPE_COLUMNS)
    assert "f.entity_id" in predicate
    assert list(params.values())[0] == ["E1"]


def test_a_denied_scope_compiles_to_false_not_true():
    """The inversion the whole scope layer exists to prevent."""
    denied = Scope(user_id="U", entity_ids=frozenset(), plant_ids=frozenset(),
                   location_ids=frozenset(), project_ids=frozenset())
    predicate, _ = repo.compile_scope(denied, rp.SCOPE_COLUMNS)
    assert predicate == "FALSE"


# ===========================================================================
# Pagination
# ===========================================================================

def test_the_ordering_always_ends_with_the_unique_group_key():
    """A sort on a measure alone has ties, and a keyset cursor over a tied
    order skips rows on one page and repeats them on the next -- so the pages
    do not sum to the total the same query reports."""
    filters = rp.FilterSet.build(group_by=["entity", "project"],
                                 sort="commitment", sort_desc=True)
    terms = rp._order_expressions(filters, ["entity_id", "project_id"])
    assert terms[0][0] == "g.commitment_paise"
    assert [expression for expression, _ in terms[1:]] == [
        "COALESCE(g.entity_id, '')", "COALESCE(g.project_id, '')"]


def test_group_keys_are_coalesced_so_a_null_cannot_drop_a_row():
    """A group key is NULL wherever the dimension does not apply -- a project
    with no plant. Any comparison with NULL is NULL, so an uncoalesced key
    drops the row from the resume condition, silently, mid-page."""
    terms = rp._order_expressions(rp.FilterSet.build(group_by=["plant"]),
                                  ["plant_id"])
    assert all("COALESCE" in expression for expression, _ in terms)


def test_an_ungrouped_query_still_has_a_deterministic_order():
    terms = rp._order_expressions(rp.FilterSet.build(), [])
    assert terms == [("1", "ASC")]


def test_the_cursor_predicate_is_expanded_lexicographically():
    """NOT a row-value comparison. `ROW(a,b) > ROW(x,y)` is exact only when
    every column shares one direction, and a report sorted by descending
    commitment with an ascending key tie-breaker mixes them."""
    filters = rp.FilterSet.build(group_by=["entity"], sort="commitment",
                                 sort_desc=True,
                                 cursor=rp.encode_cursor([500, "E1"]))
    params: dict = {}
    terms = rp._order_expressions(filters, ["entity_id"])
    predicate = rp._cursor_predicate(filters, terms, params)
    assert "ROW(" not in predicate
    assert predicate.count(" OR ") == 1
    assert params == {"cursor_0": 500, "cursor_1": "E1"}


def test_the_cursor_comparison_direction_follows_each_terms_direction():
    ascending = rp.FilterSet.build(group_by=["entity"],
                                   cursor=rp.encode_cursor(["E1"]))
    descending = rp.FilterSet.build(group_by=["entity"], sort_desc=True,
                                    cursor=rp.encode_cursor(["E1"]))
    assert ">" in rp._cursor_predicate(
        ascending, rp._order_expressions(ascending, ["entity_id"]), {})
    assert "<" in rp._cursor_predicate(
        descending, rp._order_expressions(descending, ["entity_id"]), {})


def test_no_cursor_is_a_true_predicate_and_not_a_missing_where():
    assert rp._cursor_predicate(rp.FilterSet.build(), [("1", "ASC")], {}) == "TRUE"


def test_a_cursor_from_a_different_sort_is_refused():
    """A cursor is only valid for the query that produced it. Resuming with a
    key of the wrong width would compare the wrong columns."""
    filters = rp.FilterSet.build(group_by=["entity", "project"],
                                 cursor=rp.encode_cursor(["E1"]))
    with pytest.raises(rp.FilterError) as caught:
        rp._cursor_predicate(
            filters, rp._order_expressions(filters, ["entity_id", "project_id"]),
            {})
    assert caught.value.code == "INVALID_CURSOR"


def test_an_unreadable_cursor_is_refused_not_ignored():
    with pytest.raises(rp.FilterError) as caught:
        rp.decode_cursor("!!!not-base64!!!")
    assert caught.value.code == "INVALID_CURSOR"
    assert caught.value.status == 400


def test_a_cursor_round_trips():
    assert rp.decode_cursor(rp.encode_cursor([500, "E1", ""])) == [500, "E1", ""]


# ===========================================================================
# Saved views
# ===========================================================================

def test_a_saved_view_never_stores_a_cursor():
    """A cursor is a position in one reader's pagination of one execution.
    Stored, it makes a saved view open on page four of a result set that no
    longer exists."""
    assert "cursor" not in rp._SERIALISED_FIELDS
    stored = rp.FilterSet.build(group_by=["entity"],
                                cursor=rp.encode_cursor(["E1"])).to_json()
    assert "cursor" not in stored


def test_a_filter_set_round_trips_through_a_saved_definition():
    original = rp.FilterSet.build(
        entity_ids=["E1"], project_ids=["P1"], group_by=["entity", "project"],
        date_from="2026-04-01", date_to="2026-06-30",
        document_types=["PO", "BILL"], sort="commitment", sort_desc=True,
        limit=50)
    assert rp.FilterSet.from_json(original.to_json()) == original


def test_a_definition_naming_an_unknown_filter_is_refused_not_partly_applied():
    """A view whose NAME promises filters it does not apply is worse than one
    that will not open: the reader believes the filters are in force and reads
    a wider population than they asked for."""
    with pytest.raises(rp.FilterError) as caught:
        rp.FilterSet.from_json({"entity_ids": ["E1"], "supplier_ids": ["V1"]})
    assert caught.value.code == "MALFORMED_VIEW_DEFINITION"
    assert "supplier_ids" in caught.value.message


def test_an_empty_definition_is_a_valid_unfiltered_view():
    assert rp.FilterSet.from_json({}) == rp.FilterSet.build()


def test_a_view_not_found_and_one_not_permitted_are_one_exception():
    """An error that separates them confirms an id is real to a caller who was
    refused it."""
    assert issubclass(rp.ViewNotFound, rp.FilterError)
    assert rp.ViewNotFound("RV-1").status == 404
    assert rp.EntityNotInScope("E-1").status == 404


def test_every_report_key_matches_the_migrations_shape_constraint():
    """017's `ck_report_saved_view_key_shape` is the database's half; the
    allow-list is the application's. A key the application permits and the
    database rejects is a 500 at the moment of saving."""
    pattern = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
    for key in rp.REPORT_KEYS:
        assert pattern.match(key), key


# ===========================================================================
# THE COLUMN CHECK -- the sibling agent's defect, mechanised
# ===========================================================================

def _columns_by_table() -> dict[str, set[str]]:
    """Every column of every table migrations 001..017 create or alter.

    Read from the migration text, which is the only source that cannot be
    wrong about the schema. Parsing is deliberately generous -- it over-collects
    rather than under-collects -- because a false negative here (a real column
    this parser missed) makes the test fail loudly and get fixed, while a false
    positive (a column it invented) would let a typo through, which is the
    defect this exists to catch.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")))
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
            r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);",
            text, re.S):
        columns: set[str] = set()
        for line in match.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            name = re.match(r"(\w+)\s+[a-z]", line)
            if name and name.group(1).upper() not in {
                    "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK",
                    "EXCLUDE"}:
                columns.add(name.group(1))
        tables[match.group(1)] = columns
    for table, column in re.findall(
            r"ALTER TABLE (\w+)\s+ADD COLUMN (?:IF NOT EXISTS )?(\w+)", text):
        tables.setdefault(table, set()).add(column)
    return tables


#: Aliases a FRAGMENT inherits from the statement that pastes it in.
#:
#: `_period_on()` and the label JOINs are not statements. They are pasted into
#: one, and the alias each correlates on is bound by the statement doing the
#: pasting -- so their binding cannot be derived from the fragment's own text
#: and is the one thing here still written by hand.
#:
#: `None` means "a derived table of this module's own making": `f` is the
#: unioned fact and `g` the `grouped` CTE, whose columns ARE this module's own
#: SELECT lists and are covered by
#: `test_every_branch_selects_the_columns_in_one_fixed_order`.
_INHERITED_ALIASES: dict[str, str | None] = {
    "p": "project",
    "f": None,
    "g": None,
}

#: Words that follow `FROM`/`JOIN`/`UPDATE`/`USING` in the grammar and are
#: never the alias of the thing before them.
_NEVER_AN_ALIAS = frozenset({
    "as", "on", "set", "where", "group", "order", "join", "left", "right",
    "full", "inner", "outer", "cross", "lateral", "union", "select", "using",
    "returning", "and", "or", "limit", "offset", "values", "conflict", "do",
    "nothing", "update", "from", "into", "with", "having", "window", "for",
})

_BINDS = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|USING)\s+([a-z_][a-z0-9_]*)\s+(?:AS\s+)?"
    r"([a-z][a-z0-9_]*)\b", re.IGNORECASE)
_CTE_NAME = re.compile(r"(?:\bWITH|,)\s+([a-z_][a-z0-9_]*)\s+AS\s*\(",
                       re.IGNORECASE)


def _paren_match(text: str, open_index: int) -> int:
    """The index of the `)` closing the `(` at `open_index`."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def _alias_bindings(statement: str) -> tuple[dict[str, str], set[str]]:
    """What one statement binds each of its aliases to: `(base, derived)`.

    DERIVED PER STATEMENT, WHICH IS THE WHOLE POINT OF THIS FILE'S REWRITE. An
    alias means whatever its own statement says it means: `bl` is
    `budget_line` in the budget branch and `bill_line` in the bill branch;
    `pl` is `po_line` in the PO branch and `plant` in the label JOINs; `g` is
    `grn` in two branches and the `grouped` CTE in the aggregate; `v` is
    `report_saved_view` in five statements and `entity` in `save_view`'s
    INSERT. One module-wide map was wrong about every one of them, and its
    wrongness read as a schema error rather than as a parsing error.
    """
    flat = re.sub(r"\s+", " ", statement)
    ctes = {match.group(1).lower() for match in _CTE_NAME.finditer(flat)}
    base: dict[str, str] = {}
    derived: set[str] = set()
    for match in _BINDS.finditer(flat):
        table, alias = match.group(1), match.group(2)
        if alias.lower() in _NEVER_AN_ALIAS:
            continue
        if table.lower() in ctes:
            derived.add(alias)
        else:
            base[alias] = table
    # A derived table -- `(SELECT ...) AS line` -- and a set-returning
    # function in the FROM list -- `FROM unnest(...) AS q(path)` -- both name
    # something whose columns this statement defines itself.
    for pattern, opens_at in ((r"\(\s*SELECT\b", 0),
                              (r"\bFROM\s+[a-z_][a-z0-9_]*\s*\(", -1)):
        for match in re.finditer(pattern, flat, re.IGNORECASE):
            start = match.start() if opens_at == 0 else match.end() - 1
            tail = re.match(r"\)\s*(?:AS\s+)?([a-z][a-z0-9_]*)\b",
                            flat[_paren_match(flat, start):], re.IGNORECASE)
            if tail and tail.group(1).lower() not in _NEVER_AN_ALIAS:
                derived.add(tail.group(1))
    return base, derived


def _sql_statements() -> list[str]:
    """Every SQL statement in `reporting.py`, ONE STRING PER STATEMENT.

    Not one string for the whole module: an alias is only meaningful inside
    the statement that binds it (see `_alias_bindings`).

    DOCSTRINGS ARE EXCLUDED. `app.backend`, `pg.repo` and `pg.engine` are
    prose, not column references, and the first version of this reported all
    three as unknown aliases -- a parsing failure wearing the costume of a
    schema failure, which is the most expensive kind of false alarm a gate
    like this can raise.

    An f-string is rendered by concatenating its literal parts with the string
    constants of whatever it interpolates, in source order, so a branch and
    the `_zeros_except(...)` expressions built into it remain ONE statement.

    `--` comments are stripped for the same reason docstrings are: the PO
    branch's comment says "`domain.compute_ledger`, verbatim", and
    `domain.compute_ledger` is not a column reference either.
    """
    tree = ast.parse(REPORTING_PY.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }

    def literals(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                return [node.value]
            return []
        parts: list[str] = []
        for child in ast.iter_child_nodes(node):
            parts.extend(literals(child))
        return parts

    def is_sql(text: str) -> bool:
        return bool(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b",
                              text))

    statements: list[str] = []

    def keep(text: str) -> None:
        text = re.sub(r"--[^\n]*", " ", text)
        if is_sql(text):
            statements.append(text)

    def collect(node: ast.AST) -> None:
        if isinstance(node, ast.JoinedStr):
            keep("\n".join(literals(node)))
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                keep(node.value)
            return
        for child in ast.iter_child_nodes(node):
            collect(child)

    collect(tree)
    return statements


def test_the_statements_are_read_one_at_a_time_and_not_as_one_blob():
    """The premise of the alias derivation, pinned so it cannot quietly
    regress into a single module-wide scan."""
    statements = _sql_statements()
    assert len(statements) > 10
    bindings = [_alias_bindings(statement)[0] for statement in statements]

    def bound(alias: str) -> set[str]:
        return {b[alias] for b in bindings if alias in b}

    # The three aliases a module-wide map got wrong. Each means two things.
    assert bound("bl") == {"budget_line", "bill_line"}
    assert bound("pl") == {"po_line", "plant"}
    assert bound("v") == {"report_saved_view", "entity"}


def test_the_inherited_aliases_name_only_tables_that_exist():
    tables = _columns_by_table()
    unknown = sorted(t for t in _INHERITED_ALIASES.values()
                     if t is not None and t not in tables)
    assert not unknown, f"inherited map names tables no migration creates: {unknown}"


def test_every_column_this_module_names_exists_in_a_migration():
    """THE CHECK A SIBLING AGENT NEEDED AND DID NOT HAVE.

    That agent shipped `INSERT INTO wbs_element (... code, name ...)` when the
    columns are `wbs_code` and `description`. All 27 of its live tests skipped
    on every workstation and would have errored in CI, so the mistake was
    invisible exactly where it was made.

    There is no PostgreSQL here either. This reads the migrations and checks
    every `alias.column` reference in this module's SQL against them -- so a
    remembered column name fails on the machine that wrote it, not three hours
    later in someone else's pipeline.
    """
    bad = _unknown_columns(_sql_statements())
    assert not bad, (
        "SQL names columns the migrations do not create:\n  "
        + "\n  ".join(sorted(set(bad))))


def _unknown_columns(statements: Sequence[str]) -> list[str]:
    """Every `alias.column` in `statements` that its own statement's tables
    do not have."""
    tables = _columns_by_table()
    bad: list[str] = []
    for statement in statements:
        base, derived = _alias_bindings(statement)
        flat = re.sub(r"\s+", " ", re.sub(r"--[^\n]*", " ", statement))
        for alias, column in re.findall(
                r"\b([a-z][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", flat):
            if alias in base:
                table: str | None = base[alias]
            elif alias in derived:
                continue
            elif alias in _INHERITED_ALIASES:
                table = _INHERITED_ALIASES[alias]
                if table is None:
                    continue
            else:
                bad.append(
                    f"{alias}.{column} -- nothing in this statement binds "
                    f"{alias!r}, and it is not an inherited alias")
                continue
            if column not in tables.get(table, set()):
                bad.append(f"{alias}.{column} -- {table} has no column {column!r}")
    return bad


def test_the_column_check_would_catch_the_sibling_agents_mistake():
    """A gate reporting clean proves nothing until it has been shown to fail.

    This plants the exact defect -- `wbs_element.code`, which does not exist,
    where `wbs_code` does -- and asserts the checker reports it.
    """
    tables = _columns_by_table()
    assert "code" not in tables["wbs_element"], (
        "the premise of this test is that wbs_element has no `code` column")
    assert "wbs_code" in tables["wbs_element"]
    assert "description" in tables["wbs_element"]
    assert "name" not in tables["wbs_element"]

    planted = "SELECT w.code, w.name FROM wbs_element w"
    assert _unknown_columns([planted]) == [
        "w.code -- wbs_element has no column 'code'",
        "w.name -- wbs_element has no column 'name'"]
    assert not _unknown_columns(
        ["SELECT w.wbs_code, w.description FROM wbs_element w"])


def test_the_column_check_reads_each_statement_in_its_own_alias_frame():
    """The failure mode the module-wide map HAD: `bl.bill_id` is a real column
    of `bill_line` and not of `budget_line`, so one map has to call one of the
    two statements wrong -- and it called the correct one a schema error."""
    assert not _unknown_columns(
        ["SELECT bl.bill_id FROM bill_line bl JOIN bill b ON b.bill_id = bl.bill_id"])
    assert _unknown_columns(["SELECT bl.bill_id FROM budget_line bl"]) == [
        "bl.bill_id -- budget_line has no column 'bill_id'"]


def test_the_column_check_refuses_an_alias_nothing_binds():
    """A typo'd alias is skipped by no rule here: it is reported. The
    alternative -- skipping what the parser cannot resolve -- is how a gate
    goes quiet."""
    assert _unknown_columns(["SELECT zz.wbs_id FROM wbs_element w"]) == [
        "zz.wbs_id -- nothing in this statement binds 'zz', "
        "and it is not an inherited alias"]


def test_migration_017_creates_exactly_the_two_tables_this_module_reads():
    tables = _columns_by_table()
    for table in ("report_saved_view", "report_view_default"):
        assert table in tables, f"017 does not create {table}"
    assert "definition" in tables["report_saved_view"]
    assert "visibility" in tables["report_saved_view"]
    assert "owner_user_id" in tables["report_saved_view"]


def test_migration_017_names_every_constraint_it_creates():
    """An anonymous CHECK has no `pg_constraint.conname` a later migration or
    a test can name, so it can neither be asserted nor dropped."""
    text = (MIGRATIONS / "017_reporting.sql").read_text(encoding="utf-8")
    checks = re.findall(r"CONSTRAINT (\w+) CHECK", text)
    assert "ck_report_saved_view_visibility" in checks
    assert "ck_report_saved_view_name_not_blank" in checks
    assert "ck_report_saved_view_key_shape" in checks
    assert "ck_report_saved_view_definition_object" in checks
    assert re.search(r"CONSTRAINT pk_report_view_default PRIMARY KEY", text)


def test_migration_017_forces_row_level_security_on_both_tables():
    """`ENABLE` alone does not apply to the table's owner. Every other
    migration here pairs it with `FORCE`, and a table with one and not the
    other is protected against everyone except the role most able to read it."""
    text = (MIGRATIONS / "017_reporting.sql").read_text(encoding="utf-8")
    for table in ("report_saved_view", "report_view_default"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in text


def test_017s_write_check_is_narrower_than_its_read_predicate():
    """A principal may READ a shared view somebody else authored and may WRITE
    only their own. Without the asymmetry any principal in the entity could
    UPDATE a shared view, silently editing the filters under everyone using
    it."""
    text = (MIGRATIONS / "017_reporting.sql").read_text(encoding="utf-8")
    policy = text.split("CREATE POLICY report_saved_view_scope", 1)[1]
    using, check = policy.split("WITH CHECK", 1)
    assert "visibility = 'SHARED'" in using, "the read side admits shared views"
    assert "visibility" not in check.split(";", 1)[0], (
        "the write side must not admit a view on the strength of it being "
        "shared")
    assert "owner_user_id" in check.split(";", 1)[0]


def test_the_default_table_reaches_its_dimension_through_an_exists():
    """`report_view_default` carries no dimension column. An all-NULL
    `capex_scope_permits(NULL, NULL, NULL, NULL)` call would be literally TRUE
    for every row -- the fail-open shape 006's header names."""
    text = (MIGRATIONS / "017_reporting.sql").read_text(encoding="utf-8")
    policy = text.split("CREATE POLICY report_view_default_scope", 1)[1]
    assert "capex_scope_permits(NULL" not in policy
    assert "EXISTS (SELECT 1 FROM report_saved_view v" in policy


# ===========================================================================
# The four states
# ===========================================================================

def test_the_empty_and_denied_states_are_different_values():
    """"Zero data, denied scope, stale data and service failure are four
    distinct states. Never collapse them." They are four different sentences on
    screen, which is only possible if they are four different values here."""
    empty = rp._empty_result(rp.FilterSet.build(), state="empty")
    denied = rp._empty_result(rp.FilterSet.build(), state="denied")
    assert empty["state"] != denied["state"]
    assert empty["rows"] == denied["rows"] == []


def test_an_empty_result_still_carries_a_zeroed_total_not_a_missing_one():
    """A missing total renders as a blank where a number belongs, which reads
    as a failure. Zero over an empty population is the honest figure."""
    empty = rp._empty_result(rp.FilterSet.build(), state="empty")
    assert empty["totals"]["budget"] == 0
    assert empty["totals"]["available"] == 0
    assert isinstance(empty["totals"]["budget"], int)


def test_freshness_distinguishes_local_from_never_synced():
    """Three answers, not two. "No connector supplies this" is COMPLETE;
    "a connector exists but has never polled" is STALE, and rendering the
    second as the first shows absent data as up to date."""
    source = REPORTING_PY.read_text(encoding="utf-8")
    for state in ('"state": "local"', '"state": "never_synced"',
                  '"state": "synced"'):
        assert state in source


def test_only_message_ids_present_in_c10_are_emitted():
    """Do not invent a `message_id` absent from C10_messages.json."""
    frozen = json.loads(
        (CONTRACTS / "C10_messages.json").read_text(encoding="utf-8"))
    known = set(re.findall(r"MSG-[A-Z]+-\d+", json.dumps(frozen)))
    router = (ROOT / "app" / "backend" / "api" / "reports.py").read_text(
        encoding="utf-8")
    used = set(re.findall(r'"(MSG-[A-Z]+-\d+)"', router))
    assert used, "the router emits no message id at all"
    assert used <= known, f"message ids absent from C10: {sorted(used - known)}"


def test_the_empty_state_uses_the_frozen_no_match_message():
    from app.backend.api import reports as reports_api
    assert reports_api.MSG_NO_MATCH == "MSG-GEN-002"


# ===========================================================================
# The router's own shape
# ===========================================================================

def test_the_query_routes_are_get_so_a_drill_down_has_an_address():
    from app.backend.api import reports as reports_api
    methods = {
        (route.path, method)
        for route in reports_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/reports/metrics", "GET") in methods
    assert ("/api/reports/drill-down", "GET") in methods
    assert not any(path.endswith(("metrics", "drill-down")) and method == "POST"
                   for path, method in methods)


def test_the_router_declares_an_authentication_floor_on_every_route():
    """Declared on the ROUTER, so a route added later cannot ship unguarded by
    omission -- the failure `api/budget.py`'s docstring records the audit
    router shipping with."""
    from app.backend.api import reports as reports_api
    assert reports_api.router.dependencies, "the router has no floor at all"
    assert reports_api.ROUTER_FLOOR == "budget.read"


def test_the_refused_filters_are_accepted_as_parameters_so_they_can_refuse():
    """Omitting them from the signature would make FastAPI drop them silently,
    which is exactly the failure the refusal exists to prevent."""
    import inspect

    from app.backend.api import reports as reports_api
    parameters = inspect.signature(reports_api._common_filters).parameters
    for field_name in rp.UNSUPPORTED_FILTERS:
        assert field_name in parameters, (
            f"{field_name} is not a query parameter, so it would be ignored "
            f"rather than refused")


def test_the_dimensions_route_publishes_the_refusals():
    """A dashboard that learns `vendor_ids` is unavailable only by sending one
    and getting a 422 will offer the control anyway and fail at the moment of
    use."""
    from app.backend.api import reports as reports_api
    published = reports_api.get_dimensions()
    codes = {row["code"] for row in published["unavailable_filters"]}
    assert codes == {spec.code for spec in rp.UNSUPPORTED_FILTERS.values()}
    assert published["state"] == "ok"
