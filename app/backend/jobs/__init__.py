"""Background job handlers that are invoked, not resident.

There is no always-on process in this deployment. AppSail instances live five
minutes of total uptime, scale to zero, and answer a request in at most thirty
seconds (``docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`` section 2.1); the
only place batch work can run is a Catalyst Cron or Event Function, whose
ceiling is fifteen minutes. So nothing in this package starts a thread, opens a
loop, or schedules itself. Every module here exposes a **handler**: a plain
callable that does one bounded, checkpointed, idempotent slice of work and
returns a result, which a platform scheduler invokes and which a human can
invoke from ``tools/`` with the same arguments.

``app/backend/integration/jobs.py`` is the general runner for work that queues
on the ``job`` table. This package is for handlers that do not: see
``audit_anchor``'s module docstring for why the audit anchor is one of them.
"""
from __future__ import annotations

__all__ = ["audit_anchor"]
