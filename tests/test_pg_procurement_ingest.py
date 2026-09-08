"""Inbound GRN and vendor bill, written into 013's tables, executed for real.

WHAT THIS FILE IS FOR
=====================

`app/backend/pg/procurement.py` is the first code that WRITES to
`013_procurement.sql`'s document chain. `tests/test_pg_procurement_schema.py`
proves the schema refuses what it should; this file proves the ingest puts the
right rows in it and quarantines the rest.

The split down the middle is deliberate and matches the schema file's:

  * everything that can be asserted against the SOURCE is asserted against the
    source, and runs on every machine. That half is not decoration -- it is the
    only coverage that exists locally, because there is no PostgreSQL and no
    Docker here.
  * the live half is gated on ``CAPEX_DB_URL`` and **first executes in CI**.
    A SKIP IS NOT A PASS: nothing below reports success against a database that
    was never there, and until CI's `pg_tests` job is green the honest summary
    of the live half is "the SQL names real columns and has not been executed".

WHY THE SOURCE-LEVEL HALF EXISTS AT ALL
=======================================
Two of the three defects this module is written against are invisible to a
live test that only checks the final row:

  1. **the bucket that added instead of setting.** `SET source_paise = ...`
     and `SET source_paise = source_paise + ...` produce the SAME row on a
     first pass. The difference only appears on a re-walk, and a live test
     that forgets to re-walk passes either way. So the SQL text is asserted
     directly, as well as the behaviour.
  2. **`divmod` flooring a negative.** ``divmod(-150, 100)`` is ``(-2, 50)``
     and rendered ``-2.50``. This module stores integer paise and renders
     nothing, and the test below says so about the source rather than trusting
     that nobody adds a "helpful" formatter later.

WHAT IS NOT COVERED HERE
========================
Row-level security. `013`'s policies are proved by
`tests/test_pg_procurement_schema.py`, which connects as `capex_app` through
`scoped_role_database` because CI's `POSTGRES_USER` is a superuser and a
superuser bypasses RLS unconditionally. The live tests below go through
`pg_connection` and therefore exercise the APPLICATION layer's `{scope}`
predicate, which is a different control with a different failure mode -- and
`tests/test_scope_enforcement.py` is what stops a statement here escaping it.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_app_database, pg_connection, pg_database,
    pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

import ast  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend import domain as sqlite_domain  # noqa: E402
from app.backend.integration import jobs  # noqa: E402
from app.backend.integration import sweeps  # noqa: E402
from app.backend.integration.dto import BillDTO, LineDTO, SourceRef  # noqa: E402
from app.backend.pg import audit as audit_mod  # noqa: E402
from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

from integration_fakes import (  # noqa: E402  the Wave 5 in-process doubles
    ERP as ERP_CAPABILITIES,
    FakeAdapter,
    FakeClock,
    InMemoryStore,
)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)

SOURCE = _Path(procurement.__file__).read_text(encoding="utf-8")
STORE_SOURCE = _Path(store.__file__).read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """`text` with every comment and every docstring removed.

    REGRESSION GUARD, AND THE SAME ONE `test_pg_procurement_schema.py` CARRIES.
    Its `_tables_created_by` scanned raw SQL including comments, so a header
    sentence containing the words CREATE TABLE reported a table nothing
    creates -- a documentation sentence permanently breaking adoption.

    Every scan below is vulnerable the same way, and worse: this module's
    docstrings NAME the defects it is written against. `divmod`, `uuid4` and
    the flooring example are all quoted in prose, so a scan of the raw text
    cannot tell the warning from the mistake it warns about. Stripping first is
    what makes "this module does not render paise" a statement about the code.
    """
    import io
    import tokenize

    out: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            continue
        # A STRING that is the first thing in a logical line is a docstring or
        # a bare expression statement; either way it is prose, not code. A
        # STRING used as an argument or an assignment follows an operator or a
        # name and is kept -- which is what preserves every SQL literal below.
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE,
                tokenize.NL, tokenize.ENCODING):
            previous = token.type
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE):
            previous = token.type
        out.append(token.string)
    return "\n".join(out)


#: The module with its prose removed. Use this for every "the code does not do
#: X" assertion; use :data:`SOURCE` only where the prose is the subject.
CODE = code_only(SOURCE)
MIGRATION = (_Path(__file__).resolve().parents[1] / "migrations" / "pg"
             / "013_procurement.sql").read_text(encoding="utf-8")

ORG = "ORG-ING"
ENTITY = "ENT-ING"
OTHER_ENTITY = "ENT-ING-B"
PROJECT = "PRJ-ING"
OTHER_PROJECT = "PRJ-ING-B"
WBS = "WBS-ING"
HEAD = "BH-ING"

#: Mid-day and mid-minute, so an off-by-one in a timestamp cannot pass by
#: landing on the same boundary either way.
T0 = datetime(2026, 9, 7, 11, 30, 15, tzinfo=timezone.utc)
#: Pinned, not `date.today()`. C17's rows carry effective windows and the
#: resolver compares against them; a test that let the wall clock choose would
#: start failing on a date nobody changed anything on.
AS_OF = date(2026, 9, 7)

#: One PO line, ordered at ten lakh paise. Every quantity below is a fraction
#: or a multiple of this, so a partial, a final and an over-bill are all
#: readable at a glance.
ORDERED_PAISE = 1_000_000

SOURCE_LABEL = "ZOHO_ERP"
PO_EXTERNAL = "ZPO-9001"
POL_EXTERNAL = "ZPOL-5001"


# =========================================================================
# SOURCE-LEVEL. No database. These run on every machine.
# =========================================================================
def test_the_unattributed_bucket_is_set_and_never_incremented():
    """The defect that has already been found here once.

    The sweeps re-walk by design -- a 300-second overlap and a cycling cursor
    -- so `accumulate_unattributed` runs again on every pass with the same
    `source_key`. `SET source_paise = source_paise + %(paise)s` would climb
    every fifteen minutes with no new receive arriving, and the number that
    blocks capitalisation would be fiction. Asserted against the SQL TEXT
    because a first pass produces an identical row either way: only a re-walk
    tells them apart, and a live test that forgets to re-walk passes on both.
    """
    body = inspect.getsource(store.accumulate_unattributed)
    assert "SET source_paise = %(paise)s" in body, (
        "accumulate_unattributed no longer SETS the bucket value")
    assert not re.search(r"source_paise\s*=\s*[\w.]*source_paise\s*\+", body), (
        "the unattributed bucket is being INCREMENTED. Every re-walk inflates "
        "it, and the figure that blocks capitalisation becomes fiction. This "
        "exact defect has been found in this surface once already.")
    assert not re.search(r"source_paise\s*=\s*EXCLUDED\.source_paise\s*\+", body)


def test_no_paise_is_ever_rendered_as_rupees_in_this_module():
    """`divmod` FLOORS. ``divmod(-150, 100)`` is ``(-2, 50)``, which rendered
    ``-2.50`` for a credit note of one rupee fifty.

    Credit notes, returns, reversals and debit notes are ordinary documents
    here and their paise are genuinely negative, so the safe rule is that this
    module does no rendering at all: integer paise in, integer paise out, and
    the only rendering in the whole package stays in the two adapters on the
    way OUT to the wire.
    """
    assert "divmod" not in CODE, (
        "procurement.py renders paise. divmod floors toward negative infinity "
        "and every negative document amount comes out one rupee low.")
    assert "/ 100" not in CODE and "/100" not in CODE
    assert "float(" not in CODE, (
        "money and quantity must never pass through a float here")
    # ...and the module still SAYS why, so the next person to reach for a
    # formatter reads the reason before the rule.
    assert "divmod" in SOURCE, "the reason the rule exists has been deleted"


def test_every_sum_over_a_paise_column_is_cast_to_bigint():
    """PostgreSQL's `SUM()` over `bigint` returns **numeric**, psycopg maps
    numeric to `Decimal`, and a Decimal reaching integer arithmetic is the
    defect that took down the availability verdict, both approval paths and the
    concurrency proof -- in the PostgreSQL CI job only, because a Decimal
    cannot appear without a real server. Which is exactly why this is asserted
    against the source and not left to the live half.

    COUNTED, not matched by proximity. A first version looked for `::bigint`
    within sixty characters of the `SUM(`, which failed on the correct SQL:
    the cast belongs on the enclosing `coalesce(...)`, several lines below,
    because `coalesce(SUM(x), 0)` is still numeric and casting inside would
    leave the zero-row case uncast. Counting is what survives that shape.
    """
    for statement in re.findall(r'"""(.*?)"""', CODE, re.DOTALL):
        sums = re.findall(r"\bSUM\s*\(\s*[\w.]*_paise\s*\)", statement, re.I)
        if not sums:
            continue
        casts = statement.count("::bigint")
        assert casts >= len(sums), (
            f"a statement performs {len(sums)} SUM(s) over a paise column but "
            f"carries only {casts} ::bigint cast(s); psycopg will hand the "
            f"caller a Decimal for the uncast one")


def test_the_grn_line_conflict_target_matches_the_migrations_constraint():
    """`ux_grn_line_external` is a table UNIQUE with NO predicate, so the
    conflict target is its three columns and nothing else. A target that does
    not match a real constraint is a runtime error PostgreSQL raises when the
    statement RUNS -- inside a cron function, in CI at the earliest.

    `tests/test_integration_sql_matches_schema.py` says plainly that it is not
    a SQL parser and cannot do this. So it is done here, against both texts.
    """
    assert "UNIQUE (po_line_id, receive_external_id, line_external_id)" in MIGRATION
    assert ("ON CONFLICT (po_line_id, receive_external_id, line_external_id)"
            in SOURCE)
    columns = store.GRN_LINE_EXTERNAL_UNIQUE
    assert columns == ("po_line_id", "receive_external_id", "line_external_id")


@pytest.mark.parametrize("table", ["grn", "bill"])
def test_every_partial_mirror_index_is_targeted_with_its_predicate(table):
    """`ux_grn_external` and `ux_bill_external` are both PARTIAL --
    ``WHERE external_id IS NOT NULL``. Inference by COLUMNS ALONE matches no
    index on either table, and PostgreSQL refuses rather than choosing another
    one: the good failure, but only if the predicate is written out."""
    assert re.search(
        rf"CREATE UNIQUE INDEX ux_{table}(?:_id)?_?external\s+ON {table} "
        rf"\(external_source, external_id\)\s+WHERE external_id IS NOT NULL",
        MIGRATION), f"ux_{table}_external is no longer the partial index this targets"
    targets = re.findall(
        r"ON CONFLICT \(external_source, external_id\)\s*\n\s*"
        r"WHERE external_id IS NOT NULL", SOURCE)
    assert len(targets) >= 2, (
        "a mirror upsert names (external_source, external_id) without the "
        "partial index's predicate; it matches no index and will not run")


def test_a_mirrored_row_id_is_derived_and_not_random():
    """A replay must land on the SAME row.

    `ux_grn_number` is UNIQUE, so a re-walk that minted a second number for one
    receipt would turn an idempotent replay into a unique violation at 3am; and
    `bill_line` has no external unique index at all in 013, so its PRIMARY KEY
    is the only conflict target available and `capex_app` has DELETE revoked,
    which means a duplicate line could not be tidied away afterwards.
    """
    first = procurement.derived_id("GRN", SOURCE_LABEL, "RCV-1")
    assert first == procurement.derived_id("GRN", SOURCE_LABEL, "RCV-1")
    assert first != procurement.derived_id("GRN", SOURCE_LABEL, "RCV-2")
    # ...and the separator is doing work: two different splits of one string
    # must not collide.
    assert (procurement.derived_id("X", "A", "BC")
            != procurement.derived_id("X", "AB", "C"))
    assert "uuid" not in code_only(inspect.getsource(procurement.derived_id)), (
        "the id is minted randomly again; a replay would land on a new row")


def test_an_unknown_external_source_is_refused_not_defaulted_to_erp():
    """C17 forbids a cross-product fallback: a Books value is resolved against
    Books rows or not at all. Defaulting an unrecognised source to ERP would
    map a Books status against ERP's rows silently."""
    with pytest.raises(procurement.ProcurementIngestError) as exc:
        procurement._adapter_product_of("SAP_ARIBA")
    assert exc.value.code == "UNKNOWN_EXTERNAL_SOURCE"
    assert procurement._adapter_product_of("ZOHO_ERP") == "ERP"
    assert procurement._adapter_product_of("ZOHO_BOOKS") == "BOOKS_INVENTORY"


