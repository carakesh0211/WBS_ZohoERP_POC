"""Behaviour of ``app.backend.integration.statuses``.

The contract file next door checks that the registries are internally honest.
This one checks that the code applying them behaves the way plan v1.2.1
section 8 says it must, under the cases that actually go wrong:

  * a raw value nobody has mapped
  * a raw value someone mapped but nobody sourced
  * a raw value that deliberately means nothing
  * the same raw string arriving on the wrong product
  * an object with several outbox rows disagreeing about its badge

No database, no network, no tenant.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.backend.integration import statuses as S


# ===========================================================================
# The registries load, and load once
# ===========================================================================
def test_the_registries_are_read_from_the_contract_files_not_rebuilt():
    assert S.integration_registry() is S.integration_registry()
    assert S.zoho_status_map() is S.zoho_status_map()


def test_the_four_namespaces_are_the_four_the_plan_names():
    assert set(S.integration_registry().namespaces) == {"inbox", "outbox", "job", "circuit"}


def test_a_namespace_that_does_not_exist_says_so_rather_than_returning_empty():
    with pytest.raises(KeyError, match="not a C16 namespace"):
        S.integration_registry()["webhook"]


# ===========================================================================
# Transitions - the only route to a persisted C16 state
# ===========================================================================
def test_a_row_is_created_in_the_state_the_registry_declares():
    reg = S.integration_registry()
    assert reg.inbox.create("RECEIVE") == "RECEIVED"
    assert reg.outbox.create("ENQUEUE") == "PENDING"
    assert reg.job.create("ENQUEUE") == "PENDING"
    assert reg.circuit.create("INITIALISE") == "CIRCUIT_CLOSED"


def test_the_declared_initial_status_is_the_one_creation_actually_produces():
    """Two ways of saying the same thing must not drift apart."""
    for ns in S.integration_registry().namespaces.values():
        created = {ns.transition(None, t) for t in ns.triggers_from(None)}
        assert ns.initial_status in created, ns.name


def test_an_undeclared_transition_raises_rather_than_defaulting():
    """Fail-closed. A silent default is how an undeclared state reaches a table."""
    with pytest.raises(S.IllegalTransition, match="no transition from 'PROCESSED'"):
        S.integration_registry().inbox.transition("PROCESSED", "APPLY_OK")


def test_an_undeclared_state_is_rejected_before_the_trigger_is_considered():
    with pytest.raises(S.IllegalTransition, match="not a declared status"):
        S.integration_registry().job.transition("RUNNING", "COMPLETE")


def test_a_quarantined_payload_is_resolved_by_an_operator_never_by_a_retry():
    """QUARANTINED means we could not attribute or map it. Retrying the same
    payload produces the same failure; only new information resolves it."""
    inbox = S.integration_registry().inbox
    assert inbox.transition("RECEIVED", "UNMAPPED_EXTERNAL_STATUS") == "QUARANTINED"
    assert inbox.transition("QUARANTINED", "EXCEPTION_RESOLVED") == "PROCESSED"
    with pytest.raises(S.IllegalTransition):
        inbox.transition("QUARANTINED", "APPLY_OK")


def test_a_dedupe_hit_is_a_success_and_not_a_duplicate():
    """Zoho documents no idempotency header (section 11.6). A send killed after
    despatch is recovered by cf_capex_ref lookup and UPDATE - and the outbox
    row must land in SENT, not in FAILED and not in a second PENDING."""
    outbox = S.integration_registry().outbox
    assert outbox.transition("PENDING", "DEDUPE_HIT") == "SENT"
    assert outbox.transition("PENDING", "SEND_OK") == "SENT"


def test_a_non_retryable_failure_does_not_go_round_the_backoff_loop():
    outbox = S.integration_registry().outbox
    assert outbox.transition("PENDING", "NON_RETRYABLE_FAILURE") == "DEAD"
    assert outbox.transition("PENDING", "RETRYABLE_FAILURE") == "FAILED"
    assert outbox.transition("FAILED", "BACKOFF_ELAPSED") == "PENDING"


def test_a_checkpointed_job_resumes_and_a_crashed_one_is_reclaimed():
    """The whole point of section 2.2: a 15-minute ceiling means long work must
    survive being cut in half, and a worker that dies must not strand the job."""
    job = S.integration_registry().job
    assert job.transition("CLAIMED", "SOFT_DEADLINE") == "CHECKPOINTED"
    assert job.transition("CHECKPOINTED", "RESUME") == "CLAIMED"
    assert job.transition("CLAIMED", "LEASE_EXPIRED") == "PENDING"
    assert job.transition("CHECKPOINTED", "MAX_RESUME_COUNT_EXCEEDED") == "DEAD"


def test_the_circuit_probes_before_it_closes():
    circuit = S.integration_registry().circuit
    assert circuit.transition("CIRCUIT_CLOSED", "DAILY_QUOTA_EXHAUSTED") == "CIRCUIT_OPEN"
    assert circuit.transition("CIRCUIT_OPEN", "PROBE_DUE") == "CIRCUIT_HALF_OPEN"
    assert circuit.transition("CIRCUIT_HALF_OPEN", "PROBE_FAILED") == "CIRCUIT_OPEN"
    assert circuit.transition("CIRCUIT_HALF_OPEN", "PROBE_OK") == "CIRCUIT_CLOSED"


def test_the_circuit_codes_do_not_collide_with_the_business_status_closed():
    """A closed circuit is healthy; a closed project is terminal and blocks
    posting. Only a shift key separated them before the prefix."""
    assert "CLOSED" not in S.integration_registry().circuit.codes
    assert "CLOSED" in S.business_statuses()


# ===========================================================================
# The integration_state badge - the fifth state, and not a 22nd status
# ===========================================================================
def test_an_object_with_no_outbox_row_carries_no_badge_at_all():
    """Not SENT, and not a fourth badge value. Nothing was ever queued."""
    assert S.integration_registry().integration_state_badge([]) is None


@pytest.mark.parametrize("state,expected", [
    ("PENDING", "QUEUED"),
    ("SENT", "SENT"),
    ("FAILED", "FAILED"),
    ("DEAD", "FAILED"),
])
def test_each_outbox_state_derives_the_badge_the_registry_declares(state, expected):
    assert S.integration_registry().integration_state_badge([state]) == expected


def test_a_failed_emission_outranks_a_queued_or_sent_one():
    """An object can carry an emission and a later amendment. The badge must
    never report success while something for the same object is broken - and it
    must not depend on which row the query happened to return first."""
    reg = S.integration_registry()
    assert reg.integration_state_badge(["SENT", "PENDING", "DEAD"]) == "FAILED"
    assert reg.integration_state_badge(["DEAD", "PENDING", "SENT"]) == "FAILED"
    assert reg.integration_state_badge(["SENT", "PENDING"]) == "QUEUED"
    assert reg.integration_state_badge(["SENT"]) == "SENT"


def test_the_badge_cannot_be_derived_from_a_state_that_is_not_an_outbox_state():
    with pytest.raises(S.IllegalTransition, match="not an outbox status"):
        S.integration_registry().integration_state_badge(["QUARANTINED"])


def test_no_badge_value_is_a_business_status():
    assert not set(S.integration_registry().badge_values) & S.business_statuses()


def test_the_business_screen_guard_rejects_every_operational_code():
    for code in sorted(S.integration_registry().all_codes()):
        with pytest.raises(ValueError, match="frozen C3 business statuses"):
            S.assert_business_screen_status(code)
    for code in S.integration_registry().badge_values:
        with pytest.raises(ValueError):
            S.assert_business_screen_status(code)
    assert S.assert_business_screen_status("RELEASED") == "RELEASED"


# ===========================================================================
# C17 - resolving a raw vendor value
# ===========================================================================
AS_OF = date(2026, 9, 6)


def test_a_documented_value_resolves_to_its_business_status():
    r = S.zoho_status_map().resolve("ERP", "purchase_order", "status", "open", as_of=AS_OF)
    assert r.kind == S.BUSINESS_STATUS
    assert r.business_status == "RELEASED"
    assert r.exception is None
    assert r.sets_business_status
    assert r.provisional is True, "ERP is PROVISIONAL until D-14 resolves"


def test_an_unknown_raw_value_is_accepted_and_raises_an_exception_rather_than_guessing():
    r = S.zoho_status_map().resolve(
        "ERP", "purchase_order", "status", "partially_received", as_of=AS_OF
    )
    assert r.kind == S.UNMAPPED
    assert r.business_status is None
    assert r.exception is not None
    assert r.exception.kind == S.UNMAPPED_EXTERNAL_STATUS
    assert r.exception.external_status_raw == "partially_received"
    assert r.exception.blocks_period_close and r.exception.blocks_capitalisation


def test_the_unmapped_result_is_a_value_not_an_exception_because_the_record_is_accepted():
    """Raising would tempt a caller into dropping the payload. The plan is
    explicit: accept the record, preserve the raw value, raise a
    reconciliation_exception - which is a row, not a stack unwind."""
    r = S.zoho_status_map().resolve("ERP", "bill", "status", "surprising", as_of=AS_OF)
    assert isinstance(r, S.MappingResult)
    assert isinstance(r.exception, S.ReconciliationException)


def test_a_raw_value_is_matched_verbatim_and_never_case_folded():
    """``external_status_raw`` is stored byte-for-byte, so it is matched
    byte-for-byte. A lenient match is a guess with better manners."""
    smap = S.zoho_status_map()
    assert smap.resolve("ERP", "purchase_order", "status", "open", as_of=AS_OF).is_mapped
    for variant in ("Open", "OPEN", " open", "open "):
        r = smap.resolve("ERP", "purchase_order", "status", variant, as_of=AS_OF)
        assert r.kind == S.UNMAPPED, variant
        assert r.exception.external_status_raw == variant


def test_a_row_that_exists_but_is_unsourced_is_treated_as_absent():
    """``pending_approval`` is in the file. Its spelling is not documented
    anywhere available offline, so it is inactive - and inactive must behave
    exactly like missing, or the distinction is decorative."""
    r = S.zoho_status_map().resolve(
        "ERP", "purchase_order", "status", "pending_approval", as_of=AS_OF
    )
    assert r.kind == S.UNMAPPED
    assert r.business_status is None
    assert "INACTIVE" in r.exception.reason
    assert S.UNVERIFIED_MARKER in r.exception.reason


def test_a_value_that_deliberately_means_nothing_is_mapped_and_sets_no_status():
    """An Inventory receive in transit is operational. This is a POSITIVE
    assertion - the result is mapped, carries no business status, and raises no
    exception. It must not look like an unmapped value."""
    r = S.zoho_status_map().resolve(
        "INVENTORY", "purchase_receive", "received_status", "in_transit", as_of=AS_OF
    )
    assert r.kind == S.NO_BUSINESS_STATUS
    assert r.is_mapped
    assert not r.sets_business_status
    assert r.business_status is None
    assert r.exception is None


def test_a_bill_status_drives_accounting_status_and_not_a_business_status():
    """AUD-C-004: accounting_status in {Approved, Reversal} is the
    accounting-effective set. void restores commitment; it is not CLOSED."""
    smap = S.zoho_status_map()
    void = smap.resolve("ERP", "bill", "status", "void", as_of=AS_OF)
    assert void.kind == S.ACCOUNTING_STATUS_ONLY
    assert void.accounting_status == "Void"
    assert void.business_status is None
    open_ = smap.resolve("ERP", "bill", "status", "open", as_of=AS_OF)
    assert open_.accounting_status == "Approved"
    assert open_.business_status is None


def test_an_empty_block_reports_not_yet_mapped_rather_than_no_meaning():
    """ERP Purchase Receives has no documented status field at all. The
    exception must say the value set is unknown - not imply the value was
    reviewed and found meaningless."""
    r = S.zoho_status_map().resolve(
        "ERP", "purchase_receive", "received_status", "received", as_of=AS_OF
    )
    assert r.kind == S.UNMAPPED
    assert S.INCOMPLETE_UNVERIFIED in r.exception.reason
    assert "not yet known" in r.exception.reason


def test_a_miss_against_a_complete_block_says_the_vendor_changed():
    """The two misses are operationally different: one is our gap, the other is
    news about Zoho. An operator triaging SCR-27 needs to be told which."""
    r = S.zoho_status_map().resolve("ERP", "bill", "status", "written_off", as_of=AS_OF)
    assert "vendor specification documents a complete value set" in r.exception.reason


def test_a_field_nobody_has_mapped_at_all_is_reported_as_such():
    r = S.zoho_status_map().resolve("ERP", "bill", "approval_status", "x", as_of=AS_OF)
    assert r.kind == S.UNMAPPED
    assert "No mapping block exists" in r.exception.reason


# ===========================================================================
# Product separation - a Books mapping is not evidence for ERP
# ===========================================================================
def test_the_same_raw_value_does_not_fall_back_across_products():
    """``open`` is documented for ERP and unsourced for Books. Resolving Books
    must NOT borrow the ERP row - that borrowing is the whole hazard."""
    smap = S.zoho_status_map()
    assert smap.resolve("ERP", "purchase_order", "status", "open", as_of=AS_OF).is_mapped
    books = smap.resolve("BOOKS", "purchase_order", "status", "open", as_of=AS_OF)
    assert books.kind == S.UNMAPPED
    assert books.product == "BOOKS"


def test_an_unknown_product_gets_no_mapping_rather_than_a_default_one():
    r = S.zoho_status_map().resolve("CRM", "purchase_order", "status", "open", as_of=AS_OF)
    assert r.kind == S.UNMAPPED


def test_the_inventory_row_the_plan_states_is_active_while_its_neighbours_are_not():
    smap = S.zoho_status_map()
    assert smap.resolve(
        "INVENTORY", "purchase_receive", "billed_status", "partially_billed", as_of=AS_OF
    ).business_status == "PARTIALLY_ACTUALISED"
    assert smap.resolve(
        "INVENTORY", "purchase_receive", "billed_status", "billed", as_of=AS_OF
    ).kind == S.UNMAPPED


@pytest.mark.parametrize("obj,expected", [
    ("purchase_receive", "INVENTORY"),
    ("bill", "BOOKS"),
    ("purchase_order", "BOOKS"),
])
def test_the_adapter_product_resolves_to_the_product_that_owns_the_object(obj, expected):
    assert S.contract_product("BOOKS_INVENTORY", obj) == expected
    assert S.contract_product("ERP", obj) == "ERP"


def test_an_object_with_no_declared_owner_refuses_to_guess_a_product():
    with pytest.raises(ValueError, match="product separation"):
        S.contract_product("BOOKS_INVENTORY", "sales_order")


def test_an_unknown_adapter_product_is_refused():
    with pytest.raises(ValueError, match="Unknown adapter product"):
        S.contract_product("NETSUITE", "bill")


# ===========================================================================
# Effective dating
# ===========================================================================
def test_a_mapping_is_not_applied_before_its_effective_date():
    """Mappings are versioned and effective-dated so that a correction does not
    retroactively rewrite what a past poll meant."""
    r = S.zoho_status_map().resolve(
        "ERP", "purchase_order", "status", "open", as_of=date(2026, 1, 1)
    )
    assert r.kind == S.UNMAPPED
    assert "not effective" in r.exception.reason


def test_a_mapping_is_applied_on_its_effective_date_itself():
    r = S.zoho_status_map().resolve(
        "ERP", "purchase_order", "status", "open", as_of=date(2026, 8, 28)
    )
    assert r.business_status == "RELEASED"


def test_resolving_without_a_date_uses_today_rather_than_ignoring_the_window():
    assert S.zoho_status_map().resolve(
        "ERP", "purchase_order", "status", "open"
    ).business_status == "RELEASED"


# ===========================================================================
# The loader refuses a self-contradicting registry
# ===========================================================================
def test_an_unreachable_state_is_rejected_when_the_registry_is_built():
    ns = S.StatusNamespace(
        name="inbox",
        description="x",
        initial_status="RECEIVED",
        statuses={
            "RECEIVED": S.IntegrationStatus("RECEIVED", "", False),
            "ORPHAN": S.IntegrationStatus("ORPHAN", "", True),
        },
        transitions=(
            S.Transition(None, "RECEIVED", "RECEIVE", "integration_store"),
            S.Transition("RECEIVED", "RECEIVED", "NOOP", "integration_store"),
        ),
    )
    assert "ORPHAN" not in ns.reachable()
    reg = S.IntegrationRegistry(
        version="0.0.0",
        namespaces={"inbox": ns},
        badge_values=("QUEUED",),
        badge_from_outbox_state={},
        badge_precedence=("QUEUED",),
        badge_source_namespace="outbox",
    )
    with pytest.raises(S.ContractError, match="cannot be reached"):
        S._validate_integration(reg, {"writers": {"integration_store": {}}})


def test_a_business_status_target_outside_c3_is_rejected_when_the_map_is_built():
    row = S.MappingRow(
        product="ERP", api_version="v3", object="purchase_order", field="status",
        raw="open", maps_to="INVENTED", accounting_status=None,
        mapping_kind=S.BUSINESS_STATUS, active=True,
        effective_from=date(2026, 8, 28), effective_to=None,
        evidence_class="DOCUMENTED_RAW_VALUE",
    )
    block = S.MappingBlock(
        product="ERP", api_version="v3", object="purchase_order", field="status",
        coverage=S.COMPLETE_PER_VENDOR_SPEC, provisional=True,
        effective_from=date(2026, 8, 28), effective_to=None, rows={"open": row},
    )
    with pytest.raises(S.ContractError, match="not a C3 status"):
        S._validate_map(S.ZohoStatusMap(version="0.0.0", blocks={
            ("ERP", "purchase_order", "status"): block,
        }))


def test_an_active_row_on_unverified_evidence_is_rejected_when_the_map_is_built():
    row = S.MappingRow(
        product="BOOKS", api_version="v3", object="bill", field="status",
        raw="open", maps_to=None, accounting_status="Approved",
        mapping_kind=S.ACCOUNTING_STATUS_ONLY, active=True,
        effective_from=date(2026, 8, 28), effective_to=None,
        evidence_class="UNVERIFIED",
    )
    block = S.MappingBlock(
        product="BOOKS", api_version="v3", object="bill", field="status",
        coverage=S.INCOMPLETE_UNVERIFIED, provisional=False,
        effective_from=date(2026, 8, 28), effective_to=None, rows={"open": row},
    )
    with pytest.raises(S.ContractError, match="active row with evidence class"):
        S._validate_map(S.ZohoStatusMap(version="0.0.0", blocks={
            ("BOOKS", "bill", "status"): block,
        }))
