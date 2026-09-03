"""The approval engine's decidable logic, tested without a database.

Wave 4 stream 2 (M4b). Specification: ``docs/WAVE4_CONTRACTS.md`` contracts 2,
5, 7, 8 and 9, and ``docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`` section
9.

Everything here runs unconditionally, on every machine, with no PostgreSQL.
That is not a convenience -- it is where the value is. The failures this engine
must never have are almost all arithmetic or ordering failures:

  * a routing threshold compared through a float, so ``amount_paise > 5000000.0``
    silently reroutes a purchase order;
  * a quorum over an empty approver set rounding down to zero, which is
    auto-approval wearing a percentage sign;
  * the contributor filter running BEFORE delegates are added, so a maker's
    delegate approves the maker's own document;
  * an idempotent replay applying a decision twice.

None of those needs a server to demonstrate, and every one of them would have
been invisible in a suite that skipped locally. The live counterparts --
blocking, deadlock-freedom, two approvers racing one stage -- are in
``tests/test_pg_approval_concurrency.py``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.backend.pg import approvals as engine
from app.backend.pg import approval_rules as rules
from app.backend.pg import delegation as deleg
from app.backend.pg.engine import Scope

UTC = timezone.utc


# ==========================================================================
# Helpers
# ==========================================================================
def cmp_(op: str, left, right):
    """A comparison node: `left` and `right` are given as ('path'|'value', x)."""
    def operand(spec):
        kind, value = spec
        return {"path": value} if kind == "path" else {"value": value}
    return {"op": op, "left": operand(left), "right": operand(right)}


def path(name):
    return ("path", name)


def val(value):
    return ("value", value)


def definition(definition_id, *, code="APDEF", version=1, status=rules.DEF_ACTIVE,
               object_type="PO_AMEND", entity_id=None, effective_from=None,
               effective_to=None, rules_=()):
    return rules.build_definition({
        "definition_id": definition_id, "object_type": object_type, "code": code,
        "version": version, "status": status, "entity_id": entity_id,
        "effective_from": effective_from, "effective_to": effective_to,
        "rules": [{"priority": p, "predicate": pred} for p, pred in rules_],
    })


def stage(stage_no, **kwargs):
    kwargs.setdefault("quorum_type", rules.QUORUM_ALL)
    return rules.build_stage({"stage_no": stage_no, **kwargs})


def assignees(*user_ids, via=rules.VIA_ROLE):
    return [rules.Assignee(user_id=u, assigned_via=via) for u in user_ids]


SNAPSHOT = {
    "object_type": "PO_AMEND",
    "delta_paise": 18_000_000,
    "amount_paise": 40_000_000,
    "entity_id": "ENT-01",
    "plant_id": "PLT-02",
    "project_id": "PRJ-01",
    "location_id": "LOC-07",
    "asset_category": "PLANT",
    "budget_head_id": "BH-CIVIL",
    "check_verdict": "EXCEEDS_BUDGET",
}


# ==========================================================================
# The predicate AST -- operators
# ==========================================================================
class TestPredicateOperators:
    @pytest.mark.parametrize("op,left,right,expected", [
        ("==", 5, 5, True), ("==", 5, 6, False),
        ("!=", 5, 6, True), ("!=", 5, 5, False),
        ("<", 4, 5, True), ("<", 5, 5, False),
        ("<=", 5, 5, True), ("<=", 6, 5, False),
        (">", 6, 5, True), (">", 5, 5, False),
        (">=", 5, 5, True), (">=", 4, 5, False),
    ])
    def test_every_comparison_operator_evaluates(self, op, left, right, expected):
        predicate = rules.compile_predicate(cmp_(op, val(left), val(right)))
        assert predicate.evaluate({}) is expected

    def test_in_and_not_in_test_membership_of_a_list(self):
        member = rules.compile_predicate(
            cmp_("in", path("asset_category"), val(["LAND", "PLANT"])))
        absent = rules.compile_predicate(
            cmp_("not_in", path("asset_category"), val(["LAND", "BUILDING"])))
        assert member.evaluate(SNAPSHOT) is True
        assert absent.evaluate(SNAPSHOT) is True

    def test_a_string_literal_on_the_right_of_in_is_refused_at_compile_time(self):
        """`'LAND' in 'HIGHLANDS'` is True in Python and means nothing here.

        A predicate that names a string where a list belongs would otherwise
        match on an accident of spelling -- the kind of routing bug nobody finds
        until an auditor asks why a land purchase went down the plant route. A
        string literal is caught by the compiler, which is the earliest and
        loudest place to catch it.
        """
        with pytest.raises(rules.PredicateError) as excinfo:
            rules.compile_predicate(cmp_("in", val("LAND"), val("HIGHLANDS")))
        assert "needs a list" in str(excinfo.value)

    def test_a_path_resolving_to_a_string_is_not_substring_matched_at_runtime(self):
        """The compiler cannot know what a PATH will resolve to, so the
        evaluator carries the same rule: a string is not a container here."""
        predicate = rules.compile_predicate(
            cmp_("in", val("LAND"), path("category_list")))
        assert predicate.evaluate({"category_list": "HIGHLANDS"}) is False
        assert predicate.evaluate({"category_list": ["LAND", "PLANT"]}) is True

    def test_and_or_not_compose(self):
        predicate = rules.compile_predicate({
            "op": "and",
            "args": [
                cmp_(">=", path("delta_paise"), val(10_000_000)),
                {"op": "not", "args": [
                    cmp_("==", path("asset_category"), val("LAND"))]},
                {"op": "or", "args": [
                    cmp_("==", path("entity_id"), val("ENT-99")),
                    cmp_("==", path("entity_id"), val("ENT-01"))]},
            ],
        })
        assert predicate.evaluate(SNAPSHOT) is True

    def test_bare_booleans_and_const_ops_are_catch_all_rules(self):
        """Section 9.3's `p30 true -> STANDARD` needs an always-true rule."""
        assert rules.compile_predicate(True).evaluate({}) is True
        assert rules.compile_predicate(False).evaluate({}) is False
        assert rules.compile_predicate({"op": "true"}).evaluate({}) is True
        assert rules.compile_predicate({"op": "false"}).evaluate({}) is False

    def test_a_boolean_never_equals_the_integer_beside_it(self):
        """`True == 1` is Python's rule, not this domain's."""
        predicate = rules.compile_predicate(cmp_("==", val(True), val(1)))
        assert predicate.evaluate({}) is False

    def test_an_unorderable_comparison_is_false_not_an_exception(self):
        """A misconfigured `<` must not become a 500 mid-route."""
        predicate = rules.compile_predicate(
            cmp_("<", path("asset_category"), val(5)))
        assert predicate.evaluate(SNAPSHOT) is False


