"""Every money column on `budget_ledger_cell` has a writer, and it is the
formula `domain.compute_ledger` states.

THE DEFECT THIS FILE EXISTS FOR
===============================

`budget_ledger_cell` carries nine money columns. Three of them
(`original_paise`, `revisions_paise`, `future_budget_paise`) are derived from
`budget_line` by `budget.recompute_cell`. The other six -- `ordered_paise`,
`commitment_paise`, `actual_paise`, `received_paise`,
`received_not_billed_paise` and `pr_reserved_paise` -- are derived from the
procurement chain, and before migration 014 exactly ONE of the six had a writer
anywhere in the PostgreSQL path.

`check_availability` computes

    available = budget - (commitment + actual + pr_reserved)

`commitment_paise` is `max(0, ordered - billed)` and FALLS when a bill arrives.
`actual_paise` should RISE by the same amount and never did, because nothing
wrote it. So a bill landing made AVAILABLE RISE BY THE BILLED AMOUNT and the
same budget could be committed again -- AUD-C-001 re-opened.

It was dormant only because `bill_line` had no PostgreSQL writer at all, so
`billed` was always zero and neither limb moved. Wave 6's inbound path made
`billed` non-zero and ACTIVATED it.

WHY THIS FILE RUNS WITH NO DATABASE
===================================

Every property here is a property of SOURCE TEXT and module constants, and that
is deliberate rather than a compromise. The live proof lives in
`tests/test_pg_migration_014.py`, which skips on every workstation here and
first executes in CI's `pg_tests` job. A defect whose only coverage skips on
the machine the code is written on is a defect nobody sees until CI -- and
"which columns does this UPDATE actually SET" is answerable from the source, so
it is answered here, on every machine, on every run.

The guard that matters most is
`test_every_derived_ledger_money_column_has_a_writer`: it reads the column list
out of `002_budget_control.sql` itself rather than from a list maintained
alongside it, so THE NEXT COLUMN ADDED CANNOT SILENTLY JOIN THE UNWRITTEN SET.
That is the whole reason the register in `DERIVED_LEDGER_COLUMNS` is not enough
on its own: a register and a writer that agree with each other prove nothing if
the schema has moved past both.
"""
from __future__ import annotations

import inspect
import re
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from app.backend import domain  # noqa: E402
from app.backend.pg import budget as budget_svc  # noqa: E402
from app.backend.pg import procurement as ledger  # noqa: E402
from app.backend.pg import procurement_services as svc  # noqa: E402

ROOT = _Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "pg"
_002 = (MIGRATIONS / "002_budget_control.sql").read_text(encoding="utf-8")
_014 = (MIGRATIONS / "014_procurement_corrections.sql").read_text(encoding="utf-8")

_SVC_SOURCE = _Path(svc.__file__).read_text(encoding="utf-8")
_BUDGET_SOURCE = _Path(budget_svc.__file__).read_text(encoding="utf-8")
_LEDGER_SOURCE = _Path(ledger.__file__).read_text(encoding="utf-8")

#: Every module that may legitimately write a `budget_ledger_cell` column,
#: concatenated. A seventh writer appearing somewhere else is caught by
#: `test_exactly_one_statement_writes_the_derived_columns`, not by this.
_ALL_WRITERS = _SVC_SOURCE + "\n" + _BUDGET_SOURCE


def _ledger_money_columns() -> list[str]:
    """Every `*_paise` column `budget_ledger_cell` declares, read from the
    migration rather than from any list this repository maintains.

    THE POINT OF READING THE SCHEMA. A test that walks a hand-kept register and
    checks it against a writer built from the same register agrees with itself
    and detects nothing -- which is precisely how five of six columns went
    unwritten with a green suite.
    """
    body = re.search(
        r"CREATE TABLE budget_ledger_cell \((.*?)\n\);", _002, re.DOTALL)
    assert body, "budget_ledger_cell's DDL has moved; this parser found nothing"
    columns = re.findall(r"^\s*(\w+_paise)\s+bigint", body.group(1), re.MULTILINE)
    assert len(columns) >= 6, (
        f"the DDL parser found only {columns}; it is asserting almost nothing")
    # A later migration may ADD a money column to the same table (034 adds the
    # two internal limbs). Read every migration for that, so the register is
    # still checked against the schema and not against a list of files this
    # test happens to know about.
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for stmt in re.findall(r"ALTER TABLE budget_ledger_cell\s+(.*?);",
                               path.read_text(encoding="utf-8"), re.DOTALL):
            columns.extend(re.findall(r"ADD COLUMN\s+(\w+_paise)\s+bigint", stmt))
    return columns


