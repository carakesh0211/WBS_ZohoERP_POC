"""The chaos test: a Function killed mid-send never duplicates a purchase order.

Wave 5, stream 6. §11.6 and §18.5 row Z-01.

**Zoho documents no idempotency header.** A Catalyst Function has no resident
process, no in-memory retry state and a hard soft-deadline; it starts, sends,
and may be gone before the response arrives. Between "request sent" and
"response recorded" there is a window in which we do not know whether a
purchase order exists. A purchase order is a **commitment**, so a duplicate is
not a cosmetic defect -- it is money the company believes it owes twice.

The guarantee is synthesised from three parts, and this file's structure is a
direct statement of that:

* :func:`test_a_function_killed_at_every_point_between_send_and_record_makes_one_po`
  enumerates every step in the emission and kills there.
* :func:`test_one_hundred_kills_produce_zero_duplicate_purchase_orders`
  is the test named in the brief: 100 randomised kill schedules, zero
  duplicates.
* the ``NEGATIVE CONTROL`` tests each remove one of the three parts and assert
  the duplicate appears. A safety property with no negative control is a
  property nobody has actually measured.

What is fake and what is under test
-----------------------------------
``tests/outbound_tenant_fake.py`` holds Zoho (:class:`FakeTenant`, which owns
the Z-01 unique index), the C1 adapter and the outbox. Those are not under
test. ``outbound.emit_purchase_order`` is, and it contributes exactly three
things: a key that does not change between attempts, a claim written before the
network call, and a resolve before a create.

No network, no tenant, no Catalyst. Everything here is a dict.
"""
from __future__ import annotations

import random
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backend.integration import outbound as ob          # noqa: E402
from outbound_tenant_fake import (                          # noqa: E402
    CONNECTION_ID, MODULE, NOW, C1OnlyAdapter, FakeAdapter, FakeTenant,
    FunctionKilled, InMemoryOutboxStore, KillSwitch, enqueued,
)

#: How many recovery attempts a resumed job gets before the test gives up. Far
#: above what the design needs (one), so a failure here means "never recovered",
#: not "needed a few more goes".
RECOVERY_ATTEMPTS = 6


#: Far beyond SENDING_LEASE_SECONDS, so a resumed tick may reclaim a row its
#: dead predecessor left in SENDING.
_LEASE_EXPIRED = timedelta(seconds=ob.SENDING_LEASE_SECONDS + 60)


def _drive(*, kill_at, tenant, adapter_cls=FakeAdapter, names_duplicate=True,
           outbox_id="OB-1", local_id="PR-0001"):
    """One emission, killed at ``kill_at``, then recovered by later cron ticks.

    Models a Catalyst Function dying and a later tick picking the row back up.
    The store and the tenant are constructed once and shared across the
    "processes", because that is what durable storage means; each tick gets a
    fresh adapter and a fresh, disarmed kill switch, because a new invocation is
    a new process with no memory of the last one.

    Returns ``(dedupe_key, store, tenant, adapter_calls)``.
    """
    kill = KillSwitch(kill_at=kill_at)
    store = InMemoryOutboxStore(kill=kill)
    adapter = adapter_cls(tenant=tenant, kill=kill,
                          names_duplicate=names_duplicate)
    calls: list = []

    enqueued(store, outbox_id=outbox_id, local_id=local_id)
    key = store.get(outbox_id).dedupe_key

    try:
        ob.emit_purchase_order(adapter=adapter, store=store,
                               outbox_id=outbox_id, now=NOW)
    except FunctionKilled:
        pass
    calls.extend(adapter.calls)

    for _ in range(RECOVERY_ATTEMPTS):
        row = store.get(outbox_id)
        if row is not None and row.state == ob.OUTBOX_SENT:
            break
        store.kill = KillSwitch(kill_at=None)
        resumed = adapter_cls(tenant=tenant, kill=store.kill,
                              names_duplicate=names_duplicate)
        try:
            ob.emit_purchase_order(adapter=resumed, store=store,
                                   outbox_id=outbox_id,
                                   now=NOW + _LEASE_EXPIRED)
        except (ob.DuplicateDedupeKey, ob.OutboundError, FunctionKilled):
            pass
        calls.extend(resumed.calls)

    return key, store, tenant, calls


