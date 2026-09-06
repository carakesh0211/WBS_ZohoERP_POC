"""Submit -> route -> decide -> write back, against a real PostgreSQL.

Wave 4 stream A2. The two ends of the approval gap, joined: a DRAFT revision
is submitted, the engine routes it, an approver decides it, and the decision
lands on the document with a ``budget_line`` behind it.

These run only where ``CAPEX_DB_URL`` is set -- CI's ``postgres:16`` service.
They SKIP on the development machine, which has no local PostgreSQL, so a green
local run is **not** evidence that they passed. Everything provable on a double
is in ``tests/test_budget_submission.py`` and
``tests/test_approval_writeback.py``, both of which run everywhere; what is
left here is the part only a server can settle: that the CHECK constraints
accept what the write-back writes, that ``recompute_cell`` really moves the
cell, and that an EXCEPTION_PENDING instance really survives the commit.

One thing these tests do that production will not
-------------------------------------------------
``apply_outcome`` is invoked explicitly here, immediately after ``decide``.
The call site inside ``pg/approvals.py`` -- one call, at the single point an
instance closes -- belongs to stream A1 and is not in this worktree. Invoking
it by hand tests the contract this stream owns (what the write-back does to a
document, given a closed instance) without pretending to test the wiring. When
A1's call lands, ``test_the_engine_calls_the_write_back_itself`` stops skipping
and covers the join.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see the note
# in tests/test_pg_approval_concurrency.py for why this cannot be left implicit.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import inspect                                                   # noqa: E402
import os                                                        # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date                                        # noqa: E402

import pytest                                                    # noqa: E402
from psycopg.types.json import Jsonb                             # noqa: E402

from app.backend.pg import approval_rules as rules               # noqa: E402
from app.backend.pg import approval_writeback as wb              # noqa: E402
from app.backend.pg import approvals as engine                   # noqa: E402
from app.backend.pg import budget as budget_mod                   # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        not os.environ.get("CAPEX_DB_URL"),
        reason="needs a live PostgreSQL (CAPEX_DB_URL)"),
]


# ==========================================================================
# Gate and fixtures
# ==========================================================================
@pytest.fixture()
def approval_schema(pg_connection):
    """Skip cleanly where migration 008's tables are absent."""
    missing = [name for name in ("approval_definition", "approval_instance",
                                 "approval_stage_instance", "approval_assignment",
                                 "approval_action")
               if pg_connection.execute(
                   "SELECT to_regclass(%s)", (name,)).fetchone()[0] is None]
    if missing:
        pytest.skip(f"approval schema not present ({', '.join(missing)}); "
                    f"migrations/pg/008_approval_engine.sql has not been applied")
    return True


def _scope() -> Scope:
    """Unrestricted: these tests are about the write-back, and a scope refusal
    here would be a different test failing. Scope behaviour has its own
    coverage in `tests/test_pg_scope_leakage.py` and the negative matrices."""
    return Scope(user_id="U-TEST-WRITEBACK", principal_kind="USER", read_all=True)


def _seed_estate(con, suffix, *, budget_paise=100_000_000, commitment=0):
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    wbs, wbs2 = f"W_{suffix}", f"X_{suffix}"
    head = f"H_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (head, ent, f"HC_{suffix}", "Head"))
    for node in (wbs, wbs2):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
           "wbs_path, created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')",
           (node, prj, node, "n", node))
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, "
           "updated_by) VALUES (%s,%s,%s,'t')", (node, head, budget_paise))
        # ...and the ORIGINAL grant it summarises.
        #
        # `budget_control_cell` is MATERIALISED, not authoritative:
        # `budget.recompute_cell` derives `budget_paise` by summing
        # `budget_line`. Seeding the cell alone produced an estate whose
        # summary could not be re-derived from its own ledger, so the first
        # recompute after an approved revision correctly discarded the
        # fabricated figure -- and the resulting "the cell moved to the wrong
        # number" failure looked like a write-back defect rather than a fixture
        # that had described an impossible state.
        #
        # Inserted directly rather than through `budget.record_original`
        # because this helper takes a raw connection, not a Session. The row is
        # the same shape that function writes: kind ORIGINAL, positive, and
        # effective well before any test's business date.
        if budget_paise:
            ex("INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, "
               "kind, amount_paise, effective_from, status, created_by, updated_by) "
               "VALUES (%s,%s,%s,'ORIGINAL',%s,DATE '2020-01-01','Approved','t','t')",
               (f"BL_ORIG_{node}", node, head, budget_paise))
        ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, commitment_paise, "
           "actual_paise, pr_reserved_paise, updated_by) VALUES (%s,%s,%s,0,0,'t')",
           (node, head, commitment))
    con.commit()
    return {"entity": ent, "project": prj, "wbs": wbs, "wbs2": wbs2, "head": head}


