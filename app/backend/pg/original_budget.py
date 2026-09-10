"""The ORIGINAL BUDGET document and the BUDGET CATEGORY master (Fable 5.1).

Closes the gap the approved plan's U1 "Create budget" left open: before this
module the very first grant on a control cell was, per ``budget.record_original``,
"a seed/migration concern" -- no document, no number, no approval, no
maker-checker, no screen. This module is that document.

What it owns (migration 026):

* ``budget_category`` -- a classification dimension SEPARATE from
  ``budget_head`` (AMB-04, product-owner decision). Never an alias, never
  derived from ``asset_category`` or ``item_category``.
* ``original_budget`` / ``original_budget_line`` -- the maker-checker document
  whose RELEASE writes exactly one ``kind='ORIGINAL'`` ``budget_line`` per line,
  creates the control cell and ledger cell when the cell did not exist, and
  classifies the cell. After release the document, its lines, the ORIGINAL
  lines and the cell's category are immutable AT THE DATABASE LEVEL (triggers
  in 026 and 003); this module never tries to edit them and its tests prove
  that trying fails.

Control model, restated because it is the load-bearing decision: the CONTROL
CELL stays ``(wbs_id, budget_head_id)``. One category per cell, set on
release. Category is a reporting/classification dimension; Budget Head is the
control dimension. Filtering by one is not filtering by the other, and
``tests/test_pg_original_budget.py::test_category_and_head_filters_select_different_sets``
holds that.

Money is integer paise end to end. The API accepts either ``amount_paise`` (int)
or ``amount_rupees`` (an exact decimal string, converted by ``money.to_paise``
at the boundary); nothing here does float arithmetic and ``_as_paise`` refuses
anything that is not an ``int``.

Every mutating function takes ``actor`` (the server-derived session user) and
writes an ``audit_log`` entry on stream ``ORIGINAL_BUDGET:<budget_id>`` through
``pg/audit.append`` -- the same chain the approval engine reads
``contributor_set`` from, which is what makes the maker ineligible to approve.

Errors raise :class:`OriginalBudgetError` with an RFC-7807 ``code`` and a
status. Out-of-scope READS are 404 (``repo.query`` returns nothing for a row
the scope predicate excludes, and this module never distinguishes "absent"
from "invisible"); out-of-scope WRITES are refused by the row-level policy's
``WITH CHECK`` before anything is written.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

from .. import money
from . import audit as audit_mod
from . import repo
from .budget import recompute_cell
from .engine import Session
from .locking import lock_affected_cells
from .masters import issue_number

OBJECT_TYPE = "ORIGINAL_BUDGET"
NUMBERING_SERIES = "ORIGINAL_BUDGET"
CUSTOM_FIELD_TARGET = "BUDGET"

STATUS_DRAFT = "DRAFT"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_RELEASED = "RELEASED"
STATUS_REJECTED = "REJECTED"
STATUS_RETURNED = "RETURNED"
STATUS_CANCELLED = "CANCELLED"
EDITABLE_STATUSES = frozenset({STATUS_DRAFT, STATUS_RETURNED})

#: Configurable default (recorded assumption): a cell that already carries a
#: kind='ORIGINAL' budget_line may not receive a second original grant. A
#: later grant on a funded cell is a SUPPLEMENT (budget_revision), which is a
#: separate controlled record by REQ-REV-008. Set to False only with a
#: product decision that "original" can be additive.
REFUSE_SECOND_ORIGINAL = True

#: Configurable default: how many lines a synchronous import may carry. Above
#: this the request must go through the job path (returns 202 + job id) so no
#: request exceeds AppSail's 30 s budget.
IMPORT_SYNC_ROW_LIMIT = 500

_FISCAL_YEAR_RE = re.compile(r"^(\d{4})-(\d{2})$")
_CATEGORY_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,39}$")

_PROJECT_SCOPE = {
    "project": "p.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}
_DOC_SCOPE = {
    "project": "ob.project_id",
    "entity": "ob.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}

SELECTOR_KINDS = ("entity", "project", "wbs", "budget_head", "budget_category",
                  "division", "branch", "zone", "plant", "location", "period",
                  "fiscal_year")


class OriginalBudgetError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400,
                 detail: Any = None):
        self.code, self.message, self.status, self.detail = code, message, status, detail
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400, detail: Any = None) -> None:
    raise OriginalBudgetError(code, message, status=status, detail=detail)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _as_paise(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _err("MONEY_NOT_INTEGER",
             f"{field} must be an integer number of paise, got {type(value).__name__}.",
             status=422)
    return value


def _amount_from(line: Mapping[str, Any], *, field_prefix: str = "") -> int:
    """Integer paise from either ``amount_paise`` or ``amount_rupees``."""
    if line.get("amount_paise") is not None:
        amount = _as_paise(line["amount_paise"], field=f"{field_prefix}amount_paise")
    elif line.get("amount_rupees") not in (None, ""):
        try:
            amount = money.to_paise(str(line["amount_rupees"]), field=f"{field_prefix}amount_rupees")
        except money.MoneyError as exc:
            _err("INVALID_AMOUNT", str(exc), status=422)
    else:
        _err("AMOUNT_REQUIRED", f"{field_prefix}amount_paise or amount_rupees is required.", status=422)
    if amount <= 0:
        _err("INVALID_AMOUNT", f"{field_prefix}amount must be a positive number of paise.", status=422)
    return amount


def _fiscal_year_ok(value: str) -> str:
    m = _FISCAL_YEAR_RE.match(value or "")
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        _err("INVALID_FISCAL_YEAR",
             "fiscal_year must look like 2026-27 (April-start Indian financial year).",
             status=422)
    return value


# ============================================================================
# Budget category master
# ============================================================================
def list_categories(session: Session, *, include_inactive: bool = False,
                    entity_id: str | None = None) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: reference master, RLS applies its own entity waiver
        """
        SELECT category_id, code, name, description, parent_category_id, display_order,
               active, entity_id, effective_from, effective_to, version_no,
               created_at, created_by, updated_at, updated_by
        FROM budget_category
        WHERE (%(all)s OR active)
          AND (%(entity)s::text IS NULL OR entity_id IS NULL OR entity_id = %(entity)s)
        ORDER BY display_order, code
        """,
        {"all": include_inactive, "entity": entity_id})
    keys = ("category_id", "code", "name", "description", "parent_category_id",
            "display_order", "active", "entity_id", "effective_from", "effective_to",
            "version_no", "created_at", "created_by", "updated_at", "updated_by")
    return [{k: _iso(v) for k, v in zip(keys, r)} for r in rows]


def get_category(session: Session, category_id: str) -> dict[str, Any]:
    for row in list_categories(session, include_inactive=True):
        if row["category_id"] == category_id:
            return row
    _err("CATEGORY_NOT_FOUND", f"Budget category {category_id} does not exist.", status=404)


def create_category(session: Session, *, actor: str, code: str, name: str,
                    description: str | None = None, parent_category_id: str | None = None,
                    display_order: int = 100, entity_id: str | None = None,
                    effective_from: date | None = None, effective_to: date | None = None,
                    correlation_id: str | None = None) -> dict[str, Any]:
    code = (code or "").strip().upper()
    if not _CATEGORY_CODE_RE.match(code):
        _err("INVALID_CATEGORY_CODE",
             "code must be 2-40 characters of A-Z, 0-9, _ or -, starting with a letter or digit.",
             status=422)
    if not (name or "").strip():
        _err("CATEGORY_NAME_REQUIRED", "name is required.", status=422)
    if session.fetchone("SELECT 1 FROM budget_category WHERE code = %s", (code,)):
        _err("CATEGORY_CODE_EXISTS", f"A budget category with code {code} already exists.", status=409)
    if parent_category_id:
        parent = session.fetchone("SELECT active FROM budget_category WHERE category_id = %s",
                                  (parent_category_id,))
        if parent is None:
            _err("CATEGORY_NOT_FOUND", f"Parent category {parent_category_id} does not exist.", status=404)
        if not parent[0]:
            _err("CATEGORY_PARENT_INACTIVE", "An inactive category cannot be a parent.", status=409)
    if entity_id and not session.fetchone("SELECT 1 FROM entity WHERE entity_id = %s", (entity_id,)):
        _err("ENTITY_NOT_FOUND", f"Entity {entity_id} does not exist.", status=404)
    if effective_from and effective_to and effective_to < effective_from:
        _err("INVALID_EFFECTIVE_RANGE", "effective_to must not precede effective_from.", status=422)
    category_id = _new_id("BC")
    session.execute(
        """
        INSERT INTO budget_category
            (category_id, code, name, description, parent_category_id, display_order,
             active, entity_id, effective_from, effective_to, created_by, updated_by)
        VALUES (%(id)s, %(code)s, %(name)s, %(desc)s, %(parent)s, %(order)s, true,
                %(entity)s, %(eff_from)s, %(eff_to)s, %(actor)s, %(actor)s)
        """,
        {"id": category_id, "code": code, "name": name.strip(), "desc": description,
         "parent": parent_category_id, "order": int(display_order), "entity": entity_id,
         "eff_from": effective_from, "eff_to": effective_to, "actor": actor})
    audit_mod.append(session, actor, "CATEGORY_CREATE", "BUDGET_CATEGORY", category_id,
                     f"code={code} name={name.strip()!r} entity={entity_id or 'ALL'}",
                     correlation_id=correlation_id)
    return get_category(session, category_id)


def update_category(session: Session, *, actor: str, category_id: str,
                    expected_version: int, name: str | None = None,
                    description: str | None = None, display_order: int | None = None,
                    parent_category_id: str | None = None, effective_from: date | None = None,
                    effective_to: date | None = None,
                    correlation_id: str | None = None) -> dict[str, Any]:
    current = get_category(session, category_id)
    if current["version_no"] != expected_version:
        _err("CATEGORY_STALE", "The category changed since you loaded it; reload and retry.",
             status=409, detail={"version_no": current["version_no"]})
    if parent_category_id == category_id:
        _err("CATEGORY_OWN_PARENT", "A category cannot be its own parent.", status=422)
    if parent_category_id:
        # No cycles: walk up from the proposed parent and refuse if we meet ourselves.
        cursor = parent_category_id
        for _ in range(64):
            row = session.fetchone("SELECT parent_category_id FROM budget_category WHERE category_id = %s",
                                   (cursor,))
            if row is None:
                _err("CATEGORY_NOT_FOUND", f"Parent category {parent_category_id} does not exist.", status=404)
            if row[0] == category_id:
                _err("CATEGORY_CYCLE", "That parent would make the hierarchy circular.", status=422)
            if row[0] is None:
                break
            cursor = row[0]
    session.execute(
        """
        UPDATE budget_category
           SET name = COALESCE(%(name)s, name),
               description = COALESCE(%(desc)s, description),
               display_order = COALESCE(%(order)s, display_order),
               parent_category_id = COALESCE(%(parent)s, parent_category_id),
               effective_from = COALESCE(%(eff_from)s, effective_from),
               effective_to = COALESCE(%(eff_to)s, effective_to),
               updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
         WHERE category_id = %(id)s
        """,
        {"id": category_id, "name": (name or None) and name.strip(), "desc": description,
         "order": display_order, "parent": parent_category_id, "eff_from": effective_from,
         "eff_to": effective_to, "actor": actor})
    audit_mod.append(session, actor, "CATEGORY_UPDATE", "BUDGET_CATEGORY", category_id,
                     "category updated", correlation_id=correlation_id)
    return get_category(session, category_id)


def deactivate_category(session: Session, *, actor: str, category_id: str,
                        correlation_id: str | None = None) -> dict[str, Any]:
    """Governed deactivation: refused while an ACTIVE child exists or while a
    DRAFT/SUBMITTED original budget still names it. Released cells keep it
    -- history is never re-classified."""
    current = get_category(session, category_id)
    if not current["active"]:
        return current
    child = session.fetchone(
        "SELECT code FROM budget_category WHERE parent_category_id = %s AND active LIMIT 1",
        (category_id,))
    if child:
        _err("CATEGORY_HAS_ACTIVE_CHILDREN",
             f"Deactivate child category {child[0]} first.", status=409)
    live = session.fetchone(
        """
        SELECT ob.budget_number FROM original_budget_line l
        JOIN original_budget ob ON ob.budget_id = l.budget_id
        WHERE l.budget_category_id = %s AND ob.status IN ('DRAFT', 'SUBMITTED', 'RETURNED')
        LIMIT 1
        """, (category_id,))
    if live:
        _err("CATEGORY_IN_USE_BY_OPEN_BUDGET",
             f"Original budget {live[0]} still names this category; release or cancel it first.",
             status=409)
    session.execute(
        "UPDATE budget_category SET active = false, updated_at = now(), updated_by = %s, "
        "version_no = version_no + 1 WHERE category_id = %s", (actor, category_id))
    audit_mod.append(session, actor, "CATEGORY_DEACTIVATE", "BUDGET_CATEGORY", category_id,
                     "category deactivated", correlation_id=correlation_id)
    return get_category(session, category_id)


# ============================================================================
# Governed selectors -- searchable master data, scoped, never raw ids typed
# ============================================================================
def search_selector(session: Session, *, kind: str, q: str = "",
                    project_id: str | None = None, entity_id: str | None = None,
                    limit: int = 25) -> list[dict[str, Any]]:
    """Options for a picker. Every query is scoped through ``repo.query`` where
    the table carries a dimension; reference masters go through RLS alone."""
    if kind not in SELECTOR_KINDS:
        _err("SELECTOR_KIND_UNKNOWN", f"Unknown selector {kind!r}.", status=400)
    limit = max(1, min(int(limit), 100))
    like = f"%{(q or '').strip()}%"
    params: dict[str, Any] = {"like": like, "limit": limit, "project": project_id, "entity": entity_id}

    if kind == "fiscal_year":
        today = date.today()
        start = today.year - 1 if today.month >= 4 else today.year - 2
        years = [f"{y}-{str(y + 1)[-2:]}" for y in range(start, start + 4)]
        return [{"id": y, "label": y} for y in years if like.strip("%").lower() in y.lower()]

    if kind == "entity":
        rows = repo.query(session,
                          "SELECT p.entity_id, p.name FROM entity p WHERE {scope} "
                          "AND (p.entity_id ILIKE %(like)s OR p.name ILIKE %(like)s) "
                          "ORDER BY p.name LIMIT %(limit)s", params,
                          columns={"entity": "p.entity_id", "plant": None, "location": None, "project": None})
        return [{"id": r[0], "label": r[1]} for r in rows]

    if kind == "project":
        rows = repo.query(session,
                          "SELECT p.project_id, p.capex_code, p.name, p.entity_id, p.plant_id, p.location_id "
                          "FROM project p WHERE {scope} "
                          "AND (%(entity)s::text IS NULL OR p.entity_id = %(entity)s) "
                          "AND (p.capex_code ILIKE %(like)s OR p.name ILIKE %(like)s) "
                          "ORDER BY p.capex_code LIMIT %(limit)s", params, columns=_PROJECT_SCOPE)
        return [{"id": r[0], "label": f"{r[1]} — {r[2]}", "entity_id": r[3], "plant_id": r[4],
                 "location_id": r[5]} for r in rows]

    if kind == "wbs":
        if not project_id:
            _err("SELECTOR_NEEDS_PROJECT", "Choose a project before choosing a WBS element.", status=422)
        rows = repo.query(session,
                          "SELECT w.wbs_id, w.wbs_code, w.description, w.level, w.budget_head_id "
                          "FROM wbs_element w JOIN project p ON p.project_id = w.project_id "
                          "WHERE {scope} AND w.project_id = %(project)s AND NOT w.is_abandoned "
                          "AND (w.wbs_code ILIKE %(like)s OR w.description ILIKE %(like)s) "
                          "ORDER BY w.wbs_path LIMIT %(limit)s", params, columns=_PROJECT_SCOPE)
        return [{"id": r[0], "label": f"{r[1]} — {r[2]}", "level": r[3], "default_budget_head_id": r[4]}
                for r in rows]

    if kind == "budget_head":
        rows = session.fetchall(  # scope-exempt: RLS-filtered reference master; entity narrowed below
            "SELECT budget_head_id, code, name, entity_id FROM budget_head WHERE active "
            "AND (%(entity)s::text IS NULL OR entity_id = %(entity)s) "
            "AND (code ILIKE %(like)s OR name ILIKE %(like)s) ORDER BY code LIMIT %(limit)s", params)
        return [{"id": r[0], "label": f"{r[1]} — {r[2]}", "entity_id": r[3]} for r in rows]

    if kind == "budget_category":
        rows = session.fetchall(  # scope-exempt: reference master under its own RLS waiver
            "SELECT category_id, code, name, parent_category_id, entity_id FROM budget_category "
            "WHERE active AND (%(entity)s::text IS NULL OR entity_id IS NULL OR entity_id = %(entity)s) "
            "AND (effective_from IS NULL OR effective_from <= CURRENT_DATE) "
            "AND (effective_to IS NULL OR effective_to >= CURRENT_DATE) "
            "AND (code ILIKE %(like)s OR name ILIKE %(like)s) "
            "ORDER BY display_order, code LIMIT %(limit)s", params)
        return [{"id": r[0], "label": f"{r[1]} — {r[2]}", "parent_category_id": r[3], "entity_id": r[4]}
                for r in rows]

    if kind in ("division", "branch", "zone", "plant", "location", "period"):
        table = {"division": "division", "branch": "branch", "zone": "zone", "plant": "plant",
                 "location": "location", "period": "accounting_period"}[kind]
        pk = {"division": "division_id", "branch": "branch_id", "zone": "zone_id",
              "plant": "plant_id", "location": "location_id", "period": "period_id"}[kind]
        label_col = "name" if kind != "period" else "period_id"
        extra = "" if kind != "period" else ", state"
        rows = repo.query(session,
                          f"SELECT p.{pk}, p.{label_col}, p.entity_id{extra.replace(', ', ', p.') if extra else ''} "
                          f"FROM {table} p WHERE {{scope}} "
                          "AND (%(entity)s::text IS NULL OR p.entity_id = %(entity)s) "
                          f"AND (p.{pk} ILIKE %(like)s OR p.{label_col}::text ILIKE %(like)s) "
                          f"ORDER BY p.{label_col} LIMIT %(limit)s", params,
                          columns={"entity": "p.entity_id", "plant": "p.plant_id" if kind == "location" else None,
                                   "location": None, "project": None})
        out = []
        for r in rows:
            item = {"id": r[0], "label": r[1] if kind != "period" else r[0], "entity_id": r[2]}
            if kind == "period":
                item["state"] = r[3]
            out.append(item)
        return out
    return []


# ============================================================================
# Custom fields (REQ-BUD-020) -- definitions applicable to BUDGET, enforced
# ============================================================================
def budget_custom_field_defs(session: Session) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: configuration, not business rows
        """
        SELECT d.field_def_id, d.code, d.label, d.data_type, d.select_options, d.is_required
        FROM custom_field_def d
        JOIN custom_field_applicability a ON a.field_def_id = d.field_def_id
        WHERE d.is_active AND a.is_active AND a.applies_to = %s
        ORDER BY d.code
        """, (CUSTOM_FIELD_TARGET,))
    keys = ("field_def_id", "code", "label", "data_type", "select_options", "is_required")
    return [dict(zip(keys, r)) for r in rows]


def validate_custom_fields(defs: Sequence[Mapping[str, Any]], values: Mapping[str, Any] | None,
                           *, where: str = "custom_fields") -> dict[str, Any]:
    """Refuse a missing required field, an unknown field, or a value of the
    wrong shape. NUMBER values are kept as exact decimal STRINGS -- a jsonb
    number would be a float on the way back out."""
    values = dict(values or {})
    known = {d["code"]: d for d in defs}
    problems: list[str] = []
    for code in values:
        if code not in known:
            problems.append(f"{where}.{code}: not a budget custom field")
    out: dict[str, Any] = {}
    for code, d in known.items():
        v = values.get(code)
        if v in (None, ""):
            if d["is_required"]:
                problems.append(f"{where}.{code}: required")
            continue
        t = d["data_type"]
        if t == "TEXT":
            if not isinstance(v, str):
                problems.append(f"{where}.{code}: must be text")
            else:
                out[code] = v
        elif t == "NUMBER":
            try:
                out[code] = str(Decimal(str(v)))
            except InvalidOperation:
                problems.append(f"{where}.{code}: must be a number")
        elif t == "DATE":
            try:
                out[code] = date.fromisoformat(str(v)).isoformat()
            except ValueError:
                problems.append(f"{where}.{code}: must be an ISO date")
        elif t == "BOOLEAN":
            if isinstance(v, bool):
                out[code] = v
            elif str(v).lower() in ("true", "false", "yes", "no"):
                out[code] = str(v).lower() in ("true", "yes")
            else:
                problems.append(f"{where}.{code}: must be true or false")
        elif t == "SELECT":
            options = d["select_options"] or []
            if v not in options:
                problems.append(f"{where}.{code}: must be one of {options}")
            else:
                out[code] = v
    if problems:
        _err("CUSTOM_FIELD_INVALID", "; ".join(problems), status=422, detail=problems)
    return out


# ============================================================================
# Line validation -- one place, used by create, update, import preview
# ============================================================================
def _validate_lines(session: Session, *, project: Mapping[str, Any],
                    lines: Sequence[Mapping[str, Any]], defs: Sequence[Mapping[str, Any]],
                    editing_budget_id: str | None = None) -> list[dict[str, Any]]:
    if not lines:
        _err("LINES_REQUIRED", "An original budget needs at least one line.", status=422)
    entity_id = project["entity_id"]
    project_id = project["project_id"]
    seen_cells: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    def bad(idx: int, field: str, code: str, msg: str) -> None:
        problems.append({"line": idx + 1, "field": field, "code": code, "message": msg})

    wbs_rows = {r[0]: r for r in session.fetchall(
        "SELECT wbs_id, wbs_code, is_abandoned, budget_head_id FROM wbs_element WHERE project_id = %s",
        (project_id,))}
    heads = {r[0]: r for r in session.fetchall(
        "SELECT budget_head_id, entity_id, active FROM budget_head", ())}
    categories = {r[0]: r for r in session.fetchall(
        "SELECT category_id, active, entity_id, effective_from, effective_to FROM budget_category", ())}
    masters = {}
    for kind, table, pk in (("division", "division", "division_id"), ("branch", "branch", "branch_id"),
                            ("zone", "zone", "zone_id"), ("plant", "plant", "plant_id"),
                            ("location", "location", "location_id")):
        masters[kind] = {r[0]: r[1] for r in session.fetchall(
            f"SELECT {pk}, entity_id FROM {table}", ())}
    existing_original = {(r[0], r[1]) for r in session.fetchall(
        "SELECT wbs_id, budget_head_id FROM budget_line WHERE kind = 'ORIGINAL'", ())}
    other_open = {(r[0], r[1]): r[2] for r in session.fetchall(
        """
        SELECT l.wbs_id, l.budget_head_id, ob.budget_number FROM original_budget_line l
        JOIN original_budget ob ON ob.budget_id = l.budget_id
        WHERE ob.status IN ('DRAFT', 'SUBMITTED', 'RETURNED')
          AND (%s::text IS NULL OR ob.budget_id <> %s)
        """, (editing_budget_id, editing_budget_id))}
    today = date.today()

    for idx, raw in enumerate(lines):
        line: dict[str, Any] = {}
        wbs_id = raw.get("wbs_id")
        head_id = raw.get("budget_head_id")
        cat_id = raw.get("budget_category_id")
        if not wbs_id or wbs_id not in wbs_rows:
            bad(idx, "wbs_id", "WBS_NOT_IN_PROJECT", "WBS element does not belong to this project.")
        elif wbs_rows[wbs_id][2]:
            bad(idx, "wbs_id", "WBS_ABANDONED", "WBS element is abandoned.")
        if not head_id or head_id not in heads:
            bad(idx, "budget_head_id", "BUDGET_HEAD_NOT_FOUND", "Budget head does not exist.")
        else:
            h = heads[head_id]
            if not h[2]:
                bad(idx, "budget_head_id", "BUDGET_HEAD_INACTIVE", "Budget head is inactive.")
            elif h[1] not in (None, entity_id):
                bad(idx, "budget_head_id", "BUDGET_HEAD_WRONG_ENTITY", "Budget head belongs to another entity.")
        if not cat_id or cat_id not in categories:
            bad(idx, "budget_category_id", "CATEGORY_NOT_FOUND", "Budget category does not exist.")
        else:
            c = categories[cat_id]
            if not c[1]:
                bad(idx, "budget_category_id", "CATEGORY_INACTIVE", "Budget category is inactive.")
            elif c[2] not in (None, entity_id):
                bad(idx, "budget_category_id", "CATEGORY_WRONG_ENTITY", "Budget category is not applicable to this entity.")
            elif (c[3] and c[3] > today) or (c[4] and c[4] < today):
                bad(idx, "budget_category_id", "CATEGORY_NOT_EFFECTIVE", "Budget category is outside its effective dates.")
        for kind in ("division", "branch", "zone", "plant", "location"):
            val = raw.get(f"{kind}_id")
            if val:
                if val not in masters[kind]:
                    bad(idx, f"{kind}_id", f"{kind.upper()}_NOT_FOUND", f"{kind} does not exist.")
                elif masters[kind][val] != entity_id:
                    bad(idx, f"{kind}_id", f"{kind.upper()}_WRONG_ENTITY", f"{kind} belongs to another entity.")
            line[f"{kind}_id"] = val or None
        if line["plant_id"] is None and project.get("plant_id"):
            line["plant_id"] = project["plant_id"]
        if line["location_id"] is None and project.get("location_id"):
            line["location_id"] = project["location_id"]
        try:
            line["amount_paise"] = _amount_from(raw, field_prefix=f"lines[{idx + 1}].")
        except OriginalBudgetError as exc:
            bad(idx, "amount", exc.code, exc.message)
        try:
            line["custom_fields"] = validate_custom_fields(defs, raw.get("custom_fields"),
                                                           where=f"lines[{idx + 1}].custom_fields")
        except OriginalBudgetError as exc:
            for p in (exc.detail or [exc.message]):
                bad(idx, "custom_fields", "CUSTOM_FIELD_INVALID", str(p))
        if wbs_id and head_id:
            cell = (wbs_id, head_id)
            if cell in seen_cells:
                bad(idx, "wbs_id", "DUPLICATE_CELL_IN_DOCUMENT",
                    "This WBS x budget head already appears on an earlier line.")
            seen_cells.add(cell)
            if REFUSE_SECOND_ORIGINAL and cell in existing_original:
                bad(idx, "wbs_id", "CELL_ALREADY_HAS_ORIGINAL",
                    "This cell already carries a released original budget; raise a supplement instead.")
            if cell in other_open:
                bad(idx, "wbs_id", "CELL_IN_ANOTHER_OPEN_BUDGET",
                    f"This cell is already on open original budget {other_open[cell]}.")
        line.update({"line_no": idx + 1, "wbs_id": wbs_id, "budget_head_id": head_id,
                     "budget_category_id": cat_id,
                     "justification": (raw.get("justification") or None)})
        out.append(line)
    if problems:
        _err("BUDGET_LINES_INVALID", f"{len(problems)} line problem(s); see detail.",
             status=422, detail=problems)
    return out


def _project_in_scope(session: Session, project_id: str) -> dict[str, Any]:
    row = repo.query_one(session,
                         "SELECT p.project_id, p.entity_id, p.plant_id, p.location_id, p.capex_code, p.name "
                         "FROM project p WHERE p.project_id = %(id)s AND {scope}",
                         {"id": project_id}, columns=_PROJECT_SCOPE)
    if row is None:
        _err("PROJECT_NOT_FOUND", f"Project {project_id} does not exist.", status=404)
    return dict(zip(("project_id", "entity_id", "plant_id", "location_id", "capex_code", "name"), row))


def _period_ok(session: Session, period_id: str | None, entity_id: str) -> None:
    if not period_id:
        return
    row = session.fetchone("SELECT entity_id, state FROM accounting_period WHERE period_id = %s", (period_id,))
    if row is None or row[0] != entity_id:
        _err("PERIOD_NOT_FOUND", f"Accounting period {period_id} does not exist for this entity.", status=404)
    if row[1] == "CLOSED":
        _err("PERIOD_CLOSED", f"Accounting period {period_id} is CLOSED; a budget cannot be dated into it.",
             status=409)


# ============================================================================
# The document
# ============================================================================
def _load(session: Session, budget_id: str, *, for_update: bool = False) -> dict[str, Any]:
    row = repo.query_one(session,
                         """
                         SELECT ob.budget_id, ob.budget_number, ob.entity_id, ob.project_id, ob.fiscal_year,
                                ob.period_id, ob.title, ob.justification, ob.status, ob.approving_authority,
                                ob.approval_instance_id, ob.decision_note, ob.submitted_at, ob.submitted_by,
                                ob.decided_at, ob.released_at, ob.custom_fields, ob.created_at, ob.created_by,
                                ob.updated_at, ob.updated_by, ob.version_no, p.capex_code, p.name
                         FROM original_budget ob JOIN project p ON p.project_id = ob.project_id
                         WHERE ob.budget_id = %(id)s AND {scope}
                         """ + (" FOR UPDATE OF ob" if for_update else ""),
                         {"id": budget_id}, columns=_DOC_SCOPE)
    if row is None:
        _err("BUDGET_NOT_FOUND", f"Original budget {budget_id} does not exist.", status=404)
    keys = ("budget_id", "budget_number", "entity_id", "project_id", "fiscal_year", "period_id",
            "title", "justification", "status", "approving_authority", "approval_instance_id",
            "decision_note", "submitted_at", "submitted_by", "decided_at", "released_at",
            "custom_fields", "created_at", "created_by", "updated_at", "updated_by", "version_no",
            "capex_code", "project_name")
    doc = {k: _iso(v) for k, v in zip(keys, row)}
    doc["lines"] = _lines(session, budget_id)
    doc["total_paise"] = sum(l["amount_paise"] for l in doc["lines"])
    return doc


def _lines(session: Session, budget_id: str) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: parent already scope-checked; line RLS reaches the parent
        """
        SELECT l.line_id, l.line_no, l.wbs_id, w.wbs_code, w.description, l.budget_head_id, h.name,
               l.budget_category_id, c.code, c.name, l.division_id, l.branch_id, l.zone_id,
               l.plant_id, l.location_id, l.amount_paise, l.justification, l.custom_fields, l.budget_line_id
        FROM original_budget_line l
        JOIN wbs_element w ON w.wbs_id = l.wbs_id
        JOIN budget_head h ON h.budget_head_id = l.budget_head_id
        JOIN budget_category c ON c.category_id = l.budget_category_id
        WHERE l.budget_id = %s ORDER BY l.line_no
        """, (budget_id,))
    keys = ("line_id", "line_no", "wbs_id", "wbs_code", "wbs_description", "budget_head_id",
            "budget_head_name", "budget_category_id", "budget_category_code", "budget_category_name",
            "division_id", "branch_id", "zone_id", "plant_id", "location_id", "amount_paise",
            "justification", "custom_fields", "budget_line_id")
    return [dict(zip(keys, r)) for r in rows]


def get_budget(session: Session, budget_id: str) -> dict[str, Any]:
    return _load(session, budget_id)


def list_budgets(session: Session, *, project_id: str | None = None, status: str | None = None,
                 entity_id: str | None = None, fiscal_year: str | None = None,
                 limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    after = None
    if cursor:
        try:
            after = json.loads(cursor)
            after = (after["created_at"], after["budget_id"])
        except (ValueError, KeyError, TypeError):
            _err("INVALID_CURSOR", "cursor is not one this endpoint issued.", status=400)
    rows = repo.query(session,
                      """
                      SELECT ob.budget_id, ob.budget_number, ob.entity_id, ob.project_id, p.capex_code, p.name,
                             ob.fiscal_year, ob.period_id, ob.title, ob.status, ob.created_at, ob.created_by,
                             ob.approving_authority, ob.version_no,
                             (SELECT COALESCE(SUM(l.amount_paise), 0)::bigint FROM original_budget_line l
                               WHERE l.budget_id = ob.budget_id) AS total_paise,
                             (SELECT COUNT(*) FROM original_budget_line l WHERE l.budget_id = ob.budget_id) AS line_count
                      FROM original_budget ob JOIN project p ON p.project_id = ob.project_id
                      WHERE {scope}
                        AND (%(project)s::text IS NULL OR ob.project_id = %(project)s)
                        AND (%(status)s::text IS NULL OR ob.status = %(status)s)
                        AND (%(entity)s::text IS NULL OR ob.entity_id = %(entity)s)
                        AND (%(fy)s::text IS NULL OR ob.fiscal_year = %(fy)s)
                        AND (%(after_at)s::timestamptz IS NULL
                             OR (ob.created_at, ob.budget_id) < (%(after_at)s::timestamptz, %(after_id)s))
                      ORDER BY ob.created_at DESC, ob.budget_id DESC
                      LIMIT %(limit)s
                      """,
                      {"project": project_id, "status": status, "entity": entity_id, "fy": fiscal_year,
                       "after_at": after[0] if after else None, "after_id": after[1] if after else None,
                       "limit": limit + 1},
                      columns=_DOC_SCOPE)
    keys = ("budget_id", "budget_number", "entity_id", "project_id", "capex_code", "project_name",
            "fiscal_year", "period_id", "title", "status", "created_at", "created_by",
            "approving_authority", "version_no", "total_paise", "line_count")
    items = [{k: _iso(v) for k, v in zip(keys, r)} for r in rows[:limit]]
    for it in items:
        it["total_paise"] = int(it["total_paise"])
        it["line_count"] = int(it["line_count"])
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = json.dumps({"created_at": last["created_at"], "budget_id": last["budget_id"]})
    return {"items": items, "next_cursor": next_cursor}


def create_draft(session: Session, *, actor: str, project_id: str, fiscal_year: str,
                 title: str, lines: Sequence[Mapping[str, Any]], period_id: str | None = None,
                 justification: str | None = None, custom_fields: Mapping[str, Any] | None = None,
                 correlation_id: str | None = None) -> dict[str, Any]:
    if not (title or "").strip():
        _err("TITLE_REQUIRED", "title is required.", status=422)
    _fiscal_year_ok(fiscal_year)
    project = _project_in_scope(session, project_id)
    _period_ok(session, period_id, project["entity_id"])
    defs = budget_custom_field_defs(session)
    header_cf = validate_custom_fields(defs, custom_fields)
    clean = _validate_lines(session, project=project, lines=lines, defs=defs)

    budget_id = _new_id("OB")
    number = issue_number(session, NUMBERING_SERIES, actor=actor,
                          period_key=fiscal_year[:4], object_type=OBJECT_TYPE,
                          object_id=budget_id)["formatted_number"]
    session.execute(
        """
        INSERT INTO original_budget
            (budget_id, budget_number, entity_id, project_id, fiscal_year, period_id, title,
             justification, status, custom_fields, created_by, updated_by)
        VALUES (%(id)s, %(number)s, %(entity)s, %(project)s, %(fy)s, %(period)s, %(title)s,
                %(just)s, 'DRAFT', %(cf)s, %(actor)s, %(actor)s)
        """,
        {"id": budget_id, "number": number, "entity": project["entity_id"], "project": project_id,
         "fy": fiscal_year, "period": period_id, "title": title.strip(), "just": justification,
         "cf": Jsonb(header_cf), "actor": actor})
    _write_lines(session, budget_id, clean, actor)
    total = sum(l["amount_paise"] for l in clean)
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_CREATE", OBJECT_TYPE, budget_id,
                     f"{number} created as DRAFT for {project['capex_code']} {fiscal_year}: "
                     f"{len(clean)} line(s), total {total} paise",
                     correlation_id=correlation_id)
    return _load(session, budget_id)


def _write_lines(session: Session, budget_id: str, clean: Sequence[Mapping[str, Any]], actor: str) -> None:
    session.execute("DELETE FROM original_budget_line WHERE budget_id = %s", (budget_id,))
    for l in clean:
        session.execute(
            """
            INSERT INTO original_budget_line
                (line_id, budget_id, line_no, wbs_id, budget_head_id, budget_category_id,
                 division_id, branch_id, zone_id, plant_id, location_id, amount_paise,
                 justification, custom_fields, created_by, updated_by)
            VALUES (%(id)s, %(budget)s, %(no)s, %(wbs)s, %(head)s, %(cat)s, %(div)s, %(br)s,
                    %(zone)s, %(plant)s, %(loc)s, %(amt)s, %(just)s, %(cf)s, %(actor)s, %(actor)s)
            """,
            {"id": _new_id("OBL"), "budget": budget_id, "no": l["line_no"], "wbs": l["wbs_id"],
             "head": l["budget_head_id"], "cat": l["budget_category_id"], "div": l["division_id"],
             "br": l["branch_id"], "zone": l["zone_id"], "plant": l["plant_id"], "loc": l["location_id"],
             "amt": l["amount_paise"], "just": l["justification"], "cf": Jsonb(l["custom_fields"]),
             "actor": actor})


def update_draft(session: Session, *, actor: str, budget_id: str, expected_version: int,
                 title: str | None = None, justification: str | None = None,
                 period_id: str | None = None, fiscal_year: str | None = None,
                 lines: Sequence[Mapping[str, Any]] | None = None,
                 custom_fields: Mapping[str, Any] | None = None,
                 correlation_id: str | None = None) -> dict[str, Any]:
    doc = _load(session, budget_id, for_update=True)
    if doc["status"] not in EDITABLE_STATUSES:
        _err("BUDGET_NOT_EDITABLE", f"Original budget {doc['budget_number']} is {doc['status']} "
             f"and cannot be edited.", status=409)
    if doc["version_no"] != expected_version:
        _err("BUDGET_STALE", "The budget changed since you loaded it; reload and retry.",
             status=409, detail={"version_no": doc["version_no"]})
    project = _project_in_scope(session, doc["project_id"])
    if fiscal_year:
        _fiscal_year_ok(fiscal_year)
    _period_ok(session, period_id if period_id is not None else doc["period_id"], project["entity_id"])
    defs = budget_custom_field_defs(session)
    header_cf = validate_custom_fields(defs, custom_fields if custom_fields is not None else doc["custom_fields"])
    if lines is not None:
        clean = _validate_lines(session, project=project, lines=lines, defs=defs, editing_budget_id=budget_id)
        _write_lines(session, budget_id, clean, actor)
    session.execute(
        """
        UPDATE original_budget
           SET title = COALESCE(%(title)s, title), justification = COALESCE(%(just)s, justification),
               period_id = COALESCE(%(period)s, period_id), fiscal_year = COALESCE(%(fy)s, fiscal_year),
               custom_fields = %(cf)s, status = 'DRAFT',
               updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
         WHERE budget_id = %(id)s
        """,
        {"id": budget_id, "title": (title or None) and title.strip(), "just": justification,
         "period": period_id, "fy": fiscal_year, "cf": Jsonb(header_cf), "actor": actor})
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_UPDATE", OBJECT_TYPE, budget_id,
                     f"draft edited ({'lines replaced' if lines is not None else 'header only'})",
                     correlation_id=correlation_id)
    return _load(session, budget_id)


def snapshot(session: Session, *, budget_id: str) -> dict[str, Any]:
    """The immutable document the approval rules are evaluated against.
    Money is integer paise; ``amount_paise`` is the document total so a rule
    such as ``amount_paise > 50000000`` routes a large budget differently."""
    doc = _load(session, budget_id)
    return {
        "object_type": OBJECT_TYPE, "budget_id": budget_id, "budget_number": doc["budget_number"],
        "entity_id": doc["entity_id"], "project_id": doc["project_id"],
        "fiscal_year": doc["fiscal_year"], "period_id": doc["period_id"],
        "amount_paise": _as_paise(doc["total_paise"], field="total_paise"),
        "line_count": len(doc["lines"]),
        "cells": [{"wbs_id": l["wbs_id"], "budget_head_id": l["budget_head_id"],
                   "budget_category_id": l["budget_category_id"],
                   "amount_paise": _as_paise(l["amount_paise"], field="amount_paise")}
                  for l in doc["lines"]],
        "created_by": doc["created_by"], "version_no": doc["version_no"],
    }


def submit(session: Session, *, actor: str, budget_id: str, expected_version: int | None = None,
           business_date: date | None = None, correlation_id: str | None = None) -> dict[str, Any]:
    """Raise a DRAFT into the approval engine. Writes no cell: a submitted
    budget creates no spending capacity. An unroutable document comes back
    with ``refusal`` set and the EXCEPTION_PENDING instance committed."""
    from . import approvals as approvals_mod
    from .budget import live_approval_instance

    doc = _load(session, budget_id, for_update=True)
    if expected_version is not None and doc["version_no"] != expected_version:
        _err("BUDGET_STALE", "The budget changed since you loaded it; reload and retry.",
             status=409, detail={"version_no": doc["version_no"]})
    live = live_approval_instance(session, OBJECT_TYPE, budget_id)
    if live is not None:
        _err("ALREADY_SUBMITTED", f"{doc['budget_number']} is already under approval instance "
             f"{live['instance_id']} ({live['status']}).", status=409)
    if doc["status"] not in EDITABLE_STATUSES:
        _err("BUDGET_NOT_DRAFT", f"{doc['budget_number']} is {doc['status']}; only a draft can be submitted.",
             status=409)
    if not doc["lines"]:
        _err("LINES_REQUIRED", "An original budget needs at least one line before submission.", status=422)
    # Re-validate against today's masters: a category deactivated or a cell
    # funded since the draft was saved is refused here, not discovered at release.
    project = _project_in_scope(session, doc["project_id"])
    defs = budget_custom_field_defs(session)
    validate_custom_fields(defs, doc["custom_fields"])
    _validate_lines(session, project=project, lines=doc["lines"], defs=defs, editing_budget_id=budget_id)
    _period_ok(session, doc["period_id"], doc["entity_id"])

    snap = snapshot(session, budget_id=budget_id)
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_SUBMIT", OBJECT_TYPE, budget_id,
                     f"{doc['budget_number']} submitted for approval by {actor}; "
                     f"total {snap['amount_paise']} paise over {snap['line_count']} line(s)",
                     correlation_id=correlation_id)
    instance = approvals_mod.open_instance(
        session, object_type=OBJECT_TYPE, object_id=budget_id,
        object_version=doc["version_no"], snapshot=snap, maker_user_id=doc["created_by"],
        business_date=business_date, correlation_id=correlation_id)
    status = instance.get("status")
    submitted = status == "OPEN"
    if submitted:
        session.execute(
            "UPDATE original_budget SET status = 'SUBMITTED', submitted_at = now(), submitted_by = %s, "
            "approval_instance_id = %s, updated_at = now(), updated_by = %s WHERE budget_id = %s",
            (actor, instance.get("instance_id"), actor, budget_id))
    result = {"budget_id": budget_id, "budget_number": doc["budget_number"],
              "status": STATUS_SUBMITTED if submitted else doc["status"],
              "approval_instance_id": instance.get("instance_id"),
              "approval_status": status, "submitted": submitted, "refusal": None}
    if not submitted:
        outcome = instance.get("outcome") or {}
        result["refusal"] = {"code": outcome.get("code") or "APPROVAL_ROUTE_UNRESOLVED",
                             "message": outcome.get("detail") or
                             "No approval route matched this budget; it is recorded as EXCEPTION_PENDING.",
                             "status": 409}
    return result


def cancel(session: Session, *, actor: str, budget_id: str, reason: str,
           correlation_id: str | None = None) -> dict[str, Any]:
    doc = _load(session, budget_id, for_update=True)
    if doc["status"] in (STATUS_RELEASED, STATUS_CANCELLED):
        _err("BUDGET_NOT_CANCELLABLE", f"{doc['budget_number']} is {doc['status']}.", status=409)
    if doc["status"] == STATUS_SUBMITTED:
        _err("BUDGET_UNDER_APPROVAL", "Recall the approval instance first; a submitted budget "
             "is cancelled through the approval engine.", status=409)
    if not (reason or "").strip():
        _err("REASON_REQUIRED", "A cancellation reason is required.", status=422)
    session.execute(
        "UPDATE original_budget SET status = 'CANCELLED', decision_note = %s, decided_at = now(), "
        "updated_at = now(), updated_by = %s, version_no = version_no + 1 WHERE budget_id = %s",
        (reason.strip(), actor, budget_id))
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_CANCEL", OBJECT_TYPE, budget_id,
                     f"cancelled: {reason.strip()}", correlation_id=correlation_id)
    return _load(session, budget_id)


# ============================================================================
# Release -- the approval write-back. Never callable from a route.
# ============================================================================
def release(session: Session, *, budget_id: str, actor: str, approval_instance_id: str,
            correlation_id: str | None = None) -> dict[str, Any]:
    """Apply an APPROVED outcome: create the cells that do not exist, lock
    every affected cell in the documented order, write one kind='ORIGINAL'
    budget_line per line carrying the category, classify the cell, recompute,
    and mark the document RELEASED with the approving authority captured.

    ``actor`` is the approver the engine names -- never the maker; the
    engine's ``require_separation`` and ``contributor_set`` have already
    refused a self-approval before this runs, and the audit stream this
    module wrote at CREATE/SUBMIT is what fed ``contributor_set``.
    """
    doc = _load(session, budget_id, for_update=True)
    if doc["status"] == STATUS_RELEASED:
        return doc                                   # idempotent replay
    if doc["status"] != STATUS_SUBMITTED:
        _err("BUDGET_NOT_SUBMITTED", f"{doc['budget_number']} is {doc['status']}; only a submitted "
             f"budget can be released.", status=409)
    if actor == doc["created_by"]:
        _err("SELF_APPROVAL", "The maker of an original budget cannot release it.", status=403)
    project = _project_in_scope(session, doc["project_id"])
    defs = budget_custom_field_defs(session)
    clean = _validate_lines(session, project=project, lines=doc["lines"], defs=defs,
                            editing_budget_id=budget_id)
    cells = [(l["wbs_id"], l["budget_head_id"]) for l in clean]

    # Cells first (a lock on a row that does not exist locks nothing), then the
    # ordered lock over the complete affected set, exactly once.
    for wbs_id, head_id in cells:
        session.execute(
            """
            INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by)
            VALUES (%s, %s, 0, %s) ON CONFLICT (wbs_id, budget_head_id) DO NOTHING
            """, (wbs_id, head_id, actor))
        session.execute(
            """
            INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, updated_by)
            VALUES (%s, %s, %s) ON CONFLICT (wbs_id, budget_head_id) DO NOTHING
            """, (wbs_id, head_id, actor))
    lock_affected_cells(session, cells)

    effective_from = date(int(doc["fiscal_year"][:4]), 4, 1)
    written: list[dict[str, Any]] = []
    for l, src in zip(clean, doc["lines"]):
        line_id = _new_id("BL")
        session.execute(
            """
            INSERT INTO budget_line
                (budget_line_id, wbs_id, budget_head_id, kind, amount_paise, effective_from,
                 status, justification, created_by, updated_by, budget_category_id)
            VALUES (%(id)s, %(wbs)s, %(head)s, 'ORIGINAL', %(amt)s, %(eff)s, 'Approved',
                    %(just)s, %(actor)s, %(actor)s, %(cat)s)
            """,
            {"id": line_id, "wbs": l["wbs_id"], "head": l["budget_head_id"], "amt": l["amount_paise"],
             "eff": effective_from, "just": l["justification"] or doc["title"], "actor": actor,
             "cat": l["budget_category_id"]})
        session.execute(
            "UPDATE budget_control_cell SET budget_category_id = %s, updated_at = now(), updated_by = %s "
            "WHERE wbs_id = %s AND budget_head_id = %s AND budget_category_id IS NULL",
            (l["budget_category_id"], actor, l["wbs_id"], l["budget_head_id"]))
        position = recompute_cell(session, l["wbs_id"], l["budget_head_id"], actor=actor)
        session.execute(
            "UPDATE original_budget_line SET budget_line_id = %s, updated_at = now(), updated_by = %s "
            "WHERE line_id = %s", (line_id, actor, src["line_id"]))
        for code, value in (l["custom_fields"] or {}).items():
            _store_custom_value(session, actor, code, f"{budget_id}:{src['line_no']}", value)
        written.append({"line_no": l["line_no"], "budget_line_id": line_id,
                        "wbs_id": l["wbs_id"], "budget_head_id": l["budget_head_id"],
                        "budget_category_id": l["budget_category_id"],
                        "amount_paise": l["amount_paise"], "cell": position})
    for code, value in (doc["custom_fields"] or {}).items():
        _store_custom_value(session, actor, code, budget_id, value)

    session.execute(
        """
        UPDATE original_budget
           SET status = 'RELEASED', approving_authority = %(actor)s, approval_instance_id = %(inst)s,
               decided_at = now(), released_at = now(), updated_at = now(), updated_by = %(actor)s,
               version_no = version_no + 1
         WHERE budget_id = %(id)s
        """, {"actor": actor, "inst": approval_instance_id, "id": budget_id})
    total = sum(w["amount_paise"] for w in written)
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_RELEASE", OBJECT_TYPE, budget_id,
                     f"{doc['budget_number']} RELEASED by {actor} under {approval_instance_id}: "
                     f"{len(written)} ORIGINAL budget_line(s), total {total} paise",
                     correlation_id=correlation_id)
    out = _load(session, budget_id)
    out["written"] = written
    return out


def _store_custom_value(session: Session, actor: str, code: str, object_id: str, value: Any) -> None:
    field = session.fetchone("SELECT field_def_id FROM custom_field_def WHERE code = %s AND is_active", (code,))
    if field is None:
        return
    session.execute(
        """
        INSERT INTO custom_field_value (value_id, field_def_id, object_type, object_id, value, created_by, updated_by)
        VALUES (%(id)s, %(def)s, %(type)s, %(obj)s, %(val)s, %(actor)s, %(actor)s)
        ON CONFLICT (field_def_id, object_type, object_id)
        DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now(),
                      version_no = custom_field_value.version_no + 1
        """,
        {"id": _new_id("CFV"), "def": field[0], "type": OBJECT_TYPE, "obj": object_id,
         "val": Jsonb(value), "actor": actor})


def decide_not_released(session: Session, *, budget_id: str, actor: str, target: str,
                        note: str | None, approval_instance_id: str,
                        correlation_id: str | None = None) -> dict[str, Any]:
    """REJECTED, RETURNED (editable again) or CANCELLED via the engine."""
    if target not in (STATUS_REJECTED, STATUS_RETURNED, STATUS_CANCELLED, STATUS_DRAFT):
        _err("INVALID_TARGET", f"Cannot move an original budget to {target}.", status=400)
    doc = _load(session, budget_id, for_update=True)
    if doc["status"] == target:
        return doc
    if doc["status"] != STATUS_SUBMITTED:
        _err("BUDGET_NOT_SUBMITTED", f"{doc['budget_number']} is {doc['status']}.", status=409)
    status = STATUS_RETURNED if target == STATUS_DRAFT else target
    session.execute(
        """
        UPDATE original_budget
           SET status = %(status)s, decision_note = COALESCE(%(note)s, decision_note),
               decided_at = now(), approval_instance_id = %(inst)s,
               updated_at = now(), updated_by = %(actor)s, version_no = version_no + 1
         WHERE budget_id = %(id)s
        """, {"status": status, "note": note, "inst": approval_instance_id, "actor": actor, "id": budget_id})
    audit_mod.append(session, actor, f"ORIGINAL_BUDGET_{status}", OBJECT_TYPE, budget_id,
                     f"{doc['budget_number']} {status} by {actor} under {approval_instance_id}"
                     + (f": {note}" if note else ""), correlation_id=correlation_id)
    return _load(session, budget_id)


def audit_history(session: Session, *, budget_id: str) -> list[dict[str, Any]]:
    _load(session, budget_id)                          # 404 before any audit read
    rows = session.fetchall(  # scope-exempt: the parent was scope-checked one line above
        "SELECT seq, at, actor, action, detail, entry_hash FROM audit_log "
        "WHERE stream_key = %s ORDER BY seq", (f"{OBJECT_TYPE}:{budget_id}",))
    return [{"seq": r[0], "at": _iso(r[1]), "actor": r[2], "action": r[3], "detail": r[4],
             "entry_hash": r[5]} for r in rows]


# ============================================================================
# Upload: governed CSV template, preview, all-or-nothing commit
# ============================================================================
TEMPLATE_COLUMNS = ("wbs_code", "budget_head_code", "category_code", "amount_rupees",
                    "justification", "division_id", "branch_id", "zone_id", "plant_id", "location_id")


def import_template_csv(session: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    defs = budget_custom_field_defs(session)
    cols = list(TEMPLATE_COLUMNS) + [f"cf_{d['code']}" for d in defs]
    w.writerow(cols)
    w.writerow(["CAPEX-2026-001.01", "BH-DM1-PM", "CAT-PLANT-MACHINERY", "1250000.00",
                "Example line -- delete before use"] + [""] * (len(cols) - 5))
    return buf.getvalue()


def _resolve_import_rows(session: Session, *, project: Mapping[str, Any], text: str
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """CSV text -> candidate lines with codes resolved to ids. Returns
    (lines, problems); problems carry row and column."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        _err("IMPORT_EMPTY", "The file has no header row.", status=422)
    missing = [c for c in ("wbs_code", "budget_head_code", "category_code", "amount_rupees")
               if c not in reader.fieldnames]
    if missing:
        _err("IMPORT_HEADER_INVALID", f"Missing column(s): {', '.join(missing)}.", status=422,
             detail={"missing_columns": missing})
    wbs_by_code = {r[1]: r[0] for r in session.fetchall(
        "SELECT wbs_id, wbs_code FROM wbs_element WHERE project_id = %s", (project["project_id"],))}
    head_by_code = {r[1]: r[0] for r in session.fetchall(
        "SELECT budget_head_id, code FROM budget_head WHERE active AND (entity_id IS NULL OR entity_id = %s)",
        (project["entity_id"],))}
    cat_by_code = {r[1]: r[0] for r in session.fetchall(
        "SELECT category_id, code FROM budget_category WHERE active AND (entity_id IS NULL OR entity_id = %s)",
        (project["entity_id"],))}
    lines, problems = [], []
    seen: set[tuple[str, str]] = set()
    for n, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        wbs_code = (row.get("wbs_code") or "").strip()
        head_code = (row.get("budget_head_code") or "").strip()
        cat_code = (row.get("category_code") or "").strip().upper()
        line: dict[str, Any] = {"justification": (row.get("justification") or "").strip() or None}
        if wbs_code not in wbs_by_code:
            problems.append({"row": n, "column": "wbs_code", "code": "WBS_NOT_IN_PROJECT",
                             "message": f"{wbs_code!r} is not a WBS element of this project."})
        else:
            line["wbs_id"] = wbs_by_code[wbs_code]
        if head_code not in head_by_code:
            problems.append({"row": n, "column": "budget_head_code", "code": "BUDGET_HEAD_NOT_FOUND",
                             "message": f"{head_code!r} is not an active budget head for this entity."})
        else:
            line["budget_head_id"] = head_by_code[head_code]
        if cat_code not in cat_by_code:
            problems.append({"row": n, "column": "category_code", "code": "CATEGORY_NOT_FOUND",
                             "message": f"{cat_code!r} is not an active budget category for this entity."})
        else:
            line["budget_category_id"] = cat_by_code[cat_code]
        amt = (row.get("amount_rupees") or "").strip()
        try:
            line["amount_paise"] = money.to_paise(amt, field="amount_rupees")
            if line["amount_paise"] <= 0:
                raise money.MoneyError("must be positive")
        except money.MoneyError as exc:
            problems.append({"row": n, "column": "amount_rupees", "code": "INVALID_AMOUNT",
                             "message": f"{amt!r}: {exc}"})
        for kind in ("division", "branch", "zone", "plant", "location"):
            v = (row.get(f"{kind}_id") or "").strip()
            line[f"{kind}_id"] = v or None
        cf = {k[3:]: v for k, v in row.items() if k and k.startswith("cf_") and (v or "").strip()}
        line["custom_fields"] = cf
        key = (line.get("wbs_id"), line.get("budget_head_id"))
        if all(key) and key in seen:
            problems.append({"row": n, "column": "wbs_code", "code": "DUPLICATE_CELL_IN_FILE",
                             "message": f"{wbs_code} / {head_code} appears on an earlier row."})
        if all(key):
            seen.add(key)
        line["source_row"] = n
        lines.append(line)
    if not lines:
        _err("IMPORT_EMPTY", "The file carries no data rows.", status=422)
    return lines, problems


