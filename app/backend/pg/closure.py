"""Project closure, capitalisation and asset allocation, on PostgreSQL.

The service layer over ``migrations/pg/019_closure.sql``. Three documents:

* ``project_completion_review``  -- SCR-20. The project side ASSERTS technical
  completion; someone else decides on that assertion.
* ``capitalisation_request``     -- SCR-21. The capitalisation DECISION, and
  nothing beyond it.
* ``asset_allocation``           -- SCR-22. How the CWIP balance is split into
  the assets it becomes, and what part of it is written off instead.

THE ONE THING THIS MODULE MUST NEVER LET ANYONE BELIEVE
-------------------------------------------------------
**Approval returns ``posting_status = "NOT POSTED"`` and it always will.**

Exclusion X-01: this system records the capitalisation decision. There is no
general-ledger posting and no fixed-asset register write anywhere in this
build, and nothing here should be worded so that a reader could think there
is. ``019_closure.sql`` makes that structural --
``ck_capitalisation_request_not_posted CHECK (posting_status = 'NOT POSTED')``
-- so this module could not record a posting even if a future edit here tried
to; the database refuses the row. Every response carries the string, and
:func:`approve_capitalisation`'s audit detail says it in words as well,
because an audit entry is read years later by someone who does not have this
file open.

THE BLOCKERS ARE A GATE, NOT A WARNING (AUD-C-009)
---------------------------------------------------
:func:`closure_position` computes the list and :func:`approve_capitalisation`
REFUSES on a non-empty one. The list is the SQLite path's
(``services.approve_capitalisation``) plus the three things that path could
not see, and every entry names a figure rather than a category:

1. open commitment remains on the project;
2. value received but not billed remains;
3. an Open reconciliation exception exists for the project or its entity;
4. an Open **unattributed** exception exists -- §11.8, held at FULL VALUE;
5. no completion review has been Accepted;
6. the allocation total does not equal the CWIP balance.

(4) is the one that is easy to get wrong and expensive to get wrong. An
unattributed receipt is a real sum of money that arrived and could not be tied
to an entity. §11.8 requires it held at full value, visible, and blocking --
never spread pro-rata across the projects it might belong to, and never
quietly dropped because it has no ``project_id`` to join on. It is counted
here by a deliberately scope-exempt query for exactly that reason: a row with
``entity_id IS NULL`` matches no caller's scope predicate, so a scoped count
would return zero for every principal and the gate would be permanently open.
The same reasoning, and the same shape, as
``periods._has_open_reconciliation_exceptions``.

MONEY
-----
Integer paise throughout. Every figure comes from ``budget_ledger_cell`` --
the derived store ``procurement_services.recompute_derived_position`` writes
from ``domain.compute_ledger``'s formulas verbatim -- and is NOT re-derived
from ``bill_line`` here. Two functions in one codebase computing
``actual_paise`` differently is the defect the single derived store exists to
prevent, and adding a third would re-open it.

``SUM()`` over ``bigint`` returns ``numeric`` in PostgreSQL, so every sum is
cast ``::bigint``. ``tests/test_money_sql_discipline.py`` is the gate.

SCOPE
-----
Every read and every write goes through :func:`repo.query` with a literal
``{scope}`` token and a ``columns=`` mapping that names all four dimensions,
reached through ``project``. An out-of-scope row comes back as NO ROWS and is
reported by the router as 404 -- never 403, which on an id is an existence
oracle over another entity's estate.

The one exception is the unattributed-exception count described above, which
carries a ``scope-exempt:`` comment and its reason, as
``tests/test_scope_enforcement.py`` requires.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..money import format_inr
from . import audit as audit_mod
from . import repo
from .engine import Session

# ============================================================ scope mappings
# Every mapping names all four dimensions and waives none. The closure
# documents each carry `project_id`, and the queries below join `project`, so
# entity, plant and location are all expressible -- waiving them would let a
# caller restricted to one entity, and not otherwise restricted by project,
# read every entity's CWIP position. That is the exact defect `pg/budget.py`'s
# `_CELL_SCOPE_COLUMNS` comment records.
_PROJECT_SCOPE_COLUMNS: dict[str, str | None] = {
    "project": "p.project_id",
    "entity": "p.entity_id",
    "plant": "p.plant_id",
    "location": "p.location_id",
}

#: The states in which a document is still open to change.
LIVE_REVIEW_STATES = ("Draft", "Submitted")
LIVE_REQUEST_STATES = ("Draft", "Submitted")

#: A project in one of these states cannot be capitalised again.
TERMINAL_PROJECT_STATES = ("Capitalised", "Closed")

#: The two decisions a completion review can receive.
REVIEW_DECISIONS = ("Accepted", "Rejected")


class ClosureServiceError(Exception):
    """Raised for every rejected closure-service call.

    Same shape as :class:`~app.backend.pg.budget.BudgetServiceError` and
    :class:`~app.backend.pg.periods.PeriodServiceError`: ``code`` is the
    RFC-7807 ``code`` the API contract requires and ``status`` is the HTTP
    status the router answers with. Callers match on ``.code``, never on the
    message text.

    ``blockers`` and ``message_id`` are carried because the capitalisation
    refusal has both: a list the screen renders line by line, and an id from
    the FROZEN ``C10_messages.json``. An id absent from C10 is never invented
    here -- the router validates before it reaches the wire.
    """

    def __init__(self, code: str, message: str, *, status: int = 400,
                 blockers: list[str] | None = None,
                 message_id: str | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.blockers = blockers or []
        self.message_id = message_id
        super().__init__(f"{code}: {message}")


def _err(code: str, message: str, status: int = 400, *,
         blockers: list[str] | None = None,
         message_id: str | None = None) -> None:
    raise ClosureServiceError(code, message, status=status,
                              blockers=blockers, message_id=message_id)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


# ==========================================================================
# Position: what the closure decision is being taken against
# ==========================================================================
_POSITION_SQL = """
    SELECT
        p.project_id, p.capex_code, p.name, p.status, p.entity_id,
        COALESCE(SUM(bl.actual_paise), 0)::bigint                AS actual_paise,
        COALESCE(SUM(bl.commitment_paise), 0)::bigint            AS commitment_paise,
        COALESCE(SUM(bl.received_not_billed_paise), 0)::bigint   AS received_not_billed_paise,
        COALESCE(SUM(bl.pr_reserved_paise), 0)::bigint           AS pr_reserved_paise,
        -- 034: stock allocated and not yet issued is an open obligation of
        -- the project exactly as an unbilled order is; stock issued is CWIP.
        COALESCE(SUM(bl.internal_allocation_paise), 0)::bigint   AS internal_allocation_paise,
        COALESCE(SUM(bl.internal_consumption_paise), 0)::bigint  AS internal_consumption_paise,
        -- HOW MUCH OF THE POSITION ACTUALLY EXISTS.
        --
        -- Every figure above is `COALESCE(SUM(...), 0)` over a LEFT JOIN, so a
        -- project whose WBS elements carry NO `budget_ledger_cell` rows reports
        -- four zeros -- and four zeros produce an EMPTY blocker list, which
        -- reads as "clear to capitalise". An absent input was answering as a
        -- clean result, on a financial control.
        --
        -- This is reachable, not theoretical: no application code inserts a
        -- ledger cell anywhere (`INSERT INTO budget_ledger_cell` appears only
        -- in the demo seed and in fixtures), so a project created through the
        -- product is precisely this case.
        COUNT(DISTINCT w.wbs_id)::bigint                         AS wbs_element_count,
        COUNT(DISTINCT bl.wbs_id)::bigint                        AS ledger_cell_count
    FROM project p
    LEFT JOIN wbs_element w ON w.project_id = p.project_id
    LEFT JOIN budget_ledger_cell bl ON bl.wbs_id = w.wbs_id
    WHERE p.project_id = %(project_id)s AND {scope}
    GROUP BY p.project_id, p.capex_code, p.name, p.status, p.entity_id
