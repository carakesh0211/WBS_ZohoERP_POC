"""Submitting a budget revision or transfer for approval, without a database.

Wave 4 stream A2, the front half of the gap. Before this, ``open_instance``
was called from tests and from nowhere else: a revision went from DRAFT
straight to ``approve_revision``, and the entire configurable approval engine
decided nothing in production.

What is proved here, all of it on a double, all of it on every machine:

  * submitting opens an approval instance, with a snapshot whose money is
    integer paise -- a float in a ``*_paise`` path is refused by the predicate
    compiler by design, and refusing it here too (rather than rounding) is what
    keeps that refusal meaningful;
  * an EXCEPTION_PENDING instance is a RECORDED OUTCOME: the caller must not
    treat it as success and must not roll back, because the rollback is what
    would destroy the evidence;
  * **an unroutable document never becomes an approved document**;
  * a live instance closes the direct ``/approve`` route, so submitting is not
    a formality that a holder of ``revision.approve`` can walk past.

The live counterparts are in ``tests/test_pg_approval_writeback_e2e.py``, which
SKIPS with no ``CAPEX_DB_URL``. A skip is not a pass, which is why the
properties above are here rather than there.
"""
from __future__ import annotations

import ast
import decimal
from datetime import date
from pathlib import Path

import pytest

from app.backend.pg import approval_rules as rules
from app.backend.pg import approvals as engine
from app.backend.pg import budget as budget_mod
from app.backend.pg.engine import Scope

ROOT = Path(__file__).resolve().parents[1]
API_BUDGET = ROOT / "app" / "backend" / "api" / "budget.py"
PG_BUDGET = ROOT / "app" / "backend" / "pg" / "budget.py"


# ==========================================================================
# The double
# ==========================================================================
class FakeSession:
    """A `Session` stand-in answering from (SQL substrings -> rows) rules."""

    def __init__(self, rules_=(), scope=None):
        self.scope = scope or Scope(user_id="U-SUB-TEST", principal_kind="SERVICE",
                                     read_all=True)
        self.rules = list(rules_)
        self.statements: list[tuple[str, str]] = []
        self.locks_taken: list = []

    def _rows(self, statement):
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
    def writes(self):
        return [sql for kind, sql in self.statements if kind == "execute"]


def base_rules(*, delta_paise=1_000_000, status="DRAFT", version_no=1,
               budget_paise=100_000_000, exposure=0, live_instance=None):
    """Everything `revision_snapshot` and `submit_revision` read.

    Ordered most-specific first: several of these statements share the
    substring `FROM wbs_element`, and the first matching rule wins.
    """
    return [
        # _subtree_totals -- budget, commitment, actual, pr_reserved,
        # internal_allocation, internal_consumption (migration 034 added the
        # two internal limbs; the stub returns the real statement's width).
        (("COALESCE(SUM(bc.budget_paise)",), [(budget_paise, exposure, 0, 0, 0, 0)]),
        # _owning_ancestor
        (("JOIN budget_control_cell bc",), [("W-1", "W-1")]),
        # check_availability's head existence probe
        (("FROM budget_head WHERE budget_head_id",), [(1,)]),
        # _routing_dimensions
        (("SELECT w.project_id, p.entity_id",), [("PRJ-1", "ENT-1", "PLT-1", "LOC-1")]),
        # _assert_wbs_in_scope
        (("SELECT 1 FROM wbs_element w",), [(1,)]),
        # submit_revision's own precondition read (status, created_by, version_no)
        (("SELECT r.status, r.created_by",), [(status, "U-MAKER", version_no)]),
        # revision_snapshot's read
        (("SELECT r.wbs_id, r.budget_head_id",),
         [("W-1", "H-1", delta_paise, date(2026, 4, 1), "why", "U-MAKER", version_no)]),
        # approve_revision's own unlocked read
        (("SELECT wbs_id, budget_head_id, delta_paise",),
         [("W-1", "H-1", delta_paise, date(2026, 4, 1), "why", status, "U-MAKER")]),
        # reject_revision's read
        (("SELECT wbs_id, status, created_by",), [("W-1", status, "U-MAKER")]),
        # approve_transfer's own unlocked read
        (("SELECT from_wbs_id, from_head_id, to_wbs_id",),
         [("W-1", "H-1", "W-2", "H-2", abs(delta_paise), date(2026, 4, 1),
           "why", status, "U-MAKER")]),
        # submit_transfer's precondition read
        (("SELECT t.status, t.created_by",), [(status, "U-MAKER", version_no)]),
        # transfer_snapshot's read
        (("SELECT t.from_wbs_id, t.from_head_id",),
         [("W-1", "H-1", "W-2", "H-2", abs(delta_paise), date(2026, 4, 1),
           "why", "U-MAKER", version_no)]),
        # live_approval_instance
        (("FROM approval_instance",), [] if live_instance is None else [live_instance]),
    ]


