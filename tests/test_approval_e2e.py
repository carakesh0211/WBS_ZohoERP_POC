"""The M4b demo scenario, end to end, against the seeded PostgreSQL estate.

Wave 4 / M4b, stream 5, RECONCILED against what shipped. Extends the pattern of
``tests/test_pg_audit_api_e2e.py``: seeded PostgreSQL, through the migration
runner, through the pooled scoped engine, through the approval engine, through
the FastAPI routers and their guards.

The scenario, in seven steps:

  a. a Requestor submits a request that EXCEEDS available budget
  b. it routes to exception approval -- not auto-approved, not silently allowed
  c. a DIFFERENT authorised user approves it, with a reason
  d. the budget is revalidated INSIDE the approving transaction
  e. self-approval is rejected
  f. the document status and the approval timeline both update
  g. the approval stream verifies intact, with the expected entry count

**Every step asserts the LEDGER, not merely the return value.** An approval
that answers successfully while the control cell did not move is precisely the
defect this module exists to catch, and it is invisible to a test that reads
only a response body. Money is asserted in integer paise throughout; a
``Decimal`` anywhere in a money assertion is itself a failure, because the
first arithmetic mixing it with an int propagates a non-integer amount through
the financial controls.

RECONCILIATION: WHAT MOVED, AND WHY THE SCENARIO STILL HOLDS
------------------------------------------------------------
The scenario was written against the frozen contract, before the schema and
the engine existed. Amendment A1 re-targets it from purchase requests to
**budget revisions**, and every property it demonstrates survives intact:

* **There is no PostgreSQL-backed purchase-request module.** ``app/backend/api/``
  carries budget, masters, settings, audit, admin_access, approvals and health,
  and ``approvals.OBJECT_BINDINGS`` -- the closed allow-list of routable
  document types -- names ``BUDGET_REVISION`` and ``BUDGET_TRANSFER`` only. A
  purchase request cannot be routed at all. The maker therefore raises a
  budget revision through ``POST /api/budget/revisions``, which is a real
  route, guarded by a real permission, writing a real document row.
* **Nothing opens an approval instance for a document.** ``open_instance`` is
  called from nowhere outside ``pg/approvals.py`` itself, so the scenario opens
  the instance explicitly through the engine's own entry point. That is a
  reported integration gap, not a liberty this test is taking -- see the
  module report.
* **``approval_action`` has no ``detail`` column** (amendment A3). The hashed
  payload is built from stored columns only (``chain_detail``), so
  ``verify_instance_chain`` **recomputes every digest** rather than merely
  checking that ``prev_hash`` links agree -- which is the difference between a
  hash chain and a decoration. Step (g) asserts against that verifier.
* **``action_id`` is ``GENERATED ALWAYS AS IDENTITY``** and is never supplied;
  **``created_by`` is NOT NULL with no default** on ``approval_definition``,
  ``approval_rule`` and ``approval_stage``, none of which has an
  ``updated_by``. Both are invisible without a live server and have already
  cost this project two CI cycles, so both are pinned below without one.
* **``approval.read`` excludes Auditor** (D-12). No test here assumes an
  Auditor can open an inbox; widening
  ``test_aud_c_006_auditor_is_read_only`` is not a change this stream makes.

**Runs only against a live PostgreSQL.** There is none on a developer
workstation, so the live tests skip locally and run in the ``pg_tests`` CI job.
Inside the gate nothing skips: a precondition the environment guarantees
ASSERTS. Nine end-to-end tests once opted out of CI five different ways while
the job reported green.

Ownership: stream 5 owns this file and
``tests/test_approval_maker_checker.py`` only. Nothing here edits an
application file, a migration, another test, or ``TEST_MANIFEST.json``.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, which is deliberately NOT named
# conftest.py (a second conftest under tests/ shadows the root one and breaks
# `from conftest import ...` in the baseline modules). Import them explicitly.
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
from app.backend.pg import approval_schema as schema             # noqa: E402
from app.backend.pg import approvals as engine                   # noqa: E402
from app.backend.pg.engine import Scope                          # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="needs a live PostgreSQL (CAPEX_DB_URL); runs in the pg_tests CI job",
)

PROJECT_ROOT = _Path(__file__).resolve().parents[1]
SEED_FILE = PROJECT_ROOT / "migrations" / "pg" / "seed_demo.sql"
MIGRATION_008 = PROJECT_ROOT / "migrations" / "pg" / "008_approval_engine.sql"

# ---------------------------------------------------------------- the scenario
#
# The cell the demo revision is raised against. WBS-A-CIVIL-FOUND-PIL is a leaf
# with no budget of its own, so availability rolls up from the owning ancestors
# WBS-A-CIVIL-FOUND (Rs 20,00,000) and WBS-A-CIVIL (Rs 80,00,000, plus an
# approved revision), less the subtree's exposure. Chosen because it exercises
# the multi-level-ownership rollup rather than a single flat cell.
DEMO_PROJECT = "PRJ-DM-001"
DEMO_ENTITY = "ENT-DM1"
DEMO_WBS = "WBS-A-CIVIL-FOUND-PIL"
DEMO_HEAD = "BH-DM1-CIVIL"

# Seeded identities.
#
# MAKER holds `revision.create` (Requestor) and is unrestricted on every scope
# dimension, so a refusal can never be a scope miss in disguise.
#
# APPROVER is U-PFC, and the choice is forced rather than free: the seeded
# BUDGET_REVISION workflow (migrations/pg/seed_parts/008_approvals.sql,
# APD-BREV-ORG-V1) names ROLE 'Project Finance Controller', and `role_grant`
# grants that to U-PFC alone. Note that PostgreSQL's role vocabulary is NOT
# SQLite's -- `role_grant` carries business roles ('Project Finance
# Controller'), `user_role` carries permission roles ('FinanceApprover') -- and
# an approval stage resolves against the former while `auth.PERMISSIONS`
# resolves against the latter. U-PFC satisfies both, which is why the demo
# works; U-CFO holds the permissions but not the role, and would be refused
# NOT_AN_ASSIGNEE.
MAKER = "U-REQ"
APPROVER = "U-PFC"

#: The seeded, org-wide BUDGET_REVISION workflow the demo routes under.
DEMO_DEFINITION = "APD-BREV-ORG-V1"

#: Comfortably beyond the whole project's budget, so "exceeds" cannot depend on
#: exposure figures drifting in a later seed revision. Integer paise.
OVER_BUDGET_PAISE = 99_00_00_00_000

#: Fixed, so the route never depends on the day the suite runs.
BUSINESS_DATE = date(2026, 9, 1)


def assert_paise(value, label: str) -> int:
    """A money value must be an `int` of paise -- never Decimal, never float.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so a stray flag
    would otherwise satisfy an ``isinstance(value, int)`` money assertion.
    """
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"{label} is {type(value).__name__} ({value!r}), not integer paise. "
        "A numeric column returns Decimal from psycopg, and the first "
        "arithmetic mixing it with an int propagates a non-integer amount "
        "through the financial controls.")
    return value


# ==========================================================================
# The reconciliation itself, asserted WITHOUT a database
# ==========================================================================
#
# Everything below runs on a developer's machine. Each of these facts is a
# premise the live scenario rests on, and each has a silent failure mode: if
# it stops holding, the live tests would either stop testing what they claim
# or fail in CI with a message about something else entirely.


def test_the_demo_object_type_is_routable_and_a_purchase_request_is_not():
    """Amendment A1, pinned where it can be read.

    ``OBJECT_BINDINGS`` is a closed allow-list because table and column names
    reach SQL through f-strings. A purchase request is deliberately absent --
    PR and PO live in the SQLite application and have no PostgreSQL table -- so
    the demo scenario re-targets to a budget revision rather than declaring a
    binding to a table that does not exist.
    """
    assert "BUDGET_REVISION" in engine.OBJECT_BINDINGS, (
        "BUDGET_REVISION is no longer routable, so the demo scenario has no "
        f"object to route. Bindings: {sorted(engine.OBJECT_BINDINGS)}")

    binding = engine.binding_for("BUDGET_REVISION")
    assert binding.table == "budget_revision"
    assert binding.maker_column == "created_by", (
        "the engine reads the maker from a column the demo relies on; if the "
        "document schema moved, so has maker-checker's first identity")

    assert "PURCHASE_REQUEST" not in engine.OBJECT_BINDINGS, (
        "a PURCHASE_REQUEST binding now exists. If the PostgreSQL "
        "purchase-request module has landed, amendment A1 is superseded and "
        "the demo should be re-targeted back to step (a) as originally "
        "written -- this test is the reminder to do it deliberately.")
    with pytest.raises(rules.ApprovalError) as excinfo:
        engine.binding_for("PURCHASE_REQUEST")
    assert excinfo.value.code == "OBJECT_TYPE_UNKNOWN", (
        "an unroutable object type must fail loudly before any identifier is "
        "formatted into SQL, not be treated as nobody's business")


def test_approval_action_carries_the_idempotency_columns_and_no_detail_column():
    """Amendment A3, and the reason the chain is verifiable at all.

    Contract 8 requires a decision to carry an idempotency key and a replay to
    return the ORIGINAL outcome; the frozen contract declared nowhere to store
    either. There is deliberately **no ``detail`` column**: the frozen audit
    payload ``prev|at|actor|action|type|id|detail`` must be recomputable from
    stored columns, or verification could only ever check that the
    ``prev_hash`` links agree with each other -- and an attacker who rewrote an
    entry's ``action`` from REJECT to APPROVE and recomputed nothing would pass
    that check.
    """
    columns = schema.COLUMNS[schema.APPROVAL_ACTION]

    assert {"idempotency_key", "outcome"} <= set(columns), (
        f"approval_action is missing the amendment A3 columns: {columns}")
    assert "detail" not in columns, (
        "approval_action now has a `detail` column. If the narrative is "
        "hashed again, chain_detail() and verify_instance_chain() must be "
        "revisited together -- a digest built from a column the verifier does "
        "not read back is a digest nothing can recompute.")

    # The hashed detail is composed only of columns the row actually stores.
    assert engine.chain_detail("ASI-1", "BUDGET_EXCEEDED", "board approved") == \
        "ASI-1\x1fBUDGET_EXCEEDED\x1fboard approved"
    assert engine.chain_detail(None, None, None) == "\x1f\x1f", (
        "a nullable component must still occupy its position, or two "
        "different rows could hash identically")


def test_the_configuration_tables_require_created_by_and_have_no_updated_by():
    """The fixture rule that has already bitten this project twice.

    ``created_by`` is NOT NULL with no default on all three configuration
    tables -- a workflow definition with no recorded author is exactly the row
    an auditor would ask about. There is no ``updated_by`` on any of them:
    unlike the operational tables, an ACTIVE definition is immutable, so a
    last-updated column would only ever restate ``created_by``.

    Both failures are visible ONLY against live PostgreSQL, there being no
    local server, so both are pinned here where a fixture author will see them.
    """
    for table in (schema.APPROVAL_DEFINITION, schema.APPROVAL_RULE,
                  schema.APPROVAL_STAGE):
        columns = schema.COLUMNS[table]
        assert "created_by" in columns, f"{table} no longer records its author"
        assert "updated_by" not in columns, (
            f"{table} has gained an updated_by column. Every fixture in this "
            f"suite omits it deliberately; if it is now real (and defaulted), "
            f"say so here rather than letting an INSERT fail only in CI.")

    text = MIGRATION_008.read_text(encoding="utf-8")
    assert "action_id           bigint GENERATED ALWAYS AS IDENTITY" in text, (
        "approval_action.action_id is no longer GENERATED ALWAYS AS IDENTITY. "
        "Every INSERT in this suite omits it because PostgreSQL rejects a "
        "supplied value outright; if that changed, the omission has to be "
        "reconsidered rather than inherited.")


def test_the_demo_identities_hold_what_the_scenario_needs_and_no_more():
    """Step (c)'s "a DIFFERENT authorised user", stated as a permission fact.

    If the maker ever came to hold the approving permission, or the approver
    the raising one, the scenario would still pass while proving something
    weaker than it claims. ``approval.read`` excluding Auditor is asserted in
    the same place because it is the D-12 decision this stream was told not to
    quietly widen.
    """
    assert MAKER != APPROVER

    def holds(user_roles, permission):
        return bool(set(user_roles) & set(auth.PERMISSIONS[permission]))

    maker_roles = dict(auth.DEV_USERS)[MAKER]
    approver_roles = dict(auth.DEV_USERS)[APPROVER]

    assert holds(maker_roles, "revision.create"), (
        f"{MAKER} ({maker_roles}) cannot raise a budget revision, so step (a) "
        f"could not happen at all")
    assert holds(maker_roles, "approval.act"), (
        f"{MAKER} must hold approval.act, or step (e)'s refusal would be an "
        f"ordinary permission denial rather than maker-checker")
    assert not holds(maker_roles, "revision.approve"), (
        f"{MAKER} now holds revision.approve; the demo's maker and approver "
        f"are no longer distinguishable by authority")

    assert holds(approver_roles, "revision.approve"), (
        f"{APPROVER} ({approver_roles}) cannot approve a revision, so step (c) "
        f"would be refused for the wrong reason")
    assert holds(approver_roles, "approval.act")
    assert holds(approver_roles, "approval.read")

    assert "Auditor" not in auth.PERMISSIONS["approval.read"], (
        "Auditor has been granted approval.read. That widens the allow-list "
        "test_aud_c_006_auditor_is_read_only pins, which is a D-12 decision "
        "and not one to make as a side effect. An Auditor reads approval "
        "history through the audit chain.")


def test_the_approval_stream_key_is_the_one_the_verifier_reports():
    """Contract 9's stream naming, in one expression, without a server."""
    assert schema.audit_stream_key("AINS-1") == "approval:AINS-1"
    assert schema.AUDIT_STREAM_PREFIX == "approval:"


