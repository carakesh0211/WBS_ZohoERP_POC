"""Only the two adapter implementations may know which product is in use.

Plan v1.2.1 §11, the provisional-target box: *"No ERP-specific base URL, scope
string or endpoint is written into code."* Wave 5's C1 seam repeats it as an
absolute: *"Nothing may hardcode an ERP base URL, scope string or endpoint.
Product-specific facts live behind the adapter and its ``Capabilities``, never
in a caller."*

That is a property of the source, so it is checked against the source. If D-14
resolves to Books + Inventory, everything this test protects is the difference
between changing one module and auditing a codebase.

Two files predate Wave 5 and carry ERP literals already: the POC connector
``app/backend/zoho.py`` and one seed row in ``app/backend/db.py``. They are
recorded here as a **closed set**, in the ratchet style this repository already
uses for ``CONTRACT_GAPS.md`` -- a new one fails the build, and removing one of
these two fails the build until it is struck from the register, so the count
can only go down.

Source-only. No database, no network.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
INTEGRATION = BACKEND / "integration"

#: The two modules allowed to know a product. Everything else must be blind.
ADAPTER_IMPLEMENTATIONS = {"erp.py", "books_inventory.py"}

#: Files that carried product literals before Wave 5. A closed set: this test
#: fails if it grows AND if it shrinks without being edited, so the exception
#: register cannot quietly become a habit.
PRE_EXISTING = {
    "zoho.py": "POC connector, superseded by app/backend/integration/. Not Wave 5's to edit.",
    "db.py": "One seeded connection row naming the IN host. Owned by the schema stream.",
}

#: A Zoho API or accounts host.
HOST = re.compile(r"https?://(?:www\.)?(?:zohoapis|accounts\.zoho)")
#: A product-qualified OAuth scope: the prefix IS the product's name.
SCOPE = re.compile(r"\b(?:ERP|ZohoBooks|ZohoInventory)\.[a-z_]+\.(?:[A-Z]+|\*)")
#: A product endpoint path.
ENDPOINT = re.compile(
    r"[\"']/(?:erp|books|inventory)/v\d[\"']"
    r"|[\"']/(?:bills|purchaseorders|purchasereceives)(?:/|[\"'])")


def _docstrings(tree: ast.AST) -> set[int]:
    """Node ids of every docstring, so prose may name a product freely.

    Explaining what a module must *not* hardcode requires naming the thing.
    Only executable literals are the concern here; comments are not in the AST
    at all, so they are exempt for free.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _offending_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstrings(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        text = node.value
        quoted = f'"{text}"'
        if HOST.search(text) or SCOPE.search(text) or ENDPOINT.search(quoted):
            offenders.append(f"line {node.lineno}: {text[:70]!r}")
    return offenders


def _backend_modules() -> list[Path]:
    return sorted(BACKEND.rglob("*.py"))


@pytest.mark.parametrize(
    "module",
    [p for p in _backend_modules() if p.name not in ADAPTER_IMPLEMENTATIONS
     and p.name not in PRE_EXISTING],
    ids=lambda p: str(p.relative_to(BACKEND)).replace("\\", "/"))
def test_no_module_outside_the_adapters_hardcodes_a_product(module: Path):
    offenders = _offending_literals(module)
    assert not offenders, (
        f"{module.relative_to(BACKEND)} contains a Zoho host, OAuth scope or "
        f"product endpoint in executable code:\n  " + "\n  ".join(offenders) +
        "\n\nThe target product is provisional (D-14). Product-specific facts "
        "belong in app/backend/integration/erp.py or books_inventory.py, "
        "reached through the adapter and its Capabilities.")


def test_the_two_adapter_implementations_are_where_the_literals_actually_live():
    """The rule is containment, not absence. If neither file had any, the
    product knowledge would have leaked somewhere this test is not looking."""
    for name in sorted(ADAPTER_IMPLEMENTATIONS):
        found = _offending_literals(INTEGRATION / name)
        assert found, f"{name} defines no product literals; where did they go?"


def test_the_boundary_modules_themselves_stay_product_blind():
    """``adapter.py``, ``dto.py`` and ``__init__.py`` are the seam every other
    stream imports. A single scope string in one of them is the leak."""
    for name in ("adapter.py", "dto.py", "__init__.py"):
        offenders = _offending_literals(INTEGRATION / name)
        assert not offenders, f"integration/{name}: {offenders}"


def test_the_pre_existing_exception_register_is_exactly_two_files():
    """A ratchet, not an amnesty.

    Both files still exist and still carry the literals recorded against them.
    Adding a third means editing this set, in a diff a reviewer will see;
    cleaning one up means removing it, which is the direction this should move.
    """
    for name, reason in PRE_EXISTING.items():
        path = BACKEND / name
        assert path.exists(), (
            f"{name} is gone. Remove it from PRE_EXISTING -- the register must "
            f"not outlive the exception.")
        assert _offending_literals(path), (
            f"{name} no longer hardcodes a product. Strike it from "
            f"PRE_EXISTING so the register keeps shrinking. ({reason})")
    assert len(PRE_EXISTING) == 2


def test_the_check_would_catch_a_leak(tmp_path):
    """A gate nobody has seen fire is a gate nobody trusts."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        'BASE = "https://www.zohoapis.in/erp/v3"\n'
        'SCOPE = "ERP.bills.READ"\n'
        'def go():\n'
        '    return BASE + "/bills"\n',
        encoding="utf-8")
    found = _offending_literals(planted)
    assert len(found) == 3


def test_the_check_ignores_prose(tmp_path):
    """False alarms get gates switched off, and this one protects D-14."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""Never hardcode https://www.zohoapis.in/erp/v3 or ERP.bills.READ."""\n'
        'def go():\n'
        '    """Not /bills either. See ZohoInventory.items.READ."""\n'
        '    return 1\n',
        encoding="utf-8")
    assert _offending_literals(planted) == []
