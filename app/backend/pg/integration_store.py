"""The integration platform's repository layer, and the names of its schema.

``migrations/pg/010_integration.sql`` is the only place the integration schema
is defined, and five streams build on it at once. Every one of them needs to
name a table, a column and a state value in Python, and every hand-typed
``"integration_watermark"`` is a typo waiting to become a runtime error nothing
catches until the query runs -- which, for a cron-driven connector, is at 3am
in CI's PostgreSQL job at the earliest. Streams 3-7 import from here instead,
so there is exactly one spelling of every name in the codebase.

**Two things live in this module, deliberately.** The constants are a
transcription of the migration, not a second source of truth: when they and
the migration disagree, the migration is right and this module is broken, and
``tests/test_pg_integration_schema.py`` reads the migration text and fails on
drift in either direction. The functions are the repository -- every scoped
read and write, each going through :func:`app.backend.pg.repo.query` with a
literal ``{scope}`` token and a ``columns=`` mapping that names all four
dimensions, waiving one only by mapping it to ``None`` with a comment saying
why.

**Statuses are transcribed, never invented.** Every state value below comes
from ``research/30_contracts/C16_integration_statuses.json``, which was frozen
on 2026-08-28. That file is stream 3's; this module reads it and does not
extend it. The ``CIRCUIT_`` prefixes in particular are not decoration: the bare
code ``CLOSED`` collides with the C3 business status ``CLOSED`` and the two
mean opposite things.

**Money crosses this module in exactly one place, and it is a discrepancy,
not a balance.** No table in 010 has a ``*_paise`` column: monetary amounts on
an outbound document travel inside ``integration_outbox.payload`` as the
integer paise the DTO already carries, the ledger tables own money and this
module owns transport. ``011_reconciliation_exception.sql`` is the exception --
``local_paise`` and ``source_paise`` are the two sides of a difference nobody
can yet explain. They are ``bigint``, so **every aggregate over them casts
``::bigint``**: PostgreSQL's ``SUM()`` over ``bigint`` returns ``numeric``,
psycopg maps ``numeric`` to ``Decimal``, and a Decimal that reaches arithmetic
expecting an int is the defect ``tests/test_money_sql_discipline.py`` was
written after. Nothing here converts paise to rupees, to a float, or to a
JavaScript number; the only rendering of money in the whole integration
package is ``erp.render_paise``/``books_inventory.render_paise``, on the way
out to the wire.

**No network, no tenant, no product fact.** Nothing here builds a URL, names an
endpoint, or knows what a Zoho response looks like. A connection stores
``product`` and ``dc``; stream 1's adapter turns those into a base URL and this
module never sees it. D-14 is unresolved and that is exactly why.
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import repo
from .engine import Scope, Session

# ============================================================== table names
#: One tenant connection: product, DC, organisation and MODE.
INTEGRATION_CONNECTION = "integration_connection"
#: One received external payload, deduplicated by a UNIQUE constraint.
INTEGRATION_INBOX = "integration_inbox"
#: One outbound document emission and its synthesised dedupe key.
INTEGRATION_OUTBOX = "integration_outbox"
#: One bounded, chunked, checkpointed background job.
JOB = "job"
#: The per-(connection, module) high-water mark and its overlap.
INTEGRATION_WATERMARK = "integration_watermark"
#: The per-minute AND per-day call budgets.
INTEGRATION_RATE_BUDGET = "integration_rate_budget"
#: The per-(connection, module) circuit breaker.
INTEGRATION_CIRCUIT = "integration_circuit"
#: The append-only correlation trail.
INTEGRATION_EVENT = "integration_event"

#: Every table ``010_integration.sql`` creates, in creation order -- which is
#: also an order safe to insert in, and the reverse of a safe delete order
#: (``integration_connection`` first because everything references it).
INTEGRATION_TABLES: tuple[str, ...] = (
    INTEGRATION_CONNECTION,
    INTEGRATION_INBOX,
    INTEGRATION_OUTBOX,
    JOB,
    INTEGRATION_WATERMARK,
    INTEGRATION_RATE_BUDGET,
    INTEGRATION_CIRCUIT,
    INTEGRATION_EVENT,
)

#: The migration that creates all of the above.
INTEGRATION_MIGRATION = "010_integration.sql"

#: The constraint the inbound writer targets BY NAME in ``ON CONFLICT``.
#: Inbound idempotency is this constraint (section 11.6); a generated name would work
#: until PostgreSQL chose a different one.
INBOX_IDEMPOTENCY_CONSTRAINT = "uq_integration_inbox_idempotency"

# ------------------------------------------------- reconciliation (011)
#: The §11.8 exception table. Created by ``011_reconciliation_exception.sql``,
#: NOT by 010 -- which is why it is named separately from
#: :data:`INTEGRATION_TABLES` above. ``pg/periods.py`` and the integration
#: sweeps both reference it, and until 011 landed neither could.
RECONCILIATION_EXCEPTION = "reconciliation_exception"

#: The migration that creates it.
RECONCILIATION_EXCEPTION_MIGRATION = "011_reconciliation_exception.sql"

# ------------------------------------------------- procurement chain (013)
#: The eight procurement documents ``013_procurement.sql`` creates. Named here
#: for the same reason :data:`RECONCILIATION_EXCEPTION` is -- these are NOT
#: 010's tables, they arrive in their own migration, and a module that spells
#: them inline cannot say which migration it depends on -- and as constants
#: rather than literals inside f-strings so that
#: `tests/test_integration_sql_matches_schema.py` can read these names and
#: check every statement below against the migration. A table spelled inline in
#: one query and via a constant in another is exactly the drift that check
#: cannot see.
PURCHASE_REQUEST = "purchase_request"
PR_LINE = "pr_line"
PURCHASE_ORDER = "purchase_order"
PO_LINE = "po_line"
GRN = "grn"
GRN_LINE = "grn_line"
BILL = "bill"
BILL_LINE = "bill_line"

PROCUREMENT_TABLES: tuple[str, ...] = (
    PURCHASE_REQUEST, PR_LINE, PURCHASE_ORDER, PO_LINE,
    GRN, GRN_LINE, BILL, BILL_LINE,
)

#: The migration that creates them.
PROCUREMENT_MIGRATION = "013_procurement.sql"

#: ``ux_grn_line_external_v3`` (016), the index that makes a receive replay
#: free. 014's ``_v2`` is dropped: same columns, but unpartitioned, so it also
#: constrained rows carrying no external identity at all.
#: Named because :func:`record_receive_line` targets it in ``ON CONFLICT`` by
#: its columns, and those columns must keep agreeing with the migration.
#:
#: FOUR COLUMNS SINCE MIGRATION 014, AND `NULLS NOT DISTINCT`. 013's
#: ``ux_grn_line_external`` was a table UNIQUE over the first three under
#: PostgreSQL's DEFAULT NULLS DISTINCT, which meant it did not constrain a
#: receive line with no external line id -- and on Zoho ERP that is the
#: ORDINARY case, because ERP publishes no receives-list endpoint and lines are
#: discovered PO-anchored. The sweeps re-walk by design, so every walk
#: re-inserted and ``received`` climbed with no new receive arriving. ``line_no``
#: joins the key so that two GENUINELY DISTINCT lines on one receive stay
#: distinct rather than being collapsed by the same fix.
GRN_LINE_EXTERNAL_UNIQUE: tuple[str, ...] = (
    "po_line_id", "receive_external_id", "line_external_id", "line_no")

#: The migration that corrects 013: scoped document numbers, bill-line replay
#: identity, canonical status CHECKs, ``external_status_raw``, the NULLS NOT
#: DISTINCT receive-line key, and the one-PO-per-PR index.
PROCUREMENT_CORRECTION_MIGRATION = "014_procurement_corrections.sql"

#: The PO states that RELEASE commitment. Transcribed from
#: ``domain.COMMITMENT_RELEASING_STATES``, which is the frozen `C5_formulas.json`
#: registry in code, and deliberately not re-derived: a fifth state added here
#: and not there would make PostgreSQL and the SQLite ledger disagree about how
#: much money is committed, silently, in the direction that understates.
COMMITMENT_RELEASING_STATES: tuple[str, ...] = ("Cancelled", "Closed")

#: The bill states that MOVE actual CWIP (AUD-C-004). Transcribed from
#: ``domain.ACCOUNTING_EFFECTIVE_BILL_STATES`` for the same reason.
ACCOUNTING_EFFECTIVE_BILL_STATES: tuple[str, ...] = ("Approved", "Reversal")

#: The module's name for "a row a SWEEP wrote, not a person".
#:
#: A literal, and deliberately not the string a human actor would produce: a
#: receive line attached by the PO-anchored walk was authored by no user, and
#: writing a user id into `created_by` would make the audit trail claim a
#: decision nobody took. The same rule the API router applies to `_actor`:
#: server-derived, never inferred.
#:
#: NOT THE VALUE THAT REACHES `grn_line.created_by`. The ingest writer in
#: `pg/procurement.py` stamps `SVC-SWEEP`, which is a SEEDED `app_user` row --
#: an actual service principal the estate can join back to -- and that is the
#: stronger answer to the same question, because a bare marker string names a
#: writer nothing else in the database knows about. This constant remains the
#: module's declaration of the rule; `procurement.record_receive_line`'s
#: `actor` default is where the rule is spent.
SWEEP_ACTOR = "SYSTEM:integration-sweep"

#: The four dimensions, reached through `project`, for every statement over the
#: eight procurement tables.
#:
#: ALL FOUR ARE NAMED, and none is waived. That is not tidiness: it is the only
#: mapping that agrees with what 013's own policies do. Those policies read
#: `EXISTS (SELECT 1 FROM project p WHERE ... capex_scope_permits(p.entity_id,
#: p.plant_id, p.location_id, p.project_id))` -- all four dimensions, through a
#: join -- and `rls.JOINED_VIA_PROJECT` exists to record precisely that these
#: eight are filtered through `project` rather than on a column of their own.
#:
#: Unlike ``reconciliation_exception``, which genuinely has no plant or location
#: column, a procurement document reaches its project and therefore reaches all
#: four. Waiving entity, plant and location here -- which the tables' own
#: columns would seem to invite, since only four of the eight carry
#: `project_id` and none carries the other three -- would leave a principal
#: restricted to one ENTITY unfiltered at the application layer, relying on RLS
#: alone. The repository predicate is meant to be the second, independent
#: enforcement of the same scope, and a waiver would make it the first and only
#: place the restriction is *not* expressed. So every statement joins `project`.
PROCUREMENT_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "p.entity_id", "plant": "p.plant_id",
    "location": "p.location_id", "project": "p.project_id",
}

#: The event kind that records "this bill's line items have been fetched".
#:
#: THE HYDRATION QUEUE IS A LEDGER, NOT A FLAG, and that is forced rather than
#: chosen: ``bill`` carries no ``lines_hydrated`` column, ``integration_inbox``
#: is frozen after insert by 010's append-only trigger, and 013 is the latest
#: migration -- ``tests/test_pg_procurement_schema.py`` asserts that it is, so
#: a 014 adding a flag column is not available to this module. What IS
#: available is 010's append-only correlation trail, indexed on
#: ``(kind, at DESC)`` and already carrying a jsonb ``detail``.
#:
#: Deriving the queue from "has no ``bill_line`` rows" alone was rejected: a
#: bill that genuinely has no line items -- the case ``sweep_bill_detail``
#: raises ``CONTROL_TOTAL_MISMATCH`` for -- would then be re-fetched on every
#: sweep for ever, burning the rate budget that is the binding constraint on
#: ERP Standard. The ledger records that we paid for the call once.
EVENT_BILL_DETAIL_HYDRATED = "integration.bill.detail.hydrated"

#: The PARTIAL unique index that makes a sweep's re-walk idempotent, named
#: because the writer infers it by its columns AND its predicate. Inferring by
#: columns alone matches no index here, and PostgreSQL would refuse the
#: statement rather than silently pick another -- which is the good failure,
#: but only if the predicate is written out. See :func:`raise_exception`.
EXCEPTION_OPEN_UNIQUE_INDEX = "ux_reconciliation_exception_open"

#: ``reconciliation_exception.kind`` -- exactly what
#: ``ck_reconciliation_exception_kind`` permits, and nothing else: the five 011
#: froze, FOREIGN_CURRENCY_BASIS_MISSING, which migration 030 added as the
#: deliberate contract change 011 said a sixth kind would have to be (product
#: owner decision 8, 2026-09-11: a receive or bill against a non-INR order is
#: held here, unbooked, until it carries its own currency and rate), and the
#: two migration 032 adds for adopting a tenant-raised order (product owner,
#: 2026-09-12): ADOPTION_DIMENSION_INVALID (the order's cf_capex_ref,
#: cf_wbs_code or cf_budget_head is missing, invalid or ambiguous -- nothing
#: is adopted) and ADOPTION_DIMENSION_CONFLICT (a repeat adoption sweep finds
#: those values changed since the order was adopted -- the local order is
#: left exactly as it was). A document-number gap is still folded into
#: CONTROL_TOTAL_MISMATCH with the missing numbers named, rather than
#: inventing a kind outside the set.
EXCEPTION_KINDS: tuple[str, ...] = (
    "GRN_LINE_UNATTRIBUTED", "CONTROL_TOTAL_MISMATCH",
    "LATE_ARRIVAL_CLOSED_PERIOD", "UNMAPPED_EXTERNAL_STATUS",
    "UNSANCTIONED_COMMITMENT", "FOREIGN_CURRENCY_BASIS_MISSING",
    "ADOPTION_DIMENSION_INVALID", "ADOPTION_DIMENSION_CONFLICT",
    "BILL_EXCEEDS_RECEIVE",
    # 034: internal fulfilment. Raised by `pg.internal_fulfilment`, never
    # by a sweep: a request whose project x WBS x budget-head mapping
    # resolves to no budget-owning cell, and one whose stock valuation
    # neither the ERP nor a reasoned manual entry has supplied.
    "INTERNAL_MAPPING_MISSING", "INTERNAL_VALUATION_MISSING",
)

#: C18's frozen ``exception_status`` namespace, verbatim, matching
#: ``ck_reconciliation_exception_status``. NOT a C3 business status: C18 records
#: the separation explicitly, and no value here may reach a business screen --
#: the business-visible consequence is the C3 code ``RECONCILIATION_PENDING`` on
#: the affected object.
EXCEPTION_OPEN = "Open"
EXCEPTION_RESOLVED = "Resolved"
EXCEPTION_ACCEPTED = "Accepted"
EXCEPTION_WRITTEN_OFF = "Written_off"
EXCEPTION_STATUSES: tuple[str, ...] = (
    EXCEPTION_OPEN, EXCEPTION_RESOLVED, EXCEPTION_ACCEPTED, EXCEPTION_WRITTEN_OFF,
)

#: The triage verbs a reviewer has, mapped to the C18 status each lands on.
#:
#: There are four verbs and three terminal statuses because ``retry`` and
#: ``resolve`` land on the same one for different reasons, and the reason is
#: what the audit trail keeps. ``retry`` says "the underlying condition should
#: be gone; let the next sweep decide" -- and it works precisely BECAUSE
#: :data:`EXCEPTION_OPEN_UNIQUE_INDEX` is partial on ``status = 'Open'``: once
#: this row leaves Open, the same condition recurring is a genuinely new
#: exception and is raisable again. A verb that merely deleted the row would
#: lose the fact that a human looked at it.
#:
#: ``ignore`` lands on ``Accepted``, not ``Resolved``: nothing was fixed, a
#: person decided to live with it, and a period-close report that cannot tell
#: those two apart is a report finance cannot use.
EXCEPTION_ACTIONS: dict[str, str] = {
    "resolve": EXCEPTION_RESOLVED,
    "retry": EXCEPTION_RESOLVED,
    "ignore": EXCEPTION_ACCEPTED,
    "write_off": EXCEPTION_WRITTEN_OFF,
}

# ================================================================= statuses
# C16, frozen. The database CHECK constraints in 010 carry exactly these
# values; tests/test_pg_integration_schema.py proves it rather than assuming.

#: ``integration_connection.mode`` (section 11.9). MOCK is the DEFAULT in the schema,
#: and :data:`LIVE_MODES` is the pair that cannot exist without a recorded
#: authoriser.
CONNECTION_MODES: tuple[str, ...] = ("MOCK", "SANDBOX", "LIVE_READ", "LIVE_WRITE")
#: The modes that reach a real tenant. Every one requires an authorisation
#: stamp, in the schema and again in :func:`set_connection_mode`.
LIVE_MODES: tuple[str, ...] = ("LIVE_READ", "LIVE_WRITE")
#: ``integration_connection.product`` -- C1's two-valued Literal, not a Zoho
#: product name.
PRODUCTS: tuple[str, ...] = ("ERP", "BOOKS_INVENTORY")

#: ``integration_inbox.state``.
INBOX_STATES: tuple[str, ...] = (
    "RECEIVED", "PROCESSED", "QUARANTINED", "DISCARDED", "DEAD",
)
#: ``integration_outbox.state``.
OUTBOX_STATES: tuple[str, ...] = ("PENDING", "SENT", "FAILED", "DEAD")
#: ``job.state``.
JOB_STATES: tuple[str, ...] = (
    "PENDING", "CLAIMED", "CHECKPOINTED", "DONE", "FAILED", "DEAD",
)
#: ``integration_circuit.state``. Prefixed, deliberately -- see the module
#: docstring and C16's own naming note.
CIRCUIT_STATES: tuple[str, ...] = (
    "CIRCUIT_CLOSED", "CIRCUIT_OPEN", "CIRCUIT_HALF_OPEN",
)
#: ``integration_rate_budget.window_kind``. Two windows, one shape.
WINDOW_KINDS: tuple[str, ...] = ("MINUTE", "DAY")
#: ``integration_rate_budget.allocation`` (section 11.6).
ALLOCATIONS: tuple[str, ...] = ("POLLING", "OUTBOUND", "INTERACTIVE")

# =============================================================== the numbers
# Plan constants, named once. Each is a fact from a specific section, and the
# section is cited because several of them look arbitrary and are not.

#: section 11.6's per-minute split, out of 100. The same shares are applied to the
#: DAY window -- see :func:`allocation_ceiling` for why the plan states them
#: only for the minute and why that is not a reason to leave the day
#: unallocated.
ALLOCATION_SHARES: Mapping[str, int] = {
    "POLLING": 60, "OUTBOUND": 30, "INTERACTIVE": 10,
}

#: section 2.2: the soft deadline is 12 minutes, 80% of the 15-minute Functions
#: ceiling. The schema CHECK-bounds the column at 900 s; this is the default
#: written into it.
DEFAULT_SOFT_DEADLINE_SECONDS = 720
#: section 2.1: the Catalyst Cron / Event Function ceiling, in seconds. A job is
#: KILLED here, not warned.
PLATFORM_FUNCTION_CEILING_SECONDS = 900
#: section 2.2: past this many resumes a job alerts rather than looping forever.
DEFAULT_MAX_RESUME_COUNT = 20
#: section 11.6: `max_attempts=8`, then DEAD and visible on SCR-39 with manual retry.
DEFAULT_MAX_ATTEMPTS = 8
#: section 11.6's backoff: `next_attempt_at = now() + random(0, min(900s, 2s*2^n))`.
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 900
#: section 11.5 / contracts fact 2: `last_modified_time` is filterable but NOT
#: sortable, so a window can be selected and not walked. Every poll therefore
#: re-reads 300 seconds it has already read. The schema makes 300 a FLOOR;
#: this is the value written by default.
WATERMARK_OVERLAP_SECONDS = 300

# ================================================== section 10.4 data classification
#: The JSON key names that must never appear in a stored payload, mirroring
#: ``capex_restricted_payload_keys()`` in the migration.
#:
#: Two lists rather than one, because the classification differs and
#: :func:`classify_payload` has to return the higher of the two. Regulated
#: identifiers (GSTIN, PAN) are needed for vendor matching and statutory
#: reporting and therefore live in ``vendor_master.gst_no`` / ``pan_no`` in the
#: clear -- but that mapped column is the ONLY place they belong, and a second
#: copy inside a raw payload is an unmasked, un-access-controlled duplicate of
#: a regulated identifier. Restricted fields (vendor bank details) have no
#: legitimate home anywhere in this product: it makes no payments.
#:
#: ``tests/test_pg_integration_schema.py`` asserts these name exactly the same
#: keys as the SQL function, in both directions.
REGULATED_PAYLOAD_KEYS: tuple[str, ...] = ("gst_no", "gstin", "pan_no", "pan")
RESTRICTED_ONLY_PAYLOAD_KEYS: tuple[str, ...] = (
    "bank_account_number", "account_number", "bank_accounts",
    "routing_number", "ifsc", "ifsc_code", "swift_code", "iban",
)
#: The union, in the migration's order.
RESTRICTED_PAYLOAD_KEYS: tuple[str, ...] = (
    REGULATED_PAYLOAD_KEYS + RESTRICTED_ONLY_PAYLOAD_KEYS
)

#: ``integration_inbox.payload_classification``.
CLASSIFICATIONS: tuple[str, ...] = (
    "PUBLIC", "CONFIDENTIAL", "REGULATED", "RESTRICTED",
)

#: Stamped into ``integration_inbox.redaction_policy_version`` on every row.
#: The column is NOT NULL with no default precisely so that a payload cannot be
#: stored without naming the redactor that produced it; bump this string when
#: the key lists above change, so a row is always traceable to the rules that
#: shaped it.
REDACTION_POLICY_VERSION = "2026-09-06.1"

#: A redacted key is REMOVED, not blanked, and the removal is recorded in
#: `integration_inbox.redacted_keys` instead.
#:
#: The first draft of this module replaced each value with a
#: `"[REDACTED]"` placeholder, on the reasoning that "the vendor did not send a
#: GSTIN" and "we removed the GSTIN" are different facts an operator needs to
#: tell apart on SCR-38. The reasoning was right and the mechanism was wrong,
#: in a way only writing the live test surfaced: the backstop CHECK matches on
#: KEY NAMES, so a payload that kept `"gst_no"` with a placeholder value was
#: still refused by `ck_integration_inbox_payload_carries_no_restricted_key`.
#: Every redacted receipt would have failed to insert -- inbound would have
#: stopped dead the first time a vendor record carried a GSTIN, in CI at the
#: earliest and in a cron function at worst.
#:
#: Removing the key satisfies the constraint absolutely (no restricted key name
#: appears in a stored payload, full stop) and keeps the distinction the
#: placeholder was for, in a better place: `redacted_keys` is a column an
#: operator can read and a query can filter on, which a magic string buried in
#: a JSON value is not. Section 10.4's word is "nulled"; this is that, with the
#: null recorded beside the payload rather than inside it.
#:
#: One honest limitation: `redacted_keys` holds key NAMES, not paths, so a
#: `gst_no` removed from three nested objects appears once. The question it has
#: to answer is "was this field removed, or never sent", and a name answers it.
REDACTION_REMOVES_THE_KEY = True


class IntegrationStoreError(Exception):
    """Raised for every rejected integration-store call.

    Same shape as ``budget.BudgetServiceError`` and ``periods.PeriodServiceError``
    for the same reason: ``code`` and ``status`` are what callers match on, and
    a router that has to parse a message string is a router that breaks when
    the message improves.
    """

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


# ======================================================== scope column maps
# Every mapping names ALL FOUR dimensions. `repo.compile_scope` REFUSES a
# restricted dimension a mapping omits (it raises `ScopeNotExpressible` rather
# than silently widening), so a dimension is waived only by an explicit `None`
# -- visible here and visible in review, unlike an omission.

#: `integration_connection` carries `entity_id` and nothing else. plant,
#: location and project are waived because the table has no column for any of
#: them -- the same waiver `entity_scope` uses in 004.
CONNECTION_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id", "plant": None, "location": None, "project": None,
}

#: Everything hung off a connection reaches `entity_id` through it, so the
#: `{scope}` token sits inside an `EXISTS (... integration_connection c ...)`
#: and the mapping points at the alias. The other three dimensions are waived
#: for the same reason as above: `integration_connection` has no column for
#: them either, so there is nothing further to reach.
VIA_CONNECTION_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "c.entity_id", "plant": None, "location": None, "project": None,
}

#: `job` carries its OWN entity_id and project_id, and is scoped directly.
#: Necessary rather than merely convenient: `job.connection_id` is nullable,
#: so scoping through a connection would scope by a column that is NULL on
#: exactly the estate-wide jobs. plant and location are waived -- no column,
#: and both are already enforced at `project`, which carries its own policy.
JOB_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id", "plant": None, "location": None, "project": "project_id",
}

#: `reconciliation_exception` carries its own `entity_id` AND `project_id`,
#: both nullable, and is scoped on both. plant and location are waived: 011
#: gives the table no column for either, and both are already enforced at
#: `project`, which carries its own policy.
#:
#: THE ARGUMENT ORDER THIS MIRRORS. 011's policy is
#: `capex_scope_permits(entity_id, NULL, NULL, project_id)`. It originally read
#: `(entity_id, NULL, project_id, NULL)` -- project_id in the LOCATION slot,
#: project waived -- and because all four parameters are `text`, PostgreSQL
#: accepted it silently and a principal restricted to one project could read
#: another project's `local_paise`. This mapping is the application-layer half
#: of the same predicate and is deliberately written to agree with it.
EXCEPTION_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "x.entity_id", "plant": None, "location": None,
    "project": "x.project_id",
}

#: The same mapping for a statement whose scope is decided by the values being
#: WRITTEN rather than by a row that exists yet -- an INSERT has no `x.` to
#: point at. `{scope}` sits inside an `EXISTS (... entity en ...)` for the same
#: reason `VIA_CONNECTION_SCOPE_COLUMNS` does, and `project` is waived because
#: an exception's `project_id` is copied from the purchase order the sweep is
#: already walking, never supplied by a caller, and the table's RLS
#: `WITH CHECK` is the authority on it either way.
EXCEPTION_WRITE_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "en.entity_id", "plant": None, "location": None, "project": None,
}


#: The scope clause a reconciliation WRITE embeds, spelled out at each call
#: site rather than returned by a helper.
#:
#: ``NULL`` short-circuits to permitted, exactly as :func:`record_event`'s
#: connection clause does and exactly as ``capex_scope_permits`` does
#: server-side. 011 is explicit that a NULL dimension is
#: unrestricted-by-that-dimension, because "an exception nobody can attribute
#: must be visible to whoever can resolve it"; compiling
#: ``entity_id = ANY(...)`` against a NULL evaluates to NULL, which is falsy,
#: and an unattributable exception would be silently refused by the very code
#: written to stop values disappearing.
#:
#: NOT a function returning the clause, deliberately. The token has to be
#: visible in the statement a reviewer reads -- and
#: ``tests/test_integration_store.py`` looks for a literal ``{scope}`` in the
#: SQL source for exactly that reason.

def _via_connection(connection_expr: str) -> str:
    """The `EXISTS` clause every child table's query embeds.

    `{scope}` sits INSIDE it, so `repo.query`'s token check is satisfied by the
    clause that actually does the filtering rather than by a token pasted
    somewhere harmless. `connection_expr` is whatever names the connection in
    the caller's query -- an aliased column (``i.connection_id``) or a
    parameter (``%(connection_id)s``) -- because an INSERT with no FROM has no
    column to point at and would otherwise need a second, near-identical
    helper that could drift.
    """
    return (
        f"EXISTS (SELECT 1 FROM {INTEGRATION_CONNECTION} c "
        f"WHERE c.connection_id = {connection_expr} AND {{scope}})"
    )


# ============================================================== small helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require(value: str | None, *, code: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise IntegrationStoreError(code, f"{what} is required and may not be blank.")
    return text


def _one(rows: Sequence[tuple]) -> tuple | None:
    return rows[0] if rows else None


def canonical_payload_sha(payload: Mapping[str, Any]) -> str:
    """The idempotency key: sha256 over a canonical rendering of `payload`.

    Canonical means sorted keys and no insignificant whitespace, so two
    deliveries of the same document hash the same however the transport
    happened to order or space the JSON. Without that, `ON CONFLICT` would
    miss the duplicate it exists to catch and the inbox would grow a second
    row per re-delivery.

    Hashed over the payload AS RECEIVED, before redaction. That is deliberate
    and it is the one place worth being explicit about, because section 10.4 forbids
    hashing a classified FIELD: this hashes the whole document, as an identity,
    and no field's value is recoverable from or replaced by the digest. The
    alternative -- hashing the sanitised payload -- would make two genuinely
    different vendor records collide the moment their only difference was a
    field we removed, and the second would be silently discarded as a
    duplicate.
    """
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def classify_payload(payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """``(classification, keys_present)`` for `payload` as received.

    Walks the whole document, objects and arrays alike, because a GSTIN sits
    three levels down inside `contact_persons[0]` as readily as at the top.
    Key names are matched case-insensitively HERE, unlike the SQL backstop,
    which matches exactly. The asymmetry is on purpose: the redactor should
    catch `GST_No` and remove it, and the constraint should not quietly accept
    a payload from a source whose casing we have not characterised.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).strip().lower() in _RESTRICTED_LOOKUP:
                    found.add(str(key).strip().lower())
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    if found & set(RESTRICTED_ONLY_PAYLOAD_KEYS):
        classification = "RESTRICTED"
    elif found:
        classification = "REGULATED"
    else:
        classification = "CONFIDENTIAL"
    return classification, tuple(sorted(found))


