"""The integration platform's schema: what `010_integration.sql` must declare,
and -- against a live database -- what it must actually refuse.

Two layers, and the split matters, for exactly the reason
`tests/test_pg_approval_schema.py` states it:

  * **Database-free.** The migration is read as TEXT and checked for every
    table, column, constraint, trigger, index, RLS statement and privilege
    change seam C2 of `docs/WAVE5_CONTRACTS.md` requires, plus the agreement
    between that text, `app.backend.pg.integration_store`, and
    `research/30_contracts/C16_integration_statuses.json`. These run
    everywhere, including on a machine with no PostgreSQL, which is where this
    repository is developed.

  * **Live.** Text checks prove a constraint is DECLARED. They cannot prove it
    REFUSES anything, that a trigger fires, or that an RLS policy hides a row.
    Those need a real database, so the tests below marked `@pytest.mark.pg`
    create the illegal state and assert PostgreSQL rejects it.

**A skip is not a pass**, and Wave 5's brief says so in as many words. Nothing
in the live section below has run on the machine this file was written on. The
database-free section is deliberately thorough BECAUSE of that -- it is the
half that actually executes while the schema is being written -- and it goes
further than 008's equivalent in one respect that is specific to this
migration: `test_no_check_constraint_depends_on_the_session_timezone` is a
static check for a defect class that a live test would only catch if the test
happened to run in a session whose TimeZone differed from the one that created
the constraint, which no test in this repository does.
"""
from __future__ import annotations

# Fixtures come from tests/conftest_pg.py, imported explicitly. Without this
# the live tests below fail at SETUP with "fixture 'pg_database' not found" --
# but ONLY where CAPEX_DB_URL is set. Locally they skip, so the missing fixture
# is never resolved and the gap is invisible. CI is the first place these run
# for real, which is the whole point of that job.
import sys as _sys
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    _config_and_provider, pg_admin_connection, pg_connection, pg_database,
    pg_disposable_db_name, pg_scope, pg_template, pg_url,
)

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.backend.pg import integration_store as store  # noqa: E402
from app.backend.pg import migrate_pg  # noqa: E402

PROJECT_ROOT = _Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "pg"
MIGRATION_PATH = MIGRATIONS_DIR / "010_integration.sql"
C16_PATH = PROJECT_ROOT / "research" / "30_contracts" / "C16_integration_statuses.json"

SQL = MIGRATION_PATH.read_text(encoding="utf-8")
C16 = json.loads(C16_PATH.read_text(encoding="utf-8"))

#: The migration with `--` line comments stripped. Every structural assertion
#: uses this: the file's header names almost every object it creates, in prose,
#: so searching the raw text would happily "find" a constraint that exists only
#: in a comment. The ROLLBACK tests are the deliberate exception -- that
#: section IS a comment.
CODE = re.sub(r"--[^\n]*", "", SQL)

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason="PostgreSQL not configured; set CAPEX_DB_URL to run against a live database.",
)


def _table_body(table: str) -> str:
    """The text between a `CREATE TABLE`'s outermost parentheses.

    Paren-counted rather than regex-matched, for the reason
    `migrate_pg._extract_table_bodies` gives: every table here carries CHECK
    constraints with nested parens, and "up to the next `)`" stops inside the
    first one.
    """
    match = re.search(rf"CREATE TABLE {table}\s*\(", CODE)
    assert match, f"{table} has no CREATE TABLE in {MIGRATION_PATH.name}"
    depth, index = 1, match.end()
    while index < len(CODE) and depth:
        if CODE[index] == "(":
            depth += 1
        elif CODE[index] == ")":
            depth -= 1
        index += 1
    return CODE[match.end():index - 1]


# ============================================== the eight tables, and C2's names
def test_every_table_the_contract_names_is_created():
    """Seam C2 freezes seven table names; the migration creates those seven
    plus `integration_circuit`, which it declares as an addition in its own
    header. Streams 3-7 wrote SQL against the seven before this file landed, so
    a rename here is a build break somewhere else."""
    for table in store.INTEGRATION_TABLES:
        assert re.search(rf"^CREATE TABLE {table} \(", CODE, re.MULTILINE), (
            f"{table} is named by app.backend.pg.integration_store but is not "
            f"created by {MIGRATION_PATH.name}")


def test_the_store_module_and_the_migration_name_the_same_tables():
    """Both directions. A table created by the migration and absent from the
    store module is a table five streams will hand-type, and a name in the
    store module that no migration creates is a query that fails at runtime."""
    created = set(re.findall(r"^CREATE TABLE (\w+)", CODE, re.MULTILINE))
    assert created == set(store.INTEGRATION_TABLES)


C2_COLUMNS = {
    "integration_connection": ("connection_id", "entity_id", "product", "dc",
                               "organization_id", "connector_name", "mode",
                               "created_at", "created_by", "updated_at",
                               "updated_by"),
    "integration_inbox": ("inbox_id", "connection_id", "module", "external_id",
                          "payload_sha", "payload", "external_status_raw",
                          "received_at", "state"),
    "integration_outbox": ("outbox_id", "connection_id", "module", "local_id",
                           "dedupe_key", "payload", "state", "attempts",
                           "next_attempt_at"),
    "job": ("job_id", "kind", "state", "checkpoint", "soft_deadline_at",
            "resume_count", "correlation_id"),
    "integration_watermark": ("connection_id", "module", "hwm"),
    "integration_rate_budget": ("connection_id", "window_kind", "window_start",
                                "used"),
    "integration_event": ("event_id", "connection_id", "correlation_id"),
}


@pytest.mark.parametrize("table,columns", sorted(C2_COLUMNS.items()))
def test_every_column_seam_c2_freezes_is_declared(table, columns):
    """C2 lists these column names explicitly, and five other streams have
    already written SQL against them. This is the drift check for the names
    that are not this stream's to choose."""
    body = _table_body(table)
    for column in columns:
        assert re.search(rf"^\s*{column}\s+\S", body, re.MULTILINE), (
            f"{table}.{column} is frozen by docs/WAVE5_CONTRACTS.md seam C2 "
            f"and is not declared in {MIGRATION_PATH.name}")


# ================================================ 1. inbound idempotency (11.6)
def test_inbound_idempotency_is_a_named_unique_constraint():
    """Section 11.6: "Inbound idempotency is free: integration_inbox UNIQUE
    (connection_id, module, external_id, payload_sha)."

    Named, not left to PostgreSQL's generated name, because the writer targets
    it by name in `ON CONFLICT ON CONSTRAINT` -- and a constraint an
    application names in its SQL must be one the application chose.
    """
    body = _table_body("integration_inbox")
    match = re.search(
        rf"CONSTRAINT {store.INBOX_IDEMPOTENCY_CONSTRAINT}\s+UNIQUE\s*\(([^)]*)\)",
        body)
    assert match, (
        f"integration_inbox declares no UNIQUE constraint named "
        f"{store.INBOX_IDEMPOTENCY_CONSTRAINT}")
    columns = [c.strip() for c in match.group(1).split(",")]
    assert columns == ["connection_id", "module", "external_id", "payload_sha"], (
        f"the idempotency key is {columns}, not C2's "
        f"(connection_id, module, external_id, payload_sha). Dropping a column "
        f"widens it -- two different payloads for one external_id would "
        f"collide and the second would be lost; adding one narrows it, and a "
        f"re-delivery would insert a duplicate.")


def test_the_store_targets_the_idempotency_constraint_by_name():
    source = (PROJECT_ROOT / "app" / "backend" / "pg"
              / "integration_store.py").read_text(encoding="utf-8")
    assert "ON CONFLICT ON CONSTRAINT {INBOX_IDEMPOTENCY_CONSTRAINT} DO NOTHING" in source, (
        "record_inbound must let the constraint do the deduplication. A "
        "SELECT-then-INSERT is stale by the time it is acted on, and section "
        "11.5's overlapping poll windows make the race the normal case.")
    assert "SELECT 1 FROM integration_inbox" not in source, (
        "record_inbound must not pre-check for an existing receipt -- that is "
        "the code path the UNIQUE constraint exists to delete.")


# ============================================ 2. a connection cannot go live
def test_mode_defaults_to_mock():
    """Section 11.9: no live call without explicit authorisation. The DEFAULT
    is the half that matters most, because it covers the row that never
    mentioned mode at all -- which is the failure nobody sees."""
    body = _table_body("integration_connection")
    assert re.search(r"^\s*mode\s+text NOT NULL DEFAULT 'MOCK'", body,
                     re.MULTILINE), (
        "integration_connection.mode must DEFAULT to 'MOCK'. A connection that "
        "becomes live by omission is the failure mode this schema is required "
        "to design out.")