def _seed_users(con, users):
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind, "
        "created_by, updated_by) "
        "VALUES ('SYSTEM','system@example.test','System','SERVICE','t','t') "
        "ON CONFLICT (user_id) DO NOTHING")
    for user_id, roles in users.items():
        con.execute(
            "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
            "VALUES (%s,%s,%s,'t','t') ON CONFLICT (user_id) DO NOTHING",
            (user_id, f"{user_id}@example.test", user_id))
        for role in roles:
            con.execute(
                "INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s,%s,'t') "
                "ON CONFLICT (user_id, role) DO NOTHING", (user_id, role))
    con.commit()


def _seed_definition(con, suffix, *, entity, stages, object_type="BUDGET_REVISION"):
    definition_id = f"AD_{suffix}_{object_type[:3]}"
    con.execute(
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        "status, entity_id, effective_from, created_by, activated_at, activated_by) "
        "VALUES (%s,%s,%s,1,%s,%s,%s,'TEST',now(),'TEST')",
        (definition_id, object_type, f"CODE_{suffix}_{object_type[:3]}",
         rules.DEF_ACTIVE, entity, date(2020, 1, 1)))
    con.execute(
        "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
        "created_by) VALUES (%s,%s,10,%s,'TEST')",
        (f"AR_{suffix}_{object_type[:3]}", definition_id, Jsonb({"op": "true"})))
    for spec in stages:
        stage_id = f"AS_{suffix}_{object_type[:3]}_{spec['stage_no']}"
        con.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
            "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
            "created_by) VALUES (%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,NULL,"
            "true,false,'TEST')",
            (stage_id, definition_id, spec["stage_no"], f"Stage {spec['stage_no']}",
             spec.get("quorum_type", rules.QUORUM_ANY)))
        for ordinal, (kind, ref) in enumerate(spec["approvers"], start=1):
            con.execute(
                "INSERT INTO approval_stage_approver (stage_id, ordinal, "
                "approver_kind, approver_ref, scope_expr) VALUES (%s,%s,%s,%s,NULL)",
                (stage_id, ordinal, kind, ref))
    con.commit()
    return definition_id


def _seed_revision(con, suffix, ids, *, created_by="U-MAKER", delta_paise=1_000_000):
    revision_id = f"BR_{suffix}"
    con.execute(
        "INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, "
        "delta_paise, effective_from, justification, status, created_by) "
        "VALUES (%s,%s,%s,%s,%s,'test','DRAFT',%s)",
        (revision_id, ids["wbs"], ids["head"], delta_paise, date(2026, 1, 1),
         created_by))
    con.commit()
    return revision_id


def _seed_transfer(con, suffix, ids, *, created_by="U-MAKER", amount_paise=1_000_000):
    transfer_id = f"BT_{suffix}"
    con.execute(
        "INSERT INTO budget_transfer (transfer_id, from_wbs_id, from_head_id, "
        "to_wbs_id, to_head_id, amount_paise, effective_from, justification, "
        "status, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'test','DRAFT',%s)",
        (transfer_id, ids["wbs"], ids["head"], ids["wbs2"], ids["head"],
         amount_paise, date(2026, 1, 1), created_by))
    con.commit()
    return transfer_id


