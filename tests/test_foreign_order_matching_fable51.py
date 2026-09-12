"""A receive or bill against a non-INR order is refused with a coded
reconciliation exception until it carries its own currency and rate; nothing
is booked at face value (product owner decision 8, 2026-09-11; Fable 5.1).

Database-free. Every test drives the real sweeps through the real runner with
the doubles in `integration_fakes.py`, or calls the real DTO and adapter code
directly. The PostgreSQL half -- the ledger's own refusal, the booking at the
computed paise, the rounding -- is `tests/test_pg_foreign_order_matching_
fable51.py`, and the two files pin the same code string from both sides.

WHAT IS PROVEN HERE

* The code is ONE string in every place a kind lives: the sweeps, the
  ledger's error code, the store's transcription of the CHECK, the C18
  registry and migration 030.
* A receive against a JPY order is held under FOREIGN_CURRENCY_BASIS_MISSING
  before the linkage is even resolved: nothing on `grn_line`, nothing in the
  unattributed bucket, NO `source_paise`, and the receive still in the inbox.
  The exception names the order, its currency, the face amount with its
  currency, and what is missing.
* The same receive against an INR order is attributed exactly as before.
* A bill's own rate travels: `BillDTO.exchange_rate` (a decimal string; a
  float is refused at the adapter and again at `normalise`) reaches
  `mirror_bill` with a per-document `fx_rate_source`, and a bill that states
  no rate reaches it with None so the rate book decides.
* The ledger's coded refusal is filed by the bill-detail sweep as the
  reconciliation exception of that kind, with no `source_paise`, and the run
  completes; every OTHER ledger refusal still fails the run as before.
* A foreign document's face value never lands in `source_paise` on the two
  pre-existing bill exception branches either; an INR document's still does.
* The live JPY vector (PO-00006: 2,900,000 yen at 0.561 = Rs 16,26,900) and
  the half-up rule (5 yen at 0.561 = 280.5 paise -> 281) hold through the FX
  engine the ledger books with.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parent))

from app.backend.integration import dto, erp, jobs, sweeps  # noqa: E402
from app.backend.pg import fx  # noqa: E402
from app.backend.pg import integration_store as store_mod  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from integration_fakes import (  # noqa: E402
    ERP, T0, FakeAdapter, FakeClock, FakeLine, InMemoryStore, bill, receive,
)

ROOT = _TESTS_DIR.parent
KIND = "FOREIGN_CURRENCY_BASIS_MISSING"

#: The live JPY order in the demo organisation (docs/fable51/ERP_DEMO_
#: CONNECTION_PREP.md): PO-00006, 2,900,000 yen at 0.561, base Rs 16,26,900.
LIVE_YEN = 2_900_000
LIVE_RATE = Decimal("0.561")
LIVE_PAISE = 162_690_000


def _run(job, store, clock, **kwargs):
    store.enqueue(job.kind)
    return jobs.run_job(job, store=store, clock=clock, capabilities=ERP, **kwargs)


def _order(currency: str, **overrides) -> sweeps.LocalPurchaseOrder:
    fields = dict(po_id="PO-1", external_id="PO-EXT-1", entity_id="ENT-1",
                  project_id="PRJ-1", document_number="PO-00006",
                  currency_code=currency)
    fields.update(overrides)
    return sweeps.LocalPurchaseOrder(**fields)


def _receive_estate(currency: str, *, resolvable: bool = True):
    clock = FakeClock()
    store = InMemoryStore()
    store.open_pos.append(_order(currency))
    if resolvable:
        store.po_lines[("PO-EXT-1", "POL-1")] = "POLINE-1"
    adapter = FakeAdapter(clock, receives={"PO-EXT-1": [receive(
        1, lines=[FakeLine(purchase_order_line_external_id="POL-1",
                           line_total_paise=1_450_000)])]},
        seconds_per_call=1)
    job = sweeps.SweepPoAnchored(adapter, store, store.connection_id,
                                 external_source="ZOHO_ERP")
    return clock, store, job


def _held(store) -> list[dict]:
    return [e for e in store.exceptions.values() if e["kind"] == KIND]


# ================================================= one code, everywhere
def test_the_code_is_one_string_in_every_place_a_kind_lives():
    assert sweeps.KIND_FOREIGN_CURRENCY_BASIS_MISSING == KIND
    assert procurement.FOREIGN_CURRENCY_BASIS_MISSING == KIND
    assert KIND in sweeps.EXCEPTION_KINDS
    assert KIND in store_mod.EXCEPTION_KINDS
    registry = json.loads(
        (ROOT / "research" / "30_contracts" / "C18_domain_statuses.json")
        .read_text(encoding="utf-8"))
    assert KIND in registry["namespaces"]["exception_status"]["kinds"]
    assert set(store_mod.EXCEPTION_KINDS) == sweeps.EXCEPTION_KINDS
    migration = (ROOT / "migrations" / "pg"
                 / "030_foreign_currency_basis_missing.sql").read_text(encoding="utf-8")
    assert f"'{KIND}'" in migration
    assert "ck_reconciliation_exception_kind" in migration


# ================================================= the receive, JPY order
def test_a_receive_against_a_jpy_order_is_held_under_the_code_and_nothing_is_booked():
    clock, store, job = _receive_estate("JPY")
    run = _run(job, store, clock)

    assert run.state == jobs.JOB_DONE
    assert store.receive_lines == {}, "nothing on grn_line"
    assert store.unattributed_paise == {}, "nothing in the bucket either"
    held = _held(store)
    assert len(held) == 1
    row = held[0]
    assert row["object_type"] == "grn_line"
    assert row["object_id"] == "GRN-EXT-0001:POL-1"
    assert row["source_paise"] is None and row["local_paise"] is None, (
        "a yen figure is never written into a paise column")
    assert row["project_id"] == "PRJ-1" and row["entity_id"] == "ENT-1", (
        "filed under the project and entity, so it blocks capitalisation "
        "and is visible to an entity-restricted principal")
    detail = row["detail"]
    for needle in ("PO-EXT-1", "PO-00006", "JPY", "1450000 JPY minor units",
                   "Missing:", "has NOT been booked as paise",
                   "stays in the inbox"):
        assert needle in detail, needle
    # The receive itself is still in the inbox, unmatched: the source
    # document stays recoverable for when a basis is supplied.
    assert any(key[1] == sweeps.MODULE_PURCHASE_RECEIVES
               and key[2] == "GRN-EXT-0001" for key in store.inbox)


def test_the_currency_is_decided_before_the_linkage_so_an_unresolvable_jpy_line_is_not_quarantined_at_face_value():
    """`GRN_LINE_UNATTRIBUTED` holds a line "at full value" in a paise bucket.
    Against a JPY order that value is yen, so the line must never reach that
    branch: the currency rule runs first and files its own kind."""
    clock, store, job = _receive_estate("JPY", resolvable=False)
    _run(job, store, clock)
    kinds = {e["kind"] for e in store.exceptions.values()}
    assert kinds == {KIND}
    assert sweeps.KIND_GRN_LINE_UNATTRIBUTED not in kinds
    assert store.unattributed_paise == {}
    assert store.receive_lines == {}


def test_the_hold_is_idempotent_across_the_re_walk():
    """The PO-anchored walk cycles by design. A second pass must find the
    same Open exception, not file a second one and not book anything."""
    clock, store, job = _receive_estate("JPY")
    _run(job, store, clock)
    _run(job, store, clock)
    assert len(_held(store)) == 1
    assert store.receive_lines == {}


def test_the_order_currency_comparison_is_case_and_whitespace_insensitive():
    clock, store, job = _receive_estate(" jpy ")
    _run(job, store, clock)
    assert len(_held(store)) == 1 and store.receive_lines == {}


# ================================================= the receive, INR order
def test_the_inr_order_path_is_unchanged():
    clock, store, job = _receive_estate("INR")
    run = _run(job, store, clock)
    assert run.state == jobs.JOB_DONE
    assert _held(store) == []
    assert store.exceptions == {}
    recorded = list(store.receive_lines.values())
    assert recorded == [{"quantity": 1, "amount_paise": 1_450_000}]


def test_a_local_purchase_order_defaults_to_the_base_currency():
    """Every existing construction of `LocalPurchaseOrder` omits the currency
    and is INR; the default must keep it so, and must be the same spelling
    the ledger uses."""
    po = sweeps.LocalPurchaseOrder(po_id="PO-1", external_id="PO-EXT-1")
    assert po.currency_code == sweeps.BASE_CURRENCY == fx.BASE_CURRENCY == "INR"


# ================================================= the bill's own rate
def _bill_estate(record):
    clock = FakeClock()
    store = InMemoryStore()
    store.detail_queue.append(record.external_id)
    adapter = FakeAdapter(clock, bill_details={record.external_id: record},
                          seconds_per_call=1)
    job = sweeps.SweepBillDetail(adapter, store, store.connection_id,
                                 external_source="ZOHO_ERP")
    return clock, store, job


def test_a_jpy_bill_with_its_own_rate_reaches_the_ledger_with_the_rate_and_a_per_document_source():
    record = bill(1, modified=T0, with_lines=True, total_paise=LIVE_YEN,
                  currency_code="JPY", exchange_rate="0.561")
    clock, store, job = _bill_estate(record)
    run = _run(job, store, clock)

    assert run.state == jobs.JOB_DONE
    assert len(store.mirrored_bills) == 1
    sent = store.mirrored_bills[0]
    assert sent["source_currency"] == "JPY"
    assert sent["exchange_rate"] == "0.561"
    assert isinstance(sent["exchange_rate"], str), "never a float"
    # Named per document: `fx_rate` is unique on (pair, date, source) and
    # never overwritten, so two bills dated the same day at two dealt rates
    # must each cite their own row.
    assert sent["fx_rate_source"] == "ZOHO_ERP bill BILL-EXT-0001"
    assert store.hydrated == ["BILL-EXT-0001"]


def test_a_bill_that_states_no_rate_reaches_the_ledger_with_none_so_the_rate_book_decides():
    record = bill(2, modified=T0, with_lines=True, currency_code="JPY")
    clock, store, job = _bill_estate(record)
    _run(job, store, clock)
    sent = store.mirrored_bills[0]
    assert sent["source_currency"] == "JPY"
    assert sent["exchange_rate"] is None
    assert sent["fx_rate_source"] is None


def test_normalise_carries_the_rate_as_text_and_refuses_a_float():
    record = sweeps.normalise(
        bill(3, modified=T0, currency_code="JPY", exchange_rate=" 0.561 "),
        module=sweeps.MODULE_BILLS)
    assert record.currency_code == "JPY" and record.exchange_rate == "0.561"
    assert sweeps.normalise(bill(4, modified=T0),
                            module=sweeps.MODULE_BILLS).exchange_rate is None
    with pytest.raises(sweeps.SweepError) as exc:
        sweeps.normalise(bill(5, modified=T0, currency_code="JPY",
                              exchange_rate=0.561),  # type: ignore[arg-type]
                         module=sweeps.MODULE_BILLS)
    assert "float" in str(exc.value) and "BILL-EXT-0005" in str(exc.value)


def test_rate_text_keeps_the_stated_string_and_refuses_a_float_or_a_boolean():
    assert dto.rate_text("0.561", field="bill.exchange_rate") == "0.561"
    assert dto.rate_text(1, field="bill.exchange_rate") == "1"
    assert dto.rate_text(None, field="bill.exchange_rate") is None
    assert dto.rate_text("", field="bill.exchange_rate") is None
    with pytest.raises(dto.DtoError):
        dto.rate_text(0.561, field="bill.exchange_rate")
    with pytest.raises(dto.DtoError):
        dto.rate_text(True, field="bill.exchange_rate")


def test_the_erp_adapter_populates_the_bills_rate_from_the_row():
    """A live ERP v3 bill row carries `currency_code` and `exchange_rate`
    (docs/fable51/ERP_DEMO_CONNECTION_PREP.md); with parse_float=str the rate
    is a string, and the DTO keeps it verbatim."""
    source = dto.SourceRef(product="ERP", service="erp", api_version="v3",
                           endpoint="/bills/1001",
                           retrieved_at=datetime(2026, 9, 11, tzinfo=timezone.utc))
    row = {"bill_id": "1001", "bill_number": "BILL-00001", "date": "2026-09-11",
           "last_modified_time": "2026-09-11T10:00:00+0530",
           "vendor_name": "Yen Vendor", "currency_code": "JPY",
           "exchange_rate": "0.561", "sub_total": "2900000",
           "total": "2900000", "status": "open", "line_items": []}
    mapped = erp._bill(row, source, hydrated=True)
    assert mapped.currency_code == "JPY"
    assert mapped.exchange_rate == "0.561"
    assert mapped.total_paise == LIVE_YEN, "JPY: exponent 0, not x100"
    assert erp._bill({**row, "exchange_rate": None}, source, hydrated=True).exchange_rate is None
    with pytest.raises(dto.DtoError):
        erp._bill({**row, "exchange_rate": 0.561}, source, hydrated=True)


# ================================================= the ledger's refusal
class _LedgerRefusal(Exception):
    """The shape of `pg.procurement.ProcurementIngestError` -- `.code`,
    `.message`, `.status` -- without importing the ledger, which is exactly
    the terms the sweep recognises the refusal on."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class _RefusingStore(InMemoryStore):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal
        self.mirror_calls = 0

    def mirror_bill(self, **kwargs):
        self.mirror_calls += 1
        raise self.refusal


