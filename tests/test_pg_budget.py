"""Tests for `app.backend.pg.budget` -- planning, availability and revisions.

Two layers, per this stream's brief ("prefer a test that runs WITHOUT a
database wherever the logic allows"):

  * Pure logic -- cursor encode/decode, error shape, the migration's trigger
    and constraint text, and the WATCH_PCT/CRITICAL_PCT wiring -- run
    unconditionally, no database required.
  * Live behaviour -- the ORIGINAL immutability trigger, the derive-from-
    budget_line invariant, "a revision creates no spending capacity until
    approved", and the concurrent-approval race -- needs a live PostgreSQL
    and is gated below, exactly like `tests/test_pg_locking.py`.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly -- see
# tests/test_pg_locking.py's module docstring for why this import block is
# copied rather than referenced.
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
import threading  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path as _Path2  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend import domain  # noqa: E402
from app.backend.pg import budget  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

MIGRATION_SQL = (
    _Path2(__file__).resolve().parent.parent / "migrations" / "pg" / "003_budget_planning.sql"
).read_text(encoding="utf-8")


# ============================================================== cursor helpers (pure)
def test_cursor_round_trips():
    encoded = budget._encode_cursor("root.child", "H1")
    assert budget._decode_cursor(encoded) == ("root.child", "H1")


def test_decode_cursor_rejects_garbage():
    with pytest.raises(budget.BudgetServiceError) as excinfo:
        budget._decode_cursor("not-valid-base64!!!")
    assert excinfo.value.code == "INVALID_CURSOR"


# ============================================================== error shape (pure)
def test_budget_service_error_carries_code_status_message():
    exc = budget.BudgetServiceError("SOME_CODE", "some message", status=409)
    assert exc.code == "SOME_CODE"
    assert exc.status == 409
    assert exc.message == "some message"
    assert "SOME_CODE" in str(exc)


def test_err_helper_always_raises():
    with pytest.raises(budget.BudgetServiceError) as excinfo:
        budget._err("X", "y", status=404)
    assert excinfo.value.code == "X"
    assert excinfo.value.status == 404


# ============================================================== configuration wiring (pure)
def test_watch_and_critical_thresholds_come_from_domain_not_literals():
    """WAVE2_CONTRACTS.md: "verdict thresholds come from domain.WATCH_PCT /
    CRITICAL_PCT -- configuration, not literals." Asserts the actual object
    identity, not just an equal value, so a future edit that redefines a
    same-valued literal here instead of importing the real one is caught."""
    assert budget.WATCH_PCT is domain.WATCH_PCT
    assert budget.CRITICAL_PCT is domain.CRITICAL_PCT


# ============================================================== validation without a database (pure)
def test_check_availability_rejects_negative_amount_before_touching_session():
    class ExplodingSession:
        def fetchone(self, *a, **k):
            raise AssertionError("must not query the database for a negative amount")

        def fetchall(self, *a, **k):
            raise AssertionError("must not query the database for a negative amount")

    with pytest.raises(budget.BudgetServiceError) as excinfo:
        budget.check_availability(ExplodingSession(), "W1", "H1", -1)
    assert excinfo.value.code == "NEGATIVE_AMOUNT"


def test_line_row_to_dict_shape():
    row = ("BL-1", "W1", "H1", "ORIGINAL", 1000, date(2026, 1, 1), None,
           "Approved", "because", None, "U-1", 1)
    out = budget._line_row_to_dict(row)
    assert out["budget_line_id"] == "BL-1"
    assert out["kind"] == "ORIGINAL"
    assert out["amount_paise"] == 1000
    assert out["effective_from"] == "2026-01-01"
    assert out["effective_to"] is None
    assert isinstance(out["amount_paise"], int)


# ============================================================== migration text (pure, static)
def test_migration_makes_original_budget_lines_immutable_at_the_database_level():
    """kind='ORIGINAL' rows must be immutable and undeletable AT THE DATABASE
    LEVEL, per this stream's brief -- a CHECK constraint cannot see OLD, so
    this must be a trigger with a WHEN clause gating on OLD.kind."""
    upper = MIGRATION_SQL.upper()
    assert "BEFORE UPDATE" in upper and "BEFORE DELETE" in upper
    assert "WHEN (OLD.KIND = 'ORIGINAL')" in upper
    assert "BUDGET_LINE_ORIGINAL_IMMUTABLE_UPDATE" in upper
    assert "BUDGET_LINE_ORIGINAL_IMMUTABLE_DELETE" in upper


def test_migration_original_lines_must_be_positive_amount():
    assert "CK_BUDGET_LINE_AMOUNT" in MIGRATION_SQL.upper()
    assert "AMOUNT_PAISE > 0" in MIGRATION_SQL.upper()


def test_migration_has_a_rollback_section():
    assert "-- ROLLBACK:" in MIGRATION_SQL


def test_migration_money_columns_are_bigint():
    # Every *_paise COLUMN DECLARATION in this migration must say `bigint`,
    # never `numeric`/`float`/`double precision` -- domain-controls.md,
    # "Money". Comments are stripped first so prose mentioning "amount_paise
    # is signed" etc. cannot be mistaken for a column declaration; only a
    # line beginning with the column name (leading whitespace only) counts.
    import re

    code_only = "\n".join(
        line for line in MIGRATION_SQL.splitlines() if not line.strip().startswith("--")
    )
    for match in re.finditer(r"^\s*(\w+_paise)\s+(\w+)", code_only, re.MULTILINE):
        column, sql_type = match.group(1), match.group(2).lower()
        assert sql_type == "bigint", f"{column} is declared {sql_type}, not bigint"


# ==========================================================================
# Live database
# ==========================================================================
def _seed_minimal_cell(con, *, suffix, budget_paise=0, commitment=0, actual=0, pr_reserved=0):
    """Self-contained minimal fixture: one org/entity/project/wbs/head/cell,
    mirroring the shape `tests/test_pg_locking.py` builds. Returns the ids."""
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    wbs, head = f"W_{suffix}", f"H_{suffix}"
    ex = con.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (head, ent, f"HC_{suffix}", "Head"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')",
       (wbs, prj, wbs, "n", wbs))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
       "VALUES (%s,%s,%s,'t')", (wbs, head, budget_paise))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, commitment_paise, "
       "actual_paise, pr_reserved_paise, updated_by) VALUES (%s,%s,%s,%s,%s,'t')",
       (wbs, head, commitment, actual, pr_reserved))
    con.commit()
    return {"org": org, "entity": ent, "project": prj, "wbs": wbs, "head": head}


@PG
def test_original_budget_line_refuses_update_at_database_level(pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)
    pg_connection.execute(
        "INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, "
        "amount_paise, effective_from, status, created_by, updated_by) "
        "VALUES (%s,%s,%s,'ORIGINAL',1000,'2026-01-01','Approved','t','t')",
        (f"BL_{suffix}", ids["wbs"], ids["head"]))
    pg_connection.commit()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "UPDATE budget_line SET amount_paise = 2000 WHERE budget_line_id = %s",
            (f"BL_{suffix}",))
    pg_connection.rollback()


@PG
def test_original_budget_line_refuses_delete_at_database_level(pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)
    pg_connection.execute(
        "INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, "
        "amount_paise, effective_from, status, created_by, updated_by) "
        "VALUES (%s,%s,%s,'ORIGINAL',1000,'2026-01-01','Approved','t','t')",
        (f"BL_{suffix}", ids["wbs"], ids["head"]))
    pg_connection.commit()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_connection.execute(
            "DELETE FROM budget_line WHERE budget_line_id = %s", (f"BL_{suffix}",))
    pg_connection.rollback()


@PG
def test_revision_budget_line_may_be_updated_and_deleted(pg_connection):
    """Negative control: the trigger's WHEN clause must not over-reach and
    block non-ORIGINAL rows too."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)
    pg_connection.execute(
        "INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, "
        "amount_paise, effective_from, status, created_by, updated_by) "
        "VALUES (%s,%s,%s,'REVISION',500,'2026-01-01','Draft','t','t')",
        (f"BL_{suffix}", ids["wbs"], ids["head"]))
    pg_connection.commit()

    pg_connection.execute(
        "UPDATE budget_line SET amount_paise = 600 WHERE budget_line_id = %s", (f"BL_{suffix}",))
    pg_connection.execute(
        "DELETE FROM budget_line WHERE budget_line_id = %s", (f"BL_{suffix}",))
    pg_connection.commit()