def test_a_live_mode_requires_a_recorded_authorisation():
    body = _table_body("integration_connection")
    assert "ck_integration_connection_live_is_authorised" in body
    clause = body[body.index("ck_integration_connection_live_is_authorised"):]
    for required in ("live_authorised_at", "live_authorised_by",
                     "live_authorisation_note"):
        assert required in clause[:600], (
            f"the live-mode constraint does not mention {required}; an "
            f"authoriser with no note records who to blame without recording "
            f"what they agreed to")


def test_no_base_url_or_endpoint_is_persisted_anywhere():
    """D-14 is unresolved and section 11 forbids a product fact reaching a
    caller. A connection stores `product` and `dc`; the adapter derives the
    rest and nothing persists it."""
    for forbidden in ("base_url", "api_endpoint", "endpoint_url", "scope_string"):
        assert forbidden not in CODE, (
            f"{forbidden} is persisted by {MIGRATION_PATH.name}. The target "
            f"product is PROVISIONAL (D-14); a stored URL is exactly the "
            f"product fact section 11 keeps behind the adapter.")
    assert "zohoapis" not in CODE.lower()


# ============================================ 3. a job cannot loop forever
def test_job_carries_the_three_columns_section_2_2_names():
    body = _table_body("job")
    assert re.search(r"^\s*checkpoint\s+jsonb", body, re.MULTILINE)
    assert re.search(r"^\s*soft_deadline_at\s+timestamptz", body, re.MULTILINE)
    assert re.search(r"^\s*resume_count\s+integer NOT NULL DEFAULT 0", body,
                     re.MULTILINE)


def test_the_resume_ceiling_is_terminal():
    """Section 2.2: "A job exceeding max_resume_count raises an alert rather
    than looping forever." AT the ceiling the only representable states are
    the two that stop, so there is no UPDATE that returns such a job to a
    claimable state."""
    body = _table_body("job")
    assert "ck_job_resume_ceiling_is_terminal" in body
    clause = body[body.index("ck_job_resume_ceiling_is_terminal"):][:400]
    assert "resume_count < max_resume_count" in clause
    assert "'DONE'" in clause and "'DEAD'" in clause


def test_a_dead_job_must_carry_its_alert():
    body = _table_body("job")
    assert "ck_job_dead_is_alerted" in body
    clause = body[body.index("ck_job_dead_is_alerted"):][:300]
    assert "alerted_at IS NOT NULL" in clause


def test_a_checkpointed_job_must_carry_a_cursor():
    """Without this, CHECKPOINTED degrades to "gave up politely": the next
    invocation restarts from the beginning while the state column claims
    progress was preserved."""
    body = _table_body("job")
    assert "ck_job_checkpointed_carries_a_cursor" in body
    clause = body[body.index("ck_job_checkpointed_carries_a_cursor"):][:300]
    assert "checkpoint IS NOT NULL" in clause


def test_the_soft_deadline_is_bounded_by_the_platform_ceiling():
    """Section 2.1's Functions ceiling is 15 minutes and section 2.2 puts the
    soft deadline at 80% of it. Bounded on the INTEGER column, so the bound is
    immutable -- see the timezone test below for why that matters."""
    body = _table_body("job")
    assert "ck_job_soft_deadline_within_platform_ceiling" in body
    clause = body[body.index("ck_job_soft_deadline_within_platform_ceiling"):][:300]
    assert str(store.PLATFORM_FUNCTION_CEILING_SECONDS) in clause
    assert f"DEFAULT {store.DEFAULT_SOFT_DEADLINE_SECONDS}" in body


# ==================================== 4. the rate budget, and the daily window
def test_both_windows_exist_and_share_one_shape():
    """The daily window is not an afterthought: MINUTE and DAY are two values
    of one column on one table, so there is no column only one of them has."""
    body = _table_body("integration_rate_budget")
    assert "ck_integration_rate_budget_window_kind" in body
    clause = body[body.index("ck_integration_rate_budget_window_kind"):][:300]
    assert "'MINUTE'" in clause and "'DAY'" in clause
    for column in ("ceiling", "used", "window_start", "window_start_key",
                   "window_seconds", "allocation", "window_tz"):
        assert re.search(rf"^\s*{column}\s+\S", body, re.MULTILINE), (
            f"integration_rate_budget.{column} is missing; both windows must "
            f"carry it or one of them is a special case")


def test_the_budget_is_binding_not_advisory():
    """`used <= ceiling` is what makes an over-budget reservation a refusal by
    the statement that would have spent it. Section 2.1 says there is no
    resident process, so there is nowhere else the race could be settled."""
    body = _table_body("integration_rate_budget")
    assert "ck_integration_rate_budget_used_within_ceiling" in body
    clause = body[body.index("ck_integration_rate_budget_used_within_ceiling"):][:300]
    assert "used <= ceiling" in clause


def test_the_window_length_is_an_immutable_integer():
    body = _table_body("integration_rate_budget")
    clause = body[body.index("ck_integration_rate_budget_window_seconds"):][:400]
    assert "window_seconds = 60" in clause
    assert "window_seconds = 86400" in clause


def test_the_daily_question_has_its_own_index():
    """"How much of today is left" is the question section 11.6 says actually
    binds on ERP Standard, and it is asked before every outbound call and
    every poll page. Sharing an index with the minute rows would make the
    binding constraint the second-class one."""
    assert re.search(
        r"CREATE INDEX ix_integration_rate_budget_day\b[^;]*WHERE window_kind = 'DAY'",
        CODE, re.DOTALL)


def test_the_connection_carries_a_per_connection_daily_ceiling():
    """Section 11.6 assumes ERP Standard's 2,000/day, and D-14 has not
    established the tier. A constant would make the assumption
    unchangeable per tenant."""
    body = _table_body("integration_connection")
    assert re.search(r"^\s*daily_call_ceiling\s+integer NOT NULL DEFAULT 2000",
                     body, re.MULTILINE)
    assert re.search(r"^\s*per_minute_call_ceiling\s+integer NOT NULL DEFAULT 100",
                     body, re.MULTILINE)


# ====================================================== the 300-second overlap
def test_the_poll_overlap_is_a_floor_not_a_default():
    """Contracts fact 2: `last_modified_time` is filterable but NOT sortable,
    so a window can be selected and not walked. The overlap is the whole of the
    protection against a record modified between a poll's read and its
    watermark write, and nothing downstream could detect the loss. A DEFAULT is
    a number the next person tunes down when the daily budget gets tight."""
    body = _table_body("integration_watermark")
    assert "ck_integration_watermark_overlap_floor" in body
    clause = body[body.index("ck_integration_watermark_overlap_floor"):][:200]
    assert f"overlap_seconds >= {store.WATERMARK_OVERLAP_SECONDS}" in clause


def test_a_watermark_rewind_needs_a_reason():
    assert "assert_integration_watermark_no_silent_rewind" in CODE
    assert re.search(
        r"CREATE TRIGGER integration_watermark_no_silent_rewind\s+"
        r"BEFORE UPDATE ON integration_watermark", CODE)


# ============================================== section 10.4 data classification
def test_the_restricted_key_list_is_one_list_in_two_places_that_agree():
    """The migration's `capex_restricted_payload_keys()` and the store's
    RESTRICTED_PAYLOAD_KEYS are checked in BOTH directions. A key in the SQL
    and not in Python means the redactor lets through what the constraint then
    refuses -- inbound stops, at 3am, in a cron function. A key in Python and
    not in the SQL means the backstop has a hole exactly where somebody thought
    they had closed one."""
    match = re.search(r"CREATE OR REPLACE FUNCTION capex_restricted_payload_keys"
                      r".*?SELECT ARRAY\[(.*?)\]::text\[\]", CODE, re.DOTALL)
    assert match, "capex_restricted_payload_keys() is not declared"
    in_sql = tuple(re.findall(r"'([^']+)'", match.group(1)))
    assert set(in_sql) == set(store.RESTRICTED_PAYLOAD_KEYS), (
        f"SQL has {sorted(set(in_sql) - set(store.RESTRICTED_PAYLOAD_KEYS))} "
        f"that Python does not; Python has "
        f"{sorted(set(store.RESTRICTED_PAYLOAD_KEYS) - set(in_sql))} that SQL "
        f"does not.")


