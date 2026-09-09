"""AUD-H-007 closed at the WRITE path: the rate reaches the money.

WHAT WAS STILL WRONG AFTER MIGRATION 023
========================================

023 shipped an FX engine and no caller. Its header says it "applies the
exchange rate that 013 stored and nobody ever multiplied", and that is true of
``app/backend/pg/fx.py`` and false of the product::

    grep -rn "translate_bill|assess_revaluation|record_rate|source_amount_minor" \\
         --include=*.py app/ tools/
    -> app/backend/pg/fx.py only

``mirror_bill`` -- the one writer of ``bill`` and ``bill_line``, and therefore
of every rupee of CWIP the product reports -- wrote the vendor's own figure
straight into ``amount_paise``, which 023's own ``COMMENT`` declares to be INR
base paise. It set neither ``source_currency`` nor ``source_amount_minor``. A
EUR bill therefore still stood at its face value in rupees, and
``FINDINGS_REMEDIATION_STATUS.csv`` recording AUD-H-007 as not implemented was
accidentally correct.

The wire was broken in three places at once, which is why nobody noticed one of
them: ``dto.BillDTO.currency_code`` was populated by both adapters,
``sweeps.normalise`` dropped it building ``SourceRecord``,
``SweepBillDetail._mirror`` therefore had nothing to pass, and ``mirror_bill``
had no parameter to receive it if it had.

AND FINDING H-2, WHICH IS WORSE THAN THE ABSENCE
===============================================

``_mirror_bill_line``'s upsert ends ``amount_paise = EXCLUDED.amount_paise``,
and ``trg_bill_line_source_amount_immutable`` (023:557) fires only when
``NEW.source_amount_minor IS DISTINCT FROM OLD.source_amount_minor``. Ingestion
never set that column, so on every re-mirror NEW equalled OLD, the trigger
passed, and a TRANSLATED figure was overwritten with the UNTRANSLATED one --
while ``bill.fx_translated_at`` went on asserting a translation the lines no
longer carried. The guard was not weak. It was unreachable.

WHICH HALF OF THIS FILE RUNS WHERE
==================================

MOST OF IT RUNS EVERYWHERE, and that is deliberate rather than convenient. The
allocation arithmetic is pure, the sweep wiring can be proved against the real
DTOs with an in-process store double, and the migration's own text can be read
off disk. Every property that matters about the translation -- that a JPY bill
is not translated as though yen were cents, that the lines sum to the header to
the paisa, that a credit note keeps its sign, that the allocation does not
depend on the order the payload happened to arrive in -- is a property of our
own code, and gating it behind a database nobody here has would put the proof
in the one environment the author cannot run.

THE LIVE HALF HAS NEVER EXECUTED ON THE MACHINE THIS WAS WRITTEN ON. There is
no PostgreSQL here. Every ``@pytest.mark.pg`` test below first runs in CI's
``pg_tests`` job, which runs ``pytest tests/test_pg_*.py`` and nothing else --
which is why this file is named as it is; a sibling stream lost a whole suite
this wave to a name the job did not collect. A SKIP IS NOT A PASS, and the skip
reason says so.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg
# is deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace. Same note as tests/test_pg_fx.py.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import inspect  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from app.backend.integration import jobs  # noqa: E402
from app.backend.integration import sweeps  # noqa: E402
from app.backend.integration.dto import BillDTO, LineDTO, SourceRef  # noqa: E402
from app.backend.pg import fx  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

from integration_fakes import (  # noqa: E402  the Wave 5 in-process doubles
    ERP as ERP_CAPABILITIES,
    FakeAdapter,
    FakeClock,
    InMemoryStore,
)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a "
           "live database. A SKIP IS NOT A PASS.",
)

MIGRATION = (_Path(__file__).resolve().parents[1]
             / "migrations" / "pg" / "025_fx_applied_at_ingestion.sql")

# The seed's own foreign-currency vendor: PO-012, SunPeak Energy GmbH, EUR at
# 92.50 (`app/backend/db.py:540`). A bill of EUR 1,00,000.00 against it stood
# at Rs 1,00,000 instead of Rs 92,50,000.
EUR_RATE = Decimal("92.50000000")
EUR_MINOR = 10_000_000            # EUR 1,00,000.00 in cents
EUR_BASE_PAISE = 925_000_000      # Rs 92,50,000.00 in paise

ORG = "ORG-FXI"
ENTITY = "ENT-FXI"
OTHER_ENTITY = "ENT-FXI-B"
PROJECT = "PRJ-FXI"
OTHER_PROJECT = "PRJ-FXI-B"
WBS = "WBS-FXI"
HEAD = "BH-FXI"
SOURCE_LABEL = "ZOHO_ERP"
BILL_DATE = date(2026, 9, 7)
T0 = datetime(2026, 9, 7, 11, 30, 15, tzinfo=timezone.utc)


# ===========================================================================
# THE WIRE. These run everywhere, and they are the reason the finding closes.
# ===========================================================================
class _MirroringStore(InMemoryStore):
    """`InMemoryStore` plus `mirror_bill`, recording the call VERBATIM.

    A subclass rather than an edit to `tests/integration_fakes.py`, which
    belongs to another owner. Recording the kwargs verbatim is the point: the
    assertions below are about what the sweep PASSES, not about what a double
    chose to keep. A double shaped to the reader cannot fail the way production
    failed, and that lesson is already on record in
    `tests/test_integration_dto_reader_contract.py`.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mirrored: list[dict] = []

    def mirror_bill(self, **kwargs):
        self.mirrored.append(dict(kwargs))
        return {"bill_id": "BILL-" + str(kwargs["external_id"]),
                "attributed": len(kwargs.get("lines") or ()),
                "quarantined": 0, "quarantined_paise": 0}


def _bill_dto(*, external_id: str, currency: str, total_paise: int) -> BillDTO:
    """A bill exactly as an adapter emits one. NO double anywhere.

    Built from `dto.BillDTO` and `dto.LineDTO` because the defect being proved
    fixed is a JOIN between three modules -- the DTO carries `currency_code`,
    the sweep must read it, and the ledger must receive it. A `FakeRecord`
    shaped to whichever field the reader happens to want proves nothing about
    any of the three.
    """
    return BillDTO(
        source=SourceRef(product="ERP", service="erp", api_version="v3",
                         endpoint="/bills/" + external_id, retrieved_at=T0),
        external_id=external_id, document_number="BN-" + external_id,
        document_date=BILL_DATE, last_modified=T0,
        vendor_external_id="V-SUNPEAK", vendor_name="SunPeak Energy GmbH",
        currency_code=currency, subtotal_paise=total_paise, tax_paise=0,
        total_paise=total_paise, external_status_raw="open",
        purchase_order_external_ids=("ZPO-9001",),
        lines=(LineDTO(external_line_id="RL-1", line_number=1,
                       description="Inverter", quantity="1",
                       unit_price_paise=total_paise,
                       line_total_paise=total_paise, tax_paise=0,
                       purchase_order_line_external_id="ZPOL-5001"),),
        lines_hydrated=True,
        raw={"bill_id": external_id, "currency_code": currency})