def test_a_parallel_wave_opens_even_when_one_of_its_stages_is_skipped():
    """A conditional stage inside a parallel group must not block the group.

    ``compute_waves`` puts stages sharing a ``parallel_group`` into one wave.
    ``open_instance`` records every non-applicable stage as SKIPPED first, and
    only then asks which wave to open -- so by the time ``next_wave`` is
    called, a wave can legitimately hold both a SKIPPED stage and stages with
    no state at all. That wave is the one to open, minus the stage that does
    not apply.

    Was an ``xfail(strict=True)`` while the branch was wrong; the marker is
    gone now that ``next_wave`` treats a SKIPPED member as settled rather than
    as "in progress", so this asserts the behaviour positively.
    """
    assert engine.compute_waves([]) == [], (
        "sanity: compute_waves is the function that builds the wave shape "
        "asserted below")

    opened = engine.next_wave([[1, 2]], {2: rules.STAGE_SKIPPED})

    assert opened is not None, (
        "a parallel wave whose conditional member was skipped at routing "
        "reports that there is nothing left to open. open_instance reads that "
        "as 'every stage was skipped' and holds the object EXCEPTION_PENDING, "
        "so no workflow may put an applies_when stage in a parallel group.")
    assert 1 in opened, (
        f"the wave to open is {opened}, which omits the stage that does apply")


# ============================================================ shared fixtures
@pytest.fixture()
def seeded_pg(pg_connection, pg_database):
    """A disposable PostgreSQL database carrying the demo dataset AND the
    per-stream seed fragments.

    ASSERTS, never skips. These files are committed and lead-owned, so their
    absence is not an environment condition; it means a frozen file was
    deleted or renamed, and every test below depends on them.

    Unlike ``test_pg_audit_api_e2e.py``'s fixture this also loads
    ``seed_parts/``, because the approval scenario needs the role and scope
    grants in ``004_access.sql`` and the workflow configuration in
    ``008_approvals.sql``. Without the former every seeded identity resolves to
    a restricted scope and an approval refusal could not be distinguished from
    a scope miss; without the latter there is no BUDGET_REVISION workflow to
    route under. ``seed.seed_part_files()`` is used rather than a hard-coded
    list, so a fragment added later is picked up automatically.
    """
    from app.backend.pg import seed as pg_seed

    assert SEED_FILE.is_file(), (
        f"{SEED_FILE} is missing. It is committed and frozen, so this is not a "
        "setup gap -- it means the seed was deleted or renamed, and every "
        "end-to-end test below depends on it.")
    pg_connection.execute(SEED_FILE.read_text(encoding="utf-8"))

    parts = pg_seed.seed_part_files()
    assert parts, (
        f"no seed fragments found under {pg_seed.SEED_PARTS_DIR}. "
        "seed_parts/004_access.sql carries the role and scope grants and "
        "008_approvals.sql the workflow this scenario routes under; an empty "
        "directory means they were moved.")
    for part in parts:
        pg_connection.execute(part.read_text(encoding="utf-8"))

    pg_connection.commit()
    return pg_database