@pytest.mark.parametrize("table,column", [
    ("integration_inbox", "payload"),
    ("integration_outbox", "payload"),
    ("integration_event", "detail"),
])
def test_every_stored_json_column_carries_the_restricted_key_backstop(table, column):
    """Section 10.4: Regulated and Restricted fields are encrypted or nulled on
    persist. The backstop makes forgetting the redactor loud rather than
    silent, and it has to cover the event `detail` too -- an event recording
    "bill 12345 could not be attributed" is precisely where a debug field
    carrying the offending vendor record ends up."""
    body = _table_body(table)
    assert f"capex_payload_carries_restricted_key({column})" in body, (
        f"{table}.{column} has no restricted-key CHECK; a raw payload that "
        f"skipped redact_payload would be stored looking redacted")


def test_the_redaction_policy_version_has_no_default():
    """NOT NULL with NO DEFAULT is what makes a hand-written INSERT that
    skipped the redactor FAIL rather than store an unredacted payload that
    looks redacted."""
    body = _table_body("integration_inbox")
    match = re.search(r"^\s*redaction_policy_version\s+([^,]*),", body,
                      re.MULTILINE)
    assert match, "integration_inbox.redaction_policy_version is not declared"
    declaration = match.group(1)
    assert "NOT NULL" in declaration
    assert "DEFAULT" not in declaration, (
        "a DEFAULT would let an insert that never ran the redactor claim it "
        "had, which is the one thing this column exists to prevent")


def test_a_classified_payload_must_show_what_it_did_about_it():
    body = _table_body("integration_inbox")
    assert "ck_integration_inbox_classification_shows_its_working" in body
    clause = body[body.index(
        "ck_integration_inbox_classification_shows_its_working"):][:500]
    assert "cardinality(redacted_keys)" in clause
    assert "payload_secret IS NOT NULL" in clause


def test_an_encryption_envelope_records_its_key_id():
    """Section 10.4: "Encryption is envelope with recorded key_id and a
    documented rotation procedure." Ciphertext whose key_id was not written
    down is not encrypted data, it is lost data."""
    body = _table_body("integration_inbox")
    clause = body[body.index("ck_integration_inbox_secret_key_id"):][:300]
    assert "(payload_secret IS NULL) = (payload_secret_key_id IS NULL)" in clause


def test_no_classified_field_is_hashed_anywhere():
    """Section 10.4 withdrew v1.0's `sha256(value)[:16]` on GSTIN and PAN: an
    irreversible transformation destroys the matching, statutory-reporting and
    audit-evidence function those identifiers exist for. `payload_sha` hashes
    the WHOLE payload as an identity and is not a treatment of any field."""
    for key in store.RESTRICTED_PAYLOAD_KEYS:
        assert f"digest({key}" not in CODE
        assert f"sha256({key}" not in CODE
        assert f"{key}_sha" not in CODE
        assert f"{key}_hash" not in CODE


# ======================================= no timezone-dependent CHECK anywhere
_TZ_DEPENDENT = (
    # date_trunc(text, timestamptz) is STABLE, not IMMUTABLE.
    r"\bdate_trunc\s*\(",
    # timestamptz +/- interval is STABLE: adding '1 day' across a DST boundary
    # depends on the session TimeZone.
    r"\binterval\s+'",
    r"\bmake_interval\s*\(",
    # extract(... FROM timestamptz) is STABLE for the same reason.
    r"\bextract\s*\(",
    r"\bnow\s*\(\s*\)",
    r"\bcurrent_(?:date|time|timestamp)\b",
)


def test_no_check_constraint_depends_on_the_session_timezone():
    """A STABLE expression inside a CHECK is a constraint whose truth depends
    on whichever session happens to evaluate it: a row valid for one
    connection's session is invalid for the next, and PostgreSQL does not warn.

    This is a static test because a live one would catch it only if the suite
    ran in a session whose TimeZone differed from the one that created the
    constraint -- which no test in this repository does, and which is exactly
    why the defect would reach production. Column DEFAULTs are excluded
    deliberately: `DEFAULT now()` is evaluated once at insert and is correct.
    """
    offenders: list[str] = []
    for match in re.finditer(r"CONSTRAINT (\w+)\s+CHECK\s*\(", CODE):
        name = match.group(1)
        depth, index = 1, match.end()
        while index < len(CODE) and depth:
            if CODE[index] == "(":
                depth += 1
            elif CODE[index] == ")":
                depth -= 1
            index += 1
        expression = CODE[match.end():index - 1]
        for pattern in _TZ_DEPENDENT:
            if re.search(pattern, expression, re.IGNORECASE):
                offenders.append(f"{name}: {pattern}")
    assert offenders == [], (
        f"these CHECK constraints contain a timezone-dependent (STABLE) "
        f"expression: {offenders}. Use an immutable integer -- "
        f"`window_seconds`, `soft_deadline_seconds` -- and compute the "
        f"boundary in the application, where the timezone it used can be "
        f"recorded alongside it.")


def test_the_helper_functions_are_declared_immutable():
    """Both are called from CHECK constraints, so a non-immutable one would be
    a constraint that could change its mind between evaluations."""
    for function in ("capex_restricted_payload_keys",
                     "capex_payload_carries_restricted_key"):
        match = re.search(
            rf"CREATE OR REPLACE FUNCTION {function}.*?LANGUAGE \w+ IMMUTABLE",
            CODE, re.DOTALL)
        assert match, f"{function} is not declared IMMUTABLE"


def test_the_restricted_key_predicate_is_plpgsql():
    """Not a style preference. A single-SELECT SQL function is a candidate for
    inlining, and `expression_planner()` runs over a CHECK expression when the
    constraint is created -- so an inlinable body would be spliced into the
    stored expression. The body needed here contains a loop over an array; in
    SQL that is an EXISTS sublink, and a SubLink is not something a CHECK
    expression should be relied on to carry. plpgsql is never inlined."""
    assert re.search(
        r"CREATE OR REPLACE FUNCTION capex_payload_carries_restricted_key.*?"
        r"LANGUAGE plpgsql IMMUTABLE", CODE, re.DOTALL)


# ============================================================ money discipline
def test_no_money_column_exists_in_this_migration():
    """Section 6.1: money is integer paise, and `SUM()` over bigint returns
    numeric. This migration has no money at all -- amounts on an outbound
    document live inside `integration_outbox.payload` as the integer paise the
    DTO already carries. A second copy in a column here would be a figure that
    could be wrong on its own."""
    assert "_paise" not in CODE, (
        "010_integration.sql declares a *_paise column. The ledger tables own "
        "money and this migration owns transport; adding an amount here for a "
        "screen's convenience creates a second place it can disagree.")


# ================================================================ append-only
def test_the_event_trail_is_append_only_by_trigger_and_by_privilege():
    """Both mechanisms, exactly as 001 uses both on `audit_log` and 008 on
    `approval_action`. A future migration re-running a blanket `GRANT ... ON
    ALL TABLES` silently undoes the REVOKE; a superuser bypasses privileges but
    not triggers."""
    assert "assert_integration_event_append_only" in CODE
    assert re.search(r"CREATE TRIGGER integration_event_no_update BEFORE UPDATE "
                     r"ON integration_event", CODE)
    assert re.search(r"CREATE TRIGGER integration_event_no_delete BEFORE DELETE "
                     r"ON integration_event", CODE)
    assert re.search(r"REVOKE UPDATE, DELETE ON integration_event FROM capex_app",
                     CODE)


def test_the_receipt_columns_are_frozen_by_trigger():
    """Contract C3: the raw external status is "stored verbatim and never
    overwritten". The identity columns are frozen alongside it for a sharper
    reason -- they are what the idempotency UNIQUE is built from, so an UPDATE
    that moved `payload_sha` would move the row out from under the constraint
    that deduplicated it."""
    assert "assert_integration_inbox_receipt_frozen" in CODE
    trigger = re.search(
        r"CREATE TRIGGER integration_inbox_receipt_frozen.*?EXECUTE FUNCTION",
        CODE, re.DOTALL)
    assert trigger, "the receipt-freeze trigger is not declared"
    when = trigger.group(0)
    for column in ("external_status_raw", "payload_sha", "payload",
                   "external_id", "module", "connection_id", "received_at"):
        assert f"NEW.{column}" in when, (
            f"{column} is not frozen by the receipt trigger")
    # ...and the processing columns are NOT frozen, or the table is unusable.
    for column in ("state", "attempts", "processed_at", "quarantine_reason"):
        assert f"NEW.{column}" not in when, (
            f"{column} is frozen, but processing a receipt is the entire "
            f"purpose of having one")


# ======================================================================= RLS
@pytest.mark.parametrize("table", store.INTEGRATION_TABLES)
def test_every_table_is_enabled_forced_and_policied(table):
    """All three statements, each catching a different silent failure: a policy
    with no ENABLE is inert; an ENABLE with no FORCE is bypassed by the table's
    OWNER, which in production is the deploy identity; ENABLE+FORCE with no
    policy denies everything and is caught only where something reads it."""
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in CODE
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in CODE
    assert re.search(rf"CREATE POLICY \w+ ON {table}\b", CODE)


