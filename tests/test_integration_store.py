"""`app/backend/pg/integration_store.py`: the pure functions, and the shape of
its SQL.

Runs with NO database, deliberately, and that is the point rather than a
concession. Every property held here is one a PostgreSQL-gated test would skip
past on the machine this module was written on:

  * redaction and classification are pure functions over a payload, and they
    are the half of section 10.4 that runs BEFORE the row reaches the CHECK
    constraint. If the redactor is wrong, the constraint refuses the insert --
    correctly, at 3am, inside a cron function, and inbound stops.
  * the window arithmetic decides which bucket a call is charged to. It is in
    Python precisely because every PostgreSQL expression that could do it is
    timezone-dependent, so there is no live test that could hold it.
  * the scoped-query discipline is a property of the SOURCE. The gate in
    `tests/test_scope_enforcement.py` walks `session.fetchall/fetchone/execute`
    and this module uses neither, so its `repo.query` calls would go
    unexamined; this file examines them.

The live behaviour of the SQL those functions issue is covered by
`tests/test_pg_integration_schema.py`'s `@pytest.mark.pg` section, which has
not run on this machine. A skip is not a pass, and this file is written on the
assumption that it is the only half that has actually executed.
"""
from __future__ import annotations

import ast
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend.pg import integration_store as store

MODULE_PATH = Path(store.__file__)
SOURCE = MODULE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

UTC = timezone.utc


# ============================================== section 10.4: redaction
def test_a_regulated_key_is_removed_at_the_top_level():
    sanitised, keys, classification = store.redact_payload(
        {"bill_id": "1", "gst_no": "27ABCDE1234F1Z5"})
    assert "gst_no" not in sanitised
    assert sanitised["bill_id"] == "1"
    assert keys == ("gst_no",)
    assert classification == "REGULATED"


def test_a_regulated_key_is_removed_at_any_depth_including_inside_arrays():
    """Zoho nests. A GSTIN sits inside `contact_persons[0]` as readily as at
    the top, and a redactor that only walked the top level would be a redactor
    that looked like it worked."""
    sanitised, keys, _ = store.redact_payload({
        "vendor": {"contact_persons": [{"name": "A", "pan_no": "ABCDE1234F"},
                                       {"name": "B"}]},
    })
    persons = sanitised["vendor"]["contact_persons"]
    assert "pan_no" not in persons[0]
    assert persons[0]["name"] == "A"
    assert persons[1] == {"name": "B"}
    assert keys == ("pan_no",)


def test_restricted_outranks_regulated():
    """Section 10.4 classifies vendor bank details Restricted and GSTIN/PAN
    Regulated. A payload carrying both is Restricted: the classification is the
    HIGHEST class of any field within, not the first one found."""
    _, keys, classification = store.redact_payload(
        {"gst_no": "X", "bank_accounts": [{"account_number": "1"}]})
    assert classification == "RESTRICTED"
    assert set(keys) == {"gst_no", "bank_accounts", "account_number"}


def test_a_clean_payload_is_confidential_and_untouched():
    payload = {"bill_id": "1", "line_items": [{"rate": 100}]}
    sanitised, keys, classification = store.redact_payload(payload)
    assert sanitised == payload
    assert keys == ()
    assert classification == "CONFIDENTIAL"


def test_redaction_normalises_case_but_the_sql_backstop_does_not():
    """The asymmetry is deliberate and it is stated in both files. The redactor
    should catch `GST_No` and remove it; the constraint should NOT quietly
    accept a payload from a source whose casing nobody has characterised."""
    sanitised, keys, _ = store.redact_payload({"GST_No": "27ABCDE1234F1Z5"})
    assert "GST_No" not in sanitised
    assert keys == ("gst_no",)


def test_redaction_removes_the_key_rather_than_blanking_it():
    """The regression test for this stream's sharpest defect.

    The first draft kept the key with a `"[REDACTED]"` placeholder value, on
    the reasoning that "the vendor did not send a GSTIN" and "we removed the
    GSTIN" are different facts an operator must be able to tell apart. The
    reasoning was right and the mechanism was wrong: the backstop CHECK
    matches on KEY NAMES, so every redacted receipt would have been REFUSED --
    inbound stopping dead the first time a vendor record carried a GSTIN,
    which is a thing that would have been discovered in CI at best.

    Removal satisfies the constraint absolutely, and the distinction the
    placeholder was for now lives in `redacted_keys`, which is a column an
    operator can read and a query can filter on rather than a magic string
    buried in a JSON value.
    """
    sanitised, keys, _ = store.redact_payload({"gst_no": "X", "name": "Acme"})
    assert sanitised == {"name": "Acme"}
    assert keys == ("gst_no",)
    assert not hasattr(store, "REDACTED_PLACEHOLDER"), (
        "the placeholder is back; it contradicts "
        "ck_integration_inbox_payload_carries_no_restricted_key, which "
        "matches on key names")


