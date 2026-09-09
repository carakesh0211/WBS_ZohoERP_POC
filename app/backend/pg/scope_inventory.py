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
``migrations/pg/001..018``'s **CREATE TABLE** statements -- the schema, not the
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
#:
#: **THE THIRD VALUE IS ALSO A HOLE, AND IT HAS BEEN EXPLOITED ONCE.** Because
#: it belongs to neither :func:`covered_tables` nor :func:`gap_tables`, a table
#: parked here is checked by NOTHING in ``tests/test_pg_rls_coverage.py``: the
#: covered-vs-migration sweep skips it, the still-a-gap sweep skips it, and the
#: ``set(covered_tables()) == set(rls.ALL_RLS_TABLES)`` equality holds over it
#: vacuously from both sides. ``export_job`` and ``export_job_chunk`` sat here
#: for a whole wave with no handoff test, and deleting 018's ``FORCE ROW LEVEL
#: SECURITY`` failed a single string grep. Two guards now make the status
#: cost something rather than hide something --
#: ``test_every_pending_registry_table_is_actually_protected_by_the_migration_it_names``
#: (the migration must really do all three things) and
#: ``test_every_pending_registry_table_is_named_by_a_handoff_test`` (the
#: outstanding handoff must be enumerated by a test, as 008's and 010's are).
#: Parking a table here is now a declaration with an owner, not a quiet exit.
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


