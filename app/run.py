"""Start the CAPEX & WBS Control Hub POC.

    python app/run.py            # serve on http://127.0.0.1:8000
    python app/run.py --public   # bind 0.0.0.0 so a tunnel or host can reach it

Environment:
    X_ZOHO_CATALYST_LISTEN_PORT
                    Catalyst AppSail listen port (takes precedence)
    PORT            listen port outside Catalyst (default 8000)
    HOST            bind address (default 127.0.0.1, or 0.0.0.0 with --public)
    CAPEX_DB_PATH   where the SQLite file lives
    CAPEX_DB_URL / CAPEX_DB_HOST (+ CAPEX_DB_PASSWORD, ...)
                    PostgreSQL runtime configuration. Optional at this stage
                    of the port: unset, this process serves purely on SQLite
                    as before, and /readyz honestly reports "not configured"
                    rather than 200.
    DEMO_USER       READ ONLY, TO DECIDE WHETHER TO PRINT A WARNING BELOW.
    DEMO_PASSWORD   These gate NOTHING. No middleware, dependency or route
                    in `app/backend/` reads either name -- grep finds them
                    nowhere outside this file. They were documented for
                    three waves as an access control and never were one.

DEF-01
------
This process used to call the migration runner on every boot -- fresh() when
no database file existed, upgrade() otherwise -- unconditionally. A database
that predated the migration runner made upgrade() replay migration 001
against a schema that already had it, and the whole application failed to
start with ``table entity already exists``.

**An application must never migrate itself on boot.** It cannot be rolled
back, it races when scaled horizontally, and it turns a schema problem into
an outage. So this process now does the opposite: it only ever LOOKS at the
schema before serving, through :func:`check_sqlite_schema` and, when
PostgreSQL is configured, :func:`app.backend.pg.migrate_pg.assert_schema_current`
via :mod:`app.backend.pg.runtime`. Neither ever writes. If either finds the
schema behind, missing, or drifted, this process logs the exact command to
run and exits non-zero -- it does not try to fix what it found.

Migrations are a deploy step, run deliberately, by an operator or a deploy
pipeline:

    python -m app.backend.migrate --db <path> --fresh --seed   # first run
    python -m app.backend.migrate --db <path> --upgrade        # every run after
    python -m app.backend.pg.migrate_pg --upgrade               # PostgreSQL
"""
from __future__ import annotations

import atexit
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SchemaNotCurrent(RuntimeError):
    """Boot refuses to serve. The message names the exact command to run."""


def check_sqlite_schema(path: str) -> None:
    """Read-only. Raises :class:`SchemaNotCurrent` if the database at ``path``
    is missing, predates the migration runner, or has pending migrations.
    Never creates a table, never writes a byte -- the connection itself is
    opened in SQLite's read-only URI mode, so a write would fail at the
    driver level even if this function had a bug that attempted one.
    """
    from app.backend import migrate

    expected = [version for version, _, _ in migrate.discover()]
    if not expected:
        return  # no migrations defined at all; nothing to be behind on

    if not os.path.exists(path):
        raise SchemaNotCurrent(
            f"no database at {path}. Run: "
            f"python -m app.backend.migrate --db {path} --fresh --seed")

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migration" not in tables:
            # The v1 tables exist with no ledger at all -- DEF-01's exact
            # reproduction: a database built before the migration runner
            # existed. Refuse rather than let `upgrade()` replay migration
            # 001 against a schema that already has it.
            raise SchemaNotCurrent(
                f"{path} predates the migration runner (no schema_migration "
                f"ledger present). Adopt it explicitly: "
                f"python -m app.backend.migrate --db {path} --upgrade")
        applied = {row[0] for row in con.execute(
            "SELECT version FROM schema_migration")}
    finally:
        con.close()

    pending = [v for v in expected if v not in applied]
    if pending:
        raise SchemaNotCurrent(
            f"{path} is behind: {pending} not applied. Run: "
            f"python -m app.backend.migrate --db {path} --upgrade")


def _postgres_configured() -> bool:
    return bool(os.environ.get("CAPEX_DB_URL") or os.environ.get("CAPEX_DB_HOST"))