def _refusing_estate(refusal):
    clock = FakeClock()
    store = _RefusingStore(refusal)
    record = bill(1, modified=T0, with_lines=True, total_paise=LIVE_YEN,
                  currency_code="JPY")
    store.detail_queue.append(record.external_id)
    adapter = FakeAdapter(clock, bill_details={record.external_id: record},
                          seconds_per_call=1)
    job = sweeps.SweepBillDetail(adapter, store, store.connection_id,
                                 external_source="ZOHO_ERP")
    return clock, store, job


def test_the_ledgers_coded_refusal_is_filed_as_the_reconciliation_exception_and_the_run_completes():
    sentence = ("Bill 'BILL-EXT-0001' is against purchase order 'PO-EXT-1', "
                "which is denominated in JPY, and it carries that currency "
                "but no usable rate. Missing: the bill's own rate to INR.")
    clock, store, job = _refusing_estate(_LedgerRefusal(KIND, sentence))
    run = _run(job, store, clock)

    assert run.state == jobs.JOB_DONE
    assert store.mirror_calls == 1
    held = _held(store)
    assert len(held) == 1
    row = held[0]
    assert row["object_type"] == "bill" and row["object_id"] == "BILL-EXT-0001"
    assert row["detail"] == sentence, "the ledger's own sentence, verbatim"
    assert row["source_paise"] is None and row["local_paise"] is None
    assert row["project_id"] == "PRJ-1" and row["entity_id"] == "ENT-1"
    # Hydrated -- the detail was fetched and paid for, and re-fetching it
    # every tick would not change what it lacks -- exactly as the other
    # held-at-full-value branches are. The inbox row is what stays.
    assert store.hydrated == ["BILL-EXT-0001"]
    assert any(key[1] == sweeps.MODULE_BILLS and key[2] == "BILL-EXT-0001"
               for key in store.inbox)