def _run_bill_detail(store_double, *, queued, details):
    clock = FakeClock()
    adapter = FakeAdapter(clock, bill_details=details, seconds_per_call=1)
    store_double.detail_queue.extend(queued)
    job = sweeps.SweepBillDetail(adapter, store_double,
                                 store_double.connection_id,
                                 external_source=SOURCE_LABEL)
    store_double.enqueue(job.kind)
    return jobs.run_job(job, store=store_double, clock=clock,
                        capabilities=ERP_CAPABILITIES)


def test_the_dtos_currency_survives_normalisation():
    """`SourceRecord` had no currency field, so `normalise` dropped it.

    This is the FIRST of the three breaks, and on its own it is enough to keep
    AUD-H-007 open for ever: everything downstream can be perfect and the value
    still never arrives.
    """
    record = sweeps.normalise(
        _bill_dto(external_id="ZB-EUR", currency="EUR",
                  total_paise=EUR_MINOR),
        module=sweeps.MODULE_BILLS)
    assert record.currency_code == "EUR", (
        "normalise dropped currency_code; migration 023's whole FX engine "
        "sits downstream of a value that never arrives")


def test_a_source_that_names_no_currency_is_the_base_currency():
    """The default is what every document has effectively been treated as
    since the product was written. What changes is that a document which DOES
    say EUR is now believed -- not that a silent one starts guessing."""
    dto = _bill_dto(external_id="ZB-Q", currency="", total_paise=1)
    assert sweeps.normalise(dto, module=sweeps.MODULE_BILLS).currency_code \
        == fx.BASE_CURRENCY


def test_the_sweep_hands_the_ledger_the_currency_the_bill_is_denominated_in():
    """THE WIRE, END TO END, EXECUTED -- not a source scan.

    A real `BillDTO` in EUR goes through the real `SweepBillDetail`, and the
    call the ledger surface receives is inspected. Before this change the sweep
    could not have passed a currency: `SourceRecord` had no field for it and
    `mirror_bill` had no parameter for it.
    """
    double = _MirroringStore()
    run = _run_bill_detail(
        double, queued=["ZB-EUR"],
        details={"ZB-EUR": _bill_dto(external_id="ZB-EUR", currency="EUR",
                                     total_paise=EUR_MINOR)})
    assert run.state == jobs.JOB_DONE
    assert len(double.mirrored) == 1, "the hydrated bill never reached the ledger"
    assert double.mirrored[0]["source_currency"] == "EUR", (
        "the sweep mirrored a EUR bill without telling the ledger it was EUR, "
        "so its amounts land in a column declared to be INR base paise")


def test_the_ledger_surface_declares_a_parameter_that_can_receive_it():
    """A sweep that passes an argument nothing accepts is a TypeError, not a
    wiring. Asserted against the real signature so the two halves cannot drift
    apart in the direction that type-checks and does nothing."""
    parameters = inspect.signature(procurement.mirror_bill).parameters
    assert "source_currency" in parameters
    assert parameters["source_currency"].default == fx.BASE_CURRENCY, (
        "the default must be the base currency, or every existing caller and "
        "every existing test starts describing an INR bill as untranslatable")
    protocol = inspect.signature(sweeps.SweepStore.mirror_bill).parameters
    assert "source_currency" in protocol, (
        "SweepStore does not declare the currency, so a store that ignores it "
        "is ignoring nothing the protocol says exists")


def test_mirror_bill_reaches_the_canonical_fx_service_and_not_a_second_one():
    """ONE implementation of "multiply by the rate", and this is what pins it.

    The failure this guards against is not "the rate is never applied" -- that
    was H-1 and is closed above. It is the next one: a second multiplication
    written inline here because importing the service felt heavy, diverging
    from `translate_document` on the first rounding boundary anybody tests.
    """
    source = inspect.getsource(procurement.mirror_bill)
    assert "fx_svc.resolve_basis" in source, (
        "mirror_bill does not resolve a rate through the FX service")
    assert "_translate_bill_payload" in source
    assert "fx_svc.register_translation" in source, (
        "nothing records that the rate was applied, so 'exactly once' is a "
        "promise rather than a primary key")
    body = inspect.getsource(procurement)
    translate = inspect.getsource(procurement._translate_bill_payload)
    assert "fx_svc.translate_lines" in translate
    # No inline arithmetic on a rate anywhere in the ingest module.
    assert "* rate" not in body and "rate *" not in body, (
        "a second multiplication path exists in procurement.py; the rate is "
        "multiplied into money in exactly one place, fx.translate_to_base_paise")


# ===========================================================================
# The allocation, proved on this machine
# ===========================================================================
def _identity(index: int, key: str | None = None) -> dict:
    return {"line_external_id": None, "external_line_id": key or f"L{index}",
            "line_key": key or f"L{index}",
            "quarantine_key": key or f"L{index}"}


def _basis(currency: str, rate: str, exponent: int) -> fx.TranslationBasis:
    return fx.TranslationBasis(
        source_currency=currency, minor_exponent=exponent,
        rate=Decimal(rate), rate_date=BILL_DATE, rate_source="RBI_REFERENCE",
        fx_rate_id="FXR-TEST")


def _translate(basis, money):
    """`_translate_bill_payload` with no session -- it never needs one.

    The exponent and the rate are already resolved onto the basis; this half is
    pure arithmetic, which is why it can be proved here at all.
    """
    identities = [_identity(i) for i in range(len(money))]
    return procurement._translate_bill_payload(
        None, bill_id="BILL-T", identities=identities, source_money=money,
        basis=basis)


def test_the_seeds_own_eur_bill_becomes_the_figure_it_should_always_have_had():
    """EUR 1,00,000.00 at 92.50 is Rs 92,50,000.00, and was Rs 1,00,000."""
    header, lines = _translate(_basis("EUR", "92.50", 2),
                               [(EUR_MINOR, 0, 0)])
    assert header == EUR_BASE_PAISE
    assert lines == [(EUR_BASE_PAISE, 0, 0)]


def test_a_zero_exponent_currency_is_not_read_as_hundredths_of_itself():
    """JPY HAS NO MINOR UNIT. A payload figure of 1,000,000 is a million YEN,
    not ten thousand of anything. Assuming exponent 2 understates it a
    hundredfold -- which is not a rounding error, it is a missing crore."""
    header, _ = _translate(_basis("JPY", "0.55000000", 0),
                           [(1_000_000, 0, 0)])
    # 1,000,000 yen * 0.55 * 10^(2-0) = 55,000,000 paise = Rs 5,50,000.
    assert header == 55_000_000
    wrong = _translate(_basis("JPY", "0.55000000", 2), [(1_000_000, 0, 0)])[0]
    assert wrong == 550_000
    assert header == wrong * 100, (
        "the exponent is not being applied; a JPY bill would post at one "
        "hundredth of its value")