_RESTRICTED_LOOKUP = frozenset(RESTRICTED_PAYLOAD_KEYS)


def redact_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], str]:
    """``(sanitised, redacted_keys, classification)`` -- section 10.4 on persist.

    Every key named in :data:`RESTRICTED_PAYLOAD_KEYS` is REMOVED at every
    depth, and its name is returned in `redacted_keys` for the caller to store
    beside the payload -- see :data:`REDACTION_REMOVES_THE_KEY` for why removal
    rather than a placeholder, and for the live-only defect that reading was
    wrong about first time round.

    The cleartext of a Regulated identifier lives in its mapped column
    (``vendor_master.gst_no``, ``vendor_master.pan_no``) and nowhere else; the
    cleartext of a Restricted one lives nowhere at all, because this product
    makes no payments.

    This function IS the redaction. The CHECK constraint in the migration is
    the backstop that makes forgetting to call it loud, not a substitute for
    calling it, and the two differ in exactly one respect: this one normalises
    case and the constraint does not.
    """
    classification, present = classify_payload(payload)

    def scrub(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {
                key: scrub(value) for key, value in node.items()
                if str(key).strip().lower() not in _RESTRICTED_LOOKUP
            }
        if isinstance(node, (list, tuple)):
            return [scrub(item) for item in node]
        return node

    return scrub(dict(payload)), present, classification


def backoff_delay_seconds(attempts: int,
                          rng: random.Random | None = None) -> float:
    """section 11.6's backoff: ``random(0, min(900, 2 * 2^attempts))``.

    FULL jitter, not equal jitter and not a fixed exponential: the point is to
    break up the herd of retries that a shared outage synchronises. Half a
    dozen cron invocations that all failed at the same second and all back off
    by exactly 64 seconds retry at exactly the same second, which is the same
    outage again.

    `rng` is injectable so a test can assert the bounds without asserting a
    particular draw.
    """
    if attempts < 0:
        raise IntegrationStoreError(
            "NEGATIVE_ATTEMPTS", f"attempts must be >= 0; got {attempts}.")
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(attempts, 32)))
    return (rng or random).uniform(0.0, float(ceiling))


def _jitter_fraction(rng: random.Random | None = None) -> float:
    """The full-jitter multiplier, drawn in Python and applied in SQL.

    The exponential ceiling itself is computed IN the UPDATE, from the row's
    own `attempts` column, because the row is the only thing that knows how
    many attempts it has had after the same statement incremented it. Only the
    random fraction has to come from here -- PostgreSQL's `random()` is
    VOLATILE and would be re-evaluated per row, which is harmless but makes
    the value untestable, and there is no reason for a retry schedule to be
    the one thing in this module a test cannot pin.
    """
    return (rng or random).random()


def allocation_ceiling(total: int, allocation: str) -> int:
    """This allocation's share of a window's `total` ceiling.

    section 11.6 states the 60/30/10 split for the per-minute budget only. It is
    applied to the DAY window too, and the argument is the plan's own, only
    more so: "an operator clicking Test connection never starves behind a
    backfill" is a promise a per-minute-only split cannot keep, because a
    backfill that spends the whole 2,000-call day before lunch starves the
    operator until midnight however politely it paced itself minute by minute.

    Floored at 1 rather than 0: the schema requires ``ceiling > 0``, and a
    zero-ceiling row would be a budget that refuses every call, which reads in
    the logs exactly like a circuit that is open.
    """
    share = ALLOCATION_SHARES.get(allocation)
    if share is None:
        raise IntegrationStoreError(
            "UNKNOWN_ALLOCATION",
            f"{allocation!r} is not one of {sorted(ALLOCATION_SHARES)}.")
    return max(1, (total * share) // 100)


def window_keys(moment: datetime, tz: str = "UTC") -> tuple[str, str, datetime, datetime]:
    """``(minute_key, day_key, minute_start, day_start)`` for `moment` in `tz`.

    The bucket identity is computed HERE, in Python, and stored as text --
    ``integration_rate_budget.window_start_key`` -- rather than derived in SQL.
    Every PostgreSQL expression that could derive it (``date_trunc`` on a
    ``timestamptz``, ``timestamptz + interval``, ``extract`` on a
    ``timestamptz``) is STABLE rather than IMMUTABLE, because each depends on
    the session's TimeZone. A window boundary that means one thing in one
    session and another in the next is not a boundary, and a CHECK constraint
    built on one is a constraint that is true only while nobody looks.

    `tz` is recorded on the row alongside the key. WHOSE midnight the Zoho
    daily quota resets at is a tenant fact nobody has verified -- Phase 0B-2
    has not run -- so the schema records which midnight was assumed instead of
    encoding one. UTC is the default because it is the only choice that is
    honestly arbitrary rather than falsely specific.
    """
    if moment.tzinfo is None:
        raise IntegrationStoreError(
            "NAIVE_DATETIME",
            "window_keys requires an aware datetime; a naive one would be "
            "bucketed in whatever zone the interpreter happened to be in.")
    if tz.upper() == "UTC":
        zone: Any = timezone.utc
    else:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise IntegrationStoreError(
                "UNKNOWN_TIMEZONE",
                f"{tz!r} is not a timezone this runtime knows: {exc}") from exc
    local = moment.astimezone(zone)
    minute_start = local.replace(second=0, microsecond=0)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        minute_start.strftime("%Y-%m-%dT%H:%M"),
        day_start.strftime("%Y-%m-%d"),
        minute_start,
        day_start,
    )


def poll_window(hwm: datetime, *, until: datetime,
                overlap_seconds: int = WATERMARK_OVERLAP_SECONDS
                ) -> tuple[datetime, datetime]:
    """section 11.5's bounded, overlapping poll window: ``[hwm - overlap, until]``.

    Contracts fact 2: ``last_modified_time`` is filterable but NOT sortable, so
    a window can be SELECTED and cannot be WALKED as a stable keyset. There is
    consequently no way for a later poll to notice that an earlier one missed a
    record modified in the instant between its read and its watermark write.
    The overlap is the whole of the protection against that, and the inbox's
    UNIQUE constraint is what makes re-reading it free of consequence.

    Refuses an overlap below the floor rather than clamping it. Clamping would
    let a caller ask for 30 seconds, receive 300, and go on believing it had
    tuned something.
    """
    if overlap_seconds < WATERMARK_OVERLAP_SECONDS:
        raise IntegrationStoreError(
            "OVERLAP_BELOW_FLOOR",
            f"overlap_seconds must be at least {WATERMARK_OVERLAP_SECONDS} "
            f"(section 11.5); got {overlap_seconds}. last_modified_time cannot be "
            f"sorted, so a shortened overlap loses records with no way to "
            f"detect the loss.")
    return hwm - timedelta(seconds=overlap_seconds), until


