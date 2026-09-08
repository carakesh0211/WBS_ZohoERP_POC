"""Wave 6 agent 2: ``app.backend.pg.procurement_services`` -- PR -> PO on PostgreSQL.

Two layers, and the split is the same one every Wave 3-6 file makes for the
same reason.

**Layer 1 runs everywhere.** The money guard, the line normaliser, the cell
aggregation, the optimistic-concurrency comparison, the error shape and the
emission-boundary refusals are pure functions and are proved here on every
machine.

**Layer 2 needs a live PostgreSQL and is marked ``@pytest.mark.pg``.** It
covers what only a server can answer: does the derived-total trigger actually
fire, does ``lock_affected_cells`` actually take the whole ancestor chain, does
a maker-checker refusal actually stop the UPDATE, does an out-of-scope project
actually answer as absent. There is no PostgreSQL on any workstation here, so
every one of those SKIPS locally and first executes in CI's ``pg_tests`` job.

**A SKIP IS NOT A PASS.** Every live test below is gated on ``CAPEX_DB_URL``
with a reason that says so out loud. Nothing in this file reports success
against a database that was never there.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
===========================================

That the ``project.status`` / ``wbs_element.status`` procurement gates run.
They cannot: ``domain.lifecycle_permits`` reads a ``lifecycle_state`` table
that no PostgreSQL migration creates. :func:`procurement.lifecycle_gate`
reports its own absence instead, and the tests below assert THAT -- a check
that claimed the gate had run would be the defect, not the coverage.
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
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from app.backend.integration import outbound as ob  # noqa: E402
from app.backend.pg import procurement_services as proc  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
           "database. This test SKIPS -- it does not pass.",
)

NOW = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
DOCUMENT_DATE = date(2026, 9, 7)


# ==========================================================================
# Layer 1 -- pure, no database
# ==========================================================================

def test_money_must_already_be_integer_paise():
    """Not a coercion point. Rupee parsing belongs at the API boundary."""
    for bad in (1000.0, "1000", Decimal("1000"), True, None):
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc._as_paise(bad, field="amount_paise")
        assert excinfo.value.code == "NON_INTEGER_MONEY", bad
        assert excinfo.value.status == 422

    assert proc._as_paise(1000, field="amount_paise") == 1000


def test_a_boolean_is_not_one_paisa():
    """`isinstance(True, int)` is True in Python, so this needs its own guard
    and its own test; without both, `True` becomes a one-paisa commitment."""
    with pytest.raises(proc.ProcurementError):
        proc._as_paise(True, field="amount_paise")


def test_lines_are_numbered_in_input_order_and_never_by_the_caller():
    """`ux_pr_line_number` makes "line 3" mean one row. A caller-supplied
    number is a caller-supplied collision."""
    lines = proc._normalise_lines(
        [{"wbs_id": "W1", "budget_head_id": "H1", "amount_paise": 10,
          "line_no": 99},
         {"wbs_id": "W2", "budget_head_id": "H1", "amount_paise": 20}],
        what="purchase request")

    assert [line["line_no"] for line in lines] == [1, 2]


def test_a_line_with_no_control_cell_is_refused():
    """An unchecked commitment is the failure this product exists to prevent."""
    with pytest.raises(proc.ProcurementError) as excinfo:
        proc._normalise_lines([{"wbs_id": "", "budget_head_id": "H1",
                                "amount_paise": 10}],
                              what="purchase request")
    assert excinfo.value.code == "LINE_CELL_REQUIRED"


def test_a_document_with_no_lines_is_refused():
    with pytest.raises(proc.ProcurementError) as excinfo:
        proc._normalise_lines([], what="purchase request")
    assert excinfo.value.code == "NO_LINES"


def test_a_negative_line_amount_is_refused_on_a_request():
    """`ck_pr_line_amount_nonneg`. Signed money lives on grn_line and
    bill_line, where reversals and credit notes do -- and nowhere else on
    this chain."""
    with pytest.raises(proc.ProcurementError) as excinfo:
        proc._normalise_lines([{"wbs_id": "W1", "budget_head_id": "H1",
                                "amount_paise": -1}],
                              what="purchase request")
    assert excinfo.value.code == "NEGATIVE_LINE_AMOUNT"


def test_amounts_are_aggregated_per_control_cell():
    """Two lines on the same cell are one commitment against it."""
    lines = proc._normalise_lines(
        [{"wbs_id": "W1", "budget_head_id": "H1", "amount_paise": 10},
         {"wbs_id": "W1", "budget_head_id": "H1", "amount_paise": 15},
         {"wbs_id": "W2", "budget_head_id": "H1", "amount_paise": 20}],
        what="purchase request")

    assert proc._amounts_by_cell(lines) == {("W1", "H1"): 25, ("W2", "H1"): 20}


def test_the_affected_cell_set_is_deduplicated_and_ordered_by_first_use():
    """`lock_affected_cells` is called ONCE with the COMPLETE set. A duplicate
    pair in that list is harmless but a missing one is the correctness hole."""
    lines = proc._normalise_lines(
        [{"wbs_id": "W1", "budget_head_id": "H1", "amount_paise": 10},
         {"wbs_id": "W2", "budget_head_id": "H1", "amount_paise": 10},
         {"wbs_id": "W1", "budget_head_id": "H1", "amount_paise": 10}],
        what="purchase request")

    assert proc._affected_cells(lines) == [("W1", "H1"), ("W2", "H1")]


def test_a_stale_version_is_a_conflict_not_a_silent_overwrite():
    with pytest.raises(proc.ProcurementError) as excinfo:
        proc._assert_version(1, 3, label="PR-0001")
    assert excinfo.value.code == proc.ERR_VERSION_CONFLICT
    assert excinfo.value.status == 409

    proc._assert_version(3, 3, label="PR-0001")     # matching: no refusal
    proc._assert_version(None, 3, label="PR-0001")  # unstated: no refusal


def test_the_error_carries_a_code_a_status_and_a_message():
    exc = proc.ProcurementError("SOME_CODE", "some message", status=409)
    assert (exc.code, exc.status, exc.message) == ("SOME_CODE", 409,
                                                   "some message")
    assert "SOME_CODE" in str(exc)


def test_exceeds_budget_reads_the_verdicts_it_is_given():
    assert proc.exceeds_budget([{"verdict": "OK"}]) is False
    assert proc.exceeds_budget([{"verdict": "OK"},
                                {"verdict": "EXCEEDS_BUDGET"}]) is True


def test_the_lifecycle_gate_names_its_own_absence_rather_than_passing():
    """The constant a caller reads when `lifecycle_state` is not there. It is
    a SENTENCE, not a boolean: "True" would be indistinguishable from the gate
    having run and permitted the spend."""
    assert "UNAVAILABLE" in proc.LIFECYCLE_UNAVAILABLE
    assert "lifecycle_state" in proc.LIFECYCLE_UNAVAILABLE
    assert proc.LIFECYCLE_UNAVAILABLE != proc.LIFECYCLE_PERMITTED


def test_the_module_reads_the_domain_status_sets_rather_than_restating_them():
    """Two engines must not drift on what releases commitment or what makes a
    bill accounting-effective."""
    from app.backend import domain

    assert set(proc.COMMITMENT_RELEASING_STATES) == \
        domain.COMMITMENT_RELEASING_STATES
    assert set(proc.ACCOUNTING_EFFECTIVE_BILL_STATES) == \
        domain.ACCOUNTING_EFFECTIVE_BILL_STATES


def test_the_approvable_statuses_are_the_ones_the_sqlite_service_approves():
    """`services.approve_pr` refuses anything outside these three, and 013's
    `ck_purchase_request_status` admits all three."""
    assert proc.APPROVABLE_STATUSES == ("Submitted", "Under Review",
                                        "Exception Pending")


def test_the_emission_module_is_a_constant_not_a_parameter():
    """`derive_dedupe_key` is a function of (connection, module, local_id). A
    module a caller could vary between the enqueue and the send would change
    the key and produce a second purchase order for one commitment."""
    assert proc.PO_MODULE == "purchaseorders"


# ==========================================================================
# Layer 2 -- live PostgreSQL
# ==========================================================================

def _seed_chain(con, *, suffix: str, budget_paise: int = 10_000_00):
    """A two-level WBS chain with the budget on the ROOT.

    The child carries a control cell with no budget of its own, so a spend on
    the child rolls up to the root -- which is what makes the ancestor-chain
    lock test meaningful: the nearest budget-OWNING ancestor is the root, and
    a correct lock set contains BOTH cells, not just the owner.
    """
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    root, child, head = f"WR_{suffix}", f"WC_{suffix}", f"H_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
       (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
       (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,'t','t')",
       (head, ent, f"HC_{suffix}", "Head"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
       "wbs_path, level, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,%s,0,'t','t')", (root, prj, root, "root", root))
    ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, wbs_code, "
       "description, wbs_path, level, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,%s,%s,1,'t','t')",
       (child, prj, root, child, "child", f"{root}.{child}"))
    for wbs, budget in ((root, budget_paise), (child, 0)):
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_paise, updated_by) VALUES (%s,%s,%s,'t')",
           (wbs, head, budget))
        ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, "
           "updated_by) VALUES (%s,%s,'t')", (wbs, head))
    con.commit()
    return {"org": org, "entity": ent, "project": prj, "root": root,
            "child": child, "head": head}


def _line(ids, amount_paise, *, wbs=None):
    return {"wbs_id": wbs or ids["child"], "budget_head_id": ids["head"],
            "amount_paise": amount_paise, "quantity": 1,
            "description": "a line"}


@PG
def test_create_pr_writes_its_lines_and_the_trigger_derives_the_header_total(
        pg_database, pg_connection):
    """GAP-1: the header keeps `amount_paise` as a DERIVED total, maintained
    by `capex_purchase_request_total_refresh()`. There must be no window in
    which the header disagrees with its lines."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        result = proc.create_pr(
            session, project_id=ids["project"], actor="U-REQ",
            lines=[_line(ids, 100_00), _line(ids, 250_00)])
        header = session.fetchone(
            "SELECT amount_paise, status, check_result FROM purchase_request "
            "WHERE pr_id = %s", (result["pr_id"],))
        lines = session.fetchall(
            "SELECT line_no, amount_paise FROM pr_line WHERE pr_id = %s "
            "ORDER BY line_no", (result["pr_id"],))

    assert header[0] == 350_00, "the trigger maintains the header total"
    assert header[1] == "Draft"
    assert header[2] == "WITHIN_BUDGET"
    assert [(row[0], row[1]) for row in lines] == [(1, 100_00), (2, 250_00)]


