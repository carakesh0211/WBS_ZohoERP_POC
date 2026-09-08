"""The export must not become a scope-escalation path.

`docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md` section 10.3 states the
property and names this file:

    "SVC-EXPORT runs an export under the requesting user's scope, never its
     own. The requester's Scope is serialised onto the job and rehydrated by
     the worker, and tests/test_export_scope.py asserts an export produced for
     a plant-scoped user contains no out-of-scope row."

WHAT IS PROVED HERE, AND WHERE
==============================

Split the way `tests/test_pg_reservations.py` is, and for the same reason. The
first half runs with NO DATABASE, because those properties are properties of
`app/backend/pg/exports.py`'s source and of pure functions in it, and a check
whose only coverage is an environment nobody runs locally is a check nobody
runs. The second half carries `@pytest.mark.pg` plus a `skipif` on
`CAPEX_DB_URL`, SKIPS on every workstation here, and FIRST EXECUTES IN CI's
`pg_tests` job.

A SKIP IS NOT A PASS. The shared skip reason says so, because "n skipped" read
as "n fine" is exactly how a live-only guard rots.

THE FOUR ESCALATION ROUTES, AND THE TEST THAT CLOSES EACH
=========================================================

1. **The worker runs as itself.** Closed by
   `test_a_plant_scoped_users_export_contains_no_out_of_scope_row`: the export
   is produced by `exports.advance_job`, which resolves nothing from the
   caller, and the file contains chain A and not chain B.

2. **The stored scope is edited.** Closed twice, because the two attackers are
   different. `test_a_tampered_scope_is_refused_before_a_row_is_read` covers
   the one who rewrites `scope_json` and does not know about the digest.
   `test_the_migration_trigger_refuses_to_update_a_captured_scope` covers the
   one who tries it through an ordinary UPDATE.

3. **The stored scope is edited AND the digest recomputed.** The digest is
   unkeyed, so this attacker beats it. Closed by
   `test_a_widened_scope_with_a_matching_digest_still_reads_nothing_extra`:
   the meet with the requester's live grants narrows it straight back.

4. **The worker's own session reads business data.** Closed by
   `test_the_service_scope_compiles_to_false_against_every_dataset` and by
   `test_the_registry_override_is_used_at_exactly_three_call_sites`, which
   reads the module's source rather than trusting a comment.

Plus the case that is not an attack at all and matters more often: a requester
whose grants were revoked between queueing and running. That is a PERMISSION
state and is reported as one -- `test_a_requester_whose_grants_were_revoked_
fails_rather_than_exporting_nothing`.
"""
from __future__ import annotations

import ast
import itertools
import json
import os
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

# Fixtures come from tests/conftest_pg.py, imported explicitly -- conftest_pg
# is deliberately not auto-discovered, so its fixtures must be imported by name
# into this module's namespace.
from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)

import pytest  # noqa: E402

from app.backend.pg import exports as export_svc  # noqa: E402
from app.backend.pg import principal_scope, repo  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
_EXPORTS_SOURCE = (ROOT / "app" / "backend" / "pg"
                   / "exports.py").read_text(encoding="utf-8")

#: The one skip reason, so a skipped run reads as a SKIP rather than as a pass.
PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS -- these are the only checks "
            "that produce an actual export and inspect what is in it."),
)


# =========================================================================
# The meet: it can only ever narrow
# =========================================================================
#: Every shape a single dimension can take, as the lattice sees it.
_DIMENSION_VALUES = (None, frozenset(), frozenset({"X"}), frozenset({"X", "Y"}),
                     frozenset({"Y"}), frozenset({"Z"}))


def _permits(values, candidate: str) -> bool:
    """Would a scope with this dimension value admit a row carrying
    `candidate`? `None` is unrestricted, a set is membership."""
    return True if values is None else candidate in values


@pytest.mark.parametrize("left,right",
                         list(itertools.product(_DIMENSION_VALUES, repeat=2)),
                         ids=lambda v: "ALL" if v is None else "-".join(sorted(v)) or "NONE")