@pytest.fixture()
def no_audit(monkeypatch):
    """Audit appends are proved by `tests/test_pg_audit.py`; here they would
    only be a second set of canned rows in every rule list."""
    written = []
    monkeypatch.setattr(budget_mod.audit_mod, "append",
                        lambda session, actor, action, otype, oid, detail, **kw:
                        written.append((actor, action, otype, oid)) or {})
    return written


@pytest.fixture()
def opened(monkeypatch):
    """Capture what `open_instance` was called with, and control its answer."""
    captured = {}

    def fake_open(session, **kwargs):
        captured.update(kwargs)
        return dict(captured.pop("_answer", None) or {
            "instance_id": "AINS-1", "status": "OPEN", "current_stage_no": 1})

    monkeypatch.setattr(engine, "open_instance", fake_open)
    return captured


# ==========================================================================
# Task 1 -- an instance is opened on submit
# ==========================================================================
class TestSubmissionOpensAnInstance:
    def test_submitting_a_revision_calls_open_instance(self, no_audit, opened):
        session = FakeSession(base_rules())
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")
        assert opened["object_type"] == "BUDGET_REVISION"
        assert opened["object_id"] == "REV-1"
        assert opened["maker_user_id"] == "U-MAKER"
        assert result["submitted"] is True
        assert result["approval_instance_id"] == "AINS-1"

    def test_submitting_a_transfer_calls_open_instance(self, no_audit, opened):
        session = FakeSession(base_rules())
        result = budget_mod.submit_transfer(
            session, transfer_id="TRF-1", actor="U-MAKER")
        assert opened["object_type"] == "BUDGET_TRANSFER"
        assert opened["object_id"] == "TRF-1"
        assert result["submitted"] is True

    def test_submission_moves_the_document_to_submitted(self, no_audit, opened):
        """A routed revision says SUBMITTED, and says it in the same
        transaction that opened the instance.

        It used to stay DRAFT, because migration 003's CHECK constraint had no
        SUBMITTED value -- so the document's own status asserted something
        false about it, and `_assert_not_under_approval` was the only thing
        standing between that row and a second approval taken down the direct
        route. One guard, in one language, holding a property the schema is
        capable of stating. `009_document_approval_states.sql` widened the
        domain; the guard still holds, but no longer alone.

        SUBMITTED carries no `decided_at`/`decided_by`: 009 puts it on the
        undecided side of `ck_*_decision`, because a submission is not a
        decision.
        """
        session = FakeSession(base_rules())
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")
        assert result["status"] == "SUBMITTED"

        writes = [w for w in session.writes if "UPDATE budget_revision" in w]
        assert len(writes) == 1, (
            f"expected exactly one status write, got {len(writes)}: {writes}")
        assert "decided_at" not in writes[0] and "decided_by" not in writes[0], (
            "the submission wrote a decision; ck_budget_revision_decision puts "
            "SUBMITTED on the UNDECIDED side, and a fabricated decision is "
            "exactly what that constraint exists to refuse")

    def test_an_unroutable_document_stays_draft(self, no_audit, monkeypatch):
        """SUBMITTED means routed and progressing, so a held object is not it.

        `open_instance` returns EXCEPTION_PENDING for an object nothing routes.
        Calling that SUBMITTED would claim a workflow the object never entered;
        plan §12 reads DRAFT -> SUBMITTED -> ..., and an object held for an
        administrator is progressing through nothing. Nothing is loosened by
        the honesty: `live_approval_instance` still blocks a second submission
        and still blocks the direct approval route for this document.
        """
        monkeypatch.setattr(
            engine, "open_instance",
            lambda session, **kw: {"instance_id": "AINS-X",
                                   "status": "EXCEPTION_PENDING"})
        session = FakeSession(base_rules())
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")

        assert result["status"] == "DRAFT"
        assert result["submitted"] is False
        assert not [w for w in session.writes if "UPDATE budget_revision" in w], (
            "a held object had its document status moved; only a routed one "
            "becomes SUBMITTED")

    def test_the_audit_entry_precedes_open_instance(self, monkeypatch):
        """`contributor_set` unions every actor on the document's audit stream
        into the set no approver may be drawn from. Writing the submission
        entry first is what makes a submitter who is not the drafter ineligible
        to approve their own submission; writing it afterwards would leave that
        person eligible."""
        order = []
        monkeypatch.setattr(budget_mod.audit_mod, "append",
                            lambda *a, **k: order.append("audit") or {})
        monkeypatch.setattr(engine, "open_instance",
                            lambda session, **kw: order.append("open") or
                            {"instance_id": "AINS-1", "status": "OPEN"})
        budget_mod.submit_revision(FakeSession(base_rules()),
                                    revision_id="REV-1", actor="U-SUBMITTER")
        assert order == ["audit", "open"]

    def test_a_non_draft_revision_cannot_be_submitted(self, no_audit, opened):
        session = FakeSession(base_rules(status="APPROVED"))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.submit_revision(session, revision_id="REV-1", actor="U-MAKER")
        assert exc.value.code == "REVISION_NOT_DRAFT"

    def test_a_revision_already_under_approval_cannot_be_submitted_twice(
            self, no_audit, opened):
        session = FakeSession(base_rules(live_instance=("AINS-OLD", "OPEN")))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.submit_revision(session, revision_id="REV-1", actor="U-MAKER")
        assert exc.value.code == "ALREADY_SUBMITTED"


