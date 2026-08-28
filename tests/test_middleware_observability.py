"""The HTTP middleware's observability, including the unhandled-exception path.

Review finding 2: when `call_next()` raised, the middleware recorded no
duration, no error metric and no structured detail - the single most important
request to observe was the one path with no observability at all.

Business refusals are NOT this path. `services.BusinessError`, `auth.AuthError`
and `MoneyError` are mapped to controlled 4xx by exception handlers, which is
AUD-H-008's closure and must stay that way; anything reaching the middleware's
handler is a genuine 500.
"""
from __future__ import annotations

import io
import json

import pytest
from starlette.testclient import TestClient

from app.backend import main, observability as obs


@pytest.fixture
def sink():
    stream = io.StringIO()
    obs.configure(stream=stream)
    obs.metrics.reset()
    yield stream
    obs.configure()
    obs.metrics.reset()


def records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.fixture
def exploding_app():
    """A route that raises, registered on the real app so the real middleware runs."""
    secret = "Zoho-oauthtoken 1000.SUPERSECRETTOKEN.zzz"

    @main.app.get("/api/_test_boom")
    def _boom():  # pragma: no cover - invoked through the client
        raise RuntimeError(f"upstream rejected {secret} for gst 27ABCDE1234F1Z5")

    yield secret
    main.app.router.routes = [
        r for r in main.app.router.routes
        if getattr(r, "path", None) != "/api/_test_boom"
    ]


def _call(path="/api/_test_boom"):
    # raise_server_exceptions=False so the middleware's handler runs and the
    # client sees the 500, rather than the exception propagating into the test.
    client = TestClient(main.app, raise_server_exceptions=False)
    return client.get(path, headers={"x-session": "any", "x-correlation-id": "cid-boom"})


# ---------------------------------------------------------------- happy path
def test_a_successful_request_is_recorded_with_duration(sink):
    client = TestClient(main.app)
    client.get("/api/health")
    rows = obs.metrics.snapshot()["routes"]
    assert rows, "no metric recorded for a successful request"
    assert rows[0]["requests"] == 1
    assert rows[0]["errors"] == 0


def test_a_successful_request_logs_route_status_and_duration(sink):
    client = TestClient(main.app)
    client.get("/api/health")
    req = [r for r in records(sink) if r["message"] == "request"]
    assert req, "no structured request log emitted"
    f = req[0]["fields"]
    assert f["status"] == 200
    assert f["route"] == "/api/health"
    assert isinstance(f["duration_ms"], (int, float))


# ------------------------------------------------- unhandled exception path
def test_an_unhandled_exception_is_counted_as_a_server_error(sink, exploding_app):
    assert _call().status_code == 500
    rows = {r["route"]: r for r in obs.metrics.snapshot()["routes"]}
    assert rows["/api/_test_boom"]["errors"] == 1
    assert rows["/api/_test_boom"]["requests"] == 1


def test_an_unhandled_exception_still_records_duration(sink, exploding_app):
    _call()
    row = {r["route"]: r for r in obs.metrics.snapshot()["routes"]}["/api/_test_boom"]
    assert row["p50_ms"] >= 0, "duration was not observed on the failing path"


def test_an_unhandled_exception_emits_a_structured_error_record(sink, exploding_app):
    _call()
    errs = [r for r in records(sink) if r["level"] == "ERROR"]
    assert errs, "no ERROR record emitted for an unhandled exception"
    r = errs[0]
    assert r["message"] == "unhandled exception"
    assert r["fields"]["status"] == 500
    assert r["fields"]["route"] == "/api/_test_boom"
    assert "exception" in r, "no traceback captured"
    assert "RuntimeError" in r["exception"]


def test_the_traceback_is_redacted(sink, exploding_app):
    """The highest-probability credential leak in the system: an upstream HTTP
    exception echoing the request headers that carried the token."""
    _call()
    out = sink.getvalue()
    assert "1000.SUPERSECRETTOKEN.zzz" not in out
    assert "27ABCDE1234F1Z5" not in out
    assert obs.REDACTED in out


def test_correlation_context_survives_the_exception_path(sink, exploding_app):
    """The correlation id must be on the error record and on the response, or
    the 500 cannot be traced back to the request that caused it."""
    response = _call()
    assert response.headers["X-Correlation-Id"] == "cid-boom"
    errs = [r for r in records(sink) if r["level"] == "ERROR"]
    assert errs[0]["correlation_id"] == "cid-boom"


def test_correlation_context_is_unwound_after_the_exception(sink, exploding_app):
    _call()
    assert obs.get_correlation_id() is None, (
        "correlation context leaked past the request that raised"
    )


# ----------------------------------------------------- label cardinality
def test_an_unauthenticated_request_mints_no_metric_label(sink):
    """The 401 branch runs before authentication AND before route matching, so
    the only label available is the raw path - which the caller controls.
    Labelling it would be an unbounded-cardinality memory vector on a public
    endpoint."""
    client = TestClient(main.app)
    for i in range(50):
        client.get(f"/api/nonexistent-{i}")
    assert obs.metrics.snapshot()["routes"] == []


def test_an_unmatched_authenticated_path_collapses_into_one_bucket(sink):
    client = TestClient(main.app, raise_server_exceptions=False)
    for i in range(50):
        client.get(f"/api/no-such-route-{i}", headers={"x-session": "any"})
    labels = {r["route"] for r in obs.metrics.snapshot()["routes"]}
    assert labels <= {"OTHER"}, f"unmatched paths leaked into labels: {labels}"


def test_a_five_hundred_carries_a_correlation_id_and_no_internal_detail(sink, exploding_app):
    """A user reporting "it broke" must have something to quote, and the
    response must never leak internals. Re-raising gave neither: no
    X-Correlation-Id header, and the framework's default 500 body."""
    response = _call()
    assert response.status_code == 500
    body = response.json()["detail"]
    assert body["code"] == "INTERNAL_ERROR"
    assert body["correlation_id"] == "cid-boom"
    raw = response.text
    assert "RuntimeError" not in raw, "internal exception type leaked to the client"
    assert "Traceback" not in raw
    assert "SUPERSECRETTOKEN" not in raw