def test_a_three_decimal_currency_is_honoured_too():
    """KWD is thousandths. 1,000,000 fils is KWD 1,000."""
    header, _ = _translate(_basis("KWD", "271.40000000", 3),
                           [(1_000_000, 0, 0)])
    # 1,000,000 fils * 271.4 * 10^(2-3) = 27,140,000 paise = Rs 2,71,400.
    assert header == 27_140_000


def test_all_three_money_columns_are_translated_and_not_only_the_first():
    """THE DEFECT INSIDE THE REMEDY.

    Every rollup in the product sums `amount_paise + non_creditable_tax_paise
    + freight_paise` -- `domain.compute_ledger` twice,
    `procurement_services._RECOMPUTE_DERIVED_SQL` twice (and it is the ONLY
    writer of `budget_ledger_cell`), both reconciliation readers, both of
    `reporting.py`'s branches, and `/api/bills`. `fx.py` contains not one
    occurrence of either of the other two columns. Translating one and leaving
    two adds a rupee figure to two euro figures and calls the total CWIP.
    """
    header, lines = _translate(_basis("EUR", "92.50", 2),
                               [(10_000_00, 5_000_00, 2_500_00)])
    amount, tax, freight = lines[0]
    assert tax != 500_000, "the tax column was left at its source value"
    assert freight != 250_000, "the freight column was left at its source value"
    assert amount + tax + freight == header
    # 17,50,000 cents at 92.50 = Rs 16,18,750.00
    assert header == 161_875_000


def test_the_lines_always_sum_to_the_translated_header():
    """TRANSLATE ONCE, THEN ALLOCATE. Translating each cell independently and
    summing is the other obvious implementation and it disagrees: the header is
    the figure on the vendor's document, and the lines must reconcile to it to
    the paisa."""
    money = [(333_333, 11_111, 7), (666_667, 1, 0), (1, 0, 999_999)]
    header, lines = _translate(_basis("EUR", "92.53170000", 2), money)
    assert sum(sum(cells) for cells in lines) == header


def test_the_allocation_does_not_depend_on_the_order_the_payload_arrived_in():
    """`split_pro_rata` hands the leftover paise to the largest fractional
    remainders, so payload order would otherwise decide which line receives the
    odd paisa. The same bill replayed with its lines transposed would then
    rewrite `amount_paise` by a paisa while `source_amount_minor` stayed
    identical -- which is exactly the drift 023's trigger cannot see, arriving
    through the code written to prevent it.

    So the allocation is ordered by the DERIVED `bill_line_id`, and this is
    what holds it there.
    """
    basis = _basis("EUR", "92.53170000", 2)
    money = [(333_333, 0, 0), (333_333, 0, 0), (333_334, 0, 0)]
    keys = ["Z-LAST", "A-FIRST", "M-MID"]

    forward = procurement._translate_bill_payload(
        None, bill_id="BILL-ORD",
        identities=[_identity(i, keys[i]) for i in range(3)],
        source_money=money, basis=basis)
    order = [2, 0, 1]
    reversed_ = procurement._translate_bill_payload(
        None, bill_id="BILL-ORD",
        identities=[_identity(i, keys[i]) for i in order],
        source_money=[money[i] for i in order], basis=basis)

    assert forward[0] == reversed_[0]
    by_key_forward = dict(zip(keys, forward[1], strict=True))
    by_key_reversed = dict(zip([keys[i] for i in order], reversed_[1],
                               strict=True))
    assert by_key_forward == by_key_reversed, (
        "the same bill allocated differently because its lines arrived in a "
        "different order; a reordered replay would silently move a paisa")


def test_a_credit_note_keeps_its_sign_and_rounds_symmetrically():
    """§2.4 models a credit note as negative `amount_paise`, and
    `ROUND_HALF_UP` in `decimal` is half AWAY FROM ZERO. The symmetry is
    required, not incidental: a rule that rounded negatives toward zero would
    make the reversal of a rounded line fail to reverse it by one paisa,
    permanently."""
    basis = _basis("EUR", "92.50", 2)
    positive, plines = _translate(basis, [(10_000_000, 0, 0)])
    negative, nlines = _translate(basis, [(-10_000_000, 0, 0)])
    assert negative == -positive
    assert nlines[0][0] == -plines[0][0]


def test_a_rounding_boundary_rounds_away_from_zero_in_both_directions():
    """The half-paisa case, stated both ways.

    One cent at a rate of 0.5 is exactly half a paisa -- the boundary itself,
    not a value near it -- and it must become 1 and -1, never 0 and 0 and never
    1 and 0. `ROUND_HALF_UP` in :mod:`decimal` is half AWAY FROM ZERO, and the
    symmetry is what makes a credit note reverse its bill exactly.
    """
    basis = _basis("USD", "0.50000000", 2)
    assert _translate(basis, [(1, 0, 0)])[0] == 1
    assert _translate(basis, [(-1, 0, 0)])[0] == -1
    # And the neighbouring boundary, so "always rounds up" is excluded too:
    # 0.49 of a paisa is 0, in both directions.
    below = _basis("USD", "0.49000000", 2)
    assert _translate(below, [(1, 0, 0)])[0] == 0
    assert _translate(below, [(-1, 0, 0)])[0] == 0


def test_a_document_with_both_signs_is_refused_rather_than_guessed_at():
    """Pro rata across mixed signs has no defensible reading and the weights
    can sum to zero. A bill and a credit note are two documents, which is what
    §2.4 already models."""
    with pytest.raises(fx.FxError) as excinfo:
        _translate(_basis("EUR", "92.50", 2), [(100, 0, 0), (-100, 0, 0)])
    assert excinfo.value.code == "FX_MIXED_SIGN_ALLOCATION"


def test_an_inr_document_is_not_routed_through_the_rounding_at_all():
    """THE IDENTITY IS THE IDENTITY. For an INR bill the source amount already
    IS the base amount; multiplying by 1 and quantising is a no-op that
    nonetheless routes every rupee figure in the product through a rounding
    step -- and a rounding step that is a no-op today is one policy edit away
    from not being one."""
    basis = fx.identity_basis()
    assert basis.is_identity
    money = [(123_456_789, 7, 11), (-5, 0, 0)]
    header, lines = _translate(basis, money)
    assert lines == money, "an INR bill's own integers came back changed"
    assert header == sum(sum(cells) for cells in money)


