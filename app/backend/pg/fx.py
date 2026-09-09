"""Foreign-currency translation: exact, provenanced, and never revalued.

AUD-H-007 residual. ``purchase_order.currency`` and
``purchase_order.exchange_rate`` have been stored since migration 013 and are
NEVER APPLIED -- nothing in ``app/backend/pg/`` multiplies that rate into a
money column; ``procurement.py`` renders it to a string for a JSON body and
that is the whole of its use. ``bill``, the document CWIP is actually built
from, had no currency column at all until migration 023, so a EUR vendor bill
was carried at its face value in rupees. The POC seed contains exactly that
row (PO-012, SunPeak Energy GmbH, EUR at 92.50, ``app/backend/db.py:540``): a
bill of EUR 1,00,000 against it stood at Rs 1,00,000 instead of Rs 92,50,000.

Four rules govern everything below.

**1. The source is preserved, never overwritten by its translation.**
``bill_line.source_amount_minor`` holds the amount in the SOURCE currency's own
minor units; ``bill_line.amount_paise`` keeps the meaning it has always had --
INR base, integer paise, signed -- because every rollup in the product sums it.
There is deliberately no second base column: two base figures diverge on the
first write that updates one and not the other. See migration 023's header (a).

**2. A rate with no provenance is not evidence.** A translation records the
rate, the date the rate is FOR, the named source it came from, and the
``fx_rate`` row it was taken from -- and copies the first three onto the bill,
so correcting a rate row can never retranslate a bill that has already posted.

**3. No float ever touches a monetary value, and a rate is not money.** Money
is integer paise (``app.backend.money``). A rate is ``numeric(18,8)`` in the
schema and :class:`decimal.Decimal` here: exact, with a DECLARED scale, so
"recompute the translation and check it" has one answer. :func:`parse_rate`
REFUSES a float outright -- unlike :func:`app.backend.money.to_paise`, which
accepts one that is exactly representable at 2dp. The difference is deliberate:
a money amount is typed by a person and a legacy caller may well hold one as a
float, whereas a rate arrives from a rate provider as text, and 8dp of binary
floating point is precisely where "exactly representable" stops being a useful
guard.

**4. Translate at bill date; do not revalue (plan decision D-5).** A rule about
what must NOT happen is only held if the attempt is visible, so
:func:`assess_revaluation` writes every would-be movement to
``fx_revaluation_attempt`` with both base figures and the delta, and then
refuses or returns according to ``fx_policy.FX_PERIOD_END_REVALUATION``. There
is no permitted policy value that APPLIES one. Migration 023 makes the same
rule structural with ``trg_bill_fx_basis_immutable`` and
``trg_bill_line_source_amount_immutable``, because a refusal that lives only in
this module is one forgotten call site away from not existing.

ROUNDING, STATED ONCE AND STORED AS A ROW. Half-up (half away from zero),
applied ONCE to the product, quantised to whole paise::

    base_paise = round_half_up(source_amount_minor
                               * rate
                               * 10 ** (2 - minor_exponent_of_source))

Half-up rather than banker's rounding because that is what Indian accounting
expects and what ``money.to_paise`` already does. Symmetric about zero, so a
credit note of -0.005 becomes -1 paise exactly as a bill of +0.005 becomes +1
-- ``bill_line.amount_paise`` is signed by design (§2.4) and an asymmetric rule
would make a reversal fail to reverse. ONCE, on the product, never per line
then summed: :func:`allocate_base_paise` translates the header total and
distributes it with :func:`app.backend.money.split_pro_rata`, so the lines sum
to the header to the paisa.

THE MINOR-UNIT EXPONENT IS NOT ALWAYS 2. JPY, KRW and CLP have none (0); KWD,
BHD, JOD and OMR have three. Assuming 2 understates a JPY bill a hundredfold,
which is not a rounding error. ``currency_denomination`` makes it a row;
:data:`SEEDED_MINOR_EXPONENTS` mirrors migration 023's seed so the arithmetic
can be proved on a machine with no PostgreSQL, and
``tests/test_pg_fx.py`` asserts the mirror against the migration's own text.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Mapping, Sequence

from ..money import MoneyError, split_pro_rata, to_paise
from . import audit as audit_mod
from . import repo
from .engine import Session

#: The reporting base. Every ``*_paise`` column in the product is this currency.
BASE_CURRENCY = "INR"

#: INR's own minor-unit exponent. Paise are hundredths, so the product of a
#: translation is quantised at 1e-2 rupees and nothing else.
BASE_MINOR_EXPONENT = 2

#: Declared scale of ``fx_rate.rate`` and ``bill.fx_rate``, both
#: ``numeric(18,8)``. A rate carrying more decimals than this is REFUSED rather
#: than silently rounded: more precision than the column can hold means the
#: caller is working to a different convention, and quietly truncating it would
#: make the stored translation unreproducible from the rate they sent.
RATE_SCALE = 8
RATE_QUANTUM = Decimal(1).scaleb(-RATE_SCALE)

#: ``numeric(18,8)`` -- 10 integral digits.
MAX_RATE = Decimal(10) ** 10

#: Mirror of migration 023's ``currency_denomination`` seed.
#:
#: THE DATABASE IS THE SOURCE OF TRUTH -- :func:`minor_exponent` reads the
#: table. This exists so the translation arithmetic is provable on a machine
#: with no PostgreSQL, which is every workstation here, and
#: ``tests/test_pg_fx.py::test_the_seeded_exponents_mirror_the_migration``
#: parses the migration's own INSERT and fails if the two drift.
SEEDED_MINOR_EXPONENTS: dict[str, int] = {
    "INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2, "SGD": 2,
    "CHF": 2, "AUD": 2, "CNY": 2, "JPY": 0, "KWD": 3,
}

#: Policy keys and their WORKING DEFAULTS, used when the row cannot be read.
#:
#: Migration 023 seeds all three, so a fallback should never fire on a migrated
#: database. It is here because the alternative to a documented default is an
#: exception from a policy lookup during a bill write, and because every
#: default below is the CONSERVATIVE branch: the fallback can only refuse more,
#: never less. That is the same posture as `Scope`'s empty frozenset meaning
#: "nothing" -- a value constructed without thinking gets the safe answer.
POLICY_DEFAULTS: dict[str, str] = {
    "FX_PERIOD_END_REVALUATION": "REFUSE_AND_RECORD",
    "FX_RATE_ROUNDING": "HALF_UP",
    "FX_UNKNOWN_CURRENCY": "REFUSE",
}

#: The four dimensions, reached through `project`.
#:
#: A LOCAL COPY of `integration_store.PROCUREMENT_SCOPE_COLUMNS`, not an
#: import: this module has no other reason to depend on the integration
#: package, and a money-translation module that cannot be imported because a
#: connector module failed to is a coupling nobody wants at a bill write.
#: `tests/test_pg_fx.py::test_the_bill_scope_columns_match_procurements`
#: asserts the two are identical, so the copy cannot drift.
BILL_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "p.entity_id", "plant": "p.plant_id",
    "location": "p.location_id", "project": "p.project_id",
}


class FxError(Exception):
    """Raised for every rejected FX call.

    Same shape as :class:`app.backend.pg.periods.PeriodServiceError` and
    :class:`app.backend.pg.budget.BudgetServiceError` -- ``code``/``status``
    are what callers and the routers match on.
    """

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400) -> None:
    raise FxError(code, message, status=status)


# ===========================================================================
# Rates
# ===========================================================================
def parse_rate(value: Any, *, field: str = "exchange_rate") -> Decimal:
    """Parse an exchange rate into an exact ``Decimal`` at :data:`RATE_SCALE`.

    Accepts ``str``, ``int`` and ``Decimal``. REFUSES ``float`` and ``bool``.

    The float refusal is the point of this function existing separately from
    :func:`app.backend.money.to_paise`, which accepts a float that is exactly
    representable at 2dp. Review finding H-4 is the mirror image of the same
    problem from the other side: ``api/procurement.py`` typed the field as
    ``int``, so 92.50 was REJECTED outright and no non-integer rate could be
    sent at all -- which is an independent reason the rate has never been
    applied to anything. Both are fixed by naming the type honestly: a rate is
    a decimal, sent as a string (or an integer, for the identity rate 1).

    A rate with more than :data:`RATE_SCALE` decimals is refused rather than
    rounded. ``numeric(18,8)`` cannot store it, so accepting it would mean the
    translation could not be recomputed from the value the caller sent.
    """
    if value is None:
        _err("FX_RATE_REQUIRED", f"{field} is required.")
    if isinstance(value, bool):
        _err("FX_RATE_NOT_A_NUMBER", f"{field} must be a number, not a boolean.")
    if isinstance(value, float):
        _err("FX_RATE_IS_A_FLOAT",
             f"{field} was sent as a float ({value!r}). An exchange rate is "
             f"multiplied into money, so a float rate makes the product a "
             f"float. Send it as a decimal string, for example \"92.50\".")

    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value).strip().replace(",", ""))
        except (InvalidOperation, AttributeError, TypeError):
            _err("FX_RATE_INVALID", f"{field} is not a valid rate: {value!r}")

    if not dec.is_finite():
        _err("FX_RATE_INVALID", f"{field} must be a finite rate.")
    if dec <= 0:
        _err("FX_RATE_NOT_POSITIVE",
             f"{field} must be greater than zero; got {dec}. "
             f"ck_fx_rate_positive refuses the same value at the database.")
    if dec >= MAX_RATE:
        _err("FX_RATE_OUT_OF_RANGE",
             f"{field} exceeds what numeric(18,{RATE_SCALE}) can hold: {dec}.")

    with localcontext() as ctx:
        ctx.prec = 34
        quantised = dec.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
    if quantised != dec:
        _err("FX_RATE_TOO_PRECISE",
             f"{field} carries more than {RATE_SCALE} decimal places ({dec}). "
             f"fx_rate.rate is numeric(18,{RATE_SCALE}), so this value cannot "
             f"be stored exactly and the translation could not be recomputed "
             f"from it. Round it deliberately before sending it.")
    return quantised


def minor_exponent(session: Session, currency_code: str) -> int:
    """The minor-unit exponent for ``currency_code``, from the database.

    An unknown currency is decided by ``fx_policy.FX_UNKNOWN_CURRENCY``, whose
    seeded default is REFUSE. Guessing 2 for a JPY amount understates it a
    hundredfold, and a control that cannot be evaluated must refuse -- the same
    reasoning ``periods._has_open_reconciliation_exceptions`` gives for raising
    rather than returning a falsy "nothing blocks this".
    """
    code = _currency_code(currency_code)
    row = session.fetchone(  # scope-exempt: currency_denomination is organisation-wide reference data with no dimension column; migration 023 scopes it with capex_principal_present(), exactly as 006 scopes reference data and 014 scopes procurement_policy
        "SELECT minor_exponent, is_supported FROM currency_denomination "
        "WHERE currency_code = %s", (code,))
    if row is not None:
        exponent, is_supported = row
        if not is_supported:
            _err("FX_CURRENCY_WITHDRAWN",
                 f"{code} is recorded but no longer supported for new "
                 f"translations. Its exponent is retained so bills already "
                 f"translated under it stay verifiable.")
        return int(exponent)

    if policy(session, "FX_UNKNOWN_CURRENCY") == "ASSUME_MINOR_EXPONENT_2":
        return 2
    _err("FX_CURRENCY_UNKNOWN",
         f"{code} has no currency_denomination row, so its minor-unit "
         f"exponent is UNKNOWN. Assuming 2 understates a JPY amount a "
         f"hundredfold and a KWD amount tenfold, so this refuses instead. "
         f"Add the currency, or set fx_policy.FX_UNKNOWN_CURRENCY to "
         f"ASSUME_MINOR_EXPONENT_2 for a backfill of a known two-decimal set.")
    raise AssertionError("unreachable")  # pragma: no cover


def policy(session: Session, key: str) -> str:
    """One ``fx_policy`` value, or its documented working default.

    See :data:`POLICY_DEFAULTS`: every fallback is the conservative branch, so
    an unreadable policy table can only make the product refuse more.
    """
    if key not in POLICY_DEFAULTS:
        raise KeyError(f"{key!r} is not an fx_policy key; "
                       f"known keys are {sorted(POLICY_DEFAULTS)}")
    try:
        row = session.fetchone(  # scope-exempt: fx_policy is organisation-wide configuration with no dimension column, scoped by capex_principal_present() in migration 023 -- the posture procurement_policy (014) established
            "SELECT policy_value FROM fx_policy WHERE policy_key = %s", (key,))
    except Exception:                       # noqa: BLE001 -- see docstring
        return POLICY_DEFAULTS[key]
    return str(row[0]) if row is not None else POLICY_DEFAULTS[key]


def _currency_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        _err("FX_CURRENCY_MALFORMED",
             f"{value!r} is not a three-letter ISO 4217 currency code. "
             f"ck_currency_denomination_code refuses the same value at the "
             f"database.")
    return code


# ===========================================================================
# The translation itself
# ===========================================================================
def translate_to_base_paise(source_amount_minor: int, rate: Decimal, *,
                            source_minor_exponent: int) -> int:
    """``source_amount_minor`` in its own minor units -> INR base paise.

    Exact throughout: integer minor units times an exact ``Decimal`` rate
    times an exact power of ten, quantised ONCE, half away from zero. No float
    appears at any step, and the intermediate is never rounded -- rounding a
    partial product and then rounding the result is how two implementations of
    "the same" rule disagree by a paisa on a large invoice.

    SIGN. ``ROUND_HALF_UP`` in :mod:`decimal` is half AWAY FROM ZERO, so -0.5
    paise becomes -1 and +0.5 becomes +1. That symmetry is required, not
    incidental: a credit note reverses a bill line, and a rule that rounded
    negatives toward zero would make the reversal of a rounded line fail to
    reverse it by one paisa, permanently.
    """
    if isinstance(source_amount_minor, bool) or not isinstance(source_amount_minor, int):
        _err("FX_SOURCE_AMOUNT_NOT_INTEGER",
             f"source_amount_minor must be an integer number of the source "
             f"currency's minor units; got {source_amount_minor!r}. Parse "
             f"decimal input with money.to_paise (for a 2dp currency) before "
             f"calling this.")
    if not isinstance(rate, Decimal):
        _err("FX_RATE_NOT_DECIMAL",
             f"rate must be an exact Decimal; got {type(rate).__name__}. "
             f"Use fx.parse_rate.")
    if rate <= 0:
        _err("FX_RATE_NOT_POSITIVE", f"rate must be greater than zero; got {rate}.")
    if not (0 <= int(source_minor_exponent) <= 4):
        _err("FX_MINOR_EXPONENT_OUT_OF_RANGE",
             f"source_minor_exponent must be 0..4 (ISO 4217 uses 0, 2 and 3); "
             f"got {source_minor_exponent!r}.")

    scale = BASE_MINOR_EXPONENT - int(source_minor_exponent)
    with localcontext() as ctx:
        ctx.prec = 60         # ample: 18 digits of money x 18 of rate x 10^4
        product = Decimal(int(source_amount_minor)) * rate * (Decimal(10) ** scale)
        paise = product.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    result = int(paise)

    # Reuse money.py's ledger range rather than inventing a second one: a
    # translated amount is a money amount and must satisfy the same bound the
    # untranslated ones do.
    try:
        to_paise(Decimal(result) / 100, field="translated amount",
                 allow_negative=True)
    except MoneyError as exc:
        _err("FX_TRANSLATED_AMOUNT_OUT_OF_RANGE", str(exc))
    return result


def allocate_base_paise(total_base_paise: int,
                        source_line_minors: Sequence[int]) -> list[int]:
    """Distribute one translated header total across its lines, losing nothing.

    TRANSLATE ONCE, THEN ALLOCATE -- never translate each line and sum. Both
    are defensible in isolation and they disagree: translating twelve lines
    independently and summing them can differ from translating their total by
    up to six paise, and the header is the figure on the vendor's document.
    ``money.split_pro_rata`` is the same largest-remainder allocator
    capitalisation and common-cost distribution already use, so the sum of the
    result always equals ``total_base_paise`` exactly.

    SIGNS. ``split_pro_rata`` requires a positive total weight, and a credit
    note's lines are all negative (§2.4). A wholly-negative document is
    allocated over magnitudes and negated back, which is exact. A document
    with BOTH signs is REFUSED rather than guessed at: pro rata over mixed
    signs has no defensible reading, the weights can sum to zero, and quietly
    picking one convention would put an arbitrary choice inside a money
    allocation.
    """
    minors = [int(m) for m in source_line_minors]
    if not minors:
        _err("FX_NO_LINES", "cannot allocate a translated total across no lines.")

    positives = any(m > 0 for m in minors)
    negatives = any(m < 0 for m in minors)
    if positives and negatives:
        _err("FX_MIXED_SIGN_ALLOCATION",
             f"this document has both positive and negative source line "
             f"amounts ({sum(1 for m in minors if m > 0)} positive, "
             f"{sum(1 for m in minors if m < 0)} negative), and there is no "
             f"defensible pro-rata split across mixed signs -- the weights can "
             f"even sum to zero. Split it into a bill and a credit note, which "
             f"is what §2.4 already models.")

    if all(m == 0 for m in minors):
        if total_base_paise != 0:
            _err("FX_ZERO_WEIGHT_NONZERO_TOTAL",
                 f"every source line amount is zero but the translated total "
                 f"is {total_base_paise} paise, so there is nothing to "
                 f"allocate it against.")
        return [0] * len(minors)

    sign = -1 if negatives else 1
    if sign * total_base_paise < 0:
        _err("FX_ALLOCATION_SIGN_MISMATCH",
             f"the translated total ({total_base_paise} paise) and the source "
             f"lines disagree in sign. A positive rate cannot change the sign "
             f"of an amount, so one of the two was not derived from the other.")

    weights = [sign * m for m in minors]
    allocated = split_pro_rata(sign * int(total_base_paise), weights)
    return [sign * a for a in allocated]


def translate_document(source_line_minors: Sequence[int], rate: Decimal, *,
                       source_minor_exponent: int) -> tuple[int, list[int]]:
    """``(header_base_paise, per_line_base_paise)`` for one document.

    The whole rule in one call, so no caller can perform half of it: translate
    the summed source ONCE, then allocate. ``sum(per_line) == header`` always.
    """
    total_minor = sum(int(m) for m in source_line_minors)
    header = translate_to_base_paise(
        total_minor, rate, source_minor_exponent=source_minor_exponent)
    lines = allocate_base_paise(header, source_line_minors)
    return header, lines


# ===========================================================================
# Writing a translated bill
# ===========================================================================
def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


def record_rate(session: Session, *, from_currency: str, rate_date: date,
                rate: Any, rate_source: str, actor: str,
                to_currency: str = BASE_CURRENCY,
                source_reference: str | None = None) -> dict[str, Any]:
    """Store one rate with its provenance, or return the row already stored.

    Idempotent on ``ux_fx_rate_natural`` -- (pair, date, source). Re-running a
    rate fetch must not mint a second row for the same fact, and must not
    OVERWRITE the stored rate either: a rate row that silently changed would
    retranslate nothing (bills copy the rate) but would make every stored
    translation look wrong against it, which is worse than either.
    """
    frm = _currency_code(from_currency)
    to = _currency_code(to_currency)
    if frm == to:
        _err("FX_IDENTITY_RATE",
             f"{frm} to {to} is the identity translation and is never stored; "
             f"ck_fx_rate_not_identity refuses it at the database. An INR bill "
             f"carries fx_rate = 1 and no fx_rate_id.")
    parsed = parse_rate(rate, field="rate")
    if not str(rate_source or "").strip():
        _err("FX_RATE_SOURCE_REQUIRED",
             "a rate must name where it came from. A rate with no provenance "
             "is not evidence.")

    existing = session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        "SELECT fx_rate_id, rate FROM fx_rate WHERE from_currency = %s "
        "AND to_currency = %s AND rate_date = %s AND rate_source = %s",
        (frm, to, rate_date, rate_source))
    if existing is not None:
        fx_rate_id, stored = existing
        if Decimal(stored) != parsed:
            _err("FX_RATE_CONFLICT",
                 f"{rate_source} already quoted {frm}/{to} for {rate_date} as "
                 f"{stored}, and this call says {parsed}. A stored rate is "
                 f"never overwritten: bills translated under it would then "
                 f"disagree with the row they cite. Record the correction as a "
                 f"new source, or a new rate_date.",
                 status=409)
        return {"fx_rate_id": fx_rate_id, "rate": str(parsed), "created": False}

    fx_rate_id = _new_id("FXR")
    session.execute(
        "INSERT INTO fx_rate (fx_rate_id, from_currency, to_currency, "
        "rate_date, rate, rate_source, source_reference, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (fx_rate_id, frm, to, rate_date, parsed, rate_source,
         source_reference, actor))
    audit_mod.append(
        session, actor, "FX_RATE_RECORDED", "FX_RATE", fx_rate_id,
        f"{frm}/{to} {parsed} for {rate_date} from {rate_source}"
        + (f" ({source_reference})" if source_reference else ""))
    return {"fx_rate_id": fx_rate_id, "rate": str(parsed), "created": True}


def _load_bill(session: Session, bill_id: str) -> tuple:
    """The bill header, SCOPED. Out of scope is indistinguishable from absent.

    Reached through `project` and filtering all four dimensions, the shape 013
    established for every procurement document -- see
    :data:`BILL_SCOPE_COLUMNS`.
    """
    row = repo.query_one(
        session,
        """
        SELECT b.bill_id, b.project_id, p.entity_id, b.bill_date,
               b.source_currency, b.fx_rate, b.fx_rate_date, b.fx_rate_id,
               b.fx_translated_at, b.accounting_status
        FROM bill b
        JOIN project p ON p.project_id = b.project_id
        WHERE b.bill_id = %(bill_id)s AND {scope}
        """,
        {"bill_id": bill_id},
        columns=BILL_SCOPE_COLUMNS,
    )
    if row is None:
        _err("BILL_NOT_FOUND", f"Bill {bill_id} does not exist.", status=404)
    return row


def translate_bill(session: Session, *, bill_id: str, source_currency: str,
                   fx_rate_id: str, actor: str) -> dict[str, Any]:
    """Apply a recorded rate to a bill, ONCE, at BILL DATE.

    Writes ``bill_line.source_amount_minor`` from the amount already on each
    line -- which, on an untranslated bill, IS the source amount, mis-stored as
    though it were rupees -- and then writes the translated INR figure back to
    ``bill_line.amount_paise``. That ordering is what makes this safe to run on
    the existing rows: the source is captured before the column that held it is
    overwritten, in one transaction, and ``trg_bill_line_source_amount_immutable``
    then refuses any second attempt.

    REFUSES a bill that is already translated. D-5 translates at bill date and
    does not revalue; a second translation is a revaluation wearing a different
    name, and it belongs in :func:`assess_revaluation`.

    THE RATE MUST BE FOR THE BILL DATE. A rate for another day is not the
    bill-date rate however close it is, and accepting "the nearest available"
    silently is how a translation stops being reproducible. A caller that
    genuinely needs the previous business day's rate records THAT rate against
    the bill date, with its source saying so.
    """
    (bill_id_, project_id, entity_id, bill_date, existing_currency,
     _existing_rate, _existing_rate_date, _existing_rate_id,
     translated_at, _accounting_status) = _load_bill(session, bill_id)

    if translated_at is not None:
        _err("BILL_ALREADY_TRANSLATED",
             f"Bill {bill_id} was translated at {translated_at.isoformat()} "
             f"and carries {existing_currency}. Plan decision D-5 translates "
             f"at bill date and does not revalue, so there is no second "
             f"translation. A period-end movement goes through "
             f"assess_revaluation, which records it.",
             status=409)

    currency = _currency_code(source_currency)
    if currency == BASE_CURRENCY:
        _err("FX_IDENTITY_TRANSLATION",
             f"Bill {bill_id} is in {BASE_CURRENCY}; there is nothing to "
             f"translate. An INR bill carries fx_rate = 1 and no fx_rate_id, "
             f"which is what ck_bill_fx_provenance requires.")

    rate_row = session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        "SELECT from_currency, to_currency, rate_date, rate, rate_source "
        "FROM fx_rate WHERE fx_rate_id = %s", (fx_rate_id,))
    if rate_row is None:
        _err("FX_RATE_NOT_FOUND", f"No fx_rate row {fx_rate_id}.", status=404)
    frm, to, rate_date, rate, rate_source = rate_row

    if frm != currency or to != BASE_CURRENCY:
        _err("FX_RATE_WRONG_PAIR",
             f"fx_rate {fx_rate_id} is {frm}/{to}; this bill is "
             f"{currency}/{BASE_CURRENCY}.")
    if rate_date != bill_date:
        _err("FX_RATE_WRONG_DATE",
             f"fx_rate {fx_rate_id} is the {frm} rate for {rate_date}; bill "
             f"{bill_id} is dated {bill_date}. D-5 translates at BILL DATE, "
             f"and a rate for another day is not that rate however close it "
             f"is. Record the rate you intend to use against {bill_date}, with "
             f"a source that says what it is.")

    parsed = parse_rate(rate, field="fx_rate.rate")
    exponent = minor_exponent(session, currency)

    lines = repo.query(
        session,
        """
        SELECT bl.bill_line_id, bl.amount_paise, bl.source_amount_minor
        FROM bill_line bl
        JOIN bill b ON b.bill_id = bl.bill_id
        JOIN project p ON p.project_id = b.project_id
        WHERE bl.bill_id = %(bill_id)s AND {scope}
        ORDER BY bl.bill_line_id
        """,
        {"bill_id": bill_id},
        columns=BILL_SCOPE_COLUMNS,
    )
    if not lines:
        _err("BILL_HAS_NO_LINES",
             f"Bill {bill_id} has no lines, so there is nothing to translate.")
    if any(existing is not None for _id, _amt, existing in lines):
        _err("BILL_LINE_ALREADY_HAS_SOURCE",
             f"Bill {bill_id} has at least one line whose source_amount_minor "
             f"is already set, but the header records no translation. That is "
             f"a half-written translation, not a fresh one; it is refused "
             f"rather than completed on a guess.",
             status=409)

    source_minors = [int(amount) for _id, amount, _existing in lines]
    header_base, line_bases = translate_document(
        source_minors, parsed, source_minor_exponent=exponent)

    for (line_id, _amount, _existing), base in zip(lines, line_bases,
                                                   strict=True):
        session.execute(
            "UPDATE bill_line SET source_amount_minor = %s, amount_paise = %s, "
            "updated_at = now(), updated_by = %s WHERE bill_line_id = %s",
            (int(_amount), int(base), actor, line_id))

    now = datetime.now(timezone.utc)
    session.execute(
        "UPDATE bill SET source_currency = %s, fx_rate_id = %s, fx_rate = %s, "
        "fx_rate_date = %s, fx_rate_source = %s, fx_translated_at = %s, "
        "updated_at = now(), updated_by = %s WHERE bill_id = %s",
        (currency, fx_rate_id, parsed, rate_date, rate_source, now, actor,
         bill_id))

    audit_mod.append(
        session, actor, "BILL_FX_TRANSLATED", "BILL", bill_id,
        f"{currency} {sum(source_minors)} minor units at {parsed} "
        f"({rate_source}, {rate_date}) = {header_base} paise across "
        f"{len(lines)} line(s)")

    return {
        "bill_id": bill_id_, "project_id": project_id, "entity_id": entity_id,
        "source_currency": currency, "source_amount_minor": sum(source_minors),
        "source_minor_exponent": exponent,
        "fx_rate": str(parsed), "fx_rate_date": rate_date.isoformat(),
        "fx_rate_source": rate_source, "fx_rate_id": fx_rate_id,
        "base_paise": header_base, "line_base_paise": line_bases,
        "translated_at": now.isoformat(),
    }


# ===========================================================================
# D-5: no silent revaluation
# ===========================================================================
def assess_revaluation(session: Session, *, bill_id: str, proposed_rate: Any,
                       proposed_rate_date: date, proposed_rate_source: str,
                       actor: str, period_id: str | None = None,
                       ) -> dict[str, Any]:
    """Would a movement change this bill's booked base figure? Record it.

    THE POINT IS THAT IT NEVER APPLIES ONE. Plan decision D-5 translates at
    bill date and does not revalue. This function exists so that the attempt is
    a row rather than an absence: ``fx_revaluation_attempt`` stores the booked
    rate and base, the proposed rate and base, and the delta, and
    ``ck_fx_revaluation_delta`` makes a stored delta that disagrees with them
    itself a finding.

    ``fx_policy.FX_PERIOD_END_REVALUATION`` decides the recorded ``outcome``:
    REFUSED (the seeded default) or RECORDED_NOT_APPLIED, for a reporting-only
    exposure run. Neither applies the new rate.

    THIS FUNCTION DOES NOT RAISE FOR A REFUSAL, AND THAT IS THE WHOLE DESIGN.
    The obvious shape -- write the attempt row, then raise -- DESTROYS THE ROW
    IT JUST WROTE: the exception unwinds out of ``Database.session()``, the
    transaction rolls back, and the evidence of the refused movement goes with
    it. A refusal that rolls back its own evidence leaves exactly the silence
    D-5 is written against, and it would have passed a test that only asserted
    "it raised".

    So the refusal is a RETURN VALUE. The caller commits the attempt row and
    then, if it wants an exception rather than a verdict, calls
    :func:`raise_if_refused` -- outside the transaction that recorded it.
    ``result["outcome"]`` is ``"REFUSED"``, ``"RECORDED_NOT_APPLIED"`` or
    ``"NO_MOVEMENT"``, and ``result["applied"]`` is ``False`` in every case
    because nothing here ever applies one.

    A movement of ZERO is recorded as no movement and neither refused nor
    stored: there is nothing to be silent about.
    """
    (bill_id_, _project_id, entity_id, _bill_date, currency, booked_rate,
     booked_rate_date, _rate_id, translated_at, _status) = _load_bill(
        session, bill_id)

    if translated_at is None:
        _err("BILL_NOT_TRANSLATED",
             f"Bill {bill_id} has never been translated, so there is no booked "
             f"figure for a movement to change. Translate it first.",
             status=409)
    if currency == BASE_CURRENCY:
        _err("FX_IDENTITY_TRANSLATION",
             f"Bill {bill_id} is in {BASE_CURRENCY}; no rate movement can "
             f"change its base figure.")

    proposed = parse_rate(proposed_rate, field="proposed_rate")
    booked = parse_rate(booked_rate, field="bill.fx_rate")
    exponent = minor_exponent(session, currency)

    row = repo.query_one(
        session,
        """
        SELECT COALESCE(SUM(bl.source_amount_minor), 0)::bigint,
               COALESCE(SUM(bl.amount_paise), 0)::bigint
        FROM bill_line bl
        JOIN bill b ON b.bill_id = bl.bill_id
        JOIN project p ON p.project_id = b.project_id
        WHERE bl.bill_id = %(bill_id)s AND {scope}
        """,
        {"bill_id": bill_id},
        columns=BILL_SCOPE_COLUMNS,
    )
    source_minor, booked_base = (int(row[0]), int(row[1])) if row else (0, 0)

    proposed_base = translate_to_base_paise(
        source_minor, proposed, source_minor_exponent=exponent)
    delta = proposed_base - booked_base

    result = {
        "bill_id": bill_id_, "entity_id": entity_id,
        "source_currency": currency, "source_amount_minor": source_minor,
        "booked_rate": str(booked), "booked_base_paise": booked_base,
        "proposed_rate": str(proposed), "proposed_base_paise": proposed_base,
        "delta_paise": delta, "applied": False,
    }
    if delta == 0:
        result["outcome"] = "NO_MOVEMENT"
        return result

    detail = (
        f"{currency} {source_minor} minor units booked at {booked} "
        f"({booked_rate_date}) = {booked_base} paise; proposed {proposed} "
        f"({proposed_rate_date}, {proposed_rate_source}) = {proposed_base} "
        f"paise; delta {delta} paise. NOT APPLIED: plan decision D-5 "
        f"translates at bill date and does not revalue.")

    mode = policy(session, "FX_PERIOD_END_REVALUATION")
    outcome = "REFUSED" if mode == "REFUSE_AND_RECORD" else "RECORDED_NOT_APPLIED"

    attempt_id = _new_id("FXV")
    session.execute(
        """
        INSERT INTO fx_revaluation_attempt (
            attempt_id, entity_id, period_id, bill_id, source_currency,
            booked_rate, booked_rate_date, proposed_rate, proposed_rate_date,
            proposed_rate_source, booked_base_paise, proposed_base_paise,
            delta_paise, outcome, detail, attempted_by)
        VALUES (%(attempt_id)s, %(entity_id)s, %(period_id)s, %(bill_id)s,
                %(currency)s, %(booked_rate)s, %(booked_rate_date)s,
                %(proposed_rate)s, %(proposed_rate_date)s, %(source)s,
                %(booked_base)s, %(proposed_base)s, %(delta)s, %(outcome)s,
                %(detail)s, %(actor)s)
        """,
        {"attempt_id": attempt_id, "entity_id": entity_id,
         "period_id": period_id, "bill_id": bill_id, "currency": currency,
         "booked_rate": booked, "booked_rate_date": booked_rate_date,
         "proposed_rate": proposed, "proposed_rate_date": proposed_rate_date,
         "source": proposed_rate_source, "booked_base": booked_base,
         "proposed_base": proposed_base, "delta": delta, "outcome": outcome,
         "detail": detail, "actor": actor},
    )
    audit_mod.append(
        session, actor, "FX_REVALUATION_REFUSED", "BILL", bill_id, detail)

    result["outcome"] = outcome
    result["attempt_id"] = attempt_id
    result["detail"] = detail
    return result


def raise_if_refused(result: Mapping[str, Any]) -> None:
    """Turn a REFUSED :func:`assess_revaluation` verdict into an ``FxError``.

    SEPARATE FROM THE ASSESSMENT, ON PURPOSE. Call it AFTER the transaction
    that recorded the attempt has committed. Raising inside that transaction
    rolls back the ``fx_revaluation_attempt`` row, which is the one artefact
    that makes a refused revaluation visible rather than silent -- see
    :func:`assess_revaluation`.
    """
    if result.get("outcome") == "REFUSED":
        raise FxError(
            "FX_REVALUATION_REFUSED",
            f"Bill {result.get('bill_id')}: {result.get('detail')} "
            f"Recorded as {result.get('attempt_id')}.",
            status=409)


def revaluation_exposure(session: Session, *, entity_id: str) -> dict[str, Any]:
    """What the refused movements add up to, for one entity.

    Reported rather than posted. The whole point of D-5 is that this number
    never reaches the ledger; the whole point of ``fx_revaluation_attempt`` is
    that it is nonetheless answerable.
    """
    row = repo.query_one(
        session,
        """
        SELECT COUNT(*)::bigint,
               COALESCE(SUM(a.delta_paise), 0)::bigint
        FROM fx_revaluation_attempt a
        WHERE a.entity_id = %(entity_id)s AND {scope}
        """,
        {"entity_id": entity_id},
        columns={"entity": "a.entity_id", "plant": None,
                 "location": None, "project": None},
    )
    count, total = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"entity_id": entity_id, "attempts": count,
            "unapplied_delta_paise": total,
            "note": ("Never posted. Plan decision D-5 translates at bill date "
                     "and does not revalue; this is the exposure that decision "
                     "leaves, stated rather than hidden.")}


def bill_fx_summary(session: Session, bill_id: str) -> Mapping[str, Any]:
    """Everything an auditor needs to recompute one bill's translation."""
    (bill_id_, project_id, entity_id, bill_date, currency, rate, rate_date,
     rate_id, translated_at, accounting_status) = _load_bill(session, bill_id)
    return {
        "bill_id": bill_id_, "project_id": project_id, "entity_id": entity_id,
        "bill_date": bill_date.isoformat() if bill_date else None,
        "accounting_status": accounting_status,
        "source_currency": currency,
        "fx_rate": str(rate) if rate is not None else None,
        "fx_rate_date": rate_date.isoformat() if rate_date else None,
        "fx_rate_id": rate_id,
        "translated_at": translated_at.isoformat() if translated_at else None,
        "rounding": "HALF_UP, half away from zero, applied once to the product",
        "rate_scale": RATE_SCALE,
    }