"""


def _project_position(session: Session, project_id: str) -> dict[str, Any]:
    """The project's derived CWIP position, or refuse with 404.

    Reads ``budget_ledger_cell`` -- the store
    ``procurement_services.recompute_derived_position`` maintains from
    ``domain.compute_ledger``'s formulas -- rather than re-deriving from
    ``bill_line``. See the module docstring.

    A project that does not exist and a project outside the caller's scope are
    the SAME answer, by design: `repo.query` returns no rows for both, and
    distinguishing them would confirm the id is real.
    """
    row = repo.query_one(session, _POSITION_SQL, {"project_id": project_id},
                         columns=_PROJECT_SCOPE_COLUMNS)
    if row is None:
        _err("PROJECT_NOT_FOUND",
             f"No project {project_id} is visible to you.", status=404)
    return {
        "project_id": row[0], "capex_code": row[1], "project_name": row[2],
        "project_status": row[3], "entity_id": row[4],
        "cwip_balance_paise": row[5],
        "open_commitment_paise": row[6],
        "received_not_billed_paise": row[7],
        "pr_reserved_paise": row[8],
        "internal_allocation_paise": row[9],
        "internal_consumption_paise": row[10],
        # How much of the position EXISTS. See `_POSITION_SQL`: without these
        # a project with no ledger cells reported four zeros and no blockers,
        # so an absent position read as a clean one.
        "wbs_element_count": row[11],
        "ledger_cell_count": row[12],
    }


def _open_exceptions(session: Session, *, project_id: str,
                     entity_id: str) -> dict[str, Any]:
    """Open reconciliation exceptions that bear on this project's closure.

    TWO COUNTS, NOT ONE, AND THEY ARE DIFFERENT FACTS.

    ``attributed`` are Open exceptions filed against this project or its
    entity. ``unattributed`` are Open exceptions with NO entity at all --
    §11.8's case: a receipt or a commitment that arrived and could not be tied
    to anybody. They are held at FULL VALUE and they block, which is the whole
    reason the triage queue exists.

    Deliberately NOT scoped, and this is the one place in this module that is
    true. A row with ``entity_id IS NULL`` matches no caller's scope
    predicate, so a scoped count returns zero for every principal and the gate
    is permanently open -- which is precisely the fail-open shape
    ``periods._has_open_reconciliation_exceptions`` was corrected for. A
    control that cannot be evaluated must refuse; a control evaluated through
    a filter that can only ever return zero has not been evaluated at all.

    What that discloses is a COUNT and a TOTAL, to a caller who has already
    been admitted to this project's closure surface, about money that by
    definition belongs to nobody yet. The alternative is a capitalisation that
    proceeds while an unattributed sum is outstanding, which is the thing
    §11.8 exists to prevent.
    """
    attributed = session.fetchone(  # scope-exempt: control evaluation over this project and its own entity, both already scope-gated by _project_position above
        """
        SELECT COUNT(*)::bigint,
               COALESCE(SUM(COALESCE(local_paise, source_paise, 0)), 0)::bigint
        FROM reconciliation_exception
        WHERE status = 'Open'
          AND (project_id = %(project_id)s OR entity_id = %(entity_id)s)
        """,
        {"project_id": project_id, "entity_id": entity_id})
    unattributed = session.fetchone(  # scope-exempt: an unattributed row has entity_id IS NULL and matches NO scope predicate; a scoped count is structurally always zero and the 11.8 gate would never fire
        """
        SELECT COUNT(*)::bigint,
               COALESCE(SUM(COALESCE(local_paise, source_paise, 0)), 0)::bigint
        FROM reconciliation_exception
        WHERE status = 'Open' AND entity_id IS NULL
        """)
    return {
        "attributed_count": attributed[0], "attributed_paise": attributed[1],
        "unattributed_count": unattributed[0],
        # FULL VALUE. Never pro-rata, never apportioned across candidate
        # projects: nobody knows whose this is, and inventing a share would
        # put a fabricated number on a control screen.
        "unattributed_paise": unattributed[1],
    }


def _accepted_review(session: Session, project_id: str) -> tuple | None:
    return repo.query_one(
        session,
        """
        SELECT r.review_id, r.completion_date, r.decided_by, r.decided_at
        FROM project_completion_review r
        JOIN project p ON p.project_id = r.project_id
        WHERE r.project_id = %(project_id)s AND r.status = 'Accepted'
          AND {scope}
        ORDER BY r.decided_at DESC
        LIMIT 1
        """,
        {"project_id": project_id}, columns=_PROJECT_SCOPE_COLUMNS)


def _allocated_total(session: Session, cap_id: str) -> int:
    row = repo.query_one(
        session,
        """
        SELECT COALESCE(SUM(a.amount_paise), 0)::bigint,
               COUNT(*)::bigint,
               COALESCE(SUM(a.amount_paise) FILTER (WHERE a.is_writeoff), 0)::bigint
        FROM asset_allocation a
        JOIN project p ON p.project_id = a.project_id
        WHERE a.cap_id = %(cap_id)s AND {scope}
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    return int(row[0]) if row is not None else 0