def check_postgres_schema() -> None:
    """Read-only. Builds the process-wide PostgreSQL runtime (if configured)
    and verifies its schema via ``app.backend.pg.migrate_pg.assert_schema_current``.
    Raises :class:`app.backend.pg.runtime.RuntimeStartupError` -- itself never
    carrying a DSN, host, user or raw driver message -- if the schema is
    behind, has drifted, or the database cannot be reached.

    A no-op when PostgreSQL is not configured: this milestone ships the
    PostgreSQL foundation alongside the existing SQLite-backed application,
    not yet as its replacement, so an unconfigured environment must keep
    booting on SQLite exactly as before. ``/readyz`` reports that state
    honestly (503, "not configured") rather than this process refusing to
    start over a database it was never asked to use.
    """
    if not _postgres_configured():
        return

    from app.backend.pg import runtime as pg_runtime

    pg_runtime.startup(check=True)
    atexit.register(pg_runtime.shutdown)


def check_database_ready() -> None:
    """Every read-only schema check this process performs before it will
    serve a single request. Raises on the first failure; writes nothing."""
    from app.backend import db

    check_sqlite_schema(db.DB_PATH)
    check_postgres_schema()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    from app.backend import auth, db

    if "--reseed" in argv:
        # This process no longer migrates itself -- see the module docstring.
        # --reseed used to call the migration runner directly; it is now a
        # pointer to the same command run as an explicit, deliberate step.
        print(
            "\n  This process no longer rebuilds the database itself. Run:\n"
            f"    python -m app.backend.migrate --db {db.DB_PATH} --fresh --seed\n"
            "  then start this process again.\n"
        )
        return 1

    try:
        check_database_ready()
    except SchemaNotCurrent as exc:
        print(f"\n  REFUSING TO START: {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # RuntimeStartupError, or anything check_* raised
        print(f"\n  REFUSING TO START: {exc}\n", file=sys.stderr)
        return 1

    # POC identities are deliberately provisioned separately from business
    # seed data. Idempotent (app.backend.auth.provision_dev_identities), and
    # only reached once the schema is confirmed current -- never against a
    # database this process just refused to serve.
    #
    # Fable 5.1: under CAPEX_PROFILE=uat-preview the derivable `<id>!demo`
    # scheme is NEVER installed. Credential hashes come from a file generated
    # outside the repository (tools/appsail/uat_credentials.py); a missing or
    # malformed file refuses the boot rather than falling back to demo
    # passwords on a publicly reachable address.
    con = db.connect()
    try:
        if auth.is_uat_profile():
            try:
                credentials = auth.load_uat_credentials(
                    os.environ.get(auth.UAT_CREDENTIALS_ENV, ""))
            except auth.UatCredentialsError as exc:
                print(f"\n  REFUSING TO START: {exc}\n", file=sys.stderr)
                return 1
            provisioned = auth.provision_uat_identities(con, credentials)
            print(f"  -> UAT identities ready: {provisioned} (demo passwords NOT installed)")
        else:
            provisioned = auth.provision_dev_identities(con)
            print(f"  -> demo identities ready: {provisioned}")
    finally:
        con.close()

    host = os.environ.get("HOST") or ("0.0.0.0" if "--public" in argv else "127.0.0.1")
    # AppSail assigns the listening port at runtime and verifies that the
    # process binds to it. Keep PORT as the portable/local fallback.
    port = int(
        os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT")
        or os.environ.get("PORT", "8000")
    )

    # The warning names the REAL risk. It used to say "set DEMO_USER and
    # DEMO_PASSWORD", which reads as a remedy and is not one: nothing in
    # `app/backend/` consults either variable, so setting them changes no
    # behaviour at all. Telling an operator to set them was worse than
    # saying nothing -- it left them believing the address was gated.
    if host != "127.0.0.1" and auth.is_uat_profile():
        print("\n  UAT preview profile: derivable demo passwords are NOT installed;")
        print("  identities come from", auth.UAT_CREDENTIALS_ENV, "(hashes only).")
        print("  Data is a synthetic seed on a local SQLite file and resets on restart.\n")
    elif host != "127.0.0.1":
        print("\n  WARNING: binding to", host, "-- beyond this machine.")
        print("  Every screen requires a sign-in, but in the local-demo")
        print("  profile the only accounts that exist are SEEDED DEVELOPMENT")
        print("  IDENTITIES whose passwords are the user id followed by")
        print("  '!demo'. The sign-in screen lists the user ids, so they are")
        print("  guessable by anyone who reaches it.")
        print("  DEMO_USER / DEMO_PASSWORD do NOT help: nothing reads them.")
        print("  Do not expose this address without a real access control.\n")

    import uvicorn
    print(f"CAPEX & WBS Control Hub -> http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    uvicorn.run("app.backend.main:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