# ===========================================================================
# INGESTION-TIME TRANSLATION -- the path production actually takes
# ===========================================================================
# WHY THIS EXISTS ALONGSIDE `translate_bill`, AND WHY IT IS NOT THE SAME CALL.
#
# `translate_bill` is a REPAIR. It reads the figure sitting in
# `bill_line.amount_paise` -- which, on a bill mirrored before this section
# existed, IS the vendor's source amount mis-stored as though it were rupees --
# captures it into `source_amount_minor`, and writes the translation back over
# it. That is the right shape for rows that are ALREADY WRONG and the wrong
# shape for a row being written for the first time: it requires the
# untranslated figure to be sitting in the base column, which is the very state
# AUD-H-007 names.
#
# AT INGESTION THE SOURCE AMOUNT IS IN HAND AND THE BASE COLUMN IS EMPTY. The
# vendor's currency and amounts arrive together in one payload, so the base
# figure is derived BEFORE the first write and the untranslated figure never
# enters `amount_paise` at all. Nothing to repair, because nothing was ever
# wrong.
#
# THE ARITHMETIC IS NOT DUPLICATED. Both paths end at `translate_document`,
# which is the one implementation of the rule; what differs is only where the
# source amounts come from and whether a row already exists. A second
# implementation of "multiply by the rate" is exactly the divergence rule 1
# warns about, written in code instead of in columns.

