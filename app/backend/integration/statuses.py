"""The integration status registries, and the code that applies them.

Plan v1.2.1 section 8 separates four status registries and says why they must
not be merged:

* ``C3_statuses.json``  - 21 FROZEN business statuses. Only these appear on a
  business screen.
* ``C15_approval_statuses.json`` - internal to the approval engine.
* ``C16_integration_statuses.json`` - operational. Inbox, outbox, job and
  circuit state. Visible to administrators on SCR-26/38/39 and nowhere else.
* ``C17_zoho_status_map.json`` - raw vendor values, stored verbatim, mapped to
  C3 by data rather than by code.

This module owns the last two. Three rules run through all of it.

**Raw is verbatim.** ``external_status_raw`` holds exactly the bytes Zoho sent,
alongside the product and API version that produced them. This module never
lower-cases, strips or canonicalises a raw value before matching it, because a
lenient match is a guess wearing a helpful face. ``"Open"`` is not ``"open"``,
and if a tenant starts sending one where it used to send the other we want an
exception, not a silent re-interpretation.

**Products are separate.** An ERP mapping is not evidence for Books. There is
no fallback between products; :meth:`ZohoStatusMap.resolve` will not look in a
neighbouring product's rows when its own miss.

**Unmapped never guesses.** A raw value with no ACTIVE row in its own product's
block yields :data:`UNMAPPED` plus a ``reconciliation_exception`` of kind
``UNMAPPED_EXTERNAL_STATUS``. The record is still accepted and the raw value is
still stored - fail-closed on interpretation, never on acceptance. This is the
same philosophy as :func:`app.backend.domain.lifecycle_permits`, where an
unknown state denies rather than defaults.

A fourth rule is specific to C17 v1.1: a row whose raw spelling is not sourced
in any document available offline is marked ``UNVERIFIED - REQUIRES ZOHO
CONFIRMATION`` and is INACTIVE. Inactive is not a soft warning; the resolver
treats such a row exactly as if it were absent. That is deliberate. Carrying an
unsourced spelling as if it were a mapping is how a guess gets laundered into a
posted figure.

Nothing here touches the network, the database or a Zoho tenant.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "research" / "30_contracts"

#: The exact marker a row must carry when its raw spelling is not sourced.
UNVERIFIED_MARKER = "UNVERIFIED - REQUIRES ZOHO CONFIRMATION"

#: reconciliation_exception kind raised for a raw value we will not interpret.
UNMAPPED_EXTERNAL_STATUS = "UNMAPPED_EXTERNAL_STATUS"


class ContractError(RuntimeError):
    """A registry file contradicts itself. Raised at import, not at runtime."""


class IllegalTransition(ValueError):
    """A caller asked for a transition the registry does not declare."""


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _as_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


# ===========================================================================
# C16 - operational statuses
# ===========================================================================
@dataclass(frozen=True)
class IntegrationStatus:
    code: str
    meaning: str
    terminal: bool


@dataclass(frozen=True)
class Transition:
    """One declared move. ``source is None`` means "row created in this state"."""
    source: Optional[str]
    target: str
    trigger: str
    writer: str
    side_effect: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class StatusNamespace:
    """One C16 namespace: its codes, its initial state and its transition graph."""
    name: str
    description: str
    initial_status: str
    statuses: Mapping[str, IntegrationStatus]
    transitions: tuple[Transition, ...]

    # -- queries ------------------------------------------------------------
    @property
    def codes(self) -> frozenset[str]:
        return frozenset(self.statuses)

    def is_terminal(self, code: str) -> bool:
        return self._status(code).terminal

    def triggers_from(self, code: Optional[str]) -> frozenset[str]:
        return frozenset(t.trigger for t in self.transitions if t.source == code)

    def _status(self, code: str) -> IntegrationStatus:
        try:
            return self.statuses[code]
        except KeyError:
            raise IllegalTransition(
                f"{self.name}: {code!r} is not a declared status. "
                f"Declared: {sorted(self.statuses)}"
            ) from None

    # -- the writer's entry points -----------------------------------------
    def create(self, trigger: str) -> str:
        """The state a newly written row takes. ``source is None`` transitions."""
        return self.transition(None, trigger)

    def transition(self, current: Optional[str], trigger: str) -> str:
        """Resolve ``(current, trigger)`` to the next state.

        Every persisted C16 state in the system is produced by this function.
        A move the registry does not declare raises rather than falling through
        to a default, so an undeclared state cannot reach the database by
        accident - which is what makes the reachability gate meaningful.
        """
        if current is not None:
            self._status(current)
        for t in self.transitions:
            if t.source == current and t.trigger == trigger:
                return t.target
        raise IllegalTransition(
            f"{self.name}: no transition from {current!r} on trigger {trigger!r}. "
            f"Available from {current!r}: {sorted(self.triggers_from(current))}"
        )

    def reachable(self) -> frozenset[str]:
        """Every state reachable by driving :meth:`transition` from creation.

        Breadth-first over the declared triggers, starting from the states a
        row can be created in. A code in ``statuses`` that never turns up here
        is dead weight in the registry, and the contract test says so.
        """
        seen: set[str] = set()
        queue: deque[Optional[str]] = deque([None])
        while queue:
            state = queue.popleft()
            for trigger in sorted(self.triggers_from(state)):
                target = self.transition(state, trigger)
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return frozenset(seen)


@dataclass(frozen=True)
class IntegrationRegistry:
    """C16 as an object. Namespaces are kept apart on purpose.

    ``DEAD`` in the inbox and ``DEAD`` in the job namespace are different
    operational facts; a flat lookup would let them be confused, and SQL that
    joined them would not say so.
    """
    version: str
    namespaces: Mapping[str, StatusNamespace]
    badge_values: tuple[str, ...]
    badge_from_outbox_state: Mapping[str, str]
    badge_precedence: tuple[str, ...]
    badge_source_namespace: str

    def __getitem__(self, name: str) -> StatusNamespace:
        try:
            return self.namespaces[name]
        except KeyError:
            raise KeyError(
                f"{name!r} is not a C16 namespace. Declared: {sorted(self.namespaces)}"
            ) from None

    @property
    def inbox(self) -> StatusNamespace:
        return self.namespaces["inbox"]

    @property
    def outbox(self) -> StatusNamespace:
        return self.namespaces["outbox"]

    @property
    def job(self) -> StatusNamespace:
        return self.namespaces["job"]

    @property
    def circuit(self) -> StatusNamespace:
        return self.namespaces["circuit"]

    def all_codes(self) -> frozenset[str]:
        out: set[str] = set()
        for ns in self.namespaces.values():
            out |= set(ns.codes)
        return frozenset(out)

    # -- the business-visible badge (plan section 8.4, "Queued-for-Zoho") ----
    def integration_state_badge(self, outbox_states: Iterable[str]) -> Optional[str]:
        """The badge an object carries ALONGSIDE its C3 business status.

        This is the fifth state the plan calls out, and the reason the
        registries are split at all: the UI needs to say "queued for Zoho"
        without a 22nd business status existing. The badge is derived, has its
        own slot and its own label, and is never rendered by the C3 status pill.

        Returns ``None`` when the object has no outbox row. That is not a
        fourth badge value and it is emphatically not ``SENT``: an object that
        was never queued has no integration state to report.

        An object can carry several outbox rows - an emission and a later
        amendment - so the result is resolved by the declared precedence
        (FAILED > QUEUED > SENT) rather than by row order.
        """
        badges = set()
        for state in outbox_states:
            if state not in self.outbox.codes:
                raise IllegalTransition(
                    f"{state!r} is not an outbox status; the badge cannot be "
                    f"derived from it. Declared: {sorted(self.outbox.codes)}"
                )
            badges.add(self.badge_from_outbox_state[state])
        for candidate in self.badge_precedence:
            if candidate in badges:
                return candidate
        return None


def _build_namespace(name: str, doc: dict) -> StatusNamespace:
    statuses = {
        s["code"]: IntegrationStatus(s["code"], s["meaning"], bool(s["terminal"]))
        for s in doc["statuses"]
    }
    transitions = tuple(
        Transition(
            source=t["from"],
            target=t["to"],
            trigger=t["trigger"],
            writer=t["writer"],
            side_effect=t.get("side_effect"),
            note=t.get("note"),
        )
        for t in doc["transitions"]
    )
    return StatusNamespace(
        name=name,
        description=doc["description"],
        initial_status=doc["initial_status"],
        statuses=statuses,
        transitions=transitions,
    )


def _validate_integration(reg: IntegrationRegistry, raw: dict) -> None:
    writers = {k for k in raw["writers"] if k != "note"}
    for name, ns in reg.namespaces.items():
        unknown = {t.target for t in ns.transitions} - set(ns.codes)
        if unknown:
            raise ContractError(f"{name}: transitions target undeclared states {sorted(unknown)}")
        bad_sources = {t.source for t in ns.transitions if t.source is not None} - set(ns.codes)
        if bad_sources:
            raise ContractError(f"{name}: transitions leave undeclared states {sorted(bad_sources)}")
        if ns.initial_status not in ns.codes:
            raise ContractError(f"{name}: initial_status {ns.initial_status!r} is not declared")
        unreachable = set(ns.codes) - set(ns.reachable())
        if unreachable:
            raise ContractError(
                f"{name}: {sorted(unreachable)} cannot be reached from creation. "
                "A state no writer can produce is not a state."
            )
        for code, status in ns.statuses.items():
            outgoing = [t for t in ns.transitions if t.source == code]
            if status.terminal and outgoing:
                raise ContractError(
                    f"{name}.{code} is declared terminal but has outgoing "
                    f"transitions {[t.trigger for t in outgoing]}"
                )
            if not status.terminal and not outgoing:
                raise ContractError(
                    f"{name}.{code} is declared non-terminal but nothing leaves it"
                )
        for t in ns.transitions:
            if t.writer not in writers:
                raise ContractError(
                    f"{name}: transition {t.trigger} names writer {t.writer!r}, "
                    f"which is not in the writer registry {sorted(writers)}"
                )

    badge_targets = set(reg.badge_from_outbox_state.values())
    if not badge_targets <= set(reg.badge_values):
        raise ContractError(
            f"badge derivation produces {sorted(badge_targets - set(reg.badge_values))}, "
            "which are not declared badge values"
        )
    if set(reg.badge_from_outbox_state) != set(reg.outbox.codes):
        raise ContractError(
            "every outbox status must derive a badge; missing "
            f"{sorted(set(reg.outbox.codes) - set(reg.badge_from_outbox_state))}"
        )
    if set(reg.badge_precedence) != set(reg.badge_values):
        raise ContractError("badge precedence must order exactly the declared badge values")


@lru_cache(maxsize=1)
def integration_registry() -> IntegrationRegistry:
    raw = _load("C16_integration_statuses.json")
    bridge = raw["business_visible_bridge"]
    reg = IntegrationRegistry(
        version=raw["version"],
        namespaces={n: _build_namespace(n, d) for n, d in raw["namespaces"].items()},
        badge_values=tuple(bridge["badge_values"]),
        badge_from_outbox_state=dict(bridge["badge_from_outbox_state"]),
        badge_precedence=tuple(bridge["precedence"]),
        badge_source_namespace=bridge["badge_source_namespace"],
    )
    _validate_integration(reg, raw)
    return reg


# ===========================================================================
# C3 - the frozen business registry, as a read-only guard
# ===========================================================================
@lru_cache(maxsize=1)
def business_statuses() -> frozenset[str]:
    """The 21 frozen C3 codes. This module reads them; it never adds one."""
    return frozenset(s["code"] for s in _load("C3_statuses.json")["statuses"])


def assert_business_screen_status(code: str) -> str:
    """Gate for anything about to be rendered as a business status pill.

    An integration or approval status reaching a business screen is a defect,
    so the cheapest place to catch it is the moment a caller claims a string is
    a business status.
    """
    if code not in business_statuses():
        raise ValueError(
            f"{code!r} is not one of the 21 frozen C3 business statuses. "
            "Integration state belongs on SCR-26/38/39, or in the "
            "integration_state badge - never in a business status pill."
        )
    return code


# ===========================================================================
# C17 - raw vendor statuses
# ===========================================================================
BUSINESS_STATUS = "BUSINESS_STATUS"
NO_BUSINESS_STATUS = "NO_BUSINESS_STATUS"
ACCOUNTING_STATUS_ONLY = "ACCOUNTING_STATUS_ONLY"
UNMAPPED = "UNMAPPED"

MAPPING_KINDS = frozenset({BUSINESS_STATUS, NO_BUSINESS_STATUS, ACCOUNTING_STATUS_ONLY})
COMPLETE_PER_VENDOR_SPEC = "COMPLETE_PER_VENDOR_SPEC"
INCOMPLETE_UNVERIFIED = "INCOMPLETE_UNVERIFIED"

#: Evidence classes whose rows may be applied. Anything else is inert.
ACTIVE_EVIDENCE_CLASSES = frozenset({"DOCUMENTED_RAW_VALUE", "PLAN_APPROVED"})


@dataclass(frozen=True)
class ReconciliationException:
    """The payload a caller persists when a raw value will not be interpreted.

    Deliberately a value object rather than an exception class: the plan says
    the record is ACCEPTED. Raising here would tempt a caller into dropping the
    payload, which is the one outcome the registry forbids.
    """
    kind: str
    product: str
    api_version: Optional[str]
    object: str
    field: str
    external_status_raw: str
    reason: str
    blocks_period_close: bool = True
    blocks_capitalisation: bool = True


@dataclass(frozen=True)
class MappingRow:
    product: str
    api_version: str
    object: str
    field: str
    raw: str
    maps_to: Optional[str]
    accounting_status: Optional[str]
    mapping_kind: str
    active: bool
    effective_from: Optional[date]
    effective_to: Optional[date]
    evidence_class: str
    provisional: bool = False
    note: Optional[str] = None

    def effective_on(self, as_of: date) -> bool:
        if self.effective_from is None:
            return False
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of < self.effective_to


@dataclass(frozen=True)
class MappingResult:
    """What the registry says about one raw value, and nothing more."""
    product: str
    api_version: Optional[str]
    object: str
    field: str
    raw: str
    kind: str
    business_status: Optional[str] = None
    accounting_status: Optional[str] = None
    provisional: bool = False
    exception: Optional[ReconciliationException] = None

    @property
    def is_mapped(self) -> bool:
        return self.kind != UNMAPPED

    @property
    def sets_business_status(self) -> bool:
        return self.kind == BUSINESS_STATUS


@dataclass(frozen=True)
class MappingBlock:
    product: str
    api_version: str
    object: str
    field: str
    coverage: str
    provisional: bool
    effective_from: Optional[date]
    effective_to: Optional[date]
    rows: Mapping[str, MappingRow] = field(default_factory=dict)

    def effective_on(self, as_of: date) -> bool:
        if self.effective_from is None:
            return False
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of < self.effective_to


@dataclass(frozen=True)
class ZohoStatusMap:
    version: str
    blocks: Mapping[tuple[str, str, str], MappingBlock]

    def block(self, product: str, object: str, field: str) -> Optional[MappingBlock]:
        return self.blocks.get((product, object, field))

    def resolve(
        self,
        product: str,
        object: str,
        field: str,
        raw: str,
        *,
        as_of: Optional[date] = None,
    ) -> MappingResult:
        """Interpret one verbatim raw value for exactly one product.

        ``product`` is a C17 product (``ERP``, ``BOOKS`` or ``INVENTORY``), not
        the adapter's ``BOOKS_INVENTORY`` - use :func:`contract_product` to get
        from one to the other. There is no cross-product fallback: a Books
        value is resolved against Books rows or not at all.

        ``raw`` is matched byte-for-byte. No case folding, no trimming.
        """
        as_of = as_of or date.today()
        block = self.block(product, object, field)
        if block is None:
            return self._unmapped(
                product, None, object, field, raw,
                "No mapping block exists for this (product, object, field). "
                "Nothing has been asserted about this field's values.",
            )
        if not block.effective_on(as_of):
            return self._unmapped(
                product, block.api_version, object, field, raw,
                f"The mapping block is not effective on {as_of.isoformat()} "
                f"(window {block.effective_from} .. {block.effective_to}).",
            )

        row = block.rows.get(raw)
        if row is None:
            if block.coverage == COMPLETE_PER_VENDOR_SPEC:
                reason = (
                    "The vendor specification documents a complete value set for "
                    "this field and this value is not in it. Treat it as a genuine "
                    "change on the vendor side, not as a gap in the registry."
                )
            else:
                reason = (
                    "This (product, object, field) has coverage "
                    f"{INCOMPLETE_UNVERIFIED}: its value set is not yet known. "
                    "The row is missing because nobody has confirmed it - which "
                    "is a different fact from a value that deliberately maps to "
                    "nothing."
                )
            return self._unmapped(product, block.api_version, object, field, raw, reason)

        if not row.active:
            return self._unmapped(
                product, block.api_version, object, field, raw,
                "A row exists for this value but is INACTIVE: its evidence class "
                f"is {row.evidence_class} ({UNVERIFIED_MARKER}). An unsourced "
                "spelling is not a mapping, so it is not applied.",
            )
        if not row.effective_on(as_of):
            return self._unmapped(
                product, block.api_version, object, field, raw,
                f"The row is not effective on {as_of.isoformat()} "
                f"(window {row.effective_from} .. {row.effective_to}).",
            )

        return MappingResult(
            product=product,
            api_version=block.api_version,
            object=object,
            field=field,
            raw=raw,
            kind=row.mapping_kind,
            business_status=row.maps_to,
            accounting_status=row.accounting_status,
            provisional=row.provisional,
        )

    @staticmethod
    def _unmapped(
        product: str,
        api_version: Optional[str],
        object: str,
        field: str,
        raw: str,
        reason: str,
    ) -> MappingResult:
        return MappingResult(
            product=product,
            api_version=api_version,
            object=object,
            field=field,
            raw=raw,
            kind=UNMAPPED,
            exception=ReconciliationException(
                kind=UNMAPPED_EXTERNAL_STATUS,
                product=product,
                api_version=api_version,
                object=object,
                field=field,
                external_status_raw=raw,
                reason=reason,
            ),
        )


def _build_block(block: dict) -> MappingBlock:
    rows: dict[str, MappingRow] = {}
    provisional = bool(block.get("provisional", False))
    for r in block["rows"]:
        row = MappingRow(
            product=block["product"],
            api_version=block["api_version"],
            object=block["object"],
            field=block["field"],
            raw=r["raw"],
            maps_to=r.get("maps_to"),
            accounting_status=r.get("accounting_status"),
            mapping_kind=r["mapping_kind"],
            active=bool(r["active"]),
            effective_from=_as_date(r.get("effective_from")),
            effective_to=_as_date(r.get("effective_to")),
            evidence_class=r["evidence"]["class"],
            provisional=provisional,
            note=r.get("note"),
        )
        if row.raw in rows:
            raise ContractError(
                f"{block['product']}/{block['object']}.{block['field']}: "
                f"duplicate row for raw {row.raw!r}"
            )
        rows[row.raw] = row
    return MappingBlock(
        product=block["product"],
        api_version=block["api_version"],
        object=block["object"],
        field=block["field"],
        coverage=block["coverage"],
        provisional=provisional,
        effective_from=_as_date(block.get("effective_from")),
        effective_to=_as_date(block.get("effective_to")),
        rows=rows,
    )


def _validate_map(smap: ZohoStatusMap) -> None:
    codes = business_statuses()
    for key, block in smap.blocks.items():
        if block.coverage not in {COMPLETE_PER_VENDOR_SPEC, INCOMPLETE_UNVERIFIED}:
            raise ContractError(f"{key}: unknown coverage {block.coverage!r}")
        for row in block.rows.values():
            if row.mapping_kind not in MAPPING_KINDS:
                raise ContractError(f"{key}/{row.raw}: unknown mapping_kind {row.mapping_kind!r}")
            if row.mapping_kind == BUSINESS_STATUS:
                if row.maps_to is None:
                    raise ContractError(f"{key}/{row.raw}: BUSINESS_STATUS with no maps_to")
                if row.maps_to not in codes:
                    raise ContractError(
                        f"{key}/{row.raw}: maps_to {row.maps_to!r} is not a C3 status"
                    )
            else:
                if row.maps_to is not None:
                    raise ContractError(
                        f"{key}/{row.raw}: {row.mapping_kind} must not carry a maps_to"
                    )
            if row.mapping_kind == ACCOUNTING_STATUS_ONLY and not row.accounting_status:
                raise ContractError(
                    f"{key}/{row.raw}: ACCOUNTING_STATUS_ONLY with no accounting_status"
                )
            if row.active and row.evidence_class not in ACTIVE_EVIDENCE_CLASSES:
                raise ContractError(
                    f"{key}/{row.raw}: active row with evidence class "
                    f"{row.evidence_class!r}. Only {sorted(ACTIVE_EVIDENCE_CLASSES)} "
                    "may be applied."
                )
            if row.active and row.effective_from is None:
                raise ContractError(f"{key}/{row.raw}: active row with no effective_from")


@lru_cache(maxsize=1)
def zoho_status_map() -> ZohoStatusMap:
    raw = _load("C17_zoho_status_map.json")
    blocks: dict[tuple[str, str, str], MappingBlock] = {}
    for b in raw["mappings"]:
        key = (b["product"], b["object"], b["field"])
        if key in blocks:
            raise ContractError(f"duplicate mapping block for {key}")
        blocks[key] = _build_block(b)
    smap = ZohoStatusMap(version=raw["version"], blocks=blocks)
    _validate_map(smap)
    return smap


# ---------------------------------------------------------------------------
# Adapter product -> contract product
# ---------------------------------------------------------------------------
#: Which C17 product owns each object when the adapter is BOOKS_INVENTORY.
#: Receives live in Inventory; bills and purchase orders live in Books. The
#: split is real, and collapsing it would let a Books row be applied to an
#: Inventory document.
_BOOKS_INVENTORY_OWNER = {
    "purchase_receive": "INVENTORY",
    "bill": "BOOKS",
    "purchase_order": "BOOKS",
}


def contract_product(adapter_product: str, object: str) -> str:
    """Map the adapter's ``product`` (C1) onto a C17 product.

    The adapter declares ``ERP`` or ``BOOKS_INVENTORY`` because that is the
    deployment choice (decision D-14). C17 keeps Books and Inventory apart
    because they are separate APIs with separate documentation. This is the
    only sanctioned bridge between the two vocabularies.
    """
    if adapter_product == "ERP":
        return "ERP"
    if adapter_product == "BOOKS_INVENTORY":
        try:
            return _BOOKS_INVENTORY_OWNER[object]
        except KeyError:
            raise ValueError(
                f"No Books/Inventory owner declared for object {object!r}. "
                "Guessing which product owns it would breach the product "
                "separation rule."
            ) from None
    raise ValueError(f"Unknown adapter product {adapter_product!r}")


__all__ = [
    "ACCOUNTING_STATUS_ONLY",
    "ACTIVE_EVIDENCE_CLASSES",
    "BUSINESS_STATUS",
    "COMPLETE_PER_VENDOR_SPEC",
    "INCOMPLETE_UNVERIFIED",
    "NO_BUSINESS_STATUS",
    "UNMAPPED",
    "UNMAPPED_EXTERNAL_STATUS",
    "UNVERIFIED_MARKER",
    "ContractError",
    "IllegalTransition",
    "IntegrationRegistry",
    "IntegrationStatus",
    "MappingBlock",
    "MappingResult",
    "MappingRow",
    "ReconciliationException",
    "StatusNamespace",
    "Transition",
    "ZohoStatusMap",
    "assert_business_screen_status",
    "business_statuses",
    "contract_product",
    "integration_registry",
    "zoho_status_map",
]