@pytest.mark.parametrize("table", store.INTEGRATION_TABLES)
def test_every_policy_has_both_using_and_with_check(table):
    """USING alone filters reads while leaving a caller free to INSERT a row
    into a scope it cannot see."""
    match = re.search(rf"CREATE POLICY \w+ ON {table}\b(.*?);", CODE, re.DOTALL)
    assert match, f"{table} has no policy"
    assert "USING" in match.group(1)
    assert "WITH CHECK" in match.group(1)


def test_every_policy_calls_the_frozen_scope_function():
    """004's `capex_scope_permits`, whose body 007 replaced. This migration is
    expand-only and redefines neither."""
    for match in re.finditer(r"CREATE POLICY \w+ ON \w+(.*?);", CODE, re.DOTALL):
        assert "capex_scope_permits(" in match.group(1)
    assert "CREATE OR REPLACE FUNCTION capex_scope_permits" not in CODE
    assert "CREATE OR REPLACE FUNCTION capex_dimension_permits" not in CODE


# ============================================================ C16 agreement
def _c16_codes(namespace: str) -> set[str]:
    return {s["code"] for s in C16["namespaces"][namespace]["statuses"]}


@pytest.mark.parametrize("namespace,constant,table,column", [
    ("inbox", "INBOX_STATES", "integration_inbox", "state"),
    ("outbox", "OUTBOX_STATES", "integration_outbox", "state"),
    ("job", "JOB_STATES", "job", "state"),
    ("circuit", "CIRCUIT_STATES", "integration_circuit", "state"),
])
def test_states_are_transcribed_from_c16_not_invented(namespace, constant,
                                                      table, column):
    """Seam C2: "States come from C16_integration_statuses.json (stream 3),
    never invented." Three-way: the frozen registry, the Python constant, and
    the database CHECK must all carry the same set."""
    frozen = _c16_codes(namespace)
    assert set(getattr(store, constant)) == frozen, (
        f"integration_store.{constant} disagrees with C16's {namespace} "
        f"namespace: only in Python "
        f"{sorted(set(getattr(store, constant)) - frozen)}, only in C16 "
        f"{sorted(frozen - set(getattr(store, constant)))}")
    body = _table_body(table)
    match = re.search(rf"{column} IN \(([^)]*)\)", body, re.DOTALL)
    assert match, f"{table}.{column} has no IN (...) CHECK"
    in_sql = set(re.findall(r"'([^']+)'", match.group(1)))
    assert in_sql == frozen, (
        f"{table}.{column}'s CHECK disagrees with C16: only in SQL "
        f"{sorted(in_sql - frozen)}, only in C16 {sorted(frozen - in_sql)}")


def test_the_circuit_codes_keep_their_prefix():
    """C16's naming note: the bare code CLOSED collides with the C3 business
    status CLOSED, and the two mean opposite things -- a closed circuit is
    healthy and passing traffic, a closed project is terminal and blocks
    posting."""
    for code in store.CIRCUIT_STATES:
        assert code.startswith("CIRCUIT_")


def test_the_business_visible_badge_is_the_only_bridge():
    """C16 sanctions exactly one crossing into a business screen, and it is a
    BADGE alongside the C3 status rather than a 22nd business status."""
    assert set(C16["business_visible_bridge"]["badge_values"]) == {
        "QUEUED", "SENT", "FAILED"}
    assert store.integration_state_badge("PENDING", None) == "QUEUED"
    assert store.integration_state_badge("SENT", "ZID-1") == "SENT"
    assert store.integration_state_badge("FAILED", None) == "FAILED"
    assert store.integration_state_badge("DEAD", None) == "FAILED"
    assert store.integration_state_badge(None, None) is None


# ============================================================ housekeeping
def test_the_rls_handoff_for_this_migration_is_enumerable():
    """010's half of the pending-registry handoff, and the reason narrowing
    `test_pg_approval_schema.py::test_the_pending_registry_handoff_is_enumerable`
    to 008 lost nothing.

    Every table 010 creates is protected by 010 and named by none of them in
    `rls.py`'s registry, which is lead-owned. `scope_inventory` records that
    in-between state honestly rather than calling it 'covered' (untrue: the
    registry half is missing) or 'gap' (untrue: the policies exist). When
    `rls.py` gains these eight, this test's set becomes empty and these
    entries become 'covered' in the same commit.
    """
    from app.backend.pg import scope_inventory

    pending = set(scope_inventory.pending_registry_tables())
    mine = set(scope_inventory.tables_for_migration(
        store.INTEGRATION_MIGRATION))
    assert mine == set(store.INTEGRATION_TABLES), (
        "scope_inventory does not attribute exactly 010's tables to 010; "
        f"only in the inventory {sorted(mine - set(store.INTEGRATION_TABLES))}, "
        f"only in the migration {sorted(set(store.INTEGRATION_TABLES) - mine)}")
    assert pending & mine == mine, (
        f"these tables are created by 010 but are not awaiting the rls.py "
        f"registry: {sorted(mine - pending)}. If one has been added to "
        f"rls.py, reclassify it to 'covered' in the same commit.")


def test_the_migration_is_discovered_by_the_runner():
    versions = {m.version: m for m in migrate_pg.discover(MIGRATIONS_DIR)}
    assert "010" in versions
    assert versions["010"].path.name == MIGRATION_PATH.name


def test_the_rollback_section_undoes_everything_the_migration_creates():
    """008's convention. The ROLLBACK block is a comment, so this test reads the
    RAW text rather than CODE."""
    rollback = SQL[SQL.index("-- ROLLBACK:"):SQL.index("-- ROLLBACK:") + 4000]
    for table in store.INTEGRATION_TABLES:
        assert table in rollback, f"ROLLBACK does not mention {table}"
    for policy in re.findall(r"CREATE POLICY (\w+) ON", CODE):
        assert policy in rollback, f"ROLLBACK does not drop policy {policy}"
    for trigger in re.findall(r"CREATE TRIGGER (\w+)", CODE):
        assert trigger in rollback, f"ROLLBACK does not drop trigger {trigger}"
    for function in re.findall(r"CREATE OR REPLACE FUNCTION (\w+)", CODE):
        assert function in rollback, f"ROLLBACK does not drop function {function}"


def test_the_migration_is_expand_only():
    """Section 17.1: expand/contract. Nothing here may alter, drop or redefine
    anything 001..009 created, or the migration is not independently
    deployable and not reversible on its own."""
    for statement in re.findall(r"^\s*(ALTER TABLE \w+ [A-Z]+)", CODE,
                                re.MULTILINE):
        assert "ROW LEVEL SECURITY" in CODE[CODE.index(statement):
                                            CODE.index(statement) + 120], (
            f"{statement!r} is not an RLS statement; this migration may only "
            f"create new objects and enable RLS on them")
    assert "DROP TABLE" not in CODE
    assert "DROP COLUMN" not in CODE
    assert "DROP CONSTRAINT" not in CODE


def test_the_migration_is_pure_ascii():
    """Every migration 001..009 is. A section sign or an em dash renders
    differently under a C locale, and this file is executed as one string by
    psycopg against a CI runner whose locale nobody has pinned."""
    raw = MIGRATION_PATH.read_bytes()
    offenders = [i for i, b in enumerate(raw) if b > 127]
    assert offenders == [], (
        f"{MIGRATION_PATH.name} has non-ASCII bytes at {offenders[:5]}")


# =========================================================================
# ============================== LIVE ====================================
# Everything below needs a real PostgreSQL. None of it has run on the machine
# this file was written on. A skip is not a pass.
# =========================================================================

def _refused(con, statement, params=None, *, expect: str = "") -> str:
    """Assert PostgreSQL refuses `statement`; return the message.

    Rolls back either way, so one expected failure never poisons the
    transaction for the next assertion in the same test. Lifted from
    `tests/test_pg_approval_schema.py::_refused` deliberately -- the first
    draft of this file used bare `pytest.raises` blocks, and the two-refusal
    test would have reported `InFailedSqlTransaction` for its second assertion
    while looking, in the report, exactly like a passing test of the first.

    `expect` is matched against the message, because "refused" is not the
    assertion -- "refused FOR THIS REASON" is. A row that violates two
    constraints refuses either way, and a test that only checks for a refusal
    passes while the constraint it names does nothing.
    """
    try:
        con.execute(statement, params)
    except psycopg.Error as exc:
        con.rollback()
        message = str(exc)
        if expect:
            assert expect.lower() in message.lower(), (
                f"refused, but not for the stated reason.\nexpected "
                f"{expect!r} in: {message}")
        return message
    con.rollback()
    raise AssertionError(
        f"expected PostgreSQL to refuse this, and it did not: {statement}")