# ==========================================================================
# The predicate AST -- money discipline (the headline rule)
# ==========================================================================
class TestMoneyOperandsAreIntegerPaiseOnly:
    """Section 9.2: "the compiler rejects a float literal in a `*_paise`
    comparison, mirroring `to_paise`".

    `money.to_paise` refuses a float that is not an exact 2dp amount. Here the
    value is ALREADY in paise -- an integer count of the smallest unit -- so
    there is no exact float to accept at all, and the rule is absolute.
    """

    @pytest.mark.parametrize("bad", [5_000_000.0, 0.1, 50000.50, -3.5])
    @pytest.mark.parametrize("op", ["==", "!=", "<", "<=", ">", ">="])
    def test_a_float_against_a_paise_path_is_refused_at_compile_time(self, bad, op):
        with pytest.raises(rules.PredicateError) as excinfo:
            rules.compile_predicate(cmp_(op, path("delta_paise"), val(bad)))
        assert "integer-paise" in str(excinfo.value)

    def test_the_refusal_holds_with_the_paise_path_on_either_side(self):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(cmp_("<", val(5_000_000.0), path("delta_paise")))

    def test_a_numeric_string_against_a_paise_path_is_refused(self):
        """`amount_paise > '9'` orders lexicographically and is false up to
        ten crore. A string is not a number of paise."""
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(cmp_(">", path("amount_paise"), val("9")))

    def test_a_boolean_against_a_paise_path_is_refused(self):
        """`True` is an `int` in Python. It is not a number of paise."""
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(cmp_("==", path("amount_paise"), val(True)))

    def test_a_float_inside_an_in_list_is_refused_element_by_element(self):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(
                cmp_("in", path("delta_paise"), val([1_000_000, 2_000_000.0])))

    def test_an_integer_against_a_paise_path_compiles_and_evaluates(self):
        predicate = rules.compile_predicate(
            cmp_(">=", path("delta_paise"), val(10_000_000)))
        assert predicate.evaluate(SNAPSHOT) is True

    def test_paise_path_against_paise_path_is_permitted(self):
        predicate = rules.compile_predicate(
            cmp_("<", path("delta_paise"), path("amount_paise")))
        assert predicate.evaluate(SNAPSHOT) is True

    def test_a_float_against_a_non_money_path_is_still_allowed(self):
        """The rule is about money, not about floats. A percentage threshold is
        a legitimate float and must not be caught by a blanket ban."""
        predicate = rules.compile_predicate(
            cmp_(">", path("utilisation_pct"), val(92.5)))
        assert predicate.evaluate({"utilisation_pct": 95.0}) is True

    def test_the_money_rule_follows_the_suffix_not_a_hand_kept_registry(self):
        assert rules.is_money_path("delta_paise")
        assert rules.is_money_path("line.amount_paise")
        assert not rules.is_money_path("amount")
        assert not rules.is_money_path("paise_note")


# ==========================================================================
# The predicate AST -- what it refuses to be
# ==========================================================================
class TestPredicateIsConfigurationNotCode:
    @pytest.mark.parametrize("node", [
        {"op": "exec", "left": {"value": 1}, "right": {"value": 1}},
        {"op": "regex", "left": {"path": "a"}, "right": {"value": ".*"}},
        {"op": "call", "args": []},
    ])
    def test_an_unknown_operator_is_refused(self, node):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(node)

    def test_a_path_cannot_reach_an_attribute(self):
        """The evaluator indexes mappings and never calls `getattr`, so a dunder
        path resolves to nothing. It is refused outright as well, so that the
        intent is recorded rather than quietly neutralised."""
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(
                cmp_("==", path("__class__.__mro__"), val("x")))

    def test_a_path_over_a_non_mapping_yields_missing_rather_than_an_attribute(self):
        class Sneaky:
            secret = "leaked"

        predicate = rules.compile_predicate(cmp_("==", path("obj.secret"), val("leaked")))
        assert predicate.evaluate({"obj": Sneaky()}) is False

    def test_an_operand_must_be_exactly_one_of_path_or_value(self):
        for operand in ({"path": "a", "value": 1}, {}, {"nope": 1}):
            with pytest.raises(rules.PredicateError):
                rules.compile_predicate({"op": "==", "left": operand,
                                          "right": {"value": 1}})

    def test_a_bare_literal_operand_is_refused(self):
        """'is this a field name or a string?' must never be a guess."""
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate({"op": "==", "left": "entity_id",
                                      "right": {"value": "ENT-01"}})

    def test_not_takes_exactly_one_argument(self):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate({"op": "not", "args": [True, True]})

    def test_empty_and_or_are_refused_rather_than_vacuously_true(self):
        for op in ("and", "or"):
            with pytest.raises(rules.PredicateError):
                rules.compile_predicate({"op": op, "args": []})

    def test_an_unexpected_key_is_refused_because_it_is_usually_a_typo(self):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate({"op": "and", "args": [True], "arg": [False]})

    def test_a_non_json_literal_is_refused(self):
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(cmp_("==", path("a"), val({"nested": 1})))

    def test_runaway_nesting_is_refused_rather_than_exhausting_the_stack(self):
        node = True
        for _ in range(80):
            node = {"op": "not", "args": [node]}
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate(node)


class TestMissingPathsFailClosed:
    def test_every_leaf_comparison_against_a_missing_path_is_false(self):
        """Including `!=` and `not_in`, where the intuitive answer is the
        dangerous one: a rule must match on what a document SAYS, never on what
        it omits, or a snapshot with a field missing routes itself down a
        cheaper path."""
        for op, right in (("==", val("X")), ("!=", val("X")), ("<", val(1)),
                          ("<=", val(1)), (">", val(1)), (">=", val(1)),
                          ("in", val(["X"])), ("not_in", val(["X"]))):
            predicate = rules.compile_predicate(cmp_(op, path("nope.not.here"), right))
            assert predicate.evaluate(SNAPSHOT) is False, op

    def test_a_present_null_is_not_the_same_as_an_absent_field(self):
        predicate = rules.compile_predicate(cmp_("==", path("note"), val(None)))
        assert predicate.evaluate({"note": None}) is True
        assert predicate.evaluate({}) is False

    def test_resolve_path_walks_nested_mappings(self):
        assert rules.resolve_path({"a": {"b": {"c": 7}}}, "a.b.c") == 7
        assert rules.resolve_path({"a": {"b": {}}}, "a.b.c") is rules.MISSING


class TestConditionsCoverEveryRequiredDimension:
    """Section 9.2: conditions must cover amount, entity, plant, project,
    location, category, budget head and document type."""

    @pytest.mark.parametrize("dimension", sorted(rules.CONDITION_DIMENSIONS))
    def test_each_dimension_can_be_expressed_and_evaluated(self, dimension):
        field = rules.CONDITION_DIMENSIONS[dimension]
        expected = SNAPSHOT[field]
        node = (cmp_(">=", path(field), val(expected))
                if rules.is_money_path(field)
                else cmp_("==", path(field), val(expected)))
        predicate = rules.compile_predicate(node)
        assert predicate.evaluate(SNAPSHOT) is True
        assert rules.covered_condition_dimensions(node) == {dimension}

    def test_all_eight_dimensions_are_declared(self):
        assert set(rules.CONDITION_DIMENSIONS) == {
            "amount", "entity", "plant", "project", "location", "category",
            "budget_head", "document_type"}