def test_the_uninterpretable_status_is_not_accounting_effective():
    """AUD-C-004: only `Approved` and `Reversal` are accounting-effective.

    A document whose status C17 cannot interpret must not become one finance
    acts on, and `bill.accounting_status` DEFAULTS to `Approved` -- so falling
    through to the schema default is precisely the failure. It lands on a value
    that is neither effective nor itself a claim.
    """
    assert (procurement.UNINTERPRETABLE_ACCOUNTING_STATUS
            not in procurement.ACCOUNTING_EFFECTIVE)
    assert (procurement.UNINTERPRETABLE_ACCOUNTING_STATUS
            in procurement.ACCOUNTING_STATUSES)
    assert procurement.ACCOUNTING_EFFECTIVE == frozenset({"Approved", "Reversal"})


def test_every_statement_in_this_module_carries_a_scope_token():
    """`repo.query` raises without it, but only when the statement RUNS.

    `tests/test_scope_enforcement.py` proves no scopable table is read OFF the
    chokepoint; this proves every statement that IS on it carries the token and
    names all four dimensions, which is the half that would otherwise be
    discovered at runtime.
    """
    tree = ast.parse(SOURCE)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in {"query", "query_one"}]
    assert len(calls) >= 6, "the statements moved; this gate has to move with them"
    for call in calls:
        sql = ast.unparse(call.args[1])
        assert "{scope}" in sql, (
            f"the repo call at line {call.lineno} has no scope token")
        keywords = {k.arg for k in call.keywords}
        assert "columns" in keywords, (
            f"the repo call at line {call.lineno} passes no columns= mapping, "
            f"so `compile_scope` cannot express a restricted dimension and "
            f"raises ScopeNotExpressible at runtime")
    for name in ("entity", "plant", "location", "project"):
        assert procurement.SCOPE_COLUMNS[name] is not None, (
            f"the {name} dimension is waived. 013's own policy calls "
            f"capex_scope_permits with all four; an application layer that "
            f"waives one is weaker than the policy it mirrors.")


def test_every_sql_placeholder_has_a_parameter_behind_it():
    """`%(name)s` with nothing bound to it is a `ProgrammingError` raised when
    the statement RUNS -- which for this module is inside a cron function, in
    CI at the earliest. There is no live PostgreSQL on the machine this was
    written on, so this is the only place the mismatch can be caught before
    then.

    Both directions. A missing parameter fails the call outright; an unused one
    is dead weight that reads like a filter somebody thinks is applied.

    Two call sites build their params dict above the call rather than inline --
    `record_receive_line` reuses one mapping across an UPDATE and an INSERT,
    which is the point of it -- so those are resolved from the enclosing
    function's `params = {...}` assignment rather than waved through.

    THE UNUSED-PARAMETER HALF IS CHECKED PER MAPPING, NOT PER CALL, and the
    difference matters exactly once: the shared mapping above feeds an UPDATE
    that binds seven of its keys and an INSERT that binds all eleven. Per call,
    the UPDATE reports four dead parameters that are not dead. So the rule is
    "every key is bound by at least one statement that uses this mapping",
    which still fails a key no statement binds -- the thing the rule is for.
    """
    tree = ast.parse(SOURCE)
    checked = 0
    for function in [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # Every literal `params = {...}` / `x = {...}` in this function, so a
        # call passing a name can be resolved to the keys it was built with.
        local: dict[str, set[str]] = {}
        for node in ast.walk(function):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Dict)):
                local[node.targets[0].id] = {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)}
        bound_by_mapping: dict[str, set[str]] = {name: set() for name in local}
        for call in ast.walk(function):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"query", "query_one"}):
                continue
            sql = ast.unparse(call.args[1])
            placeholders = set(re.findall(r"%\((\w+)\)s", sql))
            node = call.args[2] if len(call.args) > 2 else None
            if isinstance(node, ast.Dict):
                supplied = {k.value for k in node.keys
                            if isinstance(k, ast.Constant)}
                unused = supplied - placeholders
                assert unused == set(), (
                    f"line {call.lineno}: parameter(s) {sorted(unused)} are "
                    f"bound but never referenced; dead weight that reads like "
                    f"a filter somebody thinks is being applied")
            elif isinstance(node, ast.Name):
                supplied = local.get(node.id, set())
                assert supplied, (
                    f"the repo call at line {call.lineno} passes params by "
                    f"name {node.id!r} and this gate cannot resolve it")
                bound_by_mapping[node.id] |= placeholders
            else:
                supplied = set()
            checked += 1
            assert placeholders - supplied == set(), (
                f"line {call.lineno}: SQL binds "
                f"{sorted(placeholders - supplied)} with no matching parameter")
        for name, bound in bound_by_mapping.items():
            if not bound:
                continue
            dead = local[name] - bound
            assert dead == set(), (
                f"{function.name}: {name}[{sorted(dead)}] is bound by no "
                f"statement in this function at all")
    assert checked >= 6, "the statements moved; this gate has to move with them"


def test_every_writing_statement_returns_a_row():
    """`repo.query` FETCHES. psycopg raises on a cursor with no result set, so
    an INSERT or UPDATE issued through the chokepoint without a `RETURNING`
    clause fails at runtime -- and only at runtime."""
    tree = ast.parse(SOURCE)
    for call in [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr in {"query", "query_one"}]:
        sql = ast.unparse(call.args[1])
        if re.search(r"\b(INSERT|UPDATE|DELETE)\b", sql):
            assert "RETURNING" in sql, (
                f"line {call.lineno}: a writing statement with no RETURNING "
                f"clause; repo.query fetches and psycopg will raise")


def test_the_sweepstore_surface_no_longer_refuses():
    """The five that used to raise `SchemaNotYetMigrated` are implemented.

    `tests/test_pg_reconciliation.py` parametrises its refusal tests over
    `UNBACKED_SWEEP_SURFACE`, which is now empty -- so those tests have no
    subjects left and this is what carries the fact forward. The list itself is
    kept: the day a sixth call arrives ahead of its schema, one entry brings
    them back.
    """
    assert store.UNBACKED_SWEEP_SURFACE == {}
    for name in ("resolve_po_line", "record_receive_line",
                 "accumulate_unattributed", "bills_awaiting_detail",
                 "mark_detail_hydrated"):
        source = inspect.getsource(getattr(store, name))
        assert "raise SchemaNotYetMigrated" not in source, (
            f"{name} still refuses; 013 gave it a table")
    # ...and the type survives, because the mechanism is not the list.
    assert issubclass(store.SchemaNotYetMigrated, store.IntegrationStoreError)


def test_a_null_line_identifier_never_reaches_a_po_line_lookup():
    """`ux_po_line_external` is PARTIAL on ``line_external_id IS NOT NULL``.

    Matching a NULL against `po_line` would be matching against rows the index
    deliberately does not police -- and on ERP, where receive lines routinely
    carry no line identifier at all, it would attribute the whole population to
    whichever local line happened to be NULL first. The short-circuit returns
    before any statement is issued, which is asserted with a recorder rather
    than inferred.
    """
    class _Recorder:
        scope = Scope(user_id="U", entity_ids=frozenset({ENTITY}))

        def __init__(self):
            self.statements = []

        def fetchall(self, statement, params=None):
            self.statements.append(statement)
            return []

        def fetchone(self, statement, params=None):
            self.statements.append(statement)
            return None

    session = _Recorder()
    assert procurement.resolve_po_line(
        session, po_external_id=PO_EXTERNAL, line_external_id=None) is None
    assert procurement.resolve_po_line(
        session, po_external_id=PO_EXTERNAL, line_external_id="  ") is None
    assert session.statements == [], (
        "a null line id reached the database; the partial index makes that "
        "match rows it does not police")


# =========================================================================
# LIVE. Gated on CAPEX_DB_URL; first executes in CI.
# =========================================================================
def _seed(con: psycopg.Connection) -> None:
    """Two entities and two projects, one purchase order with one line.

    TWO of each, deliberately. A single entity makes every scope assertion
    vacuous: a query that ignores its predicate entirely still returns the
    right rows when there is only one entity's worth of them.
    """
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES (%s, 'ORGI', 'Ingest Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING", (ORG,))
    for entity, project, suffix in ((ENTITY, PROJECT, "a"),
                                    (OTHER_ENTITY, OTHER_PROJECT, "b")):
        con.execute(
            "INSERT INTO entity (entity_id, organisation_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (entity, ORG, entity, entity))
        con.execute(
            "INSERT INTO project (project_id, entity_id, capex_code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING", (project, entity, project, project))
        con.execute(
            "INSERT INTO budget_head (budget_head_id, entity_id, code, name,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'T', 'T')"
            " ON CONFLICT DO NOTHING",
            (f"{HEAD}-{suffix}", entity, f"{HEAD}-{suffix}", HEAD))
        con.execute(
            # `wbs_path` is `ltree`, whose labels admit only [A-Za-z0-9_], so
            # the hyphenated id cannot be the path.
            "INSERT INTO wbs_element (wbs_id, project_id, wbs_code,"
            " description, wbs_path, created_by, updated_by)"
            " VALUES (%s, %s, %s, %s, %s, 'T', 'T') ON CONFLICT DO NOTHING",
            (f"{WBS}-{suffix}", project, f"{WBS}-{suffix}", WBS,
             f"ing{suffix}"))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-SWEEP', 'sweep@example.test',"
        " 'Sweep service', 'SERVICE', 'T', 'T') ON CONFLICT DO NOTHING")
    con.execute(
        "INSERT INTO purchase_order (po_id, po_number, project_id,"
        " vendor_name, external_source, external_id, created_by, updated_by)"
        " VALUES ('PO-ING', 'PO-NUM-ING', %s, 'Vendor', %s, %s, 'T', 'T')"
        " ON CONFLICT DO NOTHING", (PROJECT, SOURCE_LABEL, PO_EXTERNAL))
    con.execute(
        "INSERT INTO po_line (po_line_id, po_id, line_no, project_id, wbs_id,"
        " budget_head_id, rate_paise, amount_paise, line_external_id,"
        " created_by, updated_by)"
        " VALUES ('POL-ING', 'PO-ING', 1, %s, %s, %s, 100000, %s, %s, 'T', 'T')"
        " ON CONFLICT DO NOTHING",
        (PROJECT, f"{WBS}-a", f"{HEAD}-a", ORDERED_PAISE, POL_EXTERNAL))
    con.commit()


@pytest.fixture()
def seeded(pg_connection):
    _seed(pg_connection)
    return pg_connection


def _session(con: psycopg.Connection) -> Session:
    """A RESTRICTED session, never `Scope.system()`.

    An unrestricted scope compiles the predicate to the literal ``TRUE``, so
    every scoped statement in this file would pass whether or not its scope
    join is correct.
    """
    return Session(connection=con,
                   scope=Scope(user_id="U-ING",
                               entity_ids=frozenset({ENTITY})))


def _receive(session, *, receive_id: str, line_id: str | None,
             quantity: str, paise: int, number: str | None = None) -> None:
    procurement.record_receive_line(
        session, po_line_id="POL-ING", receive_external_id=receive_id,
        line_external_id=line_id, quantity=quantity, amount_paise=paise,
        external_source=SOURCE_LABEL, receive_number=number,
        received_at=T0, external_last_modified=T0,
        payload_sha=f"sha-{receive_id}-{line_id}")


def _grn_lines(con) -> list[tuple]:
    return con.execute(
        "SELECT gl.po_line_id, gl.receive_external_id, gl.line_external_id,"
        " gl.quantity, gl.amount_paise FROM grn_line gl"
        " ORDER BY gl.receive_external_id, gl.line_external_id").fetchall()


# ------------------------------------------------------------ receipts
@PG
def test_a_partial_receipt_is_mirrored_at_the_quantity_that_arrived(seeded):
    """A partial receipt is an ordinary document, not a special case. Three
    hundred thousand paise of a ten-lakh line arrived; three hundred thousand
    is what is stored, un-grossed-up and un-apportioned."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="0.3", paise=300_000)
    seeded.commit()

    rows = _grn_lines(seeded)
    assert len(rows) == 1
    po_line_id, receive_id, line_id, quantity, paise = rows[0]
    assert (po_line_id, receive_id, line_id) == ("POL-ING", "RCV-1", POL_EXTERNAL)
    assert int(paise) == 300_000
    assert float(quantity) == pytest.approx(0.3)
    # `bigint` in, `int` out -- asserted, not assumed. No in-memory double can
    # ever disagree about psycopg's type mapping, which is why this is here.
    assert isinstance(paise, int) and not isinstance(paise, bool)


@PG
def test_two_receipts_against_one_po_line_are_two_rows(seeded):
    """Multiple receipts against one PO line is the normal case on a staged
    delivery. `ux_grn_line_external` keys on the RECEIVE id as well as the
    line, so the second receipt is a second row and not an overwrite of the
    first -- an overwrite would silently lose the first delivery."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="0.3", paise=300_000)
    _receive(session, receive_id="RCV-2", line_id=POL_EXTERNAL,
             quantity="0.7", paise=700_000)
    seeded.commit()

    rows = _grn_lines(seeded)
    assert len(rows) == 2
    assert [int(r[4]) for r in rows] == [300_000, 700_000]
    assert sum(int(r[4]) for r in rows) == ORDERED_PAISE
    # Two receipts, and exactly two GRN headers -- one per source document.
    assert seeded.execute("SELECT count(*) FROM grn").fetchone()[0] == 2


@PG
def test_replaying_both_receipts_duplicates_neither(seeded):
    """THE RE-WALK IS THE DESIGN, not a fault. `SweepPoAnchored` resumes from a
    checkpoint on a 300-second overlap and a CYCLING cursor -- it wraps to the
    beginning deliberately, because a deleted receive arrives as an absence and
    only a re-read detects it. So every receive line arrives again, and again,
    for as long as the deployment runs."""
    session = _session(seeded)
    for _pass in range(3):
        _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
                 quantity="0.3", paise=300_000)
        _receive(session, receive_id="RCV-2", line_id=POL_EXTERNAL,
                 quantity="0.7", paise=700_000)
    seeded.commit()

    rows = _grn_lines(seeded)
    assert len(rows) == 2, "the replay duplicated a receipt"
    assert sum(int(r[4]) for r in rows) == ORDERED_PAISE, (
        "three passes tripled the received value; the conflict target is not "
        "matching ux_grn_line_external")
    assert seeded.execute("SELECT count(*) FROM grn").fetchone()[0] == 2
    # The row was UPDATED rather than re-inserted, so its version moved. This
    # is what distinguishes "the upsert worked" from "the insert was skipped".
    versions = [v for (v,) in seeded.execute(
        "SELECT version_no FROM grn_line ORDER BY receive_external_id")]
    assert versions == [3, 3]


