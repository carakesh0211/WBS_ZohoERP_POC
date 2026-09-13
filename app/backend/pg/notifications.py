"""The e-mail notification framework (Stream E, migration 036).

THE SHAPE
=========

* **Enqueue inside the business transaction; send outside it.** A service
  that approves a request calls :func:`enqueue` in its own transaction: the
  outbox row commits with the approval or rolls back with it, and NO mail
  is sent while a financial row is locked. :func:`dispatch_due` -- run by
  the administrator route, a cron or the in-process ticker -- claims due
  rows in one short transaction, sends through the adapter with NO
  transaction open, and records the outcome in another.
* **Durable.** The outbox and its delivery history are PostgreSQL tables
  (036); a recycle loses nothing queued.
* **Retry with backoff, then dead-letter.** A retryable failure schedules
  the next attempt at `2^attempts` minutes (capped at an hour); a
  non-retryable failure, or the last permitted attempt, moves the row to
  DEAD. Every attempt is a `notification_delivery` row.
* **Preferences.** A recipient who turned an event off gets a SUPPRESSED
  row (visible in their history) and no mail.
* **Deduplication.** `dedupe_key` is UNIQUE: the same event for the same
  object and recipient enqueued twice lands on one row.
* **Correlation.** Every row carries the request's correlation id; it is in
  the mail's metadata and in the delivery history.
* **Templates.** Stored rows (036 seeds nine); rendered with a formatter
  that answers "(not supplied)" for a missing field rather than raising
  inside somebody's transaction.
* **Deep links** are built from `CAPEX_PUBLIC_URL`; a reset link carries a
  single-use token and nothing else.

The provider is `integration/mail_adapter.py`; this module never imports a
provider. Every table here is a SERVICE-principal table (036's policies):
callers pass a session opened under `Scope.system(...)`.
"""
from __future__ import annotations

import os
import string
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..integration import mail_adapter
from . import audit as audit_mod
from .engine import Database, Scope, Session

SERVICE_USER = "SVC-NOTIFY"
STATE_QUEUED, STATE_SENDING, STATE_SENT = "QUEUED", "SENDING", "SENT"
STATE_FAILED, STATE_DEAD, STATE_SUPPRESSED = "FAILED", "DEAD", "SUPPRESSED"
DEFAULT_MAX_ATTEMPTS = 5
MAX_BACKOFF_MINUTES = 60

#: The events the product raises; each names its template (036 seeds them).
EVENTS: tuple[str, ...] = (
    "PR_SUBMITTED", "PR_APPROVED", "IMR_APPROVED", "ADMIN_SELF_APPROVAL_OVERRIDE",
    "EXPORT_COMPLETED", "PASSWORD_RESET_REQUESTED", "PASSWORD_CHANGED",
    "IDENTITY_LINKED", "EXCEPTION_RAISED",
)
#: Events a user may not turn off: security notices about their own account,
#: and the OVERSIGHT notices about other people's actions (adversarial review
#: 2026-09-13, P1: an Administrator must not be able to silence, in advance,
#: the notice that a peer overrode segregation of duties).
MANDATORY_EVENTS: frozenset[str] = frozenset({
    "PASSWORD_RESET_REQUESTED", "PASSWORD_CHANGED", "IDENTITY_LINKED",
    "ADMIN_SELF_APPROVAL_OVERRIDE", "EXCEPTION_RAISED"})
#: Events whose rendered body carries a single-use CREDENTIAL (a reset link).
#: The body is stored so the dispatcher can send it and is NEVER returned by
#: the administrator's outbox API: reading it would let any Administrator
#: reset any account (adversarial review 2026-09-13, P0). On a UAT with the
#: recording adapter -- no mail leaves -- the owner may reveal it with
#: CAPEX_UAT_REVEAL_RESET_LINKS=1, and every reveal is audited.
SENSITIVE_EVENTS: frozenset[str] = frozenset({"PASSWORD_RESET_REQUESTED"})
REDACTED_BODY = "[redacted: this message carries a single-use credential link]"
#: A row claimed longer ago than this and never recorded is an orphan of a
#: dispatcher that died mid-send; it is reclaimed (adversarial review, P2).
STALE_SENDING = timedelta(minutes=10)


class NotificationError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def system_scope() -> Scope:
    return Scope.system(SERVICE_USER)


