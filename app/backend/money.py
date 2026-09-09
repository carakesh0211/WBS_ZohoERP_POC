"""Exact money handling.

AUD-H-007. The previous conversion was ``int(round(float(rupees) * 100))``, which
lost money in two distinct ways the audit reproduced:

    L(0.005)            -> 0                      (banker's rounding through binary float)
    L(0.015)            -> 2                      (inconsistent with the line above)
    L(90071992547409.93)-> 9007199254740994       (float64 mantissa exhausted)

Money is therefore parsed with ``decimal.Decimal`` from a *string* and rounded
once, explicitly, half-up - the convention Indian accounting expects. Amounts are
stored and computed as integer paise; no float ever touches a monetary value.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

# Guard rails. A single CAPEX line beyond this is a data error, not a business case.
MAX_PAISE = 10 ** 18          # ~1e16 rupees
CURRENCY_EXPONENT = 2         # INR: 2 decimal places

#: ISO 4217 minor-unit exponents, mirroring migration 023's
#: ``currency_denomination`` seed.
#:
#: THIS LIVES HERE, in the module that owns what a monetary value is, because
#: BOTH the ingestion boundary (`integration/dto.py`) and the translation
#: engine (`pg/fx.py`) need it, and `integration/` importing from `pg/` would
#: invert the layering. `fx.SEEDED_MINOR_EXPONENTS` is an alias of this dict,
#: not a copy, so the two cannot drift; the database remains the source of
#: truth and `fx.minor_exponent()` still reads the table.
#:
#: Not every currency is two decimals, and assuming so is not a rounding error:
#: a yen amount parsed as if it had two decimals is booked a HUNDRED TIMES too
#: high, and a dinar ten times too low.
MINOR_EXPONENTS: dict[str, int] = {
    "INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2, "SGD": 2,
    "CHF": 2, "AUD": 2, "CNY": 2, "JPY": 0, "KWD": 3,
}


def minor_exponent_of(currency_code: str | None) -> int:
    """How many decimal places `currency_code` has, defaulting to INR's two.

    An unknown code returns the base exponent rather than raising: this is the
    PARSING boundary, and a document in an unrecognised currency should reach
    the FX engine -- which refuses it with a coded error naming the missing
    `currency_denomination` row -- rather than dying here with a KeyError.
    """
    if not currency_code:
        return CURRENCY_EXPONENT
    return MINOR_EXPONENTS.get(str(currency_code).strip().upper(),
                               CURRENCY_EXPONENT)


class MoneyError(ValueError):
    """Raised when a monetary input cannot be represented exactly."""


def to_paise(value, *, field: str = "amount", allow_negative: bool = False,
             minor_exponent: int = CURRENCY_EXPONENT) -> int:
    """Convert a user/API supplied amount in rupees to integer paise.

    Accepts str, int, Decimal. A float is accepted only when it is exactly
    representable at 2dp, because silently rounding a float is how the original
    defect happened; anything else is rejected so the caller sends a string.

    `minor_exponent` is the number of decimal places the SOURCE currency has,
    defaulting to INR's two so every existing caller is unchanged. It is not
    cosmetic: this function was hardcoded to multiply by 100, so a JPY amount
    (exponent 0) was booked a hundred times too high and a KWD amount
    (exponent 3) ten times too low with a decimal silently dropped -- and both
    currencies are seeded as supported.
    """
    if value is None:
        raise MoneyError(f"{field} is required.")
    if isinstance(value, bool):
        raise MoneyError(f"{field} must be a number, not a boolean.")

    if isinstance(value, float):
        # inf/nan must fail as a controlled MoneyError, not as decimal.InvalidOperation
        # escaping from quantize() below.
        if value != value or value in (float("inf"), float("-inf")):
            raise MoneyError(f"{field} must be a finite amount.")
        # A float is only accepted when it already denotes an exact 2dp amount.
        # 0.1 + 0.2 becomes 0.30000000000000004, which is NOT an exact 2dp value:
        # accepting it would silently round away a defect the caller should fix by
        # sending a decimal string.
        exact = Decimal(repr(value))
        if exact != exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise MoneyError(
                f"{field} was sent as a float ({value!r}) that is not an exact "
                f"2-decimal amount. Send monetary values as decimal strings.")
        dec = exact
    elif isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value).strip().replace(",", ""))
        except (InvalidOperation, AttributeError):
            raise MoneyError(f"{field} is not a valid amount: {value!r}")

    if not dec.is_finite():
        raise MoneyError(f"{field} must be a finite amount.")

    # Reject values outside the supported ledger range before multiplying or
    # quantizing them.  Extremely large exponents (for example ``1e400``)
    # otherwise exceed the decimal128 working context and leak an
    # ``InvalidOperation`` as an HTTP 500 instead of a controlled MoneyError.
    if dec < 0 and not allow_negative:
        raise MoneyError(f"{field} must not be negative.")
    if abs(dec) > Decimal(MAX_PAISE) / Decimal(100):
        raise MoneyError(f"{field} exceeds the maximum supported amount.")

    try:
        with localcontext() as ctx:
            ctx.prec = 34                   # IEEE decimal128: ample for CAPEX values
            # SCALED BY THE CURRENCY'S OWN EXPONENT, not always by 100.
            # `minor_exponent` defaults to INR's 2, so every existing caller
            # is unchanged; a JPY amount passes 0 and a KWD amount 3.
            paise = (dec * (10 ** minor_exponent)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise MoneyError(f"{field} is not a valid supported amount.") from exc

    ipaise = int(paise)
    if abs(ipaise) > MAX_PAISE:
        raise MoneyError(f"{field} exceeds the maximum supported amount.")
    return ipaise


def to_rupees(paise: int) -> Decimal:
    """Integer paise -> exact Decimal rupees. Never returns a float."""
    if paise is None:
        return Decimal("0")
    return (Decimal(int(paise)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_inr(paise: int, *, symbol: bool = True) -> str:
    """Indian digit grouping, e.g. 15000000 paise -> Rs 1,50,000.00"""
    neg = paise is not None and paise < 0
    amt = to_rupees(abs(int(paise or 0)))
    whole, _, frac = str(amt).partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    out = f"{whole}.{frac or '00'}"
    if symbol:
        out = "₹" + out
    return f"({out})" if neg else out


def split_pro_rata(total_paise: int, weights: list[int]) -> list[int]:
    """Allocate an integer amount across weights with no paise lost.

    Largest-remainder method: the sum of the result always equals total_paise,
    which matters for capitalisation allocation and common-cost distribution.
    """
    total_w = sum(weights)
    if total_w <= 0:
        raise MoneyError("Cannot allocate across zero total weight.")
    raw = [(total_paise * w) // total_w for w in weights]
    remainder = total_paise - sum(raw)
    # hand the leftover paise to the largest fractional parts, deterministically
    order = sorted(range(len(weights)),
                   key=lambda i: ((total_paise * weights[i]) % total_w, weights[i], -i),
                   reverse=True)
    for k in range(remainder):
        raw[order[k % len(order)]] += 1
    return raw