def test_the_meet_of_two_dimensions_admits_no_row_either_side_would_refuse(left, right):
    """The whole property, exhaustively over the lattice rather than by
    inspection: for every candidate id, the meet admits it only if BOTH sides
    would have.

    This is the assertion that makes the unkeyed digest survivable. If a meet
    could ever admit a row one side refused, a widened `scope_json` would be
    exportable and the digest would be the only thing standing in the way.
    """
    a = Scope("U", entity_ids=left, plant_ids=None, project_ids=None,
              location_ids=None)
    b = Scope("U", entity_ids=right, plant_ids=None, project_ids=None,
              location_ids=None)
    met = export_svc.meet_scopes(a, b).entity_ids
    for candidate in ("X", "Y", "Z", "W"):
        assert _permits(met, candidate) == (_permits(left, candidate)
                                            and _permits(right, candidate)), (
            f"meet({left}, {right}) disagrees on {candidate!r}")


def test_the_meet_never_turns_read_all_on():
    """`read_all` short-circuits `compile_scope` to TRUE before it looks at a
    dimension, so a meet that ORed it would hand an attacker the whole estate
    from one side alone."""
    on = Scope("U", read_all=True)
    off = Scope("U", read_all=False)
    assert export_svc.meet_scopes(on, off).read_all is False
    assert export_svc.meet_scopes(off, on).read_all is False
    assert export_svc.meet_scopes(off, off).read_all is False
    assert export_svc.meet_scopes(on, on).read_all is True


def test_the_meet_never_turns_triage_unattributed_on():
    """Its own flag for its own reason (`engine.Scope`); it must not leak
    across a meet either."""
    on = Scope("U", triage_unattributed=True)
    off = Scope("U", triage_unattributed=False)
    assert export_svc.meet_scopes(on, off).triage_unattributed is False
    assert export_svc.meet_scopes(off, on).triage_unattributed is False


def test_the_meet_with_a_denied_scope_is_denied():
    """The revocation case: whatever was captured, a requester who may now see
    nothing exports nothing."""
    wide = Scope("U", entity_ids=None, plant_ids=None, project_ids=None,
                 location_ids=None, read_all=True)
    met = export_svc.meet_scopes(wide, principal_scope.denied_scope("U"))
    assert principal_scope.is_denied(met)
    assert repo.compile_scope(met, export_svc.BUDGET_LEDGER_CELLS.scope_columns) \
        == ("FALSE", {})


def test_the_meet_keeps_the_captured_requester_not_the_live_one():
    """The meet answers "what may this JOB read". Its requester is a fact about
    the job; letting the live side rewrite it would let a resolution for
    somebody else re-attribute the export."""
    captured = Scope("U-CAPTURED", principal_kind="USER")
    live = Scope("U-SOMEBODY-ELSE", principal_kind="SERVICE")
    met = export_svc.meet_scopes(captured, live)
    assert met.user_id == "U-CAPTURED"
    assert met.principal_kind == "USER"


# =========================================================================
# Capture: the three states survive JSON, and the digest binds to the job
# =========================================================================
@pytest.mark.parametrize("values,expected", [
    (None, None),
    (frozenset(), []),
    (frozenset({"B", "A"}), ["A", "B"]),
])
def test_the_three_scope_states_survive_serialisation(values, expected):
    """`None` is unrestricted, `[]` is NOTHING, and they must never collapse.

    A JSON round trip is exactly where a careless `or None` reintroduces the
    inversion the whole scope stack exists to prevent -- `[] or None` is
    `None`, and "no grants" would silently become "all rows".
    """
    scope = Scope("U", entity_ids=values, plant_ids=None, project_ids=None,
                  location_ids=None)
    doc = export_svc.serialise_scope(scope)
    assert doc["entity_ids"] == expected
    assert export_svc.deserialise_scope(doc).entity_ids == values


def test_serialisation_sorts_ids_so_the_digest_is_a_function_of_the_scope():
    """Two Scopes with the same ids must produce the same bytes, or the digest
    is a function of set iteration order and fails at random."""
    one = Scope("U", entity_ids=frozenset({"A", "B", "C"}), plant_ids=None,
                project_ids=None, location_ids=None)
    two = Scope("U", entity_ids=frozenset({"C", "A", "B"}), plant_ids=None,
                project_ids=None, location_ids=None)
    assert (export_svc.canonical_scope_bytes(export_svc.serialise_scope(one))
            == export_svc.canonical_scope_bytes(export_svc.serialise_scope(two)))