def _seed_estate(con: psycopg.Connection, *, entity_id: str = "ENT-1") -> None:
    """Organisation, entity and a service principal, committed.

    Raw SQL rather than `integration_store`, on purpose: these tests are about
    what the DATABASE refuses, and routing the setup through the module under
    test would let a Python-side guard stand in for a missing constraint.

    Runs as the connection's own (superuser) role, which bypasses RLS -- the
    same arrangement `test_pg_approval_schema.py::_build_estate` relies on.
    """
    con.execute(
        "INSERT INTO organisation (organisation_id, code, name, created_by,"
        " updated_by) VALUES ('ORG-1', 'ORG1', 'Test Org', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    con.execute(
        "INSERT INTO entity (entity_id, organisation_id, code, name,"
        " created_by, updated_by) VALUES (%s, 'ORG-1', %s, 'Test Entity',"
        " 'T', 'T') ON CONFLICT DO NOTHING", (entity_id, entity_id))
    con.execute(
        "INSERT INTO app_user (user_id, email, display_name, principal_kind,"
        " created_by, updated_by) VALUES ('SVC-INTEGRATION',"
        " 'svc@example.test', 'Integration service', 'SERVICE', 'T', 'T')"
        " ON CONFLICT DO NOTHING")
    con.commit()


def _seed_connection(con: psycopg.Connection, *, connection_id: str = "CONN-1",
                     entity_id: str = "ENT-1", **extra) -> str:
    """One `integration_connection` row on top of `_seed_estate`."""
    _seed_estate(con, entity_id=entity_id)
    columns = {"connection_id": connection_id, "entity_id": entity_id,
               "product": "ERP", "dc": "in", "organization_id": "60000000001",
               "connector_name": "zoho_erp", "created_by": "T",
               "updated_by": "T"}
    columns.update(extra)
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    con.execute(
        f"INSERT INTO integration_connection ({names}) VALUES ({placeholders})",
        tuple(columns.values()))
    con.commit()
    return connection_id


@PG
@pytest.mark.pg
def test_live_mode_defaults_to_mock(pg_connection):
    """The DEFAULT, not the constraint. The constraint stops a live row with no
    authoriser; the default is what stops a row that never mentioned mode at
    all -- and that is the failure nobody sees, because nothing about it looks
    wrong."""
    con = pg_connection
    _seed_estate(con)
    con.execute(
        "INSERT INTO integration_connection (connection_id, entity_id, product,"
        " dc, organization_id, connector_name, created_by, updated_by)"
        " VALUES ('CONN-D', 'ENT-1', 'ERP', 'in', '60000000001', 'zoho',"
        " 'T', 'T')")
    row = con.execute("SELECT mode FROM integration_connection "
                      "WHERE connection_id = 'CONN-D'").fetchone()
    assert row[0] == "MOCK", (
        "an INSERT that never mentioned mode produced a non-MOCK connection")


@PG
@pytest.mark.pg
def test_live_a_live_mode_without_authorisation_is_refused(pg_connection):
    con = pg_connection
    _seed_estate(con)
    _refused(
        con,
        "INSERT INTO integration_connection (connection_id, entity_id, product,"
        " dc, organization_id, connector_name, mode, created_by, updated_by)"
        " VALUES ('CONN-L', 'ENT-1', 'ERP', 'in', '60000000001', 'zoho',"
        " 'LIVE_WRITE', 'T', 'T')",
        expect="ck_integration_connection_live_is_authorised")


@PG
@pytest.mark.pg
def test_live_a_live_mode_with_an_authoriser_but_no_note_is_refused(pg_connection):
    """An authoriser with no note records who to blame without recording what
    they agreed to. Both halves, or the stamp is decoration."""
    con = pg_connection
    _seed_estate(con)
    _refused(
        con,
        "INSERT INTO integration_connection (connection_id, entity_id, product,"
        " dc, organization_id, connector_name, mode, live_authorised_at,"
        " live_authorised_by, created_by, updated_by)"
        " VALUES ('CONN-N', 'ENT-1', 'ERP', 'in', '60000000001', 'zoho',"
        " 'LIVE_READ', now(), 'rakesh', 'T', 'T')",
        expect="ck_integration_connection_live_is_authorised")


@PG
@pytest.mark.pg
def test_live_a_fully_authorised_live_mode_is_accepted(pg_connection):
    """The complement. Without it, a constraint that refused every live mode
    outright would pass both refusals above."""
    con = pg_connection
    _seed_connection(
        con, connection_id="CONN-OK", mode="LIVE_READ",
        live_authorised_at="2026-09-06T00:00:00+00:00",
        live_authorised_by="rakesh",
        live_authorisation_note="Phase 0B-2 read-only probe, approved in writing")
    row = con.execute("SELECT mode FROM integration_connection "
                      "WHERE connection_id = 'CONN-OK'").fetchone()
    assert row[0] == "LIVE_READ"


@PG
@pytest.mark.pg
def test_live_the_inbox_unique_deduplicates_a_redelivery(pg_connection):
    """Section 11.6's whole inbound story. Two poll invocations racing on the
    same bill under section 11.5's overlapping windows is the NORMAL case, and
    this is the only thing that resolves it."""
    con = pg_connection
    _seed_connection(con)
    insert = (
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version)"
        " VALUES (%s, 'CONN-1', 'bills', 'Z-1', 'sha-1', '{\"a\": 1}'::jsonb,"
        " 'v1') ON CONFLICT ON CONSTRAINT uq_integration_inbox_idempotency"
        " DO NOTHING RETURNING inbox_id")
    assert con.execute(insert, ("IB-1",)).fetchone() is not None
    assert con.execute(insert, ("IB-2",)).fetchone() is None, (
        "a re-delivered payload produced a second inbox row; inbound "
        "idempotency is supposed to be free from the constraint")
    assert con.execute(
        "SELECT count(*) FROM integration_inbox").fetchone()[0] == 1


@PG
@pytest.mark.pg
def test_live_a_different_payload_for_one_external_id_is_a_new_receipt(pg_connection):
    """The other half of the idempotency key, and the reason `payload_sha` is
    in it: a bill that CHANGED must arrive as a new receipt, not be swallowed
    as a duplicate of the version we already hold."""
    con = pg_connection
    _seed_connection(con)
    for inbox_id, sha in (("IB-1", "sha-1"), ("IB-2", "sha-2")):
        con.execute(
            "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
            " external_id, payload_sha, payload, redaction_policy_version)"
            " VALUES (%s, 'CONN-1', 'bills', 'Z-1', %s, '{}'::jsonb, 'v1')"
            " ON CONFLICT ON CONSTRAINT uq_integration_inbox_idempotency"
            " DO NOTHING", (inbox_id, sha))
    assert con.execute(
        "SELECT count(*) FROM integration_inbox").fetchone()[0] == 2


@PG
@pytest.mark.pg
def test_live_a_payload_carrying_a_regulated_key_is_refused(pg_connection):
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version)"
        " VALUES ('IB-G', 'CONN-1', 'bills', 'Z-2', 'sha-2',"
        " '{\"vendor\": {\"gst_no\": \"27ABCDE1234F1Z5\"}}'::jsonb, 'v1')",
        expect="carries_no_restricted_key")


