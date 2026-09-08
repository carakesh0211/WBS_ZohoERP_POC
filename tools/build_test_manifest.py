"""Generate tests/TEST_MANIFEST.json - the inventory that protects the suite.

Plan v1.2.1 section 15.1:

    "All control intent and traceability must be preserved. Documented
     PostgreSQL-specific test adaptations are permitted, but no assertion may
     be weakened or removed without approval."

The manifest is how that is enforced mechanically. It records every test
function that exists today. tests/test_manifest.py asserts the live inventory
still matches it, so a removed or renamed test fails the build until the
manifest is updated with a recorded reason in tests/ADAPTATIONS.md.

Usage
-----
    python tools/build_test_manifest.py            # rewrite the manifest
    python tools/build_test_manifest.py --check    # exit 1 if it has drifted

The baseline suite is the audit remediation suite: the tests are named after
the findings they close, so losing one silently loses the evidence that a
critical finding stays closed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MANIFEST = TESTS / "TEST_MANIFEST.json"

# Files added after the POC baseline. Tracked, but excluded from the
# "220 original functions" count so the baseline stays a fixed reference.
POST_BASELINE_FILES = {
    "test_contracts.py", "test_manifest.py", "test_observability.py",
    "test_known_defects.py", "test_middleware_observability.py",
    # --- Milestone 1: production PostgreSQL foundation -------------------
    # The 220 baseline counts the POC's audit-remediation suite, and its
    # purpose is to catch a baseline test being REMOVED. New milestone files
    # belong here rather than inflating that number, or the guard stops
    # meaning anything the moment the product grows.
    "test_pg_repo.py", "test_pg_locking.py", "test_pg_audit.py",
    "test_runtime_startup.py",
    # PostgreSQL integration suite, skipped without CAPEX_DB_URL. Flattened
    # out of tests/pg/ because a second conftest.py there shadowed the root
    # one under the bare module name `conftest`.
    "test_pg_fixture_guard.py", "test_pg_migrations_runner.py",
    "test_pg_constraints.py", "test_pg_transactions.py",
    "test_pg_scope_leakage.py",
    # M1-S2
    "test_pg_audit_api_e2e.py", "test_pg_seed.py", "test_pg_adoption.py",
    # --- Wave 2: M3 budget, M4a identity/scope, M2 settings and masters ---
    # Stream-owned suites.
    "test_pg_budget.py", "test_pg_periods.py",
    "test_pg_masters.py", "test_pg_settings.py",
    "test_pg_roles.py", "test_pg_rls.py", "test_negative_access_matrix.py",
    # Integration-owned: the guards written after reviewing what the streams
    # delivered. All three run without a live database, deliberately -- the
    # defects they hold lived where a database-gated test would have skipped.
    "test_budget_api_guard.py", "test_masters_settings_api_guard.py",
    "test_scope_enforcement.py", "test_money_sql_discipline.py",
    "test_no_undefined_names.py",
    # --- Wave 3: the security-closure streams -----------------------------
    "test_pg_principal_scope.py", "test_scope_negative_matrix.py",
    "test_pg_rls_coverage.py", "test_scope_sentinel.py",
    "test_pg_locking_order.py", "test_pg_period_concurrency.py",
    # --- Wave 4: the approval engine --------------------------------------
    "test_pg_approval_schema.py", "test_pg_approvals.py",
    "test_pg_approval_concurrency.py", "test_approvals_api_guard.py",
    "test_approval_negative_matrix.py", "test_approval_e2e.py",
    "test_approval_maker_checker.py",
    # Wave 4 stream A1: the static check on the api/approvals.py engine seam.
    # Post-baseline like every other Wave 4 file -- the 220 counts the POC's
    # audit-remediation suite and inflating it would make the removal guard
    # meaningless.
    "test_approvals_api_seam.py",
    # Wave 5 integration pass: the gate that reads the package's SQL against
    # migration 010's actual columns. Post-baseline like every Wave 4/5 file.
    "test_integration_sql_matches_schema.py",
    # Wave 5 integration pass, lead: the DTO/reader join (a double had been
    # shaped to the reader, hiding a money defect) and the 3.11 dataclass
    # default check (the package could not import in CI at all).
    "test_integration_dto_reader_contract.py",
    "test_no_version_sensitive_defaults.py",
    # Wave 5 integration pass, lead: unattributed reconciliation exceptions
    # were visible to EVERY principal, because capex_dimension_permits treats
    # a NULL row value as a waived dimension and entity_id is a nullable
    # column. Migration 012 confines them to a triage principal.
    "test_pg_unattributed_triage.py",

    # --- Wave 6 agent 1: the procurement schema ---------------------------
    # Migration 013 is the first PostgreSQL home the eight procurement
    # documents have ever had; 001..012 create 53 tables and not one of them
    # is a purchase order. Three other Wave 6 agents build directly on these
    # tables, so this file holds the column names, the composite FKs, the
    # signed-money exceptions and the RLS policies to a build failure.
    #
    # Roughly half of it runs with no database, deliberately. The live half
    # skips on every workstation here and first executes in CI's pg_tests job,
    # which is exactly why the source-level half exists: a check whose only
    # coverage is in an environment nobody runs locally is a check nobody
    # runs. Post-baseline like every Wave 2-5 file -- the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # stop meaning anything the moment the product grows.
    "test_pg_procurement_schema.py",

    # --- Wave 6 agent 2: PR -> PO, and the emission to Zoho ---------------
    # `test_procurement_emission.py` runs everywhere: the emission decision
    # (how many purchase orders a requisition becomes, what identity each
    # carries, what `cf_capex_ref` the tenant will index it under) is pure, so
    # the split, the dedupe keys, the at-most-once behaviour under a simulated
    # Function death and the three distinct 429 responses are all proved on
    # every machine, against fakes and never against a tenant.
    #
    # `test_pg_procurement.py` is the half only a server can answer -- the
    # derived-total trigger firing, the ancestor chain actually being locked,
    # a maker-checker refusal actually stopping the UPDATE, an out-of-scope
    # project actually answering as absent. It skips on every workstation here
    # and first executes in CI's pg_tests job. A skip is not a pass, and every
    # gate in it says so in its own skip reason.
    #
    # Post-baseline like every Wave 2-6 file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # stop meaning anything the moment the product grows.
    "test_procurement_emission.py",
    "test_pg_procurement.py",

    # --- Wave 6 agent 4: commitment against actual, over those tables ------
    # The other half of migration 013. `test_pg_procurement_schema.py` holds
    # the SHAPE (columns, composite FKs, signed money, RLS); this holds the
    # ARITHMETIC that reads it -- open commitment is ordered less BILLED and
    # never less received, received-not-billed is its own bucket, and the four
    # quantities reconcile to the paisa.
    #
    # It runs with NO database at all, which is the whole reason it is a
    # separate file from `test_pg_reconciliation.py`: that file's live half
    # skips on every workstation here, so a change to the reconciliation
    # arithmetic could pass locally with nothing having executed. Every
    # property here is a property of SQL text, module constants or integer
    # arithmetic, so none of it skips anywhere. Post-baseline like every Wave
    # 2-6 file -- the 220 counts the POC's audit-remediation suite, and
    # inflating it would make the removal guard stop meaning anything.
    "test_pg_procurement_reconciliation.py",

    # --- Wave 6 agent 3: inbound GRN / Purchase Receive and Vendor Bill ----
    # The first code that WRITES to migration 013's chain, and the file that
    # closes the five `SCHEMA_NOT_YET_MIGRATED` refusals in
    # `pg/integration_store.py`. It holds §11.8 to a build failure: no
    # pro-rata spreading, no silent drop, the unattributed bucket SET and
    # never incremented, replay idempotent against `ux_grn_line_external`,
    # signed paise on returns and credit notes, over-billing reported rather
    # than clamped, and an unmapped external status quarantined rather than
    # falling through to `bill.accounting_status`'s accounting-effective
    # DEFAULT.
    #
    # Split like agent 1's schema file and for the same reason: the live half
    # skips on every workstation here and first executes in CI's pg_tests job,
    # so everything assertable against the SOURCE is asserted against the
    # source. Two of the three defects it is written against -- the bucket
    # that added instead of setting, and a floored `divmod` on a negative --
    # are invisible to a live test that only inspects the final row.
    # Post-baseline like every Wave 2-6 file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # stop meaning anything the moment the product grows.
    "test_pg_procurement_ingest.py",


    # --- Wave 5 stream 1 (integration): ONE rate-budget implementation -----
    # Wave 5 shipped THREE implementations of the same reservation; two could
    # not execute, and every unit test over both passed because both talked to
    # in-memory doubles. These two files are the halves that a double cannot
    # provide:
    #   * test_pg_integration_rate_budget.py EXECUTES the repaired path
    #     against a live server (reserve, conflict/upsert, ceiling refusal,
    #     release). Gated on CAPEX_DB_URL and therefore skipped everywhere
    #     except CI's pg_tests job -- which is the point, and a skip is not a
    #     pass.
    #   * test_one_rate_budget_implementation.py fails if a FOURTH one ever
    #     appears. Source-level, so it holds whether or not the statement is
    #     ever executed -- which is what all three defects had in common.
    # Post-baseline like every Wave 2-5 file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # stop meaning anything the moment the product grows.
    "test_pg_integration_rate_budget.py",
    "test_one_rate_budget_implementation.py",

    # Wave 4 stream A2: submission (the engine's missing front end) and the
    # outcome write-back (its missing back end). Two of the three run with no
    # database, deliberately -- the properties they hold are the ones a
    # PostgreSQL-gated test would have skipped past on every dev machine.
    "test_budget_submission.py", "test_approval_writeback.py",
    "test_pg_approval_writeback_e2e.py",
    # --- Wave 5 stream 1: the product-agnostic adapter boundary ------------
    # D-14 is unresolved, so these hold the two candidate implementations to
    # one frozen interface and hold the "products are never mixed" rule to a
    # build failure. Post-baseline like every Wave 2-4 file: the 220 counts the
    # POC's audit-remediation suite, and inflating it would make the removal
    # guard stop meaning anything the moment the product grows.
    "test_integration_adapter_contract.py",
    "test_integration_capabilities.py",
    "test_integration_product_isolation.py",
    "test_integration_no_hardcoded_endpoints.py",
    # --- Wave 5: the integration platform ---------------------------------
    # Stream 3: the C16 integration status registry, the C17 raw-Zoho status
    # map and the module that applies them. Neither file touches a database,
    # the network or a tenant.
    "test_contracts_integration_statuses.py", "test_integration_statuses.py",
    # --- Wave 5 stream 4: rate budget, retry, backoff, circuit breaker ----
    # Runs with no database and no network: an injected clock, a seeded
    # jitter source and an in-process model of the two rate-budget
    # statements. Post-baseline like every other new file -- the 220 counts
    # the POC's audit-remediation suite, and inflating it would make the
    # removal guard meaningless.
    "test_integration_throttle.py",
    # --- Wave 5 stream 5: the chunked job framework and the sweeps ---------
    # Both run with no database and no network: the properties they hold --
    # a 12-minute soft deadline, a resumable cursor, idempotent replay -- are
    # properties of our code, and are proved with an injected clock rather
    # than by waiting twelve minutes.
    "test_integration_jobs.py", "test_integration_sweeps.py",
    # --- Wave 5 stream 6: outbound PO emission and idempotency -------------
    # Post-baseline like every other wave. All three run with no database and
    # no network: the properties they hold -- a Function killed mid-send, a
    # tenant without the Z-01 unique field, a detective control whose table is
    # missing -- are exactly the ones a PostgreSQL-gated test would skip past
    # on every dev machine, and they are the ones that decide whether a
    # commitment gets counted twice.
    "test_outbound_chaos.py", "test_outbound_emission.py",
    "test_outbound_unsanctioned.py",
    # --- Wave 5 stream 2: the integration schema and its repository --------
    # `test_pg_integration_schema.py` is split the way
    # `test_pg_approval_schema.py` is: a thorough database-free half that runs
    # everywhere, and a `@pytest.mark.pg` half that runs for the first time in
    # CI. `test_integration_store.py` is database-free ENTIRELY -- redaction,
    # window arithmetic and the scoped-query discipline are all properties of
    # the source, and a PostgreSQL-gated test would skip past every one of
    # them on the machine the module was written on.
    "test_pg_integration_schema.py", "test_integration_store.py",
    # --- Wave 6 stream A1: RLS enforced, rather than merely declared -------
    # The behavioural negative matrix for the nine tables 010/011 protect.
    # Post-baseline like every wave file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # meaningless. Two of its tests are database-free (a scan for policies
    # that pass `capex_scope_permits` its four same-typed arguments out of
    # order, and a check on the scoped-role fixture's own emitted SQL) and
    # run everywhere; the rest are `@pytest.mark.pg` and run for the first
    # time in CI.
    "test_pg_rls_integration_matrix.py",

    # --- Wave 5: the /api/integrations/* router ---------------------------
    # The guard, the frontend path-template contract, and the six operations
    # that answer a coded 503 rather than a synthesised number. Database-free
    # in its entirety and deliberately so: every property it holds is decided
    # in a router dependency or in the source, before a database is touched,
    # and a CAPEX_DB_URL gate would turn the whole file into a silent skip on
    # every machine without a server. Post-baseline like every Wave 2-6 file:
    # the 220 counts the POC's audit-remediation suite, and inflating it would
    # make the removal guard stop meaning anything.
    "test_integrations_api_guard.py",
    # --- Wave 6 stream B2: the two money-facing defects --------------------
    # `test_pg_reconciliation.py` is the PostgreSQL half of
    # `sweeps.SweepStore` -- a surface that had NO implementation at all, so
    # all eight sweeps ran exclusively against `tests/integration_fakes.py`.
    # Split the way `test_pg_integration_schema.py` is: seven database-free
    # tests that render the actual SQL and check it against migration 011's
    # DDL (including the one check `test_integration_sql_matches_schema.py`
    # says it cannot make -- that the `ON CONFLICT` target matches a real
    # partial unique index, columns AND predicate), and forty-seven live ones
    # gated on CAPEX_DB_URL that run for the first time in CI. A skip is not
    # a pass, and the live half has never executed anywhere else.
    #
    # `test_integration_outbound_money.py` is database-free entirely: the
    # outbound renderer floored `divmod(paise, 100)`, so every negative amount
    # that was not an exact multiple of 100 went out overstated -- and every
    # fixture in the suite was a round rupee, which is why it survived. Credit
    # notes, returns and reversals are the documents it was wrong for.
    #
    # Post-baseline like every wave file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # meaningless.
    "test_pg_reconciliation.py", "test_integration_outbound_money.py",

    # --- Wave 6, migration 014: the corrective migration -------------------
    # `test_ledger_cell_writers.py` runs with NO database at all, and that is
    # the whole reason it is a separate file. It holds the defect that made
    # this migration urgent: `budget_ledger_cell` has six derived money
    # columns and exactly ONE of them had a writer, so `available = budget -
    # (commitment + actual + pr_reserved)` had two terms permanently stuck at
    # zero. `actual_paise` in particular FALLS out of the subtraction the
    # moment a bill lands and `commitment_paise` drops -- availability RISES by
    # the billed amount and the same budget can be committed again, which is
    # AUD-C-001 re-opened. Its central guard reads the column list out of
    # `002_budget_control.sql` rather than from any register this repository
    # maintains, so the NEXT column added cannot silently join the unwritten
    # set.
    #
    # `test_pg_migration_014.py` is split the way `test_pg_procurement_schema.py`
    # is: a thorough source-level half that runs everywhere (the migration is
    # additive, the preflight precedes every DDL statement, no DELETE or
    # TRUNCATE anywhere, the status sets are the registries', the lifecycle
    # seed matches the POC row for row, the scope_permits argument order) and a
    # `@pytest.mark.pg` half that first executes in CI's pg_tests job -- the
    # over-commitment scenario end to end, both upgrade paths, rerun
    # idempotency, checksum drift, and the preflight refusing an ambiguity
    # rather than resolving it. A skip is not a pass, and its skip reason says
    # so.
    #
    # Post-baseline like every Wave 2-6 file: the 220 counts the POC's
    # audit-remediation suite, and inflating it would make the removal guard
    # stop meaning anything the moment the product grows.
    "test_ledger_cell_writers.py", "test_pg_migration_014.py",
}


def _assertion_count(node: ast.AST) -> int:
    """Assert statements plus pytest.raises blocks in one test function.

    Recording the NAME alone left the ratchet blind to the failure it exists to
    prevent: replacing every assertion in a test with `pass` kept the name, so
    nothing failed. The count makes a gutted test visible.
    """
    return sum(
        1 for n in ast.walk(node)
        if isinstance(n, ast.Assert)
        or (isinstance(n, ast.With)
            and any("raises" in ast.dump(item.context_expr) for item in n.items))
    )


def _body_hash(node: ast.AST) -> str:
    """Normalised structural hash of one test function body.

    Assertion COUNTS cannot see semantic weakening: replacing

        assert to_paise("0.005") == 1
    with
        assert to_paise("0.005") is not None

    keeps the count at one while destroying the property under test. The hash
    changes, so the gate fires.

    Normalised so that formatting churn does not create false alarms:
      * the docstring is excluded - prose may be improved freely
      * the body is unparsed back to canonical source, so reflowing,
        re-indenting or moving a test does not change the hash -- and
        neither does upgrading CPython, which an `ast.dump` of the AST
        repr does not survive
    A comment change is likewise invisible, because comments are not in the AST.
    """
    body = list(getattr(node, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # drop the docstring
    # `ast.unparse`, not `ast.dump`.
    #
    # `ast.dump` renders CPython's own AST repr, and that repr changes between
    # CPython releases -- moving this machine from 3.11 to 3.14 changed all 773
    # recorded hashes at once, with not one test edited. A gate that fires on
    # every test because the interpreter moved is a gate someone switches off,
    # and this one protects the 220 audit-remediation tests.
    #
    # `ast.unparse` round-trips the AST back to canonical source instead. It
    # still ignores comments, formatting, indentation and line numbers -- the
    # properties this hash was chosen for -- but its output is the Python
    # language, which is far more stable than an internal repr. CI runs 3.11
    # and a developer may run anything, so the hash has to survive that.
    dumped = chr(10).join(ast.unparse(n) for n in body)
    return hashlib.sha256(dumped.encode()).hexdigest()[:16]


def collect() -> dict[str, dict[str, dict]]:
    """{filename: {test_name: {assertions, body_sha}}}, recursing subdirectories.

    Recursive because pytest.ini sets `testpaths = tests`, which collects
    recursively - a non-recursive glob left tests/integration/ as a blind spot.
    """
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        key = path.relative_to(TESTS).as_posix()
        out[key] = {
            node.name: {
                "assertions": _assertion_count(node),
                "body_sha": _body_hash(node),
            }
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
    return out


def build() -> dict:
    inventory = collect()
    baseline = {f: n for f, n in inventory.items() if f not in POST_BASELINE_FILES}
    return {
        "generated_from": "tools/build_test_manifest.py",
        "purpose": (
            "Protects the audit remediation suite. Every test is named after the "
            "finding it closes; losing one silently loses the evidence that a "
            "critical finding stays closed."
        ),
        "policy": (
            "All control intent and traceability must be preserved. Documented "
            "PostgreSQL-specific adaptations are permitted, but no assertion may "
            "be weakened or removed without approval recorded in "
            "tests/ADAPTATIONS.md."
        ),
        "baseline": {
            "recorded_at": "2026-08-28",
            "commit_note": "POC baseline, branch phase-0a/foundations, before any port work",
            "test_functions": sum(len(v) for v in baseline.values()),
            "collected_cases": 348,
            "collected_cases_note": (
                "348 is the pytest-collected count after parametrisation expands "
                "6 files; 220 is the count of `def test_` functions. Both are real "
                "and the Definition of Done refers to the function count."
            ),
            "runtime_seconds_approx": 189,
            "database": "SQLite",
        },
        "files": inventory,
        "counts": {f: len(n) for f, n in sorted(inventory.items())},
        "assertions": {
            f: sum(t["assertions"] for t in n.values())
            for f, n in sorted(inventory.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    fresh = build()
    if args.check:
        if not MANIFEST.exists():
            print("TEST_MANIFEST.json does not exist. Run without --check.")
            return 1
        stored = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if stored["files"] != fresh["files"]:
            print("Test inventory has drifted from TEST_MANIFEST.json.")
            return 1
        print("Test inventory matches the manifest.")
        return 0

    MANIFEST.write_text(
        json.dumps(fresh, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {MANIFEST.relative_to(ROOT)} - "
        f"{sum(len(v) for v in fresh['files'].values())} test functions, "
        f"{sum(t['assertions'] for v in fresh['files'].values() for t in v.values())} assertions, "
        f"across {len(fresh['files'])} files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