def test_a_bill_with_no_lines_translates_to_nothing_rather_than_raising():
    """`allocate_base_paise` refuses to allocate across no lines, correctly.
    A header with no lines is a real inbound shape all the same -- an
    unhydrated bill, or one every line of which the source withdrew -- and it
    must mirror rather than crash the sweep."""
    assert _translate(_basis("EUR", "92.50", 2), []) == (0, [])


# ===========================================================================
# The basis: resolved once, compared honestly
# ===========================================================================
def test_the_identity_basis_is_the_shape_the_check_constraint_requires():
    """`ck_bill_fx_provenance` (023:469) requires an INR bill to carry rate 1,
    no fx_rate row and no rate date. A basis that did not is a row the database
    refuses at 3am in a sweep."""
    basis = fx.identity_basis()
    assert basis.source_currency == "INR"
    assert basis.rate == Decimal(1)
    assert basis.fx_rate_id is None
    assert basis.rate_date is None


def test_a_basis_matches_the_four_facts_the_trigger_compares_and_no_others():
    """`trg_bill_fx_basis_immutable` compares currency, rate, date and rate id.
    `rate_source` is carried for evidence and deliberately NOT compared: a feed
    that renamed itself must not turn an ordinary sweep re-walk into a
    refusal."""
    basis = _basis("EUR", "92.50", 2)
    assert basis.matches(source_currency="EUR", rate=Decimal("92.50000000"),
                         rate_date=BILL_DATE, fx_rate_id="FXR-TEST")
    assert not basis.matches(source_currency="USD", rate=Decimal("92.50"),
                             rate_date=BILL_DATE, fx_rate_id="FXR-TEST")
    assert not basis.matches(source_currency="EUR", rate=Decimal("92.51"),
                             rate_date=BILL_DATE, fx_rate_id="FXR-TEST")
    assert not basis.matches(source_currency="EUR", rate=Decimal("92.50"),
                             rate_date=date(2026, 9, 6),
                             fx_rate_id="FXR-TEST")
    assert not basis.matches(source_currency="EUR", rate=Decimal("92.50"),
                             rate_date=BILL_DATE, fx_rate_id="FXR-OTHER")


def test_an_unknown_document_type_is_a_migration_and_not_a_string():
    """`ck_fx_translation_event_document_type` names the one kind that has both
    foreign money and an ingestion path. A second is a contract change AND a
    migration, deliberately together -- which is the discipline whose absence
    left 023's columns without a writer."""
    with pytest.raises(KeyError):
        fx.register_translation(
            None, document_type="EXPENSE_CLAIM", document_id="X",
            entity_id=ENTITY, basis=fx.identity_basis(),
            source_total_minor=0, base_total_paise=0, line_count=0, actor="T")


# ===========================================================================
# Migration 025's own text
# ===========================================================================
def _applied_sql() -> str:
    text = MIGRATION.read_text(encoding="utf-8")
    applied = text.split("-- ROLLBACK:", 1)[0]
    return "\n".join(line for line in applied.splitlines()
                     if not line.lstrip().startswith("--"))


def test_the_document_types_mirror_the_migrations_own_check():
    """The constant and the CHECK are two statements of one fact, and the whole
    value of the constant is that it is the same fact. `SEEDED_MINOR_EXPONENTS`
    carries the same guard against the same drift."""
    match = re.search(
        r"ck_fx_translation_event_document_type\s*\n?\s*CHECK\s*\("
        r"document_type IN \(([^)]*)\)\)",
        MIGRATION.read_text(encoding="utf-8"))
    assert match, "the CHECK could not be found; this guard has stopped guarding"
    declared = tuple(re.findall(r"'([A-Z_]+)'", match.group(1)))
    assert declared == fx.TRANSLATABLE_DOCUMENT_TYPES


def test_one_rounding_rule_and_not_two():
    """023 made HALF_UP the only value `fx_policy.FX_RATE_ROUNDING` permits and
    said the key exists "so the rule is stated as data and reviewable, not so
    it can drift". 025 records the rule on every event row; a second permitted
    value in either place would be two rules."""
    assert fx.ROUNDING_RULE == "HALF_UP"
    assert fx.POLICY_DEFAULTS["FX_RATE_ROUNDING"] == fx.ROUNDING_RULE
    match = re.search(r"ck_fx_translation_rounding\s+CHECK \(rounding IN \(([^)]*)\)\)",
                      MIGRATION.read_text(encoding="utf-8"))
    assert match and re.findall(r"'([A-Z_]+)'", match.group(1)) == ["HALF_UP"]


def test_the_migration_is_additive():
    """No DROP and no ALTER COLUMN outside the revert block. 025 creates and
    adds; it replaces nothing 001..024 created, because those files are
    checksum-frozen over their whole text and a database whose 023 differs by
    one byte is unadoptable."""
    executable = _applied_sql().upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP CONSTRAINT",
                      "DROP POLICY", "ALTER COLUMN"):
        assert forbidden not in executable, (
            f"{forbidden} appears outside the revert block")


def test_the_rollback_block_deletes_its_own_ledger_row():
    """020, 021 and 022 exist because 015, 018 and 019 did not do this. A
    revert that drops the objects and leaves the ledger row makes `upgrade()`
    skip the migration for ever while `assert_schema_current` reports current."""
    block = MIGRATION.read_text(encoding="utf-8").split("-- ROLLBACK:", 1)[1]
    assert "DELETE FROM schema_migrations WHERE version = '025'" in block


def test_the_new_table_carries_rls_enabled_forced_and_a_policy():
    """`migrate_pg._rls_problems` verifies all three from this file's own text,
    so a database that has the table but not the enforcement cannot certify as
    adopted. A translation event names a document, and a document belongs to an
    entity: an unscoped read is another entity's foreign-currency exposure,
    document by document, with the amounts."""
    sql = _applied_sql()
    assert "ALTER TABLE fx_translation_event ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE fx_translation_event FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY fx_translation_event_scope" in sql
    assert "capex_scope_permits(entity_id, NULL, NULL, NULL)" in sql


def test_the_event_table_is_append_only_in_privileges_and_in_a_trigger():
    """A privilege can be granted by a later migration; a trigger cannot be
    granted around. Both, because the once-only argument rests on this row."""
    sql = _applied_sql()
    grant = re.search(r"GRANT ([A-Z, ]+) ON fx_translation_event", sql)
    assert grant and "UPDATE" not in grant.group(1) and "DELETE" not in grant.group(1)
    assert "trg_fx_translation_event_no_update" in sql
    assert "trg_fx_translation_event_no_delete" in sql


def test_every_paise_column_the_migration_creates_is_bigint_and_visible():
    """`_PAISE_COLUMN_RE` is anchored at `^`, so a paise column the parser
    cannot see is a money column whose TYPE adoption never verifies."""
    from app.backend.pg import migrate_pg

    migration = next(m for m in migrate_pg.discover() if m.version == "025")
    found = migrate_pg._paise_columns_by(migration)
    assert ("fx_translation_event", "base_total_paise") in found, (
        "the paise column is invisible to the adoption parser")