# ==========================================================================
# Money in the snapshot
# ==========================================================================
class TestSnapshotMoney:
    def test_every_paise_value_in_the_snapshot_is_an_int(self):
        session = FakeSession(base_rules(delta_paise=-2_500_000))
        snapshot = budget_mod.revision_snapshot(session, revision_id="REV-1")
        money = {k: v for k, v in snapshot.items() if k.endswith("_paise")}
        assert money, "the snapshot carries no money at all"
        for key, value in money.items():
            assert type(value) is int, f"{key} is {type(value).__name__}"
        for check in snapshot["budget_checks"]:
            for key, value in check.items():
                if key.endswith("_paise"):
                    assert type(value) is int, f"budget_checks.{key}"

    def test_a_decimal_amount_is_refused_rather_than_coerced(self):
        """`SUM(bigint)` returns numeric, which psycopg hands back as Decimal.
        A Decimal in a money path is refused at the boundary, not silently
        turned into an int somewhere downstream."""
        session = FakeSession(base_rules(delta_paise=decimal.Decimal("1000000")))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.revision_snapshot(session, revision_id="REV-1")
        assert exc.value.code == "MONEY_NOT_INTEGER"

    def test_a_float_amount_is_refused_rather_than_rounded(self):
        session = FakeSession(base_rules(delta_paise=1_000_000.0))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.revision_snapshot(session, revision_id="REV-1")
        assert exc.value.code == "MONEY_NOT_INTEGER"
        assert "Rounding it here" in exc.value.message

    def test_the_predicate_compiler_would_have_refused_a_float_anyway(self):
        """The reason the boundary check above must not round: the compiler
        refuses a float compared against a `*_paise` path at COMPILE time.
        Rounding to get past that would be defeating the control, not
        satisfying it -- so both halves refuse, and this pins the second."""
        with pytest.raises(rules.PredicateError):
            rules.compile_predicate({
                "op": ">", "left": {"path": "amount_paise"},
                "right": {"value": 5_000_000.0}})

    def test_a_cut_records_the_availability_baseline_an_increase_does_not(self):
        """Only a CUT consumes availability, which is why `approve_revision`
        re-checks in that direction only. The recorded baseline mirrors that,
        so contract 7's BUDGET_MOVED comparison is like with like."""
        cut = budget_mod.revision_snapshot(
            FakeSession(base_rules(delta_paise=-3_000_000)), revision_id="REV-1")
        rise = budget_mod.revision_snapshot(
            FakeSession(base_rules(delta_paise=3_000_000)), revision_id="REV-1")
        assert cut["budget_checks"][0]["requested_paise"] == 3_000_000
        assert rise["budget_checks"][0]["requested_paise"] == 0
        assert cut["amount_paise"] == rise["amount_paise"] == 3_000_000
        assert cut["direction"] == "CUT" and rise["direction"] == "INCREASE"

    def test_the_snapshot_carries_no_timestamp(self):
        """`content_sha` is a function of the snapshot, and
        `supersede_if_changed` compares that hash to decide whether the
        document changed. A `checked_at` in there would supersede every object
        on every check."""
        snapshot = budget_mod.revision_snapshot(
            FakeSession(base_rules()), revision_id="REV-1")
        assert "checked_at" not in snapshot
        for check in snapshot["budget_checks"]:
            assert "checked_at" not in check

    def test_the_snapshot_is_json_serialisable(self):
        """It is stored as jsonb and hashed as canonical JSON. A `date` object
        in there fails at the psycopg boundary, in a transaction that has
        already written rows."""
        import json as _json

        for snapshot in (
            budget_mod.revision_snapshot(FakeSession(base_rules()), revision_id="REV-1"),
            budget_mod.transfer_snapshot(FakeSession(base_rules()), transfer_id="TRF-1"),
        ):
            _json.dumps(snapshot)          # raises TypeError on a date/Decimal

    def test_the_snapshot_carries_every_routing_dimension(self):
        """`CONDITION_DIMENSIONS` is what a rule may test on. A snapshot
        missing entity_id or project_id also produces an instance whose
        denormalised columns are NULL, which no scope reaches."""
        snapshot = budget_mod.revision_snapshot(
            FakeSession(base_rules()), revision_id="REV-1")
        for dimension in ("entity_id", "project_id", "plant_id", "location_id",
                           "budget_head_id", "amount_paise", "object_type"):
            assert snapshot.get(dimension) is not None, dimension

    def test_the_transfer_snapshot_declares_both_legs_in_lock_order(self):
        """`approve_transfer` locks both cells; the engine locks what the
        snapshot declares. `approval_writeback` asserts the two agree, and this
        pins the producing end of that agreement."""
        snapshot = budget_mod.transfer_snapshot(
            FakeSession(base_rules()), transfer_id="TRF-1")
        assert snapshot["affected_cells"] == [
            {"wbs_id": "W-1", "budget_head_id": "H-1"},
            {"wbs_id": "W-2", "budget_head_id": "H-2"}]


