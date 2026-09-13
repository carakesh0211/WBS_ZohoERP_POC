"""Streams D and E, live (migration 036): forgot / reset password, the durable
credential, Continue with Zoho end to end, account linking, the notification
outbox with retry, dead-letter, dedupe and preferences.

SKIPPED without CAPEX_DB_URL -- a skip is not a pass. The SQLite half (users,
roles, sessions, the seeded credential) is an in-memory database built here
with exactly the tables `auth.py` reads; the PostgreSQL half is the
disposable per-test database every PG suite uses.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys as _sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))

from conftest_pg import (  # noqa: E402,F401  (re-exported as fixtures)
    pg_admin_connection, pg_connection, pg_database, pg_disposable_db_name,
    pg_scope, pg_template, pg_url,
)
import test_identity_oidc_fable51 as oidc_t  # noqa: E402

import pytest  # noqa: E402

from app.backend import auth as auth_mod  # noqa: E402
from app.backend import identity_oidc as oidc  # noqa: E402
from app.backend.integration import mail_adapter  # noqa: E402
from app.backend.pg import identity as ident  # noqa: E402
from app.backend.pg import notifications as notify  # noqa: E402

PG = pytest.mark.skipif(
    not os.environ.get("CAPEX_DB_URL"),
    reason=("PostgreSQL not configured; set CAPEX_DB_URL to run against a live "
            "database. THIS IS A SKIP, NOT A PASS."),
)
pytestmark = [pytest.mark.pg, PG]

SEED_PASSWORD = "Seeded-Pass-1234"
STRONG = "Correct-Horse-Battery-9"
STRONGER = "Another-Strong-Pass-77"


# ----------------------------------------------------------------- fixtures
def _sqlite(users: dict[str, list[str]]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE app_user (user_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
        CREATE TABLE user_role (user_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY (user_id, role));
        CREATE TABLE app_credential (user_id TEXT PRIMARY KEY, password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE app_session (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT);
    """)
    for user_id, roles in users.items():
        con.execute("INSERT INTO app_user VALUES (?, ?, ?)", (user_id, user_id.title(), roles[0]))
        for r in roles:
            con.execute("INSERT INTO user_role VALUES (?, ?)", (user_id, r))
        salt, digest = auth_mod.hash_password(SEED_PASSWORD)
        con.execute("INSERT INTO app_credential VALUES (?, ?, ?, 0, ?)",
                    (user_id, salt, digest, "2026-09-13T00:00:00"))
    con.commit()
    return con


@pytest.fixture()
def estate(pg_connection, monkeypatch):
    suffix = uuid.uuid4().hex[:8].upper()
    users = {f"U-ONE-{suffix}": ["Requestor"], f"U-ADM-{suffix}": ["Administrator"],
             f"U-BG-{suffix}": ["Administrator"], f"U-NOMAIL-{suffix}": ["Requestor"]}
    con = _sqlite(users)
    for user_id in users:
        if "NOMAIL" in user_id:
            continue
        pg_connection.execute(
            "INSERT INTO app_user (user_id, email, display_name, created_by, updated_by) "
            "VALUES (%s, %s, %s, 'T', 'T')", (user_id, f"{user_id.lower()}@athagroup.in", user_id))
    pg_connection.execute(
        "INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s, 'System Administrator', 'T')",
        (f"U-ADM-{suffix}",))
    pg_connection.commit()
    monkeypatch.setenv("CAPEX_PUBLIC_URL", "https://uat.example")
    monkeypatch.setenv("CAPEX_BREAK_GLASS_USER_ID", f"U-BG-{suffix}")
    adapter = mail_adapter.RecordingMailAdapter()
    mail_adapter.set_mail_adapter(adapter)
    yield {"con": con, "one": f"U-ONE-{suffix}", "admin": f"U-ADM-{suffix}",
           "break_glass": f"U-BG-{suffix}", "nomail": f"U-NOMAIL-{suffix}", "adapter": adapter}
    mail_adapter.set_mail_adapter(None)
    con.close()


def _sys(database):
    return database.session(ident.system_scope())


def _outbox(pg_connection, event=None):
    return pg_connection.execute(
        "SELECT notification_id, event, recipient_user_id, state, body_text, dedupe_key "
        "FROM notification_outbox" + (" WHERE event = %s" if event else "") + " ORDER BY created_at",
        (event,) if event else ()).fetchall()


