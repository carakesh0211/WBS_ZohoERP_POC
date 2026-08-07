"""AUD-H-007 - Money, rounding and foreign currency are unsafe.

The audited build converted rupees with ``int(round(float(rupees) * 100))``, which
lost money three ways: banker's rounding through binary float, inconsistent
half-paisa behaviour, and mantissa exhaustion on large amounts. These tests pin the
exact behaviour the corrected ``money`` module must keep.

No API or database is required: this module is pure arithmetic.
"""
from __future__ import annotations

import random
from decimal import Decimal

import pytest

from app.backend.money import (
    MAX_PAISE,
    MoneyError,
    format_inr,
    split_pro_rata,
    to_paise,
    to_rupees,
)


# ------------------------------------------------------------------ conversion
def test_aud_h_007_half_paisa_rounds_half_up_not_to_even():
    """0.005 rupees is half a paisa. Binary float + banker's rounding gave 0."""
    assert to_paise("0.005") == 1


def test_aud_h_007_half_paisa_rounding_is_internally_consistent():
    """0.015 must round the same direction as 0.005; the float path did not."""
    assert to_paise("0.015") == 2
    assert to_paise("0.025") == 3
    assert to_paise("0.035") == 4


def test_aud_h_007_large_amount_survives_float64_mantissa():
    """float64 returned 9007199254740994 for this amount - one paisa too many."""
    assert to_paise("90071992547409.93") == 9007199254740993


@pytest.mark.parametrize("value,expected", [
    ("0", 0),
    ("1", 100),
    ("0.01", 1),
    ("12.344", 1234),
    ("12.345", 1235),
    ("1,50,000.00", 15_000_000),
    ("  25.50  ", 2550),
    (Decimal("99.995"), 10000),
    (150000, 15_000_000),
])
def test_aud_h_007_supported_inputs_convert_exactly(value, expected):
    assert to_paise(value) == expected


@pytest.mark.parametrize("value", [
    0.1 + 0.2,          # 0.30000000000000004
    1.005,              # not exactly 1.00 or 1.01 in binary
    0.615,
    1e-3,
])
def test_aud_h_007_float_that_is_not_an_exact_two_decimal_amount_is_rejected(value):
    """Silently rounding an inexact float is precisely how the original defect
    happened. The caller must send a decimal string instead."""
    with pytest.raises(MoneyError):
        to_paise(value)


@pytest.mark.parametrize("value,expected", [(2.5, 250), (1.0, 100), (0.25, 25), (0.0, 0)])
def test_aud_h_007_float_that_is_an_exact_two_decimal_amount_is_accepted(value, expected):
    assert to_paise(value) == expected


@pytest.mark.parametrize("value", ["-1", "-0.01", -100, Decimal("-5.00")])
def test_aud_h_007_negative_is_rejected_by_default(value):
    with pytest.raises(MoneyError):
        to_paise(value)


@pytest.mark.parametrize("value,expected", [("-1.23", -123), ("-0.005", -1), (-100, -10000)])
def test_aud_h_007_negative_is_accepted_only_when_explicitly_allowed(value, expected):
    assert to_paise(value, allow_negative=True) == expected


