"""AUD-H-007 residual: the exchange rate applied, provenanced and never revalued.

WHAT WAS WRONG
==============

``purchase_order.currency`` and ``purchase_order.exchange_rate`` have existed
since migration 013 and were NEVER APPLIED. Nothing in ``app/backend/pg/``
multiplied that rate into a money column; ``procurement.py:1988`` renders it to
a string for a JSON body and that is the whole of its use. ``bill`` -- the
document CWIP is built from -- had no currency column at all, so a EUR vendor
bill stood at its face value in rupees. The POC seed carries exactly that row
(PO-012, SunPeak Energy GmbH, EUR at 92.50, ``app/backend/db.py:540``): a bill
of EUR 1,00,000 against it was carried at Rs 1,00,000 instead of Rs 92,50,000.

Review finding H-4 is the same defect from the other side.
``api/procurement.py`` typed the field ``exchange_rate: int = 1``, so Pydantic
REJECTED 92.50 outright and no non-integer rate could be sent at all. The one
foreign-currency purchase order in the fixtures could not have been created
through the API that is supposed to create it.

WHICH HALF OF THIS FILE RUNS WHERE
==================================

MOST OF IT RUNS EVERYWHERE, and that is deliberate rather than convenient. The
translation arithmetic is pure: integer minor units, an exact ``Decimal`` rate,
one half-up quantisation. Every property that matters about it -- that a float
is refused, that JPY's zero exponent is honoured, that the rounding is
symmetric about zero, that allocated lines sum to the header to the paisa -- is
a property of our own code and is proved on every machine. A defect in any of
them is a wrong money figure, and gating that behind a database nobody here has
would put the proof in the one environment the author cannot run.

THE LIVE HALF HAS NEVER EXECUTED ON THE MACHINE THIS WAS WRITTEN ON. There is
no PostgreSQL here. Every ``@pytest.mark.pg`` test below first runs in CI's
``pg_tests`` job. A SKIP IS NOT A PASS, and the skip reason says so.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg
# is deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace. Same note as tests/test_pg_periods.py.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

import os  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from app.backend import money  # noqa: E402
from app.backend.pg import fx  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these ask what a real "
            "server does with an immutability trigger and a numeric(18,8) "
            "column, which no in-process double can answer."),
)

MIGRATION = _Path(__file__).resolve().parents[1] / "migrations" / "pg" / \
    "023_fx_translation_and_period_reopen.sql"

#: The seed's own foreign-currency case, kept as the worked example throughout:
#: EUR 1,00,000.00 at 92.50 is Rs 92,50,000.00.
EUR_MINOR = 100_00_000          # 1,00,000.00 EUR in cents
EUR_RATE = Decimal("92.50")
EUR_BASE_PAISE = 925_000_000    # Rs 92,50,000.00 in paise


# ===========================================================================
# parse_rate -- the type of a rate, decided and enforced
# ===========================================================================
def test_a_float_rate_is_refused_outright():
    """THE RULE money.py OPENS WITH, APPLIED TO THE MULTIPLIER.

    `money.to_paise` accepts a float that is exactly representable at 2dp,
    because a legacy caller may hold a money amount as one. A rate is
    different: it arrives from a rate provider as text, it is MULTIPLIED INTO
    money, and 8 decimal places is precisely where "exactly representable in
    binary" stops being a useful guard. So it is refused, with a message that
    says what to send instead.
    """
    with pytest.raises(fx.FxError) as excinfo:
        fx.parse_rate(92.50)
    assert excinfo.value.code == "FX_RATE_IS_A_FLOAT"
    assert "decimal string" in excinfo.value.message


@pytest.mark.parametrize("value", ["92.50", "92.5", Decimal("92.50"), 1, "1"])
def test_the_forms_a_rate_may_legitimately_arrive_in(value):
    parsed = fx.parse_rate(value)
    assert isinstance(parsed, Decimal)
    assert parsed == Decimal(str(value))
    assert -parsed.as_tuple().exponent == fx.RATE_SCALE, (
        "a parsed rate is quantised to the column's declared scale, so two "
        "callers sending 92.5 and 92.50000000 store the identical value")


@pytest.mark.parametrize("bad,code", [
    (None, "FX_RATE_REQUIRED"),
    (True, "FX_RATE_NOT_A_NUMBER"),
    ("nonsense", "FX_RATE_INVALID"),
    ("0", "FX_RATE_NOT_POSITIVE"),
    ("-92.50", "FX_RATE_NOT_POSITIVE"),
    ("0.000000001", "FX_RATE_TOO_PRECISE"),
])
def test_the_rates_that_are_refused_and_why(bad, code):
    with pytest.raises(fx.FxError) as excinfo:
        fx.parse_rate(bad)
    assert excinfo.value.code == code


def test_a_rate_with_more_decimals_than_the_column_holds_is_refused_not_rounded():
    """`numeric(18,8)` cannot store nine decimals. Silently rounding would mean
    the stored translation could not be recomputed from the value the caller
    sent, which is the property `fx_rate` exists to provide."""
    with pytest.raises(fx.FxError) as excinfo:
        fx.parse_rate("1.123456789")
    assert excinfo.value.code == "FX_RATE_TOO_PRECISE"
    assert "recomputed" in excinfo.value.message


# ===========================================================================
# The translation arithmetic
# ===========================================================================
def test_the_seeds_own_eur_bill_translates_to_the_figure_it_should_always_have_had():
    """THE DEFECT, IN ONE ASSERTION. Before this, the same bill stood at
    1,00,00,000 paise (Rs 1,00,000) -- its face value read as rupees."""
    assert fx.translate_to_base_paise(
        EUR_MINOR, EUR_RATE, source_minor_exponent=2) == EUR_BASE_PAISE
    assert money.format_inr(EUR_BASE_PAISE) == "₹92,50,000.00"


def test_a_zero_exponent_currency_is_not_a_hundredth_of_itself():
    """JPY HAS NO MINOR UNIT, and assuming 2 is a hundredfold error, not a
    rounding one -- in the direction that understates capital spend.

    10,00,000 yen at 0.56 is Rs 5,60,000. Read as "yen sen" at exponent 2 it
    would be Rs 5,600, which is the number that would have reached CWIP.
    """
    assert fx.SEEDED_MINOR_EXPONENTS["JPY"] == 0
    correct = fx.translate_to_base_paise(
        1_000_000, Decimal("0.56"), source_minor_exponent=0)
    assert correct == 56_000_000
    wrong = fx.translate_to_base_paise(
        1_000_000, Decimal("0.56"), source_minor_exponent=2)
    assert wrong * 100 == correct, (
        "the two readings differ by exactly the factor this column exists to "
        "record; the exponent is not a detail")


def test_a_three_decimal_currency_is_honoured_too():
    """KWD is fils: exponent 3. 1.000 KWD at 270 is Rs 270."""
    assert fx.SEEDED_MINOR_EXPONENTS["KWD"] == 3
    assert fx.translate_to_base_paise(
        1_000, Decimal("270"), source_minor_exponent=3) == 27_000


def test_the_rounding_is_half_up_and_symmetric_about_zero():
    """A CREDIT NOTE MUST REVERSE THE BILL IT REVERSES, TO THE PAISA.

    `bill_line.amount_paise` is signed by design (§2.4). If positives rounded
    away from zero and negatives toward it, the reversal of a rounded line
    would miss by one paisa, permanently, on every such line.
    """
    half_up = fx.translate_to_base_paise(1, Decimal("0.5"),
                                         source_minor_exponent=2)
    half_down = fx.translate_to_base_paise(-1, Decimal("0.5"),
                                           source_minor_exponent=2)
    assert half_up == 1
    assert half_down == -1
    assert half_up == -half_down


def test_no_float_ever_appears_in_a_translated_figure():
    result = fx.translate_to_base_paise(
        EUR_MINOR, EUR_RATE, source_minor_exponent=2)
    assert isinstance(result, int) and not isinstance(result, bool)


def test_a_float_rate_cannot_reach_the_multiplication_even_directly():
    """`parse_rate` is the front door, but a caller reaching
    `translate_to_base_paise` with a float would bypass it. It refuses too."""
    with pytest.raises(fx.FxError) as excinfo:
        fx.translate_to_base_paise(EUR_MINOR, 92.50, source_minor_exponent=2)
    assert excinfo.value.code == "FX_RATE_NOT_DECIMAL"


# ===========================================================================
# Allocation -- translate once, then distribute
# ===========================================================================
def test_the_lines_always_sum_to_the_translated_header():
    """TRANSLATE ONCE, THEN ALLOCATE. Translating twelve lines independently
    and summing them can differ from translating their total, and the header is
    the figure on the vendor's document. `money.split_pro_rata` is the same
    largest-remainder allocator capitalisation already uses."""
    lines = [33_333, 33_333, 33_334, 1, 7, 999_999]
    header, allocated = fx.translate_document(
        lines, Decimal("83.417"), source_minor_exponent=2)
    assert sum(allocated) == header
    assert len(allocated) == len(lines)


@pytest.mark.parametrize("rate", ["1", "0.00000001", "92.5", "83.417", "12345.6789"])
def test_allocation_loses_no_paisa_at_any_rate(rate):
    lines = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
    header, allocated = fx.translate_document(
        lines, Decimal(rate), source_minor_exponent=2)
    assert sum(allocated) == header


def test_a_credit_note_allocates_over_magnitudes_and_keeps_its_sign():
    """§2.4: a credit note's line amounts are negative paise. The allocator
    requires a positive total weight, so a wholly-negative document is
    allocated over magnitudes and negated back -- exactly, not approximately."""
    lines = [-33_333, -33_333, -33_334]
    header, allocated = fx.translate_document(
        lines, Decimal("92.50"), source_minor_exponent=2)
    assert header < 0
    assert all(value <= 0 for value in allocated)
    assert sum(allocated) == header


def test_a_document_with_both_signs_is_refused_rather_than_guessed_at():
    """Pro rata across mixed signs has no defensible reading -- the weights can
    sum to zero -- and picking a convention quietly would put an arbitrary
    choice inside a money allocation."""
    with pytest.raises(fx.FxError) as excinfo:
        fx.allocate_base_paise(100, [500, -200, 300])
    assert excinfo.value.code == "FX_MIXED_SIGN_ALLOCATION"
    assert "credit note" in excinfo.value.message


def test_allocation_refuses_a_total_whose_sign_the_lines_cannot_produce():
    """A positive rate cannot change the sign of an amount, so a positive total
    over negative lines means one was not derived from the other."""
    with pytest.raises(fx.FxError) as excinfo:
        fx.allocate_base_paise(100, [-500, -300])
    assert excinfo.value.code == "FX_ALLOCATION_SIGN_MISMATCH"


# ===========================================================================
# Registry mirrors -- the copies that must not drift
# ===========================================================================
def test_the_seeded_exponents_mirror_the_migration():
    """:data:`fx.SEEDED_MINOR_EXPONENTS` exists so the arithmetic above is
    provable with no database. A mirror nobody checks is a second source of
    truth, so this parses migration 023's own INSERT and compares."""
    text = MIGRATION.read_text(encoding="utf-8")
    block = text.split("INSERT INTO currency_denomination", 1)[1].split(";", 1)[0]
    seeded = {
        code: int(exponent)
        for code, exponent in re.findall(r"\('([A-Z]{3})',\s*(\d+),", block)
    }
    assert seeded, "the seed block was not found; this test would pass vacuously"
    assert seeded == fx.SEEDED_MINOR_EXPONENTS


