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
things: a key that does not change between attempts, a claim held exclusively
across the network call, and a resolve before EVERY create.

The last of those used to read "a resolve before a create", with an in-flight
``SENDING`` state deciding when a resolve was warranted. It is unconditional
now, and the state is gone -- the table's CHECK never permitted it. Nothing in
this file got weaker for that: the same 100 seeded kill schedules produce the
same zero duplicates, and every negative control still produces its duplicate.

And two tests were added that this file did not have, at the bottom, against
the SHIPPED adapters:
:func:`test_the_first_emission_through_a_shipped_adapter_creates_the_po` and
its mutation control. Everything else here proves a *retry* is safe. Those two
prove the first attempt works at all, which -- until this stream -- it did not.

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
    FunctionKilled, InMemoryOutboxStore, KillSwitch, emission_document,
    enqueued,
)

#: How many recovery attempts a resumed job gets before the test gives up. Far
#: above what the design needs (one), so a failure here means "never recovered",
#: not "needed a few more goes".
RECOVERY_ATTEMPTS = 6


#: The gap between one simulated cron tick and the next. It clears §11.6's
#: backoff ceiling, so a row that scheduled a retry is always due by the next
#: tick and a recovery failure means "never recovered" rather than "not yet".
#:
#: It used to have to clear a 900-second SENDING **lease** as well -- an
#: additional wait, on top of the backoff, imposed by a claim whose owner was
#: already dead. That is gone: exclusion is a row lock held by the claiming
#: transaction, and a dead Function drops it with its connection
#: (``InMemoryOutboxStore.crash``). A crashed row is now claimable on the very
#: next tick, and the only thing that can delay it is the backoff we chose.
_NEXT_TICK = timedelta(seconds=ob.BACKOFF_CEILING_SECONDS + 60)


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
        # The process is gone. PostgreSQL drops the row lock with the
        # connection, which is the whole reason the recovery below needs no
        # lease to expire first.
        store.crash()
    calls.extend(adapter.calls)

    for tick in range(1, RECOVERY_ATTEMPTS + 1):
        row = store.get(outbox_id)
        if row is not None and row.state == ob.OUTBOX_SENT:
            break
        store.kill = KillSwitch(kill_at=None)
        resumed = adapter_cls(tenant=tenant, kill=store.kill,
                              names_duplicate=names_duplicate)
        try:
            ob.emit_purchase_order(adapter=resumed, store=store,
                                   outbox_id=outbox_id,
                                   now=NOW + _NEXT_TICK * tick)
        except (ob.DuplicateDedupeKey, ob.OutboundError, FunctionKilled):
            store.crash()
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

    # The row must be claimed -- held exclusively -- before anything is sent,
    # or two workers send the same document at once.
    assert STEPS.index("store.claim.after") < STEPS.index(
        "adapter.create.request_sent")
    # And the resolve must precede the create, or the lost-response case has
    # no chance to be discovered before a second record is made. THIS is the
    # ordering the recovery actually rests on. It used to share the load with a
    # write-ahead to SENDING; now it carries it alone, which is what let the
    # invented state go.
    assert STEPS.index("adapter.resolve.after") < STEPS.index(
        "adapter.create.request_sent")