#: Document kinds `fx_translation_event` accepts. Mirrors migration 025's
#: `ck_fx_translation_event_document_type`;
#: `tests/test_pg_fx_ingest.py::test_the_document_types_mirror_the_migrations_own_check`
#: parses the migration's own CHECK and fails if the two drift -- the same
#: guard :data:`SEEDED_MINOR_EXPONENTS` carries for the exponents.
#:
#: ONE KIND, AND THE SHORTNESS OF THIS TUPLE IS THE POINT. The vendor bill is
#: the only inbound document that both carries money which may be in a foreign
#: currency AND has an ingestion path to translate it on.
#:
#: The PURCHASE ORDER carries a currency and a rate (013:381-384) and its rate
#: is still applied to nothing -- but it is ORIGINATED here and EMITTED to
#: Zoho, never ingested: `procurement_services._write_po` is its only writer
#: and `create_po`/`convert_pr_to_po` its only callers, both reached from the
#: API. The GRN has the ingestion path and no currency anywhere in the
#: contract: `dto.ReceiveDTO` is the only inbound money-document DTO without a
#: `currency_code`, and `grn_line.amount_paise` feeds `received_paise` all the
#: same. Both are reported as gaps rather than half-built here.
#:
#: ADDING A KIND IS A MIGRATION, not a string appended to this tuple, which is
#: what forces the schema change and its writer to arrive together. 023 added
#: `bill.source_currency` and `bill_line.source_amount_minor` with no writer,
#: and the guard built on the second of them stayed unreachable for two waves.
TRANSLATABLE_DOCUMENT_TYPES: tuple[str, ...] = ("BILL",)