def test_every_other_ledger_refusal_still_propagates_and_files_nothing():
    clock, store, job = _refusing_estate(
        _LedgerRefusal("PURCHASE_ORDER_NOT_FOUND", "no such order", 404))
    run = _run(job, store, clock)
    assert run.state != jobs.JOB_DONE
    assert _held(store) == []
    assert store.hydrated == [], "a refused bill is not marked done"


def test_a_refusal_without_a_code_attribute_is_not_mistaken_for_the_hold():
    clock, store, job = _refusing_estate(RuntimeError(KIND))
    run = _run(job, store, clock)
    assert run.state != jobs.JOB_DONE and _held(store) == []


# ================================================= face value, never paise
def test_a_foreign_bills_face_value_never_lands_in_source_paise_on_the_other_branches():
    """The pre-existing "no line items" branch files CONTROL_TOTAL_MISMATCH
    with the bill's total. For a JPY bill that total is yen."""
    clock, store, job = _bill_estate(bill(1, modified=T0, total_paise=LIVE_YEN,
                                          currency_code="JPY"))
    _run(job, store, clock)
    rows = [e for e in store.exceptions.values()
            if e["kind"] == sweeps.KIND_CONTROL_TOTAL_MISMATCH]
    assert len(rows) == 1 and rows[0]["source_paise"] is None