@PG
def test_a_reversal_receipt_carries_negative_quantity_and_negative_paise(seeded):
    """The POC's own seed contains a receive line at ``-0.2 / -1,20,000``. A
    `>= 0` CHECK on `grn_line` would reject the reversal contract itself, and
    013 deliberately has none. Nothing here takes an absolute value."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000)
    _receive(session, receive_id="RCV-REV", line_id=POL_EXTERNAL,
             quantity="-0.2", paise=-120_000)
    seeded.commit()

    rows = _grn_lines(seeded)
    assert len(rows) == 2
    paise = sorted(int(r[4]) for r in rows)
    assert paise == [-120_000, 1_000_000]
    assert sum(paise) == 880_000, "the reversal was dropped or made positive"


@PG
def test_provenance_is_written_on_the_mirrored_grn(seeded):
    """§11.10 requires the SOURCE DOCUMENT to be recoverable, not our rendering
    of it. A row that names neither its source, nor which VERSION of the source
    it was built from, is a row nobody can trace back."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000, number="GRN-VENDOR-77")
    seeded.commit()

    row = seeded.execute(
        "SELECT grn_number, external_source, external_id,"
        " external_last_modified, payload_sha, received_at, is_reversal"
        " FROM grn").fetchone()
    (number, source, external_id, last_modified, sha, received, reversal) = row
    assert number == "GRN-VENDOR-77"
    assert (source, external_id) == (SOURCE_LABEL, "RCV-1")
    assert last_modified == T0 and received == T0
    assert sha == f"sha-RCV-1-{POL_EXTERNAL}"
    assert reversal is False
    # `grn.status` is NOT written from a raw external value: 013 gives the
    # table no `external_status_raw` column, so the verbatim copy stays in the
    # inbox and the domain column keeps its default rather than taking a guess.
    assert seeded.execute("SELECT status FROM grn").fetchone()[0] == "Approved"


@PG
def test_a_receive_line_for_an_out_of_scope_po_line_is_refused_not_skipped(seeded):
    """A receipt silently not mirrored is the drop §11.8 forbids. The refusal
    names the po_line, and nothing is written."""
    session = Session(connection=seeded,
                      scope=Scope(user_id="U-ING",
                                  entity_ids=frozenset({OTHER_ENTITY})))
    with pytest.raises(store.IntegrationStoreError) as exc:
        _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
                 quantity="1", paise=1_000_000)
    assert exc.value.code == "PO_LINE_NOT_FOUND"
    seeded.rollback()
    assert seeded.execute("SELECT count(*) FROM grn_line").fetchone()[0] == 0


@PG
@pytest.mark.parametrize("bad,code", [
    ({"quantity": None}, "RECEIVE_LINE_QUANTITY_MISSING"),
    ({"amount_paise": None}, "RECEIVE_LINE_AMOUNT_MISSING"),
])
def test_a_missing_quantity_or_amount_refuses_rather_than_defaulting(
        seeded, bad, code):
    """Zero quantity says "nothing arrived" and zero paise says "the goods were
    free". Both are claims about a document, not missing-value conventions."""
    session = _session(seeded)
    kwargs = {"receive_id": "RCV-1", "line_id": POL_EXTERNAL,
              "quantity": "1", "paise": 1_000_000}
    kwargs.update({"quantity" if "quantity" in bad else "paise":
                   list(bad.values())[0]})
    with pytest.raises(store.IntegrationStoreError) as exc:
        _receive(session, **kwargs)
    assert exc.value.code == code
    seeded.rollback()
    assert seeded.execute("SELECT count(*) FROM grn_line").fetchone()[0] == 0


# ------------------------------------------------- unattributed quarantine
@PG
def test_an_unmatched_receive_line_resolves_to_nothing_and_is_quarantined(seeded):
    """§11.8, end to end: no attribution, no pro-rata, no drop.

    The line is held at FULL value in an exception whose kind is the frozen
    `GRN_LINE_UNATTRIBUTED`, and `open_exception_exposure` -- the figure the
    capitalisation gate quotes -- reports it.
    """
    session = _session(seeded)
    assert procurement.resolve_po_line(
        session, po_external_id=PO_EXTERNAL,
        line_external_id="ZPOL-NOT-OURS") is None

    exception_id = store.raise_exception(
        session, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
        object_id="RCV-9:ZPOL-NOT-OURS",
        detail="Receive RCV-9 line ZPOL-NOT-OURS does not resolve to a known "
               "po_line. Quarantined at full value; never spread pro-rata.",
        raised_at=T0, entity_id=ENTITY, project_id=PROJECT,
        source_paise=450_000)
    store.accumulate_unattributed(session, project_id=PROJECT, paise=450_000,
                                  source_key=exception_id)
    seeded.commit()

    row = seeded.execute(
        "SELECT kind, status, source_paise, project_id FROM"
        " reconciliation_exception WHERE exception_id = %s",
        (exception_id,)).fetchone()
    assert row == ("GRN_LINE_UNATTRIBUTED", "Open", 450_000, PROJECT)
    exposure = store.open_exception_exposure(session, project_id=PROJECT)
    assert exposure["open_count"] == 1
    assert exposure["source_paise"] == 450_000
    assert isinstance(exposure["source_paise"], int), (
        "SUM() over bigint returned a Decimal; the ::bigint cast is gone")
    # Nothing was written to the ledger for it. That is the point: the value is
    # held in the exception, not attributed to a cell nobody chose.
    assert seeded.execute("SELECT count(*) FROM grn_line").fetchone()[0] == 0


@PG
def test_a_rewalk_sets_the_bucket_and_never_inflates_it(seeded):
    """The defect that has been found here once, asserted live as well as in
    the source. Ten passes, one value."""
    session = _session(seeded)
    exception_id = store.raise_exception(
        session, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
        object_id="RCV-9:#0", detail="unattributed", raised_at=T0,
        entity_id=ENTITY, project_id=PROJECT, source_paise=450_000)
    for _pass in range(10):
        store.accumulate_unattributed(session, project_id=PROJECT,
                                      paise=450_000, source_key=exception_id)
    seeded.commit()

    assert store.open_exception_exposure(
        session, project_id=PROJECT)["source_paise"] == 450_000, (
        "ten re-walks inflated the bucket; it is being incremented, and the "
        "figure that blocks capitalisation has become fiction")


@PG
def test_a_negative_unattributed_line_is_held_at_its_magnitude(seeded):
    """A return's receive line is genuinely negative, and
    `ck_reconciliation_exception_paise` forbids a negative on either side --
    "these are magnitudes of two sides, and a sign would silently encode a
    direction the `kind` is supposed to carry".

    So the column takes the magnitude, which IS the full value §11.8 demands,
    and the SIGNED original goes to the audit event. Nothing is dropped and
    nothing is spread.
    """
    session = _session(seeded)
    exception_id = store.raise_exception(
        session, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
        object_id="RCV-RET:#0", detail="return, unattributed", raised_at=T0,
        entity_id=ENTITY, project_id=PROJECT, source_paise=-120_000)
    store.accumulate_unattributed(session, project_id=PROJECT, paise=-120_000,
                                  source_key=exception_id)
    seeded.commit()

    assert seeded.execute(
        "SELECT source_paise FROM reconciliation_exception WHERE"
        " exception_id = %s", (exception_id,)).fetchone()[0] == 120_000
    signed = seeded.execute(
        "SELECT detail ->> 'source_paise_signed' FROM integration_event"
        " WHERE kind = 'reconciliation.unattributed.accumulated'").fetchone()
    assert signed is not None and int(signed[0]) == -120_000, (
        "the direction was lost; the magnitude is all that survived")


@PG
def test_a_bucket_key_naming_no_open_exception_is_refused(seeded):
    """A bucket entry keyed on a row that does not exist holds the value
    nowhere anybody triages -- the silent drop, one layer down."""
    session = _session(seeded)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.accumulate_unattributed(session, project_id=PROJECT,
                                      paise=450_000, source_key="EXC-NOPE")
    assert exc.value.code == "UNATTRIBUTED_BUCKET_KEY_UNKNOWN"


# ----------------------------------------------------------------- bills
def _bill(session, *, external_id, number, lines, doc_type="BILL",
          status_raw=None, po=PO_EXTERNAL, accounting_status="Approved"):
    return procurement.mirror_bill(
        session, external_source=SOURCE_LABEL, external_id=external_id,
        bill_number=number, vendor_name="Vendor", bill_date=date(2026, 9, 7),
        lines=lines, po_external_id=po, entity_id=ENTITY, doc_type=doc_type,
        accounting_status=accounting_status,
        external_status_raw=status_raw, external_last_modified=T0,
        payload_sha=f"sha-{external_id}", now=T0, status_as_of=AS_OF)


def _line(paise, *, line_id=POL_EXTERNAL, quantity="1"):
    return {"purchase_order_line_external_id": line_id,
            "line_total_paise": paise, "quantity": quantity}