#: The rounding rule, named ONCE.
#:
#: It is the only value `fx_policy.FX_RATE_ROUNDING` permits (migration 023),
#: it is what :func:`translate_to_base_paise` implements, and it is what
#: `fx_translation_event.rounding` records and `ck_fx_translation_rounding`
#: constrains. Three names for one rule is already one too many, so every one
#: of those places is written from this constant or checked against it.
ROUNDING_RULE = "HALF_UP"


class TranslationBasis:
    """The rate a document is translated by, resolved ONCE, with its provenance.

    Carried by value through the whole of one ingestion, so a document cannot
    have its rate resolved twice and get two answers. That is requirement 4 --
    "apply a rate exactly once" -- expressed as a type rather than as a
    convention somebody has to remember.

    THE IDENTITY BASIS IS A REAL BASIS, NOT A NULL. An INR document has
    ``rate == 1``, ``fx_rate_id is None`` and ``rate_date is None``, which is
    precisely the shape `ck_bill_fx_provenance` (023) requires of an INR bill.
    Modelling it as "no basis" would put an ``if basis is None`` in every
    caller, and one of them would eventually get it wrong in the direction that
    writes an untranslated figure into a base column -- which is the defect,
    not a way of avoiding it.
    """

    __slots__ = ("source_currency", "minor_exponent", "rate", "rate_date",
                 "rate_source", "fx_rate_id")

    def __init__(self, *, source_currency: str, minor_exponent: int,
                 rate: Decimal, rate_date: date | None,
                 rate_source: str | None, fx_rate_id: str | None):
        self.source_currency = source_currency
        self.minor_exponent = int(minor_exponent)
        self.rate = rate
        self.rate_date = rate_date
        self.rate_source = rate_source
        self.fx_rate_id = fx_rate_id

    @property
    def is_identity(self) -> bool:
        """True for the base currency: nothing multiplied, nothing rounded."""
        return self.source_currency == BASE_CURRENCY

    def matches(self, *, source_currency: Any, rate: Any, rate_date: Any,
                fx_rate_id: Any) -> bool:
        """Is that stored basis THIS basis? What makes a replay a no-op.

        Compares the four facts `trg_bill_fx_basis_immutable` (023) compares,
        and no others. `rate_source` is carried for evidence but is not
        compared: a feed that renamed itself must not turn an ordinary sweep
        re-walk into a refusal, and the row it names is compared by id anyway.
        """
        if _currency_code(source_currency) != self.source_currency:
            return False
        if rate is None or Decimal(rate) != self.rate:
            return False
        stored = rate_date.date() if isinstance(rate_date, datetime) else rate_date
        if stored != self.rate_date:
            return False
        return (fx_rate_id or None) == (self.fx_rate_id or None)

    def describe(self) -> str:
        if self.is_identity:
            return f"{BASE_CURRENCY} (identity translation, rate 1, no fx_rate row)"
        return (f"{self.source_currency}/{BASE_CURRENCY} at {self.rate} for "
                f"{self.rate_date} from {self.rate_source} ({self.fx_rate_id})")