@pytest.mark.parametrize("value", [None, True, False, "abc", "", "1.2.3", object()])
def test_aud_h_007_non_amounts_are_rejected(value):
    with pytest.raises(MoneyError):
        to_paise(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan")])
def test_aud_h_007_non_finite_amounts_are_rejected(value):
    with pytest.raises(MoneyError):
        to_paise(value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_aud_h_007_infinite_float_amount_raises_a_controlled_money_error(value):
    """Regression guard.

    ``to_paise`` used to quantise an incoming float before its ``is_finite``
    guard, so an infinite float escaped as ``decimal.InvalidOperation`` - an
    exception no handler maps, which surfaced as HTTP 500 where NaN correctly
    produced 422 INVALID_AMOUNT. Every rejection must be a MoneyError so the
    API can answer 4xx (AUD-H-008 / AUD-M-007).
    """
    with pytest.raises(MoneyError):
        to_paise(value)


def test_aud_h_007_amount_beyond_the_guard_rail_is_rejected():
    ok = str(MAX_PAISE // 100)
    assert to_paise(ok) == MAX_PAISE
    with pytest.raises(MoneyError):
        to_paise(str(MAX_PAISE // 100 + 1))


def test_aud_h_007_error_message_names_the_field():
    with pytest.raises(MoneyError, match="new_amount_rupees"):
        to_paise("-5", field="new_amount_rupees")


# ------------------------------------------------------------------ round trip
def test_aud_h_007_to_rupees_never_returns_a_float():
    assert isinstance(to_rupees(12345), Decimal)
    assert not isinstance(to_rupees(12345), float)


def test_aud_h_007_paise_to_rupees_and_back_is_lossless():
    """Property: for any paise value, str(to_rupees(p)) converts back to p."""
    rng = random.Random(20260806)
    values = [0, 1, 99, 100, 101, MAX_PAISE, 9007199254740993]
    values += [rng.randrange(0, 10 ** 15) for _ in range(500)]
    for paise in values:
        assert to_paise(str(to_rupees(paise))) == paise, paise


# ------------------------------------------------------------------ formatting
@pytest.mark.parametrize("paise,expected", [
    (0, "₹0.00"),
    (1, "₹0.01"),
    (100, "₹1.00"),
    (100000, "₹1,000.00"),
    (15_000_000, "₹1,50,000.00"),
    (123456789, "₹12,34,567.89"),
    (10_000_000_000, "₹10,00,00,000.00"),        # ten crore rupees
    (1_00_00_00_000_00, "₹1,00,00,00,000.00"),   # one hundred crore rupees
])
def test_aud_h_007_format_inr_uses_indian_digit_grouping(paise, expected):
    assert format_inr(paise) == expected


def test_aud_h_007_format_inr_brackets_negatives_and_can_drop_the_symbol():
    assert format_inr(-15_000_000) == "(₹1,50,000.00)"
    assert format_inr(15_000_000, symbol=False) == "1,50,000.00"


def test_aud_h_007_format_inr_tolerates_none_and_zero():
    assert format_inr(None) == "₹0.00"
    assert format_inr(0) == "₹0.00"


# ---------------------------------------------------------------- pro-rata split
@pytest.mark.parametrize("total,weights", [
    (100, [1, 1, 1]),
    (10, [1, 2, 3]),
    (1, [1, 1]),
    (0, [5, 5]),
    (7, [1]),
    (999_999_999, [3, 3, 3, 1]),
    (16_800_000, [7, 11, 13, 17, 19]),
])
def test_aud_h_007_split_pro_rata_conserves_every_paisa(total, weights):
    parts = split_pro_rata(total, weights)
    assert len(parts) == len(weights)
    assert sum(parts) == total


def test_aud_h_007_split_pro_rata_never_loses_a_paisa_property():
    """Property: over many random allocations the parts always re-sum to the total."""
    rng = random.Random(19700101)
    for _ in range(750):
        n = rng.randint(1, 12)
        weights = [rng.randint(1, 10_000) for _ in range(n)]
        total = rng.randrange(0, 5_000_000_00)
        parts = split_pro_rata(total, weights)
        assert sum(parts) == total, (total, weights, parts)
        assert len(parts) == n


def test_aud_h_007_split_pro_rata_is_deterministic():
    first = split_pro_rata(100, [1, 1, 1])
    for _ in range(20):
        assert split_pro_rata(100, [1, 1, 1]) == first


def test_aud_h_007_split_pro_rata_is_proportional_within_one_paisa():
    total, weights = 1_000_000, [1, 2, 7]
    parts = split_pro_rata(total, weights)
    total_w = sum(weights)
    for part, w in zip(parts, weights):
        assert abs(part - (total * w) // total_w) <= 1


@pytest.mark.parametrize("weights", [[0, 0], [], [0]])
def test_aud_h_007_split_pro_rata_refuses_a_zero_total_weight(weights):
    with pytest.raises(MoneyError):
        split_pro_rata(100, weights)