# ========================================================= connections
def create_connection(session: Session, *, connection_id: str, entity_id: str,
                      product: str, dc: str, organization_id: str,
                      connector_name: str, actor: str,
                      mode: str = "MOCK",
                      per_minute_call_ceiling: int = 100,
                      daily_call_ceiling: int = 2000,
                      live_authorised_by: str | None = None,
                      live_authorisation_note: str | None = None,
                      now: datetime | None = None) -> dict:
    """Create a connection. `mode` defaults to MOCK and stays that way unless
    the caller both names a live mode and supplies an authorisation.

    The default is repeated here rather than left to the column default because
    a Python caller that omits `mode` should get MOCK from a signature it can
    read, not from a migration it would have to go and look up.
    """
    if product not in PRODUCTS:
        raise IntegrationStoreError(
            "UNKNOWN_PRODUCT", f"{product!r} is not one of {list(PRODUCTS)}.")
    _validate_mode(mode, live_authorised_by, live_authorisation_note)
    moment = now or _utcnow()
    authorised_at = moment if mode in LIVE_MODES else None

    rows = repo.query(
        session,
        f"""
        INSERT INTO {INTEGRATION_CONNECTION} (
            connection_id, entity_id, product, dc, organization_id,
            connector_name, mode, per_minute_call_ceiling, daily_call_ceiling,
            live_authorised_at, live_authorised_by, live_authorisation_note,
            created_at, created_by, updated_at, updated_by)
        SELECT %(connection_id)s, %(entity_id)s, %(product)s, %(dc)s,
               %(organization_id)s, %(connector_name)s, %(mode)s,
               %(per_minute)s, %(daily)s,
               %(authorised_at)s, %(authorised_by)s, %(authorisation_note)s,
               %(now)s, %(actor)s, %(now)s, %(actor)s
        FROM entity e
        WHERE e.entity_id = %(entity_id)s AND {{scope}}
        RETURNING connection_id, entity_id, product, dc, organization_id,
                  connector_name, mode, per_minute_call_ceiling,
                  daily_call_ceiling, is_active
        """,
        {
            "connection_id": _require(connection_id, code="BLANK_CONNECTION_ID",
                                      what="connection_id"),
            "entity_id": _require(entity_id, code="BLANK_ENTITY_ID",
                                  what="entity_id"),
            "product": product,
            "dc": _require(dc, code="BLANK_DC", what="dc"),
            "organization_id": _require(organization_id, code="BLANK_ORG_ID",
                                        what="organization_id"),
            "connector_name": _require(connector_name, code="BLANK_CONNECTOR",
                                       what="connector_name"),
            "mode": mode,
            "per_minute": per_minute_call_ceiling,
            "daily": daily_call_ceiling,
            "authorised_at": authorised_at,
            "authorised_by": live_authorised_by,
            "authorisation_note": live_authorisation_note,
            "now": moment,
            "actor": actor,
        },
        # The INSERT is gated on the caller being able to SEE the entity it is
        # creating a connection in -- otherwise a principal scoped to one
        # entity could attach a live Zoho organisation to another's. The
        # mapping names the `entity` alias rather than reusing
        # CONNECTION_SCOPE_COLUMNS, because the `{scope}` token here filters
        # the SOURCE table, not the target. The other three dimensions are
        # waived because `entity` has no column for any of them, which is the
        # same waiver `entity_scope` uses in migration 004.
        columns={"entity": "e.entity_id", "plant": None, "location": None,
                 "project": None},
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "ENTITY_NOT_FOUND",
            f"Entity {entity_id} does not exist or is out of scope.", status=404)
    return _connection_row(row)


def get_connection(session: Session, connection_id: str, *,
                   scope: Scope | None = None) -> dict | None:
    """The connection, or `None` for both "no such row" and "out of scope".

    Indistinguishable by design: a 403 on an id lookup is an existence oracle.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT connection_id, entity_id, product, dc, organization_id,
               connector_name, mode, per_minute_call_ceiling,
               daily_call_ceiling, is_active
        FROM {INTEGRATION_CONNECTION}
        WHERE connection_id = %(connection_id)s AND {{scope}}
        """,
        {"connection_id": connection_id},
        scope=scope,
        columns=CONNECTION_SCOPE_COLUMNS,
    )
    return _connection_row(row) if row else None


def list_connections(session: Session, *, entity_id: str | None = None,
                     mode: str | None = None,
                     active_only: bool = True) -> list[dict]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {}
    if entity_id is not None:
        conditions.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if mode is not None:
        conditions.append("mode = %(mode)s")
        params["mode"] = mode
    if active_only:
        conditions.append("is_active")
    where = " AND ".join(conditions)
    rows = repo.query(
        session,
        f"""
        SELECT connection_id, entity_id, product, dc, organization_id,
               connector_name, mode, per_minute_call_ceiling,
               daily_call_ceiling, is_active
        FROM {INTEGRATION_CONNECTION}
        WHERE {where} AND {{scope}}
        ORDER BY entity_id, connection_id
        """,
        params,
        columns=CONNECTION_SCOPE_COLUMNS,
    )
    return [_connection_row(row) for row in rows]