def _submit_revision(pg_database, revision_id, actor="U-MAKER"):
    with pg_database.session(_scope()) as session:
        return budget_mod.submit_revision(
            session, revision_id=revision_id, actor=actor,
            business_date=date(2026, 6, 1))


def _decide(pg_database, instance_id, *, actor, action=rules.ACTION_APPROVE,
            object_version=1, key=None, reason_text=None):
    with pg_database.session(_scope()) as session:
        return engine.decide(
            session, instance_id=instance_id, actor_user_id=actor, action=action,
            idempotency_key=key or f"k-{uuid.uuid4().hex[:8]}",
            object_version=object_version, reason_text=reason_text)


def _write_back(pg_database, instance_id):
    with pg_database.session(_scope()) as session:
        instance = engine.get_instance(session, instance_id)
        wb.apply_outcome(session, instance)
        return instance


def _one_stage_definition(pg_connection, suffix, ids,
                          object_type="BUDGET_REVISION"):
    _seed_definition(pg_connection, suffix, entity=ids["entity"],
                     object_type=object_type,
                     stages=[{"stage_no": 1, "quorum_type": rules.QUORUM_ANY,
                              "approvers": [(rules.APPROVER_ROLE, "Finance")]}])


# ==========================================================================
# The happy path, end to end
# ==========================================================================
def test_a_submitted_revision_opens_an_instance_and_says_it_is_submitted(
        pg_connection, pg_database, approval_schema):
    """Task 1. The whole engine was unreachable from the application before
    this: `open_instance` was called from tests and nowhere else.

    This asserted the document STAYS DRAFT, because migration 003's CHECK
    constraint had no SUBMITTED value to move it to. The claim it carried --
    "a revision under approval must create no spending capacity" -- is true and
    is still asserted below, but DRAFT was never the thing that made it true:
    the budget line is written by `approve_revision`, and no status on the
    document creates capacity by itself. What DRAFT actually did was leave the
    row's own status asserting something false about it, with
    `_assert_not_under_approval` as the only guard against a second approval
    down the direct route.

    `009_document_approval_states.sql` widened the domain, so the row now says
    SUBMITTED and the guard is no longer alone. The no-capacity claim is
    checked directly rather than inferred from a status.
    """
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)

    result = _submit_revision(pg_database, revision_id)

    assert result["submitted"] is True
    assert result["refusal"] is None
    assert result["status"] == "SUBMITTED"

    instance_status = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (result["approval_instance_id"],)).fetchone()[0]
    assert instance_status == rules.INST_OPEN
    document_status = pg_connection.execute(
        "SELECT status FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    assert document_status == "SUBMITTED", (
        "a routed revision must say so; leaving it DRAFT makes the row's own "
        "status false and leaves one Python guard holding a property the "
        "schema can state")

    # The claim the old assertion stood in for, checked directly: routing
    # creates no spending capacity, and the database refuses to record any.
    line = pg_connection.execute(
        "SELECT budget_line_id FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    assert line is None, (
        "a revision under approval carries a budget_line; routing a document "
        "must create no spending capacity")

    # And no decision has been fabricated to satisfy the status change:
    # ck_budget_revision_decision puts SUBMITTED on the UNDECIDED side.
    decided = pg_connection.execute(
        "SELECT decided_at, decided_by FROM budget_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    assert decided == (None, None), (
        f"submission recorded a decision {decided}; a submission is not a "
        f"decision, and 009's CHECK constraint says so")