@pytest.fixture()
def pg_backed_app(seeded_pg):
    """``main.app`` wired to the seeded PostgreSQL database.

    Mount detection is a BEHAVIOURAL PROBE, not route-table introspection.
    Two earlier versions of the audit end-to-end fixture inspected
    ``main.app.routes`` -- a flat scan, then a recursive walk -- and both
    concluded "not mounted" in CI while the router was mounted perfectly well.
    FastAPI versions differ in how an included router appears there, so any
    structural inspection is a guess about internals that changes between
    releases. Asking the application whether it serves the route cannot be
    wrong about that.
    """
    from app.backend import main
    from app.backend.pg import engine as pg_engine

    try:
        previous = pg_engine.get_database()
    except RuntimeError:
        previous = None
    pg_engine.set_database(seeded_pg)
    try:
        yield main.app
    finally:
        pg_engine.set_database(previous)


@pytest.fixture()
def as_user(pg_backed_app, capex_db):
    """Log a SEEDED identity in and return an authenticated client.

    Seeded identities, not ``make_user`` ones: an approval instance references
    its maker, actor and assignees by ``user_id``, and every one of those
    columns is a foreign key into PostgreSQL's ``app_user``. A ``make_user``
    identity exists only in the SQLite identity store, so it would fail the
    constraint outright.

    ``capex_db`` is requested explicitly because this product authenticates
    against SQLite while holding its financial data in PostgreSQL. Without it
    ``db.DB_PATH`` is still the session sentinel that ``conftest.py``'s
    ``_isolate_database_module`` installs, every login fails, and the failure
    appears only in CI -- these tests skip on the machine where the fixture was
    written.

    The first login doubles as the mount probe: it ASSERTS rather than skips,
    because with ``CAPEX_DB_URL`` set the routers being mounted is a guarantee,
    not an environment condition.
    """
    from fastapi.testclient import TestClient
    from conftest import PASSWORD_SUFFIX, RoleClient

    client = TestClient(pg_backed_app, raise_server_exceptions=False)
    cache: dict[str, RoleClient] = {}

    def _as(user_id: str) -> RoleClient:
        if user_id not in cache:
            resp = client.post("/api/auth/login",
                               json={"user_id": user_id,
                                     "password": user_id + PASSWORD_SUFFIX})
            assert resp.status_code == 200, (
                f"login for the seeded identity {user_id} failed: {resp.text}")
            cache[user_id] = RoleClient(client, user_id, resp.json()["session_id"])
        return cache[user_id]

    probe = _as(MAKER).get(
        f"/api/budget/availability?wbs_id={DEMO_WBS}"
        f"&budget_head_id={DEMO_HEAD}&amount_paise=1")
    assert probe.status_code != 404, (
        "GET /api/budget/availability returned 404: the PostgreSQL budget "
        "router is not serving. main.py mounts it unconditionally, so a 404 "
        "means include_router did not register these paths. This ASSERTS "
        "rather than skips -- with CAPEX_DB_URL set the mounting is a "
        "guarantee, and skipping on a broken guarantee is how this suite "
        "silently opted out of CI twice while the job reported green.")
    return _as


def _system_scope() -> Scope:
    """Unrestricted. The scenario's refusals are about separation and budget,
    and a scope refusal here would be a different control failing."""
    return Scope(user_id="U-TEST-E2E", principal_kind="USER", read_all=True)


# ==========================================================================
# Step (a), proved through the real guard against real rows
# ==========================================================================


@pytest.mark.pg
@PG
def test_step_a_the_demo_request_exceeds_available_budget(as_user):
    """The scenario's premise, through the real guard, against real rows.

    If this cell ever came to have enough budget, every "routes to exception
    approval" assertion below would still pass while proving nothing -- the
    request would simply be within budget. That failure mode is silent, so the
    premise is asserted separately and first.
    """
    resp = as_user(MAKER).get(
        f"/api/budget/availability?wbs_id={DEMO_WBS}"
        f"&budget_head_id={DEMO_HEAD}&amount_paise={OVER_BUDGET_PAISE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["verdict"] == "EXCEEDS_BUDGET", (
        f"the demo request of {OVER_BUDGET_PAISE} paise is {body['verdict']} "
        f"at {DEMO_WBS}/{DEMO_HEAD}, not EXCEEDS_BUDGET. The scenario's "
        "premise no longer holds and every exception-routing assertion below "
        "would pass vacuously.")

    available = assert_paise(body["available_paise"], "available_paise")
    requested = assert_paise(body["requested_paise"], "requested_paise")
    shortfall = assert_paise(body["shortfall_paise"], "shortfall_paise")
    assert_paise(body["budget_paise"], "budget_paise")
    assert_paise(body["exposure_paise"], "exposure_paise")

    assert requested == OVER_BUDGET_PAISE
    assert shortfall > 0, "EXCEEDS_BUDGET with a zero shortfall is incoherent"
    assert shortfall == requested - available, (
        f"shortfall {shortfall} != requested {requested} - available "
        f"{available}; the arithmetic the exception approval is justified by "
        "does not add up")


@pytest.mark.pg
@PG
def test_step_a_a_within_budget_request_at_the_same_cell_is_not_an_exception(as_user):
    """The control case for the test above.

    Without it, an availability service that answered EXCEEDS_BUDGET to
    everything would satisfy the premise test and make the whole scenario
    meaningless.
    """
    resp = as_user(MAKER).get(
        f"/api/budget/availability?wbs_id={DEMO_WBS}"
        f"&budget_head_id={DEMO_HEAD}&amount_paise=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["verdict"] != "EXCEEDS_BUDGET", (
        "one paise exceeds available budget at this cell, so the seeded "
        "ledger has no headroom at all and the scenario needs a different "
        "cell")
    assert assert_paise(body["shortfall_paise"], "shortfall_paise") == 0


@pytest.mark.pg
@PG
def test_step_a_the_maker_raises_the_revision_through_the_real_route(as_user, seeded_pg):
    """The document itself: a real row, raised by a real, authorised caller.

    The maker is never named in the request body -- ``_actor`` reads it from
    the server-side session -- and ``created_by`` is what maker-checker
    compares against later. A route that let a caller nominate the maker would
    make every step below defeatable in one line.
    """
    resp = as_user(MAKER).post("/api/budget/revisions", json={
        "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD,
        "delta_paise": OVER_BUDGET_PAISE, "effective_from": "2026-09-01",
        "justification": "Foundation piling scope growth; board paper CAPEX-COMM/2026/031."})
    assert resp.status_code == 201, resp.text
    revision_id = resp.json()["revision_id"]
    assert resp.json()["status"] == "DRAFT", (
        f"a freshly raised revision is {resp.json()['status']!r}. A revision "
        "creates no spending capacity until approved, so DRAFT is the only "
        "honest starting state.")
    assert_paise(resp.json()["delta_paise"], "delta_paise")

    with seeded_pg.session(_system_scope()) as session:
        row = session.fetchone(
            "SELECT created_by, status, delta_paise, version_no "
            "FROM budget_revision WHERE revision_id = %s", (revision_id,))
    assert row is not None, f"{revision_id} was returned but no row was written"
    assert row[0] == MAKER, (
        f"the revision records created_by={row[0]!r}, not the session's "
        f"identity. Maker-checker compares exactly this value.")
    assert row[1] == "DRAFT"
    assert_paise(row[2], "budget_revision.delta_paise")


# ==========================================================================
# The scenario harness
# ==========================================================================


