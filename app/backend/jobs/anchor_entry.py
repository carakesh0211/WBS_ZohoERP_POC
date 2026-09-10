"""The ONE entry point every scheduler uses to run the daily audit anchor.

``tools/anchor_job.py`` (operator CLI) and
``catalyst/functions/capex_audit_anchor/main.py`` (Catalyst Cron Function)
both call :func:`run_from_environment`; neither builds a database or reads a
credential on its own, so the two cannot drift -- which is the failure this
module exists to prevent: the writer shipped correct and called by nothing
for a whole wave (`docs/runbooks/audit-anchor.md`).

Credentials are read from the environment BY NAME
(``CAPEX_ANCHOR_JOB_TOKEN`` and the ``CAPEX_DB_*`` set the application itself
uses). Nothing here accepts a token as an argument, logs one, or returns one:
the result is ``AnchorJobRun.as_dict()``, which the job already redacts.
"""
from __future__ import annotations

from typing import Any, Mapping

from .audit_anchor import ANCHOR_JOB_TOKEN_SECRET, AnchorJobRun, run_anchor_job


def run_from_environment(checkpoint: Mapping[str, Any] | None = None) -> AnchorJobRun:
    """Build the database from ``CAPEX_DB_*``, read the job token by name,
    run both phases (resuming from ``checkpoint`` when given), close the pool.
    Never migrates, never writes outside the anchor tables."""
    from ..pg import config as pg_config
    from ..pg.engine import Database

    secrets = pg_config.EnvSecretProvider()
    database = Database(pg_config.from_env(), secret_provider=secrets)
    try:
        return run_anchor_job(
            database,
            credential=secrets.get(ANCHOR_JOB_TOKEN_SECRET),
            secrets=secrets,
            checkpoint=dict(checkpoint) if isinstance(checkpoint, Mapping) else None,
        )
    finally:
        database.close()