@PG
def test_a_partial_bill_then_a_final_bill_are_two_lines_on_one_po_line(seeded):
    """Partial billing is ordinary. Two bills against one PO line, each
    attributed to that line's own control cell -- read FROM the PO line, never
    accepted from the caller, because `fk_bill_line_po_line_cell` is a
    four-column FK and the four columns ARE the cell."""
    session = _session(seeded)
    first = _bill(session, external_id="ZB-1", number="BILL-1",
                  lines=[_line(400_000, quantity="0.4")])
    second = _bill(session, external_id="ZB-2", number="BILL-2",
                   lines=[_line(600_000, quantity="0.6")])
    seeded.commit()

    assert (first["attributed"], first["quarantined"]) == (1, 0)
    assert (second["attributed"], second["quarantined"]) == (1, 0)
    rows = seeded.execute(
        "SELECT bl.po_line_id, bl.wbs_id, bl.budget_head_id, bl.amount_paise"
        " FROM bill_line bl ORDER BY bl.amount_paise").fetchall()
    assert [int(r[3]) for r in rows] == [400_000, 600_000]
    for po_line_id, wbs_id, head_id, _paise in rows:
        assert (po_line_id, wbs_id, head_id) == ("POL-ING", f"{WBS}-a",
                                                 f"{HEAD}-a")
    recon = procurement.reconcile_po_lines(session, project_id=PROJECT)
    assert len(recon) == 1
    assert recon[0]["billed_paise"] == ORDERED_PAISE
    assert recon[0]["open_paise"] == 0
    assert recon[0]["over_billed"] is False


@PG
def test_replaying_a_bill_duplicates_no_line(seeded):
    """`bill_line` has NO external unique index in 013, so the primary key is
    the only conflict target -- and it is DERIVED from `(bill_id, line_key)`
    for exactly that reason. `capex_app` has DELETE revoked, so a duplicated
    line could not be cleaned up afterwards."""
    session = _session(seeded)
    for _pass in range(3):
        _bill(session, external_id="ZB-1", number="BILL-1",
              lines=[_line(400_000, quantity="0.4")])
    seeded.commit()

    assert seeded.execute("SELECT count(*) FROM bill").fetchone()[0] == 1
    assert seeded.execute("SELECT count(*) FROM bill_line").fetchone()[0] == 1
    assert seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM bill_line").fetchone()[0] == 400_000


@PG
def test_over_billing_is_visible_and_never_clamped(seeded):
    """A ten-lakh PO line billed at fourteen lakh.

    The over-bill is NOT clamped to the ordered value, NOT rejected and NOT
    netted away. Clamping would make the ledger agree with the budget by lying
    about the invoice; `reconcile_po_lines` puts ordered, received and billed
    side by side and leaves the difference where finance can see it.
    """
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=ORDERED_PAISE)
    _bill(session, external_id="ZB-1", number="BILL-1",
          lines=[_line(1_000_000)])
    _bill(session, external_id="ZB-2", number="BILL-2",
          lines=[_line(400_000)])
    seeded.commit()

    assert seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM bill_line").fetchone()[0] == 1_400_000

    recon = procurement.reconcile_po_lines(session, project_id=PROJECT)[0]
    assert recon["ordered_paise"] == ORDERED_PAISE
    assert recon["received_paise"] == ORDERED_PAISE
    assert recon["billed_paise"] == 1_400_000
    assert recon["over_billed"] is True
    assert recon["over_billed_paise"] == 400_000
    # `open` is floored at zero rather than going negative: one signed number
    # meaning both "not yet billed" and "billed too much" is a number finance
    # cannot read.
    assert recon["open_paise"] == 0
    for key in ("ordered_paise", "received_paise", "billed_paise",
                "over_billed_paise"):
        assert isinstance(recon[key], int), (
            f"{key} came back as {type(recon[key]).__name__}; a SUM lost its "
            f"::bigint cast and psycopg handed us a Decimal")


@PG
def test_a_credit_note_carries_negative_paise_and_reduces_the_billed_total(seeded):
    """A credit note is an ordinary document with a `doc_type` of its own and
    genuinely negative line amounts. `bill_line.amount_paise` has no `>= 0`
    CHECK precisely so it can, and nothing here takes an absolute value."""
    session = _session(seeded)
    _bill(session, external_id="ZB-1", number="BILL-1", lines=[_line(1_000_000)])
    credit = _bill(session, external_id="ZB-CN", number="CN-1",
                   doc_type="CREDIT_NOTE", lines=[_line(-250_000)])
    seeded.commit()

    assert credit["attributed"] == 1
    assert seeded.execute(
        "SELECT doc_type FROM bill WHERE bill_number = 'CN-1'").fetchone()[0] \
        == "CREDIT_NOTE"
    assert seeded.execute(
        "SELECT min(amount_paise) FROM bill_line").fetchone()[0] == -250_000
    recon = procurement.reconcile_po_lines(session, project_id=PROJECT)[0]
    assert recon["billed_paise"] == 750_000
    assert recon["open_paise"] == 250_000
    assert recon["over_billed"] is False


@PG
def test_an_unattributable_bill_line_is_quarantined_not_spread(seeded):
    """Two lines, one resolvable and one not. The resolvable one is written at
    its own value; the other is held at FULL value in an exception.

    NOT spread across the line that did resolve, which is what pro-rata would
    do and what §11.8 forbids by name -- and NOT dropped, which is what a
    `bill_line` with NOT NULL `wbs_id` would otherwise force.
    """
    session = _session(seeded)
    result = _bill(session, external_id="ZB-1", number="BILL-1", lines=[
        _line(600_000),
        _line(400_000, line_id="ZPOL-NOT-OURS"),
    ])
    seeded.commit()

    assert result["attributed"] == 1
    assert result["quarantined"] == 1
    assert result["quarantined_paise"] == 400_000

    written = seeded.execute(
        "SELECT amount_paise FROM bill_line").fetchall()
    assert [int(r[0]) for r in written] == [600_000], (
        "the unattributable line was written to a cell nobody chose, or its "
        "value was spread over the line that did resolve")

    kind, paise, status = seeded.execute(
        "SELECT kind, source_paise, status FROM reconciliation_exception"
        " WHERE exception_id = %s", (result["exception_ids"][0],)).fetchone()
    assert (kind, int(paise), status) == ("CONTROL_TOTAL_MISMATCH", 400_000,
                                          "Open")
    # An Open exception blocks capitalisation, and the full value is what it
    # blocks with.
    assert store.open_exception_exposure(
        session, project_id=PROJECT)["source_paise"] == 400_000


@PG
def test_a_bill_naming_no_purchase_order_and_no_project_is_refused(seeded):
    """`bill.project_id` is NOT NULL and there is nothing to derive it from.
    Inferring it from whichever line happened to resolve first would attribute
    a whole document on the strength of one line."""
    session = _session(seeded)
    with pytest.raises(procurement.ProcurementIngestError) as exc:
        procurement.mirror_bill(
            session, external_source=SOURCE_LABEL, external_id="ZB-ORPHAN",
            bill_number="BILL-ORPHAN", vendor_name="Vendor",
            bill_date=date(2026, 9, 7), lines=[_line(100)], now=T0)
    assert exc.value.code == "BILL_PROJECT_UNKNOWN"
    seeded.rollback()
    assert seeded.execute("SELECT count(*) FROM bill").fetchone()[0] == 0


@PG
def test_a_bill_against_an_unknown_purchase_order_is_refused(seeded):
    """Mirroring it under an invented header would hide the very thing
    `poll_purchaseorders` raises UNSANCTIONED_COMMITMENT for."""
    session = _session(seeded)
    with pytest.raises(procurement.ProcurementIngestError) as exc:
        _bill(session, external_id="ZB-1", number="BILL-1",
              lines=[_line(100)], po="ZPO-NEVER-SEEN")
    assert exc.value.code == "PURCHASE_ORDER_NOT_FOUND"


# ------------------------------------------------------ external statuses
@PG
def test_a_mapped_external_status_is_applied_from_c17_not_guessed(seeded):
    """`void` is a DOCUMENTED_RAW_VALUE in C17's ERP bill block and maps to the
    accounting status `Void`. Applied from the registry, and no exception."""
    session = _session(seeded)
    result = _bill(session, external_id="ZB-V", number="BILL-V",
                   lines=[_line(100_000)], status_raw="void")
    seeded.commit()

    assert result["accounting_status"] == "Void"
    assert result["accounting_effective"] is False
    assert result["status_exception_id"] is None
    assert seeded.execute(
        "SELECT accounting_status FROM bill WHERE bill_number = 'BILL-V'"
    ).fetchone()[0] == "Void"


@PG
@pytest.mark.parametrize("raw,why", [
    ("cancelled_by_vendor", "no row exists for this value at all"),
    # `draft` HAS a row in C17's ERP bill block, and it is INACTIVE with
    # evidence class UNVERIFIED. An unsourced spelling is not a mapping, so it
    # must fail exactly as an absent row does -- which is the harder half, and
    # the one an implementation is most likely to get wrong by reading only
    # `raw in rows`.
    ("draft", "the row is INACTIVE (UNVERIFIED - REQUIRES ZOHO CONFIRMATION)"),
])
def test_an_unmapped_external_status_is_quarantined_and_never_guessed(
        seeded, raw, why):
    """C17: the record is ACCEPTED, the raw value preserved, and an
    `UNMAPPED_EXTERNAL_STATUS` raised. What must NOT happen is the fall-through
    to `bill.accounting_status`'s schema DEFAULT of `Approved`, which
    AUD-C-004 makes accounting-effective -- a value nobody could interpret
    becoming a document finance acts on.
    """
    session = _session(seeded)
    result = _bill(session, external_id="ZB-U", number="BILL-U",
                   lines=[_line(100_000)], status_raw=raw)
    seeded.commit()

    assert result["status_exception_id"] is not None, (
        f"{raw!r} was interpreted; {why}")
    assert result["accounting_status"] == "Draft"
    assert result["accounting_effective"] is False, (
        "an uninterpretable status became accounting-effective")

    kind, status, detail = seeded.execute(
        "SELECT kind, status, detail FROM reconciliation_exception"
        " WHERE exception_id = %s",
        (result["status_exception_id"],)).fetchone()
    assert (kind, status) == ("UNMAPPED_EXTERNAL_STATUS", "Open")
    assert raw in detail, "the exception does not name the raw value"

    # The document itself was ACCEPTED -- header and line both. Refusing it
    # would be the drop; accepting it silently as Approved would be the guess.
    assert seeded.execute(
        "SELECT accounting_status FROM bill WHERE bill_number = 'BILL-U'"
    ).fetchone()[0] == "Draft"
    assert seeded.execute("SELECT count(*) FROM bill_line").fetchone()[0] == 1


@PG
def test_a_missing_external_status_raises_nothing(seeded):
    """`None` means the source sent no status at all. There is nothing to
    interpret and therefore nothing to quarantine -- treating an absence as an
    unmapped value would raise an exception for every product that does not
    send the field, and every one of them blocks period close."""
    session = _session(seeded)
    result = _bill(session, external_id="ZB-N", number="BILL-N",
                   lines=[_line(100_000)], status_raw=None)
    seeded.commit()
    assert result["status_exception_id"] is None
    assert result["accounting_status"] == "Approved"