def _audit(pg_connection, user_id):
    return [r[0] for r in pg_connection.execute(
        "SELECT action FROM audit_log WHERE object_type = 'AppUser' AND object_id = %s ORDER BY audit_id",
        (user_id,)).fetchall()]


def _token_from_mail(body: str) -> str:
    return re.search(r"#reset\?token=([A-Za-z0-9_\-]+)", body).group(1)


# ======================================================= forgot / reset
def test_forgot_and_reset_end_to_end_with_the_durable_credential(pg_database, pg_connection, estate):
    con, one = estate["con"], estate["one"]
    before = auth_mod.login(con, one, SEED_PASSWORD)  # a live session to be revoked

    with _sys(pg_database) as s:
        out = ident.request_password_reset(s, con, user_id=one, requested_from="10.0.0.1",
                                           correlation_id="corr-forgot")
    assert out == {"message": ident.GENERIC_FORGOT_MESSAGE}
    [(nid, event, recipient, state, body, _key)] = _outbox(pg_connection, "PASSWORD_RESET_REQUESTED")
    assert (event, recipient, state) == ("PASSWORD_RESET_REQUESTED", one, "QUEUED")
    token = _token_from_mail(body)
    stored = pg_connection.execute(
        "SELECT token_hash, consumed_at FROM identity_password_reset WHERE user_id = %s", (one,)).fetchone()
    assert stored[0] == oidc.sha256_hex(token) and stored[1] is None
    assert token not in json.dumps(pg_connection.execute(
        "SELECT * FROM identity_password_reset").fetchall(), default=str)

    with pytest.raises(ident.IdentityError) as weak:
        with _sys(pg_database) as s:
            ident.reset_password(s, con, token=token, new_password="short", requested_from="10.0.0.1")
    assert weak.value.code == "PASSWORD_POLICY"
    with pytest.raises(ident.IdentityError) as reused:
        with _sys(pg_database) as s:
            ident.reset_password(s, con, token=token, new_password=SEED_PASSWORD, requested_from="10.0.0.1")
    assert reused.value.code == "PASSWORD_REUSED"

    with _sys(pg_database) as s:
        done = ident.reset_password(s, con, token=token, new_password=STRONG, requested_from="10.0.0.1",
                                    correlation_id="corr-reset")
    assert done["sessions_revoked"] == 1
    assert con.execute("SELECT revoked_at FROM app_session WHERE session_id = ?",
                       (before["session_id"],)).fetchone()[0] is not None
    with pytest.raises(auth_mod.AuthError):
        ident.login(con, pg_database, one, SEED_PASSWORD)
    signed = ident.login(con, pg_database, one, STRONG)
    assert signed["user"]["user_id"] == one and "break_glass" not in signed
    # The SQLite seed is untouched: the durable credential overrides it.
    assert auth_mod.verify_password(SEED_PASSWORD, *con.execute(
        "SELECT password_salt, password_hash FROM app_credential WHERE user_id = ?", (one,)).fetchone())

    with _sys(pg_database) as s:
        again = ident.reset_password(s, con, token=token, new_password=STRONGER, requested_from="10.0.0.1")
    assert (again["ok"], again["code"]) == (False, "RESET_TOKEN_INVALID")
    assert [r[3] for r in _outbox(pg_connection, "PASSWORD_CHANGED")] == ["QUEUED"]
    actions = _audit(pg_connection, one)
    assert "IDENTITY_RESET_REQUESTED" in actions and "IDENTITY_PASSWORD_CHANGED" in actions
    assert "IDENTITY_RESET_REFUSED" in actions

    # History: a later reset back to STRONG is refused.
    with _sys(pg_database) as s:
        ident.request_password_reset(s, con, user_id=one, requested_from="10.0.0.2")
    body2 = _outbox(pg_connection, "PASSWORD_RESET_REQUESTED")[-1][4]
    with pytest.raises(ident.IdentityError) as hist:
        with _sys(pg_database) as s:
            ident.reset_password(s, con, token=_token_from_mail(body2), new_password=STRONG,
                                 requested_from="10.0.0.2")
    assert hist.value.code == "PASSWORD_REUSED"


