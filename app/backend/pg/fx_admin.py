"""Exchange-rate administration over the ``fx_rate`` table (Fable 5.1,
migration 028).

Migration 023 shipped the translation engine (``pg/fx.py``) and exactly one
writer of rates: ``record_rate``, called when a vendor's own document carried a
rate. A month of reference rates loaded AHEAD of the bills that need them, a
rate keyed wrongly and retired, a correction that keeps the wrong row citable
by the bills already translated at it -- none of that had a door. This module
is the door, and it is deliberately narrow.

THE RULES, EACH HELD BY A TEST IN ``tests/test_pg_fx_admin.py``
==============================================================

* RATES ARE APPEND-ONLY HISTORY. A correction is a NEW row. Activating it
  marks the row it replaces inactive and points ``superseded_by`` at the
  correction. The numeric value on a row is never UPDATEd -- not by this
  module, and not by anything else: ``trg_fx_rate_history_immutable`` refuses
  it at the database, and refuses DELETE outright.

* A RATE CREATED HERE STARTS INACTIVE. It is put into force by
  :func:`activate_rate`, and under ``fx_policy.FX_ACTIVATION_SEPARATION``
  (seeded ``REQUIRED``) the activating user must differ from the creating
  user -- refusal code ``FX_SELF_ACTIVATION``. The same shape as maker-checker
  on a budget: one pair of hands types the figure, another puts it into force.

* THE LOOKUP READS ACTIVE ROWS ONLY AND REFUSES WHEN NONE COVERS (pair, date).
  ``FX_RATE_UNAVAILABLE`` is a 404 here, so the "Test lookup" control on the
  screen shows the operator exactly what the bills ingestion will hit for that
  date. There is no fallback to the previous day, to a stale row, or to 1.
  ``pg/fx.py::resolve_basis`` applies the same ``active`` filter at the write
  path.

* EXACT DECIMAL STRINGS IN AND OUT. ``fx.parse_rate`` refuses a float, refuses
  more than eight decimals, and quantises to ``numeric(18,8)``; every rate this
  module returns is ``str(Decimal)`` at that scale. Nothing here touches a
  float.

* MINOR-UNIT EXPONENTS COME FROM ``money.py``. The translation preview in
  :func:`lookup_rate` uses ``money.minor_exponent_of`` for both currencies and
  ``fx.translate_to_base_paise`` for the arithmetic, so a JPY amount (exponent
  0) and a KWD amount (exponent 3) preview exactly as the ingestion would book
  them.

``fx_rate`` carries no scope dimension (organisation-wide reference data,
``capex_principal_present()`` in 023), so every read here is a direct
``session.fetch*`` with the ``# scope-exempt:`` marker the scope gate requires,
exactly as ``pg/fx.py`` reads the same table.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .. import money
from . import audit as audit_mod
from . import fx
from .engine import Session

OBJECT_TYPE = "FX_RATE"

#: How many rows one synchronous import may carry. The screen pastes a month
#: of quotes for a handful of currencies; a feed of thousands is a job.
IMPORT_ROW_LIMIT = 500

STATUS_PENDING = "AWAITING_ACTIVATION"  # not "PENDING": that word is an integration/approval status and the business-screen gate refuses it
STATUS_ACTIVE = "ACTIVE"
STATUS_RETIRED = "RETIRED"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_RETIRED, STATUS_SUPERSEDED)

_COLUMNS = (
    "fx_rate_id", "from_currency", "to_currency", "rate_date", "rate", "rate_source",
    "source_reference", "captured_at", "created_by", "active", "activated_at",
    "activated_by", "deactivated_at", "deactivated_by", "deactivation_reason",
    "superseded_by", "note", "updated_at", "updated_by",
)
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM fx_rate"


class FxAdminError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, detail: Any = None):
        self.code, self.message, self.status, self.detail = code, message, status, detail
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400, detail: Any = None) -> None:
    raise FxAdminError(code, message, status=status, detail=detail)


def _new_id() -> str:
    return f"FXR-{uuid.uuid4().hex[:12].upper()}"


def _iso(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value.isoformat() if hasattr(value, "isoformat") else value


def _status_of(row: Mapping[str, Any]) -> str:
    if row["superseded_by"]:
        return STATUS_SUPERSEDED
    if row["active"]:
        return STATUS_ACTIVE
    if row["deactivated_at"]:
        return STATUS_RETIRED
    return STATUS_PENDING


def _to_dict(record: Sequence[Any]) -> dict[str, Any]:
    row = {k: _iso(v) for k, v in zip(_COLUMNS, record)}
    # The exact scale the column holds, however the driver rendered it.
    row["rate"] = str(fx.parse_rate(row["rate"], field="fx_rate.rate"))
    row["status"] = _status_of(row)
    row["source_minor_exponent"] = money.minor_exponent_of(row["from_currency"])
    row["target_minor_exponent"] = money.minor_exponent_of(row["to_currency"])
    return row


# ============================================================ validation
def _code(value: Any, *, field: str) -> str:
    """Three-letter shape only (no table read) -- for list filters."""
    try:
        return fx._currency_code(value)
    except fx.FxError as exc:
        _err(exc.code, f"{field}: {exc.message}", status=422)
    raise AssertionError("unreachable")  # pragma: no cover


def _currency(session: Session, value: Any, *, field: str) -> str:
    """A three-letter code that ``currency_denomination`` knows and still
    supports. The EXPONENT is not read here -- ``money.minor_exponent_of`` is
    the one source the API answers with -- only existence and support."""
    try:
        code = fx._currency_code(value)
    except fx.FxError as exc:
        _err(exc.code, f"{field}: {exc.message}", status=422)
    row = session.fetchone(  # scope-exempt: currency_denomination is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        "SELECT is_supported FROM currency_denomination WHERE currency_code = %s", (code,))
    if row is None:
        _err("FX_CURRENCY_UNKNOWN",
             f"{field}: {code} has no currency_denomination row. Add the currency "
             f"before quoting it; its minor-unit exponent is not guessed.", status=422)
    if not row[0]:
        _err("FX_CURRENCY_WITHDRAWN",
             f"{field}: {code} is no longer supported for new quotes.", status=409)
    return code


def _rate_date(value: Any, *, field: str = "rate_date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        _err("FX_DATE_INVALID", f"{field} must be an ISO date (YYYY-MM-DD); got {value!r}.",
             status=422)
    raise AssertionError("unreachable")  # pragma: no cover


def _parse_rate(value: Any, *, field: str = "rate") -> Decimal:
    try:
        return fx.parse_rate(value, field=field)
    except fx.FxError as exc:
        _err(exc.code, exc.message, status=422)
    raise AssertionError("unreachable")  # pragma: no cover


def _source(value: Any, *, field: str = "rate_source") -> str:
    text = str(value or "").strip()
    if not text:
        _err("FX_RATE_SOURCE_REQUIRED",
             f"{field} is required: a rate with no provenance is not evidence.", status=422)
    if len(text) > 80:
        _err("FX_RATE_SOURCE_TOO_LONG", f"{field} must be at most 80 characters.", status=422)
    return text


def activation_separation_required(session: Session) -> bool:
    """``fx_policy.FX_ACTIVATION_SEPARATION``; an unreadable or absent row is
    the conservative branch, REQUIRED. Read here rather than through
    ``fx.policy`` so ``fx.POLICY_DEFAULTS`` -- which a test pins to migration
    023's own text -- is not widened by a 028 key."""
    try:
        row = session.fetchone(  # scope-exempt: fx_policy is organisation-wide configuration with no dimension column, scoped by capex_principal_present() in migration 023
            "SELECT policy_value FROM fx_policy WHERE policy_key = 'FX_ACTIVATION_SEPARATION'")
    except Exception:                       # noqa: BLE001 -- fail closed, as fx.policy does
        return True
    return not (row is not None and str(row[0]) == "WAIVED")


