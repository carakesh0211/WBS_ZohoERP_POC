"""The inbound sweeps, run server-side against a LIVE connection (Fable 5.1).

WHAT THIS IS
  The wiring that lets a connection in ``LIVE_READ`` (or ``LIVE_WRITE``)
  mode run the EXISTING sweep framework -- :mod:`.sweeps` on :mod:`.jobs`,
  persisting through :mod:`app.backend.pg.integration_store` -- against the
  real Zoho ERP tenant through :class:`.live_transport.LiveTransport`. Nothing
  here is a second sweep: every poll below is `poll_contacts`,
  `poll_items`, `poll_purchaseorders`, `poll_bills` or `SweepPoAnchored`
  exactly as the cron path would run them, claimed through a ``job`` row,
  checkpointed per step, and charged against the connection's POLLING rate
  budget before every call.

THE THREE PIECES, AND WHY EACH IS SHAPED AS IT IS
  * :class:`PgSweepStore` fills :class:`sweeps.SweepStore` by DELEGATING to
    the session-bound functions in ``pg/integration_store.py``. The names on
    the two sides differ (`upsert_inbox` is `record_inbound`,
    `get_watermark`/`set_watermark` are `read_watermark`/`advance_watermark`)
    and so does one return shape; the mapping is written out method by method
    so a reviewer can check each one against the protocol without a third
    document. The two PO-discovery reads the store never had --
    `open_purchase_orders`, `known_purchase_order` -- are queries on
    ``purchase_order.external_source / external_id`` for the connection's
    estate, through `repo.query` with the literal ``{scope}`` token.
  * :class:`ConnectionJobStore` IS :class:`jobs.PostgresJobStore`, with the
    claim bound to one connection. The framework's claim statement selects by
    ``kind`` alone -- right for a cron tick that serves every connection --
    but an operator sweeping connection A must not claim, run and checkpoint a
    ``poll_bills`` row that belongs to connection B. The statement is DERIVED
    from ``jobs.CLAIM_JOB_SQL`` at import time rather than copied, and the
    derivation is asserted, so the two cannot drift.
  * :class:`PollingBudget` fills :class:`jobs.RateBudget` with
    :func:`throttle.reserve` on the POLLING lane. Over budget returns False,
    the job checkpoints, and the next call resumes -- plan §11.6, unchanged.

READ-ONLY, TWICE OVER
  Nothing in this module names an HTTP method. The adapter issues GETs; the
  transport refuses anything else while ``CAPEX_ERP_OUTBOUND_WRITES`` is
  unset, and this module never sets it. The tests drive a fake transport that
  records every request and asserts no non-GET ever reached it.

WHAT IT DOES NOT DO
  Books / Inventory. No live transport exists for that product on this
  branch, so :func:`adapter_for_connection` answers ``None`` and
  :func:`run_inbound_sweep` refuses with a coded error rather than
  pretending. Control totals and the completeness sweep are likewise not
  wired: the store has no ``periods_to_reconcile`` surface yet, and the
  protocol methods that would need it raise a coded error naming the gap.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..pg import integration_store as store
from ..pg import procurement, repo
from ..pg.engine import Session
from . import jobs, sweeps, throttle
from .adapter import Transport, receives_strategy
from .erp import API_VERSION as _ERP_API_VERSION
from .erp import DAILY_CALL_CEILING_STANDARD, ErpAdapter

# ------------------------------------------------------------------ constants
#: The request-facing names of the five inbound sweeps, in the order they run.
#: Masters first (a bill names a vendor and an item), documents next, and the
#: PO-anchored receives walk last because it is the most expensive per unit
#: and the one whose cursor most needs the others' rows to already be there.
MODULE_CONTACTS = "contacts"
MODULE_ITEMS = "items"
MODULE_PURCHASE_ORDERS = "purchaseorders"
MODULE_BILLS = "bills"
MODULE_RECEIVES = "receives"
DEFAULT_MODULES: tuple[str, ...] = (
    MODULE_CONTACTS, MODULE_ITEMS, MODULE_PURCHASE_ORDERS, MODULE_BILLS,
    MODULE_RECEIVES)

#: ``integration_connection.product`` -> the ``external_source`` label
#: `purchase_order`, `grn` and `bill` carry (`002_financial_controls.sql`,
#: `pg/masters.ingest_from_adapter`, `pg/procurement._adapter_product_of`).
#: OUR label for where a document came from; the adapter's product name is
#: not written into the ledger.
EXTERNAL_SOURCE_BY_PRODUCT: Mapping[str, str] = {"ERP": "ZOHO_ERP"}

#: Purchase-order statuses under which the PO-anchored walk stops reading a
#: PO. `compute_ledger` releases commitment on exactly these two
#: (014_procurement_corrections.sql:553), so a PO in either state can no
#: longer receive against an open commitment.
PO_TERMINAL_STATUSES: tuple[str, ...] = ("Cancelled", "Closed")

#: The soft deadline for ONE operator-triggered sweep call. The framework's
#: default (720 s) is sized for a 15-minute Function; this path answers an
#: HTTP request whose AppSail ceiling is 30 s
#: (`jobs.APPSAIL_REQUEST_CEILING_SECONDS`). The budget is SHARED across the
#: modules of one call, and a job that runs out simply checkpoints -- the next
#: call resumes it, which is the framework's contract, not a workaround.
ROUTE_SOFT_DEADLINE_SECONDS = 20

#: Pages one poll may walk per call on this path. Bounded for the same reason:
#: the window stays open in the checkpoint and the watermark does NOT move
#: until it is exhausted, so a bound here costs nothing but latency.
ROUTE_MAX_PAGES_PER_POLL = 5


class LiveSweepError(store.IntegrationStoreError):
    """A coded refusal from this module. Same shape as the store's errors so
    the router maps it to HTTP the same way."""


# ============================================================== the job store
def _bind_claim_to_connection(statement: str) -> str:
    """`jobs.CLAIM_JOB_SQL` with the claimable CTE narrowed to one connection.

    Derived, not copied: if the framework's statement changes shape the
    anchor below stops matching and this raises at import time, which is the
    loud failure a silently unbound claim must never be allowed to hide.
    """
    anchor = "WHERE kind = %(kind)s"
    if statement.count(anchor) != 1:
        raise RuntimeError(
            "jobs.CLAIM_JOB_SQL no longer carries the single `WHERE kind = "
            "%(kind)s` anchor this module binds the connection to; the "
            "connection-bound claim cannot be derived and must be revisited.")
    return statement.replace(
        anchor, anchor + "\n       AND connection_id = %(connection_id)s")


CLAIM_CONNECTION_JOB_SQL = _bind_claim_to_connection(jobs.CLAIM_JOB_SQL)


class ConnectionJobStore(jobs.PostgresJobStore):
    """:class:`jobs.PostgresJobStore`, claiming only this connection's rows.

    Everything else -- checkpoint, finish, event -- is inherited unchanged.
    """

    def __init__(self, session: Session, *, connection_id: str) -> None:
        super().__init__(session)
        self.connection_id = connection_id

    def claim_job(self, *, kind: str, now: datetime, lease_seconds: int,
                  soft_deadline_at: datetime, limit: int = 1,
                  locked_by: str | None = None) -> jobs.ClaimedJob | None:
        holder = locked_by or getattr(
            getattr(self.session, "scope", None), "user_id", None) or "SVC-JOBS"
        rows = self._repo().query(
            self.session, CLAIM_CONNECTION_JOB_SQL,
            {"kind": kind,
             "connection_id": self.connection_id,
             "claimable_states": list(jobs.CLAIMABLE_STATES),
             "claimed_state": jobs.JOB_CLAIMED,
             "now": now,
             "lease_until": now + timedelta(seconds=lease_seconds),
             "soft_deadline_at": soft_deadline_at,
             "locked_by": holder,
             "limit": limit},
            columns=jobs.JOB_SCOPE_COLUMNS)
        if not rows:
            return None
        job_id, job_kind, checkpoint, resume_count, cid, connection_id = rows[0]
        return jobs.ClaimedJob(job_id=job_id, kind=job_kind,
                               checkpoint=dict(checkpoint or {}),
                               resume_count=resume_count or 0,
                               correlation_id=cid, connection_id=connection_id)


# ================================================================= the budget
@dataclass
class _At:
    """A `throttle.Clock` frozen at the instant the runner asked about."""

    moment: datetime

    def now(self) -> datetime:
        return self.moment


@dataclass
class PollingBudget:
    """`jobs.RateBudget` over `throttle.reserve` on the POLLING lane.

    `consume` returns False rather than raising, as the port requires: over
    budget is an ordinary outcome that ends in a checkpoint. The last verdict
    is kept so the sweep's result can report what each window holds.
    """

    session: Session
    connection_id: str
    actor: str
    last_verdict: throttle.BudgetVerdict | None = None
    refusals: int = 0

    def consume(self, *, connection_id: str | None, calls: int,
                now: datetime) -> bool:
        verdict = throttle.reserve(
            self.session, connection_id=connection_id or self.connection_id,
            lane=throttle.Lane.POLLING, cost=calls, actor=self.actor,
            clock=_At(now if now.tzinfo else now.replace(tzinfo=timezone.utc)))
        self.last_verdict = verdict
        if not verdict:
            self.refusals += 1
        return bool(verdict)

    def describe(self) -> dict[str, Any]:
        verdict = self.last_verdict
        if verdict is None:
            return {"asked": False, "refusals": self.refusals}
        return {
            "asked": True, "refusals": self.refusals,
            "binding": verdict.binding.value if verdict.binding else None,
            "windows": {
                w.kind.value: {"used": w.used, "ceiling": w.ceiling,
                               "remaining": w.remaining}
                for w in verdict.windows},
        }


# ================================================================== the store
@dataclass
class PgSweepStore:
    """:class:`sweeps.SweepStore`, delegating to `pg/integration_store.py`.

    Bound to ONE session and ONE connection. Every method below names the
    store function it stands in front of; where the two signatures differ the
    difference is visible in the body rather than in a helper.
    """

    session: Session
    connection: Mapping[str, Any]
    actor: str
    #: Stamped on every inbox row as `external_status_product` /
    #: `external_status_api_version`, so an unmapped raw status stays
    #: resolvable against the C17 rows for the product that emitted it.
    status_api_version: str | None = None
    # ---- counters the sweep result reports. Kept on the store because the
    # runner only retains the LAST step's detail, and "how many rows did this
    # call create" is a whole-run figure.
    created: dict[str, int] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=dict)
    exceptions_raised: list[str] = field(default_factory=list)
    receive_lines_recorded: int = 0

    # ------------------------------------------------------------ identity
    @property
    def connection_id(self) -> str:
        return str(self.connection["connection_id"])

    @property
    def entity_id(self) -> str:
        return str(self.connection["entity_id"])

    @property
    def external_source(self) -> str:
        return external_source_for(self.connection)

    # ---- inbound idempotency -> record_inbound ---------------------------
    def upsert_inbox(self, *, connection_id: str, module: str,
                     external_id: str, payload_sha: str,
                     payload: Mapping[str, Any],
                     external_status_raw: str | None,
                     received_at: datetime,
                     correlation_id: str | None) -> bool:
        """True when a row was CREATED. `record_inbound` hashes the payload
        itself, canonically -- the same rendering `sweeps.payload_sha` uses --
        so `payload_sha` is the sweep's expectation, and the store's own hash
        is what lands in the row."""
        _inbox_id, inserted = store.record_inbound(
            self.session, inbox_id=f"INB-{uuid.uuid4().hex}",
            connection_id=connection_id, module=module,
            external_id=external_id, raw_payload=payload,
            external_status_raw=external_status_raw,
            external_status_product=str(self.connection.get("product") or "") or None,
            external_status_api_version=self.status_api_version,
            correlation_id=correlation_id, now=received_at)
        bucket = self.created if inserted else self.duplicates
        bucket[module] = bucket.get(module, 0) + 1
        return inserted

    # ---- watermarks -> read_watermark / advance_watermark ---------------
    def get_watermark(self, *, connection_id: str,
                      module: str) -> datetime | None:
        row = store.read_watermark(self.session, connection_id=connection_id,
                                   module=module)
        return None if row is None else row["hwm"]

    def set_watermark(self, *, connection_id: str, module: str,
                      hwm: datetime, now: datetime) -> None:
        store.advance_watermark(self.session, connection_id=connection_id,
                                module=module, hwm=hwm, actor=self.actor,
                                now=now)

    # ---- reconciliation -> raise_exception / open_exceptions ------------
    def raise_exception(self, *, kind: str, object_type: str,
                        object_id: str | None, detail: str,
                        raised_at: datetime, entity_id: str | None = None,
                        project_id: str | None = None,
                        local_paise: int | None = None,
                        source_paise: int | None = None,
                        correlation_id: str | None = None) -> str:
        exception_id = store.raise_exception(
            self.session, kind=kind, object_type=object_type,
            object_id=object_id, detail=detail, raised_at=raised_at,
            entity_id=entity_id, project_id=project_id,
            local_paise=local_paise, source_paise=source_paise,
            correlation_id=correlation_id, actor=self.actor)
        self.exceptions_raised.append(exception_id)
        return exception_id

    def open_exceptions(self, *, entity_id: str | None = None,
                        project_id: str | None = None) -> list[Mapping[str, Any]]:
        return store.open_exceptions(self.session, entity_id=entity_id,
                                     project_id=project_id)

    # ---- PO-anchored discovery -> purchase_order.external_* -------------
    def open_purchase_orders(self, *, connection_id: str,
                             after_po_id: str | None,
                             limit: int) -> list[sweeps.LocalPurchaseOrder]:
        """The connection's mirrored, still-open purchase orders, keyed on
        `po_id` so the walk's `last_po_id_swept` cursor is stable.

        "Open" is `status NOT IN ('Cancelled', 'Closed')` -- the two states on
        which commitment is released -- AND `external_id IS NOT NULL` for THIS
        connection's `external_source`: a locally-raised PO that has not been
        emitted yet has nothing at Zoho to anchor a receive on.
        """
        rows = repo.query(
            self.session,
            f"""
            SELECT po.po_id, po.external_id, p.entity_id, po.project_id,
                   po.po_number, po.currency
            FROM {store.PURCHASE_ORDER} po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.external_source = %(source)s
              AND po.external_id IS NOT NULL
              AND NOT (po.status = ANY(%(terminal)s))
              AND p.entity_id = %(entity_id)s
              AND (%(after)s::text IS NULL OR po.po_id > %(after)s)
              AND {{scope}}
            ORDER BY po.po_id
            LIMIT %(limit)s
            """,
            {"source": self.external_source, "terminal": list(PO_TERMINAL_STATUSES),
             "entity_id": self.entity_id, "after": after_po_id,
             "limit": int(limit)},
            columns=store.PROCUREMENT_SCOPE_COLUMNS,
        )
        # `currency` (migration 029) rides along so the PO-anchored walk can
        # hold a receive against a non-INR order under
        # FOREIGN_CURRENCY_BASIS_MISSING (decision 8) instead of booking its
        # face value as paise. NULL never happens post-029, but the dataclass
        # default is the base currency, so a NULL would mean INR -- the same
        # reading `_po_basis` gives an order with no currency stated.
        return [sweeps.LocalPurchaseOrder(
            po_id=r[0], external_id=r[1], entity_id=r[2], project_id=r[3],
            document_number=r[4], currency_code=str(r[5] or sweeps.BASE_CURRENCY))
            for r in rows]

    def known_purchase_order(self, *, connection_id: str,
                             external_id: str) -> bool:
        row = repo.query_one(
            self.session,
            f"""
            SELECT 1
            FROM {store.PURCHASE_ORDER} po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.external_source = %(source)s
              AND po.external_id = %(external_id)s
              AND p.entity_id = %(entity_id)s
              AND {{scope}}
            """,
            {"source": self.external_source, "external_id": str(external_id),
             "entity_id": self.entity_id},
            columns=store.PROCUREMENT_SCOPE_COLUMNS,
        )
        return row is not None

    def resolve_po_line(self, *, po_external_id: str,
                        line_external_id: str | None) -> str | None:
        return store.resolve_po_line(self.session, po_external_id=po_external_id,
                                     line_external_id=line_external_id)

    def record_receive_line(self, *, po_line_id: str, receive_external_id: str,
                            line_external_id: str | None, quantity: Any,
                            amount_paise: int | None,
                            external_source: str | None = None,
                            receive_number: str | None = None,
                            received_at: datetime | None = None,
                            external_last_modified: datetime | None = None,
                            payload_sha: str | None = None) -> None:
        store.record_receive_line(
            self.session, po_line_id=po_line_id,
            receive_external_id=receive_external_id,
            line_external_id=line_external_id, quantity=quantity,
            amount_paise=amount_paise,
            external_source=external_source or self.external_source,
            receive_number=receive_number, received_at=received_at,
            external_last_modified=external_last_modified,
            payload_sha=payload_sha, actor=self.actor)
        self.receive_lines_recorded += 1

    def accumulate_unattributed(self, *, project_id: str | None, paise: int,
                                source_key: str) -> None:
        store.accumulate_unattributed(self.session, project_id=project_id,
                                      paise=paise, source_key=source_key)

    # ---- bill detail hydration -> procurement.mirror_bill + the queue ---
    def mirror_bill(self, *, external_source: str, external_id: str,
                    bill_number: str, vendor_name: str, bill_date: date,
                    lines: Sequence[Any],
                    po_external_id: str | None = None,
                    project_id: str | None = None,
                    entity_id: str | None = None,
                    external_status_raw: str | None = None,
                    external_last_modified: datetime | None = None,
                    payload_sha: str | None = None,
                    source_currency: str = "INR",
                    exchange_rate: Any = None,
                    fx_rate_source: str | None = None,
                    correlation_id: str | None = None) -> Mapping[str, Any]:
        # `exchange_rate` / `fx_rate_source` are the bill's OWN rate as the
        # sweep read it (a decimal string, never a float) and its provenance;
        # the ledger's `mirror_bill` uses them for a non-INR bill and refuses
        # the bill with FOREIGN_CURRENCY_BASIS_MISSING when they are absent.
        return procurement.mirror_bill(
            self.session, external_source=external_source,
            external_id=external_id, bill_number=bill_number,
            vendor_name=vendor_name, bill_date=bill_date, lines=lines,
            po_external_id=po_external_id, project_id=project_id,
            entity_id=entity_id, external_status_raw=external_status_raw,
            external_last_modified=external_last_modified,
            payload_sha=payload_sha, source_currency=source_currency,
            exchange_rate=exchange_rate, fx_rate_source=fx_rate_source,
            correlation_id=correlation_id, actor=self.actor)

    def bills_awaiting_detail(self, *, connection_id: str,
                              limit: int) -> list[str]:
        return store.bills_awaiting_detail(self.session,
                                           connection_id=connection_id,
                                           limit=limit)

    def mark_detail_hydrated(self, *, connection_id: str,
                             external_id: str) -> None:
        store.mark_detail_hydrated(self.session, connection_id=connection_id,
                                   external_id=external_id, actor=self.actor)

    # ---- completeness and control totals: NOT WIRED ---------------------
    def _unsupported(self, name: str) -> Any:
        raise LiveSweepError(
            "SWEEP_STORE_UNSUPPORTED",
            f"PgSweepStore.{name} has no PostgreSQL implementation: "
            f"`integration_store` carries no accounting-period surface for the "
            f"control-total and completeness sweeps yet. Reported rather than "
            f"answered with an empty list a sweep would read as 'nothing to "
            f"reconcile'.", status=501)

    def periods_to_reconcile(self, *, connection_id: str) -> list[sweeps.PeriodRef]:
        return self._unsupported("periods_to_reconcile")

    def control_total_slices(self, *, connection_id: str,
                             period: sweeps.PeriodRef) -> list[tuple[str, str]]:
        return self._unsupported("control_total_slices")

    def local_control_total(self, *, module: str, period: sweeps.PeriodRef,
                            status: str) -> sweeps.ControlTotal:
        return self._unsupported("local_control_total")

    def local_documents(self, *, module: str,
                        period: sweeps.PeriodRef) -> list[sweeps.LocalDocument]:
        return self._unsupported("local_documents")


# ================================================================ the adapter
def external_source_for(connection: Mapping[str, Any]) -> str:
    product = str(connection.get("product") or "").strip().upper()
    try:
        return EXTERNAL_SOURCE_BY_PRODUCT[product]
    except KeyError:
        raise LiveSweepError(
            "LIVE_ADAPTER_UNAVAILABLE",
            f"No external_source label is defined for product {product!r}; "
            f"only {sorted(EXTERNAL_SOURCE_BY_PRODUCT)} can be swept live on "
            f"this branch.", status=503) from None


def is_live(connection: Mapping[str, Any]) -> bool:
    return str(connection.get("mode") or "") in store.LIVE_MODES


def adapter_for_connection(connection: Mapping[str, Any], *,
                           transport: Transport | None = None) -> ErpAdapter | None:
    """The adapter that can reach this connection's tenant, or ``None``.

    ``None`` for every connection that is not (ERP, data centre IN, a live
    mode). Books / Inventory answers ``None`` even in a live mode because no
    live transport exists for it on this branch; :func:`run_inbound_sweep`
    turns that into a coded refusal rather than a silent no-op.

    `transport` is injectable so a test can drive the real adapter with a
    recording fake; when it is omitted the process-wide
    :func:`~app.backend.integration.live_transport.shared_transport` cache is
    asked instead of constructing a fresh :class:`LiveTransport`, so the
    100/minute sliding window and the minted-token cache span more than one
    sweep tick (2026-09-12 review, item 2). It reads its credential from
    outside the repository and refuses every non-GET.
    """
    product = str(connection.get("product") or "").strip().upper()
    dc = str(connection.get("dc") or "").strip().upper()
    if product != "ERP" or dc != "IN" or not is_live(connection):
        return None
    if transport is None:
        # Imported here, not at module level: `live_transport` reads no
        # credential at import, but keeping it out of the import graph of a
        # module the router loads means a process with no credential file
        # still mounts the route and answers a coded 503 from it.
        from .live_transport import shared_transport
        transport = shared_transport()
    # The connection row's own ceiling, so `Capabilities.daily_call_ceiling`
    # reports the tenant's plan rather than the Standard-plan default.
    return ErpAdapter(organization_id=str(connection["organization_id"]),
                      dc="IN", transport=transport,
                      plan_daily_ceiling=int(connection.get("daily_call_ceiling")
                                             or DAILY_CALL_CEILING_STANDARD))


# ================================================================ the sweep
def _require_modules(modules: Sequence[str]) -> tuple[str, ...]:
    wanted = tuple(str(m).strip() for m in (modules or ()))
    unknown = [m for m in wanted if m not in DEFAULT_MODULES]
    if unknown or not wanted:
        raise LiveSweepError(
            "UNKNOWN_SWEEP_MODULE",
            f"modules must name one or more of {list(DEFAULT_MODULES)}; "
            f"got {list(wanted)}.", status=422)
    # Run in the canonical order whatever order was asked for.
    return tuple(m for m in DEFAULT_MODULES if m in wanted)


def _require_principal(session: Session, actor: str) -> str:
    """`job.principal_user_id` is a NOT NULL FK to `app_user`. A caller the
    PostgreSQL identity tables do not know cannot own a job row, and a
    ForeignKeyViolation at minute one is a worse report than this."""
    # `app_user` is the identity table (migration 004), not a scoped business
    # row, so it is not in the scope gate's SCOPABLE set; this is an existence
    # check on the caller's own user id and nothing is read off the row.
    row = session.fetchone("SELECT 1 FROM app_user WHERE user_id = %s",
                           (actor,))
    if row is None:
        raise LiveSweepError(
            "PRINCIPAL_NOT_PROVISIONED",
            f"User {actor!r} has no app_user row in PostgreSQL, so no job can "
            f"be enqueued as that principal (job.principal_user_id references "
            f"app_user). Provision the identity server-side first.",
            status=409)
    return actor


def _job_row_for(session: Session, *, kind: str, connection_id: str,
                 entity_id: str, principal: str, actor: str,
                 correlation_id: str | None) -> tuple[str, list[str]]:
    """The one `job` row this connection's `kind` runs on, creating it if
    absent. Returns ``(job_id, dead_job_ids)``.

    THE ROW IS REUSED, never re-enqueued per call: `jobs.reschedule` explains
    why -- the checkpoint (a poll's open window, the PO walk's cursor) lives
    on the row, and a fresh row per call would reset it every time. A DEAD
    row is left exactly as it is (a human's to revive) and a new row is
    started beside it; the dead ids are reported so that decision is visible.

    THE SELECT AND THE INSERT ARE ONE DECISION, so they are made under one
    lock (2026-09-12 review, item 3). Without it, two concurrent sweep ticks
    on the same connection -- a scheduled tick racing a manual trigger, or
    two workers both waking for the same due job -- could each run the
    SELECT below, each see no live row, and each call `enqueue_job`: a
    plain SELECT-then-INSERT has no way to see a concurrent transaction's
    uncommitted work. Two job rows for the same `(kind, connection_id)`
    then race on the single watermark row and on the PO-anchored walk's
    checkpoint. `pg_advisory_xact_lock(hashtext(...))`, keyed on this
    connection and kind, is the same primitive `budget.py` uses for the
    per-project first-revision race (see its comment there): transaction-
    scoped, so it releases on commit or rollback with no code path to
    forget, and it needs no row to lock, which matters here because the
    first call for a (kind, connection_id) has no row yet to lock. A second
    transaction blocks here until the first commits, and then its own
    SELECT sees the row the first transaction just created.

    `031_job_one_live_row_per_connection.sql` adds a partial UNIQUE index
    on `(kind, connection_id)` over the same non-terminal states as a
    second, database-level guarantee: even a caller that skipped this lock
    -- or skipped `_job_row_for` entirely -- cannot create a second live row.
    This lock is what keeps that from ever being reached in the first place,
    so `enqueue_job` here is never refused by it in ordinary operation.
    """
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"capex.job_row:{kind}:{connection_id}",))
    rows = repo.query(
        session,
        f"""
        SELECT job_id, state
        FROM {store.JOB}
        WHERE kind = %(kind)s
          AND connection_id = %(connection_id)s
          AND {{scope}}
        ORDER BY created_at
        """,
        {"kind": kind, "connection_id": connection_id},
        columns=store.JOB_SCOPE_COLUMNS,
    )
    dead = [r[0] for r in rows if r[1] == jobs.JOB_DEAD]
    live = [r[0] for r in rows if r[1] in jobs.CLAIMABLE_STATES]
    if live:
        return live[0], dead
    job_id = store.enqueue_job(
        session, job_id=f"JOB-{uuid.uuid4().hex}", kind=kind,
        principal_user_id=principal, actor=actor, entity_id=entity_id,
        connection_id=connection_id, correlation_id=correlation_id)
    return job_id, dead


def _build_job(module: str, *, adapter: Any, sweep_store: PgSweepStore,
               connection_id: str, max_pages: int) -> Any:
    if module == MODULE_CONTACTS:
        return sweeps.poll_contacts(adapter, sweep_store, connection_id,
                                    max_pages=max_pages)
    if module == MODULE_ITEMS:
        return sweeps.poll_items(adapter, sweep_store, connection_id,
                                 max_pages=max_pages)
    if module == MODULE_PURCHASE_ORDERS:
        return sweeps.poll_purchaseorders(adapter, sweep_store, connection_id,
                                          max_pages=max_pages)
    if module == MODULE_BILLS:
        return sweeps.poll_bills(adapter, sweep_store, connection_id,
                                 max_pages=max_pages)
    if module == MODULE_RECEIVES:
        return sweeps.SweepPoAnchored(
            adapter=adapter, store=sweep_store, connection_id=connection_id,
            external_source=sweep_store.external_source)
    raise LiveSweepError("UNKNOWN_SWEEP_MODULE", f"{module!r}", status=422)


#: The `integration_watermark.module` each request-facing module advances.
#: Receives have no watermark: the PO-anchored walk's cursor is a PO id, not
#: a modification time.
WATERMARK_MODULE: Mapping[str, str | None] = {
    MODULE_CONTACTS: sweeps.MODULE_CONTACTS,
    MODULE_ITEMS: sweeps.MODULE_ITEMS,
    MODULE_PURCHASE_ORDERS: sweeps.MODULE_PURCHASE_ORDERS,
    MODULE_BILLS: sweeps.MODULE_BILLS,
    MODULE_RECEIVES: None,
}

#: Which `integration_inbox.module` each request-facing module writes.
INBOX_MODULE: Mapping[str, str] = {
    MODULE_CONTACTS: sweeps.MODULE_CONTACTS,
    MODULE_ITEMS: sweeps.MODULE_ITEMS,
    MODULE_PURCHASE_ORDERS: sweeps.MODULE_PURCHASE_ORDERS,
    MODULE_BILLS: sweeps.MODULE_BILLS,
    MODULE_RECEIVES: sweeps.MODULE_PURCHASE_RECEIVES,
}


def run_inbound_sweep(session: Session, *, connection: Mapping[str, Any],
                      actor: str, correlation_id: str,
                      modules: Sequence[str] = DEFAULT_MODULES,
                      transport: Transport | None = None,
                      clock: jobs.Clock | None = None,
                      soft_deadline_seconds: int = ROUTE_SOFT_DEADLINE_SECONDS,
                      max_pages_per_poll: int = ROUTE_MAX_PAGES_PER_POLL) -> dict[str, Any]:
    """Run the inbound sweeps for one LIVE connection, through the framework.

    Refuses with ``CONNECTION_NOT_LIVE`` (409) for MOCK / SANDBOX and with
    ``LIVE_ADAPTER_UNAVAILABLE`` (503) for a product no live transport exists
    for. Every other outcome -- a page walked, a budget refused, a job that
    hit the deadline, a poll that failed on a Zoho error -- is REPORTED in the
    returned mapping rather than raised, because `run_job` records a failure
    on the job row and the event trail and the operator reads it there.

    Never writes to Zoho: the adapter issues GETs and the transport refuses
    anything else.
    """
    if not is_live(connection):
        raise LiveSweepError(
            "CONNECTION_NOT_LIVE",
            f"Connection {connection.get('connection_id')!r} is in mode "
            f"{connection.get('mode')!r}; only {list(store.LIVE_MODES)} reach "
            f"a tenant. Nothing was swept.", status=409)
    wanted = _require_modules(modules)
    external_source_for(connection)          # refuses an unsupported product
    adapter = adapter_for_connection(connection, transport=transport)
    if adapter is None:
        raise LiveSweepError(
            "LIVE_ADAPTER_UNAVAILABLE",
            f"No live adapter exists for product "
            f"{connection.get('product')!r} on data centre "
            f"{connection.get('dc')!r}: only Zoho ERP on IN has a transport "
            f"that can reach a tenant on this branch.", status=503)

    clock = clock or jobs.SystemClock()
    connection_id = str(connection["connection_id"])
    entity_id = str(connection["entity_id"])
    principal = _require_principal(session, actor)
    sweep_store = PgSweepStore(session=session, connection=connection,
                               actor=actor, status_api_version=_ERP_API_VERSION)
    job_store = ConnectionJobStore(session, connection_id=connection_id)
    budget = PollingBudget(session=session, connection_id=connection_id,
                           actor=actor)
    capabilities = adapter.capabilities()

    started_at = clock.now()
    deadline_at = started_at.timestamp() + soft_deadline_seconds
    results: dict[str, Any] = {}
    skipped: list[str] = []
    dead_jobs: dict[str, list[str]] = {}

    for module in wanted:
        remaining = int(deadline_at - clock.now().timestamp())
        if remaining <= 0:
            skipped.append(module)
            continue
        job = _build_job(module, adapter=adapter, sweep_store=sweep_store,
                         connection_id=connection_id,
                         max_pages=max_pages_per_poll)
        job_id, dead = _job_row_for(
            session, kind=job.kind, connection_id=connection_id,
            entity_id=entity_id, principal=principal, actor=actor,
            correlation_id=correlation_id)
        if dead:
            dead_jobs[module] = dead
        before_created = dict(sweep_store.created)
        before_dupes = dict(sweep_store.duplicates)
        run = jobs.run_job(
            job, store=job_store, clock=clock, connection_id=connection_id,
            correlation_id=correlation_id, capabilities=capabilities,
            budget=budget,
            soft_deadline_seconds=min(remaining, jobs.SOFT_DEADLINE_SECONDS))
        if run.finished:
            jobs.reschedule(job_store, run, clock=clock)
        inbox_module = INBOX_MODULE[module]
        results[module] = {
            "job_id": run.job_id or job_id,
            "kind": run.kind,
            "claimed": run.claimed,
            "state": run.state,
            "reason": run.reason,
            "error": run.error,
            "records_seen": run.units,
            "inbox_created": (sweep_store.created.get(inbox_module, 0)
                              - before_created.get(inbox_module, 0)),
            "inbox_duplicates": (sweep_store.duplicates.get(inbox_module, 0)
                                 - before_dupes.get(inbox_module, 0)),
            "pages": _pages_walked(run),
            "steps": run.steps,
            "calls": run.calls,
            "checkpoint": dict(run.checkpoint),
        }

    watermarks: dict[str, str | None] = {}
    for module in wanted:
        wm_module = WATERMARK_MODULE[module]
        if wm_module is None:
            continue
        watermarks[wm_module] = jobs.iso(sweep_store.get_watermark(
            connection_id=connection_id, module=wm_module))

    describe = getattr(transport or adapter.transport, "describe", None)
    return {
        "connection_id": connection_id,
        "mode": connection.get("mode"),
        "product": connection.get("product"),
        "external_source": sweep_store.external_source,
        "receives_strategy": receives_strategy(adapter),
        "modules": results,
        "skipped_for_deadline": skipped,
        "dead_jobs": dead_jobs,
        "watermarks": watermarks,
        "exceptions": {
            "raised": len(set(sweep_store.exceptions_raised)),
            "ids": sorted(set(sweep_store.exceptions_raised)),
        },
        "receive_lines_recorded": sweep_store.receive_lines_recorded,
        "budget": budget.describe(),
        "transport": describe() if callable(describe) else {},
        "correlation_id": correlation_id,
        "started_at": jobs.iso(started_at),
        "finished_at": jobs.iso(clock.now()),
    }


def _pages_walked(run: jobs.JobRun) -> int:
    """Pages a windowed poll walked this run, off its own checkpoint.

    A completed window checkpoints ``{"page": 1, "hwm": ...}`` with the page
    count in the final step's detail, which the runner keeps as
    ``last_step``; an interrupted one leaves ``page`` at the NEXT page to
    read. The PO-anchored sweep reports no pages at all.
    """
    checkpoint = run.checkpoint or {}
    if "last_po_id_swept" in checkpoint:
        return 0
    if checkpoint.get("window_until"):
        return max(0, int(checkpoint.get("page", 1)) - 1)
    return run.steps - 1 if run.steps else 0


__all__ = [
    "CLAIM_CONNECTION_JOB_SQL", "ConnectionJobStore", "DEFAULT_MODULES",
    "EXTERNAL_SOURCE_BY_PRODUCT", "INBOX_MODULE", "LiveSweepError",
    "PO_TERMINAL_STATUSES", "PgSweepStore", "PollingBudget",
    "ROUTE_MAX_PAGES_PER_POLL", "ROUTE_SOFT_DEADLINE_SECONDS",
    "WATERMARK_MODULE", "adapter_for_connection", "external_source_for",
    "is_live", "run_inbound_sweep",
]