def import_preview(session: Session, *, project_id: str, text: str) -> dict[str, Any]:
    project = _project_in_scope(session, project_id)
    lines, problems = _resolve_import_rows(session, project=project, text=text)
    if len(lines) > IMPORT_SYNC_ROW_LIMIT:
        _err("IMPORT_TOO_LARGE",
             f"{len(lines)} rows exceeds the synchronous limit of {IMPORT_SYNC_ROW_LIMIT}; split the "
             f"file or use the job endpoint.", status=413,
             detail={"rows": len(lines), "limit": IMPORT_SYNC_ROW_LIMIT})
    validated: list[dict[str, Any]] = []
    if not problems:
        defs = budget_custom_field_defs(session)
        try:
            validated = _validate_lines(session, project=project, lines=lines, defs=defs)
        except OriginalBudgetError as exc:
            if exc.code != "BUDGET_LINES_INVALID":
                raise
            for p in exc.detail or []:
                src = lines[p["line"] - 1]["source_row"] if 0 < p["line"] <= len(lines) else None
                problems.append({"row": src, "column": p["field"], "code": p["code"], "message": p["message"]})
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"project_id": project_id, "rows": len(lines), "problems": problems,
            "valid": not problems, "content_sha256": content_sha,
            "total_paise": sum(l["amount_paise"] for l in validated) if validated else None,
            "lines": validated if validated else [
                {k: v for k, v in l.items() if k != "custom_fields" or v} for l in lines]}