# ============================================================ reads
def get_rate(session: Session, fx_rate_id: str) -> dict[str, Any]:
    record = session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + " WHERE fx_rate_id = %s", (str(fx_rate_id or ""),))
    if record is None:
        _err("FX_RATE_NOT_FOUND", f"No exchange rate {fx_rate_id}.", status=404)
    return _to_dict(record)


def list_rates(session: Session, *, from_currency: str | None = None,
               to_currency: str | None = None, active: bool | None = None,
               status: str | None = None, rate_source: str | None = None,
               effective_from: date | None = None, effective_to: date | None = None,
               limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    """Newest rate date first, keyset-paginated on (rate_date, fx_rate_id)."""
    limit = max(1, min(int(limit), 200))
    if status is not None and status not in STATUSES:
        _err("FX_STATUS_UNKNOWN", f"status must be one of {', '.join(STATUSES)}.", status=422)
    after_date = after_id = None
    if cursor:
        try:
            after = json.loads(cursor)
            after_date = date.fromisoformat(after["rate_date"])
            after_id = str(after["fx_rate_id"])
        except (ValueError, KeyError, TypeError):
            _err("INVALID_CURSOR", "cursor is not one this endpoint issued.", status=400)
    frm = _code(from_currency, field="source") if from_currency else None
    to = _code(to_currency, field="target") if to_currency else None
    records = session.fetchall(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + """
        WHERE (%(frm)s::text IS NULL OR from_currency = %(frm)s)
          AND (%(to)s::text IS NULL OR to_currency = %(to)s)
          AND (%(active)s::boolean IS NULL OR active = %(active)s)
          AND (%(source)s::text IS NULL OR rate_source = %(source)s)
          AND (%(eff_from)s::date IS NULL OR rate_date >= %(eff_from)s)
          AND (%(eff_to)s::date IS NULL OR rate_date <= %(eff_to)s)
          AND (%(after_date)s::date IS NULL
               OR (rate_date, fx_rate_id) < (%(after_date)s::date, %(after_id)s))
        ORDER BY rate_date DESC, fx_rate_id DESC
        LIMIT %(limit)s
        """,
        {"frm": frm, "to": to, "active": active, "source": rate_source,
         "eff_from": effective_from, "eff_to": effective_to,
         "after_date": after_date, "after_id": after_id,
         # A status filter is applied after the fetch (it is derived), so
         # over-fetch generously and page on the filtered set.
         "limit": (limit + 1) if status is None else (limit + 1) * 4})
    items = [_to_dict(r) for r in records]
    if status is not None:
        items = [it for it in items if it["status"] == status]
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        last = page[-1]
        next_cursor = json.dumps({"rate_date": last["rate_date"], "fx_rate_id": last["fx_rate_id"]})
    return {"items": page, "next_cursor": next_cursor}


def history(session: Session, *, from_currency: str, to_currency: str = fx.BASE_CURRENCY,
            limit: int = 200) -> dict[str, Any]:
    """Every row ever recorded for a pair, newest date first, in every state.
    This is the append-only record: a superseded row sits beside the row that
    replaced it and both keep their values."""
    frm = _currency(session, from_currency, field="source")
    to = _currency(session, to_currency, field="target")
    limit = max(1, min(int(limit), 1000))
    records = session.fetchall(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + " WHERE from_currency = %s AND to_currency = %s "
        "ORDER BY rate_date DESC, captured_at DESC, fx_rate_id DESC LIMIT %s",
        (frm, to, limit))
    return {"source": frm, "target": to, "items": [_to_dict(r) for r in records]}


def lookup_rate(session: Session, *, from_currency: str, to_currency: str = fx.BASE_CURRENCY,
                on_date: Any, rate_source: str | None = None,
                amount_minor: int | None = None) -> dict[str, Any]:
    """The ACTIVE quote for (pair, date), or the coded refusal.

    ``FX_RATE_UNAVAILABLE`` (404) when no active row covers the date -- the
    same code ``fx.resolve_basis`` raises at the bill write, so what this
    answers is what the ingestion will do. ``FX_RATE_AMBIGUOUS`` (409) when
    two sources quote the day and the caller named neither. ``amount_minor``,
    an integer in the SOURCE currency's own minor units, adds a preview of the
    INR paise the engine would book.
    """
    frm = _currency(session, from_currency, field="source")
    to = _currency(session, to_currency, field="target")
    on = _rate_date(on_date, field="date")
    if amount_minor is not None and (isinstance(amount_minor, bool) or not isinstance(amount_minor, int)):
        _err("MONEY_NOT_INTEGER", "amount_minor must be an integer in the source currency's minor units.",
             status=422)
    src_exp = money.minor_exponent_of(frm)
    tgt_exp = money.minor_exponent_of(to)
    if frm == to:
        result = {"identity": True, "source": frm, "target": to, "date": on.isoformat(),
                  "rate": str(fx.parse_rate(1, field="rate")), "fx_rate_id": None, "rate_source": None,
                  "source_minor_exponent": src_exp, "target_minor_exponent": tgt_exp}
        if amount_minor is not None:
            result["amount_minor"] = int(amount_minor)
            result["translated_paise"] = int(amount_minor)
        return result
    records = session.fetchall(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + " WHERE from_currency = %s AND to_currency = %s AND rate_date = %s AND active "
        "AND (%s::text IS NULL OR rate_source = %s) ORDER BY fx_rate_id",
        (frm, to, on, rate_source, rate_source))
    if not records:
        _err("FX_RATE_UNAVAILABLE",
             f"No ACTIVE {frm}/{to} rate is on file for {on.isoformat()}"
             + (f" from {rate_source}" if rate_source else "")
             + ". A document in this currency dated that day is REFUSED, not translated at "
             "another day's rate or at face value. Record the rate for that date and activate it.",
             status=404)
    if len(records) > 1:
        sources = sorted(str(r[_COLUMNS.index("rate_source")]) for r in records)
        _err("FX_RATE_AMBIGUOUS",
             f"{len(records)} sources quote {frm}/{to} for {on.isoformat()} ({', '.join(sources)}). "
             f"Name the rate_source; the choice is not made on a row ordering.",
             status=409, detail={"sources": sources})
    row = _to_dict(records[0])
    result = {"identity": False, "source": frm, "target": to, "date": on.isoformat(),
              "rate": row["rate"], "fx_rate_id": row["fx_rate_id"], "rate_source": row["rate_source"],
              "source_minor_exponent": src_exp, "target_minor_exponent": tgt_exp, "row": row}
    if amount_minor is not None:
        result["amount_minor"] = int(amount_minor)
        result["translated_paise"] = fx.translate_to_base_paise(
            int(amount_minor), Decimal(row["rate"]), source_minor_exponent=src_exp)
    return result


# ============================================================ writes
def _pending_for_key(session: Session, frm: str, to: str, on: date, source: str) -> tuple | None:
    return session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + " WHERE from_currency = %s AND to_currency = %s AND rate_date = %s "
        "AND rate_source = %s AND NOT active AND deactivated_at IS NULL AND superseded_by IS NULL "
        "ORDER BY captured_at DESC LIMIT 1", (frm, to, on, source))


