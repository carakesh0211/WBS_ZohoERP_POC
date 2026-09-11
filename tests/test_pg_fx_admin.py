"""Exchange-rate administration against a LIVE PostgreSQL (Fable 5.1,
migration 028, ``pg/fx_admin.py``). Skipped without CAPEX_DB_URL, and a skip
is not a pass. What only a server can answer:

* a quote is created INACTIVE, is invisible to the lookup until a DIFFERENT
  user activates it, and the lookup then returns the exact decimal string;
* the creator may not activate their own quote (FX_SELF_ACTIVATION) while
  fx_policy.FX_ACTIVATION_SEPARATION is REQUIRED, and may once it is WAIVED;
* retiring a quote makes the lookup refuse (FX_RATE_UNAVAILABLE) rather
  than answer the retired value, and a retired quote cannot be reactivated;
* a date nothing covers -- a gap between two quoted days -- is refused, never
  filled from the neighbouring day;
* history is immutable AT THE DATABASE: UPDATE of the value and DELETE of a
  row are refused by trigger, whoever asks;
* a correction is a NEW row that, on activation, supersedes the old one and
  leaves its value in place, and history shows both;
* JPY (exponent 0) and KWD (exponent 3) preview to the paise the engine books;
* import is all-or-nothing and re-sending a payload creates nothing;
* the list pages by cursor without duplicates or gaps;
* ``pg/fx.py::resolve_basis`` -- the bill write path -- refuses a retired
  quote and an inactive fx_rate_id, so an inactive rate translates nothing;
* every mutation leaves an intact audit chain on FX_RATE:<id>.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

from app.backend.pg import audit as audit_mod                 # noqa: E402
from app.backend.pg import fx                                 # noqa: E402
from app.backend.pg import fx_admin as adm                    # noqa: E402
from app.backend.pg.engine import Scope                       # noqa: E402

pytestmark = pytest.mark.pg

MAKER = "U-FX-MAKER"
CHECKER = "U-FX-CHECKER"


def _scope(user: str = MAKER) -> Scope:
    return Scope(user_id=user, principal_kind="USER", read_all=True)


def _create(db, *, actor=MAKER, currency="USD", on="2026-09-01", rate="83.80",
            source="RBI_REFERENCE", to="INR", **extra):
    with db.session(_scope(actor)) as s:
        return adm.create_rate(s, actor=actor, from_currency=currency, to_currency=to,
                               rate_date=on, rate=rate, rate_source=source, **extra)


def _activate(db, fx_rate_id, *, actor=CHECKER):
    with db.session(_scope(actor)) as s:
        return adm.activate_rate(s, actor=actor, fx_rate_id=fx_rate_id)


def _lookup(db, *, currency="USD", on="2026-09-01", **kw):
    with db.session(_scope()) as s:
        return adm.lookup_rate(s, from_currency=currency, on_date=on, **kw)


def _get(db, fx_rate_id):
    with db.session(_scope()) as s:
        return adm.get_rate(s, fx_rate_id)


def _err(fn, *a, **kw):
    with pytest.raises(adm.FxAdminError) as excinfo:
        fn(*a, **kw)
    return excinfo.value


# ================================================================ lifecycle
def test_a_quote_is_pending_until_another_user_activates_it(pg_database):
    row = _create(pg_database)
    assert row["created"] is True
    assert row["status"] == adm.STATUS_PENDING and row["active"] is False
    assert row["rate"] == "83.80000000"           # exact, at numeric(18,8) scale
    assert row["created_by"] == MAKER and row["activated_by"] is None

    refusal = _err(_lookup, pg_database)
    assert refusal.code == "FX_RATE_UNAVAILABLE" and refusal.status == 404

    live = _activate(pg_database, row["fx_rate_id"])
    assert live["status"] == adm.STATUS_ACTIVE and live["active"] is True
    assert live["activated_by"] == CHECKER and live["activated_at"]
    assert live["superseded"] == []

    found = _lookup(pg_database)
    assert found["fx_rate_id"] == row["fx_rate_id"]
    assert found["rate"] == "83.80000000" and found["identity"] is False
    assert found["source_minor_exponent"] == 2 and found["target_minor_exponent"] == 2


def test_the_maker_may_not_activate_their_own_quote_unless_the_policy_is_waived(
        pg_database, pg_connection):
    row = _create(pg_database)
    refusal = _err(_activate, pg_database, row["fx_rate_id"], actor=MAKER)
    assert refusal.code == "FX_SELF_ACTIVATION"
    assert _get(pg_database, row["fx_rate_id"])["status"] == adm.STATUS_PENDING

    pg_connection.execute("UPDATE fx_policy SET policy_value = 'WAIVED' "
                          "WHERE policy_key = 'FX_ACTIVATION_SEPARATION'")
    pg_connection.commit()
    try:
        live = _activate(pg_database, row["fx_rate_id"], actor=MAKER)
        assert live["status"] == adm.STATUS_ACTIVE and live["activated_by"] == MAKER
    finally:
        pg_connection.execute("UPDATE fx_policy SET policy_value = 'REQUIRED' "
                              "WHERE policy_key = 'FX_ACTIVATION_SEPARATION'")
        pg_connection.commit()


def test_the_policy_refuses_a_value_it_does_not_name(pg_connection):
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute("UPDATE fx_policy SET policy_value = 'SOMETIMES' "
                              "WHERE policy_key = 'FX_ACTIVATION_SEPARATION'")
    pg_connection.rollback()


def test_a_retired_quote_is_refused_by_the_lookup_and_cannot_come_back(pg_database):
    row = _create(pg_database, on="2026-09-02")
    _activate(pg_database, row["fx_rate_id"])
    assert _lookup(pg_database, on="2026-09-02")["rate"] == "83.80000000"

    with pg_database.session(_scope(CHECKER)) as s:
        retired = adm.deactivate_rate(s, actor=CHECKER, fx_rate_id=row["fx_rate_id"],
                                      reason="Keyed against the wrong day.")
    assert retired["status"] == adm.STATUS_RETIRED and retired["active"] is False
    assert retired["deactivated_by"] == CHECKER
    assert retired["rate"] == "83.80000000", "retiring never touches the value"

    refusal = _err(_lookup, pg_database, on="2026-09-02")
    assert refusal.code == "FX_RATE_UNAVAILABLE" and refusal.status == 404

    again = _err(_activate, pg_database, row["fx_rate_id"])
    assert again.code == "FX_RATE_RETIRED"

    with pg_database.session(_scope(CHECKER)) as s:
        blank = _err(adm.deactivate_rate, s, actor=CHECKER, fx_rate_id=row["fx_rate_id"], reason="   ")
    assert blank.code == "FX_REASON_REQUIRED"


def test_a_gap_between_two_quoted_days_is_refused_not_filled(pg_database):
    for day in ("2026-09-10", "2026-09-12"):
        _activate(pg_database, _create(pg_database, on=day)["fx_rate_id"])
    assert _lookup(pg_database, on="2026-09-10")["fx_rate_id"]
    assert _lookup(pg_database, on="2026-09-12")["fx_rate_id"]
    refusal = _err(_lookup, pg_database, on="2026-09-11")
    assert refusal.code == "FX_RATE_UNAVAILABLE"
    assert "2026-09-11" in refusal.message


# ================================================================ immutability
def test_history_is_immutable_at_the_database(pg_database, pg_connection):
    row = _create(pg_database, on="2026-09-03")
    _activate(pg_database, row["fx_rate_id"])

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        pg_connection.execute("UPDATE fx_rate SET rate = 99 WHERE fx_rate_id = %s", (row["fx_rate_id"],))
    assert "immutable" in str(excinfo.value)
    pg_connection.rollback()

    for column, value in (("rate_date", date(2020, 1, 1)), ("rate_source", "X"),
                          ("from_currency", "EUR"), ("created_by", "SOMEONE")):
        with pytest.raises(psycopg.errors.RestrictViolation):
            pg_connection.execute(f"UPDATE fx_rate SET {column} = %s WHERE fx_rate_id = %s",
                                  (value, row["fx_rate_id"]))
        pg_connection.rollback()

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        pg_connection.execute("DELETE FROM fx_rate WHERE fx_rate_id = %s", (row["fx_rate_id"],))
    assert "append-only" in str(excinfo.value)
    pg_connection.rollback()

    assert _get(pg_database, row["fx_rate_id"])["rate"] == "83.80000000"


def test_a_correction_supersedes_and_history_keeps_both(pg_database):
    first = _create(pg_database, on="2026-09-04", rate="83.10")
    _activate(pg_database, first["fx_rate_id"])

    # The same quote again is not a second row.
    same = _create(pg_database, on="2026-09-04", rate="83.10")
    assert same["created"] is False and same["fx_rate_id"] == first["fx_rate_id"]

    fix = _create(pg_database, on="2026-09-04", rate="83.91")
    assert fix["created"] is True and fix["status"] == adm.STATUS_PENDING
    assert fix["note"] == f"Correction of {first['fx_rate_id']}."
    assert _lookup(pg_database, on="2026-09-04")["rate"] == "83.10000000", \
        "a pending correction changes nothing until activated"

    # A second, different pending correction for the same key is refused.
    clash = _err(_create, pg_database, on="2026-09-04", rate="83.92")
    assert clash.code == "FX_RATE_PENDING_CONFLICT"

    live = _activate(pg_database, fix["fx_rate_id"])
    assert live["superseded"] == [first["fx_rate_id"]]
    assert _lookup(pg_database, on="2026-09-04")["rate"] == "83.91000000"

    old = _get(pg_database, first["fx_rate_id"])
    assert old["status"] == adm.STATUS_SUPERSEDED and old["active"] is False
    assert old["superseded_by"] == fix["fx_rate_id"]
    assert old["rate"] == "83.10000000", "the superseded row keeps the rate bills cite"

    with pg_database.session(_scope()) as s:
        hist = adm.history(s, from_currency="USD")
    ids = [h["fx_rate_id"] for h in hist["items"]]
    assert first["fx_rate_id"] in ids and fix["fx_rate_id"] in ids
    statuses = {h["fx_rate_id"]: h["status"] for h in hist["items"]}
    assert statuses[first["fx_rate_id"]] == adm.STATUS_SUPERSEDED
    assert statuses[fix["fx_rate_id"]] == adm.STATUS_ACTIVE

    reactivate = _err(_activate, pg_database, first["fx_rate_id"])
    assert reactivate.code == "FX_RATE_RETIRED"


# ================================================================ decimals, exponents
def test_the_rate_is_an_exact_decimal_string_never_a_float(pg_database):
    for bad, code in ((83.8, "FX_RATE_IS_A_FLOAT"), ("0", "FX_RATE_NOT_POSITIVE"),
                      ("83.123456789", "FX_RATE_TOO_PRECISE"), ("abc", "FX_RATE_INVALID"),
                      (None, "FX_RATE_REQUIRED")):
        refusal = _err(_create, pg_database, on="2026-09-05", rate=bad)
        assert refusal.code == code and refusal.status == 422
    row = _create(pg_database, on="2026-09-05", rate="83.12345678")
    assert row["rate"] == "83.12345678"
    identity = _err(_create, pg_database, on="2026-09-05", currency="INR")
    assert identity.code == "FX_IDENTITY_RATE"
    unknown = _err(_create, pg_database, on="2026-09-05", currency="XXX")
    assert unknown.code == "FX_CURRENCY_UNKNOWN"


def test_jpy_and_kwd_preview_at_their_own_exponents(pg_database):
    jpy = _create(pg_database, currency="JPY", on="2026-09-06", rate="0.55")
    kwd = _create(pg_database, currency="KWD", on="2026-09-06", rate="270.5")
    _activate(pg_database, jpy["fx_rate_id"])
    _activate(pg_database, kwd["fx_rate_id"])
    assert jpy["source_minor_exponent"] == 0 and kwd["source_minor_exponent"] == 3

    # 100000 yen at 0.55 = Rs 55,000.00 = 5,500,000 paise (exponent 0 -> x100).
    yen = _lookup(pg_database, currency="JPY", on="2026-09-06", amount_minor=100_000)
    assert yen["source_minor_exponent"] == 0 and yen["translated_paise"] == 5_500_000
    # 1000 fils = 1.000 KWD at 270.5 = Rs 270.50 = 27,050 paise (exponent 3 -> /10).
    fils = _lookup(pg_database, currency="KWD", on="2026-09-06", amount_minor=1_000)
    assert fils["source_minor_exponent"] == 3 and fils["translated_paise"] == 27_050
    # And the same figures parsed as if they had two decimals would be wrong
    # by a hundredfold and tenfold respectively, which is the point.
    assert yen["translated_paise"] != 55_000 and fils["translated_paise"] != 270_500

    with pg_database.session(_scope()) as s:
        bad = _err(adm.lookup_rate, s, from_currency="JPY", on_date="2026-09-06", amount_minor=1.5)
    assert bad.code == "MONEY_NOT_INTEGER"


def test_two_active_sources_for_one_day_are_ambiguous_until_named(pg_database):
    a = _create(pg_database, currency="EUR", on="2026-09-07", rate="97.50", source="RBI_REFERENCE")
    b = _create(pg_database, currency="EUR", on="2026-09-07", rate="97.80", source="BANK_DEALT")
    _activate(pg_database, a["fx_rate_id"])
    _activate(pg_database, b["fx_rate_id"])
    refusal = _err(_lookup, pg_database, currency="EUR", on="2026-09-07")
    assert refusal.code == "FX_RATE_AMBIGUOUS" and refusal.status == 409
    assert refusal.detail == {"sources": ["BANK_DEALT", "RBI_REFERENCE"]}
    assert _lookup(pg_database, currency="EUR", on="2026-09-07", rate_source="BANK_DEALT")["rate"] == "97.80000000"


# ================================================================ import
def test_import_is_all_or_nothing_and_resending_creates_nothing(pg_database, pg_connection):
    rows = [
        {"from_currency": "USD", "rate_date": "2026-10-01", "rate": "84.01", "rate_source": "RBI_REFERENCE"},
        {"from_currency": "EUR", "rate_date": "2026-10-01", "rate": "98.02", "rate_source": "RBI_REFERENCE"},
        {"from_currency": "JPY", "rate_date": "2026-10-01", "rate": "0.57", "rate_source": "RBI_REFERENCE"},
    ]
    bad = rows + [{"from_currency": "USD", "rate_date": "2026-10-02", "rate": 84.5,
                   "rate_source": "RBI_REFERENCE"},
                  {"from_currency": "USD", "rate_date": "2026-10-01", "rate": "84.01",
                   "rate_source": "RBI_REFERENCE"}]
    before = pg_connection.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0]
    with pg_database.session(_scope()) as s:
        refusal = _err(adm.import_rates, s, actor=MAKER, rows=bad)
    assert refusal.code == "FX_IMPORT_INVALID" and refusal.status == 422
    assert [(p["row"], p["code"]) for p in refusal.detail] == \
        [(4, "FX_RATE_IS_A_FLOAT"), (5, "FX_IMPORT_DUPLICATE_KEY")]
    after = pg_connection.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0]
    assert after == before, "a problem in one row must create no rows at all"

    with pg_database.session(_scope()) as s:
        preview = adm.import_preview(s, rows)
    assert preview["valid"] and preview["would_create"] == 3 and preview["would_skip"] == 0

    with pg_database.session(_scope()) as s:
        first = adm.import_rates(s, actor=MAKER, rows=rows)
    assert first["created"] == 3 and first["skipped"] == 0
    assert all(it["status"] == adm.STATUS_PENDING for it in first["items"])

    with pg_database.session(_scope()) as s:
        again = adm.import_rates(s, actor=MAKER, rows=rows)
    assert again["created"] == 0 and again["skipped"] == 3
    assert sorted(again["skipped_ids"]) == sorted(it["fx_rate_id"] for it in first["items"])
    assert pg_connection.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0] == before + 3

    with pg_database.session(_scope()) as s:
        preview = adm.import_preview(s, rows)
    assert preview["would_create"] == 0 and preview["would_skip"] == 3


# ================================================================ pagination
def test_the_list_pages_by_cursor_without_duplicates_or_gaps(pg_database):
    ids = set()
    for i in range(7):
        on = f"2026-11-{i + 1:02d}"
        ids.add(_create(pg_database, currency="KWD", on=on, rate=f"27{i}.5")["fx_rate_id"])
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        with pg_database.session(_scope()) as s:
            page = adm.list_rates(s, from_currency="KWD", effective_from=date(2026, 11, 1),
                                  effective_to=date(2026, 11, 30), limit=3, cursor=cursor)
        pages += 1
        seen.extend(it["fx_rate_id"] for it in page["items"])
        assert len(page["items"]) <= 3
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and len(seen) == 7 and set(seen) == ids
    dates = [it for it in seen]
    assert len(dates) == len(set(dates)), "no id appears twice across pages"

    with pg_database.session(_scope()) as s:
        pending = adm.list_rates(s, from_currency="KWD", status=adm.STATUS_PENDING, limit=50)
        active = adm.list_rates(s, from_currency="KWD", active=True, limit=50)
        bad = _err(adm.list_rates, s, cursor="{not json")
    assert {it["fx_rate_id"] for it in pending["items"]} >= ids
    assert not ({it["fx_rate_id"] for it in active["items"]} & ids)
    assert bad.code == "INVALID_CURSOR"


# ================================================================ the write path
def test_the_bill_translation_path_refuses_an_inactive_quote(pg_database):
    """``fx.resolve_basis`` is what the ingestion calls. A retired quote for
    the document date is UNAVAILABLE, and an inactive fx_rate_id named
    explicitly is INACTIVE -- in neither case is anything translated."""
    row = _create(pg_database, currency="EUR", on="2026-09-08", rate="97.00")
    with pg_database.session(_scope()) as s:
        with pytest.raises(fx.FxError) as pending:
            fx.resolve_basis(s, source_currency="EUR", document_date=date(2026, 9, 8), actor=MAKER)
        assert pending.value.code == "FX_RATE_UNAVAILABLE"
        with pytest.raises(fx.FxError) as named:
            fx.resolve_basis(s, source_currency="EUR", document_date=date(2026, 9, 8), actor=MAKER,
                             fx_rate_id=row["fx_rate_id"])
        assert named.value.code == "FX_RATE_INACTIVE"

    _activate(pg_database, row["fx_rate_id"])
    with pg_database.session(_scope()) as s:
        basis = fx.resolve_basis(s, source_currency="EUR", document_date=date(2026, 9, 8), actor=MAKER)
    assert basis.fx_rate_id == row["fx_rate_id"] and str(basis.rate) == "97.00000000"

    with pg_database.session(_scope(CHECKER)) as s:
        adm.deactivate_rate(s, actor=CHECKER, fx_rate_id=row["fx_rate_id"], reason="retired for the test")
    with pg_database.session(_scope()) as s:
        with pytest.raises(fx.FxError) as retired:
            fx.resolve_basis(s, source_currency="EUR", document_date=date(2026, 9, 8), actor=MAKER)
        assert retired.value.code == "FX_RATE_UNAVAILABLE"


def test_record_rate_from_a_vendor_document_still_finds_only_the_active_row(pg_database):
    """The ingestion's own writer keys on the natural key; after a
    supersession it must find the correction, not the superseded row."""
    first = _create(pg_database, currency="EUR", on="2026-09-09", rate="97.10", source="VENDOR_DOC")
    _activate(pg_database, first["fx_rate_id"])
    fix = _create(pg_database, currency="EUR", on="2026-09-09", rate="97.20", source="VENDOR_DOC")
    _activate(pg_database, fix["fx_rate_id"])
    with pg_database.session(_scope()) as s:
        found = fx.record_rate(s, from_currency="EUR", rate_date=date(2026, 9, 9), rate="97.20",
                               rate_source="VENDOR_DOC", actor=MAKER)
        assert found == {"fx_rate_id": fix["fx_rate_id"], "rate": "97.20000000", "created": False}
        with pytest.raises(fx.FxError) as conflict:
            fx.record_rate(s, from_currency="EUR", rate_date=date(2026, 9, 9), rate="97.10",
                           rate_source="VENDOR_DOC", actor=MAKER)
        assert conflict.value.code == "FX_RATE_CONFLICT"


# ================================================================ audit
def test_every_mutation_leaves_an_intact_audit_chain(pg_database, pg_connection):
    first = _create(pg_database, currency="KWD", on="2026-09-20", rate="271.0")
    _activate(pg_database, first["fx_rate_id"])
    fix = _create(pg_database, currency="KWD", on="2026-09-20", rate="271.5")
    _activate(pg_database, fix["fx_rate_id"])
    with pg_database.session(_scope(CHECKER)) as s:
        adm.deactivate_rate(s, actor=CHECKER, fx_rate_id=fix["fx_rate_id"], reason="done")

    def actions(fx_rate_id):
        return [r[0] for r in pg_connection.execute(
            "SELECT action FROM audit_log WHERE object_type = 'FX_RATE' AND object_id = %s ORDER BY seq",
            (fx_rate_id,)).fetchall()]

    assert actions(first["fx_rate_id"]) == ["FX_RATE_CREATED", "FX_RATE_ACTIVATED", "FX_RATE_SUPERSEDED"]
    assert actions(fix["fx_rate_id"]) == ["FX_RATE_CREATED", "FX_RATE_ACTIVATED", "FX_RATE_DEACTIVATED"]
    with pg_database.session(_scope()) as s:
        for fx_rate_id in (first["fx_rate_id"], fix["fx_rate_id"]):
            assert audit_mod.verify_chain(s, f"FX_RATE:{fx_rate_id}")["intact"] is True


def test_an_unknown_id_is_404_on_every_operation(pg_database):
    with pg_database.session(_scope(CHECKER)) as s:
        for fn, kw in ((adm.get_rate, {}),
                       (adm.activate_rate, {"actor": CHECKER}),
                       (adm.deactivate_rate, {"actor": CHECKER, "reason": "x"})):
            with pytest.raises(adm.FxAdminError) as excinfo:
                if fn is adm.get_rate:
                    fn(s, "FXR-NONE")
                else:
                    fn(s, fx_rate_id="FXR-NONE", **kw)
            assert excinfo.value.code == "FX_RATE_NOT_FOUND" and excinfo.value.status == 404