def test_the_bill_scope_columns_match_procurements():
    """`fx.BILL_SCOPE_COLUMNS` is a deliberate local copy of
    `integration_store.PROCUREMENT_SCOPE_COLUMNS` -- so this module does not
    have to import the integration package to translate a bill. A copy that
    drifts is a scope predicate that filters on the wrong columns."""
    from app.backend.pg import integration_store

    assert fx.BILL_SCOPE_COLUMNS == integration_store.PROCUREMENT_SCOPE_COLUMNS


def test_every_policy_default_is_a_value_the_migration_permits():
    """`POLICY_DEFAULTS` fires when the row cannot be read. A default the CHECK
    constraint would refuse is a fallback that cannot be written back."""
    text = MIGRATION.read_text(encoding="utf-8")
    for key, value in fx.POLICY_DEFAULTS.items():
        assert key in text, f"{key} is not named in ck_fx_policy_known"
        assert f"'{value}'" in text, f"{value} is not a permitted value for {key}"


def test_no_policy_value_applies_a_revaluation():
    """D-5 IS NOT CONFIGURABLE. A policy table is for choices, not for a switch
    that turns a control off, and this fails the moment somebody adds one."""
    text = MIGRATION.read_text(encoding="utf-8")
    # The CONSTRAINT, not the header prose that also names it. Reading the
    # header would make this test pass on a comment.
    marker = "CONSTRAINT ck_fx_policy_known CHECK ("
    assert marker in text, "the constraint was not found; this would pass vacuously"
    body = text.split(marker, 1)[1].split("\n);", 1)[0]
    revaluation = body.split("FX_PERIOD_END_REVALUATION", 1)[1].split("OR (", 1)[0]
    assert "REFUSE_AND_RECORD" in revaluation and "RECORD_ONLY" in revaluation
    assert "APPLY" not in revaluation.upper(), (
        f"a permitted FX_PERIOD_END_REVALUATION value that applies the new "
        f"rate would reverse plan decision D-5 through a configuration row: "
        f"{revaluation!r}")