@PG
def test_recompute_cell_is_idempotent(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        line_id = f"BL_{suffix}"
        session.execute(
            "INSERT INTO budget_line (budget_line_id, wbs_id, budget_head_id, kind, "
            "amount_paise, effective_from, status, created_by, updated_by) "
            "VALUES (%s,%s,%s,'ORIGINAL',100000,'2026-01-01','Approved','t','t')",
            (line_id, ids["wbs"], ids["head"]))
        first = budget.recompute_cell(session, ids["wbs"], ids["head"], as_of=date(2026, 6, 1))
        second = budget.recompute_cell(session, ids["wbs"], ids["head"], as_of=date(2026, 6, 1))
    assert first == second
    assert first["budget_paise"] == 100000
    assert first["original_paise"] == 100000


@PG
def test_revision_creates_no_spending_capacity_until_approved(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=100000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        result = budget.create_revision(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"], delta_paise=50000,
            effective_from=date(2026, 1, 1), justification="growth", actor="U-MAKER")
        assert result["status"] == "DRAFT"

        row = session.fetchone(
            "SELECT budget_paise FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (ids["wbs"], ids["head"]))
        assert row[0] == 100000, "a DRAFT revision must not move the control cell's budget"

        approved = budget.approve_revision(session, revision_id=result["revision_id"], actor="U-CHECKER")
        assert approved["status"] == "APPROVED"

        row = session.fetchone(
            "SELECT budget_paise FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (ids["wbs"], ids["head"]))
        assert row[0] == 150000, "approval must move the control cell's budget"


@PG
def test_approve_revision_refuses_self_approval(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)
    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=100000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        result = budget.create_revision(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"], delta_paise=10000,
            effective_from=date(2026, 1, 1), justification="growth", actor="U-MAKER")
        with pytest.raises(budget.BudgetServiceError) as excinfo:
            budget.approve_revision(session, revision_id=result["revision_id"], actor="U-MAKER")
        assert excinfo.value.code == "SELF_APPROVAL_FORBIDDEN"


@PG
def test_transfer_moves_budget_net_zero_between_two_cells(pg_database, pg_connection):
    suffix = uuid.uuid4().hex[:10]
    from_ids = _seed_minimal_cell(pg_connection, suffix=f"{suffix}A")
    to_ids = _seed_minimal_cell(pg_connection, suffix=f"{suffix}B")

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=from_ids["wbs"], budget_head_id=from_ids["head"],
            amount_paise=200000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        budget.record_original(
            session, wbs_id=to_ids["wbs"], budget_head_id=to_ids["head"],
            amount_paise=50000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        created = budget.create_transfer(
            session, from_wbs_id=from_ids["wbs"], from_head_id=from_ids["head"],
            to_wbs_id=to_ids["wbs"], to_head_id=to_ids["head"], amount_paise=70000,
            effective_from=date(2026, 1, 1), justification="rebalance", actor="U-MAKER")
        assert created["status"] == "DRAFT"

        approved = budget.approve_transfer(session, transfer_id=created["transfer_id"], actor="U-CHECKER")
        assert approved["status"] == "APPROVED"

        from_row = session.fetchone(
            "SELECT budget_paise FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (from_ids["wbs"], from_ids["head"]))
        to_row = session.fetchone(
            "SELECT budget_paise FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (to_ids["wbs"], to_ids["head"]))
        assert from_row[0] == 130000    # 200000 - 70000
        assert to_row[0] == 120000      # 50000 + 70000


@PG
def test_approve_transfer_raises_budget_moved_when_source_exposure_grew(pg_database, pg_connection):
    """Approval does not freeze availability: if spend accrues against the
    source cell between drafting and approving a transfer, approval must
    re-check and refuse rather than push the cell negative."""
    suffix = uuid.uuid4().hex[:10]
    from_ids = _seed_minimal_cell(pg_connection, suffix=f"{suffix}A")
    to_ids = _seed_minimal_cell(pg_connection, suffix=f"{suffix}B")

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=from_ids["wbs"], budget_head_id=from_ids["head"],
            amount_paise=100000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        created = budget.create_transfer(
            session, from_wbs_id=from_ids["wbs"], from_head_id=from_ids["head"],
            to_wbs_id=to_ids["wbs"], to_head_id=to_ids["head"], amount_paise=90000,
            effective_from=date(2026, 1, 1), justification="rebalance", actor="U-MAKER")

        # Spend accrues against the source cell after the transfer was
        # drafted but before it is approved.
        session.execute(
            "UPDATE budget_ledger_cell SET commitment_paise = 50000 "
            "WHERE wbs_id=%s AND budget_head_id=%s",
            (from_ids["wbs"], from_ids["head"]))

        with pytest.raises(budget.BudgetServiceError) as excinfo:
            budget.approve_transfer(session, transfer_id=created["transfer_id"], actor="U-CHECKER")
        assert excinfo.value.code == "BUDGET_MOVED"


@PG
def test_check_availability_invariants(pg_database, pg_connection):
    """available == budget - actual - commitment - reservation;
    exposure == commitment + actual + reservation -- asserted against a real,
    ltree-scanned subtree rollup, not just the pure-python formula."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(
        pg_connection, suffix=suffix, budget_paise=0, commitment=40000, actual=20000, pr_reserved=10000)
    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=100000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        result = budget.check_availability(session, ids["wbs"], ids["head"], 1000)

    assert result["exposure_paise"] == 40000 + 20000 + 10000
    assert result["available_paise"] == result["budget_paise"] - result["exposure_paise"]
    assert result["available_paise"] == 100000 - (40000 + 20000 + 10000)
    assert isinstance(result["available_paise"], int)
    assert isinstance(result["exposure_paise"], int)


@PG
def test_check_availability_rolls_up_the_whole_subtree_not_just_the_owner(pg_database, pg_connection):
    """rollup == own + sum(children): a child cell's exposure must count
    toward the owning ancestor's availability even though the child itself
    owns no budget."""
    suffix = uuid.uuid4().hex[:10]
    org, ent, prj = f"O_{suffix}", f"E_{suffix}", f"P_{suffix}"
    root, child, head = f"R_{suffix}", f"C_{suffix}", f"H_{suffix}"
    ex = pg_connection.execute
    ex("INSERT INTO organisation (organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,'t','t')", (org, f"OC_{suffix}", "Org"))
    ex("INSERT INTO entity (entity_id, organisation_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (ent, org, f"EC_{suffix}", "Entity"))
    ex("INSERT INTO project (project_id, entity_id, capex_code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (prj, ent, f"C_{suffix}", "Project"))
    ex("INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by) "
       "VALUES (%s,%s,%s,%s,'t','t')", (head, ent, f"HC_{suffix}", "Head"))
    ex("INSERT INTO wbs_element (wbs_id, project_id, wbs_code, description, wbs_path, "
       "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,'t','t')", (root, prj, root, "root", root))
    ex("INSERT INTO wbs_element (wbs_id, project_id, parent_wbs_id, wbs_code, description, "
       "wbs_path, created_by, updated_by) VALUES (%s,%s,%s,%s,%s,%s,'t','t')",
       (child, prj, root, child, "child", f"{root}.{child}"))
    ex("INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by) "
       "VALUES (%s,%s,100000,'t'), (%s,%s,0,'t')", (root, head, child, head))
    ex("INSERT INTO budget_ledger_cell (wbs_id, budget_head_id, commitment_paise, updated_by) "
       "VALUES (%s,%s,0,'t'), (%s,%s,30000,'t')", (root, head, child, head))
    pg_connection.commit()

    with pg_database.session(Scope.system()) as session:
        result = budget.check_availability(session, child, head, 1000)

    assert result["owning_wbs_id"] == root
    assert result["exposure_paise"] == 30000, "the child's own exposure must roll up to the owner"
    assert result["available_paise"] == 100000 - 30000


@PG
def test_concurrent_revision_approvals_at_60_percent_each_exactly_one_succeeds(pg_database, pg_connection):
    """The domain race this stream's DoD names explicitly: two revisions,
    each cutting 60% of a cell's available budget, approved concurrently.
    Exactly one may succeed; the other must observe the shifted availability
    and raise BUDGET_MOVED, never silently over-commit."""
    suffix = uuid.uuid4().hex[:10]
    ids = _seed_minimal_cell(pg_connection, suffix=suffix)

    with pg_database.session(Scope.system()) as session:
        budget.record_original(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"],
            amount_paise=10000, effective_from=date(2026, 1, 1), actor="U-MAKER")
        rev_a = budget.create_revision(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"], delta_paise=-6000,
            effective_from=date(2026, 1, 1), justification="cut A", actor="U-MAKER-A")
        rev_b = budget.create_revision(
            session, wbs_id=ids["wbs"], budget_head_id=ids["head"], delta_paise=-6000,
            effective_from=date(2026, 1, 1), justification="cut B", actor="U-MAKER-B")

    results: dict[str, object] = {}
    first_locked = threading.Event()
    release_first = threading.Event()

    def approve_a():
        with pg_database.session(Scope.system()) as session:
            # Peek the lock before the real call so the second thread can be
            # released only once the first genuinely holds it.
            from app.backend.pg.locking import lock_affected_cells
            lock_affected_cells(session, [(ids["wbs"], ids["head"])])
            first_locked.set()
            release_first.wait(timeout=5)
            try:
                results["a"] = budget.approve_revision(
                    session, revision_id=rev_a["revision_id"], actor="U-CHECKER")
            except budget.BudgetServiceError as exc:
                results["a"] = exc

    def approve_b():
        first_locked.wait(timeout=5)
        with pg_database.session(Scope.system()) as session:
            try:
                results["b"] = budget.approve_revision(
                    session, revision_id=rev_b["revision_id"], actor="U-CHECKER")
            except budget.BudgetServiceError as exc:
                results["b"] = exc

    t_a = threading.Thread(target=approve_a)
    t_b = threading.Thread(target=approve_b)
    t_a.start()
    t_a.join(timeout=0.5)  # let it acquire and start waiting on release_first
    t_b.start()
    import time
    time.sleep(0.3)
    release_first.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    outcomes = [results.get("a"), results.get("b")]
    successes = [o for o in outcomes if isinstance(o, dict) and o.get("status") == "APPROVED"]
    failures = [o for o in outcomes if isinstance(o, budget.BudgetServiceError)]
    assert len(successes) == 1, f"expected exactly one success, got {outcomes}"
    assert len(failures) == 1
    assert failures[0].code == "BUDGET_MOVED"

    with pg_database.session(Scope.system()) as session:
        row = session.fetchone(
            "SELECT budget_paise FROM budget_control_cell WHERE wbs_id=%s AND budget_head_id=%s",
            (ids["wbs"], ids["head"]))
    assert row[0] == 4000, "exactly one cut of 6000 from an original of 10000"