def test_the_digest_binds_the_scope_to_ITS_job():
    """Without the job id inside the digest, a wide scope lifted from one job
    verifies perfectly on another -- so any user holding one wide export could
    paste its authorisation onto a narrow one."""
    doc = export_svc.serialise_scope(
        Scope("U", entity_ids=frozenset({"E1"}), plant_ids=None,
              project_ids=None, location_ids=None))
    assert (export_svc.scope_digest("EXP-A", doc)
            != export_svc.scope_digest("EXP-B", doc))
    export_svc.verify_scope_digest("EXP-A", doc,
                                   export_svc.scope_digest("EXP-A", doc))
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.verify_scope_digest("EXP-B", doc,
                                       export_svc.scope_digest("EXP-A", doc))
    assert caught.value.code == "SCOPE_DIGEST_MISMATCH"


def test_a_widened_scope_changes_the_digest():
    """The detection the digest actually provides, stated as a test."""
    narrow = export_svc.serialise_scope(
        Scope("U", entity_ids=None, plant_ids=frozenset({"PL-A"}),
              project_ids=None, location_ids=None))
    widened = dict(narrow, plant_ids=["PL-A", "PL-B"])
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.verify_scope_digest("EXP-1", widened,
                                       export_svc.scope_digest("EXP-1", narrow))
    assert caught.value.code == "SCOPE_DIGEST_MISMATCH"


@pytest.mark.parametrize("doc,code", [
    ({"version": 99, "user_id": "U", "principal_kind": "USER",
      "entity_ids": None, "plant_ids": None, "project_ids": None,
      "location_ids": None}, "SCOPE_VERSION_UNSUPPORTED"),
    ({"version": 1, "user_id": "", "principal_kind": "USER",
      "entity_ids": None, "plant_ids": None, "project_ids": None,
      "location_ids": None}, "SCOPE_UNREADABLE"),
    # A dimension OMITTED is not a dimension that is unrestricted.
    ({"version": 1, "user_id": "U", "principal_kind": "USER",
      "plant_ids": None, "project_ids": None,
      "location_ids": None}, "SCOPE_UNREADABLE"),
    # A bare string would become a frozenset of its CHARACTERS.
    ({"version": 1, "user_id": "U", "principal_kind": "USER",
      "entity_ids": "ENT-1", "plant_ids": None, "project_ids": None,
      "location_ids": None}, "SCOPE_UNREADABLE"),
])
def test_an_unreadable_stored_scope_is_refused_not_guessed_at(doc, code):
    """Every rejection raises rather than returning a partially-read scope. A
    scope we cannot read is a scope we must not widen."""
    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.deserialise_scope(doc)
    assert caught.value.code == code


def test_a_truthy_non_bool_read_all_is_not_read_all():
    """`read_all` must be the literal `true`. A JSON type confusion producing
    `1` or `"yes"` must not become an unrestricted read."""
    base = export_svc.serialise_scope(
        Scope("U", entity_ids=frozenset({"E"}), plant_ids=None,
              project_ids=None, location_ids=None))
    for truthy in (1, "true", "yes", [1]):
        assert export_svc.deserialise_scope(dict(base, read_all=truthy)).read_all \
            is False
    assert export_svc.deserialise_scope(dict(base, read_all=True)).read_all is True


# =========================================================================
# The worker's own scope
# =========================================================================
@pytest.mark.parametrize("dataset", sorted(export_svc.DATASETS),
                         ids=lambda n: n)
def test_the_service_scope_compiles_to_false_against_every_dataset(dataset):
    """SVC-EXPORT's own session can read no business row, on any dataset, by
    construction -- not because the worker is careful."""
    columns = export_svc.DATASETS[dataset].scope_columns
    assert repo.compile_scope(export_svc.service_scope(), columns) == ("FALSE", {})