def identity_basis() -> TranslationBasis:
    """The base currency's own basis. No rate lookup, no rounding, no row."""
    return TranslationBasis(
        source_currency=BASE_CURRENCY, minor_exponent=BASE_MINOR_EXPONENT,
        rate=Decimal(1), rate_date=None, rate_source=None, fx_rate_id=None)


def resolve_basis(session: Session, *, source_currency: Any,
                  document_date: date, actor: str,
                  exchange_rate: Any = None, rate_source: str | None = None,
                  fx_rate_id: str | None = None,
                  source_reference: str | None = None) -> TranslationBasis:
    """The rate this document is translated by, resolved once -- or refused.

    THREE WAYS A RATE MAY ARRIVE, and all three end at an ``fx_rate`` row, so
    every translation cites provenance however it was fed (rule 2):

    * ``fx_rate_id`` -- a row already recorded. Validated for PAIR and DATE.
    * ``exchange_rate`` + ``rate_source`` -- the figure on the vendor's own
      document. :func:`record_rate` stores it against ``document_date``,
      idempotently, so a replay of the same payload finds the row it wrote last
      time instead of minting a second one for the same fact.
    * neither -- the rate already on file for this currency and this date.

    A DOCUMENT WITH NO RATE IS REFUSED, NOT BOOKED AT FACE VALUE. Booking it at
    face value IS AUD-H-007: a EUR 1,00,000 bill standing at Rs 1,00,000
    because nothing multiplied. The refusal names the currency and the date, so
    an operator records the missing rate and the ingestion re-runs.

    TWO SOURCES FOR ONE DATE IS ALSO REFUSED. `ux_fx_rate_natural` admits an
    RBI reference rate and a bank's dealt rate for the same day deliberately
    (023), because both are real -- which means choosing between them here
    would be this module deciding, silently and on a row ordering, which rate
    the estate books at. The caller names the source, or names the row.
    """
    currency = _currency_code(source_currency)
    if currency == BASE_CURRENCY:
        if fx_rate_id or exchange_rate is not None:
            _err("FX_IDENTITY_TRANSLATION",
                 f"a {BASE_CURRENCY} document is the identity translation and "
                 f"carries no rate, but this one was sent with an explicit "
                 f"rate. ck_bill_fx_provenance refuses the same combination at "
                 f"the database.")
        return identity_basis()

    exponent = minor_exponent(session, currency)

    if fx_rate_id:
        row = session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
            "SELECT from_currency, to_currency, rate_date, rate, rate_source "
            "FROM fx_rate WHERE fx_rate_id = %s", (fx_rate_id,))
        if row is None:
            _err("FX_RATE_NOT_FOUND", f"No fx_rate row {fx_rate_id}.",
                 status=404)
        frm, to, found_date, found_rate, found_source = row
        if frm != currency or to != BASE_CURRENCY:
            _err("FX_RATE_WRONG_PAIR",
                 f"fx_rate {fx_rate_id} is {frm}/{to}; this document is "
                 f"{currency}/{BASE_CURRENCY}.")
        if found_date != document_date:
            _err("FX_RATE_WRONG_DATE",
                 f"fx_rate {fx_rate_id} is the {frm} rate for {found_date}; "
                 f"this document is dated {document_date}. D-5 translates at "
                 f"DOCUMENT DATE, and a rate for another day is not that rate "
                 f"however close it is. Record the rate you intend against "
                 f"{document_date}, with a source that says what it is.")
        return TranslationBasis(
            source_currency=currency, minor_exponent=exponent,
            rate=parse_rate(found_rate, field="fx_rate.rate"),
            rate_date=found_date, rate_source=found_source,
            fx_rate_id=fx_rate_id)

    if exchange_rate is not None:
        if not str(rate_source or "").strip():
            _err("FX_RATE_SOURCE_REQUIRED",
                 f"this document supplies its own {currency}/{BASE_CURRENCY} "
                 f"rate but names no source for it. A rate with no provenance "
                 f"is not evidence (rule 2): say where the figure came from -- "
                 f"the vendor's own document, a bank advice, a reference feed "
                 f"-- and the translation becomes reproducible.")
        recorded = record_rate(
            session, from_currency=currency, rate_date=document_date,
            rate=exchange_rate, rate_source=str(rate_source), actor=actor,
            source_reference=source_reference)
        return TranslationBasis(
            source_currency=currency, minor_exponent=exponent,
            rate=parse_rate(recorded["rate"], field="exchange_rate"),
            rate_date=document_date, rate_source=str(rate_source),
            fx_rate_id=str(recorded["fx_rate_id"]))

    candidates = session.fetchall(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        "SELECT fx_rate_id, rate, rate_source FROM fx_rate "
        "WHERE from_currency = %s AND to_currency = %s AND rate_date = %s "
        "ORDER BY fx_rate_id", (currency, BASE_CURRENCY, document_date))
    if not candidates:
        _err("FX_RATE_UNAVAILABLE",
             f"no {currency}/{BASE_CURRENCY} rate is on file for "
             f"{document_date}, and this document is in {currency}. It is "
             f"REFUSED rather than written at face value: writing it would put "
             f"a {currency} amount in a base-currency column as though it were "
             f"rupees, which is AUD-H-007 exactly. Record the rate for "
             f"{document_date} with its source and re-run the ingestion.",
             status=409)
    if len(candidates) > 1:
        sources = ", ".join(sorted(str(c[2]) for c in candidates))
        _err("FX_RATE_AMBIGUOUS",
             f"{len(candidates)} sources quote {currency}/{BASE_CURRENCY} for "
             f"{document_date} ({sources}). ux_fx_rate_natural admits that "
             f"deliberately -- a reference rate and a dealt rate for one day "
             f"are both real -- so choosing between them is a decision about "
             f"which rate the estate books at, and it is not made here on a "
             f"row ordering. Name the rate_source, or pass the fx_rate_id.",
             status=409)
    found_id, found_rate, found_source = candidates[0]
    return TranslationBasis(
        source_currency=currency, minor_exponent=exponent,
        rate=parse_rate(found_rate, field="fx_rate.rate"),
        rate_date=document_date, rate_source=found_source,
        fx_rate_id=found_id)


