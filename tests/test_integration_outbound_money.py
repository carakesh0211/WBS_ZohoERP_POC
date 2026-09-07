"""Negative paise on the outbound wire, and the flooring that got them wrong.

WHY THIS FILE EXISTS
====================

Both product adapters rendered a line's unit price as::

    rupees, sub = divmod(line.unit_price_paise, 100)
    ...
    "rate": f"{rupees}.{sub:02d}"

Python's ``divmod`` **floors**: it rounds the quotient toward negative
infinity and returns a non-negative remainder. So ``divmod(-150, 100)`` is
``(-2, 50)``, and a line worth minus one rupee fifty left as ``-2.50``::

       paise   true rupees   emitted    verdict
        -150        -1.50      -2.50    WRONG
         -99        -0.99      -1.01    WRONG
          -1        -0.01      -1.99    WRONG
      -25000      -250.00    -250.00    ok

The last row is the whole reason this survived. An exact multiple of 100 has
a zero remainder, and flooring a value with no fractional part is the
identity -- so every round-rupee fixture in the suite agreed with the broken
code, and every fixture was a round rupee.

The function's own docstring asserted the opposite: "there is no float here
and no ``Decimal``: the value is assembled by integer division so the string
is exactly what the ledger holds." Integer division was exactly the problem.
The arithmetic was exact and the answer was wrong, which is the failure mode
no amount of "we never use floats" discipline catches.

NEGATIVES ARE NOT HYPOTHETICAL
==============================
``dto.paise()`` calls ``to_paise(..., allow_negative=True)`` deliberately.
**Credit notes, returns, reversals and negative adjustments** are ordinary
documents on both products, and a debit-note or return line is exactly a
negative unit price. The scope of the defect was every one of them whose
amount was not a whole number of rupees.

WHAT IS ASSERTED
================
1. the measured table above, in both directions, against an INDEPENDENT
   oracle (``money.to_rupees``, which is Decimal-based and shares no code
   with the renderer);
2. that the two adapters' renderers agree byte for byte -- they are
   deliberate twins rather than a shared import, because §11 keeps the
   products from reaching into one another, and a duplicated money renderer
   is held to agreement by a test or it drifts;
3. that the wire form and the DISPLAY form (``money.format_inr``, Indian
   digit grouping at lakh and crore scale) describe the same amount, so the
   number a reviewer reads on screen is the number that went out;
4. a source-level gate: neither adapter may floor a paise value again.

No database, no network, no tenant.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.backend.integration import books_inventory, erp
from app.backend.integration.dto import LineDTO, paise
from app.backend.money import format_inr, to_paise, to_rupees

RENDERERS = {"erp": erp.render_paise, "books_inventory": books_inventory.render_paise}

#: The four rows measured on the shipped code, plus their positive mirrors.
#: ``-25000`` is kept even though it always passed: it is the row that hid the
#: other three, and dropping it would lose the evidence of why.
MEASURED = [
    (-150, "-1.50"),
    (-99, "-0.99"),
    (-1, "-0.01"),
    (-25000, "-250.00"),
    (150, "1.50"),
    (99, "0.99"),
    (1, "0.01"),
    (25000, "250.00"),
    (0, "0.00"),
    (-100, "-1.00"),
    (100, "1.00"),
]


# ============================================================ the defect itself
@pytest.mark.parametrize("product", sorted(RENDERERS))
@pytest.mark.parametrize("value,expected", MEASURED)
def test_negative_paise_render_as_the_amount_they_are(product, value, expected):
    """The measured table, both adapters. Three of these rows used to fail."""
    assert RENDERERS[product](value) == expected


@pytest.mark.parametrize("product", sorted(RENDERERS))
@pytest.mark.parametrize("value", [-1, -7, -99, -101, -150, -25099, -123456789])
def test_the_old_flooring_overstated_every_negative_it_touched(product, value):
    """Name the failure SHAPE, not just the four measured literals.

    The error was not a constant. Flooring borrows a rupee to keep the
    remainder non-negative, and the string then re-applies the sign to the
    whole, so the emitted value is ``floored_rupees - sub/100`` where the
    pair meant ``floored_rupees + sub/100``. The gap is therefore
    ``2 * sub / 100`` -- one rupee at ``-150``, two paise at ``-99``, one
    rupee ninety-eight at ``-1``. Every one of them made the amount **more
    negative than it was**: a credit note overstated, a return overstated, a
    reversal overstated. Systematically, and always in the direction that
    takes money out.

    Asserting the shape means a renderer that is wrong in some NEW way cannot
    pass by happening to match one of the literals in :data:`MEASURED`.
    """
    floored_rupees, floored_sub = divmod(value, 100)
    the_old_bug = Decimal(f"{floored_rupees}.{floored_sub:02d}")
    truth = Decimal(RENDERERS[product](value))

    assert truth == to_rupees(value)
    assert the_old_bug != truth, (
        f"{value} was chosen because flooring got it wrong; if the two now "
        f"agree, this parametrisation has drifted off the defect")
    assert abs(the_old_bug) > abs(truth), (
        "the historical defect always OVERSTATED the magnitude of a negative "
        "amount; a case that understated it would be a different bug")
    assert the_old_bug == truth - 2 * (Decimal(floored_sub) / 100), (
        "the gap is exactly twice the floored remainder; if that no longer "
        "holds, the arithmetic here has drifted from the defect it describes")


@pytest.mark.parametrize("product", sorted(RENDERERS))
def test_a_negative_under_one_rupee_keeps_its_sign(product):
    """The ``-0.xx`` band, where a sign taken from the quotient disappears.

    For ``-99 <= paise <= -1`` the magnitude's rupee part is ``0``, and an
    ``int`` has no ``-0``. A renderer that derived the sign from ``rupees``
    rather than from the value would emit ``0.99`` for minus ninety-nine
    paise -- the same money lost as the original defect, in the other
    direction, and invisible to a test that only checked ``-150``.
    """
    for value in range(-99, 0):
        rendered = RENDERERS[product](value)
        assert rendered.startswith("-0."), (value, rendered)
        assert to_paise(rendered, allow_negative=True) == value


# ================================================= against an independent oracle
@pytest.mark.parametrize("product", sorted(RENDERERS))
def test_every_paise_value_across_two_rupees_matches_the_decimal_oracle(product):
    """Exhaustive over [-200, 200] against ``money.to_rupees``.

    ``to_rupees`` is Decimal-based and shares no code with the renderer, so
    agreement is evidence rather than a tautology. Exhaustive rather than
    sampled because the band that was wrong is narrow and dense: every value
    that is not a multiple of 100.
    """
    render = RENDERERS[product]
    for value in range(-200, 201):
        assert render(value) == str(to_rupees(value)), value


@pytest.mark.parametrize("product", sorted(RENDERERS))
@pytest.mark.parametrize("value", [
    -1, -99, -101, -999_99, -1_00_00_000_01, -10_00_00_00_000_99,
    123456789, -123456789, 10_000_000_000, -10_000_000_000,
])
def test_the_wire_string_round_trips_back_to_the_same_paise(product, value):
    """The only property that actually matters: nothing is lost.

    ``to_paise`` is the reader the platform uses on the way IN, so this closes
    the loop -- the string we emit is one we would ourselves read back as the
    integer we started with. Values reach crore scale, where a renderer that
    reached for a float would start losing the paise.
    """
    assert to_paise(RENDERERS[product](value), allow_negative=True) == value


# ================================================ the two adapters must agree
@pytest.mark.parametrize("value", [v for v, _ in MEASURED] + [
    -1_23_45_678_99, 1_23_45_678_99, -7, 7, -100_000_000_000, 100_000_000_000,
])
def test_both_products_render_money_identically(value):
    """Deliberate twins, held to agreement.

    §11 keeps the two adapters from importing one another, so the renderer is
    duplicated on purpose. Duplication without a test is drift with a delay;
    this is the test.
    """
    assert erp.render_paise(value) == books_inventory.render_paise(value)


@pytest.mark.parametrize("module,builder", [
    ("erp", erp._outbound_line), ("books_inventory", books_inventory._outbound_line),
])
def test_the_emitted_line_carries_the_repaired_rate(module, builder):
    """Through the real ``LineDTO``, not just the helper.

    The defect lived in ``_outbound_line``; proving ``render_paise`` alone
    would leave open that the line builder never calls it.
    """
    line = LineDTO(
        external_line_id="L-1", line_number=1, description="Return of pump",
        quantity="-1", unit_price_paise=paise("-1.50", field="line.rate"),
        line_total_paise=-150, tax_paise=0, item_external_id="ITM-1")
    assert line.unit_price_paise == -150
    assert builder(line)["rate"] == "-1.50"


# ============================== the display form and the wire form must agree
@pytest.mark.parametrize("paise_value,grouped", [
    (15_000_000, "₹1,50,000.00"),            # one lakh fifty thousand rupees
    (10_000_000_000, "₹10,00,00,000.00"),    # ten crore rupees
    (1_00_00_00_000_00, "₹1,00,00,00,000.00"),  # one hundred crore rupees
    (123456789, "₹12,34,567.89"),
])
def test_indian_grouping_at_lakh_and_crore_describes_the_wire_value(
        paise_value, grouped):
    """Grouping is for humans; the wire gets none of it.

    Both directions are asserted: the display string groups at lakh and crore
    (2,2,3 from the right, not 3,3,3), and the wire string carries no
    separator at all -- a comma in a JSON decimal string is not a thousands
    separator to the receiving system, it is a parse failure or, worse, a
    truncation.
    """
    assert format_inr(paise_value) == grouped
    wire = erp.render_paise(paise_value)
    assert "," not in wire and "₹" not in wire
    assert Decimal(wire) == Decimal(grouped.replace("₹", "").replace(",", ""))


@pytest.mark.parametrize("paise_value,grouped", [
    (-15_000_099, "(₹1,50,000.99)"),
    (-1, "(₹0.01)"),
    (-10_000_000_001, "(₹10,00,00,000.01)"),
])
def test_the_display_form_survives_a_negative_that_is_not_a_round_rupee(
        paise_value, grouped):
    """``format_inr``'s own negative coverage was one round value: ``-15_000_000``.

    The same blind spot that hid the wire defect -- every negative fixture in
    the suite was an exact multiple of 100. ``format_inr`` takes ``abs()``
    before dividing and so was always correct, but "correct and never tested
    at the value that would expose it" is how the renderer next door shipped
    broken for a wave. Accounting brackets, so the magnitude is what is
    grouped and the sign is the parentheses.
    """
    assert format_inr(paise_value) == grouped
    assert erp.render_paise(paise_value).startswith("-")
    assert Decimal(erp.render_paise(paise_value)) == -Decimal(
        grouped.strip("()").replace("₹", "").replace(",", ""))


# ================================================================ source gate
_ADAPTERS = [
    Path(__file__).resolve().parents[1] / "app" / "backend" / "integration" / name
    for name in ("erp.py", "books_inventory.py")
]


@pytest.mark.parametrize("path", _ADAPTERS, ids=lambda p: p.name)
def test_no_adapter_floors_a_paise_value_again(path):
    """``divmod`` over a paise value must be over its MAGNITUDE.

    A regression gate rather than a style rule: the repair is one ``abs()``
    away from being reverted by anyone who reads the original docstring and
    believes it. Parsed, not grepped, so a ``divmod`` inside a comment or a
    docstring -- both files now describe the defect at length, in prose that
    contains the offending expression -- is not mistaken for code.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "divmod" and node.args):
            continue
        first = node.args[0]
        wrapped_in_abs = (isinstance(first, ast.Call)
                          and isinstance(first.func, ast.Name)
                          and first.func.id == "abs")
        if not wrapped_in_abs:
            offenders.append(ast.unparse(node))
    assert not offenders, (
        f"{path.name} divides a value by 100 without taking its magnitude "
        f"first: {offenders}. divmod() floors, so this is wrong for every "
        f"negative amount that is not an exact multiple of 100 -- see this "
        f"module's docstring for the measured table.")


@pytest.mark.parametrize("path", _ADAPTERS, ids=lambda p: p.name)
def test_no_adapter_routes_money_through_a_float(path):
    """No ``float(``, no ``/`` on a paise name, no ``round(``.

    "Never route paise through a JavaScript floating-point value" is the rule;
    Python's ``float`` is that same IEEE double, so the rule is enforced at the
    only place it can be -- where the value is turned into text.
    """
    source = path.read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"float", "round"}, (
                f"{path.name} calls {node.func.id}() -- money must never "
                f"become a float, and rounding it is how AUD-H-007 happened.")
    assert not re.search(r"_paise\s*/\s*[^/]", "\n".join(code_lines)), (
        f"{path.name} applies true division to a paise value; integer paise "
        f"divided by 100 with `/` is a float.")