class _DemoScenario:
    """One over-budget revision, routed, plus the levers the seven steps pull.

    Reads go to Contract 1's tables -- the ledger, not a return value.
    Decisions go through the engine's own ``decide``, which is the only
    surface that takes the second identity Contract 5 is about: the HTTP body
    model forbids unknown fields and declares no ``acting_for_user_id``.
    """

    maker = MAKER
    approver = APPROVER

    def __init__(self, database, as_user, *, instance_id, revision_id,
                 object_version, routed, routed_available_paise):
        self._database = database
        self._as_user = as_user
        self.instance_id = instance_id
        self.revision_id = revision_id
        self.object_version = object_version
        self.routed = routed
        self.routed_available_paise = assert_paise(
            routed_available_paise, "availability recorded at routing")

    # -------------------------------------------------------------- engine
    def decide(self, *, as_who, action="APPROVE",
               reason_text="board-approved overrun, ref CAPEX-COMM/2026/031",
               reason_code=None, object_version=None, idempotency_key=None,
               acting_for=None):
        with self._database.session(_system_scope()) as session:
            return engine.decide(
                session, instance_id=self.instance_id, actor_user_id=as_who,
                action=action,
                idempotency_key=idempotency_key or f"e2e-{uuid.uuid4().hex[:12]}",
                object_version=(self.object_version if object_version is None
                                else object_version),
                reason_code=reason_code, reason_text=reason_text,
                acting_for_user_id=acting_for)

    # ---------------------------------------------------------------- HTTP
    def decide_over_http(self, *, as_who, action="APPROVE",
                         reason_text="board-approved overrun"):
        return self._as_user(as_who).post(
            f"/api/approvals/{self.instance_id}/decide", json={
                "action": action, "reason_text": reason_text,
                "idempotency_key": f"http-{uuid.uuid4().hex[:12]}",
                "object_version": self.object_version})

    # ------------------------------------------------------------ ledger
    def _fetchone(self, statement, params):
        with self._database.session(_system_scope()) as session:
            return session.fetchone(statement, params)

    def _fetchall(self, statement, params):
        with self._database.session(_system_scope()) as session:
            return session.fetchall(statement, params)

    def instance_status(self) -> str:
        row = self._fetchone(
            "SELECT status FROM approval_instance WHERE instance_id = %s",
            (self.instance_id,))
        assert row is not None, (
            f"approval_instance {self.instance_id!r} does not exist")
        return row[0]

    def document_status(self) -> str:
        row = self._fetchone(
            "SELECT status FROM budget_revision WHERE revision_id = %s",
            (self.revision_id,))
        assert row is not None, f"budget_revision {self.revision_id!r} is gone"
        return row[0]

    def stage_statuses(self) -> list[tuple]:
        return [tuple(r) for r in self._fetchall(
            "SELECT stage_no, status, skip_reason FROM approval_stage_instance "
            "WHERE instance_id = %s ORDER BY stage_no", (self.instance_id,))]

    def timeline(self) -> list[tuple]:
        """The append-only action stream, which is what a timeline renders.

        ``approval_action`` carries ``instance_id`` directly, so no join
        through ``approval_stage_instance`` is needed -- and a join would
        silently drop the instance-level entries (OPEN, ESCALATE, RECALL)
        whose ``stage_instance_id`` is NULL by design.
        """
        return self._fetchall(
            "SELECT seq, actor_user_id, acting_for_user_id, action, reason_text "
            "FROM approval_action WHERE instance_id = %s ORDER BY seq",
            (self.instance_id,))

    def approve_actions(self) -> list[tuple]:
        return [row for row in self.timeline() if row[3] == rules.ACTION_APPROVE]

    def assignments(self) -> list[tuple]:
        """(assignee, state, assigned_via) for every stage of this instance."""
        return [tuple(row) for row in self._fetchall(
            """SELECT a.assignee_user_id, a.state, a.assigned_via
               FROM approval_assignment a
               JOIN approval_stage_instance si USING (stage_instance_id)
               WHERE si.instance_id = %s ORDER BY a.assignee_user_id""",
            (self.instance_id,))]

    def assignment_state(self, user_id: str) -> str | None:
        for assignee, state, _via in self.assignments():
            if assignee == user_id:
                return state
        return None

    def attempt_to_rewrite_the_log(self):
        """Try to edit the append-only decision log through the product's own
        connection. Raises whatever the database raises."""
        with self._database.session(_system_scope()) as session:
            session.execute(
                "UPDATE approval_action SET reason_text = 'rewritten' "
                "WHERE instance_id = %s", (self.instance_id,))

    def available_paise(self) -> int:
        """Availability at the demo cell, from the product's own service."""
        from app.backend.pg import budget as budget_svc

        with self._database.session(_system_scope()) as session:
            verdict = budget_svc.check_availability(session, DEMO_WBS, DEMO_HEAD, 0)
        return assert_paise(verdict["available_paise"], "available_paise")

    def verify_approval_chain(self) -> dict:
        """Contract 9, through the engine's own verifier.

        ``approval_action`` is a separate hash-chained ledger from
        ``audit_log``: ``_append_action`` writes there and nowhere else, so
        ``audit.verify_chain`` on ``approval:{id}`` would find no stream at
        all. ``verify_instance_chain`` mirrors it exactly -- including the
        contiguity check -- and, because amendment A3 removed the ``detail``
        column, it RECOMPUTES every digest from the stored columns rather than
        merely walking the ``prev_hash`` links.
        """
        with self._database.session(_system_scope()) as session:
            return engine.verify_instance_chain(session, self.instance_id)

    # ------------------------------------------------- moving the goalposts
    def consume_the_remaining_budget(self):
        """Spend the cell's headroom after routing, before the decision.

        Contract 7 requires the decision to re-run ``check_availability``
        inside the approving transaction. The only way to prove it ran is to
        move availability underneath an already-routed instance and require
        the decision to notice.
        """
        before = self.available_paise()
        with self._database.session(_system_scope()) as session:
            session.execute(
                """
                INSERT INTO budget_ledger_cell
                    (wbs_id, budget_head_id, commitment_paise, updated_by)
                VALUES (%(wbs)s, %(head)s, %(amount)s, 'E2E')
                ON CONFLICT (wbs_id, budget_head_id) DO UPDATE
                SET commitment_paise = budget_ledger_cell.commitment_paise
                                       + EXCLUDED.commitment_paise,
                    updated_by = 'E2E'
                """,
                {"wbs": DEMO_WBS, "head": DEMO_HEAD, "amount": before})
        after = self.available_paise()
        assert after < before, (
            f"the fixture did not actually move availability ({before} -> "
            f"{after} paise), so the test it serves cannot prove revalidation "
            f"happened")
        return before, after