@pytest.mark.parametrize("payload", [
    {"gst_no": "27ABCDE1234F1Z5"},
    {"vendor": {"pan_no": "ABCDE1234F"}},
    {"a": {"b": [{"c": {"bank_accounts": [{"account_number": "1",
                                           "ifsc": "HDFC0000001"}]}}]}},
    {"list": [{"gstin": "X"}, {"iban": "Y"}, {"swift_code": "Z"}]},
])
def test_the_redactor_output_satisfies_the_sql_backstop(payload):
    """THE INVARIANT THE TWO HALVES OF SECTION 10.4 SHARE, held without a
    database.

    `capex_payload_carries_restricted_key` scans the rendered JSON for
    `"<key>":`. That predicate is reimplemented here, character for character,
    over `redact_payload`'s output -- so a redactor that stops agreeing with
    the constraint fails HERE, on every developer machine, rather than in CI
    or in a cron function at 3am.

    This is deliberately a duplicate of what
    `test_pg_integration_schema.py::test_live_a_payload_the_redactor_produced_is_accepted`
    proves against a real database. The live one is the proof; this one is the
    one that will actually run.
    """
    import json as _json

    sanitised, _, _ = store.redact_payload(payload)
    rendered = _json.dumps(sanitised)
    for key in store.RESTRICTED_PAYLOAD_KEYS:
        assert f'"{key}":' not in rendered, (
            f"redact_payload left {key!r} in the payload; the migration's "
            f"ck_integration_inbox_payload_carries_no_restricted_key would "
            f"refuse this row and inbound would stop")


def test_redaction_does_not_mutate_the_callers_payload():
    """The caller still needs the original: `canonical_payload_sha` hashes it,
    and a redactor that scrubbed in place would change the identity of the
    document between the two calls."""
    original = {"vendor": {"gst_no": "X"}}
    store.redact_payload(original)
    assert original == {"vendor": {"gst_no": "X"}}


def test_the_two_key_lists_partition_the_whole():
    assert set(store.REGULATED_PAYLOAD_KEYS) & set(
        store.RESTRICTED_ONLY_PAYLOAD_KEYS) == set()
    assert set(store.RESTRICTED_PAYLOAD_KEYS) == (
        set(store.REGULATED_PAYLOAD_KEYS)
        | set(store.RESTRICTED_ONLY_PAYLOAD_KEYS))


def test_no_classified_key_is_hashed_by_this_module():
    """Section 10.4 withdrew v1.0's hashing of GSTIN and PAN. `payload_sha`
    hashes a whole document as an identity; nothing here digests a field."""
    for key in store.RESTRICTED_PAYLOAD_KEYS:
        assert f'["{key}"]' not in SOURCE.replace(
            "RESTRICTED_PAYLOAD_KEYS", "")


# ============================================ the idempotency hash
def test_the_payload_hash_is_insensitive_to_key_order_and_spacing():
    """Two deliveries of one document must hash the same however the transport
    ordered or spaced the JSON, or `ON CONFLICT` misses the duplicate it exists
    to catch."""
    assert (store.canonical_payload_sha({"a": 1, "b": 2})
            == store.canonical_payload_sha({"b": 2, "a": 1}))


def test_the_payload_hash_changes_when_the_document_does():
    assert (store.canonical_payload_sha({"a": 1})
            != store.canonical_payload_sha({"a": 2}))


def test_the_hash_is_taken_before_redaction_not_after():
    """If the sanitised payload were hashed, two genuinely different vendor
    records whose only difference was a removed field would collide, and the
    second would be silently dropped as a duplicate."""
    with_bank = {"vendor": "V", "account_number": "111"}
    other_bank = {"vendor": "V", "account_number": "222"}
    assert (store.canonical_payload_sha(with_bank)
            != store.canonical_payload_sha(other_bank))
    assert (store.redact_payload(with_bank)[0]
            == store.redact_payload(other_bank)[0])


