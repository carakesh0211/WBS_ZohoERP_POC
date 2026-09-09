"""Frontend controls the Python suite can prove: money, permissions, names.

The visual-regression suite runs Playwright and lives under `tests/vrt/`. It
is a separate CI job with its own inventory, and the Python manifest gate
"sees no JavaScript at all" (`tests/vrt/vrt-inventory.spec.js` says so). That
leaves three properties which are checkable from here, cheaply, on every
commit, and which the VRT suite either does not cover or covers over ten files
out of eighty.

1. **No floating-point financial arithmetic anywhere in the frontend.**
   `tests/vrt/closure.spec.js` already forbids it -- across the ten files of
   one feature, fetched over HTTP from a running server. Money is rendered on
   every screen, so the scan has to be over every screen.

2. **L2: the screen gates on the permission the API enforces.**

3. **L6: per-row disclosure buttons have distinct accessible names.**

Nothing here needs a database or a browser.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "app" / "frontend"
BACKEND = PROJECT_ROOT / "app" / "backend"


# ======================================================================
# 1. Floating-point money
# ======================================================================
#: A line is suspect when it names money AND does float arithmetic on it.
#: Integer `+`/`-` on `*_paise` is exact below 2^53 and is deliberately NOT
#: matched: flagging it would bury the real findings in noise, and the
#: overflow case is covered by `analytics-metrics.js`'s BigInt accumulator.
MONEY_TOKENS = re.compile(r"paise|rupees|amount|budget|committed|actual|\binr\b",
                          re.IGNORECASE)
FLOAT_ARITHMETIC = re.compile(
    r"parseFloat\s*\(|\.toFixed\s*\(|/\s*100\b|/\s*1e[0-9]+\b|\*\s*100\b")

#: Files that STILL do float arithmetic on money, with the reason each is
#: still open and who owns it. Asserted in BOTH directions below: a new
#: offender fails because it is unregistered, and a registered file that has
#: been cleaned fails because the register is excusing nothing.
#:
#: Neither file belongs to this wave. Recording them here, with line numbers,
#: is what stops "we scanned the frontend" being mistaken for "the frontend is
#: clean" -- and what makes the fix a five-minute job for whoever owns them.
FLOAT_MONEY_OPEN_FINDINGS = {
    "app/frontend/app.js": (
        "LEAD-OWNED, and this wave is explicitly forbidden to edit it. Five "
        "sites: inr() rounds `Math.abs(paise) / 100` and DROPS THE PAISE "
        "(:38); inrShort() float-divides again and then by 1e7/1e5 (:51); the "
        "two budget bar widths are `commitment / budget * 100` (:105-106); and "
        "the PO amendment field is pre-filled with "
        "`(amount_paise / 100).toFixed(2)` (:1447), which is the exact "
        "paisa-eating pattern tests/vrt/closure.spec.js was written against. "
        "`src/core/format.js` already does the same job with integer "
        "floor/modulo and is the fix."),
    "app/frontend/src/components/budget/wbs-tree-table.js": (
        "Two bar-width ratios, `(commit / budget) * 100` (:88-89). Not a "
        "displayed money value, but a utilisation figure derived from money by "
        "float division, so a breach of a hair can round to 'at the line'. "
        "`features/analytics/analytics-metrics.js::basisPoints()` is the "
        "integer implementation this should use. Owner: the budget stream."),
}

JS_FILES = sorted(FRONTEND.rglob("*.js"))


def _float_money_lines(path: Path) -> list[tuple[int, str]]:
    """Suspect lines, with their real line numbers.

    Whole-line comments are skipped; block comments are NOT stripped with a
    dot-all regex, because one unbalanced `/*` inside a string then deletes
    hundreds of lines of real code and the scan silently passes. Skipping
    lines that BEGIN as comments costs nothing and cannot swallow code.
    """
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        if MONEY_TOKENS.search(line) and FLOAT_ARITHMETIC.search(line):
            found.append((number, line.strip()))
    return found


CLEAN_JS_FILES = [
    p for p in JS_FILES
    if p.relative_to(PROJECT_ROOT).as_posix() not in FLOAT_MONEY_OPEN_FINDINGS
]


@pytest.mark.parametrize("path", CLEAN_JS_FILES,
                         ids=lambda p: p.relative_to(FRONTEND).as_posix())
def test_no_frontend_module_does_float_arithmetic_on_money(path):
    """IEEE-754 cannot represent a rupee, and money is not a quantity to
    approximate. Every value crossing the wire is integer paise; the frontend's
    job is to format it, not to divide it."""
    hits = _float_money_lines(path)
    assert not hits, (
        f"{path.relative_to(PROJECT_ROOT).as_posix()} does float arithmetic on "
        f"money:\n" + "\n".join(f"  :{n} {line}" for n, line in hits))


def test_the_float_money_register_matches_reality():
    """Both directions, so the register can neither hide a new one nor
    outlive a fixed one."""
    offending = {
        p.relative_to(PROJECT_ROOT).as_posix()
        for p in JS_FILES if _float_money_lines(p)
    }
    registered = set(FLOAT_MONEY_OPEN_FINDINGS)

    assert offending - registered == set(), (
        "a frontend file does float money arithmetic and is not recorded: "
        f"{sorted(offending - registered)}")
    assert registered - offending == set(), (
        "FLOAT_MONEY_OPEN_FINDINGS excuses a file that no longer offends; "
        f"delete the entry and let the strict test cover it: "
        f"{sorted(registered - offending)}")


def test_the_registered_files_offend_at_the_lines_the_register_names():
    """A register that says "five sites" while four remain is a stale record."""
    app_js = FRONTEND / "app.js"
    lines = {n for n, _ in _float_money_lines(app_js)}
    assert len(lines) == 5, (
        f"app.js now has {len(lines)} float-money sites, not the five recorded "
        f"in FLOAT_MONEY_OPEN_FINDINGS: {sorted(lines)}")

    tree = FRONTEND / "src" / "components" / "budget" / "wbs-tree-table.js"
    assert len(_float_money_lines(tree)) == 2


def test_the_money_scanner_catches_the_pattern_it_is_written_against(tmp_path):
    """Mutation check on the scanner, including the case VRT already pins:
    `(paise / 100).toFixed(2)` silently eats the paise."""
    sample = tmp_path / "sample.js"
    sample.write_text(
        "// amount_paise / 100 in a comment must not count\n"
        "const rendered = (row.amount_paise / 100).toFixed(2);\n"
        "const safe = Math.floor(Math.abs(paise) / 100n);\n",
        encoding="utf-8")
    hits = _float_money_lines(sample)
    assert [n for n, _ in hits] == [2], (
        f"the scanner flagged {hits}; it must catch line 2 and ignore the "
        "comment on line 1.")


def test_the_integer_reference_implementation_is_still_there():
    """The scan is only actionable because a correct implementation exists."""
    fmt = (FRONTEND / "src" / "core" / "format.js").read_text(encoding="utf-8")
    assert "Math.floor" in fmt and "% 100" in fmt, (
        "core/format.js no longer formats money by integer floor/modulo, so "
        "there is nothing to point the registered offenders at.")


# ======================================================================
# 2. L2 -- the screen asks for the permission the API enforces
# ======================================================================
CONNECTOR_AUDIT_LOG = FRONTEND / "src" / "features" / "mapping" / "connector-audit-log.js"
AUDIT_ROUTER = BACKEND / "api" / "audit.py"


def test_the_connector_audit_screen_gates_on_the_permission_the_router_enforces():
    """L2. The screen said `connector.read`; the router checks `audit.read`.

    No live defect -- the two are held by exactly the same roles today -- but a
    screen that offers a control the API refuses is a defect waiting for a
    role change, and the withheld branch it renders instead was unreachable,
    so nobody would have seen it fail in review either.
    """
    screen = CONNECTOR_AUDIT_LOG.read_text(encoding="utf-8")
    match = re.search(r"const REVEAL_PERMISSION\s*=\s*'([^']+)'", screen)
    assert match, "REVEAL_PERMISSION is no longer a simple constant to read"
    declared = match.group(1)

    router = AUDIT_ROUTER.read_text(encoding="utf-8")
    enforced = re.search(r'require\([^,]+,\s*"([^"]+)"\)', router)
    assert enforced, "the audit router no longer names the permission it requires"

    assert declared == enforced.group(1), (
        f"the connector audit screen gates its reveal on {declared!r} while "
        f"the router behind it enforces {enforced.group(1)!r}. The screen "
        "would offer a control the API refuses.")


def test_the_two_permissions_are_still_identically_held_which_is_why_l2_was_invisible():
    """The reason this was a latent defect and not an outage, recorded.

    If these sets ever diverge, the old code would have started offering a
    control the API refuses -- and this assertion is where a reader finds out
    that the divergence is now possible.
    """
    from app.backend import auth

    assert set(auth.PERMISSIONS["connector.read"]) == set(auth.PERMISSIONS["audit.read"]), (
        "connector.read and audit.read now have different holders. That is "
        "allowed -- but it is exactly the change that would have made L2 a "
        "live defect, so confirm the screen still asks for audit.read.")


# ======================================================================
# 3. L6 -- per-row disclosure buttons
# ======================================================================
DATATABLE = FRONTEND / "src" / "components" / "capex-datatable.js"


def test_every_row_disclosure_button_has_its_own_accessible_name():
    """L6. Twenty buttons all called "Details" are twenty identical names.

    A sighted user disambiguates by the row the button sits in. A screen
    reader listing the controls on the page gets "Details, button" twenty
    times over, with nothing to choose between them.
    """
    source = DATATABLE.read_text(encoding="utf-8")
    assert "'aria-label':" in source, (
        "the per-row disclosure button has no aria-label, so every one of them "
        "is named 'Details'.")
    assert "rowLabel(" in source, (
        "the aria-label is not derived from the row, so all of them would "
        "still be identical.")
    assert "'aria-controls': detailId" in source, (
        "the button does not say which element it discloses, so a screen "
        "reader user cannot move to the content it opened.")
    assert re.search(r"id:\s*detailId", source), (
        "aria-controls points at an id no element carries, which is worse "
        "than omitting it: assistive technology follows it nowhere.")
    assert "'aria-expanded': 'false'" in source, (
        "the button no longer announces its state.")


def test_the_visible_label_is_unchanged_so_the_specs_that_assert_on_it_still_pass():
    """The fix must not move the assertions in specs this wave does not own.

    `tests/vrt/audit-trail.spec.js` matches the button by `textContent`, and
    `tests/vrt/mapping.spec.js` by `:has-text("Details")`; three pixel
    baselines contain the rendered word. An `aria-label` overrides the
    accessible name without touching any of them, which is why the fix took
    that shape rather than renaming the button.
    """
    source = DATATABLE.read_text(encoding="utf-8")
    assert "'Details',\n" in source or "'Details'," in source
    assert "'Hide details'" in source, (
        "the expanded-state text has changed; the audit-trail keyboard "
        "traversal spec asserts on the collapsed one and the pixel baselines "
        "on both.")


def test_the_row_label_falls_back_rather_than_producing_duplicates():
    """A row whose first column is empty must still get a unique name.

    The fallback is the ordinal. It is worse copy than an identity, and far
    better than another identical name.
    """
    source = DATATABLE.read_text(encoding="utf-8")
    assert "`row ${index + 1}`" in source, (
        "rowLabel has no ordinal fallback, so rows with an empty first column "
        "would go back to sharing one name.")


def test_the_detail_row_id_is_unique_across_tables_on_one_page():
    """Two tables on one screen must not mint the same id.

    `aria-controls` is resolved by id, and a duplicate id means half the
    buttons point at the other table's rows.
    """
    source = DATATABLE.read_text(encoding="utf-8")
    assert "let tableSequence = 0;" in source, (
        "the id counter is not module-scoped, so two independently created "
        "tables would both start at 1.")
    assert "tableSequence += 1;" in source
    assert "`capex-dt-${tableSequence}`" in source