def test_an_inr_bills_face_value_still_lands_in_source_paise_unchanged():
    clock, store, job = _bill_estate(bill(1, modified=T0, total_paise=123_456))
    _run(job, store, clock)
    rows = [e for e in store.exceptions.values()
            if e["kind"] == sweeps.KIND_CONTROL_TOTAL_MISMATCH]
    assert len(rows) == 1 and rows[0]["source_paise"] == 123_456


# ================================================= the arithmetic the ledger uses
def test_the_live_jpy_vector_and_the_half_up_rule_hold_through_the_fx_engine():
    """Same function, same rounding as `_translate_po_lines` and `mirror_bill`:
    integer minor units x exact Decimal rate x 10^(2 - exponent), quantised
    ONCE, half away from zero. No float anywhere."""
    assert fx.translate_to_base_paise(LIVE_YEN, LIVE_RATE, source_minor_exponent=0) == LIVE_PAISE
    # 5 yen x 0.561 = 2.805 rupees = 280.5 paise -> 281 (ROUND_HALF_UP).
    assert fx.translate_to_base_paise(5, LIVE_RATE, source_minor_exponent=0) == 281
    # 1,234,572 yen x 0.5615 = 693,212.178 rupees -> 69,321,217.8 -> 69,321,218.
    assert fx.translate_to_base_paise(1_234_572, Decimal("0.5615"), source_minor_exponent=0) == 69_321_218
    basis = fx.TranslationBasis(source_currency="JPY", minor_exponent=0,
                                rate=Decimal("0.5615"), rate_date=None,
                                rate_source="test", fx_rate_id="FXR-test")
    header, per_line = fx.translate_lines(basis, [1_234_567, 5])
    assert header == 69_321_218 and sum(per_line) == header
    with pytest.raises(fx.FxError):
        fx.translate_to_base_paise(5, 0.561, source_minor_exponent=0)  # type: ignore[arg-type]