def test_the_generic_answer_hides_unknown_disabled_and_unaddressed_accounts_and_rate_limits(
        pg_database, pg_connection, estate):
    con = estate["con"]
    for user_id in ("U-NOBODY", estate["nomail"], ""):
        with _sys(pg_database) as s:
            out = ident.request_password_reset(s, con, user_id=user_id, requested_from="10.0.0.9")
        assert out == {"message": ident.GENERIC_FORGOT_MESSAGE}
    assert _outbox(pg_connection, "PASSWORD_RESET_REQUESTED") == []
    assert pg_connection.execute("SELECT COUNT(*) FROM identity_password_reset").fetchone()[0] == 0
    assert "IDENTITY_RESET_REQUEST_IGNORED" in _audit(pg_connection, "U-NOBODY")

    one = estate["one"]
    for _ in range(3):
        with _sys(pg_database) as s:
            ident.request_password_reset(s, con, user_id=one, requested_from="10.0.0.10")
    with pytest.raises(ident.IdentityError) as limited:
        with _sys(pg_database) as s:
            ident.request_password_reset(s, con, user_id=one, requested_from="10.0.0.11")
    assert limited.value.code == "RATE_LIMITED" and limited.value.status == 429

    # An expired token is refused even though it is otherwise valid.
    body = _outbox(pg_connection, "PASSWORD_RESET_REQUESTED")[0][4]
    late = datetime.now(timezone.utc) + timedelta(minutes=16)
    with _sys(pg_database) as s:
        expired = ident.reset_password(s, con, token=_token_from_mail(body), new_password=STRONG,
                                       requested_from="10.0.0.12", now=late)
    assert (expired["ok"], expired["code"]) == (False, "RESET_TOKEN_INVALID")
    # Guessing is counted: the refused attempts persist and the limiter fires.
    for _ in range(10):
        with _sys(pg_database) as s:
            ident.reset_password(s, con, token="not-a-token", new_password=STRONG, requested_from="10.0.0.13")
    with pytest.raises(ident.IdentityError) as guessed:
        with _sys(pg_database) as s:
            ident.reset_password(s, con, token="not-a-token", new_password=STRONG, requested_from="10.0.0.13")
    assert guessed.value.code == "RATE_LIMITED"


def test_change_password_and_the_break_glass_login_are_audited(pg_database, pg_connection, estate):
    con, one, bg = estate["con"], estate["one"], estate["break_glass"]
    keep = auth_mod.login(con, one, SEED_PASSWORD)["session_id"]
    other = auth_mod.login(con, one, SEED_PASSWORD)["session_id"]
    with pytest.raises(ident.IdentityError) as wrong:
        with _sys(pg_database) as s:
            ident.change_password(s, con, user_id=one, current_password="nope", new_password=STRONG,
                                  keep_session=keep)
    assert wrong.value.code == "INVALID_CREDENTIALS"
    with _sys(pg_database) as s:
        out = ident.change_password(s, con, user_id=one, current_password=SEED_PASSWORD,
                                    new_password=STRONG, keep_session=keep)
    assert out["sessions_revoked"] == 1
    assert con.execute("SELECT revoked_at FROM app_session WHERE session_id = ?", (keep,)).fetchone()[0] is None
    assert con.execute("SELECT revoked_at FROM app_session WHERE session_id = ?", (other,)).fetchone()[0]
    signed = ident.login(con, pg_database, bg, SEED_PASSWORD)
    assert signed["break_glass"] is True
    assert "IDENTITY_BREAK_GLASS_LOGIN" in _audit(pg_connection, bg)


# ======================================================================= OIDC
def _cfg(**over) -> oidc.OidcConfig:
    return oidc.OidcConfig(**{**oidc_t.CFG.__dict__, **over})


class _TokenOpener:
    """The token endpoint: answers with a token signed by the test key for the
    nonce this sign-in started with (read from the stored state)."""

    def __init__(self, key, pg_connection, *, sub="zoho-sub-1", email="u-one@athagroup.in"):
        self.key, self.pg, self.sub, self.email = key, pg_connection, sub, email
        self.nonce = None

    def __call__(self, req, timeout=None):
        claims = oidc_t._claims(sub=self.sub, email=self.email, nonce=self.nonce)
        token = oidc_t._sign(self.key, {"alg": "RS256", "kid": self.key["kid"]}, claims)
        body = json.dumps({"access_token": "a", "id_token": token}).encode()

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return body
        return _Resp()