def _enumerate_steps(adapter_cls=FakeAdapter) -> list[str]:
    """Every named step one clean emission passes through, for this adapter.

    Enumerated by running a real emission rather than written out by hand, so a
    new durable step added to ``emit_purchase_order`` is killed at
    automatically. A hand-written list is a list that goes stale exactly when a
    new step introduces a new window.
    """
    kill = KillSwitch(kill_at=None)
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore(kill=kill)
    adapter = adapter_cls(tenant=tenant, kill=kill)
    enqueued(store)
    ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                           now=NOW)
    return list(kill.steps)


STEPS = _enumerate_steps()
C1_ONLY_STEPS = _enumerate_steps(C1OnlyAdapter)


def _kill_at(step_name: str, adapter_cls=FakeAdapter) -> int:
    """The 1-based kill index of a NAMED step, for a given adapter.

    Named, never a literal index: :class:`C1OnlyAdapter` has no resolve step,
    so the two adapters number their steps differently and an index borrowed
    from one lands somewhere else entirely in the other. That is not a
    hypothetical -- it is the bug this helper was written to fix, and it made a
    negative control pass for the wrong reason.
    """
    steps = STEPS if adapter_cls is FakeAdapter else C1_ONLY_STEPS
    return steps.index(step_name) + 1


#: The kill that matters: the tenant has created the purchase order and the
#: response has not reached us. Everything in this file exists for this point.
LOST_RESPONSE = "adapter.create.response_received"


# ======================================================================
# The positive case
# ======================================================================

def test_the_emission_passes_through_the_window_the_whole_design_is_about():
    """A guard on the guard.

    If ``adapter.create.request_sent`` and ``adapter.create.response_received``
    ever stop being distinct steps, every kill test below would still pass and
    would be testing nothing: there would be no window to be killed in. The
    chaos test is only evidence while this holds.
    """
    for steps in (STEPS, C1_ONLY_STEPS):
        assert "adapter.create.request_sent" in steps
        assert LOST_RESPONSE in steps
        assert "store.record_sent.after" in steps
        assert steps.index("adapter.create.request_sent") < steps.index(
            LOST_RESPONSE)
        assert steps.index(LOST_RESPONSE) < steps.index("store.record_sent.after"), (
            "The external id must be recorded AFTER the response, or there is "
            "no lost-response window and this suite proves nothing.")

    # The claim must be durable BEFORE anything is sent: that is the
    # write-ahead the whole recovery depends on.
    assert STEPS.index("store.claim.after") < STEPS.index(
        "adapter.create.request_sent")
    # And the resolve must precede the create, or the lost-response case has
    # no chance to be discovered before a second record is made.
    assert STEPS.index("adapter.resolve.after") < STEPS.index(
        "adapter.create.request_sent")


def test_the_worst_kill_leaves_a_purchase_order_we_can_still_find():
    """The lost response, on its own, spelled out.

    The tenant holds the purchase order. We hold an outbox row that says
    ``SENDING`` and an ``external_id`` of ``None``. There is no request we can
    replay and no id we can look up. The only thing connecting the two is the
    ``cf_capex_ref`` we wrote before we sent -- which is why the key is written
    ahead of the call and not derived from the response.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    kill = KillSwitch(kill_at=_kill_at(LOST_RESPONSE))
    store = InMemoryOutboxStore(kill=kill)
    adapter = FakeAdapter(tenant=tenant, kill=kill)
    enqueued(store)
    key = store.get("OB-1").dedupe_key

    with pytest.raises(FunctionKilled):
        ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                               now=NOW)

    orphaned = store.get("OB-1")
    assert orphaned.state == ob.OUTBOX_SENDING
    assert orphaned.external_id is None
    assert len(tenant.records) == 1, "The purchase order exists in the tenant."
    assert tenant.find_by_capex_ref(key) is not None, (
        "and the only handle we have on it is the key we wrote before sending.")

    recovered = ob.emit_purchase_order(
        adapter=FakeAdapter(tenant=tenant), store=store, outbox_id="OB-1",
        now=NOW + _LEASE_EXPIRED)

    assert recovered.created is False and recovered.adopted is True
    assert recovered.external_id == tenant.find_by_capex_ref(key)
    assert len(tenant.records) == 1


@pytest.mark.parametrize("kill_at", range(1, len(STEPS) + 1),
                         ids=[f"{i + 1}-{s}" for i, s in enumerate(STEPS)])
def test_a_function_killed_at_every_point_between_send_and_record_makes_one_po(kill_at):
    """Kill at each step in turn. Exactly one purchase order, every time.

    The step names in the ids are the point of this test: a failure says which
    ordering produced the duplicate, not merely that one appeared.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    key, store, tenant, _ = _drive(kill_at=kill_at, tenant=tenant)

    assert tenant.count_with_capex_ref(key) == 1, (
        f"Killing at step {kill_at} ({STEPS[kill_at - 1]}) produced "
        f"{tenant.count_with_capex_ref(key)} purchase orders carrying "
        f"{ob.CF_CAPEX_REF}={key!r}. A duplicate purchase order is a duplicate "
        "commitment.")
    assert len(tenant.records) == 1
    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_SENT
    assert row.external_id in tenant.records