# ==================================== the rate-budget windows and allocations
def test_the_minute_and_day_keys_bucket_the_same_instant():
    minute, day, minute_start, day_start = store.window_keys(
        datetime(2026, 9, 6, 14, 23, 45, 123456, tzinfo=UTC))
    assert minute == "2026-09-06T14:23"
    assert day == "2026-09-06"
    assert minute_start == datetime(2026, 9, 6, 14, 23, tzinfo=UTC)
    assert day_start == datetime(2026, 9, 6, 0, 0, tzinfo=UTC)


def test_the_day_boundary_follows_the_named_timezone():
    """WHOSE midnight the Zoho daily quota resets at is a tenant fact Phase 0B
    has not established, so the boundary is computed in a named zone and the
    zone is recorded on the row. 20:00 UTC on the 6th is already the 7th in
    Asia/Kolkata, and a budget that got that wrong would let a connection spend
    two days' quota in one."""
    _, utc_day, _, _ = store.window_keys(
        datetime(2026, 9, 6, 20, 0, tzinfo=UTC))
    _, ist_day, _, _ = store.window_keys(
        datetime(2026, 9, 6, 20, 0, tzinfo=UTC), "Asia/Kolkata")
    assert utc_day == "2026-09-06"
    assert ist_day == "2026-09-07"


def test_a_naive_datetime_is_refused():
    """A naive datetime would be bucketed in whatever zone the interpreter
    happened to be in, which on an AppSail instance is not a zone anybody
    chose."""
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.window_keys(datetime(2026, 9, 6, 14, 23))
    assert excinfo.value.code == "NAIVE_DATETIME"


def test_an_unknown_timezone_is_refused_rather_than_defaulted():
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.window_keys(datetime(2026, 9, 6, tzinfo=UTC), "Mars/Olympus")
    assert excinfo.value.code == "UNKNOWN_TIMEZONE"


def test_the_allocation_shares_are_section_11_6s_sixty_thirty_ten():
    assert store.ALLOCATION_SHARES == {
        "POLLING": 60, "OUTBOUND": 30, "INTERACTIVE": 10}
    assert sum(store.ALLOCATION_SHARES.values()) == 100
    assert set(store.ALLOCATION_SHARES) == set(store.ALLOCATIONS)


def test_the_daily_ceiling_is_split_by_the_same_shares_as_the_minute():
    """The plan states the split for the per-minute budget only. Applying it to
    the day is this stream's reading, and the argument is the plan's own, only
    more so: a backfill that spends the whole 2,000-call day before lunch
    starves the operator until midnight however politely it paced itself."""
    assert store.allocation_ceiling(2000, "POLLING") == 1200
    assert store.allocation_ceiling(2000, "OUTBOUND") == 600
    assert store.allocation_ceiling(2000, "INTERACTIVE") == 200
    assert store.allocation_ceiling(100, "POLLING") == 60


def test_an_allocation_ceiling_is_never_zero():
    """The schema requires `ceiling > 0`, and a zero-ceiling row would refuse
    every call -- which reads in the logs exactly like an open circuit."""
    assert store.allocation_ceiling(1, "INTERACTIVE") == 1


def test_an_unknown_allocation_is_refused():
    with pytest.raises(store.IntegrationStoreError):
        store.allocation_ceiling(2000, "BACKFILL")


# ================================================ the 300-second poll overlap
def test_the_poll_window_starts_before_the_watermark():
    hwm = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    until = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    start, end = store.poll_window(hwm, until=until)
    assert (hwm - start).total_seconds() == store.WATERMARK_OVERLAP_SECONDS
    assert end == until


def test_a_shortened_overlap_is_refused_rather_than_clamped():
    """Clamping would let a caller ask for 30 seconds, silently receive 300, and
    go on believing it had tuned something. The overlap is the whole protection
    against a record modified between a poll's read and its watermark write --
    contracts fact 2 means nothing downstream could detect the loss."""
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.poll_window(datetime(2026, 9, 6, tzinfo=UTC),
                          until=datetime(2026, 9, 7, tzinfo=UTC),
                          overlap_seconds=30)
    assert excinfo.value.code == "OVERLAP_BELOW_FLOOR"