def closure_position(session: Session, *, project_id: str,
                     cap_id: str | None = None) -> dict[str, Any]:
    """Everything SCR-20 and SCR-21 need to show, and the blocker list.

    ``blockers`` is the AUTHORITY. :func:`approve_capitalisation` recomputes
    it inside its own transaction and refuses on a non-empty list; this
    function exists so a screen can show the same list BEFORE anyone presses
    the button, from the same code, rather than a screen-side approximation
    that can disagree with the server.

    Every entry names a figure. "Open commitment remains" is not actionable;
    "Rs 12,00,000 of open commitment remains" is.
    """
    position = _project_position(session, project_id)
    exceptions = _open_exceptions(session, project_id=project_id,
                                  entity_id=position["entity_id"])
    review = _accepted_review(session, project_id)
    allocated = _allocated_total(session, cap_id) if cap_id else 0

    blockers: list[str] = []

    # AN ABSENT POSITION IS NOT A CLEAN POSITION, and this blocker exists
    # because the difference was being lost. Every money figure above is
    # `COALESCE(SUM(...), 0)` over a LEFT JOIN, so a project whose WBS elements
    # carry no `budget_ledger_cell` rows produced four zeros -- and four zeros
    # produced an EMPTY blocker list, which this function's own docstring calls
    # THE AUTHORITY. The gate said "nothing is blocking" when it had found
    # nothing at all.
    #
    # A project with no WBS elements is a different case and keeps its previous
    # behaviour: there is genuinely nothing to capitalise, and saying so is
    # correct.
    if position["wbs_element_count"] and not position["ledger_cell_count"]:
        blockers.append(
            f"this project has {position['wbs_element_count']} WBS element(s) "
            "and no budget ledger cell, so its CWIP position cannot be "
            "computed. The zeros shown are the ABSENCE of a position, not a "
            "position of zero. Run the ledger recompute for this project "
            "before capitalising.")

    if position["open_commitment_paise"] != 0:
        blockers.append(
            f"{format_inr(position['open_commitment_paise'])} of open "
            f"commitment remains")
    if position["received_not_billed_paise"] != 0:
        blockers.append(
            f"{format_inr(position['received_not_billed_paise'])} received "
            f"but not billed")
    if position["pr_reserved_paise"] != 0:
        blockers.append(
            f"{format_inr(position['pr_reserved_paise'])} still held by live "
            f"purchase-request reservations")
    if position["internal_allocation_paise"] != 0:
        blockers.append(
            f"{format_inr(position['internal_allocation_paise'])} of stock is "
            f"allocated to this project and not yet issued or released")
    if exceptions["attributed_count"]:
        blockers.append(
            f"{exceptions['attributed_count']} open reconciliation "
            f"exception(s) on this project or its entity")
    if exceptions["unattributed_count"]:
        # §11.8, at full value. See _open_exceptions.
        blockers.append(
            f"{exceptions['unattributed_count']} unattributed reconciliation "
            f"exception(s) totalling "
            f"{format_inr(exceptions['unattributed_paise'])} are outstanding "
            f"and are held at full value until they are attributed")
    if review is None:
        blockers.append(
            "no project completion review has been Accepted for this project")
    if position["project_status"] in TERMINAL_PROJECT_STATES:
        blockers.append(
            f"the project is already {position['project_status']}")
    if cap_id is not None and allocated != position["cwip_balance_paise"]:
        difference = position["cwip_balance_paise"] - allocated
        blockers.append(
            f"the allocation totals {format_inr(allocated)} against a CWIP "
            f"balance of {format_inr(position['cwip_balance_paise'])}, a "
            f"difference of {format_inr(difference)}")

    return {
        **position,
        "open_exceptions": exceptions,
        "accepted_review_id": review[0] if review else None,
        "completion_date": _iso(review[1]) if review else None,
        "allocated_paise": allocated,
        "unallocated_paise": position["cwip_balance_paise"] - allocated,
        "blockers": blockers,
        "capitalisable": not blockers,
        # Stated on the POSITION as well as on the decision, so a screen
        # showing the balance before approval cannot imply a posting either.
        "posting_status": "NOT POSTED",
        "posting_note": (
            "This application records the capitalisation DECISION. No general "
            "ledger or fixed-asset posting exists in this build."),
        "source": "wave7-closure",
    }


# ==========================================================================
# SCR-20: project completion review
# ==========================================================================
_REVIEW_COLUMNS = (
    "r.review_id, r.project_id, p.capex_code, p.name, r.status, "
    "r.completion_date, r.summary, r.decision_note, r.submitted_by, "
    "r.submitted_at, r.decided_by, r.decided_at, r.version_no")