@PG
@pytest.mark.pg
def test_live_a_restricted_key_is_caught_at_any_nesting_depth(pg_connection):
    """The backstop scans the RENDERED json text, so depth costs it nothing.
    This is the assertion that would fail if it were ever "simplified" to a
    top-level key test, which is the shape most people reach for first."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version)"
        " VALUES ('IB-B', 'CONN-1', 'bills', 'Z-3', 'sha-3',"
        " '{\"a\": {\"b\": [{\"account_number\": \"123\"}]}}'::jsonb,"
        " 'v1')",
        expect="carries_no_restricted_key")


@PG
@pytest.mark.pg
def test_live_a_payload_the_redactor_produced_is_accepted(pg_connection):
    """THE END-TO-END OF SECTION 10.4, and the test that caught this stream's
    sharpest defect.

    The redactor and the constraint have to agree, and in the first draft they
    did not: `redact_payload` kept the key with a placeholder value and the
    CHECK matches on key NAMES, so every redacted receipt would have been
    refused -- inbound stopping dead the first time a vendor record carried a
    GSTIN. Without this test the two halves would each have looked correct on
    their own, and the contradiction would have surfaced in CI at best.
    """
    con = pg_connection
    _seed_connection(con)
    sanitised, keys, classification = store.redact_payload(
        {"bill_id": "B1",
         "vendor": {"gst_no": "27ABCDE1234F1Z5", "name": "Acme",
                    "contact_persons": [{"pan_no": "ABCDE1234F"}]}})
    con.execute(
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version,"
        " payload_classification, redacted_keys)"
        " VALUES ('IB-R', 'CONN-1', 'bills', 'Z-4', 'sha-4', %s::jsonb,"
        " %s, %s, %s)",
        (json.dumps(sanitised), store.REDACTION_POLICY_VERSION,
         classification, list(keys)))
    row = con.execute(
        "SELECT payload, payload_classification, redacted_keys FROM "
        "integration_inbox WHERE inbox_id = 'IB-R'").fetchone()
    assert row[0] == {"bill_id": "B1",
                      "vendor": {"name": "Acme", "contact_persons": [{}]}}
    assert row[1] == "REGULATED"
    assert sorted(row[2]) == ["gst_no", "pan_no"]


@PG
@pytest.mark.pg
def test_live_a_regulated_row_showing_no_treatment_is_refused(pg_connection):
    """A label with nothing behind it. A row saying "this carried regulated
    data" and showing neither a removed key nor an envelope is not a
    classification, it is a claim."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version,"
        " payload_classification)"
        " VALUES ('IB-X', 'CONN-1', 'bills', 'Z-5', 'sha-5',"
        " '{\"a\": 1}'::jsonb, 'v1', 'REGULATED')",
        expect="classification_shows_its_working")


@PG
@pytest.mark.pg
def test_live_a_receipt_without_a_redaction_policy_is_refused(pg_connection):
    """NOT NULL with no DEFAULT. This is what makes a hand-written INSERT that
    skipped the redactor fail rather than store an unredacted payload that
    looks redacted."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload)"
        " VALUES ('IB-NP', 'CONN-1', 'bills', 'Z-9', 'sha-9', '{}'::jsonb)",
        expect="redaction_policy_version")


@PG
@pytest.mark.pg
def test_live_the_raw_external_status_cannot_be_overwritten(pg_connection):
    """Contract C3: stored verbatim, never overwritten. It is what makes an
    UNMAPPED_EXTERNAL_STATUS exception resolvable months later."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version,"
        " external_status_raw)"
        " VALUES ('IB-S', 'CONN-1', 'bills', 'Z-6', 'sha-6', '{}'::jsonb,"
        " 'v1', 'partially_paid')")
    con.commit()
    _refused(con,
             "UPDATE integration_inbox SET external_status_raw = 'paid' "
             "WHERE inbox_id = 'IB-S'",
             expect="frozen at insert")
    _refused(con,
             "UPDATE integration_inbox SET payload_sha = 'sha-other' "
             "WHERE inbox_id = 'IB-S'",
             expect="frozen at insert")


@PG
@pytest.mark.pg
def test_live_processing_a_receipt_is_still_permitted(pg_connection):
    """The freeze is column-scoped. Table-wide it would make the inbox
    write-once, which is to say useless."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version)"
        " VALUES ('IB-P', 'CONN-1', 'bills', 'Z-7', 'sha-7', '{}'::jsonb, 'v1')")
    con.execute("UPDATE integration_inbox SET state = 'PROCESSED', "
                "processed_at = now() WHERE inbox_id = 'IB-P'")
    row = con.execute("SELECT state FROM integration_inbox "
                      "WHERE inbox_id = 'IB-P'").fetchone()
    assert row[0] == "PROCESSED"


@PG
@pytest.mark.pg
def test_live_a_processed_receipt_must_say_when(pg_connection):
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
        " external_id, payload_sha, payload, redaction_policy_version)"
        " VALUES ('IB-W', 'CONN-1', 'bills', 'Z-8', 'sha-8', '{}'::jsonb, 'v1')")
    con.commit()
    _refused(con,
             "UPDATE integration_inbox SET state = 'PROCESSED' "
             "WHERE inbox_id = 'IB-W'",
             expect="ck_integration_inbox_processed_at")


@PG
@pytest.mark.pg
def test_live_a_rate_budget_cannot_exceed_its_ceiling(pg_connection):
    """The binding constraint. Section 2.1 says no process is resident, so this
    is the only place two concurrent cron invocations can be arbitrated."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_rate_budget (connection_id, window_kind,"
        " allocation, window_start, window_start_key, window_seconds,"
        " window_tz, ceiling, used)"
        " VALUES ('CONN-1', 'DAY', 'POLLING', now(), '2026-09-06', 86400,"
        " 'UTC', 1200, 1199)")
    con.execute("UPDATE integration_rate_budget SET used = 1200, "
                "exhausted_at = now() WHERE window_kind = 'DAY'")
    con.commit()
    _refused(con,
             "UPDATE integration_rate_budget SET used = 1201 "
             "WHERE window_kind = 'DAY'",
             expect="used_within_ceiling")


@PG
@pytest.mark.pg
def test_live_a_full_window_must_stamp_its_exhaustion(pg_connection):
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_rate_budget (connection_id, window_kind,"
        " allocation, window_start, window_start_key, window_seconds,"
        " window_tz, ceiling, used)"
        " VALUES ('CONN-1', 'DAY', 'POLLING', now(), '2026-09-06', 86400,"
        " 'UTC', 100, 100)",
        expect="exhausted_stamp")


@PG
@pytest.mark.pg
def test_live_a_minute_window_cannot_claim_a_days_length(pg_connection):
    """A MINUTE row carrying 86,400 seconds would make the per-minute throttle
    a per-day one, and nothing else in the system would notice."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_rate_budget (connection_id, window_kind,"
        " allocation, window_start, window_start_key, window_seconds,"
        " window_tz, ceiling)"
        " VALUES ('CONN-1', 'MINUTE', 'POLLING', now(), '2026-09-06T00:00',"
        " 86400, 'UTC', 60)",
        expect="window_seconds")


@PG
@pytest.mark.pg
def test_live_a_job_at_its_resume_ceiling_cannot_be_claimable(pg_connection):
    """Section 2.2's "alerts rather than looping forever", as a constraint. AT
    the ceiling the only representable states are the two that stop."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO job (job_id, kind, principal_user_id, max_resume_count,"
        " resume_count, created_by, updated_by) VALUES ('J-1', 'poll_bills',"
        " 'SVC-INTEGRATION', 3, 2, 'T', 'T')")
    con.commit()
    _refused(con, "UPDATE job SET resume_count = 3 WHERE job_id = 'J-1'",
             expect="resume_ceiling_is_terminal")
    # ...and the permitted landing, so the constraint is not simply a wall.
    con.execute("UPDATE job SET resume_count = 3, state = 'DEAD', "
                "alerted_at = now(), finished_at = now() WHERE job_id = 'J-1'")
    assert con.execute("SELECT state FROM job WHERE job_id = 'J-1'"
                       ).fetchone()[0] == "DEAD"


@PG
@pytest.mark.pg
def test_live_a_dead_job_without_an_alert_is_refused(pg_connection):
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO job (job_id, kind, state, principal_user_id,"
        " finished_at, created_by, updated_by) VALUES ('J-2', 'poll_bills',"
        " 'DEAD', 'SVC-INTEGRATION', now(), 'T', 'T')",
        expect="ck_job_dead_is_alerted")


@PG
@pytest.mark.pg
def test_live_a_checkpointed_job_without_a_cursor_is_refused(pg_connection):
    """Without this, CHECKPOINTED means "gave up politely": the next
    invocation restarts from nothing while the state column claims progress."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO job (job_id, kind, state, principal_user_id,"
        " created_by, updated_by) VALUES ('J-3', 'poll_bills',"
        " 'CHECKPOINTED', 'SVC-INTEGRATION', 'T', 'T')",
        expect="checkpointed_carries_a_cursor")


@PG
@pytest.mark.pg
def test_live_a_claimed_job_must_be_bounded_and_owned(pg_connection):
    """The reaper's precondition. A claim with no expiry and no holder cannot
    be told apart from a live invocation, so a killed Function's job is either
    stolen from a running worker or leaked forever."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO job (job_id, kind, state, principal_user_id,"
        " created_by, updated_by) VALUES ('J-4', 'poll_bills', 'CLAIMED',"
        " 'SVC-INTEGRATION', 'T', 'T')",
        expect="ck_job_claimed_is_bounded")