def _set_columns(statement: str) -> set[str]:
    """The columns an `UPDATE ... SET` statement assigns, ignoring the
    right-hand sides -- which contain the same column names inside their
    subqueries, so a naive substring search would report every column as
    written by every statement."""
    tail = statement.split(" SET ", 1)[1] if " SET " in statement else statement
    return set(re.findall(r"(?:^\s{0,12}|\bSET\s+)(\w+_paise)\s*=", tail,
                          re.MULTILINE))


# =========================================================================
# The guard the whole file exists for
# =========================================================================
def test_every_derived_ledger_money_column_has_a_writer():
    """No `budget_ledger_cell` money column may be left at its DEFAULT 0.

    A column with no writer is not "zero until someone fills it in". It is a
    term in `available = budget - (commitment + actual + pr_reserved)` that
    never moves, and every one of the three that appear in that subtraction is
    permissive when stuck at zero: availability is OVERSTATED and the same
    rupees can be committed twice.

    Reads the column list from `002_budget_control.sql` so the next column
    added cannot silently join the unwritten set.
    """
    # A column is written where it stands at the head of an assignment line
    # OR directly after the `SET` keyword -- `release_carried_commitment`
    # writes `commitment_carried_paise` in the latter position, and a reader
    # that only knew the former reported it unwritten.
    unwritten = [
        column for column in _ledger_money_columns()
        if not re.search(rf"(?:^\s{{0,12}}|\bSET\s+){column}\s*=",
                         _ALL_WRITERS, re.MULTILINE)
    ]
    assert unwritten == [], (
        f"budget_ledger_cell columns with NO writer in pg/budget.py or "
        f"pg/procurement_services.py: {unwritten}. A derived money column that "
        f"nothing writes stays at DEFAULT 0 for ever. For the three that "
        f"appear in `available = budget - (commitment + actual + "
        f"pr_reserved)`, zero OVERSTATES availability -- which is how "
        f"AUD-C-001 was re-opened by `actual_paise` having no writer while "
        f"`commitment_paise` fell on every bill.")


def test_the_six_procurement_derived_columns_are_written_by_one_statement():
    """All six in ONE `UPDATE`, not four in one and two in another.

    Leaving some of six derived columns unwritten is exactly how this defect
    arose, and two statements would recreate it in a subtler form: a window in
    which `commitment` has fallen for a bill and `actual` has not yet risen for
    it, which is the same overstatement measured in microseconds instead of
    for ever. It also means `received_not_billed` and `commitment` are derived
    from the SAME per-line `billed`, rather than from two reads a concurrent
    bill can separate.
    """
    written = _set_columns(svc._RECOMPUTE_DERIVED_SQL)
    assert written == set(svc.DERIVED_LEDGER_COLUMNS), (
        f"_RECOMPUTE_DERIVED_SQL sets {sorted(written)}; the six derived "
        f"columns are {sorted(svc.DERIVED_LEDGER_COLUMNS)}")


def test_the_register_matches_the_schema_minus_the_budget_columns():
    """`DERIVED_LEDGER_COLUMNS` is not allowed to drift from the DDL.

    The three budget columns are `recompute_cell`'s, derived from
    `budget_line`; everything else on the table is the procurement position and
    belongs to `recompute_derived_position`. If a tenth money column appears,
    this fails until somebody decides which half it is in -- which is the
    decision that was never made for `actual_paise`.
    """
    budget_columns = {"original_paise", "revisions_paise", "future_budget_paise"}
    # The decision for the tenth column, made: `commitment_carried_paise`
    # (027) is in NEITHER half. It is a carried stock, backfilled once by the
    # migration and written afterwards by exactly one explicit statement,
    # `release_carried_commitment`, under the cell locks and with a reason;
    # the recompute reads and preserves it (H-7) and must never assign it.
    carried_columns = {"commitment_carried_paise"}
    assert carried_columns <= set(_ledger_money_columns())
    assert "commitment_carried_paise" in _set_columns(
        inspect.getsource(svc.release_carried_commitment)), (
        "release_carried_commitment no longer writes the carried figure")
    assert "commitment_carried_paise" not in _set_columns(svc._RECOMPUTE_DERIVED_SQL), (
        "the recompute must preserve the carried figure, not derive it (027, H-7)")
    assert set(_ledger_money_columns()) - budget_columns - carried_columns == set(
        svc.DERIVED_LEDGER_COLUMNS)


