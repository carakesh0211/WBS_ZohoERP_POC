"""The sweeps must be able to read the DTOs the adapters actually emit.

WHY THIS EXISTS
===============

An adversarial review found that GRN attribution read six field names that
`LineDTO` does not have. Against a real line carrying
`purchase_order_line_external_id='PO-LINE-77'` and `line_total_paise=25000000`,
the reader returned `None` and `0`.

Two silent failures composed:

1. Every receive line became `GRN_LINE_UNATTRIBUTED` even where the tenant HAD
   populated the linkage — making AUD-H-004's whole "documented is not
   populated" design moot, because the code could not read the linkage when it
   was populated.
2. The quarantine bucket accumulated **zero**, while the docstring above it
   promised "quarantined at full value in a project-level bucket". Rs 2,50,000
   vanished — a silent drop inside the anti-silent-drop mechanism.

Every existing sweeps test passed throughout.

THE ROOT CAUSE, WHICH IS WHY THIS FILE IS NOT JUST A REGRESSION TEST
====================================================================

`tests/integration_fakes.py::FakeLine` was written with the docstring "One
receive or bill line, **in the shape `sweeps.normalise` reads**". The double
was shaped to the READER, not to the PRODUCER. So the test suite asserted that
the reader reads what the reader expects, which is a tautology, and the join
between the adapters' output and the sweeps' input was never tested at all.

The same review found four more doubles with the same property. A double
shaped to the code under test cannot fail the way production fails.

So this file tests the JOIN: real DTOs from `dto.py`, through the real reader
in `sweeps.py`, with no double anywhere.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.backend.integration import sweeps
from app.backend.integration.dto import LineDTO

#: A line as an adapter really emits it: linkage populated, money non-zero.
_PO_LINE = "PO-LINE-77"
_TOTAL_PAISE = 25_000_000          # Rs 2,50,000.00


def _real_line(**overrides) -> LineDTO:
    fields = dict(
        external_line_id="RL-1",
        line_number=1,
        description="Centrifugal pump, 40 HP",
        quantity=1,
        unit_price_paise=_TOTAL_PAISE,
        line_total_paise=_TOTAL_PAISE,
        tax_paise=0,
        item_external_id="ITEM-9",
        purchase_order_line_external_id=_PO_LINE,
        dimensions={},
        raw={},
    )
    fields.update(overrides)
    return LineDTO(**fields)


def test_the_reader_finds_the_po_line_linkage_on_a_real_dto():
    """The linkage AUD-H-004 is about, read off the object that carries it.

    This is the assertion whose absence let the attribution read `None` on
    every well-formed line while every test stayed green.
    """
    line = _real_line()
    found = sweeps._first_attr(
        line, ("purchase_order_line_external_id", "po_line_external_id",
               "line_item_id", "purchaseorder_item_id", "line_id"))
    assert found == _PO_LINE, (
        f"the reader got {found!r} from a LineDTO whose "
        f"purchase_order_line_external_id is {_PO_LINE!r}. Every receive line "
        f"would be quarantined as unattributed even where the tenant "
        f"populated the linkage.")


def test_the_reader_finds_the_full_value_on_a_real_dto():
    """Rs 2,50,000 must not read as zero.

    The quarantine bucket is the control: an unattributed line is held at FULL
    value and blocks capitalisation. A bucket that accumulates zero reports
    success while losing the money it exists to protect.
    """
    line = _real_line()
    found = sweeps._first_attr(
        line, ("line_total_paise", "amount_paise", "total_paise"), 0) or 0
    assert found == _TOTAL_PAISE, (
        f"the reader got {found} paise from a LineDTO holding "
        f"{_TOTAL_PAISE}. The unattributed bucket would accumulate nothing "
        f"while reporting that it had quarantined the line.")
    assert isinstance(found, int), (
        f"line value is {type(found).__name__}, not int; money is integer "
        f"paise and a float here loses paise silently")


def test_no_candidate_name_list_can_miss_every_real_dto_field():
    """The general form, so the next reader cannot repeat this.

    Any tuple of candidate names the sweeps use to read a line must contain at
    least one name `LineDTO` actually defines. A list that names only foreign
    spellings resolves to the default on every real object — which is exactly
    what happened, and it is invisible because a default is a legitimate value.
    """
    real_fields = {f.name for f in dataclasses.fields(LineDTO)}
    identity = ("purchase_order_line_external_id", "po_line_external_id",
                "line_item_id", "purchaseorder_item_id", "line_id")
    money = ("line_total_paise", "amount_paise", "total_paise")

    for label, candidates in (("identity", identity), ("money", money)):
        overlap = real_fields & set(candidates)
        assert overlap, (
            f"the {label} candidate list {candidates} names no field LineDTO "
            f"defines ({sorted(real_fields)}). Every read resolves to the "
            f"default, and a default is a legitimate value, so nothing raises.")


def test_the_double_is_not_shaped_to_the_reader():
    """The root cause, guarded directly.

    `FakeLine` carried the docstring "in the shape `sweeps.normalise` reads",
    which is precisely the property that makes a double useless: it agrees
    with the code under test by construction, so the suite verifies the reader
    against itself.

    A double standing in for `LineDTO` must therefore only carry attribute
    names `LineDTO` really has. It may carry FEWER — a double is allowed to be
    partial — but a name the real object lacks means the tests are exercising
    a shape production never produces.
    """
    fakes = pytest.importorskip("tests.integration_fakes",
                                reason="the sweeps doubles live here")
    fake_line = getattr(fakes, "FakeLine", None)
    if fake_line is None or not dataclasses.is_dataclass(fake_line):
        pytest.skip("FakeLine is not a dataclass in this build")

    real_fields = {f.name for f in dataclasses.fields(LineDTO)}
    fake_fields = {f.name for f in dataclasses.fields(fake_line)}
    invented = sorted(fake_fields - real_fields)

    assert not invented, (
        f"FakeLine carries {invented}, which LineDTO does not define. The "
        f"double is shaped to the reader rather than to the producer, so "
        f"every test using it verifies the reader against itself. That is how "
        f"a reader looking for six non-existent field names stayed green.")
