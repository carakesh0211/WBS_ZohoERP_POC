"""Run the bounded VRT release plan (`tools/vrt/batches_release.json`), one
spec at a time, one of the three approved viewports at a time.

    python tools/vrt/run_batches.py [--plan tools/vrt/batches_release.json]
                                    [--out .vrt-batches-release] [--port 8897]

NOT executed by the change that adds this file -- a PostgreSQL-backed run is
already using this machine today, and this script starts its own local-demo
SQLite server. The lead runs it when that is clear.

Same reasoning as `tools/run_vrt_batches.sh`, ported to Python rather than
shelled out to, so the release plan can be DATA (`batches_release.json`)
instead of a positional-argument list, and so a blocking batch's failure can
be told apart from an informational one in the summary and in the exit code:

  * ONE SPEC, ONE VIEWPORT PER BATCH -- a hang or a failure is attributable to
    a batch, never to "the suite".
  * ONE SERVER FOR EVERY BATCH -- `CAPEX_VRT_REUSE=1` attaches to a server
    this script starts once, migrated fresh under `CAPEX_PROFILE=local-demo`,
    rather than paying `migrate --fresh --seed` per batch.
  * TWO TIMEOUTS -- `--timeout` bounds one Playwright test; this script's own
    subprocess timeout bounds the whole batch, because a runner wedged
    outside a test is exactly what the per-test timeout cannot catch. A
    timed-out batch is reported as TIMEOUT, never folded in with a pass.
  * REAL EXIT CODES -- `tools/run_vrt.mjs` passes Playwright's own exit code
    through untouched (see that file's docstring for the two ways a naive
    wrapper reported green on a failed suite); this script records it
    per batch rather than trusting stdout text.
  * BLOCKING VS. INFORMATIONAL -- a plan batch carries its own `blocking`
    flag (see `batches_release.json`'s `blocking_categories`). The exit code
    is the count of FAILED BLOCKING batches; an informational batch's
    failure is still logged and still appears in the summary, but never
    holds the release by itself.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = ROOT / "tools" / "vrt" / "batches_release.json"


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _health_check(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_server(port: int, out_dir: Path, wait_s: int = 40) -> subprocess.Popen:
    """Migrate a disposable local-demo SQLite database, then start the app
    server on `port`, exactly the way `tools/run_vrt_batches.sh` does. The
    caller is responsible for terminating the returned process."""
    db_path = ROOT / "app" / "data" / f"capex_vrt-release-{port}.db"
    env = dict(os.environ)
    env["CAPEX_PROFILE"] = "local-demo"
    env["CAPEX_DB_PATH"] = str(db_path)
    env.pop("CAPEX_DB_URL", None)

    migrate_log = out_dir / "migrate.log"
    with open(migrate_log, "w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "-m", "app.backend.migrate", "--fresh", "--seed"],
            cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"migrate failed rc={proc.returncode}; see {migrate_log}")

    env["PORT"] = str(port)
    env["X_ZOHO_CATALYST_LISTEN_PORT"] = str(port)
    server_log = open(out_dir / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "app/run.py"], cwd=str(ROOT), env=env,
                             stdout=server_log, stderr=subprocess.STDOUT)
    for _ in range(wait_s):
        if _health_check(port):
            return server
        time.sleep(1)
    server.terminate()
    raise RuntimeError(f"server did not come up on {port}; see {out_dir / 'server.log'}")


def run_one_batch(spec: str, viewport: str, port: int, out_dir: Path,
                  per_test_ms: int, per_batch_s: int) -> tuple[str, str]:
    """`(verdict, tail)`. `verdict` is `ok`, `FAIL rc=<n>` or
    `TIMEOUT after <n>s`. `tail` is the last summarising line of Playwright's
    own output, read back from the batch's log file."""
    label = spec[:-len(".spec.js")] if spec.endswith(".spec.js") else spec
    log_path = out_dir / f"{label}.{viewport}.log"
    env = dict(os.environ)
    env["CAPEX_VRT_REUSE"] = "1"
    env["CAPEX_VRT_PORT"] = str(port)
    cmd = [
        "node", "tools/run_vrt.mjs", f"tests/vrt/{spec}",
        f"--project={viewport}", f"--timeout={per_test_ms}", "--reporter=line",
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=per_batch_s)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = None
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = next((ln for ln in reversed(lines)
                if ln.strip() and any(w in ln for w in ("passed", "failed", "skipped"))), None)
    if tail is None:
        tail = " ".join(lines[-2:]) if lines else ""
    if rc is None:
        return f"TIMEOUT after {per_batch_s}s", tail
    if rc == 0:
        return "ok", tail
    return f"FAIL rc={rc}", tail


def run(plan_path: Path, out_dir: Path, port: int) -> int:
    plan = load_plan(plan_path)
    viewports = plan["viewports"]
    defaults = plan.get("defaults", {})
    per_test_ms = defaults.get("per_test_timeout_ms", 60000)
    per_batch_s = defaults.get("per_batch_timeout_s", 900)

    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.txt"
    summary_lines = [
        f"plan={plan_path} port={port} per_test={per_test_ms}ms per_batch={per_batch_s}s",
        f"viewports: {', '.join(viewports)}",
        f"started_at: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    server = start_server(port, out_dir)
    blocking_failed = 0
    informational_failed = 0
    try:
        for batch in plan["batches"]:
            spec = batch["spec"]
            blocking = bool(batch.get("blocking"))
            tag = "BLOCKING" if blocking else "informational"
            for viewport in viewports:
                verdict, tail = run_one_batch(spec, viewport, port, out_dir, per_test_ms, per_batch_s)
                failed = verdict != "ok"
                if failed and blocking:
                    blocking_failed += 1
                elif failed:
                    informational_failed += 1
                line = (f"{spec[:-len('.spec.js')]:28} {viewport:14} {tag:13} "
                       f"{verdict:22} {tail}")
                print(line)
                summary_lines.append(line)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    summary_lines.append("")
    summary_lines.append(f"blocking batches failed: {blocking_failed}")
    summary_lines.append(f"informational batches failed: {informational_failed}")
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print()
    print(f"blocking batches failed: {blocking_failed}")
    print(f"informational batches failed: {informational_failed}")
    print(f"summary written to {summary_path}")
    return blocking_failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap.add_argument("--out", default=str(ROOT / ".vrt-batches-release"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CAPEX_VRT_PORT", "8897")))
    args = ap.parse_args(argv)
    return run(Path(args.plan), Path(args.out), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