def translate_lines(basis: TranslationBasis,
                    source_line_minors: Sequence[int]) -> tuple[int, list[int]]:
    """``(header_base_paise, per_line_base_paise)`` for one document's lines.

    THE IDENTITY SHORT-CIRCUIT IS NOT AN OPTIMISATION. For an INR document the
    source amount ALREADY IS the base amount, so multiplying by 1 and
    quantising would be a no-op that nonetheless routed every rupee figure in
    the product through a rounding step -- and a rounding step that is a no-op
    today is one policy edit away from not being one. The integers are returned
    unchanged and `split_pro_rata` never sees them.

    ORDER IS THE CALLER'S RESPONSIBILITY, AND IT MATTERS. `split_pro_rata`
    hands the leftover paise to the largest fractional remainders, so the same
    document with its lines in a different order can allocate the odd paisa to
    a different line. The header total is identical either way; the per-line
    figures are not. :func:`app.backend.pg.procurement.mirror_bill` sorts by
    the DERIVED `bill_line_id` before calling this, which is stable across a
    reordered replay -- a caller that passes payload order gets payload-order
    answers, and a reordered replay would then rewrite `amount_paise` by a
    paisa with `source_amount_minor` unchanged, which is precisely the silent
    drift this module exists to prevent.
    """
    minors = [int(m) for m in source_line_minors]
    if basis.is_identity:
        return sum(minors), list(minors)
    return translate_document(minors, basis.rate,
                              source_minor_exponent=basis.minor_exponent)


