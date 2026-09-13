"""Identity flows over migration 036 (Stream D): the durable local credential,
forgot / reset password, "Continue with Zoho", account linking, break-glass.

Two stores, one rule each. SQLite (`auth.py`) holds the users, roles,
sessions and the SEEDED credential hashes; it is per-process on AppSail.
PostgreSQL (036) holds what must outlive a process: the credential a user
SET, the reset tokens (hash only), the password history, the rate-limit
ledger, the external-identity links, the OIDC round-trip state and the
handoff codes. Every PostgreSQL read or write here runs under
`Scope.system(SERVICE_USER)`; 036's policies admit nothing else.

Every refusal a caller can act on carries an RFC-7807 `code`; every state
change is an audit entry on the user's own stream (`AppUser:<user_id>`),
including the failures that matter (a bad token, a refused link).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .. import auth as auth_mod
from .. import identity_oidc as oidc
from . import audit as audit_mod
from . import notifications
from .engine import Database, Scope, Session

SERVICE_USER = "SVC-IDENTITY"
RESET_TTL = timedelta(minutes=15)
PASSWORD_MIN_LENGTH = 12
PASSWORD_HISTORY_DEPTH = 5
#: Attempts per window before the unauthenticated flows refuse.
RATE_LIMITS: dict[str, tuple[int, timedelta]] = {
    "FORGOT:user": (3, timedelta(minutes=15)),
    "FORGOT:ip": (10, timedelta(minutes=15)),
    "RESET:ip": (10, timedelta(minutes=15)),
    "OIDC_START:ip": (30, timedelta(minutes=15)),
    "OIDC_CALLBACK:ip": (30, timedelta(minutes=15)),
}
GENERIC_FORGOT_MESSAGE = ("If that account exists and has an e-mail address, a reset "
                          "link has been sent. It is valid for 15 minutes.")
AUDIT_OBJECT = "AppUser"


class IdentityError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def system_scope() -> Scope:
    return Scope.system(SERVICE_USER)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


# ============================================================== rate limiting
def note_attempt(session: Session, kind: str, key: str) -> None:
    session.execute("INSERT INTO identity_attempt (kind, attempt_key) VALUES (%s, %s)", (kind, key))


def refuse_if_limited(session: Session, kind: str, key: str, *, bucket: str) -> None:
    limit, window = RATE_LIMITS[f"{kind}:{bucket}"]
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT COUNT(*) FROM identity_attempt WHERE kind = %s AND attempt_key = %s AND at >= %s",
        (kind, key, _utcnow() - window))
    if int(row[0]) >= limit:
        raise IdentityError("RATE_LIMITED",
                            "Too many attempts. Wait a while and try again.", status=429)


# ======================================================= the durable credential
def durable_credential(session: Session, user_id: str) -> dict[str, Any] | None:
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT password_salt, password_hash, credential_version, changed_at "
        "FROM identity_credential WHERE user_id = %s", (user_id,))
    if row is None:
        return None
    return {"salt": row[0], "hash": row[1], "version": int(row[2]), "changed_at": row[3]}


def login(con: sqlite3.Connection, database: Database | None, user_id: str,
          password: str, *, correlation_id: str | None = None) -> dict[str, Any]:
    """`auth.login`, with the DURABLE credential consulted first.

    The SQLite row still decides whether the account exists and is enabled
    and still issues the session; only the hash compared may come from
    PostgreSQL, where a password the user set survives a recycle. The
    break-glass account's sign-in is audited (see :func:`break_glass_user`).
    """
    override = None
    if database is not None:
        with database.session(system_scope()) as session:
            override = durable_credential(session, user_id)
    if override is None:
        result = auth_mod.login(con, user_id, password)
    else:
        row = con.execute("SELECT c.disabled FROM app_credential c WHERE c.user_id = ?",  # scope-exempt: the SQLite identity store (auth.py's own tables), not a PostgreSQL scoped read
                          (user_id,)).fetchone()
        if not row or row["disabled"]:
            raise auth_mod.AuthError(401, "INVALID_CREDENTIALS", "Invalid user or password.")
        if not auth_mod.verify_password(password, override["salt"], override["hash"]):
            raise auth_mod.AuthError(401, "INVALID_CREDENTIALS", "Invalid user or password.")
        result = _issue_session(con, user_id)
    if database is not None and user_id == break_glass_user():
        with database.session(system_scope()) as session:
            audit_mod.append(session, user_id, "IDENTITY_BREAK_GLASS_LOGIN", AUDIT_OBJECT,
                             user_id, "the break-glass Administrator signed in with the "
                                      "local credential", correlation_id=correlation_id)
        result["break_glass"] = True
    return result


def _issue_session(con: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    sid = secrets.token_urlsafe(32)
    now = _utcnow()
    con.execute("INSERT INTO app_session (session_id, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (sid, user_id, _iso(now), _iso(now + timedelta(hours=auth_mod.SESSION_HOURS))))
    con.commit()
    return {"session_id": sid, "user": auth_mod.principal(con, user_id)}


def break_glass_user() -> str | None:
    import os
    return (os.environ.get("CAPEX_BREAK_GLASS_USER_ID") or "").strip() or None


def sqlite_user(con: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT u.user_id, u.name, c.disabled FROM app_user u "  # scope-exempt: the SQLite identity store (auth.py's own tables), not a PostgreSQL scoped read
                      "LEFT JOIN app_credential c ON c.user_id = u.user_id WHERE u.user_id = ?",
                      (user_id,)).fetchone()
    if row is None:
        return None
    return {"user_id": row["user_id"], "name": row["name"],
            "disabled": bool(row["disabled"]) if row["disabled"] is not None else True}


def revoke_sessions(con: sqlite3.Connection, user_id: str, *, keep: str | None = None) -> int:
    cur = con.execute(
        "UPDATE app_session SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"
        + (" AND session_id <> ?" if keep else ""),
        (_iso(_utcnow()), user_id, *( [keep] if keep else [] )))
    con.commit()
    return cur.rowcount


# ============================================================= password policy
_UPPER, _LOWER, _DIGIT = re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"[0-9]")


def check_password_policy(password: str, *, user_id: str) -> None:
    problems = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if not _UPPER.search(password) or not _LOWER.search(password):
        problems.append("both upper- and lower-case letters")
    if not _DIGIT.search(password):
        problems.append("a digit")
    if user_id and user_id.lower() in password.lower():
        problems.append("not your user id")
    if password.strip() != password:
        problems.append("no leading or trailing spaces")
    if problems:
        raise IdentityError("PASSWORD_POLICY",
                            "The password needs " + ", ".join(problems) + ".", status=422)


def _in_history(session: Session, user_id: str, password: str) -> bool:
    rows = session.fetchall(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT password_salt, password_hash FROM identity_password_history "
        "WHERE user_id = %s ORDER BY set_at DESC LIMIT %s", (user_id, PASSWORD_HISTORY_DEPTH))
    return any(auth_mod.verify_password(password, salt, digest) for salt, digest in rows)


def set_password(session: Session, con: sqlite3.Connection, *, user_id: str,
                 new_password: str, actor: str, reason: str,
                 seeded_salt: str | None = None, seeded_hash: str | None = None,
                 keep_session: str | None = None,
                 correlation_id: str | None = None) -> dict[str, Any]:
    """Policy, history, the durable credential, every other session revoked,
    the audit entry, the PASSWORD_CHANGED notice. The SEEDED hash (when the
    caller read it) enters the history too, so a reset back to the seed is
    refused like any other reuse."""
    check_password_policy(new_password, user_id=user_id)
    if seeded_salt and seeded_hash and auth_mod.verify_password(new_password, seeded_salt, seeded_hash):
        raise IdentityError("PASSWORD_REUSED", "That password was used before; choose a new one.",
                            status=422)
    if _in_history(session, user_id, new_password):
        raise IdentityError("PASSWORD_REUSED", "That password was used before; choose a new one.",
                            status=422)
    salt, digest = auth_mod.hash_password(new_password)
    session.execute(
        """
        INSERT INTO identity_credential (user_id, password_salt, password_hash, changed_by, change_reason)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET password_salt = EXCLUDED.password_salt,
            password_hash = EXCLUDED.password_hash, credential_version = identity_credential.credential_version + 1,
            changed_at = now(), changed_by = EXCLUDED.changed_by, change_reason = EXCLUDED.change_reason
        """, (user_id, salt, digest, actor, reason))
    session.execute(
        "INSERT INTO identity_password_history (user_id, password_salt, password_hash, set_by) "
        "VALUES (%s, %s, %s, %s)", (user_id, salt, digest, actor))
    revoked = revoke_sessions(con, user_id, keep=keep_session)
    audit_mod.append(session, actor, "IDENTITY_PASSWORD_CHANGED", AUDIT_OBJECT, user_id,
                     f"{reason}; {revoked} session(s) revoked", correlation_id=correlation_id)
    notifications.enqueue(
        session, event="PASSWORD_CHANGED",
        recipients=notifications.recipients_for_users(session, [user_id]),
        context={"user_id": user_id, "changed_at": _iso(_utcnow())},
        dedupe_key=f"PASSWORD_CHANGED:{user_id}:{_utcnow().timestamp()}",
        actor=actor, correlation_id=correlation_id, object_type=AUDIT_OBJECT, object_id=user_id)
    return {"user_id": user_id, "sessions_revoked": revoked}


# ============================================================ forgot / reset
def request_password_reset(session: Session, con: sqlite3.Connection, *, user_id: str,
                           requested_from: str, correlation_id: str | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """ALWAYS the generic answer. Internally: rate-limited per user and per
    address, a token only for an existing, enabled, addressable account,
    stored as its SHA-256, mailed through the outbox with a deep link.

    Returns the generic message and, for the CALLER's tests only, nothing
    else -- the token is never returned; it leaves this function inside the
    outbox row and nowhere else.
    """
    moment = now or _utcnow()
    user_id = (user_id or "").strip()
    # The window is counted BEFORE this attempt is noted, and a refusal is
    # raised before anything else happens: the rows that count are the
    # attempts that completed, which is what makes the limit hold.
    refuse_if_limited(session, "FORGOT", f"ip:{requested_from}", bucket="ip")
    if user_id:
        refuse_if_limited(session, "FORGOT", f"user:{user_id.upper()}", bucket="user")
    note_attempt(session, "FORGOT", f"ip:{requested_from}")
    if user_id:
        note_attempt(session, "FORGOT", f"user:{user_id.upper()}")
    account = sqlite_user(con, user_id) if user_id else None
    recipients = notifications.recipients_for_users(session, [user_id]) if user_id else []
    if account is None or account["disabled"] or not recipients:
        audit_mod.append(session, SERVICE_USER, "IDENTITY_RESET_REQUEST_IGNORED", AUDIT_OBJECT,
                         user_id or "(blank)",
                         "no enabled, addressable account; the caller saw the generic answer",
                         correlation_id=correlation_id)
        return {"message": GENERIC_FORGOT_MESSAGE}
    token = oidc.b64url(secrets.token_bytes(32))
    reset_id = f"PRS-{secrets.token_hex(6).upper()}"
    session.execute(
        "INSERT INTO identity_password_reset (reset_id, user_id, token_hash, requested_at, expires_at, "
        "requested_from, correlation_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (reset_id, user_id, oidc.sha256_hex(token), moment, moment + RESET_TTL,
         requested_from, correlation_id))
    link = notifications.public_url(f"#reset?token={token}")
    notifications.enqueue(
        session, event="PASSWORD_RESET_REQUESTED", recipients=recipients,
        context={"user_id": user_id, "link": link}, dedupe_key=f"PASSWORD_RESET_REQUESTED:{reset_id}",
        actor=SERVICE_USER, correlation_id=correlation_id, object_type=AUDIT_OBJECT, object_id=user_id)
    audit_mod.append(session, SERVICE_USER, "IDENTITY_RESET_REQUESTED", AUDIT_OBJECT, user_id,
                     f"{reset_id} issued, expires {_iso(moment + RESET_TTL)}, from {requested_from}",
                     correlation_id=correlation_id)
    return {"message": GENERIC_FORGOT_MESSAGE}


def reset_password(session: Session, con: sqlite3.Connection, *, token: str, new_password: str,
                   requested_from: str, correlation_id: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Consume the token ONCE, under a row lock, then :func:`set_password`.

    A BAD TOKEN IS RETURNED, NOT RAISED: ``{"ok": False, "code":
    "RESET_TOKEN_INVALID", ...}``. Raising would roll the transaction back
    and with it the attempt row and the refusal's audit entry -- the two
    things that make token guessing visible and rate-limited. A policy or
    reuse failure on a GOOD token still raises, so the token stays unconsumed
    for a second try with a better password.
    """
    moment = now or _utcnow()
    refuse_if_limited(session, "RESET", f"ip:{requested_from}", bucket="ip")
    note_attempt(session, "RESET", f"ip:{requested_from}")
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT reset_id, user_id, expires_at, consumed_at FROM identity_password_reset "
        "WHERE token_hash = %s FOR UPDATE", (oidc.sha256_hex((token or "").strip()),))
    if row is None or row[3] is not None or row[2] < moment:
        audit_mod.append(session, SERVICE_USER, "IDENTITY_RESET_REFUSED", AUDIT_OBJECT,
                         row[1] if row else "(unknown)",
                         ("token already used" if row and row[3] is not None else
                          "token expired" if row else "token unknown") + f"; from {requested_from}",
                         correlation_id=correlation_id)
        return {"ok": False, "code": "RESET_TOKEN_INVALID", "status": 400,
                "message": "That reset link is not valid. Request a new one."}
    reset_id, user_id = row[0], row[1]
    account = sqlite_user(con, user_id)
    if account is None or account["disabled"]:
        return {"ok": False, "code": "RESET_TOKEN_INVALID", "status": 400,
                "message": "That reset link is not valid. Request a new one."}
    seeded = con.execute("SELECT password_salt, password_hash FROM app_credential WHERE user_id = ?",  # scope-exempt: the SQLite identity store (auth.py's own tables), not a PostgreSQL scoped read
                         (user_id,)).fetchone()
    session.execute("UPDATE identity_password_reset SET consumed_at = %s WHERE reset_id = %s",
                    (moment, reset_id))
    out = set_password(session, con, user_id=user_id, new_password=new_password, actor=user_id,
                       reason=f"password reset {reset_id}",
                       seeded_salt=seeded["password_salt"] if seeded else None,
                       seeded_hash=seeded["password_hash"] if seeded else None,
                       correlation_id=correlation_id)
    return {**out, "ok": True, "reset_id": reset_id}


