"""Audit (READ ONLY) of the stray schema in the UAT project's `postgres` database.

    python tools/uat/stray_schema_audit.py            # audit, writes the evidence JSON
    python tools/uat/stray_schema_audit.py --backup   # audit + pg_dump of that schema

Context (docs/fable51/STAGE_B_PERSISTENT_UAT.md, "Incident 2026-09-12"): a
migration run fed a stale db.env built the app's 30-migration schema in the
provider's default `postgres` database instead of `capex_tmpl_uat`. This tool
proves, before anything is dropped: the target identity (project reference
from the pooler user, database, schema), the explicit table list, that every
table holds zero rows, that nothing outside the list depends on it (views,
functions, triggers, foreign keys from other schemas), and that the intended
WBS schema lives, intact and ahead, in `capex_tmpl_uat`. It never modifies
anything; the drop is a separate, reviewed step. No secret is printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/fable51/evidence/uat-db"
EXPECTED_PROJECT_REF = "lmljdkluuqpgjboiejro"      # wbs-capex-uat (Supabase Free, Mumbai)
PROTECTED_NAMES = ("praktiq",)                     # never touched, never connected to
STRAY_DB = "postgres"
ESTATE_DB = "capex_tmpl_uat"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in open(os.path.expanduser("~/.capex-tools/stage-b/db.env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def dsn(env: dict[str, str], dbname: str) -> str:
    ca = os.path.expanduser("~/.capex-tools/stage-b/ca-bundle.pem").replace("\\", "/")
    return (f"host={env['CAPEX_DB_HOST']} port={env['CAPEX_DB_PORT']} dbname={dbname} "
            f"user={env['CAPEX_DB_USER']} password={env['CAPEX_DB_PASSWORD']} "
            f"sslmode=verify-full sslrootcert={ca}")


def identity(env: dict[str, str]) -> dict:
    user = env["CAPEX_DB_USER"]
    ref = user.split(".", 1)[1] if "." in user else None
    host = env["CAPEX_DB_HOST"]
    for name in PROTECTED_NAMES:
        if name in host.lower() or name in user.lower():
            raise SystemExit(f"REFUSED: the configured target names the protected project {name!r}")
    if ref != EXPECTED_PROJECT_REF:
        raise SystemExit(f"REFUSED: pooler user carries project ref {ref!r}, expected {EXPECTED_PROJECT_REF!r}")
    return {"project_ref": ref, "host": host, "user_prefix": user.split(".", 1)[0], "database": STRAY_DB, "schema": "public"}


def audit(env: dict[str, str]) -> dict:
    rec = {"taken_at": datetime.now(timezone.utc).isoformat(), "identity": identity(env)}
    with psycopg.connect(dsn(env, STRAY_DB)) as con, con.cursor() as cur:
        cur.execute("select current_database(), current_user, version()")
        db, usr, ver = cur.fetchone()
        assert db == STRAY_DB, db
        rec["server"] = {"current_database": db, "current_user": usr, "version": ver.split(" on ")[0]}
        cur.execute("""select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
                       where n.nspname='public' and c.relkind in ('r','p') order by 1""")
        tables = [r[0] for r in cur.fetchall()]
        counts = {}
        for t in tables:
            cur.execute(f'select count(*) from public."{t}"')
            counts[t] = cur.fetchone()[0]
        cur.execute("select version, name, applied_at from schema_migrations order by version") if "schema_migrations" in tables else None
        migs = [(r[0], r[1], r[2].isoformat()) for r in cur.fetchall()] if "schema_migrations" in tables else []
        cur.execute("select schemaname, viewname from pg_views where schemaname not in ('pg_catalog','information_schema') order by 1,2")
        views = cur.fetchall()
        cur.execute("""select n.nspname, p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                       where n.nspname='public' order by 1,2""")
        functions = cur.fetchall()
        cur.execute("""select c.relname, t.tgname from pg_trigger t join pg_class c on c.oid=t.tgrelid
                       join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and not t.tgisinternal order by 1,2""")
        triggers = cur.fetchall()
        cur.execute("""select n1.nspname, c1.relname, n2.nspname, c2.relname from pg_constraint k
                       join pg_class c1 on c1.oid=k.conrelid join pg_namespace n1 on n1.oid=c1.relnamespace
                       join pg_class c2 on c2.oid=k.confrelid join pg_namespace n2 on n2.oid=c2.relnamespace
                       where k.contype='f' and (n1.nspname<>'public' or n2.nspname<>'public')""")
        cross_schema_fks = cur.fetchall()
        cur.execute("""select n.nspname, count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
                       where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema','pg_toast')
                       group by 1 order by 1""")
        other_schemas = cur.fetchall()
        cur.execute("select pg_size_pretty(sum(pg_total_relation_size(c.oid))) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind in ('r','p')")
        size = cur.fetchone()[0]
    rec["stray"] = {"tables": tables, "table_count": len(tables), "row_counts": counts,
                    "tables_with_rows": {t: n for t, n in counts.items() if n},
                    "schema_migrations_rows": migs, "views": views, "public_functions": functions,
                    "triggers_on_public_tables": len(triggers), "cross_schema_foreign_keys": cross_schema_fks,
                    "tables_per_schema": other_schemas, "public_total_size": size}
    with psycopg.connect(dsn(env, ESTATE_DB)) as con, con.cursor() as cur:
        cur.execute("""select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
                       where n.nspname='public' and c.relkind in ('r','p') order by 1""")
        estate_tables = [r[0] for r in cur.fetchall()]
        cur.execute("select max(version) from schema_migrations")
        estate_version = cur.fetchone()[0]
        cur.execute("select count(*) from app_user")
        users = cur.fetchone()[0]
    rec["estate"] = {"database": ESTATE_DB, "schema_version": estate_version, "app_user_rows": users,
                     "table_count": len(estate_tables),
                     "stray_tables_not_in_estate": sorted(set(tables) - set(estate_tables)),
                     "estate_tables_not_in_stray": sorted(set(estate_tables) - set(tables))}
    # The populated tables: are their rows EXACTLY the migration-seeded catalogue
    # rows (the same rows the estate holds, less the estate's own later
    # migrations), and are the public functions/triggers the same set as the
    # estate's (i.e. ours, not the provider's)?
    populated = [t for t, n in counts.items() if n and t != "schema_migrations"]
    same_rows, differing = {}, {}
    with psycopg.connect(dsn(env, STRAY_DB)) as c1, psycopg.connect(dsn(env, ESTATE_DB)) as c2:
        k1, k2 = c1.cursor(), c2.cursor()
        for t in populated:
            k1.execute(f'select * from public."{t}" order by 1'); a = k1.fetchall(); cols1 = [d.name for d in k1.description]
            k2.execute(f'select * from public."{t}" order by 1'); b = k2.fetchall(); cols2 = [d.name for d in k2.description]
            volatile = {"created_at", "updated_at", "applied_at"}
            keep = [i for i, cn in enumerate(cols1) if cn not in volatile and cn in cols2]
            a2 = sorted(tuple(str(r[i]) for i in keep) for r in a)
            b2 = sorted(tuple(str(r[cols2.index(cols1[i])]) for i in keep) for r in b)
            (same_rows if a2 == b2 else differing)[t] = {"stray_rows": len(a), "estate_rows": len(b)}
        q_fn = """select p.proname||'/'||pg_get_function_identity_arguments(p.oid) from pg_proc p
                  join pg_namespace n on n.oid=p.pronamespace where n.nspname='public'"""
        k1.execute(q_fn); f1 = {r[0] for r in k1.fetchall()}
        k2.execute(q_fn); f2 = {r[0] for r in k2.fetchall()}
        q_tg = """select c.relname||'.'||t.tgname from pg_trigger t join pg_class c on c.oid=t.tgrelid
                  join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and not t.tgisinternal"""
        k1.execute(q_tg); t1 = {r[0] for r in k1.fetchall()}
        k2.execute(q_tg); t2 = {r[0] for r in k2.fetchall()}
        # which migration seeds each populated table (grep the SQL files)
    seeded_by = {}
    for t in populated:
        hits = sorted(f.name for f in (ROOT / "migrations/pg").glob("0*.sql")
                      if f"INSERT INTO {t}" in f.read_text(encoding="utf-8") or f"insert into {t}" in f.read_text(encoding="utf-8").lower())
        seeded_by[t] = hits
    rec["populated_tables"] = {"rows_identical_to_estate": same_rows, "rows_differ_from_estate": differing,
                               "seeded_by_migration_file": seeded_by}
    rec["objects_vs_estate"] = {"public_functions_only_in_stray": sorted(f1 - f2), "public_functions_only_in_estate": sorted(f2 - f1),
                                "public_function_count": [len(f1), len(f2)],
                                "triggers_only_in_stray": sorted(t1 - t2), "triggers_only_in_estate": sorted(t2 - t1),
                                "trigger_count": [len(t1), len(t2)]}
    return rec


def backup(env: dict[str, str], tag: str) -> dict:
    """pg_dump of the stray schema (schema + data, custom format) with its checksum."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"stray-postgres-public-{tag}.dump"
    exe = os.path.expanduser("~/.capex-tools/pgsql/bin/pg_dump.exe")
    if not os.path.exists(exe):
        exe = "pg_dump"
    cmd = [exe, "--format=custom", "--schema=public", "--no-owner", "--no-privileges",
           f"--file={path}", f"--host={env['CAPEX_DB_HOST']}", f"--port={env['CAPEX_DB_PORT']}",
           f"--username={env['CAPEX_DB_USER']}", f"--dbname={STRAY_DB}"]
    penv = dict(os.environ, PGPASSWORD=env["CAPEX_DB_PASSWORD"], PGSSLMODE="verify-full",
                PGSSLROOTCERT=os.path.expanduser("~/.capex-tools/stage-b/ca-bundle.pem").replace("\\", "/"))
    r = subprocess.run(cmd, env=penv, capture_output=True, text=True)
    out = {"command": " ".join(c if not c.startswith("--username") else "--username=<pooler user>" for c in cmd),
           "returncode": r.returncode, "stderr": r.stderr.strip()[-600:], "tool_version": subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()}
    if r.returncode == 0 and path.exists():
        out["file"] = str(path.relative_to(ROOT))
        out["bytes"] = path.stat().st_size
        out["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        out["restore_command"] = (f"pg_restore --no-owner --no-privileges --schema=public --dbname=postgres "
                                  f"--host=<pooler host> --port=5432 --username=<pooler user> {path.name}")
    return out


