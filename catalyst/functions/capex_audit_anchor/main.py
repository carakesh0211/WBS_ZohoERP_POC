"""Catalyst Cron Function: the daily audit anchor (Fable 5.1).

Scheduled by Catalyst Job Scheduling (`capex-audit-anchor-daily`, 02:15 UTC,
see docs/runbooks/audit-anchor.md). This file is a THIN shim: it calls the
same entry point the operator CLI calls,
``app.backend.jobs.anchor_entry.run_from_environment``, and reports the
outcome to Catalyst. The handler owns nothing the CLI does not.

Runtime contract (Python Cron Function, 15-minute ceiling):

    handler(cron_details, context)
        context.close_with_success() / context.close_with_failure()

* Credentials come from the function's environment BY NAME:
  ``CAPEX_ANCHOR_JOB_TOKEN`` and the ``CAPEX_DB_*`` set. They are never
  logged and never returned; the only thing returned or logged is
  ``AnchorJobRun.as_dict()``, which the job redacts.
* A PARTIAL run's checkpoint is written to the function log as JSON so the
  next tick can resume by passing it back as ``cron_details``' `checkpoint`
  parameter (Job Scheduling lets a cron carry request parameters).
* Exit outcomes map to Catalyst as: SUCCEEDED -> success; PARTIAL,
  FAILED_RETRYABLE -> failure (retry); TAMPER_DETECTED -> failure (do NOT
  simply retry: the database is the problem -- investigate);
  FAILED_PERMANENT -> failure (configuration; retrying is noise).

The application tree (`app/`, `migrations/`, `research/`) and the vendored
dependency closure (`vendor/`) are placed beside this file by
tools/appsail/build_anchor_function.py; Catalyst installs nothing.
"""
from __future__ import annotations

import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "vendor"), HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger("capex.anchor_function")

_SUCCESS_OUTCOMES = frozenset({"SUCCEEDED"})


def _checkpoint_from(cron_details) -> dict | None:
    """The previous PARTIAL run's checkpoint, if the schedule carries one."""
    getter = getattr(cron_details, "get_all_cron_params", None) or getattr(cron_details, "get_cron_param", None)
    params = None
    try:
        params = getter() if callable(getter) else None
    except Exception:  # noqa: BLE001 - a malformed parameter must not stop the day being anchored
        params = None
    if isinstance(params, dict):
        raw = params.get("checkpoint")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
    return None


def handler(cron_details, context):
    from app.backend.jobs.anchor_entry import run_from_environment

    try:
        run = run_from_environment(checkpoint=_checkpoint_from(cron_details))
    except Exception as exc:  # noqa: BLE001 - the class name is logged, never the message (a driver message can echo the DSN)
        log.error("audit anchor job raised %s; refusing to guess an outcome", type(exc).__name__)
        context.close_with_failure()
        return {"job": "audit_anchor", "outcome": "FAILED_RETRYABLE", "error_class": type(exc).__name__}

    result = run.as_dict()
    if run.outcome == "PARTIAL":
        result["checkpoint"] = run.checkpoint()
    log.info("audit anchor %s", json.dumps(result, default=str))
    if run.outcome in _SUCCESS_OUTCOMES:
        context.close_with_success()
    else:
        context.close_with_failure()
    return result