@PG
@pytest.mark.pg
def test_live_a_soft_deadline_past_the_platform_ceiling_is_refused(pg_connection):
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO job (job_id, kind, principal_user_id,"
        " soft_deadline_seconds, created_by, updated_by) VALUES ('J-5',"
        " 'poll_bills', 'SVC-INTEGRATION', 1800, 'T', 'T')",
        expect="soft_deadline_within_platform_ceiling")


@PG
@pytest.mark.pg
def test_live_a_watermark_cannot_rewind_without_a_reason(pg_connection):
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_watermark (connection_id, module, hwm,"
        " updated_by) VALUES ('CONN-1', 'bills',"
        " '2026-09-06T12:00:00+00:00', 'T')")
    con.commit()
    _refused(con,
             "UPDATE integration_watermark SET hwm = "
             "'2026-09-01T00:00:00+00:00' WHERE module = 'bills'",
             expect="may not move backwards")


@PG
@pytest.mark.pg
def test_live_a_declared_rewind_is_permitted(pg_connection):
    """The complement. A trigger that refused every rewind would make a
    backfill impossible, and the requirement is that a rewind be DECLARED, not
    that it be forbidden."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_watermark (connection_id, module, hwm,"
        " updated_by) VALUES ('CONN-1', 'bills',"
        " '2026-09-06T12:00:00+00:00', 'T')")
    con.execute(
        "UPDATE integration_watermark SET hwm = '2026-09-01T00:00:00+00:00',"
        " rewound_at = now(), rewind_reason = 'backfill after mapping fix'"
        " WHERE module = 'bills'")
    row = con.execute("SELECT hwm, rewind_reason FROM integration_watermark "
                      "WHERE module = 'bills'").fetchone()
    assert row[1] == "backfill after mapping fix"


@PG
@pytest.mark.pg
def test_live_an_overlap_below_the_floor_is_refused(pg_connection):
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_watermark (connection_id, module, hwm,"
        " overlap_seconds, updated_by) VALUES ('CONN-1', 'bills', now(), 30,"
        " 'T')",
        expect="overlap_floor")


@PG
@pytest.mark.pg
def test_live_a_closed_circuit_may_still_be_accumulating_failures(pg_connection):
    """The regression test for a constraint this stream got wrong first time.
    An earlier draft required `consecutive_counted_failures = 0` for a CLOSED
    circuit, which made the first four failures of every outage unstorable --
    and passed every text assertion in this file."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_circuit (connection_id, module, state,"
        " consecutive_counted_failures, first_failure_at) VALUES"
        " ('CONN-1', 'bills', 'CIRCUIT_CLOSED', 4, now())")
    row = con.execute("SELECT consecutive_counted_failures FROM "
                      "integration_circuit WHERE module = 'bills'").fetchone()
    assert row[0] == 4


@PG
@pytest.mark.pg
def test_live_a_failure_count_without_a_start_time_is_refused(pg_connection):
    """Section 11.6 counts "5 consecutive counted failures in 60 s". Without a
    start time the 60-second window cannot be evaluated at all."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_circuit (connection_id, module,"
        " consecutive_counted_failures) VALUES ('CONN-1', 'bills', 3)",
        expect="failure_window")


@PG
@pytest.mark.pg
def test_live_an_open_circuit_must_schedule_a_probe(pg_connection):
    """OPEN with no scheduled probe is not a circuit breaker; it is an outage
    that waits for a human to notice."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_circuit (connection_id, module, state,"
        " opened_at, opened_reason) VALUES ('CONN-1', 'bills',"
        " 'CIRCUIT_OPEN', now(), 'five 5xx in 60s')",
        expect="open_is_explained")


@PG
@pytest.mark.pg
def test_live_the_event_trail_refuses_update_and_delete(pg_connection):
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_event (connection_id, kind, actor)"
        " VALUES ('CONN-1', 'POLL_STARTED', 'SVC-INTEGRATION')")
    con.commit()
    _refused(con, "UPDATE integration_event SET kind = 'X'",
             expect="append-only")
    _refused(con, "DELETE FROM integration_event", expect="append-only")


@PG
@pytest.mark.pg
def test_live_the_event_id_is_minted_by_the_database(pg_connection):
    """`GENERATED ALWAYS AS IDENTITY` rejects an explicit value outright --
    the defect Wave 4 shipped on `approval_action` and found only against a
    live database."""
    con = pg_connection
    _seed_connection(con)
    row = con.execute(
        "INSERT INTO integration_event (connection_id, kind, actor)"
        " VALUES ('CONN-1', 'POLL_STARTED', 'SVC') RETURNING event_id"
    ).fetchone()
    assert isinstance(row[0], int)
    con.commit()
    _refused(con,
             "INSERT INTO integration_event (event_id, connection_id, kind,"
             " actor) VALUES (99, 'CONN-1', 'X', 'SVC')",
             expect="identity")


@PG
@pytest.mark.pg
def test_live_an_event_detail_carrying_a_restricted_key_is_refused(pg_connection):
    """An event recording "bill 12345 could not be attributed" is precisely
    where a well-meaning debug field carrying the offending vendor record ends
    up."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_event (connection_id, kind, actor, detail)"
        " VALUES ('CONN-1', 'QUARANTINED', 'SVC',"
        " '{\"vendor\": {\"pan_no\": \"ABCDE1234F\"}}'::jsonb)",
        expect="carries_no_restricted_key")


@PG
@pytest.mark.pg
def test_live_an_outbox_row_cannot_be_sent_without_an_external_id(pg_connection):
    """C16's SENT is "accepted by Zoho; external_id persisted". SENT without
    one is a claim we cannot substantiate, and the retry path would then have
    no id to update by -- which is how a duplicate purchase order happens."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_outbox (outbox_id, connection_id, module,"
        " local_id, dedupe_key, payload, state, sent_at, created_by,"
        " updated_by) VALUES ('OB-1', 'CONN-1', 'purchaseorders', 'PO-1',"
        " 'CAPEX-PO-1', '{}'::jsonb, 'SENT', now(), 'T', 'T')",
        expect="sent_carries_external_id")


@PG
@pytest.mark.pg
def test_live_a_failed_outbox_row_must_schedule_its_retry(pg_connection):
    """A retryable failure that scheduled no retry is not retryable; it is a
    row that stops moving and appears on no queue."""
    con = pg_connection
    _seed_connection(con)
    _refused(
        con,
        "INSERT INTO integration_outbox (outbox_id, connection_id, module,"
        " local_id, dedupe_key, payload, state, attempts, created_by,"
        " updated_by) VALUES ('OB-F', 'CONN-1', 'purchaseorders', 'PO-9',"
        " 'CAPEX-PO-9', '{}'::jsonb, 'FAILED', 1, 'T', 'T')",
        expect="failed_reschedules")


@PG
@pytest.mark.pg
def test_live_two_outbox_rows_cannot_share_a_dedupe_key(pg_connection):
    """Zoho enforces uniqueness of `cf_capex_ref` within an organisation, so
    two local rows sharing one would be two rows fighting over one remote
    document -- and the second would silently overwrite the first."""
    con = pg_connection
    _seed_connection(con)
    con.execute(
        "INSERT INTO integration_outbox (outbox_id, connection_id, module,"
        " local_id, dedupe_key, payload, created_by, updated_by) VALUES"
        " ('OB-A', 'CONN-1', 'purchaseorders', 'PO-1', 'CAPEX-PO-1',"
        " '{}'::jsonb, 'T', 'T')")
    con.commit()
    _refused(
        con,
        "INSERT INTO integration_outbox (outbox_id, connection_id, module,"
        " local_id, dedupe_key, payload, created_by, updated_by) VALUES"
        " ('OB-B', 'CONN-1', 'purchaseorders', 'PO-2', 'CAPEX-PO-1',"
        " '{}'::jsonb, 'T', 'T')",
        expect="uq_integration_outbox_dedupe_key")