@pytest.fixture()
def demo_scenario(as_user, seeded_pg):
    """The routed over-budget revision: steps (a) and (b) as arrangement.

    The maker raises the revision through the real HTTP route, the product's
    own availability service supplies the numbers the snapshot records, and the
    engine's own entry point routes it.

    ``open_instance`` is called directly because **nothing else calls it**:
    ``pg/budget.py::create_revision`` opens no approval instance, and no route
    does either. That is a reported integration gap, and it is stated here
    rather than hidden behind a helper, because a reader has to know that this
    seam is the test's and not the product's.
    """
    from app.backend.pg import budget as budget_svc

    created = as_user(MAKER).post("/api/budget/revisions", json={
        "wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD,
        "delta_paise": OVER_BUDGET_PAISE, "effective_from": "2026-09-01",
        "justification": "Foundation piling scope growth; board paper CAPEX-COMM/2026/031."})
    assert created.status_code == 201, (
        f"the maker could not raise the demo revision: {created.text}")
    revision_id = created.json()["revision_id"]

    with seeded_pg.session(_system_scope()) as session:
        check = budget_svc.check_availability(
            session, DEMO_WBS, DEMO_HEAD, OVER_BUDGET_PAISE)
    assert check["verdict"] == "EXCEEDS_BUDGET", (
        f"the demo revision is {check['verdict']} at this cell, not over "
        f"budget; the scenario's premise no longer holds")

    snapshot = {
        "object_type": "BUDGET_REVISION",
        "object_id": revision_id,
        "entity_id": DEMO_ENTITY,
        "project_id": DEMO_PROJECT,
        "budget_head_id": DEMO_HEAD,
        "amount_paise": OVER_BUDGET_PAISE,
        "exceeds_available": True,
        "maker_checker_permission": "revision.approve",
        "affected_cells": [{"wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD}],
        "budget_checks": [check],
    }

    with seeded_pg.session(_system_scope()) as session:
        routed = engine.open_instance(
            session, object_type="BUDGET_REVISION", object_id=revision_id,
            object_version=1, snapshot=snapshot, maker_user_id=MAKER,
            business_date=BUSINESS_DATE)

    return _DemoScenario(
        seeded_pg, as_user, instance_id=routed["instance_id"],
        revision_id=revision_id, object_version=1, routed=routed,
        routed_available_paise=check["available_paise"])


# ==========================================================================
# Steps (b) - (g)
# ==========================================================================


@pytest.mark.pg
@PG
def test_step_b_an_over_budget_request_routes_to_exception_approval(demo_scenario):
    """Not auto-approved, and not silently allowed.

    Contract 2 admits no route to auto-approval. The instance is OPEN with a
    PENDING stage awaiting a named, independent approver -- never APPROVED, and
    never "no instance at all", which would mean the revision proceeded with no
    approval record.

    "Exception approval" is what the seeded BUDGET_REVISION workflow models for
    a revision: a gate that demands a written justification. That is asserted
    behaviourally by the companion test below rather than read off a label.
    """
    s = demo_scenario

    assert s.routed["definition_id"] == DEMO_DEFINITION, (
        f"the revision routed under {s.routed['definition_id']!r}, not the "
        f"seeded org-wide BUDGET_REVISION workflow {DEMO_DEFINITION!r}. The "
        f"demo is no longer demonstrating the configuration that ships.")

    status = s.instance_status()
    assert status == rules.INST_OPEN, (
        f"an over-budget revision routed to {status!r}. Contract 2 admits no "
        f"route to auto-approval.")
    assert status != rules.INST_APPROVED

    stages = s.stage_statuses()
    assert stages, (
        "the instance has no stage instances at all. Contract 2 requires even "
        "a stage that does not apply to be recorded SKIPPED with a "
        "skip_reason, because an auditor must see what did not run.")
    assert any(st == rules.STAGE_PENDING for _no, st, _skip in stages), (
        f"no stage is PENDING, so nobody has been asked to approve: {stages}")

    assignees = s.assignments()
    assert s.assignment_state(APPROVER) == rules.ASSIGN_PENDING, (
        f"{APPROVER} was not asked to approve: {assignees}")
    assert MAKER not in {row[0] for row in assignees}, (
        f"the maker is an assignee of their own revision: {assignees}. The "
        f"contributor filter did not run.")


@pytest.mark.pg
@PG
def test_step_b_the_exception_gate_refuses_a_decision_with_no_reason(demo_scenario):
    """What makes this an EXCEPTION approval rather than an ordinary one.

    Approving spend above available budget is exactly the decision that must be
    justified in words. The seeded stage carries ``requires_reason``; this
    proves the engine enforces it, so the "with a reason" in step (c) is a
    control rather than a convention the next caller can drop.
    """
    s = demo_scenario

    with pytest.raises(rules.ApprovalError) as excinfo:
        s.decide(as_who=APPROVER, reason_text=None, reason_code=None)

    assert excinfo.value.code == rules.ERR_REASON_REQUIRED, (
        f"a reason-less decision on the exception gate was refused with "
        f"{excinfo.value.code!r} rather than REASON_REQUIRED")
    assert s.instance_status() != rules.INST_APPROVED
    assert not s.approve_actions(), (
        "an APPROVE action was written for a refused decision")


@pytest.mark.pg
@PG
def test_step_e_the_maker_cannot_approve_their_own_request(demo_scenario):
    """Step (e), asserted BEFORE the successful approval.

    Deliberately ordered first: once the instance is APPROVED a self-approval
    attempt would be refused as STAGE_NOT_OPEN, and the test would pass
    without maker-checker having been consulted at all.

    The maker is not even an assignee here -- the contributor filter removed
    them at routing -- so the refusal comes from the assignment check, which is
    the earlier and stronger of Contract 5's two enforcement points. Either
    code is an acceptable refusal; an acceptance is not, and neither is a
    ledger that moved.
    """
    s = demo_scenario

    with pytest.raises(rules.ApprovalError) as excinfo:
        s.decide(as_who=s.maker, reason_text="approving my own revision")

    assert excinfo.value.code in (rules.ERR_SELF_APPROVAL, rules.ERR_NOT_AN_ASSIGNEE), (
        f"the maker attempted to approve their own revision and was refused "
        f"with {excinfo.value.code!r}. Neither maker-checker nor the "
        f"assignment check turned them away.")

    assert s.instance_status() != rules.INST_APPROVED, (
        "the self-approval was refused but the instance is APPROVED. The "
        "refusal did not hold in the database.")
    assert not s.approve_actions(), (
        "an APPROVE action was written for a refused self-approval. "
        "approval_action is append-only, so this is permanent.")


@pytest.mark.pg
@PG
def test_step_e_a_decision_taken_on_the_makers_behalf_is_also_refused(demo_scenario):
    """Step (e)'s second half: the maker's hands are not the only ones.

    A genuinely assigned, independent approver decides "on behalf of" the
    person who raised the revision. Contract 5 checks BOTH identities, so this
    is refused; an engine that checked only the actor would accept it and the
    maker would have approved their own revision through somebody else.
    """
    s = demo_scenario

    with pytest.raises(rules.ApprovalError) as excinfo:
        s.decide(as_who=APPROVER, acting_for=MAKER,
                 reason_text="deciding on behalf of the person who raised this")

    assert excinfo.value.code == rules.ERR_SELF_APPROVAL, (
        f"a decision taken on the MAKER's behalf was refused with "
        f"{excinfo.value.code!r} rather than SELF_APPROVAL")
    assert s.instance_status() != rules.INST_APPROVED
    assert not s.approve_actions()


@pytest.mark.pg
@PG
def test_step_c_a_different_authorised_user_approves_with_a_reason(demo_scenario):
    """Step (c), and the ledger it must move.

    A successful return proves nothing on its own: the assertion that matters
    is that the instance, the stage, the assignment and the append-only action
    log all record the decision, attributed to the approver and carrying the
    reason.
    """
    s = demo_scenario
    assert s.approver != s.maker, (
        "the scenario's approver and maker are the same identity")

    result = s.decide(as_who=s.approver,
                      reason_text="board-approved overrun, ref CAPEX-COMM/2026/031")
    assert result.instance_status == rules.INST_APPROVED, (
        f"an independent, authorised approver produced "
        f"{result.instance_status!r}")

    assert s.instance_status() == rules.INST_APPROVED, (
        f"the decision reported APPROVED but the instance is "
        f"{s.instance_status()!r}. This is the exact defect this module exists "
        "to catch: an approval that answers successfully while the ledger did "
        "not move.")

    stages = s.stage_statuses()
    assert all(st != rules.STAGE_PENDING for _no, st, _skip in stages), (
        f"the instance is APPROVED while a stage is still PENDING: {stages}")

    approves = s.approve_actions()
    assert len(approves) == 1, (
        f"expected exactly one APPROVE action, found {len(approves)}: {approves}")
    _seq, actor, acting_for, _action, reason_text = approves[0]
    assert actor == s.approver, (
        f"the approval is attributed to {actor!r}, not to the approver who "
        "took it")
    assert acting_for is None, (
        f"a direct decision recorded acting_for_user_id={acting_for!r}")
    assert reason_text and reason_text.strip(), (
        "an exception approval was recorded with no reason. The reason is the "
        "justification an auditor reads; it is not optional on this path.")

    assert s.assignment_state(s.approver) == rules.ASSIGN_ACTED, (
        f"the approver's assignment is {s.assignment_state(s.approver)!r}, so "
        f"the record does not show that the person asked is the person who "
        f"answered")


@pytest.mark.pg
@PG
def test_step_d_the_budget_is_revalidated_inside_the_approving_transaction(
        demo_scenario):
    """Contract 7: availability may have moved since routing.

    Proved by moving it. The headroom is consumed after the instance is routed
    and before the decision is taken; the decision must refuse with
    BUDGET_MOVED rather than approve against the numbers that were true at
    submission. A decision that merely re-read the routing snapshot would
    approve here, and the money would be committed twice.
    """
    s = demo_scenario

    before, after = s.consume_the_remaining_budget()
    assert before == s.routed_available_paise, (
        f"availability was {s.routed_available_paise} when the approvers were "
        f"asked and {before} before this test moved it; something else has "
        f"already changed the cell and the comparison below is not the one "
        f"this test intends")
    assert_paise(after, "available_paise after the move")

    with pytest.raises(rules.ApprovalError) as excinfo:
        s.decide(as_who=APPROVER,
                 reason_text="deciding against numbers that have since moved")

    assert excinfo.value.code == rules.ERR_BUDGET_MOVED, (
        f"refused with {excinfo.value.code!r} rather than the frozen "
        f"BUDGET_MOVED, so the caller cannot tell availability was the cause")
    assert excinfo.value.status == 409, (
        "BUDGET_MOVED is a conflict with state that has moved, not an "
        "authorisation refusal: the same caller may well succeed once "
        "refreshed, which is what 409 tells a client and 403 does not")
    assert s.instance_status() != rules.INST_APPROVED
    assert not s.approve_actions()


@pytest.mark.pg
@PG
def test_step_f_the_instance_status_and_the_timeline_both_update(demo_scenario):
    """Step (f). Both, not either.

    A timeline that grows while the status does not is a screen showing an
    approval nobody can act on; a status that moves with no timeline entry is
    an approval with no audit trail. Each has shipped before in products of
    this shape, so both are asserted together.
    """
    s = demo_scenario

    before = s.timeline()
    assert before, (
        "the instance was routed with no timeline entry at all; an auditor "
        "cannot see when or under what workflow it opened")
    status_before = s.instance_status()

    s.decide(as_who=APPROVER, reason_text="board-approved overrun")

    assert s.instance_status() == rules.INST_APPROVED, (
        f"the status did not move: {status_before!r} -> "
        f"{s.instance_status()!r}")

    after = s.timeline()
    assert len(after) > len(before), (
        f"the timeline did not grow ({len(before)} -> {len(after)} entries) "
        f"although a decision was recorded")

    latest = after[-1]
    assert latest[3] == rules.ACTION_APPROVE, (
        f"the newest timeline entry is {latest[3]!r}, not the approval just "
        f"taken")
    assert latest[1] == APPROVER, (
        f"the timeline attributes the approval to {latest[1]!r}, not to "
        f"{APPROVER}")
    assert [row[0] for row in after] == list(range(1, len(after) + 1)), (
        f"the timeline's seq values are not contiguous from 1: "
        f"{[row[0] for row in after]}. A gap is how a deletion hides.")


@pytest.mark.pg
@PG
@pytest.mark.xfail(strict=False, reason=(
    "REPORTED GAP: nothing writes the approval outcome back to the document. "
    "approvals._apply_decision moves approval_instance, approval_stage_instance "
    "and approval_assignment and never touches budget_revision, and no route "
    "or service does it either -- pg/budget.py::approve_revision is a separate, "
    "parallel approval path that the engine does not call. The business "
    "document therefore stays DRAFT after its approval instance reaches "
    "APPROVED. Not strict: this asserts the intended behaviour, and the day "
    "the write-back lands it must turn green rather than fail."))
def test_step_f_the_business_document_status_also_updates(demo_scenario):
    """Step (f) as a reader of the demo would mean it: the REVISION is approved.

    ``approval_instance.status`` moving is what the approval screen shows. What
    the business asks is whether the budget revision is approved -- and that is
    a different row, in a different table, which nothing currently updates.

    Written as an assertion of the intended behaviour rather than of the gap,
    so it turns green when the write-back lands instead of having to be
    rewritten. It is non-strict deliberately: this stream could not run it
    against a live database, and a strict xfail is a claim about a failure
    somebody has actually observed.
    """
    s = demo_scenario

    s.decide(as_who=APPROVER, reason_text="board-approved overrun")
    assert s.instance_status() == rules.INST_APPROVED

    assert s.document_status() == "APPROVED", (
        f"the approval instance is APPROVED but budget_revision "
        f"{s.revision_id} is {s.document_status()!r}. An approval that does "
        f"not reach the document is an approval the business cannot act on, "
        f"and the two records now disagree about the same decision.")


@pytest.mark.pg
@PG
def test_step_g_the_approval_chain_verifies_with_the_expected_entry_count(
        demo_scenario):
    """Contract 9, through the engine's own verifier.

    ``intact`` alone is not enough: an empty or misspelled stream once returned
    ``intact=True`` with ``entries_checked=0``, so a typo in the nightly
    verification job read as a pass. The entry count is asserted against the
    actions actually recorded, which is the only number that makes "intact"
    mean anything.
    """
    s = demo_scenario

    s.decide(as_who=APPROVER, reason_text="board-approved overrun")

    result = s.verify_approval_chain()

    assert result["stream_key"] == schema.audit_stream_key(s.instance_id), (
        f"the chain verified stream {result['stream_key']!r}, not the "
        f"Contract 9 stream for this instance")
    assert result["stream_found"] is True, (
        f"no chain for {result['stream_key']}. Contract 9 requires every "
        "action to write its entry inside the same transaction as the state "
        "change, so an approved instance with no stream means the write was "
        "skipped or the stream key drifted.")
    assert result["intact"] is True, (
        f"the approval chain does not verify: {result}. Because amendment A3 "
        "removed the `detail` column, every digest is RECOMPUTED from the "
        "stored columns -- so a break here means a stored field no longer "
        "agrees with its hash, not merely that a link is missing.")
    assert result["sequence_contiguous"] is True, (
        f"the approval chain has a sequence gap: first break at "
        f"{result['first_break_seq']}")
    assert result["first_break_seq"] is None

    expected = len(s.timeline())
    assert expected > 0, "no approval actions were recorded at all"
    assert result["entries_checked"] == expected, (
        f"the chain verified {result['entries_checked']} entries but "
        f"approval_action holds {expected} for this instance. Every action "
        "must write exactly one chained entry, in the same transaction; a "
        "mismatch means an action was recorded without its entry or vice "
        "versa.")


@pytest.mark.pg
@PG
def test_step_g_the_decision_log_cannot_be_rewritten_at_all(demo_scenario):
    """What makes ``intact`` worth reading: the log cannot be edited to match.

    A hash chain proves that stored fields and digests agree. It says nothing
    about somebody who rewrites both. ``approval_action`` is append-only by
    trigger AND by privilege, so the rewrite is refused before it reaches the
    digest -- and this asserts that on the product's own connection, which is
    the one an attacker would have.
    """
    s = demo_scenario
    s.decide(as_who=APPROVER, reason_text="board-approved overrun")
    assert s.verify_approval_chain()["intact"] is True
    entries_before = len(s.timeline())
    assert entries_before >= 2, (
        f"only {entries_before} action(s) recorded; the demo should have at "
        f"least the routing entry and the approval")

    with pytest.raises(Exception) as excinfo:
        s.attempt_to_rewrite_the_log()
    assert "append-only" in str(excinfo.value), (
        f"an UPDATE on approval_action was not refused by the append-only "
        f"trigger; it raised {excinfo.value!r} instead, which may be an "
        f"unrelated failure")

    assert s.verify_approval_chain()["intact"] is True, (
        "the refused UPDATE nevertheless disturbed the chain")
    assert len(s.timeline()) == entries_before


@pytest.mark.pg
@PG
def test_the_whole_scenario_leaves_a_verifiable_chain_and_a_moved_ledger(
        demo_scenario):
    """The demo, run once, asserted as a whole.

    The step tests above each isolate one property against a fresh instance.
    This one runs the sequence a demonstrator actually runs -- refusal then
    approval, on the SAME instance -- and asserts what a reviewer checks at the
    end: the instance moved, the permanent record verifies, and the refused
    attempt left no approval behind it.

    Worth having separately because the per-step tests cannot catch an
    interaction: a refused self-approval that consumed the stage, or left a
    partial row the later approval then appended to, would pass every step
    test above and fail here.
    """
    s = demo_scenario

    with pytest.raises(rules.ApprovalError) as refused:
        s.decide(as_who=s.maker, reason_text="approving my own revision")
    assert refused.value.code in (rules.ERR_SELF_APPROVAL, rules.ERR_NOT_AN_ASSIGNEE)

    result = s.decide(as_who=s.approver,
                      reason_text="board-approved overrun, ref CAPEX-COMM/2026/031")
    assert result.instance_status == rules.INST_APPROVED, (
        "the refused self-approval left the instance unable to be approved by "
        "an independent approver")

    assert s.instance_status() == rules.INST_APPROVED

    chain = s.verify_approval_chain()
    assert chain["intact"] is True, chain
    assert chain["entries_checked"] == len(s.timeline()), (
        "every action must write exactly one chained entry, in the same "
        "transaction")

    maker_approvals = [row for row in s.approve_actions()
                       if s.maker in (row[1], row[2])]
    assert not maker_approvals, (
        f"the maker appears on an APPROVE, as actor or as the person it was "
        f"taken for: {maker_approvals}. approval_action is append-only, so "
        f"this is permanent.")


@pytest.mark.pg
@PG
def test_a_replayed_decision_returns_the_original_outcome_and_approves_once(
        demo_scenario):
    """Contract 8, on the demo's own instance.

    A retry of a successful approval must not approve twice, and must not turn
    into a spurious refusal because the world moved in between. Asserted on the
    ledger, because "returned the same thing" and "wrote only one row" are
    different claims and only the second is the one that matters.
    """
    s = demo_scenario
    key = f"e2e-replay-{uuid.uuid4().hex[:8]}"

    first = s.decide(as_who=APPROVER, idempotency_key=key,
                     reason_text="board-approved overrun")
    assert first.replayed is False
    assert s.instance_status() == rules.INST_APPROVED

    second = s.decide(as_who=APPROVER, idempotency_key=key,
                      reason_text="board-approved overrun")
    assert second.replayed is True, (
        "the same idempotency key was accepted as a new decision")
    assert second.instance_status == first.instance_status, (
        f"the replay returned {second.instance_status!r} rather than the "
        f"original {first.instance_status!r}")
    assert len(s.approve_actions()) == 1, (
        f"a replay wrote a second APPROVE: {s.approve_actions()}")


# ==========================================================================
# Routing that must be visible: the stage that did NOT run
# ==========================================================================


def _seed_exception_workflow(connection, entity_id):
    """An ENT-DM1 workflow with a real exception gate.

    The seeded org-wide BUDGET_REVISION definition has a single always-applies
    stage, so it cannot demonstrate ``applies_when``. This one adds a second,
    conditional stage.

    The two stages are SEQUENTIAL, not a parallel group, which is what this
    scenario models: an ordinary controller gate, and behind it an exception
    gate that fires only when the request is over budget. (A parallel group
    with a conditional member routes correctly too, since ``next_wave`` now
    treats a SKIPPED member as settled -- see
    ``test_a_parallel_wave_opens_even_when_one_of_its_stages_is_skipped``.)

    An entity-specific definition sorts BEFORE a global one
    (``candidate_definitions``), so this deterministically wins for ENT-DM1.
    ``created_by`` is supplied on all three tables; none of them has an
    ``updated_by``.
    """
    tag = uuid.uuid4().hex[:8].upper()
    definition_id = f"APD-E2E-{tag}"
    connection.execute(
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        "status, entity_id, effective_from, created_by) "
        "VALUES (%s,'BUDGET_REVISION',%s,1,'ACTIVE',%s,%s,'E2E')",
        (definition_id, f"BREV-EXC-{tag}", entity_id, date(2020, 1, 1)))
    connection.execute(
        "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
        "description, created_by) VALUES (%s,%s,10,%s,%s,'E2E')",
        (f"APR-E2E-{tag}", definition_id,
         Jsonb({"all": [{"field": "object_type", "op": "eq",
                         "value": "BUDGET_REVISION"}], "route": "EXCEPTION"}),
         "Every budget revision in this entity."))
    connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
        "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
        "created_by) VALUES (%s,%s,1,'Controller review',NULL,'ALL',NULL,NULL,"
        "24,NULL,NULL,true,false,'E2E')",
        (f"APS-E2E-{tag}-1", definition_id))
    connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
        "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
        "created_by) VALUES (%s,%s,2,'Finance exception approval',NULL,'ALL',"
        "NULL,%s,24,NULL,NULL,false,true,'E2E')",
        (f"APS-E2E-{tag}-2", definition_id,
         Jsonb({"field": "exceeds_available", "op": "eq", "value": True})))
    connection.execute(
        "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
        "approver_ref, scope_expr) VALUES (%s,1,'ROLE','Project Finance Controller',NULL)",
        (f"APS-E2E-{tag}-1",))
    connection.execute(
        "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
        "approver_ref, scope_expr) VALUES (%s,1,'USER','U-CFO',NULL)",
        (f"APS-E2E-{tag}-2",))
    connection.commit()
    return definition_id