def change_password(session: Session, con: sqlite3.Connection, *, user_id: str,
                    current_password: str, new_password: str, keep_session: str | None,
                    correlation_id: str | None = None) -> dict[str, Any]:
    """A signed-in user changing their own password: the current one must
    verify against whichever credential is live."""
    override = durable_credential(session, user_id)
    seeded = con.execute("SELECT password_salt, password_hash, disabled FROM app_credential "  # scope-exempt: the SQLite identity store (auth.py's own tables), not a PostgreSQL scoped read
                         "WHERE user_id = ?", (user_id,)).fetchone()
    if seeded is None or seeded["disabled"]:
        raise IdentityError("INVALID_CREDENTIALS", "Invalid user or password.", status=401)
    live = (override["salt"], override["hash"]) if override else (seeded["password_salt"], seeded["password_hash"])
    if not auth_mod.verify_password(current_password, *live):
        raise IdentityError("INVALID_CREDENTIALS", "Invalid user or password.", status=401)
    return set_password(session, con, user_id=user_id, new_password=new_password, actor=user_id,
                        reason="password changed by the user", seeded_salt=seeded["password_salt"],
                        seeded_hash=seeded["password_hash"], keep_session=keep_session,
                        correlation_id=correlation_id)


# ====================================================================== OIDC
def start_oidc(session: Session, *, cfg: oidc.OidcConfig, requested_from: str,
               correlation_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    if not cfg.enabled:
        raise IdentityError("OIDC_NOT_CONFIGURED", "Continue with Zoho is not enabled on this "
                            "deployment.", status=404)
    moment = now or _utcnow()
    refuse_if_limited(session, "OIDC_START", f"ip:{requested_from}", bucket="ip")
    note_attempt(session, "OIDC_START", f"ip:{requested_from}")
    state, nonce, verifier = oidc.new_state(), oidc.new_nonce(), oidc.new_verifier()
    session.execute(
        "INSERT INTO identity_oidc_state (state, provider, nonce_hash, code_verifier, created_at, "
        "expires_at, requested_from, correlation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (state, oidc.PROVIDER, oidc.sha256_hex(nonce), verifier, moment,
         moment + timedelta(seconds=oidc.STATE_TTL_SECONDS), requested_from, correlation_id))
    return {"authorization_url": oidc.authorization_url(cfg, state=state, nonce=nonce, verifier=verifier),
            "state": state, "_nonce": nonce}


