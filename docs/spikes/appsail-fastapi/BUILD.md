# Rebuilding the AppSail deployment bundle

Reproduces the exact artefact whose SHA-256 is recorded in [`EVIDENCE.md`](EVIDENCE.md).

## The constraint

Catalyst's managed runtime does **not** install dependencies. Every module must be present in the uploaded build path. The AppSail container is **Linux x86_64, CPython 3.13**.

> **Never copy Windows `site-packages`.** `pydantic_core` ships a compiled extension — the bundle needs `_pydantic_core.cpython-313-x86_64-linux-gnu.so`. A Windows `.pyd` produces exactly the same opaque `503 "Execution failed. Please check the startup command or port."` as a missing package, so the failure mode is indistinguishable and expensive to diagnose.

## Option A — on Linux, or in a Linux container (preferred)

```bash
pip install --target ./vendor --require-hashes -r vendored-versions.txt
```

If not already on Linux x86_64 with CPython 3.13, run it inside one:

```bash
docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
  pip install --target ./vendor -r vendored-versions.txt
```

## Option B — cross-platform, no Docker

Download platform-correct wheels explicitly. This is how the committed artefact was produced, because the build host was Windows and had no `pip`.

```bash
pip download -r vendored-versions.txt -d wheels \
  --platform manylinux2014_x86_64 \
  --python-version 313 \
  --implementation cp \
  --only-binary=:all:
cd vendor && for w in ../wheels/*.whl; do unzip -o "$w"; done
```

For each package prefer the `py3-none-any` wheel; where none exists — only `pydantic-core` — take `cp313-…-manylinux…x86_64`, never `musllinux`, never `win_amd64`.

## Assemble

```
bundle/
├── main.py
├── app-config.json
├── requirements.txt      # a comment only; Catalyst does not read it
└── vendor/               # all dependencies, flat
```

Zip **the contents**, not the containing folder — `main.py` must sit at the archive root. Exclude `__pycache__` and `*.pyc`.

## Verify before uploading

```bash
python - <<'EOF'
import zipfile
z = zipfile.ZipFile("bundle.zip"); n = z.namelist()
assert "main.py" in n and "app-config.json" in n
assert any(x.endswith("cpython-313-x86_64-linux-gnu.so") for x in n), "missing Linux pydantic_core"
assert not [x for x in n if x.endswith(".pyd")], "Windows binaries present"
assert "vendor/fastapi/__init__.py" in n
print("OK:", len(n), "files")
EOF
```

The third assertion is the one that matters most: a Windows `.pyd` slipping in is the single most likely way to reproduce the original failure while believing the bundle is correct.

## Deploy

Console → AppSail → **Create Deployment** on the existing service. Runtime **Python 3.13**, command `python3 -u main.py`, port **9000**, memory **512 MB**, no environment variables, **Development only**.

## Confirm

```bash
curl -s https://<service-url>/ | python -m json.tool
```

Expect `"framework": "fastapi"` with `machine: x86_64` and `python: 3.13.x`. If the response instead says `"framework": "stdlib-fallback"`, the vendoring is wrong and the payload carries the exact import error — read it rather than guessing.