def test_exactly_one_statement_writes_the_derived_columns():
    """One writer, so the two engines cannot disagree.

    `recompute_commitment` was a second statement deriving `commitment_paise`
    until 014 made it a thin delegation. A third would be free to compute
    `max(0, ordered - billed)` slightly differently -- a status filter placed
    in the join instead of the CASE, say -- and the two would disagree about
    how much money is committed, silently, on alternate code paths.
    """
    updates = re.findall(
        r"UPDATE budget_ledger_cell SET(.*?)WHERE wbs_id",
        _ALL_WRITERS, re.DOTALL)
    assert len(updates) == 2, (
        f"expected exactly two UPDATE budget_ledger_cell statements -- "
        f"`recompute_cell`'s budget columns and "
        f"`recompute_derived_position`'s six -- but found {len(updates)}")
    derived = [u for u in updates if "commitment_paise" in u]
    assert len(derived) == 1, (
        "more than one statement derives commitment_paise; the two can drift")


# =========================================================================
# The formulas are `domain.compute_ledger`'s, not a reimplementation
# =========================================================================
def test_actual_is_grouped_on_the_bill_lines_own_cell_and_counts_non_po_lines():
    """THE ONE THAT IS NOT PER-PO-LINE, and the difference is load-bearing.

    `domain.compute_ledger` groups `bill_line` on `(wbs_id, budget_head_id)` --
    the line's OWN control cell -- with NO `po_line_id IS NOT NULL` filter, so
    it counts NON-PO BILL LINES too. A non-PO bill is an ordinary document
    (`bill.po_id` is nullable in 013 precisely for it) and it moves actual
    CWIP.

    Deriving `actual` from PO lines instead would miss every one of them,
    UNDERSTATING exposure -- the permissive direction, and the exact class of
    error a reimplementation-from-memory produces.
    """
    sql = svc._RECOMPUTE_DERIVED_SQL
    actual = sql[sql.index("actual_paise = COALESCE(("):]
    actual = actual[:actual.index("), 0)::bigint")]
    assert "FROM bill_line bl" in actual
    assert "bl.wbs_id = %(wbs_id)s" in actual, (
        "actual is not grouped on the bill line's OWN wbs_id")
    assert "bl.budget_head_id = %(head)s" in actual
    assert "po_line_id" not in actual, (
        "the actual limb filters on po_line_id, so every non-PO bill line is "
        "missed and exposure is UNDERSTATED -- the permissive direction")
    assert "b.accounting_status = ANY(%(effective)s)" in actual, (
        "actual counts bills outside AUD-C-004's accounting-effective set")


def test_reversal_negates_by_flag_and_never_by_data_entry():
    """AUD-C-004. A reversal is a NEW row carrying a flag, never an edit and
    never a negative typed in by hand -- so the SQL takes `-ABS(...)` of the
    document's own amount rather than trusting its sign."""
    sql = svc._RECOMPUTE_DERIVED_SQL
    assert sql.count("-ABS(") == 3, (
        "expected three reversal negations -- receives, per-line billed, and "
        "actual -- each taking -ABS of the document's own amount")
    assert "WHEN g.is_reversal" in sql, "receipt reversal is not by flag"
    assert sql.count("b.accounting_status = 'Reversal'") == 2, (
        "bill reversal is not by flag on both limbs that read a bill")


def test_a_void_receipt_does_not_count_as_received():
    """`compute_ledger`: `WHERE g.status <> 'Void'`. Migration 014's
    `ck_grn_status` is what makes 'Void' the only spelling that can reach the
    column, so this predicate and that constraint hold each other up."""
    assert "g.status <> 'Void'" in svc._RECOMPUTE_DERIVED_SQL
    assert "ck_grn_status CHECK (status IN ('Approved', 'Void'))" in _014


def test_commitment_is_ordered_less_billed_and_never_less_received():
    """The anti-double-count and the anti-under-count, both.

    A line commits its UNBILLED balance, so `max(0, ordered - billed)`; value
    received but not yet billed is its OWN bucket, `max(0, received - billed)`,
    and is NOT commitment. Subtracting `received` from commitment instead would
    release commitment for goods that have arrived and have not been invoiced.
    """
    sql = svc._RECOMPUTE_DERIVED_SQL
    assert "GREATEST(0, ordered_paise - billed_paise)" in sql
    assert "GREATEST(0, received_paise - billed_paise)" in sql
    assert "ordered_paise - received_paise" not in sql, (
        "commitment is being relieved by RECEIPTS rather than by bills; goods "
        "that arrived and were never invoiced would release their commitment")