# ===========================================================================
# Migration 023's own text
# ===========================================================================
def _migration_023() -> migrate_pg.Migration:
    return migrate_pg.Migration("023", "fx_translation_and_period_reopen",
                                MIGRATION)


def test_the_rate_columns_declare_their_scale():
    """`013` used unqualified `numeric`, which has arbitrary scale: two rows
    can hold 92.5 and 92.50000000001 and both read as "the EUR rate that day",
    so the translation is not reproducible from the stored value."""
    text = MIGRATION.read_text(encoding="utf-8")
    for column in ("rate", "fx_rate", "booked_rate", "proposed_rate"):
        assert re.search(rf"\b{column}\s+numeric\(18,8\)", text), (
            f"{column} must declare numeric(18,8); an unqualified numeric "
            f"makes the stored rate unreproducible")


def test_every_new_table_carries_rls_enabled_forced_and_a_policy():
    migration = _migration_023()
    tables = set(migrate_pg._tables_created_by(migration))
    enabled, forced = migrate_pg._rls_tables_by(migration)
    policied = {table for _policy, table in
                migrate_pg._policies_created_by(migration)}
    assert tables, "no tables parsed; this test would pass vacuously"
    assert tables <= set(enabled), f"RLS not ENABLEd on {tables - set(enabled)}"
    assert tables <= set(forced), f"RLS not FORCEd on {tables - set(forced)}"
    assert tables <= policied, f"no policy on {tables - policied}"


