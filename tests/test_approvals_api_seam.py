"""Static check of the API/engine seam in ``app/backend/api/approvals.py``.

Why this file exists
--------------------
``api/approvals.py`` reaches the engine through one adapter::

    _call_engine(key, (name, alias, ...), session, request=request, **kwargs)

That adapter was built while the engine modules were not visible, so it accepts
a TUPLE of acceptable names and degrades to a clean 503 when it finds none of
them. The degradation is the problem this test exists for. A seam designed to
tolerate an unknown name will, by construction, fail SILENTLY: a route whose
engine function was renamed, or never written, answers
``503 APPROVAL_ENGINE_UNAVAILABLE`` -- which reads as "this deployment has no
database" and not as "this route has been broken since it was merged". Every
one of the eight routes listed in the Wave 4 stream A1 brief was in exactly
that state, and no runtime test caught any of them, because a runtime test of
those routes skips without a live PostgreSQL and a 503 is what a skipped
environment produces anyway.

The keyword half fails the other way and is no better. ``_call_engine`` finds
the function, calls it, and a mismatched keyword raises ``TypeError`` -- which
carries no ``.code``, so ``_raise_for_engine_error`` re-raises it and the route
answers 500. A control refusing correctly and a server fault then look
identical to a caller.

So the seam is checked STATICALLY, here, with no database and no import of
FastAPI's app: parse the router, resolve every ``_call_engine`` site against
the real engine module, and assert that the named function exists and that the
keywords passed to it are keywords it accepts. A rename on either side of the
seam fails this test loudly at collection time, in every environment, with the
line number of the call site.

This is a *shape* check, not a behaviour check. It cannot tell you the engine
does the right thing -- ``tests/test_approval_e2e.py`` and
``tests/test_approvals_api_guard.py`` do that. It tells you the two halves can
talk to each other at all, which is the precondition those tests silently
assumed.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

#: The three engine modules the router is allowed to call, under the keys
#: ``_call_engine`` uses. Kept in sync with ``api/approvals.py::_ENGINE_MODULES``
#: by :func:`test_engine_module_map_matches_the_router`, so this table cannot
#: drift into checking a module the router no longer calls.
ENGINE_MODULES = {
    "approvals": "app.backend.pg.approvals",
    "rules": "app.backend.pg.approval_rules",
    "delegation": "app.backend.pg.delegation",
}

ROUTER_PATH = (Path(__file__).resolve().parents[1]
               / "app" / "backend" / "api" / "approvals.py")


def _router_tree() -> ast.Module:
    return ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))


class _CallSite:
    """One resolved ``_call_engine(...)`` call in the router."""

    def __init__(self, node: ast.Call) -> None:
        self.lineno = node.lineno
        self.key = ast.literal_eval(node.args[0])
        self.names = tuple(ast.literal_eval(node.args[1]))
        # `request=` is consumed by `_call_engine` itself and never forwarded.
        self.kwargs = sorted(kw.arg for kw in node.keywords
                             if kw.arg and kw.arg != "request")
        # args[0] and args[1] are the adapter's own; everything after is
        # forwarded positionally (in practice exactly `session`).
        self.positional = len(node.args) - 2

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"L{self.lineno} {self.key}.{self.names}"


def _call_sites() -> list[_CallSite]:
    sites: list[_CallSite] = []
    for node in ast.walk(_router_tree()):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_call_engine":
            continue
        try:
            sites.append(_CallSite(node))
        except ValueError as exc:                       # non-literal arguments
            raise AssertionError(
                f"{ROUTER_PATH.name}:{node.lineno}: _call_engine was given a "
                f"non-literal module key or name tuple, so the seam cannot be "
                f"checked statically. Both must be literals: that is the whole "
                f"reason this test can exist. ({exc})") from exc
    return sites


def test_the_router_has_call_sites_to_check():
    """A seam test that silently checks nothing is worse than no seam test."""
    sites = _call_sites()
    assert len(sites) >= 12, (
        f"only {len(sites)} _call_engine sites found in {ROUTER_PATH.name}. "
        f"Either the router stopped using the adapter -- in which case this "
        f"test no longer covers the seam and must be rewritten, not deleted -- "
        f"or the parse is wrong.")


def test_engine_module_map_matches_the_router():
    """This file's module table is the router's, not a second opinion."""
    module = importlib.import_module("app.backend.api.approvals")
    assert module._ENGINE_MODULES == ENGINE_MODULES, (
        "api/approvals.py::_ENGINE_MODULES and this test's ENGINE_MODULES "
        "disagree. The router may have gained an engine module this test does "
        "not check, which is exactly the blind spot the test exists to close.")


@pytest.mark.parametrize("site", _call_sites(), ids=repr)
def test_every_engine_call_site_resolves_to_a_real_function(site: _CallSite):
    """Every ``_call_engine`` name tuple names a function that EXISTS.

    ``_call_engine`` answers 503 APPROVAL_ENGINE_UNAVAILABLE when none of the
    names is callable, which is indistinguishable from an unconfigured
    deployment. That is the failure this asserts away.
    """
    module = importlib.import_module(ENGINE_MODULES[site.key])
    resolved = [name for name in site.names
                if callable(getattr(module, name, None))]
    assert resolved, (
        f"{ROUTER_PATH.name}:{site.lineno} calls "
        f"{site.key}.{site.names} but {ENGINE_MODULES[site.key]} defines none "
        f"of those names. The route does not 500 -- it answers 503 "
        f"APPROVAL_ENGINE_UNAVAILABLE, which looks like a missing database.")


@pytest.mark.parametrize("site", _call_sites(), ids=repr)
def test_every_engine_call_site_passes_arguments_the_engine_accepts(site: _CallSite):
    """Every forwarded keyword is one the resolved function accepts, and every
    required parameter of that function is supplied.

    A mismatched keyword is a ``TypeError`` with no ``.code``, which
    ``_raise_for_engine_error`` re-raises as a 500 -- so an unenforceable
    control and a server fault are the same response to a caller.
    """
    module = importlib.import_module(ENGINE_MODULES[site.key])
    fn = next((getattr(module, name) for name in site.names
               if callable(getattr(module, name, None))), None)
    if fn is None:
        pytest.skip("covered by test_every_engine_call_site_resolves_to_a_real_function")

    signature = inspect.signature(fn)
    parameters = signature.parameters
    if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
        # A **kwargs engine function accepts anything, so there is nothing to
        # check -- and nothing to protect, either. Flagged rather than passed
        # silently: **kwargs on an engine entry point defeats this test.
        pytest.fail(
            f"{ENGINE_MODULES[site.key]}.{fn.__name__} takes **kwargs, which "
            f"makes the seam uncheckable. Name the parameters.")

    unexpected = [name for name in site.kwargs if name not in parameters]
    required = [p.name for p in parameters.values()
                if p.default is p.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    # Positional arguments forwarded by the router (`session`) fill the first
    # `site.positional` required parameters.
    missing = [name for name in required
               if name not in site.kwargs][site.positional:]

    assert not unexpected and not missing, (
        f"{ROUTER_PATH.name}:{site.lineno} -> "
        f"{ENGINE_MODULES[site.key]}.{fn.__name__}{signature}\n"
        f"  keywords the engine does not accept: {unexpected}\n"
        f"  required parameters the router does not supply: {missing}")