def test_the_service_scope_is_false_even_where_every_dimension_is_waived():
    """`JOB_SCOPE_COLUMNS` waives all four, and a waiver must not resurrect a
    denied scope. `compile_scope` checks emptiness BEFORE it honours a waiver
    precisely so this holds."""
    assert repo.compile_scope(export_svc.service_scope(),
                              export_svc.JOB_SCOPE_COLUMNS) == ("FALSE", {})


def test_the_registry_override_never_sets_read_all():
    """`read_all` short-circuits `compile_scope` to TRUE before any dimension
    is examined. The override is passed at three call sites; `read_all` on it
    would make those three unconditional rather than merely wide."""
    assert export_svc.job_registry_scope().read_all is False
    assert export_svc.job_registry_scope().principal_kind == "SERVICE"
    assert export_svc.job_registry_scope().user_id == export_svc.SERVICE_USER_ID


#: The functions permitted to use the wide override, and what each does with
#: it. Every one of them touches `export_job` / `export_job_chunk` and nothing
#: else. Named rather than counted: a count tells a reader a number changed,
#: this tells them WHICH function acquired the privilege.
_REGISTRY_OVERRIDE_CALLERS = {
    "_claim_candidates":         "reads job rows to find due work",
    "_fail_under_service_scope": "fails a job whose requester can no longer "
                                 "read its own row",
    "advance_job":               "reads the job row before rehydrating its scope",
    "expire_due":                "ages out finished jobs estate-wide",
}


def test_the_registry_override_is_used_only_where_it_is_declared_to_be():
    """Read from the SOURCE, because "it is only used for job rows" is a claim
    a comment can make and a reviewer cannot check.

    The override is wide -- it compiles to TRUE against a business dataset's
    column mapping too -- so what makes it safe is that it reaches only
    statements over `export_job`. If a new function starts using it, this test
    fails and somebody has to add it above with a reason.
    """
    tree = ast.parse(_EXPORTS_SOURCE)
    callers: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name == "job_registry_scope":
            continue          # its own definition, not a use
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "job_registry_scope"):
                callers.add(function.name)
    assert callers == set(_REGISTRY_OVERRIDE_CALLERS), (
        f"job_registry_scope() is used by {sorted(callers)}; the declared set "
        f"is {sorted(_REGISTRY_OVERRIDE_CALLERS)}. It compiles to TRUE against "
        f"a business dataset's column mapping, so every user of it must be "
        f"listed with a reason -- it is safe only while it reaches statements "
        f"over export_job alone.")


def test_the_worker_entry_points_open_their_session_with_the_requesters_scope():
    """Source-level: `advance_job` must hand `database.session` the REHYDRATED
    scope, never `service_scope()`.

    Structural, from the AST rather than a substring search, so a comment
    mentioning `service_scope` cannot satisfy it and a rename cannot silently
    pass it.
    """
    tree = ast.parse(_EXPORTS_SOURCE)
    advance = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "advance_job")
    session_calls = [
        node for node in ast.walk(advance)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "session"
    ]
    assert session_calls, "advance_job opens no session at all"
    used = set()
    for call in session_calls:
        assert call.args, "database.session() called with no scope"
        arg = call.args[0]
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            used.add(arg.func.id)
        elif isinstance(arg, ast.Name):
            used.add(arg.id)
    assert "scope" in used, (
        "advance_job must open the working session with the rehydrated "
        f"requester scope; it opened sessions with {sorted(used)}")


def test_no_dataset_waives_a_scope_dimension():
    """An export is the widest read in the product and the one where a waived
    dimension would be least visible in the output. Every dataset names all
    four with a real column."""
    for name, dataset in sorted(export_svc.DATASETS.items()):
        assert set(dataset.scope_columns) == {"entity", "plant", "location",
                                              "project"}, name
        waived = [d for d, c in dataset.scope_columns.items() if c is None]
        assert waived == [], f"{name} waives {waived}"


def test_every_dataset_query_carries_the_scope_token():
    """`repo.query` refuses SQL without a literal `{scope}` token, so this is
    belt and braces -- but the token must sit in the WHERE clause that actually
    filters, and a token in a comment would satisfy the refusal while filtering
    nothing."""
    assert "WHERE {' AND '.join(where) if where else 'TRUE'} AND {{scope}}" \
        in _EXPORTS_SOURCE, (
        "the dataset page query no longer places {scope} in its WHERE clause")