@PG
def test_create_pr_locks_every_budget_owning_ancestor_not_only_the_nearest(
        pg_database, pg_connection):
    """Plan section 7.3's correctness hole, asserted directly.

    Exposure rolls up the WHOLE tree, so a spend on the child reduces
    availability at every budget-owning ancestor above it. Locking only the
    nearest one lets a concurrent transaction spend the same headroom at a
    different depth.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        proc.create_pr(session, project_id=ids["project"], actor="U-REQ",
                       lines=[_line(ids, 100_00)])
        taken = list(session.locks_taken)

    assert (ids["child"], ids["head"]) in taken, "the spend's own cell"
    assert (ids["root"], ids["head"]) in taken, (
        "and the budget-owning ancestor above it -- locking only the nearest "
        "cell is the hole plan section 7.3 records")


@PG
def test_reserve_true_is_refused_because_pr_reservation_has_no_pg_table(
        pg_database, pg_connection):
    """The caller asked for budget to be held. There is nowhere to hold it, so
    the request is NOT created. Ignoring the flag would report a reservation
    that nothing is reserving."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.create_pr(session, project_id=ids["project"], actor="U-REQ",
                           lines=[_line(ids, 100_00)], reserve=True)
        rows = session.fetchall("SELECT pr_id FROM purchase_request")

    assert excinfo.value.code == proc.ERR_RESERVATION_UNAVAILABLE
    assert excinfo.value.status == 501
    assert rows == [], "nothing was written"