# ==========================================================================
# Route resolution
# ==========================================================================
class TestCandidateSelection:
    def test_only_active_definitions_are_candidates(self):
        for status in (rules.DEF_DRAFT, rules.DEF_RETIRED):
            kept = rules.candidate_definitions(
                [definition("D1", status=status, rules_=[(10, True)])],
                object_type="PO_AMEND", business_date=date(2026, 6, 1), entity_id="ENT-01")
            assert kept == []

    def test_object_type_must_match(self):
        kept = rules.candidate_definitions(
            [definition("D1", object_type="PR", rules_=[(10, True)])],
            object_type="PO_AMEND", business_date=date(2026, 6, 1), entity_id="ENT-01")
        assert kept == []

    def test_business_date_must_fall_inside_effectivity(self):
        d = definition("D1", effective_from=date(2026, 4, 1),
                       effective_to=date(2027, 3, 31), rules_=[(10, True)])
        assert rules.candidate_definitions(
            [d], object_type="PO_AMEND", business_date=date(2026, 3, 31),
            entity_id=None) == []
        assert rules.candidate_definitions(
            [d], object_type="PO_AMEND", business_date=date(2027, 4, 1),
            entity_id=None) == []
        assert len(rules.candidate_definitions(
            [d], object_type="PO_AMEND", business_date=date(2026, 4, 1),
            entity_id=None)) == 1

    def test_effective_to_is_inclusive_so_the_last_day_still_routes(self):
        d = definition("D1", effective_from=date(2026, 4, 1),
                       effective_to=date(2027, 3, 31), rules_=[(10, True)])
        assert len(rules.candidate_definitions(
            [d], object_type="PO_AMEND", business_date=date(2027, 3, 31),
            entity_id=None)) == 1

    def test_entity_specific_matches_its_entity_and_global_matches_any(self):
        specific = definition("D-ENT", entity_id="ENT-01", rules_=[(10, True)])
        globaldef = definition("D-GLOBAL", entity_id=None, rules_=[(10, True)])
        kept = rules.candidate_definitions(
            [globaldef, specific], object_type="PO_AMEND",
            business_date=date(2026, 6, 1), entity_id="ENT-02")
        assert [d.definition_id for d in kept] == ["D-GLOBAL"]

    def test_entity_specific_is_ordered_before_global(self):
        specific = definition("D-ENT", code="B", entity_id="ENT-01", rules_=[(10, True)])
        globaldef = definition("D-GLOBAL", code="A", entity_id=None, rules_=[(10, True)])
        kept = rules.candidate_definitions(
            [globaldef, specific], object_type="PO_AMEND",
            business_date=date(2026, 6, 1), entity_id="ENT-01")
        assert [d.definition_id for d in kept] == ["D-ENT", "D-GLOBAL"]


class TestResolution:
    def test_first_definition_whose_first_matching_rule_is_true_wins(self):
        first = definition("D1", code="A", rules_=[
            (10, cmp_(">=", path("delta_paise"), val(50_000_000))),
        ])
        second = definition("D2", code="B", rules_=[
            (20, cmp_(">=", path("delta_paise"), val(10_000_000))),
        ])
        resolution = rules.resolve_route([first, second], SNAPSHOT,
                                          object_type="PO_AMEND",
                                          business_date=date(2026, 6, 1))
        assert resolution.definition.definition_id == "D2"
        assert resolution.rule.priority == 20

    def test_rules_are_evaluated_in_ascending_priority(self):
        d = definition("D1", rules_=[
            (30, True),
            (10, cmp_(">=", path("delta_paise"), val(50_000_000))),
            (20, cmp_(">=", path("delta_paise"), val(10_000_000))),
        ])
        resolution = rules.resolve_route([d], SNAPSHOT, object_type="PO_AMEND",
                                          business_date=date(2026, 6, 1))
        assert resolution.rule.priority == 20

    def test_duplicate_priorities_are_refused_because_first_has_no_meaning(self):
        with pytest.raises(rules.PredicateError):
            definition("D1", rules_=[(10, True), (10, False)])

    def test_no_match_raises_route_unresolved_and_never_auto_approves(self):
        d = definition("D1", rules_=[
            (10, cmp_(">=", path("delta_paise"), val(50_000_000)))])
        with pytest.raises(rules.ApprovalRouteUnresolved) as excinfo:
            rules.resolve_route([d], SNAPSHOT, object_type="PO_AMEND",
                                 business_date=date(2026, 6, 1))
        assert excinfo.value.code == rules.ERR_ROUTE_UNRESOLVED
        assert "NEVER auto-approved" in excinfo.value.message

    def test_no_candidates_at_all_also_raises(self):
        with pytest.raises(rules.ApprovalRouteUnresolved):
            rules.resolve_route([], SNAPSHOT, object_type="PO_AMEND",
                                 business_date=date(2026, 6, 1))

    def test_a_definition_with_no_rules_is_not_a_catch_all(self):
        with pytest.raises(rules.ApprovalRouteUnresolved):
            rules.resolve_route([definition("D1", rules_=[])], SNAPSHOT,
                                 object_type="PO_AMEND",
                                 business_date=date(2026, 6, 1))

    def test_entity_is_read_from_the_snapshot_when_not_supplied(self):
        specific = definition("D-ENT", entity_id="ENT-01", rules_=[(10, True)])
        resolution = rules.resolve_route([specific], SNAPSHOT,
                                          object_type="PO_AMEND",
                                          business_date=date(2026, 6, 1))
        assert resolution.definition.definition_id == "D-ENT"


class TestWorkedExampleFromSectionNinePointThree:
    """`APDEF-POAMD` v3: p10 committee, p20 CFO, p30 standard.

    `delta_paise = 18000000` must miss p10 on both disjuncts and hit p20.
    """

    DEFINITION = None   # built per test; kept as a method for readability

    def _definition(self):
        return definition(
            "APDEF-POAMD", code="APDEF-POAMD", version=3, entity_id="ENT-01",
            effective_from=date(2026, 4, 1),
            rules_=[
                (10, {"op": "or", "args": [
                    cmp_(">=", path("delta_paise"), val(50_000_000)),
                    cmp_("==", path("asset_category"), val("LAND"))]}),
                (20, cmp_(">=", path("delta_paise"), val(10_000_000))),
                (30, True),
            ])

    def test_priority_ten_does_not_match(self):
        rule = self._definition().rules[0]
        assert rule.priority == 10
        assert rule.predicate.evaluate(SNAPSHOT) is False

    def test_priority_twenty_matches_and_wins(self):
        resolution = rules.resolve_route(
            [self._definition()], SNAPSHOT, object_type="PO_AMEND",
            business_date=date(2026, 6, 1))
        assert resolution.rule.priority == 20
        assert resolution.definition.code == "APDEF-POAMD"
        assert resolution.definition.version == 3

    def test_a_land_purchase_below_the_threshold_still_hits_priority_ten(self):
        land = {**SNAPSHOT, "delta_paise": 1_000_000, "asset_category": "LAND"}
        resolution = rules.resolve_route(
            [self._definition()], land, object_type="PO_AMEND",
            business_date=date(2026, 6, 1))
        assert resolution.rule.priority == 10

    def test_a_small_plant_amendment_falls_through_to_the_catch_all(self):
        small = {**SNAPSHOT, "delta_paise": 500}
        resolution = rules.resolve_route(
            [self._definition()], small, object_type="PO_AMEND",
            business_date=date(2026, 6, 1))
        assert resolution.rule.priority == 30