if __name__ == "__main__":
    env = load_env()
    rec = audit(env)
    tag = rec["taken_at"][:19].replace(":", "")
    if "--backup" in sys.argv:
        rec["backup"] = backup(env, tag)
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"stray-schema-audit-{tag}.json"
    p.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    s = rec["stray"]
    print("wrote", p.relative_to(ROOT))
    print("identity:", rec["identity"], "| server:", rec["server"])
    print(f"stray public tables: {s['table_count']}  with rows: {s['tables_with_rows'] or 'NONE'}  size: {s['public_total_size']}")
    print(f"schema_migrations rows: {len(s['schema_migrations_rows'])} (latest {s['schema_migrations_rows'][-1][0] if s['schema_migrations_rows'] else None})")
    print(f"views: {s['views']}  functions in public: {len(s['public_functions'])}  triggers: {s['triggers_on_public_tables']}  cross-schema FKs: {s['cross_schema_foreign_keys']}")
    print("tables per schema:", s["tables_per_schema"])
    e = rec["estate"]
    print(f"estate {e['database']}: version {e['schema_version']}, app_user rows {e['app_user_rows']}, tables {e['table_count']}; "
          f"stray-not-in-estate {e['stray_tables_not_in_estate']}; estate-not-in-stray {e['estate_tables_not_in_stray']}")
    print("populated tables:", json.dumps(rec["populated_tables"], default=str))
    print("objects vs estate:", json.dumps(rec["objects_vs_estate"], default=str)[:1500])
    if "backup" in rec:
        b = rec["backup"]
        print("backup:", {k: b[k] for k in b if k != "stderr"}, "| stderr:", b["stderr"][:200])