def test_every_paise_column_the_migration_creates_is_bigint():
    """`migrate_pg._PAISE_COLUMN_RE` is anchored at `^` on each stripped field,
    so a paise column the parser cannot see never has its type verified."""
    migration = _migration_023()
    paise = migrate_pg._paise_columns_by(migration)
    assert {column for _table, column in paise} == {
        "booked_base_paise", "proposed_base_paise", "delta_paise"}
    text = MIGRATION.read_text(encoding="utf-8")
    for _table, column in paise:
        assert re.search(rf"^{column}\s+bigint", text, re.MULTILINE)


def test_the_rollback_block_deletes_its_own_ledger_row():
    """020, 021 and 022 exist because 015, 018 and 019 did not do this. A
    revert that drops the objects and leaves the ledger row makes `upgrade()`
    skip the migration forever while `assert_schema_current` reports current."""
    text = MIGRATION.read_text(encoding="utf-8")
    assert "-- ROLLBACK:" in text
    block = text.split("-- ROLLBACK:", 1)[1]
    assert "DELETE FROM schema_migrations WHERE version = '023'" in block


def test_the_migration_is_additive():
    """No DROP TABLE, DROP COLUMN or DROP CONSTRAINT outside the revert block.

    022 dropped and recreated one policy and argued for it at length; this
    migration creates and does not replace, so the check is absolute here.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    applied = text.split("-- ROLLBACK:", 1)[0]
    executable = "\n".join(
        line for line in applied.splitlines()
        if not line.lstrip().startswith("--"))
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP CONSTRAINT",
                      "DROP POLICY", "ALTER COLUMN"):
        assert forbidden not in executable.upper(), (
            f"{forbidden} appears outside the revert block; 023 is additive")


def test_023_is_the_only_migration_this_stream_adds():
    """Migration numbers are owned per stream this wave. A second file would
    collide with the stream that owns 024."""
    versions = [m.version for m in migrate_pg.discover()]
    assert versions.count("023") == 1
    assert "024" not in versions or versions.index("024") > versions.index("023")


# ===========================================================================
# H-4: the API can now send the rate the seed already contains
# ===========================================================================
def test_the_purchase_order_api_accepts_the_seeds_own_eur_rate():
    """`exchange_rate: int = 1` REJECTED 92.50, so the one foreign-currency
    purchase order in the fixtures could not be created through the API that
    creates purchase orders. That is review finding H-4."""
    from app.backend.api.procurement import _ConvertIn, _PurchaseOrderIn

    assert _ConvertIn(vendor_name="SunPeak Energy GmbH", currency="EUR",
                      exchange_rate="92.50").exchange_rate == EUR_RATE
    body = _PurchaseOrderIn(
        project_id="PRJ-03", vendor_name="SunPeak Energy GmbH",
        lines=[{"wbs_id": "W", "budget_head_id": "B", "amount_paise": 1}],
        currency="EUR", exchange_rate="92.50")
    assert body.exchange_rate == EUR_RATE


def test_the_purchase_order_api_still_refuses_a_float_rate():
    """A 422 and not a silent rounding. The same refusal `fx.parse_rate` makes,
    surfaced as a bad request rather than a server error."""
    from pydantic import ValidationError

    from app.backend.api.procurement import _ConvertIn

    with pytest.raises(ValidationError) as excinfo:
        _ConvertIn(vendor_name="V", currency="EUR", exchange_rate=92.50)
    assert "float" in str(excinfo.value)


# ===========================================================================
# Live PostgreSQL. NONE OF THESE HAS EVER EXECUTED ON THIS MACHINE.
# ===========================================================================
def _seed(connection, *, suffix: str) -> dict[str, str]:
    """One organisation, entity, project, WBS element, head, bill and bill line.

    Every column below is derived from the migration that creates it:
    `organisation`/`entity`/`project`/`app_user` from 001 and 002,
    `budget_head`/`wbs_element` from 002, `bill`/`bill_line` from 013 with
    014's additions (`bill.entity_id` is NOT NULL as of 014:389). The bill is
    written with `amount_paise` holding the SOURCE figure, which is exactly the
    state every foreign-currency bill in the product is in today.
    """
    ids = {
        "org": f"O_{suffix}", "entity": f"E_{suffix}", "project": f"PRJ_{suffix}",
        "wbs": f"W_{suffix}", "head": f"BH_{suffix}", "bill": f"BILL_{suffix}",
        "line": f"BL_{suffix}", "user": f"U_{suffix}",
    }
    ex = connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,'Org','t','t')", (ids["org"], f"OC_{suffix}"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Entity','t','t')",
       (ids["entity"], ids["org"], f"EC_{suffix}"))
    ex("INSERT INTO app_user (user_id, email, display_name, created_by, "
       "updated_by) VALUES (%s,%s,'Tester','t','t')",
       (ids["user"], f"{ids['user']}@example.test"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Project','Released','t','t')",
       (ids["project"], ids["entity"], f"C_{suffix}"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,'Head','t','t')",
       (ids["head"], ids["entity"], f"HC_{suffix}"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
       "wbs_path, created_by, updated_by) "
       "VALUES (%s,%s,%s,'Root',%s::ltree,'t','t')",
       (ids["wbs"], ids["project"], f"WC_{suffix}", f"w{suffix}"))
    ex("INSERT INTO bill (bill_id, bill_number, project_id, entity_id, "
       "vendor_name, bill_date, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'SunPeak Energy GmbH','2026-06-15','t','t')",
       (ids["bill"], f"BN_{suffix}", ids["project"], ids["entity"]))
    ex("INSERT INTO bill_line (bill_line_id, bill_id, wbs_id, budget_head_id, "
       "amount_paise, created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')",
       (ids["line"], ids["bill"], ids["wbs"], ids["head"], EUR_MINOR))
    connection.commit()
    return ids


def _record_eur_rate(database, *, actor: str) -> str:
    with database.session(Scope.system()) as session:
        return fx.record_rate(
            session, from_currency="EUR", rate_date="2026-06-15",
            rate="92.50", rate_source="RBI_REFERENCE", actor=actor,
            source_reference="RBI reference rate, 15 June 2026")["fx_rate_id"]


@pytest.mark.pg
@PG
def test_a_eur_bill_is_translated_and_its_source_survives(pg_database,
                                                          pg_connection):
    """THE RESIDUAL, CLOSED, AGAINST A REAL SERVER.

    Before: `amount_paise` held 1,00,00,000 and was read as Rs 1,00,000.
    After: `amount_paise` holds 92,50,00,000 (Rs 92,50,000) and
    `source_amount_minor` still holds the EUR 1,00,000.00 it was derived from.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    fx_rate_id = _record_eur_rate(pg_database, actor=ids["user"])

    with pg_database.session(Scope.system()) as session:
        result = fx.translate_bill(
            session, bill_id=ids["bill"], source_currency="EUR",
            fx_rate_id=fx_rate_id, actor=ids["user"])

    assert result["base_paise"] == EUR_BASE_PAISE
    row = pg_connection.execute(
        "SELECT bl.source_amount_minor, bl.amount_paise, b.source_currency, "
        "b.fx_rate, b.fx_rate_date, b.fx_rate_source, b.fx_rate_id "
        "FROM bill_line bl JOIN bill b ON b.bill_id = bl.bill_id "
        "WHERE bl.bill_line_id = %s", (ids["line"],)).fetchone()
    source_minor, base_paise, currency, rate, rate_date, rate_source, rate_id = row
    assert source_minor == EUR_MINOR, "the source amount was overwritten"
    assert base_paise == EUR_BASE_PAISE
    assert currency == "EUR"
    assert rate == EUR_RATE
    assert rate_date.isoformat() == "2026-06-15"
    assert rate_source == "RBI_REFERENCE", "a rate with no source is not evidence"
    assert rate_id == fx_rate_id


