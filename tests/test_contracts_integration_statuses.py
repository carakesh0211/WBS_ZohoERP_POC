"""Contract gates for C16 (integration statuses) and C17 (raw Zoho statuses).

A companion to ``tests/test_contracts.py``, written in the same shape and to
the same standard, kept in its own file because Wave 5 streams run in parallel
and file-disjointness is what keeps them from colliding. Every gate here is
additive; nothing in ``test_contracts.py`` is weakened, and its existing C16/C17
gates still run.

Plan v1.2.1 section 8 separates four status registries. The whole value of that
separation is that it can be checked, so this file checks it:

  * every mapping target exists in ``C3_statuses.json``
  * no integration or approval status reaches a business-screen renderer
  * every C16 state is reachable from the code that writes it
  * "maps to nothing" and "not yet mapped" stay distinguishable
  * an unsourced Zoho spelling is inert, not applied

No test here touches the database, the network or a Zoho tenant.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.backend.integration import statuses as S

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "research" / "30_contracts"
FRONTEND = ROOT / "app" / "frontend" / "src"


def contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _c3_codes() -> set[str]:
    return {s["code"] for s in contract("C3_statuses.json")["statuses"]}


def _c16_codes() -> set[str]:
    out: set[str] = set()
    for ns in contract("C16_integration_statuses.json")["namespaces"].values():
        out |= {s["code"] for s in ns["statuses"]}
    return out


def _c15_codes() -> set[str]:
    out: set[str] = set()
    for ns in contract("C15_approval_statuses.json")["namespaces"].values():
        out |= {s["code"] for s in ns.get("statuses", [])}
    return out


def _c17_rows():
    for block in contract("C17_zoho_status_map.json")["mappings"]:
        for row in block["rows"]:
            yield block, row


# ===========================================================================
# Gate 1 - every mapping target exists in C3_statuses.json
#
# tests/test_contracts.py already asserts this for non-null maps_to. These add
# the two halves it cannot see: that a NULL target is a deliberate assertion
# rather than an omission, and that the loaded registry agrees with the file.
# ===========================================================================
def test_every_mapping_target_exists_in_the_frozen_business_registry():
    codes = _c3_codes()
    for block, row in _c17_rows():
        if row["maps_to"] is None:
            continue
        assert row["maps_to"] in codes, (
            f"{block['product']}/{block['object']}.{block['field']} maps raw "
            f"{row['raw']!r} to {row['maps_to']!r}, which is not one of the 21 "
            "frozen C3 business statuses."
        )


def test_the_loaded_map_targets_the_same_registry_the_file_names():
    """The file is checked above; this checks what the CODE actually built.

    A gate that only reads JSON proves nothing about the object the resolver
    uses. These are the mappings that will really be applied.
    """
    codes = S.business_statuses()
    assert codes == _c3_codes()
    for block in S.zoho_status_map().blocks.values():
        for row in block.rows.values():
            if row.maps_to is not None:
                assert row.maps_to in codes, f"{block.product}/{row.raw}"


def test_every_row_declares_whether_it_maps_to_nothing_or_is_not_yet_mapped():
    """The distinction the plan insists on, made mechanical.

    ``maps_to: null`` carries two completely different meanings - "this value
    has no business-status consequence" and "nobody has decided yet" - and
    letting them share a representation is how the second quietly becomes the
    first. ``mapping_kind`` on the row says which, and ``coverage`` on the
    block says whether the field's value set is even known.
    """
    for block, row in _c17_rows():
        kind = row.get("mapping_kind")
        assert kind in S.MAPPING_KINDS, (
            f"{block['product']}/{block['object']}.{block['field']} row "
            f"{row['raw']!r} has mapping_kind {kind!r}; a row may not leave it unsaid."
        )
        if kind == S.BUSINESS_STATUS:
            assert row["maps_to"] is not None
        else:
            assert row["maps_to"] is None, (
                f"{row['raw']!r} is {kind} but still carries a business status."
            )


def test_every_block_declares_whether_its_value_set_is_known():
    for block in contract("C17_zoho_status_map.json")["mappings"]:
        assert block.get("coverage") in {
            S.COMPLETE_PER_VENDOR_SPEC, S.INCOMPLETE_UNVERIFIED,
        }, block
        assert block.get("coverage_evidence"), (
            f"{block['product']}/{block['object']} claims coverage "
            f"{block['coverage']} without saying what sources it."
        )
        if not block["rows"]:
            assert block["coverage"] == S.INCOMPLETE_UNVERIFIED, (
                f"{block['product']}/{block['object']}.{block['field']} has no "
                "rows but claims a complete value set. An empty block means NOT "
                "YET MAPPED - it never means 'maps to nothing'."
            )


# ===========================================================================
# Gate 2 - no integration or approval status reaches a business-screen renderer
# ===========================================================================
#: Renderers permitted to name statuses from a non-business namespace. Each is
#: the screen family that OWNS that namespace.
_APPROVAL_RENDERERS = ("components/approvals/", "features/approvals/")
_INTEGRATION_RENDERERS = ("components/integration/", "features/integration/")


def _frontend_sources():
    for path in sorted(FRONTEND.rglob("*.js")):
        yield path, path.relative_to(FRONTEND).as_posix()


def _quoted(code: str) -> re.Pattern:
    """Match the code as a STRING LITERAL, not as a substring of prose.

    Scanning for the bare word finds it in comments and in unrelated
    identifiers; scanning for a quoted literal finds the thing that actually
    reaches a template.
    """
    return re.compile(r"""['"`]""" + re.escape(code) + r"""['"`]""")