def _consume_state(session: Session, state: str, *, now: datetime) -> dict[str, Any]:
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT nonce_hash, code_verifier, expires_at, consumed_at, correlation_id FROM identity_oidc_state "
        "WHERE state = %s AND provider = %s FOR UPDATE", (state, oidc.PROVIDER))
    if row is None or row[3] is not None or row[2] < now:
        raise IdentityError("OIDC_STATE_INVALID",
                            "This sign-in did not start here, or it took too long. Start again.",
                            status=400)
    session.execute("UPDATE identity_oidc_state SET consumed_at = %s WHERE state = %s", (now, state))
    return {"nonce_hash": row[0], "verifier": row[1], "correlation_id": row[4]}


def linked_user(session: Session, *, subject: str) -> dict[str, Any] | None:
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT link_id, user_id, email FROM identity_external_link WHERE provider = %s AND subject = %s "
        "AND revoked_at IS NULL", (oidc.PROVIDER, subject))
    return None if row is None else {"link_id": row[0], "user_id": row[1], "email": row[2]}


def link_identity(session: Session, *, subject: str, user_id: str, email: str | None,
                  email_verified: bool, linked_by: str, method: str,
                  correlation_id: str | None = None) -> dict[str, Any]:
    if method not in ("ADMIN", "EMAIL_MATCH", "SELF"):
        raise IdentityError("LINK_METHOD_INVALID", f"unknown link method {method!r}", status=422)
    existing = linked_user(session, subject=subject)
    if existing is not None and existing["user_id"] != user_id:
        raise IdentityError("SUBJECT_ALREADY_LINKED",
                            "That Zoho identity is already linked to a different account.", status=409)
    if existing is not None:
        return existing
    taken = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT subject FROM identity_external_link WHERE provider = %s AND user_id = %s AND revoked_at IS NULL",
        (oidc.PROVIDER, user_id))
    if taken is not None:
        raise IdentityError("USER_ALREADY_LINKED",
                            f"{user_id} is already linked to a Zoho identity; unlink it first.", status=409)
    link_id = f"IDL-{secrets.token_hex(6).upper()}"
    session.execute(
        "INSERT INTO identity_external_link (link_id, provider, subject, user_id, email, email_verified, "
        "linked_by, link_method) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (link_id, oidc.PROVIDER, subject, user_id, email, email_verified, linked_by, method))
    audit_mod.append(session, linked_by, "IDENTITY_LINKED", AUDIT_OBJECT, user_id,
                     f"{link_id}: {oidc.PROVIDER} subject linked ({method}), e-mail {email or '(none)'}",
                     correlation_id=correlation_id)
    notifications.enqueue(
        session, event="IDENTITY_LINKED",
        recipients=notifications.recipients_for_users(session, [user_id]),
        context={"user_id": user_id, "email": email or "(none)", "linked_by": linked_by, "method": method},
        dedupe_key=f"IDENTITY_LINKED:{link_id}", actor=linked_by, correlation_id=correlation_id,
        object_type=AUDIT_OBJECT, object_id=user_id)
    return {"link_id": link_id, "user_id": user_id, "email": email}


def unlink_identity(session: Session, *, user_id: str, actor: str, reason: str,
                    correlation_id: str | None = None) -> dict[str, Any]:
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "UPDATE identity_external_link SET revoked_at = now(), revoked_by = %s, revoke_reason = %s "
        "WHERE provider = %s AND user_id = %s AND revoked_at IS NULL RETURNING link_id",
        (actor, reason, oidc.PROVIDER, user_id))
    if row is None:
        raise IdentityError("LINK_NOT_FOUND", f"{user_id} has no live Zoho identity link.", status=404)
    audit_mod.append(session, actor, "IDENTITY_UNLINKED", AUDIT_OBJECT, user_id,
                     f"{row[0]} revoked: {reason}", correlation_id=correlation_id)
    return {"link_id": row[0], "user_id": user_id}