# ---------------------------------------------------------------------------
# Once, exactly once: the translation event ledger
# ---------------------------------------------------------------------------
def register_translation(session: Session, *, document_type: str,
                         document_id: str, entity_id: str,
                         basis: TranslationBasis, source_total_minor: int,
                         base_total_paise: int, line_count: int, actor: str,
                         correlation_id: str | None = None) -> dict[str, Any]:
    """Record that this document was translated -- or prove that it already was.

    ONE ROW PER DOCUMENT, ENFORCED BY THE PRIMARY KEY, which is what makes
    requirement 4 structural rather than a property of whichever caller
    happened to run. `pk_fx_translation_event` is
    ``(document_type, document_id)``: a second translation of one document
    cannot be inserted, so a code path that tried would fail loudly instead of
    quietly applying a rate twice.

    FOUR OUTCOMES, and the three that are not "first time" are the whole reason
    this is a ledger rather than a boolean:

    ``created``
        No row existed. Written, and audited.
    ``replayed``
        A row existed and every figure in it agrees with what this pass
        recomputed -- the ordinary sweep re-walk on its 300-second overlap.
        NOTHING is written and nothing is raised: an idempotent replay must be
        a no-op, not a conflict.
    ``FX_SOURCE_DOCUMENT_CHANGED``
        The basis agrees, the SOURCE TOTAL does not: the vendor revised the
        document. REFUSED. This is requirement 8's exception, and refusing is
        not timidity -- the booked base figure was derived at a rate fixed on
        the document's own date, so re-deriving it from a new source amount
        without a record is exactly the value drift the requirement forbids.
    ``FX_TRANSLATION_BASIS_CONFLICT``
        The document is being presented in a different currency, or at a
        different rate, from the one it posted under. REFUSED for the reason
        `trg_bill_fx_basis_immutable` refuses it at the database: re-basing in
        place moves a posted CWIP figure with nothing recording that it moved.
        A period-end movement goes through :func:`assess_revaluation`, which
        records it.

    A FIFTH OUTCOME WOULD BE A BUG IN THIS MODULE, NOT IN THE DATA.
    ``FX_TRANSLATION_NOT_REPRODUCIBLE`` fires when the basis and the source
    total both agree and the BASE figure does not -- which cannot happen if the
    arithmetic is deterministic, and is therefore reported as the determinism
    failure it is rather than absorbed as a difference.
    """
    if document_type not in TRANSLATABLE_DOCUMENT_TYPES:
        raise KeyError(
            f"{document_type!r} is not a translatable document type; known "
            f"types are {list(TRANSLATABLE_DOCUMENT_TYPES)}. Adding one is a "
            f"migration (ck_fx_translation_event_document_type), not a string.")

    existing = session.fetchone(  # scope-exempt: this is the read-back of the row this call is about to write or replay, and the entity predicate is applied by fx_translation_event's own RLS policy (migration 025). A scoped re-read here would answer "no translation" for a row that DOES exist whenever the caller's scope is narrower than the writer's -- the one wrong answer that would let a rate be applied twice.
        "SELECT source_currency, fx_rate, fx_rate_date, fx_rate_id, "
        "       source_total_minor, base_total_paise "
        "FROM fx_translation_event "
        "WHERE document_type = %s AND document_id = %s",
        (document_type, document_id))

    if existing is not None:
        (had_currency, had_rate, had_rate_date, had_rate_id,
         had_source_total, had_base_total) = existing
        if not basis.matches(source_currency=had_currency, rate=had_rate,
                             rate_date=had_rate_date, fx_rate_id=had_rate_id):
            _err("FX_TRANSLATION_BASIS_CONFLICT",
                 f"{document_type} {document_id} was translated as "
                 f"{had_currency} at {had_rate} for {had_rate_date} (fx_rate "
                 f"{had_rate_id}); this pass presents it as "
                 f"{basis.describe()}. A rate is applied EXACTLY ONCE: "
                 f"re-basing a document that has already posted would move its "
                 f"booked figure with nothing recording the movement, which is "
                 f"what plan decision D-5 and trg_bill_fx_basis_immutable both "
                 f"refuse. A period-end movement goes through "
                 f"assess_revaluation, which records it.",
                 status=409)
        if int(had_source_total) != int(source_total_minor):
            _err("FX_SOURCE_DOCUMENT_CHANGED",
                 f"{document_type} {document_id} posted a source total of "
                 f"{had_source_total} {had_currency} minor units; this pass "
                 f"carries {source_total_minor}. The source document was "
                 f"REVISED. Its base figure was derived at the "
                 f"{had_rate_date} rate and is not silently re-derived from a "
                 f"new source amount -- that is value drift with nothing "
                 f"recording it. Nothing was written. The revision needs a "
                 f"recorded correction against the document, not a quieter "
                 f"re-mirror.",
                 status=409)
        if int(had_base_total) != int(base_total_paise):
            _err("FX_TRANSLATION_NOT_REPRODUCIBLE",
                 f"{document_type} {document_id}: the same source total "
                 f"({source_total_minor}) at the same rate ({had_rate}) "
                 f"produced {base_total_paise} paise now and {had_base_total} "
                 f"paise when it posted. This translation is deterministic by "
                 f"construction, so the difference is a defect in the "
                 f"arithmetic, not a difference in the data.",
                 status=500)
        return {"document_type": document_type, "document_id": document_id,
                "created": False, "replayed": True,
                "base_total_paise": int(had_base_total),
                "source_total_minor": int(had_source_total)}

    session.execute(  # scope-exempt: an INSERT names its own entity_id in the VALUES list and fx_translation_event's RLS policy (migration 025) checks it with WITH CHECK, so an out-of-scope row is refused by the database rather than admitted by a predicate this statement has no FROM clause to carry
        """
        INSERT INTO fx_translation_event (
            document_type, document_id, entity_id, source_currency,
            source_minor_exponent, source_total_minor, fx_rate, fx_rate_date,
            fx_rate_source, fx_rate_id, base_total_paise, line_count,
            rounding, correlation_id, translated_by)
        VALUES (%(document_type)s, %(document_id)s, %(entity_id)s,
                %(source_currency)s, %(exponent)s, %(source_total)s,
                %(rate)s, %(rate_date)s, %(rate_source)s, %(rate_id)s,
                %(base_total)s, %(line_count)s, %(rounding)s,
                %(correlation_id)s, %(actor)s)
        """,
        {"document_type": document_type, "document_id": document_id,
         "entity_id": entity_id, "source_currency": basis.source_currency,
         "exponent": basis.minor_exponent,
         "source_total": int(source_total_minor), "rate": basis.rate,
         "rate_date": basis.rate_date, "rate_source": basis.rate_source,
         "rate_id": basis.fx_rate_id, "base_total": int(base_total_paise),
         "line_count": int(line_count), "rounding": ROUNDING_RULE,
         "correlation_id": correlation_id, "actor": actor},
    )
    # AUDITED ONLY WHEN SOMETHING HAPPENED. A replay returns above without
    # reaching this, so `audit_log` records translations rather than sweep
    # passes. The chain is evidence; one entry every fifteen minutes on a
    # document nothing touched is noise that makes the entries that matter
    # harder to find.
    audit_mod.append(
        session, actor, "FX_DOCUMENT_TRANSLATED", document_type, document_id,
        f"{basis.describe()}: {source_total_minor} minor units across "
        f"{line_count} line(s) = {base_total_paise} paise "
        f"({ROUNDING_RULE}, applied once to the product)")
    return {"document_type": document_type, "document_id": document_id,
            "created": True, "replayed": False,
            "base_total_paise": int(base_total_paise),
            "source_total_minor": int(source_total_minor)}