def test_an_approved_instance_writes_the_budget_line_and_moves_the_cell(
        pg_connection, pg_database, approval_schema):
    """Task 2, the whole point. Before the write-back existed the instance
    reached APPROVED and the document sat at DRAFT for ever."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids, delta_paise=5_000_000)

    submitted = _submit_revision(pg_database, revision_id)
    outcome = _decide(pg_database, submitted["approval_instance_id"], actor="U-A")
    assert outcome.instance_status == rules.INST_APPROVED

    _write_back(pg_database, submitted["approval_instance_id"])

    status, line_id, decided_by = pg_connection.execute(
        "SELECT status, budget_line_id, decided_by FROM budget_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    assert status == "APPROVED"
    assert line_id, "an APPROVED revision must carry the budget_line it produced"
    assert decided_by == "U-A", (
        "the document must be attributed to the approver who closed the "
        "instance, not to a service account")

    kind, amount = pg_connection.execute(
        "SELECT kind, amount_paise FROM budget_line WHERE budget_line_id = %s",
        (line_id,)).fetchone()
    assert kind == "REVISION"
    assert amount == 5_000_000
    assert isinstance(amount, int)

    cell_budget = pg_connection.execute(
        "SELECT budget_paise FROM budget_control_cell WHERE wbs_id = %s "
        "AND budget_head_id = %s", (ids["wbs"], ids["head"])).fetchone()[0]
    assert cell_budget == 100_000_000 + 5_000_000


def test_the_original_grant_row_is_untouched_by_an_approved_revision(
        pg_connection, pg_database, approval_schema):
    """"An APPROVED budget revision must not become spendable by a route that
    bypasses the existing controls." `approve_revision` APPENDS an
    effective-dated REVISION line; the ORIGINAL row is immutable at the
    database level. The write-back calls that function rather than writing a
    line itself, so the property is inherited, not re-implemented."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    with pg_database.session(_scope()) as session:
        budget_mod.record_original(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=100_000_000, effective_from=date(2026, 1, 1), actor="U-SEED")
    before = pg_connection.execute(
        "SELECT budget_line_id, amount_paise, effective_from FROM budget_line "
        "WHERE wbs_id = %s AND kind = 'ORIGINAL'", (ids["wbs"],)).fetchall()

    revision_id = _seed_revision(pg_connection, suffix, ids, delta_paise=2_000_000)
    submitted = _submit_revision(pg_database, revision_id)
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A")
    _write_back(pg_database, submitted["approval_instance_id"])

    after = pg_connection.execute(
        "SELECT budget_line_id, amount_paise, effective_from FROM budget_line "
        "WHERE wbs_id = %s AND kind = 'ORIGINAL'", (ids["wbs"],)).fetchall()
    assert before == after, "the ORIGINAL grant was edited rather than appended to"
    revisions = pg_connection.execute(
        "SELECT count(*), min(effective_from) FROM budget_line "
        "WHERE wbs_id = %s AND kind = 'REVISION'", (ids["wbs"],)).fetchone()
    assert revisions[0] == 1
    assert revisions[1] == date(2026, 1, 1), "the revision line must be effective-dated"