def test_no_integration_status_is_rendered_by_a_business_screen():
    """Operational state belongs on SCR-26/38/39.

    The three badge values are excluded here because they are the SANCTIONED
    bridge - an object may legitimately show QUEUED/SENT/FAILED beside its
    business status. That exemption is exactly why the next test exists.
    """
    badge = set(contract("C16_integration_statuses.json")["business_visible_bridge"]["badge_values"])
    forbidden = _c16_codes() - badge
    offences = []
    for path, rel in _frontend_sources():
        if rel.startswith(_INTEGRATION_RENDERERS):
            continue
        source = path.read_text(encoding="utf-8")
        for code in sorted(forbidden):
            if _quoted(code).search(source):
                offences.append(f"{rel}: {code}")
    assert not offences, (
        "Integration job status rendered outside the integration screens:\n  "
        + "\n  ".join(offences)
        + "\nOperational state reaches a business screen through the C3 codes "
          "INTEGRATION_FAILED / RECONCILIATION_PENDING or the integration_state "
          "badge, and by no other route."
    )


def test_no_approval_status_is_rendered_outside_the_approval_screens():
    forbidden = _c15_codes() - _c3_codes()
    offences = []
    for path, rel in _frontend_sources():
        if rel.startswith(_APPROVAL_RENDERERS):
            continue
        source = path.read_text(encoding="utf-8")
        for code in sorted(forbidden):
            if _quoted(code).search(source):
                offences.append(f"{rel}: {code}")
    assert not offences, (
        "Approval engine status rendered outside the approval screens:\n  "
        + "\n  ".join(offences)
        + "\nApproval state is INTERNAL; it reaches an object through its "
          "mapped business status."
    )


def test_the_business_status_pill_knows_nothing_of_the_other_namespaces():
    """The specific defect these registries exist to prevent.

    ``capex-statuschip.js`` renders the C3 business status. If an integration
    code - or a badge value - can be passed through it, the separation has
    collapsed at the only place a user would ever notice.
    """
    pill = (FRONTEND / "components" / "capex-statuschip.js").read_text(encoding="utf-8")
    bridge = contract("C16_integration_statuses.json")["business_visible_bridge"]
    leaked = [
        code for code in sorted(_c16_codes() | _c15_codes() | set(bridge["badge_values"]))
        if _quoted(code).search(pill)
    ]
    assert not leaked, (
        f"The C3 status pill names non-business statuses {leaked}. The "
        "integration badge has its own slot and its own component."
    )


def test_the_badge_is_not_a_twenty_second_business_status():
    bridge = contract("C16_integration_statuses.json")["business_visible_bridge"]
    assert set(bridge["badge_values"]) == {"QUEUED", "SENT", "FAILED"}
    assert not set(bridge["badge_values"]) & _c3_codes(), (
        "A badge value collides with a C3 code. The badge exists precisely so "
        "that the frozen 21 stay 21."
    )
    assert len(_c3_codes()) == 21


# ===========================================================================
# Gate 3 - every C16 state is reachable from the code that writes it
# ===========================================================================
@pytest.mark.parametrize("namespace", sorted(S.integration_registry().namespaces))
def test_every_declared_state_is_reachable_by_driving_the_real_transition_code(namespace):
    """Not a graph walk over JSON - a walk driven through ``StatusNamespace``.

    ``reachable()`` calls ``transition()``, the same function every writer must
    call, so a state nothing can produce fails here rather than sitting in the
    registry looking authoritative. A registry may only declare states the
    system can actually reach.
    """
    ns = S.integration_registry()[namespace]
    unreachable = set(ns.codes) - set(ns.reachable())
    assert not unreachable, (
        f"{namespace}: {sorted(unreachable)} is declared but no sequence of "
        "declared triggers produces it."
    )