# ==========================================================================
# Stage skipping (contract 2: recorded, never omitted)
# ==========================================================================
class TestStageSkipping:
    def test_a_stage_with_no_applies_when_always_applies(self):
        applies, reason = rules.stage_applies(stage(1), SNAPSHOT)
        assert applies is True and reason is None

    def test_a_true_applies_when_opens_the_stage(self):
        s = stage(4, applies_when=cmp_("==", path("check_verdict"),
                                        val("EXCEEDS_BUDGET")))
        assert rules.stage_applies(s, SNAPSHOT) == (True, None)

    def test_a_false_applies_when_yields_skipped_with_a_reason_not_omission(self):
        s = stage(5, applies_when=cmp_(">=", path("delta_paise"), val(50_000_000)))
        applies, reason = rules.stage_applies(s, SNAPSHOT)
        assert applies is False
        assert reason == rules.SKIP_RULE_NOT_MET
        assert reason is not None, "an auditor must see WHY a stage did not run"


# ==========================================================================
# Assignees: expansion, delegation, then the contributor filter
# ==========================================================================
class TestRoleExpansionAndScope:
    def test_role_refs_expand_against_role_grant(self):
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": rules.APPROVER_ROLE,
                                  "approver_ref": "Finance"}])
        out = rules.expand_role_members(s, SNAPSHOT, {"Finance": ["U-FIN", "U-CFO"]})
        assert [a.user_id for a in out] == ["U-CFO", "U-FIN"]
        assert all(a.assigned_via == rules.VIA_ROLE for a in out)

    def test_scope_expr_narrows_a_role_to_holders_granted_that_dimension(self):
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": rules.APPROVER_ROLE,
                                  "approver_ref": "PFC",
                                  "scope_expr": {"project": "project_id"}}])
        out = rules.expand_role_members(
            s, SNAPSHOT, {"PFC": ["U-PFC", "U-OTHER"]},
            {"U-PFC": {"project": ["PRJ-01"]}, "U-OTHER": {"project": ["PRJ-99"]}})
        assert [a.user_id for a in out] == ["U-PFC"]

    def test_a_holder_unrestricted_on_a_dimension_matches_anything(self):
        """Mirrors `roles.resolve_scope`: no restriction row means unrestricted,
        and the two layers must not disagree about what an absent grant means."""
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": rules.APPROVER_ROLE,
                                  "approver_ref": "PFC",
                                  "scope_expr": {"project": "project_id"}}])
        out = rules.expand_role_members(s, SNAPSHOT, {"PFC": ["U-WIDE"]}, {"U-WIDE": {}})
        assert [a.user_id for a in out] == ["U-WIDE"]

    def test_a_user_approver_is_taken_literally(self):
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": rules.APPROVER_USER,
                                  "approver_ref": "U-CFO"}])
        out = rules.expand_role_members(s, SNAPSHOT, {})
        assert [(a.user_id, a.assigned_via) for a in out] == [("U-CFO", rules.VIA_USER)]

    def test_an_unimplemented_approver_kind_is_refused_not_silently_empty(self):
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": "ATTRIBUTE",
                                  "approver_ref": "cost_centre_owner"}])
        with pytest.raises(rules.PredicateError):
            rules.expand_role_members(s, SNAPSHOT, {})

    def test_expansion_order_is_stable_across_runs(self):
        s = stage(1, approvers=[{"ordinal": 1, "approver_kind": rules.APPROVER_ROLE,
                                  "approver_ref": "R"}])
        first = rules.expand_role_members(s, SNAPSHOT, {"R": ["U-C", "U-A", "U-B"]})
        second = rules.expand_role_members(s, SNAPSHOT, {"R": ["U-B", "U-C", "U-A"]})
        assert [a.user_id for a in first] == [a.user_id for a in second]


class TestContributorFilter:
    def test_contributors_are_removed_from_the_assignee_set(self):
        result = rules.apply_contributor_filter(
            assignees("U-PROC", "U-FIN", "U-CFO"), ["U-PROC"])
        assert result.user_ids == ("U-FIN", "U-CFO")
        assert result.excluded_contributors == ("U-PROC",)

    def test_an_empty_set_raises_and_never_returns_a_falsy_value(self):
        """Section 9.3 stage 3: the only Procurement holder is the maker. The
        engine does not skip and does not auto-approve."""
        with pytest.raises(rules.NoIndependentApprover) as excinfo:
            rules.apply_contributor_filter(assignees("U-PROC"), ["U-PROC"])
        assert excinfo.value.code == rules.ERR_NO_INDEPENDENT_APPROVER
        assert "NOT auto-approved" in excinfo.value.message

    def test_the_exception_names_who_was_removed_so_it_is_actionable(self):
        with pytest.raises(rules.NoIndependentApprover) as excinfo:
            rules.apply_contributor_filter(assignees("U-PROC"), ["U-PROC"])
        assert excinfo.value.detail["excluded_contributors"] == ["U-PROC"]

    def test_a_candidate_set_that_was_empty_to_begin_with_also_raises(self):
        with pytest.raises(rules.NoIndependentApprover):
            rules.apply_contributor_filter([], ["U-PROC"])

    def test_the_filter_runs_after_delegates_are_added_not_before(self):
        """Order is the control. A delegate who is also a contributor must not
        slip in through the delegation route, and only a filter that sees the
        FINAL set can stop them."""
        base = assignees("U-FIN")
        delegations = [deleg.Delegation("D1", "U-FIN", "U-PROC",
                                         active_from=date(2026, 1, 1))]
        expanded = deleg.expand_delegates(base, delegations, date(2026, 6, 1))
        assert "U-PROC" in [a.user_id for a in expanded]
        result = rules.apply_contributor_filter(expanded, ["U-PROC"])
        assert result.user_ids == ("U-FIN",)