def import_commit(session: Session, *, actor: str, project_id: str, fiscal_year: str, title: str,
                  text: str, period_id: str | None = None, justification: str | None = None,
                  custom_fields: Mapping[str, Any] | None = None,
                  correlation_id: str | None = None) -> dict[str, Any]:
    """All-or-nothing: a single problem refuses the whole file and creates
    nothing. The result is one DRAFT original budget, which then follows the
    same submit/approve path as a hand-entered one -- an import is a way of
    typing, not a way around the controls."""
    preview = import_preview(session, project_id=project_id, text=text)
    if not preview["valid"]:
        _err("IMPORT_INVALID", f"{len(preview['problems'])} problem(s); nothing was imported.",
             status=422, detail=preview["problems"])
    doc = create_draft(session, actor=actor, project_id=project_id, fiscal_year=fiscal_year, title=title,
                       lines=preview["lines"], period_id=period_id, justification=justification,
                       custom_fields=custom_fields, correlation_id=correlation_id)
    audit_mod.append(session, actor, "ORIGINAL_BUDGET_IMPORT", OBJECT_TYPE, doc["budget_id"],
                     f"imported {preview['rows']} row(s), content sha256 {preview['content_sha256']}, "
                     f"total {doc['total_paise']} paise", correlation_id=correlation_id)
    doc["import"] = {"rows": preview["rows"], "content_sha256": preview["content_sha256"]}
    return doc


# ============================================================================
# Reporting helpers the reporting stream can call
# ============================================================================
def category_of_cell(session: Session, wbs_id: str, budget_head_id: str) -> str | None:
    row = session.fetchone(
        "SELECT budget_category_id FROM budget_control_cell WHERE wbs_id = %s AND budget_head_id = %s",
        (wbs_id, budget_head_id))
    return None if row is None else row[0]