@pytest.mark.parametrize("namespace", sorted(S.integration_registry().namespaces))
def test_every_transition_names_a_writer_that_the_registry_declares(namespace):
    declared = {k for k in contract("C16_integration_statuses.json")["writers"] if k != "note"}
    for t in S.integration_registry()[namespace].transitions:
        assert t.writer in declared, (
            f"{namespace}.{t.trigger} names writer {t.writer!r}, which is not "
            "in the writer registry. A state with no owning module is a state "
            "nobody is accountable for."
        )


def test_every_declared_writer_actually_writes_something():
    """The other direction: a phantom writer is a claim of coverage that isn't."""
    doc = contract("C16_integration_statuses.json")
    declared = {k: v for k, v in doc["writers"].items() if k != "note"}
    used = {
        t.writer
        for ns in S.integration_registry().namespaces.values()
        for t in ns.transitions
    }
    assert set(declared) == used, (
        f"declared but unused: {sorted(set(declared) - used)}; "
        f"used but undeclared: {sorted(used - set(declared))}"
    )
    for name, spec in declared.items():
        for ns_name in spec["namespaces"]:
            assert ns_name in S.integration_registry().namespaces, (name, ns_name)


def test_every_namespace_has_at_least_one_writer_claiming_it():
    doc = contract("C16_integration_statuses.json")
    claimed: set[str] = set()
    for name, spec in doc["writers"].items():
        if name == "note":
            continue
        claimed |= set(spec["namespaces"])
    assert claimed == set(S.integration_registry().namespaces)


def test_terminal_states_are_terminal_and_non_terminal_states_are_not():
    """``terminal`` is used by SCR-38/39 to decide what may be retried, so a
    state flagged terminal that still has an exit is a real operator hazard."""
    for name, ns in S.integration_registry().namespaces.items():
        for code, status in ns.statuses.items():
            outgoing = sorted(t.trigger for t in ns.transitions if t.source == code)
            if status.terminal:
                assert not outgoing, f"{name}.{code} is terminal but exits via {outgoing}"
            else:
                assert outgoing, f"{name}.{code} is non-terminal but nothing leaves it"


# ===========================================================================
# Gate 4 - every mapping is data, versioned and effective-dated
# ===========================================================================
def test_both_registries_carry_a_version_and_a_version_history():
    for name in ("C16_integration_statuses.json", "C17_zoho_status_map.json"):
        doc = contract(name)
        assert re.fullmatch(r"\d+\.\d+\.\d+", doc["version"]), name
        history = doc["version_history"]
        assert history and history[-1]["version"] == doc["version"], (
            f"{name}: version_history does not end at the declared version."
        )
        for entry in history:
            assert entry["effective_from"], entry


def test_every_mapping_block_and_row_carries_an_effective_window():
    for block, row in _c17_rows():
        assert "effective_from" in block, block["object"]
        assert "effective_from" in row and "effective_to" in row, (block["object"], row["raw"])
        if row["active"]:
            assert row["effective_from"], (
                f"{block['product']}/{row['raw']} is active with no effective_from. "
                "An undated mapping cannot be superseded without rewriting history."
            )


def test_no_mapping_is_hardcoded_in_the_module_that_applies_it():
    """Section 8.4: every mapping is data, never a dict in application code.

    The one table the module is allowed to hold is the adapter-product bridge,
    which is a deployment fact rather than a status mapping.
    """
    source = (ROOT / "app" / "backend" / "integration" / "statuses.py").read_text(encoding="utf-8")
    for _, row in _c17_rows():
        if row["maps_to"]:
            assert f'"{row["maps_to"]}"' not in source and f"'{row['maps_to']}'" not in source, (
                f"business status {row['maps_to']!r} is hardcoded in statuses.py; "
                "mappings live in C17, not in the code that reads it."
            )