def _review_row(row: tuple) -> dict[str, Any]:
    return {
        "review_id": row[0], "project_id": row[1], "capex_code": row[2],
        "project_name": row[3], "status": row[4],
        "completion_date": _iso(row[5]), "summary": row[6],
        "decision_note": row[7], "submitted_by": row[8],
        "submitted_at": _iso(row[9]), "decided_by": row[10],
        "decided_at": _iso(row[11]), "version_no": row[12],
        "source": "wave7-closure",
    }


def list_completion_reviews(session: Session, *, project_id: str | None = None,
                            status: str | None = None,
                            limit: int = 100) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 500))}
    if project_id is not None:
        conditions.append("r.project_id = %(project_id)s")
        params["project_id"] = project_id
    if status is not None:
        conditions.append("r.status = %(status)s")
        params["status"] = status
    where = (" AND ".join(conditions) + " AND ") if conditions else ""
    rows = repo.query(
        session,
        f"""
        SELECT {_REVIEW_COLUMNS}
        FROM project_completion_review r
        JOIN project p ON p.project_id = r.project_id
        WHERE {where}{{scope}}
        ORDER BY r.created_at DESC, r.review_id DESC
        LIMIT %(limit)s
        """,
        params, columns=_PROJECT_SCOPE_COLUMNS)
    return [_review_row(r) for r in rows]