@PG
def test_pr_reservation_really_has_no_postgresql_table(pg_connection):
    """The premise of the refusal above, checked rather than asserted in prose.
    If a later migration creates the table this test fails, which is the
    reminder that `reserve=True` should then start working."""
    row = pg_connection.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'pr_reservation'"
    ).fetchone()
    assert row is None


@PG
def test_the_lifecycle_gate_reports_unavailable_rather_than_claiming_to_pass(
        pg_database, pg_connection):
    """`lifecycle_state` has no PostgreSQL table, so two thirds of
    `domain.budget_check`'s lifecycle refusal cannot run. The result says so."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        result = proc.create_pr(session, project_id=ids["project"],
                                actor="U-REQ", lines=[_line(ids, 100_00)])

    assert result["lifecycle_gate"] == proc.LIFECYCLE_UNAVAILABLE


@PG
def test_an_abandoned_wbs_is_refused(pg_database, pg_connection):
    """The one third that DOES port: `is_abandoned` is a boolean column, not a
    lookup, so `domain.budget_check`'s WBS_ABANDONED refusal survives."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)
    pg_connection.execute(
        "UPDATE wbs_element SET is_abandoned = true WHERE wbs_id = %s",
        (ids["child"],))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.create_pr(session, project_id=ids["project"], actor="U-REQ",
                           lines=[_line(ids, 100_00)])

    assert excinfo.value.code == "WBS_ABANDONED"