def _route_revision(database, connection, revision_suffix, *, exceeds):
    """Raise a revision straight into the database and route it."""
    revision_id = f"BR-E2E-{revision_suffix}"
    connection.execute(
        "INSERT INTO budget_revision (revision_id, wbs_id, budget_head_id, "
        "delta_paise, effective_from, justification, status, created_by) "
        "VALUES (%s,%s,%s,%s,%s,'e2e','DRAFT',%s)",
        (revision_id, DEMO_WBS, DEMO_HEAD, 1_000_000, date(2026, 9, 1), MAKER))
    connection.commit()
    snapshot = {
        "object_type": "BUDGET_REVISION", "object_id": revision_id,
        "entity_id": DEMO_ENTITY, "project_id": DEMO_PROJECT,
        "budget_head_id": DEMO_HEAD, "amount_paise": 1_000_000,
        "exceeds_available": exceeds,
        "affected_cells": [{"wbs_id": DEMO_WBS, "budget_head_id": DEMO_HEAD}],
        "budget_checks": [],
    }
    with database.session(_system_scope()) as session:
        return engine.open_instance(
            session, object_type="BUDGET_REVISION", object_id=revision_id,
            object_version=1, snapshot=snapshot, maker_user_id=MAKER,
            business_date=BUSINESS_DATE)


