#!/usr/bin/env python
"""Role-based UAT for the CAPEX & WBS Control Hub, executed against a RUNNING server.

    python tests/uat/uat_role_matrix.py --base http://127.0.0.1:8871

Exit code is the result: 0 all scenarios passed, 1 one or more failed, 2 the
harness could not run at all. Nothing prints "PASSED" without the exit code
agreeing -- `tools/run_vrt.mjs` exists because that divergence has happened
here before, and this file must not reintroduce it in a different language.


WHY THIS IS NOT A pytest FILE
=============================
`tools/build_test_manifest.py` inventories ``TESTS.rglob("test_*.py")`` and
pins a baseline of exactly 220 test functions; a file named ``test_*.py``
anywhere under ``tests/`` inflates that baseline and breaks the removal guard
until it is registered in that tool's ``POST_BASELINE_FILES``. That tool is
owned by another stream. So this harness is deliberately named ``uat_*``:
pytest does not collect it, ``rglob("test_*.py")`` does not see it, and the
220 baseline is untouched. It is run on purpose, by a person or a pipeline,
and it reports its own exit code.


WHAT THIS ASSERTS, AND WHY THE NEGATIVES ARE THE POINT
======================================================
A UAT script that only walks happy paths proves the least interesting thing.
For every permission below this harness asserts BOTH directions:

  * every seeded identity that HOLDS the permission is not refused, and
  * every seeded identity that LACKS it IS refused, with 403.

The expectation is not a copy of the permission table -- it is READ from
``app.backend.auth.PERMISSIONS`` and ``auth.DEV_USERS`` at run time. A change
to the role model therefore fails this harness rather than sliding past a
hand-maintained duplicate that nobody re-checked.

THE 403 / 503 DISTINCTION IS LOAD-BEARING
-----------------------------------------
Most business surfaces are PostgreSQL-backed, and this POC is routinely run
without PostgreSQL configured, where they answer
``503 DATABASE_NOT_CONFIGURED``. That is not a problem for a permission test:
FastAPI resolves the permission dependency BEFORE the handler touches a
database, so

    403  -> the permission gate REFUSED this identity
    503  -> the permission gate ADMITTED this identity, and the request then
            died on the absent database

which is precisely the discrimination a negative authorisation case needs.
A negative case that could not tell "you may not" from "there is nothing
here" would pass against a server with no database at all, which is the
failure mode this file is built to avoid.

Run it with PostgreSQL configured as well: the authorisation assertions are
identical, and the business-flow scenarios then execute instead of stopping
at the database boundary. `--require-postgres` makes that a hard requirement
so a pipeline cannot quietly grade the shallower run.


ZOHO IS MOCK
------------
`app.backend.zoho.MODE` is `"MOCK"`. No live Zoho call is made by this
harness, none has ever been made by this build, and no sandbox credential
exists. Scenario UAT-INT-01 asserts the server itself still says so, so that
a future build that flipped to live without telling anyone fails here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.backend import auth  # noqa: E402  (after sys.path fix)

# --------------------------------------------------------------------------
# The seven business roles of the UAT charter, mapped to the seeded identity
# that ACTUALLY holds the permissions -- read from auth.DEV_USERS, never
# assumed from the name. `U-PLH` and `U-PROC` carry identical grants
# (ProcurementApprover), which is what makes the maker-checker scenarios
# possible without inventing a user.
# --------------------------------------------------------------------------
BUSINESS_ROLES = {
    "Requestor":          "U-REQ",   # ['Requestor']
    "Approver":           "U-PROC",  # ['ProcurementApprover']  - approves PRs
    "Procurement":        "U-PLH",   # ['ProcurementApprover']  - PO amend/cancel/close
    "Finance":            "U-FIN",   # ['FinanceApprover']
    "Project Controller": "U-PM",    # ['Requestor','BudgetController']
    "Administrator":      "U-ADM",   # ['Administrator']
    "Auditor":            "U-AUD",   # ['Auditor']
}
# The two remaining seeded identities are exercised too: they are the
# multi-role principals, and a role model is easiest to get wrong there.
EXTRA_IDENTITIES = ["U-PFC", "U-CFO"]

ALL_USERS = [u for u, _ in auth.DEV_USERS]


def password_for(user_id: str) -> str:
    """The seeded development credential. `provision_dev_identities` hashes
    `user_id + '!demo'`; these are development identities in a demo profile and
    are the only credentials that may appear in any document for this build."""
    return user_id + "!demo"


# --------------------------------------------------------------------------
# Scenarios. Each names the permission it exercises and ONE representative
# call. `expect_allowed_is_2xx` marks the few routes that really do serve a
# result without PostgreSQL, so an allowed caller must get a non-403 there too
# -- everywhere else "not 403" is the assertion, because 503 is the honest
# answer of an unconfigured process and 404 is the honest answer of a route
# whose object id does not exist in the demo set.
# --------------------------------------------------------------------------
Scenario = dict

# Object identifiers taken from the SEEDED demo dataset, which is deterministic
# (see docs/RELEASE_PACKAGE.md): the same `--fresh --seed` produces these same
# ids every time. Using real ids rather than invented ones is what makes a
# negative case mean something -- a refusal on an id that does not exist would
# be indistinguishable from a 404, and on several routes it IS a 404.
PR_WITHIN = "PR-015"      # Submitted, WITHIN_BUDGET, raised by U-REQ
PR_EXCEPTION = "PR-010"   # Exception Pending, EXCEEDS_BUDGET, raised by U-REQ
PO_COMMITTED = "PO-004"   # Fully Committed
BILL_APPROVED = "BILL-001"
CAP_SUBMITTED = "CAP-001"
REV_SUBMITTED = "REV-003"
PROJECT = "PRJ-01"
ENTITY = "ENT-DM1"


def S(sid, flow, permission, method, path, body=None, note="", maker=None,
      last=False) -> Scenario:
    """One scenario.

    `permission` is either a permission name or a tuple of names, in which case
    the route admits a caller holding ANY of them -- `/api/procurement/
    purchase-requests/{id}/approve` really is declared that way, because
    `services.approve_pr` picks between `pr.approve` and `pr.approve_exception`
    from the request's own `check_result` and the two are held by different
    roles.

    `maker` names the identity that RAISED the object this scenario acts on.
    That identity is expected to be refused by maker-checker even though it may
    hold the permission -- which is the whole point of segregation of duties and
    is asserted here rather than assumed.

    `last` defers the scenario until every other assertion has been made.
    `/api/admin/reset` rebuilds the demo database and revokes every session
    along with it, so running it in sequence would 401 everything after it.
    """
    return {"id": sid, "flow": flow, "permission": permission, "method": method,
            "path": path, "body": body, "note": note, "maker": maker, "last": last}


SCENARIOS: list[Scenario] = [
    # ---------------------------------------------- budget planning & revisions
    S("UAT-BUD-01", "Budget planning", "budget.read", "GET", "/api/budget/cells",
      note="Every seeded role holds budget.read (it is declared as the whole ROLES "
           "tuple), so this scenario has NO negative case by construction. Recorded "
           "as such rather than left looking like a passing negative."),
    S("UAT-BUD-02", "Budget planning", "budget.read", "GET", "/api/budget/versions"),
    S("UAT-BUD-03", "Budget revision", "revision.create", "POST", "/api/budget/revisions",
      {"entity_id": ENTITY, "reason": "UAT revision"}),
    S("UAT-BUD-04", "Budget revision", "revision.approve", "POST",
      f"/api/budget/revisions/{REV_SUBMITTED}/approve", {},
      note="FinanceApprover only. The Administrator is deliberately NOT an approver "
           "of money movements."),
    S("UAT-BUD-05", "Period control", "period.transition", "POST",
      "/api/budget/periods/PD-UAT/transition", {"to_state": "CLOSED"},
      note="Closing a period freezes what may still be posted into it, so it is a "
           "finance control. Auditor and Administrator are both refused."),

    # ---------------------------------------------- PR reservation and approval
    S("UAT-PR-01", "PR raise", "pr.create", "POST", "/api/procurement/purchase-requests",
      {"entity_id": ENTITY},
      note="Requestor and BudgetController only. The Administrator cannot raise a PR."),
    S("UAT-PR-02", "PR approve", ("pr.approve", "pr.approve_exception"), "POST",
      f"/api/procurement/purchase-requests/{PR_WITHIN}/approve", {"decision": "APPROVE"},
      note="This route admits EITHER permission, because approve_pr picks between "
           "them from the request's own check_result and the two are held by "
           "different roles. Requestor, Auditor and Administrator hold neither and "
           "are refused."),
    S("UAT-PR-03", "PR approve (within budget)", "pr.approve", "POST",
      f"/api/purchase-requests/{PR_WITHIN}/approve", {"reason": "UAT"},
      maker="U-REQ",
      note="The legacy SQLite route on a WITHIN_BUDGET request: the permission "
           "resolves to pr.approve, so ProcurementApprover alone may act -- and "
           "U-REQ, who raised it, is refused by maker-checker despite the route "
           "having no router-level permission dependency at all."),
    S("UAT-PR-04", "PR exception approve", "pr.approve_exception", "POST",
      f"/api/purchase-requests/{PR_EXCEPTION}/approve", {"reason": "UAT exception"},
      maker="U-REQ",
      note="THE SAME ROUTE, a DIFFERENT REQUEST, and a different answer: PR-010 "
           "exceeds budget, so the required permission becomes pr.approve_exception "
           "and the ProcurementApprover who could approve PR-015 is refused here. "
           "An over-budget request routes to Finance rather than being blocked."),

    # ---------------------------------------------- PR -> PO, and PO lifecycle
    S("UAT-PO-01", "PR to PO", "po.amend", "POST",
      f"/api/purchase-orders/{PO_COMMITTED}/amend", {"reason": "UAT"}),
    S("UAT-PO-02", "PO cancel", "po.cancel", "POST",
      f"/api/purchase-orders/{PO_COMMITTED}/cancel", {"reason": "UAT"},
      note="Cancelling releases commitment back to available budget."),
    S("UAT-PO-03", "PO close", "po.close", "POST",
      f"/api/purchase-orders/{PO_COMMITTED}/close", {"reason": "UAT"}),

    # ---------------------------------------------- GRN / bill inbound (mock)
    S("UAT-GRN-01", "GRN inbound", "budget.read", "GET", "/api/grns",
      note="SQLite ledger surface; serves the seeded receipts with no PostgreSQL."),
    S("UAT-BIL-01", "Bill inbound", "budget.read", "GET", "/api/bills"),
    S("UAT-BIL-02", "Bill void", "bill.void", "POST",
      f"/api/bills/{BILL_APPROVED}/void", {"reason": "UAT"},
      note="FinanceApprover only -- voiding an actual reverses CWIP."),

    # ---------------------------------------------- reconciliation & retry
    S("UAT-REC-01", "Reconciliation", "budget.read", "GET", "/api/reconciliation"),
    S("UAT-REC-02", "Reconciliation", "connector.read", "GET",
      "/api/integrations/connections"),
    S("UAT-REC-03", "Exception triage", "reconciliation.triage", "GET",
      "/api/integrations/exceptions/unattributed",
      note="ADMINISTRATOR ONLY -- and notably NOT the Auditor. Triage is an "
           "operational act over other entities' discrepancies, not an audit read."),
    S("UAT-REC-04", "Retry a dead letter", "connector.manage", "POST",
      "/api/integrations/dead-letters/outbox/1/retry", {}),

    # ---------------------------------------------- reporting and export
    S("UAT-REP-01", "Reporting", "budget.read", "GET", "/api/reports/metrics"),
    S("UAT-REP-02", "Saved view (PRIVATE)", "budget.read", "POST", "/api/reports/views",
      {"entity_id": ENTITY, "report_key": "budget", "name": "UAT private",
       "visibility": "PRIVATE"},
      note="A PRIVATE saved view is a bookmark under the caller's own identity and "
           "sits at the router floor, which every role holds."),
    S("UAT-REP-03", "Saved view (SHARED)", "report.view.share", "POST",
      "/api/reports/views",
      {"entity_id": ENTITY, "report_key": "budget", "name": "UAT shared",
       "visibility": "SHARED"},
      note="THE PAIR THAT MATTERS. Same route, same body but one field, and the "
           "authorisation answer changes: publishing a view into an entity is a "
           "write that affects other principals, so Requestor, Procurement and "
           "Auditor are refused here while passing UAT-REP-02."),
    S("UAT-EXP-01", "Export", "export.create", "POST", "/api/exports",
      {"dataset": "budget", "entity_id": ENTITY},
      note="Auditor is deliberately absent: creating an export job is a row INSERT. "
           "An Auditor reads through /api/audit and the report API."),

    # ---------------------------------------------- completion, capitalisation
    S("UAT-CAP-01", "Capitalisation allocate", "capitalisation.allocate", "POST",
      f"/api/closure/requests/{CAP_SUBMITTED}/allocations", {},
      note="BudgetController and FinanceApprover propose the split."),
    S("UAT-CAP-02", "Capitalisation approve", "capitalisation.approve", "POST",
      f"/api/closure/requests/{CAP_SUBMITTED}/approve", {},
      note="CapitalisationApprover ONLY -- of the nine seeded identities, only U-CFO "
           "holds it. Exclusion X-01: approval records the DECISION and posts "
           "nothing to a GL or fixed-asset register."),

    # ---------------------------------------------- closure and reopening
    S("UAT-CLO-01", "Completion review", "budget.read", "GET", "/api/closure/reviews"),
    S("UAT-CLO-02", "Closure position", "budget.read", "GET",
      f"/api/closure/projects/{PROJECT}/position"),

    # ---------------------------------------------- approvals engine
    S("UAT-APR-01", "Approval inbox", "approval.read", "GET", "/api/approvals/inbox",
      note="Auditor is EXCLUDED from approval.read; an Auditor reads approval history "
           "through the hash-chained audit trail instead."),
    S("UAT-APR-02", "Approval matrix config", "approval.configure", "GET",
      "/api/approvals/definitions",
      note="Administrator only."),
    S("UAT-APR-03", "Delegation", "approval.delegate", "GET", "/api/approvals/delegations",
      note="The four approver roles. NOT the Administrator and NOT the Requestor -- "
           "you cannot delegate an authority you do not hold."),

    # ---------------------------------------------- audit
    S("UAT-AUD-01", "Audit trail", "audit.read", "GET", "/api/audit/streams",
      note="Auditor and Administrator only."),
    S("UAT-AUD-02", "Audit entries", "audit.read", "GET", "/api/audit/entries"),

    # ---------------------------------------------- settings and master data
    S("UAT-SET-01", "Settings read", "settings.read", "GET", "/api/settings/tax"),
    S("UAT-SET-02", "Settings write", "settings.write", "POST", "/api/settings/tax", {},
      note="Administrator only."),
    S("UAT-MAS-01", "Masters read", "masters.read", "GET", "/api/masters/vendor"),
    S("UAT-MAS-02", "Masters write", "masters.write", "POST", "/api/masters/vendor", {},
      note="Administrator and BudgetController."),

    # ---------------------------------------------- destructive administration
    S("UAT-ADM-01", "Reset demo data", "admin.reset", "POST", "/api/admin/reset", {},
      last=True,
      note="Administrator AND the local-demo profile. Eight of nine identities are "
           "refused outright. Deferred to the very end: a successful reset rebuilds "
           "the demo database and revokes every open session with it, so any "
           "assertion made after it would fail on 401 rather than on its own merits."),
]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, session: str | None = None):
        req = urllib.request.Request(self.base + path, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        if session:
            req.add_header("X-Session", session)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise SystemExit(f"UAT: cannot reach {self.base}: {e.reason}")


def error_code(body: str) -> str:
    try:
        payload = json.loads(body)
    except Exception:
        return ""
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    return detail.get("code", "") if isinstance(detail, dict) else ""


# --------------------------------------------------------------------------
# Result accumulation. A failure names the scenario, the identity and what it
# expected -- a UAT report that says only "3 failed" is a report nobody can act
# on in a client meeting.
# --------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, label: str, detail: str = ""):
        (self.passed if ok else self.failed).append(
            label if ok else f"{label}: {detail}")
        return ok

    def note(self, text: str):
        self.notes.append(text)


def run(base: str, require_postgres: bool) -> int:
    client = Client(base)
    rep = Report()

    status, body = client.call("GET", "/api/health")
    if status != 200:
        print(f"UAT: /api/health answered {status}; is the server running at {base}?")
        return 2
    health = json.loads(body)

    # ------------------------------------------------ sign in, all nine
    sessions: dict[str, str] = {}
    for user in ALL_USERS:
        st, b = client.call("POST", "/api/auth/login",
                            {"user_id": user, "password": password_for(user)})
        if st != 200:
            print(f"UAT: {user} could not sign in ({st}). "
                  f"Run `python app/run.py` once so provision_dev_identities() has run.")
            return 2
        sessions[user] = json.loads(b)["session_id"]

    # UAT-AUTH-01 -- a wrong password is refused, and does not disclose whether
    # the account exists.
    st, b = client.call("POST", "/api/auth/login",
                        {"user_id": "U-REQ", "password": "wrong"})
    rep.check(st == 401 and error_code(b) == "INVALID_CREDENTIALS",
              "UAT-AUTH-01 a wrong password is refused with INVALID_CREDENTIALS",
              f"got {st} {error_code(b)}")
    st2, b2 = client.call("POST", "/api/auth/login",
                          {"user_id": "U-NOBODY", "password": "wrong"})
    rep.check(st2 == st and error_code(b2) == error_code(b),
              "UAT-AUTH-02 an unknown user and a wrong password are indistinguishable",
              f"known={st}/{error_code(b)} unknown={st2}/{error_code(b2)}")

    # UAT-AUTH-03 -- no session, no access. The caller cannot nominate an actor.
    st, b = client.call("GET", "/api/auth/me")
    rep.check(st == 401 and error_code(b) == "NOT_AUTHENTICATED",
              "UAT-AUTH-03 an unauthenticated caller is refused everywhere",
              f"got {st} {error_code(b)}")

    # UAT-AUTH-04 -- a forged session id is refused.
    st, b = client.call("GET", "/api/auth/me", session="not-a-real-session-id")
    rep.check(st == 401 and error_code(b) in ("SESSION_INVALID", "NOT_AUTHENTICATED"),
              "UAT-AUTH-04 a forged session identifier is refused",
              f"got {st} {error_code(b)}")

    # UAT-AUTH-05 -- a signed-out session stops working immediately.
    st, b = client.call("POST", "/api/auth/login",
                        {"user_id": "U-REQ", "password": password_for("U-REQ")})
    throwaway = json.loads(b)["session_id"]
    client.call("POST", "/api/auth/logout", {}, session=throwaway)
    st, b = client.call("GET", "/api/auth/me", session=throwaway)
    rep.check(st == 401,
              "UAT-AUTH-05 a session stops working the moment it is signed out",
              f"got {st} after logout")

    # UAT-AUTH-06 -- the acting identity is server-derived. Naming somebody else
    # in the body changes nothing.
    st, b = client.call("GET", "/api/auth/me", session=sessions["U-REQ"])
    who = json.loads(b) if st == 200 else {}
    rep.check(who.get("user_id") == "U-REQ",
              "UAT-AUTH-06 the acting identity comes from the session, not the request",
              f"got {who.get('user_id')!r}")

    # ------------------------------------------------ Zoho is MOCK
    st, b = client.call("GET", "/api/zoho/connections", session=sessions["U-ADM"])
    blob = b.upper()
    rep.check(st == 200 and "MOCK" in blob and "LIVE" not in blob.replace("LIVE_", ""),
              "UAT-INT-01 the connector reports MOCK mode and never claims LIVE",
              f"status {st}; body did not read as MOCK-only")
    rep.note("Zoho: MODE is MOCK. No sandbox credential exists in this build and no "
             "live call has ever been made. Every connector response below is a "
             "synthesised representative payload built from the verified endpoint "
             "inventory.")

    # ------------------------------------------------ the permission matrix
    postgres_seen = False

    def run_scenario(sc: Scenario) -> None:
        nonlocal postgres_seen
        names = sc["permission"] if isinstance(sc["permission"], tuple) \
            else (sc["permission"],)
        holders: set[str] = set()
        for name in names:
            allowed = auth.PERMISSIONS.get(name)
            if allowed is None:
                rep.check(False, f'{sc["id"]} unknown permission {name!r}',
                          "not present in auth.PERMISSIONS")
                return
            holders |= set(allowed)
        shown = " or ".join(names)

        # NEGATIVE CASES RUN FIRST, AND THAT ORDERING IS DELIBERATE.
        #
        # Several scenarios act on a real seeded row, and an entitled identity
        # acting first MOVES that row: PR-015 becomes Approved, after which the
        # next caller meets `INVALID_TRANSITION` instead of the permission gate.
        # `services.approve_pr` checks existence and status BEFORE it calls
        # `auth.require` (app/backend/services.py:178-186), so a state change
        # made by the positive half of this scenario would mask the negative
        # half entirely -- the refusals would still "pass" as non-200 while
        # proving nothing about authorisation. Refusing first, on a row that is
        # still in an actionable state, is what keeps the negative meaningful.
        ordered = sorted(auth.DEV_USERS, key=lambda ur: bool(set(ur[1]) & holders))

        for user, roles in ordered:
            entitled = bool(set(roles) & holders)
            st, body = client.call(sc["method"], sc["path"], sc["body"],
                                   session=sessions[user])
            code = error_code(body)
            if st in (200, 201) and sc["path"].startswith(
                    ("/api/budget/", "/api/approvals/", "/api/reports/",
                     "/api/closure/", "/api/masters/", "/api/settings/")):
                postgres_seen = True

            if entitled and user == sc["maker"]:
                # SEGREGATION OF DUTIES, live. An identity that holds the
                # permission AND raised the object must still be refused, and
                # refused with the code that says why -- SELF_APPROVAL, not a
                # plain FORBIDDEN wearing the same status line.
                #
                # No seeded identity currently reaches this branch. See
                # UAT-SOD-04: `pr.create` and `pr.approve` are held by DISJOINT
                # role sets, as are every other create/approve pair, so a maker
                # never holds the matching approval permission and `auth.require`
                # refuses them one line before `require_separation` would. The
                # branch is kept because the day a client's role mapping grants
                # one principal both, this is the assertion that must hold.
                rep.check(
                    st == 403 and code == "SELF_APPROVAL",
                    f'{sc["id"]} {shown} REFUSES the maker {user} (self-approval)',
                    f"expected 403 SELF_APPROVAL, got {st} {code or ''}".strip())
            elif entitled:
                # POSITIVE: the gate must not refuse a holder. It may still 503
                # (no database), 404 (no such object), 409 (the object already
                # moved on -- the second holder to act on the same seeded row)
                # or 422 (a body this harness did not fully populate). None of
                # those is a refusal.
                rep.check(
                    st != 403,
                    f'{sc["id"]} {shown} ALLOWS {user} ({"+".join(roles)})',
                    f"refused with 403 ({code}) although {user} holds {shown}")
            else:
                # NEGATIVE: the gate MUST refuse, and refuse with 403 -- not with
                # a 503 that would have been the same answer for everyone, and
                # not with a 404 that would mean the object was read first.
                rep.check(
                    st == 403,
                    f'{sc["id"]} {shown} REFUSES {user} ({"+".join(roles)})',
                    f"expected 403, got {st} {code or ''}".strip())

    for sc in SCENARIOS:
        if not sc["last"]:
            run_scenario(sc)

    # ------------------------------------------------ segregation of duties
    # UAT-SOD-01. Maker-checker is not a permission; it is decided per instance
    # inside the transaction. U-PLH and U-PROC hold IDENTICAL grants, so an
    # instance either of them raised must still be refusable to its own maker
    # while remaining approvable by the other. Without a database there is no
    # instance to act on, so this is asserted structurally against the source
    # of truth rather than pretended.
    rep.check(
        auth.MAKER_CHECKER == {"pr.approve", "pr.approve_exception", "revision.approve",
                               "capitalisation.approve", "bill.void"},
        "UAT-SOD-01 the maker-checker set still covers all five approval permissions",
        f"got {sorted(auth.MAKER_CHECKER)}")
    for permission in sorted(auth.MAKER_CHECKER):
        principal = {"user_id": "U-PROC", "roles": ["ProcurementApprover"]}
        try:
            auth.require_separation(principal, permission, "U-PROC")
            raised = False
        except auth.AuthError as exc:
            raised = exc.code == "SELF_APPROVAL"
        rep.check(raised,
                  f"UAT-SOD-02 {permission}: the raiser of an item cannot approve it",
                  "require_separation did not raise SELF_APPROVAL")
        try:
            auth.require_separation(principal, permission, "U-PLH")
            independent_ok = True
        except auth.AuthError:
            independent_ok = False
        rep.check(independent_ok,
                  f"UAT-SOD-03 {permission}: an independent approver is not blocked",
                  "require_separation refused a different user")

    # UAT-SOD-04. THE FINDING THIS SECTION EXISTS TO SURFACE.
    #
    # Maker-checker is a genuine second line of defence, and it is unreachable
    # by any seeded identity -- because the role model already separates the
    # duties one layer earlier. `pr.create` is held by Requestor and
    # BudgetController; `pr.approve` by ProcurementApprover alone. The sets are
    # DISJOINT, so nobody who can raise a purchase request can also hold the
    # permission to approve one, and `auth.require` refuses them before
    # `require_separation` is ever consulted. The same is true of every other
    # create/approve pair below.
    #
    # This is asserted, not merely noted, because it cuts both ways. It is
    # reassuring today: segregation does not depend on the second check. It is
    # also fragile: a client role mapping that grants one principal both
    # permissions (D-12 is still open) moves the entire weight of segregation
    # onto `require_separation`, which is exercised only by UAT-SOD-02 above and
    # by no live path at all. If this assertion ever fails, the live
    # maker-checker path has become reachable and must be tested end to end.
    PAIRS = [("pr.create", "pr.approve"),
             ("pr.create", "pr.approve_exception"),
             ("revision.create", "revision.approve"),
             ("capitalisation.allocate", "capitalisation.approve")]
    for maker_perm, checker_perm in PAIRS:
        both = set(auth.PERMISSIONS[maker_perm]) & set(auth.PERMISSIONS[checker_perm])
        rep.check(
            not both,
            f"UAT-SOD-04 no role holds both {maker_perm} and {checker_perm}",
            f"{sorted(both)} hold both, so maker-checker is now the ONLY thing "
            f"separating these duties and needs a live end-to-end test")
    rep.note("Maker-checker (auth.require_separation) is asserted structurally, not "
             "live: no seeded identity holds both a create and its matching approve "
             "permission, so the SELF_APPROVAL path cannot be reached by any demo "
             "user. UAT-SOD-04 is the assertion that will fail if that changes.")

    # ------------------------------------------------ duplicates / outages
    # UAT-DUP-01. Two identical logins must produce two DISTINCT sessions; a
    # session identifier that repeated would be a replayable credential.
    st1, b1 = client.call("POST", "/api/auth/login",
                          {"user_id": "U-REQ", "password": password_for("U-REQ")})
    st2, b2 = client.call("POST", "/api/auth/login",
                          {"user_id": "U-REQ", "password": password_for("U-REQ")})
    rep.check(json.loads(b1)["session_id"] != json.loads(b2)["session_id"],
              "UAT-DUP-01 repeating an identical login yields a distinct session",
              "two logins returned the same session identifier")

    # UAT-OUT-01. With PostgreSQL absent, a PostgreSQL-backed surface must fail
    # LOUDLY and specifically -- never with an empty list that a demonstrator
    # would read out as "no exceptions found".
    st, body = client.call("GET", "/api/reports/metrics", session=sessions["U-ADM"])
    if st == 503:
        payload = json.loads(body).get("detail", {})
        rep.check(payload.get("code") == "DATABASE_NOT_CONFIGURED"
                  and payload.get("state") == "unavailable",
                  "UAT-OUT-01 an absent database is reported as unavailable, not as empty",
                  f"got {payload}")
        rep.note("PostgreSQL is NOT configured for this run: the PostgreSQL-backed "
                 "surfaces answered 503 DATABASE_NOT_CONFIGURED and the business "
                 "flow scenarios above were asserted to the authorisation boundary "
                 "only. Re-run with CAPEX_DB_URL set to exercise them end to end.")
    elif st == 200:
        postgres_seen = True
        rep.check(True, "UAT-OUT-01 reporting served a result (PostgreSQL configured)")
    else:
        rep.check(False, "UAT-OUT-01 reporting answered neither 200 nor 503",
                  f"got {st}")

    # ------------------------------------------------ deferred: destructive admin
    # Runs only now, because a successful reset revokes every open session.
    for sc in SCENARIOS:
        if sc["last"]:
            run_scenario(sc)
            rep.note(f'{sc["id"]} ran last and rebuilt the demo database; every '
                     "session opened by this run is now revoked.")

    # UAT-ADM-02. The reset must actually invalidate the sessions it destroyed --
    # a reset that left a live session pointing at rebuilt data would be worse
    # than one that refused.
    st, _ = client.call("GET", "/api/auth/me", session=sessions["U-ADM"])
    rep.check(st == 401,
              "UAT-ADM-02 a demo reset revokes the sessions that survived it",
              f"a pre-reset session still answered {st}")

    if require_postgres and not postgres_seen:
        rep.check(False, "UAT-ENV-01 --require-postgres was passed",
                  "no PostgreSQL-backed surface served a result; this run graded the "
                  "authorisation contract only")

    # ------------------------------------------------ report
    print("=" * 78)
    print("ROLE-BASED UAT -- CAPEX & WBS Control Hub")
    print("=" * 78)
    print(f"  server        : {base}")
    print(f"  profile       : {health.get('profile', '?')}")
    print(f"  postgres      : {'configured' if postgres_seen else 'NOT configured'}")
    print(f"  zoho          : MOCK (no live call, no sandbox credential)")
    print(f"  identities    : {len(sessions)} seeded")
    print(f"  scenarios     : {len(SCENARIOS)} permission scenarios x "
          f"{len(ALL_USERS)} identities, plus {6} authentication and "
          f"{len(auth.MAKER_CHECKER) * 2 + 1} segregation checks")
    print()
    for text in rep.notes:
        print(f"  NOTE  {text}")
    print()
    if rep.failed:
        print(f"  FAILED {len(rep.failed)} of {len(rep.failed) + len(rep.passed)}:")
        for line in rep.failed:
            print(f"    x {line}")
    else:
        print(f"  All {len(rep.passed)} assertions passed.")
    print("=" * 78)
    return 1 if rep.failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("UAT_BASE", "http://127.0.0.1:8000"),
                    help="base URL of a RUNNING server (default %(default)s)")
    ap.add_argument("--require-postgres", action="store_true",
                    help="fail unless a PostgreSQL-backed surface actually served a "
                         "result, so a pipeline cannot silently grade the shallower run")
    args = ap.parse_args(argv)
    return run(args.base, args.require_postgres)


if __name__ == "__main__":
    raise SystemExit(main())
