"""Registry and CSP-discipline checks for the Budget Setup / Budget
Categories frontend stream (Fable 5.1, migration 026's screens).

Pure Python, source-level, no browser and no database — the visual-regression
suite (tests/vrt/) is out of scope for this stream and is not touched here.
What IS checkable cheaply, on every commit:

  1. Both new screen ids (budget-setup, budget-categories) are declared in
     BOTH app/frontend/app.js's SCR_ROUTES table and
     app/frontend/src/core/router.js's SCREENS array, with the same `need`
     permission list in both places — the same discipline
     tests/vrt/spa-routing.spec.js holds the other thirteen ids to, checked
     here without a browser.
  2. None of the new JS files ever emits a `style="` attribute — the AST/CSP
     gate every other screen in this codebase is held to
     (app/frontend/src/core/dom.js::h() refuses one structurally; this
     confirms nothing here tries to route around that by building the
     attribute as a raw string).
  3. Every `var(--token)` referenced in the new CSS is a custom property
     app/frontend/styles.css actually declares in its `:root` block — no
     invented token, in the one stylesheet this stream adds outright.
  4. The eleven analytics-* and four mapping-*/connector-audit SCR_ROUTES
     ids, previously deep-link-only, are now referenced from app.js's NAV
     table (each inside a `scr('...')` splice), which is what makes them
     reachable from the rail rather than merely bookmarkable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "app" / "frontend"

APP_JS = FRONTEND / "app.js"
ROUTER_JS = FRONTEND / "src" / "core" / "router.js"
STYLES_CSS = FRONTEND / "styles.css"

NEW_JS_FILES = [
    FRONTEND / "src" / "features" / "budget" / "budget-setup.js",
    FRONTEND / "src" / "components" / "budget" / "governed-select.js",
    FRONTEND / "src" / "features" / "settings" / "budget-categories.js",
    # Fable 5.1: exchange-rate administration (migration 028).
    FRONTEND / "src" / "features" / "settings" / "fx-rates.js",
    FRONTEND / "src" / "features" / "settings" / "fx-api.js",
]
NEW_CSS_FILES = [
    FRONTEND / "src" / "features" / "budget" / "budget-setup.css",
    FRONTEND / "src" / "features" / "settings" / "settings-fx.css",
]

NEW_SCREEN_IDS = {
    "budget-setup": ["budget.read"],
    "budget-categories": ["settings.read"],
    "fx-rates": ["fx.read"],
}

ANALYTICS_IDS = [
    "analytics-executive", "analytics-controller", "analytics-project-list",
    "analytics-project-object", "analytics-wbs-explorer", "analytics-wbs-tree",
    "analytics-wbs-element", "analytics-cwip-ledger", "analytics-commitment-ageing",
    "analytics-cwip-ageing", "analytics-exceptions",
]
MAPPING_IDS = ["mapping-master", "mapping-fields", "mapping-sync", "connector-audit"]


@pytest.fixture(scope="module")
def app_js_text() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_js_text() -> str:
    return ROUTER_JS.read_text(encoding="utf-8")


# ======================================================================
# 1. The two new screens are registered, with matching `need`, in both
#    app.js's SCR_ROUTES and router.js's SCREENS.
# ======================================================================
@pytest.mark.parametrize("screen_id", sorted(NEW_SCREEN_IDS))
def test_new_screen_id_in_app_js_scr_routes(app_js_text: str, screen_id: str) -> None:
    assert f"id: '{screen_id}'" in app_js_text, (
        f"app.js SCR_ROUTES does not declare a row for {screen_id!r}."
    )


@pytest.mark.parametrize("screen_id", sorted(NEW_SCREEN_IDS))
def test_new_screen_id_in_router_js_screens(router_js_text: str, screen_id: str) -> None:
    assert f"id: '{screen_id}'" in router_js_text, (
        f"router.js SCREENS does not declare a row for {screen_id!r}."
    )


def _need_list_after(text: str, screen_id: str) -> list[str] | None:
    """The `need: [...]` array on the same object literal as `id: '<screen_id>'`,
    searched within a bounded window after the id so a later, unrelated
    `need:` in the file is never picked up by accident."""
    marker = f"id: '{screen_id}'"
    idx = text.find(marker)
    if idx == -1:
        return None
    window = text[idx: idx + 400]
    m = re.search(r"need:\s*\[([^\]]*)\]", window)
    if not m:
        return None
    return [tok.strip().strip("'\"") for tok in m.group(1).split(",") if tok.strip()]


@pytest.mark.parametrize("screen_id,expected_need", sorted(NEW_SCREEN_IDS.items()))
def test_new_screen_need_matches_in_app_js(app_js_text: str, screen_id: str, expected_need: list[str]) -> None:
    need = _need_list_after(app_js_text, screen_id)
    assert need == expected_need, (
        f"app.js SCR_ROUTES[{screen_id!r}].need is {need!r}, expected {expected_need!r}."
    )


@pytest.mark.parametrize("screen_id,expected_need", sorted(NEW_SCREEN_IDS.items()))
def test_new_screen_need_matches_in_router_js(router_js_text: str, screen_id: str, expected_need: list[str]) -> None:
    need = _need_list_after(router_js_text, screen_id)
    assert need == expected_need, (
        f"router.js SCREENS[{screen_id!r}].need is {need!r}, expected {expected_need!r}."
    )


def test_new_screens_have_a_nav_row(app_js_text: str) -> None:
    """Both ids are spliced into NAV via `scr('<id>')`, not deep-link-only —
    Budget Setup and Budget Categories are meant to be reachable from the
    rail, unlike the analytics/mapping ids before this change."""
    for screen_id in NEW_SCREEN_IDS:
        assert f"scr('{screen_id}')" in app_js_text, (
            f"NAV does not splice scr({screen_id!r}) — the screen is registered but not on the rail."
        )


# ======================================================================
# 2. No `style="` attribute anywhere in the new JS files — the CSP gate.
# ======================================================================
def _strip_js_block_comments(source: str) -> str:
    """Remove /* ... */ comments so prose ABOUT style="" (this file's own
    module docstrings discuss the CSP rule at length) is never mistaken for
    the attribute itself. Good enough for this codebase's comment style —
    it does not need to handle a `/*` inside a string literal, and none of
    these files have one."""
    return re.sub(r"/\*[\s\S]*?\*/", "", source)


@pytest.mark.parametrize("path", NEW_JS_FILES, ids=lambda p: p.relative_to(FRONTEND).as_posix())
def test_no_style_attribute_in_new_js(path: Path) -> None:
    assert path.exists(), f"Expected new file {path} does not exist."
    source = _strip_js_block_comments(path.read_text(encoding="utf-8"))
    # A literal style="..." attribute (double or single quoted), the thing
    # core/dom.js::h() refuses to build and style-src 'self' refuses to run.
    offenders = re.findall(r"\bstyle\s*=\s*[\"']", source)
    assert not offenders, f"{path} contains a style=\"...\" attribute: {offenders}"


# ======================================================================
# 3. Every var(--token) in the new CSS is declared in styles.css :root.
# ======================================================================
def _root_block(css_text: str) -> str:
    m = re.search(r":root\s*\{(.*?)\}", css_text, re.DOTALL)
    assert m, "styles.css has no :root block to check custom properties against."
    return m.group(1)


@pytest.fixture(scope="module")
def declared_tokens() -> set[str]:
    root_block = _root_block(STYLES_CSS.read_text(encoding="utf-8"))
    return set(re.findall(r"--[A-Za-z0-9-]+(?=\s*:)", root_block))


@pytest.mark.parametrize("path", NEW_CSS_FILES, ids=lambda p: p.relative_to(FRONTEND).as_posix())
def test_new_css_vars_are_declared(path: Path, declared_tokens: set[str]) -> None:
    assert path.exists(), f"Expected new file {path} does not exist."
    css_text = path.read_text(encoding="utf-8")
    used = set(re.findall(r"var\((--[A-Za-z0-9-]+)\)", css_text))
    undeclared = used - declared_tokens
    assert not undeclared, (
        f"{path} references undeclared custom propert{'y' if len(undeclared) == 1 else 'ies'} "
        f"not in styles.css :root: {sorted(undeclared)}"
    )
    # And the file earns the "token-only" claim: no raw hex colour literal.
    hex_colours = re.findall(r"#[0-9A-Fa-f]{3,8}\b", css_text)
    assert not hex_colours, f"{path} contains a raw hex colour, not a var(--token): {hex_colours}"


def test_new_css_files_are_nonempty() -> None:
    for path in NEW_CSS_FILES:
        assert path.exists(), f"Expected new CSS file {path} does not exist."
        assert path.stat().st_size > 0, f"{path} is empty."


# ======================================================================
# 4. The analytics/mapping ids are now referenced from NAV.
# ======================================================================
@pytest.mark.parametrize("screen_id", ANALYTICS_IDS + MAPPING_IDS)
def test_analytics_and_mapping_ids_referenced_from_nav(app_js_text: str, screen_id: str) -> None:
    assert f"scr('{screen_id}')" in app_js_text, (
        f"NAV does not splice scr({screen_id!r}) — this id is still deep-link-only."
    )


def test_analytics_and_mapping_groups_declared(app_js_text: str) -> None:
    assert "ANALYTICS" in app_js_text, "No ANALYTICS nav group declared in app.js."
    assert "INTEGRATION MAPPING" in app_js_text, "No INTEGRATION MAPPING nav group declared in app.js."


def test_new_nav_groups_default_collapsed(app_js_text: str) -> None:
    """New groups must start collapsed (`defaultExpanded: false`); existing
    groups are untouched and therefore carry no `defaultExpanded` at all,
    which renderNav() treats as expanded."""
    for group_name in ("ANALYTICS", "INTEGRATION MAPPING"):
        idx = app_js_text.find(f"g: '{group_name}'")
        assert idx != -1, f"No {{ g: '{group_name}' }} marker found in NAV."
        window = app_js_text[idx: idx + 120]
        assert "defaultExpanded: false" in window, (
            f"{group_name} nav group does not declare defaultExpanded: false."
        )


def test_nav_groups_are_collapsible_buttons(app_js_text: str) -> None:
    """renderNav() must render a group marker as an interactive, stateful
    toggle (aria-expanded + a data-nav-group hook the click handler reads),
    not a bare heading."""
    assert "data-nav-group" in app_js_text
    assert "aria-expanded" in app_js_text
    assert re.search(r"sessionStorage\.(get|set)Item\(NAV_GROUP_STATE_PREFIX", app_js_text), (
        "Nav group expand/collapse state is not persisted via sessionStorage."
    )
    assert "localStorage" not in app_js_text.split("NAV_GROUP_STATE_PREFIX", 1)[-1][:400], (
        "Nav group state must use sessionStorage, never localStorage."
    )