def test_the_source_columns_are_not_named_paise():
    """For a JPY line the source figure is whole yen and for a KWD line it is
    fils. Calling it `_paise` would write the defect into the column name, and
    `migrate_pg`'s paise-type rule would then certify a figure that is not in
    paise. 023 made this argument for `bill_line.source_amount_minor`; the four
    columns 025 adds obey it."""
    sql = _applied_sql()
    for column in ("source_tax_minor", "source_freight_minor"):
        assert column in sql
    assert "source_total_paise" not in sql
    assert "source_amount_paise" not in sql


def test_the_purchase_order_is_named_as_a_gap_and_not_given_dead_columns():
    """THE DISCIPLINE THIS WHOLE FINDING IS ABOUT, APPLIED TO ITS OWN REMEDY.

    The approved contracts identify TWO documents carrying money that may be
    foreign: the vendor bill and the purchase order. Only the bill is wired
    here, and 025 adds the purchase order NO COLUMNS -- because
    `procurement_services._write_po` is its only writer and its only callers
    are `create_po` and `convert_pr_to_po`, both reached from the API. A
    purchase order is ORIGINATED in this product and EMITTED to Zoho; it does
    not arrive, so there is no ingestion path on which to translate it.

    Adding provenance columns with nothing to write them is EXACTLY what 023
    did -- `bill.source_currency` and `bill_line.source_amount_minor`, and the
    guard built on the second stayed unreachable for two waves. Repeating it
    inside the remedy would be worse than leaving the gap named.
    """
    sql = _applied_sql()
    assert "purchase_order ADD COLUMN" not in sql
    assert "po_line ADD COLUMN" not in sql
    assert "PURCHASE_ORDER" not in sql.upper(), (
        "the event table admits a document type nothing writes")
    # ...and the absence is ARGUED in the migration, not merely present.
    header = MIGRATION.read_text(encoding="utf-8")
    assert "WHAT THIS FILE DELIBERATELY DOES NOT ADD" in header
    assert "_write_po" in header, (
        "the reason the purchase order is absent is not recorded where the "
        "next person will look for it")


def test_no_constraint_is_added_to_any_table_001_to_024_created():
    """`ALTER TABLE ... ADD CONSTRAINT` VALIDATES against every existing row.

    A CHECK true of new rows and false of old ones does not fail at the first
    bad write -- it fails the migration, on exactly the database it was written
    for. 023 could add `ck_bill_fx_provenance` unconditionally only because
    `bill` had no currency column before it, so every row was INR by
    construction. Nothing here has that luxury, so nothing here tries.
    """
    assert "ADD CONSTRAINT" not in _applied_sql(), (
        "a constraint is being added to an existing table; it will be "
        "validated against every row already in it")