def test_applying_the_same_closed_instance_twice_leaves_the_same_state(
        pg_connection, pg_database, approval_schema):
    """A retry or a replayed decision must not apply twice -- one approval
    must not produce two budget_line rows and two increments of the cell."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids, delta_paise=4_000_000)

    submitted = _submit_revision(pg_database, revision_id)
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A")

    def snapshot_of_the_world():
        return (
            pg_connection.execute(
                "SELECT status, budget_line_id FROM budget_revision "
                "WHERE revision_id = %s", (revision_id,)).fetchone(),
            pg_connection.execute(
                "SELECT count(*) FROM budget_line WHERE wbs_id = %s AND kind = 'REVISION'",
                (ids["wbs"],)).fetchone()[0],
            pg_connection.execute(
                "SELECT budget_paise FROM budget_control_cell WHERE wbs_id = %s "
                "AND budget_head_id = %s", (ids["wbs"], ids["head"])).fetchone()[0],
        )

    _write_back(pg_database, submitted["approval_instance_id"])
    first = snapshot_of_the_world()
    _write_back(pg_database, submitted["approval_instance_id"])
    second = snapshot_of_the_world()

    assert first == second
    assert first[1] == 1, "one approval produced more than one budget_line"
    assert first[2] == 104_000_000


def test_a_rejected_instance_rejects_the_document_and_writes_no_line(
        pg_connection, pg_database, approval_schema):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)

    submitted = _submit_revision(pg_database, revision_id)
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A",
            action=rules.ACTION_REJECT, reason_text="not justified")
    _write_back(pg_database, submitted["approval_instance_id"])

    status, line_id = pg_connection.execute(
        "SELECT status, budget_line_id FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    assert status == "REJECTED"
    assert line_id is None
    # REVISION lines only. `_seed_estate` now seeds the ORIGINAL grant behind
    # the control cell -- it has to, because the cell is materialised from
    # `budget_line` and a cell with no ledger behind it is a state that cannot
    # exist. Counting every kind would assert that the estate has no budget at
    # all, which is not what this test is about.
    lines = pg_connection.execute(
        "SELECT count(*) FROM budget_line WHERE wbs_id = %s AND kind = 'REVISION'",
        (ids["wbs"],)).fetchone()[0]
    assert lines == 0, "no spending capacity may be created by this outcome"


def test_a_returned_instance_leaves_the_document_editable_and_says_why(
        pg_connection, pg_database, approval_schema):
    """C15 maps instance RETURNED to business RETURNED, and since migration 009
    the column can hold it, so the write-back writes RETURNED and records the
    reason in decision_note,
    which ck_budget_revision_decision permits on a DRAFT row where it forbids
    decided_at/decided_by. Reported to the lead as a schema gap."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)

    submitted = _submit_revision(pg_database, revision_id)
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A",
            action=rules.ACTION_RETURN, reason_text="add a quote")
    _write_back(pg_database, submitted["approval_instance_id"])

    status, note, decided_at, decided_by = pg_connection.execute(
        "SELECT status, decision_note, decided_at, decided_by FROM budget_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()

    # RETURNED, not DRAFT, and this is the point of migration 009.
    #
    # 003's CHECK constraint had no RETURNED value, so the write-back wrote
    # DRAFT and kept the real outcome in `decision_note` prose -- true, but a
    # reader could not tell a revision sent back for correction from one never
    # submitted. 009 widened the domain; the status now says it.
    assert status == "RETURNED"
    assert submitted["approval_instance_id"] in note
    assert "RETURNED" in note

    # And a return IS a decision, so it names who and when. `ck_*_decision`
    # puts RETURNED on the decided side and refuses the row otherwise.
    assert decided_at is not None, (
        "a returned revision carries no decision time; an approver did decide "
        "to send it back, at a known moment")
    assert decided_by == "U-A", (
        f"the return is attributed to {decided_by!r} rather than the approver "
        f"who took it")


def test_an_approved_transfer_moves_budget_between_both_cells(
        pg_connection, pg_database, approval_schema):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids, object_type="BUDGET_TRANSFER")
    transfer_id = _seed_transfer(pg_connection, suffix, ids, amount_paise=3_000_000)

    with pg_database.session(_scope()) as session:
        submitted = budget_mod.submit_transfer(
            session, transfer_id=transfer_id, actor="U-MAKER",
            business_date=date(2026, 6, 1))
    assert submitted["submitted"] is True
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A")
    _write_back(pg_database, submitted["approval_instance_id"])

    status = pg_connection.execute(
        "SELECT status FROM budget_transfer WHERE transfer_id = %s",
        (transfer_id,)).fetchone()[0]
    assert status == "APPROVED"
    source = pg_connection.execute(
        "SELECT budget_paise FROM budget_control_cell WHERE wbs_id = %s "
        "AND budget_head_id = %s", (ids["wbs"], ids["head"])).fetchone()[0]
    destination = pg_connection.execute(
        "SELECT budget_paise FROM budget_control_cell WHERE wbs_id = %s "
        "AND budget_head_id = %s", (ids["wbs2"], ids["head"])).fetchone()[0]
    assert source == 100_000_000 - 3_000_000
    assert destination == 100_000_000 + 3_000_000


