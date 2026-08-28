# Spike — FastAPI on Zoho Catalyst AppSail

**Result: PROVEN.** Full evidence in [`EVIDENCE.md`](EVIDENCE.md); narrative in [`../../PHASE_0A_EXIT.md` §8](../../PHASE_0A_EXIT.md).

## Why this spike existed

Zoho's AppSail documentation names Flask, Django, Bottle, CherryPy and Tornado. It **never names FastAPI**. It does state there are no framework restrictions — so FastAPI is permitted by a *general clause*, not by explicit support. Plan v1.2.1 Phase 1 assumes FastAPI works on AppSail, and a general permission clause is weaker evidence than a tested framework. The difference only surfaces at deployment time, which is the worst moment to discover it.

## Contents

| File | Purpose |
|---|---|
| `main.py` | The spike application. No CAPEX code, no database, no Zoho API call, no credentials, no client data |
| `app-config.json` | Startup command and stack |
| `vendored-versions.txt` | **Exact** versions vendored into the deployment that succeeded |
| `BUILD.md` | How to rebuild the deployable bundle for Linux x86_64 / CPython 3.13 |
| `EVIDENCE.md` | Service and deployment IDs, bundle SHA-256, endpoint results, CI run |

**The 3.07 MB vendored bundle is deliberately not committed.** It is 413 files of third-party wheel contents including a compiled `.so`, all reproducible from `vendored-versions.txt` via `BUILD.md`. Committing it would add binary weight to the repository for no traceability gain — the SHA-256 of the exact artefact that deployed is recorded in `EVIDENCE.md` instead.

## The finding that matters for Phase 1

**Catalyst does not run `pip install -r requirements.txt`.** From the managed-runtime documentation:

> "You must ensure that you add all modules and configuration files, along with the main file and native client files in the build path."

The first deployment of this spike built successfully and then failed every request with:

```
503 {"message": "Execution failed. Please check the startup command or port."}
```

The command and port were correct throughout. The process died on `from fastapi import FastAPI` before it could bind, and AppSail reports that as a generic startup failure — a misleading message that costs time if you take it literally.

**Consequence:** the Phase 1 deployment pipeline must vendor dependencies for Linux x86_64 / CPython 3.13 as a build step. This is not optional and it is not a footnote.

## Two design points worth carrying forward

**Read the port inside Python.** AppSail assigns the listen port at runtime via `X_ZOHO_CATALYST_LISTEN_PORT` and executes the start command *without a shell*, so `--port $X_ZOHO_CATALYST_LISTEN_PORT` will not expand. `main.py` reads the environment variable and passes the port to uvicorn programmatically.

**Make failures self-diagnosing.** `main.py` falls back to a standard-library HTTP server that reports the exact import error if FastAPI cannot be imported. A failed deployment then returns evidence instead of another opaque 503. Any future AppSail work in this project should keep that property.