# =========================================================================
# LIVE: the export a plant-scoped user actually gets
# =========================================================================
_ORG = "ORG-EXP"
_ENT_A, _ENT_B = "ENT-EXP-A", "ENT-EXP-B"
_PLT_A, _PLT_B = "PL-EXP-A", "PL-EXP-B"
_LOC_A, _LOC_B = "LOC-EXP-A", "LOC-EXP-B"
_PRJ_A, _PRJ_B = "PRJ-EXP-A", "PRJ-EXP-B"
_WBS_A, _WBS_B = "WBS-EXP-A", "WBS-EXP-B"
_BH_A, _BH_B = "BH-EXP-A", "BH-EXP-B"
_USER_PLANT_A = "U-EXP-PLANT-A"
_USER_WIDE = "U-EXP-WIDE"


def _seed(session) -> None:
    """Two independent entity/plant/location/project/WBS chains and one budget
    cell in each, plus two users: one restricted to plant A, one unrestricted.

    Two chains rather than one, because "the export contained chain A" proves
    nothing on its own -- the assertion that matters is that chain B EXISTS and
    is absent.
    """
    ex = session.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, "
       "updated_by) VALUES (%s, 'EXP', 'Export Org', 'TEST', 'TEST')", (_ORG,))
    for ent in (_ENT_A, _ENT_B):
        ex("INSERT INTO entity (entity_id, organisation_id, code, name, "
           "created_by, updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')",
           (ent, _ORG, ent, ent))
    for plant, ent in ((_PLT_A, _ENT_A), (_PLT_B, _ENT_B)):
        ex("INSERT INTO plant (plant_id, entity_id, code, name, created_by, "
           "updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')",
           (plant, ent, plant, plant))
    for loc, ent, plant in ((_LOC_A, _ENT_A, _PLT_A), (_LOC_B, _ENT_B, _PLT_B)):
        ex("INSERT INTO location (location_id, entity_id, plant_id, code, name, "
           "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (loc, ent, plant, loc, loc))
    for prj, ent, plant, loc in ((_PRJ_A, _ENT_A, _PLT_A, _LOC_A),
                                 (_PRJ_B, _ENT_B, _PLT_B, _LOC_B)):
        ex("INSERT INTO project (project_id, entity_id, plant_id, location_id, "
           "capex_code, name, created_by, updated_by) "
           "VALUES (%s, %s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (prj, ent, plant, loc, prj, prj))
    for wbs, prj in ((_WBS_A, _PRJ_A), (_WBS_B, _PRJ_B)):
        ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, "
           "wbs_path, created_by, updated_by) "
           "VALUES (%s, %s, %s, %s, %s, 'TEST', 'TEST')",
           (wbs, prj, wbs, f"{wbs} description",
            wbs.lower().replace("-", "_")))
    for head, ent in ((_BH_A, _ENT_A), (_BH_B, _ENT_B)):
        ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, "
           "created_by, updated_by) VALUES (%s, %s, %s, %s, 'TEST', 'TEST')",
           (head, ent, head, head))
    for wbs, head, budget in ((_WBS_A, _BH_A, 100_000), (_WBS_B, _BH_B, 250_000)):
        ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, "
           "budget_paise, updated_by) VALUES (%s, %s, %s, 'TEST')",
           (wbs, head, budget))
        ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, "
           "commitment_paise, actual_paise, pr_reserved_paise, updated_by) "
           "VALUES (%s, %s, 1000, 2000, 500, 'TEST')", (wbs, head))

    for user in (_USER_PLANT_A, _USER_WIDE):
        ex("INSERT INTO app_user (user_id, email, display_name, created_by, "
           "updated_by) VALUES (%s, %s, %s, 'TEST', 'TEST')",
           (user, f"{user}@example.test", user))
    # Restricted on PLANT only: entity, project and location stay unrestricted,
    # so the ONLY thing keeping chain B out of this user's export is the plant
    # predicate. That makes the assertion below specific rather than
    # over-determined by four overlapping restrictions.
    ex("INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
       "VALUES (%s, 'plant', 'TEST')", (_USER_PLANT_A,))
    ex("INSERT INTO user_scope_grant (user_id, dimension, scope_value, "
       "granted_by) VALUES (%s, 'plant', %s, 'TEST')", (_USER_PLANT_A, _PLT_A))


