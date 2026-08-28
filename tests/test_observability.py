"""Structured logging, redaction, correlation and metrics.

Remediates AUD-M-007. The redaction tests are the important ones: structured
logging lets a caller attach arbitrary fields, which is precisely how regulated
identifiers and credentials end up in a log sink.
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from app.backend import observability as obs


@pytest.fixture
def sink():
    """A logger writing structured JSON to an in-memory stream."""
    stream = io.StringIO()
    logger = obs.configure(stream=stream)
    yield logger, stream
    obs.configure()  # restore stdout handler


def records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------- structure
def test_every_record_is_one_json_object_per_line(sink):
    logger, stream = sink
    logger.info("first")
    logger.info("second")
    assert len(records(stream)) == 2


def test_record_carries_timestamp_level_logger_and_message(sink):
    logger, stream = sink
    logger.info("budget check evaluated")
    r = records(stream)[0]
    assert r["level"] == "INFO"
    assert r["logger"] == "capex"
    assert r["message"] == "budget check evaluated"
    assert r["ts"].endswith("Z")


def test_extra_fields_are_promoted_onto_the_record(sink):
    logger, stream = sink
    logger.info("po amended", extra={"po_id": "PO-014", "delta_paise": 18000000})
    r = records(stream)[0]
    assert r["fields"]["po_id"] == "PO-014"
    assert r["fields"]["delta_paise"] == 18000000


# ---------------------------------------------------------------- correlation
def test_correlation_id_is_absent_when_unbound(sink):
    logger, stream = sink
    logger.info("no request context")
    assert "correlation_id" not in records(stream)[0]


def test_correlation_id_propagates_into_every_record_in_the_block(sink):
    logger, stream = sink
    with obs.correlation("abc123def456", actor="U-PROC"):
        logger.info("step one")
        logger.info("step two")
    for r in records(stream):
        assert r["correlation_id"] == "abc123def456"
        assert r["actor"] == "U-PROC"


def test_correlation_context_is_restored_on_exit(sink):
    logger, stream = sink
    with obs.correlation("outer"):
        with obs.correlation("inner"):
            logger.info("nested")
        logger.info("back outside")
    rs = records(stream)
    assert rs[0]["correlation_id"] == "inner"
    assert rs[1]["correlation_id"] == "outer"
    assert obs.get_correlation_id() is None


def test_correlation_is_restored_even_when_the_block_raises(sink):
    with pytest.raises(RuntimeError):
        with obs.correlation("doomed"):
            raise RuntimeError("boom")
    assert obs.get_correlation_id() is None


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("field", [
    "password", "client_secret", "refresh_token", "access_token",
    "session_id", "api_key", "authorization",
])
def test_credentials_never_reach_the_sink(sink, field):
    logger, stream = sink
    logger.info("connection configured", extra={field: "super-secret-value"})
    r = records(stream)[0]
    assert r["fields"][field] == obs.REDACTED
    assert "super-secret-value" not in stream.getvalue()


@pytest.mark.parametrize("field,value", [
    ("gst_no", "27ABCDE1234F1Z5"),
    ("pan_no", "ABCDE1234F"),
    ("email", "vendor@example.com"),
    ("bank_account", "50100123456789"),
])
def test_regulated_and_personal_identifiers_are_removed_from_logs(sink, field, value):
    """Plan section 10.4: values needed for matching, compliance or reporting are
    encrypted and masked in storage - and simply REMOVED from logs. They are
    never hashed, because a hash in a log is a correlatable pseudo-identifier
    with no operational value."""
    logger, stream = sink
    logger.info("vendor synced", extra={field: value})
    assert records(stream)[0]["fields"][field] == obs.REDACTED
    assert value not in stream.getvalue()


def test_no_sensitive_field_is_hashed_anywhere(sink):
    """Regression guard against the approach withdrawn in plan v1.2."""
    logger, stream = sink
    logger.info("vendor synced", extra={"gst_no": "27ABCDE1234F1Z5"})
    out = stream.getvalue()
    assert obs.REDACTED in out
    # a hex digest of any length would indicate hashing rather than removal
    assert not any(len(tok) >= 16 and all(c in "0123456789abcdef" for c in tok)
                   for tok in out.replace('"', " ").split())


# --------------------------------------------------------- review bypasses
# Every test below reproduces a redaction bypass found in adversarial review.
# They failed against the first implementation. Do not weaken them.
SECRET_TOKEN = "1000.SUPERSECRETREFRESHTOKEN.zzz"
SECRET_GSTIN = "27ABCDE1234F1Z5"
SECRET_PAN = "ABCDE1234F"


def test_bypass_a_secret_passed_as_a_lazy_format_argument(sink):
    """log.info("gstin is %s", SECRET) - getMessage() interpolates it into the
    message before the formatter sees it, so key-based redaction never applies."""
    logger, stream = sink
    logger.info("vendor gstin is %s", SECRET_GSTIN)
    assert SECRET_GSTIN not in stream.getvalue()


def test_bypass_b_secret_interpolated_into_the_message_by_the_caller(sink):
    logger, stream = sink
    logger.warning(f"refresh_token={SECRET_TOKEN} rejected")
    assert SECRET_TOKEN not in stream.getvalue()


def test_bypass_c_secret_inside_an_exception_traceback(sink):
    """The highest-probability real leak: an HTTP client exception routinely
    echoes the request headers that carried the token."""
    logger, stream = sink
    try:
        raise ValueError(f"auth failed for client_secret={SECRET_TOKEN} gst {SECRET_GSTIN}")
    except ValueError:
        logger.exception("upstream call failed")
    out = stream.getvalue()
    assert SECRET_TOKEN not in out
    assert SECRET_GSTIN not in out


@pytest.mark.parametrize("key", [
    "client-secret", "clientSecret", "Client_Secret",
    "refreshToken", "X-Api-Key", "gstNo", "GSTIN", "panNumber",
])
def test_bypass_d_key_name_variants_are_normalised(sink, key):
    """Zoho payloads and HTTP headers use camelCase and hyphens. Exact
    lowercase matching missed every one of these."""
    logger, stream = sink
    logger.info("payload", extra={key: "leak-me-please"})
    assert "leak-me-please" not in stream.getvalue()


def test_bypass_h_secret_in_a_list_under_a_benign_key(sink):
    logger, stream = sink
    logger.info("headers", extra={"headers": ["x-api-key", f"Bearer {SECRET_TOKEN}"]})
    assert SECRET_TOKEN not in stream.getvalue()


def test_bypass_i_object_whose_repr_carries_a_secret(sink):
    """Stringification used to happen in json.dumps(default=str), i.e. AFTER
    redaction had finished. It now happens before."""
    class Conn:
        def __repr__(self):
            return f"Conn(client_secret={SECRET_TOKEN!r})"

    logger, stream = sink
    logger.info("connection", extra={"conn": Conn()})
    assert SECRET_TOKEN not in stream.getvalue()


def test_bypass_j_bytes_value(sink):
    logger, stream = sink
    logger.info("raw", extra={"body": f"gst_no={SECRET_GSTIN}".encode()})
    assert SECRET_GSTIN not in stream.getvalue()


def test_a_bare_gstin_or_pan_is_scrubbed_by_value_shape(sink):
    """Key-name matching cannot help when there is no key. Indian GSTIN and PAN
    have distinctive formats, so they are matched by shape as well."""
    logger, stream = sink
    logger.info(f"matched vendor {SECRET_GSTIN} against {SECRET_PAN}")
    out = stream.getvalue()
    assert SECRET_GSTIN not in out
    assert SECRET_PAN not in out


def test_a_non_string_dict_key_does_not_destroy_the_record(sink):
    """key.lower() on an int raised AttributeError inside format(), which
    logging swallows - dropping the record entirely. Reconciliation code uses
    id-keyed maps routinely."""
    logger, stream = sink
    logger.info("ledger", extra={"by_line": {1: "x", 2: "y"}})
    r = records(stream)[0]
    assert r["message"] == "ledger"


def test_bearer_tokens_inside_free_text_are_scrubbed(sink):
    logger, stream = sink
    logger.warning("upstream rejected Zoho-oauthtoken 1000.abcdef123456.ghijkl")
    out = stream.getvalue()
    assert "abcdef123456" not in out
    assert obs.REDACTED in out


def test_redaction_reaches_into_nested_structures(sink):
    logger, stream = sink
    logger.info("connection", extra={"conn": {"org": "60000", "client_secret": "shh"}})
    f = records(stream)[0]["fields"]
    assert f["conn"]["org"] == "60000"
    assert f["conn"]["client_secret"] == obs.REDACTED


def test_redaction_is_case_insensitive_on_field_names(sink):
    logger, stream = sink
    logger.info("hdr", extra={"Authorization": "Bearer xyz", "GST_NO": "27ABCDE1234F1Z5"})
    f = records(stream)[0]["fields"]
    assert f["Authorization"] == obs.REDACTED
    assert f["GST_NO"] == obs.REDACTED


def test_non_sensitive_business_fields_survive_untouched(sink):
    """Redaction must not be so broad that it destroys the trail it exists to keep."""
    logger, stream = sink
    logger.info("check", extra={
        "wbs_code": "CAPEX-2026-001.03", "budget_head": "Plant & Machinery",
        "available_paise": 764000, "verdict": "WITHIN_BUDGET",
    })
    f = records(stream)[0]["fields"]
    assert f["wbs_code"] == "CAPEX-2026-001.03"
    assert f["available_paise"] == 764000
    assert f["verdict"] == "WITHIN_BUDGET"


# ---------------------------------------------------------------- metrics
def test_metrics_count_requests_and_errors_per_route():
    m = obs.Metrics()
    m.observe("GET", "/api/dashboard", 200, 12.0)
    m.observe("GET", "/api/dashboard", 200, 18.0)
    m.observe("GET", "/api/dashboard", 500, 30.0)
    m.observe("POST", "/api/budget-check", 409, 9.0)
    snap = {(r["method"], r["route"]): r for r in m.snapshot()["routes"]}
    assert snap[("GET", "/api/dashboard")]["requests"] == 3
    assert snap[("GET", "/api/dashboard")]["errors"] == 1
    # a 409 is a controlled business refusal, not a server error
    assert snap[("POST", "/api/budget-check")]["errors"] == 0


def test_metrics_report_latency_percentiles():
    m = obs.Metrics()
    for ms in range(1, 101):
        m.observe("POST", "/api/budget-check", 200, float(ms))
    row = m.snapshot()["routes"][0]
    assert row["p50_ms"] == pytest.approx(51.0, abs=2)
    assert row["p95_ms"] == pytest.approx(96.0, abs=2)


def test_metrics_snapshot_states_that_it_is_not_durable():
    """AppSail kills instances after 5 minutes. Anyone reading this must not
    mistake it for a metric store."""
    assert "not a durable metric store" in obs.Metrics().snapshot()["note"]


def test_metrics_are_resettable():
    m = obs.Metrics()
    m.observe("GET", "/x", 200, 1.0)
    m.reset()
    assert m.snapshot()["routes"] == []


# ---------------------------------------------------------------- alerts
def test_every_alert_condition_has_a_severity_and_description():
    for condition, detail in obs.ALERT_CONDITIONS.items():
        assert condition.isupper()
        assert detail.startswith("P1") or detail.startswith("P2")


def test_the_two_financial_integrity_alerts_are_p1():
    assert obs.ALERT_CONDITIONS["LEDGER_DIVERGENCE"].startswith("P1")
    assert obs.ALERT_CONDITIONS["AUDIT_CHAIN_BROKEN"].startswith("P1")


def test_raising_an_alert_emits_a_structured_error_record(sink):
    logger, stream = sink
    with obs.correlation("cid-1"):
        obs.alert("LEDGER_DIVERGENCE", "cell disagrees with derived ledger",
                  wbs_id="WBS-014", budget_head_id="BH-PM")
    r = records(stream)[0]
    assert r["level"] == "ERROR"
    assert r["fields"]["alert"] == "LEDGER_DIVERGENCE"
    assert r["fields"]["severity"].startswith("P1")
    assert r["fields"]["alert_fields"]["wbs_id"] == "WBS-014"
    assert r["correlation_id"] == "cid-1"


def test_an_undefined_alert_condition_is_refused():
    """An alert with no runbook behind it is not an alert. Fail closed."""
    with pytest.raises(ValueError, match="Unknown alert condition"):
        obs.alert("SOMETHING_BAD_HAPPENED", "...")


def test_alert_payloads_are_redacted_too(sink):
    logger, stream = sink
    obs.alert("CIRCUIT_OPEN", "connection failing", refresh_token="secret-token")
    assert "secret-token" not in stream.getvalue()
    assert records(stream)[0]["fields"]["alert_fields"]["refresh_token"] == obs.REDACTED


def test_metrics_do_not_grow_without_bound(sink):
    """C2 from review: duration samples were an unbounded list per route, and
    route labels were the raw path - unbounded cardinality on a public path."""
    m = obs.Metrics()
    for i in range(5000):
        m.observe("GET", "/api/dashboard", 200, float(i))
    assert len(m.duration_ms[("GET", "/api/dashboard")]) == m.MAX_SAMPLES

    for i in range(m.MAX_ROUTES + 200):
        m.observe("GET", f"/api/route-{i}", 200, 1.0)
    assert len(m.requests) <= m.MAX_ROUTES + 2
    assert ("GET", "OVERFLOW") in m.requests


def test_alert_accepts_a_field_named_module(sink):
    """M4 from review: `module` collides with a LogRecord attribute, so the
    natural call raised KeyError instead of alerting. An alert path that throws
    is worse than no alert path."""
    logger, stream = sink
    obs.alert("DLQ_DEPTH", "queue above threshold", module="Vendors", depth=412)
    f = records(stream)[0]["fields"]
    assert f["alert"] == "DLQ_DEPTH"
    assert f["alert_fields"]["module"] == "Vendors"
    assert f["alert_fields"]["depth"] == 412


def test_the_application_installs_the_structured_formatter_at_import():
    """C1 from review, the most serious finding: nothing called configure(), so
    the 'capex' logger had no handler and an effective level of WARNING. Every
    structured record - and every redaction rule, which lives in the formatter -
    was silently discarded in the running application."""
    import logging
    from app.backend import main  # noqa: F401  - import installs it

    logger = logging.getLogger("capex")
    assert logger.handlers, "no handler installed; structured logging is inert"
    assert any(isinstance(h.formatter, obs.StructuredFormatter) for h in logger.handlers)
    assert logger.level <= logging.INFO


# ------------------------------------------------- protected root keys (fix 1)
@pytest.mark.parametrize("protected", [
    # "message" is absent deliberately: Python's logging refuses it one layer
    # earlier, at makeRecord. See the companion test below.
    "level", "logger", "correlation_id", "actor", "exception", "ts",
])
def test_a_caller_cannot_overwrite_a_protected_root_key(sink, protected):
    """Merging `extra` into the root object let a caller shadow any root key.

    A record claiming level=INFO for an error, or carrying a forged
    correlation_id or actor, is worse than no record: it is evidence that reads
    as trustworthy and is not. Caller fields now nest under `fields`.
    """
    logger, stream = sink
    with obs.correlation("real-cid", actor="U-REAL"):
        logger.error("boom", extra={protected: "FORGED"})
    r = records(stream)[0]
    assert r.get(protected) != "FORGED", f"caller overwrote root key {protected!r}"
    assert r["fields"][protected] == "FORGED", "caller value should be preserved, nested"


def test_protected_root_keys_keep_their_real_values_under_attack(sink):
    logger, stream = sink
    with obs.correlation("real-cid", actor="U-REAL"):
        logger.error("boom", extra={"level": "DEBUG", "actor": "U-IMPOSTOR",
                                    "correlation_id": "forged-cid"})
    r = records(stream)[0]
    assert r["level"] == "ERROR"
    assert r["actor"] == "U-REAL"
    assert r["correlation_id"] == "real-cid"


def test_the_protected_key_set_is_exactly_what_the_formatter_emits(sink):
    """If the formatter grows a root key, it must be protected too."""
    logger, stream = sink
    with obs.correlation("cid", actor="U-1"):
        try:
            raise ValueError("x")
        except ValueError:
            logger.exception("failed", extra={"a": 1})
    assert set(records(stream)[0]) <= obs.PROTECTED_ROOT_KEYS


def test_nested_caller_fields_are_still_redacted(sink):
    logger, stream = sink
    logger.info("conn", extra={"client_secret": "shh"})
    assert records(stream)[0]["fields"]["client_secret"] == obs.REDACTED


def test_stdlib_logging_refuses_to_overwrite_its_own_record_attributes(sink):
    """Defence in depth, and worth knowing where each layer sits.

    `message`, `args`, `module`, `filename` and friends are LogRecord
    attributes, so logging raises before the formatter is ever reached. The
    formatter's PROTECTED_ROOT_KEYS covers the keys logging does NOT defend -
    `level`, `logger`, `correlation_id`, `actor`, `exception`, `ts` - which are
    ours, not the stdlib's.

    This is also why `alert()` nests its caller fields: `module` is used
    throughout this codebase for the Zoho sync module.
    """
    logger, _ = sink
    for reserved in ("message", "args", "module", "filename", "levelname"):
        with pytest.raises(KeyError, match="Attempt to overwrite"):
            logger.info("x", extra={reserved: "forged"})
