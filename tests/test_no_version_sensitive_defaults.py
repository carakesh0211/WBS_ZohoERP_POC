"""No dataclass may carry a default Python 3.11's dataclasses would reject.

WHY THIS EXISTS
===============

`app/backend/integration/dto.py` declared five fields as
`= MappingProxyType({})`. Python 3.11's `dataclasses` decides "is this default
mutable?" by asking whether it is HASHABLE, and a `mappingproxy` is not — so
every one of those raised, at import:

    ValueError: mutable default <class 'mappingproxy'> for field dimensions
                is not allowed: use default_factory

Nothing local caught it. This machine runs Python 3.14, where that check was
relaxed. CI runs 3.11, which the project's own setup instructions specify. So
**the entire integration package failed to import in CI** and every integration
test errored during collection — the package had never once been imported
there, through the whole of Wave 5.

This is the second time this project has been bitten by exactly this shape.
The first was `ast.dump`, whose output is not stable across CPython releases,
which broke the test-manifest hashes on a version bump. Both are the same
defect: **a stdlib behaviour that differs between the development Python and
the build Python, hidden by the gap between them.**

WHAT THIS ENFORCES
==================

Python 3.11's rule, applied on whatever version happens to be running. A
default that 3.11 would refuse is a defect here even when the local
interpreter accepts it, because CI is the arbiter and CI is on 3.11.
"""
from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"


def _modules() -> list[str]:
    """Every importable module under `app.backend`."""
    names: list[str] = []
    for info in pkgutil.walk_packages([str(BACKEND)], prefix="app.backend."):
        if "__pycache__" in info.name:
            continue
        names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("module_name", _modules())
def test_no_dataclass_default_would_be_rejected_by_python_311(module_name: str):
    """A dataclass default must be hashable, which is 3.11's actual test.

    `default_factory` is the fix and is unaffected — this checks `default`
    only. `field(default_factory=...)` produces `MISSING` here and passes,
    which is correct: a factory builds a fresh object per instance and is
    never shared.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:                      # noqa: BLE001
        pytest.skip(f"{module_name} is not importable here: {exc}")

    offenders: list[str] = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name, None)
        if not dataclasses.is_dataclass(obj) or not isinstance(obj, type):
            continue
        # Only classes this module defines; an imported one is checked where
        # it lives, and reporting it twice would name the wrong file.
        if getattr(obj, "__module__", None) != module_name:
            continue
        for field in dataclasses.fields(obj):
            default = field.default
            if default is dataclasses.MISSING:
                continue
            try:
                hash(default)
            except TypeError:
                offenders.append(
                    f"{obj.__name__}.{field.name} = "
                    f"{type(default).__name__}, which is unhashable")

    assert not offenders, (
        f"{module_name} has dataclass defaults Python 3.11 REFUSES at import, "
        f"even though this interpreter accepted them. CI runs 3.11, so this "
        f"module would fail to import there and every test touching it would "
        f"error during collection. Use `field(default_factory=...)`:\n  "
        + "\n  ".join(offenders))