@pytest.mark.pg
@PG
def test_step_b_the_exception_stage_opens_only_when_the_request_is_over_budget(
        pg_connection, seeded_pg):
    """Step (b), in its literal form, and the control an auditor must see.

    Within budget, the Finance exception gate is recorded SKIPPED **with a
    skip_reason** at routing -- never omitted, because an auditor has to be
    able to see the control that did not fire, and a control that vanishes
    from the record is indistinguishable from one that was never configured.

    Over budget it is NOT skipped: it stands ahead of the object, and the
    instance stays OPEN behind the ordinary gate that precedes it. The stages
    are sequential, so its row is written when its wave opens rather than at
    routing; what matters here is that it was not written off, and that
    neither revision is approved by having been routed.
    """
    _seed_exception_workflow(pg_connection, DEMO_ENTITY)

    over = _route_revision(seeded_pg, pg_connection, "OVER", exceeds=True)
    within = _route_revision(seeded_pg, pg_connection, "WITHIN", exceeds=False)

    def stages(instance_id):
        with seeded_pg.session(_system_scope()) as session:
            return {row[0]: (row[1], row[2]) for row in session.fetchall(
                "SELECT stage_no, status, skip_reason FROM approval_stage_instance "
                "WHERE instance_id = %s", (instance_id,))}

    over_stages = stages(over["instance_id"])
    assert over_stages[1][0] == rules.STAGE_PENDING, (
        f"the over-budget revision's first gate is {over_stages}, so nobody "
        f"has been asked to approve it")
    assert over_stages.get(2, (None,))[0] != rules.STAGE_SKIPPED, (
        f"the over-budget revision skipped its Finance exception gate: "
        f"{over_stages}. Contract 2 admits no route past a control that "
        f"applies.")

    within_stages = stages(within["instance_id"])
    assert within_stages[1][0] == rules.STAGE_PENDING
    assert within_stages[2][0] == rules.STAGE_SKIPPED, (
        f"the within-budget revision did not record its inapplicable "
        f"exception gate as SKIPPED: {within_stages}. A stage that does not "
        f"apply is recorded, not omitted.")
    assert within_stages[2][1], (
        "the skipped exception gate carries no skip_reason. 'This did not "
        "run' without 'because' is not something an auditor can act on.")

    for instance_id in (over["instance_id"], within["instance_id"]):
        with seeded_pg.session(_system_scope()) as session:
            status = session.fetchone(
                "SELECT status FROM approval_instance WHERE instance_id = %s",
                (instance_id,))[0]
        assert status == rules.INST_OPEN, (
            f"{instance_id} routed to {status!r} rather than OPEN")


def _seed_parallel_group_workflow(connection, entity_id, *, conditional_only=False):
    """An ENT-DM1 workflow whose ONE wave is a parallel group of two stages.

    Stage 1 always applies; stage 2 applies only when ``exceeds_available`` is
    true. They share ``parallel_group='PG'``, so ``compute_waves`` puts them in
    a single wave -- and a within-budget revision therefore arrives at
    ``next_wave`` as the mixed wave ``[1, 2]`` with state ``{2: SKIPPED}``,
    which is the shape that used to stall.

    ``conditional_only=True`` makes stage 1 conditional as well, so the same
    within-budget revision produces an ALL-skipped wave. That is the
    fail-closed case, and it must still hold the object EXCEPTION_PENDING: the
    mixed-wave fix must not turn "no stage applies" into an approval.
    """
    tag = uuid.uuid4().hex[:8].upper()
    definition_id = f"APD-E2E-PG-{tag}"
    connection.execute(
        "INSERT INTO approval_definition (definition_id, object_type, code, version, "
        "status, entity_id, effective_from, created_by) "
        "VALUES (%s,'BUDGET_REVISION',%s,1,'ACTIVE',%s,%s,'E2E')",
        (definition_id, f"BREV-PG-{tag}", entity_id, date(2020, 1, 1)))
    connection.execute(
        "INSERT INTO approval_rule (rule_id, definition_id, priority, predicate, "
        "description, created_by) VALUES (%s,%s,5,%s,%s,'E2E')",
        (f"APR-E2E-PG-{tag}", definition_id,
         Jsonb({"all": [{"field": "object_type", "op": "eq",
                         "value": "BUDGET_REVISION"}], "route": "PARALLEL"}),
         "Every budget revision in this entity, priority 5 so it beats the "
         "sequential exception workflow if both are seeded."))

    over_budget = Jsonb({"field": "exceeds_available", "op": "eq", "value": True})
    connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
        "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
        "created_by) VALUES (%s,%s,1,'Controller review','PG','ALL',NULL,%s,"
        "24,NULL,NULL,true,false,'E2E')",
        (f"APS-E2E-PG-{tag}-1", definition_id,
         over_budget if conditional_only else None))
    connection.execute(
        "INSERT INTO approval_stage (stage_id, definition_id, stage_no, name, "
        "parallel_group, quorum_type, quorum_n, applies_when, sla_hours, "
        "escalate_after_hours, escalate_to, allow_delegation, requires_reason, "
        "created_by) VALUES (%s,%s,2,'Finance exception approval','PG','ALL',"
        "NULL,%s,24,NULL,NULL,false,true,'E2E')",
        (f"APS-E2E-PG-{tag}-2", definition_id, over_budget))
    connection.execute(
        "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
        "approver_ref, scope_expr) VALUES (%s,1,'ROLE','Project Finance Controller',NULL)",
        (f"APS-E2E-PG-{tag}-1",))
    connection.execute(
        "INSERT INTO approval_stage_approver (stage_id, ordinal, approver_kind, "
        "approver_ref, scope_expr) VALUES (%s,1,'USER','U-CFO',NULL)",
        (f"APS-E2E-PG-{tag}-2",))
    connection.commit()
    return definition_id