class TestQuorumArithmetic:
    @pytest.mark.parametrize("count", [1, 2, 5])
    def test_all_requires_every_assignee(self, count):
        assert rules.required_quorum(rules.QUORUM_ALL, None, count) == count

    @pytest.mark.parametrize("count", [1, 3, 9])
    def test_any_requires_one(self, count):
        assert rules.required_quorum(rules.QUORUM_ANY, None, count) == 1

    def test_n_of_m_requires_n(self):
        assert rules.required_quorum(rules.QUORUM_N_OF_M, 2, 5) == 2

    def test_n_of_m_larger_than_the_set_is_refused_as_unsatisfiable(self):
        with pytest.raises(rules.PredicateError):
            rules.required_quorum(rules.QUORUM_N_OF_M, 6, 5)

    @pytest.mark.parametrize("pct,count,expected", [
        (100, 3, 3),      # the float form gives 2.9999999999999996 -> 2
        (50, 3, 2),       # ceil(1.5)
        (50, 4, 2),
        (60, 5, 3),
        (1, 3, 1),        # never rounds down to zero
        (33, 3, 1),
        (67, 3, 3),       # ceil(2.01)
    ])
    def test_percent_is_an_integer_ceiling(self, pct, count, expected):
        assert rules.required_quorum(rules.QUORUM_PERCENT, pct, count) == expected

    def test_percent_never_exceeds_the_assignee_count(self):
        assert rules.required_quorum(rules.QUORUM_PERCENT, 100, 2) == 2

    @pytest.mark.parametrize("pct", [0, -5, 101, None, 50.0, True])
    def test_a_nonsensical_percentage_is_refused(self, pct):
        with pytest.raises(rules.PredicateError):
            rules.required_quorum(rules.QUORUM_PERCENT, pct, 4)

    @pytest.mark.parametrize("quorum_type,quorum_n", [
        (rules.QUORUM_ALL, None), (rules.QUORUM_ANY, None),
        (rules.QUORUM_N_OF_M, 1), (rules.QUORUM_PERCENT, 50)])
    def test_an_empty_assignee_set_raises_for_every_quorum_type(self, quorum_type, quorum_n):
        """A zero requirement is a met requirement. "The stage needs zero
        approvals" is the arithmetic form of auto-approval."""
        with pytest.raises(rules.NoIndependentApprover):
            rules.required_quorum(quorum_type, quorum_n, 0)

    def test_quorum_met_is_false_for_a_non_positive_requirement(self):
        assert rules.quorum_met(0, 0) is False
        assert rules.quorum_met(0, 5) is False
        assert rules.quorum_met(-1, 99) is False

    def test_quorum_met_is_true_only_at_or_above_the_requirement(self):
        assert rules.quorum_met(2, 1) is False
        assert rules.quorum_met(2, 2) is True
        assert rules.quorum_met(2, 3) is True

    def test_an_unknown_quorum_type_is_refused(self):
        with pytest.raises(rules.PredicateError):
            rules.required_quorum("MAJORITY", None, 3)


class TestParallelGroups:
    def test_a_group_completes_when_every_stage_meets_quorum_in_either_order(self):
        first_done = [{"quorum_required": 1, "quorum_met": 1},
                      {"quorum_required": 1, "quorum_met": 0}]
        second_done = [{"quorum_required": 1, "quorum_met": 0},
                       {"quorum_required": 1, "quorum_met": 1}]
        both = [{"quorum_required": 1, "quorum_met": 1},
                {"quorum_required": 1, "quorum_met": 1}]
        assert rules.group_complete(first_done) is False
        assert rules.group_complete(second_done) is False
        assert rules.group_complete(both) is True

    def test_an_empty_group_is_not_complete(self):
        assert rules.group_complete([]) is False

    def test_stages_sharing_a_group_open_together_as_one_wave(self):
        stages = [stage(1), stage(2, parallel_group="G2"),
                  stage(3, parallel_group="G2"), stage(4)]
        assert engine.compute_waves(stages) == [[1], [2, 3], [4]]

    def test_a_non_contiguous_parallel_group_is_refused(self):
        stages = [stage(1, parallel_group="G"), stage(2),
                  stage(3, parallel_group="G")]
        with pytest.raises(engine.ApprovalError):
            engine.compute_waves(stages)

    def test_the_next_wave_opens_only_once_the_previous_one_is_settled(self):
        waves = [[1], [2, 3], [4]]
        assert engine.next_wave(waves, {}) == [1]
        assert engine.next_wave(waves, {1: rules.STAGE_PENDING}) is None
        assert engine.next_wave(waves, {1: rules.STAGE_APPROVED}) == [2, 3]
        assert engine.next_wave(
            waves, {1: rules.STAGE_APPROVED, 2: rules.STAGE_APPROVED}) is None
        assert engine.next_wave(
            waves, {1: rules.STAGE_APPROVED, 2: rules.STAGE_APPROVED,
                    3: rules.STAGE_APPROVED}) == [4]

    def test_a_skipped_stage_settles_its_wave_without_approving_anything(self):
        waves = [[1], [2]]
        assert engine.next_wave(waves, {1: rules.STAGE_SKIPPED}) == [2]
        assert engine.all_waves_settled(
            waves, {1: rules.STAGE_SKIPPED, 2: rules.STAGE_APPROVED}) is True

    def test_a_definition_with_no_waves_is_never_settled(self):
        """'There was nothing to approve' must not read as 'it is approved'."""
        assert engine.all_waves_settled([], {}) is False


# ==========================================================================
# Delegation
# ==========================================================================
class TestDelegationWindow:
    def make(self, **kwargs):
        params = {"delegation_id": "D1", "delegator_user_id": "U-FIN",
                  "delegate_user_id": "U-PFC",
                  "active_from": date(2026, 6, 1), "active_to": date(2026, 6, 10)}
        params.update(kwargs)
        return deleg.Delegation(**params)

    def test_the_lower_bound_is_inclusive(self):
        assert self.make().is_live(date(2026, 6, 1)) is True

    def test_the_upper_bound_is_exclusive_matching_daterange(self):
        assert self.make().is_live(date(2026, 6, 9)) is True
        assert self.make().is_live(date(2026, 6, 10)) is False

    def test_a_date_before_the_window_is_not_live(self):
        assert self.make().is_live(date(2026, 5, 31)) is False

    def test_open_ended_bounds_are_permitted(self):
        assert self.make(active_to=None).is_live(date(2030, 1, 1)) is True
        assert self.make(active_from=None).is_live(date(2000, 1, 1)) is True

    def test_a_revocation_ends_the_delegation_at_the_instant_not_the_day(self):
        d = self.make(revoked_at=datetime(2026, 6, 5, 11, 0, tzinfo=UTC))
        assert d.is_live(datetime(2026, 6, 5, 10, 0, tzinfo=UTC)) is True
        assert d.is_live(datetime(2026, 6, 5, 14, 0, tzinfo=UTC)) is False

    def test_a_naive_instant_is_read_as_utc_rather_than_raising(self):
        d = self.make(revoked_at=datetime(2026, 6, 5, 11, 0))
        assert d.is_live(datetime(2026, 6, 5, 14, 0)) is False

    def test_scope_key_narrows_a_delegation(self):
        d = self.make(scope_key="PRJ-01")
        assert d.is_live(date(2026, 6, 2), scope_key="PRJ-01") is True
        assert d.is_live(date(2026, 6, 2), scope_key="PRJ-99") is False

    def test_a_delegation_with_no_scope_key_covers_everything(self):
        assert self.make(scope_key=None).is_live(date(2026, 6, 2),
                                                  scope_key="PRJ-99") is True

    def test_an_empty_window_is_refused_because_it_looks_configured(self):
        with pytest.raises(deleg.DelegationWindowInvalid) as excinfo:
            deleg.validate_window(date(2026, 6, 1), date(2026, 6, 1))
        assert excinfo.value.code == rules.ERR_DELEGATION_WINDOW_INVALID

    def test_an_inverted_window_is_refused(self):
        with pytest.raises(deleg.DelegationWindowInvalid):
            deleg.validate_window(date(2026, 6, 10), date(2026, 6, 1))

    def test_a_one_day_window_is_expressed_as_the_following_day(self):
        deleg.validate_window(date(2026, 6, 1), date(2026, 6, 2))     # no raise
        d = self.make(active_from=date(2026, 6, 1), active_to=date(2026, 6, 2))
        assert d.is_live(date(2026, 6, 1)) is True
        assert d.is_live(date(2026, 6, 2)) is False