# ===========================================================================
# Live PostgreSQL. NONE OF THESE HAS EVER EXECUTED ON THIS MACHINE.
# ===========================================================================
def _seed(con) -> None:
    """Two entities, two projects, one purchase order, one PO line.

    TWO of each deliberately: a single entity makes every scope assertion
    vacuous, because a query that ignores its predicate entirely still returns
    the right rows when there is only one entity's worth of them.

    Every column below is derived from the migration that creates it --
    `organisation`/`entity`/`project` from 001, `budget_head`/`wbs_element`
    from 002, `purchase_order`/`po_line` from 013 -- and every NOT NULL column
    with no DEFAULT is supplied. `created_by`/`updated_by` are NOT NULL with no
    default on all of them. `wbs_path` is `ltree`, whose labels admit only
    [A-Za-z0-9_], so the hyphenated id cannot be the path.
    """
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES (%s, 'ORGFXI', 'FX Ingest Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING", (ORG,))
    for entity, project, suffix in ((ENTITY, PROJECT, "a"),
                                    (OTHER_ENTITY, OTHER_PROJECT, "b")):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (entity, ORG, entity, entity))
        con.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (project, entity, project, project))
        con.execute(
            "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (f"{HEAD}-{suffix}", entity, f"{HEAD}-{suffix}", HEAD))
        con.execute(
            "INSERT INTO wbs_element (wbs_id, project_id, wbs_code,"
            " description, wbs_path, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
            (f"{WBS}-{suffix}", project, f"{WBS}-{suffix}", WBS,
             f"fxi{suffix}"))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-SWEEP', 'sweep@example.test',"
        " 'Sweep service', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    con.commit()


@pytest.fixture()
def seeded(pg_connection):
    _seed(pg_connection)
    return pg_connection


def _session(con) -> Session:
    """A RESTRICTED session, never `Scope.system()`. An unrestricted scope
    compiles the predicate to the literal TRUE, so every scoped statement here
    would pass whether or not its scope join is correct."""
    return Session(connection=con,
                   scope=Scope(user_id="SVC-SWEEP",
                               entity_ids=frozenset({ENTITY})))


def _rate(session, *, currency: str, rate: str,
          rate_date: date = BILL_DATE,
          source: str = "RBI_REFERENCE") -> str:
    return fx.record_rate(
        session, from_currency=currency, rate_date=rate_date, rate=rate,
        rate_source=source, actor="SVC-SWEEP",
        source_reference=f"{source} {currency} for {rate_date}")["fx_rate_id"]


def _line(paise: int, *, line_id: str, tax: int = 0, freight: int = 0) -> dict:
    """A NON-PO bill line naming its own control cell.

    `bill_line.po_id`/`po_line_id` are NULLABLE precisely for this, and the
    four-column `fk_bill_line_po_line_cell` is MATCH SIMPLE, so it is not
    checked at all when `po_line_id` is NULL. Using a non-PO line keeps these
    tests about the TRANSLATION rather than about PO-line resolution.
    """
    return {"bill_line_external_id": line_id, "line_total_paise": paise,
            "non_creditable_tax_paise": tax, "freight_paise": freight,
            "quantity": "1", "wbs_id": f"{WBS}-a",
            "budget_head_id": f"{HEAD}-a"}


def _mirror(session, *, external_id: str, lines, currency: str = "INR",
            bill_date: date = BILL_DATE, doc_type: str = "BILL",
            **kwargs) -> dict:
    return procurement.mirror_bill(
        session, external_source=SOURCE_LABEL, external_id=external_id,
        bill_number="BN-" + external_id, vendor_name="SunPeak Energy GmbH",
        bill_date=bill_date, lines=lines, project_id=PROJECT,
        entity_id=ENTITY, doc_type=doc_type, source_currency=currency,
        external_last_modified=T0, payload_sha=f"sha-{external_id}",
        actor="SVC-SWEEP", now=T0, **kwargs)


@pytest.mark.pg
@PG
def test_a_eur_bill_ingested_lands_translated_with_its_source_preserved(seeded):
    """AUD-H-007, CLOSED AT THE WRITE PATH, AGAINST A REAL SERVER.

    This is the test the finding turns on. Before: `mirror_bill` wrote
    1,00,00,000 into `amount_paise` and it was read as Rs 1,00,000. After:
    `amount_paise` holds 92,50,00,000 -- Rs 92,50,000 -- and
    `source_amount_minor` still holds the EUR 1,00,000.00 it was derived from,
    with the rate, its date and its named source on the bill.
    """
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    result = _mirror(session, external_id="ZB-EUR", currency="EUR",
                     lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    assert result["translated"] is True
    assert result["base_total_paise"] == EUR_BASE_PAISE
    row = seeded.execute(
        "SELECT bl.amount_paise, bl.source_amount_minor, b.source_currency,"
        " b.fx_rate, b.fx_rate_date, b.fx_rate_source, b.fx_rate_id,"
        " b.fx_translated_at FROM bill_line bl JOIN bill b"
        " ON b.bill_id = bl.bill_id WHERE b.external_id = 'ZB-EUR'"
    ).fetchone()
    amount, source_minor, currency, rate, rate_date, source, rate_id, at = row
    assert int(amount) == EUR_BASE_PAISE, (
        "the vendor's euro figure is still standing in a column declared to "
        "be INR base paise; AUD-H-007 is not closed")
    assert int(source_minor) == EUR_MINOR, "the source amount was not preserved"
    assert currency == "EUR"
    assert rate == EUR_RATE
    assert rate_date == BILL_DATE
    assert source == "RBI_REFERENCE", "a rate with no source is not evidence"
    assert rate_id is not None and at is not None


@pytest.mark.pg
@PG
def test_the_translation_is_recorded_as_a_row_an_auditor_can_recompute_from(seeded):
    """Requirement 3. The rate, its date, its source, the source total, the
    exponent that scaled it, the base total, and the rounding rule."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    result = _mirror(session, external_id="ZB-EV", currency="EUR",
                     lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    event = fx.translation_event(_session(seeded), document_type="BILL",
                                 document_id=result["bill_id"])
    assert event is not None, "nothing recorded that the rate was applied"
    assert event["source_currency"] == "EUR"
    assert event["source_minor_exponent"] == 2
    assert int(event["source_total_minor"]) == EUR_MINOR
    assert int(event["base_total_paise"]) == EUR_BASE_PAISE
    assert event["rounding"] == fx.ROUNDING_RULE
    assert event["fx_rate_source"] == "RBI_REFERENCE"


@pytest.mark.pg
@PG
def test_replaying_the_same_bill_translates_once_and_changes_nothing(seeded):
    """THE RE-WALK IS THE DESIGN. `SweepBillDetail` resumes on a 300-second
    overlap, so every bill arrives again, and again, for as long as the
    deployment runs. Three passes must leave one translation and one figure."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    for _pass in range(3):
        _mirror(session, external_id="ZB-RE", currency="EUR",
                lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    assert seeded.execute(
        "SELECT count(*) FROM fx_translation_event WHERE document_type ="
        " 'BILL'").fetchone()[0] == 1, "three passes wrote three translations"
    rows = seeded.execute(
        "SELECT amount_paise, source_amount_minor FROM bill_line bl"
        " JOIN bill b ON b.bill_id = bl.bill_id"
        " WHERE b.external_id = 'ZB-RE'").fetchall()
    assert len(rows) == 1, "the replay duplicated a line"
    assert int(rows[0][0]) == EUR_BASE_PAISE, (
        "a replay overwrote the translated figure -- this is H-2, and it is "
        "the sharp edge the source columns in the upsert's SET list exist to "
        "make the trigger able to see")
    assert int(rows[0][1]) == EUR_MINOR


@pytest.mark.pg
@PG
def test_a_revised_source_amount_is_refused_and_never_drifts(seeded):
    """REQUIREMENT 8. The base figure was derived at the bill-date rate. A
    vendor revising the document must produce an EXCEPTION, not a quiet
    re-derivation at whatever the source now says.

    Refused at the SERVICE, with a sentence naming both totals -- and the
    database would refuse it too if the service did not, which is the next test.
    """
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    _mirror(session, external_id="ZB-REV", currency="EUR",
            lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    with pytest.raises(fx.FxError) as excinfo:
        _mirror(_session(seeded), external_id="ZB-REV", currency="EUR",
                lines=[_line(EUR_MINOR + 500_000, line_id="BL-1")])
    assert excinfo.value.code == "FX_SOURCE_DOCUMENT_CHANGED"
    seeded.rollback()

    assert int(seeded.execute(
        "SELECT bl.amount_paise FROM bill_line bl JOIN bill b"
        " ON b.bill_id = bl.bill_id WHERE b.external_id = 'ZB-REV'"
    ).fetchone()[0]) == EUR_BASE_PAISE, "the refused revision moved the money"


@pytest.mark.pg
@PG
def test_the_database_refuses_the_revision_even_with_the_service_bypassed(seeded):
    """MUTATION TEST, and the mutation is "the service check is gone".

    `trg_bill_line_source_amount_immutable` (023:557) was written for exactly
    this and was UNREACHABLE for two waves, because ingestion never wrote the
    column it watches. Writing it is the whole of the H-2 repair, and this is
    the proof that the guard is now live: the UPDATE below is what
    `_mirror_bill_line`'s upsert performs when a source amount has genuinely
    moved, issued directly so no service-layer refusal can be credited with
    the result.
    """
    import psycopg

    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    _mirror(session, external_id="ZB-TRG", currency="EUR",
            lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        seeded.execute(
            "UPDATE bill_line SET source_amount_minor = source_amount_minor + 1"
            " WHERE bill_line_id IN (SELECT bl.bill_line_id FROM bill_line bl"
            " JOIN bill b ON b.bill_id = bl.bill_id"
            " WHERE b.external_id = 'ZB-TRG')")
    seeded.rollback()


@pytest.mark.pg
@PG
def test_a_translation_event_can_be_neither_updated_nor_deleted(seeded):
    """"Applied exactly once" rests on this row, so the row is a fact.
    A privilege can be granted by a later migration; a trigger cannot be
    granted around, which is why 025 carries both."""
    import psycopg

    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    _mirror(session, external_id="ZB-IMM", currency="EUR",
            lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    for statement in (
            "UPDATE fx_translation_event SET base_total_paise = 1",
            "DELETE FROM fx_translation_event"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            seeded.execute(statement)
        seeded.rollback()


@pytest.mark.pg
@PG
def test_a_bill_with_no_rate_on_file_is_refused_and_nothing_is_written(seeded):
    """REFUSED, NOT BOOKED AT FACE VALUE. Writing it would stand a euro amount
    in a rupee column, which is AUD-H-007 exactly -- so the ingestion stops and
    the message names the currency and the date an operator has to record."""
    session = _session(seeded)
    with pytest.raises(fx.FxError) as excinfo:
        _mirror(session, external_id="ZB-NORATE", currency="EUR",
                lines=[_line(EUR_MINOR, line_id="BL-1")])
    assert excinfo.value.code == "FX_RATE_UNAVAILABLE"
    assert excinfo.value.status == 409
    seeded.rollback()
    assert seeded.execute(
        "SELECT count(*) FROM bill WHERE external_id = 'ZB-NORATE'"
    ).fetchone()[0] == 0, "a bill was written without a rate for its currency"


@pytest.mark.pg
@PG
def test_two_sources_quoting_one_day_is_refused_rather_than_resolved(seeded):
    """`ux_fx_rate_natural` admits an RBI reference rate and a bank's dealt
    rate for the same day deliberately, because both are real. Choosing between
    them is a decision about which rate the estate books at, and it is not made
    on a row ordering."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50", source="RBI_REFERENCE")
    _rate(session, currency="EUR", rate="92.80", source="BANK_DEALT")
    seeded.commit()

    with pytest.raises(fx.FxError) as excinfo:
        _mirror(_session(seeded), external_id="ZB-AMB", currency="EUR",
                lines=[_line(EUR_MINOR, line_id="BL-1")])
    assert excinfo.value.code == "FX_RATE_AMBIGUOUS"
    seeded.rollback()


@pytest.mark.pg
@PG
def test_a_rate_for_another_day_cannot_be_borrowed(seeded):
    """D-5 translates at BILL DATE. "The nearest available rate" accepted
    silently is how a translation stops being reproducible; a caller that needs
    the previous business day's rate records THAT rate against the bill date,
    with a source saying so."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.10", rate_date=date(2026, 9, 6))
    seeded.commit()

    with pytest.raises(fx.FxError) as excinfo:
        _mirror(_session(seeded), external_id="ZB-DATE", currency="EUR",
                lines=[_line(EUR_MINOR, line_id="BL-1")])
    assert excinfo.value.code == "FX_RATE_UNAVAILABLE"
    seeded.rollback()


@pytest.mark.pg
@PG
def test_a_bill_re_presented_in_another_currency_is_refused(seeded):
    """A rate is applied EXACTLY ONCE. Re-basing a bill that has already posted
    would move its CWIP figure with nothing recording the movement -- which is
    what D-5 and `trg_bill_fx_basis_immutable` both refuse."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    _rate(session, currency="USD", rate="84.20")
    _mirror(session, external_id="ZB-SWAP", currency="EUR",
            lines=[_line(EUR_MINOR, line_id="BL-1")])
    seeded.commit()

    with pytest.raises(procurement.ProcurementIngestError) as excinfo:
        _mirror(_session(seeded), external_id="ZB-SWAP", currency="USD",
                lines=[_line(EUR_MINOR, line_id="BL-1")])
    assert excinfo.value.code == "BILL_FX_BASIS_CONFLICT"
    seeded.rollback()


@pytest.mark.pg
@PG
def test_a_credit_note_is_translated_and_reverses_to_the_paisa(seeded):
    """§2.4 models a credit note as negative `amount_paise` and `bill_line` has
    no `>= 0` CHECK precisely so it can. A credit note that reversed its bill
    by all but one paisa would be an asymmetric rounding rule, permanently."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.53170000")
    _mirror(session, external_id="ZB-B", currency="EUR",
            lines=[_line(333_333, line_id="BL-1"),
                   _line(666_667, line_id="BL-2")])
    _mirror(session, external_id="ZB-CN", currency="EUR", doc_type="CREDIT_NOTE",
            lines=[_line(-333_333, line_id="CL-1"),
                   _line(-666_667, line_id="CL-2")])
    seeded.commit()

    total = seeded.execute(
        "SELECT COALESCE(SUM(bl.amount_paise + bl.non_creditable_tax_paise"
        " + bl.freight_paise), 0) FROM bill_line bl").fetchone()[0]
    assert int(total) == 0, (
        "the credit note did not reverse the bill exactly; the rounding is "
        "not symmetric about zero")


@pytest.mark.pg
@PG
def test_a_zero_exponent_currency_posts_at_its_own_scale(seeded):
    """JPY end to end. A million yen is a million yen, not ten thousand of
    anything, and reading it as hundredths understates the capital position a
    hundredfold in the direction nobody notices."""
    session = _session(seeded)
    _rate(session, currency="JPY", rate="0.55000000")
    _mirror(session, external_id="ZB-JPY", currency="JPY",
            lines=[_line(1_000_000, line_id="BL-1")])
    seeded.commit()

    assert int(seeded.execute(
        "SELECT bl.amount_paise FROM bill_line bl JOIN bill b"
        " ON b.bill_id = bl.bill_id WHERE b.external_id = 'ZB-JPY'"
    ).fetchone()[0]) == 55_000_000


@pytest.mark.pg
@PG
def test_a_three_decimal_currency_posts_at_its_own_scale(seeded):
    """KWD is thousandths, and 023 seeds its exponent as 3 for this reason."""
    session = _session(seeded)
    _rate(session, currency="KWD", rate="271.40000000")
    _mirror(session, external_id="ZB-KWD", currency="KWD",
            lines=[_line(1_000_000, line_id="BL-1")])
    seeded.commit()

    assert int(seeded.execute(
        "SELECT bl.amount_paise FROM bill_line bl JOIN bill b"
        " ON b.bill_id = bl.bill_id WHERE b.external_id = 'ZB-KWD'"
    ).fetchone()[0]) == 27_140_000


@pytest.mark.pg
@PG
def test_an_unknown_currency_refuses_rather_than_guessing_an_exponent(seeded):
    """`fx_policy.FX_UNKNOWN_CURRENCY` is seeded REFUSE. Guessing 2 for a
    currency with no `currency_denomination` row understates a JPY-shaped one a
    hundredfold, and a control that cannot be evaluated must refuse."""
    session = _session(seeded)
    with pytest.raises(fx.FxError) as excinfo:
        _mirror(session, external_id="ZB-XXX", currency="XAF",
                lines=[_line(1_000, line_id="BL-1")])
    assert excinfo.value.code == "FX_CURRENCY_UNKNOWN"
    seeded.rollback()


@pytest.mark.pg
@PG
def test_an_inr_bill_is_untouched_by_any_of_this(seeded):
    """THE REGRESSION THAT MATTERS MOST, because it is the whole existing
    estate. An INR bill is the identity translation: one figure, no rate, no
    rounding, nothing derived from anything. Its source columns stay NULL, it
    is not stamped as translated, no event row is written -- and it can still
    be REVISED and re-mirrored, which writing a source figure for it would have
    taken away."""
    session = _session(seeded)
    _mirror(session, external_id="ZB-INR",
            lines=[_line(300_000, line_id="BL-1")])
    _mirror(session, external_id="ZB-INR",
            lines=[_line(320_000, line_id="BL-1")])
    seeded.commit()

    row = seeded.execute(
        "SELECT bl.amount_paise, bl.source_amount_minor, bl.source_tax_minor,"
        " b.source_currency, b.fx_rate, b.fx_translated_at FROM bill_line bl"
        " JOIN bill b ON b.bill_id = bl.bill_id"
        " WHERE b.external_id = 'ZB-INR'").fetchone()
    amount, source_minor, source_tax, currency, rate, translated_at = row
    assert int(amount) == 320_000, (
        "an ordinary rupee invoice could not be revised and re-mirrored; the "
        "FX rules reached a document with no foreign exchange in it")
    assert source_minor is None and source_tax is None
    assert currency == "INR" and rate == 1 and translated_at is None
    assert seeded.execute(
        "SELECT count(*) FROM fx_translation_event").fetchone()[0] == 0


@pytest.mark.pg
@PG
def test_the_quarantined_value_is_held_in_rupees_and_not_in_euros(seeded):
    """`reconciliation_exception.source_paise` and the unattributed bucket are
    compared against INR figures -- `open_exception_exposure` is what blocks
    capitalisation. A quarantined EUR line held at its face value would block
    it against a number 92.5 times too small."""
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    result = _mirror(
        session, external_id="ZB-QTN", currency="EUR",
        lines=[{"bill_line_external_id": "BL-X",
                "line_total_paise": EUR_MINOR, "quantity": "1"}])
    seeded.commit()

    assert result["quarantined"] == 1
    assert int(result["quarantined_paise"]) == EUR_BASE_PAISE, (
        "the quarantine holds the untranslated euro figure, so the exposure "
        "that blocks capitalisation is stated in the wrong currency")
    assert int(seeded.execute(
        "SELECT source_paise FROM reconciliation_exception"
        " WHERE object_id = 'ZB-QTN:BL-X'").fetchone()[0]) == EUR_BASE_PAISE


@pytest.mark.pg
@PG
def test_the_repair_path_translates_all_three_money_columns_too(seeded):
    """`translate_bill` is for rows that are ALREADY WRONG, and it was wrong in
    the same way itself.

    It translated `amount_paise` and left `non_creditable_tax_paise` and
    `freight_paise` at their source-currency face value -- there is not one
    occurrence of either column anywhere in `fx.py` as 023 shipped it. Every
    consumer then summed one rupee figure and two euro figures and reported the
    total as CWIP. Migration 025's two source columns are what let the repair
    path capture all three before it overwrites them.

    Written with raw SQL rather than through `mirror_bill`, deliberately: this
    is the state of a bill mirrored BEFORE the ingestion wire existed, which is
    every foreign-currency bill in an existing deployment, and going through
    `mirror_bill` would translate it on the way in and prove nothing about the
    repair.
    """
    session = _session(seeded)
    fx_rate_id = _rate(session, currency="EUR", rate="92.50")
    seeded.execute(
        "INSERT INTO bill (bill_id, bill_number, project_id, entity_id,"
        " vendor_name, bill_date, created_by, updated_by)"
        " VALUES ('BILL-RPR', 'BN-RPR', %s, %s, 'SunPeak Energy GmbH', %s,"
        " 'T', 'T')", (PROJECT, ENTITY, BILL_DATE))
    seeded.execute(
        "INSERT INTO bill_line (bill_line_id, bill_id, wbs_id, budget_head_id,"
        " amount_paise, non_creditable_tax_paise, freight_paise, line_no,"
        " created_by, updated_by)"
        " VALUES ('BL-RPR', 'BILL-RPR', %s, %s, 1000000, 500000, 250000, 1,"
        " 'T', 'T')", (f"{WBS}-a", f"{HEAD}-a"))
    seeded.commit()

    fx.translate_bill(_session(seeded), bill_id="BILL-RPR",
                      source_currency="EUR", fx_rate_id=fx_rate_id,
                      actor="SVC-SWEEP")
    seeded.commit()

    row = seeded.execute(
        "SELECT amount_paise, non_creditable_tax_paise, freight_paise,"
        " source_amount_minor, source_tax_minor, source_freight_minor"
        " FROM bill_line WHERE bill_line_id = 'BL-RPR'").fetchone()
    amount, tax, freight, s_amount, s_tax, s_freight = (int(v) for v in row)
    assert (s_amount, s_tax, s_freight) == (1_000_000, 500_000, 250_000), (
        "the source figures were not captured before being overwritten")
    assert tax != 500_000, "the tax column was left in euros"
    assert freight != 250_000, "the freight column was left in euros"
    # 17,50,000 cents at 92.50 = Rs 16,18,750.00, and the three cells sum to it.
    assert amount + tax + freight == 161_875_000


@pytest.mark.pg
@PG
def test_a_revaluation_delta_is_measured_against_the_base_the_ledger_reports(seeded):
    """`assess_revaluation` summed `amount_paise` ALONE on the booked side and
    `source_amount_minor` alone on the source side.

    `budget_ledger_cell.actual_paise` -- the figure every report, every
    capitalisation gate and every period close quotes -- is derived from all
    three columns. A delta measured against a different base is a movement
    against a number nothing else in the product reports, recorded in
    `fx_revaluation_attempt` as though it were the exposure.
    """
    session = _session(seeded)
    _rate(session, currency="EUR", rate="92.50")
    _mirror(session, external_id="ZB-RVL", currency="EUR",
            lines=[_line(1_000_000, line_id="BL-1", tax=500_000,
                         freight=250_000)])
    seeded.commit()

    bill_id = seeded.execute(
        "SELECT bill_id FROM bill WHERE external_id = 'ZB-RVL'").fetchone()[0]
    ledger_base = int(seeded.execute(
        "SELECT SUM(amount_paise + non_creditable_tax_paise + freight_paise)"
        " FROM bill_line WHERE bill_id = %s", (bill_id,)).fetchone()[0])

    result = fx.assess_revaluation(
        _session(seeded), bill_id=bill_id, proposed_rate="93.10",
        proposed_rate_date=date(2026, 9, 30),
        proposed_rate_source="RBI_REFERENCE", actor="SVC-SWEEP")
    seeded.commit()

    assert result["booked_base_paise"] == ledger_base, (
        "the revaluation is measured against a base the ledger does not hold")
    assert result["source_amount_minor"] == 1_750_000
    assert result["applied"] is False, "D-5 does not revalue"
    assert result["outcome"] == "REFUSED"
    assert result["delta_paise"] == (
        result["proposed_base_paise"] - result["booked_base_paise"])
