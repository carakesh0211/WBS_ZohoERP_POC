"""Local PostgreSQL demonstration of the principal workflow (Fable 5.1, brief G).

    python tools/demo_pg.py --create        # build a DISPOSABLE database and seed it
    python tools/demo_pg.py --serve         # start the application against it
    python tools/demo_pg.py --create --serve
    python tools/demo_pg.py --drop          # remove the disposable database

What this does, and what it refuses to do:

* It creates a database whose NAME matches the seed guard's disposable
  pattern (`capex_tmpl_demo_<suffix>`), migrates it with the product's own
  runner (`app.backend.pg.migrate_pg --upgrade`) and seeds it under
  `CAPEX_PROFILE=local-demo` through `app.backend.pg.seed`, which refuses any
  database that is not provably disposable. It never touches an existing
  database: `--create` refuses if the name already exists unless `--fresh`
  is given, and `--drop` refuses any name outside the pattern.
* It builds the SQLite shell database the legacy screens still read, with the
  same `migrate --fresh --seed` step, in a scratch path -- the two are one
  application at this stage of the port.
* It starts `app/run.py` with BOTH configured. Startup verifies both schemas
  read-only and refuses to serve if either is behind (DEF-01); nothing here
  migrates on boot.

Demo identities (password = user id + "!demo", local-demo profile ONLY --
never on a hosted address):

    U-REQ  Requestor                 creates and submits the original budget
    U-FIN  Finance                   approves it (a different identity)
    U-PFC  Project Finance Controller (alternate approver, ENT-DM1 only)
    U-ADM  Administrator             budget categories, settings
    U-AUD  Internal Auditor          audit trail (read-only)

Walk-through (brief G): sign in as U-REQ -> Project Control -> Budget Setup
-> New Budget -> pick project CAPEX-2026-001, two or more lines with SEPARATE
Category and Budget Head -> Save draft -> Submit. Sign in as U-FIN -> My
Approval Inbox -> approve. Budget Planning Grid (cells) shows the released
cells; filter by Category and by Budget Head independently; Executive
Dashboard totals reconcile; Budget Revisions creates a supplement without
touching the original; Audit Trail shows every step.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADMIN_URL = "postgresql://capex:capex@127.0.0.1:5432/postgres"
NAME_RE = re.compile(r"^capex_tmpl_[a-z0-9_]+$")


def _admin_url() -> str:
    return os.environ.get("CAPEX_DEMO_ADMIN_URL", DEFAULT_ADMIN_URL)


def _db_url(name: str) -> str:
    parsed = urllib.parse.urlsplit(_admin_url())
    return urllib.parse.urlunsplit(parsed._replace(path="/" + name))


def _exists(name: str) -> bool:
    import psycopg
    with psycopg.connect(_admin_url(), autocommit=True) as con:
        return con.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def _run(cmd: list[str], env: dict[str, str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), env=env, check=True)


def create(name: str, *, fresh: bool, sqlite_path: Path) -> None:
    import psycopg
    if not NAME_RE.match(name):
        raise SystemExit(f"refusing: {name!r} is not a disposable demo name (capex_tmpl_...)")
    if _exists(name):
        if not fresh:
            raise SystemExit(f"{name} already exists; pass --fresh to rebuild it (it is disposable)")
        with psycopg.connect(_admin_url(), autocommit=True) as con:
            con.execute(f'DROP DATABASE "{name}"')
    with psycopg.connect(_admin_url(), autocommit=True) as con:
        con.execute(f'CREATE DATABASE "{name}"')
    env = dict(os.environ, CAPEX_DB_URL=_db_url(name), CAPEX_DB_SSLMODE="disable",
               CAPEX_PROFILE="local-demo")
    _run([sys.executable, "-m", "app.backend.pg.migrate_pg", "--upgrade"], env)
    _run([sys.executable, "-m", "app.backend.pg.seed"], env)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_path.exists():
        sqlite_path.unlink()
    _run([sys.executable, "-m", "app.backend.migrate", "--db", str(sqlite_path), "--fresh", "--seed"],
         dict(env, CAPEX_DB_PATH=str(sqlite_path)))
    print(f"\nready: PostgreSQL {name} migrated+seeded; SQLite shell at {sqlite_path}")


def serve(name: str, *, sqlite_path: Path, port: int) -> None:
    if not _exists(name):
        raise SystemExit(f"{name} does not exist; run with --create first")
    env = dict(os.environ, CAPEX_DB_URL=_db_url(name), CAPEX_DB_SSLMODE="disable",
               CAPEX_PROFILE="local-demo", CAPEX_DB_PATH=str(sqlite_path), PORT=str(port),
               HOST="127.0.0.1")
    print(f"\nserving http://127.0.0.1:{port}  (PostgreSQL {name}; local-demo identities; Ctrl+C to stop)")
    _run([sys.executable, "app/run.py"], env)


def drop(name: str) -> None:
    import psycopg
    if not NAME_RE.match(name):
        raise SystemExit(f"refusing to drop {name!r}: not a disposable demo name")
    with psycopg.connect(_admin_url(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{name}"')
    print(f"dropped {name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", default="capex_tmpl_demo_fable51")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="with --create: rebuild if it exists")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--sqlite", default=str(ROOT / "app" / "data" / "capex_demo_pg.db"))
    args = ap.parse_args(argv)
    sqlite_path = Path(args.sqlite)
    if args.drop:
        drop(args.name)
        return 0
    if args.create:
        create(args.name, fresh=args.fresh, sqlite_path=sqlite_path)
    if args.serve:
        serve(args.name, sqlite_path=sqlite_path, port=args.port)
    if not (args.create or args.serve):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