def set_connection_mode(session: Session, *, connection_id: str, mode: str,
                        actor: str, live_authorised_by: str | None = None,
                        live_authorisation_note: str | None = None,
                        now: datetime | None = None) -> dict:
    """Move a connection between modes, recording the authorisation for a live one.

    The refusal is duplicated -- here and in
    `ck_integration_connection_live_is_authorised` -- and both copies are
    wanted. The constraint is the guarantee: it holds against a psql session, a
    migration, and a caller that has not been written yet. This check is the
    one that produces an error a screen can render, instead of a
    CheckViolation whose message names a constraint the operator has never
    heard of.
    """
    _validate_mode(mode, live_authorised_by, live_authorisation_note)
    moment = now or _utcnow()
    live = mode in LIVE_MODES
    rows = repo.query(
        session,
        f"""
        UPDATE {INTEGRATION_CONNECTION} SET
            mode = %(mode)s,
            live_authorised_at = CASE WHEN %(live)s THEN %(now)s ELSE NULL END,
            live_authorised_by = CASE WHEN %(live)s THEN %(authorised_by)s ELSE NULL END,
            live_authorisation_note =
                CASE WHEN %(live)s THEN %(authorisation_note)s ELSE NULL END,
            updated_at = %(now)s,
            updated_by = %(actor)s,
            version_no = version_no + 1
        WHERE connection_id = %(connection_id)s AND {{scope}}
        RETURNING connection_id, entity_id, product, dc, organization_id,
                  connector_name, mode, per_minute_call_ceiling,
                  daily_call_ceiling, is_active
        """,
        {
            "connection_id": connection_id, "mode": mode, "live": live,
            "authorised_by": live_authorised_by,
            "authorisation_note": live_authorisation_note,
            "now": moment, "actor": actor,
        },
        columns=CONNECTION_SCOPE_COLUMNS,
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return _connection_row(row)


def _validate_mode(mode: str, authorised_by: str | None,
                   note: str | None) -> None:
    if mode not in CONNECTION_MODES:
        raise IntegrationStoreError(
            "UNKNOWN_MODE", f"{mode!r} is not one of {list(CONNECTION_MODES)}.")
    if mode not in LIVE_MODES:
        return
    if not (authorised_by or "").strip() or not (note or "").strip():
        raise IntegrationStoreError(
            "LIVE_MODE_UNAUTHORISED",
            f"Mode {mode} reaches a real tenant. section 11.9 requires explicit "
            f"authorisation: supply live_authorised_by and a "
            f"live_authorisation_note saying what was agreed.",
            status=403)


def _connection_row(row: tuple) -> dict:
    return {
        "connection_id": row[0], "entity_id": row[1], "product": row[2],
        "dc": row[3], "organization_id": row[4], "connector_name": row[5],
        "mode": row[6], "per_minute_call_ceiling": row[7],
        "daily_call_ceiling": row[8], "is_active": row[9],
    }


# ================================================================== inbox
def record_inbound(session: Session, *, inbox_id: str, connection_id: str,
                   module: str, external_id: str,
                   raw_payload: Mapping[str, Any],
                   external_status_raw: str | None = None,
                   external_status_product: str | None = None,
                   external_status_api_version: str | None = None,
                   correlation_id: str | None = None,
                   now: datetime | None = None) -> tuple[str | None, bool]:
    """Persist one received payload. Returns ``(inbox_id, inserted)``.

    IDEMPOTENCY IS THE CONSTRAINT (section 11.6). This is
    ``INSERT ... ON CONFLICT ON CONSTRAINT uq_integration_inbox_idempotency DO
    NOTHING RETURNING``: a re-delivered payload produces no row and returns
    ``(None, False)``. There is no "have I seen this?" SELECT anywhere in this
    function, deliberately -- the answer to that question is stale by the time
    it is acted on, and two poll invocations racing on the same bill under
    section 11.5's overlapping windows is the normal case, not the exceptional one.

    The payload is redacted BEFORE it is stored and hashed BEFORE it is
    redacted; see :func:`redact_payload` and :func:`canonical_payload_sha` for
    both halves of that and why they are that way round.
    """
    moment = now or _utcnow()
    sanitised, redacted_keys, classification = redact_payload(raw_payload)
    payload_sha = canonical_payload_sha(raw_payload)

    # TWO CTEs AND ONE ROW BACK, because "already seen" and "no such
    # connection" must not look alike.
    #
    # The plain `INSERT ... WHERE EXISTS(...) ON CONFLICT DO NOTHING RETURNING`
    # returns nothing in BOTH cases, and a poller reading that as "duplicate,
    # skip" would skip every payload on a connection it cannot see, silently
    # and for as long as anybody relied on it. `conn` resolves the connection
    # under scope, `ins` inserts from it, and the final SELECT returns one row
    # always: whether the connection resolved, and the inbox_id if a row was
    # actually written. One statement, so the duplicate path -- the COMMON path
    # under section 11.5's overlapping windows -- still costs one round trip.
    rows = repo.query(
        session,
        f"""
        WITH conn AS (
            SELECT c.connection_id
            FROM {INTEGRATION_CONNECTION} c
            WHERE c.connection_id = %(connection_id)s AND {{scope}}
        ), ins AS (
            INSERT INTO {INTEGRATION_INBOX} (
                inbox_id, connection_id, module, external_id, payload_sha,
                payload, external_status_raw, external_status_product,
                external_status_api_version, received_at, state,
                correlation_id, payload_classification,
                redaction_policy_version, redacted_keys)
            SELECT %(inbox_id)s, conn.connection_id, %(module)s,
                   %(external_id)s, %(payload_sha)s, %(payload)s::jsonb,
                   %(status_raw)s, %(status_product)s, %(status_api_version)s,
                   %(now)s, 'RECEIVED', %(correlation_id)s,
                   %(classification)s, %(policy)s, %(redacted_keys)s::text[]
            FROM conn
            ON CONFLICT ON CONSTRAINT {INBOX_IDEMPOTENCY_CONSTRAINT} DO NOTHING
            RETURNING inbox_id
        )
        SELECT (SELECT count(*) FROM conn), (SELECT inbox_id FROM ins)
        """,
        {
            "inbox_id": _require(inbox_id, code="BLANK_INBOX_ID",
                                 what="inbox_id"),
            "connection_id": connection_id,
            "module": _require(module, code="BLANK_MODULE", what="module"),
            "external_id": _require(external_id, code="BLANK_EXTERNAL_ID",
                                    what="external_id"),
            "payload_sha": payload_sha,
            "payload": json.dumps(sanitised, default=str),
            "status_raw": external_status_raw,
            "status_product": external_status_product,
            "status_api_version": external_status_api_version,
            "now": moment,
            "correlation_id": correlation_id,
            "classification": classification,
            "policy": REDACTION_POLICY_VERSION,
            "redacted_keys": list(redacted_keys),
        },
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    connection_resolved, written = rows[0]
    if not connection_resolved:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope, so "
            f"nothing was recorded. This is deliberately NOT reported as a "
            f"duplicate: a caller that treated it as one would silently drop "
            f"every payload on this connection.", status=404)
    return (written, True) if written is not None else (None, False)


def claim_inbox_batch(session: Session, *, connection_id: str, module: str,
                      limit: int = 200,
                      now: datetime | None = None) -> list[dict]:
    """The next batch of unprocessed receipts, claimed with
    ``FOR UPDATE SKIP LOCKED`` (section 2.2 step 1).

    SKIP LOCKED rather than a status flag, because a Function killed mid-batch
    releases its locks when its transaction dies and the rows become claimable
    again on the next tick with no reaper. A claimed-flag would need one.
    """
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        SELECT i.inbox_id, i.external_id, i.payload, i.external_status_raw,
               i.attempts, i.correlation_id
        FROM {INTEGRATION_INBOX} i
        WHERE i.connection_id = %(connection_id)s
          AND i.module = %(module)s
          AND i.state = 'RECEIVED'
          AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= %(now)s)
          AND {_via_connection('i.connection_id')}
        ORDER BY i.received_at
        LIMIT %(limit)s
        FOR UPDATE OF i SKIP LOCKED
        """,
        {"connection_id": connection_id, "module": module, "now": moment,
         "limit": limit},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return [
        {"inbox_id": r[0], "external_id": r[1], "payload": r[2],
         "external_status_raw": r[3], "attempts": r[4], "correlation_id": r[5]}
        for r in rows
    ]


def mark_inbox_processed(session: Session, *, inbox_id: str,
                         now: datetime | None = None) -> None:
    _transition_inbox(session, inbox_id=inbox_id, state="PROCESSED",
                      processed_at=now or _utcnow())


def discard_inbox(session: Session, *, inbox_id: str) -> None:
    """Mark a receipt superseded by one already applied.

    C16 defines DISCARDED as "duplicate payload_sha for an already-processed
    external_id". With `uq_integration_inbox_idempotency` in place that case
    never produces a row to mark -- the insert conflicts and nothing is
    written. What reaches here is the case the constraint cannot see: a
    DIFFERENT payload_sha for an external_id whose newer version has already
    been applied, which is ordinary out-of-order delivery under section 11.5's
    overlapping windows. The migration's header records the same thing, because
    a reader who assumes this counts re-deliveries will read a permanently
    near-zero number and conclude the poller is not overlapping.
    """
    _transition_inbox(session, inbox_id=inbox_id, state="DISCARDED")


def quarantine_inbox(session: Session, *, inbox_id: str, reason: str) -> None:
    """QUARANTINED, with a mandatory reason. section 11.8: never guessed, never spread
    pro-rata, never silently dropped. The caller is also expected to raise the
    matching `reconciliation_exception` -- which this stream does not own, and
    which does not exist yet."""
    _transition_inbox(
        session, inbox_id=inbox_id, state="QUARANTINED",
        quarantine_reason=_require(reason, code="BLANK_QUARANTINE_REASON",
                                   what="quarantine reason"))


def fail_inbox(session: Session, *, inbox_id: str, error: str,
               now: datetime | None = None,
               rng: random.Random | None = None) -> str:
    """Record a retryable failure, or DEAD once the attempts are spent.

    Returns the state written. The attempt increment and the state decision
    happen in ONE statement: a Function killed between "increment attempts" and
    "decide whether that was the last one" would otherwise leave a row that
    retries forever one attempt short of its ceiling.

    RECEIVED only. A QUARANTINED receipt is not waiting for another attempt, it
    is waiting for a person: section 11.8 has an unattributable record accumulate for
    review rather than being retried into a guess. Retrying it here would also
    have to move it out of QUARANTINED to satisfy
    `ck_integration_inbox_quarantine_reason`, which would throw away the reason
    it was quarantined for.
    """
    moment = now or _utcnow()
    jitter = _jitter_fraction(rng)
    rows = repo.query(
        session,
        f"""
        UPDATE {INTEGRATION_INBOX} i SET
            attempts = i.attempts + 1,
            last_error = %(error)s,
            state = CASE WHEN i.attempts + 1 >= i.max_attempts
                         THEN 'DEAD' ELSE 'RECEIVED' END,
            next_attempt_at = CASE WHEN i.attempts + 1 >= i.max_attempts
                                   THEN NULL
                                   ELSE %(now)s + make_interval(
                                       secs => LEAST(%(cap)s::double precision,
                                                     %(base)s::double precision
                                                     * power(2, i.attempts + 1))
                                               * %(jitter)s) END
        WHERE i.inbox_id = %(inbox_id)s
          AND i.state = 'RECEIVED'
          AND {_via_connection('i.connection_id')}
        RETURNING i.state
        """,
        {"inbox_id": inbox_id, "error": error, "now": moment,
         "cap": float(BACKOFF_CAP_SECONDS),
         "base": float(BACKOFF_BASE_SECONDS),
         "jitter": jitter},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "INBOX_NOT_RETRYABLE",
            f"Receipt {inbox_id} does not exist, is out of scope, or is in a "
            f"state that cannot be failed.", status=404)
    return row[0]


def _transition_inbox(session: Session, *, inbox_id: str, state: str,
                      processed_at: datetime | None = None,
                      quarantine_reason: str | None = None) -> None:
    if state not in INBOX_STATES:
        raise IntegrationStoreError(
            "UNKNOWN_INBOX_STATE", f"{state!r} is not one of {list(INBOX_STATES)}.")
    rows = repo.query(
        session,
        f"""
        UPDATE {INTEGRATION_INBOX} i SET
            state = %(state)s,
            processed_at = %(processed_at)s,
            quarantine_reason = %(quarantine_reason)s
        WHERE i.inbox_id = %(inbox_id)s
          AND {_via_connection('i.connection_id')}
        RETURNING i.inbox_id
        """,
        {"inbox_id": inbox_id, "state": state, "processed_at": processed_at,
         "quarantine_reason": quarantine_reason},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if not rows:
        raise IntegrationStoreError(
            "INBOX_NOT_FOUND",
            f"Receipt {inbox_id} does not exist or is out of scope.", status=404)


# ================================================================= outbox
def enqueue_outbound(session: Session, *, outbox_id: str, connection_id: str,
                     module: str, local_id: str, dedupe_key: str,
                     payload: Mapping[str, Any], actor: str,
                     correlation_id: str | None = None,
                     now: datetime | None = None) -> tuple[str, bool]:
    """Enqueue one outbound document. Returns ``(outbox_id, created)``.

    Idempotent on ``(connection_id, module, local_id)``: enqueuing the same
    local document twice returns the EXISTING row rather than creating a
    second. section 11.6's whole outbound story rests on there being exactly one row
    per local document per connection, because that row is what a retry
    updates rather than duplicating.

    `dedupe_key` is the value written into Zoho's unique custom field
    `cf_capex_ref`. Zoho documents no idempotency header, so this is the one we
    synthesise; `uq_integration_outbox_dedupe_key` keeps our side at least as
    tight as theirs.
    """
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        INSERT INTO {INTEGRATION_OUTBOX} (
            outbox_id, connection_id, module, local_id, dedupe_key, payload,
            state, correlation_id, created_at, created_by, updated_at, updated_by)
        SELECT %(outbox_id)s, %(connection_id)s, %(module)s, %(local_id)s,
               %(dedupe_key)s, %(payload)s::jsonb, 'PENDING',
               %(correlation_id)s, %(now)s, %(actor)s, %(now)s, %(actor)s
        WHERE EXISTS (SELECT 1 FROM {INTEGRATION_CONNECTION} c
                       WHERE c.connection_id = %(connection_id)s AND {{scope}})
        ON CONFLICT ON CONSTRAINT uq_integration_outbox_local_id DO NOTHING
        RETURNING outbox_id
        """,
        {
            "outbox_id": _require(outbox_id, code="BLANK_OUTBOX_ID",
                                  what="outbox_id"),
            "connection_id": connection_id,
            "module": _require(module, code="BLANK_MODULE", what="module"),
            "local_id": _require(local_id, code="BLANK_LOCAL_ID",
                                 what="local_id"),
            "dedupe_key": _require(dedupe_key, code="BLANK_DEDUPE_KEY",
                                   what="dedupe_key"),
            "payload": json.dumps(dict(payload), default=str),
            "correlation_id": correlation_id, "now": moment, "actor": actor,
        },
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    row = _one(rows)
    if row is not None:
        return row[0], True

    existing = repo.query_one(
        session,
        f"""
        SELECT o.outbox_id FROM {INTEGRATION_OUTBOX} o
        WHERE o.connection_id = %(connection_id)s
          AND o.module = %(module)s
          AND o.local_id = %(local_id)s
          AND {_via_connection('o.connection_id')}
        """,
        {"connection_id": connection_id, "module": module, "local_id": local_id},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if existing is None:
        # No insert and no existing row means the scope predicate refused the
        # connection. Reported as not-found, never as denied: see repo.py's
        # module docstring on existence oracles.
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return existing[0], False


def claim_outbox_batch(session: Session, *, connection_id: str,
                       limit: int = 25,
                       now: datetime | None = None) -> list[dict]:
    """section 2.2's `drain_outbox`: 25 rows per invocation, `FOR UPDATE SKIP LOCKED`."""
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        SELECT o.outbox_id, o.module, o.local_id, o.dedupe_key, o.payload,
               o.attempts, o.max_attempts, o.correlation_id
        FROM {INTEGRATION_OUTBOX} o
        WHERE o.connection_id = %(connection_id)s
          AND o.state IN ('PENDING', 'FAILED')
          AND coalesce(o.next_attempt_at, o.created_at) <= %(now)s
          AND {_via_connection('o.connection_id')}
        ORDER BY coalesce(o.next_attempt_at, o.created_at), o.created_at
        LIMIT %(limit)s
        FOR UPDATE OF o SKIP LOCKED
        """,
        {"connection_id": connection_id, "now": moment, "limit": limit},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return [
        {"outbox_id": r[0], "module": r[1], "local_id": r[2],
         "dedupe_key": r[3], "payload": r[4], "attempts": r[5],
         "max_attempts": r[6], "correlation_id": r[7]}
        for r in rows
    ]


def mark_outbox_sent(session: Session, *, outbox_id: str, external_id: str,
                     actor: str, now: datetime | None = None) -> None:
    """Record the external id Zoho minted. SENT and `external_id` move
    together, because `ck_integration_outbox_sent_carries_external_id` is a
    biconditional and either half alone is a claim we cannot substantiate."""
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        UPDATE {INTEGRATION_OUTBOX} o SET
            state = 'SENT',
            external_id = %(external_id)s,
            sent_at = %(now)s,
            next_attempt_at = NULL,
            last_error = NULL,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE o.outbox_id = %(outbox_id)s
          AND o.state IN ('PENDING', 'FAILED')
          AND {_via_connection('o.connection_id')}
        RETURNING o.outbox_id
        """,
        {"outbox_id": outbox_id,
         "external_id": _require(external_id, code="BLANK_EXTERNAL_ID",
                                 what="external_id"),
         "now": moment, "actor": actor},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if not rows:
        raise IntegrationStoreError(
            "OUTBOX_NOT_SENDABLE",
            f"Outbox row {outbox_id} does not exist, is out of scope, or has "
            f"already left PENDING/FAILED.", status=404)


def mark_outbox_failed(session: Session, *, outbox_id: str, error: str,
                       actor: str, now: datetime | None = None,
                       rng: random.Random | None = None) -> str:
    """section 11.6's retry: increment, back off with full jitter, DEAD at the ceiling.

    One statement, for the reason :func:`fail_inbox` gives: a Function killed
    between the increment and the decision leaves a row retrying forever one
    attempt short of DEAD, which is exactly the shape of failure that never
    reaches an alert.
    """
    moment = now or _utcnow()
    jitter = _jitter_fraction(rng)
    rows = repo.query(
        session,
        f"""
        UPDATE {INTEGRATION_OUTBOX} o SET
            attempts = o.attempts + 1,
            last_error = %(error)s,
            state = CASE WHEN o.attempts + 1 >= o.max_attempts
                         THEN 'DEAD' ELSE 'FAILED' END,
            next_attempt_at = CASE WHEN o.attempts + 1 >= o.max_attempts
                                   THEN NULL
                                   ELSE %(now)s + make_interval(
                                       secs => LEAST(%(cap)s::double precision,
                                                     %(base)s::double precision
                                                     * power(2, o.attempts + 1))
                                               * %(jitter)s) END,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE o.outbox_id = %(outbox_id)s
          AND o.state IN ('PENDING', 'FAILED')
          AND {_via_connection('o.connection_id')}
        RETURNING o.state
        """,
        {"outbox_id": outbox_id, "error": error, "now": moment, "actor": actor,
         "cap": float(BACKOFF_CAP_SECONDS),
         "base": float(BACKOFF_BASE_SECONDS),
         "jitter": jitter},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "OUTBOX_NOT_RETRYABLE",
            f"Outbox row {outbox_id} does not exist, is out of scope, or has "
            f"already left PENDING/FAILED.", status=404)
    return row[0]


# =================================================================== jobs
def enqueue_job(session: Session, *, job_id: str, kind: str,
                principal_user_id: str, actor: str,
                entity_id: str | None, project_id: str | None = None,
                connection_id: str | None = None,
                checkpoint: Mapping[str, Any] | None = None,
                scope_snapshot: Mapping[str, Any] | None = None,
                soft_deadline_seconds: int = DEFAULT_SOFT_DEADLINE_SECONDS,
                max_resume_count: int = DEFAULT_MAX_RESUME_COUNT,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                run_after: datetime | None = None,
                correlation_id: str | None = None,
                now: datetime | None = None) -> str:
    """Enqueue one job.

    `entity_id` has NO DEFAULT even though it is nullable. A NULL waives the
    entity scope dimension -- correct for `verify_audit_chains` and
    `sweep_control_totals`, which are estate-wide -- and a keyword with a
    default of None would let a connector job acquire that waiver by omission.
    Passing `entity_id=None` is a decision visible at the call site.

    `scope_snapshot` is SVC-EXPORT's: section 10.3 says an export runs under the
    REQUESTING user's scope and never its own, which is only possible if that
    scope is serialised onto the row and rehydrated by whichever invocation
    picks it up.
    """
    if soft_deadline_seconds > PLATFORM_FUNCTION_CEILING_SECONDS:
        raise IntegrationStoreError(
            "SOFT_DEADLINE_ABOVE_CEILING",
            f"soft_deadline_seconds {soft_deadline_seconds} exceeds the "
            f"{PLATFORM_FUNCTION_CEILING_SECONDS}s AppSail Functions ceiling "
            f"(section 2.1). A deadline past the kill point is not a deadline.")
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        INSERT INTO {JOB} (
            job_id, kind, state, connection_id, entity_id, project_id,
            principal_user_id, scope_snapshot, checkpoint,
            soft_deadline_seconds, max_resume_count, max_attempts, run_after,
            correlation_id, created_at, created_by, updated_at, updated_by)
        SELECT %(job_id)s, %(kind)s, 'PENDING', %(connection_id)s,
               %(entity_id)s, %(project_id)s, %(principal_user_id)s,
               %(scope_snapshot)s::jsonb, %(checkpoint)s::jsonb,
               %(soft_deadline_seconds)s, %(max_resume_count)s,
               %(max_attempts)s, %(run_after)s, %(correlation_id)s,
               %(now)s, %(actor)s, %(now)s, %(actor)s
        WHERE capex_scope_permits(%(entity_id)s, NULL, NULL, %(project_id)s)
          AND {{scope}}
        RETURNING job_id
        """,
        {
            "job_id": _require(job_id, code="BLANK_JOB_ID", what="job_id"),
            "kind": _require(kind, code="BLANK_JOB_KIND", what="kind"),
            "connection_id": connection_id, "entity_id": entity_id,
            "project_id": project_id, "principal_user_id": principal_user_id,
            "scope_snapshot": (json.dumps(dict(scope_snapshot), default=str)
                               if scope_snapshot is not None else None),
            "checkpoint": (json.dumps(dict(checkpoint), default=str)
                           if checkpoint is not None else None),
            "soft_deadline_seconds": soft_deadline_seconds,
            "max_resume_count": max_resume_count,
            "max_attempts": max_attempts,
            "run_after": run_after or moment,
            "correlation_id": correlation_id, "now": moment, "actor": actor,
        },
        # The compiled predicate is a second, independent enforcement of the
        # same scope the `capex_scope_permits` call above applies at the RLS
        # layer -- defence in depth, exactly as repo.py's docstring describes.
        # There is no FROM clause here, so the predicate is evaluated against
        # the parameter values rather than a column, which is why the
        # dimensions are waived: there is no column for it to name.
        columns={"entity": None, "plant": None, "location": None,
                 "project": None},
    )
    if not rows:
        raise IntegrationStoreError(
            "JOB_SCOPE_REFUSED",
            f"Job {job_id} names entity {entity_id!r} / project {project_id!r}, "
            f"which this principal's scope does not permit.", status=403)
    return rows[0][0]


def claim_job(session: Session, *, kind: str, worker: str,
              lease_seconds: int | None = None, limit: int = 1,
              now: datetime | None = None) -> list[dict]:
    """Claim runnable jobs of `kind` with ``FOR UPDATE SKIP LOCKED`` (section 2.2).

    Sets the four things `ck_job_claimed_is_bounded` requires together --
    `soft_deadline_at`, `locked_until`, `locked_by`, `started_at` -- because
    the constraint refuses a CLAIMED row missing any of them, and it refuses
    them for the reason the reaper needs: a claim with no expiry cannot be
    distinguished from a live invocation, so a killed Function's job would
    either be stolen from a running worker or leaked forever.

    The lease is `lease_seconds` (defaulting to the platform ceiling, NOT to
    the soft deadline): a Function that overruns its soft deadline is still
    alive and still holding the row until the platform kills it at 15 minutes.
    A lease equal to the soft deadline would let a second invocation claim a
    job the first is still working on.
    """
    moment = now or _utcnow()
    lease = lease_seconds or PLATFORM_FUNCTION_CEILING_SECONDS
    rows = repo.query(
        session,
        f"""
        WITH claimable AS (
            SELECT j.job_id
            FROM {JOB} j
            WHERE j.kind = %(kind)s
              AND j.state IN ('PENDING', 'CHECKPOINTED', 'FAILED')
              AND j.run_after <= %(now)s
              AND j.resume_count < j.max_resume_count
              AND {{scope}}
            ORDER BY j.run_after
            LIMIT %(limit)s
            FOR UPDATE OF j SKIP LOCKED
        )
        UPDATE {JOB} SET
            state = 'CLAIMED',
            started_at = coalesce({JOB}.started_at, %(now)s),
            locked_by = %(worker)s,
            locked_until = %(now)s + make_interval(secs => %(lease)s),
            soft_deadline_at = %(now)s + make_interval(
                secs => {JOB}.soft_deadline_seconds),
            updated_at = %(now)s,
            updated_by = %(worker)s
        FROM claimable
        WHERE {JOB}.job_id = claimable.job_id
        RETURNING {JOB}.job_id, {JOB}.kind, {JOB}.checkpoint,
                  {JOB}.scope_snapshot, {JOB}.soft_deadline_at,
                  {JOB}.resume_count, {JOB}.max_resume_count,
                  {JOB}.connection_id, {JOB}.correlation_id
        """,
        {"kind": kind, "worker": worker, "now": moment, "lease": float(lease),
         "limit": limit},
        # The `{scope}` token sits inside the CTE, over `j`, so the claim can
        # never lock a row the principal could not see. Aliased there, which is
        # why the mapping names `j.` columns.
        columns={"entity": "j.entity_id", "plant": None, "location": None,
                 "project": "j.project_id"},
    )
    return [
        {"job_id": r[0], "kind": r[1], "checkpoint": r[2],
         "scope_snapshot": r[3], "soft_deadline_at": r[4],
         "resume_count": r[5], "max_resume_count": r[6],
         "connection_id": r[7], "correlation_id": r[8]}
        for r in rows
    ]


def checkpoint_job(session: Session, *, job_id: str,
                   checkpoint: Mapping[str, Any], actor: str,
                   run_after: datetime | None = None,
                   now: datetime | None = None) -> str:
    """Commit progress, persist the cursor, release the lease. Returns the state.

    THIS is where the resume ceiling is enforced, and it is here rather than in
    :func:`claim_job` on purpose. `resume_count` counts the invocations that
    ran out of time, so it is incremented by the invocation that ran out --
    and the same statement that increments it decides whether that was the last
    one. A job whose incremented count reaches `max_resume_count` is written
    DEAD, with `alerted_at` and `finished_at`, in that single statement.

    section 2.2: "a job exceeding max_resume_count raises an alert rather than looping
    forever." `ck_job_resume_ceiling_is_terminal` makes the alternative
    unrepresentable -- at the ceiling the only states the row may hold are DONE
    and DEAD, so there is no UPDATE anywhere that could return it to a
    claimable state. This function is what makes the reachable case DEAD rather
    than a constraint violation nobody expected.
    """
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        UPDATE {JOB} j SET
            resume_count = j.resume_count + 1,
            checkpoint = %(checkpoint)s::jsonb,
            state = CASE WHEN j.resume_count + 1 >= j.max_resume_count
                         THEN 'DEAD' ELSE 'CHECKPOINTED' END,
            alerted_at = CASE WHEN j.resume_count + 1 >= j.max_resume_count
                              THEN %(now)s ELSE j.alerted_at END,
            finished_at = CASE WHEN j.resume_count + 1 >= j.max_resume_count
                               THEN %(now)s ELSE NULL END,
            last_error = CASE WHEN j.resume_count + 1 >= j.max_resume_count
                              THEN %(ceiling_error)s ELSE j.last_error END,
            locked_by = NULL,
            locked_until = NULL,
            soft_deadline_at = NULL,
            run_after = %(run_after)s,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE j.job_id = %(job_id)s
          AND j.state = 'CLAIMED'
          AND {{scope}}
        RETURNING j.state, j.resume_count
        """,
        {"job_id": job_id,
         "checkpoint": json.dumps(dict(checkpoint), default=str),
         "run_after": run_after or moment, "now": moment, "actor": actor,
         "ceiling_error":
             f"RESUME_CEILING_EXCEEDED: this job consumed its full "
             f"max_resume_count of invocations without completing. section 2.2 "
             f"requires an alert rather than another resume."},
        columns={"entity": "j.entity_id", "plant": None, "location": None,
                 "project": "j.project_id"},
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "JOB_NOT_CLAIMED",
            f"Job {job_id} does not exist, is out of scope, or is not CLAIMED "
            f"-- only the invocation holding a job may checkpoint it.",
            status=409)
    return row[0]