def _start(pg_database, cfg):
    with _sys(pg_database) as s:
        out = ident.start_oidc(s, cfg=cfg, requested_from="10.1.1.1")
    return out["state"], out["_nonce"]


def _complete(pg_database, con, cfg, *, state, opener, key, now=None):
    with _sys(pg_database) as s:
        return ident.complete_oidc(s, con, cfg=cfg, state=state, code="code-1", requested_from="10.1.1.1",
                                   jwks=oidc_t._jwks(key), opener=opener, now=now)


@pytest.fixture(scope="module")
def key():
    return _make_key()


def _make_key():
    p, q = oidc_t._P, oidc_t._Q
    if not (oidc_t._is_probable_prime(p) and oidc_t._is_probable_prime(q)):
        def next_prime(x):
            while not oidc_t._is_probable_prime(x):
                x += 1
            return x
        p, q = next_prime(p | 1), next_prime((q | 1) + 1000)
    n, e = p * q, 65537
    return {"n": n, "e": e, "d": pow(e, -1, (p - 1) * (q - 1)), "kid": "test-key-1"}


def test_continue_with_zoho_maps_a_linked_subject_to_a_user_and_never_grants_roles(
        pg_database, pg_connection, estate, key):
    con, one, admin = estate["con"], estate["one"], estate["admin"]
    cfg = _cfg()
    email = f"{one.lower()}@athagroup.in"
    opener = _TokenOpener(key, pg_connection, email=email)

    # Unlinked: refused by name, audited, no session.
    state, nonce = _start(pg_database, cfg)
    opener.nonce = nonce
    unlinked = _complete(pg_database, con, cfg, state=state, opener=opener, key=key)
    assert (unlinked["ok"], unlinked["code"]) == (False, "OIDC_NOT_LINKED")
    assert con.execute("SELECT COUNT(*) FROM app_session").fetchone()[0] == 0
    # The state is consumed: replaying it is refused.
    with pytest.raises(ident.IdentityError) as replay:
        _complete(pg_database, con, cfg, state=state, opener=opener, key=key)
    assert replay.value.code == "OIDC_STATE_INVALID"

    # An Administrator links the subject; the next sign-in succeeds.
    with _sys(pg_database) as s:
        link = ident.link_identity(s, subject="zoho-sub-1", user_id=one, email=email, email_verified=False,
                                   linked_by=admin, method="ADMIN", correlation_id="corr-link")
    assert link["user_id"] == one
    assert [r[3] for r in _outbox(pg_connection, "IDENTITY_LINKED")] == ["QUEUED"]
    state, nonce = _start(pg_database, cfg)
    opener.nonce = nonce
    out = _complete(pg_database, con, cfg, state=state, opener=opener, key=key)
    assert out["user_id"] == one and out["handoff_code"]
    with _sys(pg_database) as s:
        session = ident.complete_handoff(s, con, code=out["handoff_code"])
    assert session["user"]["user_id"] == one
    assert session["user"]["roles"] == ["Requestor"], "roles come from the WBS account, never the token"
    with pytest.raises(ident.IdentityError) as twice:
        with _sys(pg_database) as s:
            ident.complete_handoff(s, con, code=out["handoff_code"])
    assert twice.value.code == "HANDOFF_INVALID"
    assert "IDENTITY_SSO_LOGIN" in _audit(pg_connection, one)

    # A second link for the same user is refused; unlink is audited.
    with pytest.raises(ident.IdentityError) as dup:
        with _sys(pg_database) as s:
            ident.link_identity(s, subject="zoho-sub-9", user_id=one, email=email, email_verified=False,
                                linked_by=admin, method="ADMIN")
    assert dup.value.code == "USER_ALREADY_LINKED"
    with _sys(pg_database) as s:
        ident.unlink_identity(s, user_id=one, actor=admin, reason="left the company")
        assert ident.list_links(s, one)[0]["revoked_by"] == admin
    assert "IDENTITY_UNLINKED" in _audit(pg_connection, one)