@PG
def test_two_lines_under_one_owner_are_checked_as_their_sum(
        pg_database, pg_connection):
    """The strengthening GAP-1 forced.

    Two lines of 60% of the pot each, on DIFFERENT WBS elements rolling up to
    the SAME budget owner. Checked independently they both pass and the pair
    overspends by 20%. `budget_verdicts` resolves the owner first and checks
    the SUM.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=1_000_00)

    with pg_database.session(Scope.system()) as session:
        # One line on the child, one on the root: two cells, one owner.
        result = proc.create_pr(
            session, project_id=ids["project"], actor="U-REQ",
            lines=[_line(ids, 600_00),
                   _line(ids, 600_00, wbs=ids["root"])])

    assert result["check_result"] == "EXCEEDS_BUDGET", (
        "1,200 paise requested against a 1,000-paise pot must exceed it, "
        "however the lines are distributed under the owner")
    assert len(result["verdicts"]) == 1, (
        "both cells roll up to one owner, so there is one verdict about one "
        "pot -- not two verdicts each ignorant of the other")


@PG
def test_submit_moves_a_within_budget_request_to_submitted(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        submitted = proc.submit_pr(session, pr_id=created["pr_id"],
                                   actor="U-REQ")

    assert submitted["status"] == "Submitted"
    assert submitted["check_result"] == "WITHIN_BUDGET"


@PG
def test_submit_moves_an_over_budget_request_to_exception_pending(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_00)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 500_00)])
        submitted = proc.submit_pr(session, pr_id=created["pr_id"],
                                   actor="U-REQ")

    assert submitted["status"] == "Exception Pending"
    assert submitted["check_result"] == "EXCEEDS_BUDGET"


@PG
def test_a_maker_cannot_approve_their_own_request(pg_database, pg_connection):
    """Maker-checker, at the service, with no router in the way."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-REQ",
                principal={"user_id": "U-REQ", "roles": ["ProcurementApprover"]})
        status = session.fetchone(
            "SELECT status FROM purchase_request WHERE pr_id = %s",
            (created["pr_id"],))[0]

    assert excinfo.value.status == 403
    assert excinfo.value.code in ("SELF_APPROVAL", proc.ERR_SELF_APPROVAL)
    assert status != "Approved", "the refusal must stop the UPDATE"


@PG
def test_a_delegation_cannot_launder_a_self_approval(pg_database, pg_connection):
    """BOTH identities are checked against the contributor set.

    `require_separation` compares one identity against `requested_by` and
    cannot see the delegated one. An approver acting FOR the requester would
    otherwise route the maker's own decision through somebody else -- which is
    a self-approval with an extra hop.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-PROC",
                acting_for_user_id="U-REQ",
                principal={"user_id": "U-PROC",
                           "roles": ["ProcurementApprover"]})
        status = session.fetchone(
            "SELECT status FROM purchase_request WHERE pr_id = %s",
            (created["pr_id"],))[0]

    assert excinfo.value.code == proc.ERR_SELF_APPROVAL
    assert excinfo.value.status == 403
    assert excinfo.value.detail["acting_for_user_id"] == "U-REQ"
    assert status != "Approved"


@PG
def test_an_independent_approver_may_approve(pg_database, pg_connection):
    """The negative control for the two refusals above: if this failed too,
    they would be proving that approval is broken rather than that
    segregation of duties works."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        approved = proc.approve_pr(
            session, pr_id=created["pr_id"], actor="U-PROC",
            principal={"user_id": "U-PROC", "roles": ["ProcurementApprover"]})

    assert approved["status"] == "Approved"
    assert approved["approver"] == "U-PROC"


@PG
def test_a_procurement_approver_cannot_approve_an_exception(
        pg_database, pg_connection):
    """An EXCEEDS_BUDGET request needs `pr.approve_exception`, which only
    FinanceApprover holds. Holding the ordinary approval permission is not
    authority over an over-budget commitment."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_00)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 500_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-PROC",
                reason="board approved",
                principal={"user_id": "U-PROC",
                           "roles": ["ProcurementApprover"]})

    assert excinfo.value.status == 403
    assert excinfo.value.code == "FORBIDDEN"


@PG
def test_an_exception_approval_without_a_reason_is_refused(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=100_00)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 500_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-FIN", reason="  ",
                principal={"user_id": "U-FIN", "roles": ["FinanceApprover"]})

    assert excinfo.value.code == "REASON_REQUIRED"


@PG
def test_budget_moved_between_submission_and_approval_refuses_the_approval(
        pg_database, pg_connection):
    """The re-check happens INSIDE the lock, at write time, because
    availability moves. This is `services.approve_pr`'s BUDGET_MOVED pattern,
    preserved."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=1_000_00)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 800_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")

    # Somebody else spends the headroom between submission and approval.
    # 90,000 paise, written as a plain SQL integer: `900_00` is Python's digit
    # separator and is a syntax error to PostgreSQL, not a number.
    pg_connection.execute(
        "UPDATE budget_ledger_cell SET commitment_paise = 90000 "
        "WHERE wbs_id = %s AND budget_head_id = %s", (ids["root"], ids["head"]))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-PROC",
                principal={"user_id": "U-PROC",
                           "roles": ["ProcurementApprover"]})
        status = session.fetchone(
            "SELECT status FROM purchase_request WHERE pr_id = %s",
            (created["pr_id"],))[0]

    assert excinfo.value.code == proc.ERR_BUDGET_MOVED
    assert excinfo.value.status == 409
    assert status != "Approved", "no overspend was written"