def public_url(path: str = "") -> str:
    base = (os.environ.get("CAPEX_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/{path.lstrip('/')}" if path else base


class _Formatter(string.Formatter):
    """`{name}` from the context; a missing name renders as text, never raises."""

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "(not supplied)")
        return super().get_value(key, args, kwargs)


_FORMATTER = _Formatter()


def render(template: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[str, str, str | None]:
    ctx = {str(k): ("" if v is None else str(v)) for k, v in dict(context).items()}
    subject = _FORMATTER.vformat(template["subject_tpl"], (), ctx).replace("\n", " ").strip()
    text = _FORMATTER.vformat(template["text_tpl"], (), ctx)
    html = _FORMATTER.vformat(template["html_tpl"], (), ctx) if template.get("html_tpl") else None
    return subject, text, html


def _template(session: Session, name: str) -> dict[str, Any]:
    row = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT template, subject_tpl, text_tpl, html_tpl FROM notification_template "
        "WHERE template = %s", (name,))
    if row is None:
        raise NotificationError("TEMPLATE_UNKNOWN", f"no notification template {name!r}", status=500)
    return {"template": row[0], "subject_tpl": row[1], "text_tpl": row[2], "html_tpl": row[3]}


def _enabled(session: Session, user_id: str | None, event: str) -> bool:
    if user_id is None or event in MANDATORY_EVENTS:
        return True
    row = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT enabled FROM notification_preference WHERE user_id = %s AND event = %s "
        "AND channel = 'email'", (user_id, event))
    return True if row is None else bool(row[0])


# ------------------------------------------------------------------ recipients
def recipients_for_users(session: Session, user_ids: Iterable[str]) -> list[tuple[str, str]]:
    """`(user_id, email)` for every ACTIVE application user named. A user with
    no PostgreSQL identity row has no address and is silently absent -- the
    caller's audit entry, not a mail, is the record of the event."""
    ids = sorted({u for u in user_ids if u})
    if not ids:
        return []
    rows = session.fetchall(  # scope-exempt: the identity directory is not row-scoped; addresses of named users
        "SELECT user_id, email FROM app_user WHERE user_id = ANY(%s) AND is_active "
        "AND email <> '' ORDER BY user_id", (ids,))
    return [(r[0], r[1]) for r in rows]


def recipients_for_roles(session: Session, roles: Iterable[str],
                         exclude: Iterable[str] = (),
                         project_id: str | None = None) -> list[tuple[str, str]]:
    """Every active user holding one of the PostgreSQL catalogue roles --
    and, when the event belongs to a PROJECT, only those whose own scope
    permits that project.

    The scope rule is `repo.compile_scope`'s, restated in SQL: `read_all`
    permits everything; otherwise every dimension the user is RESTRICTED on
    must carry a grant for the project's value of that dimension, and an
    unrestricted dimension permits everything. Without this a Plant Head of
    plant B would be mailed plant A's request amounts -- the RLS boundary
    every read enforces, leaked through the outbox (adversarial review
    2026-09-13, P0).
    """
    wanted = sorted({r for r in roles if r})
    if not wanted:
        return []
    skip = {u for u in exclude if u}
    if project_id is None:
        rows = session.fetchall(  # scope-exempt: role holders from the grant table; not row-scoped data
            "SELECT DISTINCT u.user_id, u.email FROM role_grant g JOIN app_user u ON u.user_id = g.user_id "
            "WHERE g.role = ANY(%s) AND u.is_active AND u.email <> '' ORDER BY u.user_id", (wanted,))
    else:
        rows = session.fetchall(  # scope-exempt: the grant tables that DEFINE scope, evaluated for one project
            """
            SELECT DISTINCT u.user_id, u.email
            FROM role_grant g
            JOIN app_user u ON u.user_id = g.user_id
            JOIN project p ON p.project_id = %(project_id)s
            WHERE g.role = ANY(%(roles)s) AND u.is_active AND u.email <> ''
              AND (
                COALESCE((SELECT f.read_all FROM user_access_flag f WHERE f.user_id = u.user_id), false)
                OR NOT EXISTS (
                    SELECT 1 FROM user_scope_restriction r
                    WHERE r.user_id = u.user_id
                      AND NOT EXISTS (
                        SELECT 1 FROM user_scope_grant sg
                        WHERE sg.user_id = r.user_id AND sg.dimension = r.dimension
                          AND sg.scope_value = CASE r.dimension
                                WHEN 'entity' THEN p.entity_id
                                WHEN 'plant' THEN p.plant_id
                                WHEN 'location' THEN p.location_id
                                WHEN 'project' THEN p.project_id END)))
            ORDER BY u.user_id
            """, {"project_id": project_id, "roles": wanted})
    return [(r[0], r[1]) for r in rows if r[0] not in skip]