@PG
@pytest.mark.pg
def test_live_rls_hides_another_entitys_receipts(pg_connection, pg_url,
                                                 pg_disposable_db_name):
    """The policies, not merely their declaration. Two entities, one receipt
    each, then a scoped session that must see exactly one.

    Run through `Database.session()` rather than the raw superuser connection,
    because a superuser BYPASSES RLS entirely -- a version of this test written
    on `pg_connection` would see both rows and pass only if the assertion were
    written to expect that.
    """
    con = pg_connection
    _seed_estate(con, entity_id="ENT-A")
    _seed_estate(con, entity_id="ENT-B")
    for connection_id, entity in (("CONN-A", "ENT-A"), ("CONN-B", "ENT-B")):
        con.execute(
            "INSERT INTO integration_connection (connection_id, entity_id,"
            " product, dc, organization_id, connector_name, created_by,"
            " updated_by) VALUES (%s, %s, 'ERP', 'in', %s, 'zoho', 'T', 'T')",
            (connection_id, entity, f"ORG-{entity}"))
        con.execute(
            "INSERT INTO integration_inbox (inbox_id, connection_id, module,"
            " external_id, payload_sha, payload, redaction_policy_version)"
            " VALUES (%s, %s, 'bills', 'Z-1', 'sha-1', '{}'::jsonb, 'v1')",
            (f"IB-{entity}", connection_id))
    con.commit()

    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, pg_disposable_db_name)
    database = pg_engine.Database(cfg, secret_provider=provider,
                                  min_size=1, max_size=2)
    scope = pg_engine.Scope(
        user_id="U-A", principal_kind="USER",
        entity_ids=frozenset({"ENT-A"}), plant_ids=None,
        project_ids=None, location_ids=None, read_all=False)
    try:
        with database.session(scope) as session:
            rows = session.fetchall(  # scope-exempt: asserting RLS itself
                "SELECT inbox_id FROM integration_inbox ORDER BY inbox_id")
    finally:
        database.close()
    assert [r[0] for r in rows] == ["IB-ENT-A"], (
        "the RLS policy on integration_inbox did not confine the read to the "
        "principal's entity; another entity's supplier invoices were visible")


# =========================================================================
# The store and the schema, end to end. Still live-only, still unrun here.
# =========================================================================

def _scoped_session(pg_url, dbname, entity_ids):
    """A `Database` and a `Scope` over `entity_ids`, for the store functions.

    The raw `pg_connection` fixture is a superuser and BYPASSES RLS, so it
    cannot be used to test anything the policies do -- a test written on it
    would see every entity's rows and pass only if its assertions expected
    that.
    """
    from app.backend.pg import engine as pg_engine

    cfg, provider = _config_and_provider(pg_url, dbname)
    database = pg_engine.Database(cfg, secret_provider=provider,
                                  min_size=1, max_size=2)
    scope = pg_engine.Scope(
        user_id="SVC-INTEGRATION", principal_kind="SERVICE",
        entity_ids=frozenset(entity_ids), plant_ids=None,
        project_ids=None, location_ids=None, read_all=False)
    return database, scope


@PG
@pytest.mark.pg
def test_live_record_inbound_tells_a_duplicate_from_an_unknown_connection(
        pg_connection, pg_url, pg_disposable_db_name):
    """The two failures that used to look identical.

    `INSERT ... WHERE EXISTS(scope) ON CONFLICT DO NOTHING RETURNING` gives
    back nothing whether the payload was a re-delivery or the connection was
    invisible, and a poller reading that as "duplicate, skip" would skip every
    payload on a connection it cannot see -- silently, for as long as anybody
    relied on it. The two CTEs exist to keep them apart, and only a live
    database can show that they do.
    """
    con = pg_connection
    _seed_estate(con, entity_id="ENT-A")
    _seed_estate(con, entity_id="ENT-B")
    for connection_id, entity in (("CONN-A", "ENT-A"), ("CONN-B", "ENT-B")):
        con.execute(
            "INSERT INTO integration_connection (connection_id, entity_id,"
            " product, dc, organization_id, connector_name, created_by,"
            " updated_by) VALUES (%s, %s, 'ERP', 'in', %s, 'zoho', 'T', 'T')",
            (connection_id, entity, "ORG-" + entity))
    con.commit()

    database, scope = _scoped_session(pg_url, pg_disposable_db_name, {"ENT-A"})
    payload = {"bill_id": "B-1", "total": 12345}
    try:
        with database.session(scope) as session:
            inbox_id, inserted = store.record_inbound(
                session, inbox_id="IB-1", connection_id="CONN-A",
                module="bills", external_id="Z-1", raw_payload=payload)
            assert (inbox_id, inserted) == ("IB-1", True)

        # The same payload again: the CONSTRAINT deduplicates it.
        with database.session(scope) as session:
            assert store.record_inbound(
                session, inbox_id="IB-2", connection_id="CONN-A",
                module="bills", external_id="Z-1",
                raw_payload=payload) == (None, False)

        # A connection this principal cannot see is NOT a duplicate.
        with database.session(scope) as session:
            with pytest.raises(store.IntegrationStoreError) as excinfo:
                store.record_inbound(
                    session, inbox_id="IB-3", connection_id="CONN-B",
                    module="bills", external_id="Z-9", raw_payload=payload)
            assert excinfo.value.code == "CONNECTION_NOT_FOUND"
    finally:
        database.close()

    assert con.execute(
        "SELECT count(*) FROM integration_inbox").fetchone()[0] == 1


@PG
@pytest.mark.pg
def test_live_reserve_calls_charges_both_windows_or_neither(
        pg_connection, pg_url, pg_disposable_db_name):
    """The savepoint, which nothing static can hold.

    Three things at once: both windows are charged together; a reservation the
    DAY window cannot afford leaves the MINUTE window untouched; and the
    caller's transaction is still USABLE afterwards, which is what lets a
    poller checkpoint and return (section 2.2) instead of losing the work it
    had already done this invocation.
    """
    con = pg_connection
    _seed_estate(con, entity_id="ENT-A")
    con.execute(
        "INSERT INTO integration_connection (connection_id, entity_id, product,"
        " dc, organization_id, connector_name, per_minute_call_ceiling,"
        " daily_call_ceiling, created_by, updated_by) VALUES ('CONN-A',"
        " 'ENT-A', 'ERP', 'in', 'ORG-A', 'zoho', 100, 20, 'T', 'T')")
    con.commit()

    # POLLING gets 60% of the minute (60) and 60% of the day (12).
    database, scope = _scoped_session(pg_url, pg_disposable_db_name, {"ENT-A"})
    moment = datetime(2026, 9, 6, 14, 23, tzinfo=timezone.utc)
    try:
        with database.session(scope) as session:
            granted = store.reserve_calls(
                session, connection_id="CONN-A", allocation="POLLING",
                count=10, now=moment)
            assert granted["MINUTE"]["used"] == 10
            assert granted["DAY"]["used"] == 10
            assert granted["DAY"]["ceiling"] == 12

            # Five more fits the minute (60) and not the day (12).
            with pytest.raises(store.RateBudgetExhausted) as excinfo:
                store.reserve_calls(
                    session, connection_id="CONN-A", allocation="POLLING",
                    count=5, now=moment)
            assert excinfo.value.window_kind == "DAY", (
                "the wrong window was named: a MINUTE exhaustion checkpoints "
                "and resumes on the next tick, a DAY exhaustion opens the "
                "circuit until the day boundary and alerts")

            # The transaction survived, and neither window was charged.
            state = store.read_rate_budget(
                session, connection_id="CONN-A", allocation="POLLING",
                now=moment)
            assert state["MINUTE"]["used"] == 10, (
                "the minute window was charged for a call the day refused")
            assert state["DAY"]["used"] == 10
    finally:
        database.close()


@PG
@pytest.mark.pg
def test_live_a_job_that_exhausts_its_resumes_dies_alerting(
        pg_connection, pg_url, pg_disposable_db_name):
    """Section 2.2's sentence, end to end: claim, checkpoint, claim,
    checkpoint... and at the ceiling the job goes DEAD with an alert rather
    than being handed back to the scheduler again."""
    con = pg_connection
    _seed_estate(con, entity_id="ENT-A")
    con.commit()

    database, scope = _scoped_session(pg_url, pg_disposable_db_name, {"ENT-A"})
    try:
        with database.session(scope) as session:
            store.enqueue_job(session, job_id="J-1", kind="poll_bills",
                              principal_user_id="SVC-INTEGRATION",
                              actor="T", entity_id="ENT-A",
                              max_resume_count=2)
        for expected in ("CHECKPOINTED", "DEAD"):
            with database.session(scope) as session:
                claimed = store.claim_job(session, kind="poll_bills",
                                          worker="w1")
                assert [job["job_id"] for job in claimed] == ["J-1"]
                assert store.checkpoint_job(
                    session, job_id="J-1", checkpoint={"page": 3},
                    actor="T") == expected
        with database.session(scope) as session:
            assert store.claim_job(session, kind="poll_bills",
                                   worker="w1") == [], (
                "a job at its resume ceiling was claimed again; section 2.2 "
                "requires it to alert rather than loop forever")
    finally:
        database.close()

    row = con.execute(
        "SELECT state, alerted_at, finished_at, last_error FROM job "
        "WHERE job_id = 'J-1'").fetchone()
    assert row[0] == "DEAD"
    assert row[1] is not None, "a DEAD job with no alert is a silent give-up"
    assert row[2] is not None
    assert "RESUME_CEILING_EXCEEDED" in row[3]