def test_the_domain_policy_the_break_glass_rule_and_the_stale_state(pg_database, pg_connection, estate, key):
    con, one, bg, admin = estate["con"], estate["one"], estate["break_glass"], estate["admin"]
    cfg = _cfg()
    gmail = _TokenOpener(key, pg_connection, sub="zoho-sub-g", email="someone@gmail.com")
    state, nonce = _start(pg_database, cfg)
    gmail.nonce = nonce
    refused = _complete(pg_database, con, cfg, state=state, opener=gmail, key=key)
    assert (refused["ok"], refused["code"]) == (False, "OIDC_DOMAIN_REFUSED")

    with _sys(pg_database) as s:
        ident.link_identity(s, subject="zoho-sub-bg", user_id=bg, email=None, email_verified=False,
                            linked_by=admin, method="ADMIN")
    bg_opener = _TokenOpener(key, pg_connection, sub="zoho-sub-bg", email=f"{bg.lower()}@athagroup.in")
    state, nonce = _start(pg_database, cfg)
    bg_opener.nonce = nonce
    never = _complete(pg_database, con, cfg, state=state, opener=bg_opener, key=key)
    assert (never["ok"], never["code"]) == (False, "OIDC_BREAK_GLASS_REFUSED")
    assert "IDENTITY_SSO_REFUSED" in _audit(pg_connection, bg)

    state, nonce = _start(pg_database, cfg)
    opener = _TokenOpener(key, pg_connection, sub="zoho-sub-late", email=f"{one.lower()}@athagroup.in")
    opener.nonce = nonce
    with pytest.raises(ident.IdentityError) as stale:
        _complete(pg_database, con, cfg, state=state, opener=opener, key=key,
                  now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert stale.value.code == "OIDC_STATE_INVALID"
    with pytest.raises(ident.IdentityError) as off:
        _start(pg_database, _cfg(client_id=""))
    assert off.value.code == "OIDC_NOT_CONFIGURED"


def test_auto_link_by_verified_email_is_opt_in_and_exact(pg_database, pg_connection, estate, key):
    con, one = estate["con"], estate["one"]
    email = f"{one.lower()}@athagroup.in"
    opener = _TokenOpener(key, pg_connection, sub="zoho-sub-auto", email=email)
    state, nonce = _start(pg_database, _cfg())
    opener.nonce = nonce
    off = _complete(pg_database, con, _cfg(), state=state, opener=opener, key=key)
    assert off["code"] == "OIDC_NOT_LINKED", "auto-link is off by default"
    cfg = _cfg(auto_link_by_email=True)
    state, nonce = _start(pg_database, cfg)
    opener.nonce = nonce
    out = _complete(pg_database, con, cfg, state=state, opener=opener, key=key)
    assert out["user_id"] == one
    with _sys(pg_database) as s:
        [link] = [l for l in ident.list_links(s, one) if l["revoked_at"] is None]
    assert link["link_method"] == "EMAIL_MATCH" and link["email_verified"] is True


# ============================================================= notifications
def test_the_outbox_dispatches_retries_dead_letters_dedupes_and_honours_preferences(
        pg_database, pg_connection, estate):
    one, admin, adapter = estate["one"], estate["admin"], estate["adapter"]
    with _sys(pg_database) as s:
        recipients = notify.recipients_for_users(s, [one, admin, "U-GHOST"])
        assert [r[0] for r in recipients] == [admin, one]
        assert notify.recipients_for_roles(s, ["System Administrator"], exclude=[admin]) == []
        first = notify.enqueue(s, event="PR_APPROVED", recipients=recipients,
                               context={"pr_number": "PR-1", "amount": 100, "approver": admin,
                                        "link": notify.public_url("#prs?pr=PR-1")},
                               dedupe_key="PR_APPROVED:PR-1:v2", actor=admin, correlation_id="c-1",
                               object_type="PurchaseRequest", object_id="PR-1")
        again = notify.enqueue(s, event="PR_APPROVED", recipients=recipients, context={},
                               dedupe_key="PR_APPROVED:PR-1:v2", actor=admin)
    assert len(first) == 2 and again == [], "the same key lands on one row per recipient"
    rows = _outbox(pg_connection, "PR_APPROVED")
    assert all(r[3] == "QUEUED" for r in rows) and "PR-1" in rows[0][4]
    assert "(not supplied)" not in rows[0][4]

    summary = notify.dispatch_due(pg_database, adapter=adapter)
    assert (summary["claimed"], summary["sent"]) == (2, 2)
    assert len(adapter.sent) == 2 and adapter.sent[0].correlation_id == "c-1"
    assert {r[3] for r in _outbox(pg_connection, "PR_APPROVED")} == {"SENT"}

    # Retry with backoff, then dead-letter; every attempt recorded.
    adapter.fail_next = [mail_adapter.MailSendError("smtp 421", retryable=True)]
    with _sys(pg_database) as s:
        [nid] = notify.enqueue(s, event="EXPORT_COMPLETED", recipients=[(one, "x@athagroup.in")],
                               context={"export_job_id": "EXP-1", "dataset": "d", "rows": 1,
                                        "format": "xlsx", "expires_at": "soon", "link": "l"},
                               dedupe_key="EXPORT_COMPLETED:EXP-1", actor=one, max_attempts=2)
    out = notify.dispatch_due(pg_database, adapter=adapter)
    assert out["retry"] == 1
    row = pg_connection.execute(
        "SELECT state, attempts, next_attempt_at > now() FROM notification_outbox WHERE notification_id = %s",
        (nid,)).fetchone()
    assert (row[0], int(row[1]), row[2]) == ("FAILED", 1, True)
    assert notify.dispatch_due(pg_database, adapter=adapter)["claimed"] == 0, "not due yet"
    adapter.fail_next = [mail_adapter.MailSendError("bounced", retryable=False)]
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    out = notify.dispatch_due(pg_database, adapter=adapter, now=later)
    assert out["dead"] == 1
    with _sys(pg_database) as s:
        detail = notify.get_notification(s, nid)
    assert detail["state"] == "DEAD" and [d["outcome"] for d in detail["deliveries"]] == ["RETRY", "DEAD"]
    assert pg_connection.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'NOTIFICATION_DEAD' AND object_id = %s",
        (nid,)).fetchone()[0] == 1
    with _sys(pg_database) as s:
        requeued = notify.retry_dead(s, nid, actor=admin)
    assert requeued["state"] == "QUEUED" and requeued["attempts"] == 0

    # Preferences: off means SUPPRESSED; a security notice cannot be turned off.
    with _sys(pg_database) as s:
        notify.set_preference(s, user_id=one, event="PR_APPROVED", enabled=False, actor=one)
        with pytest.raises(notify.NotificationError) as mandatory:
            notify.set_preference(s, user_id=one, event="PASSWORD_CHANGED", enabled=False, actor=one)
        assert mandatory.value.code == "EVENT_MANDATORY"
        [sid] = notify.enqueue(s, event="PR_APPROVED", recipients=[(one, "x@athagroup.in")], context={},
                               dedupe_key="PR_APPROVED:PR-2:v2", actor=admin)
        prefs = {p["event"]: p["enabled"] for p in notify.get_preferences(s, one)}
        mon = notify.monitoring(s)
    assert prefs["PR_APPROVED"] is False and prefs["PASSWORD_CHANGED"] is True
    assert pg_connection.execute("SELECT state FROM notification_outbox WHERE notification_id = %s",
                                 (sid,)).fetchone()[0] == "SUPPRESSED"
    assert mon["counts"]["SUPPRESSED"] == 1 and mon["adapter"] == "recording"

    # Unavailable adapter: the row waits (RETRY), nothing is lost.
    class _Down:
        name = "catalyst_sdk"
        def available(self): return False, "sdk missing"
        def send(self, mail): raise AssertionError("never called")
    out = notify.dispatch_due(pg_database, adapter=_Down())
    assert out["retry"] == 1 and out["sent"] == 0


def test_try_enqueue_never_fails_a_business_call_for_a_deploy_defect(pg_database, pg_connection, estate):
    with _sys(pg_database) as s:
        assert notify.try_enqueue(s, event="NOT_AN_EVENT", recipients=[("U", "u@x")], context={},
                                  dedupe_key="k", actor="U-X", object_type="X", object_id="1") == []
    assert pg_connection.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'NOTIFICATION_SKIPPED'").fetchone()[0] == 1
