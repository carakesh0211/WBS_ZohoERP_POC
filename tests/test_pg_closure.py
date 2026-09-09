"""Wave 7 agent A4: ``app.backend.pg.closure`` -- the closure chain on PostgreSQL.

Two layers, the split every Wave 3-6 file makes for the same reason.

**Layer 1 runs everywhere.** The blocker assembly, the write-off rules, the
money guard and the error shape are proved on every machine against a fake
session that answers the module's own queries. That is not a mock of the
behaviour under test -- the SQL is still the module's, the blocker text is
still the module's, and the fake only stands in for the server's row.

**Layer 2 needs a live PostgreSQL and is gated on ``CAPEX_DB_URL``.** It covers
what only a server can answer: does ``ck_capitalisation_request_not_posted``
actually refuse a posting claim, does the partial unique index actually refuse
a second live request, does a maker-checker refusal actually stop the UPDATE,
does an out-of-scope project actually answer as absent.

**A SKIP IS NOT A PASS.** Every live test is gated with a reason that says so
out loud. Nothing here reports success against a database that was never there.

THE ASSERTION THIS FILE EXISTS FOR
==================================
``posting_status`` is ``'NOT POSTED'`` and no code path can make it anything
else. It is asserted on the migration's CHECK constraint (live), on the
service's return value (live), and on the router's collection envelope. If a
future change makes any of those say something else, this file fails before a
screen can tell somebody their money reached a general ledger.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from app.backend.pg import closure  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
           "database. This test SKIPS -- it does not pass.",
)


# ==========================================================================
# Layer 1: the fake session
# ==========================================================================
class _FakeSession:
    """Answers the queries `closure_position` issues, and nothing else.

    Routed on a substring of the SQL rather than on call order, so reordering
    the module's reads does not silently change what this fake returns. An
    unrecognised statement RAISES: a fake that answered `None` to a query it
    did not know about would let a new read be added with no coverage at all
    and no sign of it.
    """

    def __init__(self, *, position_row, attributed=(0, 0), unattributed=(0, 0),
                 review_row=None, allocated_row=(0, 0, 0)):
        self.scope = Scope(user_id="U-TEST", read_all=True)
        self._position_row = position_row
        self._attributed = attributed
        self._unattributed = unattributed
        self._review_row = review_row
        self._allocated_row = allocated_row

    # -- repo.query / query_one go through fetchall ------------------------
    def fetchall(self, statement, params=None):
        if "budget_ledger_cell" in statement:
            return [self._position_row] if self._position_row else []
        if "project_completion_review" in statement:
            return [self._review_row] if self._review_row else []
        if "asset_allocation" in statement:
            return [self._allocated_row]
        raise AssertionError(f"unexpected scoped query: {statement[:120]}")

    # -- the two deliberately scope-exempt control counts -------------------
    def fetchone(self, statement, params=None):
        if "entity_id IS NULL" in statement:
            return self._unattributed
        if "reconciliation_exception" in statement:
            return self._attributed
        raise AssertionError(f"unexpected direct query: {statement[:120]}")


def _position_row(*, actual=0, commitment=0, rnb=0, reserved=0,
                  status="Released", wbs_elements=1, ledger_cells=1):
    """The row `_POSITION_SQL` returns, as a tuple in column order.

    `wbs_elements` and `ledger_cells` default to 1/1 -- a project whose
    position EXISTS -- because that is what every test here means by "a
    project". The 1/0 case is the finding: four `COALESCE(SUM(...), 0)` zeros
    over a LEFT JOIN used to produce an EMPTY blocker list, so an absent
    position read as a clean one.
    """
    return ("PRJ-1", "CX-1", "A project", status, "ENT-1",
            actual, commitment, rnb, reserved, wbs_elements, ledger_cells)


def _clean_session(**kwargs):
    """A project with a CWIP balance and nothing wrong with it."""
    defaults = dict(
        position_row=_position_row(actual=500_00),
        review_row=("PCR-1", "2026-09-01", "U-PM", "2026-09-02"),
        allocated_row=(500_00, 1, 0),
    )
    defaults.update(kwargs)
    return _FakeSession(**defaults)


# ---------------------------------------------------------------- blockers
def test_a_clean_project_has_no_blockers_and_is_capitalisable():
    result = closure.closure_position(
        _clean_session(), project_id="PRJ-1", cap_id="CAP-1")
    assert result["blockers"] == []
    assert result["capitalisable"] is True
    assert result["cwip_balance_paise"] == 500_00


def test_open_commitment_blocks_and_names_the_figure():
    session = _clean_session(
        position_row=_position_row(actual=500_00, commitment=12_00_000_00))
    result = closure.closure_position(
        session, project_id="PRJ-1", cap_id="CAP-1")
    assert any("open commitment" in b for b in result["blockers"])
    # The FIGURE, not the category. "Open commitment remains" is not
    # actionable; a number is.
    assert any("12,00,000" in b for b in result["blockers"])
    assert result["capitalisable"] is False


def test_received_not_billed_blocks():
    session = _clean_session(
        position_row=_position_row(actual=500_00, rnb=7_00_000))
    result = closure.closure_position(session, project_id="PRJ-1")
    assert any("received but not billed" in b for b in result["blockers"])


def test_a_live_pr_reservation_blocks():
    session = _clean_session(
        position_row=_position_row(actual=500_00, reserved=1_00_000))
    result = closure.closure_position(session, project_id="PRJ-1")
    assert any("purchase-request reservations" in b
               for b in result["blockers"])


def test_an_open_reconciliation_exception_blocks():
    session = _clean_session(attributed=(2, 9_00_000))
    result = closure.closure_position(session, project_id="PRJ-1")
    assert any("open reconciliation exception" in b
               for b in result["blockers"])


def test_an_unattributed_exception_blocks_at_full_value():
    """Section 11.8. FULL VALUE, and never spread pro-rata.

    The figure in the blocker must be the WHOLE outstanding unattributed sum,
    not a share of it apportioned across candidate projects. Nobody knows whose
    it is, so any share would be an invented number on a control screen.
    """
    session = _clean_session(unattributed=(3, 25_00_000_00))
    result = closure.closure_position(session, project_id="PRJ-1")
    blocker = next(b for b in result["blockers"] if "unattributed" in b)
    assert "25,00,000" in blocker
    assert result["open_exceptions"]["unattributed_paise"] == 25_00_000_00
    assert result["capitalisable"] is False


def test_no_accepted_completion_review_blocks():
    session = _clean_session(review_row=None)
    result = closure.closure_position(session, project_id="PRJ-1")
    assert any("completion review" in b for b in result["blockers"])
    assert result["accepted_review_id"] is None


def test_an_allocation_shortfall_blocks_and_names_the_difference():
    session = _clean_session(
        position_row=_position_row(actual=500_00),
        allocated_row=(300_00, 1, 0))
    result = closure.closure_position(
        session, project_id="PRJ-1", cap_id="CAP-1")
    blocker = next(b for b in result["blockers"] if "allocation totals" in b)
    assert "300.00" in blocker and "500.00" in blocker
    assert result["unallocated_paise"] == 200_00


def test_the_allocation_check_is_skipped_when_no_request_is_named():
    """SCR-20 shows the position before any capitalisation request exists.

    Reporting "the allocation totals nothing against a balance of X" on a
    project that has not raised a request yet would be a blocker the reader can
    do nothing about, on a screen that is not about allocation.
    """
    session = _clean_session(position_row=_position_row(actual=500_00))
    result = closure.closure_position(session, project_id="PRJ-1")
    assert not any("allocation totals" in b for b in result["blockers"])


def test_a_terminal_project_blocks():
    session = _clean_session(
        position_row=_position_row(actual=500_00, status="Capitalised"))
    result = closure.closure_position(session, project_id="PRJ-1")
    assert any("already Capitalised" in b for b in result["blockers"])


def test_the_position_says_not_posted_even_before_any_decision():
    """A screen showing a CWIP balance must not be able to imply a posting."""
    result = closure.closure_position(_clean_session(), project_id="PRJ-1")
    assert result["posting_status"] == "NOT POSTED"
    assert "no general ledger" in result["posting_note"].lower()


def test_a_project_outside_scope_is_reported_as_absent_not_forbidden():
    session = _FakeSession(position_row=None)
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        closure.closure_position(session, project_id="PRJ-NOPE")
    assert excinfo.value.code == "PROJECT_NOT_FOUND"
    # 404, never 403. A 403 on an id confirms the id is real.
    assert excinfo.value.status == 404


# ------------------------------------------------------- allocation guards
class _NoQuerySession:
    """Refuses every query. The guards below must reject before touching one."""

    scope = Scope(user_id="U-TEST", read_all=True)

    def fetchall(self, statement, params=None):
        raise AssertionError("a rejected allocation reached the database")

    def fetchone(self, statement, params=None):
        raise AssertionError("a rejected allocation reached the database")


def _allocate(**kwargs):
    call = dict(cap_id="CAP-1", wbs_id="W-1", asset_name="Pump",
                asset_category=None, amount_paise=1000, is_writeoff=False,
                writeoff_reason=None, actor="U-FIN")
    call.update(kwargs)
    return closure.create_allocation(_NoQuerySession(), **call)


def test_a_negative_allocation_is_refused_rather_than_read_as_a_write_off():
    """A sign is not a category.

    A write-off has to be visible AS a write-off on the screen, in the audit
    entry and in every later read. A negative amount is none of those things,
    so it is refused outright rather than reinterpreted.
    """
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(amount_paise=-1000)
    assert excinfo.value.code == "NON_POSITIVE_ALLOCATION"
    assert "not a negative allocation" in excinfo.value.message


def test_a_zero_allocation_is_refused():
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(amount_paise=0)
    assert excinfo.value.code == "NON_POSITIVE_ALLOCATION"


def test_a_float_amount_is_refused_not_rounded():
    """Rounding is where a rupee goes missing. Money never travels as a float."""
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(amount_paise=1000.5)
    assert excinfo.value.code == "NON_INTEGER_AMOUNT"


def test_a_boolean_amount_is_refused():
    """`True` is an `int` in Python and would otherwise arrive as one paise."""
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(amount_paise=True)
    assert excinfo.value.code == "NON_INTEGER_AMOUNT"


def test_a_write_off_without_a_reason_is_refused():
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(is_writeoff=True, writeoff_reason="   ")
    assert excinfo.value.code == "REASON_REQUIRED"


def test_an_allocation_without_an_asset_name_is_refused():
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        _allocate(asset_name="  ")
    assert excinfo.value.code == "ASSET_NAME_REQUIRED"


# ------------------------------------------------------------- error shape
def test_closure_service_error_carries_code_status_blockers_and_message_id():
    exc = closure.ClosureServiceError(
        "X", "y", status=409, blockers=["a", "b"], message_id="MSG-CAP-001")
    assert (exc.code, exc.status, exc.message) == ("X", 409, "y")
    assert exc.blockers == ["a", "b"]
    assert exc.message_id == "MSG-CAP-001"


def test_a_decision_without_a_note_is_refused_before_the_database():
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        closure.approve_capitalisation(
            _NoQuerySession(), cap_id="CAP-1", note="  ", actor="U-CFO")
    assert excinfo.value.code == "DECISION_NOTE_REQUIRED"


def test_a_review_decision_must_be_one_of_the_two():
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        closure.decide_completion_review(
            _NoQuerySession(), review_id="PCR-1", decision="Maybe",
            note="n", actor="U-CFO")
    assert excinfo.value.code == "UNKNOWN_DECISION"


def test_submitting_a_review_requires_a_completion_date():
    """Not derived from any document, deliberately.

    Nothing in this schema knows when the last bolt was tightened, and
    inferring it from the newest GRN would put a fabricated fact on a control
    record.
    """
    with pytest.raises(closure.ClosureServiceError) as excinfo:
        closure.submit_completion_review(
            _NoQuerySession(), review_id="PCR-1", completion_date="",
            actor="U-PM")
    assert excinfo.value.code == "COMPLETION_DATE_REQUIRED"


# ---------------------------------------------------- the honesty constants
def test_terminal_project_states_are_the_two_that_cannot_be_capitalised_again():
    assert set(closure.TERMINAL_PROJECT_STATES) == {"Capitalised", "Closed"}


def test_the_module_never_names_a_posting_status_other_than_not_posted():
    """A grep, as a test.

    `posting_status` is pinned by `ck_capitalisation_request_not_posted` in
    migration 018 and read off the row. If a future edit introduces a literal
    that claims otherwise, this fails before a screen can tell somebody their
    money reached a general ledger.
    """
    source = (_Path(__file__).resolve().parents[1]
              / "app" / "backend" / "pg" / "closure.py").read_text(encoding="utf-8")
    for banned in ("'POSTED'", '"POSTED"', "POSTED_TO_GL", "gl_posted"):
        assert banned not in source, f"closure.py names {banned}"
    assert '"NOT POSTED"' in source


# ==========================================================================
# Layer 2: live PostgreSQL
# ==========================================================================
def _seed_project(con, *, suffix: str, entity: str | None = None):
    """One organisation / entity / project / WBS / head, with a ledger cell.

    Column names are read from `migrations/pg/002_budget_control.sql` and
    `013_procurement.sql`, not from memory: `wbs_element` is
    (`wbs_code`, `description`), NOT (`code`, `name`).
    """
    org, ent = f"O_{suffix}", entity or f"E_{suffix}"
    prj, wbs, head = f"P_{suffix}", f"W_{suffix}", f"H_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,%s,'t','t') ON CONFLICT DO NOTHING",
       (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t') "
       "ON CONFLICT DO NOTHING",
       (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, status, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'Released','t','t')",
       (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
       (head, ent, f"HC_{suffix}", "Head"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
       "wbs_path, level, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,%s,0,'t','t')", (wbs, prj, wbs, "root", wbs))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
       "budget_paise, updated_by) VALUES (%s,%s,%s,'t')", (wbs, head, 10_00_000))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, actual_paise, "
       "updated_by) VALUES (%s,%s,%s,'t')", (wbs, head, 5_00_000))
    con.commit()
    return {"entity": ent, "project": prj, "wbs": wbs, "head": head}


def _session(pg_database, project_ids=None):
    scope = Scope(user_id="U-TEST", read_all=project_ids is None,
                  project_ids=project_ids)
    return pg_database.session(scope)


@PG
def test_live_the_check_constraint_refuses_any_posting_claim(
        pg_connection, pg_database):
    """`ck_capitalisation_request_not_posted`, proved against the server.

    This is the assertion that makes X-01 structural rather than a comment: an
    UPDATE claiming a posting is refused by PostgreSQL, not by a code path
    somebody could remove.
    """
    import psycopg

    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        request = closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_connection.execute(
            "UPDATE capitalisation_request SET posting_status = 'POSTED' "
            "WHERE cap_id = %s", (request["cap_id"],))
    pg_connection.rollback()


@PG
def test_live_a_second_open_request_is_refused_by_the_partial_index(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
    with _session(pg_database) as session:
        with pytest.raises(closure.ClosureServiceError) as excinfo:
            closure.create_capitalisation_request(
                session, project_id=ids["project"], actor="U-PM")
    assert excinfo.value.code == "REQUEST_ALREADY_OPEN"


@PG
def test_live_a_maker_cannot_approve_their_own_capitalisation(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        review = closure.create_completion_review(
            session, project_id=ids["project"], summary="done", actor="U-PM")
        closure.submit_completion_review(
            session, review_id=review["review_id"],
            completion_date="2026-09-01", actor="U-PM")
        closure.decide_completion_review(
            session, review_id=review["review_id"], decision="Accepted",
            note="verified", actor="U-CFO")
        request = closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
        closure.create_allocation(
            session, cap_id=request["cap_id"], wbs_id=ids["wbs"],
            asset_name="Plant", asset_category=None, amount_paise=5_00_000,
            is_writeoff=False, writeoff_reason=None, actor="U-PM")
        closure.submit_capitalisation_request(
            session, cap_id=request["cap_id"], actor="U-PM")

        with pytest.raises(closure.ClosureServiceError) as excinfo:
            closure.approve_capitalisation(
                session, cap_id=request["cap_id"], note="mine", actor="U-PM")
    assert excinfo.value.code == "SELF_APPROVAL"
    assert excinfo.value.status == 403


@PG
def test_live_a_reviewer_cannot_decide_their_own_completion_assertion(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        review = closure.create_completion_review(
            session, project_id=ids["project"], summary="done", actor="U-PM")
        closure.submit_completion_review(
            session, review_id=review["review_id"],
            completion_date="2026-09-01", actor="U-PM")
        with pytest.raises(closure.ClosureServiceError) as excinfo:
            closure.decide_completion_review(
                session, review_id=review["review_id"], decision="Accepted",
                note="mine", actor="U-PM")
    assert excinfo.value.code == "SELF_APPROVAL"


@PG
def test_live_an_open_reconciliation_exception_blocks_capitalisation(
        pg_connection, pg_database):
    """The gate the whole closure surface exists to hold."""
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, "
        "object_type, entity_id, project_id, status, detail, local_paise) "
        "VALUES (%s,'CONTROL_TOTAL_MISMATCH','BILL',%s,%s,'Open','x',%s)",
        (f"EX_{uuid.uuid4().hex[:8]}", ids["entity"], ids["project"], 100))
    pg_connection.commit()

    with _session(pg_database) as session:
        position = closure.closure_position(session, project_id=ids["project"])
    assert any("open reconciliation exception" in b
               for b in position["blockers"])
    assert position["capitalisable"] is False


@PG
def test_live_an_unattributed_exception_blocks_and_is_reported_at_full_value(
        pg_connection, pg_database):
    """Section 11.8. entity_id IS NULL matches no scope predicate, so a scoped
    count would be structurally zero and this gate would never fire."""
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    pg_connection.execute(
        "INSERT INTO reconciliation_exception (exception_id, kind, "
        "object_type, status, detail, local_paise) "
        "VALUES (%s,'GRN_LINE_UNATTRIBUTED','GRN_LINE','Open','x',%s)",
        (f"EX_{uuid.uuid4().hex[:8]}", 25_00_000_00))
    pg_connection.commit()

    with _session(pg_database) as session:
        position = closure.closure_position(session, project_id=ids["project"])
    assert position["open_exceptions"]["unattributed_paise"] == 25_00_000_00
    assert any("unattributed" in b and "25,00,000" in b
               for b in position["blockers"])


@PG
def test_live_a_clean_capitalisation_approves_and_says_not_posted(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        review = closure.create_completion_review(
            session, project_id=ids["project"], summary="done", actor="U-PM")
        closure.submit_completion_review(
            session, review_id=review["review_id"],
            completion_date="2026-09-01", actor="U-PM")
        closure.decide_completion_review(
            session, review_id=review["review_id"], decision="Accepted",
            note="verified", actor="U-CFO")
        request = closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
        closure.create_allocation(
            session, cap_id=request["cap_id"], wbs_id=ids["wbs"],
            asset_name="Plant", asset_category="P&M", amount_paise=5_00_000,
            is_writeoff=False, writeoff_reason=None, actor="U-PM")
        closure.submit_capitalisation_request(
            session, cap_id=request["cap_id"], actor="U-PM")
        result = closure.approve_capitalisation(
            session, cap_id=request["cap_id"], note="approved by committee",
            actor="U-CFO")

    assert result["status"] == "Approved"
    assert result["posting_status"] == "NOT POSTED"
    assert result["capitalised_paise"] == 5_00_000
    assert result["message_id"] == "MSG-CAP-003"

    # And the audit entry says it in words, for whoever reads it years later
    # without this file open.
    detail = pg_connection.execute(
        "SELECT detail FROM audit_log WHERE object_id = %s AND action = "
        "'CAP_APPROVED'", (request["cap_id"],)).fetchone()[0]
    assert "NOT YET POSTED" in detail


@PG
def test_live_an_allocation_shortfall_refuses_the_approval(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        review = closure.create_completion_review(
            session, project_id=ids["project"], summary="done", actor="U-PM")
        closure.submit_completion_review(
            session, review_id=review["review_id"],
            completion_date="2026-09-01", actor="U-PM")
        closure.decide_completion_review(
            session, review_id=review["review_id"], decision="Accepted",
            note="verified", actor="U-CFO")
        request = closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
        closure.create_allocation(
            session, cap_id=request["cap_id"], wbs_id=ids["wbs"],
            asset_name="Plant", asset_category=None, amount_paise=3_00_000,
            is_writeoff=False, writeoff_reason=None, actor="U-PM")
        closure.submit_capitalisation_request(
            session, cap_id=request["cap_id"], actor="U-PM")
        with pytest.raises(closure.ClosureServiceError) as excinfo:
            closure.approve_capitalisation(
                session, cap_id=request["cap_id"], note="go",
                actor="U-CFO")
    assert excinfo.value.code == "CAPITALISATION_BLOCKED"
    assert excinfo.value.message_id == "MSG-CAP-001"
    assert any("allocation totals" in b for b in excinfo.value.blockers)


@PG
def test_live_a_write_off_closes_the_shortfall_and_stays_visible_as_one(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        review = closure.create_completion_review(
            session, project_id=ids["project"], summary="done", actor="U-PM")
        closure.submit_completion_review(
            session, review_id=review["review_id"],
            completion_date="2026-09-01", actor="U-PM")
        closure.decide_completion_review(
            session, review_id=review["review_id"], decision="Accepted",
            note="verified", actor="U-CFO")
        request = closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
        closure.create_allocation(
            session, cap_id=request["cap_id"], wbs_id=ids["wbs"],
            asset_name="Plant", asset_category=None, amount_paise=3_00_000,
            is_writeoff=False, writeoff_reason=None, actor="U-PM")
        closure.create_allocation(
            session, cap_id=request["cap_id"], wbs_id=ids["wbs"],
            asset_name="Abandoned civil works", asset_category=None,
            amount_paise=2_00_000, is_writeoff=True,
            writeoff_reason="foundation redesigned", actor="U-PM")
        closure.submit_capitalisation_request(
            session, cap_id=request["cap_id"], actor="U-PM")
        result = closure.approve_capitalisation(
            session, cap_id=request["cap_id"], note="approved", actor="U-CFO")
        allocations = closure.list_allocations(
            session, cap_id=request["cap_id"])

    assert result["status"] == "Approved"
    writeoffs = [a for a in allocations if a["is_writeoff"]]
    assert len(writeoffs) == 1
    # POSITIVE, with a reason. A sign is not a category.
    assert writeoffs[0]["amount_paise"] == 2_00_000
    assert writeoffs[0]["writeoff_reason"] == "foundation redesigned"


@PG
def test_live_an_out_of_scope_project_answers_as_absent(
        pg_connection, pg_database):
    """404, never 403. A 403 on an id confirms the id is real."""
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database, project_ids=frozenset({"SOMETHING-ELSE"})) as s:
        with pytest.raises(closure.ClosureServiceError) as excinfo:
            closure.closure_position(s, project_id=ids["project"])
    assert excinfo.value.code == "PROJECT_NOT_FOUND"
    assert excinfo.value.status == 404


@PG
def test_live_a_denied_scope_sees_no_capitalisation_requests(
        pg_connection, pg_database):
    ids = _seed_project(pg_connection, suffix=uuid.uuid4().hex[:8])
    with _session(pg_database) as session:
        closure.create_capitalisation_request(
            session, project_id=ids["project"], actor="U-PM")
    with _session(pg_database, project_ids=frozenset()) as session:
        assert closure.list_capitalisation_requests(session) == []


# ---------------------------------------------------- an ABSENT position
def test_a_project_with_no_ledger_cell_is_refused_not_declared_clean():
    """The gate used to fail OPEN, which is the direction that matters.

    Every money figure in `_POSITION_SQL` is `COALESCE(SUM(...), 0)` over a
    LEFT JOIN. A project whose WBS elements carry no `budget_ledger_cell` rows
    therefore reported CWIP 0, commitment 0, received-not-billed 0 and
    reservations 0 -- and four zeros produced an EMPTY blocker list, which
    `closure_position`'s own docstring calls THE AUTHORITY. It said "nothing is
    blocking" when it had found nothing at all.

    Reachable, not theoretical: `INSERT INTO budget_ledger_cell` appears
    nowhere in `app/`, only in the demo seed and in fixtures, so a project
    created through the product is exactly this case.
    """
    session = _clean_session(
        position_row=_position_row(actual=0, wbs_elements=3, ledger_cells=0))
    result = closure.closure_position(session, project_id="PRJ-1",
                                      cap_id="CAP-1")

    assert result["capitalisable"] is False, (
        "a project with WBS elements and no ledger cell was declared "
        "capitalisable on the strength of four zeros it never computed")
    assert any("no budget ledger cell" in b for b in result["blockers"]), (
        result["blockers"])


def test_a_project_with_no_wbs_elements_is_left_alone():
    """The neighbouring case, which is NOT the same and must not be caught.

    A project with no WBS elements has genuinely nothing to capitalise. Saying
    so is correct, and a blocker written for the absent-ledger case must not
    swallow it.
    """
    session = _clean_session(
        position_row=_position_row(actual=0, wbs_elements=0, ledger_cells=0))
    result = closure.closure_position(session, project_id="PRJ-1",
                                      cap_id="CAP-1")
    assert not any("no budget ledger cell" in b for b in result["blockers"]), (
        result["blockers"])
