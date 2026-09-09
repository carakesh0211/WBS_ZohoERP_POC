"""Invoke the daily audit-anchor job. The entry point a scheduler calls.

    python tools/anchor_job.py                       # anchor today, then verify
    python tools/anchor_job.py --json run.json       # ...and write the result
    python tools/anchor_job.py --verify-only         # verify, write nothing
    python tools/anchor_job.py --resume-from run.json  # skip completed phases

WHY THIS FILE EXISTS
--------------------
``pg/audit.write_anchor`` shipped correct, tested, and called by nothing. There
was no route (deliberately -- ``api/audit.py`` explains why), no CLI, and no
schedule, so ``audit_anchor`` stayed empty in every deployment and whole-stream
truncation stayed undetectable. This is one of the two callers that make the
control operative; the other is a Catalyst Cron Function, which invokes the
same handler with the same arguments.

CREDENTIALS
-----------
The job token is read from the environment by NAME -- ``CAPEX_ANCHOR_JOB_TOKEN``
-- and never from an argument. A token passed on a command line is in the shell
history, in ``ps``, and in the CI log that echoed the command. There is no
``--token`` flag and adding one would be a regression.

The database is configured exactly as the application configures it
(``app.backend.pg.config.from_env``); this tool holds no connection string of
its own.

EXIT CODES -- distinct per outcome, because a scheduler that can only see
"non-zero" cannot tell a database that needs investigating from one that needs
a retry::

    0  SUCCEEDED          today is anchored and the anchors verify
    2  usage error        (argparse)
    3  PARTIAL            anchored; verification did not finish -- invoke again
    4  TAMPER_DETECTED    the log does not match the anchors -- INVESTIGATE
    5  FAILED_RETRYABLE   transient; invoking again is the fix
    6  FAILED_PERMANENT   bad or missing credential, or a refused request

Verification status
-------------------
UNVERIFIED against a live database on the authoring machine: there is no
PostgreSQL here. The argument parsing, exit-code mapping and the refusal to
accept a token on the command line are exercised in
``tests/test_pg_anchor_job.py`` without one; everything that touches a database
first runs in CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.jobs.audit_anchor import (  # noqa: E402
    ANCHOR_JOB_TOKEN_SECRET, EXIT_CODES, OUTCOME_FAILED_PERMANENT,
    AnchorJobRun, run_anchor_job,
)

#: argparse's own failure code. Listed so the table above is complete and so a
#: caller mapping exit codes cannot mistake a typo for a job outcome.
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anchor_job",
        description=("Write today's audit anchor and verify the anchor set. "
                     "The job token is read from the environment variable "
                     f"{ANCHOR_JOB_TOKEN_SECRET}; there is deliberately no "
                     "flag to pass it on the command line."))
    parser.add_argument("--json", dest="json_path", metavar="PATH",
                        help="write the run result to PATH as JSON. Contains "
                             "no credential, no audit payload and no full hash.")
    # THERE IS NO --verify-only, AND THAT IS DELIBERATE.
    #
    # It would be a mode in which the scheduled invocation writes no anchor
    # while still exiting 0 -- one flag in one crontab away from the exact
    # state this whole stream exists to end, with a green job reporting it.
    # Read-only verification already has a caller that cannot make that
    # mistake: `GET /api/audit/anchors/verify`, guarded by `audit.read`.
    parser.add_argument("--resume-from", dest="resume_from", metavar="PATH",
                        help="a previous run's JSON. Phases it completed for "
                             "TODAY are skipped; a checkpoint from another "
                             "date is discarded rather than trusted.")
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing on stdout; report by exit code.")
    return parser


def _load_checkpoint(path: str) -> dict[str, Any] | None:
    """A previous run's checkpoint, or None if the file says nothing useful.

    Tolerant on purpose. A missing or malformed resume file must not stop the
    day being anchored -- both phases are idempotent, so the cost of ignoring
    a checkpoint is repeated work, and the cost of refusing to run is a day
    with no evidence.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checkpoint = payload.get("checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


def _run(args: argparse.Namespace) -> AnchorJobRun:
    # Imported inside the function so `--help` and the parser tests work on a
    # machine with no database configured at all.
    from app.backend.pg import config as pg_config
    from app.backend.pg.engine import Database

    secrets = pg_config.EnvSecretProvider()
    database_config = pg_config.from_env()
    database = Database(database_config, secret_provider=secrets)
    try:
        # ONE call, one set of arguments. The Catalyst Cron Function invokes
        # exactly this handler with exactly these arguments; a second way to
        # reach the writer is how one of them ends up unguarded.
        return run_anchor_job(
            database,
            credential=secrets.get(ANCHOR_JOB_TOKEN_SECRET),
            secrets=secrets,
            checkpoint=(_load_checkpoint(args.resume_from)
                        if args.resume_from else None),
        )
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run = _run(args)
    except Exception as exc:  # noqa: BLE001
        # Configuration faults reaching this far -- no DSN, no pool -- are
        # permanent, and are reported by CLASS. A libpq message echoes host,
        # user and database; `engine.Database.readiness` refuses to print one
        # for the same reason.
        run = AnchorJobRun(outcome=OUTCOME_FAILED_PERMANENT,
                           error_class=type(exc).__name__,
                           reason="The anchor job could not start. See "
                                  "docs/runbooks/audit-anchor.md")
    if not args.quiet:
        print(json.dumps(run.as_dict(), indent=2, sort_keys=True))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(run.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return EXIT_CODES[run.outcome]


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