@PG
def test_a_stale_version_no_refuses_the_approval(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        proc.submit_pr(session, pr_id=created["pr_id"], actor="U-REQ")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.approve_pr(
                session, pr_id=created["pr_id"], actor="U-PROC",
                expected_version=1,
                principal={"user_id": "U-PROC",
                           "roles": ["ProcurementApprover"]})

    assert excinfo.value.code == proc.ERR_VERSION_CONFLICT
    assert excinfo.value.status == 409


# ---------------------------------------------------------------- conversion

def _approved_pr(session, ids, amount=100_00, actor="U-REQ"):
    created = proc.create_pr(session, project_id=ids["project"], actor=actor,
                             lines=[_line(ids, amount)])
    proc.submit_pr(session, pr_id=created["pr_id"], actor=actor)
    proc.approve_pr(session, pr_id=created["pr_id"], actor="U-PROC",
                    principal={"user_id": "U-PROC",
                               "roles": ["ProcurementApprover"]})
    return created["pr_id"]


@PG
def test_conversion_requires_an_approved_request(pg_database, pg_connection):
    """Approval-driven: a Draft or Submitted request is not a commitment."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.convert_pr_to_po(session, pr_id=created["pr_id"],
                                  actor="U-PROC", vendor_name="Acme")

    assert excinfo.value.code == "PR_NOT_APPROVED"
    assert excinfo.value.status == 409


@PG
def test_a_request_converts_exactly_once(pg_database, pg_connection):
    """AUD-H-001, and the guard is now TWO deep rather than one.

    `ix_purchase_order_pr` was a PLAIN index, so the only thing stopping a
    second conversion was the request's own row lock plus the question asked
    under it -- application discipline, which is what
    `014_procurement_corrections.sql` D6 replaces with
    `ux_purchase_order_pr UNIQUE (pr_id) WHERE pr_id IS NOT NULL`.

    This test still asserts the APPLICATION's answer, deliberately: a caller
    must get `PR_ALREADY_CONVERTED` naming the existing order, not a raw 23505
    from three frames down. The index is the backstop underneath it, proved
    separately in `tests/test_pg_migration_014.py`.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        pr_id = _approved_pr(session, ids)
        first = proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC",
                                      vendor_name="Acme")
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC",
                                  vendor_name="Acme")
        orders = session.fetchall(
            "SELECT po_id FROM purchase_order WHERE pr_id = %s", (pr_id,))

    assert excinfo.value.code == "PR_ALREADY_CONVERTED"
    assert excinfo.value.detail["po_id"] == first["po_id"]
    assert len(orders) == 1


@PG
def test_conversion_copies_each_line_onto_the_cell_it_was_checked_against(
        pg_database, pg_connection):
    """Plan section 11.7. Structural rather than validated: the PO line is
    built FROM the PR line, so the cell cannot be re-supplied wrongly."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        pr_id = _approved_pr(session, ids)
        converted = proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC",
                                          vendor_name="Acme")
        pr_cells = session.fetchall(
            "SELECT wbs_id, budget_head_id, amount_paise FROM pr_line "
            "WHERE pr_id = %s ORDER BY line_no", (pr_id,))
        po_cells = session.fetchall(
            "SELECT wbs_id, budget_head_id, amount_paise FROM po_line "
            "WHERE po_id = %s ORDER BY line_no", (converted["po_id"],))

    assert po_cells == pr_cells


@PG
def test_a_purchase_order_raises_the_commitment_the_next_check_reads(
        pg_database, pg_connection):
    """The hole this stream closed -- HALF of it.

    `budget_ledger_cell.commitment_paise` had NO writer in the PostgreSQL
    path: `recompute_cell` derives only the budget columns. So availability
    never fell, and the same pot could be committed an unbounded number of
    times -- the re-check inside the lock was re-checking a number nothing
    moved.

    The OTHER half stayed open until migration 014: `actual_paise` had no
    writer either, and `commitment` FALLS when a bill arrives. So availability
    rose by the billed amount and the pot could be committed again anyway.
    `tests/test_pg_migration_014.py::test_a_bill_landing_does_not_raise_available_live`
    is that half; this one is unchanged and still proves an order lowers
    availability.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=1_000_00)

    with pg_database.session(Scope.system()) as session:
        before = proc._availability(session, ids["child"], ids["head"], 0)
        pr_id = _approved_pr(session, ids, amount=600_00)
        proc.convert_pr_to_po(session, pr_id=pr_id, actor="U-PROC",
                              vendor_name="Acme")
        after = proc._availability(session, ids["child"], ids["head"], 0)

    assert before["commitment_paise"] == 0
    assert after["commitment_paise"] == 600_00
    assert after["available_paise"] == before["available_paise"] - 600_00