# ===========================================================================
# Gate 5 - an unsourced spelling is inert, not applied
# ===========================================================================
def test_every_unverified_row_carries_the_exact_marker_and_is_inactive():
    for block, row in _c17_rows():
        if row["evidence"]["class"] != "UNVERIFIED":
            continue
        assert row["evidence"].get("marker") == S.UNVERIFIED_MARKER, (
            f"{block['product']}/{row['raw']}: an unverified row must carry the "
            f"exact marker {S.UNVERIFIED_MARKER!r}."
        )
        assert row["active"] is False, (
            f"{block['product']}/{row['raw']} is UNVERIFIED but active. An "
            "unsourced spelling applied as if it were a mapping is exactly the "
            "guess the registry forbids."
        )
        assert row["evidence"].get("reasoning"), row["raw"]
        assert row["evidence"].get("confirm_by"), row["raw"]


def test_every_active_row_names_the_document_that_sources_it():
    sources = set(
        s["id"] for s in contract("C17_zoho_status_map.json")["evidence_sources"].values()
    )
    for block, row in _c17_rows():
        if not row["active"]:
            continue
        ev = row["evidence"]
        assert ev["class"] in S.ACTIVE_EVIDENCE_CLASSES, (block["product"], row["raw"], ev)
        assert ev["source"] in sources, (
            f"{block['product']}/{row['raw']} cites source {ev.get('source')!r}, "
            f"which is not in evidence_sources."
        )
        assert ev.get("locator"), (
            f"{block['product']}/{row['raw']} cites a source but not where in it."
        )


def test_no_books_or_inventory_row_is_active_without_its_own_evidence():
    """The product separation rule, enforced rather than asserted in prose.

    No Books or Inventory specification has been captured. A Books row may
    therefore only be active if the approved plan states it directly - never
    because the ERP bundle happens to say so.
    """
    for block, row in _c17_rows():
        if block["product"] in {"BOOKS", "INVENTORY"} and row["active"]:
            assert row["evidence"]["class"] == "PLAN_APPROVED", (
                f"{block['product']}/{block['object']}.{block['field']} row "
                f"{row['raw']!r} is active on evidence class "
                f"{row['evidence']['class']!r}. An ERP document is not evidence "
                "for Books or Inventory."
            )


def test_the_erp_evidence_source_is_the_bundle_actually_captured_in_the_repo():
    """A citation nobody can follow is not a citation."""
    src = contract("C17_zoho_status_map.json")["evidence_sources"]["erp_openapi"]
    spec_dir = ROOT / src["local_path"]
    assert spec_dir.is_dir(), src["local_path"]
    for f in ("purchase-order.yml", "bills.yml", "purchasereceives.yml"):
        assert (spec_dir / f).is_file(), f
    verification = json.loads((ROOT / src["corroboration"]).read_text(encoding="utf-8"))
    assert verification["spec_sha256"] == src["sha256"]
    assert verification["spec_source"] == src["url"]


@pytest.mark.parametrize("raw", ["draft", "open", "billed", "cancelled"])
def test_the_documented_erp_purchase_order_values_are_the_ones_actually_documented(raw):
    """Pins the active ERP rows to the captured specification's own words.

    If someone widens the active set later, this fails unless the spec really
    does say so.
    """
    spec = (ROOT / "research" / "00_intake" / "openapi" / "spec" / "purchase-order.yml").read_text(
        encoding="utf-8"
    )
    assert f"<code>{raw}</code>" in spec
    row = S.zoho_status_map().block("ERP", "purchase_order", "status").rows[raw]
    assert row.active and row.evidence_class == "DOCUMENTED_RAW_VALUE"


@pytest.mark.parametrize("raw", ["paid", "open", "overdue", "void", "partially_paid"])
def test_the_documented_erp_bill_values_are_the_ones_actually_documented(raw):
    spec = (ROOT / "research" / "00_intake" / "openapi" / "spec" / "bills.yml").read_text(
        encoding="utf-8"
    )
    assert f"<code>{raw}</code>" in spec
    row = S.zoho_status_map().block("ERP", "bill", "status").rows[raw]
    assert row.active and row.evidence_class == "DOCUMENTED_RAW_VALUE"


def test_erp_purchase_receive_has_no_status_rows_because_the_spec_has_no_status():
    """Plan section 11.4 and the captured spec agree: ERP Purchase Receives has
    four endpoints, no list endpoint and no documented status field. The block
    is empty, and empty here means NOT YET MAPPED."""
    spec = (ROOT / "research" / "00_intake" / "openapi" / "spec" / "purchasereceives.yml").read_text(
        encoding="utf-8"
    )
    assert "received_status" not in spec
    block = S.zoho_status_map().block("ERP", "purchase_receive", "received_status")
    assert block.rows == {}
    assert block.coverage == S.INCOMPLETE_UNVERIFIED