def _run_to_completion(database, job_id, *, limit: int = 20) -> dict:
    outcome: dict = {}
    for _ in range(limit):
        outcome = export_svc.advance_job(database, job_id, rows_budget=1000)
        if not outcome.get("more"):
            return outcome
    raise AssertionError(f"export {job_id} did not finish in {limit} invocations")


def _queue(database, user_id: str, *, dataset: str = "budget_ledger_cells",
           filters=None) -> str:
    scope = principal_scope.scope_for_request(database, {"user_id": user_id})
    with database.session(scope) as session:
        job = export_svc.create_job(session, dataset=dataset, filters=filters,
                                    scope=scope, requested_by=user_id,
                                    chunk_rows=2)
    return job["export_job_id"]


@PG
def test_a_plant_scoped_users_export_contains_no_out_of_scope_row(pg_database):
    """The assertion the plan names this file for.

    Chain B exists, carries a different budget, and is exported for the
    unrestricted user -- so its absence from the plant-scoped user's file is
    the scope working, not the fixture being empty.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)

    narrow_job = _queue(pg_database, _USER_PLANT_A)
    wide_job = _queue(pg_database, _USER_WIDE)
    assert _run_to_completion(pg_database, narrow_job)["state"] == "SUCCEEDED"
    assert _run_to_completion(pg_database, wide_job)["state"] == "SUCCEEDED"

    def _read(user, job_id):
        scope = principal_scope.scope_for_request(pg_database, {"user_id": user})
        with pg_database.session(scope) as session:
            body, _meta = export_svc.read_result(session, job_id, requester=user)
        return body

    narrow = _read(_USER_PLANT_A, narrow_job)
    wide = _read(_USER_WIDE, wide_job)

    # The out-of-scope chain is real: it is in the unrestricted export.
    assert _WBS_B in wide and _PLT_B in wide and "2500.00" in wide
    # ...and wholly absent from the plant-scoped one.
    assert _WBS_A in narrow and _PLT_A in narrow
    for out_of_scope in (_WBS_B, _PLT_B, _ENT_B, _PRJ_B, _LOC_B, _BH_B):
        assert out_of_scope not in narrow, (
            f"{out_of_scope} reached a plant-scoped user's export")
    # The absence is of ROWS, not of an error: the file has a header and one
    # data row, so "empty because it broke" is excluded.
    assert len(narrow.strip().splitlines()) == 2


@PG
def test_a_tampered_scope_is_refused_before_a_row_is_read(pg_database):
    """Route 2: `scope_json` rewritten by somebody who does not recompute the
    digest.

    The row is DELETED and re-inserted rather than UPDATEd, deliberately: an
    UPDATE is what migration 017's trigger refuses, and this test is about the
    attacker who goes around the trigger. Nothing is read and no chunk exists.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, _USER_PLANT_A)

    with pg_database.session(Scope.system()) as session:
        row = session.fetchone(
            "SELECT scope_json, scope_digest, dataset, column_order, "
            "filter_json, expires_at FROM export_job WHERE export_job_id = %s",
            (job_id,))
        scope_doc, digest, dataset, column_order, filter_json, expires = row
        widened = dict(scope_doc, plant_ids=[_PLT_A, _PLT_B])
        session.execute("DELETE FROM export_job WHERE export_job_id = %s",
                        (job_id,))
        session.execute(
            "INSERT INTO export_job (export_job_id, dataset, output_format, "
            "state, requested_by, scope_json, scope_digest, filter_json, "
            "column_order, expires_at) VALUES (%s, %s, 'csv', 'QUEUED', %s, "
            "%s, %s, %s, %s, %s)",
            (job_id, dataset, _USER_PLANT_A, json.dumps(widened), digest,
             json.dumps(filter_json), column_order, expires))

    with pytest.raises(export_svc.ExportError) as caught:
        export_svc.advance_job(pg_database, job_id)
    assert caught.value.code == "SCOPE_DIGEST_MISMATCH"

    with pg_database.session(Scope.system()) as session:
        state, code = session.fetchone(
            "SELECT state, error_code FROM export_job WHERE export_job_id = %s",
            (job_id,))
        chunks = session.fetchone(
            "SELECT COUNT(*) FROM export_job_chunk WHERE export_job_id = %s",
            (job_id,))[0]
    assert state == "FAILED"
    assert code == "SCOPE_DIGEST_MISMATCH"
    assert chunks == 0, "a refused job must not have read or written anything"