class TestDelegationAddsCapacityNeverRemovesAccountability:
    DELEGATIONS = [deleg.Delegation("D1", "U-FIN", "U-PFC",
                                     active_from=date(2026, 6, 1),
                                     active_to=date(2026, 6, 30))]

    def test_the_delegate_is_added(self):
        out = deleg.expand_delegates(assignees("U-FIN"), self.DELEGATIONS,
                                      date(2026, 6, 5))
        assert [a.user_id for a in out] == ["U-FIN", "U-PFC"]

    def test_the_delegator_stays_in_the_set(self):
        """Section 9.2 step 5(b). Withdrawing the delegator would move
        accountability off the person the configuration named."""
        out = deleg.expand_delegates(assignees("U-FIN"), self.DELEGATIONS,
                                      date(2026, 6, 5))
        assert "U-FIN" in [a.user_id for a in out]

    def test_the_delegate_carries_the_delegation_provenance(self):
        out = deleg.expand_delegates(assignees("U-FIN"), self.DELEGATIONS,
                                      date(2026, 6, 5))
        delegate = [a for a in out if a.user_id == "U-PFC"][0]
        assert delegate.assigned_via == rules.VIA_DELEGATION
        assert delegate.delegated_from == "U-FIN"

    def test_an_expired_delegation_adds_nobody(self):
        out = deleg.expand_delegates(assignees("U-FIN"), self.DELEGATIONS,
                                      date(2026, 7, 5))
        assert [a.user_id for a in out] == ["U-FIN"]

    def test_a_delegation_of_somebody_not_on_this_stage_adds_nobody(self):
        out = deleg.expand_delegates(assignees("U-CFO"), self.DELEGATIONS,
                                      date(2026, 6, 5))
        assert [a.user_id for a in out] == ["U-CFO"]

    def test_a_stage_that_forbids_delegation_gets_no_delegates(self):
        out = deleg.expand_delegates(assignees("U-FIN"), self.DELEGATIONS,
                                      date(2026, 6, 5), allow_delegation=False)
        assert [a.user_id for a in out] == ["U-FIN"]

    def test_delegation_chains_are_not_followed(self):
        """One hop. A chain would carry accountability arbitrarily far from the
        person the workflow named, through people who never saw the object."""
        chain = [deleg.Delegation("D1", "U-FIN", "U-PFC", active_from=date(2026, 1, 1)),
                 deleg.Delegation("D2", "U-PFC", "U-REQ", active_from=date(2026, 1, 1))]
        out = deleg.expand_delegates(assignees("U-FIN"), chain, date(2026, 6, 5))
        assert [a.user_id for a in out] == ["U-FIN", "U-PFC"]

    def test_an_existing_assignee_is_not_added_twice(self):
        d = [deleg.Delegation("D1", "U-FIN", "U-CFO", active_from=date(2026, 1, 1))]
        out = deleg.expand_delegates(assignees("U-FIN", "U-CFO"), d, date(2026, 6, 5))
        assert [a.user_id for a in out] == ["U-FIN", "U-CFO"]
        assert all(a.assigned_via == rules.VIA_ROLE for a in out)


class TestDelegationCannotLaunderASelfApproval:
    """Contract 5 / section 9.4 step 4: BOTH identities are checked."""

    def test_a_contributor_acting_personally_is_refused(self):
        with pytest.raises(deleg.DelegatedSelfApproval) as excinfo:
            deleg.assert_delegation_independent("U-PROC", None, ["U-PROC"])
        assert excinfo.value.code == rules.ERR_SELF_APPROVAL

    def test_a_contributor_acting_as_somebody_elses_delegate_is_refused(self):
        with pytest.raises(deleg.DelegatedSelfApproval):
            deleg.assert_delegation_independent("U-PROC", "U-FIN", ["U-PROC"])

    def test_acting_for_a_contributor_is_refused(self):
        with pytest.raises(deleg.DelegatedSelfApproval) as excinfo:
            deleg.assert_delegation_independent("U-FIN", "U-PROC", ["U-PROC"])
        assert "launder" in excinfo.value.message

    def test_two_independent_identities_pass(self):
        deleg.assert_delegation_independent("U-FIN", "U-CFO", ["U-PROC"])   # no raise


# ==========================================================================
# Idempotency and staleness (contract 8)
# ==========================================================================
class TestIdempotencyKeyHandling:
    def test_a_key_is_required_on_every_decision(self):
        for missing in (None, "", "   "):
            with pytest.raises(engine.ApprovalError) as excinfo:
                engine.assert_idempotency_key(missing)
            assert excinfo.value.code == rules.ERR_IDEMPOTENCY_KEY_REQUIRED

    def test_a_supplied_key_is_returned_trimmed(self):
        assert engine.assert_idempotency_key("  key-1 ") == "key-1"

    def test_a_replayed_decision_reports_idempotent_replay(self):
        """The engine's replay branch reconstructs the ORIGINAL outcome from the
        stored one and flags it, rather than re-applying anything."""
        original = engine.DecisionResult(
            instance_id="AINS-1", action=rules.ACTION_APPROVE, stage_no=2,
            stage_status=rules.STAGE_APPROVED, instance_status=rules.INST_OPEN,
            quorum_required=2, quorum_met=2, opened_stage_nos=(3,))
        stored = original.as_dict()
        replayed = engine.DecisionResult(
            instance_id="AINS-1", action=stored["action"],
            stage_no=stored["stage_no"], stage_status=stored["stage_status"],
            instance_status=stored["instance_status"],
            quorum_required=stored["quorum_required"],
            quorum_met=stored["quorum_met"],
            opened_stage_nos=tuple(stored["opened_stage_nos"]),
            replayed=True, code=rules.ERR_IDEMPOTENT_REPLAY)
        assert replayed.instance_status == original.instance_status
        assert replayed.stage_no == original.stage_no
        assert replayed.quorum_met == original.quorum_met
        assert replayed.replayed is True
        assert replayed.code == rules.ERR_IDEMPOTENT_REPLAY

    def test_the_outcome_round_trips_through_its_serialised_form(self):
        result = engine.DecisionResult(
            instance_id="AINS-1", action=rules.ACTION_APPROVE, stage_no=1,
            stage_status=rules.STAGE_APPROVED,
            instance_status=rules.INST_APPROVED, quorum_required=1, quorum_met=1)
        assert result.as_dict()["instance_status"] == rules.INST_APPROVED
        assert result.as_dict()["opened_stage_nos"] == []


