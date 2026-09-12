"""`POST /api/integrations/connections/{connection_id}/sweep` (Fable 5.1).

Runs the inbound sweeps for ONE live connection, server-side, through
:func:`app.backend.integration.live_sweep.run_inbound_sweep` -- the existing
`sweeps.py` jobs on the existing `jobs.py` runner, persisted through
`pg/integration_store.py`, reaching the tenant through `live_transport.py`.

A SEPARATE ROUTER, deliberately. `api/integrations.py` is being edited
concurrently (organisations / validate / health) and this route must not
land in that file. It carries the SAME guard: `require_integration_access`
(`connector.read`) at router level and `connector.manage` on the route,
imported from `integrations.py` rather than restated, so the two routers
cannot disagree about what "an integration operator" means.

THE ORDER OF THE REFUSALS IS THE CONTRACT
  1. no PostgreSQL         -> 503 DATABASE_NOT_CONFIGURED (the dependency)
  2. invisible connection  -> 404 CONNECTION_NOT_FOUND, for BOTH "no such
                              row" and "out of scope" (existence oracle rule)
  3. not a live mode       -> 409 CONNECTION_NOT_LIVE, BEFORE a transport is
                              built, so a MOCK connection never causes a
                              credential to be read
  4. no live transport     -> 503 LIVE_TRANSPORT_UNAVAILABLE (no credential
                              file / environment; a Books product)
  5. Zoho refused or failed-> 502 ZOHO_API_ERROR / 429 RATE_BUDGET_EXHAUSTED

Read-only: the transport refuses every non-GET while
``CAPEX_ERP_OUTBOUND_WRITES`` is unset, and nothing here sets it.

THE TWO WRITE-SIDE ROUTES (2026-09-12, the owner's controlled outbound
authorisation): ``/mode`` moves a connection between modes with the
authorisation section 11.9 requires recorded on the row, and refuses
LIVE_WRITE while the gate is shut; ``/drain-outbox`` sends the connection's
claimable outbox rows through the live adapter, and is refused unless the
gate is open AND the connection is LIVE_WRITE. Neither sets the gate: that is
platform configuration, outside this process.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import zoho
from ..integration import adoption, live_sweep
from ..integration import outbound as ob
from ..integration.adapter import (CapabilityError, IntegrationError,
                                   NetworkForbidden, UnsupportedDataCentre)
from ..pg import integration_store as store
from ..pg import procurement_services as procurement_svc
from ..pg.engine import Database
from . import integrations as _integrations

# The helpers are REUSED by module attribute, not copied, and not bound at
# import time either: `tests/test_integrations_api_guard.py` monkeypatches
# `integrations._require_visible_connection` to run the guard without a
# database, and a name bound here at import would silently escape that.
router = APIRouter(
    dependencies=[Depends(_integrations.require_integration_access)])


class _SweepIn(BaseModel):
    """`{modules?: [...]}`. Absent means all five, in canonical order."""

    model_config = ConfigDict(extra="forbid")
    modules: list[str] | None = None


@router.post("/api/integrations/connections/{connection_id}/sweep",
             dependencies=[Depends(_integrations._requires("connector.manage"))])
def run_sweep(
    connection_id: str, response: Response, request: Request,
    body: _SweepIn | None = None,
    database: Database = Depends(_integrations._get_database),
) -> dict[str, Any]:
    """Run the inbound sweeps for this LIVE connection now, and report.

    The response is `run_inbound_sweep`'s mapping verbatim: per-module job
    state and counts, the watermarks after, the exceptions raised, the
    budget verdict and the transport's own `describe()` (which carries no
    secret). A job that ran out of time or budget reports CHECKPOINTED with
    its reason; calling again resumes it from its checkpoint.
    """
    correlation_id = _integrations._set_correlation_header(response, request)
    connection = _integrations._require_visible_connection(
        request, database, connection_id)
    if not live_sweep.is_live(connection):
        raise _integrations._problem(
            409, "CONNECTION_NOT_LIVE", "Connection Not Live",
            f"Connection {connection_id} is in mode "
            f"{connection.get('mode')!r}; a sweep reaches the tenant and only "
            f"{list(store.LIVE_MODES)} may. Nothing was swept.")
    modules = (body.modules if body is not None and body.modules
               else list(live_sweep.DEFAULT_MODULES))
    try:
        live_sweep._require_modules(modules)
        # The transport is built BEFORE a transaction is opened: reading the
        # credential and minting nothing is cheap, but a refusal here must
        # not hold a `SET LOCAL statement_timeout` transaction while it
        # happens, and a connection with no live adapter answers a coded 503
        # without ever touching the database.
        adapter = live_sweep.adapter_for_connection(connection)
        if adapter is None:
            raise live_sweep.LiveSweepError(
                "LIVE_ADAPTER_UNAVAILABLE",
                f"No live adapter exists for product "
                f"{connection.get('product')!r} on data centre "
                f"{connection.get('dc')!r}: only Zoho ERP on IN has a "
                f"transport that can reach a tenant on this branch.",
                status=503)
        with _integrations._session(request, database) as session:
            return live_sweep.run_inbound_sweep(
                session, connection=connection,
                actor=_integrations._actor(request),
                correlation_id=correlation_id, modules=modules,
                transport=adapter.transport)
    except store.IntegrationStoreError as exc:
        raise _integrations._store_error_to_http(exc)
    except UnsupportedDataCentre as exc:
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except NetworkForbidden as exc:
        # The write gate, or the no-network default. Either way nothing left.
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except CapabilityError as exc:
        raise _integrations._problem(
            409, "SCOPE_NOT_GRANTED", "Scope Not Granted", str(exc))
    except IntegrationError as exc:
        # `live_transport.RateBudgetExhausted` and `ZohoApiError` are both
        # IntegrationErrors; the class name is the discriminator the
        # transport gives us, and the message never carries a token.
        name = type(exc).__name__
        if name == "RateBudgetExhausted":
            raise _integrations._problem(
                429, "RATE_BUDGET_EXHAUSTED", "Rate Budget Exhausted", str(exc))
        if name == "ZohoApiError":
            raise _integrations._problem(
                502, "ZOHO_API_ERROR", "Zoho Api Error", str(exc),
                extra={"zoho_status": getattr(exc, "status", None),
                       "zoho_code": getattr(exc, "zoho_code", None)})
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))


class _AdoptIn(BaseModel):
    """`{limit?: int}`. Absent means `adoption.adopt_tenant_orders`'s own
    default (50)."""

    model_config = ConfigDict(extra="forbid")
    limit: int | None = None


@router.post("/api/integrations/connections/{connection_id}/adopt-orders",
             dependencies=[Depends(_integrations._requires("connector.manage"))])
def run_adopt_orders(
    connection_id: str, response: Response, request: Request,
    body: _AdoptIn | None = None,
    database: Database = Depends(_integrations._get_database),
) -> dict[str, Any]:
    """Adopt this LIVE connection's tenant-raised purchase orders now.

    `docs/fable51/ORDER_ADOPTION.md` has the rule this route enacts. THE
    ORDER OF THE REFUSALS is the same as `run_sweep`'s, with one more
    inserted after "is it live": (1) no PostgreSQL, (2) invisible connection
    -> 404, (3) not live -> 409 `CONNECTION_NOT_LIVE`, (4) no live transport
    -> 503 `LIVE_TRANSPORT_UNAVAILABLE`, (5) not the demo organisation on ERP
    -> 403 `ADOPTION_NOT_AUTHORISED_FOR_ORGANISATION`, (6) Zoho refused or
    failed -> 502 / 429. Read-only: every call this route can reach is a GET
    (`adapter.get_purchase_order`); nothing here writes to Zoho.

    The response is `adoption.adopt_tenant_orders`'s summary verbatim:
    ``adopted``, ``skipped``, ``exceptions``, ``calls`` and the raised
    exceptions' ids.
    """
    correlation_id = _integrations._set_correlation_header(response, request)
    connection = _integrations._require_visible_connection(
        request, database, connection_id)
    if not live_sweep.is_live(connection):
        raise _integrations._problem(
            409, "CONNECTION_NOT_LIVE", "Connection Not Live",
            f"Connection {connection_id} is in mode "
            f"{connection.get('mode')!r}; adoption reaches the tenant and "
            f"only {list(store.LIVE_MODES)} may. Nothing was adopted.")
    limit = body.limit if body is not None and body.limit is not None else 50
    try:
        adapter = live_sweep.adapter_for_connection(connection)
        if adapter is None:
            raise live_sweep.LiveSweepError(
                "LIVE_ADAPTER_UNAVAILABLE",
                f"No live adapter exists for product "
                f"{connection.get('product')!r} on data centre "
                f"{connection.get('dc')!r}: only Zoho ERP on IN has a "
                f"transport that can reach a tenant on this branch.",
                status=503)
        with _integrations._session(request, database) as session:
            return adoption.adopt_tenant_orders(
                session, connection=connection, adapter=adapter,
                actor=_integrations._actor(request),
                correlation_id=correlation_id, limit=limit)
    except store.IntegrationStoreError as exc:
        raise _integrations._store_error_to_http(exc)
    except UnsupportedDataCentre as exc:
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except NetworkForbidden as exc:
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except CapabilityError as exc:
        raise _integrations._problem(
            409, "SCOPE_NOT_GRANTED", "Scope Not Granted", str(exc))
    except IntegrationError as exc:
        name = type(exc).__name__
        if name == "RateBudgetExhausted":
            raise _integrations._problem(
                429, "RATE_BUDGET_EXHAUSTED", "Rate Budget Exhausted", str(exc))
        if name == "ZohoApiError":
            raise _integrations._problem(
                502, "ZOHO_API_ERROR", "Zoho Api Error", str(exc),
                extra={"zoho_status": getattr(exc, "status", None),
                       "zoho_code": getattr(exc, "zoho_code", None)})
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))


# ================================================================= mode
class _ModeIn(BaseModel):
    """`{mode, authorised_by?, note?}`. A live mode needs both the name of
    the person who authorised it and their note (section 11.9); the store
    refuses a live mode without them and the schema's own check constraint
    refuses it again."""

    model_config = ConfigDict(extra="forbid")
    mode: str
    authorised_by: str | None = None
    note: str | None = None


@router.post("/api/integrations/connections/{connection_id}/mode",
             dependencies=[Depends(_integrations._requires("connector.manage"))])
def set_mode(
    connection_id: str, body: _ModeIn, response: Response, request: Request,
    database: Database = Depends(_integrations._get_database),
) -> dict[str, Any]:
    """Move this connection between MOCK / SANDBOX / LIVE_READ / LIVE_WRITE.

    LIVE_WRITE is refused with 409 ERP_WRITES_DISABLED while
    ``CAPEX_ERP_OUTBOUND_WRITES`` is not 1: the platform gate and the row's
    mode are two separate controls and both must say yes before a byte is
    written to a tenant, so a mode change cannot be the thing that opens the
    write path on its own. Every change is recorded on the correlation trail
    with the mode it came from and went to.
    """
    correlation_id = _integrations._set_correlation_header(response, request)
    connection = _integrations._require_visible_connection(
        request, database, connection_id)
    mode = str(body.mode or "").strip().upper()
    if mode == "LIVE_WRITE" and not zoho.outbound_writes_enabled():
        raise _integrations._problem(
            409, "ERP_WRITES_DISABLED", "ERP Writes Disabled",
            "Outbound ERP writes are disabled in this deployment "
            "(CAPEX_ERP_OUTBOUND_WRITES is not 1), so the connection cannot be "
            "placed in LIVE_WRITE. The mode was not changed.")
    actor = _integrations._actor(request)
    try:
        with _integrations._session(request, database) as session:
            before = str(connection.get("mode"))
            row = store.set_connection_mode(
                session, connection_id=connection_id, mode=mode, actor=actor,
                live_authorised_by=body.authorised_by,
                live_authorisation_note=body.note)
            store.record_event(
                session, kind="CONNECTION_MODE_CHANGED", actor=actor,
                connection_id=connection_id, correlation_id=correlation_id,
                detail={"from": before, "to": row.get("mode"),
                        "authorised_by": body.authorised_by,
                        "outbound_writes_enabled": zoho.outbound_writes_enabled()})
            return {"connection_id": connection_id, "mode_before": before,
                    "mode": row.get("mode"), "version_no": row.get("version_no"),
                    "outbound_writes_enabled": zoho.outbound_writes_enabled()}
    except store.IntegrationStoreError as exc:
        raise _integrations._store_error_to_http(exc)


# ========================================================= drain the outbox
class _DrainIn(BaseModel):
    """`{limit?: int}` -- at most this many rows this call (default and
    ceiling 25, section 2.2's `drain_outbox` batch)."""

    model_config = ConfigDict(extra="forbid")
    limit: int | None = None


def _outbound_writes_open() -> None:
    """409 ERP_WRITES_DISABLED before any lookup, as the emit route does."""
    if not zoho.outbound_writes_enabled():
        raise _integrations._problem(
            409, "ERP_WRITES_DISABLED", "ERP Writes Disabled",
            "Outbound ERP writes are disabled in this deployment "
            "(CAPEX_ERP_OUTBOUND_WRITES is not 1). Nothing was sent.")


@router.post("/api/integrations/connections/{connection_id}/drain-outbox",
             dependencies=[Depends(_integrations._requires("connector.manage")),
                           Depends(_outbound_writes_open)])
def drain_outbox(
    connection_id: str, response: Response, request: Request,
    body: _DrainIn | None = None,
    database: Database = Depends(_integrations._get_database),
) -> dict[str, Any]:
    """Send this LIVE_WRITE connection's claimable outbox rows now, and report.

    THE ORDER OF THE REFUSALS: (0) gate shut -> 409 ERP_WRITES_DISABLED (the
    dependency, before any lookup); (1) no PostgreSQL -> 503; (2) invisible
    connection -> 404; (3) not LIVE_WRITE -> 409 CONNECTION_NOT_LIVE_WRITE
    (LIVE_READ is a read-only promise and this route keeps it); (4) no live
    adapter -> 503; (5) the tenant's answer per row, REPORTED and never
    raised, through `procurement_services.send_purchase_order`, so a failed
    row's attempt is recorded rather than rolled back.

    A sent row stamps the local order's `external_id` and appends the
    `PO_EMITTED` audit entry in the same transaction as the outbox row's
    SENT. A second call finds nothing claimable and sends nothing: the
    identical-retry proof is this route returning `sent: 0`.
    """
    correlation_id = _integrations._set_correlation_header(response, request)
    connection = _integrations._require_visible_connection(
        request, database, connection_id)
    if str(connection.get("mode") or "") != "LIVE_WRITE":
        raise _integrations._problem(
            409, "CONNECTION_NOT_LIVE_WRITE", "Connection Not Live Write",
            f"Connection {connection_id} is in mode "
            f"{connection.get('mode')!r}; draining the outbox writes to the "
            f"tenant and only LIVE_WRITE may. Nothing was sent.")
    limit = body.limit if body is not None and body.limit is not None else 25
    limit = max(1, min(int(limit), 25))
    actor = _integrations._actor(request)
    try:
        adapter = live_sweep.adapter_for_connection(connection)
        if adapter is None:
            raise live_sweep.LiveSweepError(
                "LIVE_ADAPTER_UNAVAILABLE",
                f"No live adapter exists for product "
                f"{connection.get('product')!r} on data centre "
                f"{connection.get('dc')!r}: only Zoho ERP on IN has a "
                f"transport that can reach a tenant on this branch.",
                status=503)
        external_source = live_sweep.external_source_for(connection)
        results: list[dict[str, Any]] = []
        with _integrations._session(request, database) as session:
            claimable = store.claim_outbox_batch(
                session, connection_id=connection_id, limit=limit)
            for row in claimable:
                outcome = procurement_svc.send_purchase_order(
                    session, outbox_id=row["outbox_id"],
                    connection_id=connection_id, adapter=adapter, actor=actor)
                if outcome.get("sent") and row.get("module") == procurement_svc.PO_MODULE:
                    # A per-cell row (D-7 False) is `po_id#wbs#head`; the
                    # order it belongs to is the part before the first `#`.
                    outcome["local"] = procurement_svc.record_po_emitted(
                        session, po_id=ob.purchase_order_id_of(row["local_id"]),
                        external_source=external_source,
                        external_id=str(outcome.get("external_id")),
                        outbox_id=row["outbox_id"], dedupe_key=row["dedupe_key"],
                        actor=actor, created=bool(outcome.get("created")),
                        correlation_id=correlation_id)
                outcome["local_id"] = row["local_id"]
                results.append(outcome)
            store.record_event(
                session, kind="OUTBOX_DRAINED", actor=actor,
                connection_id=connection_id, correlation_id=correlation_id,
                module=procurement_svc.PO_MODULE,
                detail={"claimed": len(claimable),
                        "sent": sum(1 for r in results if r.get("sent")),
                        "outbox_ids": [r["outbox_id"] for r in claimable]})
        transport = getattr(adapter, "transport", None)
        return {"connection_id": connection_id, "claimed": len(claimable),
                "sent": sum(1 for r in results if r.get("sent")),
                "results": results,
                # `describe()` carries counts and never a secret.
                "transport": (transport.describe()
                              if transport is not None and hasattr(transport, "describe")
                              else None)}
    except store.IntegrationStoreError as exc:
        raise _integrations._store_error_to_http(exc)
    except procurement_svc.ProcurementError as exc:
        raise _integrations._problem(
            getattr(exc, "status", 422) or 422, exc.code, exc.code.replace("_", " ").title(),
            str(exc))
    except UnsupportedDataCentre as exc:
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except NetworkForbidden as exc:
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
    except CapabilityError as exc:
        raise _integrations._problem(
            409, "SCOPE_NOT_GRANTED", "Scope Not Granted", str(exc))
    except IntegrationError as exc:
        name = type(exc).__name__
        if name == "RateBudgetExhausted":
            raise _integrations._problem(
                429, "RATE_BUDGET_EXHAUSTED", "Rate Budget Exhausted", str(exc))
        if name == "ZohoApiError":
            raise _integrations._problem(
                502, "ZOHO_API_ERROR", "Zoho Api Error", str(exc),
                extra={"zoho_status": getattr(exc, "status", None),
                       "zoho_code": getattr(exc, "zoho_code", None)})
        raise _integrations._problem(
            503, "LIVE_TRANSPORT_UNAVAILABLE", "Live Transport Unavailable",
            str(exc))