def _active_for_key(session: Session, frm: str, to: str, on: date, source: str) -> tuple | None:
    return session.fetchone(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
        _SELECT + " WHERE from_currency = %s AND to_currency = %s AND rate_date = %s "
        "AND rate_source = %s AND active", (frm, to, on, source))


def _normalise(session: Session, values: Mapping[str, Any]) -> dict[str, Any]:
    frm = _currency(session, values.get("from_currency"), field="from_currency")
    to = _currency(session, values.get("to_currency") or fx.BASE_CURRENCY, field="to_currency")
    if frm == to:
        _err("FX_IDENTITY_RATE",
             f"{frm} to {to} is the identity translation and is never stored.", status=422)
    on = _rate_date(values.get("rate_date"))
    rate = _parse_rate(values.get("rate"))
    source = _source(values.get("rate_source"))
    reference = str(values.get("source_reference") or "").strip() or None
    note = str(values.get("note") or "").strip() or None
    return {"from_currency": frm, "to_currency": to, "rate_date": on, "rate": rate,
            "rate_source": source, "source_reference": reference, "note": note}


def create_rate(session: Session, *, actor: str, from_currency: str, rate_date: Any, rate: Any,
                rate_source: str, to_currency: str = fx.BASE_CURRENCY,
                source_reference: str | None = None, note: str | None = None,
                correlation_id: str | None = None) -> dict[str, Any]:
    """Record a quote, INACTIVE, awaiting activation by another user.

    Idempotent on the natural key and the value: the same quote sent twice
    returns the row it made the first time (``created: false``), whether that
    row is still pending or already active. A DIFFERENT value for a key that
    already has an active quote is a correction: it is recorded as a new
    pending row that, on activation, supersedes the active one. Two different
    pending corrections for one key are refused -- resolve the first.
    """
    v = _normalise(session, {"from_currency": from_currency, "to_currency": to_currency,
                             "rate_date": rate_date, "rate": rate, "rate_source": rate_source,
                             "source_reference": source_reference, "note": note})
    frm, to, on, parsed, source = (v["from_currency"], v["to_currency"], v["rate_date"],
                                   v["rate"], v["rate_source"])
    active = _active_for_key(session, frm, to, on, source)
    if active is not None:
        active_row = _to_dict(active)
        if Decimal(active_row["rate"]) == parsed:
            return {**active_row, "created": False}
    pending = _pending_for_key(session, frm, to, on, source)
    if pending is not None:
        pending_row = _to_dict(pending)
        if Decimal(pending_row["rate"]) == parsed:
            return {**pending_row, "created": False}
        _err("FX_RATE_PENDING_CONFLICT",
             f"{pending_row['fx_rate_id']} already awaits activation for {frm}/{to} on "
             f"{on.isoformat()} from {source} at {pending_row['rate']}; this call says {parsed}. "
             f"Activate or retire the pending quote before recording another.",
             status=409)
    fx_rate_id = _new_id()
    corrects = _to_dict(active)["fx_rate_id"] if active is not None else None
    if corrects and not v["note"]:
        v["note"] = f"Correction of {corrects}."
    session.execute(
        """
        INSERT INTO fx_rate (fx_rate_id, from_currency, to_currency, rate_date, rate, rate_source,
                             source_reference, created_by, active, note, updated_by)
        VALUES (%(id)s, %(frm)s, %(to)s, %(on)s, %(rate)s, %(source)s, %(ref)s, %(actor)s,
                false, %(note)s, %(actor)s)
        """,
        {"id": fx_rate_id, "frm": frm, "to": to, "on": on, "rate": parsed, "source": source,
         "ref": v["source_reference"], "note": v["note"], "actor": actor})
    audit_mod.append(session, actor, "FX_RATE_CREATED", OBJECT_TYPE, fx_rate_id,
                     f"{frm}/{to} {parsed} for {on.isoformat()} from {source} (pending activation)"
                     + (f"; corrects {corrects}" if corrects else ""),
                     correlation_id=correlation_id)
    return {**get_rate(session, fx_rate_id), "created": True}


