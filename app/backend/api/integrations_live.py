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
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from ..integration import live_sweep
from ..integration.adapter import (CapabilityError, IntegrationError,
                                   NetworkForbidden, UnsupportedDataCentre)
from ..pg import integration_store as store
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