# ------------------------------------------------------- hydration queue
@PG
def test_the_hydration_queue_holds_a_line_less_bill_and_releases_it(seeded):
    """A list response omits `line_items`, so a bill arrives with no lines and
    is queued for a detail fetch. Once a payload carrying lines lands in the
    inbox the bill leaves the queue by itself; the marker is what covers the
    bill that genuinely HAS no lines, so it is not re-fetched every fifteen
    minutes for the rest of the deployment."""
    connection_id = "CONN-ING"
    seeded.execute(
        "INSERT INTO integration_connection (connection_id, entity_id,"
        " product, dc, organization_id, connector_name, created_by,"
        " updated_by)"
        " VALUES (%s, %s, 'ERP', 'in', 'ORG1', 'zoho-erp', 'T', 'T')",
        (connection_id, ENTITY))
    seeded.commit()
    session = _session(seeded)

    store.record_inbound(session, inbox_id="IN-1", connection_id=connection_id,
                         module="bills", external_id="ZB-1",
                         raw_payload={"bill_id": "ZB-1", "status": "open"},
                         now=T0)
    seeded.commit()
    assert store.bills_awaiting_detail(
        session, connection_id=connection_id, limit=10) == ["ZB-1"]

    # The detail arrives, carrying lines. That alone drains the queue.
    store.record_inbound(
        session, inbox_id="IN-2", connection_id=connection_id, module="bills",
        external_id="ZB-1",
        raw_payload={"bill_id": "ZB-1", "status": "open",
                     "line_items": [{"line_item_id": POL_EXTERNAL}]},
        now=T0)
    store.mark_detail_hydrated(session, connection_id=connection_id,
                               external_id="ZB-1")
    seeded.commit()
    assert store.bills_awaiting_detail(
        session, connection_id=connection_id, limit=10) == []


@PG
def test_a_line_less_bill_is_not_refetched_for_ever(seeded):
    """`sweep_bill_detail` raises CONTROL_TOTAL_MISMATCH for a bill whose
    detail fetch returned no lines. Without the marker, that bill would be
    re-fetched on every sweep -- one call each, for ever, against the rate
    budget that is the binding constraint on ERP Standard."""
    connection_id = "CONN-ING"
    seeded.execute(
        "INSERT INTO integration_connection (connection_id, entity_id,"
        " product, dc, organization_id, connector_name, created_by,"
        " updated_by)"
        " VALUES (%s, %s, 'ERP', 'in', 'ORG1', 'zoho-erp', 'T', 'T')",
        (connection_id, ENTITY))
    seeded.commit()
    session = _session(seeded)
    store.record_inbound(session, inbox_id="IN-1", connection_id=connection_id,
                         module="bills", external_id="ZB-EMPTY",
                         raw_payload={"bill_id": "ZB-EMPTY"}, now=T0)
    seeded.commit()
    assert store.bills_awaiting_detail(
        session, connection_id=connection_id, limit=10) == ["ZB-EMPTY"]

    store.mark_detail_hydrated(session, connection_id=connection_id,
                               external_id="ZB-EMPTY")
    seeded.commit()
    assert store.bills_awaiting_detail(
        session, connection_id=connection_id, limit=10) == [], (
        "a bill that genuinely has no line items is still queued; it will be "
        "re-fetched on every sweep for ever")


@PG
def test_an_invisible_connection_raises_rather_than_reporting_an_empty_queue(
        seeded):
    """`sweep_bill_detail` reads an empty list as "queue drained" and reports
    success. An out-of-scope connection answered with `[]` would leave every
    bill unhydrated -- no lines, no WBS attribution, zero CWIP booked, green
    build."""
    session = _session(seeded)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.bills_awaiting_detail(session, connection_id="CONN-NOPE",
                                    limit=10)
    assert exc.value.code == "CONNECTION_NOT_FOUND"
    assert exc.value.status == 404


@PG
def test_marking_a_bill_nobody_received_is_refused(seeded):
    """A hydration marker for a bill nothing received would suppress a later
    fetch of a bill we do hold: a silent drop with a fifteen-minute fuse."""
    connection_id = "CONN-ING"
    seeded.execute(
        "INSERT INTO integration_connection (connection_id, entity_id,"
        " product, dc, organization_id, connector_name, created_by,"
        " updated_by)"
        " VALUES (%s, %s, 'ERP', 'in', 'ORG1', 'zoho-erp', 'T', 'T')",
        (connection_id, ENTITY))
    seeded.commit()
    session = _session(seeded)
    with pytest.raises(store.IntegrationStoreError) as exc:
        store.mark_detail_hydrated(session, connection_id=connection_id,
                                   external_id="ZB-NEVER-SEEN")
    assert exc.value.code == "BILL_NOT_IN_INBOX"


# =========================================================================
# THE WAVE 6 ADVERSARIAL REVIEW: C-1, C-2, H-1, H-2, H-5, H-8, M-2.
#
# Every one of these is written against a defect that was VERIFIED with a
# concrete scenario, and the source-level half exists for the same reason the
# rest of this file's does: there is no PostgreSQL on the machine these were
# written on, and a check whose only coverage is a job nobody runs locally is
# a check nobody runs.
# =========================================================================
SWEEPS_SOURCE = _Path(sweeps.__file__).read_text(encoding="utf-8")
#: `domain.compute_ledger` is the frozen `C5_formulas.json` registry in code.
#: Every money formula in `procurement.py` is transcribed from it, so the
#: transcription is checked against the ORIGINAL rather than against a second
#: statement of what the original says.
DOMAIN_SOURCE = _Path(sqlite_domain.__file__).read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """One function of `procurement.py`, prose stripped.

    Use this for "the code does not do X" over single tokens. `code_only`
    joins TOKENS with newlines, so a multi-word SQL fragment cannot be found
    in its output at all -- which is what :func:`_sql` is for.
    """
    return code_only(inspect.getsource(getattr(procurement, name)))


def _sql(name: str) -> str:
    """One function of `procurement.py`, verbatim.

    The SQL assertions below are about multi-word fragments -- a join
    condition, an ON CONFLICT set list, a CASE expression -- and those survive
    only in the unmangled source. Every use of this is a POSITIVE assertion
    (this fragment is present); the negative ones stay on :data:`CODE`, or on a
    regex specific enough that a sentence of prose cannot satisfy it.
    """
    return inspect.getsource(getattr(procurement, name))


#: A SQL DELETE, as opposed to the word "delete" in a sentence explaining why
#: there is not one. `capex_app` has the privilege revoked and every row this
#: module writes is evidence, so the whole module is held to it.
_DELETE_STATEMENT = re.compile(r"\bDELETE\s+FROM\b", re.I)


# ------------------------------------------------------- C-1 (CRITICAL)
def test_the_bill_detail_sweep_writes_the_ledger_and_not_only_the_inbox():
    """`mirror_bill` had NO CALLER, so `billed_paise` was structurally 0.

    `grep -rn "mirror_bill" app/` found only the definition. The bill-detail
    sweep fetched `GET /bills/{id}`, wrote `upsert_inbox`, raised an exception
    when there were no line items, called `mark_detail_hydrated` -- and
    stopped. It never wrote `bill` or `bill_line`, and `SweepStore` declared no
    verb that could. Agent 3 built the ledger; nothing joined it to the sweep.

    So every reconciliation line reported `billed_paise = 0` and therefore
    `open_commitment == ordered` for the whole estate; `recompute_commitment`
    subtracted a `billed` that was always zero, so a commitment never fell when
    its bill was paid; and `/api/integrations/reconciliation` reported
    `identity_balanced: true` while quoting all of it -- an identity between
    three figures all derived from one empty table balances perfectly.
    """
    assert hasattr(sweeps.SweepStore, "mirror_bill"), (
        "SweepStore declares no mirror_bill verb, so nothing in the sweep "
        "layer can write bill or bill_line at all")
    detail = code_only(inspect.getsource(sweeps.SweepBillDetail))
    assert "mirror_bill" in detail, (
        "the bill-detail sweep does not call mirror_bill; a hydrated bill is "
        "paid for, read and then discarded")
    # ...and the ORDER is load-bearing. Marking a bill hydrated is what takes
    # it off the queue; doing that before the ledger write would drain the
    # queue of documents nothing ever recorded.
    body = code_only(inspect.getsource(sweeps.SweepBillDetail.resume))
    assert body.index("_mirror") < body.index("mark_detail_hydrated"), (
        "the queue entry is marked hydrated before the ledger write, so a "
        "refused mirror still takes the bill off the queue for ever")


