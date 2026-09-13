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
`CAPEX_ERP_OUTBOUND_WRITES` is stripped in the preview and honoured on
stage B only when the platform sets exactly `1`), no secrets read from
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
    # On Catalyst every request arrives from the platform gateway, so the
    # per-address login throttle would key every reviewer on one address and
    # ten bad guesses by anyone would lock everyone out for fifteen minutes.
    # Trust the gateway's first X-Forwarded-For hop THERE ONLY; locally the
    # peer address is the truth and a forged header must not be believed.
    if os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT"):
        os.environ.setdefault("CAPEX_TRUST_PROXY", "1")
    # STAGE B (Fable 5.1): when the Catalyst configuration supplies a
    # PostgreSQL host or URL, the financial API runs against it -- the
    # provider's CA at the bundle root with sslmode verify-full, never
    # relaxed. The SQLite copy above still carries the shell (identities,
    # sessions, legacy screens) and resets on restart, which the product
    # owner accepted for UAT on 2026-09-11. Without a host this is Stage A:
    # the variables are unset rather than trusted.
    on_catalyst = bool(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT"))
    if on_catalyst and (os.environ.get("CAPEX_DB_HOST") or os.environ.get("CAPEX_DB_URL")):
        ca = os.path.join(BUNDLE, "ca-bundle.pem")
        # 2026-09-12 review, item 4: `setdefault` alone only supplies
        # verify-full when the console left the variable UNSET -- a console
        # value of `require`, `prefer` or `disable` passed straight through,
        # silently weaker than "never relaxed" above promises. A value is
        # accepted here ONLY if it is already verify-full or absent; anything
        # else refuses to start rather than connect at a weaker level.
        _sslmode = os.environ.get("CAPEX_DB_SSLMODE")
        if _sslmode and _sslmode != "verify-full":
            _fail(f"CAPEX_DB_SSLMODE is {_sslmode!r}, not 'verify-full'. Stage "
                  f"B's PostgreSQL connection is never relaxed below "
                  f"verify-full; unset CAPEX_DB_SSLMODE in the platform "
                  f"configuration (Stage B supplies verify-full itself) or "
                  f"set it explicitly to 'verify-full'.")
        os.environ.setdefault("CAPEX_DB_SSLMODE", "verify-full")
        if os.path.isfile(ca):
            os.environ.setdefault("CAPEX_DB_SSLROOTCERT", ca)
        elif os.environ.get("CAPEX_DB_SSLMODE", "verify-full") == "verify-full" \
                and not os.environ.get("CAPEX_DB_SSLROOTCERT"):
            _fail("CAPEX_DB_HOST is set but no ca-bundle.pem is at the bundle root and "
                  "CAPEX_DB_SSLROOTCERT is unset; verify-full cannot be honoured")
        print("UAT stage B: PostgreSQL configured from the platform environment "
              f"(sslmode={os.environ.get('CAPEX_DB_SSLMODE')}, CA at bundle root="
              f"{os.path.isfile(ca)})", flush=True)
    else:
        for name in ("CAPEX_DB_URL", "CAPEX_DB_HOST"):
            os.environ.pop(name, None)
    # Outbound ERP writes. The ephemeral preview (no PostgreSQL) can never
    # write: the gate is stripped whatever the platform says. Stage B (a
    # PostgreSQL configured from the platform environment) honours the
    # platform's own gate, and ONLY the exact value "1" -- the owner's
    # controlled outbound authorisation of 2026-09-12/13 is enacted by setting
    # it in the console and revoked by unsetting it there. Until 2026-09-13
    # this launcher stripped it in every stage, so a console value could never
    # reach the process (four recycles proved it). The state is announced,
    # never the value.
    gate = os.environ.get("CAPEX_ERP_OUTBOUND_WRITES")
    stage_b = on_catalyst and bool(os.environ.get("CAPEX_DB_HOST"))
    if stage_b and gate is not None and gate.strip() == "1":
        os.environ["CAPEX_ERP_OUTBOUND_WRITES"] = "1"
        print("UAT stage B: outbound ERP writes ENABLED by the platform gate "
              "(CAPEX_ERP_OUTBOUND_WRITES=1); the connection mode is the second control",
              flush=True)
    else:
        os.environ.pop("CAPEX_ERP_OUTBOUND_WRITES", None)
        if gate is not None:
            print("UAT: CAPEX_ERP_OUTBOUND_WRITES was set but is stripped "
                  f"({'not stage B' if not stage_b else 'value is not exactly 1'}); "
                  "outbound ERP writes stay disabled", flush=True)
    return db_path


def main() -> int:
    db_path = prepare_environment()
    print(f"UAT preview: profile=uat-preview db={os.path.basename(db_path)} "
          f"(ephemeral copy of the synthetic seed)", flush=True)
    from app import run as app_run
    return app_run.main(["--public"])


if __name__ == "__main__":
    raise SystemExit(main())
