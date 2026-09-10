"""Executable contract validation.

Closes AUD-M-001 - "Contracts and implementation can still drift without
detection." The frozen registries in research/30_contracts/ were previously
prose that nothing enforced. These tests make them executable.

This suite is a RATCHET, not a purity check. Every existing divergence is
recorded in CONTRACT_GAPS.md and asserted here as an exact set:

    - a NEW divergence fails the build
    - a CLOSED divergence fails the build until it is removed from the register

The gap count is not zero today. The point is that it can only go down
through a deliberate, reviewed decision.

No test here touches the database, the network or a Zoho tenant.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "research" / "30_contracts"
BACKEND = ROOT / "app" / "backend"
FRONTEND = ROOT / "app" / "frontend"


def contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def backend_source() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(BACKEND.glob("*.py"))
    )


# ===========================================================================
# C3 - the frozen business status registry
# ===========================================================================
def test_c3_holds_exactly_twenty_one_business_statuses():
    c3 = contract("C3_statuses.json")
    assert c3["count"] == 21
    assert len(c3["statuses"]) == 21, (
        "C3_statuses.json is frozen at 21 codes. Adding one requires client "
        "agreement - see CONTRACT_GAPS.md GAP-01."
    )


def test_c3_every_status_is_distinguishable_without_colour():
    """An accessibility requirement stated by the registry itself."""
    for s in contract("C3_statuses.json")["statuses"]:
        assert s["non_colour_indicator_required"] is True, s["code"]


def test_c3_semantic_roles_are_from_the_declared_set():
    c3 = contract("C3_statuses.json")
    allowed = set(c3["semantic_roles"])
    for s in c3["statuses"]:
        assert s["semantic_role"] in allowed, s["code"]


# ===========================================================================
# Registry separation - C3 vs C15 (approval) vs C16 (integration) vs C18 (domain)
#
# v1.1 of the plan wrongly implied every status maps into C3. It does not.
# These tests hold the four namespaces apart.
# ===========================================================================
def _c3_codes() -> set[str]:
    return {s["code"] for s in contract("C3_statuses.json")["statuses"]}


def _namespace_codes(filename: str) -> set[str]:
    doc = contract(filename)
    out: set[str] = set()
    for ns in doc["namespaces"].values():
        for s in ns.get("statuses", []):
            out.add(s["code"])
    return out


def test_approval_label_overlap_with_c3_is_exactly_the_reviewed_set():
    """C15 shares four LABELS with C3 - deliberately, because they are
    different facts in different namespaces (an approval instance being
    APPROVED is a decision on one object version; a purchase order being
    APPROVED is a document lifecycle state).

    The original version of this test asserted only that a prose string in the
    JSON began with the word "Disjoint". It computed no intersection and would
    have passed had C15 and C3 been byte-identical - and the prose it checked
    was factually wrong. This computes the real overlap so that a FIFTH,
    accidental collision fails the build.
    """
    doc = contract("C15_approval_statuses.json")
    declared = set(doc["reviewed_label_overlap_with_C3"])
    actual = _namespace_codes("C15_approval_statuses.json") & _c3_codes()
    assert actual == declared, (
        f"C15/C3 label overlap changed.\n"
        f"  newly colliding: {sorted(actual - declared)}\n"
        f"  no longer colliding: {sorted(declared - actual)}\n"
        "Each overlap must be a reviewed, deliberate decision recorded in "
        "reviewed_label_overlap_with_C3."
    )


def test_every_approval_status_mapping_targets_a_real_business_status():
    codes = _c3_codes()
    for ns in contract("C15_approval_statuses.json")["namespaces"].values():
        for s in ns.get("statuses", []):
            target = s.get("maps_to_business_status")
            if target is not None:
                assert target in codes, f"{s['code']} maps to unknown {target!r}"


def test_integration_statuses_are_disjoint_from_business_statuses():
    overlap = _namespace_codes("C16_integration_statuses.json") & _c3_codes()
    assert not overlap, (
        f"Integration job statuses leaked into the business namespace: {overlap}. "
        "Operational state belongs on SCR-26/38/39, never on a business screen."
    )


def test_domain_status_overlap_with_c3_is_exactly_the_reviewed_set():
    """Case-folded. C18 uses TitleCase and C3 SCREAMING_SNAKE, so a
    case-SENSITIVE comparison finds nothing - while 'Approved' vs 'APPROVED' is
    precisely the confusion the separation exists to prevent. The only thing
    between them was a shift key.
    """
    doc = contract("C18_domain_statuses.json")
    declared = {c.upper() for c in doc["reviewed_case_insensitive_overlap_with_C3"]["codes"]}
    actual = {c.upper() for c in _namespace_codes("C18_domain_statuses.json")} & {
        c.upper() for c in _c3_codes()
    }
    assert actual == declared, (
        f"C18/C3 case-insensitive overlap changed.\n"
        f"  newly colliding: {sorted(actual - declared)}\n"
        f"  no longer colliding: {sorted(declared - actual)}"
    )


def _namespace_codes_by_ns(filename: str) -> dict[str, set[str]]:
    doc = contract(filename)
    return {
        name: {s["code"] for s in ns.get("statuses", [])}
        for name, ns in doc["namespaces"].items()
    }


@pytest.mark.parametrize("filename,allowed", [
    # A code reused across namespaces is a real hazard: a job in DEAD and an
    # inbox row in DEAD are different operational facts, and SQL joining them
    # will not say so. Each reuse below is reviewed and deliberate.
    ("C16_integration_statuses.json", {"DEAD", "FAILED", "PENDING"}),
    ("C15_approval_statuses.json", {"APPROVED", "REJECTED", "RETURNED", "PENDING"}),
])
def test_cross_namespace_code_reuse_is_exactly_the_reviewed_set(filename, allowed):
    """The flattened disjointness tests above cannot see this - flattening into
    one set destroys exactly the information needed to detect it."""
    by_ns = _namespace_codes_by_ns(filename)
    seen: dict[str, list[str]] = {}
    for ns, codes in by_ns.items():
        for c in codes:
            seen.setdefault(c, []).append(ns)
    reused = {c for c, namespaces in seen.items() if len(namespaces) > 1}
    assert reused == allowed, (
        f"{filename}: cross-namespace code reuse changed.\n"
        f"  newly reused: {sorted(reused - allowed)}\n"
        f"  no longer reused: {sorted(allowed - reused)}"
    )


def test_every_integration_badge_value_derives_from_a_real_status():
    """The badge is what a business screen actually shows, so each value must
    trace to a declared integration status rather than float free."""
    doc = contract("C16_integration_statuses.json")
    bridge = doc["business_visible_bridge"]
    for badge in bridge["badge_values"]:
        assert badge in bridge["derivation"], f"badge {badge} has no derivation rule"


def test_the_only_business_visible_integration_bridge_is_the_two_c3_codes():
    """Integration state reaches a business screen through exactly two C3 codes
    plus a separate badge - never by rendering a job status as a status pill."""
    doc = contract("C16_integration_statuses.json")
    assert set(doc["business_visible_bridge"]["badge_values"]) == {
        "QUEUED", "SENT", "FAILED",
    }
    assert {"INTEGRATION_FAILED", "RECONCILIATION_PENDING"} <= _c3_codes()


# ===========================================================================
# C17 - raw Zoho status mapping
# ===========================================================================
def test_every_zoho_mapping_target_exists_in_the_business_registry():
    codes = _c3_codes()
    for block in contract("C17_zoho_status_map.json")["mappings"]:
        for row in block["rows"]:
            target = row["maps_to"]
            if target is None:
                continue  # a deliberate assertion of "no business meaning"
            assert target in codes, (
                f"{block['product']}/{block['object']} maps raw "
                f"{row['raw']!r} to {target!r}, which is not a C3 status."
            )


def test_zoho_mappings_always_name_their_product():
    """Books documentation is never evidence for ERP. The registry must not
    contain a product-agnostic mapping row."""
    for block in contract("C17_zoho_status_map.json")["mappings"]:
        assert block.get("product") in {"ERP", "BOOKS", "INVENTORY"}, block
        assert block.get("api_version"), block


def test_erp_mappings_are_marked_provisional_pending_d14():
    """Zoho ERP is the PROVISIONAL target. Nothing may present it as settled
    until the tenant confirms product, edition and plan (decision D-14)."""
    for block in contract("C17_zoho_status_map.json")["mappings"]:
        if block["product"] == "ERP":
            assert block.get("provisional") is True, (
                f"ERP mapping for {block['object']} is not marked provisional."
            )


# ===========================================================================
# C14 - traceability
# ===========================================================================
def test_every_traced_requirement_has_a_known_provenance():
    doc = contract("C14_traceability.json")
    allowed = set(doc["provenance_values"])
    for r in doc["requirements"]:
        assert r["provenance"] in allowed, r["requirement_id"]


def _traced() -> list[dict]:
    return [
        r for r in contract("C14_traceability.json")["requirements"]
        if r.get("traceability_status") == "TRACED"
    ]


def test_every_traced_requirement_has_at_least_one_test():
    """A requirement nobody can demonstrate is not a delivered requirement."""
    for r in _traced():
        assert r.get("tests"), (
            f"{r['requirement_id']} is marked TRACED but has no tests[]. Every "
            "traced requirement must name what proves it."
        )


def test_every_traced_requirement_has_acceptance_criteria():
    for r in _traced():
        assert r.get("acceptance_criteria", "").strip(), r["requirement_id"]


def test_every_requirement_declares_a_traceability_status():
    for r in contract("C14_traceability.json")["requirements"]:
        assert r.get("traceability_status") in {"TRACED", "PENDING"}, r["requirement_id"]


# CONTRACT_GAPS.md GAP-05. The 107 CLIENT-PDF requirements were migrated from
# C1_req_ids.json with their real metadata but WITHOUT invented screens, tables
# or test names - a plausible-looking wrong traceability row silently reports
# coverage that does not exist, which is worse than a visibly empty one.
#
# Lower this constant as requirements are traced. It must never rise.
MAX_PENDING_TRACEABILITY = 107


def test_untraced_requirement_count_only_ever_falls():
    doc = contract("C14_traceability.json")
    pending = sum(
        1 for r in doc["requirements"] if r["traceability_status"] == "PENDING"
    )
    assert pending <= MAX_PENDING_TRACEABILITY, (
        f"{pending} requirements lack traceability, up from "
        f"{MAX_PENDING_TRACEABILITY}. A new requirement must be traced when it "
        "is added, not deferred."
    )


def test_no_pending_requirement_pretends_to_have_coverage():
    """A PENDING row must be visibly empty. Half-filled rows are the failure
    mode this guards against."""
    for r in contract("C14_traceability.json")["requirements"]:
        if r["traceability_status"] != "PENDING":
            continue
        assert not r.get("tests"), f"{r['requirement_id']} is PENDING but names tests"
        assert not r.get("acceptance_criteria", "").strip(), (
            f"{r['requirement_id']} is PENDING but states acceptance criteria"
        )


def test_every_client_note_and_product_owner_requirement_is_fully_traced():
    """The 15 requirements confirmed on 2026-08-28 were traced as part of the
    same work that recorded them. None may be left pending."""
    for r in contract("C14_traceability.json")["requirements"]:
        if r["provenance"] in {
            "CLIENT-NOTES-2026-08-28", "PRODUCT-OWNER-REQUEST-2026-08-28",
        }:
            assert r["traceability_status"] == "TRACED", (
                f"{r['requirement_id']} was confirmed on 2026-08-28 and must be traced."
            )


def test_requirement_ids_follow_the_frozen_scheme():
    pattern = re.compile(r"^REQ-(PRJ|WBS|BUD|REV|PRC|PO|GRN|BILL|CWIP|CAP|RPT|SEC|INT|ALT)-\d{3}$")
    for r in contract("C14_traceability.json")["requirements"]:
        assert pattern.match(r["requirement_id"]), r["requirement_id"]


def test_client_note_and_product_owner_requirements_are_separately_attributed():
    """A provenance correction on 2026-08-28 split these two classes apart.
    GRN and Vendor Bill inbound sync came from the product owner, NOT from the
    handwritten pages, and must not be reattributed to them."""
    rows = {r["requirement_id"]: r for r in contract("C14_traceability.json")["requirements"]}
    for rid in ("REQ-GRN-002", "REQ-BILL-003"):
        assert rows[rid]["provenance"] == "PRODUCT-OWNER-REQUEST-2026-08-28", (
            f"{rid} must carry product-owner provenance, not client-notes."
        )
        assert rows[rid].get("secondary_evidence"), (
            f"{rid} should retain its supporting CLIENT-PDF requirements."
        )


def test_master_prompt_requirements_are_never_presented_as_client_confirmed():
    """C1_req_ids.json states the rule; this asserts nobody quietly relabels."""
    doc = contract("C14_traceability.json")
    notes = doc["provenance_notes"]
    assert "NEVER presented to the client as a confirmed client requirement" in notes["MASTER-PROMPT"]


def test_wbs_requirement_records_its_provenance_caveat():
    """The client notes evidence a multi-level BUDGET hierarchy. They do not
    describe a WBS element model. That boundary must stay visible."""
    rows = {r["requirement_id"]: r for r in contract("C14_traceability.json")["requirements"]}
    assert rows["REQ-WBS-001"].get("provenance_caveat"), (
        "REQ-WBS-001 must record that the WBS element model remains MASTER-PROMPT."
    )


# ===========================================================================
# C10 - message registry
# ===========================================================================
def test_every_message_id_used_in_code_exists_in_the_registry():
    registry = {m["id"] for m in contract("C10_messages.json")["messages"]}
    used = set(re.findall(r"MSG-[A-Z]+-\d+", backend_source()))
    unknown = used - registry
    assert not unknown, f"Message ids used in code but absent from C10: {sorted(unknown)}"


# ===========================================================================
# C6 - design tokens.  styles.css is the APPROVED baseline and must not change.
# ===========================================================================
def _token_hexes() -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            found.update(h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", node))

    walk(contract("C6_tokens.json"))
    return found


def _css() -> str:
    return (FRONTEND / "styles.css").read_text(encoding="utf-8")


def test_the_root_palette_is_exactly_the_frozen_token_palette():
    """The strict gate. Every colour declared as a custom property must be a
    frozen token value, and every frozen token value must be declared."""
    root = re.search(r":root\s*\{(.*?)\}", _css(), re.S).group(1)
    declared = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", root)}
    assert declared == _token_hexes(), (
        f"styles.css :root diverged from C6_tokens.json.\n"
        f"  in CSS only: {sorted(declared - _token_hexes())}\n"
        f"  in C6 only:  {sorted(_token_hexes() - declared)}"
    )


# CONTRACT_GAPS.md GAP-02. These nine tones are real design decisions that were
# never lifted into the token registry. styles.css is approved and frozen, so
# the fix is to complete C6 - not to edit the stylesheet. Allowlisted so that a
# NEW off-token colour still fails.
KNOWN_OFF_TOKEN_HEXES = {
    "#7D1A15", "#F0C4C1",   # .msg-error   text, border
    # 6D4600 WAS HERE and is not any more, because it stopped being an
    # off-token literal: it is now `--warning-text` in :root and a
    # declared colour in C6_tokens.json. This gate's own failure message
    # says "add a new colour to C6_tokens.json rather than to
    # styles.css" -- that is what happened, so the entry leaves this
    # list. It was five repeated literals across three stylesheets, and
    # `closure.spec.js` refused the two in a feature stylesheet, which
    # is how it was found.
    "#ECD6A8",   # .msg-warning border (+ .mock-chip border)
    "#145232", "#BFE0CC",   # .msg-success text, border
    "#1F3F7A", "#C5D4F0",   # .msg-info    text, border
    "#7A1A15",              # .bar.breach > i.actual
}


def test_no_new_off_token_colour_is_introduced():
    everywhere = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", _css())}
    off_token = everywhere - _token_hexes()
    assert off_token == KNOWN_OFF_TOKEN_HEXES, (
        f"The off-token colour set changed.\n"
        f"  newly introduced: {sorted(off_token - KNOWN_OFF_TOKEN_HEXES)}\n"
        f"  no longer present: {sorted(KNOWN_OFF_TOKEN_HEXES - off_token)}\n"
        "Add a new colour to C6_tokens.json rather than to styles.css, and "
        "update CONTRACT_GAPS.md GAP-02."
    )


# The client has approved this stylesheet. It is the visual baseline and is
# frozen. Changing it requires a deliberate design-approval decision recorded in
# the same commit that updates this pin - which is the point: the pin makes the
# change impossible to slip through unnoticed.
#
# The hash is taken over LINE-ENDING-NORMALISED content, not raw bytes. This
# repository is developed on Windows with core.autocrlf=true, so the working
# tree holds CRLF while the repository holds LF - a raw byte hash would pass on
# a developer's machine and fail on every Linux CI runner. Normalising makes the
# pin describe the content, which is what we actually care about.
#
# Recompute with:
#   python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('app/frontend/styles.css').read_bytes().replace(b'\r\n',b'\n')).hexdigest())"
#
# Updated 2026-09-09, deliberately, for the APPROVED WARNING-TEXT CONTRAST
# CORRECTION (Wave 8, stream C). The product owner approved, in writing, the
# replacement of the FAILING warning-text colour and nothing else.
#
# The defect. `--warning` (#A66A00) fails WCAG 2.2 AA as normal text on every
# background it is actually used on. Recomputed independently for this change
# (sRGB relative luminance, WCAG 2.x formula):
#
#     on --n0        (FFFFFF)   4.4843:1   FAIL   (body text needs 4.5:1)
#     on --n50       (F7F8F9)   4.2173:1   FAIL
#     on --n100      (EFF1F3)   3.9604:1   FAIL
#     on --warning-bg(FDF3E2)   4.0775:1   FAIL
#
# The three warning-TEXT rules in this stylesheet all render at body size --
# `.st-warning` 12px/600, `.nav-item .pill.warn` 10px/600 and `.mock-chip`
# 10px/700 -- none of which is WCAG "large text" (18.66px bold / 24px), so the
# 4.5:1 threshold applies to all three. They now take 6D4600, the darker amber
# `.msg-warning` has always used:
#
#     on --n0        (FFFFFF)   8.3125:1   PASS
#     on --n50       (F7F8F9)   7.8176:1   PASS
#     on --n100      (EFF1F3)   7.3413:1   PASS
#     on --warning-bg(FDF3E2)   7.5585:1   PASS
#
# NO new colour was introduced: 6D4600 is already present in this stylesheet
# and already allowlisted in KNOWN_OFF_TOKEN_HEXES, so
# `test_the_root_palette_is_exactly_the_frozen_token_palette` and
# `test_no_new_off_token_colour_is_introduced` are unaffected by design -- the
# correction had to be reachable without touching the token registry.
#
# The NON-text uses of `--warning` are deliberately UNCHANGED: the `.st-warning`
# dot, the `.tile.accent-watch` rule and the `.mock-chip` border are non-text
# indicators needing 3:1, which A66A00 clears everywhere (3.96:1 worst case).
# Keeping them preserves the amber as the semantic signal while the text that
# has to be READ becomes legible.
# SECOND MOVEMENT OF THIS PIN, same approved change, no pixel difference.
# The approved correction shipped as a repeated literal `#6d4600` in
# three stylesheets. `closure.spec.js` refused the two in feature
# stylesheets -- a feature stylesheet must reference tokens, because a
# colour repeated as a literal in five places is five places to miss
# when it next moves. The colour is now `--warning-text` in :root and a
# declared token in C6_tokens.json, and every use references it.
#
# THE RENDERED VALUE IS IDENTICAL at every call site -- 6D4600 before,
# 6D4600 after -- so no baseline moves and this is the mechanical
# completion of the approved change, not a new visual decision.
# Previous pin: cae43990b8c3cb7ea047f80771eeb103d6022f1e55a03e07b4b68aa9060ff847
# Then:         04c89fe5f19683b238e4928059ac6fcc85f2bf276cc3890ac76c31bf2475c2a4
# Then:         f56656aa9b3265c35530a636ad2cb73635e48cd7c4c0e7bdbc317eba4fbebceb
#
# THIRD MOVEMENT OF THIS PIN -- the `--n500` HOVERED-ROW CORRECTION, approved
# by the product owner on 2026-09-10 for the Fable 5.1 hardening pass, with
# the explicit instruction "using an existing darker neutral design token".
#
# The defect (RELEASE_PACKAGE L-06, second half, previously OPEN): `.muted`
# text is `--n500` (#6B7280), which measures 4.8345:1 on white but only
# 4.3210:1 against the `--primary-50` (#EAF4F6) tint that `tbody tr:hover`
# paints, so muted text in any hovered table row failed AA application-wide.
#
# The correction is ONE additional rule, placed directly after the hover
# rule: `tbody tr:hover .muted, tbody tr:hover button.tree-toggle` take
# `--n700` (#3A4048), which measures 9.3528:1 on the hover tint (and
# 10.4642:1 on white, 9.8412:1 on --n50, 9.2416:1 on --n100). No token value
# changed, no new colour entered the palette, and no rule other than the one
# added was touched; a resting (un-hovered) row renders byte-identically, so
# the approved static baselines do not move. `tests/test_uat_profile.py::
# test_the_hovered_row_muted_text_correction_measures_aa` recomputes both
# ratios from the stylesheet's own hex values.
APPROVED_STYLES_CSS_SHA256 = (
    "410cfb8d3b0f3f4465045f30856cd74b97445d5b8145fd92d9b7a26a93adc1ee"
)


def _normalised_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_the_approved_stylesheet_is_unchanged():
    """The strongest gate on the approved design.

    The palette test above proves the colours still match the frozen tokens. It
    would NOT notice a changed spacing scale, a lost `font-variant-numeric:
    tabular-nums` on a money column, or a deleted focus ring - all of which are
    part of what the client approved. This notices any of them.
    """
    actual = _normalised_sha256(FRONTEND / "styles.css")
    assert actual == APPROVED_STYLES_CSS_SHA256, (
        "app/frontend/styles.css has changed.\n"
        f"  expected {APPROVED_STYLES_CSS_SHA256}\n"
        f"  actual   {actual}\n"
        "This file is the client-approved visual baseline. If the change is "
        "intended, update APPROVED_STYLES_CSS_SHA256 in the SAME commit and "
        "record the design approval in the commit message."
    )


def test_the_server_sends_a_style_src_self_content_security_policy():
    """Renamed from a name that promised something it did not check.

    The previous version was called
    `test_styles_css_declares_no_inline_style_attribute_escape_hatch` but never
    opened styles.css and never looked at any markup - it was a substring grep
    over Python source, which a commented-out CSP line would have satisfied.
    This asserts the header the app actually sends; the companion test below
    asserts the frontend property.
    """
    from starlette.testclient import TestClient

    from app.backend.main import app

    with TestClient(app) as client:
        csp = client.get("/api/health").headers.get("content-security-policy", "")
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp, (
        "'unsafe-inline' would permit markup style attributes and remove the "
        "constraint that keeps the approved design framework-free."
    )


def test_the_frontend_carries_no_inline_style_attribute():
    """The other half. Under `style-src 'self'` a markup style="" attribute is
    blocked outright, so geometry must be set through the CSSOM - which CSP
    does not govern. `app.js::enhance()` depends on exactly that distinction.

    A `style="` literal anywhere in the shipped frontend means either a broken
    page or a weakened CSP, and both must fail here.
    """
    for name in ("index.html", "app.js", "styles.css"):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        assert 'style="' not in text, (
            f"{name} contains a markup style attribute, which CSP blocks. "
            "Set computed geometry via element.style.<prop> instead."
        )


# ===========================================================================
# C9 - roles.  GAP-04: business and technical roles are unreconciled (D-12).
# ===========================================================================
KNOWN_TECHNICAL_ROLES = {
    "Requestor", "BudgetController", "ProcurementApprover", "FinanceApprover",
    "CapitalisationApprover", "Auditor", "Administrator",
}


def test_technical_role_set_has_not_drifted_while_d12_is_unresolved():
    """C9 freezes 13 business roles; auth.py implements 7 technical roles and
    only 'Requestor' overlaps. Reconciling them is blocking decision D-12.

    Until it is resolved, nobody adds an eighth technical role silently.
    """
    from app.backend import auth

    assert set(auth.ROLES) == KNOWN_TECHNICAL_ROLES, (
        "auth.ROLES changed while the business/technical role mapping is still "
        "unresolved. See CONTRACT_GAPS.md GAP-04 and blocking decision D-12."
    )


def test_every_permission_maps_only_to_known_technical_roles():
    from app.backend import auth

    for permission, roles in auth.PERMISSIONS.items():
        unknown = set(roles) - KNOWN_TECHNICAL_ROLES
        assert not unknown, f"{permission} grants unknown roles {unknown}"


def test_every_maker_checker_permission_is_a_real_permission():
    from app.backend import auth

    assert auth.MAKER_CHECKER <= set(auth.PERMISSIONS), (
        "MAKER_CHECKER names a permission that does not exist."
    )


# ===========================================================================
# C5 - the frozen formula registry.  These are the financial invariants.
# ===========================================================================
def _indian_number_to_paise(text: str) -> int:
    """Parse the two notations C5 actually uses.

        '25,00,000'  Indian digit grouping   -> 2,500,000 rupees
        '1.50 Cr'    crore shorthand         -> 15,000,000 rupees
        '30L'        lakh shorthand          -> 3,000,000 rupees

    Conversion runs through money.to_paise, so the test exercises the real
    money path rather than a parallel one.
    """
    from decimal import Decimal

    from app.backend.money import to_paise

    cleaned = text.replace(",", "").strip()
    m = re.fullmatch(r"(?i)\s*([\d.]+)\s*(Cr|L)?\s*", cleaned)
    assert m, f"unparseable amount in C5: {text!r}"
    amount = Decimal(m.group(1))
    unit = (m.group(2) or "").upper()
    if unit == "CR":
        amount *= 10_000_000
    elif unit == "L":
        amount *= 100_000
    return to_paise(amount)


def test_derive_reproduces_every_frozen_worked_example_in_c5():
    """This actually OPENS C5_formulas.json and runs its worked examples
    through domain._derive.

    The previous version of this test asserted hardcoded numbers and never read
    the registry, so it would have passed even if C5 had been rewritten to
    define exposure differently - which is the exact failure mode this suite
    exists to prevent.

    C5's rule states authors "must NOT recompute or restate [the examples] in
    their own numbers", so the numbers are taken from the file, not retyped.
    """
    from app.backend.domain import _derive

    examples = contract("C5_formulas.json")["client_invariant"]["frozen_worked_examples"]
    numeric = [e for e in examples if {"budget", "commitment", "actual", "available"} <= e.keys()]
    assert numeric, "C5 no longer carries any numeric worked example"

    for e in numeric:
        cell = _derive({
            "budget": _indian_number_to_paise(e["budget"]),
            "commitment": _indian_number_to_paise(e["commitment"]),
            "actual": _indian_number_to_paise(e["actual"]),
        })
        assert cell["available"] == _indian_number_to_paise(e["available"]), (
            f"{e['context']}: available budget disagrees with the frozen example"
        )
        assert cell["exposure"] == (
            _indian_number_to_paise(e["commitment"]) + _indian_number_to_paise(e["actual"])
        ), f"{e['context']}: exposure disagrees with the frozen formula"


def test_pr_reservation_extends_exposure_without_changing_the_frozen_formula():
    """The client invariant is Exposure = Open PO Commitment + Actual CWIP.
    The reservation term (AUD-H-001) is an ADDITION that must reduce to the
    frozen formula whenever no reservation is live."""
    from app.backend.domain import _derive

    without = _derive({"budget": 1_000_000, "commitment": 300_000, "actual": 200_000})
    with_res = _derive({**{"budget": 1_000_000, "commitment": 300_000,
                           "actual": 200_000}, "pr_reserved": 100_000})
    assert without["exposure"] == 500_000
    assert with_res["exposure"] == 600_000
    assert with_res["available"] == 400_000


def test_the_alert_bands_match_the_frozen_thresholds():
    from app.backend.domain import CRITICAL_PCT, WATCH_PCT, _derive

    assert (WATCH_PCT, CRITICAL_PCT) == (80.0, 90.0)
    assert _derive({"budget": 100, "commitment": 50})["band"] == "safe"
    assert _derive({"budget": 100, "commitment": 85})["band"] == "watch"
    assert _derive({"budget": 100, "commitment": 95})["band"] == "critical"
    assert _derive({"budget": 100, "commitment": 101})["band"] == "breach"


def test_only_accounting_effective_bill_states_move_actual_cwip():
    """AUD-C-004. Widening this set silently would let a draft or void bill
    move actual CWIP.

    C18 carries the answer in machine-readable form via `accounting_effective`,
    so this cross-checks the registry against the code rather than restating a
    literal - the two can no longer diverge in either direction.
    """
    from app.backend.domain import ACCOUNTING_EFFECTIVE_BILL_STATES

    ns = contract("C18_domain_statuses.json")["namespaces"]["accounting_status"]
    from_registry = {s["code"] for s in ns["statuses"] if s["accounting_effective"]}
    assert ACCOUNTING_EFFECTIVE_BILL_STATES == from_registry == {"Approved", "Reversal"}
    assert ns["source_of_truth"] == "domain.ACCOUNTING_EFFECTIVE_BILL_STATES"


def test_only_cancelled_and_closed_release_commitment():
    from app.backend.domain import COMMITMENT_RELEASING_STATES

    assert COMMITMENT_RELEASING_STATES == {"Cancelled", "Closed"}


def test_money_is_integer_paise_and_never_float():
    """AUD-H-007. The single most load-bearing property in the system."""
    from app.backend.money import to_paise

    assert to_paise("0.005") == 1, "ROUND_HALF_UP, not banker's rounding"
    assert to_paise("0.015") == 2
    assert to_paise("90071992547409.93") == 9007199254740993, "beyond float64 mantissa"
    assert isinstance(to_paise("1.00"), int)


# ===========================================================================
# C8 - screens
# ===========================================================================
def test_c8_freezes_forty_screens_each_with_twenty_four_attributes():
    c8 = contract("C8_screens.json")
    assert c8["count"] == 40
    assert len(c8["screens"]) == 40
    assert len(c8["required_attributes_per_screen"]) == 24


def test_every_traced_screen_reference_is_a_real_screen_id():
    known = {s["screen_id"] for s in contract("C8_screens.json")["screens"]}
    for r in contract("C14_traceability.json")["requirements"]:
        for scr in r.get("screens", []):
            assert scr in known, f"{r['requirement_id']} references unknown screen {scr}"


# ===========================================================================
# The gap register itself
# ===========================================================================
def test_the_contract_gap_register_exists_and_lists_every_open_gap():
    """CONTRACT_GAPS.md is the ratchet's record. If a gap is closed in code it
    must be closed here too, and vice versa."""
    text = (ROOT / "CONTRACT_GAPS.md").read_text(encoding="utf-8")
    for gap in ("GAP-01", "GAP-02", "GAP-03", "GAP-04", "GAP-05"):
        assert gap in text, f"{gap} is asserted by this suite but absent from the register."


@pytest.mark.parametrize("filename", [
    "C1_req_ids.json", "C3_statuses.json", "C4_entities.json", "C5_formulas.json",
    "C6_tokens.json", "C8_screens.json", "C9_roles.json", "C10_messages.json",
    "C13_conflicts.json", "C14_traceability.json", "C15_approval_statuses.json",
    "C16_integration_statuses.json", "C17_zoho_status_map.json",
    "C18_domain_statuses.json",
])
def test_every_contract_file_is_valid_json_and_declares_its_freeze_date(filename):
    doc = contract(filename)
    assert doc.get("frozen_at"), f"{filename} does not declare frozen_at"
    assert doc.get("rule"), f"{filename} does not declare its rule"


# ===========================================================================
# Ambiguity ownership - documentation consistency
#
# A stale exit-report line claimed the open ambiguities were "open with
# owners" while every named owner in the register read UNASSIGNED. A proposed
# ROLE is not an owner: a role cannot answer a question. Reporting unowned
# items as owned is the specific failure this guards against, because it makes
# a blocked dependency look attended to.
# ===========================================================================
AMBIGUITY_REGISTER = ROOT / "research" / "00_intake" / "client_notes_2026-08-28.md"
PHASE_0A_EXIT = ROOT / "docs" / "PHASE_0A_EXIT.md"


def _unassigned_ambiguities() -> list[str]:
    text = AMBIGUITY_REGISTER.read_text(encoding="utf-8")
    return [
        row.split("|")[1].strip()
        for row in text.splitlines()
        if row.startswith("| AMB-") and "UNASSIGNED" in row
    ]


def test_no_document_claims_ambiguities_are_owned_while_any_is_unassigned():
    """The assertion this correction exists to make permanent."""
    unassigned = _unassigned_ambiguities()
    if not unassigned:
        return  # every ambiguity has a named owner; the claim would be fair

    forbidden = ("open with owners", "owners named", "owners assigned",
                 "all owned", "ownership assigned")
    offenders = []
    for doc in sorted(ROOT.glob("docs/*.md")) + sorted(ROOT.glob("research/00_intake/*.md")):
        lowered = doc.read_text(encoding="utf-8").lower()
        for phrase in forbidden:
            if phrase in lowered:
                offenders.append(f"{doc.relative_to(ROOT).as_posix()}: {phrase!r}")

    assert not offenders, (
        f"{len(unassigned)} ambiguities have no named owner ({', '.join(unassigned)}), "
        f"but a document claims otherwise: {offenders}. A proposed role is not an owner."
    )


def test_the_exit_report_states_the_ambiguity_position_accurately():
    text = PHASE_0A_EXIT.read_text(encoding="utf-8")
    row = [ln for ln in text.splitlines() if "Ambiguities resolved or owned" in ln]
    assert row, "exit report no longer states the ambiguity criterion"
    line = row[0]
    assert "AMB-08 closed" in line
    if _unassigned_ambiguities():
        assert "UNASSIGNED" in line, (
            "Named owners are UNASSIGNED in the register, so the exit report must "
            "say so rather than implying the items are owned."
        )


def test_every_open_ambiguity_declares_a_decision_gate_and_cost_of_delay():
    """An unowned item still needs a gate and a stated cost, or it reads as
    optional rather than as a dependency."""
    text = AMBIGUITY_REGISTER.read_text(encoding="utf-8")
    rows = [r for r in text.splitlines() if r.startswith("| AMB-") and "CLOSED" not in r]
    assert rows, "ambiguity register has no open rows"
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        rid = cells[0]
        assert len(cells) >= 9, f"{rid}: register row is missing columns"
        assert cells[6], f"{rid}: no decision gate recorded"
        assert cells[7], f"{rid}: no cost of delay recorded"


def test_every_open_ambiguity_appears_in_the_client_questionnaire():
    """The questionnaire is the instrument for closing these; an ambiguity
    absent from it has no route to an answer."""
    questionnaire = (ROOT / "research" / "00_intake"
                     / "client_decision_questionnaire.md").read_text(encoding="utf-8")
    text = AMBIGUITY_REGISTER.read_text(encoding="utf-8")
    open_ids = [
        r.split("|")[1].strip()
        for r in text.splitlines()
        if r.startswith("| AMB-") and "CLOSED" not in r
    ]
    missing = [i for i in open_ids if i not in questionnaire]
    assert not missing, f"open ambiguities absent from the questionnaire: {missing}"
