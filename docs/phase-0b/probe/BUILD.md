# Building the Phase 0B Stage 1 probe bundle

Reproduces the artefact recorded in `../STAGE1_STATUS.md`.

## Constraint

Catalyst's managed runtime does **not** install dependencies — every module must be present in the uploaded build path. The container is **Linux x86_64, CPython 3.13**.

**Never copy Windows `site-packages`.** `pydantic_core` ships a compiled extension; the bundle needs `_pydantic_core.cpython-313-x86_64-linux-gnu.so`. A Windows `.pyd` produces the identical opaque `503 "Execution failed. Please check the startup command or port."` as a missing package, so the two are indistinguishable from the error alone.

`pg8000` is **pure Python** — chosen deliberately so the database driver adds no second compiled wheel, and so a driver problem can never masquerade as a network problem.

## Dependencies

Exact versions in [`dependency-inventory.json`](dependency-inventory.json), including each wheel filename and its PyPI SHA-256.

Two resolution traps, each of which fails at import:

- **`pydantic` 2.13.4 pins `pydantic-core==2.46.4`.** Taking the latest breaks it.
- **`fastapi` 0.133.1 additionally requires `annotated-doc`**, which is easy to miss.

## Build the vendored tree — before the exposure window

`vendor_deps.py` does this, and it must be run **outside** the repository. It
refuses a `--dest` inside the repo, because the tree must not be committable.

```bash
py docs/phase-0b/probe/vendor_deps.py --dest <staging-dir-outside-the-repo>
```

It **resolves** the closure from the four top-level pins (`fastapi`, `uvicorn`,
`pg8000`, `pydantic`) and then asserts the result equals
`dependency-inventory.json` exactly, hash for hash. Downloading the inventory
directly would only prove the inventory is downloadable; resolving and comparing
detects a dependency that has appeared, disappeared or changed.

It did exactly that on first run: `sniffio` was listed in the inventory but is
**not** in the closure. `anyio` 4.14.2 imports it under
`try/except ModuleNotFoundError` with every call site handling `sniffio is None`,
so it is genuinely optional and correctly absent from `Requires-Dist`. The
attempt-1 bundle had been shipping a package nothing depends on. Entry removed;
18 packages remain.

Checks, all of which fail the run rather than warn:

- resolved closure equals the inventory, filename and SHA-256 per wheel
- no source distributions — a sdist would be built for the host, not the target
- declared-pure packages really are `-none-any`; declared-binary really is manylinux
- no `win_amd64`, `musllinux` or `macosx` wheels
- zero `.pyd`, zero `.dll`
- exactly the expected `cpython-313-x86_64-linux-gnu.so`
- every required module present in the extracted tree

It writes `staged-inventory.json` to the staging root with a deterministic
**tree hash** (sorted relative path + content), so two independently built trees
can be compared for identity.

Two programs, deliberately: `vendor_deps.py` may use pip and the network;
`build_bundle.py` may not, and a test enforces that. The in-window build must
never depend on an index being reachable.

## The CA bundle is mandatory

Supabase presents a chain rooted in a CA that is **not** in the system trust
store, so verification against system CAs fails correctly and uninformatively.
`ca-bundle.pem` must be at the archive root.

Obtain it per [`CA_BUNDLE.md`](CA_BUNDLE.md), which is binding: it comes from
**the exact throwaway project used for the test**, from that project's Database
Settings -> SSL Configuration, with the project reference recorded first. It is
**not** assumed to be shared across projects -- that was claimed once, was an
inference from a filename rather than a documented fact, and is withdrawn.

This means the build happens **inside** the exposure window, after the project
exists. Everything else -- vendoring, code, tests -- must therefore be finished
beforehand, so the in-window build is one file plus a zip.

There is no "build without it" path. `build_bundle.py` exits non-zero if the
file is missing, empty, malformed, expired, or absent from the finished ZIP.

## Assemble

