"""Every seeded approval predicate must COMPILE, and every ACTIVE seeded
definition must ROUTE (Fable 5.1).

Found while walking the original-budget workflow on the PostgreSQL demo:
`seed_parts/008_approvals.sql` wrote its rule predicates and stage
`applies_when` clauses in an `{"all": [{"field", "op": "eq", "value"}],
"route": ...}` shape that `approval_rules.compile_predicate` refuses (it
wants `op` in and/or/not/==/!=/</<=/>/>=/in/not_in/true/false with
`path`/`value` operands). Every seeded definition -- including the ACTIVE
purchase-request ones -- raised APPROVAL_PREDICATE_INVALID the first time
anything routed against it, and a submission on the demo estate answered
500. No test had ever compiled the SEED: the engine tests seed their own
`{"op": "true"}` rules, and the seed loader only checks that the SQL
executes.

This file loads the real seed into a disposable database and proves the
configuration the demo runs on is one the engine can use.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_template, pg_url,
)

from app.backend.pg import approval_rules as rules            # noqa: E402
from app.backend.pg import approvals as engine                # noqa: E402
from app.backend.pg import seed as seed_mod                   # noqa: E402
from app.backend.pg.engine import Scope                       # noqa: E402

pytestmark = pytest.mark.pg


@pytest.fixture()
def seeded(pg_connection, monkeypatch):
    """The real demo seed, loaded through the product's own guarded loader
    into this test's disposable database (its name satisfies the guard)."""
    monkeypatch.setenv("CAPEX_PROFILE", "local-demo")
    seed_mod.seed(pg_connection)
    pg_connection.commit()
    return pg_connection


def test_every_seeded_rule_and_stage_predicate_compiles(seeded):
    rows = seeded.execute(
        "SELECT 'rule', rule_id, predicate FROM approval_rule "
        "UNION ALL SELECT 'stage', stage_id, applies_when FROM approval_stage "
        "WHERE applies_when IS NOT NULL").fetchall()
    assert rows, "the seed carries no approval rules at all"
    failures = []
    for kind, ident, predicate in rows:
        try:
            rules.compile_predicate(predicate)
        except rules.PredicateError as exc:
            failures.append(f"{kind} {ident}: {exc}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("object_type, snapshot", [
    ("PURCHASE_REQUEST", {"object_type": "PURCHASE_REQUEST", "entity_id": "ENT-DM2",
                          "amount_paise": 1_00_000_00, "exceeds_available": False}),
    ("ORIGINAL_BUDGET", {"object_type": "ORIGINAL_BUDGET", "entity_id": "ENT-DM1",
                         "amount_paise": 25_00_000_00, "line_count": 3}),
])
def test_every_active_seeded_definition_routes_a_plain_document(seeded, pg_database, object_type, snapshot):
    with pg_database.session(Scope(user_id="U-TEST-SEED", principal_kind="USER", read_all=True)) as session:
        definitions = engine._load_definitions(session, object_type)
    active = [d for d in definitions]
    assert active, f"no ACTIVE seeded definition for {object_type}"
    resolution = rules.resolve_route(definitions, snapshot, object_type=object_type,
                                     business_date=date(2026, 9, 1))
    assert resolution.definition.object_type == object_type
    assert resolution.rule.predicate.evaluate(snapshot) is True


def test_the_over_budget_requisition_takes_the_exception_rule(seeded, pg_database):
    with pg_database.session(Scope(user_id="U-TEST-SEED", principal_kind="USER", read_all=True)) as session:
        definitions = engine._load_definitions(session, "PURCHASE_REQUEST")
    dm1 = [d for d in definitions if d.entity_id in ("ENT-DM1", None)]
    assert dm1, "ENT-DM1 has no active purchase-request definition"
    within = {"object_type": "PURCHASE_REQUEST", "entity_id": "ENT-DM1",
              "amount_paise": 1_00_000_00, "exceeds_available": False}
    over = dict(within, amount_paise=6_00_00_000_00)
    r_within = rules.resolve_route(definitions, within, object_type="PURCHASE_REQUEST",
                                   business_date=date(2026, 9, 1), entity_id="ENT-DM1")
    r_over = rules.resolve_route(definitions, over, object_type="PURCHASE_REQUEST",
                                 business_date=date(2026, 9, 1), entity_id="ENT-DM1")
    assert r_over.rule.priority < r_within.rule.priority, \
        "the exception rule (priority 10) must win for an over-threshold requisition"


def test_the_large_original_budget_opens_the_cfo_stage(seeded):
    row = seeded.execute(
        "SELECT applies_when FROM approval_stage WHERE stage_id = 'APS-OB-ORG-V1-2'").fetchone()
    assert row is not None
    pred = rules.compile_predicate(row[0])
    assert pred.evaluate({"amount_paise": 5_000_000_001}) is True   # Rs 5,00,00,000.01
    assert pred.evaluate({"amount_paise": 5_000_000_000}) is False  # exactly Rs 5 crore: not over
    assert pred.evaluate({"amount_paise": 1}) is False