def activate_rate(session: Session, *, actor: str, fx_rate_id: str,
                  correlation_id: str | None = None) -> dict[str, Any]:
    row = get_rate(session, fx_rate_id)
    if row["status"] == STATUS_ACTIVE:
        _err("FX_RATE_ALREADY_ACTIVE", f"{fx_rate_id} is already in force.", status=409)
    if row["status"] in (STATUS_RETIRED, STATUS_SUPERSEDED):
        _err("FX_RATE_RETIRED",
             f"{fx_rate_id} was {row['status'].lower()} and cannot be put back into force. "
             f"Record the quote again as a new row.", status=409)
    if activation_separation_required(session) and str(row["created_by"]) == str(actor):
        _err("FX_SELF_ACTIVATION",
             f"{actor} recorded {fx_rate_id} and may not also put it into force "
             f"(fx_policy.FX_ACTIVATION_SEPARATION = REQUIRED). A different user activates it.",
             status=409)
    superseded: list[str] = []
    for old in session.fetchall(  # scope-exempt: fx_rate is organisation-wide reference data with no dimension column, scoped by capex_principal_present() in migration 023
            "SELECT fx_rate_id FROM fx_rate WHERE from_currency = %s AND to_currency = %s "
            "AND rate_date = %s AND rate_source = %s AND active AND fx_rate_id <> %s",
            (row["from_currency"], row["to_currency"], date.fromisoformat(row["rate_date"]),
             row["rate_source"], fx_rate_id)):
        old_id = old[0]
        session.execute(
            "UPDATE fx_rate SET active = false, deactivated_at = now(), deactivated_by = %s, "
            "deactivation_reason = %s, superseded_by = %s, updated_by = %s WHERE fx_rate_id = %s",
            (actor, f"Superseded by {fx_rate_id}.", fx_rate_id, actor, old_id))
        audit_mod.append(session, actor, "FX_RATE_SUPERSEDED", OBJECT_TYPE, old_id,
                         f"superseded by {fx_rate_id}", correlation_id=correlation_id)
        superseded.append(old_id)
    session.execute(
        "UPDATE fx_rate SET active = true, activated_at = now(), activated_by = %s, updated_by = %s "
        "WHERE fx_rate_id = %s", (actor, actor, fx_rate_id))
    audit_mod.append(session, actor, "FX_RATE_ACTIVATED", OBJECT_TYPE, fx_rate_id,
                     f"{row['from_currency']}/{row['to_currency']} {row['rate']} for {row['rate_date']} "
                     f"from {row['rate_source']} in force"
                     + (f"; supersedes {', '.join(superseded)}" if superseded else ""),
                     correlation_id=correlation_id)
    return {**get_rate(session, fx_rate_id), "superseded": superseded}