# --------------------------------------------------------------------- enqueue
def enqueue(session: Session, *, event: str, recipients: Sequence[tuple[str | None, str]],
            context: Mapping[str, Any], dedupe_key: str, actor: str = SERVICE_USER,
            correlation_id: str | None = None, object_type: str | None = None,
            object_id: str | None = None, template: str | None = None,
            max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[str]:
    """Queue one row per recipient, inside the CALLER's transaction.

    `dedupe_key` names the event, the object and the recipient; a second
    enqueue with the same key is a no-op (UNIQUE, `ON CONFLICT DO NOTHING`)
    and is NOT reported as an error -- an idempotent retry of the business
    call must not raise here. Returns the ids actually inserted.
    """
    if event not in EVENTS:
        raise NotificationError("EVENT_UNKNOWN", f"{event!r} is not a notification event", status=500)
    tpl = _template(session, template or event)
    subject, text, html = render(tpl, context)
    inserted: list[str] = []
    for user_id, email in recipients:
        if not email:
            continue
        key = f"{dedupe_key}:{user_id or email}"
        state = STATE_QUEUED if _enabled(session, user_id, event) else STATE_SUPPRESSED
        notification_id = f"NTF-{uuid.uuid4().hex[:12].upper()}"
        row = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
            """
            INSERT INTO notification_outbox (
                notification_id, event, template, recipient_user_id, recipient_email,
                subject, body_text, body_html, dedupe_key, correlation_id,
                object_type, object_id, state, max_attempts, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING notification_id
            """,
            (notification_id, event, tpl["template"], user_id, email, subject, text, html,
             key, correlation_id, object_type, object_id, state, max_attempts, actor))
        if row is None:
            continue
        inserted.append(row[0])
        if state == STATE_SUPPRESSED:
            session.execute(
                "INSERT INTO notification_delivery (notification_id, attempt, outcome, provider, detail) "
                "VALUES (%s, 0, 'SUPPRESSED', 'preference', %s)",
                (row[0], f"{user_id} has {event} turned off"))
    return inserted


def try_enqueue(session: Session, **kwargs: Any) -> list[str]:
    """`enqueue`, for the hooks inside business transactions: a missing
    template or an unknown event (deploy defects, both raised BEFORE any SQL)
    must not fail an approval. Anything the database raises still propagates
    -- that transaction is already aborted and must roll back."""
    try:
        return enqueue(session, **kwargs)
    except NotificationError as exc:
        audit_mod.append(session, kwargs.get("actor") or SERVICE_USER, "NOTIFICATION_SKIPPED",
                         kwargs.get("object_type") or "Notification",
                         kwargs.get("object_id") or kwargs.get("event", "?"),
                         f"{exc.code}: {exc.message}",
                         correlation_id=kwargs.get("correlation_id"))
        return []


# -------------------------------------------------------------------- dispatch
def _claim_due(session: Session, *, limit: int, now: datetime) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        """
        UPDATE notification_outbox o SET state = 'SENDING', attempts = attempts + 1,
                                        next_attempt_at = %(now)s
        WHERE notification_id IN (
            SELECT notification_id FROM notification_outbox
            WHERE (state IN ('QUEUED', 'FAILED') AND next_attempt_at <= %(now)s)
               OR (state = 'SENDING' AND next_attempt_at <= %(stale)s)
            ORDER BY next_attempt_at
            LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED)
        RETURNING notification_id, recipient_email, subject, body_text, body_html,
                  correlation_id, attempts, max_attempts
        """, {"now": now, "limit": limit, "stale": now - STALE_SENDING})
    return [{"notification_id": r[0], "recipient_email": r[1], "subject": r[2],
             "body_text": r[3], "body_html": r[4], "correlation_id": r[5],
             "attempts": int(r[6]), "max_attempts": int(r[7])} for r in rows]


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(MAX_BACKOFF_MINUTES, 2 ** max(0, attempts)))


def _record(session: Session, item: Mapping[str, Any], *, outcome: str, provider: str,
            detail: str, now: datetime, message_id: str | None = None) -> None:
    session.execute(
        "INSERT INTO notification_delivery (notification_id, attempt, outcome, provider, detail) "
        "VALUES (%s, %s, %s, %s, %s)",
        (item["notification_id"], item["attempts"], outcome, provider, detail[:2000]))
    if outcome in ("SENT", "RECORDED"):
        session.execute(
            "UPDATE notification_outbox SET state = 'SENT', sent_at = %s, provider = %s, "
            "provider_message_id = %s, last_error = NULL WHERE notification_id = %s",
            (now, provider, message_id, item["notification_id"]))
    elif outcome == "RETRY":
        session.execute(
            "UPDATE notification_outbox SET state = 'FAILED', last_error = %s, "
            "next_attempt_at = %s, provider = %s WHERE notification_id = %s",
            (detail[:2000], now + _backoff(item["attempts"]), provider, item["notification_id"]))
    else:
        session.execute(
            "UPDATE notification_outbox SET state = 'DEAD', last_error = %s, provider = %s "
            "WHERE notification_id = %s", (detail[:2000], provider, item["notification_id"]))