def test_one_hundred_kills_produce_zero_duplicate_purchase_orders():
    """**The test named in the brief.** 100 kills, zero duplicates.

    Randomised rather than enumerated, and with a *seeded* generator so a
    failure is reproducible. Each iteration kills at a random step of a random
    emission; the recovery ticks then run to completion and the tenant is
    counted.

    The assertion is on the tenant, not on our own bookkeeping. Our outbox
    saying "SENT once" would be worthless evidence: the question is how many
    purchase orders the ERP holds.
    """
    rng = random.Random(20260906)
    duplicates: list[tuple[int, int, str, int]] = []

    for iteration in range(100):
        kill_at = rng.randint(1, len(STEPS))
        local_id = f"PR-{iteration:04d}"
        tenant = FakeTenant(unique_capex_ref=True)
        key, store, tenant, _ = _drive(kill_at=kill_at, tenant=tenant,
                                       outbox_id=f"OB-{iteration}",
                                       local_id=local_id)
        count = tenant.count_with_capex_ref(key)
        if count != 1:
            duplicates.append((iteration, kill_at, STEPS[kill_at - 1], count))

    assert not duplicates, (
        "A Function killed mid-send duplicated a purchase order. "
        "(iteration, kill_at, step, purchase_orders): " + repr(duplicates))


def test_a_row_already_sent_is_never_sent_again():
    """The simplest duplicate: a job re-run over settled work.

    A cron tick that re-reads its chunk after a partial failure will see rows
    it already sent. ``emit`` must report the settled result and make no call
    at all -- not "make the call and rely on the tenant to refuse it", which
    burns outbound budget every tick for the life of the row.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    adapter = FakeAdapter(tenant=tenant)
    enqueued(store)

    first = ob.emit_purchase_order(adapter=adapter, store=store,
                                   outbox_id="OB-1", now=NOW)
    calls_after_first = len(adapter.calls)
    second = ob.emit_purchase_order(adapter=adapter, store=store,
                                    outbox_id="OB-1", now=NOW)

    assert second.external_id == first.external_id
    assert second.created is False
    assert len(adapter.calls) == calls_after_first, (
        "A settled outbox row must cost zero API calls to re-observe.")
    assert len(tenant.records) == 1


def test_a_live_lease_stops_a_second_worker_claiming_an_in_flight_row():
    """Two workers, one row: the second must not send.

    §2.2 gives a job a 12-minute soft deadline, so a claim leases the row for
    longer than that. Without the lease the second worker resolves the key
    before the first worker's request lands, finds nothing, and creates a
    second purchase order -- see the negative control below, which is exactly
    that scenario with Z-01 removed as well.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    enqueued(store)

    claimed = store.claim("OB-1", now=NOW)
    assert claimed is not None

    adapter = FakeAdapter(tenant=tenant)
    with pytest.raises(ob.OutboundError):
        ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                               now=NOW + timedelta(seconds=30))
    assert adapter.calls == []
    assert tenant.records == {}


# ======================================================================
# NEGATIVE CONTROLS -- remove one part, watch the duplicate appear
# ======================================================================