def deactivate_rate(session: Session, *, actor: str, fx_rate_id: str, reason: str,
                    correlation_id: str | None = None) -> dict[str, Any]:
    row = get_rate(session, fx_rate_id)
    if not str(reason or "").strip():
        _err("FX_REASON_REQUIRED", "A reason is required to retire a rate.", status=422)
    if row["status"] != STATUS_ACTIVE:
        _err("FX_RATE_NOT_ACTIVE",
             f"{fx_rate_id} is {row['status'].lower()}, not active; only an active rate is retired.",
             status=409)
    session.execute(
        "UPDATE fx_rate SET active = false, deactivated_at = now(), deactivated_by = %s, "
        "deactivation_reason = %s, updated_by = %s WHERE fx_rate_id = %s",
        (actor, str(reason).strip(), actor, fx_rate_id))
    audit_mod.append(session, actor, "FX_RATE_DEACTIVATED", OBJECT_TYPE, fx_rate_id,
                     f"retired: {str(reason).strip()}", correlation_id=correlation_id)
    return get_rate(session, fx_rate_id)


# ============================================================ import
def import_preview(session: Session, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate every row and say what committing would do to each, creating
    nothing. Problems name the row (1-based) and the field."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        _err("FX_IMPORT_ROWS_REQUIRED", "rows must be a list of rate objects.", status=422)
    if len(rows) == 0:
        _err("FX_IMPORT_ROWS_REQUIRED", "rows is empty.", status=422)
    if len(rows) > IMPORT_ROW_LIMIT:
        _err("FX_IMPORT_TOO_LARGE",
             f"{len(rows)} rows exceeds the synchronous import limit of {IMPORT_ROW_LIMIT}.", status=422)
    out: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    seen: dict[tuple, int] = {}
    for index, raw in enumerate(rows, start=1):
        entry: dict[str, Any] = {"row": index}
        if not isinstance(raw, Mapping):
            problems.append({"row": index, "field": None, "code": "FX_IMPORT_ROW_SHAPE",
                             "message": "each row must be an object."})
            entry.update({"action": "error", "problem": problems[-1]})
            out.append(entry)
            continue
        try:
            v = _normalise(session, raw)
        except FxAdminError as exc:
            problem = {"row": index, "field": _field_of(exc.code), "code": exc.code,
                       "message": exc.message}
            problems.append(problem)
            entry.update({"action": "error", "problem": problem})
            out.append(entry)
            continue
        key = (v["from_currency"], v["to_currency"], v["rate_date"], v["rate_source"])
        entry.update({"from_currency": key[0], "to_currency": key[1],
                      "rate_date": key[2].isoformat(), "rate": str(v["rate"]),
                      "rate_source": key[3], "source_reference": v["source_reference"]})
        if key in seen:
            problem = {"row": index, "field": "rate_date", "code": "FX_IMPORT_DUPLICATE_KEY",
                       "message": f"row {seen[key]} already quotes {key[0]}/{key[1]} for "
                                  f"{key[2].isoformat()} from {key[3]}."}
            problems.append(problem)
            entry.update({"action": "error", "problem": problem})
            out.append(entry)
            continue
        seen[key] = index
        active = _active_for_key(session, *key)
        pending = _pending_for_key(session, *key)
        if active is not None and Decimal(_to_dict(active)["rate"]) == v["rate"]:
            entry.update({"action": "existing", "fx_rate_id": _to_dict(active)["fx_rate_id"]})
        elif pending is not None and Decimal(_to_dict(pending)["rate"]) == v["rate"]:
            entry.update({"action": "existing", "fx_rate_id": _to_dict(pending)["fx_rate_id"]})
        elif pending is not None:
            problem = {"row": index, "field": "rate", "code": "FX_RATE_PENDING_CONFLICT",
                       "message": f"{_to_dict(pending)['fx_rate_id']} already awaits activation for "
                                  f"this key at {_to_dict(pending)['rate']}."}
            problems.append(problem)
            entry.update({"action": "error", "problem": problem})
        elif active is not None:
            entry.update({"action": "correction", "corrects": _to_dict(active)["fx_rate_id"],
                          "current_rate": _to_dict(active)["rate"]})
        else:
            entry.update({"action": "create"})
        out.append(entry)
    return {"rows": out, "problems": problems, "valid": not problems,
            "would_create": sum(1 for e in out if e.get("action") in ("create", "correction")),
            "would_skip": sum(1 for e in out if e.get("action") == "existing")}


def _field_of(code: str) -> str | None:
    return {
        "FX_CURRENCY_MALFORMED": "from_currency", "FX_CURRENCY_UNKNOWN": "from_currency",
        "FX_CURRENCY_WITHDRAWN": "from_currency", "FX_IDENTITY_RATE": "to_currency",
        "FX_DATE_INVALID": "rate_date", "FX_RATE_SOURCE_REQUIRED": "rate_source",
        "FX_RATE_SOURCE_TOO_LONG": "rate_source",
    }.get(code, "rate" if code.startswith("FX_RATE_") else None)


def import_rates(session: Session, *, actor: str, rows: Sequence[Mapping[str, Any]],
                 correlation_id: str | None = None) -> dict[str, Any]:
    """All or nothing. Every row is validated before any row is written; one
    problem refuses the whole payload with every problem listed. Rows that
    already exist at the same value are skipped, so re-sending a payload is a
    no-op rather than a second book of pending corrections."""
    preview = import_preview(session, rows)
    if not preview["valid"]:
        _err("FX_IMPORT_INVALID",
             f"{len(preview['problems'])} row problem(s); nothing was imported.",
             status=422, detail=preview["problems"])
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in preview["rows"]:
        result = create_rate(
            session, actor=actor, from_currency=entry["from_currency"], to_currency=entry["to_currency"],
            rate_date=entry["rate_date"], rate=entry["rate"], rate_source=entry["rate_source"],
            source_reference=entry.get("source_reference"), correlation_id=correlation_id)
        if result.get("created"):
            created.append(result)
        else:
            skipped.append(result["fx_rate_id"])
    audit_mod.append(session, actor, "FX_RATE_IMPORT", OBJECT_TYPE, "IMPORT",
                     f"{len(created)} created (pending activation), {len(skipped)} already on file",
                     correlation_id=correlation_id,
                     stream_key=f"{OBJECT_TYPE}:IMPORT")
    return {"created": len(created), "skipped": len(skipped), "items": created,
            "skipped_ids": skipped}
