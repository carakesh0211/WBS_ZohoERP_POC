"""Fable 5.1 - failed-login throttle on the public sign-in route.

The UAT preview is reachable from the open internet.  Before this, a caller could
try passwords at whatever rate the server answered.  Now ten failures inside a
fifteen-minute window, counted per user id and per client address, refuse the
next attempt with 429 ``LOGIN_THROTTLED`` and a ``Retry-After``; a success clears
the key; the table is an LRU that cannot grow past 10,000 keys; and the
throttled reply is the same whether or not the account exists.
"""
from __future__ import annotations

import pytest

from app.backend import login_throttle, main
from app.backend.login_throttle import LoginThrottle

GOOD_USER = "U-CFO"
GOOD_PASSWORD = GOOD_USER + "!demo"  # conftest.PASSWORD_SUFFIX


def _fail(client, user_id, n, headers=None):
    last = None
    for _ in range(n):
        last = client.post("/api/auth/login", json={"user_id": user_id, "password": "wrong"},
                           headers=headers or {})
    return last


# ---------------------------------------------------------------- route behaviour
def test_ten_failures_refuse_the_eleventh_with_429_and_retry_after(client):
    for _ in range(login_throttle.MAX_FAILURES):
        assert _fail(client, GOOD_USER, 1).status_code == 401
    resp = _fail(client, GOOD_USER, 1)
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "LOGIN_THROTTLED"
    retry = int(resp.headers["Retry-After"])
    assert 0 < retry <= login_throttle.WINDOW_SECONDS + 1


def test_a_throttled_correct_password_is_still_refused(client):
    _fail(client, GOOD_USER, login_throttle.MAX_FAILURES)
    resp = client.post("/api/auth/login", json={"user_id": GOOD_USER, "password": GOOD_PASSWORD})
    assert resp.status_code == 429, "the throttle must not be a password oracle"


def test_throttled_reply_is_identical_for_existing_and_unknown_user_ids(client, monkeypatch):
    # Retry-After is derived from the clock; freeze it so the two replies are
    # compared on their content, not on whether a second boundary fell
    # between the two requests (it did, once, in a full-suite run).
    monkeypatch.setattr(login_throttle.THROTTLE, "_clock", lambda: 1_000_000.0)
    _fail(client, GOOD_USER, login_throttle.MAX_FAILURES)
    existing = _fail(client, GOOD_USER, 1)
    # Address key is shared, so the unknown id is throttled by address alone.
    unknown = _fail(client, "U-NOBODY", 1)
    assert existing.status_code == unknown.status_code == 429
    assert existing.json() == unknown.json()
    assert existing.headers["Retry-After"] == unknown.headers["Retry-After"]


def test_user_id_key_throttles_across_addresses(client, monkeypatch):
    monkeypatch.setenv("CAPEX_TRUST_PROXY", "1")
    for i in range(login_throttle.MAX_FAILURES):
        r = _fail(client, GOOD_USER, 1, headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 401
    resp = _fail(client, GOOD_USER, 1, headers={"X-Forwarded-For": "10.0.0.250"})
    assert resp.status_code == 429


def test_address_key_throttles_across_user_ids(client, monkeypatch):
    monkeypatch.setenv("CAPEX_TRUST_PROXY", "1")
    for i in range(login_throttle.MAX_FAILURES):
        r = _fail(client, f"U-GUESS-{i}", 1, headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 401
    resp = _fail(client, "U-GUESS-X", 1, headers={"X-Forwarded-For": "203.0.113.9"})
    assert resp.status_code == 429
    # Another address, another user: not throttled.
    other = _fail(client, "U-GUESS-Y", 1, headers={"X-Forwarded-For": "203.0.113.10"})
    assert other.status_code == 401


def test_x_forwarded_for_is_ignored_unless_the_proxy_is_trusted(client, monkeypatch):
    monkeypatch.delenv("CAPEX_TRUST_PROXY", raising=False)
    for i in range(login_throttle.MAX_FAILURES):
        _fail(client, f"U-GUESS-{i}", 1, headers={"X-Forwarded-For": f"198.51.100.{i}"})
    # Rotating the header did not rotate the key: the real peer address is throttled.
    resp = _fail(client, "U-GUESS-X", 1, headers={"X-Forwarded-For": "198.51.100.200"})
    assert resp.status_code == 429


def test_a_successful_login_clears_the_key(client):
    _fail(client, GOOD_USER, login_throttle.MAX_FAILURES - 1)
    ok = client.post("/api/auth/login", json={"user_id": GOOD_USER, "password": GOOD_PASSWORD})
    assert ok.status_code == 200
    assert len(login_throttle.THROTTLE) == 0
    # A fresh run of failures is needed before the throttle engages again.
    assert _fail(client, GOOD_USER, login_throttle.MAX_FAILURES - 1).status_code == 401


def test_login_remains_a_public_path():
    assert "/api/auth/login" in main.PUBLIC_PATHS


# ---------------------------------------------------------------- the table itself
def test_table_stays_bounded_after_twenty_thousand_keys():
    t = LoginThrottle(max_keys=10_000)
    for i in range(20_000):
        t.record_failure(f"user:U-{i}")
    assert len(t) <= 10_000
    # Oldest keys were evicted, newest kept.
    assert t.retry_after("user:U-0") == 0
    for _ in range(login_throttle.MAX_FAILURES):
        t.record_failure("user:U-19999")
    assert t.retry_after("user:U-19999") > 0


def test_window_expiry_releases_the_key():
    clock = {"t": 1000.0}
    t = LoginThrottle(window=900, clock=lambda: clock["t"])
    for _ in range(login_throttle.MAX_FAILURES):
        t.record_failure("addr:1.2.3.4")
    assert t.retry_after("addr:1.2.3.4") == 901
    clock["t"] += 450
    assert t.retry_after("addr:1.2.3.4") == 451
    clock["t"] += 450
    assert t.retry_after("addr:1.2.3.4") == 0
    assert len(t) == 0


def test_retry_after_reports_the_longest_wait_across_keys():
    clock = {"t": 0.0}
    t = LoginThrottle(window=900, clock=lambda: clock["t"])
    for _ in range(login_throttle.MAX_FAILURES):
        t.record_failure("user:A")
    clock["t"] = 100
    for _ in range(login_throttle.MAX_FAILURES):
        t.record_failure("addr:B")
    assert t.retry_after("user:A", "addr:B") == 901
