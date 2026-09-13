"""Separation of duties, proved exhaustively rather than by sampling.

Wave 4 / M4b, stream 5, RECONCILED against what shipped. Three layers, and the
split is deliberate.

**Layer 1 -- the SQLite application, no PostgreSQL, runs everywhere, today.**
``auth.MAKER_CHECKER`` names five permissions. The existing suite proves three
of them behaviourally (``tests/test_api_auth.py``), asserts a fourth as a set
membership, and never exercises the fifth. A set membership is not a proof: it
passes just as happily when the enforcement call underneath has been deleted.

The parametrised test below closes that. For **every** permission in
``auth.MAKER_CHECKER`` it builds the hardest case -- an identity deliberately
granted the approving role as well as the raising one, i.e. a stage
CONFIGURED to allow the maker -- and requires the refusal anyway. Because it
is parametrised over ``auth.MAKER_CHECKER`` itself and pinned by
:func:`test_every_maker_checker_permission_has_a_behavioural_scenario`, adding
a sixth maker-checker permission without adding a scenario fails the suite
instead of silently shipping an unproven control.

These tests exist to fail if anyone deletes an
``auth.require_separation(...)`` call in ``app/backend/services.py``.

**Layer 2 -- Contract 5's rule itself, as pure functions, no database.**
``delegation.assert_delegation_independent`` and
``approval_rules.apply_contributor_filter`` are where the M4b engine's
separation lives, and both are pure. Everything about them that can be proved
without a server is proved here rather than behind the ``CAPEX_DB_URL`` gate,
because a proof that only runs in CI is a proof nobody sees while they are
writing the change that breaks it.

**Layer 3 -- the engine against a live PostgreSQL.**
That the engine actually CALLS those functions, on the paths it claims to, is
not a property of a pure function; it needs real ``approval_instance`` /
``approval_assignment`` rows. Gated on ``CAPEX_DB_URL``, and inside the gate
nothing skips -- with a database configured, the Wave 4 engine is a guaranteed
precondition, not an environment condition.

RECONCILIATION NOTES (what moved between the frozen contract and the build)
--------------------------------------------------------------------------
* ``POST /api/approvals/{id}/decide`` forbids unknown body fields and has **no
  ``acting_for_user_id``**, so a delegated decision cannot be expressed over
  HTTP at all. Layer 3 therefore drives ``approvals.decide`` directly, which is
  the only surface that takes the second identity Contract 5 is about.
* ``delegation.create_delegation`` writes the delegation row and nothing else.
  Delegates are expanded into assignments by ``resolve_stage_assignees`` when a
  stage OPENS, so a delegation added after routing changes no assignment. The
  delegation is therefore created BEFORE the object is routed, which is the
  point at which the engine actually applies it.
* ``open_instance`` records its fail-closed outcome and RETURNS it. It used to
  re-raise, and that was the defect: ``Database.session`` rolls back on any
  exception leaving the block, so the exception destroyed the
  EXCEPTION_PENDING row the engine had just written -- the object ended with no
  approval instance at all, which is the single outcome Contract 2 exists to
  prevent. The evidence was lost as a direct consequence of reporting it, and
  only a live database shows that: in memory, nothing rolls back.
  An unroutable object is a recorded OUTCOME, not a control-flow exception. The
  safety property is unchanged -- the returned status is EXCEPTION_PENDING,
  never APPROVED -- and the fail-closed code an administrator triages on is
  readable from ``approval_action.outcome``.
* ``approval_action`` gained ``idempotency_key`` and ``outcome`` and has no
  ``detail`` column (amendment A3); ``action_id`` is
  ``GENERATED ALWAYS AS IDENTITY`` and is never supplied; ``created_by`` is NOT
  NULL with no default on ``approval_definition`` / ``approval_rule`` /
  ``approval_stage``, none of which has an ``updated_by``.

Ownership: stream 5 owns this file and ``tests/test_approval_e2e.py`` only.
Defects found were reported, not patched.

WAVE 8 UPDATE. The defect this file reported -- ``bill.void`` carrying a
maker-checker call that compared nobody, because ``services.void_bill`` read a
``created_by`` column the `bill` table did not have -- has been CLOSED by the
wave that owns those files: the column exists and is seeded
(``app/backend/db.py``), and ``services.void_bill`` now passes
``require_maker=True`` so an unattributed bill is refused rather than waved
through. ``KNOWN_INERT`` is consequently empty and the strict xfail is gone;
see ``test_bill_void_maker_checker_reads_a_real_maker_column`` and
``test_an_unattributed_bill_cannot_be_voided_at_all``.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_locking.py's module docstring for why this block is copied
# rather than referenced (a second conftest.py under tests/ would shadow the
# root one and break `from conftest import ...` in the baseline modules).
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import os                                                        # noqa: E402
import uuid                                                      # noqa: E402
from datetime import date                                        # noqa: E402

import pytest                                                    # noqa: E402
from psycopg.types.json import Jsonb                             # noqa: E402

from app.backend import auth                                     # noqa: E402
from app.backend.pg import approval_rules as rules               # noqa: E402
from app.backend.pg import approvals as engine                   # noqa: E402
from app.backend.pg import delegation as delegation_mod          # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402

from conftest import code_of                                     # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="needs a live PostgreSQL (CAPEX_DB_URL); runs in the pg_tests CI job",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]

# The seeded SQLite demo project the layer-1 scenarios raise objects against.
# `1000` rupees sits inside the cell's availability and `90000000` far outside
# it; both are the amounts tests/test_api_auth.py already relies on, and each
# scenario asserts the verdict it depends on rather than assuming it.
DEMO_PROJECT = "PRJ-01"
DEMO_WBS = "W-03-01"
DEMO_HEAD = "BH-PM"
WITHIN_BUDGET_RUPEES = "1000"
OVER_BUDGET_RUPEES = "90000000"


# ==========================================================================
# Layer 1 -- every maker-checker permission, no PostgreSQL
# ==========================================================================
#
# Each scenario returns the response to a maker's attempt to approve their own
# work, having first been granted the approving role as well. The refusal must
# come from segregation of duties (403 SELF_APPROVAL), never from the
# permission check -- so every scenario asserts, as a PRECONDITION, that the
# maker actually holds the approving permission. Otherwise a scenario could
# "pass" on a 403 FORBIDDEN and prove nothing about maker-checker at all.


def _assert_holds(user_id: str, permission: str, roles: list[str]) -> None:
    """The maker must genuinely hold `permission`, or the scenario is vacuous."""
    allowed = auth.PERMISSIONS[permission]
    assert set(roles) & set(allowed), (
        f"scenario for {permission!r} gave {user_id} roles {roles}, which do "
        f"not hold it (needs one of {list(allowed)}). The refusal under test "
        f"would then be an ordinary permission denial, not maker-checker, and "
        f"the test would pass with the require_separation call deleted.")


def _scenario_pr_approve(make_user, raw_con):
    """Within-budget PR: the maker also holds ProcurementApprover."""
    roles = ["Requestor", "ProcurementApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "pr.approve", roles)

    created = maker.post("/api/purchase-requests", json={
        "project_id": DEMO_PROJECT, "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD,
        "description": "own request, within budget", "amount_rupees": WITHIN_BUDGET_RUPEES})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "Submitted", (
        "this scenario must exercise pr.approve, not pr.approve_exception; the "
        f"request routed to {created.json()['status']!r} instead")

    return maker.post(f"/api/purchase-requests/{created.json()['pr_id']}/approve",
                      json={"reason": "approving my own within-budget request"})


def _scenario_pr_approve_exception(make_user, raw_con):
    """Over-budget PR: the maker also holds FinanceApprover."""
    roles = ["BudgetController", "FinanceApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "pr.approve_exception", roles)

    created = maker.post("/api/purchase-requests", json={
        "project_id": DEMO_PROJECT, "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD,
        "description": "own request, over budget", "amount_rupees": OVER_BUDGET_RUPEES})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "Exception Pending", (
        "this scenario must exercise pr.approve_exception; the request routed "
        f"to {created.json()['status']!r} instead")

    return maker.post(f"/api/purchase-requests/{created.json()['pr_id']}/approve",
                      json={"reason": "approving my own exception"})


def _scenario_revision_approve(make_user, raw_con):
    """Budget revision: the maker also holds FinanceApprover.

    Against the SQLite surface (`POST /api/budget-revisions`), not the
    PostgreSQL one. `auth.require_separation` is the SQLite application's
    control and `services.py` is where it is called; the PostgreSQL revision
    path carries its own, separate enforcement, which
    ``tests/test_approval_e2e.py`` proves against a live database.
    """
    roles = ["BudgetController", "FinanceApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "revision.approve", roles)

    created = maker.post("/api/budget-revisions", json={
        "project_id": DEMO_PROJECT, "wbs_id": "W-03", "budget_head_id": DEMO_HEAD,
        "kind": "SUPPLEMENT", "amount_rupees": "500000",
        "reason": "own revision"})
    assert created.status_code == 201, created.text

    return maker.post(
        f"/api/budget-revisions/{created.json()['revision_id']}/approve", json={})


def _scenario_capitalisation_approve(make_user, raw_con):
    """Capitalisation request: no creation route exists, so the maker is seeded."""
    roles = ["CapitalisationApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "capitalisation.approve", roles)

    raw_con.execute(
        """INSERT INTO capitalisation_request
           (cap_id, cap_number, project_id, requested_by, requested_at, status,
            approver, approved_at, cap_date, total_paise, version_no)
           VALUES ('CAP-MC-SELF','CAP-2026-8801','PRJ-02',?,'2026-08-05T00:00:00',
                   'Submitted',NULL,NULL,'2026-08-31',0,1)""",
        (maker.user_id,))
    raw_con.commit()

    return maker.post("/api/capitalisation/CAP-MC-SELF/approve", json={})


def _scenario_bill_void(make_user, raw_con):
    """Bill void: the maker also holds FinanceApprover.

    Wave 8: this scenario used to be an expected failure. The `bill` table now
    carries `created_by` (``app/backend/db.py``), which is the column
    ``services.void_bill`` has always read, so the control is real and the
    xfail is gone -- see
    ``test_bill_void_maker_checker_reads_a_real_maker_column``.

    ``conftest.insert_bill`` does not take a maker (it predates the column and
    belongs to another owner), so the maker is written here with an explicit
    UPDATE rather than by widening a shared helper.
    """
    from conftest import insert_bill

    roles = ["FinanceApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "bill.void", roles)

    insert_bill(raw_con, bill_id="BILL-MC-SELF", bill_number="BILL-2026-8801",
                po_id=None, po_line_id=None, wbs_id=DEMO_WBS,
                budget_head_id=DEMO_HEAD, amount_paise=100_00_000)
    raw_con.execute("UPDATE bill SET created_by=? WHERE bill_id='BILL-MC-SELF'",
                    (maker.user_id,))
    raw_con.commit()

    return maker.post("/api/bills/BILL-MC-SELF/void",
                      json={"reason": "voiding a bill I raised myself"})


def _scenario_period_reopen_apply(make_user, raw_con):
    """Period reopen apply (Fable 5.1 / M-3): the requester also holds the
    applying role.

    THE PERSISTENCE UNDER THIS ROUTE IS STUBBED, AND ONLY THAT. The apply
    route is PostgreSQL-backed (`api/budget.py` -> `pg/periods.py`) and this
    layer is the SQLite application, so the engine is replaced by a sentinel
    and the requester lookup by the maker's own id. Everything the scenario
    is asserting runs for real: the route's permission dependency, the
    server-derived actor, and the `auth.require_separation(...,
    maker_user_id=<requester>, require_maker=True)` call that
    `api/budget.py::_refuse_requester_as_checker` makes BEFORE the engine.
    Neutralise that call and the sentinel answers instead of a refusal, which
    is what the companion mutation test requires.

    What this does NOT prove -- the closer-is-not-applier limb, and the whole
    chain against real rows -- is proven live in
    `tests/test_pg_periods_reopen.py` and
    `tests/test_period_reopen_permissions.py`.
    """
    import contextlib

    from app.backend.api import budget as budget_api
    from app.backend.pg import engine
    from app.backend.pg import periods as periods_svc
    from app.backend.pg.engine import Scope

    roles = ["FinanceApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "period.reopen.apply", roles)

    class _StubDatabase:
        @contextlib.contextmanager
        def session(self, scope):
            yield None

    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None

    with pytest.MonkeyPatch.context() as mp:
        engine.set_database(_StubDatabase())
        mp.setattr(budget_api, "_scope_for",
                   lambda request, database: Scope(user_id=maker.user_id))
        mp.setattr(periods_svc, "reopen_request_requester",
                   lambda session, reopen_id: maker.user_id)
        mp.setattr(periods_svc, "apply_period_reopen",
                   lambda session, **kw: {"reopen_id": kw["reopen_id"],
                                          "status": "APPLIED", "stubbed": True})
        try:
            return maker.post(
                "/api/budget/period-reopen-requests/RO-MC-SELF/apply", json={})
        finally:
            engine.set_database(previous)


def _scenario_imr_approve(make_user, raw_con):
    """Internal material request (Fable 5.1 Stream A, 034): the requester also
    holds ProcurementApprover.

    THE PERSISTENCE UNDER THIS ROUTE IS STUBBED, AND ONLY THAT -- the same
    shape as `_scenario_period_reopen_apply`. The route is PostgreSQL-backed
    (`api/internal_fulfilment.py` -> `pg/internal_fulfilment.py`) and this is
    the SQLite application, so the engine is a sentinel, the request re-read
    (`_imr`) answers the maker's own id as `requested_by`, and the first
    statement AFTER the maker-checker gate (`lock_affected_cells`) answers a
    sentinel refusal that is not SELF_APPROVAL. Everything the scenario
    asserts runs for real: the router's permission dependency, the
    server-derived actor, `auth.require` and the
    `auth.require_separation(principal, "imr.approve", requested_by)` call
    that `approve_request` makes BEFORE any lock.

    ONE MORE CONTROL IS STUBBED, and the reason is stated: this route carries
    a SECOND, independent producer of SELF_APPROVAL -- the delegation check
    `assert_delegation_independent(actor, acting_for, contributors)`, which
    refuses a maker who is among the object's contributors whether acting
    personally or for somebody else. Left in place it would answer the
    companion mutation test's neutralised `require_separation` with the very
    code that test asserts must disappear, so the scenario could not tell
    the two apart. It is replaced by a no-op HERE ONLY; its own refusals --
    direct, delegated and through the principal -- are proven against real
    rows in `tests/test_pg_internal_fulfilment_fable51.py` (the
    `SELF_APPROVAL` block around its line 729), as is the whole chain.
    """
    import contextlib

    from app.backend.api import internal_fulfilment as imr_api
    from app.backend.pg import engine
    from app.backend.pg import internal_fulfilment as imr_svc
    from app.backend.pg.engine import Scope

    roles = ["Requestor", "ProcurementApprover"]
    maker = make_user(roles)
    _assert_holds(maker.user_id, "imr.approve", roles)

    class _StubDatabase:
        @contextlib.contextmanager
        def session(self, scope):
            yield None

    def _stub_lock(session, cells):
        raise imr_svc.ProcurementError(
            "PERSISTENCE_STUBBED", "the SQLite surface carries no IMR rows", status=409)

    try:
        previous = engine.get_database()
    except RuntimeError:
        previous = None

    with pytest.MonkeyPatch.context() as mp:
        engine.set_database(_StubDatabase())
        mp.setattr(imr_api, "_scope_for",
                   lambda request, database: Scope(user_id=maker.user_id))
        mp.setattr(imr_svc, "_imr", lambda session, imr_id: {
            "imr_id": imr_id, "imr_number": "IMR-MC-SELF", "status": imr_svc.STATUS_REQUESTED,
            "requested_by": maker.user_id, "created_by": maker.user_id,
            "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD, "requested_quantity": "1"})
        mp.setattr(imr_svc, "lock_affected_cells", _stub_lock)
        mp.setattr(imr_svc, "assert_delegation_independent",
                   lambda actor, acting_for, contributors: None)
        try:
            return maker.post(
                "/api/procurement/internal-material-requests/IMR-MC-SELF/approve",
                json={})
        finally:
            engine.set_database(previous)


#: permission -> the scenario that puts a maker, holding the approving role,
#: in front of their own object. Pinned to ``auth.MAKER_CHECKER`` by the
#: exhaustiveness test below.
SCENARIOS = {
    "pr.approve": _scenario_pr_approve,
    "pr.approve_exception": _scenario_pr_approve_exception,
    "revision.approve": _scenario_revision_approve,
    "capitalisation.approve": _scenario_capitalisation_approve,
    "bill.void": _scenario_bill_void,
    "period.reopen.apply": _scenario_period_reopen_apply,
    "imr.approve": _scenario_imr_approve,
}

#: Permissions whose enforcement is currently impossible, with the reason.
#: Kept as data so the parametrisation stays generated from
#: ``auth.MAKER_CHECKER`` rather than hand-listed.
#:
#: EMPTY, and that is the point of the structure rather than an oversight.
#: ``bill.void`` lived here for three waves: ``services.void_bill`` passed
#: ``b.get("created_by")`` as the maker while the `bill` table had no such
#: column, so the maker was always ``None``, ``require_separation``
#: short-circuited on the falsy value, and a FinanceApprover could void a bill
#: they had raised themselves while the segregation-of-duties report stayed
#: clean. Wave 8 added the column to ``app/backend/db.py`` (PostgreSQL had
#: carried ``bill.created_by NOT NULL`` since migration 013), seeds it, and
#: passes ``require_maker=True`` so an unattributed bill is REFUSED rather
#: than waved through. The xfail was strict, so the fix could not land without
#: this dictionary being emptied in the same change.
KNOWN_INERT: dict[str, str] = {}


def _params():
    """One parameter per permission in ``auth.MAKER_CHECKER``, generated from
    the set itself so the parametrisation cannot drift away from it."""
    out = []
    for permission in sorted(auth.MAKER_CHECKER):
        marks = []
        if permission in KNOWN_INERT:
            marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_INERT[permission]))
        out.append(pytest.param(permission, marks=marks, id=permission))
    return out


def test_every_maker_checker_permission_has_a_behavioural_scenario():
    """Adding a permission to MAKER_CHECKER must force a proof of it.

    Set equality both ways: a permission with no scenario is an unproven
    control, and a scenario for a permission no longer in MAKER_CHECKER is
    dead weight that would quietly stop testing anything.
    """
    assert set(SCENARIOS) == set(auth.MAKER_CHECKER), (
        "maker-checker scenarios and auth.MAKER_CHECKER have diverged.\n"
        f"  in MAKER_CHECKER with no scenario: {sorted(set(auth.MAKER_CHECKER) - set(SCENARIOS))}\n"
        f"  scenario for a non-maker-checker permission: {sorted(set(SCENARIOS) - set(auth.MAKER_CHECKER))}")


@pytest.mark.parametrize("permission", _params())
def test_a_maker_holding_the_approving_role_is_still_refused(permission, make_user, raw_con):
    """The whole control, for every permission that claims to carry it.

    The identity here is configured to allow the maker: it holds the approving
    role, so ``auth.require`` passes and the ONLY thing standing between it and
    approving its own work is ``auth.require_separation``. Delete that call in
    ``app/backend/services.py`` and this test goes red for every permission at
    once, which is exactly the regression cover it exists to provide.
    """
    resp = SCENARIOS[permission](make_user, raw_con)

    assert resp.status_code == 403, (
        f"{permission}: a maker holding the approving role approved their own "
        f"object and got {resp.status_code}. Segregation of duties was not "
        f"applied.\nResponse: {resp.text}")
    assert code_of(resp) == "SELF_APPROVAL", (
        f"{permission}: refused, but with {code_of(resp)!r} rather than "
        f"SELF_APPROVAL. A permission denial is not maker-checker; the caller "
        f"deliberately holds the approving role here.")


@pytest.mark.parametrize("permission", [
    p for p in sorted(SCENARIOS) if p not in KNOWN_INERT])
def test_the_refusal_comes_from_require_separation_and_nothing_else(
        permission, make_user, raw_con, monkeypatch):
    """Mutation check: neutralise the control, and the refusal must disappear.

    Without this, the test above could be passing for an incidental reason --
    a status transition guard, a missing field, a permission denial -- and
    would keep passing after somebody deleted
    ``auth.require_separation(...)`` from ``app/backend/services.py``. That is
    exactly the failure mode a set-membership assertion has.

    Here ``require_separation`` is replaced with a no-op for the duration of
    one scenario. The SELF_APPROVAL refusal must then be gone.

    What is asserted is the disappearance of that code, not a 200. Some
    scenarios meet a further, unrelated gate once separation stops refusing --
    ``capitalisation.approve`` on the demo project hits CAPITALISATION_BLOCKED,
    because PRJ-02 carries open commitment. That is a different control doing
    its own job downstream, and demanding a 200 would make this test assert
    the absence of every other control in the product rather than the presence
    of this one.
    """
    monkeypatch.setattr(auth, "require_separation",
                        lambda *args, **kwargs: None)

    resp = SCENARIOS[permission](make_user, raw_con)

    assert resp.status_code != 403 or code_of(resp) != "SELF_APPROVAL", (
        f"{permission}: require_separation was neutralised and the request was "
        f"STILL refused with SELF_APPROVAL ({resp.status_code}: {resp.text}).\n"
        "The companion test's 403 therefore does not prove that call is what "
        "enforces maker-checker here -- something else produces the same code, "
        "and deleting the require_separation call would go unnoticed.")


def test_bill_void_maker_checker_reads_a_real_maker_column(raw_con):
    """The CLOSURE of the defect this file reported for three waves.

    Formerly ``test_bill_void_maker_checker_is_structurally_inert``, which
    asserted the cause: ``services.void_bill`` passed ``b.get("created_by")``
    as the maker while the `bill` table had no such column, so the maker was
    always ``None``, ``require_separation`` short-circuited, and the call
    enforced nothing.

    It now asserts the opposite, against the same schema, so the fix cannot be
    reverted quietly: the column must exist, the seeded bills must actually
    carry a maker (a column of NULLs enforces exactly as much as no column at
    all), and ``services.void_bill`` must still be reading it.
    """
    columns = {row[1] for row in raw_con.execute("PRAGMA table_info(bill)")}
    assert columns, "the `bill` table does not exist; this assertion is against the wrong schema"

    assert "created_by" in columns, (
        "the `bill` table has lost its maker column. `bill.void` is in "
        "auth.MAKER_CHECKER and services.void_bill reads `created_by`; without "
        "the column the maker is always None and segregation of duties on a "
        "void enforces nothing.")
    assert "voided_by" in columns, (
        "sanity check: migration 002 adds voided_by, so this IS the table "
        "carrying the void lifecycle.")

    attributed = raw_con.execute(
        "SELECT COUNT(*) FROM bill WHERE created_by IS NOT NULL AND created_by <> ''"
    ).fetchone()[0]
    total = raw_con.execute("SELECT COUNT(*) FROM bill").fetchone()[0]
    assert total and attributed == total, (
        f"{total - attributed} of {total} seeded bills record no maker. A "
        "column that is always NULL restores the exact defect it was added to "
        "close, because require_separation cannot compare against NULL.")

    source = (PROJECT_ROOT / "app" / "backend" / "services.py").read_text(encoding="utf-8")
    assert 'auth.require_separation(actor, "bill.void", b.get("created_by")' in source, (
        "services.void_bill no longer passes the bill's created_by to "
        "require_separation. The column exists but nothing reads it, which is "
        "the same inert control wearing a schema.")
    assert "require_maker=True" in source, (
        "services.void_bill no longer demands a known maker. An unattributed "
        "bill would then be voidable by anyone, which is how this defect "
        "behaved before it was found.")


def test_require_separation_refuses_rather_than_waves_through_an_unknown_maker():
    """The mechanism behind the original finding, and its fix.

    Refusing to GUESS a maker is correct, so the permissive default stays:
    several callers resolve the maker from a row they may not be able to read,
    and turning "not found" into a segregation refusal would answer the wrong
    question. What was wrong was that a caller who MUST know the maker had no
    way to say so, and silence read as permission.

    ``require_maker=True`` is that way. Both halves are asserted here, because
    a fail-closed flag that also fires on the permissive path would break every
    other call site, and one that never fires would be decoration.
    """
    approver = {"user_id": "U-SOMEONE", "roles": ["FinanceApprover"]}

    # permissive by default, unchanged
    auth.require_separation(approver, "bill.void", None, object_label="a bill")

    # fail-closed on request
    with pytest.raises(auth.AuthError) as unknown:
        auth.require_separation(approver, "bill.void", None,
                                object_label="a bill", require_maker=True)
    assert unknown.value.code == "MAKER_UNKNOWN"
    assert unknown.value.status == 403

    # a permission that is NOT maker-checked is never refused by either mode
    auth.require_separation(approver, "budget.read", None,
                            object_label="a budget", require_maker=True)

    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_separation(approver, "bill.void", "U-SOMEONE", object_label="a bill")
    assert excinfo.value.code == "SELF_APPROVAL"

    # and an independent maker is still permitted, in both modes
    auth.require_separation(approver, "bill.void", "U-SOMEBODY-ELSE",
                            object_label="a bill", require_maker=True)


def test_an_unattributed_bill_cannot_be_voided_at_all(make_user, raw_con):
    """Fail-closed, end to end: no maker recorded means no void.

    The regression this guards is precise. A future ingestion path that writes
    `bill` rows without a `created_by` would not break any test that asserts a
    maker is refused -- it would simply produce bills nobody is recorded as
    having raised, and the segregation check would go back to comparing
    ``None`` against a user id and permitting everyone.
    """
    from conftest import insert_bill

    voider = make_user(["FinanceApprover"])
    insert_bill(raw_con, bill_id="BILL-MC-ORPHAN", bill_number="BILL-2026-8802",
                po_id=None, po_line_id=None, wbs_id=DEMO_WBS,
                budget_head_id=DEMO_HEAD, amount_paise=50_00_000)
    raw_con.commit()
    assert raw_con.execute(
        "SELECT created_by FROM bill WHERE bill_id='BILL-MC-ORPHAN'"
    ).fetchone()[0] is None, "this scenario requires a bill with no recorded maker"

    resp = voider.post("/api/bills/BILL-MC-ORPHAN/void",
                       json={"reason": "voiding a bill nobody is recorded as raising"})

    assert resp.status_code == 403, (
        "a bill with no recorded maker was voided anyway "
        f"({resp.status_code}: {resp.text}). Segregation of duties could not be "
        "verified, and 'cannot verify' was treated as 'permitted'.")
    assert code_of(resp) == "MAKER_UNKNOWN", (
        f"refused, but with {code_of(resp)!r}. The caller must be told the "
        "maker is unknown, not that they approved their own work.")


def test_an_independent_approver_is_not_blocked_by_maker_checker(make_user, raw_con):
    """The control must refuse the maker, not everybody.

    A separation check that refused every caller would pass every test above
    while making the product unusable, so the positive case is proved in the
    same file.
    """
    maker = make_user(["Requestor"])
    created = maker.post("/api/purchase-requests", json={
        "project_id": DEMO_PROJECT, "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD,
        "description": "raised by one person", "amount_rupees": WITHIN_BUDGET_RUPEES})
    assert created.status_code == 201, created.text

    approver = make_user(["ProcurementApprover"])
    resp = approver.post(f"/api/purchase-requests/{created.json()['pr_id']}/approve",
                         json={"reason": "independent approval"})
    assert resp.status_code == 200, (
        f"an independent approver was refused: {resp.text}")
    assert resp.json()["approver"] == approver.user_id
    assert resp.json()["exception"] is False


# ==========================================================================
# Layer 2 -- Contract 5's rule as pure functions, no database
# ==========================================================================
#
# `assert_delegation_independent` is the single function the engine routes
# EVERY decision through -- delegated or not, with `acting_for_user_id=None`
# for a direct one -- so it is the one place the "both identities" rule can be
# proved exhaustively. Doing that here rather than behind the CAPEX_DB_URL gate
# means the proof runs on the machine where somebody is editing it.


def test_a_direct_decision_by_a_contributor_is_refused():
    """The base case: the actor alone, with no delegation in sight."""
    with pytest.raises(delegation_mod.DelegatedSelfApproval) as excinfo:
        delegation_mod.assert_delegation_independent(
            "U-MAKER", None, {"U-MAKER", "U-EDITOR"})
    assert excinfo.value.code == rules.ERR_SELF_APPROVAL


@pytest.mark.parametrize("actor,acting_for,barred", [
    # the obvious attack: a contributor decides under somebody else's authority
    ("U-MAKER", "U-CLEAN", "U-MAKER"),
    # the mirror image, and the one a single-identity check misses: a clean
    # actor decides "on behalf of" the person who raised the document
    ("U-CLEAN", "U-MAKER", "U-MAKER"),
    # both dirty
    ("U-MAKER", "U-EDITOR", "U-MAKER"),
    # the acting-for identity is a prior actor rather than the maker
    ("U-CLEAN", "U-EDITOR", "U-EDITOR"),
])
def test_a_delegation_cannot_launder_a_self_approval(actor, acting_for, barred):
    """Contract 5: refused if EITHER identity is in the contributor set.

    Parametrised over both directions on purpose. An implementation that
    checked only ``actor_user_id`` passes half of these and lets a maker
    approve their own object through somebody else's hands; one that checked
    only ``acting_for_user_id`` passes the other half and lets a contributor
    approve directly. Only checking both passes all four.
    """
    contributors = {"U-MAKER", "U-EDITOR"}

    with pytest.raises(delegation_mod.DelegatedSelfApproval) as excinfo:
        delegation_mod.assert_delegation_independent(actor, acting_for, contributors)

    assert excinfo.value.code == rules.ERR_SELF_APPROVAL, (
        f"actor={actor}, acting_for={acting_for}: refused with "
        f"{excinfo.value.code!r} rather than SELF_APPROVAL")
    assert excinfo.value.status == 403
    assert barred in str(excinfo.value), (
        f"the refusal does not name {barred}, who is the contributor it turned "
        f"on. An approver reading this message cannot act on it: {excinfo.value}")


def test_two_independent_identities_are_not_refused():
    """The control must refuse contributors, not everybody.

    Without this, ``assert_delegation_independent = raise`` would satisfy every
    assertion above while making the product unusable.
    """
    delegation_mod.assert_delegation_independent(
        "U-CLEAN", "U-ALSO-CLEAN", {"U-MAKER", "U-EDITOR"})
    delegation_mod.assert_delegation_independent(
        "U-CLEAN", None, {"U-MAKER"})
    # An empty contributor set bars nobody; a falsy id in the set is not a bar.
    delegation_mod.assert_delegation_independent("U-CLEAN", None, set())
    delegation_mod.assert_delegation_independent("U-CLEAN", None, {"", None})


def test_a_stage_emptied_by_the_contributor_filter_raises_rather_than_returning_empty():
    """Contract 2's fail-closed rule, at the function that decides it.

    ``apply_contributor_filter`` must never hand a caller an empty
    ``AssigneeSet``: the one reading of "nobody is left to approve this" that
    must never happen is "so it is approved", and a falsy return value is an
    open invitation to exactly that.
    """
    maker = rules.Assignee(user_id="U-MAKER", assigned_via=rules.VIA_ROLE)

    with pytest.raises(rules.NoIndependentApprover) as excinfo:
        rules.apply_contributor_filter([maker], {"U-MAKER"})

    assert excinfo.value.code == rules.ERR_NO_INDEPENDENT_APPROVER
    assert excinfo.value.detail["excluded_contributors"] == ["U-MAKER"], (
        "the exception does not say WHO was removed. 'There is nobody to "
        "approve this' is not an actionable message for the administrator who "
        "has to widen the role, the scope or the delegation.")


def test_the_contributor_filter_runs_after_delegates_are_added():
    """Order is the control (section 9.2 step 5), so it is proved, not assumed.

    A delegate who is themselves a contributor must not slip in through the
    delegation route. Expanding delegates and THEN filtering is what makes that
    true; filtering first and delegating second would add the maker back after
    the bar had been applied.
    """
    approver = rules.Assignee(user_id="U-APPROVER", assigned_via=rules.VIA_ROLE)
    to_the_maker = delegation_mod.Delegation(
        delegation_id="ADLG-TEST", delegator_user_id="U-APPROVER",
        delegate_user_id="U-MAKER", scope_key=None,
        active_from=date(2026, 1, 1), active_to=date(2027, 1, 1))

    with_delegates = delegation_mod.expand_delegates(
        [approver], [to_the_maker], date(2026, 6, 1),
        scope_key="PRJ-ANY", allow_delegation=True)
    assert "U-MAKER" in {a.user_id for a in with_delegates}, (
        "precondition: the delegation must actually add the maker, or the "
        "filter below has nothing to remove and this test is vacuous")

    kept = rules.apply_contributor_filter(with_delegates, {"U-MAKER"})

    assert kept.user_ids == ("U-APPROVER",), (
        f"the maker survived the filter as {kept.user_ids}. A delegation from "
        "an approver to the person who raised the document would then put that "
        "person on their own approval.")
    assert "U-MAKER" in kept.excluded_contributors


# ==========================================================================
# Layer 3 -- the engine, against a live PostgreSQL
# ==========================================================================
#
# The pure functions above cannot prove that the engine CALLS them. These do,
# against real rows, and they read the LEDGER rather than a return value: a
# refusal that nevertheless moved the instance, or left an APPROVE in the
# append-only action log, is the defect worth catching.

#: A `role_grant` CHECK value. The PostgreSQL role vocabulary is NOT the
#: SQLite one -- `role_grant` names business roles ('Project Finance
#: Controller'), `user_role` names permission roles ('FinanceApprover') -- and
#: an approval stage resolves against the former.
APPROVER_ROLE = "Project Finance Controller"

#: Fixed so a route never depends on the day the suite runs.
BUSINESS_DATE = date(2026, 6, 1)


def _system_scope() -> Scope:
    """Unrestricted: these tests are about separation, and a scope refusal
    here would be a different control failing."""
    return Scope(user_id="U-TEST-MC", principal_kind="USER", read_all=True)


class _Estate:
    """A disposable approval estate, plus the levers the layer-3 tests pull.

    Deliberately synthetic rather than the seeded demo dataset: these tests
    need particular contributor and delegation arrangements
    (``tests/test_approval_e2e.py`` is the one that runs the demo estate), and
    an estate built here is one whose every relevant row is visible in this
    file.
    """

    def __init__(self, connection, database, suffix):
        self._con = connection
        self._database = database
        self.suffix = suffix
        self.entity = f"E_{suffix}"
        self.project = f"P_{suffix}"
        self.wbs = f"W_{suffix}"
        self.head = f"H_{suffix}"

    # ------------------------------------------------------------- seeding
    def seed_estate(self, *, budget_paise=100_000_000):
        ex = self._con.execute
        # `approval_action.actor_user_id` is a foreign key to `app_user`, and
        # `approvals.py` attributes every system-initiated action (an
        # unroutable object, an empty approver set, an SLA escalation) to
        # actor_user_id='SYSTEM'. In the real estate this row comes from
        # `migrations/pg/seed_parts/008_approvals.sql`, which this synthetic
        # estate deliberately does not load (see the class docstring); without
        # it here, every fail-closed path this suite exercises raises
        # ForeignKeyViolation instead of reaching EXCEPTION_PENDING. Same
        # `principal_kind='SERVICE'`, no role grant, `ON CONFLICT DO NOTHING`
        # in case a suffix is ever reused within one connection.
        ex("INSERT INTO app_user (user_id, email, display_name, principal_kind, "
           "created_by, updated_by) VALUES ('SYSTEM', 'system@capex.invalid', "
           "'System', 'SERVICE', 'SEED', 'SEED') ON CONFLICT (user_id) DO NOTHING")
        org = f"O_{self.suffix}"
        ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{self.suffix}", "Org"))
        ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'t','t')", (self.entity, org, f"EC_{self.suffix}", "Entity"))
        ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'t','t')", (self.project, self.entity, f"C_{self.suffix}", "Project"))
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
           "VALUES (%s,%s,%s,%s,'t','t')", (self.head, self.entity, f"HC_{self.suffix}", "Head"))
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
           "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')",
           (self.wbs, self.project, self.wbs, "n", self.wbs))
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
           "VALUES (%s,%s,%s,'t')", (self.wbs, self.head, budget_paise))
        ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, commitment_paise, "
           "actual_paise, pr_reserved_paise, updated_by) VALUES (%s,%s,0,0,0,'t')",
           (self.wbs, self.head))
        self._con.commit()

    def seed_users(self, users: dict[str, list[str]]):
        """`users` maps a user id to `role_grant` role names (possibly empty)."""
        for user_id, roles in users.items():
            self._con.execute(
                "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
                "VALUES (%s,%s,%s,'t','t') ON CONFLICT (user_id) DO NOTHING",
                (user_id, f"{user_id}@example.test", user_id))
            for role in roles:
                self._con.execute(
                    "INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s,%s,'t') "
                    "ON CONFLICT (user_id, role) DO NOTHING", (user_id, role))
        self._con.commit()

    def seed_definition(self, *, approvers, object_type="BUDGET_REVISION",
                        allow_delegation=True, requires_reason=False,
                        quorum_type=None, quorum_n=None, tag="D"):
        """One ACTIVE definition, one catch-all rule, one stage.

        ``created_by`` is supplied on all three configuration tables: it is NOT
        NULL with no default. None of them has an ``updated_by`` -- an ACTIVE
        definition is immutable, so a last-updated column would only ever
        restate ``created_by``. Both of those are visible ONLY against live
        PostgreSQL, and both have already cost this project a CI cycle.
        """
        definition_id = f"AD_{self.suffix}_{tag}"
        self._con.execute(
            "INSERT INTO approval_definition (definition_id, object_type, code, version, "
            "status, entity_id, effective_from, created_by, activated_at, activated_by) "
            "VALUES (%s,%s,%s,1,%s,%s,%s,'TEST',now(),'TEST')",
            (definition_id, object_type, f"CODE_{self.suffix}_{tag}", rules.DEF_ACTIVE,
             self.entity, date(2020, 1, 1)))
        self._con.execute(
            "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
            "created_by) VALUES (%s,%s,10,%s,'TEST')",
            (f"AR_{self.suffix}_{tag}", definition_id, Jsonb({"op": "true"})))
        stage_id = f"AS_{self.suffix}_{tag}"
        self._con.execute(
            "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
            "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
            "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
            "created_by) VALUES (%s,%s,1,%s,NULL,%s,%s,NULL,24,NULL,NULL,%s,%s,'TEST')",
            (stage_id, definition_id, "Independent approval",
             quorum_type or rules.QUORUM_ALL, quorum_n, allow_delegation, requires_reason))
        for ordinal, (kind, ref) in enumerate(approvers, start=1):
            self._con.execute(
                "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
                "approver_ref, scope_expr) VALUES (%s,%s,%s,%s,NULL)",
                (stage_id, ordinal, kind, ref))
        self._con.commit()
        return definition_id

    def seed_revision(self, *, created_by, tag="R", delta_paise=1_000_000):
        revision_id = f"BR_{self.suffix}_{tag}"
        self._con.execute(
            "INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, "
            "delta_paise, effective_from, justification, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,'test','DRAFT',%s)",
            (revision_id, self.wbs, self.head, delta_paise, date(2026, 1, 1), created_by))
        self._con.commit()
        return revision_id

    def seed_delegation(self, *, delegator, delegate, tag="G"):
        """A live, scope-wide delegation, written BEFORE anything is routed.

        ``create_delegation`` writes the delegation row and nothing else:
        delegates become assignments in ``resolve_stage_assignees``, when a
        stage OPENS. A delegation added after routing therefore changes no
        assignment, which is why this is arranged first.
        """
        with self._database.session(_system_scope()) as session:
            return delegation_mod.create_delegation(
                session, delegator_user_id=delegator, delegate_user_id=delegate,
                scope_key=None, active_from=date(2026, 1, 1),
                active_to=date(2027, 1, 1), created_by="TEST")

    def contribute(self, object_id, *, actor, object_type="BUDGET_REVISION"):
        """Make `actor` a contributor to an already-routed document.

        ``contributor_set`` unions the maker, everyone on the document's own
        audit stream, and every prior approval actor. Appending to the audit
        stream is what an EDIT leaves behind, so this is the realistic way for
        somebody to become a contributor after the object was routed -- and the
        point is that the check runs again at decision time, not only at
        routing.
        """
        from app.backend.pg import audit as audit_mod

        with self._database.session(_system_scope()) as session:
            audit_mod.append(session, actor, "REVISION_EDIT", object_type, object_id,
                             "edited after routing")

    # ------------------------------------------------------------- routing
    def route(self, object_id, *, maker, object_type="BUDGET_REVISION",
              snapshot=None, object_version=1):
        with self._database.session(_system_scope()) as session:
            return engine.open_instance(
                session, object_type=object_type, object_id=object_id,
                object_version=object_version,
                snapshot=snapshot or self.snapshot(object_id, object_type=object_type),
                maker_user_id=maker, business_date=BUSINESS_DATE)

    def route_failing_closed(self, code, object_id, *, maker,
                             object_type="BUDGET_REVISION", snapshot=None):
        """Route, require the fail-closed outcome `code`, and KEEP the row.

        This was `route_expecting(exc_type, ...)`, catching INSIDE the session
        block because `open_instance` re-raised after writing its
        `EXCEPTION_PENDING` row, and `Database.session` rolls back on any
        exception leaving the block -- so a caller who let it propagate
        destroyed the very row Contract 2 requires to be visible (SCR-25). The
        docstring called that "a trap every real caller will hit too", which
        was true and was an argument for removing the trap, not documenting it.

        `open_instance` now RETURNS that instance instead. The assertion is not
        weakened by the change: the exception type carried exactly one thing
        this suite ever read off it, `exc.code`, and that same code is written
        to `approval_action.outcome->>'code'` on every fail-closed branch.
        Reading it from the row is strictly the better check, because it also
        proves the administrator triaging SCR-25 can machine-read the cause --
        which an exception that never reached a table could not.

        A silent success is still the worst available outcome, and is still
        refused here: the returned status must be EXCEPTION_PENDING.
        """
        with self._database.session(_system_scope()) as session:
            try:
                instance = engine.open_instance(
                    session, object_type=object_type, object_id=object_id,
                    object_version=1,
                    snapshot=snapshot or self.snapshot(object_id,
                                                       object_type=object_type),
                    maker_user_id=maker, business_date=BUSINESS_DATE)
            except Exception as exc:                              # noqa: BLE001
                # Converted rather than propagated. The most likely wrong
                # exception here is a foreign-key violation on
                # `approval_action.actor_user_id`, because `open_instance`
                # attributes its fail-closed ESCALATE to the literal "SYSTEM"
                # -- see
                # test_the_fail_closed_paths_attribute_their_action_to_an_unprovisioned_principal.
                # A raw psycopg traceback in CI would send the next reader
                # looking for a test bug instead. Propagating it would also
                # roll the row back, which is the defect this contract change
                # exists to fix.
                raise AssertionError(
                    f"routing {object_id} raised {type(exc).__name__} instead "
                    f"of returning a fail-closed {code} instance:\n"
                    f"  {exc}\n"
                    f"If this is a ForeignKeyViolation on actor_user_id, the "
                    f"cause is the unprovisioned SYSTEM principal, the "
                    f"EXCEPTION_PENDING row has been lost with the aborted "
                    f"transaction, and Contract 2's visible fail-closed "
                    f"outcome is unreachable against a real database.") from exc

        assert instance is not None and \
            instance.get("status") == rules.INST_EXCEPTION_PENDING, (
                f"routing {object_id} was expected to fail closed with {code} "
                f"and returned {instance!r}. Contract 2 admits no route to "
                f"auto-approval, so a silent success here is the worst outcome "
                f"available.")

        recorded = self._fetchone(
            "SELECT outcome->>%s FROM approval_action WHERE instance_id = %s "
            "ORDER BY seq DESC LIMIT 1",
            ("code", instance["instance_id"]))
        assert recorded and recorded[0] == code, (
            f"the fail-closed outcome recorded on the audit action was "
            f"{recorded and recorded[0]!r}, not the frozen {code!r}. An "
            f"administrator triaging the queue cannot tell which fail-closed "
            f"cause applied.")
        return instance

    def snapshot(self, object_id, *, object_type="BUDGET_REVISION",
                 amount_paise=1_000_000, available_paise=100_000_000):
        """The routing snapshot. ``entity_id`` is mandatory: ``approval_instance``
        declares it NOT NULL REFERENCES entity."""
        return {
            "object_type": object_type,
            "object_id": object_id,
            "entity_id": self.entity,
            "project_id": self.project,
            "budget_head_id": self.head,
            "amount_paise": amount_paise,
            "affected_cells": [{"wbs_id": self.wbs, "budget_head_id": self.head}],
            "budget_checks": [{"wbs_id": self.wbs, "budget_head_id": self.head,
                               "requested_paise": amount_paise,
                               "available_paise": available_paise,
                               "verdict": "OK"}],
        }

    # ------------------------------------------------------------ decisions
    def decide(self, instance_id, *, actor, acting_for=None, action="APPROVE",
               reason_text="layer-3 decision", object_version=1):
        with self._database.session(_system_scope()) as session:
            return engine.decide(
                session, instance_id=instance_id, actor_user_id=actor,
                action=action, idempotency_key=f"mc-{uuid.uuid4().hex[:12]}",
                object_version=object_version, reason_text=reason_text,
                acting_for_user_id=acting_for)

    # --------------------------------------------------------------- ledger
    def _fetchone(self, statement, params):
        with self._database.session(_system_scope()) as session:
            return session.fetchone(statement, params)

    def _fetchall(self, statement, params):
        with self._database.session(_system_scope()) as session:
            return session.fetchall(statement, params)

    def instance_status(self, instance_id) -> str:
        row = self._fetchone(
            "SELECT status FROM approval_instance WHERE instance_id = %s",
            (instance_id,))
        assert row is not None, (
            f"approval_instance {instance_id!r} does not exist -- the engine "
            f"recorded nothing, or the transaction that wrote it rolled back.")
        return row[0]

    def instance_id_for(self, object_id) -> str:
        row = self._fetchone(
            "SELECT instance_id FROM approval_instance WHERE object_id = %s "
            "ORDER BY opened_at DESC LIMIT 1", (object_id,))
        assert row is not None, (
            f"no approval_instance was recorded for {object_id!r}. Contract 2 "
            f"requires the fail-closed outcome to be VISIBLE (SCR-25): an "
            f"unroutable object that leaves no row behind is an object nobody "
            f"will ever look at.")
        return row[0]

    def assignment(self, instance_id, user_id) -> tuple | None:
        return self._fetchone(
            """SELECT a.state, a.assigned_via, a.delegated_from
               FROM approval_assignment a
               JOIN approval_stage_instance s USING (stage_instance_id)
               WHERE s.instance_id = %s AND a.assignee_user_id = %s""",
            (instance_id, user_id))

    def approve_actions(self, instance_id) -> list[tuple]:
        return self._fetchall(
            "SELECT seq, actor_user_id, acting_for_user_id FROM approval_action "
            "WHERE instance_id = %s AND action = 'APPROVE' ORDER BY seq",
            (instance_id,))

    def assert_not_approved(self, instance_id):
        """The refusal must hold in the LEDGER, not only in the exception.

        A refusal that nevertheless moved the instance, or left an APPROVE in
        the append-only action log, is the defect worth catching -- and
        ``approval_action`` is append-only by trigger, so it is permanent.
        """
        status = self.instance_status(instance_id)
        assert status != rules.INST_APPROVED, (
            f"the decision was refused but approval_instance {instance_id} is "
            f"{status!r}. The refusal did not hold in the database.")
        recorded = self.approve_actions(instance_id)
        assert not recorded, (
            f"{len(recorded)} APPROVE action(s) recorded for a refused "
            f"decision: {recorded}. approval_action is append-only, so this is "
            f"permanent.")


@pytest.fixture()
def estate(pg_connection, pg_database):
    """A disposable estate with a maker, an approver and a bystander."""
    built = _Estate(pg_connection, pg_database, uuid.uuid4().hex[:8].upper())
    built.seed_estate()
    built.seed_users({
        "U-MC-MAKER": [],
        "U-MC-APPROVER": [APPROVER_ROLE],
        "U-MC-OTHER": [],
    })
    return built


@pytest.mark.pg
@PG
def test_a_decision_taken_on_the_makers_behalf_is_refused(estate):
    """Contract 5, second identity: the attack a single-identity check misses.

    A clean, genuinely assigned approver decides "on behalf of" the person who
    raised the document. Were only ``actor_user_id`` checked, this would
    succeed and the maker would have approved their own object through somebody
    else's hands.
    """
    estate.seed_definition(approvers=[(rules.APPROVER_ROLE, APPROVER_ROLE)])
    revision = estate.seed_revision(created_by="U-MC-MAKER")
    routed = estate.route(revision, maker="U-MC-MAKER")
    instance_id = routed["instance_id"]

    assert estate.assignment(instance_id, "U-MC-APPROVER") is not None, (
        "precondition: the approver must genuinely be an assignee, or the "
        "refusal below would be NOT_AN_ASSIGNEE and would prove nothing about "
        "maker-checker")

    with pytest.raises(rules.ApprovalError) as excinfo:
        estate.decide(instance_id, actor="U-MC-APPROVER", acting_for="U-MC-MAKER")

    assert excinfo.value.code == rules.ERR_SELF_APPROVAL, (
        f"a decision taken on the MAKER's behalf was refused with "
        f"{excinfo.value.code!r} rather than SELF_APPROVAL. Contract 5 requires "
        f"BOTH identities to be checked against the contributor set.")
    estate.assert_not_approved(instance_id)


@pytest.mark.pg
@PG
def test_an_actor_who_became_a_contributor_after_routing_is_refused(estate):
    """Contract 5, first identity, re-checked INSIDE the approving transaction.

    The approver is independent when the object is routed and edits it
    afterwards, which puts them on the document's audit stream and therefore in
    ``contributor_set``. Their assignment still stands. An engine that filtered
    contributors only at routing time would let them approve the edit they
    themselves made; ``decide`` recomputes the set under the locks, which is
    what this proves.
    """
    estate.seed_definition(approvers=[(rules.APPROVER_ROLE, APPROVER_ROLE)])
    revision = estate.seed_revision(created_by="U-MC-MAKER")
    routed = estate.route(revision, maker="U-MC-MAKER")
    instance_id = routed["instance_id"]

    assert estate.assignment(instance_id, "U-MC-APPROVER") is not None, (
        "precondition: the approver was assigned while still independent")

    estate.contribute(revision, actor="U-MC-APPROVER")

    with pytest.raises(rules.ApprovalError) as excinfo:
        estate.decide(instance_id, actor="U-MC-APPROVER")

    assert excinfo.value.code == rules.ERR_SELF_APPROVAL, (
        f"the approver edited the document after being assigned to it and was "
        f"then refused with {excinfo.value.code!r} rather than SELF_APPROVAL. "
        f"contributor_set unions the document's audit-stream actors precisely "
        f"so an edit counts as contribution.")
    estate.assert_not_approved(instance_id)


@pytest.mark.pg
@PG
def test_a_delegation_to_the_maker_never_produces_an_assignment(estate):
    """The engine's stronger answer to delegated self-approval.

    The maker holds a delegation from the only approver -- the obvious attack.
    Section 9.2 step 5 expands delegates and only THEN subtracts contributors,
    so the maker is added and immediately removed, and never becomes an
    assignee at all. The refusal therefore happens before any decision is
    taken rather than at the point one is attempted, which is the safer place
    for it.

    The stage names two approvers so that removing the maker does not empty it;
    the empty case is a different outcome, proved separately below.
    """
    estate.seed_users({"U-MC-SECOND": [APPROVER_ROLE]})
    estate.seed_delegation(delegator="U-MC-APPROVER", delegate="U-MC-MAKER")
    estate.seed_definition(approvers=[(rules.APPROVER_ROLE, APPROVER_ROLE)])
    revision = estate.seed_revision(created_by="U-MC-MAKER")

    routed = estate.route(revision, maker="U-MC-MAKER")
    instance_id = routed["instance_id"]

    assert estate.assignment(instance_id, "U-MC-MAKER") is None, (
        "the maker holds a delegation from an approver and was ASSIGNED to "
        "their own object. apply_contributor_filter runs after "
        "expand_delegates precisely so a delegate of an approver who is also "
        "the maker is removed again.")
    assert estate.assignment(instance_id, "U-MC-APPROVER") is not None, (
        "the delegating approver lost their own assignment: delegation adds "
        "capacity, it never withdraws the delegator")


@pytest.mark.pg
@PG
def test_adding_a_delegate_leaves_the_delegators_assignment_pending(estate):
    """Delegation adds capacity; it never removes accountability.

    If a delegator's own assignment were withdrawn when they delegated,
    delegation would be a way to disappear from an approval you were named on
    -- and the record would show you were never asked.

    The delegation is created BEFORE routing because that is when the engine
    applies it: ``create_delegation`` writes one row, and
    ``resolve_stage_assignees`` is what turns a delegation into an assignment,
    at the moment a stage opens.
    """
    estate.seed_delegation(delegator="U-MC-APPROVER", delegate="U-MC-OTHER")
    estate.seed_definition(approvers=[(rules.APPROVER_ROLE, APPROVER_ROLE)])
    revision = estate.seed_revision(created_by="U-MC-MAKER")

    routed = estate.route(revision, maker="U-MC-MAKER")
    instance_id = routed["instance_id"]

    delegator = estate.assignment(instance_id, "U-MC-APPROVER")
    assert delegator is not None, "the delegator was not assigned at all"
    assert delegator[0] == rules.ASSIGN_PENDING, (
        f"{'U-MC-APPROVER'} delegated to U-MC-OTHER and their own assignment "
        f"is {delegator[0]!r}. Contract 5's delegation model ADDS an assignee; "
        f"it does not withdraw the delegator, who remains accountable.")

    delegate = estate.assignment(instance_id, "U-MC-OTHER")
    assert delegate is not None, (
        "the delegate was not assigned, so the delegation added no capacity "
        "either -- it changed nothing at all")
    assert delegate[0] == rules.ASSIGN_PENDING
    assert delegate[1] == rules.VIA_DELEGATION, (
        f"the delegate's assignment records assigned_via={delegate[1]!r}; an "
        f"auditor reading the row cannot tell it came from a delegation.")
    assert delegate[2] == "U-MC-APPROVER", (
        f"delegated_from is {delegate[2]!r}, so the row does not say WHOSE "
        f"authority the delegate is acting on.")


@pytest.mark.pg
@PG
def test_an_empty_approver_set_is_exception_pending_never_an_approval(estate):
    """Contract 2: an empty approver set after the contributor filter fails closed.

    The stage names exactly one approver and that approver is the maker, so
    after the contributor filter there is nobody left. The one outcome that
    must never occur is "nobody has to approve this" being read as "this is
    approved".
    """
    estate.seed_definition(approvers=[(rules.APPROVER_USER, "U-MC-MAKER")])
    revision = estate.seed_revision(created_by="U-MC-MAKER")

    # The `exc.code` assertion now lives inside `route_failing_closed`, read
    # from `approval_action.outcome` -- the row an administrator actually
    # triages, rather than an exception object that never reached a table.
    estate.route_failing_closed(rules.ERR_NO_INDEPENDENT_APPROVER, revision,
                                maker="U-MC-MAKER")

    instance_id = estate.instance_id_for(revision)
    assert estate.instance_status(instance_id) == rules.INST_EXCEPTION_PENDING, (
        f"a stage whose only approver is the maker resolved to "
        f"{estate.instance_status(instance_id)!r}. Contract 2 admits no route "
        f"to auto-approval.")
    estate.assert_not_approved(instance_id)


@pytest.mark.pg
@PG
def test_no_matching_rule_is_exception_pending_never_an_approval(estate):
    """Contract 2: no rule match fails closed.

    An object nothing routes is a configuration defect. Treating it as approved
    would make "we have no approval workflow for this" the fastest way to get
    something through.

    No definition is seeded at all here, so nothing is eligible -- the same
    outcome as an eligible definition whose every rule evaluates false, and the
    one this estate can state without inventing a predicate that must be kept
    false forever.
    """
    revision = estate.seed_revision(created_by="U-MC-MAKER")

    estate.route_failing_closed(rules.ERR_ROUTE_UNRESOLVED, revision,
                                maker="U-MC-MAKER")

    instance_id = estate.instance_id_for(revision)
    assert estate.instance_status(instance_id) == rules.INST_EXCEPTION_PENDING, (
        f"an unroutable object resolved to "
        f"{estate.instance_status(instance_id)!r}. Contract 2: no match -> "
        f"EXCEPTION_PENDING, never an approval and never a silent pass.")
    estate.assert_not_approved(instance_id)


@pytest.mark.pg
@PG
def test_an_independent_assigned_approver_is_not_blocked_by_the_engine(estate):
    """The control must refuse contributors, not everybody.

    Every refusal above would also be produced by an engine that refused every
    decision. This is the case that separates a working control from a broken
    one, and it asserts the LEDGER: a decision that returns a result while the
    instance stays OPEN is exactly the defect this module exists to catch.
    """
    estate.seed_definition(approvers=[(rules.APPROVER_ROLE, APPROVER_ROLE)])
    revision = estate.seed_revision(created_by="U-MC-MAKER")
    instance_id = estate.route(revision, maker="U-MC-MAKER")["instance_id"]

    result = estate.decide(instance_id, actor="U-MC-APPROVER",
                           reason_text="independent approval")

    assert result.instance_status == rules.INST_APPROVED, (
        f"an independent, assigned approver produced {result.instance_status!r}")
    assert estate.instance_status(instance_id) == rules.INST_APPROVED, (
        f"the decision reported APPROVED but the instance is "
        f"{estate.instance_status(instance_id)!r}. The ledger did not move.")

    recorded = estate.approve_actions(instance_id)
    assert len(recorded) == 1, f"expected exactly one APPROVE action: {recorded}"
    assert recorded[0][1] == "U-MC-APPROVER", (
        f"the approval is attributed to {recorded[0][1]!r}, not to the "
        f"approver who took it")
    assert recorded[0][2] is None, (
        f"a direct decision recorded acting_for_user_id={recorded[0][2]!r}; "
        f"nobody was acting for anybody here")


# ==========================================================================
# A reported gap, now CLOSED
# ==========================================================================
#
# test_the_fail_closed_paths_attribute_their_action_to_an_unprovisioned_principal
# stood here and is deleted on its own instruction.
#
# It flagged that the engine attributes fail-closed actions to a literal
# "SYSTEM" while no migration or seed provisioned such a principal -- so
# NO_INDEPENDENT_APPROVER and the every-stage-skipped branch would have hit a
# foreign-key violation instead of the contract's code, and the
# EXCEPTION_PENDING row would have died with the aborted transaction.
#
# Its assertion message named the condition for its own retirement: "a SYSTEM
# principal now appears ... If it is an app_user row, this finding is CLOSED:
# delete this test." migrations/pg/seed_parts/008_approvals.sql now seeds it
# as a real SERVICE row holding no role -- attributable, permitted nothing,
# able to act through no API.
#
# The contract it protected is still proved, by
# test_an_empty_approver_set_is_exception_pending_never_an_approval, which
# asserts the error code directly rather than inferring it from an absence.