# ==========================================================================
# EXCEPTION_PENDING is a recorded outcome, not an error
# ==========================================================================
class TestUnroutable:
    @staticmethod
    def _exception_pending(monkeypatch, code="APPROVAL_ROUTE_UNRESOLVED"):
        monkeypatch.setattr(
            engine, "open_instance",
            lambda session, **kw: {"instance_id": "AINS-X",
                                    "status": "EXCEPTION_PENDING"})

    def test_an_unroutable_submission_returns_rather_than_raising(
            self, monkeypatch, no_audit):
        """`open_instance` returns rather than raising on purpose: the caller
        owns the transaction, and an exception propagating out of it rolls back
        the EXCEPTION_PENDING row along with everything else -- destroying the
        evidence as a direct consequence of reporting it. The caller must not
        undo that by raising either."""
        self._exception_pending(monkeypatch)
        session = FakeSession(base_rules() + [
            (("FROM approval_action",), [({"code": "APPROVAL_ROUTE_UNRESOLVED"},)]),
        ])
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")
        assert result["refusal"] is not None

    def test_an_unroutable_submission_is_not_success(self, monkeypatch, no_audit):
        self._exception_pending(monkeypatch)
        result = budget_mod.submit_revision(
            FakeSession(base_rules()), revision_id="REV-1", actor="U-MAKER")
        assert result["submitted"] is False
        assert result["approval_status"] == "EXCEPTION_PENDING"

    def test_the_refusal_names_the_reason(self, monkeypatch, no_audit):
        """"Surface it as a refusal that names the reason": the reason is the
        code the engine recorded on the instance's last action, not a guess."""
        self._exception_pending(monkeypatch)
        session = FakeSession(base_rules() + [
            (("FROM approval_action",), [({"code": "NO_INDEPENDENT_APPROVER"},)]),
        ])
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")
        assert result["refusal"]["code"] == "NO_INDEPENDENT_APPROVER"
        assert result["refusal"]["status"] == 409
        assert "AINS-X" in result["refusal"]["message"]

    def test_an_unroutable_document_never_becomes_an_approved_document(
            self, monkeypatch, no_audit):
        """The assertion the brief asks for by name. Nothing in the submission
        path writes a document status at all, and the returned status is the
        DRAFT it went in as."""
        self._exception_pending(monkeypatch)
        session = FakeSession(base_rules())
        result = budget_mod.submit_revision(
            session, revision_id="REV-1", actor="U-MAKER")
        assert result["status"] == "DRAFT"
        assert result["status"] != "APPROVED"
        assert not [w for w in session.writes if "budget_revision SET" in w]
        assert not [w for w in session.writes if "budget_line" in w]

    def test_a_transfer_is_refused_the_same_way(self, monkeypatch, no_audit):
        self._exception_pending(monkeypatch)
        result = budget_mod.submit_transfer(
            FakeSession(base_rules()), transfer_id="TRF-1", actor="U-MAKER")
        assert result["submitted"] is False
        assert result["refusal"]["code"] == "APPROVAL_ROUTE_UNRESOLVED"


