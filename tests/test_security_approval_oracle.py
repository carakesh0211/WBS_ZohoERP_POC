"""A refusal must not answer the question it is refusing.

`services.approve_pr` read the purchase request and checked its status BEFORE
it checked whether the caller could approve anything. A principal holding
neither `pr.approve` nor `pr.approve_exception` therefore learned two things it
was never entitled to:

    an unknown id      -> 404 PR_NOT_FOUND
    a non-approvable   -> 409 INVALID_TRANSITION, naming the pr_number
    request               and the status

That is an existence-and-state oracle on a financial document, reachable by any
authenticated role. An Auditor -- whose whole definition is read-only -- could
enumerate purchase request ids by watching which ones answered 404.

WHY IT WAS WRITTEN THAT WAY, because the fix has to respect it. Which
permission `approve_pr` requires DEPENDS ON THE ROW: `pr.approve` for a
within-budget request, `pr.approve_exception` for one that exceeded. The
specific requirement genuinely cannot be evaluated before the read.

So the repair is a COARSE gate in front -- hold at least one of the two, or
learn nothing -- with the row-dependent check left exactly where it was. Both
halves are pinned below, because either alone is satisfied by a broken
implementation: the oracle tests would pass if the coarse gate refused
EVERYONE, and the capability tests would pass if it were removed altogether.
"""
import pytest

from app.backend import auth, services


def _actor(*roles):
    return {"user_id": "U-PROBE", "roles": list(roles)}


@pytest.fixture()
def a_pr(raw_con):
    """A real, freshly raised purchase request, in `Draft`-equivalent state.

    Ids are read from the seeded estate rather than hard-coded, so this does
    not break the day the demo seed changes shape.
    """
    row = raw_con.execute(
        "SELECT w.wbs_id, w.project_id, b.budget_head_id"
        "  FROM wbs_element w"
        "  JOIN budget_line b ON b.wbs_id = w.wbs_id"
        " LIMIT 1").fetchone()
    if row is None:
        pytest.skip("the seeded estate has no WBS element carrying a budget line")
    result = services.create_pr(
        raw_con, _actor("Requestor"),
        project_id=row["project_id"], wbs_id=row["wbs_id"],
        budget_head_id=row["budget_head_id"],
        description="oracle probe", amount="1.00")
    return result["pr_id"]


def _refusal(con, actor, pr_id):
    """Whatever `approve_pr` raises, normalised for comparison."""
    with pytest.raises(Exception) as excinfo:
        services.approve_pr(con, actor, pr_id, reason=None)
    exc = excinfo.value
    return (getattr(exc, "status", None), getattr(exc, "code", None),
            str(getattr(exc, "message", exc)))


# =========================================================================
# The oracle is closed.
# =========================================================================

def test_a_caller_with_no_approval_permission_is_refused_before_the_row_is_read(
        raw_con):
    """An id that does not exist must answer 403, not 404.

    404 and 403 are different disclosures. 404 says "this id is not one of
    ours", which is an answer. 403 says "you may not ask", which is not.
    """
    status, code, _ = _refusal(raw_con, _actor("Auditor"), "PR-DOES-NOT-EXIST")
    assert status == 403, (
        f"an unauthorised caller learned something about an id: {status} {code}")


def test_a_caller_with_no_approval_permission_learns_nothing_about_a_real_pr(
        raw_con, a_pr):
    """A real id must answer exactly as an invented one does.

    HONEST LIMIT, measured rather than assumed: removing the coarse gate does
    NOT fail this test. `create_pr` leaves the request in an approvable state,
    so `approve_pr` never reaches the 409 branch and refuses at `auth.require`
    either way. The two tests that DO fail without the gate are the unknown-id
    one above and the indistinguishability one below; this one guards the
    weaker property that the refusal never quotes the document.

    The 409 disclosure it was written for -- naming the pr_number and status to
    a caller who could approve nothing -- needs a request in a non-approvable
    state, which this fixture does not produce. Left in with its limit stated
    rather than dressed up as coverage it does not provide.
    """
    status, _, message = _refusal(raw_con, _actor("Auditor"), a_pr)
    assert status == 403
    assert a_pr not in message, (
        f"the refusal quoted the request it was refusing to discuss: {message}")


def test_the_refusal_does_not_distinguish_a_real_id_from_an_invented_one(
        raw_con, a_pr):
    """The two refusals must be INDISTINGUISHABLE.

    Two 403s carrying different codes or different text are still an oracle,
    just a quieter one.
    """
    real = _refusal(raw_con, _actor("Auditor"), a_pr)
    invented = _refusal(raw_con, _actor("Auditor"), "PR-DOES-NOT-EXIST")
    assert real == invented, (
        f"the refusal for a real id differs from the one for an invented id, "
        f"which is the same oracle in a quieter form:\n  real:     {real}\n"
        f"  invented: {invented}")


# =========================================================================
# ...and the coarse gate did not become a blanket refusal.
# =========================================================================

def test_an_approver_still_reaches_the_not_found_answer(raw_con):
    """A caller who MAY approve is entitled to know the id is unknown.

    Without this, a coarse gate that refused everyone would satisfy all three
    tests above while breaking the feature outright.
    """
    status, code, _ = _refusal(raw_con, _actor("ProcurementApprover"),
                               "PR-DOES-NOT-EXIST")
    assert code == "PR_NOT_FOUND", (
        f"an entitled approver was denied the ordinary 404: {status} {code}")


def test_actor_holds_agrees_with_require_for_every_permission_and_role(raw_con):
    """The predicate and the enforcement point must not drift.

    `actor_holds` exists because `auth.require` raises rather than returning a
    bool, and two implementations of "does this actor hold this permission" is
    two things to keep in step. It reads `auth.PERMISSIONS` through the same
    role intersection; this asserts they agree across every permission and a
    spread of role sets, including the empty one.
    """
    for permission in auth.PERMISSIONS:
        for roles in ((), ("Auditor",), ("ProcurementApprover",),
                      ("FinanceApprover",), ("Administrator",),
                      ("ProcurementApprover", "FinanceApprover")):
            actor = _actor(*roles)
            held = services.actor_holds(actor, permission)
            try:
                auth.require(actor, permission)
                required_ok = True
            except auth.AuthError:
                required_ok = False
            assert held == required_ok, (
                f"actor_holds and auth.require disagree for {permission} "
                f"with roles {roles}: {held} vs {required_ok}")