# ===================================================== backoff (section 11.6)
def test_the_backoff_is_bounded_by_the_exponential_and_by_the_cap():
    rng = random.Random(20260906)
    for attempts in range(0, 12):
        ceiling = min(store.BACKOFF_CAP_SECONDS,
                      store.BACKOFF_BASE_SECONDS * (2 ** attempts))
        for _ in range(50):
            delay = store.backoff_delay_seconds(attempts, rng)
            assert 0.0 <= delay <= ceiling


def test_the_backoff_is_full_jitter_not_a_fixed_exponential():
    """Equal or absent jitter retries the whole synchronised herd at the same
    second, which is the same outage again."""
    rng = random.Random(1)
    draws = {round(store.backoff_delay_seconds(8, rng), 6) for _ in range(50)}
    assert len(draws) > 40


def test_the_backoff_never_exceeds_the_documented_cap():
    rng = random.Random(2)
    assert store.backoff_delay_seconds(1000, rng) <= store.BACKOFF_CAP_SECONDS


def test_negative_attempts_are_refused():
    with pytest.raises(store.IntegrationStoreError):
        store.backoff_delay_seconds(-1)


# ============================================ section 11.9: no live by omission
def test_a_live_mode_is_refused_before_the_session_is_touched():
    """`create_connection` validates the mode BEFORE it builds a statement, so
    the refusal does not depend on a database being reachable. `None` is passed
    as the session deliberately: if the ordering ever changed, this test would
    fail with AttributeError instead of the IntegrationStoreError it asserts,
    and the ordering is the property under test."""
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.create_connection(
            None, connection_id="C", entity_id="E", product="ERP", dc="in",
            organization_id="1", connector_name="z", actor="a",
            mode="LIVE_WRITE")
    assert excinfo.value.code == "LIVE_MODE_UNAUTHORISED"
    assert excinfo.value.status == 403


def test_a_live_mode_with_an_authoriser_but_no_note_is_still_refused():
    """An authoriser with no note records who to blame without recording what
    they agreed to."""
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.create_connection(
            None, connection_id="C", entity_id="E", product="ERP", dc="in",
            organization_id="1", connector_name="z", actor="a",
            mode="LIVE_READ", live_authorised_by="rakesh")
    assert excinfo.value.code == "LIVE_MODE_UNAUTHORISED"


def test_an_unknown_product_is_refused():
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.create_connection(
            None, connection_id="C", entity_id="E", product="ZOHO_ONE",
            dc="in", organization_id="1", connector_name="z", actor="a")
    assert excinfo.value.code == "UNKNOWN_PRODUCT"


def test_mock_and_sandbox_need_no_authorisation():
    """They reach no tenant, so requiring a stamp would train people to write
    one -- and a stamp everybody writes is a stamp nobody reads."""
    for mode in ("MOCK", "SANDBOX"):
        with pytest.raises(AttributeError):
            # Past the validation and into the (absent) session: the mode was
            # accepted, which is what this asserts.
            store.create_connection(
                None, connection_id="C", entity_id="E", product="ERP",
                dc="in", organization_id="1", connector_name="z", actor="a",
                mode=mode)


def test_the_platform_ceiling_bounds_the_soft_deadline_argument():
    with pytest.raises(store.IntegrationStoreError) as excinfo:
        store.enqueue_job(None, job_id="J", kind="poll_bills",
                          principal_user_id="SVC", actor="a", entity_id=None,
                          soft_deadline_seconds=1800)
    assert excinfo.value.code == "SOFT_DEADLINE_ABOVE_CEILING"


def test_the_plan_constants_are_the_plan_constants():
    """Named once, here, so a change to any of them is a visible diff rather
    than a number quietly edited inside one query."""
    assert store.PLATFORM_FUNCTION_CEILING_SECONDS == 900   # section 2.1
    assert store.DEFAULT_SOFT_DEADLINE_SECONDS == 720       # section 2.2, 80%
    assert store.DEFAULT_MAX_ATTEMPTS == 8                  # section 11.6
    assert store.WATERMARK_OVERLAP_SECONDS == 300           # section 11.5
    assert store.BACKOFF_CAP_SECONDS == 900                 # section 11.6


# ========================================= the scoped-query discipline, statically
def _repo_calls() -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and func.attr in {"query", "query_one"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "repo"):
            calls.append(node)
    return calls


def _sql_literal_of(call: ast.Call) -> str:
    """The statically knowable part of a call's SQL, with interpolations
    rendered as the source text of the expression that produced them."""
    first = call.args[1] if len(call.args) > 1 else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        parts = []
        for value in first.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(ast.unparse(value))
        return "".join(parts)
    return ""