def translation_event(session: Session, *, document_type: str,
                      document_id: str) -> dict[str, Any] | None:
    """The recorded translation of one document, SCOPED, or None.

    Read-only, and everything an auditor needs to recompute the figure:
    the source total in its own minor units, the exponent that scaled it, the
    rate with its date and source, the base total it produced, and the rounding
    rule under which it was produced.
    """
    row = repo.query_one(
        session,
        """
        SELECT e.document_type, e.document_id, e.entity_id, e.source_currency,
               e.source_minor_exponent, e.source_total_minor, e.fx_rate,
               e.fx_rate_date, e.fx_rate_source, e.fx_rate_id,
               e.base_total_paise, e.line_count, e.rounding, e.translated_at,
               e.translated_by
        FROM fx_translation_event e
        WHERE e.document_type = %(document_type)s
          AND e.document_id = %(document_id)s
          AND {scope}
        """,
        {"document_type": document_type, "document_id": document_id},
        columns={"entity": "e.entity_id", "plant": None,
                 "location": None, "project": None},
    )
    if row is None:
        return None
    keys = ("document_type", "document_id", "entity_id", "source_currency",
            "source_minor_exponent", "source_total_minor", "fx_rate",
            "fx_rate_date", "fx_rate_source", "fx_rate_id", "base_total_paise",
            "line_count", "rounding", "translated_at", "translated_by")
    out: dict[str, Any] = dict(zip(keys, row, strict=True))
    out["fx_rate"] = str(out["fx_rate"]) if out["fx_rate"] is not None else None
    for key in ("fx_rate_date", "translated_at"):
        value = out[key]
        out[key] = value.isoformat() if value is not None else None
    return out
