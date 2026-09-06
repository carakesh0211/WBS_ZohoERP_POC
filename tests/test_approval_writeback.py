"""The approval write-back, tested without a database.

Wave 4 stream A2. ``app/backend/pg/approval_writeback.py`` is the boundary
between two frozen status registries: an approval-engine status
(``research/30_contracts/C15_approval_statuses.json``) goes in and a business
status from C3's frozen 21 comes out. Everything decidable about that mapping
-- that it never leaks an engine status onto a document, that it follows C15
except in the one place the schema makes C15 unwritable, that it is idempotent,
that it refuses a stale document rather than overwriting it -- is decidable
without PostgreSQL, and is decided here.

That matters more than usual for this file. There is no local PostgreSQL, so
``tests/test_pg_approval_writeback_e2e.py`` SKIPS on a development machine. A
green local run says nothing about it. Every property that can be proved on a
double is proved here instead, so the local run does say something about this.

The double
----------
:class:`FakeSession` answers ``fetchall``/``fetchone`` from a list of
(SQL substring, rows) rules and records every statement it is handed. It is
enough because the write-back's own logic is entirely in the ORDER of its
checks -- and getting that order wrong is exactly how idempotency breaks: put
the staleness check before the already-applied check and the second application
of the same instance fails, because the first one bumped ``version_no``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.pg import approval_writeback as wb
from app.backend.pg import approvals as engine
from app.backend.pg import budget as budget_mod
from app.backend.pg.engine import Scope

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "research" / "30_contracts"
MIGRATION_003 = ROOT / "migrations" / "pg" / "003_budget_planning.sql"
MIGRATION_009 = ROOT / "migrations" / "pg" / "009_document_approval_states.sql"


def contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def c3_codes() -> set[str]:
    return {s["code"] for s in contract("C3_statuses.json")["statuses"]}


def c15_instance_statuses() -> dict[str, dict]:
    ns = contract("C15_approval_statuses.json")["namespaces"]["instance"]
    return {s["code"]: s for s in ns["statuses"]}


# ==========================================================================
# The double
# ==========================================================================
class FakeSession:
    """A `Session` stand-in that answers from rules and records statements."""

    def __init__(self, rules=(), scope=None):
        # `read_all` so `repo.compile_scope` returns TRUE and the scope layer
        # is not what these tests are exercising. Scope refusal has its own
        # test below, driven by an empty result rather than by a Scope.
        self.scope = scope or Scope(user_id="U-WB-TEST", principal_kind="SERVICE",
                                     read_all=True)
        self.rules = list(rules)
        self.statements: list[tuple[str, str]] = []
        self.locks_taken: list = []

    def _rows(self, statement: str):
        flat = " ".join(statement.split())
        for needles, rows in self.rules:
            if all(needle in flat for needle in needles):
                return rows
        return []

    def fetchall(self, statement, params=None):
        self.statements.append(("fetchall", " ".join(statement.split())))
        return self._rows(statement)

    def fetchone(self, statement, params=None):
        self.statements.append(("fetchone", " ".join(statement.split())))
        rows = self._rows(statement)
        return rows[0] if rows else None

    def execute(self, statement, params=None):
        self.statements.append(("execute", " ".join(statement.split())))
        return None

    @property
    def writes(self) -> list[str]:
        return [sql for kind, sql in self.statements if kind == "execute"]


REVISION_READ = ("FROM budget_revision r",)
TRANSFER_READ = ("FROM budget_transfer t",)
ACTION_READ = ("FROM approval_action",)
ASSIGNMENT_READ = ("FROM approval_assignment a",)


def revision_row(status="DRAFT", version_no=1, note=None):
    return [(status, version_no, "W-1", "H-1", note)]


def transfer_row(status="DRAFT", version_no=1, note=None):
    return [(status, version_no, "W-1", "H-1", "W-2", "H-2", note)]


def instance(status="APPROVED", *, object_type="BUDGET_REVISION",
             object_id="REV-1", object_version=1, cells=None):
    if cells is None:
        cells = [{"wbs_id": "W-1", "budget_head_id": "H-1"}]
    return {
        "instance_id": "AINS-1",
        "object_type": object_type,
        "object_id": object_id,
        "object_version": str(object_version),
        "status": status,
        "maker_user_id": "U-MAKER",
        "snapshot": {"affected_cells": cells},
    }


# ==========================================================================
# The namespace boundary -- C15 in, C3 out
# ==========================================================================
class TestRegistrySeparation:
    def test_every_mapping_target_is_a_frozen_c3_business_status(self):
        """The rule C15 states in as many words: an approval status is mapped
        to a C3 business status on the underlying object. A target outside the
        21 is a status no business screen has a label for."""
        codes = c3_codes()
        for source, target in wb.INSTANCE_TO_BUSINESS_STATUS.items():
            if target is None:
                continue
            assert target in codes, (
                f"{source} maps to {target!r}, which is not one of C3's frozen "
                f"21 business statuses")

    def test_no_engine_only_status_is_ever_written_onto_a_document(self):
        """The four codes C15 and C3 share are the reviewed overlap. Everything
        else in the engine namespace -- OPEN, RECALLED, SUPERSEDED, CANCELLED
        -- must never appear as a target, and a fifth accidental collision
        would show up here as well as in `test_contracts.py`."""
        engine_only = set(c15_instance_statuses()) - c3_codes()
        targets = {t for t in wb.INSTANCE_TO_BUSINESS_STATUS.values() if t}
        assert engine_only, "sanity: C15 must carry codes C3 does not"
        assert not (targets & engine_only), (
            f"approval-engine statuses leaked onto a business document: "
            f"{sorted(targets & engine_only)}")

    def test_the_mapping_follows_c15_with_no_deviation_at_all(self):
        """C15 is the authority, and this table now follows it everywhere.

        It did not always. `budget_revision.status` and
        `budget_transfer.status` were defined by migration 003 as
        `CHECK (status IN ('DRAFT','APPROVED','REJECTED','CANCELLED'))`, so
        C15's RETURNED target could not be stored and the write-back wrote
        DRAFT, keeping the real outcome in `decision_note` prose. That was true
        of a returned revision but strictly less informative than the fact it
        replaced -- a reader could not distinguish "sent back for correction"
        from "never submitted".

        `009_document_approval_states.sql` widened both domains, so the
        exception is closed rather than tolerated. This asserts NO deviation:
        an empty difference set, not a permitted one. Re-narrowing the schema
        without revisiting this mapping fails here.
        """
        declared = {code: spec.get("maps_to_business_status")
                    for code, spec in c15_instance_statuses().items()}
        differences = {
            code: (declared.get(code), wb.INSTANCE_TO_BUSINESS_STATUS[code])
            for code in wb.INSTANCE_TO_BUSINESS_STATUS
            if code in declared
            and declared[code] != wb.INSTANCE_TO_BUSINESS_STATUS[code]
        }
        assert differences == {}, (
            f"this table disagrees with C15's maps_to_business_status: "
            f"{differences}. There is no longer a schema constraint forcing "
            f"one, so a difference here is a choice, and C15 is the authority.")
        assert wb.INSTANCE_TO_BUSINESS_STATUS["RETURNED"] == "RETURNED"

    def test_the_schema_can_actually_hold_what_the_mapping_writes(self):
        """The other half: a mapping target the column refuses is a runtime
        CheckViolation dressed as a passing unit test.

        Every non-null target must appear in the document domain that migration
        009 widened. SUBMITTED is checked alongside them because `submit_*`
        writes it and nothing in this table does -- if 009 were reverted, this
        catches it here rather than in a live insert.
        """
        ddl = MIGRATION_009.read_text(encoding="utf-8")
        targets = {t for t in wb.INSTANCE_TO_BUSINESS_STATUS.values() if t}
        for value in sorted(targets | {"SUBMITTED"}):
            assert f"'{value}'" in ddl, (
                f"the write-back can write {value!r} but migration 009 does "
                f"not admit it on budget_revision/budget_transfer; the insert "
                f"would fail with a CheckViolation at runtime")

        # And the constraint that says which statuses have been decided must
        # keep SUBMITTED on the undecided side: forcing decided_at/decided_by
        # onto a submitted document would fabricate a decision at exactly the
        # moment the point is that none has been taken.
        assert "status IN ('DRAFT', 'SUBMITTED')" in ddl, (
            "009 no longer exempts SUBMITTED from ck_*_decision; a submitted "
            "document would have to carry a decision it has not had")

    def test_cancelled_is_not_a_c3_status_so_it_is_not_written(self):
        """The DDL admits 'CANCELLED' on both documents and C3 does not carry
        it. The write-back therefore never writes it -- an administrator
        cancelling an instance returns the document to the maker."""
        assert "CANCELLED" not in c3_codes()
        assert wb.INSTANCE_TO_BUSINESS_STATUS["CANCELLED"] == "DRAFT"

    def test_the_registry_covers_exactly_the_engine_s_routable_object_types(self):
        """PR and PO are deliberately absent from `OBJECT_BINDINGS` because
        they have no PostgreSQL table. The write-back must not invent a
        handler for a type the engine cannot route."""
        assert set(wb._REGISTRY) == set(engine.OBJECT_BINDINGS)
        assert set(wb._REGISTRY) == {"BUDGET_REVISION", "BUDGET_TRANSFER"}

    def test_open_and_exception_pending_are_not_closed_statuses(self):
        assert "OPEN" not in wb.CLOSED_STATUSES
        assert "EXCEPTION_PENDING" not in wb.CLOSED_STATUSES


# ==========================================================================
# What apply_outcome refuses
# ==========================================================================
class TestRefusals:
    def test_an_unknown_object_type_is_a_silent_no_op(self):
        """The registry is keyed on object_type precisely so adding a routable
        type later cannot crash a flow that predates its handler."""
        session = FakeSession()
        wb.apply_outcome(session, instance(object_type="PURCHASE_ORDER"))
        assert session.statements == []

    @pytest.mark.parametrize("status", ["OPEN", "EXCEPTION_PENDING"])
    def test_a_non_closed_instance_is_refused(self, status):
        """EXCEPTION_PENDING especially: an object HELD for an administrator
        is the one thing that must never become an approved object."""
        session = FakeSession(rules=[(REVISION_READ, revision_row())])
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance(status))
        assert exc.value.code == wb.ERR_NOT_CLOSED
        assert session.writes == []

    def test_a_superseded_instance_changes_no_document_status(self):
        """C15 gives SUPERSEDED a null business status: a new instance was
        routed over the same object and that one will decide it."""
        session = FakeSession(rules=[(REVISION_READ, revision_row())])
        wb.apply_outcome(session, instance("SUPERSEDED"))
        assert session.statements == []

    def test_a_document_the_session_cannot_read_refuses_loudly(self):
        """`repo.query_one` returns None for both "no such row" and "out of
        scope" by design. Inside a closing instance either reading is a real
        inconsistency, and doing nothing would leave an APPROVED instance
        beside an untouched document."""
        session = FakeSession()          # no rule -> the scoped read finds nothing
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance("APPROVED"))
        assert exc.value.code == wb.ERR_DOCUMENT_UNREACHABLE
        assert session.writes == []

    def test_a_stale_object_version_refuses_loudly(self):
        """Contract 8's reasoning, applied at the other end: approving version
        1 of a document now at version 4 applies consent to text nobody read.
        `supersede_if_changed` is the path for a changed document; this one
        refuses."""
        session = FakeSession(rules=[
            (REVISION_READ, revision_row(version_no=4)),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance("APPROVED", object_version=1))
        assert exc.value.code == wb.ERR_STALE
        assert exc.value.status == 409
        assert session.writes == []

    def test_a_document_already_decided_differently_is_refused(self):
        session = FakeSession(rules=[(REVISION_READ, revision_row(status="REJECTED"))])
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance("APPROVED"))
        assert exc.value.code == wb.ERR_CONFLICT
        assert session.writes == []

    def test_a_snapshot_that_does_not_declare_the_document_s_cells_is_refused(self):
        """`locking.py` rule 1: the affected set is declared once, first and
        complete. The engine locks the snapshot's set; `approve_revision` locks
        the set it reads off the document. If those differ, the second is a
        cell lock taken after the instance and document rows are already
        locked -- the edge the deadlock-freedom proof rules out."""
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        stale_cells = [{"wbs_id": "W-OTHER", "budget_head_id": "H-OTHER"}]
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance("APPROVED", cells=stale_cells))
        assert exc.value.code == wb.ERR_CELLS_DISAGREE
        assert session.writes == []


# ==========================================================================
# What apply_outcome does
# ==========================================================================
class TestApplication:
    def test_approved_goes_through_the_existing_budget_service_path(self, monkeypatch):
        """The rule that keeps an approved revision from becoming spendable by
        a route that bypasses the controls: the write-back calls
        `budget.approve_revision`, which holds maker-checker, the ordered lock,
        the availability re-check and the effective-dated budget_line append.
        It does not write budget_line itself."""
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("APPROVED"))
        assert calls["revision_id"] == "REV-1"
        assert calls["actor"] == "U-APPROVER"
        assert calls["approval_instance_id"] == "AINS-1"
        assert session.writes == [], (
            "the write-back issued its own UPDATE on an approval; the budget "
            "service must be the only writer of a budget_line")

    def test_the_actor_is_the_closing_approver_not_the_maker(self, monkeypatch):
        """Attribution matters twice over: `budget_line.created_by` names who
        released the money, and `approve_revision`'s maker-checker compares
        exactly that identity against the drafter."""
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-FINAL-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("APPROVED"))
        assert calls["actor"] == "U-FINAL-APPROVER"
        assert calls["actor"] != "U-MAKER"

    def test_with_no_action_row_yet_the_actor_comes_from_the_assignment(
            self, monkeypatch):
        """The ordering that would otherwise attribute every production
        approval to SYSTEM.

        `_apply_decision` marks the assignment ACTED before it sets the
        instance status, and `decide` appends the approval_action row after it
        returns. A write-back invoked at the moment the instance closes -- the
        single call site A1 adds -- therefore sees the assignment and not yet
        the action. Every test that calls the write-back afterwards sees the
        action. Both must name the same person.
        """
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ASSIGNMENT_READ, [("U-FINAL-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("APPROVED"))
        assert calls["actor"] == "U-FINAL-APPROVER"

    def test_a_supplied_actor_beats_a_stale_action_log(self, monkeypatch):
        """The defect that only appeared when A1's and A2's work were merged.

        Neither stream could see it. A2's write-back inferred the closing actor
        and consulted `approval_action` FIRST; A1's engine appends that row
        AFTER `_set_instance_status(closed=True)` fires the write-back. On a
        single-stage approval that is merely "no row yet" and the assignment
        fallback covers it -- which is the case the test above pins, and it
        passed.

        On a MULTI-STAGE approval the action log is not empty, it is STALE: the
        newest closing action belongs to the previous stage's approver, who did
        not close anything. The write-back would attribute the budget line to
        them, and because `approve_revision` compares the attributed actor
        against the maker, the self-approval control would have been evaluated
        against the wrong identity -- a control silently checking the wrong
        person is worse than one visibly failing.

        `decide` knows who is acting, so it now says so, and a supplied actor
        short-circuits both inferred sources. The action log here holds the
        previous approver precisely so that consulting it would fail this test.
        """
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-STAGE-1-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("APPROVED"),
                         closing_actor="U-STAGE-2-APPROVER")
        assert calls["actor"] == "U-STAGE-2-APPROVER", (
            "the write-back preferred the action log over the actor the engine "
            "handed it; on a multi-stage approval that log names the previous "
            "stage's approver")

    def test_the_engine_supplies_the_actor_rather_than_letting_it_be_inferred(self):
        """The seam itself, checked statically because the live path skips here.

        A correct `apply_outcome` is no use if `_set_instance_status` never
        passes the actor. This asserts the parameter exists on both sides and
        that the engine's call site actually populates it -- the half of the
        contract that lives in a file this test's module does not own.
        """
        import ast
        import inspect
        from app.backend.pg import approvals as engine

        assert "closing_actor" in inspect.signature(wb.apply_outcome).parameters
        assert "closing_actor" in inspect.signature(
            engine._set_instance_status).parameters

        source = Path(engine.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        call = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "apply_outcome"), None)
        assert call is not None, "the engine no longer calls apply_outcome"
        assert any(kw.arg == "closing_actor" for kw in call.keywords), (
            "the engine calls apply_outcome without closing_actor, so the "
            "write-back is back to inferring the actor from a table the engine "
            "has deliberately not finished writing")

    def test_with_neither_source_the_actor_falls_back_to_system(self, monkeypatch):
        """A maker recall or an administrative cancel closes an instance with
        no approver having acted. SYSTEM is a real principal_kind='SERVICE'
        row, not a magic string, so the foreign key holds either way."""
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[(REVISION_READ, revision_row())])
        wb.apply_outcome(session, instance("APPROVED"))
        assert calls["actor"] == "SYSTEM"

    def test_rejected_goes_through_the_existing_reject_path(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(budget_mod, "reject_revision",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("REJECTED"))
        assert calls["revision_id"] == "REV-1"
        assert calls["approval_instance_id"] == "AINS-1"
        assert "AINS-1" in calls["reason"]

    def test_a_transfer_approval_declares_both_legs_and_calls_the_service(
            self, monkeypatch):
        calls = {}
        monkeypatch.setattr(budget_mod, "approve_transfer",
                            lambda session, **kw: calls.update(kw))
        session = FakeSession(rules=[
            (TRANSFER_READ, transfer_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        both = [{"wbs_id": "W-1", "budget_head_id": "H-1"},
                {"wbs_id": "W-2", "budget_head_id": "H-2"}]
        wb.apply_outcome(session, instance(
            "APPROVED", object_type="BUDGET_TRANSFER", object_id="TRF-1",
            cells=both))
        assert calls["transfer_id"] == "TRF-1"
        assert calls["approval_instance_id"] == "AINS-1"

    def test_a_transfer_snapshot_missing_the_destination_leg_is_refused(self):
        """A transfer moves budget INTO a cell as well as out of one, and
        `approve_transfer` locks both. A snapshot declaring only the source
        would leave the destination locked after the fact."""
        session = FakeSession(rules=[
            (TRANSFER_READ, transfer_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        source_only = [{"wbs_id": "W-1", "budget_head_id": "H-1"}]
        with pytest.raises(wb.WritebackError) as exc:
            wb.apply_outcome(session, instance(
                "APPROVED", object_type="BUDGET_TRANSFER", object_id="TRF-1",
                cells=source_only))
        assert exc.value.code == wb.ERR_CELLS_DISAGREE

    @pytest.mark.parametrize("status", ["RETURNED", "RECALLED", "CANCELLED"])
    def test_a_return_to_the_maker_writes_the_status_and_the_reason(
            self, status):
        """Since migration 009 these targets have a status to write, and the
        decision columns move with it.

        All three used to map to DRAFT, and the document was already DRAFT, so
        only the note was written -- `status` alone could not distinguish
        "returned for correction" from "never submitted", and the note was what
        preserved the fact. 009 changed both halves: a routed document is
        SUBMITTED, and RETURNED maps to RETURNED. Writing nothing would now
        leave a returned revision SUBMITTED, stuck under an approval that has
        closed and editable by nobody.

        `decided_at`/`decided_by` are not optional decoration here. 009's
        `ck_*_decision` puts RETURNED on the DECIDED side -- an approver did
        decide to send it back, at a known time -- and DRAFT on the undecided
        side, so a recall or an administrative cancel must CLEAR them rather
        than leave a decision standing on a document that is editable again.
        The database refuses either mistake, so both directions are asserted.
        """
        session = FakeSession(rules=[
            (REVISION_READ, revision_row()),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance(status))

        assert len(session.writes) == 1
        written = session.writes[0]
        assert "UPDATE budget_revision SET status" in written
        assert "decision_note" in written, (
            "the reason is still written; it is what an administrator reads "
            "when the status alone does not say which decision this was")

        expected = "RETURNED" if status == "RETURNED" else "DRAFT"
        assert wb.INSTANCE_TO_BUSINESS_STATUS[status] == expected

        if expected == "RETURNED":
            assert "decided_at = now()" in written and "decided_by = %(actor)s" in written, (
                "a RETURNED document sits on ck_*_decision's DECIDED side and "
                "must name who returned it and when")
        else:
            assert "decided_at = NULL" in written and "decided_by = NULL" in written, (
                "a document returned to DRAFT is undecided again; leaving a "
                "decision on it violates ck_*_decision and tells an auditor "
                "something untrue")


# ==========================================================================
# Idempotency
# ==========================================================================
class TestIdempotence:
    def test_a_second_application_after_approval_writes_nothing(self, monkeypatch):
        """A retry or a replayed decision must not apply twice. The document is
        already APPROVED, so there is nothing to do -- and this check must come
        BEFORE the staleness check, because the first application incremented
        version_no and the instance still records the old one."""
        called = []
        monkeypatch.setattr(budget_mod, "approve_revision",
                            lambda session, **kw: called.append(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row(status="APPROVED", version_no=2)),
        ])
        wb.apply_outcome(session, instance("APPROVED", object_version=1))
        assert called == []
        assert session.writes == []

    def test_a_second_application_after_rejection_writes_nothing(self, monkeypatch):
        called = []
        monkeypatch.setattr(budget_mod, "reject_revision",
                            lambda session, **kw: called.append(kw))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row(status="REJECTED", version_no=2)),
        ])
        wb.apply_outcome(session, instance("REJECTED", object_version=1))
        assert called == []
        assert session.writes == []

    def test_a_second_return_writes_nothing(self):
        """A genuinely already-applied return is a no-op.

        The fixture now carries the STATUS as well as the note. It used to
        carry only the note, which since migration 009 is not an already-
        applied state at all: RETURNED maps to RETURNED, so a row still showing
        SUBMITTED has had the outcome half-applied and the second call is right
        to finish it. Asserting "no writes" against that fixture would have
        been asserting that the write-back leaves a returned document stuck
        under a closed approval.
        """
        note = wb._note(instance("RETURNED"))
        session = FakeSession(rules=[
            (REVISION_READ, revision_row(status="RETURNED", note=note)),
            (ACTION_READ, [("U-APPROVER",)]),
        ])
        wb.apply_outcome(session, instance("RETURNED"))
        assert session.writes == []

    def test_the_already_applied_check_precedes_the_staleness_check(self):
        """Stated as its own test because the ORDER is the property, and an
        edit that swaps two `if` blocks would leave every other test here
        green while breaking every retry in production."""
        source = (ROOT / "app" / "backend" / "pg" / "approval_writeback.py").read_text(
            encoding="utf-8")
        body = source.split("def _apply_budget_revision", 1)[1]
        already_applied = body.index("if status == target")
        staleness = body.index("_assert_version_matches")
        assert already_applied < staleness
