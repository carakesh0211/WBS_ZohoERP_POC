"""FastAPI-on-AppSail proof, with vendored Linux CPython 3.13 dependencies.

Why dependencies are vendored
-----------------------------
Catalyst's managed-runtime documentation states:

    "You must ensure that you add all modules and configuration files, along
     with the main file and native client files in the build path."

There is no automatic `pip install -r requirements.txt` step. The first spike
deployment therefore built successfully but died at start-up on
`from fastapi import FastAPI`, producing:

    503 {"message": "Execution failed. Please check the startup command or port."}

`vendor/` holds the resolved dependency graph as Linux x86_64 CPython 3.13
wheels, downloaded from PyPI - NOT copied from Windows site-packages, because
pydantic-core ships a compiled extension
(_pydantic_core.cpython-313-x86_64-linux-gnu.so) that is platform-specific.

Version resolution is exact, not "latest of everything":
pydantic 2.13.4 pins pydantic-core==2.46.4, and fastapi 0.133.1 additionally
requires annotated-doc - both are easy to miss and both break at import.

Self-diagnosing by design
-------------------------
If FastAPI imports, it serves and the proof is made. If it does not, this file
still binds the port with a stdlib server and reports the exact import error,
so a failure produces evidence rather than another opaque 503.
"""
import json
import os
import platform
import sys
import time
import traceback

BOOT = time.time()

# Vendored packages must precede anything else on the path.
HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

PORT = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", "9000"))

print(f"[spike] python {platform.python_version()} ({platform.machine()})", flush=True)
print(f"[spike] executable {sys.executable}", flush=True)
print(f"[spike] vendor path {VENDOR} exists={os.path.isdir(VENDOR)}", flush=True)
print(f"[spike] target port {PORT}", flush=True)

IMPORT_ERROR = None
try:
    from fastapi import FastAPI
    import uvicorn
    import fastapi as _fastapi
    import pydantic as _pydantic
    FASTAPI_OK = True
    print(f"[spike] fastapi {_fastapi.__version__} / pydantic {_pydantic.VERSION} imported "
          f"in {round(time.time() - BOOT, 3)}s", flush=True)
except Exception:
    FASTAPI_OK = False
    IMPORT_ERROR = traceback.format_exc()
    print("[spike] FastAPI import FAILED:", flush=True)
    print(IMPORT_ERROR, flush=True)


def payload():
    import fastapi as f
    import pydantic as p
    return {
        "framework": "fastapi",
        "proof": "FastAPI serves requests on Zoho Catalyst AppSail",
        "fastapi_version": f.__version__,
        "pydantic_version": p.VERSION,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "machine": platform.machine(),
        "listen_port_env": os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT"),
        "bound_port": PORT,
        "seconds_from_process_start": round(time.time() - BOOT, 3),
        "dependencies_vendored": True,
    }


if FASTAPI_OK:
    app = FastAPI(title="wbs-platform-spike", version="1.0.0")

    @app.get("/")
    def root():
        return payload()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "framework": "fastapi"}

    if __name__ == "__main__":
        print(f"[spike] binding 0.0.0.0:{PORT} via uvicorn "
              f"at {round(time.time() - BOOT, 3)}s", flush=True)
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
else:
    # Fallback: still bind, still report. A failure must yield evidence.
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({
                "framework": "stdlib-fallback",
                "fastapi_import_failed": True,
                "error": IMPORT_ERROR,
                "python": platform.python_version(),
                "machine": platform.machine(),
                "vendor_exists": os.path.isdir(VENDOR),
                "vendor_sample": sorted(os.listdir(VENDOR))[:30] if os.path.isdir(VENDOR) else [],
                "sys_path": sys.path[:6],
            }, indent=2).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            print(f"[spike] {fmt % a}", flush=True)

    if __name__ == "__main__":
        print(f"[spike] fallback binding 0.0.0.0:{PORT}", flush=True)
        HTTPServer(("0.0.0.0", PORT), H).serve_forever()
