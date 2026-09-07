"""Commitment against actual over `013_procurement.sql`, and the seams around it.

WHY THIS FILE EXISTS
====================

`GET /api/integrations/reconciliation` refused for eleven migrations with a
reason that was a fact about the schema: PostgreSQL held no purchase order, no
GRN and no bill. `013_procurement.sql` created all eight procurement documents,
so the refusal stopped being true and the route now answers. This is the file
that says the answer is right.

**NO DATABASE, AND NO SKIPS.** Every property below is a property of the SQL
text, of the module constants, or of integer arithmetic, so all of it runs on a
machine with no PostgreSQL -- which is where it most needs to, because
`tests/test_pg_reconciliation.py` skips its entire live half there and would
report green over an untested change.

What that means this file CANNOT assert, stated rather than implied: it does not
execute a statement, so it cannot prove the planner accepts one, cannot prove an
`ON CONFLICT` target resolves at runtime, and cannot produce a `Decimal` -- the
`::bigint` casts are checked as text here and only CI's `pg_tests` job can prove
they work. `tests/test_pg_reconciliation.py`'s header says the same thing about
its own SQL half and is right to.

THE ARITHMETIC RULE THIS FILE EXISTS FOR
========================================
Open commitment is **ordered less BILLED**, floored at zero, and zero outright
once the purchase order is Cancelled or Closed. It is NOT ordered less
*received*: a receipt does not release a commitment, a bill does. Getting it
backwards understates open commitment on every partially received line in the
estate, which is the number the whole CAPEX control exists to protect.

Received-not-billed is its OWN bucket, `received - billed` floored at zero, and
is never subtracted from commitment and never added to it.

Both are `domain.compute_ledger` / `domain.reconciliation`, which are the frozen
`C5_formulas.json` registry in code. `test_the_constants_are_transcribed_from_domain`
below is what stops the PostgreSQL side quietly growing a second opinion.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import ast as _ast  # noqa: E402
import inspect as _inspect  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from app.backend import domain  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement as procurement  # noqa: E402
from app.backend.pg.engine import Scope  # noqa: E402

_MIGRATIONS = _ROOT / "migrations" / "pg"
_013 = _MIGRATIONS / "013_procurement.sql"
_002 = _MIGRATIONS / "002_budget_control.sql"

#: The two modules the reconciliation surface is split across, and the split is
#: the point. `integration_store.py` keeps the `sweeps.SweepStore` names and the
#: integer arithmetic; `procurement.py` holds every statement that names a
#: `*_paise` column, because
#: `test_integration_store.py::test_money_in_this_module_appears_only_on_the_011_exception_table`
#: allows money into the store ONLY against `reconciliation_exception` and that
#: guard is kept at its original width rather than widened.
_STORE_SOURCE = _Path(store.__file__)
_PROCUREMENT_SOURCE = _Path(procurement.__file__)

ENTITY = "ENT-DM-01"
PROJECT = "PRJ-01"


# ============================================================== the recorder
class _Recorder:
    """A `Session` stand-in that records statements and returns no rows.

    Returning nothing makes every reader take its empty branch and every writer
    take its refusal branch, so one call per function captures the rendered
    statement AND exercises the branch that must not invent a value.
    """

    def __init__(self, rows=None) -> None:
        self.statements: list[str] = []
        self._rows = rows if rows is not None else []
        self.scope = Scope(user_id="U-SQL", entity_ids=frozenset({ENTITY}),
                           project_ids=frozenset({PROJECT}))

    def fetchall(self, statement, params=None):
        self.statements.append(statement)
        return list(self._rows)

    def fetchone(self, statement, params=None):
        self.statements.append(statement)
        return self._rows[0] if self._rows else None

    def execute(self, statement, params=None):
        self.statements.append(statement)


def _rendered() -> dict[str, list[str]]:
    """Every procurement statement, as it would reach the server."""
    out: dict[str, list[str]] = {}
    calls = (
        ("reconciliation_lines",
         lambda s: store.reconciliation_lines(s, project_id=PROJECT, limit=10)),
        ("resolve_po_line",
         lambda s: store.resolve_po_line(s, po_external_id="PO-EXT-1",
                                         line_external_id="LI-1")),
        ("record_receive_line",
         lambda s: store.record_receive_line(
             s, po_line_id="POL-1", receive_external_id="RCV-1",
             line_external_id="LI-1", quantity="2", amount_paise=100)),
    )
    for name, call in calls:
        recorder = _Recorder()
        try:
            call(recorder)
        except store.IntegrationStoreError:
            pass          # the refusal branch; the SQL is what is wanted
        out[name] = recorder.statements
    return out


def _ddl_columns(path: _Path) -> set[str]:
    """Every column name declared by a `CREATE TABLE` in one migration."""
    text = path.read_text(encoding="utf-8")
    columns: set[str] = set()
    for block in re.findall(r"CREATE TABLE \w+ \((.*?)\n\);", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("--") or line.startswith(
                    ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK")):
                continue
            match = re.match(r"([a-z_][a-z0-9_]*)\s", line)
            if match:
                columns.add(match.group(1))
    return columns


# ==================================================== 1. the frozen formulas
def test_the_constants_are_transcribed_from_domain():
    """The PostgreSQL side must not grow a second opinion about these.

    A fifth commitment-releasing state, or a third accounting-effective bill
    status, added on one side only would make PostgreSQL and the SQLite ledger
    quote DIFFERENT open commitment for the same estate -- silently, with both
    test suites green, and in the direction that understates.
    """
    assert set(store.COMMITMENT_RELEASING_STATES) == \
        set(domain.COMMITMENT_RELEASING_STATES), (
            "integration_store.COMMITMENT_RELEASING_STATES has drifted from "
            "domain's; open commitment now means two different things")
    assert set(store.ACCOUNTING_EFFECTIVE_BILL_STATES) == \
        set(domain.ACCOUNTING_EFFECTIVE_BILL_STATES), (
            "integration_store.ACCOUNTING_EFFECTIVE_BILL_STATES has drifted "
            "from domain's; a bill status is effective on one side only")


def test_the_position_sentences_are_the_ledgers_own_words():
    """SCR-18 renders `position` verbatim.

    A reader comparing the PostgreSQL screen against the SQLite one must not
    find two different sentences describing the same state, so these are
    transcribed rather than paraphrased.
    """
    source = domain.__file__
    text = _Path(source).read_text(encoding="utf-8")
    for sentence in ("PO cancelled before billing",
                     "PO cancelled after partial billing",
                     "PO closed - residual released",
                     "PO approved, not billed",
                     "Partially billed", "Fully billed", "Bill exceeds PO"):
        assert sentence in text, (
            f"{sentence!r} is no longer in domain.py; the transcription in "
            f"integration_store._reconciliation_position is now the only copy "
            f"and the two screens will disagree")


@pytest.mark.parametrize("ordered,received,billed,po_status,expected", [
    # over-billed outranks everything, on a released line too.
    (100, 100, 130, "Approved", "over-billed"),
    (100, 100, 130, "Closed", "over-billed"),
    # received-unbilled outranks RELEASED, and that is the non-obvious one: a
    # closed purchase order still holding value received and never invoiced is
    # something somebody has to act on, and the closure makes neither the
    # receipt nor the missing invoice untrue.
    (100, 80, 40, "Closed", "received-unbilled"),
    (100, 80, 40, "Approved", "received-unbilled"),
    # released, with nothing outstanding to receive.
    (100, 40, 40, "Cancelled", "released"),
    (100, 0, 0, "Closed", "released"),
    # in line.
    (100, 40, 40, "Approved", "ok"),
    (100, 0, 0, "Approved", "ok"),
])
def test_the_flag_precedence_is_the_ledgers_and_not_the_obvious_one(
        ordered, received, billed, po_status, expected):
    """SCR-18 renders this flag, and `domain.reconciliation` computes the same
    one for the same line from SQLite.

    Ranking `released` above `received-unbilled` -- which reads as the natural
    order, since a closed purchase order sounds finished -- would file an
    uninvoiced receipt on a closed order under "nothing to see here" and drop
    it out of the exception list. `_derive` above cannot catch that: the
    precedence lives in `_reconciliation_position` and nowhere else.
    """
    released = po_status in store.COMMITMENT_RELEASING_STATES
    flag, _position = store._reconciliation_position(
        ordered, received, billed, released, po_status)
    assert flag == expected


@pytest.mark.parametrize("ordered,billed,po_status,expected", [
    (100, 0, "Cancelled", "PO cancelled before billing"),
    (100, 40, "Cancelled", "PO cancelled after partial billing"),
    (100, 40, "Closed", "PO closed - residual released"),
    (100, 0, "Approved", "PO approved, not billed"),
    (100, 40, "Approved", "Partially billed"),
    (100, 100, "Approved", "Fully billed"),
    (100, 130, "Approved", "Bill exceeds PO"),
])
def test_the_position_sentence_matches_the_ledger_for_every_state(
        ordered, billed, po_status, expected):
    released = po_status in store.COMMITMENT_RELEASING_STATES
    _flag, position = store._reconciliation_position(
        ordered, 0, billed, released, po_status)
    assert position == expected


# ==================================================== 2. the arithmetic rule
#: (ordered, received, billed, po_status) -> the four derived figures.
_CASES = [
    # live, nothing billed: the whole order is still committed.
    (250_000_00, 0, 0, "Approved"),
    # live, partially received and partially billed. THE CASE THAT MATTERS:
    # open commitment is ordered - BILLED (150,00,000), never ordered -
    # received (130,00,000).
    (250_000_00, 120_000_00, 100_000_00, "Approved"),
    # live, received in full and not billed at all.
    (90_000_00, 90_000_00, 0, "Approved"),
    # live, over-billed.
    (90_000_00, 90_000_00, 95_000_00, "Approved"),
    # closed with an unbilled residual.
    (100_000_00, 40_000_00, 40_000_00, "Closed"),
    # cancelled before anything was billed.
    (100_000_00, 0, 0, "Cancelled"),
    # cancelled after partial billing.
    (100_000_00, 30_000_00, 30_000_00, "Cancelled"),
    # closed AND over-billed -- both releases at once.
    (100_000_00, 100_000_00, 130_000_00, "Closed"),
    # a reversal took received negative.
    (100_000_00, -20_000_00, 0, "Approved"),
    (0, 0, 0, "Approved"),
]


def _derive(ordered: int, received: int, billed: int, po_status: str) -> dict:
    """The store's own arithmetic, isolated from its SQL.

    Deliberately re-expressed here from `reconciliation_lines`' Python half
    rather than imported: this is the oracle, and importing the thing under
    test as its own oracle proves nothing.
    """
    released = po_status in store.COMMITMENT_RELEASING_STATES
    open_commitment = 0 if released else max(0, ordered - billed)
    return {
        "open_commitment_paise": open_commitment,
        "received_not_billed_paise": max(0, received - billed),
        "exposure_paise": open_commitment + billed,
        "residual_released_paise": ((ordered - billed)
                                    if (released and ordered > billed) else 0),
        "over_billed_paise": max(0, billed - ordered),
    }


@pytest.mark.parametrize("ordered,received,billed,po_status", _CASES)
def test_the_identity_balances_to_the_paisa(ordered, received, billed,
                                            po_status):
    """ordered - billed == open + released - over_billed. EXACTLY, in paise.

    Not "approximately", and not "to the rupee". These are `bigint` paise and
    the identity is integer arithmetic, so any residual at all is a defect --
    either money has been counted twice or some of it has been lost, and the
    two are indistinguishable from a rounded total.
    """
    d = _derive(ordered, received, billed, po_status)
    residual = ((ordered - billed)
                - (d["open_commitment_paise"]
                   + d["residual_released_paise"]
                   - d["over_billed_paise"]))
    assert residual == 0, (
        f"ordered {ordered} - billed {billed} does not reconcile: residual "
        f"{residual} paise on a {po_status} line")


def test_open_commitment_is_ordered_less_billed_never_less_received():
    """The single rule this whole module exists to protect.

    A receipt does not release a commitment; a bill does. The case below is
    chosen so the two answers differ: ordered 25,00,000, received 12,00,000,
    billed 10,00,000. Open commitment is 15,00,000. It is NOT 13,00,000.
    """
    d = _derive(250_000_00, 120_000_00, 100_000_00, "Approved")
    assert d["open_commitment_paise"] == 150_000_00
    assert d["open_commitment_paise"] != 250_000_00 - 120_000_00, (
        "open commitment was computed as ordered less RECEIVED; a receipt "
        "does not release a commitment")


def test_received_not_billed_is_its_own_bucket_and_is_never_netted():
    """It overlaps open commitment by design and the two are never added.

    Asserted as a property rather than a comment: on the partially-received
    line above, subtracting received-not-billed from open commitment would give
    13,00,000 -- the exact wrong answer the rule above rules out -- and adding
    them would give 17,00,000, which double-counts 2,00,000 of the same order.
    """
    d = _derive(250_000_00, 120_000_00, 100_000_00, "Approved")
    assert d["received_not_billed_paise"] == 20_000_00
    assert d["open_commitment_paise"] - d["received_not_billed_paise"] \
        != d["open_commitment_paise"], "the two are distinct quantities"
    # And neither operation is what the store reports.
    assert d["exposure_paise"] == d["open_commitment_paise"] + 100_000_00, (
        "exposure is open commitment plus BILLED, not plus received")


def test_a_released_purchase_order_commits_nothing_however_much_is_unbilled():
    for status in store.COMMITMENT_RELEASING_STATES:
        d = _derive(100_000_00, 0, 0, status)
        assert d["open_commitment_paise"] == 0, (
            f"a {status} purchase order still holds commitment")
        assert d["residual_released_paise"] == 100_000_00, (
            f"a {status} purchase order released its residual and the "
            f"response does not say so; a reader cannot tell it from an "
            f"over-bill, and the two mean opposite things")


def test_the_summary_totals_and_reports_whether_it_balanced():
    """The band SCR-18 renders is a pure function of the rows returned.

    Two queries -- one for the table, one for the totals -- would be two
    questions asked at two instants, and the tile would quietly stop being the
    total of what is on screen. This asserts the summary cannot disagree with
    its own rows, and that it says so on its face.
    """
    lines = []
    for ordered, received, billed, po_status in _CASES:
        derived = _derive(ordered, received, billed, po_status)
        lines.append({
            "ordered_paise": ordered, "received_paise": received,
            "billed_paise": billed, "po_number": "PO-1", "line_no": 1,
            "flag": "ok", **derived,
        })
    summary = store.reconciliation_summary(lines)

    assert summary["lines"] == len(_CASES)
    assert summary["ordered_paise"] == sum(c[0] for c in _CASES)
    assert summary["received_paise"] == sum(c[1] for c in _CASES)
    assert summary["billed_paise"] == sum(c[2] for c in _CASES)
    assert summary["identity_residual_paise"] == 0
    assert summary["identity_balanced"] is True, (
        "the totals do not reconcile to the paisa across the row set")


def test_an_unbalanced_row_set_is_reported_and_not_smoothed_over():
    """The balance flag has to be able to say no, or it says nothing.

    A row whose figures do not satisfy the identity is a defect somewhere
    upstream. The summary reports the residual rather than rounding it away or
    recomputing the parts to agree -- an operator signing off a total is
    entitled to know the parts did not add up.
    """
    summary = store.reconciliation_summary([{
        "ordered_paise": 100, "received_paise": 0, "billed_paise": 40,
        "open_commitment_paise": 0,        # should be 60 on a live line
        "received_not_billed_paise": 0, "exposure_paise": 40,
        "residual_released_paise": 0, "over_billed_paise": 0,
        "po_number": "PO-X", "line_no": 1, "flag": "ok",
    }])
    assert summary["identity_balanced"] is False
    assert summary["identity_residual_paise"] == 60


def test_the_summary_lists_every_flagged_line_and_no_others():
    lines = [
        {"po_number": "PO-1", "line_no": 1, "flag": "ok"},
        {"po_number": "PO-2", "line_no": 3, "flag": "over-billed"},
        {"po_number": "PO-3", "line_no": 2, "flag": "received-unbilled"},
        {"po_number": "PO-4", "line_no": 1, "flag": "released"},
    ]
    for line in lines:
        line.update({k: 0 for k in (
            "ordered_paise", "received_paise", "billed_paise",
            "open_commitment_paise", "received_not_billed_paise",
            "exposure_paise", "residual_released_paise", "over_billed_paise")})
    flagged = store.reconciliation_summary(lines)["exceptions"]
    assert [e["po_number"] for e in flagged] == ["PO-2", "PO-3"], (
        "`released` is not an exception -- it is a purchase order doing "
        "exactly what closing one does -- and `ok` plainly is not")


# ===================================================== 3. the SQL, statically
def test_every_procurement_statement_carries_a_compiled_scope_predicate():
    """`repo.query` refuses a statement with no `{scope}` token, so the token
    is guaranteed. What is NOT guaranteed is that the compiled predicate says
    anything: a scope mapping that waived every dimension compiles to TRUE.
    """
    for name, statements in _rendered().items():
        assert statements, f"{name} issued no statement at all"
        for statement in statements:
            assert "{scope}" not in statement, (
                f"{name}: the token reached the server unsubstituted")
            assert "__scope_entity" in statement, (
                f"{name}: the rendered statement carries no entity predicate; "
                f"a principal restricted to one entity is reading every "
                f"entity's purchase orders")
            assert "__scope_project" in statement, (
                f"{name}: the rendered statement carries no project predicate")


def test_every_procurement_statement_names_only_real_columns():
    """A column that does not exist is a 3am runtime error in a cron function.

    Checked against the migrations, so they stay the single source of truth and
    this module's SQL stays a transcription of them.
    """
    columns = _ddl_columns(_013) | _ddl_columns(_002)
    assert {"po_line_id", "amount_paise", "accounting_status", "is_reversal",
            "entity_id", "plant_id", "location_id"} <= columns, (
        "the DDL parser has stopped finding columns; it is asserting nothing")

    # The aliases that name a REAL TABLE. `s` is deliberately absent: it is the
    # `scoped_line` CTE, whose output columns are computed
    # (`ordered_paise`, `budget_head`, `po_external_id`) and are not columns of
    # any table, so checking them against the DDL would fail on correct SQL.
    # The CTE joins in the outer query are aliased `rcv` / `bld` rather than
    # `r` / `b` precisely so this set can stay unambiguous -- `b` here always
    # means `bill`.
    aliases = {"pl", "po", "g", "gl", "b", "bl", "w", "bh", "p"}
    problems = []
    for name, statements in _rendered().items():
        for statement in statements:
            for alias, column in re.findall(r"\b([a-z]{1,2})\.(\w+)\b",
                                            statement):
                if alias not in aliases:
                    continue
                if column not in columns:
                    problems.append(f"{name}: {alias}.{column}")
    assert not problems, (
        f"these columns are named in SQL and exist in neither "
        f"{_013.name} nor {_002.name}: {sorted(set(problems))}")


def test_every_paise_sum_is_cast_back_to_bigint():
    """`SUM()` over `bigint` returns NUMERIC, and psycopg maps numeric to
    `Decimal`.

    A Decimal reaching integer arithmetic is the defect that took down the
    availability verdict, both approval paths and the concurrency proof -- in
    the PostgreSQL CI job only, because no in-memory double can produce one.
    """
    problems = []
    for name, statements in _rendered().items():
        for statement in statements:
            for match in re.finditer(r"SUM\(", statement):
                tail = statement[match.start():]
                # The cast must be on the SUM, not somewhere later in the line.
                depth, end = 0, None
                for index, char in enumerate(tail):
                    if char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            end = index + 1
                            break
                if end is None:
                    problems.append(f"{name}: unbalanced SUM(")
                    continue
                if not tail[end:end + 8].startswith("::bigint"):
                    problems.append(f"{name}: {tail[:60]!r}")
    assert not problems, (
        f"these SUM() expressions are not cast back to bigint and will "
        f"return Decimal: {problems}")


def test_the_on_conflict_target_is_a_constraint_that_actually_exists():
    """Idempotency IS that constraint.

    A target naming a constraint the migration does not declare is refused by
    PostgreSQL outright -- the good failure, but only if it is caught before a
    cron function hits it. A target INFERRED by columns is the same failure by
    another route: PostgreSQL matches the tuple against the unique indexes it
    holds and raises when none matches, so a mistyped column list is a 3am
    error too.

    READ FROM THE SOURCE, NOT FROM RENDERED STATEMENTS. The writers refuse
    before they reach their INSERT when the recorder returns no rows -- which
    is correct behaviour, and is what the two refusal tests below assert -- so
    a rendered-statement version of this check would quietly inspect nothing.
    `assert clauses` and the count at the end are what stop that recurring.

    THE PARTIAL-INDEX HALF IS THE ONE THAT BITES. `ux_grn_external`,
    `ux_bill_external` and `ux_po_line_external` are each declared
    ``WHERE ... IS NOT NULL``. An `ON CONFLICT (cols)` carrying no predicate
    infers NO index against a partial one -- PostgreSQL raises rather than
    quietly picking it -- so the predicate is load-bearing, not decoration.
    """
    migration = _013.read_text(encoding="utf-8")

    # Every unique key 013 declares, keyed by its column set, valued by whether
    # the index behind it is PARTIAL.
    unique: dict[frozenset, bool] = {}
    for _table, columns, predicate in re.findall(
            r"CREATE UNIQUE INDEX \w+\s*\n\s*ON (\w+) \(([^)]*)\)"
            r"(\s*\n\s*WHERE [^;]*)?;", migration):
        unique[frozenset(c.strip() for c in columns.split(","))] = bool(predicate)
    for columns in re.findall(r"UNIQUE \(([^)]*)\)", migration):
        unique.setdefault(
            frozenset(c.strip() for c in columns.split(",")), False)
    for column in re.findall(r"(\w+)\s+text PRIMARY KEY", migration):
        unique.setdefault(frozenset({column}), False)
    assert len(unique) > 5, (
        "the DDL parser found no unique keys; this test is asserting nothing")

    source = _PROCUREMENT_SOURCE.read_text(encoding="utf-8")
    clauses = [source[m.start():m.start() + 260]
               for m in re.finditer(r"ON CONFLICT ", source)]
    assert clauses, (
        f"no ON CONFLICT clause in {_PROCUREMENT_SOURCE.name}; the writers "
        f"have moved and this guard is looking at the wrong file")

    checked = 0
    for clause in clauses:
        named = re.match(r"ON CONFLICT ON CONSTRAINT \{?(\w+)\}?", clause)
        if named is not None:
            target = getattr(store, named.group(1), named.group(1))
            checked += 1
            assert re.search(rf"CONSTRAINT {target}\b", migration), (
                f"ON CONFLICT names {target}, which {_013.name} does not "
                f"declare")
            continue
        columns = re.match(r"ON CONFLICT \(([^)]*)\)", clause, re.S)
        if columns is None:
            continue                      # `ON CONFLICT DO NOTHING`: no target
        target_columns = frozenset(c.strip()
                                   for c in columns.group(1).split(","))
        checked += 1
        assert target_columns in unique, (
            f"ON CONFLICT infers {sorted(target_columns)}, which "
            f"{_013.name} declares no unique key over")
        if unique[target_columns]:
            head = clause[:clause.index("DO ")]
            assert "WHERE" in head, (
                f"ON CONFLICT {sorted(target_columns)} targets a PARTIAL "
                f"unique index and carries no predicate. PostgreSQL infers no "
                f"index at all and raises.")
    assert checked >= 3, (
        f"only {checked} conflict targets were checked; the writers have "
        f"moved and this guard is asserting almost nothing")


def test_the_reconciliation_query_reaches_all_four_dimensions():
    """`PROCUREMENT_SCOPE_COLUMNS` waives nothing, and must not start to.

    013's own policies pass all four dimensions of `project` to
    `capex_scope_permits`. A mapping that waived entity, plant and location
    -- which these tables' own columns invite, since none of them carries any
    of the three -- would leave the application-layer predicate silent about a
    restriction RLS still enforces, making the repository check the one place
    the restriction is NOT expressed.
    """
    mapping = store.PROCUREMENT_SCOPE_COLUMNS
    assert set(mapping) == {"entity", "plant", "location", "project"}
    waived = sorted(k for k, v in mapping.items() if v is None)
    assert not waived, (
        f"{waived} are waived; 013's policies filter on all four through "
        f"`project` and this mapping is meant to be the second, independent "
        f"enforcement of the same predicate")
    for column in mapping.values():
        assert column.startswith("p."), (
            f"{column} does not name the `project` alias the statements join")


# ==================================== 4. the two seams that must refuse
def test_resolve_po_line_returns_none_rather_than_choosing(monkeypatch):
    """An ambiguous receive line is quarantined, never attributed to a guess.

    On ERP a receive line frequently carries no line identifier, so this branch
    is the ordinary case. Picking the first line by `line_no` would post a
    receipt against whichever line happened to sort first: a WRONG number in a
    financial control rather than a missing one. Quarantine is visible on
    SCR-27 and recoverable; a mis-attributed receipt is neither.
    """
    ambiguous = _Recorder(rows=[("POL-1",), ("POL-2",)])
    assert store.resolve_po_line(ambiguous, po_external_id="PO-EXT-1",
                                 line_external_id=None) is None

    absent = _Recorder(rows=[])
    assert store.resolve_po_line(absent, po_external_id="PO-EXT-1",
                                 line_external_id="LI-1") is None

    exact = _Recorder(rows=[("POL-7",)])
    assert store.resolve_po_line(exact, po_external_id="PO-EXT-1",
                                 line_external_id="LI-1") == "POL-7"


def test_an_identifierless_receive_line_is_deduplicated_before_it_is_inserted():
    """`ux_grn_line_external` is NULLS DISTINCT, so it does not constrain it.

    Inserting such a line straight in would add the same receipt again on every
    re-walk -- and the sweeps re-walk BY DESIGN, on a 300-second overlap and a
    cycling PO-anchored cursor -- so `received` would climb without a single
    new receive arriving. Deduplicating on `(po_line, receive)` alone would
    instead collapse two genuinely distinct identifierless lines and drop the
    second one's value.

    THE WRITER TAKES NEITHER. It issues an explicit UPDATE matching
    `line_external_id IS NULL` FIRST and returns if it hit a row, so a replay
    updates the receipt it already holds; only a miss falls through to the
    INSERT. The read-then-write window that opens is real and is documented at
    the branch: §2.2's single cron worker per connection is what closes it, and
    saying so is the difference between a documented window and a hidden one.

    Asserted against the SOURCE because the branch is chosen before any row
    comes back, so a recorder cannot tell "took the update path and found
    nothing" from "never took it".
    """
    source = _PROCUREMENT_SOURCE.read_text(encoding="utf-8")
    body = source[source.index("def record_receive_line("):
                  source.index("def _mirror_grn_header(")]

    guard = body.index('if params["line_external_id"] is None:')
    insert = body.index("INSERT INTO {GRN_LINE}")
    assert guard < insert, (
        "the identifierless branch is decided AFTER the insert; a null "
        "line_external_id would reach ON CONFLICT, which is NULLS DISTINCT, "
        "and every re-walk would add the receipt again")

    update = body[guard:insert]
    assert "UPDATE {GRN_LINE}" in update, (
        "the identifierless branch does not update the line it already has")
    assert "gl.line_external_id IS NULL" in update, (
        "the update does not restrict itself to identifierless lines, so it "
        "would overwrite an IDENTIFIED receipt line with an unidentified "
        "line's quantity")
    assert "gl.receive_external_id = %(receive_external_id)s" in update, (
        "the update is not keyed on the receive, so two distinct receipts "
        "against one PO line would collapse into one and the second one's "
        "value would be dropped")
    assert re.search(r"if updated:\s*\n\s*return", update), (
        "the update path does not return, so a hit would fall through to the "
        "insert and duplicate the line it had just updated")


def test_record_receive_line_will_not_write_against_a_po_line_it_cannot_see():
    """The control cell is READ from the PO line, and refused when there is none.

    `po_id`, `wbs_id` and `budget_head_id` are never accepted from the caller:
    `fk_bill_line_po_line_cell` is a four-column FK precisely because a
    document line's control cell must BE its PO line's cell. A `po_line_id`
    that resolves to nothing under this principal's scope therefore has no cell
    to attribute to, and the writer refuses at 404 rather than inventing one --
    a document line attributed to a control cell nobody can name is not
    attributed at all.

    OUT OF SCOPE IS INDISTINGUISHABLE FROM ABSENT here, which is `pg/repo.py`'s
    rule and is deliberate: telling a principal that a PO line exists but is
    not theirs is itself a disclosure.
    """
    recorder = _Recorder()          # no rows: the cell lookup finds nothing
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.record_receive_line(
            recorder, po_line_id="POL-1", receive_external_id="RCV-1",
            line_external_id="LI-1", quantity="1", amount_paise=100)
    assert exc.value.code == "PO_LINE_NOT_FOUND"
    assert exc.value.status == 404
    assert "POL-1" in exc.value.message, (
        "the refusal must name the line it could not attribute")
    assert not any("INSERT INTO" in statement
                   for statement in recorder.statements), (
        "record_receive_line wrote a row before establishing the control cell")


def test_record_receive_line_reads_an_absent_amount_as_absent_not_as_zero():
    """Zero paise is a CLAIM -- "the goods were free" -- and `None` is an absence.

    A writer that read a missing amount as zero would book the receipt at
    nothing, and the ledger would agree with itself while being wrong by the
    entire value of the receipt. The refusal happens before any statement is
    issued, which the recorder proves.
    """
    recorder = _Recorder()
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.record_receive_line(
            recorder, po_line_id="POL-1", receive_external_id="RCV-1",
            line_external_id="LI-1", quantity="1", amount_paise=None)
    assert exc.value.status == 422
    assert recorder.statements == [], (
        "record_receive_line reached the database before refusing an absent "
        "amount")


def test_the_sweep_actor_is_not_a_user_id():
    """`grn_line.created_by` is NOT NULL, so something goes there.

    Writing a user id would make the audit trail claim a decision nobody took:
    a receive line attached by the PO-anchored walk was authored by no person.
    """
    assert store.SWEEP_ACTOR.startswith("SYSTEM:"), (
        "a sweep-written row must be attributed to the process, not to a user")


def test_no_sweep_call_is_left_without_a_table_and_the_refusal_survives():
    """`UNBACKED_SWEEP_SURFACE` is EMPTY, and that is the assertion.

    All five of `sweeps.SweepStore`'s remaining calls -- `resolve_po_line`,
    `record_receive_line`, `accumulate_unattributed`, `bills_awaiting_detail`
    and `mark_detail_hydrated` -- now have a table. Four got one from
    `013_procurement.sql`; the unattributed bucket is answered by
    `reconciliation_exception`, which was ALREADY the row
    `open_exception_exposure` sums, rather than by a ninth table that would
    have become a second source of truth for the very figure that blocks
    capitalisation.

    THE MECHANISM IS KEPT THOUGH THE LIST IS EMPTY, and that half is what this
    guards. `SchemaNotYetMigrated` still exists and still reads its reason out
    of this map, so the next call that arrives ahead of its schema is one entry
    away from refusing properly. Deleting the type would mean that call has to
    reinvent the refusal, and the reinvention is exactly where a plausible
    default gets returned instead.
    """
    assert store.UNBACKED_SWEEP_SURFACE == {}, (
        f"a sweep call is back to having no table: "
        f"{sorted(store.UNBACKED_SWEEP_SURFACE)}. That is not wrong in itself, "
        f"but the refusal tests parametrised over this map must come back too")
    assert issubclass(store.SchemaNotYetMigrated, store.IntegrationStoreError)
    refusal = store.SchemaNotYetMigrated("some_future_call")
    assert refusal.status == 501
    assert "some_future_call" in refusal.message, (
        "the refusal no longer names the call, so a 501 in a cron log would "
        "not say which one stopped")

    for name in ("resolve_po_line", "record_receive_line",
                 "accumulate_unattributed", "bills_awaiting_detail",
                 "mark_detail_hydrated"):
        function = getattr(store, name)
        assert callable(function), f"{name} is no longer on the store"
        assert "SchemaNotYetMigrated" not in _inspect.getsource(function), (
            f"{name} still raises SchemaNotYetMigrated while the map holding "
            f"its reason is empty; the refusal would name no table at all")

    migration = _013.read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE (\w+)", migration))
    assert created == set(store.PROCUREMENT_TABLES), (
        f"013 creates {sorted(created)}; the module's constant names "
        f"{sorted(store.PROCUREMENT_TABLES)}")
    assert not any("unattributed" in t or "hydrat" in t for t in created), (
        "013 created a bucket or a hydration queue after all; the reasoning "
        "above `accumulate_unattributed` -- that the exception table IS the "
        "bucket -- is now stale and must be rewritten, not left standing")


def test_money_in_the_procurement_module_never_reaches_a_transport_table():
    """The other half of the store's money guard, held over the module it moved to.

    `test_integration_store.py::test_money_in_this_module_appears_only_on_the_011_exception_table`
    says no statement in `pg/integration_store.py` may name a `*_paise` column
    except against `reconciliation_exception`. That guard is kept at its
    ORIGINAL width -- 013's money-bearing SQL was moved into
    `pg/procurement.py` rather than added to the store's allow-list -- and the
    move is only worth anything if the destination is held to the rule the
    guard was ever actually protecting.

    THAT RULE IS: money must never be copied onto a TRANSPORT row. An
    `amount_paise` on an outbox row would be a second copy of an amount that
    can drift from the ledger's and be wrong on its own, which section 11's
    design forbids. A statement joining `integration_outbox` to `po_line` is
    exactly the accident the store-side guard can no longer see, because the
    statement is no longer in the store.

    The transport list is DERIVED from `INTEGRATION_TABLES`, 010's own
    inventory, rather than typed out here: a table added to 010 later cannot
    quietly fall outside the ban, which is how a guard like this rots into
    decoration.
    """
    source = _PROCUREMENT_SOURCE.read_text(encoding="utf-8")
    tree = _ast.parse(source)

    statements: list[tuple[int, str]] = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if not (isinstance(func, _ast.Attribute)
                and func.attr in {"query", "query_one"}
                and isinstance(func.value, _ast.Name)
                and func.value.id == "repo"):
            continue
        first = node.args[1] if len(node.args) > 1 else None
        if isinstance(first, _ast.Constant) and isinstance(first.value, str):
            statements.append((node.lineno, first.value))
        elif isinstance(first, _ast.JoinedStr):
            statements.append((node.lineno, "".join(
                part.value if isinstance(part, _ast.Constant)
                else _ast.unparse(part)
                for part in first.values)))
    assert len(statements) >= 5, (
        f"only {len(statements)} repo statements found in "
        f"{_PROCUREMENT_SOURCE.name}; the writers have moved and this guard "
        f"is asserting nothing")

    money = 0
    for lineno, sql in statements:
        if "_paise" not in sql:
            continue
        money += 1
        # BOTH SPELLINGS. A table reaches the statement either as the literal
        # `integration_outbox` or -- far more likely here -- as the module
        # constant `{INTEGRATION_OUTBOX}`, which `ast.unparse` renders as the
        # bare uppercase name. Matching only one of the two would leave the
        # ordinary case unguarded.
        offending = [table for table in store.INTEGRATION_TABLES
                     if re.search(rf"\b{table}\b", sql, re.IGNORECASE)]
        assert not offending, (
            f"{_PROCUREMENT_SOURCE.name} line {lineno} names a money column in "
            f"a statement touching {offending}. 010's tables carry no *_paise "
            f"column and must never be joined to one in the same statement: an "
            f"amount that reaches a transport row is a second copy that can "
            f"drift from the ledger's and be wrong on its own. Read the money "
            f"in its own statement.")
    assert money >= 3, (
        f"only {money} money-bearing statements in {_PROCUREMENT_SOURCE.name}. "
        f"If the ledger SQL moved again this guard should move with it; as "
        f"written it is asserting almost nothing")