def dispatch_due(database: Database, *, adapter: mail_adapter.MailAdapter | None = None,
                 limit: int = 50, now: datetime | None = None,
                 actor: str = SERVICE_USER) -> dict[str, Any]:
    """Claim, send, record. Three transactions per batch, none of them held
    open across the provider call."""
    adapter = adapter or mail_adapter.get_mail_adapter()
    moment = now or _utcnow()
    with database.session(system_scope()) as session:
        batch = _claim_due(session, limit=limit, now=moment)
    summary = {"claimed": len(batch), "sent": 0, "retry": 0, "dead": 0,
               "provider": adapter.name}
    ok, why = adapter.available()
    for item in batch:
        outcome, detail, message_id = "SENT", "", None
        if not ok:
            outcome, detail = ("RETRY" if item["attempts"] < item["max_attempts"] else "DEAD"), (
                f"adapter {adapter.name} unavailable: {why}")
        else:
            try:
                receipt = adapter.send(mail_adapter.OutboundMail(
                    to_email=item["recipient_email"], subject=item["subject"],
                    body_text=item["body_text"], body_html=item["body_html"],
                    notification_id=item["notification_id"],
                    correlation_id=item["correlation_id"]))
                message_id = receipt.provider_message_id
                detail = receipt.detail
                outcome = "RECORDED" if adapter.name == "recording" else "SENT"
            except mail_adapter.MailSendError as exc:
                retry = exc.retryable and item["attempts"] < item["max_attempts"]
                outcome, detail = ("RETRY" if retry else "DEAD"), str(exc)
        with database.session(system_scope()) as session:
            _record(session, item, outcome=outcome, provider=adapter.name,
                    detail=detail, now=_utcnow(), message_id=message_id)
            if outcome == "DEAD":
                audit_mod.append(
                    session, actor, "NOTIFICATION_DEAD", "Notification",
                    item["notification_id"],
                    f"gave up after {item['attempts']} attempt(s): {detail[:200]}",
                    correlation_id=item["correlation_id"])
        summary["sent" if outcome in ("SENT", "RECORDED") else
                "retry" if outcome == "RETRY" else "dead"] += 1
    return summary


# ----------------------------------------------------------------------- reads
def _outbox_row(r: tuple) -> dict[str, Any]:
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    return {"notification_id": r[0], "event": r[1], "template": r[2],
            "recipient_user_id": r[3], "recipient_email": r[4], "subject": r[5],
            "state": r[6], "attempts": int(r[7]), "max_attempts": int(r[8]),
            "next_attempt_at": iso(r[9]), "last_error": r[10], "provider": r[11],
            "provider_message_id": r[12], "correlation_id": r[13],
            "object_type": r[14], "object_id": r[15], "created_at": iso(r[16]),
            "sent_at": iso(r[17])}


_OUTBOX_SELECT = """
    SELECT notification_id, event, template, recipient_user_id, recipient_email,
           subject, state, attempts, max_attempts, next_attempt_at, last_error,
           provider, provider_message_id, correlation_id, object_type, object_id,
           created_at, sent_at
    FROM notification_outbox
"""


def list_outbox(session: Session, *, state: str | None = None,
                recipient_user_id: str | None = None, event: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        _OUTBOX_SELECT + """
        WHERE (%(state)s::text IS NULL OR state = %(state)s)
          AND (%(user)s::text IS NULL OR recipient_user_id = %(user)s)
          AND (%(event)s::text IS NULL OR event = %(event)s)
        ORDER BY created_at DESC LIMIT %(limit)s
        """, {"state": state, "user": recipient_user_id, "event": event,
              "limit": max(1, min(int(limit), 1000))})
    return [_outbox_row(r) for r in rows]


def reveal_permitted() -> bool:
    """Only a UAT running the recording adapter (no mail leaves) may show a
    reset link to an Administrator, and only when the owner set the switch."""
    adapter = os.environ.get("CAPEX_MAIL_ADAPTER", "recording").strip().lower()
    return adapter == "recording" and os.environ.get("CAPEX_UAT_REVEAL_RESET_LINKS", "").strip() == "1"