class _MirroringStore(InMemoryStore):
    """`InMemoryStore` plus the one verb `SweepStore` gained.

    A SUBCLASS rather than an edit to `tests/integration_fakes.py`, which
    belongs to another owner. It records the call verbatim, so the assertions
    below are about what the sweep PASSES rather than about what a double chose
    to keep -- the failure mode
    `tests/test_integration_dto_reader_contract.py` exists for.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mirrored: list[dict] = []

    def mirror_bill(self, **kwargs):
        self.mirrored.append(dict(kwargs))
        lines = kwargs.get("lines") or ()
        return {"bill_id": "BILL-" + str(kwargs["external_id"]),
                "attributed": len(lines), "quarantined": 0,
                "quarantined_paise": 0}


def _real_bill_dto(*, external_id, total_paise,
                   po_external_ids=(PO_EXTERNAL,)) -> BillDTO:
    """A bill exactly as an adapter emits one. NO double anywhere.

    Built from `dto.BillDTO` and `dto.LineDTO` rather than from
    `tests/integration_fakes.FakeRecord`, because the defect this proves fixed
    is a JOIN between two modules: the sweep must read the names the DTO really
    carries -- `vendor_name`, and `purchase_order_external_ids`, which is
    PLURAL and a TUPLE. A double shaped to the reader cannot fail the way
    production fails; that lesson is already recorded in
    `tests/test_integration_dto_reader_contract.py`.
    """
    return BillDTO(
        source=SourceRef(product="ERP", service="erp", api_version="v3",
                         endpoint="/bills/" + external_id, retrieved_at=T0),
        external_id=external_id, document_number="BN-" + external_id,
        document_date=date(2026, 9, 7), last_modified=T0,
        vendor_external_id="V-1", vendor_name="Pumps and Motors Pvt Ltd",
        currency_code="INR", subtotal_paise=total_paise, tax_paise=0,
        total_paise=total_paise, external_status_raw="open",
        purchase_order_external_ids=po_external_ids,
        lines=(LineDTO(external_line_id="RL-1", line_number=1,
                       description="Centrifugal pump", quantity="1",
                       unit_price_paise=total_paise,
                       line_total_paise=total_paise, tax_paise=0,
                       purchase_order_line_external_id=POL_EXTERNAL),),
        lines_hydrated=True,
        raw={"bill_id": external_id, "status": "open"})


def _run_bill_detail(store_double, *, queued, details, external_source):
    clock = FakeClock()
    adapter = FakeAdapter(clock, bill_details=details, seconds_per_call=1)
    store_double.detail_queue.extend(queued)
    job = sweeps.SweepBillDetail(adapter, store_double,
                                 store_double.connection_id,
                                 external_source=external_source)
    store_double.enqueue(job.kind)
    return jobs.run_job(job, store=store_double, clock=clock,
                        capabilities=ERP_CAPABILITIES)


def test_a_hydrated_bill_is_handed_to_the_ledger_with_the_names_the_dto_carries():
    """The sweep reads `vendor_name` and `purchase_order_external_ids`.

    Both are on `BillDTO`; neither was on `SourceRecord` before this. Without
    them `mirror_bill` cannot be called at all -- `bill.vendor_name` is NOT
    NULL and the control cell of every line is resolved through the purchase
    order the bill names -- so a reader that missed either would have made the
    wiring impossible rather than merely lossy.

    `purchase_order_external_ids` is the one to get wrong: it is PLURAL and a
    TUPLE, and a reader taking the singular spelling gets `None` from every
    real bill the adapters emit while passing against any double shaped to the
    reader.
    """
    double = _MirroringStore()
    dto = _real_bill_dto(external_id="ZB-SWEEP", total_paise=1_000_000)
    run = _run_bill_detail(double, queued=["ZB-SWEEP"],
                           details={"ZB-SWEEP": dto},
                           external_source=SOURCE_LABEL)
    assert run.state == jobs.JOB_DONE
    assert len(double.mirrored) == 1, (
        "the hydrated bill never reached the ledger surface")
    call = double.mirrored[0]
    assert call["external_source"] == SOURCE_LABEL
    assert call["external_id"] == "ZB-SWEEP"
    assert call["bill_number"] == "BN-ZB-SWEEP"
    assert call["vendor_name"] == "Pumps and Motors Pvt Ltd", (
        "vendor_name was read as None; bill.vendor_name is NOT NULL and the "
        "ledger refuses a placeholder, so the mirror could never run")
    assert call["bill_date"] == date(2026, 9, 7)
    assert call["po_external_id"] == PO_EXTERNAL, (
        "the PO linkage was read as None. BillDTO spells it "
        "purchase_order_external_ids -- plural, a tuple -- and without it "
        "every line has to name its own control cell or be quarantined")
    assert call["external_status_raw"] == "open", (
        "the raw status is carried across verbatim; C17 maps it in the ledger")
    assert call["external_last_modified"] == T0
    assert call["payload_sha"], "no payload_sha; §11.10 provenance is lost"
    assert [line.line_total_paise for line in call["lines"]] == [1_000_000]
    # ...and the bill still leaves the queue, so the call is not paid for twice.
    assert double.hydrated == ["ZB-SWEEP"] and double.detail_queue == []


def test_a_bill_naming_two_purchase_orders_is_held_not_attributed_to_the_first():
    """`bill.po_id` holds ONE purchase order and `BillDTO` may name several.

    Picking the first would attribute a whole document -- and every line's
    control cell through it -- to whichever order the source happened to list
    first, which is the guess §11.8 forbids in the one place where guessing
    wrong moves money into another project's budget. The bill is held at its
    FULL value instead and nothing is written.
    """
    double = _MirroringStore()
    dto = _real_bill_dto(external_id="ZB-TWO", total_paise=2_500_000,
                         po_external_ids=(PO_EXTERNAL, "ZPO-9002"))
    run = _run_bill_detail(double, queued=["ZB-TWO"],
                           details={"ZB-TWO": dto},
                           external_source=SOURCE_LABEL)
    assert run.state == jobs.JOB_DONE
    assert double.mirrored == [], "the ambiguous linkage was resolved by guessing"
    held = [e for e in double.exceptions.values()
            if e["object_id"] == "ZB-TWO"]
    assert len(held) == 1
    assert held[0]["kind"] == sweeps.KIND_CONTROL_TOTAL_MISMATCH
    assert held[0]["source_paise"] == 2_500_000, (
        "the bill was not held at its full value")


class _StoreWithoutMirrorBill(InMemoryStore):
    """`InMemoryStore` with the ledger verb taken away again.

    This test originally used a bare `InMemoryStore`, which HAD no
    `mirror_bill` -- that absence was the whole point, and the absence is what
    made the assertion meaningful. Teaching the shared double the verb (which
    `test_integration_sweeps.py` needs, because the sweep now genuinely calls
    it) made this test silently false: the store could mirror, so it no longer
    refused, and the assertion would have started passing for the wrong reason
    or failing for a reason unrelated to what it guards.

    Subclassing and removing the attribute keeps the ORIGINAL property under
    test -- a store that cannot record the bill must not let the queue drain --
    without weakening it and without holding the shared double back.
    """

    mirror_bill = None      # noqa: F811 - deliberately absent, see docstring

    def __getattribute__(self, name: str):
        if name == "mirror_bill":
            raise AttributeError(
                "mirror_bill: this store deliberately does not implement the "
                "ledger verb")
        return super().__getattribute__(name)


def test_a_store_that_cannot_mirror_refuses_rather_than_draining_the_queue():
    """The failure mode this whole defect wore: a queue that drains while the
    ledger stays empty. A store with no `mirror_bill` is a WIRING defect, and
    it is reported against the seam by name rather than letting the sweep mark
    a bill hydrated that nothing ever wrote."""
    plain = _StoreWithoutMirrorBill()
    run = _run_bill_detail(
        plain, queued=["ZB-1"],
        details={"ZB-1": _real_bill_dto(external_id="ZB-1", total_paise=100)},
        external_source=SOURCE_LABEL)
    assert run.state == jobs.JOB_FAILED, (
        "a store that cannot mirror reported success; the queue would drain "
        "while bill_line stayed empty")
    assert plain.hydrated == [], (
        "the bill was marked hydrated even though nothing wrote it")


# ------------------------------------------------------- C-2 (CRITICAL)
def test_both_attribution_paths_retract_the_quarantine_they_may_have_raised():
    """A line quarantined on pass 1 and attributed on pass 2 was counted TWICE.

    The sweeps re-walk BY DESIGN -- a 300-second overlap and a cycling cursor
    -- and PO-poll against bill-poll ordering is not guaranteed, so
    raise-then-succeed is the ORDINARY sequence, not an edge case. Nothing
    resolved the exception when the same line later attributed, so the same
    rupees sat in `bill_line.amount_paise` (feeding billed, and therefore
    actual) AND in `open_exception_exposure`'s SUM(source_paise) -- the figure
    the capitalisation gate and the period close quote. The exception also
    blocked capitalisation for ever on a line that was by then correctly
    posted.

    Keyed on the SAME triple the raise conflicts on, which is what lets the
    retraction find the row: `ux_reconciliation_exception_open` is UNIQUE on
    `(kind, object_type, object_id)` while it is Open.
    """
    assert procurement.QUARANTINE_RECEIVE_LINE == (
        "GRN_LINE_UNATTRIBUTED", "grn_line")
    assert procurement.QUARANTINE_BILL_LINE == (
        "CONTROL_TOTAL_MISMATCH", "bill_line")
    # The receive half: raised by `sweeps._attribute`, retracted by the ledger.
    raised = inspect.getsource(
        sweeps.SweepPoAnchored._attribute).replace('"', "'")
    assert "KIND_GRN_LINE_UNATTRIBUTED" in raised
    assert "f'{receive.external_id}:{line_key}'" in raised, (
        "the receive quarantine's object_id shape moved; the retraction "
        "reconstructs it and would silently stop matching")
    settle = _sql("_settle_receive_line").replace('"', "'")
    assert "retract_quarantine" in settle
    assert "f'{receive_external_id}:{line_external_id}'" in settle, (
        "the retraction no longer keys on the receive quarantine's object_id")
    # The bill half: raised and retracted in the same function, which is what
    # makes the two keys impossible to drift apart.
    line = _sql("_mirror_bill_line").replace('"', "'")
    assert "retract_quarantine" in line
    assert line.count("f'{bill_external_id}:{line_key}'") >= 2, (
        "the raise and the retraction do not key on the same object_id, so "
        "the exception a first pass raised is never found by the second")


def test_a_retracted_quarantine_is_transitioned_and_never_deleted():
    """DELETE is revoked on `capex_app` and the audit trail is the point.

    The row stays, keeps its `source_paise` as the record of what WAS held, and
    moves to `Resolved` with an actor, a time and a reason -- which is what
    `ck_reconciliation_exception_resolution` demands, and what makes the value
    stop counting, because `open_exception_exposure` and `open_exceptions` both
    filter `status = 'Open'`.
    """
    body = _sql("retract_quarantine")
    assert not _DELETE_STATEMENT.search(SOURCE), (
        "this module issues a DELETE; capex_app has the privilege revoked and "
        "the row is evidence")
    assert "UPDATE" in body and "status = %(resolved)s" in body
    for column in ("resolved_at", "resolved_by", "resolution_note"):
        assert column in body, (
            "{} is not set, so ck_reconciliation_exception_resolution refuses "
            "the row: a resolution names who and when, or it is not a "
            "resolution".format(column))
    assert "x.status = %(open)s" in body, (
        "a row a human already Accepted or Wrote off would be overwritten; "
        "the first reviewer's decision is evidence, not a draft")
    assert store.EXCEPTION_RESOLVED == "Resolved"
    # ...and it is audited, so the money leaving the bucket has a named reason.
    assert "_audit" in body and "AUDIT_QUARANTINE_RETRACTED" in body


# ----------------------------------------------------------- H-1 (HIGH)
class _NoStatements:
    """A session that records every statement it is asked to run.

    Used where the assertion is that NOTHING was issued: a refusal that reaches
    the database has already failed.
    """

    scope = Scope(user_id="U", entity_ids=frozenset({ENTITY}))

    def __init__(self):
        self.statements: list[str] = []

    def fetchall(self, statement, params=None):
        self.statements.append(statement)
        return []

    def fetchone(self, statement, params=None):
        self.statements.append(statement)
        return None


@pytest.mark.parametrize("raw,default,expected", [
    (None, "Void", "Void"),
    ("   ", "Void", "Void"),
    (None, "Draft", "Draft"),
    ("", "Reversal", "Reversal"),
])
def test_the_status_resolver_returns_the_callers_default_not_a_hard_coded_approved(
        raw, default, expected):
    """The comment said "the caller's own default stands"; the code said
    `return "Approved", None`.

    `mirror_bill` then assigned that answer to `accounting_status`, so a bill
    passed `accounting_status="Void"` came out **Approved** -- accounting
    effective under AUD-C-004, relieving commitment and raising actual on a
    VOIDED document. Two branches did it: the blank-raw one and the C17
    `BUSINESS_STATUS` / `NO_BUSINESS_STATUS` one.
    """
    session = _NoStatements()
    status, exception_id = procurement.resolve_accounting_status(
        session, adapter_product="ERP", object="bill", field="status",
        raw=raw, object_id="ZB-1", default_status=default, as_of=AS_OF)
    assert (status, exception_id) == (expected, None)
    assert session.statements == [], (
        "a status the resolver had nothing to interpret still reached the "
        "database")


def test_a_c17_row_that_says_nothing_about_accounting_leaves_the_default_alone():
    """C17's `BUSINESS_STATUS` and `NO_BUSINESS_STATUS` rows map a raw value
    without saying anything about accounting effect. That silence is
    deliberate, so the CALLER's default stands -- and it must be the caller's,
    not the literal `Approved` this branch used to return.

    Driven through `purchase_order.status`, whose ERP block is entirely
    `BUSINESS_STATUS`, because both `bill.status` blocks are
    `ACCOUNTING_STATUS_ONLY` today and the branch would otherwise be
    unreachable from a test -- which is part of how it survived review.
    """
    session = _NoStatements()
    for default in ("Void", "Draft", "Reversal", "Approved"):
        status, exception_id = procurement.resolve_accounting_status(
            session, adapter_product="ERP", object="purchase_order",
            field="status", raw="cancelled", object_id="ZPO-1",
            default_status=default, as_of=AS_OF)
        assert (status, exception_id) == (default, None), (
            "a C17 row carrying no accounting status overrode the caller's "
            + repr(default))
    assert session.statements == []


def test_the_status_resolver_no_longer_hard_codes_approved_anywhere():
    """The literal is gone from the function, and the caller hands its own
    default in. Asserted against the code because both replacements are one
    word each and a revert would read as a tidy-up."""
    body = _sql("resolve_accounting_status")
    assert 'return "Approved"' not in body and "return 'Approved'" not in body, (
        "resolve_accounting_status hard-codes Approved again")
    assert "default_status" in body
    tree = ast.parse(SOURCE)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "resolve_accounting_status"]
    assert calls, "mirror_bill no longer resolves the external status at all"
    for call in calls:
        supplied = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert supplied.get("default_status") == "accounting_status", (
            "the caller does not hand the resolver its own default, so the "
            "resolver has nothing to fall back to but a hard-coded value")


def test_an_invalid_default_status_is_refused_rather_than_substituted():
    """A caller's typo reaching `bill.accounting_status` is refused by
    `ck_bill_accounting_status` at 3am inside a cron function. Refused here
    instead, and NOT quietly replaced with a valid-looking one."""
    with pytest.raises(procurement.ProcurementIngestError) as exc:
        procurement.resolve_accounting_status(
            _NoStatements(), adapter_product="ERP", object="bill",
            field="status", raw=None, object_id="ZB-1",
            default_status="approved", as_of=AS_OF)
    assert exc.value.code == "UNKNOWN_ACCOUNTING_STATUS"


# ----------------------------------------------------------- H-2 (HIGH)
def test_every_mirrored_grn_carries_all_four_provenance_columns():
    """§6.1's block was ONE of four populated on every real receive.

    `_mirror_grn_header` had a second INSERT branch, taken whenever
    `external_source` was absent, that omitted BOTH `payload_sha` and
    `external_last_modified` though `sweeps._attribute` passes them and
    comments them "PROVENANCE, §11.10". `SweepPoAnchored.external_source`
    defaults to `None` and nothing constructed it with a value, so that branch
    was the only one anything ever took.
    """
    header = _sql("_mirror_grn_header")
    inserts = re.findall(r"INSERT INTO \{GRN\}\s*\(([^)]*)\)", header)
    assert len(inserts) == 1, (
        "{} INSERT branches into grn; the second one is what dropped the "
        "provenance, and there is nothing left for it to handle now that a "
        "blank external_source is refused".format(len(inserts)))
    columns = inserts[0]
    for column in ("external_source", "external_id", "external_last_modified",
                   "payload_sha"):
        assert column in columns, (
            "a mirrored GRN is written without {}; §11.10 requires the SOURCE "
            "DOCUMENT to be recoverable, not our rendering of it".format(column))
    # ...and a re-read carrying a newer version refreshes the two that say
    # WHICH version, rather than leaving the first one we happened to see.
    assert "external_last_modified = EXCLUDED.external_last_modified" in header
    assert "payload_sha = EXCLUDED.payload_sha" in header


def test_a_receive_with_no_external_source_is_refused_not_written_as_null():
    """The NULL that caused all three defects, closed at the one place that can
    close it.

    `ux_grn_external ON grn (external_source, external_id)` is NULLS DISTINCT
    in its LEADING column, so a NULL source made the index constrain nothing;
    and `grn_id` is derived from `(external_source, receive_external_id)` while
    `grn_number` is derived from the receive id alone, so the day a deployment
    set the label every already-mirrored receive would derive a NEW `grn_id`
    and the SAME `grn_number`, violate `ux_grn_number` -- which no ON CONFLICT
    target here covers -- abort the transaction, and kill the sweep on every
    tick, with DELETE revoked so nothing could be tidied.
    """
    class _CellResolves(_NoStatements):
        """The control cell IS found, so the refusal below is the provenance
        one and not the 404 that would otherwise mask it."""

        def fetchall(self, statement, params=None):
            self.statements.append(statement)
            return [("PO-ING", PROJECT, WBS + "-a", HEAD + "-a")]

    session = _CellResolves()
    for blank in (None, "", "   "):
        with pytest.raises(store.IntegrationStoreError) as exc:
            procurement.record_receive_line(
                session, po_line_id="POL-ING", receive_external_id="RCV-1",
                line_external_id=POL_EXTERNAL, quantity="1",
                amount_paise=1_000_000, external_source=blank)
        assert exc.value.code == "RECEIVE_EXTERNAL_SOURCE_MISSING"
    assert not any("INSERT" in statement for statement in session.statements), (
        "a receive with no provenance reached a write before being refused")


def test_the_grn_id_and_the_grn_number_can_no_longer_disagree():
    """`grn_id` is derived, `grn_number` is derived, and the source label can no
    longer change under them -- because it can no longer be absent. The
    derivation itself is unchanged, so a replay still lands on the same row."""
    first = procurement.derived_id("GRN", SOURCE_LABEL, "RCV-1")
    assert first == procurement.derived_id("GRN", SOURCE_LABEL, "RCV-1")
    assert "derived_id('GRN', external_source, receive_external_id)" \
        in _sql("_mirror_grn_header").replace('"', "'"), (
        "the grn_id derivation moved; it must stay a pure function of the "
        "source label and the receive id, both of which are now mandatory")
    # ...and the coalesce that made the two disagree is gone from the header,
    # which is the only place it could reach an id. `_adapter_product_of` has
    # its own `external_source or ""`, and that one is a normalisation before
    # a REFUSAL rather than a value written anywhere, so this is scoped to the
    # function rather than to the module.
    assert "external_source or ''" not in _sql(
        "_mirror_grn_header").replace('"', "'"), (
        "a blank external_source is being coalesced into the id derivation "
        "again, which is what made grn_id and grn_number disagree")
    assert "external_source or ''" not in _sql(
        "record_receive_line").replace('"', "'")


# ----------------------------------------------------------- H-5 (HIGH)
def test_every_money_moving_success_path_writes_an_audit_entry():
    """Nothing in the ledger wrote an audit entry, on any SUCCESS path.

    `record_receive_line` creates a `grn` header and a `grn_line`;
    `mirror_bill` creates a `bill` and N `bill_line` rows. Those move `actual`
    and `commitment`. Every mutation in `procurement_services.py` audits on the
    same session; the mirrored documents -- where the money actually arrives --
    had none, so §11.9's trace ("one id traces a Zoho bill from HTTP response
    to ledger movement to audit entry") stopped one step short.
    """
    for name in ("_settle_receive_line", "mirror_bill",
                 "_supersede_withdrawn_bill_lines", "retract_quarantine"):
        assert "_audit(" in _sql(name), (
            name + " moves money and writes no audit entry")
    assert "audit_mod.append" in _sql("_audit")
    assert procurement.AUDIT_RECEIVE_MIRRORED == "GRN_MIRRORED"
    assert procurement.AUDIT_BILL_MIRRORED == "BILL_MIRRORED"


def test_the_audit_entry_is_written_on_the_callers_own_session():
    """INSIDE the transaction, so a rolled-back mirror leaves no entry claiming
    it happened. `audit_mod.append` takes the caller's `session`; a second
    connection, or a deferred write, would survive the rollback."""
    tree = ast.parse(SOURCE)
    appends = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append"
               and getattr(n.func.value, "id", None) == "audit_mod"]
    assert len(appends) == 1, (
        "the audit append moved or multiplied; there is one, and it takes the "
        "caller's session")
    assert ast.unparse(appends[0].args[0]) == "session"


def test_the_audit_actor_is_server_derived_and_never_blank():
    """Server-derived, never a caller-supplied header. An entry attributed to
    nobody is indistinguishable from one nobody wrote, and `audit_log.actor` is
    NOT NULL with no foreign key, so the refusal has to be in the code."""
    # EVERY audit call passes the function's own `actor` parameter, which the
    # ledger API takes from the calling service layer and defaults to the
    # sweep's service identity. Nothing here reads a request, a header or an
    # environment variable, so there is no path by which a caller-supplied
    # string could become the actor without also being the authenticated one.
    tree = ast.parse(SOURCE)
    audits = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_audit"]
    assert len(audits) >= 4, "the audit calls moved; this gate moves with them"
    for call in audits:
        supplied = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert supplied.get("actor") == "actor", (
            "an audit entry is attributed to something other than the "
            "server-derived actor the ledger function was called with")
    for name in ("request", "headers", "environ", "getenv"):
        assert name not in CODE, (
            "this module reads " + name + "; the audit actor is the service "
            "or principal identity the calling layer already authenticated")
    body = _sql("_audit").replace('"', "'")
    assert "code='BLANK_AUDIT_ACTOR'" in body
    with pytest.raises(store.IntegrationStoreError) as exc:
        procurement._audit(_NoStatements(), actor="  ", action="X",
                           object_type="Y", object_id="Z", detail="d")
    assert exc.value.code == "BLANK_AUDIT_ACTOR"


# ----------------------------------------------------------- H-8 (HIGH)
def test_reconcile_po_lines_is_transcribed_from_compute_ledger_verbatim():
    """`received_paise` summed `grn_line.amount_paise` with NO JOIN to `grn`.

    So it counted VOID goods receipts, which `reconciliation_lines` and
    `domain.compute_ledger` both exclude, and it did not negate a reversal,
    which both spell `SUM(CASE WHEN g.is_reversal THEN -ABS(...) ...)`. The
    reviewer's numbers: a receipt of +12,000,000 with a reversal GRN stored
    POSITIVE at 12,000,000 and `is_reversal = true` gives 0 from
    `reconciliation_lines` and 24,000,000 here. Add a Void GRN of 5,000,000 and
    the two differ by Rs 2,90,000 on ONE line.

    `billed_paise` and `ordered_paise` had drifted the same way -- both dropped
    `non_creditable_tax_paise` and `freight_paise`, and `billed` did not negate
    a `Reversal` bill -- so all three limbs are checked against the ORIGINAL
    rather than against a second statement of what the original says.
    """
    body = _sql("reconcile_po_lines")
    assert re.search(r"JOIN \{GRN\} g ON g\.grn_id = gl\.grn_id", body), (
        "received_paise sums grn_line with no join to grn, so a Void receipt "
        "counts and a reversal does not subtract")
    assert "g.status <> 'Void'" in body, (
        "Void goods receipts are being counted as received")
    assert re.search(
        r"SUM\(CASE WHEN g\.is_reversal\s+THEN -ABS\(gl\.amount_paise\)",
        body), (
        "a reversal GRN is not negated; domain.compute_ledger spells it "
        "-ABS(...) precisely so a reversal stored positive and one stored "
        "negative reduce received by the same amount")
    # The billed limb, AUD-C-004.
    assert "b.accounting_status = 'Reversal'" in body
    assert "b.accounting_status = ANY(%(effective)s)" in body, (
        "the effective-status list is a second literal that can drift from "
        "store.ACCOUNTING_EFFECTIVE_BILL_STATES")
    # Tax and freight are part of the money on all three limbs.
    for limb in ("pl.non_creditable_tax_paise", "bl.non_creditable_tax_paise"):
        assert limb in body, limb + " is dropped from the arithmetic"
    assert body.count("freight_paise") >= 3
    # ...and the ORIGINAL still says what this claims to transcribe, so the day
    # compute_ledger changes, this fails rather than drifting quietly.
    assert "WHERE g.status <> 'Void'" in DOMAIN_SOURCE
    assert "-ABS(gl.amount_paise)" in DOMAIN_SOURCE
    assert sqlite_domain.ACCOUNTING_EFFECTIVE_BILL_STATES == {
        "Approved", "Reversal"}
    assert set(store.ACCOUNTING_EFFECTIVE_BILL_STATES) == \
        sqlite_domain.ACCOUNTING_EFFECTIVE_BILL_STATES, (
        "PostgreSQL and the SQLite ledger disagree about which bill states are "
        "accounting-effective; they would quote different open commitment for "
        "the same estate, silently")


# --------------------------------------------------------- M-2 (MEDIUM)
def test_a_revised_bill_supersedes_the_lines_it_no_longer_carries():
    """A bill revised from three lines to two kept the removed line
    contributing to billed -- and through it to actual -- for ever.

    `mirror_bill` upserted the lines it was given and never reconciled the ones
    that had DISAPPEARED from the source, and DELETE is revoked so nothing
    could remove them afterwards. They are made NON-EFFECTIVE instead: the
    money zeroed so every formula that sums it sees nothing, the row kept and
    stamped so the trail still says what it was.
    """
    assert "_supersede_withdrawn_bill_lines" in _sql("mirror_bill")
    body = _sql("_supersede_withdrawn_bill_lines")
    assert "UPDATE" in body and not _DELETE_STATEMENT.search(body)
    for column in ("amount_paise = 0", "non_creditable_tax_paise = 0",
                   "freight_paise = 0", "quantity = 0"):
        assert column in body, (
            column + " is not zeroed, so a withdrawn line keeps contributing "
            "to billed and to actual")
    assert "NOT (bl.bill_line_id = ANY(%(keep)s::text[]))" in body, (
        "the lines this pass wrote are not protected, or the empty-list case "
        "cannot be typed by the driver")
    # The stamp is also the idempotency guard: the sweeps re-walk, and a second
    # pass must not re-stamp and re-version a row it already withdrew.
    assert "NOT LIKE %(stamped)s" in body
    assert procurement.SUPERSEDED_DESCRIPTION_PREFIX == "SUPERSEDED "


def test_a_re_mirrored_bill_line_takes_its_new_control_cell():
    """The `ON CONFLICT DO UPDATE` set list omitted `po_id`, `po_line_id`,
    `wbs_id` and `budget_head_id` -- the four columns
    `fk_bill_line_po_line_cell` binds together -- so a re-mirrored line kept
    its ORIGINAL control cell after its linkage changed, and the money stayed
    posted against a WBS element and budget head the source no longer names."""
    body = _sql("_mirror_bill_line")
    conflict = body.split("ON CONFLICT (bill_line_id)")[1]
    for column in ("po_id", "po_line_id", "wbs_id", "budget_head_id",
                   "po_line_external_id"):
        assert column + " = EXCLUDED." + column in conflict, (
            column + " is not refreshed on a re-mirror; the line keeps a "
            "control cell the source no longer names")


# =========================================================================
# LIVE. Gated on CAPEX_DB_URL; first executes in CI.
# =========================================================================
def _open_exceptions_paise(con) -> int:
    row = con.execute(
        "SELECT coalesce(sum(source_paise), 0)::bigint"
        " FROM reconciliation_exception WHERE status = 'Open'").fetchone()
    return int(row[0])


@PG
def test_the_bill_quarantine_then_attribute_sequence_counts_the_money_once(seeded):
    """C-2, end to end, with the reviewer's own scenario.

    Pass 1 sees a bill line whose linkage does not resolve: it is quarantined
    at FULL value. Pass 2 sees the same bill with the linkage populated: the
    line posts. The money must then exist ONCE -- in `bill_line`, feeding
    billed and actual -- and NOT also in `open_exception_exposure`, which is
    what the capitalisation gate and the period close quote.
    """
    session = _session(seeded)
    first = _bill(session, external_id="ZB-Q", number="BILL-Q",
                  lines=[_line(2_500_000, line_id="NOT-A-LINE")])
    seeded.commit()
    assert first["quarantined"] == 1 and first["attributed"] == 0
    assert _open_exceptions_paise(seeded) == 2_500_000
    exception_id = first["exception_ids"][0]

    # The same source document on the next pass, now carrying the linkage.
    second = _bill(session, external_id="ZB-Q", number="BILL-Q",
                   lines=[_line(2_500_000, line_id=POL_EXTERNAL)])
    seeded.commit()
    assert second["attributed"] == 1 and second["quarantined"] == 0

    status, resolved_by, note, held = seeded.execute(
        "SELECT status, resolved_by, resolution_note, source_paise"
        " FROM reconciliation_exception WHERE exception_id = %s",
        (exception_id,)).fetchone()
    assert status == "Resolved", (
        "the exception raised on pass 1 is still Open, so the line is counted "
        "in bill_line AND in open_exception_exposure, and capitalisation is "
        "blocked for ever on a line that is correctly posted")
    assert resolved_by and note, (
        "ck_reconciliation_exception_resolution requires who and when")
    assert int(held) == 2_500_000, (
        "the row lost the record of what it was holding; the value stops "
        "counting because the row leaves Open, not because it was zeroed")

    assert _open_exceptions_paise(seeded) == 0
    assert store.open_exception_exposure(
        session, project_id=PROJECT)["source_paise"] == 0
    billed = seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM bill_line").fetchone()[0]
    assert int(billed) == 2_500_000, "the money is counted twice, or not at all"
    assert procurement.reconcile_po_lines(
        session, project_id=PROJECT)[0]["billed_paise"] == 2_500_000


@PG
def test_a_receive_quarantined_on_pass_one_is_retracted_when_it_attributes(seeded):
    """The GRN half of the same sequence.

    The quarantine is raised by `sweeps.SweepPoAnchored._attribute` with an
    object_id of `receive:line`, and the ledger retracts exactly that key when
    the line posts -- no ordinal has to be carried through the store surface,
    because a line with no identifier can never attribute at all.
    """
    session = _session(seeded)
    exception_id = store.raise_exception(
        session, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
        object_id="RCV-1:" + POL_EXTERNAL,
        detail="Receive RCV-1 line does not resolve to a known po_line.",
        raised_at=T0, entity_id=ENTITY, project_id=PROJECT)
    store.accumulate_unattributed(session, project_id=PROJECT,
                                  paise=1_000_000, source_key=exception_id)
    seeded.commit()
    assert _open_exceptions_paise(seeded) == 1_000_000

    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000)
    seeded.commit()

    status = seeded.execute(
        "SELECT status FROM reconciliation_exception WHERE exception_id = %s",
        (exception_id,)).fetchone()[0]
    assert status == "Resolved"
    assert _open_exceptions_paise(seeded) == 0
    assert int(seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM grn_line").fetchone()[0]) \
        == 1_000_000


@PG
def test_a_human_triaged_exception_is_not_reopened_or_overwritten(seeded):
    """A reviewer who Accepted or Wrote off an exception has made a decision.
    The retraction only ever moves a row OUT of Open, so an already-triaged row
    keeps its own actor, reason and status."""
    session = _session(seeded)
    exception_id = store.raise_exception(
        session, kind="GRN_LINE_UNATTRIBUTED", object_type="grn_line",
        object_id="RCV-1:" + POL_EXTERNAL, detail="unattributed",
        raised_at=T0, entity_id=ENTITY, project_id=PROJECT)
    store.act_on_exception(session, exception_id=exception_id, action="accept",
                           actor="U-FINANCE", reason="Known vendor shortfall.")
    seeded.commit()

    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000)
    seeded.commit()

    status, by, note = seeded.execute(
        "SELECT status, resolved_by, resolution_note"
        " FROM reconciliation_exception WHERE exception_id = %s",
        (exception_id,)).fetchone()
    assert (status, by) == ("Accepted", "U-FINANCE")
    assert note == "Known vendor shortfall."


@PG
def test_a_void_bill_stays_void_and_moves_no_money(seeded):
    """H-1's scenario: `accounting_status="Void"` must survive.

    An `Approved` here is accounting-effective under AUD-C-004: it would
    relieve commitment and raise actual on a VOIDED document.
    """
    session = _session(seeded)
    result = _bill(session, external_id="ZB-VD", number="BILL-VD",
                   lines=[_line(700_000)], accounting_status="Void",
                   status_raw="void")
    seeded.commit()
    assert result["accounting_status"] == "Void"
    assert result["accounting_effective"] is False
    assert seeded.execute(
        "SELECT accounting_status FROM bill WHERE bill_number = 'BILL-VD'"
    ).fetchone()[0] == "Void"
    # ...and a Void bill moves nothing: not billed, and therefore not actual.
    assert procurement.reconcile_po_lines(
        session, project_id=PROJECT)[0]["billed_paise"] == 0
    assert store.reconciliation_lines(
        session, project_id=PROJECT)[0]["billed_paise"] == 0


@PG
def test_reconcile_po_lines_and_reconciliation_lines_agree_on_void_and_reversal(
        seeded):
    """H-8, with the reviewer's exact numbers.

    A receipt of +12,000,000, a reversal GRN stored POSITIVE at 12,000,000 with
    `is_reversal = true`, and a Void GRN of 5,000,000. `reconciliation_lines`
    said 0; `reconcile_po_lines` said 24,000,000, and 29,000,000 once the Void
    receipt was added -- a difference of Rs 2,90,000 on one line.
    """
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=12_000_000)
    procurement.record_receive_line(
        session, po_line_id="POL-ING", receive_external_id="RCV-REV",
        line_external_id=POL_EXTERNAL, quantity="-1",
        amount_paise=12_000_000, external_source=SOURCE_LABEL,
        received_at=T0, external_last_modified=T0, payload_sha="sha-rev",
        is_reversal=True)
    _receive(session, receive_id="RCV-VOID", line_id=POL_EXTERNAL,
             quantity="1", paise=5_000_000)
    seeded.execute(
        "UPDATE grn SET status = 'Void' WHERE external_id = 'RCV-VOID'")
    seeded.commit()

    mine = procurement.reconcile_po_lines(session, project_id=PROJECT)[0]
    theirs = store.reconciliation_lines(session, project_id=PROJECT)[0]
    assert mine["received_paise"] == 0, (
        "a reversal stored positive was added instead of subtracted, or a "
        "Void goods receipt was counted")
    assert mine["received_paise"] == theirs["received_paise"]
    assert mine["ordered_paise"] == theirs["ordered_paise"]
    assert mine["billed_paise"] == theirs["billed_paise"]
    for key in ("ordered_paise", "received_paise", "billed_paise"):
        assert isinstance(mine[key], int) and not isinstance(mine[key], bool), (
            key + " came back as a Decimal; a SUM lost its ::bigint cast")


@PG
def test_a_revised_bill_stops_counting_the_line_it_no_longer_carries(seeded):
    """M-2, end to end. Three lines become two; the third stops counting and
    its row is still there to say what it was."""
    session = _session(seeded)
    _bill(session, external_id="ZB-R", number="BILL-R", lines=[
        _line(300_000), _line(400_000), _line(500_000)])
    seeded.commit()
    assert int(seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM bill_line").fetchone()[0]) \
        == 1_200_000

    revised = _bill(session, external_id="ZB-R", number="BILL-R",
                    lines=[_line(300_000), _line(400_000)])
    seeded.commit()
    assert revised["superseded"] == 1

    assert int(seeded.execute(
        "SELECT sum(amount_paise)::bigint FROM bill_line").fetchone()[0]) \
        == 700_000, "the withdrawn line is still contributing to billed"
    assert seeded.execute(
        "SELECT count(*) FROM bill_line").fetchone()[0] == 3, (
        "the row was DELETEd; capex_app has the privilege revoked and the row "
        "is evidence of what the bill once carried")
    stamped = seeded.execute(
        "SELECT description FROM bill_line WHERE amount_paise = 0"
    ).fetchone()[0]
    assert stamped.startswith(procurement.SUPERSEDED_DESCRIPTION_PREFIX)
    assert procurement.reconcile_po_lines(
        session, project_id=PROJECT)[0]["billed_paise"] == 700_000

    # ...and a re-mirror of the SAME revision withdraws nothing further and
    # does not re-stamp: the sweeps re-walk, and version_no would otherwise
    # climb every fifteen minutes on a row nothing had touched.
    version_before = seeded.execute(
        "SELECT version_no FROM bill_line WHERE amount_paise = 0").fetchone()[0]
    again = _bill(session, external_id="ZB-R", number="BILL-R",
                  lines=[_line(300_000), _line(400_000)])
    seeded.commit()
    assert again["superseded"] == 0
    assert seeded.execute(
        "SELECT version_no FROM bill_line WHERE amount_paise = 0"
    ).fetchone()[0] == version_before


@PG
def test_a_mirrored_receive_and_a_mirrored_bill_each_leave_an_audit_entry(seeded):
    """H-5. These rows move `actual` and `commitment`; every mutation in
    `procurement_services.py` audits on the same session and these had none."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000)
    _bill(session, external_id="ZB-1", number="BILL-1",
          lines=[_line(1_000_000)])
    seeded.commit()

    actions = [row[0] for row in seeded.execute(
        "SELECT action FROM audit_log ORDER BY audit_id").fetchall()]
    assert procurement.AUDIT_RECEIVE_MIRRORED in actions
    assert procurement.AUDIT_BILL_MIRRORED in actions
    actor, detail = seeded.execute(
        "SELECT actor, detail FROM audit_log WHERE action = %s",
        (procurement.AUDIT_BILL_MIRRORED,)).fetchone()
    assert actor == "SVC-SWEEP"
    assert "1000000" in detail, "the entry does not say how much moved"
    # The chain is intact, which is the property the whole log rests on.
    bill_id = seeded.execute("SELECT bill_id FROM bill").fetchone()[0]
    assert audit_mod.verify_chain(session, "VendorBill:" + bill_id)["intact"]


@PG
def test_a_rolled_back_mirror_leaves_no_audit_entry_claiming_it_happened(seeded):
    """The entry is appended INSIDE the caller's transaction, so it dies with
    the rows it describes. A second connection, or a deferred write, would
    leave the log asserting a receipt that no `grn_line` backs."""
    session = _session(seeded)
    _receive(session, receive_id="RCV-1", line_id=POL_EXTERNAL,
             quantity="1", paise=1_000_000)
    assert seeded.execute(
        "SELECT count(*) FROM audit_log WHERE action = %s",
        (procurement.AUDIT_RECEIVE_MIRRORED,)).fetchone()[0] == 1
    seeded.rollback()
    assert seeded.execute("SELECT count(*) FROM grn_line").fetchone()[0] == 0
    assert seeded.execute(
        "SELECT count(*) FROM audit_log WHERE action = %s",
        (procurement.AUDIT_RECEIVE_MIRRORED,)).fetchone()[0] == 0