def test_the_module_actually_uses_the_chokepoint():
    """A guard on an empty set proves nothing. If this module is ever rewritten
    to issue `session.fetchall` directly, the tests below would pass by
    vacuity."""
    assert len(_repo_calls()) >= 20


@pytest.mark.parametrize("call", _repo_calls(),
                         ids=lambda c: f"line{c.lineno}")
def test_every_repo_query_carries_the_scope_token(call):
    """`repo.query` raises `ScopeTokenMissing` without it, but only when the
    call runs -- and most of these run for the first time in CI's PostgreSQL
    job. This holds the same property on the source."""
    sql = _sql_literal_of(call)
    assert "{scope}" in sql or "_via_connection(" in sql, (
        f"the repo.query at line {call.lineno} has no literal {{scope}} token "
        f"and does not embed one through _via_connection()")


@pytest.mark.parametrize("call", _repo_calls(),
                         ids=lambda c: f"line{c.lineno}")
def test_every_repo_query_maps_all_four_scope_dimensions(call):
    """`repo.compile_scope` REFUSES a restricted dimension the mapping omits --
    it raises `ScopeNotExpressible` rather than silently widening -- so an
    omission is an uncaught RuntimeError in a router, which is how three of
    nine seeded demo users got a 500 on the period list in Wave 3. Naming all
    four, and waiving only by an explicit None, is what keeps that from
    happening in a cron function where nobody is watching."""
    columns = next((kw for kw in call.keywords if kw.arg == "columns"), None)
    assert columns is not None, (
        f"the repo.query at line {call.lineno} passes no columns= mapping; the "
        f"default is None, which compiles to an unconditional TRUE")

    if isinstance(columns.value, ast.Name):
        mapping = getattr(store, columns.value.id, None)
        assert isinstance(mapping, dict), (
            f"columns= at line {call.lineno} names {columns.value.id}, which "
            f"is not a module-level mapping")
        keys = set(mapping)
    elif isinstance(columns.value, ast.Dict):
        keys = {k.value for k in columns.value.keys
                if isinstance(k, ast.Constant)}
    else:
        pytest.fail(f"columns= at line {call.lineno} is neither a named "
                    f"mapping nor a dict literal, so it cannot be reviewed")

    assert keys == {"entity", "plant", "location", "project"}, (
        f"columns= at line {call.lineno} names {sorted(keys)}; every one of "
        f"entity/plant/location/project must appear, mapped to a column or "
        f"explicitly to None")


@pytest.mark.parametrize("name", ["CONNECTION_SCOPE_COLUMNS",
                                  "VIA_CONNECTION_SCOPE_COLUMNS",
                                  "JOB_SCOPE_COLUMNS"])
def test_every_published_scope_mapping_names_all_four_dimensions(name):
    assert set(getattr(store, name)) == {
        "entity", "plant", "location", "project"}


def test_every_waived_dimension_is_explained_in_the_source():
    """A dimension mapped to None is a deliberate waiver, and the module's
    contract is that it is deliberate AND explained. This checks the
    explanations exist rather than that they are good -- which is a review
    question, not a test one -- but an unexplained waiver is what an omission
    looks like after somebody tidies the comments."""
    for name in ("CONNECTION_SCOPE_COLUMNS", "VIA_CONNECTION_SCOPE_COLUMNS",
                 "JOB_SCOPE_COLUMNS"):
        index = SOURCE.index(f"{name}: dict")
        preamble = SOURCE[max(0, index - 1200):index]
        assert "waive" in preamble.lower() or "waiv" in preamble.lower(), (
            f"{name} waives a dimension with no comment saying why")


#: The local names the f-string SQL in this module interpolates besides
#: module-level constants. Supplied so the statements can be RENDERED here
#: without a database -- see the test below.
_LOCAL_INTERPOLATIONS = {
    # `list_connections` assembles this from its optional filters. The MAXIMAL
    # form is supplied, not "TRUE": rendering the empty form would make the
    # statement bind no parameters at all, and the placeholder check below
    # would then pass by having nothing to check.
    "where": "TRUE AND entity_id = %(entity_id)s AND mode = %(mode)s "
             "AND is_active",
    "ceiling_column": "daily_call_ceiling",   # ensure_rate_budget_windows
}