def test_the_fake_record_only_carries_names_the_real_bill_dto_has():
    """The doubles are shaped to the PRODUCER (`test_integration_dto_reader_
    contract.py`'s rule). The two fields added for this decision exist on
    `BillDTO` under exactly these names."""
    from integration_fakes import FakeRecord
    real = {f.name for f in dataclasses.fields(dto.BillDTO)}
    for name in ("currency_code", "exchange_rate"):
        assert name in real
        assert name in {f.name for f in dataclasses.fields(FakeRecord)}


# ================== the poll's unsanctioned-commitment record on a foreign order
def test_a_foreign_unsanctioned_order_names_its_face_value_with_its_currency():
    """Live finding 2026-09-12 (DEMO WBS PO-00006): the JPY order's
    UNSANCTIONED_COMMITMENT exception carried source_paise=2900000 -- yen
    minor units in the one column the closure gate sums as rupees.
    source_paise is None for a non-INR order and the face value is named in
    detail WITH its currency, where nothing can add it up as paise."""
    import dataclasses
    from tests.integration_fakes import (ERP, T0, FakeAdapter, FakeClock, FakePage,
                                         InMemoryStore, purchase_order)
    clock = FakeClock()
    store = InMemoryStore()
    jpy = dataclasses.replace(purchase_order(6, modified=T0, capex_ref="DEMO-JPY-0001",
                                             total_paise=2_900_000), currency_code="JPY")
    inr = purchase_order(1, modified=T0, capex_ref="DEMO-INR-0001", total_paise=562_000_000)
    adapter = FakeAdapter(clock, po_pages=[FakePage((jpy, inr), False)], seconds_per_call=1)
    job = sweeps.poll_purchaseorders(adapter, store, store.connection_id)
    store.enqueue(job.kind)
    jobs.run_job(job, store=store, clock=clock, capabilities=ERP)
    raised = {e["object_id"]: e for e in store.exceptions.values()
              if e["kind"] == sweeps.KIND_UNSANCTIONED_COMMITMENT}
    assert raised["PO-EXT-0006"]["source_paise"] is None
    assert "JPY 2900000" in raised["PO-EXT-0006"]["detail"]
    assert "not paise" in raised["PO-EXT-0006"]["detail"]
    assert raised["PO-EXT-0001"]["source_paise"] == 562_000_000
    assert "Face value" not in raised["PO-EXT-0001"]["detail"]


def test_an_inr_bill_stating_the_identity_rate_reaches_the_ledger_with_no_rate():
    """VERIFIED LIVE 2026-09-12 (DEMO WBS bill 3912780000000117004): Zoho ERP
    states `exchange_rate: 1` on every base-currency document. That is the
    identity, not provenance; forwarded as a stated rate it is refused by
    mirror_bill (FX_IDENTITY_TRANSLATION) and the first live bill-detail sweep
    failed on it. The sweep now drops the tenant's rate for a base-currency
    bill and forwards None, so the ledger's identity path applies."""
    record = bill(1, modified=T0, with_lines=True, total_paise=185_000_000,
                  currency_code="INR", exchange_rate="1")
    clock, store, job = _bill_estate(record)
    run = _run(job, store, clock)
    assert run.state == jobs.JOB_DONE, run.error
    assert len(store.mirrored_bills) == 1
    sent = store.mirrored_bills[0]
    assert sent["source_currency"] == "INR"
    assert sent["exchange_rate"] is None and sent["fx_rate_source"] is None