@pytest.mark.pg
@PG
def test_a_parallel_group_with_one_inapplicable_stage_routes_rather_than_stalling(
        pg_connection, seeded_pg):
    """The mixed parallel wave, end to end against a real database.

    ``tests/test_pg_approvals.py`` pins the arithmetic; this pins the
    consequence, which is what actually mattered: a workflow whose parallel
    group contained an ``applies_when`` stage was UNROUTABLE. Every such object
    was held EXCEPTION_PENDING with an approvable stage that nobody had ever
    been asked to act on -- and EXCEPTION_PENDING looks like a configuration
    problem, so the defect presented as somebody else's fault.

    Within budget: stage 2 is SKIPPED at routing, stage 1 must still be opened
    PENDING and the instance must be OPEN.
    """
    _seed_parallel_group_workflow(pg_connection, DEMO_ENTITY)

    routed = _route_revision(seeded_pg, pg_connection, "PGMIX", exceeds=False)

    with seeded_pg.session(_system_scope()) as session:
        status = session.fetchone(
            "SELECT status FROM approval_instance WHERE instance_id = %s",
            (routed["instance_id"],))[0]
        stages = {row[0]: (row[1], row[2]) for row in session.fetchall(
            "SELECT stage_no, status, skip_reason FROM approval_stage_instance "
            "WHERE instance_id = %s", (routed["instance_id"],))}
        assignments = session.fetchall(
            "SELECT s.stage_no, a.assignee_user_id, a.state "
            "FROM approval_assignment a "
            "JOIN approval_stage_instance s "
            "  ON s.stage_instance_id = a.stage_instance_id "
            "WHERE s.instance_id = %s", (routed["instance_id"],))

    assert status == rules.INST_OPEN, (
        f"the instance routed to {status!r}. A parallel group holding one "
        f"applying and one non-applying stage is a routable workflow: the "
        f"applying stage must be opened, not written off as 'every stage was "
        f"skipped'.")
    assert stages[2][0] == rules.STAGE_SKIPPED and stages[2][1], (
        f"the inapplicable member of the group is {stages.get(2)}. It must be "
        f"recorded SKIPPED with a skip_reason, never omitted (Contract 2).")
    assert stages[1][0] == rules.STAGE_PENDING, (
        f"the applying member of the group is {stages.get(1)}, not PENDING. "
        f"An approvable stage that was never opened is the whole defect.")
    assert any(row[0] == 1 and row[2] == rules.ASSIGN_PENDING
               for row in assignments), (
        f"stage 1 is PENDING but carries no PENDING assignment: {assignments}. "
        f"A stage nobody is addressed on cannot meet a quorum, so it would "
        f"stall in a second way.")
    assert not any(row[0] == 2 for row in assignments), (
        f"the SKIPPED stage was given an assignment: {assignments}. A skipped "
        f"control took no assignment and can never be acted on.")


@pytest.mark.pg
@PG
def test_a_parallel_group_whose_every_stage_is_inapplicable_still_fails_closed(
        pg_connection, seeded_pg):
    """The fix must not erode the fail-closed branch.

    With BOTH members of the group conditional and neither applying, there is
    genuinely nothing to approve. That is EXCEPTION_PENDING -- held for an
    administrator, with the reason recorded -- and emphatically not APPROVED.
    A change that made mixed waves open would be worth nothing if it also made
    empty ones auto-approve.
    """
    _seed_parallel_group_workflow(pg_connection, DEMO_ENTITY,
                                   conditional_only=True)

    routed = _route_revision(seeded_pg, pg_connection, "PGNONE", exceeds=False)

    with seeded_pg.session(_system_scope()) as session:
        status = session.fetchone(
            "SELECT status FROM approval_instance WHERE instance_id = %s",
            (routed["instance_id"],))[0]
        stages = {row[0]: row[1] for row in session.fetchall(
            "SELECT stage_no, status FROM approval_stage_instance "
            "WHERE instance_id = %s", (routed["instance_id"],))}

    assert status == rules.INST_EXCEPTION_PENDING, (
        f"a definition whose every stage was skipped left the instance "
        f"{status!r}. 'Nothing applied' must never resolve to an approval, and "
        f"it must not resolve to OPEN either -- an OPEN instance with no "
        f"assignment waits on nobody, forever.")
    assert set(stages.values()) == {rules.STAGE_SKIPPED}, (
        f"stage states {stages}: every stage should be recorded SKIPPED.")


# ==========================================================================
# The HTTP surface, reported rather than assumed
# ==========================================================================


@pytest.mark.pg
@PG
def test_the_decide_route_moves_the_ledger(demo_scenario):
    """Step (c) through the HTTP surface the approval screens actually call.

    Everything else in this module reaches the engine directly, which proves
    the engine and says nothing about the router in front of it. This is the
    one test that asks the application, over HTTP, to approve the demo
    revision -- and then reads the LEDGER, because a 200 whose control cell did
    not move is precisely the defect worth catching.

    The ``xfail`` this carried is gone rather than relaxed. It documented a
    REAL defect -- ``post_decide`` called ``approvals.decide`` with ``actor=``
    and ``correlation_id=`` against a function that takes ``actor_user_id=``
    and, at the time, no ``correlation_id`` at all, so the call raised
    ``TypeError`` and the route answered 500. Both halves of that are now
    fixed: the call passes ``actor_user_id=``, and ``decide`` takes a
    ``correlation_id`` it writes into ``approval_action.outcome``. The whole
    class is held closed statically by
    ``tests/test_approvals_api_seam.py``, which resolves every
    ``_call_engine`` site against the real engine signature and needs no
    database to do it -- which is what makes removing this marker safe rather
    than optimistic.
    """
    s = demo_scenario

    resp = s.decide_over_http(as_who=APPROVER,
                              reason_text="board-approved overrun")

    assert resp.status_code == 200, (
        f"POST /api/approvals/{s.instance_id}/decide as {APPROVER} returned "
        f"{resp.status_code}: {resp.text}")
    assert s.instance_status() == rules.INST_APPROVED, (
        f"the route answered 200 but the instance is {s.instance_status()!r}. "
        f"The ledger did not move.")
    assert len(s.approve_actions()) == 1


@pytest.mark.pg
@PG
def test_an_auditor_cannot_open_the_approval_surface(as_user):
    """D-12, asserted rather than assumed.

    ``approval.read`` is the router's floor and deliberately excludes Auditor.
    This is the behavioural half of the permission assertion above: if the
    exclusion were ever quietly reversed, the allow-list
    ``test_aud_c_006_auditor_is_read_only`` pins would have been widened
    without the decision being taken.

    Asserted as "not 200", not as a specific code: a 403 and a 404 are both
    legitimate answers here -- ``get_instance`` deliberately answers 404 for a
    row a caller may not see -- and pinning one would make this test about the
    error envelope rather than about access.
    """
    resp = as_user("U-AUD").get("/api/approvals/inbox")
    assert resp.status_code != 200, (
        f"an Auditor opened the approval inbox ({resp.status_code}). "
        f"approval.read excludes Auditor by the D-12 decision recorded in "
        f"auth.PERMISSIONS; granting it widens an audit-finding assertion.")
    assert resp.status_code in (401, 403), (
        f"the Auditor was turned away with {resp.status_code}: {resp.text}. "
        f"An authenticated caller lacking a permission is a 403; anything else "
        f"suggests the refusal came from somewhere other than the guard.")