def _rendered_sql(call: ast.Call) -> str:
    """The call's SQL with every interpolation actually evaluated.

    `eval` on the reconstructed f-string, in the module's own globals. That is
    a strong thing to do in a test and it is the point: `_via_connection(...)`
    returns the `EXISTS (...)` clause that carries the `{scope}` token, and a
    check that only read the literal fragments would be checking half of every
    statement in this module.
    """
    namespace = dict(vars(store))
    namespace.update(_LOCAL_INTERPOLATIONS)
    return eval(ast.unparse(call.args[1]), namespace)  # noqa: S307


@pytest.mark.parametrize("call", _repo_calls(),
                         ids=lambda c: f"line{c.lineno}")
def test_every_sql_placeholder_has_a_parameter_behind_it(call):
    """`%(name)s` with nothing bound to it is a `ProgrammingError` raised when
    the statement RUNS -- which for most of this module is inside a cron
    function, in CI at the earliest. There is no live PostgreSQL on the machine
    this was written on, so this is the only place the mismatch can be caught
    before then.

    Both directions. A missing parameter fails the call outright; an unused one
    is dead weight that reads like a filter somebody thinks is being applied.
    """
    sql = _rendered_sql(call)
    placeholders = set(re.findall(r"%\((\w+)\)s", sql))

    params_node = call.args[2] if len(call.args) > 2 else None
    if params_node is None:
        assert placeholders == set(), (
            f"the repo.query at line {call.lineno} passes no params but its "
            f"SQL binds {sorted(placeholders)}")
        return
    if not isinstance(params_node, ast.Dict):
        # `list_connections` builds its params alongside its optional filter
        # clauses, so the two have to be assembled together and neither is a
        # literal. Pinned by name rather than waved through: a placeholder
        # added there that is NOT one of these still fails this test.
        assert placeholders == {"entity_id", "mode"}, (
            f"line {call.lineno} passes a computed params mapping, which only "
            f"list_connections' optional filters may do. Its SQL binds "
            f"{sorted(placeholders)}; if that is a new filter, add it here so "
            f"the exemption keeps naming exactly what it covers.")
        return
    supplied = {k.value for k in params_node.keys
                if isinstance(k, ast.Constant)}

    assert placeholders - supplied == set(), (
        f"line {call.lineno}: SQL binds {sorted(placeholders - supplied)} "
        f"with no matching parameter")
    assert supplied - placeholders == set(), (
        f"line {call.lineno}: params supply {sorted(supplied - placeholders)} "
        f"that the SQL never binds")


@pytest.mark.parametrize("call", _repo_calls(),
                         ids=lambda c: f"line{c.lineno}")
def test_every_statement_is_paren_balanced_and_carries_exactly_one_scope_token(call):
    """A rendered statement with unbalanced parentheses is a syntax error the
    server would report; a statement carrying the token twice would have the
    predicate applied twice, which is harmless, and one carrying it zero times
    is refused by `repo.query` -- at runtime, which is the problem."""
    sql = _rendered_sql(call)
    assert sql.count("(") == sql.count(")"), (
        f"line {call.lineno}: unbalanced parentheses in the rendered SQL")
    assert sql.count("{scope}") == 1, (
        f"line {call.lineno}: the rendered SQL carries "
        f"{sql.count('{scope}')} scope tokens, not one")


@pytest.mark.parametrize("call", _repo_calls(),
                         ids=lambda c: f"line{c.lineno}")
def test_every_writing_statement_returns_something(call):
    """`repo.query` ends in `session.fetchall`, and psycopg raises
    "the last operation didn't produce a result" for an INSERT or UPDATE with
    no RETURNING. Every write in this module therefore has to carry one -- and
    every one of them wants the returned row anyway, to tell "not found" from
    "out of scope"."""
    sql = _rendered_sql(call).upper()
    # The FIRST keyword, not any occurrence: `FOR UPDATE OF i SKIP LOCKED` is a
    # locking clause on a SELECT, and matching it as a write would have made
    # this test demand a RETURNING from two claim queries that are reads.
    # A data-modifying CTE opens with WITH, so that form is recognised by the
    # statement that follows the CTE's closing paren.
    is_write = bool(
        re.match(r"\s*(INSERT|UPDATE|DELETE)\b", sql)
        or (re.match(r"\s*WITH\b", sql)
            and re.search(r"\)\s*(INSERT|UPDATE|DELETE)\b", sql)))
    if not is_write:
        return
    assert "RETURNING" in sql, (
        f"line {call.lineno}: a writing statement with no RETURNING clause; "
        f"repo.query fetches, and psycopg raises on a cursor with no result")


