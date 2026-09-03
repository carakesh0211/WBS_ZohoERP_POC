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
``migrations/pg/001..008``'s **CREATE TABLE** statements -- the schema, not the
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
#:                forces and policies the table, AND ``app.backend.pg.rls``
#:                registers it. The coverage test asserts both.
#: ``"gap"``      the table needs RLS, nothing provides it yet, and the owner
#:                named in :attr:`ScopedTable.note` has not yet closed it. The
#:                coverage test asserts the gap is still exactly where this
#:                file says it is, so closing it forces this entry to be
#:                reclassified rather than silently rotting into a lie.
#: ``"protected_pending_registry"``
#:                the migration DOES enable, force and policy the table, but
#:                ``app.backend.pg.rls``'s registry does not yet name it.
#:
#: The third value exists because coverage has two halves that are owned by
#: different people. ``tests/test_pg_rls_coverage.py`` asserts
#: ``set(covered_tables()) == set(rls.ALL_RLS_TABLES)`` -- an equality in both
#: directions -- and ``rls.py`` is lead-owned and frozen for Wave 4, while
#: ``migrations/pg/008_approval_engine.sql`` and this file are stream 1's. A
#: table protected by 008 therefore cannot honestly be called ``"covered"``
#: (the registry half is missing, and claiming otherwise would break that
#: equality) and cannot be called ``"gap"`` either (it IS protected, and
#: ``test_reported_gaps_are_still_gaps`` would rightly fail). Recording the
#: real, in-between state keeps this inventory truthful and makes the
#: outstanding handoff visible instead of hiding it in one of two labels that
#: would each be a lie. :func:`pending_registry_tables` enumerates them for
#: whoever extends ``rls.py``; reclassify to ``"covered"`` in the same commit.
Status = Literal["covered", "gap", "protected_pending_registry"]


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


#: Every table in `migrations/pg/001..008` whose rows carry or reach a scope
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

    # ------------------------------------------ 008_approval_engine.sql
    # Written from 008's CREATE TABLE statements, before reading its
    # policies -- the maintenance rule in this module's docstring. All eight
    # ARE protected by 008; none is yet in `rls.py`'s registry, which is
    # lead-owned and frozen for Wave 4. Hence `protected_pending_registry`;
    # see the Status docstring above for why neither of the other two labels
    # would be true.
    ScopedTable(
        table="approval_definition", dimensions=("entity",), reach="direct",
        path="approval_definition.entity_id",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="entity_id is NULLABLE and a NULL means organisation-wide -- the "
             "dimension is waived for that row, so an org-wide workflow is "
             "visible to every principal while an entity-specific one is "
             "confined. Left bare this would repeat the budget_head defect "
             "below: a caller restricted to one entity could enumerate "
             "another entity's approval workflow, its money thresholds and "
             "its named approvers."),
    ScopedTable(
        table="approval_rule", dimensions=("entity",), reach="joined",
        path="approval_rule.definition_id -> approval_definition.entity_id",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="The routing predicate carries the money thresholds that decide "
             "which approvals a document needs; it is the sensitive half of a "
             "workflow, not the definition header that points at it."),
    ScopedTable(
        table="approval_stage", dimensions=("entity",), reach="joined",
        path="approval_stage.definition_id -> approval_definition.entity_id",
        status="protected_pending_registry",
        migration="008_approval_engine.sql"),
    ScopedTable(
        table="approval_stage_approver", dimensions=("entity",), reach="joined",
        path="approval_stage_approver.stage_id -> approval_stage.definition_id "
             "-> approval_definition.entity_id",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="Two joins out. Names the individuals and roles that approve, "
             "which is exactly what an out-of-scope caller must not enumerate."),
    ScopedTable(
        table="approval_instance", dimensions=("entity", "project"),
        reach="direct",
        path="approval_instance.entity_id, approval_instance.project_id",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="Both columns are DENORMALISED at creation (WAVE4 contract 6) "
             "precisely so this table is scopable without a join: "
             "object_type/object_id are polymorphic and no RLS predicate "
             "could follow them. project_id is nullable; a NULL waives the "
             "project dimension for that row, so such an instance is visible "
             "to any caller whose ENTITY permits it. plant/location are "
             "waived -- no column for either, and project carries its own "
             "policy."),
    ScopedTable(
        table="approval_stage_instance", dimensions=("entity", "project"),
        reach="joined",
        path="approval_stage_instance.instance_id -> approval_instance."
             "{entity_id, project_id}",
        status="protected_pending_registry",
        migration="008_approval_engine.sql"),
    ScopedTable(
        table="approval_assignment", dimensions=("entity", "project"),
        reach="joined",
        path="approval_assignment.stage_instance_id -> "
             "approval_stage_instance.instance_id -> approval_instance."
             "{entity_id, project_id}",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="This is the SCOPE filter only. Contract 6 also requires a "
             "caller to see just the assignments addressed to them unless "
             "they hold approval.configure -- an application-layer rule in "
             "the inbox query, because RLS cannot know what permissions a "
             "principal holds. The two are complementary; neither replaces "
             "the other."),
    ScopedTable(
        table="approval_action", dimensions=("entity", "project"),
        reach="joined",
        path="approval_action.instance_id -> approval_instance."
             "{entity_id, project_id}",
        status="protected_pending_registry",
        migration="008_approval_engine.sql",
        note="Reached through instance_id, NOT stage_instance_id: the latter "
             "is NULL for RECALL and CANCEL, so joining through it would make "
             "exactly those two action kinds invisible."),
)

#: Tables deliberately left WITHOUT a scope policy, each with the reason.
#: Kept here so "not in SCOPED_TABLES" is a decision on record rather than an
#: omission nobody ever looked at. Reviewed against `001..008`'s full
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
    "reason_code": "Organisation-wide configuration -- the controlled "
                   "vocabulary a decision may cite. No entity, plant, "
                   "location or project column and no join to one; a reason "
                   "such as BUDGET_EXCEEDED means the same thing in every "
                   "entity. Same class as numbering_series.",
    "approval_delegation": "Authorisation input, not scoped data -- 'X may "
                           "act for Y' is the same kind of statement as "
                           "role_grant, and is classified the same way. Its "
                           "`scope_key` is an opaque application-level key, "
                           "not an entity/plant/location/project id, so there "
                           "is no dimension for a predicate to filter on. Who "
                           "may read a delegation is a privilege question; "
                           "the lead may wish to revisit that separately, "
                           "because a delegation is more operational than "
                           "role_grant is.",
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


def pending_registry_tables() -> tuple[str, ...]:
    """Tables a migration protects that ``rls.py``'s registry does not name.

    The outstanding half of coverage, made enumerable. Whoever extends
    ``app/backend/pg/rls.py`` (lead-owned) should add exactly these, then
    reclassify each entry here to ``status="covered"`` in the same commit --
    at which point this function returns empty again and
    ``test_rls_registry_and_the_independent_inventory_name_the_same_tables``
    keeps holding.
    """
    return tuple(sorted(
        entry.table for entry in SCOPED_TABLES
        if entry.status == "protected_pending_registry"))


def tables_for_migration(migration: str) -> tuple[str, ...]:
    """Tables this inventory expects `migration` to protect, sorted."""
    return tuple(sorted(
        entry.table for entry in SCOPED_TABLES if entry.migration == migration))


def by_table(table: str) -> ScopedTable:
    for entry in SCOPED_TABLES:
        if entry.table == table:
            return entry
    raise KeyError(f"{table!r} is not in the scope inventory")