#: Every table in `migrations/pg/001..018` whose rows carry or reach a scope
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

    # -------------------------------------------- 010_integration.sql
    # Written from 010's CREATE TABLE statements, before reading its policies
    # -- the maintenance rule in this module's docstring. All eight ARE
    # protected by 010; none is yet in `rls.py`'s registry, which is
    # lead-owned. Hence `protected_pending_registry`, exactly as 008's are.
    #
    # `integration_circuit` is 010's one table beyond seam C2's frozen seven;
    # 010's own header declares it and says why. It is inventoried here on the
    # same terms as the rest, because a table's need for RLS does not depend
    # on which document named it first.
    ScopedTable(
        table="integration_connection", dimensions=("entity",), reach="direct",
        path="integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="The row names the tenant's Zoho organisation_id, its data "
             "centre and its connector -- which is what tells an out-of-scope "
             "caller WHERE another entity's financial data lives. It also "
             "carries `mode`, so an unscoped read would enumerate which "
             "entities have a LIVE connection."),
    ScopedTable(
        table="reconciliation_exception", dimensions=("entity", "project"),
        reach="direct",
        path="reconciliation_exception.entity_id / .project_id",
        status="protected_pending_registry",
        migration="011_reconciliation_exception.sql",
        note="Both dimensions are columns on the row, so the reach is direct "
             "rather than joined. It is scoped because an exception NAMES a "
             "discrepancy -- which purchase order, which receive line, and "
             "the two paise figures that disagree -- so an unscoped read "
             "hands one entity a description of another's commitments and "
             "the exact amount by which their books do not tie out. The "
             "dimensions are nullable on purpose (an unsanctioned commitment "
             "can be discovered on a PO we hold no local record of), and a "
             "NULL dimension is unrestricted-by-that-dimension, which is "
             "correct here: an exception nobody can attribute must stay "
             "visible to whoever can resolve it."),
    ScopedTable(
        table="integration_inbox", dimensions=("entity",), reach="joined",
        path="integration_inbox.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="The one where this matters most and is least obvious: `payload` "
             "holds another entity's supplier invoices. Leaving it bare "
             "because it looks like integration plumbing would put every "
             "entity's purchase ledger behind a table nobody thought of as "
             "financial."),
    ScopedTable(
        table="integration_outbox", dimensions=("entity",), reach="joined",
        path="integration_outbox.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="`payload` is a purchase order about to be created in the "
             "tenant, amounts included, and `dedupe_key` is the value written "
             "into Zoho's unique custom field."),
    ScopedTable(
        table="job", dimensions=("entity", "project"), reach="direct",
        path="job.entity_id, job.project_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="Scoped DIRECTLY rather than through a connection, and that is "
             "necessary rather than convenient: `job.connection_id` is "
             "nullable, so scoping through it would scope by a column that is "
             "NULL on exactly the estate-wide jobs. Both dimension columns "
             "are nullable; a NULL waives that dimension, which is deliberate "
             "for `verify_audit_chains` and `sweep_control_totals` -- their "
             "rows carry a kind, a checkpoint cursor and an error string "
             "rather than any entity's data. plant/location are waived: no "
             "column for either, and project carries its own policy."),
    ScopedTable(
        table="integration_watermark", dimensions=("entity",), reach="joined",
        path="integration_watermark.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="Low-sensitivity content, but writable: an unscoped UPDATE of "
             "another entity's hwm would silently skip or re-pull its "
             "inbound feed, which is why the policy carries WITH CHECK as "
             "well as USING."),
    ScopedTable(
        table="integration_rate_budget", dimensions=("entity",), reach="joined",
        path="integration_rate_budget.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="Same shape and the same reason as the watermark: spending "
             "another entity's daily budget is a denial of service on its "
             "connector that leaves no trace anywhere else."),
    ScopedTable(
        table="integration_circuit", dimensions=("entity",), reach="joined",
        path="integration_circuit.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="Beyond seam C2's seven; see 010's header. Writable state that "
             "can stop another entity's connector outright."),
    ScopedTable(
        table="integration_event", dimensions=("entity",), reach="joined",
        path="integration_event.connection_id -> "
             "integration_connection.entity_id",
        status="protected_pending_registry",
        migration="010_integration.sql",
        note="`connection_id` is NULLABLE -- an event may be raised before a "
             "connection is resolved, and an estate-wide job's events belong "
             "to no connection -- so a NULL waives the dimension and the row "
             "is visible to any established principal. That waiver is real, "
             "and what keeps `detail` from becoming an unscoped copy of "
             "somebody's vendor record is the separate "
             "`ck_integration_event_detail_carries_no_restricted_key` CHECK. "
             "The two controls are complementary; neither substitutes for the "
             "other."),

    # ------------------------------------------- 013_procurement.sql
    # Written from 013's CREATE TABLE statements, before reading its policies
    # -- the maintenance rule in this module's docstring. All eight are
    # `covered`, not `protected_pending_registry`, because 013's stream owns
    # BOTH halves and lands them in one commit: the policies in the migration
    # and `rls.RLS_PROCUREMENT_TABLE_COLUMNS` in `rls.py`. 008's and 010's
    # tables sit in the in-between state precisely because their registry half
    # was somebody else's to write; there is no such handoff here.
    #
    # EVERY ONE OF THESE REACHES ALL FOUR DIMENSIONS through `project`, which
    # is stronger than the project-only waiver `wbs_element` and the two cell
    # tables take. The reason is in 013's header and is worth repeating where
    # someone reviewing scope will read it: dimensions resolve independently,
    # so a principal restricted to one ENTITY and to no project carries
    # `project_ids=None` -- unrestricted -- and a project-only predicate would
    # hand that principal every other entity's purchase orders. For a WBS
    # element the primary control covers it; for the commitments and the money
    # the backstop has to hold on its own.
    ScopedTable(
        table="purchase_request",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="purchase_request.project_id -> project.{entity_id, plant_id, "
             "location_id, project_id}",
        status="covered", migration="013_procurement.sql",
        note="A request names a control cell and a sum before anyone has "
             "approved either. Unfiltered it tells a caller restricted to one "
             "entity what every other entity is about to spend."),
    ScopedTable(
        table="pr_line",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="pr_line.project_id -> project.{entity_id, plant_id, "
             "location_id, project_id}",
        status="covered", migration="013_procurement.sql",
        note="`project_id` is DENORMALISED and is not an independent claim: "
             "`fk_pr_line_pr_project` forces it to equal the header's, so "
             "reaching `project` through it reaches the parent's project by "
             "construction. One join, FK-guaranteed -- unlike "
             "budget_version_cell, which needs both its paths precisely "
             "because 003 declares no FK on its wbs_id."),
    ScopedTable(
        table="purchase_order",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="purchase_order.project_id -> project.{entity_id, plant_id, "
             "location_id, project_id}",
        status="covered", migration="013_procurement.sql",
        note="The committed value. `vendor_name`, `external_id` and the "
             "amendment history together describe who another entity buys "
             "from and on what terms."),
    ScopedTable(
        table="po_line",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="po_line.project_id -> project.{entity_id, plant_id, "
             "location_id, project_id}",
        status="covered", migration="013_procurement.sql",
        note="As pr_line: `fk_po_line_po_project` binds the denormalised "
             "project to the order's. This is the (wbs_id, budget_head_id) "
             "grain the whole ordered/received/billed reconciliation is "
             "computed at, so an unfiltered read is another entity's budget "
             "consumption cell by cell."),
    ScopedTable(
        table="grn",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="grn.po_id -> purchase_order.project_id -> project.{entity_id, "
             "plant_id, location_id, project_id}",
        status="covered", migration="013_procurement.sql",
        note="Carries no project of its own; `po_id` is NOT NULL, so the reach "
             "through the purchase order always resolves and there is no NULL "
             "waiver to get wrong here."),
    ScopedTable(
        table="grn_line",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="grn_line.po_id -> purchase_order.project_id -> project.{...}",
        status="covered", migration="013_procurement.sql",
        note="Reached through the DENORMALISED `po_id` rather than through "
             "`grn_id`, and the two are the same PO by constraint: "
             "`fk_grn_line_grn_po` ties this column to the parent GRN's. One "
             "join instead of two, with nothing waived to buy it."),
    ScopedTable(
        table="bill",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="bill.project_id -> project.{entity_id, plant_id, location_id, "
             "project_id}",
        status="covered", migration="013_procurement.sql",
        note="`project_id` is a column 013 ADDS and the frozen contract does "
             "not list -- recorded there as CONTRACT GAP 4. The POC's only "
             "dimension-bearing column was `po_id`, which is NULLABLE because "
             "non-PO bills exist, and both readings of that NULL are the two "
             "halves of the defect 012 closed: waive it and every non-PO bill "
             "is world-readable, refuse it and every non-PO bill is invisible "
             "to everyone. `fk_bill_po_project` keeps the new column from "
             "drifting from the purchase order whenever one is named."),
    ScopedTable(
        table="bill_line",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="bill_line.bill_id -> bill.project_id -> project.{...} AND "
             "bill_line.wbs_id -> wbs_element.project_id -> project.{...}",
        status="covered", migration="013_procurement.sql",
        note="BOTH paths required -- the budget_version_cell shape, for the "
             "same reason. Nothing ties the line's wbs_id to its bill's "
             "project when `po_line_id` is NULL, because a composite FK is not "
             "checked at all while one of its columns is NULL, so the bill "
             "path alone would let a non-PO line ride in on its bill's "
             "visibility while posting to another project's control cell."),

    # -------------------------- 014_procurement_corrections.sql -----------
    # Written from 014's four CREATE TABLE statements, before reading its
    # policies -- the maintenance rule in this module's docstring.
    ScopedTable(
        table="pr_reservation",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="pr_reservation.project_id -> project.{entity_id, plant_id, "
             "location_id, project_id}",
        status="covered", migration="014_procurement_corrections.sql",
        note="A reservation is HELD BUDGET -- the `pr_reserved` limb of "
             "exposure, which `check_availability` subtracts. `project_id` is "
             "denormalised and is not an independent claim: "
             "`fk_pr_reservation_pr_project` forces it to equal the purchase "
             "request's, so reaching `project` through it reaches the "
             "request's project by construction. All four dimensions are "
             "passed, not project alone, for the reason 013's header gives at "
             "length: a principal restricted to one entity and to no project "
             "carries project_ids=None, and a project-only predicate would "
             "hand them every other entity's held budget. "
             "015_reservation_grain.sql changes the GRAIN of this table -- one "
             "live hold per (request x RESOLVED control cell) rather than one "
             "per request -- and adds no table, no policy and no dimension "
             "column, so the row above is unchanged. The reach is unchanged "
             "too, and that is checked rather than assumed: 015 moves `wbs_id` "
             "from the line's own cell to the budget-owning ANCESTOR, and an "
             "ancestor in ANOTHER project would put the denormalised "
             "`project_id` at odds with `wbs_id` and open a scope hole. It "
             "cannot: `fk_wbs_parent_same_project` (002) makes a child's "
             "project_id identical to its parent's for the whole chain, and "
             "`fk_pr_reservation_wbs_project` (014) binds this row's "
             "(wbs_id, project_id) pair to a real wbs_element -- so the "
             "resolved ancestor is in the request's project by construction."),
    ScopedTable(
        table="lifecycle_state", dimensions=(), reach="reference",
        path="no dimension column and no join to one -- organisation-wide",
        status="covered", migration="014_procurement_corrections.sql",
        note="AUD-C-008's rule table: which project/WBS states permit "
             "procurement and posting. The answer is the same in every "
             "entity, so there is no dimension for a predicate to filter on. "
             "Policy admits any session with an established principal and "
             "denies a session with no scope applied at all -- 006's "
             "`capex_principal_present()`, reused rather than reinvented. "
             "READ-ONLY to capex_app: a rule table the running application "
             "can rewrite is code with extra steps."),
    ScopedTable(
        table="procurement_policy", dimensions=(), reach="reference",
        path="no dimension column and no join to one -- organisation-wide",
        status="covered", migration="014_procurement_corrections.sql",
        note="One row today: FRACTIONAL_PO_QUANTITY, default REFUSE. A "
             "deployment-wide business choice, not per-entity configuration, "
             "so it carries no dimension. capex_app may UPDATE it (that is "
             "what makes it configuration) but never DELETE it -- a policy "
             "that stops existing silently reverts every caller to whatever "
             "default the code carries."),
    ScopedTable(
        table="procurement_transition", dimensions=(), reach="reference",
        path="no dimension column and no join to one -- organisation-wide",
        status="covered", migration="014_procurement_corrections.sql",
        note="Plan section 12's PR and PO state machines as data. A legal "
             "state change is legal estate-wide. READ-ONLY to capex_app, as "
             "lifecycle_state."),

    # ------------------------------------------------ 017_reporting.sql
    # Read from 017's CREATE TABLE statements, before looking at its policies
    # -- the maintenance rule in this module's docstring. `report_saved_view`
    # has a literal `entity_id text NOT NULL REFERENCES entity`, so it carries
    # a dimension whether or not anything protects it; `report_view_default`
    # has no dimension column at all and one foreign key that reaches one.
    ScopedTable(
        table="report_saved_view", dimensions=("entity",), reach="direct",
        path="report_saved_view.entity_id", status="covered",
        migration="017_reporting.sql",
        note="A saved view stores a QUESTION -- a FilterSet and a grouping -- "
             "and never an answer, so opening one runs under the OPENER'S "
             "scope and cannot serve the author's rows. What it can still "
             "leak is the filter itself: a project or vendor id the reader "
             "holds no grant for, sitting in `definition`. That is why this "
             "is scoped data and not a user preference. NOTE that the entity "
             "predicate is only half the control -- a colleague in the same "
             "entity passes it and must still not read a PRIVATE view; 017's "
             "policy carries the visibility/owner disjunction in the same "
             "expression, and its WITH CHECK is narrower than its USING so a "
             "shared view cannot be edited under everyone who uses it."),
    ScopedTable(
        table="report_view_default", dimensions=(), reach="joined",
        path="report_view_default.view_id -> report_saved_view.entity_id",
        status="covered", migration="017_reporting.sql",
        note="One default per (user, report); the primary key IS that rule. "
             "Carries no dimension column, so its policy is an EXISTS against "
             "`report_saved_view` -- itself under RLS -- and NOT an all-NULL "
             "`capex_scope_permits` call, which would be literally TRUE for "
             "every row. A default pointing at a view the principal may not "
             "read is therefore absent rather than an error naming a view id "
             "they were not entitled to learn exists."),

    # ------------------------------------------------ 018_export_jobs.sql
    # Wave 7 stream A2. Both entries were `protected_pending_registry` because
    # `app.backend.pg.rls`'s registry was lead-owned and not edited by that
    # stream. THE HANDOFF WAS NEVER MADE, AND NOTHING COULD SEE THAT: unlike
    # 008's and 010's, 018's pending pair had no `pending_registry_tables()`
    # handoff test, and the status itself removes a table from BOTH sides of
    # `test_pg_rls_coverage.py`'s `set(covered_tables()) == set(ALL_RLS_TABLES)`
    # -- so the equality held over them vacuously for the whole wave. Deleting
    # `ALTER TABLE export_job FORCE ROW LEVEL SECURITY` from 018 failed one
    # string grep in `tests/test_pg_exports.py` and nothing else.
    #
    # Both are now `covered`: `rls.RLS_EXPORT_TABLE_COLUMNS` names them and
    # `rls.RLS_MIGRATION_BY_TABLE` attributes them to 018, in the same commit
    # as this reclassification, exactly as the Status docstring requires.
    ScopedTable(
        table="export_job", dimensions=(), reach="reference",
        path="no dimension column -- the row's authorisation is its REQUESTER, "
             "and its scope_json is a SET of ids across all four dimensions, "
             "which is not a value a per-row predicate can filter on",
        status="covered",
        migration="018_export_jobs.sql",
        note="Classified `reference` for the shape of its predicate, NOT "
             "because it is organisation-wide data -- it is the opposite of "
             "organisation-wide. `lifecycle_state` admits any established "
             "principal; `export_job_owner` admits ONE, the requester named on "
             "the row, and `export_job_service` admits the SVC-EXPORT worker "
             "so it can claim and advance jobs. That is a narrower line than "
             "any dimension predicate in this file draws, not a waiver of one. "
             "It has to be: the row carries the requester's whole resolved "
             "scope, and a rendered export of another entity's financial data "
             "hangs off it."),
    ScopedTable(
        table="export_job_chunk", dimensions=(), reach="joined",
        path="export_job_chunk.export_job_id -> export_job, whose own two "
             "policies decide",
        status="covered",
        migration="018_export_jobs.sql",
        note="The rendered bytes. Visible to exactly whoever the parent job is "
             "visible to, expressed as an EXISTS over `export_job` so there is "
             "one definition of 'may see this export' rather than two that can "
             "drift. Append-only by trigger AND by privilege (capex_app holds "
             "SELECT/INSERT/DELETE, never UPDATE); DELETE is granted on purpose "
             "-- expiry purges the bytes while the job row recording who "
             "exported what, under which scope, stays."),

    # ---------------------------------------------------- 019_closure.sql
    # The 013 shape, restated for the three closure documents: a denormalised
    # `project_id` bound by FK to a real project, reached through `project` so
    # that ALL FOUR dimensions are filtered. Filtering the column directly
    # would waive entity, plant and location -- and a principal restricted to
    # one entity but to no project carries `project_ids = NULL`, so such a
    # predicate is TRUE for every project in the estate.
    ScopedTable(
        table="project_completion_review",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="project_completion_review.project_id -> project.entity_id / "
             "plant_id / location_id / project_id",
        status="covered", migration="019_closure.sql",
        note="ONE live review per project (ux_project_completion_review_live, "
             "partial on Draft/Submitted), so a project cannot accumulate "
             "competing completion assertions."),
    ScopedTable(
        table="capitalisation_request",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="capitalisation_request.project_id -> project.entity_id / "
             "plant_id / location_id / project_id",
        status="covered", migration="019_closure.sql",
        note="Carries cwip_balance_paise and allocated_paise -- an unscoped "
             "read is another entity's capital position. `review_id` is bound "
             "to the SAME project by fk_capitalisation_request_review_project, "
             "so a request cannot cite another project's completion review."),
    ScopedTable(
        table="asset_allocation",
        dimensions=("entity", "plant", "location", "project"), reach="joined",
        path="asset_allocation.project_id -> project.entity_id / plant_id / "
             "location_id / project_id",
        status="covered", migration="019_closure.sql",
        note="`wbs_id` is bound to the same project by "
             "fk_asset_allocation_wbs_project against wbs_element "
             "(wbs_id, project_id); without it an allocation could name "
             "another project's element and the project_id-based policy would "
             "still admit the row."),
)

#: Tables deliberately left WITHOUT a scope policy, each with the reason.
#: Kept here so "not in SCOPED_TABLES" is a decision on record rather than an
#: omission nobody ever looked at. Reviewed against `001..018`'s full
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