class TestObjectVersionStaleness:
    def test_a_matching_version_passes(self):
        engine.assert_object_version_fresh(3, 3, instance_id="AINS-1")
        engine.assert_object_version_fresh("3", 3, instance_id="AINS-1")

    def test_a_moved_version_is_refused(self):
        with pytest.raises(engine.ApprovalError) as excinfo:
            engine.assert_object_version_fresh(3, 4, instance_id="AINS-1")
        assert excinfo.value.code == rules.ERR_OBJECT_VERSION_STALE
        assert excinfo.value.status == 409
        assert excinfo.value.detail["current_object_version"] == 4

    def test_the_content_sha_changes_when_the_document_changes(self):
        before = engine.content_sha({"amount_paise": 100, "note": "a"})
        after = engine.content_sha({"amount_paise": 200, "note": "a"})
        assert before != after

    def test_the_content_sha_is_stable_under_key_reordering(self):
        """A re-serialisation is not an edit, and must not trigger a supersede."""
        assert engine.content_sha({"a": 1, "b": 2}) == engine.content_sha({"b": 2, "a": 1})

    def test_the_sha_is_taken_over_the_NORMALISED_snapshot_on_both_paths(self):
        """`open_instance` fills in object_type/object_id before hashing. If the
        supersede check hashed the caller's raw snapshot instead, it would find
        a difference that is nothing but those two keys and supersede an
        unchanged document on every single check."""
        raw = {"amount_paise": 100}
        normalised = engine.normalise_snapshot(raw, object_type="BUDGET_REVISION",
                                                object_id="BR-1")
        assert engine.content_sha(raw) != engine.content_sha(normalised), (
            "the fill-in genuinely changes the digest, which is why both paths "
            "must normalise before hashing")
        again = engine.normalise_snapshot(dict(raw), object_type="BUDGET_REVISION",
                                           object_id="BR-1")
        assert engine.content_sha(again) == engine.content_sha(normalised)

    def test_normalisation_never_overwrites_what_the_caller_supplied(self):
        normalised = engine.normalise_snapshot(
            {"object_type": "MINE", "object_id": "X"},
            object_type="BUDGET_REVISION", object_id="BR-1")
        assert normalised["object_type"] == "MINE"
        assert normalised["object_id"] == "X"


# ==========================================================================
# Reason codes, budget movement, SLA
# ==========================================================================
class TestReasonRequirement:
    def test_a_stage_requiring_a_reason_refuses_an_approval_without_one(self):
        with pytest.raises(engine.ApprovalError) as excinfo:
            engine.assert_reason_supplied(stage(4, requires_reason=True),
                                           rules.ACTION_APPROVE, None, None)
        assert excinfo.value.code == rules.ERR_REASON_REQUIRED

    def test_either_a_code_or_free_text_satisfies_the_requirement(self):
        s = stage(4, requires_reason=True)
        engine.assert_reason_supplied(s, rules.ACTION_APPROVE, "RC-01", None)
        engine.assert_reason_supplied(s, rules.ACTION_APPROVE, None, "because")

    def test_whitespace_is_not_a_reason(self):
        with pytest.raises(engine.ApprovalError):
            engine.assert_reason_supplied(stage(4, requires_reason=True),
                                           rules.ACTION_APPROVE, "  ", "  ")

    @pytest.mark.parametrize("action", [rules.ACTION_REJECT, rules.ACTION_RETURN])
    def test_reject_and_return_always_need_a_reason_whatever_the_stage_says(self, action):
        with pytest.raises(engine.ApprovalError):
            engine.assert_reason_supplied(stage(1, requires_reason=False),
                                           action, None, None)

    def test_an_approval_on_a_stage_that_does_not_require_one_needs_no_reason(self):
        engine.assert_reason_supplied(stage(1), rules.ACTION_APPROVE, None, None)


class TestBudgetMovement:
    def test_availability_falling_since_routing_is_a_move(self):
        assert engine.budget_moved({"available_paise": 1_000_000},
                                    {"available_paise": 400_000}) is True

    def test_availability_rising_is_not_a_move(self):
        assert engine.budget_moved({"available_paise": 400_000},
                                    {"available_paise": 1_000_000}) is False

    def test_unchanged_availability_is_not_a_move(self):
        assert engine.budget_moved({"available_paise": 400_000},
                                    {"available_paise": 400_000}) is False

    def test_an_exception_route_already_exceeding_budget_is_not_blocked_by_its_verdict(self):
        """Section 9.3's stage 4 exists precisely to approve an EXCEEDS_BUDGET
        object. Testing the verdict rather than the number would make the
        exception route unusable."""
        routed = {"verdict": "EXCEEDS_BUDGET", "available_paise": 100}
        current = {"verdict": "EXCEEDS_BUDGET", "available_paise": 100}
        assert engine.budget_moved(routed, current) is False

    def test_a_verdict_that_stays_ok_while_availability_halves_is_still_a_move(self):
        assert engine.budget_moved({"verdict": "OK", "available_paise": 1_000_000},
                                    {"verdict": "OK", "available_paise": 500_000}) is True

    def test_a_missing_check_is_not_reported_as_a_move(self):
        assert engine.budget_moved(None, {"available_paise": 1}) is False
        assert engine.budget_moved({}, {}) is False


class TestSlaAndEscalation:
    def test_due_at_is_the_sla_offset_from_opening(self):
        opened = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert engine.due_at_for(opened, 24) == datetime(2026, 6, 2, 9, 0, tzinfo=UTC)

    def test_no_sla_means_no_due_date(self):
        assert engine.due_at_for(datetime.now(UTC), None) is None
        assert engine.due_at_for(datetime.now(UTC), 0) is None

    def test_escalation_is_due_only_after_the_threshold(self):
        opened = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert engine.is_escalation_due(opened, 24, opened + timedelta(hours=23)) is False
        assert engine.is_escalation_due(opened, 24, opened + timedelta(hours=24)) is True

    def test_no_threshold_never_escalates(self):
        opened = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        assert engine.is_escalation_due(opened, None, opened + timedelta(days=365)) is False

    def test_a_naive_timestamp_is_read_as_utc_rather_than_raising(self):
        """A scheduled job that crashes on a timezone is a job that silently
        stops escalating anything."""
        opened = datetime(2026, 6, 1, 9, 0)
        assert engine.is_escalation_due(opened, 1, datetime(2026, 6, 1, 11, 0)) is True
        assert engine.is_escalation_due(
            opened, 1, datetime(2026, 6, 1, 11, 0, tzinfo=UTC)) is True


# ==========================================================================
# Object bindings and the shared error shape
# ==========================================================================
class TestObjectBindings:
    def test_an_unknown_object_type_is_refused_before_any_sql_is_formatted(self):
        with pytest.raises(engine.ApprovalError) as excinfo:
            engine.binding_for("'; DROP TABLE budget_line; --")
        assert excinfo.value.code == "OBJECT_TYPE_UNKNOWN"

    def test_the_registry_is_a_closed_allow_list(self):
        for object_type, binding in engine.OBJECT_BINDINGS.items():
            assert binding.object_type == object_type
            assert binding.table.replace("_", "").isalnum()
            assert binding.pk_column.replace("_", "").isalnum()

    def test_known_types_resolve_to_real_postgres_tables(self):
        assert engine.binding_for("BUDGET_REVISION").table == "budget_revision"
        assert engine.binding_for("BUDGET_TRANSFER").table == "budget_transfer"