# ==========================================================================
# The direct-approval route cannot step around a live instance
# ==========================================================================
class TestDirectApprovalGuard:
    @pytest.mark.parametrize("status", ["OPEN", "EXCEPTION_PENDING"])
    def test_direct_approval_is_refused_while_an_instance_is_live(self, status):
        """EXCEPTION_PENDING is included deliberately. An object held for an
        administrator because nothing could route it must not be approvable by
        the route that exists for objects no workflow routes -- that is exactly
        the bypass the exception exists to prevent."""
        session = FakeSession(base_rules(live_instance=("AINS-LIVE", status)))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.approve_revision(session, revision_id="REV-1", actor="U-APPROVER")
        assert exc.value.code == "APPROVAL_IN_PROGRESS"
        assert exc.value.status == 409
        assert session.writes == []

    def test_direct_rejection_is_refused_too(self):
        """Not symmetry for its own sake: a direct rejection under a live
        instance moves the document to a terminal status while approvers are
        still being asked to decide it."""
        session = FakeSession(base_rules(live_instance=("AINS-LIVE", "OPEN")))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.reject_revision(session, revision_id="REV-1", actor="U-APPROVER")
        assert exc.value.code == "APPROVAL_IN_PROGRESS"

    def test_a_transfer_is_guarded_the_same_way(self):
        session = FakeSession(base_rules(live_instance=("AINS-LIVE", "OPEN")))
        with pytest.raises(budget_mod.BudgetServiceError) as exc:
            budget_mod.approve_transfer(session, transfer_id="TRF-1", actor="U-APPROVER")
        assert exc.value.code == "APPROVAL_IN_PROGRESS"

    def test_the_closing_instance_itself_is_exempt(self, monkeypatch):
        """The write-back passes the closing instance's own id EXPLICITLY,
        rather than relying on the engine having already flipped the status.
        That is what makes the guard correct whether the engine closes the
        instance before or after it calls the write-back."""
        session = FakeSession(base_rules(live_instance=("AINS-LIVE", "OPEN")))
        # Reaching past the guard is the assertion; the rest of approve_revision
        # is not what this test is about, so stop it at the next read.
        budget_mod._assert_not_under_approval(
            session, "BUDGET_REVISION", "REV-1",
            approval_instance_id="AINS-LIVE", label="Revision")

    def test_an_object_with_no_instance_is_unaffected(self):
        """Every revision raised before this stream existed has no instance.
        The direct route must keep working for them, or the guard is a
        regression rather than a control."""
        session = FakeSession(base_rules())
        budget_mod._assert_not_under_approval(
            session, "BUDGET_REVISION", "REV-1",
            approval_instance_id=None, label="Revision")