def test_money_in_this_module_never_reaches_a_transport_table():
    """This used to read "no SQL in this module names a money column".

    That was true of `010_integration.sql`, which has no `*_paise` column on
    any table, and the guard existed to make somebody remember the cast rule
    the moment one appeared. `011_reconciliation_exception.sql` is the moment:
    `local_paise` and `source_paise` are the two sides of a discrepancy, and
    §11.8's reconciliation surface cannot be written without them.

    So the guard is not removed -- removing it would lose exactly the check it
    was placed here to trigger. It becomes the rule it was protecting, and
    gains a second half the original could not have:

      * money may appear ONLY in a statement against `reconciliation_exception`,
        so a `*_paise` on an 010 table -- an amount copied onto an outbox row,
        say, which section 11's design forbids because it would be a second copy
        that can be wrong on its own -- still fails here; and
      * wherever it appears, every `SUM()` over it is cast `::bigint`, because
        PostgreSQL's `SUM()` over `bigint` returns **numeric** and psycopg maps
        numeric to `Decimal`.

    The Decimal half is not theoretical. It shipped, and it failed where it
    hurts: `check_availability` multiplied the Decimal by a float, raised
    `TypeError`, and took down the availability verdict, both approval paths
    and the concurrency proof -- in the PostgreSQL CI job only, because a
    Decimal cannot appear without a real server.

    WIDENED AGAIN BY `013_procurement.sql`, AND STRENGTHENED IN THE SAME EDIT.

    013 created `po_line`, `grn_line` and `bill_line`, and those tables ARE the
    ledger: `amount_paise`, `non_creditable_tax_paise` and `freight_paise` are
    what ordered / received / billed are computed from. `reconciliation_lines`
    cannot be written without naming them, so the allow-list gains the
    procurement tables.

    THE THING THE GUARD WAS EVER ACTUALLY PROTECTING IS UNCHANGED AND IS NOW
    ASSERTED DIRECTLY. Its point was never "money is rare"; it was **money must
    not be copied onto a TRANSPORT row**. An `amount_paise` on an outbox row
    would be a second copy of an amount that can drift from the ledger's and be
    wrong on its own, which section 11's design forbids. Widening the
    allow-list would have let exactly that through by accident -- a statement
    joining `integration_outbox` to `po_line` names a procurement table and
    would have passed. So the transport tables are now named and BANNED
    explicitly, which the original could not do, because until this migration
    there was nothing to distinguish "an allowed money table" from "any table
    at all".
    """
    # The tables money may legitimately be read from here: 011's exception
    # table, and 013's ledger tables.
    MONEY_TABLES = ("{RECONCILIATION_EXCEPTION}", "{PO_LINE}", "{GRN_LINE}",
                    "{BILL_LINE}", "{PURCHASE_ORDER}", "{GRN}", "{BILL}",
                    "{PURCHASE_REQUEST}", "{PR_LINE}")
    # 010's transport tables. Money must NEVER appear in a statement that
    # touches one, allow-listed table in the same query or not.
    TRANSPORT_TABLES = ("{INTEGRATION_INBOX}", "{INTEGRATION_OUTBOX}",
                        "{INTEGRATION_EVENT}", "{INTEGRATION_CONNECTION}",
                        "{JOB}", "{INTEGRATION_WATERMARK}",
                        "{INTEGRATION_RATE_BUDGET}", "{INTEGRATION_CIRCUIT}")
    money_statements = 0
    for call in _repo_calls():
        sql = _sql_literal_of(call)
        if "_paise" not in sql:
            continue
        money_statements += 1
        assert any(table in sql for table in MONEY_TABLES), (
            f"line {call.lineno} names a money column in a statement against "
            f"none of {list(MONEY_TABLES)}. The ledger owns money and this "
            f"module owns transport; a second copy of an amount here is a copy "
            f"that can be wrong on its own.")
        offending = [table for table in TRANSPORT_TABLES if table in sql]
        assert not offending, (
            f"line {call.lineno} names a money column in a statement touching "
            f"{offending}. 010's tables carry no *_paise column and must never "
            f"be joined to one in the same statement: an amount that reaches a "
            f"transport row is a second copy that can drift from the ledger's "
            f"and be wrong on its own. Read the money in its own statement.")
        # The tail is a LOOKAHEAD so it is not consumed -- captured normally it
        # swallows the next `SUM(` and a statement with two paise sums reports
        # only one.
        for tail in re.findall(r"SUM\s*\([^()]*_paise[^()]*\)(?=(.{0,40}))",
                               sql, re.I | re.DOTALL):
            assert "::bigint" in tail, (
                f"line {call.lineno}: a SUM over a paise column with no "
                f"::bigint cast. psycopg will hand the caller a Decimal.")
    assert money_statements >= 1, (
        "no statement in this module names a money column any more. If the "
        "reconciliation surface moved elsewhere this guard should move with "
        "it; as written it is now asserting nothing.")