@pytest.mark.pg
@PG
def test_a_rate_for_the_wrong_date_is_refused(pg_database, pg_connection):
    """D-5 TRANSLATES AT BILL DATE. "The nearest available rate" accepted
    silently is how a translation stops being reproducible."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        wrong = fx.record_rate(
            session, from_currency="EUR", rate_date="2026-06-14",
            rate="92.10", rate_source="RBI_REFERENCE",
            actor=ids["user"])["fx_rate_id"]

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(fx.FxError) as excinfo:
            fx.translate_bill(session, bill_id=ids["bill"],
                              source_currency="EUR", fx_rate_id=wrong,
                              actor=ids["user"])
    assert excinfo.value.code == "FX_RATE_WRONG_DATE"


@pytest.mark.pg
@PG
def test_the_basis_cannot_be_edited_once_translated(pg_database, pg_connection):
    """D-5 AS STRUCTURE, NOT AS A CODE PATH. A refusal that lives only in
    `translate_bill` is one UPDATE away from not existing, and RLS does not
    apply to a superuser at all -- which is what this connection is."""
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    fx_rate_id = _record_eur_rate(pg_database, actor=ids["user"])
    with pg_database.session(Scope.system()) as session:
        fx.translate_bill(session, bill_id=ids["bill"], source_currency="EUR",
                          fx_rate_id=fx_rate_id, actor=ids["user"])

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "UPDATE bill SET fx_rate = 95.00 WHERE bill_id = %s", (ids["bill"],))
    pg_connection.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "UPDATE bill_line SET source_amount_minor = 1 "
            "WHERE bill_line_id = %s", (ids["line"],))
    pg_connection.rollback()


@pytest.mark.pg
@PG
def test_a_second_translation_is_refused(pg_database, pg_connection):
    """A second translation is a revaluation wearing a different name."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    fx_rate_id = _record_eur_rate(pg_database, actor=ids["user"])
    with pg_database.session(Scope.system()) as session:
        fx.translate_bill(session, bill_id=ids["bill"], source_currency="EUR",
                          fx_rate_id=fx_rate_id, actor=ids["user"])

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(fx.FxError) as excinfo:
            fx.translate_bill(session, bill_id=ids["bill"],
                              source_currency="EUR", fx_rate_id=fx_rate_id,
                              actor=ids["user"])
    assert excinfo.value.code == "BILL_ALREADY_TRANSLATED"