@PG
def test_a_widened_scope_with_a_matching_digest_still_reads_nothing_extra(
        pg_database):
    """Route 3, and the one the digest cannot close.

    The digest is unkeyed, so an attacker who can rewrite the row can also
    recompute it -- this test gives them that. What stops them is the MEET with
    the requester's live grants, which narrows the widened scope straight back
    to plant A.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, _USER_PLANT_A)

    with pg_database.session(Scope.system()) as session:
        row = session.fetchone(
            "SELECT scope_json, dataset, column_order, filter_json, expires_at "
            "FROM export_job WHERE export_job_id = %s", (job_id,))
        scope_doc, dataset, column_order, filter_json, expires = row
        widened = dict(scope_doc, plant_ids=[_PLT_A, _PLT_B],
                       entity_ids=None, project_ids=None, location_ids=None)
        # The attacker recomputes the digest over their own document.
        forged = export_svc.scope_digest(job_id, widened)
        session.execute("DELETE FROM export_job WHERE export_job_id = %s",
                        (job_id,))
        session.execute(
            "INSERT INTO export_job (export_job_id, dataset, output_format, "
            "state, requested_by, scope_json, scope_digest, filter_json, "
            "column_order, chunk_rows, expires_at) VALUES (%s, %s, 'csv', "
            "'QUEUED', %s, %s, %s, %s, %s, 2, %s)",
            (job_id, dataset, _USER_PLANT_A, json.dumps(widened), forged,
             json.dumps(filter_json), column_order, expires))

    # The digest verifies -- the forgery worked, as it must for this test to be
    # about the meet rather than about the digest.
    export_svc.verify_scope_digest(job_id, widened, forged)
    assert export_svc.deserialise_scope(widened).plant_ids == frozenset(
        {_PLT_A, _PLT_B})

    assert _run_to_completion(pg_database, job_id)["state"] == "SUCCEEDED"
    scope = principal_scope.scope_for_request(pg_database,
                                              {"user_id": _USER_PLANT_A})
    with pg_database.session(scope) as session:
        body, _meta = export_svc.read_result(session, job_id,
                                             requester=_USER_PLANT_A)
    for out_of_scope in (_WBS_B, _PLT_B, _ENT_B, _PRJ_B):
        assert out_of_scope not in body, (
            f"a forged scope widened the export to include {out_of_scope}")
    assert _WBS_A in body
    assert len(body.strip().splitlines()) == 2


@PG
def test_the_migration_trigger_refuses_to_update_a_captured_scope(pg_database):
    """Route 2's ordinary form: an UPDATE. 017's
    `trg_export_job_identity_immutable` refuses it, for every captured field.
    """
    import psycopg

    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, _USER_PLANT_A)

    immutable = {
        "scope_json": json.dumps({"version": 1, "user_id": _USER_PLANT_A,
                                  "principal_kind": "USER", "read_all": True,
                                  "triage_unattributed": False,
                                  "entity_ids": None, "plant_ids": None,
                                  "project_ids": None, "location_ids": None}),
        "requested_by": _USER_WIDE,
        "scope_digest": "0" * 64,
        "dataset": "wbs_elements",
    }
    for column, value in immutable.items():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg_database.session(Scope.system()) as session:
                session.execute(
                    f"UPDATE export_job SET {column} = %s "
                    f"WHERE export_job_id = %s", (value, job_id))

    # ...and the mutable half still moves, or the trigger would be a freeze.
    with pg_database.session(Scope.system()) as session:
        session.execute(
            "UPDATE export_job SET rows_written = 7 WHERE export_job_id = %s",
            (job_id,))
        assert session.fetchone(
            "SELECT rows_written FROM export_job WHERE export_job_id = %s",
            (job_id,))[0] == 7


@PG
def test_a_requester_whose_grants_were_revoked_fails_rather_than_exporting_nothing(
        pg_database):
    """Not an attack -- the ordinary case, and the one the three-state rule is
    about.

    An empty CSV handed to a controller who lost their grants says "there is no
    budget". The truth is "you may no longer see it", and the job says so with
    a code.
    """
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, _USER_PLANT_A)

    with pg_database.session(Scope.system()) as session:
        # Restricted on entity with NO grants: the empty-frozenset state, which
        # is "may see rows in zero entities" -- not "unrestricted".
        session.execute(
            "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
            "VALUES (%s, 'entity', 'TEST')", (_USER_PLANT_A,))

    outcome = export_svc.advance_job(pg_database, job_id)
    assert outcome["state"] == "FAILED"
    assert outcome["error_code"] == "REQUESTER_SCOPE_DENIED"

    with pg_database.session(Scope.system()) as session:
        state, code, rows_total, chunks = session.fetchone(
            "SELECT j.state, j.error_code, j.rows_total, "
            "(SELECT COUNT(*) FROM export_job_chunk c "
            " WHERE c.export_job_id = j.export_job_id) "
            "FROM export_job j WHERE j.export_job_id = %s", (job_id,))
    assert (state, code) == ("FAILED", "REQUESTER_SCOPE_DENIED")
    assert chunks == 0
    # NOT a successful export of zero rows: `rows_total` was never set, so
    # nothing anywhere claims this user has no budget.
    assert rows_total is None


@PG
def test_one_users_export_job_is_invisible_to_another(pg_database):
    """An export job is the requester's own artefact. Somebody else's comes
    back as `None` -- the same answer as a job that never existed, because a
    403 on an id confirms the id is real."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    job_id = _queue(pg_database, _USER_PLANT_A)

    other = principal_scope.scope_for_request(pg_database, {"user_id": _USER_WIDE})
    with pg_database.session(other) as session:
        assert export_svc.get_job(session, job_id, requester=_USER_WIDE) is None
        assert export_svc.list_jobs(session, requester=_USER_WIDE)["items"] == []
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.read_result(session, job_id, requester=_USER_WIDE)
    assert caught.value.code == "EXPORT_JOB_NOT_FOUND"
    assert caught.value.status == 404


