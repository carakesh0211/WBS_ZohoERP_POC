"""The independent inventory of which tables must carry row-level security.

**Why this file exists, and why it is not generated.**

`rls.RLS_TABLE_COLUMNS` is *derived from the migration*: it was written by
reading `migrations/pg/004_identity_scope.sql` and transcribing what that file
does. Every test built on it therefore checks the migration against a
restatement of itself. That catches drift -- a policy dropped from one side but
not the other -- and cannot, even in principle, catch the failure that actually
happened: a table that is absent from the migration **and** absent from the
registry. Both sides agree, both sides are wrong, every test passes.
`accounting_period`, `budget_line`, `budget_revision`, `budget_transfer`,
`budget_version`, `budget_version_cell`, `item_master` and `vendor_master` each
sat in exactly that hole from Wave 2 until Wave 3.

This module is the second, *independent* source: written from
``migrations/pg/001..005``'s **CREATE TABLE** statements -- the schema, not the
policies -- asking of each table only "does a row of this carry, or reach, a
scope dimension?". A table that must be protected appears here whether or not
any migration protects it, which is what makes "protected nowhere" detectable.

**Maintenance rule.** When a migration adds a table, add it here by hand, from
the ``CREATE TABLE``, before looking at any policy. Never populate this file by
parsing a migration, importing :mod:`app.backend.pg.rls`, or copying from
either -- that would collapse the two sources back into one and delete the only
property this file has.

`tests/test_pg_rls_coverage.py` cross-checks this inventory against both the
migration text and `rls.py`'s registry, and proves the cross-check actually
detects an uncovered table rather than merely passing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How a table's rows reach the four scope dimensions.
#:
#: ``"direct"``      the table has a column for at least one dimension.
#: ``"joined"``      no dimension column; reached through a foreign key.
#: ``"reference"``   organisation-wide data with no dimension at all; the
#:                   only line RLS can draw is "a principal is established".
Reach = Literal["direct", "joined", "reference"]

#: Whether RLS is expected to be in place today.
#:
#: ``"covered"``  a migration listed in :attr:`ScopedTable.migration` enables,
#:                forces and policies the table. The coverage test asserts it.
#: ``"gap"``      the table needs RLS, nothing provides it yet, and the owner
#:                named in :attr:`ScopedTable.note` has not yet closed it. The
#:                coverage test asserts the gap is still exactly where this
#:                file says it is, so closing it forces this entry to be
#:                reclassified rather than silently rotting into a lie.
Status = Literal["covered", "gap"]


@dataclass(frozen=True)
class ScopedTable:
    """One table that must carry RLS, and how its rows reach a dimension."""

    table: str
    #: Dimension names (``entity`` / ``plant`` / ``location`` / ``project``)
    #: the table's RLS predicate actually filters on. Empty for ``reference``.
    dimensions: tuple[str, ...]
    reach: Reach
    #: Human-readable path from a row to the dimension values above.
    path: str
    status: Status
    #: The migration file that provides the coverage, or ``None`` for a gap.
    migration: str | None
    note: str = ""


#: Every table in `migrations/pg/001..005` whose rows carry or reach a scope
#: dimension. Hand-maintained from the CREATE TABLE statements -- see the
#: module docstring. Ordered by migration, then by table name.
SCOPED_TABLES: tuple[ScopedTable, ...] = (
    # ------------------------------------------------ 001_foundation.sql
    ScopedTable(
        table="entity", dimensions=("entity",), reach="direct",
        path="entity.entity_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="division", dimensions=("entity",), reach="direct",
        path="division.entity_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="branch", dimensions=("entity",), reach="direct",
        path="branch.entity_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="zone", dimensions=("entity",), reach="direct",
        path="zone.entity_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="department", dimensions=("entity",), reach="direct",
        path="department.entity_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="plant", dimensions=("entity", "plant"), reach="direct",
        path="plant.entity_id, plant.plant_id", status="covered",
        migration="004_identity_scope.sql"),
    ScopedTable(
        table="location", dimensions=("entity", "plant", "location"),
        reach="direct",
        path="location.entity_id, location.plant_id, location.location_id",
        status="covered", migration="004_identity_scope.sql"),
    ScopedTable(
        table="accounting_period", dimensions=("entity",), reach="direct",
        path="accounting_period.entity_id", status="covered",
        migration="006_rls_coverage.sql",
        note="Period state is an entity-level control; unfiltered it disclosed "
             "every other entity's open/closed books."),

    # -------------------------------------------- 002_budget_control.sql
    ScopedTable(
        table="project",
        dimensions=("entity", "plant", "location", "project"), reach="direct",
        path="project.entity_id, plant_id, location_id, project_id",
        status="covered", migration="004_identity_scope.sql"),
    ScopedTable(
        table="wbs_element", dimensions=("project",), reach="direct",
        path="wbs_element.project_id", status="covered",
        migration="004_identity_scope.sql",
        note="entity/plant/location waived deliberately -- reachable only "
             "through project, which carries its own policy."),
    ScopedTable(
        table="budget_control_cell", dimensions=("project",), reach="joined",
        path="budget_control_cell.wbs_id -> wbs_element.project_id",
        status="covered", migration="004_identity_scope.sql"),
    ScopedTable(
        table="budget_ledger_cell", dimensions=("project",), reach="joined",
        path="budget_ledger_cell.wbs_id -> wbs_element.project_id",
        status="covered", migration="004_identity_scope.sql"),
    ScopedTable(
        table="budget_head", dimensions=("entity",), reach="direct",
        path="budget_head.entity_id", status="gap", migration=None,
        note="REPORTED GAP (Wave 3 stream 2). budget_head carries "
             "`entity_id NOT NULL REFERENCES entity` and has no policy in 004 "
             "or 006. It is NOT in docs/WAVE3_CONTRACTS.md's eight-table "
             "closure list, so stream 2 reports it rather than widening its "
             "own migration past its brief. Closing it is a one-policy change "
             "shaped exactly like accounting_period_scope in 006. Until then "
             "a caller restricted to one entity can enumerate every other "
             "entity's budget heads by reading this table directly."),

    # ------------------------------------------- 003_budget_planning.sql
    ScopedTable(
        table="budget_line", dimensions=("project",), reach="joined",
        path="budget_line.wbs_id -> wbs_element.project_id",
        status="covered", migration="006_rls_coverage.sql"),
    ScopedTable(
        table="budget_revision", dimensions=("project",), reach="joined",
        path="budget_revision.wbs_id -> wbs_element.project_id",
        status="covered", migration="006_rls_coverage.sql"),
    ScopedTable(
        table="budget_transfer", dimensions=("project",), reach="joined",
        path="budget_transfer.from_wbs_id -> wbs_element.project_id AND "
             "budget_transfer.to_wbs_id -> wbs_element.project_id",
        status="covered", migration="006_rls_coverage.sql",
        note="BOTH legs required. OR would disclose the far side of a "
             "cross-project transfer to a caller scoped to one side."),
    ScopedTable(
        table="budget_version", dimensions=("project",), reach="direct",
        path="budget_version.project_id", status="covered",
        migration="006_rls_coverage.sql"),
    ScopedTable(
        table="budget_version_cell", dimensions=("project",), reach="joined",
        path="budget_version_cell.version_id -> budget_version.project_id AND "
             "budget_version_cell.wbs_id -> wbs_element.project_id",
        status="covered", migration="006_rls_coverage.sql",
        note="Both paths required; wbs_id carries no FK in 003, so the "
             "version path alone would admit a foreign wbs_id."),

    # ---------------------------------------------- 005_master_data.sql
    ScopedTable(
        table="item_master", dimensions=(), reach="reference",
        path="no dimension column and no join to one -- organisation-wide",
        status="covered", migration="006_rls_coverage.sql",
        note="Policy admits any session with an established principal and "
             "denies a session with no scope applied at all."),
    ScopedTable(
        table="vendor_master", dimensions=(), reach="reference",
        path="no dimension column and no join to one -- organisation-wide",
        status="covered", migration="006_rls_coverage.sql",
        note="As item_master. gst_no/pan_no are protected by API-boundary "
             "masking plus an audited reveal permission, not by RLS."),
)

#: Tables deliberately left WITHOUT a scope policy, each with the reason.
#: Kept here so "not in SCOPED_TABLES" is a decision on record rather than an
#: omission nobody ever looked at. Reviewed against `001..005`'s full
#: CREATE TABLE list; every table in the schema appears in exactly one of
#: these two structures.
UNSCOPED_TABLES: dict[str, str] = {
    "organisation":
        "Root of the hierarchy. No entity/plant/location/project column of "
        "its own; any predicate would be literally TRUE. 004's header states "
        "this decision.",
    "app_user":
        "Identity, not scoped data. A user is not owned by an entity in this "
        "schema.",
    "audit_log":
        "Polymorphic (object_type/object_id), so no dimension column and no "
        "single join to one. Append-only by trigger AND by privilege "
        "(capex_app holds INSERT/SELECT only). Scoping the audit trail is a "
        "separate design question -- an auditor's read is deliberately wider "
        "than an operator's -- not an oversight of this inventory.",
    "audit_anchor": "Daily Merkle anchors over audit_log; carries no object "
                    "reference at all.",
    "role_grant": "Authorisation input, not scoped data. Who may read it is a "
                  "privilege question, not a row-level-scope one.",
    "user_access_flag": "As role_grant.",
    "user_scope_restriction": "As role_grant -- this table DEFINES scope; it "
                              "cannot be filtered by the scope it defines "
                              "without circularity.",
    "user_scope_grant": "As user_scope_restriction.",
    "numbering_series": "Organisation-wide configuration; no dimension.",
    "numbering_counter": "Child of numbering_series; no dimension.",
    "numbering_issued": "Append-only issuance log; object_type/object_id is "
                        "polymorphic, as audit_log.",
    "custom_field_def": "REQ-SEC-007 field definitions -- organisation-wide "
                        "configuration, no dimension.",
    "custom_field_applicability": "Child of custom_field_def; no dimension.",
    "custom_field_value": "Values against ITEM/VENDOR objects, both of which "
                          "are themselves organisation-wide reference data.",
    "schema_migrations": "The migration runner's own ledger; not application "
                         "data.",
}


def tables_requiring_rls() -> tuple[str, ...]:
    """Every table that must carry RLS, covered or not, sorted."""
    return tuple(sorted(entry.table for entry in SCOPED_TABLES))


def covered_tables() -> tuple[str, ...]:
    """Tables this inventory asserts are RLS-protected today, sorted."""
    return tuple(sorted(
        entry.table for entry in SCOPED_TABLES if entry.status == "covered"))


def gap_tables() -> tuple[str, ...]:
    """Tables that need RLS and do not have it -- reported, not fixed."""
    return tuple(sorted(
        entry.table for entry in SCOPED_TABLES if entry.status == "gap"))


def tables_for_migration(migration: str) -> tuple[str, ...]:
    """Tables this inventory expects `migration` to protect, sorted."""
    return tuple(sorted(
        entry.table for entry in SCOPED_TABLES if entry.migration == migration))


def by_table(table: str) -> ScopedTable:
    for entry in SCOPED_TABLES:
        if entry.table == table:
            return entry
    raise KeyError(f"{table!r} is not in the scope inventory")