def test_the_money_guards_transport_list_is_every_010_table():
    """The ban above is only as good as the list it bans.

    `INTEGRATION_TABLES` is 010's own inventory. Deriving the check's list from
    it rather than from a hand-typed tuple means a table added to 010 later
    cannot quietly fall outside the ban -- which is exactly how a guard like
    this rots into decoration.
    """
    import inspect as _inspect

    from app.backend.pg import integration_store as _store

    source = _inspect.getsource(test_money_in_this_module_never_reaches_a_transport_table)
    missing = [name for name in _store.INTEGRATION_TABLES
               if f"{{{name.upper()}}}" not in source]
    assert not missing, (
        f"010 creates {missing}, and the transport ban above does not name "
        f"them; money could reach one of those tables and this guard would "
        f"stay green")


def test_no_sql_in_this_module_hardcodes_a_zoho_url_or_endpoint():
    """D-14 is unresolved. A product fact must never reach a caller, and the
    store is a caller."""
    lowered = SOURCE.lower()
    for forbidden in ("zohoapis", "https://", "books/v3", "inventory/v1"):
        assert forbidden not in lowered, (
            f"{forbidden!r} appears in integration_store.py; the base URL is "
            f"the adapter's, derived from `dc`, and is never persisted or "
            f"named here")


def test_the_event_id_is_never_supplied_by_this_module():
    """`integration_event.event_id` is GENERATED ALWAYS AS IDENTITY, which
    PostgreSQL rejects an explicit value for outright. Wave 4 shipped exactly
    this defect on `approval_action` and it was visible only against a live
    database."""
    insert = SOURCE[SOURCE.index("INSERT INTO {INTEGRATION_EVENT}"):]
    header = insert[:insert.index(")")]
    assert "event_id" not in header, (
        "record_event names event_id in its INSERT column list; the column is "
        "GENERATED ALWAYS AS IDENTITY and the value must come back through "
        "RETURNING")
    assert "RETURNING event_id" in insert


def test_the_reservation_savepoint_unwinds_by_raising_not_by_returning():
    """`psycopg`'s `Connection.transaction()` rolls the savepoint back only if
    the block exits by RAISING. A `return` inside it COMMITS -- which, for a
    reservation that charged one window and not the other, is the exact bug the
    savepoint is there to prevent."""
    source = SOURCE[SOURCE.index("def reserve_calls("):
                    SOURCE.index("def read_rate_budget(")]
    block = source[source.index("with session.connection.transaction():"):]
    refusal = block[:block.index("except _ReservationRefused")]
    assert "raise _ReservationRefused" in refusal
    assert re.search(r"\n\s+return granted\b", refusal), (
        "the success path must return from INSIDE the savepoint block, so the "
        "savepoint commits only when both windows were charged")


def test_the_reservation_guard_is_in_the_statement():
    """Section 2.1: no resident process. Two cron invocations reserving at the
    same instant have nowhere but the statement to arbitrate, and a
    read-then-check in Python is stale in the direction that overspends."""
    source = SOURCE[SOURCE.index("def reserve_calls("):
                    SOURCE.index("def read_rate_budget(")]
    assert "b.used + %(count)s <= b.ceiling" in source


def test_both_windows_are_created_together_or_not_at_all():
    """There is no function in this module that creates one window without the
    other, so a caller cannot end up throttled per-minute and unlimited
    per-day by forgetting an argument -- there is no argument to forget."""
    source = SOURCE[SOURCE.index("def ensure_rate_budget_windows("):
                    SOURCE.index("class _ReservationRefused")]
    assert '("MINUTE"' in source and '("DAY"' in source
    assert "for kind, key, start, seconds, ceiling_column in (" in source