@PG
def test_a_second_order_cannot_spend_the_headroom_the_first_took(
        pg_database, pg_connection):
    """The consequence of the test above, as a control rather than a number."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=1_000_00)

    with pg_database.session(Scope.system()) as session:
        proc.create_po(session, project_id=ids["project"], vendor_name="Acme",
                       actor="U-PROC", lines=[_line(ids, 600_00)])
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.create_po(session, project_id=ids["project"],
                           vendor_name="Acme", actor="U-PROC",
                           lines=[_line(ids, 600_00)])
        orders = session.fetchall("SELECT po_id FROM purchase_order")

    assert excinfo.value.code == "PO_EXCEEDS_BUDGET"
    assert len(orders) == 1


@PG
def test_a_multi_cell_purchase_order_keeps_one_line_per_cell(
        pg_database, pg_connection):
    """`po_line` is keyed on `(wbs_id, budget_head_id)`. A multi-WBS purchase
    order is normal LOCALLY and is one header with N lines -- the split into N
    documents is a Zoho-side consequence of D-7, not a local one."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_po(
            session, project_id=ids["project"], vendor_name="Acme",
            actor="U-PROC",
            lines=[_line(ids, 100_00), _line(ids, 200_00, wbs=ids["root"])])
        headers = session.fetchall(
            "SELECT po_id FROM purchase_order WHERE po_id = %s",
            (created["po_id"],))
        lines = session.fetchall(
            "SELECT wbs_id FROM po_line WHERE po_id = %s ORDER BY line_no",
            (created["po_id"],))

    assert len(headers) == 1
    assert [row[0] for row in lines] == [ids["child"], ids["root"]]


# ------------------------------------------------------------------- scoping

@PG
def test_an_out_of_scope_project_answers_not_found_never_forbidden(
        pg_database, pg_connection):
    """A 403 on an id lookup is an existence oracle: it confirms the project is
    real to somebody who may not see it."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)
    outsider = Scope(user_id="U-OTHER", principal_kind="USER",
                     entity_ids=frozenset({f"E_OTHER_{suffix}"}),
                     plant_ids=None, project_ids=None, location_ids=None,
                     read_all=False)

    with pg_database.session(outsider) as session:
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.create_pr(session, project_id=ids["project"], actor="U-OTHER",
                           lines=[_line(ids, 100_00)])

    assert excinfo.value.status == 404
    assert excinfo.value.code == "PROJECT_NOT_FOUND"


@PG
def test_an_out_of_scope_purchase_request_answers_as_absent(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix)
    outsider = Scope(user_id="U-OTHER", principal_kind="USER",
                     entity_ids=frozenset({f"E_OTHER_{suffix}"}),
                     plant_ids=None, project_ids=None, location_ids=None,
                     read_all=False)

    with pg_database.session(Scope.system()) as session:
        created = proc.create_pr(session, project_id=ids["project"],
                                 actor="U-REQ", lines=[_line(ids, 100_00)])

    with pg_database.session(outsider) as session:
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.submit_pr(session, pr_id=created["pr_id"], actor="U-OTHER")

    assert excinfo.value.status == 404
    assert excinfo.value.code == "PR_NOT_FOUND"


# ------------------------------------------------------------------ emission

def _connection(session, ids, *, suffix):
    from app.backend.pg import integration_store as store

    connection_id = f"CONN_{suffix}"
    store.create_connection(
        session, connection_id=connection_id, entity_id=ids["entity"],
        product="ERP", dc="IN", organization_id="60000000000",
        connector_name=f"conn-{suffix}", actor="U-ADM")
    return connection_id


class _Caps:
    """Header-only dimensions -- D-7's own assumed value."""
    line_level_custom_fields = False


class _MetadataOnlyAdapter:
    """Capabilities and nothing else. It has no transport and cannot call
    anything, which is the shape `api/procurement.py` builds too."""

    def capabilities(self):
        return _Caps()