@pytest.mark.pg
@PG
def test_a_period_end_movement_is_recorded_and_not_applied(pg_database,
                                                           pg_connection):
    """NO SILENT CWIP REVALUATION, AND THE EVIDENCE SURVIVES.

    The assessment RETURNS the refusal rather than raising it. Raising inside
    the transaction that wrote `fx_revaluation_attempt` would roll the row
    back -- a refusal that destroys its own evidence, which is exactly the
    silence D-5 is written against, and which would have passed a test that
    only asserted "it raised". So this asserts THE ROW IS THERE after the
    commit, and that the booked figure did not move.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed(pg_connection, suffix=suffix)
    fx_rate_id = _record_eur_rate(pg_database, actor=ids["user"])
    with pg_database.session(Scope.system()) as session:
        fx.translate_bill(session, bill_id=ids["bill"], source_currency="EUR",
                          fx_rate_id=fx_rate_id, actor=ids["user"])

    with pg_database.session(Scope.system()) as session:
        verdict = fx.assess_revaluation(
            session, bill_id=ids["bill"], proposed_rate="95.00",
            proposed_rate_date="2026-06-30",
            proposed_rate_source="PERIOD_END_CLOSING", actor=ids["user"])

    assert verdict["outcome"] == "REFUSED"
    assert verdict["applied"] is False
    assert verdict["booked_base_paise"] == EUR_BASE_PAISE
    assert verdict["proposed_base_paise"] == 950_000_000
    assert verdict["delta_paise"] == 25_000_000

    with pytest.raises(fx.FxError) as excinfo:
        fx.raise_if_refused(verdict)
    assert excinfo.value.code == "FX_REVALUATION_REFUSED"

    stored = pg_connection.execute(
        "SELECT outcome, delta_paise FROM fx_revaluation_attempt "
        "WHERE bill_id = %s", (ids["bill"],)).fetchall()
    assert stored == [("REFUSED", 25_000_000)], (
        "the refused movement must survive as a row; a refusal whose evidence "
        "rolled back is indistinguishable from no refusal at all")

    unchanged = pg_connection.execute(
        "SELECT amount_paise FROM bill_line WHERE bill_line_id = %s",
        (ids["line"],)).fetchone()[0]
    assert unchanged == EUR_BASE_PAISE, "the booked figure was revalued"


@pytest.mark.pg
@PG
def test_a_rate_row_is_never_silently_overwritten(pg_database, pg_connection):
    """Two different rates for one (pair, date, source) is a contradiction, and
    resolving it by overwriting would make every bill already translated under
    the row disagree with the row it cites."""
    suffix = uuid.uuid4().hex[:10]
    _seed(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        first = fx.record_rate(session, from_currency="EUR",
                               rate_date="2026-06-15", rate="92.50",
                               rate_source="RBI_REFERENCE", actor="U-FIN")
    with pg_database.session(Scope.system()) as session:
        replay = fx.record_rate(session, from_currency="EUR",
                                rate_date="2026-06-15", rate="92.50",
                                rate_source="RBI_REFERENCE", actor="U-FIN")
    assert replay["fx_rate_id"] == first["fx_rate_id"]
    assert replay["created"] is False

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(fx.FxError) as excinfo:
            fx.record_rate(session, from_currency="EUR",
                           rate_date="2026-06-15", rate="95.00",
                           rate_source="RBI_REFERENCE", actor="U-FIN")
    assert excinfo.value.code == "FX_RATE_CONFLICT"


@pytest.mark.pg
@PG
def test_an_unknown_currency_refuses_rather_than_guessing_an_exponent(
        pg_database, pg_connection):
    """The seeded FX_UNKNOWN_CURRENCY default. Guessing 2 for a currency whose
    exponent is unknown is a hundredfold error waiting for the first JPY bill."""
    suffix = uuid.uuid4().hex[:10]
    _seed(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        assert fx.policy(session, "FX_UNKNOWN_CURRENCY") == "REFUSE"
        with pytest.raises(fx.FxError) as excinfo:
            fx.minor_exponent(session, "ZWL")
    assert excinfo.value.code == "FX_CURRENCY_UNKNOWN"
