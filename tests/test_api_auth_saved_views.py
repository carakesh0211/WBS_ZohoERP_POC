"""The five ungated saved-view routes, and the one write that is not ungated.

Split out of `tests/test_api_auth.py` rather than written there, and not for
tidiness: that file is inside the 220-function baseline the test manifest holds
as a FIXED reference (`tools/build_test_manifest.py::POST_BASELINE_FILES`).
Adding two functions to it moved the count to 222, which the baseline guard
caught. Raising the number would have been the easy reading of that failure and
the wrong one -- the 220 is not a score, it is the point of comparison.

What is asserted here is the coverage `test_api_auth.py`'s own comment used to
CLAIM existed in `tests/test_pg_reporting.py`. It did not: `grep` finds nothing
there, and being PostgreSQL-only that file skips on every machine without a
server, so the claim could sit unexamined indefinitely. These run everywhere.
"""
from test_api_auth import MUTATING_ROUTES  # noqa: F401  (the matrix under test)


def test_a_caller_with_no_role_cannot_write_a_saved_view_either(make_user):
    """The five `None`-permission rows, asserted rather than assumed.

    `test_aud_c_006_a_caller_with_no_role_at_all_can_mutate_nothing` skips
    every row whose permission is `None`, which is exactly these five. The
    comment above MUTATING_ROUTES used to say another file covered them. It
    did not, so this does.
    """
    caller = make_user([])
    # `/api/auth/logout` is also a `None` row, and correctly so -- signing out
    # needs no permission. These five are the saved-view writes.
    rows = [r for r in MUTATING_ROUTES
            if r[4] is None and r[0].startswith("/api/reports/")]
    assert len(rows) == 5, (
        f"expected five ungated /api/reports/ rows, found {len(rows)}: "
        f"{[r[0] for r in rows]}")
    for _template, method, url, body, _permission, _denied in rows:
        resp = caller.request(method, url, json=body)
        assert resp.status_code == 403, (
            f"a caller with no role reached {method} {url} -> "
            f"{resp.status_code}")


def test_sharing_a_saved_view_needs_more_than_the_router_floor(make_user):
    """PRIVATE stays at the floor; SHARED does not.

    An Auditor holds `budget.read` like every role, so it reaches the reports
    router. Saving its own private view is correct and must keep working. But
    `visibility='SHARED'` publishes the view into the entity, where other
    people open it and read money through its filters -- so it is refused.

    The two assertions must differ, or the test proves nothing: a 503 means
    authorisation PASSED and the request died later at the database, which is
    the honest state on a machine with no PostgreSQL.
    """
    auditor = make_user(["Auditor"])
    body = {"entity_id": "ENT-01", "report_key": "executive_dashboard",
            "name": "an auditor's own bookmark", "definition": {}}

    private = auditor.post("/api/reports/views", json={**body, "visibility": "PRIVATE"})
    assert private.status_code != 403, (
        "an Auditor was refused its OWN private saved view; the floor is the "
        f"right gate for PRIVATE and this is now over-restricted: {private.text}")

    shared = auditor.post("/api/reports/views", json={**body, "visibility": "SHARED"})
    assert shared.status_code == 403, (
        "an Auditor published a SHARED view into the entity: "
        f"{shared.status_code} {shared.text}")