def test_negative_control_without_z01_the_unique_field_a_kill_duplicates_the_po():
    """**Z-01 is load-bearing.** Remove the unique field and duplicates appear.

    §18.5 assigns Z-01 to this project rather than to the client "because the
    idempotency guarantee ... is a correctness property, not a configuration
    preference". This is that claim, measured.

    The adapter here is :class:`C1OnlyAdapter` -- exactly the frozen C1 surface,
    no ``resolve_by_dedupe_key``. That combination is the honest worst case: the
    contract as frozen, in a tenant where nobody ticked Unique. The purchase
    order is created twice and the company owes the money twice.
    """
    tenant = FakeTenant(unique_capex_ref=False)
    key, store, tenant, _ = _drive(
        kill_at=_kill_at(LOST_RESPONSE, C1OnlyAdapter), tenant=tenant,
        adapter_cls=C1OnlyAdapter)

    assert tenant.count_with_capex_ref(key) == 2, (
        "Expected the duplicate this control exists to demonstrate. If this "
        "now reports 1, the tenant fake has stopped modelling a tenant without "
        "Z-01 and the positive chaos test above has lost its meaning.")
    assert len(tenant.records) == 2


def test_negative_control_with_z01_present_the_same_kill_makes_one_po():
    """The paired positive: the *only* difference is the unique index.

    Same adapter (frozen C1, no resolve), same kill point, same everything --
    except the tenant has ``cf_capex_ref`` marked Unique. One purchase order.
    That isolation is what makes the previous test evidence for Z-01 rather
    than evidence for something else in the arrangement.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    key, store, tenant, _ = _drive(
        kill_at=_kill_at(LOST_RESPONSE, C1OnlyAdapter), tenant=tenant,
        adapter_cls=C1OnlyAdapter)

    assert tenant.count_with_capex_ref(key) == 1
    assert len(tenant.records) == 1
    assert store.get("OB-1").state == ob.OUTBOX_SENT
    assert store.get("OB-1").external_id in tenant.records, (
        "Recovery must adopt the id of the purchase order that already exists, "
        "not record an id for one it never created.")


def test_negative_control_a_generated_dedupe_key_duplicates_the_po():
    """**Determinism is load-bearing.** A fresh key per attempt defeats Z-01.

    A uuid4 minted at send time is the most natural-looking way to write this
    code and it is silently wrong: the retry writes a ``cf_capex_ref`` the
    tenant has never seen, the unique index has nothing to refuse, and a second
    purchase order is created. Z-01 cannot save a key that changes.

    Driven against the adapter directly rather than through
    ``emit_purchase_order``, because ``emit`` refuses a key that does not match
    the current derivation -- see
    :func:`test_a_dedupe_key_that_no_longer_derives_the_same_way_is_quarantined`.
    That refusal is a *second* layer over the same defect, and this control
    measures what the first layer is for.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    payload = {"local_id": "PR-0001", "state": ob.PO_STATE_DRAFT}
    adapter = C1OnlyAdapter(tenant=tenant)

    # Attempt one: sent, response lost. Attempt two: a freshly minted key.
    adapter.create_purchase_order(payload, f"CAPEX-{uuid.uuid4()}")
    adapter.create_purchase_order(payload, f"CAPEX-{uuid.uuid4()}")

    assert len(tenant.records) == 2, (
        "Expected two purchase orders: a per-attempt key defeats the unique "
        "field entirely, because the index has nothing to match against.")

    # The same two attempts with the deterministic key: the second is refused.
    stable = FakeTenant(unique_capex_ref=True)
    steady = C1OnlyAdapter(tenant=stable)
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    steady.create_purchase_order(payload, key)
    with pytest.raises(ob.DuplicateDedupeKey):
        steady.create_purchase_order(payload, key)
    assert len(stable.records) == 1