def list_links(session: Session, user_id: str) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT link_id, provider, subject, email, email_verified, linked_at, linked_by, link_method, "
        "revoked_at, revoked_by, revoke_reason FROM identity_external_link WHERE user_id = %s "
        "ORDER BY linked_at DESC", (user_id,))
    return [{"link_id": r[0], "provider": r[1], "subject_suffix": r[2][-6:], "email": r[3],
             "email_verified": bool(r[4]), "linked_at": r[5].isoformat(), "linked_by": r[6],
             "link_method": r[7], "revoked_at": r[8].isoformat() if r[8] else None,
             "revoked_by": r[9], "revoke_reason": r[10]} for r in rows]


def complete_oidc(session: Session, con: sqlite3.Connection, *, cfg: oidc.OidcConfig,
                  state: str, code: str, requested_from: str, jwks: oidc.JwksCache,
                  opener=urllib.request.urlopen, correlation_id: str | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """The callback: state, exchange, token verification, domain policy,
    the MAPPING to an application user, a session, a one-time handoff code.

    Never auto-provisions: a subject with no link is refused unless the
    e-mail policy allows linking by a verified e-mail that matches exactly
    one active application user (opt-in, `CAPEX_OIDC_AUTO_LINK_BY_EMAIL=1`).
    Roles come from the linked application user and from nowhere in the
    token; an Administrator role is never granted by this path.
    """
    moment = now or _utcnow()
    refuse_if_limited(session, "OIDC_CALLBACK", f"ip:{requested_from}", bucket="ip")
    note_attempt(session, "OIDC_CALLBACK", f"ip:{requested_from}")
    started = _consume_state(session, state, now=moment)
    # From here every refusal is RETURNED (``ok: False``), never raised: the
    # consumed state, the attempt row and the refusal's audit entry must
    # commit, or a refused callback could be replayed.
    try:
        tokens = oidc.exchange_code(cfg, code=code, verifier=started["verifier"], opener=opener)
        header, unverified, _si, _sig = oidc.decode_unverified(tokens["id_token"])
        nonce_claim = str(unverified.get("nonce") or "")
        if oidc.sha256_hex(nonce_claim) != started["nonce_hash"]:
            raise oidc.OidcError("OIDC_NONCE_MISMATCH",
                                 "The sign-in response does not belong to this attempt.", status=401)
        claims = oidc.verify_id_token(tokens["id_token"], cfg=cfg, jwks=jwks, nonce=nonce_claim,
                                      now=moment.timestamp())
    except oidc.OidcError as exc:
        audit_mod.append(session, SERVICE_USER, "IDENTITY_SSO_REFUSED", AUDIT_OBJECT, "(unmapped)",
                         f"{exc.code}: {exc.message}", correlation_id=correlation_id)
        return {"ok": False, "code": exc.code, "status": exc.status, "message": exc.message}
    subject, email = str(claims["sub"]), (claims.get("email") or None)
    email_verified = bool(claims.get("email_verified", False))
    if not oidc.email_domain_permitted(cfg, email):
        audit_mod.append(session, SERVICE_USER, "IDENTITY_SSO_REFUSED", AUDIT_OBJECT, "(unmapped)",
                         f"domain policy refused {email or '(no e-mail)'}", correlation_id=correlation_id)
        return {"ok": False, "code": "OIDC_DOMAIN_REFUSED", "status": 403,
                "message": "That Zoho account's e-mail domain is not permitted here."}
    link = linked_user(session, subject=subject)
    if link is None and cfg.auto_link_by_email and email and email_verified:
        candidates = session.fetchall(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
            "SELECT user_id FROM app_user WHERE lower(email) = lower(%s) AND is_active "
            "AND principal_kind = 'USER'", (email,))
        if len(candidates) == 1:
            link = link_identity(session, subject=subject, user_id=candidates[0][0], email=email,
                                 email_verified=True, linked_by=SERVICE_USER, method="EMAIL_MATCH",
                                 correlation_id=correlation_id)
    if link is None:
        audit_mod.append(session, SERVICE_USER, "IDENTITY_SSO_REFUSED", AUDIT_OBJECT, "(unmapped)",
                         f"no application user is linked to this Zoho identity "
                         f"(e-mail {email or '(none)'}); an Administrator links it",
                         correlation_id=correlation_id)
        return {"ok": False, "code": "OIDC_NOT_LINKED", "status": 403,
                "message": "Your Zoho identity is not linked to a WBS account. Ask an "
                           "Administrator to link it."}
    user_id = link["user_id"]
    account = sqlite_user(con, user_id)
    if account is None or account["disabled"]:
        return {"ok": False, "code": "OIDC_ACCOUNT_DISABLED", "status": 403,
                "message": "The linked WBS account is disabled or not provisioned here."}
    if user_id == break_glass_user():
        audit_mod.append(session, SERVICE_USER, "IDENTITY_SSO_REFUSED", AUDIT_OBJECT, user_id,
                         "the break-glass account signs in with its local credential only",
                         correlation_id=correlation_id)
        return {"ok": False, "code": "OIDC_BREAK_GLASS_REFUSED", "status": 403,
                "message": "The break-glass account signs in with its local credential only."}
    issued = _issue_session(con, user_id)
    code = oidc.b64url(secrets.token_bytes(32))
    session.execute(
        "INSERT INTO identity_handoff (code_hash, user_id, session_id, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (oidc.sha256_hex(code), user_id, issued["session_id"], moment,
         moment + timedelta(seconds=oidc.HANDOFF_TTL_SECONDS)))
    audit_mod.append(session, user_id, "IDENTITY_SSO_LOGIN", AUDIT_OBJECT, user_id,
                     f"signed in with {oidc.PROVIDER} (subject …{subject[-6:]}, "
                     f"e-mail {email or '(none)'})", correlation_id=correlation_id)
    return {"ok": True, "handoff_code": code, "user_id": user_id}


def complete_handoff(session: Session, con: sqlite3.Connection, *, code: str,
                     now: datetime | None = None) -> dict[str, Any]:
    moment = now or _utcnow()
    row = session.fetchone(  # scope-exempt: system identity tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT user_id, session_id, expires_at, consumed_at FROM identity_handoff WHERE code_hash = %s "
        "FOR UPDATE", (oidc.sha256_hex((code or "").strip()),))
    if row is None or row[3] is not None or row[2] < moment:
        raise IdentityError("HANDOFF_INVALID", "That sign-in code is not valid. Start again.", status=400)
    session.execute("UPDATE identity_handoff SET consumed_at = %s WHERE code_hash = %s",
                    (moment, oidc.sha256_hex(code.strip())))
    live = con.execute("SELECT revoked_at FROM app_session WHERE session_id = ?", (row[1],)).fetchone()  # scope-exempt: the SQLite identity store (auth.py's own tables), not a PostgreSQL scoped read
    if live is None or live["revoked_at"]:
        raise IdentityError("HANDOFF_INVALID", "That sign-in code is not valid. Start again.", status=400)
    return {"session_id": row[1], "user": auth_mod.principal(con, row[0])}