# ==========================================================================
# The refusals, against a real server
# ==========================================================================
def test_an_unroutable_revision_is_recorded_and_survives_the_commit(
        pg_connection, pg_database, approval_schema):
    """The failure the return-instead-of-raise design exists for. No ACTIVE
    definition matches, so the engine writes an EXCEPTION_PENDING instance and
    RETURNS; the caller commits; the row is still there afterwards. Raising
    would have rolled it away with everything else."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"]})
    # No definition seeded at all.
    revision_id = _seed_revision(pg_connection, suffix, ids)

    result = _submit_revision(pg_database, revision_id)

    assert result["submitted"] is False
    assert result["approval_status"] == rules.INST_EXCEPTION_PENDING
    assert result["refusal"]["code"] == rules.ERR_ROUTE_UNRESOLVED
    assert result["refusal"]["status"] == 409

    # A SEPARATE connection, after the submitting transaction committed.
    row = pg_connection.execute(
        "SELECT status FROM approval_instance WHERE instance_id = %s",
        (result["approval_instance_id"],)).fetchone()
    assert row is not None, (
        "the EXCEPTION_PENDING instance did not survive the commit; the "
        "evidence of an unroutable object was destroyed by reporting it")
    assert row[0] == rules.INST_EXCEPTION_PENDING


def test_an_unroutable_document_never_becomes_an_approved_document(
        pg_connection, pg_database, approval_schema):
    """Stated as its own test because it is the property that matters most.
    The revision is unroutable, is held, and is not approvable by any route:
    not by the workflow, which never opened a stage, and not by the direct
    route, which the held instance closes."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    revision_id = _seed_revision(pg_connection, suffix, ids)

    result = _submit_revision(pg_database, revision_id)
    assert result["approval_status"] == rules.INST_EXCEPTION_PENDING

    status = pg_connection.execute(
        "SELECT status FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    assert status == "DRAFT"

    with pytest.raises(budget_mod.BudgetServiceError) as exc:
        with pg_database.session(_scope()) as session:
            budget_mod.approve_revision(session, revision_id=revision_id, actor="U-A")
    assert exc.value.code == "APPROVAL_IN_PROGRESS"

    status = pg_connection.execute(
        "SELECT status FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    assert status == "DRAFT"
    # REVISION lines only. `_seed_estate` now seeds the ORIGINAL grant behind
    # the control cell -- it has to, because the cell is materialised from
    # `budget_line` and a cell with no ledger behind it is a state that cannot
    # exist. Counting every kind would assert that the estate has no budget at
    # all, which is not what this test is about.
    lines = pg_connection.execute(
        "SELECT count(*) FROM budget_line WHERE wbs_id = %s AND kind = 'REVISION'",
        (ids["wbs"],)).fetchone()[0]
    assert lines == 0, "no spending capacity may be created by this outcome"


def test_the_direct_route_cannot_approve_a_revision_under_an_open_instance(
        pg_connection, pg_database, approval_schema):
    """Otherwise submitting would be a formality anybody holding
    `revision.approve` could step around."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)
    _submit_revision(pg_database, revision_id)

    with pytest.raises(budget_mod.BudgetServiceError) as exc:
        with pg_database.session(_scope()) as session:
            budget_mod.approve_revision(session, revision_id=revision_id, actor="U-A")
    assert exc.value.code == "APPROVAL_IN_PROGRESS"


def test_a_stale_write_back_refuses_rather_than_overwriting(
        pg_connection, pg_database, approval_schema):
    """The document moved underneath the instance, so the approvers decided
    text that is no longer there. `supersede_if_changed` is the path that
    re-routes a changed document; the write-back refuses loudly."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)

    submitted = _submit_revision(pg_database, revision_id)
    instance_id = submitted["approval_instance_id"]

    # WHERE THIS GUARD IS REACHABLE, which is not where this test used to look.
    #
    # Two earlier shapes of this test both failed, for opposite reasons, and
    # both are worth recording because they map the guard's real position:
    #
    #   * decide, THEN move the document, then write back -- the engine now
    #     calls the write-back itself at closure, so the outcome was already
    #     applied and the second call returned early as idempotent.
    #   * move the document, THEN decide -- `decide` runs
    #     `assert_object_version_fresh` of its own and raises
    #     OBJECT_VERSION_STALE before the write-back is ever entered.
    #
    # So in the decide path the write-back's `_assert_version_matches` is
    # unreachable: the engine's own check fires first, and after closure
    # idempotency fires. The guard is defence in depth for a caller that drives
    # `apply_outcome` DIRECTLY -- a retry or a replayed decision, which is
    # exactly what the module documents it for. That is the case tested here.
    #
    # The instance is closed by statement rather than by `decide` precisely so
    # the write-back has NOT run: this is an instance that closed and whose
    # outcome is being re-driven later, with the document having moved in
    # between.
    pg_connection.execute(
        "UPDATE approval_instance SET status = %s, closed_at = now() "
        "WHERE instance_id = %s", (rules.INST_APPROVED, instance_id))
    pg_connection.execute(
        "UPDATE budget_revision SET version_no = version_no + 1 WHERE revision_id = %s",
        (revision_id,))
    pg_connection.commit()

    with pytest.raises(wb.WritebackError) as exc:
        _write_back(pg_database, instance_id)
    assert exc.value.code == wb.ERR_STALE

    status, line_id = pg_connection.execute(
        "SELECT status, budget_line_id FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    assert status == "SUBMITTED", (
        f"the document is {status!r}; a refused write-back must leave it "
        f"exactly as the approvers found it")
    assert line_id is None, "a refused write-back must create no spending capacity"

    lines = pg_connection.execute(
        "SELECT count(*) FROM budget_line WHERE wbs_id = %s AND kind = 'REVISION'",
        (ids["wbs"],)).fetchone()[0]
    assert lines == 0


def test_a_revision_cannot_be_submitted_twice(
        pg_connection, pg_database, approval_schema):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids)
    _submit_revision(pg_database, revision_id)

    with pytest.raises(budget_mod.BudgetServiceError) as exc:
        _submit_revision(pg_database, revision_id)
    assert exc.value.code == "ALREADY_SUBMITTED"

    instances = pg_connection.execute(
        "SELECT count(*) FROM approval_instance WHERE object_id = %s",
        (revision_id,)).fetchone()[0]
    assert instances == 1


# ==========================================================================
# The seam stream A1 owns
# ==========================================================================
def test_the_engine_calls_the_write_back_itself(pg_connection, pg_database,
                                                 approval_schema):
    """The join between the two streams.

    Stream A1 adds ONE call to `approval_writeback.apply_outcome` in
    `pg/approvals.py`, at the single point an instance closes. Until it lands,
    every test above invokes the write-back by hand -- which tests this
    stream's contract honestly but proves nothing about the wiring. This test
    skips while the call is absent and starts covering the seam the moment it
    appears, so nobody has to remember to switch it on.
    """
    if "approval_writeback" not in inspect.getsource(engine):
        pytest.skip(
            "pg/approvals.py does not yet call approval_writeback.apply_outcome. "
            "That call site is Wave 4 stream A1's; this test covers the seam "
            "as soon as it lands.")

    suffix = uuid.uuid4().hex[:10]
    ids = _seed_estate(pg_connection, suffix)
    _seed_users(pg_connection, {"U-MAKER": ["Requestor"], "U-A": ["Finance"]})
    _one_stage_definition(pg_connection, suffix, ids)
    revision_id = _seed_revision(pg_connection, suffix, ids, delta_paise=7_000_000)

    submitted = _submit_revision(pg_database, revision_id)
    _decide(pg_database, submitted["approval_instance_id"], actor="U-A")

    # No manual apply_outcome: the engine must have done it.
    status, line_id = pg_connection.execute(
        "SELECT status, budget_line_id FROM budget_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    assert status == "APPROVED"
    assert line_id