def test_the_dedupe_key_is_the_same_in_every_process_that_derives_it():
    """Determinism, stated directly rather than only inferred from the kills.

    The recovery tick is a different process from the one that died. It has no
    memory, no shared state and no way to ask the dead process anything: the
    only reason it writes the same ``cf_capex_ref`` is that the derivation is a
    pure function of identifiers both processes can read from the outbox row.
    """
    first = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    again = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    assert first == again

    # A subprocess -- a genuinely different interpreter, and therefore a
    # different PYTHONHASHSEED. A derivation accidentally built on `hash()`
    # would pass the two calls above and fail here.
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "from app.backend.integration.outbound import derive_dedupe_key as d;"
         f"print(d({CONNECTION_ID!r}, {MODULE!r}, 'PR-0001'))"],
        capture_output=True, text=True, check=True,
        cwd=str(Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == first

    # Different rows must not collide, or one purchase order would suppress
    # another that is genuinely different.
    assert ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0002") != first
    assert ob.derive_dedupe_key("CONN-OTHER", MODULE, "PR-0001") != first


def test_negative_control_a_read_before_write_does_not_replace_the_unique_index():
    """Why Z-01 is a *constraint* and not a lookup.

    ``resolve_by_dedupe_key`` covers the single-threaded lost-response case
    perfectly well. It does not cover two workers, and two workers is not
    exotic -- it is a lease expiring while a request is still in flight, which
    §2.2's 12-minute soft deadline makes a routine event on a slow tenant.

    Both workers resolve, both find nothing (the first request has not landed
    yet), both create. With Z-01 the second create is refused and the worker
    adopts the first record. Without it, two purchase orders. A read-then-write
    is not an idempotency mechanism; a unique index is.
    """
    for unique, expected in ((False, 2), (True, 1)):
        tenant = FakeTenant(unique_capex_ref=unique)
        store = InMemoryOutboxStore()
        enqueued(store)
        key = store.get("OB-1").dedupe_key
        payload = store.get("OB-1").payload

        worker_a = FakeAdapter(tenant=tenant)
        worker_b = FakeAdapter(tenant=tenant)

        # Both workers resolve before either create lands.
        assert worker_a.resolve_by_dedupe_key(key) is None
        assert worker_b.resolve_by_dedupe_key(key) is None

        worker_a.create_purchase_order(payload, key)
        if unique:
            with pytest.raises(ob.DuplicateDedupeKey) as refused:
                worker_b.create_purchase_order(payload, key)
            assert refused.value.external_id in tenant.records
        else:
            worker_b.create_purchase_order(payload, key)

        assert tenant.count_with_capex_ref(key) == expected, (
            f"unique_capex_ref={unique} produced "
            f"{tenant.count_with_capex_ref(key)} purchase orders, expected "
            f"{expected}.")


def test_a_duplicate_the_tenant_will_not_name_is_not_recorded_as_a_failure():
    """The subtle one: refused, but the tenant would not say which record.

    Zoho's unique-field error does not reliably carry the colliding record's
    id. Treating that as "the send failed" would be the worst possible reading
    -- the purchase order demonstrably exists -- and a later attempt made on
    that reading is how a duplicate gets created by the error handler rather
    than by the network.

    So the attempt is recorded as retryable, the row stays claimable, and the
    next tick resolves the key explicitly. One purchase order.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    enqueued(store)
    key = store.get("OB-1").dedupe_key

    # A purchase order already exists for this key: the lost-response case.
    tenant.create({ob.CF_CAPEX_REF: key, "state": ob.PO_STATE_DRAFT})

    blind = C1OnlyAdapter(tenant=tenant, names_duplicate=False)
    with pytest.raises(ob.DuplicateDedupeKey):
        ob.emit_purchase_order(adapter=blind, store=store, outbox_id="OB-1",
                               now=NOW)

    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_RETRY, (
        "A refused duplicate must leave the row retryable, not DEAD: the "
        "purchase order exists and its id still has to be adopted.")
    assert "NOT re-creating" in (row.last_error or "")

    seeing = FakeAdapter(tenant=tenant)
    result = ob.emit_purchase_order(
        adapter=seeing, store=store, outbox_id="OB-1",
        now=NOW + _LEASE_EXPIRED)

    assert result.adopted is True and result.created is False
    assert tenant.count_with_capex_ref(key) == 1


def test_a_dedupe_key_that_no_longer_derives_the_same_way_is_quarantined():
    """A derivation change under a live row is a duplicate waiting to happen.

    If :func:`derive_dedupe_key` is ever edited, every outbox row enqueued
    before the change carries a key the new code would not produce. Sending
    such a row writes a ``cf_capex_ref`` the tenant has never seen -- the
    generated-key defect, arriving by refactor instead of by design. The row is
    quarantined for a human instead.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    enqueued(store)
    store.rows["OB-1"].dedupe_key = "CAPEX-from-an-older-derivation-0000"

    adapter = FakeAdapter(tenant=tenant)
    with pytest.raises(ob.OutboundError, match=ob.KIND_DEDUPE_KEY_COLLISION):
        ob.emit_purchase_order(adapter=adapter, store=store, outbox_id="OB-1",
                               now=NOW)

    assert adapter.calls == []
    assert tenant.records == {}
    assert store.get("OB-1").state == ob.OUTBOX_DEAD


def test_a_frozen_c1_adapter_cannot_recover_an_unnamed_duplicate_and_says_so():
    """**A finding, not a passing test dressed up as one.**

    Put together the two conditions the frozen contract permits -- an adapter
    implementing only C1 (no ``resolve_by_dedupe_key``) and a tenant whose
    unique-field error does not name the record it already holds -- and the
    lost-response case becomes **unrecoverable**. The purchase order exists.
    Its ``cf_capex_ref`` is the only handle on it. Frozen C1 has no call that
    reads a record by custom field, so there is no way to learn its id.

    This is the gap in C1 reported to stream 1, measured rather than asserted.
    What this module can do about it is refuse to lie: the row goes DEAD after
    the documented eight attempts rather than retrying for ever against a 2,000
    call/day ceiling, its error says the purchase order EXISTS, and
    ``orphaned_emission_finding`` turns it into an exception that blocks period
    close. **No duplicate is created** -- Z-01 still holds -- but the
    commitment is unlinked, and that is stated loudly instead of looking like
    an unsent row on SCR-39.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    enqueued(store)
    key = store.get("OB-1").dedupe_key
    tenant.create({ob.CF_CAPEX_REF: key, "state": ob.PO_STATE_DRAFT})

    for _ in range(ob.MAX_ATTEMPTS):
        with pytest.raises(ob.DuplicateDedupeKey):
            ob.emit_purchase_order(
                adapter=C1OnlyAdapter(tenant=tenant, names_duplicate=False),
                store=store, outbox_id="OB-1", now=NOW + _LEASE_EXPIRED)

    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_DEAD
    assert row.external_id is None
    assert "ORPHANED" in row.last_error
    assert "EXISTS" in row.last_error, (
        "DEAD must not read as 'never sent'; the purchase order is in the "
        "tenant.")

    # Exactly one purchase order: Z-01 held throughout. The defect is that we
    # cannot name it, not that we made another.
    assert tenant.count_with_capex_ref(key) == 1

    finding = ob.orphaned_emission_finding(row, entity_id="ENT-1")
    assert finding is not None
    assert finding.kind == ob.KIND_ORPHANED_EMISSION
    assert finding.blocks_period_close is True
    assert finding.entity_id == "ENT-1"
    assert "EXISTS" in finding.detail


def test_the_same_row_recovers_the_moment_the_adapter_can_resolve_by_key():
    """The gap is in the contract, not in the tenant. One call closes it.

    Same tenant, same refused duplicate, same orphaned row -- an adapter with
    ``resolve_by_dedupe_key`` adopts the existing purchase order on the next
    tick. That single call is what stream 1 is being asked for.
    """
    tenant = FakeTenant(unique_capex_ref=True)
    store = InMemoryOutboxStore()
    enqueued(store)
    key = store.get("OB-1").dedupe_key
    external_id = tenant.create({ob.CF_CAPEX_REF: key,
                                 "state": ob.PO_STATE_DRAFT})

    result = ob.emit_purchase_order(adapter=FakeAdapter(tenant=tenant),
                                    store=store, outbox_id="OB-1", now=NOW)

    assert result.adopted is True and result.created is False
    assert result.external_id == external_id
    assert store.get("OB-1").state == ob.OUTBOX_SENT
    assert tenant.count_with_capex_ref(key) == 1
    assert ob.orphaned_emission_finding(store.get("OB-1"),
                                        entity_id="ENT-1") is None


def test_a_dead_row_that_never_reached_the_tenant_is_not_called_an_orphan():
    """The distinction the finding exists to make, from the other side.

    A row that died on eight timeouts sent nothing. Raising an ORPHANED
    exception for it would put a commitment on the books that does not exist --
    the mirror-image error, and just as wrong.
    """
    tenant = FakeTenant()
    store = InMemoryOutboxStore()
    enqueued(store)

    class _Broken(C1OnlyAdapter):
        def create_purchase_order(self, payload, dedupe_key):
            raise TimeoutError("gateway timeout")

    for _ in range(ob.MAX_ATTEMPTS):
        with pytest.raises(TimeoutError):
            ob.emit_purchase_order(adapter=_Broken(tenant=tenant), store=store,
                                   outbox_id="OB-1", now=NOW + _LEASE_EXPIRED)

    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_DEAD
    assert tenant.records == {}
    assert ob.orphaned_emission_finding(row, entity_id="ENT-1") is None