def test_ordered_and_received_ignore_the_purchase_orders_status():
    """`compute_ledger` applies `COMMITMENT_RELEASING_STATES` to COMMITMENT
    ONLY. A cancelled order was still ordered, and goods received against it
    were still received; only the open commitment goes to zero. A status filter
    in the CTE's `FROM` would zero all four at once."""
    sql = svc._RECOMPUTE_DERIVED_SQL
    cte = sql[sql.index("WITH po_line_position AS ("):sql.index("UPDATE budget_ledger_cell")]
    assert "%(releasing)s" not in cte, (
        "the releasing-status filter is inside the CTE, so `ordered` and "
        "`received` would drop to zero when a purchase order is cancelled -- "
        "which is not what compute_ledger does")
    assert "po_status = ANY(%(releasing)s)" in sql, (
        "commitment is not released for a Cancelled/Closed purchase order")


def test_the_releasing_and_effective_sets_come_from_the_domain_module():
    """Not restated. A fifth releasing state or a third accounting-effective
    state added in one place and not the other makes the two engines disagree
    about how much money is committed, silently."""
    assert tuple(svc.COMMITMENT_RELEASING_STATES) == tuple(
        sorted(domain.COMMITMENT_RELEASING_STATES)) or set(
        svc.COMMITMENT_RELEASING_STATES) == domain.COMMITMENT_RELEASING_STATES
    assert set(svc.ACCOUNTING_EFFECTIVE_BILL_STATES) == (
        domain.ACCOUNTING_EFFECTIVE_BILL_STATES)


def test_pr_reserved_counts_only_a_live_reservation():
    """AUD-H-001: a reservation is Reserved, then Converted or Released or
    Expired, exactly once. Only `Reserved` holds budget."""
    sql = svc._RECOMPUTE_DERIVED_SQL
    assert "FROM pr_reservation r" in sql
    assert "r.state = 'Reserved'" in sql
    assert svc.RESERVATION_LIVE_STATE == "Reserved"


def test_every_sum_over_paise_is_cast_back_to_bigint():
    """`SUM(bigint)` returns NUMERIC in PostgreSQL, which psycopg hands back as
    `decimal.Decimal`. One uncast aggregate puts a Decimal into the financial
    controls, which is the only float/Decimal-leakage path in the system."""
    sql = svc._RECOMPUTE_DERIVED_SQL
    sums = re.findall(r"\bSUM\s*\(", sql)
    casts = sql.count("::bigint")
    assert casts >= len(sums), (
        f"{len(sums)} SUM(s) and only {casts} ::bigint cast(s)")


# =========================================================================
# The writer is actually REACHED from the inbound path
# =========================================================================
def test_the_inbound_bill_path_refreshes_the_cells_it_touched():
    """A writer nothing calls closes nothing.

    Before 014 the note in `procurement_services` said plainly that "nothing on
    the inbound path calls this function", and treated the resulting staleness
    as the safe half of the gap. It was safe only while `actual_paise` was not
    also being written: once both limbs move, a bill that lowers `commitment`
    and never raises `actual` is the over-commitment hole itself.
    """
    body = _LEDGER_SOURCE[_LEDGER_SOURCE.index("def mirror_bill("):
                          _LEDGER_SOURCE.index("def _mirror_bill_line(")]
    assert "refresh_cells_after_ingest" in body, (
        "mirror_bill writes bill lines and never re-derives the control cells "
        "they post to; `actual_paise` would stay at zero while "
        "`commitment_paise` falls on the next purchase-order write")
    assert "touched_cells" in body


def test_the_inbound_receipt_path_refreshes_the_cell_it_touched():
    """A receipt moves `received_paise` and `received_not_billed_paise`."""
    body = _LEDGER_SOURCE[_LEDGER_SOURCE.index("def record_receive_line("):
                          _LEDGER_SOURCE.index("def _mirror_grn_header(")]
    assert "refresh_cells_after_ingest" in body


def test_the_refresh_takes_the_cell_locks_before_it_writes():
    """Rule 1 of the global lock order, held inside the function that writes.

    The lock and the write it protects are one function on purpose: a caller
    that took the lock itself and then looped over
    `recompute_derived_position` would be correct only until somebody edited
    it, and `tests/test_pg_locking_order.py` could not see the pairing.
    """
    body = _SVC_SOURCE[_SVC_SOURCE.index("def refresh_cells_after_ingest("):
                       _SVC_SOURCE.index("def recompute_commitment(")]
    lock = body.index("lock_affected_cells(")
    write = body.index("recompute_derived_position(")
    assert lock < write, (
        "refresh_cells_after_ingest writes a cell before it locks it")


def test_recompute_commitment_is_a_delegation_and_not_a_second_statement():
    """The historical name is kept so no caller breaks, and kept THIN so the
    two can never derive commitment differently."""
    body = _SVC_SOURCE[_SVC_SOURCE.index("def recompute_commitment("):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
    assert "recompute_derived_position(" in body
    assert "UPDATE" not in body, (
        "recompute_commitment issues its own UPDATE again; there must be "
        "exactly one statement deriving a ledger column")
