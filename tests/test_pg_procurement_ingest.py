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

from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import procurement  # noqa: E402
from app.backend.pg.engine import Scope, Session  # noqa: E402

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
#: 014 CORRECTS several of the objects 013 created. Where an assertion below is
#: about an object 014 replaced, it reads THIS text -- the migrations are
#: additive and the corrected object lives here, not in 013.
CORRECTION = (_Path(__file__).resolve().parents[1] / "migrations" / "pg"
              / "014_procurement_corrections.sql").read_text(encoding="utf-8")

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
    """The receive-line conflict target must name a real index, columns and
    all. A target that does not match one is a runtime error PostgreSQL raises
    when the statement RUNS -- inside a cron function, in CI at the earliest.

    `tests/test_integration_sql_matches_schema.py` says plainly that it is not
    a SQL parser and cannot do this. So it is done here, against both texts.

    RE-POINTED AT 014, AND STRENGTHENED. 013's `ux_grn_line_external` was a
    table UNIQUE over three columns under PostgreSQL's DEFAULT NULLS DISTINCT,
    so it did not constrain a receive line with no external line id -- the
    ORDINARY case on Zoho ERP, where lines are discovered PO-anchored. Every
    sweep re-walk re-inserted and `received` climbed with no new receive
    arriving. 014 drops it and creates
    `ux_grn_line_external_v2 ... NULLS NOT DISTINCT` over FOUR columns.

    The assertion is not weakened by moving: it now also requires the
    `NULLS NOT DISTINCT` clause, without which the index is 013's defect again
    under a new name, and it requires the ordinal that keeps two genuinely
    distinct lines on one receive from collapsing into one.
    """
    assert "DROP CONSTRAINT IF EXISTS ux_grn_line_external" in CORRECTION, (
        "013's NULLS DISTINCT constraint must be dropped by name, not shadowed")
    assert re.search(
        r"CREATE UNIQUE INDEX ux_grn_line_external_v2\s+ON grn_line\s+"
        r"\(po_line_id, receive_external_id, line_external_id, line_no\)\s+"
        r"NULLS NOT DISTINCT", CORRECTION), (
        "ux_grn_line_external_v2 must be NULLS NOT DISTINCT over four columns; "
        "without the clause it is 013's defect under a new name")
    assert ("ON CONFLICT (po_line_id, receive_external_id, line_external_id, "
            "line_no)" in SOURCE)
    columns = store.GRN_LINE_EXTERNAL_UNIQUE
    assert columns == ("po_line_id", "receive_external_id",
                       "line_external_id", "line_no")


@pytest.mark.parametrize("table", ["grn", "bill"])
def test_every_partial_mirror_index_is_targeted_with_its_predicate(table):
    """The mirror upserts are PARTIAL -- ``WHERE external_id IS NOT NULL``.
    Inference by COLUMNS ALONE matches no index on either table, and PostgreSQL
    refuses rather than choosing another one: the good failure, but only if the
    predicate is written out.

    RE-POINTED AT 014, AND STRENGTHENED. 013's `ux_grn_external` /
    `ux_bill_external` were UNIQUE on `(external_source, external_id)`
    ESTATE-WIDE, so the same external document mirrored from two Zoho
    ORGANISATIONS collided and the second was refused. 014 drops both and
    creates `(connection_id, external_source, external_id)` NULLS NOT DISTINCT
    -- which is a strict replacement, not a relaxation: with `connection_id`
    NULL on both rows the group is identical to 013's, and with two different
    organisations it correctly is not.

    The predicate half of the assertion -- the part this test exists for -- is
    unchanged. It now also requires `NULLS NOT DISTINCT`, without which
    dropping 013's index would genuinely weaken replay idempotency for every
    row whose `connection_id` is NULL.
    """
    assert f"DROP INDEX IF EXISTS ux_{table}_external" in CORRECTION, (
        f"ux_{table}_external must be dropped by name; leaving it in place "
        f"means the two-organisation case still collides on it and the new "
        f"index changes nothing")
    assert re.search(
        rf"CREATE UNIQUE INDEX ux_{table}_external_identity\s+ON {table}\s+"
        rf"\(connection_id, external_source, external_id\)\s+"
        rf"NULLS NOT DISTINCT\s+WHERE external_id IS NOT NULL",
        CORRECTION), (
        f"ux_{table}_external_identity is no longer the partial, "
        f"NULLS NOT DISTINCT index this targets")
    targets = re.findall(
        r"ON CONFLICT \(connection_id, external_source, external_id\)\s*\n\s*"
        r"WHERE external_id IS NOT NULL", SOURCE)
    assert len(targets) >= 2, (
        "a mirror upsert names the identity columns without the partial "
        "index's predicate; it matches no index and will not run")


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
          status_raw=None, po=PO_EXTERNAL):
    return procurement.mirror_bill(
        session, external_source=SOURCE_LABEL, external_id=external_id,
        bill_number=number, vendor_name="Vendor", bill_date=date(2026, 9, 7),
        lines=lines, po_external_id=po, entity_id=ENTITY, doc_type=doc_type,
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
