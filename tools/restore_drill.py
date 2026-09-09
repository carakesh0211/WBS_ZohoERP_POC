"""Restore drill: prove a backup is restorable, and that the restore is SOUND.

    python tools/restore_drill.py --sqlite <restored.db>          # POC
    python tools/restore_drill.py --dsn <restored-dsn>            # PostgreSQL
    python tools/restore_drill.py --sqlite <db> --json report.json

The gate this tool exists for
-----------------------------
**A restore that breaks the audit chain is a FAILED restore.** Not a restore
with a caveat, not a restore to investigate later -- a failure, with a non-zero
exit code, that a scheduled drill treats exactly as it would treat "the dump
would not load at all".

The reason is that the two failures are indistinguishable in consequence. A
database that loads but whose history no longer verifies cannot answer the one
question the product exists to answer -- "was this approval edited after the
fact?" -- and it will keep serving traffic while being unable to answer it. A
dump that does not load at least announces itself.

So the drill runs four checks, in order, and stops at the first failure:

1. **The restored database opens.** Nothing else is meaningful otherwise.
2. **The schema is at a known revision.** A restore from before a migration is
   a different product, and reporting it as healthy is how the wrong backup
   gets promoted.
3. **Row counts are plausible against the expectations file**, if one is given.
   An empty restore opens perfectly and verifies perfectly.
4. **Every audit stream verifies.** Using the PRODUCT's verifier, never a copy.

Check 4 is the one that makes this a drill rather than a smoke test.

Verification status
-------------------
``--sqlite`` is ``VERIFIED-LOCAL``: exercised in ``tests/test_ops_readiness.py``
against disposable databases the test creates, including a deliberately
corrupted one that must FAIL.

``--dsn`` is ``UNVERIFIED``. There is no PostgreSQL on this build host, so the
PostgreSQL path has never run. It is written, and it is wrong to read it as
working.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DrillFailure(RuntimeError):
    """The restore is not acceptable. Exit non-zero; do not promote it."""


# ------------------------------------------------------------------ the checks
def check_opens(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if integrity != "ok":
        raise DrillFailure(f"the restored file fails PRAGMA integrity_check: {integrity}")
    return {"check": "opens", "ok": True, "integrity_check": integrity}


def check_schema_revision(con: sqlite3.Connection,
                          expected: list[str] | None) -> dict[str, Any]:
    try:
        applied = [f"{r[0]} {r[1]}" for r in con.execute(
            "SELECT version, name FROM schema_migration ORDER BY version")]
    except sqlite3.OperationalError as exc:
        raise DrillFailure(
            "the restored database has no schema_migration table, so its "
            f"revision is unknown and it cannot be certified: {exc}") from exc
    if not applied:
        raise DrillFailure("schema_migration is empty: revision unknown")
    if expected is not None and applied != expected:
        raise DrillFailure(
            f"schema revision mismatch. Restored: {applied}. Expected: {expected}. "
            "A restore from before a migration is a different product.")
    return {"check": "schema_revision", "ok": True, "applied": applied}


def check_row_counts(con: sqlite3.Connection,
                     expected: dict[str, int] | None) -> dict[str, Any]:
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    if expected is None:
        return {"check": "row_counts", "ok": True, "counts": counts,
                "note": "no expectations file supplied; counts recorded, not asserted"}
    differences = [f"{t}: restored {counts.get(t, 0)}, expected {n}"
                   for t, n in sorted(expected.items()) if counts.get(t, 0) != n]
    if differences:
        raise DrillFailure("restored row counts do not match expectations:\n  "
                           + "\n  ".join(differences))
    return {"check": "row_counts", "ok": True, "counts": counts}


def check_audit_chain_sqlite(con: sqlite3.Connection) -> dict[str, Any]:
    """The POC chain, through the PRODUCT's verifier.

    ``services.verify_audit_chain`` is imported rather than reimplemented. A
    drill that checks the chain with its own copy of the hash walk proves the
    two copies agree, which is not the question.
    """
    from app.backend.services import verify_audit_chain

    con.row_factory = sqlite3.Row
    result = verify_audit_chain(con)
    unhashed = con.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entry_hash IS NULL").fetchone()[0]
    if not result["intact"]:
        raise DrillFailure(
            f"THE RESTORE BROKE THE AUDIT CHAIN. Broken audit_id(s): "
            f"{result['broken']}. This is a FAILED restore, not a restore with a "
            "caveat: a database that serves traffic while unable to prove its own "
            "history is worse than one that will not start. Do not promote it.")
    return {"check": "audit_chain", "ok": True, "entries": result["entries"],
            "unhashed_rows": unhashed,
            "unhashed_note": (
                "Rows with entry_hash IS NULL predate POC migration 002 and are "
                "skipped by the verifier. They are history, not evidence, and "
                "this count is where verifiable history begins.")}


def check_audit_chain_postgres(database) -> dict[str, Any]:      # pragma: no cover
    """UNVERIFIED: never executed. No PostgreSQL on this build host.

    Every stream, not a sampled one. ``verify_chain`` is per-stream, so a drill
    that checks one stream certifies one stream.
    """
    from app.backend.pg.audit import verify_chain

    results, broken = {}, []
    with database.session() as session:
        streams = [r[0] for r in session.fetchall(
            "SELECT DISTINCT stream_key FROM audit_log ORDER BY stream_key")]
        for stream in streams:
            result = verify_chain(session, stream)
            results[stream] = result
            if not result["intact"]:
                broken.append(stream)
    if broken:
        raise DrillFailure(
            f"THE RESTORE BROKE THE AUDIT CHAIN in stream(s) {broken}: "
            f"{ {s: results[s] for s in broken} }. This is a FAILED restore. "
            "Do not promote it, and do not recompute any hash to make it pass.")
    return {"check": "audit_chain", "ok": True, "streams_checked": len(results),
            "streams": results}


# -------------------------------------------------------------------- the drill
def drill_sqlite(path: str | Path, *, expected_counts: dict[str, int] | None = None,
                 expected_migrations: list[str] | None = None) -> dict[str, Any]:
    target = Path(path)
    checks = [check_opens(target)]
    con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        checks.append(check_schema_revision(con, expected_migrations))
        checks.append(check_row_counts(con, expected_counts))
        checks.append(check_audit_chain_sqlite(con))
    finally:
        con.close()
    return {
        "target": target.as_posix(),
        "backend": "sqlite",
        "sha256": _sha256(target),
        "passed": True,
        "checks": checks,
        "verification_status": "VERIFIED-LOCAL",
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", help="path to a RESTORED SQLite database")
    ap.add_argument("--dsn", help="DSN of a RESTORED PostgreSQL database (UNVERIFIED path)")
    ap.add_argument("--expect-counts", help="JSON file of {table: row_count}")
    ap.add_argument("--json", help="write the drill report here")
    args = ap.parse_args(argv)

    expected = None
    if args.expect_counts:
        expected = json.loads(Path(args.expect_counts).read_text(encoding="utf-8"))

    try:
        if args.sqlite:
            report = drill_sqlite(args.sqlite, expected_counts=expected)
        elif args.dsn:
            from app.backend.pg.engine import Database  # noqa: F401
            print("The PostgreSQL restore drill has never been executed on this "
                  "build host -- there is no PostgreSQL here. Run it from a host "
                  "with the restored server reachable. See "
                  "docs/runbooks/backup-restore.md, which marks this UNVERIFIED.")
            return 2
        else:
            ap.error("one of --sqlite or --dsn is required")
    except DrillFailure as exc:
        print(f"RESTORE DRILL FAILED\n\n{exc}")
        if args.json:
            Path(args.json).write_text(json.dumps(
                {"passed": False, "failure": str(exc)}, indent=2) + "\n", encoding="utf-8")
        return 1

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"restore drill PASSED for {report['target']}")
    for check in report["checks"]:
        print(f"  [ok] {check['check']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
