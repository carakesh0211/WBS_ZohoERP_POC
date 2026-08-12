"""Start the CAPEX & WBS Control Hub POC.

    python app/run.py            # serve on http://127.0.0.1:8000
    python app/run.py --reseed   # rebuild the demo dataset first
    python app/run.py --public   # bind 0.0.0.0 so a tunnel or host can reach it

Environment:
    X_ZOHO_CATALYST_LISTEN_PORT
                    Catalyst AppSail listen port (takes precedence)
    PORT            listen port outside Catalyst (default 8000)
    HOST            bind address (default 127.0.0.1, or 0.0.0.0 with --public)
    CAPEX_DB_PATH   where the SQLite file lives
    DEMO_USER       set both of these to require a username and password
    DEMO_PASSWORD   before anything in the app is reachable
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    from app.backend import auth, db, migrate

    if "--reseed" in sys.argv or not os.path.exists(db.DB_PATH):
        print("Seeding demo dataset ...")
        # reset_and_seed() creates only the legacy v1 schema. Build through
        # the migration runner so the authentication and financial-control
        # tables added by later migrations are present in hosted instances.
        migrate.fresh(db.DB_PATH, seed=True)
        print("  ->", db.DB_PATH)
    else:
        migrate.upgrade(db.DB_PATH, backup=False)

    # POC identities are deliberately provisioned separately from business
    # seed data. Keep this idempotent step in startup so a fresh AppSail
    # instance always has the documented demo accounts.
    con = db.connect()
    try:
        provisioned = auth.provision_dev_identities(con)
        print(f"  -> demo identities ready: {provisioned}")
    finally:
        con.close()

    host = os.environ.get("HOST") or ("0.0.0.0" if "--public" in sys.argv else "127.0.0.1")
    # AppSail assigns the listening port at runtime and verifies that the
    # process binds to it. Keep PORT as the portable/local fallback.
    port = int(
        os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT")
        or os.environ.get("PORT", "8000")
    )

    if host != "127.0.0.1" and not (os.environ.get("DEMO_USER") and os.environ.get("DEMO_PASSWORD")):
        print("\n  WARNING: binding to", host, "with no DEMO_USER / DEMO_PASSWORD set.")
        print("  Anyone who reaches this address can approve, cancel and capitalise.")
        print("  Set both variables before exposing this beyond your own machine.\n")

    import uvicorn
    print(f"CAPEX & WBS Control Hub -> http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    uvicorn.run("app.backend.main:app", host=host, port=port, reload=False)