```
bundle/
├── main.py
├── ca.py                 # stdlib-only; shared with the build gate
├── ca-bundle.pem         # REQUIRED; the build fails without it
├── app-config.json
├── requirements.txt      # a comment only; Catalyst does not read it
└── vendor/
```

Zip the **contents**, so `main.py` sits at the archive root. Exclude `__pycache__` and `*.pyc`.

Use the script rather than zipping by hand -- it assembles and then re-opens and
verifies the artefact, because checking the source tree is not the same as
checking the thing that gets uploaded:

```bash
py docs/phase-0b/probe/build_bundle.py --out wbs-phase0b-probe.zip --vendor <staging>/vendor
```

This is the **only** command that runs inside the exposure window. It performs
no network access: the tree is already staged, and the sole missing input is
`ca-bundle.pem`. Measured on a rehearsal build with a synthetic CA: **480 files,
3.52 MB, ~1.1 s**.

## Verify before uploading

`build_bundle.py` already performs every check below on the finished archive.
The manual form is kept because a reviewer should be able to verify a bundle
without trusting the script that produced it.

```bash
python - <<'EOF'
import zipfile
z = zipfile.ZipFile("bundle.zip"); n = z.namelist()
assert "main.py" in n and "app-config.json" in n
assert any(x.endswith("cpython-313-x86_64-linux-gnu.so") for x in n), "missing Linux pydantic_core"
assert not [x for x in n if x.endswith(".pyd")], "Windows binaries present"
for m in ("fastapi", "pg8000", "scramp", "uvicorn"):
    assert f"vendor/{m}/__init__.py" in n, m
print("OK:", len(n), "files")
EOF
```

The `.pyd` assertion is the one that matters most: a Windows binary slipping in reproduces the original failure while the bundle looks correct.

## Local checks — run before any deployment

```bash
python -m pytest docs/phase-0b/probe/test_probe.py -q
```

92 tests: authorisation, SSRF surface, secret containment, deadline behaviour, concurrency, TLS verification, CA pinning and fail-closed behaviour, stage-skip attribution, and the Q-B claim guard. The CA tests run against committed synthetic fixtures under `fixtures/`, so they need neither the real Supabase certificate nor a certificate-parsing dependency.

**Run these WITHOUT the vendor directory on `PYTHONPATH`.** The vendored `pydantic_core` is a Linux binary and cannot load on Windows or macOS — by design. Verify the vendored `pg8000` separately, since it is pure Python and imports anywhere:

```bash
PYTHONPATH=./vendor python -c "import pg8000.dbapi, scramp; print(pg8000.__version__)"
```

**The full bundle can only be executed on Linux.** Its import path is proven when it runs on AppSail, not before — the same limitation the Phase 0A spike carried.

## Deploy — only after the Supabase stop gate clears

Console → AppSail → **Create Deployment** on the existing `wbs-platform-spike`. Python 3.13, command `python3 -u main.py`, port 9000, 512 MB, **Development only**.

Temporary environment variables — removed at cleanup, never committed:

| Variable | Purpose |
|---|---|
| `PROBE_TOKEN` | High-entropy one-time token for `POST /probe` |
| `PGHOST_DIRECT` | P1 — Supabase direct host (IPv6, 5432) |
| `PGHOST_POOLER` | P2 — Supabase session pooler host (IPv4, 5432) |
| `PGUSER` / `PGPASSWORD` / `PGDATABASE` | Ephemeral role and throwaway database |

Omitting a host variable simply removes that endpoint from the table — the probe cannot be asked for an endpoint it was not configured with.

**No variable for a transaction pooler exists.** P3 and port 6543 are Stage 2 and are rejected by the request schema regardless of configuration.

## Invoke

```bash
curl -s -X POST https://<service-url>/probe \
  -H "X-Probe-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"endpoint":"P1"}' | python -m json.tool
```

Then repeat for `P2`. The token goes in a **header**, never a query string, so it cannot leak through logs or history.

`GET /healthz` is public and touches no database.