def finish_job(session: Session, *, job_id: str, actor: str,
               checkpoint: Mapping[str, Any] | None = None,
               now: datetime | None = None) -> None:
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        UPDATE {JOB} j SET
            state = 'DONE',
            checkpoint = coalesce(%(checkpoint)s::jsonb, j.checkpoint),
            finished_at = %(now)s,
            locked_by = NULL,
            locked_until = NULL,
            soft_deadline_at = NULL,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE j.job_id = %(job_id)s
          AND j.state = 'CLAIMED'
          AND {{scope}}
        RETURNING j.job_id
        """,
        {"job_id": job_id, "now": moment, "actor": actor,
         "checkpoint": (json.dumps(dict(checkpoint), default=str)
                        if checkpoint is not None else None)},
        columns={"entity": "j.entity_id", "plant": None, "location": None,
                 "project": "j.project_id"},
    )
    if not rows:
        raise IntegrationStoreError(
            "JOB_NOT_CLAIMED",
            f"Job {job_id} does not exist, is out of scope, or is not CLAIMED.",
            status=409)


def fail_job(session: Session, *, job_id: str, error: str, actor: str,
             now: datetime | None = None,
             rng: random.Random | None = None) -> str:
    """Retryable failure, or DEAD at the attempt ceiling -- with the alert."""
    moment = now or _utcnow()
    jitter = _jitter_fraction(rng)
    rows = repo.query(
        session,
        f"""
        UPDATE {JOB} j SET
            attempts = j.attempts + 1,
            last_error = %(error)s,
            state = CASE WHEN j.attempts + 1 >= j.max_attempts
                         THEN 'DEAD' ELSE 'FAILED' END,
            alerted_at = CASE WHEN j.attempts + 1 >= j.max_attempts
                              THEN %(now)s ELSE j.alerted_at END,
            finished_at = CASE WHEN j.attempts + 1 >= j.max_attempts
                               THEN %(now)s ELSE NULL END,
            locked_by = NULL,
            locked_until = NULL,
            soft_deadline_at = NULL,
            run_after = CASE WHEN j.attempts + 1 >= j.max_attempts
                             THEN j.run_after
                             ELSE %(now)s + make_interval(
                                 secs => LEAST(%(cap)s::double precision,
                                               %(base)s::double precision
                                               * power(2, j.attempts + 1))
                                         * %(jitter)s) END,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE j.job_id = %(job_id)s
          AND j.state = 'CLAIMED'
          AND {{scope}}
        RETURNING j.state
        """,
        {"job_id": job_id, "error": error, "now": moment, "actor": actor,
         "cap": float(BACKOFF_CAP_SECONDS),
         "base": float(BACKOFF_BASE_SECONDS),
         "jitter": jitter},
        columns={"entity": "j.entity_id", "plant": None, "location": None,
                 "project": "j.project_id"},
    )
    row = _one(rows)
    if row is None:
        raise IntegrationStoreError(
            "JOB_NOT_CLAIMED",
            f"Job {job_id} does not exist, is out of scope, or is not CLAIMED.",
            status=409)
    return row[0]


def reap_expired_jobs(session: Session, *, actor: str, limit: int = 100,
                      now: datetime | None = None) -> list[str]:
    """Return CLAIMED jobs whose holder was killed to a claimable state.

    section 2.1: an AppSail instance has a 5-minute lifetime and a Function is killed
    at 15. A job held by an invocation that no longer exists is the normal
    outcome, not an exception, and nothing releases the row on its own: the
    lock died with the transaction, but `state = 'CLAIMED'` and the claim
    filter excludes it. This is what puts it back.

    `resume_count` is NOT incremented here. The count exists to bound the
    number of times a job politely checkpoints and asks for more; an invocation
    that was killed never got to checkpoint, and charging it a resume would let
    a run of platform restarts kill a job that had made no progress at all and
    had nothing wrong with it.
    """
    moment = now or _utcnow()
    rows = repo.query(
        session,
        f"""
        WITH expired AS (
            SELECT j.job_id FROM {JOB} j
            WHERE j.state = 'CLAIMED'
              AND j.locked_until IS NOT NULL
              AND j.locked_until < %(now)s
              AND {{scope}}
            ORDER BY j.locked_until
            LIMIT %(limit)s
            FOR UPDATE OF j SKIP LOCKED
        )
        UPDATE {JOB} SET
            state = CASE WHEN {JOB}.checkpoint IS NOT NULL
                         THEN 'CHECKPOINTED' ELSE 'PENDING' END,
            locked_by = NULL,
            locked_until = NULL,
            soft_deadline_at = NULL,
            run_after = %(now)s,
            last_error = %(reaped)s,
            updated_at = %(now)s,
            updated_by = %(actor)s
        FROM expired
        WHERE {JOB}.job_id = expired.job_id
        RETURNING {JOB}.job_id
        """,
        {"now": moment, "limit": limit, "actor": actor,
         "reaped": "REAPED: the invocation holding this job did not return "
                   "before its lease expired (section 2.1: Functions are killed at "
                   "15 minutes, instances live 5)."},
        columns={"entity": "j.entity_id", "plant": None, "location": None,
                 "project": "j.project_id"},
    )
    return [r[0] for r in rows]


# ============================================================= watermarks
def read_watermark(session: Session, *, connection_id: str,
                   module: str) -> dict | None:
    row = repo.query_one(
        session,
        f"""
        SELECT w.hwm, w.overlap_seconds, w.last_polled_at, w.last_page_count,
               w.rewound_at, w.rewind_reason
        FROM {INTEGRATION_WATERMARK} w
        WHERE w.connection_id = %(connection_id)s
          AND w.module = %(module)s
          AND {_via_connection('w.connection_id')}
        """,
        {"connection_id": connection_id, "module": module},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        return None
    return {"hwm": row[0], "overlap_seconds": row[1], "last_polled_at": row[2],
            "last_page_count": row[3], "rewound_at": row[4],
            "rewind_reason": row[5]}


def advance_watermark(session: Session, *, connection_id: str, module: str,
                      hwm: datetime, actor: str,
                      pages_read: int | None = None,
                      now: datetime | None = None) -> datetime:
    """Move the high-water mark forward, never backward.

    `GREATEST` rather than a plain assignment: two poll invocations that
    overlap -- which section 11.5's windows guarantee they will -- must not let the
    slower one's older mark overwrite the faster one's newer. Without it the
    same window is re-polled indefinitely and the daily budget drains for
    nothing, and every symptom of that points at the vendor.

    A genuine rewind goes through :func:`rewind_watermark`, which is refused by
    a trigger unless it records why.
    """
    moment = now or _utcnow()
    row = repo.query_one(
        session,
        f"""
        INSERT INTO {INTEGRATION_WATERMARK} (
            connection_id, module, hwm, overlap_seconds, last_polled_at,
            last_page_count, updated_at, updated_by)
        SELECT %(connection_id)s, %(module)s, %(hwm)s, %(overlap)s, %(now)s,
               %(pages)s, %(now)s, %(actor)s
        WHERE EXISTS (SELECT 1 FROM {INTEGRATION_CONNECTION} c
                       WHERE c.connection_id = %(connection_id)s AND {{scope}})
        ON CONFLICT (connection_id, module) DO UPDATE SET
            hwm = GREATEST({INTEGRATION_WATERMARK}.hwm, EXCLUDED.hwm),
            last_polled_at = EXCLUDED.last_polled_at,
            last_page_count = EXCLUDED.last_page_count,
            updated_at = EXCLUDED.updated_at,
            updated_by = EXCLUDED.updated_by
        RETURNING hwm
        """,
        {"connection_id": connection_id, "module": module, "hwm": hwm,
         "overlap": WATERMARK_OVERLAP_SECONDS, "pages": pages_read,
         "now": moment, "actor": actor},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return row[0]


def rewind_watermark(session: Session, *, connection_id: str, module: str,
                     hwm: datetime, reason: str, actor: str,
                     now: datetime | None = None) -> datetime:
    """Deliberately move the mark backwards, on the record.

    A rewind re-pulls a window already read. Correctness is safe -- the inbox
    UNIQUE absorbs every duplicate -- and capacity is not: on ERP Standard
    (2,000 calls/day) re-pulling a month can consume a whole day, after which
    the poller starves silently until midnight and every downstream figure is
    quietly stale with no error anywhere.

    So the reason is mandatory here AND enforced by
    `assert_integration_watermark_no_silent_rewind` in the migration. The
    trigger is the guarantee; this is what turns it into an error a screen can
    show.
    """
    moment = now or _utcnow()
    row = repo.query_one(
        session,
        f"""
        UPDATE {INTEGRATION_WATERMARK} w SET
            hwm = %(hwm)s,
            rewound_at = %(now)s,
            rewind_reason = %(reason)s,
            updated_at = %(now)s,
            updated_by = %(actor)s
        WHERE w.connection_id = %(connection_id)s
          AND w.module = %(module)s
          AND {_via_connection('w.connection_id')}
        RETURNING w.hwm
        """,
        {"connection_id": connection_id, "module": module, "hwm": hwm,
         "reason": _require(reason, code="BLANK_REWIND_REASON",
                            what="rewind reason"),
         "now": moment, "actor": actor},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "WATERMARK_NOT_FOUND",
            f"No watermark for ({connection_id}, {module}), or it is out of "
            f"scope.", status=404)
    return row[0]


# =========================================================== rate budgets
class RateBudgetExhausted(IntegrationStoreError):
    """The reservation would have exceeded a window's ceiling.

    Carries `window_kind` because the two windows require entirely different
    responses. MINUTE means "checkpoint and resume on the next tick" -- section 11.6's
    handling of a 429/44, which does NOT count toward the circuit because our
    own throttle failed, not the vendor. DAY means the connection is finished
    until the next day boundary, which is the binding constraint on ERP
    Standard and is an alert, not a retry.
    """

    def __init__(self, window_kind: str, message: str):
        super().__init__("RATE_BUDGET_EXHAUSTED", message, status=429)
        self.window_kind = window_kind


def ensure_rate_budget_windows(session: Session, *, connection_id: str,
                               allocation: str, moment: datetime,
                               tz: str = "UTC",
                               scope: Scope | None = None) -> None:
    """Create the MINUTE and DAY rows for `moment`'s windows if absent.

    BOTH, always, in one call, and there is no function anywhere in this module
    that creates one without the other. That is the schema-adjacent half of
    "the daily window is not an afterthought": a caller cannot end up throttled
    per-minute and unlimited per-day by forgetting an argument, because there
    is no argument to forget.

    The ceiling is copied onto each row rather than joined to the connection at
    read time. Raising a tenant's plan tier at noon must not retroactively
    rewrite what the morning's budget was, and
    `ck_integration_rate_budget_used_within_ceiling` has to compare `used`
    against a value that cannot move underneath it.
    """
    if allocation not in ALLOCATIONS:
        raise IntegrationStoreError(
            "UNKNOWN_ALLOCATION",
            f"{allocation!r} is not one of {list(ALLOCATIONS)}.")
    minute_key, day_key, minute_start, day_start = window_keys(moment, tz)
    for kind, key, start, seconds, ceiling_column in (
        ("MINUTE", minute_key, minute_start, 60, "per_minute_call_ceiling"),
        ("DAY", day_key, day_start, 86400, "daily_call_ceiling"),
    ):
        repo.query(
            session,
            f"""
            INSERT INTO {INTEGRATION_RATE_BUDGET} (
                connection_id, window_kind, allocation, window_start,
                window_start_key, window_seconds, window_tz, ceiling, used,
                updated_at)
            SELECT c.connection_id, %(kind)s, %(allocation)s, %(start)s,
                   %(key)s, %(seconds)s, %(tz)s,
                   GREATEST(1, (c.{ceiling_column} * %(share)s) / 100), 0,
                   %(now)s
            FROM {INTEGRATION_CONNECTION} c
            WHERE c.connection_id = %(connection_id)s AND {{scope}}
            ON CONFLICT (connection_id, window_kind, allocation,
                         window_start_key) DO NOTHING
            RETURNING window_kind
            """,
            {"connection_id": connection_id, "kind": kind,
             "allocation": allocation, "start": start, "key": key,
             "seconds": seconds, "tz": tz,
             "share": ALLOCATION_SHARES[allocation], "now": moment},
            scope=scope,
            columns=VIA_CONNECTION_SCOPE_COLUMNS,
        )


class _ReservationRefused(Exception):
    """Internal: unwinds the reservation savepoint. Never escapes this module.

    A private exception rather than an early `return`, because the savepoint is
    only rolled back if the `with` block exits by RAISING. Returning would
    COMMIT the partial reservation, which is the exact bug the savepoint is
    there to prevent.
    """

    def __init__(self, windows: list[str]):
        self.windows = windows
        super().__init__(f"reservation refused for {windows}")


def reserve_calls(session: Session, *, connection_id: str, allocation: str,
                  count: int = 1, tz: str = "UTC",
                  now: datetime | None = None,
                  scope: Scope | None = None) -> dict[str, dict[str, int]]:
    """Reserve `count` API calls against BOTH windows, atomically.

    ONE `UPDATE` touching both rows, never two statements. If the day is
    exhausted and the minute is not, neither is spent; two statements would
    leave the minute window charged for a call the day refused, and the drift
    is silent, cumulative, and only ever in the direction that makes the
    throttle look healthier than it is.

    THE GUARD IS IN THE STATEMENT, and so is the arbitration.
    ``used + count <= ceiling`` sits in the ``WHERE``, so an over-budget row is
    simply not updated and the reservation is decided by the same statement
    that would have done the spending. section 2.1 says there is no resident process,
    so two cron invocations reserving at the same instant have nowhere else to
    arbitrate, and a read-then-check in Python is stale by the time it is acted
    on -- stale, again, in the direction that overspends.

    The whole reservation runs inside a SAVEPOINT. That is what makes a partial
    reservation impossible: if the statement updates one window and not the
    other, the savepoint is rolled back and neither charge survives -- and,
    unlike letting `ck_integration_rate_budget_used_within_ceiling` raise, the
    caller's transaction is still usable afterwards, so a poller can checkpoint
    and return rather than losing the work it had already done this
    invocation. That last point is not a nicety: section 2.2's whole contract is
    "commit progress and return", and a function that can only report
    exhaustion by poisoning the transaction cannot honour it.

    The CHECK constraint is still there and is still the guarantee. It holds
    against a psql session, a future caller, and a bug in this function; the
    ``WHERE`` guard is what turns the ordinary case into an error a screen can
    render instead of a CheckViolation naming a constraint the operator has
    never heard of. Both, for the same reason the migration uses both a trigger
    and a REVOKE on its append-only table.

    Returns ``{window_kind: {"used": n, "ceiling": n, "remaining": n}}``.
    """
    if count < 1:
        raise IntegrationStoreError(
            "NON_POSITIVE_RESERVATION",
            f"count must be at least 1; got {count}.")
    moment = now or _utcnow()
    ensure_rate_budget_windows(session, connection_id=connection_id,
                               allocation=allocation, moment=moment, tz=tz,
                               scope=scope)
    minute_key, day_key, _, _ = window_keys(moment, tz)

    try:
        with session.connection.transaction():
            rows = repo.query(
                session,
                f"""
                UPDATE {INTEGRATION_RATE_BUDGET} b SET
                    used = b.used + %(count)s,
                    exhausted_at = CASE WHEN b.used + %(count)s >= b.ceiling
                                        THEN %(now)s ELSE NULL END,
                    updated_at = %(now)s
                WHERE b.connection_id = %(connection_id)s
                  AND b.allocation = %(allocation)s
                  AND b.used + %(count)s <= b.ceiling
                  AND ((b.window_kind = 'MINUTE'
                        AND b.window_start_key = %(minute_key)s)
                    OR (b.window_kind = 'DAY'
                        AND b.window_start_key = %(day_key)s))
                  AND {_via_connection('b.connection_id')}
                RETURNING b.window_kind, b.used, b.ceiling
                """,
                {"connection_id": connection_id, "allocation": allocation,
                 "count": count, "minute_key": minute_key, "day_key": day_key,
                 "now": moment},
                columns=VIA_CONNECTION_SCOPE_COLUMNS,
            )
            granted = {
                r[0]: {"used": r[1], "ceiling": r[2], "remaining": r[2] - r[1]}
                for r in rows
            }
            if set(granted) != set(WINDOW_KINDS):
                raise _ReservationRefused(
                    sorted(set(WINDOW_KINDS) - set(granted)))
            return granted
    except _ReservationRefused as refused:
        blocked = refused.windows

    # Outside the savepoint, so this read runs on a healthy transaction and
    # sees the pre-reservation figures -- which are the ones an operator needs.
    state = read_rate_budget(session, connection_id=connection_id,
                             allocation=allocation, tz=tz, now=moment,
                             scope=scope)
    absent = [window for window in blocked if window not in state]
    if absent:
        raise IntegrationStoreError(
            "RATE_BUDGET_WINDOWS_MISSING",
            f"Connection {connection_id} has no {absent} budget row for this "
            f"window, so nothing was reserved. Both windows must exist "
            f"together: a connection throttled per-minute and unlimited "
            f"per-day is exactly the failure section 11.6 exists to prevent, and it "
            f"is the one that goes unnoticed, because everything appears to "
            f"work right up until the daily quota is gone.",
            status=500)

    # DAY first when both are exhausted. On ERP Standard the daily ceiling is
    # the binding one, and it is the one whose response differs: section 11.6 has a
    # minute exhaustion (429/44) checkpoint and resume on the next tick, NOT
    # counting toward the circuit because our own throttle failed rather than
    # the vendor, while a day exhaustion (429/45) opens the circuit until the
    # next day boundary and alerts. Naming the wrong window sends the operator
    # to the wrong response.
    window = "DAY" if "DAY" in blocked else blocked[0]
    raise RateBudgetExhausted(
        window,
        f"Connection {connection_id} has no {allocation} budget left in the "
        f"{window} window: {state[window]['used']}/{state[window]['ceiling']} "
        f"used, {count} more requested. On ERP Standard the DAILY ceiling "
        f"(2,000 calls) is the binding one, not the per-minute.")


def read_rate_budget(session: Session, *, connection_id: str,
                     allocation: str, tz: str = "UTC",
                     now: datetime | None = None,
                     scope: Scope | None = None) -> dict[str, dict[str, int]]:
    """What is left in each window, without spending anything."""
    moment = now or _utcnow()
    minute_key, day_key, _, _ = window_keys(moment, tz)
    rows = repo.query(
        session,
        f"""
        SELECT b.window_kind, b.used, b.ceiling, b.exhausted_at
        FROM {INTEGRATION_RATE_BUDGET} b
        WHERE b.connection_id = %(connection_id)s
          AND b.allocation = %(allocation)s
          AND ((b.window_kind = 'MINUTE' AND b.window_start_key = %(minute_key)s)
            OR (b.window_kind = 'DAY' AND b.window_start_key = %(day_key)s))
          AND {_via_connection('b.connection_id')}
        """,
        {"connection_id": connection_id, "allocation": allocation,
         "minute_key": minute_key, "day_key": day_key},
        scope=scope,
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return {
        r[0]: {"used": r[1], "ceiling": r[2], "remaining": r[2] - r[1],
               "exhausted_at": r[3]}
        for r in rows
    }


# ============================================================== circuit
def read_circuit(session: Session, *, connection_id: str,
                 module: str) -> dict | None:
    row = repo.query_one(
        session,
        f"""
        SELECT x.state, x.consecutive_counted_failures, x.opened_at,
               x.opened_reason, x.next_probe_at, x.last_probe_at
        FROM {INTEGRATION_CIRCUIT} x
        WHERE x.connection_id = %(connection_id)s
          AND x.module = %(module)s
          AND {_via_connection('x.connection_id')}
        """,
        {"connection_id": connection_id, "module": module},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        return None
    return {"state": row[0], "consecutive_counted_failures": row[1],
            "opened_at": row[2], "opened_reason": row[3],
            "next_probe_at": row[4], "last_probe_at": row[5]}


def record_circuit_failure(session: Session, *, connection_id: str,
                           module: str, counts_toward_circuit: bool,
                           reason: str, open_until: datetime | None = None,
                           threshold: int = 5,
                           now: datetime | None = None) -> str:
    """Record one failure and return the circuit's state afterwards.

    `counts_toward_circuit` is NOT a convenience flag. section 11.6's table is
    explicit that a 429/44 (per-minute) and a 429/45 (daily quota) do NOT count
    -- our own throttle failed, or our own budget ran out, and neither is a
    vendor fault -- while a 5xx, a timeout and a 401 do. A caller that passes
    True for a 429 makes the breaker open every afternoon at the same time and
    the cause looks like the vendor.

    A non-counting failure that still needs the circuit held (429/45: "open
    until the next day boundary") passes `counts_toward_circuit=False` WITH an
    `open_until`, which opens the circuit without touching the consecutive
    count.
    """
    moment = now or _utcnow()
    forced_open = open_until is not None
    row = repo.query_one(
        session,
        f"""
        INSERT INTO {INTEGRATION_CIRCUIT} (
            connection_id, module, state, consecutive_counted_failures,
            first_failure_at, opened_at, opened_reason, next_probe_at,
            updated_at)
        SELECT c.connection_id, %(module)s,
               CASE WHEN %(forced_open)s THEN 'CIRCUIT_OPEN'
                    WHEN %(counts)s AND 1 >= %(threshold)s THEN 'CIRCUIT_OPEN'
                    ELSE 'CIRCUIT_CLOSED' END,
               CASE WHEN %(counts)s THEN 1 ELSE 0 END,
               CASE WHEN %(counts)s THEN %(now)s ELSE NULL END,
               CASE WHEN %(forced_open)s
                      OR (%(counts)s AND 1 >= %(threshold)s)
                    THEN %(now)s ELSE NULL END,
               CASE WHEN %(forced_open)s
                      OR (%(counts)s AND 1 >= %(threshold)s)
                    THEN %(reason)s ELSE NULL END,
               CASE WHEN %(forced_open)s THEN %(open_until)s
                    WHEN %(counts)s AND 1 >= %(threshold)s
                    THEN %(now)s + make_interval(secs => 60)
                    ELSE NULL END,
               %(now)s
        FROM {INTEGRATION_CONNECTION} c
        WHERE c.connection_id = %(connection_id)s AND {{scope}}
        ON CONFLICT (connection_id, module) DO UPDATE SET
            consecutive_counted_failures =
                {INTEGRATION_CIRCUIT}.consecutive_counted_failures
                + CASE WHEN %(counts)s THEN 1 ELSE 0 END,
            first_failure_at = CASE
                WHEN %(counts)s
                THEN coalesce({INTEGRATION_CIRCUIT}.first_failure_at, %(now)s)
                ELSE {INTEGRATION_CIRCUIT}.first_failure_at END,
            state = CASE
                WHEN %(forced_open)s THEN 'CIRCUIT_OPEN'
                WHEN %(counts)s
                     AND {INTEGRATION_CIRCUIT}.consecutive_counted_failures + 1
                         >= %(threshold)s
                THEN 'CIRCUIT_OPEN'
                ELSE {INTEGRATION_CIRCUIT}.state END,
            opened_at = CASE
                WHEN %(forced_open)s
                     OR (%(counts)s
                         AND {INTEGRATION_CIRCUIT}.consecutive_counted_failures + 1
                             >= %(threshold)s)
                THEN coalesce({INTEGRATION_CIRCUIT}.opened_at, %(now)s)
                ELSE {INTEGRATION_CIRCUIT}.opened_at END,
            opened_reason = CASE
                WHEN %(forced_open)s
                     OR (%(counts)s
                         AND {INTEGRATION_CIRCUIT}.consecutive_counted_failures + 1
                             >= %(threshold)s)
                THEN %(reason)s
                ELSE {INTEGRATION_CIRCUIT}.opened_reason END,
            next_probe_at = CASE
                WHEN %(forced_open)s THEN %(open_until)s
                WHEN %(counts)s
                     AND {INTEGRATION_CIRCUIT}.consecutive_counted_failures + 1
                         >= %(threshold)s
                THEN %(now)s + make_interval(secs => 60)
                ELSE {INTEGRATION_CIRCUIT}.next_probe_at END,
            updated_at = %(now)s
        RETURNING state
        """,
        {"connection_id": connection_id, "module": module,
         "counts": counts_toward_circuit, "forced_open": forced_open,
         "open_until": open_until, "threshold": threshold,
         "reason": _require(reason, code="BLANK_CIRCUIT_REASON",
                            what="circuit reason"),
         "now": moment},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return row[0]


def record_circuit_success(session: Session, *, connection_id: str,
                           module: str, now: datetime | None = None) -> str:
    """A success closes the circuit and clears the count.

    Clearing matters: five failures spread over five days with successes
    between them are not five CONSECUTIVE failures, and a counter that only
    ever rises turns the breaker into a lifetime error budget.
    `ck_integration_circuit_closed_is_clean` refuses a CLOSED row that kept
    either the count or the opened stamp, so this is the only shape the
    transition can take.
    """
    moment = now or _utcnow()
    row = repo.query_one(
        session,
        f"""
        INSERT INTO {INTEGRATION_CIRCUIT} (
            connection_id, module, state, consecutive_counted_failures,
            last_probe_at, updated_at)
        SELECT c.connection_id, %(module)s, 'CIRCUIT_CLOSED', 0, %(now)s, %(now)s
        FROM {INTEGRATION_CONNECTION} c
        WHERE c.connection_id = %(connection_id)s AND {{scope}}
        ON CONFLICT (connection_id, module) DO UPDATE SET
            state = 'CIRCUIT_CLOSED',
            consecutive_counted_failures = 0,
            first_failure_at = NULL,
            opened_at = NULL,
            opened_reason = NULL,
            next_probe_at = NULL,
            last_probe_at = %(now)s,
            updated_at = %(now)s
        RETURNING state
        """,
        {"connection_id": connection_id, "module": module, "now": moment},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return row[0]


# ====================================================== reconciliation (§11.8)
# The other half of `sweeps.SweepStore`. Everything above this line has run
# against a real server since Wave 5 stream 2; everything below it had NO
# PostgreSQL implementation at all, and all eight sweeps ran exclusively
# against `tests/integration_fakes.py::InMemoryStore`. A fake written from the
# same reading as the code agrees with the code about everything, including
# statements the server cannot parse -- which is the defect
# `tests/test_pg_integration_rate_budget.py` was written after, one section up.
#
# TWO SILENT DROPS HAVE ALREADY BEEN FOUND IN THIS SURFACE, and both arrived
# through the anti-silent-drop code itself:
#
#   1. the unattributed bucket was INCREMENTED rather than set, so every
#      re-walk -- and the sweeps re-walk, by design, on a 300-second overlap
#      and a cycling cursor -- inflated it without a single new receive
#      arriving. The number that blocks capitalisation became fiction.
#   2. two identifierless lines on one receive keyed the SAME exception, so
#      the second line's value vanished. `sweeps._attribute` now falls back to
#      the line's ORDINAL (`#0`, `#1`) rather than to a constant, and
#      `ux_reconciliation_exception_open` keys on `object_id`, so two ordinals
#      are two rows. :func:`raise_exception` must not undo that by keying on
#      anything coarser.
#
# Neither is reintroducible without failing a test in
# `tests/test_pg_reconciliation.py`, which asserts both directly.


def _exception_id() -> str:
    """A fresh `exception_id`. Minted here because the `SweepStore` protocol
    does not take one -- the caller is a cron sweep with nothing to name a row
    after."""
    return f"EXC-{uuid.uuid4().hex[:12].upper()}"


def _magnitude(paise: int | None, *, side: str) -> int | None:
    """A discrepancy side as the non-negative magnitude 011 requires.

    `ck_reconciliation_exception_paise` forbids a negative on either side, and
    says why: "these are magnitudes of two sides, and a sign would silently
    encode a direction the `kind` is supposed to carry."

    A negative genuinely arrives. `dto.paise()` passes
    ``allow_negative=True`` because credit notes, returns and reversals are
    ordinary documents, so a receive line on a return carries a negative
    `line_total_paise`, and `sweeps._attribute` hands it straight through.
    Rejecting it would stop the GRN sweep on a legitimate document; storing it
    would violate the CHECK at 3am inside a cron function.

    So the column takes the magnitude -- which IS the full value §11.8 demands
    the line be held at -- and **the signed original is written to the audit
    event**, so the direction is recovered from the trail rather than lost.
    Nothing is dropped and nothing is spread.
    """
    if paise is None:
        return None
    value = int(paise)
    if value < 0:
        return -value
    return value


def raise_exception(session: Session, *, kind: str, object_type: str,
                    object_id: str | None, detail: str,
                    raised_at: datetime | None = None,
                    entity_id: str | None = None,
                    project_id: str | None = None,
                    local_paise: int | None = None,
                    source_paise: int | None = None,
                    correlation_id: str | None = None,
                    actor: str = "SVC-SWEEP") -> str:
    """Raise a reconciliation exception, or return the Open one already there.

    Returns the `exception_id` either way, which is the contract
    `sweeps.SweepStore` declares and which
    `sweeps.SweepPurchaseOrderAnchored._attribute` depends on: it passes the
    returned id straight to `accumulate_unattributed` as the bucket's
    `source_key`, so a re-walk that gets the same id back writes the same
    bucket entry rather than a second one.

    IDEMPOTENCY IS THE INDEX, NOT A READ
    ------------------------------------
    `ux_reconciliation_exception_open` is UNIQUE on
    ``(kind, object_type, object_id)`` and PARTIAL on
    ``status = 'Open' AND object_id IS NOT NULL``. This statement infers it by
    naming both the columns and the predicate -- inference by columns alone
    matches no index on this table, and PostgreSQL refuses rather than
    choosing another, which is the good failure but only if the predicate is
    written out.

    ``DO UPDATE SET detail = <the row's own detail>`` rather than
    ``DO NOTHING``: a no-op assignment changes no value but makes the
    statement RETURN the conflicting row, so the existing `exception_id` comes
    back from the same statement that tried to insert. `DO NOTHING` returns no
    row, and recovering the id would then need a follow-up SELECT -- a
    read-after-write with a window a concurrent sweep can land in. There is no
    read-then-write here at all: the index arbitrates, once.

    Whether this call created the row is decided by comparing the returned id
    with the one this call minted. That is exact. The `xmax = 0` trick would
    also work and is not used, because it is an implementation detail of
    PostgreSQL's tuple header and this is money.

    WHEN `object_id` IS NULL the partial index does not cover the row, so no
    conflict is possible and every call inserts. That is 011's design, not an
    oversight: an exception with nothing to key on cannot be recognised as
    "the same one again", and inventing a key for it is precisely how two
    identifierless GRN lines collapsed into one exception and half the value
    disappeared. Callers that can supply an ordinal must
    (`sweeps._attribute` does).
    """
    if kind not in EXCEPTION_KINDS:
        raise IntegrationStoreError(
            "UNKNOWN_EXCEPTION_KIND",
            f"{kind!r} is not one of the kinds C18 declares and "
            f"ck_reconciliation_exception_kind permits ({', '.join(EXCEPTION_KINDS)}). "
            f"A new kind is a deliberate contract change -- a migration, the "
            f"registry and this tuple together, as 030 did -- not a typo that "
            f"silently creates a category nobody triages.")
    moment = raised_at or _utcnow()
    candidate = _exception_id()
    object_type = _require(object_type, code="BLANK_OBJECT_TYPE",
                           what="object_type")
    detail = _require(detail, code="BLANK_EXCEPTION_DETAIL", what="detail")
    local_magnitude = _magnitude(local_paise, side="local")
    source_magnitude = _magnitude(source_paise, side="source")
    row = repo.query_one(
        session,
        f"""
        INSERT INTO {RECONCILIATION_EXCEPTION} (
            exception_id, kind, object_type, object_id, entity_id, project_id,
            status, detail, local_paise, source_paise, correlation_id,
            raised_at)
        SELECT %(exception_id)s, %(kind)s, %(object_type)s, %(object_id)s,
               %(entity_id)s, %(project_id)s, %(open)s, %(detail)s,
               %(local_paise)s, %(source_paise)s, %(correlation_id)s,
               %(raised_at)s
        WHERE %(entity_id)s::text IS NULL
           OR EXISTS (SELECT 1 FROM entity en
                       WHERE en.entity_id = %(entity_id)s AND {{scope}})
        ON CONFLICT (kind, object_type, object_id)
            WHERE status = 'Open' AND object_id IS NOT NULL
        DO UPDATE SET detail = {RECONCILIATION_EXCEPTION}.detail
        RETURNING exception_id
        """,
        {"exception_id": candidate, "kind": kind, "object_type": object_type,
         "object_id": object_id, "detail": detail, "entity_id": entity_id,
         "project_id": project_id, "local_paise": local_magnitude,
         "source_paise": source_magnitude, "correlation_id": correlation_id,
         "raised_at": moment, "open": EXCEPTION_OPEN},
        columns=EXCEPTION_WRITE_SCOPE_COLUMNS,
    )
    if row is None:
        # The INSERT's `WHERE` refused: this principal may not write into
        # `entity_id`. RAISED, never returned as a fabricated id -- a caller
        # that got an id back for a row that does not exist would hand it to
        # `accumulate_unattributed` as a bucket key, and the value it was
        # holding would be attributed to nothing at all. Which is the silent
        # drop, one layer down.
        raise IntegrationStoreError(
            "EXCEPTION_OUT_OF_SCOPE",
            f"Cannot raise a {kind} exception against entity "
            f"{entity_id!r}: it does not exist or is out of scope for this "
            f"principal. Nothing was written and no exception id exists.",
            status=403)
    exception_id = row[0]
    created = exception_id == candidate
    record_event(
        session,
        kind=("reconciliation.exception.raised" if created
              else "reconciliation.exception.rewalked"),
        actor=actor, correlation_id=correlation_id, now=moment,
        detail={
            "exception_id": exception_id, "exception_kind": kind,
            "object_type": object_type, "object_id": object_id,
            "entity_id": entity_id, "project_id": project_id,
            "detail": detail,
            "status": EXCEPTION_OPEN,
            "local_paise": local_magnitude,
            "source_paise": source_magnitude,
            # The signed originals, so a magnitude in the column never costs
            # the direction. See `_magnitude`.
            "local_paise_signed": None if local_paise is None else int(local_paise),
            "source_paise_signed": None if source_paise is None else int(source_paise),
            "created": created,
        })
    return exception_id


def open_exceptions(session: Session, *, entity_id: str | None = None,
                    project_id: str | None = None,
                    kinds: Sequence[str] | None = None,
                    limit: int = 500) -> list[dict]:
    """Every Open exception in scope, newest first. SCR-16 and SCR-27's list.

    THE `OR` IS NOT A HOLE. 011 makes `entity_id` and `project_id` nullable and
    says why: "some exceptions are raised before the owning entity or project
    is known -- an unsanctioned commitment discovered on a PO we have no local
    record of... an exception nobody can attribute must be visible to whoever
    can resolve it." Its RLS policy delivers that by passing the NULLs to
    `capex_scope_permits`, which treats a NULL dimension as
    unrestricted-by-that-dimension.

    `repo.compile_scope` cannot express that: it emits ``col = ANY(array)``,
    and ``NULL = ANY(...)`` is NULL, which is falsy. So a wholly unattributable
    exception -- both dimensions NULL -- would be invisible to every restricted
    principal, and the row that most needs a human would be the one nobody
    could see. The disjunction restores exactly that case and nothing wider:
    BOTH dimensions must be NULL, so a row attributed to an entity is still
    filtered by entity.

    RESIDUAL, STATED RATHER THAN HIDDEN: a principal restricted on `project`
    does not see exceptions whose `project_id` is NULL but whose `entity_id`
    is set -- an entity-level CONTROL_TOTAL_MISMATCH, typically. Server-side
    RLS would show them. This layer is therefore STRICTER than RLS, never
    looser, which is the safe direction for a read; and the period-close gate
    does not come through here (`pg/periods.py` asks the table directly, under
    its own documented scope exemption), so nothing that blocks a close can be
    hidden by it.

    TWO DIFFERENT MEANINGS OF NULL SIT IN THIS ONE `WHERE`, and confusing them
    is easy, so they are named. ``%(entity_id)s::text IS NULL`` is the CALLER
    passing no filter -- one fixed statement rather than a WHERE clause
    assembled from a list, which is what lets
    ``tests/test_integration_store.py`` render this SQL and check every
    placeholder against its parameter without a database. ``x.entity_id IS
    NULL`` in the last clause is the ROW having no entity to be filtered by.
    The first is about the query; the second is about the data.
    """
    rows = repo.query(
        session,
        f"""
        SELECT x.exception_id, x.kind, x.object_type, x.object_id,
               x.entity_id, x.project_id, x.status, x.detail,
               x.local_paise, x.source_paise, x.correlation_id, x.raised_at,
               x.resolved_at, x.resolved_by, x.resolution_note
        FROM {RECONCILIATION_EXCEPTION} x
        WHERE x.status = %(open)s
          AND (%(entity_id)s::text IS NULL OR x.entity_id = %(entity_id)s)
          AND (%(project_id)s::text IS NULL OR x.project_id = %(project_id)s)
          AND (%(kinds)s::text[] IS NULL OR x.kind = ANY(%(kinds)s))
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        ORDER BY x.raised_at DESC, x.exception_id
        LIMIT %(limit)s
        """,
        {"open": EXCEPTION_OPEN, "limit": int(limit), "entity_id": entity_id,
         "project_id": project_id, "kinds": _checked_kinds(kinds)},
        columns=EXCEPTION_SCOPE_COLUMNS,
    )
    return [_exception_row(r) for r in rows]


def _checked_kinds(kinds: Sequence[str] | None) -> list[str] | None:
    """`kinds` as a list, or None -- refusing anything outside the frozen five.

    An unknown kind here filters to nothing and reads on screen as "there are
    no exceptions of that kind", which is the wrong answer to give about a
    control. Refused instead.
    """
    if kinds is None:
        return None
    unknown = sorted(set(kinds) - set(EXCEPTION_KINDS))
    if unknown:
        raise IntegrationStoreError(
            "UNKNOWN_EXCEPTION_KIND",
            f"{unknown} are not reconciliation exception kinds; expected some "
            f"of {list(EXCEPTION_KINDS)}.")
    return list(kinds)


def _exception_row(row: tuple) -> dict:
    (exception_id, kind, object_type, object_id, entity_id, project_id, status,
     detail, local_paise, source_paise, correlation_id, raised_at, resolved_at,
     resolved_by, resolution_note) = row
    return {
        "exception_id": exception_id, "kind": kind, "object_type": object_type,
        "object_id": object_id, "entity_id": entity_id,
        "project_id": project_id, "status": status, "detail": detail,
        # `int()`, not the driver's word for it. These are `bigint` columns and
        # psycopg returns ints for them today; the cast is here so a future
        # expression that wraps one in `SUM()` cannot leak a Decimal past this
        # boundary the way `check_availability` once did.
        "local_paise": None if local_paise is None else int(local_paise),
        "source_paise": None if source_paise is None else int(source_paise),
        "correlation_id": correlation_id, "raised_at": raised_at,
        "resolved_at": resolved_at, "resolved_by": resolved_by,
        "resolution_note": resolution_note,
    }


def open_exception_exposure(session: Session, *, entity_id: str | None = None,
                            project_id: str | None = None) -> dict:
    """How many Open exceptions are in scope, and how much money they hold.

    The number the capitalisation gate and the period-close report quote. It
    is the sum of `source_paise` held at FULL value -- §11.8 forbids spreading
    an unattributed line pro-rata, so this is an exposure, not an allocation,
    and it is deliberately not netted against anything.

    ``::bigint`` ON BOTH SUMS, and not decoration. PostgreSQL's ``SUM()`` over
    a `bigint` column returns **numeric**, psycopg maps numeric to
    `decimal.Decimal`, and a Decimal that reaches arithmetic expecting an int
    is the defect that took down the availability verdict, both approval paths
    and the concurrency proof -- in the PostgreSQL CI job only, because a
    Decimal cannot appear without a real server.
    `tests/test_money_sql_discipline.py` fails the build if either cast is
    removed.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT count(*),
               coalesce(SUM(x.source_paise), 0)::bigint,
               coalesce(SUM(x.local_paise), 0)::bigint
        FROM {RECONCILIATION_EXCEPTION} x
        WHERE x.status = %(open)s
          AND (%(entity_id)s::text IS NULL OR x.entity_id = %(entity_id)s)
          AND (%(project_id)s::text IS NULL OR x.project_id = %(project_id)s)
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        """,
        {"open": EXCEPTION_OPEN, "entity_id": entity_id,
         "project_id": project_id},
        columns=EXCEPTION_SCOPE_COLUMNS,
    )
    count, source_total, local_total = row if row is not None else (0, 0, 0)
    return {"open_count": int(count),
            "source_paise": int(source_total),
            "local_paise": int(local_total)}


def act_on_exception(session: Session, *, exception_id: str, action: str,
                     actor: str, reason: str,
                     now: datetime | None = None) -> dict:
    """Resolve / retry / ignore / write off one Open exception. Returns the row.

    A RESOLUTION NAMES WHO AND WHEN, OR IT IS NOT A RESOLUTION.
    `ck_reconciliation_exception_resolution` is a biconditional: a row whose
    status is not Open MUST carry both `resolved_at` and `resolved_by`, and a
    row that is Open must carry neither. This statement sets all four
    resolution columns in one UPDATE so the constraint is satisfied by the
    same statement that leaves Open -- there is no intermediate state, and no
    path that stamps a status without an actor.

    THE REASON IS MANDATORY, and checked here rather than left to the column:
    `resolution_note` is nullable in 011, so the database would accept a
    resolution with no explanation. An exception closed with no reason is
    indistinguishable from one closed by accident, which defeats the audit
    trail the whole control rests on. Blank and whitespace-only are both
    refused.

    ONLY FROM `Open`. A second call naming a different actor and a different
    reason is refused rather than silently overwriting the first -- the first
    reviewer's decision is evidence, not a draft.

    `retry` lands on `Resolved` and is the interesting one: it does not re-run
    anything. It closes the row so the NEXT sweep can raise it again if the
    condition is still there, which works only because
    `ux_reconciliation_exception_open` is partial on `status = 'Open'`. 011
    says so in its own header: "once an exception is resolved, the same
    condition recurring is a genuinely new exception and must be raisable
    again."
    """
    if action not in EXCEPTION_ACTIONS:
        raise IntegrationStoreError(
            "UNKNOWN_EXCEPTION_ACTION",
            f"{action!r} is not a reconciliation action; expected one of "
            f"{sorted(EXCEPTION_ACTIONS)}.")
    status = EXCEPTION_ACTIONS[action]
    moment = now or _utcnow()
    exception_id = _require(exception_id, code="BLANK_EXCEPTION_ID",
                            what="exception_id")
    resolved_by = _require(actor, code="BLANK_EXCEPTION_ACTOR", what="actor")
    note = _require(
        reason, code="BLANK_EXCEPTION_REASON",
        what=f"a reason for {action!r} on a reconciliation exception")
    row = repo.query_one(
        session,
        f"""
        UPDATE {RECONCILIATION_EXCEPTION} AS x
        SET status = %(status)s,
            resolved_at = %(now)s,
            resolved_by = %(resolved_by)s,
            resolution_note = %(reason)s
        WHERE x.exception_id = %(exception_id)s
          AND x.status = %(open)s
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        RETURNING x.exception_id, x.kind, x.object_type, x.object_id,
                  x.entity_id, x.project_id, x.status, x.detail,
                  x.local_paise, x.source_paise, x.correlation_id, x.raised_at,
                  x.resolved_at, x.resolved_by, x.resolution_note
        """,
        {"exception_id": exception_id, "status": status,
         "resolved_by": resolved_by, "reason": note, "now": moment,
         "open": EXCEPTION_OPEN},
        columns=EXCEPTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise _no_open_exception(session, exception_id, action)
    result = _exception_row(row)
    record_event(
        session, kind=f"reconciliation.exception.{action}", actor=actor,
        correlation_id=result["correlation_id"], now=moment,
        detail={"exception_id": result["exception_id"],
                "exception_kind": result["kind"],
                "object_type": result["object_type"],
                "object_id": result["object_id"],
                "entity_id": result["entity_id"],
                "project_id": result["project_id"],
                "action": action, "status": status,
                "reason": note,
                "source_paise": result["source_paise"],
                "local_paise": result["local_paise"]})
    return result


def _no_open_exception(session: Session, exception_id: str,
                       action: str) -> IntegrationStoreError:
    """Say WHICH of the three refusals happened, without widening any of them.

    Diagnostic only, and it runs AFTER the UPDATE has already declined to
    touch a row -- so it cannot change the outcome, only the message. The
    alternative, one message covering "no such row", "not yours" and "already
    resolved", is the message that sends a reviewer to the wrong place.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT x.status, x.resolved_by, x.resolved_at
        FROM {RECONCILIATION_EXCEPTION} x
        WHERE x.exception_id = %(exception_id)s
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        """,
        {"exception_id": exception_id},
        columns=EXCEPTION_SCOPE_COLUMNS,
    )
    if row is None:
        return IntegrationStoreError(
            "EXCEPTION_NOT_FOUND",
            f"Reconciliation exception {exception_id} does not exist or is "
            f"out of scope.", status=404)
    return IntegrationStoreError(
        "EXCEPTION_NOT_OPEN",
        f"Reconciliation exception {exception_id} is already {row[0]}, "
        f"resolved by {row[1]} at {row[2]}. It cannot be {action}d again: the "
        f"first reviewer's decision is evidence, not a draft.",
        status=409)


# ================================================ commitment against actual (013)
#
# THE ONE ARITHMETIC THIS SECTION MUST NOT GET WRONG.
#
# Open commitment is ORDERED LESS BILLED, floored at zero, and zero outright
# once the purchase order has reached a commitment-releasing state. It is NOT
# ordered less RECEIVED. A receipt does not release a commitment; a bill does.
#
# Received-not-billed is its OWN quantity -- received less billed, floored at
# zero -- and is never subtracted from commitment and never added to it. The two
# overlap by design.
#
# Both rules are `domain.compute_ledger` / `domain.reconciliation`, which are
# the frozen `C5_formulas.json` registry in code. Everything below is
# TRANSCRIBED from them rather than re-derived, and the two states lists are
# transcribed as constants at the head of this module for the same reason: a
# second derivation that disagrees would make PostgreSQL and the SQLite ledger
# quote different open commitment for the same estate, silently.
#
# WHERE THE ARITHMETIC HAPPENS. The three per-line sums are done by the server
# in `bigint`; the four derived figures are done here in Python `int`. Neither
# is float, and neither is `numeric` past this boundary: every `SUM()` over a
# `bigint` column is cast back with `::bigint`, because PostgreSQL's SUM returns
# numeric, psycopg maps numeric to `Decimal`, and a Decimal reaching integer
# arithmetic is the defect that took down the availability verdict and both
# approval paths in the PostgreSQL CI job only.


def _reconciliation_position(ordered: int, received: int, billed: int,
                             released: bool, po_status: str) -> tuple[str, str]:
    """`(flag, position)` for one line -- transcribed from `domain.reconciliation`.

    Character for character in the sentences, because SCR-18 renders `position`
    verbatim and a reader comparing the PostgreSQL screen with the SQLite one
    must not find two different words for the same state.

    THE FLAG PRECEDENCE IS `domain`'s, AND IT IS NOT THE OBVIOUS ONE.
    `received-unbilled` outranks `released`, not the other way round. A closed
    purchase order that still holds value received and never invoiced is
    reported as `received-unbilled`, because that is the condition somebody has
    to act on -- the receipt is real, the invoice is missing, and the closure
    does not make either untrue. Ranking `released` first would file it under
    "nothing to see here" and it would leave the exception list.

    `over-billed` outranks everything, on a released line too: money billed
    beyond the order is the condition that raises an exception.
    """
    if released:
        if po_status == "Cancelled":
            position = ("PO cancelled before billing" if billed == 0
                        else "PO cancelled after partial billing")
        else:
            position = "PO closed - residual released"
    elif billed == 0:
        position = "PO approved, not billed"
    elif billed < ordered:
        position = "Partially billed"
    elif billed == ordered:
        position = "Fully billed"
    else:
        position = "Bill exceeds PO"

    # The exact precedence in `domain.reconciliation`, in the same order.
    flag = ("over-billed" if billed > ordered else
            "received-unbilled" if received > billed else
            "released" if released else
            "ok")
    return flag, position


def reconciliation_lines(session: Session, *, project_id: str | None = None,
                         limit: int = 500) -> list[dict]:
    """Every purchase-order line in scope, with ordered / received / billed.

    THE `sweeps.SweepStore`-SIDE NAME. The statement itself lives in
    `pg/procurement.py`, next to the receive and bill writers, and this
    delegates to it -- the same split `resolve_po_line` and
    `record_receive_line` use, and for the same reason: `po_line`, `grn_line`
    and `bill_line` are money-bearing LEDGER rows, and
    `test_money_in_this_module_appears_only_on_the_011_exception_table` holds
    THIS module to transport, with `reconciliation_exception` the single
    exception 011 forced. Moving the statement across the line keeps that guard
    at its original width; widening the guard instead would have relaxed the
    one check standing between an amount and an outbox row.

    The arithmetic the statement feeds is still this module's, and stays here:
    :func:`_reconciliation_position` is the transcription of
    `domain.reconciliation`, and :func:`reconciliation_summary` sums the rows
    that were actually returned. Both are pure functions over integers and
    neither touches SQL.
    """
    from . import procurement
    return procurement.reconciliation_lines(
        session, project_id=project_id, limit=limit)


def reconciliation_summary(lines: Sequence[Mapping[str, Any]]) -> dict:
    """The control-total band, summed over the SAME rows that were returned.

    A pure function of `lines`, and that is the point: the summary and the
    table can never disagree, because there is only one row set. A second query
    that re-aggregated server-side would be a different question asked at a
    different instant, and the tile would quietly stop being the total of what
    is on screen.

    `identity_balanced` is asserted here rather than assumed. If integer
    arithmetic over these five columns ever stops reconciling to the paisa the
    response says so on its face, instead of the discrepancy being discovered
    by whoever signs the number off.
    """
    def total(key: str) -> int:
        return sum(int(line[key]) for line in lines)

    ordered = total("ordered_paise")
    billed = total("billed_paise")
    open_commitment = total("open_commitment_paise")
    residual_released = total("residual_released_paise")
    over_billed = total("over_billed_paise")
    residual = (ordered - billed) - (open_commitment + residual_released - over_billed)

    return {
        "lines": len(lines),
        "ordered_paise": ordered,
        "received_paise": total("received_paise"),
        "billed_paise": billed,
        "open_commitment_paise": open_commitment,
        "received_not_billed_paise": total("received_not_billed_paise"),
        "exposure_paise": total("exposure_paise"),
        "residual_released_paise": residual_released,
        "over_billed_paise": over_billed,
        # The reconciliation, stated so it can be read rather than trusted.
        "identity": "ordered - billed = open_commitment + residual_released "
                    "- over_billed",
        "identity_residual_paise": residual,
        "identity_balanced": residual == 0,
        "exceptions": [
            {"po_number": line["po_number"], "line_no": line["line_no"],
             "flag": line["flag"]}
            for line in lines
            if line["flag"] in ("over-billed", "received-unbilled")
        ],
    }


# ============================================ the rest of `sweeps.SweepStore`
#
# `resolve_po_line`, `record_receive_line`, `accumulate_unattributed`,
# `bills_awaiting_detail` and `mark_detail_hydrated` USED TO RAISE
# `SchemaNotYetMigrated`, because every `CREATE TABLE` in `migrations/pg/` had
# been enumerated and there was no purchase-order header, no purchase-order
# line and no receive/GRN line among them. `013_procurement.sql` creates all
# eight, so four of the five now have the table they named, and the fifth --
# the unattributed bucket -- is answered below by the table that was already
# holding the number.
#
# WHAT DID **NOT** CHANGE is the rule the refusals existed to protect. Each of
# these still has a "harmless" default that is not harmless, and none of them
# returns one:
#
#   * `resolve_po_line` -> None is a real answer and only a real answer: the
#     line names no PO line we hold, so `sweeps._attribute` quarantines it at
#     full value. It is NOT the answer to "two purchase orders share this
#     external id" -- that is ambiguity, not absence, and it raises.
#   * `record_receive_line` refuses a `po_line_id` it cannot resolve under the
#     caller's scope rather than writing a receipt against nothing.
#   * `accumulate_unattributed` refuses a `source_key` naming no Open
#     exception. A bucket entry keyed on a row that does not exist is the
#     silent drop, one layer down.
#   * `bills_awaiting_detail` distinguishes "queue empty" from "connection out
#     of scope"; the second raises. A poller reading the second as the first
#     would fetch nothing and report success for as long as anyone relied on
#     it.
#
# THE UNATTRIBUTED BUCKET IS `reconciliation_exception`, NOT A NINTH TABLE.
#
# The shape the refusal asked for was "PRIMARY KEY (project_id, source_key),
# the value SET and never incremented". `reconciliation_exception` already is
# that: `source_key` IS `exception_id`, which is the table's PRIMARY KEY, so
# there is exactly one row per key; `project_id` is a column on that row; the
# value is `source_paise`, held at FULL value per §11.8; and the per-project
# total that blocks capitalisation is `open_exception_exposure`, which sums it.
# A ninth table carrying the same numbers would be a SECOND source of truth for
# the figure that blocks capitalisation, and the two would eventually disagree.
# `accumulate_unattributed` therefore writes `SET source_paise = %(paise)s` --
# **set, never `source_paise + %(paise)s`** -- on the row `source_key` names.

#: What still has no table. EMPTY, and deliberately kept rather than deleted:
#: it is the data `tests/test_pg_reconciliation.py` parametrises its refusal
#: tests over, so the day a sixth call arrives with no schema behind it the
#: refusal is one entry away and the tests come back on their own.
UNBACKED_SWEEP_SURFACE: dict[str, str] = {}


class SchemaNotYetMigrated(IntegrationStoreError):
    """The table this call needs is in no migration.

    A distinct type, so a caller can tell "the schema does not support this
    yet" from "this call was refused" -- and so nothing can catch it by
    accident while catching an ordinary store error.

    Retained with :data:`UNBACKED_SWEEP_SURFACE` empty. The type is the
    mechanism, not the list: deleting it would mean the next call that arrives
    ahead of its schema has to reinvent the refusal, and the reinvention is
    exactly where a plausible default gets returned instead.
    """

    def __init__(self, function: str) -> None:
        missing = UNBACKED_SWEEP_SURFACE.get(
            function, "the table this call writes to")
        super().__init__(
            "SCHEMA_NOT_YET_MIGRATED",
            f"{function}() has no table to write to: migrations/pg/ creates "
            f"no {missing}. Refusing rather than "
            f"returning a value that would read as success -- §11.8 forbids a "
            f"silent drop, and a plausible default here IS one. The lead owns "
            f"migrations/; this is the diff being requested.",
            status=501)


def resolve_po_line(session: Session, *, po_external_id: str,
                    line_external_id: str | None) -> str | None:
    """The local `po_line_id` a receive/bill line's linkage names, or `None`.

    `None` means ONE thing and is load-bearing: no purchase-order line we hold
    carries this external line id on this external purchase order, so the
    caller quarantines the line at full value (§11.8). It is not an error path
    and it is not a default -- `sweeps._attribute` has no third branch and this
    return is the second one.

    A `line_external_id` of `None` short-circuits to `None` WITHOUT a query,
    and that is not an optimisation. `ux_po_line_external` is PARTIAL on
    ``WHERE line_external_id IS NOT NULL`` precisely so that locally-raised
    lines with no external identity do not all collide on one NULL; matching a
    NULL against them would therefore be matching against rows the index
    deliberately does not police, and on ERP -- where receive lines routinely
    carry no line identifier at all -- it would attribute the whole population
    to whichever local line happened to be NULL first.

    AMBIGUITY RAISES, IT DOES NOT QUARANTINE. `ux_po_external` is UNIQUE on
    ``(external_source, external_id)``, so one external PO id CAN legitimately
    appear twice under two different sources. Two candidate lines is not
    "unresolved" -- it is "resolved, twice, differently" -- and returning
    `None` would file it as an absence, sending a line to triage with a detail
    that says the linkage is unknown when in fact it is over-known. Refused
    with a code instead, so the sweep stops at the row it cannot honour.
    """
    from . import procurement
    return procurement.resolve_po_line(
        session, po_external_id=po_external_id,
        line_external_id=line_external_id)


def record_receive_line(session: Session, *, po_line_id: str,
                        receive_external_id: str,
                        line_external_id: str | None, quantity: Any,
                        amount_paise: int | None,
                        external_source: str | None = None,
                        receive_number: str | None = None,
                        received_at: datetime | None = None,
                        external_last_modified: datetime | None = None,
                        payload_sha: str | None = None,
                        is_reversal: bool = False,
                        actor: str = "SVC-SWEEP",
                        now: datetime | None = None) -> None:
    """Mirror one attributed receive line, header included, idempotently.

    The five keyword arguments `sweeps.SweepStore` declares are the contract;
    everything after `amount_paise` is provenance the caller supplies when it
    has it. They are optional because the protocol is frozen and a required
    argument here would break every existing caller -- but a GRN mirrored
    without `external_source` and `payload_sha` is a row whose source document
    cannot be recovered, so `sweeps._attribute` passes them.

    SIGNED, both columns. `grn_line.quantity` and `grn_line.amount_paise` carry
    no `>= 0` CHECK, on purpose: a return, a reversal and a credit note are
    ordinary documents and the POC's own seed contains a receive line at
    ``-0.2 / -1,20,000``. Nothing here takes an absolute value, and nothing
    here renders paise -- `divmod` FLOORS, which is how ``-150`` once reached
    the wire as ``-2.50``, and the only rendering in the package is on the way
    OUT, in the adapters.

    REPLAY IS `ux_grn_line_external`. The sweeps re-walk by design -- a
    300-second overlap and a cycling cursor -- so the same receive line arrives
    again and must not become a second receipt. The `ON CONFLICT` names the
    constraint's three columns exactly, and the constraint is a table
    ``UNIQUE`` with no predicate, so there is nothing further to name.

    NULLS ARE DISTINCT in that constraint, which is what 013 wants for a
    locally-raised line carrying neither external id -- and which means a line
    with a `receive_external_id` but no `line_external_id` would NOT conflict
    and WOULD duplicate on replay. In practice `resolve_po_line` returns `None`
    for a null line id, so no such line ever reaches here through the sweep;
    a caller that supplies one directly gets an explicit update-then-insert
    instead, whose read-then-write window is documented rather than hidden. The
    single cron worker per connection (§2.2) is what makes that window safe,
    not luck.

    The SQL is in `pg/procurement.py`, not here: `grn_line` is a money-bearing
    ledger row and `test_integration_store.py` holds this module to transport
    only. This is the `sweeps.SweepStore` surface; that is the ledger.
    """
    from . import procurement
    procurement.record_receive_line(
        session, po_line_id=po_line_id,
        receive_external_id=receive_external_id,
        line_external_id=line_external_id, quantity=quantity,
        amount_paise=amount_paise, external_source=external_source,
        receive_number=receive_number, received_at=received_at,
        external_last_modified=external_last_modified,
        payload_sha=payload_sha, is_reversal=is_reversal, actor=actor,
        now=now)


def accumulate_unattributed(session: Session, *, project_id: str | None,
                            paise: int, source_key: str) -> None:
    """Hold one unattributed line's FULL value in the project's bucket, ONCE.

    THE BUCKET IS `reconciliation_exception` and `source_key` is its PRIMARY
    KEY. See the section header for why that is the bucket rather than a ninth
    table: it is already the row `open_exception_exposure` sums, and a second
    table carrying the same figure would eventually disagree with the one that
    blocks capitalisation.

    **SET, NEVER `+=`.** The statement is `SET source_paise = %(paise)s`. A
    sweep resumed from a checkpoint re-reads purchase orders it has already
    walked -- that is the whole point of the 300-second overlap and of the
    cycling walk -- so a bucket that added on every pass would climb every
    fifteen minutes without a single new receive arriving, and the number that
    blocks capitalisation would be fiction. That defect has been found here
    once already; `tests/test_pg_reconciliation.py` asserts against it
    directly.

    THE MAGNITUDE, NOT THE SIGN. `ck_reconciliation_exception_paise` forbids a
    negative and says why. A return or a credit note genuinely arrives here
    negative, so the column takes `_magnitude()` -- which IS the full value
    §11.8 requires the line be held at -- and the signed original goes to the
    audit event, where the direction is recovered from the trail rather than
    lost. Identical treatment to :func:`raise_exception`, on purpose: the two
    write the same column and must not disagree about its sign.
    """
    source_key = _require(source_key, code="BLANK_SOURCE_KEY",
                          what="source_key")
    if paise is None:
        # `_magnitude` maps None to None, which would NULL the bucket -- and a
        # NULL `source_paise` is summed as nothing by `open_exception_exposure`,
        # so the exception would stay Open while holding no value at all. An
        # exception blocking capitalisation for zero rupees is not a smaller
        # version of the control; it is the control reporting a figure that
        # cannot be reconciled against the source.
        raise IntegrationStoreError(
            "UNATTRIBUTED_PAISE_MISSING",
            f"accumulate_unattributed was given no amount for source_key "
            f"{source_key!r}. Nothing was written: a NULL bucket value sums as "
            f"zero, and an exception holding zero is indistinguishable from "
            f"one that was never a problem.", status=422)
    magnitude = _magnitude(paise, side="source")
    rows = repo.query(
        session,
        f"""
        UPDATE {RECONCILIATION_EXCEPTION} x
        SET source_paise = %(paise)s
        WHERE x.exception_id = %(source_key)s
          AND x.status = %(open)s
          AND (%(project_id)s::text IS NULL
               OR x.project_id IS NULL
               OR x.project_id = %(project_id)s)
          AND ({{scope}}
               OR (x.entity_id IS NULL AND x.project_id IS NULL))
        RETURNING x.exception_id, x.project_id
        """,
        {"source_key": source_key, "paise": magnitude,
         "project_id": project_id, "open": EXCEPTION_OPEN},
        columns=EXCEPTION_SCOPE_COLUMNS,
    )
    if not rows:
        raise IntegrationStoreError(
            "UNATTRIBUTED_BUCKET_KEY_UNKNOWN",
            f"source_key {source_key!r} names no Open reconciliation exception "
            f"in scope, or names one belonging to a project other than "
            f"{project_id!r}. NOTHING was accumulated. Creating a bucket entry "
            f"for a key with no exception behind it would hold the value "
            f"nowhere anybody triages -- the silent drop this function exists "
            f"to prevent.", status=404)
    record_event(
        session, kind="reconciliation.unattributed.accumulated", actor="SVC-SWEEP",
        detail={"exception_id": rows[0][0], "project_id": rows[0][1],
                "source_paise": magnitude,
                # The SIGNED original. `_magnitude` is lossy about direction by
                # design and this is where the direction survives.
                "source_paise_signed": int(paise)})


def bills_awaiting_detail(session: Session, *, connection_id: str,
                          limit: int = 50) -> list[str]:
    """The bills whose `line_items` we have not fetched, oldest receipt first.

    A list response omits `line_items` (plan §11.5) and a bill with no lines
    cannot be attributed to a WBS element at all, so every bill the poll
    accepted is queued for a `GET /bills/{id}`. The queue is derived, not
    stored -- see :data:`EVENT_BILL_DETAIL_HYDRATED` for why a flag column was
    not available -- and it is derived from two facts:

      * NO payload we hold for this external id carries a non-empty line
        array. `bool_or` over every inbox row for the id, so a bill whose LIST
        payload already carried its lines (Books can) never costs a call; and
        so the detail payload `sweep_bill_detail` writes on its way through
        removes the bill from this queue by itself.
      * and no `integration.bill.detail.hydrated` event has been recorded for
        it, which is what stops a bill that genuinely HAS no line items --
        `sweep_bill_detail` raises `CONTROL_TOTAL_MISMATCH` for exactly that --
        from being re-fetched on every sweep for ever.

    AN EMPTY LIST MEANS EMPTY, NOT UNREACHABLE. A connection that does not
    exist or is out of scope RAISES. Those two look identical to a caller that
    only counts rows, and `sweep_bill_detail` reads an empty list as "queue
    drained" and reports success -- so an out-of-scope connection returning
    `[]` would leave every bill unhydrated, meaning no lines, meaning no WBS
    attribution, meaning zero CWIP booked while the sweep runs green.
    """
    _assert_connection_visible(session, connection_id)
    rows = repo.query(
        session,
        f"""
        SELECT i.external_id
        FROM {INTEGRATION_INBOX} i
        WHERE i.connection_id = %(connection_id)s
          AND i.module = %(module)s
          AND i.state IN ('RECEIVED', 'PROCESSED')
          AND NOT EXISTS (
              SELECT 1 FROM {INTEGRATION_EVENT} e
              WHERE e.connection_id = i.connection_id
                AND e.kind = %(hydrated)s
                AND e.detail ->> 'external_id' = i.external_id)
          AND {_via_connection('i.connection_id')}
        GROUP BY i.external_id
        -- COALESCE, AND WITHOUT IT THIS QUEUE DROPPED THE ONE CASE IT EXISTS
        -- FOR. A bill whose payload carries NEITHER key makes
        -- `i.payload -> 'line_items'` SQL NULL; `jsonb_typeof(NULL) = 'array'`
        -- is NULL, so each conjunct is NULL, the OR is NULL, `bool_or` over an
        -- all-NULL input returns NULL, and `HAVING NOT NULL` is NULL --
        -- which EXCLUDES the group. A bill that HAS lines yields true and is
        -- correctly excluded; one carrying `line_items: []` yields false and is
        -- correctly included. Only the missing-key case inverted, and a bill
        -- with no line_items key at all is exactly a line-less bill.
        HAVING NOT COALESCE(bool_or(
            (jsonb_typeof(i.payload -> 'line_items') = 'array'
                AND jsonb_array_length(i.payload -> 'line_items') > 0)
            OR (jsonb_typeof(i.payload -> 'lines') = 'array'
                AND jsonb_array_length(i.payload -> 'lines') > 0)), false)
        ORDER BY min(i.received_at), i.external_id
        LIMIT %(limit)s
        """,
        {"connection_id": connection_id, "module": "bills",
         "hydrated": EVENT_BILL_DETAIL_HYDRATED, "limit": max(int(limit), 0)},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return [row[0] for row in rows]


def mark_detail_hydrated(session: Session, *, connection_id: str,
                         external_id: str,
                         actor: str = "SVC-SWEEP",
                         correlation_id: str | None = None,
                         now: datetime | None = None) -> None:
    """Record that this bill's detail fetch has been paid for. Append-only.

    Called AFTER `sweep_bill_detail` has written the detail payload to the
    inbox, so in the ordinary case the bill has already left
    :func:`bills_awaiting_detail` by carrying lines. This marker is what
    covers the case that has NO lines -- the one that raised
    `CONTROL_TOTAL_MISMATCH` -- so it is not re-fetched every fifteen minutes
    for the rest of the deployment.

    REFUSES AN EXTERNAL ID THIS CONNECTION HAS NEVER SEEN. Writing a hydration
    marker for a bill nothing received would suppress a future fetch of a bill
    we do hold, which is a silent drop with a fifteen-minute fuse.
    """
    external_id = _require(external_id, code="BLANK_EXTERNAL_ID",
                           what="external_id")
    seen = repo.query_one(
        session,
        f"""
        SELECT 1
        FROM {INTEGRATION_INBOX} i
        WHERE i.connection_id = %(connection_id)s
          AND i.module = %(module)s
          AND i.external_id = %(external_id)s
          AND {_via_connection('i.connection_id')}
        LIMIT 1
        """,
        {"connection_id": connection_id, "module": "bills",
         "external_id": external_id},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if seen is None:
        raise IntegrationStoreError(
            "BILL_NOT_IN_INBOX",
            f"Bill {external_id!r} has never been received on connection "
            f"{connection_id!r}, or the connection is out of scope. No "
            f"hydration marker was written: one for a bill nothing received "
            f"would suppress a later fetch of a bill we do hold.", status=404)
    record_event(
        session, kind=EVENT_BILL_DETAIL_HYDRATED, actor=actor,
        connection_id=connection_id, correlation_id=correlation_id,
        module="bills", detail={"external_id": external_id}, now=now)


def _assert_connection_visible(session: Session, connection_id: str) -> None:
    """Raise unless `connection_id` exists and is in the caller's scope.

    Separated so "no rows because there are none" and "no rows because the
    connection is invisible" are answered by two different statements. Folding
    the check into the queue query would make them the same empty list again.
    """
    row = repo.query_one(
        session,
        f"""
        SELECT 1 FROM {INTEGRATION_CONNECTION} c
        WHERE c.connection_id = %(connection_id)s AND {{scope}}
        """,
        {"connection_id": connection_id},
        columns=CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id!r} does not exist or is out of scope. "
            f"Reported rather than answered with an empty result, which a "
            f"caller would read as 'nothing to do'.", status=404)




# =============================================================== events
def record_event(session: Session, *, kind: str, actor: str,
                 connection_id: str | None = None,
                 correlation_id: str | None = None,
                 module: str | None = None, job_id: str | None = None,
                 inbox_id: str | None = None, outbox_id: str | None = None,
                 detail: Mapping[str, Any] | None = None,
                 now: datetime | None = None) -> int:
    """Append one event to the correlation trail. Returns the minted `event_id`.

    `event_id` is `GENERATED ALWAYS AS IDENTITY` and is therefore NOT supplied
    here -- PostgreSQL rejects an explicit value outright, and letting the
    database mint it is the right design for an append-only ledger anyway:
    there is no id to collide and no caller can choose where its row lands. It
    comes back through `RETURNING`, because returning a fabricated id would
    hand the caller an identifier for a record that does not exist. That exact
    defect was found on `approval_action` in Wave 4 and is not repeated here.

    `detail` is redacted the same way an inbound payload is. An event recording
    "bill 12345 could not be attributed" is precisely where a well-meaning
    debug field carrying the offending vendor record ends up, and
    `ck_integration_event_detail_carries_no_restricted_key` would refuse the
    row -- correctly, but at 3am, inside a cron function, in a code path whose
    whole purpose was to report a different problem.
    """
    moment = now or _utcnow()
    sanitised = redact_payload(detail)[0] if detail is not None else None
    row = repo.query_one(
        session,
        f"""
        INSERT INTO {INTEGRATION_EVENT} (
            connection_id, correlation_id, kind, module, at, job_id, inbox_id,
            outbox_id, actor, detail)
        SELECT %(connection_id)s, %(correlation_id)s, %(kind)s, %(module)s,
               %(now)s, %(job_id)s, %(inbox_id)s, %(outbox_id)s, %(actor)s,
               %(detail)s::jsonb
        WHERE %(connection_id)s::text IS NULL
           OR EXISTS (SELECT 1 FROM {INTEGRATION_CONNECTION} c
                       WHERE c.connection_id = %(connection_id)s AND {{scope}})
        RETURNING event_id
        """,
        {"connection_id": connection_id, "correlation_id": correlation_id,
         "kind": _require(kind, code="BLANK_EVENT_KIND", what="kind"),
         "module": module, "now": moment, "job_id": job_id,
         "inbox_id": inbox_id, "outbox_id": outbox_id,
         "actor": _require(actor, code="BLANK_EVENT_ACTOR", what="actor"),
         "detail": (json.dumps(sanitised, default=str)
                    if sanitised is not None else None)},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    if row is None:
        raise IntegrationStoreError(
            "CONNECTION_NOT_FOUND",
            f"Connection {connection_id} does not exist or is out of scope.",
            status=404)
    return row[0]


def trace(session: Session, correlation_id: str,
          limit: int = 500) -> list[dict]:
    """section 11.9's trace: everything recorded under one correlation id, in order.

    "One id traces a Zoho bill from HTTP response to ledger movement to audit
    entry." This is the integration segment; `audit.py` owns the other end.
    """
    rows = repo.query(
        session,
        f"""
        SELECT e.event_id, e.at, e.kind, e.module, e.connection_id, e.job_id,
               e.inbox_id, e.outbox_id, e.actor, e.detail
        FROM {INTEGRATION_EVENT} e
        WHERE e.correlation_id = %(correlation_id)s
          AND (e.connection_id IS NULL OR {_via_connection('e.connection_id')})
        ORDER BY e.at, e.event_id
        LIMIT %(limit)s
        """,
        {"correlation_id": correlation_id, "limit": limit},
        columns=VIA_CONNECTION_SCOPE_COLUMNS,
    )
    return [
        {"event_id": r[0], "at": r[1], "kind": r[2], "module": r[3],
         "connection_id": r[4], "job_id": r[5], "inbox_id": r[6],
         "outbox_id": r[7], "actor": r[8], "detail": r[9]}
        for r in rows
    ]


def integration_state_badge(outbox_state: str | None,
                            external_id: str | None) -> str | None:
    """C16's business-visible bridge: QUEUED / SENT / FAILED, or `None`.

    The registries are separated precisely so an operational status never
    renders on a business screen as though it were one of C3's frozen 21. This
    is the ONLY sanctioned crossing, and it produces a BADGE that sits ALONGSIDE
    the object's business status rather than a 22nd business status.

    Derivation, transcribed from C16 rather than inferred:
      QUEUED  an outbox row exists for this object in state PENDING
      SENT    outbox row in state SENT and external_id populated
      FAILED  outbox row in state FAILED or DEAD -- which also sets the C3
              business status INTEGRATION_FAILED, and that write is the
              caller's, not this function's
    """
    if outbox_state is None:
        return None
    if outbox_state == "PENDING":
        return "QUEUED"
    if outbox_state == "SENT" and external_id:
        return "SENT"
    if outbox_state in ("FAILED", "DEAD"):
        return "FAILED"
    return None


def restricted_payload_keys() -> tuple[str, ...]:
    """The frozen key list, for a caller that would otherwise hard-code it."""
    return RESTRICTED_PAYLOAD_KEYS


__all__: Iterable[str] = [
    "INTEGRATION_CONNECTION", "INTEGRATION_INBOX", "INTEGRATION_OUTBOX", "JOB",
    "INTEGRATION_WATERMARK", "INTEGRATION_RATE_BUDGET", "INTEGRATION_CIRCUIT",
    "INTEGRATION_EVENT", "INTEGRATION_TABLES", "INTEGRATION_MIGRATION",
    "INBOX_IDEMPOTENCY_CONSTRAINT",
    "CONNECTION_MODES", "LIVE_MODES", "PRODUCTS", "INBOX_STATES",
    "OUTBOX_STATES", "JOB_STATES", "CIRCUIT_STATES", "WINDOW_KINDS",
    "ALLOCATIONS", "CLASSIFICATIONS",
    "ALLOCATION_SHARES", "DEFAULT_SOFT_DEADLINE_SECONDS",
    "PLATFORM_FUNCTION_CEILING_SECONDS", "DEFAULT_MAX_RESUME_COUNT",
    "DEFAULT_MAX_ATTEMPTS", "BACKOFF_BASE_SECONDS", "BACKOFF_CAP_SECONDS",
    "WATERMARK_OVERLAP_SECONDS", "REDACTION_POLICY_VERSION",
    "REDACTION_REMOVES_THE_KEY", "REGULATED_PAYLOAD_KEYS",
    "RESTRICTED_ONLY_PAYLOAD_KEYS", "RESTRICTED_PAYLOAD_KEYS",
    "IntegrationStoreError", "RateBudgetExhausted",
    "CONNECTION_SCOPE_COLUMNS", "VIA_CONNECTION_SCOPE_COLUMNS",
    "JOB_SCOPE_COLUMNS",
    "canonical_payload_sha", "classify_payload", "redact_payload",
    "backoff_delay_seconds", "allocation_ceiling", "window_keys", "poll_window",
    "create_connection", "get_connection", "list_connections",
    "set_connection_mode",
    "record_inbound", "claim_inbox_batch", "mark_inbox_processed",
    "discard_inbox", "quarantine_inbox", "fail_inbox",
    "enqueue_outbound", "claim_outbox_batch", "mark_outbox_sent",
    "mark_outbox_failed",
    "enqueue_job", "claim_job", "checkpoint_job", "finish_job", "fail_job",
    "reap_expired_jobs",
    "read_watermark", "advance_watermark", "rewind_watermark",
    "ensure_rate_budget_windows", "reserve_calls", "read_rate_budget",
    "read_circuit", "record_circuit_failure", "record_circuit_success",
    "record_event", "trace", "integration_state_badge",
    "restricted_payload_keys",
]