class TestTheHashChainIsReconstructibleFromStoredColumns:
    """Contract 9 reuses the frozen payload `prev|at|actor|action|type|id|detail`
    -- but contract 1's `approval_action` has **no `detail` column**.

    Hashing the engine's narrative would give entries nobody can recompute, and
    verification could then only check that the `prev_hash` links agree with
    each other. Somebody who rewrote an entry's `action` from REJECT to APPROVE
    would pass that check, because nothing tied the stored fields to the digest.
    So the hashed detail is built only from columns the row actually keeps.
    """

    def test_the_chain_detail_uses_only_stored_columns(self):
        assert engine.chain_detail("ASTG-1", "RC-01", "why") == \
            "ASTG-1\x1fRC-01\x1fwhy"

    def test_absent_fields_render_as_empty_and_stay_positional(self):
        assert engine.chain_detail(None, None, None) == "\x1f\x1f"
        assert engine.chain_detail(None, "RC", None) == "\x1fRC\x1f"

    def test_the_separator_is_not_the_payload_format_s_own_pipe(self):
        """The frozen payload joins its seven fields with `|`. A detail
        containing `|` could shift a field boundary; `\\x1f` cannot appear in an
        id or a reason code."""
        assert "|" not in engine.chain_detail("A", "B", "C")

    def test_tampering_with_any_stored_field_changes_the_digest(self):
        from app.backend.pg import audit as audit_mod

        def digest(action, reason_code, reason_text, actor, acting_for):
            return audit_mod.compute_entry_hash(
                None, "2026-06-01T00:00:00+00:00",
                engine._actor_for_hash(actor, acting_for), action,
                "BUDGET_REVISION", "BR-1",
                engine.chain_detail("ASTG-1", reason_code, reason_text))

        baseline = digest("REJECT", "RC-01", "no", "U-A", None)
        assert digest("APPROVE", "RC-01", "no", "U-A", None) != baseline
        assert digest("REJECT", "RC-02", "no", "U-A", None) != baseline
        assert digest("REJECT", "RC-01", "yes", "U-A", None) != baseline
        assert digest("REJECT", "RC-01", "no", "U-B", None) != baseline
        assert digest("REJECT", "RC-01", "no", "U-A", "U-C") != baseline, (
            "a delegated action must not be rewritable into a direct one")

    def test_the_narrative_is_kept_in_the_outcome_column_not_lost(self):
        """It is not hashed, because there is no column to recompute it from --
        but it is still stored, so the timeline can show it."""
        assert "detail" not in engine.DecisionResult(
            instance_id="X", action="APPROVE", stage_no=1,
            stage_status="APPROVED", instance_status="APPROVED").as_dict()


class TestInstanceScope:
    """Contract 6: `approval_instance` carries entity_id and project_id
    denormalised at creation so it is scopable directly.

    This check is what makes the two `scope-exempt:` document reads in
    `approvals.py` defensible: they reach a table through a computed name that
    no static gate can attribute, keyed by an id that came from an instance
    already checked here.
    """

    class FakeSession:
        """Carries a scope and nothing else. Never touches a database."""

        def __init__(self, scope):
            self.scope = scope

    def instance(self, **kwargs):
        base = {"instance_id": "AINS-1", "entity_id": "ENT-01",
                "project_id": "PRJ-01"}
        base.update(kwargs)
        return base

    def test_an_instance_inside_scope_passes(self):
        session = self.FakeSession(Scope(user_id="U", entity_ids=frozenset({"ENT-01"})))
        engine.assert_instance_in_scope(session, self.instance())

    def test_an_instance_outside_scope_is_refused(self):
        session = self.FakeSession(Scope(user_id="U", entity_ids=frozenset({"ENT-99"})))
        with pytest.raises(engine.ApprovalError) as excinfo:
            engine.assert_instance_in_scope(session, self.instance())
        assert excinfo.value.status == 403

    def test_the_project_dimension_is_checked_too(self):
        session = self.FakeSession(Scope(user_id="U", project_ids=frozenset({"PRJ-99"})))
        with pytest.raises(engine.ApprovalError):
            engine.assert_instance_in_scope(session, self.instance())

    def test_an_unrestricted_dimension_is_none_and_permits_everything(self):
        session = self.FakeSession(Scope(user_id="U", entity_ids=None,
                                          project_ids=None))
        engine.assert_instance_in_scope(session, self.instance())

    def test_an_empty_frozenset_means_nothing_not_everything(self):
        """The inversion `engine.Scope` exists to make unrepresentable."""
        session = self.FakeSession(Scope(user_id="U", entity_ids=frozenset()))
        with pytest.raises(engine.ApprovalError):
            engine.assert_instance_in_scope(session, self.instance())

    def test_read_all_bypasses(self):
        session = self.FakeSession(
            Scope(user_id="U", entity_ids=frozenset({"ENT-99"}), read_all=True))
        engine.assert_instance_in_scope(session, self.instance())

    def test_an_instance_carrying_no_id_on_a_dimension_is_not_refused(self):
        """There is nothing to compare -- the same reading `repo.compile_scope`
        gives a NULL column."""
        session = self.FakeSession(Scope(user_id="U", entity_ids=frozenset({"ENT-01"})))
        engine.assert_instance_in_scope(session, self.instance(entity_id=None))


class TestErrorShapeMatchesTheBudgetService:
    def test_every_error_carries_a_code_a_message_and_a_status(self):
        err = rules.ApprovalError("X", "message", status=409)
        assert (err.code, err.message, err.status) == ("X", "message", 409)
        assert err.detail == {}

    def test_the_frozen_error_codes_are_all_declared(self):
        frozen = {"APPROVAL_ROUTE_UNRESOLVED", "NO_INDEPENDENT_APPROVER",
                  "SELF_APPROVAL", "NOT_AN_ASSIGNEE", "STAGE_NOT_OPEN",
                  "REASON_REQUIRED", "OBJECT_VERSION_STALE",
                  "IDEMPOTENCY_KEY_REQUIRED", "IDEMPOTENT_REPLAY",
                  "DEFINITION_NOT_ACTIVE", "DEFINITION_IMMUTABLE",
                  "DELEGATION_WINDOW_INVALID", "BUDGET_MOVED"}
        declared = {getattr(rules, name) for name in dir(rules)
                    if name.startswith("ERR_")}
        assert frozen <= declared

    def test_the_contract_status_vocabularies_are_complete(self):
        assert set(rules.INSTANCE_STATUSES) == {
            "OPEN", "APPROVED", "REJECTED", "RETURNED", "RECALLED", "CANCELLED",
            "SUPERSEDED", "EXCEPTION_PENDING"}
        assert set(rules.STAGE_STATUSES) == {
            "PENDING", "APPROVED", "REJECTED", "RETURNED", "SKIPPED", "ESCALATED"}
        assert set((rules.ASSIGN_PENDING, rules.ASSIGN_ACTED,
                    rules.ASSIGN_WITHDRAWN)) == {"PENDING", "ACTED", "WITHDRAWN"}
