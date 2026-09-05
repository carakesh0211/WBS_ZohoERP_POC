"""Every name a backend module uses must actually exist.

Python resolves a global name at CALL time, so a function that references
something undefined imports cleanly, passes every test that does not reach
that line, and raises `NameError` in production the first time a user takes
the path.

That happened twice in Wave 2, both on the tax-identity reveal:

  * `api/settings.py` called `_reveal_entity_tax_identity` while the
    definition had been deleted -- collateral from an integration patch that
    replaced a block of helper functions. Every entity reveal raised
    NameError inside the session, rolled the transaction back and returned
    500, so no entity reveal ever succeeded and none was ever audited.
  * `api/masters.py` and `api/settings.py` both lost `_reveal_context` the
    same way, which broke `?reveal=true` on both routers.

Neither showed up locally: the reveal paths need a live PostgreSQL, so every
test touching them skipped, and the whole local suite stayed green. An
adversarial reviewer found the first; CI found the second. Both are the same
five-line check, run on source, in under a second.

This is deliberately narrow. It resolves module globals, builtins, imports,
class and function definitions, assignments, comprehension and function
locals -- and reports what is left. It is not a type checker and does not
try to be.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"


def _modules() -> list[Path]:
    return sorted(p for p in BACKEND.rglob("*.py") if not p.name.startswith("__"))


def _bound_anywhere(tree: ast.AST) -> set[str]:
    """Every name bound anywhere in the module.

    Deliberately flat rather than scope-aware: a name bound in one function
    and used in another is a bug this test is not trying to find, and being
    generous here keeps it free of false alarms. It still catches a name that
    is bound NOWHERE, which is the failure that shipped.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for group in (args.posonlyargs, args.args, args.kwonlyargs):
                    bound.update(a.arg for a in group)
                for maybe in (args.vararg, args.kwarg):
                    if maybe is not None:
                        bound.add(maybe.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, (ast.Lambda,)):
            for group in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs):
                bound.update(a.arg for a in group)
    return bound


def _undefined(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = _bound_anywhere(tree) | set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    loaded = {(n.id, n.lineno) for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(f"{name} (line {line})" for name, line in loaded if name not in known)


@pytest.mark.parametrize("module", _modules(),
                         ids=lambda p: str(p.relative_to(BACKEND)))
def test_every_name_a_module_uses_is_defined_somewhere_in_it(module):
    undefined = _undefined(module)
    assert not undefined, (
        f"{module.relative_to(BACKEND)} uses names bound nowhere in the "
        f"module. Python resolves these at call time, so this imports "
        f"cleanly and raises NameError the first time the path runs:\n  "
        + "\n  ".join(undefined))


def test_the_check_catches_a_deleted_helper(tmp_path):
    """The exact shape of the defect: a call to a helper that no longer
    exists, in a module that still imports and still passes every test that
    does not reach the line."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def handler(session, row_id):\n"
        "    return _reveal_entity_tax_identity(session, row_id)\n",
        encoding="utf-8")
    undefined = _undefined(planted)
    assert undefined and undefined[0].startswith("_reveal_entity_tax_identity")


def test_the_check_does_not_flag_ordinary_code(tmp_path):
    """A gate with false alarms gets switched off, so this pins the common
    shapes it must stay quiet about."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "CONST = 1\n"
        "class Thing:\n"
        "    attr = 2\n"
        "def f(a, *args, b=1, **kwargs):\n"
        "    total = sum(x for x in args)\n"
        "    try:\n"
        "        json.dumps(CONST)\n"
        "    except ValueError as exc:\n"
        "        return str(exc)\n"
        "    return [Path(p) for p in kwargs] + [total, a, b, Thing]\n",
        encoding="utf-8")
    assert _undefined(planted) == []


def test_there_are_modules_to_check():
    names = {str(p.relative_to(BACKEND)) for p in _modules()}
    assert len(names) > 10
    assert any(n.startswith("api") for n in names)
    assert any(n.startswith("pg") for n in names)


# ==========================================================================
# The half the check above does not cover
# ==========================================================================
@pytest.mark.parametrize("module", _modules(),
                          ids=lambda p: str(p.relative_to(BACKEND)))
def test_no_function_references_a_name_bound_in_no_scope(module: Path) -> None:
    """A name used inside a function must be bound in SOME enclosing scope.

    The check above resolves module globals, and that is genuinely all it
    resolves. It cannot see a name that is neither a global nor bound anywhere
    in the function that uses it -- the shape you get by deleting a parameter
    while leaving its uses behind, or by threading an argument through a call
    site and forgetting the signature.

    That is not hypothetical. Widening `_set_instance_status` to carry a
    `closing_actor` meant passing `actor_user_id` from `_apply_decision`, which
    did not take one. The whole local suite stayed green -- `decide` needs a
    live PostgreSQL, so every test that would have executed the line skipped --
    and CI reported eleven `NameError`s twenty-five minutes later. Same class
    of defect as the one this module was written for, and the same reason it
    hid: a skip is not a pass, and a green local run says nothing about a line
    no local test can reach.

    `symtable` answers this exactly, because it is the same analysis the
    compiler does when it decides whether a name is local, free or global. A
    name that is referenced but is not a parameter, not assigned, not free,
    not declared global or nonlocal, and not resolvable at module level, is
    unbound at every call -- a guaranteed `NameError` the moment the line runs.
    """
    import builtins
    import symtable

    source = module.read_text(encoding="utf-8")
    table = symtable.symtable(source, module.name, "exec")

    module_names = set(table.get_identifiers()) | set(dir(builtins))
    unbound: list[str] = []

    def visit(scope, path: str) -> None:
        for child in scope.get_children():
            here = f"{path}.{child.get_name()}"
            if child.get_type() == "function":
                for symbol in child.get_symbols():
                    if not symbol.is_referenced():
                        continue
                    if (symbol.is_parameter() or symbol.is_assigned()
                            or symbol.is_free() or symbol.is_imported()):
                        continue
                    # `is_global()` is NOT an exemption on its own, and that
                    # subtlety is the whole test. symtable marks a name
                    # "global" whenever it is neither local nor free -- which
                    # is precisely what an unbound name looks like. Treating
                    # that as "resolved elsewhere" is what let the original
                    # defect through the first version of this check. A global
                    # reference is fine only if the module or builtins really
                    # define the name.
                    if symbol.get_name() in module_names:
                        continue
                    unbound.append(f"{here}: {symbol.get_name()}")
            visit(child, here)

    visit(table, module.stem)
    assert not unbound, (
        f"{module.relative_to(BACKEND)} references names bound in no scope, "
        f"which raise NameError the moment the line executes:\n  "
        + "\n  ".join(unbound))