@PG
def test_a_denied_principal_cannot_even_queue_an_export(pg_database):
    """`create_job` refuses a denied scope rather than queueing a job that
    would export nothing -- again because "nothing" and "not permitted" are
    different statements."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
        session.execute(
            "INSERT INTO app_user (user_id, email, display_name, created_by, "
            "updated_by) VALUES ('U-EXP-NONE', 'none@example.test', 'none', "
            "'TEST', 'TEST')")
        session.execute(
            "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
            "VALUES ('U-EXP-NONE', 'entity', 'TEST')")

    scope = principal_scope.scope_for_request(pg_database,
                                              {"user_id": "U-EXP-NONE"})
    assert principal_scope.is_denied(scope)
    with pg_database.session(scope) as session:
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.create_job(session, dataset="budget_ledger_cells",
                                  filters=None, scope=scope,
                                  requested_by="U-EXP-NONE")
    assert caught.value.code == "SCOPE_DENIED"
    assert caught.value.status == 403


@PG
def test_a_job_cannot_be_created_carrying_somebody_elses_scope(pg_database):
    """The last shape of the escalation, and the cheapest to get wrong: a
    router that resolved one principal's scope and attributed the job to
    another."""
    with pg_database.session(Scope.system()) as session:
        _seed(session)
    wide = principal_scope.scope_for_request(pg_database, {"user_id": _USER_WIDE})
    with pg_database.session(wide) as session:
        with pytest.raises(export_svc.ExportError) as caught:
            export_svc.create_job(session, dataset="budget_ledger_cells",
                                  filters=None, scope=wide,
                                  requested_by=_USER_PLANT_A)
    assert caught.value.code == "SCOPE_ACTOR_MISMATCH"
