"""AppSail entry point for the UAT visual preview (Fable 5.1, Stage A).

Copied to the bundle root as `main.py` by tools/appsail/build_uat_bundle.py
and started by AppSail as `python3 -u main.py`.

What this process does, in order, and what it deliberately does NOT do:

1. Puts the vendored dependency tree (`vendor/`, Linux x86-64 CPython 3.13
   wheels resolved by the build script) on `sys.path`. Catalyst installs
   nothing; every module ships in the archive.
2. Restores the SYNTHETIC seed database. `uat_seed.db` in the archive is a
   read-only artefact built at bundle time by `python -m app.backend.migrate
   --fresh --seed` under the local-demo profile. It is COPIED to a writable
   scratch path on every cold start, so:
     * the data is ephemeral -- an instance restart or scale event resets it;
     * this process never migrates anything (DEF-01 holds: the schema is
       verified read-only by app/run.py and a behind/missing schema refuses
       to serve); restoring a snapshot is not a migration.
3. Sets the profile to `uat-preview` and points the application at the UAT
   credential HASHES shipped alongside it (`uat-credentials.json`, generated
   outside the repository). The derivable `<id>!demo` passwords are never
   installed under this profile -- app/run.py refuses to start without the
   credentials file rather than falling back.
4. Hands over to `app.run.main()`, which binds `X_ZOHO_CATALYST_LISTEN_PORT`.

Not done here: no PostgreSQL (`CAPEX_DB_URL` is unset, so `/readyz` honestly
answers 503 DatabaseNotConfigured -- that is the truthful state of a visual
preview, not a fault), no ERP call (`zoho.MODE` is MOCK and
`CAPEX_ERP_OUTBOUND_WRITES` is left unset = disabled), no secrets read from
anywhere but the environment, nothing printed that a log should not hold.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

BUNDLE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BUNDLE, "vendor")
SEED = os.path.join(BUNDLE, "uat_seed.db")
CREDENTIALS = os.path.join(BUNDLE, "uat-credentials.json")


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"REFUSING TO START: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def prepare_environment() -> str:
    """Everything that must be true BEFORE `app.backend.db` is imported --
    `CAPEX_DB_PATH` is read at import time. Returns the scratch DB path."""
    if os.path.isdir(VENDOR) and VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    if BUNDLE not in sys.path:
        sys.path.insert(0, BUNDLE)

    if not os.path.isfile(SEED):
        _fail("uat_seed.db is missing from the bundle")
    if not os.path.isfile(CREDENTIALS):
        _fail("uat-credentials.json is missing from the bundle")

    scratch_dir = os.environ.get("CAPEX_UAT_SCRATCH_DIR") or tempfile.gettempdir()
    os.makedirs(scratch_dir, exist_ok=True)
    db_path = os.path.join(scratch_dir, f"capex_uat_{os.getpid()}.db")
    # Fresh copy on every process start: the preview always boots from the
    # same synthetic snapshot, which is what "resets on restart" means.
    shutil.copyfile(SEED, db_path)

    os.environ["CAPEX_PROFILE"] = "uat-preview"
    os.environ["CAPEX_DB_PATH"] = db_path
    os.environ["CAPEX_UAT_CREDENTIALS"] = CREDENTIALS
    os.environ.setdefault("HOST", "0.0.0.0")
    # A visual preview never talks to PostgreSQL or an ERP. Unset rather than
    # trust that nothing in the container environment set them.
    for name in ("CAPEX_DB_URL", "CAPEX_DB_HOST", "CAPEX_ERP_OUTBOUND_WRITES"):
        os.environ.pop(name, None)
    return db_path


def main() -> int:
    db_path = prepare_environment()
    print(f"UAT preview: profile=uat-preview db={os.path.basename(db_path)} "
          f"(ephemeral copy of the synthetic seed)", flush=True)
    from app import run as app_run
    return app_run.main(["--public"])


if __name__ == "__main__":
    raise SystemExit(main())