@PG
def test_plan_po_emission_writes_one_outbox_row_per_purchase_order(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(
            session, project_id=ids["project"], vendor_name="Acme",
            actor="U-PROC",
            lines=[_line(ids, 100_00), _line(ids, 200_00, wbs=ids["root"])])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        rows = session.fetchall(
            "SELECT local_id, dedupe_key, state FROM integration_outbox "
            "WHERE connection_id = %s ORDER BY local_id", (connection_id,))

    assert planned["purchase_orders"] == 2
    assert planned["process_change"]["code"] == "HEADER_ONLY_PO_SPLIT"
    assert len(rows) == 2
    assert len({row[1] for row in rows}) == 2, "two distinct cf_capex_ref values"
    assert {row[2] for row in rows} == {"PENDING"}


@PG
def test_replanning_the_same_purchase_order_reuses_its_outbox_rows(
        pg_database, pg_connection):
    """At most one LOGICAL purchase order across retries, at the enqueue step.
    `uq_integration_outbox_local_id` is what makes the second plan a no-op
    rather than a second commitment."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        kwargs = dict(po_id=created["po_id"], connection_id=connection_id,
                      adapter=_MetadataOnlyAdapter(),
                      vendor_external_id="ZV-77",
                      document_date=DOCUMENT_DATE, actor="U-ADM",
                      acknowledged=True)
        first = proc.plan_po_emission(session, **kwargs)
        second = proc.plan_po_emission(session, **kwargs)
        rows = session.fetchall(
            "SELECT outbox_id FROM integration_outbox WHERE connection_id = %s",
            (connection_id,))

    assert first["outbox"][0]["created"] is True
    assert second["outbox"][0]["created"] is False
    assert second["outbox"][0]["outbox_id"] == first["outbox"][0]["outbox_id"]
    assert len(rows) == 1


@PG
def test_an_unacknowledged_split_writes_no_outbox_row_at_all(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(
            session, project_id=ids["project"], vendor_name="Acme",
            actor="U-PROC",
            lines=[_line(ids, 100_00), _line(ids, 200_00, wbs=ids["root"])])
        with pytest.raises(proc.ProcurementError) as excinfo:
            proc.plan_po_emission(
                session, po_id=created["po_id"], connection_id=connection_id,
                adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
                document_date=DOCUMENT_DATE, actor="U-ADM")
        rows = session.fetchall(
            "SELECT outbox_id FROM integration_outbox WHERE connection_id = %s",
            (connection_id,))

    assert excinfo.value.code == "PROCESS_CHANGE_UNACKNOWLEDGED"
    assert rows == []


class _Boom(RuntimeError):
    """A transport failure carrying Zoho's body-level code."""

    def __init__(self, status, code=None):
        self.status_code = status
        self.code = code
        super().__init__(f"HTTP {status} code={code}")


class _FailingAdapter(_MetadataOnlyAdapter):
    def __init__(self, exc):
        self.exc = exc

    def resolve_by_dedupe_key(self, dedupe_key):
        return None

    def create_purchase_order(self, po, dedupe_key):
        raise self.exc


@PG
def test_send_reports_a_failure_instead_of_raising_it(
        pg_database, pg_connection):
    """`Database.session` rolls back on an exception, so an escaping error
    would roll back the very row recording that the attempt happened -- leaving
    `attempts` at zero and the row retrying for ever."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        result = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_FailingAdapter(_Boom(502)), actor="U-ADM", now=NOW)

    with pg_database.session(Scope.system()) as session:
        row = session.fetchone(
            "SELECT state, attempts FROM integration_outbox WHERE outbox_id = %s",
            (outbox_id,))

    assert result["sent"] is False
    assert result["failure_kind"] == "TRANSIENT"
    assert row[0] == "FAILED"
    assert row[1] == 1, "the attempt survived the transaction"


@PG
def test_a_daily_quota_refusal_costs_no_attempt_and_opens_the_circuit(
        pg_database, pg_connection):
    """429/45. There is no backoff that helps before midnight UTC, and the
    attempt does not count -- our budget ran out, Zoho did nothing wrong."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        result = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_FailingAdapter(_Boom(429, 45)), actor="U-ADM", now=NOW)

    with pg_database.session(Scope.system()) as session:
        row = session.fetchone(
            "SELECT attempts FROM integration_outbox WHERE outbox_id = %s",
            (outbox_id,))
        circuit = session.fetchone(
            "SELECT state FROM integration_circuit WHERE connection_id = %s "
            "AND module = %s", (connection_id, proc.PO_MODULE))

    assert result["failure_kind"] == "RATE_LIMIT_DAILY"
    assert result["counts_toward_attempts"] is False
    assert row[0] == 0, "the attempt was given back"
    assert circuit[0] == "CIRCUIT_OPEN"


@PG
def test_a_duplicate_refusal_does_not_move_the_circuit(
        pg_database, pg_connection):
    """Z-01 refusing a second record is the idempotency design WORKING.

    It arrives as an exception because that is how Zoho reports a unique-field
    violation. Counting it toward the breaker would open the circuit on the one
    outcome that proves the mechanism works.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    class _AlreadyThere(_MetadataOnlyAdapter):
        def resolve_by_dedupe_key(self, dedupe_key):
            return None

        def create_purchase_order(self, po, dedupe_key):
            raise ob.DuplicateDedupeKey(dedupe_key, "ZPO-00042")

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        result = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_AlreadyThere(), actor="U-ADM", now=NOW)
        circuit = session.fetchone(
            "SELECT state FROM integration_circuit WHERE connection_id = %s "
            "AND module = %s", (connection_id, proc.PO_MODULE))

    assert result["refused"] == "DUPLICATE_DEDUPE_KEY"
    assert result["external_id"] == "ZPO-00042"
    assert circuit is None, "no circuit row was written at all"


@PG
def test_an_unclaimable_row_is_reported_without_moving_the_circuit(
        pg_database, pg_connection):
    """"Another worker holds this row" is our own state, not Zoho's health."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        # Push it DEAD, which `CLAIMABLE_STATES` excludes.
        session.execute(
            "UPDATE integration_outbox SET state = 'DEAD', "
            "attempts = max_attempts, next_attempt_at = NULL "
            "WHERE outbox_id = %s", (outbox_id,))
        result = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), actor="U-ADM", now=NOW)
        circuit = session.fetchone(
            "SELECT state FROM integration_circuit WHERE connection_id = %s "
            "AND module = %s", (connection_id, proc.PO_MODULE))

    assert result["sent"] is False
    assert result["refused"] == "NOT_EMITTABLE"
    assert circuit is None