def create_completion_review(session: Session, *, project_id: str,
                             summary: str, actor: str) -> dict[str, Any]:
    """Raise a Draft completion review. One live review per project.

    The uniqueness is the database's (``ux_project_completion_review_live``),
    not a read-then-write here: two concurrent requests both reading "no live
    review" and both inserting is exactly what a partial unique index exists
    to refuse, and a check in Python cannot.
    """
    position = _project_position(session, project_id)
    if position["project_status"] in TERMINAL_PROJECT_STATES:
        _err("PROJECT_TERMINAL",
             f"{position['capex_code']} is {position['project_status']}. A "
             f"completion review cannot be raised against a project that is "
             f"already closed.", status=409)

    existing = repo.query_one(
        session,
        """
        SELECT r.review_id, r.status
        FROM project_completion_review r
        JOIN project p ON p.project_id = r.project_id
        WHERE r.project_id = %(project_id)s
          AND r.status = ANY(%(live)s) AND {scope}
        """,
        {"project_id": project_id, "live": list(LIVE_REVIEW_STATES)},
        columns=_PROJECT_SCOPE_COLUMNS)
    if existing is not None:
        _err("REVIEW_ALREADY_OPEN",
             f"{position['capex_code']} already has a {existing[1]} completion "
             f"review ({existing[0]}). Decide it before raising another.",
             status=409)

    review_id = _new_id("PCR")
    rows = repo.query(
        session,
        """
        INSERT INTO project_completion_review
            (review_id, project_id, status, summary,
             created_by, updated_by)
        SELECT %(review_id)s, p.project_id, 'Draft', %(summary)s,
               %(actor)s, %(actor)s
        FROM project p
        WHERE p.project_id = %(project_id)s AND {scope}
        RETURNING review_id, project_id, status
        """,
        {"review_id": review_id, "project_id": project_id,
         "summary": _text(summary) or None, "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("PROJECT_NOT_FOUND",
             f"No project {project_id} is visible to you.", status=404)

    audit_mod.append(
        session, actor, "COMPLETION_REVIEW_RAISED",
        "PROJECT_COMPLETION_REVIEW", review_id,
        f"Completion review raised for {position['capex_code']} by {actor}.")
    return {"review_id": review_id, "project_id": project_id,
            "status": "Draft", "source": "wave7-closure"}


def submit_completion_review(session: Session, *, review_id: str,
                             completion_date: str, actor: str) -> dict[str, Any]:
    """Assert technical completion, with the date.

    ``completion_date`` is REQUIRED and is not derived. Nothing in this schema
    knows when the last bolt was tightened, and inferring it from the newest
    GRN would put a fabricated fact on a control record.
    """
    if not _text(completion_date):
        _err("COMPLETION_DATE_REQUIRED",
             "A completion review must state the date completion is asserted "
             "on. It is not derived from any document in this system.",
             status=422)

    rows = repo.query(
        session,
        """
        UPDATE project_completion_review r
           SET status = 'Submitted',
               completion_date = %(completion_date)s,
               submitted_by = %(actor)s,
               submitted_at = now(),
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = r.version_no + 1
          FROM project p
         WHERE p.project_id = r.project_id
           AND r.review_id = %(review_id)s
           AND r.status = 'Draft'
           AND {scope}
        RETURNING r.review_id, r.project_id, r.status, r.completion_date
        """,
        {"review_id": review_id, "completion_date": completion_date,
         "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        # "Not yours", "already submitted" and "no such review" are three
        # different facts about a row the caller may not be entitled to know
        # exists. One answer for all three.
        _err("DRAFT_REVIEW_NOT_FOUND",
             f"No Draft completion review {review_id} is visible to you.",
             status=404)
    row = rows[0]
    audit_mod.append(
        session, actor, "COMPLETION_REVIEW_SUBMITTED",
        "PROJECT_COMPLETION_REVIEW", review_id,
        f"Technical completion asserted as of {_iso(row[3])} by {actor}.")
    return {"review_id": row[0], "project_id": row[1], "status": row[2],
            "completion_date": _iso(row[3]), "source": "wave7-closure"}


def decide_completion_review(session: Session, *, review_id: str,
                             decision: str, note: str,
                             actor: str) -> dict[str, Any]:
    """Accept or reject a submitted completion review. Maker-checker applies.

    The person who ASSERTED completion may not also decide it. That is not a
    permission question -- both actors hold the same permission -- so it is
    checked here, against the row, inside the transaction, exactly as
    ``auth.require_separation`` checks the capitalisation approval.
    """
    if decision not in REVIEW_DECISIONS:
        _err("UNKNOWN_DECISION",
             f"A completion review is decided by one of "
             f"{', '.join(REVIEW_DECISIONS)}.", status=400)
    if not _text(note):
        _err("DECISION_NOTE_REQUIRED",
             "The decision note is written into the audit entry and into the "
             "review. Accepting a completion assertion releases the "
             "capitalisation gate, and doing so without a stated basis leaves "
             "an auditor with a status change and no reason for it.",
             status=422)

    current = repo.query_one(
        session,
        """
        SELECT r.review_id, r.submitted_by, p.capex_code
        FROM project_completion_review r
        JOIN project p ON p.project_id = r.project_id
        WHERE r.review_id = %(review_id)s AND r.status = 'Submitted'
          AND {scope}
        FOR UPDATE OF r
        """,
        {"review_id": review_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if current is None:
        _err("SUBMITTED_REVIEW_NOT_FOUND",
             f"No Submitted completion review {review_id} is visible to you.",
             status=404)
    if current[1] and current[1] == actor:
        _err("SELF_APPROVAL",
             f"You asserted completion on {current[2]} and cannot also decide "
             f"it. Segregation of duties requires an independent reviewer.",
             status=403)

    rows = repo.query(
        session,
        """
        UPDATE project_completion_review r
           SET status = %(decision)s,
               decision_note = %(note)s,
               decided_by = %(actor)s,
               decided_at = now(),
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = r.version_no + 1
          FROM project p
         WHERE p.project_id = r.project_id
           AND r.review_id = %(review_id)s
           AND r.status = 'Submitted'
           AND {scope}
        RETURNING r.review_id, r.project_id, r.status, r.decided_at
        """,
        {"review_id": review_id, "decision": decision,
         "note": _text(note), "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("SUBMITTED_REVIEW_NOT_FOUND",
             f"No Submitted completion review {review_id} is visible to you.",
             status=404)
    row = rows[0]
    audit_mod.append(
        session, actor, f"COMPLETION_REVIEW_{decision.upper()}",
        "PROJECT_COMPLETION_REVIEW", review_id,
        f"{decision} by {actor} (asserted by {current[1] or 'unknown'}). "
        f"Reason: {_text(note)}")
    return {"review_id": row[0], "project_id": row[1], "status": row[2],
            "decided_at": _iso(row[3]), "decided_by": actor,
            "source": "wave7-closure"}


# ==========================================================================
# SCR-21: capitalisation request
# ==========================================================================
_REQUEST_COLUMNS = (
    "c.cap_id, c.cap_number, c.project_id, p.capex_code, p.name, c.review_id, "
    "c.status, c.cwip_balance_paise, c.allocated_paise, c.posting_status, "
    "c.requested_by, c.requested_at, c.submitted_at, c.approver, "
    "c.approved_at, c.decision_note, c.version_no")


def _request_row(row: tuple) -> dict[str, Any]:
    return {
        "cap_id": row[0], "cap_number": row[1], "project_id": row[2],
        "capex_code": row[3], "project_name": row[4], "review_id": row[5],
        "status": row[6], "cwip_balance_paise": row[7],
        "allocated_paise": row[8],
        # Straight off the row. `ck_capitalisation_request_not_posted` is what
        # makes it true; this is not a literal typed in by the renderer.
        "posting_status": row[9],
        "requested_by": row[10], "requested_at": _iso(row[11]),
        "submitted_at": _iso(row[12]), "approver": row[13],
        "approved_at": _iso(row[14]), "decision_note": row[15],
        "version_no": row[16],
        "source": "wave7-closure",
    }


def list_capitalisation_requests(session: Session, *,
                                 project_id: str | None = None,
                                 status: str | None = None,
                                 limit: int = 100) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 500))}
    if project_id is not None:
        conditions.append("c.project_id = %(project_id)s")
        params["project_id"] = project_id
    if status is not None:
        conditions.append("c.status = %(status)s")
        params["status"] = status
    where = (" AND ".join(conditions) + " AND ") if conditions else ""
    rows = repo.query(
        session,
        f"""
        SELECT {_REQUEST_COLUMNS}
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE {where}{{scope}}
        ORDER BY c.requested_at DESC, c.cap_id DESC
        LIMIT %(limit)s
        """,
        params, columns=_PROJECT_SCOPE_COLUMNS)
    return [_request_row(r) for r in rows]


def get_capitalisation_request(session: Session, *, cap_id: str) -> dict[str, Any]:
    row = repo.query_one(
        session,
        f"""
        SELECT {_REQUEST_COLUMNS}
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE c.cap_id = %(cap_id)s AND {{scope}}
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if row is None:
        _err("CAP_NOT_FOUND",
             f"No capitalisation request {cap_id} is visible to you.",
             status=404)
    return _request_row(row)


def create_capitalisation_request(session: Session, *, project_id: str,
                                  actor: str) -> dict[str, Any]:
    """Raise a Draft capitalisation request against the project's CWIP balance.

    The balance is captured on the row so the figure the approver sees is the
    figure that was raised against. It is RE-READ and re-checked at approval;
    a divergence refuses rather than silently updating, because a CWIP balance
    that moved between raising and approving is a fact the approver has to be
    told about.
    """
    position = _project_position(session, project_id)
    if position["project_status"] in TERMINAL_PROJECT_STATES:
        _err("PROJECT_TERMINAL",
             f"{position['capex_code']} is {position['project_status']} and "
             f"cannot be capitalised again.", status=409)

    existing = repo.query_one(
        session,
        """
        SELECT c.cap_id, c.status
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE c.project_id = %(project_id)s AND c.status = ANY(%(live)s)
          AND {scope}
        """,
        {"project_id": project_id, "live": list(LIVE_REQUEST_STATES)},
        columns=_PROJECT_SCOPE_COLUMNS)
    if existing is not None:
        _err("REQUEST_ALREADY_OPEN",
             f"{position['capex_code']} already has a {existing[1]} "
             f"capitalisation request ({existing[0]}).", status=409)

    review = _accepted_review(session, project_id)
    cap_id = _new_id("CAP")
    cap_number = f"CAP-{position['capex_code']}-{cap_id[-6:]}"
    rows = repo.query(
        session,
        """
        INSERT INTO capitalisation_request
            (cap_id, cap_number, project_id, review_id, status,
             cwip_balance_paise, allocated_paise, requested_by,
             created_by, updated_by)
        SELECT %(cap_id)s, %(cap_number)s, p.project_id, %(review_id)s,
               'Draft', %(balance)s, 0, %(actor)s, %(actor)s, %(actor)s
        FROM project p
        WHERE p.project_id = %(project_id)s AND {scope}
        RETURNING cap_id, cap_number, project_id, status,
                  cwip_balance_paise, posting_status
        """,
        {"cap_id": cap_id, "cap_number": cap_number, "project_id": project_id,
         "review_id": review[0] if review else None,
         "balance": position["cwip_balance_paise"], "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("PROJECT_NOT_FOUND",
             f"No project {project_id} is visible to you.", status=404)
    row = rows[0]

    audit_mod.append(
        session, actor, "CAP_RAISED", "CAPITALISATION_REQUEST", cap_id,
        f"{row[1]} raised against a CWIP balance of "
        f"{format_inr(row[4])} on {position['capex_code']} by {actor}. "
        f"NOT POSTED to any general ledger or fixed-asset register - this "
        f"application records the decision only.")
    return {"cap_id": row[0], "cap_number": row[1], "project_id": row[2],
            "status": row[3], "cwip_balance_paise": row[4],
            "posting_status": row[5], "source": "wave7-closure"}


def submit_capitalisation_request(session: Session, *, cap_id: str,
                                  actor: str) -> dict[str, Any]:
    rows = repo.query(
        session,
        """
        UPDATE capitalisation_request c
           SET status = 'Submitted',
               submitted_at = now(),
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = c.version_no + 1
          FROM project p
         WHERE p.project_id = c.project_id
           AND c.cap_id = %(cap_id)s
           AND c.status = 'Draft'
           AND {scope}
        RETURNING c.cap_id, c.cap_number, c.project_id, c.status,
                  c.cwip_balance_paise, c.allocated_paise, c.posting_status
        """,
        {"cap_id": cap_id, "actor": actor}, columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("DRAFT_REQUEST_NOT_FOUND",
             f"No Draft capitalisation request {cap_id} is visible to you.",
             status=404)
    row = rows[0]
    audit_mod.append(
        session, actor, "CAP_SUBMITTED", "CAPITALISATION_REQUEST", cap_id,
        f"{row[1]} submitted for approval by {actor}: "
        f"{format_inr(row[5])} allocated against a CWIP balance of "
        f"{format_inr(row[4])}.")
    return {"cap_id": row[0], "cap_number": row[1], "project_id": row[2],
            "status": row[3], "cwip_balance_paise": row[4],
            "allocated_paise": row[5], "posting_status": row[6],
            "source": "wave7-closure"}


def approve_capitalisation(session: Session, *, cap_id: str, note: str,
                           actor: str) -> dict[str, Any]:
    """Approve the capitalisation DECISION. Nothing is posted anywhere.

    AUD-C-009: eligibility is a GATE, not a warning. Every blocker
    :func:`closure_position` computes is recomputed here, inside this
    transaction, against the row locked below -- so a blocker that appeared
    after the screen last refreshed still refuses.

    MAKER-CHECKER is enforced against the ROW, not against a role. The router
    has already run ``auth.require(actor, "capitalisation.approve")`` and
    ``auth.require_separation(...)`` in that order; this is the second,
    independent check, against ``requested_by`` read under the lock, because a
    router-level check reads a value the router was handed and this one reads
    the value the database holds.

    Returns ``posting_status = "NOT POSTED"``. Read the module docstring
    before changing that.
    """
    if not _text(note):
        _err("DECISION_NOTE_REQUIRED",
             "The approval note is written into the audit entry and into the "
             "request. A capitalisation decision without a stated basis "
             "leaves an auditor with a status change and no reason for it.",
             status=422)

    locked = repo.query_one(
        session,
        """
        SELECT c.cap_id, c.cap_number, c.project_id, c.requested_by,
               c.cwip_balance_paise
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE c.cap_id = %(cap_id)s AND c.status = 'Submitted' AND {scope}
        FOR UPDATE OF c
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if locked is None:
        _err("SUBMITTED_REQUEST_NOT_FOUND",
             f"No Submitted capitalisation request {cap_id} is visible to you.",
             status=404)
    _cap_id, cap_number, project_id, requested_by, raised_balance = locked

    if requested_by and requested_by == actor:
        _err("SELF_APPROVAL",
             f"You raised {cap_number} and cannot also approve it. "
             f"Segregation of duties requires an independent approver.",
             status=403)

    position = closure_position(session, project_id=project_id, cap_id=cap_id)
    if position["blockers"]:
        _err("CAPITALISATION_BLOCKED",
             f"{cap_number} cannot be capitalised: "
             + "; ".join(position["blockers"])
             + ". Resolve these, or record an explicit write-off, before "
               "capitalising.",
             status=409, blockers=position["blockers"],
             message_id="MSG-CAP-001")

    if position["cwip_balance_paise"] != raised_balance:
        _err("BALANCE_MOVED",
             f"{cap_number} was raised against a CWIP balance of "
             f"{format_inr(raised_balance)} and the balance is now "
             f"{format_inr(position['cwip_balance_paise'])}. Re-raise the "
             f"request against the current balance rather than approving a "
             f"figure that has changed.",
             status=409, message_id="MSG-CAP-002")

    rows = repo.query(
        session,
        """
        UPDATE capitalisation_request c
           SET status = 'Approved',
               approver = %(actor)s,
               approved_at = now(),
               decision_note = %(note)s,
               allocated_paise = %(allocated)s,
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = c.version_no + 1
          FROM project p
         WHERE p.project_id = c.project_id
           AND c.cap_id = %(cap_id)s
           AND c.status = 'Submitted'
           AND {scope}
        RETURNING c.cap_id, c.cap_number, c.project_id, c.status,
                  c.cwip_balance_paise, c.allocated_paise, c.posting_status,
                  c.approved_at
        """,
        {"cap_id": cap_id, "actor": actor, "note": _text(note),
         "allocated": position["allocated_paise"]},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("SUBMITTED_REQUEST_NOT_FOUND",
             f"No Submitted capitalisation request {cap_id} is visible to you.",
             status=404)
    row = rows[0]

    # The project moves to `Capitalised`, which `lifecycle_state` (014) marks
    # as permitting neither procurement nor posting. That is the business
    # consequence of the decision and it is local to this application; it is
    # NOT a posting.
    repo.query(
        session,
        """
        UPDATE project p
           SET status = 'Capitalised',
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = p.version_no + 1
         WHERE p.project_id = %(project_id)s AND {scope}
        RETURNING p.project_id
        """,
        {"project_id": project_id, "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)

    audit_mod.append(
        session, actor, "CAP_APPROVED", "CAPITALISATION_REQUEST", cap_id,
        f"{format_inr(row[4])} approved for capitalisation by {actor} "
        f"(requested by {requested_by}). {format_inr(row[5])} allocated "
        f"across the recorded asset allocations. "
        f"NOT YET POSTED to any general ledger or fixed-asset register - "
        f"local approval only. Reason: {_text(note)}")

    return {
        "cap_id": row[0], "cap_number": row[1], "project_id": row[2],
        "status": row[3], "capitalised_paise": row[4],
        "allocated_paise": row[5],
        "posting_status": row[6],
        "posting_note": (
            "NOT POSTED - local approval only; no ERP/GL or fixed-asset "
            "posting exists in this build."),
        "approved_at": _iso(row[7]), "approver": actor,
        "message_id": "MSG-CAP-003",
        "source": "wave7-closure",
    }


def reject_capitalisation(session: Session, *, cap_id: str, note: str,
                          actor: str) -> dict[str, Any]:
    if not _text(note):
        _err("DECISION_NOTE_REQUIRED",
             "A rejection states its reason; the requester has to know what "
             "to change.", status=422)
    locked = repo.query_one(
        session,
        """
        SELECT c.cap_id, c.cap_number, c.requested_by
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE c.cap_id = %(cap_id)s AND c.status = 'Submitted' AND {scope}
        FOR UPDATE OF c
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if locked is None:
        _err("SUBMITTED_REQUEST_NOT_FOUND",
             f"No Submitted capitalisation request {cap_id} is visible to you.",
             status=404)
    if locked[2] and locked[2] == actor:
        _err("SELF_APPROVAL",
             f"You raised {locked[1]} and cannot also decide it.", status=403)

    rows = repo.query(
        session,
        """
        UPDATE capitalisation_request c
           SET status = 'Rejected',
               approver = %(actor)s,
               approved_at = now(),
               decision_note = %(note)s,
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = c.version_no + 1
          FROM project p
         WHERE p.project_id = c.project_id
           AND c.cap_id = %(cap_id)s
           AND c.status = 'Submitted'
           AND {scope}
        RETURNING c.cap_id, c.cap_number, c.project_id, c.status,
                  c.posting_status
        """,
        {"cap_id": cap_id, "actor": actor, "note": _text(note)},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        _err("SUBMITTED_REQUEST_NOT_FOUND",
             f"No Submitted capitalisation request {cap_id} is visible to you.",
             status=404)
    row = rows[0]
    audit_mod.append(
        session, actor, "CAP_REJECTED", "CAPITALISATION_REQUEST", cap_id,
        f"{row[1]} rejected by {actor} (requested by {locked[2]}). "
        f"Reason: {_text(note)}")
    return {"cap_id": row[0], "cap_number": row[1], "project_id": row[2],
            "status": row[3], "posting_status": row[4],
            "source": "wave7-closure"}


# ==========================================================================
# SCR-22: asset allocation
# ==========================================================================
def list_allocations(session: Session, *, cap_id: str) -> list[dict[str, Any]]:
    rows = repo.query(
        session,
        """
        SELECT a.allocation_id, a.cap_id, a.project_id, a.wbs_id, w.wbs_code,
               w.description, a.asset_name, a.asset_category, a.amount_paise,
               a.is_writeoff, a.writeoff_reason, a.created_by, a.created_at
        FROM asset_allocation a
        JOIN project p ON p.project_id = a.project_id
        JOIN wbs_element w ON w.wbs_id = a.wbs_id
        WHERE a.cap_id = %(cap_id)s AND {scope}
        ORDER BY a.created_at, a.allocation_id
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    return [
        {"allocation_id": r[0], "cap_id": r[1], "project_id": r[2],
         "wbs_id": r[3], "wbs_code": r[4], "wbs_description": r[5],
         "asset_name": r[6], "asset_category": r[7], "amount_paise": r[8],
         "is_writeoff": r[9], "writeoff_reason": r[10],
         "created_by": r[11], "created_at": _iso(r[12]),
         "source": "wave7-closure"}
        for r in rows
    ]


def create_allocation(session: Session, *, cap_id: str, wbs_id: str,
                      asset_name: str, asset_category: str | None,
                      amount_paise: int, is_writeoff: bool,
                      writeoff_reason: str | None,
                      actor: str) -> dict[str, Any]:
    """Allocate part of the CWIP balance to an asset, or write it off.

    A WRITE-OFF IS A POSITIVE AMOUNT WITH A FLAG AND A REASON, never a
    negative allocation. A sign is not a category: the write-off has to be
    visible as a write-off on the screen, in the audit entry and in any later
    read, and a negative number is none of those things. The database enforces
    both halves (``ck_asset_allocation_amount_positive``,
    ``ck_asset_allocation_writeoff_reason``); the messages below exist so the
    caller is told which rule they hit rather than reading a constraint name.
    """
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        # `bool` is an `int` in Python and `True` would arrive as one paise.
        # A float is refused outright rather than rounded: rounding is where a
        # rupee goes missing, and money never travels as a float here.
        raise ClosureServiceError(
            "NON_INTEGER_AMOUNT",
            "An allocation amount is integer paise. Money never travels as a "
            "float on this surface.", status=422)
    amount = amount_paise
    if amount <= 0:
        _err("NON_POSITIVE_ALLOCATION",
             "An allocation must be a positive amount of paise. A write-off "
             "is a positive amount carrying is_writeoff and a reason, not a "
             "negative allocation.", status=422)
    if is_writeoff and not _text(writeoff_reason):
        _err("REASON_REQUIRED",
             "A write-off requires a recorded reason. It is the difference "
             "between value that became an asset and value that did not.",
             status=422)
    if not _text(asset_name):
        _err("ASSET_NAME_REQUIRED",
             "Every allocation names the asset it becomes, including a "
             "write-off, so the CWIP balance can be read back line by line.",
             status=422)

    request = repo.query_one(
        session,
        """
        SELECT c.cap_id, c.cap_number, c.project_id, c.status,
               c.cwip_balance_paise
        FROM capitalisation_request c
        JOIN project p ON p.project_id = c.project_id
        WHERE c.cap_id = %(cap_id)s AND {scope}
        FOR UPDATE OF c
        """,
        {"cap_id": cap_id}, columns=_PROJECT_SCOPE_COLUMNS)
    if request is None:
        _err("CAP_NOT_FOUND",
             f"No capitalisation request {cap_id} is visible to you.",
             status=404)
    if request[3] != "Draft":
        _err("INVALID_TRANSITION",
             f"{request[1]} is {request[3]}; allocations are closed once a "
             f"request leaves Draft.", status=409)

    allocation_id = _new_id("ALLOC")
    rows = repo.query(
        session,
        """
        INSERT INTO asset_allocation
            (allocation_id, cap_id, project_id, wbs_id, asset_name,
             asset_category, amount_paise, is_writeoff, writeoff_reason,
             created_by, updated_by)
        SELECT %(allocation_id)s, %(cap_id)s, w.project_id, w.wbs_id,
               %(asset_name)s, %(asset_category)s, %(amount)s,
               %(is_writeoff)s, %(writeoff_reason)s, %(actor)s, %(actor)s
        FROM wbs_element w
        JOIN project p ON p.project_id = w.project_id
        WHERE w.wbs_id = %(wbs_id)s
          AND w.project_id = %(project_id)s
          AND {scope}
        RETURNING allocation_id, amount_paise, is_writeoff
        """,
        {"allocation_id": allocation_id, "cap_id": cap_id, "wbs_id": wbs_id,
         "project_id": request[2], "asset_name": _text(asset_name),
         "asset_category": _text(asset_category) or None, "amount": amount,
         "is_writeoff": bool(is_writeoff),
         "writeoff_reason": _text(writeoff_reason) or None, "actor": actor},
        columns=_PROJECT_SCOPE_COLUMNS)
    if not rows:
        # The WBS element does not exist, is out of scope, or belongs to a
        # DIFFERENT project. One answer: the caller may not be entitled to
        # know which.
        _err("WBS_NOT_ON_PROJECT",
             f"No WBS element {wbs_id} on this request's project is visible "
             f"to you.", status=404)

    # Recompute the cache from the child rows in the same transaction. It is
    # never adjusted incrementally: a cache that drifts from its source is
    # worse than no cache, and this one is read by the approval gate.
    total = _recompute_allocated(session, cap_id, actor=actor)

    audit_mod.append(
        session, actor, "CAP_ALLOCATED", "CAPITALISATION_REQUEST", cap_id,
        f"{_text(asset_name)}: {format_inr(amount)}"
        + (f" (WRITE-OFF: {_text(writeoff_reason)})" if is_writeoff else "")
        + f". Allocated to date {format_inr(total)} of "
          f"{format_inr(request[4])}.")
    return {"allocation_id": allocation_id, "cap_id": cap_id,
            "amount_paise": amount, "is_writeoff": bool(is_writeoff),
            "allocated_paise": total,
            "unallocated_paise": request[4] - total,
            "source": "wave7-closure"}


def _recompute_allocated(session: Session, cap_id: str, *, actor: str) -> int:
    """Re-derive ``capitalisation_request.allocated_paise`` from its children.

    In full, from the child rows, never by adding a delta -- the same rule
    ``budget.recompute_cell`` and
    ``procurement_services.recompute_derived_position`` follow, and for the
    same reason: recomputation is idempotent and a delta is not.
    """
    rows = repo.query(
        session,
        """
        UPDATE capitalisation_request c
           SET allocated_paise = COALESCE((
                   SELECT SUM(a.amount_paise)::bigint
                   FROM asset_allocation a
                   WHERE a.cap_id = c.cap_id
               ), 0),
               updated_by = %(actor)s,
               updated_at = now(),
               version_no = c.version_no + 1
          FROM project p
         WHERE p.project_id = c.project_id
           AND c.cap_id = %(cap_id)s
           AND {scope}
        RETURNING c.allocated_paise
        """,
        {"cap_id": cap_id, "actor": actor}, columns=_PROJECT_SCOPE_COLUMNS)
    return int(rows[0][0]) if rows else 0
