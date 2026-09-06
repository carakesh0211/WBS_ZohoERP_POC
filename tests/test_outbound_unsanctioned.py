"""The detective control: a purchase order created directly in Zoho (§11.7).

Wave 5, stream 6.

§11.7 states the risk plainly and this file is its evidence. Choosing the WBS
Hub as the PR system of record buys the one thing that matters -- ``budget_check``
can *refuse* a commitment before it becomes one -- and it buys it at a price:
**a purchase order raised straight in Zoho never passes that check.**

Prevention is Zoho role configuration restricting PO creation on CAPEX vendors
and accounts. That is **D-8**, it is unresolved, and it may turn out to be
unenforceable in the client's tenant. So none of the code here depends on it.
Every test below runs as though prevention had failed, because that is the
condition the control has to work in.

What ships regardless is detection: Sweep A pulls every purchase order, any one
carrying a CAPEX dimension with no matching local ``external_id`` becomes an
``UNSANCTIONED_COMMITMENT``, it surfaces on SCR-25, and **it blocks period
close**. The commitment is not stopped -- by the time we see it, it exists --
but the CAPEX number is never *silently* wrong. It is loudly wrong, and
somebody has to resolve the exception before the books close. §11.7 calls that
the acceptable outcome, and it is only acceptable while the detection is real.

Which is why :func:`test_the_control_refuses_to_report_success_when_it_cannot_persist`
is the most important test in this file.

No network, no tenant, no Catalyst.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backend.integration import outbound as ob          # noqa: E402
from app.backend.pg import periods as periods_mod           # noqa: E402
from outbound_tenant_fake import NOW                        # noqa: E402


def observed(external_id, **kwargs):
    return ob.ObservedPurchaseOrder(external_id=external_id, **kwargs)


CAPEX = {"cf_wbs_code": "WBS-1000", "cf_budget_head": "BH-PLANT"}


# ======================================================================
# Detection
# ======================================================================

def test_a_purchase_order_raised_in_zoho_against_a_capex_cell_is_an_exception():
    """The case §11.7 names. A commitment nothing in this product approved."""
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions=CAPEX, entity_id="ENT-1",
                  total_paise=4_500_000_00)],
        known_external_ids=["ZPO-01", "ZPO-02"])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == ob.KIND_UNSANCTIONED_COMMITMENT
    assert finding.object_id == "ZPO-99"
    assert finding.object_type == "purchase_order"
    assert finding.entity_id == "ENT-1"
    assert finding.source_paise == 4_500_000_00
    assert finding.local_paise is None, (
        "There is no local record. Reporting a local figure would invent one.")
    assert finding.screen == "SCR-25"
    assert finding.blocks_period_close is True
    assert "bypassed the budget check" in finding.detail


def test_a_purchase_order_we_emitted_is_not_flagged():
    """The control has to be quiet about our own work or it is unusable."""
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-01", dimensions=CAPEX, entity_id="ENT-1")],
        known_external_ids=["ZPO-01"])
    assert findings == []


def test_a_purchase_order_with_no_capex_dimension_is_not_ours_to_flag():
    """The client's non-CAPEX procurement is none of this product's business.

    Flagging it would fill SCR-25 with every stationery order in the company,
    and an exception queue nobody can clear is an exception queue nobody reads.
    """
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-77", dimensions={"cf_department": "Admin"})],
        known_external_ids=[])
    assert findings == []


def test_an_empty_custom_field_is_not_a_capex_dimension():
    """Zoho returns every custom field on every record, mostly blank.

    Treating presence as significance would flag the whole tenant on the first
    sweep. Noise is how a detective control gets switched off, and a control
    switched off is the silent wrong number §11.7 rules out.
    """
    for blank in ("", "   ", None):
        findings = ob.detect_unsanctioned_commitments(
            [observed("ZPO-77", dimensions={"cf_wbs_code": blank,
                                            "cf_budget_head": blank})],
            known_external_ids=[])
        assert findings == [], f"blank {blank!r} was treated as a dimension"


def test_the_control_cannot_be_configured_to_see_nothing():
    """An empty dimension list makes every purchase order look innocent."""
    with pytest.raises(ob.OutboundError):
        ob.detect_unsanctioned_commitments(
            [observed("ZPO-77", dimensions=CAPEX)],
            known_external_ids=[], dimension_fields=())


def test_reporting_tags_work_as_well_as_custom_fields():
    """Z-02 may land as reporting tags if the plan restricts custom fields.

    The dimension field names are configurable for that reason, and the control
    does not care which mechanism the tenant ended up using.
    """
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions={"tag_wbs": "WBS-1000"})],
        known_external_ids=[], dimension_fields=("tag_wbs", "tag_budget_head"))
    assert len(findings) == 1


def test_every_unsanctioned_purchase_order_is_reported_not_just_the_first():
    findings = ob.detect_unsanctioned_commitments(
        [observed(f"ZPO-{i}", dimensions=CAPEX, entity_id="ENT-1")
         for i in range(5)],
        known_external_ids=["ZPO-2"])
    assert sorted(f.object_id for f in findings) == [
        "ZPO-0", "ZPO-1", "ZPO-3", "ZPO-4"]


def test_detection_takes_no_argument_saying_whether_prevention_is_on():
    """**The control does not depend on D-8, and cannot be made to.**

    §11.7: "Detective, always on regardless of D-8". A parameter that could
    switch this off when roles are believed to be configured would make the
    guarantee conditional on an unresolved decision -- and the day the role
    configuration turns out to be unenforceable is exactly the day the
    detection would have been switched off.
    """
    signature = inspect.signature(ob.detect_unsanctioned_commitments)
    names = set(signature.parameters)
    assert names == {"observed", "known_external_ids", "dimension_fields"}, (
        f"detect_unsanctioned_commitments takes {sorted(names)}; it must not "
        "grow a switch that couples detection to prevention (D-8).")


# ======================================================================
# Persistence, and the refusal to be silent
# ======================================================================

class _FakeSession:
    def __init__(self, *, tables=("reconciliation_exception",)):
        self.tables = set(tables)
        self.executed = []

    def fetchone(self, statement, params=None):
        if "information_schema.tables" in statement:
            return (1,) if params[0] in self.tables else None
        return None                                       # pragma: no cover

    def execute(self, statement, params=None):
        self.executed.append((statement, params))


def test_the_control_refuses_to_report_success_when_it_cannot_persist():
    """**The most important test in this file.**

    ``periods.py`` treats a missing ``reconciliation_exception`` table as "no
    open exceptions" -- correct for the period gate, catastrophic here. If this
    function did the same, the sweep would report success, the findings would
    evaporate, and the period would close over an unsanctioned commitment with
    nothing anywhere saying so.

    §11.7's bargain is that prevention may fail *because* detection always
    runs. A detective control that degrades to a no-op is worse than none,
    because it is believed.
    """
    session = _FakeSession(tables=())
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions=CAPEX, entity_id="ENT-1")],
        known_external_ids=[])

    with pytest.raises(ob.DetectiveControlUnavailable) as refused:
        ob.record_unsanctioned_commitments(session, findings)

    assert "period close would not be blocked" in str(refused.value)
    assert session.executed == []


def test_findings_are_written_as_open_exceptions_carrying_their_entity():
    session = _FakeSession()
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions=CAPEX, entity_id="ENT-1",
                  total_paise=4_500_000_00)],
        known_external_ids=[])

    written = ob.record_unsanctioned_commitments(session, findings, now=NOW)

    assert written == 1
    statement, params = session.executed[0]
    assert "INSERT INTO reconciliation_exception" in statement
    assert params["status"] == "Open"
    assert params["entity_id"] == "ENT-1"
    assert params["kind"] == ob.KIND_UNSANCTIONED_COMMITMENT
    assert params["source_paise"] == 4_500_000_00
    assert params["raised_at"] == NOW


def test_nothing_is_written_when_there_is_nothing_to_write():
    """No findings must not become an existence probe on every sweep."""
    session = _FakeSession(tables=())
    assert ob.record_unsanctioned_commitments(session, []) == 0
    assert session.executed == []


def test_re_running_the_sweep_does_not_raise_the_same_exception_twice():
    """A nightly sweep minting a fresh id per run fills SCR-25 with copies.

    The same reasoning as the dedupe key one layer up: an operator facing one
    new row per night for the same purchase order stops reading the queue, and
    a queue nobody reads is not a control.
    """
    finding = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions=CAPEX, entity_id="ENT-1")],
        known_external_ids=[])[0]

    assert ob.exception_id_for(finding) == ob.exception_id_for(finding)

    session = _FakeSession()
    ob.record_unsanctioned_commitments(session, [finding], now=NOW)
    ob.record_unsanctioned_commitments(session, [finding], now=NOW)
    ids = {params["exception_id"] for _, params in session.executed}
    assert len(ids) == 1
    assert "ON CONFLICT (exception_id) DO NOTHING" in session.executed[0][0]

    other = ob.detect_unsanctioned_commitments(
        [observed("ZPO-98", dimensions=CAPEX, entity_id="ENT-1")],
        known_external_ids=[])[0]
    assert ob.exception_id_for(other) != ob.exception_id_for(finding)


def test_the_finding_carries_exactly_what_the_period_gate_reads():
    """The seam between "we found it" and "the period cannot close".

    ``periods.transition_period`` refuses a CLOSE while
    ``reconciliation_exception`` holds a row with this ``entity_id`` and
    ``status = 'Open'``. Those two columns are the entire linkage. Asserted
    against ``periods.py``'s own source, so a change on either side of the seam
    breaks this test rather than quietly unblocking period close.
    """
    gate = inspect.getsource(periods_mod._has_open_reconciliation_exceptions)
    assert "entity_id = %s" in gate
    assert "status = 'Open'" in gate
    assert periods_mod._RECONCILIATION_EXCEPTION_TABLE == (
        ob._RECONCILIATION_EXCEPTION_TABLE)

    row = ob.ReconciliationFinding(
        kind=ob.KIND_UNSANCTIONED_COMMITMENT, object_type="purchase_order",
        object_id="ZPO-99", detail="d", entity_id="ENT-1").as_row()
    assert row["entity_id"] == "ENT-1"
    assert row["status"] == "Open"


def test_period_close_blockers_are_the_open_blocking_findings():
    findings = ob.detect_unsanctioned_commitments(
        [observed("ZPO-99", dimensions=CAPEX, entity_id="ENT-1")],
        known_external_ids=[])
    assert ob.period_close_blockers(findings) == findings

    from dataclasses import replace
    resolved = [replace(findings[0], status="Resolved")]
    assert ob.period_close_blockers(resolved) == []


# ======================================================================
# §11.7 -- a status change we did not initiate
# ======================================================================

def test_someone_opening_our_draft_in_zoho_is_an_exception_not_a_state_update():
    """§11.7: draft, then open **by our own call**, after our approval closes.

    A purchase order that moved to open without us is a commitment somebody
    made behind the control. Absorbing that as a state update would mean the
    hub learns the commitment exists and quietly agrees with it -- which is the
    same silent-wrong-number failure as the unsanctioned purchase order, just
    arriving through a different door.
    """
    findings = ob.detect_unsolicited_transitions(
        [observed("ZPO-01", status_raw="open", entity_id="ENT-1")],
        intended_states={"ZPO-01": ob.PO_STATE_DRAFT})

    assert len(findings) == 1
    assert findings[0].kind == ob.KIND_UNSOLICITED_EXTERNAL_TRANSITION
    assert findings[0].blocks_period_close is True
    assert "We did not initiate this transition" in findings[0].detail


def test_a_purchase_order_we_opened_ourselves_raises_nothing():
    findings = ob.detect_unsolicited_transitions(
        [observed("ZPO-01", status_raw="open")],
        intended_states={"ZPO-01": ob.PO_STATE_OPEN})
    assert findings == []


def test_a_status_we_have_no_mapping_for_is_reported_and_never_guessed():
    """C3: an unmapped raw value never guesses.

    Guessing is how a cancelled purchase order stays counted as committed. The
    raw value goes into the exception verbatim so a human decides.
    """
    findings = ob.detect_unsolicited_transitions(
        [observed("ZPO-01", status_raw="pending_approval_zoho_side")],
        intended_states={"ZPO-01": ob.PO_STATE_DRAFT},
        status_map={"open": ob.PO_STATE_OPEN})

    assert len(findings) == 1
    assert "pending_approval_zoho_side" in findings[0].detail


def test_a_mapped_status_matching_our_intent_raises_nothing():
    findings = ob.detect_unsolicited_transitions(
        [observed("ZPO-01", status_raw="Draft")],
        intended_states={"ZPO-01": ob.PO_STATE_DRAFT},
        status_map={"Draft": ob.PO_STATE_DRAFT})
    assert findings == []


def test_a_purchase_order_we_never_emitted_is_not_a_transition_finding():
    """That one is an UNSANCTIONED_COMMITMENT, and mislabelling it loses it."""
    findings = ob.detect_unsolicited_transitions(
        [observed("ZPO-99", status_raw="open")], intended_states={})
    assert findings == []


# ======================================================================
# The two controls together, as a sweep would run them
# ======================================================================

def test_a_sweep_separates_our_purchase_orders_from_the_ones_we_never_made():
    """One pass over what Sweep A returned, two kinds of exception.

    The tenant here holds four purchase orders: one of ours behaving, one of
    ours moved behind our back, one raised directly in Zoho against a CAPEX
    cell, and one ordinary non-CAPEX order that is none of our business. Every
    one of them has to land in exactly the right bucket, because the difference
    between the buckets is the difference between "someone changed our record"
    and "someone committed money we never approved".
    """
    tenant_pos = [
        observed("ZPO-01", dimensions=CAPEX, status_raw="draft",
                 entity_id="ENT-1", total_paise=100_000_00),
        observed("ZPO-02", dimensions=CAPEX, status_raw="open",
                 entity_id="ENT-1", total_paise=200_000_00),
        observed("ZPO-99", dimensions=CAPEX, status_raw="open",
                 entity_id="ENT-1", total_paise=4_500_000_00),
        observed("ZPO-77", dimensions={"cf_department": "Admin"},
                 status_raw="open", entity_id="ENT-1"),
    ]
    ours = {"ZPO-01": ob.PO_STATE_DRAFT, "ZPO-02": ob.PO_STATE_DRAFT}

    unsanctioned = ob.detect_unsanctioned_commitments(
        tenant_pos, known_external_ids=ours)
    unsolicited = ob.detect_unsolicited_transitions(
        tenant_pos, intended_states=ours)

    assert [f.object_id for f in unsanctioned] == ["ZPO-99"]
    assert [f.object_id for f in unsolicited] == ["ZPO-02"]

    blockers = ob.period_close_blockers(unsanctioned + unsolicited)
    assert len(blockers) == 2, (
        "Both kinds block period close: one is money we never approved, the "
        "other is a commitment somebody made behind the control.")

    session = _FakeSession()
    assert ob.record_unsanctioned_commitments(session, blockers, now=NOW) == 2
    assert {p["entity_id"] for _, p in session.executed} == {"ENT-1"}
    assert all(p["status"] == "Open" for _, p in session.executed)