@PG
def test_an_open_circuit_refuses_before_a_call_is_made(
        pg_database, pg_connection):
    """The point of having one: the second row does not spend a call
    discovering what the first already established."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_FailingAdapter(_Boom(429, 45)), actor="U-ADM", now=NOW)

        class _MustNotBeCalled(_MetadataOnlyAdapter):
            def create_purchase_order(self, po, dedupe_key):
                raise AssertionError("an open circuit must make no call")

        refused = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_MustNotBeCalled(), actor="U-ADM",
            now=NOW + timedelta(seconds=1))

    assert refused["sent"] is False
    assert refused["refused"] == "CIRCUIT_OPEN"


@PG
def test_a_successful_send_records_the_external_id_and_closes_the_circuit(
        pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    class _Ok(_MetadataOnlyAdapter):
        def resolve_by_dedupe_key(self, dedupe_key):
            return None

        def create_purchase_order(self, po, dedupe_key):
            assert not isinstance(po, dict), (
                "both shipped adapters read the document by attribute")
            assert po.vendor_external_id == "ZV-77"
            return "ZPO-00001"

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        result = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=_Ok(), actor="U-ADM", now=NOW)
        row = session.fetchone(
            "SELECT state, external_id FROM integration_outbox "
            "WHERE outbox_id = %s", (outbox_id,))
        circuit = session.fetchone(
            "SELECT state FROM integration_circuit WHERE connection_id = %s "
            "AND module = %s", (connection_id, proc.PO_MODULE))

    assert result["sent"] is True
    assert result["external_id"] == "ZPO-00001"
    assert row == ("SENT", "ZPO-00001")
    assert circuit[0] == "CIRCUIT_CLOSED"


@PG
def test_a_settled_outbox_row_costs_no_second_call(pg_database, pg_connection):
    """At most one logical purchase order, at the SEND step: re-draining a
    chunk must report the settled result and make no call."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)
    calls: list[str] = []

    class _CountingAdapter(_MetadataOnlyAdapter):
        def resolve_by_dedupe_key(self, dedupe_key):
            calls.append("resolve")
            return None

        def create_purchase_order(self, po, dedupe_key):
            calls.append("create")
            return "ZPO-00001"

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 100_00)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        outbox_id = planned["outbox"][0]["outbox_id"]
        adapter = _CountingAdapter()
        first = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=adapter, actor="U-ADM", now=NOW)
        after_first = len(calls)
        second = proc.send_purchase_order(
            session, outbox_id=outbox_id, connection_id=connection_id,
            adapter=adapter, actor="U-ADM", now=NOW + timedelta(minutes=1))

    assert first["sent"] is True
    assert second["sent"] is True
    assert second["external_id"] == first["external_id"]
    assert len(calls) == after_first, (
        "a settled outbox row must cost zero API calls to re-observe")


@PG
def test_the_planned_payload_survives_the_round_trip_through_jsonb(
        pg_database, pg_connection):
    """`integration_outbox.payload` is `jsonb`, which has no dates and no
    tuples. The fields at risk of being lost across it are monetary."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_chain(pg_connection, suffix=suffix, budget_paise=10_000_00)

    with pg_database.session(Scope.system()) as session:
        connection_id = _connection(session, ids, suffix=suffix)
        created = proc.create_po(session, project_id=ids["project"],
                                 vendor_name="Acme", actor="U-PROC",
                                 lines=[_line(ids, 123_45)])
        planned = proc.plan_po_emission(
            session, po_id=created["po_id"], connection_id=connection_id,
            adapter=_MetadataOnlyAdapter(), vendor_external_id="ZV-77",
            document_date=DOCUMENT_DATE, actor="U-ADM", acknowledged=True)
        entry = planned["outbox"][0]
        store = proc.PgOutboxStore(session, connection_id=connection_id,
                                   actor="U-ADM")
        record = store.get(entry["outbox_id"])
        document = ob.emission_dto_from_record(record)

    assert document.total_paise == 123_45
    assert document.document_date == DOCUMENT_DATE
    assert document.lines[0].line_total_paise == 123_45