# ==========================================================================
# The router
# ==========================================================================
def _api_tree() -> ast.Module:
    return ast.parse(API_BUDGET.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in api/budget.py")


class TestRouter:
    def test_a_submit_route_exists_for_each_routable_object_type(self):
        from app.backend.api import budget as api_budget

        paths = {getattr(r, "path", "") for r in api_budget.router.routes}
        assert "/api/budget/revisions/{revision_id}/submit" in paths
        assert "/api/budget/transfers/{transfer_id}/submit" in paths

    def test_the_submit_routes_require_a_permission_beyond_the_router_floor(self):
        """The router's floor is `budget.read`. Raising a document into a
        workflow is a mutation and must carry its own permission.

        Read off the mounted route rather than out of the source text: a
        decorator that names the permission in a docstring and forgets the
        dependency would pass a grep and ship unguarded.
        """
        from app.backend.api import budget as api_budget

        for path in ("/api/budget/revisions/{revision_id}/submit",
                      "/api/budget/transfers/{transfer_id}/submit"):
            route = next(r for r in api_budget.router.routes
                          if getattr(r, "path", "") == path)
            permissions = set()
            for depends in route.dependencies:
                closure = getattr(depends.dependency, "__closure__", None) or ()
                permissions.update(
                    cell.cell_contents for cell in closure
                    if isinstance(cell.cell_contents, str))
            assert "revision.create" in permissions, (
                f"{path} carries no route-level permission: {permissions}")

    @pytest.mark.parametrize("handler",
                             ["post_revision_submit", "post_transfer_submit"])
    def test_the_refusal_is_raised_after_the_session_block_not_inside_it(self, handler):
        """The property the whole EXCEPTION_PENDING design rests on.
        `Database.session` rolls back on any exception, so raising the 409
        inside the `with` block would delete the instance row the 409 is
        reporting. `_raise_if_refused` must therefore sit at statement level in
        the function body, after the `with`."""
        fn = _function(_api_tree(), handler)
        withs = [i for i, stmt in enumerate(fn.body) if isinstance(stmt, ast.Try)]
        assert withs, f"{handler} has no try/with block"
        raiser = [
            i for i, stmt in enumerate(fn.body)
            if isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", None) == "_raise_if_refused"
        ]
        assert raiser, f"{handler} never calls _raise_if_refused"
        assert raiser[0] > withs[-1], (
            f"{handler} refuses inside the transaction block; the rollback "
            f"would destroy the EXCEPTION_PENDING evidence")

    def test_no_submit_handler_swallows_the_refusal(self):
        """A handler that returned `result` directly would answer 200 for a
        document that was never routed."""
        source = API_BUDGET.read_text(encoding="utf-8")
        for handler in ("post_revision_submit", "post_transfer_submit"):
            body = source.split(f"def {handler}", 1)[1].split("\n@router", 1)[0]
            assert "_raise_if_refused(result)" in body, handler
            assert "\n    return result\n" not in body, handler


# ==========================================================================
# Locking discipline of the new code paths
# ==========================================================================
class TestLockingDiscipline:
    def test_submission_takes_no_cell_lock(self):
        """Submitting creates no spending capacity and writes no cell, so
        there is nothing to serialise. Taking a lock it does not need would
        add a participant to the wait-for graph for no benefit."""
        tree = ast.parse(PG_BUDGET.read_text(encoding="utf-8"))
        for name in ("submit_revision", "submit_transfer",
                      "revision_snapshot", "transfer_snapshot"):
            fn = _function(tree, name)
            calls = {getattr(c.func, "attr", getattr(c.func, "id", None))
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            assert "lock_affected_cells" not in calls, name
            assert "recompute_cell" not in calls, name

    def test_the_guard_runs_before_the_lock_in_every_decision_path(self):
        """`_assert_not_under_approval` reads `approval_instance` only. Placing
        it before `lock_affected_cells` keeps the ordered cell lock the FIRST
        locking action of these functions, as `locking.py` rule 1 requires."""
        tree = ast.parse(PG_BUDGET.read_text(encoding="utf-8"))
        for name in ("approve_revision", "approve_transfer"):
            fn = _function(tree, name)
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
            calls.sort(key=lambda c: (c.lineno, c.col_offset))
            names = [getattr(c.func, "attr", getattr(c.func, "id", None)) for c in calls]
            assert "_assert_not_under_approval" in names, name
            assert names.index("_assert_not_under_approval") < \
                names.index("lock_affected_cells"), name