def get_notification(session: Session, notification_id: str, *,
                     actor: str | None = None) -> dict[str, Any]:
    row = session.fetchone(_OUTBOX_SELECT + " WHERE notification_id = %s", (notification_id,))  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
    if row is None:
        raise NotificationError("NOTIFICATION_NOT_FOUND", f"no notification {notification_id}", status=404)
    deliveries = session.fetchall(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT attempt, at, outcome, provider, detail FROM notification_delivery "
        "WHERE notification_id = %s ORDER BY delivery_id", (notification_id,))
    body = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT body_text FROM notification_outbox WHERE notification_id = %s", (notification_id,))
    body_text = body[0] if body else None
    if row[1] in SENSITIVE_EVENTS:
        if reveal_permitted() and actor:
            audit_mod.append(session, actor, "NOTIFICATION_BODY_REVEALED", "Notification",
                             notification_id, f"{row[1]} body shown to an administrator on a "
                                              f"recording-adapter UAT (CAPEX_UAT_REVEAL_RESET_LINKS=1)")
        else:
            body_text = REDACTED_BODY
    return {**_outbox_row(row), "body_text": body_text,
            "deliveries": [{"attempt": int(d[0]), "at": d[1].isoformat(), "outcome": d[2],
                            "provider": d[3], "detail": d[4]} for d in deliveries]}


def retry_dead(session: Session, notification_id: str, *, actor: str) -> dict[str, Any]:
    row = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "UPDATE notification_outbox SET state = 'QUEUED', attempts = 0, next_attempt_at = now(), "
        "last_error = NULL WHERE notification_id = %s AND (state IN ('DEAD', 'FAILED') "
        "OR (state = 'SENDING' AND next_attempt_at <= now() - %s)) "
        "RETURNING notification_id", (notification_id, STALE_SENDING))
    if row is None:
        raise NotificationError("NOTIFICATION_NOT_RETRYABLE",
                                f"{notification_id} is not DEAD, FAILED or a stale SENDING row",
                                status=409)
    audit_mod.append(session, actor, "NOTIFICATION_REQUEUED", "Notification", notification_id,
                     "re-queued by an administrator")
    return get_notification(session, notification_id)


def monitoring(session: Session) -> dict[str, Any]:
    rows = session.fetchall(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT state, COUNT(*)::bigint FROM notification_outbox GROUP BY state")
    counts = {r[0]: int(r[1]) for r in rows}
    oldest = session.fetchone(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT MIN(next_attempt_at) FROM notification_outbox WHERE state IN ('QUEUED', 'FAILED')")
    adapter = mail_adapter.get_mail_adapter()
    ok, why = adapter.available()
    return {"counts": {s: counts.get(s, 0) for s in
                       (STATE_QUEUED, STATE_SENDING, STATE_SENT, STATE_FAILED,
                        STATE_DEAD, STATE_SUPPRESSED)},
            "oldest_due": oldest[0].isoformat() if oldest and oldest[0] else None,
            "adapter": adapter.name, "adapter_available": ok, "adapter_reason": why,
            "sender": mail_adapter.sender()[0] or None}


# ----------------------------------------------------------------- preferences
def get_preferences(session: Session, user_id: str) -> list[dict[str, Any]]:
    rows = session.fetchall(  # scope-exempt: system outbox tables (036) under the SERVICE-principal policy; not row-scoped data
        "SELECT event, enabled FROM notification_preference WHERE user_id = %s AND channel = 'email'",
        (user_id,))
    stored = {r[0]: bool(r[1]) for r in rows}
    return [{"event": e, "channel": "email", "enabled": stored.get(e, True),
             "mandatory": e in MANDATORY_EVENTS} for e in EVENTS]


def set_preference(session: Session, *, user_id: str, event: str, enabled: bool,
                   actor: str) -> dict[str, Any]:
    if event not in EVENTS:
        raise NotificationError("EVENT_UNKNOWN", f"{event!r} is not a notification event", status=422)
    if event in MANDATORY_EVENTS and not enabled:
        raise NotificationError("EVENT_MANDATORY",
                                f"{event} is a security notice about your own account and "
                                f"cannot be turned off.", status=422)
    session.execute(
        "INSERT INTO notification_preference (user_id, event, channel, enabled) VALUES (%s, %s, 'email', %s) "
        "ON CONFLICT (user_id, event, channel) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
        (user_id, event, enabled))
    audit_mod.append(session, actor, "NOTIFICATION_PREFERENCE_SET", "AppUser", user_id,
                     f"{event}: {'on' if enabled else 'off'}")
    return {"event": event, "channel": "email", "enabled": enabled,
            "mandatory": event in MANDATORY_EVENTS}