def test_the_worst_kill_leaves_a_purchase_order_we_can_still_find():
    """The lost response, on its own, spelled out.

    The tenant holds the purchase order. We hold an outbox row that is
    ``PENDING`` with an ``external_id`` of ``None`` -- **byte for byte
    indistinguishable from a row that was never sent at all**. There is no
    request we can replay and no id we can look up.

    That indistinguishability is the whole reason this test is worth reading
    twice. It used to be avoided by writing ``SENDING`` before the call, so the
    row itself remembered that a despatch might have happened. ``SENDING`` is
    not a state the table permits, so that memory never actually survived to
    production -- and it turns out not to be needed, because the recovery does
    not consult the row's state to decide whether to resolve. It resolves
    every time. The only thing connecting the outbox row to the document is the
    ``cf_capex_ref`` we wrote before we sent, which is why the key is derived
    ahead of the call and never from the response.
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
    store.crash()

    orphaned = store.get("OB-1")
    assert orphaned.state == ob.OUTBOX_PENDING, (
        "A killed emission leaves the row in a state the table permits. The "
        "row cannot tell us a send happened -- the tenant can, and does.")
    assert orphaned.external_id is None
    assert len(tenant.records) == 1, "The purchase order exists in the tenant."
    assert tenant.find_by_capex_ref(key) is not None, (
        "and the only handle we have on it is the key we wrote before sending.")

    recovered = ob.emit_purchase_order(
        adapter=FakeAdapter(tenant=tenant), store=store, outbox_id="OB-1",
        now=NOW + _NEXT_TICK)

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
    document = emission_document("PR-0001")
    adapter = C1OnlyAdapter(tenant=tenant)

    # Attempt one: sent, response lost. Attempt two: a freshly minted key.
    # The DOCUMENT is identical both times -- only the key varies, which is
    # what isolates this control to the key's determinism.
    adapter.create_purchase_order(document, f"CAPEX-{uuid.uuid4()}")
    adapter.create_purchase_order(document, f"CAPEX-{uuid.uuid4()}")

    assert len(tenant.records) == 2, (
        "Expected two purchase orders: a per-attempt key defeats the unique "
        "field entirely, because the index has nothing to match against.")

    # The same two attempts with the deterministic key: the second is refused.
    stable = FakeTenant(unique_capex_ref=True)
    steady = C1OnlyAdapter(tenant=stable)
    key = ob.derive_dedupe_key(CONNECTION_ID, MODULE, "PR-0001")
    steady.create_purchase_order(document, key)
    with pytest.raises(ob.DuplicateDedupeKey):
        steady.create_purchase_order(document, key)
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
    exotic. **It is more reachable now, not less.**

    Under the old lease, a Function killed mid-request kept the row locked for
    the remaining 900 seconds, so a second worker could not touch it until long
    after the first request had either landed or timed out. Under the row lock
    the dead Function's lock dies with its connection, so the next one-minute
    cron tick may claim the row **while the first worker's request is still on
    its way to the tenant**. That is a real cost of dropping the lease, it is
    stated here rather than buried, and it is affordable for exactly one
    reason: Z-01.

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
        # Through the production mapper, so this control cannot pass on a
        # shape `emit_purchase_order` would never produce.
        document = ob.emission_dto_from_record(store.get("OB-1"))

        worker_a = FakeAdapter(tenant=tenant)
        worker_b = FakeAdapter(tenant=tenant)

        # Both workers resolve before either create lands.
        assert worker_a.resolve_by_dedupe_key(key) is None
        assert worker_b.resolve_by_dedupe_key(key) is None

        worker_a.create_purchase_order(document, key)
        if unique:
            with pytest.raises(ob.DuplicateDedupeKey) as refused:
                worker_b.create_purchase_order(document, key)
            assert refused.value.external_id in tenant.records
        else:
            worker_b.create_purchase_order(document, key)

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
    assert row.state == ob.OUTBOX_FAILED, (
        "A refused duplicate must leave the row retryable, not DEAD: the "
        "purchase order exists and its id still has to be adopted.")
    assert "NOT re-creating" in (row.last_error or "")

    seeing = FakeAdapter(tenant=tenant)
    result = ob.emit_purchase_order(
        adapter=seeing, store=store, outbox_id="OB-1",
        now=NOW + _NEXT_TICK)

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

    # Each attempt is a LATER tick. It has to be: a FAILED row carries
    # `next_attempt_at`, and the claim now honours that backoff rather than
    # ignoring it the way the lease-based one did. Re-running eight attempts at
    # a single instant would silently stop claiming after the first.
    for attempt in range(1, ob.MAX_ATTEMPTS + 1):
        with pytest.raises(ob.DuplicateDedupeKey):
            ob.emit_purchase_order(
                adapter=C1OnlyAdapter(tenant=tenant, names_duplicate=False),
                store=store, outbox_id="OB-1", now=NOW + _NEXT_TICK * attempt)

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
        def create_purchase_order(self, po, dedupe_key):
            raise TimeoutError("gateway timeout")

    for attempt in range(1, ob.MAX_ATTEMPTS + 1):
        with pytest.raises(TimeoutError):
            ob.emit_purchase_order(adapter=_Broken(tenant=tenant), store=store,
                                   outbox_id="OB-1",
                                   now=NOW + _NEXT_TICK * attempt)

    row = store.get("OB-1")
    assert row.state == ob.OUTBOX_DEAD
    assert tenant.records == {}
    assert ob.orphaned_emission_finding(row, entity_id="ENT-1") is None

# ==========================================================================
# THE REAL ADAPTERS, not the fake
#
# Everything above proves `emit_purchase_order`. It proves it against
# `FakeAdapter`, which was written alongside these tests and has always had
# `resolve_by_dedupe_key` -- so it proved the *design* while the two shipped
# adapters still could not do it. `ErpAdapter` and `BooksInventoryAdapter`
# implemented frozen C1, and frozen C1 had no call that read a purchase order
# back by its dedupe key.
#
# So the tests below drive the SHIPPED adapters, over the real
# `Transport` seam, against a tenant that loses the response exactly where
# §11.6 says it can be lost. The transport is a dict; no network, no tenant,
# no Catalyst. What is under test is the adapter code that will ship.
# ==========================================================================

from datetime import date                                   # noqa: E402

from app.backend.integration.adapter import (                # noqa: E402
    DEDUPE_CUSTOM_FIELD,
    RecoverableProcurementAdapter,
)
from app.backend.integration.books_inventory import (        # noqa: E402
    BooksInventoryAdapter,
)
from app.backend.integration.dto import (                     # noqa: E402
    LineDTO, PurchaseOrderDTO, SourceRef,
)
from app.backend.integration.erp import ErpAdapter            # noqa: E402

#: (product, adapter factory, service base) for the parametrised recovery tests.
REAL_ADAPTERS = ["ERP", "BOOKS_INVENTORY"]


class ResponseLost(Exception):
    """The Function died between "request sent" and "response received".

    Not a transport error the adapter could classify and retry -- the write
    landed. This is the window the whole idempotency design exists for.
    """


class StatefulTenantTransport:
    """A tenant that holds purchase orders, in a dict, behind the real seam.

    Implements :class:`~app.backend.integration.adapter.Transport`, so the
    shipped adapters build the URL, the query, the scope and the body exactly
    as they would in production and this only decides what comes back.

    It owns two things the design depends on:

    * **Z-01** -- ``cf_capex_ref`` is unique. A second create carrying a key
      the tenant already holds is refused, and ``names_duplicate`` controls
      whether the refusal says *which* record holds it (Zoho's error payloads
      do not reliably say).
    * **the lost response** -- ``lose_response_on`` makes exactly one create
      write the record and then raise, which is the failure that used to leave
      a purchase order that exists and cannot be named.
    """

    def __init__(self, product, base_urls, *, unique_capex_ref=True,
                 names_duplicate=False, lose_response_on=0, per_page=50):
        self.product = product
        self.base_urls = base_urls
        self.unique_capex_ref = unique_capex_ref
        self.names_duplicate = names_duplicate
        self.lose_response_on = lose_response_on
        self.per_page = per_page
        self.records: dict[str, dict] = {}
        self.creates = 0
        self.log: list[tuple[str, str]] = []
        self._seq = 0

    # -------------------------------------------------------------- helpers
    def count_with_capex_ref(self, key) -> int:
        return sum(1 for r in self.records.values()
                   if _capex_ref(r) == key)

    def find_by_capex_ref(self, key):
        for external_id, row in self.records.items():
            if _capex_ref(row) == key:
                return external_id
        return None

    # ------------------------------------------------------------- Transport
    def request(self, *, method, base_url, path, scope, params=None, body=None):
        assert base_url in self.base_urls, f"unexpected base_url {base_url}"
        assert (params or {}).get("organization_id"), (
            "organization_id is a required query parameter on every request")
        self.log.append((method.upper(), path))
        params = dict(params or {})

        if method.upper() == "POST" and path.endswith("/status/open"):
            external_id = path.split("/")[2]
            self.records[external_id]["status"] = "open"
            return {"code": 0, "message": "status changed"}

        if method.upper() == "POST" and path == "/purchaseorders":
            return self._create(dict(body or {}))

        if method.upper() == "PUT" and path.startswith("/purchaseorders/"):
            external_id = path.rsplit("/", 1)[-1]
            if external_id not in self.records:
                raise IntegrationErrorish(f"no such purchase order {external_id}")
            self.records[external_id].update(dict(body or {}))
            return {"code": 0,
                    "purchaseorder": {"purchaseorder_id": external_id}}

        if method.upper() == "GET" and path == "/purchaseorders":
            return self._list(params)

        raise IntegrationErrorish(f"unhandled {method} {path}")

    # ---------------------------------------------------------------- writes
    def _create(self, body):
        self.creates += 1
        key = _capex_ref(body)
        if self.unique_capex_ref and key is not None:
            existing = self.find_by_capex_ref(key)
            if existing is not None:
                # Z-01 did its job. This is the success path of the design,
                # dressed as an error because that is how Zoho reports it.
                raise ob.DuplicateDedupeKey(
                    key, existing if self.names_duplicate else None)
        self._seq += 1
        external_id = f"PO-TENANT-{self._seq:04d}"
        body.setdefault("status", "draft")
        self.records[external_id] = body

        if self.creates == self.lose_response_on:
            # Written, and the caller will never learn the id.
            raise ResponseLost(
                "Function killed after the write landed, before the response "
                "was recorded.")
        return {"code": 0,
                "purchaseorder": {"purchaseorder_id": external_id}}

    # ----------------------------------------------------------------- reads
    def _list(self, params):
        rows = []
        for external_id, row in self.records.items():
            rows.append({**row, "purchaseorder_id": external_id})

        wanted = params.get("custom_field")
        if wanted:
            # The documented ERP search. Modelled as a real filter here; the
            # test below models the tenant that IGNORES it instead.
            field, _, value = str(wanted).partition(":")
            rows = [r for r in rows if _capex_ref(r) == value]

        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", self.per_page))
        start = (page - 1) * per_page
        window = rows[start:start + per_page]
        return {
            "code": 0,
            "purchaseorders": window,
            "page_context": {"page": page, "per_page": per_page,
                             "has_more_page": start + per_page < len(rows)},
        }


class IntegrationErrorish(Exception):
    """A transport-level failure that is not the lost-response window."""


def _capex_ref(row):
    for field in row.get("custom_fields") or ():
        if field.get("api_name") == DEDUPE_CUSTOM_FIELD:
            return field.get("value")
    return None


def _real_adapter(product, **kwargs):
    """A shipped adapter wired to a stateful fake tenant over the real seam."""
    if product == "ERP":
        bases = {"https://www.zohoapis.in/erp/v3"}
        transport = StatefulTenantTransport(product, bases, **kwargs)
        return ErpAdapter(organization_id="60000000001", dc="IN",
                          transport=transport), transport
    bases = {"https://www.zohoapis.in/books/v3",
             "https://www.zohoapis.in/inventory/v1"}
    transport = StatefulTenantTransport(product, bases, **kwargs)
    return BooksInventoryAdapter(organization_id="60000000001", dc="IN",
                                 transport=transport), transport


def _po_draft(local_id="PR-0001", *, dedupe_key=None):
    """The document handed to a REAL adapter, built through the real mapping.

    This used to build a ``PurchaseOrderDTO`` -- and it could only do so by
    fabricating five fields an emission does not have: an empty
    ``external_id`` (Zoho mints it in the response), an empty
    ``document_number``, an empty ``external_status_raw``, a ``last_modified``
    borrowed from the test clock, and a ``SourceRef`` claiming the document had
    been *retrieved* from ``/purchaseorders``. That last one is a provenance
    claim about a document nobody had fetched. Four empty strings and a false
    audit trail is what reusing the inbound type actually cost, and it is why
    :class:`PurchaseOrderEmissionDTO` exists.

    Routed through ``outbound.emission_dto`` rather than constructed here, so
    these tests exercise the same mapper ``emit_purchase_order`` uses. A
    hand-built document would let the mapper rot while every test still passed
    -- which is exactly how a payload dict reached two adapters that read it by
    attribute.
    """
    return emission_document(local_id, dedupe_key=dedupe_key)


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_the_shipped_adapters_can_now_recover_a_lost_purchase_order(product):
    """**The gap, closed and measured, on the code that ships.**

    The sequence is the one §11.6 describes and the one that used to be
    unrecoverable:

    1. we emit a purchase order carrying ``cf_capex_ref``;
    2. the tenant writes it and the response is lost -- the Function is gone
       before it learns the id;
    3. a later tick retries, and *resolves the key first*;
    4. it finds the purchase order that already exists, adopts its id, and
       updates rather than creating.

    The assertions that matter are the last two: the local record ends up
    LINKED to the real purchase order, and the tenant holds exactly ONE.
    """
    adapter, tenant = _real_adapter(product, lose_response_on=1,
                                    names_duplicate=False)
    key = "CAPEX-PO-000117"

    # 1 + 2. The write lands; the response does not come back.
    with pytest.raises(ResponseLost):
        adapter.create_purchase_order(_po_draft(), key)
    assert tenant.count_with_capex_ref(key) == 1
    assert tenant.creates == 1

    # 3. The retry resolves before it creates. THIS is the call C1 lacked.
    adopted = adapter.resolve_by_dedupe_key(key)
    assert adopted is not None, (
        "The purchase order EXISTS in the tenant and the retry could not name "
        "it. That is the money-relevant gap this stream exists to close.")

    # 4. Adopt and update -- never create a second commitment.
    adapter.update_purchase_order(adopted, _po_draft(), key)

    assert tenant.count_with_capex_ref(key) == 1, (
        "A duplicate purchase order was created. A PO is a commitment; a "
        "duplicate is money the company believes it owes twice.")
    assert tenant.creates == 1, "The recovery path must not re-create."
    assert _capex_ref(tenant.records[adopted]) == key, (
        "The adopted record must still carry the dedupe key, or the next lost "
        "response is unrecoverable again.")


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_the_whole_emission_recovers_through_emit_purchase_order(product):
    """The same recovery, driven by the real caller rather than by hand.

    ``emit_purchase_order`` is what the job actually calls. It probes the
    adapter for ``resolve_by_dedupe_key`` with ``getattr`` -- so before this
    stream it found nothing on either shipped adapter, went DEAD after eight
    attempts and raised ``ORPHANED_EMISSION``. Now it resolves, adopts, and
    the row reaches SENT with the external id recorded against it.
    """
    adapter, tenant = _real_adapter(product, lose_response_on=1,
                                    names_duplicate=False)
    store = InMemoryOutboxStore()
    enqueued(store)
    key = store.get("OB-1").dedupe_key

    # Seed the tenant the way a lost response would have: the record exists,
    # and nothing local knows its id.
    try:
        adapter.create_purchase_order(
            ob.emission_dto_from_record(store.get("OB-1")), key)
    except ResponseLost:
        pass
    assert tenant.count_with_capex_ref(key) == 1

    # NO SHIM. `emit_purchase_order` is handed the shipped adapter directly.
    #
    # It used to be handed a `_PayloadShim`, because `emit` passed the outbox
    # payload dict and these adapters take a document they read by attribute.
    # The shim was labelled and honest about standing in for a mapping nobody
    # had written -- but it meant this test proved the RECOVERY while leaving
    # the ordinary path unproven, and the ordinary path was broken. The mapping
    # now lives in `outbound.emission_dto_from_record`, so the shim has nothing
    # left to stand in for.
    result = ob.emit_purchase_order(adapter=adapter, store=store,
                                    outbox_id="OB-1", now=NOW)

    assert result.adopted is True and result.created is False
    assert store.get("OB-1").state == ob.OUTBOX_SENT
    assert store.get("OB-1").external_id == result.external_id
    assert tenant.count_with_capex_ref(key) == 1
    assert ob.orphaned_emission_finding(store.get("OB-1"),
                                        entity_id="ENT-1") is None


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_the_first_emission_through_a_shipped_adapter_creates_the_po(product):
    """**The defect this stream was sent to fix, stated as a positive test.**

    Not a retry. Not a recovery. The FIRST emission of a fresh outbox row,
    driven by ``emit_purchase_order`` into a real adapter, with nothing in the
    tenant and nothing lost.

    Before the mapping existed this raised
    ``AttributeError: 'dict' object has no attribute 'vendor_external_id'`` on
    both products, because ``emit`` passed ``record.payload`` -- a
    ``Mapping`` -- and ``_emission_body`` reads the document by attribute.
    Every chaos test above passed anyway, because ``FakeAdapter`` accepted a
    Mapping. That is the whole lesson: the suite proved the caller agreed with
    its own double.

    The monetary assertions are the other half. A mapping that swapped
    ``amount_paise`` into ``unit_price_paise`` would still create exactly one
    purchase order and still satisfy every duplicate assertion in this file --
    for a different amount of money.
    """
    adapter, tenant = _real_adapter(product, names_duplicate=False)
    store = InMemoryOutboxStore()
    enqueued(store)
    key = store.get("OB-1").dedupe_key

    assert tenant.records == {}, "Nothing in the tenant. This is attempt one."

    result = ob.emit_purchase_order(adapter=adapter, store=store,
                                    outbox_id="OB-1", now=NOW)

    assert result.created is True and result.adopted is False
    assert result.attempts == 0, "First attempt: nothing had failed before it."
    assert store.get("OB-1").state == ob.OUTBOX_SENT
    assert store.get("OB-1").external_id == result.external_id
    assert tenant.count_with_capex_ref(key) == 1

    # The document actually reached the tenant, with the values the draft held.
    expected = ob.emission_dto_from_record(store.get("OB-1"))
    record = tenant.records[result.external_id]
    assert record["vendor_id"] == expected.vendor_external_id == "ZV-77"
    assert record["date"] == expected.document_date.isoformat()
    assert record["currency_code"] == "INR"

    # Money, end to end and integer throughout. `line_total_paise` is the LINE
    # TOTAL and `unit_price_paise` the rate; a mapper that confused the two
    # would multiply the commitment by the quantity and nothing else here would
    # notice.
    line = expected.lines[0]
    assert line.unit_price_paise == 250_000_00
    assert line.line_total_paise == 250_000_00
    assert line.quantity == "1"
    assert expected.subtotal_paise + expected.tax_paise == expected.total_paise
    assert expected.total_paise == 250_000_00
    rupees, sub = divmod(line.unit_price_paise, 100)
    assert record["line_items"][0]["rate"] == f"{rupees}.{sub:02d}"
    assert all(not isinstance(v, float) for v in
               (line.unit_price_paise, line.line_total_paise,
                expected.total_paise, expected.subtotal_paise,
                expected.tax_paise)), "No float ever touches a commitment."


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_a_raw_payload_mapping_is_refused_by_the_shipped_adapters(product):
    """The mutation control for the test above.

    Restore the defect -- hand the adapter ``record.payload`` instead of the
    mapped document -- and the emission must fail. If this ever passes, the
    adapters have started accepting a Mapping and the positive test above has
    stopped meaning anything.
    """
    adapter, _ = _real_adapter(product, names_duplicate=False)
    store = InMemoryOutboxStore()
    enqueued(store)
    row = store.get("OB-1")

    with pytest.raises(AttributeError, match="vendor_external_id"):
        adapter.create_purchase_order(row.payload, row.dedupe_key)


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_a_tenant_that_ignores_the_search_parameter_still_cannot_mislink(product):
    """The failure mode the verification guard exists for.

    An unrecognised query parameter on these APIs is **ignored, not rejected**.
    So a search that silently degrades returns page 1 of every purchase order
    in the tenant. An adapter that trusted the filter would adopt the first row
    and link this commitment to an unrelated document -- silently, and wrongly.

    Here the tenant ignores ``custom_field`` entirely and holds three unrelated
    purchase orders in front of ours. The resolver must still return ours, and
    must return ``None`` for a key nobody holds.
    """
    adapter, tenant = _real_adapter(product, names_duplicate=False)
    for other in ("CAPEX-OTHER-1", "CAPEX-OTHER-2", "CAPEX-OTHER-3"):
        adapter.create_purchase_order(_po_draft(), other)
    ours = "CAPEX-PO-000117"
    adapter.create_purchase_order(_po_draft(), ours)

    # The tenant stops honouring the documented filter.
    original = StatefulTenantTransport._list

    def ignoring_list(self, params):
        return original(self, {k: v for k, v in params.items()
                               if k != "custom_field"})

    StatefulTenantTransport._list = ignoring_list
    try:
        assert adapter.resolve_by_dedupe_key(ours) is not None
        assert _capex_ref(tenant.records[adapter.resolve_by_dedupe_key(ours)]) == ours
        assert adapter.resolve_by_dedupe_key("CAPEX-NOBODY-HOLDS") is None
    finally:
        StatefulTenantTransport._list = original


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_negative_control_without_the_resolve_the_link_is_lost(product):
    """NEGATIVE CONTROL. Remove the resolve; watch the link disappear.

    Z-01 still holds, so there is still no duplicate -- that is the point of
    keeping the two mechanisms separate. What is lost without the resolve is
    the ability to NAME the purchase order, which is exactly the state that
    used to go DEAD as ``ORPHANED_EMISSION`` and block period close.
    """
    adapter, tenant = _real_adapter(product, lose_response_on=1,
                                    names_duplicate=False)
    key = "CAPEX-PO-000117"
    with pytest.raises(ResponseLost):
        adapter.create_purchase_order(_po_draft(), key)

    # The retry, on an adapter that cannot resolve: the tenant refuses it.
    with pytest.raises(ob.DuplicateDedupeKey) as exc:
        adapter.create_purchase_order(_po_draft(), key)
    assert exc.value.external_id is None, (
        "This tenant does not name the colliding record -- which is the whole "
        "reason a resolve call is needed.")

    assert tenant.count_with_capex_ref(key) == 1     # Z-01 held
    # ...but nothing in that exception could tell us which record it is.
    # With the resolve, it can:
    assert adapter.resolve_by_dedupe_key(key) is not None


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_negative_control_without_z01_the_lost_response_duplicates(product):
    """NEGATIVE CONTROL. Remove the unique field; the duplicate appears.

    The resolve is only half the guarantee. Without Z-01 the retry races: if
    the resolve runs before the write is visible, nothing stops a second
    create. This is what the unique custom field is buying, and removing it
    must produce a duplicate or it was never load-bearing.
    """
    adapter, tenant = _real_adapter(product, unique_capex_ref=False,
                                    lose_response_on=1)
    key = "CAPEX-PO-000117"
    with pytest.raises(ResponseLost):
        adapter.create_purchase_order(_po_draft(), key)
    adapter.create_purchase_order(_po_draft(), key)

    assert tenant.count_with_capex_ref(key) == 2, (
        "Without Z-01 a lost response must duplicate. If it does not, this "
        "test is no longer measuring the mechanism it names.")
    assert tenant.creates == 2


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_negative_control_a_changed_dedupe_key_duplicates(product):
    """NEGATIVE CONTROL. Change the key between attempts; Z-01 cannot help.

    The unique field only deduplicates values that are *equal*. A key derived
    freshly per attempt -- a uuid, a timestamp -- makes every retry a new
    commitment, and the unique index never fires. Which is why
    ``derive_dedupe_key`` is deterministic and why that is separately tested.
    """
    adapter, tenant = _real_adapter(product, lose_response_on=1)
    with pytest.raises(ResponseLost):
        adapter.create_purchase_order(_po_draft(), "CAPEX-" + uuid.uuid4().hex)
    adapter.create_purchase_order(_po_draft(), "CAPEX-" + uuid.uuid4().hex)

    assert len(tenant.records) == 2, (
        "Two different keys must produce two purchase orders -- otherwise the "
        "determinism of derive_dedupe_key is not what is protecting us.")


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_the_shipped_adapters_declare_themselves_recoverable(product):
    """A caller can ask, rather than probing with getattr and hoping."""
    adapter, _ = _real_adapter(product)
    assert isinstance(adapter, RecoverableProcurementAdapter)


@pytest.mark.parametrize("product", REAL_ADAPTERS)
def test_a_recovered_purchase_order_can_then_be_opened(product):
    """§11.7's last step, on a record we only hold because we recovered it.

    An adopted purchase order is still a draft. If it could not then be
    transitioned, recovery would have produced a commitment stuck outside the
    approval flow -- linked, but never actually placed.
    """
    adapter, tenant = _real_adapter(product, lose_response_on=1,
                                    names_duplicate=False)
    key = "CAPEX-PO-000117"
    with pytest.raises(ResponseLost):
        adapter.create_purchase_order(_po_draft(), key)

    adopted = adapter.resolve_by_dedupe_key(key)
    assert tenant.records[adopted]["status"] == "draft"
    adapter.transition_purchase_order(adopted, "open", "U-1")
    assert tenant.records[adopted]["status"] == "open"
    assert tenant.count_with_capex_ref(key) == 1
